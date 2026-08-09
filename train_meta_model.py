"""
train_meta_model.py — LightGBM でメタモデルを訓練・評価
=========================================================

meta_train_bt.csv を読み込み、LightGBM 分類器で
「このシグナルは勝つか (y=1)?」を予測するモデルを訓練する。

【評価方法】(ユーザー案: 直近N日をフォワード期間として使う)
  各ホールドアウト期間 (30〜180日) ごとに:
    - split_<N>=train で学習
    - split_<N>=test で評価
  → 6期間の AUC / リフトを横断比較して汎化性を確認

【実際のトレードへの転用方法】
  モデルが出す確率スコア (0〜1) を「BTスコア」の後継として使う。
  スコアが高いほど勝ちやすい → 発注する銘柄の選別フィルターになる。
  具体的な使い方は --threshold で確認できる。

使い方:
  python train_meta_model.py                     # 6期間全評価 + モデル保存
  python train_meta_model.py --eval-only         # 評価だけ (既存モデルを読む)
  python train_meta_model.py --horizon 180       # 180日ホールドアウトで最終モデル保存
  python train_meta_model.py --threshold 0.55    # スコア≥閾値のシグナルの成績を表示
  python train_meta_model.py --input other.csv   # 入力ファイル指定
  python train_meta_model.py --aggressive        # aggressive 用モデル

依存:
  pip install lightgbm scikit-learn
"""

from __future__ import annotations

import argparse
import csv
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HOLDOUT_DAYS = [30, 60, 90, 120, 150, 180]

# 特徴量列 (すべて signal_date 時点で確定 → リーク無し)
# 過学習源の signal_month と、定数で死んでいた rr_ratio/entry_type は除外。
FEATURE_COLS = [
    "strategy",      # カテゴリ
    "family",        # カテゴリ
    "regime",        # カテゴリ: up/flat/down
    "signal_dow",    # 整数 0-4 (曜日)
    "atr_ratio",     # 連続値: ボラ水準
    "vol_ratio",     # 連続値: 出来高スパイク
    "stop_pct",      # 連続値: 損切り幅%
    "target_pct",    # 連続値: 目標幅%
    # ── トレーリング成績 (BTスコアの本体) ──
    "tr_n_prior",    # 過去トレード数
    "tr_winrate",    # 直近20件の勝率
    "tr_pf",         # 直近20件のPF
    "tr_loss_streak",# 現在の連敗数
    "tr_avg_hold",   # 直近20件の平均保有日数 (速度)
]

CAT_COLS = ["strategy", "family", "regime"]

# 最終モデルの保存先
def _model_path(suffix: str = "") -> Path:
    return Path(f"meta_model{suffix}.pkl")


# ── データ読み込み & 前処理 ────────────────────────────────────────
def load_data(csv_path: Path) -> "pd.DataFrame":
    import pandas as pd
    import numpy as np

    print(f"データ読み込み: {csv_path}")
    df = pd.read_csv(csv_path, dtype=str, low_memory=False)
    print(f"  {len(df):,} 行, {len(df.columns)} 列")

    # 数値列を変換 (空欄 → NaN)
    num_cols = ["signal_month", "signal_dow", "atr_ratio", "vol_ratio",
                "rr_ratio", "stop_pct", "target_pct", "pnl", "hold_days", "days_ago",
                "tr_n_prior", "tr_winrate", "tr_pf", "tr_loss_streak", "tr_avg_hold"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # y を整数に
    df["y"] = pd.to_numeric(df["y"], errors="coerce").astype("Int64")

    # カテゴリ列: 空欄 → "unknown"
    for col in CAT_COLS:
        if col in df.columns:
            df[col] = df[col].fillna("unknown").astype("category")

    # split 列を文字列に
    for n in HOLDOUT_DAYS:
        col = f"split_{n}"
        if col in df.columns:
            df[col] = df[col].fillna("train")

    return df


def make_xy(df: "pd.DataFrame") -> tuple:
    """特徴量行列 X と ターゲット y を返す。"""
    import pandas as pd
    cols = [c for c in FEATURE_COLS if c in df.columns]
    X = df[cols].copy()
    y = df["y"].astype(int)
    return X, y


# ── LightGBM 学習 ─────────────────────────────────────────────────
def train_lgbm(X_train, y_train, cat_cols: list[str]):
    """LightGBM 分類器を学習して返す。"""
    import lightgbm as lgb

    cat_features = [c for c in cat_cols if c in X_train.columns]

    params = dict(
        objective="binary",
        metric="auc",
        num_leaves=31,
        learning_rate=0.05,
        n_estimators=400,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=0.1,
        random_state=42,
        verbose=-1,
    )
    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train, y_train,
        categorical_feature=cat_features if cat_features else "auto",
    )
    return model


# ── 評価 ─────────────────────────────────────────────────────────
def evaluate(model, X_test, y_test, horizon: int,
             threshold: float | None = None) -> dict:
    """AUC / Accuracy / スコア十分位別リフト表を返す。"""
    import numpy as np
    from sklearn.metrics import roc_auc_score, accuracy_score

    prob = model.predict_proba(X_test)[:, 1]
    y_arr = y_test.to_numpy()

    auc  = roc_auc_score(y_arr, prob) if len(set(y_arr)) > 1 else float("nan")
    acc  = accuracy_score(y_arr, (prob >= 0.5).astype(int))
    base = y_arr.mean()

    # スコア十分位別の実際の勝率 (リフト表)
    order = np.argsort(-prob)   # スコア高い順
    n = len(order)
    decile_rows = []
    for q in range(10):
        s = int(q * n / 10)
        e = int((q + 1) * n / 10)
        idx = order[s:e]
        wr = y_arr[idx].mean()
        threshold_lo = float(prob[idx].min())
        decile_rows.append(dict(
            decile=q + 1,
            n=len(idx),
            win_rate=round(wr * 100, 1),
            score_lo=round(threshold_lo, 3),
        ))

    # 閾値別分析 (--threshold 指定時 & 固定閾値群)
    thresholds = sorted({0.50, 0.52, 0.55, 0.58, 0.60, 0.65,
                         *([] if threshold is None else [threshold])})
    thr_rows = []
    for thr in thresholds:
        mask = prob >= thr
        n_sel = mask.sum()
        if n_sel == 0:
            continue
        wr = y_arr[mask].mean()
        thr_rows.append(dict(
            threshold=thr,
            n_selected=int(n_sel),
            pct_selected=round(n_sel / n * 100, 1),
            win_rate=round(wr * 100, 1),
            lift=round(wr / base, 3) if base > 0 else float("nan"),
        ))

    return dict(
        horizon=horizon,
        n_test=n,
        auc=round(auc, 4),
        accuracy=round(acc * 100, 1),
        base_win_rate=round(base * 100, 1),
        decile_rows=decile_rows,
        threshold_rows=thr_rows,
    )


def print_eval(ev: dict) -> None:
    n  = ev["n_test"]
    print(f"\n  ▶ ホールドアウト {ev['horizon']}日  "
          f"(test {n:,}件 / base勝率 {ev['base_win_rate']}%)")
    print(f"    AUC={ev['auc']}  Accuracy={ev['accuracy']}%")

    # スコア十分位 (上位3位だけ強調表示)
    rows = ev["decile_rows"]
    print(f"    {'十分位':>5}{'件数':>7}{'勝率':>8}{'スコア下限':>12}")
    print(f"    " + "-" * 34)
    for r in rows:
        mark = " ◀" if r["decile"] <= 3 else ""
        print(f"    {r['decile']:>3}位  {r['n']:>6,}  {r['win_rate']:>6.1f}%"
              f"  {r['score_lo']:>9.3f}{mark}")

    # 閾値別
    print(f"\n    {'閾値':>7}{'件数':>8}{'%':>6}{'勝率':>8}{'リフト':>8}")
    print(f"    " + "-" * 40)
    for r in ev["threshold_rows"]:
        print(f"    {r['threshold']:>6.2f}  {r['n_selected']:>7,}"
              f"  {r['pct_selected']:>5.1f}%"
              f"  {r['win_rate']:>6.1f}%"
              f"  {r['lift']:>7.3f}")


def print_importance(model, feature_names: list[str]) -> None:
    import numpy as np
    imp = model.feature_importances_
    order = np.argsort(-imp)
    print("\n  【特徴量重要度 (gain)】")
    for i in order:
        bar = "█" * int(imp[i] / max(imp) * 20)
        print(f"    {feature_names[i]:<14} {imp[i]:>6.0f}  {bar}")


# ── メイン ───────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="LightGBM メタモデル 訓練・評価")
    ap.add_argument("--input", default=None,
                    help="入力 CSV (既定: meta_train_bt[_aggressive].csv)")
    ap.add_argument("--aggressive", action="store_true")
    ap.add_argument("--eval-only", action="store_true",
                    help="既存モデルを読み込んで評価だけ実施")
    ap.add_argument("--horizon", type=int, default=180,
                    help="最終モデルを保存する際に使うホールドアウト期間 (既定: 180)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="スコア閾値: ≥threshold の勝率/件数を強調表示 (例: 0.55)")
    ap.add_argument("--holdouts", default=None,
                    help="評価する期間 (カンマ区切り, 既定: 全6期間)")
    args = ap.parse_args()

    try:
        import lightgbm  # noqa: F401
    except ImportError:
        print("[ERROR] lightgbm が見つかりません。", file=sys.stderr)
        print("  pip install lightgbm scikit-learn", file=sys.stderr)
        sys.exit(1)

    suffix   = "_aggressive" if args.aggressive else ""
    csv_path = Path(args.input) if args.input else Path(f"meta_train_bt{suffix}.csv")
    if not csv_path.exists():
        print(f"[ERROR] {csv_path} が見つかりません。先に build_meta_dataset_bt.py を実行してください。",
              file=sys.stderr)
        sys.exit(1)

    if args.holdouts:
        holdouts = sorted({int(x) for x in args.holdouts.split(",") if x.strip()})
    else:
        holdouts = [n for n in HOLDOUT_DAYS if f"split_{n}" in
                    _peek_columns(csv_path)]
    if not holdouts:
        holdouts = HOLDOUT_DAYS

    print("=" * 78)
    print(f"メタモデル 訓練・評価")
    print(f"  入力    : {csv_path}  (ホールドアウト: {holdouts})")
    print(f"  モード  : {'評価のみ' if args.eval_only else '訓練+評価'}")
    print("=" * 78)

    df = load_data(csv_path)
    X_all, y_all = make_xy(df)
    feature_names = list(X_all.columns)

    eval_results: list[dict] = []
    final_model = None
    final_model_horizon = None

    for n in holdouts:
        col = f"split_{n}"
        if col not in df.columns:
            print(f"  ⚠ {col} 列がありません — スキップ")
            continue

        mask_train = df[col] == "train"
        mask_test  = df[col] == "test"
        n_tr = mask_train.sum()
        n_te = mask_test.sum()
        if n_tr < 50 or n_te < 10:
            print(f"  ⚠ {n}日: train={n_tr}, test={n_te} — 少なすぎるためスキップ")
            continue

        print(f"\n[ ホールドアウト {n}日 ]  train={n_tr:,}  test={n_te:,}")

        if args.eval_only:
            mp = _model_path(f"{suffix}_{n}d")
            if not mp.exists():
                print(f"  モデルファイルが見つかりません: {mp} — スキップ")
                continue
            with open(mp, "rb") as f:
                model = pickle.load(f)
            print(f"  モデル読み込み: {mp}")
        else:
            print(f"  学習中...", end="", flush=True)
            model = train_lgbm(X_all[mask_train], y_all[mask_train], CAT_COLS)
            print(f" 完了")
            # 期間別モデルを保存 (後で個別評価できるよう)
            mp = _model_path(f"{suffix}_{n}d")
            with open(mp, "wb") as f:
                pickle.dump(model, f)
            print(f"  保存: {mp}")

        ev = evaluate(model, X_all[mask_test], y_all[mask_test],
                      n, threshold=args.threshold)
        eval_results.append(ev)
        print_eval(ev)

        if n == args.horizon:
            final_model = model
            final_model_horizon = n

    # 最終モデル (horizon 指定分) を標準名で保存
    if final_model is not None and not args.eval_only:
        fp = _model_path(suffix)
        with open(fp, "wb") as f:
            pickle.dump(final_model, f)
        print(f"\n最終モデル ({final_model_horizon}日ホールドアウト) 保存: {fp}")
        print_importance(final_model, feature_names)

    # ── 横断サマリー ──
    if len(eval_results) >= 2:
        print("\n" + "=" * 78)
        print("【ホールドアウト横断サマリー】")
        print(f"  {'期間':>5}{'test件数':>10}{'AUC':>8}{'Acc%':>8}{'base勝率':>10}{'top2割勝率':>12}")
        print("  " + "-" * 55)
        for ev in eval_results:
            top20_wr = ev["decile_rows"][1]["win_rate"] if len(ev["decile_rows"]) > 1 else "-"
            print(f"  {ev['horizon']:>4}日  {ev['n_test']:>8,}"
                  f"  {ev['auc']:>7.4f}  {ev['accuracy']:>6.1f}%"
                  f"  {ev['base_win_rate']:>8.1f}%  {top20_wr:>10}%")

        best = max(eval_results, key=lambda e: e["auc"])
        print(f"\n  ベストAUC: {best['horizon']}日ホールドアウト ({best['auc']})")

    print("\n次のステップ:")
    print("  1. スコア≥0.55 など閾値を決めて発注銘柄を絞る")
    print("     → python train_meta_model.py --threshold 0.55")
    print("  2. run_signals.py のスコア列に meta_model.pkl の予測値を追加する")


def _peek_columns(csv_path: Path) -> list[str]:
    """CSV の先頭行だけ読んで列名リストを返す。"""
    with open(csv_path, encoding="utf-8", newline="") as f:
        return csv.DictReader(f).fieldnames or []


if __name__ == "__main__":
    main()
