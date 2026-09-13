"""mirror_signals.py — ロング6戦略(MACDTF/A7/RSI2/DON/VOLTF/MOM)の『忠実な鏡像』ショート信号。

#7(lss鏡像=ショート候補を逆指値買い)用。従来 scan_mirror_daytrade は check_signals_short の
calc_*_short を使っていたが、それらはロングの厳密な反転になっていない箇所が多く(下記)、
信号頻度・合格銘柄数がロングと非対称になっていた。本モジュールは backtest_limit_entry /
scan_breakout_entry のロング calc 関数を **条件ごとに1:1で反転** して、信号頻度・品質が
ロングと対称になるようにする(=「すべてをロングの鏡像」)。

ロング calc(反転元)と、旧 calc_*_short との主な非対称(修正点):
  MACDTF: 長=ヒストグラム zero-cross-up|上加速 & c>MA10 & c>MA50。
          旧MACD_S=macd/signalデッドクロス(別ロジック) → 本版はヒストグラムを下方向に反転。
  A7    : 長=デッド/ゴールデン: golden & slow_k<買われすぎ(70) & c>MA75。
          旧A7_S=prev_k>買われすぎ(条件が別) → 本版は slow_k>売られすぎ(30) に反転。
  RSI2  : 長=rsi2<=10 & c>MA200 & IBS<0.35 → 反転 rsi2>=90 & c<MA200 & IBS>0.65(旧RSI2_Sと概ね一致)。
  DON   : 長=c>15日高値 & c>MA50。 旧DON_S=20日安値(頻度非対称) → 本版は15日安値 & c<MA50。
  VOLTF : 長=c>5日高値 & vol>20日平均×1.5 & c>MA50。 旧は短版なし(GAP_Sで代用=別物・ほぼ0発火)
          → 本版で c<5日安値 & vol>×1.5 & c<MA50 を新設(VOLTFの厳密反転)。
  MOM   : 長=ROC(10)>3% & MA25>MA75 & vol>20日平均×1.2。 旧MOM_S=出来高フィルタ欠落 → 本版で追加。

注意: ATR・出来高スパイク倍率・MA期間・ストキャス期間・RSI閾値 はロングと同一定数を使う
(backtest_limit_entry から import)。方向条件(>/<, golden/dead, 高値/安値, 売られ/買われ)だけ反転。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest_limit_entry import (
    ATR_PERIOD_MACD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    VOL_MA_PERIOD, VOL_SPIKE_MULT, MA_TREND_PERIOD,
    STOCH_K_PERIOD, STOCH_D_PERIOD, STOCH_SMOOTH, STOCH_OVERBOUGHT,
    MA_TREND_PERIOD_A7, ATR_PERIOD_A7, ATR_PERIOD_RSI2, RSI2_ENTRY,
)

# ストキャスの「買われすぎ(70)」を鏡像反転した「売られすぎ(30)」。
STOCH_OVERSOLD = 100.0 - STOCH_OVERBOUGHT


def _atr(df: pd.DataFrame, span: int) -> pd.Series:
    c, h, l = df["close"], df["high"], df["low"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(span=span, adjust=False).mean()


def mirror_macdtf(df: pd.DataFrame) -> pd.DataFrame:
    """calc_macd_tf の忠実反転: ヒストグラム zero-cross-DOWN | 下加速 & 出来高 & c<MA10 & c<MA50。"""
    df = df.copy()
    c, v = df["close"], df["volume"]
    ema_fast = c.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = c.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    hist = macd - signal
    vol_ma = v.rolling(VOL_MA_PERIOD).mean()
    ma10 = c.rolling(MA_TREND_PERIOD).mean()
    ma50 = c.rolling(50).mean()
    ph = hist.shift(1)
    p2 = hist.shift(2)
    vol_ok = v > vol_ma * VOL_SPIKE_MULT                 # 出来高スパイク(方向不問=長と同一)
    zero_cross_down = (hist < 0) & (ph >= 0)             # 長: (hist>0)&(ph<=0) の反転
    hist_accel_down = (hist < 0) & (hist < ph) & (ph < p2)   # 長: 上加速 の反転
    df["atr"] = _atr(df, ATR_PERIOD_MACD)
    df["entry_sig"] = (zero_cross_down | hist_accel_down) & vol_ok & (c < ma10) & (c < ma50)
    return df


def mirror_a7(df: pd.DataFrame) -> pd.DataFrame:
    """calc_a7 の忠実反転: ストキャス デッドクロス & slow_k>売られすぎ(30) & c<MA75。"""
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]
    ll = l.rolling(STOCH_K_PERIOD).min()
    hh = h.rolling(STOCH_K_PERIOD).max()
    fast_k = (c - ll) / (hh - ll).replace(0, np.nan) * 100
    slow_k = fast_k.rolling(STOCH_SMOOTH).mean()
    slow_d = slow_k.rolling(STOCH_D_PERIOD).mean()
    ma75 = c.rolling(MA_TREND_PERIOD_A7).mean()
    pk = slow_k.shift(1)
    pdd = slow_d.shift(1)
    dead_cross = (slow_k < slow_d) & (pk >= pdd)         # 長: golden の反転
    df["atr"] = _atr(df, ATR_PERIOD_A7)
    df["entry_sig"] = dead_cross & (slow_k > STOCH_OVERSOLD) & (c < ma75)  # 長: slow_k<買われすぎ の反転
    return df


def mirror_rsi2(df: pd.DataFrame) -> pd.DataFrame:
    """calc_rsi2 の忠実反転: RSI2>=90(=100-RSI2_ENTRY) & c<MA200 & IBS>0.65。"""
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    loss = (-delta).clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    rsi2 = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    ma200 = c.rolling(200).mean()
    br = h - l
    ibs = np.where(br > 0, (c - l) / br, 0.5)
    df["atr"] = _atr(df, ATR_PERIOD_RSI2)
    df["entry_sig"] = ((rsi2 >= 100.0 - RSI2_ENTRY) & (c < ma200)
                       & (pd.Series(ibs, index=c.index) > 0.65))   # 長: <=10 / >MA200 / <0.35 の反転
    return df


def mirror_don(df: pd.DataFrame) -> pd.DataFrame:
    """calc_donchian の忠実反転: c < 15日安値(前日) & c < MA50。"""
    df = df.copy()
    c, l = df["close"], df["low"]
    low15 = l.rolling(15).min().shift(1)     # 長: 15日高値ブレイク の反転
    ma50 = c.rolling(50).mean()
    df["atr"] = _atr(df, 14)
    df["entry_sig"] = (c < low15) & (c < ma50)
    return df


def mirror_voltf(df: pd.DataFrame) -> pd.DataFrame:
    """calc_vol_breakout_tf の忠実反転: c < 5日安値(前日) & 出来高>20日平均×1.5 & c < MA50。"""
    df = df.copy()
    c, l, v = df["close"], df["low"], df["volume"]
    low5 = l.rolling(5).min().shift(1)       # 長: 5日高値ブレイク の反転
    vol_ma = v.rolling(20).mean()
    ma50 = c.rolling(50).mean()
    df["atr"] = _atr(df, 14)
    df["entry_sig"] = (c < low5) & (v > vol_ma * 1.5) & (c < ma50)
    return df


def mirror_mom(df: pd.DataFrame) -> pd.DataFrame:
    """calc_momentum の忠実反転: ROC(10) < -3% & MA25 < MA75 & 出来高>20日平均×1.2。"""
    df = df.copy()
    c, v = df["close"], df["volume"]
    roc = c.pct_change(10) * 100
    ma25 = c.rolling(25).mean()
    ma75 = c.rolling(75).mean()
    vol_ma = v.rolling(20).mean()
    df["atr"] = _atr(df, 14)
    df["entry_sig"] = (roc < -3.0) & (ma25 < ma75) & (v > vol_ma * 1.2)
    return df


# ロング6戦略と同名キー(=すべてロングの鏡像)。値は忠実反転の calc 関数。
MIRROR_CALC = {
    "MACDTF": mirror_macdtf,
    "A7":     mirror_a7,
    "RSI2":   mirror_rsi2,
    "DON":    mirror_don,
    "VOLTF":  mirror_voltf,
    "MOM":    mirror_mom,
}
