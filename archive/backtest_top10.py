"""
バックテスト: swing_notify_top10.py 戦略
過去5年の日足データで10銘柄一括検証（S株 1株単位）

使い方:
  python backtest_top10.py
"""



import base64
import io
import os
import subprocess
import sys
import warnings

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore")

from datetime import datetime, timedelta

import logging
import matplotlib
matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import yfinance as yf

# ── 戦略パラメータ（swing_notify_top10.py と完全一致） ──────────
WATCH_LIST = [
    ("8604.T", "野村HD"),
    ("2802.T", "味の素"),
    ("2914.T", "JT"),
    ("7203.T", "トヨタ自動車"),
    ("8001.T", "伊藤忠商事"),
    ("8306.T", "三菱UFJ"),
    ("6752.T", "パナソニックHD"),
    ("8002.T", "丸紅"),
    ("9020.T", "JR東日本"),
    ("6902.T", "デンソー"),
]

EMA_FAST       = 5
EMA_MID        = 20
EMA_SLOW       = 50
RSI_PERIOD     = 14
RSI_ENTRY      = 55
RSI_EXIT       = 60
BB_PERIOD      = 20
BB_K           = 2.0
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5
RISK_PER_TRADE = 0.03
INITIAL_CASH   = 500_000
MAX_COST_RATIO = 0.10
MAX_QTY        = 3000


# ── データ取得 ────────────────────────────────────────────────
def fetch_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].dropna()


# ── インジケーター（swing_notify_top10.py と同一） ────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

    df["ema_fast"] = c.ewm(span=EMA_FAST,  adjust=False).mean()
    df["ema_mid"]  = c.ewm(span=EMA_MID,   adjust=False).mean()
    df["ema_slow"] = c.ewm(span=EMA_SLOW,  adjust=False).mean()
    df["ema_cross_up"] = (
        (df["ema_fast"] > df["ema_mid"]) &
        (df["ema_fast"].shift(1) <= df["ema_mid"].shift(1))
    )

    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

    sma = c.rolling(BB_PERIOD).mean()
    std = c.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = sma + BB_K * std
    df["bb_lower"] = sma - BB_K * std
    df["bb_band"]  = BB_K * std

    prev_c = c.shift(1)
    tr = pd.concat([h - l,
                    (h - prev_c).abs(),
                    (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()
    return df


# ── シグナル（swing_notify_top10.py と同一） ─────────────────
def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    trend_up = df["close"] > df["ema_slow"]
    df["entry_sig"] = trend_up & (
        (df["rsi"] < RSI_ENTRY) |
        df["ema_cross_up"] |
        (df["close"] <= df["bb_lower"] + df["bb_band"])
    )
    df["exit_sig"] = (
        (df["rsi"]      > RSI_EXIT) |
        (df["close"]    >= df["bb_upper"]) |
        (df["ema_fast"] < df["ema_mid"])
    )
    return df


# ── 株数計算（swing_notify_top10.py と同一） ─────────────────
def calc_qty(cash: float, atr: float, close: float) -> int:
    stop_dist = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    qty_by_risk = int(cash * RISK_PER_TRADE / stop_dist)
    qty_by_cost = int(cash * MAX_COST_RATIO / close) if close > 0 else qty_by_risk
    qty = min(qty_by_risk, qty_by_cost, MAX_QTY)
    return max(qty, 1)


# ── 1銘柄バックテスト ─────────────────────────────────────────
def backtest_symbol(df: pd.DataFrame, symbol: str, name: str) -> dict:
    """
    シグナルは前日終値で確認し、翌日始値で執行。
    ストップは当日の安値がストップ価格以下になった場合に執行
    （始値がギャップダウンしていれば始値で決済）。
    """
    cash = INITIAL_CASH
    in_pos = False
    entry_price = stop_price = 0.0
    qty = 0
    entry_dt = None
    trades = []

    for i in range(1, len(df)):
        today = df.iloc[i]
        prev  = df.iloc[i - 1]

        # ── 保有中: 決済判定 ─────────────────────────────────
        if in_pos:
            exit_price  = None
            exit_reason = None

            if today["low"] <= stop_price:
                # ストップ到達: ギャップダウン考慮
                exit_price  = min(today["open"], stop_price)
                exit_reason = "stop"
            elif prev["exit_sig"]:
                exit_price  = today["open"]
                exit_reason = "signal"

            if exit_price is not None:
                pnl    = (exit_price - entry_price) * qty
                cash  += exit_price * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     df.index[i],
                    "symbol":      symbol,
                    "name":        name,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "pnl":         pnl,
                    "reason":      exit_reason,
                })
                in_pos = False

        # ── 待機中: エントリー判定 ───────────────────────────
        if not in_pos and prev["entry_sig"]:
            q    = calc_qty(cash, prev["atr"], prev["close"])
            cost = today["open"] * q
            if q > 0 and cost <= cash:
                entry_price = today["open"]
                stop_price  = entry_price - prev["atr"] * ATR_STOP_MULT
                qty         = q
                cash       -= cost
                entry_dt    = df.index[i]
                in_pos      = True

    # 未決済: 最終日終値で強制クローズ
    if in_pos:
        exit_price = df.iloc[-1]["close"]
        pnl        = (exit_price - entry_price) * qty
        cash      += exit_price * qty
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     df.index[-1],
            "symbol":      symbol,
            "name":        name,
            "entry_price": entry_price,
            "exit_price":  exit_price,
            "qty":         qty,
            "pnl":         pnl,
            "reason":      "force_close",
        })

    total_pnl = cash - INITIAL_CASH
    n_trades  = len(trades)

    wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf     = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")

    return {
        "symbol":        symbol,
        "name":          name,
        "trades":        n_trades,
        "win_rate":      len(wins) / n_trades * 100 if n_trades else 0.0,
        "total_pnl":     total_pnl,
        "return_pct":    total_pnl / INITIAL_CASH * 100,
        "avg_pnl":       np.mean([t["pnl"] for t in trades]) if trades else 0.0,
        "max_win":       max(wins)    if wins   else 0.0,
        "max_loss":      min(losses)  if losses else 0.0,
        "profit_factor": pf,
        "trades_detail": trades,
    }


# ── ポートフォリオ指標 ────────────────────────────────────────
def portfolio_metrics(all_trades: list) -> dict:
    if not all_trades:
        return {}

    td = pd.DataFrame(all_trades).sort_values("exit_dt").reset_index(drop=True)

    # 累積損益 → 最大ドローダウン
    td["cum_pnl"] = td["pnl"].cumsum()
    initial_total = INITIAL_CASH * len(WATCH_LIST)
    equity        = initial_total + td["cum_pnl"]
    peak          = equity.cummax()
    drawdown      = (peak - equity) / peak * 100
    max_dd        = drawdown.max()

    wins   = td[td["pnl"] > 0]
    losses = td[td["pnl"] <= 0]
    pf     = wins["pnl"].sum() / abs(losses["pnl"].sum()) \
             if len(losses) > 0 and losses["pnl"].sum() != 0 else float("inf")

    td["hold_days"] = (td["exit_dt"] - td["entry_dt"]).dt.days

    return {
        "n_trades":      len(td),
        "win_rate":      len(wins) / len(td) * 100,
        "profit_factor": pf,
        "max_drawdown":  max_dd,
        "avg_hold_days": td["hold_days"].mean(),
        "avg_pnl":       td["pnl"].mean(),
        "best_trade":    td["pnl"].max(),
        "worst_trade":   td["pnl"].min(),
        "stop_count":    len(td[td["reason"] == "stop"]),
        "signal_count":  len(td[td["reason"] == "signal"]),
        "force_count":   len(td[td["reason"] == "force_close"]),
    }


# ── HTML エクスポート ─────────────────────────────────────────
def export_html(results: list, all_trades: list, m: dict,
                start: str, end: str, path: str = "backtest_result.html") -> str:

    total_pnl  = sum(r["total_pnl"] for r in results)
    total_init = INITIAL_CASH * len(WATCH_LIST)
    total_ret  = total_pnl / total_init * 100
    years      = (datetime.strptime(end, "%Y-%m-%d") -
                  datetime.strptime(start, "%Y-%m-%d")).days / 365.25
    cagr       = ((total_init + total_pnl) / total_init) ** (1 / years) - 1
    sign       = "+" if total_pnl >= 0 else ""
    pf_disp    = "∞" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"

    plt.rcParams["font.family"] = ["IPAexGothic", "Noto Sans CJK JP",
                                   "Hiragino Sans", "MS Gothic", "sans-serif"]

    def fig_to_b64(fig) -> str:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode()

    # ── グラフ① 累積損益推移（折れ線） ──────────────────────
    sorted_td = sorted(all_trades, key=lambda t: t["exit_dt"])
    cum_dates, cum_vals = [datetime.strptime(start, "%Y-%m-%d")], [0.0]
    acc = 0.0
    for t in sorted_td:
        acc += t["pnl"]
        cum_dates.append(t["exit_dt"])
        cum_vals.append(acc)

    fig1, ax1 = plt.subplots(figsize=(11, 4), facecolor="#f8f9fa")
    ax1.set_facecolor("#ffffff")
    color = "#2e75b6" if cum_vals[-1] >= 0 else "#c00000"
    ax1.plot(cum_dates, cum_vals, color=color, linewidth=2)
    ax1.fill_between(cum_dates, cum_vals, 0,
                     where=[v >= 0 for v in cum_vals],
                     alpha=0.15, color="#2e75b6")
    ax1.fill_between(cum_dates, cum_vals, 0,
                     where=[v < 0 for v in cum_vals],
                     alpha=0.15, color="#c00000")
    ax1.axhline(0, color="#888", linewidth=0.8, linestyle="--")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f"{x:+,.0f}"))
    ax1.set_title("累積損益推移", fontsize=13, pad=10)
    ax1.grid(axis="y", linestyle=":", alpha=0.5)
    ax1.spines[["top", "right"]].set_visible(False)
    img_line = fig_to_b64(fig1)

    # ── グラフ② 銘柄別 総損益（棒グラフ） ─────────────────
    names  = [r["name"] for r in results]
    pnls   = [r["total_pnl"] for r in results]
    colors = ["#276221" if v >= 0 else "#9c0006" for v in pnls]

    fig2, ax2 = plt.subplots(figsize=(10, 4), facecolor="#f8f9fa")
    ax2.set_facecolor("#ffffff")
    bars = ax2.bar(names, pnls, color=colors, width=0.6, edgecolor="white")
    ax2.axhline(0, color="#888", linewidth=0.8)
    for bar_, val in zip(bars, pnls):
        ax2.text(bar_.get_x() + bar_.get_width() / 2,
                 val + (max(abs(v) for v in pnls) * 0.02 * (1 if val >= 0 else -1)),
                 f"{val:+,.0f}", ha="center", va="bottom" if val >= 0 else "top",
                 fontsize=8)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f"{x:+,.0f}"))
    ax2.set_title("銘柄別 総損益 (円)", fontsize=13, pad=10)
    ax2.grid(axis="y", linestyle=":", alpha=0.5)
    ax2.spines[["top", "right"]].set_visible(False)
    plt.xticks(rotation=20, ha="right")
    img_bar_pnl = fig_to_b64(fig2)

    # ── グラフ③ 銘柄別 勝率（棒グラフ） ────────────────────
    wrs = [r["win_rate"] for r in results]
    wr_colors = ["#2e75b6" if w >= 50 else "#e06c00" for w in wrs]

    fig3, ax3 = plt.subplots(figsize=(10, 4), facecolor="#f8f9fa")
    ax3.set_facecolor("#ffffff")
    ax3.bar(names, wrs, color=wr_colors, width=0.6, edgecolor="white")
    ax3.axhline(50, color="#888", linewidth=1, linestyle="--", label="50%")
    ax3.set_ylim(0, 100)
    ax3.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax3.set_title("銘柄別 勝率 (%)", fontsize=13, pad=10)
    ax3.grid(axis="y", linestyle=":", alpha=0.5)
    ax3.spines[["top", "right"]].set_visible(False)
    plt.xticks(rotation=20, ha="right")
    img_bar_wr = fig_to_b64(fig3)

    # ── HTML 組み立て ─────────────────────────────────────
    reason_map = {"signal": "シグナル", "stop": "ストップ", "force_close": "強制"}

    def pnl_td(val, fmt=","):
        s = "profit" if val >= 0 else "loss"
        sign_ = "+" if val >= 0 else ""
        return f'<td class="{s}">{sign_}{val:{fmt}.0f}</td>'

    # 銘柄別テーブル行
    sym_rows = ""
    for r in results:
        td    = r["trades_detail"]
        mxw   = max((t["pnl"] for t in td), default=0)
        mxl   = min((t["pnl"] for t in td), default=0)
        avg_h = (sum((t["exit_dt"] - t["entry_dt"]).days for t in td) / len(td)
                 if td else 0)
        pf_s  = "∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        rc    = "profit-row" if r["total_pnl"] >= 0 else "loss-row"
        sym_rows += (
            f'<tr class="{rc}">'
            f'<td>{r["name"]}</td><td>{r["symbol"]}</td>'
            f'<td class="num">{r["trades"]}</td>'
            f'<td class="num">{r["win_rate"]:.1f}%</td>'
            + pnl_td(r["total_pnl"]) +
            f'<td class="num">{"+" if r["return_pct"]>=0 else ""}{r["return_pct"]:.1f}%</td>'
            + pnl_td(mxw) + pnl_td(mxl) +
            f'<td class="num">{pf_s}</td>'
            f'<td class="num">{avg_h:.1f}</td>'
            f'</tr>\n'
        )

    # 全トレード行
    trade_rows = ""
    for t in sorted(all_trades, key=lambda x: x["entry_dt"]):
        hold  = (t["exit_dt"] - t["entry_dt"]).days
        pnl_r = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
        rc    = "profit-row" if t["pnl"] >= 0 else "loss-row"
        trade_rows += (
            f'<tr class="{rc}">'
            f'<td>{t["name"]}</td><td>{t["symbol"]}</td>'
            f'<td>{t["entry_dt"].strftime("%Y/%m/%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y/%m/%d")}</td>'
            f'<td class="num">{hold}</td>'
            f'<td class="num">{t["entry_price"]:,.1f}</td>'
            f'<td class="num">{t["exit_price"]:,.1f}</td>'
            f'<td class="num">{t["qty"]}</td>'
            + pnl_td(t["pnl"]) +
            f'<td class="num">{"+" if pnl_r>=0 else ""}{pnl_r:.1f}%</td>'
            f'<td>{reason_map.get(t["reason"], t["reason"])}</td>'
            f'</tr>\n'
        )

    # KPI カード
    kpi_color = "#276221" if total_pnl >= 0 else "#9c0006"
    kpis = [
        ("総損益",           f'<span style="color:{kpi_color};font-size:1.6em;font-weight:700">{sign}{total_pnl:,.0f} 円</span>'),
        ("総収益率",          f"{sign}{total_ret:.2f} %"),
        ("年率(CAGR)",       f"{cagr*100:+.2f} %"),
        ("最大ドローダウン",    f"-{m['max_drawdown']:.2f} %"),
        ("総取引数",          f"{m['n_trades']} 回"),
        ("勝率",             f"{m['win_rate']:.1f} %"),
        ("PF",              pf_disp),
        ("平均保有日数",       f"{m['avg_hold_days']:.1f} 日"),
        ("平均損益",          f"{m['avg_pnl']:+,.0f} 円"),
        ("最大利益",          f"+{m['best_trade']:,.0f} 円"),
        ("最大損失",          f"{m['worst_trade']:,.0f} 円"),
        ("決済内訳",          f"シグナル {m['signal_count']} / ストップ {m['stop_count']} / 強制 {m['force_count']}"),
    ]
    kpi_html = "".join(
        f'<div class="kpi-card"><div class="kpi-label">{k}</div>'
        f'<div class="kpi-value">{v}</div></div>'
        for k, v in kpis
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>バックテスト結果</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Yu Gothic UI","Hiragino Sans","Noto Sans JP",sans-serif;
          background: #f0f2f5; color: #222; font-size: 14px; }}
  .header {{ background: linear-gradient(135deg,#1f3864,#2e75b6);
             color: #fff; padding: 24px 32px; }}
  .header h1 {{ font-size: 1.5em; margin-bottom: 6px; }}
  .header p  {{ opacity: .85; font-size: .9em; }}
  .section   {{ margin: 24px 32px; }}
  h2 {{ font-size: 1.1em; color: #1f3864; border-left: 4px solid #2e75b6;
        padding-left: 10px; margin-bottom: 14px; }}
  /* KPI cards */
  .kpi-grid  {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .kpi-card  {{ background: #fff; border-radius: 8px; padding: 14px 18px;
                min-width: 160px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  .kpi-label {{ font-size: .78em; color: #666; margin-bottom: 4px; }}
  .kpi-value {{ font-size: 1.15em; font-weight: 600; }}
  /* Table */
  table  {{ width: 100%; border-collapse: collapse; background: #fff;
             border-radius: 8px; overflow: hidden;
             box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  thead th {{ background: #1f3864; color: #fff; padding: 10px 12px;
              text-align: center; font-size: .88em; white-space: nowrap; }}
  tbody tr:hover {{ background: #eaf1fb !important; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #e8edf3; white-space: nowrap; }}
  .num   {{ text-align: right; }}
  .profit     {{ text-align: right; color: #276221; font-weight: 600; }}
  .loss       {{ text-align: right; color: #9c0006; font-weight: 600; }}
  .profit-row {{ background: #f0fff4; }}
  .loss-row   {{ background: #fff5f5; }}
  tfoot td {{ background: #ffd966; font-weight: 700; padding: 9px 12px; }}
  /* Chart */
  .chart-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .chart-card {{ background: #fff; border-radius: 8px; padding: 16px;
                 box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  .chart-card img {{ width: 100%; height: auto; }}
  .chart-wide {{ grid-column: 1 / -1; }}
  @media (max-width: 700px) {{ .chart-grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>📊 バックテスト結果 — swing_notify_top10.py 戦略</h1>
  <p>期間: {start} ～ {end}（約5年）　初期資金: {INITIAL_CASH:,}円/銘柄 × {len(WATCH_LIST)}銘柄
     　コスト上限{MAX_COST_RATIO*100:.0f}%　リスク{RISK_PER_TRADE*100:.0f}%　ストップATR×{ATR_STOP_MULT}
     　生成: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>
</div>

<div class="section">
  <h2>ポートフォリオ指標</h2>
  <div class="kpi-grid">{kpi_html}</div>
</div>

<div class="section">
  <h2>グラフ</h2>
  <div class="chart-grid">
    <div class="chart-card chart-wide">
      <img src="data:image/png;base64,{img_line}" alt="累積損益推移">
    </div>
    <div class="chart-card">
      <img src="data:image/png;base64,{img_bar_pnl}" alt="銘柄別損益">
    </div>
    <div class="chart-card">
      <img src="data:image/png;base64,{img_bar_wr}" alt="銘柄別勝率">
    </div>
  </div>
</div>

<div class="section">
  <h2>銘柄別パフォーマンス</h2>
  <table>
    <thead><tr>
      <th>銘柄名</th><th>コード</th><th>取引数</th><th>勝率</th>
      <th>総損益(円)</th><th>収益率</th><th>最大利益(円)</th>
      <th>最大損失(円)</th><th>PF</th><th>平均保有日数</th>
    </tr></thead>
    <tbody>{sym_rows}</tbody>
    <tfoot><tr>
      <td colspan="2">【合計】</td>
      <td class="num">{m['n_trades']}</td>
      <td class="num">{m['win_rate']:.1f}%</td>
      <td class="{'profit' if total_pnl>=0 else 'loss'}">{sign}{total_pnl:,.0f}</td>
      <td class="num">{sign}{total_ret:.1f}%</td>
      <td class="profit">+{m['best_trade']:,.0f}</td>
      <td class="loss">{m['worst_trade']:,.0f}</td>
      <td class="num">{pf_disp}</td>
      <td class="num">{m['avg_hold_days']:.1f}</td>
    </tr></tfoot>
  </table>
</div>

<div class="section">
  <h2>全トレード一覧 ({len(all_trades)} 件)</h2>
  <table>
    <thead><tr>
      <th>銘柄名</th><th>コード</th><th>エントリー日</th><th>決済日</th>
      <th>保有日数</th><th>買値(円)</th><th>売値(円)</th><th>株数</th>
      <th>損益(円)</th><th>損益率</th><th>決済理由</th>
    </tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</div>

</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ── メイン ───────────────────────────────────────────────────
def main():
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * 5 + 60)).strftime("%Y-%m-%d")

    print(f"\n{'='*65}")
    print(f"  バックテスト  swing_notify_top10.py 戦略")
    print(f"  期間 : {start} 〜 {end}（約5年）")
    print(f"  資金 : {INITIAL_CASH:,}円/銘柄 × {len(WATCH_LIST)}銘柄"
          f" = {INITIAL_CASH * len(WATCH_LIST):,}円")
    print(f"  設定 : コスト上限{MAX_COST_RATIO*100:.0f}%  "
          f"リスク{RISK_PER_TRADE*100:.0f}%  "
          f"ストップATR×{ATR_STOP_MULT}")
    print(f"{'='*65}")

    results    = []
    all_trades = []

    for symbol, name in WATCH_LIST:
        print(f"  取得中: {name}({symbol}) ...", end="\r", flush=True)
        try:
            df = fetch_data(symbol, start, end)
            df = add_indicators(df)
            df = add_signals(df)
            res = backtest_symbol(df, symbol, name)
            results.append(res)
            all_trades.extend(res["trades_detail"])
        except Exception as e:
            print(f"\n  ⚠ {name}({symbol}) スキップ: {e}")

    print(" " * 60, end="\r")

    m          = portfolio_metrics(all_trades)
    total_pnl  = sum(r["total_pnl"] for r in results)
    total_init = INITIAL_CASH * len(WATCH_LIST)
    total_ret  = total_pnl / total_init * 100
    years      = (datetime.strptime(end, "%Y-%m-%d") -
                  datetime.strptime(start, "%Y-%m-%d")).days / 365.25
    cagr       = ((total_init + total_pnl) / total_init) ** (1 / years) - 1

    W  = 72   # 表の幅
    SB = "═"  # 二重線
    DB = "─"  # 単線

    def bar(ch=DB, w=W): return ch * w
    def row(*cols, widths, align="left"):
        """cols を widths に合わせて整形した1行を返す。"""
        cells = []
        for i, (val, w) in enumerate(zip(cols, widths)):
            if i == 0:
                cells.append(f" {val:<{w}}")
            else:
                cells.append(f"{val:>{w}} ")
        return "│" + "│".join(cells) + "│"

    def pnl_str(v):
        return f"+{v:,.0f}" if v >= 0 else f"{v:,.0f}"

    def pct_str(v):
        return f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"

    def pf_str(v):
        return "  ∞" if v == float("inf") else f"{v:.2f}"

    # ── ヘッダー ──────────────────────────────────────────────
    print()
    print(f"╔{bar(SB)}╗")
    title = f"バックテスト結果  {start} 〜 {end}  (約5年)"
    print(f"║ {title:<{W-2}} ║")
    init_str = (f"初期資金 {INITIAL_CASH:,}円/銘柄 × {len(WATCH_LIST)}銘柄"
                f" = {total_init:,}円   "
                f"コスト上限{MAX_COST_RATIO*100:.0f}%  "
                f"リスク{RISK_PER_TRADE*100:.0f}%  "
                f"ストップATR×{ATR_STOP_MULT}")
    print(f"║ {init_str:<{W-2}} ║")
    print(f"╠{bar(SB)}╣")

    # ── 銘柄別テーブル ────────────────────────────────────────
    # 列幅: 銘柄名16 | 取引4 | 勝率7 | 損益12 | 収益率8 | 最大利益12 | 最大損失12 | PF6
    CW = [14, 4, 7, 12, 8, 10, 10, 6]
    hdrs = ("銘柄名", "取引", "勝率", "損益(円)", "収益率", "最大利益", "最大損失", "PF")
    print(f"║ {'銘柄別パフォーマンス':<{W-2}} ║")
    print(f"╠{'┬'.join(DB*(w+2) for w in CW)}╣".replace("╠", "╟").replace("╣", "╢"))
    print(row(*hdrs, widths=CW))
    print(f"╠{'┼'.join(DB*(w+2) for w in CW)}╣".replace("╠", "╟").replace("╣", "╢"))

    for r in results:
        td    = r["trades_detail"]
        mxw   = max((t["pnl"] for t in td), default=0)
        mxl   = min((t["pnl"] for t in td), default=0)
        print(row(
            r["name"],
            r["trades"],
            f"{r['win_rate']:.1f}%",
            pnl_str(r["total_pnl"]),
            pct_str(r["return_pct"]),
            f"+{mxw:,.0f}",
            f"{mxl:,.0f}",
            pf_str(r["profit_factor"]),
            widths=CW,
        ))

    # 合計行
    print(f"╠{'┼'.join(DB*(w+2) for w in CW)}╣".replace("╠", "╟").replace("╣", "╢"))
    print(row(
        "【合計】",
        m["n_trades"],
        f"{m['win_rate']:.1f}%",
        pnl_str(total_pnl),
        pct_str(total_ret),
        f"+{m['best_trade']:,.0f}",
        f"{m['worst_trade']:,.0f}",
        pf_str(m["profit_factor"]),
        widths=CW,
    ))
    print(f"╚{'╧'.join(SB*(w+2) for w in CW)}╝".replace("╚", "╙").replace("╝", "╜"))

    # ── ポートフォリオ指標 ────────────────────────────────────
    pf_disp = "∞" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    sign    = "+" if total_pnl >= 0 else ""

    print()
    print(f"╔{bar(SB)}╗")
    print(f"║ {'ポートフォリオ指標':<{W-2}} ║")
    print(f"╠{bar(SB)}╣")

    # 2列レイアウト
    KW, VW = 16, 16   # キー幅・値幅
    GAP = 4
    def kv(k, v): return f"  {k:<{KW}}: {v:<{VW}}"
    def kv2(k1, v1, k2, v2):
        return f"║ {kv(k1,v1)}{' '*GAP}{kv(k2,v2):<{W - KW - VW - GAP - 6}} ║"

    print(kv2("総損益",          f"{sign}{total_pnl:,.0f} 円",
              "年率(CAGR)",      f"{cagr*100:+.2f} %"))
    print(kv2("総収益率",         f"{sign}{total_ret:.2f} %",
              "最大DD",          f"-{m['max_drawdown']:.2f} %"))
    print(kv2("総取引数",         f"{m['n_trades']} 回",
              "平均保有日数",     f"{m['avg_hold_days']:.1f} 日"))
    print(kv2("勝率",            f"{m['win_rate']:.1f} %",
              "平均損益",        f"{m['avg_pnl']:+,.0f} 円"))
    print(kv2("PF",             pf_disp,
              "最大利益",        f"+{m['best_trade']:,.0f} 円"))
    exit_str = (f"シグナル {m['signal_count']} / "
                f"ストップ {m['stop_count']} / "
                f"強制 {m['force_count']}")
    print(kv2("決済内訳",         exit_str,
              "最大損失",        f"{m['worst_trade']:,.0f} 円"))
    print(f"╚{bar(SB)}╝")
    print()

    # ── HTML 出力 ─────────────────────────────────────────────
    html_path = "backtest_result.html"
    export_html(results, all_trades, m, start, end, html_path)
    abs_path  = os.path.abspath(html_path)
    print(f"  HTML レポートを保存しました: {abs_path}")

    # OS に応じて自動で開く
    try:
        if sys.platform == "win32":
            os.startfile(abs_path)
        elif sys.platform == "darwin":
            subprocess.run(["open", abs_path], check=False)
        else:
            subprocess.run(["xdg-open", abs_path], check=False)
    except Exception:
        pass
    print()


if __name__ == "__main__":
    main()
