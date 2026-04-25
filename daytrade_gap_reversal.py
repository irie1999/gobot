"""
daytrade_gap_reversal.py  ―  ギャップリバーサル戦略
==================================================================
寄付で前日比 -1〜-3% のギャップダウン → 反発を狙う逆張り。
毎日数銘柄は必ずギャップダウンするためシグナル頻度が高い。
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

BUDGET       = 600_000
MAX_RISK     = 6_000
GAP_MIN_PCT  = 1.0
GAP_MAX_PCT  = 3.0
VOL_MULT     = 1.2          # エントリーバー出来高 ≥ 直前5本平均×1.2
STOP_MAX_PCT = 2.0          # 損切り距離の上限 (-2.0%でクリップ)
FORCE_CLOSE  = dtime(14, 55)
ENTRY_CUTOFF = dtime(11, 0)
WARMUP       = 3


def backtest_day(day_df, prev_close, budget=BUDGET, max_risk=MAX_RISK):
    if prev_close is None or prev_close <= 0:
        return []

    opens   = day_df["open"].to_numpy(dtype=float)
    highs   = day_df["high"].to_numpy(dtype=float)
    lows    = day_df["low"].to_numpy(dtype=float)
    closes  = day_df["close"].to_numpy(dtype=float)
    volumes = day_df["volume"].to_numpy(dtype=float)
    times   = day_df.index
    n = len(day_df)

    if n < WARMUP + 2:
        return []

    gap_pct = (opens[0] - prev_close) / prev_close * 100
    if gap_pct > -GAP_MIN_PCT or gap_pct < -GAP_MAX_PCT:
        return []

    trades = []
    state = "idle"
    entry_p = stop_p = target_p = 0.0
    entry_dt = None
    qty = 0

    def _finish(exit_p, exit_dt, reason):
        pnl = (exit_p - entry_p) * qty
        pct = (exit_p - entry_p) / entry_p * 100
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=exit_dt,
            entry_p=entry_p, exit_p=exit_p,
            stop_p=stop_p, target_p=target_p,
            qty=qty, pnl=pnl, pct=pct,
            strategy="GapReversal", reason=reason,
        ))

    i = WARMUP
    while i < n:
        t = times[i].time()
        hi, lo, cl = highs[i], lows[i], closes[i]

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                _finish(cl, times[i], "引け強制")
                break
            if lo <= stop_p:
                _finish(stop_p, times[i], "損切り")
                state = "idle"
                i += 1
                continue
            if hi >= target_p:
                _finish(target_p, times[i], "目標達成")
                state = "idle"
                i += 1
                continue
            progress = (hi - entry_p) / (target_p - entry_p) if target_p > entry_p else 0
            if progress >= 0.5:
                new_stop = entry_p
                if new_stop > stop_p:
                    stop_p = new_stop
            i += 1
            continue

        if t >= ENTRY_CUTOFF:
            i += 1
            continue

        # 反発確認: ①始値超え ②陽線バー ③出来高が直前5本平均×VOL_MULT以上
        if cl > opens[0] and cl > opens[i]:
            ref_start = max(0, i - 5)
            avg_vol = volumes[ref_start:i].mean() if i > ref_start else 0
            if avg_vol <= 0 or volumes[i] < avg_vol * VOL_MULT:
                i += 1
                continue
            if i + 1 >= n:
                break
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            day_low = lows[:i + 1].min()
            # 損切り: 当日安値の0.999倍 or エントリー-STOP_MAX_PCT% の浅い方
            stop_floor = entry_p * (1 - STOP_MAX_PCT / 100)
            stop_p = max(day_low * 0.999, stop_floor)
            target_p = prev_close
            if entry_p >= target_p or entry_p <= stop_p:
                i += 1
                continue
            qty = calc_position_size(entry_p, stop_p, budget, max_risk)
            state = "in_pos"
            i += 2
            continue

        i += 1

    if state == "in_pos":
        _finish(closes[-1], times[-1], "引け強制")

    return trades


def backtest_symbol(sym, name, df, budget=BUDGET, max_risk=MAX_RISK):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None
    trades = []
    prev_close = None
    for d in dates:
        if prev_close is not None:
            day_trades = backtest_day(daily[d], prev_close, budget, max_risk)
            trades.extend(day_trades)
        prev_close = float(daily[d].iloc[-1]["close"])
    return dict(symbol=sym, name=name, trades=trades)


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0, max_dd=dd)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    parser = argparse.ArgumentParser(description="ギャップリバーサル デイトレ")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    targets = DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"GapReversal: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円",
          flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, name, fetched[sym], args.budget, MAX_RISK)
        if r:
            items.append(r)

    all_trades = sorted([t for it in items for t in it["trades"]],
                        key=lambda x: str(x.get("entry_dt", "")))
    stats = calc_stats(all_trades, args.budget)
    print(f"\n取引:{stats['n']}  勝率:{stats['win_rate']:.1f}%  PF:{_pf(stats['pf'])}  "
          f"損益:{stats['total_pnl']:+,.0f}  DD:{stats['max_dd']:+.1f}%")

    print(f"\n{'='*78}")
    print(f"  銘柄別サマリ ({len(items)}銘柄)")
    print("=" * 78)
    print(f"{'銘柄':<22} {'コード':<8} {'取引':>4} {'勝率':>5} {'PF':>7} "
          f"{'損益':>10} {'DD':>6}")
    print("-" * 78)
    rows = []
    for it in items:
        if not it["trades"]:
            continue
        ist = calc_stats(it["trades"], args.budget)
        rows.append((it["name"], it["symbol"], ist))
    rows.sort(key=lambda x: x[2]["total_pnl"], reverse=True)
    for name, sym, ist in rows:
        disp = name[:20] if len(name) <= 20 else name[:19] + "…"
        print(f"{disp:<22} {sym:<8} {ist['n']:>4} {ist['win_rate']:>4.0f}% "
              f"{_pf(ist['pf']):>7} {ist['total_pnl']:>+10,.0f} "
              f"{ist['max_dd']:>+5.1f}%")
    print("=" * 78)


if __name__ == "__main__":
    main()
