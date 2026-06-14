"""
daytrade_strategies_5m.py  ―  デイトレ戦略集 (5分足版)
==================================================================
逆指値ロング (gobot) の戦略を5分足デイトレ用に移植。
各戦略は (signal_calc_fn, em, sm, tm) のタプルを公開する。

【戦略】
  DON  : Donchian 8本高値ブレイク
  MACD : MACD(8,17,5) クロス + 出来高スパイク + MA10
  RSI2 : RSI(2) 過売り + MA20上昇 + IBS<0.35
  A7   : Stochastic(14,3,3) 過売り反発 + ATR + MA75
  VOL  : 出来高ブレイク (5本高値 + 出来高>20本平均×1.5)
  MOM  : ROC(10)>3% + MA25>MA75
"""

from __future__ import annotations

from datetime import time as dtime
from typing import Callable

import numpy as np
import pandas as pd


# ── 共通定数 ─────────────────────────────────────────────────
# ENTRY_START は環境変数 DAYTRADE_ENTRY_START="HH:MM" で上書き可能
# (compare_entry_times_daytrade.py で複数パターン検証するために用意)
import os as _os
_es_env = _os.environ.get("DAYTRADE_ENTRY_START")
if _es_env:
    try:
        _h, _m = map(int, _es_env.split(":"))
        ENTRY_START = dtime(_h, _m)
    except Exception:
        ENTRY_START = dtime(9, 30)
else:
    ENTRY_START = dtime(9, 30)     # エントリー開始時刻 (デフォルト)
ENTRY_CUTOFF  = dtime(14, 30)    # エントリー期限
FORCE_CLOSE   = dtime(14, 55)    # 強制決済
WARMUP_BARS   = 20               # ウォームアップ (バー数)


# ── 共通技術指標 ──────────────────────────────────────────────
def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
         period: int = 14) -> np.ndarray:
    """ATR (Average True Range) を計算。"""
    n = len(highs)
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        )
    atr = np.zeros(n)
    if n < period:
        return atr
    atr[period-1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period-1) + tr[i]) / period
    return atr


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """EMA (指数移動平均) を計算。"""
    n = len(values)
    ema = np.zeros(n)
    if n == 0:
        return ema
    alpha = 2.0 / (period + 1)
    ema[0] = values[0]
    for i in range(1, n):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i-1]
    return ema


def _sma(values: np.ndarray, period: int) -> np.ndarray:
    """SMA (単純移動平均)。"""
    n = len(values)
    sma = np.full(n, np.nan)
    for i in range(period-1, n):
        sma[i] = values[i-period+1:i+1].mean()
    return sma


# ── 指標キャッシュ ────────────────────────────────────────
# 注: バグが再発したため、安全策としてキャッシュを「素通し」(直接計算) に変更。
# 速度は遅くなるが、結果の正確性を優先。後日、ユニットテスト整備後に
# 再度キャッシュ最適化を試みる。
import threading as _threading
_INDICATOR_CACHE_TLS = _threading.local()


def _get_cache():
    if not hasattr(_INDICATOR_CACHE_TLS, "d"):
        _INDICATOR_CACHE_TLS.d = {}
    return _INDICATOR_CACHE_TLS.d


def _cache_clear():
    """銘柄ごとにキャッシュをクリア (engine から呼ぶ)。"""
    _get_cache().clear()


# 「素通し」実装 - キャッシュなしで直接計算 (正確性優先)
def _ema_cached(values: np.ndarray, period: int) -> np.ndarray:
    return _ema(values, period)


def _sma_cached(values: np.ndarray, period: int) -> np.ndarray:
    return _sma(values, period)


def _rsi_cached(closes: np.ndarray, period: int = 14) -> np.ndarray:
    return _rsi(closes, period)


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI を計算。"""
    n = len(closes)
    rsi = np.zeros(n)
    if n < period + 1:
        return rsi
    gains = np.zeros(n)
    losses = np.zeros(n)
    for i in range(1, n):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains[i] = diff
        else:
            losses[i] = -diff
    avg_g = gains[1:period+1].mean()
    avg_l = losses[1:period+1].mean()
    for i in range(period, n):
        if i > period:
            avg_g = (avg_g * (period-1) + gains[i]) / period
            avg_l = (avg_l * (period-1) + losses[i]) / period
        if avg_l == 0:
            rsi[i] = 100.0
        else:
            rs = avg_g / avg_l
            rsi[i] = 100.0 - 100.0 / (1.0 + rs)
    return rsi


def _stoch(highs, lows, closes, k_period=14, d_period=3, smooth=3):
    """Stochastic %K, %D を計算。"""
    n = len(closes)
    k = np.zeros(n)
    for i in range(k_period-1, n):
        hh = highs[i-k_period+1:i+1].max()
        ll = lows[i-k_period+1:i+1].min()
        if hh == ll:
            k[i] = 50.0
        else:
            k[i] = (closes[i] - ll) / (hh - ll) * 100
    k_smooth = _sma(k, smooth)
    d = _sma(k_smooth, d_period)
    return k_smooth, d


def _stoch_cached(highs, lows, closes, k_period=14, d_period=3, smooth=3):
    """素通し実装 (キャッシュなし)。"""
    return _stoch(highs, lows, closes, k_period, d_period, smooth)


# ─────────────────────────────────────────────────────────────
# 戦略シグナル関数 (各バー時点で「次バー寄付買い」のシグナルを判定)
# 引数: bar インデックス i (i は現在バー、i+1 で寄付買い)
# 戻り値: (entry: bool, order_p_base: float, stop_p: float, target_p: float)
# ─────────────────────────────────────────────────────────────

def signal_don(opens, highs, lows, closes, volumes, i, atr_arr,
                don_period=8, em=0.0, sm=1.5, tm=3.0):
    """Donchian 過去N本高値ブレイク + 陽線。"""
    if i < don_period:
        return None
    don_hi = highs[max(0, i-don_period):i].max()
    if closes[i] > don_hi and closes[i] > opens[i]:
        a = atr_arr[i]
        if a <= 0:
            return None
        # 次バー寄付買い (em=0.0 なら次バー始値、em>0 なら +ATR*em 上)
        return ("entry", a, em, sm, tm)
    return None


def signal_macd(opens, highs, lows, closes, volumes, i, atr_arr,
                 macd_fast=8, macd_slow=17, macd_signal=5,
                 ma_trend=10, vol_ma=10, vol_spike=1.2,
                 em=0.0, sm=1.5, tm=3.0):
    """MACD クロス + 出来高スパイク + MA10上昇 (5分足調整)。"""
    if i < max(macd_slow + macd_signal, vol_ma, ma_trend) + 1:
        return None
    # MACD
    ema_fast = _ema_cached(closes[:i+1], macd_fast)
    ema_slow = _ema_cached(closes[:i+1], macd_slow)
    macd_line = ema_fast - ema_slow
    sig_line = _ema(macd_line, macd_signal)
    # ゴールデンクロス (前バー: 下, 現バー: 上)
    if not (macd_line[i-1] <= sig_line[i-1] and macd_line[i] > sig_line[i]):
        return None
    # MA10 上昇トレンド
    ma = _sma_cached(closes[:i+1], ma_trend)
    if not (closes[i] > ma[i]):
        return None
    # 出来高スパイク
    avg_vol = volumes[max(0, i-vol_ma):i].mean()
    if avg_vol <= 0 or volumes[i] < avg_vol * vol_spike:
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry", a, em, sm, tm)


def signal_rsi2(opens, highs, lows, closes, volumes, i, atr_arr,
                 rsi2_entry=10.0, ma_trend=20,
                 em=0.0, sm=2.0, tm=4.0):
    """RSI(2) 過売り + MA20 上 + IBS<0.35 (押し目反発)。"""
    if i < max(ma_trend, 3) + 1:
        return None
    rsi2 = _rsi_cached(closes[:i+1], 2)
    if rsi2[i] >= rsi2_entry:
        return None
    # MA20 上の押し目
    ma = _sma_cached(closes[:i+1], ma_trend)
    if not (closes[i] > ma[i]):
        return None
    # IBS (Internal Bar Strength)
    rng = highs[i] - lows[i]
    if rng <= 0:
        return None
    ibs = (closes[i] - lows[i]) / rng
    if ibs >= 0.35:
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry", a, em, sm, tm)


def signal_a7(opens, highs, lows, closes, volumes, i, atr_arr,
               k_period=14, d_period=3, smooth=3,
               overbought=70, ma_trend=30,
               em=0.0, sm=1.5, tm=3.0):
    """A7: Stochastic 過売り反発 + MA30トレンド (5分足デイトレ調整)。"""
    if i < max(k_period * 2, ma_trend) + 1:
        return None
    k, d = _stoch_cached(highs[:i+1], lows[:i+1], closes[:i+1],
                   k_period, d_period, smooth)
    # %K が過売り域 (<30) から反発 (前バーより上昇)
    if not (k[i-1] < 30 and k[i] > k[i-1]):
        return None
    # トレンドフィルタ MA75 上
    ma = _sma_cached(closes[:i+1], ma_trend)
    if not (closes[i] > ma[i]):
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry", a, em, sm, tm)


def signal_vol(opens, highs, lows, closes, volumes, i, atr_arr,
                lookback=5, vol_ma=20, vol_spike=1.5,
                em=0.0, sm=1.5, tm=3.0):
    """VOL: 過去N本高値ブレイク + 出来高>20本平均×1.5"""
    if i < max(lookback, vol_ma) + 1:
        return None
    hh = highs[max(0, i-lookback):i].max()
    if closes[i] <= hh:
        return None
    avg_v = volumes[max(0, i-vol_ma):i].mean()
    if avg_v <= 0 or volumes[i] < avg_v * vol_spike:
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry", a, em, sm, tm)


def signal_mom(opens, highs, lows, closes, volumes, i, atr_arr,
                roc_period=10, roc_thr=1.0, ma_fast=10, ma_slow=25,
                em=0.0, sm=1.5, tm=3.0):
    """MOM: ROC(N) > 閾値% + MA10 > MA25 (5分足デイトレ調整)。"""
    if i < max(roc_period, ma_slow) + 1:
        return None
    roc = (closes[i] / closes[i-roc_period] - 1) * 100
    if roc <= roc_thr:
        return None
    ma_f = _sma_cached(closes[:i+1], ma_fast)
    ma_s = _sma_cached(closes[:i+1], ma_slow)
    if not (ma_f[i] > ma_s[i]):
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry", a, em, sm, tm)


# ── 戦略レジストリ ───────────────────────────────────────────
# 各戦略名 → (signal_fn, default_params)
STRATEGIES = {
    "DON":  signal_don,
    "MACD": signal_macd,
    "RSI2": signal_rsi2,
    "A7":   signal_a7,
    "VOL":  signal_vol,
    "MOM":  signal_mom,
}


# ── ATR ヘルパー ────────────────────────────────────────────
def atr_from_bars(highs, lows, closes, period=14):
    return _atr(highs, lows, closes, period)
