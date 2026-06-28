"""
nikkei_analysis.py  ―  日経平均 総合分析レポート

select_signals.py / analyze_nikkei_trend.py / analyze_trend_timing.py を1本に統合。
日経データを1回だけ取得し、タブ付きHTMLで以下3セクションを生成する。

  タブ1: シグナル判定    — 相場環境 + 今日使うべきスクリプト
  タブ2: トレンド期間    — 上昇/下落/横ばい期間の統計と一覧
  タブ3: エントリー分析  — 上昇何日目に入ると良いか / 生存確率
  タブ4: シグナル一覧    — 全WATCHLISTの今日のシグナルをBTスコア昇順表示 (--with-signals)
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
_last_signals: list[dict] = []   # _tab4_signals_html() 呼び出し後に最新シグナルリストを保持
_FROZEN_BT_SCORES: dict[tuple, int] = {}  # (symbol, strategy) → 初回発信時のBTスコア (外部から注入)
_SIGNAL_DATE_BT_SCORES: dict[tuple, int] = {}  # (symbol, strategy, signal_date_str) → シグナル発生時BTスコア (外部から注入)
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

# ── ショートモード フラグ ─────────────────────────────────────────────────────
# run_signals_holdout_all.py が "--short" で起動した場合に True を設定する。
# _tab5_pnl_html の日経トレンド別成績テーブルでの表示順・凡例切替に使用。
_IS_SHORT_MODE: bool = False

# ── ショートモジュール (guarded: 失敗してもロングに影響しない) ────────────────
# strat名でモジュールを振り分ける (_mod_for)。短期戦略は "_S" で終わる。
_short = None
_sbrk  = None
_CON_SHORT: dict = {}; _AGG_SHORT: dict = {}
_CON_SBRK:  dict = {}; _AGG_SBRK:  dict = {}
try:
    os.environ["TRADING_MODE"] = "conservative"
    import check_signals_short           as _short
    import check_signals_short_breakout  as _sbrk
    _importlib.reload(_short); _importlib.reload(_sbrk)
    _CON_SHORT = _copy.deepcopy(_short.STRATEGY_PARAMS)
    _CON_SBRK  = _copy.deepcopy(_sbrk.STRATEGY_PARAMS)
    os.environ["TRADING_MODE"] = "aggressive"
    _importlib.reload(_short); _importlib.reload(_sbrk)
    _AGG_SHORT = _copy.deepcopy(_short.STRATEGY_PARAMS)
    _AGG_SBRK  = _copy.deepcopy(_sbrk.STRATEGY_PARAMS)
    os.environ["TRADING_MODE"] = "conservative"
    _importlib.reload(_short); _importlib.reload(_sbrk)
    _short.STRATEGY_PARAMS.update(_CON_SHORT)
    _sbrk.STRATEGY_PARAMS.update(_CON_SBRK)
except Exception:
    _short = None
    _sbrk  = None


def _mod_for(strat: str):
    """戦略名から対応するシグナルモジュールを返す (ロング/ショート自動判定)。"""
    if _short is not None and strat in getattr(_short, "STRATEGY_PARAMS", {}):
        return _short
    if _sbrk is not None and strat in getattr(_sbrk, "STRATEGY_PARAMS", {}):
        return _sbrk
    if strat in getattr(_brk, "STRATEGY_PARAMS", {}):
        return _brk
    return _stop


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
    import os as _os
    from pathlib import Path as _P
    # yfinanceのSQLiteキャッシュを削除して常に最新データを取得
    for _cd in [
        _P(_os.environ.get("APPDATA", "")) / "py-yfinance",
        _P.home() / ".cache" / "py-yfinance",
        _P.home() / "AppData" / "Roaming" / "py-yfinance",
    ]:
        if _cd.exists():
            for _f in list(_cd.glob("*.db")) + list(_cd.glob("*.sqlite")):
                try:
                    _f.unlink()
                except Exception:
                    pass

    ticker = yf.Ticker("^N225")
    if end_date is not None:
        start = pd.Timestamp(end_date) - pd.Timedelta(days=years * 365 + 60)
        end   = pd.Timestamp(end_date) + pd.Timedelta(days=2)
        df = ticker.history(start=start, end=end, interval="1d", auto_adjust=True)
    else:
        df = ticker.history(period=f"{years * 365 + 60}d", interval="1d", auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError("日経データ取得失敗")
    close = df["Close"].squeeze()
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.dropna().sort_index()


# トレンド判定パラメータ（調整可）
TREND_MA_SHORT = 5     # 短期MA（小さいほど細かく区切れる）
TREND_MA_LONG  = 10    # 長期MA
TREND_DROP_PCT = -3.0  # 直近5日でこの%以下の下落は MA に関わらず「下落」扱い(V字対策)


def label_trend(close: pd.Series) -> pd.Series:
    """短期MA(5/10)クロス + 急落判定でトレンドラベル付け: 'up' / 'down' / 'sideways'

    - MA10/MA25 から MA5/MA10 に短縮 → 局面の切り替わりを細かく捉える。
    - 直近5日が TREND_DROP_PCT 以下の急落は、MA がラグしても「下落」扱い。
      (急落して戻すV字が「横ばい」に誤分類されるのを防ぐ)
    """
    ma_s = close.rolling(TREND_MA_SHORT).mean()
    ma_l = close.rolling(TREND_MA_LONG).mean()
    ret5 = close.pct_change(5) * 100
    trend = pd.Series("sideways", index=close.index)
    trend[(close > ma_s) & (ma_s > ma_l)] = "up"
    trend[(close < ma_s) & (ma_s < ma_l)] = "down"
    trend[ret5 <= TREND_DROP_PCT] = "down"   # 急落は下落（V字含む）
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
        if is_current:
            import numpy as np
            days = int(np.busday_count(sd, ed))
        else:
            days = end_idx - start_idx
        return {
            "trend": cur_trend, "start": sd, "end": ed,
            "days": days,
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

    if is_current:
        import numpy as np
        days = int(np.busday_count(sd, ed))
    else:
        days = end_idx - start_idx
    periods.append({
        "start_date": sd, "end_date": ed,
        "start_p": sp, "end_p": ep,
        "days": days,
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
    # 現在のモードを環境変数にも反映 (バックテストキャッシュのキー整合のため)。
    # これが無いと con/agg でキャッシュキーが衝突し結果が混ざる。
    os.environ["TRADING_MODE"] = "aggressive" if mode != "conservative" else "conservative"
    if mode == "conservative":
        _stop.STRATEGY_PARAMS.update(_CON_STOP)
        _brk.STRATEGY_PARAMS.update(_CON_BRK)
    else:
        _stop.STRATEGY_PARAMS.update(_AGG_STOP)
        _brk.STRATEGY_PARAMS.update(_AGG_BRK)
    if _short is not None:
        if mode == "conservative":
            _short.STRATEGY_PARAMS.update(_CON_SHORT)
            _sbrk.STRATEGY_PARAMS.update(_CON_SBRK)
        else:
            _short.STRATEGY_PARAMS.update(_AGG_SHORT)
            _sbrk.STRATEGY_PARAMS.update(_AGG_SBRK)
    if sm_tm:
        sm, tm = sm_tm
        for k, v in list(_stop.STRATEGY_PARAMS.items()):
            _stop.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)
        for k, v in list(_brk.STRATEGY_PARAMS.items()):
            _brk.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)
        if _short is not None:
            for k, v in list(_short.STRATEGY_PARAMS.items()):
                _short.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)
            for k, v in list(_sbrk.STRATEGY_PARAMS.items()):
                _sbrk.STRATEGY_PARAMS[k] = (v[0], v[1], sm, tm)


_BT_TYPE_COLORS = {"安定": "#10b981", "高WR": "#3b82f6", "高PF": "#f59e0b", "取引数": "#a855f7"}

def _fmt_score_cell(s: dict, col: str) -> str:
    """シグナルテーブルのスコアセルHTML。WFスコアとBTスコアを両表示。"""
    rank = s["rank"]
    bt_type = s.get("bt_type", "")
    tc = _BT_TYPE_COLORS.get(bt_type, "#94a3b8")
    type_badge = (
        f'<span style="background:{tc}22;color:{tc};padding:1px 5px;'
        f'border-radius:3px;font-size:0.65rem;display:inline-block;margin-top:2px">'
        f'{bt_type}</span>'
    ) if bt_type else ""
    if s.get("is_wf") and s.get("wf_score") is not None:
        rec = s.get("rec_score", "—")
        return (
            f'<span style="color:{col};font-weight:700">WF&nbsp;{s["wf_score"]}</span>'
            f'<span style="font-size:0.68rem;color:#64748b;display:block">{rank} / BT:{rec}</span>'
            f'{type_badge}'
        )
    else:
        return (
            f'<span style="color:{col};font-weight:700">{rank}&nbsp;{s["score"]}</span>'
            f'<br><span style="font-size:0.68rem;color:#f59e0b">BT(参考)</span>'
            f'<br>{type_badge}'
        )


def _tab4_signals_html(workers: int, min_score: int = 0, target_date=None,
                       score_filter: int | None = None,
                       cfg_filter: str | None = None) -> str:
    """タブ4: 全WATCHLISTのシグナルをBTスコア昇順表示。target_date=None で今日。
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
            mod = _mod_for(strat)
            bt = mod.backtest_one(sym, name, strat)
            if not bt:
                return None
            # おすすめスコアは常に計算
            rec_score, rec_rank = _stop.calc_recommend_score(bt["period_results"])
            # 初回発信時スコアが凍結されていればそちらを使用 (BTスコアの日次変動を抑制)
            _fz = _FROZEN_BT_SCORES.get((sym, strat))
            if _fz is not None:
                rec_score = _fz
                rec_rank  = ("★★★" if _fz >= 80 else "★★" if _fz >= 60
                             else "★" if _fz >= 40 else "△")
            _bt_type_fn = getattr(_stop, "calc_bt_type", None)
            bt_type = _bt_type_fn(bt["period_results"]) if _bt_type_fn else "?"
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
            # period_results が空の場合 (全エントリーが "発注中") は空リストで続行
            if bt["period_results"]:
                max_period = max(bt["period_results"].keys())
                trade_log  = bt["period_results"][max_period].get("trade_log", [])
            else:
                trade_log  = []
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
            _is_short_sig = str(strat).upper().endswith("_S")
            _lim_mult = (1.0 - 0.03) if _is_short_sig else (1.0 + 0.03)
            limit_p = sig.get("limit_entry_price", round(order_p * _lim_mult) if order_p else 0)
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
                    "rec_score": rec_score, "bt_type": bt_type,
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

    signals.sort(key=lambda x: -(x.get("rec_score") or 0))
    _last_signals.clear()
    _last_signals.extend(signals)
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
  <td style="text-align:right;color:#4ade80">+{st2['gp']:,.0f}円</td>
  <td style="text-align:right;color:#f87171">-{st2['gl']:,.0f}円</td>
  <td style="text-align:right;color:{'#4ade80' if st2['pnl']>=0 else '#f87171'}">{st2['pnl']:+,.0f}円</td>
  <td style="text-align:right;color:{'#4ade80' if avg2>=0 else '#f87171'}">{avg2:+,.0f}円</td>
</tr>"""

        adj_table = ""
        if adj_rows:
            adj_table = f"""
<h2>スコア {lo_range}〜{hi_range} の詳細（◀ = 指定スコア）</h2>
<table style="max-width:600px">
  <thead><tr>
    <th>スコア</th><th>取引数</th><th>勝率</th><th>PF</th><th style="color:#4ade80">利益</th><th style="color:#f87171">損失</th><th>合計損益</th><th>平均損益</th>
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
    <th>約定値</th><th>決済値</th><th>保有</th>
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
        total_n = total_w = total_pnl = total_gp = total_gl = 0
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
  <td style="text-align:right;color:#4ade80">+{st['gp']:,.0f}円</td>
  <td style="text-align:right;color:#f87171">-{st['gl']:,.0f}円</td>
  <td style="text-align:right;color:{'#4ade80' if st['pnl']>=0 else '#f87171'}">{st['pnl']:+,.0f}円</td>
  <td style="text-align:right;color:{'#4ade80' if avg>=0 else '#f87171'}">{avg:+,.0f}円</td>
</tr>"""
            total_n += st["n"]; total_w += st["wins"]; total_pnl += st["pnl"]
            total_gp += st["gp"]; total_gl += st["gl"]

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
    <th>取引数</th><th>勝率</th><th>PF</th><th style="color:#4ade80">利益</th><th style="color:#f87171">損失</th><th>合計損益</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{band_rows}</tbody>
  <tfoot><tr style="border-top:2px solid #334155;font-weight:700">
    <td style="text-align:left;color:#94a3b8">合計</td>
    <td style="text-align:right">{total_n}</td>
    <td style="text-align:right;color:{'#4ade80' if total_wr>=55 else '#fbbf24'}">{total_wr:.1f}%</td>
    <td></td>
    <td style="text-align:right;color:#4ade80">+{total_gp:,.0f}円</td>
    <td style="text-align:right;color:#f87171">-{total_gl:,.0f}円</td>
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
    try:
        from signal_risk_check import (
            render_risk_badges   as _rrb_fn,
            render_earnings_date as _red_fn,
        )
    except Exception:
        def _rrb_fn(sym):         return ""
        def _red_fn(sym, td=None): return ""
    rows = ""
    for i, s in enumerate(signals, 1):
        col      = col_map.get(s["rank"], "#94a3b8")
        stop_pct = (s["order_p"] - s["stop_p"])  / s["order_p"] * 100 if s["order_p"] else 0
        tgt_pct  = (s["target_p"] - s["order_p"]) / s["order_p"] * 100 if s["order_p"] else 0
        qty      = _calc_qty(s["order_p"], s["stop_p"]) if s["order_p"] else 0
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
        src_html  = "".join(src_parts)
        risk_html = _rrb_fn(s["symbol"])
        earn_html = _red_fn(s["symbol"], target_date)
        _sig_is_short = str(s["strategy"]).upper().endswith("_S")
        lim_pct  = (s["limit_p"] - s["order_p"]) / s["order_p"] * 100 if s["order_p"] else 0
        max_exit = str(s["max_exit"]) if s.get("max_exit") else "—"
        # 📥 登録 / 🚀 発注ボタン: position_server (8765) と連携
        _side   = "short" if str(s["strategy"]).upper().endswith("_S") else "long"
        _scode  = str(s["symbol"]).split(".")[0]
        _reg_url = (f"http://127.0.0.1:8765/?prefill=1"
                    f"&symbol={_scode}"
                    f"&entry={s['order_p']:.0f}"
                    f"&stop={s['stop_p']:.0f}"
                    f"&target={s['target_p']:.0f}"
                    f"&strategy={s['strategy']}"
                    f"&qty={qty}"
                    f"&side={_side}")
        # 🚀発注: このタブから fetch で /order に発注リクエスト（確認ダイアログ付き）
        _ord_btn = (f"<button type=\"button\" "
                    f"onclick=\"gobotOrder('{_scode}','{_side}','{s['strategy']}',"
                    f"{s['order_p']:.0f},{s['stop_p']:.0f},{s['target_p']:.0f},{qty})\" "
                    f"style=\"display:inline-block;padding:4px 8px;background:#dc2626;"
                    f"color:#fff;border:none;border-radius:5px;font-size:12px;cursor:pointer;"
                    f"white-space:nowrap;margin-bottom:3px\">🚀 発注</button>")
        _reg_link = (f'<a href="{_reg_url}" target="_blank" '
                     f'style="display:inline-block;padding:4px 8px;background:#2d6cdf;'
                     f'color:#fff;border-radius:5px;font-size:12px;text-decoration:none;'
                     f'white-space:nowrap">📥 登録</a>')
        _reg_btn = f'<div style="display:flex;flex-direction:column;gap:2px;align-items:center">{_ord_btn}{_reg_link}</div>'
        rows += f"""<tr>
  <td style="text-align:center;font-weight:700">{i}</td>
  <td class="sym" style="text-align:left">{s["symbol"]}<br>
    <span style="color:#64748b;font-size:0.75rem">{s["name"]}</span>{earn_html}<br>
    <span style="display:inline-flex;flex-wrap:wrap;gap:2px;margin-top:3px">{src_html}{risk_html}</span></td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">{ _fmt_score_cell(s, col) }</td>
  <td style="text-align:right;color:#94a3b8">{s.get("signal_date","")}<br><span style="font-size:0.72rem">{s.get("signal_price",0):,.0f}円</span></td>
  <td style="text-align:right;color:#38bdf8;font-weight:700">{s["order_p"]:,.0f}円</td>
  <td style="text-align:right;color:#f59e0b">{lim_pct:+.1f}%<br><span style="font-size:0.72rem">{s["limit_p"]:,.0f}円</span></td>
  <td style="text-align:right;color:#f87171">-{stop_pct:.1f}%<br><span style="font-size:0.72rem">{s["stop_p"]:,.0f}円</span></td>
  <td style="text-align:right;color:#4ade80">+{tgt_pct:.1f}%<br><span style="font-size:0.72rem">{s["target_p"]:,.0f}円</span></td>
  <td style="text-align:right;color:#e2e8f0">{qty}株<br><span style="font-size:0.72rem;color:#94a3b8">{pos_val:,.0f}円</span></td>
  <td style="text-align:center;color:#94a3b8">{s.get("max_hold","—")}日</td>
  <td style="text-align:center;color:#f59e0b">{max_exit}</td>
  <td style="text-align:center">{_reg_btn}</td>
</tr>"""

    min_note = f"（スコア{min_score}点以上のみ）" if min_score > 0 else ""
    _order_js = """
<script>
function gobotOrder(sym, side, strat, entry, stop, target, qty){
  var lbl = (side==='short') ? ('逆指値売り(信用新規) @\\u2264'+entry) : ('逆指値買い @\\u2265'+entry);
  if(!confirm('\\u3010\\u767a\\u6ce8\\u78ba\\u8a8d\\u3011\\n'+sym+' '+strat+' ('+side+')\\n'+lbl
      +'\\n\\u682a\\u6570: '+qty+'\\u682a\\n\\n\\u767a\\u6ce8\\u30b5\\u30fc\\u30d0(order_server:8765)\\u3078\\u767a\\u6ce8\\u30ea\\u30af\\u30a8\\u30b9\\u30c8\\u3092\\u9001\\u308a\\u307e\\u3059\\u3002'
      +'\\n\\u5b9f\\u767a\\u6ce8/dry-run\\u30fb\\u30c7\\u30e2/\\u672c\\u756a\\u306f\\u30b5\\u30fc\\u30d0\\u8d77\\u52d5\\u30aa\\u30d7\\u30b7\\u30e7\\u30f3\\u306b\\u5f93\\u3044\\u307e\\u3059\\u3002\\n\\n\\u3088\\u308d\\u3057\\u3044\\u3067\\u3059\\u304b\\uff1f')) return;
  var body = new URLSearchParams({symbol:sym,entry:entry,stop:stop,target:target,
                                  strategy:strat,side:side,qty:qty});
  fetch('http://127.0.0.1:8765/order',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})
    .then(function(r){return r.text();})
    .then(function(t){alert(t);})
    .catch(function(e){alert('\\u767a\\u6ce8\\u5931\\u6557: \\u767a\\u6ce8\\u30b5\\u30fc\\u30d0\\u3092\\u8d77\\u52d5\\u3057\\u3066\\u304f\\u3060\\u3055\\u3044\\n  python order_server.py\\n'+e);});
}
</script>
"""
    return score_section + _order_js + f"""
<h2>{sig_label} のシグナル一覧 — BTスコア降順 {min_note}</h2>
<p style="color:#64748b;font-size:0.82rem;margin-bottom:12px">
  全WATCHLIST {len(all_items)}件から {sig_label} のエントリーシグナルを抽出。BTスコアが高い順に並んでいます。
</p>
<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:10px">
  ※ 逆指値注文（青）= ロング:翌日高値がこの価格以上で発動 / ショート:翌日安値がこの価格以下で発動<br>
  ※ 指値（橙）= ロング:上限(+3%) ギャップアップが大きすぎたらキャンセル / ショート:下限(-3%) ギャップダウンが大きすぎたらキャンセル
</p>
<table>
  <thead><tr>
    <th>順位</th>
    <th style="text-align:left">銘柄 / スクリプト</th>
    <th>戦略</th><th>スコア</th>
    <th>シグナル日<br>時株価</th>
    <th style="color:#38bdf8">逆指値<br>(トリガー)</th>
    <th style="color:#f59e0b">指値上限/下限<br>(±3%)</th>
    <th>損切り(-)</th><th>目標(+)</th>
    <th>株数<br><small>想定額</small></th>
    <th>最大保有</th><th>最大決済日</th><th>登録</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p class="footnote">※ 最大決済日 = シグナル日 + 約定期限3営業日 + 最大保有15日</p>"""


_DETAIL_TAB_SEQ = 0  # 取引明細タブの DOM id 衝突回避用カウンタ

_OOS_BT_SCORES: dict = {}  # (sym, strat) -> rec_score, populated by _tab5_pnl_html
_pnl_bt_cache: dict = {}         # cfg_key -> items_per_cfg (バックテスト結果キャッシュ)
_preoos_tab5_score_cache: dict = {}  # (sym, strat, cutoff_days) -> score


def _calc_preoos_bt_score_for_tab5(sym: str, strat: str, cutoff_days: int) -> int:
    """cutoff_date=today-cutoff_days 以前のデータのみでBTスコアを計算（バイアスフリー）。
    メイン損益タブのOOS前BTスコアフィルタ用。計算結果はモジュール内キャッシュに保持。"""
    key = (sym, strat, cutoff_days)
    if key in _preoos_tab5_score_cache:
        return _preoos_tab5_score_cache[key]
    try:
        _mod = _mod_for(strat)
        calc_fn, em, sm, tm = _mod.STRATEGY_PARAMS[strat]
        total_fetch = cutoff_days + 365 + 60
        df_full = _mod.fetch(sym, total_fetch)
        if df_full is None or df_full.empty:
            _preoos_tab5_score_cache[key] = 0
            return 0
        from backtest_limit_entry import _TODAY as _blt_today, run_limit_backtest as _rbt
        cutoff_dt = _blt_today - timedelta(days=cutoff_days)
        df_pre = df_full[df_full.index <= pd.Timestamp(cutoff_dt)].copy()
        if len(df_pre) < 30:
            _preoos_tab5_score_cache[key] = 0
            return 0
        bt_days = cutoff_days + 365
        full_r = _rbt(sym, "", df_pre, calc_fn, em, sm, tm, bt_days, strat,
                      entry_type=_mod.ENTRY_TYPE)
        if not full_r or not full_r.get("trade_log"):
            _preoos_tab5_score_cache[key] = 0
            return 0
        _SLICE_PERIODS = [30, 60, 90, 120, 150, 180]
        period_results: dict = {}
        for p in _SLICE_PERIODS:
            slice_since = cutoff_dt - timedelta(days=p)
            sub = [t for t in full_r["trade_log"]
                   if t.get("signal_dt") and t["signal_dt"].date() >= slice_since
                   and t.get("reason") not in ("発注中", "保有中")]
            if not sub:
                continue
            wins = sum(1 for t in sub if t["pnl"] > 0)
            gp = sum(t["pnl"] for t in sub if t["pnl"] > 0)
            gl = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
            pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
            period_results[p] = {
                "trades": len(sub), "wins": wins,
                "win_rate": wins / len(sub) * 100,
                "pf": pf, "total_pnl": sum(t["pnl"] for t in sub),
            }
        score, _ = _stop.calc_recommend_score(period_results)
        _preoos_tab5_score_cache[key] = score
        return score
    except Exception:
        _preoos_tab5_score_cache[key] = 0
        return 0


def _wf_history_html(wf_until_date, workers: int, max_price: float = 0.0,
                     min_price: float = 0.0, universe_path: str | None = None,
                     cache_only: bool = False, _uid: str = "",
                     force: bool = False) -> tuple:
    """WF歴史検証HTML（ユニバース全体からの新規銘柄選定版）。

    wf_until_date 時点のデータのみで FOLDS_HISTORICAL（3fold/TRAIN2年/TEST1年）を使い
    ユニバース全体をスキャンして WF 基準に合格した銘柄を新規選定。
    ≥1fold 通過銘柄の wf_until_date〜今日 OOS 成績を表示。
    現行 WATCHLIST/FOLDS/シグナルには一切影響しない。
    WF結果は .wfh_cache/ に日付別でキャッシュされ、2回目以降は即座に表示。

    cache_only=True のとき: キャッシュがなければ (None, {}) を返す（スキャンしない）。
    _uid: HTML要素IDのプレフィックス。複数インスタンスを1ページに埋め込む際に指定。
    """
    import pickle as _pkl
    from pathlib import Path as _PL

    if not _uid:
        import random as _rnd
        _uid = f"w{_rnd.randint(100000, 999999)}"

    if not _SIGNALS_AVAILABLE:
        return '<p style="color:#64748b;padding:20px">シグナルモジュールが見つかりません</p>', {}

    try:
        from scan_walkforward import (walkforward_one_asof as _wf_asof,
                                      FOLDS_HISTORICAL as _HIST_FOLDS,
                                      _HIST_MAX_LOOKBACK as _HML,
                                      load_universe as _load_univ,
                                      STRATEGY_DEFS as _SDEFS_WFH)
    except ImportError as _ie:
        return f'<p style="color:#f87171;padding:20px">scan_walkforward インポートエラー: {_ie}</p>'

    from backtest_limit_entry import fetch as _wfh_fetch, run_limit_backtest as _wfh_rbt, _TODAY as _wfh_today
    from collections import defaultdict
    from datetime import timedelta as _td

    # ── ユニバース読み込み × 全戦略でタスク生成（現行WATCHLISTとは独立）──────────
    try:
        universe_syms, _univ_name = _load_univ(universe_path)
    except RuntimeError as _ue:
        return (f'<p style="color:#f87171;padding:20px">ユニバースファイルが見つかりません: {_ue}<br>'
                f'<code>python fetch_listed_symbols.py --market prime</code> を実行してください</p>')

    _all_strats = list(_SDEFS_WFH.keys())
    all_tasks: list[tuple] = [(_s, _n, _st) for (_s, _n) in universe_syms for _st in _all_strats]

    print(f"[WF歴史検証] ユニバース: {_univ_name} ({len(universe_syms)}銘柄) × {len(_all_strats)}戦略 = {len(all_tasks)}タスク", flush=True)

    # ── WF結果キャッシュ（価格フィルタはキャッシュキーに含めない → ロード後に適用）──
    # 理由: max_price/min_price をキーに含めると、価格範囲を変えるたびに全再スキャンが必要になる。
    # 代わりに全銘柄（価格フィルタなし）でスキャン・キャッシュし、ロード後に絞り込む。
    # これにより同一基準日なら価格範囲変更が即座（再スキャン不要）。
    _wfh_cache_dir = _PL(".wfh_cache")
    _wfh_cache_dir.mkdir(exist_ok=True)
    import hashlib as _hl
    _fold_sig = str(_HIST_FOLDS)
    _strats_sig = ",".join(_all_strats)
    _cache_key  = f"{wf_until_date}_{_univ_name}_{_fold_sig}_{_strats_sig}"
    _cache_hash = _hl.md5(_cache_key.encode()).hexdigest()[:12]
    _cache_path = _wfh_cache_dir / f"wfh_{wf_until_date}_{_cache_hash}.pkl"

    # ── 旧形式キャッシュの自動移行（max_price/min_priceがキーに入っていた旧バージョン）──
    # 旧ファイル名: wfh_{date}_{旧hash}.pkl → 同じ日付の別ハッシュを探して移行
    if not _cache_path.exists():
        _old_candidates = sorted(_wfh_cache_dir.glob(f"wfh_{wf_until_date}_*.pkl"))
        _old_candidates = [p for p in _old_candidates if p != _cache_path]
        if _old_candidates:
            _old_path = _old_candidates[-1]  # 最新の旧キャッシュ
            try:
                with open(_old_path, "rb") as _f:
                    _migrated = _pkl.load(_f)
                # 旧キャッシュを新キーで保存（移行完了）
                with open(_cache_path, "wb") as _f:
                    _pkl.dump(_migrated, _f)
                print(f"[WF歴史検証] 旧キャッシュを移行: {_old_path.name} → {_cache_path.name} ({len(_migrated)}件)", flush=True)
            except Exception as _me:
                print(f"[WF歴史検証] 旧キャッシュ移行失敗: {_me}", flush=True)

    if cache_only and not _cache_path.exists():
        return None, {}

    wf_results_all: list[dict] = []
    _already_done: set = set()
    if _cache_path.exists():
        try:
            with open(_cache_path, "rb") as _f:
                wf_results_all = _pkl.load(_f)
            _already_done = {(r["symbol"], r["strategy"]) for r in wf_results_all}
            print(f"[WF歴史検証] キャッシュ読込: {len(wf_results_all)}件 ({_cache_path.name})", flush=True)
        except Exception:
            wf_results_all = []

    # 未処理タスクのみ実行（中断再開対応）
    _remaining_tasks = [t for t in all_tasks if (t[0], t[2]) not in _already_done]
    if not _remaining_tasks and _already_done:
        print(f"[WF歴史検証] 全{len(all_tasks)}件キャッシュ済み → スキャンをスキップ", flush=True)

    if _remaining_tasks:
        # ── 銘柄データを先にpre-fetch（同一銘柄の重複ダウンロード排除）──────────
        unique_syms = list({(_sym, _nm) for _sym, _nm, _ in _remaining_tasks})
        _since = wf_until_date - _td(days=_HML + 200)
        _fetch_days = int((_wfh_today - _since).days * 1.1) + 60

        print(f"[WF歴史検証] {len(unique_syms)}銘柄の長期データ取得中 ({_since}〜)…", flush=True)

        def _pre_fetch(sym_nm):
            _s, _n = sym_nm
            try:
                _wfh_fetch(_s, _fetch_days, min_start_date=_since)
            except Exception:
                pass

        with _TPE(max_workers=min(workers, 8)) as _ex_pf:
            list(_ex_pf.map(_pre_fetch, unique_syms))

        print(f"[WF歴史検証] {len(_remaining_tasks)}件 WFスキャン中 (as_of={wf_until_date}"
              f"{f' / 残り({len(_remaining_tasks)}/{len(all_tasks)})' if _already_done else ''})…", flush=True)

        def _run_wf_hist(args):
            _s, _n, _st = args
            try:
                # 価格フィルタなしでスキャン（キャッシュを価格非依存にするため）
                r = _wf_asof(_s, _n, _st, wf_until_date, max_price=0)
                return r  # None も含めてそのまま返す（None は latest_price 取得失敗）
            except Exception:
                return None

        _total_for_pct = len(all_tasks)
        with _TPE(max_workers=workers) as _ex:
            _futs = {_ex.submit(_run_wf_hist, t): t for t in _remaining_tasks}
            for _i, _fut in enumerate(_asc(_futs)):
                _r = _fut.result()
                if _r is not None:
                    wf_results_all.append(_r)
                # 200件ごとに中間保存（クラッシュ対策・中断再開対応）
                if (_i + 1) % 200 == 0:
                    try:
                        with open(_cache_path, "wb") as _f:
                            _pkl.dump(wf_results_all, _f)
                        _done_total = len(_already_done) // len(_all_strats) * len(_all_strats) + _i + 1
                        _pct = _done_total / _total_for_pct * 100
                        print(f"[WF歴史検証] 中間保存: {_i+1}/{len(_remaining_tasks)}件 ({_pct:.0f}%) / 通過:{len(wf_results_all)}件", flush=True)
                    except Exception:
                        pass

        # 最終キャッシュ保存（全銘柄・価格フィルタなし）
        try:
            with open(_cache_path, "wb") as _f:
                _pkl.dump(wf_results_all, _f)
        except Exception:
            pass

    print(f"[WF歴史検証] WFスキャン完了: {len(wf_results_all)}件（価格フィルタ前）", flush=True)

    # ── 価格フィルタ（キャッシュとは独立して適用）────────────────────────────
    wf_results = wf_results_all
    if max_price > 0:
        wf_results = [r for r in wf_results if r.get("latest_price", 0) <= max_price]
    if min_price > 0:
        wf_results = [r for r in wf_results if r.get("latest_price", 0) >= min_price]
    print(f"[WF歴史検証] 価格フィルタ後: {len(wf_results)}件"
          f"（{f'≤{max_price:,.0f}円' if max_price else ''}{'・' if max_price and min_price else ''}{f'≥{min_price:,.0f}円' if min_price else ''}）",
          flush=True)

    # ── folds_passed lookup (for adding to OOS trades) ───────────────────────
    _fp_lookup = {(r["symbol"], r["strategy"]): r["folds_passed"] for r in wf_results}

    # ── ≥1fold 通過銘柄の OOS 成績（wf_until_date〜今日）──────────────────────
    _oos_backtest_days = (_wfh_today - wf_until_date).days
    passed_tasks = [(r["symbol"], r["name"], r["strategy"])
                    for r in wf_results if r["folds_passed"] >= 1]

    # OOS前365日のBTスコア計算ヘルパー（calc_recommend_scoreと同一式）
    _BT_SLICE_DAYS = [180, 150, 120, 90, 60, 30]

    def _calc_preoos_bt_score(df_pre, cf, em, sm, tm, st, etype):
        """wf_until_date以前のデータだけで算出したBTスコア（OSSバイアスなし）"""
        if df_pre is None or len(df_pre) < 35:
            return 0
        period_res = {}
        for _d in _BT_SLICE_DAYS:
            try:
                _r = _wfh_rbt(None, "", df_pre, cf, em, sm, tm, _d, st, entry_type=etype)
                if _r and _r.get("trades", 0) > 0:
                    period_res[_d] = _r
            except Exception:
                pass
        if not period_res:
            return 0
        _vals = list(period_res.values())
        _avg_wr  = sum(r["win_rate"] for r in _vals) / len(_vals)
        _avg_pf  = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10) for r in _vals) / len(_vals)
        _stable  = sum(1 for r in _vals if r["total_pnl"] > 0) / len(_vals)
        _tot_tr  = sum(r["trades"] for r in _vals)
        return min(round(_avg_wr * 0.4 + (_avg_pf / 10) * 30 + _stable * 20 + min(_tot_tr / 20, 1) * 10), 100)

    # MAX_HOLD(15) + ENTRY_EXPIRE(3) + バッファ(3) = 21日以内のシグナルは未決済の可能性あり
    _RECOMPUTE_DAYS = 21

    def _run_wf_oos(args, override_days=None):
        _s, _n, _st = args
        try:
            _sdef = _SDEFS_WFH.get(_st)
            if not _sdef:
                return []
            _cf, _em, _sm, _tm, _family, _etype = _sdef
            _bt_days = override_days or _oos_backtest_days
            _df = _wfh_fetch(_s, _bt_days + 60)
            if _df is None or _df.empty:
                return []
            # OOS前BTスコア：wf_until_date以前のデータのみで計算（バイアスなし）
            import pandas as _pd_oos
            _df_pre = _df[_df.index <= _pd_oos.Timestamp(wf_until_date)].copy()
            _bt_pre = _calc_preoos_bt_score(_df_pre, _cf, _em, _sm, _tm, _st, _etype)
            _res = _wfh_rbt(_s, _n, _df, _cf, _em, _sm, _tm, _bt_days, _st, entry_type=_etype)
            if not _res:
                return []
            _tlog = _res.get("trade_log", [])
            _seen: set = set()
            _out = []
            _fp = _fp_lookup.get((_s, _st), 0)
            for _t in _tlog:
                if _t.get("reason") in ("発注中", "保有中") or _t.get("exit_dt") is None:
                    continue
                _k = (_s, _st, _t.get("signal_dt"))
                if _k not in _seen:
                    _seen.add(_k)
                    _out.append({**_t, "symbol": _s, "name": _n, "strategy": _st,
                                 "folds_passed": _fp,
                                 "bt_score_preoos": _bt_pre,
                                 "family": "short" if _st.endswith("_S") else "long"})
            return _out
        except Exception:
            return []

    # ── OOS取引キャッシュ（2段階: 決済済み永久保存 + 直近21日当日キャッシュ）────
    # 決済済み: signal_dt が今日から21日以上前 → 結果確定・永久保存
    # 直近21日: 毎日再計算（未決済トレードが含まれる可能性）
    _oos_cache_dir   = _PL(".wfh_cache")
    _closed_cache    = _oos_cache_dir / f"oos_{wf_until_date}_closed.pkl"
    _recent_cache    = _oos_cache_dir / f"oos_{wf_until_date}_{_wfh_today}_recent.pkl"

    # --force はHTML再生成のみ。OOSキャッシュは削除しない。
    # （翌日になると _recent_cache のファイル名が変わり自動的に再計算される）

    # 決済済みキャッシュ読込
    _closed_trades: list[dict] = []
    if _closed_cache.exists():
        try:
            with open(_closed_cache, "rb") as _f:
                _closed_trades = _pkl.load(_f)
            print(f"[WF歴史検証] 決済済みキャッシュ読込: {len(_closed_trades)}件 ({wf_until_date})", flush=True)
        except Exception:
            _closed_trades = []

    # 直近キャッシュ読込または計算
    _recent_trades: list[dict] = []
    if _recent_cache.exists():
        try:
            with open(_recent_cache, "rb") as _f:
                _recent_trades = _pkl.load(_f)
            print(f"[WF歴史検証] OOS直近キャッシュ読込: {len(_recent_trades)}件 ({wf_until_date})", flush=True)
        except Exception:
            _recent_trades = []

    if not _recent_trades and passed_tasks:
        if _closed_trades:
            # 増分モード: 直近21日のみ計算（高速）
            print(f"[WF歴史検証] OOS直近{_RECOMPUTE_DAYS}日を計算中 ({wf_until_date})…", flush=True)
            from functools import partial as _partial
            _run_recent = _partial(_run_wf_oos, override_days=_RECOMPUTE_DAYS)
            with _TPE(max_workers=workers) as _ex2:
                _futs2 = {_ex2.submit(_run_recent, t): t for t in passed_tasks}
                for _fut in _asc(_futs2):
                    try:
                        _recent_trades.extend(_fut.result())
                    except Exception:
                        pass
        else:
            # 初回: 全OOS期間を計算（決済済みキャッシュがない場合）
            print(f"[WF歴史検証] OOS全期間を計算中（初回） ({wf_until_date})…", flush=True)
            with _TPE(max_workers=workers) as _ex2:
                _futs2 = {_ex2.submit(_run_wf_oos, t): t for t in passed_tasks}
                for _fut in _asc(_futs2):
                    try:
                        _recent_trades.extend(_fut.result())
                    except Exception:
                        pass
            # 初回は全結果を決済済み+直近に分割して保存
            _cutoff = _wfh_today - _td(days=_RECOMPUTE_DAYS)
            def _sig_date(t):
                _d = t.get("signal_dt")
                return _d.date() if hasattr(_d, "date") else (_d if _d else _wfh_today)
            _closed_trades = [t for t in _recent_trades if _sig_date(t) < _cutoff]
            _recent_trades = [t for t in _recent_trades if _sig_date(t) >= _cutoff]
            try:
                with open(_closed_cache, "wb") as _f:
                    _pkl.dump(_closed_trades, _f)
                print(f"[WF歴史検証] 決済済みキャッシュ保存（初回）: {len(_closed_trades)}件", flush=True)
            except Exception:
                pass

        try:
            with open(_recent_cache, "wb") as _f:
                _pkl.dump(_recent_trades, _f)
            print(f"[WF歴史検証] OOS直近キャッシュ保存: {len(_recent_trades)}件", flush=True)
        except Exception:
            pass

    # マージ: 決済済み + 直近
    oos_trades: list[dict] = _closed_trades + _recent_trades

    # 決済済みキャッシュの更新（直近トレードの中で確定済みのものを追加）
    _cutoff = _wfh_today - _td(days=_RECOMPUTE_DAYS)
    def _sig_date(t):
        _d = t.get("signal_dt")
        return _d.date() if hasattr(_d, "date") else (_d if _d else _wfh_today)
    _new_closed = [t for t in oos_trades if _sig_date(t) < _cutoff]
    if len(_new_closed) > len(_closed_trades):
        try:
            with open(_closed_cache, "wb") as _f:
                _pkl.dump(_new_closed, _f)
            print(f"[WF歴史検証] 決済済みキャッシュ更新: {len(_new_closed)}件 (+{len(_new_closed)-len(_closed_trades)}件)", flush=True)
        except Exception:
            pass

    # ── N225 MA75フィルター用フラグをロングトレードに付与 ────────────────────────
    try:
        from backtest_limit_entry import get_n225_ma75_above as _get_n225_above
        _n225_dict = _get_n225_above()
    except Exception:
        _n225_dict = {}
    for _t in oos_trades:
        if _t.get("family") == "long":
            _sig_dt = _t.get("signal_dt")
            _ds = (_sig_dt.strftime("%Y-%m-%d") if hasattr(_sig_dt, "strftime")
                   else str(_sig_dt)[:10]) if _sig_dt else ""
            _t["n225_above"] = _n225_dict.get(_ds, True)
        else:
            _t["n225_above"] = True  # ショートは常に通す

    # ── HTML 生成 ─────────────────────────────────────────────────────────────
    _td  = "border:1px solid #1e293b;padding:6px 10px"
    _th  = f"{_td};background:#1e293b;color:#94a3b8;text-align:center;font-size:0.78rem"
    _tdc = f"{_td};text-align:center"

    def _ok(v: bool) -> str:
        return ('<span style="color:#4ade80;font-weight:bold">✓</span>' if v
                else '<span style="color:#f87171">✗</span>')

    # fold期間の日付計算（表示用）
    # fold環境ラベル: TEST期間を基に動的に生成（新fold設計に対応）
    def _fold_env_label(test_start, test_end):
        yr = test_start.year
        if yr <= 2020: return "COVID暴落・回復"
        if yr <= 2021: return "上昇・調整期"
        if yr <= 2022: return "利上げ・ベア"
        if yr <= 2023: return "回復・上昇"
        return "強気相場"
    fold_periods = []
    for fn, ts, te, vs, ve in _HIST_FOLDS:
        _vs_date = wf_until_date - timedelta(days=vs)
        _ve_date = wf_until_date - timedelta(days=ve)
        fold_periods.append((fn,
                             wf_until_date - timedelta(days=ts),
                             wf_until_date - timedelta(days=te),
                             _vs_date,
                             _ve_date,
                             _fold_env_label(_vs_date, _ve_date)))

    n_4 = sum(1 for r in wf_results if r["folds_passed"] == 4)
    n_3 = sum(1 for r in wf_results if r["folds_passed"] >= 3)
    n_2 = sum(1 for r in wf_results if r["folds_passed"] >= 2)
    n_1 = sum(1 for r in wf_results if r["folds_passed"] >= 1)
    n_t = len(wf_results)

    # ── ① fold設計テーブル ────────────────────────────────────────────────────
    _fold_design_rows = "".join(
        f'<tr>'
        f'<td style="{_td};color:#e2e8f0">{fn}</td>'
        f'<td style="{_tdc};color:#94a3b8">{ts.strftime("%Y/%m")}〜{te.strftime("%Y/%m")}</td>'
        f'<td style="{_tdc};color:#fbbf24">{vs.strftime("%Y/%m")}〜{ve.strftime("%Y/%m")}</td>'
        f'<td style="{_td};color:#64748b;font-size:0.75rem">{env}</td>'
        f'</tr>'
        for fn, ts, te, vs, ve, env in fold_periods
    )

    # ── ② OOS成績（主役）──────────────────────────────────────────────────────
    # OOS統計ヘルパー
    def _oos_stats(trades):
        n = len(trades)
        if n == 0:
            return dict(n=0, wins=0, wr=0.0, pnl=0.0, win_pnl=0.0, loss_pnl=0.0, pf=0.0)
        wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
        pnl = sum(float(t.get("pnl", 0)) for t in trades)
        win_pnl = sum(float(t.get("pnl", 0)) for t in trades if float(t.get("pnl", 0)) > 0)
        loss_pnl = sum(float(t.get("pnl", 0)) for t in trades if float(t.get("pnl", 0)) <= 0)
        pf = (win_pnl / abs(loss_pnl)) if loss_pnl != 0 else float("inf")
        wr = wins / n * 100
        return dict(n=n, wins=wins, wr=wr, pnl=pnl, win_pnl=win_pnl, loss_pnl=loss_pnl, pf=pf)

    def _pf_str_fn(pf):
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    def _pnl_color(v):
        return "#4ade80" if v > 0 else ("#f87171" if v < 0 else "#94a3b8")

    # OOS前BTスコアのルックアップ（シンボル×戦略 → bt_score_preoos）
    # _sym_strat_rows 内で参照するため、ここで先に計算する
    _bt_preoos_lookup: dict = {}
    for _t in oos_trades:
        _k = (_t["symbol"], _t["strategy"])
        if _k not in _bt_preoos_lookup:
            _bt_preoos_lookup[_k] = _t.get("bt_score_preoos", 0)

    # WFテスト期間からBTスコア相当を計算（訓練データ評価）
    def _calc_wf_bt_score(r):
        folds = [fd for fd in r.get("fold_detail", []) if fd.get("test_trades", 0) >= 1]
        if not folds:
            return 0
        avg_wr  = sum(fd["test_wr"] for fd in folds) / len(folds) / 100
        avg_pf  = sum(min(fd["test_pf"] if fd["test_pf"] != float("inf") else 10, 10) for fd in folds) / len(folds)
        stable  = sum(1 for fd in folds if fd.get("test_pnl", 0) > 0) / len(folds)
        tot_tr  = sum(fd.get("test_trades", 0) for fd in folds)
        return min(round(avg_wr * 40 + (avg_pf / 10) * 30 + stable * 20 + min(tot_tr / 20, 1) * 10), 100)

    _wf_score_lookup = {(r["symbol"], r["strategy"]): _calc_wf_bt_score(r) for r in wf_results}

    # 戦略別集計
    def _strategy_rows(trades):
        from collections import defaultdict as _dd
        by_st = _dd(list)
        for t in trades:
            by_st[t["strategy"]].append(t)
        rows = ""
        for st in sorted(by_st):
            s = _oos_stats(by_st[st])
            pc = _pnl_color(s["pnl"])
            _fam = "short" if st.endswith("_S") else "long"
            rows += (
                f'<tr data-family="{_fam}">'
                f'<td style="{_td};color:#e2e8f0">{st}</td>'
                f'<td style="{_tdc}">{s["n"]}</td>'
                f'<td style="{_tdc}">{s["wr"]:.1f}%</td>'
                f'<td style="{_tdc}">{_pf_str_fn(s["pf"])}</td>'
                f'<td style="{_tdc};color:#4ade80">{s["win_pnl"]:+,.0f}</td>'
                f'<td style="{_tdc};color:#f87171">{s["loss_pnl"]:+,.0f}</td>'
                f'<td style="{_tdc};color:{pc};font-weight:bold">{s["pnl"]:+,.0f}円</td>'
                f'</tr>\n'
            )
        return rows or f'<tr><td colspan="7" style="text-align:center;color:#64748b;padding:12px">取引なし</td></tr>'

    # 銘柄×戦略別集計（損益降順 top30）
    def _sym_strat_rows(trades):
        from collections import defaultdict as _dd
        by_ss = _dd(list)
        for t in trades:
            by_ss[(t["symbol"], t["name"], t["strategy"], t.get("folds_passed", 0))].append(t)
        ranked = sorted(by_ss.items(), key=lambda kv: -sum(float(x.get("pnl", 0)) for x in kv[1]))[:30]
        rows = ""
        for (sym, nm, st, fp), tlist in ranked:
            s = _oos_stats(tlist)
            pc = _pnl_color(s["pnl"])
            fp_c = "#4ade80" if fp >= 3 else ("#fbbf24" if fp == 2 else "#94a3b8")
            _fam = "short" if st.endswith("_S") else "long"
            _wf_sc = _wf_score_lookup.get((sym, st), 0)
            _sc_c = "#4ade80" if _wf_sc >= 60 else ("#fbbf24" if _wf_sc >= 40 else "#94a3b8")
            _bt_pre = _bt_preoos_lookup.get((sym, st), 0)
            _bt_c = "#4ade80" if _bt_pre >= 60 else ("#fbbf24" if _bt_pre >= 40 else "#94a3b8")
            rows += (
                f'<tr data-fp="{fp}" data-family="{_fam}" data-wfscore="{_wf_sc}" class="wfhoos-row">'
                f'<td style="{_tdc};color:{fp_c};font-weight:bold">{fp}</td>'
                f'<td style="{_tdc};color:{_sc_c};font-weight:bold">{_wf_sc}</td>'
                f'<td style="{_tdc};color:{_bt_c};font-weight:bold">{_bt_pre}</td>'
                f'<td style="{_td};color:#e2e8f0">{sym}</td>'
                f'<td style="{_td};color:#94a3b8;font-size:0.8rem">{nm}</td>'
                f'<td style="{_td};color:#94a3b8">{st}</td>'
                f'<td style="{_tdc}">{s["n"]}</td>'
                f'<td style="{_tdc}">{s["wr"]:.1f}%</td>'
                f'<td style="{_tdc}">{_pf_str_fn(s["pf"])}</td>'
                f'<td style="{_tdc};color:{pc};font-weight:bold">{s["pnl"]:+,.0f}円</td>'
                f'</tr>\n'
            )
        return rows or f'<tr><td colspan="10" style="text-align:center;color:#64748b;padding:12px">取引なし</td></tr>'

    # 月次内訳
    def _monthly_rows(trades):
        from collections import defaultdict as _dd
        by_ym = _dd(list)
        for t in trades:
            _dt = t.get("exit_dt") or t.get("signal_dt")
            if _dt:
                try:
                    _ym = str(_dt)[:7]
                    by_ym[_ym].append(t)
                except Exception:
                    pass
        rows = ""
        for ym in sorted(by_ym.keys()):
            s = _oos_stats(by_ym[ym])
            pc = _pnl_color(s["pnl"])
            rows += (
                f'<tr>'
                f'<td style="{_td};color:#e2e8f0">{ym}</td>'
                f'<td style="{_tdc}">{s["n"]}</td>'
                f'<td style="{_tdc}">{s["wr"]:.1f}%</td>'
                f'<td style="{_tdc}">{_pf_str_fn(s["pf"])}</td>'
                f'<td style="{_tdc};color:{pc};font-weight:bold">{s["pnl"]:+,.0f}円</td>'
                f'</tr>\n'
            )
        return rows or f'<tr><td colspan="5" style="text-align:center;color:#64748b;padding:12px">取引なし</td></tr>'

    # ③ WF選定詳細テーブル（折りたたみ）
    _fold_header = "".join(
        f'<th style="{_th}" colspan="2">{fn}<br>'
        f'<span style="font-size:0.7rem;color:#64748b">TEST:{vs.strftime("%Y/%m")}〜{ve.strftime("%Y/%m")}</span></th>'
        for fn, _, _, vs, ve, _ in fold_periods
    )
    _fold_subhdr = (f'<th style="{_th}">訓練</th><th style="{_th}">検証</th>') * len(_HIST_FOLDS)

    # 各フィルタレベルのデータを計算
    oos_by_fp = {
        1: [t for t in oos_trades if t.get("folds_passed", 0) >= 1],
        2: [t for t in oos_trades if t.get("folds_passed", 0) >= 2],
        3: [t for t in oos_trades if t.get("folds_passed", 0) >= 3],
    }
    _s1 = _oos_stats(oos_by_fp[1])
    _def_s = _oos_stats(oos_by_fp[2])
    _s3 = _oos_stats(oos_by_fp[3])
    _def_pc = _pnl_color(_def_s["pnl"])

    # OOSテーブル HTML（3バージョン）- JS文字列ではなく直接HTMLに埋め込む
    _strat1 = _strategy_rows(oos_by_fp[1])
    _strat2 = _strategy_rows(oos_by_fp[2])
    _strat3 = _strategy_rows(oos_by_fp[3])
    _sym1   = _sym_strat_rows(oos_by_fp[1])
    _sym2   = _sym_strat_rows(oos_by_fp[2])
    _sym3   = _sym_strat_rows(oos_by_fp[3])
    _mon1   = _monthly_rows(oos_by_fp[1])
    _mon2   = _monthly_rows(oos_by_fp[2])
    _mon3   = _monthly_rows(oos_by_fp[3])

    # WF選定詳細：全行を data-wffp 属性付きで一度だけ生成（JS切り替え不要）
    def _wf_detail_rows_all():
        rows = ""
        for r in sorted(wf_results, key=lambda x: (-x["folds_passed"], -x.get("total_test_pnl", 0))):
            fp = r["folds_passed"]
            fp_c = "#4ade80" if fp >= 3 else ("#fbbf24" if fp == 2 else "#f87171")
            _hidden = ' style="display:none"' if fp < 2 else ""
            _cells = ""
            for fd in r.get("fold_detail", []):
                _cells += (
                    f'<td style="{_tdc}">{_ok(fd["pass_train"])}'
                    f'<br><span style="font-size:0.7rem;color:#94a3b8">PF{fd["train_pf"]}</span></td>'
                    f'<td style="{_tdc}">{_ok(fd["pass_test"])}'
                    f'<br><span style="font-size:0.7rem;color:#94a3b8">PF{fd["test_pf"]}</span></td>'
                )
            _pnl = r.get("total_test_pnl", 0)
            rows += (
                f'<tr data-wffp="{fp}"{_hidden}>'
                f'<td style="{_td};color:#e2e8f0">{r["symbol"]}</td>'
                f'<td style="{_td};color:#94a3b8;font-size:0.8rem">{r["name"]}</td>'
                f'<td style="{_td};color:#94a3b8;font-size:0.8rem">{r["strategy"]}</td>'
                f'<td style="{_tdc};color:{fp_c};font-weight:bold">{fp}/4</td>'
                f'{_cells}'
                f'<td style="{_tdc}">{r.get("avg_test_wr", 0):.1f}%</td>'
                f'<td style="{_tdc}">{r.get("avg_test_pf", 0):.2f}</td>'
                f'<td style="{_td};text-align:right;color:{"#4ade80" if _pnl>0 else "#f87171"}">'
                f'{_pnl:+,.0f}円</td>'
                f'</tr>\n'
            )
        return rows or f'<tr><td colspan="12" style="text-align:center;color:#64748b;padding:16px">結果なし</td></tr>'

    _det_all = _wf_detail_rows_all()

    # OOSテーブル共通ヘッダ HTML
    def _oos_strat_table(body_html):
        return (
            f'<div style="overflow-x:auto;margin-bottom:16px">'
            f'<table style="border-collapse:collapse;font-size:0.82rem;min-width:600px">'
            f'<thead><tr>'
            f'<th style="{_th}">戦略</th><th style="{_th}">取引</th>'
            f'<th style="{_th}">勝率</th><th style="{_th}">PF</th>'
            f'<th style="{_th}">利益計</th><th style="{_th}">損失計</th>'
            f'<th style="{_th}">損益</th></tr></thead>'
            f'<tbody>{body_html}</tbody></table></div>'
        )

    def _oos_sym_table(body_html):
        return (
            f'<div style="overflow-x:auto;margin-bottom:16px">'
            f'<table style="border-collapse:collapse;font-size:0.82rem;min-width:850px">'
            f'<thead><tr>'
            f'<th style="{_th}">fold数</th>'
            f'<th style="{_th}" title="WFテスト期間の成績ベーススコア（訓練データ評価）">WFスコア</th>'
            f'<th style="{_th}" title="OOS期間開始前のBTスコア（バイアスなし）">OOS前BT</th>'
            f'<th style="{_th}">コード</th>'
            f'<th style="{_th}">銘柄</th><th style="{_th}">戦略</th>'
            f'<th style="{_th}">取引</th><th style="{_th}">勝率</th>'
            f'<th style="{_th}">PF</th><th style="{_th}">損益</th>'
            f'</tr></thead><tbody>{body_html}</tbody></table></div>'
        )

    def _oos_mon_table(body_html):
        return (
            f'<div style="overflow-x:auto;margin-bottom:20px">'
            f'<table style="border-collapse:collapse;font-size:0.82rem;min-width:400px">'
            f'<thead><tr>'
            f'<th style="{_th}">年月</th><th style="{_th}">取引</th>'
            f'<th style="{_th}">勝率</th><th style="{_th}">PF</th>'
            f'<th style="{_th}">損益</th>'
            f'</tr></thead><tbody>{body_html}</tbody></table></div>'
        )

    def _oos_container(fp_lvl, body1, body2, s, visible):
        _tab_btn = (
            f'<div style="display:flex;gap:4px;margin-bottom:8px">'
            f'<button id="wfhtab-{fp_lvl}-strat" onclick="wfhShowTab({fp_lvl},\'strat\')" '
            f'style="padding:4px 12px;border-radius:4px;border:1px solid #38bdf8;background:#1e40af;color:#e2e8f0;font-size:0.8rem;cursor:pointer">戦略別</button>'
            f'<button id="wfhtab-{fp_lvl}-sym" onclick="wfhShowTab({fp_lvl},\'sym\')" '
            f'style="padding:4px 12px;border-radius:4px;border:1px solid #334155;background:#1e293b;color:#94a3b8;font-size:0.8rem;cursor:pointer">銘柄別</button>'
            f'</div>'
        )
        _pane_strat = f'<div id="wfhpane-{fp_lvl}-strat">' + _oos_strat_table(body1) + '</div>'
        _pane_sym   = f'<div id="wfhpane-{fp_lvl}-sym" style="display:none">' + _oos_sym_table(body2) + '</div>'
        _disp = "" if visible else ' style="display:none"'
        return f'<div id="wfhoos-container-{fp_lvl}"{_disp}>{_tab_btn}{_pane_strat}{_pane_sym}</div>'

    _container1 = _oos_container(1, _strat1, _sym1, _s1, False)
    _container2 = _oos_container(2, _strat2, _sym2, _def_s, True)
    _container3 = _oos_container(3, _strat3, _sym3, _s3, False)

    # ── ロング/ショート分離の事前計算（9パターン: fold×family）──────────────────
    _long_trades  = [t for t in oos_trades if t.get("family") == "long"]
    _short_trades = [t for t in oos_trades if t.get("family") == "short"]
    _stats_9: dict = {}
    for _fp9 in [1, 2, 3]:
        for _fam9, _pool9 in [("all", oos_trades), ("long", _long_trades), ("short", _short_trades)]:
            _s9 = _oos_stats([t for t in _pool9 if t.get("folds_passed", 0) >= _fp9])
            _stats_9[f"{_fp9}_{_fam9}"] = _s9

    # fold3のTEST終了日 = 選定に使ったデータの最終日
    _last_fold_test_end = wf_until_date - timedelta(days=_HIST_FOLDS[-1][4])  # ve of last fold
    _unused_days = (_wfh_today - _last_fold_test_end).days

    # ── 36→144パターン統計+月次テーブルデータ (fold×family×wfscore_threshold×bt_preoos_threshold) ──────
    _score_thresholds = [0, 40, 60, 80]
    _bt_thresholds = [0, 40, 60, 80]
    _stats_36: dict = {}
    _monthly_36: dict = {}  # {key: [[ym, n, wr, pf, win_pnl, loss_pnl, pnl], ...]}

    from collections import defaultdict as _ddm
    def _calc_monthly_rows(trades):
        by_ym = _ddm(list)
        for t in trades:
            _dt = t.get("exit_dt") or t.get("signal_dt")
            if _dt:
                by_ym[str(_dt)[:7]].append(t)
        result = []
        for ym in sorted(by_ym.keys()):
            s = _oos_stats(by_ym[ym])
            result.append([ym, s["n"], round(s["wr"], 1), round(s["pf"], 2),
                           round(s["win_pnl"]), round(s["loss_pnl"]), round(s["pnl"])])
        return result

    for _fp9 in [1, 2, 3]:
        for _fam9, _pool9 in [("all", oos_trades), ("long", _long_trades), ("short", _short_trades)]:
            for _sc9 in _score_thresholds:
                for _bts9 in _bt_thresholds:
                    _base9 = [t for t in _pool9 if t.get("folds_passed", 0) >= _fp9]
                    if _sc9 > 0:
                        _base9 = [t for t in _base9 if _wf_score_lookup.get((t["symbol"], t["strategy"]), 0) >= _sc9]
                    if _bts9 > 0:
                        _base9 = [t for t in _base9 if t.get("bt_score_preoos", 0) >= _bts9]
                    _key9 = f"{_fp9}_{_fam9}_{_sc9}_{_bts9}"
                    _stats_36[_key9] = _oos_stats(_base9)
                    _monthly_36[_key9] = _calc_monthly_rows(_base9)

    # ── N225フィルター適用版 144パターン（ロング：MA75以上の日のみ）──────────────
    _n225_long_trades = [t for t in _long_trades if t.get("n225_above", True)]
    _n225_all_trades  = _n225_long_trades + _short_trades
    _stats_36_n225: dict = {}
    _monthly_36_n225: dict = {}
    for _fp9 in [1, 2, 3]:
        for _fam9, _pool9 in [("all", _n225_all_trades), ("long", _n225_long_trades), ("short", _short_trades)]:
            for _sc9 in _score_thresholds:
                for _bts9 in _bt_thresholds:
                    _base9 = [t for t in _pool9 if t.get("folds_passed", 0) >= _fp9]
                    if _sc9 > 0:
                        _base9 = [t for t in _base9 if _wf_score_lookup.get((t["symbol"], t["strategy"]), 0) >= _sc9]
                    if _bts9 > 0:
                        _base9 = [t for t in _base9 if t.get("bt_score_preoos", 0) >= _bts9]
                    _key9 = f"{_fp9}_{_fam9}_{_sc9}_{_bts9}"
                    _stats_36_n225[_key9] = _oos_stats(_base9)
                    _monthly_36_n225[_key9] = _calc_monthly_rows(_base9)

    import json as _json
    _monthly_36_js = _json.dumps(_monthly_36, ensure_ascii=False)
    _monthly_36_n225_js = _json.dumps(_monthly_36_n225, ensure_ascii=False)

    _html = f"""
<div style="background:#0f172a;color:#e2e8f0;padding:20px;border-radius:8px;margin:12px 0">
  <h3 style="color:#38bdf8;margin:0 0 4px">
    WF歴史検証（{wf_until_date} 時点で新規選定 → 以降 {(_wfh_today - wf_until_date).days}日間 OOS）
  </h3>
  <p style="color:#64748b;font-size:0.82rem;margin:0 0 16px">
    <strong style="color:#fbbf24">ユニバース {_univ_name}（{len(universe_syms)}銘柄）</strong> × 全{len(_all_strats)}戦略を
    <strong style="color:#fbbf24">{wf_until_date}</strong> 以前のデータのみで
    3fold WF（TRAIN2年/TEST1年、時系列順・非重複）スキャンして新規銘柄選定。
    現行WATCHLIST・シグナルには一切影響なし。
    {'株価フィルタ: ' + (f'¥{min_price:,.0f}〜' if min_price > 0 else '') + (f'¥{max_price:,.0f}' if max_price > 0 else '上限なし') + ' / 100株' if max_price > 0 or min_price > 0 else ''}
  </p>

  <!-- ① fold設計 -->
  <h4 style="color:#94a3b8;margin:0 0 6px;font-size:0.9rem">① fold 設計（{wf_until_date} 基準）</h4>
  <table style="font-size:0.8rem;border-collapse:collapse;margin-bottom:20px">
    <thead><tr>
      <th style="{_th}">fold</th>
      <th style="{_th}">TRAIN期間（2年）</th>
      <th style="{_th}">TEST期間（1年）</th>
      <th style="{_th}">テスト環境</th>
    </tr></thead>
    <tbody>{_fold_design_rows}</tbody>
  </table>

  <!-- データ期間説明 -->
  <div style="background:#1e293b;border-left:3px solid #38bdf8;padding:10px 16px;margin-bottom:20px;font-size:0.82rem">
    <span style="color:#94a3b8">訓練データ最終日:</span>
    <strong style="color:#fbbf24">{_last_fold_test_end}</strong>
    <span style="color:#64748b;margin:0 12px">|</span>
    <span style="color:#94a3b8">選定に使っていない期間:</span>
    <strong style="color:#f87171">{_last_fold_test_end} 〜 今日（{_unused_days}日間）</strong>
    <span style="color:#64748b;margin:0 12px">|</span>
    <span style="color:#94a3b8">OOS:</span>
    <strong style="color:#4ade80">{wf_until_date} 〜 今日</strong>
  </div>

  <!-- ② OOS成績（主役） -->
  <h4 style="color:#38bdf8;margin:0 0 10px;font-size:1rem">
    ② OOS成績（{wf_until_date}〜{_wfh_today} / 全{len(oos_trades)}取引）
  </h4>

  <!-- fold別フィルタボタン -->
  <div style="margin-bottom:8px">
    <span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">fold通過数:</span>
    <button id="wfhoos-btn-1" onclick="wfhoosFilter(1)"
      style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem">
      ≥1fold ({n_1}件)
    </button>
    <button id="wfhoos-btn-2" onclick="wfhoosFilter(2)"
      style="background:#1e40af;color:#e2e8f0;border:1px solid #38bdf8;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem;font-weight:bold">
      ≥2fold ({n_2}件) ★デフォルト
    </button>
    <button id="wfhoos-btn-3" onclick="wfhoosFilter(3)"
      style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:0.82rem">
      ≥3fold ({n_3}件)
    </button>
  </div>
  <!-- ロング/ショートフィルタボタン -->
  <div style="margin-bottom:12px">
    <span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">戦略ファミリー:</span>
    <button id="wfhfam-btn-all" onclick="wfhoosFamilyFilter('all')"
      style="background:#1e40af;color:#e2e8f0;border:1px solid #38bdf8;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem;font-weight:bold">
      全て
    </button>
    <button id="wfhfam-btn-long" onclick="wfhoosFamilyFilter('long')"
      style="background:#1e293b;color:#4ade80;border:1px solid #334155;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem">
      ロングのみ
    </button>
    <button id="wfhfam-btn-short" onclick="wfhoosFamilyFilter('short')"
      style="background:#1e293b;color:#f87171;border:1px solid #334155;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:0.82rem">
      ショートのみ
    </button>
  </div>
  <!-- WFスコアフィルタボタン -->
  <div style="margin-bottom:12px">
    <span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">WFスコア:</span>
    <button id="wfhsc-btn-0" onclick="wfhoosScoreFilter(0)"
      style="background:#1e40af;color:#e2e8f0;border:1px solid #38bdf8;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem;font-weight:bold">
      すべて
    </button>
    <button id="wfhsc-btn-40" onclick="wfhoosScoreFilter(40)"
      style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem">
      ★≥40
    </button>
    <button id="wfhsc-btn-60" onclick="wfhoosScoreFilter(60)"
      style="background:#1e293b;color:#fbbf24;border:1px solid #334155;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem">
      ★★≥60
    </button>
    <button id="wfhsc-btn-80" onclick="wfhoosScoreFilter(80)"
      style="background:#1e293b;color:#fbbf24;border:1px solid #334155;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:0.82rem">
      ★★★≥80
    </button>
  </div>
  <!-- OOS前BTスコアフィルタボタン -->
  <div style="margin-bottom:12px">
    <span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">OOS前BTスコア（バイアスなし）:</span>
    <button id="wfhbt-btn-0" onclick="wfhoosBtFilter(0)"
      style="background:#1e40af;color:#e2e8f0;border:1px solid #38bdf8;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem;font-weight:bold">
      すべて
    </button>
    <button id="wfhbt-btn-40" onclick="wfhoosBtFilter(40)"
      style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem">
      ★≥40
    </button>
    <button id="wfhbt-btn-60" onclick="wfhoosBtFilter(60)"
      style="background:#1e293b;color:#fbbf24;border:1px solid #334155;border-radius:4px;padding:4px 12px;margin-right:6px;cursor:pointer;font-size:0.82rem">
      ★★≥60
    </button>
    <button id="wfhbt-btn-80" onclick="wfhoosBtFilter(80)"
      style="background:#1e293b;color:#fbbf24;border:1px solid #334155;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:0.82rem">
      ★★★≥80
    </button>
  </div>

  <!-- N225 MA75フィルター -->
  <div style="margin-bottom:14px;padding:10px 14px;background:#1c2c1c;border:1px solid #365636;border-radius:6px;display:inline-block">
    <label style="color:#4ade80;font-size:0.85rem;cursor:pointer;display:inline-flex;align-items:center;gap:8px">
      <input type="checkbox" id="wfh-n225-filter" onchange="wfhoosN225Toggle()"
             style="width:15px;height:15px;accent-color:#4ade80;cursor:pointer">
      <strong>日経MA75フィルター</strong>
      <span style="color:#86efac;font-size:0.78rem">ロング：日経平均がMA75以上の日のみエントリー（3月のような急落相場を回避）</span>
    </label>
  </div>

  <!-- サマリー行（JS で更新） -->
  <div id="wfhoos-summary" style="background:#1e293b;border-radius:6px;padding:14px 20px;margin-bottom:16px;display:inline-block;min-width:500px">
    <table style="font-size:0.9rem;border-collapse:collapse">
      <tr>
        <td style="color:#94a3b8;padding:4px 20px 4px 0">取引数</td>
        <td id="wfhoos-sum-n" style="color:#e2e8f0;font-weight:bold">{_def_s["n"]}件</td>
        <td style="color:#94a3b8;padding:4px 20px 4px 20px">勝率</td>
        <td id="wfhoos-sum-wr" style="color:#e2e8f0;font-weight:bold">{_def_s["wr"]:.1f}% ({_def_s["wins"]}勝{_def_s["n"]-_def_s["wins"]}負)</td>
      </tr>
      <tr>
        <td style="color:#94a3b8;padding:4px 20px 4px 0">PF</td>
        <td id="wfhoos-sum-pf" style="color:#e2e8f0;font-weight:bold">{_pf_str_fn(_def_s["pf"])}</td>
        <td style="color:#94a3b8;padding:4px 20px 4px 20px">損益合計</td>
        <td id="wfhoos-sum-pnl" style="color:{_def_pc};font-weight:bold;font-size:1.1rem">{_def_s["pnl"]:+,.0f}円</td>
      </tr>
    </table>
  </div>

  <!-- OOSテーブル 3コンテナ（CSS display:none で切り替え） -->
  {_container1}
  {_container2}
  {_container3}

  <!-- 月次内訳（フィルタ連動・動的更新） -->
  <h5 style="color:#94a3b8;margin:16px 0 6px;font-size:0.85rem">月次内訳</h5>
  <div style="overflow-x:auto;margin-bottom:20px">
  <table style="border-collapse:collapse;font-size:0.82rem;min-width:500px">
    <thead><tr>
      <th style="{_th}">年月</th><th style="{_th}">取引</th>
      <th style="{_th}">勝率</th><th style="{_th}">PF</th>
      <th style="{_th}">利益計</th><th style="{_th}">損失計</th>
      <th style="{_th}">損益</th>
    </tr></thead>
    <tbody id="wfhmon-tbody"><tr><td colspan="7" style="text-align:center;color:#64748b;padding:12px">...</td></tr></tbody>
  </table>
  </div>

  <!-- ③ WF選定詳細（折りたたみ） -->
  <details style="margin-top:8px">
    <summary style="color:#94a3b8;cursor:pointer;font-size:0.9rem;padding:6px 0">
      ③ WF選定詳細（全{n_t}件 / 4fold:{n_4}件 / ≥3fold:{n_3}件 / ≥2fold:{n_2}件 / ≥1fold:{n_1}件）
    </summary>
    <div style="margin-top:10px">
      <div style="margin-bottom:8px">
        <span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">表示フィルタ:</span>
        <button onclick="wfhdetFilter(2)" id="wfhdet-btn-2"
          style="background:#1e40af;color:#e2e8f0;border:1px solid #38bdf8;border-radius:4px;padding:3px 10px;margin-right:4px;cursor:pointer;font-size:0.78rem;font-weight:bold">
          ≥2fold
        </button>
        <button onclick="wfhdetFilter(1)" id="wfhdet-btn-1"
          style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:3px 10px;margin-right:4px;cursor:pointer;font-size:0.78rem">
          ≥1fold
        </button>
        <button onclick="wfhdetFilter(0)" id="wfhdet-btn-0"
          style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:0.78rem">
          全件
        </button>
      </div>
      <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:0.78rem;min-width:900px">
        <thead>
          <tr>
            <th style="{_th}" rowspan="2">コード</th>
            <th style="{_th}" rowspan="2">銘柄名</th>
            <th style="{_th}" rowspan="2">戦略</th>
            <th style="{_th}" rowspan="2">合格</th>
            {_fold_header}
            <th style="{_th}" rowspan="2">平均勝率</th>
            <th style="{_th}" rowspan="2">平均PF</th>
            <th style="{_th}" rowspan="2">WF内TEST損益</th>
          </tr>
          <tr>{_fold_subhdr}</tr>
        </thead>
        <tbody id="wfhdet-body">{_det_all}</tbody>
      </table>
      </div>
    </div>
  </details>
</div>

<script>
(function() {{
  var _wfhCurrentFp      = 2;
  var _wfhCurrentFam     = "all";
  var _wfhCurrentScore   = 0;
  var _wfhCurrentBtScore = 0;
  var _wfhN225On         = false;

  // 144パターン統計 (fold×family×wfscore_threshold×bt_preoos_threshold) — N225フィルターなし
  var _wfhMonthly36 = {_monthly_36_js};
  var _wfhStats36 = {{
    {", ".join(
      f'"{k}": {{ n: {v["n"]}, wins: {v["wins"]}, wr: {v["wr"]:.1f}, pf: "{_pf_str_fn(v["pf"])}", pnl: {v["pnl"]:.0f}, pnlColor: "{_pnl_color(v["pnl"])}" }}'
      for k, v in _stats_36.items()
    )}
  }};

  // N225 MA75フィルターあり版（ロング：MA75以上の日のみ）
  var _wfhMonthly36N225 = {_monthly_36_n225_js};
  var _wfhStats36N225 = {{
    {", ".join(
      f'"{k}": {{ n: {v["n"]}, wins: {v["wins"]}, wr: {v["wr"]:.1f}, pf: "{_pf_str_fn(v["pf"])}", pnl: {v["pnl"]:.0f}, pnlColor: "{_pnl_color(v["pnl"])}" }}'
      for k, v in _stats_36_n225.items()
    )}
  }};

  window.wfhoosN225Toggle = function() {{
    var cb = document.getElementById("wfh-n225-filter");
    _wfhN225On = cb ? cb.checked : false;
    _updateSummary();
    _updateMonthlyTable();
  }};

  function _updateSummary() {{
    var key = _wfhCurrentFp + "_" + _wfhCurrentFam + "_" + _wfhCurrentScore + "_" + _wfhCurrentBtScore;
    var d = (_wfhN225On ? _wfhStats36N225 : _wfhStats36)[key];
    if (!d) return;
    var losses = d.n - d.wins;
    document.getElementById("wfhoos-sum-n").textContent = d.n + "件";
    document.getElementById("wfhoos-sum-wr").textContent = d.wr.toFixed(1) + "% (" + d.wins + "勝" + losses + "負)";
    document.getElementById("wfhoos-sum-pf").textContent = d.pf;
    var pnlEl = document.getElementById("wfhoos-sum-pnl");
    pnlEl.textContent = (d.pnl >= 0 ? "+" : "") + Math.round(d.pnl).toLocaleString() + "円";
    pnlEl.style.color = d.pnlColor;
  }}

  function _updateMonthlyTable() {{
    var key  = _wfhCurrentFp + "_" + _wfhCurrentFam + "_" + _wfhCurrentScore + "_" + _wfhCurrentBtScore;
    var rows = (_wfhN225On ? _wfhMonthly36N225 : _wfhMonthly36)[key] || [];
    var tbody = document.getElementById("wfhmon-tbody");
    if (!tbody) return;
    if (rows.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:12px">取引なし</td></tr>';
      return;
    }}
    var tdS  = "padding:6px 12px;border:1px solid #1e293b;text-align:right;font-size:0.82rem";
    var tdSL = "padding:6px 12px;border:1px solid #1e293b;text-align:left;font-size:0.82rem;color:#e2e8f0";
    var html = "";
    rows.forEach(function(r) {{
      var ym = r[0], n = r[1], wr = r[2], pf = r[3], winP = r[4], lossP = r[5], pnl = r[6];
      var pc  = pnl >= 0 ? "#4ade80" : "#f87171";
      var pfStr   = (pf === null || pf >= 9999) ? "∞" : pf.toFixed(2);
      var winStr  = "+" + Math.round(winP).toLocaleString() + "円";
      var lossStr = Math.round(lossP).toLocaleString() + "円";
      var pnlStr  = (pnl >= 0 ? "+" : "") + Math.round(pnl).toLocaleString() + "円";
      html += "<tr>";
      html += '<td style="' + tdSL + '">' + ym + "</td>";
      html += '<td style="' + tdS + '">' + n + "</td>";
      html += '<td style="' + tdS + '">' + wr.toFixed(1) + "%</td>";
      html += '<td style="' + tdS + '">' + pfStr + "</td>";
      html += '<td style="' + tdS + ';color:#4ade80">' + winStr + "</td>";
      html += '<td style="' + tdS + ';color:#f87171">' + lossStr + "</td>";
      html += '<td style="' + tdS + ';color:' + pc + ';font-weight:bold">' + pnlStr + "</td>";
      html += "</tr>";
    }});
    tbody.innerHTML = html;
  }}

  function _applyFilters() {{
    var c = document.getElementById("wfhoos-container-" + _wfhCurrentFp);
    if (!c) return;
    c.querySelectorAll("tr[data-family]").forEach(function(r) {{
      var fam = r.dataset.family || "long";
      var sc  = parseInt(r.dataset.wfscore || "0");
      var famOk   = (_wfhCurrentFam === "all" || fam === _wfhCurrentFam);
      var scoreOk = (sc >= _wfhCurrentScore);
      r.style.display = (famOk && scoreOk) ? "" : "none";
    }});
  }}

  window.wfhShowTab = function(fp, tab) {{
    ["strat","sym"].forEach(function(t) {{
      var pane = document.getElementById("wfhpane-" + fp + "-" + t);
      if (pane) pane.style.display = t === tab ? "" : "none";
      var btn = document.getElementById("wfhtab-" + fp + "-" + t);
      if (!btn) return;
      if (t === tab) {{
        btn.style.background = "#1e40af"; btn.style.color = "#e2e8f0";
        btn.style.borderColor = "#38bdf8";
      }} else {{
        btn.style.background = "#1e293b"; btn.style.color = "#94a3b8";
        btn.style.borderColor = "#334155";
      }}
    }});
  }};

  window.wfhoosFilter = function(minFp) {{
    _wfhCurrentFp = minFp;
    [1,2,3].forEach(function(f) {{
      var c = document.getElementById("wfhoos-container-" + f);
      if (c) c.style.display = f === minFp ? "" : "none";
      var btn = document.getElementById("wfhoos-btn-" + f);
      if (!btn) return;
      if (f === minFp) {{
        btn.style.background = "#1e40af"; btn.style.color = "#e2e8f0";
        btn.style.borderColor = "#38bdf8"; btn.style.fontWeight = "bold";
      }} else {{
        btn.style.background = "#1e293b"; btn.style.color = "#94a3b8";
        btn.style.borderColor = "#334155"; btn.style.fontWeight = "normal";
      }}
    }});
    _applyFilters();
    _updateSummary();
    _updateMonthlyTable();

  }};

  window.wfhoosFamilyFilter = function(fam) {{
    _wfhCurrentFam = fam;
    _applyFilters();
    _updateSummary();
    _updateMonthlyTable();

    ["all","long","short"].forEach(function(f) {{
      var btn = document.getElementById("wfhfam-btn-" + f);
      if (!btn) return;
      var isLong = f === "long", isShort = f === "short";
      if (f === fam) {{
        btn.style.background = "#1e40af";
        btn.style.color = isLong ? "#4ade80" : isShort ? "#f87171" : "#e2e8f0";
        btn.style.borderColor = "#38bdf8"; btn.style.fontWeight = "bold";
      }} else {{
        btn.style.background = "#1e293b";
        btn.style.color = isLong ? "#4ade80" : isShort ? "#f87171" : "#94a3b8";
        btn.style.borderColor = "#334155"; btn.style.fontWeight = "normal";
      }}
    }});
  }};

  window.wfhoosScoreFilter = function(minScore) {{
    _wfhCurrentScore = minScore;
    _applyFilters();
    _updateSummary();
    _updateMonthlyTable();

    [0,40,60,80].forEach(function(s) {{
      var btn = document.getElementById("wfhsc-btn-" + s);
      if (!btn) return;
      if (s === minScore) {{
        btn.style.background = "#1e40af"; btn.style.color = "#e2e8f0";
        btn.style.borderColor = "#38bdf8"; btn.style.fontWeight = "bold";
      }} else {{
        btn.style.background = "#1e293b";
        btn.style.color = s >= 60 ? "#fbbf24" : "#94a3b8";
        btn.style.borderColor = "#334155"; btn.style.fontWeight = "normal";
      }}
    }});
  }};

  window.wfhoosBtFilter = function(minBt) {{
    _wfhCurrentBtScore = minBt;
    _updateSummary();
    _updateMonthlyTable();

    [0,40,60,80].forEach(function(s) {{
      var btn = document.getElementById("wfhbt-btn-" + s);
      if (!btn) return;
      if (s === minBt) {{
        btn.style.background = "#1e40af"; btn.style.color = "#e2e8f0";
        btn.style.borderColor = "#38bdf8"; btn.style.fontWeight = "bold";
      }} else {{
        btn.style.background = "#1e293b";
        btn.style.color = s >= 60 ? "#fbbf24" : "#94a3b8";
        btn.style.borderColor = "#334155"; btn.style.fontWeight = "normal";
      }}
    }});
  }};

  window.wfhdetFilter = function(minFp) {{
    var rows = document.querySelectorAll('#wfhdet-body tr[data-wffp]');
    rows.forEach(function(r) {{
      r.style.display = parseInt(r.dataset.wffp) >= minFp ? "" : "none";
    }});
    [0,1,2].forEach(function(f) {{
      var btn = document.getElementById("wfhdet-btn-" + f);
      if (!btn) return;
      if (f === minFp) {{
        btn.style.background = "#1e40af"; btn.style.color = "#e2e8f0";
        btn.style.borderColor = "#38bdf8"; btn.style.fontWeight = "bold";
      }} else {{
        btn.style.background = "#1e293b"; btn.style.color = "#94a3b8";
        btn.style.borderColor = "#334155"; btn.style.fontWeight = "normal";
      }}
    }});
  }};

  // ページロード時に初期描画
  _updateSummary();
  _updateMonthlyTable();
}})();
</script>"""

    # ── ID プレフィックス適用（複数インスタンスを同一ページに埋め込む際の衝突防止）──
    # 関数名（長いものを先に）
    for _old, _new in [
        ("wfhoosFamilyFilter", f"{_uid}FamilyFilter"),
        ("wfhoosScoreFilter",  f"{_uid}ScFilter"),
        ("wfhoosBtFilter",     f"{_uid}BtFilter"),
        ("wfhoosFilter",       f"{_uid}Filter"),
        ("wfhdetFilter",       f"{_uid}DetFilter"),
        ("wfhShowTab",         f"{_uid}ShowTab"),
        # HTML id= / JS 文字列リテラル（ダブルクォート付き）
        ('"wfhoos-',           f'"{_uid}oos-'),
        ('"wfhfam-',           f'"{_uid}fam-'),
        ('"wfhsc-',            f'"{_uid}sc-'),
        ('"wfhbt-',            f'"{_uid}bt-'),
        ('"wfhdet-',           f'"{_uid}det-'),
        ('"wfhmon-tbody"',     f'"{_uid}mon-tbody"'),
        ('"wfhtab-',           f'"{_uid}tab-'),
        ('"wfhpane-',          f'"{_uid}pane-'),
        ('"wfh-n225-filter"',  f'"{_uid}n225-filter"'),
        ("wfhoosN225Toggle",   f"{_uid}N225Toggle"),
        # querySelectorAll のシングルクォート+ハッシュ
        ("'#wfhdet-body",      f"'#{_uid}det-body"),
    ]:
        _html = _html.replace(_old, _new)

    return _html, _stats_36


def _wf_multi_history_html(dates, workers: int, max_price: float = 0.0,
                            min_price: float = 0.0, universe_path: str | None = None,
                            force: bool = False) -> str:
    """複数基準日WF歴史検証の比較HTML。

    各基準日でキャッシュがあれば即座にOOS成績を集計し、クロス期間比較テーブルを生成。
    キャッシュがない日付は「未実行」として表示（スキャンはしない）。
    最も新しい基準日の詳細インタラクティブHTMLも下部に埋め込む。
    force=True のとき: OOSトレードキャッシュを削除して再計算する。
    """
    from datetime import date as _d

    _today = _d.today()

    def _pf_str(pf):
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    def _color(v):
        return "#4ade80" if v > 0 else "#f87171"

    _th = ("background:#1e293b;color:#94a3b8;padding:8px 14px;text-align:center;"
           "border:1px solid #334155;font-size:0.82rem")
    _td = "padding:8px 14px;border:1px solid #1e293b;text-align:right;font-size:0.85rem"
    _tdl = "padding:8px 14px;border:1px solid #1e293b;text-align:left;font-size:0.85rem"

    results = []       # (wf_date, stats_36, detail_html, uid)
    uncached = []      # dates without cache

    for i, wf_date in enumerate(sorted(dates)):
        _uid = f"wfhd{i}"
        html, stats = _wf_history_html(
            wf_date, workers=workers,
            max_price=max_price, min_price=min_price,
            universe_path=universe_path,
            cache_only=True, _uid=_uid, force=force,
        )
        if html is None:
            uncached.append(wf_date)
            results.append((wf_date, None, None, _uid))
        else:
            results.append((wf_date, stats, html, _uid))

    # ── 比較サマリーテーブル ────────────────────────────────────────────────
    summary_rows = ""
    all_positive = True
    any_data = False
    for wf_date, stats, _, uid in results:
        oos_days = (_today - wf_date).days
        if stats is None:
            summary_rows += (
                f'<tr>'
                f'<td style="{_tdl}">{wf_date}</td>'
                f'<td style="{_td}">{oos_days}日</td>'
                f'<td colspan="7" style="{_td};color:#64748b;text-align:center">'
                f'キャッシュなし — '
                f'<code style="font-size:0.78rem">python run_wf_history_scan.py --wf-until {wf_date}</code>'
                f' を事前実行してください</td>'
                f'</tr>\n'
            )
            all_positive = False
            continue
        any_data = True
        s  = stats.get("2_all_0_0",   {"n": 0, "wins": 0, "wr": 0, "pf": 0, "pnl": 0})
        sl = stats.get("2_long_0_0",  {"n": 0, "wins": 0, "wr": 0, "pf": 0, "pnl": 0})
        ss = stats.get("2_short_0_0", {"n": 0, "wins": 0, "wr": 0, "pf": 0, "pnl": 0})
        pnl = s["pnl"]
        pc  = _color(pnl)
        if pnl <= 0:
            all_positive = False
        verd = ('✓' if pnl > 0 and s["pf"] >= 1.0
                else '△' if pnl > 0
                else '✗')
        vc   = "#4ade80" if verd == "✓" else ("#fbbf24" if verd == "△" else "#f87171")
        summary_rows += (
            f'<tr>'
            f'<td style="{_tdl}">'
            f'<a href="#{uid}-anchor" onclick="wfhShowDate(\'{uid}\')" '
            f'style="color:#38bdf8">{wf_date}</a></td>'
            f'<td style="{_td}">{oos_days}日<br>'
            f'<span style="color:#64748b;font-size:0.75rem">({oos_days//30}ヶ月)</span></td>'
            f'<td style="{_td}">{s["n"]}件</td>'
            f'<td style="{_td}">{s["wr"]:.1f}%</td>'
            f'<td style="{_td}">{_pf_str(s["pf"])}</td>'
            f'<td style="{_td};color:{pc};font-weight:bold">{pnl:+,.0f}円</td>'
            f'<td style="{_td};color:{_color(sl["pnl"])}">{sl["pnl"]:+,.0f}円</td>'
            f'<td style="{_td};color:{_color(ss["pnl"])}">{ss["pnl"]:+,.0f}円</td>'
            f'<td style="{_td};text-align:center;color:{vc};font-size:1.1rem">{verd}</td>'
            f'</tr>\n'
        )

    if all_positive and any_data:
        verdict_bar = ('<div style="background:#1e293b;border-left:4px solid #4ade80;'
                       'padding:10px 18px;margin-bottom:20px;border-radius:0 6px 6px 0">'
                       '<span style="color:#4ade80;font-weight:bold">✓ 全期間プラス — '
                       'シグナルに複数期間での再現性あり</span></div>')
    elif any_data:
        verdict_bar = ('<div style="background:#1e293b;border-left:4px solid #fbbf24;'
                       'padding:10px 18px;margin-bottom:20px;border-radius:0 6px 6px 0">'
                       '<span style="color:#fbbf24;font-weight:bold">△ 一部マイナス期間あり — '
                       '継続検証を推奨</span></div>')
    else:
        verdict_bar = ('<div style="background:#1e293b;border-left:4px solid #64748b;'
                       'padding:10px 18px;margin-bottom:20px;border-radius:0 6px 6px 0">'
                       '<span style="color:#64748b">キャッシュなし — '
                       '<code>run_wf_history_scan.py</code> または '
                       '<code>run_wf_multi.py</code> を先に実行してください</span></div>')

    uncached_note = ""
    if uncached:
        cmds = " ".join(f"python run_wf_history_scan.py --wf-until {d} --min-price {min_price:.0f} "
                        f"--max-price {max_price:.0f} --no-browser" for d in uncached)
        uncached_note = (
            f'<details style="margin-bottom:16px">'
            f'<summary style="color:#fbbf24;cursor:pointer;font-size:0.82rem">'
            f'⚠ {len(uncached)}件 キャッシュ未作成の基準日 — クリックでコマンド表示</summary>'
            f'<pre style="background:#1e293b;padding:12px;border-radius:4px;'
            f'font-size:0.75rem;color:#94a3b8;margin-top:8px;overflow-x:auto">'
            f'{cmds}</pre></details>'
        )

    summary_table = f"""
<div style="overflow-x:auto;margin-bottom:20px">
<table style="border-collapse:collapse;min-width:780px;width:100%">
  <thead><tr>
    <th style="{_th}">基準日</th>
    <th style="{_th}">OOS期間</th>
    <th style="{_th}">取引数<br><span style="font-size:0.72rem;color:#64748b">≥2fold</span></th>
    <th style="{_th}">勝率</th>
    <th style="{_th}">PF</th>
    <th style="{_th}">損益（全体）</th>
    <th style="{_th}">損益（ロング）</th>
    <th style="{_th}">損益（ショート）</th>
    <th style="{_th}">判定</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>
</div>"""

    # ── 日付セレクタ + 各日付の詳細セクション ──────────────────────────────────
    date_tabs = ""
    detail_sections = ""
    first_with_data = None

    for i, (wf_date, stats, detail_html, uid) in enumerate(results):
        if stats is None:
            continue
        is_first = (first_with_data is None)
        if is_first:
            first_with_data = uid
        disp = "block" if is_first else "none"
        active_style = (
            "background:#1e40af;color:#e2e8f0;border-color:#38bdf8;font-weight:bold"
            if is_first else
            "background:#1e293b;color:#94a3b8;border-color:#334155"
        )
        date_tabs += (
            f'<button id="wfhdt-btn-{uid}" onclick="wfhShowDate(\'{uid}\')" '
            f'style="border:1px solid;border-radius:4px;padding:5px 14px;'
            f'margin-right:6px;cursor:pointer;font-size:0.82rem;{active_style}">'
            f'{wf_date}</button>'
        )
        detail_sections += (
            f'<div id="{uid}-anchor"></div>'
            f'<div id="wfhdt-pane-{uid}" style="display:{disp}">'
            f'{detail_html}'
            f'</div>\n'
        )

    tab_row = ""
    if date_tabs:
        tab_row = (
            f'<div style="margin-bottom:16px">'
            f'<span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">基準日を選択:</span>'
            f'{date_tabs}</div>'
        )

    switch_js = ""
    if first_with_data:
        all_uids_js = "[" + ",".join(f'"{uid}"' for _, _, html, uid in results if html) + "]"
        switch_js = f"""
<script>
(function() {{
  var _allUids = {all_uids_js};
  window.wfhShowDate = function(uid) {{
    _allUids.forEach(function(u) {{
      var p = document.getElementById("wfhdt-pane-" + u);
      var b = document.getElementById("wfhdt-btn-" + u);
      if (!p || !b) return;
      var active = u === uid;
      p.style.display = active ? "block" : "none";
      b.style.background = active ? "#1e40af" : "#1e293b";
      b.style.color = active ? "#e2e8f0" : "#94a3b8";
      b.style.borderColor = active ? "#38bdf8" : "#334155";
      b.style.fontWeight = active ? "bold" : "normal";
    }});
  }};
}})();
</script>"""

    return f"""
<div style="background:#0f172a;color:#e2e8f0;padding:20px;border-radius:8px;margin:12px 0">
  <h3 style="color:#38bdf8;margin:0 0 4px">クロス期間検証（複数基準日WF歴史検証）</h3>
  <p style="color:#64748b;font-size:0.82rem;margin:0 0 16px">
    複数の独立した時間軸でシグナルの再現性を検証。
    全期間プラス = フォワードテストを複数回やったのと等価の証拠。
    ≥2fold通過・WFスコアフィルタなし（すべて）の集計。
  </p>
  {verdict_bar}
  {uncached_note}
  {summary_table}
  {tab_row}
  {detail_sections}
  {switch_js}
</div>"""


def _oos_pnl_html(until_date, days: int, workers: int) -> str:
    """OOS（訓練前データ）バックテスト検証HTML。

    until_date: 検証終了日 (date オブジェクト)
    days:       検証期間（日数）
    workers:    並列数

    WATCHLIST × 全戦略 を until_date より前の days 日間でバックテストし、
    訓練前データでの戦略性能を検証する。
    """
    if not _SIGNALS_AVAILABLE:
        return '<p style="color:#64748b;padding:20px">シグナルモジュールが見つかりません</p>'

    from backtest_limit_entry import (
        fetch as _oos_fetch,
        run_limit_backtest as _oos_rbt,
        _TODAY as _oos_today,
    )
    from collections import defaultdict

    since = until_date - timedelta(days=days)
    backtest_days_adj = (_oos_today - since).days

    # 全configの全 (sym, strat) ペアを収集（デデュップ）
    all_tasks: list[tuple] = []  # (sym, name, strat, entry_type)
    seen_tasks: set = set()
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg["stop_wl"]:
            key = (sym, strat)
            if key not in seen_tasks:
                seen_tasks.add(key)
                is_short = strat.endswith("_S")
                etype = "stop_sell" if is_short else "stop"
                all_tasks.append((sym, name, strat, etype))
        for sym, name, strat in cfg["brk_wl"]:
            key = (sym, strat)
            if key not in seen_tasks:
                seen_tasks.add(key)
                is_short = strat.endswith("_S")
                etype = "stop_sell" if is_short else "stop"
                all_tasks.append((sym, name, strat, etype))

    def _run_one(sym: str, name: str, strat: str, etype: str) -> list[dict]:
        """1銘柄×1戦略のOOSバックテストを実行して trade_log を返す。"""
        try:
            mod = _mod_for(strat)
            params = mod.STRATEGY_PARAMS.get(strat)
            if params is None:
                return []
            calc_fn, em, sm, tm = params
            # min_start_date を指定して5年分のデータを取得
            df_full = _oos_fetch(sym, backtest_days_adj + 60, min_start_date=since)
            if df_full is None or df_full.empty:
                return []
            # until_date より後のデータを除去（未来データ漏洩防止）
            df_oos = df_full[df_full.index <= pd.Timestamp(until_date)].copy()
            if df_oos.empty or len(df_oos) < 30:
                return []
            result = _oos_rbt(
                sym, name, df_oos, calc_fn, em, sm, tm,
                backtest_days_adj, strat,
                entry_type=etype,
            )
            if result is None:
                return []
            trade_log = result.get("trade_log", [])
            # 未決済行を除外
            finished = [
                t for t in trade_log
                if t.get("reason") not in ("発注中", "保有中")
                and t.get("exit_dt") is not None
            ]
            # signal_dt でのデデュップ（同一シグナルの重複排除）
            seen_sig: set = set()
            deduped = []
            for t in finished:
                sdt = t.get("signal_dt")
                sk = (sym, strat, sdt)
                if sk not in seen_sig:
                    seen_sig.add(sk)
                    deduped.append({**t, "symbol": sym, "name": name, "strategy": strat})
            return deduped
        except Exception:
            return []

    # 並列実行
    all_trades: list[dict] = []
    with _TPE(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, sym, name, strat, etype): None
                for sym, name, strat, etype in all_tasks}
        for fut in _asc(futs):
            try:
                all_trades.extend(fut.result())
            except Exception:
                pass

    # ── BTスコア付与 ───────────────────────────────────────────────────────
    for _t in all_trades:
        _k = (_t.get("symbol"), _t.get("strategy"))
        _t["_bt"] = _OOS_BT_SCORES.get(_k)

    # ── 集計用 dicts ──────────────────────────────────────────────────────
    def _empty_stat():
        return {"wins": 0, "losses": 0, "pnl": 0.0, "win_pnl": 0.0, "loss_pnl": 0.0}

    strat_stats: dict[str, dict] = defaultdict(_empty_stat)
    band_stats: dict[str, dict]  = defaultdict(_empty_stat)
    sym_strat_stats: dict[tuple, dict] = defaultdict(_empty_stat)
    sym_strat_name: dict[tuple, str] = {}
    monthly: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    # monthly per band: band_key -> {mon -> {wins, losses, pnl}}
    band_monthly: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0}))
    annual: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})

    def _bt_band(bt) -> str:
        if bt is None:
            return "スコア不明"
        if bt >= 80:
            return "★★★ BT≥80"
        if bt >= 60:
            return "★★ BT60-79"
        if bt >= 40:
            return "★ BT40-59"
        return "△ BT<40"

    for t in all_trades:
        strat  = t.get("strategy", "")
        sym    = t.get("symbol", "")
        name   = t.get("name", "")
        pnl    = float(t.get("pnl", 0.0))
        is_win = pnl > 0
        bt     = t.get("_bt")
        band   = _bt_band(bt)

        def _upd(s, pnl, is_win):
            s["pnl"] += pnl
            if is_win:
                s["wins"]    += 1
                s["win_pnl"] += pnl
            else:
                s["losses"]    += 1
                s["loss_pnl"]  += pnl

        _upd(strat_stats[strat], pnl, is_win)
        _upd(band_stats[band], pnl, is_win)
        _upd(sym_strat_stats[(sym, strat)], pnl, is_win)
        sym_strat_name[(sym, strat)] = name

        sdt = t.get("signal_dt")
        if sdt is not None:
            mon = str(sdt)[:7] if isinstance(sdt, str) else sdt.strftime("%Y-%m")
            yr  = mon[:4]
            m = monthly[mon]
            m["pnl"] += pnl
            if is_win:
                m["wins"] += 1
            else:
                m["losses"] += 1
            bm = band_monthly[band][mon]
            bm["pnl"] += pnl
            if is_win:
                bm["wins"] += 1
            else:
                bm["losses"] += 1
            ay = annual[yr]
            ay["pnl"] += pnl
            if is_win:
                ay["wins"] += 1
            else:
                ay["losses"] += 1

    # ── helper functions ───────────────────────────────────────────────────
    def _pf(wins: int, win_pnl: float, losses: int, loss_pnl: float) -> str:
        if losses == 0 or loss_pnl == 0:
            return "∞" if win_pnl > 0 else "0"
        pf_val = win_pnl / abs(loss_pnl)
        return f"{pf_val:.2f}"

    def _wr(wins: int, total: int) -> str:
        if total == 0:
            return "-"
        return f"{wins / total * 100:.1f}%"

    def _pnl_c(v: float) -> str:
        return "#4ade80" if v > 0 else ("#f87171" if v < 0 else "#94a3b8")

    def _summary_row(label: str, s: dict, bold: bool = False) -> str:
        n   = s["wins"] + s["losses"]
        pnl = s["pnl"]
        w   = s["wins"]
        l   = s["losses"]
        avg = pnl / n if n > 0 else 0.0
        wc  = _pnl_c(pnl)
        sty = ' style="border-top:2px solid #334155;font-weight:bold"' if bold else ""
        return (
            f'<tr{sty}>'
            f'<td style="color:#e2e8f0">{label}</td>'
            f'<td style="color:#e2e8f0;text-align:center">{n}</td>'
            f'<td style="text-align:center;color:#4ade80">{w}</td>'
            f'<td style="text-align:center;color:#f87171">{l}</td>'
            f'<td style="text-align:center">{_wr(w, n)}</td>'
            f'<td style="text-align:center">{_pf(w, s["win_pnl"], l, s["loss_pnl"])}</td>'
            f'<td style="text-align:right;color:{wc}">{pnl:+,.0f}円</td>'
            f'<td style="text-align:right;color:{_pnl_c(avg)}">{avg:+,.0f}円</td>'
            f'</tr>\n'
        )

    td = "border:1px solid #1e293b;padding:6px 10px"
    th = f"{td};background:#1e293b;color:#94a3b8;text-align:center"

    summary_thead = f"""<thead><tr>
      <th style="{th}">戦略</th>
      <th style="{th}">取引数</th>
      <th style="{th}">勝</th>
      <th style="{th}">負</th>
      <th style="{th}">勝率</th>
      <th style="{th}">PF</th>
      <th style="{th}">損益合計</th>
      <th style="{th}">平均損益</th>
    </tr></thead>"""

    # ── Section 1: 戦略別サマリー ──────────────────────────────────────────
    strat_rows = ""
    total_s = _empty_stat()
    for strat in sorted(strat_stats.keys()):
        s = strat_stats[strat]
        strat_rows += _summary_row(strat, s)
        total_s["wins"]     += s["wins"]
        total_s["losses"]   += s["losses"]
        total_s["pnl"]      += s["pnl"]
        total_s["win_pnl"]  += s["win_pnl"]
        total_s["loss_pnl"] += s["loss_pnl"]
    strat_rows += _summary_row("合計", total_s, bold=True)
    total_n = total_s["wins"] + total_s["losses"]

    # ── Section 2: BTスコア帯別サマリー ───────────────────────────────────
    band_order = ["★★★ BT≥80", "★★ BT60-79", "★ BT40-59", "△ BT<40", "スコア不明"]
    band_colors = {
        "★★★ BT≥80":  "#4ade80",
        "★★ BT60-79": "#60a5fa",
        "★ BT40-59":  "#fbbf24",
        "△ BT<40":    "#f87171",
        "スコア不明":  "#94a3b8",
    }
    band_rows = ""
    band_total = _empty_stat()
    for band in band_order:
        if band not in band_stats:
            continue
        s = band_stats[band]
        bc = band_colors.get(band, "#e2e8f0")
        n   = s["wins"] + s["losses"]
        pnl = s["pnl"]
        w   = s["wins"]
        l   = s["losses"]
        avg = pnl / n if n > 0 else 0.0
        wc  = _pnl_c(pnl)
        band_rows += (
            f'<tr>'
            f'<td style="color:{bc}">{band}</td>'
            f'<td style="color:#e2e8f0;text-align:center">{n}</td>'
            f'<td style="text-align:center;color:#4ade80">{w}</td>'
            f'<td style="text-align:center;color:#f87171">{l}</td>'
            f'<td style="text-align:center">{_wr(w, n)}</td>'
            f'<td style="text-align:center">{_pf(w, s["win_pnl"], l, s["loss_pnl"])}</td>'
            f'<td style="text-align:right;color:{wc}">{pnl:+,.0f}円</td>'
            f'<td style="text-align:right;color:{_pnl_c(avg)}">{avg:+,.0f}円</td>'
            f'</tr>\n'
        )
        band_total["wins"]     += s["wins"]
        band_total["losses"]   += s["losses"]
        band_total["pnl"]      += s["pnl"]
        band_total["win_pnl"]  += s["win_pnl"]
        band_total["loss_pnl"] += s["loss_pnl"]
    band_rows += _summary_row("合計", band_total, bold=True)

    # ── Section 3: 銘柄×戦略別サマリー（上位30） ──────────────────────────
    sym_strat_sorted = sorted(
        sym_strat_stats.items(),
        key=lambda kv: kv[1]["pnl"],
        reverse=True,
    )[:30]
    sym_rows = ""
    for (sym, strat), s in sym_strat_sorted:
        name = sym_strat_name.get((sym, strat), "")
        bt_val = _OOS_BT_SCORES.get((sym, strat))
        bt_str = f"{bt_val:.0f}" if bt_val is not None else "-"
        n   = s["wins"] + s["losses"]
        pnl = s["pnl"]
        w   = s["wins"]
        l   = s["losses"]
        avg = pnl / n if n > 0 else 0.0
        rc  = _pnl_c(pnl)
        sym_rows += (
            f'<tr>'
            f'<td style="color:#e2e8f0">{sym}</td>'
            f'<td style="color:#94a3b8;font-size:0.8rem">{name}</td>'
            f'<td style="color:#e2e8f0">{strat}</td>'
            f'<td style="text-align:center;color:#60a5fa">{bt_str}</td>'
            f'<td style="color:#e2e8f0;text-align:center">{n}</td>'
            f'<td style="text-align:center;color:#4ade80">{w}</td>'
            f'<td style="text-align:center;color:#f87171">{l}</td>'
            f'<td style="text-align:center">{_wr(w, n)}</td>'
            f'<td style="text-align:center">{_pf(w, s["win_pnl"], l, s["loss_pnl"])}</td>'
            f'<td style="text-align:right;color:{rc}">{pnl:+,.0f}円</td>'
            f'<td style="text-align:right;color:{_pnl_c(avg)}">{avg:+,.0f}円</td>'
            f'</tr>\n'
        )
    if not sym_rows:
        sym_rows = '<tr><td colspan="11" style="text-align:center;color:#64748b;padding:12px">取引なし</td></tr>'

    # ── Section 4: 年次内訳 ────────────────────────────────────────────────
    annual_rows = ""
    cum_pnl = 0.0
    for yr in sorted(annual.keys()):
        ay  = annual[yr]
        n   = ay["wins"] + ay["losses"]
        pnl = ay["pnl"]
        cum_pnl += pnl
        yc  = _pnl_c(pnl)
        cc  = _pnl_c(cum_pnl)
        annual_rows += (
            f'<tr>'
            f'<td style="color:#e2e8f0">{yr}</td>'
            f'<td style="text-align:center;color:#4ade80">{ay["wins"]}</td>'
            f'<td style="text-align:center;color:#f87171">{ay["losses"]}</td>'
            f'<td style="text-align:center">{_wr(ay["wins"], n)}</td>'
            f'<td style="text-align:right;color:{yc}">{pnl:+,.0f}円</td>'
            f'<td style="text-align:right;color:{cc}">{cum_pnl:+,.0f}円</td>'
            f'</tr>\n'
        )
    if not annual_rows:
        annual_rows = '<tr><td colspan="6" style="text-align:center;color:#64748b;padding:12px">取引なし</td></tr>'

    # ── Section 5: 月次内訳（BTバンド切替タブ付き） ───────────────────────
    import random as _random
    _uid = f"{id(all_trades):x}"

    def _mon_rows_for(band_key: str | None) -> str:
        if band_key is None:
            src = monthly
        else:
            src = band_monthly.get(band_key, {})
        rows = ""
        for mon in sorted(src.keys()):
            m   = src[mon]
            n   = m["wins"] + m["losses"]
            pnl = m["pnl"]
            mc  = "#4ade80" if pnl > 0 else "#f87171"
            rows += (
                f'<tr>'
                f'<td style="{td};color:#e2e8f0">{mon}</td>'
                f'<td style="{td};text-align:center;color:#4ade80">{m["wins"]}</td>'
                f'<td style="{td};text-align:center;color:#f87171">{m["losses"]}</td>'
                f'<td style="{td};text-align:center">{_wr(m["wins"], n)}</td>'
                f'<td style="{td};text-align:right;color:{mc}">{pnl:+,.0f}円</td>'
                f'</tr>\n'
            )
        if not rows:
            rows = f'<tr><td colspan="5" style="text-align:center;color:#64748b;padding:12px">取引なし</td></tr>'
        return rows

    mon_band_tabs = [
        ("全体", None),
        ("★★★ BT≥80", "★★★ BT≥80"),
        ("★★ BT60-79", "★★ BT60-79"),
        ("★ BT40-59", "★ BT40-59"),
        ("△ BT<40", "△ BT<40"),
    ]
    mon_tab_btns = ""
    mon_tab_panes = ""
    for i, (label, bk) in enumerate(mon_band_tabs):
        pane_id = f"oos_mon_{_uid}_{i}"
        active_btn = "background:#1e40af;color:#fff" if i == 0 else "background:#1e293b;color:#94a3b8"
        display    = "block" if i == 0 else "none"
        mon_tab_btns += (
            f'<button id="btn_{pane_id}" onclick="switchOosMon_{_uid}({i})" '
            f'style="border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:0.8rem;{active_btn}">'
            f'{label}</button> '
        )
        mon_tab_panes += (
            f'<div id="{pane_id}" style="display:{display}">'
            f'<table style="width:100%;border-collapse:collapse;font-size:0.85rem">'
            f'<thead><tr>'
            f'<th style="{th}">月</th><th style="{th}">勝</th>'
            f'<th style="{th}">負</th><th style="{th}">勝率</th><th style="{th}">損益</th>'
            f'</tr></thead>'
            f'<tbody>{_mon_rows_for(bk)}</tbody>'
            f'</table></div>\n'
        )

    mon_js = f"""<script>
function switchOosMon_{_uid}(idx) {{
  var tabs = {[f"oos_mon_{_uid}_{i}" for i in range(len(mon_band_tabs))]};
  var btns = {[f"btn_oos_mon_{_uid}_{i}" for i in range(len(mon_band_tabs))]};
  for (var i = 0; i < tabs.length; i++) {{
    document.getElementById(tabs[i]).style.display = (i === idx) ? 'block' : 'none';
    document.getElementById(btns[i]).style.background = (i === idx) ? '#1e40af' : '#1e293b';
    document.getElementById(btns[i]).style.color = (i === idx) ? '#fff' : '#94a3b8';
  }}
}}
</script>"""

    html = f"""
<div style="background:#0f172a;color:#e2e8f0;padding:20px;border-radius:8px;margin:12px 0">
  <h3 style="color:#38bdf8;margin:0 0 4px">OOS検証（訓練前データ: {since} 〜 {until_date}）</h3>
  <p style="color:#64748b;font-size:0.82rem;margin:0 0 16px">
    WF訓練開始前データでの検証 — 銘柄選定に一切使用していない期間
    ({days}日間 / 取引数: {total_n}件)
  </p>

  <h4 style="color:#94a3b8;margin:0 0 8px">① 戦略別サマリー</h4>
  <table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-bottom:20px">
    {summary_thead}
    <tbody>{strat_rows}</tbody>
  </table>

  <h4 style="color:#94a3b8;margin:0 0 8px">② BTスコア帯別サマリー</h4>
  <table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-bottom:20px">
    <thead><tr>
      <th style="{th}">BTスコア帯</th>
      <th style="{th}">取引数</th>
      <th style="{th}">勝</th>
      <th style="{th}">負</th>
      <th style="{th}">勝率</th>
      <th style="{th}">PF</th>
      <th style="{th}">損益合計</th>
      <th style="{th}">平均損益</th>
    </tr></thead>
    <tbody>{band_rows}</tbody>
  </table>

  <h4 style="color:#94a3b8;margin:0 0 8px">③ 銘柄×戦略別成績（損益上位30）</h4>
  <table style="width:100%;border-collapse:collapse;font-size:0.82rem;margin-bottom:20px">
    <thead><tr>
      <th style="{th}">コード</th>
      <th style="{th}">銘柄名</th>
      <th style="{th}">戦略</th>
      <th style="{th}">BTスコア</th>
      <th style="{th}">取引数</th>
      <th style="{th}">勝</th>
      <th style="{th}">負</th>
      <th style="{th}">勝率</th>
      <th style="{th}">PF</th>
      <th style="{th}">損益合計</th>
      <th style="{th}">平均損益</th>
    </tr></thead>
    <tbody>{sym_rows}</tbody>
  </table>

  <h4 style="color:#94a3b8;margin:0 0 8px">④ 年次内訳</h4>
  <table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-bottom:20px">
    <thead><tr>
      <th style="{th}">年</th>
      <th style="{th}">勝</th>
      <th style="{th}">負</th>
      <th style="{th}">勝率</th>
      <th style="{th}">損益</th>
      <th style="{th}">累計損益</th>
    </tr></thead>
    <tbody>{annual_rows}</tbody>
  </table>

  <h4 style="color:#94a3b8;margin:0 0 8px">⑤ 月次内訳</h4>
  <div style="margin-bottom:8px">{mon_tab_btns}</div>
  {mon_tab_panes}
</div>
{mon_js}"""
    return html


def build_max_hold_comparison_html(hold_list: list[int], days: int, workers: int,
                                    compare_modes: bool = False) -> str:
    """MAX_HOLD別の損益比較セクションを生成（詳細分析タブ用）。

    hold_list: 比較する最大保有日数 (例: [7, 10, 15, 20])
    days: 集計対象期間
    workers: 並列数
    compare_modes: True=conservative/aggressive 両方を比較表示
    """
    if not _SIGNALS_AVAILABLE or not hold_list:
        return ""

    # 全 WATCHLIST を収集（重複除去）
    seen: set = set()
    unique_items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                unique_items.append((sym, name, strat))
    if not unique_items:
        return ""

    since = _TODAY - timedelta(days=days)

    def _collect(mh: int) -> dict:
        trades: list[dict] = []
        _errors = 0
        with _TPE(max_workers=workers) as ex:
            futs = {ex.submit(_mod_for(strat).backtest_one, sym, name, strat, mh): (sym, strat)
                    for sym, name, strat in unique_items}
            for fut in _asc(futs):
                sym_st = futs[fut]
                try:
                    r = fut.result()
                    if not r:
                        continue
                    pr = r.get("period_results", {})
                    max_p = max(pr.keys()) if pr else None
                    if max_p is None:
                        continue
                    for t in pr[max_p].get("trade_log", []):
                        sig = t.get("signal_dt")
                        if not sig:
                            continue
                        sig_date = sig.date() if hasattr(sig, "date") else sig
                        if (sig_date >= since
                                and t.get("reason") not in ("発注中", "保有中")):
                            trades.append(t)
                except Exception as _e:
                    _errors += 1
                    if _errors <= 3:
                        print(f"  [max_hold比較] エラー {sym_st}: {_e}", flush=True)
        if _errors:
            print(f"  [max_hold比較] MAX_HOLD={mh}: {len(trades)}件 (エラー{_errors}件)", flush=True)

        n = len(trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        gp = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
        gl = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        timecuts = sum(1 for t in trades if t.get("reason") == "タイムカット")
        avg_hold = sum(t.get("hold_days", 0) for t in trades) / n if n else 0.0
        return {
            "trades": n, "wins": wins,
            "win_rate": wins / n * 100 if n else 0.0,
            "total_pnl": sum(t.get("pnl", 0) for t in trades),
            "pf": pf, "avg_hold": avg_hold, "timecuts": timecuts,
        }

    def _build_table(results: dict) -> str:
        best_mh = max(hold_list, key=lambda m: results[m]["total_pnl"])
        rows = ""
        for mh in hold_list:
            r = results[mh]
            pnl = r["total_pnl"]
            pnl_str = f"+{pnl:,.0f}" if pnl > 0 else f"{pnl:,.0f}"
            pnl_color = "#4ade80" if pnl > 0 else "#f87171" if pnl < 0 else "#94a3b8"
            pf_str = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "∞"
            bg = "background:#172032;" if mh == best_mh else ""
            badge = (' <span style="font-size:0.6rem;background:#4ade80;color:#052e16;'
                     'padding:1px 4px;border-radius:3px;vertical-align:middle">最良</span>'
                     if mh == best_mh else "")
            rows += (
                f'<tr style="{bg}">'
                f'<td style="padding:6px 10px;font-weight:700;color:#e2e8f0">最大{mh}日{badge}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{r["trades"]:,}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{r["win_rate"]:.1f}%</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#93c5fd">{pf_str}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{r["avg_hold"]:.1f}日</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#fbbf24">{r["timecuts"]:,}</td>'
                f'<td style="padding:6px 10px;text-align:right;font-weight:700;color:{pnl_color}">{pnl_str}円</td>'
                f'</tr>\n'
            )
        return (
            f'<table style="width:100%;border-collapse:collapse;font-size:0.88rem">'
            f'<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.78rem">'
            f'<th style="padding:5px 10px;text-align:left">最大保有</th>'
            f'<th style="padding:5px 10px;text-align:right">件数</th>'
            f'<th style="padding:5px 10px;text-align:right">勝率</th>'
            f'<th style="padding:5px 10px;text-align:right">PF</th>'
            f'<th style="padding:5px 10px;text-align:right">平均保有日</th>'
            f'<th style="padding:5px 10px;text-align:right">タイムカット</th>'
            f'<th style="padding:5px 10px;text-align:right">損益合計</th>'
            f'</tr></thead>'
            f'<tbody>{rows}</tbody>'
            f'</table>'
        )

    if compare_modes:
        # Conservative
        print(f"  [max_hold比較] conservative 集計中...", flush=True)
        _set_sig_params("conservative")
        con_results: dict[int, dict] = {}
        for mh in hold_list:
            print(f"    MAX_HOLD={mh}日...", flush=True)
            con_results[mh] = _collect(mh)
        # Aggressive
        print(f"  [max_hold比較] aggressive 集計中...", flush=True)
        _set_sig_params("aggressive")
        agg_results: dict[int, dict] = {}
        for mh in hold_list:
            print(f"    MAX_HOLD={mh}日...", flush=True)
            agg_results[mh] = _collect(mh)
        # Restore conservative
        _set_sig_params("conservative")

        con_table = _build_table(con_results)
        agg_table = _build_table(agg_results)
        return (
            f'<div style="margin:0 0 16px;padding:16px 20px;background:#1e293b;'
            f'border-radius:8px;border-left:3px solid #60a5fa">'
            f'<h4 style="margin:0 0 10px;color:#60a5fa;font-size:0.9rem">Conservative（目標3R・損切り1.5ATR）</h4>'
            f'{con_table}</div>'
            f'<div style="padding:16px 20px;background:#1e293b;'
            f'border-radius:8px;border-left:3px solid #f97316">'
            f'<h4 style="margin:0 0 10px;color:#f97316;font-size:0.9rem">Aggressive（目標1.5R・損切り1ATR）</h4>'
            f'{agg_table}</div>'
        )
    else:
        results: dict[int, dict] = {}
        for mh in hold_list:
            print(f"  [max_hold比較] MAX_HOLD={mh}日 集計中...", flush=True)
            results[mh] = _collect(mh)
        return (
            f'<div style="margin:0 0 24px;padding:16px 20px;background:#1e293b;'
            f'border-radius:8px;border-left:3px solid #a78bfa">'
            f'<h4 style="margin:0 0 12px;color:#a78bfa;font-size:0.95rem">'
            f'⏱ 最大保有日数 比較（直近{days}日）</h4>'
            f'{_build_table(results)}</div>'
        )


def _tab5_pnl_html(days: int, workers: int, cfg_filter: str | None = None,
                   symbol_filter: list[str] | None = None,
                   entry_days: int | None = None,
                   skip_timing9: bool = False,
                   preoos_cutoff_days: int | None = None) -> str:
    """タブ5: 直近N日 取引損益レポート。cfg_filter 指定時は対象configのみ表示。
    entry_days 指定時は「エントリー日が直近N日以内」の取引だけを取引明細に表示する。
    skip_timing9=True なら⑨Rolling/em比較をスキップ（期間フィルタタブ用に軽量化）。
    preoos_cutoff_days 指定時は「today-N日以前のデータのみ」でBTスコアを再計算し
    OOS前BTスコア別成績タブを追加（メインBTスコアは変更しない）。"""
    if not _SIGNALS_AVAILABLE:
        return '<p style="color:#64748b;padding:20px">シグナルモジュールが見つかりません</p>'

    import gc
    from collections import defaultdict
    until = _TODAY
    since = until - timedelta(days=days)

    # ── バックテスト結果キャッシュ ─────────────────────────────────────────────
    # 同一セッション内で複数の days で呼ばれる場合、バックテスト自体は1回だけ実行する。
    # days の違いは後段のフィルタで対応するため再実行は不要。
    _sym_filter_key = tuple(sorted(symbol_filter)) if symbol_filter else None
    _cfg_cache_key = (
        tuple((c["label"], c["mode"], str(c.get("sm_tm")),
               tuple(c.get("stop_wl", [])), tuple(c.get("brk_wl", []))) for c in _PNL_CONFIGS),
        _sym_filter_key,
    )

    if _cfg_cache_key not in _pnl_bt_cache:
        # バックテスト実行（初回のみ）
        _cached_items_per_cfg: dict[str, list] = {}
        for cfg in _PNL_CONFIGS:
            _set_sig_params(cfg["mode"], cfg.get("sm_tm"))
            _wl_stop = cfg["stop_wl"]
            _wl_brk  = cfg["brk_wl"]
            if symbol_filter:
                _wl_stop = [(s, n, st) for s, n, st in _wl_stop if s in symbol_filter]
                _wl_brk  = [(s, n, st) for s, n, st in _wl_brk  if s in symbol_filter]
            items: list[dict] = []
            with _TPE(max_workers=workers) as ex:
                futs = {}
                for sym, name, strat in _wl_stop:
                    futs[ex.submit(_mod_for(strat).backtest_one, sym, name, strat)] = None
                for sym, name, strat in _wl_brk:
                    futs[ex.submit(_mod_for(strat).backtest_one, sym, name, strat)] = None
                for fut in _asc(futs):
                    try:
                        r = fut.result()
                        if r:
                            items.append(r)
                    except Exception:
                        pass
            _cached_items_per_cfg[cfg["label"]] = items
        _pnl_bt_cache[_cfg_cache_key] = _cached_items_per_cfg
        gc.collect()
        print(f"  [cache] バックテスト結果をキャッシュ済み (key={len(_pnl_bt_cache)}件)", flush=True)
    else:
        _cached_items_per_cfg = _pnl_bt_cache[_cfg_cache_key]
        print(f"  [cache] バックテスト結果を再利用 (days={days})", flush=True)

    # ── OOS前BTスコア計算（preoos_cutoff_days 指定時のみ）──────────────────────
    # today - preoos_cutoff_days 以前のデータだけでBTスコアを計算し、
    # 各取引に preoos_score を付与する。メインのBTスコア（rec_score）は変更しない。
    _preoos_score_map: dict = {}
    if preoos_cutoff_days:
        _all_sym_strats: set = set()
        for _cfg in _PNL_CONFIGS:
            for _it in _cached_items_per_cfg.get(_cfg["label"], []):
                _s2, _st2 = _it.get("symbol", ""), _it.get("strategy", "")
                if _s2 and _st2:
                    _all_sym_strats.add((_s2, _st2))
        if _all_sym_strats:
            print(f"  [preoos] OOS前BTスコア計算中 ({len(_all_sym_strats)}件, cutoff={preoos_cutoff_days}日前)...", flush=True)
            with _TPE(max_workers=workers) as _pex:
                _pfuts = {
                    _pex.submit(_calc_preoos_bt_score_for_tab5, _s2, _st2, preoos_cutoff_days): (_s2, _st2)
                    for _s2, _st2 in _all_sym_strats
                }
                for _pf in _asc(_pfuts):
                    _ps, _pst = _pfuts[_pf]
                    try:
                        _preoos_score_map[(_ps, _pst)] = _pf.result()
                    except Exception:
                        _preoos_score_map[(_ps, _pst)] = 0
            print(f"  [preoos] 完了 (cached={len(_preoos_tab5_score_cache)}件)", flush=True)

    # ── シグナル時点BTスコア計算（月次バケット化、既存 trade_log 流用）──────────
    # 各取引の signal_dt の月初時点のBTスコアを事前計算する。
    # _SIGNAL_DATE_BT_SCORES: run_signals_holdout_all.py から注入される
    # (sym, strat, signal_date_str) → シグナル発生時のBTスコア
    # キャッシュにない場合は rec_score2（今日のスコア）をフォールバックとして使用

    all_trades: list[dict] = []        # デデュップ済み（総KPI・取引リスト用）
    full_year_trades: list[dict] = []  # デデュップ済み（スコア別実績用）
    cfg_trades_map: dict = {}          # config別取引（サマリーテーブル用・デデュップなし）
    # 取引リスト表示用: 同一 (sym, strat, signal_dt) は最初のconfig分だけ表示
    seen_global: set = set()

    for cfg in _PNL_CONFIGS:
        items = _cached_items_per_cfg.get(cfg["label"], [])

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
            _OOS_BT_SCORES[(sym, strat)] = rec_score2
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
                entry_d = entry_dt.date() if hasattr(entry_dt, "date") else entry_dt
                _preoos_sc = _preoos_score_map.get((sym, strat)) if _preoos_score_map else None
                # シグナル時点BTスコア（優先順: ①キャッシュ済み発生時スコア → ②今日のスコア）
                # signal_score_cache.json にある場合は発生時スコア固定、なければ今日のスコア
                _sdt_for_key = t.get("signal_dt")
                _sd_for_key = _sdt_for_key.date() if hasattr(_sdt_for_key, "date") else _sdt_for_key
                _cache_key = (sym, strat, str(_sd_for_key)) if _sd_for_key else None
                if _cache_key and _cache_key in _SIGNAL_DATE_BT_SCORES:
                    _sig_sc = _SIGNAL_DATE_BT_SCORES[_cache_key]
                else:
                    _sig_sc = rec_score2
                # signal_score からランクを決定
                if _sig_sc >= 80: _sig_rank = "★★★"
                elif _sig_sc >= 60: _sig_rank = "★★"
                elif _sig_sc >= 40: _sig_rank = "★"
                else: _sig_rank = "△"
                base = {"label": cfg["label"], "color": cfg["color"],
                        "symbol": sym, "name": name, "strategy": strat,
                        "score": _sig_sc, "rank": _sig_rank,
                        "is_wf": is_wf2, "wf_score": wf_score2,
                        "rec_score": _sig_sc,   # BT表示・フィルタも発生時スコアで統一
                        "preoos_score": _sig_sc,
                        "entry_d_raw": entry_d, "exit_d_raw": exit_d,
                        "pnl": t.get("pnl", 0), "reason": reason}
                _sdt_raw = t.get("signal_dt")
                extra = {
                    "entry_dt":     entry_dt.strftime("%m/%d") if hasattr(entry_dt, "strftime") else str(entry_dt),
                    "exit_dt":      exit_dt.strftime("%m/%d")  if hasattr(exit_dt,  "strftime") else str(exit_dt),
                    "entry_p":      t.get("entry_p", 0),
                    "exit_p":       t.get("exit_p", 0),
                    "qty":          t.get("qty", 0),
                    "hold_days":    t.get("hold_days", 0),
                    "days_neg":     t.get("days_neg", 0),
                    "days_to_fill": t.get("days_to_fill", 0),
                    "reason":       reason,
                    "order_limit":  t.get("order_limit", 0),
                    "order_stop":   t.get("order_stop", 0),
                    "order_target": t.get("order_target", 0),
                    "signal_dt_raw": _sdt_raw.date() if hasattr(_sdt_raw, "date") else _sdt_raw,
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
                if reason != "発注中" and since <= exit_d <= until:
                    full_year_trades.append(base)
                # 取引明細テーブルには発注中も表示
                if since <= exit_d <= until:
                    all_trades.append({**base, **extra})

    # reset to conservative
    _stop.STRATEGY_PARAMS.update(_CON_STOP)
    _brk.STRATEGY_PARAMS.update(_CON_BRK)
    if _short is not None:
        _short.STRATEGY_PARAMS.update(_CON_SHORT)
        _sbrk.STRATEGY_PARAMS.update(_CON_SBRK)

    # ── cfg_filter: 対象configのみに絞り込み ──
    if cfg_filter:
        all_trades       = [t for t in all_trades       if t.get("label") == cfg_filter]
        full_year_trades = [t for t in full_year_trades if t.get("label") == cfg_filter]
        cfg_trades_map   = {k: [t for t in v if t.get("label") == cfg_filter]
                            for k, v in cfg_trades_map.items()}

    # ── symbol_filter: 銘柄コードで絞り込み ──
    if symbol_filter:
        syms = {s.upper() for s in symbol_filter}
        all_trades       = [t for t in all_trades       if t.get("symbol","").upper() in syms]
        full_year_trades = [t for t in full_year_trades if t.get("symbol","").upper() in syms]
        cfg_trades_map   = {k: [t for t in v if t.get("symbol","").upper() in syms]
                            for k, v in cfg_trades_map.items()}

    # ── entry_days: エントリー日ベースで取引明細を絞り込み ──
    # (スコア統計・サマリーには影響しない。明細テーブルのみ)
    if entry_days:
        entry_since = until - timedelta(days=entry_days)
        all_trades = [t for t in all_trades
                      if (t.get("entry_d_raw") or until) >= entry_since]

    # ── 1銘柄1ポジションフィルター ──────────────────────────────────
    # エントリー日昇順・スコア降順でソートし、同一銘柄が既にオープン中の場合はスキップ
    def _one_pos_filter(trades: list[dict], dropped: list | None = None) -> list[dict]:
        from datetime import date as _date
        sorted_t = sorted(
            trades,
            key=lambda t: (t.get("entry_d_raw") or _date.min, -(t.get("score") or 0))
        )
        open_pos: dict[str, _date] = {}  # sym → exit_d (その日まで保有中)
        result = []
        for t in sorted_t:
            sym     = t.get("symbol", "")
            entry_d = t.get("entry_d_raw")
            exit_d  = t.get("exit_d_raw")
            if not sym or entry_d is None:
                result.append(t)
                continue
            if sym in open_pos and open_pos[sym] >= entry_d:
                # 既にオープンポジションあり → 計測には入れずスキップ。
                # dropped が渡されていれば「重複シグナル」として回収し、
                # 取引明細に参考表示できるようにする。
                if dropped is not None:
                    t["_overlap"] = True
                    dropped.append(t)
                continue
            open_pos[sym] = exit_d if exit_d else entry_d
            result.append(t)
        result.sort(key=lambda t: t.get("exit_d_raw") or _date.min, reverse=True)
        return result

    _overlap_dropped: list[dict] = []
    all_trades       = _one_pos_filter(all_trades, dropped=_overlap_dropped)
    full_year_trades = _one_pos_filter(full_year_trades)

    # ── KPI (発注中=未約定は除外) ──
    # all_trades は (sym, strat, signal_dt) で重複除外済み（同一シグナルは最初のconfigのみ）
    kpi_trades = [t for t in all_trades if t.get("reason") != "発注中"]

    # ── 日経トレンド別成績 ──────────────────────────────────────────────────────
    _trend_breakdown_html = ""
    try:
        _n225_close = fetch_n225(2)
        _n225_trend = label_trend(_n225_close)
        _tmap = {dt.date(): tr for dt, tr in zip(_n225_trend.index, _n225_trend)}

        from collections import defaultdict as _ddict
        _tbuckets: dict = _ddict(list)
        for _t in kpi_trades:
            _sdt = _t.get("signal_dt_raw")
            _trend_key = _tmap.get(_sdt) if _sdt else None
            _tbuckets[_trend_key].append(_t)

        # ロング: ▲上昇=有利（緑）/ ショート: ▼下落=有利（緑）
        _is_short = _IS_SHORT_MODE
        _tlabels = {
            "up":       ("▲ 上昇",   "#4ade80" if not _is_short else "#f87171",
                         "#052e16"  if not _is_short else "#2d0a0a"),
            "sideways": ("→ 横ばい", "#fbbf24", "#2d1f00"),
            "down":     ("▼ 下落",   "#f87171" if not _is_short else "#4ade80",
                         "#2d0a0a"  if not _is_short else "#052e16"),
            None:       ("不明",     "#64748b", "#0d1424"),
        }
        # ショートは ▼→→▲ の順（有利→不利）
        _order = ["down", "sideways", "up", None] if _is_short else ["up", "sideways", "down", None]
        # 有利トレンドの期待バッジ
        _fav_badge = {
            "up":       ('<span style="font-size:0.7rem;color:#4ade80;margin-left:4px">✓ロング有利</span>'
                         if not _is_short else
                         '<span style="font-size:0.7rem;color:#f87171;margin-left:4px">✗ショート不利</span>'),
            "sideways": '<span style="font-size:0.7rem;color:#fbbf24;margin-left:4px">→中立</span>',
            "down":     ('<span style="font-size:0.7rem;color:#f87171;margin-left:4px">✗ロング不利</span>'
                         if not _is_short else
                         '<span style="font-size:0.7rem;color:#4ade80;margin-left:4px">✓ショート有利</span>'),
            None:       "",
        }
        _trows = ""
        for _tk in _order:
            _ts = _tbuckets.get(_tk, [])
            if not _ts:
                continue
            _wins   = [_t for _t in _ts if _t["pnl"] > 0]
            _losses = [_t for _t in _ts if _t["pnl"] <= 0]
            _tpnl   = sum(_t["pnl"] for _t in _ts)
            _twr    = len(_wins) / len(_ts) * 100
            _avg_w  = sum(_t["pnl"] for _t in _wins)  / len(_wins)  if _wins   else 0
            _avg_l  = sum(_t["pnl"] for _t in _losses) / len(_losses) if _losses else 0
            _gp     = sum(_t["pnl"] for _t in _wins)
            _gl     = abs(sum(_t["pnl"] for _t in _losses))
            _pf_v   = _gp / _gl if _gl > 0 else (float("inf") if _gp > 0 else 0.0)
            _pf_s   = "∞" if _pf_v == float("inf") else f"{_pf_v:.2f}"
            _lbl, _col, _bg = _tlabels[_tk]
            _badge  = _fav_badge.get(_tk, "")
            _pc     = "profit" if _tpnl >= 0 else "loss"
            _wr_c   = "#4ade80" if _twr >= 55 else ("#fbbf24" if _twr >= 45 else "#f87171")
            _trows += f"""<tr style="background:{_bg}20">
  <td style="color:{_col};font-weight:700;border-left:3px solid {_col};padding-left:10px">{_lbl}{_badge}</td>
  <td style="text-align:right">{len(_ts)}</td>
  <td style="text-align:right;color:{_wr_c};font-weight:600">{_twr:.1f}%</td>
  <td style="text-align:right">{_pf_s}</td>
  <td class="profit" style="text-align:right">+{_gp:,.0f}円</td>
  <td class="loss"   style="text-align:right">-{_gl:,.0f}円</td>
  <td class="{_pc}" style="text-align:right;font-weight:600">{_tpnl:+,.0f}円</td>
  <td style="text-align:right;color:{'#4ade80' if _avg_w>0 else '#94a3b8'}">{_avg_w:+,.0f}円</td>
  <td style="text-align:right;color:{'#f87171' if _avg_l<0 else '#94a3b8'}">{_avg_l:+,.0f}円</td>
</tr>"""

        _mode_note = (
            '<span style="color:#f87171">ショートモード: ▼下落が有利 / ▲上昇は不利</span>'
            if _is_short else
            '<span style="color:#4ade80">ロングモード: ▲上昇が有利 / ▼下落は不利</span>'
        )
        # ── BT×トレンド クロス分析 ─────────────────────────────────────────────
        # trade の id → トレンドキー の逆引きマップ
        _tbuckets_map = {}
        for _tk2, _tlist in _tbuckets.items():
            for _t2 in _tlist:
                _tbuckets_map[id(_t2)] = _tk2

        _bt_trend_rows = ""
        _bt_cross_buckets = [
            (80, 101, "BT≥80",  "#4ade80"),
            (60,  80, "BT60-79","#86efac"),
            (40,  60, "BT40-59","#fbbf24"),
            ( 0,  40, "BT<40",  "#f87171"),
        ]
        _trend_order_main = ["up", "sideways", "down"]
        for _blo, _bhi, _blbl, _bcol in _bt_cross_buckets:
            _band = [_t for _t in kpi_trades
                     if _t.get("rec_score") is not None
                     and _blo <= _t["rec_score"] < _bhi]
            if not _band:
                continue
            _cells = ""
            for _tk in _trend_order_main:
                _sub = [_t for _t in _band if _tbuckets_map.get(id(_t)) == _tk]
                if not _sub:
                    _cells += '<td colspan="4" style="color:#475569;text-align:center">—</td>'
                    continue
                _sw = sum(1 for _t in _sub if _t["pnl"] > 0)
                _sl = sum(1 for _t in _sub if _t["pnl"] <= 0)
                _swr = _sw / len(_sub) * 100
                _sgp = sum(_t["pnl"] for _t in _sub if _t["pnl"] > 0)
                _sgl = abs(sum(_t["pnl"] for _t in _sub if _t["pnl"] < 0))
                _spf = _sgp / _sgl if _sgl > 0 else (float("inf") if _sgp > 0 else 0.0)
                _spf_s = "∞" if _spf == float("inf") else f"{_spf:.2f}"
                _wr_c = "#4ade80" if _swr >= 60 else ("#fbbf24" if _swr >= 50 else "#f87171")
                _pf_c = "#4ade80" if _spf >= 1.5 else ("#fbbf24" if _spf >= 1.0 else "#f87171")
                _cells += f"""<td style="text-align:right;color:{_wr_c}">{_swr:.0f}%</td>
<td style="text-align:right;color:{_pf_c}">{_spf_s}</td>
<td style="text-align:right;font-size:0.75rem" class="profit">+{_sgp:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({_sw}勝)</span></td>
<td style="text-align:right;font-size:0.75rem" class="loss">-{_sgl:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({_sl}敗)</span></td>"""
            _bt_trend_rows += f"""<tr>
  <td style="color:{_bcol};font-weight:700;border-left:3px solid {_bcol};padding-left:8px">{_blbl}</td>
  {_cells}
</tr>"""

        _trend_col_headers = ""
        for _tk in _trend_order_main:
            _lbl2, _col2, _ = _tlabels[_tk]
            _trend_col_headers += f'<th colspan="4" style="text-align:center;color:{_col2};border-bottom:2px solid {_col2}">{_lbl2}</th>'
        _trend_sub_headers = '<th>勝率</th><th>PF</th><th>利益計</th><th>損失計</th>' * 3

        _bt_cross_html = f"""
<h3 style="margin-top:24px;margin-bottom:8px;color:#94a3b8;font-size:0.95rem">
  BTスコア × 日経トレンド クロス分析
</h3>
<p class="footnote" style="margin-bottom:8px">
  BTスコア帯とトレンド条件の組み合わせで成績を集計。利益計・損失計は勝ち/負けトレードの合計を分けて表示。
  緑=勝率60%以上/PF1.5以上、黄=50-60%/1.0-1.5、赤=50%未満/1.0未満
</p>
<table style="font-size:0.85rem">
  <thead>
    <tr>
      <th rowspan="2" style="text-align:left">BTスコア</th>
      {_trend_col_headers}
    </tr>
    <tr>{_trend_sub_headers}</tr>
  </thead>
  <tbody>{_bt_trend_rows}</tbody>
</table>""" if _bt_trend_rows else ""

        # ── BT70以上 × トレンド別 損益（下落相場が本当に損か検証）──────────────
        _bt70 = [_t for _t in kpi_trades
                 if _t.get("rec_score") is not None and _t["rec_score"] >= 70]
        _bt70_rows = ""
        for _tk in _trend_order_main:   # up, sideways, down
            _lbl, _col, _bg = _tlabels[_tk]
            _sub = [_t for _t in _bt70 if _tbuckets_map.get(id(_t)) == _tk]
            if not _sub:
                _bt70_rows += (f'<tr style="background:{_bg}20">'
                               f'<td style="color:{_col};font-weight:700;border-left:3px solid {_col};padding-left:10px">{_lbl}</td>'
                               f'<td colspan="6" style="text-align:center;color:#475569">該当なし</td></tr>')
                continue
            _w  = [_t for _t in _sub if _t["pnl"] > 0]
            _l  = [_t for _t in _sub if _t["pnl"] <= 0]
            _gp = sum(_t["pnl"] for _t in _w)
            _gl = abs(sum(_t["pnl"] for _t in _l))
            _pnl = _gp - _gl
            _wr  = len(_w) / len(_sub) * 100
            _pf  = _gp / _gl if _gl > 0 else (float("inf") if _gp > 0 else 0.0)
            _pf_s = "∞" if _pf == float("inf") else f"{_pf:.2f}"
            _pc   = "profit" if _pnl >= 0 else "loss"
            _wr_c = "#4ade80" if _wr >= 55 else ("#fbbf24" if _wr >= 45 else "#f87171")
            _bt70_rows += f"""<tr style="background:{_bg}20">
  <td style="color:{_col};font-weight:700;border-left:3px solid {_col};padding-left:10px">{_lbl}</td>
  <td style="text-align:right">{len(_sub)}</td>
  <td style="text-align:right;color:{_wr_c};font-weight:600">{_wr:.1f}%</td>
  <td style="text-align:right">{_pf_s}</td>
  <td class="profit" style="text-align:right">+{_gp:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({len(_w)}勝)</span></td>
  <td class="loss"   style="text-align:right">-{_gl:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({len(_l)}敗)</span></td>
  <td class="{_pc}"  style="text-align:right;font-weight:700">{_pnl:+,.0f}円</td>
</tr>"""
        _bt70_total = sum(_t["pnl"] for _t in _bt70)
        _bt70_html = f"""
<h3 style="margin-top:24px;margin-bottom:8px;color:#94a3b8;font-size:0.95rem">
  BT70以上 × 日経トレンド別 損益（下落相場が本当に損か検証）
</h3>
<p class="footnote" style="margin-bottom:8px">
  BTスコア70以上のトレードのみを日経トレンド別に集計。利益計/損失計を分けて表示。
  BT70合計: <span style="color:{'#4ade80' if _bt70_total>=0 else '#f87171'};font-weight:700">{_bt70_total:+,.0f}円</span> ({len(_bt70)}件)
</p>
<table style="font-size:0.88rem">
  <thead><tr>
    <th style="text-align:left">日経トレンド</th><th>件数</th><th>勝率</th><th>PF</th>
    <th>利益計</th><th>損失計</th><th>損益合計</th>
  </tr></thead>
  <tbody>{_bt70_rows}</tbody>
</table>""" if _bt70 else ""

        # ── 日経トレンド × 含み損分析 ────────────────────────────────────────
        _trend_neg_rows = ""
        for _tk in _order:
            _ts = _tbuckets.get(_tk, [])
            if not _ts:
                continue
            _wins_t   = [_t for _t in _ts if _t.get("pnl", 0) > 0]
            _tgt_t    = [_t for _t in _ts if "目標達成" in _t.get("reason", "")]
            _stops_t  = [_t for _t in _ts if "損切" in _t.get("reason", "")]
            _avg_neg_tgt = (sum(_t.get("days_neg", 0) for _t in _tgt_t) / len(_tgt_t)
                            if _tgt_t else 0.0)
            _avg_sh   = (sum(_t.get("hold_days", 0) for _t in _stops_t) / len(_stops_t)
                         if _stops_t else 0.0)
            _lbl, _col, _bg = _tlabels[_tk]
            _neg_c_t  = "#f87171" if _avg_neg_tgt > 3 else ("#fbbf24" if _avg_neg_tgt > 1 else "#94a3b8")
            _trend_neg_rows += f"""<tr style="background:{_bg}20">
  <td style="color:{_col};font-weight:700;border-left:3px solid {_col};padding-left:10px">{_lbl}</td>
  <td style="text-align:right">{len(_ts)}</td>
  <td style="text-align:right">{len(_wins_t)}</td>
  <td style="text-align:right;color:{_neg_c_t}">{_avg_neg_tgt:.1f}日 <span style="color:#64748b;font-size:0.75rem">({len(_tgt_t)}件)</span></td>
  <td style="text-align:right;color:#fb923c">{_avg_sh:.1f}日 <span style="color:#64748b;font-size:0.75rem">({len(_stops_t)}件)</span></td>
</tr>"""

        # ── 戦略別 × 含み損分析 ───────────────────────────────────────────────
        _strat_neg_rows = ""
        _strats_all = sorted({_t.get("strategy", "?") for _t in kpi_trades})
        for _strat in _strats_all:
            _ss      = [_t for _t in kpi_trades if _t.get("strategy") == _strat]
            _wins_s  = [_t for _t in _ss if _t.get("pnl", 0) > 0]
            _tgt_s   = [_t for _t in _ss if "目標達成" in _t.get("reason", "")]
            _stops_s = [_t for _t in _ss if "損切" in _t.get("reason", "")]
            _avg_neg_tgt_s = (sum(_t.get("days_neg", 0) for _t in _tgt_s) / len(_tgt_s)
                              if _tgt_s else 0.0)
            _avg_sh_s   = (sum(_t.get("hold_days", 0) for _t in _stops_s) / len(_stops_s)
                           if _stops_s else 0.0)
            _wr_s     = len(_wins_s) / len(_ss) * 100 if _ss else 0.0
            _neg_c_ts = "#f87171" if _avg_neg_tgt_s > 3 else ("#fbbf24" if _avg_neg_tgt_s > 1 else "#94a3b8")
            _strat_neg_rows += f"""<tr>
  <td style="font-weight:600">{_strat}</td>
  <td style="text-align:right">{len(_ss)}</td>
  <td style="text-align:right;color:{'#4ade80' if _wr_s>=55 else ('#fbbf24' if _wr_s>=45 else '#f87171')}">{_wr_s:.1f}%</td>
  <td style="text-align:right;color:{_neg_c_ts}">{_avg_neg_tgt_s:.1f}日 <span style="color:#64748b;font-size:0.75rem">({len(_tgt_s)}件)</span></td>
  <td style="text-align:right;color:#fb923c">{_avg_sh_s:.1f}日 <span style="color:#64748b;font-size:0.75rem">({len(_stops_s)}件)</span></td>
</tr>"""

        if _trows:
            _tbd_id = f"tbd_{days}_{id(cfg_trades_map) % 100000}"
            _trend_breakdown_html = f"""
<button class="analysis-toggle" onclick="toggleTrendBreakdown('{_tbd_id}')" id="{_tbd_id}_btn">▶ 日経トレンド別成績・クロス分析を表示</button>
<div id="{_tbd_id}_block" class="analysis-block" style="display:none">
<nav style="display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid #334155;padding-bottom:6px">
  <button id="{_tbd_id}_tab_trend"  class="detail-tab-btn active" onclick="switchTbd('{_tbd_id}','trend')">トレンド別成績</button>
  <button id="{_tbd_id}_tab_cross"  class="detail-tab-btn"        onclick="switchTbd('{_tbd_id}','cross')">BT×トレンド クロス</button>
  <button id="{_tbd_id}_tab_neg"    class="detail-tab-btn"        onclick="switchTbd('{_tbd_id}','neg')">含み損の原因</button>
</nav>

<div id="{_tbd_id}_pane_trend">
<p class="footnote" style="margin-bottom:10px">
  シグナル発生日（引け後エントリー判断日）の日経トレンドで分類。
  {_mode_note}<br>
  ▲=終値&gt;MA5&gt;MA10 ／ ▼=終値&lt;MA5&lt;MA10 または直近5日{TREND_DROP_PCT:.0f}%以下の急落 ／ →=移行期間
</p>
<table>
  <thead><tr>
    <th style="text-align:left">日経トレンド</th>
    <th>件数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益計</th>
    <th style="color:#f87171">損失計</th>
    <th>損益合計</th>
    <th>勝ち平均</th>
    <th>負け平均</th>
  </tr></thead>
  <tbody>{_trows}</tbody>
</table>
</div>

<div id="{_tbd_id}_pane_cross" style="display:none">
{_bt_cross_html if _bt_cross_html else '<p class="footnote">データなし</p>'}
{_bt70_html}
</div>

<div id="{_tbd_id}_pane_neg" style="display:none">
<p class="footnote" style="margin-bottom:6px">
  含み損保有(目標) = 目標達成トレードが途中で含み損だった日数の平均（days_neg）｜
  損切保有 = 損切りトレードの平均保有日数
</p>
<div style="display:flex;flex-direction:column;gap:2rem">
<div>
<h3 style="color:#94a3b8;font-size:0.95rem;margin-bottom:6px">▌ 日経トレンド別</h3>
<table>
  <thead><tr>
    <th style="text-align:left">日経トレンド</th>
    <th>全取引</th><th>勝ち</th>
    <th style="color:#fbbf24">含み損保有(目標)</th>
    <th style="color:#fb923c">損切保有</th>
  </tr></thead>
  <tbody>{_trend_neg_rows}</tbody>
</table>
</div>
<div>
<h3 style="color:#94a3b8;font-size:0.95rem;margin-bottom:6px">▌ 戦略別</h3>
<table>
  <thead><tr>
    <th style="text-align:left">戦略</th>
    <th>全取引</th><th>勝率</th>
    <th style="color:#fbbf24">含み損保有(目標)</th>
    <th style="color:#fb923c">損切保有</th>
  </tr></thead>
  <tbody>{_strat_neg_rows}</tbody>
</table>
</div>
</div>
</div>

</div>
<script>
function toggleTrendBreakdown(id) {{
  var blk = document.getElementById(id+'_block');
  var btn = document.getElementById(id+'_btn');
  if (!blk) return;
  var show = (blk.style.display === 'none');
  blk.style.display = show ? 'block' : 'none';
  if (btn) btn.textContent = show
    ? '▼ 日経トレンド別成績・クロス分析を隠す'
    : '▶ 日経トレンド別成績・クロス分析を表示';
}}
function switchTbd(id, tab) {{
  var tabs = ['trend','cross','neg'];
  tabs.forEach(function(t) {{
    var pane = document.getElementById(id+'_pane_'+t);
    var btn  = document.getElementById(id+'_tab_'+t);
    if (pane) pane.style.display = (t===tab) ? '' : 'none';
    if (btn)  btn.classList.toggle('active', t===tab);
  }});
}}
</script>"""
    except Exception:
        pass

    # 上部KPI / 全体合計は「決済済みトレードのみ」で集計する。
    # 保有中(未決済=含み損益)は勝率・損益を歪めるため計測から除外し、
    # 取引明細テーブルには引き続き表示する(昨日のシグナル結果の確認用)。
    # ※ 日経トレンド別・BT×トレンドクロス・スコア帯別は従来どおり kpi_trades /
    #    full_year_trades を使う(保有中を含む)。
    settled_trades = [t for t in kpi_trades if t.get("reason") != "保有中"]
    n_total = len(settled_trades)
    n_win   = sum(1 for t in settled_trades if t["pnl"] > 0)
    pnl_sum = sum(t["pnl"] for t in settled_trades)
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
    _all_cfg_win = [t for t in _all_cfg if t["pnl"] > 0]
    cfg_avg_hold     = sum(t.get("hold_days", 0) for t in _all_cfg) / cfg_n_all if cfg_n_all else 0.0
    cfg_avg_neg_days = (sum(t.get("days_neg", 0) for t in _all_cfg_win) / len(_all_cfg_win)
                        if _all_cfg_win else 0.0)
    cfg_avg_delay    = sum(t.get("days_to_fill", 0) for t in _all_cfg) / cfg_n_all if cfg_n_all else 0.0
    # 重複除外合計（= 全体KPIと同じ数値。保有中=未決済は除外して整合させる）
    dedup_gp  = sum(t["pnl"] for t in settled_trades if t["pnl"] > 0)
    dedup_gl  = abs(sum(t["pnl"] for t in settled_trades if t["pnl"] < 0))
    dedup_pf  = dedup_gp / dedup_gl if dedup_gl > 0 else (float("inf") if dedup_gp > 0 else 0.0)
    dedup_pf_s = "∞" if dedup_pf == float("inf") else f"{dedup_pf:.2f}"
    _dedup_win = [t for t in settled_trades if t["pnl"] > 0]
    dedup_avg_hold     = sum(t.get("hold_days", 0) for t in settled_trades) / len(settled_trades) if settled_trades else 0.0
    dedup_avg_neg_days = (sum(t.get("days_neg", 0) for t in _dedup_win) / len(_dedup_win)
                          if _dedup_win else 0.0)
    dedup_avg_delay    = (sum(t.get("days_to_fill", 0) for t in settled_trades) / len(settled_trades)
                          if settled_trades else 0.0)
    kpi_html = f"""
<div class="kpi-grid" style="margin-bottom:8px">
  <div class="kpi"><div class="kpi-l">総取引数 ※</div><div class="kpi-v">{n_total}件</div></div>
  <div class="kpi"><div class="kpi-l">勝率</div><div class="kpi-v">{"—" if not n_total else f"{wr:.1f}%"}</div></div>
  <div class="kpi"><div class="kpi-l">合計損益</div><div class="kpi-v {pc}">{"—" if not n_total else f"{pnl_sum:+,.0f}円"}</div></div>
  <div class="kpi"><div class="kpi-l">勝ち/負け</div><div class="kpi-v">{n_win}W / {n_total - n_win}L</div></div>
</div>
<p class="footnote" style="margin-bottom:18px">※ 同一シグナル（銘柄+戦略+シグナル日が同一）は重複除外し1件として集計。設定別サマリーの合計とは異なります。<br>※ 保有中（未決済・含み損益）は計測から除外（決済済みトレードのみで勝率・損益を集計）。昨日のシグナル等の保有中は取引明細に表示のみ。</p>"""

    # ── サマリーテーブル（各configの独立実績、cross-config重複なし）──
    def _sum_rows_for(min_bt: int) -> str:
        rows = ""
        for cfg in _PNL_CONFIGS:
            lbl    = cfg["label"]
            trades = [t for t in cfg_trades_map.get(lbl, [])
                      if t.get("score", 0) >= min_bt]
            n      = len(trades)
            wins   = sum(1 for t in trades if t["pnl"] > 0)
            pnl    = sum(t["pnl"] for t in trades)
            wr_l   = wins / n * 100 if n else 0.0
            gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
            gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
            pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
            pf_s   = "∞" if pf == float("inf") else f"{pf:.2f}"
            lpc    = "profit" if pnl >= 0 else "loss"
            win_t  = [t for t in trades if t["pnl"] > 0]
            avg_hold      = sum(t.get("hold_days", 0) for t in trades) / n if n else 0.0
            avg_neg_days  = sum(t.get("days_neg", 0) for t in win_t) / len(win_t) if win_t else 0.0
            avg_delay     = sum(t.get("days_to_fill", 0) for t in trades) / n if n else 0.0
            dot = f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{cfg["color"]};margin-right:6px;vertical-align:middle"></span>'
            rows += f"""<tr>
  <td class="sym" style="text-align:left">{dot}{lbl}</td>
  <td>{n}</td><td>{wins}</td>
  <td>{"—" if not n else f"{wr_l:.1f}%"}</td>
  <td>{"—" if not n else pf_s}</td>
  <td style="text-align:right">{"—" if not n else f"{avg_hold:.1f}日"}</td>
  <td style="text-align:right;color:#f87171">{"—" if not win_t else f"{avg_neg_days:.1f}日"}</td>
  <td style="text-align:right;color:#fbbf24">{"—" if not n else f"{avg_delay:.1f}日"}</td>
  <td class="profit" style="text-align:right">{"—" if not n else f"+{gp:,.0f}円"}</td>
  <td class="loss"   style="text-align:right">{"—" if not n else f"-{gl:,.0f}円"}</td>
  <td class="{lpc}"  style="text-align:right;font-weight:700">{"—" if not n else f"{pnl:+,.0f}円"}</td>
</tr>"""
        return rows

    sum_rows      = _sum_rows_for(0)
    sum_rows_bt70 = _sum_rows_for(70)

    # BT70フィルター合計行用
    _bt70_cfg = [t for t in _all_cfg if t.get("score", 0) >= 70]
    bt70_n   = len(_bt70_cfg); bt70_win = sum(1 for t in _bt70_cfg if t["pnl"] > 0)
    bt70_pnl = sum(t["pnl"] for t in _bt70_cfg)
    bt70_gp  = sum(t["pnl"] for t in _bt70_cfg if t["pnl"] > 0)
    bt70_gl  = abs(sum(t["pnl"] for t in _bt70_cfg if t["pnl"] < 0))
    bt70_pf  = bt70_gp / bt70_gl if bt70_gl > 0 else (float("inf") if bt70_gp > 0 else 0.0)
    bt70_pf_s = "∞" if bt70_pf == float("inf") else f"{bt70_pf:.2f}"
    bt70_lpc = "profit" if bt70_pnl >= 0 else "loss"
    _bt70_cfg_win = [t for t in _bt70_cfg if t["pnl"] > 0]
    bt70_avg_hold     = sum(t.get("hold_days", 0) for t in _bt70_cfg) / bt70_n if bt70_n else 0.0
    bt70_avg_neg_days = (sum(t.get("days_neg", 0) for t in _bt70_cfg_win) / len(_bt70_cfg_win)
                         if _bt70_cfg_win else 0.0)
    bt70_avg_delay = sum(t.get("days_to_fill", 0) for t in _bt70_cfg) / bt70_n if bt70_n else 0.0
    _bt70_ded  = [t for t in settled_trades if t.get("score", 0) >= 70]
    _bt70_ded_win = [t for t in _bt70_ded if t["pnl"] > 0]
    bt70d_n   = len(_bt70_ded); bt70d_win = sum(1 for t in _bt70_ded if t["pnl"] > 0)
    bt70d_pnl = sum(t["pnl"] for t in _bt70_ded)
    bt70d_gp  = sum(t["pnl"] for t in _bt70_ded if t["pnl"] > 0)
    bt70d_gl  = abs(sum(t["pnl"] for t in _bt70_ded if t["pnl"] < 0))
    bt70d_pf  = bt70d_gp / bt70d_gl if bt70d_gl > 0 else (float("inf") if bt70d_gp > 0 else 0.0)
    bt70d_pf_s = "∞" if bt70d_pf == float("inf") else f"{bt70d_pf:.2f}"
    bt70d_lpc = "profit" if bt70d_pnl >= 0 else "loss"
    bt70d_avg_hold     = sum(t.get("hold_days", 0) for t in _bt70_ded) / bt70d_n if bt70d_n else 0.0
    bt70d_avg_neg_days = (sum(t.get("days_neg", 0) for t in _bt70_ded_win) / len(_bt70_ded_win)
                          if _bt70_ded_win else 0.0)
    bt70d_avg_delay = sum(t.get("days_to_fill", 0) for t in _bt70_ded) / bt70d_n if bt70d_n else 0.0

    # ── スコア細粒度分析 ──
    score_buckets = [
        (90,101,"90-100","#4ade80"),(80,90,"80-89","#86efac"),
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
        wf_vals = [t["wf_score"]  for t in trades if t.get("wf_score")  is not None]
        bt_vals = [t["rec_score"] for t in trades if t.get("rec_score") is not None]
        avg_wf = round(sum(wf_vals) / len(wf_vals)) if wf_vals else None
        avg_bt = round(sum(bt_vals) / len(bt_vals)) if bt_vals else None
        return n, wins, pnl, gp, gl, pf, avg, avg_wf, avg_bt

    def _wf_cell(v):
        if v is None: return '<td style="color:#475569;text-align:center">—</td>'
        c = "#4ade80" if v >= 70 else ("#fbbf24" if v >= 50 else "#f87171")
        return f'<td style="color:{c};font-weight:700;text-align:center">{v}</td>'
    def _bt_cell(v):
        if v is None: return '<td style="color:#475569;text-align:center">—</td>'
        c = "#4ade80" if v >= 60 else ("#fbbf24" if v >= 40 else "#f87171")
        return f'<td style="color:{c};font-weight:700;text-align:center">{v}</td>'

    fine_rows = ""
    for lo, hi, lbl_s, col in score_buckets:
        tr = [t for t in full_year_trades if t.get("score") is not None and lo <= t["score"] < hi]
        n  = len(tr)
        if not n:
            continue
        n, wins, pnl, gp, gl, pf, avg, avg_wf, avg_bt = _band_stats(tr)
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
  {_wf_cell(avg_wf)}{_bt_cell(avg_bt)}
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
            sn, sw, sp, sgp, sgl, spf, savg, swf, sbt = _band_stats(sub)
            spf_s = "∞" if spf == float("inf") else f"{spf:.2f}"
            swr   = sw / sn * 100
            slpc  = "profit" if sp >= 0 else "loss"
            sapc  = "profit" if savg >= 0 else "loss"
            dot   = f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:{cfg["color"]};margin-right:5px;vertical-align:middle"></span>'
            swf_s = str(swf) if swf is not None else "—"
            sbt_s = str(sbt) if sbt is not None else "—"
            fine_rows += f"""<tr style="background:#0f172a">
  <td style="text-align:left;padding-left:20px;color:#94a3b8;font-size:0.8rem">{dot}{cfg["label"]}</td>
  <td style="color:#94a3b8;font-size:0.8rem">{sn}</td>
  <td style="color:#94a3b8;font-size:0.8rem">{swr:.1f}%</td>
  <td style="color:#94a3b8;font-size:0.8rem">{spf_s}</td>
  <td style="color:#94a3b8;font-size:0.8rem;text-align:center">{swf_s}</td>
  <td style="color:#94a3b8;font-size:0.8rem;text-align:center">{sbt_s}</td>
  <td class="profit" style="text-align:right;font-size:0.8rem">+{sgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-size:0.8rem">-{sgl:,.0f}円</td>
  <td class="{slpc}" style="text-align:right;font-size:0.8rem">{sp:+,.0f}円</td>
  <td class="{sapc}" style="text-align:right;font-size:0.8rem">{savg:+,.0f}円</td>
</tr>"""

    # ── BTスコア帯別集計 ──
    bt_buckets = [
        (90,101,"90-100","#4ade80"),(80,90,"80-89","#86efac"),
        (70,80,"70-79","#60a5fa"),(60,70,"60-69","#93c5fd"),
        (50,60,"50-59","#fbbf24"),(40,50,"40-49","#fcd34d"),
        (30,40,"30-39","#f87171"),(0,30,"0-29","#94a3b8"),
    ]
    bt_fine_rows = ""
    for lo, hi, lbl_s, col in bt_buckets:
        tr = [t for t in full_year_trades
              if t.get("score") is not None and lo <= t["score"] < hi]
        n = len(tr)
        if not n:
            continue
        n, wins, pnl, gp, gl, pf, avg, avg_wf, avg_bt = _band_stats(tr)
        pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
        wr_s  = wins / n * 100
        lpc   = "profit" if pnl >= 0 else "loss"
        apc   = "profit" if avg >= 0 else "loss"
        border_style = "border-top:2px solid #334155;" if lo in (40,60,80) else ""
        bt_fine_rows += f"""<tr style="{border_style}">
  <td style="color:{col};font-weight:700;text-align:left">{lbl_s}</td>
  <td style="font-weight:700">{n}</td>
  <td style="font-weight:700">{wr_s:.1f}%</td>
  <td style="font-weight:700">{pf_s}</td>
  {_wf_cell(avg_wf)}{_bt_cell(avg_bt)}
  <td class="profit" style="text-align:right;font-weight:700">+{gp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{gl:,.0f}円</td>
  <td class="{lpc}"  style="text-align:right;font-weight:700">{pnl:+,.0f}円</td>
  <td class="{apc}"  style="text-align:right;font-weight:700">{avg:+,.0f}円</td>
</tr>"""
        for cfg in _PNL_CONFIGS:
            sub = [t for t in tr if t.get("label") == cfg["label"]]
            if not sub:
                continue
            sn, sw, sp, sgp, sgl, spf, savg, swf, sbt = _band_stats(sub)
            spf_s = "∞" if spf == float("inf") else f"{spf:.2f}"
            swr   = sw / sn * 100
            slpc  = "profit" if sp >= 0 else "loss"
            sapc  = "profit" if savg >= 0 else "loss"
            dot   = f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:{cfg["color"]};margin-right:5px;vertical-align:middle"></span>'
            swf_s = str(swf) if swf is not None else "—"
            sbt_s = str(sbt) if sbt is not None else "—"
            bt_fine_rows += f"""<tr style="background:#0f172a">
  <td style="text-align:left;padding-left:20px;color:#94a3b8;font-size:0.8rem">{dot}{cfg["label"]}</td>
  <td style="color:#94a3b8;font-size:0.8rem">{sn}</td>
  <td style="color:#94a3b8;font-size:0.8rem">{swr:.1f}%</td>
  <td style="color:#94a3b8;font-size:0.8rem">{spf_s}</td>
  <td style="color:#94a3b8;font-size:0.8rem;text-align:center">{swf_s}</td>
  <td style="color:#94a3b8;font-size:0.8rem;text-align:center">{sbt_s}</td>
  <td class="profit" style="text-align:right;font-size:0.8rem">+{sgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-size:0.8rem">-{sgl:,.0f}円</td>
  <td class="{slpc}" style="text-align:right;font-size:0.8rem">{sp:+,.0f}円</td>
  <td class="{sapc}" style="text-align:right;font-size:0.8rem">{savg:+,.0f}円</td>
</tr>"""

    # ── ③ BT×WF クロス分析 (BT≥60内でWFスコア帯別比較) ──
    bt60_trades = [t for t in full_year_trades if (t.get("score") or 0) >= 60]
    wf_cross_bands = [
        (70, 101, "WF70以上",  "#4ade80"),
        (50,  70, "WF50-69",   "#fbbf24"),
        (0,   50, "WF0-49",    "#f87171"),
        (None, None, "WFなし", "#94a3b8"),
    ]
    cross_rows = ""
    for wlo, whi, wlbl, wcol in wf_cross_bands:
        if wlo is None:
            band = [t for t in bt60_trades if t.get("wf_score") is None]
        else:
            band = [t for t in bt60_trades
                    if t.get("wf_score") is not None and wlo <= t["wf_score"] < whi]
        if not band:
            continue
        bn, bw, bpnl, bgp, bgl, bpf, bavg, _, _ = _band_stats(band)
        bpf_s = "∞" if bpf == float("inf") else f"{bpf:.2f}"
        bpc   = "profit" if bpnl >= 0 else "loss"
        bapc  = "profit" if bavg >= 0 else "loss"
        cross_rows += f"""<tr>
  <td style="color:{wcol};font-weight:700;text-align:left">{wlbl}</td>
  <td style="font-weight:700">{bn}</td>
  <td style="font-weight:700">{bw/bn*100:.1f}%</td>
  <td style="font-weight:700">{bpf_s}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{bgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{bgl:,.0f}円</td>
  <td class="{bpc}"  style="text-align:right;font-weight:700">{bpnl:+,.0f}円</td>
  <td class="{bapc}" style="text-align:right;font-weight:700">{bavg:+,.0f}円</td>
</tr>"""

    # BT全体行（BT≥60 合計）
    if bt60_trades:
        an, aw, apnl, agp, agl, apf, aavg, _, _ = _band_stats(bt60_trades)
        apf_s = "∞" if apf == float("inf") else f"{apf:.2f}"
        apc2  = "profit" if apnl >= 0 else "loss"
        cross_rows += f"""<tr style="border-top:2px solid #475569;background:#0d1424">
  <td style="color:#60a5fa;font-weight:700;text-align:left">BT≥60 合計</td>
  <td style="color:#60a5fa;font-weight:700">{an}</td>
  <td style="color:#60a5fa;font-weight:700">{aw/an*100:.1f}%</td>
  <td style="color:#60a5fa;font-weight:700">{apf_s}</td>
  <td class="profit" style="text-align:right;color:#60a5fa;font-weight:700">+{agp:,.0f}円</td>
  <td class="loss"   style="text-align:right;color:#60a5fa;font-weight:700">-{agl:,.0f}円</td>
  <td class="{apc2}" style="text-align:right;color:#60a5fa;font-weight:700">{apnl:+,.0f}円</td>
  <td style="text-align:right;color:#60a5fa;font-weight:700">{aavg:+,.0f}円</td>
</tr>"""

    # ── ④ 高BT銘柄別成績 (BT≥60 / rec_scoreで選定・per-symbol) ──
    sym_agg: dict = defaultdict(lambda: {"n":0,"w":0,"pnl":0,"gp":0,"gl":0,
                                          "strats":set(),"wf_scores":[],"rec_scores":[]})
    for t in bt60_trades:
        k = (t["symbol"], t.get("name",""))
        d = sym_agg[k]
        d["n"]   += 1
        d["w"]   += 1 if t["pnl"] > 0 else 0
        d["pnl"] += t["pnl"]
        d["gp"]  += t["pnl"] if t["pnl"] > 0 else 0
        d["gl"]  += abs(t["pnl"]) if t["pnl"] < 0 else 0
        d["strats"].add(t.get("strategy",""))
        if t.get("wf_score") is not None:
            d["wf_scores"].append(t["wf_score"])
        if t.get("rec_score") is not None:
            d["rec_scores"].append(t["rec_score"])

    # 損益降順でソート
    sym_sorted = sorted(sym_agg.items(), key=lambda x: x[1]["pnl"], reverse=True)
    sym_rows = ""
    for (sym, name), d in sym_sorted:
        n    = d["n"]; w = d["w"]; pnl = d["pnl"]; gp = d["gp"]; gl = d["gl"]
        pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        wr_v = w / n * 100 if n else 0
        spc  = "profit" if pnl >= 0 else "loss"
        strat_tags = " ".join(
            f'<span class="tag tag-{s.lower()}" style="font-size:0.7rem">{s}</span>'
            for s in sorted(d["strats"])
        )
        avg_wf  = round(sum(d["wf_scores"])  / len(d["wf_scores"]))  if d["wf_scores"]  else None
        avg_rec = round(sum(d["rec_scores"]) / len(d["rec_scores"])) if d["rec_scores"] else None
        wf_disp  = f'<span style="color:{"#4ade80" if avg_wf and avg_wf>=70 else ("#fbbf24" if avg_wf and avg_wf>=50 else "#f87171")};font-weight:700">{avg_wf}</span>' if avg_wf is not None else '—'
        rec_disp = f'<span style="color:{"#4ade80" if avg_rec and avg_rec>=60 else ("#fbbf24" if avg_rec and avg_rec>=40 else "#f87171")};font-weight:700">{avg_rec}</span>' if avg_rec is not None else '—'
        # 赤枠: 損失が大きい銘柄を強調
        row_style = ' style="background:#1a0a0a;border-left:3px solid #f87171"' if pnl < -30000 else (
                    ' style="background:#0a1a0a;border-left:3px solid #4ade80"' if pnl > 50000 else "")
        sym_rows += f"""<tr{row_style}>
  <td class="sym" style="text-align:left">{sym}<br><span style="color:#64748b;font-size:0.75rem">{name}</span></td>
  <td style="text-align:center">{strat_tags}</td>
  <td style="text-align:center">{rec_disp}</td>
  <td style="text-align:center">{wf_disp}</td>
  <td style="font-weight:700">{n}</td>
  <td style="font-weight:700">{wr_v:.1f}%</td>
  <td style="font-weight:700">{pf_s}</td>
  <td class="profit" style="text-align:right">+{gp:,.0f}円</td>
  <td class="loss"   style="text-align:right">-{gl:,.0f}円</td>
  <td class="{spc}"  style="text-align:right;font-weight:700">{pnl:+,.0f}円</td>
</tr>"""
    if not sym_rows:
        sym_rows = '<tr><td colspan="10" style="text-align:center;color:#64748b;padding:12px">BT≥60の取引なし</td></tr>'

    # ── ⑤ BT60-69 × WFクロス + 銘柄別（conservative限定）──
    # conservativeラベルを含む設定のみ抽出
    con_labels = {cfg["label"] for cfg in _PNL_CONFIGS if "conservative" in cfg.get("mode", "").lower() or "conservative" in cfg.get("label", "").lower()}
    bt6069_con_trades = [
        t for t in full_year_trades
        if t.get("rec_score") is not None
        and 60 <= t["rec_score"] < 70
        and (t.get("label", "") in con_labels or not con_labels)
    ]

    # WFクロス分析 (BT60-69 conservative)
    wf6069_rows = ""
    for wlo, whi, wlbl, wcol in [
        (70, 101, "WF70以上",  "#4ade80"),
        (50,  70, "WF50-69",   "#fbbf24"),
        (0,   50, "WF0-49",    "#f87171"),
        (None, None, "WFなし", "#94a3b8"),
    ]:
        if wlo is None:
            band = [t for t in bt6069_con_trades if t.get("wf_score") is None]
        else:
            band = [t for t in bt6069_con_trades
                    if t.get("wf_score") is not None and wlo <= t["wf_score"] < whi]
        if not band:
            continue
        bn, bw, bpnl, bgp, bgl, bpf, bavg, _, _ = _band_stats(band)
        bpf_s = "∞" if bpf == float("inf") else f"{bpf:.2f}"
        bpc   = "profit" if bpnl >= 0 else "loss"
        bapc  = "profit" if bavg >= 0 else "loss"
        verdict = (
            '<span style="background:#4ade80;color:#0f172a;font-size:0.65rem;'
            'font-weight:700;padding:1px 6px;border-radius:3px;margin-left:6px">投資対象</span>'
            if bpnl >= 0 else
            '<span style="background:#f87171;color:#0f172a;font-size:0.65rem;'
            'font-weight:700;padding:1px 6px;border-radius:3px;margin-left:6px">スキップ</span>'
        )
        wf6069_rows += f"""<tr>
  <td style="color:{wcol};font-weight:700;text-align:left">{wlbl}{verdict}</td>
  <td style="font-weight:700">{bn}</td>
  <td style="font-weight:700">{bw/bn*100:.1f}%</td>
  <td style="font-weight:700">{bpf_s}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{bgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{bgl:,.0f}円</td>
  <td class="{bpc}"  style="text-align:right;font-weight:700">{bpnl:+,.0f}円</td>
  <td class="{bapc}" style="text-align:right;font-weight:700">{bavg:+,.0f}円</td>
</tr>"""
    if bt6069_con_trades:
        tn, tw, tpnl, tgp, tgl, tpf, tavg, _, _ = _band_stats(bt6069_con_trades)
        tpf_s = "∞" if tpf == float("inf") else f"{tpf:.2f}"
        tpc   = "profit" if tpnl >= 0 else "loss"
        wf6069_rows += f"""<tr style="border-top:2px solid #475569;background:#0d1424">
  <td style="color:#93c5fd;font-weight:700;text-align:left">BT60-69 con 合計</td>
  <td style="color:#93c5fd;font-weight:700">{tn}</td>
  <td style="color:#93c5fd;font-weight:700">{tw/tn*100:.1f}%</td>
  <td style="color:#93c5fd;font-weight:700">{tpf_s}</td>
  <td class="profit" style="text-align:right;color:#93c5fd;font-weight:700">+{tgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;color:#93c5fd;font-weight:700">-{tgl:,.0f}円</td>
  <td class="{tpc}"  style="text-align:right;color:#93c5fd;font-weight:700">{tpnl:+,.0f}円</td>
  <td style="text-align:right;color:#93c5fd;font-weight:700">{tavg:+,.0f}円</td>
</tr>"""
    if not wf6069_rows:
        wf6069_rows = '<tr><td colspan="8" style="text-align:center;color:#64748b;padding:12px">BT60-69（conservative）の取引なし</td></tr>'
    sym6069_agg: dict = defaultdict(lambda: {"n":0,"w":0,"pnl":0,"gp":0,"gl":0,
                                              "strats":set(),"wf_scores":[],"rec_scores":[]})
    for t in bt6069_con_trades:
        k = (t["symbol"], t.get("name",""))
        d = sym6069_agg[k]
        d["n"]   += 1
        d["w"]   += 1 if t["pnl"] > 0 else 0
        d["pnl"] += t["pnl"]
        d["gp"]  += t["pnl"] if t["pnl"] > 0 else 0
        d["gl"]  += abs(t["pnl"]) if t["pnl"] < 0 else 0
        d["strats"].add(t.get("strategy",""))
        if t.get("wf_score") is not None:
            d["wf_scores"].append(t["wf_score"])
        if t.get("rec_score") is not None:
            d["rec_scores"].append(t["rec_score"])

    sym6069_sorted = sorted(sym6069_agg.items(), key=lambda x: x[1]["pnl"], reverse=True)
    sym6069_rows = ""
    for (sym, name), d in sym6069_sorted:
        n    = d["n"]; w = d["w"]; pnl = d["pnl"]; gp = d["gp"]; gl = d["gl"]
        pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        wr_v = w / n * 100 if n else 0
        spc  = "profit" if pnl >= 0 else "loss"
        strat_tags = " ".join(
            f'<span class="tag tag-{s.lower()}" style="font-size:0.7rem">{s}</span>'
            for s in sorted(d["strats"])
        )
        avg_wf  = round(sum(d["wf_scores"])  / len(d["wf_scores"]))  if d["wf_scores"]  else None
        avg_rec = round(sum(d["rec_scores"]) / len(d["rec_scores"])) if d["rec_scores"] else None
        wf_disp  = f'<span style="color:{"#4ade80" if avg_wf and avg_wf>=70 else ("#fbbf24" if avg_wf and avg_wf>=50 else "#f87171")};font-weight:700">{avg_wf}</span>' if avg_wf is not None else '—'
        rec_disp = f'<span style="color:{"#93c5fd"};font-weight:700">{avg_rec}</span>' if avg_rec is not None else '—'
        # スキップ候補: 損失-1万超を赤枠、利益+3万超を緑枠
        if pnl < -10000:
            row_style = ' style="background:#1a0a0a;border-left:3px solid #f87171"'
            skip_badge = '<span style="background:#f87171;color:#0f172a;font-size:0.65rem;font-weight:700;padding:1px 5px;border-radius:3px;margin-left:4px">スキップ候補</span>'
        elif pnl > 30000:
            row_style = ' style="background:#0a1a0a;border-left:3px solid #4ade80"'
            skip_badge = '<span style="background:#4ade80;color:#0f172a;font-size:0.65rem;font-weight:700;padding:1px 5px;border-radius:3px;margin-left:4px">優先</span>'
        else:
            row_style = ""
            skip_badge = ""
        sym6069_rows += f"""<tr{row_style}>
  <td class="sym" style="text-align:left">{sym}{skip_badge}<br><span style="color:#64748b;font-size:0.75rem">{name}</span></td>
  <td style="text-align:center">{strat_tags}</td>
  <td style="text-align:center">{rec_disp}</td>
  <td style="text-align:center">{wf_disp}</td>
  <td style="font-weight:700">{n}</td>
  <td style="font-weight:700">{wr_v:.1f}%</td>
  <td style="font-weight:700">{pf_s}</td>
  <td class="profit" style="text-align:right">+{gp:,.0f}円</td>
  <td class="loss"   style="text-align:right">-{gl:,.0f}円</td>
  <td class="{spc}"  style="text-align:right;font-weight:700">{pnl:+,.0f}円</td>
</tr>"""
    if not sym6069_rows:
        sym6069_rows = '<tr><td colspan="10" style="text-align:center;color:#64748b;padding:12px">BT60-69（conservative）の取引なし</td></tr>'

    # ── 取引明細テーブル ──
    col_map = {"★★★": "#4ade80", "★★": "#60a5fa", "★": "#fbbf24", "△": "#f87171"}
    def _rhtml(reason):
        if reason == "目標達成": return '<span style="color:#4ade80;font-weight:600">目標達成</span>'
        if reason == "損切り":   return '<span style="color:#f87171;font-weight:600">損切り</span>'
        if reason == "タイムカット": return '<span style="color:#94a3b8">タイムカット</span>'
        return f'<span style="color:#fbbf24">{reason}</span>'

    # 直近損切りマップ: symbol -> [exit_date, ...]
    _recent_stop_map: dict[str, list] = {}
    for _t in all_trades:
        if _t.get("reason") == "損切り" and _t.get("exit_d_raw"):
            _sym = _t.get("symbol", "")
            if _sym:
                _recent_stop_map.setdefault(_sym, []).append(_t["exit_d_raw"])

    def _stop_warn(sym: str, entry_d) -> str:
        if not sym or entry_d is None or sym not in _recent_stop_map:
            return ""
        prior = [d for d in _recent_stop_map[sym]
                 if d < entry_d and (entry_d - d).days <= 30]
        if not prior:
            return ""
        days_ago = (entry_d - max(prior)).days
        return (f'<br><span style="color:#f87171;font-size:0.68rem;font-weight:600">'
                f'⚠ {days_ago}日前に損切り</span>')

    # 取引明細の表示用リスト。1銘柄1ポジションで弾かれた重複シグナルのうち
    # 「未決済(発注中/保有中)」のものを参考表示する(計測には入れない)。
    # 例: デジタルアーツが 06/03 から保有中でも、06/10 の新シグナルを表示する。
    _unsettled_overlaps = [t for t in _overlap_dropped
                           if t.get("reason") in ("発注中", "保有中")]
    display_trades = all_trades + _unsettled_overlaps

    # 発注中を先頭に、それ以外は決済日降順
    pending_trades = [t for t in display_trades if t.get("reason") == "発注中"]
    done_trades    = [t for t in display_trades if t.get("reason") != "発注中"]
    sorted_trades  = pending_trades + sorted(done_trades, key=lambda x: x["exit_d_raw"], reverse=True)

    def _build_trade_row(t, entry_first=False) -> str:
        is_pending = t.get("reason") == "発注中"
        is_overlap = bool(t.get("_overlap"))
        overlap_badge = ('<br><span style="background:#7c3aed;color:#fff;font-size:0.66rem;'
                         'font-weight:700;padding:1px 5px;border-radius:3px;white-space:nowrap">'
                         '重複保有・計測外</span>') if is_overlap else ""
        tpc = "profit" if t["pnl"] > 0 else ("" if is_pending else "loss")
        tag = f'<span class="tag tag-{t["strategy"].lower()}">{t["strategy"]}</span>'
        sc  = t.get("score"); rk = t.get("rank")
        if sc is not None and rk and rk != "-":
            _col = col_map.get(rk, "#94a3b8")
            sc_html = _fmt_score_cell(t, _col)
        else:
            sc_html = ""
        if is_overlap:
            row_style = ' style="opacity:0.6;border-left:3px solid #7c3aed"'
        elif is_pending:
            row_style = ' style="opacity:0.7;border-left:3px solid #fbbf24"'
        else:
            row_style = ""
        pnl_cell  = '—' if is_pending else f'{t["pnl"]:+,.0f}円'
        dtf = t.get("days_to_fill", 0)
        delay_cell = (f'<td style="text-align:right;color:#94a3b8">当日</td>'
                      if dtf == 0 else
                      f'<td style="text-align:right;color:#fbbf24">{dtf}日後</td>')
        is_loss = not is_pending and t.get("pnl", 0) < 0
        hold_sub = ('<br><span style="font-size:0.7rem;color:#f87171">含み損</span>'
                    if is_loss else "")
        hold_cell = f'<td style="text-align:right">{t["hold_days"]}日{hold_sub}</td>'
        cfg_color = t.get("color", "#64748b")
        cfg_label = t.get("label", "")
        cfg_badge = f'<span style="background:{cfg_color};color:#0f172a;font-size:0.68rem;font-weight:700;padding:1px 6px;border-radius:3px;white-space:nowrap">{cfg_label}</span>'
        olp = t.get("order_limit", 0)
        osp = t.get("order_stop", 0)
        otp = t.get("order_target", 0)
        if olp > 0 and osp > 0 and otp > 0:
            sp_pct   = (osp - olp) / olp * 100
            tp_pct   = (otp - olp) / olp * 100
            stop_cell = (f'<td style="text-align:right;white-space:nowrap">{osp:,.0f}'
                         f'<br><span style="font-size:0.73rem;color:#f87171">{sp_pct:+.1f}%</span></td>')
            tgt_cell  = (f'<td style="text-align:right;white-space:nowrap">{otp:,.0f}'
                         f'<br><span style="font-size:0.73rem;color:#4ade80">{tp_pct:+.1f}%</span></td>')
            olp_sub   = f'<br><span style="font-size:0.71rem;color:#64748b">逆:{olp:,.0f}</span>'
            # 現在値: 保有中のみ表示（現在株価・損切りまで・目標まで）
            cur = t.get("exit_p", 0)
            if t.get("reason") == "保有中" and cur > 0 and osp > 0 and otp > 0:
                d_sp  = (cur - osp) / cur * 100
                d_tp  = (otp - cur) / cur * 100
                sp_c  = "#4ade80" if d_sp >= 5 else ("#fbbf24" if d_sp >= 2 else "#f87171")
                loc_cell = (f'<td style="text-align:right;white-space:nowrap">'
                            f'<strong>{cur:,.0f}</strong>'
                            f'<br><span style="font-size:0.72rem;color:{sp_c}">損切まで{d_sp:+.1f}%</span>'
                            f'<br><span style="font-size:0.72rem;color:#4ade80">目標まで{d_tp:+.1f}%</span>'
                            f'</td>')
            else:
                loc_cell = '<td style="color:#475569;text-align:center">—</td>'
        else:
            stop_cell = '<td style="color:#475569;text-align:right">—</td>'
            tgt_cell  = '<td style="color:#475569;text-align:right">—</td>'
            loc_cell  = '<td style="color:#475569;text-align:center">—</td>'
            olp_sub   = ""
        first_col = (f'<td style="color:#94a3b8">{t["entry_dt"]}</td>'
                     if entry_first else f'<td>{t["exit_dt"]}</td>')
        last_col  = (f'<td>{t["exit_dt"]}</td>'
                     if entry_first else f'<td style="color:#94a3b8">{t["entry_dt"]}</td>')
        return f"""<tr{row_style}>
  {first_col}
  <td class="sym" style="text-align:left">{t["symbol"]} {sc_html}<br><span style="color:#64748b;font-size:0.75rem">{t["name"]}</span>{_stop_warn(t.get("symbol",""), t.get("entry_d_raw"))}</td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">{cfg_badge}</td>
  <td style="text-align:right">{t["entry_p"]:,.0f}{olp_sub}</td>
  {stop_cell}
  {tgt_cell}
  {loc_cell}
  <td style="text-align:right">{t["exit_p"]:,.0f}</td>
  <td style="text-align:right">{t.get("qty",0):,}</td>
  {hold_cell}
  {delay_cell}
  <td class="{tpc}" style="text-align:right">{pnl_cell}</td>
  <td>{_rhtml(t["reason"])}{overlap_badge}</td>
  {last_col}
</tr>"""

    def _rows_for(trades, empty_msg, entry_first=False) -> str:
        rows = "".join(_build_trade_row(t, entry_first=entry_first) for t in trades)
        if not rows:
            rows = f'<tr><td colspan="15" style="text-align:center;color:#64748b;padding:16px">{empty_msg}</td></tr>'
        return rows

    # 全部 / BT70以上 / エントリー日別グリッド の 3 系統を用意
    bt70_trades = [t for t in sorted_trades if (t.get("rec_score") or 0) >= 70]
    # エントリー日降順（発注中を先頭、それ以外はエントリー日降順）
    entry_sorted_trades = pending_trades + sorted(
        done_trades,
        key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"],
        reverse=True
    )
    trade_rows_all  = _rows_for(sorted_trades, f"直近{days}日に決済した取引なし")
    trade_rows_bt70 = _rows_for(bt70_trades,   "BT70以上の取引なし")

    # ── エントリー日別グリッド HTML ──────────────────────────────────
    from collections import defaultdict as _dd
    _ENTRY_GRID_DAYS = days  # グリッド表示は分析期間全体

    def _build_entry_grid(trades_list, prefix):
        """trades_list をエントリー日でグループ化して (by_date, sorted_dates) を返す。"""
        by_date: dict = _dd(list)
        cutoff_d = until - timedelta(days=_ENTRY_GRID_DAYS)
        for _t in trades_list:
            _dk = str(_t.get("entry_d_raw") or _t["exit_d_raw"])
            if _dk >= str(cutoff_d):
                by_date[_dk].append(_t)
        return by_date, sorted(by_date.keys(), reverse=True)

    _entry_by_date, _sorted_entry_dates = _build_entry_grid(entry_sorted_trades, "e")
    _bt70_entry_sorted = pending_trades + sorted(
        [t for t in done_trades if (t.get("rec_score") or 0) >= 70],
        key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"],
        reverse=True
    )
    _bt70_entry_by_date, _sorted_bt70_entry_dates = _build_entry_grid(_bt70_entry_sorted, "b")

    def _group_by_month(sorted_dates):
        """sorted_dates(降順)を月ごとにグループ化。OrderedDict {ym: [dk,...]}"""
        from collections import OrderedDict
        result = OrderedDict()
        for dk in sorted_dates:
            ym = dk[:7]
            if ym not in result:
                result[ym] = []
            result[ym].append(dk)
        return result

    def _month_accordion_html(by_date, sorted_dates, dseq, pfx, expand_months=2):
        """月折りたたみアコーディオン＋インライン詳細HTML生成。"""
        by_month = _group_by_month(sorted_dates)
        html = ""
        for i, (ym, dks) in enumerate(by_month.items()):
            is_open   = (i < expand_months)
            ym_key    = ym.replace("-", "")
            all_t     = [t for dk in dks for t in by_date[dk]]
            done_m    = [t for t in all_t if t.get("reason") not in ("発注中", "保有中")]
            wins_m    = sum(1 for t in done_m if t["pnl"] > 0)
            wr_m      = wins_m / len(done_m) * 100 if done_m else 0
            pnl_m     = sum(t["pnl"] for t in done_m)
            pnl_col   = "#4ade80" if pnl_m >= 0 else "#f87171"
            arrow     = "▼" if is_open else "▶"
            body_disp = "block" if is_open else "none"
            btns_html = "".join(_entry_date_btn(dk, dseq, by_date, pfx) for dk in dks)
            dets_html = "".join(_entry_date_detail(dk, dseq, False, by_date, pfx) for dk in dks)
            html += (
                f'<div class="mg-block">'
                f'<div class="mg-header" onclick="toggleMG(\'{pfx}\',{dseq},\'{ym_key}\')">'
                f'<span class="mg-arrow" id="mg_arr_{pfx}{dseq}_{ym_key}">{arrow}</span>'
                f'<span class="mg-ym">{ym[:4]}/{ym[5:7]}月</span>'
                f'<span class="mg-stats">{len(done_m)}件&nbsp;{wr_m:.0f}%&nbsp;'
                f'<span style="color:{pnl_col};font-weight:700">{pnl_m:+,.0f}円</span></span>'
                f'</div>'
                f'<div class="mg-body" id="mgb_{pfx}{dseq}_{ym_key}" style="display:{body_disp}">'
                f'<div class="edate-grid">{btns_html}</div>'
                f'<div class="mg-detail" id="mgd_{pfx}{dseq}_{ym_key}">{dets_html}</div>'
                f'</div></div>\n'
            )
        return html

    def _entry_date_btn(dk, dseq, by_date, pfx):
        trades_d  = by_date[dk]
        done_d    = [t for t in trades_d if t.get("reason") not in ("発注中", "保有中")]
        wins_d    = sum(1 for t in done_d if t["pnl"] > 0)
        wr_d      = wins_d / len(done_d) * 100 if done_d else 0
        pnl_d     = sum(t["pnl"] for t in done_d)
        n_pend    = sum(1 for t in trades_d if t.get("reason") == "発注中")
        pnl_col   = "#4ade80" if pnl_d >= 0 else "#f87171"
        mm_dd     = dk[5:7] + "/" + dk[8:10]
        pend_span = (f'<span style="color:#fbbf24;font-size:0.66rem">発注中{n_pend}</span>'
                     if n_pend else "")
        dk_key = dk.replace("-", "")
        return (f'<button class="edate-btn" id="{pfx}date_btn_{dseq}_{dk_key}" '
                f'onclick="showEntryDate{pfx.upper()}({dseq},\'{dk_key}\')">'
                f'<span class="edate-mm">{mm_dd}</span>'
                f'<span class="edate-stat">{len(trades_d)}件 {wr_d:.0f}%</span>'
                f'<span class="edate-pnl" style="color:{pnl_col}">{pnl_d:+,.0f}</span>'
                f'{pend_span}</button>')

    def _entry_date_detail(dk, dseq, show, by_date, pfx):
        trades_d = by_date[dk]
        done_d   = [t for t in trades_d if t.get("reason") not in ("発注中", "保有中")]
        wins_d   = sum(1 for t in done_d if t["pnl"] > 0)
        wr_d     = wins_d / len(done_d) * 100 if done_d else 0
        pnl_d    = sum(t["pnl"] for t in done_d)
        pnl_col  = "#4ade80" if pnl_d >= 0 else "#f87171"
        rows_d   = "".join(_build_trade_row(t, entry_first=True) for t in trades_d)
        dk_key   = dk.replace("-", "")
        disp     = "block" if show else "none"
        return f"""<div id="{pfx}date_detail_{dseq}_{dk_key}" style="display:{disp}">
<div style="padding:8px 0 12px;margin-bottom:8px;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:16px">
  <span style="font-size:0.9rem;font-weight:700;color:#60a5fa">{dk} のエントリー</span>
  <span style="font-size:0.8rem;color:#94a3b8">{len(done_d)}件決済 &nbsp;勝率{wr_d:.0f}%
    &nbsp;損益<span style="color:{pnl_col};font-weight:700">{pnl_d:+,.0f}円</span></span>
</div>
<table>
  <thead><tr>
    <th>エントリー</th><th style="text-align:left">銘柄</th><th>戦略</th><th>設定</th>
    <th>約定値</th><th style="color:#f87171">損切り</th><th style="color:#4ade80">目標</th>
    <th>現在値</th><th>決済値</th><th>株数</th><th>保有</th><th>遅延</th>
    <th>損益</th><th>理由</th><th>決済日</th>
  </tr></thead>
  <tbody>{rows_d}</tbody>
</table>
</div>"""

    def _month_summary_html(trades_list):
        """月別サマリーバーを生成する（全期間）。"""
        from collections import defaultdict as _dd2
        by_month: dict = _dd2(list)
        for _t in trades_list:
            _dk = str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or "")
            if len(_dk) >= 7:
                by_month[_dk[:7]].append(_t)
        rows = ""
        for ym in sorted(by_month.keys(), reverse=True):
            trades_m = by_month[ym]
            done_m   = [t for t in trades_m if t.get("reason") not in ("発注中", "保有中")]
            wins_m   = sum(1 for t in done_m if t["pnl"] > 0)
            wr_m     = wins_m / len(done_m) * 100 if done_m else 0
            gp_m     = sum(t["pnl"] for t in done_m if t["pnl"] > 0)
            gl_m     = abs(sum(t["pnl"] for t in done_m if t["pnl"] <= 0))
            pnl_m    = sum(t["pnl"] for t in done_m)
            pnl_col  = "#4ade80" if pnl_m >= 0 else "#f87171"
            bar_w    = min(abs(pnl_m) / 300000 * 100, 100)  # 30万円で100%
            bar_col  = "rgba(74,222,128,0.25)" if pnl_m >= 0 else "rgba(248,113,113,0.25)"
            mm       = ym[5:7] + "月"
            rows += (f'<tr>'
                     f'<td style="font-weight:700;color:#e2e8f0;white-space:nowrap">{ym[:4]}/{mm}</td>'
                     f'<td style="text-align:right;color:#94a3b8">{len(done_m)}件</td>'
                     f'<td style="text-align:right;color:#94a3b8">{wr_m:.0f}%</td>'
                     f'<td style="text-align:right;color:#4ade80">+{gp_m:,.0f}円</td>'
                     f'<td style="text-align:right;color:#f87171">-{gl_m:,.0f}円</td>'
                     f'<td style="width:160px;position:relative;padding:4px 8px">'
                     f'<div style="position:absolute;top:4px;bottom:4px;left:{"50%" if pnl_m>=0 else f"calc(50% - {bar_w/2:.1f}%)"};'
                     f'width:{bar_w/2:.1f}%;background:{bar_col};border-radius:2px"></div>'
                     f'<span style="position:relative;font-weight:700;color:{pnl_col}">{pnl_m:+,.0f}円</span>'
                     f'</td>'
                     f'</tr>')
        return f"""<div style="margin-bottom:14px">
<table style="border-collapse:collapse;width:auto">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.78rem;padding:3px 8px">月</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px">件数</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px">勝率</th>
    <th style="color:#4ade80;font-size:0.78rem;padding:3px 8px">利益</th>
    <th style="color:#f87171;font-size:0.78rem;padding:3px 8px">損失</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px;text-align:center">損益合計</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>"""

    global _DETAIL_TAB_SEQ
    # ── 目標達成速度分析 ──────────────────────────────────────────
    def _speed_analysis_html(trades_list):
        """戦略×con/agg別 目標達成速度テーブルを生成。"""
        from collections import defaultdict as _dd3
        target_trades = [t for t in trades_list
                         if t.get("reason") == "目標達成"
                         or (t.get("reason") == "タイムカット" and (t.get("pnl") or 0) > 0)]
        n_tgt  = sum(1 for t in target_trades if t.get("reason") == "目標達成")
        n_tcut = len(target_trades) - n_tgt
        if len(target_trades) < 5:
            return ""

        # 戦略×mode でグループ化
        by_strat: dict = _dd3(list)
        for _t in target_trades:
            _strat = _t.get("strategy", "?")
            _cfg   = _t.get("cfg_label", "")
            _mode  = "agg" if "/agg" in _cfg else "con"
            by_strat[(_strat, _mode)].append(_t.get("hold_days", 0))

        # BT帯別
        by_bt: dict = _dd3(list)
        for _t in target_trades:
            _bt = _t.get("rec_score") or 0
            _band = "BT80+" if _bt >= 80 else ("BT70-79" if _bt >= 70 else ("BT60-69" if _bt >= 60 else "BT<60"))
            by_bt[_band].append(_t.get("hold_days", 0))

        def _med(lst):
            s = sorted(lst)
            n = len(s)
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

        def _spd_color(m):
            if m <= 3:   return "#4ade80"
            if m <= 7:   return "#facc15"
            return "#f87171"

        # 戦略テーブル（中央値昇順）
        strat_rows = ""
        strat_order = ["VOL", "DON", "MOM", "MACD", "A7", "RSI2"]
        rows_data = []
        for (_s, _m), days_list in sorted(by_strat.items(),
                                           key=lambda x: _med(x[1])):
            med = _med(days_list)
            avg = sum(days_list) / len(days_list)
            rows_data.append((_s, _m, med, avg, len(days_list)))

        rows_data.sort(key=lambda x: (x[2], x[3]))
        for (_s, _m, med, avg, cnt) in rows_data:
            col = _spd_color(med)
            mode_badge = (f'<span style="background:#334155;color:#94a3b8;font-size:0.7rem;'
                          f'padding:1px 5px;border-radius:3px">{_m}</span>')
            strat_rows += (f'<tr>'
                           f'<td style="text-align:left;font-weight:700;color:#e2e8f0">{_s} {mode_badge}</td>'
                           f'<td style="text-align:right;color:{col};font-weight:700">{med:.0f}日</td>'
                           f'<td style="text-align:right;color:#94a3b8">{avg:.1f}日</td>'
                           f'<td style="text-align:right;color:#94a3b8">{cnt}件</td>'
                           f'</tr>')

        # BT帯テーブル
        bt_rows = ""
        for band in ["BT80+", "BT70-79", "BT60-69", "BT<60"]:
            if band not in by_bt:
                continue
            dl = by_bt[band]
            med = _med(dl)
            avg = sum(dl) / len(dl)
            col = _spd_color(med)
            bt_rows += (f'<tr>'
                        f'<td style="text-align:left;font-weight:700;color:#e2e8f0">{band}</td>'
                        f'<td style="text-align:right;color:{col};font-weight:700">{med:.0f}日</td>'
                        f'<td style="text-align:right;color:#94a3b8">{avg:.1f}日</td>'
                        f'<td style="text-align:right;color:#94a3b8">{len(dl)}件</td>'
                        f'</tr>')

        # 速達ヒント（中央値3日以下のコンボ）
        fast_combos = [(s, m) for (s, m, med, avg, cnt) in rows_data if med <= 3 and cnt >= 3]
        hint = ""
        if fast_combos:
            combo_str = "、".join(f"{s}/{m}" for s, m in fast_combos[:6])
            hint = (f'<p style="margin:8px 0 0;color:#4ade80;font-size:0.82rem">'
                    f'⚡ 中央値3日以内の組合せ: <strong>{combo_str}</strong>（複数シグナルがある場合に優先）</p>')

        return f"""<h2 style="margin-top:24px">⑥ 目標達成速度分析（目標達成 {n_tgt}件 ＋ プラスタイムカット {n_tcut}件 = {len(target_trades)}件）</h2>
<p class="footnote">複数シグナルから選ぶ際の参考指標。中央値が短い戦略・設定を優先すると回転率が上がる。</p>
{hint}
<div style="display:flex;gap:32px;flex-wrap:wrap;margin-top:12px">
<div>
<p style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">戦略×設定別（中央値昇順）</p>
<table style="border-collapse:collapse">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.78rem;padding:3px 10px">戦略/設定</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">中央値</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">平均</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">件数</th>
  </tr></thead>
  <tbody>{strat_rows}</tbody>
</table>
</div>
<div>
<p style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">BTスコア帯別</p>
<table style="border-collapse:collapse">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.78rem;padding:3px 10px">BT帯</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">中央値</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">平均</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">件数</th>
  </tr></thead>
  <tbody>{bt_rows}</tbody>
</table>
</div>
</div>"""

    _speed_html = _speed_analysis_html(done_trades)

    # ── ⑥-2 保有期限深堀り：決済理由別×戦略別分析 ──────────────────────────
    def _exit_reason_analysis_html(trades_list) -> str:
        """決済理由（目標達成/損切り/タイムカット）を戦略別・全体で集計して表示。"""
        from collections import defaultdict as _dd4
        import backtest_limit_entry as _bte4
        settled = [t for t in trades_list
                   if t.get("reason") not in ("発注中", "保有中")]
        if len(settled) < 5:
            return ""

        max_hold = _bte4.MAX_HOLD
        REASONS = ["目標達成", "損切り", "タイムカット"]
        COLOR   = {"目標達成": "#4ade80", "損切り": "#f87171", "タイムカット": "#fbbf24"}

        # ── 全体サマリー ──
        total = len(settled)
        overall: dict[str, list] = {r: [] for r in REASONS}
        for t in settled:
            r = t.get("reason", "")
            if r in overall:
                overall[r].append(t)

        def _pf(trades):
            gp = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
            gl = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
            return gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)

        def _pf_str(v):
            return "∞" if v == float("inf") else f"{v:.2f}"

        def _avg_hold(trades):
            return sum(t.get("hold_days", 0) for t in trades) / len(trades) if trades else 0.0

        overall_rows = ""
        for r in REASONS:
            ts = overall[r]
            n  = len(ts)
            pct = n / total * 100 if total else 0.0
            pnl = sum(t.get("pnl", 0) for t in ts)
            pnl_col = "#4ade80" if pnl > 0 else "#f87171" if pnl < 0 else "#94a3b8"
            wins = sum(1 for t in ts if t.get("pnl", 0) > 0)
            wr   = wins / n * 100 if n else 0.0
            avg_h = _avg_hold(ts)
            pf_v = _pf(ts)
            bar_w = int(pct / 100 * 120)
            col = COLOR[r]
            overall_rows += f"""<tr>
  <td style="color:{col};font-weight:600;white-space:nowrap">{r}</td>
  <td style="text-align:right">{n}件</td>
  <td style="text-align:right">{pct:.0f}%
    <div style="height:6px;background:{col};width:{bar_w}px;margin-top:2px;border-radius:3px"></div></td>
  <td style="text-align:right">{wr:.0f}%</td>
  <td style="text-align:right;color:{pnl_col};font-weight:700">{pnl:+,.0f}円</td>
  <td style="text-align:right">{_pf_str(pf_v)}</td>
  <td style="text-align:right">{avg_h:.1f}日</td>
</tr>"""

        # タイムカット詳細（プラス vs マイナス）
        tcuts = overall["タイムカット"]
        tcut_plus  = [t for t in tcuts if t.get("pnl", 0) > 0]
        tcut_minus = [t for t in tcuts if t.get("pnl", 0) <= 0]
        tcut_detail = ""
        if tcuts:
            tp_pnl = sum(t["pnl"] for t in tcut_plus)
            tm_pnl = sum(t["pnl"] for t in tcut_minus)
            tcut_detail = f"""<p style="color:#fbbf24;font-size:0.8rem;margin:10px 0 4px">
  タイムカット内訳 (MAX_HOLD={max_hold}日):</p>
<table style="border-collapse:collapse;font-size:0.82rem">
  <tr><td style="padding:2px 10px;color:#4ade80">含み益で終了</td>
      <td style="padding:2px 10px;text-align:right">{len(tcut_plus)}件</td>
      <td style="padding:2px 10px;text-align:right;color:#4ade80">{tp_pnl:+,.0f}円</td></tr>
  <tr><td style="padding:2px 10px;color:#f87171">含み損で終了</td>
      <td style="padding:2px 10px;text-align:right">{len(tcut_minus)}件</td>
      <td style="padding:2px 10px;text-align:right;color:#f87171">{tm_pnl:+,.0f}円</td></tr>
</table>"""

        # ── 戦略別内訳 ──
        by_strat: dict = _dd4(list)
        for t in settled:
            by_strat[t.get("strategy", "?")].append(t)

        strat_rows = ""
        for strat, ts in sorted(by_strat.items(), key=lambda x: -len(x[1])):
            n = len(ts)
            pnl = sum(t.get("pnl", 0) for t in ts)
            pnl_col = "#4ade80" if pnl > 0 else "#f87171"
            wins = sum(1 for t in ts if t.get("pnl", 0) > 0)
            wr = wins / n * 100 if n else 0.0
            avg_h = _avg_hold(ts)
            reason_counts = {r: sum(1 for t in ts if t.get("reason") == r) for r in REASONS}
            reason_cells = ""
            for r in REASONS:
                rc = reason_counts[r]
                rp = rc / n * 100 if n else 0.0
                col = COLOR[r]
                reason_cells += f'<td style="text-align:right;color:{col}">{rc}件 <span style="color:#64748b">({rp:.0f}%)</span></td>'
            strat_rows += f"""<tr>
  <td style="color:#e2e8f0;font-weight:600;white-space:nowrap">{strat}</td>
  <td style="text-align:right">{n}</td>
  <td style="text-align:right">{wr:.0f}%</td>
  {reason_cells}
  <td style="text-align:right">{avg_h:.1f}日</td>
  <td style="text-align:right;color:{pnl_col};font-weight:700">{pnl:+,.0f}円</td>
</tr>"""

        return f"""<div style="margin:20px 0">
<h3 style="color:#e2e8f0;font-size:1rem;margin-bottom:12px">
  保有期限 MAX_HOLD={max_hold}日 — 決済理由分析
  <span style="font-size:0.78rem;color:#64748b;font-weight:400;margin-left:8px">
    決済済み{total}件 (保有中・発注中除外)</span>
</h3>

<div style="display:flex;gap:30px;flex-wrap:wrap">
<div>
<p style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">全体</p>
<table style="border-collapse:collapse;font-size:0.82rem">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;padding:3px 10px">理由</th>
    <th style="color:#94a3b8;padding:3px 10px">件数</th>
    <th style="color:#94a3b8;padding:3px 10px">割合</th>
    <th style="color:#94a3b8;padding:3px 10px">勝率</th>
    <th style="color:#94a3b8;padding:3px 10px">損益</th>
    <th style="color:#94a3b8;padding:3px 10px">PF</th>
    <th style="color:#94a3b8;padding:3px 10px">平均保有</th>
  </tr></thead>
  <tbody>{overall_rows}</tbody>
</table>
{tcut_detail}
</div>
</div>

<p style="color:#94a3b8;font-size:0.78rem;margin:14px 0 6px">戦略別</p>
<table style="border-collapse:collapse;font-size:0.82rem">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;padding:3px 10px">戦略</th>
    <th style="color:#94a3b8;padding:3px 10px">件数</th>
    <th style="color:#94a3b8;padding:3px 10px">勝率</th>
    <th style="color:#4ade80;padding:3px 10px">目標達成</th>
    <th style="color:#f87171;padding:3px 10px">損切り</th>
    <th style="color:#fbbf24;padding:3px 10px">タイムカット</th>
    <th style="color:#94a3b8;padding:3px 10px">平均保有</th>
    <th style="color:#94a3b8;padding:3px 10px">損益</th>
  </tr></thead>
  <tbody>{strat_rows}</tbody>
</table>
</div>"""

    _exit_reason_html = _exit_reason_analysis_html(done_trades)
    if _exit_reason_html:
        _speed_html = (_speed_html or "") + _exit_reason_html

    # ── タイムカット 寄り付き vs 引け 比較 ──────────────────────────────────
    def _timecut_open_vs_close_html(trades_list) -> str:
        """タイムカットトレードについて、引け決済 vs 翌日寄り付き決済を比較する。"""
        try:
            from backtest_limit_entry import fetch as _fetch_tc
        except ImportError:
            return ""

        tc_trades = [t for t in trades_list if t.get("reason") == "タイムカット"]
        if len(tc_trades) < 5:
            return ""

        # symbol ごとにキャッシュからOHLC取得
        import pandas as _pd_tc
        _df_cache: dict = {}

        def _get_next_open(symbol, exit_dt):
            if symbol not in _df_cache:
                try:
                    _df_cache[symbol] = _fetch_tc(symbol)
                except Exception:
                    _df_cache[symbol] = None
            df_s = _df_cache[symbol]
            if df_s is None or df_s.empty:
                return None
            # タイムゾーン差異を吸収してから比較
            exit_dt_norm = exit_dt.tz_localize(None) if getattr(exit_dt, "tz", None) else exit_dt
            idx = df_s.index
            try:
                idx_norm = idx.tz_localize(None) if idx.tz is not None else idx
            except Exception:
                idx_norm = idx
            try:
                mask = idx_norm > exit_dt_norm
            except Exception:
                return None
            future = df_s[mask]
            if future.empty:
                return None
            return float(future.iloc[0]["open"])

        rows_data = []
        for t in tc_trades:
            # exit_d_raw (datetime.date) を優先。なければ exit_dt 文字列をパース
            _exit_raw = t.get("exit_d_raw")
            if _exit_raw is not None:
                exit_dt = _pd_tc.Timestamp(_exit_raw)
            else:
                _s = t.get("exit_dt", "")
                try:
                    exit_dt = _pd_tc.Timestamp(_s)
                except Exception:
                    continue
            symbol  = t.get("symbol", "")
            # 方向はorder_stop vs entry_pで判定(ショート=stop > entry)
            order_stop = float(t.get("order_stop", 0))
            ep         = float(t.get("entry_p", 0))
            is_short   = (order_stop > ep) if (order_stop > 0 and ep > 0) else False
            qty     = int(t.get("qty", 100))
            cl_exit = float(t.get("exit_p", 0))  # 現在の引け決済価格
            next_op = _get_next_open(symbol, exit_dt)
            if next_op is None or cl_exit <= 0 or ep <= 0:
                continue
            # サニティチェック: next_opが終値の80%〜125%の範囲外なら異常値として除外
            if not (cl_exit * 0.80 <= next_op <= cl_exit * 1.25):
                continue
            # 翌日寄り付きで決済した場合のPnL差分
            if is_short:
                pnl_close    = (ep - cl_exit) * qty
                pnl_next_op  = (ep - next_op) * qty
            else:
                pnl_close    = (cl_exit - ep) * qty
                pnl_next_op  = (next_op - ep) * qty
            delta = pnl_next_op - pnl_close
            rows_data.append({
                "strategy": t.get("strategy", ""),
                "symbol":   symbol,
                "name":     t.get("name", ""),
                "exit_dt":  exit_dt,
                "cl_exit":  cl_exit,
                "next_op":  next_op,
                "pnl_close":   pnl_close,
                "pnl_next_op": pnl_next_op,
                "delta":    delta,
                "is_short": is_short,
            })

        if len(rows_data) < 3:
            return ""

        n_total       = len(rows_data)
        delta_total   = sum(r["delta"] for r in rows_data)
        pnl_close_sum = sum(r["pnl_close"] for r in rows_data)
        pnl_open_sum  = sum(r["pnl_next_op"] for r in rows_data)
        n_open_better = sum(1 for r in rows_data if r["delta"] > 0)
        n_close_better = sum(1 for r in rows_data if r["delta"] < 0)
        winner = "翌日寄り付き" if delta_total > 0 else "当日引け"
        winner_color = "#4ade80" if delta_total > 0 else "#94a3b8"

        # 戦略別集計
        from collections import defaultdict as _dd_tc
        strat_agg: dict = _dd_tc(lambda: {"n": 0, "delta": 0.0, "pnl_c": 0.0, "pnl_o": 0.0, "n_open_better": 0})
        for r in rows_data:
            s = r["strategy"]
            strat_agg[s]["n"] += 1
            strat_agg[s]["delta"] += r["delta"]
            strat_agg[s]["pnl_c"] += r["pnl_close"]
            strat_agg[s]["pnl_o"] += r["pnl_next_op"]
            if r["delta"] > 0:
                strat_agg[s]["n_open_better"] += 1

        def _fmt(v):
            s = f"{int(v):,}"
            return f'<span style="color:#4ade80">+{s}</span>' if v > 0 else (f'<span style="color:#f87171">{s}</span>' if v < 0 else s)

        strat_rows_html = ""
        for strat in sorted(strat_agg.keys()):
            ag = strat_agg[strat]
            pct_open = ag["n_open_better"] / ag["n"] * 100 if ag["n"] > 0 else 0
            winner_s = "寄り付き有利" if ag["delta"] > 0 else "引け有利"
            wc = "#4ade80" if ag["delta"] > 0 else "#94a3b8"
            strat_rows_html += f"""<tr style="border-bottom:1px solid #334155">
  <td style="padding:4px 10px;color:#e2e8f0">{strat}</td>
  <td style="padding:4px 10px;text-align:right;color:#94a3b8">{ag['n']}</td>
  <td style="padding:4px 10px;text-align:right">{_fmt(ag['pnl_c'])}</td>
  <td style="padding:4px 10px;text-align:right">{_fmt(ag['pnl_o'])}</td>
  <td style="padding:4px 10px;text-align:right">{_fmt(ag['delta'])}</td>
  <td style="padding:4px 10px;text-align:right;color:#94a3b8">{pct_open:.0f}%</td>
  <td style="padding:4px 10px;text-align:center;color:{wc};font-weight:bold">{winner_s}</td>
</tr>"""

        return f"""
<div style="background:#1e293b;border-radius:8px;padding:16px;margin:16px 0">
  <h3 style="color:#f8fafc;margin:0 0 4px">⏰ タイムカット: 当日引け vs 翌日寄り付き 比較</h3>
  <p style="color:#94a3b8;font-size:12px;margin:0 0 12px">
    保有期限到達時に「当日引け成行(MOC)」で売るか「翌日寄付き成行(MOO)」で売るかの損益比較。
    対象: タイムカット {n_total}件
  </p>

  <div style="display:flex;gap:16px;margin-bottom:16px;flex-wrap:wrap">
    <div style="background:#0f172a;border-radius:6px;padding:12px 20px;text-align:center">
      <div style="color:#94a3b8;font-size:11px">当日引け合計損益</div>
      <div style="font-size:20px;font-weight:bold">{_fmt(pnl_close_sum)}</div>
    </div>
    <div style="background:#0f172a;border-radius:6px;padding:12px 20px;text-align:center">
      <div style="color:#94a3b8;font-size:11px">翌日寄り付き合計損益</div>
      <div style="font-size:20px;font-weight:bold">{_fmt(pnl_open_sum)}</div>
    </div>
    <div style="background:#0f172a;border-radius:6px;padding:12px 20px;text-align:center">
      <div style="color:#94a3b8;font-size:11px">差分（寄り付き－引け）</div>
      <div style="font-size:20px;font-weight:bold">{_fmt(delta_total)}</div>
    </div>
    <div style="background:#0f172a;border-radius:6px;padding:12px 20px;text-align:center">
      <div style="color:#94a3b8;font-size:11px">結論</div>
      <div style="font-size:18px;font-weight:bold;color:{winner_color}">{winner}が有利</div>
      <div style="color:#64748b;font-size:11px">寄り付き有利 {n_open_better}件 / 引け有利 {n_close_better}件</div>
    </div>
  </div>

  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="border-bottom:2px solid #475569">
      <th style="color:#94a3b8;padding:4px 10px;text-align:left">戦略</th>
      <th style="color:#94a3b8;padding:4px 10px;text-align:right">件数</th>
      <th style="color:#94a3b8;padding:4px 10px;text-align:right">引け損益</th>
      <th style="color:#94a3b8;padding:4px 10px;text-align:right">寄り付き損益</th>
      <th style="color:#94a3b8;padding:4px 10px;text-align:right">差分</th>
      <th style="color:#94a3b8;padding:4px 10px;text-align:right">寄り付き有利%</th>
      <th style="color:#94a3b8;padding:4px 10px;text-align:center">判定</th>
    </tr></thead>
    <tbody>{strat_rows_html}</tbody>
  </table>
  <p style="color:#64748b;font-size:11px;margin:8px 0 0">
    ※ 翌日寄り付き損益 = タイムカット翌営業日の始値で決済した場合の仮想損益（スリッページ除く）
  </p>
</div>"""

    _tc_cmp_html = _timecut_open_vs_close_html(done_trades)
    if _tc_cmp_html:
        _speed_html = (_speed_html or "") + _tc_cmp_html

    # ── ⑦ 損切りパターン分析（BT70以上）────────────────────────────────────
    def _stop_pattern_html(trades_list):
        """BT70以上の損切りトレードについて、損切り日のOHLC vs 損切り価格を分析する。
        ①終値割れ（close < stop）vs ②ヒゲのみ（low < stop ≤ close）を分類。
        close モードでは②は発生しない（回避済み）ことを確認する用途も兼ねる。
        """
        try:
            from backtest_limit_entry import fetch as _fetch_ohlc
        except ImportError:
            return ""

        bt70_stops = sorted(
            [t for t in trades_list
             if t.get("reason") == "損切り" and (t.get("rec_score") or 0) >= 70],
            key=lambda x: x.get("exit_d_raw") or _date.min, reverse=True
        )
        if len(bt70_stops) < 2:
            return ""

        # 損切り日OHLCを取得して分類
        analyzed = []
        for t in bt70_stops[:50]:  # 最新50件
            sym    = t.get("symbol", "")
            stop_p = t.get("order_stop", 0)
            exit_d = t.get("exit_d_raw")
            if not stop_p or not exit_d:
                analyzed.append({**t, "day_low": None, "day_close": None,
                                  "pattern": "?", "close_gap": None, "low_gap": None})
                continue
            try:
                df = _fetch_ohlc(sym, 400)
                if df is None or df.empty:
                    raise ValueError("no data")
                df.index = pd.to_datetime(df.index)
                mask = [idx.date() == exit_d for idx in df.index]
                if not any(mask):
                    raise ValueError("date not found")
                row_idx = next(i for i, m in enumerate(mask) if m)
                row = df.iloc[row_idx]
                day_low   = float(row["low"])
                day_close = float(row["close"])
                # close < stop → ① 終値割れ（closeモードの損切り条件そのもの）
                # low < stop ≤ close → ② ヒゲのみ（closeモードでは理論上発生しない）
                if day_close < stop_p:
                    pattern = "①終値割れ"
                elif day_low < stop_p:
                    pattern = "②ヒゲのみ"
                else:
                    pattern = "参照不一致"
                close_gap = (day_close / stop_p - 1) * 100
                low_gap   = (day_low   / stop_p - 1) * 100
                analyzed.append({**t, "day_low": day_low, "day_close": day_close,
                                  "pattern": pattern, "close_gap": close_gap,
                                  "low_gap": low_gap})
            except Exception:
                analyzed.append({**t, "day_low": None, "day_close": None,
                                  "pattern": "?", "close_gap": None, "low_gap": None})

        if not analyzed:
            return ""

        n1 = sum(1 for a in analyzed if a["pattern"] == "①終値割れ")
        n2 = sum(1 for a in analyzed if a["pattern"] == "②ヒゲのみ")
        nq = sum(1 for a in analyzed if a["pattern"] in ("?", "参照不一致"))

        # 深さ分布（①終値割れのみ）
        gaps = [a["close_gap"] for a in analyzed if a["close_gap"] is not None and a["pattern"] == "①終値割れ"]
        depth_html = ""
        if gaps:
            buckets = [
                ("ギリギリ割れ（−1%以内）",  [g for g in gaps if g >= -1.0]),
                ("小幅割れ（−1〜−3%）",      [g for g in gaps if -3.0 <= g < -1.0]),
                ("中幅割れ（−3〜−6%）",      [g for g in gaps if -6.0 <= g < -3.0]),
                ("大幅割れ（−6%超）",         [g for g in gaps if g < -6.0]),
            ]
            depth_rows = ""
            for label, lst in buckets:
                if not lst:
                    continue
                pct = len(lst) / len(gaps) * 100
                bar_w = max(4, int(pct * 1.5))
                avg_g = sum(lst) / len(lst)
                depth_rows += (
                    f'<tr>'
                    f'<td style="text-align:left;color:#e2e8f0;padding:3px 10px">{label}</td>'
                    f'<td style="text-align:right;color:#94a3b8;padding:3px 10px">{len(lst)}件</td>'
                    f'<td style="text-align:right;color:#f87171;padding:3px 10px">{avg_g:+.1f}%</td>'
                    f'<td style="padding:3px 10px"><div style="width:{bar_w}px;height:10px;background:#f87171;border-radius:2px;display:inline-block"></div></td>'
                    f'</tr>'
                )
            depth_html = f"""
<div>
<p style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">①終値割れの深さ分布（終値 vs 損切り価格）</p>
<table style="border-collapse:collapse">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.78rem;padding:3px 10px">カテゴリ</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">件数</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">平均乖離</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px"></th>
  </tr></thead>
  <tbody>{depth_rows}</tbody>
</table>
</div>"""

        # 明細テーブル（直近20件）
        detail_rows = ""
        for a in analyzed[:20]:
            sym_disp  = str(a.get("symbol","")).split(".")[0]
            name_disp = a.get("name","")[:8]
            strat     = a.get("strategy","")
            exit_dt   = a.get("exit_dt","")
            stop_p    = a.get("order_stop", 0)
            pnl       = a.get("pnl", 0)
            pnl_col   = "#f87171"
            pat       = a["pattern"]

            if a["close_gap"] is not None:
                close_str = f'{a["day_close"]:,.0f}（{a["close_gap"]:+.1f}%）'
                low_str   = f'{a["day_low"]:,.0f}（{a["low_gap"]:+.1f}%）'
            else:
                close_str = "—"
                low_str   = "—"

            if pat == "①終値割れ":
                pat_html = '<span style="color:#f87171;font-weight:700">①終値割れ</span>'
            elif pat == "②ヒゲのみ":
                pat_html = '<span style="color:#4ade80;font-weight:700">②ヒゲのみ</span>'
            else:
                pat_html = f'<span style="color:#64748b">{pat}</span>'

            detail_rows += (
                f'<tr>'
                f'<td style="text-align:left;padding:3px 8px;color:#e2e8f0">{exit_dt}</td>'
                f'<td style="text-align:left;padding:3px 8px;color:#e2e8f0">{sym_disp} {name_disp}</td>'
                f'<td style="padding:3px 8px;color:#94a3b8">{strat}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:#94a3b8">{stop_p:,.0f}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:#94a3b8">{low_str}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:#94a3b8">{close_str}</td>'
                f'<td style="padding:3px 8px">{pat_html}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:{pnl_col}">{pnl:+,.0f}円</td>'
                f'</tr>'
            )

        n2_note = (f'<p style="color:#4ade80;font-size:0.82rem;margin:8px 0 0">'
                   f'✅ ②ヒゲのみ: {n2}件 — closeモードが回避済み（終値は損切りライン上）</p>'
                   if n2 > 0 else
                   f'<p style="color:#94a3b8;font-size:0.82rem;margin:8px 0 0">'
                   f'✅ ②ヒゲのみ: 0件（closeモードにより全てのヒゲ刈りを回避）</p>')

        return f"""<h2 style="margin-top:24px">⑦ BT70以上 損切りパターン分析（{len(analyzed)}件）</h2>
<p class="footnote">closeモードでは終値が損切りラインを超えた場合のみ決済。①のみが発生し、②（ヒゲのみ）はcloseモードで回避済み。</p>
<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px">
  <div style="background:#1e293b;padding:10px 20px;border-radius:6px;text-align:center">
    <div style="color:#f87171;font-size:1.4rem;font-weight:700">{n1}</div>
    <div style="color:#94a3b8;font-size:0.78rem">①終値割れ</div>
  </div>
  <div style="background:#1e293b;padding:10px 20px;border-radius:6px;text-align:center">
    <div style="color:#4ade80;font-size:1.4rem;font-weight:700">{n2}</div>
    <div style="color:#94a3b8;font-size:0.78rem">②ヒゲのみ</div>
  </div>
  {f'<div style="background:#1e293b;padding:10px 20px;border-radius:6px;text-align:center"><div style="color:#64748b;font-size:1.4rem;font-weight:700">{nq}</div><div style="color:#94a3b8;font-size:0.78rem">データ取得不可</div></div>' if nq else ''}
</div>
{n2_note}
<div style="display:flex;gap:32px;flex-wrap:wrap;margin-top:16px">
{depth_html}
<div>
<p style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">損切り明細（直近20件、損切り日終値との比較）</p>
<div style="overflow-x:auto">
<table style="border-collapse:collapse;font-size:0.82rem">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.75rem;padding:3px 8px">決済日</th>
    <th style="text-align:left;color:#94a3b8;font-size:0.75rem;padding:3px 8px">銘柄</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">戦略</th>
    <th style="color:#f87171;font-size:0.75rem;padding:3px 8px">損切り価格</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">当日安値</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">当日終値</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">パターン</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">損益</th>
  </tr></thead>
  <tbody>{detail_rows}</tbody>
</table>
</div>
</div>
</div>"""

    _stop_pattern_html_str = _stop_pattern_html(done_trades)

    # ── ⑧ 保有中の2回目以降シグナル成績分析 ────────────────────────────────
    def _overlap_analysis_html(overlap_dropped):
        """保有中に同一銘柄で発生した2回目以降のシグナルの成績分析。"""
        from datetime import date as _date2
        settled = [t for t in overlap_dropped
                   if t.get("reason") not in ("発注中", "保有中", None)
                   and t.get("pnl") is not None]
        if len(settled) < 3:
            return ""

        wins   = [t for t in settled if (t.get("pnl") or 0) > 0]
        losses = [t for t in settled if (t.get("pnl") or 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in settled)
        win_rate  = len(wins) / len(settled) * 100
        win_pnl   = sum(t.get("pnl", 0) for t in wins)
        loss_pnl  = abs(sum(t.get("pnl", 0) for t in losses))
        pf_val    = win_pnl / loss_pnl if loss_pnl > 0 else 9.99
        pf_str    = f"{pf_val:.2f}" if pf_val < 9.99 else "∞"
        pnl_col   = "#4ade80" if total_pnl >= 0 else "#f87171"
        wr_col    = "#4ade80" if win_rate >= 55 else ("#facc15" if win_rate >= 45 else "#f87171")

        # BT70以上フィルター
        bt70_settled = [t for t in settled if (t.get("rec_score") or 0) >= 70]
        def _band_kpi(lst):
            if not lst:
                return 0.0, "∞", 0, 0, 0, 0, 0
            w = [t for t in lst if (t.get("pnl") or 0) > 0]
            l = [t for t in lst if (t.get("pnl") or 0) <= 0]
            wr = len(w) / len(lst) * 100
            gp = sum(t.get("pnl", 0) for t in w)
            gl = abs(sum(t.get("pnl", 0) for t in l))
            pf = gp / gl if gl > 0 else 9.99
            pf_s = f"{pf:.2f}" if pf < 9.99 else "∞"
            return wr, pf_s, gp - gl, len(w), len(l), gp, gl

        bt70_wr, bt70_pf, bt70_pnl, bt70_w, bt70_l, _bt70_gp, _bt70_gl = _band_kpi(bt70_settled)

        # BT帯別集計
        def _bt_band_row(label, lst, hl_color=None):
            if not lst:
                return f'<tr><td style="color:#94a3b8;padding:3px 10px">{label}</td><td colspan="6" style="color:#475569;padding:3px 10px">-</td></tr>'
            wr, pf_s, pnl, w, l, gp, gl = _band_kpi(lst)
            wc  = "#4ade80" if wr >= 60 else ("#facc15" if wr >= 50 else "#f87171")
            pc  = "#4ade80" if pnl >= 0 else "#f87171"
            nm  = hl_color or "#e2e8f0"
            return (f'<tr>'
                    f'<td style="color:{nm};padding:3px 10px;font-weight:{"700" if hl_color else "400"}">{label}</td>'
                    f'<td style="text-align:right;color:{wc};padding:3px 10px">{wr:.0f}%</td>'
                    f'<td style="text-align:right;color:#e2e8f0;padding:3px 10px">{pf_s}</td>'
                    f'<td style="text-align:right;color:#4ade80;padding:3px 10px">+{gp:,.0f}円</td>'
                    f'<td style="text-align:right;color:#f87171;padding:3px 10px">-{gl:,.0f}円</td>'
                    f'<td style="text-align:right;color:{pc};padding:3px 10px">{pnl:+,.0f}円</td>'
                    f'<td style="text-align:right;color:#94a3b8;padding:3px 10px">{w+l}件</td>'
                    f'</tr>')

        bt_band_rows = (
            _bt_band_row("BT80以上",  [t for t in settled if (t.get("rec_score") or 0) >= 80], "#4ade80")
          + _bt_band_row("BT70-79",   [t for t in settled if 70 <= (t.get("rec_score") or 0) < 80], "#86efac")
          + _bt_band_row("BT60-69",   [t for t in settled if 60 <= (t.get("rec_score") or 0) < 70])
          + _bt_band_row("BT60未満",  [t for t in settled if (t.get("rec_score") or 0) < 60])
        )

        # 戦略別集計（BT70以上）
        from collections import defaultdict as _dd8
        by_strat = _dd8(list)
        for t in bt70_settled:
            by_strat[t.get("strategy","?")].append(t)
        strat_rows = ""
        for s, lst in sorted(by_strat.items(), key=lambda x: -sum(t.get("pnl",0) for t in x[1])):
            w = sum(1 for t in lst if (t.get("pnl") or 0) > 0)
            wr = w / len(lst) * 100
            gp = sum(t.get("pnl", 0) for t in lst if (t.get("pnl") or 0) > 0)
            gl = abs(sum(t.get("pnl", 0) for t in lst if (t.get("pnl") or 0) <= 0))
            pnl = gp - gl
            pc = "#4ade80" if pnl >= 0 else "#f87171"
            wc = "#4ade80" if wr >= 55 else ("#facc15" if wr >= 45 else "#f87171")
            strat_rows += (f'<tr>'
                           f'<td style="text-align:left;color:#e2e8f0;padding:3px 10px">{s}</td>'
                           f'<td style="text-align:right;color:{wc};padding:3px 10px">{wr:.0f}%</td>'
                           f'<td style="text-align:right;color:#4ade80;padding:3px 10px">+{gp:,.0f}円</td>'
                           f'<td style="text-align:right;color:#f87171;padding:3px 10px">-{gl:,.0f}円</td>'
                           f'<td style="text-align:right;color:{pc};padding:3px 10px">{pnl:+,.0f}円</td>'
                           f'<td style="text-align:right;color:#94a3b8;padding:3px 10px">{len(lst)}件</td>'
                           f'</tr>')

        # 明細テーブル（BT70以上 直近25件）
        detail_rows = ""
        for t in sorted(bt70_settled, key=lambda x: x.get("exit_d_raw") or _date2.min, reverse=True)[:25]:
            sym   = str(t.get("symbol","")).split(".")[0]
            name  = t.get("name","")[:8]
            strat = t.get("strategy","")
            pnl   = t.get("pnl", 0)
            reas  = t.get("reason","")
            pc    = "#4ade80" if pnl > 0 else "#f87171"
            if reas == "目標達成":
                rc = '<span style="color:#4ade80">目標達成</span>'
            elif reas == "損切り":
                rc = '<span style="color:#f87171">損切り</span>'
            elif reas == "タイムカット":
                rc = f'<span style="color:{"#4ade80" if pnl > 0 else "#f87171"}">タイムカット</span>'
            else:
                rc = f'<span style="color:#94a3b8">{reas}</span>'
            ep   = t.get("entry_p", 0)
            xp   = t.get("exit_p", 0)
            hold = t.get("hold_days", 0)
            bt   = t.get("rec_score") or 0
            bt_c = "#4ade80" if bt >= 70 else ("#facc15" if bt >= 60 else "#94a3b8")
            detail_rows += (
                f'<tr>'
                f'<td style="color:#94a3b8;padding:3px 8px">{t.get("exit_dt","")}</td>'
                f'<td style="text-align:left;padding:3px 8px;color:#e2e8f0">{sym} {name}</td>'
                f'<td style="padding:3px 8px;color:#94a3b8">{strat}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:{bt_c}">BT:{bt}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:#94a3b8">{ep:,.0f}→{xp:,.0f}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:#94a3b8">{hold}日</td>'
                f'<td style="padding:3px 8px">{rc}</td>'
                f'<td style="text-align:right;padding:3px 8px;color:{pc}">{pnl:+,.0f}円</td>'
                f'</tr>'
            )

        bt70_wr_col  = "#4ade80" if bt70_wr >= 55 else ("#facc15" if bt70_wr >= 45 else "#f87171")
        bt70_pnl_col = "#4ade80" if bt70_pnl >= 0 else "#f87171"
        return f"""<h2 style="margin-top:24px">⑧ 保有中の2回目以降シグナル成績（{len(settled)}件）</h2>
<p class="footnote">1銘柄1ポジション制で弾かれた「既保有中の同銘柄シグナル」を仮発注した場合の成績。<br>
計測外だが参考として: 勝率・損益が1回目と同程度なら追加エントリーの根拠になる。</p>

<div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:8px;align-items:flex-start">
<div>
<p style="color:#94a3b8;font-size:0.75rem;margin:0 0 6px">全体（{len(settled)}件）</p>
<div style="display:flex;gap:10px;flex-wrap:wrap">
  <div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">
    <div style="color:{wr_col};font-size:1.3rem;font-weight:700">{win_rate:.1f}%</div>
    <div style="color:#94a3b8;font-size:0.72rem">勝率 ({len(wins)}W/{len(losses)}L)</div>
  </div>
  <div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">
    <div style="color:#e2e8f0;font-size:1.3rem;font-weight:700">{pf_str}</div>
    <div style="color:#94a3b8;font-size:0.72rem">PF</div>
  </div>
  <div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">
    <div style="color:{pnl_col};font-size:1.3rem;font-weight:700">{total_pnl:+,.0f}円</div>
    <div style="color:#94a3b8;font-size:0.72rem">合計損益</div>
  </div>
</div>
</div>
<div>
<p style="color:#4ade80;font-size:0.75rem;margin:0 0 6px">★ BT70以上のみ（{len(bt70_settled)}件）</p>
<div style="display:flex;gap:10px;flex-wrap:wrap">
  <div style="background:#0d2818;border:1px solid #166534;padding:8px 16px;border-radius:6px;text-align:center">
    <div style="color:{bt70_wr_col};font-size:1.3rem;font-weight:700">{bt70_wr:.1f}%</div>
    <div style="color:#94a3b8;font-size:0.72rem">勝率 ({bt70_w}W/{bt70_l}L)</div>
  </div>
  <div style="background:#0d2818;border:1px solid #166534;padding:8px 16px;border-radius:6px;text-align:center">
    <div style="color:#e2e8f0;font-size:1.3rem;font-weight:700">{bt70_pf}</div>
    <div style="color:#94a3b8;font-size:0.72rem">PF</div>
  </div>
  <div style="background:#0d2818;border:1px solid #166534;padding:8px 16px;border-radius:6px;text-align:center">
    <div style="color:{bt70_pnl_col};font-size:1.3rem;font-weight:700">{bt70_pnl:+,.0f}円</div>
    <div style="color:#94a3b8;font-size:0.72rem">合計損益</div>
  </div>
</div>
</div>
</div>

<div style="display:flex;gap:32px;flex-wrap:wrap;margin-top:16px">
<div>
<p style="color:#94a3b8;font-size:0.78rem;margin-bottom:6px">BT帯別集計</p>
<table style="border-collapse:collapse">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.78rem;padding:3px 10px">BT帯</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">勝率</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">PF</th>
    <th style="color:#4ade80;font-size:0.78rem;padding:3px 10px">利益</th>
    <th style="color:#f87171;font-size:0.78rem;padding:3px 10px">損失</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">損益</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">件数</th>
  </tr></thead>
  <tbody>{bt_band_rows}</tbody>
</table>
</div>
<div>
<p style="color:#4ade80;font-size:0.78rem;margin-bottom:6px">戦略別（BT70以上）</p>
<table style="border-collapse:collapse">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.78rem;padding:3px 10px">戦略</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">勝率</th>
    <th style="color:#4ade80;font-size:0.78rem;padding:3px 10px">利益</th>
    <th style="color:#f87171;font-size:0.78rem;padding:3px 10px">損失</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">損益</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 10px">件数</th>
  </tr></thead>
  <tbody>{strat_rows}</tbody>
</table>
</div>
<div style="overflow-x:auto">
<p style="color:#4ade80;font-size:0.78rem;margin-bottom:6px">明細（BT70以上 直近25件）</p>
<table style="border-collapse:collapse;font-size:0.82rem">
  <thead><tr>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">決済日</th>
    <th style="text-align:left;color:#94a3b8;font-size:0.75rem;padding:3px 8px">銘柄</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">戦略</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">BT</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">約定→決済</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">保有</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">理由</th>
    <th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">損益</th>
  </tr></thead>
  <tbody>{detail_rows}</tbody>
</table>
</div>
</div>"""

    _overlap_html = _overlap_analysis_html(_overlap_dropped)

    # ── ⑨ エントリータイミング比較（2軸：注文保持期間 / エントリー遅延 / ローリング比較）──
    def _entry_timing_cmp_html(trades_list):
        try:
            from backtest_limit_entry import (
                ROLLING_ENTRY as _RE, ENTRY_EXPIRE as _EE,
                run_limit_backtest as _rbt, fetch as _fetch9,
            )
        except Exception:
            _RE, _EE, _rbt, _fetch9 = 0, 1, None, None
        done = [t for t in trades_list if t.get("reason") not in ("発注中", "保有中")]

        # ── ⑨ キャッシュ（Rolling/em比較は重いので銘柄セット単位でキャッシュ）──
        # 日付ではなく days 単位のファイルを使い、銘柄/戦略セットが変わらない限り再利用
        import pickle as _pk9c
        from pathlib import Path as _P9c
        import datetime as _dt9c
        _9c_cache_dir = _P9c(".holdout_bt_cache")
        _9c_cache_dir.mkdir(exist_ok=True)
        _9c_cache_file = _9c_cache_dir / f"timing9cmp_{days}d.pkl"
        _9c_cache: dict = {}
        if _9c_cache_file.exists():
            try:
                with open(_9c_cache_file, "rb") as _f9c:
                    _9c_cache = _pk9c.load(_f9c)
                print(f"[⑨ キャッシュ] ロード: {_9c_cache_file} ({len(_9c_cache)}件)")
            except Exception:
                _9c_cache = {}

        def _9c_save():
            try:
                with open(_9c_cache_file, "wb") as _f9c:
                    _pk9c.dump(_9c_cache, _f9c, protocol=_pk9c.HIGHEST_PROTOCOL)
                print(f"[⑨ キャッシュ] 保存: {_9c_cache_file}")
            except Exception as _e9c:
                print(f"[⑨ キャッシュ] 保存失敗: {_e9c}")

        def _st(ts):
            if not ts:
                return {"n":0,"wr":0,"pf":0,"pnl":0,"avg":0,"gw":0,"gl":0}
            wins = [t for t in ts if t["pnl"] > 0]
            gw   = sum(t["pnl"] for t in wins)
            gl   = abs(sum(t["pnl"] for t in ts if t["pnl"] <= 0))
            pnl  = sum(t["pnl"] for t in ts)
            return {"n":len(ts),"wr":len(wins)/len(ts)*100,
                    "pf": gw/gl if gl else float("inf"),
                    "pnl":pnl,"avg":pnl/len(ts),"gw":gw,"gl":gl}

        def _pf_str(v):
            return "∞" if v == float("inf") else f"{v:.2f}"

        def _row(label, s, highlight=False, note=""):
            bg   = "background:#0c1f3a;" if highlight else ""
            pcol = "#4ade80" if s["pnl"] >= 0 else "#f87171"
            wcol = "#4ade80" if s["wr"] >= 55 else ("#fbbf24" if s["wr"] >= 45 else "#f87171")
            pfc  = "#4ade80" if s["pf"] >= 1.5 else ("#fbbf24" if s["pf"] >= 1.0 else "#f87171")
            note_td = f'<td style="color:#64748b;font-size:0.75rem">{note}</td>' if note is not None else ""
            return (f'<tr style="{bg}">'
                    f'<td style="font-weight:700;color:#e2e8f0;padding:8px 12px">{label}</td>'
                    + (note_td if note is not None else "")
                    + f'<td style="text-align:right;color:#94a3b8">{s["n"]}件</td>'
                    f'<td style="text-align:right;color:{wcol};font-weight:700">{s["wr"]:.1f}%</td>'
                    f'<td style="text-align:right;color:{pfc};font-weight:700">{_pf_str(s["pf"])}</td>'
                    f'<td style="text-align:right;color:#4ade80">+{s["gw"]:,.0f}円</td>'
                    f'<td style="text-align:right;color:#f87171">-{s["gl"]:,.0f}円</td>'
                    f'<td style="text-align:right;color:{pcol};font-weight:700">{s["pnl"]:+,.0f}円</td>'
                    f'<td style="text-align:right;color:{pcol}">{s["avg"]:+,.0f}円</td>'
                    f'</tr>')

        # ── A: 注文保持期間（ENTRY_EXPIRE）分析 ──
        expire1 = [t for t in done if (t.get("days_to_fill") or 0) == 0]
        expire2 = [t for t in done if (t.get("days_to_fill") or 0) <= 1]
        expire3 = done  # 全件 = 現状のENTRY_EXPIRE=3

        s_exp1 = _st(expire1)
        s_exp2 = _st(expire2)
        s_exp3 = _st(expire3)

        expire_th = '<th style="text-align:left">区分（ENTRY_EXPIRE）</th><th style="color:#64748b;font-size:0.8rem;text-align:left">説明</th>'
        expire_rows = (
            _row("EXPIRE=1（翌日のみ有効）", s_exp1, note="翌日に約定しなければキャンセル")
            + _row("EXPIRE=2（2日間有効）",   s_exp2, note="翌日・翌々日の約定を含む")
            + _row("EXPIRE=3（3日間有効）★現状", s_exp3, highlight=True, note="現行設定")
        )

        # ── B: エントリー遅延（Delay）分析 ──
        delay0 = done                                                           # 全件（翌日入り）
        delay1 = [t for t in done if (t.get("days_to_fill") or 0) >= 1]       # 2日後から入り
        delay2 = [t for t in done if (t.get("days_to_fill") or 0) >= 2]       # 3日後から入り

        s_del0 = _st(delay0)
        s_del1 = _st(delay1)
        s_del2 = _st(delay2)

        delay_th = '<th style="text-align:left">区分（Delay）</th><th style="color:#64748b;font-size:0.8rem;text-align:left">説明</th>'
        delay_rows = (
            _row("delay=0（翌日から注文）★現状", s_del0, highlight=True, note="シグナル翌日に逆指値注文を出す（現行）")
            + _row("delay=1（2日後から注文）",   s_del1, note="シグナル翌日を見送り、2日後に注文")
            + _row("delay=2（3日後から注文）",   s_del2, note="シグナルから2日見送り、3日後に注文")
        )

        common_th_tail = '<th>件数</th><th>勝率</th><th>PF</th><th style="color:#4ade80">総利益</th><th style="color:#f87171">総損失</th><th>損益合計</th><th>平均損益</th>'

        # ── 月別: 注文保持期間 ──
        from collections import defaultdict as _dd9
        by_ym_exp = {1: _dd9(list), 2: _dd9(list), 3: _dd9(list)}
        for t in done:
            ym  = str(t.get("entry_d_raw") or t.get("exit_d_raw") or "")[:7]
            dtf = t.get("days_to_fill") or 0
            if not ym: continue
            by_ym_exp[3][ym].append(t)
            if dtf <= 1: by_ym_exp[2][ym].append(t)
            if dtf == 0: by_ym_exp[1][ym].append(t)

        all_ym = sorted(set(by_ym_exp[3].keys()), reverse=True)
        expire_month_rows = ""
        for ym in all_ym:
            s1 = _st(by_ym_exp[1][ym])
            s2 = _st(by_ym_exp[2][ym])
            s3 = _st(by_ym_exp[3][ym])
            def _pc(s): return "#4ade80" if s["pnl"] >= 0 else "#f87171"
            expire_month_rows += (
                f'<tr><td style="font-weight:700;color:#e2e8f0;padding:5px 10px">{ym[:4]}/{ym[5:7]}月</td>'
                f'<td style="text-align:right;color:#94a3b8">{s1["n"]}件</td>'
                f'<td style="text-align:right;color:#94a3b8">{s1["wr"]:.0f}%</td>'
                f'<td style="text-align:right;color:{_pc(s1)};font-weight:700">{s1["pnl"]:+,.0f}円</td>'
                f'<td style="border-left:1px solid #334155;text-align:right;color:#94a3b8">{s2["n"]}件</td>'
                f'<td style="text-align:right;color:#94a3b8">{s2["wr"]:.0f}%</td>'
                f'<td style="text-align:right;color:{_pc(s2)};font-weight:700">{s2["pnl"]:+,.0f}円</td>'
                f'<td style="border-left:1px solid #334155;text-align:right;color:#94a3b8">{s3["n"]}件</td>'
                f'<td style="text-align:right;color:#94a3b8">{s3["wr"]:.0f}%</td>'
                f'<td style="text-align:right;color:{_pc(s3)};font-weight:700">{s3["pnl"]:+,.0f}円</td>'
                f'</tr>'
            )

        # ── 月別: エントリー遅延 ──
        by_ym_del = {0: _dd9(list), 1: _dd9(list), 2: _dd9(list)}
        for t in done:
            ym  = str(t.get("entry_d_raw") or t.get("exit_d_raw") or "")[:7]
            dtf = t.get("days_to_fill") or 0
            if not ym: continue
            by_ym_del[0][ym].append(t)
            if dtf >= 1: by_ym_del[1][ym].append(t)
            if dtf >= 2: by_ym_del[2][ym].append(t)

        delay_month_rows = ""
        for ym in all_ym:
            s0 = _st(by_ym_del[0][ym])
            s1 = _st(by_ym_del[1][ym])
            s2 = _st(by_ym_del[2][ym])
            def _pc(s): return "#4ade80" if s["pnl"] >= 0 else "#f87171"
            delay_month_rows += (
                f'<tr><td style="font-weight:700;color:#e2e8f0;padding:5px 10px">{ym[:4]}/{ym[5:7]}月</td>'
                f'<td style="text-align:right;color:#94a3b8">{s0["n"]}件</td>'
                f'<td style="text-align:right;color:#94a3b8">{s0["wr"]:.0f}%</td>'
                f'<td style="text-align:right;color:{_pc(s0)};font-weight:700">{s0["pnl"]:+,.0f}円</td>'
                f'<td style="border-left:1px solid #334155;text-align:right;color:#94a3b8">{s1["n"]}件</td>'
                f'<td style="text-align:right;color:#94a3b8">{s1["wr"]:.0f}%</td>'
                f'<td style="text-align:right;color:{_pc(s1)};font-weight:700">{s1["pnl"]:+,.0f}円</td>'
                f'<td style="border-left:1px solid #334155;text-align:right;color:#94a3b8">{s2["n"]}件</td>'
                f'<td style="text-align:right;color:#94a3b8">{s2["wr"]:.0f}%</td>'
                f'<td style="text-align:right;color:{_pc(s2)};font-weight:700">{s2["pnl"]:+,.0f}円</td>'
                f'</tr>'
            )

        # ── C: ローリング逆指値 効果比較（rolling=0/1/2 を並列バックテスト）──
        # 集計レポート（複数銘柄）のみ実行。銘柄詳細タブ（1銘柄だけ）はスキップ
        _rolling_cmp_section = ""
        # 固有 (symbol, strategy) → name を先に収集して件数チェック
        _sym_strat9: dict = {}
        for _t9 in done:
            _k9 = (_t9.get("symbol"), _t9.get("strategy"))
            if _k9[0] and _k9[1] and _k9 not in _sym_strat9:
                _sym_strat9[_k9] = _t9.get("name", _k9[0])
        _MIN_SYMS_FOR_CMP = 3  # 3銘柄未満ならC/Dをスキップ
        if _rbt is not None and _fetch9 is not None and len(_sym_strat9) >= _MIN_SYMS_FOR_CMP:

            def _get_params9(strat9):
                for _m9, _et9 in [(_stop, "stop"), (_brk, "stop")]:
                    if hasattr(_m9, "STRATEGY_PARAMS") and strat9 in _m9.STRATEGY_PARAMS:
                        _cf9, _em9, _sm9, _tm9 = _m9.STRATEGY_PARAMS[strat9]
                        return _cf9, _em9, _sm9, _tm9, _et9
                for _mn9 in ["check_signals_short", "check_signals_short_breakout"]:
                    try:
                        import importlib as _il9
                        _mod9 = _il9.import_module(_mn9)
                        if strat9 in _mod9.STRATEGY_PARAMS:
                            _cf9, _em9, _sm9, _tm9 = _mod9.STRATEGY_PARAMS[strat9]
                            return _cf9, _em9, _sm9, _tm9, "stop_sell"
                    except Exception:
                        pass
                return None

            # キャッシュキー: ("rolling", frozenset(sym_strat pairs))
            _roll_cache_key = ("rolling", frozenset(_sym_strat9.keys()))
            if _roll_cache_key in _9c_cache:
                _trades_by_roll = _9c_cache[_roll_cache_key]
                print(f"[⑨ Rolling比較] キャッシュ使用: " + " / ".join(f"rolling={r}: {len(_trades_by_roll[r])}件" for r in (0,1,2)))
            else:
                # rolling=0/1/2 それぞれで1回ずつバックテスト実行（1銘柄×3rolling を並列）
                def _run_one9(args9):
                    (_s9, _st9), _nm9, _roll9 = args9
                    _p9 = _get_params9(_st9)
                    if not _p9:
                        return _roll9, []
                    _cf9, _em9, _sm9, _tm9, _et9 = _p9
                    _df9 = _fetch9(_s9, days + 60)
                    if _df9 is None:
                        return _roll9, []
                    try:
                        _r9 = _rbt(_s9, _nm9, _df9, _cf9, _em9, _sm9, _tm9, days, _st9,
                                   entry_type=_et9, rolling_entry=_roll9)
                        return _roll9, (_r9.get("trade_log", []) if _r9 else [])
                    except Exception:
                        return _roll9, []

                from concurrent.futures import ThreadPoolExecutor as _TPE9
                _trades_by_roll: dict = {0: [], 1: [], 2: []}
                _all_jobs9 = [
                    (item9, _rn9)
                    for item9 in _sym_strat9.items()
                    for _rn9 in (0, 1, 2)
                ]
                _n_jobs9 = len(_sym_strat9) * 3
                print(f"[⑨ Rolling比較] {len(_sym_strat9)}銘柄×戦略 × 3パターン = {_n_jobs9}件 バックテスト中…")
                with _TPE9(max_workers=workers) as _ex9:
                    _futs9 = {}
                    for (_sym_key9, _nm9), _rn9 in _all_jobs9:
                        _futs9[_ex9.submit(_run_one9, (tuple(_sym_key9), _nm9, _rn9))] = None
                    for _f9 in _futs9:
                        try:
                            _rn9_res, _tlog9 = _f9.result()
                            _trades_by_roll[_rn9_res].extend(_tlog9)
                        except Exception:
                            pass
                print(f"[⑨ Rolling比較] 完了: " + " / ".join(f"rolling={r}: {len(_trades_by_roll[r])}件" for r in (0,1,2)))
                _9c_cache[_roll_cache_key] = _trades_by_roll
                _9c_save()

            def _done9(tl): return [t for t in tl if t.get("reason") not in ("発注中", "保有中")]
            _s9 = {r: _st(_done9(_trades_by_roll[r])) for r in (0, 1, 2)}

            def _cmp_row9(label, s, highlight=False):
                bg   = "background:#0c1f3a;" if highlight else ""
                pcol = "#4ade80" if s["pnl"] >= 0 else "#f87171"
                wcol = "#4ade80" if s["wr"] >= 55 else ("#fbbf24" if s["wr"] >= 45 else "#f87171")
                pfc  = "#4ade80" if s["pf"] >= 1.5 else ("#fbbf24" if s["pf"] >= 1.0 else "#f87171")
                pfs  = "∞" if s["pf"] == float("inf") else f'{s["pf"]:.2f}'
                return (f'<tr style="{bg}">'
                        f'<td style="font-weight:700;color:#e2e8f0;padding:8px 12px">{label}</td>'
                        f'<td style="text-align:right;color:#94a3b8">{s["n"]}件</td>'
                        f'<td style="text-align:right;color:{wcol};font-weight:700">{s["wr"]:.1f}%</td>'
                        f'<td style="text-align:right;color:{pfc};font-weight:700">{pfs}</td>'
                        f'<td style="text-align:right;color:#4ade80">+{s["gw"]:,.0f}円</td>'
                        f'<td style="text-align:right;color:#f87171">-{s["gl"]:,.0f}円</td>'
                        f'<td style="text-align:right;color:{pcol};font-weight:700">{s["pnl"]:+,.0f}円</td>'
                        f'<td style="text-align:right;color:{pcol}">{s["avg"]:+,.0f}円</td>'
                        f'</tr>')

            _cur_hi = {0: _RE == 0, 1: _RE == 1, 2: _RE == 2}
            _row_r0 = _cmp_row9(f"rolling=0（翌日のみ・EXPIRE=1）{'★現状' if _RE==0 else ''}", _s9[0], highlight=_cur_hi[0])
            _row_r1 = _cmp_row9(f"rolling=1（最大1回更新）{'★現状' if _RE==1 else ''}",         _s9[1], highlight=_cur_hi[1])
            _row_r2 = _cmp_row9(f"rolling=2（最大2回更新）{'★現状' if _RE==2 else ''}",         _s9[2], highlight=_cur_hi[2])

            # 月別比較（3列）
            from collections import defaultdict as _dd9c
            _by_ym9 = {r: _dd9c(list) for r in (0, 1, 2)}
            for _r9m in (0, 1, 2):
                for _t9m in _done9(_trades_by_roll[_r9m]):
                    _sd9m = _t9m.get("signal_dt")
                    _ym9m = str(_sd9m)[:7] if _sd9m else ""
                    if _ym9m:
                        _by_ym9[_r9m][_ym9m].append(_t9m)
            _all_ym9 = sorted(set().union(*[set(_by_ym9[r].keys()) for r in (0,1,2)]), reverse=True)
            _cmp_month_rows9 = ""
            for _ym9 in _all_ym9:
                _sm9 = {r: _st(_by_ym9[r][_ym9]) for r in (0, 1, 2)}
                def _pc9(s): return "#4ade80" if s["pnl"] >= 0 else "#f87171"
                _cells9 = ""
                for _rr in (0, 1, 2):
                    _sep9 = ' style="border-left:1px solid #334155"' if _rr > 0 else ""
                    _cells9 += (
                        f'<td{_sep9}><span style="color:#94a3b8">{_sm9[_rr]["n"]}件</span></td>'
                        f'<td style="text-align:right;color:#94a3b8">{_sm9[_rr]["wr"]:.0f}%</td>'
                        f'<td style="text-align:right;color:{_pc9(_sm9[_rr])};font-weight:700">{_sm9[_rr]["pnl"]:+,.0f}円</td>'
                    )
                _cmp_month_rows9 += (
                    f'<tr><td style="font-weight:700;color:#e2e8f0;padding:5px 10px">{_ym9[:4]}/{_ym9[5:7]}月</td>'
                    + _cells9 + "</tr>"
                )

            _rolling_cmp_section = f"""
<h3 style="color:#7c3aed;margin:20px 0 6px">C. ローリング逆指値 効果比較（rolling=0 / 1 / 2 並列バックテスト）</h3>
<p class="footnote">同一銘柄×戦略リストに対してローリング回数 0/1/2 でバックテストを再実行。★現状 = 現在の設定。
取引の重複除外はしていないため件数はA/Bタブと異なります。</p>
<table style="width:auto;min-width:700px;margin-bottom:12px">
  <thead><tr>
    <th style="text-align:left">設定</th>
    <th>件数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">総利益</th><th style="color:#f87171">総損失</th>
    <th>損益合計</th><th>平均損益</th>
  </tr></thead>
  <tbody>{_row_r0}{_row_r1}{_row_r2}</tbody>
</table>
<details style="margin-bottom:16px">
  <summary style="cursor:pointer;color:#7c3aed;font-size:0.85rem;padding:4px 0">月別内訳（rolling 0/1/2）を表示</summary>
  <table style="width:auto;min-width:600px;margin-top:8px">
    <thead>
      <tr>
        <th style="text-align:left">月</th>
        <th colspan="3" style="color:#60a5fa;border-bottom:2px solid #60a5fa">rolling=0（翌日のみ）</th>
        <th colspan="3" style="color:#fbbf24;border-left:1px solid #334155;border-bottom:2px solid #fbbf24">rolling=1（1回更新）</th>
        <th colspan="3" style="color:#7c3aed;border-left:1px solid #334155;border-bottom:2px solid #7c3aed">rolling=2（2回更新）</th>
      </tr><tr>
        <th></th>
        <th>件数</th><th>勝率</th><th>損益</th>
        <th style="border-left:1px solid #334155">件数</th><th>勝率</th><th>損益</th>
        <th style="border-left:1px solid #334155">件数</th><th>勝率</th><th>損益</th>
      </tr>
    </thead>
    <tbody>{_cmp_month_rows9}</tbody>
  </table>
</details>"""

        # ── D: entry_atr_mult 比較（em=0.0/0.3/0.5/1.0 を並列バックテスト）──
        # C と同じく集計レポートのみ（3銘柄未満はスキップ）
        _em_cmp_section = ""
        if _rbt is not None and _fetch9 is not None and len(_sym_strat9) >= _MIN_SYMS_FOR_CMP:
            _EM_VALS = [0.0, 0.3, 0.5, 1.0]
            # C と同じ sym_strat セットを再利用
            _sym_strat_em: dict = _sym_strat9
            # BTスコアルックアップ: (symbol, strategy) → rec_score (from original done trades)
            _bt_score_lut: dict = {}
            for _tem in done:
                _kem = (_tem.get("symbol"), _tem.get("strategy"))
                if _kem[0] and _kem[1]:
                    _sc = _tem.get("rec_score")
                    if _sc is not None:
                        _bt_score_lut[_kem] = _sc

            def _get_params_em(strat_em):
                for _mem, _ete in [(_stop, "stop"), (_brk, "stop")]:
                    if hasattr(_mem, "STRATEGY_PARAMS") and strat_em in _mem.STRATEGY_PARAMS:
                        _cf_em, _em_em, _sm_em, _tm_em = _mem.STRATEGY_PARAMS[strat_em]
                        return _cf_em, _em_em, _sm_em, _tm_em, _ete
                for _mne in ["check_signals_short", "check_signals_short_breakout"]:
                    try:
                        import importlib as _ile
                        _mode = _ile.import_module(_mne)
                        if strat_em in _mode.STRATEGY_PARAMS:
                            _cf_em, _em_em, _sm_em, _tm_em = _mode.STRATEGY_PARAMS[strat_em]
                            return _cf_em, _em_em, _sm_em, _tm_em, "stop_sell"
                    except Exception:
                        pass
                return None

            # キャッシュキー: ("em", frozenset(sym_strat pairs))
            _em_cache_key = ("em", frozenset(_sym_strat_em.keys()))
            if _em_cache_key in _9c_cache:
                _trades_by_em = _9c_cache[_em_cache_key]
                print(f"[⑨ em比較] キャッシュ使用: " + " / ".join(f"em={e}: {len(_trades_by_em[e])}件" for e in _EM_VALS))
            else:
                def _run_one_em(args_em):
                    (_se, _ste), _nme, _em_val = args_em
                    _pe = _get_params_em(_ste)
                    if not _pe:
                        return _em_val, []
                    _cfe, _eme, _sme, _tme, _ete = _pe
                    _dfe = _fetch9(_se, days + 60)
                    if _dfe is None:
                        return _em_val, []
                    try:
                        _re = _rbt(_se, _nme, _dfe, _cfe, _em_val, _sme, _tme, days, _ste,
                                   entry_type=_ete)
                        _tlog = _re.get("trade_log", []) if _re else []
                        # symbol/strategy をトレードに付与（BT帯別ルックアップに必要）
                        return _em_val, [{**_t, "symbol": _se, "strategy": _ste} for _t in _tlog]
                    except Exception:
                        return _em_val, []

                from concurrent.futures import ThreadPoolExecutor as _TPEM
                _trades_by_em: dict = {em: [] for em in _EM_VALS}
                _all_jobs_em = [
                    (item_em, _em_v)
                    for item_em in _sym_strat_em.items()
                    for _em_v in _EM_VALS
                ]
                _n_jobs_em = len(_sym_strat_em) * len(_EM_VALS)
                print(f"[⑨ em比較] {len(_sym_strat_em)}銘柄×戦略 × {len(_EM_VALS)}パターン = {_n_jobs_em}件 バックテスト中…")
                with _TPEM(max_workers=workers) as _exe:
                    _futs_em = {}
                    for (_sym_em, _nm_em), _em_v in _all_jobs_em:
                        _futs_em[_exe.submit(_run_one_em, (tuple(_sym_em), _nm_em, _em_v))] = None
                    for _fe in _futs_em:
                        try:
                            _em_res, _tlog_em = _fe.result()
                            _trades_by_em[_em_res].extend(_tlog_em)
                        except Exception:
                            pass
                print(f"[⑨ em比較] 完了: " + " / ".join(f"em={e}: {len(_trades_by_em[e])}件" for e in _EM_VALS))
                _9c_cache[_em_cache_key] = _trades_by_em
                _9c_save()

            def _done_em(tl): return [t for t in tl if t.get("reason") not in ("発注中", "保有中")]
            _s_em = {e: _st(_done_em(_trades_by_em[e])) for e in _EM_VALS}

            def _cmp_row_em(label, s, highlight=False):
                bg   = "background:#0c1f3a;" if highlight else ""
                pcol = "#4ade80" if s["pnl"] >= 0 else "#f87171"
                wcol = "#4ade80" if s["wr"] >= 55 else ("#fbbf24" if s["wr"] >= 45 else "#f87171")
                pfc  = "#4ade80" if s["pf"] >= 1.5 else ("#fbbf24" if s["pf"] >= 1.0 else "#f87171")
                pfs  = "∞" if s["pf"] == float("inf") else f'{s["pf"]:.2f}'
                return (f'<tr style="{bg}">'
                        f'<td style="font-weight:700;color:#e2e8f0;padding:8px 12px">{label}</td>'
                        f'<td style="text-align:right;color:#94a3b8">{s["n"]}件</td>'
                        f'<td style="text-align:right;color:{wcol};font-weight:700">{s["wr"]:.1f}%</td>'
                        f'<td style="text-align:right;color:{pfc};font-weight:700">{pfs}</td>'
                        f'<td style="text-align:right;color:#4ade80">+{s["gw"]:,.0f}円</td>'
                        f'<td style="text-align:right;color:#f87171">-{s["gl"]:,.0f}円</td>'
                        f'<td style="text-align:right;color:{pcol};font-weight:700">{s["pnl"]:+,.0f}円</td>'
                        f'<td style="text-align:right;color:{pcol}">{s["avg"]:+,.0f}円</td>'
                        f'</tr>')

            # 現状のem（全戦略0.0で統一）を現状フラグに使う
            _cur_em = 0.0
            _em_labels = {0.0: "em=0.0（終値ちょうど・現状）", 0.3: "em=0.3（ATR×0.3上）", 0.5: "em=0.5（ATR×0.5上）", 1.0: "em=1.0（ATR×1.0上）"}
            _em_rows_html = ""
            for _ev in _EM_VALS:
                _lbl = _em_labels.get(_ev, f"em={_ev}")
                if _ev == _cur_em:
                    _lbl += " ★現状"
                _em_rows_html += _cmp_row_em(_lbl, _s_em[_ev], highlight=(_ev == _cur_em))

            # BT帯別比較（★★★≥80 / ★★60-79 / ★40-59 / △<40）
            _BT_BANDS = [
                (80, 101, "★★★ BT≥80", "#4ade80"),
                (60,  80, "★★  BT60-79", "#60a5fa"),
                (40,  60, "★   BT40-59", "#fbbf24"),
                ( 0,  40, "△   BT<40",   "#f87171"),
            ]
            # BTスコアをトレードに付与（(symbol,strategy)→rec_score のルックアップ）
            def _tag_bt(tl):
                out = []
                for _t in tl:
                    _k = (_t.get("symbol"), _t.get("strategy"))
                    _sc = _bt_score_lut.get(_k)
                    out.append(dict(_t, _bt=_sc))
                return out

            _em_bt_rows = ""
            for _blo, _bhi, _blbl, _bcol in _BT_BANDS:
                _em_bt_rows += (
                    f'<tr><td colspan="9" style="padding:4px 8px;font-weight:700;'
                    f'color:{_bcol};border-top:1px solid #334155">{_blbl}</td></tr>'
                )
                for _ev in _EM_VALS:
                    _tagged = _tag_bt(_done_em(_trades_by_em[_ev]))
                    _band_t = [t for t in _tagged if t.get("_bt") is not None and _blo <= t["_bt"] < _bhi]
                    _bs = _st(_band_t)
                    _lbl = _em_labels.get(_ev, f"em={_ev}")
                    if _ev == _cur_em:
                        _lbl += " ★"
                    _em_bt_rows += _cmp_row_em(f"  {_lbl}", _bs, highlight=(_ev == _cur_em))

            # 月別比較（BT帯フィルター付き）
            from collections import defaultdict as _dd_em
            _em_col_colors = {0.0: "#60a5fa", 0.3: "#34d399", 0.5: "#fbbf24", 1.0: "#f87171"}

            # BT帯ごとの月別テーブルを生成
            _ALL_BANDS_FOR_MONTH = [
                (0, 101, "全体", "#e2e8f0"),
            ] + [(lo, hi, lbl, col) for lo, hi, lbl, col in _BT_BANDS]

            def _make_month_table(blo9, bhi9):
                _by_ym = {e: _dd_em(list) for e in _EM_VALS}
                for _eve in _EM_VALS:
                    for _tem2 in _tag_bt(_done_em(_trades_by_em[_eve])):
                        if _tem2.get("_bt") is None and (blo9 > 0 or bhi9 < 101):
                            continue  # BTスコアなしは全体以外から除外
                        if not (blo9 <= (_tem2.get("_bt") or 0) < bhi9) and not (blo9 == 0 and bhi9 == 101):
                            continue
                        _sd = _tem2.get("signal_dt")
                        _ym = str(_sd)[:7] if _sd else ""
                        if _ym:
                            _by_ym[_eve][_ym].append(_tem2)
                _all_ym = sorted(set().union(*[set(_by_ym[e].keys()) for e in _EM_VALS]), reverse=True)
                rows = ""
                for _ymv in _all_ym:
                    _sm_m = {e: _st(_by_ym[e][_ymv]) for e in _EM_VALS}
                    _cells = ""
                    for _i, _evv in enumerate(_EM_VALS):
                        _sep = ' style="border-left:1px solid #334155"' if _i > 0 else ""
                        _pce = "#4ade80" if _sm_m[_evv]["pnl"] >= 0 else "#f87171"
                        _cells += (
                            f'<td{_sep}><span style="color:#94a3b8">{_sm_m[_evv]["n"]}件</span></td>'
                            f'<td style="text-align:right;color:#94a3b8">{_sm_m[_evv]["wr"]:.0f}%</td>'
                            f'<td style="text-align:right;color:{_pce};font-weight:700">{_sm_m[_evv]["pnl"]:+,.0f}円</td>'
                        )
                    rows += (f'<tr><td style="font-weight:700;color:#e2e8f0;padding:5px 10px">'
                             f'{_ymv[:4]}/{_ymv[5:7]}月</td>' + _cells + "</tr>")
                return rows

            _em_th_cols = ""
            for _i_e, _ev3 in enumerate(_EM_VALS):
                _sep3 = 'border-left:1px solid #334155;' if _i_e > 0 else ''
                _c3 = _em_col_colors[_ev3]
                _em_th_cols += f'<th colspan="3" style="{_sep3}color:{_c3};border-bottom:2px solid {_c3}">em={_ev3}</th>'
            _em_th_sub = ""
            for _i_e2, _ev4 in enumerate(_EM_VALS):
                _sep4 = ' style="border-left:1px solid #334155"' if _i_e2 > 0 else ""
                _em_th_sub += f'<th{_sep4}>件数</th><th>勝率</th><th>損益</th>'

            _thead_html = f"""<thead>
      <tr><th style="text-align:left">月</th>{_em_th_cols}</tr>
      <tr><th></th>{_em_th_sub}</tr>
    </thead>"""

            # 各BT帯のテーブルHTMLを生成
            _uid_em = abs(id(done)) % 999999  # ユニークID（複数タブ衝突回避）
            _month_tabs_btns = ""
            _month_tabs_panes = ""
            for _bi, (_blo9, _bhi9, _blbl9, _bcol9) in enumerate(_ALL_BANDS_FOR_MONTH):
                _tid9 = f"em9month_{_uid_em}_{_bi}"
                _active_btn = "font-weight:700;border-bottom:2px solid" if _bi == 0 else "border-bottom:2px solid transparent"
                _month_tabs_btns += (
                    f'<button onclick="switchEm9_{_uid_em}(this,\'{_tid9}\')" '
                    f'style="background:none;border:none;cursor:pointer;padding:4px 10px;'
                    f'color:{_bcol9};font-size:0.82rem;{_active_btn} {_bcol9}">'
                    f'{_blbl9}</button>'
                )
                _rows9 = _make_month_table(_blo9, _bhi9)
                _display9 = "block" if _bi == 0 else "none"
                _month_tabs_panes += (
                    f'<div id="{_tid9}" style="display:{_display9}">'
                    f'<table style="width:auto;min-width:700px;margin-top:4px">'
                    f'{_thead_html}<tbody>{_rows9}</tbody></table></div>'
                )

            _em_cmp_section = f"""
<h3 style="color:#f97316;margin:20px 0 6px">D. エントリー閾値（entry_atr_mult）比較（em=0.0 / 0.3 / 0.5 / 1.0 並列バックテスト）</h3>
<p class="footnote">em=0.0 は終値ちょうどで逆指値（翌日少しでも上がれば約定）。em=0.5 なら前日ATR×0.5 上を超えないと約定しない（ブレイクアウト確認）。
sm/tm は各戦略の既存値を使用。★現状 = 現在の全戦略共通設定（em=0.0）。⑨キャッシュ有効（同日2回目以降はスキップ）。</p>
<table style="width:auto;min-width:700px;margin-bottom:12px">
  <thead><tr>
    <th style="text-align:left">設定</th>
    <th>件数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">総利益</th><th style="color:#f87171">総損失</th>
    <th>損益合計</th><th>平均損益</th>
  </tr></thead>
  <tbody>{_em_rows_html}</tbody>
</table>
<details style="margin-bottom:16px">
  <summary style="cursor:pointer;color:#f97316;font-size:0.85rem;padding:4px 0">BT帯別内訳（★★★/★★/★/△ × em値）を表示</summary>
  <p style="font-size:0.8rem;color:#94a3b8;margin:6px 0">BTスコアはシグナル元の銘柄×戦略のスコアをルックアップ。re-runトレード数とBTスコアなし件数は除外されることがあります。</p>
  <table style="width:auto;min-width:700px;margin-top:4px">
    <thead><tr>
      <th style="text-align:left">BT帯 / em設定</th>
      <th>件数</th><th>勝率</th><th>PF</th>
      <th style="color:#4ade80">総利益</th><th style="color:#f87171">総損失</th>
      <th>損益合計</th><th>平均損益</th>
    </tr></thead>
    <tbody>{_em_bt_rows}</tbody>
  </table>
</details>
<details style="margin-bottom:16px">
  <summary style="cursor:pointer;color:#f97316;font-size:0.85rem;padding:4px 0">月別内訳（em=0.0/0.3/0.5/1.0）を表示</summary>
  <div style="margin:6px 0 4px;border-bottom:1px solid #334155;padding-bottom:4px">{_month_tabs_btns}</div>
  <script>
  function switchEm9_{_uid_em}(btn, tid) {{
    var parent = btn.parentNode.parentNode;
    var panes = parent.querySelectorAll('div[id]');
    panes.forEach(function(p) {{ p.style.display = 'none'; }});
    document.getElementById(tid).style.display = 'block';
    var btns = btn.parentNode.querySelectorAll('button');
    btns.forEach(function(b) {{ b.style.borderBottom = '2px solid transparent'; b.style.fontWeight = 'normal'; }});
    btn.style.borderBottom = '2px solid currentColor'; btn.style.fontWeight = '700';
  }}
  </script>
  {_month_tabs_panes}
</details>"""

        _rolling_badge = (
            f'<span style="background:#7c3aed;color:#fff;border-radius:4px;padding:2px 8px;font-size:0.78rem;margin-left:8px">ローリング有効（最大{_RE}回更新）</span>'
            if _RE > 0 else
            '<span style="background:#334155;color:#94a3b8;border-radius:4px;padding:2px 8px;font-size:0.78rem;margin-left:8px">ローリング無効（--rolling N で有効化）</span>'
        )
        _expire_note = (
            f"現在 ENTRY_EXPIRE={_EE}、ROLLING_ENTRY={_RE}。"
            + (" ローリング有効時は days_to_fill≥1 の取引も毎日終値で価格を更新して入っています。" if _RE > 0 else
               " ローリング無効時は days_to_fill=0（翌日一発で入った取引）のみが EXPIRE=1 相当です。")
        )
        return f"""<h2>⑨ エントリータイミング比較 {_rolling_badge}</h2>
<div style="background:#1a2744;border-radius:6px;padding:12px 16px;margin-bottom:16px;font-size:0.82rem;color:#94a3b8;line-height:1.7">
  <b style="color:#e2e8f0">2つの独立した軸で比較します：</b><br>
  <b style="color:#60a5fa">A. 注文保持期間（ENTRY_EXPIRE）</b>：注文を出した後、何日間有効にするか。
  翌日に約定しなければ保持するか即キャンセルするかの比較。{_expire_note}<br>
  <b style="color:#fbbf24">B. エントリー遅延（Delay）</b>：シグナルが出てから何日後に注文を出すか。
  翌日すぐ出すか、1〜2日見送ってから出すかの比較。
  ローリング有効時は比較に意味があります（翌日見送った場合の成績 = 2日目以降の取引品質）。
</div>

<h3 style="color:#60a5fa;margin:0 0 6px">A. 注文保持期間（ENTRY_EXPIRE）</h3>
<p class="footnote">days_to_fill=0が翌日約定。EXPIRE=1は翌日に約定しなければキャンセル、EXPIRE=3は現行設定（3日間有効）。</p>
<table style="width:auto;min-width:650px;margin-bottom:12px">
  <thead><tr>{expire_th}{common_th_tail}</tr></thead>
  <tbody>{expire_rows}</tbody>
</table>
<details style="margin-bottom:20px">
  <summary style="cursor:pointer;color:#60a5fa;font-size:0.85rem;padding:4px 0">月別内訳（注文保持期間）を表示</summary>
  <table style="width:auto;min-width:560px;margin-top:8px">
    <thead>
      <tr>
        <th style="text-align:left">月</th>
        <th colspan="3" style="color:#60a5fa;border-bottom:2px solid #60a5fa">EXPIRE=1（翌日のみ）</th>
        <th colspan="3" style="color:#fbbf24;border-left:1px solid #334155;border-bottom:2px solid #fbbf24">EXPIRE=2（2日間）</th>
        <th colspan="3" style="color:#e2e8f0;border-left:1px solid #334155;border-bottom:2px solid #475569">EXPIRE=3（現状）</th>
      </tr><tr>
        <th></th>
        <th>件数</th><th>勝率</th><th>損益</th>
        <th style="border-left:1px solid #334155">件数</th><th>勝率</th><th>損益</th>
        <th style="border-left:1px solid #334155">件数</th><th>勝率</th><th>損益</th>
      </tr>
    </thead>
    <tbody>{expire_month_rows}</tbody>
  </table>
</details>

<h3 style="color:#fbbf24;margin:0 0 6px">B. エントリー遅延（Delay）</h3>
<p class="footnote">delay=0が現状（翌日に注文）。delay=1は「シグナル翌日を見送り2日後に注文」相当、delay=2は「3日後から注文」相当。件数は累積でなく各delay時点で初めて注文を出した場合に含まれる取引。</p>
<table style="width:auto;min-width:650px;margin-bottom:12px">
  <thead><tr>{delay_th}{common_th_tail}</tr></thead>
  <tbody>{delay_rows}</tbody>
</table>
<details style="margin-bottom:16px">
  <summary style="cursor:pointer;color:#fbbf24;font-size:0.85rem;padding:4px 0">月別内訳（エントリー遅延）を表示</summary>
  <table style="width:auto;min-width:560px;margin-top:8px">
    <thead>
      <tr>
        <th style="text-align:left">月</th>
        <th colspan="3" style="color:#60a5fa;border-bottom:2px solid #60a5fa">delay=0（翌日入り・現状）</th>
        <th colspan="3" style="color:#fbbf24;border-left:1px solid #334155;border-bottom:2px solid #fbbf24">delay=1（2日後から）</th>
        <th colspan="3" style="color:#e2e8f0;border-left:1px solid #334155;border-bottom:2px solid #475569">delay=2（3日後から）</th>
      </tr><tr>
        <th></th>
        <th>件数</th><th>勝率</th><th>損益</th>
        <th style="border-left:1px solid #334155">件数</th><th>勝率</th><th>損益</th>
        <th style="border-left:1px solid #334155">件数</th><th>勝率</th><th>損益</th>
      </tr>
    </thead>
    <tbody>{delay_month_rows}</tbody>
  </table>
</details>
{_rolling_cmp_section}
{_em_cmp_section}"""

    _timing_html = "" if skip_timing9 else _entry_timing_cmp_html(kpi_trades)

    # ── シグナル時点BTスコア別成績セクション（常に生成）──────
    _preoos_section_html = ""
    if True:
        _poo_cutoff_label = "シグナル発生月時点（月次）"
        _poo_buckets = [
            (80, 101, "★★★≥80", "#4ade80"),
            (60,  80, "★★60-79", "#86efac"),
            (40,  60, "★40-59",  "#fbbf24"),
            ( 0,  40, "△<40",    "#f87171"),
        ]
        _poo_rows = ""
        for _plo, _phi, _plbl, _pcol in _poo_buckets:
            _ptr = [t for t in full_year_trades
                    if t.get("score") is not None and _plo <= t["score"] < _phi]
            _pn = len(_ptr)
            if not _pn:
                continue
            _pw  = sum(1 for t in _ptr if t["pnl"] > 0)
            _ppnl = sum(t["pnl"] for t in _ptr)
            _pgp  = sum(t["pnl"] for t in _ptr if t["pnl"] > 0)
            _pgl  = abs(sum(t["pnl"] for t in _ptr if t["pnl"] < 0))
            _ppf  = _pgp / _pgl if _pgl > 0 else (float("inf") if _pgp > 0 else 0.0)
            _ppf_s = "∞" if _ppf == float("inf") else f"{_ppf:.2f}"
            _pwr  = _pw / _pn * 100
            _pppc = "profit" if _ppnl >= 0 else "loss"
            _pavg = _ppnl / _pn
            _papc = "profit" if _pavg >= 0 else "loss"
            _poo_rows += f"""<tr>
  <td style="color:{_pcol};font-weight:700;text-align:left;border-left:3px solid {_pcol};padding-left:8px">{_plbl}</td>
  <td style="font-weight:700">{_pn}</td>
  <td style="font-weight:700;color:{'#4ade80' if _pwr>=55 else ('#fbbf24' if _pwr>=45 else '#f87171')}">{_pwr:.1f}%</td>
  <td style="font-weight:700">{_ppf_s}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{_pgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{_pgl:,.0f}円</td>
  <td class="{_pppc}" style="text-align:right;font-weight:700">{_ppnl:+,.0f}円</td>
  <td class="{_papc}" style="text-align:right;font-weight:700">{_pavg:+,.0f}円</td>
</tr>"""
        # 銘柄別: シグナル時点BTスコア≥60の銘柄
        _poo60_trades = [t for t in full_year_trades if (t.get("score") or 0) >= 60]
        _poo_sym_agg: dict = {}
        for _pt in _poo60_trades:
            _pk = (_pt["symbol"], _pt.get("name", ""))
            if _pk not in _poo_sym_agg:
                _poo_sym_agg[_pk] = {"n":0,"w":0,"pnl":0,"gp":0,"gl":0,"strats":set(),
                                      "sig_time_scores":[],"rec_scores":[]}
            _pd = _poo_sym_agg[_pk]
            _pd["n"]   += 1
            _pd["w"]   += 1 if _pt["pnl"] > 0 else 0
            _pd["pnl"] += _pt["pnl"]
            _pd["gp"]  += _pt["pnl"] if _pt["pnl"] > 0 else 0
            _pd["gl"]  += abs(_pt["pnl"]) if _pt["pnl"] < 0 else 0
            _pd["strats"].add(_pt.get("strategy", ""))
            if _pt.get("score") is not None:
                _pd["sig_time_scores"].append(_pt["score"])
            if _pt.get("rec_score") is not None:
                _pd["rec_scores"].append(_pt["rec_score"])
        _poo_sym_rows = ""
        for (_psym, _pname), _pd in sorted(_poo_sym_agg.items(),
                                           key=lambda x: x[1]["pnl"], reverse=True):
            _pn2 = _pd["n"]; _pw2 = _pd["w"]
            _ppnl2 = _pd["pnl"]; _pgp2 = _pd["gp"]; _pgl2 = _pd["gl"]
            _ppf2  = _pgp2 / _pgl2 if _pgl2 > 0 else (float("inf") if _pgp2 > 0 else 0.0)
            _ppf2s = "∞" if _ppf2 == float("inf") else f"{_ppf2:.2f}"
            _pwr2  = _pw2 / _pn2 * 100 if _pn2 else 0
            _pspc  = "profit" if _ppnl2 >= 0 else "loss"
            _avg_poo = round(sum(_pd["sig_time_scores"]) / len(_pd["sig_time_scores"])) if _pd["sig_time_scores"] else None
            _avg_bt2 = round(sum(_pd["rec_scores"]) / len(_pd["rec_scores"])) if _pd["rec_scores"] else None
            _poo_disp  = (f'<span style="color:{"#4ade80" if _avg_poo and _avg_poo>=60 else ("#fbbf24" if _avg_poo and _avg_poo>=40 else "#f87171")};font-weight:700">{_avg_poo}</span>'
                          if _avg_poo is not None else "—")
            _bt2_disp  = (f'<span style="color:{"#4ade80" if _avg_bt2 and _avg_bt2>=60 else ("#fbbf24" if _avg_bt2 and _avg_bt2>=40 else "#f87171")};font-weight:700">{_avg_bt2}</span>'
                          if _avg_bt2 is not None else "—")
            _stag = " ".join(f'<span class="tag tag-{_s.lower()}" style="font-size:0.7rem">{_s}</span>'
                             for _s in sorted(_pd["strats"]))
            _prow_style = (' style="background:#1a0a0a;border-left:3px solid #f87171"' if _ppnl2 < -30000
                           else (' style="background:#0a1a0a;border-left:3px solid #4ade80"' if _ppnl2 > 50000
                                 else ""))
            _poo_sym_rows += f"""<tr{_prow_style}>
  <td class="sym" style="text-align:left">{_psym}<br><span style="color:#64748b;font-size:0.75rem">{_pname}</span></td>
  <td style="text-align:center">{_stag}</td>
  <td style="text-align:center">{_poo_disp}</td>
  <td style="text-align:center">{_bt2_disp}</td>
  <td style="font-weight:700">{_pn2}</td>
  <td style="font-weight:700">{_pwr2:.1f}%</td>
  <td style="font-weight:700">{_ppf2s}</td>
  <td class="profit" style="text-align:right">+{_pgp2:,.0f}円</td>
  <td class="loss"   style="text-align:right">-{_pgl2:,.0f}円</td>
  <td class="{_pspc}" style="text-align:right;font-weight:700">{_ppnl2:+,.0f}円</td>
</tr>"""
        if not _poo_sym_rows:
            _poo_sym_rows = '<tr><td colspan="10" style="text-align:center;color:#64748b;padding:12px">シグナル時点BT≥60の取引なし</td></tr>'
        if not _poo_rows:
            _poo_rows = '<tr><td colspan="8" style="text-align:center;color:#64748b;padding:12px">データなし</td></tr>'
        _preoos_section_html = f"""
<h2>⑩ シグナル時点BTスコア別成績
  <span style="color:#94a3b8;font-size:0.8rem;font-weight:400">
    {_poo_cutoff_label}
  </span>
</h2>
<p class="footnote" style="margin-bottom:8px">
  「シグナル時点BTスコア」= シグナル発生月の月初時点（その月より前の完了済み取引のみ）でBTスコアを計算。
  今日のスコアではなく、シグナルが出た時点での評価スコアです（月次バケット化）。
  メインのBTスコア（② 欄に表示）は変更されません。<br>
  ★★★≥80が最も信頼性高。このスコアが高い銘柄のOOS成績を確認してください。
</p>
<table>
  <thead><tr>
    <th style="text-align:left">シグナル時点BTスコア帯</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{_poo_rows}</tbody>
</table>

<h2 style="margin-top:20px">シグナル時点BT≥60 銘柄別成績</h2>
<p class="footnote" style="margin-bottom:8px">
  シグナル時点BTスコア≥60の銘柄ごとの損益集計。
  <strong>シグナル時点BT</strong> = シグナル発生月時点のスコア / <strong>BT(現行)</strong> = 今日のスコア（参考）。
  <span style="color:#f87171">■</span> = 損失-3万超、<span style="color:#4ade80">■</span> = 利益+5万超。
</p>
<table>
  <thead><tr>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th style="color:#10b981">シグナル時点BT</th>
    <th style="color:#fbbf24">BT(現行)</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th>
  </tr></thead>
  <tbody>{_poo_sym_rows}</tbody>
</table>"""

    _DETAIL_TAB_SEQ += 1
    _dseq = _DETAIL_TAB_SEQ

    _preoos_tab_btn = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'preoos\')">⑩ シグナル時点BTスコア</button>'
    _maxhold_tab_btn = (
        f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'maxhold\')">⑪ 保有日数比較</button>\n'
        f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'maxhold_cmp\')">⑫ con/agg比較</button>'
    )

    return f"""
<h2>直近{days}日 取引損益 <span style="font-size:0.8rem;color:#64748b;font-weight:400">（{since} 〜 {until}）</span></h2>
{kpi_html}

<button class="analysis-toggle" onclick="toggleAnalysis({_dseq})" id="analysis_btn_{_dseq}">▶ 詳細分析（スクリプト別・スコア別・銘柄別）を表示</button>
<div id="analysis_{_dseq}" class="analysis-block" style="display:none">

<div class="analysis-tab-nav">
  <button class="analysis-tab-btn active" onclick="switchAnalysisTab({_dseq},'summary')">スクリプト別</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'score')">① ② スコア別実績</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'cross')">③ ④ BT×WF・高BT銘柄</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'bt6069')">⑤ BT60-69</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'speed')">⑥ 速度分析</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'extra')">⑦ ⑧ 損切り・追加</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'timing')">⑨ 翌日のみ比較</button>
{_preoos_tab_btn}
{_maxhold_tab_btn}
</div>

<div id="analtab_{_dseq}_summary" class="analysis-tab-pane active">
<h2>スクリプト別サマリー</h2>
<p style="font-size:0.75rem;color:#64748b;margin-bottom:10px">
  ※ 含み損保有 = 損失で終わったトレードの平均保有日数（最終PnL &lt; 0 の取引のみ対象）
</p>
<div style="margin-bottom:10px">
  <button onclick="switchSumFilter({_dseq},'all')"  id="sumf_{_dseq}_all"  style="background:#3b82f6;color:#fff;border:none;padding:4px 14px;border-radius:4px;cursor:pointer;margin-right:6px;font-size:0.82rem">全件</button>
  <button onclick="switchSumFilter({_dseq},'bt70')" id="sumf_{_dseq}_bt70" style="background:#1e293b;color:#94a3b8;border:1px solid #334155;padding:4px 14px;border-radius:4px;cursor:pointer;font-size:0.82rem">BT70以上</button>
</div>
<div id="sumt_{_dseq}_all">
<table>
  <thead><tr>
    <th style="text-align:left">スクリプト</th>
    <th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th>
    <th>平均保有</th>
    <th style="color:#f87171">含み損保有</th>
    <th style="color:#fbbf24">平均遅延</th>
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
  <td style="text-align:right;color:#94a3b8">{"—" if not cfg_n_all else f"{cfg_avg_hold:.1f}日"}</td>
  <td style="text-align:right;color:#94a3b8">{"—" if not _all_cfg_win else f"{cfg_avg_neg_days:.1f}日"}</td>
  <td style="text-align:right;color:#94a3b8">{"—" if not cfg_n_all else f"{cfg_avg_delay:.1f}日"}</td>
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
  <td style="text-align:right;font-weight:700">{"—" if not settled_trades else f"{dedup_avg_hold:.1f}日"}</td>
  <td style="text-align:right;font-weight:700;color:#f87171">{"—" if not _dedup_win else f"{dedup_avg_neg_days:.1f}日"}</td>
  <td style="text-align:right;font-weight:700;color:#fbbf24">{"—" if not settled_trades else f"{dedup_avg_delay:.1f}日"}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{dedup_gp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{dedup_gl:,.0f}円</td>
  <td class="{"profit" if pnl_sum >= 0 else "loss"}" style="text-align:right;font-weight:700">{pnl_sum:+,.0f}円</td>
</tr>
</tbody>
</table>
</div>
<div id="sumt_{_dseq}_bt70" style="display:none">
<table>
  <thead><tr>
    <th style="text-align:left">スクリプト</th>
    <th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th>
    <th>平均保有</th>
    <th style="color:#f87171">含み損保有</th>
    <th style="color:#fbbf24">平均遅延</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th>
  </tr></thead>
  <tbody>{sum_rows_bt70}
<tr style="border-top:1px dashed #475569;background:#1a2435">
  <td style="text-align:left;color:#94a3b8;font-size:0.82rem;font-style:italic">設定別合計（重複あり・BT70以上）</td>
  <td style="color:#94a3b8">{bt70_n}</td>
  <td style="color:#94a3b8">{bt70_win}</td>
  <td style="color:#94a3b8">{"—" if not bt70_n else f"{bt70_win/bt70_n*100:.1f}%"}</td>
  <td style="color:#94a3b8">{bt70_pf_s}</td>
  <td style="text-align:right;color:#94a3b8">{"—" if not bt70_n else f"{bt70_avg_hold:.1f}日"}</td>
  <td style="text-align:right;color:#94a3b8">{"—" if not _bt70_cfg_win else f"{bt70_avg_neg_days:.1f}日"}</td>
  <td style="text-align:right;color:#94a3b8">{"—" if not bt70_n else f"{bt70_avg_delay:.1f}日"}</td>
  <td class="profit" style="text-align:right;color:#94a3b8">+{bt70_gp:,.0f}円</td>
  <td class="loss"   style="text-align:right;color:#94a3b8">-{bt70_gl:,.0f}円</td>
  <td class="{bt70_lpc}" style="text-align:right;color:#94a3b8">{bt70_pnl:+,.0f}円</td>
</tr>
<tr style="border-top:2px solid #3b82f6;background:#0d1424">
  <td style="text-align:left;color:#60a5fa;font-weight:700">▶ 合計（重複除外・BT70以上）</td>
  <td style="color:#60a5fa;font-weight:700">{bt70d_n}</td>
  <td style="color:#60a5fa;font-weight:700">{bt70d_win}</td>
  <td style="color:#60a5fa;font-weight:700">{"—" if not bt70d_n else f"{bt70d_win/bt70d_n*100:.1f}%"}</td>
  <td style="color:#60a5fa;font-weight:700">{bt70d_pf_s}</td>
  <td style="text-align:right;font-weight:700">{"—" if not _bt70_ded else f"{bt70d_avg_hold:.1f}日"}</td>
  <td style="text-align:right;font-weight:700;color:#f87171">{"—" if not _bt70_ded_win else f"{bt70d_avg_neg_days:.1f}日"}</td>
  <td style="text-align:right;font-weight:700;color:#fbbf24">{"—" if not _bt70_ded else f"{bt70d_avg_delay:.1f}日"}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{bt70d_gp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{bt70d_gl:,.0f}円</td>
  <td class="{bt70d_lpc}" style="text-align:right;font-weight:700">{bt70d_pnl:+,.0f}円</td>
</tr>
</tbody>
</table>
</div>
</div>

<div id="analtab_{_dseq}_score" class="analysis-tab-pane">
<h2>スコア別実績（直近{days}日 / {period_note}）</h2>
<h3 style="color:#60a5fa;font-size:0.95rem;margin:8px 0 4px">① WFスコア軸 <span style="color:#94a3b8;font-size:0.8rem;font-weight:400">銘柄選定基準で分類</span></h3>
<table>
  <thead><tr>
    <th style="text-align:left">スコア帯 (WF)</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#60a5fa">平均WF</th>
    <th style="color:#fbbf24">平均BT</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{fine_rows}</tbody>
</table>
<h3 style="color:#fbbf24;font-size:0.95rem;margin:20px 0 4px">② BTスコア軸 <span style="color:#94a3b8;font-size:0.8rem;font-weight:400">直近機能度で分類</span></h3>
<table>
  <thead><tr>
    <th style="text-align:left">スコア帯 (BT)</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#60a5fa">平均WF</th>
    <th style="color:#fbbf24">平均BT</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{bt_fine_rows}</tbody>
</table>
<p class="footnote">WF=ウォークフォワードスコア（銘柄選定基準・高いほど過去フォールドで安定）／ BT=直近バックテストスコア（最近の機能度・低いと最近機能していない）</p>
</div>

<div id="analtab_{_dseq}_cross" class="analysis-tab-pane">
<h2>③ BT×WF クロス分析（BT≥60内でWFスコア帯別 / 365日全取引）</h2>
<p class="footnote" style="margin-bottom:8px">BT≥60の銘柄についてWFスコア帯ごとに分割。WFスコアがBT≥60の中で追加の識別力を持つか確認。</p>
<table>
  <thead><tr>
    <th style="text-align:left">WFスコア帯</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{"<tr><td colspan='8' style='text-align:center;color:#64748b;padding:12px'>BT≥60の取引なし</td></tr>" if not cross_rows else cross_rows}</tbody>
</table>

<h2 style="margin-top:24px">④ 高BT銘柄別成績（BT≥60 / 損益降順 / 365日全取引）</h2>
<p class="footnote" style="margin-bottom:8px">
  BTスコア≥60の銘柄ごとの損益集計。<span style="color:#f87171">■</span> = 損失-3万超、<span style="color:#4ade80">■</span> = 利益+5万超。<br>
  BT・WFは各取引のスコアの平均値。損失が出ている高BT銘柄を特定し、スキップ候補の検討に使う。
</p>
<table>
  <thead><tr>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th>BT</th>
    <th>WF</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th>
  </tr></thead>
  <tbody>{sym_rows}</tbody>
</table>
</div>

<div id="analtab_{_dseq}_bt6069" class="analysis-tab-pane">
<h2>⑤ BT60-69 × WFクロス分析（conservative限定）</h2>
<p class="footnote" style="margin-bottom:8px">
  BT60代はconservativeのみで運用するとプラス。さらにWFスコアで絞り込めるか検証。<br>
  <span style="background:#4ade80;color:#0f172a;font-size:0.65rem;font-weight:700;padding:1px 6px;border-radius:3px">投資対象</span>
  = そのWF帯はプラス圏 &nbsp;
  <span style="background:#f87171;color:#0f172a;font-size:0.65rem;font-weight:700;padding:1px 6px;border-radius:3px">スキップ</span>
  = そのWF帯はマイナス圏
</p>
<table>
  <thead><tr>
    <th style="text-align:left">WFスコア帯</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{wf6069_rows}</tbody>
</table>

<h2 style="margin-top:20px">⑤ BT60-69 銘柄別成績（conservative限定 / 損益降順）</h2>
<p class="footnote" style="margin-bottom:8px">
  BT60-69帯はconservativeのみで運用するとプラスになるが、その内訳を銘柄別に表示。<br>
  <span style="color:#f87171">■</span>スキップ候補（損失-1万超）はWATCHLISTから除外検討。<span style="color:#4ade80">■</span>優先（利益+3万超）は積極的に取る。
</p>
<table>
  <thead><tr>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th style="color:#93c5fd">BT</th>
    <th style="color:#60a5fa">WF</th>
    <th>取引数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80">利益</th>
    <th style="color:#f87171">損失</th>
    <th>損益合計</th>
  </tr></thead>
  <tbody>{sym6069_rows}</tbody>
</table>
</div>

<div id="analtab_{_dseq}_speed" class="analysis-tab-pane">
{_speed_html}
</div>

<div id="analtab_{_dseq}_extra" class="analysis-tab-pane">
{_stop_pattern_html_str}
{_overlap_html}
</div>

<div id="analtab_{_dseq}_timing" class="analysis-tab-pane">
{_timing_html}
</div>

<div id="analtab_{_dseq}_preoos" class="analysis-tab-pane">
{_preoos_section_html if _preoos_section_html else '<p style="color:#64748b;padding:20px">preoos_cutoff_days 未指定のため表示なし</p>'}
</div>

<div id="analtab_{_dseq}_maxhold" class="analysis-tab-pane">
<!-- MAXHOLD_SLOT -->
</div>

<div id="analtab_{_dseq}_maxhold_cmp" class="analysis-tab-pane">
<!-- MAXHOLD_CMP_SLOT -->
</div>

</div>

{_trend_breakdown_html}

<h2>取引明細</h2>
<div class="detail-tab-nav">
  <button class="detail-tab-btn active" onclick="switchDetailTab({_dseq},'all')">全部（決済日順） <span style="font-size:0.72rem;color:#94a3b8">({len(sorted_trades)})</span></button>
  <button class="detail-tab-btn" onclick="switchDetailTab({_dseq},'bt70')">BT70以上 <span style="font-size:0.72rem;color:#94a3b8">({len(bt70_trades)})</span></button>
  <button class="detail-tab-btn" onclick="switchDetailTab({_dseq},'entry')">エントリー日別 <span style="font-size:0.72rem;color:#94a3b8">(直近{_ENTRY_GRID_DAYS}日)</span></button>
  <button class="detail-tab-btn" onclick="switchDetailTab({_dseq},'bt70entry')">BT70×エントリー日別 <span style="font-size:0.72rem;color:#94a3b8">(直近{_ENTRY_GRID_DAYS}日)</span></button>
</div>
<div id="detail_{_dseq}_all" class="detail-tab-pane active">
<table>
  <thead><tr>
    <th>決済日</th>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th>設定</th>
    <th>約定値</th><th style="color:#f87171">損切り</th><th style="color:#4ade80">目標</th><th>現在値</th><th>決済値</th><th>株数</th><th>保有</th><th>遅延</th>
    <th>損益</th><th>理由</th><th>エントリー</th>
  </tr></thead>
  <tbody>{trade_rows_all}</tbody>
</table>
</div>
<div id="detail_{_dseq}_bt70" class="detail-tab-pane">
<table>
  <thead><tr>
    <th>決済日</th>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th>設定</th>
    <th>約定値</th><th style="color:#f87171">損切り</th><th style="color:#4ade80">目標</th><th>現在値</th><th>決済値</th><th>株数</th><th>保有</th><th>遅延</th>
    <th>損益</th><th>理由</th><th>エントリー</th>
  </tr></thead>
  <tbody>{trade_rows_bt70}</tbody>
</table>
</div>
<div id="detail_{_dseq}_entry" class="detail-tab-pane">
<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:10px">日付をクリックで詳細表示（直近{_ENTRY_GRID_DAYS}日）</p>
{_month_summary_html(entry_sorted_trades)}
{_month_accordion_html(_entry_by_date, _sorted_entry_dates, _dseq, "e")}
</div>
<div id="detail_{_dseq}_bt70entry" class="detail-tab-pane">
<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:10px">BT70以上の銘柄のみ　日付をクリックで詳細表示（直近{_ENTRY_GRID_DAYS}日）</p>
{_month_summary_html(_bt70_entry_sorted)}
{_month_accordion_html(_bt70_entry_by_date, _sorted_bt70_entry_dates, _dseq, "b")}
</div>
<script>
function switchAnalysisTab(seq, which) {{
  var tabs = ['summary','score','cross','bt6069','speed','extra','timing','preoos','maxhold','maxhold_cmp'];
  tabs.forEach(function(t) {{
    var pane = document.getElementById('analtab_'+seq+'_'+t);
    if (pane) pane.classList.toggle('active', t === which);
  }});
  var nav = document.querySelector('#analysis_'+seq+' .analysis-tab-nav');
  if (nav) {{
    nav.querySelectorAll('.analysis-tab-btn').forEach(function(b) {{
      var onclick = b.getAttribute('onclick') || '';
      var m = onclick.match(/switchAnalysisTab\(\d+,'([^']+)'\)/);
      if (m) b.classList.toggle('active', m[1] === which);
    }});
  }}
}}
function switchSumFilter(seq, mode) {{
  var allDiv  = document.getElementById('sumt_'+seq+'_all');
  var bt70Div = document.getElementById('sumt_'+seq+'_bt70');
  var allBtn  = document.getElementById('sumf_'+seq+'_all');
  var bt70Btn = document.getElementById('sumf_'+seq+'_bt70');
  if (!allDiv || !bt70Div) return;
  var isAll = (mode === 'all');
  allDiv.style.display  = isAll ? '' : 'none';
  bt70Div.style.display = isAll ? 'none' : '';
  if (allBtn)  {{ allBtn.style.background  = isAll ? '#3b82f6' : '#1e293b'; allBtn.style.color  = isAll ? '#fff' : '#94a3b8'; allBtn.style.border  = isAll ? 'none' : '1px solid #334155'; }}
  if (bt70Btn) {{ bt70Btn.style.background = isAll ? '#1e293b' : '#3b82f6'; bt70Btn.style.color = isAll ? '#94a3b8' : '#fff'; bt70Btn.style.border = isAll ? '1px solid #334155' : 'none'; }}
}}
function switchDetailTab(seq, which) {{
  var target = document.getElementById('detail_'+seq+'_'+which);
  var closing = target && target.classList.contains('active');
  ['all','bt70','entry','bt70entry'].forEach(function(w) {{
    var pane = document.getElementById('detail_'+seq+'_'+w);
    if (pane) pane.classList.toggle('active', (!closing) && (w === which));
  }});
  var nav = document.getElementById('detail_'+seq+'_all');
  if (nav) {{
    var btns = nav.parentNode.querySelectorAll('.detail-tab-btn');
    var order = ['all','bt70','entry','bt70entry'];
    btns.forEach(function(b, i) {{
      b.classList.toggle('active', (!closing) && order[i] === which);
    }});
  }}
}}
function toggleMG(pfx, seq, ym) {{
  var body = document.getElementById('mgb_'+pfx+seq+'_'+ym);
  var arr  = document.getElementById('mg_arr_'+pfx+seq+'_'+ym);
  if (!body) return;
  var isOpen = body.style.display !== 'none';
  body.style.display = isOpen ? 'none' : 'block';
  if (arr) arr.textContent = isOpen ? '▶' : '▼';
}}
function _showEntryDateGrid(seq, dk, pfx) {{
  var ym     = dk.substr(0,6);
  var btnId  = pfx+'date_btn_'+seq+'_'+dk;
  var detId  = pfx+'date_detail_'+seq+'_'+dk;
  var curBtn = document.getElementById(btnId);
  var isActive = curBtn && curBtn.classList.contains('edate-active');
  // この月の詳細エリアだけ閉じる
  var detArea = document.getElementById('mgd_'+pfx+seq+'_'+ym);
  if (detArea) {{
    detArea.querySelectorAll('[id^="'+pfx+'date_detail_'+seq+'_"]').forEach(function(el) {{ el.style.display='none'; }});
  }}
  // アクティブボタンをリセット（全体）
  var con = document.getElementById('detail_'+seq+'_'+(pfx==='e'?'entry':'bt70entry'));
  if (con) con.querySelectorAll('.edate-btn').forEach(function(b) {{ b.classList.remove('edate-active'); }});
  if (!isActive) {{
    var det = document.getElementById(detId);
    if (det) {{
      det.style.display = 'block';
      setTimeout(function() {{ det.scrollIntoView({{behavior:'smooth', block:'nearest'}}); }}, 50);
    }}
    if (curBtn) curBtn.classList.add('edate-active');
  }}
}}
function showEntryDateE(seq, dk) {{ _showEntryDateGrid(seq, dk, 'e'); }}
function showEntryDateB(seq, dk) {{ _showEntryDateGrid(seq, dk, 'b'); }}
function toggleAnalysis(seq) {{
  var blk = document.getElementById('analysis_'+seq);
  var btn = document.getElementById('analysis_btn_'+seq);
  if (!blk) return;
  var show = (blk.style.display === 'none');
  blk.style.display = show ? 'block' : 'none';
  if (btn) btn.textContent = (show ? '▼ 詳細分析（スクリプト別・スコア別・銘柄別）を隠す'
                                   : '▶ 詳細分析（スクリプト別・スコア別・銘柄別）を表示');
}}
</script>"""


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

/* 取引明細 サブタブ (全部 / BT70以上 / エントリー日別) */
.detail-tab-nav { display:flex; gap:6px; margin-bottom:10px; }
.detail-tab-btn { padding:6px 16px; background:#1e293b; border:1px solid #334155;
                  border-radius:6px; color:#94a3b8; cursor:pointer;
                  font-size:0.85rem; font-family:inherit; }
.detail-tab-btn.active { background:#0d2818; color:#4ade80; border-color:#4ade80; font-weight:700; }
.detail-tab-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.detail-tab-pane { display:none; }
.detail-tab-pane.active { display:block; overflow-x:auto; }

/* 詳細分析ブロック 内部タブ */
.analysis-tab-nav { display:flex; gap:6px; margin-bottom:14px; flex-wrap:wrap; }
.analysis-tab-btn { padding:6px 14px; background:#1e293b; border:1px solid #334155;
                   border-radius:6px; color:#94a3b8; cursor:pointer;
                   font-size:0.85rem; font-family:inherit; }
.analysis-tab-btn.active { background:#0c1f3a; color:#93c5fd; border-color:#60a5fa; font-weight:700; }
.analysis-tab-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.analysis-tab-pane { display:none; }
.analysis-tab-pane.active { display:block; }

/* エントリー日別グリッド */
.edate-grid { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px; }
.edate-btn { display:flex; flex-direction:column; align-items:center; gap:2px;
             padding:6px 10px; background:#1e293b; border:1px solid #334155;
             border-radius:6px; cursor:pointer; font-family:inherit; min-width:72px; }
.edate-btn:hover { background:#263349; border-color:#60a5fa; }
.edate-btn.edate-active { background:#0c1f3a; border-color:#60a5fa; border-width:2px; }
.edate-mm  { font-size:0.85rem; font-weight:700; color:#e2e8f0; }
.edate-stat { font-size:0.68rem; color:#94a3b8; }
.edate-pnl  { font-size:0.72rem; font-weight:600; }

/* 月アコーディオン */
.mg-block  { border:1px solid #1e293b; border-radius:6px; margin-bottom:6px; overflow:hidden; }
.mg-header { display:flex; align-items:center; gap:10px; padding:7px 12px;
             background:#1a2744; cursor:pointer; user-select:none; }
.mg-header:hover { background:#263349; }
.mg-arrow  { color:#60a5fa; font-size:0.75rem; width:12px; flex-shrink:0; }
.mg-ym     { font-weight:700; color:#e2e8f0; font-size:0.88rem; min-width:65px; }
.mg-stats  { color:#94a3b8; font-size:0.78rem; }
.mg-body   { padding:10px 8px 4px; }
.mg-detail { min-height:0; }

/* 詳細分析 折りたたみトグル */
.analysis-toggle { display:block; width:100%; text-align:left;
                   padding:10px 16px; margin:16px 0; background:#1e293b;
                   border:1px solid #334155; border-radius:6px; color:#93c5fd;
                   cursor:pointer; font-size:0.9rem; font-family:inherit; font-weight:600; }
.analysis-toggle:hover { background:#263349; color:#e2e8f0; }
.analysis-block { border-left:2px solid #1e293b; padding-left:4px; margin-bottom:8px; }

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
table { width:100%; min-width:900px; border-collapse:collapse; font-size:0.83rem; margin-bottom:8px; }
th { background:#0f2040; color:#cbd5e1; padding:9px 10px;
     border:1px solid #334155; border-bottom:2px solid #3b82f6;
     text-align:center; white-space:nowrap; font-size:0.82rem; font-weight:700;
     letter-spacing:0.03em; position:sticky; top:0; z-index:1; }
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
    parser.add_argument("--symbol",       type=str, default=None, help="銘柄コードで絞り込み (カンマ区切り, 例: --symbol 8061.T または --symbol 8061.T,8173.T)")
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
        sym_list = [s.strip() for s in args.symbol.split(",")] if args.symbol else None
        tab5_html = _tab5_pnl_html(args.days, args.workers, cfg_filter=args.config,
                                   symbol_filter=sym_list)

    html_path = Path(f"nikkei_analysis_{ref_date}.html")
    html_path.write_text(
        build_html(close, trend, r, periods, up_timing, args.years, ref_date,
                   indicators=indicators, tab4_html=tab4_html, tab5_html=tab5_html,
                   wf_banner=wf_banner),
        encoding="utf-8"
    )
    print(f"生成: {html_path}")

    if not args.no_browser:
        from _open_html import open_html
        open_html(html_path)


if __name__ == "__main__":
    main()
