"""
daytrade_gap_reversal_bi.py  ―  双方向ギャップリバーサル戦略 (買い+空売り)
==================================================================
信用デイトレ前提で、両方向のギャップ埋めを狙う。
- ギャップダウン (-1〜-3%) → 反発確認後に買い → 前日終値で利確
- ギャップアップ (+1〜+3%) → 失速確認後に空売り → 前日終値で利確

【単方向版 (daytrade_gap_reversal.py) との違い】
  - 売り(空売り)シグナルを追加
  - long_only / short_only / both をオプションで切替可能
  - prime/winners ユニバース選択可

【使い方】
  python daytrade_gap_reversal_bi.py                       # winners 60日
  python daytrade_gap_reversal_bi.py --universe prime --days 60
  python daytrade_gap_reversal_bi.py --side long           # 買いのみ
  python daytrade_gap_reversal_bi.py --side short          # 空売りのみ
  python daytrade_gap_reversal_bi.py --side both           # 両方 (デフォルト)
"""

from __future__ import annotations

import argparse
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

BUDGET       = 200_000
MAX_RISK     = 1_000
GAP_MIN_PCT  = 1.0
GAP_MAX_PCT  = 3.0
VOL_MULT     = 1.2          # エントリーバー出来高 ≥ 直前5本平均×1.2
STOP_MAX_PCT = 2.0          # 損切り距離の上限 (2.0%でクリップ)
FORCE_CLOSE  = dtime(14, 55)
ENTRY_CUTOFF = dtime(11, 0)
WARMUP       = 3


def _load_universe(name):
    if name == "winners":
        from daytrade_donchian_winners import SYMBOLS
        return SYMBOLS
    if name == "prime":
        from symbols_listed_all import SYMBOLS
        return SYMBOLS
    if name == "watch":
        from daytrade_symbols import DAYTRADE_SYMBOLS
        return DAYTRADE_SYMBOLS
    raise ValueError(f"unknown universe: {name}")


def backtest_day_long(day_df, prev_close, budget, max_risk):
    """ギャップダウン → 買いリバーサル"""
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
            entry_dt=entry_dt, exit_dt=exit_dt, side="long",
            entry_p=entry_p, exit_p=exit_p, stop_p=stop_p, target_p=target_p,
            qty=qty, pnl=pnl, pct=pct, strategy="GapRev-L", reason=reason,
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
                state = "idle"; i += 1; continue
            if hi >= target_p:
                _finish(target_p, times[i], "目標達成")
                state = "idle"; i += 1; continue
            progress = (hi - entry_p) / (target_p - entry_p) if target_p > entry_p else 0
            if progress >= 0.5:
                new_stop = entry_p
                if new_stop > stop_p:
                    stop_p = new_stop
            i += 1; continue

        if t >= ENTRY_CUTOFF:
            i += 1; continue

        # 反発確認: ①始値超え ②陽線 ③出来高条件
        if cl > opens[0] and cl > opens[i]:
            ref_start = max(0, i - 5)
            avg_vol = volumes[ref_start:i].mean() if i > ref_start else 0
            if avg_vol <= 0 or volumes[i] < avg_vol * VOL_MULT:
                i += 1; continue
            if i + 1 >= n:
                break
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            day_low = lows[:i + 1].min()
            stop_floor = entry_p * (1 - STOP_MAX_PCT / 100)
            stop_p = max(day_low * 0.999, stop_floor)
            target_p = prev_close
            if entry_p >= target_p or entry_p <= stop_p:
                i += 1; continue
            qty = calc_position_size(entry_p, stop_p, budget, max_risk)
            if qty <= 0:
                i += 1; continue
            state = "in_pos"; i += 2; continue

        i += 1

    if state == "in_pos":
        _finish(closes[-1], times[-1], "引け強制")
    return trades


def backtest_day_short(day_df, prev_close, budget, max_risk):
    """ギャップアップ → 空売りリバーサル (買いの逆)"""
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
    if gap_pct < GAP_MIN_PCT or gap_pct > GAP_MAX_PCT:
        return []

    trades = []
    state = "idle"
    entry_p = stop_p = target_p = 0.0
    entry_dt = None
    qty = 0

    def _finish(exit_p, exit_dt, reason):
        # 空売り: pnl = (entry - exit) * qty
        pnl = (entry_p - exit_p) * qty
        pct = (entry_p - exit_p) / entry_p * 100
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=exit_dt, side="short",
            entry_p=entry_p, exit_p=exit_p, stop_p=stop_p, target_p=target_p,
            qty=qty, pnl=pnl, pct=pct, strategy="GapRev-S", reason=reason,
        ))

    i = WARMUP
    while i < n:
        t = times[i].time()
        hi, lo, cl = highs[i], lows[i], closes[i]

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                _finish(cl, times[i], "引け強制")
                break
            # 空売り: stop は上、target は下
            if hi >= stop_p:
                _finish(stop_p, times[i], "損切り")
                state = "idle"; i += 1; continue
            if lo <= target_p:
                _finish(target_p, times[i], "目標達成")
                state = "idle"; i += 1; continue
            # 半分到達でブレイクイーブンへトレール
            progress = (entry_p - lo) / (entry_p - target_p) if entry_p > target_p else 0
            if progress >= 0.5:
                new_stop = entry_p
                if new_stop < stop_p:
                    stop_p = new_stop
            i += 1; continue

        if t >= ENTRY_CUTOFF:
            i += 1; continue

        # 失速確認: ①始値割れ ②陰線 ③出来高条件
        if cl < opens[0] and cl < opens[i]:
            ref_start = max(0, i - 5)
            avg_vol = volumes[ref_start:i].mean() if i > ref_start else 0
            if avg_vol <= 0 or volumes[i] < avg_vol * VOL_MULT:
                i += 1; continue
            if i + 1 >= n:
                break
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            day_high = highs[:i + 1].max()
            # 空売り損切り: 当日高値の1.001倍 or エントリー+STOP_MAX_PCT% の浅い方
            stop_ceil = entry_p * (1 + STOP_MAX_PCT / 100)
            stop_p = min(day_high * 1.001, stop_ceil)
            target_p = prev_close
            if entry_p <= target_p or entry_p >= stop_p:
                i += 1; continue
            qty = calc_position_size(entry_p, stop_p, budget, max_risk)
            if qty <= 0:
                i += 1; continue
            state = "in_pos"; i += 2; continue

        i += 1

    if state == "in_pos":
        _finish(closes[-1], times[-1], "引け強制")
    return trades


def backtest_symbol(sym, name, df, side, budget, max_risk):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None
    trades = []
    prev_close = None
    for d in dates:
        if prev_close is not None:
            if side in ("long", "both"):
                trades.extend(backtest_day_long(daily[d], prev_close, budget, max_risk))
            if side in ("short", "both"):
                trades.extend(backtest_day_short(daily[d], prev_close, budget, max_risk))
        prev_close = float(daily[d].iloc[-1]["close"])
    return dict(symbol=sym, name=name, trades=trades)


def calc_stats(trades, budget):
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
    parser = argparse.ArgumentParser(description="双方向ギャップリバーサル デイトレ")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--max-risk", type=int, default=MAX_RISK)
    parser.add_argument("--universe", choices=["watch", "winners", "prime"],
                        default="winners",
                        help="対象銘柄群 (デフォルト: winners)")
    parser.add_argument("--side", choices=["long", "short", "both"], default="both",
                        help="買いのみ/空売りのみ/両方 (デフォルト: both)")
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
    parser.add_argument("--limit", type=int, default=0,
                        help="銘柄数制限 (テスト用)")
    args = parser.parse_args()

    targets = _load_universe(args.universe)
    if args.limit > 0:
        targets = targets[:args.limit]
    symbols = [s for s, _ in targets]
    print(f"GapRev-Bi [{args.side}] {args.universe}: {len(targets)}銘柄 / "
          f"{args.days}日 / 予算{args.budget:,}円 / リスク{args.max_risk:,}円",
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
        r = backtest_symbol(sym, name, fetched[sym], args.side,
                            args.budget, args.max_risk)
        if r:
            items.append(r)

    all_trades = sorted([t for it in items for t in it["trades"]],
                        key=lambda x: str(x.get("entry_dt", "")))
    long_trades = [t for t in all_trades if t.get("side") == "long"]
    short_trades = [t for t in all_trades if t.get("side") == "short"]

    s_all   = calc_stats(all_trades, args.budget)
    s_long  = calc_stats(long_trades, args.budget)
    s_short = calc_stats(short_trades, args.budget)

    print(f"\n{'='*78}")
    print(f"  サマリ ({len(items)}銘柄 / side={args.side})")
    print("=" * 78)
    print(f"  全体    : 取引{s_all['n']:>4}  勝率{s_all['win_rate']:>5.1f}%  "
          f"PF{_pf(s_all['pf']):>6}  損益{s_all['total_pnl']:>+10,.0f}円  "
          f"DD{s_all['max_dd']:>+5.1f}%")
    if args.side in ("long", "both"):
        print(f"  買いのみ: 取引{s_long['n']:>4}  勝率{s_long['win_rate']:>5.1f}%  "
              f"PF{_pf(s_long['pf']):>6}  損益{s_long['total_pnl']:>+10,.0f}円  "
              f"DD{s_long['max_dd']:>+5.1f}%")
    if args.side in ("short", "both"):
        print(f"  空売り  : 取引{s_short['n']:>4}  勝率{s_short['win_rate']:>5.1f}%  "
              f"PF{_pf(s_short['pf']):>6}  損益{s_short['total_pnl']:>+10,.0f}円  "
              f"DD{s_short['max_dd']:>+5.1f}%")

    print(f"\n{'='*78}")
    print(f"  銘柄別 (損益上位20)")
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
    for name, sym, ist in rows[:20]:
        disp = name[:20] if len(name) <= 20 else name[:19] + "…"
        print(f"{disp:<22} {sym:<8} {ist['n']:>4} {ist['win_rate']:>4.0f}% "
              f"{_pf(ist['pf']):>7} {ist['total_pnl']:>+10,.0f} "
              f"{ist['max_dd']:>+5.1f}%")
    print("=" * 78)


if __name__ == "__main__":
    main()
