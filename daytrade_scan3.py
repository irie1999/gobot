"""
daytrade_scan3.py  ―  3ブレイクアウト戦略で全銘柄スキャン
==================================================================
Donchian / MACD Break / Stoch+ATR の3戦略で全銘柄バックテストし、
戦略別に上位銘柄をランキング。

【使い方】
  python daytrade_scan3.py --source local --days 730 --budget 600000
  python daytrade_scan3.py --source yfinance --days 60 --budget 600000
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch
from daytrade_scan import get_prime_symbols
from daytrade_donchian import backtest_symbol as don_bt
from daytrade_macd_break import backtest_symbol as macd_bt
from daytrade_stoch_atr import backtest_symbol as stoch_bt

JST = timezone(timedelta(hours=9))
BUDGET = 600_000


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return None
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades))


def scan_strategy(fetched, targets, bt_fn, strat_name, budget):
    """1戦略で全銘柄スキャン。"""
    results = []
    total = len(targets)
    print(f"\n[{strat_name}] スキャン中...", flush=True)
    for i, (sym, name) in enumerate(targets, 1):
        if sym not in fetched:
            continue
        try:
            r = bt_fn(sym, name, fetched[sym], budget, 6000)
        except Exception:
            continue
        if r and r.get("trades"):
            s = calc_stats(r["trades"], budget)
            if s and s["n"] >= 5:
                results.append({
                    "symbol": sym, "name": name,
                    "price": float(fetched[sym].iloc[-1]["close"]),
                    **s
                })
        if i % 200 == 0 or i == total:
            print(f"  {i}/{total} 完了 ({len(results)}銘柄ヒット)", flush=True)
    return results


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def print_top(results, name, limit=30):
    print(f"\n{'='*80}")
    print(f"  {name} 上位{limit}銘柄 (取引≥5 / PF順)")
    print("=" * 80)
    # PF有限 & PF≥1.3 に絞る
    filtered = [r for r in results if r["pf"] < 100 and r["pf"] >= 1.3]
    filtered.sort(key=lambda x: x["pf"], reverse=True)
    for i, r in enumerate(filtered[:limit], 1):
        print(f"  {i:>3} {r['name']:<16} {r['symbol']:<8} "
              f"{r['price']:>7,.0f} {r['n']:>4}回 "
              f"{r['win_rate']:>4.0f}% PF{_pf(r['pf']):>5} "
              f"{r['total_pnl']:>+10,.0f}円")
    print(f"  (全{len(filtered)}銘柄が PF≥1.3)")


def main():
    parser = argparse.ArgumentParser(description="3ブレイクアウト戦略スキャン")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--strategy", choices=["all", "donchian", "macd", "stoch"],
                        default="all", help="実行する戦略 (default: all)")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    # プライム銘柄取得
    print("プライム銘柄取得中...", flush=True)
    symbols = get_prime_symbols()
    targets = [(s, s) for s in symbols]
    print(f"  対象: {len(targets)}銘柄", flush=True)

    # データ取得 (1回だけ)
    print(f"\nデータ取得中 ({len(targets)}銘柄)...", flush=True)
    fetched = load_intraday_batch(symbols, args.days, source=args.source)

    # 予算フィルタ
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    # 戦略実行
    don_results = []
    macd_results = []
    stoch_results = []

    if args.strategy in ("all", "donchian"):
        don_results = scan_strategy(fetched, targets, don_bt, "Donchian", args.budget)
        print_top(don_results, "Donchian", args.top)
    if args.strategy in ("all", "macd"):
        macd_results = scan_strategy(fetched, targets, macd_bt, "MACD", args.budget)
        print_top(macd_results, "MACD Break", args.top)
    if args.strategy in ("all", "stoch"):
        stoch_results = scan_strategy(fetched, targets, stoch_bt, "Stoch+ATR", args.budget)
        print_top(stoch_results, "Stoch+ATR", args.top)

    # CSV保存 (結果がある戦略のみ)
    for results, name in [(don_results, "donchian"),
                          (macd_results, "macd_break"),
                          (stoch_results, "stoch_atr")]:
        if results:
            results.sort(key=lambda x: x["pf"], reverse=True)
            df = pd.DataFrame(results)
            csv_path = Path(f"scan_{name}_result.csv")
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"\n保存: {csv_path}")

    # 共通銘柄 (3戦略全てで PF≥1.3、all モード時のみ)
    if args.strategy != "all":
        return

    def _good(results):
        return set(r["symbol"] for r in results
                   if r["pf"] < 100 and r["pf"] >= 1.3 and r["n"] >= 5)

    don_set = _good(don_results)
    macd_set = _good(macd_results)
    stoch_set = _good(stoch_results)
    common_3 = don_set & macd_set & stoch_set
    common_2 = (don_set & macd_set) | (don_set & stoch_set) | (macd_set & stoch_set)

    print(f"\n{'='*80}")
    print(f"  共通銘柄分析")
    print("=" * 80)
    print(f"  3戦略全てで有効 (PF≥1.3): {len(common_3)}銘柄")
    for sym in sorted(common_3):
        # 名前と各PFを取得
        d_pf = next((r["pf"] for r in don_results if r["symbol"] == sym), 0)
        m_pf = next((r["pf"] for r in macd_results if r["symbol"] == sym), 0)
        s_pf = next((r["pf"] for r in stoch_results if r["symbol"] == sym), 0)
        name = next((r["name"] for r in don_results if r["symbol"] == sym), sym)
        print(f"    {sym:<8} {name:<20} "
              f"DON PF{d_pf:.2f} / MACD PF{m_pf:.2f} / Stoch PF{s_pf:.2f}")
    print(f"\n  2戦略以上で有効: {len(common_2)}銘柄")


if __name__ == "__main__":
    main()
