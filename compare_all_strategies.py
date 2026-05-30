"""
compare_all_strategies.py  ―  全戦略一括比較
==================================================================
1コマンドで複数戦略 × 複数ユニバースを実行し、サマリを並べて表示。

【比較対象】
  1. Donchian (winners 40銘柄)
  2. Donchian (prime 全銘柄)
  3. GapReversal-Bi (winners, both)
  4. GapReversal-Bi (prime, both)
  5. GapReversal-Bi (prime, long)
  6. GapReversal-Bi (prime, short)

【使い方】
  python compare_all_strategies.py                # 60日でフル比較
  python compare_all_strategies.py --days 30      # 30日
  python compare_all_strategies.py --skip-prime   # primeはスキップ (時短)
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch, split_by_day

# 各戦略のバックテスト関数をインポート
import daytrade_donchian as don
import daytrade_gap_reversal_bi as gap_bi


def _load_universe(name):
    if name == "winners":
        from daytrade_donchian_winners import SYMBOLS
        return SYMBOLS
    if name == "prime":
        from symbols_listed_all import SYMBOLS
        return SYMBOLS
    raise ValueError(f"unknown universe: {name}")


def run_donchian(universe, days, budget, max_risk):
    targets = _load_universe(universe)
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, days, source="local")
    max_price = budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = don.backtest_symbol(sym, name, fetched[sym], budget, max_risk)
        if r:
            items.append(r)
    all_trades = [t for it in items for t in it["trades"]]
    stats = don.calc_stats(all_trades, budget)
    return stats, len(items), len(all_trades)


def run_gap_bi(universe, days, side, budget, max_risk):
    targets = _load_universe(universe)
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, days, source="local")
    max_price = budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = gap_bi.backtest_symbol(sym, name, fetched[sym], side, budget, max_risk)
        if r:
            items.append(r)
    all_trades = [t for it in items for t in it["trades"]]
    stats = gap_bi.calc_stats(all_trades, budget)
    return stats, len(items), len(all_trades)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def fmt_row(name, stats, n_symbols, days, budget):
    trades_per_day = stats["n"] / max(1, days)
    return (f"  {name:<28} {stats['n']:>5} 取引  勝率{stats['win_rate']:>5.1f}%  "
            f"PF{_pf(stats['pf']):>6}  損益{stats['total_pnl']:>+10,.0f}円  "
            f"DD{stats['max_dd']:>+6.1f}%  ({n_symbols}銘柄, "
            f"{trades_per_day:.1f}取引/日)")


def main():
    parser = argparse.ArgumentParser(description="全戦略一括比較")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--skip-prime", action="store_true",
                        help="prime ユニバースをスキップ (時短)")
    parser.add_argument("--skip-donchian-prime", action="store_true",
                        help="Donchian prime のみスキップ")
    args = parser.parse_args()

    print(f"=" * 100)
    print(f"  全戦略一括比較  (days={args.days}, budget={args.budget:,}円, "
          f"max_risk={args.max_risk:,}円)")
    print(f"=" * 100)
    print()

    results = []

    # 1. Donchian × winners
    t0 = time.time()
    print(f"[1/6] Donchian winners 実行中...", flush=True)
    try:
        stats, ns, nt = run_donchian("winners", args.days, args.budget, args.max_risk)
        results.append(("Donchian      [winners]", stats, ns))
        print(f"      → 完了 ({time.time()-t0:.1f}s, {nt}取引)")
    except Exception as e:
        print(f"      ✗ エラー: {e}")

    # 2. Donchian × prime
    if not args.skip_prime and not args.skip_donchian_prime:
        t0 = time.time()
        print(f"[2/6] Donchian prime 実行中...", flush=True)
        try:
            stats, ns, nt = run_donchian("prime", args.days, args.budget, args.max_risk)
            results.append(("Donchian      [prime]  ", stats, ns))
            print(f"      → 完了 ({time.time()-t0:.1f}s, {nt}取引)")
        except Exception as e:
            print(f"      ✗ エラー: {e}")

    # 3. GapRev-Bi × winners × both
    t0 = time.time()
    print(f"[3/6] GapRev-Bi winners both 実行中...", flush=True)
    try:
        stats, ns, nt = run_gap_bi("winners", args.days, "both",
                                    args.budget, args.max_risk)
        results.append(("GapRev-Bi[both][winners]", stats, ns))
        print(f"      → 完了 ({time.time()-t0:.1f}s, {nt}取引)")
    except Exception as e:
        print(f"      ✗ エラー: {e}")

    if not args.skip_prime:
        # 4. GapRev-Bi × prime × both
        t0 = time.time()
        print(f"[4/6] GapRev-Bi prime both 実行中...", flush=True)
        try:
            stats, ns, nt = run_gap_bi("prime", args.days, "both",
                                        args.budget, args.max_risk)
            results.append(("GapRev-Bi[both][prime]  ", stats, ns))
            print(f"      → 完了 ({time.time()-t0:.1f}s, {nt}取引)")
        except Exception as e:
            print(f"      ✗ エラー: {e}")

        # 5. GapRev-Bi × prime × long
        t0 = time.time()
        print(f"[5/6] GapRev-Bi prime long 実行中...", flush=True)
        try:
            stats, ns, nt = run_gap_bi("prime", args.days, "long",
                                        args.budget, args.max_risk)
            results.append(("GapRev-L      [prime]  ", stats, ns))
            print(f"      → 完了 ({time.time()-t0:.1f}s, {nt}取引)")
        except Exception as e:
            print(f"      ✗ エラー: {e}")

        # 6. GapRev-Bi × prime × short
        t0 = time.time()
        print(f"[6/6] GapRev-Bi prime short 実行中...", flush=True)
        try:
            stats, ns, nt = run_gap_bi("prime", args.days, "short",
                                        args.budget, args.max_risk)
            results.append(("GapRev-S      [prime]  ", stats, ns))
            print(f"      → 完了 ({time.time()-t0:.1f}s, {nt}取引)")
        except Exception as e:
            print(f"      ✗ エラー: {e}")

    # ── 比較表 ─────────────────────────────────────────
    print()
    print("=" * 100)
    print(f"  比較結果サマリ  ({args.days}日間)")
    print("=" * 100)
    for name, stats, ns in results:
        print(fmt_row(name, stats, ns, args.days, args.budget))
    print("=" * 100)

    # ランキング (損益、PF順)
    if results:
        print()
        print("【損益ランキング】")
        ranked_pnl = sorted(results, key=lambda x: x[1]["total_pnl"], reverse=True)
        for i, (name, stats, ns) in enumerate(ranked_pnl, 1):
            print(f"  {i}. {name}  {stats['total_pnl']:>+10,.0f}円  "
                  f"(PF{_pf(stats['pf']):>6}  勝率{stats['win_rate']:>5.1f}%)")

        print()
        print("【PFランキング】 (取引数10以上のみ)")
        ranked_pf = sorted(
            [r for r in results if r[1]["n"] >= 10],
            key=lambda x: x[1]["pf"] if x[1]["pf"] != float("inf") else 999,
            reverse=True
        )
        for i, (name, stats, ns) in enumerate(ranked_pf, 1):
            print(f"  {i}. {name}  PF{_pf(stats['pf']):>6}  "
                  f"取引{stats['n']:>4}  損益{stats['total_pnl']:>+10,.0f}円")
        print("=" * 100)


if __name__ == "__main__":
    main()
