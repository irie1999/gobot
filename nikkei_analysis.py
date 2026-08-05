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
# 損益タブの月別グリッドを全期間表示するためのバックテスト窓(日)。
# None/365以下 → 従来どおり直近365日のみ。run_signals_holdout_all が --days に応じて設定。
# backtest_one(スコア計算・ライブ)は一切変えず、表示用の全期間 trade_log だけ別途取得する。
_BT_WINDOW_DAYS: int | None = None
# 過去検証で損益タブを「基準日〜基準日+N日」だけ集計する(現在まで走らせず軽量化)。
# _REPORT_END を date に設定すると until をそこにし、全期間trade_logも同日でトリムする。
_REPORT_START = None   # 基準日 (date)。全期間バックテストの下限側(warmup)の起点に使う。
_REPORT_END = None     # 集計終了日 (date)。None なら現在(_TODAY)まで。
# 損益タブ: 約定値(entry_p)で取引を弾く予算フィルタ。0=無効。
# 選定時の latest_price フィルタと違い「約定時に予算内で買えた取引だけ」に絞る。
# (選定時は安かったが約定時に急騰した銘柄=100株買えない取引を明細/集計から除外)
_PNL_ENTRY_MIN_PRICE = 0.0
_PNL_ENTRY_MAX_PRICE = 0.0
# 予算月別CSV(LSS_BUDGET_MONTHLY_CSV)出力: これまでに書いた最多月数。_tab5_pnl_html は
# メイン/期間パネル/銘柄詳細で多数回呼ばれるので、最長窓(月数最多)の1回だけを採用する。
_BUD_CSV_BEST_MONTHS = -1
# 全取引CSV(LSS_TRADES_CSV)出力: これまでに書いた最多取引件数。予算CSVと同じく最長窓の1回だけ採用。
_TRADES_CSV_BEST_N = -1
# 損益タブ: ロングBTスコアで銘柄を絞るフィルタ。0=無効。
# mirror/lss(ロングミラー/ロング銘柄ショート)で「ロングが弱い銘柄だけをフェード」する用。
# _LONG_BT_REF が与えられればそのスコア(=別モードで測ったロングBT)で判定し、
# 無ければ現モードの item BTスコア(rec_score)で判定する。
_PNL_BT_MAX = 0.0
_PNL_BT_MIN = 0.0
_LONG_BT_REF: dict[tuple, float] = {}   # (symbol, strategy) -> ロングBTスコア
_SAMEDAY_SWEEP_TAB = False   # mirror/lss 用: 詳細分析に「同日TP/SLスイープ」タブを出す
# run_signals_holdout_all から注入する追加トレードレコード（lss転換ロングなど）。
# display_trades に結合して月別アコーディオン・日別カードに自然に混合表示される。
_EXTRA_TRADES: list = []
_SAMEDAY_SWEEP_INVERTED = True   # ミラー(符号反転)なら True / ロング銘柄ショートなら False
_SAMEDAY_5M_TAB = False   # mirror/lss 用: 詳細分析に「5分足TP/SL最適化」タブを出す


def _bt_filter_banner_html() -> str:
    """ロングBTフィルタが有効なとき、損益タブに表示する説明バナー(mirror/lss用)。"""
    if not (_PNL_BT_MAX > 0 or _PNL_BT_MIN > 0):
        return ""
    _lo = f"{_PNL_BT_MIN:.0f} ≤ " if _PNL_BT_MIN > 0 else ""
    _hi = f" &lt; {_PNL_BT_MAX:.0f}" if _PNL_BT_MAX > 0 else ""
    _src = "別途測定したロングBT" if _LONG_BT_REF else "現レポートのBT"
    return (
        '<div style="margin:10px 0 14px;padding:10px 14px;border-radius:8px;'
        'background:#1e1b2e;border:1px solid #6d28d9;color:#ddd6fe;font-size:0.86rem">'
        f'🔎 <b>ロングBTフィルタ有効</b>: この損益レポートは全体で '
        f'<b style="color:#c4b5fd">ロングBT {_lo}銘柄{_hi}</b> '
        'の銘柄だけを集計しています（＝ロングが弱い銘柄をフェード）。'
        '上の月別グリッド・KPI・下の取引明細すべてに適用済み。<br>'
        f'<span style="color:#a5b4fc;font-size:0.78rem">判定は{_src}で行っています。'
        '閾値は <code>--bt-max</code> / <code>--bt-min</code> で変更できます。</span>'
        '</div>'
    )
_last_signals: list[dict] = []   # _tab4_signals_html() 呼び出し後に最新シグナルリストを保持
_FROZEN_BT_SCORES: dict[tuple, int] = {}  # (symbol, strategy) → 初回発信時のBTスコア (外部から注入)
_SIGNAL_DATE_BT_SCORES: dict[tuple, int] = {}  # (symbol, strategy, signal_date_str) → シグナル発生時BTスコア (外部から注入)

# トレンドフィルタ版(TF)は基底戦略の別名。凍結スコアは改名前(VOL/MACD)で
# 記録されていることがあるため、照会時に両表記を試せるよう対応表を持つ。
_TF_STRAT_ALIASES: dict[str, str] = {
    "VOLTF": "VOL", "VOL": "VOLTF",
    "MACDTF": "MACD", "MACD": "MACDTF",
}


def _lookup_signal_date_bt(sym: str, strat: str, sig_date_str: str | None):
    """(sym, strat, signal_date) の発生時BTスコアを返す。
    TF別名(VOLTF↔VOL / MACDTF↔MACD)でも探す。無ければ None。"""
    if not sig_date_str:
        return None
    v = _SIGNAL_DATE_BT_SCORES.get((sym, strat, sig_date_str))
    if v is not None:
        return v
    alt = _TF_STRAT_ALIASES.get(strat)
    if alt is not None:
        v = _SIGNAL_DATE_BT_SCORES.get((sym, alt, sig_date_str))
        if v is not None:
            return v
    return None


def _lookup_frozen_bt(sym: str, strat: str):
    """(sym, strat) の凍結BTスコアを返す。TF別名でも探す。無ければ None。"""
    v = _FROZEN_BT_SCORES.get((sym, strat))
    if v is not None:
        return v
    alt = _TF_STRAT_ALIASES.get(strat)
    if alt is not None:
        return _FROZEN_BT_SCORES.get((sym, alt))
    return None
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

# mirror/lss (ロング銘柄を空売りで評価する分析専用モード) のとき True。
# シグナルタブの発注ボタンはロング逆指値買いの値を送るため、このモードでは
# 発注を無効化して誤発注を防ぐ(実発注は逆指値空売りの専用フローが必要)。
_ANALYSIS_ONLY: bool = False

# lss タブ(逆指値空売り選定)のとき True。発注ボタンを『信用新規売りの逆指値』
# として正しく送れるようにする(side=short / order_server が send_stop_sell で発注)。
# lss は同日決済なので自動利確(target)は付けず、エントリーのみ発注する
# (同日引けの買戻しは手動 or close_stop_guard で対応)。mirror(却下)は対象外。
_LSS_ORDER_MODE: bool = False

# _LSS_ORDER_MODE の「向き」フラグ。False(既定)=lss(逆指値空売り・売り建玉)。
# True=ロングデイトレ(逆指値買い=上ブレイク・同日決済/買い建玉)。同日決済レポートの
# 骨格(月別/BT50/予算400万タブ)は共通で使い、発注side・損切(下)/利確(上)・ラベルだけ
# ロング向きに反転する。run_signals_holdout_all の --long-daytrade で立てる。
_LSS_LONG: bool = False

# lss の損切/利確 ATR倍率(実際の注文内容に合わせた表示計算に使う)。
# run_signals_holdout_all が _bte._SM_FORCE/_TM_FORCE(既定 0.1/1.0)から設定する。
_LSS_SM: float = 0.1
_LSS_TM: float = 1.0

# lss proposal をマージした場合、(銘柄, 戦略) → 選定基準月ラベル(例 "12/6")。
# merge_lss_proposals.py が出力する SOURCE_BASES を run_signals_holdout_all が流し込む。
# 空なら基準月バッジは出ない(単一基準・後方互換)。
_LSS_SRC_BASES: dict = {}
_LSS_START_DATES: dict = {}   # (正規化コード, 戦略) -> OOS開始日文字列。run_signals が流し込む。


def _lss_src_base_of(sym, strat):
    """(銘柄, 戦略) の選定基準月ラベルを返す(無ければ None)。銘柄は .T 有無を吸収。"""
    if not _LSS_SRC_BASES:
        return None
    _c = str(sym).upper().removesuffix(".T").split(".")[0]
    return (_LSS_SRC_BASES.get((_c, strat)) or _LSS_SRC_BASES.get(_c)
            or _LSS_SRC_BASES.get((str(sym), strat)))

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

    # ── 大局レジーム (上げ/横ばい/下げ) ──────────────────────────────
    # 終値のみなのでADXの代わりに「効率比ER(トレンド強度)」+ MA200傾き で判定。
    #   ER = 60日の正味変化 / 経路長。1に近い=強トレンド, 0に近い=もみ合い。
    ma75 = float(close.rolling(75).mean().iloc[-1]) if len(close) >= 75 else None
    slope200 = 0.0
    if ma200 and len(close) >= 221:
        _ma200_prev = float(close.rolling(200).mean().iloc[-21])
        slope200 = (ma200 / _ma200_prev - 1) * 100 if _ma200_prev else 0.0
    er = 0.0
    if len(close) > 61:
        _net  = abs(float(close.iloc[-1]) - float(close.iloc[-61]))
        _path = float(close.diff().abs().tail(60).sum())
        er = _net / _path if _path > 0 else 0.0
    if er < 0.20:                                   # トレンド弱い = 横ばい
        macro = "sideways"
    elif above_ma200 and slope200 > 0:
        macro = "up"
    elif (not above_ma200) and slope200 < 0:
        macro = "down"
    else:                                            # 方向が曖昧 = 移行/横ばい扱い
        macro = "sideways"

    return {
        "cur": cur, "ma10": ma10, "ma25": ma25, "ma75": ma75, "ma200": ma200,
        "vol": vol14, "vol_level": vol_level, "trend": trend,
        "mom5": mom5, "mom20": mom20,
        "above_ma200": above_ma200, "max_1d_drop": max_1d_drop,
        "macro": macro, "er": er, "slope200": slope200,
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
            return "❌ 停止", f"トレンド下落 (MA10&lt;MA25)", "相場回復まで使用停止"
        if not above:
            return "❌ 停止", f"日経 &lt; MA200 (長期下落トレンド)", "既存版のみに絞る"
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


def _regime_history_html(ref_date, months: int = 120) -> str:
    """相場環境タブ用: 過去の大局レジーム(月末)を折りたたみ表で返す。
    各月末までの終値だけで判定(先読みなし・get_regime と同一ロジック)。"""
    try:
        import numpy as np
        c = fetch_n225(months // 12 + 3, end_date=ref_date)
        if c is None or len(c) < 260:
            return ""
        c = c.sort_index()
        ma200 = c.rolling(200).mean()
        slope200 = ma200.pct_change(20) * 100
        er = (c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, np.nan)
        above = c >= ma200
        _df = pd.DataFrame({"c": c, "ma200": ma200, "slope": slope200,
                            "er": er, "above": above})
        _df["ym"] = c.index.strftime("%Y-%m")
        _me = _df.groupby("ym").tail(1).tail(months)
        _lbl = {"up": ("🟢 上げ", "#4ade80"), "sideways": ("🟡 横ばい", "#fbbf24"),
                "down": ("🔴 下げ", "#f87171"), "?": ("―", "#64748b")}

        def _mac(row):
            if pd.isna(row["ma200"]) or pd.isna(row["er"]):
                return "?"
            if row["er"] < 0.20:
                return "sideways"
            if row["above"] and row["slope"] > 0:
                return "up"
            if (not row["above"]) and row["slope"] < 0:
                return "down"
            return "sideways"

        rows = ""
        cnt = {"up": 0, "sideways": 0, "down": 0}
        for _, row in _me.iterrows():
            rg = _mac(row)
            if rg in cnt:
                cnt[rg] += 1
            lbl, col = _lbl.get(rg, ("―", "#64748b"))
            vm = (row["c"] / row["ma200"] - 1) * 100 if not pd.isna(row["ma200"]) else float("nan")
            rows += (f'<tr><td style="padding:2px 6px">{row["ym"]}</td>'
                     f'<td style="text-align:right;padding:2px 6px">{row["c"]:,.0f}</td>'
                     f'<td style="text-align:right;padding:2px 6px">{row["er"]:.2f}</td>'
                     f'<td style="text-align:right;padding:2px 6px">{row["slope"]:+.1f}%</td>'
                     f'<td style="text-align:right;padding:2px 6px">{vm:+.1f}%</td>'
                     f'<td style="padding:2px 6px;color:{col};font-weight:700">{lbl}</td></tr>')
        if not rows:
            return ""
        _sm = f'上げ {cnt["up"]} / 横ばい {cnt["sideways"]} / 下げ {cnt["down"]} ヶ月'
        return (
            '<details style="margin:10px 0 4px;background:#0d1424;border:1px solid #1e293b;'
            'border-radius:8px;padding:8px 14px">'
            '<summary style="cursor:pointer;color:#93c5fd;font-weight:600">'
            f'▶ 過去の大局レジーム推移（月末・{len(_me)}ヶ月）— {_sm}</summary>'
            '<div style="max-height:420px;overflow-y:auto;margin-top:8px">'
            '<table style="border-collapse:collapse;font-size:0.78rem;width:auto">'
            '<thead><tr style="color:#64748b;border-bottom:1px solid #1e293b">'
            '<th style="text-align:left;padding:2px 6px">月</th>'
            '<th style="text-align:right;padding:2px 6px">終値</th>'
            '<th style="text-align:right;padding:2px 6px">ER</th>'
            '<th style="text-align:right;padding:2px 6px">MA200傾</th>'
            '<th style="text-align:right;padding:2px 6px">vsMA200</th>'
            '<th style="text-align:left;padding:2px 6px">レジーム</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            '<p style="font-size:0.7rem;color:#64748b;margin:6px 0 0">'
            'ER=トレンド強度(0=もみ合い/1=強トレンド)。ER&lt;0.20=横ばい。'
            '各月末までの終値のみで判定(先読みなし)。</p></details>'
        )
    except Exception:
        return ""


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

    _macro_map = {
        "up":       ("🟢 上げ", "#4ade80", "順張り(現行)が得意"),
        "sideways": ("🟡 横ばい", "#fbbf24", "逆張り(RSI2)向き・現行は苦手"),
        "down":     ("🔴 下げ", "#f87171", "現金/ショート"),
    }
    _mlbl, _mcol, _mnote = _macro_map.get(r.get("macro", "sideways"), ("―", "#94a3b8", ""))

    # 横ばい戦略(指数ETF STOロングbounce)のゲート状態。
    # check_signals_index の厳密判定(ER<0.15 かつ |MA200傾き|<0.5%)と一致させる。
    _g_er = r.get("er", 0.0)
    _g_sl = r.get("slope200", 0.0)
    _gate_on = (_g_er < 0.15 and abs(_g_sl) < 0.5)
    if _gate_on:
        _gate_val = (
            '<span style="color:#4ade80;font-weight:700;font-size:1.1rem">● 発動中</span>'
            '<br><span style="font-size:0.68rem;color:#64748b">'
            '指数ETF逆張り(check_signals_index)を発注可</span>')
    else:
        _greason = []
        if _g_er >= 0.15:
            _greason.append(f"ER {_g_er:.2f}≥0.15")
        if abs(_g_sl) >= 0.5:
            _greason.append(f"MA200傾き{_g_sl:+.1f}%(±0.5%超)")
        _gate_val = (
            '<span style="color:#94a3b8;font-weight:700;font-size:1.1rem">○ 横ばい待ち</span>'
            '<br><span style="font-size:0.68rem;color:#64748b">'
            + (" / ".join(_greason) or "条件計算中")
            + '<br>まだ真のレンジでない=現行(順張り)の管轄</span>')

    regime_items = [
        ("日経225",     f'<strong style="font-size:1.25rem">{r["cur"]:,.0f}円</strong>'),
        ("大局レジーム",
         f'<span style="color:{_mcol};font-weight:700;font-size:1.15rem">{_mlbl}</span>'
         f'<br><span style="font-size:0.68rem;color:#64748b">{_mnote}<br>'
         f'ER {r.get("er",0):.2f} / MA200傾き {r.get("slope200",0):+.1f}%</span>'),
        ("横ばい戦略ゲート", _gate_val),
        ("トレンド(短期)", f'<span style="color:{trend_color};font-weight:700;font-size:1.05rem">{trend_ja}</span>'),
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

    _regime_hist_html = _regime_history_html(ref_date)

    return f"""
<h2>{ref_date} 時点の相場環境（日経225）</h2>
<div class="regime-panel">{regime_html}</div>
{_regime_hist_html}
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


def _tab2_trend_html(close: pd.Series, trend: pd.Series, periods: list[dict], years: int,
                     trades: list[dict] | None = None) -> str:
    """タブ2: トレンド期間分析。trades を渡すと全期間一覧に損益列を追加する。"""
    # 期間ごとの損益 (エントリー(約定)日が期間内のトレードを集計)。損益タブの日別と基準を統一。
    _pnl_by_period: dict = {}
    _regime_agg: dict = {}   # trend種別 -> {"all":{...}, "bt70":{...}, "n_periods":int}
    if trades:
        def _agg(_sub):
            _gp = sum(_t["pnl"] for _t in _sub if _t.get("pnl", 0) > 0)
            _gl = abs(sum(_t["pnl"] for _t in _sub if _t.get("pnl", 0) <= 0))
            return (len(_sub), _gp, _gl)
        _rbucket = {"up": [], "down": [], "sideways": []}
        _nperiods = {"up": 0, "down": 0, "sideways": 0}
        for _pp in periods:
            _ps, _pe = _pp["start"], _pp["end"]
            _sub = [_t for _t in trades
                    if _t.get("entry_d_raw") and _ps <= _t["entry_d_raw"] <= _pe]
            _pnl_by_period[(_ps, _pe)] = {
                "all":  _agg(_sub),
                "bt70": _agg([_t for _t in _sub if (_t.get("rec_score") or 0) >= 70]),
            }
            _tr = _pp.get("trend")
            if _tr in _rbucket:
                _rbucket[_tr].extend(_sub)
                _nperiods[_tr] += 1
        def _rstats(_lst):
            _n = len(_lst)
            _wins = sum(1 for _t in _lst if _t.get("pnl", 0) > 0)
            _gp = sum(_t["pnl"] for _t in _lst if _t.get("pnl", 0) > 0)
            _gl = abs(sum(_t["pnl"] for _t in _lst if _t.get("pnl", 0) <= 0))
            _pnl = _gp - _gl
            _pf = _gp / _gl if _gl > 0 else (float("inf") if _gp > 0 else 0.0)
            return {"n": _n, "wr": (_wins / _n * 100 if _n else 0.0),
                    "gp": _gp, "gl": _gl,
                    "pnl": _pnl, "pf": _pf, "avg": (_pnl / _n if _n else 0.0)}
        for _tr in _rbucket:
            _bt = [_t for _t in _rbucket[_tr] if (_t.get("rec_score") or 0) >= 70]
            _regime_agg[_tr] = {"all": _rstats(_rbucket[_tr]),
                                "bt70": _rstats(_bt),
                                "n_periods": _nperiods[_tr]}
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
        _pnl_cells = ""
        if trades:
            _d = _pnl_by_period.get((p['start'], p['end']), {"all": (0, 0.0, 0.0), "bt70": (0, 0.0, 0.0)})

            def _dual_cell(metric):
                # metric(cnt,gp,gl) -> (表示文字, 色) を全/BT70で出し分けるtd
                def _fmt(_t):
                    _c, _gp, _gl = _t
                    if metric == "cnt":
                        return (f"{_c}件", "#94a3b8") if _c else ("—", "#475569")
                    if not _c:
                        return ("—", "#475569")
                    if metric == "gp":
                        return (f"+{_gp:,.0f}", "#4ade80")
                    if metric == "gl":
                        return (f"-{_gl:,.0f}", "#f87171")
                    _net = _gp - _gl
                    return (f"{_net:+,.0f}", "#4ade80" if _net >= 0 else "#f87171")
                _ta, _ca = _fmt(_d["all"])
                _tb, _cb = _fmt(_d["bt70"])
                _w = "font-weight:700" if metric == "net" else ""
                return (f'<td style="text-align:right">'
                        f'<span class="m-all"  style="color:{_ca};{_w}">{_ta}</span>'
                        f'<span class="m-bt70" style="color:{_cb};{_w};display:none">{_tb}</span></td>')
            _pnl_cells = (_dual_cell("cnt") + _dual_cell("gp")
                          + _dual_cell("gl") + _dual_cell("net"))
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
  {_pnl_cells}
</tr>"""

    _pnl_toggle = ""
    if trades:
        _pnl_toggle = """
<div style="margin:6px 0 10px">
  <span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">損益の対象:</span>
  <button id="trdpnl-all-btn" onclick="setTrdPnl('all')"
    style="padding:5px 14px;border:none;border-radius:6px;cursor:pointer;font-weight:600;
    background:#2d6cdf;color:#fff">全トレード</button>
  <button id="trdpnl-bt70-btn" onclick="setTrdPnl('bt70')"
    style="padding:5px 14px;border:none;border-radius:6px;cursor:pointer;font-weight:600;
    background:#1e293b;color:#94a3b8">BT70以上</button>
</div>
<script>
function setTrdPnl(m){
  document.querySelectorAll('.m-all').forEach(function(e){e.style.display = (m==='all')?'':'none';});
  document.querySelectorAll('.m-bt70').forEach(function(e){e.style.display = (m==='bt70')?'':'none';});
  var a=document.getElementById('trdpnl-all-btn'), b=document.getElementById('trdpnl-bt70-btn');
  if(a&&b){
    a.style.background = (m==='all')?'#2d6cdf':'#1e293b'; a.style.color=(m==='all')?'#fff':'#94a3b8';
    b.style.background = (m==='bt70')?'#2d6cdf':'#1e293b'; b.style.color=(m==='bt70')?'#fff':'#94a3b8';
  }
}
</script>"""

    # ── レジーム別 損益サマリー (全/BT70は上のトグルに連動) ──
    _regime_summary_html = ""
    if trades and _regime_agg:
        def _rcell(a_val, a_col, b_val, b_col, bold=False):
            _w = "font-weight:700" if bold else ""
            return (f'<td style="text-align:right;padding:5px 14px">'
                    f'<span class="m-all"  style="color:{a_col};{_w}">{a_val}</span>'
                    f'<span class="m-bt70" style="color:{b_col};{_w};display:none">{b_val}</span></td>')
        def _pnl_c(x):
            return "#4ade80" if x >= 0 else "#f87171"
        def _pf_s(x):
            return "∞" if x == float("inf") else f"{x:.2f}"
        _srows = ""
        for _key, _lbl, _col in (("up", "▲ 上昇", "#4ade80"),
                                 ("down", "▼ 下落", "#f87171"),
                                 ("sideways", "→ 横ばい", "#fbbf24")):
            _ag = _regime_agg.get(_key)
            if not _ag:
                continue
            _a, _b = _ag["all"], _ag["bt70"]
            _srows += (
                f'<tr>'
                f'<td style="text-align:left;color:{_col};font-weight:700;padding:5px 14px">{_lbl}</td>'
                f'<td style="text-align:right;color:#94a3b8;padding:5px 14px">{_ag["n_periods"]}期間</td>'
                + _rcell(f'{_a["n"]}件', "#94a3b8", f'{_b["n"]}件', "#94a3b8")
                + _rcell(f'{_a["wr"]:.0f}%', "#e2e8f0", f'{_b["wr"]:.0f}%', "#e2e8f0")
                + _rcell(_pf_s(_a["pf"]), "#e2e8f0", _pf_s(_b["pf"]), "#e2e8f0")
                + _rcell(f'+{_a["gp"]:,.0f}', "#4ade80", f'+{_b["gp"]:,.0f}', "#4ade80")
                + _rcell(f'-{_a["gl"]:,.0f}', "#f87171", f'-{_b["gl"]:,.0f}', "#f87171")
                + _rcell(f'{_a["pnl"]:+,.0f}', _pnl_c(_a["pnl"]), f'{_b["pnl"]:+,.0f}', _pnl_c(_b["pnl"]), bold=True)
                + _rcell(f'{_a["avg"]:+,.0f}', _pnl_c(_a["avg"]), f'{_b["avg"]:+,.0f}', _pnl_c(_b["avg"]))
                + '</tr>')
        _regime_summary_html = (
            '<h2>レジーム別 損益サマリー'
            '<span style="font-size:0.75rem;color:#64748b;font-weight:400">'
            '（約定日が属するレジームで集計・上の「損益の対象」トグルで全/BT70切替）</span></h2>'
            '<p class="footnote" style="color:#94a3b8;margin:2px 0 8px">'
            '各レジーム(日経の上昇/下落/横ばい)で発注したトレードの合計。'
            '<b>「上昇の日だけ張る」が有効か</b>は、上昇の総損益が突出して大きく、下落/横ばいが'
            'マイナス〜低調かで判断する。3つとも大きくプラスなら、レジームでの足切りは逆効果。</p>'
            '<table style="width:auto;min-width:660px;border-collapse:collapse">'
            '<thead><tr style="color:#64748b;font-size:0.75rem;border-bottom:1px solid #334155">'
            '<th style="text-align:left;padding:5px 14px">レジーム</th>'
            '<th style="padding:5px 14px">期間数</th>'
            '<th style="padding:5px 14px">取引数</th>'
            '<th style="padding:5px 14px">勝率</th>'
            '<th style="padding:5px 14px">PF</th>'
            '<th style="padding:5px 14px;color:#4ade80">利益計</th>'
            '<th style="padding:5px 14px;color:#f87171">損失計</th>'
            '<th style="padding:5px 14px">総損益</th>'
            '<th style="padding:5px 14px">平均/取引</th>'
            f'</tr></thead><tbody>{_srows}</tbody></table>')

    return f"""
<h2>現在のトレンド状況</h2>
{current_box}

<h2>トレンド統計（過去{years}年）</h2>
<div style="display:flex;flex-wrap:wrap;gap:16px">
  {up_card}
  {down_card}
</div>

{_pnl_toggle}
{_regime_summary_html}

<h2>全トレンド期間一覧（新しい順）</h2>
<table>
<thead><tr>
  <th>種別</th><th>開始日</th><th>終了日</th>
  <th>日数</th><th>騰落率</th><th>最大下落</th>
  <th>開始日終値(円)</th><th>最安値(円)</th><th>終了日終値(円)</th>
  {'<th>件数</th><th>利益計</th><th>損失計</th><th>損益合計</th>' if trades else ''}
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

# BT高・WF低(OOS弱)の警告しきい値: (BT下限, WF上限)。③-b 影響分析の集計に使用。
_BT_HIGH_WF_LOW = (60, 40)


def _rolling_oos_cache_html() -> str:
    """例外が絶対に外へ漏れないラッパ(現行レポートを壊さない保証)。"""
    try:
        return _rolling_oos_cache_html_impl()
    except Exception as _e:
        return (f'<p class="footnote" style="color:#94a3b8">月次ロールフォワード表示スキップ '
                f'(キャッシュ異常: {_e})</p>')


def _rolling_oos_cache_html_impl() -> str:
    """rolling_selection_validation.py が書いた rolling_oos_cache.json を読み、
    現行レポート風の月次ロールフォワードOOSマトリクスを描画。未計算なら案内を返す。"""
    import json as _json2
    from pathlib import Path as _P2
    p = _P2("rolling_oos_cache.json")
    if not p.exists():
        return ('<h2 style="margin-top:8px">★ 月次ロールフォワードOOS検証（後知恵ゼロ）</h2>'
                '<p class="footnote" style="color:#94a3b8">まだ計算されていません。以下を別途実行(重い・夜間推奨)すると、'
                'ここに現行と同じ形式で各基準月の結果が表示されます:<br>'
                '<code>python rolling_selection_validation.py --both --price-ranges 6000,10000 '
                '--min-price 1000 --start 2025-07 --workers 4</code></p>')
    try:
        d = _json2.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return f'<p class="footnote">ロールフォワードキャッシュ読込失敗: {e}</p>'
    months = d.get("months", [])
    bases = d.get("base_months", [])
    cells_data = d.get("cells", {})
    month_h = "".join(f"<th>{m[5:]}月</th>" for m in months)

    def _matrix(fwd_key: str, sel_key: str):
        """fwd_key('fwd' or 'fwd70') の月次マトリクスHTMLと翌月OOS合計を返す。"""
        rows = ""
        dt = dn = dw = 0
        for bm in bases:
            c = cells_data.get(bm, {})
            fwd = c.get(fwd_key, {})
            cells = ""
            s_pnl = s_n = s_w = 0
            for m in months:
                if m <= bm:
                    cells += '<td style="color:#334155;text-align:center">·</td>'
                    continue
                v = fwd.get(m)
                if not v:
                    cells += '<td style="color:#475569;text-align:center">—</td>'
                    continue
                p_ = v.get("pnl", 0); n_ = v.get("n", 0); w_ = v.get("w", 0)
                s_pnl += p_; s_n += n_; s_w += w_
                col = "#4ade80" if p_ > 0 else ("#f87171" if p_ < 0 else "#94a3b8")
                cells += (f'<td style="text-align:right;color:{col};font-weight:700">{p_:+,.0f}'
                          f'<br><span style="font-size:.68rem;color:#94a3b8">{n_}件 {w_}勝</span></td>')
            nxt = next((m for m in months if m > bm), None)
            if nxt and fwd.get(nxt):
                _nv = fwd[nxt]
                dt += _nv.get("pnl", 0); dn += _nv.get("n", 0); dw += _nv.get("w", 0)
            twr = s_w / s_n * 100 if s_n else 0
            tc = "#4ade80" if s_pnl > 0 else ("#f87171" if s_pnl < 0 else "#94a3b8")
            selc = c.get(sel_key, 0)
            rows += (f'<tr><td style="text-align:left;white-space:nowrap">{bm} まで選定'
                     f'<br><span style="font-size:.65rem;color:#94a3b8">{selc}件</span></td>'
                     f'{cells}<td style="text-align:right;color:{tc};font-weight:700">{s_pnl:+,.0f}円'
                     f'<br><span style="font-size:.65rem;color:#94a3b8">{s_n}件 {twr:.0f}%</span></td></tr>')
        dwr = dw / dn * 100 if dn else 0
        dc = "#4ade80" if dt > 0 else "#f87171"
        summary = (f'<div style="background:#1e293b;border-left:4px solid {dc};padding:8px 12px;margin:8px 0">'
                   f'<b>翌月OOS合計(非重複):</b> <b style="color:{dc};font-size:1.15rem">{dt:+,.0f}円</b>'
                   f' &nbsp;{dn}件 勝率{dwr:.0f}%</div>')
        table = (f'<div style="overflow-x:auto"><table>'
                 f'<thead><tr><th style="text-align:left">選定基準月</th>{month_h}<th>全期間計</th></tr></thead>'
                 f'<tbody>{rows or "<tr><td colspan=99>データなし</td></tr>"}</tbody></table></div>')
        return summary + table

    # BT70取引明細(基準月ごとに折りたたみ)
    det_html = ""
    for bm in bases:
        det = cells_data.get(bm, {}).get("det70", [])
        if not det:
            continue
        det_sorted = sorted(det, key=lambda x: x.get("signal", ""))
        drows = ""
        for t in det_sorted:
            pnl = t.get("pnl", 0)
            pc = "#4ade80" if pnl > 0 else ("#f87171" if pnl < 0 else "#94a3b8")
            drows += (f'<tr><td>{t.get("signal","")}</td><td>{t.get("exit","")}</td>'
                      f'<td style="text-align:left">{t.get("sym","")}</td>'
                      f'<td>{t.get("strat","")}/{t.get("mode","")}</td>'
                      f'<td style="text-align:center">{t.get("bt","")}</td>'
                      f'<td style="text-align:right;color:{pc};font-weight:700">{pnl:+,.0f}</td>'
                      f'<td>{t.get("reason","")}</td></tr>')
        _tot = sum(t.get("pnl", 0) for t in det)
        _w = sum(1 for t in det if t.get("pnl", 0) > 0)
        det_html += (f'<details style="margin:4px 0"><summary style="cursor:pointer">'
                     f'{bm} まで選定 の BT70取引明細 — {len(det)}件 {_w}勝 '
                     f'<b style="color:{"#4ade80" if _tot>0 else "#f87171"}">{_tot:+,.0f}円</b></summary>'
                     f'<table style="margin-top:4px"><thead><tr><th>シグナル日</th><th>決済日</th>'
                     f'<th style="text-align:left">銘柄</th><th>戦略/モード</th><th>BT</th><th>損益</th>'
                     f'<th>理由</th></tr></thead><tbody>{drows}</tbody></table></details>')

    return f"""
<h2 style="margin-top:8px">★ 月次ロールフォワードOOS検証（後知恵ゼロ / 現行と同一選定を各基準月で再現）</h2>
<p class="footnote">{d.get("meta", "")}<br>生成日: {d.get("generated", "")}（rolling_selection_validation.py のキャッシュ）</p>
<h3 style="color:#cbd5e1;margin:14px 0 2px">① 全選定銘柄</h3>
{_matrix("fwd", "sel")}
<h3 style="color:#fbbf24;margin:18px 0 2px">② BT70以上のみ（各基準月末時点BT・後知恵なし）</h3>
{_matrix("fwd70", "n70")}
<h3 style="color:#cbd5e1;margin:18px 0 6px">③ BT70 取引明細（基準月ごと・クリックで展開）</h3>
{det_html or '<p class="footnote" style="color:#94a3b8">BT70取引明細なし(キャッシュが旧版の可能性→再実行で付与)。</p>'}
<p class="footnote">各セル=OOS損益(取引数/勝ち)。<span style="color:#334155">·</span>=基準月以前/—=取引なし。
 「全期間計」は月が重複するので足さない。非重複は「翌月OOS合計」。BTは各基準月末までの実績で算出=後知恵なし。</p>
"""


def _holdout_window_map() -> dict:
    """(symbol, strategy) → その銘柄が選定された中で最長の holdout_days。
    純OOS判定は「選定に使っていない直近除外窓」で行うが、同じ銘柄でも設定に
    よって窓長(HO30d〜HO180d)が違う。最長窓を使うと除外期間が最も広く取れ、
    OOSトレードを十分拾える(短窓HO30dだけだと数件しか拾えない)。"""
    import re as _re_hw
    m: dict = {}
    for cfg in _PNL_CONFIGS:
        _mm = _re_hw.search(r"HO(\d+)d", cfg.get("label", "") or "")
        if not _mm:
            continue
        hd = int(_mm.group(1))
        for wl in (cfg.get("stop_wl") or [], cfg.get("brk_wl") or []):
            for row in wl:
                try:
                    k = (row[0], row[2])
                except Exception:
                    continue
                if hd > m.get(k, 0):
                    m[k] = hd
    return m


def _oos_weak_badge(s: dict) -> str:
    """⚠OOS弱 バッジHTML。純OOS(各configの直近除外窓=選定に使っていない期間)の
    損益がマイナスの銘柄に赤バッジ。WF(選定バイアスあり)ではなく holdout実績で判定。
    OOSトレードが無い(窓に決済トレードなし)場合はバッジ無し。"""
    _on = s.get("oos_n", 0) or 0
    _op = s.get("oos_pnl", 0) or 0
    _ow = s.get("oos_win", 0) or 0
    if _on > 0 and _op < 0:
        return (
            '<span style="background:#7f1d1d;color:#fca5a5;padding:1px 5px;'
            'border-radius:3px;font-size:0.62rem;display:block;margin-top:1px" '
            f'title="純OOS({_ow}日除外窓)で {_op:+,.0f}円 / {_on}件。選定に使っていない期間で負けている">'
            f'⚠OOS弱 {_op:+,.0f}円</span>')
    return ""


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
    # BTスコアの計算元になった実取引数。少ない(10件未満)と少数サンプル=赤で警告
    _bt_tr = s.get("bt_trades")
    if _bt_tr is not None:
        _tr_c = "#f87171" if _bt_tr < 10 else "#94a3b8"
        _tr_badge = (f'<span style="font-size:0.62rem;color:{_tr_c};display:block;margin-top:1px">'
                     f'取引{_bt_tr}件{"⚠少" if _bt_tr < 10 else ""}</span>')
    else:
        _tr_badge = ""
    # ⚠OOS弱 バッジ: 純OOS(各configが選定に使っていない直近除外窓)の損益が
    # マイナスなら警告。WF(選定バイアスあり)ではなく holdout実績で判定する。
    _oos_badge = _oos_weak_badge(s)
    # 選定基準月バッジ(proposalマージ時のみ)。同じ銘柄が複数基準で選ばれていても
    # どの基準月由来かを見分けられるようにする(例 「基 12/6」)。
    _sb = s.get("src_base") or _lss_src_base_of(
        s.get("symbol") or s.get("sym"), s.get("strategy") or s.get("strat"))
    _src_badge = (f'<span style="font-size:0.62rem;color:#38bdf8;font-weight:700;display:block;'
                  f'margin-top:1px" title="選定基準月">基&nbsp;{_sb}</span>') if _sb else ""
    if s.get("is_wf") and s.get("wf_score") is not None:
        rec = s.get("rec_score", "—")
        return (
            f'<span style="color:{col};font-weight:700">WF&nbsp;{s["wf_score"]}</span>'
            f'<span style="font-size:0.68rem;color:#64748b;display:block">{rank} / BT:{rec}</span>'
            f'{type_badge}{_tr_badge}{_oos_badge}{_src_badge}'
        )
    else:
        return (
            f'<span style="color:{col};font-weight:700">{rank}&nbsp;{s["score"]}</span>'
            f'<br><span style="font-size:0.68rem;color:#f59e0b">BT(参考)</span>'
            f'<br>{type_badge}{_tr_badge}{_oos_badge}{_src_badge}'
        )


def _factor_analysis_html(trades) -> str:
    """成績に効く要素を機械的にランキングするHTML(詳細分析タブ★効く要素)。
    各要素(BT/WFスコア・戦略・設定・損切幅・目標幅・保有日数・約定遅延・重複保有・
    決済理由)ごとに勝率/PF/平均損益/総損益を集計し、平均損益の加重RMS(判別力)で
    『成績を最も分離する要素』を上位に並べる。※in-sample探索用(OOSで裏取り前提)。"""
    import math as _m
    from collections import defaultdict as _dd
    rows = []
    for t in trades:
        if t.get("reason") in ("発注中", "保有中"):
            continue
        olp = t.get("order_limit") or 0
        osp = t.get("order_stop") or 0
        otp = t.get("order_target") or 0
        cfg = str(t.get("label", "") or "")
        rows.append(dict(
            strategy=t.get("strategy", "") or "?",
            holdout=(cfg.split("/")[0] if "/" in cfg else cfg) or "?",
            mode=("agg" if "/agg" in cfg else ("con" if "/con" in cfg else "?")),
            bt=float(t.get("rec_score") if t.get("rec_score") is not None else (t.get("score") or 0)),
            wf=(float(t.get("wf_score")) if t.get("wf_score") not in (None, "") else None),
            bt_type=t.get("bt_type", "") or "?",
            stop_pct=((osp - olp) / olp * 100 if olp else 0.0),
            target_pct=((otp - olp) / olp * 100 if olp else 0.0),
            hold=float(t.get("hold_days", 0) or 0),
            dtf=float(t.get("days_to_fill", 0) or 0),
            overlap=("再エントリー" if t.get("_overlap") else "通常"),
            reason=t.get("reason", "") or "?",
            pnl=float(t.get("pnl", 0) or 0),
        ))
    if len(rows) < 20:
        return '<p style="color:#64748b;padding:20px">取引が少なく要素分析できません。</p>'
    base_avg = sum(r["pnl"] for r in rows) / len(rows)

    def _st(g):
        n = len(g)
        p = [r["pnl"] for r in g]
        w = sum(1 for x in p if x > 0)
        gp = sum(x for x in p if x > 0); gl = abs(sum(x for x in p if x <= 0))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        return dict(n=n, wr=w / n * 100, pf=pf, avg=sum(p) / n, tot=sum(p))

    def _numbuck(key, edges, labels):
        o = _dd(list)
        for r in rows:
            v = r[key]
            if v is None:
                continue
            placed = False
            for i, e in enumerate(edges):
                if v < e:
                    o[labels[i]].append(r); placed = True; break
            if not placed:
                o[labels[-1]].append(r)
        return o

    def _catbuck(key):
        o = _dd(list)
        for r in rows:
            o[str(r[key])].append(r)
        return o

    factors = [
        ("BTスコア帯", _numbuck("bt", [30, 50, 60, 70, 80],
                               ["0-29", "30-49", "50-59", "60-69", "70-79", "80+"])),
        ("WFスコア帯", _numbuck("wf", [30, 50, 70, 90],
                               ["<30", "30-49", "50-69", "70-89", "90+"])),
        ("損切り幅(絶対%)", _numbuck("stop_pct", [-15, -10, -7, -5, -3, 0],
                               ["<-15", "-15~-10", "-10~-7", "-7~-5", "-5~-3", "-3~0", ">0"])),
        ("目標幅(%)", _numbuck("target_pct", [5, 8, 12, 18], ["<5", "5-8", "8-12", "12-18", "18+"])),
        ("保有日数", _numbuck("hold", [1, 3, 5, 8, 12], ["0", "1-2", "3-4", "5-7", "8-11", "12+"])),
        ("約定遅延(日)", _numbuck("dtf", [1, 2], ["0(当日)", "1", "2+"])),
        ("戦略", _catbuck("strategy")),
        ("モード(con/agg)", _catbuck("mode")),
        ("ホールドアウト設定", _catbuck("holdout")),
        ("BTタイプ", _catbuck("bt_type")),
        ("重複保有", _catbuck("overlap")),
        ("決済理由", _catbuck("reason")),
    ]
    MIN_C = 15
    tables = []
    ranking = []
    for title, groups in factors:
        valid = {k: _st(v) for k, v in groups.items() if len(v) >= MIN_C}
        if len(valid) < 2:
            continue
        tot_n = sum(s["n"] for s in valid.values())
        spread = sum(s["n"] / tot_n * (s["avg"] - base_avg) ** 2 for s in valid.values())
        rms = _m.sqrt(spread)
        # 決済理由/保有日数は『結果指標(循環)』= エントリー時に選べない。別枠に分ける。
        _is_circ = title in ("決済理由", "保有日数")
        ranking.append((rms, title, _is_circ))
        body = ""
        for k in sorted(valid, key=lambda x: -valid[x]["avg"]):
            s = valid[k]
            pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            wrc = "#4ade80" if s["wr"] >= 55 else ("#fbbf24" if s["wr"] >= 45 else "#f87171")
            ac = "#4ade80" if s["avg"] >= 0 else "#f87171"
            body += (f'<tr><td style="text-align:left">{k}</td>'
                     f'<td style="text-align:right">{s["n"]}</td>'
                     f'<td style="text-align:right;color:{wrc}">{s["wr"]:.1f}%</td>'
                     f'<td style="text-align:right">{pf_s}</td>'
                     f'<td style="text-align:right;color:{ac};font-weight:700">{s["avg"]:+,.0f}円</td>'
                     f'<td style="text-align:right;color:{ac}">{s["tot"]:+,.0f}円</td></tr>')
        tables.append(
            f'<details style="margin:6px 0"><summary style="cursor:pointer;color:#93c5fd;'
            f'font-weight:700;padding:3px 0">{title}（判別力 {rms:,.0f}円）</summary>'
            f'<table style="max-width:560px"><thead><tr><th style="text-align:left">グループ</th>'
            f'<th>件数</th><th>勝率</th><th>PF</th><th>平均損益</th><th>総損益</th></tr></thead>'
            f'<tbody>{body}</tbody></table></details>')
    ranking.sort(reverse=True)
    _act = [r for r in ranking if not r[2]]   # エントリー時に選べる=本当に効く要素
    _cir = [r for r in ranking if r[2]]        # 結果指標(循環)
    _max = _act[0][0] if _act else 1

    def _rrows(items, accent):
        html = ""
        for i, (rms, title, _c) in enumerate(items, 1):
            w = max(2, int(rms / _max * 100))
            html += (f'<tr><td style="text-align:right;color:#64748b">{i}</td>'
                     f'<td style="text-align:left;font-weight:700">{title}</td>'
                     f'<td style="text-align:right;color:{accent};font-weight:700">{rms:,.0f}円</td>'
                     f'<td style="width:220px"><div style="height:12px;width:{w}%;'
                     f'background:{accent};border-radius:3px;opacity:.85"></div></td></tr>')
        return html
    return (
        '<h2>★ 成績に効く要素ランキング</h2>'
        '<p style="color:#64748b;font-size:0.82rem;margin-bottom:8px">'
        '各要素でグループ分けし、平均損益が全体からどれだけ離れるか(加重RMS=判別力)で並べています。'
        '上位ほど「その分け方で成績が大きく変わる=効く要素」。'
        '<b style="color:#fbbf24">※ in-sample を含む探索用。実運用判断は'
        '★ロールフォワードOOS/損益タブで裏取りしてください。</b></p>'
        '<h3 style="color:#4ade80;margin:8px 0 4px">◎ エントリー時に選べる要素（=本当に効く）</h3>'
        f'<table style="max-width:640px"><thead><tr><th>#</th>'
        '<th style="text-align:left">要素</th><th>判別力</th><th style="text-align:left">効き具合</th>'
        f'</tr></thead><tbody>{_rrows(_act, "#38bdf8")}</tbody></table>'
        '<h3 style="color:#94a3b8;margin:14px 0 4px">△ 結果指標（循環・選定には使えない）</h3>'
        '<p style="color:#64748b;font-size:0.78rem;margin:0 0 4px">'
        '決済理由(損切り=定義上マイナス等)や保有日数(早期決済=勝ちが多い)は、'
        '<b>結果を後から見た値</b>なので判別力が高くて当然。エントリー時には選べないため'
        '銘柄選定の基準には使えません。</p>'
        f'<table style="max-width:640px"><tbody>{_rrows(_cir, "#64748b")}</tbody></table>'
        '<h3 style="color:#93c5fd;margin:14px 0 4px">要素ごとのグループ別成績（クリックで展開）</h3>'
        + "".join(tables))


def _calc_strat_priority(wr: float, pf: float, avgh: float) -> tuple[float, str, str]:
    """戦略の総合優先度スコア(0〜100)とランク★・色を返す(戦略別サマリーと共通)。
    勝率40点 + PF40点(PF10でキャップ,∞=10) + 速度20点(平均保有15日で0)。"""
    pf_c = 10.0 if pf == float("inf") else min(pf, 10.0)
    wr_pts    = wr * 0.4
    pf_pts    = pf_c / 10.0 * 40.0
    speed_pts = max(0.0, 1.0 - avgh / 15.0) * 20.0
    sc = round(wr_pts + pf_pts + speed_pts, 1)
    if sc >= 80:   return sc, "★★★", "#4ade80"
    if sc >= 60:   return sc, "★★",  "#fbbf24"
    if sc >= 40:   return sc, "★",   "#93c5fd"
    return sc, "△", "#94a3b8"


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

    _maxwin_sig = _holdout_window_map()   # (sym,strat)→最長holdout窓(⚠OOS弱バッジ用)

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
            _fz = _lookup_frozen_bt(sym, strat)
            if _fz is not None:
                rec_score = _fz
                rec_rank  = ("★★★" if _fz >= 80 else "★★" if _fz >= 60
                             else "★" if _fz >= 40 else "△")
            _bt_type_fn = getattr(_stop, "calc_bt_type", None)
            bt_type = _bt_type_fn(bt["period_results"]) if _bt_type_fn else "?"
            # BTスコアの計算元になった実取引数(180日=最長窓、重複なし)
            _pr_vals = [r for r in bt["period_results"].values()
                        if r and r.get("trades", 0) > 0]
            bt_trades = max((r["trades"] for r in _pr_vals), default=0)
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

            # ── lss タブ: 実際の注文内容(逆指値空売り・同日決済)に合わせた表示値 ──
            # check_signal_on_date はロング値(損切=下/目標=上/em=0)を返すので、
            # そこから ATR を逆算して lss の 損切=上 / 目標=下 / 指値下限(-3%) を作る。
            # 注文数量(qty)は long stop から算出するので stop_p/target_p は温存する。
            _lss_stop = _lss_target = _lss_hold = _lss_exit = _lss_sigprice = None
            _lss_limit = limit_p
            if _LSS_ORDER_MODE and _LSS_LONG and order_p:
                # ロングデイトレ(逆指値買い=上ブレイク・同日決済)。check_signal_on_date が返す
                # ロング値(order_p=前日終値+atr*em[em=0→前日終値] / 損切=下 / 目標=上)は
                # そのまま正しい。トリガーだけ「前日終値+1ティック」に置く(逆指値買いは現在値
                # 以下だと即約定で弾かれるため)。指値上限=+3%。損切=下/目標=上は反転しない
                # (_lss_stop/_lss_target=None のまま=ロング値 stop_p/target_p を表示)。
                try:
                    from backtest_limit_entry import (round_to_tick as _r2t,
                                                      tick_size as _tsz)
                    _lss_sigprice = order_p                         # シグナル日時株価=前日終値
                    order_p = _r2t(order_p + _tsz(order_p))         # トリガー=前日終値+1ティック(約定用)
                    _lss_limit = _r2t(order_p * (1.0 + 0.03))       # 発動後の指値上限(トリガー+3%)
                    _lss_hold  = "同日"                             # 同日決済(max_hold=0)
                    try:
                        _lss_exit = _pd.bdate_range(start=_pd.to_datetime(sig_dt),
                                                    periods=2)[-1].date()
                    except Exception:
                        _lss_exit = _max_exit
                except Exception:
                    pass
            elif _LSS_ORDER_MODE and order_p:
                try:
                    from backtest_limit_entry import (round_to_tick as _r2t,
                                                      tick_size as _tsz,
                                                      ceil_to_tick as _c2t)
                    _plp = mod.STRATEGY_PARAMS.get(strat)
                    _sm_long = float(_plp[2]) if _plp else 0.0
                    _stop_long = float(sig.get("stop_price", 0) or 0)
                    _atr = (order_p - _stop_long) / _sm_long if (_sm_long and _stop_long) else 0.0
                    # order_p は round_to_tick 済み = 前日終値(呼値に合った値)。
                    # 逆指値売りは現在値(引け後=前日終値)以上のトリガーだと即約定で弾かれる
                    # (kabu Code 100217)。トリガーだけ「前日終値-1ティック」に置く。
                    # 損切/利確のリスク幅は前日終値基準(§18検証モデル)で測り、呼値グリッド上は
                    # ライン直上ティック(ceil)。-1tickは約定用の小細工なので損切りには反映しない
                    # (不必要にタイトにしない)。例:応用地質 トリガー2,825 / 損切2,835 / 利確2,785。
                    _lss_sigprice = order_p                        # シグナル日時株価=前日終値
                    _close_ref = order_p                           # 損切/利確の基準=前日終値
                    order_p = _r2t(order_p - _tsz(order_p))        # トリガー=前日終値-1ティック(約定用のみ)
                    if _atr > 0:
                        _lss_stop   = _c2t(_close_ref + _atr * _LSS_SM)   # 損切=前日終値+atr*sm(上)
                        _lss_target = _c2t(_close_ref - _atr * _LSS_TM)   # 目標=前日終値-atr*tm(下)
                    _lss_limit = _r2t(order_p * (1.0 - 0.03))          # 発動後の指値下限(トリガー基準-3%)
                    _lss_hold  = "同日"                          # 同日決済(max_hold=0)
                    try:  # 決済日 = 約定日(=シグナル翌営業日)。同日引けで決済
                        _lss_exit = _pd.bdate_range(start=_pd.to_datetime(sig_dt),
                                                    periods=2)[-1].date()
                    except Exception:
                        _lss_exit = _max_exit
                except Exception:
                    pass
            # 純OOS成績: その銘柄が選定された最長holdout窓(選定に使っていない
            # 直近除外期間)のトレードだけを集計。⚠OOS弱バッジの根拠。
            _oos_pnl = 0.0
            _oos_n = 0
            _oos_win = _maxwin_sig.get((sym, strat), 0)
            if _oos_win:
                _oos_cut = _TODAY - timedelta(days=_oos_win)
                for _tt in trade_log:
                    _tsd = _tt.get("signal_dt")
                    _tsd = _tsd.date() if hasattr(_tsd, "date") else _tsd
                    if (_tsd and _tsd >= _oos_cut
                            and _tt.get("reason") not in ("発注中", "保有中")):
                        _oos_n += 1
                        _oos_pnl += _tt.get("pnl", 0)
            return {
                "_bt": bt_info,
                "_sig": {
                    "symbol": sym, "name": name, "strategy": strat,
                    "score": score, "rank": rank, "is_wf": is_wf,
                    "src_base": _lss_src_base_of(sym, strat),
                    "wf_score": wf_score, "wf_rank_str": wf_rank_str,
                    "rec_score": rec_score, "bt_type": bt_type,
                    "bt_trades":    bt_trades,
                    "oos_pnl":      _oos_pnl,
                    "oos_n":        _oos_n,
                    "oos_win":      _oos_win,
                    "signal_date":  sig_dt,
                    # lss の「シグナル日時株価」は前日終値(呼値に合った値)。トリガーは
                    # その前日終値-1ティック(即約定回避=実発注値)なので、両者は1ティック差になる。
                    "signal_price": (_lss_sigprice if (_LSS_ORDER_MODE and _lss_sigprice) else sig.get("signal_price", 0)),
                    "order_p":      order_p,
                    "limit_p":      _lss_limit if _LSS_ORDER_MODE else limit_p,
                    "stop_p":       sig.get("stop_price",  0),
                    "target_p":     sig.get("target_price", 0),
                    "is_lss":       _LSS_ORDER_MODE,
                    "lss_stop":     _lss_stop,      # lss 損切=上 (None ならロング値表示)
                    "lss_target":   _lss_target,    # lss 目標=下
                    "max_hold":     (_lss_hold if _LSS_ORDER_MODE else _MH),
                    "max_exit":     (_lss_exit if _LSS_ORDER_MODE else _max_exit),
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

    # 流動性(平均日次売買代金=出来高×終値)を各シグナルに付与。
    # 検証で「厚い銘柄ほど1件あたり利益が大きい」と分かったため、BT同点時は厚い方を優先。
    try:
        from backtest_limit_entry import fetch as _fq
        for _s in signals:
            try:
                _dfq = _fq(_s["symbol"], 200)
                if _dfq is not None and not _dfq.empty:
                    _cc = "close" if "close" in _dfq.columns else "Close"
                    _vv = "volume" if "volume" in _dfq.columns else "Volume"
                    _s["liquidity"] = float((_dfq[_vv] * _dfq[_cc]).tail(120).mean())
                else:
                    _s["liquidity"] = 0.0
            except Exception:
                _s["liquidity"] = 0.0
    except Exception:
        for _s in signals:
            _s["liquidity"] = 0.0

    # 主: BTスコア降順 / 副: 売買代金降順(BTが同点なら流動性の高い銘柄を上に)
    signals.sort(key=lambda x: (-(x.get("rec_score") or 0), -(x.get("liquidity") or 0)))
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
        gross_win  = sum(t["pnl"] for t in win_ts)
        gross_loss = abs(sum(t["pnl"] for t in loss_ts))
        items = [
            ("取引数",       f'{st["n"]}件',                            "#e2e8f0"),
            ("勝率",         f'{wr:.1f}%',                              wrc),
            ("PF",           pf_s,                                      pfc),
            ("利益計",       f'+{gross_win:,.0f}円' if gross_win else "—",  "#4ade80"),
            ("損失計",       f'-{gross_loss:,.0f}円' if gross_loss else "—","#f87171"),
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

    # ── 戦略別 優先度サマリー(BT70以上): シグナルタブ上部に表示 ──
    # 損益タブの戦略別サマリーと同じ優先度(勝率+PF+速度)を、今日のシグナル判断用に
    # シグナルタブにも載せる。all_trade_infos(全銘柄×config)をBT70で絞り集計。
    from collections import defaultdict as _dd_sp
    _g_sp: dict = _dd_sp(list)
    _seen_sp: set = set()
    for info in all_trade_infos:
        if info.get("score", 0) < 70:
            continue
        for t in info["trades"]:
            _k = (info["sym"], info["strat"], t.get("signal_dt"))
            if _k in _seen_sp:
                continue
            _seen_sp.add(_k)
            _g_sp[info["strat"]].append(t)
    _sp_rows = []
    for _strat, _tr in _g_sp.items():
        _n = len(_tr)
        if _n == 0:
            continue
        _wins = sum(1 for t in _tr if t["pnl"] > 0)
        _gp = sum(t["pnl"] for t in _tr if t["pnl"] > 0)
        _gl = abs(sum(t["pnl"] for t in _tr if t["pnl"] < 0))
        _pnl = _gp - _gl
        _pf = _gp / _gl if _gl > 0 else (float("inf") if _gp > 0 else 0.0)
        _wr = _wins / _n * 100
        _avgh = sum(t.get("hold_days", 0) for t in _tr) / _n
        _sc, _rk, _col = _calc_strat_priority(_wr, _pf, _avgh)
        _sp_rows.append((_sc, _rk, _col, _strat, _n, _wr, _pf, _avgh, _pnl))
    _sp_rows.sort(key=lambda x: -x[0])
    _strat_pri_html = ""
    if _sp_rows:
        _sp_body = ""
        for _sc, _rk, _col, _strat, _n, _wr, _pf, _avgh, _pnl in _sp_rows:
            _pf_s = "∞" if _pf == float("inf") else f"{_pf:.2f}"
            _pnl_c = "#4ade80" if _pnl >= 0 else "#f87171"
            _sp_body += (f'<tr><td style="text-align:left;font-weight:700">{_strat}</td>'
                         f'<td style="text-align:center;color:{_col};font-weight:700;white-space:nowrap">{_rk}'
                         f'<br><span style="font-size:0.72rem">{_sc:.0f}</span></td>'
                         f'<td style="text-align:right">{_n}</td>'
                         f'<td style="text-align:right">{_wr:.1f}%</td>'
                         f'<td style="text-align:right">{_pf_s}</td>'
                         f'<td style="text-align:right">{_avgh:.1f}日</td>'
                         f'<td style="text-align:right;color:{_pnl_c};font-weight:700">{_pnl:+,.0f}円</td></tr>')
        _strat_pri_html = (
            '<h2>戦略別 優先度（BT70以上）</h2>'
            '<p style="color:#64748b;font-size:0.8rem;margin-bottom:8px">'
            '優先度 = 勝率+PF+速度の総合スコア(★★★≥80/★★≥60/★≥40)。'
            '今日どの戦略のシグナルを優先するかの目安。</p>'
            '<table style="max-width:680px"><thead><tr>'
            '<th style="text-align:left">戦略</th>'
            '<th>優先度<br><small style="color:#64748b">勝率+PF+速度</small></th>'
            '<th>取引数</th><th>勝率</th><th>PF</th><th>平均保有</th><th>損益合計</th>'
            '</tr></thead><tbody>' + _sp_body + '</tbody></table>')
    # スコア帯別テーブルは普段見ないので折りたたむ(details)
    if score_section:
        score_section = (
            '<details style="margin:10px 0"><summary style="cursor:pointer;color:#93c5fd;'
            'font-weight:700;padding:4px 0;font-size:0.95rem">▸ スコア帯別 バックテスト勝率'
            '（クリックで展開）</summary>' + score_section + '</details>')
    # 戦略別優先度を上、スコア帯別(折りたたみ)を下に
    score_section = _strat_pri_html + score_section

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
    # lss: 同一銘柄は全戦略でトリガー/損切/目標が同一(=同じ1トレード)。発注リストを
    # 1銘柄1行にデデュープし、数量 = 一致した戦略数 × 基本株数 にする(2戦略一致→200株)。
    # 200株を1注文=1建玉で出せて確実。別々の注文が発生しないので取消・統合も不要。
    # watcherは建玉の実保有数量で損切り・決済する。
    if _LSS_ORDER_MODE and signals:
        _dd: dict = {}
        for _s in signals:
            _k = str(_s["symbol"]).split(".")[0].upper()
            _st = str(_s.get("strategy", ""))
            if _k not in _dd:
                _r = dict(_s); _r["_agree_strats"] = {_st}; _dd[_k] = _r
            else:
                _r = _dd[_k]; _sset = _r["_agree_strats"]; _sset.add(_st)
                # 代表は BT(rec_score)最大の行に差し替え(一致情報は保持)
                if (_s.get("rec_score") or 0) > (_r.get("rec_score") or 0):
                    _r = dict(_s); _r["_agree_strats"] = _sset; _dd[_k] = _r
        signals = sorted(_dd.values(), key=lambda x: -(x.get("rec_score") or 0))

    rows = ""
    _cum_cap = 0   # 予算表示用: BT降順の累計必要額(この行まで全部発注したら合計いくら)
    for i, s in enumerate(signals, 1):
        col      = col_map.get(s["rank"], "#94a3b8")
        stop_pct = (s["order_p"] - s["stop_p"])  / s["order_p"] * 100 if s["order_p"] else 0
        tgt_pct  = (s["target_p"] - s["order_p"]) / s["order_p"] * 100 if s["order_p"] else 0
        _agree   = len(s.get("_agree_strats", ())) or 1   # 一致した戦略数(lssのみ>1あり)
        qty      = (_calc_qty(s["order_p"], s["stop_p"]) if s["order_p"] else 0) * _agree
        _agree_badge = (f' <span style="font-size:0.62rem;color:#a78bfa;font-weight:700">'
                        f'{_agree}戦略一致</span>' if _agree > 1 else "")
        pos_val  = round(s["order_p"] * qty)
        _cum_cap += pos_val
        # 損切/目標の表示セル。lss は実際の注文(空売り)に合わせて 損切=上(+%)/目標=下(-%)。
        _is_lss_row = bool(s.get("is_lss"))
        if _is_lss_row and s.get("lss_stop") and s.get("lss_target"):
            _op = s["order_p"]; _ls = s["lss_stop"]; _lt = s["lss_target"]
            _stop_up = (_ls - _op) / _op * 100 if _op else 0   # 価格上昇で損切
            _tgt_dn  = (_op - _lt) / _op * 100 if _op else 0   # 価格下落で利確
            _stop_cell = f'+{_stop_up:.1f}%<br><span style="font-size:0.72rem">{_ls:,.0f}円</span>'
            _tgt_cell  = f'-{_tgt_dn:.1f}%<br><span style="font-size:0.72rem">{_lt:,.0f}円</span>'
        else:
            _stop_cell = f'-{stop_pct:.1f}%<br><span style="font-size:0.72rem">{s["stop_p"]:,.0f}円</span>'
            _tgt_cell  = f'+{tgt_pct:.1f}%<br><span style="font-size:0.72rem">{s["target_p"]:,.0f}円</span>'
        _hold_cell = "同日" if _is_lss_row else f'{s.get("max_hold","—")}日'
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
        # lss タブ(_LSS_ORDER_MODE)は『信用新規売りの逆指値』として side=short で送る。
        # lss は同日決済。利確(下)・損切(上)は placed_orders に記録され、日中は
        # lss_exit_watcher.py が価格監視して先着で成行決済する(order_server 側は
        # lss には自動利確を置かない=ポーリングと二重にしない)。
        _side   = ("long" if _LSS_LONG
                   else ("short" if (str(s["strategy"]).upper().endswith("_S") or _LSS_ORDER_MODE)
                         else "long"))
        # lss は損切=上/利確=下(空売り)。発注ログ(placed_orders)に実際の値を記録する。
        _ord_target = (s.get("lss_target") or s['target_p']) if _is_lss_row else s['target_p']
        _ord_stop   = (s.get("lss_stop") or s['stop_p']) if _is_lss_row else s['stop_p']
        _scode  = str(s["symbol"]).split(".")[0]
        _reg_url = (f"http://127.0.0.1:8765/?prefill=1"
                    f"&symbol={_scode}"
                    f"&entry={s['order_p']:.0f}"
                    f"&stop={_ord_stop:.0f}"
                    f"&target={_ord_target:.0f}"
                    f"&strategy={s['strategy']}"
                    f"&qty={qty}"
                    f"&side={_side}"
                    f"&bt={s.get('rec_score', '') or ''}")
        # 🚀発注: このタブから fetch で /order に発注リクエスト（確認ダイアログ付き）
        # 第1引数に this(=ボタン) を渡し、成功時に行へ✓を付けて発注済み累計に想定額を足す。
        _ord_btn = (f"<button type=\"button\" "
                    f"onclick=\"gobotOrder(this,'{_scode}','{_side}','{s['strategy']}',"
                    f"{s['order_p']:.0f},{_ord_stop:.0f},{_ord_target:.0f},{qty},"
                    f"'{s.get('rec_score', '') or ''}',{pos_val})\" "
                    f"style=\"display:inline-block;padding:4px 8px;background:#dc2626;"
                    f"color:#fff;border:none;border-radius:5px;font-size:12px;cursor:pointer;"
                    f"white-space:nowrap;margin-bottom:3px\">🚀 発注</button>")
        # 発注株数を100株単位で変更できる入力欄。既定=計算株数(100×戦略一致数)。減らせば分散発注。
        # gobotOrder がボタン隣接の .gobotqty を読んで qty/想定額を上書きする(id不要=衝突回避)。
        _qty_input = (f'<input type="number" class="gobotqty" value="{qty}" step="100" min="100" '
                      f'max="{qty}" title="発注株数(100株単位で変更可)" '
                      f'style="width:54px;padding:2px 4px;margin-bottom:2px;border:1px solid #475569;'
                      f'border-radius:4px;background:#0f172a;color:#e2e8f0;font-size:12px;'
                      f'text-align:right">')
        _ord_btn = _qty_input + _ord_btn
        _reg_link = (f'<a href="{_reg_url}" target="_blank" '
                     f'style="display:inline-block;padding:4px 8px;background:#2d6cdf;'
                     f'color:#fff;border-radius:5px;font-size:12px;text-decoration:none;'
                     f'white-space:nowrap">📥 登録</a>')
        if _ANALYSIS_ONLY and (not _LSS_ORDER_MODE or _LSS_LONG):
            # mirror(却下済): 発注/登録ボタンはロング逆指値買いの値を送ってしまう(=誤発注)。
            # ロングデイトレ(_LSS_LONG): 値は正しいロング逆指値買いだが、同日決済の自動決済
            # (ロング用watcher)が未整備なので、当面は分析専用として発注を無効化する。
            # どちらも分析専用なので無効化し、注意書きに置き換える。
            _reg_btn = ('<div style="text-align:center;color:#f87171;font-size:0.7rem;'
                        'line-height:1.3">🚫 発注不可<br><span style="color:#94a3b8">分析専用'
                        + ('<br>(ロングデイトレ)' if _LSS_LONG else '<br>(値はロング)')
                        + '</span></div>')
        else:
            _ord100_btn = (
                f'<span><button type="button" '
                f'onclick="gobotOrder(this,\'{_scode}\',\'{_side}\',\'{s["strategy"]}\','
                f'{s["order_p"]:.0f},{_ord_stop:.0f},{_ord_target:.0f},100,'
                f'\'{s.get("rec_score", "") or ""}\',{round(100 * s["order_p"])})" '
                f'style="display:inline-block;padding:4px 8px;background:#b45309;'
                f'color:#fff;border:none;border-radius:5px;font-size:12px;cursor:pointer;'
                f'white-space:nowrap">100株 発注</button></span>'
            )
            _reg_btn = (f'<div style="display:flex;flex-direction:column;gap:2px;'
                        f'align-items:center">{_ord_btn}{_ord100_btn}{_reg_link}</div>')
        _liq_v = float(s.get("liquidity", 0) or 0)
        _liq_oku = _liq_v / 1e8
        if _liq_v <= 0:
            _liq_cell = '<span style="color:#64748b">—</span>'
        elif _liq_oku < 3:
            _liq_cell = (f'<span style="color:#f87171;font-weight:700">{_liq_oku:,.0f}億</span>'
                         f'<br><span style="font-size:0.64rem;color:#f87171">⚠低流動</span>')
        elif _liq_oku >= 30:
            _liq_cell = (f'<span style="color:#4ade80;font-weight:700">{_liq_oku:,.0f}億</span>'
                         f'<br><span style="font-size:0.64rem;color:#4ade80">厚</span>')
        else:
            _liq_cell = f'<span style="color:#cbd5e1">{_liq_oku:,.0f}億</span>'
        rows += f"""<tr class="sigrow" data-posval="{pos_val}" data-cum="{_cum_cap}">
  <td style="text-align:center;font-weight:700">{i}</td>
  <td class="sym" style="text-align:left">{s["symbol"]}<br>
    <span style="color:#64748b;font-size:0.75rem">{s["name"]}</span>{earn_html}<br>
    <span style="display:inline-flex;flex-wrap:wrap;gap:2px;margin-top:3px">{src_html}{risk_html}</span></td>
  <td style="text-align:center">{tag}</td>
  <td style="text-align:center">{ _fmt_score_cell(s, col) }</td>
  <td style="text-align:right;font-size:0.78rem">{_liq_cell}</td>
  <td style="text-align:right;color:#94a3b8">{s.get("signal_date","")}<br><span style="font-size:0.72rem">{s.get("signal_price",0):,.0f}円</span></td>
  <td style="text-align:right;color:#38bdf8;font-weight:700">{s["order_p"]:,.0f}円</td>
  <td style="text-align:right;color:#f59e0b">{lim_pct:+.1f}%<br><span style="font-size:0.72rem">{s["limit_p"]:,.0f}円</span></td>
  <td style="text-align:right;color:#f87171">{_stop_cell}</td>
  <td style="text-align:right;color:#4ade80">{_tgt_cell}</td>
  <td style="text-align:right;color:#e2e8f0">{qty}株{_agree_badge}<br><span style="font-size:0.72rem;color:#94a3b8">{pos_val:,.0f}円</span>
    <br><span style="font-size:0.68rem;color:#64748b">累計 {_cum_cap:,.0f}円</span></td>
  <td style="text-align:center;color:#94a3b8">{_hold_cell}</td>
  <td style="text-align:center;color:#f59e0b">{max_exit}</td>
  <td style="text-align:center">{_reg_btn}</td>
</tr>"""

    min_note = f"（スコア{min_score}点以上のみ）" if min_score > 0 else ""
    _order_js = """
<script>
var gobotOrderedTotal = 0;   // 発注済み合計額(円)
var gobotOrderedCount = 0;   // 発注済み件数
function gobotYen(n){ return Math.round(n).toLocaleString('ja-JP'); }

// 予算入力 → BT降順で累計し、予算内までを緑ハイライト。上位N銘柄/合計/残を表示。
function gobotApplyBudget(){
  var el = document.getElementById('gobotBudget');
  if(!el) return;
  var budget = parseFloat((el.value||'').replace(/[^0-9.]/g,'')) || 0;
  var rows = document.querySelectorAll('tr.sigrow');
  var inN = 0, inSum = 0, lastInRow = null;
  rows.forEach(function(r){
    var cum = parseFloat(r.getAttribute('data-cum')) || 0;
    var pv  = parseFloat(r.getAttribute('data-posval')) || 0;
    r.classList.remove('inbudget','budgetline');
    if(budget > 0 && cum <= budget){
      r.classList.add('inbudget');
      inN += 1; inSum += pv; lastInRow = r;
    }
  });
  if(lastInRow) lastInRow.classList.add('budgetline');
  var box = document.getElementById('gobotBudgetInfo');
  if(box){
    if(budget <= 0){ box.innerHTML = '予算を入力すると、上位から何銘柄まで発注できるかを緑で表示します。'; }
    else {
      box.innerHTML = '予算 <b>\\u00a5'+gobotYen(budget)+'</b> \\u2192 上位 <b style="color:#4ade80">'
        + inN + '銘柄</b> まで発注可 / 合計必要額 <b>\\u00a5'+gobotYen(inSum)+'</b>'
        + ' / 予算残 <b>\\u00a5'+gobotYen(budget-inSum)+'</b>';
    }
  }
  gobotUpdateOrdered();
}

function gobotUpdateOrdered(){
  var el = document.getElementById('gobotBudget');
  var budget = el ? (parseFloat((el.value||'').replace(/[^0-9.]/g,'')) || 0) : 0;
  var box = document.getElementById('gobotOrderedInfo');
  if(!box) return;
  var rem = budget > 0 ? (' / 予算残 <b>\\u00a5'+gobotYen(budget-gobotOrderedTotal)+'</b>') : '';
  box.innerHTML = '発注済み <b style="color:#f87171">'+gobotOrderedCount+'件</b>'
    + ' 合計 <b>\\u00a5'+gobotYen(gobotOrderedTotal)+'</b>' + rem;
}
function gobotResetOrdered(){
  gobotOrderedTotal = 0; gobotOrderedCount = 0;
  document.querySelectorAll('tr.sigrow.ordered').forEach(function(r){ r.classList.remove('ordered'); });
  document.querySelectorAll('.gobot-ordermark').forEach(function(m){ m.remove(); });
  gobotUpdateOrdered();
}

function gobotOrder(btn, sym, side, strat, entry, stop, target, qty, bt, posval){
  // ボタン隣接の株数入力(.gobotqty)があればその値で発注(100株単位で減らせる)。想定額も再計算。
  var qin = btn ? (btn.parentNode && btn.parentNode.querySelector('.gobotqty')) : null;
  if(qin){ var qv = parseInt(qin.value,10); if(qv>0){ qty = qv; posval = Math.round(entry*qv); } }
  var lbl = (side==='short') ? ('逆指値売り(信用新規) @\\u2264'+entry) : ('逆指値買い @\\u2265'+entry);
  if(!confirm('\\u3010\\u767a\\u6ce8\\u78ba\\u8a8d\\u3011\\n'+sym+' '+strat+' ('+side+')\\n'+lbl
      +'\\n\\u682a\\u6570: '+qty+'\\u682a\\n\\n\\u767a\\u6ce8\\u30b5\\u30fc\\u30d0(order_server:8765)\\u3078\\u767a\\u6ce8\\u30ea\\u30af\\u30a8\\u30b9\\u30c8\\u3092\\u9001\\u308a\\u307e\\u3059\\u3002'
      +'\\n\\u5b9f\\u767a\\u6ce8/dry-run\\u30fb\\u30c7\\u30e2/\\u672c\\u756a\\u306f\\u30b5\\u30fc\\u30d0\\u8d77\\u52d5\\u30aa\\u30d7\\u30b7\\u30e7\\u30f3\\u306b\\u5f93\\u3044\\u307e\\u3059\\u3002\\n\\n\\u3088\\u308d\\u3057\\u3044\\u3067\\u3059\\u304b\\uff1f')) return;
  var body = new URLSearchParams({symbol:sym,entry:entry,stop:stop,target:target,
                                  strategy:strat,side:side,qty:qty,bt:(bt||'')});
  fetch('http://127.0.0.1:8765/order',{method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})
    .then(function(r){return r.text();})
    .then(function(t){
      // 発注済みとして記録(想定額を累計に加算・行に✓)。実際に発注できた(🚀発注完了)か
      // dry-runプレビューのみ計上。⏭スキップ(売建規制/デイトレ非対応)・⚠エラー・中止は計上しない。
      var ok = /\\u767a\\u6ce8\\u5b8c\\u4e86|dry-run/.test(t);
      if(ok){
        gobotOrderedTotal += (parseFloat(posval)||0);
        gobotOrderedCount += 1;
        var row = btn ? btn.closest('tr') : null;
        if(row && !row.classList.contains('ordered')){
          row.classList.add('ordered');
          var mark = document.createElement('span');
          mark.className = 'gobot-ordermark';
          mark.textContent = ' \\u2713発注済';
          mark.style.cssText = 'color:#f87171;font-size:0.7rem;font-weight:700;display:block';
          btn.parentNode.appendChild(mark);
        }
        gobotUpdateOrdered();
      }
      alert(t);
    })
    .catch(function(e){alert('\\u767a\\u6ce8\\u5931\\u6557: \\u767a\\u6ce8\\u30b5\\u30fc\\u30d0\\u3092\\u8d77\\u52d5\\u3057\\u3066\\u304f\\u3060\\u3055\\u3044\\n  python order_server.py\\n'+e);});
}
document.addEventListener('DOMContentLoaded', function(){
  var el = document.getElementById('gobotBudget');
  if(el){ el.addEventListener('input', gobotApplyBudget); gobotApplyBudget(); }
});

</script>
<style>
tr.sigrow.inbudget > td { background: rgba(34,197,94,0.10); }
tr.sigrow.budgetline > td { border-bottom: 2px solid #22c55e; }
tr.sigrow.ordered > td { background: rgba(220,38,38,0.14); }
#gobotBudgetBar input { width:150px;padding:6px 8px;border-radius:6px;border:1px solid #475569;
  background:#0f172a;color:#e2e8f0;font-size:0.95rem;text-align:right; }
#gobotBudgetBar button { padding:6px 10px;border-radius:6px;border:1px solid #475569;
  background:#1e293b;color:#cbd5e1;font-size:0.82rem;cursor:pointer; }
</style>
"""
    # 予算バー: 予算を入れると予算内までを緑表示 / 発注ボタンで発注済み累計を集計
    _budget_bar = (
        '<div id="gobotBudgetBar" style="margin:10px 0;padding:12px 16px;background:#0b1220;'
        'border:1px solid #334155;border-radius:8px;display:flex;flex-wrap:wrap;gap:14px;'
        'align-items:center">'
        '<label style="color:#93c5fd;font-weight:700;font-size:0.9rem">💰 予算(円): '
        '<input id="gobotBudget" type="text" inputmode="numeric" placeholder="例: 3000000"></label>'
        '<button type="button" onclick="gobotApplyBudget()">反映</button>'
        '<span id="gobotBudgetInfo" style="color:#cbd5e1;font-size:0.86rem">'
        '予算を入力すると、上位から何銘柄まで発注できるかを緑で表示します。</span>'
        '<span style="flex-basis:100%;height:0"></span>'
        '<span id="gobotOrderedInfo" style="color:#cbd5e1;font-size:0.86rem">'
        '発注済み <b style="color:#f87171">0件</b> 合計 <b>¥0</b></span>'
        '<button type="button" onclick="gobotResetOrdered()">発注済みをリセット</button>'
        '</div>')
    if _LSS_ORDER_MODE and _LSS_LONG:
        # ロングデイトレ タブ: 逆指値買い(上ブレイク)・同日決済。発注は当面 分析専用。
        _analysis_warn = (
            '<div style="margin:10px 0;padding:12px 16px;background:#14312b;border:1px solid #34d399;'
            'border-radius:8px;color:#a7f3d0;font-size:0.86rem;line-height:1.6">'
            '📈 <b>このタブは ロングデイトレ(逆指値買い=上ブレイク・同日決済)です。</b><br>'
            'トリガー=<b>前日終値+1ティック</b>(以上で発動 / 発動後 +3%上限指値)、'
            '<b>損切り=下 / 目標=上</b>(5分足OCOの基準値)。約定したら<b>その日の引けに売り</b>で決済します。<br>'
            '⚠ 同日決済の自動決済(ロング用watcher)が未整備のため、'
            '<b>このタブからの発注は当面無効化</b>しています(検証・銘柄確認用)。'
            'lss が強い相場(5-7月)の裏で、ロング有利な相場(1月など)を取るための鏡像戦略です。</div>')
    elif _LSS_ORDER_MODE:
        # lss タブ: 発注ボタンは『信用新規売りの逆指値』として正しく送られる。
        _analysis_warn = (
            '<div style="margin:10px 0;padding:12px 16px;background:#1e293b;border:1px solid #38bdf8;'
            'border-radius:8px;color:#bae6fd;font-size:0.86rem;line-height:1.6">'
            '🔻 <b>このタブは lss(逆指値空売り・同日決済)です。</b><br>'
            '🚀発注は <b>信用新規売りの逆指値</b>(トリガー=<b>前日終値-1ティック</b>・以下で発動 / 発動後 -3%下限指値)'
            'として order_server に送られます(side=short)。'
            '<b>前日終値ちょうどは kabu が即約定(Code 100217)で弾く</b>ため、実発注は必ず1ティック下になります'
            '(=前日終値を割ったら発動)。表の<b>トリガー・指値下限・損切り(上)・目標(下)・同日決済</b>は'
            'この実発注値に合わせて表示しています(損切り=上/目標=下=5分足OCOの基準値)。<br>'
            '<b>発注はエントリー(逆指値売り)のみ</b>で自動の損切り・利確注文は置きません。'
            '約定したら<b>その日の引けに買戻し</b>で決済します — '
            '<code>python close_lss_guard.py --execute</code> を引け前(14:50頃)に実行すると自動で引け成行買戻しします'
            '(ロング建玉・メインショート*_Sは触りません)。'
            'まず <code>order_server.py</code> / <code>close_lss_guard.py</code> を --execute なし(dry-run)で確認してください。</div>')
    elif _ANALYSIS_ONLY:
        # mirror(却下済): 値がロングのままなので発注不可。
        _analysis_warn = (
            '<div style="margin:10px 0;padding:12px 16px;background:#3f1d1d;border:1px solid #b91c1c;'
            'border-radius:8px;color:#fecaca;font-size:0.86rem;line-height:1.6">'
            '🚫 <b>この画面から発注しないでください（分析専用）。</b><br>'
            'このタブは <b>ロングミラー(指値空売り)</b> の検証で、'
            '下表の逆指値・損切り・目標・保有日数は<b>ロング逆指値買いの値</b>のまま表示されています。'
            'そのため発注ボタンは無効化しています。</div>')
    else:
        _analysis_warn = ""
    return score_section + _order_js + f"""
<h2>{sig_label} のシグナル一覧 — BTスコア降順 {min_note}</h2>
{_analysis_warn}
<p style="color:#64748b;font-size:0.82rem;margin-bottom:12px">
  全WATCHLIST {len(all_items)}件から {sig_label} のエントリーシグナルを抽出。BTスコアが高い順に並んでいます。
</p>
<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:10px">
  ※ 逆指値注文（青）= ロング:翌日高値がこの価格以上で発動 / ショート:翌日安値がこの価格以下で発動<br>
  ※ 指値（橙）= ロング:上限(+3%) ギャップアップが大きすぎたらキャンセル / ショート:下限(-3%) ギャップダウンが大きすぎたらキャンセル
</p>
{_budget_bar}
<table>
  <thead><tr>
    <th>順位</th>
    <th style="text-align:left">銘柄 / スクリプト</th>
    <th>戦略</th><th>スコア</th>
    <th>売買代金<br><small>/日</small></th>
    <th>シグナル日<br>時株価</th>
    <th style="color:#38bdf8">逆指値<br>(トリガー)</th>
    <th style="color:#f59e0b">{'指値上限<br>(+3%)' if _LSS_LONG else ('指値下限<br>(-3%)' if _LSS_ORDER_MODE else '指値上限/下限<br>(±3%)')}</th>
    <th>{'損切り(下)' if _LSS_LONG else ('損切り(上)' if _LSS_ORDER_MODE else '損切り(-)')}</th><th>{'目標(上)' if _LSS_LONG else ('目標(下)' if _LSS_ORDER_MODE else '目標(+)')}</th>
    <th>株数<br><small>想定額</small></th>
    <th>最大保有</th><th>最大決済日</th><th>登録</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p class="footnote">{'※ lss は同日決済: 逆指値売りが約定した当日の引け成行で買戻し(close_lss_guard.py)。損切り=上/目標=下は5分足OCOの基準値で、実発注はエントリーのみ(利確・損切りは引け決済)。' if _LSS_ORDER_MODE else '※ 最大決済日 = シグナル日 + 約定期限3営業日 + 最大保有15日'}</p>"""


_DETAIL_TAB_SEQ = 0  # 取引明細タブの DOM id 衝突回避用カウンタ

# 取引明細フラットテーブル(全部/BT30以上/BT70以上)の描画行数上限。
# lss top指定なしで数万行になりHTMLが重くなるため、既定で直近1500件に打ち切る。
# 集計(サマリー/月別/スコア別)は全件ベースで別計算なので数字は変わらない。
# 全件描画したいときは環境変数 DETAIL_ROW_CAP=0 (または大きい値) で上書き。
try:
    _DETAIL_ROW_CAP = int(os.environ.get("DETAIL_ROW_CAP", "1500"))
except Exception:
    _DETAIL_ROW_CAP = 1500

_OOS_BT_SCORES: dict = {}  # (sym, strat) -> rec_score, populated by _tab5_pnl_html
_pnl_bt_cache: dict = {}         # cfg_key -> items_per_cfg (バックテスト結果キャッシュ)
_preoos_tab5_score_cache: dict = {}  # (sym, strat, cutoff_days) -> score
_ASOF_BT_CACHE: dict = {}  # (sym, strat, mode, sig_date, window) -> シグナル日時点BTスコア


def _asof_bt_score(full_log, mod, asof_date, periods=(30, 90, 180, 365)):
    """full_trade_log から asof_date 時点のBTスコアを計算(先読みなし)。
    各期間pは [asof-p, asof) に『決済済み』のトレードだけで統計を作り
    mod.calc_recommend_score にかける。asof より前の決済トレードが無ければ None。
    → 過去検証で「当時のBTスコア」を追加バックテストなしに再現するための関数。"""
    pr = {}
    for p in periods:
        lo = asof_date - timedelta(days=p)
        n = wins = 0
        gp = gl = tot = 0.0
        for t in full_log:
            if t.get("reason") in ("発注中", "保有中"):
                continue
            ed = t.get("exit_dt")
            if ed is None:
                continue
            edd = ed.date() if hasattr(ed, "date") else ed
            if lo <= edd < asof_date:
                pnl = float(t.get("pnl", 0.0))
                n += 1
                tot += pnl
                if pnl > 0:
                    wins += 1
                    gp += pnl
                else:
                    gl += -pnl
        if n == 0:
            continue
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[p] = {"trades": n, "win_rate": wins / n * 100.0, "pf": pf, "total_pnl": tot}
    if not pr:
        return None
    try:
        return mod.calc_recommend_score(pr)[0]
    except Exception:
        return None


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

    def _stats(trades: list[dict]) -> dict:
        n = len(trades)
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
        gp = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
        gl = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        timecuts = sum(1 for t in trades if t.get("reason") == "タイムカット")
        avg_hold = sum(t.get("hold_days", 0) for t in trades) / n if n else 0.0
        return {
            "trades": n, "wins": wins, "losses": losses,
            "win_rate": wins / n * 100 if n else 0.0,
            "gross_profit": gp, "gross_loss": -gl,
            "avg_win": gp / wins if wins else 0.0,
            "avg_loss": -gl / losses if losses else 0.0,
            "total_pnl": sum(t.get("pnl", 0) for t in trades),
            "pf": pf, "avg_hold": avg_hold, "timecuts": timecuts,
        }

    def _collect(mh: int) -> list[dict]:
        """MAX_HOLD=mh で全銘柄をバックテストし、対象期間の決済済みトレードを
        戦略タグ(_strat)付きで返す。"""
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
                    # (sym,strat) のBTスコア(生 calc_recommend_score・押し目/フェア版と同式)。
                    # BTフィルタ用。ATRペナルティは掛けない(sibling分析と統一)。
                    try:
                        _bt_sc, _ = _mod_for(sym_st[1]).calc_recommend_score(pr)
                    except Exception:
                        _bt_sc = 0
                    for t in pr[max_p].get("trade_log", []):
                        sig = t.get("signal_dt")
                        if not sig:
                            continue
                        sig_date = sig.date() if hasattr(sig, "date") else sig
                        if (sig_date >= since
                                and t.get("reason") not in ("発注中", "保有中")):
                            _t = dict(t)
                            _t["_strat"] = sym_st[1]
                            _t["_sym"] = sym_st[0]
                            _t["_bt"] = _bt_sc
                            trades.append(_t)
                except Exception as _e:
                    _errors += 1
                    if _errors <= 3:
                        print(f"  [max_hold比較] エラー {sym_st}: {_e}", flush=True)
        if _errors:
            print(f"  [max_hold比較] MAX_HOLD={mh}: {len(trades)}件 (エラー{_errors}件)", flush=True)
        return trades

    def _per_strategy_html(trades_by_mh: dict) -> str:
        """戦略別に MAX_HOLD 比較テーブルを折りたたみで出す。各戦略の最良保有日数を明示。"""
        try:
            from backtest_limit_entry import default_max_hold as _dmh_default
        except Exception:
            _dmh_default = lambda s: 15
        strats = sorted({t["_strat"] for mh in hold_list for t in trades_by_mh[mh]})
        out = ""
        for st in strats:
            sr = {mh: _stats([t for t in trades_by_mh[mh] if t["_strat"] == st]) for mh in hold_list}
            if max(sr[mh]["trades"] for mh in hold_list) < 10:
                continue   # サンプル過少はスキップ
            best = max(hold_list, key=lambda m: sr[m]["total_pnl"])
            _cur = _dmh_default(st)
            _note = (f'現行{_cur}日' + ('＝最良' if _cur == best else f' → 最良は{best}日'))
            out += (f'<details style="margin:6px 0"><summary style="cursor:pointer;'
                    f'color:#93c5fd;font-weight:700;padding:3px 0">{st}'
                    f'（最良=最大{best}日 / {_note}）</summary>{_build_table(sr)}</details>')
        return out or '<p style="color:#64748b">戦略別に十分なサンプルがありません。</p>'

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
                # 利益側 (勝ち件数 / 利益合計 / 平均利益)
                f'<td style="padding:6px 10px;text-align:right;color:#4ade80">{r["wins"]:,}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#4ade80">+{r["gross_profit"]:,.0f}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#4ade80">+{r["avg_win"]:,.0f}</td>'
                # 損失側 (負け件数 / 損失合計 / 平均損失)
                f'<td style="padding:6px 10px;text-align:right;color:#f87171">{r["losses"]:,}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#f87171">{r["gross_loss"]:,.0f}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#f87171">{r["avg_loss"]:,.0f}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{r["avg_hold"]:.1f}日</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#fbbf24">{r["timecuts"]:,}</td>'
                f'<td style="padding:6px 10px;text-align:right;font-weight:700;color:{pnl_color}">{pnl_str}円</td>'
                f'</tr>\n'
            )
        return (
            f'<table style="width:100%;border-collapse:collapse;font-size:0.85rem">'
            f'<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            f'<th style="padding:5px 10px;text-align:left">最大保有</th>'
            f'<th style="padding:5px 10px;text-align:right">件数</th>'
            f'<th style="padding:5px 10px;text-align:right">勝率</th>'
            f'<th style="padding:5px 10px;text-align:right">PF</th>'
            f'<th style="padding:5px 10px;text-align:right;color:#4ade80">勝数</th>'
            f'<th style="padding:5px 10px;text-align:right;color:#4ade80">利益合計</th>'
            f'<th style="padding:5px 10px;text-align:right;color:#4ade80">平均利益</th>'
            f'<th style="padding:5px 10px;text-align:right;color:#f87171">負数</th>'
            f'<th style="padding:5px 10px;text-align:right;color:#f87171">損失合計</th>'
            f'<th style="padding:5px 10px;text-align:right;color:#f87171">平均損失</th>'
            f'<th style="padding:5px 10px;text-align:right">平均保有日</th>'
            f'<th style="padding:5px 10px;text-align:right">タイムカット</th>'
            f'<th style="padding:5px 10px;text-align:right">純損益</th>'
            f'</tr></thead>'
            f'<tbody>{rows}</tbody>'
            f'</table>'
        )

    def _recovery_html(trades_by_mh: dict) -> str:
        """『7日でタイムカット(=未決着)だった玉を、そのまま10日/15日まで持つと
        どうなるか』を、7日決済時の含み損/含み益で分けて実測する。
        同一エントリー(sym,strat,signal_dt)を延長版バックテストと突き合わせ。
        BTフィルタ(全部/BT60/BT70)別 + 戦略別に出す。BTは base(7日)エントリーの
        生 calc_recommend_score でフィルタ(押し目/フェア版と同じ基準)。"""
        base = 7
        longer = [m for m in (10, 15) if m in trades_by_mh and m != base]
        if base not in trades_by_mh or not longer:
            return ""

        def _key(t):
            sd = t.get("signal_dt")
            sd = sd.date() if hasattr(sd, "date") else sd
            return (t.get("_sym"), t.get("_strat"), sd)

        long_maps = {mh: {_key(t): t for t in trades_by_mh[mh]} for mh in longer}
        base_tc = {k: t for k, t in
                   {_key(t): t for t in trades_by_mh[base]}.items()
                   if t.get("reason") == "タイムカット"}
        if not base_tc:
            return ""

        _thead = (
            '<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            '<th style="padding:5px 10px;text-align:left">7日決済時グループ</th>'
            '<th style="padding:5px 10px;text-align:right">延長</th>'
            '<th style="padding:5px 10px;text-align:right">件数</th>'
            '<th style="padding:5px 10px;text-align:right">7日損益</th>'
            '<th style="padding:5px 10px;text-align:right">延長後損益</th>'
            '<th style="padding:5px 10px;text-align:right">差分(延長効果)</th>'
            '<th style="padding:5px 10px;text-align:right;color:#4ade80">プラス転換</th>'
            '<th style="padding:5px 10px;text-align:right;color:#f87171">悪化</th>'
            '<th style="padding:5px 10px;text-align:right">なお未決着</th>'
            '<th style="padding:5px 10px;text-align:right">延長後勝率</th>'
            '</tr></thead>')

        def _one_table(keep) -> tuple[str, list]:
            """keep(base_trade)->bool でフィルタした部分集合で回復テーブルを作る。"""
            tc7 = {k: t for k, t in base_tc.items() if keep(t)}
            if not tc7:
                return "", []
            rows = ""
            verdicts = []
            for mh in longer:
                lmap = long_maps[mh]
                for want_loss, glabel, gcol in ((True, "7日時点 含み損", "#f87171"),
                                                (False, "7日時点 含み益", "#4ade80")):
                    keys = [k for k, t in tc7.items()
                            if (t.get("pnl", 0) < 0) == want_loss and k in lmap]
                    n = len(keys)
                    if n == 0:
                        continue
                    base_pnl = sum(tc7[k].get("pnl", 0) for k in keys)
                    long_pnl = sum(lmap[k].get("pnl", 0) for k in keys)
                    delta = long_pnl - base_pnl
                    turned_pos = sum(1 for k in keys
                                     if tc7[k].get("pnl", 0) < 0 and lmap[k].get("pnl", 0) > 0)
                    worse = sum(1 for k in keys if lmap[k].get("pnl", 0) < tc7[k].get("pnl", 0))
                    still_tc = sum(1 for k in keys if lmap[k].get("reason") == "タイムカット")
                    wins = sum(1 for k in keys if lmap[k].get("pnl", 0) > 0)
                    dc = "#4ade80" if delta >= 0 else "#f87171"
                    wr = wins / n * 100 if n else 0.0
                    rows += (
                        f'<tr>'
                        f'<td style="padding:5px 10px;color:{gcol};text-align:left">{glabel}</td>'
                        f'<td style="padding:5px 10px;text-align:right">→{mh}日</td>'
                        f'<td style="padding:5px 10px;text-align:right">{n}件</td>'
                        f'<td style="padding:5px 10px;text-align:right;color:#94a3b8">{base_pnl:+,.0f}</td>'
                        f'<td style="padding:5px 10px;text-align:right">{long_pnl:+,.0f}</td>'
                        f'<td style="padding:5px 10px;text-align:right;color:{dc};font-weight:700">{delta:+,.0f}</td>'
                        f'<td style="padding:5px 10px;text-align:right;color:#4ade80">{turned_pos}件</td>'
                        f'<td style="padding:5px 10px;text-align:right;color:#f87171">{worse}件</td>'
                        f'<td style="padding:5px 10px;text-align:right;color:#94a3b8">{still_tc}件</td>'
                        f'<td style="padding:5px 10px;text-align:right">{wr:.0f}%</td>'
                        f'</tr>'
                    )
                    if want_loss:
                        kind = "改善" if delta > 0 else "悪化"
                        verdicts.append(
                            f'含み損 {n}件を{mh}日まで持つと純損益 '
                            f'<b style="color:{dc}">{delta:+,.0f}円 {kind}</b>'
                            f'（プラス転換 {turned_pos} / 悪化 {worse}）')
            if not rows:
                return "", []
            return (f'<table style="width:100%;border-collapse:collapse;font-size:0.85rem">'
                    f'{_thead}<tbody>{rows}</tbody></table>'), verdicts

        _bt_levels = [(0, "全部"), (60, "BT60以上"), (70, "BT70以上")]

        # ── 全体（BTフィルタ別に縦積み） ──
        agg_html = ""
        for bt_min, lbl in _bt_levels:
            tbl, vs = _one_table(lambda t, b=bt_min: (t.get("_bt") or 0) >= b)
            if not tbl:
                continue
            vhtml = ("<br>".join(vs)) if vs else ""
            agg_html += (
                f'<h5 style="margin:14px 0 4px;color:#e2e8f0;font-size:0.82rem">{lbl}</h5>'
                f'{tbl}'
                + (f'<p style="margin:6px 0 0;color:#cbd5e1;font-size:0.8rem">{vhtml}</p>' if vhtml else ""))
        if not agg_html:
            return ""

        # ── 戦略別（折りたたみ・各BTレベル） ──
        strats = sorted({t.get("_strat") for t in base_tc.values() if t.get("_strat")})
        strat_html = ""
        for st in strats:
            inner = ""
            for bt_min, lbl in _bt_levels:
                tbl, vs = _one_table(
                    lambda t, s=st, b=bt_min: t.get("_strat") == s and (t.get("_bt") or 0) >= b)
                if not tbl:
                    continue
                vhtml = ("<br>".join(vs)) if vs else ""
                inner += (
                    f'<div style="margin:8px 0 0;color:#93c5fd;font-size:0.76rem">{lbl}</div>{tbl}'
                    + (f'<p style="margin:4px 0 0;color:#cbd5e1;font-size:0.78rem">{vhtml}</p>' if vhtml else ""))
            if inner:
                strat_html += (
                    f'<details style="margin:6px 0"><summary style="cursor:pointer;'
                    f'color:#a78bfa;font-size:0.82rem;font-weight:600">▶ {st}</summary>'
                    f'<div style="padding:4px 0 10px">{inner}</div></details>')

        return (
            f'<div style="margin:20px 0 0;padding:16px 20px;background:#1e293b;'
            f'border-radius:8px;border-left:3px solid #fbbf24">'
            f'<h4 style="margin:0 0 6px;color:#fbbf24;font-size:0.9rem">'
            f'⑬ 7日タイムカット玉を延長すると回復するか（BTフィルタ別・戦略別・後知恵なし）</h4>'
            f'<p style="margin:0 0 4px;color:#94a3b8;font-size:0.78rem">'
            f'7日で未決着(タイムカット)だった同一エントリーを、そのまま10日/15日まで持った'
            f'場合の実損益。<b>「差分」がプラスなら延長に価値あり／マイナスなら7日で切るのが正解</b>。'
            f'BTは base(7日)の生スコア(押し目/フェア版と同基準)でフィルタ。'
            f'「損切りが惜しい」という感情ではなく、この差分で判断する。</p>'
            f'{agg_html}'
            f'<h5 style="margin:18px 0 4px;color:#c4b5fd;font-size:0.82rem">戦略別（クリックで展開）</h5>'
            f'{strat_html}</div>'
        )

    if compare_modes:
        # Conservative
        print(f"  [max_hold比較] conservative 集計中...", flush=True)
        _set_sig_params("conservative")
        con_results: dict[int, dict] = {}
        for mh in hold_list:
            print(f"    MAX_HOLD={mh}日...", flush=True)
            con_results[mh] = _stats(_collect(mh))
        # Aggressive
        print(f"  [max_hold比較] aggressive 集計中...", flush=True)
        _set_sig_params("aggressive")
        agg_results: dict[int, dict] = {}
        for mh in hold_list:
            print(f"    MAX_HOLD={mh}日...", flush=True)
            agg_results[mh] = _stats(_collect(mh))
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
        trades_by_mh: dict[int, list] = {}
        for mh in hold_list:
            print(f"  [max_hold比較] MAX_HOLD={mh}日 集計中...", flush=True)
            trades_by_mh[mh] = _collect(mh)
        results = {mh: _stats(trades_by_mh[mh]) for mh in hold_list}
        return (
            f'<div style="margin:0 0 24px;padding:16px 20px;background:#1e293b;'
            f'border-radius:8px;border-left:3px solid #a78bfa">'
            f'<h4 style="margin:0 0 12px;color:#a78bfa;font-size:0.95rem">'
            f'⏱ 最大保有日数 比較（全戦略まとめ・直近{days}日）</h4>'
            f'{_build_table(results)}'
            f'<h4 style="margin:16px 0 6px;color:#93c5fd;font-size:0.9rem">'
            f'戦略別 最大保有日数比較（クリックで展開・各戦略の最良保有日数を表示）</h4>'
            f'{_per_strategy_html(trades_by_mh)}</div>'
            f'{_recovery_html(trades_by_mh)}'
        )


def build_fade_short_html(days: int, workers: int) -> str:
    """『ブレイク逆張りショート』検証。ロングのブレイクシグナルを同じトリガーで
    ショートで入り、N日(1〜5)後の終値で手仕舞いした場合の損益を実測する。
    全戦略 em=0 なのでトリガー=前日終値。約定=long と同条件(高値>=前日終値)。
    エントリー(空売り)にスリッページ、往復手数料を計上。差分ではなく総損益で判断。"""
    if not _SIGNALS_AVAILABLE:
        return ""
    from backtest_limit_entry import (
        fetch as _fetch, SLIPPAGE_STOP_PCT as _SLIP, FEE_PCT_ONE_WAY as _FEE,
        FIXED_QTY as _QTY, ENTRY_EXPIRE as _EXP, MIN_PRICE as _MINP,
        MAX_PRICE as _MAXP, MAX_ATR_RATIO as _MAXATR)
    _NS = [1, 2, 3, 4, 5]

    seen: set = set()
    items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                items.append((sym, name, strat))
    if not items:
        return ""
    since = _TODAY - timedelta(days=days)

    def _one(sym, name, strat) -> list:
        mod = _mod_for(strat)
        try:
            params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
            if not params:
                return []
            calc_fn = params[0]
            df = _fetch(sym)
            if df is None or len(df) < 30:
                return []
            df = calc_fn(df.copy())
            if "entry_sig" not in df:
                return []
            try:
                r = mod.backtest_one(sym, name, strat)
                bt = mod.calc_recommend_score(r.get("period_results", {}))[0] if r else 0
            except Exception:
                bt = 0
            closes = df["close"].values
            highs = df["high"].values
            sigs = df["entry_sig"].values
            atrs = df["atr"].values if "atr" in df else None
            idx = df.index
            n = len(df)
            recs = []
            for s in range(n - 1):
                if not bool(sigs[s]):
                    continue
                cp = closes[s]
                if not (_MINP <= cp <= _MAXP):
                    continue
                if atrs is not None:
                    a = atrs[s]
                    if a and cp and a / cp > _MAXATR:
                        continue
                order_p = cp   # em=0 → トリガー=前日終値
                fill = None
                for j in range(s + 1, min(s + 2 + _EXP, n)):
                    if highs[j] >= order_p:
                        fill = j
                        break
                if fill is None:
                    continue
                fdate = idx[fill]
                fdate = fdate.date() if hasattr(fdate, "date") else fdate
                if fdate < since:
                    continue
                entry_p = order_p * (1 - _SLIP)   # 空売り約定=不利側
                for N in _NS:
                    ei = fill + N
                    if ei >= n:
                        continue
                    exit_c = closes[ei]
                    fee = (entry_p + exit_c) * _QTY * _FEE
                    pnl = (entry_p - exit_c) * _QTY - fee   # ショート損益
                    recs.append({"strat": strat, "bt": bt, "N": N, "pnl": pnl,
                                 "fd": fdate})
            return recs
        except Exception:
            return []

    all_recs: list[dict] = []
    with _TPE(max_workers=workers) as ex:
        futs = [ex.submit(_one, s, n, st) for s, n, st in items]
        for fut in _asc(futs):
            try:
                all_recs.extend(fut.result() or [])
            except Exception:
                pass
    if not all_recs:
        return ""

    # ── 日経レジーム（約定日基準）を各レコードに付与 ──
    _tmap = {}
    try:
        # 過去検証対応: レコードの約定日レンジをカバーする日経を取得
        # (fetch_n225(2)固定だと過去基準でトレードと重ならず trend=None になる)
        _fds = [r.get("fd") for r in all_recs if r.get("fd")]
        if _fds:
            _fd_min, _fd_max = min(_fds), max(_fds)
            _ny = max(2, (_fd_max - _fd_min).days // 365 + 3)
            _n225_close = fetch_n225(_ny, end_date=_fd_max)
        else:
            _n225_close = fetch_n225(2)
        _n225_trend = label_trend(_n225_close)
        _tmap = {(dt.date() if hasattr(dt, "date") else dt): tr
                 for dt, tr in zip(_n225_trend.index, _n225_trend)}
    except Exception:
        _tmap = {}
    for r in all_recs:
        r["trend"] = _tmap.get(r.get("fd"))

    def _split(sub):
        """件数/勝率/PF/勝件数/利益合計/負件数/損失合計/総損益/平均 の辞書。"""
        cnt = len(sub)
        if cnt == 0:
            return None
        pnls = [r["pnl"] for r in sub]
        wins = [p for p in pnls if p > 0]
        gp = sum(wins)
        gl = -sum(p for p in pnls if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        return {"n": cnt, "wr": len(wins) / cnt * 100, "pf": pf,
                "nw": len(wins), "gp": gp, "nl": cnt - len(wins), "gl": -gl,
                "total": sum(pnls), "avg": sum(pnls) / cnt}

    def _pf(pf):
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    def _row(label, st):
        tc = "#4ade80" if st["total"] >= 0 else "#f87171"
        return (
            f'<tr><td style="padding:5px 10px;text-align:left">{label}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:#94a3b8">{st["n"]}件</td>'
            f'<td style="padding:5px 10px;text-align:right">{st["wr"]:.0f}%</td>'
            f'<td style="padding:5px 10px;text-align:right">{_pf(st["pf"])}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:#4ade80;'
            f'border-left:1px solid #334155">{st["nw"]}件</td>'
            f'<td style="padding:5px 10px;text-align:right;color:#4ade80;font-weight:700">{st["gp"]:+,.0f}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:#f87171;'
            f'border-left:1px solid #334155">{st["nl"]}件</td>'
            f'<td style="padding:5px 10px;text-align:right;color:#f87171;font-weight:700">{st["gl"]:+,.0f}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:{tc};font-weight:700;'
            f'border-left:1px solid #334155">{st["total"]:+,.0f}</td>'
            f'<td style="padding:5px 10px;text-align:right;color:{tc}">{st["avg"]:+,.0f}</td></tr>')

    def _head(first):
        return (
            '<div style="overflow-x:auto"><table style="width:auto;min-width:720px;'
            'border-collapse:collapse;font-size:0.85rem"><thead>'
            '<tr style="color:#64748b;font-size:0.7rem"><th colspan="4"></th>'
            '<th colspan="2" style="padding:3px 10px;text-align:center;color:#4ade80;'
            'border-left:1px solid #334155">利益側(ショート勝ち)</th>'
            '<th colspan="2" style="padding:3px 10px;text-align:center;color:#f87171;'
            'border-left:1px solid #334155">損失側(踏み上げ)</th>'
            '<th colspan="2" style="border-left:1px solid #334155"></th></tr>'
            '<tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            f'<th style="padding:5px 10px;text-align:left">{first}</th>'
            '<th style="padding:5px 10px;text-align:right">件数</th>'
            '<th style="padding:5px 10px;text-align:right">勝率</th>'
            '<th style="padding:5px 10px;text-align:right">PF</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">勝件数</th>'
            '<th style="padding:5px 10px;text-align:right">利益合計</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">負件数</th>'
            '<th style="padding:5px 10px;text-align:right">損失合計</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">総損益</th>'
            '<th style="padding:5px 10px;text-align:right">平均</th></tr></thead><tbody>')

    def _table(recs):
        rows = ""
        best_n, best_pnl = None, None
        for N in _NS:
            st = _split([r for r in recs if r["N"] == N])
            if not st:
                continue
            if best_pnl is None or st["total"] > best_pnl:
                best_pnl, best_n = st["total"], N
            rows += _row(f"{N}日で手仕舞い", st)
        if not rows:
            return "", ""
        tbl = _head("手仕舞い") + rows + "</tbody></table></div>"
        if best_pnl is None:
            verdict = ""
        elif best_pnl > 0:
            verdict = (f'最良は<b>{best_n}日</b>で総損益 '
                       f'<b style="color:#4ade80">{best_pnl:+,.0f}円</b>。'
                       f'プラスなら逆張りショートに一定の有効性(要フォワード)。')
        else:
            verdict = ('<b style="color:#f87171">全N日でマイナス</b>。'
                       'ブレイクを逆張りショートしても勝てない＝理屈どおり'
                       '(勝ちブレイクの上昇に踏まれる)。')
        return tbl, verdict

    def _trend_table(recs, N=3):
        """N日手仕舞い固定で、日経レジーム別に利益/損失分離。"""
        rows = ""
        pos_down = None
        for tk, tl in [("up", "▲ 上昇"), ("sideways", "→ 横ばい"), ("down", "▼ 下落")]:
            st = _split([r for r in recs if r["N"] == N and r.get("trend") == tk])
            if not st:
                continue
            if tk == "down":
                pos_down = st["total"]
            rows += _row(tl, st)
        if not rows:
            return "", None
        return _head("約定日の日経") + rows + "</tbody></table></div>", pos_down

    blocks = ""
    for bt_min, lbl in [(0, "全部"), (60, "BT60以上"), (70, "BT70以上")]:
        sub = [r for r in all_recs if (r["bt"] or 0) >= bt_min]
        tbl, verdict = _table(sub)
        if not tbl:
            continue
        ttbl, down_pnl = _trend_table(sub, N=3)
        tblock = ""
        if ttbl:
            if down_pnl is not None and down_pnl > 0:
                tv = ('<b style="color:#4ade80">▼下落レジームでは逆張りショートがプラス</b>'
                      '＝下げ相場ではブレイクのダマシをショートで取れる可能性(要フォワード)。')
            elif down_pnl is not None:
                tv = ('▼下落レジームでもマイナス＝下げ相場でも逆張りショートは不成立。')
            else:
                tv = ""
            tblock = (
                '<div style="margin:8px 0 0"><span style="color:#64748b;font-size:0.74rem">'
                'トレンド別（3日手仕舞い固定・約定日の日経レジームで集計）</span>'
                f'{ttbl}<p style="margin:4px 0 0;color:#cbd5e1;font-size:0.78rem">{tv}</p></div>')
        blocks += (
            f'<h5 style="margin:14px 0 4px;color:#e2e8f0;font-size:0.82rem">{lbl}</h5>{tbl}'
            f'<p style="margin:6px 0 0;color:#cbd5e1;font-size:0.8rem">{verdict}</p>{tblock}')
    if not blocks:
        return ""
    return (
        f'<div style="margin:20px 0 0;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #f472b6">'
        f'<h4 style="margin:0 0 6px;color:#f472b6;font-size:0.9rem">'
        f'⑲ ブレイク逆張りショート（ロングシグナルをショートで入りN日で手仕舞い・直近{days}日）</h4>'
        f'<p style="margin:0 0 4px;color:#94a3b8;font-size:0.78rem">'
        f'ロングのブレイクシグナルを、同じトリガー(前日終値)を高値が超えたところで'
        f'<b>ショート</b>し、1〜5日後の終値で手仕舞いした場合の損益。空売り約定にスリッページ、'
        f'往復手数料を計上。<b>プラスなら「序盤の押しをショートで取る」が成立／マイナスなら不成立</b>。</p>'
        f'{blocks}</div>'
    )


def build_holdday_curve_html(days: int, workers: int) -> str:
    """『保有N日目の平均含み損益』を実測（詳細分析タブ用）。
    各ロングエントリーを日次で追い、終値ベースの目標/損切りで決済されるまでの
    未決着玉の平均含み損益を保有日ごとに出す。加えて『初日(1日目)に含み損だった
    エントリーだけ』に絞った平均も並べ、序盤マイナス→回復の形を確認できる。
    全戦略 em=0(トリガー=前日終値)・close損切りに統一済みなので再現可能。"""
    if not _SIGNALS_AVAILABLE:
        return ""
    from backtest_limit_entry import (
        fetch as _fetch, SLIPPAGE_STOP_PCT as _SLIP, FIXED_QTY as _QTY,
        ENTRY_EXPIRE as _EXP, MIN_PRICE as _MINP, MAX_PRICE as _MAXP,
        MAX_ATR_RATIO as _MAXATR)
    HOLD_MAX = 12   # 10日タイムカット + 余裕2日

    seen: set = set()
    items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                items.append((sym, name, strat))
    if not items:
        return ""
    since = _TODAY - timedelta(days=days)

    def _one(sym, name, strat) -> list:
        """(bt, {保有日: 含み損益}) のリストを返す（エントリー単位の軌跡）。"""
        mod = _mod_for(strat)
        try:
            params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
            if not params or len(params) < 4:
                return []
            calc_fn, _em, _sm, _tm = params[0], params[1], params[2], params[3]
            df = _fetch(sym)
            if df is None or len(df) < 40:
                return []
            df = calc_fn(df.copy())
            if "entry_sig" not in df:
                return []
            try:
                bt = mod.calc_recommend_score(
                    mod.backtest_one(sym, name, strat).get("period_results", {}))[0]
            except Exception:
                bt = 0
            closes = df["close"].values
            highs = df["high"].values
            sigs = df["entry_sig"].values
            if "atr" in df:
                atrs = df["atr"].values
            else:
                h, l, c = df["high"], df["low"], df["close"]
                pc = c.shift(1)
                tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
                atrs = tr.ewm(alpha=1 / 14, adjust=False).mean().values
            idx = df.index
            n = len(df)
            entries = []   # (bt, {day: pnl})
            for s in range(n - 1):
                if not bool(sigs[s]):
                    continue
                cp = closes[s]
                if not (_MINP <= cp <= _MAXP):
                    continue
                a = atrs[s]
                if not a or a <= 0 or a / cp > _MAXATR:
                    continue
                order_p = cp   # em=0
                fill = None
                for j in range(s + 1, min(s + 2 + _EXP, n)):
                    if highs[j] >= order_p:
                        fill = j
                        break
                if fill is None:
                    continue
                fdate = idx[fill]
                fdate = fdate.date() if hasattr(fdate, "date") else fdate
                if fdate < since:
                    continue
                entry_p = order_p * (1 + _SLIP)
                stop = order_p - a * _sm
                target = order_p + a * _tm
                traj = {}
                for d in range(1, HOLD_MAX + 1):
                    ei = fill + d
                    if ei >= n:
                        break
                    c = closes[ei]
                    if c >= target or c <= stop:   # 終値ベースで決済 → 以降は保有せず
                        break
                    traj[d] = (c - entry_p) * _QTY
                if traj:
                    entries.append((bt, traj))
            return entries
        except Exception:
            return []

    all_entries: list = []
    with _TPE(max_workers=workers) as ex:
        futs = [ex.submit(_one, s, n, st) for s, n, st in items]
        for fut in _asc(futs):
            try:
                all_entries.extend(fut.result() or [])
            except Exception:
                pass
    if not all_entries:
        return ""

    def _table(entries: list) -> str:
        # 初日(1日目)に含み損だったエントリー = traj[1] < 0
        cond = [t for (_b, t) in entries if t.get(1) is not None and t[1] < 0]
        allt = [t for (_b, t) in entries]
        rows = ""
        for d in range(1, HOLD_MAX + 1):
            av = [t[d] for t in allt if d in t]
            cv = [t[d] for t in cond if d in t]
            if not av:
                continue
            a_avg = sum(av) / len(av)
            ac = "#4ade80" if a_avg >= 0 else "#f87171"
            if cv:
                c_avg = sum(cv) / len(cv)
                cc = "#4ade80" if c_avg >= 0 else "#f87171"
                c_cnt = f"{len(cv)}件"
                c_val = f'{c_avg:+,.0f}'
            else:
                cc, c_cnt, c_val = "#475569", "—", "—"
            rows += (
                f'<tr><td style="padding:5px 12px;text-align:right">{d}日目</td>'
                f'<td style="padding:5px 12px;text-align:right;color:#94a3b8">{len(av)}件</td>'
                f'<td style="padding:5px 12px;text-align:right;color:{ac};font-weight:700">{a_avg:+,.0f}</td>'
                f'<td style="padding:5px 12px;text-align:right;color:#94a3b8;'
                f'border-left:1px solid #334155">{c_cnt}</td>'
                f'<td style="padding:5px 12px;text-align:right;color:{cc};font-weight:700">{c_val}</td></tr>')
        if not rows:
            return ""
        return (
            '<table style="width:auto;min-width:560px;border-collapse:collapse;font-size:0.85rem">'
            '<thead>'
            '<tr style="color:#64748b;font-size:0.72rem">'
            '<th></th><th colspan="2" style="padding:3px 12px;text-align:center;color:#94a3b8">全エントリー</th>'
            '<th colspan="2" style="padding:3px 12px;text-align:center;color:#f87171;'
            'border-left:1px solid #334155">初日に含み損だった玉のみ</th></tr>'
            '<tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            '<th style="padding:5px 12px;text-align:right">保有</th>'
            '<th style="padding:5px 12px;text-align:right">件数</th>'
            '<th style="padding:5px 12px;text-align:right">平均含み損益</th>'
            '<th style="padding:5px 12px;text-align:right;border-left:1px solid #334155">件数</th>'
            '<th style="padding:5px 12px;text-align:right">平均含み損益</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')

    blocks = ""
    # (BT下限, BT上限, ラベル)。低BT帯を追加(ミラー早期利確の検討用)。
    for bt_lo, bt_hi, lbl in [
        (0, 1e9, "全部"),
        (0, 40, "BT&lt;40 (低BT)"),
        (40, 60, "BT40-59"),
        (60, 1e9, "BT60以上"),
        (70, 1e9, "BT70以上"),
    ]:
        sub = [e for e in all_entries if bt_lo <= (e[0] or 0) < bt_hi]
        tbl = _table(sub)
        if not tbl:
            continue
        blocks += f'<h5 style="margin:14px 0 4px;color:#e2e8f0;font-size:0.82rem">{lbl}</h5>{tbl}'
    if not blocks:
        return ""
    return (
        f'<div style="margin:20px 0 0;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #38bdf8">'
        f'<h4 style="margin:0 0 6px;color:#38bdf8;font-size:0.9rem">'
        f'⑳ 保有日数に対する平均含み損益（未決着玉・終値ベース・直近{days}日）</h4>'
        f'<p style="margin:0 0 4px;color:#94a3b8;font-size:0.78rem">'
        f'各ロングエントリーを日次で追い、終値が目標/損切りに達するまでの"まだ持っている玉"の'
        f'平均含み損益を保有日ごとに集計。右2列は<b>「初日(1日目)に含み損だったエントリー」だけ</b>'
        f'に絞った平均。<b>初日マイナスでも日を追うごとに平均が回復していく</b>なら、'
        f'あなたの今の含み損は待つのが正解。逆に悪化し続けるなら早期損切りが必要。</p>'
        f'{blocks}</div>'
    )


def build_stop_width_html(days: int, workers: int) -> str:
    """⑯ 損切り幅別 成績（詳細分析タブ用）。
    完了トレードを損切り幅%（|order_stop-order_limit|/order_limit）で帯分けし、
    件数/勝率/PF/総損益/平均損益/最大単発損失を集計する。加えて『損切り幅>8%を
    除外した場合』の総損益・最大単発損失・MaxDDを現状と並べ、高ボラ(広損切り)銘柄の
    一発大損がテールをどれだけ作っているかを可視化する。
    (sym,strat)単位で1回だけバックテストするのでconfig重複はカウントされない。"""
    if not _SIGNALS_AVAILABLE:
        return ""

    seen: set = set()
    items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                items.append((sym, name, strat))
    if not items:
        return ""
    since = _TODAY - timedelta(days=days)

    def _one(sym, name, strat) -> list:
        """(bt, stop_pct絶対%, pnl, exit_date) のリスト。"""
        mod = _mod_for(strat)
        try:
            res = mod.backtest_one(sym, name, strat)
            if not res:
                return []
            pr = res.get("period_results", {})
            if not pr:
                return []
            try:
                bt = mod.calc_recommend_score(pr)[0]
            except Exception:
                bt = 0
            trade_log = pr[max(pr.keys())].get("trade_log", [])
            out = []
            tseen: set = set()
            for t in trade_log:
                reason = t.get("reason", "") or ""
                if reason in ("", "発注中", "保有中"):
                    continue
                exit_dt = t.get("exit_dt")
                if exit_dt is None:
                    continue
                exit_d = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
                if exit_d < since:
                    continue
                k = (t.get("entry_dt"), exit_dt)
                if k in tseen:
                    continue
                tseen.add(k)
                olp = t.get("order_limit", 0) or 0
                osp = t.get("order_stop", 0) or 0
                if olp <= 0 or osp <= 0:
                    continue
                sw = abs(osp - olp) / olp * 100.0
                out.append((bt, sw, t.get("pnl", 0), exit_d))
            return out
        except Exception:
            return []

    rows_all: list = []
    with _TPE(max_workers=workers) as ex:
        futs = [ex.submit(_one, s, n, st) for s, n, st in items]
        for fut in _asc(futs):
            try:
                rows_all.extend(fut.result() or [])
            except Exception:
                pass
    if not rows_all:
        return ""

    # 損切り幅の帯
    BUCKETS = [(0, 5, "≤5%"), (5, 8, "5〜8%"), (8, 10, "8〜10%"),
               (10, 13, "10〜13%"), (13, 999, ">13%")]

    def _stats(trs: list) -> dict:
        pnls = [p for (_b, _w, p, _d) in trs]
        n = len(pnls)
        if n == 0:
            return {}
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gp = sum(wins)
        gl = -sum(losses)
        pf = (gp / gl) if gl > 0 else float("inf")
        return {
            "n": n, "wr": len(wins) / n * 100.0,
            "pf": pf, "total": sum(pnls),
            "avg": sum(pnls) / n,
            "worst": min(pnls) if pnls else 0,
            "nw": len(wins), "nl": len(losses),
            "gp": gp, "gl": -gl,   # 利益合計(+) / 損失合計(-)
            "avg_win": (gp / len(wins)) if wins else 0.0,
            "avg_loss": (-gl / len(losses)) if losses else 0.0,
        }

    def _maxdd(trs: list) -> float:
        # 決済日順の累積損益から最大ドローダウン（円）
        s = sorted(trs, key=lambda x: x[3])
        cum = 0.0
        peak = 0.0
        dd = 0.0
        for (_b, _w, p, _d) in s:
            cum += p
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        return dd

    def _fmt_pf(pf: float) -> str:
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    def _bucket_table(trs: list) -> str:
        rows = ""
        for lo, hi, lbl in BUCKETS:
            sub = [r for r in trs if lo <= r[1] < hi]
            st = _stats(sub)
            if not st:
                continue
            tc = "#4ade80" if st["total"] >= 0 else "#f87171"
            wide = lo >= 8
            row_bg = "background:#2d0a0a;" if wide else ""
            rows += (
                f'<tr style="{row_bg}"><td style="padding:5px 10px;text-align:left">{lbl}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#94a3b8">{st["n"]}</td>'
                f'<td style="padding:5px 10px;text-align:right">{st["wr"]:.0f}%</td>'
                f'<td style="padding:5px 10px;text-align:right">{_fmt_pf(st["pf"])}</td>'
                # ── 利益側（勝ち）──
                f'<td style="padding:5px 10px;text-align:right;color:#4ade80;'
                f'border-left:1px solid #334155">{st["nw"]}件</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#4ade80;font-weight:700">{st["gp"]:+,.0f}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#4ade80">{st["avg_win"]:+,.0f}</td>'
                # ── 損失側（負け）──
                f'<td style="padding:5px 10px;text-align:right;color:#f87171;'
                f'border-left:1px solid #334155">{st["nl"]}件</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#f87171;font-weight:700">{st["gl"]:+,.0f}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#f87171">{st["avg_loss"]:+,.0f}</td>'
                # ── 差引 ──
                f'<td style="padding:5px 10px;text-align:right;color:{tc};font-weight:700;'
                f'border-left:1px solid #334155">{st["total"]:+,.0f}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#f87171">{st["worst"]:+,.0f}</td></tr>')
        if not rows:
            return ""
        return (
            '<div style="overflow-x:auto">'
            '<table style="width:auto;min-width:820px;border-collapse:collapse;font-size:0.85rem">'
            '<thead>'
            '<tr style="color:#64748b;font-size:0.7rem">'
            '<th colspan="4"></th>'
            '<th colspan="3" style="padding:3px 10px;text-align:center;color:#4ade80;'
            'border-left:1px solid #334155">利益側（勝ち）</th>'
            '<th colspan="3" style="padding:3px 10px;text-align:center;color:#f87171;'
            'border-left:1px solid #334155">損失側（負け）</th>'
            '<th colspan="2" style="border-left:1px solid #334155"></th></tr>'
            '<tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            '<th style="padding:5px 10px;text-align:left">損切り幅</th>'
            '<th style="padding:5px 10px;text-align:right">件数</th>'
            '<th style="padding:5px 10px;text-align:right">勝率</th>'
            '<th style="padding:5px 10px;text-align:right">PF</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">勝件数</th>'
            '<th style="padding:5px 10px;text-align:right">利益合計</th>'
            '<th style="padding:5px 10px;text-align:right">平均利益</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">負件数</th>'
            '<th style="padding:5px 10px;text-align:right">損失合計</th>'
            '<th style="padding:5px 10px;text-align:right">平均損失</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">差引損益</th>'
            '<th style="padding:5px 10px;text-align:right">最大単発損失</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>')

    def _sim_table(trs: list) -> str:
        # 現状 vs 損切り幅>8%除外
        keep = [r for r in trs if r[1] <= 8]
        cur_st, keep_st = _stats(trs), _stats(keep)
        if not cur_st or not keep_st:
            return ""
        cur_dd, keep_dd = _maxdd(trs), _maxdd(keep)
        dropped = cur_st["n"] - keep_st["n"]

        def _cell(v, good_pos=True, money=True):
            c = "#4ade80" if (v >= 0) == good_pos else "#f87171"
            s = f'{v:+,.0f}' if money else f'{v:,.0f}'
            return f'<td style="padding:5px 14px;text-align:right;color:{c};font-weight:700">{s}</td>'

        return (
            '<table style="width:auto;min-width:520px;border-collapse:collapse;font-size:0.85rem;margin-top:6px">'
            '<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            '<th style="padding:5px 14px;text-align:left"></th>'
            '<th style="padding:5px 14px;text-align:right">件数</th>'
            '<th style="padding:5px 14px;text-align:right">総損益</th>'
            '<th style="padding:5px 14px;text-align:right">最大単発損失</th>'
            '<th style="padding:5px 14px;text-align:right">MaxDD(円)</th>'
            '</tr></thead><tbody>'
            f'<tr><td style="padding:5px 14px;text-align:left;color:#e2e8f0">現状(全部)</td>'
            f'<td style="padding:5px 14px;text-align:right;color:#94a3b8">{cur_st["n"]}</td>'
            f'{_cell(cur_st["total"])}{_cell(cur_st["worst"])}{_cell(cur_dd)}</tr>'
            f'<tr style="background:#052e16"><td style="padding:5px 14px;text-align:left;color:#e2e8f0">損切り幅>8%を除外</td>'
            f'<td style="padding:5px 14px;text-align:right;color:#94a3b8">{keep_st["n"]}<br>'
            f'<span style="font-size:0.7rem;color:#64748b">(-{dropped}件)</span></td>'
            f'{_cell(keep_st["total"])}{_cell(keep_st["worst"])}{_cell(keep_dd)}</tr>'
            '</tbody></table>')

    blocks = ""
    for bt_min, lbl in [(0, "全部"), (60, "BT60以上"), (70, "BT70以上")]:
        sub = [r for r in rows_all if (r[0] or 0) >= bt_min]
        tbl = _bucket_table(sub)
        if not tbl:
            continue
        sim = _sim_table(sub)
        blocks += (f'<h5 style="margin:14px 0 4px;color:#e2e8f0;font-size:0.82rem">{lbl}</h5>{tbl}'
                   f'{sim}')
    if not blocks:
        return ""
    return (
        f'<div style="margin:20px 0 0;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #38bdf8">'
        f'<h4 style="margin:0 0 6px;color:#38bdf8;font-size:0.9rem">'
        f'⑯ 損切り幅別 成績＋広損切り除外シミュレーション（完了トレード・直近{days}日）</h4>'
        f'<p style="margin:0 0 4px;color:#94a3b8;font-size:0.78rem">'
        f'完了トレードを損切り幅%（|損切り価格-注文価格|/注文価格＝ボラの代理）で帯分けし、'
        f'<b>利益側（勝ち）と損失側（負け）を分けて</b>集計。'
        f'<b>赤帯=損切り幅8%超（高ボラ銘柄）</b>。100株固定なので損切り幅が広い（高ボラ）ほど'
        f'1発の利益も損失も大きくなる。<b>「高ボラ帯は損失合計も大きいが利益合計はそれ以上に大きい」'
        f'なら、ボラの高さ自体は稼ぎの源</b>。下段は'
        f'<b>「損切り幅8%超を除外」した場合</b>の総損益・最大単発損失・MaxDDを現状と比較。'
        f'総損益をあまり削らずにテールだけ小さくできるなら、損切り幅フィルターが有効。</p>'
        f'{blocks}</div>'
    )


def build_open_direction_accuracy_html(days: int, workers: int = 1) -> str:
    """⑰ 寄り付き方向 予測精度（詳細分析タブ用）。
    夜間指標（前夜S&P500・CME日経先物ナイト）が、翌日の日経の
      ①寄りギャップ（始値/前日終値）
      ②寄り→引け（終値/始値）
      ③前日終値→当日終値
    の方向をどれだけ当てるかを的中率で測る。指標=上/下 それぞれの条件付き精度も出す。
    相場別（上昇/横ばい/下落）にも分割し「下落局面で翌日プラスを当てられるか」を見る。
    データは yfinance（^N225 / ^GSPC / NKD=F）。取得失敗時は空文字。"""
    import bisect
    from datetime import datetime as _dt, timedelta as _td
    try:
        import yfinance as _yf
        import pandas as _pd
    except Exception:
        return ""

    buf = max(int(days * 1.6) + 30, 120)
    _now = _dt.now()
    _start = (_now - _td(days=buf)).strftime("%Y-%m-%d")
    _end = (_now + _td(days=1)).strftime("%Y-%m-%d")

    def _ohlc(ticker):
        try:
            raw = _yf.Ticker(ticker).history(
                start=_start, end=_end, interval="1d",
                auto_adjust=False, actions=False)
            if raw is None or len(raw) < 30:
                return None
            return raw
        except Exception:
            return None

    n225 = _ohlc("^N225")
    if n225 is None:
        return ""

    def _dkey(idx):
        return idx.date() if hasattr(idx, "date") else idx

    # 日経: 日付→(open, close)
    n_dates = [_dkey(i) for i in n225.index]
    n_open = [float(x) for x in n225["Open"].values]
    n_close = [float(x) for x in n225["Close"].values]

    # 相場ラベル（既存 label_trend を流用）
    _tmap = {}
    try:
        _trend = label_trend(n225["Close"])
        _tmap = {_dkey(dt): tr for dt, tr in zip(_trend.index, _trend)}
    except Exception:
        _tmap = {}

    since = (_now - _td(days=days)).date()

    # 予測元: ticker → sorted[(date, daily_return)]
    def _ret_series(ticker):
        raw = _ohlc(ticker)
        if raw is None:
            return None
        cl = raw["Close"].astype(float)
        rets = cl.pct_change()
        out = [(_dkey(dt), float(r)) for dt, r in zip(raw.index, rets.values)
               if r == r]  # NaN除外
        out.sort(key=lambda x: x[0])
        return out

    predictors = []
    for tk, lbl in [("^GSPC", "前夜S&P500"), ("NKD=F", "CME日経先物ナイト")]:
        rs = _ret_series(tk)
        if rs and len(rs) >= 30:
            predictors.append((lbl, rs))
    if not predictors:
        return ""

    def _pred_before(rs, d):
        """JP営業日 d の寄り前に判明している最新の指標リターン（date < d の最後）。"""
        ds = [x[0] for x in rs]
        i = bisect.bisect_left(ds, d)
        if i == 0:
            return None
        return rs[i - 1][1]

    # JP各日の実測（②③は前日終値が必要なので index>=1 から）
    targets = [("寄りギャップ", "gap"), ("寄り→引け", "intra"), ("前日終値→当日終値", "full")]
    # 各(predictor, target)ごとに (pred_ret, actual_ret, trend) を貯める
    samples: dict = {}
    for plbl, _rs in predictors:
        for _, tk in targets:
            samples[(plbl, tk)] = []

    for k in range(1, len(n_dates)):
        d = n_dates[k]
        if d < since:
            continue
        prev_c = n_close[k - 1]
        op = n_open[k]
        cl = n_close[k]
        if prev_c <= 0 or op <= 0 or cl <= 0:
            continue
        actual = {
            "gap": op / prev_c - 1.0,
            "intra": cl / op - 1.0,
            "full": cl / prev_c - 1.0,
        }
        tr = _tmap.get(d)
        for plbl, rs in predictors:
            pr = _pred_before(rs, d)
            if pr is None or pr == 0:
                continue
            for _, tk in targets:
                av = actual[tk]
                if av == 0:
                    continue
                samples[(plbl, tk)].append((pr, av, tr))

    def _acc(pairs):
        n = len(pairs)
        if n == 0:
            return None
        hit = sum(1 for pr, av, _ in pairs if (pr > 0) == (av > 0))
        up = [av for pr, av, _ in pairs if pr > 0]
        dn = [av for pr, av, _ in pairs if pr < 0]
        up_prec = (sum(1 for a in up if a > 0) / len(up) * 100.0) if up else None
        dn_prec = (sum(1 for a in dn if a < 0) / len(dn) * 100.0) if dn else None
        return {"n": n, "acc": hit / n * 100.0,
                "n_up": len(up), "up_prec": up_prec,
                "n_dn": len(dn), "dn_prec": dn_prec}

    def _c(v):
        if v is None:
            return '<span style="color:#475569">—</span>'
        col = "#4ade80" if v >= 60 else ("#fbbf24" if v >= 52 else "#f87171")
        return f'<span style="color:{col};font-weight:700">{v:.0f}%</span>'

    blocks = ""
    for plbl, _rs in predictors:
        rows = ""
        for tlbl, tk in targets:
            st = _acc(samples[(plbl, tk)])
            if not st:
                continue
            rows += (
                f'<tr><td style="padding:5px 12px;text-align:left">{tlbl}</td>'
                f'<td style="padding:5px 12px;text-align:right;color:#94a3b8">{st["n"]}</td>'
                f'<td style="padding:5px 12px;text-align:right">{_c(st["acc"])}</td>'
                f'<td style="padding:5px 12px;text-align:right">{_c(st["up_prec"])}'
                f'<br><span style="font-size:0.68rem;color:#64748b">{st["n_up"]}日</span></td>'
                f'<td style="padding:5px 12px;text-align:right">{_c(st["dn_prec"])}'
                f'<br><span style="font-size:0.68rem;color:#64748b">{st["n_dn"]}日</span></td></tr>')
        if not rows:
            continue
        # 相場別（③前日終値→当日終値のみ）
        reg_rows = ""
        reg_samples = samples[(plbl, "full")]
        for rk, rlbl in [("up", "▲上昇"), ("sideways", "→横ばい"), ("down", "▼下落")]:
            sub = [(pr, av, tr) for pr, av, tr in reg_samples if tr == rk]
            st = _acc(sub)
            if not st:
                continue
            reg_rows += (
                f'<tr><td style="padding:4px 12px;text-align:left;color:#94a3b8">{rlbl}</td>'
                f'<td style="padding:4px 12px;text-align:right;color:#94a3b8">{st["n"]}</td>'
                f'<td style="padding:4px 12px;text-align:right">{_c(st["acc"])}</td>'
                f'<td style="padding:4px 12px;text-align:right">{_c(st["up_prec"])}'
                f'<br><span style="font-size:0.68rem;color:#64748b">{st["n_up"]}日</span></td></tr>')
        reg_tbl = ""
        if reg_rows:
            reg_tbl = (
                '<div style="margin:6px 0 0 12px"><span style="color:#64748b;font-size:0.72rem">'
                '相場別（③前日終値→当日終値）</span>'
                '<table style="width:auto;min-width:420px;border-collapse:collapse;font-size:0.8rem;margin-top:2px">'
                '<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.7rem">'
                '<th style="padding:4px 12px;text-align:left">相場</th>'
                '<th style="padding:4px 12px;text-align:right">日数</th>'
                '<th style="padding:4px 12px;text-align:right">的中率</th>'
                '<th style="padding:4px 12px;text-align:right">指標=上→実際上</th>'
                f'</tr></thead><tbody>{reg_rows}</tbody></table></div>')
        blocks += (
            f'<h5 style="margin:14px 0 4px;color:#e2e8f0;font-size:0.82rem">{plbl}</h5>'
            '<table style="width:auto;min-width:560px;border-collapse:collapse;font-size:0.85rem">'
            '<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            '<th style="padding:5px 12px;text-align:left">予測対象</th>'
            '<th style="padding:5px 12px;text-align:right">日数</th>'
            '<th style="padding:5px 12px;text-align:right">全体的中率</th>'
            '<th style="padding:5px 12px;text-align:right">指標=上→実際上</th>'
            '<th style="padding:5px 12px;text-align:right">指標=下→実際下</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>{reg_tbl}')

    if not blocks:
        return ""
    return (
        f'<div style="margin:20px 0 0;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #38bdf8">'
        f'<h4 style="margin:0 0 6px;color:#38bdf8;font-size:0.9rem">'
        f'⑰ 寄り付き方向 予測精度（夜間指標→翌日の日経・直近{days}日）</h4>'
        f'<p style="margin:0 0 4px;color:#94a3b8;font-size:0.78rem">'
        f'前夜に判明している指標（S&amp;P500の当日リターン / CME日経先物ナイト）が、'
        f'翌日の日経の方向をどれだけ当てるかを的中率で測定。'
        f'<b>①寄りギャップ</b>（始値/前日終値）は当たりやすいが寄り値に織り込み済み。'
        f'<b>②寄り→引け</b>と<b>③前日終値→当日終値</b>が当たるなら発注判断に使える。'
        f'色: <span style="color:#4ade80">≥60%</span> / '
        f'<span style="color:#fbbf24">52〜60%</span> / '
        f'<span style="color:#f87171">&lt;52%（コイン投げ以下）</span>。'
        f'「指標=上→実際上」は<b>指標が上を示した日に実際に上がった割合</b>＝'
        f'"上げ予測で多めに発注"の信頼度。</p>'
        f'{blocks}</div>'
    )


def build_drop_entry_html(days: int, workers: int) -> str:
    """⑱ 下落深さ別 成績（詳細分析タブ用）。
    各トレードのシグナル日時点で「日経が直前2営業日でどれだけ下げていたか」で
    帯分けし、利益側/損失側を分けて集計する。
    「深い持続下落（≤-4%＝今の局面）で約定した玉は勝つのか負けるのか」を実測し、
    "下げた後に買う"意味と、今の下げで新規を続けるべきかを数値で判断する。
    (sym,strat)単位で1回だけBT。日経は yfinance(^N225)。"""
    if not _SIGNALS_AVAILABLE:
        return ""

    seen: set = set()
    items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                items.append((sym, name, strat))
    if not items:
        return ""
    since = _TODAY - timedelta(days=days)

    # ── 日経の「直前2営業日リターン」マップ ──
    try:
        import yfinance as _yf
        from datetime import datetime as _dt, timedelta as _td
        buf = max(int(days * 1.6) + 30, 120)
        _s = (_dt.now() - _td(days=buf)).strftime("%Y-%m-%d")
        _e = (_dt.now() + _td(days=1)).strftime("%Y-%m-%d")
        _raw = _yf.Ticker("^N225").history(start=_s, end=_e, interval="1d",
                                           auto_adjust=False, actions=False)
        if _raw is None or len(_raw) < 10:
            return ""
        _ndates = [(i.date() if hasattr(i, "date") else i) for i in _raw.index]
        _nclose = [float(x) for x in _raw["Close"].values]
        _didx = {d: k for k, d in enumerate(_ndates)}
    except Exception:
        return ""

    import bisect

    def _drop2(sig_d):
        """シグナル日の直前2営業日リターン(%)。日経営業日に無ければ直近以前。"""
        if sig_d is None:
            return None
        k = _didx.get(sig_d)
        if k is None:
            j = bisect.bisect_right(_ndates, sig_d) - 1
            if j < 0:
                return None
            k = j
        if k < 2:
            return None
        base = _nclose[k - 2]
        if base <= 0:
            return None
        return (_nclose[k] / base - 1.0) * 100.0

    def _one(sym, name, strat) -> list:
        mod = _mod_for(strat)
        try:
            res = mod.backtest_one(sym, name, strat)
            if not res:
                return []
            pr = res.get("period_results", {})
            if not pr:
                return []
            try:
                bt = mod.calc_recommend_score(pr)[0]
            except Exception:
                bt = 0
            trade_log = pr[max(pr.keys())].get("trade_log", [])
            out = []
            tseen: set = set()
            for t in trade_log:
                reason = t.get("reason", "") or ""
                if reason in ("", "発注中", "保有中"):
                    continue
                exit_dt = t.get("exit_dt")
                if exit_dt is None:
                    continue
                exit_d = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
                if exit_d < since:
                    continue
                k = (t.get("entry_dt"), exit_dt)
                if k in tseen:
                    continue
                tseen.add(k)
                sdt = t.get("signal_dt")
                sd = sdt.date() if hasattr(sdt, "date") else sdt
                dr = _drop2(sd)
                if dr is None:
                    continue
                out.append((bt, dr, t.get("pnl", 0)))
            return out
        except Exception:
            return []

    rows_all: list = []
    with _TPE(max_workers=workers) as ex:
        futs = [ex.submit(_one, s, n, st) for s, n, st in items]
        for fut in _asc(futs):
            try:
                rows_all.extend(fut.result() or [])
            except Exception:
                pass
    if not rows_all:
        return ""

    # 直前2日リターンの帯（下→上）
    BUCKETS = [
        (-1e9, -4, "≤ -4%（急落・今の局面）", True),
        (-4, -2, "-4〜-2%（強い下げ）", True),
        (-2, -1, "-2〜-1%（下げ）", False),
        (-1, 0, "-1〜0%（小幅安）", False),
        (0, 2, "0〜+2%（上げ）", False),
        (2, 1e9, "+2%以上（急騰）", False),
    ]

    def _stats(trs):
        pnls = [p for (_b, _r, p) in trs]
        n = len(pnls)
        if n == 0:
            return None
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gp, gl = sum(wins), -sum(losses)
        pf = (gp / gl) if gl > 0 else float("inf")
        return {"n": n, "wr": len(wins) / n * 100.0, "pf": pf,
                "nw": len(wins), "gp": gp, "avgw": gp / len(wins) if wins else 0,
                "nl": len(losses), "gl": -gl,
                "avgl": (-gl / len(losses)) if losses else 0,
                "total": sum(pnls), "avg": sum(pnls) / n}

    def _pf(pf):
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    def _table(trs):
        rows = ""
        for lo, hi, lbl, deep in BUCKETS:
            sub = [r for r in trs if lo <= r[1] < hi]
            st = _stats(sub)
            if not st:
                continue
            tc = "#4ade80" if st["total"] >= 0 else "#f87171"
            bg = "background:#2d0a0a;" if deep else ""
            rows += (
                f'<tr style="{bg}"><td style="padding:5px 10px;text-align:left">{lbl}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#94a3b8">{st["n"]}</td>'
                f'<td style="padding:5px 10px;text-align:right">{st["wr"]:.0f}%</td>'
                f'<td style="padding:5px 10px;text-align:right">{_pf(st["pf"])}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#4ade80;'
                f'border-left:1px solid #334155">{st["nw"]}件</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#4ade80;font-weight:700">{st["gp"]:+,.0f}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#f87171;'
                f'border-left:1px solid #334155">{st["nl"]}件</td>'
                f'<td style="padding:5px 10px;text-align:right;color:#f87171;font-weight:700">{st["gl"]:+,.0f}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:{tc};font-weight:700;'
                f'border-left:1px solid #334155">{st["total"]:+,.0f}</td>'
                f'<td style="padding:5px 10px;text-align:right;color:{tc}">{st["avg"]:+,.0f}</td></tr>')
        if not rows:
            return ""
        return (
            '<div style="overflow-x:auto">'
            '<table style="width:auto;min-width:760px;border-collapse:collapse;font-size:0.85rem">'
            '<thead><tr style="color:#64748b;font-size:0.7rem">'
            '<th colspan="4"></th>'
            '<th colspan="2" style="padding:3px 10px;text-align:center;color:#4ade80;'
            'border-left:1px solid #334155">利益側</th>'
            '<th colspan="2" style="padding:3px 10px;text-align:center;color:#f87171;'
            'border-left:1px solid #334155">損失側</th>'
            '<th colspan="2" style="border-left:1px solid #334155"></th></tr>'
            '<tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            '<th style="padding:5px 10px;text-align:left">シグナル日の直前2日 日経</th>'
            '<th style="padding:5px 10px;text-align:right">件数</th>'
            '<th style="padding:5px 10px;text-align:right">勝率</th>'
            '<th style="padding:5px 10px;text-align:right">PF</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">勝件数</th>'
            '<th style="padding:5px 10px;text-align:right">利益合計</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">負件数</th>'
            '<th style="padding:5px 10px;text-align:right">損失合計</th>'
            '<th style="padding:5px 10px;text-align:right;border-left:1px solid #334155">総損益</th>'
            '<th style="padding:5px 10px;text-align:right">平均</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>')

    blocks = ""
    for bt_min, lbl in [(0, "全部"), (60, "BT60以上"), (70, "BT70以上")]:
        sub = [r for r in rows_all if (r[0] or 0) >= bt_min]
        tbl = _table(sub)
        if tbl:
            blocks += f'<h5 style="margin:14px 0 4px;color:#e2e8f0;font-size:0.82rem">{lbl}</h5>{tbl}'
    if not blocks:
        return ""
    return (
        f'<div style="margin:20px 0 0;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #38bdf8">'
        f'<h4 style="margin:0 0 6px;color:#38bdf8;font-size:0.9rem">'
        f'⑱ 下落深さ別 成績（"下げた後に買う"の検証・直近{days}日）</h4>'
        f'<p style="margin:0 0 4px;color:#94a3b8;font-size:0.78rem">'
        f'各トレードの<b>シグナル日時点で、日経が直前2営業日でどれだけ動いていたか</b>で帯分けし、'
        f'利益側/損失側を分けて集計。<b>赤帯=急落後（≤-4%＝今の局面）に約定した玉</b>。'
        f'<b>「急落後の帯が総損益プラス」なら"下げた後に買う"は有効</b>（反発を取れている）、'
        f'<b>マイナスなら落ちるナイフを掴んでいる</b>＝深い下落中は新規を絞るべき。'
        f'今の相場（直前2日で約-4%）が、過去に約定した玉でどうだったかを直接読める。</p>'
        f'{blocks}</div>'
    )


def build_em_comparison_html(days: int, workers: int, ems=(0.0, 0.5, 1.0),
                             recent_days: int = 15) -> str:
    """㉑ 逆指値の高さ(em)別 成績＋直近の取引がどうなるか（詳細分析タブ用）。
    同一シグナルで em（前日終値からの上乗せ・ATR単位）だけ変えて再バックテスト。
      em=0   ＝ トリガー前日終値（現行・約定しやすい）
      em=0.5 ＝ 前日終値+0.5ATR（≒+1%前後・強めの上抜けのみ約定）
      em=1.0 ＝ 前日終値+1.0ATR（≒+2%前後・かなり強い上抜けのみ約定）
    上段: em別の約定率/勝率/PF/総損益（全・BT70）。
    下段: 直近{recent_days}日に約定した実トレードが、em>0 なら約定したか/回避されたかを個別表示。
    → 今週の下げ銘柄が em>0 で回避できたかを直接読める。"""
    if not _SIGNALS_AVAILABLE:
        return ""
    seen: set = set()
    items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                items.append((sym, name, strat))
    if not items:
        return ""

    from backtest_limit_entry import fetch as _fetch, run_limit_backtest as _rlb

    # BTスコア
    bt: dict = {}
    def _btone(it):
        sym, name, strat = it
        mod = _mod_for(strat)
        try:
            if strat not in getattr(mod, "STRATEGY_PARAMS", {}):
                return (sym, strat, 0)
            r = mod.backtest_one(sym, name, strat)
            if not r:
                return (sym, strat, 0)
            sc, _rk = mod.calc_recommend_score(r.get("period_results", {}))
            return (sym, strat, sc)
        except Exception:
            return (sym, strat, 0)
    print("  [em比較] BTスコア算出中...", flush=True)
    with _TPE(max_workers=workers) as ex:
        for sym, strat, sc in ex.map(_btone, items):
            bt[(sym, strat)] = sc

    from datetime import date as _date
    since_recent = _TODAY - timedelta(days=recent_days)
    ems = list(ems)

    # em変種ごとに再バックテスト → 約定トレードを収集
    per_em: dict = {}          # em -> [(sym, strat, signals, filled_trades)]
    for em in ems:
        def _one(it, em=em):
            sym, name, strat = it
            mod = _mod_for(strat)
            params = getattr(mod, "STRATEGY_PARAMS", {})
            if strat not in params:
                return None
            cf, _em0, sm, tm = params[strat]
            df = _fetch(sym, days)
            if df is None:
                return None
            try:
                r = _rlb(sym, name, df, cf, em, sm, tm, days, strat, entry_type="stop")
            except Exception:
                return None
            if not r:
                return None
            filled = [t for t in r.get("trade_log", [])
                      if t.get("entry_dt") is not None
                      and t.get("reason") not in ("発注中", "保有中", "")]
            return (sym, strat, r.get("signals", 0), name, filled)
        print(f"  [em比較] em={em} 集計中...", flush=True)
        res = []
        with _TPE(max_workers=workers) as ex:
            for x in ex.map(_one, items):
                if x:
                    res.append(x)
        per_em[em] = res

    def _sigkey(t, sym, strat):
        sdt = t.get("signal_dt")
        sd = sdt.date() if hasattr(sdt, "date") else sdt
        return (sym, strat, sd)

    # em -> {sigkey: (pnl, reason, entry_p, exit_d)}
    fmap: dict = {em: {} for em in ems}
    for em in ems:
        for sym, strat, _s, _nm, filled in per_em[em]:
            for t in filled:
                fmap[em][_sigkey(t, sym, strat)] = (
                    t.get("pnl", 0), t.get("reason", ""),
                    t.get("entry_p", 0),
                    (t.get("exit_dt").date() if hasattr(t.get("exit_dt"), "date")
                     else t.get("exit_dt")))

    # ── 上段: em別サマリー ──
    def _agg(em, only_bt70):
        sig = 0
        pnls = []
        for sym, strat, s, _nm, filled in per_em[em]:
            if only_bt70 and bt.get((sym, strat), 0) < 70:
                continue
            sig += s
            pnls += [t.get("pnl", 0) for t in filled]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        gl = -sum(p for p in pnls if p <= 0)
        gp = sum(wins)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        return dict(signals=sig, filled=n,
                    fill=(n / sig * 100 if sig else 0.0),
                    wr=(len(wins) / n * 100 if n else 0.0),
                    pf=pf, total=sum(pnls))

    def _summary_table(only_bt70):
        base = _agg(ems[0], only_bt70)["total"]
        rows = ""
        for em in ems:
            st = _agg(em, only_bt70)
            pf_s = "∞" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
            tc = "#4ade80" if st["total"] >= 0 else "#f87171"
            diff = st["total"] - base
            dc = "#4ade80" if diff >= 0 else "#f87171"
            lbl = ("em=0（現行/前日終値）" if em == 0 else
                   f"em=+{em}ATR（≒+{int(em*2)}%前後）")
            rows += (
                f'<tr><td style="padding:5px 12px;text-align:left">{lbl}</td>'
                f'<td style="padding:5px 12px;text-align:right;color:#94a3b8">{st["filled"]}/{st["signals"]}</td>'
                f'<td style="padding:5px 12px;text-align:right">{st["fill"]:.0f}%</td>'
                f'<td style="padding:5px 12px;text-align:right">{st["wr"]:.0f}%</td>'
                f'<td style="padding:5px 12px;text-align:right">{pf_s}</td>'
                f'<td style="padding:5px 12px;text-align:right;color:{tc};font-weight:700">{st["total"]:+,.0f}</td>'
                f'<td style="padding:5px 12px;text-align:right;color:{dc}">'
                f'{("基準" if em == ems[0] else f"{diff:+,.0f}")}</td></tr>')
        return (
            '<table style="width:auto;min-width:600px;border-collapse:collapse;font-size:0.85rem">'
            '<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.72rem">'
            '<th style="padding:5px 12px;text-align:left">逆指値の高さ</th>'
            '<th style="padding:5px 12px;text-align:right">約定/シグナル</th>'
            '<th style="padding:5px 12px;text-align:right">約定率</th>'
            '<th style="padding:5px 12px;text-align:right">勝率</th>'
            '<th style="padding:5px 12px;text-align:right">PF</th>'
            '<th style="padding:5px 12px;text-align:right">総損益</th>'
            '<th style="padding:5px 12px;text-align:right">em=0比</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')

    # ── 下段: 直近の実トレードが em>0 でどうなるか ──
    base_recent = []
    for k, (pnl, reason, ep, exit_d) in fmap[ems[0]].items():
        if exit_d is not None and exit_d >= since_recent:
            base_recent.append((k, pnl, reason, ep, exit_d))
    # name参照
    _name_of = {(s, st): nm for em in [ems[0]] for s, st, _sg, nm, _f in per_em[em]}
    base_recent.sort(key=lambda x: (x[4] or _date.min), reverse=True)

    recent_rows = ""
    for (sym, strat, sd), pnl, reason, ep, exit_d in base_recent:
        b = bt.get((sym, strat), 0)
        nm = _name_of.get((sym, strat), "")
        tc0 = "#4ade80" if pnl >= 0 else "#f87171"
        cells = (
            f'<td style="padding:4px 10px;text-align:left">{sym} '
            f'<span style="color:#64748b;font-size:0.72rem">{nm[:8]} {strat} BT{b}</span></td>'
            f'<td style="padding:4px 10px;text-align:center;color:#94a3b8">{sd}</td>'
            f'<td style="padding:4px 10px;text-align:right;color:{tc0};font-weight:700">{pnl:+,.0f}<br>'
            f'<span style="font-size:0.68rem;color:#64748b">{reason}</span></td>')
        for em in ems[1:]:
            hit = fmap[em].get((sym, strat, sd))
            if hit is None:
                cells += ('<td style="padding:4px 10px;text-align:center;color:#4ade80;'
                          'font-weight:700;background:#052e16">回避<br>'
                          '<span style="font-size:0.68rem;color:#64748b">約定せず</span></td>')
            else:
                p2, r2, _e2, _x2 = hit
                c2 = "#4ade80" if p2 >= 0 else "#f87171"
                cells += (f'<td style="padding:4px 10px;text-align:right;color:{c2};font-weight:700">'
                          f'{p2:+,.0f}<br><span style="font-size:0.68rem;color:#64748b">{r2}</span></td>')
        recent_rows += f'<tr>{cells}</tr>'

    recent_tbl = ""
    if recent_rows:
        heads = ''.join(
            f'<th style="padding:5px 10px;text-align:right">em=+{em}ATR</th>' for em in ems[1:])
        recent_tbl = (
            f'<h5 style="margin:16px 0 4px;color:#e2e8f0;font-size:0.82rem">'
            f'直近{recent_days}日に約定した実トレード（em=0）が、em>0でどうなるか</h5>'
            '<div style="overflow-x:auto"><table style="width:auto;min-width:560px;'
            'border-collapse:collapse;font-size:0.82rem">'
            '<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.7rem">'
            '<th style="padding:5px 10px;text-align:left">銘柄/戦略</th>'
            '<th style="padding:5px 10px;text-align:center">シグナル日</th>'
            '<th style="padding:5px 10px;text-align:right">em=0（実際）</th>'
            f'{heads}</tr></thead><tbody>{recent_rows}</tbody></table></div>'
            '<p style="margin:6px 0 0;color:#64748b;font-size:0.72rem">'
            '「回避」= em を上げたトリガーに高値が届かず約定しなかった（＝その損益が消える）。'
            '損失トレードが「回避」なら em>0 で今週の負けを避けられたことを意味する。'
            'ただし勝ちトレードが「回避」になっていないか（利益も消えていないか）を必ず確認。</p>')

    return (
        f'<div style="margin:20px 0 0;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #38bdf8">'
        f'<h4 style="margin:0 0 6px;color:#38bdf8;font-size:0.9rem">'
        f'㉑ 逆指値の高さ(em)別 成績＋今週の取引がどうなるか（直近{days}日）</h4>'
        f'<p style="margin:0 0 8px;color:#94a3b8;font-size:0.78rem">'
        f'同一シグナルで<b>逆指値トリガーの高さ(em)だけ</b>変えて再計算。'
        f'<b>em=0=前日終値（現行・約定しやすい）</b>／em=+0.5〜1.0ATR=強い上抜けのみ約定（≒+1〜2%）。'
        f'em を上げると<b>ダマシの弱い上抜けを弾ける</b>が、<b>本物の反発の初動や勝ちトレードも一部逃す</b>。'
        f'総損益が下がらずに済むかがカギ。</p>'
        f'<h5 style="margin:8px 0 4px;color:#e2e8f0;font-size:0.82rem">全部</h5>{_summary_table(False)}'
        f'<h5 style="margin:12px 0 4px;color:#e2e8f0;font-size:0.82rem">BT70以上</h5>{_summary_table(True)}'
        f'{recent_tbl}</div>'
    )


def build_pullback_comparison_html(pullbacks: list[float], days: int,
                                   workers: int) -> str:
    """押し目指値買い vs 逆指値ブレイク買い の比較セクション（詳細分析タブ用）。

    同一シグナルで entry だけ変える:
      逆指値ブレイク(現行) = entry_type=stop, em=0  (前日終値で約定)
      押し目指値          = entry_type=limit, em=PB (前日終値-ATR×PB で約定)
    『全トレード』と『BT70以上の銘柄のみ』の2表を出す。
    対象は本レポートの _PNL_CONFIGS（=実際にトレードする選定銘柄）。
    """
    if not _SIGNALS_AVAILABLE or not pullbacks:
        return ""
    seen: set = set()
    items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                items.append((sym, name, strat))
    if not items:
        return ""

    from backtest_limit_entry import fetch as _fetch, run_limit_backtest as _rlb

    # ── BTスコア (production: stop・365スライス) を (sym,strat) ごとに算出 ──
    bt: dict = {}
    def _btone(it):
        sym, name, strat = it
        mod = _mod_for(strat)
        try:
            if strat not in getattr(mod, "STRATEGY_PARAMS", {}):
                return (sym, strat, 0)
            r = mod.backtest_one(sym, name, strat)
            if not r:
                return (sym, strat, 0)
            sc, _rk = mod.calc_recommend_score(r.get("period_results", {}))
            return (sym, strat, sc)
        except Exception:
            return (sym, strat, 0)
    print("  [押し目比較] BTスコア算出中...", flush=True)
    with _TPE(max_workers=workers) as ex:
        for sym, strat, sc in ex.map(_btone, items):
            bt[(sym, strat)] = sc

    variants = [("逆指値ブレイク(現行)", "stop", 0.0)]
    for pb in pullbacks:
        variants.append((f"押し目指値 -{pb}ATR", "limit", float(pb)))

    per_variant: dict = {}
    for label, etype, em in variants:
        def _one(it, etype=etype, em=em):
            sym, name, strat = it
            mod = _mod_for(strat)
            params = getattr(mod, "STRATEGY_PARAMS", {})
            if strat not in params:
                return None
            cf, _em0, sm, tm = params[strat]
            df = _fetch(sym, days)
            if df is None:
                return None
            try:
                r = _rlb(sym, name, df, cf, em, sm, tm, days, strat,
                         entry_type=etype)
            except Exception:
                return None
            if not r:
                return None
            tl = [t for t in r.get("trade_log", [])
                  if t.get("reason") not in ("発注中", "保有中")]
            return (sym, strat, r.get("signals", 0), len(tl), tl)
        print(f"  [押し目比較] {label} 集計中...", flush=True)
        res = []
        with _TPE(max_workers=workers) as ex:
            for x in ex.map(_one, items):
                if x:
                    res.append(x)
        per_variant[label] = res

    def _agg(label: str, only_bt70: bool) -> dict:
        sig = fil = 0
        trades: list = []
        for sym, strat, s, f, tl in per_variant[label]:
            if only_bt70 and bt.get((sym, strat), 0) < 70:
                continue
            sig += s; fil += f; trades += tl
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        gp = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        return dict(signals=sig, filled=fil,
                    fill_rate=fil / sig * 100 if sig else 0.0,
                    win_rate=len(wins) / len(trades) * 100 if trades else 0.0,
                    pf=pf, total_pnl=sum(t.get("pnl", 0) for t in trades),
                    avg_win=gp / len(wins) if wins else 0.0)

    def _table(only_bt70: bool) -> str:
        base = _agg(variants[0][0], only_bt70)["total_pnl"]
        best = max(variants, key=lambda v: _agg(v[0], only_bt70)["total_pnl"])[0]
        rows = ""
        for label, _e, _m in variants:
            st = _agg(label, only_bt70)
            is_base = (label == variants[0][0])
            diff = st["total_pnl"] - base
            pf_s = "∞" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
            pnl = st["total_pnl"]
            pc = "#4ade80" if pnl > 0 else "#f87171" if pnl < 0 else "#94a3b8"
            dc = "#94a3b8" if is_base else ("#4ade80" if diff > 0 else "#f87171")
            ds = "—" if is_base else (f"+{diff:,.0f}" if diff >= 0 else f"{diff:,.0f}")
            bg = "background:#172032;" if label == best else ""
            badge = ('' if label != best else
                     ' <span style="font-size:0.6rem;background:#4ade80;color:#052e16;'
                     'padding:1px 4px;border-radius:3px">最良</span>')
            rows += (
                f'<tr style="{bg}">'
                f'<td style="padding:6px 10px;font-weight:700;color:#e2e8f0">{label}{badge}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{st["signals"]:,}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{st["filled"]:,}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{st["fill_rate"]:.0f}%</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{st["win_rate"]:.0f}%</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#93c5fd">{pf_s}</td>'
                f'<td style="padding:6px 10px;text-align:right;font-weight:700;color:{pc}">{pnl:+,.0f}円</td>'
                f'<td style="padding:6px 10px;text-align:right;color:{dc}">{ds}</td>'
                f'<td style="padding:6px 10px;text-align:right;color:#94a3b8">{st["avg_win"]:+,.0f}</td>'
                f'</tr>\n'
            )
        return (
            f'<table style="width:100%;border-collapse:collapse;font-size:0.88rem">'
            f'<thead><tr style="border-bottom:1px solid #334155;color:#64748b;font-size:0.78rem">'
            f'<th style="padding:5px 10px;text-align:left">エントリー方式</th>'
            f'<th style="padding:5px 10px;text-align:right">ｼｸﾞﾅﾙ</th>'
            f'<th style="padding:5px 10px;text-align:right">約定</th>'
            f'<th style="padding:5px 10px;text-align:right">fill率</th>'
            f'<th style="padding:5px 10px;text-align:right">勝率</th>'
            f'<th style="padding:5px 10px;text-align:right">PF</th>'
            f'<th style="padding:5px 10px;text-align:right">総損益</th>'
            f'<th style="padding:5px 10px;text-align:right">対現行</th>'
            f'<th style="padding:5px 10px;text-align:right">平均利益</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )

    return (
        f'<div style="margin:0 0 16px;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #34d399">'
        f'<h4 style="margin:0 0 8px;color:#34d399;font-size:0.95rem">'
        f'📥 押し目指値買い vs 逆指値ブレイク買い（全トレード・直近{days}日）</h4>'
        f'<p style="margin:0 0 10px;color:#94a3b8;font-size:0.76rem">'
        f'同一シグナルで entry のみ変更。押し目=前日終値−ATR×n を指値買い。'
        f'「対現行」がプラス かつ PF改善 なら押し目買いが優位。'
        f'マイナスなら、押さず上昇する強い勝ちを取り逃す方が大きい。</p>'
        f'{_table(False)}</div>'
        f'<div style="padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #fbbf24">'
        f'<h4 style="margin:0 0 10px;color:#fbbf24;font-size:0.95rem">'
        f'📥 同上 — BT70以上の銘柄のみ</h4>'
        f'{_table(True)}</div>'
    )


def build_sameday_tpsl_sweep_html(days: int, workers: int,
                                  inverted: bool = True,
                                  sm_list: list | None = None,
                                  tm_list: list | None = None) -> str:
    """㉓ 同日決済(日計り)向け 損切/利確幅スイープ（mirror/lss 用の詳細分析）。

    現モード(mirror/lss = max_hold を 0 に強制)のまま、各戦略の (sm, tm)=
    (損切ATR倍率, 利確ATR倍率) を総当りで差し替えて同日決済し、損益/PF/勝率を
    2次元グリッドで比較する。スイング用の広い幅ではなく、その日のうちに当たる
    現実的な幅(≈0.3〜2ATR)の中で最適点を探すためのタブ。

    ※ run_limit_backtest は唯一の約定エンジン。mirror/lss の pnl反転・entry_type・
       max_hold=0 はモジュールフラグで全体に効いているので、ここでは (sm,tm) を
       変えて呼ぶだけで“そのモードの同日決済”を測れる。
    """
    if not _SIGNALS_AVAILABLE:
        return ""
    # 5分足版と同じ軸(0.1〜1.0)にして直接見比べられるようにする。狭い側(0.1)ほど
    # 同日で当たりやすく最適が寄りがちなので必ず含める。
    sm_list = sm_list or [0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0]
    tm_list = tm_list or [0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0]

    seen: set = set()
    items: list[tuple] = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat))
                items.append((sym, name, strat))
    if not items:
        return ""

    from backtest_limit_entry import fetch as _fetch, run_limit_backtest as _rlb
    since = _TODAY - timedelta(days=days)

    def _run(sm: float, tm: float) -> list:
        def _one(it, sm=sm, tm=tm) -> list:
            sym, name, strat = it
            mod = _mod_for(strat)
            p = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
            if not p:
                return []
            cf, em, _sm0, _tm0 = p
            df = _fetch(sym, days)
            if df is None:
                return []
            try:
                r = _rlb(sym, name, df, cf, em, sm, tm, days, strat,
                         entry_type=getattr(mod, "ENTRY_TYPE", "stop"))
            except Exception:
                return []
            if not r:
                return []
            out = []
            for t in r.get("trade_log", []):
                if t.get("reason") in ("発注中", "保有中"):
                    continue
                ed = t.get("exit_dt")
                ed = ed.date() if hasattr(ed, "date") else ed
                if ed is None or ed < since:
                    continue
                out.append((t.get("pnl", 0) or 0, t.get("reason", "") or ""))
            return out
        trades: list = []
        with _TPE(max_workers=workers) as ex:
            for x in ex.map(_one, items):
                trades += x
        return trades

    def _stat(trades: list) -> dict:
        pnls = [p for p, _r in trades]
        n = len(pnls)
        if n == 0:
            return {"n": 0, "pnl": 0.0, "pf": 0.0, "wr": 0.0,
                    "tgt": 0, "stp": 0, "tc": 0}
        wins = [p for p in pnls if p > 0]
        gp = sum(wins)
        gl = -sum(p for p in pnls if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        # 決済理由の内訳(目標達成/損切り/タイムカット)
        tgt = sum(1 for _p, r in trades if r == "目標達成")
        stp = sum(1 for _p, r in trades if r == "損切り")
        tc  = sum(1 for _p, r in trades if r == "タイムカット")
        return {"n": n, "pnl": sum(pnls), "pf": pf,
                "wr": len(wins) / n * 100.0, "tgt": tgt, "stp": stp, "tc": tc}

    # baseline: ほぼ発火しない広い幅(=実質「引け決済のみ」)。
    # 極大値(99等)にすると sp=lp-atr*sm が負になり、エンジンの妥当性チェック
    # (sp>0 / tp>0)で全シグナルが弾かれ0取引になる(MAX_ATR_RATIO=0.20 のため
    # sm<5 でないと sp が負)。同日ではまず当たらない 4ATR を使う。
    # スイープ中は sm/tm 強制上書き(--mirror-sm/--mirror-tm による適用値)を一時解除。
    # そうしないと全マスが適用値で計算され、総当りグリッドの意味が消える。
    import backtest_limit_entry as _blm
    _saved_sm_force, _saved_tm_force = getattr(_blm, "_SM_FORCE", None), getattr(_blm, "_TM_FORCE", None)
    _blm._SM_FORCE = None
    _blm._TM_FORCE = None
    try:
        _BASE_W = 4.0
        print(f"  [同日TP/SL] baseline(≈引け決済 {_BASE_W}ATR幅) 集計中...", flush=True)
        base = _stat(_run(_BASE_W, _BASE_W))

        grid: dict = {}   # (sm,tm) -> stat
        best_key = None
        for tm in tm_list:
            for sm in sm_list:
                print(f"  [同日TP/SL] sm{sm}/tm{tm} 集計中...", flush=True)
                st = _stat(_run(sm, tm))
                grid[(sm, tm)] = st
                if st["n"] > 0 and (best_key is None or st["pnl"] > grid[best_key]["pnl"]):
                    best_key = (sm, tm)
    finally:
        _blm._SM_FORCE = _saved_sm_force
        _blm._TM_FORCE = _saved_tm_force

    def _pf(pf) -> str:
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    # ── グリッド表(行=sm, 列=tm)。セル=総損益、下段にPF/勝率 ──
    _th = 'padding:5px 8px;text-align:center;color:#94a3b8;font-size:0.72rem'
    head = f'<th style="{_th};text-align:left">損切ATR＼利確ATR</th>' + "".join(
        f'<th style="{_th}">tm={tm}</th>' for tm in tm_list)
    body = ""
    for sm in sm_list:
        cells = f'<td style="{_th};text-align:left;color:#e2e8f0;font-weight:700">sm={sm}</td>'
        for tm in tm_list:
            st = grid[(sm, tm)]
            if st["n"] == 0:
                cells += f'<td style="{_th}">—</td>'
                continue
            is_best = (best_key == (sm, tm))
            pc = "#4ade80" if st["pnl"] > 0 else "#f87171" if st["pnl"] < 0 else "#94a3b8"
            bg = "background:#0e3320;" if is_best else ("background:#101826;" if st["pnl"] > 0 else "")
            star = " ★" if is_best else ""
            cells += (
                f'<td style="padding:5px 8px;text-align:center;{bg}">'
                f'<div style="color:{pc};font-weight:700;font-size:0.82rem">{st["pnl"]:+,.0f}{star}</div>'
                f'<div style="color:#64748b;font-size:0.66rem">PF{_pf(st["pf"])}/{st["wr"]:.0f}%</div>'
                f'</td>'
            )
        body += f'<tr>{cells}</tr>'

    bk = best_key
    best_line = ""
    if bk:
        b = grid[bk]
        d = b["pnl"] - base["pnl"]
        best_line = (
            f'<p style="margin:10px 0 4px;font-size:0.9rem">'
            f'★ 最適: <b style="color:#4ade80">損切ATR={bk[0]} / 利確ATR={bk[1]}</b> → '
            f'<b style="color:{"#4ade80" if b["pnl"]>=0 else "#f87171"}">{b["pnl"]:+,.0f}円</b> '
            f'(PF{_pf(b["pf"])} / 勝率{b["wr"]:.0f}% / {b["n"]}取引) '
            f'&nbsp;<span style="color:#94a3b8;font-size:0.8rem">'
            f'目標{b["tgt"]}/損切{b["stp"]}/引け{b["tc"]}件</span><br>'
            f'<span style="color:#94a3b8;font-size:0.8rem">'
            f'「ほぼ引け決済(4ATR幅)」比 {("+"+format(d,",.0f")) if d>=0 else format(d,",.0f")}円 '
            f'(4ATR幅: {base["pnl"]:+,.0f}円 / PF{_pf(base["pf"])} / 勝率{base["wr"]:.0f}% / {base["n"]}取引)'
            f'</span></p>'
        )

    inv_note = (
        'ミラーでは long の <b>損切(sm)=ミラーの利確</b> / long の <b>利確(tm)=ミラーの損切</b> '
        'に対応します（符号反転）。表の sm/tm はエンジンに渡す long 基準の ATR 倍率です。'
        if inverted else
        'sm=損切ATR倍率 / tm=利確ATR倍率（この空売りの実際の損切/利確幅）。'
    )

    return (
        f'<div style="margin:0 0 16px;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #a78bfa">'
        f'<h4 style="margin:0 0 6px;color:#c4b5fd;font-size:0.95rem">'
        f'🎯 同日決済(日計り)向け 損切/利確幅スイープ（直近{days}日 / 対象{len(items)}銘柄）</h4>'
        f'<p style="margin:0 0 10px;color:#94a3b8;font-size:0.76rem">'
        f'その日のうちに決済する前提で、(損切ATR, 利確ATR) を総当りで変えて同日決済した'
        f'ときの総損益を比較。スイング用の広い幅(sm1.5/tm3.0等)は同日ではほぼ発火せず'
        f'「引け決済のみ」に等しくなるので、当たる幅(≈0.3〜1ATR)を探すためのタブ。<br>'
        f'{inv_note}</p>'
        f'{best_line}'
        f'<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.8rem">'
        f'<thead><tr style="border-bottom:1px solid #334155">{head}</tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
        f'<p style="margin:8px 0 0;color:#64748b;font-size:0.72rem">'
        f'各セル上段=総損益 / 下段=PF・勝率。★=最良。'
        f'「目標/損切/引け件数」は最適セルの決済理由内訳。</p>'
        f'</div>'
    )


def build_sameday_5m_sweep_html(days: int, workers: int, is_mirror: bool,
                                sm_list: list | None = None,
                                tm_list: list | None = None,
                                source: str = "auto", slip: float = 0.0) -> str:
    """㉔ 同日TP/SL最適化を「5分足」で総当り(mirror/lss 用)。

    日足スイープは同日の TP/SL どちらが先かを日足高安だけで判定する近似。ここでは
    5分足の実際の値動き順で first-touch 判定して正確な同日損益を出す。ロジックは
    sameday5m_core(スタンドアロン検証ツールと共通)に集約=二重実装しない。
    5分足が無い銘柄は集計から除外し、カバレッジを明示する。
    """
    if not _SIGNALS_AVAILABLE:
        return ""
    try:
        import sameday5m_core as _c5
    except Exception as _e:
        return (f'<div style="padding:16px;color:#f87171">5分足コア(sameday5m_core)の'
                f'読み込みに失敗しました: {_e}</div>')
    sm_list = sm_list or [0.1, 0.15, 0.2, 0.3, 0.5, 1.0]
    tm_list = tm_list or [0.1, 0.15, 0.2, 0.3, 0.5, 1.0]
    cells = [(sm, tm) for tm in tm_list for sm in sm_list]

    seen: set = set()
    items: list = []
    for cfg in _PNL_CONFIGS:
        for sym, name, strat in cfg.get("stop_wl", []) + cfg.get("brk_wl", []):
            if (sym, strat) not in seen:
                seen.add((sym, strat)); items.append((sym, name, strat))
    if not items:
        return ""

    import backtest_limit_entry as _blm
    from backtest_limit_entry import FIXED_QTY as _QTY, FEE_PCT_ONE_WAY as fee
    agg = {c: [] for c in cells}
    n_with_5m = 0

    # ── 最適値キャッシュ ─────────────────────────────────────────────────
    # 5分足スイープは重い(sm×tm × 全銘柄 × 5分足)。一度計算したら結果(stats)を保存し、
    # 2回目以降は計算せずキャッシュを表示する。再計算したいときだけ SAMEDAY5M_RESWEEP=1。
    import os as _os5, pickle as _pk5
    from datetime import datetime as _dt5
    from pathlib import Path as _P5
    _mode5 = _os5.getenv("TRADING_MODE", "conservative")
    _fam5 = "mirror" if is_mirror else "lss"
    _cdir5 = _P5(__file__).resolve().parent / ".sameday5m_cache"
    _cf5 = _cdir5 / f"sweep_{_mode5}_{_fam5}.pkl"
    _resweep5 = _os5.getenv("SAMEDAY5M_RESWEEP") == "1"
    _cached5 = None
    if _cf5.exists() and not _resweep5:
        try:
            with open(_cf5, "rb") as _cfp:
                _c = _pk5.load(_cfp)
            if (_c.get("sm_list") == sm_list and _c.get("tm_list") == tm_list
                    and _c.get("days") == days):
                _cached5 = _c
        except Exception:
            _cached5 = None

    _cache_note5 = ""
    if _cached5 is not None:
        stats = _cached5["stats"]
        n_with_5m = _cached5.get("n_with_5m", 0)
        _cache_note5 = (f'<span style="color:#fbbf24">（キャッシュ再利用: '
                        f'{_cached5.get("computed", "?")} 計算。再計算は '
                        f'SAMEDAY5M_RESWEEP=1 を付けて実行）</span>')
    else:
        print(f"  [5分足TP/SL] {len(items)}銘柄 × {len(cells)}マス 集計中(5分足ソース={source})...", flush=True)
        # ── スイープ中はレポートのモードグローバルを一時解除 ──────────────────────
        # sweep_symbol は各セルの sm/tm を run_limit_backtest に直接渡し、決済も自前の
        # 5分足 first-touch で計算する。_SM_FORCE/_TM_FORCE(適用値)や _INTRADAY_5M
        # (集計用の5分足置換)が効いたままだと、全マスが同じ幅になり、かつ余計な5分足
        # 置換が49倍走って激遅になる。ここで退避→解除→復元する(スレッド起動前なので安全)。
        _saved = {k: getattr(_blm, k, None) for k in
                  ("_SM_FORCE", "_TM_FORCE", "_INTRADAY_5M", "_MIRROR_PNL", "_ENTRY_TYPE_FORCE")}
        _blm._SM_FORCE = None; _blm._TM_FORCE = None
        _blm._INTRADAY_5M = False; _blm._MIRROR_PNL = False; _blm._ENTRY_TYPE_FORCE = None
        try:
            with _TPE(max_workers=workers) as ex:
                futs = {ex.submit(_c5.sweep_symbol, s, n, st, is_mirror, cells, days,
                                  source, 0.0, 1e12, _QTY, fee, slip): (s, st)
                        for (s, n, st) in items}
                _done = 0
                for fut in _asc(futs):
                    _done += 1
                    if _done % 50 == 0:
                        print(f"    ...{_done}/{len(items)}銘柄", flush=True)
                    try:
                        r = fut.result()
                    except Exception:
                        continue
                    _tot = sum(len(v) for v in r.values())
                    if _tot > 0:
                        n_with_5m += 1
                    for c, pnls in r.items():
                        agg[c] += pnls
        finally:
            for k, v in _saved.items():
                setattr(_blm, k, v)
        stats = {c: _c5.cell_stat(agg[c]) for c in cells}

    if not any(stats.values()):
        return (f'<div style="margin:16px 0;padding:16px 20px;background:#1e293b;'
                f'border-radius:8px;border-left:3px solid #f59e0b;color:#fbbf24">'
                f'🕔 5分足TP/SL: 5分足データが見つからないか、対象銘柄に5分足がありません'
                f'(対象{len(items)}銘柄 / 5分足あり{n_with_5m}銘柄)。<br>'
                f'ローカルの stock_5min を用意し、run_signals_holdout_all を '
                f'MINUTE_5M_DIR 環境変数付きで実行するか、5分足ソースを見直してください。</div>')

    best = None
    for c in cells:
        st = stats[c]
        if st and (best is None or st["pnl"] > stats[best]["pnl"]):
            best = c

    # 新規計算したときだけ結果を保存(次回以降は計算スキップ)。
    if _cached5 is None:
        try:
            _cdir5.mkdir(exist_ok=True)
            with open(_cf5, "wb") as _cfp:
                _pk5.dump({"stats": stats, "n_with_5m": n_with_5m, "sm_list": sm_list,
                           "tm_list": tm_list, "days": days,
                           "computed": _dt5.now().strftime("%Y-%m-%d %H:%M")}, _cfp)
        except Exception:
            pass

    def _pf(x):
        return "∞" if x == float("inf") else f"{x:.2f}"
    _th = 'padding:5px 8px;text-align:center;color:#94a3b8;font-size:0.72rem'
    head = f'<th style="{_th};text-align:left">損切ATR＼利確ATR</th>' + "".join(
        f'<th style="{_th}">tm={tm}</th>' for tm in tm_list)
    body = ""
    for sm in sm_list:
        row = f'<td style="{_th};text-align:left;color:#e2e8f0;font-weight:700">sm={sm}</td>'
        for tm in tm_list:
            st = stats[(sm, tm)]
            if not st:
                row += f'<td style="{_th}">—</td>'; continue
            is_best = (best == (sm, tm))
            pc = "#4ade80" if st["pnl"] > 0 else "#f87171" if st["pnl"] < 0 else "#94a3b8"
            bg = "background:#0e3320;" if is_best else ("background:#101826;" if st["pnl"] > 0 else "")
            star = " ★" if is_best else ""
            row += (f'<td style="padding:5px 8px;text-align:center;{bg}">'
                    f'<div style="color:{pc};font-weight:700;font-size:0.82rem">{st["pnl"]:+,.0f}{star}</div>'
                    f'<div style="color:#64748b;font-size:0.66rem">PF{_pf(st["pf"])}/{st["wr"]:.0f}%</div></td>')
        body += f"<tr>{row}</tr>"
    bline = ""
    if best:
        b = stats[best]
        bline = (f'<p style="margin:10px 0 4px;font-size:0.9rem">★ 5分足の最適: '
                 f'<b style="color:#4ade80">損切ATR={best[0]} / 利確ATR={best[1]}</b> → '
                 f'<b style="color:{"#4ade80" if b["pnl"]>=0 else "#f87171"}">{b["pnl"]:+,.0f}円</b> '
                 f'(PF{_pf(b["pf"])} / 勝率{b["wr"]:.0f}% / {b["n"]}取引) {_cache_note5}</p>')
    _cost = "摩擦なし(スリッページ0)" if slip == 0 else f"損切スリップ{slip*100:.2f}%"
    return (
        f'<div style="margin:0 0 16px;padding:16px 20px;background:#1e293b;'
        f'border-radius:8px;border-left:3px solid #38bdf8">'
        f'<h4 style="margin:0 0 6px;color:#7dd3fc;font-size:0.95rem">'
        f'🕔 同日TP/SL最適化【5分足・正確】（直近{days}日 / 対象{len(items)}銘柄 '
        f'/ 5分足あり{n_with_5m}銘柄）</h4>'
        f'<p style="margin:0 0 10px;color:#94a3b8;font-size:0.76rem">'
        f'5分足の実際の値動き順で first-touch 判定(約定前ヒットの先読み回避・同時タッチは'
        f'損切優先)。エントリーは注文価格ちょうど(ミラーの幻スリッページ排除)。{_cost}。<br>'
        f'※ 日足スイープ(隣タブ)は近似。狭い幅(0.1等)ほど日足では両方が値幅に入り不正確に'
        f'なるので、こちらの5分足版が本当の最適値。5分足の無い銘柄は集計から除外。</p>'
        f'{bline}'
        f'<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.8rem">'
        f'<thead><tr style="border-bottom:1px solid #334155">{head}</tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
        f'<p style="margin:8px 0 0;color:#64748b;font-size:0.72rem">'
        f'各セル上段=総損益 / 下段=PF・勝率。★=最良。</p></div>')


_ORDERED_CACHE = None

def _load_ordered_orders() -> list[dict]:
    """kabu_send_signals が実発注時に記録した ordered_signals.csv を読む。
    レポートの取引明細で『発注済』印を付けるための突合データ(銘柄+戦略+日付)。"""
    global _ORDERED_CACHE
    if _ORDERED_CACHE is not None:
        return _ORDERED_CACHE
    import csv as _csv
    from pathlib import Path as _P
    from datetime import datetime as _dt
    rows: list[dict] = []
    for _fn in ("ordered_signals.csv", "ordered_signals_aggressive.csv"):
        p = _P(_fn)
        if not p.exists():
            continue
        try:
            for r in _csv.DictReader(open(p, encoding="utf-8")):
                try:
                    rd = _dt.strptime(str(r.get("record_date", "")).strip(), "%Y-%m-%d").date()
                except Exception:
                    continue
                rows.append({
                    "date": rd,
                    "symbol": str(r.get("symbol", "")).upper().removesuffix(".T"),
                    "strategy": str(r.get("strategy", "")).upper().replace("TF", ""),
                    "prod": str(r.get("prod", "")).strip(),
                })
        except Exception:
            pass
    _ORDERED_CACHE = rows
    return rows


def _ordered_badge_for(t: dict) -> str:
    """トレード t が実発注済みなら『発注済』バッジHTMLを返す。
    突合: 銘柄一致 + 戦略一致(TF差は無視) + 発注日がエントリー日の[-1,+5]日以内。"""
    orders = _load_ordered_orders()
    if not orders:
        return ""
    sym = str(t.get("symbol", "")).upper().removesuffix(".T")
    strat = str(t.get("strategy", "")).upper().replace("TF", "")
    ed = t.get("entry_d_raw")
    for o in orders:
        if o["symbol"] != sym:
            continue
        if strat and o["strategy"] and o["strategy"] != strat:
            continue
        if ed is not None and hasattr(ed, "toordinal") and hasattr(o["date"], "toordinal"):
            delta = (ed - o["date"]).days
            if not (-1 <= delta <= 5):
                continue
        prod_lbl = "" if o["prod"] == "1" else "(デモ)"
        return ('<br><span style="background:#0891b2;color:#fff;font-size:0.66rem;'
                'font-weight:700;padding:1px 5px;border-radius:3px;white-space:nowrap">'
                f'📤発注済{prod_lbl}</span>')
    return ""


_MONTH_REGIME_CACHE = None

def _month_regime(ym: str) -> tuple:
    """ym('YYYY-MM') 末時点の大局レジーム(先読みなし)。(ラベル, 色)を返す。
    相場環境タブ/market_regime.py と同じ ER+MA200傾き 判定。"""
    global _MONTH_REGIME_CACHE
    if _MONTH_REGIME_CACHE is None:
        _MONTH_REGIME_CACHE = {}
        try:
            import numpy as _np
            c = fetch_n225(15)
            if c is not None and len(c) >= 230:
                c = c.sort_index()
                ma200 = c.rolling(200).mean()
                slope = ma200.pct_change(20) * 100
                er = (c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, _np.nan)
                above = c >= ma200
                _me = c.groupby(c.index.strftime("%Y-%m")).tail(1)
                for d in _me.index:
                    i = c.index.get_loc(d)
                    e, s, a, m = er.iloc[i], slope.iloc[i], above.iloc[i], ma200.iloc[i]
                    k = d.strftime("%Y-%m")
                    if not _np.isfinite(m) or not _np.isfinite(e):
                        _MONTH_REGIME_CACHE[k] = "?"
                    elif e < 0.20:
                        _MONTH_REGIME_CACHE[k] = "sideways"
                    elif a and s > 0:
                        _MONTH_REGIME_CACHE[k] = "up"
                    elif (not a) and s < 0:
                        _MONTH_REGIME_CACHE[k] = "down"
                    else:
                        _MONTH_REGIME_CACHE[k] = "sideways"
        except Exception:
            pass
    _lbl = {"up": ("🟢上げ", "#4ade80"), "sideways": ("🟡横ばい", "#fbbf24"),
            "down": ("🔴下げ", "#f87171"), "?": ("―", "#64748b")}
    return _lbl.get(_MONTH_REGIME_CACHE.get(ym, "?"), ("―", "#64748b"))


def _tab5_pnl_html(days: int, workers: int, cfg_filter: str | None = None,
                   symbol_filter: list[str] | None = None,
                   entry_days: int | None = None,
                   skip_timing9: bool = False,
                   preoos_cutoff_days: int | None = None,
                   strategy_filter: list[str] | None = None) -> str:
    """タブ5: 直近N日 取引損益レポート。cfg_filter 指定時は対象configのみ表示。
    entry_days 指定時は「エントリー日が直近N日以内」の取引だけを取引明細に表示する。
    skip_timing9=True なら⑨Rolling/em比較をスキップ（期間フィルタタブ用に軽量化）。
    preoos_cutoff_days 指定時は「today-N日以前のデータのみ」でBTスコアを再計算し
    OOS前BTスコア別成績タブを追加（メインBTスコアは変更しない）。
    strategy_filter 指定時はその戦略のみに絞る（銘柄詳細タブで「本日シグナルが
    出た戦略=チップのBT」に一致する取引だけを表示するために使用）。"""
    if not _SIGNALS_AVAILABLE:
        return '<p style="color:#64748b;padding:20px">シグナルモジュールが見つかりません</p>'

    import gc
    from collections import defaultdict
    # _REPORT_END 指定時は「基準日+N日」で集計を打ち切る(現在まで走らせない・軽量化)。
    until = _REPORT_END or _TODAY
    since = until - timedelta(days=days)

    # ── バックテスト結果キャッシュ ─────────────────────────────────────────────
    # 同一セッション内で複数の days で呼ばれる場合、バックテスト自体は1回だけ実行する。
    # days の違いは後段のフィルタで対応するため再実行は不要。
    _sym_filter_key = tuple(sorted(symbol_filter)) if symbol_filter else None
    _strat_filter_key = tuple(sorted(strategy_filter)) if strategy_filter else None
    _cfg_cache_key = (
        tuple((c["label"], c["mode"], str(c.get("sm_tm")),
               tuple(c.get("stop_wl", [])), tuple(c.get("brk_wl", []))) for c in _PNL_CONFIGS),
        _sym_filter_key,
        _strat_filter_key,
        _BT_WINDOW_DAYS,   # 全期間バックテスト窓が変われば再構築
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
            if strategy_filter:
                _wl_stop = [(s, n, st) for s, n, st in _wl_stop if st in strategy_filter]
                _wl_brk  = [(s, n, st) for s, n, st in _wl_brk  if st in strategy_filter]
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

            # ── 全期間 trade_log（過去検証で月別グリッドを全期間表示するため）──────
            # backtest_one のスコアは365日のまま（＝ライブと同一）。ここで別途、
            # 指定窓(_BT_WINDOW_DAYS)の全期間バックテストを回し full_trade_log を付与する。
            # _set_sig_params(cfg["mode"]) はこの時点で有効なので con/agg も正しく反映。
            if (_BT_WINDOW_DAYS and _BT_WINDOW_DAYS > 365) or _REPORT_END is not None:
                import pandas as _pd_fl
                from backtest_limit_entry import (
                    fetch as _fw_fetch, run_limit_backtest as _fw_rbt)

                # スコア計算用に、基準日より約1年前(+400日)からバックテストする。
                # これで基準日直後(例2018-08)のシグナルにも『直近1年の実績』が揃い、
                # 当時のBTスコアを正しく算出できる(先読みではない=当時入手可能な過去)。
                # 表示・集計は since=基準日 でフィルタするので、基準日前のトレードは
                # スコア計算にのみ使われ、月別グリッドには出ない。
                if _REPORT_END is not None:
                    # 基準月+N日で打ち切る軽量モード: 窓を [基準日-400, _REPORT_END] に限定。
                    _cutoff_start = (_REPORT_START or (_REPORT_END - timedelta(days=365))) \
                        - timedelta(days=400)
                    _score_win = max(210, (_TODAY - _cutoff_start).days)
                else:
                    _score_win = _BT_WINDOW_DAYS + 400

                def _full_log(_sym, _name, _strat):
                    try:
                        _mod = _mod_for(_strat)
                        _p = _mod.STRATEGY_PARAMS.get(_strat)
                        if not _p:
                            return None
                        _cf, _e, _s, _t = _p
                        if _REPORT_END is not None:
                            _dfx = _fw_fetch(_sym, _score_win, min_start_date=_cutoff_start)
                        else:
                            _dfx = _fw_fetch(_sym, _score_win)
                        if _dfx is None:
                            return None
                        if _REPORT_END is not None:
                            # 集計終了日で未来をトリム(基準月+N日以降は生成しない)
                            _dfx = _dfx[_dfx.index <= _pd_fl.Timestamp(_REPORT_END)]
                            if len(_dfx) < 210:
                                return None
                        _et = getattr(_mod, "ENTRY_TYPE",
                                      "stop_sell" if _strat.endswith("_S") else "stop")
                        _rr = _fw_rbt(_sym, _name, _dfx, _cf, _e, _s, _t,
                                      _score_win, _strat, entry_type=_et)
                        if not _rr:
                            return None
                        return (_rr["trade_log"], _rr.get("nofill_log", []))
                    except Exception:
                        return None

                with _TPE(max_workers=workers) as _ex2:
                    _f2 = {_ex2.submit(_full_log, it["symbol"], it["name"], it["strategy"]): it
                           for it in items}
                    for _fut in _asc(_f2):
                        _it = _f2[_fut]
                        try:
                            _fl = _fut.result()
                            if _fl is not None:
                                _it["full_trade_log"] = _fl[0]
                                _it["full_nofill_log"] = _fl[1]
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
    all_nofills: list[dict] = []       # lss不約定(発注枠は消費/pnl=0)。予算シミュ専用。
    # 取引リスト表示用: 同一 (sym, strat, signal_dt) は最初のconfig分だけ表示
    seen_global: set = set()
    _seen_nofill: set = set()          # 不約定デデュップ (sym, strat, entry_d)

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
            _btf2 = getattr(_stop, "calc_bt_type", None)
            bt_type2 = _btf2(period_results) if _btf2 else ""
            _OOS_BT_SCORES[(sym, strat)] = rec_score2
            # ロングBTスコアフィルタ (mirror/lss で「ロングが弱い銘柄だけ」に絞る)。
            # _LONG_BT_REF があれば別モードで測ったロングBT、無ければ現モードのBTで判定。
            if _PNL_BT_MAX > 0 or _PNL_BT_MIN > 0:
                _bt_flt = _LONG_BT_REF.get((sym, strat), rec_score2) if _LONG_BT_REF else rec_score2
                if _PNL_BT_MAX > 0 and _bt_flt >= _PNL_BT_MAX:
                    continue   # BTが高い(ロングが強い)銘柄はフェード対象外
                if _PNL_BT_MIN > 0 and _bt_flt < _PNL_BT_MIN:
                    continue
            if wf2:
                wf_score2, wf_rank_str2 = wf2
                score, rank = wf_score2, wf_rank_str2
                is_wf2 = True
            else:
                wf_score2, wf_rank_str2 = None, None
                score, rank = rec_score2, rec_rank2
                is_wf2 = False
            # 全期間 trade_log があればそれを使う(過去検証で月別グリッドを全期間表示)。
            # 無ければ従来どおり最長期間(365日)の trade_log。
            trade_log     = it.get("full_trade_log")
            if trade_log is None:
                max_period = max(period_results.keys())
                trade_log  = period_results[max_period].get("trade_log", [])
            seen: set     = set()
            # START_DATES によるOOS開始日制限: 最新基準月のみ銘柄はその日付以前を除外。
            # _eff_since = max(since, pair_oos_start_date) で取引フィルタを引き上げる。
            _cn_key = str(sym).upper().removesuffix(".T").split(".")[0]
            _pair_oos_date = None
            if _LSS_START_DATES:
                _sd = _LSS_START_DATES.get((_cn_key, strat))
                if _sd:
                    _pair_oos_date = pd.Timestamp(_sd).date()
            _eff_since = max(since, _pair_oos_date) if _pair_oos_date else since
            for t in trade_log:
                # 予算フィルタ: 約定値(entry_p)が範囲外の取引は「100株買えない」ので除外。
                # 選定時 latest_price は範囲内でも、約定時に急騰した銘柄をここで弾く。
                if _PNL_ENTRY_MAX_PRICE > 0 or _PNL_ENTRY_MIN_PRICE > 0:
                    _ep = float(t.get("entry_p", 0) or 0)
                    if _ep > 0 and (
                        (_PNL_ENTRY_MAX_PRICE > 0 and _ep > _PNL_ENTRY_MAX_PRICE)
                        or (_PNL_ENTRY_MIN_PRICE > 0 and _ep < _PNL_ENTRY_MIN_PRICE)
                    ):
                        continue
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
                _sig_sc = _lookup_signal_date_bt(
                    sym, strat, str(_sd_for_key) if _sd_for_key else None)
                # 過去検証(全期間)モード: シグナル日時点のBTスコアを full_trade_log から
                # 算出(先読みなし)。当時決済済みのトレードだけで calc_recommend_score する。
                if (_sig_sc is None and it.get("full_trade_log") is not None
                        and _sd_for_key is not None):
                    _ak = (sym, strat, cfg["mode"], str(_sd_for_key), _BT_WINDOW_DAYS)
                    if _ak in _ASOF_BT_CACHE:
                        _sig_sc = _ASOF_BT_CACHE[_ak]
                    else:
                        _sig_sc = _asof_bt_score(it["full_trade_log"], _mod_for(strat), _sd_for_key)
                        _ASOF_BT_CACHE[_ak] = _sig_sc
                    # 当時の決済実績が無い(履歴不足)→ 0=未実証 として扱い先読みを避ける
                    if _sig_sc is None:
                        _sig_sc = 0
                if _sig_sc is None:
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
                        "signal_dt_raw": _sd_for_key,   # ④純OOS列: 除外窓判定用
                        "bt_type": bt_type2,    # BTスコアの支配要素(安定/取引数/高WR/高PF)
                        "entry_d_raw": entry_d, "exit_d_raw": exit_d,
                        "entry_time": t.get("entry_time", ""),   # 約定5分足の開始時刻(#9・明細表示用)
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
                    "entry_time":   t.get("entry_time", ""),   # 約定5分足の開始時刻(#9・CSV用)
                    "signal_dt_raw": _sdt_raw.date() if hasattr(_sdt_raw, "date") else _sdt_raw,
                }
                # サマリー用: config独立でカウント（発注中・他configとの重複は除外しない）
                if reason != "発注中" and _eff_since <= exit_d <= until:
                    cfg_trades_map[cfg["label"]].append({**base, **extra})
                # 取引リスト・総KPI用: 同一シグナルは最初のconfig分だけ
                gkey = (sym, strat, signal_dt)
                if gkey in seen_global:
                    continue
                seen_global.add(gkey)
                # 発注中はスコア帯統計から除外 (未約定のためpnl=0で歪む)
                if reason != "発注中" and _eff_since <= exit_d <= until:
                    full_year_trades.append(base)
                # 取引明細テーブルには発注中も表示
                if _eff_since <= exit_d <= until:
                    all_trades.append({**base, **extra})

            # ── 不約定(発注枠は消費/pnl=0)を予算シミュ用に別リストへ ──────────────
            # lssのみ。注文は出したがトリガー未達/ギャップ過大で約定しなかったシグナル。
            # KPI・月次・取引明細には一切混ぜず、予算シミュ(注文時に枠を消費)だけが参照する。
            if _LSS_ORDER_MODE:
                _nf_src = it.get("full_nofill_log")
                if _nf_src is None:
                    _mp_nf = max(period_results.keys())
                    _nf_src = period_results[_mp_nf].get("nofill_log", [])
                for _nt in (_nf_src or []):
                    _n_edt = _nt.get("entry_dt")
                    if _n_edt is None:
                        continue
                    _n_ed = _n_edt.date() if hasattr(_n_edt, "date") else _n_edt
                    if not (_eff_since <= _n_ed <= until):
                        continue
                    _n_lp = float(_nt.get("order_limit", 0) or 0)
                    if _n_lp <= 0:
                        continue
                    # 価格帯フィルタ(6000円タブ等)は注文トリガー価格(終値ベース)で判定。
                    if _PNL_ENTRY_MAX_PRICE > 0 and _n_lp > _PNL_ENTRY_MAX_PRICE:
                        continue
                    if _PNL_ENTRY_MIN_PRICE > 0 and _n_lp < _PNL_ENTRY_MIN_PRICE:
                        continue
                    _nkey = (sym, strat, _n_ed)
                    if _nkey in _seen_nofill:
                        continue
                    _seen_nofill.add(_nkey)
                    # シグナル時点BT(fillと同じ算出)。BT30未満は発注対象外なので枠も消費しない。
                    _n_sdt = _nt.get("signal_dt")
                    _n_sd = _n_sdt.date() if hasattr(_n_sdt, "date") else (_n_sdt or _n_ed)
                    _n_sc = _lookup_signal_date_bt(sym, strat, str(_n_sd) if _n_sd else None)
                    if (_n_sc is None and it.get("full_trade_log") is not None
                            and _n_sd is not None):
                        _ak2 = (sym, strat, cfg["mode"], str(_n_sd), _BT_WINDOW_DAYS)
                        if _ak2 in _ASOF_BT_CACHE:
                            _n_sc = _ASOF_BT_CACHE[_ak2]
                        else:
                            _n_sc = _asof_bt_score(it["full_trade_log"], _mod_for(strat), _n_sd)
                            _ASOF_BT_CACHE[_ak2] = _n_sc
                        if _n_sc is None:
                            _n_sc = 0
                    if _n_sc is None:
                        _n_sc = rec_score2
                    if _n_sc < 30:
                        continue   # BT30未満は注文しない → 枠も消費しない
                    all_nofills.append({
                        "symbol": sym, "name": name, "strategy": strat,
                        "rec_score": _n_sc, "score": _n_sc,
                        "entry_d_raw": _n_ed, "exit_d_raw": _n_ed,
                        "order_limit": _n_lp, "entry_p": _n_lp,
                        "qty": _nt.get("qty", 0), "reason": "約定せず", "pnl": 0.0,
                    })

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
    globals()["_LAST_KPI_TRADES"] = kpi_trades   # トレンド期間タブの損益列で再利用

    # ── 日経トレンド別成績 ──────────────────────────────────────────────────────
    _trend_breakdown_html = ""
    try:
        # 過去検証対応: 表示期間 [since, until] をカバーする日経を取得。
        # fetch_n225(2)固定だと直近2年しか取れず、過去基準の月では
        # トレードと日経が重ならず全トレンドが「該当なし」になっていた。
        _n_years = max(2, (until - since).days // 365 + 3)
        _n225_close = fetch_n225(_n_years, end_date=until)
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

        # ── 相場環境判定別 成績（✅推奨/⚠️注意/❌荒れ）──────────────────────────
        def _env_level(_r):
            _tr, _vl, _dp = _r["trend"], _r["vol_level"], _r["max_1d_drop"]
            if _tr == "down" and _vl == "high" and _dp < -3.0:
                return "❌ 荒れ(急落)"
            if _tr == "down" or (_tr == "sideways" and _vl == "high"):
                return "⚠️ 注意"
            return "✅ 推奨"
        _env_order = ["✅ 推奨", "⚠️ 注意", "❌ 荒れ(急落)"]
        _env_col = {"✅ 推奨": "#4ade80", "⚠️ 注意": "#fbbf24", "❌ 荒れ(急落)": "#f87171"}
        _env_buckets = {_k: [] for _k in _env_order}
        _reg_cache: dict = {}
        for _t in kpi_trades:
            _sdt = _t.get("signal_dt_raw")
            if not _sdt:
                continue
            if _sdt not in _reg_cache:
                _sc = _n225_close[_n225_close.index <= pd.Timestamp(_sdt)]
                try:
                    _reg_cache[_sdt] = _env_level(get_regime(_sc)) if len(_sc) >= 21 else None
                except Exception:
                    _reg_cache[_sdt] = None
            _lv = _reg_cache[_sdt]
            if _lv in _env_buckets:
                _env_buckets[_lv].append(_t)
        def _env_stats(_ts):
            _w = [_t for _t in _ts if _t["pnl"] > 0]
            _l = [_t for _t in _ts if _t["pnl"] <= 0]
            _gp = sum(_t["pnl"] for _t in _w)
            _gl = abs(sum(_t["pnl"] for _t in _l))
            _net = _gp - _gl
            _n = len(_ts)
            _wr = len(_w) / _n * 100 if _n else 0.0
            _pf = _gp / _gl if _gl > 0 else (float("inf") if _gp > 0 else 0.0)
            _pf_s = "∞" if _pf == float("inf") else f"{_pf:.2f}"
            return {"n": _n, "wr": _wr, "pf": _pf_s, "gp": _gp, "gl": _gl,
                    "net": _net, "nw": len(_w), "nl": len(_l)}

        def _env_dual(_metric, _sa, _sb):
            # _sa=全, _sb=BT70。e-all/e-bt70 spanで出し分け
            def _fmt(_s):
                if _s["n"] == 0:
                    return ("—", "#475569", "")
                if _metric == "n":
                    return (f"{_s['n']}件", "#e2e8f0", "")
                if _metric == "wr":
                    _c = "#4ade80" if _s["wr"] >= 55 else ("#fbbf24" if _s["wr"] >= 45 else "#f87171")
                    return (f"{_s['wr']:.0f}%", _c, "")
                if _metric == "pf":
                    return (_s["pf"], "#e2e8f0", "")
                if _metric == "gp":
                    return (f"+{_s['gp']:,.0f}", "#4ade80",
                            f"<br><span style='color:#64748b;font-size:0.7rem'>({_s['nw']}勝)</span>")
                if _metric == "gl":
                    return (f"-{_s['gl']:,.0f}", "#f87171",
                            f"<br><span style='color:#64748b;font-size:0.7rem'>({_s['nl']}敗)</span>")
                _c = "#4ade80" if _s["net"] >= 0 else "#f87171"
                return (f"{_s['net']:+,.0f}円", _c, "")
            _ta, _ca, _xa = _fmt(_sa)
            _tb, _cb, _xb = _fmt(_sb)
            _wgt = "font-weight:700" if _metric == "net" else ""
            return (f'<td style="text-align:right">'
                    f'<span class="e-all"  style="color:{_ca};{_wgt}">{_ta}{_xa}</span>'
                    f'<span class="e-bt70" style="color:{_cb};{_wgt};display:none">{_tb}{_xb}</span></td>')

        _env_rows = ""
        for _k in _env_order:
            _ts = _env_buckets[_k]
            _col = _env_col[_k]
            _sa = _env_stats(_ts)
            _sb = _env_stats([_t for _t in _ts if (_t.get("rec_score") or 0) >= 70])
            _env_rows += f"""<tr>
  <td style="color:{_col};font-weight:700;border-left:3px solid {_col};padding-left:8px">{_k}</td>
  {_env_dual('n', _sa, _sb)}{_env_dual('wr', _sa, _sb)}{_env_dual('pf', _sa, _sb)}
  {_env_dual('gp', _sa, _sb)}{_env_dual('gl', _sa, _sb)}{_env_dual('net', _sa, _sb)}
</tr>"""
        _env_html = f"""
<h3 style="margin-top:24px;margin-bottom:8px;color:#94a3b8;font-size:0.95rem">
  相場環境判定別 成績（シグナル日の日経環境で分類）
</h3>
<p class="footnote" style="margin-bottom:8px">
  ✅推奨=通常 ／ ⚠️注意=下落 or 横ばい高ボラ ／ ❌荒れ(急落)=下落×高ボラ×過去30日に-3%超の急落日あり。
</p>
<div style="margin:4px 0 8px">
  <span style="color:#94a3b8;font-size:0.82rem;margin-right:8px">損益の対象:</span>
  <button id="envpnl-all-btn" onclick="setEnvPnl('all')"
    style="padding:4px 12px;border:none;border-radius:6px;cursor:pointer;font-weight:600;background:#2d6cdf;color:#fff">全トレード</button>
  <button id="envpnl-bt70-btn" onclick="setEnvPnl('bt70')"
    style="padding:4px 12px;border:none;border-radius:6px;cursor:pointer;font-weight:600;background:#1e293b;color:#94a3b8">BT70以上</button>
</div>
<script>
function setEnvPnl(m){{
  document.querySelectorAll('.e-all').forEach(function(e){{e.style.display=(m==='all')?'':'none';}});
  document.querySelectorAll('.e-bt70').forEach(function(e){{e.style.display=(m==='bt70')?'':'none';}});
  var a=document.getElementById('envpnl-all-btn'), b=document.getElementById('envpnl-bt70-btn');
  if(a&&b){{
    a.style.background=(m==='all')?'#2d6cdf':'#1e293b'; a.style.color=(m==='all')?'#fff':'#94a3b8';
    b.style.background=(m==='bt70')?'#2d6cdf':'#1e293b'; b.style.color=(m==='bt70')?'#fff':'#94a3b8';
  }}
}}
</script>
<table style="font-size:0.88rem">
  <thead><tr><th style="text-align:left">相場環境</th><th>件数</th><th>勝率</th><th>PF</th>
    <th>利益計</th><th>損失計</th><th>損益合計</th></tr></thead>
  <tbody>{_env_rows}</tbody>
</table>"""

        # ── BTタイプ別 成績（安定/取引数/高WR/高PF）──────────────────────────
        _bt_type_colors = getattr(_stop, "_BT_TYPE_COLORS",
                                  {"安定": "#10b981", "高WR": "#3b82f6",
                                   "高PF": "#f59e0b", "取引数": "#a855f7"})
        _bttype_order = ["安定", "取引数", "高WR", "高PF"]

        def _bttype_rows(_min_bt):
            _src = [_t for _t in kpi_trades if (_t.get("rec_score") or 0) >= _min_bt]
            _rows = ""
            for _bt in _bttype_order:
                _col = _bt_type_colors.get(_bt, "#94a3b8")
                _ts = [_t for _t in _src if _t.get("bt_type") == _bt]
                if not _ts:
                    _rows += (f'<tr><td style="color:{_col};font-weight:700;border-left:3px solid {_col};'
                              f'padding-left:8px">{_bt}</td>'
                              f'<td colspan="6" style="text-align:center;color:#475569">該当なし</td></tr>')
                    continue
                _w = [_t for _t in _ts if _t["pnl"] > 0]
                _l = [_t for _t in _ts if _t["pnl"] <= 0]
                _gp = sum(_t["pnl"] for _t in _w)
                _gl = abs(sum(_t["pnl"] for _t in _l))
                _net = _gp - _gl
                _wr = len(_w) / len(_ts) * 100
                _pf = _gp / _gl if _gl > 0 else (float("inf") if _gp > 0 else 0.0)
                _pf_s = "∞" if _pf == float("inf") else f"{_pf:.2f}"
                _pc = "profit" if _net >= 0 else "loss"
                _wr_c = "#4ade80" if _wr >= 55 else ("#fbbf24" if _wr >= 45 else "#f87171")
                _rows += f"""<tr>
  <td style="color:{_col};font-weight:700;border-left:3px solid {_col};padding-left:8px">{_bt}</td>
  <td style="text-align:right">{len(_ts)}</td>
  <td style="text-align:right;color:{_wr_c}">{_wr:.0f}%</td>
  <td style="text-align:right">{_pf_s}</td>
  <td class="profit" style="text-align:right">+{_gp:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({len(_w)}勝)</span></td>
  <td class="loss"   style="text-align:right">-{_gl:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({len(_l)}敗)</span></td>
  <td class="{_pc}"  style="text-align:right;font-weight:700">{_net:+,.0f}円</td>
</tr>"""
            return _rows

        _bttype_head = ('<thead><tr><th style="text-align:left">BTタイプ</th><th>件数</th>'
                        '<th>勝率</th><th>PF</th><th>利益計</th><th>損失計</th><th>損益合計</th></tr></thead>')
        _bttype_html = f"""
<h3 style="margin-top:24px;margin-bottom:8px;color:#94a3b8;font-size:0.95rem">
  BTタイプ別 成績（BTスコアの支配要素ごと）
</h3>
<p class="footnote" style="margin-bottom:8px">
  各トレードを「BTスコアを最も押し上げた要素」で分類。安定=期間安定性 ／ 取引数=取引回数 ／
  高WR=勝率 ／ 高PF=PF。<strong>取引数タイプは件数が少なく参考値</strong>の場合あり。
</p>
<div style="color:#94a3b8;font-size:0.85rem;margin:6px 0 2px">▼ BT70以上</div>
<table style="font-size:0.88rem">{_bttype_head}<tbody>{_bttype_rows(70)}</tbody></table>
<div style="color:#94a3b8;font-size:0.85rem;margin:12px 0 2px">▼ BT80以上</div>
<table style="font-size:0.88rem">{_bttype_head}<tbody>{_bttype_rows(80)}</tbody></table>"""

        # ── トレンド期間別 損益（個々の期間ごと・新しい順）──────────────────────
        try:
            _ppnl_periods = extract_periods(_n225_close, _n225_trend,
                                            _n225_close.index[-1].date())
        except Exception:
            _ppnl_periods = []
        _per_rows = ""
        for _p in reversed(_ppnl_periods):   # 新しい順
            _ps, _pe = _p["start"], _p["end"]
            _psub = [_t for _t in kpi_trades
                     if _t.get("signal_dt_raw") and _ps <= _t["signal_dt_raw"] <= _pe]
            if not _psub:
                continue   # トレードのない期間は省略
            _w  = [_t for _t in _psub if _t["pnl"] > 0]
            _l  = [_t for _t in _psub if _t["pnl"] <= 0]
            _gp = sum(_t["pnl"] for _t in _w)
            _gl = abs(sum(_t["pnl"] for _t in _l))
            _pnl = _gp - _gl
            _wr  = len(_w) / len(_psub) * 100
            _lbl, _col, _bg = _tlabels.get(_p["trend"], _tlabels[None])
            _pc = "profit" if _pnl >= 0 else "loss"
            _per_rows += f"""<tr style="background:{_bg}15">
  <td style="color:{_col};font-weight:700;border-left:3px solid {_col};padding-left:8px">{_lbl}</td>
  <td style="text-align:center;color:#94a3b8;font-size:0.8rem">{_ps}〜{_pe}</td>
  <td style="text-align:right;color:{'#f87171' if _p['pct']<0 else '#4ade80'}">{_p['pct']:+.1f}%</td>
  <td style="text-align:right">{len(_psub)}件</td>
  <td style="text-align:right;color:{'#4ade80' if _wr>=55 else ('#fbbf24' if _wr>=45 else '#f87171')}">{_wr:.0f}%</td>
  <td class="profit" style="text-align:right">+{_gp:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({len(_w)}勝)</span></td>
  <td class="loss"   style="text-align:right">-{_gl:,.0f}円<br><span style="color:#64748b;font-size:0.7rem">({len(_l)}敗)</span></td>
  <td class="{_pc}"  style="text-align:right;font-weight:700">{_pnl:+,.0f}円</td>
</tr>"""
        _period_pnl_html = f"""
<h3 style="margin-top:24px;margin-bottom:8px;color:#94a3b8;font-size:0.95rem">
  トレンド期間別 損益（個々の期間ごと・新しい順）
</h3>
<p class="footnote" style="margin-bottom:8px">
  各トレンド期間に発生したシグナルのトレードを期間ごとに集計。トレードのある期間のみ表示。
</p>
<table style="font-size:0.85rem">
  <thead><tr>
    <th style="text-align:left">種別</th><th>期間</th><th>騰落率</th><th>件数</th><th>勝率</th>
    <th>利益計</th><th>損失計</th><th>損益合計</th>
  </tr></thead>
  <tbody>{_per_rows}</tbody>
</table>""" if _per_rows else ""

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
{_bttype_html}
{_env_html}
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
  <div class="kpi"><div class="kpi-l">利益合計</div><div class="kpi-v profit">{"—" if not n_total else f"+{dedup_gp:,.0f}円"}</div></div>
  <div class="kpi"><div class="kpi-l">損失合計</div><div class="kpi-v loss">{"—" if not n_total else f"-{dedup_gl:,.0f}円"}</div></div>
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

    # ── 戦略別サマリー (MACD/A7/RSI2/DON/VOL/MOM) 重複除外の実取引ベース ──
    _strat_priority = _calc_strat_priority   # 共通のモジュール関数を使用

    def _strat_sum_rows(min_bt: int) -> str:
        from collections import defaultdict as _dd
        g = _dd(list)
        for t in kpi_trades:
            if t.get("score", 0) >= min_bt:
                g[t.get("strategy", "?")].append(t)
        # 各戦略の指標を先に計算し、優先度スコア降順で並べる
        stats = {}
        for strat, tr in g.items():
            n = len(tr)
            wins = sum(1 for t in tr if t["pnl"] > 0); losses = n - wins
            gp = sum(t["pnl"] for t in tr if t["pnl"] > 0)
            gl = abs(sum(t["pnl"] for t in tr if t["pnl"] < 0))
            pnl = gp - gl
            pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
            wr = wins / n * 100 if n else 0.0
            avgh = sum(t.get("hold_days", 0) for t in tr) / n if n else 0.0
            avgw = gp / wins if wins else 0.0
            avgl = -gl / losses if losses else 0.0
            pri_sc, pri_rk, pri_col = _strat_priority(wr, pf, avgh)
            stats[strat] = dict(n=n, wins=wins, losses=losses, gp=gp, gl=gl,
                                pnl=pnl, pf=pf, wr=wr, avgh=avgh, avgw=avgw,
                                avgl=avgl, pri_sc=pri_sc, pri_rk=pri_rk,
                                pri_col=pri_col)
        rows = ""
        for strat in sorted(stats, key=lambda s: -stats[s]["pri_sc"]):
            d = stats[strat]
            pf_s = "∞" if d["pf"] == float("inf") else f"{d['pf']:.2f}"
            lpc = "profit" if d["pnl"] >= 0 else "loss"
            rows += (f'<tr><td class="sym" style="text-align:left;font-weight:700">{strat}</td>'
                     f'<td style="text-align:center;color:{d["pri_col"]};font-weight:700;white-space:nowrap">'
                     f'{d["pri_rk"]}<br><span style="font-size:0.72rem">{d["pri_sc"]:.0f}</span></td>'
                     f'<td>{d["n"]}</td><td>{d["wins"]}</td><td>{d["wr"]:.1f}%</td><td>{pf_s}</td>'
                     f'<td style="text-align:right">{d["avgh"]:.1f}日</td>'
                     f'<td class="profit" style="text-align:right">+{d["gp"]:,.0f}円</td>'
                     f'<td class="profit" style="text-align:right">+{d["avgw"]:,.0f}</td>'
                     f'<td class="loss" style="text-align:right">-{d["gl"]:,.0f}円</td>'
                     f'<td class="loss" style="text-align:right">{d["avgl"]:,.0f}</td>'
                     f'<td class="{lpc}" style="text-align:right;font-weight:700">{d["pnl"]:+,.0f}円</td></tr>')
        return rows or '<tr><td colspan="12" style="color:#64748b">該当なし</td></tr>'

    def _strat_table(min_bt: int, title: str) -> str:
        return (f'<h3 style="color:#93c5fd;margin:12px 0 4px;font-size:0.95rem">{title}</h3>'
                '<table><thead><tr><th style="text-align:left">戦略</th>'
                '<th>優先度<br><small style="color:#64748b">勝率+PF+速度</small></th>'
                '<th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>平均保有</th>'
                '<th style="color:#4ade80">利益</th><th style="color:#4ade80">平均利益</th>'
                '<th style="color:#f87171">損失</th><th style="color:#f87171">平均損失</th>'
                '<th>損益合計</th></tr></thead><tbody>'
                f'{_strat_sum_rows(min_bt)}</tbody></table>')

    strat_summary_html = (_strat_table(0, "戦略別サマリー（全件・重複除外の実取引ベース）")
                          + _strat_table(70, "戦略別サマリー（BT70以上）"))

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
        # ① WF軸: wf_score で帯分け(旧: score=WF値のため②BTと同一表になっていた)。
        # WF未付与の取引はWF軸に載せられないので除外。
        tr = [t for t in full_year_trades
              if t.get("wf_score") is not None and lo <= t["wf_score"] < hi]
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
        # ② BT軸: rec_score(直近BTスコア)で帯分け(旧: score=WF値でWF軸と同一だった)。
        tr = [t for t in full_year_trades
              if t.get("rec_score") is not None and lo <= t["rec_score"] < hi]
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

    # ── BT帯×戦略 クロス分析 (同BT帯内で戦略差がBT分布と独立か検証) ──
    _strat_list = sorted({str(t.get("strategy", "")) for t in full_year_trades if t.get("strategy")})
    strat_band_html = ""
    for lo, hi, lbl_s, col in bt_buckets:
        _bt_band_tr = [t for t in full_year_trades
                       if t.get("rec_score") is not None and lo <= t["rec_score"] < hi]
        if not _bt_band_tr:
            continue
        _bn, _bw, _bpnl, _bgp, _bgl, _bpf, _, _, _ = _band_stats(_bt_band_tr)
        _bpf_s = "∞" if _bpf == float("inf") else f"{_bpf:.2f}"
        _bpc = "profit" if _bpnl >= 0 else "loss"
        strat_band_html += f"""<tr style="border-top:2px solid #334155">
  <td style="color:{col};font-weight:700;text-align:left">{lbl_s}</td>
  <td style="color:{col};font-weight:700">合計</td>
  <td style="font-weight:700">{_bn}</td>
  <td style="font-weight:700">{_bw/_bn*100:.1f}%</td>
  <td style="font-weight:700">{_bpf_s}</td>
  <td class="profit" style="text-align:right;font-weight:700">+{_bgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-weight:700">-{_bgl:,.0f}円</td>
  <td class="{_bpc}"  style="text-align:right;font-weight:700">{_bpnl:+,.0f}円</td>
</tr>"""
        _se = []
        for _strat in _strat_list:
            _sub = [t for t in _bt_band_tr if str(t.get("strategy", "")) == _strat]
            if not _sub:
                continue
            _sn, _sw, _sp, _sgp, _sgl, _spf, _, _, _ = _band_stats(_sub)
            _se.append((_strat, _sn, _sw, _sp, _sgp, _sgl, _spf))
        for _strat, _sn, _sw, _sp, _sgp, _sgl, _spf in sorted(_se, key=lambda x: -x[3]):
            _spf_s = "∞" if _spf == float("inf") else f"{_spf:.2f}"
            _spc   = "profit" if _sp >= 0 else "loss"
            _spf_c = "#4ade80" if _spf >= 1.5 else ("#fbbf24" if _spf >= 1.0 else "#f87171")
            strat_band_html += f"""<tr style="background:#0f172a">
  <td></td>
  <td style="color:#e2e8f0;font-size:0.85rem;font-weight:600;padding-left:16px">{_strat}</td>
  <td style="color:#94a3b8;font-size:0.85rem">{_sn}</td>
  <td style="color:#94a3b8;font-size:0.85rem">{_sw/_sn*100:.1f}%</td>
  <td style="color:{_spf_c};font-size:0.85rem;font-weight:700">{_spf_s}</td>
  <td class="profit" style="text-align:right;font-size:0.85rem">+{_sgp:,.0f}円</td>
  <td class="loss"   style="text-align:right;font-size:0.85rem">-{_sgl:,.0f}円</td>
  <td class="{_spc}"  style="text-align:right;font-size:0.85rem">{_sp:+,.0f}円</td>
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

    # ── ③-b ⚠OOS弱 影響分析 (BT≥60 を WF健全/OOS弱 で二分し成績差を定量化) ──
    # シグナル一覧の⚠OOS弱バッジ(_BT_HIGH_WF_LOW = BT≥60 & WF<40)が、実際に
    # 成績を悪化させているかを、同じ閾値で健全群と比較して検証する。
    _bt_lo, _wf_hi = _BT_HIGH_WF_LOW
    _oos_healthy = [t for t in bt60_trades
                    if t.get("wf_score") is not None and t["wf_score"] >= _wf_hi]
    _oos_weak    = [t for t in bt60_trades
                    if t.get("wf_score") is not None and t["wf_score"] < _wf_hi]
    _oos_none    = [t for t in bt60_trades if t.get("wf_score") is None]
    _oos_groups = [
        (f"健全 (BT≥{_bt_lo}・WF{_wf_hi}以上)",   _oos_healthy, "#4ade80"),
        (f"⚠OOS弱 (BT≥{_bt_lo}・WF{_wf_hi}未満)", _oos_weak,    "#f87171"),
    ]
    _oos_rows = ""
    _oos_stat: dict = {}
    for _lbl, _grp, _c in _oos_groups:
        if not _grp:
            _oos_rows += (f'<tr><td style="color:{_c};font-weight:700;text-align:left">{_lbl}</td>'
                          f'<td colspan="5" style="text-align:center;color:#64748b">該当なし</td></tr>')
            _oos_stat[_lbl] = None
            continue
        gn, gw, gpnl, ggp, ggl, gpf, gavg, _, _ = _band_stats(_grp)
        _oos_stat[_lbl] = (gn, gw / gn * 100, gpf, gavg, gpnl)
        _pfs = "∞" if gpf == float("inf") else f"{gpf:.2f}"
        _pc  = "profit" if gpnl >= 0 else "loss"
        _ac  = "profit" if gavg >= 0 else "loss"
        _oos_rows += f"""<tr>
  <td style="color:{_c};font-weight:700;text-align:left">{_lbl}</td>
  <td style="font-weight:700">{gn}</td>
  <td style="font-weight:700">{gw / gn * 100:.1f}%</td>
  <td style="font-weight:700">{_pfs}</td>
  <td class="{_pc}" style="text-align:right;font-weight:700">{gpnl:+,.0f}円</td>
  <td class="{_ac}" style="text-align:right;font-weight:700">{gavg:+,.0f}円</td>
</tr>"""
    if _oos_none:
        _nn, _nw, _npnl, _, _, _npf, _navg, _, _ = _band_stats(_oos_none)
        _npc = "profit" if _npnl >= 0 else "loss"
        _nac = "profit" if _navg >= 0 else "loss"
        _npfs = "∞" if _npf == float("inf") else f"{_npf:.2f}"
        _oos_rows += f"""<tr style="opacity:.7">
  <td style="color:#94a3b8;font-weight:700;text-align:left">参考: WFなし</td>
  <td>{_nn}</td><td>{_nw / _nn * 100:.1f}%</td><td>{_npfs}</td>
  <td class="{_npc}" style="text-align:right">{_npnl:+,.0f}円</td>
  <td class="{_nac}" style="text-align:right">{_navg:+,.0f}円</td>
</tr>"""
    _hk = f"健全 (BT≥{_bt_lo}・WF{_wf_hi}以上)"
    _wk = f"⚠OOS弱 (BT≥{_bt_lo}・WF{_wf_hi}未満)"
    if _oos_stat.get(_hk) and _oos_stat.get(_wk):
        _hn, _hwr, _hpf, _havg, _hpnl = _oos_stat[_hk]
        _wn, _wwr, _wpf, _wavg, _wpnl = _oos_stat[_wk]
        _dwr = _wwr - _hwr
        _davg = _wavg - _havg
        _hpf_s = "∞" if _hpf == float("inf") else f"{_hpf:.2f}"
        _wpf_s = "∞" if _wpf == float("inf") else f"{_wpf:.2f}"
        _worse = _davg < 0
        _oos_verdict = (
            f"WF低群({_wn}件)は健全({_hn}件)に比べ、"
            f"勝率 {_dwr:+.1f}pt・PF {_wpf_s} vs {_hpf_s}・"
            f"平均損益 {_wavg:+,.0f}円 vs {_havg:+,.0f}円/取引"
            f"（1取引あたり {_davg:+,.0f}円 {'悪化' if _worse else '良化'}）。"
            f" ⇒ WFは{'BT高群の中で追加の識別力あり' if _worse else '今回は明確な差が小さい'}。")
        _oos_vcol = "#f87171" if _worse else "#94a3b8"
    else:
        _oos_verdict = "健全またはWF低群のいずれかが該当なしのため比較不可。"
        _oos_vcol = "#94a3b8"
    oos_impact_html = f"""
<h2 style="margin-top:24px">③-b WF高低 影響分析（BT≥{_bt_lo} を WF で二分 / 365日全取引）</h2>
<p class="footnote" style="margin-bottom:8px">BT≥{_bt_lo} を WF{_wf_hi}以上(健全)/WF{_wf_hi}未満 に二分し、WFスコアがBT高群の中で追加の識別力を持つかを検証（<b>WF基準</b>。選定バイアスあり）。純OOS基準の判定は ④表の「純OOS損益」列と シグナル一覧の <span style="color:#f87171;font-weight:700">⚠OOS弱</span> バッジを参照。</p>
<table>
  <thead><tr>
    <th style="text-align:left">区分</th><th>取引数</th><th>勝率</th><th>PF</th>
    <th>損益合計</th><th>平均損益/取引</th>
  </tr></thead>
  <tbody>{_oos_rows}</tbody>
</table>
<p class="footnote" style="margin-top:6px;color:{_oos_vcol};font-weight:700">{_oos_verdict}</p>
"""

    # ── holdout-OOS 集計 (④表「純OOS」列用) ────────────────────────────────
    # ④行と同じ bt60_trades を母集団とし、そのうち「各config が選定に使っていない
    # 直近除外窓(signal日が TODAY - holdout_days 以降)」に入るトレードだけを抽出。
    # → 必ず行の取引数の部分集合になる(純OOS件数 ≤ 行の取引数)。WFと違い選定
    #   バイアスの無い本物のOOS。標準WF(HOxx無)のconfigでは判定不能=対象外。
    _maxwin = _holdout_window_map()   # (sym,strat) → 最長holdout窓(日)
    _oos_sym: dict = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0, "win": 0})
    for _t in bt60_trades:
        _hd = _maxwin.get((_t.get("symbol"), _t.get("strategy")), 0)
        if not _hd:
            continue
        _sd = _t.get("signal_dt_raw")
        if _sd is None:
            continue
        if _sd >= _TODAY - timedelta(days=_hd):
            _o = _oos_sym[_t["symbol"]]
            _o["n"]   += 1
            _o["pnl"] += _t["pnl"]
            if _t["pnl"] > 0:
                _o["w"] += 1
            _o["win"] = max(_o["win"], _hd)

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
        # 純OOS(holdout除外窓)の成績。WFと違い選定バイアスのない本物のOOS。
        _oo = _oos_sym.get(sym)
        if _oo and _oo["n"] > 0:
            _owr  = _oo["w"] / _oo["n"] * 100
            _opnl = _oo["pnl"]
            _oc   = "#4ade80" if _opnl > 0 else ("#f87171" if _opnl < 0 else "#94a3b8")
            _oos_cell = (f'<td style="text-align:right;color:{_oc};font-weight:700">'
                         f'{_opnl:+,.0f}円<br><span style="font-size:0.66rem;color:#94a3b8">'
                         f'{_oo["n"]}件 {_owr:.0f}% / {_oo["win"]}d窓</span></td>')
        else:
            _oos_cell = '<td style="text-align:center;color:#475569">—</td>'
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
  {_oos_cell}
</tr>"""
    if not sym_rows:
        sym_rows = '<tr><td colspan="11" style="text-align:center;color:#64748b;padding:12px">BT≥60の取引なし</td></tr>'

    # ── ⑥ ロールフォワードOOS検証 (選定as-o別・月次) ───────────────────────────
    # 各holdout設定は「今日−N日」時点で選定=それ以降が純OOS。HO30d〜HO180d を
    # 6つの選定基準日として使い、各基準日の"その後の月次成績"を並べる。新規
    # スキャン不要(既存の設定別トレードを除外窓で切るだけ)。conservativeのみ。
    import re as _re_rf
    _RF_BT_MIN = 70   # ロールフォワードOOSに適用するBTスコア下限(実運用フィルタ相当)
    _con_lbls_rf = {cfg["label"] for cfg in _PNL_CONFIGS
                    if str(cfg.get("mode", "")).lower() == "conservative"}
    _rf_data = []   # (holdout_days, as_of_date, oos_trades)
    for _lab, _trs in cfg_trades_map.items():
        if _lab not in _con_lbls_rf:
            continue
        _m = _re_rf.search(r"HO(\d+)d", _lab)
        if not _m:
            continue
        _hd = int(_m.group(1))
        _asof = _TODAY - timedelta(days=_hd)
        _oos = [t for t in _trs
                if t.get("signal_dt_raw") and t["signal_dt_raw"] >= _asof
                and t.get("reason") not in ("発注中", "保有中")
                and (t.get("rec_score") or 0) >= _RF_BT_MIN]   # BT≥70 フィルタ
        _rf_data.append((_hd, _asof, _oos))
    _rf_data.sort(key=lambda x: -x[0])   # 最長holdout(=最古as-of)を上に
    _rf_months = sorted({t["signal_dt_raw"].strftime("%Y-%m")
                         for _, _, _oos in _rf_data for t in _oos})
    _rf_rows = ""
    for _hd, _asof, _oos in _rf_data:
        _asof_m = _asof.strftime("%Y-%m")
        _cells = ""
        for _mo in _rf_months:
            if _mo < _asof_m:   # 選定に使った月=in-sample
                _cells += '<td style="color:#334155;text-align:center">·</td>'
                continue
            _mt = [t for t in _oos if t["signal_dt_raw"].strftime("%Y-%m") == _mo]
            if not _mt:
                _cells += '<td style="color:#475569;text-align:center">—</td>'
                continue
            _p = sum(t["pnl"] for t in _mt)
            _w = sum(1 for t in _mt if t["pnl"] > 0)
            _c = "#4ade80" if _p > 0 else ("#f87171" if _p < 0 else "#94a3b8")
            _cells += (f'<td style="text-align:right;color:{_c};font-weight:700">{_p:+,.0f}'
                       f'<br><span style="font-size:0.6rem;color:#94a3b8">{len(_mt)}件{_w}勝</span></td>')
        _tp = sum(t["pnl"] for t in _oos)
        _tn = len(_oos)
        _tw = sum(1 for t in _oos if t["pnl"] > 0)
        _twr = _tw / _tn * 100 if _tn else 0
        _tc = "#4ade80" if _tp > 0 else ("#f87171" if _tp < 0 else "#94a3b8")
        _rf_rows += (f'<tr><td style="text-align:left;white-space:nowrap">as-of {_asof_m}'
                     f'<br><span style="font-size:0.65rem;color:#94a3b8">HO{_hd}d</span></td>{_cells}'
                     f'<td style="text-align:right;color:{_tc};font-weight:700">{_tp:+,.0f}円'
                     f'<br><span style="font-size:0.6rem;color:#94a3b8">{_tn}件 {_twr:.0f}%</span></td></tr>')
    _rf_month_h = "".join(f'<th>{_mo[5:]}月</th>' for _mo in _rf_months)
    rollforward_html = f"""
<h2 style="margin-top:24px">★ ロールフォワードOOS検証（選定as-of別・月次 / conservative / <span style="color:#fbbf24">BT≥{_RF_BT_MIN}</span>）</h2>
<p class="footnote" style="margin-bottom:8px">
  各holdout設定は「今日−N日」時点での選定＝それ以降が純OOS。<b>行</b>=選定基準日(古い順)、<b>列</b>=各月の成績。
  <span style="color:#334155">·</span>=選定に使った月(in-sample) / 数字=OOS月の損益 / —=取引なし。下の行ほど長く先まで検証。
  <b>各取引に BT≥{_RF_BT_MIN} フィルタ適用</b>（BTフィルタ無しだと全期間マイナス＝BTの識別力を示す）。各行は別々の再選定＝正しいロールフォワード。
  ※ as-of は最短でも 6月(HO30d)。7月基準は holdout&lt;30日のスキャンが必要で、かつ7月は数日のみ→フォワードテストで蓄積。7月の決済があれば列に自動表示。
</p>
<div style="overflow-x:auto"><table>
<thead><tr><th style="text-align:left">選定as-of</th>{_rf_month_h}<th>OOS計</th></tr></thead>
<tbody>{_rf_rows or '<tr><td colspan="99" style="text-align:center;color:#64748b;padding:12px">OOSデータなし</td></tr>'}</tbody>
</table></div>
"""

    # ── 基準月別 union ロールフォワード ──────────────────────────────────────
    # 「基準月Mまでに選定できた全holdout設定の銘柄をまとめて(union)、Mより後(=純OOS)
    # の月次成績を集計」。設定を横断unionするので件数が増え、"5月基準→6月"の検証に
    # ちょうど対応する。dedup=(sym,strat,signal日)。BT≥_RF_BT_MIN フィルタ適用。
    _cfg_asof2 = []   # (as_of_month_str, trades)
    for _lab, _trs in cfg_trades_map.items():
        if _lab not in _con_lbls_rf:
            continue
        _m2 = _re_rf.search(r"HO(\d+)d", _lab)
        if not _m2:
            continue
        _asof2 = (_TODAY - timedelta(days=int(_m2.group(1)))).strftime("%Y-%m")
        _cfg_asof2.append((_asof2, _trs))
    _base_ms = sorted({am for am, _ in _cfg_asof2})
    _u_fwd = set()
    _u_seen_by_base = {}
    for _bm in _base_ms:
        _seen_u = {}   # (sym,strat,sd) -> (pnl, fwd_month, bt)  ※BTフィルタなし=全選定銘柄
        for _am, _trs in _cfg_asof2:
            if _am > _bm:
                continue
            for t in _trs:
                if t.get("reason") in ("発注中", "保有中"):
                    continue
                _sd = t.get("signal_dt_raw")
                if not _sd:
                    continue
                _sm = _sd.strftime("%Y-%m")
                if _sm <= _bm:
                    continue
                _seen_u[(t["symbol"], t["strategy"], _sd)] = (
                    t.get("pnl", 0), _sm, (t.get("rec_score") or 0))
                _u_fwd.add(_sm)
        _u_seen_by_base[_bm] = _seen_u
    _u_months = sorted(_u_fwd)
    _u_rows = ""
    for _bm in _base_ms:
        _sn = _u_seen_by_base[_bm]
        _cells = ""
        for _mo in _u_months:
            if _mo <= _bm:
                _cells += '<td style="color:#334155;text-align:center">·</td>'
                continue
            _pl = [p for (p, m, b) in _sn.values() if m == _mo]
            if not _pl:
                _cells += '<td style="color:#475569;text-align:center">—</td>'
                continue
            _p = sum(_pl)
            _w = sum(1 for x in _pl if x > 0)
            _c = "#4ade80" if _p > 0 else ("#f87171" if _p < 0 else "#94a3b8")
            _cells += (f'<td style="text-align:right;color:{_c};font-weight:700">{_p:+,.0f}'
                       f'<br><span style="font-size:0.6rem;color:#94a3b8">{len(_pl)}件{_w}勝</span></td>')
        _allv = list(_sn.values())
        _tp = sum(p for p, _m, _b in _allv)
        _tn = len(_allv)
        _tw = sum(1 for p, _m, _b in _allv if p > 0)
        _twr = _tw / _tn * 100 if _tn else 0
        _tc = "#4ade80" if _tp > 0 else ("#f87171" if _tp < 0 else "#94a3b8")
        # BT≥70 の内訳
        _b70 = [p for p, _m, b in _allv if b >= _RF_BT_MIN]
        _bp = sum(_b70)
        _bn = len(_b70)
        _bw = sum(1 for x in _b70 if x > 0)
        _bwr = _bw / _bn * 100 if _bn else 0
        _bc = "#4ade80" if _bp > 0 else ("#f87171" if _bp < 0 else "#94a3b8")
        # 選定銘柄×戦略のユニーク数(この基準で選ばれた広さ)
        _usym = len({(k[0], k[1]) for k in _sn.keys()})
        _u_rows += (f'<tr><td style="text-align:left;white-space:nowrap">{_bm} まで選定'
                    f'<br><span style="font-size:0.6rem;color:#94a3b8">{_usym}銘柄×戦略</span></td>{_cells}'
                    f'<td style="text-align:right;color:{_tc};font-weight:700">{_tp:+,.0f}円'
                    f'<br><span style="font-size:0.6rem;color:#94a3b8">{_tn}件 {_twr:.0f}%</span></td>'
                    f'<td style="text-align:right;color:{_bc};font-weight:700">{_bp:+,.0f}円'
                    f'<br><span style="font-size:0.6rem;color:#94a3b8">{_bn}件 {_bwr:.0f}%</span></td></tr>')
    _u_month_h = "".join(f'<th>{_mo[5:]}月</th>' for _mo in _u_months)
    _union_html = f"""
<h2 style="margin-top:24px">★ 基準月別 ロールフォワードOOS（全holdout選定を union / conservative / <span style="color:#4ade80">全選定銘柄</span>）</h2>
<p class="footnote" style="margin-bottom:8px">
  <b>行=選定の基準月</b>（その月末までのデータで選定できた全holdout設定の銘柄を <b>まとめて union</b>。左端に選定銘柄×戦略数）、<b>列=基準月より後(=純OOS)の各月成績（全選定銘柄・BTフィルタなし）</b>。
  例: 「2026-05 まで選定」行の 06月 = <b>5月末までに選ばれた全銘柄を、未知の6月で試した結果</b>。
  <span style="color:#334155">·</span>=基準月以前(in-sample) / 数字=OOS月損益 / —=取引なし。dedup=銘柄×戦略×シグナル日。
  右端2列: <b>全選定銘柄のOOS計</b> と <b>うちBT≥{_RF_BT_MIN}のOOS計</b>（BTフィルタの効果が分かる）。
</p>
<div style="overflow-x:auto"><table>
<thead><tr><th style="text-align:left">選定基準月</th>{_u_month_h}<th>全OOS計</th><th>うちBT≥{_RF_BT_MIN}</th></tr></thead>
<tbody>{_u_rows or '<tr><td colspan="99" style="text-align:center;color:#64748b;padding:12px">OOSデータなし</td></tr>'}</tbody>
</table></div>
"""
    rollforward_html = _union_html + rollforward_html

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
    # 重複保有(保有中の2回目以降)シグナルも、決済済み含めて全て取引明細・日別・
    # 月別に「普通のシグナル」として表示する。ユーザーが実際に保有中の追加シグナルで
    # 取引するため(例: 北日本銀行を保有中に2回目シグナルで再エントリー)。
    # ※ 計測(BTスコア/戦略サマリー/上部KPI)は all_trades ベースのままで不変。
    #   display_trades は表示・日別グリッド・月別集計にのみ使われる。
    display_trades = all_trades + _overlap_dropped + list(_EXTRA_TRADES)
    # 成績に効く要素の分析HTML(詳細分析タブ★効く要素)
    try:
        _factors_html = _factor_analysis_html(display_trades)
    except Exception:
        _factors_html = ""

    # ── 重複保有(計測外)シグナルの勝率サマリー ──────────────────────────────
    # 1銘柄1ポジション制で弾かれた「既保有中の同銘柄シグナル」の決済済み成績を
    # 取引明細の見出し直下に要約表示する(詳細は⑧タブ)。計測外だが「追加エントリー
    # したらどうだったか」の参考値。
    _ov_settled = [t for t in _overlap_dropped
                   if t.get("reason") not in ("発注中", "保有中", None)
                   and t.get("pnl") is not None]
    if _ov_settled:
        _ov_n   = len(_ov_settled)
        _ov_win = sum(1 for t in _ov_settled if t["pnl"] > 0)
        _ov_wr  = _ov_win / _ov_n * 100
        _ov_gp  = sum(t["pnl"] for t in _ov_settled if t["pnl"] > 0)
        _ov_gl  = abs(sum(t["pnl"] for t in _ov_settled if t["pnl"] < 0))
        _ov_pnl = _ov_gp - _ov_gl
        _ov_pf  = _ov_gp / _ov_gl if _ov_gl > 0 else float("inf")
        _ov_pf_s = "∞" if _ov_pf == float("inf") else f"{_ov_pf:.2f}"
        _ov_wr_c  = "#4ade80" if _ov_wr >= 55 else ("#facc15" if _ov_wr >= 45 else "#f87171")
        _ov_pnl_c = "#4ade80" if _ov_pnl >= 0 else "#f87171"
        _ov_pend  = len(_unsettled_overlaps)
        _overlap_kpi_html = (
            '<div style="background:#1a1333;border:1px solid #7c3aed;border-radius:8px;'
            'padding:10px 14px;margin:6px 0 14px;font-size:0.82rem">'
            '<span style="color:#a78bfa;font-weight:700">重複保有シグナル（再エントリー）</span>'
            '<span style="color:#64748b">：既に同銘柄を保有中に出た2回目以降のシグナル。'
            '取引明細・日別・月別には普通のシグナルとして計上。上部KPI/BTスコアには'
            '含みません（下記は再エントリー分だけの決済済み成績の内訳）。</span><br>'
            f'<span style="color:#94a3b8">決済 </span><b>{_ov_n}件</b>'
            f'&nbsp;/&nbsp;<span style="color:#94a3b8">勝率 </span>'
            f'<b style="color:{_ov_wr_c}">{_ov_wr:.1f}%</b>'
            f'&nbsp;({_ov_win}W/{_ov_n - _ov_win}L)'
            f'&nbsp;/&nbsp;<span style="color:#94a3b8">PF </span><b>{_ov_pf_s}</b>'
            f'&nbsp;/&nbsp;<span style="color:#4ade80">+{_ov_gp:,.0f}円</span>'
            f'&nbsp;<span style="color:#f87171">-{_ov_gl:,.0f}円</span>'
            f'&nbsp;/&nbsp;<span style="color:#94a3b8">損益 </span>'
            f'<b style="color:{_ov_pnl_c}">{_ov_pnl:+,.0f}円</b>'
            + (f'&nbsp;<span style="color:#64748b">（未決済 {_ov_pend}件も下表に表示）</span>'
               if _ov_pend else "")
            + '&nbsp;<span style="color:#64748b">詳細は⑧タブ</span></div>'
        )
    else:
        _overlap_kpi_html = ""

    # 損益タブ(取引明細)には「発注中」を表示しない。
    # 発注中はBTキャッシュ由来の未決着シグナルで、キャッシュが古いと実際とズレる
    # (幽霊表示)。今日の発注はライブ判定のシグナルタブが権威なので、明細は
    # 決済済み(＋保有中)のみを出す。pending_trades は空にして先頭付加を止める。
    pending_trades = []
    done_trades    = [t for t in display_trades if t.get("reason") != "発注中"]
    sorted_trades  = pending_trades + sorted(done_trades, key=lambda x: x["exit_d_raw"], reverse=True)

    # 全取引CSV(env LSS_TRADES_CSV=path): done_trades(全決済済み・全BT・切り捨て無し)を丸ごと出力。
    # HTML明細は直近1500件に切られるので、基準月まとめ等で全件を手元に落とす用途。予算CSVと同じく
    # メイン(フィルター無し)かつ最長窓の1回だけ採用して、部分呼び出しによる上書き事故を防ぐ。
    _trades_csv = os.environ.get("LSS_TRADES_CSV", "").strip()
    if (_trades_csv and cfg_filter is None
            and not symbol_filter and not strategy_filter):
        try:
            import csv as _csvmod2
            global _TRADES_CSV_BEST_N
            _rows_out = sorted(done_trades,
                               key=lambda x: (str(x.get("exit_d_raw") or ""),
                                              str(x.get("symbol") or "")))
            if len(_rows_out) >= _TRADES_CSV_BEST_N:   # 最長窓(件数最多)だけ採用
                _TRADES_CSV_BEST_N = len(_rows_out)
                _cols = ["entry_date", "exit_date", "symbol", "name", "strategy",
                         "bt", "wf", "reason", "order_limit", "entry_p", "exit_p",
                         "stop_price", "target_price", "qty", "hold_days",
                         "liquidity", "mode", "pnl", "entry_time"]
                with open(_trades_csv, "w", newline="", encoding="utf-8-sig") as _f2:
                    _w2 = _csvmod2.writer(_f2)
                    _w2.writerow(_cols)
                    for _t in _rows_out:
                        _w2.writerow([
                            _t.get("entry_d_raw", ""), _t.get("exit_d_raw", ""),
                            _t.get("symbol", ""), _t.get("name", ""),
                            _t.get("strategy", "") or _t.get("strat", ""),
                            _t.get("rec_score", "") if _t.get("rec_score") is not None else _t.get("score", ""),
                            _t.get("wf_score", ""), _t.get("reason", ""),
                            _t.get("order_limit", ""), _t.get("entry_p", ""),
                            _t.get("exit_p", ""), _t.get("order_stop", _t.get("stop_price", "")),
                            _t.get("order_target", _t.get("target_price", "")), _t.get("qty", ""),
                            _t.get("hold_days", ""), _t.get("liquidity", ""),
                            _t.get("mode", ""), _t.get("pnl", ""),
                            _t.get("entry_time", ""),   # 約定5分足の開始時刻 HH:MM(#9・約定時刻分析用)
                        ])
                print(f"[全取引CSV] {_trades_csv} に {len(_rows_out)}件を出力", flush=True)
        except Exception as _te:
            print(f"[全取引CSV] 出力失敗: {_te}", flush=True)

    def _build_trade_row(t, entry_first=False) -> str:
        is_pending = t.get("reason") == "発注中"
        is_overlap = bool(t.get("_overlap"))
        # 重複保有(再エントリー)は情報として印だけ残すが、日別/月別では普通に計上する。
        overlap_badge = ('<br><span style="background:#7c3aed;color:#fff;font-size:0.66rem;'
                         'font-weight:700;padding:1px 5px;border-radius:3px;white-space:nowrap">'
                         '重複保有(再エントリー)</span>') if is_overlap else ""
        tpc = "profit" if t["pnl"] > 0 else ("" if is_pending else "loss")
        tag = f'<span class="tag tag-{t["strategy"].lower()}">{t["strategy"]}</span>'
        sc  = t.get("score"); rk = t.get("rank")
        if sc is not None and rk and rk != "-":
            _col = col_map.get(rk, "#94a3b8")
            sc_html = _fmt_score_cell(t, _col)
        else:
            sc_html = ""
        is_tenkan = t.get("strategy") == "転換"
        if is_overlap:
            # 普通のシグナルと同様に扱う(dimしない)。左帯だけで再エントリーを示す。
            row_style = ' style="border-left:3px solid #7c3aed"'
        elif is_pending:
            row_style = ' style="opacity:0.7;border-left:3px solid #fbbf24"'
        elif is_tenkan:
            row_style = ' style="border-left:3px solid #60a5fa;background:rgba(96,165,250,0.06)"'
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
        # lss は実発注の呼値グリッドに合わせて表示(=BTの5分約定判定と同一)。
        # トリガーだけ前日終値-1ティック(逆指値売りは現在値以上だと即約定で弾かれる)、
        # 損切/利確のリスク幅は前日終値基準(§18検証モデル)でライン直上ティック(ceil)。
        # -1tickは約定用の小細工なので損切りには反映しない(不必要にタイトにしない)。
        # 例:応用地質 トリガー2,825 / 損切2,835 / 利確2,785。
        if _LSS_ORDER_MODE and olp > 0 and osp > 0 and otp > 0:
            from backtest_limit_entry import (round_to_tick as _r2t2,
                                              ceil_to_tick as _c2t2,
                                              tick_size as _tsz2)
            _base2 = _r2t2(olp)                        # 前日終値(呼値)
            olp = float(_r2t2(_base2 - _tsz2(_base2)))  # トリガー=前日終値-1tick(約定用のみ)
            osp = float(_c2t2(osp))                    # 損切=前日終値+atr*sm(ceil)
            otp = float(_c2t2(otp))                    # 利確=前日終値-atr*tm(ceil)
        if olp > 0 and osp > 0 and otp > 0:
            sp_pct   = (osp - olp) / olp * 100
            tp_pct   = (otp - olp) / olp * 100
            stop_cell = (f'<td style="text-align:right;white-space:nowrap">{osp:,.0f}'
                         f'<br><span style="font-size:0.73rem;color:#f87171">{sp_pct:+.1f}%</span></td>')
            tgt_cell  = (f'<td style="text-align:right;white-space:nowrap">{otp:,.0f}'
                         f'<br><span style="font-size:0.73rem;color:#4ade80">{tp_pct:+.1f}%</span></td>')
            # 逆指値トリガー + 指値上限/下限(±3%)。シグナル一覧と同じ発注条件を併記し、
            # 「シグナルで出た指値条件」を取引詳細でも突合できるようにする。
            # ショート判定は「損切りがエントリーより上」で行う(戦略名末尾_Sだと lss を
            # 取りこぼす: lssは MACDTF/DON 等と同名なので、指値ガードが+3%になってしまう)。
            _is_short_t = (osp > olp)
            _lim_entry  = olp * ((1.0 - 0.03) if _is_short_t else (1.0 + 0.03))
            _lim_lbl    = "指値下限≥" if _is_short_t else "指値上限≤"
            _lim_pct    = "-3.0%" if _is_short_t else "+3.0%"
            olp_sub   = (f'<br><span style="font-size:0.71rem;color:#38bdf8">逆:{olp:,.0f}</span>'
                         f'<br><span style="font-size:0.71rem;color:#f59e0b">{_lim_lbl}{_lim_entry:,.0f}'
                         f'<span style="color:#64748b">({_lim_pct})</span></span>')
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
        # 約定時刻(#9: entry_time=約定5分足の開始時刻 HH:MM)をエントリー日の下に表示。
        # 実約定は [entry_time, entry_time+5分) の窓内。空(非lss等)なら非表示。
        _etime = str(t.get("entry_time", "") or "")
        _etime_sub = (f'<br><span style="font-size:0.72rem;color:#38bdf8;white-space:nowrap">'
                      f'約定 {_etime}</span>') if _etime else ""
        first_col = (f'<td style="color:#94a3b8">{t["entry_dt"]}{_etime_sub}</td>'
                     if entry_first else f'<td>{t["exit_dt"]}</td>')
        last_col  = (f'<td>{t["exit_dt"]}</td>'
                     if entry_first else f'<td style="color:#94a3b8">{t["entry_dt"]}{_etime_sub}</td>')
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
  <td>{_rhtml(t["reason"])}{overlap_badge}{_ordered_badge_for(t)}</td>
  {last_col}
</tr>"""

    def _rows_for(trades, empty_msg, entry_first=False, cap=None) -> str:
        # 描画行数を上限で打ち切る(軽量化)。trades は決済日/エントリー日の降順で
        # 渡ってくるので、先頭=直近を残す。集計(サマリー/月別/スコア別)は全件ベースの
        # まま別途計算しているので、ここでの打ち切りは表示だけに影響する。
        # 環境変数 DETAIL_ROW_CAP=0 で無制限(全件描画)に戻せる。
        _cap = _DETAIL_ROW_CAP if cap is None else cap
        _omitted = 0
        if _cap and len(trades) > _cap:
            _omitted = len(trades) - _cap
            trades = trades[:_cap]
        rows = "".join(_build_trade_row(t, entry_first=entry_first) for t in trades)
        if not rows:
            rows = f'<tr><td colspan="15" style="text-align:center;color:#64748b;padding:16px">{empty_msg}</td></tr>'
        elif _omitted:
            rows += (f'<tr><td colspan="15" style="text-align:center;color:#94a3b8;'
                     f'padding:12px;background:#1e293b">… 軽量化のため直近{_cap:,}件のみ表示。'
                     f'残り{_omitted:,}件は省略（集計値は全件ベース）。'
                     f'全件表示は環境変数 DETAIL_ROW_CAP=0 で再実行 …</td></tr>')
        return rows

    # 全部 / BT70以上 / BT40以上 / エントリー日別グリッド の 系統を用意
    bt70_trades = [t for t in sorted_trades if (t.get("rec_score") or 0) >= 70]

    # BT40以上バンド: BTスコア40以上(=質の高い銘柄)だけを抽出。
    # 分析でBT<40はPF1.4/薄利、BT≥40はPF3〜6.5と判明したため、良い層をハイライトする。
    # 予算・BTxxタブの足切り/並べ替えは【画面表示のBT(rec_score=凍結シグナル時BT)】と一致させる。
    #   旧実装は _LONG_BT_REF(別測定のロングBT・BTキャッシュ由来)を優先していたため、
    #   「表示はBT76なのに予算タブでは別BTで40未満と判定され足切り」→ BTキャッシュ再構築の
    #   たびに40ラインを跨いで 200↔100 とフリッカーするバグがあった(#9 三井E&S VOLTF)。
    #   表示と一致させ、凍結BT(signal_score_cache由来=安定)で判定することで解消。
    def _eff_long_bt(t) -> float:
        return t.get("rec_score") or 0
    # 「BTスコア○以上」タブの下限。既定50(env LSS_BT_TAB_MIN で変更可。30に戻すなら =30)。
    _BT_TAB_MIN = int(os.environ.get("LSS_BT_TAB_MIN", "50") or "50")
    bt40_trades = [t for t in sorted_trades if _eff_long_bt(t) >= _BT_TAB_MIN]
    bt60_trades = [t for t in sorted_trades if _eff_long_bt(t) >= 60]
    # エントリー日降順（発注中を先頭、それ以外はエントリー日降順）
    entry_sorted_trades = pending_trades + sorted(
        done_trades,
        key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"],
        reverse=True
    )
    # 全取引タブ: 転換トレードは表示上限カット外でも必ず含める
    # 「全部」タブは参照用途が主なので通常トレードのキャップを小さく抑える(高速化)。
    # BT40+タブが主力分析のため全部タブは直近300件＋転換全件で十分。
    _ALL_TAB_CAP = min(_DETAIL_ROW_CAP, 300)
    _tenkan_in_sorted = [t for t in sorted_trades if t.get("strategy") == "転換"]
    _non_tenkan_sorted = [t for t in sorted_trades if t.get("strategy") != "転換"]
    _capped_non_tenkan = _non_tenkan_sorted[:_ALL_TAB_CAP]
    _capped_ids_all = {id(t) for t in _capped_non_tenkan}
    _tenkan_extra_all = [t for t in _tenkan_in_sorted if id(t) not in _capped_ids_all]
    _sorted_trades_for_all = sorted(
        _capped_non_tenkan + _tenkan_extra_all,
        key=lambda x: x["exit_d_raw"], reverse=True
    )
    trade_rows_all  = _rows_for(_sorted_trades_for_all, f"直近{days}日に決済した取引なし", cap=0)
    trade_rows_bt70 = _rows_for(bt70_trades,   "BT70以上の取引なし")
    trade_rows_bt40 = _rows_for(bt40_trades,   f"BT{_BT_TAB_MIN}以上の取引なし")
    trade_rows_bt60 = _rows_for(bt60_trades,   "BT60以上の取引なし")

    # ── ㉒ シグナル数別 成績（その日のBT70シグナル数と成績の関係）──
    from collections import defaultdict as _dd_b

    def _breadth_rows(trs):
        """trs(BT70の決着済トレード)をエントリー日でまとめ、その日のシグナル数(件数)で
        バケツ分けして成績を返す。"""
        by_ed: dict = _dd_b(list)
        for t in trs:
            ed = t.get("entry_d_raw")
            if ed:
                by_ed[str(ed)].append(t)
        buckets = [("1〜2件", 1, 2), ("3〜5件", 3, 5),
                   ("6〜10件", 6, 10), ("11件以上", 11, 999999)]
        out = []
        for lab, lo, hi in buckets:
            dts = [d for d, ts in by_ed.items() if lo <= len(ts) <= hi]
            group = [t for d in dts for t in by_ed[d]]
            if not group:
                out.append((lab, 0, 0, 0.0, None, 0, 0, 0.0, 0.0))
                continue
            n = len(group)
            wins = sum(1 for t in group if t["pnl"] > 0)
            stops = sum(1 for t in group if t.get("reason") == "損切り")
            tcs = sum(1 for t in group if t.get("reason") == "タイムカット")
            gp = sum(t["pnl"] for t in group if t["pnl"] > 0)
            gl = -sum(t["pnl"] for t in group if t["pnl"] < 0)
            pf = (gp / gl) if gl > 0 else None
            tot = sum(t["pnl"] for t in group)
            out.append((lab, len(dts), n, wins / n * 100, pf, tot,
                        tot / n, stops / n * 100, tcs / n * 100))
        return out

    _bt70_done = [t for t in bt70_trades
                  if t.get("reason") in ("目標達成", "損切り", "タイムカット")]
    _brow_html = ""
    for lab, ndays, n, wr, pf, tot, avg, sr, tcr in _breadth_rows(_bt70_done):
        if n == 0:
            _brow_html += (f'<tr><td>{lab}</td><td colspan="8" '
                           f'style="text-align:center;color:#64748b">該当なし</td></tr>')
            continue
        pfs = "∞" if pf is None else f"{pf:.2f}"
        totc = "#4ade80" if tot >= 0 else "#f87171"
        _brow_html += (
            f'<tr><td>{lab}</td><td style="text-align:right">{ndays}</td>'
            f'<td style="text-align:right">{n}</td>'
            f'<td style="text-align:right">{wr:.0f}%</td>'
            f'<td style="text-align:right">{pfs}</td>'
            f'<td style="text-align:right;color:{totc};font-weight:600">{tot:+,.0f}</td>'
            f'<td style="text-align:right">{avg:+,.0f}</td>'
            f'<td style="text-align:right;color:#f87171">{sr:.0f}%</td>'
            f'<td style="text-align:right;color:#94a3b8">{tcr:.0f}%</td></tr>')
    _signal_breadth_html = f"""<h2>㉒ シグナル数別 成績（BT70シグナルが多い日ほど良いか）</h2>
<p style="color:#94a3b8;font-size:0.85rem;margin-bottom:10px;line-height:1.7">
同じエントリー日に BT70以上のシグナルが何件出たか（＝相場の広がり／ブレッド）でバケツ分けし、
その日に入ったBT70トレードのその後の成績を集計。<br>
・<b>件数が多い日ほど勝率・PF・平均損益が高ければ「多く出た日ほど張り時」</b>（ブレッドが有効な指標）。<br>
・<b>『1〜2件』など少ない日のバケツで損切率が高ければ、損切りは“孤立シグナルの日”に集中</b>していた
（＝あなたの仮説「損切が多い時は少シグナルの日だった」の検証）。</p>
<table>
  <thead><tr>
    <th>その日のBT70シグナル数</th><th>日数</th><th>取引数</th><th>勝率</th><th>PF</th>
    <th>総損益</th><th>平均/取引</th><th style="color:#f87171">損切率</th>
    <th style="color:#94a3b8">TC率</th>
  </tr></thead>
  <tbody>{_brow_html}</tbody>
</table>
<p class="footnote">※ 「その日のシグナル数」= 同一エントリー日に決着したBT70トレードの件数
（未約定・期限切れは含まない近似）。日数が少ないバケツは統計的に不安定。</p>"""

    # ── 同時保有の最大必要資金（sweep-line） ──────────────────────────
    # 各ポジションが約定額(entry_p×qty)を entry_d〜exit_d の間だけ拘束すると考え、
    # 時系列を走査して「同時に拘束されている資金」のピークを求める。
    # = この取引群を全部やるのに最低限必要な資金。発注中(未約定)は拘束なしで除外。
    # 同日は entry を exit より先に処理して保守的(最大)ピークを取る。
    def _peak_capital(trades):
        evts = []
        total = 0.0
        for t in trades:
            if t.get("reason") == "発注中":
                continue
            ed = t.get("entry_d_raw"); xd = t.get("exit_d_raw")
            cap = (t.get("entry_p") or 0) * (t.get("qty") or 0)
            if ed is None or cap <= 0:
                continue
            total += cap
            evts.append((ed, 0, cap, 1))       # 約定=資金拘束+
            if xd is not None:
                evts.append((xd, 1, -cap, -1))  # 決済=解放(同日約定の後)
        if not evts:
            return 0.0, None, 0, 0, 0.0
        evts.sort(key=lambda e: (e[0], e[1]))
        cur_cap = cur_cnt = 0.0
        peak_cap = 0.0; peak_date = None; peak_cnt = 0; max_cnt = 0
        for d, _o, dcap, dcnt in evts:
            cur_cap += dcap; cur_cnt += dcnt
            if cur_cnt > max_cnt:
                max_cnt = int(cur_cnt)
            if cur_cap > peak_cap:
                peak_cap = cur_cap; peak_date = d; peak_cnt = int(cur_cnt)
        return peak_cap, peak_date, peak_cnt, max_cnt, total

    # ── エントリー日別グリッド HTML ──────────────────────────────────
    from collections import defaultdict as _dd
    _ENTRY_GRID_DAYS = days  # グリッド表示は分析期間全体

    def _build_entry_grid(trades_list, prefix):
        """trades_list をエントリー日でグループ化して (by_date, sorted_dates) を返す。"""
        by_date: dict = _dd(list)
        cutoff_d = until - timedelta(days=_ENTRY_GRID_DAYS)
        for _t in trades_list:
            _dk = str(_t.get("entry_d_raw") or _t["exit_d_raw"])
            # 転換トレードはOOS期間全体を表示するためカットオフを適用しない
            if _dk >= str(cutoff_d) or _t.get("strategy") == "転換":
                by_date[_dk].append(_t)
        return by_date, sorted(by_date.keys(), reverse=True)

    _entry_by_date, _sorted_entry_dates = _build_entry_grid(entry_sorted_trades, "e")
    _bt70_entry_sorted = pending_trades + sorted(
        [t for t in done_trades if (t.get("rec_score") or 0) >= 70],
        key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"],
        reverse=True
    )
    _bt70_entry_by_date, _sorted_bt70_entry_dates = _build_entry_grid(_bt70_entry_sorted, "b")

    # BT40以上(質の高い銘柄)のエントリー日別グリッド。判定は _eff_long_bt
    # (=表示のシグナル時BT rec_score。mirror/lss も表示と一致・安定)。
    _bt40_entry_sorted = pending_trades + sorted(
        [t for t in done_trades if _eff_long_bt(t) >= _BT_TAB_MIN],
        key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"],
        reverse=True
    )
    _bt40_entry_by_date, _sorted_bt40_entry_dates = _build_entry_grid(_bt40_entry_sorted, "c")
    # 予算シミュ専用の候補プール(BT30固定・表示閾値 _BT_TAB_MIN とは独立)。予算はBT降順で埋める
    # ため実質高BTのみ約定するが、プールは従来どおりBT30から用意して挙動を変えない。
    _bt30_entry_sorted = pending_trades + sorted(
        [t for t in done_trades if _eff_long_bt(t) >= 30],
        key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"],
        reverse=True
    )
    # 予算固定シミュ: 毎日その日のBT降順で、予算(既定400万円)まで注文した場合の成績。lssのみ。
    #  ・「終値で判断」: 予算に収まるかは注文トリガー価格(order_limit=前日終値ベース)×株数で判定
    #    (約定値ではない=発注時点で確定する必要資金)。
    #  ・「注文時に枠を消費」: 約定した注文だけでなく、不約定(トリガー未達/ギャップ過大)の注文も
    #    発注枠を消費する(=その下のBTの約定を締め出す)。損益・グリッドには約定分のみ計上。
    #  ・同日決済なので予算は毎日リセット。予算は環境変数 LSS_BUDGET_MAN(万, 既定400)で変更可。
    try:
        _budget_yen = float(os.environ.get("LSS_BUDGET_MAN", "400")) * 1e4
    except Exception:
        _budget_yen = 4e6
    _budget_man = int(_budget_yen / 1e4)
    # 予算シミュのBT下限(既定30)。LSS_BUDGET_MIN_BT=70 で「全取引をBT70以上に集中」できる。
    # 複数基準月をunionマージした大きなプールと組み合わせると、高BTだけで枠が埋まりやすくなる。
    try:
        _BUD_MIN_BT = max(30, int(os.environ.get("LSS_BUDGET_MIN_BT", "30")))
    except Exception:
        _BUD_MIN_BT = 30

    def _order_notional(_t):
        # 注文時の必要資金 = 注文トリガー価格(終値ベース) × 株数。約定値ではない。
        _op = float(_t.get("order_limit", 0) or 0) or float(_t.get("entry_p", 0) or 0)
        return _op * float(_t.get("qty", 0) or 0)

    def _run_budget_sim(_min_bt, strat_set=None, fill_budget=False, multi_lot=False):
        """毎日その日のBT降順で予算まで注文したときの『約定トレード』を返す(BT下限=_min_bt)。
        strat_set: 戦略名のセット(例: {"A7","RSI2","VOLTF"})。Noneなら全戦略。
        fill_budget=True: 約定額ベース(kabuステーションwatch取り消し方式)。
          不約定は予算を消費しない。約定価格×株数で累計し、超過したらbreak。
          不約定でも枠を消費する発注額ベース(=既定)より1日の約定件数が増えやすい。
        multi_lot=True: ループ充填モード。
          BT降順に100株ずつ1周目を配置後、予算残があれば先頭から再度100株ずつ追加。
          400万円に限りなく近づくまでループ。出力は株数n×100の合成トレード。
        """
        _out = []
        if not _LSS_ORDER_MODE:
            return _out

        if multi_lot:
            # ── ループ充填モード ──────────────────────────────────────
            # 約定済みトレードのみ対象(不約定は実際に買えないので除外)。
            _by_day_ml: dict = _dd(list)
            for _t in _bt30_entry_sorted:
                if _eff_long_bt(_t) < _min_bt:
                    continue
                if strat_set and _t.get("strategy", "").upper() not in strat_set:
                    continue
                if _t.get("reason") == "約定せず":
                    continue
                _dk = str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or "")
                _by_day_ml[_dk].append(_t)
            for _dk, _day_trades in _by_day_ml.items():
                _sorted_day = sorted(_day_trades, key=lambda x: -_eff_long_bt(x))
                if not _sorted_day:
                    continue
                # 各トレードの単価(100株あたりコスト)
                _units = {id(_t): float(_t.get("entry_p", 0) or 0) * 100
                          for _t in _sorted_day}
                _lots: dict = {id(_t): 0 for _t in _sorted_day}
                _cap = 0.0
                _changed = True
                while _changed:
                    _changed = False
                    for _t in _sorted_day:
                        _u = _units[id(_t)]
                        if _u <= 0:
                            continue
                        if _cap + _u > _budget_yen:
                            continue
                        _lots[id(_t)] += 1
                        _cap += _u
                        _changed = True
                        if _cap >= _budget_yen:
                            break
                for _t in _sorted_day:
                    _n = _lots[id(_t)]
                    if _n <= 0:
                        continue
                    # 株数・損益をnロット分にスケールした合成トレードを生成
                    _syn = dict(_t)
                    _syn["qty"] = _n * 100
                    _syn["pnl"] = float(_t.get("pnl", 0) or 0) * _n
                    _out.append(_syn)
            _out.sort(key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"], reverse=True)
            return _out

        _by_day_bud: dict = _dd(list)
        for _t in _bt30_entry_sorted:
            if _eff_long_bt(_t) < _min_bt:
                continue
            if strat_set and _t.get("strategy", "").upper() not in strat_set:
                continue
            _by_day_bud[str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or "")].append(_t)
        if not fill_budget:
            # 発注額ベース: 不約定も同日バケットへ(枠は消費するが損益0・グリッド非表示)。
            # ただし同一銘柄同日に約定注文があれば二重発注しない(1銘柄1注文/日)。
            _fill_sym_day = {(_t.get("symbol"),
                              str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or ""))
                             for _t in _bt30_entry_sorted
                             if not strat_set or _t.get("strategy", "").upper() in strat_set}
            for _t in all_nofills:
                if _eff_long_bt(_t) < _min_bt:
                    continue
                if strat_set and _t.get("strategy", "").upper() not in strat_set:
                    continue
                _dk2 = str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or "")
                if (_t.get("symbol"), _dk2) in _fill_sym_day:
                    continue
                _by_day_bud[_dk2].append(_t)
        for _dk in _by_day_bud:
            _cap = 0.0
            for _t in sorted(_by_day_bud[_dk], key=lambda x: -_eff_long_bt(x)):
                if fill_budget:
                    # 約定額ベース: 不約定はスキップ(予算消費なし)、約定価格×株数で管理
                    if _t.get("reason") == "約定せず":
                        continue
                    _no = float(_t.get("entry_p", 0) or 0) * float(_t.get("qty", 0) or 0)
                    if _no <= 0:
                        continue
                    if _cap + _no > _budget_yen:
                        break  # watch発動→以降の注文をキャンセル
                    _cap += _no
                    _out.append(_t)
                else:
                    _no = _order_notional(_t)
                    if _no <= 0 or _cap + _no > _budget_yen:
                        continue   # 予算超過はスキップ(次の安い注文が入るか試す=貪欲)
                    _cap += _no
                    if _t.get("reason") != "約定せず":
                        _out.append(_t)   # 約定分だけグリッド・損益に計上
        _out.sort(key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"], reverse=True)
        return _out

    # 予算タブは _BT_TAB_MIN(既定50)以上のみで発注。BT降順で埋めるので実質高BTのみだが、
    # 下限を明示的に _BT_TAB_MIN に揃える(『BT50以上』表示と一致)。
    _budget_entry_sorted = _run_budget_sim(max(_BUD_MIN_BT, _BT_TAB_MIN))
    # 400万×BT降順(BT50以上)版。予算はBT降順で埋めるため多くの日はBT30版と同一になるが、
    # 薄い日(BT50候補が予算に満たない日)はBT30-49の穴埋めが無くなるぶん差が出る。
    # _BUD_MIN_BT が既に50以上なら重複するので作らない。
    _budget50_entry_sorted: list = []   # 予算タブ本体をBT50化したので別タブは廃止
    # A7/RSI2/VOLTF戦略限定版(決済日別タブの代替)。
    _STRAT_NARROW = {"A7", "RSI2", "VOLTF"}
    _budget_narrow_entry_sorted = _run_budget_sim(max(_BUD_MIN_BT, _BT_TAB_MIN), strat_set=_STRAT_NARROW) if _LSS_ORDER_MODE else []
    # 約定額ベース(watchで取り消し方式): 不約定は予算消費しない。全戦略・絞り版。
    _budget_fill_entry_sorted = _run_budget_sim(max(_BUD_MIN_BT, _BT_TAB_MIN), fill_budget=True) if _LSS_ORDER_MODE else []
    _budget_fill_narrow_entry_sorted = _run_budget_sim(max(_BUD_MIN_BT, _BT_TAB_MIN), strat_set=_STRAT_NARROW, fill_budget=True) if _LSS_ORDER_MODE else []
    # ループ充填版: BT降順に100株ずつ繰り返し追加し、400万円に限りなく近づける。全戦略・絞り版。
    _budget_mlot_entry_sorted = _run_budget_sim(max(_BUD_MIN_BT, _BT_TAB_MIN), multi_lot=True) if _LSS_ORDER_MODE else []
    _budget_mlot_narrow_entry_sorted = _run_budget_sim(max(_BUD_MIN_BT, _BT_TAB_MIN), strat_set=_STRAT_NARROW, multi_lot=True) if _LSS_ORDER_MODE else []
    # 基準月スイープ用: 400万×BT予算フィルター後の月別P&LをCSV出力(env LSS_BUDGET_MONTHLY_CSV=path)。
    # 複数の基準月マージを1つずつ回して、この月別成績を比較する用途(sweep_base_months.py)。
    # ※ _tab5_pnl_html は「メインタブ」以外に「銘柄詳細(symbol_filter)」「期間パネル(短い days)」でも
    #   多数回呼ばれる。フィルター付き呼び出しは _budget_entry_sorted が部分的/空になるので、
    #   フィルター無し かつ 最も月数の多い(=最長窓の)呼び出しだけを採用して上書き事故を防ぐ。
    _bud_csv = os.environ.get("LSS_BUDGET_MONTHLY_CSV", "").strip()
    if (_bud_csv and _LSS_ORDER_MODE and cfg_filter is None
            and not symbol_filter and not strategy_filter):
        try:
            import csv as _csvmod
            _bm: dict = {}
            for _t in _budget_entry_sorted:
                if _t.get("reason") in ("発注中", "保有中"):
                    continue
                _ym = str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or "")[:7]
                if not _ym:
                    continue
                _pv = float(_t.get("pnl", 0) or 0)
                _e = _bm.setdefault(_ym, {"n": 0, "win": 0, "pnl": 0.0})
                _e["n"] += 1
                _e["pnl"] += _pv
                if _pv > 0:
                    _e["win"] += 1
            global _BUD_CSV_BEST_MONTHS
            if len(_bm) >= _BUD_CSV_BEST_MONTHS:   # 最長窓(月数最多)の呼び出しだけ採用
                _BUD_CSV_BEST_MONTHS = len(_bm)
                with open(_bud_csv, "w", newline="", encoding="utf-8-sig") as _f:
                    _w = _csvmod.writer(_f)
                    _w.writerow(["month", "trades", "win_rate", "pnl", "budget_man", "min_bt"])
                    for _ym in sorted(_bm):
                        _e = _bm[_ym]
                        _w.writerow([_ym, _e["n"],
                                     round(_e["win"] / _e["n"] * 100, 1) if _e["n"] else 0,
                                     round(_e["pnl"], 0), _budget_man, _BUD_MIN_BT])
                print(f"[予算月別CSV] {_bud_csv} に {len(_bm)}ヶ月を出力", flush=True)
        except Exception as _ce:
            print(f"[予算月別CSV] 出力失敗: {_ce}", flush=True)
    _budget_entry_by_date, _sorted_budget_entry_dates = _build_entry_grid(_budget_entry_sorted, "q")
    # 400万円タブ用: ショートのみ（転換トレードを除外）
    _budget_entry_sorted_short = [t for t in _budget_entry_sorted if t.get("strategy") != "転換"]
    _budget_entry_by_date_short, _sorted_budget_entry_dates_short = _build_entry_grid(_budget_entry_sorted_short, "q")
    _budget50_entry_by_date, _sorted_budget50_entry_dates = _build_entry_grid(_budget50_entry_sorted, "q5")
    # BT70以上のみ投資版予算シミュ（高品質集中）。
    _budget60_entry_sorted = _run_budget_sim(60) if _LSS_ORDER_MODE else []
    _budget60_entry_sorted_short = [t for t in _budget60_entry_sorted if t.get("strategy") != "転換"]
    _budget60_entry_by_date_short, _sorted_budget60_entry_dates_short = _build_entry_grid(_budget60_entry_sorted_short, "q6")
    _budget_narrow_entry_by_date, _sorted_budget_narrow_entry_dates = _build_entry_grid(_budget_narrow_entry_sorted, "qn")
    _budget_fill_entry_by_date, _sorted_budget_fill_entry_dates = _build_entry_grid(_budget_fill_entry_sorted, "qf")
    _budget_fill_narrow_entry_by_date, _sorted_budget_fill_narrow_entry_dates = _build_entry_grid(_budget_fill_narrow_entry_sorted, "qfn")
    _budget_mlot_entry_by_date, _sorted_budget_mlot_entry_dates = _build_entry_grid(_budget_mlot_entry_sorted, "qml")
    _budget_mlot_narrow_entry_by_date, _sorted_budget_mlot_narrow_entry_dates = _build_entry_grid(_budget_mlot_narrow_entry_sorted, "qmln")

    # OOS予算シミュCSV出力 (env LSS_OOS_BUDGET_CSV=path)。
    # _tab5_pnl_html が各ホールドアウト設定(HO30d〜HO180d)で呼ばれるたびに追記する。
    # LSS_OOS_BUDGET_DAYS: 出力対象のdays値をカンマ区切りで指定(例: "365,730")。
    #   省略時はホールドアウト候補 {30,60,90,120,150,180}。
    _oos_csv_path = os.environ.get("LSS_OOS_BUDGET_CSV", "").strip()
    _oos_days_env = os.environ.get("LSS_OOS_BUDGET_DAYS", "").strip()
    _oos_bt_tiers_env = os.environ.get("LSS_OOS_BUDGET_BT_TIERS", "").strip()
    _ho_days_set = (
        {int(x) for x in _oos_days_env.split(",") if x.strip().isdigit()}
        if _oos_days_env else {30, 60, 90, 120, 150, 180}
    )
    # BT閾値リスト: 既定は _BUD_MIN_BT のみ。LSS_OOS_BUDGET_BT_TIERS="30,60" で複数層を1回のスイープで出力可。
    _oos_bt_tiers = sorted({_BUD_MIN_BT} | (
        {int(x) for x in _oos_bt_tiers_env.split(",") if x.strip().isdigit()}
        if _oos_bt_tiers_env else set()
    ))
    if (_oos_csv_path and _LSS_ORDER_MODE and days in _ho_days_set
            and cfg_filter is None and not symbol_filter and not strategy_filter):
        try:
            import csv as _ocsv
            _cfg_lbl = "/".join(c.get("label", "") for c in _PNL_CONFIGS) if _PNL_CONFIGS else ""
            _oos_rows = []
            for _obt in _oos_bt_tiers:
                # OOS出力は常に _run_budget_sim を直接呼ぶ。
                # 通常の予算シミュは max(_BUD_MIN_BT, _BT_TAB_MIN) で閾値が引き上がるが、
                # OOSでは各 BT 層を正確に反映するため _BT_TAB_MIN を介さず直呼び。
                _e_s = _run_budget_sim(_obt)
                _fne_s = _run_budget_sim(_obt, fill_budget=True)
                _ml_s = _run_budget_sim(_obt, multi_lot=True)
                _mln_s = _run_budget_sim(_obt, strat_set=_STRAT_NARROW, multi_lot=True)
                for _osim_name, _osim_trades in [
                    ("通常予算", _e_s),
                    ("約定額ベース", _fne_s),
                    ("ループ充填_全戦略", _ml_s),
                    ("ループ充填_絞り", _mln_s),
                ]:
                    _oby_ym: dict = {}
                    for _ot in _osim_trades:
                        if _ot.get("reason") in ("発注中", "保有中"):
                            continue
                        _oym = str(_ot.get("entry_d_raw") or _ot.get("exit_d_raw") or "")[:7]
                        if not _oym:
                            continue
                        _oe = _oby_ym.setdefault(_oym, {"n": 0, "wins": 0, "pnl": 0.0})
                        _oe["n"] += 1
                        _opv = float(_ot.get("pnl", 0) or 0)
                        _oe["pnl"] += _opv
                        if _opv > 0:
                            _oe["wins"] += 1
                    for _oym, _oe in sorted(_oby_ym.items()):
                        _owr = round(_oe["wins"] / _oe["n"] * 100, 1) if _oe["n"] else 0.0
                        _oos_rows.append({
                            "holdout_days": days,
                            "config": _cfg_lbl,
                            "sim_type": _osim_name,
                            "month": _oym,
                            "trades": _oe["n"],
                            "wins": _oe["wins"],
                            "win_rate_pct": _owr,
                            "pnl": round(_oe["pnl"], 0),
                            "budget_man": _budget_man,
                            "min_bt": _obt,
                        })
            if _oos_rows:
                _oflds = ["holdout_days", "config", "sim_type", "month",
                          "trades", "wins", "win_rate_pct", "pnl", "budget_man", "min_bt"]
                _oappend = os.path.exists(_oos_csv_path)
                with open(_oos_csv_path, "a" if _oappend else "w",
                          newline="", encoding="utf-8-sig") as _of:
                    _ow = _ocsv.DictWriter(_of, fieldnames=_oflds)
                    if not _oappend:
                        _ow.writeheader()
                    _ow.writerows(_oos_rows)
                print(f"[OOS予算CSV] {_oos_csv_path} HO{days}d BT層{_oos_bt_tiers}: {len(_oos_rows)}行追記", flush=True)
        except Exception as _oce:
            print(f"[OOS予算CSV] 出力失敗: {_oce}", flush=True)

    # OOS生トレードCSV出力 (env LSS_OOS_RAW_CSV=path)。
    # BTスコア付き1行1トレードで出力 → sim_oos_budget.py で任意BT閾値をpost-hocに再シミュ可能。
    # LSS_OOS_MONTH: OOSとして抽出する月(YYYY-MM)。スイープスクリプトが折ごとに設定。
    # LSS_OOS_FOLD / LSS_OOS_TRAIN_MONTHS: フォールドメタデータ。CSVに列として含む。
    _oos_raw_path = os.environ.get("LSS_OOS_RAW_CSV", "").strip()
    _oos_month_filter = os.environ.get("LSS_OOS_MONTH", "").strip()
    _oos_fold_label = os.environ.get("LSS_OOS_FOLD", "").strip()
    _oos_train_label = os.environ.get("LSS_OOS_TRAIN_MONTHS", "").strip()
    if (_oos_raw_path and _LSS_ORDER_MODE and _oos_month_filter and
            cfg_filter is None and not symbol_filter and not strategy_filter):
        try:
            import csv as _rcsv
            _rflds = ["fold", "train_months", "oos_month", "entry_date",
                      "symbol", "name", "strategy", "bt_score", "entry_p", "pnl", "filled"]
            _raw_rows = []
            # 約定済みトレード (BT≥30 は _bt30_entry_sorted 構築時に保証済み)
            for _rt in _bt30_entry_sorted:
                _rym = str(_rt.get("entry_d_raw") or _rt.get("exit_d_raw") or "")[:7]
                if _rym != _oos_month_filter:
                    continue
                _raw_rows.append({
                    "fold": _oos_fold_label,
                    "train_months": _oos_train_label,
                    "oos_month": _oos_month_filter,
                    "entry_date": str(_rt.get("entry_d_raw") or ""),
                    "symbol": _rt.get("symbol", ""),
                    "name": str(_rt.get("name", "")).replace(",", "、"),
                    "strategy": _rt.get("strategy", ""),
                    "bt_score": round(float(_eff_long_bt(_rt)), 1),
                    "entry_p": round(float(_rt.get("entry_p", 0) or 0), 1),
                    "pnl": round(float(_rt.get("pnl", 0) or 0), 0),
                    "filled": 1,
                })
            # 不約定トレード(発注枠は消費・損益0)。通常予算モード専用。
            for _rt in all_nofills:
                _rym = str(_rt.get("entry_d_raw") or _rt.get("exit_d_raw") or "")[:7]
                if _rym != _oos_month_filter:
                    continue
                _raw_rows.append({
                    "fold": _oos_fold_label,
                    "train_months": _oos_train_label,
                    "oos_month": _oos_month_filter,
                    "entry_date": str(_rt.get("entry_d_raw") or ""),
                    "symbol": _rt.get("symbol", ""),
                    "name": str(_rt.get("name", "")).replace(",", "、"),
                    "strategy": _rt.get("strategy", ""),
                    "bt_score": round(float(_eff_long_bt(_rt)), 1),
                    "entry_p": round(float(_rt.get("entry_p", 0) or 0), 1),
                    "pnl": 0.0,
                    "filled": 0,
                })
            if _raw_rows:
                _rappend = os.path.exists(_oos_raw_path)
                with open(_oos_raw_path, "a" if _rappend else "w",
                          newline="", encoding="utf-8-sig") as _rf:
                    _rw = _rcsv.DictWriter(_rf, fieldnames=_rflds)
                    if not _rappend:
                        _rw.writeheader()
                    _rw.writerows(_raw_rows)
                print(f"[OOS生CSV] {_oos_raw_path} fold={_oos_fold_label} {_oos_month_filter}: "
                      f"{len(_raw_rows)}行追記 (約定{sum(r['filled'] for r in _raw_rows)}/"
                      f"不約定{sum(1-r['filled'] for r in _raw_rows)})", flush=True)
        except Exception as _rce:
            print(f"[OOS生CSV] 出力失敗: {_rce}", flush=True)

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

    def _month_accordion_html(by_date, sorted_dates, dseq, pfx, expand_months=2, expand_tenkan=True):
        """月折りたたみアコーディオン＋インライン詳細HTML生成。expand_tenkan=False なら転換月を自動展開しない。"""
        by_month = _group_by_month(sorted_dates)
        html = ""
        for i, (ym, dks) in enumerate(by_month.items()):
            ym_key    = ym.replace("-", "")
            all_t     = [t for dk in dks for t in by_date[dk]]
            has_tenkan  = expand_tenkan and any(t.get("strategy") == "転換" for t in all_t)
            is_open     = (i < expand_months) or has_tenkan
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
                f'<div class="mg-header" onclick="toggleMG(this)">'
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
        # その日の最大必要資金(同時保有ピーク=約定額の同時拘束)。同日決済なら約定額合計。
        _cap_d = _peak_capital(trades_d)[0]
        cap_span = (f'<span style="color:#38bdf8;font-size:0.6rem">要¥{_cap_d:,.0f}</span>'
                    if _cap_d > 0 else "")
        dk_key = dk.replace("-", "")
        return (f'<button class="edate-btn" id="{pfx}date_btn_{dseq}_{dk_key}" '
                f'onclick="showEntryDateGrid(this,\'{dk_key}\')">'
                f'<span class="edate-mm">{mm_dd}</span>'
                f'<span class="edate-stat">{len(trades_d)}件 {wr_d:.0f}%</span>'
                f'<span class="edate-pnl" style="color:{pnl_col}">{pnl_d:+,.0f}</span>'
                f'{cap_span}{pend_span}</button>')

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
  <span style="font-size:0.8rem;color:#38bdf8">最大必要資金 <span style="font-weight:700">¥{_peak_capital(trades_d)[0]:,.0f}</span></span>
</div>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>エントリー</th><th style="text-align:left">銘柄</th><th>戦略</th><th>設定</th>
    <th>約定値<br><small style="color:#94a3b8">逆指値/指値</small></th><th style="color:#f87171">損切り</th><th style="color:#4ade80">目標</th>
    <th>現在値</th><th>決済値</th><th>株数</th><th>保有</th><th>遅延</th>
    <th>損益</th><th>理由</th><th>決済日</th>
  </tr></thead>
  <tbody>{rows_d}</tbody>
</table>
</div>
</div>"""

    # ── 決済日別グリッド（目標達成/損切り/タイムカット 基準） ──────────────
    _REASON_ORDER = {"目標達成": 0, "損切り": 1, "タイムカット": 2}
    _DONE_REASONS = ("目標達成", "損切り", "タイムカット")

    def _build_exit_grid(trades_list):
        """trades_list を決済日でグループ化（決着済のみ）。(by_date, sorted_dates)。"""
        by_date: dict = _dd(list)
        cutoff_d = until - timedelta(days=_ENTRY_GRID_DAYS)
        for _t in trades_list:
            if _t.get("reason") not in _DONE_REASONS:
                continue
            _dk = str(_t.get("exit_d_raw") or "")
            if _dk and _dk >= str(cutoff_d):
                by_date[_dk].append(_t)
        return by_date, sorted(by_date.keys(), reverse=True)

    def _exit_reason_counts(trades_d):
        n_t = sum(1 for t in trades_d if t.get("reason") == "目標達成")
        n_s = sum(1 for t in trades_d if t.get("reason") == "損切り")
        n_c = sum(1 for t in trades_d if t.get("reason") == "タイムカット")
        return n_t, n_s, n_c

    def _exit_date_btn(dk, dseq, by_date, pfx):
        trades_d = by_date[dk]
        n_t, n_s, n_c = _exit_reason_counts(trades_d)
        pnl_d = sum(t["pnl"] for t in trades_d)
        pnl_col = "#4ade80" if pnl_d >= 0 else "#f87171"
        mm_dd = dk[5:7] + "/" + dk[8:10]
        dk_key = dk.replace("-", "")
        brk = ('<span style="font-size:0.62rem">'
               f'<span style="color:#4ade80">目{n_t}</span> '
               f'<span style="color:#f87171">損{n_s}</span> '
               f'<span style="color:#94a3b8">T{n_c}</span></span>')
        return (f'<button class="edate-btn" id="{pfx}date_btn_{dseq}_{dk_key}" '
                f'onclick="showEntryDateGrid(this,\'{dk_key}\')">'
                f'<span class="edate-mm">{mm_dd}</span>'
                f'<span class="edate-stat">{len(trades_d)}件</span>'
                f'<span class="edate-pnl" style="color:{pnl_col}">{pnl_d:+,.0f}</span>'
                f'{brk}</button>')

    def _exit_date_detail(dk, dseq, show, by_date, pfx):
        trades_d = sorted(by_date[dk],
                          key=lambda t: (_REASON_ORDER.get(t.get("reason"), 9), -t["pnl"]))
        n_t, n_s, n_c = _exit_reason_counts(trades_d)
        pnl_d = sum(t["pnl"] for t in trades_d)
        pnl_col = "#4ade80" if pnl_d >= 0 else "#f87171"
        rows_d = "".join(_build_trade_row(t, entry_first=False) for t in trades_d)
        dk_key = dk.replace("-", "")
        disp = "block" if show else "none"
        return f"""<div id="{pfx}date_detail_{dseq}_{dk_key}" style="display:{disp}">
<div style="padding:8px 0 12px;margin-bottom:8px;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:16px;flex-wrap:wrap">
  <span style="font-size:0.9rem;font-weight:700;color:#a78bfa">{dk} の決済</span>
  <span style="font-size:0.8rem;color:#94a3b8">{len(trades_d)}件決済 &nbsp;
    <span style="color:#4ade80">目標達成{n_t}</span> /
    <span style="color:#f87171">損切り{n_s}</span> /
    <span style="color:#94a3b8">タイムカット{n_c}</span>
    &nbsp;損益<span style="color:{pnl_col};font-weight:700">{pnl_d:+,.0f}円</span></span>
</div>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>決済日</th><th style="text-align:left">銘柄</th><th>戦略</th><th>設定</th>
    <th>約定値<br><small style="color:#94a3b8">逆指値/指値</small></th><th style="color:#f87171">損切り</th><th style="color:#4ade80">目標</th>
    <th>現在値</th><th>決済値</th><th>株数</th><th>保有</th><th>遅延</th>
    <th>損益</th><th>理由</th><th>エントリー</th>
  </tr></thead>
  <tbody>{rows_d}</tbody>
</table>
</div>
</div>"""

    def _month_accordion_exit_html(by_date, sorted_dates, dseq, pfx, expand_months=2):
        """決済日別の月折りたたみアコーディオン（理由内訳付き）。"""
        by_month = _group_by_month(sorted_dates)
        html = ""
        for i, (ym, dks) in enumerate(by_month.items()):
            is_open   = (i < expand_months)
            ym_key    = ym.replace("-", "")
            all_t     = [t for dk in dks for t in by_date[dk]]
            n_t, n_s, n_c = _exit_reason_counts(all_t)
            pnl_m     = sum(t["pnl"] for t in all_t)
            pnl_col   = "#4ade80" if pnl_m >= 0 else "#f87171"
            arrow     = "▼" if is_open else "▶"
            body_disp = "block" if is_open else "none"
            btns_html = "".join(_exit_date_btn(dk, dseq, by_date, pfx) for dk in dks)
            dets_html = "".join(_exit_date_detail(dk, dseq, False, by_date, pfx) for dk in dks)
            html += (
                f'<div class="mg-block">'
                f'<div class="mg-header" onclick="toggleMG(this)">'
                f'<span class="mg-arrow" id="mg_arr_{pfx}{dseq}_{ym_key}">{arrow}</span>'
                f'<span class="mg-ym">{ym[:4]}/{ym[5:7]}月</span>'
                f'<span class="mg-stats">{len(all_t)}件決済&nbsp;'
                f'<span style="color:#4ade80">目{n_t}</span> '
                f'<span style="color:#f87171">損{n_s}</span> '
                f'<span style="color:#94a3b8">T{n_c}</span>&nbsp;'
                f'<span style="color:{pnl_col};font-weight:700">{pnl_m:+,.0f}円</span></span>'
                f'</div>'
                f'<div class="mg-body" id="mgb_{pfx}{dseq}_{ym_key}" style="display:{body_disp}">'
                f'<div class="edate-grid">{btns_html}</div>'
                f'<div class="mg-detail" id="mgd_{pfx}{dseq}_{ym_key}">{dets_html}</div>'
                f'</div></div>\n'
            )
        return html

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
            _reg_lbl, _reg_col = _month_regime(ym)   # その月末の大局レジーム
            # その月に約定した取引の同時保有ピーク資金(=その月を回すのに必要な資金)
            _mc_cap, _mc_pd, _mc_pcnt, _mc_max, _mc_tot = _peak_capital(trades_m)
            _tenkan_m = sum(1 for t in done_m if t.get("strategy") == "転換")
            _tenkan_badge = (f'<br><span style="font-size:0.66rem;color:#60a5fa;font-weight:700">'
                             f'🔄転換{_tenkan_m}件</span>') if _tenkan_m > 0 else ""
            rows += (f'<tr>'
                     f'<td style="font-weight:700;color:#e2e8f0;white-space:nowrap">{ym[:4]}/{mm}{_tenkan_badge}</td>'
                     f'<td style="text-align:center;color:{_reg_col};font-weight:700;white-space:nowrap">{_reg_lbl}</td>'
                     f'<td style="text-align:right;color:#94a3b8">{len(done_m)}件</td>'
                     f'<td style="text-align:right;color:#94a3b8">{wr_m:.0f}%</td>'
                     f'<td style="text-align:right;color:#4ade80">+{gp_m:,.0f}円</td>'
                     f'<td style="text-align:right;color:#f87171">-{gl_m:,.0f}円</td>'
                     f'<td style="width:160px;position:relative;padding:4px 8px">'
                     f'<div style="position:absolute;top:4px;bottom:4px;left:{"50%" if pnl_m>=0 else f"calc(50% - {bar_w/2:.1f}%)"};'
                     f'width:{bar_w/2:.1f}%;background:{bar_col};border-radius:2px"></div>'
                     f'<span style="position:relative;font-weight:700;color:{pnl_col}">{pnl_m:+,.0f}円</span>'
                     f'</td>'
                     f'<td style="text-align:right;color:#38bdf8;font-weight:700;white-space:nowrap">'
                     f'{_mc_cap:,.0f}円'
                     f'<br><span style="font-size:0.7rem;color:#64748b">最大{_mc_max}銘柄同時</span></td>'
                     f'</tr>')
        return f"""<div style="margin-bottom:14px">
<table style="border-collapse:collapse;width:auto">
  <thead><tr>
    <th style="text-align:left;color:#94a3b8;font-size:0.78rem;padding:3px 8px">月</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px;text-align:center">大局<br>レジーム</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px">件数</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px">勝率</th>
    <th style="color:#4ade80;font-size:0.78rem;padding:3px 8px">利益</th>
    <th style="color:#f87171;font-size:0.78rem;padding:3px 8px">損失</th>
    <th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px;text-align:center">損益合計</th>
    <th style="color:#38bdf8;font-size:0.78rem;padding:3px 8px;text-align:right">必要資金<br><small style="color:#64748b">同時保有ピーク</small></th>
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

    # ⑦ 損切りパターン分析(BT70以上)はロング方向ロジック(当日安値/終値<stop)。
    # lss(ショート=stopは上側)には合わず誤解を招くので lss では出さず、下の
    # 『終値損切り比較』を出す。ロング/ショートでは方向が合うので従来どおり表示。
    _stop_pattern_html_str = "" if _LSS_ORDER_MODE else _stop_pattern_html(done_trades)

    # ── 🔻 lss 終値損切り比較（BT30以上）─────────────────────────────────────
    # lssの損切りは現行「5分足の高値がstopにタッチ」で発火(=一瞬の上ヒゲでも損切り)。
    # これを「5分足の終値がstop/targetを超えたバーで発火」に替えたら成績が良くなるか？
    # エントリーは同一・決済ルールだけ変えて apples-to-apples 比較する。重い(全trade×
    # 5分足)ので結果をディスクにキャッシュし、2回目以降は再計算しない
    # (環境変数 LSS_CLOSESTOP_RESWEEP=1 で強制再計算)。
    def _close_stop_compare_html(trades_list, nofills=None):
        # 調査の各部品をキー別に返す(呼び出し側が別々の調査タブに載せる):
        #   closestop / guard / liq / budgetsim / slotsim
        _EMPTY = {"closestop": "", "guard": "", "liq": "", "budgetsim": "", "slotsim": ""}
        if not _LSS_ORDER_MODE:
            return dict(_EMPTY)   # lss(逆指値空売り・同日決済)レポートのときだけ
        try:
            from backtest_limit_entry import (
                _load_5m_by_day as _l5, FEE_PCT_ONE_WAY as _fee5,
                _INTRADAY_5M_SLIP as _slip5, _INTRADAY_5M_CACHE as _m5c,
                _INTRADAY_5M_ENTRY_GAP_LIMIT as _gaplim5,
                fetch as _fetchd)
            from sameday5m_firsttouch import (short_exit_5m as _se, short_pnl as _sp,
                                              short_entry_fill_5m as _sef)
        except Exception:
            return dict(_EMPTY)

        # 銘柄の流動性(平均日次売買代金=出来高×終値)。銘柄あたり1回だけ日足から算出。
        _liq_cache: dict = {}
        def _liq_of(sym):
            if sym in _liq_cache:
                return _liq_cache[sym]
            v = None
            try:
                _dfd = _fetchd(sym, 200)
                if _dfd is not None and not _dfd.empty:
                    _c = "close" if "close" in _dfd.columns else "Close"
                    _vv = "volume" if "volume" in _dfd.columns else "Volume"
                    v = float((_dfd[_vv] * _dfd[_c]).tail(120).mean())
            except Exception:
                v = None
            _liq_cache[sym] = v
            return v
        _LIQ_BUCKETS = [("<3億", 0, 3e8), ("3-10億", 3e8, 1e9),
                        ("10-30億", 1e9, 3e9), ("30-100億", 3e9, 1e10),
                        ("100億+", 1e10, float("inf"))]
        def _liq_bucket(v):
            if v is None:
                return "不明"
            for lab, lo, hi in _LIQ_BUCKETS:
                if lo <= v < hi:
                    return lab
            return "不明"
        def _build_liq_html(_liq, _liqlabs):
            _lqvalid = [(l, _liq[l]) for l in _liqlabs if l in _liq and _liq[l]["n"] > 0]
            if not _lqvalid:
                return ""
            _tot = sum(v["n"] for _, v in _lqvalid) or 1
            _rows = ""
            for _l, _v in _lqvalid:
                _wr = _v["win"] / _v["n"] * 100 if _v["n"] else 0
                _avg = _v["pnl"] / _v["n"] if _v["n"] else 0
                _shr = _v["n"] / _tot * 100
                _pc = "#4ade80" if _v["pnl"] >= 0 else "#f87171"
                _ac = "#4ade80" if _avg >= 0 else "#f87171"
                _rows += (f'<tr>'
                          f'<td style="text-align:left;padding:3px 10px;font-weight:700">{_l}</td>'
                          f'<td style="text-align:right;padding:3px 10px;color:{_pc};font-weight:700">{_v["pnl"]:+,.0f}円</td>'
                          f'<td style="text-align:right;padding:3px 10px;color:{_ac}">{_avg:+,.0f}円</td>'
                          f'<td style="text-align:right;padding:3px 10px">{_v["n"]}件</td>'
                          f'<td style="text-align:right;padding:3px 10px;color:#94a3b8">{_shr:.0f}%</td>'
                          f'<td style="text-align:right;padding:3px 10px">{_wr:.0f}%</td>'
                          f'</tr>')
            return (
                '<h3 style="color:#cbd5e1;margin:22px 0 6px">🔻 流動性(売買代金)帯別の成績（BT30以上・現行約定）</h3>'
                '<p class="footnote">各銘柄の平均日次売買代金(出来高×終値)で帯分けし、lssの成績を比較。'
                '<b>1トレード平均損益</b>が帯によって大きく違えば「エッジが出来高で変わる」。均等なら'
                '出来高はエッジに効かない。件数%はどの流動性にトレードが偏っているか。'
                '（既存トレードのpnlから集計＝軽い。終値判定の再計算は不要）</p>'
                '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">'
                '<thead><tr>'
                '<th style="text-align:left;padding:3px 10px;color:#94a3b8;font-size:0.75rem">売買代金/日</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">総損益</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">平均損益/件</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">件数</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">件数%</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">勝率</th>'
                '</tr></thead><tbody>' + _rows + '</tbody></table></div>'
                '<p class="footnote" style="margin-top:6px">※ 平均損益/件が全帯で似ていれば出来高はエッジに'
                '効かない。特定帯だけ突出/劣後していれば、その流動性を優先/回避する余地あり。</p>')
        def _build_liq_threshold_html(pairs):
            # 「売買代金 X億円以上」に絞った場合の累計成績(BT30以上)。
            # 流動性の高い銘柄だけに投資した場合の総損益・平均損益/件・件数・勝率を見る。
            if len(pairs) < 5:
                return ""
            _ths = [("全部(≥0)", 0.0), ("≥3億", 3e8), ("≥5億", 5e8),
                    ("≥10億", 1e9), ("≥30億", 3e9), ("≥100億", 1e10)]
            _rows = ""
            for _lab, _th in _ths:
                _sel = [(v, p) for v, p in pairs if v >= _th]
                _n = len(_sel)
                if _n == 0:
                    continue
                _pnl = sum(p for _, p in _sel)
                _win = sum(1 for _, p in _sel if p > 0)
                _avg = _pnl / _n
                _wr = _win / _n * 100
                _pc = "#4ade80" if _pnl >= 0 else "#f87171"
                _ac = "#4ade80" if _avg >= 0 else "#f87171"
                _rows += (f'<tr>'
                          f'<td style="text-align:left;padding:3px 10px;font-weight:700">{_lab}</td>'
                          f'<td style="text-align:right;padding:3px 10px;color:{_pc};font-weight:700">{_pnl:+,.0f}円</td>'
                          f'<td style="text-align:right;padding:3px 10px;color:{_ac}">{_avg:+,.0f}円</td>'
                          f'<td style="text-align:right;padding:3px 10px">{_n}件</td>'
                          f'<td style="text-align:right;padding:3px 10px">{_wr:.0f}%</td>'
                          f'</tr>')
            if not _rows:
                return ""
            return (
                '<h4 style="color:#cbd5e1;margin:16px 0 6px">▸ 売買代金しきい値以上に絞った場合の成績（BT30以上）</h4>'
                '<p class="footnote">「BT30以上 かつ 売買代金がX億円以上」だけに投資した場合の累計。'
                '厚い銘柄に絞ると件数は減るが、1件あたりの利益や勝率が上がるか(＝流動性で絞る価値があるか)を見る。'
                '総損益は件数が減るので下がるのが普通。<b>平均損益/件</b>が上がるかに注目。</p>'
                '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">'
                '<thead><tr>'
                '<th style="text-align:left;padding:3px 10px;color:#94a3b8;font-size:0.75rem">売買代金</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">総損益</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">平均損益/件</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">件数</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">勝率</th>'
                '</tr></thead><tbody>' + _rows + '</tbody></table></div>')
        def _build_budget_sim_html(rows):
            # 予算固定シミュ: 毎日その日のBT降順で、予算(既定400万円)まで注文した場合の月次成績。
            #  ・必要資金=注文トリガー価格(終値ベース)×株数の累計が予算を超えたら打ち切り。
            #  ・不約定の注文(is_fill=False)も発注枠を消費するが、損益・件数には計上しない。
            #  ・同日決済なので予算は毎日リセット。
            if len(rows) < 20:
                return ""
            import os as _osb
            try:
                _bud = float(_osb.environ.get("LSS_BUDGET_MAN", "400")) * 1e4
            except Exception:
                _bud = 4e6
            _bud_man = int(_bud / 1e4)
            # 開始月フィルタ(環境変数 LSS_MONTH_FROM=YYYY-MM)。マージ提案で最新基準月より
            # 後だけ(=OOS)を見たいとき指定。例: 最新基準2026-03 → LSS_MONTH_FROM=2026-04。
            _mfrom = _osb.environ.get("LSS_MONTH_FROM", "").strip()
            _by_day: dict = {}
            for _d, _bt, _no, _p, _isf in rows:
                _by_day.setdefault(_d, []).append((_bt, _no, _p, _isf))
            _mon: dict = {}   # YYYY-MM -> {n,pnl,win,days,used_sum}
            _tot = {"n": 0, "pnl": 0.0, "win": 0}
            for _d, _lst in _by_day.items():
                _mk = _d[:7]   # YYYY-MM
                if _mfrom and _mk < _mfrom:
                    continue   # 開始月より前(In-sample側)はスキップ
                _cap = 0.0
                _picked = []
                for _bt, _no, _p, _isf in sorted(_lst, key=lambda x: -x[0]):
                    if _cap + _no > _bud:
                        continue   # 予算超過はスキップ(次に安い注文が入るか試す=貪欲)
                    _cap += _no                      # 約定・不約定とも発注枠を消費
                    if _isf:
                        _picked.append((_no, _p))    # 損益・件数は約定分のみ
                _m = _mon.setdefault(_mk, {"n": 0, "pnl": 0.0, "win": 0, "used": 0.0, "days": 0})
                _m["days"] += 1; _m["used"] += _cap
                for _no, _p in _picked:
                    _m["n"] += 1; _m["pnl"] += _p
                    if _p > 0: _m["win"] += 1
                    _tot["n"] += 1; _tot["pnl"] += _p
                    if _p > 0: _tot["win"] += 1
            _rows = ""
            for _mk in sorted(_mon.keys(), reverse=True):
                _m = _mon[_mk]
                if _m["n"] == 0:
                    continue
                _wr = _m["win"] / _m["n"] * 100
                _avgd = _m["used"] / _m["days"] if _m["days"] else 0
                _pc = "#4ade80" if _m["pnl"] >= 0 else "#f87171"
                _rows += (f'<tr>'
                          f'<td style="text-align:left;padding:3px 10px;font-weight:700">{_mk}</td>'
                          f'<td style="text-align:right;padding:3px 10px">{_m["n"]}件</td>'
                          f'<td style="text-align:right;padding:3px 10px">{_wr:.0f}%</td>'
                          f'<td style="text-align:right;padding:3px 10px;color:{_pc};font-weight:700">{_m["pnl"]:+,.0f}円</td>'
                          f'<td style="text-align:right;padding:3px 10px;color:#94a3b8">{_avgd/1e4:,.0f}万</td>'
                          f'</tr>')
            _twr = _tot["win"] / _tot["n"] * 100 if _tot["n"] else 0
            _tpc = "#4ade80" if _tot["pnl"] >= 0 else "#f87171"
            _rows += (f'<tr style="border-top:2px solid #475569">'
                      f'<td style="text-align:left;padding:4px 10px;font-weight:700">合計</td>'
                      f'<td style="text-align:right;padding:4px 10px;font-weight:700">{_tot["n"]}件</td>'
                      f'<td style="text-align:right;padding:4px 10px">{_twr:.0f}%</td>'
                      f'<td style="text-align:right;padding:4px 10px;color:{_tpc};font-weight:700">{_tot["pnl"]:+,.0f}円</td>'
                      f'<td style="text-align:right;padding:4px 10px;color:#64748b">—</td>'
                      f'</tr>')
            return (
                f'<h4 style="color:#cbd5e1;margin:18px 0 6px">💰 予算固定シミュ: 毎日BT降順で {_bud_man}万円まで注文した場合（月次）'
                + (f'<span style="color:#fbbf24">［BT{_BUD_MIN_BT}以上のみ］</span>' if _BUD_MIN_BT > 30 else '')
                + (f'<span style="color:#38bdf8">［{_mfrom}以降=OOS］</span>' if _mfrom else '')
                + '</h4>'
                f'<p class="footnote">毎日その日のBT降順で、必要資金(<b>注文トリガー価格＝前日終値ベース</b>×100株)の累計が'
                f'<b>{_bud_man}万円</b>に収まるだけ<b>注文</b>する（同日決済なので予算は毎日リセット）。'
                f'<b>不約定(トリガー未達・ギャップ過大)の注文も発注枠を消費</b>する（＝その下のBTの約定を締め出す）。'
                f'損益・件数は約定分のみ計上。この価格帯タブ(表示中の価格上限)の銘柄のみ。'
                f'BT下限={_BUD_MIN_BT}(環境変数 LSS_BUDGET_MIN_BT, 既定30)。'
                f'予算は環境変数 LSS_BUDGET_MAN(万, 既定400)で変更可。</p>'
                '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">'
                '<thead><tr>'
                '<th style="text-align:left;padding:3px 10px;color:#94a3b8;font-size:0.75rem">月</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">取引数</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">勝率</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">損益</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">平均使用額/日</th>'
                '</tr></thead><tbody>' + _rows + '</tbody></table></div>'
                '<p class="footnote" style="margin-top:6px">※ これが「400万円で毎日上から買う」実運用に最も近い月次成績。'
                '全月プラスなら予算内運用でも安定。平均使用額/日が予算に届いていなければ、その月は候補が少なく予算を使い切れていない。</p>')
        def _build_slot_sim_html(sim_rows):
            # 資金制約=「1日N銘柄まで」を想定した方式対決:
            #   方式A: その日のBT降順で上位N
            #   方式B: 流動性≥しきい値 の中からBT降順で上位N
            # 期間全体で実現総損益・勝率を比較。どちらが資金効率が良いかを直接判定。
            if len(sim_rows) < 20:
                return ""
            import os as _oss
            try:
                _thr = float(_oss.environ.get("LSS_SLOTSIM_LIQ_OKU", "10")) * 1e8
            except Exception:
                _thr = 1e9
            _thr_oku = int(_thr / 1e8)
            _by_day: dict = {}
            for _d, _bt, _lv, _p in sim_rows:
                _by_day.setdefault(_d, []).append((_bt, _lv, _p))
            _Ns = [3, 5, 10, 20, 30, 50]
            _srows = ""
            for _N in _Ns:
                _a_pnl = _a_n = _a_w = 0
                _b_pnl = _b_n = _b_w = 0
                for _d, _lst in _by_day.items():
                    _a = sorted(_lst, key=lambda x: -x[0])[:_N]
                    _b = sorted([x for x in _lst if x[1] >= _thr], key=lambda x: -x[0])[:_N]
                    for _bt, _lv, _p in _a:
                        _a_pnl += _p; _a_n += 1
                        if _p > 0: _a_w += 1
                    for _bt, _lv, _p in _b:
                        _b_pnl += _p; _b_n += 1
                        if _p > 0: _b_w += 1
                if _a_n == 0:
                    continue
                _awr = _a_w / _a_n * 100 if _a_n else 0
                _bwr = _b_w / _b_n * 100 if _b_n else 0
                _diff = _b_pnl - _a_pnl
                _dc = "#4ade80" if _diff > 0 else ("#f87171" if _diff < 0 else "#94a3b8")
                _apnlc = "#4ade80" if _a_pnl >= 0 else "#f87171"
                _bpnlc = "#4ade80" if _b_pnl >= 0 else "#f87171"
                _srows += (f'<tr>'
                           f'<td style="text-align:left;padding:3px 10px;font-weight:700">{_N}銘柄/日</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:{_apnlc};font-weight:700">{_a_pnl:+,.0f}円</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:#94a3b8">{_awr:.0f}%</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:{_bpnlc};font-weight:700">{_b_pnl:+,.0f}円</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:#94a3b8">{_bwr:.0f}%({_b_n}件)</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:{_dc};font-weight:700">{_diff:+,.0f}円</td>'
                           f'</tr>')
            if not _srows:
                return ""
            return (
                '<h4 style="color:#cbd5e1;margin:18px 0 6px">🆚 資金制約シミュ: BTのみ vs 流動性フィルタ（1日N銘柄まで）</h4>'
                f'<p class="footnote">「1日にN銘柄しか張れない」資金制約を想定し、各日で'
                f'<b>方式A=BT降順で上位N</b> と <b>方式B=売買代金≥{_thr_oku}億の中からBT降順で上位N</b> '
                f'を選び、期間全体の実現損益を比較。差分(B−A)がプラスなら「その枠数では流動性で絞る方が得」、'
                f'マイナスなら「BT降順のまま（絞らない方）が得」。しきい値は環境変数 LSS_SLOTSIM_LIQ_OKU(既定10億)。</p>'
                '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">'
                '<thead><tr>'
                '<th style="text-align:left;padding:3px 10px;color:#94a3b8;font-size:0.75rem">枠数</th>'
                '<th style="text-align:right;padding:3px 10px;color:#f59e0b;font-size:0.75rem">方式A(BTのみ)</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">A勝率</th>'
                f'<th style="text-align:right;padding:3px 10px;color:#38bdf8;font-size:0.75rem">方式B(≥{_thr_oku}億)</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">B勝率(件数)</th>'
                '<th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">差分(B−A)</th>'
                '</tr></thead><tbody>' + _srows + '</tbody></table></div>'
                '<p class="footnote" style="margin-top:6px">※ 差分がほぼ全枠でマイナス→BT降順が正解(流動性で絞らない)。'
                'プラスの枠がある→その枠数なら流動性フィルタの価値あり。</p>')
        import os as _os2, pickle as _pk2, hashlib as _hl2
        from pathlib import Path as _P2

        # 対象: BT30以上 / 決済済み / 5分足OCOの基準値がある lss トレード
        tgt = []
        for t in trades_list:
            if t.get("reason") in ("発注中", "保有中", None):
                continue
            if _eff_long_bt(t) < 30:
                continue
            lp  = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            # 約定日は正規化済み date の entry_d_raw を使う(entry_dt は "%m/%d" 文字列で
            # 年が無く、5分足の日付キー[date]と一致しない)。lss同日なので exit_d_raw で代替可。
            edt = t.get("entry_d_raw") or t.get("exit_d_raw")
            fd  = edt.date() if hasattr(edt, "date") else edt
            if lp <= 0 or osp <= 0 or otp <= 0 or fd is None:
                continue
            tgt.append((str(t.get("symbol", "")), str(t.get("name", "")),
                        str(t.get("strategy", "")), fd, lp, osp, otp,
                        int(t.get("qty", 0) or 0)))
        if len(tgt) < 5:
            return {"closestop": ('<p class="footnote">対象トレードが5件未満のため表示なし。</p>'),
                    "guard": "", "liq": "", "budgetsim": "", "slotsim": ""}

        # ── 流動性(売買代金)帯別: 既存トレードのpnlから軽く集計(重い5分足ループ不要・常時表示) ──
        _liqlabs = [l for l, _, _ in _LIQ_BUCKETS] + ["不明"]
        _liq = {l: {"n": 0, "pnl": 0.0, "win": 0} for l in _liqlabs}
        _liq_pairs = []   # (売買代金, pnl) — しきい値以上の累計成績用
        _sim_rows = []    # (entry_date, bt, 売買代金, pnl) — 枠数固定の方式対決用
        # (entry_date, bt, 必要資金(注文トリガー価格×株数), pnl, is_fill) — 予算固定シミュ用。
        # 必要資金は「終値で判断」= 注文トリガー価格(order_limit=前日終値ベース)で集計。
        _bud_rows = []
        _fill_sym_day_m = set()
        for _t in trades_list:
            if _t.get("reason") in ("発注中", "保有中", None):
                continue
            if _eff_long_bt(_t) < 30:
                continue
            _p = float(_t.get("pnl", 0) or 0)
            _lv = _liq_of(str(_t.get("symbol", "")))
            _b = _liq[_liq_bucket(_lv)]
            _b["n"] += 1; _b["pnl"] += _p
            if _p > 0: _b["win"] += 1
            _dk = str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or "")
            _bt_v = float(_eff_long_bt(_t))
            # 必要資金 = 注文トリガー価格(終値ベース) × 株数。約定値ではない。
            _notional = (float(_t.get("order_limit", 0) or 0) or float(_t.get("entry_p", 0) or 0)) \
                * float(_t.get("qty", 0) or 0)
            if _notional > 0:
                _bud_rows.append((_dk, _bt_v, _notional, _p, True))
                _fill_sym_day_m.add((_t.get("symbol"), _dk))
            if _lv is not None:
                _liq_pairs.append((_lv, _p))
                _sim_rows.append((_dk, _bt_v, _lv, _p))
        # 不約定(発注枠は消費するが損益0)を月次シミュにも投入(1銘柄1注文/日で重複除外)。
        for _t in (nofills or []):
            if _eff_long_bt(_t) < 30:
                continue
            _dk = str(_t.get("entry_d_raw") or _t.get("exit_d_raw") or "")
            if (_t.get("symbol"), _dk) in _fill_sym_day_m:
                continue
            _notional = (float(_t.get("order_limit", 0) or 0) or float(_t.get("entry_p", 0) or 0)) \
                * float(_t.get("qty", 0) or 0)
            if _notional > 0:
                _bud_rows.append((_dk, float(_eff_long_bt(_t)), _notional, 0.0, False))
        # 調査ごとに部品を分けて保持(それぞれ別の調査タブに載せる)。
        _liq_html = _build_liq_html(_liq, _liqlabs) + _build_liq_threshold_html(_liq_pairs)
        _budgetsim_html = _build_budget_sim_html(_bud_rows)
        _slotsim_html = _build_slot_sim_html(_sim_rows)

        _sig2 = _hl2.md5(repr(sorted([x[:1] + (str(x[3]),) + x[4:] for x in tgt])).encode()).hexdigest()[:16]
        _cdir2 = _P2(__file__).resolve().parent / ".lss_closestop_cache"
        _cf2 = _cdir2 / f"cmp_{_sig2}.pkl"
        # LSS_GUARD_ONLY=1: 指値ガードスイープだけ計算(終値損切り比較dstop/dtgtをスキップ=速い)。
        # 計算は強制するがキャッシュは書かない(既存のフル cache を汚さない・毎回ガードだけ再計算)。
        _guard_only = _os2.getenv("LSS_GUARD_ONLY") == "1"
        _resweep2 = _os2.getenv("LSS_CLOSESTOP_RESWEEP") == "1" or _guard_only
        agg = None
        if _cf2.exists() and not _resweep2:
            try:
                agg = _pk2.loads(_cf2.read_bytes())
            except Exception:
                agg = None

        # 既定では計算しない(.\daily を止めない/BTがディスク復元だと5分足が未ロードで重い)。
        # 有効キャッシュがあれば表示、無ければ「未計算」案内のみ。計算は明示 RESWEEP=1 のとき。
        if (agg is None or agg.get("_v") != 7) and not _resweep2:
            # 終値判定/ガードは未計算(重い)。流動性/資金シミュ(軽い)は各タブに常時表示。
            _note = ('<h3 style="color:#cbd5e1;margin:8px 0 6px">🔻 lss 終値損切り比較（BT30以上）</h3>'
                     '<p class="footnote">未計算です（重いので既定ではスキップ）。'
                     '<b>一度だけ</b> <code>$env:LSS_CLOSESTOP_RESWEEP=1; .\\daily; $env:LSS_CLOSESTOP_RESWEEP=&quot;&quot;</code> '
                     'で計算するとキャッシュされ、以降の <code>.\\daily</code> で自動表示されます。</p>')
            return {"closestop": _note, "guard": "", "liq": _liq_html,
                    "budgetsim": _budgetsim_html, "slotsim": _slotsim_html}

        if agg is None or agg.get("_v") != 7:
            # 計算時は5分足を必要に応じてロードする(BTがディスク復元だとメモリに無いため)。
            # 銘柄あたり1回だけロード(_l5=プロセスキャッシュ)。進捗を出して無反応を防ぐ。
            _swlab = "指値ガードスイープ(終値比較はスキップ)" if _guard_only else "終値損切り比較"
            print(f"  [{_swlab}] {len(tgt)}件を再判定中(5分足ロード中)...", flush=True)
            _by_sym5: dict = {}
            def _si():
                return {"n": 0, "pnl": 0.0, "win": 0, "stop": 0, "tgt": 0, "close": 0}
            def _acc(st, pnl, rsn):
                st["n"] += 1; st["pnl"] += pnl
                if pnl > 0: st["win"] += 1
                if rsn == "stop": st["stop"] += 1
                elif rsn == "target": st["tgt"] += 1
                else: st["close"] += 1
            # 3モード(すべて日足終値=同日lssでは引け決済に委ねる):
            #   touch  = 現行(損切り・利確ともタッチ)
            #   dstop  = 損切りを日足終値(日中は損切りせず引けまで持つ / 利確はタッチ)
            #   dtgt   = 利確を日足終値(日中は利確せず引けまで持つ / 損切りはタッチ)
            touch, dstop, dtgt = _si(), _si(), _si()
            # 指値ガード%スイープ(約定モデルの検証): 決済はtouch固定、entryだけ可変。
            # 3%以上は非拘束(-3%超ギャップが実質無い)と判明→サブ1%を細かく探索する。
            _GUARDS = [("0.25%", 0.0025), ("0.5%", 0.005), ("0.75%", 0.0075),
                       ("1%", 0.01), ("1.5%", 0.015), ("2%", 0.02), ("3%", 0.03),
                       ("5%", 0.05), ("無制限", None)]
            guards = {lab: {"n": 0, "pnl": 0.0, "win": 0, "skip": 0} for lab, _ in _GUARDS}
            diffs = []   # 利確を日足終値 vs 現行 で判定が変わったトレード
            _miss = 0; _tot5 = len(tgt)
            for _i5, (sym, name, strat, fd, lp, osp, otp, qty) in enumerate(tgt, 1):
                if _i5 % 2000 == 0:
                    print(f"    [{_swlab}] {_i5}/{_tot5} 判定済み...", flush=True)
                # メモリにあれば即利用、無ければその場でロード(銘柄あたり1回)。
                _sd = _m5c.get(sym)
                if _sd is None:
                    _sd = _by_sym5.get(sym)
                    if _sd is None:
                        try: _sd = _l5(sym) or {}
                        except Exception: _sd = {}
                        _by_sym5[sym] = _sd
                db = _sd.get(fd) if _sd else None
                if db is None or len(db) < 2:
                    _miss += 1
                    continue
                stop_p = max(osp, otp); tp = min(osp, otp)
                q = qty or 100
                # touch決済(損切り・利確ともタッチ)を先に1回。ガードスイープ・決済比較の共通ベース。
                _xpt, _rsnt, _, _ = _se(db, lp, stop_p, tp, False)
                if _rsnt in ("no_5m", "no_entry"):
                    _miss += 1; continue
                # ── ガード%スイープ(決済touch固定・entryだけ可変。全約定トレードで評価) ──
                for _glab, _g in _GUARDS:
                    _ef = _sef(db, lp, False, entry_gap_limit=_g)
                    _gs = guards[_glab]
                    if _ef is None:
                        _gs["skip"] += 1; continue   # そのガードでは約定不可
                    _pg = _sp(_ef, _xpt, _rsnt, q, _fee5, _slip5)
                    _gs["n"] += 1; _gs["pnl"] += _pg
                    if _pg > 0: _gs["win"] += 1
                # ── 決済モード比較(既定=3%約定モデル) ── guard_only 時はスキップして高速化
                if _guard_only:
                    continue
                _efill = _sef(db, lp, False, entry_gap_limit=_gaplim5)
                if _efill is None:
                    continue   # 既定モデルで約定しない → 決済比較からは除外(ガードには計上済み)
                res = {"touch": (_sp(_efill, _xpt, _rsnt, q, _fee5, _slip5), _rsnt)}
                _bad = False
                for key, notgt, nostop in (("dstop", False, True), ("dtgt", True, False)):
                    xp, rsn, _, _ = _se(db, lp, stop_p, tp, False,
                                        no_target=notgt, no_stop=nostop)
                    if rsn in ("no_5m", "no_entry"):
                        _bad = True; break
                    res[key] = (_sp(_efill, xp, rsn, q, _fee5, _slip5), rsn)
                if _bad:
                    continue
                _acc(touch, res["touch"][0], res["touch"][1])
                _acc(dstop, res["dstop"][0], res["dstop"][1])
                _acc(dtgt,  res["dtgt"][0],  res["dtgt"][1])
                pt, rt = res["touch"]; ps, rs = res["dtgt"]
                if rt != rs or abs(pt - ps) > 1:
                    diffs.append((sym, name, strat, str(fd), pt, rt, ps, rs))
            agg = {"_v": 7, "touch": touch, "dstop": dstop, "dtgt": dtgt,
                   "guards": guards, "_guard_only": _guard_only,
                   "ndiff": len(diffs),
                   "diffs": sorted(diffs, key=lambda d: d[6] - d[4], reverse=True)}
            if not _guard_only:   # guard_only はキャッシュを書かない(フルcacheを汚さない)
                try:
                    _cdir2.mkdir(exist_ok=True)
                    _cf2.write_bytes(_pk2.dumps(agg))
                except Exception:
                    pass

        tc = agg["touch"]; ds = agg["dstop"]; dt = agg["dtgt"]
        # guard_only は touch/dstop/dtgt を空にしている(意図的)。この早期リターンは
        # 終値比較モードのみ適用し、guard_only ではガードHTMLの描画へ進める。
        if tc["n"] == 0 and not agg.get("_guard_only"):
            return {"closestop": ('<p class="footnote">5分足が揃うトレードが無く比較不可'
                                  '（対象銘柄の5分足pklが stock_5min に無い等）。</p>'),
                    "guard": "", "liq": _liq_html,
                    "budgetsim": _budgetsim_html, "slotsim": _slotsim_html}

        def _wr(st): return st["win"] / st["n"] * 100 if st["n"] else 0.0

        def _card(title, st, col, base=None):
            d_html = ""
            if base is not None:
                dp = st["pnl"] - base["pnl"]
                dcol = "#4ade80" if dp > 0 else ("#f87171" if dp < 0 else "#94a3b8")
                d_html = (f'<div style="color:{dcol};font-size:0.82rem;font-weight:700;'
                          f'margin-top:4px">対現行 {dp:+,.0f}円</div>')
            return (f'<div style="background:#1e293b;padding:12px 18px;border-radius:8px;'
                    f'min-width:200px;{"border:1px solid "+col if base is not None else ""}">'
                    f'<div style="color:{col};font-weight:700;margin-bottom:6px">{title}</div>'
                    f'<div style="font-size:1.25rem;font-weight:700;color:'
                    f'{"#4ade80" if st["pnl"]>=0 else "#f87171"}">{st["pnl"]:+,.0f}円</div>'
                    f'{d_html}'
                    f'<div style="color:#94a3b8;font-size:0.78rem;margin-top:4px">'
                    f'{st["n"]}取引 / 勝率 {_wr(st):.0f}%</div>'
                    f'<div style="color:#64748b;font-size:0.72rem;margin-top:2px">'
                    f'損切{st["stop"]} / 利確{st["tgt"]} / 引け{st["close"]}</div></div>')

        # 最良モードを判定
        _cands = [("損切りを日足終値", ds), ("利確を日足終値", dt), ("現行タッチ", tc)]
        _best = max(_cands, key=lambda x: x[1]["pnl"])
        verdict = (f'✅ 最良は <b>{_best[0]}</b>（{_best[1]["pnl"]:+,.0f}円 / 現行比 '
                   f'{_best[1]["pnl"]-tc["pnl"]:+,.0f}円）' if _best[1] is not tc
                   else '→ 現行(両方タッチ)が最良。日足終値に委ねる利点なし')

        # ── 指値ガード%スイープ(約定モデルの検証)の描画 ──
        _guards = agg.get("guards") or {}
        _glabels = ["0.25%", "0.5%", "0.75%", "1%", "1.5%", "2%", "3%", "5%", "無制限"]
        _gvalid = [(l, _guards[l]) for l in _glabels if l in _guards and _guards[l]["n"] > 0]
        _guard_html = ""
        if _gvalid:
            _gbest = max(_gvalid, key=lambda x: x[1]["pnl"])
            _grows = ""
            for _l, _g in _gvalid:
                _gwr = _g["win"] / _g["n"] * 100 if _g["n"] else 0
                _is_cur = (_l == "3%")
                _is_bst = (_l == _gbest[0])
                _mark = (" ★最良" if _is_bst else "") + ("（現行）" if _is_cur else "")
                _pc = "#4ade80" if _g["pnl"] >= 0 else "#f87171"
                _bg = "background:#0e3320;" if _is_bst else ("background:#101826;" if _is_cur else "")
                _avg = _g["pnl"] / _g["n"] if _g["n"] else 0
                _grows += (f'<tr style="{_bg}">'
                           f'<td style="text-align:left;padding:3px 10px;font-weight:700">{_l}{_mark}</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:{_pc};font-weight:700">{_g["pnl"]:+,.0f}円</td>'
                           f'<td style="text-align:right;padding:3px 10px">{_g["n"]}件</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:#94a3b8">{_avg:+,.0f}円</td>'
                           f'<td style="text-align:right;padding:3px 10px">{_gwr:.0f}%</td>'
                           f'<td style="text-align:right;padding:3px 10px;color:#94a3b8">{_g["skip"]}件</td>'
                           f'</tr>')
            _guard_html = f"""
<h3 style="color:#cbd5e1;margin:22px 0 6px">🔻 指値ガード% スイープ（約定モデルの検証・BT30以上）</h3>
<p class="footnote">逆指値売りが発火した後の<b>指値下限（−X%）</b>を変えて比較。決済はtouch固定、
約定価格＝min(トリガー,始値)で下限X%未満のギャップダウンは約定不可(=不成立)。
ガードが緩い(5%/無制限)ほど深いギャップも約定するが不利な安値で売る。厳しい(2%)ほど
不成立が増える。<b>総損益が最大のガードが最適</b>。現行は3%。</p>
<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.85rem">
  <thead><tr>
    <th style="text-align:left;padding:3px 10px;color:#94a3b8;font-size:0.75rem">指値ガード</th>
    <th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">総損益</th>
    <th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">約定数</th>
    <th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">1件あたり</th>
    <th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">勝率</th>
    <th style="text-align:right;padding:3px 10px;color:#94a3b8;font-size:0.75rem">不成立</th>
  </tr></thead>
  <tbody>{_grows}</tbody>
</table></div>
<p style="margin:6px 0 14px;font-size:0.9rem;color:#cbd5e1">
  ✅ 総損益が最大のガードは <b>{_gbest[0]}</b>（{_gbest[1]["pnl"]:+,.0f}円）。
  {'現行3%が最良。変更不要。' if _gbest[0]=='3%' else '現行3%と比べて要検討（差が小さければ誤差）。'}</p>"""

        # 流動性帯別(_liq_html)は上で既に構築済み(既存pnlから軽く集計)。

        drows = ""
        _ja = {"stop": "損切り", "target": "利確", "close": "引け"}
        for sym, name, strat, fd, pt, rt, ps, rs in agg["diffs"][:20]:
            dd = ps - pt
            dc = "#4ade80" if dd > 0 else ("#f87171" if dd < 0 else "#94a3b8")
            drows += (f'<tr>'
                      f'<td style="text-align:left;padding:3px 8px;color:#94a3b8">{fd}</td>'
                      f'<td style="text-align:left;padding:3px 8px">{sym.split(".")[0]} '
                      f'<span style="color:#64748b;font-size:0.72rem">{name[:6]}</span></td>'
                      f'<td style="text-align:center;padding:3px 8px;color:#94a3b8">{strat}</td>'
                      f'<td style="text-align:right;padding:3px 8px">{pt:+,.0f}<br>'
                      f'<span style="font-size:0.68rem;color:#64748b">{_ja.get(rt,rt)}</span></td>'
                      f'<td style="text-align:right;padding:3px 8px">{ps:+,.0f}<br>'
                      f'<span style="font-size:0.68rem;color:#64748b">{_ja.get(rs,rs)}</span></td>'
                      f'<td style="text-align:right;padding:3px 8px;color:{dc};font-weight:700">{dd:+,.0f}</td>'
                      f'</tr>')

        _cache_note = "（キャッシュ済み・再計算は LSS_CLOSESTOP_RESWEEP=1）"
        _closestop_html = f"""<p class="footnote">同じエントリーで決済ルールだけ変更して比較（BT30以上のlssトレードのみ）{_cache_note}。
同日決済なので<b>日足終値で判定＝その決済を日中に置かず引けまで持つ（引け成行）</b>を意味する。<br>
・<b>損切りを日足終値</b>＝日中は損切りせず引けまで持つ（損失が引けまで走る）／利確はタッチのまま。<br>
・<b>利確を日足終値</b>＝日中は利確せず引けまで持つ／損切りはタッチのまま。</p>
<div style="display:flex;gap:14px;flex-wrap:wrap;margin:12px 0">
{_card("現行: 両方タッチ", tc, "#f59e0b")}
{_card("損切りを日足終値(引けまで持つ)", ds, "#f472b6", base=tc)}
{_card("利確を日足終値(引けまで持つ)", dt, "#38bdf8", base=tc)}
</div>
<p style="margin:6px 0 12px;font-size:0.9rem;color:#cbd5e1">{verdict}</p>
<p style="color:#94a3b8;font-size:0.78rem;margin:10px 0 4px">
  「利確を日足終値 vs 現行」で決済が変わったトレード（差分の大きい順・上位20件）</p>
<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.82rem">
  <thead><tr>
    <th style="text-align:left;padding:3px 8px;color:#94a3b8;font-size:0.75rem">約定日</th>
    <th style="text-align:left;padding:3px 8px;color:#94a3b8;font-size:0.75rem">銘柄</th>
    <th style="padding:3px 8px;color:#94a3b8;font-size:0.75rem">戦略</th>
    <th style="padding:3px 8px;color:#f59e0b;font-size:0.75rem">現行損益</th>
    <th style="padding:3px 8px;color:#38bdf8;font-size:0.75rem">利確を日足終値の損益</th>
    <th style="padding:3px 8px;color:#94a3b8;font-size:0.75rem">差分</th>
  </tr></thead>
  <tbody>{drows or '<tr><td colspan="6" style="text-align:center;color:#64748b;padding:12px">決済が変わったトレードなし</td></tr>'}</tbody>
</table></div>
<p class="footnote" style="margin-top:8px">※ 総損益で比較。現行より大きいモードがあれば、その決済ルールに
切替える価値あり。実運用に反映するには watcher/close_lss_guard を同じ判定に揃える(§16 と整合確認)。</p>"""
        if agg.get("_guard_only"):
            _closestop_html = ('<p class="footnote">🔻 終値損切り比較は <b>LSS_GUARD_ONLY=1</b>（ガードのみ計算）'
                               'のためスキップしました。⑦を見るには LSS_CLOSESTOP_RESWEEP=1 で再計算してください。</p>')
        return {"closestop": _closestop_html, "guard": _guard_html, "liq": _liq_html,
                "budgetsim": _budgetsim_html, "slotsim": _slotsim_html}

    _inv = _close_stop_compare_html(done_trades, nofills=all_nofills)
    if not isinstance(_inv, dict):
        _inv = {}
    _inv_closestop = _inv.get("closestop", "")
    _inv_guard     = _inv.get("guard", "")
    _inv_liq       = _inv.get("liq", "")
    _inv_budgetsim = _inv.get("budgetsim", "")
    _inv_slotsim   = _inv.get("slotsim", "")

    # ── ⑧ 保有中の2回目以降シグナル成績分析 ────────────────────────────────
    def _overlap_analysis_html(overlap_dropped, uid=0):
        """保有中に同一銘柄で発生した2回目以降のシグナルの成績分析(BTフィルタ付)。"""
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

        # 明細テーブル行ビルダー（BTフィルタ後の任意リストに対応）
        def _ov_detail_rows(lst):
            rows = ""
            for t in sorted(lst, key=lambda x: x.get("exit_d_raw") or _date2.min,
                            reverse=True)[:30]:
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
                rows += (
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
            return rows or ('<tr><td colspan="8" style="color:#475569;'
                            'padding:8px;text-align:center">該当トレードなし</td></tr>')

        # BTフィルタ: 全部 / BT60以上 / BT70以上 の3ブロック(KPI+明細)をJSトグル
        _ov_filters = [
            ("all",  "全部",     settled),
            ("bt60", "BT60以上", [t for t in settled if (t.get("rec_score") or 0) >= 60]),
            ("bt70", "BT70以上", bt70_settled),
        ]

        def _ov_kpi_and_detail(lst, key, active):
            wr, pf_s, pnl, w, l, gp, gl = _band_kpi(lst)
            n   = w + l
            wrc = "#4ade80" if wr >= 55 else ("#facc15" if wr >= 45 else "#f87171")
            pc  = "#4ade80" if pnl >= 0 else "#f87171"
            disp = "block" if active else "none"
            kpi = (
                '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">'
                f'<div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">'
                f'<div style="color:{wrc};font-size:1.3rem;font-weight:700">{wr:.1f}%</div>'
                f'<div style="color:#94a3b8;font-size:0.72rem">勝率 ({w}W/{l}L)</div></div>'
                f'<div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">'
                f'<div style="color:#e2e8f0;font-size:1.3rem;font-weight:700">{pf_s}</div>'
                f'<div style="color:#94a3b8;font-size:0.72rem">PF</div></div>'
                f'<div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">'
                f'<div style="color:#4ade80;font-size:1.3rem;font-weight:700">+{gp:,.0f}円</div>'
                f'<div style="color:#94a3b8;font-size:0.72rem">利益合計</div></div>'
                f'<div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">'
                f'<div style="color:#f87171;font-size:1.3rem;font-weight:700">-{gl:,.0f}円</div>'
                f'<div style="color:#94a3b8;font-size:0.72rem">損失合計</div></div>'
                f'<div style="background:#1e293b;padding:8px 16px;border-radius:6px;text-align:center">'
                f'<div style="color:{pc};font-size:1.3rem;font-weight:700">{pnl:+,.0f}円</div>'
                f'<div style="color:#94a3b8;font-size:0.72rem">合計損益 ({n}件)</div></div>'
                '</div>'
            )
            det = (
                '<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.82rem">'
                '<thead><tr>'
                '<th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">決済日</th>'
                '<th style="text-align:left;color:#94a3b8;font-size:0.75rem;padding:3px 8px">銘柄</th>'
                '<th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">戦略</th>'
                '<th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">BT</th>'
                '<th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">約定→決済</th>'
                '<th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">保有</th>'
                '<th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">理由</th>'
                '<th style="color:#94a3b8;font-size:0.75rem;padding:3px 8px">損益</th>'
                f'</tr></thead><tbody>{_ov_detail_rows(lst)}</tbody></table>'
                '<p class="footnote">明細は決済日降順・直近30件</p></div>'
            )
            return (f'<div id="ovblk_{uid}_{key}" style="display:{disp}">{kpi}{det}</div>')

        _ov_btns = "".join(
            f'<button class="ovbt-btn{" active" if k=="all" else ""}" '
            f'id="ovbtn_{uid}_{k}" onclick="switchOvBt({uid},\'{k}\')">'
            f'{lbl} <span style="font-size:0.72rem;color:#94a3b8">({len(lst)})</span></button>'
            for k, lbl, lst in _ov_filters
        )
        _ov_blocks = "".join(
            _ov_kpi_and_detail(lst, k, k == "all") for k, lbl, lst in _ov_filters
        )

        return f"""<h2 style="margin-top:24px">⑧ 重複保有シグナル成績（保有中の2回目以降 / {len(settled)}件）</h2>
<p class="footnote">1銘柄1ポジション制で弾かれた「既保有中の同銘柄シグナル」を仮発注した場合の成績。<br>
計測外だが参考として: 勝率・損益が1回目と同程度なら追加エントリー(同銘柄の複数保有)の根拠になる。</p>

<div class="detail-tab-nav" style="margin-bottom:12px">{_ov_btns}</div>
{_ov_blocks}

<div style="display:flex;gap:32px;flex-wrap:wrap;margin-top:20px">
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
</div>"""

    _overlap_html = ""  # _dseq 確定後に生成する（下記参照）

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

    # 取引明細タブのID順(ボタン描画順と一致させる)。
    _detail_tab_ids = ['all', 'bt60', 'entry']
    if _LSS_ORDER_MODE:
        _detail_tab_ids.append('budget')
        if _budget60_entry_sorted_short:
            _detail_tab_ids.append('budget60')
    if _LSS_ORDER_MODE and _tenkan_in_sorted:
        _detail_tab_ids.append('tenkan')
    _detail_tabs_js = "[" + ",".join(f"'{x}'" for x in _detail_tab_ids) + "]"

    # 予算固定(400万円/日・BT降順)タブ(lssのみ、ショートのみ)。ボタンとペインをここで組み立てる。
    _bt40liq_btn = ""
    _bt40liq_pane = ""
    if _LSS_ORDER_MODE:
        _bt40liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget\')" '
            f'style="border-color:#38bdf8">💰 {_budget_man}万円×BT降順×日別 (BT{_BT_TAB_MIN}以上) '
            f'<span style="font-size:0.72rem;color:#7dd3fc">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _bt40liq_pane = (
            f'<div id="detail_{_dseq}_budget" class="detail-tab-pane">'
            f'<p style="color:#7dd3fc;font-size:0.8rem;margin-bottom:10px">'
            f'💰 毎日その日のBT降順で、必要資金(<b>注文トリガー価格＝前日終値ベース</b>×100株)の累計が '
            f'<b>{_budget_man}万円</b> に収まるだけ<b>注文</b>した場合の「約定したトレード」を表示'
            f'（同日決済なので予算は毎日リセット）。<b>不約定の注文も発注枠を消費</b>する'
            f'（＝その下のBTの約定を締め出す）ので、実運用「予算内で上から注文」に最も近い。'
            f'<b>ショートのみ表示</b>（転換は転換タブ参照）。日付クリックで詳細'
            f'（直近{_ENTRY_GRID_DAYS}日）。予算は環境変数 LSS_BUDGET_MAN(万,既定400)で変更可。</p>'
            + _month_summary_html(_budget_entry_sorted_short)
            + _month_accordion_html(_budget_entry_by_date_short, _sorted_budget_entry_dates_short, _dseq, "q")
            + '</div>')

    # 転換トレード専用タブ(lssのみ)。ショートの400万円タブとは別に転換だけをまとめる。
    _tenkan_tab_btn = ""
    _tenkan_tab_pane = ""
    if _LSS_ORDER_MODE and _tenkan_in_sorted:
        _tenkan_entry_sorted = sorted(
            _tenkan_in_sorted,
            key=lambda x: x.get("entry_d_raw") or x["exit_d_raw"],
            reverse=True,
        )
        _tenkan_by_date, _sorted_tenkan_dates = _build_entry_grid(_tenkan_entry_sorted, "tk")
        _tenkan_tab_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'tenkan\')" '
            f'style="border-color:#60a5fa">🔄 転換 '
            f'<span style="font-size:0.72rem;color:#93c5fd">'
            f'({len(_tenkan_in_sorted)}件 直近{_ENTRY_GRID_DAYS}日)</span></button>'
        )
        _tenkan_tab_pane = (
            f'<div id="detail_{_dseq}_tenkan" class="detail-tab-pane">'
            f'<p style="color:#60a5fa;font-size:0.8rem;margin-bottom:10px">'
            f'🔄 <b>転換トレード</b>: lss未約定 → ロング転換（09:09以降最初バー買い / 11:30前最後バー売り）。'
            f' 全{len(_tenkan_in_sorted)}件。月別サマリー→日付クリックで詳細（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_tenkan_entry_sorted)
            + _month_accordion_html(_tenkan_by_date, _sorted_tenkan_dates, _dseq, "tk",
                                    expand_months=2, expand_tenkan=False)
            + '</div>'
        )

    # BT60以上のみ投資版(高品質集中)予算タブ。
    _bt70liq_btn = ""
    _bt70liq_pane = ""
    if _LSS_ORDER_MODE and _budget60_entry_sorted_short:
        _bt70liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget60\')" '
            f'style="border-color:#a78bfa">💰 {_budget_man}万円×BT降順×日別 (BT60以上) '
            f'<span style="font-size:0.72rem;color:#c4b5fd">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _bt70liq_pane = (
            f'<div id="detail_{_dseq}_budget60" class="detail-tab-pane">'
            f'<p style="color:#c4b5fd;font-size:0.8rem;margin-bottom:10px">'
            f'💰 <b>BTスコア60以上のみに絞った</b>予算シミュ。毎日BT降順で'
            f'<b>{_budget_man}万円</b>まで注文した場合の約定トレード（ショートのみ）。'
            f'日付クリックで詳細（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_budget60_entry_sorted_short)
            + _month_accordion_html(_budget60_entry_by_date_short, _sorted_budget60_entry_dates_short, _dseq, "q6")
            + '</div>')

    # BT50以上のみ投資版(高品質集中)タブ。
    _bt50liq_btn = ""
    _bt50liq_pane = ""
    if _LSS_ORDER_MODE and _budget50_entry_sorted:
        _bt50liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget50\')" '
            f'style="border-color:#22c55e">💰 {_budget_man}万円×BT降順×日別 (BT50以上) '
            f'<span style="font-size:0.72rem;color:#86efac">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _bt50liq_pane = (
            f'<div id="detail_{_dseq}_budget50" class="detail-tab-pane">'
            f'<p style="color:#86efac;font-size:0.8rem;margin-bottom:10px">'
            f'💰 上の予算シミュと同条件だが <b>BTスコア50以上のみに投資</b>（高品質集中版）。'
            f'毎日その日のBT降順で、必要資金(注文トリガー価格×100株)の累計が '
            f'<b>{_budget_man}万円</b> に収まるだけ注文（同日決済＝予算は毎日リセット）。'
            f'日付クリックで詳細（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_budget50_entry_sorted)
            + _month_accordion_html(_budget50_entry_by_date, _sorted_budget50_entry_dates, _dseq, "q5")
            + '</div>')

    # A7/RSI2/VOLTF戦略限定版予算シミュ(削除済み: 戦略絞りタブは非表示)。
    _narrow_liq_btn = ""
    _narrow_liq_pane = ""
    if False and _LSS_ORDER_MODE and _budget_narrow_entry_sorted:
        _narrow_liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget_narrow\')" '
            f'style="border-color:#f59e0b">💰 {_budget_man}万円×A7/RSI2/VOLTF限定 '
            f'<span style="font-size:0.72rem;color:#fcd34d">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _narrow_liq_pane = (
            f'<div id="detail_{_dseq}_budget_narrow" class="detail-tab-pane">'
            f'<p style="color:#fcd34d;font-size:0.8rem;margin-bottom:10px">'
            f'💰 <b>A7/RSI2/VOLTF戦略のみ</b>に絞った予算シミュ。毎日BT降順で'
            f'<b>{_budget_man}万円</b>まで注文した場合の約定トレード。'
            f'DON/MOMを除いた高効率戦略の実力を確認できます（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_budget_narrow_entry_sorted)
            + _month_accordion_html(_budget_narrow_entry_by_date, _sorted_budget_narrow_entry_dates, _dseq, "qn")
            + '</div>')

    # 約定額ベース版(削除済み: 約定400万円タブは非表示)。
    _fill_liq_btn = ""
    _fill_liq_pane = ""
    if False and _LSS_ORDER_MODE and _budget_fill_entry_sorted:
        _fill_liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget_fill\')" '
            f'style="border-color:#a78bfa">💳 約定{_budget_man}万円・全戦略 '
            f'<span style="font-size:0.72rem;color:#c4b5fd">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _fill_liq_pane = (
            f'<div id="detail_{_dseq}_budget_fill" class="detail-tab-pane">'
            f'<p style="color:#c4b5fd;font-size:0.8rem;margin-bottom:10px">'
            f'💳 <b>約定額ベース（watchで取り消し方式）</b>。BT降順に発注し、'
            f'累計<b>約定額</b>が<b>{_budget_man}万円</b>に達したら残り注文をキャンセル。'
            f'<b>不約定の注文は予算消費しない</b>ため、隣の発注額ベースより1日の約定件数が多くなる傾向。'
            f'左タブ(発注額ベース)との比較で不約定の影響を確認できます（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_budget_fill_entry_sorted)
            + _month_accordion_html(_budget_fill_entry_by_date, _sorted_budget_fill_entry_dates, _dseq, "qf")
            + '</div>')

    # 約定額ベース版(A7/RSI2/VOLTF限定・削除済み: 戦略絞りタブは非表示)。
    _fill_narrow_liq_btn = ""
    _fill_narrow_liq_pane = ""
    if False and _LSS_ORDER_MODE and _budget_fill_narrow_entry_sorted:
        _fill_narrow_liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget_fill_narrow\')" '
            f'style="border-color:#f97316">💳 約定{_budget_man}万円×A7/RSI2/VOLTF '
            f'<span style="font-size:0.72rem;color:#fdba74">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _fill_narrow_liq_pane = (
            f'<div id="detail_{_dseq}_budget_fill_narrow" class="detail-tab-pane">'
            f'<p style="color:#fdba74;font-size:0.8rem;margin-bottom:10px">'
            f'💳 <b>A7/RSI2/VOLTF限定 × 約定額ベース</b>。累計<b>約定額</b>が'
            f'<b>{_budget_man}万円</b>に達したら残り注文をキャンセル（watch方式）。'
            f'不約定は予算消費しないので、ギャップダウン不約定が多い日も予算を埋めやすい。'
            f'隣タブ(発注額ベース)との比較で不約定ロスを定量確認（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_budget_fill_narrow_entry_sorted)
            + _month_accordion_html(_budget_fill_narrow_entry_by_date, _sorted_budget_fill_narrow_entry_dates, _dseq, "qfn")
            + '</div>')

    # ループ充填版(削除済み: ループ充填タブは非表示)。
    _mlot_liq_btn = ""
    _mlot_liq_pane = ""
    if False and _LSS_ORDER_MODE and _budget_mlot_entry_sorted:
        _mlot_liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget_mlot\')" '
            f'style="border-color:#34d399">🔄 ループ充填・全戦略 '
            f'<span style="font-size:0.72rem;color:#6ee7b7">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _mlot_liq_pane = (
            f'<div id="detail_{_dseq}_budget_mlot" class="detail-tab-pane">'
            f'<p style="color:#6ee7b7;font-size:0.8rem;margin-bottom:10px">'
            f'🔄 <b>ループ充填モード（全戦略）</b>。BT降順に1銘柄100株ずつ1周目を配置し、'
            f'予算残があれば先頭から再度100株ずつ追加。<b>{_budget_man}万円</b>に'
            f'限りなく近づくまで繰り返します。1銘柄に複数ロット入るため、'
            f'表示の株数はn×100株・損益もn倍スケール。'
            f'発注額ベース・約定額ベースより確実に予算を埋められます（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_budget_mlot_entry_sorted)
            + _month_accordion_html(_budget_mlot_entry_by_date, _sorted_budget_mlot_entry_dates, _dseq, "qml")
            + '</div>')

    # ループ充填版(A7/RSI2/VOLTF限定・削除済み: 戦略絞りタブは非表示)。
    _mlot_narrow_liq_btn = ""
    _mlot_narrow_liq_pane = ""
    if False and _LSS_ORDER_MODE and _budget_mlot_narrow_entry_sorted:
        _mlot_narrow_liq_btn = (
            f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'budget_mlot_narrow\')" '
            f'style="border-color:#f43f5e">🔄 ループ充填×A7/RSI2/VOLTF '
            f'<span style="font-size:0.72rem;color:#fb7185">'
            f'(直近{_ENTRY_GRID_DAYS}日)</span></button>')
        _mlot_narrow_liq_pane = (
            f'<div id="detail_{_dseq}_budget_mlot_narrow" class="detail-tab-pane">'
            f'<p style="color:#fb7185;font-size:0.8rem;margin-bottom:10px">'
            f'🔄 <b>ループ充填モード（A7/RSI2/VOLTF限定）</b>。高効率3戦略に絞って'
            f'BT降順に100株ずつ繰り返し追加し、<b>{_budget_man}万円</b>に近づけます。'
            f'1銘柄複数ロット・株数n×100・損益n倍スケール（直近{_ENTRY_GRID_DAYS}日）。</p>'
            + _month_summary_html(_budget_mlot_narrow_entry_sorted)
            + _month_accordion_html(_budget_mlot_narrow_entry_by_date, _sorted_budget_mlot_narrow_entry_dates, _dseq, "qmln")
            + '</div>')

    # lss 調査タブ(⑦終値損切りに集約せず、調査ごとに分割)。ボタン・ペイン・tabs配列を用意。
    _inv_analysis_btns = ""
    _inv_analysis_panes = ""
    _inv_analysis_ids = []
    if _LSS_ORDER_MODE:
        _inv_analysis_btns = (
            f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'guard\')" style="border-color:#38bdf8">㉓ 指値ガード</button>\n'
            f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'liq\')" style="border-color:#38bdf8">㉔ 流動性</button>\n'
            f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'moneysim\')" style="border-color:#38bdf8">㉕ 資金シミュ</button>')
        _guard_pane_body = _inv_guard or '<p class="footnote">指値ガードの比較は未計算です。$env:LSS_CLOSESTOP_RESWEEP=1 で計算してください。</p>'
        _money_pane_body = (_inv_budgetsim or "") + (_inv_slotsim or "") or '<p class="footnote">資金シミュのデータがありません。</p>'
        _inv_analysis_panes = (
            f'<div id="analtab_{_dseq}_guard" class="analysis-tab-pane">{_guard_pane_body}</div>'
            f'<div id="analtab_{_dseq}_liq" class="analysis-tab-pane">{_inv_liq}</div>'
            f'<div id="analtab_{_dseq}_moneysim" class="analysis-tab-pane">{_money_pane_body}</div>')
        _inv_analysis_ids = ['guard', 'liq', 'moneysim']
    _analysis_tab_ids = ['summary', 'score', 'cross', 'strat_band', 'rollfwd', 'factors', 'bt6069',
                         'speed', 'extra'] + _inv_analysis_ids + \
        ['overlap', 'timing', 'preoos', 'maxhold', 'maxhold_cmp', 'pullback',
         'openconfirm', 'filltiming', 'stopwidth', 'opendir', 'drop', 'fadeshort',
         'holdcurve', 'emcmp', 'breadth', 'sameday', 'sameday5m']
    _analysis_tabs_js = "[" + ",".join(f"'{x}'" for x in _analysis_tab_ids) + "]"

    # 重複保有分析は _dseq 確定後に生成（BTフィルタのDOM id一意化のため）
    _overlap_html = _overlap_analysis_html(_overlap_dropped, _dseq)

    _preoos_tab_btn = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'preoos\')">⑩ シグナル時点BTスコア</button>'
    _maxhold_tab_btn = (
        f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'maxhold\')">⑪ 保有日数比較</button>\n'
        f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'maxhold_cmp\')">⑫ con/agg比較</button>'
    )
    _pullback_tab_btn = (
        f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'pullback\')">⑬ 押し目買い比較</button>'
    )
    _openconfirm_tab_btn = (
        f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'openconfirm\')">⑭ 寄り確認</button>'
    )
    _filltiming_tab_btn = (
        f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'filltiming\')">⑮ 約定タイミング</button>'
    )
    _stopwidth_tab_btn = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'stopwidth\')">⑯ 損切り幅</button>'
    _opendir_tab_btn   = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'opendir\')">⑰ 寄り付き方向</button>'
    _drop_tab_btn      = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'drop\')">⑱ 下落深さ</button>'
    _fadeshort_tab_btn = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'fadeshort\')">⑲ 逆張りショート</button>'
    _holdcurve_tab_btn = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'holdcurve\')">⑳ 保有日数カーブ</button>'
    _emcmp_tab_btn     = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'emcmp\')">㉑ em比較</button>'
    _breadth_tab_btn   = f'  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},\'breadth\')">㉒ シグナル数別</button>'
    _sameday_tab_btn   = (
        f'  <button class="analysis-tab-btn" style="border-color:#6d28d9" onclick="switchAnalysisTab({_dseq},\'sameday\')">🎯 同日TP/SL最適化</button>'
        if _SAMEDAY_SWEEP_TAB else "")
    _sameday5m_tab_btn = (
        f'  <button class="analysis-tab-btn" style="border-color:#0369a1" onclick="switchAnalysisTab({_dseq},\'sameday5m\')">🕔 5分足TP/SL</button>'
        if _SAMEDAY_5M_TAB else "")

    # 決済日別ボタン: 常時表示（絞り版タブ削除済みのため条件撤廃）
    _exit_tab_btn = (
        f'<button class="detail-tab-btn" onclick="switchDetailTab({_dseq},\'exit\')">'
        f'決済日別（目標/損切/TC） '
        f'<span style="font-size:0.72rem;color:#94a3b8">(直近{_ENTRY_GRID_DAYS}日)</span></button>')

    # 決済日別ペイン: 常時表示
    _exit_pane_or_narrow = (
        f'<div id="detail_{_dseq}_exit" class="detail-tab-pane">'
        f'<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:10px">'
        f'決済（決着）した日ごとの集計。各日を <b>目標達成 / 損切り / タイムカット</b>'
        f' 別に分けて表示（決済日をクリックで明細・直近{_ENTRY_GRID_DAYS}日）</p>'
        + _month_accordion_exit_html(*_build_exit_grid(entry_sorted_trades), _dseq, "x")
        + '</div>')

    return f"""
<h2>直近{days}日 取引損益 <span style="font-size:0.8rem;color:#64748b;font-weight:400">（{since} 〜 {until}）</span></h2>
{kpi_html}

<button class="analysis-toggle" onclick="toggleAnalysis({_dseq})" id="analysis_btn_{_dseq}">▶ 詳細分析（スクリプト別・スコア別・銘柄別）を表示</button>
<div id="analysis_{_dseq}" class="analysis-block" style="display:none">

<div class="analysis-tab-nav">
  <button class="analysis-tab-btn active" onclick="switchAnalysisTab({_dseq},'summary')">スクリプト別</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'score')">① ② スコア別実績</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'cross')">③ ④ BT×WF・高BT銘柄</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'strat_band')">★ BT帯×戦略</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'rollfwd')">★ ロールフォワードOOS</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'factors')">★ 効く要素</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'bt6069')">⑤ BT60-69</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'speed')">⑥ 速度分析</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'extra')">⑦ 損切り{'(終値比較)' if _LSS_ORDER_MODE else ''}</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'overlap')">⑧ 重複保有</button>
  <button class="analysis-tab-btn" onclick="switchAnalysisTab({_dseq},'timing')">⑨ 翌日のみ比較</button>
{_preoos_tab_btn}
{_maxhold_tab_btn}
{_pullback_tab_btn}
{_openconfirm_tab_btn}
{_filltiming_tab_btn}
{_stopwidth_tab_btn}
{_opendir_tab_btn}
{_drop_tab_btn}
{_fadeshort_tab_btn}
{_holdcurve_tab_btn}
{_emcmp_tab_btn}
{_breadth_tab_btn}
{_inv_analysis_btns}
{_sameday_tab_btn}
{_sameday5m_tab_btn}
</div>

<div id="analtab_{_dseq}_summary" class="analysis-tab-pane active">
<h2>戦略別サマリー</h2>
{strat_summary_html}
<h2 style="margin-top:20px">設定別サマリー（ホールドアウト×con/agg）</h2>
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
{oos_impact_html}
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
    <th>純OOS損益<br><small style="color:#94a3b8;font-weight:400">holdout除外窓のみ</small></th>
  </tr></thead>
  <tbody>{sym_rows}</tbody>
</table>
<p class="footnote" style="margin-top:6px">
  <b>純OOS損益</b> = その銘柄を選定に使っていない期間（各holdout設定の直近除外窓）だけのトレード損益。WFと違い選定バイアスが無い唯一の本物のOOS。<br>
  「損益合計プラス・純OOSもプラス」= 本物 / 「損益合計プラスだが純OOSマイナス」= 直近で崩れている。窓(d)が長いほど信頼度が高い。
</p>
</div>

<div id="analtab_{_dseq}_strat_band" class="analysis-tab-pane">
<h2>BTスコア帯×戦略 クロス分析</h2>
<p style="font-size:0.8rem;color:#64748b;margin-bottom:12px">
  同BT帯内での戦略間比較。戦略効果がBT分布と独立しているかを検証（BT帯を揃えてもPFに差があれば戦略選別に意味がある）。
</p>
<table>
  <thead><tr>
    <th style="text-align:left">BT帯</th>
    <th style="text-align:left">戦略</th>
    <th>件数</th><th>勝率</th><th>PF</th>
    <th style="color:#4ade80;text-align:right">利益</th>
    <th style="color:#f87171;text-align:right">損失</th>
    <th style="text-align:right">損益合計</th>
  </tr></thead>
  <tbody>{"<tr><td colspan='8' style='text-align:center;color:#64748b;padding:12px'>取引データなし</td></tr>" if not strat_band_html else strat_band_html}</tbody>
</table>
<p class="footnote" style="margin-top:8px">
  各BT帯の「合計」行はその帯の全取引。その下の戦略行は同帯内のサブセット（損益降順）。<br>
  同BT帯内で戦略間のPFに差があれば、戦略選別がBT選別に加えて有効な証拠。
</p>
</div>

<div id="analtab_{_dseq}_rollfwd" class="analysis-tab-pane">
{_rolling_oos_cache_html()}
<hr style="border-color:#334155;margin:20px 0">
{rollforward_html}
</div>

<div id="analtab_{_dseq}_factors" class="analysis-tab-pane">
{_factors_html}
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
{('<h3 style="color:#cbd5e1;margin:8px 0 6px">🔻 lss 終値損切り比較（BT30以上）</h3>' + _inv_closestop) if _LSS_ORDER_MODE else _inv_closestop}
</div>
{_inv_analysis_panes}

<div id="analtab_{_dseq}_overlap" class="analysis-tab-pane">
{_overlap_html if _overlap_html else '<p style="color:#64748b;padding:20px">重複保有(既保有中の同銘柄シグナル)の決済済みトレードが3件未満のため表示なし</p>'}
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

<div id="analtab_{_dseq}_pullback" class="analysis-tab-pane">
<!-- PULLBACK_CMP_SLOT -->
</div>

<div id="analtab_{_dseq}_openconfirm" class="analysis-tab-pane">
<!-- OPENCONFIRM_SLOT -->
</div>

<div id="analtab_{_dseq}_filltiming" class="analysis-tab-pane">
<!-- FILLTIMING_SLOT -->
</div>

<div id="analtab_{_dseq}_stopwidth" class="analysis-tab-pane">
<!-- STOPWIDTH_SLOT -->
</div>

<div id="analtab_{_dseq}_opendir" class="analysis-tab-pane">
<!-- OPENDIR_SLOT -->
</div>

<div id="analtab_{_dseq}_drop" class="analysis-tab-pane">
<!-- DROP_SLOT -->
</div>

<div id="analtab_{_dseq}_fadeshort" class="analysis-tab-pane">
<!-- FADESHORT_SLOT -->
</div>

<div id="analtab_{_dseq}_holdcurve" class="analysis-tab-pane">
<!-- HOLDCURVE_SLOT -->
</div>

<div id="analtab_{_dseq}_emcmp" class="analysis-tab-pane">
<!-- EMCMP_SLOT -->
</div>

<div id="analtab_{_dseq}_breadth" class="analysis-tab-pane">
{_signal_breadth_html}
</div>

<div id="analtab_{_dseq}_sameday" class="analysis-tab-pane">
<!-- SAMEDAY_TPSL_SLOT -->
</div>

<div id="analtab_{_dseq}_sameday5m" class="analysis-tab-pane">
<!-- SAMEDAY_5M_SLOT -->
</div>

</div>

{_trend_breakdown_html}

<h2>取引明細</h2>
{_bt_filter_banner_html()}
{_overlap_kpi_html}
<div class="detail-tab-nav">
  <button class="detail-tab-btn active" onclick="switchDetailTab({_dseq},'all')">全部（決済日順） <span style="font-size:0.72rem;color:#94a3b8">({len(sorted_trades)})</span></button>
  <button class="detail-tab-btn" onclick="switchDetailTab({_dseq},'bt60')" style="border-color:#16a34a">🎯 BT60以上 <span style="font-size:0.72rem;color:#86efac">({len(bt60_trades)})</span></button>
  <button class="detail-tab-btn" onclick="switchDetailTab({_dseq},'entry')">エントリー日別 <span style="font-size:0.72rem;color:#94a3b8">(直近{_ENTRY_GRID_DAYS}日)</span></button>
  {_bt40liq_btn}
  {_bt70liq_btn}
  {_tenkan_tab_btn}
</div>
<div id="detail_{_dseq}_all" class="detail-tab-pane active">
{'<p style="color:#60a5fa;font-size:0.82rem;font-weight:700;margin:4px 0 10px;border-left:3px solid #60a5fa;padding-left:8px">🔄 転換トレード(lss未約定→ロング転換): <b>' + str(len(_tenkan_in_sorted)) + '件</b> 含む（直近' + str(_DETAIL_ROW_CAP) + '件上限の外でも追加表示）</p>' if _tenkan_in_sorted else ''}
<table>
  <thead><tr>
    <th>決済日</th>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th>設定</th>
    <th>約定値<br><small style="color:#94a3b8">逆指値/指値</small></th><th style="color:#f87171">損切り</th><th style="color:#4ade80">目標</th><th>現在値</th><th>決済値</th><th>株数</th><th>保有</th><th>遅延</th>
    <th>損益</th><th>理由</th><th>エントリー</th>
  </tr></thead>
  <tbody>{trade_rows_all}</tbody>
</table>
</div>
<div id="detail_{_dseq}_bt60" class="detail-tab-pane">
<p style="color:#86efac;font-size:0.8rem;margin-bottom:10px">
🎯 BTスコア60以上だけを抽出。このレポートのシグナル時BT(表示BTと同一)で判定。{len(bt60_trades)}件。</p>
<table>
  <thead><tr>
    <th>決済日</th>
    <th style="text-align:left">銘柄</th>
    <th>戦略</th>
    <th>設定</th>
    <th>約定値<br><small style="color:#94a3b8">逆指値/指値</small></th><th style="color:#f87171">損切り</th><th style="color:#4ade80">目標</th><th>現在値</th><th>決済値</th><th>株数</th><th>保有</th><th>遅延</th>
    <th>損益</th><th>理由</th><th>エントリー</th>
  </tr></thead>
  <tbody>{trade_rows_bt60}</tbody>
</table>
</div>
<div id="detail_{_dseq}_entry" class="detail-tab-pane">
<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:10px">日付をクリックで詳細表示（直近{_ENTRY_GRID_DAYS}日）</p>
{_month_summary_html(entry_sorted_trades)}
{_month_accordion_html(_entry_by_date, _sorted_entry_dates, _dseq, "e")}
</div>
{_bt40liq_pane}
{_bt70liq_pane}
{_tenkan_tab_pane}
{_bt50liq_pane}
{_fill_liq_pane}
{_mlot_liq_pane}
{_narrow_liq_pane}
{_fill_narrow_liq_pane}
{_mlot_narrow_liq_pane}
<script>
function switchAnalysisTab(seq, which) {{
  var tabs = {_analysis_tabs_js};
  tabs.forEach(function(t) {{
    var pane = document.getElementById('analtab_'+seq+'_'+t);
    if (pane) pane.classList.toggle('active', t === which);
  }});
  var nav = document.querySelector('#analysis_'+seq+' .analysis-tab-nav');
  if (nav) {{
    nav.querySelectorAll('.analysis-tab-btn').forEach(function(b) {{
      var onclick = b.getAttribute('onclick') || '';
      var m = onclick.match(/switchAnalysisTab\\(\\d+,'([^']+)'\\)/);
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
function switchOvBt(uid, key) {{
  ['all','bt60','bt70'].forEach(function(k) {{
    var blk = document.getElementById('ovblk_'+uid+'_'+k);
    if (blk) blk.style.display = (k === key) ? 'block' : 'none';
    var btn = document.getElementById('ovbtn_'+uid+'_'+k);
    if (btn) btn.classList.toggle('active', k === key);
  }});
}}
function switchDetailTab(seq, which) {{
  var target = document.getElementById('detail_'+seq+'_'+which);
  if (!target) return;
  var closing = target.classList.contains('active');
  var allPane = document.getElementById('detail_'+seq+'_all');
  if (!allPane) return;
  var container = allPane.parentNode;
  var pfx = 'detail_'+seq+'_';
  container.querySelectorAll('[id^="'+pfx+'"]').forEach(function(el) {{
    el.classList.toggle('active', (!closing) && el === target);
  }});
  container.querySelectorAll('.detail-tab-btn').forEach(function(b) {{
    var oc = b.getAttribute('onclick') || '';
    var m = oc.match(/switchDetailTab\(\d+,'([^']+)'\)/);
    if (m) b.classList.toggle('active', (!closing) && m[1] === which);
  }});
}}
function toggleMG(hdr) {{
  // 月ヘッダー(this)を起点に、その直後の .mg-body を開閉する。
  // 以前は getElementById で id 指定していたが、同一ページに同じ id が複数あると
  // 常に最初の要素だけを操作してしまい「押しても開かない」不具合が出た。
  // DOM の兄弟関係で辿ることで id 重複に依存しない(=堅牢)。
  if (typeof hdr === 'string') {{  // 旧シグネチャ toggleMG(pfx,seq,ym) 後方互換
    var _b = document.getElementById('mgb_'+arguments[0]+arguments[1]+'_'+arguments[2]);
    if (_b) hdr = _b.previousElementSibling; else return;
  }}
  var body = hdr.nextElementSibling;
  var arr  = hdr.querySelector('.mg-arrow');
  if (!body) return;
  var isOpen = body.style.display !== 'none';
  body.style.display = isOpen ? 'none' : 'block';
  if (arr) arr.textContent = isOpen ? '▶' : '▼';
}}
function _showEntryDateGrid(btn, dk) {{
  // クリックしたボタン(this)を起点に、同じ月ブロック(.mg-body)内の詳細だけ開閉する。
  // 以前は getElementById で id 指定していたが、同一ページに同じ id が複数あると
  // 常に最初の(隠れた)要素を操作してしまい「押しても詳細が出ない」不具合が出た。
  // DOM の親子関係で辿ることで id 重複に依存しない(=堅牢)。
  if (typeof btn === 'string' || typeof btn === 'number') return;  // 旧シグネチャ無効化
  var body = btn.closest('.mg-body');
  if (!body) return;
  var wasActive = btn.classList.contains('edate-active');
  // この月のボタン選択を解除 + 詳細を全部閉じる(この月ブロック内だけ)
  body.querySelectorAll('.edate-btn').forEach(function(b) {{ b.classList.remove('edate-active'); }});
  var det = body.querySelector('.mg-detail');
  if (det) {{
    det.querySelectorAll('[id*="date_detail_"]').forEach(function(el) {{ el.style.display='none'; }});
  }}
  if (!wasActive && det) {{
    // クリック日の詳細は id が _<dk> で終わる。この月ブロック内で一意。
    var target = det.querySelector('[id*="date_detail_"][id$="_'+dk+'"]');
    if (target) {{
      target.style.display = 'block';
      setTimeout(function() {{ target.scrollIntoView({{behavior:'smooth', block:'nearest'}}); }}, 50);
    }}
    btn.classList.add('edate-active');
  }}
}}
// 日別カードのクリックはすべてこの共通関数を呼ぶ(prefix 非依存)。
// prefix ごとに showEntryDate<PFX> を定義する方式は、新タブ(q6 等)追加時に
// 定義漏れ→クリック無反応になっていたため廃止した。以下の別名は後方互換用。
function showEntryDateGrid(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateE(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateB(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateC(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateX(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateY(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQ(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQ5(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQ6(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQN(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQF(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQFN(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQML(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateQMLN(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function showEntryDateTK(btn, dk) {{ _showEntryDateGrid(btn, dk); }}
function toggleAnalysis(seq) {{
  var blk = document.getElementById('analysis_'+seq);
  var btn = document.getElementById('analysis_btn_'+seq);
  if (!blk) return;
  var show = (blk.style.display === 'none');
  blk.style.display = show ? 'block' : 'none';
  if (btn) btn.textContent = (show ? '▼ 詳細分析（スクリプト別・スコア別・銘柄別）を隠す'
                                   : '▶ 詳細分析（スクリプト別・スコア別・銘柄別）を表示');
}}

// === タブ遅延DOM (Lazy Tab DOM) ===
// 非アクティブな .detail-tab-pane / .tab-pane の DOM ノードを DOMContentLoaded 後に
// <template> へ退避し、ブラウザが管理するライブDOMを大幅削減。
// タブ初回クリック時に注入することで描画スループットを維持する。
(function() {{
  var _inj = Object.create(null);

  var _orig = switchDetailTab;
  switchDetailTab = function(seq, which) {{
    var pid = 'detail_' + seq + '_' + which;
    if (!_inj[pid]) {{
      var tpl = document.getElementById('_ltpl_' + pid);
      var pane = document.getElementById(pid);
      if (tpl && pane) {{ pane.appendChild(tpl.content); tpl.parentNode.removeChild(tpl); }}
      _inj[pid] = true;
    }}
    _orig(seq, which);
  }};

  var _origTab = switchTab;
  switchTab = function(id) {{
    var tpl = document.getElementById('_ltpl_tab_' + id);
    var pane = document.getElementById(id);
    if (tpl && pane) {{ pane.appendChild(tpl.content); tpl.parentNode.removeChild(tpl); }}
    _origTab(id);
  }};

  document.addEventListener('DOMContentLoaded', function() {{
    // 1) 非アクティブ外側タブを退避（最大の削減効果）
    document.querySelectorAll('.tab-pane:not(.active)').forEach(function(p) {{
      if (!p.id || !p.firstChild) return;
      var t = document.createElement('template');
      t.id = '_ltpl_tab_' + p.id;
      while (p.firstChild) t.content.appendChild(p.firstChild);
      p.after(t);
    }});
    // 2) アクティブ外側タブ内の非アクティブ内側タブを退避
    document.querySelectorAll('.detail-tab-pane:not(.active)').forEach(function(p) {{
      if (!p.id || !p.firstChild) return;
      var t = document.createElement('template');
      t.id = '_ltpl_' + p.id;
      while (p.firstChild) t.content.appendChild(p.firstChild);
      p.after(t);
    }});
  }});
}})();
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
.ovbt-btn { padding:6px 16px; background:#1e293b; border:1px solid #334155;
            border-radius:6px; color:#94a3b8; cursor:pointer;
            font-size:0.85rem; font-family:inherit; }
.ovbt-btn.active { background:#1a1333; color:#a78bfa; border-color:#7c3aed; font-weight:700; }
.ovbt-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.detail-tab-pane { display:none; }
.detail-tab-pane.active { display:block; }
/* 取引明細テーブルは横スクロールを出さず全列を一画面に収める (コンパクト表示) */
.detail-tab-pane table { width:100%; border-collapse:collapse; }
.detail-tab-pane th, .detail-tab-pane td {
  font-size:0.72rem; padding:3px 4px; white-space:nowrap; }
/* 銘柄名だけは折り返し可 (切り詰めない) */
.detail-tab-pane td:nth-child(2), .detail-tab-pane th:nth-child(2) {
  white-space:normal; word-break:break-word; }
.detail-tab-pane th small { font-size:0.6rem; }

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
