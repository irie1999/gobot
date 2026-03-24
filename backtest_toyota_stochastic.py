"""
トヨタ自動車 (7203.T) A6: ストキャスティクス バックテスト
──────────────────────────────────────────────────────────
アルゴリズム A6: ストキャスティクス クロス
  Entry: スロー%K が %D を 30 以下から上抜け（ゴールデンクロス）
  Exit : スロー%K が %D を 60 以上から下抜け（デッドクロス）
  ストップロス: ATR × 1.5

使い方:
  pip install yfinance pandas numpy matplotlib
  python backtest_toyota_stochastic.py
"""

import base64
import io
import os
import subprocess
import sys
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
SYMBOL       = "7203.T"
NAME         = "トヨタ自動車"
YEARS        = 5               # バックテスト期間（年）
INITIAL_CASH = 500_000         # 初期資金（円）

ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5
RISK_PER_TRADE = 0.03          # 1トレードあたりリスク 3%
MAX_COST_RATIO = 0.10          # 1回の購入上限 10%
MAX_QTY        = 3000

STOCH_K  = 14
STOCH_SM = 3
STOCH_D  = 3

OUTPUT_HTML = os.path.join(os.path.dirname(__file__),
                            "backtest_toyota_stochastic.html")


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
    c, h, l = df["close"], df["high"], df["low"]

    # ATR
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # スロー・ストキャスティクス
    hh = h.rolling(STOCH_K).max()
    ll = l.rolling(STOCH_K).min()
    fast_k = (c - ll) / (hh - ll).replace(0, np.nan) * 100
    slow_k = fast_k.rolling(STOCH_SM).mean()
    slow_d = slow_k.rolling(STOCH_D).mean()
    df["stoch_k"] = slow_k
    df["stoch_d"] = slow_d

    # クロス判定
    df["cross_up"] = (slow_k > slow_d) & (slow_k.shift(1) <= slow_d.shift(1))
    df["cross_dn"] = (slow_k < slow_d) & (slow_k.shift(1) >= slow_d.shift(1))

    # A6 シグナル
    df["entry_sig"] = df["cross_up"] & (slow_k.shift(1) < 30)
    df["exit_sig"]  = df["cross_dn"] & (slow_k.shift(1) > 60)

    return df


# ── バックテストエンジン ──────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> dict:
    cash        = float(INITIAL_CASH)
    in_pos      = False
    entry_price = stop_price = 0.0
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
            if today["low"] <= stop_price:
                exit_p = min(float(today["open"]), stop_price)
                exit_r = "ストップ"
            elif prev["exit_sig"]:
                exit_p = float(today["open"])
                exit_r = "シグナル"

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
                    stop_price   = entry_price - stop_dist
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
    n     = len(trades)
    wins  = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses= [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf    = (sum(wins) / abs(sum(losses))
             if losses and sum(losses) != 0 else float("inf"))
    max_pnl  = max((t["pnl"] for t in trades), default=0)
    min_pnl  = min((t["pnl"] for t in trades), default=0)
    avg_hold = sum(t["hold_days"] for t in trades) / n if n else 0

    # 最大ドローダウン
    eq = np.array(equity_curve)
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

    # トレードマーカー
    for t in trades:
        ax1.axvline(t["entry_dt"], color="#00e676", alpha=0.4, linewidth=0.8)
        ax1.axvline(t["exit_dt"],  color="#ff5252", alpha=0.4, linewidth=0.8)
        ax1.scatter(t["entry_dt"], t["entry_price"],
                    marker="^", color="#00e676", s=60, zorder=5)
        ax1.scatter(t["exit_dt"],  t["exit_price"],
                    marker="v", color="#ff5252", s=60, zorder=5)

    ax1.set_title(f"{NAME} ({SYMBOL})  A6: ストキャスティクス バックテスト",
                  color="#e0e0e0", fontsize=13, pad=10)
    ax1.set_ylabel("株価 (円)", color="#e0e0e0")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
    ax1.tick_params(colors="#aaa")
    ax1.grid(alpha=0.15, color="#555")
    ax1.spines[list(ax1.spines)].set_color("#333")
    ax1.legend(facecolor="#1e2a3a", labelcolor="#e0e0e0", fontsize=9)

    # ストキャスティクス
    ax2.plot(df.index, df["stoch_k"], color="#ffd740", linewidth=1.0, label="%K")
    ax2.plot(df.index, df["stoch_d"], color="#ff6d00", linewidth=1.0, label="%D", linestyle="--")
    ax2.axhline(30, color="#64dd17", linewidth=0.6, linestyle=":")
    ax2.axhline(70, color="#ff1744", linewidth=0.6, linestyle=":")
    ax2.fill_between(df.index, 0, 30,  alpha=0.06, color="#64dd17")
    ax2.fill_between(df.index, 70, 100, alpha=0.06, color="#ff1744")
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Stoch", color="#e0e0e0")
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
    colors = ["#00e676" if v >= INITIAL_CASH else "#ff5252" for v in eq]
    ax.fill_between(equity_dates, INITIAL_CASH, eq,
                    where=(eq >= INITIAL_CASH), alpha=0.3, color="#00e676", interpolate=True)
    ax.fill_between(equity_dates, INITIAL_CASH, eq,
                    where=(eq < INITIAL_CASH),  alpha=0.3, color="#ff5252", interpolate=True)
    ax.plot(equity_dates, eq, color="#a0c4ff", linewidth=1.2)
    ax.axhline(INITIAL_CASH, color="#888", linewidth=0.8, linestyle="--", label=f"初期資金 {INITIAL_CASH:,}円")

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

    # KPI カード
    pf_str = "∞" if result["profit_factor"] == float("inf") else f"{result['profit_factor']:.2f}"
    sign   = "+" if result["total_pnl"] >= 0 else ""
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

    # トレード一覧
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
<title>{NAME} A6: ストキャスティクス バックテスト</title>
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
  td{{padding:8px 14px;border-bottom:1px solid #21262d;white-space:nowrap;
      color:#e6edf3}}
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
  <h1>📊 {NAME}（{SYMBOL}）— A6: ストキャスティクス バックテスト</h1>
  <p>
    バックテスト期間: {start} ～ {end}（直近 {YEARS} 年）<br>
    アルゴリズム: A6 ストキャスティクス クロス
    Entry: %K が %D を <strong>30 以下</strong>から上抜け ／
    Exit: %K が %D を <strong>60 以上</strong>から下抜け<br>
    ストップロス: ATR({ATR_PERIOD}) × {ATR_STOP_MULT}
    初期資金: {INITIAL_CASH:,}円
    リスク/トレード: {RISK_PER_TRADE*100:.0f}%<br>
    生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </p>
</div>

{kpi_html}

<div class="params">
  <strong>パラメータ</strong>：
  ストキャスティクス %K={STOCH_K} / Smooth={STOCH_SM} / %D={STOCH_D} ／
  ATR={ATR_PERIOD} ／ ストップ倍率={ATR_STOP_MULT} ／
  過売りライン=30 ／ 過買いライン=60（Exit判定）
</div>

<div class="section">
  <h2 class="sec">株価チャート & ストキャスティクス（エントリー/エグジット）</h2>
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

    print(f"\n{'='*60}")
    print(f"  {NAME} ({SYMBOL})  A6: ストキャスティクス バックテスト")
    print(f"  期間: {start} ～ {end}  初期資金: {INITIAL_CASH:,}円")
    print(f"{'='*60}")

    print("\n[1/3] データ取得中 ...")
    df = fetch_data(SYMBOL, start, end)
    print(f"  取得完了: {len(df)} 日分")

    print("[2/3] インジケーター計算 & バックテスト実行中 ...")
    df = add_indicators(df)
    result = run_backtest(df)

    n   = result["n_trades"]
    pf  = "∞" if result["profit_factor"] == float("inf") else f"{result['profit_factor']:.2f}"
    sgn = "+" if result["total_pnl"] >= 0 else ""
    print(f"\n  ── 結果サマリー ──────────────────────────")
    print(f"  総損益:     {sgn}{result['total_pnl']:>12,.0f} 円")
    print(f"  収益率:     {sgn}{result['return_pct']:>11.2f} %")
    print(f"  取引回数:   {n:>12} 回")
    print(f"  勝率:       {result['win_rate']:>11.1f} %")
    print(f"  PF:         {pf:>12}")
    print(f"  最大DD:     {result['max_drawdown']:>11.2f} %")
    print(f"  平均保有:   {result['avg_hold']:>11.1f} 日")
    print(f"  ──────────────────────────────────────────")

    print("\n[3/3] HTML レポート生成中 ...")
    export_html(df, result, start, end)
    abs_path = os.path.abspath(OUTPUT_HTML)
    print(f"  保存完了: {abs_path}")

    try:
        if sys.platform == "win32":
            os.startfile(abs_path)
        elif sys.platform == "darwin":
            subprocess.run(["open", abs_path], check=False)
        else:
            subprocess.run(["xdg-open", abs_path], check=False)
    except Exception:
        pass

    print(f"\n{'='*60}")
    print("  完了！HTMLレポートを確認してください。")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
