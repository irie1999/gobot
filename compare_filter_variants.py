"""
compare_filter_variants.py  ―  VOL vs VOLTF / MACD vs MACDTF のWF比較 (スイング)
==================================================================
トレンドフィルタ(MA50)の有無で選定がどう変わるかを、scan_walkforward が出力した
walkforward_{strat}[_aggressive]_holdout{N}d_<date>.csv から一覧比較する。

比較指標 (各CSV=選定窓の合格銘柄):
  合格数 / total_test_pnl合計 / 平均PF / 平均WR / 平均MaxDD%
※ スイングWF CSVには holdout(OOS)損益列が無い(=損益タブ側で評価)ため、
  ここで見えるのは『選定窓の質』。フィルタで除外された銘柄数(除外数)も表示。

使い方:
  python compare_filter_variants.py
  python compare_filter_variants.py --date 2026-07-01
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
RESULTS = Path("walkforward_results")
HOLDOUTS = [30, 60, 90, 120, 150, 180]
PAIRS = [
    ("VOL", "VOLTF"), ("MACD", "MACDTF"),            # ロング (買い)
    ("MACD_S", "MACD_S_TF"), ("GAP_S", "GAP_S_TF"),  # ショート (売り)
]
MODES = [("", "con"), ("_aggressive", "agg")]


def _f(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _latest(strat: str, msuf: str, hd: int, date: str | None):
    if date:
        p = RESULTS / f"walkforward_{strat}{msuf}_holdout{hd}d_{date}.csv"
        return p if p.exists() else None
    cands = sorted(RESULTS.glob(f"walkforward_{strat}{msuf}_holdout{hd}d_*.csv"),
                   reverse=True)
    return cands[0] if cands else None


def _stats(strat: str, msuf: str, hd: int, date: str | None):
    p = _latest(strat, msuf, hd, date)
    if not p:
        return None
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    n = len(rows)
    if n == 0:
        return {"n": 0, "pnl": 0.0, "pf": 0.0, "wr": 0.0, "dd": 0.0}
    return {
        "n": n,
        "pnl": sum(_f(r.get("total_test_pnl")) for r in rows),
        "pf":  sum(_f(r.get("avg_test_pf")) for r in rows) / n,
        "wr":  sum(_f(r.get("avg_test_wr")) for r in rows) / n,
        "dd":  sum(_f(r.get("max_drawdown_pct")) for r in rows) / n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (省略時は最新)")
    args = ap.parse_args()

    for base, tf in PAIRS:
        print("\n" + "=" * 100)
        print(f"  【{base} vs {tf}】  MA50フィルタ有無の比較 (選定窓の合格銘柄)")
        print("=" * 100)
        print(f"  {'モード/HO':<10}"
              f"{base+'合格':>8}{'TF合格':>8}{'除外':>6}"
              f"{base+'PnL':>13}{'TF PnL':>13}"
              f"{base+'PF':>7}{'TF PF':>7}"
              f"{base+'DD':>7}{'TF DD':>7}")
        print("  " + "-" * 96)
        for msuf, mlabel in MODES:
            for hd in HOLDOUTS:
                b = _stats(base, msuf, hd, args.date)
                t = _stats(tf, msuf, hd, args.date)
                if b is None and t is None:
                    continue
                b = b or {"n": 0, "pnl": 0, "pf": 0, "wr": 0, "dd": 0}
                t = t or {"n": 0, "pnl": 0, "pf": 0, "wr": 0, "dd": 0}
                excl = b["n"] - t["n"]
                print(f"  {mlabel+'/HO'+str(hd):<10}"
                      f"{b['n']:>8}{t['n']:>8}{excl:>6}"
                      f"{b['pnl']:>+13,.0f}{t['pnl']:>+13,.0f}"
                      f"{b['pf']:>7.2f}{t['pf']:>7.2f}"
                      f"{b['dd']:>6.1f}%{t['dd']:>6.1f}%")
        print("  " + "-" * 96)
    print("\n読み方:")
    print("  ・TF(フィルタ版)は『除外』の分だけ合格が減る(falling knife除外)")
    print("  ・TFの平均PF↑・平均DD(絶対値)↓ なら質が上がっている＝フィルタ有効")
    print("  ・PnLはフィルタで減るのが普通(件数減)。質(PF/DD)と除外内容で採否を判断")
    print("  ・真のOOS判定は損益タブ(run_signals_holdout_all)側。ここは選定窓の質の目安")


if __name__ == "__main__":
    main()
