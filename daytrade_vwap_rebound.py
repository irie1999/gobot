"""
daytrade_vwap_rebound.py  ―  VWAPリバウンド戦略
==================================================================
前場で VWAP を下回った後、VWAP を再突破する瞬間に順張り買い。
VWAP は機関投資家の基準線で、突破で追随買いが入りやすい。
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

BUDGET         = 600_000
MAX_RISK       = 6_000
TARGET_PCT     = 0.008          # 最低利確幅 (+0.8%)
TARGET_RANGE_K = 0.5            # 当日値幅の50%を伸び余地として目標化
STOP_BELOW_VWAP = 0.005         # ストップ拡大 (VWAP-0.5%) でヒゲ刈り回避
VOL_MULT       = 1.5            # 出来高 1.2→1.5倍 (強い突破のみ)
TREND_PCT      = 0.002          # 当日陽線フィルタ: 始値+0.2%以上
FORCE_CLOSE    = dtime(14, 55)
ENTRY_CUTOFF   = dtime(13, 0)
WARMUP         = 5


def _calc_vwap(highs, lows, closes, volumes):
    tp = (highs + lows + closes) / 3.0
    cum_tpv = np.cumsum(tp * volumes)
    cum_vol = np.cumsum(volumes)
    cum_vol[cum_vol == 0] = 1
    return cum_tpv / cum_vol


def backtest_day(day_df, prev_close=None, budget=BUDGET, max_risk=MAX_RISK):
    opens   = day_df["open"].to_numpy(dtype=float)
    highs   = day_df["high"].to_numpy(dtype=float)
    lows    = day_df["low"].to_numpy(dtype=float)
    closes  = day_df["close"].to_numpy(dtype=float)
    volumes = day_df["volume"].to_numpy(dtype=float)
    times   = day_df.index
    n = len(day_df)

    if n < WARMUP + 2:
        return []

    vwap = _calc_vwap(highs, lows, closes, volumes)

    trades = []
    state = "idle"
    entered_today = False           # 1日1回制限 (連敗防止)
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
            strategy="VWAP", reason=reason,
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

        # 1日1回制限: 一度エントリーしたら同日再エントリーしない
        if entered_today:
            i += 1
            continue

        prev_below = closes[i - 1] < vwap[i - 1]
        curr_above = cl > vwap[i]
        avg_vol = volumes[max(0, i - 5):i].mean() if i >= 1 else 1
        vol_ok = volumes[i] > avg_vol * VOL_MULT if avg_vol > 0 else False
        # 当日トレンドフィルタ: 始値+TREND_PCT以上 (薄陽日を除外)
        bullish_day = cl >= opens[0] * (1 + TREND_PCT)

        if prev_below and curr_above and vol_ok and bullish_day:
            if i + 1 >= n:
                break
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            stop_p = vwap[i] * (1 - STOP_BELOW_VWAP)
            # 動的ターゲット: 当日値幅 × TARGET_RANGE_K を伸び余地に加算
            #   値動き乏しい日は固定+0.8%、ボラ日は値幅に応じて拡大
            day_range = highs[:i + 1].max() - lows[:i + 1].min()
            dyn_target = entry_p + day_range * TARGET_RANGE_K
            min_target = entry_p * (1 + TARGET_PCT)
            target_p = max(min_target, dyn_target)
            if entry_p <= stop_p:
                i += 1
                continue
            qty = calc_position_size(entry_p, stop_p, budget, max_risk)
            state = "in_pos"
            entered_today = True
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
    parser = argparse.ArgumentParser(description="VWAPリバウンド デイトレ")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    targets = DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"VWAPリバウンド: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円",
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
