"""
analyze_nikkei_trend.py  ―  日経平均トレンド期間分析

MA10 / MA25 のクロスを使ってトレンド転換を検出し、
上昇・下落それぞれの期間の長さ・騰落率を集計する。HTMLレポートを生成。

select_signals.py と同じ判定基準:
  上昇 = 終値 > MA10 かつ MA10 > MA25
  下落 = 終値 < MA10 かつ MA10 < MA25
  横ばい = それ以外

Usage:
    python analyze_nikkei_trend.py              # 過去5年 HTMLレポート
    python analyze_nikkei_trend.py --years 10
    python analyze_nikkei_trend.py --no-browser # HTML生成のみ
"""
from __future__ import annotations
import argparse
import webbrowser
from datetime import timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

JST = timezone(timedelta(hours=9))


def fetch_n225(years: int) -> pd.Series:
    df = yf.download("^N225", period=f"{years * 365 + 30}d", interval="1d",
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError("日経データ取得失敗")
    close = df["Close"].squeeze()
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.dropna().sort_index()


def label_trend(close: pd.Series) -> pd.Series:
    """各日のトレンドラベルを返す: 'up' / 'down' / 'sideways'"""
    ma10 = close.rolling(10).mean()
    ma25 = close.rolling(25).mean()
    trend = pd.Series("sideways", index=close.index)
    trend[(close > ma10) & (ma10 > ma25)] = "up"
    trend[(close < ma10) & (ma10 < ma25)] = "down"
    return trend


def extract_periods(close: pd.Series, trend: pd.Series) -> list[dict]:
    """連続するトレンド区間を抽出（横ばい含む）。期間内の最安値・最大下落幅も記録。"""
    periods = []
    cur_trend = None
    start_idx = None

    def _make(cur_trend, start_idx, end_idx):
        start_price = float(close.iloc[start_idx])
        end_price   = float(close.iloc[end_idx])
        start_date  = close.index[start_idx].date()
        end_date    = close.index[end_idx].date()
        days        = (end_date - start_date).days
        pct         = (end_price / start_price - 1) * 100
        seg         = close.iloc[start_idx:end_idx + 1]
        min_price   = float(seg.min())
        max_price   = float(seg.max())
        max_drop    = (min_price / start_price - 1) * 100   # 開始値からの最大下落率
        max_rise    = (max_price / start_price - 1) * 100   # 開始値からの最大上昇率
        return {
            "trend": cur_trend, "start": start_date, "end": end_date,
            "days": days, "pct": pct,
            "start_price": start_price, "end_price": end_price,
            "min_price": min_price, "max_price": max_price,
            "max_drop": max_drop, "max_rise": max_rise,
        }

    for i in range(len(trend)):
        t = trend.iloc[i]
        if t != cur_trend:
            if cur_trend is not None:
                periods.append(_make(cur_trend, start_idx, i - 1))
            cur_trend = t
            start_idx = i

    if cur_trend is not None and start_idx is not None:
        periods.append(_make(cur_trend, start_idx, len(trend) - 1))

    return periods


def calc_stats(periods: list[dict]) -> dict:
    if not periods:
        return {}
    days_list = [p["days"] for p in periods]
    pct_list  = [p["pct"]  for p in periods]
    return {
        "count":    len(periods),
        "avg_days": sum(days_list) / len(days_list),
        "med_days": sorted(days_list)[len(days_list) // 2],
        "max_days": max(days_list),
        "min_days": min(days_list),
        "avg_pct":  sum(pct_list) / len(pct_list),
        "days_list": days_list,
    }


def print_stats(label: str, periods: list[dict]) -> None:
    s = calc_stats(periods)
    if not s:
        print(f"  {label}: データなし")
        return
    print(f"  {label}: {s['count']}回")
    print(f"    期間: 平均 {s['avg_days']:.0f}日 / 中央値 {s['med_days']}日 / 最短 {s['min_days']}日 / 最長 {s['max_days']}日")
    print(f"    騰落: 平均 {s['avg_pct']:+.1f}%")
    buckets = [(0,10),(10,20),(20,30),(30,60),(60,90),(90,180),(180,9999)]
    dist = []
    for lo, hi in buckets:
        cnt = sum(1 for d in s["days_list"] if lo <= d < hi)
        if cnt:
            lbl = f"{lo}〜{hi-1}日" if hi < 9999 else f"{lo}日以上"
            dist.append(f"{lbl}:{cnt}回")
    print(f"    分布: {' / '.join(dist)}")


def build_html(close: pd.Series, trend: pd.Series, periods: list[dict], years: int) -> str:
    up_periods   = [p for p in periods if p["trend"] == "up"]
    down_periods = [p for p in periods if p["trend"] == "down"]
    su = calc_stats(up_periods)
    sd = calc_stats(down_periods)
    today_str   = str(close.index[-1].date())
    cur_price   = float(close.iloc[-1])
    cur_trend   = trend.iloc[-1]
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[cur_trend]
    trend_ja    = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}[cur_trend]

    # ── 現在トレンド継続状況 ──────────────────────────────────────────────────
    last = periods[-1] if periods else None
    current_block = ""
    if last:
        ref_stats = su if last["trend"] == "up" else sd
        med = ref_stats.get("med_days", 0)
        avg = ref_stats.get("avg_days", 0)
        remaining = med - last["days"]
        remain_str = (f"中央値まであと <strong>{remaining}日</strong>（参考値）"
                      if remaining > 0
                      else f"中央値({med}日)を超過中 → 転換注意（参考値）")
        pct_c = "#4ade80" if last["pct"] >= 0 else "#f87171"
        last_ja = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[last["trend"]]
        current_block = f"""
<div class="current-box" style="border-color:{'#166534' if last['trend']=='up' else '#991b1b'}">
  <div style="font-size:1.1rem;font-weight:700;margin-bottom:10px;color:{trend_color}">
    現在: {last_ja}トレンド継続中
  </div>
  <div class="stat-grid">
    <div class="stat-item"><span class="stat-label">開始日</span><span class="stat-val">{last['start']}</span></div>
    <div class="stat-item"><span class="stat-label">継続日数</span><span class="stat-val">{last['days']}日</span></div>
    <div class="stat-item"><span class="stat-label">開始値</span><span class="stat-val">{last['start_price']:,.0f}円</span></div>
    <div class="stat-item"><span class="stat-label">現在値</span><span class="stat-val">{cur_price:,.0f}円</span></div>
    <div class="stat-item"><span class="stat-label">騰落率</span>
      <span class="stat-val" style="color:{pct_c}">{last['pct']:+.1f}%</span></div>
    <div class="stat-item"><span class="stat-label">平均期間</span><span class="stat-val">{avg:.0f}日</span></div>
    <div class="stat-item"><span class="stat-label">中央値期間</span><span class="stat-val">{med}日</span></div>
  </div>
  <div style="margin-top:12px;padding:10px;background:#0f172a;border-radius:6px;font-size:0.88rem;color:#fbbf24">
    📊 {remain_str}
  </div>
</div>"""

    # ── 統計カード ─────────────────────────────────────────────────────────────
    def stat_card(title: str, s: dict, color: str, bg: str) -> str:
        if not s:
            return ""
        buckets = [(0,10),(10,20),(20,30),(30,60),(60,90),(90,180),(180,9999)]
        bar_rows = ""
        max_cnt = 1
        dist_data = []
        for lo, hi in buckets:
            cnt = sum(1 for d in s["days_list"] if lo <= d < hi)
            if cnt:
                lbl = f"{lo}〜{hi-1}日" if hi < 9999 else f"{lo}日以上"
                dist_data.append((lbl, cnt))
                max_cnt = max(max_cnt, cnt)
        for lbl, cnt in dist_data:
            w = int(cnt / max_cnt * 100)
            bar_rows += f"""
<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:0.82rem">
  <span style="width:80px;color:#94a3b8;flex-shrink:0">{lbl}</span>
  <div style="flex:1;background:#1e293b;border-radius:3px;height:14px">
    <div style="width:{w}%;background:{color};height:100%;border-radius:3px"></div>
  </div>
  <span style="width:30px;text-align:right;color:#e2e8f0">{cnt}回</span>
</div>"""
        return f"""
<div style="background:{bg};border:1px solid {color}44;border-radius:10px;padding:18px;flex:1;min-width:280px">
  <div style="color:{color};font-weight:700;font-size:1.05rem;margin-bottom:12px">{title}</div>
  <div class="stat-grid" style="margin-bottom:14px">
    <div class="stat-item"><span class="stat-label">回数</span><span class="stat-val">{s['count']}回</span></div>
    <div class="stat-item"><span class="stat-label">平均期間</span><span class="stat-val">{s['avg_days']:.0f}日</span></div>
    <div class="stat-item"><span class="stat-label">中央値</span><span class="stat-val">{s['med_days']}日</span></div>
    <div class="stat-item"><span class="stat-label">最短</span><span class="stat-val">{s['min_days']}日</span></div>
    <div class="stat-item"><span class="stat-label">最長</span><span class="stat-val">{s['max_days']}日</span></div>
    <div class="stat-item"><span class="stat-label">平均騰落</span>
      <span class="stat-val" style="color:{color}">{s['avg_pct']:+.1f}%</span></div>
  </div>
  <div style="font-size:0.78rem;color:#64748b;margin-bottom:6px">期間分布</div>
  {bar_rows}
</div>"""

    up_card   = stat_card("上昇トレンド ▲", su, "#4ade80", "#052e16")
    down_card = stat_card("下落トレンド ▼", sd, "#f87171", "#2d0a0a")

    # ── 全期間テーブル ─────────────────────────────────────────────────────────
    rows = ""
    for p in reversed(periods):
        t        = p["trend"]
        is_last  = (p is periods[-1])
        if t == "up":
            tc, mark, bg, border = "#4ade80", "▲ 上昇", "background:#052e1620;", "border-left:3px solid #4ade80;"
        elif t == "down":
            tc, mark, bg, border = "#f87171", "▼ 下落", "background:#2d0a0a20;", "border-left:3px solid #f87171;"
        else:
            tc, mark, bg, border = "#fbbf24", "→ 横ばい", "background:#2d1f0020;", "border-left:3px solid #fbbf24;"
        active   = "font-weight:700;" if is_last else ""
        drop_val = p.get("max_drop", 0.0)
        drop_str = f"{drop_val:+.1f}%" if drop_val else "—"
        drop_c   = "#f87171" if drop_val < -2 else "#94a3b8"
        # 横ばいで大きな下落があった場合に注記
        note = ""
        if t == "sideways" and drop_val < -3:
            note = f'<span style="color:#f87171;font-size:0.75rem"> ⚠️V字{drop_val:.0f}%</span>'
        rows += f"""<tr style="{bg}{active}">
  <td style="color:{tc};{border}padding-left:10px">{mark}{note}</td>
  <td>{p['start']}</td>
  <td>{p['end']}{'　▶現在' if is_last else ''}</td>
  <td style="text-align:right">{p['days']}日</td>
  <td style="text-align:right;color:{tc}">{p['pct']:+.1f}%</td>
  <td style="text-align:right;color:{drop_c}">{drop_str}</td>
  <td style="text-align:right">{p['start_price']:,.0f}</td>
  <td style="text-align:right">{p['min_price']:,.0f}</td>
  <td style="text-align:right">{p['end_price']:,.0f}</td>
</tr>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日経平均トレンド期間分析 — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif;
          background:#0f172a; color:#e2e8f0; padding:24px; max-width:1000px; margin:0 auto; }}
  h1 {{ color:#60a5fa; font-size:1.6rem; margin-bottom:4px; }}
  h2 {{ color:#60a5fa; font-size:1.1rem; margin:28px 0 12px;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:24px; }}
  .stat-grid {{ display:flex; flex-wrap:wrap; gap:10px; }}
  .stat-item {{ display:flex; flex-direction:column; min-width:100px; }}
  .stat-label {{ font-size:0.72rem; color:#64748b; }}
  .stat-val   {{ font-size:1rem; font-weight:600; }}
  .current-box {{ border:1px solid; border-radius:10px; padding:18px; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.83rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:7px 10px;
        border:1px solid #334155; text-align:center; white-space:nowrap; }}
  td {{ padding:5px 10px; border:1px solid #1e293b; }}
  tr:hover td {{ filter:brightness(1.15); }}
</style>
</head>
<body>
<h1>日経平均 トレンド期間分析</h1>
<p class="subtitle">
  生成日: {today_str} ／ 分析期間: {close.index[0].date()} 〜 {today_str} (過去{years}年) ／
  現在: <strong style="color:{trend_color}">{trend_ja} {cur_price:,.0f}円</strong>
</p>

<h2>現在のトレンド状況</h2>
{current_block}

<h2>トレンド統計</h2>
<div style="display:flex;flex-wrap:wrap;gap:16px">
  {up_card}
  {down_card}
</div>

<h2>全トレンド期間一覧（新しい順）</h2>
<table>
<thead><tr>
  <th>種別</th><th>開始日</th><th>終了日</th>
  <th>日数</th><th>騰落率</th><th>最大下落</th><th>開始値(円)</th><th>最安値(円)</th><th>終了値(円)</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>

<p style="color:#334155;font-size:0.75rem;margin-top:24px">
  ※ 判定: 終値&gt;MA10&gt;MA25=上昇(▲) ／ 終値&lt;MA10&lt;MA25=下落(▼) ／ それ以外=横ばい(→ MAが交差中の移行期間)<br>
  ※ 「中央値まであと〇日」は過去の統計であり、将来のトレンド継続を保証しません。
</p>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="日経平均トレンド期間分析")
    parser.add_argument("--years",      type=int, default=5)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print(f"日経平均トレンド期間分析 (過去{args.years}年)...", flush=True)
    close   = fetch_n225(args.years)
    trend   = label_trend(close)
    periods = extract_periods(close, trend)

    up_periods   = [p for p in periods if p["trend"] == "up"]
    down_periods = [p for p in periods if p["trend"] == "down"]

    # ── コンソール出力 ─────────────────────────────────────────────────────────
    print_stats("上昇トレンド", up_periods)
    print_stats("下落トレンド", down_periods)

    if periods:
        last = periods[-1]
        trend_ja = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[last["trend"]]
        print(f"\n現在: {trend_ja} {last['days']}日継続中 ({last['pct']:+.1f}%)")

    # ── HTML生成 ───────────────────────────────────────────────────────────────
    today_str = str(close.index[-1].date())
    html_path = Path(f"nikkei_trend_{today_str}.html")
    html_path.write_text(build_html(close, trend, periods, args.years), encoding="utf-8")
    print(f"生成: {html_path}")

    if not args.no_browser:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
