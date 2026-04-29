"""
Statistical Pairs Trading Backtest for Japanese Equities
=========================================================
統計的ペアトレード バックテスト（共和分ペアトレード）

戦略概要:
  - 同一セクター内で連動するペアを発見
  - 一方が乖離した時（高Zスコア）に平均回帰を期待
  - 割高股のショート、割安股のロング

使い方:
  python backtest_pairs.py                    # 監視銀柄スキャン（1年）
  python backtest_pairs.py --universe all     # 日経225全銀柄スキャン
  python backtest_pairs.py --days 180         # 180日バックテスト
  python backtest_pairs.py --no-browser       # ブラウザ自動起動なし
"""

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import itertools
import pickle
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── 定数 ─────────────────────────────────────────────────────
WORKERS        = 4
POSITION_SIZE  = 100_000
BACKTEST_DAYS  = 365
JST            = timezone(timedelta(hours=9))
_TODAY         = pd.Timestamp(datetime.now(tz=JST).date())
_CACHE_DIR     = Path(".rsi2_cache")
Z_ENTRY        = 2.0
Z_STOP         = 3.5
Z_EXIT         = 0.2
MAX_HOLD       = 20
LOOKBACK       = 60
MIN_CORR       = 0.65


# ── 銘柄ユニバース ────────────────────────────────────────────
from symbols_watch_rsi2 import SYMBOLS as _WATCH_SYMBOLS

try:
    from rsi2 import SYMBOLS as _ALL_SYMBOLS
except ImportError:
    _ALL_SYMBOLS = _WATCH_SYMBOLS


def _load_symbols(universe: str) -> list[tuple[str, str]]:
    """ユニバース名に応じて銘柄リストを返す。"""
    if universe == "all":
        return list(_ALL_SYMBOLS)
    return list(_WATCH_SYMBOLS)


# ── セクター分類 ──────────────────────────────────────────────
def _guess_sector(symbol: str) -> str:
    """銘柄コードの数値範囲からセクターを推定する。"""
    code = symbol.replace(".T", "")
    if not code.isdigit():
        return "other"
    c = int(code)
    if 1000 <= c < 2000: return "mining"
    if 2000 <= c < 3000: return "food"
    if 3000 <= c < 4000: return "textile"
    if 4000 <= c < 5000: return "chemical"
    if 5000 <= c < 5600: return "glass_steel"
    if 5600 <= c < 6000: return "nonferrous"
    if 6000 <= c < 6400: return "machinery"
    if 6400 <= c < 6700: return "electric_parts"
    if 6700 <= c < 7000: return "electronics"
    if 7000 <= c < 7200: return "auto_heavy"
    if 7200 <= c < 7500: return "auto_parts"
    if 7500 <= c < 8000: return "precision"
    if 8000 <= c < 8300: return "finance"
    if 8300 <= c < 8700: return "bank_insurance"
    if 8700 <= c < 9000: return "realestate"
    if 9000 <= c < 9300: return "transport"
    if 9300 <= c < 9600: return "telecom"
    if 9600 <= c < 10000: return "services"
    return "other"


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
                with open(persistent, "rb") as fh:
                    df = pickle.load(fh)
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
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw.get("volume", pd.Series(0, index=raw.index)).to_numpy(dtype=float),
        }, index=raw.index)
    except Exception:
        return None


# ── ペア計算 ──────────────────────────────────────────────────
def calc_pair(df_a: pd.DataFrame, df_b: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    """
    2銘柄の共和分スプレッドとZスコアを計算する。

    返り値:
      DataFrame (columns: log_a, log_b, beta, spread, z, corr)
      インデックス = 共通日付
    """
    idx = df_a.index.intersection(df_b.index)
    if len(idx) < lookback + 10:
        return pd.DataFrame()

    log_a = np.log(df_a.loc[idx, "close"])
    log_b = np.log(df_b.loc[idx, "close"])

    # ローリングOLSヘッジ比率
    cov  = log_a.rolling(lookback).cov(log_b)
    var  = log_b.rolling(lookback).var()
    beta = cov / var.replace(0, np.nan)

    spread = log_a - beta * log_b
    z      = (spread - spread.rolling(lookback).mean()) / spread.rolling(lookback).std()
    corr   = log_a.rolling(lookback).corr(log_b)

    return pd.DataFrame({
        "log_a":  log_a,
        "log_b":  log_b,
        "beta":   beta,
        "spread": spread,
        "z":      z,
        "corr":   corr,
    }, index=idx)


def _is_valid_pair(pair_df: pd.DataFrame, min_corr: float = 0.65) -> bool:
    """ペアが有効かどうかをチェックする。"""
    if len(pair_df) < 250:
        return False

    # 直近 60 行の平均相関 > min_corr
    recent_corr = pair_df["corr"].iloc[-60:]
    if recent_corr.isna().all():
        return False
    if float(recent_corr.mean()) <= min_corr:
        return False

    # Z スコアが少なくとも 2 回ゼロクロスする（平均回帰を確認）
    z_recent  = pair_df["z"].iloc[-60:].dropna()
    if len(z_recent) < 10:
        return False
    z_arr     = z_recent.to_numpy()
    crossings = int(np.sum(z_arr[:-1] * z_arr[1:] < 0))
    if crossings < 2:
        return False

    return True


# ── ペアバックテスト ──────────────────────────────────────────
def backtest_pair(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    pair_df: pd.DataFrame,
    sym_a: str,
    sym_b: str,
    backtest_days: int,
) -> list[dict]:
    """
    ペアトレードバックテスト。

    エントリー:
      z >  2.0 かつ 前日 z < 2.0  → Aショート/Bロング  (direction = "-A/+B")
      z < -2.0 かつ 前日 z > -2.0 → Aロング/Bショート  (direction = "+A/-B")

    エグジット:
      |z| < 0.2 (中心回帰), |z| > 3.5 (ストップ), 最大 20 日

    P&L:
      ロング脚 pct = (exit - entry) / entry * 100
      ショート脚 pct = -(exit - entry) / entry * 100
      combined = (long_pct + short_pct) / 2
      pnl      = combined / 100 * POSITION_SIZE
    """
    cutoff = _TODAY - pd.Timedelta(days=backtest_days)

    # pair_df の Z スコアを cutoff 以降に絞る
    pf = pair_df[pair_df.index >= cutoff].copy()
    if len(pf) < 3:
        return []

    # 各日付で終値を引き当てるための close シリーズ
    close_a = df_a["close"].reindex(pf.index)
    close_b = df_b["close"].reindex(pf.index)

    trades: list[dict] = []

    # ステートマシン
    in_trade        = False
    direction       = ""     # "+A/-B" or "-A/+B"
    entry_dt        = None
    entry_close_a   = 0.0
    entry_close_b   = 0.0
    entry_z         = 0.0
    hold_days_count = 0

    z_arr  = pf["z"].to_numpy(dtype=float)
    dates  = pf.index
    ca_arr = close_a.to_numpy(dtype=float)
    cb_arr = close_b.to_numpy(dtype=float)

    for i in range(1, len(pf)):
        dt     = dates[i]
        z_cur  = z_arr[i]
        z_prev = z_arr[i - 1]
        cl_a   = ca_arr[i]
        cl_b   = cb_arr[i]

        if np.isnan(z_cur) or np.isnan(z_prev):
            continue
        if np.isnan(cl_a) or np.isnan(cl_b):
            continue

        # ── ポジションあり: エグジット判定 ────────────────────
        if in_trade:
            hold_days_count = (dt - entry_dt).days
            exit_reason     = None

            # 中心回帰: |z| < Z_EXIT (0.2)
            if abs(z_cur) < Z_EXIT:
                exit_reason = "ゼロクロス"

            # ストップロス: |z| > Z_STOP (3.5)
            elif abs(z_cur) > Z_STOP:
                exit_reason = "ストップ"

            # 最大保有日数
            elif hold_days_count >= MAX_HOLD:
                exit_reason = f"最大{MAX_HOLD}日"

            if exit_reason is not None:
                # P&L計算
                if direction == "+A/-B":
                    long_pct  = (cl_a - entry_close_a) / entry_close_a * 100
                    short_pct = -(cl_b - entry_close_b) / entry_close_b * 100
                else:  # -A/+B
                    long_pct  = (cl_b - entry_close_b) / entry_close_b * 100
                    short_pct = -(cl_a - entry_close_a) / entry_close_a * 100

                combined_pct = (long_pct + short_pct) / 2.0
                pnl          = combined_pct / 100.0 * POSITION_SIZE

                trades.append(dict(
                    entry_dt      = entry_dt,
                    exit_dt       = dt,
                    direction     = direction,
                    entry_z       = entry_z,
                    exit_z        = z_cur,
                    entry_price_a = entry_close_a,
                    entry_price_b = entry_close_b,
                    exit_price_a  = cl_a,
                    exit_price_b  = cl_b,
                    pnl           = pnl,
                    pct           = combined_pct,
                    hold          = hold_days_count,
                    reason        = exit_reason,
                ))
                in_trade = False
                continue

        # ── ポジションなし: エントリー判定 ────────────────────
        if not in_trade:
            # A が高すぎる: z が 2.0 を上突きした → Aショート/Bロング
            if z_cur > Z_ENTRY and z_prev <= Z_ENTRY:
                in_trade        = True
                direction       = "-A/+B"
                entry_dt        = dt
                entry_close_a   = cl_a
                entry_close_b   = cl_b
                entry_z         = z_cur
                hold_days_count = 0

            # A が安すぎる: z が -2.0 を下突きした → Aロング/Bショート
            elif z_cur < -Z_ENTRY and z_prev >= -Z_ENTRY:
                in_trade        = True
                direction       = "+A/-B"
                entry_dt        = dt
                entry_close_a   = cl_a
                entry_close_b   = cl_b
                entry_z         = z_cur
                hold_days_count = 0

    # 保有中ポジションを未決済として記録
    if in_trade:
        last_cl_a       = float(ca_arr[-1])
        last_cl_b       = float(cb_arr[-1])
        last_z          = float(z_arr[-1])
        hold_days_count = (dates[-1] - entry_dt).days

        if direction == "+A/-B":
            long_pct  = (last_cl_a - entry_close_a) / entry_close_a * 100
            short_pct = -(last_cl_b - entry_close_b) / entry_close_b * 100
        else:
            long_pct  = (last_cl_b - entry_close_b) / entry_close_b * 100
            short_pct = -(last_cl_a - entry_close_a) / entry_close_a * 100

        combined_pct = (long_pct + short_pct) / 2.0
        pnl          = combined_pct / 100.0 * POSITION_SIZE

        trades.append(dict(
            entry_dt      = entry_dt,
            exit_dt       = dates[-1],
            direction     = direction,
            entry_z       = entry_z,
            exit_z        = last_z,
            entry_price_a = entry_close_a,
            entry_price_b = entry_close_b,
            exit_price_a  = last_cl_a,
            exit_price_b  = last_cl_b,
            pnl           = pnl,
            pct           = combined_pct,
            hold          = hold_days_count,
            reason        = "保有中★",
        ))

    return trades


# ── ペア処理 ──────────────────────────────────────────────────
def _process_pair(
    sym_a: str,
    name_a: str,
    sym_b: str,
    name_b: str,
    backtest_days: int,
    min_corr: float = MIN_CORR,
) -> "dict | None":
    """
    1ペアのデータ取得・検定・バックテストを実行する（スレッドプール用）。
    """
    df_a = fetch(sym_a, backtest_days)
    if df_a is None:
        return None
    df_b = fetch(sym_b, backtest_days)
    if df_b is None:
        return None

    pair_df = calc_pair(df_a, df_b, lookback=LOOKBACK)
    if pair_df.empty:
        return None

    if not _is_valid_pair(pair_df, min_corr=min_corr):
        return None

    trades    = backtest_pair(df_a, df_b, pair_df, sym_a, sym_b, backtest_days)
    wins      = [t for t in trades if t["pnl"] > 0]
    losses    = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    total_pct = sum(t["pct"] for t in trades)
    win_rate  = len(wins) / len(trades) * 100 if trades else float("nan")
    pf_val    = (
        sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
        if losses and sum(t["pnl"] for t in losses) != 0
        else float("inf")
    )

    # 現在のシグナルの確認
    last_z    = float(pair_df["z"].iloc[-1])   if not pair_df.empty else float("nan")
    last_corr = float(pair_df["corr"].iloc[-1]) if not pair_df.empty else float("nan")
    today_sig = None
    if not np.isnan(last_z) and abs(last_z) > Z_ENTRY:
        if last_z > Z_ENTRY:
            sig_dir    = "-A/+B"
            sig_action = f"{sym_a}ショート / {sym_b}ロング"
        else:
            sig_dir    = "+A/-B"
            sig_action = f"{sym_a}ロング / {sym_b}ショート"
        today_sig = dict(
            sym_a     = sym_a,
            name_a    = name_a,
            sym_b     = sym_b,
            name_b    = name_b,
            direction = sig_dir,
            z         = last_z,
            corr      = last_corr,
            action    = sig_action,
        )

    return dict(
        sym_a       = sym_a,
        name_a      = name_a,
        sym_b       = sym_b,
        name_b      = name_b,
        corr        = last_corr,
        trade_count = len(trades),
        win_rate    = win_rate,
        pf          = pf_val,
        total       = total_pnl,
        total_pct   = total_pct,
        trades_list = trades,
        today_sig   = today_sig,
    )

# ── HTML レポート生成 ─────────────────────────────────────────
def build_html(
    signals: list[dict],
    pair_results: list[dict],
    backtest_days: int,
    universe: str,
) -> Path:
    """
    HTMLレポートを生成する。

    引数:
      signals      : 現在シグナルのリスト
      pair_results : 有効ペアのバックテスト結果
      backtest_days: バックテスト日数
      universe     : "watch" または "all"
    """
    today_str = _TODAY.strftime("%Y-%m-%d")
    since_str = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    gen_time  = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    # ── シグナルテーブル ─────────────────────────────────────
    signal_rows = ""
    for s in signals:
        sa     = s.get("sym_a",    "")
        na     = s.get("name_a",   "")
        sb     = s.get("sym_b",    "")
        nb     = s.get("name_b",   "")
        d      = s.get("direction","")
        z      = s.get("z",        float("nan"))
        corr   = s.get("corr",     float("nan"))
        action = s.get("action",   "")
        z_abs  = abs(z) if not np.isnan(z) else 0.0
        z_cls  = "neg" if z_abs > Z_STOP else ("pos" if z_abs > Z_ENTRY else "neu")
        corr_s = f"{corr:.3f}" if not np.isnan(corr) else "—"
        z_s    = f"{z:+.3f}"  if not np.isnan(z)    else "—"
        signal_rows += (
            "<tr>"
            f"<td><b>{sa}</b> / <b>{sb}</b>"
            f"<br><small style='color:#666'>{na} / {nb}</small></td>"
            f'<td class="{z_cls}">{d}</td>'
            f'<td class="{z_cls}">{z_s}</td>'
            f'<td class="neu">{corr_s}</td>'
            f"<td>{action}</td>"
            "</tr>\n"
        )

    if not signal_rows:
        signal_rows = '<tr><td colspan="5" style="text-align:center;color:#555;padding:20px">本日シグナルなし</td></tr>\n'

    # ── バックテスト結果ランキングテーブル ───────────────────
    ranked    = sorted(pair_results, key=lambda x: (-x.get("total_pct", 0), x["sym_a"]))
    scan_rows = ""
    for rank, r in enumerate(ranked, 1):
        wr_v   = r.get("win_rate",  float("nan"))
        pf_v   = r.get("pf",        float("nan"))
        tot    = r.get("total",     0)
        tp     = r.get("total_pct", 0.0)
        tr_n   = r.get("trade_count", 0)
        corr   = r.get("corr",      float("nan"))
        pcls   = "pos" if tot >= 0 else "neg"
        wr_s   = f"{wr_v:.1f}%" if not pd.isna(wr_v) else "—"
        pf_inf = pf_v == float("inf")
        pf_s   = "∞" if pf_inf else (f"{pf_v:.2f}" if not pd.isna(pf_v) else "—")
        corr_s = f"{corr:.3f}" if not pd.isna(corr) else "—"
        wr_cls = "pos" if (not pd.isna(wr_v) and wr_v >= 60) else "neu"
        pf_cls = "pos" if (pf_inf or (not pd.isna(pf_v) and pf_v >= 1.5)) else "neu"
        sig_mk = "★" if r.get("today_sig") else ""
        row_cl = "win" if tot >= 0 else "lose"
        scan_rows += (
            f'<tr class="{row_cl}">'
            f"<td>{rank}</td>"
            f"<td><b>{r['sym_a']}</b> / <b>{r['sym_b']}</b> {sig_mk}</td>"
            f'<td class="neu">{corr_s}</td>'
            f'<td class="{pcls}">{tot:+,.0f}円</td>'
            f'<td class="{pcls}">{tp:+.2f}%</td>'
            f'<td class="{wr_cls}">{wr_s}</td>'
            f'<td class="{pf_cls}">{pf_s}</td>'
            f"<td>{tr_n}</td>"
            "</tr>\n"
        )

    # ── トレード詳細セクション（シグナルペアのみ）────────────
    detail_sections = ""
    sig_pairs = {(s["sym_a"], s["sym_b"]) for s in signals}
    for r in pair_results:
        if (r["sym_a"], r["sym_b"]) not in sig_pairs:
            continue
        trades = r.get("trades_list", [])
        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total  = sum(t["pnl"] for t in trades)
        wr_val = len(wins) / len(trades) * 100 if trades else 0
        pf_val = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                  if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))
        pf_str = "∞" if pf_val == float("inf") else f"{pf_val:.2f}"
        tc_cls = "pos" if total >= 0 else "neg"
        wr_cls2 = "pos" if wr_val >= 60 else "neu"
        pf_cls2 = "pos" if (pf_val == float("inf") or pf_val >= 1.5) else "neu"
        corr_v  = r.get("corr", float("nan"))
        corr_s2 = f"{corr_v:.3f}" if not pd.isna(corr_v) else "—"

        trade_rows = ""
        for i, t in enumerate(trades, 1):
            cls       = "hold" if "保有中" in t.get("reason", "") else ("win" if t["pnl"] > 0 else "lose")
            pct_cls   = "pos" if t["pct"] >= 0 else "neg"
            ez_s      = f"{t['entry_z']:+.3f}"
            xz_s      = f"{t['exit_z']:+.3f}"
            trade_rows += (
                f'<tr class="{cls}">'
                f"<td>{i}</td>"
                f"<td>{t['entry_dt'].strftime('%Y-%m-%d')}</td>"
                f"<td>{t['exit_dt'].strftime('%Y-%m-%d')}</td>"
                f"<td>{t['direction']}</td>"
                f"<td>{ez_s}</td>"
                f"<td>{xz_s}</td>"
                f'<td class="{pct_cls}">{t["pct"]:+.2f}%</td>'
                f"<td>{t['hold']}日</td>"
                f"<td>{t.get('reason', '')}</td>"
                "</tr>\n"
            )

        detail_sections += (
            '<div class="detail-section">'
            f'<h2>{r["sym_a"]} / {r["sym_b"]}'
            f' &nbsp;<span class="name-label">{r["name_a"]} / {r["name_b"]}</span>'
            "</h2>"
            '<div class="detail-stats">'
            f"<span>トレード: <b>{len(trades)}</b></span>"
            f'<span>勝率: <b class="{wr_cls2}">{wr_val:.1f}%</b></span>'
            f'<span>PF: <b class="{pf_cls2}">{pf_str}</b></span>'
            f'<span>損益: <b class="{tc_cls}">{total:+,.0f}円</b></span>'
            f"<span>勝/負: <b>{len(wins)}/{len(losses)}</b></span>"
            f'<span>相関: <b class="neu">{corr_s2}</b></span>'
            "</div>"
            "<table>"
            "<thead><tr>"
            "<th>#</th><th>エントリー</th><th>エグジット</th>"
            "<th>方向</th><th>エントリーZ</th><th>エグジットZ</th>"
            "<th>変化率</th><th>保有日数</th><th>決済理由</th>"
            "</tr></thead>"
            f"<tbody>{trade_rows}</tbody>"
            "</table>"
            "</div>\n"
        )

    # ── サマリーカード用 ─────────────────────────────────────
    total_pnl    = sum(r.get("total", 0)       for r in pair_results)
    total_trades = sum(r.get("trade_count", 0) for r in pair_results)
    plus_count   = sum(1 for r in pair_results if r.get("total", 0) > 0)
    tc_cls2      = "pos" if total_pnl >= 0 else "neg"
    nsigs        = len(signals)
    npairs       = len(pair_results)
    nsigs_cls    = "pos" if nsigs > 0 else "neu"

    html = (
        "<!DOCTYPE html>\n"
        '<html lang="ja">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>ペアトレード バックテスト {today_str}</title>\n"
        "<style>\n"
        "*{box-sizing:border-box;margin:0;padding:0}\n"
        "body{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;"
        "background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}\n"
        "h1{font-size:1.4em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}\n"
        "h2{font-size:1.1em;color:#e2e8f0;margin:24px 0 8px;border-left:3px solid #a78bfa;padding-left:10px}\n"
        ".meta{color:#666;font-size:0.82em;margin:2px 0 8px 16px}\n"
        ".cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}\n"
        ".card{background:#16192a;border:1px solid #252840;border-radius:10px;padding:14px 20px;min-width:130px}\n"
        ".clabel{font-size:0.72em;color:#777;letter-spacing:.05em}\n"
        ".cval{font-size:1.55em;font-weight:700;margin-top:3px}\n"
        ".pos{color:#4ade80}.neg{color:#f87171}.neu{color:#c8cfe8}\n"
        ".section{background:#11141f;border:1px solid #1e2235;border-radius:10px;padding:16px;margin:20px 0}\n"
        ".section-title{font-size:1.0em;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;"
        "margin-bottom:12px;font-weight:600}\n"
        "table{width:100%;border-collapse:collapse;font-size:0.85em}\n"
        "th{background:#16192a;color:#888;padding:8px 12px;text-align:right;"
        "border-bottom:1px solid #252840;white-space:nowrap}\n"
        "th:first-child,th:nth-child(2),th:nth-child(3){text-align:left}\n"
        "td{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}\n"
        "td:first-child,td:nth-child(2),td:nth-child(3){text-align:left}\n"
        "tr.win>td{background:rgba(74,222,128,.04)}\n"
        "tr.lose>td{background:rgba(248,113,113,.04)}\n"
        "tr.hold>td{background:rgba(251,191,36,.06)}\n"
        "tr:hover>td{background:#1b1f35!important}\n"
        ".detail-section{background:#13162b;border:1px solid #1e2235;border-radius:10px;padding:20px;margin:24px 0}\n"
        ".name-label{color:#94a3b8;font-size:0.85em;font-weight:400}\n"
        ".detail-stats{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 14px;font-size:0.88em;color:#888}\n"
        ".detail-stats b{color:#dde1ec}\n"
        ".footer{margin-top:32px;color:#444;font-size:0.78em;text-align:right}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>ペアトレード バックテスト</h1>\n"
        '<div class="meta">\n'
        f"  期間: {since_str} 〜 {today_str} &nbsp;|&nbsp;\n"
        f"  ユニバース: {universe} ({npairs}ペア) &nbsp;|&nbsp;\n"
        f"  バックテスト: {backtest_days}日\n"
        "</div>\n"
        '\n<div class="cards">\n'
        '  <div class="card"><div class="clabel">本日シグナル</div>\n'
        f'    <div class="cval {nsigs_cls}">{nsigs}ペア</div></div>\n'
        '  <div class="card"><div class="clabel">全損益合計</div>\n'
        f'    <div class="cval {tc_cls2}">{total_pnl:+,.0f}円</div></div>\n'
        '  <div class="card"><div class="clabel">トレード計</div>\n'
        f'    <div class="cval neu">{total_trades}回</div></div>\n'
        '  <div class="card"><div class="clabel">プラスペア</div>\n'
        f'    <div class="cval neu">{plus_count}/{npairs}</div></div>\n'
        "</div>\n"
        '\n<div class="section">\n'
        f'  <div class="section-title">本日のシグナル &mdash; |Z| &gt; {Z_ENTRY:.1f}</div>\n'
        "  <table>\n"
        "  <thead><tr>\n"
        "    <th>ペア</th><th>方向</th><th>現在Z-score</th><th>相関</th><th>推奨アクション</th>\n"
        "  </tr></thead>\n"
        f"  <tbody>{signal_rows}</tbody>\n"
        "  </table>\n"
        "</div>\n"
        f"\n{detail_sections}\n"
        '\n<div class="section">\n'
        f'  <div class="section-title">バックテスト結果ランキング ({since_str} 〜 {today_str})</div>\n'
        "  <table>\n"
        "  <thead><tr>\n"
        "    <th>ランク</th><th>ペア</th><th>相関</th>\n"
        "    <th>損益（円）</th><th>損益（%）</th><th>勝率</th><th>PF</th><th>取引回数</th>\n"
        "  </tr></thead>\n"
        f"  <tbody>{scan_rows}</tbody>\n"
        "  </table>\n"
        "</div>\n"
        f'\n<div class="footer">生成: {gen_time} &nbsp;|&nbsp; ペアトレード（共和分 Zスコアストラテジー）</div>\n'
        "</body></html>"
    )

    fname = f"pairs_bt_{today_str}.html"
    path  = Path(fname)
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン処理 ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="統計的ペアトレード バックテスト（共和分ペアトレード）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_pairs.py                     # 監視銘柄スキャン（1年）
  python backtest_pairs.py --universe all      # 日経225全銘柄スキャン
  python backtest_pairs.py --days 180          # 180日バックテスト
  python backtest_pairs.py --no-browser        # ブラウザ自動起動なし
  python backtest_pairs.py --min-corr 0.70     # 最低相関を 0.70 に変更
""")
    parser.add_argument("--days",       type=int,   default=BACKTEST_DAYS,
                        help=f"バックテスト日数（デフォルト: {BACKTEST_DAYS}）")
    parser.add_argument("--universe",   type=str,   default="watch",
                        choices=["watch", "all"],
                        help="銘柄ユニバース: watch（監視銘柄）または all（日経225）")
    parser.add_argument("--no-browser", action="store_true",
                        help="HTMLレポートをブラウザで自動起動しない")
    parser.add_argument("--min-corr",   type=float, default=MIN_CORR,
                        help=f"有効ペアの最低相関（デフォルト: {MIN_CORR}）")
    args = parser.parse_args()

    backtest_days = args.days
    min_corr      = args.min_corr
    since_str     = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today_str     = _TODAY.strftime("%Y-%m-%d")

    # 銘柄をロードしてセクター別にグループ化
    symbols   = _load_symbols(args.universe)
    by_sector: dict[str, list[tuple[str, str]]] = {}
    for sym, name in symbols:
        sec = _guess_sector(sym)
        by_sector.setdefault(sec, []).append((sym, name))

    # 各セクター内のペアを生成
    all_pairs: list[tuple[str, str, str, str]] = []
    for sec, members in by_sector.items():
        if len(members) < 2:
            continue
        for (sa, na), (sb, nb) in itertools.combinations(members, 2):
            all_pairs.append((sa, na, sb, nb))

    print(f"\n  ペアトレードスキャン  {len(symbols)}銘柄  {since_str} 〜 {today_str}")
    print(f"  セクター数: {len(by_sector)}セクター  ペア候補: {len(all_pairs)}ペア")
    print(f"  相関閾値: {min_corr}  データ取得・バックテスト中 (並列{WORKERS}スレッド) ...")

    pair_results: list[dict] = []
    signals:      list[dict] = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(_process_pair, sa, na, sb, nb, backtest_days, min_corr): (sa, na, sb, nb)
            for sa, na, sb, nb in all_pairs
        }
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            sa, na, sb, nb = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                print(f"  エラー [{sa}/{sb}]: {e}")
                result = None
            if result is not None:
                pair_results.append(result)
                if result["today_sig"] is not None:
                    signals.append(result["today_sig"])
            # 進捗表示（20件ごと）
            if done_count % 20 == 0 or done_count == len(all_pairs):
                print(f"  {done_count}/{len(all_pairs)} 完了...", end="\r", flush=True)
    print()

    # ── 結果表示 ─────────────────────────────────────────────
    total_pnl    = sum(r.get("total", 0)       for r in pair_results)
    total_trades = sum(r.get("trade_count", 0) for r in pair_results)
    plus_count   = sum(1 for r in pair_results if r.get("total", 0) > 0)
    tc_sign      = "+" if total_pnl >= 0 else ""

    print(f"\n  スキャン完了: {len(all_pairs)}ペア候補 → 有効: {len(pair_results)}ペア")
    print(f"  トレード合計: {total_trades}回  プラスペア: {plus_count}/{len(pair_results)}")
    print(f"  全損益合計: {tc_sign}{total_pnl:,.0f}円\n")

    if signals:
        print(f"  ★ 本日のシグナル ({len(signals)}ペア):")
        for s in sorted(signals, key=lambda x: -abs(x.get("z", 0))):
            print(f"    [{s['sym_a']}/{s['sym_b']}]  {s['direction']}  "
                  f"Z:{s['z']:+.3f}  相関:{s['corr']:.3f}  推奨: {s['action']}")
    else:
        print("  本日シグナルなし")

    # ── HTMLレポート生成 ─────────────────────────────────────
    path = build_html(signals, pair_results, backtest_days, args.universe)
    print(f"\n  HTMLレポート保存: {path}")
    if not args.no_browser:
        webbrowser.open(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
