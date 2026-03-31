"""
東京海上HD (8766.T)  MACDブレイクアウト × 出来高急増 × ATRトレイリングストップ
──────────────────────────────────────────────────────────────────────────────────
【アルゴリズム】

  Entry（3条件すべて満たす）:
    1. MACDヒストグラムがゼロラインを下から上へクロス（前日<=0 → 当日>0）
       ≡ モメンタムの転換点を捉える
    2. 出来高 > 20日平均出来高 × 1.5 倍
       ≡ 機関投資家の本物の参入を確認
    3. 終値 > 25日移動平均線
       ≡ 短期上昇トレンドにある銘柄のみエントリー

  Exit（いずれか一方）:
    A. MACDヒストグラムがゼロラインを上から下へクロス（前日>=0 → 当日<0）
       ≡ モメンタム喪失で即撤退
    B. ATRトレイリングストップ発動（ATR × 2.5、含み益で自動切り上げ）

使い方:
  pip install yfinance pandas numpy matplotlib
  python backtest_macd_breakout.py
"""

import base64
import io
import os
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import yfinance as yf

# ── パラメータ ────────────────────────────────────────────────
SYMBOL       = "8766.T"
NAME         = "東京海上HD"
YEARS        = 5               # バックテスト期間（年）
INITIAL_CASH = 500_000         # 初期資金（円）

# MACD パラメータ
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9

# 出来高フィルター
VOL_MA_PERIOD   = 20           # 出来高移動平均期間
VOL_SPIKE_MULT  = 1.5          # 出来高スパイク倍率

# トレンドフィルター
MA_TREND_PERIOD = 25           # 短期トレンド確認MA

# ATR・リスク管理
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5           # 初期ストップロス倍率
ATR_TRAIL_MULT = 2.5           # トレイリングストップ倍率（ストキャスより広め）
RISK_PER_TRADE = 0.03          # 1トレードあたりリスク 3%
MAX_COST_RATIO = 0.10          # 1回の購入上限 10%
MAX_QTY        = 3000

OUTPUT_HTML = os.path.join(os.path.dirname(__file__),
                            "backtest_macd_breakout.html")


# ── データ取得 ────────────────────────────────────────────────
def fetch_data(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(symbol, start=start, end=end,
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.empty:
        raise RuntimeError(f"データが取得できませんでした: {symbol}")
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    return df[["open", "high", "low", "close", "volume"]].dropna()


# ── インジケーター ────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # ATR
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # MACD
    ema_fast   = c.ewm(span=MACD_FAST,   adjust=False).mean()
    ema_slow   = c.ewm(span=MACD_SLOW,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line= macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram  = macd_line - signal_line
    df["macd"]      = macd_line
    df["macd_sig"]  = signal_line
    df["macd_hist"] = histogram

    # 出来高 20日平均
    df["vol_ma"] = v.rolling(VOL_MA_PERIOD).mean()

    # 25日移動平均（トレンドフィルター）
    df["ma25"] = c.rolling(MA_TREND_PERIOD).mean()

    # シグナル判定
    prev_hist = histogram.shift(1)

    # Entry: MACDヒストグラムがゼロラインを下から上抜け
    #        AND 出来高スパイク AND 終値 > 25MA
    macd_cross_up = (histogram > 0) & (prev_hist <= 0)
    vol_spike     = v > df["vol_ma"] * VOL_SPIKE_MULT
    trend_up      = c > df["ma25"]
    df["entry_sig"] = macd_cross_up & vol_spike & trend_up

    # Exit: MACDヒストグラムがゼロラインを上から下抜け
    df["exit_sig"] = (histogram < 0) & (prev_hist >= 0)

    return df


# ── バックテストエンジン ──────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> dict:
    cash        = float(INITIAL_CASH)
    in_pos      = False
    entry_price = trail_stop = 0.0
    qty         = 0
    entry_dt    = None
    trades      = []

    equity_curve = [cash]
    equity_dates = [df.index[0]]

    for i in range(1, len(df)):
        today = df.iloc[i]
        prev  = df.iloc[i - 1]

        # ── エグジット ──
        if in_pos:
            exit_p = exit_r = None

            if today["low"] <= trail_stop:
                exit_p = min(float(today["open"]), trail_stop)
                exit_r = "トレイリング"
            elif prev["exit_sig"]:
                exit_p = float(today["open"])
                exit_r = "MACDクロス"

            if exit_p is not None:
                pnl   = (exit_p - entry_price) * qty
                cash += exit_p * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     df.index[i],
                    "entry_price": entry_price,
                    "exit_price":  exit_p,
                    "qty":         qty,
                    "pnl":         pnl,
                    "reason":      exit_r,
                    "hold_days":   (df.index[i] - entry_dt).days,
                })
                in_pos = False
            else:
                # トレイリングストップ切り上げ（含み益方向にのみ）
                atr_v     = float(today["atr"]) if not pd.isna(today["atr"]) else 0.0
                candidate = float(today["close"]) - atr_v * ATR_TRAIL_MULT
                if candidate > trail_stop:
                    trail_stop = candidate

        # ── エントリー ──
        if not in_pos and prev["entry_sig"]:
            atr_v     = float(prev["atr"])
            stop_dist = atr_v * ATR_STOP_MULT
            if stop_dist > 0:
                q_risk = int(cash * RISK_PER_TRADE / stop_dist)
                q_cost = int(cash * MAX_COST_RATIO / float(prev["close"])) if prev["close"] > 0 else q_risk
                q      = max(min(q_risk, q_cost, MAX_QTY), 0)
                cost   = float(today["open"]) * q
                if q > 0 and cost <= cash:
                    cash        -= cost
                    entry_price  = float(today["open"])
                    trail_stop   = entry_price - float(today["atr"]) * ATR_TRAIL_MULT
                    qty          = q
                    entry_dt     = df.index[i]
                    in_pos       = True

        equity_curve.append(cash + (float(today["close"]) * qty if in_pos else 0))
        equity_dates.append(df.index[i])

    # 未決済ポジションを最終終値で決済
    if in_pos:
        ep  = float(df.iloc[-1]["close"])
        pnl = (ep - entry_price) * qty
        cash += ep * qty
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     df.index[-1],
            "entry_price": entry_price,
            "exit_price":  ep,
            "qty":         qty,
            "pnl":         pnl,
            "reason":      "最終日",
            "hold_days":   (df.index[-1] - entry_dt).days,
        })

    total_pnl = cash - INITIAL_CASH
    n      = len(trades)
    wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf     = (sum(wins) / abs(sum(losses))
              if losses and sum(losses) != 0 else float("inf"))
    max_pnl  = max((t["pnl"] for t in trades), default=0)
    min_pnl  = min((t["pnl"] for t in trades), default=0)
    avg_hold = sum(t["hold_days"] for t in trades) / n if n else 0

    eq   = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd   = (eq - peak) / peak * 100
    max_dd = float(dd.min())

    return {
        "total_pnl":     total_pnl,
        "return_pct":    total_pnl / INITIAL_CASH * 100,
        "n_trades":      n,
        "win_rate":      len(wins) / n * 100 if n else 0.0,
        "profit_factor": pf,
        "max_pnl":       max_pnl,
        "min_pnl":       min_pnl,
        "avg_hold":      avg_hold,
        "max_drawdown":  max_dd,
        "trades":        trades,
        "equity_curve":  (equity_dates, equity_curve),
    }


# ── チャート生成 ──────────────────────────────────────────────
def fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def make_price_chart(df: pd.DataFrame, trades: list) -> str:
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 7), facecolor="#1a1a2e",
        gridspec_kw={"height_ratios": [3, 1]},
    )
    for ax in (ax1, ax2):
        ax.set_facecolor("#16213e")

    # 株価
    ax1.plot(df.index, df["close"], color="#a0c4ff", linewidth=1.2, label="終値")

    # 25日移動平均線
    ax1.plot(df.index, df["ma25"], color="#ff9100", linewidth=1.0,
             linestyle="--", label="25MA", alpha=0.85)

    # トレードマーカー
    for t in trades:
        ax1.axvline(t["entry_dt"], color="#00e676", alpha=0.4, linewidth=0.8)
        ax1.axvline(t["exit_dt"],  color="#ff5252", alpha=0.4, linewidth=0.8)
        ax1.scatter(t["entry_dt"], t["entry_price"],
                    marker="^", color="#00e676", s=60, zorder=5)
        ax1.scatter(t["exit_dt"],  t["exit_price"],
                    marker="v", color="#ff5252", s=60, zorder=5)

    ax1.set_title(
        f"{NAME} ({SYMBOL})  MACDブレイクアウト × 出来高急増 × ATRトレイリングストップ",
        color="#e0e0e0", fontsize=13, pad=10)
    ax1.set_ylabel("株価 (円)", color="#e0e0e0")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax1.tick_params(colors="#aaa")
    ax1.grid(alpha=0.15, color="#555")
    ax1.spines[list(ax1.spines)].set_color("#333")
    ax1.legend(facecolor="#1e2a3a", labelcolor="#e0e0e0", fontsize=9)

    # MACDヒストグラム
    hist = df["macd_hist"]
    colors_hist = ["#00e676" if v >= 0 else "#ff5252" for v in hist]
    ax2.bar(df.index, hist, color=colors_hist, width=1.0, alpha=0.8)
    ax2.plot(df.index, df["macd"],     color="#ffd740", linewidth=0.9, label="MACD")
    ax2.plot(df.index, df["macd_sig"], color="#ff6d00", linewidth=0.9,
             linestyle="--", label="Signal")
    ax2.axhline(0, color="#888", linewidth=0.6)
    ax2.set_ylabel("MACD", color="#e0e0e0")
    ax2.tick_params(colors="#aaa")
    ax2.grid(alpha=0.15, color="#555")
    ax2.spines[list(ax2.spines)].set_color("#333")
    ax2.legend(facecolor="#1e2a3a", labelcolor="#e0e0e0", fontsize=9)

    fig.tight_layout(h_pad=0.5)
    return fig_to_b64(fig)


def make_equity_chart(equity_dates, equity_curve: list) -> str:
    fig, ax = plt.subplots(figsize=(14, 4), facecolor="#1a1a2e")
    ax.set_facecolor("#16213e")

    eq = np.array(equity_curve)
    ax.fill_between(equity_dates, INITIAL_CASH, eq,
                    where=(eq >= INITIAL_CASH), alpha=0.3, color="#00e676", interpolate=True)
    ax.fill_between(equity_dates, INITIAL_CASH, eq,
                    where=(eq < INITIAL_CASH),  alpha=0.3, color="#ff5252", interpolate=True)
    ax.plot(equity_dates, eq, color="#a0c4ff", linewidth=1.2)
    ax.axhline(INITIAL_CASH, color="#888", linewidth=0.8, linestyle="--",
               label=f"初期資金 {INITIAL_CASH:,}円")

    ax.set_title("資産推移 (エクイティカーブ)", color="#e0e0e0", fontsize=12, pad=8)
    ax.set_ylabel("資産 (円)", color="#e0e0e0")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax.tick_params(colors="#aaa")
    ax.grid(alpha=0.15, color="#555")
    ax.spines[list(ax.spines)].set_color("#333")
    ax.legend(facecolor="#1e2a3a", labelcolor="#e0e0e0", fontsize=9)
    fig.tight_layout()
    return fig_to_b64(fig)


def make_monthly_pnl_chart(trades: list) -> str:
    if not trades:
        return ""
    rows = [{"month": t["exit_dt"].to_period("M"), "pnl": t["pnl"]} for t in trades]
    df_m = pd.DataFrame(rows).groupby("month")["pnl"].sum()

    fig, ax = plt.subplots(figsize=(14, 3.5), facecolor="#1a1a2e")
    ax.set_facecolor("#16213e")
    colors = ["#00e676" if v >= 0 else "#ff5252" for v in df_m.values]
    ax.bar(range(len(df_m)), df_m.values, color=colors, width=0.7, edgecolor="none")
    ax.axhline(0, color="#888", linewidth=0.6)
    ax.set_xticks(range(len(df_m)))
    ax.set_xticklabels([str(p) for p in df_m.index], rotation=45, ha="right", fontsize=7)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:+,.0f}"))
    ax.set_title("月別損益", color="#e0e0e0", fontsize=12, pad=8)
    ax.set_ylabel("損益 (円)", color="#e0e0e0")
    ax.tick_params(colors="#aaa")
    ax.grid(axis="y", alpha=0.15, color="#555")
    ax.spines[list(ax.spines)].set_color("#333")
    fig.tight_layout()
    return fig_to_b64(fig)


# ── HTML 出力 ─────────────────────────────────────────────────
def export_html(df: pd.DataFrame, result: dict, start: str, end: str) -> None:
    plt.rcParams["font.family"] = [
        "IPAexGothic", "Noto Sans CJK JP", "Hiragino Sans", "MS Gothic", "sans-serif"
    ]

    trades = result["trades"]
    eq_dates, eq_curve = result["equity_curve"]

    price_img   = make_price_chart(df, trades)
    equity_img  = make_equity_chart(eq_dates, eq_curve)
    monthly_img = make_monthly_pnl_chart(trades)

    pf_str    = "∞" if result["profit_factor"] == float("inf") else f"{result['profit_factor']:.2f}"
    sign      = "+" if result["total_pnl"] >= 0 else ""
    pnl_class = "profit" if result["total_pnl"] >= 0 else "loss"

    kpi_html = f"""
<div class="kpi-grid">
  <div class="kpi-card {pnl_class}">
    <div class="kpi-label">総損益</div>
    <div class="kpi-value">{sign}{result['total_pnl']:,.0f}円</div>
  </div>
  <div class="kpi-card {pnl_class}">
    <div class="kpi-label">収益率</div>
    <div class="kpi-value">{sign}{result['return_pct']:.2f}%</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">取引回数</div>
    <div class="kpi-value">{result['n_trades']} 回</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">勝率</div>
    <div class="kpi-value">{result['win_rate']:.1f}%</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">プロフィットファクター</div>
    <div class="kpi-value">{pf_str}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">最大ドローダウン</div>
    <div class="kpi-value loss">{result['max_drawdown']:.2f}%</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">平均保有日数</div>
    <div class="kpi-value">{result['avg_hold']:.1f} 日</div>
  </div>
  <div class="kpi-card profit">
    <div class="kpi-label">最大利益トレード</div>
    <div class="kpi-value">+{result['max_pnl']:,.0f}円</div>
  </div>
  <div class="kpi-card loss">
    <div class="kpi-label">最大損失トレード</div>
    <div class="kpi-value">{result['min_pnl']:,.0f}円</div>
  </div>
</div>
"""

    rows_html = ""
    for i, t in enumerate(trades, 1):
        cls  = "profit" if t["pnl"] >= 0 else "loss"
        sign = "+" if t["pnl"] >= 0 else ""
        rows_html += (
            f'<tr>'
            f'<td class="num">{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td class="num">{t["entry_price"]:,.1f}</td>'
            f'<td class="num">{t["exit_price"]:,.1f}</td>'
            f'<td class="num">{t["qty"]:,}</td>'
            f'<td class="num {cls}">{sign}{t["pnl"]:,.0f}</td>'
            f'<td class="num">{t["hold_days"]}</td>'
            f'<td style="text-align:center">{t["reason"]}</td>'
            f'</tr>\n'
        )

    monthly_section = ""
    if monthly_img:
        monthly_section = f"""
<div class="section">
  <h2 class="sec">月別損益</h2>
  <div class="chart-card"><img src="data:image/png;base64,{monthly_img}" alt="月別損益"></div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{NAME} MACDブレイクアウト バックテスト</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:"Yu Gothic UI","Hiragino Sans","Noto Sans JP",sans-serif;
        background:#0d1117;color:#e6edf3;font-size:14px}}
  .header{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);
           padding:28px 36px;border-bottom:1px solid #30363d}}
  .header h1{{font-size:1.6em;margin-bottom:8px;color:#fff}}
  .header p{{opacity:.8;font-size:.88em;line-height:1.9;color:#cdd9e5}}
  .section{{margin:24px 36px}}
  h2.sec{{font-size:1.05em;color:#79c0ff;border-left:4px solid #388bfd;
           padding-left:10px;margin-bottom:16px}}
  .kpi-grid{{display:flex;flex-wrap:wrap;gap:14px;margin:24px 36px}}
  .kpi-card{{background:#161b22;border:1px solid #30363d;border-radius:10px;
             padding:16px 22px;min-width:160px;flex:1}}
  .kpi-label{{font-size:.78em;color:#8b949e;margin-bottom:6px}}
  .kpi-value{{font-size:1.35em;font-weight:700;color:#e6edf3}}
  .kpi-card.profit .kpi-value{{color:#3fb950}}
  .kpi-card.loss   .kpi-value{{color:#f85149}}
  table{{width:100%;border-collapse:collapse;background:#161b22;
         border-radius:8px;overflow:hidden;border:1px solid #30363d}}
  thead th{{background:#21262d;color:#8b949e;padding:10px 14px;
            text-align:center;font-size:.82em;white-space:nowrap;
            border-bottom:1px solid #30363d}}
  tbody tr:hover{{background:#1c2128}}
  td{{padding:8px 14px;border-bottom:1px solid #21262d;white-space:nowrap;color:#e6edf3}}
  .num{{text-align:right}}
  .profit{{color:#3fb950;font-weight:600}}
  .loss{{color:#f85149;font-weight:600}}
  .chart-card{{background:#161b22;border:1px solid #30363d;border-radius:10px;
               padding:16px;margin-bottom:0}}
  .chart-card img{{width:100%;height:auto;border-radius:6px}}
  .params{{background:#161b22;border:1px solid #30363d;border-radius:10px;
           padding:16px 24px;margin:24px 36px;font-size:.87em;
           color:#8b949e;line-height:2}}
  .params strong{{color:#79c0ff}}
</style>
</head>
<body>

<div class="header">
  <h1>📊 {NAME}（{SYMBOL}）— MACDブレイクアウト × 出来高急増 × ATRトレイリングストップ</h1>
  <p>
    バックテスト期間: {start} ～ {end}（直近 {YEARS} 年）<br>
    <strong>Entry</strong>: MACDヒストグラムがゼロライン上抜け
    ＋ 出来高 &gt; {VOL_MA_PERIOD}日平均 × {VOL_SPIKE_MULT}倍
    ＋ 終値 &gt; {MA_TREND_PERIOD}日MA（3条件すべて必要）<br>
    <strong>Exit</strong>: MACDヒストグラムがゼロライン下抜け
    または ATR({ATR_PERIOD}) × {ATR_TRAIL_MULT} トレイリングストップ発動<br>
    初期資金: {INITIAL_CASH:,}円　リスク/トレード: {RISK_PER_TRADE*100:.0f}%
    生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </p>
</div>

{kpi_html}

<div class="params">
  <strong>パラメータ</strong>：
  MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}) ／
  出来高スパイク閾値 = {VOL_MA_PERIOD}日平均 × {VOL_SPIKE_MULT} ／
  トレンドフィルター = {MA_TREND_PERIOD}日MA ／
  ATR期間 = {ATR_PERIOD} ／
  初期ストップ倍率 = {ATR_STOP_MULT} ／
  トレイリング倍率 = {ATR_TRAIL_MULT}
</div>

<div class="section">
  <h2 class="sec">株価チャート・25MA & MACDヒストグラム（エントリー/エグジット）</h2>
  <div class="chart-card"><img src="data:image/png;base64,{price_img}" alt="株価チャート"></div>
</div>

<div class="section">
  <h2 class="sec">資産推移（エクイティカーブ）</h2>
  <div class="chart-card"><img src="data:image/png;base64,{equity_img}" alt="エクイティカーブ"></div>
</div>

{monthly_section}

<div class="section">
  <h2 class="sec">全トレード一覧（{len(trades)} 件）</h2>
  <table>
    <thead><tr>
      <th>#</th><th>エントリー日</th><th>エグジット日</th>
      <th>買値(円)</th><th>売値(円)</th><th>株数</th>
      <th>損益(円)</th><th>保有日数</th><th>決済理由</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


# ── メイン ────────────────────────────────────────────────────
def main():
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * YEARS + 60)).strftime("%Y-%m-%d")

    print(f"\n{'='*62}")
    print(f"  {NAME} ({SYMBOL})")
    print(f"  MACDブレイクアウト × 出来高急増 × ATRトレイリングストップ")
    print(f"  期間: {start} ～ {end}  初期資金: {INITIAL_CASH:,}円")
    print(f"{'='*62}")

    print("\n[1/3] データ取得中 ...")
    df = fetch_data(SYMBOL, start, end)
    print(f"  取得完了: {len(df)} 日分")

    print("[2/3] インジケーター計算 & バックテスト実行中 ...")
    print(f"  MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})"
          f"  出来高スパイク×{VOL_SPIKE_MULT}  25MA  トレイリングATR×{ATR_TRAIL_MULT}")
    df = add_indicators(df)
    result = run_backtest(df)

    n    = result["n_trades"]
    sign = "+" if result["total_pnl"] >= 0 else ""
    pf_s = "∞" if result["profit_factor"] == float("inf") else f"{result['profit_factor']:.2f}"

    print(f"\n  ── 結果 ─────────────────────────────────────")
    print(f"  総損益       : {sign}{result['total_pnl']:,.0f}円  ({sign}{result['return_pct']:.2f}%)")
    print(f"  取引回数     : {n} 回")
    print(f"  勝率         : {result['win_rate']:.1f}%")
    print(f"  PF           : {pf_s}")
    print(f"  最大DD       : {result['max_drawdown']:.2f}%")
    print(f"  平均保有日数 : {result['avg_hold']:.1f} 日")
    print(f"  最大利益     : +{result['max_pnl']:,.0f}円")
    print(f"  最大損失     : {result['min_pnl']:,.0f}円")
    print()

    print("[3/3] HTMLレポート生成中 ...")
    export_html(df, result, start, end)
    print(f"  出力: {OUTPUT_HTML}")

    try:
        import subprocess, sys
        if sys.platform == "darwin":
            subprocess.run(["open", OUTPUT_HTML])
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", OUTPUT_HTML])
        else:
            import webbrowser
            webbrowser.open(f"file://{OUTPUT_HTML}")
    except Exception:
        pass

    print(f"\n  完了！ブラウザで {OUTPUT_HTML} を確認してください。")


if __name__ == "__main__":
    main()
