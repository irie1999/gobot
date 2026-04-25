"""
daytrade_eod_selector.py  ―  前日終値ベースの翌日候補セレクター
==================================================================
5分足データを日足に集約し、前日終値時点で「翌日上がりそう」な
銘柄を上位N位まで返す。

【セレクター】
  simple   : 前日陽線+1〜3% / 出来高直近10日平均×1.2倍以上 /
             高値引け (引け値が日中値幅の上位30%)
  multi    : 20日MA上 / RSI(14) 50〜75 / 20MA乖離率/出来高/モメンタム複合
  breakout : 20日高値ブレイク + 出来高急増

【使い方】
  from daytrade_eod_selector import build_daily, select_for_date, SELECTORS

  daily_data = build_daily(fetched_5min)   # {sym: daily_df}
  cands = select_for_date(name_map, daily_data,
                          SELECTORS['simple'], target_date, top_n=20)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# 5分足 → 日足
# ─────────────────────────────────────────────────────────────

def aggregate_to_daily(intraday_df: pd.DataFrame) -> pd.DataFrame:
    """5分足の DataFrame を日足 OHLCV に集約。"""
    if intraday_df is None or intraday_df.empty:
        return pd.DataFrame()
    df = intraday_df.copy()
    df["__date__"] = df.index.date
    daily = df.groupby("__date__").agg({
        "open":  "first",
        "high":  "max",
        "low":   "min",
        "close": "last",
        "volume": "sum",
    })
    daily.index = pd.to_datetime(daily.index)
    daily.index.name = "Date"
    return daily.dropna(subset=["close"])


def build_daily(fetched: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """全銘柄の 5分足 → 日足 を一括変換。"""
    return {sym: aggregate_to_daily(df) for sym, df in fetched.items()}


# ─────────────────────────────────────────────────────────────
# セレクター 1: シンプルモメンタム
# ─────────────────────────────────────────────────────────────

def _score_simple_momentum(daily_df: pd.DataFrame, as_of_date) -> float | None:
    """
    前日陽線+1〜3% / 出来高 直近10日平均×1.2倍 / 高値引け の3条件を満たし、
    上昇率 × 出来高比率 × 引け位置 をスコア化。
    """
    df = daily_df[daily_df.index.date <= as_of_date]
    if len(df) < 11:
        return None

    last = df.iloc[-1]
    o, h, l, c, v = last["open"], last["high"], last["low"], last["close"], last["volume"]
    if o <= 0 or v <= 0:
        return None

    pct = (c - o) / o * 100
    if pct < 1.0 or pct > 3.0:
        return None  # 弱すぎ・過熱しすぎ除外

    avg_vol = df["volume"].iloc[-11:-1].mean()
    if avg_vol <= 0 or v < avg_vol * 1.2:
        return None
    vol_ratio = v / avg_vol

    rng = h - l
    if rng <= 0:
        return None
    close_pos = (c - l) / rng  # 0=安値引け, 1=高値引け
    if close_pos < 0.7:
        return None  # 上ヒゲ陰線除外

    return pct * vol_ratio * close_pos


# ─────────────────────────────────────────────────────────────
# セレクター 2: 多指標スコア型
# ─────────────────────────────────────────────────────────────

def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    delta = np.diff(closes[-(period + 1):])
    up = delta[delta > 0].sum()
    down = -delta[delta < 0].sum()
    if down == 0:
        return 100.0
    rs = up / down
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(arr: np.ndarray, span: int) -> float:
    if len(arr) < span:
        return float(arr[-1])
    k = 2.0 / (span + 1.0)
    e = float(arr[0])
    for v in arr[1:]:
        e = v * k + e * (1 - k)
    return e


def _score_multi_factor(daily_df: pd.DataFrame, as_of_date) -> float | None:
    """20MA上 + RSI 50〜75 + MACDシグナル上抜け + 出来高 で複合評価。"""
    df = daily_df[daily_df.index.date <= as_of_date]
    if len(df) < 30:
        return None

    closes = df["close"].to_numpy(dtype=float)
    volumes = df["volume"].to_numpy(dtype=float)

    ma20 = closes[-20:].mean()
    if closes[-1] <= ma20:
        return None
    ma_dev = (closes[-1] - ma20) / ma20 * 100  # 20MAからの上方乖離率

    rsi = _calc_rsi(closes, period=14)
    if rsi < 50 or rsi > 75:
        return None

    # MACD: 12EMA - 26EMA, signal = 9EMA(MACD)
    ema12 = _ema(closes[-26:], 12)
    ema26 = _ema(closes[-26:], 26)
    macd = ema12 - ema26
    # signal は直近MACD系列の9EMA。簡易に MACD > 0 を採用
    if macd <= 0:
        return None

    avg_vol = volumes[-11:-1].mean()
    if avg_vol <= 0 or volumes[-1] < avg_vol * 1.2:
        return None
    vol_ratio = volumes[-1] / avg_vol

    return ma_dev * (rsi - 50) * vol_ratio


# ─────────────────────────────────────────────────────────────
# セレクター 3: ブレイクアウト型
# ─────────────────────────────────────────────────────────────

def _score_breakout(daily_df: pd.DataFrame, as_of_date) -> float | None:
    """20日高値ブレイク + 出来高急増。突破率 × 出来高比率 をスコア化。"""
    df = daily_df[daily_df.index.date <= as_of_date]
    if len(df) < 25:
        return None

    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    volumes = df["volume"].to_numpy(dtype=float)

    don_hi_20 = highs[-21:-1].max()  # 当日除く20日高値
    if don_hi_20 <= 0 or closes[-1] <= don_hi_20:
        return None
    breakout_pct = (closes[-1] - don_hi_20) / don_hi_20 * 100

    avg_vol = volumes[-11:-1].mean()
    if avg_vol <= 0 or volumes[-1] < avg_vol * 1.2:
        return None
    vol_ratio = volumes[-1] / avg_vol

    return breakout_pct * vol_ratio


# ─────────────────────────────────────────────────────────────
# 公開API
# ─────────────────────────────────────────────────────────────

SELECTORS = {
    "simple":   _score_simple_momentum,
    "multi":    _score_multi_factor,
    "breakout": _score_breakout,
}


def select_for_date(name_map: dict[str, str],
                    daily_data: dict[str, pd.DataFrame],
                    score_fn,
                    as_of_date,
                    top_n: int = 20) -> list[tuple[str, str, float]]:
    """
    指定日 (as_of_date) 終値時点で score_fn が高い上位N銘柄を返す。

    Returns:
        [(symbol, name, score), ...]  (score 降順)
    """
    rows = []
    for sym, df in daily_data.items():
        s = score_fn(df, as_of_date)
        if s is None or s <= 0:
            continue
        rows.append((sym, name_map.get(sym, sym), float(s)))
    rows.sort(key=lambda x: -x[2])
    return rows[:top_n]
