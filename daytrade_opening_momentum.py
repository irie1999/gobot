"""
daytrade_opening_momentum.py  ―  寄付モメンタム押し目戦略 (v2)
==================================================================
寄付5本 (9:00-9:25) で強い上昇を確認した銘柄を「強気監視」に登録し、
その後の高値からの押し目→反発バーで押し目買いを入れる。
v1 (寄付3本目買い) は寄付ジャンプの天井を掴みやすかったため再設計。

【エントリー】
  Step 1 (9:25時点で勢い判定):
    - 5本目終値 > 寄付 × (1 + MOMENTUM_PCT)  (寄付+0.3%)
    - 5本中 BULLISH_MIN 本以上が陽線         (3/5)
    Step 1 が成立しない日はその日エントリーなし。

  Step 2 (9:25〜11:00で押し目待ち):
    - 9:00以降の最高値を pivot_high として追跡
    - 現バー安値 ≤ pivot_high × (1 - PULLBACK_PCT)
    - 現バーが陽線 (close > open)
    → 翌バー寄付で買い

【決済】
  ストップ: 押し目バー安値 × (1 - STOP_BUFFER)
  目標   : エントリー + (エントリー - ストップ) × TARGET_R
  トレーリング: 2段階 (建値撤退 → +0.4Rロック)
  強制決済: EXIT_CUTOFF (11:30)
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
PRE_BARS       = 5          # 9:00-9:25 の5本で勢い判定
MOMENTUM_PCT   = 0.005      # 5本目終値が寄付+0.5%以上 (勢い銘柄に絞る)
BULLISH_MIN    = 4          # 5本中4本以上が陽線 (強気度を厳格化)
PULLBACK_PCT   = 0.005      # 高値から-0.5%以上の押し目で待機 (浅い押し目を除外)
STOP_BUFFER    = 0.003      # 押し目安値 ×(1 - 0.3%) をストップ (ヒゲ刈り回避)
TARGET_R       = 2.0        # R:R 2.0:1 (損益分岐に必要なW/L比を確保)
# 2段階トレーリング: (進捗率, ロックするリスク倍率)
TRAIL_STEPS = [
    (0.375, 0.0),   # +0.75R進捗 → 建値撤退
    (0.75,  0.5),   # +1.5R進捗  → +0.5Rロック
]
ENTRY_CUTOFF   = dtime(11, 0)
EXIT_CUTOFF    = dtime(11, 30)
FORCE_CLOSE    = dtime(14, 55)


def backtest_day(day_df, prev_close=None, budget=BUDGET, max_risk=MAX_RISK):
    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times  = day_df.index
    n = len(day_df)

    if n < PRE_BARS + 3:
        return []

    # === Step 1: 9:25 (PRE_BARS本目) で勢い判定 ===
    open_price = opens[0]
    check_close = closes[PRE_BARS - 1]
    if check_close < open_price * (1 + MOMENTUM_PCT):
        return []
    bullish_count = sum(1 for j in range(PRE_BARS) if closes[j] > opens[j])
    if bullish_count < BULLISH_MIN:
        return []

    # === Step 2: PRE_BARS以降、押し目→反発バーを待つ ===
    entry_p = stop_p = target_p = orig_stop = 0.0
    entry_dt = None
    qty = 0
    trail_level = 0
    state = "wait_pullback"
    pivot_high = highs[:PRE_BARS].max()
    stop_labels = ("損切り", "建値撤退", "+0.5Rロック")

    def _finish(exit_p, exit_dt, reason):
        pnl = (exit_p - entry_p) * qty
        pct = (exit_p - entry_p) / entry_p * 100
        return dict(entry_dt=entry_dt, exit_dt=exit_dt,
                    entry_p=entry_p, exit_p=exit_p,
                    stop_p=stop_p, target_p=target_p,
                    qty=qty, pnl=pnl, pct=pct,
                    strategy="OpenMomentum", reason=reason)

    i = PRE_BARS
    while i < n:
        t = times[i].time()
        hi, lo, cl, op = highs[i], lows[i], closes[i], opens[i]

        if state == "wait_pullback":
            if t >= ENTRY_CUTOFF:
                return []
            # 高値更新
            if hi > pivot_high:
                pivot_high = hi
            # 押し目+陽線で次バー寄付エントリー
            pullback_trigger = pivot_high * (1 - PULLBACK_PCT)
            if lo <= pullback_trigger and cl > op:
                if i + 1 >= n:
                    return []
                entry_p = opens[i + 1]
                entry_dt = times[i + 1]
                stop_p = lo * (1 - STOP_BUFFER)
                orig_stop = stop_p
                if entry_p <= stop_p:
                    i += 1
                    continue
                target_p = entry_p + (entry_p - stop_p) * TARGET_R
                qty = calc_position_size(entry_p, stop_p, budget, max_risk)
                state = "in_pos"
                trail_level = 0
                i += 2
                continue
            i += 1
            continue

        # state == "in_pos"
        if t >= EXIT_CUTOFF:
            return [_finish(cl, times[i], "前場終了")]
        if lo <= stop_p:
            reason = stop_labels[min(trail_level, len(stop_labels) - 1)]
            return [_finish(stop_p, times[i], reason)]
        if hi >= target_p:
            return [_finish(target_p, times[i], "目標達成")]
        # 2段階トレーリング
        if target_p > entry_p:
            risk = entry_p - orig_stop
            progress = (hi - entry_p) / (target_p - entry_p)
            for idx, (trigger, lock_r) in enumerate(TRAIL_STEPS):
                if progress >= trigger and trail_level <= idx:
                    new_stop = entry_p + risk * lock_r
                    if new_stop > stop_p:
                        stop_p = new_stop
                        trail_level = idx + 1
        i += 1

    if state == "in_pos":
        return [_finish(closes[-1], times[-1], "引け強制")]
    return []


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
