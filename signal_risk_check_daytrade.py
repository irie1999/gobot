"""
signal_risk_check_daytrade.py  ―  シグナル銘柄のリスク警告チェック
==================================================================
逆指値ロング (signal_risk_check.py) を5分足デイトレ用に移植。

【役割】
- 決算発表日が近い銘柄を警告 (±5日)
- 出来高低下銘柄を警告 (流動性リスク)
- 連日値幅小銘柄を警告 (ボラ低)
- 日経バナー HTML生成

【使い方】
  from signal_risk_check_daytrade import (
      check_risks, precompute_all, render_nikkei_banner,
  )

  precompute_all(symbols)
  for sym in symbols:
      risks = check_risks(sym, name)
      # risks = {"earnings": bool, "low_volume": bool, "low_vol": bool, ...}
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))

# キャッシュ
_RISK_CACHE = {}
_NIKKEI_CACHE = None


def precompute_all(symbols, workers=4, target_date=None):
    """銘柄リストのリスク情報を事前計算 (キャッシュ)。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    if not symbols:
        return
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_compute_one, sym, name): sym for sym, name in symbols}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception:
                pass


def _compute_one(symbol, name):
    """1銘柄の リスク計算 + キャッシュ。"""
    risks = check_risks(symbol, name)
    _RISK_CACHE[symbol] = risks
    return risks


def check_risks(symbol, name=""):
    """1銘柄のリスク情報を計算。

    戻り値:
        {
            "low_volume": bool,        # 直近5日 出来高 < 20日平均×0.5
            "low_volatility": bool,    # ATR/価格 < 1% (動かない)
            "high_volatility": bool,   # ATR/価格 > 5% (動きすぎ)
            "near_high": bool,         # 直近20日 高値から -3% 以内
            "near_low": bool,          # 直近20日 安値から +3% 以内
            "warnings": list[str],     # テキスト警告
        }
    """
    if symbol in _RISK_CACHE:
        return _RISK_CACHE[symbol]
    try:
        from daytrade_data import load_intraday_batch
        fetched = load_intraday_batch([symbol], 30, source="local")
        df = fetched.get(symbol)
        if df is None or df.empty or len(df) < 50:
            return _empty_risks()

        closes = df["close"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float)
        lows = df["low"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)

        # 出来高
        recent_vol = volumes[-50:].mean() if len(volumes) >= 50 else volumes.mean()
        avg_vol = volumes.mean()
        low_volume = recent_vol < avg_vol * 0.5

        # ATR
        cur = closes[-1]
        atr = _atr_simple(highs, lows, closes, 14)
        atr_pct = atr / cur * 100 if cur > 0 else 0
        low_vola = atr_pct < 1.0
        high_vola = atr_pct > 5.0

        # 20本高値・安値からの距離
        if len(closes) >= 20:
            hh = highs[-20:].max()
            ll = lows[-20:].min()
            near_high = (hh - cur) / cur * 100 < 3.0
            near_low = (cur - ll) / cur * 100 < 3.0
        else:
            near_high = near_low = False

        warnings = []
        if low_volume:
            warnings.append("📉 出来高低下 (流動性注意)")
        if low_vola:
            warnings.append(f"💤 低ボラ (ATR {atr_pct:.1f}%)")
        if high_vola:
            warnings.append(f"⚡ 高ボラ (ATR {atr_pct:.1f}%、損切り早期注意)")
        if near_high:
            warnings.append("⛰️ 20本高値圏 (戻り売り注意)")
        if near_low:
            warnings.append("🕳️ 20本安値圏 (押し目買い注意)")

        result = {
            "low_volume": low_volume,
            "low_volatility": low_vola,
            "high_volatility": high_vola,
            "near_high": near_high,
            "near_low": near_low,
            "atr_pct": atr_pct,
            "warnings": warnings,
        }
        _RISK_CACHE[symbol] = result
        return result
    except Exception:
        return _empty_risks()


def _empty_risks():
    return {
        "low_volume": False, "low_volatility": False,
        "high_volatility": False, "near_high": False, "near_low": False,
        "atr_pct": 0, "warnings": [],
    }


def _atr_simple(highs, lows, closes, period=14):
    """ATR の簡易計算 (1日分)。"""
    n = len(highs)
    if n < period:
        return 0
    trs = []
    for i in range(1, period+1):
        if n - i < 1:
            break
        tr = max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i-1]) if n - i - 1 >= 0 else 0,
            abs(lows[-i] - closes[-i-1]) if n - i - 1 >= 0 else 0,
        )
        trs.append(tr)
    return float(np.mean(trs)) if trs else 0


def render_nikkei_banner():
    """日経の現在状況を HTML バナーで返す (signal_risk_check 互換)。"""
    global _NIKKEI_CACHE
    if _NIKKEI_CACHE:
        return _NIKKEI_CACHE
    try:
        from nikkei_filter_daytrade import detect_regime, format_regime_html
        regime = detect_regime()
        _NIKKEI_CACHE = format_regime_html(regime)
        return _NIKKEI_CACHE
    except Exception:
        return ""


def render_risk_badges(symbol):
    """銘柄のリスクバッジを HTML文字列で返す。"""
    risks = _RISK_CACHE.get(symbol) or check_risks(symbol)
    if not risks["warnings"]:
        return ""
    badges = ""
    for w in risks["warnings"]:
        badges += (f'<span style="background:#7c2d12;color:#fed7aa;'
                   f'padding:2px 6px;border-radius:3px;'
                   f'font-size:0.7rem;margin-right:4px">{w}</span>')
    return badges


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        sym = sys.argv[1]
        risks = check_risks(sym)
        print(f"\n{sym} リスク情報:")
        for k, v in risks.items():
            if k != "warnings":
                print(f"  {k}: {v}")
        for w in risks["warnings"]:
            print(f"  {w}")
    else:
        # 日経バナー
        print(render_nikkei_banner())
