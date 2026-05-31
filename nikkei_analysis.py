"""
nikkei_analysis.py  ―  日経平均 総合分析レポート

select_signals.py / analyze_nikkei_trend.py / analyze_trend_timing.py を1本に統合。
日経データを1回だけ取得し、タブ付きHTMLで以下3セクションを生成する。

  タブ1: シグナル判定    — 相場環境 + 今日使うべきスクリプト
  タブ2: トレンド期間    — 上昇/下落/横ばい期間の統計と一覧
  タブ3: エントリー分析  — 上昇何日目に入ると良いか / 生存確率

Usage:
    python nikkei_analysis.py                       # 過去5年 HTML生成 & ブラウザ表示
    python nikkei_analysis.py --years 10            # 過去10年
    python nikkei_analysis.py --date 2024-01-15     # 指定日時点の分析
    python nikkei_analysis.py --no-browser          # HTML生成のみ
"""
from __future__ import annotations
import argparse
import webbrowser
from datetime import timedelta, timezone, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

JST    = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()

# ═══════════════════════════════════════════════════════════════════════════════
# データ取得 & トレンド判定
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_n225(years: int, end_date=None) -> pd.Series:
    """日経225の日足終値を取得。end_date 指定時はその日までのデータを返す。"""
    if end_date is not None:
        start = pd.Timestamp(end_date) - pd.Timedelta(days=years * 365 + 60)
        end   = pd.Timestamp(end_date) + pd.Timedelta(days=1)
        df = yf.download("^N225", start=start, end=end, interval="1d",
                         progress=False, auto_adjust=True)
    else:
        df = yf.download("^N225", period=f"{years * 365 + 60}d", interval="1d",
                         progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError("日経データ取得失敗")
    close = df["Close"].squeeze()
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.dropna().sort_index()


def label_trend(close: pd.Series) -> pd.Series:
    """MA10/MA25 クロスでトレンドラベル付け: 'up' / 'down' / 'sideways'"""
    ma10 = close.rolling(10).mean()
    ma25 = close.rolling(25).mean()
    trend = pd.Series("sideways", index=close.index)
    trend[(close > ma10) & (ma10 > ma25)] = "up"
    trend[(close < ma10) & (ma10 < ma25)] = "down"
    return trend


MARKET_DEFS = [
    {"ticker": "1306.T", "label": "TOPIX",           "unit": "pt",  "fmt": ",.1f",
     "note": "東証全体の動き。日経と同方向なら信頼性↑"},
    {"ticker": "JPY=X",  "label": "USD/JPY",          "unit": "円", "fmt": ".2f",
     "note": ">150円: 円安 → 輸出株↑, <140円: 円高 → 輸出株↓"},
    {"ticker": "^GSPC",  "label": "S&P500",           "unit": "pt",  "fmt": ",.0f",
     "note": "米株上昇 → 翌日の日本株に追い風"},
    {"ticker": "^VIX",   "label": "VIX (恐怖指数)",   "unit": "",    "fmt": ".1f",
     "note": "<20: 平静, 20-30: 警戒, >30: 恐怖 → 逆指値損切り多発"},
    {"ticker": "^TNX",   "label": "米10年国債",        "unit": "%",   "fmt": ".2f",
     "note": "急上昇: 株→債券への資金移動リスク"},
]


def fetch_market_indicators(years: int = 1, end_date=None) -> dict:
    """各市場指標の日足終値を取得。ticker→pd.Series の辞書を返す。失敗した指標はスキップ。"""
    result = {}
    period_days = years * 365 + 60
    tickers = [d["ticker"] for d in MARKET_DEFS]
    try:
        if end_date is not None:
            start = pd.Timestamp(end_date) - pd.Timedelta(days=period_days)
            end   = pd.Timestamp(end_date) + pd.Timedelta(days=1)
            raw = yf.download(tickers, start=start, end=end, interval="1d",
                              progress=False, auto_adjust=True, group_by="ticker")
        else:
            raw = yf.download(tickers, period=f"{period_days}d", interval="1d",
                              progress=False, auto_adjust=True, group_by="ticker")
    except Exception:
        return result

    for ticker in tickers:
        try:
            if ticker in raw.columns.get_level_values(0):
                s = raw[ticker]["Close"]
            elif "Close" in raw.columns:
                s = raw["Close"]
            else:
                continue
            s = s.squeeze()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            s = s.dropna().sort_index()
            if end_date is not None:
                s = s[s.index <= pd.Timestamp(end_date)]
            if not s.empty:
                result[ticker] = s
        except Exception:
            pass
    return result


def get_indicator_regime(series: pd.Series) -> dict:
    """指標の現在状態（トレンド・騰落）を計算"""
    if len(series) < 2:
        return {}
    cur = float(series.iloc[-1])
    ma10 = float(series.rolling(10).mean().iloc[-1]) if len(series) >= 10 else cur
    ma25 = float(series.rolling(25).mean().iloc[-1]) if len(series) >= 25 else cur
    if cur > ma10 and ma10 > ma25:
        trend = "up"
    elif cur < ma10 and ma10 < ma25:
        trend = "down"
    else:
        trend = "sideways"
    mom5  = (cur / float(series.iloc[-6])  - 1) * 100 if len(series) >= 6  else 0.0
    mom20 = (cur / float(series.iloc[-21]) - 1) * 100 if len(series) >= 21 else 0.0
    return {"cur": cur, "trend": trend, "mom5": mom5, "mom20": mom20}


def get_regime(close: pd.Series) -> dict:
    """現在の相場環境を計算して返す"""
    rets    = close.pct_change().dropna()
    cur     = float(close.iloc[-1])
    ma10    = float(close.rolling(10).mean().iloc[-1])
    ma25    = float(close.rolling(25).mean().iloc[-1])
    ma200   = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    vol14   = float(rets.tail(14).std() * 100)
    mom5    = (cur / float(close.iloc[-6])  - 1) * 100
    mom20   = (cur / float(close.iloc[-21]) - 1) * 100
    max_1d_drop = float(rets.tail(30).min() * 100)

    if cur > ma10 and ma10 > ma25:
        trend = "up"
    elif cur < ma10 and ma10 < ma25:
        trend = "down"
    else:
        trend = "sideways"

    vol_level   = "high" if vol14 > 1.5 else ("mid" if vol14 > 0.8 else "low")
    above_ma200 = (cur >= ma200) if ma200 else True

    return {
        "cur": cur, "ma10": ma10, "ma25": ma25, "ma200": ma200,
        "vol": vol14, "vol_level": vol_level, "trend": trend,
        "mom5": mom5, "mom20": mom20,
        "above_ma200": above_ma200, "max_1d_drop": max_1d_drop,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# シグナル判定ルール (select_signals.py 相当)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_winners_files() -> list[dict]:
    """レジーム別 winners ファイルをロード (SCRIPTS互換形式)。

    winners_by_regime.py で生成された3ファイル + 汎用版を読み込み。
    各エントリは {cmd, label, regime, symbols, count, risk, note, sublabel}。
    """
    import importlib.util
    base = Path(__file__).resolve().parent
    file_info = [
        ("daytrade_donchian_winners_bull.py",     "Bull (上昇相場用)",     "up",       "中"),
        ("daytrade_donchian_winners_sideways.py", "Sideways (横ばい相場用)", "sideways", "低中"),
        ("daytrade_donchian_winners_bear.py",     "Bear (下落相場用)",     "down",     "中高"),
        ("daytrade_donchian_winners.py",          "Default (汎用・現用)",  "any",      "中"),
    ]
    items = []
    for fname, label, regime, risk in file_info:
        path = base / fname
        if not path.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(fname[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            syms = getattr(mod, "SYMBOLS", [])
        except Exception:
            continue
        sample = ", ".join(n for _, n in syms[:5])
        if len(syms) > 5:
            sample += f"... 他{len(syms)-5}"
        items.append({
            "cmd": f"# 監視: {fname}  （bot に SYMBOLS を読み込ませる）",
            "label": label,
            "regime": regime,
            "symbols": syms,
            "count": len(syms),
            "sublabel": f"{len(syms)}銘柄",
            "risk": risk,
            "note": f"対象銘柄(先頭): {sample}",
        })
    return items


SCRIPTS = _load_winners_files()


def judge(script: dict, r: dict) -> tuple[str, str, str]:
    """(status, reason, advice)  status= ✅推奨 / ⚠️注意 / ❌停止

    レジーム別 winners 用の判定:
      - winners の regime と現在のトレンドが一致 → ✅推奨
      - 'any' (汎用) → 常に⚠️注意 (専用版を使う方が良い)
      - 一致しない → ❌停止
    """
    regime = script.get("regime", "any")
    trend  = r["trend"]
    vol    = r["vol_level"]
    mom5   = r["mom5"]
    above  = r["above_ma200"]
    drop   = r["max_1d_drop"]
    crash  = drop < -3.0

    # 全レジーム共通: 急落リスクで全部 ❌
    if crash and not above:
        return "❌ 停止", f"日経<MA200 + 過去30日最大{drop:+.1f}%急落", "相場安定まで取引停止"

    # 汎用 (any) は専用版未生成時のフォールバック
    if regime == "any":
        if trend == "down" and vol == "high":
            return "⚠️ 注意", "下落×高ボラ", "Bear専用 winners 生成 (winners_by_regime.py) 推奨"
        return "⚠️ 注意", "全相場対応 (汎用)", "レジーム別winners生成で精度UP"

    # レジームマッチング
    if regime == trend:
        return "✅ 推奨", f"トレンド={trend} と一致", "このwinnersをbotに設定"

    # 不一致
    if regime == "up" and trend == "sideways":
        return "⚠️ 注意", "上昇用winnersを横ばい相場で使用", "シグナル少なめ・慎重に"
    if regime == "sideways" and trend == "up":
        return "⚠️ 注意", "横ばい用winnersを上昇相場で使用", "Bull版を優先"
    if regime == "bear" and trend != "down":
        return "❌ 停止", f"下落用winnersを{trend}相場で使用", "別winnersに切替"
    if regime == "up" and trend == "down":
        return "❌ 停止", "上昇用winnersを下落相場で使用", "Bear版に切替 or 取引停止"

    return "⚠️ 注意", f"regime={regime} ≠ trend={trend}", "適合winnersを使用"


# ═══════════════════════════════════════════════════════════════════════════════
# トレンド期間抽出 (analyze_nikkei_trend.py 相当)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_periods(close: pd.Series, trend: pd.Series, ref_date) -> list[dict]:
    periods = []
    cur_trend = None
    start_idx = None

    def _make(cur_trend, start_idx, end_idx, is_current=False):
        sp = float(close.iloc[start_idx])
        ep = float(close.iloc[end_idx])
        sd = close.index[start_idx].date()
        ed = ref_date if is_current else close.index[end_idx].date()
        seg = close.iloc[start_idx:end_idx + 1]
        return {
            "trend": cur_trend, "start": sd, "end": ed,
            "days": (ed - sd).days,
            "pct": (ep / sp - 1) * 100,
            "start_price": sp, "end_price": ep,
            "min_price": float(seg.min()), "max_price": float(seg.max()),
            "max_drop": (float(seg.min()) / sp - 1) * 100,
            "is_current": is_current,
        }

    for i in range(len(trend)):
        t = trend.iloc[i]
        if t != cur_trend:
            if cur_trend is not None:
                periods.append(_make(cur_trend, start_idx, i - 1))
            cur_trend = t
            start_idx = i

    if cur_trend is not None and start_idx is not None:
        periods.append(_make(cur_trend, start_idx, len(trend) - 1, is_current=True))

    return periods


def calc_stats(periods: list[dict]) -> dict:
    if not periods:
        return {}
    days = [p["days"] for p in periods]
    pcts = [p["pct"]  for p in periods]
    return {
        "count": len(periods),
        "avg_days": sum(days) / len(days),
        "med_days": sorted(days)[len(days) // 2],
        "max_days": max(days), "min_days": min(days),
        "avg_pct": sum(pcts) / len(pcts),
        "days_list": days,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# エントリータイミング分析 (analyze_trend_timing.py 相当)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_up_periods(close: pd.Series, trend: pd.Series, ref_date) -> list[dict]:
    periods = []
    cur_trend = None
    start_idx = None
    n = len(trend)
    for i in range(n):
        t = trend.iloc[i]
        if t != cur_trend:
            if cur_trend == "up" and start_idx is not None:
                _append_up(close, start_idx, i - 1, periods, ref_date=ref_date)
            cur_trend = t
            start_idx = i
    if cur_trend == "up" and start_idx is not None:
        _append_up(close, start_idx, n - 1, periods, is_current=True, ref_date=ref_date)
    return periods


def _append_up(close, start_idx, end_idx, periods, is_current=False, ref_date=None):
    sp = float(close.iloc[start_idx])
    ep = float(close.iloc[end_idx])
    sd = close.index[start_idx].date()
    ed = (ref_date if ref_date else _TODAY) if is_current else close.index[end_idx].date()

    look_back    = max(0, start_idx - 30)
    pre_seg      = close.iloc[look_back:start_idx + 1]
    tl_idx       = int(pre_seg.values.argmin())
    true_low_p   = float(pre_seg.iloc[tl_idx])
    lag_bars     = start_idx - (look_back + tl_idx)
    lag_pct      = (sp / true_low_p - 1) * 100

    daily_rets = {}
    for n_days in [1, 3, 5, 10, 15, 20, 30]:
        idx = start_idx + n_days
        daily_rets[n_days] = (float(close.iloc[idx]) / sp - 1) * 100 if idx <= end_idx else None

    periods.append({
        "start_date": sd, "end_date": ed,
        "start_p": sp, "end_p": ep,
        "days": (ed - sd).days,
        "total_pct": (ep / sp - 1) * 100,
        "true_low_p": true_low_p,
        "lag_bars": lag_bars, "lag_pct": lag_pct,
        "daily_rets": daily_rets,
        "is_current": is_current,
        "n_bars": end_idx - start_idx + 1,
    })


def survival_curve(periods: list[dict]) -> dict[int, float]:
    done = [p for p in periods if not p["is_current"]]
    if not done:
        return {}
    return {n: sum(1 for p in done if p["n_bars"] > n) / len(done) * 100
            for n in range(1, 51)}


def entry_stats(periods: list[dict]) -> dict[int, dict]:
    done = [p for p in periods if not p["is_current"]]
    result = {}
    for n in [1, 3, 5, 10, 15, 20, 30]:
        valid = [p for p in done if p["daily_rets"].get(n) is not None]
        if not valid:
            continue
        rets = [(p["end_p"] / (p["start_p"] * (1 + p["daily_rets"][n] / 100)) - 1) * 100
                for p in valid]
        result[n] = {
            "count": len(rets),
            "avg_ret": sum(rets) / len(rets),
            "win_rate": sum(1 for r in rets if r > 0) / len(rets) * 100,
            "med_ret": sorted(rets)[len(rets) // 2],
        }
    return result


def downtrend_risk(periods: list[dict]) -> dict[int, float]:
    done = [p for p in periods if not p["is_current"]]
    risk = {}
    for n in [1, 3, 5, 10, 15, 20, 30]:
        total = sum(1 for p in done if p["n_bars"] > n)
        fell  = sum(1 for p in done if p["n_bars"] > n and p["n_bars"] <= n + 5)
        if total > 0:
            risk[n] = fell / total * 100
    return risk


# ═══════════════════════════════════════════════════════════════════════════════
# トレンド継続予測 (条件付き生存分析)
# ═══════════════════════════════════════════════════════════════════════════════

def calc_trend_prediction(periods: list[dict], current_trend: str, current_days: int) -> dict:
    """
    条件付き生存分析: すでに current_days 日続いているトレンドが
    あと何日続くかを過去データから推計する。
    """
    done = [p for p in periods if p["trend"] == current_trend and not p["is_current"]]
    survived = [p for p in done if p["days"] >= current_days]

    if len(survived) < 3:
        return {"insufficient": True, "total_count": len(done),
                "survived_count": len(survived), "current_days": current_days}

    remaining = sorted(p["days"] - current_days for p in survived)
    thresholds = [3, 5, 10, 15, 20, 30]
    probs = {t: sum(1 for r in remaining if r >= t) / len(remaining) * 100
             for t in thresholds}

    return {
        "insufficient": False,
        "total_count": len(done),
        "survived_count": len(survived),
        "remaining": remaining,
        "mean_remaining": sum(remaining) / len(remaining),
        "median_remaining": remaining[len(remaining) // 2],
        "max_remaining": max(remaining),
        "probs": probs,
        "current_days": current_days,
    }


def _trend_prediction_html(pred: dict, current_trend: str) -> str:
    """トレンド継続予測ボックス HTML"""
    trend_ja   = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[current_trend]
    trend_icon = {"up": "📈", "down": "📉", "sideways": "➡️"}[current_trend]
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[current_trend]
    cd = pred["current_days"]

    if pred["insufficient"]:
        n = pred["survived_count"]
        total = pred["total_count"]
        return f"""
<div style="background:#0d1424;border:1px solid #1e3a5f;border-radius:10px;
            padding:16px 20px;margin-bottom:16px">
  <div style="font-size:0.95rem;font-weight:700;color:#60a5fa;margin-bottom:8px">
    {trend_icon} {trend_ja}トレンド継続予測 — 現在 {cd}日目
  </div>
  <div style="color:#64748b;font-size:0.85rem">
    過去に{cd}日以上続いた{trend_ja}トレンドは {total}回中 {n}回のみ。
    サンプル不足のため統計的な予測が困難です。<br>
    現在のトレンドは過去データの中では稀なほど長続きしています。転換に注意してください。
  </div>
</div>"""

    survived_pct = pred["survived_count"] / pred["total_count"] * 100
    med = pred["median_remaining"]
    avg = pred["mean_remaining"]
    mx  = pred["max_remaining"]
    probs = pred["probs"]

    # 確率バー
    bar_rows = ""
    for days, prob in probs.items():
        bar_color = "#4ade80" if prob >= 60 else ("#fbbf24" if prob >= 30 else "#f87171")
        bar_w = max(2, round(prob))
        bar_rows += f"""
<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
  <span style="width:80px;font-size:0.78rem;color:#94a3b8;text-align:right;flex-shrink:0">あと{days}日以上</span>
  <div style="flex:1;background:#1e293b;border-radius:4px;height:18px;position:relative">
    <div style="width:{bar_w}%;background:{bar_color};height:100%;border-radius:4px;
                transition:width 0.3s"></div>
    <span style="position:absolute;left:8px;top:50%;transform:translateY(-50%);
                 font-size:0.75rem;font-weight:700;color:#0f172a">{prob:.0f}%</span>
  </div>
</div>"""

    # 分布ヒストグラム (10日ごとのバケット)
    buckets: dict[int, int] = {}
    for r in pred["remaining"]:
        b = (r // 10) * 10
        buckets[b] = buckets.get(b, 0) + 1
    hist_max = max(buckets.values()) if buckets else 1
    hist_rows = ""
    for b in sorted(buckets):
        cnt  = buckets[b]
        w    = round(cnt / hist_max * 100)
        hist_rows += (f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                      f'<span style="width:60px;font-size:0.72rem;color:#64748b;text-align:right'
                      f';flex-shrink:0">{b}〜{b+9}日</span>'
                      f'<div style="width:{w}%;background:#334155;height:14px;border-radius:3px'
                      f';min-width:2px"></div>'
                      f'<span style="font-size:0.72rem;color:#475569">{cnt}回</span></div>')

    return f"""
<div style="background:#0d1424;border:1px solid #1e3a5f;border-radius:10px;
            padding:16px 20px;margin-bottom:16px">
  <div style="font-weight:700;font-size:0.98rem;color:#60a5fa;margin-bottom:12px">
    {trend_icon} {trend_ja}トレンド継続予測 — 現在
    <span style="color:{trend_color};font-size:1.1rem">{cd}日目</span>
  </div>

  <div style="display:flex;flex-wrap:wrap;gap:16px;margin-bottom:14px">
    <div style="background:#111827;border-radius:8px;padding:10px 16px;text-align:center">
      <div style="font-size:0.7rem;color:#64748b;margin-bottom:3px">過去の同トレンド</div>
      <div style="font-size:1.1rem;font-weight:700">{pred['total_count']}回</div>
    </div>
    <div style="background:#111827;border-radius:8px;padding:10px 16px;text-align:center">
      <div style="font-size:0.7rem;color:#64748b;margin-bottom:3px">{cd}日以上続いた</div>
      <div style="font-size:1.1rem;font-weight:700">
        {pred['survived_count']}回
        <span style="font-size:0.78rem;color:#64748b">({survived_pct:.0f}%)</span>
      </div>
    </div>
    <div style="background:#111827;border-radius:8px;padding:10px 16px;text-align:center">
      <div style="font-size:0.7rem;color:#64748b;margin-bottom:3px">残り中央値</div>
      <div style="font-size:1.1rem;font-weight:700;color:{trend_color}">{med:.0f}日</div>
    </div>
    <div style="background:#111827;border-radius:8px;padding:10px 16px;text-align:center">
      <div style="font-size:0.7rem;color:#64748b;margin-bottom:3px">残り平均</div>
      <div style="font-size:1.1rem;font-weight:700">{avg:.0f}日</div>
    </div>
    <div style="background:#111827;border-radius:8px;padding:10px 16px;text-align:center">
      <div style="font-size:0.7rem;color:#64748b;margin-bottom:3px">過去最長残り</div>
      <div style="font-size:1.1rem;font-weight:700">{mx:.0f}日</div>
    </div>
  </div>

  <div style="display:flex;flex-wrap:wrap;gap:24px">
    <div style="flex:1;min-width:220px">
      <div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px;font-weight:600">
        ▶ 継続確率（条件付き）
      </div>
      {bar_rows}
    </div>
    <div style="flex:1;min-width:200px">
      <div style="font-size:0.78rem;color:#94a3b8;margin-bottom:8px;font-weight:600">
        ▶ 残り日数の分布
      </div>
      {hist_rows}
    </div>
  </div>

  <div style="font-size:0.72rem;color:#334155;margin-top:12px;line-height:1.6">
    ※ 過去{pred['total_count']}回の{trend_ja}トレンドのうち、{cd}日以上続いた
    {pred['survived_count']}回を対象に集計。確率はあくまで過去の傾向であり、
    将来を保証するものではありません。
  </div>
</div>"""


# ═══════════════════════════════════════════════════════════════════════════════
# HTML パーツ生成
# ═══════════════════════════════════════════════════════════════════════════════

STATUS_META = {
    "✅ 推奨": ("推奨", "#4ade80", "#052e16", "#166534"),
    "⚠️ 注意": ("注意", "#fbbf24", "#2d1f00", "#92400e"),
    "❌ 停止": ("停止", "#f87171", "#2d0a0a", "#991b1b"),
}
RISK_COLOR = {"高": "#f87171", "中高": "#fb923c", "中": "#fbbf24", "低中": "#86efac", "低": "#4ade80"}


def _priority_score(cmd: str, r: dict, status: str, regime: str = "any") -> int:
    """おすすめスコア 0〜100 (レジーム適合度ベース)"""
    if status == "❌ 停止":
        return 0
    base = 30 if status == "⚠️ 注意" else 65

    trend = r["trend"]
    mom5  = r["mom5"]

    # レジーム完全一致 → 最高評価
    if regime == trend:
        base += 25
        # さらに強いトレンドならボーナス
        if regime == "up" and mom5 >= 2.0 and r["above_ma200"]:
            base += 10
        if regime == "down" and mom5 <= -2.0:
            base += 10
    # 汎用 (any) は使えるが専用版が優先
    elif regime == "any":
        base -= 10

    return min(100, max(0, base))


def _priority_reason(cmd: str, r: dict, score: int, regime: str = "any") -> str:
    """おすすめ度の短い理由"""
    trend = r["trend"]
    label = {"up": "Bull (上昇)", "down": "Bear (下落)",
             "sideways": "Sideways (横ばい)", "any": "汎用"}.get(regime, regime)
    if regime == trend:
        return f"{label}winners × トレンド{trend} 一致 — 最適"
    if regime == "any":
        return "汎用版 — 専用winners生成で精度UP可"
    return f"{label}winners × トレンド{trend} 不一致"


def _stars_html(score: int, rank: int | None) -> str:
    """★バー + 順位バッジを返す"""
    if score == 0:
        return '<span style="color:#334155;font-size:0.8rem">— 停止中</span>'
    filled = round(score / 20)          # 0-100 → 0-5 stars
    filled = max(1, min(5, filled))
    stars  = '★' * filled + '<span style="color:#1e293b">★</span>' * (5 - filled)
    star_color = "#fbbf24" if score >= 70 else ("#94a3b8" if score >= 45 else "#475569")

    rank_html = ""
    if rank == 1:
        rank_html = '<span style="background:#b45309;color:#fef3c7;padding:1px 8px;border-radius:4px;font-size:0.72rem;font-weight:700">1位</span>'
    elif rank == 2:
        rank_html = '<span style="background:#475569;color:#e2e8f0;padding:1px 8px;border-radius:4px;font-size:0.72rem;font-weight:700">2位</span>'
    elif rank == 3:
        rank_html = '<span style="background:#7c2d12;color:#fed7aa;padding:1px 8px;border-radius:4px;font-size:0.72rem;font-weight:700">3位</span>'
    elif rank is not None:
        rank_html = f'<span style="color:#475569;font-size:0.72rem">{rank}位</span>'

    return f'<span style="color:{star_color};font-size:1.05rem;letter-spacing:1px">{stars}</span> {rank_html}'


def _market_overview_html(indicators: dict) -> str:
    """マーケット概況グリッド HTML (各市場指標カード)"""
    if not indicators:
        return ""

    trend_arrow = {"up": "▲", "down": "▼", "sideways": "→"}
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}

    cards = []
    for mdef in MARKET_DEFS:
        series = indicators.get(mdef["ticker"])
        if series is None or series.empty:
            cards.append(f"""
<div class="mkt-card">
  <div class="mkt-label">{mdef['label']}</div>
  <div class="mkt-val" style="color:#475569">取得失敗</div>
</div>""")
            continue

        reg   = get_indicator_regime(series)
        cur   = reg["cur"]
        tc    = trend_color[reg["trend"]]
        arr   = trend_arrow[reg["trend"]]
        m5c   = "#4ade80" if reg["mom5"]  >= 0 else "#f87171"
        m20c  = "#4ade80" if reg["mom20"] >= 0 else "#f87171"
        fmt   = mdef["fmt"]
        unit  = mdef["unit"]
        val_str = f"{cur:{fmt}}{unit}"

        # VIX 特別表示
        vix_badge = ""
        if mdef["ticker"] == "^VIX":
            if cur >= 30:
                vix_badge = '<span style="background:#991b1b;color:#fca5a5;padding:1px 7px;border-radius:4px;font-size:0.7rem;font-weight:700;margin-left:6px">恐怖</span>'
            elif cur >= 20:
                vix_badge = '<span style="background:#92400e;color:#fde68a;padding:1px 7px;border-radius:4px;font-size:0.7rem;font-weight:700;margin-left:6px">警戒</span>'
            else:
                vix_badge = '<span style="background:#14532d;color:#86efac;padding:1px 7px;border-radius:4px;font-size:0.7rem;font-weight:700;margin-left:6px">平静</span>'

        # USD/JPY 特別表示
        usdjpy_badge = ""
        if mdef["ticker"] == "JPY=X":
            if cur >= 150:
                usdjpy_badge = '<span style="background:#1e3a5f;color:#93c5fd;padding:1px 7px;border-radius:4px;font-size:0.7rem;margin-left:6px">円安</span>'
            elif cur < 140:
                usdjpy_badge = '<span style="background:#164e63;color:#a5f3fc;padding:1px 7px;border-radius:4px;font-size:0.7rem;margin-left:6px">円高</span>'

        cards.append(f"""
<div class="mkt-card">
  <div class="mkt-label">{mdef['label']}</div>
  <div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap">
    <span class="mkt-val">{val_str}</span>
    <span style="color:{tc};font-size:0.95rem;font-weight:700">{arr}</span>
    {vix_badge}{usdjpy_badge}
  </div>
  <div class="mkt-chg">
    <span style="color:{m5c}">5日: {reg['mom5']:+.1f}%</span>
    &nbsp;/&nbsp;
    <span style="color:{m20c}">20日: {reg['mom20']:+.1f}%</span>
  </div>
  <div class="mkt-note">{mdef['note']}</div>
</div>""")

    return f"""
<h2>マーケット概況（参考指標）</h2>
<div class="mkt-grid">{''.join(cards)}</div>"""


def _tab1_signal_html(r: dict, ref_date, indicators: dict | None = None,
                      periods: list | None = None) -> str:
    """タブ1: シグナル判定"""
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[r["trend"]]
    trend_ja    = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}[r["trend"]]
    vol_color   = {"high": "#f87171", "mid": "#fbbf24", "low": "#4ade80"}[r["vol_level"]]
    vol_ja      = {"high": "高ボラ", "mid": "中ボラ", "low": "低ボラ"}[r["vol_level"]]
    ma200_str   = f"{r['ma200']:,.0f}" if r.get("ma200") else "N/A"
    ma200_color = "#4ade80" if r["above_ma200"] else "#f87171"
    ma200_pos   = f'<span style="color:{ma200_color}">{"▲ 上" if r["above_ma200"] else "▼ 下"}</span>'
    mom5_c  = "#4ade80" if r["mom5"]  >= 0 else "#f87171"
    mom20_c = "#4ade80" if r["mom20"] >= 0 else "#f87171"
    drop_c  = "#f87171" if r["max_1d_drop"] < -3 else "#94a3b8"

    regime_items = [
        ("日経225",     f'<strong style="font-size:1.25rem">{r["cur"]:,.0f}円</strong>'),
        ("トレンド",    f'<span style="color:{trend_color};font-weight:700;font-size:1.05rem">{trend_ja}</span>'),
        ("ボラ (14日)", f'<span style="color:{vol_color}">{vol_ja} ({r["vol"]:.2f}%)</span>'),
        ("5日騰落",     f'<span style="color:{mom5_c};font-weight:600">{r["mom5"]:+.2f}%</span>'),
        ("20日騰落",    f'<span style="color:{mom20_c};font-weight:600">{r["mom20"]:+.2f}%</span>'),
        ("MA200",       f'{ma200_str}円 → {ma200_pos}'),
        ("過去30日最大下落", f'<span style="color:{drop_c}">{r["max_1d_drop"]:+.2f}%</span>'),
    ]
    regime_html = "".join(
        f'<div class="regime-item"><span class="ri-label">{lbl}</span>'
        f'<span class="ri-val">{val}</span></div>'
        for lbl, val in regime_items
    )

    # リスク警告
    warn_html = ""
    risks = []
    if r["max_1d_drop"] < -3.0:
        risks.append(f"過去30日に <strong>{r['max_1d_drop']:+.1f}%</strong> の急落あり → 複数ポジションの同時損切りリスク")
    if not r["above_ma200"]:
        risks.append("日経 &lt; MA200 → 長期下落トレンド。逆指値が連続損切りするリスク大")
    if risks:
        li = "".join(f"<li>{rk}</li>" for rk in risks)
        warn_html = f"""
<div class="warn-box">
  <div style="font-weight:700;margin-bottom:8px">⚠️ 株価制限なし 大損リスク要因</div>
  <ul style="padding-left:1.4em;line-height:1.9">{li}</ul>
  <div style="margin-top:8px;color:#94a3b8;font-size:0.82rem">
    損失目安: ATR×1.5×100株/銘柄 &nbsp;例) 5,000円株・ATR200円 → −30,000円/銘柄
  </div>
</div>"""

    # スクリプトカード — スコア事前計算 → ランク付け → 描画
    judged = [(s, *judge(s, r)) for s in SCRIPTS]            # (s, status, reason, advice)
    scored = [(s, st, rs, adv, _priority_score(s["cmd"], r, st, s.get("regime", "any")))
              for s, st, rs, adv in judged]                   # +score

    # 推奨の中だけでランク付け
    rec_scores = sorted(
        [(i, sc[4]) for i, sc in enumerate(scored) if sc[1] == "✅ 推奨"],
        key=lambda x: -x[1]
    )
    rank_map = {idx: rank + 1 for rank, (idx, _) in enumerate(rec_scores)}

    recommended = []
    cards_html = ""
    for i, (s, status, reason, advice, score) in enumerate(scored):
        lbl_ja, fg, bg, border = STATUS_META[status]
        rc       = RISK_COLOR.get(s["risk"], "#94a3b8")
        rank     = rank_map.get(i)
        stars    = _stars_html(score, rank)
        p_reason = _priority_reason(s["cmd"], r, score, s.get("regime", "any"))
        adv_html = (f'<div style="color:#94a3b8;font-size:0.8rem;margin-top:4px">→ {advice}</div>'
                    if advice else "")
        p_reason_html = (f'<span style="color:#94a3b8;font-size:0.78rem;margin-left:8px">{p_reason}</span>'
                         if p_reason else "")
        cards_html += f"""
<div class="script-card" style="border-color:{border};background:{bg}">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span class="badge" style="background:{border};color:{fg}">{lbl_ja}</span>
    <span style="font-weight:700;font-size:1.05rem">{s['label']}</span>
    <span style="color:#64748b;font-size:0.8rem">{s['sublabel']}</span>
    <span style="margin-left:auto;font-size:0.78rem;color:{rc}">リスク: {s['risk']}</span>
  </div>
  <code class="cmd-box">{s['cmd']}</code>
  <div style="display:flex;align-items:center;gap:6px;margin-top:8px">
    <span style="font-size:0.72rem;color:#64748b;white-space:nowrap">おすすめ度</span>
    {stars}{p_reason_html}
  </div>
  <div style="color:#94a3b8;font-size:0.82rem;margin-top:6px">{reason}</div>
  {adv_html}
  <div style="color:#64748b;font-size:0.78rem;margin-top:4px">{s['note']}</div>
</div>"""
        if status == "✅ 推奨":
            recommended.append(s["cmd"])

    # 推奨winners (今日使うべき銘柄リスト)
    if recommended:
        rec_html = '<div style="background:#052e16;border:1px solid #166534;border-radius:8px;padding:16px;color:#86efac">'
        for s in SCRIPTS:
            if judge(s, r)[0] != "✅ 推奨":
                continue
            sym_list = ", ".join(f"{n}({c.replace('.T','')})" for c, n in s["symbols"][:10])
            if len(s["symbols"]) > 10:
                sym_list += f" ... 他{len(s['symbols'])-10}銘柄"
            rec_html += f'''
<div style="margin:8px 0">
  <div style="font-weight:700;color:#4ade80">{s["label"]} ({s["count"]}銘柄)</div>
  <div style="font-size:0.82rem;color:#86efac;margin-top:4px">{sym_list}</div>
</div>'''
        rec_html += '</div>'
    elif not SCRIPTS:
        rec_html = '<div class="warn-box" style="border-color:#991b1b">⚠ winners ファイルが見つかりません。<br>winners_by_regime.py を実行してレジーム別 winners を生成してください:<br><code class="cmd-box" style="display:inline-block;margin-top:8px">python winners_by_regime.py</code></div>'
    else:
        rec_html = '<div class="warn-box" style="border-color:#991b1b">❌ 全winners停止推奨。相場が回復するまで様子見を。</div>'

    nolimit_block = ""

    mkt_html = _market_overview_html(indicators or {})

    # トレンド継続予測
    pred_html = ""
    if periods:
        cur_p   = periods[-1]
        pred    = calc_trend_prediction(periods, cur_p["trend"], cur_p["days"])
        pred_html = _trend_prediction_html(pred, cur_p["trend"])

    return f"""
<h2>{ref_date} 時点の相場環境（日経225）</h2>
<div class="regime-panel">{regime_html}</div>
{warn_html}
{pred_html}
{mkt_html}

<h2>レジーム別 Donchian winners 判定</h2>
{cards_html}

<h2>{ref_date} 時点の推奨銘柄</h2>
{rec_html}
{nolimit_block}

<p class="footnote">
  ※ 判定ルールは過去バックテスト実績から導出。株価制限なし条件: 5日≥+2% / 20日≥+3% / 上昇 / 日経&gt;MA200
</p>"""


def _tab2_trend_html(close: pd.Series, trend: pd.Series, periods: list[dict], years: int) -> str:
    """タブ2: トレンド期間分析"""
    up_p   = [p for p in periods if p["trend"] == "up"]
    down_p = [p for p in periods if p["trend"] == "down"]
    su = calc_stats(up_p)
    sd = calc_stats(down_p)
    cur_trend   = trend.iloc[-1]
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[cur_trend]
    trend_ja    = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}[cur_trend]
    cur_price   = float(close.iloc[-1])

    # 現在トレンドボックス
    last = periods[-1]
    ref_s = su if last["trend"] == "up" else sd
    med = ref_s.get("med_days", 0)
    avg = ref_s.get("avg_days", 0)
    remaining = med - last["days"]
    remain_str = (f"中央値まであと <strong>{remaining}日</strong>（参考値）"
                  if remaining > 0
                  else f"中央値({med}日)を超過中 → 転換注意")
    pct_c   = "#4ade80" if last["pct"] >= 0 else "#f87171"
    last_ja = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[last["trend"]]
    box_border = "#166534" if last["trend"] == "up" else "#991b1b"
    current_box = f"""
<div class="current-box" style="border-color:{box_border}">
  <div style="font-size:1.1rem;font-weight:700;margin-bottom:10px;color:{trend_color}">
    現在: {last_ja}トレンド継続中
  </div>
  <div class="sg">
    <div class="si"><span class="sl">開始日</span><span class="sv">{last['start']}</span></div>
    <div class="si"><span class="sl">継続日数</span><span class="sv">{last['days']}日</span></div>
    <div class="si"><span class="sl">開始日終値</span><span class="sv">{last['start_price']:,.0f}円</span></div>
    <div class="si"><span class="sl">現在値</span><span class="sv">{cur_price:,.0f}円</span></div>
    <div class="si"><span class="sl">騰落率</span><span class="sv" style="color:{pct_c}">{last['pct']:+.1f}%</span></div>
    <div class="si"><span class="sl">平均期間</span><span class="sv">{avg:.0f}日</span></div>
    <div class="si"><span class="sl">中央値期間</span><span class="sv">{med}日</span></div>
  </div>
  <div style="margin-top:12px;padding:10px;background:#0f172a;border-radius:6px;font-size:0.88rem;color:#fbbf24">
    📊 {remain_str}
  </div>
</div>"""

    # 統計カード
    def stat_card(title, s, color, bg):
        if not s:
            return ""
        buckets = [(0,10),(10,20),(20,30),(30,60),(60,90),(90,180),(180,9999)]
        bars = ""
        dist = []
        mx = 1
        for lo, hi in buckets:
            cnt = sum(1 for d in s["days_list"] if lo <= d < hi)
            if cnt:
                lbl = f"{lo}〜{hi-1}日" if hi < 9999 else f"{lo}日以上"
                dist.append((lbl, cnt))
                mx = max(mx, cnt)
        for lbl, cnt in dist:
            w = int(cnt / mx * 100)
            bars += f"""<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:0.8rem">
  <span style="width:76px;color:#94a3b8;flex-shrink:0">{lbl}</span>
  <div style="flex:1;background:#1e293b;border-radius:3px;height:13px">
    <div style="width:{w}%;background:{color};height:100%;border-radius:3px"></div>
  </div>
  <span style="width:28px;text-align:right;color:#e2e8f0">{cnt}</span>
</div>"""
        return f"""
<div style="background:{bg};border:1px solid {color}44;border-radius:10px;padding:18px;flex:1;min-width:270px">
  <div style="color:{color};font-weight:700;font-size:1rem;margin-bottom:12px">{title}</div>
  <div class="sg" style="margin-bottom:12px">
    <div class="si"><span class="sl">回数</span><span class="sv">{s['count']}回</span></div>
    <div class="si"><span class="sl">平均期間</span><span class="sv">{s['avg_days']:.0f}日</span></div>
    <div class="si"><span class="sl">中央値</span><span class="sv">{s['med_days']}日</span></div>
    <div class="si"><span class="sl">最短</span><span class="sv">{s['min_days']}日</span></div>
    <div class="si"><span class="sl">最長</span><span class="sv">{s['max_days']}日</span></div>
    <div class="si"><span class="sl">平均騰落</span><span class="sv" style="color:{color}">{s['avg_pct']:+.1f}%</span></div>
  </div>
  <div style="font-size:0.75rem;color:#64748b;margin-bottom:5px">期間分布</div>
  {bars}
</div>"""

    up_card   = stat_card("上昇トレンド ▲", su, "#4ade80", "#052e16")
    down_card = stat_card("下落トレンド ▼", sd, "#f87171", "#2d0a0a")

    # 全期間テーブル
    rows = ""
    for p in reversed(periods):
        t = p["trend"]
        is_c = p.get("is_current", False)
        if t == "up":
            tc, mark, bg_r, bl = "#4ade80", "▲ 上昇", "background:#052e1620;", "border-left:3px solid #4ade80;"
        elif t == "down":
            tc, mark, bg_r, bl = "#f87171", "▼ 下落", "background:#2d0a0a20;", "border-left:3px solid #f87171;"
        else:
            tc, mark, bg_r, bl = "#fbbf24", "→ 横ばい", "background:#2d1f0020;", "border-left:3px solid #fbbf24;"
        bold   = "font-weight:700;" if is_c else ""
        drop   = p.get("max_drop", 0.0)
        drop_s = f"{drop:+.1f}%" if drop else "—"
        drop_c = "#f87171" if drop < -2 else "#94a3b8"
        note   = ""
        if t == "sideways" and drop < -3:
            note = f'<span style="color:#f87171;font-size:0.75rem"> ⚠️V字{drop:.0f}%</span>'
        rows += f"""<tr style="{bg_r}{bold}">
  <td style="color:{tc};{bl}padding-left:10px">{mark}{note}</td>
  <td>{p['start']}</td>
  <td>{p['end']}{'　▶現在' if is_c else ''}</td>
  <td style="text-align:right">{p['days']}日</td>
  <td style="text-align:right;color:{tc}">{p['pct']:+.1f}%</td>
  <td style="text-align:right;color:{drop_c}">{drop_s}</td>
  <td style="text-align:right">{p['start_price']:,.0f}</td>
  <td style="text-align:right">{p['min_price']:,.0f}</td>
  <td style="text-align:right">{p['end_price']:,.0f}</td>
</tr>"""

    return f"""
<h2>現在のトレンド状況</h2>
{current_box}

<h2>トレンド統計（過去{years}年）</h2>
<div style="display:flex;flex-wrap:wrap;gap:16px">
  {up_card}
  {down_card}
</div>

<h2>全トレンド期間一覧（新しい順）</h2>
<table>
<thead><tr>
  <th>種別</th><th>開始日</th><th>終了日</th>
  <th>日数</th><th>騰落率</th><th>最大下落</th>
  <th>開始日終値(円)</th><th>最安値(円)</th><th>終了日終値(円)</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="footnote">
  ※ 価格はすべて <strong>終値</strong>（始値・高値・安値は使用していません）<br>
  ※ 判定: 終値&gt;MA10&gt;MA25=上昇(▲) ／ 終値&lt;MA10&lt;MA25=下落(▼) ／ それ以外=横ばい(→)<br>
  ※ 横ばいでも ⚠️V字 は3%超の下落があったV字回復を示す
</p>"""


def _tab3_timing_html(close: pd.Series, up_periods: list[dict], all_stats: dict) -> str:
    """タブ3: エントリータイミング分析"""
    surv    = survival_curve(up_periods)
    entry_s = entry_stats(up_periods)
    d_risk  = downtrend_risk(up_periods)
    done    = [p for p in up_periods if not p["is_current"]]

    lags      = [p["lag_bars"] for p in done if p["lag_bars"] >= 0]
    lag_pcts  = [p["lag_pct"]  for p in done if p["lag_pct"]  >= 0]
    avg_lag   = sum(lags) / len(lags)       if lags     else 0
    avg_lagp  = sum(lag_pcts) / len(lag_pcts) if lag_pcts else 0
    avg_days  = sum(p["days"] for p in done) / len(done) if done else 0
    avg_pct   = sum(p["total_pct"] for p in done) / len(done) if done else 0

    safe_end = next((n for n in sorted(d_risk) if d_risk[n] > 25), 30)
    su       = all_stats.get("up", {})

    # 現在の上昇トレンド状況
    cur_up = next((p for p in reversed(up_periods) if p["is_current"]), None)
    cur_up_html = ""
    if cur_up:
        cd     = cur_up["days"]
        sv     = surv.get(cd, None)
        dr_key = min(cd, max(d_risk.keys())) if d_risk else 30
        dr     = d_risk.get(dr_key, 0)
        sv_str = f"{sv:.0f}%" if sv is not None else "—"
        sv_c   = "#4ade80" if (sv or 0) > 60 else ("#fbbf24" if (sv or 0) > 30 else "#f87171")
        dr_c   = "#f87171" if dr > 30 else ("#fbbf24" if dr > 15 else "#4ade80")
        status = (f'<span style="color:#4ade80">✅ 推奨ウィンドウ内（〜{safe_end}日目）</span>'
                  if cd <= safe_end
                  else f'<span style="color:#f87171">⚠️ {safe_end}日目超過 — 新規エントリーは慎重に</span>')
        cur_up_html = f"""
<div class="info-box" style="border-color:#166534">
  <div style="font-weight:700;color:#4ade80;margin-bottom:10px">📈 現在の上昇トレンド（エントリータイミング）</div>
  <div class="sg">
    <div class="si"><span class="sl">開始日</span><span class="sv">{cur_up['start_date']}</span></div>
    <div class="si"><span class="sl">経過日数</span><span class="sv">{cd}日</span></div>
    <div class="si"><span class="sl">開始日終値</span><span class="sv">{cur_up['start_p']:,.0f}円</span></div>
    <div class="si"><span class="sl">確認ラグ</span><span class="sv">{cur_up['lag_bars']}営業日</span></div>
    <div class="si"><span class="sl">乗り遅れ幅</span><span class="sv" style="color:#fbbf24">+{cur_up['lag_pct']:.1f}%</span></div>
    <div class="si"><span class="sl">まだ継続確率</span><span class="sv" style="color:{sv_c}">{sv_str}</span></div>
    <div class="si"><span class="sl">5日内転換リスク</span><span class="sv" style="color:{dr_c}">{dr:.0f}%</span></div>
  </div>
  <div style="margin-top:10px;padding:8px 12px;background:#0f172a;border-radius:6px;font-size:0.88rem">
    {status}
  </div>
</div>"""

    # KPI
    kpi_html = f"""
<div class="kpi-grid">
  <div class="kpi"><div class="kpi-l">平均ラグ（営業日）</div>
    <div class="kpi-v" style="color:#fbbf24">{avg_lag:.1f}日</div></div>
  <div class="kpi"><div class="kpi-l">平均乗り遅れ幅</div>
    <div class="kpi-v" style="color:#f87171">+{avg_lagp:.1f}%</div></div>
  <div class="kpi"><div class="kpi-l">上昇トレンド平均期間</div>
    <div class="kpi-v">{avg_days:.0f}日</div></div>
  <div class="kpi"><div class="kpi-l">上昇トレンド平均騰落</div>
    <div class="kpi-v" style="color:#4ade80">+{avg_pct:.1f}%</div></div>
  <div class="kpi"><div class="kpi-l">完結トレンド数</div>
    <div class="kpi-v">{len(done)}回</div></div>
</div>"""

    # エントリーテーブル
    entry_rows = ""
    for n, s in sorted(entry_s.items()):
        rc = "#4ade80" if s["avg_ret"] > 0 else "#f87171"
        wc = "#4ade80" if s["win_rate"] >= 50 else "#f87171"
        dr = d_risk.get(n, 0)
        dc = "#f87171" if dr > 30 else ("#fbbf24" if dr > 15 else "#4ade80")
        entry_rows += f"""<tr>
  <td style="text-align:center;font-weight:600">{n}日目</td>
  <td style="text-align:right">{s['count']}回</td>
  <td style="text-align:right;color:{wc}">{s['win_rate']:.0f}%</td>
  <td style="text-align:right;color:{rc}">{s['avg_ret']:+.1f}%</td>
  <td style="text-align:right;color:{rc}">{s['med_ret']:+.1f}%</td>
  <td style="text-align:right;color:{dc}">{dr:.0f}%</td>
</tr>"""

    # 生存曲線
    surv_rows = ""
    for n in [1,2,3,4,5,6,7,8,9,10,12,15,20,25,30,35,40,50]:
        if n not in surv:
            continue
        pct = surv[n]
        bw  = int(pct)
        bc  = "#4ade80" if pct > 60 else ("#fbbf24" if pct > 30 else "#f87171")
        surv_rows += f"""<tr>
  <td style="text-align:center">{n}日目</td>
  <td>
    <div style="display:flex;align-items:center;gap:8px">
      <div style="width:180px;background:#1e293b;border-radius:3px;height:13px">
        <div style="width:{bw}%;background:{bc};height:100%;border-radius:3px"></div>
      </div>
      <span style="color:{bc};font-weight:600">{pct:.0f}%</span>
    </div>
  </td>
</tr>"""

    # 全上昇区間テーブル
    p_rows = ""
    for p in reversed(up_periods):
        is_c  = p["is_current"]
        bold  = "font-weight:700;" if is_c else ""
        lc    = "#4ade80" if p["total_pct"] >= 0 else "#f87171"
        lag_c = "#f87171" if p["lag_pct"] > 3 else "#94a3b8"
        p_rows += f"""<tr style="{bold}">
  <td>{p['start_date']}</td>
  <td>{p['end_date']}{'　▶現在' if is_c else ''}</td>
  <td style="text-align:right">{p['days']}日</td>
  <td style="text-align:right;color:{lc}">{p['total_pct']:+.1f}%</td>
  <td style="text-align:right">{p['start_p']:,.0f}</td>
  <td style="text-align:right;color:{lag_c}">{p['lag_bars']}営業日 / {p['lag_pct']:+.1f}%</td>
</tr>"""

    return f"""
<h2>シグナル確認ラグ（底値からMAクロスまで）</h2>
<div class="info-box">
  <p style="color:#94a3b8;font-size:0.88rem;margin-bottom:12px">
    MA10がMA25を上抜けた時点（シグナル確認日）は実際の底値より遅れます。<br>
    この乗り遅れ分を差し引いても上昇トレンドの残りリターンが取れるかが判断基準です。
  </p>
  {kpi_html}
</div>

{cur_up_html}

<h2>推奨エントリーウィンドウ</h2>
<div class="rec-box">
  <div style="font-size:1.05rem;font-weight:700;color:#4ade80;margin-bottom:10px">
    ✅ シグナル確認後 1〜{safe_end}日目 が最も効率的
  </div>
  <ul style="color:#94a3b8;font-size:0.88rem;line-height:2;padding-left:1.4em">
    <li>シグナル確認直後（1〜3日目）: 乗り遅れ幅が小さく残りリターンが最大</li>
    <li>{safe_end}日目以降: 5日内に下落転換する確率が25%を超え始める</li>
    <li>トレンド開始から中央値（{su.get('med_days', 0)}日）を超えたら新規エントリーは慎重に</li>
  </ul>
</div>

<h2>エントリー日別 期待リターン（シグナル確認後 N 日目 → トレンド終了まで保有）</h2>
<table>
<thead><tr>
  <th>エントリー</th><th>サンプル数</th><th>勝率</th>
  <th>平均リターン</th><th>中央値リターン</th><th>5日内転換リスク</th>
</tr></thead>
<tbody>{entry_rows}</tbody>
</table>
<p style="color:#475569;font-size:0.78rem;margin-top:4px">
  ※ リターン = N日目の終値でエントリー → トレンド終了日終値まで保有した場合の騰落率
</p>

<h2>生存確率（上昇 N 日目でまだトレンドが続いている確率）</h2>
<table style="max-width:440px">
<thead><tr><th>経過日数</th><th>まだ上昇トレンド中の確率</th></tr></thead>
<tbody>{surv_rows}</tbody>
</table>

<h2>全上昇トレンド区間 一覧（確認ラグ付き）</h2>
<table>
<thead><tr>
  <th>開始日</th><th>終了日</th><th>期間</th><th>騰落率</th>
  <th>開始日終値(円)</th><th>確認ラグ（底値→シグナル）</th>
</tr></thead>
<tbody>{p_rows}</tbody>
</table>
<p class="footnote">
  ※ 確認ラグ = MA10がMA25を上抜けた日 − 直前30日間の最安値の日（営業日数）<br>
  ※ 乗り遅れ幅3%超（赤表示）は、シグナル時点で底値から既に大きく上昇済みのケース
</p>"""


# ═══════════════════════════════════════════════════════════════════════════════
# メイン HTML 組み立て
# ═══════════════════════════════════════════════════════════════════════════════

CSS = """
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:"Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a; color:#e2e8f0; padding:24px; max-width:1080px; margin:0 auto; }
h1 { color:#60a5fa; font-size:1.55rem; margin-bottom:4px; }
h2 { color:#60a5fa; font-size:1.05rem; margin:26px 0 11px;
     border-left:3px solid #60a5fa; padding-left:10px; }
.subtitle { color:#94a3b8; font-size:0.9rem; margin-bottom:22px; }
.footnote { color:#334155; font-size:0.75rem; margin-top:20px; line-height:1.7; }

/* タブ */
.tab-nav { display:flex; gap:6px; margin-bottom:24px; border-bottom:2px solid #1e293b; padding-bottom:0; }
.tab-btn { padding:9px 22px; background:#1e293b; border:none; border-radius:6px 6px 0 0;
           color:#94a3b8; cursor:pointer; font-size:0.92rem; font-family:inherit;
           border-bottom:2px solid transparent; margin-bottom:-2px; }
.tab-btn.active { background:#0f172a; color:#60a5fa; border-bottom:2px solid #60a5fa; font-weight:700; }
.tab-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.tab-pane { display:none; }
.tab-pane.active { display:block; }

/* 相場環境パネル */
.regime-panel { display:flex; flex-wrap:wrap; gap:14px;
                background:#0d1424; border:1px solid #1e3a5f;
                border-radius:10px; padding:18px; margin-bottom:16px; }
.regime-item  { display:flex; flex-direction:column; min-width:110px; }
.ri-label { font-size:0.71rem; color:#64748b; margin-bottom:3px; }
.ri-val   { font-size:0.95rem; font-weight:600; }

/* 警告ボックス */
.warn-box { background:#2d1f00; border:1px solid #92400e;
            border-radius:8px; padding:14px 18px; margin-bottom:16px;
            color:#fde68a; font-size:0.88rem; line-height:1.7; }

/* スクリプトカード */
.script-card { border:1px solid; border-radius:10px; padding:14px 18px;
               margin-bottom:10px; }
.script-card:hover { filter:brightness(1.08); }
.badge { display:inline-block; padding:2px 10px; border-radius:99px;
         font-size:0.78rem; font-weight:700; }
.cmd-box { display:block; margin-top:8px; background:#0f172a;
           padding:6px 12px; border-radius:6px; color:#38bdf8;
           font-size:0.85rem; font-family:monospace; }

/* 現在トレンドボックス */
.current-box { border:1px solid; border-radius:10px; padding:18px; margin-bottom:16px; }
.info-box { background:#0d1424; border:1px solid #1e3a5f;
            border-radius:10px; padding:16px 20px; margin-bottom:16px; }
.rec-box  { background:#052e16; border:1px solid #166534;
            border-radius:8px; padding:16px 20px; margin-bottom:16px; }

/* stat grid */
.sg { display:flex; flex-wrap:wrap; gap:10px; }
.si { display:flex; flex-direction:column; min-width:100px; }
.sl { font-size:0.71rem; color:#64748b; }
.sv { font-size:1rem; font-weight:600; }

/* KPI */
.kpi-grid { display:flex; flex-wrap:wrap; gap:14px; margin-bottom:16px; }
.kpi { background:#111827; border:1px solid #1e293b; border-radius:8px;
       padding:13px 16px; min-width:150px; flex:1; }
.kpi-l { font-size:0.74rem; color:#64748b; margin-bottom:4px; }
.kpi-v { font-size:1.25rem; font-weight:700; }

/* テーブル */
table { width:100%; border-collapse:collapse; font-size:0.83rem; margin-bottom:8px; }
th { background:#1e293b; color:#94a3b8; padding:7px 10px;
     border:1px solid #334155; text-align:center; white-space:nowrap; }
td { padding:5px 10px; border:1px solid #1e293b; }
tr:hover td { filter:brightness(1.15); }

/* マーケット概況グリッド */
.mkt-grid { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:16px; }
.mkt-card { background:#0d1424; border:1px solid #1e3a5f; border-radius:10px;
            padding:14px 18px; min-width:160px; flex:1; }
.mkt-label { font-size:0.72rem; color:#64748b; margin-bottom:5px; }
.mkt-val   { font-size:1.15rem; font-weight:700; color:#e2e8f0; }
.mkt-chg   { font-size:0.8rem; margin-top:5px; }
.mkt-note  { font-size:0.72rem; color:#475569; margin-top:5px; line-height:1.5; }
"""

JS = """
function switchTab(id) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelector('[data-tab="'+id+'"]').classList.add('active');
  document.getElementById(id).classList.add('active');
}
"""


def build_html(close: pd.Series, trend: pd.Series, r: dict,
               periods: list[dict], up_periods: list[dict],
               years: int, ref_date, indicators: dict | None = None) -> str:
    ref_str     = str(ref_date)
    is_past     = (ref_date != _TODAY)
    past_badge  = (f' <span style="background:#7c3aed;color:#fff;padding:2px 10px;'
                   f'border-radius:6px;font-size:0.78rem;vertical-align:middle">'
                   f'過去日付: {ref_str}</span>') if is_past else ""
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[r["trend"]]
    trend_ja    = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}[r["trend"]]

    tab1 = _tab1_signal_html(r, ref_date, indicators=indicators, periods=periods)
    tab2 = _tab2_trend_html(close, trend, periods, years)

    all_stats = {
        "up":   calc_stats([p for p in periods if p["trend"] == "up"]),
        "down": calc_stats([p for p in periods if p["trend"] == "down"]),
    }
    tab3 = _tab3_timing_html(close, up_periods, all_stats)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日経平均 総合分析 — {ref_str}</title>
<style>{CSS}</style>
</head>
<body>
<h1>日経平均 総合分析レポート{past_badge}</h1>
<p class="subtitle">
  基準日: {ref_str} ／ 分析期間: {close.index[0].date()} 〜 {ref_str} (過去{years}年) ／
  {ref_str}時点: <strong style="color:{trend_color}">{trend_ja} {r['cur']:,.0f}円</strong>
</p>

<div class="tab-nav">
  <button class="tab-btn active" data-tab="t1" onclick="switchTab('t1')">📊 シグナル判定</button>
  <button class="tab-btn"        data-tab="t2" onclick="switchTab('t2')">📈 トレンド期間</button>
  <button class="tab-btn"        data-tab="t3" onclick="switchTab('t3')">⏱ エントリー分析</button>
</div>

<div id="t1" class="tab-pane active">{tab1}</div>
<div id="t2" class="tab-pane">{tab2}</div>
<div id="t3" class="tab-pane">{tab3}</div>

<script>{JS}</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="日経平均 総合分析レポート")
    parser.add_argument("--years",      type=int, default=5,   help="分析期間（年）")
    parser.add_argument("--date",       type=str, default=None, help="基準日 YYYY-MM-DD (省略時=今日)")
    parser.add_argument("--no-browser", action="store_true",    help="HTML生成のみ")
    args = parser.parse_args()

    # 基準日を決定
    if args.date:
        try:
            from datetime import date as date_type
            ref_date = date_type.fromisoformat(args.date)
        except ValueError:
            print(f"[ERROR] --date の形式が不正です: {args.date}  (例: 2024-01-15)")
            return
        if ref_date > _TODAY:
            print(f"[ERROR] --date に未来の日付は指定できません: {ref_date}")
            return
        print(f"日経平均 総合分析 (基準日: {ref_date} / 過去{args.years}年)...", flush=True)
    else:
        ref_date = _TODAY
        print(f"日経平均 総合分析 (過去{args.years}年)...", flush=True)

    close = fetch_n225(args.years, end_date=ref_date if args.date else None)
    # --date 指定時: データが基準日以降まで含まれる場合は切り捨て
    if args.date:
        close = close[close.index <= pd.Timestamp(ref_date)]
    if close.empty:
        print(f"[ERROR] {ref_date} 時点のデータが取得できませんでした")
        return

    print("参考指標を取得中...", flush=True)
    indicators = fetch_market_indicators(years=1, end_date=ref_date if args.date else None)
    for mdef in MARKET_DEFS:
        s = indicators.get(mdef["ticker"])
        if s is not None and not s.empty:
            reg = get_indicator_regime(s)
            arr = {"up": "▲", "down": "▼", "sideways": "→"}[reg["trend"]]
            print(f"  {mdef['label']}: {reg['cur']:{mdef['fmt']}}{mdef['unit']} {arr}  "
                  f"5日{reg['mom5']:+.1f}% / 20日{reg['mom20']:+.1f}%")
        else:
            print(f"  {mdef['label']}: 取得失敗")

    trend     = label_trend(close)
    r         = get_regime(close)
    periods   = extract_periods(close, trend, ref_date)
    up_timing = extract_up_periods(close, trend, ref_date)

    # コンソールサマリー
    trend_ja = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[r["trend"]]
    print(f"{ref_date}: {trend_ja} / 日経 {r['cur']:,.0f}円 / 5日 {r['mom5']:+.1f}% / 20日 {r['mom20']:+.1f}%")

    up_p   = [p for p in periods if p["trend"] == "up"]
    down_p = [p for p in periods if p["trend"] == "down"]
    su = calc_stats(up_p)
    sd = calc_stats(down_p)
    if su:
        print(f"上昇: {su['count']}回 / 平均{su['avg_days']:.0f}日 / 中央値{su['med_days']}日")
    if sd:
        print(f"下落: {sd['count']}回 / 平均{sd['avg_days']:.0f}日 / 中央値{sd['med_days']}日")
    last = periods[-1]
    last_ja = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[last["trend"]]
    print(f"{last_ja}トレンド継続: {last['days']}日 ({last['pct']:+.1f}%)")

    html_path = Path(f"nikkei_analysis_{ref_date}.html")
    html_path.write_text(
        build_html(close, trend, r, periods, up_timing, args.years, ref_date,
                   indicators=indicators),
        encoding="utf-8"
    )
    print(f"生成: {html_path}")

    if not args.no_browser:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
