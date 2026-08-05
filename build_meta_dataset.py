"""
build_meta_dataset.py — forward_test_log.csv からメタモデル訓練データを生成
=============================================================================

forward_test_log.csv に溜まったシグナル結果を読み込み、
メタモデル (LightGBM/XGBoost) の訓練用 CSV を生成します。

【特徴量】
  基本属性:
    strategy      カテゴリ (MACD/A7/RSI2/DON/VOL/MOM)
    family        カテゴリ (stop/breakout)
    signal_month  整数 1-12 (季節性)
    signal_dow    整数 0-4 (曜日 0=月)
  相場環境 (シグナル日時点):
    regime        カテゴリ (up/flat/down) — N225 MA25/MA75
    atr_ratio     連続値 ATR/終値 (ボラティリティ水準)
    vol_ratio     連続値 出来高/20日平均 (出来高スパイク)
  リスクリワード構造:
    rr_ratio      連続値 (target-order)/(order-stop) = R:R 比
    stop_pct      連続値 (order-stop)/order (損切り幅 %)
    target_pct    連続値 (target-order)/order (目標幅 %)

【ターゲット変数】
    y = 1  pnl > 0 (目標達成 or 早期利確)
    y = 0  pnl <= 0 (損切り or タイムカット損)

対象: status が target / stop / timeout の決済済み行のみ
      (pending / filled / holding / expired は除外)

使い方:
  python build_meta_dataset.py                        # conservative ログ
  python build_meta_dataset.py --aggressive           # aggressive ログ
  python build_meta_dataset.py --log custom.csv       # 任意ログ指定
  python build_meta_dataset.py --out meta_train.csv   # 出力先変更
  python build_meta_dataset.py --stats                # 特徴量統計を表示
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path


SETTLED_STATUSES = {"target", "stop", "timeout"}

# 出力列の定義順
OUT_FIELDS = [
    # 識別子 (訓練には使わないが追跡用)
    "record_date", "symbol", "name", "strategy", "family",
    "signal_date", "fill_date", "exit_date",
    # 特徴量
    "signal_month", "signal_dow",
    "regime", "atr_ratio", "vol_ratio",
    "rr_ratio", "stop_pct", "target_pct",
    # メタ情報
    "status", "pnl",
    # ターゲット
    "y",
]


def _float(v: str, default: float = float("nan")) -> float:
    try:
        return float(v) if v != "" else default
    except (ValueError, TypeError):
        return default


def _build_row(raw: dict) -> dict | None:
    """CSV の 1 行を特徴量行に変換。変換不可なら None を返す。"""
    status = raw.get("status", "")
    if status not in SETTLED_STATUSES:
        return None

    pnl = _float(raw.get("pnl", ""))
    if pnl != pnl:  # NaN
        return None

    order_price  = _float(raw.get("order_price",  ""))
    stop_price   = _float(raw.get("stop_price",   ""))
    target_price = _float(raw.get("target_price", ""))

    # リスクリワード構造
    risk   = order_price - stop_price   if order_price and stop_price  else float("nan")
    reward = target_price - order_price if order_price and target_price else float("nan")

    rr_ratio   = round(reward / risk, 3) if risk and risk > 0 else ""
    stop_pct   = round(risk   / order_price * 100, 3) if order_price and risk else ""
    target_pct = round(reward / order_price * 100, 3) if order_price and reward else ""

    # 曜日・月
    signal_month, signal_dow = "", ""
    sd = raw.get("signal_date", "")
    if sd:
        try:
            dt = datetime.strptime(sd, "%Y-%m-%d")
            signal_month = dt.month
            signal_dow   = dt.weekday()
        except ValueError:
            pass

    return {
        "record_date":  raw.get("record_date", ""),
        "symbol":       raw.get("symbol", ""),
        "name":         raw.get("name", ""),
        "strategy":     raw.get("strategy", ""),
        "family":       raw.get("family", ""),
        "signal_date":  sd,
        "fill_date":    raw.get("fill_date", ""),
        "exit_date":    raw.get("exit_date", ""),
        "signal_month": signal_month,
        "signal_dow":   signal_dow,
        "regime":       raw.get("regime", ""),
        "atr_ratio":    raw.get("atr_ratio", ""),
        "vol_ratio":    raw.get("vol_ratio", ""),
        "rr_ratio":     rr_ratio,
        "stop_pct":     stop_pct,
        "target_pct":   target_pct,
        "status":       status,
        "pnl":          round(pnl),
        "y":            1 if pnl > 0 else 0,
    }


def load_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        print(f"[ERROR] ログが見つかりません: {log_path}", file=sys.stderr)
        sys.exit(1)
    with open(log_path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build(log_path: Path, out_path: Path, show_stats: bool) -> None:
    print(f"ログ読み込み: {log_path}")
    rows = load_log(log_path)
    print(f"  全行数: {len(rows)}")

    # 決済済みのみ、重複排除 (同 symbol+strategy+signal_date は最新1件)
    unique: dict[tuple, dict] = {}
    for r in rows:
        if r.get("status", "") not in SETTLED_STATUSES:
            continue
        key = (r.get("symbol", ""), r.get("strategy", ""), r.get("signal_date", ""))
        cur = unique.get(key)
        if cur is None or r.get("updated_date", "") > cur.get("updated_date", ""):
            unique[key] = r

    print(f"  決済済みユニーク: {len(unique)} 件")

    out_rows: list[dict] = []
    skipped = 0
    for raw in unique.values():
        row = _build_row(raw)
        if row is None:
            skipped += 1
            continue
        out_rows.append(row)

    if skipped:
        print(f"  スキップ (変換不可): {skipped} 件")

    if not out_rows:
        print("[WARNING] 出力行が 0 件です。300〜500 件の決済済みトレードが溜まるまで待ってください。")
        return

    # 時系列順にソート (walk-forward 分割のため)
    out_rows.sort(key=lambda r: (r.get("signal_date", ""), r.get("symbol", "")))

    # 保存
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        for r in out_rows:
            writer.writerow({k: r.get(k, "") for k in OUT_FIELDS})

    print(f"\n保存: {out_path.resolve()}  ({len(out_rows)} 件)")

    # ── 統計サマリー ──
    n = len(out_rows)
    n_win  = sum(r["y"] for r in out_rows)
    n_loss = n - n_win
    pnl_total = sum(float(r["pnl"]) for r in out_rows)

    print(f"\n【データセット概要】")
    print(f"  総トレード数 : {n}")
    print(f"  勝ち (y=1)  : {n_win}  ({n_win/n*100:.1f}%)")
    print(f"  負け (y=0)  : {n_loss}  ({n_loss/n*100:.1f}%)")
    print(f"  合計損益    : {pnl_total:+,.0f} 円")

    # regime 分布
    regimes = [r["regime"] for r in out_rows if r["regime"]]
    if regimes:
        from collections import Counter
        rc = Counter(regimes)
        print(f"\n  regime 分布 : {dict(rc)}")
        regime_covered = len(regimes) / n * 100
        if regime_covered < 80:
            print(f"  ⚠ regime が空の行が {100-regime_covered:.0f}% あります。")
            print(f"    古いログ行には regime が記録されていません。")
            print(f"    今日以降のログには自動記録されます。")

    # atr_ratio / vol_ratio のカバレッジ
    n_atr = sum(1 for r in out_rows if r.get("atr_ratio", "") != "")
    n_vol = sum(1 for r in out_rows if r.get("vol_ratio", "") != "")
    print(f"\n  atr_ratio カバレッジ: {n_atr}/{n} ({n_atr/n*100:.0f}%)")
    print(f"  vol_ratio カバレッジ: {n_vol}/{n} ({n_vol/n*100:.0f}%)")

    if show_stats:
        print(f"\n【戦略別集計】")
        from collections import defaultdict
        strat_stats: dict[str, list] = defaultdict(list)
        for r in out_rows:
            strat_stats[r["strategy"]].append(r["y"])
        print(f"  {'戦略':<8}{'件数':>6}{'勝率':>8}")
        print(f"  " + "-" * 24)
        for strat, ys in sorted(strat_stats.items()):
            wr = sum(ys) / len(ys) * 100 if ys else 0
            print(f"  {strat:<8}{len(ys):>6}{wr:>7.1f}%")

    if n < 300:
        needed = 300 - n
        print(f"\n  ℹ️  メタモデル訓練には 300〜500 件推奨。あと約 {needed} 件必要です。")
        print(f"     毎日 `python forward_test.py --record` を実行してください。")
    else:
        print(f"\n  ✓ {n} 件 — メタモデルの訓練を開始できます。")
        print(f"    次のステップ: LightGBM で walk-forward 時系列分割で訓練 → meta_model.pkl を保存")


def main() -> None:
    ap = argparse.ArgumentParser(description="メタモデル訓練データを生成")
    ap.add_argument("--log",        default=None, help="入力ログ CSV (デフォルト: forward_test_log.csv)")
    ap.add_argument("--out",        default=None, help="出力 CSV (デフォルト: meta_train[_aggressive].csv)")
    ap.add_argument("--aggressive", action="store_true", help="aggressive ログを使う")
    ap.add_argument("--stats",      action="store_true", help="戦略別統計を表示")
    args = ap.parse_args()

    suffix   = "_aggressive" if args.aggressive else ""
    log_path = Path(args.log) if args.log else Path(f"forward_test_log{suffix}.csv")
    out_path = Path(args.out) if args.out else Path(f"meta_train{suffix}.csv")

    build(log_path, out_path, show_stats=args.stats)


if __name__ == "__main__":
    main()
