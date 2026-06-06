"""
nikkei_analysis.py  ―  日経平均 総合分析レポート

select_signals.py / analyze_nikkei_trend.py / analyze_trend_timing.py を1本に統合。
日経データを1回だけ取得し、タブ付きHTMLで以下3セクションを生成する。

  タブ1: シグナル判定    — 相場環境 + 今日使うべきスクリプト
  タブ2: トレンド期間    — 上昇/下落/横ばい期間の統計と一覧
  タブ3: エントリー分析  — 上昇何日目に入ると良いか / 生存確率
  タブ4: シグナル一覧    — 全WATCHLISTの今日のシグナルをスコア降順表示 (--with-signals)
  タブ5: 損益レポート    — 直近N日取引損益 (--with-pnl)

Usage:
    python nikkei_analysis.py                       # 過去5年 HTML生成 & ブラウザ表示
    python nikkei_analysis.py --years 10            # 過去10年
    python nikkei_analysis.py --date 2024-01-15     # 指定日時点の分析
    python nikkei_analysis.py --no-browser          # HTML生成のみ
    python nikkei_analysis.py                    # 全5タブ（デフォルト）
    python nikkei_analysis.py --no-signals       # シグナルタブなし
    python nikkei_analysis.py --no-pnl           # 損益タブなし
    python nikkei_analysis.py --min-score 60     # ★★以上シグナルのみ
    python nikkei_analysis.py --days 30          # 直近30日損益
"""
from __future__ import annotations
import argparse
import os
import copy as _copy
import importlib as _importlib
import webbrowser
from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
from datetime import timedelta, timezone, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

JST    = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()

# ═══════════════════════════════════════════════════════════════════════════════
# シグナル / 損益モジュール (オプション)
# ═══════════════════════════════════════════════════════════════════════════════

_SIGNALS_AVAILABLE = False
_DEF_WORKERS = 4
_PNL_CONFIGS: list[dict] = []
try:
    os.environ.setdefault("TRADING_MODE", "conservative")
    import check_signals_stop     as _stop
    import check_signals_breakout as _brk
    from backtest_limit_entry import WORKERS as _DEF_WORKERS, calc_qty as _calc_qty
    _CON_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
    _CON_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
    os.environ["TRADING_MODE"] = "aggressive"
    _importlib.reload(_stop); _importlib.reload(_brk)
    _AGG_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
    _AGG_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
    os.environ["TRADING_MODE"] = "conservative"
    _importlib.reload(_stop); _importlib.reload(_brk)
    import run_signals_wf as _wf_mod
    _WF_AGG_STOP = list(_wf_mod._STOP_WATCHLIST_AGGRESSIVE)
    _WF_AGG_BRK  = list(_wf_mod._BRK_WATCHLIST_AGGRESSIVE)
    _WF_CON_STOP = list(_wf_mod._STOP_WATCHLIST_CONSERVATIVE)
    _WF_CON_BRK  = list(_wf_mod._BRK_WATCHLIST_CONSERVATIVE)
    _BASE_STOP   = list(_stop.WATCHLIST)
    _BASE_BRK    = list(_brk.WATCHLIST)
    try:
        import run_signals_nolimit as _nl_mod
        _NL_STOP = list(_nl_mod.STOP_WATCHLIST)
        _NL_BRK  = list(_nl_mod.BRK_WATCHLIST)
    except Exception:
        _NL_STOP = []
        _NL_BRK  = []
    try:
        import run_signals_merged as _mg_mod
        def _dedup(a, b):
            seen = set(); out = []
            for e in a + b:
                k = (e[0], e[2])
                if k not in seen: seen.add(k); out.append(e)
            return out
        _MG_STOP = _dedup(_BASE_STOP, list(_mg_mod._WF_STOP))
        _MG_BRK  = _dedup(_BASE_BRK,  list(_mg_mod._WF_BRK))
    except Exception:
        _MG_STOP = []
        _MG_BRK  = []
    _stop.STRATEGY_PARAMS.update(_CON_STOP)
    _brk.STRATEGY_PARAMS.update(_CON_BRK)
    _PNL_CONFIGS = [
        {"label": "既存版 conservative", "color": "#3498db", "mode": "conservative", "sm_tm": None, "stop_wl": _BASE_STOP, "brk_wl": _BASE_BRK},
        {"label": "既存版 aggressive",   "color": "#e74c3c", "mode": "aggressive",   "sm_tm": None, "stop_wl": _BASE_STOP, "brk_wl": _BASE_BRK},
        {"label": "WF conservative",    "color": "#06b6d4", "mode": "conservative", "sm_tm": None, "stop_wl": _WF_CON_STOP, "brk_wl": _WF_CON_BRK},
        {"label": "WF aggressive",      "color": "#f39c12", "mode": "aggressive",   "sm_tm": None, "stop_wl": _WF_AGG_STOP, "brk_wl": _WF_AGG_BRK},
        *([{"label": "NOLIMIT WF",      "color": "#a855f7", "mode": "aggressive",   "sm_tm": (1.5, 2.0), "stop_wl": _NL_STOP, "brk_wl": _NL_BRK}]
          if _NL_STOP or _NL_BRK else []),
        *([{"label": "WF+既存統合",     "color": "#10b981", "mode": "conservative", "sm_tm": None, "stop_wl": _MG_STOP,    "brk_wl": _MG_BRK}]
          if _MG_STOP or _MG_BRK else []),
    ]
    _SIGNALS_AVAILABLE = True
except Exception:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# WFスコア再実行チェック
# ═══════════════════════════════════════════════════════════════════════════════

def check_wf_refresh_needed() -> dict:
    """WFスコアの再実行要否をチェックして警告情報を返す。

    Returns:
        level       : "ok" / "info" / "warn" / "critical"
        warnings    : 表示すべき警告メッセージのリスト
        latest_date : 最新WF CSVの日付 (date | None)
        days_elapsed: 最新CSV から今日までの日数 (int | None)
        next_run_in : 次回推奨実行までの残日数 (int | None)
    """
    import csv as _csv_mod
    from datetime import date as _date

    warnings: list[str] = []
    level = "ok"

    # ── 1. walkforward_results/ の最新CSV日付 ──────────────────────────────
    wf_dir = Path("walkforward_results")
    latest_date: _date | None = None
    if wf_dir.exists():
        for f in sorted(wf_dir.glob("walkforward_*_????-??-??.csv")):
            parts = f.stem.split("_")
            if len(parts) < 3:
                continue
            date_str = parts[-1]
            try:
                d = _date.fromisoformat(date_str)
                if latest_date is None or d > latest_date:
                    latest_date = d
            except ValueError:
                continue

    days_elapsed: int | None = None
    next_run_in: int | None = None

    if latest_date is None:
        warnings.append(
            "⚠️ WFスコアCSVが見つかりません。"
            "scan_walkforward.py を実行して WFスコアを生成してください。"
        )
        level = "warn"
    else:
        days_elapsed = (_TODAY - latest_date).days
        next_run_in  = max(0, 90 - days_elapsed)
        if days_elapsed >= 180:
            warnings.append(
                f"🚨 WFスコアが {days_elapsed}日前 ({latest_date}) のデータです。"
                f"半年以上経過 — scan_walkforward.py の再実行を強く推奨します。"
            )
            level = "critical"
        elif days_elapsed >= 90:
            warnings.append(
                f"⚠️ WFスコアが {days_elapsed}日前 ({latest_date}) のデータです。"
                f"3ヶ月経過 — scan_walkforward.py の再実行を推奨します。"
            )
            level = "warn"

    # ── 2. forward_test_log.csv 直近14日の勝率チェック ──────────────────────
    from datetime import timedelta as _td
    fwd_log = Path("forward_test_log.csv")
    if fwd_log.exists():
        try:
            cutoff = _TODAY - _td(days=14)
            wins = losses = 0
            with open(fwd_log, newline="", encoding="utf-8") as fp:
                for row in _csv_mod.DictReader(fp):
                    try:
                        rd = _date.fromisoformat(row.get("record_date", ""))
                        if rd < cutoff:
                            continue
                        status = row.get("status", "")
                        if status == "target":
                            wins += 1
                        elif status in ("stop", "timeout"):
                            losses += 1
                    except (ValueError, KeyError):
                        continue
            total = wins + losses
            if total >= 5:
                wr = wins / total * 100
                if wr < 50:
                    warnings.append(
                        f"📉 フォワードテスト直近14日: 勝率 {wr:.0f}% ({wins}勝{losses}敗) — "
                        f"WATCHLISTの見直しを検討してください。"
                    )
                    if level == "ok":
                        level = "warn"
        except Exception:
            pass

    return {
        "level":        level,
        "warnings":     warnings,
        "latest_date":  latest_date,
        "days_elapsed": days_elapsed,
        "next_run_in":  next_run_in,
    }


def _wf_refresh_banner_html(status: dict) -> str:
    """check_wf_refresh_needed() の結果をHTMLバナーに変換。警告なしなら空文字。"""
    if not status["warnings"]:
        return ""

    level = status["level"]
    bg_map = {
        "critical": ("#7f1d1d", "#fca5a5"),
        "warn":     ("#451a03", "#fde68a"),
        "info":     ("#1e3a5f", "#93c5fd"),
    }
    bg, fg = bg_map.get(level, ("#374151", "#d1d5db"))

    items = "".join(
        f'<li style="margin:4px 0">{w}</li>' for w in status["warnings"]
    )

    next_hint = ""
    if status["next_run_in"] is not None and status["next_run_in"] > 0:
        next_hint = (
            f'<p style="margin:8px 0 0;font-size:0.82rem;opacity:0.85">'
            f'次回推奨実行まで残り約 <strong>{status["next_run_in"]}日</strong>'
            f'（目安: {status["latest_date"]} + 90日）</p>'
        )

    cmd_hint = (
        '<p style="margin:8px 0 0;font-size:0.82rem;font-family:monospace;opacity:0.85">'
        'python scan_walkforward.py &amp;&amp; python compute_wf_scores.py</p>'
    )

    return (
        f'<div style="background:{bg};color:{fg};border-radius:8px;'
        f'padding:14px 18px;margin-bottom:16px">'
        f'<ul style="margin:0;padding-left:1.4em">{items}</ul>'
        f'{next_hint}{cmd_hint}'
        f'</div>'
    )


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

SCRIPTS = [
    {"cmd": "python run_signals_nolimit.py",       "label": "株価制限なし",
     "sublabel": "aggressive / 84銘柄",            "risk": "高",
     "note": "上昇相場専用。高株価銘柄は急落時の損失も大きい。"},
    {"cmd": "python run_signals_prime.py",          "label": "プライム全銘柄",
     "sublabel": "aggressive / 84銘柄",            "risk": "中高",
     "note": "上昇・横ばい高ボラで有効。"},
    {"cmd": "python run_signals_wf.py --aggressive","label": "WF aggressive",
     "sublabel": "aggressive / WF選定 59銘柄",     "risk": "中",
     "note": "上昇相場で最も効率が良い（30日PF 3.24）。"},
    {"cmd": "python run_signals_wf.py --conservative","label": "WF conservative",
     "sublabel": "conservative / WF選定",          "risk": "低中",
     "note": "横ばい相場でも安定。逆指値Bが中心。"},
    {"cmd": "python run_signals.py",                "label": "既存版 conservative",
     "sublabel": "conservative / デフォルト 55銘柄","risk": "低",
     "note": "全相場で使用可能なベースライン。"},
    {"cmd": "python run_signals_merged.py",         "label": "WF+既存統合",
     "sublabel": "conservative / 統合WATCHLIST",   "risk": "低中",
     "note": "上昇・横ばいで有効。WFと既存のマージ。"},
]


def judge(script: dict, r: dict) -> tuple[str, str, str]:
    """(status, reason, advice)  status= ✅推奨 / ⚠️注意 / ❌停止"""
    trend = r["trend"]
    vol   = r["vol_level"]
    mom5  = r["mom5"]
    mom20 = r["mom20"]
    above = r["above_ma200"]
    drop  = r["max_1d_drop"]
    cmd   = script["cmd"]
    crash = drop < -3.0

    if "nolimit" in cmd:
        if trend == "down":
            return "❌ 停止", f"トレンド下落 (MA10<MA25)", "相場回復まで使用停止"
        if not above:
            return "❌ 停止", f"日経 < MA200 (長期下落トレンド)", "既存版のみに絞る"
        if mom5 < -2.0:
            return "❌ 停止", f"5日騰落 {mom5:+.1f}% (急落中)", "反発確認後に再開"
        if mom5 >= 2.0 and mom20 >= 3.0 and trend == "up":
            return "✅ 推奨", f"5日 {mom5:+.1f}% / 20日 {mom20:+.1f}% / 上昇トレンド", "条件を全て満たす"
        if mom5 >= 0.0 and trend == "up":
            return "⚠️ 注意", f"5日 {mom5:+.1f}% (上昇弱い)", "半数に絞るか WF に切替"
        return "⚠️ 注意", f"5日 {mom5:+.1f}% / 20日 {mom20:+.1f}%", "条件未達。プライムを優先"

    if "prime" in cmd:
        if trend == "down" and vol == "high":
            return "❌ 停止", "下落×高ボラ (急落相場)", "損切り連発のリスク大"
        if trend == "down":
            return "⚠️ 注意", "下落トレンド", "WF conservative に切り替え推奨"
        if trend == "up" or (trend == "sideways" and vol in ("mid", "high")):
            return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "相場環境と合致"
        return "⚠️ 注意", "低ボラ横ばい", "シグナルが少ない可能性"

    if "--aggressive" in cmd:
        if trend == "down" and vol == "high":
            return "❌ 停止", "下落×高ボラ", "DON戦略の損失が拡大しやすい"
        if trend == "down":
            return "⚠️ 注意", "下落トレンド", "conservative に切り替え"
        return "✅ 推奨", f"トレンド={trend}", "上昇・横ばいで有効"

    if trend == "down" and vol == "high" and crash:
        return "⚠️ 注意", f"下落×高ボラ×急落リスク (過去30日最大1日 {drop:+.1f}%)", "シグナルを精査して選択的に発注"
    return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "安定した相場適合"


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


def _priority_score(cmd: str, r: dict, status: str) -> int:
    """おすすめスコア 0〜100"""
    if status == "❌ 停止":
        return 0
    base = 30 if status == "⚠️ 注意" else 55

    trend = r["trend"]
    mom5  = r["mom5"]
    mom20 = r["mom20"]

    # Walk-forward選定: バックテスト信頼性が高い
    if "wf" in cmd:
        base += 20

    # aggressive × 上昇トレンド
    if "--aggressive" in cmd and trend == "up":
        base += 15

    # 強い上昇相場（5日+2%以上 / 20日+3%以上）では aggressive/nolimit を優遇
    if mom5 >= 2.0 and mom20 >= 3.0 and trend == "up" and r["above_ma200"]:
        if "--aggressive" in cmd or "nolimit" in cmd:
            base += 8

    # 株価制限なし: 全条件揃っている時のみ追加ボーナス
    if "nolimit" in cmd and mom5 >= 2.0 and mom20 >= 3.0 and trend == "up" and r["above_ma200"]:
        base += 5

    # プライム全銘柄: 株価制限なしと対象が重複する
    if "prime" in cmd:
        base -= 15

    # 統合版: 個別管理と重複し運用が複雑になる
    if "merged" in cmd:
        base -= 12

    # 下落相場でのペナルティ
    if trend == "down" and ("--aggressive" in cmd or "nolimit" in cmd or "prime" in cmd):
        base -= 20

    return min(100, max(0, base))


def _priority_reason(cmd: str, r: dict, score: int) -> str:
    """おすすめ度の短い理由"""
    trend = r["trend"]
    mom5  = r["mom5"]
    mom20 = r["mom20"]
    if "wf" in cmd and "--aggressive" in cmd:
        return "WF選定×aggressive — 信頼性と収益性のバランスが最良"
    if "nolimit" in cmd:
        nolimit_ok = mom5 >= 2.0 and mom20 >= 3.0 and trend == "up" and r["above_ma200"]
        if nolimit_ok:
            return f"5日{mom5:+.1f}%/20日{mom20:+.1f}% — 全条件が揃った稀なタイミング"
        return "条件が一部未達 — 使用は慎重に"
    if "wf" in cmd and "--conservative" in cmd:
        return "WF選定×conservative — 全相場で安定。サブ枠に最適"
    if "run_signals.py" in cmd and "wf" not in cmd and "nolimit" not in cmd and "merged" not in cmd:
        return "全相場のベースライン — 常時稼働用"
    if "prime" in cmd:
        return "株価制限なしと対象重複 — 両方使うなら不要"
    if "merged" in cmd:
        return "WF+既存の個別実行と重複 — 管理が複雑になる"
    return ""


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
    scored = [(s, st, rs, adv, _priority_score(s["cmd"], r, st))
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
        p_reason = _priority_reason(s["cmd"], r, score)
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

    # 推奨コマンド
    if recommended:
        rec_rows = "".join(
            f'<div style="margin:5px 0"><code class="cmd-box" style="display:inline-block">{c}</code></div>'
            for c in recommended
        )
        rec_html = f'<div style="background:#052e16;border:1px solid #166534;border-radius:8px;padding:16px">{rec_rows}</div>'
    else:
        rec_html = '<div class="warn-box" style="border-color:#991b1b">❌ 全スクリプト停止推奨。相場が回復するまで様子見を。</div>'

    # 株価制限なし 停止理由
    nolimit_s, nolimit_r, _ = judge(SCRIPTS[0], r)
    nolimit_block = ""
    if nolimit_s != "✅ 推奨":
        _tj  = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[r["trend"]]
        _mp  = "上" if r["above_ma200"] else "下"
        nolimit_block = f"""
<h2>株価制限なし が推奨外の理由</h2>
<div class="warn-box">
  <div><strong>{nolimit_r}</strong></div>
  <div style="margin-top:8px;color:#94a3b8;font-size:0.85rem">
    使用条件: 5日騰落 ≥ +2% ／ 20日騰落 ≥ +3% ／ 上昇トレンド ／ 日経 &gt; MA200<br>
    現　　状: 5日 <strong>{r['mom5']:+.1f}%</strong> ／
              20日 <strong>{r['mom20']:+.1f}%</strong> ／
              {_tj} ／ MA200{_mp}<br>
    根　　拠: 直近30日利益の95%が「日経急騰局面」に集中。条件外では1件あたり平均+782円。
  </div>
</div>"""

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

<h2>スクリプト判定</h2>
{cards_html}

<h2>{ref_date} 時点の推奨コマンド</h2>
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

def _set_sig_params(mode: str, sm_tm=None) -> None:
    if mode == "conservative":
        _stop.STRATEGY_PARAMS.update(_CON_STOP)
        _brk.STRATEGY_PARAMS.update(_CON_BRK)
    else:
        _stop.STRATEGY_PARAMS.update(_AGG_STOP)
        _brk.STRATEGY_PARAMS.update(_AGG_BRK)
    if sm_tm:
        sm, tm = sm_tm
        for k, v in list(_stop.STRATEGY_PARAMS.items()):
            _stop.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)
        for k, v in list(_brk.STRATEGY_PARAMS.items()):
            _brk.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)


def _fmt_score_cell(s: dict, col: str) -> str:
    """シグナルテーブルのスコアセルHTML。WFスコアとBTスコアを両表示。"""
    rank = s["rank"]
    if s.get("is_wf") and s.get("wf_score") is not None:
        rec = s.get("rec_score", "—")
        return (
            f'<span style="color:{col};font-weight:700">WF&nbsp;{s["wf_score"]}</span>'
            f'<span style="font-size:0.68rem;color:#64748b;display:block">{rank} / BT:{rec}</span>'
        )
    else:
        return (
            f'<span style="color:{col};font-weight:700">{rank}&nbsp;{s["score"]}</span>'
            f'<br><span style="font-size:0.68rem;color:#f59e0b">BT(参考)</span>'
        )


def _tab4_signals_html(workers: int, min_score: int = 0, target_date=None,
                       score_filter: int | None = None,
                       cfg_filter: str | None = None) -> str:
    """タブ4: 全WATCHLISTのシグナルをスコア降順表示。target_date=None で今日。
    score_filter 指定時: そのスコアだけの成績フォーカスカードを表示。"""
    if not _SIGNALS_AVAILABLE:
        return '<p style="color:#64748b;padding:20px">シグナルモジュールが見つかりません (check_signals_stop.py が必要)</p>'

    # config label → スクリプト名マッピング
    # 全configから重複排除した (sym, name, strat, is_stop) リストを作成
    # 各 (sym, strat) がどのスクリプトに含まれるかも記録
    # (sym, strat) → 出典 (label, color) リスト / 主configインデックス
    source_map: dict = {}   # (sym, strat) -> [(label, color), ...]
    primary_cfg: dict = {}  # (sym, strat, is_stop) -> cfg
    seen: set = set()
    all_items: list = []
    for cfg in _PNL_CONFIGS:
        entry = (cfg["label"], cfg["color"])
        for sym, name, strat in cfg["stop_wl"]:
            k = (sym, strat)
            source_map.setdefault(k, [])
            if entry not in source_map[k]:
                source_map[k].append(entry)
            ki = (sym, strat, True)
            if ki not in seen:
                seen.add(ki); all_items.append((sym, name, strat, True)); primary_cfg[ki] = cfg
        for sym, name, strat in cfg["brk_wl"]:
            k = (sym, strat)
            source_map.setdefault(k, [])
            if entry not in source_map[k]:
                source_map[k].append(entry)
            ki = (sym, strat, False)
            if ki not in seen:
                seen.add(ki); all_items.append((sym, name, strat, False)); primary_cfg[ki] = cfg

    # configごとにパラメータを設定して並列実行（パラメータ競合を避けるため順次処理）
    signals: list[dict] = []
    seen_sig: set = set()
    all_trade_infos: list[dict] = []   # スコア別勝率計算用（全銘柄×全config）

    # all_trade_infos は全config × 全銘柄で集計（重複排除しない）
    # → 同一銘柄が conservative/aggressive 両方に存在する場合、両方のスコアを収集
    # シグナル確認は primary_cfg の銘柄のみ（重複排除済み）
    items_by_cfg: dict = {cfg["label"]: [] for cfg in _PNL_CONFIGS}
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg["stop_wl"]:
            items_by_cfg[cfg["label"]].append((sym, name, strat, True))
        for sym, name, strat in cfg["brk_wl"]:
            items_by_cfg[cfg["label"]].append((sym, name, strat, False))

    import pandas as _pd
    from backtest_limit_entry import ENTRY_EXPIRE as _EXP, MAX_HOLD as _MH

    for cfg in _PNL_CONFIGS:
        group = items_by_cfg.get(cfg["label"], [])
        if not group:
            continue
        _set_sig_params(cfg["mode"], cfg.get("sm_tm"))

        def _check_one(item, _td=target_date, _ms=min_score, _sm=source_map,
                       _cfg_label=cfg["label"], _cfg_color=cfg["color"]):
            sym, name, strat, is_stop = item
            mod = _stop if is_stop else _brk
            bt = mod.backtest_one(sym, name, strat)
            if not bt:
                return None
            # おすすめスコアは常に計算
            rec_score, rec_rank = _stop.calc_recommend_score(bt["period_results"])
            # WFスコア（out-of-sample）があれば優先してソートキーに使う
            _get_wf = getattr(_stop, "get_wf_score", None)
            wf = _get_wf(sym, strat) if _get_wf else None
            if wf:
                wf_score, wf_rank_str = wf
                score, rank = wf_score, wf_rank_str
                is_wf = True
            else:
                wf_score, wf_rank_str = None, None
                score, rank = rec_score, rec_rank
                is_wf = False

            # 全銘柄のトレード履歴を収集（スコア別勝率のため）
            max_period = max(bt["period_results"].keys())
            trade_log  = bt["period_results"][max_period].get("trade_log", [])
            bt_info = {"score": score, "trades": trade_log,
                       "sym": sym, "name": name, "strat": strat, "is_wf": is_wf,
                       "wf_score": wf_score, "rec_score": rec_score,
                       "cfg_label": _cfg_label, "cfg_color": _cfg_color}

            # シグナル確認はプライマリconfigの銘柄のみ（重複シグナル防止）
            ki = (sym, strat, is_stop)
            is_primary = primary_cfg.get(ki, {}).get("label") == _cfg_label
            if not is_primary or score < _ms:
                return {"_bt": bt_info}

            sig = mod.check_signal_on_date(sym, strat, _td)
            if not sig:
                return {"_bt": bt_info}

            order_p = sig.get("order_price", 0)
            limit_p = sig.get("limit_entry_price", round(order_p * 1.03) if order_p else 0)
            sig_dt  = sig.get("signal_date")
            try:
                _max_exit = _pd.bdate_range(start=_pd.to_datetime(sig_dt),
                                            periods=_EXP + _MH + 1)[-1].date()
            except Exception:
                _max_exit = None
            return {
                "_bt": bt_info,
                "_sig": {
                    "symbol": sym, "name": name, "strategy": strat,
                    "score": score, "rank": rank, "is_wf": is_wf,
                    "wf_score": wf_score, "wf_rank_str": wf_rank_str,
                    "rec_score": rec_score,
                    "signal_date":  sig_dt,
                    "signal_price": sig.get("signal_price", 0),
                    "order_p":      order_p,
                    "limit_p":      limit_p,
                    "stop_p":       sig.get("stop_price",  0),
                    "target_p":     sig.get("target_price", 0),
                    "max_hold":     _MH,
                    "max_exit":     _max_exit,
                    "sources":      _sm.get((sym, strat), []),
                    "cfg_label":    _cfg_label,
                },
            }

        with _TPE(max_workers=workers) as ex:
            futs = {ex.submit(_check_one, item): item for item in group}
            for fut in _asc(futs):
                try:
                    r = fut.result()
                    if not r:
                        continue
                    if r.get("_bt"):
                        all_trade_infos.append(r["_bt"])
                    sig_r = r.get("_sig")
                    if sig_r and (sig_r["symbol"], sig_r["strategy"]) not in seen_sig:
                        seen_sig.add((sig_r["symbol"], sig_r["strategy"]))
                        signals.append(sig_r)
                except Exception:
                    pass

    _set_sig_params("conservative")

    signals.sort(key=lambda x: -x["score"])
    if cfg_filter:
        signals = [s for s in signals if s.get("cfg_label") == cfg_filter]

    # ── スコア別集計ヘルパー ──────────────────────────────────────────────────
    def _score_stats(trades_list):
        """決済済みトレードのリストから (n, wins, pnl, gp, gl) を返す"""
        ts  = [t for t in trades_list if t.get("exit_dt") is not None]
        n   = len(ts)
        if not n:
            return None
        wins = sum(1 for t in ts if t.get("pnl", 0) > 0)
        pnl  = sum(t.get("pnl", 0) for t in ts)
        gp   = sum(t.get("pnl", 0) for t in ts if t.get("pnl", 0) > 0)
        gl   = abs(sum(t.get("pnl", 0) for t in ts if t.get("pnl", 0) < 0))
        return {"n": n, "wins": wins, "pnl": pnl, "gp": gp, "gl": gl}

    def _stat_html(st, label, color="#e2e8f0"):
        if not st or st["n"] == 0:
            return ""
        wr  = st["wins"] / st["n"] * 100
        pf  = st["gp"] / st["gl"] if st["gl"] > 0 else (float("inf") if st["gp"] > 0 else 0.0)
        avg = st["pnl"] / st["n"]
        pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
        wrc   = "#4ade80" if wr >= 55  else ("#fbbf24" if wr >= 45  else "#f87171")
        pfc   = "#4ade80" if pf >= 1.5 else ("#fbbf24" if pf >= 1.0 else "#f87171")
        _target_score = score_filter if score_filter is not None else -1
        _mxseen: set = set()
        loss_ts = []
        win_ts  = []
        for info in all_trade_infos:
            if info["score"] != _target_score:
                continue
            for t in info["trades"]:
                if t.get("exit_dt") is None:
                    continue
                _mk = (info["sym"], info["strat"], t.get("signal_dt"))
                if _mk in _mxseen:
                    continue
                _mxseen.add(_mk)
                if t.get("pnl", 0) < 0:
                    loss_ts.append(t)
                elif t.get("pnl", 0) > 0:
                    win_ts.append(t)
        max_loss = min((t["pnl"] for t in loss_ts), default=0)
        max_win  = max((t["pnl"] for t in win_ts), default=0)
        items = [
            ("取引数",       f'{st["n"]}件',                            "#e2e8f0"),
            ("勝率",         f'{wr:.1f}%',                              wrc),
            ("PF",           pf_s,                                      pfc),
            ("合計損益",     f'{st["pnl"]:+,.0f}円',                   "#4ade80" if st["pnl"]>=0 else "#f87171"),
            ("平均損益",     f'{avg:+,.0f}円',                          "#4ade80" if avg>=0 else "#f87171"),
            ("最大利益",     f'{max_win:+,.0f}円' if max_win else "—",  "#4ade80"),
            ("最大損失",     f'{max_loss:+,.0f}円' if max_loss else "—","#f87171"),
        ]
        kpis = "".join(
            f'<div style="background:#111827;border:1px solid #1e293b;border-radius:8px;'
            f'padding:10px 14px;text-align:center;min-width:110px;flex:1">'
            f'<div style="font-size:0.7rem;color:#64748b;margin-bottom:4px">{k}</div>'
            f'<div style="font-size:1.1rem;font-weight:700;color:{c}">{v}</div></div>'
            for k, v, c in items
        )
        return f"""
<div style="background:#0d1424;border:2px solid {color};border-radius:12px;
            padding:18px 20px;margin-bottom:16px">
  <div style="font-size:1rem;font-weight:700;color:{color};margin-bottom:14px">
    {label}
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:10px">{kpis}</div>
</div>"""

    # ── score_filter 指定時: そのスコア単体フォーカス ─────────────────────
    score_section = ""
    if score_filter is not None:
        # 同一シグナルが複数configから重複しないよう signal_dt でデデュップ
        _sf_seen: set = set()
        sf_trades = []
        for info in all_trade_infos:
            if info["score"] != score_filter:
                continue
            for t in info["trades"]:
                _k = (info["sym"], info["strat"], t.get("signal_dt"))
                if _k in _sf_seen:
                    continue
                _sf_seen.add(_k)
                sf_trades.append(t)
        st = _score_stats(sf_trades)
        n_stocks = len({(info["sym"], info["strat"])
                        for info in all_trade_infos if info["score"] == score_filter})
        focus_html = ""
        if st:
            color = "#4ade80" if st["wins"]/st["n"]*100 >= 55 else ("#fbbf24" if st["wins"]/st["n"]*100 >= 45 else "#f87171")
            focus_html = _stat_html(st, f"スコア {score_filter} の成績（直近365日 / {n_stocks}銘柄)", color)
        else:
            focus_html = f'<div style="color:#64748b;padding:16px">スコア {score_filter} の取引データがありません</div>'

        # スコア±10の隣接スコアテーブル
        lo_range = max(0, score_filter - 10)
        hi_range = min(100, score_filter + 10)
        by_score: dict[int, list] = {}
        for info in all_trade_infos:
            s = info["score"]
            if lo_range <= s <= hi_range:
                by_score.setdefault(s, []).extend(info["trades"])

        adj_rows = ""
        for s in sorted(by_score.keys(), reverse=True):
            st2 = _score_stats(by_score[s])
            if not st2:
                continue
            wr2   = st2["wins"] / st2["n"] * 100
            pf2   = st2["gp"] / st2["gl"] if st2["gl"] > 0 else (float("inf") if st2["gp"] > 0 else 0.0)
            pf2_s = "∞" if pf2 == float("inf") else f"{pf2:.2f}"
            avg2  = st2["pnl"] / st2["n"]
            wrc2  = "#4ade80" if wr2 >= 55 else ("#fbbf24" if wr2 >= 45 else "#f87171")
            pfc2  = "#4ade80" if pf2 >= 1.5 else ("#fbbf24" if pf2 >= 1.0 else "#f87171")
            hl    = ' style="background:#1e3a2f;font-weight:700;"' if s == score_filter else ""
            mark  = " ◀" if s == score_filter else ""
            adj_rows += f"""<tr{hl}>
  <td style="text-align:center;color:#60a5fa">{s}{mark}</td>
  <td style="text-align:right">{st2['n']}</td>
  <td style="text-align:right;color:{wrc2}">{wr2:.1f}%</td>
  <td style="text-align:right;color:{pfc2}">{pf2_s}</td>
  <td style="text-align:right;color:{'#4ade80' if st2['pnl']>=0 else '#f87171'}">{st2['pnl']:+,.0f}円</td>
  <td style="text-align:right;color:{'#4ade80' if avg2>=0 else '#f87171'}">{avg2:+,.0f}円</td>
</tr>"""

        adj_table = ""
        if adj_rows:
            adj_table = f"""
<h2>スコア {lo_range}〜{hi_range} の詳細（◀ = 指定スコア）</h2>
<table style="max-width:600px">
  <thead><tr>
    <th>スコア</th><th>取引数</th><th>勝率</th><th>PF</th><th>合計損益</th><th>平均損益</th>
  </tr></thead>
  <tbody>{adj_rows}</tbody>
</table>
<p class="footnote">全WATCHLIST / 直近365日バックテスト</p>"""

        # ── スコア単体の取引明細テーブル ─────────────────────────────────
        def _rhtml(reason):
            if reason == "目標達成":   return '<span style="color:#4ade80;font-weight:600">目標達成</span>'
            if reason == "損切り":     return '<span style="color:#f87171;font-weight:600">損切り</span>'
            if reason == "タイムカット": return '<span style="color:#94a3b8">タイムカット</span>'
            return f'<span style="color:#fbbf24">{reason}</span>'

        detail_trades = []
        seen_detail: set = set()
        for info in all_trade_infos:
            if info["score"] != score_filter:
                continue
            for t in info["trades"]:
                if t.get("exit_dt") is None:
                    continue
                sig_dt = t.get("signal_dt")
                dk = (info["sym"], info["strat"], sig_dt)
                if dk in seen_detail:
                    continue
                seen_detail.add(dk)
                exit_d = t["exit_dt"].date() if hasattr(t["exit_dt"], "date") else t["exit_dt"]
                entry_d = t["entry_dt"].date() if hasattr(t.get("entry_dt"), "date") else t.get("entry_dt")
                detail_trades.append({
                    "sym":       info["sym"],
                    "name":      info["name"],
                    "strat":     info["strat"],
                    "exit_d":    exit_d,
                    "entry_d":   entry_d,
                    "entry_p":   t.get("entry_p", 0),
                    "exit_p":    t.get("exit_p", 0),
                    "qty":       t.get("qty", 0),
                    "hold":      t.get("hold_days", 0),
                    "pnl":       t.get("pnl", 0),
                    "reason":    t.get("reason", "") or "—",
                    "cfg_label": info.get("cfg_label", ""),
                    "cfg_color": info.get("cfg_color", "#64748b"),
                })
        detail_trades.sort(key=lambda x: x["exit_d"], reverse=True)

        detail_html = ""
        if detail_trades:
            det_rows = ""
            for dt in detail_trades:
                tpc = "profit" if dt["pnl"] > 0 else "loss"
                tag = f'<span class="tag tag-{dt["strat"].lower()}">{dt["strat"]}</span>'
                cfg_c = dt.get("cfg_color", "#64748b")
                cfg_l = dt.get("cfg_label", "")
                cfg_badge = f'<span style="background:{cfg_c};color:#0f172a;font-size:0.68rem;font-weight:700;padding:1px 6px;border-radius:3px;white-space:nowrap">{cfg_l}</span>'
                det_rows += f"""<tr>
  <td style="color:#94a3b8">{dt['exit_d']}</td>
  <td class="sym" style="text-align:left">{dt['sym']}<br>
    <span style="color:#64748b;font-size:0.75rem">{dt['name']}</span></td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">{cfg_badge}</td>
  <td style="text-align:right">{dt['entry_p']:,.0f}</td>
  <td style="text-align:right">{dt['exit_p']:,.0f}</td>
  <td style="text-align:right">{dt.get('qty', 0)}株</td>
  <td style="text-align:right">{dt['hold']}日</td>
  <td class="{tpc}" style="text-align:right;font-weight:600">{dt['pnl']:+,.0f}円</td>
  <td>{_rhtml(dt['reason'])}</td>
  <td style="color:#64748b;font-size:0.78rem">{dt['entry_d']}</td>
</tr>"""
            detail_html = f"""
<h2>スコア {score_filter} の取引明細（決済日降順 / {len(detail_trades)}件）</h2>
<table>
  <thead><tr>
    <th>決済日</th>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th>設定</th>
    <th>約定値</th><th>決済値</th><th>株数</th><th>保有</th>
    <th>損益</th><th>理由</th><th>エントリー日</th>
  </tr></thead>
  <tbody>{det_rows}</tbody>
</table>"""

        score_section = focus_html + adj_table + detail_html

    else:
        # ── score_filter なし: バンド別テーブル ──────────────────────────
        _score_buckets = [
            (90, 100, "90-100", "#4ade80"),
            (80,  90, "80-89",  "#86efac"),
            (70,  80, "70-79",  "#60a5fa"),
            (60,  70, "60-69",  "#93c5fd"),
            (50,  60, "50-59",  "#fbbf24"),
            (40,  50, "40-49",  "#fcd34d"),
            (30,  40, "30-39",  "#f87171"),
            ( 0,  30, "0-29",   "#94a3b8"),
        ]
        band_rows = ""
        total_n = total_w = total_pnl = 0
        for lo, hi, lbl_s, col in _score_buckets:
            _bseen: set = set()
            bucket = []
            for info in all_trade_infos:
                if not (lo <= info["score"] < hi):
                    continue
                for t in info["trades"]:
                    _bk = (info["sym"], info["strat"], t.get("signal_dt"))
                    if _bk in _bseen:
                        continue
                    _bseen.add(_bk)
                    bucket.append(t)
            st = _score_stats(bucket)
            if not st:
                continue
            wr_v = st["wins"] / st["n"] * 100
            pf   = st["gp"] / st["gl"] if st["gl"] > 0 else (float("inf") if st["gp"] > 0 else 0.0)
            pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
            avg  = st["pnl"] / st["n"]
            wrc  = "#4ade80" if wr_v >= 55 else ("#fbbf24" if wr_v >= 45 else "#f87171")
            pfc  = "#4ade80" if pf >= 1.5  else ("#fbbf24" if pf >= 1.0  else "#f87171")
            hl   = ' style="background:#1e3a2f;"' if min_score > 0 and lo >= min_score else ""
            band_rows += f"""<tr{hl}>
  <td style="color:{col};font-weight:600;text-align:left">{lbl_s}</td>
  <td style="text-align:right">{st['n']}</td>
  <td style="text-align:right;color:{wrc};font-weight:700">{wr_v:.1f}%</td>
  <td style="text-align:right;color:{pfc}">{pf_s}</td>
  <td style="text-align:right;color:{'#4ade80' if st['pnl']>=0 else '#f87171'}">{st['pnl']:+,.0f}円</td>
  <td style="text-align:right;color:{'#4ade80' if avg>=0 else '#f87171'}">{avg:+,.0f}円</td>
</tr>"""
            total_n += st["n"]; total_w += st["wins"]; total_pnl += st["pnl"]

        if band_rows:
            total_wr = total_w / total_n * 100 if total_n else 0
            hl_note  = f'（ハイライト = スコア{min_score}点以上）' if min_score > 0 else ""
            score_section = f"""
<h2>スコア帯別 バックテスト勝率（全WATCHLIST / 直近365日） {hl_note}</h2>
<p style="color:#64748b;font-size:0.8rem;margin-bottom:8px">
  スコアを絞って詳細を見るには: <code style="color:#38bdf8">python nikkei_analysis.py --score 80</code>
</p>
<table style="max-width:620px">
  <thead><tr>
    <th style="text-align:left">スコア帯</th>
    <th>取引数</th><th>勝率</th><th>PF</th><th>合計損益</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{band_rows}</tbody>
  <tfoot><tr style="border-top:2px solid #334155;font-weight:700">
    <td style="text-align:left;color:#94a3b8">合計</td>
    <td style="text-align:right">{total_n}</td>
    <td style="text-align:right;color:{'#4ade80' if total_wr>=55 else '#fbbf24'}">{total_wr:.1f}%</td>
    <td></td>
    <td style="text-align:right;color:{'#4ade80' if total_pnl>=0 else '#f87171'}">{total_pnl:+,.0f}円</td>
    <td></td>
  </tr></tfoot>
</table>"""

    sig_label = str(target_date) if target_date else str(_TODAY)
    if not signals:
        note = f"（スコア{min_score}点以上）" if min_score > 0 else ""
        return (score_section +
                f'<div style="color:#64748b;padding:30px;text-align:center">{sig_label} のシグナルなし {note}</div>')

    col_map = {"★★★": "#4ade80", "★★": "#60a5fa", "★": "#fbbf24", "△": "#f87171"}
    rows = ""
    for i, s in enumerate(signals, 1):
        col      = col_map.get(s["rank"], "#94a3b8")
        stop_pct = (s["order_p"] - s["stop_p"])  / s["order_p"] * 100 if s["order_p"] else 0
        tgt_pct  = (s["target_p"] - s["order_p"]) / s["order_p"] * 100 if s["order_p"] else 0
        qty      = _calc_qty(s["order_p"]) if s["order_p"] else 0
        pos_val  = round(s["order_p"] * qty)
        if stop_pct > 15:
            atr_badge = "<span style='background:#ef4444;color:white;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR大</span>"
        elif stop_pct > 10:
            atr_badge = "<span style='background:#f97316;color:white;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR高</span>"
        elif stop_pct > 7:
            atr_badge = "<span style='background:#eab308;color:#111;padding:1px 5px;border-radius:3px;font-size:9px;margin-left:3px'>ATR↑</span>"
        else:
            atr_badge = ""
        tag      = f'<span class="tag tag-{s["strategy"].lower()}">{s["strategy"]}</span>{atr_badge}'
        src_parts = []
        for src in s.get("sources", []):
            lbl, clr = src if isinstance(src, tuple) else (src, "#475569")
            src_parts.append(
                f'<span style="background:{clr};color:#0f172a;font-size:0.65rem;'
                f'font-weight:700;padding:1px 7px;border-radius:3px;white-space:nowrap;'
                f'display:inline-block;margin:1px 2px">{lbl}</span>'
            )
        src_html = "".join(src_parts)
        lim_pct  = (s["limit_p"] - s["order_p"]) / s["order_p"] * 100 if s["order_p"] else 0
        max_exit = str(s["max_exit"]) if s.get("max_exit") else "—"
        rows += f"""<tr>
  <td style="text-align:center;font-weight:700">{i}</td>
  <td class="sym" style="text-align:left">{s["symbol"]}<br>
    <span style="color:#64748b;font-size:0.75rem">{s["name"]}</span><br>
    <span style="display:inline-flex;flex-wrap:wrap;gap:2px;margin-top:3px">{src_html}</span></td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">{ _fmt_score_cell(s, col) }</td>
  <td style="text-align:right;color:#94a3b8">{s.get("signal_date","")}<br><span style="font-size:0.72rem">{s.get("signal_price",0):,.0f}円</span></td>
  <td style="text-align:right;color:#38bdf8;font-weight:700">{s["order_p"]:,.0f}円</td>
  <td style="text-align:right;color:#f59e0b">+{lim_pct:.1f}%<br><span style="font-size:0.72rem">{s["limit_p"]:,.0f}円</span></td>
  <td style="text-align:right;color:#f87171">-{stop_pct:.1f}%<br><span style="font-size:0.72rem">{s["stop_p"]:,.0f}円</span></td>
  <td style="text-align:right;color:#4ade80">+{tgt_pct:.1f}%<br><span style="font-size:0.72rem">{s["target_p"]:,.0f}円</span></td>
  <td style="text-align:right;color:#e2e8f0">{qty}株<br><span style="font-size:0.72rem;color:#94a3b8">{pos_val:,.0f}円</span></td>
  <td style="text-align:center;color:#94a3b8">{s.get("max_hold","—")}日</td>
  <td style="text-align:center;color:#f59e0b">{max_exit}</td>
</tr>"""

    min_note = f"（スコア{min_score}点以上のみ）" if min_score > 0 else ""
    return score_section + f"""
<h2>{sig_label} のシグナル一覧 — スコア降順 {min_note}</h2>
<p style="color:#64748b;font-size:0.82rem;margin-bottom:12px">
  全WATCHLIST {len(all_items)}件から {sig_label} のエントリーシグナルを抽出。スコアが高い順に並んでいます。
</p>
<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:10px">
  ※ 逆指値注文（青）= 翌日高値がこの価格以上になれば発動<br>
  ※ 指値上限（橙）= 逆指値→指値発注時の上限。寄付ギャップがこれ以下なら約定、超えたら不約定
</p>
<table>
  <thead><tr>
    <th>順位</th>
    <th style="text-align:left">銘柄 / スクリプト</th>
    <th>戦略</th><th>スコア</th>
    <th>シグナル日<br>時株価</th>
    <th style="color:#38bdf8">逆指値<br>(トリガー)</th>
    <th style="color:#f59e0b">指値上限<br>(+3%)</th>
    <th>損切り(-)</th><th>目標(+)</th>
    <th>株数<br><small>想定額</small></th>
    <th>最大保有</th><th>最大決済日</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p class="footnote">※ 最大決済日 = シグナル日 + 約定期限3営業日 + 最大保有15日</p>"""


def _tab5_pnl_html(days: int, workers: int, cfg_filter: str | None = None) -> str:
    """タブ5: 直近N日 取引損益レポート。cfg_filter 指定時は対象configのみ表示。"""
    if not _SIGNALS_AVAILABLE:
        return '<p style="color:#64748b;padding:20px">シグナルモジュールが見つかりません</p>'

    from collections import defaultdict
    until = _TODAY
    since = until - timedelta(days=days)

    all_trades: list[dict] = []        # デデュップ済み（総KPI・取引リスト用）
    full_year_trades: list[dict] = []  # デデュップ済み（スコア別実績用）
    cfg_trades_map: dict = {}          # config別取引（サマリーテーブル用・デデュップなし）
    # 取引リスト表示用: 同一 (sym, strat, signal_dt) は最初のconfig分だけ表示
    seen_global: set = set()

    for cfg in _PNL_CONFIGS:
        _set_sig_params(cfg["mode"], cfg.get("sm_tm"))
        items: list[dict] = []
        with _TPE(max_workers=workers) as ex:
            futs = {}
            for sym, name, strat in cfg["stop_wl"]:
                futs[ex.submit(_stop.backtest_one, sym, name, strat)] = None
            for sym, name, strat in cfg["brk_wl"]:
                futs[ex.submit(_brk.backtest_one, sym, name, strat)] = None
            for fut in _asc(futs):
                try:
                    r = fut.result()
                    if r:
                        items.append(r)
                except Exception:
                    pass

        cfg_trades_map[cfg["label"]] = []  # このconfigの取引（重複なし=同一configでの重複のみ除外）
        for it in items:
            sym  = it.get("symbol", "")
            name = it.get("name", "")
            strat = it.get("strategy", "")
            period_results = it.get("period_results", {})
            if not period_results:
                continue
            _get_wf2 = getattr(_stop, "get_wf_score", None)
            wf2 = _get_wf2(sym, strat) if _get_wf2 else None
            rec_score2, rec_rank2 = _stop.calc_recommend_score(period_results)
            if wf2:
                wf_score2, wf_rank_str2 = wf2
                score, rank = wf_score2, wf_rank_str2
                is_wf2 = True
            else:
                wf_score2, wf_rank_str2 = None, None
                score, rank = rec_score2, rec_rank2
                is_wf2 = False
            max_period    = max(period_results.keys())
            trade_log     = period_results[max_period].get("trade_log", [])
            seen: set     = set()
            for t in trade_log:
                exit_dt = t.get("exit_dt")
                if exit_dt is None:
                    continue
                exit_d   = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
                entry_dt = t.get("entry_dt")
                signal_dt = t.get("signal_dt")
                reason = t.get("reason", "") or "保有中"
                key = (sym, strat, entry_dt, exit_dt)
                if key in seen:
                    continue
                seen.add(key)
                base = {"label": cfg["label"], "color": cfg["color"],
                        "symbol": sym, "name": name, "strategy": strat,
                        "score": score, "rank": rank,
                        "is_wf": is_wf2, "wf_score": wf_score2, "rec_score": rec_score2,
                        "exit_d_raw": exit_d, "pnl": t.get("pnl", 0),
                        "reason": reason}
                extra = {
                    "entry_dt":  entry_dt.strftime("%m/%d") if hasattr(entry_dt, "strftime") else str(entry_dt),
                    "exit_dt":   exit_dt.strftime("%m/%d")  if hasattr(exit_dt,  "strftime") else str(exit_dt),
                    "entry_p":   t.get("entry_p", 0),
                    "exit_p":    t.get("exit_p", 0),
                    "qty":       t.get("qty", 0),
                    "hold_days": t.get("hold_days", 0),
                    "reason":    reason,
                }
                # サマリー用: config独立でカウント（発注中・他configとの重複は除外しない）
                if reason != "発注中" and since <= exit_d <= until:
                    cfg_trades_map[cfg["label"]].append({**base, **extra})
                # 取引リスト・総KPI用: 同一シグナルは最初のconfig分だけ
                gkey = (sym, strat, signal_dt)
                if gkey in seen_global:
                    continue
                seen_global.add(gkey)
                # 発注中はスコア帯統計から除外 (未約定のためpnl=0で歪む)
                if reason != "発注中":
                    full_year_trades.append(base)
                # 取引明細テーブルには発注中も表示
                if since <= exit_d <= until:
                    all_trades.append({**base, **extra})

    # reset to conservative
    _stop.STRATEGY_PARAMS.update(_CON_STOP)
    _brk.STRATEGY_PARAMS.update(_CON_BRK)

    # ── cfg_filter: 対象configのみに絞り込み ──
    if cfg_filter:
        all_trades       = [t for t in all_trades       if t.get("label") == cfg_filter]
        full_year_trades = [t for t in full_year_trades if t.get("label") == cfg_filter]
        cfg_trades_map   = {k: [t for t in v if t.get("label") == cfg_filter]
                            for k, v in cfg_trades_map.items()}

    # ── KPI (発注中=未約定は除外) ──
    # all_trades は (sym, strat, signal_dt) で重複除外済み（同一シグナルは最初のconfigのみ）
    kpi_trades = [t for t in all_trades if t.get("reason") != "発注中"]
    n_total = len(kpi_trades)
    n_win   = sum(1 for t in kpi_trades if t["pnl"] > 0)
    pnl_sum = sum(t["pnl"] for t in kpi_trades)
    wr      = n_win / n_total * 100 if n_total else 0.0
    pc      = "profit" if pnl_sum >= 0 else "loss"
    # 設定別単純合計（重複あり）を別途計算 → サマリーテーブルのフッターに使う
    _all_cfg = [t for v in cfg_trades_map.values() for t in v]
    cfg_n_all   = len(_all_cfg)
    cfg_win_all = sum(1 for t in _all_cfg if t["pnl"] > 0)
    cfg_pnl_all = sum(t["pnl"] for t in _all_cfg)
    cfg_gp_all  = sum(t["pnl"] for t in _all_cfg if t["pnl"] > 0)
    cfg_gl_all  = abs(sum(t["pnl"] for t in _all_cfg if t["pnl"] < 0))
    cfg_pf_all  = cfg_gp_all / cfg_gl_all if cfg_gl_all > 0 else (float("inf") if cfg_gp_all > 0 else 0.0)
    cfg_pf_all_s = "∞" if cfg_pf_all == float("inf") else f"{cfg_pf_all:.2f}"
    cfg_lpc_all  = "profit" if cfg_pnl_all >= 0 else "loss"
    # 重複除外合計（= 全体KPIと同じ数値）
    dedup_gp  = sum(t["pnl"] for t in kpi_trades if t["pnl"] > 0)
    dedup_gl  = abs(sum(t["pnl"] for t in kpi_trades if t["pnl"] < 0))
    dedup_pf  = dedup_gp / dedup_gl if dedup_gl > 0 else (float("inf") if dedup_gp > 0 else 0.0)
    dedup_pf_s = "∞" if dedup_pf == float("inf") else f"{dedup_pf:.2f}"
    kpi_html = f"""
<div class="kpi-grid" style="margin-bottom:8px">
  <div class="kpi"><div class="kpi-l">総取引数 ※</div><div class="kpi-v">{n_total}件</div></div>
  <div class="kpi"><div class="kpi-l">勝率</div><div class="kpi-v">{"—" if not n_total else f"{wr:.1f}%"}</div></div>
  <div class="kpi"><div class="kpi-l">合計損益</div><div class="kpi-v {pc}">{"—" if not n_total else f"{pnl_sum:+,.0f}円"}</div></div>
  <div class="kpi"><div class="kpi-l">勝ち/負け</div><div class="kpi-v">{n_win}W / {n_total - n_win}L</div></div>
</div>
<p class="footnote" style="margin-bottom:18px">※ 同一シグナル（銘柄+戦略+シグナル日が同一）は重複除外し1件として集計。設定別サマリーの合計とは異なります。</p>"""

    # ── サマリーテーブル（各configの独立実績、cross-config重複なし）──
    sum_rows = ""
    for cfg in _PNL_CONFIGS:
        lbl    = cfg["label"]
        trades = cfg_trades_map.get(lbl, [])
        n      = len(trades)
        wins   = sum(1 for t in trades if t["pnl"] > 0)
        pnl    = sum(t["pnl"] for t in trades)
        wr_l   = wins / n * 100 if n else 0.0
        gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s   = "∞" if pf == float("inf") else f"{pf:.2f}"
        lpc    = "profit" if pnl >= 0 else "loss"
        dot    = f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{cfg["color"]};margin-right:6px;vertical-align:middle"></span>'
        sum_rows += f"""<tr>
  <td class="sym" style="text-align:left">{dot}{lbl}</td>
  <td>{n}</td><td>{wins}</td>
  <td>{"—" if not n else f"{wr_l:.1f}%"}</td>
  <td>{"—" if not n else pf_s}</td>
  <td class="profit" style="text-align:right">{"—" if not n else f"+{gp:,.0f}円"}</td>
  <td class="loss"   style="text-align:right">{"—" if not n else f"-{gl:,.0f}円"}</td>
  <td class="{lpc}"  style="text-align:right;font-weight:700">{"—" if not n else f"{pnl:+,.0f}円"}</td>
</tr>"""

    # ── スコア細粒度分析 ──
    score_buckets = [
        (90,100,"90-100","#4ade80"),(80,90,"80-89","#86efac"),
        (70,80,"70-79","#60a5fa"),(60,70,"60-69","#93c5fd"),
        (50,60,"50-59","#fbbf24"),(40,50,"40-49","#fcd34d"),
        (30,40,"30-39","#f87171"),(0,30,"0-29","#94a3b8"),
    ]
    dates = [t["exit_d_raw"] for t in full_year_trades if t.get("exit_d_raw")]
    period_note = ""
    if dates:
        d_min, d_max = min(dates), max(dates)
        period_note = f"{d_min} 〜 {d_max} / {len(full_year_trades)}取引"
    def _band_stats(trades):
        n    = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        pnl  = sum(t["pnl"] for t in trades)
        gp   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        avg  = pnl / n if n else 0
        return n, wins, pnl, gp, gl, pf, avg

    fine_rows = ""
    for lo, hi, lbl_s, col in score_buckets:
        tr = [t for t in full_year_trades if t.get("score") is not None and lo <= t["score"] < hi]
        n  = len(tr)
        if not n:
            continue
        n, wins, pnl, gp, gl, pf, avg = _band_stats(tr)
        pf_s   = "∞" if pf == float("inf") else f"{pf:.2f}"
        wr_s   = wins / n * 100
        lpc    = "profit" if pnl >= 0 else "loss"
        apc    = "profit" if avg >= 0 else "loss"
        border_style = "border-top:2px solid #334155;" if lo in (40,60,80) else ""
        fine_rows += f"""<tr style="{border_style}">
  <td style="color:{col};font-weight:700;text-align:left">{lbl_s}</td>
  <td style="font-weight:700">{n}</td>
  <td style="font-weight:700">{wr_s:.1f}%</td>
  <td style="font-weight:700">{pf_s}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{gp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{gl:,.0f}円</td>
  <td class="{lpc}"  style="text-align:right;font-weight:700">{pnl:+,.0f}円</td>
  <td class="{apc}"  style="text-align:right;font-weight:700">{avg:+,.0f}円</td>
</tr>"""
        # スクリプト別内訳（cfgの順序で表示）
        for cfg in _PNL_CONFIGS:
            sub = [t for t in tr if t.get("label") == cfg["label"]]
            if not sub:
                continue
            sn, sw, sp, sgp, sgl, spf, savg = _band_stats(sub)
            spf_s = "∞" if spf == float("inf") else f"{spf:.2f}"
            swr   = sw / sn * 100
            slpc  = "profit" if sp >= 0 else "loss"
            sapc  = "profit" if savg >= 0 else "loss"
            dot   = f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:{cfg["color"]};margin-right:5px;vertical-align:middle"></span>'
            fine_rows += f"""<tr style="background:#0f172a">
  <td style="text-align:left;padding-left:20px;color:#94a3b8;font-size:0.8rem">{dot}{cfg["label"]}</td>
  <td style="color:#94a3b8;font-size:0.8rem">{sn}</td>
  <td style="color:#94a3b8;font-size:0.8rem">{swr:.1f}%</td>
  <td style="color:#94a3b8;font-size:0.8rem">{spf_s}</td>
  <td class="profit" style="text-align:right;font-size:0.8rem">+{sgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-size:0.8rem">-{sgl:,.0f}円</td>
  <td class="{slpc}" style="text-align:right;font-size:0.8rem">{sp:+,.0f}円</td>
  <td class="{sapc}" style="text-align:right;font-size:0.8rem">{savg:+,.0f}円</td>
</tr>"""

    # ── 取引明細テーブル ──
    col_map = {"★★★": "#4ade80", "★★": "#60a5fa", "★": "#fbbf24", "△": "#f87171"}
    def _rhtml(reason):
        if reason == "目標達成": return '<span style="color:#4ade80;font-weight:600">目標達成</span>'
        if reason == "損切り":   return '<span style="color:#f87171;font-weight:600">損切り</span>'
        if reason == "タイムカット": return '<span style="color:#94a3b8">タイムカット</span>'
        return f'<span style="color:#fbbf24">{reason}</span>'

    # 発注中を先頭に、それ以外は決済日降順
    pending_trades = [t for t in all_trades if t.get("reason") == "発注中"]
    done_trades    = [t for t in all_trades if t.get("reason") != "発注中"]
    sorted_trades  = pending_trades + sorted(done_trades, key=lambda x: x["exit_d_raw"], reverse=True)

    trade_rows = ""
    for t in sorted_trades:
        is_pending = t.get("reason") == "発注中"
        tpc = "profit" if t["pnl"] > 0 else ("" if is_pending else "loss")
        tag = f'<span class="tag tag-{t["strategy"].lower()}">{t["strategy"]}</span>'
        sc  = t.get("score"); rk = t.get("rank")
        if sc is not None and rk and rk != "-":
            _col = col_map.get(rk, "#94a3b8")
            sc_html = _fmt_score_cell(t, _col)
        else:
            sc_html = ""
        row_style = ' style="opacity:0.7;border-left:3px solid #fbbf24"' if is_pending else ""
        pnl_cell  = '—' if is_pending else f'{t["pnl"]:+,.0f}円'
        cfg_color = t.get("color", "#64748b")
        cfg_label = t.get("label", "")
        cfg_badge = f'<span style="background:{cfg_color};color:#0f172a;font-size:0.68rem;font-weight:700;padding:1px 6px;border-radius:3px;white-space:nowrap">{cfg_label}</span>'
        trade_rows += f"""<tr{row_style}>
  <td>{t["exit_dt"]}</td>
  <td class="sym" style="text-align:left">{t["symbol"]} {sc_html}<br><span style="color:#64748b;font-size:0.75rem">{t["name"]}</span></td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">{cfg_badge}</td>
  <td style="text-align:right">{t["entry_p"]:,.0f}</td>
  <td style="text-align:right">{t["exit_p"]:,.0f}</td>
  <td style="text-align:right">{t.get("qty", 0)}株</td>
  <td style="text-align:right">{t["hold_days"]}日</td>
  <td class="{tpc}" style="text-align:right">{pnl_cell}</td>
  <td>{_rhtml(t["reason"])}</td>
  <td style="color:#94a3b8">{t["entry_dt"]}</td>
</tr>"""
    if not trade_rows:
        trade_rows = f'<tr><td colspan="11" style="text-align:center;color:#64748b;padding:16px">直近{days}日に決済した取引なし</td></tr>'

    return f"""
<h2>直近{days}日 取引損益 <span style="font-size:0.8rem;color:#64748b;font-weight:400">（{since} 〜 {until}）</span></h2>
{kpi_html}

<h2>スクリプト別サマリー</h2>
<table>
  <thead><tr>
    <th style="text-align:left">スクリプト</th>
    <th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th>
  </tr></thead>
  <tbody>{sum_rows}
<tr style="border-top:1px dashed #475569;background:#1a2435">
  <td style="text-align:left;color:#94a3b8;font-size:0.82rem;font-style:italic">設定別合計（重複あり）</td>
  <td style="color:#94a3b8">{cfg_n_all}</td>
  <td style="color:#94a3b8">{cfg_win_all}</td>
  <td style="color:#94a3b8">{"—" if not cfg_n_all else f"{cfg_win_all/cfg_n_all*100:.1f}%"}</td>
  <td style="color:#94a3b8">{cfg_pf_all_s}</td>
  <td class="profit" style="text-align:right;color:#94a3b8">+{cfg_gp_all:,.0f}円</td>
  <td class="loss"   style="text-align:right;color:#94a3b8">-{cfg_gl_all:,.0f}円</td>
  <td class="{cfg_lpc_all}" style="text-align:right;color:#94a3b8">{cfg_pnl_all:+,.0f}円</td>
</tr>
<tr style="border-top:2px solid #3b82f6;background:#0d1424">
  <td style="text-align:left;color:#60a5fa;font-weight:700">▶ 合計（重複除外・実取引ベース）</td>
  <td style="color:#60a5fa;font-weight:700">{n_total}</td>
  <td style="color:#60a5fa;font-weight:700">{n_win}</td>
  <td style="color:#60a5fa;font-weight:700">{"—" if not n_total else f"{n_win/n_total*100:.1f}%"}</td>
  <td style="color:#60a5fa;font-weight:700">{dedup_pf_s}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{dedup_gp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{dedup_gl:,.0f}円</td>
  <td class="{"profit" if pnl_sum >= 0 else "loss"}" style="text-align:right;font-weight:700">{pnl_sum:+,.0f}円</td>
</tr>
</tbody>
</table>

<h2>スコア別実績（365日全取引 / {period_note}）</h2>
<table>
  <thead><tr>
    <th style="text-align:left">スコア帯</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{fine_rows}</tbody>
</table>
<p class="footnote">境界線 = ランク区切り（△/★/★★/★★★）</p>

<h2>取引明細（決済日降順）</h2>
<table>
  <thead><tr>
    <th>決済日</th>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th>設定</th>
    <th>約定値</th><th>決済値</th><th>株数</th><th>保有</th>
    <th>損益</th><th>理由</th><th>エントリー</th>
  </tr></thead>
  <tbody>{trade_rows}</tbody>
</table>"""


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

/* P&L / Signal tabs */
.profit { color:#4ade80; }
.loss   { color:#f87171; }
.tag { display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }
.tag-macd { background:#1d4ed8; color:#bfdbfe; }
.tag-a7   { background:#065f46; color:#a7f3d0; }
.tag-rsi2 { background:#7c3aed; color:#ddd6fe; }
.tag-don  { background:#0f766e; color:#99f6e4; }
.tag-vol  { background:#b45309; color:#fde68a; }
.tag-mom  { background:#be185d; color:#fbcfe8; }
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
               years: int, ref_date, indicators: dict | None = None,
               tab4_html: str = "", tab5_html: str = "",
               wf_banner: str = "") -> str:
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

    extra_btns = ""
    extra_panes = ""
    if tab4_html:
        extra_btns  += '\n  <button class="tab-btn" data-tab="t4" onclick="switchTab(\'t4\')">📋 シグナル</button>'
        extra_panes += f'\n<div id="t4" class="tab-pane">{tab4_html}</div>'
    if tab5_html:
        extra_btns  += '\n  <button class="tab-btn" data-tab="t5" onclick="switchTab(\'t5\')">💹 損益</button>'
        extra_panes += f'\n<div id="t5" class="tab-pane">{tab5_html}</div>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日経平均 総合分析 — {ref_str}</title>
<style>{CSS}</style>
</head>
<body>
{wf_banner}<h1>日経平均 総合分析レポート{past_badge}</h1>
<p class="subtitle">
  基準日: {ref_str} ／ 分析期間: {close.index[0].date()} 〜 {ref_str} (過去{years}年) ／
  {ref_str}時点: <strong style="color:{trend_color}">{trend_ja} {r['cur']:,.0f}円</strong>
</p>

<div class="tab-nav">
  <button class="tab-btn active" data-tab="t1" onclick="switchTab('t1')">📊 シグナル判定</button>
  <button class="tab-btn"        data-tab="t2" onclick="switchTab('t2')">📈 トレンド期間</button>
  <button class="tab-btn"        data-tab="t3" onclick="switchTab('t3')">⏱ エントリー分析</button>{extra_btns}
</div>

<div id="t1" class="tab-pane active">{tab1}</div>
<div id="t2" class="tab-pane">{tab2}</div>
<div id="t3" class="tab-pane">{tab3}</div>{extra_panes}

<script>{JS}</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="日経平均 総合分析レポート")
    parser.add_argument("--years",        type=int, default=5,   help="分析期間（年）")
    parser.add_argument("--date",         type=str, default=None, help="基準日 YYYY-MM-DD (省略時=今日)")
    parser.add_argument("--no-browser",   action="store_true",    help="HTML生成のみ")
    parser.add_argument("--no-signals",   action="store_true",    help="タブ4: シグナル一覧を非表示")
    parser.add_argument("--no-pnl",       action="store_true",    help="タブ5: 損益レポートを非表示")
    parser.add_argument("--days",         type=int, default=7,    help="損益集計日数 (--with-pnl 使用時)")
    parser.add_argument("--min-score",    type=int, default=0,    help="シグナルフィルター最低スコア")
    parser.add_argument("--score",        type=int, default=None, help="スコア単体の詳細成績を表示 (例: --score 80)")
    parser.add_argument("--config",       type=str, default=None, help="表示するconfigラベルを絞り込み (例: --config 'v2新WL conservative')")
    parser.add_argument("--workers",      type=int, default=_DEF_WORKERS, help="並列数")
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

    # ── WFスコア再実行チェック ──────────────────────────────────────────────
    wf_status = check_wf_refresh_needed()
    for w in wf_status["warnings"]:
        print(w, flush=True)
    if wf_status["level"] == "ok" and wf_status["latest_date"]:
        print(
            f"WFスコア: {wf_status['latest_date']} 時点 "
            f"(経過{wf_status['days_elapsed']}日 / 次回推奨まで約{wf_status['next_run_in']}日)",
            flush=True,
        )
    wf_banner = _wf_refresh_banner_html(wf_status)

    tab4_html = ""
    tab5_html = ""
    sig_target = ref_date if args.date else None
    if not args.no_signals and _SIGNALS_AVAILABLE:
        date_label = str(ref_date) if args.date else "今日"
        print(f"シグナル収集中 ({date_label})...", flush=True)
        tab4_html = _tab4_signals_html(args.workers, args.min_score,
                                       target_date=sig_target, score_filter=args.score,
                                       cfg_filter=args.config)
    if not args.no_pnl and _SIGNALS_AVAILABLE:
        print(f"損益集計中 (直近{args.days}日)...", flush=True)
        tab5_html = _tab5_pnl_html(args.days, args.workers, cfg_filter=args.config)

    html_path = Path(f"nikkei_analysis_{ref_date}.html")
    html_path.write_text(
        build_html(close, trend, r, periods, up_timing, args.years, ref_date,
                   indicators=indicators, tab4_html=tab4_html, tab5_html=tab5_html,
                   wf_banner=wf_banner),
        encoding="utf-8"
    )
    print(f"生成: {html_path}")

    if not args.no_browser:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
