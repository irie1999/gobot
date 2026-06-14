"""
daytrade_strategies_5m_short.py  ―  デイトレ戦略集 (5分足版・空売り)
==================================================================
逆指値ロングの空売り戦略 (DON_S/MOM_S/GAP_S/A7_S/MACD_S/RSI2_S/VOL_S) を
5分足デイトレ用に移植。

【特徴】
- 空売り (信用デイトレ前提)
- entry_p = next_open - ATR×em (逆指値で売る)
- target_p = entry_p - ATR×tm (下落で利益確定)
- stop_p = entry_p + ATR×sm (上昇で損切り)
- 引け強制決済 (買戻し) 14:55

【戦略】
  DON_S  : 過去N本安値ブレイクダウン
  MACD_S : MACD デッドクロス + 出来高 + MA10下
  RSI2_S : RSI(2)>90 + MA20下 + IBS>0.65 (戻り売り)
  A7_S   : Stochastic 過買い反落 + MA75下
  VOL_S  : 5本安値 + 出来高1.5倍
  MOM_S  : ROC10<-3% + MA25<MA75
  GAP_S  : 寄りギャップアップ +1-3% → 失速確認後 空売り
"""

from __future__ import annotations

import numpy as np

from daytrade_strategies_5m import (
    _atr, _ema, _sma, _rsi, _stoch,
    _ema_cached, _sma_cached, _rsi_cached, _stoch_cached,
    atr_from_bars,
)


def signal_don_short(opens, highs, lows, closes, volumes, i, atr_arr,
                     don_period=8, em=0.0, sm=1.5, tm=3.0):
    """過去N本安値ブレイクダウン + 陰線。"""
    if i < don_period:
        return None
    don_lo = lows[max(0, i-don_period):i].min()
    if closes[i] < don_lo and closes[i] < opens[i]:
        a = atr_arr[i]
        if a <= 0:
            return None
        return ("entry_short", a, em, sm, tm)
    return None


def signal_macd_short(opens, highs, lows, closes, volumes, i, atr_arr,
                       macd_fast=8, macd_slow=17, macd_signal=5,
                       ma_trend=10, vol_ma=10, vol_spike=1.2,
                       em=0.0, sm=1.5, tm=3.0):
    """MACD デッドクロス + 出来高 + MA10下。"""
    if i < max(macd_slow + macd_signal, vol_ma, ma_trend) + 1:
        return None
    ema_fast = _ema_cached(closes[:i+1], macd_fast)
    ema_slow = _ema_cached(closes[:i+1], macd_slow)
    macd_line = ema_fast - ema_slow
    sig_line = _ema(macd_line, macd_signal)
    # デッドクロス
    if not (macd_line[i-1] >= sig_line[i-1] and macd_line[i] < sig_line[i]):
        return None
    ma = _sma_cached(closes[:i+1], ma_trend)
    if not (closes[i] < ma[i]):
        return None
    avg_vol = volumes[max(0, i-vol_ma):i].mean()
    if avg_vol <= 0 or volumes[i] < avg_vol * vol_spike:
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry_short", a, em, sm, tm)


def signal_rsi2_short(opens, highs, lows, closes, volumes, i, atr_arr,
                       rsi2_entry=90.0, ma_trend=20,
                       em=0.0, sm=2.0, tm=4.0):
    """RSI(2)>90 + MA20下 + IBS>0.65 (戻り売り)。"""
    if i < max(ma_trend, 3) + 1:
        return None
    rsi2 = _rsi_cached(closes[:i+1], 2)
    if rsi2[i] <= rsi2_entry:
        return None
    ma = _sma_cached(closes[:i+1], ma_trend)
    if not (closes[i] < ma[i]):
        return None
    rng = highs[i] - lows[i]
    if rng <= 0:
        return None
    ibs = (closes[i] - lows[i]) / rng
    if ibs <= 0.65:
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry_short", a, em, sm, tm)


def signal_a7_short(opens, highs, lows, closes, volumes, i, atr_arr,
                    k_period=14, d_period=3, smooth=3,
                    ma_trend=30,
                    em=0.0, sm=1.5, tm=3.0,
                    _precomp=None):
    """Stochastic 過買い反落 + MA30下トレンド (5分足調整)。

    _precomp が渡された場合は事前計算済みの k/d/ma を使用 (高速化)。
    """
    if i < max(k_period * 2, ma_trend) + 1:
        return None
    if _precomp is not None and "stoch_k" in _precomp:
        k = _precomp["stoch_k"]
        ma = _precomp["ma_a7"]
    else:
        k, _ = _stoch(highs[:i+1], lows[:i+1], closes[:i+1],
                       k_period, d_period, smooth)
        ma = _sma(closes[:i+1], ma_trend)
    if not (k[i-1] > 70 and k[i] < k[i-1]):
        return None
    if not (closes[i] < ma[i]):
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry_short", a, em, sm, tm)


def signal_vol_short(opens, highs, lows, closes, volumes, i, atr_arr,
                     lookback=5, vol_ma=20, vol_spike=1.5,
                     em=0.0, sm=1.5, tm=3.0):
    """過去N本安値ブレイク + 出来高1.5倍。"""
    if i < max(lookback, vol_ma) + 1:
        return None
    ll = lows[max(0, i-lookback):i].min()
    if closes[i] >= ll:
        return None
    avg_v = volumes[max(0, i-vol_ma):i].mean()
    if avg_v <= 0 or volumes[i] < avg_v * vol_spike:
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry_short", a, em, sm, tm)


def signal_mom_short(opens, highs, lows, closes, volumes, i, atr_arr,
                     roc_period=10, roc_thr=-3.0, ma_fast=10, ma_slow=25,
                     em=0.0, sm=1.5, tm=3.0):
    """ROC10 < -3% + MA10 < MA25 (5分足調整、ロング MOM と対称化)。"""
    if i < max(roc_period, ma_slow) + 1:
        return None
    roc = (closes[i] / closes[i-roc_period] - 1) * 100
    if roc >= roc_thr:
        return None
    ma_f = _sma_cached(closes[:i+1], ma_fast)
    ma_s = _sma_cached(closes[:i+1], ma_slow)
    if not (ma_f[i] < ma_s[i]):
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry_short", a, em, sm, tm)


def signal_gap_short(opens, highs, lows, closes, volumes, i, atr_arr,
                     gap_min_pct=1.0, gap_max_pct=3.0,
                     fade_bars=3, vol_spike=1.2, vol_ma=10,
                     em=0.0, sm=1.5, tm=3.0,
                     prev_close=None):
    """寄りギャップアップ +1-3% → 失速確認後 空売り。

    引数 prev_close: 前日終値 (省略時はその日の最初のbarのopenを参考)
    """
    # 当日の最初のbarが必要 (シンプルな日次比較は省略)
    # i=fade_bars未満は判定不可
    if i < fade_bars or i < vol_ma:
        return None
    # 当日の開始openと前日終値 (相対近似)
    day_open = opens[0]
    # prev_close は省略 → day_open の代替を使う
    if prev_close is None:
        # 1日内で判定不能。簡略化のため、当日始値からの上昇率を見る
        return None
    gap_pct = (day_open - prev_close) / prev_close * 100
    if not (gap_min_pct <= gap_pct <= gap_max_pct):
        return None
    # 失速確認: 始値割れ + 陰線
    if closes[i] >= day_open or closes[i] >= opens[i]:
        return None
    # 出来高
    avg_v = volumes[max(0, i-vol_ma):i].mean()
    if avg_v <= 0 or volumes[i] < avg_v * vol_spike:
        return None
    a = atr_arr[i]
    if a <= 0:
        return None
    return ("entry_short", a, em, sm, tm)


# ── 戦略レジストリ (空売り) ─────────────────────────────────
STRATEGIES_SHORT = {
    "DON_S":  signal_don_short,
    "MACD_S": signal_macd_short,
    "RSI2_S": signal_rsi2_short,
    "A7_S":   signal_a7_short,
    "VOL_S":  signal_vol_short,
    "MOM_S":  signal_mom_short,
    # GAP_S は前日終値が必要なので、エンジン側でハンドリング
    # "GAP_S":  signal_gap_short,
}
