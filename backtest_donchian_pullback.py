"""
Donchian Channel Breakout + Pullback Limit Entry Backtest
=========================================================
ドンチャン・ブレイクアウト 押し目指値エントリー バックテスト

Strategy:
  1. Signal: Today close breaks above highest close of past BREAK_PERIOD days
  2. Entry: Place LIMIT BUY at breakout level * (1 - LIMIT_OFFSET) - pullback retest
  3. Exit: Stop loss (ATR-based), profit target (ATR-based), max hold days

Usage:
  python backtest_donchian_pullback.py                   # watch universe scan
  python backtest_donchian_pullback.py --universe all    # all symbols scan
  python backtest_donchian_pullback.py --symbol 7203.T   # single symbol
  python backtest_donchian_pullback.py --days 730        # change backtest period
  python backtest_donchian_pullback.py --no-browser      # skip browser launch
"""

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import pickle
import webbrowser
from _open_html import open_html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── 定数 ─────────────────────────────────────────────────────
WORKERS         = 4
LOT_SIZE        = 100       # 1回あたり株数（固定100株）
BACKTEST_DAYS   = 365       # デフォルトのバックテスト日数
JST             = timezone(timedelta(hours=9))
_TODAY          = pd.Timestamp(datetime.now(tz=JST).date())
_CACHE_DIR      = Path(".rsi2_cache")
_BT_CACHE       = _CACHE_DIR / "bt_results_donchian_pullback.pkl"

BREAK_PERIOD    = 20     # N日高値ブレイクアウト判定期間
VOL_MA_PERIOD   = 20     # 出来高移動平均期間
VOL_THRESHOLD   = 1.2    # ブレイクアウト時の出来高倍率（平均の1.2倍以上）
LIMIT_OFFSET    = 0.002  # 指値オフセット（ブレイク水準の0.2%下）
STOP_ATR_MULT   = 1.5    # ストップ（ATR倍率）
TARGET_ATR_MULT = 3.0    # 利確（ATR倍率）
ENTRY_EXPIRE    = 5      # 指値有効日数（5日以内に未成立でキャンセル）
MAX_HOLD        = 20     # 最大保有日数
MA_FILTER       = 200    # トレンドフィルター（MA200上）

WATCHLIST_PERIODS = [30, 90, 180, 365]
WL_MIN_TRADES     = 3
WL_MIN_WR         = 55.0   # トレンドフォローは少し緩める
WL_MIN_PF         = 1.3


# ── 銘柄ユニバース ────────────────────────────────────────────
from symbols_watch_rsi2 import SYMBOLS as _WATCH_SYMBOLS

try:
    from rsi2 import SYMBOLS as _ALL_SYMBOLS
except ImportError:
    _ALL_SYMBOLS = _WATCH_SYMBOLS


def _load_symbols(universe: str) -> list:
    """ユニバース名に応じて銘柄リストを返す。
    watch : 監視銘柄 / 225 : 日経225 / all : 全上場銘柄
    """
    if universe == "225":
        from symbols_all import SYMBOLS as _S225
        return list(_S225)
    if universe == "all":
        _p = Path("symbols_listed_all.py")
        if _p.exists():
            import importlib.util
            _spec = importlib.util.spec_from_file_location("_listed_all", _p)
            _mod  = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            return list(_mod.SYMBOLS)
        print("  ※ symbols_listed_all.py が見つかりません。日経225を使用します。")
        from symbols_all import SYMBOLS as _S225
        return list(_S225)
    return list(_WATCH_SYMBOLS)


# ── データ取得 ────────────────────────────────────────────────
def fetch(symbol: str, backtest_days: int) -> "pd.DataFrame | None":
    """永続キャッシュ優先（.rsi2_cache/*.pkl）・フォールバックでyfinanceダウンロード。"""
    _CACHE_DIR.mkdir(exist_ok=True)

    # ── 永続キャッシュ確認 ──────────────────────────────────
    persistent = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    if persistent.exists():
        try:
            _mtime = datetime.fromtimestamp(persistent.stat().st_mtime, tz=JST)
            if _mtime.date() == datetime.now(JST).date():
                with open(persistent, "rb") as f:
                    df = pickle.load(f)
                if len(df) >= 210:
                    return df
        except Exception:
            pass

    # ── フォールバック: yfinanceダウンロード ──────────────────
    buf_days  = 200 + 30
    total_cal = int((backtest_days + buf_days) * 1.5)
    now_jst   = datetime.now(JST)
    dl_start  = (now_jst - timedelta(days=total_cal)).strftime("%Y-%m-%d")
    dl_end    = (now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        raw = yf.Ticker(symbol).history(
            start=dl_start, end=dl_end, interval="1d",
            auto_adjust=False, actions=False
        )
        if raw.empty:
            return None
        if raw.index.tz is not None:
            raw.index = raw.index.tz_localize(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
        raw  = raw[cols]
        # 最終行のCloseがNaN・出来高あり → fast_infoで補完
        if len(raw) > 0:
            _last = raw.iloc[-1]
            if pd.isna(_last.get("close", float("nan"))) and _last.get("volume", 0) > 0:
                try:
                    _lp = yf.Ticker(symbol).fast_info.last_price
                    if _lp and not pd.isna(_lp):
                        raw.at[raw.index[-1], "close"] = float(_lp)
                except Exception:
                    pass
        raw = raw.dropna(subset=["close"])
        if len(raw) < 210:
            return None
        df_out = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw.get("volume", pd.Series(0, index=raw.index)).to_numpy(dtype=float),
        }, index=raw.index)
        try:
            with open(persistent, "wb") as f:
                pickle.dump(df_out, f)
        except Exception:
            pass
        return df_out
    except Exception:
        return None


# ── 指標計算 ──────────────────────────────────────────────────
def calc(df: pd.DataFrame) -> pd.DataFrame:
    """
    ドンチャン・ブレイクアウト判定に必要な指標を計算する。

    追加列:
      ma200    : 200日移動平均
      tr       : True Range
      atr      : ATR (Wilder smoothing, com=13)
      don_high : 過去BREAK_PERIOD本の終値最高値（shift(1)で当日を除外）
      vol_ma   : 出来高のVOL_MA_PERIOD日移動平均
      breakout : ブレイクアウトシグナル（bool）
    """
    df   = df.copy()
    c    = df["close"]
    h    = df["high"]
    l    = df["low"]
    prev = c.shift(1)

    # MA200
    df["ma200"] = c.rolling(MA_FILTER).mean()

    # ATR (Wilder smoothing, com=13 == span=27 approx, matches ewm(com=13))
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=13, adjust=False).mean()

    # Donchian high: highest close of previous BREAK_PERIOD bars (today excluded)
    df["don_high"] = c.shift(1).rolling(BREAK_PERIOD).max()

    # Volume MA
    df["vol_ma"] = df["volume"].rolling(VOL_MA_PERIOD).mean()

    # Breakout signal
    df["breakout"] = (
        (c > df["don_high"]) &
        (df["volume"] > VOL_THRESHOLD * df["vol_ma"]) &
        (c > df["ma200"])
    )

    return df


# ── バックテスト（ドンチャン・ブレイクアウト押し目指値）────────────────────
def backtest_donchian(df: pd.DataFrame, backtest_days: int) -> list:
    """
    ドンチャン・ブレイクアウト + 押し目指値エントリー バックテスト。

    エントリー: ブレイクアウト翌日から limit = don_high * (1 - LIMIT_OFFSET) の指値
    決済: ストップロス / 利確 / 最大保有日数
    Returns: トレードログ（dict のリスト）
    """
    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()
    if len(df) < 3:
        return []

    trades = []

    # 状態変数
    in_pos        = False
    entry_p       = 0.0
    entry_dt      = None
    entry_atr     = 0.0
    limit_p_entry = 0.0   # actual limit price used for entry
    stop_level    = 0.0
    target_level  = 0.0

    # 未約定注文
    pending_order = None  # dict: {limit_price, atr, placed_date, expire}

    for i in range(1, len(df)):
        row          = df.iloc[i]
        prev         = df.iloc[i - 1]
        current_date = df.index[i]

        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        # ── ポジションあり: 決済判定 ──────────────────────────
        if in_pos:
            hold_days = (current_date - entry_dt).days
            exit_p    = None
            reason    = None

            if op <= stop_level:
                # ギャップダウンでストップ
                exit_p = op
                reason = "ギャップストップ"
            elif lo <= stop_level:
                exit_p = stop_level
                reason = "ストップロス"
            elif hi >= target_level:
                exit_p = target_level
                reason = "利確"
            elif hold_days >= MAX_HOLD:
                exit_p = cl
                reason = f"最大{MAX_HOLD}日"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * LOT_SIZE
                pct = (exit_p - entry_p) / entry_p * 100
                trades.append(dict(
                    entry_dt     = entry_dt,
                    exit_dt      = current_date,
                    entry_p      = entry_p,
                    exit_p       = exit_p,
                    limit_p      = limit_p_entry,
                    stop_p       = stop_level,
                    target_p     = target_level,
                    atr          = entry_atr,
                    pnl          = pnl,
                    pct          = pct,
                    hold         = hold_days,
                    reason       = reason,
                    signal_dt    = signal_dt,
                    signal_close = signal_close,
                ))
                in_pos        = False
                pending_order = None
            continue

        # ── 未約定注文: フィル判定 ────────────────────────────
        if pending_order is not None:
            age = (current_date - pending_order["placed_date"]).days
            if age > ENTRY_EXPIRE:
                # 期限切れキャンセル
                pending_order = None
            elif lo <= pending_order["limit_price"]:
                # 約定
                entry_p       = pending_order["limit_price"]
                entry_atr     = pending_order["atr"]
                entry_dt      = current_date
                limit_p_entry = entry_p
                stop_level    = entry_p - STOP_ATR_MULT   * entry_atr
                target_level  = entry_p + TARGET_ATR_MULT * entry_atr
                signal_dt     = pending_order["signal_dt"]
                signal_close  = pending_order["signal_close"]
                in_pos        = True
                pending_order = None
            continue

        # ── ポジションなし・注文なし: シグナル判定 ─────────────
        prev_breakout = prev.get("breakout", False)
        if not prev_breakout:
            continue
        prev_don_high = float(prev.get("don_high", float("nan")))
        prev_atr      = float(prev.get("atr",      float("nan")))
        if pd.isna(prev_don_high) or pd.isna(prev_atr) or prev_atr <= 0:
            continue

        lim_price = prev_don_high * (1.0 - LIMIT_OFFSET)
        pending_order = {
            "limit_price":  lim_price,
            "atr":          prev_atr,
            "placed_date":  current_date,
            "expire":       ENTRY_EXPIRE,
            "signal_dt":    prev.name,
            "signal_close": float(prev["close"]),
        }

    # 未決済ポジション（バックテスト最終日）
    if in_pos:
        last_cl   = float(df.iloc[-1]["close"])
        last_dt   = df.index[-1]
        hold_days = (last_dt - entry_dt).days
        pnl = (last_cl - entry_p) * LOT_SIZE
        pct = (last_cl - entry_p) / entry_p * 100
        trades.append(dict(
            entry_dt     = entry_dt,
            exit_dt      = last_dt,
            entry_p      = entry_p,
            exit_p       = last_cl,
            limit_p      = limit_p_entry,
            stop_p       = stop_level,
            target_p     = target_level,
            atr          = entry_atr,
            pnl          = pnl,
            pct          = pct,
            hold         = hold_days,
            reason       = "保有中",
            signal_dt    = signal_dt,
            signal_close = signal_close,
        ))

    return trades


# ── 今日のシグナル判定 ────────────────────────────────────────
def _has_signal_today(df: pd.DataFrame) -> "dict | None":
    """
    最終バーのブレイクアウトシグナルを判定する。
    シグナルあり → 注文情報dictを返す。
    シグナルなし → None を返す。
    """
    if len(df) < 2:
        return None
    last = df.iloc[-1]

    breakout = bool(last.get("breakout", False))
    if not breakout:
        return None

    don_high = float(last.get("don_high", float("nan")))
    atr      = float(last.get("atr",      float("nan")))
    close    = float(last.get("close",    float("nan")))
    vol_ma   = float(last.get("vol_ma",   float("nan")))
    volume   = float(last.get("volume",   float("nan")))

    if pd.isna(don_high) or pd.isna(atr) or atr <= 0:
        return None

    limit_price = don_high * (1.0 - LIMIT_OFFSET)
    stop        = limit_price - STOP_ATR_MULT   * atr
    target      = limit_price + TARGET_ATR_MULT * atr
    vol_ratio   = (volume / vol_ma) if (not pd.isna(vol_ma) and vol_ma > 0) else float("nan")

    return dict(
        close       = close,
        don_high    = don_high,
        atr         = atr,
        limit_price = limit_price,
        stop        = stop,
        target      = target,
        vol_ratio   = vol_ratio,
    )


# ── 1銘柄処理（スレッドプール用）──────────────────────────────
def _process_symbol(symbol: str, name: str, backtest_days: int) -> "dict | None":
    """
    1銘柄のデータ取得・指標計算・バックテストを実行する。
    結果 dict または None を返す。
    """
    df = fetch(symbol, backtest_days)
    if df is None:
        return None
    df = calc(df)
    if len(df) < 50:
        return None

    trades     = backtest_donchian(df, backtest_days)
    today_sig  = _has_signal_today(df)
    wins       = [t for t in trades if t["pnl"] > 0]
    losses     = [t for t in trades if t["pnl"] <= 0]
    total_pnl  = sum(t["pnl"] for t in trades)
    total_pct  = sum(t["pct"] for t in trades)
    win_rate   = len(wins) / len(trades) * 100 if trades else float("nan")
    pf_val     = (
        sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
        if losses and sum(t["pnl"] for t in losses) != 0
        else float("inf")
    )

    return dict(
        symbol      = symbol,
        name        = name,
        trades_list = trades,
        trade_count = len(trades),
        win_rate    = win_rate,
        pf          = pf_val,
        total       = total_pnl,
        total_pct   = total_pct,
        today_sig   = today_sig,
    )


# ── HTML レポート生成 ─────────────────────────────────────────
_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}
h1{font-size:1.4em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}
h2{font-size:1.1em;color:#e2e8f0;margin:24px 0 8px;border-left:3px solid #a78bfa;padding-left:10px}
.meta{color:#666;font-size:0.82em;margin:2px 0 8px 16px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.card{background:#16192a;border:1px solid #252840;border-radius:10px;padding:14px 20px;min-width:130px}
.clabel{font-size:0.72em;color:#777;letter-spacing:.05em}
.cval{font-size:1.55em;font-weight:700;margin-top:3px}
.pos{color:#4ade80}.neg{color:#f87171}.neu{color:#c8cfe8}
.section{background:#11141f;border:1px solid #1e2235;border-radius:10px;padding:16px;margin:20px 0}
.section-title{font-size:1.0em;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;
                margin-bottom:12px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:0.85em}
th{background:#16192a;color:#888;padding:8px 12px;text-align:right;
    border-bottom:1px solid #252840;white-space:nowrap}
th:first-child,th:nth-child(2),th:nth-child(3){text-align:left}
td{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}
td:first-child,td:nth-child(2),td:nth-child(3){text-align:left}
tr.win>td{background:rgba(74,222,128,.04)}
tr.lose>td{background:rgba(248,113,113,.04)}
tr.hold>td{background:rgba(251,191,36,.06)}
tr:hover>td{background:#1b1f35!important}
.detail-section{background:#13162b;border:1px solid #1e2235;border-radius:10px;padding:20px;margin:24px 0}
.name-label{color:#94a3b8;font-size:0.85em;font-weight:400}
.detail-stats{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 14px;font-size:0.88em;color:#888}
.detail-stats b{color:#dde1ec}
.footer{margin-top:32px;color:#444;font-size:0.78em;text-align:right}
"""


def _pct_class(v: float) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "neu")


def _trade_table_html(trades: list) -> str:
    rows = ""
    for i, t in enumerate(trades, 1):
        cls  = _pct_class(t["pct"])
        mark = "★" if "保有中" in t["reason"] else ""
        rows += (
            f'<tr class="{"hold" if mark else ("win" if t["pnl"] > 0 else "lose")}">'
            f'<td>{mark}{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["limit_p"]:,.0f}</td>'
            f'<td>{t["entry_p"]:,.0f}</td>'
            f'<td>{t["exit_p"]:,.0f}</td>'
            f'<td>{t["stop_p"]:,.0f}</td>'
            f'<td>{t["target_p"]:,.0f}</td>'
            f'<td class="{cls}">{t["pct"]:+.2f}%</td>'
            f'<td>{t["hold"]}日</td>'
            f'<td>{t["reason"]}</td></tr>\n'
        )
    header = (
        "<tr>"
        "<th>#</th><th>エントリー日</th><th>決済日</th>"
        "<th>指値</th><th>約定値</th><th>決済値</th>"
        "<th>ストップ</th><th>利確</th>"
        "<th>損益%</th><th>保有</th><th>理由</th></tr>"
    )
    return f"<table>{header}{rows}</table>"



def build_html(
    signals: list,
    scan_results: list,
    backtest_days: int,
    universe: str,
) -> "Path":
    """
    HTMLレポートを生成してPathを返す。
    """
    today_str = _TODAY.strftime("%Y-%m-%d")
    since_str = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    gen_time  = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    # ── 今日のシグナルテーブル ───────────────────────────────
    signal_rows = ""
    for s in signals:
        sym   = s.get("symbol", "")
        name  = s.get("name",   "")
        close = s.get("close",       float("nan"))
        dh    = s.get("don_high",    float("nan"))
        lim   = s.get("limit_price", float("nan"))
        stp   = s.get("stop",        float("nan"))
        tgt   = s.get("target",      float("nan"))
        vr    = s.get("vol_ratio",   float("nan"))
        vr_s  = f"{vr:.2f}x" if not pd.isna(vr) else "—"

        signal_rows += (
            f'<tr>'
            f'<td><b>{sym}</b></td>'
            f'<td>{name}</td>'
            f'<td class="neu">{close:,.0f}</td>'
            f'<td class="neu">{dh:,.0f}</td>'
            f'<td class="pos"><b>{lim:,.0f}</b></td>'
            f'<td class="neg">{stp:,.0f}</td>'
            f'<td class="pos">{tgt:,.0f}</td>'
            f'<td>{vr_s}</td>'
            f'</tr>\n'
        )

    if not signal_rows:
        signal_rows = '<tr><td colspan="8" style="text-align:center;color:#555;padding:20px">本日シグナルなし</td></tr>\n'

    # ── スキャン結果テーブル ──────────────────────────────────
    ranked = sorted(scan_results, key=lambda x: -x.get("total_pct", 0))
    scan_rows = ""
    for rank, r in enumerate(ranked, 1):
        wr_v = r.get("win_rate",   float("nan"))
        pf_v = r.get("pf",        float("nan"))
        tot  = r.get("total",     0)
        tp   = r.get("total_pct", 0.0)
        tr_n = r.get("trade_count", 0)
        pcls = "pos" if tot >= 0 else "neg"
        wr_s = f"{wr_v:.1f}%" if not pd.isna(wr_v) else "—"
        pf_s = ("∞" if pf_v == float("inf") else f"{pf_v:.2f}") if not pd.isna(pf_v) else "—"
        scan_rows += (
            f'<tr class="{"win" if tot >= 0 else "lose"}">'
            f'<td>{rank}</td>'
            f'<td>{r["symbol"]}</td>'
            f'<td>{r["name"]}</td>'
            f'<td class="{pcls}">{tot:+,.0f}円</td>'
            f'<td class="{pcls}">{tp:+.2f}%</td>'
            f'<td>{wr_s}</td>'
            f'<td>{pf_s}</td>'
            f'<td>{tr_n}</td>'
            f'</tr>\n'
        )

    # ── 詳細セクション（シグナル銘柄のみ）───────────────────
    detail_sections = ""
    signal_symbols  = {s["symbol"] for s in signals}
    for r in scan_results:
        if r["symbol"] not in signal_symbols:
            continue
        trades  = r.get("trades_list", [])
        wins    = [t for t in trades if t["pnl"] > 0]
        losses  = [t for t in trades if t["pnl"] <= 0]
        total   = sum(t["pnl"] for t in trades)
        wr_val  = len(wins) / len(trades) * 100 if trades else 0
        pf_val  = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                   if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))
        pf_str  = "∞" if pf_val == float("inf") else f"{pf_val:.2f}"
        tc_cls  = "pos" if total >= 0 else "neg"
        detail_sections += f"""
<div class="detail-section">
  <h2>{r["symbol"]} &nbsp;<span class="name-label">{r["name"]}</span></h2>
  <div class="detail-stats">
    <span>トレード: <b>{len(trades)}</b></span>
    <span>勝率: <b class="{"pos" if wr_val >= 60 else "neu"}">{wr_val:.1f}%</b></span>
    <span>PF: <b class="{"pos" if pf_val >= 1.5 else "neu"}">{pf_str}</b></span>
    <span>損益: <b class="{tc_cls}">{total:+,.0f}円</b></span>
    <span>勝/負: <b>{len(wins)}/{len(losses)}</b></span>
  </div>
  {_trade_table_html(trades)}
</div>
"""

    # ── サマリー統計 ──────────────────────────────────────────
    total_pnl    = sum(r.get("total", 0)       for r in scan_results)
    total_trades = sum(r.get("trade_count", 0) for r in scan_results)
    plus_count   = sum(1 for r in scan_results if r.get("total", 0) > 0)
    tc_cls2      = "pos" if total_pnl >= 0 else "neg"
    nsigs        = len(signals)
    nuniv        = len(scan_results)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ドンチャン・ブレイクアウト バックテスト {today_str}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>ドンチャン・ブレイクアウト バックテスト</h1>
<div class="meta">
  期間: {since_str} 〜 {today_str} &nbsp;|&nbsp;
  ユニバース: {universe} ({nuniv}銘柄) &nbsp;|&nbsp;
  バックテスト: {backtest_days}日 &nbsp;|&nbsp;
  N日高値: {BREAK_PERIOD}日 / 指値オフセット: {LIMIT_OFFSET*100:.1f}%
</div>

<div class="cards">
  <div class="card"><div class="clabel">本日シグナル</div>
    <div class="cval {"pos" if nsigs > 0 else "neu"}">{nsigs}銘柄</div></div>
  <div class="card"><div class="clabel">全損益合計</div>
    <div class="cval {tc_cls2}">{total_pnl:+,.0f}円</div></div>
  <div class="card"><div class="clabel">トレード計</div>
    <div class="cval neu">{total_trades}回</div></div>
  <div class="card"><div class="clabel">プラス銘柄</div>
    <div class="cval neu">{plus_count}/{nuniv}</div></div>
</div>

<div class="section">
  <div class="section-title">本日のシグナル — {BREAK_PERIOD}日高値ブレイクアウト</div>
  <table>
  <thead><tr>
    <th>コード</th><th>銘柄名</th><th>終値</th><th>N日高値</th>
    <th>指値</th><th>ストップ</th><th>利確目標</th><th>出来高比</th>
  </tr></thead>
  <tbody>{signal_rows}</tbody>
  </table>
</div>

{detail_sections}

<div class="section">
  <div class="section-title">スキャン結果ランキング ({since_str} 〜 {today_str})</div>
  <table>
  <thead><tr>
    <th>ランク</th><th>コード</th><th>銘柄名</th>
    <th>損益(円)</th><th>損益(%)</th><th>勝率</th><th>PF</th><th>取引回数</th>
  </tr></thead>
  <tbody>{scan_rows}</tbody>
  </table>
</div>

<div class="footer">生成: {gen_time} &nbsp;|&nbsp; ドンチャン・ブレイクアウト押し目指値 (N={BREAK_PERIOD}日 / オフセット{LIMIT_OFFSET*100:.1f}% / Stop{STOP_ATR_MULT}ATR / Target{TARGET_ATR_MULT}ATR)</div>
</body></html>"""

    fname = f"donchian_bt_{today_str}.html"
    path  = Path(fname)
    path.write_text(html, encoding="utf-8")
    return path



def _calc_period_stats(trades):
    if not trades:
        return dict(n=0, wr=float("nan"), pf=float("nan"), total=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    loss = [t for t in trades if t["pnl"] <= 0]
    wr   = len(wins) / len(trades) * 100
    ls   = abs(sum(t["pnl"] for t in loss))
    pf   = sum(t["pnl"] for t in wins) / ls if ls > 0 else float("inf")
    return dict(n=len(trades), wr=wr, pf=pf, total=sum(t["pnl"] for t in trades))


def _load_bt_cache() -> dict:
    try:
        if _BT_CACHE.exists():
            with open(_BT_CACHE, "rb") as f:
                return pickle.load(f)
    except Exception:
        pass
    return {}

def _save_bt_cache(cache: dict) -> None:
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        with open(_BT_CACHE, "wb") as f:
            pickle.dump(cache, f)
    except Exception:
        pass

def _price_cache_mtime(symbol: str) -> float:
    p = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    return p.stat().st_mtime if p.exists() else 0.0


def _process_symbol_multiperiod(symbol, name, periods, bt_cache=None):
    mtime = _price_cache_mtime(symbol)
    cache_key = (symbol, tuple(sorted(periods)))
    if bt_cache is not None and cache_key in bt_cache:
        cached_mtime, cached_result = bt_cache[cache_key]
        if cached_mtime == mtime and mtime > 0 and "period_trades" in cached_result:
            return cached_result

    max_days = max(periods)
    df_raw = fetch(symbol, max_days)
    if df_raw is None:
        return None
    df = calc(df_raw)
    if len(df) < 50:
        return None
    period_results = {}
    period_trades  = {}
    for days in periods:
        trades = backtest_donchian(df, days)
        period_results[days] = _calc_period_stats(trades)
        period_trades[days]  = trades
    today_sig = _has_signal_today(df)
    last_close = float(df.iloc[-1]["close"]) if len(df) > 0 else 0.0
    result = dict(symbol=symbol, name=name, period_results=period_results, period_trades=period_trades, today_sig=today_sig, last_close=last_close)
    if bt_cache is not None:
        bt_cache[cache_key] = (mtime, result)
    return result


def _passes_watchlist_filter(period_results):
    for days, s in period_results.items():
        if s["n"] < WL_MIN_TRADES:
            return False
        if pd.isna(s["wr"]) or s["wr"] < WL_MIN_WR:
            return False
        pf = s["pf"]
        if pd.isna(pf) or (pf != float("inf") and pf < WL_MIN_PF):
            return False
    return True


def build_watchlist_html(candidates, periods):
    today_str = _TODAY.strftime("%Y-%m-%d")
    scan_dt   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    periods_s = sorted(periods)
    period_headers    = "".join(f'<th colspan="4">{d}日</th>' for d in periods_s)
    period_subheaders = "".join('<th>回数</th><th>勝率</th><th>PF</th><th>損益(円)</th>' for _ in periods_s)

    rows = ""
    for c in candidates:
        pr  = c["period_results"]
        sig = c["today_sig"]
        sig_mark = "★" if sig else ""
        sym_id = c["symbol"].replace(".", "_").replace("-", "_")
        period_cells = ""
        for d in periods_s:
            s    = pr.get(d, {})
            n    = s.get("n", 0)
            wr   = s.get("wr", float("nan"))
            pf   = s.get("pf", float("nan"))
            wr_s = f"{wr:.0f}%" if not pd.isna(wr) else "—"
            pf_s = ("∞" if pf == float("inf") else f"{pf:.2f}") if not pd.isna(pf) else "—"
            wr_cls = "pos" if (not pd.isna(wr) and wr >= WL_MIN_WR) else "neg"
            pf_cls = "pos" if (pf == float("inf") or (not pd.isna(pf) and pf >= WL_MIN_PF)) else "neg"
            total   = s.get("total", 0.0)
            tot_cls = "pos" if total >= 0 else "neg"
            tot_s   = f"{total:+,.0f}" if n > 0 else "—"
            period_cells += f'<td>{n}</td><td class="{wr_cls}">{wr_s}</td><td class="{pf_cls}">{pf_s}</td><td class="{tot_cls}">{tot_s}</td>'
        cl_s = f'{sig["close"]:,.0f}' if sig else "—"
        lp_s = f'{sig["limit_price"]:,.0f}' if sig else "—"
        st_s = f'{sig["stop"]:,.0f}' if sig else "—"
        rows += (
            f'<tr class="sym-row" onclick="toggleDetail(this)">'
            f'<td>▶\u00a0{sig_mark}{c["symbol"]}</td><td>{c["name"]}</td>'
            f'<td>{cl_s}</td><td class="pos">{lp_s}</td><td class="neg">{st_s}</td>'
            + period_cells + f'</tr>\n'
        )

        pt = c.get("period_trades", {})
        sections = ""
        if pt:
            for d in periods_s:
                trades = pt.get(d, [])
                if not trades:
                    continue
                total_pnl = sum(t["pnl"] for t in trades)
                wins      = [t for t in trades if t["pnl"] > 0]
                tc_cls    = "pos" if total_pnl >= 0 else "neg"
                t_rows = ""
                for i, t in enumerate(trades, 1):
                    cls   = "win" if t["pnl"] > 0 else ("hold" if "保有中" in t.get("reason","") else "lose")
                    lp    = t.get("limit_p", t.get("limit_price", t["entry_p"]))
                    sl    = t.get("stop_p", float("nan"))
                    tgt   = t.get("target_p", float("nan"))
                    sl_s  = f'{sl:,.0f}'  if not pd.isna(sl)  else "—"
                    tgt_s = f'{tgt:,.0f}' if not pd.isna(tgt) else "—"
                    t_rows += (
                        f'<tr class="{cls}"><td>{i}</td>'
                        f'<td>{t["entry_dt"].strftime("%m/%d")}</td>'
                        f'<td>{t["exit_dt"].strftime("%m/%d")}</td>'
                        f'<td>{lp:,.0f}</td>'
                        f'<td>{t["entry_p"]:,.0f}</td>'
                        f'<td class="neg">{sl_s}</td>'
                        f'<td class="pos">{tgt_s}</td>'
                        f'<td>{t["exit_p"]:,.0f}</td>'
                        f'<td class="{"pos" if t["pct"]>=0 else "neg"}">{t["pct"]:+.1f}%</td>'
                        f'<td class="{"pos" if t["pnl"]>=0 else "neg"}">{t["pnl"]:+,.0f}円</td>'
                        f'<td>{t["hold"]}日</td>'
                        f'<td>{t.get("reason","")}</td></tr>\n'
                    )
                sections += f"""
<h3 style="margin:14px 0 6px;font-size:0.95em;color:#94a3b8">{d}日間
  <span style="color:#666;font-size:0.85em">
    {len(trades)}回 / 勝:{len(wins)} / 損益:<span class="{tc_cls}">{total_pnl:+,.0f}円</span>
  </span></h3>
<table><thead><tr>
  <th>#</th><th>IN</th><th>OUT</th>
  <th>指値</th><th>約定値</th><th>逆指値</th><th>利確目標</th><th>決済値</th>
  <th>損益%</th><th>損益(円)</th><th>保有</th><th>理由</th>
</tr></thead><tbody>{t_rows}</tbody></table>"""
        rows += (
            f'<tr class="detail-row" style="display:none">'
            f'<td colspan="99"><div class="detail-inner">'
            f'<h2 style="font-size:1.05em;color:#e2e8f0;margin-bottom:4px">{c["symbol"]}'
            f' <span style="color:#94a3b8;font-size:0.85em;font-weight:400">{c["name"]}</span>'
            f'</h2>{sections}</div></td></tr>\n'
        )

    signal_count = sum(1 for c in candidates if c["today_sig"])

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>監視銘柄リスト（ドンチャン） {today_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,sans-serif;background:#0f1117;color:#dde1ec;padding:24px;font-size:13px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}}
.meta{{color:#555;font-size:0.8em;margin:2px 0 16px 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0 24px}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:12px 18px;min-width:120px}}
.clabel{{font-size:0.7em;color:#666}}.cval{{font-size:1.4em;font-weight:700;margin-top:2px}}
.criteria{{background:#131825;border:1px solid #1e3a5f;border-radius:8px;padding:12px 18px;margin:0 0 20px;font-size:0.85em;color:#7ab3d4}}
.criteria b{{color:#38bdf8}}
table{{width:100%;border-collapse:collapse;font-size:0.83em}}
th{{background:#16192a;color:#666;padding:7px 10px;text-align:right;border-bottom:2px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3),th:nth-child(4),th:nth-child(5){{text-align:left}}
th[colspan]{{text-align:center;border-left:1px solid #2a2f4a;color:#94a3b8}}
td{{padding:6px 10px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3),td:nth-child(4),td:nth-child(5){{text-align:left}}
tr:hover>td{{background:#1b1f35}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.footer{{margin-top:28px;color:#333;font-size:0.75em;text-align:right}}
.sym-row{{cursor:pointer}}
.sym-row td:first-child{{user-select:none}}
.detail-row>td{{padding:0;background:#0d1020!important;border-bottom:2px solid #252840}}
.detail-inner{{padding:12px 20px}}
.detail-inner table{{width:auto;min-width:600px}}
.detail-inner th{{background:#0d1020}}
tr.win>td{{background:rgba(74,222,128,.05)}}
tr.lose>td{{background:rgba(248,113,113,.05)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
</style></head><body>
<h1>監視銘柄リスト — ドンチャン・ブレイクアウト戦略</h1>
<div class="meta">スキャン: {scan_dt} ／ 期間: {', '.join(str(d)+'日' for d in periods_s)}</div>
<div class="criteria">選定基準（全期間で満たすこと）：<b>取引回数 ≥ {WL_MIN_TRADES}回</b> ／ <b>勝率 ≥ {WL_MIN_WR:.0f}%</b> ／ <b>PF ≥ {WL_MIN_PF}</b></div>
<div class="cards">
  <div class="card"><div class="clabel">候補銘柄数</div><div class="cval pos">{len(candidates)}</div></div>
  <div class="card"><div class="clabel">本日シグナル</div><div class="cval pos">{signal_count}</div></div>
</div>
<table><thead>
<tr><th rowspan="2">コード</th><th rowspan="2">銘柄名</th>
<th rowspan="2">終値</th><th rowspan="2">指値</th><th rowspan="2">損切り</th>
{period_headers}</tr>
<tr>{period_subheaders}</tr>
</thead><tbody>{rows}</tbody></table>
<div class="footer">★ = 本日シグナルあり</div>
<script>
function toggleDetail(row){{
  var next=row.nextElementSibling;
  if(!next||!next.classList.contains('detail-row'))return;
  var open=next.style.display==='table-row';
  next.style.display=open?'none':'table-row';
  var td=row.querySelector('td');
  if(td)td.textContent=td.textContent.replace(/^[▶▼]\u00a0/,''+(open?'▶\u00a0':'▼\u00a0'));
}}
</script>
</body></html>"""
    out = Path(f"watchlist_donchian_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    return out


# ── メイン処理 ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ドンチャン・ブレイクアウト押し目指値 バックテスト & シグナルスキャン",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_donchian_pullback.py                     # 監視銘柄スキャン（1年）
  python backtest_donchian_pullback.py --universe all      # 全銘柄スキャン
  python backtest_donchian_pullback.py --symbol 7203.T     # 1銘柄詳細
  python backtest_donchian_pullback.py --days 730          # 730日バックテスト
  python backtest_donchian_pullback.py --no-browser        # ブラウザ自動起動なし
""")
    parser.add_argument("--symbol",     type=str,  default=None,
                        help="銘柄コード（例: 7203.T）省略時はユニバース全体スキャン")
    parser.add_argument("--days",       type=int,  default=BACKTEST_DAYS,
                        help=f"バックテスト日数（デフォルト: {BACKTEST_DAYS}）")
    parser.add_argument("--universe",   type=str,  default="watch",
                        choices=["watch", "225", "all"],
                        help="銘柄ユニバース: watch（監視銘柄）/ 225（日経225）/ all（全上場銘柄）")
    parser.add_argument("--no-browser", action="store_true",
                        help="HTMLレポートをブラウザで自動起動しない")
    parser.add_argument("--watchlist", action="store_true", help="4期間(30/90/180/365日)で監視銘柄を選定")
    args = parser.parse_args()

    backtest_days = args.days
    since_str     = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today_str     = _TODAY.strftime("%Y-%m-%d")

    # --universe 225/all の場合、--symbol 未指定なら自動で4期間バックテストモードへ
    if not args.watchlist and args.universe in ("225", "all") and not args.symbol:
        args.watchlist = True

    if args.watchlist:
        symbols = _load_symbols(args.universe)
        periods      = WATCHLIST_PERIODS   # 常に全期間でバックテスト（キャッシュ安定化）
        show_periods = [p for p in WATCHLIST_PERIODS if p <= args.days] or [args.days]
        bt_cache = _load_bt_cache()
        cached_count = sum(1 for sym, _ in symbols if (sym, tuple(sorted(periods))) in bt_cache)
        print(f"\nバックテスト実行: {len(symbols)}銘柄 / 期間:{periods}日 / 表示:{show_periods}日")
        if cached_count:
            print(f"  キャッシュ: {cached_count}銘柄（株価更新なし → スキップ）")
        print(f"  データ取得・バックテスト中 (並列{WORKERS}スレッド) ...")
        all_results = []
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_process_symbol_multiperiod, sym, name, periods, bt_cache): (sym, name)
                    for sym, name in symbols}
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                    if r:
                        all_results.append(r)
                except Exception:
                    pass
                if done % 20 == 0 or done == len(symbols):
                    print(f"  {done}/{len(symbols)} 完了", end="\r", flush=True)
        _save_bt_cache(bt_cache)
        print()
        # 表示対象期間に取引があった銘柄のみ・表示期間の損益順でソート
        candidates = [r for r in all_results if any(r["period_results"].get(d, {}).get("n", 0) > 0 for d in show_periods)]
        candidates.sort(key=lambda r: (-sum(r["period_results"].get(d, {}).get("total", 0) for d in show_periods), r["symbol"]))
        print(f"\nスキャン結果（利益順）: {len(candidates)}銘柄")
        for c in candidates:
            sig  = c["today_sig"]
            mark = "★" if sig else "  "
            pr   = c["period_results"]
            stats_str = "  ".join(
                f"{d}日:勝率{pr[d]['wr']:.0f}%/PF{pr[d]['pf']:.1f}" if pr[d]['n'] > 0 else f"{d}日:—"
                for d in sorted(show_periods)
            )
            print(f"  {mark} {c['symbol']:12} {c['name']:20}  {stats_str}")
        path = build_watchlist_html(candidates, show_periods)
        print(f"\nHTML: {path.resolve()}")
        # CSV出力（run_ranking.py用）
        import csv as _csv
        _csv_path = Path(f"candidates_donchian_pullback.csv")
        with open(_csv_path, "w", newline="", encoding="utf-8") as _f:
            _w = _csv.writer(_f)
            _w.writerow(["symbol","name","last_close",
                         "30_n","30_wr","30_pf","30_total",
                         "90_n","90_wr","90_pf","90_total",
                         "180_n","180_wr","180_pf","180_total",
                         "365_n","365_wr","365_pf","365_total"])
            for _c in candidates:
                _pr = _c["period_results"]
                _row = [_c["symbol"], _c["name"], _c.get("last_close", 0)]
                for _d in [30, 90, 180, 365]:
                    _s = _pr.get(_d, {})
                    _pf = _s.get("pf", 0) or 0
                    _row += [_s.get("n", 0),
                             round(_s.get("wr", 0) or 0, 1),
                             round(0 if _pf == float("inf") else _pf, 4),
                             round(_s.get("total", 0) or 0, 0)]
                _w.writerow(_row)
        print(f"CSV: {_csv_path.resolve()}")
        if not args.no_browser:
            open_html(f"file://{path.resolve()}")
        return

    # ── 1銘柄モード ──────────────────────────────────────────
    if args.symbol:
        sym = args.symbol.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        name = sym
        print(f"\n  データ取得中: {sym} ...")
        df = fetch(sym, backtest_days)
        if df is None:
            print(f"  エラー: {sym} のデータ取得に失敗しました\n")
            return
        df     = calc(df)
        trades = backtest_donchian(df, backtest_days)
        sig    = _has_signal_today(df)

        wins  = [t for t in trades if t["pnl"] > 0]
        loss  = [t for t in trades if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trades)
        wr    = len(wins) / len(trades) * 100 if trades else 0
        pf_v  = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                 if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
        pf_s  = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"

        print(f"\n  [{sym}]  {since_str} 〜 {today_str}  ({backtest_days}日)")
        print(f"  トレード: {len(trades)}回  勝率: {wr:.1f}%  PF: {pf_s}  損益: {total:+,.0f}円")
        print()
        for i, t in enumerate(trades, 1):
            mark = "★" if "保有中" in t.get("reason", "") else " "
            print(f" {mark}{i:3d}  "
                  f"{t['entry_dt'].strftime('%Y-%m-%d')} → "
                  f"{t['exit_dt'].strftime('%Y-%m-%d')}  "
                  f"指値:{t['limit_p']:,.0f}  "
                  f"約定:{t['entry_p']:,.0f}  "
                  f"決済:{t['exit_p']:,.0f}  "
                  f"{t['pct']:+.2f}%  "
                  f"{t['hold']}日  "
                  f"{t.get('reason','')}")

        if sig:
            print(f"\n  ★ 本日シグナルあり!")
            print(f"     終値: {sig['close']:,.0f}  N日高値: {sig['don_high']:,.0f}")
            print(f"     指値: {sig['limit_price']:,.0f}  "
                  f"ストップ: {sig['stop']:,.0f}  "
                  f"利確: {sig['target']:,.0f}")
            print(f"     出来高比: {sig['vol_ratio']:.2f}x" if not pd.isna(sig['vol_ratio']) else "")
        else:
            print(f"\n  本日シグナルなし")

        result_entry = dict(
            symbol      = sym,
            name        = name,
            trades_list = trades,
            trade_count = len(trades),
            win_rate    = wr,
            pf          = pf_v,
            total       = total,
            total_pct   = sum(t["pct"] for t in trades),
            today_sig   = sig,
        )
        sigs_list = []
        if sig:
            s2 = dict(sig)
            s2["symbol"] = sym
            s2["name"]   = name
            sigs_list.append(s2)

        path = build_html(sigs_list, [result_entry], backtest_days, "single")
        print(f"\n  HTMLレポート保存: {path}")
        if not args.no_browser:
            open_html(f"file://{path.resolve()}")
        return

    # ── スキャンモード ───────────────────────────────────────
    symbols = _load_symbols(args.universe)
    print(f"\n  ドンチャン・ブレイクアウトスキャン  {len(symbols)}銘柄  {since_str} 〜 {today_str}")
    print(f"  データ取得・バックテスト中 (並列{WORKERS}スレッド) ...")

    scan_results: list = []
    done_count = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(_process_symbol, sym, name, backtest_days): (sym, name)
            for sym, name in symbols
        }
        for fut in as_completed(futures):
            done_count += 1
            sym, name = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                print(f"  エラー [{sym}]: {e}")
                result = None
            if result is None:
                pass
            else:
                scan_results.append(result)
            if done_count % 20 == 0 or done_count == len(symbols):
                print(f"  {done_count}/{len(symbols)} 完了...", end="\r", flush=True)
    print()

    signals: list = []
    for r in scan_results:
        if r["today_sig"] is not None:
            s = dict(r["today_sig"])
            s["symbol"] = r["symbol"]
            s["name"]   = r["name"]
            signals.append(s)

    # シグナルを出来高比降順でソート
    signals.sort(key=lambda x: -x.get("vol_ratio", 0) if not pd.isna(x.get("vol_ratio", 0)) else 0)

    total_pnl    = sum(r.get("total", 0)       for r in scan_results)
    total_trades = sum(r.get("trade_count", 0) for r in scan_results)
    plus_count   = sum(1 for r in scan_results if r.get("total", 0) > 0)

    print(f"\n  スキャン完了: {len(scan_results)}/{len(symbols)}銘柄")
    print(f"  トレード合計: {total_trades}回  プラス銘柄: {plus_count}/{len(scan_results)}")
    print(f"  全損益合計: {total_pnl:+,.0f}円")
    print(f"  本日シグナル: {len(signals)}件\n")

    for s in signals:
        vr_s = f"{s['vol_ratio']:.2f}x" if not pd.isna(s.get('vol_ratio', float('nan'))) else ""
        print(f"    {s['symbol']:8s} {s['name'][:12]:<12}  "
              f"終値{s['close']:>8,.0f}  N高値{s['don_high']:>8,.0f}  "
              f"指値{s['limit_price']:>8,.0f}  {vr_s}")

    path = build_html(signals, scan_results, backtest_days, args.universe)
    print(f"\n  レポート出力: {path}")
    if not args.no_browser:
        open_html(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
