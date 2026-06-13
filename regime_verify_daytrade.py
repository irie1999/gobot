"""
regime_verify_daytrade.py  ―  相場局面判定の検証
==================================================================
逆指値ロング (regime_verify.py) を5分足デイトレ用に移植。

【役割】
nikkei_filter_daytrade.detect_regime のロジックを過去データで検証。
局面判定の精度・遷移パターンを HTML 可視化。

【使い方】
  python regime_verify_daytrade.py
  python regime_verify_daytrade.py --days 365
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))

REGIME_LABELS = {
    "up": "上昇", "sideways": "横ばい", "down": "下落",
    "high": "高ボラ", "mid": "中ボラ", "low": "低ボラ",
}
REGIME_COLORS = {
    "up": "#4ade80", "sideways": "#fbbf24", "down": "#f87171",
    "high": "#f87171", "mid": "#fbbf24", "low": "#4ade80",
}


def fetch_daily_regime(days):
    """日経 日次のレジーム遷移を取得。"""
    import yfinance as yf
    df = yf.download("^N225", period=f"{days+60}d", interval="1d",
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        return []

    close = df["Close"].squeeze()
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]

    ma10 = close.rolling(10).mean()
    ma25 = close.rolling(25).mean()
    rolling_vol = close.pct_change().rolling(14).std() * 100

    records = []
    cutoff = datetime.now(JST).date() - timedelta(days=days)
    for dt in close.index:
        d = dt.date() if hasattr(dt, "date") else dt
        if d < cutoff:
            continue
        try:
            c = float(close.loc[dt])
            m10 = float(ma10.loc[dt])
            m25 = float(ma25.loc[dt])
            v = float(rolling_vol.loc[dt])
        except Exception:
            continue
        if pd.isna(m10) or pd.isna(m25) or pd.isna(v):
            continue
        if c > m10 and m10 > m25:
            trend = "up"
        elif c < m10 and m10 < m25:
            trend = "down"
        else:
            trend = "sideways"
        if v > 1.5:
            vol_l = "high"
        elif v > 0.8:
            vol_l = "mid"
        else:
            vol_l = "low"
        records.append({
            "date": d,
            "close": c,
            "ma10": m10,
            "ma25": m25,
            "vol_14d": v,
            "trend": trend,
            "vol_level": vol_l,
        })
    return records


def main():
    parser = argparse.ArgumentParser(description="相場局面判定 検証")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    records = fetch_daily_regime(args.days)
    if not records:
        print("[error] データ取得失敗")
        return

    today = datetime.now(JST).strftime("%Y-%m-%d")

    # 遷移統計
    counts = {}
    for r in records:
        k = (r["trend"], r["vol_level"])
        counts[k] = counts.get(k, 0) + 1
    total = len(records)

    # 連続日数 (同一局面の継続)
    streaks = {}
    current = None
    streak = 0
    for r in records:
        k = (r["trend"], r["vol_level"])
        if k == current:
            streak += 1
        else:
            if current is not None:
                streaks.setdefault(current, []).append(streak)
            current = k
            streak = 1
    if current:
        streaks.setdefault(current, []).append(streak)

    # 統計HTML
    stat_rows = ""
    for k, n in sorted(counts.items(), key=lambda x: -x[1]):
        trend, vol = k
        pct = n / total * 100
        sks = streaks.get(k, [])
        avg_sk = sum(sks) / len(sks) if sks else 0
        max_sk = max(sks) if sks else 0
        stat_rows += f"""
<tr>
  <td><span style="color:{REGIME_COLORS[trend]}">{REGIME_LABELS[trend]}</span> /
      <span style="color:{REGIME_COLORS[vol]}">{REGIME_LABELS[vol]}</span></td>
  <td>{n}日</td>
  <td>{pct:.1f}%</td>
  <td>{avg_sk:.1f}日</td>
  <td>{max_sk}日</td>
</tr>"""

    # 日次遷移テーブル (直近60日)
    transition_rows = ""
    for r in records[-60:]:
        transition_rows += f"""
<tr>
  <td>{r["date"]}</td>
  <td>{r["close"]:,.0f}</td>
  <td>{r["ma10"]:,.0f}</td>
  <td>{r["ma25"]:,.0f}</td>
  <td>{r["vol_14d"]:.2f}%</td>
  <td><span style="color:{REGIME_COLORS[r["trend"]]}">{REGIME_LABELS[r["trend"]]}</span></td>
  <td><span style="color:{REGIME_COLORS[r["vol_level"]]}">{REGIME_LABELS[r["vol_level"]]}</span></td>
</tr>"""

    css = """
body { font-family: "Segoe UI", "Hiragino Sans", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 20px; }
h1 { color: #10b981; font-size: 1.5rem; }
h2 { color: #10b981; font-size: 1.1rem; margin-top: 24px;
     border-left: 3px solid #10b981; padding-left: 10px; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem;
        margin-bottom: 14px; }
th { background: #1e293b; color: #94a3b8; padding: 6px 8px;
     text-align: center; border: 1px solid #334155; }
td { padding: 5px 8px; border: 1px solid #1e293b;
     text-align: center; }
.subtitle { color: #94a3b8; font-size: 0.85rem; margin-bottom: 20px; }
"""

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>相場局面検証 — {today}</title>
<style>{css}</style></head><body>
<h1>📊 相場局面 判定検証 (日経225)</h1>
<p class="subtitle">生成: {today} / 期間: 過去{args.days}日 / 営業日: {total}日</p>

<h2>局面別 出現頻度・継続日数</h2>
<table>
  <thead><tr>
    <th>局面</th><th>出現日数</th><th>割合</th>
    <th>平均継続</th><th>最長継続</th>
  </tr></thead>
  <tbody>{stat_rows}</tbody>
</table>
<p style="color:#64748b;font-size:0.78rem">
  💡 平均継続が長い局面 = 安定して機能する戦略を運用可<br>
  💡 高頻度の局面 = その局面用 winners を強化する価値あり
</p>

<h2>日次遷移 (直近60日)</h2>
<table>
  <thead><tr>
    <th>日付</th><th>終値</th><th>MA10</th><th>MA25</th>
    <th>14日ボラ</th><th>トレンド</th><th>ボラ</th>
  </tr></thead>
  <tbody>{transition_rows}</tbody>
</table>
</body></html>"""

    out = Path(f"regime_verify_daytrade_{today}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
