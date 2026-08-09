"""
TTM Squeeze Breakout Backtest for Japanese Equities
====================================================
スクイーズブレイクアウトバックテスト（ボリンジャーバンド + ケルトナーチャネル + モメンタム）

TTM Squeeze (John Carter):
  スクイーズ: BBがKCの内側に収まる（ボラティリティ圧縮）
  ブレイクアウト: スクイーズ解除 + モメンタム > 0 + MA200上
  エントリー: 翌日始値
  損切り: エントリー - 1.5 * ATR
  利確: エントリー + 2.5 * ATR
  最大保有: 10日

使い方:
  python backtest_squeeze.py                    # 監視銘柄スキャン（1年）
  python backtest_squeeze.py --universe all     # 日経225全銘柄スキャン
  python backtest_squeeze.py --symbol 7011.T    # 1銘柄詳細
  python backtest_squeeze.py --days 180         # 180日バックテスト
  python backtest_squeeze.py --no-browser       # ブラウザ自動起動なし
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
WORKERS        = 4
POSITION_SIZE  = 100_000   # 1回あたりの投資金額（円）
BACKTEST_DAYS  = 365
JST            = timezone(timedelta(hours=9))
_TODAY         = pd.Timestamp(datetime.now(tz=JST).date())
_CACHE_DIR     = Path(".rsi2_cache")

BB_PERIOD      = 20
BB_STD         = 2.0
KC_PERIOD      = 20
KC_ATR_MULT    = 1.5
MOM_PERIOD     = 12
MA_PERIOD      = 200
STOP_ATR_MULT  = 1.5
PROF_ATR_MULT  = 2.5
MAX_HOLD       = 10

# ── 銘柄ユニバース ────────────────────────────────────────────
from symbols_watch_rsi2 import SYMBOLS as _WATCH_SYMBOLS

try:
    from rsi2 import SYMBOLS as _ALL_SYMBOLS
except ImportError:
    _ALL_SYMBOLS = _WATCH_SYMBOLS


def _load_symbols(universe: str) -> list[tuple[str, str]]:
    if universe == "all":
        return list(_ALL_SYMBOLS)
    return list(_WATCH_SYMBOLS)


# ── データ取得 ────────────────────────────────────────────────
def fetch(symbol: str, backtest_days: int) -> "pd.DataFrame | None":
    """永続キャッシュ優先（.rsi2_cache/*.pkl）・フォールバックでyfinanceダウンロード。"""
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
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw.get("volume", pd.Series(0, index=raw.index)).to_numpy(dtype=float),
        }, index=raw.index)
    except Exception:
        return None


# ── 指標計算 ──────────────────────────────────────────────────
def calc(df: pd.DataFrame) -> pd.DataFrame:
    """
    TTM Squeeze指標を計算する。

    追加列:
      ma200          : 200日移動平均
      atr            : ATR14 (Wilder smoothing)
      bb_mid/upper/lower/width : ボリンジャーバンド (20, 2σ)
      kc_mid/upper/lower       : ケルトナーチャネル (EMA20, 1.5ATR)
      squeeze        : BB が KC 内側に完全に収まる (bool)
      momentum       : 線形回帰モメンタム (12日)
      squeeze_release: 前日squeeze かつ 当日非squeeze (bool)
    """
    df   = df.copy()
    c    = df["close"]
    h    = df["high"]
    l    = df["low"]
    prev = c.shift(1)

    # ATR14 — Wilder smoothing (com=13)
    tr  = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=13, adjust=False).mean()

    # MA200
    df["ma200"] = c.rolling(MA_PERIOD).mean()

    # ボリンジャーバンド (20, 2σ)
    bb_mid        = c.rolling(BB_PERIOD).mean()
    bb_std_s      = c.rolling(BB_PERIOD).std()
    df["bb_mid"]  = bb_mid
    df["bb_upper"]= bb_mid + BB_STD * bb_std_s
    df["bb_lower"]= bb_mid - BB_STD * bb_std_s
    df["bb_width"]= (df["bb_upper"] - df["bb_lower"]) / bb_mid.replace(0, np.nan)

    # ケルトナーチャネル (EMA20, 1.5×ATR)
    kc_mid         = c.ewm(span=KC_PERIOD, adjust=False).mean()
    kc_atr_s       = tr.ewm(com=KC_PERIOD - 1, adjust=False).mean()
    df["kc_mid"]   = kc_mid
    df["kc_upper"] = kc_mid + KC_ATR_MULT * kc_atr_s
    df["kc_lower"] = kc_mid - KC_ATR_MULT * kc_atr_s

    # スクイーズ: BB が KC の内側に完全に収まる
    df["squeeze"] = (df["bb_upper"] < df["kc_upper"]) & (df["bb_lower"] > df["kc_lower"])

    # モメンタム: 線形回帰の最終値
    delta = c - (h.rolling(MOM_PERIOD).max() + l.rolling(MOM_PERIOD).min()) / 2

    def _linreg_val(y: np.ndarray) -> float:
        n  = len(y)
        x  = np.arange(n, dtype=float)
        xm = x - x.mean()
        denom = float(np.dot(xm, xm))
        if denom == 0:
            return float(y[-1])
        b = float(np.dot(xm, y)) / denom
        return float(y.mean()) + b * (n - 1)

    df["momentum"] = delta.rolling(MOM_PERIOD).apply(_linreg_val, raw=True)

    # スクイーズ解除: 前日squeezeかつ当日非squeeze
    sq = df["squeeze"]
    df["squeeze_release"] = sq.shift(1).fillna(False) & ~sq

    return df


# ── 今日のシグナル判定 ────────────────────────────────────────
def _has_signal_today(df: pd.DataFrame) -> "dict | None":
    """最新バーのスクイーズシグナルを判定する。"""
    if len(df) < 2:
        return None
    last = df.iloc[-1]

    sq_rel  = bool(last.get("squeeze_release", False))
    mom     = last.get("momentum", float("nan"))
    close   = float(last["close"])
    ma200   = last.get("ma200",    float("nan"))
    atr     = last.get("atr",      float("nan"))
    bb_w    = last.get("bb_width", float("nan"))

    if not sq_rel:
        return None
    if pd.isna(mom) or mom <= 0:
        return None
    if pd.isna(ma200) or close <= ma200:
        return None
    if pd.isna(atr):
        return None

    return {
        "close":     close,
        "momentum":  float(mom),
        "bb_width":  float(bb_w) if not pd.isna(bb_w) else float("nan"),
        "atr":       float(atr),
        "stop_loss": close - STOP_ATR_MULT * atr,
        "target":    close + PROF_ATR_MULT * atr,
    }


# ── スクイーズバックテスト ─────────────────────────────────────
def backtest_squeeze(df: pd.DataFrame, backtest_days: int) -> list[dict]:
    """
    TTM Squeezeブレイクアウトバックテスト。

    エントリー条件:
      - スクイーズ解除（前日squeeze, 当日非squeeze）
      - モメンタム > 0
      - 終値 > MA200
    エントリー: 翌日始値
    損切り: エントリー - 1.5 * ATR
    利確: エントリー + 2.5 * ATR
    最大保有: 10日
    """
    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()
    if len(df) < 3:
        return []

    trades: list[dict] = []
    in_pos     = False
    entry_p    = 0.0
    entry_dt   = None
    stop_p     = 0.0
    target_p   = 0.0
    signal_atr = 0.0

    for i in range(len(df) - 1):
        row  = df.iloc[i]
        next_row = df.iloc[i + 1]
        dt       = df.index[i]
        next_dt  = df.index[i + 1]

        if in_pos:
            op = float(next_row["open"])
            hi = float(next_row["high"])
            lo = float(next_row["low"])
            cl = float(next_row["close"])
            hold = (next_dt - entry_dt).days

            exit_p = None
            reason = None

            if op <= stop_p:
                exit_p = op
                reason = "ギャップストップ"
            elif lo <= stop_p:
                exit_p = stop_p
                reason = "ストップロス"
            elif hi >= target_p:
                exit_p = target_p
                reason = "利確"
            elif hold >= MAX_HOLD:
                exit_p = cl
                reason = f"最大{MAX_HOLD}日"

            if exit_p is not None:
                pnl = (exit_p - entry_p) / entry_p * POSITION_SIZE
                pct = (exit_p - entry_p) / entry_p * 100
                trades.append(dict(
                    entry_dt = entry_dt,
                    exit_dt  = next_dt,
                    entry_p  = entry_p,
                    exit_p   = exit_p,
                    stop_p   = stop_p,
                    target_p = target_p,
                    atr      = signal_atr,
                    pnl      = pnl,
                    pct      = pct,
                    hold     = hold,
                    reason   = reason,
                ))
                in_pos = False
            continue

        # エントリーシグナル判定（当日バーのシグナル → 翌日エントリー）
        sq_rel = bool(row.get("squeeze_release", False))
        mom    = row.get("momentum", float("nan"))
        close  = float(row["close"])
        ma200  = row.get("ma200", float("nan"))
        atr    = row.get("atr",   float("nan"))

        if not sq_rel:
            continue
        if pd.isna(mom) or mom <= 0:
            continue
        if pd.isna(ma200) or close <= ma200:
            continue
        if pd.isna(atr):
            continue

        entry_p    = float(next_row["open"])
        entry_dt   = next_dt
        stop_p     = entry_p - STOP_ATR_MULT * float(atr)
        target_p   = entry_p + PROF_ATR_MULT * float(atr)
        signal_atr = float(atr)
        in_pos     = True

    # バックテスト期間末に保有中のポジション
    if in_pos:
        last_cl = float(df.iloc[-1]["close"])
        last_dt = df.index[-1]
        hold    = (last_dt - entry_dt).days
        pnl     = (last_cl - entry_p) / entry_p * POSITION_SIZE
        pct     = (last_cl - entry_p) / entry_p * 100
        trades.append(dict(
            entry_dt = entry_dt,
            exit_dt  = last_dt,
            entry_p  = entry_p,
            exit_p   = last_cl,
            stop_p   = stop_p,
            target_p = target_p,
            atr      = signal_atr,
            pnl      = pnl,
            pct      = pct,
            hold     = hold,
            reason   = "保有中",
        ))

    return trades


# ── 1銘柄処理（スレッドプール用） ────────────────────────────
def _process_symbol(
    symbol: str,
    name: str,
    backtest_days: int,
) -> "dict | None":
    df = fetch(symbol, backtest_days)
    if df is None:
        return None
    df = calc(df)

    trades    = backtest_squeeze(df, backtest_days)
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


# ── HTML レポート生成 ─────────────────────────────────────────
def build_html(
    signals: list[dict],
    scan_results: list[dict],
    backtest_days: int,
    universe: str,
) -> Path:
    today_str = _TODAY.strftime("%Y-%m-%d")
    since_str = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    gen_time  = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    # シグナルテーブル行
    signal_rows = ""
    for s in signals:
        sym  = s.get("symbol", "")
        name = s.get("name",   "")
        cl   = s.get("close",     float("nan"))
        mom  = s.get("momentum",  float("nan"))
        bbw  = s.get("bb_width",  float("nan"))
        atr  = s.get("atr",       float("nan"))
        sl   = s.get("stop_loss", float("nan"))
        tgt  = s.get("target",    float("nan"))
        wr   = s.get("win_rate",  float("nan"))
        pf   = s.get("pf",        float("nan"))
        mom_s = f"{mom:+.2f}" if not pd.isna(mom) else "—"
        bbw_s = f"{bbw:.3f}"  if not pd.isna(bbw) else "—"
        atr_s = f"{atr:,.0f}" if not pd.isna(atr) else "—"
        wr_s  = f"{wr:.1f}%"  if not pd.isna(wr)  else "N/A"
        pf_s  = ("∞" if pf == float("inf") else f"{pf:.2f}") if not pd.isna(pf) else "N/A"
        signal_rows += (
            f'<tr>'
            f'<td><b>{sym}</b></td>'
            f'<td>{name}</td>'
            f'<td class="neu">{cl:,.0f}</td>'
            f'<td class="pos">{mom_s}</td>'
            f'<td class="neu">{bbw_s}</td>'
            f'<td class="neu">{atr_s}</td>'
            f'<td class="neg">{sl:,.0f}</td>'
            f'<td class="pos">{tgt:,.0f}</td>'
            f'<td class="{"pos" if not pd.isna(wr) and wr >= 60 else "neu"}">{wr_s}</td>'
            f'<td class="{"pos" if not pd.isna(pf) and pf >= 1.5 else "neu"}">{pf_s}</td>'
            f'</tr>\n'
        )
    if not signal_rows:
        signal_rows = '<tr><td colspan="10" style="text-align:center;color:#555;padding:20px">本日シグナルなし</td></tr>\n'

    # 詳細セクション（シグナル銘柄のみ）
    detail_sections = ""
    signal_symbols  = {s["symbol"] for s in signals}
    for r in scan_results:
        if r["symbol"] not in signal_symbols:
            continue
        trd   = r.get("trades_list", [])
        wins  = [t for t in trd if t["pnl"] > 0]
        losses= [t for t in trd if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trd)
        wr_v  = len(wins) / len(trd) * 100 if trd else 0
        pf_v  = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
                 if losses and sum(t["pnl"] for t in losses) != 0 else float("inf"))
        pf_s2 = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
        tc_cls = "pos" if total >= 0 else "neg"

        trade_rows = ""
        for i, t in enumerate(trd, 1):
            cls = "hold" if "保有中" in t.get("reason","") else ("win" if t["pnl"] > 0 else "lose")
            trade_rows += (
                f'<tr class="{cls}">'
                f'<td>{i}</td>'
                f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td>{t["entry_p"]:,.0f}</td>'
                f'<td>{t["stop_p"]:,.0f}</td>'
                f'<td>{t["target_p"]:,.0f}</td>'
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
    <span>トレード: <b>{len(trd)}</b></span>
    <span>勝率: <b class="{"pos" if wr_v>=60 else "neu"}">{wr_v:.1f}%</b></span>
    <span>PF: <b class="{"pos" if pf_v>=1.5 else "neu"}">{pf_s2}</b></span>
    <span>損益: <b class="{tc_cls}">{total:+,.0f}円</b></span>
    <span>勝/負: <b>{len(wins)}/{len(losses)}</b></span>
  </div>
  <table>
  <thead><tr>
    <th>#</th><th>エントリー</th><th>エグジット</th>
    <th>約定価格</th><th>損切り</th><th>利確目標</th><th>決済価格</th>
    <th>変化率</th><th>保有日数</th><th>決済理由</th>
  </tr></thead>
  <tbody>{trade_rows}</tbody>
  </table>
</div>
"""

    # スキャン結果テーブル
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
            f'<td>{rank}</td>'
            f'<td>{r["symbol"]}</td>'
            f'<td>{r["name"]}</td>'
            f'<td class="{pcls}">{tot:+,.0f}円</td>'
            f'<td class="{pcls}">{tp:+.2f}%</td>'
            f'<td>{wr_s}</td>'
            f'<td>{pf_s2}</td>'
            f'<td>{tr_n}</td>'
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
<title>スクイーズブレイクアウト バックテスト {today_str}</title>
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
<h1>スクイーズブレイクアウト バックテスト</h1>
<div class="meta">
  期間: {since_str} 〜 {today_str} &nbsp;|&nbsp;
  ユニバース: {universe} ({nuniv}銘柄) &nbsp;|&nbsp;
  バックテスト: {backtest_days}日
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
  <div class="section-title">本日のシグナル — スクイーズ解除 + モメンタム &gt; 0 + MA200上</div>
  <table>
  <thead><tr>
    <th>銘柄</th><th>名称</th><th>終値</th><th>モメンタム</th><th>BB幅</th>
    <th>ATR</th><th>損切り</th><th>利確目標</th><th>勝率</th><th>PF</th>
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

<div class="footer">生成: {gen_time} &nbsp;|&nbsp; スクイーズブレイクアウト (BB+KC+モメンタム)</div>
</body></html>"""

    fname = f"squeeze_bt_{today_str}.html"
    path  = Path(fname)
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン処理 ────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="TTM Squeezeブレイクアウトバックテスト（BB + KC + モメンタム）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python backtest_squeeze.py                    # 監視銘柄スキャン（1年）
  python backtest_squeeze.py --universe all     # 日経225全銘柄スキャン
  python backtest_squeeze.py --symbol 7011.T    # 1銘柄詳細
  python backtest_squeeze.py --days 180         # 180日バックテスト
  python backtest_squeeze.py --no-browser       # ブラウザ自動起動なし
""")
    parser.add_argument("--symbol",     type=str, default=None,
                        help="銘柄コード（省略時はユニバース全体スキャン）")
    parser.add_argument("--days",       type=int, default=BACKTEST_DAYS,
                        help=f"バックテスト日数（デフォルト: {BACKTEST_DAYS}）")
    parser.add_argument("--universe",   type=str, default="watch",
                        choices=["watch", "all"],
                        help="銘柄ユニバース: watch（監視銘柄）または all（日経225）")
    parser.add_argument("--no-browser", action="store_true",
                        help="HTMLレポートをブラウザで自動起動しない")
    args = parser.parse_args()

    backtest_days = args.days
    since_str     = (_TODAY - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today_str     = _TODAY.strftime("%Y-%m-%d")

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
        trades = backtest_squeeze(df, backtest_days)
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
                  f"約定:{t['entry_p']:,.0f}  "
                  f"損切:{t['stop_p']:,.0f}  "
                  f"利確:{t['target_p']:,.0f}  "
                  f"決済:{t['exit_p']:,.0f}  "
                  f"{t['pct']:+.2f}%  "
                  f"{t['hold']}日  "
                  f"{t.get('reason','')}")

        if sig:
            print(f"\n  ★ 本日シグナルあり!")
            print(f"     終値: {sig['close']:,.0f}  モメンタム: {sig['momentum']:+.2f}  ATR: {sig['atr']:,.0f}")
            print(f"     損切り: {sig['stop_loss']:,.0f}  利確目標: {sig['target']:,.0f}")
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
            sigs_list = [{**sig, "symbol": sym, "name": name, "win_rate": wr, "pf": pf_v}]
        path = build_html(sigs_list, [result_entry], backtest_days, args.universe)
        print(f"\n  HTMLレポート保存: {path}")
        if not args.no_browser:
            open_html(f"file://{path.resolve()}")
        return

    # ── スキャンモード ────────────────────────────────────────
    symbols = _load_symbols(args.universe)
    print(f"\n  スクイーズブレイクアウト スキャン")
    print(f"  {len(symbols)}銘柄  期間: {since_str} 〜 {today_str}  ({backtest_days}日)")
    print(f"  データ取得・バックテスト中 (並列{WORKERS}スレッド) ...")

    all_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(_process_symbol, sym, name, backtest_days): (sym, name)
            for sym, name in symbols
        }
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            try:
                r = fut.result()
                if r:
                    all_results.append(r)
            except Exception:
                pass
            if done_count % 20 == 0 or done_count == len(symbols):
                print(f"  {done_count}/{len(symbols)} 完了...", end="\r", flush=True)
    print()

    signals = []
    for r in all_results:
        sig = r.get("today_sig")
        if sig:
            signals.append({
                **sig,
                "symbol":   r["symbol"],
                "name":     r["name"],
                "win_rate": r.get("win_rate", float("nan")),
                "pf":       r.get("pf",       float("nan")),
            })

    total_pnl    = sum(r.get("total", 0) for r in all_results)
    total_trades = sum(r.get("trade_count", 0) for r in all_results)
    plus_count   = sum(1 for r in all_results if r.get("total", 0) > 0)

    print(f"\n  スキャン完了: {len(all_results)}/{len(symbols)}銘柄処理")
    print(f"  全損益: {total_pnl:+,.0f}円  トレード: {total_trades}回  "
          f"プラス銘柄: {plus_count}/{len(all_results)}")

    if signals:
        print(f"\n  ★ 本日シグナル ({len(signals)}銘柄):")
        for s in sorted(signals, key=lambda x: x["symbol"]):
            print(f"     {s['symbol']:10s} {s['name'][:14]:<14}  "
                  f"終値:{s['close']:,.0f}  "
                  f"モメンタム:{s['momentum']:+.2f}  "
                  f"損切:{s['stop_loss']:,.0f}  "
                  f"利確:{s['target']:,.0f}")
    else:
        print(f"\n  本日シグナルなし")

    path = build_html(signals, all_results, backtest_days, args.universe)
    print(f"\n  HTMLレポート保存: {path}")
    if not args.no_browser:
        open_html(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
