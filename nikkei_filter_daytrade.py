"""
nikkei_filter_daytrade.py  ―  デイトレ用 相場局面別 戦略フィルタ
==================================================================
逆指値ロング (run_signals.py の _REGIME_STRATEGY_MAP) を5分足デイトレ用に移植。

【役割】
- 日経トレンド × ボラを判定
- 局面に適合した戦略のみ抽出
- WATCHLIST を動的にフィルタ

【局面マッピング】
  上昇×高ボラ : DON / VOL / MACD / MOM (順張り強)
  上昇×中ボラ : MACD / DON / VOL / MOM / A7 (バランス)
  上昇×低ボラ : MACD / MOM / A7
  横ばい×高ボラ: RSI2 / A7 (逆張り)
  横ばい×中ボラ: MACD / A7 / RSI2
  横ばい×低ボラ: MACD / A7 / MOM
  下落×高ボラ : RSI2 (押し目買い限定) / DON_S / MOM_S (空売り)
  下落×中ボラ : RSI2 / A7 / RSI2_S
  下落×低ボラ : RSI2 / A7

【使い方】
  from nikkei_filter_daytrade import detect_regime, filter_watchlist

  regime = detect_regime()
  # regime = {"trend": "up", "vol": "mid", "label": "上昇/中ボラ", ...}
  filtered = filter_watchlist(watchlist, regime)
"""

from __future__ import annotations

from datetime import timedelta, timezone
import pandas as pd

JST = timezone(timedelta(hours=9))


# 局面 → 推奨戦略マップ
REGIME_STRATEGY_MAP = {
    # (trend, vol): {strategies}
    ("up",       "high"): {"DON", "VOL", "MACD", "MOM"},
    ("up",       "mid"):  {"MACD", "DON", "VOL", "MOM", "A7"},
    ("up",       "low"):  {"MACD", "MOM", "A7"},
    ("sideways", "high"): {"RSI2", "A7"},
    ("sideways", "mid"):  {"MACD", "A7", "RSI2"},
    ("sideways", "low"):  {"MACD", "A7", "MOM"},
    ("down",     "high"): {"RSI2", "DON_S", "MOM_S", "VOL_S"},
    ("down",     "mid"):  {"RSI2", "A7", "RSI2_S", "MACD_S"},
    ("down",     "low"):  {"RSI2", "A7", "MACD_S"},
}


def detect_regime():
    """日経225 の現在の相場局面を判定。

    戻り値:
        {
            "trend": "up"/"sideways"/"down",
            "vol": "high"/"mid"/"low",
            "label": "上昇/中ボラ",
            "nikkei": 70000.0,
            "ma10": ..., "ma25": ...,
            "vol_14d": 1.50,
        }
    """
    try:
        import yfinance as yf
        df = yf.download("^N225", period="2mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 25:
            return _unknown_regime("データ取得失敗")

        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]

        vol = float(close.pct_change().tail(14).std() * 100)
        ma10 = float(close.rolling(10).mean().iloc[-1])
        ma25 = float(close.rolling(25).mean().iloc[-1])
        cur = float(close.iloc[-1])

        # トレンド判定
        if cur > ma10 and ma10 > ma25:
            trend = "up"
            trend_label = "上昇"
        elif cur < ma10 and ma10 < ma25:
            trend = "down"
            trend_label = "下落"
        else:
            trend = "sideways"
            trend_label = "横ばい"

        # ボラ判定
        if vol > 1.5:
            vol_level = "high"
            vol_label = "高ボラ"
        elif vol > 0.8:
            vol_level = "mid"
            vol_label = "中ボラ"
        else:
            vol_level = "low"
            vol_label = "低ボラ"

        allowed = REGIME_STRATEGY_MAP.get((trend, vol_level), set())

        return {
            "trend": trend,
            "vol": vol_level,
            "label": f"{trend_label}/{vol_label}",
            "nikkei": cur,
            "ma10": ma10,
            "ma25": ma25,
            "vol_14d": vol,
            "allowed_strategies": allowed,
        }
    except Exception as e:
        return _unknown_regime(str(e))


def _unknown_regime(reason):
    """局面判定失敗時のデフォルト (全戦略許可)。"""
    return {
        "trend": "unknown",
        "vol": "unknown",
        "label": f"不明 ({reason})",
        "nikkei": 0,
        "ma10": 0,
        "ma25": 0,
        "vol_14d": 0,
        "allowed_strategies": None,  # None = 全許可
    }


def filter_watchlist(watchlist, regime):
    """局面に合わない戦略の銘柄を除外。

    引数:
        watchlist: [(symbol, name, strategy), ...] or [(symbol, name), ...]
        regime: detect_regime() の戻り値

    戻り値:
        フィルタ済み watchlist
    """
    allowed = regime.get("allowed_strategies")
    if not allowed:
        return list(watchlist)
    filtered = []
    for entry in watchlist:
        if len(entry) >= 3:
            sym, name, strat = entry[:3]
            if strat in allowed:
                filtered.append(entry)
        else:
            # 戦略列なし → そのまま採用
            filtered.append(entry)
    return filtered


def format_regime_html(regime):
    """局面情報をHTMLバナーで出力。"""
    trend_color = {"up": "#4ade80", "down": "#f87171",
                   "sideways": "#fbbf24"}.get(regime["trend"], "#94a3b8")
    vol_color = {"high": "#f87171", "mid": "#fbbf24",
                 "low": "#4ade80"}.get(regime["vol"], "#94a3b8")
    allowed = regime.get("allowed_strategies")
    allowed_str = " / ".join(sorted(allowed)) if allowed else "全戦略"
    return f"""
<div style="background:#0d1424;border:1px solid #1e3a5f;border-radius:10px;
            padding:16px 20px;margin-bottom:16px">
  <div style="display:flex;gap:24px;flex-wrap:wrap;align-items:center">
    <div>
      <div style="font-size:0.72rem;color:#64748b">日経225</div>
      <div style="font-size:1.2rem;font-weight:700">{regime["nikkei"]:,.0f}</div>
    </div>
    <div>
      <div style="font-size:0.72rem;color:#64748b">トレンド</div>
      <div style="font-size:1.05rem;font-weight:700;color:{trend_color}">
        {regime["label"]}</div>
    </div>
    <div>
      <div style="font-size:0.72rem;color:#64748b">14日ボラ</div>
      <div style="font-size:1rem;color:{vol_color}">{regime["vol_14d"]:.2f}%</div>
    </div>
    <div style="flex:1">
      <div style="font-size:0.72rem;color:#64748b">推奨戦略</div>
      <div style="font-size:0.95rem;color:#60a5fa">{allowed_str}</div>
    </div>
  </div>
</div>"""


if __name__ == "__main__":
    # 動作確認用
    regime = detect_regime()
    print(f"トレンド: {regime['trend']} / {regime['label']}")
    print(f"ボラ: {regime['vol_14d']:.2f}% / {regime['vol']}")
    print(f"推奨戦略: {regime['allowed_strategies']}")
