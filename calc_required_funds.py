"""
calc_required_funds.py  ―  過去N日のシグナル銘柄・必要資金の集計

使い方:
  python calc_required_funds.py           # 過去30日
  python calc_required_funds.py --days 60 # 過去60日
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import check_signals_stop     as _stop
import check_signals_breakout as _brk
import check_signals_short    as _short
from backtest_limit_entry import (
    fetch, run_limit_backtest, compute_period_result,
    FIXED_QTY, WORKERS,
)

JST = timezone(timedelta(hours=9))


def _run_one(symbol, name, strategy, params, entry_type, days):
    calc_fn, em, sm, tm = params[strategy]
    df = fetch(symbol, max(365, days + 60))
    if df is None:
        return []
    try:
        df = calc_fn(df)
    except Exception:
        return []
    result = run_limit_backtest(
        symbol, name, df, calc_fn, em, sm, tm, max(365, days + 60),
        strategy, entry_type=entry_type
    )
    if not result:
        return []

    today  = datetime.now(JST).date()
    cutoff = today - timedelta(days=days)
    rows = []
    for t in result["trade_log"]:
        if t["signal_dt"].date() >= cutoff:
            rows.append({
                "symbol":       symbol,
                "name":         name,
                "strategy":     strategy,
                "signal_dt":    t["signal_dt"].date(),
                "signal_price": t["signal_price"],
                "order_price":  t["order_limit"],
                "stop_price":   t["order_stop"],
                "target_price": t["order_target"],
                "required":     t["signal_price"] * FIXED_QTY,
                "entry_dt":     t["entry_dt"].date() if t.get("entry_dt") else None,
                "reason":       t.get("reason") or "保有中",
                "pnl":          t.get("pnl", 0),
            })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args()

    tasks = []
    for symbol, name, strategy in _stop.WATCHLIST:
        tasks.append((symbol, name, strategy, _stop.STRATEGY_PARAMS, "stop"))
    for symbol, name, strategy in _brk.WATCHLIST:
        tasks.append((symbol, name, strategy, _brk.STRATEGY_PARAMS, "stop"))
    for symbol, name, strategy in _short.WATCHLIST:
        tasks.append((symbol, name, strategy, _short.STRATEGY_PARAMS, "stop_sell"))

    all_rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(_run_one, s, n, st, p, et, args.days): (s, n, st)
            for s, n, st, p, et in tasks
        }
        for future in as_completed(futures):
            rows = future.result()
            all_rows.extend(rows)

    # シグナル日・銘柄・戦略でユニーク化（同一組み合わせは1回分のみ計上）
    seen = set()
    unique = []
    for r in sorted(all_rows, key=lambda x: x["signal_dt"]):
        key = (r["symbol"], r["strategy"], r["signal_dt"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    total = sum(r["required"] for r in unique)
    holding = [r for r in unique if r["reason"] == "保有中"]
    closed  = [r for r in unique if r["reason"] != "保有中"]

    print(f"\n{'='*80}")
    print(f"  過去{args.days}日 シグナル銘柄 必要資金集計  ({datetime.now(JST).strftime('%Y-%m-%d')})")
    print(f"{'='*80}")
    print(f"  シグナル件数: {len(unique)}件  （保有中: {len(holding)}件 / 決済済: {len(closed)}件）")
    print(f"  必要資金合計: {total:>12,.0f}円  （{total/10000:.0f}万円）")
    print(f"  1件あたり平均: {total/len(unique):>10,.0f}円" if unique else "")
    print()

    # 保有中
    if holding:
        print(f"【保有中】 {len(holding)}件  合計 {sum(r['required'] for r in holding):,.0f}円")
        print(f"  {'シグナル日':<12} {'銘柄':<22} {'戦略':<6} {'株価':>7}  {'必要資金':>10}")
        print(f"  {'-'*65}")
        for r in holding:
            print(f"  {str(r['signal_dt']):<12} {r['name']:<22} {r['strategy']:<6} "
                  f"{r['signal_price']:>7,.0f}円  {r['required']:>9,.0f}円")
        print()

    # 決済済み
    if closed:
        print(f"【決済済み】 {len(closed)}件  合計 {sum(r['required'] for r in closed):,.0f}円")
        print(f"  {'シグナル日':<12} {'銘柄':<22} {'戦略':<6} {'株価':>7}  {'必要資金':>10}  {'結果'}")
        print(f"  {'-'*75}")
        for r in closed:
            pnl_str = f"{r['pnl']:+,.0f}円" if r["pnl"] != 0 else ""
            print(f"  {str(r['signal_dt']):<12} {r['name']:<22} {r['strategy']:<6} "
                  f"{r['signal_price']:>7,.0f}円  {r['required']:>9,.0f}円  {r['reason']} {pnl_str}")

    print(f"\n{'='*80}")
    print(f"  ※ 必要資金 = シグナル時株価 × {FIXED_QTY}株（固定数量）")
    print(f"  ※ 同時保有する場合は合計額が必要。順番に入る場合は最大1件分。")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
