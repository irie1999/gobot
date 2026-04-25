"""
daytrade_compare.py  ―  4戦略比較バックテスト
==================================================================
Donchian / GapReversal / VWAP / OpenMomentum を同一データ・同一期間で
一括バックテストし、結果を比較表示。

【使い方】
  python daytrade_compare.py
  python daytrade_compare.py --days 60 --source local
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch

from daytrade_donchian import backtest_symbol as don_bt, calc_stats as don_stats
from daytrade_gap_reversal import backtest_symbol as gr_bt, calc_stats as gr_stats
from daytrade_vwap_rebound import backtest_symbol as vwap_bt, calc_stats as vwap_stats
from daytrade_opening_momentum import backtest_symbol as om_bt, calc_stats as om_stats

JST = timezone(timedelta(hours=9))
BUDGET = 600_000
MAX_RISK = 6_000


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def run_strategy(name, bt_fn, stats_fn, fetched, targets, budget):
    items = []
    for sym, sname in targets:
        if sym not in fetched:
            continue
        r = bt_fn(sym, sname, fetched[sym], budget, MAX_RISK)
        if r:
            items.append(r)

    all_trades = sorted(
        [t for it in items for t in it["trades"]],
        key=lambda x: str(x.get("entry_dt", ""))
    )
    stats = stats_fn(all_trades, budget)

    trade_dates = set()
    for t in all_trades:
        dt = t.get("entry_dt")
        if hasattr(dt, "date"):
            trade_dates.add(dt.date())

    return dict(
        name=name,
        stats=stats,
        items=items,
        all_trades=all_trades,
        trade_days=len(trade_dates),
    )


def print_comparison(results, total_days):
    print(f"\n{'='*90}")
    print(f"  4戦略比較 ({total_days}営業日)")
    print("=" * 90)
    print(f"{'戦略':<20} {'取引':>5} {'勝率':>6} {'PF':>7} "
          f"{'損益':>11} {'DD':>7} {'取引日':>5} {'日率':>5}")
    print("-" * 90)

    for r in results:
        s = r["stats"]
        day_rate = f"{r['trade_days']/total_days*100:.0f}%" if total_days > 0 else "-"
        print(f"{r['name']:<20} {s['n']:>5} {s['win_rate']:>5.1f}% "
              f"{_pf(s['pf']):>7} {s['total_pnl']:>+11,.0f} "
              f"{s['max_dd']:>+6.1f}% {r['trade_days']:>5} {day_rate:>5}")

    print("=" * 90)

    best = max(results, key=lambda x: x["stats"]["total_pnl"])
    most_freq = max(results, key=lambda x: x["trade_days"])
    best_pf = max(results, key=lambda x: x["stats"]["pf"]
                  if x["stats"]["pf"] < 100 else 0)

    print(f"\n  最高利益: {best['name']} ({best['stats']['total_pnl']:+,.0f}円)")
    print(f"  最高頻度: {most_freq['name']} ({most_freq['trade_days']}/{total_days}日)")
    print(f"  最高PF:   {best_pf['name']} (PF {_pf(best_pf['stats']['pf'])})")

    from daytrade_donchian import calc_stats
    for r in results:
        if not r["items"]:
            continue
        print(f"\n--- {r['name']} 銘柄別 TOP5 ---")
        rows = []
        for it in r["items"]:
            if not it["trades"]:
                continue
            ist = calc_stats(it["trades"], BUDGET)
            rows.append((it["name"], it["symbol"], ist))
        rows.sort(key=lambda x: x[2]["total_pnl"], reverse=True)
        for name, sym, ist in rows[:5]:
            disp = name[:18] if len(name) <= 18 else name[:17] + "…"
            print(f"  {disp:<20} {sym:<8} {ist['n']:>3}回 "
                  f"{ist['win_rate']:>4.0f}% PF{_pf(ist['pf']):>5} "
                  f"{ist['total_pnl']:>+10,.0f}")


def main():
    parser = argparse.ArgumentParser(description="4戦略比較バックテスト")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    args = parser.parse_args()

    targets = DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"4戦略比較: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円",
          flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄\n", flush=True)

    all_dates = set()
    for df in fetched.values():
        all_dates.update(df.index.date)
    total_days = len(all_dates)

    strategies = [
        ("Donchian", don_bt, don_stats),
        ("GapReversal", gr_bt, gr_stats),
        ("VWAPリバウンド", vwap_bt, vwap_stats),
        ("寄付モメンタム", om_bt, om_stats),
    ]

    results = []
    for name, bt_fn, stats_fn in strategies:
        print(f"[{name}] 実行中...", flush=True)
        r = run_strategy(name, bt_fn, stats_fn, fetched, targets, args.budget)
        s = r["stats"]
        print(f"  → 取引:{s['n']}  勝率:{s['win_rate']:.1f}%  "
              f"PF:{_pf(s['pf'])}  損益:{s['total_pnl']:+,.0f}")
        results.append(r)

    print_comparison(results, total_days)


if __name__ == "__main__":
    main()
