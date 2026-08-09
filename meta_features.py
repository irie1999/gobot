"""
meta_features.py — メタモデル用 point-in-time 特徴量計算
=========================================================

メタモデル (P(このトレードは勝つ?) を予測する分類器) に食わせる特徴量を、
**シグナル日「まで」のデータだけ** から計算する共通モジュール。

ここが守るべき最重要ルールは「リーク (lookahead) 禁止」:
  - regime / atr_ratio / vol_ratio はすべてシグナル日の終値時点で確定する量のみ。
  - 約定後の値動き (MAE/MFE/exit/hold_days) は **特徴量にしない** (それは結果=y側)。

forward_test.py (フォワード記録) と build_meta_dataset_bt.py (バックテスト由来)
の両方がこのモジュールを使うことで、特徴量の定義を 1 箇所に集約する。
"""

from __future__ import annotations

import pandas as pd


# レジーム判定の MA 乖離しきい値 (±0.5%)
_REGIME_BAND = 0.005
# ATR 算出期間 (戦略の ATR 期間とは独立した汎用 14 日)
_ATR_PERIOD = 14
# 出来高平均の参照期間
_VOL_LOOKBACK = 20


def _locate(df: pd.DataFrame, signal_ts) -> int:
    """signal_ts 以前で最も近い行の整数 index を返す。見つからなければ -1。"""
    try:
        ts = pd.Timestamp(signal_ts)
        i = int(df.index.get_indexer([ts], method="pad")[0])
        return i
    except Exception:
        return -1


def compute_regime(n225_df: pd.DataFrame | None, signal_ts) -> str:
    """
    シグナル日の日経平均レジームを返す: "up" / "flat" / "down" / ""(不明)。

    MA25 と MA75 の乖離で判定:
      MA25 > MA75 * 1.005 → up
      MA25 < MA75 * 0.995 → down
      それ以外            → flat
    """
    if n225_df is None or len(n225_df) < 75:
        return ""
    i = _locate(n225_df, signal_ts)
    if i < 74:
        return ""
    c = n225_df["close"]
    ma25 = float(c.iloc[i - 24:i + 1].mean())
    ma75 = float(c.iloc[i - 74:i + 1].mean())
    if ma75 <= 0:
        return ""
    if ma25 > ma75 * (1 + _REGIME_BAND):
        return "up"
    if ma25 < ma75 * (1 - _REGIME_BAND):
        return "down"
    return "flat"


def compute_atr_vol(df: pd.DataFrame, signal_ts) -> tuple[float | None, float | None]:
    """
    シグナル日時点の (atr_ratio, vol_ratio) を返す。算出不能なら (None, None)。

      atr_ratio = ATR(14) / 終値        … ボラティリティ水準
      vol_ratio = 当日出来高 / 過去20日平均出来高 … 出来高スパイク
    """
    if df is None or len(df) < 30:
        return None, None
    i = _locate(df, signal_ts)
    if i < _VOL_LOOKBACK:
        return None, None

    close = float(df["close"].iloc[i])
    if close <= 0:
        return None, None

    # ── ATR(14) ── (戦略側で計算済みなら使い回す)
    atr_val: float | None = None
    if "atr" in df.columns:
        a = df["atr"].iloc[i]
        if pd.notna(a) and a > 0:
            atr_val = float(a)
    if atr_val is None:
        h, l, cp = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h - l), (h - cp).abs(), (l - cp).abs()], axis=1).max(axis=1)
        seg = tr.iloc[max(0, i - _ATR_PERIOD + 1):i + 1]
        if len(seg) >= 5:
            atr_val = float(seg.mean())

    atr_ratio = round(atr_val / close, 4) if atr_val and atr_val > 0 else None

    # ── vol_ratio ──
    vol_ratio: float | None = None
    hist = df["volume"].iloc[max(0, i - _VOL_LOOKBACK):i]
    if len(hist) >= 5:
        avg = float(hist.mean())
        if avg > 0:
            vol_ratio = round(float(df["volume"].iloc[i]) / avg, 2)

    return atr_ratio, vol_ratio


def rr_features(order_price: float, stop_price: float,
                target_price: float) -> dict:
    """
    リスクリワード構造の特徴量 (すべてシグナル時に確定):
      rr_ratio   = (target-order) / (order-stop)   R:R 比
      stop_pct   = (order-stop) / order * 100       損切り幅 %
      target_pct = (target-order) / order * 100     目標幅 %
    算出不能な項は None。
    """
    out: dict = {"rr_ratio": None, "stop_pct": None, "target_pct": None}
    try:
        risk   = order_price - stop_price
        reward = target_price - order_price
        if order_price and risk and risk > 0:
            out["rr_ratio"]   = round(reward / risk, 3)
            out["stop_pct"]   = round(risk / order_price * 100, 3)
        if order_price and reward:
            out["target_pct"] = round(reward / order_price * 100, 3)
    except Exception:
        pass
    return out
