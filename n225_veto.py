"""
N 停止判定器 — 日経「始値→終値」上昇予測による発注 veto の検証
────────────────────────────────────────────────────────────────────────
【運用課題】
  N は寄り後にギャップアップ銘柄を空売りし、引けで決済する。
  大負け日が「日経が日中に上昇した日」に集中する (実測 R^2 ≈ 0.235)。
  ⇒ N の最初の発注 (09:00) より前に「今日の日経は始値→終値で上がるか」を
    予測し、強気日だけ N を停止 / 縮小できれば、月平均÷σ・CVaR・最大DD が改善するはず。

【このスクリプトが答える問い】
  「その最低限の予測力は存在するか?」— 精度ではなくリスク指標で判定する。

【最重要の設計判断: ランダム休場との比較】
  何日か休めば σ も DD も機械的に下がる。だから「無停止 vs veto」の比較は
  ほぼ必ず改善に見える。正しいベンチマークは
  **同じ日数をランダムに休んだ場合の分布** であり、本スクリプトは
  ブートストラップでその分布を作り、実際の veto がどの分位にいるかを p 値として出す。
  p < 0.05 で初めて「予測力がある」と言える。

使い方:
  # 0) 先に日経パネルを作る
  python n225_research.py --build --years 15

  # 1) N の実損益がある場合 (推奨。CSV: date,pnl の 2 列)
  python n225_veto.py --npnl n_pnl.csv

  # 2) 実損益が無い場合 — 日経日中リターンとの相関 R^2=0.235 を再現した代理損益で検証
  python n225_veto.py

  # 情報境界を厳格化 (当日ギャップを一切使わない)
  python n225_veto.py --asof strict

  # 「強い上昇日」の定義を変える (default: 始値→終値 +0.5% 超)
  python n225_veto.py --strong-pct 0.8

  # ネット不要の自己テスト
  python n225_veto.py --selftest
"""

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

from n225_research import (
    Gbdt,
    LogisticL2,
    PANEL_CSV,
    load_panel,
    make_features,
    make_synthetic_panel,
    make_targets,
    walk_forward,
)

TRADING_DAYS = 252
DEFAULT_R2   = 0.235      # 実測: 日経 始値→終値 が N 損益を説明する決定係数
SKIP_RATES   = (0.05, 0.10, 0.15, 0.20, 0.30)
N_BOOTSTRAP  = 4000


# ══════════════════════════════════════════════════════════════
#  N の損益系列
# ══════════════════════════════════════════════════════════════
def load_n_pnl(path: Path) -> pd.Series:
    """CSV (date,pnl) を読み込む。列名は先頭2列を日付・損益として扱う。"""
    df = pd.read_csv(path)
    date_col = next((c for c in df.columns if c.lower() in
                     ("date", "日付", "day", "trade_date", "record_date")), df.columns[0])
    pnl_col = next((c for c in df.columns if c.lower() in
                    ("pnl", "profit", "損益", "pl", "return", "ret")), df.columns[1])
    s = pd.Series(pd.to_numeric(df[pnl_col], errors="coerce").to_numpy(),
                  index=pd.to_datetime(df[date_col]).dt.normalize(), name="pnl")
    return s.dropna().groupby(level=0).sum().sort_index()


def surrogate_n_pnl(r_day: pd.Series, r2: float = DEFAULT_R2,
                    target_sharpe: float = 1.0, seed: int = 7) -> pd.Series:
    """
    N の代理損益。日経日中リターンとの相関だけを実測 R^2 に合わせて再現する。

      pnl = ( rho * z(r_day) + sqrt(1-rho^2) * z_noise ) * sigma + drift
      rho = -sqrt(r2)   … 日経が日中上昇するほど N は負ける (空売りなので負相関)

    ⚠ これは「日経との連動部分」しか持たない人工系列。
      N の銘柄選択・約定・建玉数の効果は含まれない。
      必ず --npnl で実損益を渡して再検証すること。
    """
    rng = np.random.default_rng(seed)
    rho = -math.sqrt(r2)
    z_r = (r_day - r_day.mean()) / r_day.std()
    z_e = rng.standard_normal(len(r_day))
    core = rho * z_r.to_numpy() + math.sqrt(1 - r2) * z_e
    core = core / core.std()
    drift = target_sharpe / math.sqrt(TRADING_DAYS)      # 単位: 標準偏差
    return pd.Series(core + drift, index=r_day.index, name="pnl")


# ══════════════════════════════════════════════════════════════
#  リスク指標
# ══════════════════════════════════════════════════════════════
def risk_stats(pnl: pd.Series) -> dict:
    """N の運用指標。pnl は日次損益 (円でも標準化単位でも可)。"""
    x = pnl.to_numpy(dtype=float)
    if len(x) == 0 or x.std() == 0:
        return {k: float("nan") for k in
                ("days", "daily_mean", "daily_std", "m_mean", "m_std", "m_ratio",
                 "sharpe", "cvar95", "maxdd", "total")}

    monthly = pnl.groupby([pnl.index.year, pnl.index.month]).sum()
    m_mean, m_std = float(monthly.mean()), float(monthly.std())

    k = max(int(len(x) * 0.05), 1)
    cvar95 = float(np.sort(x)[:k].mean())               # 下位5%の平均 (負が大きいほど悪い)

    eq = np.cumsum(x)
    maxdd = float((eq - np.maximum.accumulate(eq)).min())

    return {
        "days":       len(x),
        "daily_mean": float(x.mean()),
        "daily_std":  float(x.std()),
        "m_mean":     m_mean,
        "m_std":      m_std,
        "m_ratio":    m_mean / m_std if m_std > 0 else float("nan"),
        "sharpe":     float(x.mean() / x.std() * math.sqrt(TRADING_DAYS)),
        "cvar95":     cvar95,
        "maxdd":      maxdd,
        "total":      float(x.sum()),
    }


def _print_stats(label: str, st: dict) -> None:
    print(f"{label:<22}{st['days']:>7}{st['m_mean']:>12.3f}{st['m_std']:>11.3f}"
          f"{st['m_ratio']:>10.3f}{st['sharpe']:>9.2f}{st['cvar95']:>11.3f}"
          f"{st['maxdd']:>11.2f}{st['total']:>12.2f}")


def _stats_header() -> None:
    print(f"{'':<22}{'稼働日':>7}{'月平均':>12}{'月σ':>11}{'月平均/σ':>10}"
          f"{'Sharpe':>9}{'CVaR95':>11}{'最大DD':>11}{'累計':>12}")
    print("─" * 95)


# ══════════════════════════════════════════════════════════════
#  スコア生成 (walk-forward)
# ══════════════════════════════════════════════════════════════
def build_scores(panel: pd.DataFrame, args) -> pd.DataFrame:
    """
    日経の「当日 始値→終値」が強い上昇になる確率を walk-forward で予測する。
    戻り値: index=日付, columns=[r_day, y_strong, score_<model>...]
    """
    tg = make_targets(panel)
    feats = make_features(panel, "day", asof=args.asof)

    data = feats.copy()
    data["_r"] = tg["day"]
    data = data.replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < args.min_train + args.step:
        raise SystemExit(f"有効行 {len(data)} 件。min_train={args.min_train}+step={args.step} に不足。")

    r_day = data.pop("_r")
    thr = args.strong_pct / 100.0
    y = (r_day.to_numpy() > thr).astype(int)
    X = data.to_numpy(dtype=float)

    print(f"■ スコア生成   asof={args.asof}   特徴量 {X.shape[1]}   有効行 {len(data)} "
          f"({data.index[0].date()} .. {data.index[-1].date()})")
    print(f"  『強い上昇日』の定義: 始値→終値 > +{args.strong_pct:.2f}%  "
          f"→ 該当 {y.mean()*100:.1f}% ({y.sum()} 日)")
    print(f"  walk-forward: min_train={args.min_train}, step={args.step}, embargo={args.embargo}")

    models = [("logistic", lambda: LogisticL2(lam=args.lam))]
    try:
        import sklearn  # noqa: F401
        models.append(("gbdt", lambda: Gbdt()))
    except ImportError:
        print("  [info] sklearn 未導入のため GBDT はスキップ")

    out = None
    for name, factory in models:
        idx, p = walk_forward(X, y, factory, args.min_train, args.step, args.embargo)
        if len(idx) == 0:
            continue
        col = pd.DataFrame({f"score_{name}": p}, index=data.index[idx])
        out = col if out is None else out.join(col, how="outer")

    if out is None:
        raise SystemExit("walk-forward が 1 つも実行できませんでした。")

    out["r_day"]    = r_day.reindex(out.index)
    out["y_strong"] = (out["r_day"] > thr).astype(int)
    return out.dropna()


# ══════════════════════════════════════════════════════════════
#  デシル分析 (予測力があるかの一次判定)
# ══════════════════════════════════════════════════════════════
def decile_report(scores: pd.DataFrame, score_col: str, pnl: pd.Series) -> None:
    df = scores.copy()
    df["pnl"] = pnl.reindex(df.index)
    df = df.dropna(subset=["pnl"])
    df["dec"] = pd.qcut(df[score_col].rank(method="first"), 10, labels=False)

    print(f"\n■ デシル分析 ({score_col})  ─ スコアが高い = 日経が日中上がると予測")
    print("─" * 84)
    print(f"{'デシル':<8}{'日数':>7}{'平均スコア':>12}{'日経日中平均':>14}"
          f"{'強上昇率':>11}{'N平均損益':>12}{'N累計':>12}")
    print("─" * 84)
    for d, g in df.groupby("dec"):
        print(f"{'D'+str(int(d)+1):<8}{len(g):>7}{g[score_col].mean():>12.3f}"
              f"{g['r_day'].mean()*100:>13.3f}%{g['y_strong'].mean()*100:>10.1f}%"
              f"{g['pnl'].mean():>12.4f}{g['pnl'].sum():>12.2f}")
    print("─" * 84)
    lo, hi = df[df["dec"] == 0], df[df["dec"] == 9]
    spread = hi["r_day"].mean() - lo["r_day"].mean()
    se = math.sqrt(hi["r_day"].var() / len(hi) + lo["r_day"].var() / len(lo))
    t = spread / se if se > 0 else 0.0
    print(f"  D10 − D1 の日経日中リターン差: {spread*100:+.3f}%  (t = {t:+.2f})")
    print(f"  D10 − D1 の N 平均損益差:      {hi['pnl'].mean()-lo['pnl'].mean():+.4f}")
    print("  ※ スコアが機能していれば、デシルが上がるほど日経日中リターンは上昇し、"
          "N の損益は悪化するはず。")


# ══════════════════════════════════════════════════════════════
#  veto シミュレーション (本体)
# ══════════════════════════════════════════════════════════════
def veto_report(scores: pd.DataFrame, score_col: str, pnl: pd.Series,
                size_when_vetoed: float, n_boot: int, seed: int = 0) -> None:
    df = scores.copy()
    df["pnl"] = pnl.reindex(df.index)
    df = df.dropna(subset=["pnl"]).sort_index()

    base_pnl = df["pnl"]
    base = risk_stats(base_pnl)
    rng = np.random.default_rng(seed)
    s = df[score_col].to_numpy()
    x = base_pnl.to_numpy()
    n = len(x)

    mode = "停止" if size_when_vetoed == 0.0 else f"{size_when_vetoed:.0%} に縮小"
    print(f"\n■ veto シミュレーション ({score_col}, 該当日は {mode})")
    print("─" * 95)
    _stats_header()
    _print_stats("veto なし (現行N)", base)

    results = []
    for rate in SKIP_RATES:
        k = max(int(round(n * rate)), 1)
        cut = np.partition(s, n - k)[n - k]               # 上位 k 日の閾値
        mask = s >= cut
        if mask.sum() == 0:
            continue

        w = np.where(mask, size_when_vetoed, 1.0)
        vetoed = pd.Series(x * w, index=df.index)
        st = risk_stats(vetoed)
        st["days"] = int((w > 0).sum())          # 実際に建てた日数
        _print_stats(f"veto 上位{rate*100:.0f}% ({mask.sum()}日)", st)

        # ── ランダム休場ベンチマーク: 同じ日数を無作為に休んだ分布 ──
        k_actual = int(mask.sum())
        boot = {"m_ratio": [], "cvar95": [], "maxdd": [], "sharpe": []}
        for _ in range(n_boot):
            sel = rng.choice(n, size=k_actual, replace=False)
            wr = np.ones(n)
            wr[sel] = size_when_vetoed
            b = risk_stats(pd.Series(x * wr, index=df.index))
            for key in boot:
                boot[key].append(b[key])

        pv = {}
        for key in boot:
            arr = np.asarray(boot[key])
            # 全指標「大きいほど良い」向きに揃える (maxdd/cvar95 は負値なので大きい=浅い)
            pv[key] = float((arr >= st[key]).mean())
        results.append((rate, k_actual, st, pv))

    print("─" * 95)

    print(f"\n■ ランダム休場との比較 (同じ日数を無作為に休んだ {n_boot} 回の分布に対する p 値)")
    print("─" * 78)
    print(f"{'veto率':<12}{'休場日数':>9}{'月平均/σ':>12}{'Sharpe':>11}"
          f"{'CVaR95':>11}{'最大DD':>11}{'判定':>10}")
    print("─" * 78)
    for rate, k_actual, st, pv in results:
        best = min(pv["m_ratio"], pv["sharpe"], pv["cvar95"], pv["maxdd"])
        verdict = "有意" if best < 0.05 else ("△" if best < 0.15 else "―")
        print(f"{rate*100:>5.0f}%{'':<7}{k_actual:>9}{pv['m_ratio']:>12.3f}{pv['sharpe']:>11.3f}"
              f"{pv['cvar95']:>11.3f}{pv['maxdd']:>11.3f}{verdict:>10}")
    print("─" * 78)
    print("  p 値 = ランダムに同日数休んだ方が同等以上に良くなる確率。")
    print("  ⇒ p < 0.05 で初めて『スコアがランダムより意味のある日を選べている』と言える。")
    print(f"  ⇒ {len(SKIP_RATES)} 通りの veto 率 × 4 指標を見ているので、"
          f"Bonferroni 目安は {0.05/(len(SKIP_RATES)*4):.4f}。")


# ══════════════════════════════════════════════════════════════
#  実行
# ══════════════════════════════════════════════════════════════
def run(panel: pd.DataFrame, args, n_pnl: pd.Series | None) -> pd.DataFrame:
    scores = build_scores(panel, args)

    if n_pnl is None:
        print("\n[!] N の実損益が指定されていないため、代理損益で検証します。")
        print(f"    日経 始値→終値 との R^2 を {args.r2:.3f} に合わせた人工系列 "
              f"(単位: 日次σ=1, 年率Sharpe≈{args.surrogate_sharpe:.1f})。")
        print("    → 銘柄選択・約定の効果は含まれません。必ず --npnl で実損益を渡して再検証してください。")
        pnl = surrogate_n_pnl(scores["r_day"], r2=args.r2,
                              target_sharpe=args.surrogate_sharpe)
    else:
        pnl = n_pnl.reindex(scores.index).dropna()
        overlap = len(pnl)
        print(f"\n[+] N 実損益: {overlap} 日ぶんがスコア期間と重複 "
              f"({pnl.index[0].date()} .. {pnl.index[-1].date()})" if overlap else "")
        if overlap < 120:
            print(f"    [warn] 重複が {overlap} 日しかありません。結論は暫定扱いにしてください。")
        # 実損益と日経日中リターンの実測相関を報告 (前提 R^2=0.235 の確認)
        j = pd.concat([pnl, scores["r_day"]], axis=1).dropna()
        if len(j) > 30:
            c = float(j.iloc[:, 0].corr(j.iloc[:, 1]))
            print(f"    実測: corr(N損益, 日経日中) = {c:+.3f}  (R^2 = {c*c:.3f})")

    score_cols = [c for c in scores.columns if c.startswith("score_")]
    for col in score_cols:
        decile_report(scores, col, pnl)
        veto_report(scores, col, pnl, args.veto_size, args.bootstrap)
    return scores


def run_selftest(args) -> int:
    print("=" * 72)
    print(" 自己テスト: veto 検証器の配管検査 (ネットワーク不要)")
    print("=" * 72)
    ok = True
    args.bootstrap = min(args.bootstrap, 600)      # 自己テストは軽量に

    for label, signal in (("A. 予測不能な相場 (signal=0)", 0.0),
                          ("B. 既知シグナルあり (signal=0.5)", 0.5)):
        print(f"\n{'#'*72}\n### {label}\n{'#'*72}")
        raw = make_synthetic_panel(n=3000, signal=signal, seed=42)
        panel = raw.copy()
        for col in ("gspc_close", "vix_close", "usdjpy_close", "tnx_close"):
            panel[col] = raw[col].shift(1)
        panel = panel.dropna()
        try:
            run(panel, args, None)
        except SystemExit as e:
            print(f"  [FAIL] {e}")
            ok = False

    print("\n" + "=" * 72)
    print(" 自己テスト完了 — 上の p 値を確認してください:")
    print("   A では p が概ね 0.05 を超えている (= ランダム休場と区別できない) のが正常。")
    print("   B では p が 0.05 を大きく下回る (= スコアが有効な日を選べている) のが正常。")
    print("=" * 72)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="日経 始値→終値 上昇予測による N 発注 veto の検証",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--npnl", default=None, help="N の日次損益 CSV (date,pnl)")
    ap.add_argument("--panel", default=str(PANEL_CSV), help="日経パネル CSV")
    ap.add_argument("--asof", default="preopen", choices=["strict", "preopen"],
                    help="情報境界。strict=当日ギャップ不使用 / "
                         "preopen=09:00前に先物から観測できるギャップを使う (default)")
    ap.add_argument("--strong-pct", type=float, default=0.5, dest="strong_pct",
                    help="『強い上昇日』の閾値 (%%)。default: 0.5")
    ap.add_argument("--veto-size", type=float, default=0.0, dest="veto_size",
                    help="veto 日の建玉倍率。0.0=完全停止, 0.5=半分に縮小 (default: 0.0)")
    ap.add_argument("--min-train", type=int, default=1000, dest="min_train")
    ap.add_argument("--step", type=int, default=63)
    ap.add_argument("--embargo", type=int, default=1)
    ap.add_argument("--lam", type=float, default=5.0)
    ap.add_argument("--r2", type=float, default=DEFAULT_R2,
                    help="代理損益の R^2 (実測値 0.235)")
    ap.add_argument("--surrogate-sharpe", type=float, default=1.0, dest="surrogate_sharpe",
                    help="代理損益の年率 Sharpe (平均の水準を決めるだけ)")
    ap.add_argument("--bootstrap", type=int, default=N_BOOTSTRAP,
                    help="ランダム休場ベンチマークの反復回数")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest(args)

    panel = load_panel(Path(args.panel))
    n_pnl = load_n_pnl(Path(args.npnl)) if args.npnl else None
    run(panel, args, n_pnl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
