"""
check_signals_daytrade.py  ―  デイトレ シグナル確認モジュール
==================================================================
逆指値ロング (check_signals_stop.py / check_signals_breakout.py) を
5分足デイトレ用に統合移植。

【役割】
- 1銘柄 × 1戦略の backtest を実行 → 期間別統計を返す
- 直近Nバーでシグナルが発生したか判定 (entry/active)
- run_signals_daytrade / run_signals_holdout_all_daytrade から呼ばれる

【スコア計算】
- calc_recommend_score: 期間別統計から総合スコア (★★★/★★/★/△)

【WATCHLIST】
- daytrade_combined_watchlist.py または各戦略別 watchlist から読み込み
- 戦略でフィルタ可
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from daytrade_data import load_intraday_batch, split_by_day
from daytrade_engine_5m import backtest_symbol_5m, calc_stats
from daytrade_strategies_5m import STRATEGIES
from daytrade_strategies_5m_short import STRATEGIES_SHORT
from risk_metrics_5m import enrich_stats, calc_recommend_score

JST = timezone(timedelta(hours=9))

# 期間
SIGNAL_PERIODS = [30, 60, 90, 120, 150, 180]
DEFAULT_BACKTEST_DAYS = 365

# 共通: 全戦略 (Long + Short)
ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}

# WATCHLIST デフォルト読み込み
DEFAULT_WATCHLIST = "daytrade_combined_watchlist.py"


def _load_watchlist(file_path=None):
    """WATCHLIST を読み込む。"""
    if file_path is None:
        file_path = DEFAULT_WATCHLIST
    p = Path(file_path)
    if not p.exists():
        return []
    import importlib.util
    spec = importlib.util.spec_from_file_location("wl", p)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return []
    return getattr(mod, "SYMBOLS", [])


WATCHLIST = _load_watchlist()


def backtest_one(symbol, name, strategy):
    """1銘柄 × 1戦略の backtest 全期間 + 期間別統計。

    戻り値:
        dict {
            symbol, name, strategy,
            trades, all_stats,
            period_stats: {30: stats, 60: stats, ...},
            score, rank,
            recent_signals: list  # 直近の発生シグナル
        }
    """
    fn = ALL_STRATEGIES.get(strategy)
    if fn is None:
        return None

    # データロード
    fetched = load_intraday_batch([symbol], DEFAULT_BACKTEST_DAYS,
                                    source="local", quiet=True)
    df = fetched.get(symbol)
    if df is None or df.empty:
        return None

    # backtest
    r = backtest_symbol_5m(symbol, name, df, fn,
                            strategy_params={"name": strategy},
                            budget=200_000, max_risk=1_000)
    if not r:
        return None

    trades = r["trades"]
    today = datetime.now(JST).date()

    # 期間別統計
    period_stats = {}
    for days in SIGNAL_PERIODS:
        cutoff = today - timedelta(days=days)
        sub = [t for t in trades
               if hasattr(t.get("entry_dt"), "date")
               and t["entry_dt"].date() >= cutoff]
        period_stats[days] = enrich_stats(sub)

    # 全期間
    all_stats = enrich_stats(trades)

    # スコア
    score, rank = calc_recommend_score(list(period_stats.values()))

    # 直近シグナル抽出 (直近7日)
    cutoff_7d = today - timedelta(days=7)
    recent_signals = []
    for t in trades:
        dt = t.get("entry_dt")
        if hasattr(dt, "date") and dt.date() >= cutoff_7d:
            recent_signals.append(t)

    return dict(
        symbol=symbol, name=name, strategy=strategy,
        trades=trades, all_stats=all_stats,
        period_stats=period_stats,
        score=score, rank=rank,
        recent_signals=recent_signals,
        last_price=float(df.iloc[-1]["close"]),
    )


def run_all(workers=4, watchlist=None):
    """WATCHLIST 全銘柄を並列でbacktest。"""
    targets = watchlist or WATCHLIST
    if not targets:
        return []

    # WATCHLIST が (symbol, name) 形式の場合、デフォルト戦略を割り当て
    # (symbol, name, strategy) 形式なら strategy を使う
    items = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for entry in targets:
            if len(entry) == 3:
                sym, name, strat = entry
            else:
                sym, name = entry[:2]
                strat = "DON"  # デフォルト
            futs[ex.submit(backtest_one, sym, name, strat)] = (sym, strat)
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    items.append(r)
            except Exception:
                pass

    # スコア順ソート
    items.sort(key=lambda x: -x["score"])
    return items
