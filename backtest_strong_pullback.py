"""
強い株の押し目 (Strong Stock Pullback) バックテスト & シグナルスキャン
────────────────────────────────────────────────────────
■ 戦略概要
  1. シグナル判定（終値ベース）
     - 終値 > MA200（長期上昇トレンド）
     - MA50 > MA200（中長期上昇）
     - MA50 > MA50[20日前]（中期上昇継続）
     - RSI(5) < 40（短期過売り）
     - 終値 < MA50（押し目）
     - ROC(60) > 0（中期モメンタム陽）

  2. 指値エントリー（翌日から有効）
     - 指値価格 = 終値 - ATR × ENTRY_ATR_MULT (0.5)
     - 有効日数 = ENTRY_EXPIRE 日（未成立でキャンセル）

  3. OCO決済
     - 利確指値   : エントリー価格 + ATR × TARGET_ATR_MULT (4.0)
     - 損切り逆指値: エントリー価格 - ATR × STOP_ATR_MULT (2.0)
     - 最大保有日数: MAX_HOLD 日（超過で終値決済）

■ 実行方法
  python backtest_strong_pullback.py                   # 全銘柄スキャン
  python backtest_strong_pullback.py --symbol 7203.T   # 個別銘柄詳細
  python backtest_strong_pullback.py --days 730        # バックテスト期間変更
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
_BT_CACHE       = _CACHE_DIR / "bt_results_strong_pullback.pkl"

RSI_PERIOD      = 5      # RSI期間
MA_SHORT        = 50     # 短期MA期間
MA_LONG         = 200    # 長期MA期間
MA_LOOKBACK     = 20     # MA50上昇確認期間
ROC_PERIOD      = 60     # ROC計算期間
ENTRY_ATR_MULT  = 0.5    # 指値オフセット（ATR倍率）
STOP_ATR_MULT   = 2.0    # ストップ（ATR倍率）
TARGET_ATR_MULT = 4.0    # 利確（ATR倍率）
ENTRY_EXPIRE    = 3      # 指値有効日数
MAX_HOLD        = 15     # 最大保有日数

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
def calc(df):
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]
    prev = c.shift(1)

    # MA200, MA50
    df["ma200"] = c.rolling(MA_LONG).mean()
    df["ma50"]  = c.rolling(MA_SHORT).mean()

    # ATR(14)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=13, adjust=False).mean()

    # RSI(5)
    delta = c.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    df["rsi5"] = 100 - 100 / (1 + rs)

    # ROC(60)
    df["roc60"] = (c / c.shift(ROC_PERIOD) - 1) * 100

    # Signal: all 6 conditions
    ma50 = df["ma50"]
    df["signal"] = (
        (c > df["ma200"]) &
        (ma50 > df["ma200"]) &
        (ma50 > ma50.shift(MA_LOOKBACK)) &
        (df["rsi5"] < 40) &
        (c < ma50) &
        (df["roc60"] > 0)
    )

    return df.dropna(subset=["ma200"])



# ── バックテスト（強い株の押し目）────────────────────────────────────
def backtest_strong_pullback(df: pd.DataFrame, backtest_days: int) -> list:
    """
    強い株の押し目 + 押し目指値エントリー バックテスト。

    エントリー: シグナル翌日から limit = close - ATR * ENTRY_ATR_MULT の指値
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
    pending_order = None  # dict: {limit_price, atr, placed_date}

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
        if not prev.get("signal", False):
            continue
        prev_atr = float(prev.get("atr", float("nan")))
        if pd.isna(prev_atr) or prev_atr <= 0:
            continue

        limit_price = float(prev["close"]) - float(prev["atr"]) * ENTRY_ATR_MULT
        pending_order = {"limit_price": limit_price, "atr": float(prev["atr"]), "placed_date": current_date,
                         "signal_dt": prev.name, "signal_close": float(prev["close"])}

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
    最終バーのシグナルを判定する。
    シグナルあり → 注文情報dictを返す。
    シグナルなし → None を返す。
    """
    if len(df) < 2:
        return None
    last = df.iloc[-1]

    if not bool(last.get("signal", False)):
        return None

    atr   = float(last.get("atr",   float("nan")))
    if pd.isna(atr) or atr <= 0:
        return None

    return dict(
        close=float(last["close"]),
        limit_price=float(last["close"]) - float(last["atr"]) * ENTRY_ATR_MULT,
        stop=float(last["close"]) - float(last["atr"]) * ENTRY_ATR_MULT - STOP_ATR_MULT * float(last["atr"]),
        target=float(last["close"]) - float(last["atr"]) * ENTRY_ATR_MULT + TARGET_ATR_MULT * float(last["atr"]),
        atr=float(last["atr"]),
        rsi5=float(last["rsi5"]),
    )


# ── 統計計算 ─────────────────────────────────────────────────────
def _calc_period_stats(trades):
    if not trades:
        return dict(n=0, wr=float("nan"), pf=float("nan"), total=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    loss = [t for t in trades if t["pnl"] <= 0]
    wr   = len(wins) / len(trades) * 100
    ls   = abs(sum(t["pnl"] for t in loss))
    pf   = sum(t["pnl"] for t in wins) / ls if ls > 0 else float("inf")
    return dict(n=len(trades), wr=wr, pf=pf, total=sum(t["pnl"] for t in trades))


# ── キャッシュ操作 ────────────────────────────────────────────────
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


# ── マルチピリオド処理 ────────────────────────────────────────────
def _process_symbol_multiperiod(symbol, name, periods, bt_cache=None):
    mtime     = _price_cache_mtime(symbol)
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
        trades = backtest_strong_pullback(df, days)
        period_results[days] = _calc_period_stats(trades)
        period_trades[days]  = trades
    today_sig  = _has_signal_today(df)
    last_close = float(df.iloc[-1]["close"]) if len(df) > 0 else 0.0
    result = dict(symbol=symbol, name=name, period_results=period_results,
                  period_trades=period_trades, today_sig=today_sig, last_close=last_close)

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


# ── HTML生成 ──────────────────────────────────────────────────────
def build_watchlist_html(candidates, periods):
    today_str = datetime.now(JST).strftime('%Y-%m-%d')
    scan_dt   = datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')
    periods_s = sorted(periods)
    period_headers    = ''.join(f'<th colspan="4">{d}日</th>' for d in periods_s)
    period_subheaders = ''.join('<th>回数</th><th>勝率</th><th>PF</th><th>損益(円)</th>' for _ in periods_s)

    rows = ''
    for c in candidates:
        pr  = c['period_results']
        sig = c['today_sig']
        sig_mark = '★' if sig else ''
        period_cells = ''
        for d in periods_s:
            s    = pr.get(d, {})
            n    = s.get('n', 0)
            wr   = s.get('wr', float('nan'))
            pf   = s.get('pf', float('nan'))
            wr_s = f'{wr:.0f}%' if not pd.isna(wr) else '—'
            pf_s = ('∞' if pf == float('inf') else f'{pf:.2f}') if not pd.isna(pf) else '—'
            wr_cls  = 'pos' if (not pd.isna(wr) and wr >= WL_MIN_WR) else 'neg'
            pf_cls  = 'pos' if (pf == float('inf') or (not pd.isna(pf) and pf >= WL_MIN_PF)) else 'neg'
            total   = s.get('total', 0.0)
            tot_cls = 'pos' if total >= 0 else 'neg'
            tot_s   = f'{total:+,.0f}' if n > 0 else '—'
            period_cells += f'<td>{n}</td><td class="{wr_cls}">{wr_s}</td><td class="{pf_cls}">{pf_s}</td><td class="{tot_cls}">{tot_s}</td>'
        cl_s = f'{sig["close"]:,.0f}' if sig else '—'
        lp_s = f'{sig["limit_price"]:,.0f}' if sig else '—'
        st_s = f'{sig["stop"]:,.0f}' if sig else '—'
        rows += (
            f'<tr class="sym-row" onclick="toggleDetail(this)">'
            f'<td>▶\u00a0{sig_mark}{c["symbol"]}</td><td>{c["name"]}</td>'
            f'<td>{cl_s}</td><td class="pos">{lp_s}</td><td class="neg">{st_s}</td>'
            + period_cells + '</tr>\n'
        )

        pt = c.get('period_trades', {})
        sections = ''
        if pt:
            for d in periods_s:
                trades = pt.get(d, [])
                if not trades:
                    continue
                total_pnl = sum(t['pnl'] for t in trades)
                wins      = [t for t in trades if t['pnl'] > 0]
                tc_cls    = 'pos' if total_pnl >= 0 else 'neg'
                t_rows = ''
                for i, t in enumerate(trades, 1):
                    cls   = 'win' if t['pnl'] > 0 else ('hold' if '保有中' in t['reason'] else 'lose')
                    lp    = t.get('limit_p',  t['entry_p'])
                    sl    = t.get('stop_p',   float('nan'))
                    tgt   = t.get('target_p', float('nan'))
                    sl_s  = f'{sl:,.0f}'  if not pd.isna(sl)  else '—'
                    tgt_s = f'{tgt:,.0f}' if not pd.isna(tgt) else '—'
                    pct_cls = 'pos' if t['pct'] >= 0 else 'neg'
                    pnl_cls = 'pos' if t['pnl'] >= 0 else 'neg'
                    t_rows += (
                        f'<tr class="{cls}"><td>{i}</td>'
                        f'<td>{t["entry_dt"].strftime("%m/%d")}</td>'
                        f'<td>{t["exit_dt"].strftime("%m/%d")}</td>'
                        f'<td>{lp:,.0f}</td>'
                        f'<td>{t["entry_p"]:,.0f}</td>'
                        f'<td class="neg">{sl_s}</td>'
                        f'<td class="pos">{tgt_s}</td>'
                        f'<td>{t["exit_p"]:,.0f}</td>'
                        f'<td class="{pct_cls}">{t["pct"]:+.1f}%</td>'
                        f'<td class="{pnl_cls}">{t["pnl"]:+,.0f}円</td>'
                        f'<td>{t["hold"]}日</td>'
                        f'<td>{t["reason"]}</td></tr>\n'

                    )
                n_trades = len(trades)
                n_wins   = len(wins)
                sections += (
                    f'<h3 style="margin:14px 0 6px;font-size:0.95em;color:#94a3b8">{d}日間'
                    f'  <span style="color:#666;font-size:0.85em">'
                    f'    {n_trades}回 / 勝:{n_wins} / 損益:<span class="{tc_cls}">{total_pnl:+,.0f}円</span>'
                    f'  </span></h3>'
                    f'<table><thead><tr>'
                    f'  <th>#</th><th>IN</th><th>OUT</th>'
                    f'  <th>指値</th><th>約定値</th><th>逆指値</th><th>利確目標</th><th>決済値</th>'
                    f'  <th>損益%</th><th>損益(円)</th><th>保有</th><th>理由</th>'
                    f'</tr></thead><tbody>{t_rows}</tbody></table>'
                )
        sig_info = ''
        if sig:
            sig_info = (
                f'<div style="margin:4px 0 12px;font-size:0.85em;color:#94a3b8">'
                f'終値: <b class="neu">{sig["close"]:,.0f}</b> ／ '
                f'指値: <b class="pos">{sig["limit_price"]:,.0f}</b> ／ '
                f'損切り: <b class="neg">{sig["stop"]:,.0f}</b>'
                f'</div>'
            )
        rows += (
            f'<tr class="detail-row" style="display:none">'
            f'<td colspan="99"><div class="detail-inner">{sig_info}{sections}</div></td></tr>\n'
        )

    signal_count = sum(1 for c in candidates if c['today_sig'])
    periods_str  = ', '.join(str(d) + '日' for d in periods_s)
    css = (
        '*{box-sizing:border-box;margin:0;padding:0}'
        'body{font-family:\'Helvetica Neue\',Arial,sans-serif;background:#0f1117;color:#dde1ec;padding:24px;font-size:13px}'
        'h1{font-size:1.35em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}'
        '.meta{color:#555;font-size:0.8em;margin:2px 0 16px 16px}'
        '.cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0 24px}'
        '.card{background:#16192a;border:1px solid #252840;border-radius:10px;padding:12px 18px;min-width:120px}'
        '.clabel{font-size:0.7em;color:#666}.cval{font-size:1.4em;font-weight:700;margin-top:2px}'
        '.criteria{background:#131825;border:1px solid #1e3a5f;border-radius:8px;padding:12px 18px;margin:0 0 20px;font-size:0.85em;color:#7ab3d4}'
        '.criteria b{color:#38bdf8}'
        'table{width:100%;border-collapse:collapse;font-size:0.83em}'
        'th{background:#16192a;color:#666;padding:7px 10px;text-align:right;border-bottom:2px solid #252840;white-space:nowrap}'
        'th:first-child,th:nth-child(2),th:nth-child(3),th:nth-child(4),th:nth-child(5){text-align:left}'
        'th[colspan]{text-align:center;border-left:1px solid #2a2f4a;color:#94a3b8}'
        'td{padding:6px 10px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}'
        'td:first-child,td:nth-child(2),td:nth-child(3),td:nth-child(4),td:nth-child(5){text-align:left}'
        'tr:hover>td{background:#1b1f35}'
        '.pos{color:#4ade80}.neg{color:#f87171}.neu{color:#c8cfe8}'
        '.footer{margin-top:28px;color:#333;font-size:0.75em;text-align:right}'
        '.sym-row{cursor:pointer}'
        '.sym-row td:first-child{user-select:none}'
        '.detail-row>td{padding:0;background:#0d1020!important;border-bottom:2px solid #252840}'
        '.detail-inner{padding:12px 20px}'
        '.detail-inner table{width:auto;min-width:600px}'
        '.detail-inner th{background:#0d1020}'
        'tr.win>td{background:rgba(74,222,128,.05)}'
        'tr.lose>td{background:rgba(248,113,113,.05)}'
        'tr.hold>td{background:rgba(251,191,36,.06)}'
    )

    js = (
        "function toggleDetail(row){"
        "var next=row.nextElementSibling;"
        "if(!next||!next.classList.contains('detail-row'))return;"
        "var open=next.style.display==='table-row';"
        "next.style.display=open?'none':'table-row';"
        "var td=row.querySelector('td');"
        "if(td)td.textContent=td.textContent.replace(/^[\u25b6\u25bc]\u00a0/,''+(open?'\u25b6\u00a0':'\u25bc\u00a0'));"
        "}"
    )

    crit = (
        f'選定基準（全期間で満たすこと）：<b>取引回数 ≥ {WL_MIN_TRADES}回</b>'
        f' ／ <b>勝率 ≥ {WL_MIN_WR:.0f}%</b> ／ <b>PF ≥ {WL_MIN_PF}</b>'
    )
    n_cand = len(candidates)

    html = (
        '<!DOCTYPE html>\n'
        '<html lang="ja"><head><meta charset="UTF-8">\n'
        f'<title>監視銘柄リスト（強い株の押し目） {today_str}</title>\n'
        f'<style>{css}</style></head><body>\n'
        '<h1>監視銘柄リスト — 強い株の押し目戦略</h1>\n'
        f'<div class="meta">スキャン: {scan_dt} ／ 期間: {periods_str}</div>\n'
        f'<div class="criteria">{crit}</div>\n'
        '<div class="cards">\n'
        f'  <div class="card"><div class="clabel">候補銘柄数</div><div class="cval pos">{n_cand}</div></div>\n'
        f'  <div class="card"><div class="clabel">本日シグナル</div><div class="cval pos">{signal_count}</div></div>\n'
        '</div>\n'
        '<table><thead>\n'
        '<tr><th rowspan="2">コード</th><th rowspan="2">銘柄名</th>\n'
        '<th rowspan="2">終値</th><th rowspan="2">指値</th><th rowspan="2">損切り</th>\n'
        f'{period_headers}</tr>\n'
        f'<tr>{period_subheaders}</tr>\n'
        f'</thead><tbody>{rows}</tbody></table>\n'
        '<div class="footer">★ = 本日シグナルあり</div>\n'
        f'<script>{js}</script>\n'
        '</body></html>'
    )
    out = Path(f'watchlist_strong_pullback_{today_str}.html')
    out.write_text(html, encoding='utf-8')
    return out

# ── メイン処理 ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="強い株の押し目戦略 バックテスト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_strong_pullback.py                     # 監視銘柄スキャン（1年）
  python backtest_strong_pullback.py --universe all      # 全銘柄スキャン
  python backtest_strong_pullback.py --symbol 7203.T     # 1銘柄詳細
  python backtest_strong_pullback.py --days 730          # 730日バックテスト
  python backtest_strong_pullback.py --no-browser        # ブラウザ自動起動なし
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
        periods = [p for p in WATCHLIST_PERIODS if p <= args.days] or [args.days]
        bt_cache = _load_bt_cache()
        cached_count = sum(1 for sym, _ in symbols if (sym, tuple(sorted(periods))) in bt_cache)
        print(f"\nバックテスト実行: {len(symbols)}銘柄 / 期間:{periods}日")
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
        # フィルターなし・全銘柄を利益順にソート
        candidates = [r for r in all_results if any(s["n"] > 0 for s in r["period_results"].values())]
        candidates.sort(key=lambda r: (-sum(s.get("total", 0) for s in r["period_results"].values()), r["symbol"]))
        print(f"\nスキャン結果（利益順）: {len(candidates)}銘柄")
        for c in candidates:
            sig  = c["today_sig"]
            mark = "★" if sig else "  "
            pr   = c["period_results"]
            stats_str = "  ".join(
                f"{d}日:勝率{pr[d]['wr']:.0f}%/PF{pr[d]['pf']:.1f}" if pr[d]['n'] > 0 else f"{d}日:—"
                for d in sorted(periods)
            )
            print(f"  {mark} {c['symbol']:12} {c['name']:20}  {stats_str}")
        path = build_watchlist_html(candidates, periods)
        print(f"\nHTML: {path.resolve()}")
        # CSV出力（run_ranking.py用）
        import csv as _csv
        _csv_path = Path(f"candidates_strong_pullback.csv")
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
            webbrowser.open(f"file://{path.resolve()}")
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
        trades = backtest_strong_pullback(df, backtest_days)
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
            last = df.iloc[-1]
            print(f"\n  ★ 本日シグナルあり!")
            print(f"     終値:    {sig['close']:,.0f}")
            print(f"     指値:    {sig['limit_price']:,.0f}  "
                  f"ストップ: {sig['stop']:,.0f}  "
                  f"利確: {sig['target']:,.0f}")
            print(f"     RSI5:   {sig['rsi5']:.1f}  "
                  f"MA50: {float(last['ma50']):,.0f}  "
                  f"MA200: {float(last['ma200']):,.0f}")
        else:
            print(f"\n  本日シグナルなし")

        return

    # ── スキャンモード ───────────────────────────────────────
    symbols = _load_symbols(args.universe)
    print(f"\n  強い株の押し目スキャン  {len(symbols)}銘柄  {since_str} 〜 {today_str}")
    print(f"  データ取得・バックテスト中 (並列{WORKERS}スレッド) ...")

    scan_results: list = []
    done_count = 0

    def _process_symbol(symbol: str, name: str, backtest_days: int):
        df = fetch(symbol, backtest_days)
        if df is None:
            return None
        df = calc(df)
        if len(df) < 50:
            return None

        trades     = backtest_strong_pullback(df, backtest_days)
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

    signals.sort(key=lambda x: x.get("limit_price", 0))

    total_pnl    = sum(r.get("total", 0)       for r in scan_results)
    total_trades = sum(r.get("trade_count", 0) for r in scan_results)
    plus_count   = sum(1 for r in scan_results if r.get("total", 0) > 0)

    print(f"\n  スキャン完了: {len(scan_results)}/{len(symbols)}銘柄")
    print(f"  トレード合計: {total_trades}回  プラス銘柄: {plus_count}/{len(scan_results)}")
    print(f"  全損益合計: {total_pnl:+,.0f}円")
    print(f"  本日シグナル: {len(signals)}件\n")

    for s in signals:
        print(f"    {s['symbol']:8s} {s['name'][:12]:<12}  "
              f"終値{s['close']:>8,.0f}  "
              f"指値{s['limit_price']:>8,.0f}  "
              f"RSI5:{s['rsi5']:.1f}")


if __name__ == "__main__":
    main()