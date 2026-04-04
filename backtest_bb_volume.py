"""
BB下限＋出来高急増 指値エントリー バックテスト
================================================
- Signal: close <= BB lower band AND volume > 2x average AND close > MA200
- Entry: Limit order at BB lower band * 0.999
- Exit: BB mid band (profit), -1.5xATR (stop), max 15 days
Usage:
  python backtest_bb_volume.py
  python backtest_bb_volume.py --symbol 7203.T
  python backtest_bb_volume.py --universe all
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

WORKERS        = 4
POSITION_SIZE  = 100_000
BB_PERIOD      = 20
BB_STD         = 2.0
VOL_PERIOD     = 20
VOL_SURGE      = 2.0
LIMIT_BELOW_BB = 0.001
STOP_ATR_MULT  = 1.5
ENTRY_EXPIRE   = 3
MAX_HOLD       = 15
MA_FILTER      = 200
JST            = timezone(timedelta(hours=9))

WATCHLIST_PERIODS = [30, 90, 180, 365]
WL_MIN_TRADES     = 3
WL_MIN_WR         = 60.0
WL_MIN_PF         = 1.2
_TODAY         = pd.Timestamp(datetime.now(tz=JST).date())
_CACHE_DIR     = Path(".rsi2_cache")

from symbols_watch_rsi2 import SYMBOLS as _WATCH_SYMBOLS
try:
    from rsi2 import SYMBOLS as _ALL_SYMBOLS
except ImportError:
    _ALL_SYMBOLS = _WATCH_SYMBOLS


def _load_symbols(universe):
    if universe == "all":
        return list(_ALL_SYMBOLS)
    return list(_WATCH_SYMBOLS)


def fetch(symbol, backtest_days):
    _CACHE_DIR.mkdir(exist_ok=True)
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
        df = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw.get("volume", pd.Series(0, index=raw.index)).to_numpy(dtype=float),
        }, index=raw.index)
        with open(persistent, "wb") as f:
            pickle.dump(df, f)
        return df
    except Exception:
        return None


def calc(df):
    df   = df.copy()
    c    = df["close"]
    h    = df["high"]
    l    = df["low"]
    v    = df["volume"]
    prev = c.shift(1)

    df["ma200"]   = c.rolling(MA_FILTER).mean()
    tr            = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    df["atr"]     = tr.ewm(com=13, adjust=False).mean()
    df["bb_mid"]  = c.rolling(BB_PERIOD).mean()
    bb_std_s      = c.rolling(BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * bb_std_s
    df["bb_lower"] = df["bb_mid"] - BB_STD * bb_std_s
    df["vol_ma"]   = v.rolling(VOL_PERIOD).mean()
    df["vol_ratio"] = v / df["vol_ma"].replace(0, np.nan)
    df["signal"]   = (
        (c <= df["bb_lower"]) &
        (v > VOL_SURGE * df["vol_ma"]) &
        (c > df["ma200"])
    )
    return df


def _has_signal_today(df):
    if len(df) < 2:
        return None
    last      = df.iloc[-1]
    bb_lower  = last.get("bb_lower", float("nan"))
    bb_mid    = last.get("bb_mid",   float("nan"))
    atr       = last.get("atr",      float("nan"))
    vol_ratio = last.get("vol_ratio", float("nan"))
    if not last.get("signal", False):
        return None
    if pd.isna(bb_lower) or pd.isna(atr):
        return None
    limit_p = bb_lower * (1 - LIMIT_BELOW_BB)
    return {
        "close":       float(last["close"]),
        "bb_lower":    float(bb_lower),
        "bb_mid":      float(bb_mid),
        "atr":         float(atr),
        "vol_ratio":   float(vol_ratio) if not pd.isna(vol_ratio) else float("nan"),
        "limit_price": limit_p,
        "stop":        limit_p - STOP_ATR_MULT * atr,
        "target":      float(bb_mid),
    }


def backtest_bb_vol(df, backtest_days):
    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()
    if len(df) < 3:
        return []

    trades        = []
    in_pos        = False
    entry_p       = 0.0
    entry_dt      = None
    entry_atr     = 0.0
    entry_target  = 0.0
    pending_order = None

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        dt   = df.index[i]
        op   = float(row["open"])
        hi   = float(row["high"])
        lo   = float(row["low"])
        cl   = float(row["close"])

        if in_pos:
            hold_days   = (dt - entry_dt).days
            stop_level  = entry_p - STOP_ATR_MULT * entry_atr
            target_level = entry_target

            exit_p = None
            reason = None
            if op <= stop_level:
                exit_p = op
                reason = "ギャップストップ"
            elif lo <= stop_level:
                exit_p = stop_level
                reason = "ストップロス"
            elif hi >= target_level:
                exit_p = target_level
                reason = "BBミッド到達"
            elif hold_days >= MAX_HOLD:
                exit_p = cl
                reason = f"最大{MAX_HOLD}日"

            if exit_p is not None:
                pnl = (exit_p - entry_p) / entry_p * POSITION_SIZE
                pct = (exit_p - entry_p) / entry_p * 100
                trades.append(dict(
                    entry_dt = entry_dt,
                    exit_dt  = dt,
                    entry_p  = entry_p,
                    exit_p   = exit_p,
                    limit_p  = entry_p,
                    stop_p   = entry_p - STOP_ATR_MULT * entry_atr,
                    target_p = entry_target,
                    atr      = entry_atr,
                    pnl      = pnl,
                    pct      = pct,
                    hold     = hold_days,
                    reason   = reason,
                ))
                in_pos        = False
                pending_order = None
            continue

        if pending_order is not None:
            age = (dt - pending_order["placed_dt"]).days
            if age > ENTRY_EXPIRE:
                pending_order = None
            elif lo <= pending_order["limit_price"]:
                entry_p      = pending_order["limit_price"]
                entry_atr    = pending_order["atr"]
                entry_target = pending_order["bb_mid"]
                entry_dt     = dt
                in_pos       = True
                continue

        if not in_pos and pending_order is None:
            if prev.get("signal", False):
                bb_lower = prev.get("bb_lower", float("nan"))
                bb_mid   = prev.get("bb_mid",   float("nan"))
                prev_atr = prev.get("atr",       float("nan"))
                if not pd.isna(bb_lower) and not pd.isna(prev_atr):
                    pending_order = {
                        "limit_price": float(bb_lower) * (1 - LIMIT_BELOW_BB),
                        "atr":         float(prev_atr),
                        "bb_mid":      float(bb_mid),
                        "placed_dt":   dt,
                    }

    if in_pos:
        last_cl = float(df.iloc[-1]["close"])
        pnl = (last_cl - entry_p) / entry_p * POSITION_SIZE
        pct = (last_cl - entry_p) / entry_p * 100
        trades.append(dict(
            entry_dt = entry_dt,
            exit_dt  = df.index[-1],
            entry_p  = entry_p,
            exit_p   = last_cl,
            limit_p  = entry_p,
            stop_p   = entry_p - STOP_ATR_MULT * entry_atr,
            target_p = entry_target,
            atr      = entry_atr,
            pnl      = pnl,
            pct      = pct,
            hold     = (df.index[-1] - entry_dt).days,
            reason   = "保有中",
        ))
    return trades


def _process_symbol(symbol, name, backtest_days):
    df = fetch(symbol, backtest_days)
    if df is None:
        return None
    df = calc(df)
    trades    = backtest_bb_vol(df, backtest_days)
    today_sig = _has_signal_today(df)
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


def build_html(signals, scan_results, backtest_days, universe):
    today_str = _TODAY.strftime("%Y-%m-%d")
    since_str = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    gen_time  = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    signal_rows = ""
    for s in signals:
        sym  = s.get("symbol", "")
        name = s.get("name", "")
        cl   = s.get("close",       float("nan"))
        bl   = s.get("bb_lower",    float("nan"))
        lp   = s.get("limit_price", float("nan"))
        st   = s.get("stop",        float("nan"))
        tg   = s.get("target",      float("nan"))
        atr  = s.get("atr",         float("nan"))
        vr   = s.get("vol_ratio",   float("nan"))
        wr   = s.get("win_rate",    float("nan"))
        pf   = s.get("pf",          float("nan"))
        wr_s = f"{wr:.1f}%" if not pd.isna(wr) else "N/A"
        pf_s = ("∞" if pf == float("inf") else f"{pf:.2f}") if not pd.isna(pf) else "N/A"
        vr_s = f"{vr:.1f}x" if not pd.isna(vr) else "—"
        atr_s = f"{atr:,.0f}" if not pd.isna(atr) else "—"
        signal_rows += (
            f'<tr>'
            f'<td><b>{sym}</b></td><td>{name}</td>'
            f'<td class="neu">{cl:,.0f}</td>'
            f'<td class="neu">{bl:,.0f}</td>'
            f'<td class="pos">{lp:,.0f}</td>'
            f'<td class="neg">{st:,.0f}</td>'
            f'<td class="pos">{tg:,.0f}</td>'
            f'<td class="neu">{atr_s}</td>'
            f'<td class="neu">{vr_s}</td>'
            f'<td class="{"pos" if not pd.isna(wr) and wr>=55 else "neu"}">{wr_s}</td>'
            f'<td class="{"pos" if not pd.isna(pf) and pf>=1.5 else "neu"}">{pf_s}</td>'
            f'</tr>\n'
        )
    if not signal_rows:
        signal_rows = '<tr><td colspan="11" style="text-align:center;color:#555;padding:20px">本日シグナルなし</td></tr>\n'

    detail_sections = ""
    signal_symbols  = {s["symbol"] for s in signals}
    for r in scan_results:
        if r["symbol"] not in signal_symbols:
            continue
        trades = r.get("trades_list", [])
        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total  = sum(t["pnl"] for t in trades)
        wr_v   = len(wins) / len(trades) * 100 if trades else 0
        pf_v   = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                  if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))
        pf_str = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
        tc_cls = "pos" if total >= 0 else "neg"
        trade_rows = ""
        for i, t in enumerate(trades, 1):
            cls = "hold" if "保有中" in t.get("reason", "") else ("win" if t["pnl"] > 0 else "lose")
            trade_rows += (
                f'<tr class="{cls}">'
                f'<td>{i}</td>'
                f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td>{t.get("limit_p", t["entry_p"]):,.0f}</td>'
                f'<td>{t["entry_p"]:,.0f}</td>'
                f'<td>{t["exit_p"]:,.0f}</td>'
                f'<td class="{"pos" if t["pct"]>=0 else "neg"}">{t["pct"]:+.2f}%</td>'
                f'<td>{t["hold"]}日</td>'
                f'<td>{t.get("reason","")}</td>'
                f'</tr>\n'
            )
        detail_sections += f"""
<div class="detail-section">
  <h2>{r["symbol"]} &nbsp;<span class="name-label">{r["name"]}</span></h2>
  <div class="detail-stats">
    <span>トレード: <b>{len(trades)}</b></span>
    <span>勝率: <b class="{"pos" if wr_v>=55 else "neu"}">{wr_v:.1f}%</b></span>
    <span>PF: <b class="{"pos" if pf_v>=1.5 else "neu"}">{pf_str}</b></span>
    <span>損益: <b class="{tc_cls}">{total:+,.0f}円</b></span>
  </div>
  <table>
  <thead><tr>
    <th>#</th><th>エントリー</th><th>エグジット</th>
    <th>指値</th><th>約定価格</th><th>決済価格</th>
    <th>変化率</th><th>保有日数</th><th>決済理由</th>
  </tr></thead>
  <tbody>{trade_rows}</tbody>
  </table>
</div>
"""

    ranked = sorted(scan_results, key=lambda x: (-x.get("total_pct", 0), x["symbol"]))
    scan_rows = ""
    for rank, r in enumerate(ranked, 1):
        wr_v  = r.get("win_rate", float("nan"))
        pf_v  = r.get("pf",      float("nan"))
        tot   = r.get("total",   0)
        tp    = r.get("total_pct", 0.0)
        tr_n  = r.get("trade_count", 0)
        pcls  = "pos" if tot >= 0 else "neg"
        wr_s  = f"{wr_v:.1f}%" if not pd.isna(wr_v) else "—"
        pf_s2 = ("∞" if pf_v == float("inf") else f"{pf_v:.2f}") if not pd.isna(pf_v) else "—"
        scan_rows += (
            f'<tr class="{"win" if tot>=0 else "lose"}">'
            f'<td>{rank}</td><td>{r["symbol"]}</td><td>{r["name"]}</td>'
            f'<td class="{pcls}">{tot:+,.0f}円</td>'
            f'<td class="{pcls}">{tp:+.2f}%</td>'
            f'<td>{wr_s}</td><td>{pf_s2}</td><td>{tr_n}</td>'
            f'</tr>\n'
        )

    total_pnl    = sum(r.get("total", 0) for r in scan_results)
    total_trades = sum(r.get("trade_count", 0) for r in scan_results)
    plus_count   = sum(1 for r in scan_results if r.get("total", 0) > 0)
    tc_cls2      = "pos" if total_pnl >= 0 else "neg"
    nsigs        = len(signals)
    nuniv        = len(scan_results)

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BB下限＋出来高急増 バックテスト {today_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}}
h1{{font-size:1.4em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}}
h2{{font-size:1.1em;color:#e2e8f0;margin:24px 0 8px;border-left:3px solid #a78bfa;padding-left:10px}}
.meta{{color:#666;font-size:0.82em;margin:2px 0 8px 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:14px 20px;min-width:130px}}
.clabel{{font-size:0.72em;color:#777;letter-spacing:.05em}}
.cval{{font-size:1.55em;font-weight:700;margin-top:3px}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.section{{background:#11141f;border:1px solid #1e2235;border-radius:10px;padding:16px;margin:20px 0}}
.section-title{{font-size:1.0em;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;
                margin-bottom:12px;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:0.85em}}
th{{background:#16192a;color:#888;padding:8px 12px;text-align:right;
    border-bottom:1px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3){{text-align:left}}
td{{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3){{text-align:left}}
tr.win>td{{background:rgba(74,222,128,.04)}}
tr.lose>td{{background:rgba(248,113,113,.04)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
tr:hover>td{{background:#1b1f35!important}}
.detail-section{{background:#13162b;border:1px solid #1e2235;border-radius:10px;padding:20px;margin:24px 0}}
.name-label{{color:#94a3b8;font-size:0.85em;font-weight:400}}
.detail-stats{{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 14px;font-size:0.88em;color:#888}}
.detail-stats b{{color:#dde1ec}}
.footer{{margin-top:32px;color:#444;font-size:0.78em;text-align:right}}
</style>
</head>
<body>
<h1>BB下限＋出来高急増 バックテスト</h1>
<div class="meta">
  期間: {since_str} 〜 {today_str} &nbsp;|&nbsp;
  ユニバース: {universe} ({nuniv}銘柄) &nbsp;|&nbsp;
  BB: {BB_PERIOD}日/{BB_STD}σ / 出来高急増: {VOL_SURGE}倍 / 損切り: {STOP_ATR_MULT}xATR / 最大{MAX_HOLD}日
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
  <div class="section-title">本日のシグナル — BB下限タッチ＋出来高{VOL_SURGE}倍急増</div>
  <table>
  <thead><tr>
    <th>コード</th><th>銘柄名</th><th>終値</th><th>BB下限</th>
    <th>指値</th><th>損切り</th><th>BBミッド(目標)</th><th>ATR</th><th>出来高比</th>
    <th>勝率</th><th>PF</th>
  </tr></thead>
  <tbody>{signal_rows}</tbody>
  </table>
</div>
{detail_sections}
<div class="section">
  <div class="section-title">スキャン結果ランキング ({since_str} 〜 {today_str})</div>
  <table>
  <thead><tr>
    <th>#</th><th>銘柄</th><th>名称</th>
    <th>損益</th><th>累積%</th><th>勝率</th><th>PF</th><th>取引数</th>
  </tr></thead>
  <tbody>{scan_rows}</tbody>
  </table>
</div>
<div class="footer">生成: {gen_time} &nbsp;|&nbsp; BB下限＋出来高急増 指値エントリー</div>
</body></html>"""

    fname = f"bb_vol_bt_{today_str}.html"
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


def _process_symbol_multiperiod(symbol, name, periods):
    max_days = max(periods)
    df_raw = fetch(symbol, max_days)
    if df_raw is None:
        return None
    df = calc(df_raw)
    if len(df) < 50:
        return None
    period_results = {}
    for days in periods:
        trades = backtest_bb_vol(df, days)
        period_results[days] = _calc_period_stats(trades)
    today_sig = _has_signal_today(df)
    return dict(symbol=symbol, name=name, period_results=period_results, today_sig=today_sig)


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
    period_headers    = "".join(f'<th colspan="3">{d}日</th>' for d in periods_s)
    period_subheaders = "".join('<th>回数</th><th>勝率</th><th>PF</th>' for _ in periods_s)

    rows = ""
    for c in candidates:
        pr  = c["period_results"]
        sig = c["today_sig"]
        sig_mark = "★" if sig else ""
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
            period_cells += f'<td>{n}</td><td class="{wr_cls}">{wr_s}</td><td class="{pf_cls}">{pf_s}</td>'
        cl_s = f'{sig["close"]:,.0f}' if sig else "—"
        lp_s = f'{sig["limit_price"]:,.0f}' if sig else "—"
        st_s = f'{sig["stop"]:,.0f}' if sig else "—"
        rows += (
            f'<tr><td>{sig_mark}{c["symbol"]}</td><td>{c["name"]}</td>'
            f'<td>{cl_s}</td><td class="pos">{lp_s}</td><td class="neg">{st_s}</td>'
            + period_cells + f'</tr>\n'
        )

    signal_count = sum(1 for c in candidates if c["today_sig"])
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>監視銘柄リスト（BB出来高） {today_str}</title>
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
</style></head><body>
<h1>監視銘柄リスト — BB下限＋出来高急増戦略</h1>
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
</body></html>"""
    out = Path(f"watchlist_bb_vol_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser(
        description="BB下限＋出来高急増 指値エントリー バックテスト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol",     default=None)
    parser.add_argument("--days",       type=int, default=365)
    parser.add_argument("--universe",   choices=["watch", "all"], default="watch")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--watchlist", action="store_true", help="4期間(30/90/180/365日)で監視銘柄を選定")
    args = parser.parse_args()

    if args.watchlist:
        symbols = _load_symbols(args.universe)
        periods = WATCHLIST_PERIODS
        print(f"\n監視銘柄選定モード: {len(symbols)}銘柄 / 期間:{periods}日")
        print(f"基準: 取引≥{WL_MIN_TRADES} / 勝率≥{WL_MIN_WR}% / PF≥{WL_MIN_PF}")
        all_results = []
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_process_symbol_multiperiod, sym, name, periods): (sym, name)
                    for sym, name in symbols}
            for fut in as_completed(futs):
                done += 1
                r = fut.result()
                if r:
                    all_results.append(r)
                if done % 20 == 0 or done == len(symbols):
                    print(f"  {done}/{len(symbols)} 完了", end="\r", flush=True)
        print()
        candidates = [r for r in all_results if _passes_watchlist_filter(r["period_results"])]
        candidates.sort(key=lambda r: -r["period_results"].get(max(periods), {}).get("wr", 0))
        print(f"\n選定結果: {len(candidates)}銘柄")
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
        if not args.no_browser:
            webbrowser.open(f"file://{path.resolve()}")
        return

    if args.symbol:
        print(f"\n{args.symbol} バックテスト ({args.days}日)")
        df = fetch(args.symbol, args.days)
        if df is None:
            print("データ取得失敗")
            return
        df     = calc(df)
        trades = backtest_bb_vol(df, args.days)
        sig    = _has_signal_today(df)

        print(f"\n{'#':>3}  {'エントリー':10}  {'エグジット':10}  {'指値':>8}  {'約定':>8}  {'決済':>8}  {'変化率':>7}  {'保有':>4}  決済理由")
        print("-" * 90)
        for i, t in enumerate(trades, 1):
            print(
                f"{i:>3}  {t['entry_dt'].strftime('%Y-%m-%d'):10}  "
                f"{t['exit_dt'].strftime('%Y-%m-%d'):10}  "
                f"{t.get('limit_p', t['entry_p']):>8,.0f}  "
                f"{t['entry_p']:>8,.0f}  {t['exit_p']:>8,.0f}  "
                f"{t['pct']:>+6.2f}%  {t['hold']:>3}日  {t.get('reason','')}"
            )
        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total  = sum(t["pnl"] for t in trades)
        wr     = len(wins) / len(trades) * 100 if trades else 0
        pf     = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                  if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))
        print(f"\n合計: {len(trades)}回  勝率: {wr:.1f}%  PF: {'∞' if pf==float('inf') else f'{pf:.2f}'}  損益: {total:+,.0f}円")

        if sig:
            print(f"\n【本日シグナルあり】")
            print(f"  終値: {sig['close']:,.0f}  BB下限: {sig['bb_lower']:,.0f}  BBミッド: {sig['bb_mid']:,.0f}")
            print(f"  指値: {sig['limit_price']:,.0f}  損切り: {sig['stop']:,.0f}  目標: {sig['target']:,.0f}")
            print(f"  出来高比: {sig['vol_ratio']:.1f}x")

        results = [_process_symbol(args.symbol, "", args.days)]
        results = [r for r in results if r]
        sigs    = []
        if sig:
            sig["symbol"] = args.symbol
            sig["name"]   = ""
            sigs.append(sig)
        path = build_html(sigs, results, args.days, "single")
        print(f"\nHTMLレポート: {path.resolve()}")
        if not args.no_browser:
            webbrowser.open(f"file://{path.resolve()}")
        return

    symbols = _load_symbols(args.universe)
    print(f"\nBB下限＋出来高急増 スキャン ({args.universe}: {len(symbols)}銘柄, {args.days}日)")

    results = []
    signals = []
    done    = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_process_symbol, sym, name, args.days): (sym, name)
                for sym, name in symbols}
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
                if r["today_sig"]:
                    sig = dict(r["today_sig"])
                    sig["symbol"]   = r["symbol"]
                    sig["name"]     = r["name"]
                    sig["win_rate"] = r["win_rate"]
                    sig["pf"]       = r["pf"]
                    signals.append(sig)
            if done % 20 == 0 or done == len(symbols):
                print(f"  {done}/{len(symbols)} 完了")

    ranked       = sorted(results, key=lambda x: (-x.get("total_pct", 0), x["symbol"]))
    total_trades = sum(r["trade_count"] for r in results)
    total_pnl    = sum(r["total"] for r in results)
    plus_count   = sum(1 for r in results if r["total"] > 0)

    print(f"\n── スキャン結果 ──────────────────────────────────────")
    print(f"銘柄数: {len(results)}  トレード計: {total_trades}  損益合計: {total_pnl:+,.0f}円  プラス銘柄: {plus_count}")

    if signals:
        print(f"\n【本日のシグナル】 {len(signals)}銘柄")
        for s in signals:
            print(f"  {s['symbol']:12} {s['name']:20} 終値:{s['close']:>8,.0f}  "
                  f"指値:{s['limit_price']:>8,.0f}  出来高比:{s.get('vol_ratio',0):.1f}x")
    else:
        print("\n本日シグナルなし")

    print(f"\n── ランキング (上位20) ───────────────────────────────")
    print(f"{'#':>3}  {'銘柄':12}  {'名称':20}  {'損益':>10}  {'累積%':>7}  {'勝率':>6}  {'PF':>6}  {'回数':>4}")
    print("-" * 80)
    for rank, r in enumerate(ranked[:20], 1):
        wr_s = f"{r['win_rate']:.1f}%" if not pd.isna(r['win_rate']) else "—"
        pf_v = r['pf']
        pf_s = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
        print(f"{rank:>3}  {r['symbol']:12}  {r['name']:20}  "
              f"{r['total']:>+10,.0f}  {r['total_pct']:>+6.2f}%  {wr_s:>6}  {pf_s:>6}  {r['trade_count']:>4}")

    path = build_html(signals, results, args.days, args.universe)
    print(f"\nHTMLレポート: {path.resolve()}")
    if not args.no_browser:
        webbrowser.open(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
