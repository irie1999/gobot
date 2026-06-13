"""
risk_metrics_5m.py  ―  デイトレ用 リスク指標
==================================================================
逆指値ロング (risk_metrics.py) を5分足デイトレ用に拡張。

【追加指標】
- Sharpe ratio (年率換算 252日)
- Max drawdown (DD)
- Max losing streak (最大連敗)
- Recovery factor (RF = 総損益 / |MaxDD|)
- Profit factor by day (日次PF)
- Expectancy (期待値 = 勝率×平均利益 - 敗率×|平均損失|)
- Calmar ratio (年率リターン / |MaxDD|)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from datetime import timedelta


def enrich_stats(trades, budget=200_000):
    """トレード列に対し詳細リスク指標を計算。

    引数:
        trades: list[dict] (entry_dt/exit_dt/pnl/qty 必須)
        budget: 初期資金

    戻り値:
        dict (n, wins, win_rate, pf, total_pnl, avg_win, avg_loss,
              max_dd, max_dd_yen, sharpe, sortino, calmar,
              max_losing_streak, recovery_factor, expectancy,
              avg_hold_min, trades_per_day)
    """
    n = len(trades)
    if n == 0:
        return _empty_stats()

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")

    # 累積損益曲線
    eq = budget
    peak = budget
    dd_pct = 0.0
    dd_yen = 0.0
    streak = 0
    max_streak = 0
    rets = []  # トレード単位リターン

    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        cur_dd_pct = (eq - peak) / peak * 100
        cur_dd_yen = eq - peak
        if cur_dd_pct < dd_pct:
            dd_pct = cur_dd_pct
            dd_yen = cur_dd_yen
        rets.append(t["pnl"] / budget)
        if t["pnl"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Sharpe ratio (日次想定で年率化, 252取引日)
    mean_ret = np.mean(rets) if rets else 0
    std_ret = np.std(rets, ddof=1) if len(rets) >= 2 else 1
    sharpe = mean_ret / std_ret * np.sqrt(252) if std_ret > 0 else 0

    # Sortino (下方偏差のみ使う)
    neg_rets = [r for r in rets if r < 0]
    downside = np.std(neg_rets, ddof=1) if len(neg_rets) >= 2 else 1
    sortino = mean_ret / downside * np.sqrt(252) if downside > 0 else 0

    # Recovery factor
    total_pnl = sum(t["pnl"] for t in trades)
    rf = total_pnl / abs(dd_yen) if dd_yen != 0 else float("inf")

    # Calmar = 年率リターン / |MaxDD%|
    # 仮定: backtest期間内のトレードから年率推定
    days_total = _estimate_days(trades)
    annual_ret = (total_pnl / budget) * (365 / max(days_total, 1)) * 100
    calmar = annual_ret / abs(dd_pct) if dd_pct < 0 else float("inf")

    # Expectancy
    win_rate = len(wins) / n
    avg_win = gp / len(wins) if wins else 0
    avg_loss = gl / len(losses) if losses else 0
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

    # 平均保有時間
    holds = []
    for t in trades:
        try:
            delta = t["exit_dt"] - t["entry_dt"]
            holds.append(delta.total_seconds() / 60)
        except Exception:
            pass
    avg_hold_min = np.mean(holds) if holds else 0

    # 1日あたり取引数
    trade_days = set()
    for t in trades:
        dt = t.get("entry_dt")
        if hasattr(dt, "date"):
            trade_days.add(dt.date())
    trades_per_day = n / max(len(trade_days), 1)

    return dict(
        n=n,
        wins=len(wins),
        win_rate=win_rate * 100,
        pf=pf,
        total_pnl=total_pnl,
        avg_win=avg_win,
        avg_loss=-avg_loss,
        max_dd=dd_pct,
        max_dd_yen=dd_yen,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_losing_streak=max_streak,
        recovery_factor=rf,
        expectancy=expectancy,
        avg_hold_min=avg_hold_min,
        trades_per_day=trades_per_day,
        trade_days=len(trade_days),
    )


def _empty_stats():
    return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                avg_win=0.0, avg_loss=0.0,
                max_dd=0.0, max_dd_yen=0.0,
                sharpe=0.0, sortino=0.0, calmar=0.0,
                max_losing_streak=0, recovery_factor=0.0,
                expectancy=0.0, avg_hold_min=0.0,
                trades_per_day=0.0, trade_days=0)


def _estimate_days(trades):
    """トレード列のカバー期間を日数で推定。"""
    if not trades:
        return 1
    try:
        first = trades[0].get("entry_dt")
        last = trades[-1].get("exit_dt") or trades[-1].get("entry_dt")
        if hasattr(first, "date") and hasattr(last, "date"):
            return max(1, (last.date() - first.date()).days + 1)
    except Exception:
        pass
    return 1


def calc_recommend_score(stats_list, total_periods=6):
    """逆指値ロングと同じスコア式 (run_signals.py:calc_recommend_score)。

    引数:
        stats_list: list[stats] (期間別の stats)
        total_periods: 期間数 (6 = 30/60/90/120/150/180)

    戻り値:
        (score, rank) score=0-100, rank=★★★/★★/★/△
    """
    valid = [s for s in stats_list if s["n"] > 0]
    if not valid:
        return 0, "△"

    avg_wr = np.mean([s["win_rate"] for s in valid])
    # PF=∞ は 10 として扱う (逆指値ロング準拠)
    avg_pf = np.mean([min(10, s["pf"]) if s["pf"] != float("inf") else 10
                      for s in valid])
    # 安定性: プラス期間 / 有効期間
    positive = sum(1 for s in valid if s["total_pnl"] > 0)
    stable = positive / len(valid)
    # 平均取引数
    t_trades = np.mean([s["n"] for s in valid])

    score = round(
        avg_wr * 0.4                          # 最大 40点
        + (avg_pf / 10) * 30                  # 最大 30点
        + stable * 20                         # 最大 20点
        + min(t_trades / 20, 1) * 10          # 最大 10点 (20取引で満点)
    )

    if score >= 80:
        rank = "★★★"
    elif score >= 60:
        rank = "★★"
    elif score >= 40:
        rank = "★"
    else:
        rank = "△"

    return score, rank
