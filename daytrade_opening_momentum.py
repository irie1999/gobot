"""
daytrade_opening_momentum.py  ―  寄付モメンタム戦略
==================================================================
寄付直後 (9:00-9:15) の値動き方向に順張り。
毎銘柄・毎日必ず判定 → 圧倒的シグナル量。
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

BUDGET        = 600_000
MAX_RISK      = 6_000
MOMENTUM_PCT  = 0.003   # 寄付から +0.3% でモメンタム確認
TARGET_PCT    = 0.008   # +0.8% 利確 (R:R 1.6:1)
STOP_PCT      = 0.005   # -0.5% 損切り (ノイズ回避)
VOL_MULT      = 1.5     # 出来高フィルタ: 初期バーの1.5倍
FORCE_CLOSE   = dtime(14, 55)
EXIT_CUTOFF   = dtime(11, 30)  # 前場終了で退場
CHECK_BAR     = 3


def backtest_day(day_df, prev_close=None):
    opens   = day_df["open"].to_numpy(dtype=float)
    highs   = day_df["high"].to_numpy(dtype=float)
    lows    = day_df["low"].to_numpy(dtype=float)
    closes  = day_df["close"].to_numpy(dtype=float)
    volumes = day_df["volume"].to_numpy(dtype=float)
    times   = day_df.index
    n = len(day_df)

    if n < CHECK_BAR + 2:
        return []

    open_price = opens[0]
    check_close = closes[CHECK_BAR - 1]

    if check_close <= open_price * (1 + MOMENTUM_PCT):
        return []

    bullish_count = sum(1 for j in range(CHECK_BAR) if closes[j] > opens[j])
    if bullish_count < 2:
        return []

    # 出来高フィルタ: CHECK_BAR目の出来高が初期バー平均の1.5倍以上
    avg_vol = volumes[:CHECK_BAR].mean()
    if avg_vol <= 0 or volumes[CHECK_BAR - 1] < avg_vol * VOL_MULT:
        return []

    entry_bar = CHECK_BAR
    if entry_bar >= n:
        return []

    entry_p = opens[entry_bar]
    entry_dt = times[entry_bar]
    target_p = entry_p * (1 + TARGET_PCT)
    stop_p = entry_p * (1 - STOP_PCT)

    if entry_p <= stop_p:
        return []

    qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)
    trailing_active = False

    for i in range(entry_bar, n):
        t = times[i].time()
        hi, lo, cl = highs[i], lows[i], closes[i]

        # 前場終了で退場
        if t >= EXIT_CUTOFF:
            pnl = (cl - entry_p) * qty
            pct = (cl - entry_p) / entry_p * 100
            return [dict(entry_dt=entry_dt, exit_dt=times[i],
                        entry_p=entry_p, exit_p=cl,
                        stop_p=stop_p, target_p=target_p,
                        qty=qty, pnl=pnl, pct=pct,
                        strategy="OpenMomentum", reason="前場終了")]

        if lo <= stop_p:
            pnl = (stop_p - entry_p) * qty
            pct = (stop_p - entry_p) / entry_p * 100
            reason = "建値撤退" if trailing_active else "損切り"
            return [dict(entry_dt=entry_dt, exit_dt=times[i],
                        entry_p=entry_p, exit_p=stop_p,
                        stop_p=stop_p, target_p=target_p,
                        qty=qty, pnl=pnl, pct=pct,
                        strategy="OpenMomentum", reason=reason)]

        if hi >= target_p:
            pnl = (target_p - entry_p) * qty
            pct = (target_p - entry_p) / entry_p * 100
            return [dict(entry_dt=entry_dt, exit_dt=times[i],
                        entry_p=entry_p, exit_p=target_p,
                        stop_p=stop_p, target_p=target_p,
                        qty=qty, pnl=pnl, pct=pct,
                        strategy="OpenMomentum", reason="目標達成")]

        # トレーリング: +0.3%で建値ストップ
        if not trailing_active and hi >= entry_p * (1 + 0.003):
            stop_p = entry_p
            trailing_active = True

    exit_p = closes[-1]
    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100
    return [dict(entry_dt=entry_dt, exit_dt=times[-1],
                entry_p=entry_p, exit_p=exit_p,
                stop_p=stop_p, target_p=target_p,
                qty=qty, pnl=pnl, pct=pct,
                strategy="OpenMomentum", reason="引け強制")]


def backtest_symbol(sym, name, df, budget=BUDGET, max_risk=MAX_RISK):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None
    trades = []
    prev_close = None
    for d in dates:
        day_trades = backtest_day(daily[d], prev_close)
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
    parser = argparse.ArgumentParser(description="寄付モメンタム デイトレ")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    targets = DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"寄付モメンタム: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円",
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
