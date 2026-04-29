"""
risk_metrics.py  ―  バックテスト結果のリスク指標計算
==========================================================
backtest_limit_entry.run_limit_backtest の戻り値 (trade_log を含む) から
MaxDD / 最大連敗数 / Sharpe 比率 / 資産曲線など、実運用で必要な
リスク指標を計算する共通モジュール。

使い方:
    from risk_metrics import enrich_backtest_result
    result = run_limit_backtest(...)
    enriched = enrich_backtest_result(result, initial_cash=500_000)
    print(enriched["max_drawdown_pct"], enriched["sharpe"])
"""

from __future__ import annotations

import math
from typing import Any


def _sorted_trades(trade_log: list[dict]) -> list[dict]:
    """トレードログを決済日時でソート (未指定時は元順維持)。"""
    try:
        return sorted(trade_log, key=lambda t: t.get("exit_dt") or t.get("entry_dt"))
    except Exception:
        return list(trade_log)


def calc_equity_curve(trade_log: list[dict],
                      initial_cash: float = 500_000) -> list[float]:
    """トレードログから資産曲線 (初期キャッシュからの累積 P&L) を作成。"""
    trades = _sorted_trades(trade_log)
    equity = float(initial_cash)
    curve = [equity]
    for t in trades:
        equity += float(t.get("pnl", 0.0))
        curve.append(equity)
    return curve


def calc_max_drawdown(trade_log: list[dict],
                      initial_cash: float = 500_000) -> tuple[float, float]:
    """
    最大ドローダウンを (金額, パーセンテージ) で返す。
    peak からの下落の最大値。
    """
    if not trade_log:
        return 0.0, 0.0
    curve = calc_equity_curve(trade_log, initial_cash)
    peak       = curve[0]
    max_dd     = 0.0
    max_dd_pct = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd     = peak - v
        dd_pct = (dd / peak * 100.0) if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
    return round(max_dd, 0), round(max_dd_pct, 2)


def calc_max_consecutive_losses(trade_log: list[dict]) -> int:
    """最大連敗数 (pnl <= 0 を負け扱い)。"""
    if not trade_log:
        return 0
    trades     = _sorted_trades(trade_log)
    max_streak = 0
    cur_streak = 0
    for t in trades:
        if float(t.get("pnl", 0.0)) <= 0:
            cur_streak += 1
            if cur_streak > max_streak:
                max_streak = cur_streak
        else:
            cur_streak = 0
    return max_streak


def calc_max_consecutive_wins(trade_log: list[dict]) -> int:
    """最大連勝数 (pnl > 0 を勝ち扱い)。"""
    if not trade_log:
        return 0
    trades     = _sorted_trades(trade_log)
    max_streak = 0
    cur_streak = 0
    for t in trades:
        if float(t.get("pnl", 0.0)) > 0:
            cur_streak += 1
            if cur_streak > max_streak:
                max_streak = cur_streak
        else:
            cur_streak = 0
    return max_streak


def calc_sharpe(trade_log: list[dict],
                initial_cash: float = 500_000,
                trades_per_year: int = 20) -> float:
    """
    Sharpe 比率 (単純化: 各トレードを1観測値として扱う)。
    年率換算は trades_per_year で行う (デフォルト20 = 月2回程度のスイング想定)。
    """
    if len(trade_log) < 2:
        return 0.0
    trades  = _sorted_trades(trade_log)
    returns = [float(t.get("pnl", 0.0)) / float(initial_cash) for t in trades]
    mean_r  = sum(returns) / len(returns)
    var_r   = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r   = math.sqrt(var_r)
    if std_r <= 0:
        return 0.0
    return round(mean_r / std_r * math.sqrt(trades_per_year), 3)


def calc_recovery_factor(trade_log: list[dict],
                         initial_cash: float = 500_000) -> float:
    """リカバリーファクター = 総損益 / MaxDD。MaxDDが0のときは0.0。"""
    if not trade_log:
        return 0.0
    total_pnl = sum(float(t.get("pnl", 0.0)) for t in trade_log)
    max_dd, _ = calc_max_drawdown(trade_log, initial_cash)
    if max_dd <= 0:
        return round(float("inf") if total_pnl > 0 else 0.0, 3)
    return round(total_pnl / max_dd, 3)


def calc_hold_stats(trade_log: list[dict]) -> dict:
    """
    保有日数の統計を返す。

    Returns dict with keys:
      avg        : 全トレードの平均保有日数
      count      : 全トレード数
      target_avg : 目標達成トレードの平均保有日数 (無ければ 0)
      stop_avg   : 損切りトレードの平均保有日数
      tc_avg     : タイムカット (必ず MAX_HOLD なのでほぼ固定)
      held_avg   : 保有中 (期間末で未決済) の平均経過日数
      target_n / stop_n / tc_n / held_n : 件数
    """
    empty = dict(avg=0.0, count=0,
                 target_avg=0.0, stop_avg=0.0, tc_avg=0.0, held_avg=0.0,
                 target_n=0, stop_n=0, tc_n=0, held_n=0, same_day_n=0)
    if not trade_log:
        return empty

    by_reason: dict[str, list[int]] = {
        "目標達成": [], "損切り": [], "タイムカット": [], "保有中": []
    }
    all_days: list[int] = []
    same_day = 0
    for t in trade_log:
        days   = int(t.get("hold_days", 0) or 0)
        reason = str(t.get("reason", ""))
        all_days.append(days)
        if days == 0:
            same_day += 1
        if reason in by_reason:
            by_reason[reason].append(days)

    def _avg(lst: list[int]) -> float:
        return round(sum(lst) / len(lst), 1) if lst else 0.0

    return dict(
        avg=_avg(all_days),
        count=len(all_days),
        target_avg=_avg(by_reason["目標達成"]),
        stop_avg  =_avg(by_reason["損切り"]),
        tc_avg    =_avg(by_reason["タイムカット"]),
        held_avg  =_avg(by_reason["保有中"]),
        target_n=len(by_reason["目標達成"]),
        stop_n  =len(by_reason["損切り"]),
        tc_n    =len(by_reason["タイムカット"]),
        held_n  =len(by_reason["保有中"]),
        same_day_n=same_day,
    )


def enrich_backtest_result(result: dict[str, Any],
                           initial_cash: float = 500_000) -> dict[str, Any]:
    """
    run_limit_backtest の result にリスク指標をインプレースで追加して返す。

    追加されるキー:
      - max_drawdown          : 最大DD (円)
      - max_drawdown_pct      : 最大DD (%)
      - max_consecutive_losses: 最大連敗数
      - max_consecutive_wins  : 最大連勝数
      - sharpe                : Sharpe比率 (年率換算)
      - recovery_factor       : 総損益/MaxDD
    """
    trade_log = result.get("trade_log", []) or []
    max_dd, max_dd_pct = calc_max_drawdown(trade_log, initial_cash)
    result["max_drawdown"]           = max_dd
    result["max_drawdown_pct"]       = max_dd_pct
    result["max_consecutive_losses"] = calc_max_consecutive_losses(trade_log)
    result["max_consecutive_wins"]   = calc_max_consecutive_wins(trade_log)
    result["sharpe"]                 = calc_sharpe(trade_log, initial_cash)
    result["recovery_factor"]        = calc_recovery_factor(trade_log, initial_cash)
    result["hold_stats"]             = calc_hold_stats(trade_log)
    return result


# ── 単体テスト ──
if __name__ == "__main__":
    # ダミーのトレードログ
    from datetime import datetime
    dummy = [
        {"entry_dt": datetime(2025,1,1), "exit_dt": datetime(2025,1,3), "pnl":  5000,
         "hold_days": 2, "reason": "目標達成"},
        {"entry_dt": datetime(2025,1,5), "exit_dt": datetime(2025,1,7), "pnl":  8000,
         "hold_days": 2, "reason": "目標達成"},
        {"entry_dt": datetime(2025,1,10),"exit_dt": datetime(2025,1,12),"pnl": -3000,
         "hold_days": 2, "reason": "損切り"},
        {"entry_dt": datetime(2025,1,15),"exit_dt": datetime(2025,1,30),"pnl": -4000,
         "hold_days": 15, "reason": "タイムカット"},
        {"entry_dt": datetime(2025,2,1), "exit_dt": datetime(2025,2,6), "pnl": 10000,
         "hold_days": 5, "reason": "目標達成"},
        {"entry_dt": datetime(2025,2,8), "exit_dt": datetime(2025,2,8), "pnl": -2000,
         "hold_days": 0, "reason": "損切り"},
    ]
    result = {"trade_log": dummy}
    enriched = enrich_backtest_result(result)
    print("max_drawdown       :", enriched["max_drawdown"])
    print("max_drawdown_pct   :", enriched["max_drawdown_pct"], "%")
    print("max_consec_losses  :", enriched["max_consecutive_losses"])
    print("max_consec_wins    :", enriched["max_consecutive_wins"])
    print("sharpe             :", enriched["sharpe"])
    print("recovery_factor    :", enriched["recovery_factor"])
    print("hold_stats         :", enriched["hold_stats"])
    print("equity_curve       :", calc_equity_curve(dummy))
