"""
RSI(2) 平均回帰バックテスト  軽量版（1銘柄）
────────────────────────────────────────────
使い方:
  python rsi2.py 7011.T                # 三菱重工（デフォルト1年）
  python rsi2.py 7011.T --years 2      # 2年
  python rsi2.py 7011.T --months 6     # 6ヶ月
  python rsi2.py 7011.T --days 90      # 90日
"""

import argparse
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── パラメーター ────────────────────────────────────────────
RSI2_ENTRY      =  10.0   # RSI(2) ≤ この値 → 翌日買い
RSI2_EXIT       =  65.0   # RSI(2) ≥ この値 → 翌日売り
MA_TREND        = 200     # トレンドフィルター（終値 > MA200 のみ）
HARD_STOP_PCT   =   3.0   # 即損切り %
HALF_PROFIT_PCT =   5.0   # 半分利確 %
ATR_TRAIL_MULT  =   2.0   # ATR トレイリング係数
POSITION_SIZE   = 100_000 # 1回あたりの投資金額（円）
BACKTEST_DAYS   =   365   # デフォルトのバックテスト日数


# ── データ取得 ──────────────────────────────────────────────
def fetch(symbol: str, backtest_days: int) -> pd.DataFrame | None:
    buf_days  = 200 + 30
    total_cal = int((backtest_days + buf_days) * 1.5)

    if   total_cal <= 180:  period = "6mo"
    elif total_cal <= 365:  period = "1y"
    elif total_cal <= 730:  period = "2y"
    elif total_cal <= 1095: period = "3y"
    elif total_cal <= 1825: period = "5y"
    else:                   period = "max"

    try:
        raw = yf.download(symbol, period=period, interval="1d",
                          auto_adjust=True, progress=False)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()
        if len(raw) < 210:
            return None
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
    except Exception:
        return None


# ── 指標計算 ────────────────────────────────────────────────
def calc(df: pd.DataFrame) -> pd.DataFrame:
    c    = df["close"]
    h    = df["high"]
    l    = df["low"]
    prev = c.shift(1)

    d    = c.diff()
    gain = d.clip(lower=0).ewm(span=2,  adjust=False).mean()
    loss = (-d).clip(lower=0).ewm(span=2, adjust=False).mean()
    rsi2 = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    g14   = d.clip(lower=0).ewm(span=14, adjust=False).mean()
    l14   = (-d).clip(lower=0).ewm(span=14, adjust=False).mean()
    rsi14 = 100 - 100 / (1 + g14 / l14.replace(0, np.nan))

    ma200 = c.rolling(200).mean()
    ma50  = c.rolling(50).mean()
    tr    = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr   = tr.ewm(span=14, adjust=False).mean()

    df = df.copy()
    df["rsi2"]  = rsi2
    df["rsi14"] = rsi14
    df["ma200"] = ma200
    df["ma50"]  = ma50
    df["atr"]   = atr
    return df


# ── バックテスト ────────────────────────────────────────────
def backtest(df: pd.DataFrame, backtest_days: int) -> list[dict]:
    cutoff = pd.Timestamp(datetime.today() - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()

    trades    = []
    in_pos    = False
    entry_p   = trail = 0.0
    entry_dt  = None
    half_done = False
    qty       = 0

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        dt   = df.index[i]

        if pd.isna(prev["rsi2"]) or pd.isna(prev["ma200"]):
            continue

        op = float(row["open"])
        lo = float(row["low"])

        if in_pos:
            exit_p = reason = None

            if lo <= entry_p * (1 - HARD_STOP_PCT / 100):
                exit_p = min(op, entry_p * (1 - HARD_STOP_PCT / 100))
                reason = f"損切り(-{HARD_STOP_PCT:.0f}%)"
            elif lo <= trail:
                exit_p = min(op, trail)
                reason = "トレイリング"
            elif float(prev["rsi2"]) >= RSI2_EXIT:
                exit_p = op
                reason = "RSI2回復"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * qty
                trades.append(dict(
                    entry_dt=entry_dt, exit_dt=dt,
                    entry_p=entry_p, exit_p=exit_p, qty=qty,
                    pnl=pnl, hold=(dt - entry_dt).days, reason=reason,
                ))
                in_pos = half_done = False
                continue

            if not half_done:
                cl = float(row["close"])
                if (cl - entry_p) / entry_p * 100 >= HALF_PROFIT_PCT:
                    hq = qty // 2
                    if hq > 0:
                        trades.append(dict(
                            entry_dt=entry_dt, exit_dt=dt,
                            entry_p=entry_p, exit_p=cl, qty=hq,
                            pnl=(cl - entry_p) * hq,
                            hold=(dt - entry_dt).days, reason="半分利確",
                        ))
                        qty -= hq
                        half_done = True

            cand = float(row["close"]) - float(row["atr"]) * ATR_TRAIL_MULT
            if cand > trail:
                trail = cand

        if not in_pos:
            if (float(prev["rsi2"]) <= RSI2_ENTRY
                    and float(prev["close"]) > float(prev["ma200"])
                    and op > 0):
                qty = max(int(POSITION_SIZE / op), 1)
                entry_p  = op
                trail    = op - float(row["atr"]) * ATR_TRAIL_MULT
                entry_dt = dt
                half_done = False
                in_pos   = True

    if in_pos:
        lp = float(df.iloc[-1]["close"])
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=df.index[-1],
            entry_p=entry_p, exit_p=lp, qty=qty,
            pnl=(lp - entry_p) * qty,
            hold=(df.index[-1] - entry_dt).days, reason="保有中★",
        ))

    return trades


# ── ターミナル表示 ──────────────────────────────────────────
def print_result(symbol: str, trades: list[dict],
                 backtest_days: int, label: str) -> None:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")

    if not trades:
        print(f"\n  [{symbol}]  シグナルなし（{since} ～ {today}）\n")
        return

    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    wr    = len(wins) / len(trades) * 100
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    wh    = sum(t["hold"] for t in wins) / len(wins) if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss) if loss else 0
    pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"

    print()
    print("═" * 66)
    print(f"  RSI(2) 平均回帰  [{symbol}]  直近{label}")
    print(f"  期間: {since} ～ {today}")
    print(f"  【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値エントリー")
    print(f"  【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル / "
          f"-{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確")
    print("═" * 66)
    print(f"  トレード: {len(trades)}回  勝: {len(wins)}  負: {len(loss)}")
    print(f"  勝率: {wr:.1f}%   PF: {pf_s}   損益: {total:+,.0f}円")
    print(f"  平均保有: 勝ち {wh:.1f}日 / 負け {lh:.1f}日")
    print()
    print(f"  {'#':<3} {'エントリー':>10} {'エグジット':>10} "
          f"{'買値':>8} {'売値':>8} {'株数':>4} {'損益':>10} 保有  決済理由")
    print("  " + "─" * 66)
    for i, t in enumerate(trades, 1):
        pct  = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        mark = "★" if "保有中" in t["reason"] else " "
        print(f" {mark}{i:<3} "
              f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
              f"{t['entry_p']:>8,.0f} {t['exit_p']:>8,.0f} "
              f"{t['qty']:>4} {t['pnl']:>+10,.0f} "
              f"{t['hold']:>3}日  {t['reason']}({pct:+.1f}%)")
    print("  " + "─" * 66)
    print(f"  合計: {total:+,.0f}円")
    print()


# ── HTML レポート生成 ───────────────────────────────────────
def generate_html(symbol: str, trades: list[dict],
                  backtest_days: int, label: str) -> Path:
    since = (datetime.today() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    today = datetime.today().strftime("%Y-%m-%d")

    wins  = [t for t in trades if t["pnl"] > 0]
    loss  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades) if trades else 0
    wr    = len(wins) / len(trades) * 100 if trades else 0
    pf    = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
             if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
    pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
    wh    = sum(t["hold"] for t in wins) / len(wins) if wins else 0
    lh    = sum(t["hold"] for t in loss) / len(loss) if loss else 0

    # チャート用データ（累積損益）
    cum = 0.0
    chart_dates, chart_cum = [], []
    for t in trades:
        cum += t["pnl"]
        chart_dates.append(t["exit_dt"].strftime("%Y-%m-%d"))
        chart_cum.append(round(cum, 0))

    # トレード行
    rows = ""
    for i, t in enumerate(trades, 1):
        pct = (t["exit_p"] - t["entry_p"]) / t["entry_p"] * 100
        cls = "hold" if "保有中" in t["reason"] else ("win" if t["pnl"] > 0 else "lose")
        rows += (
            f'<tr class="{cls}">'
            f'<td>{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["entry_p"]:,.0f}</td>'
            f'<td>{t["exit_p"]:,.0f}</td>'
            f'<td>{t["qty"]}</td>'
            f'<td class="{"pos" if t["pnl"]>=0 else "neg"}">{t["pnl"]:+,.0f}円</td>'
            f'<td class="{"pos" if pct>=0 else "neg"}">{pct:+.1f}%</td>'
            f'<td>{t["hold"]}日</td>'
            f'<td>{t["reason"]}</td>'
            f'</tr>\n'
        )

    total_cls = "pos" if total >= 0 else "neg"

    html = f"""\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSI(2) [{symbol}] バックテスト結果</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,'Hiragino Sans','Noto Sans JP',sans-serif;
      background:#0f1117;color:#dde1ec;padding:24px;font-size:14px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #3a86ff;
    padding-left:12px;margin-bottom:6px}}
.meta{{color:#666;font-size:0.82em;margin:2px 0 0 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;
       padding:14px 20px;min-width:130px}}
.clabel{{font-size:0.72em;color:#777;letter-spacing:.05em}}
.cval{{font-size:1.55em;font-weight:700;margin-top:3px}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.chart-wrap{{background:#16192a;border:1px solid #252840;border-radius:10px;
             padding:16px;margin:20px 0;max-width:900px}}
table{{width:100%;border-collapse:collapse;font-size:0.86em;margin-top:16px}}
th{{background:#16192a;color:#888;padding:8px 12px;text-align:right;
    border-bottom:1px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3),th:last-child{{text-align:left}}
td{{padding:7px 12px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3),td:last-child{{text-align:left}}
tr.win>td{{background:rgba(74,222,128,.04)}}
tr.lose>td{{background:rgba(248,113,113,.04)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
tr:hover>td{{background:#1b1f35!important}}
.footer{{margin-top:32px;color:#444;font-size:0.78em;text-align:right}}
</style>
</head>
<body>
<h1>RSI(2) 平均回帰バックテスト  [{symbol}]  直近{label}</h1>
<div class="meta">期間: {since} ～ {today}</div>
<div class="meta">
  【条件】RSI(2)≤{RSI2_ENTRY:.0f} + MA{MA_TREND}上 → 翌日始値エントリー
  【決済】RSI(2)≥{RSI2_EXIT:.0f} / ATR×{ATR_TRAIL_MULT}トレイル /
  -{HARD_STOP_PCT:.0f}%損切り / +{HALF_PROFIT_PCT:.0f}%半分利確
</div>

<div class="cards">
  <div class="card"><div class="clabel">損益合計</div>
    <div class="cval {total_cls}">{total:+,.0f}円</div></div>
  <div class="card"><div class="clabel">勝率</div>
    <div class="cval neu">{wr:.1f}%</div></div>
  <div class="card"><div class="clabel">プロフィットF</div>
    <div class="cval neu">{pf_s}</div></div>
  <div class="card"><div class="clabel">トレード数</div>
    <div class="cval neu">{len(trades)}回</div></div>
  <div class="card"><div class="clabel">勝/負</div>
    <div class="cval neu">{len(wins)}勝{len(loss)}負</div></div>
  <div class="card"><div class="clabel">平均保有(勝)</div>
    <div class="cval pos">{wh:.1f}日</div></div>
  <div class="card"><div class="clabel">平均保有(負)</div>
    <div class="cval neg">{lh:.1f}日</div></div>
</div>

<div class="chart-wrap">
  <canvas id="cumChart" height="80"></canvas>
</div>

<table>
<thead><tr>
  <th>#</th><th>エントリー</th><th>エグジット</th>
  <th>買値</th><th>売値</th><th>株数</th>
  <th>損益</th><th>変化率</th><th>保有日数</th><th>決済理由</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>

<div class="footer">生成: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>

<script>
new Chart(document.getElementById('cumChart'), {{
  type: 'line',
  data: {{
    labels: {chart_dates},
    datasets: [{{
      label: '累積損益（円）',
      data: {chart_cum},
      borderColor: '#3a86ff',
      backgroundColor: 'rgba(58,134,255,0.08)',
      borderWidth: 2,
      pointRadius: 3,
      pointBackgroundColor: '#3a86ff',
      fill: true,
      tension: 0.3,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ labels: {{ color: '#aaa' }} }},
      tooltip: {{ callbacks: {{ label: c => c.parsed.y.toLocaleString() + '円' }} }}
    }},
    scales: {{
      x: {{ ticks: {{ color: '#666', maxTicksLimit: 12 }}, grid: {{ color: '#1e2030' }} }},
      y: {{ ticks: {{ color: '#666', callback: v => v.toLocaleString() + '円' }},
             grid: {{ color: '#1e2030' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""

    path = Path(f"rsi2_{symbol.replace('.','_')}_{datetime.today().strftime('%Y%m%d')}_{label}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ── メイン ──────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="RSI(2) 平均回帰バックテスト（1銘柄）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python rsi2.py 7011.T                # 三菱重工（1年）
  python rsi2.py 7203.T --years 3      # トヨタ 3年
  python rsi2.py 9984.T --months 6     # SBG 6ヶ月
  python rsi2.py 6758.T --days 90      # ソニー 90日
""")
    parser.add_argument("symbol", help="銘柄コード（例: 7011.T）")
    parser.add_argument("--days",   type=int, default=None, help="バックテスト日数")
    parser.add_argument("--months", type=int, default=None, help="バックテスト月数")
    parser.add_argument("--years",  type=int, default=None, help="バックテスト年数")
    args = parser.parse_args()

    # 期間を日数とラベルに変換
    if args.days is not None:
        days, label = args.days, f"{args.days}日"
    elif args.months is not None:
        days, label = args.months * 30, f"{args.months}ヶ月"
    elif args.years is not None:
        days, label = args.years * 365, f"{args.years}年"
    else:
        days, label = BACKTEST_DAYS, "1年"

    sym = args.symbol.upper()
    if not sym.endswith(".T"):
        sym += ".T"

    print(f"\n  データ取得中: {sym} ...")
    df = fetch(sym, days)
    if df is None:
        print(f"  エラー: {sym} のデータ取得に失敗しました\n")
        return

    df     = calc(df)
    trades = backtest(df, days)
    print_result(sym, trades, days, label)

    path = generate_html(sym, trades, days, label)
    print(f"  HTMLレポート保存: {path}")
    webbrowser.open(f"file://{path.resolve()}")


if __name__ == "__main__":
    main()
