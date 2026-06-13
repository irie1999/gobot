"""
daytrade_engine_5m.py  ―  5分足デイトレ共通バックテストエンジン
==================================================================
逆指値ロング (backtest_limit_entry.py) を5分足デイトレ用に移植。

【特徴】
- スリッページ・手数料反映 (実運用想定)
- 任意の戦略 (DON/MACD/RSI2/A7/VOL/MOM) を受け取り
- ATR×em/sm/tm の逆指値ロジック (entry/stop/target)
- 引け強制決済 (14:55)
- トレード単位の集計

【使い方】
  from daytrade_engine_5m import backtest_symbol_5m
  from daytrade_strategies_5m import STRATEGIES

  result = backtest_symbol_5m(
      sym="7203.T", name="トヨタ", df=df,
      strategy_fn=STRATEGIES["MACD"],
      strategy_params={"em": 0.0, "sm": 1.5, "tm": 3.0},
  )
"""

from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrade_data import split_by_day, calc_position_size
from daytrade_strategies_5m import (
    ENTRY_START, ENTRY_CUTOFF, FORCE_CLOSE, WARMUP_BARS,
    atr_from_bars,
)

# ── 実運用コスト ─────────────────────────────────────────────
SLIPPAGE_STOP_PCT = 0.003   # 逆指値約定スリッページ (買い+0.3%/売り-0.3%) ※デイトレは日中なので0.5→0.3
FEE_PCT_ONE_WAY   = 0.001   # 片道手数料 0.1% (信用デイトレ前提では0でも良い)


def _apply_slippage_buy(price):
    """買い約定時の不利スリッページ。"""
    return price * (1 + SLIPPAGE_STOP_PCT)


def _apply_fee(amount):
    """手数料控除 (絶対値)。"""
    return amount * FEE_PCT_ONE_WAY


def backtest_day_strategy(day_df, strategy_fn, strategy_params,
                            budget, max_risk,
                            atr_period=14):
    """1日分の5分足DFで指定戦略をbacktest。"""
    opens   = day_df["open"].to_numpy(dtype=float)
    highs   = day_df["high"].to_numpy(dtype=float)
    lows    = day_df["low"].to_numpy(dtype=float)
    closes  = day_df["close"].to_numpy(dtype=float)
    volumes = day_df["volume"].to_numpy(dtype=float)
    times   = day_df.index
    n = len(day_df)

    if n < WARMUP_BARS + 2:
        return []

    # ATR を1日分で計算
    atr_arr = atr_from_bars(highs, lows, closes, atr_period)

    trades = []
    state = "idle"
    entry_p = stop_p = target_p = 0.0
    entry_dt = None
    qty = 0

    def _finish(exit_p, exit_dt, reason):
        # スリッページ・手数料反映
        gross = (exit_p - entry_p) * qty
        fees = _apply_fee(entry_p * qty) + _apply_fee(exit_p * qty)
        pnl = gross - fees
        pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=exit_dt,
            entry_p=entry_p, exit_p=exit_p,
            stop_p=stop_p, target_p=target_p,
            qty=qty, pnl=pnl, pct=pct,
            strategy=strategy_params.get("name", "?"), reason=reason,
        ))

    i = WARMUP_BARS
    while i < n:
        t = times[i].time()

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                _finish(closes[i], times[i], "引け強制")
                break
            # 目標達成優先
            if highs[i] >= target_p:
                _finish(target_p, times[i], "目標達成")
                state = "idle"
                i += 1
                continue
            if lows[i] <= stop_p:
                _finish(stop_p, times[i], "損切り")
                state = "idle"
                i += 1
                continue
            i += 1
            continue

        # idle: 時刻フィルタ
        if t < ENTRY_START or t >= ENTRY_CUTOFF:
            i += 1
            continue

        # シグナル判定
        try:
            sig = strategy_fn(opens, highs, lows, closes, volumes,
                              i, atr_arr, **{k: v for k, v in
                              strategy_params.items() if k != "name"})
        except Exception:
            sig = None

        if sig is None:
            i += 1
            continue

        _, atr_val, em, sm, tm = sig
        if i + 1 >= n:
            break

        # 翌バー寄付で entry (em>0 なら ATR×em 上を逆指値)
        next_open = opens[i + 1]
        order_p = next_open + atr_val * em
        # 翌バーの高値が order_p に到達したら約定
        if highs[i + 1] < order_p:
            # 未約定 → 次バーに進む
            i += 1
            continue

        # 約定 (スリッページ含む実約定価格)
        entry_p_raw = max(order_p, opens[i + 1])
        entry_p = _apply_slippage_buy(entry_p_raw)
        entry_dt = times[i + 1]
        stop_p = entry_p - atr_val * sm
        target_p = entry_p + atr_val * tm

        # 株数計算
        qty = calc_position_size(entry_p, stop_p, budget, max_risk)
        if qty <= 0:
            i += 1
            continue

        state = "in_pos"
        i += 2

    if state == "in_pos":
        _finish(closes[-1], times[-1], "引け強制")

    return trades


def backtest_symbol_5m(sym, name, df, strategy_fn, strategy_params=None,
                       budget=200_000, max_risk=1_000):
    """全期間を1日単位で backtest。"""
    if strategy_params is None:
        strategy_params = {}
    if "name" not in strategy_params:
        strategy_params["name"] = strategy_fn.__name__.replace("signal_", "").upper()

    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None

    trades = []
    for d in dates:
        day_trades = backtest_day_strategy(daily[d], strategy_fn,
                                            strategy_params, budget, max_risk)
        trades.extend(day_trades)

    return dict(symbol=sym, name=name, trades=trades)


def calc_stats(trades, budget=200_000):
    """統計算出 (スリッページ・手数料反映済みPF/勝率)。"""
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0, sharpe=0.0,
                    max_losing_streak=0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    rets = []
    streak = max_streak = 0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
        rets.append(t["pnl"] / budget)
        if t["pnl"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    # Sharpe (年率換算は省略、平均/標準偏差)
    mean_ret = np.mean(rets) if rets else 0
    std_ret = np.std(rets) if rets else 1
    sharpe = mean_ret / std_ret * np.sqrt(252) if std_ret > 0 else 0
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0,
                max_dd=dd, sharpe=sharpe,
                max_losing_streak=max_streak)
