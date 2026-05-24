"""
analyze_gap_miss.py  ─  ギャップアップによる約定ミス率を分析

バックテストで「約定」となった取引のうち、
実運用の逆指値→指値注文では「約定しなかった」ケースを集計する。

判定: 約定日の始値 > 注文価格 × (1 + LIMIT_MARGIN) → 実運用では未約定

Usage:
    python analyze_gap_miss.py              # 指値上限+1%（デフォルト）
    python analyze_gap_miss.py --margin 3   # 指値上限+3%
    python analyze_gap_miss.py --margin 5   # 指値上限+5%
"""
from __future__ import annotations
import argparse
import sys
import os

# モード設定
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

import pandas as pd
from backtest_limit_entry import run_limit_backtest, fetch
from check_signals_stop     import WATCHLIST as STOP_WL, STRATEGY_PARAMS
from check_signals_breakout import WATCHLIST as BRK_WL,  STRATEGY_PARAMS as BRK_PARAMS

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--margin", type=float, default=1.0,
                        help="指値上限マージン %（デフォルト=1.0）")
    parser.add_argument("--aggressive", action="store_true")
    args = parser.parse_args()

    limit_margin = args.margin / 100.0
    print(f"指値上限マージン: +{args.margin:.1f}%")
    print(f"モード: {'aggressive' if args.aggressive else 'conservative'}")
    print()

    all_wl = [(sym, name, strat, STRATEGY_PARAMS) for sym, name, strat in STOP_WL] + \
             [(sym, name, strat, BRK_PARAMS)       for sym, name, strat in BRK_WL]

    total = gap_miss = small_gap = ok = no_open = 0
    gap_examples = []
    strategy_stats: dict[str, dict] = {}

    print(f"分析中... ({len(all_wl)}銘柄)", flush=True)

    for sym, name, strat, params in all_wl:
        calc_fn, em, sm, tm = params[strat]
        df = fetch(sym, 400)
        if df is None or len(df) < 10:
            continue
        try:
            result = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm,
                                        backtest_days=365, strategy_name=strat,
                                        entry_type="stop")
        except Exception:
            continue

        logs = result.get("trade_log", [])
        df.index = pd.to_datetime(df.index)

        if strat not in strategy_stats:
            strategy_stats[strat] = {"total": 0, "miss": 0}

        for t in logs:
            order_p = t.get("order_limit", 0)
            if not order_p:
                continue
            entry_dt = t.get("entry_dt") or t.get("signal_dt")
            if entry_dt is None:
                continue
            entry_date = pd.to_datetime(entry_dt).date()
            row = df[df.index.date == entry_date]
            if row.empty:
                no_open += 1
                continue

            open_p  = float(row.iloc[0]["open"])
            limit_p = order_p * (1 + limit_margin)
            gap_pct = (open_p - order_p) / order_p * 100

            total += 1
            strategy_stats[strat]["total"] += 1

            if open_p > limit_p:
                gap_miss += 1
                strategy_stats[strat]["miss"] += 1
                if len(gap_examples) < 15:
                    gap_examples.append({
                        "sym": sym, "name": name[:10], "strat": strat,
                        "order_p": order_p, "limit_p": limit_p,
                        "open_p": open_p, "gap_pct": gap_pct,
                    })
            elif gap_pct > 0:
                small_gap += 1
            else:
                ok += 1

    if total == 0:
        print("データなし")
        return

    miss_rate = gap_miss / total * 100

    print("=" * 60)
    print(f"=== ギャップアップ約定ミス分析結果 (指値上限+{args.margin:.1f}%) ===")
    print("=" * 60)
    print(f"バックテスト総約定数              : {total}件")
    print(f"問題なし（始値≤注文価格）         : {ok}件 ({ok/total*100:.1f}%)")
    print(f"小幅ギャップ（約定可）            : {small_gap}件 ({small_gap/total*100:.1f}%)")
    print(f"指値超えギャップ（実運用で未約定）: {gap_miss}件 ({miss_rate:.1f}%)")
    print()
    print("【戦略別ミス率】")
    for strat, s in sorted(strategy_stats.items()):
        if s["total"] == 0:
            continue
        r = s["miss"] / s["total"] * 100
        bar = "█" * int(r / 2)
        print(f"  {strat:6s}: {s['miss']:3d}/{s['total']:3d} ({r:4.1f}%)  {bar}")

    print()
    print("【指値超えギャップの例（最大15件）】")
    print(f"  {'銘柄':<8} {'戦略':6} {'注文価格':>7} {'指値上限':>7} {'始値':>7} {'ギャップ':>7}")
    for ex in sorted(gap_examples, key=lambda x: -x["gap_pct"]):
        print(f"  {ex['sym']:<8} {ex['strat']:6} "
              f"{ex['order_p']:>7,.0f} {ex['limit_p']:>7,.0f} "
              f"{ex['open_p']:>7,.0f} +{ex['gap_pct']:5.1f}%")

    print()
    print("【まとめ】")
    if miss_rate < 5:
        print(f"  ミス率 {miss_rate:.1f}%: 実運用への影響は小さい")
    elif miss_rate < 15:
        print(f"  ミス率 {miss_rate:.1f}%: 取引機会を一部逃す（許容範囲内）")
    else:
        print(f"  ミス率 {miss_rate:.1f}%: 影響大。指値上限を広げることを検討")
    print(f"  → 指値+{args.margin:.0f}%ではなく+3%なら: python analyze_gap_miss.py --margin 3")
    print(f"  → 指値+{args.margin:.0f}%ではなく+5%なら: python analyze_gap_miss.py --margin 5")

if __name__ == "__main__":
    main()
