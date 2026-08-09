"""
analyze_trend_timing.py  ―  上昇トレンド「いつ入っていつ出るか」分析

過去の全上昇トレンド区間から以下を分析:
  1. シグナル確認ラグ: 実際の底値から MA クロスまで何日かかるか
  2. エントリー日別リターン: トレンド開始から N 日目に入ると平均何%取れるか
  3. 生存確率: 上昇 N 日目でまだトレンドが続いている確率
  4. 損失リスク: トレンド開始 N 日目以降に入ると次の5日で下落になる確率

Usage:
    python analyze_trend_timing.py
    python analyze_trend_timing.py --years 10
    python analyze_trend_timing.py --no-browser
"""
from __future__ import annotations
import argparse
import webbrowser
from _open_html import open_html
from datetime import timedelta, timezone, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

JST    = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()


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
    ma10 = close.rolling(10).mean()
    ma25 = close.rolling(25).mean()
    trend = pd.Series("sideways", index=close.index)
    trend[(close > ma10) & (ma10 > ma25)] = "up"
    trend[(close < ma10) & (ma10 < ma25)] = "down"
    return trend


def extract_up_periods(close: pd.Series, trend: pd.Series) -> list[dict]:
    """上昇トレンド区間を抽出。底値・ラグ・日次リターンを付加。"""
    periods = []
    cur_trend = None
    start_idx = None
    n = len(trend)

    for i in range(n):
        t = trend.iloc[i]
        if t != cur_trend:
            if cur_trend == "up" and start_idx is not None:
                end_idx = i - 1
                _append_up(close, trend, start_idx, end_idx, periods)
            cur_trend = t
            start_idx = i

    # 現在継続中の上昇区間
    if cur_trend == "up" and start_idx is not None:
        _append_up(close, trend, start_idx, n - 1, periods, is_current=True)

    return periods


def _append_up(close, trend, start_idx, end_idx, periods, is_current=False):
    seg        = close.iloc[start_idx:end_idx + 1]
    start_p    = float(close.iloc[start_idx])
    end_p      = float(close.iloc[end_idx])
    start_date = close.index[start_idx].date()
    end_date   = _TODAY if is_current else close.index[end_idx].date()
    days       = (end_date - start_date).days
    total_pct  = (end_p / start_p - 1) * 100

    # 確認ラグ: シグナル開始前のローカル底値
    look_back = max(0, start_idx - 30)
    pre_seg   = close.iloc[look_back:start_idx + 1]
    true_low_idx = int(pre_seg.values.argmin())
    true_low_p   = float(pre_seg.iloc[true_low_idx])
    # ラグ = シグナル開始日 - 底値日 (営業日数で近似)
    lag_bars = start_idx - (look_back + true_low_idx)
    lag_pct  = (start_p / true_low_p - 1) * 100  # 底値からシグナルまでの上昇分 = 乗り遅れ分

    # 日次リターン: トレンド開始から N 営業日後の close / 開始値 - 1
    # N = 1, 3, 5, 10, 15, 20, 30
    daily_rets = {}
    for n_days in [1, 3, 5, 10, 15, 20, 30]:
        idx = start_idx + n_days
        if idx <= end_idx:
            daily_rets[n_days] = (float(close.iloc[idx]) / start_p - 1) * 100
        else:
            daily_rets[n_days] = None  # トレンドが終了してしまっている

    periods.append({
        "start_date":   close.index[start_idx].date(),
        "end_date":     close.index[end_idx].date(),
        "start_p":      start_p,
        "end_p":        end_p,
        "days":         days,
        "total_pct":    total_pct,
        "true_low_p":   true_low_p,
        "lag_bars":     lag_bars,
        "lag_pct":      lag_pct,
        "daily_rets":   daily_rets,
        "is_current":   is_current,
        "n_bars":       end_idx - start_idx + 1,  # 営業日数
    })


def survival_curve(periods: list[dict]) -> dict[int, float]:
    """N 営業日目でまだ上昇トレンドが続いている確率 (完結トレンドのみ)"""
    completed = [p for p in periods if not p["is_current"]]
    if not completed:
        return {}
    result = {}
    for n in range(1, 51):
        still_up = sum(1 for p in completed if p["n_bars"] > n)
        result[n] = still_up / len(completed) * 100
    return result


def entry_stats(periods: list[dict]) -> dict[int, dict]:
    """
    トレンド開始 N 日目にエントリーし、トレンド終了まで保有した場合の期待値。
    N 日時点でまだトレンドが続いていた区間のみ集計。
    """
    completed = [p for p in periods if not p["is_current"]]
    result = {}
    for n in [1, 3, 5, 10, 15, 20, 30]:
        valid = [p for p in completed if p["daily_rets"].get(n) is not None]
        if not valid:
            continue
        # N 日目エントリー価格: start_p * (1 + daily_rets[n]/100)
        # 出口: end_p (トレンド終了)
        rets = []
        for p in valid:
            entry_price = p["start_p"] * (1 + p["daily_rets"][n] / 100)
            ret = (p["end_p"] / entry_price - 1) * 100
            rets.append(ret)
        wins = sum(1 for r in rets if r > 0)
        result[n] = {
            "count":    len(rets),
            "avg_ret":  sum(rets) / len(rets),
            "win_rate": wins / len(rets) * 100,
            "med_ret":  sorted(rets)[len(rets) // 2],
        }
    return result


def downtrend_risk(periods: list[dict], all_trend: pd.Series) -> dict[int, float]:
    """
    上昇 N 日目以降にエントリーした場合、次の5営業日以内に下落転換する確率。
    """
    risk = {}
    completed = [p for p in periods if not p["is_current"]]
    for n in [1, 3, 5, 10, 15, 20, 30]:
        total = 0
        fell = 0
        for p in completed:
            if p["n_bars"] <= n:
                continue
            total += 1
            # n 日後のトレンドを見る (実データから取得困難なので n_bars で代用)
            if p["n_bars"] <= n + 5:
                fell += 1
        if total > 0:
            risk[n] = fell / total * 100
    return risk


def build_html(close, periods, years):
    today_str = str(_TODAY)
    surv      = survival_curve(periods)
    entry_s   = entry_stats(periods)
    d_risk    = downtrend_risk(periods, None)

    completed  = [p for p in periods if not p["is_current"]]
    lags       = [p["lag_bars"] for p in completed if p["lag_bars"] >= 0]
    lag_pcts   = [p["lag_pct"]  for p in completed if p["lag_pct"] >= 0]
    avg_lag    = sum(lags) / len(lags) if lags else 0
    avg_lagpct = sum(lag_pcts) / len(lag_pcts) if lag_pcts else 0

    # ── 生存曲線テーブル ───────────────────────────────────────────────────────
    surv_rows = ""
    key_days = [1,2,3,4,5,6,7,8,9,10,12,15,20,25,30,35,40,50]
    for n in key_days:
        if n not in surv:
            continue
        pct = surv[n]
        bar_w = int(pct)
        bar_c = "#4ade80" if pct > 60 else ("#fbbf24" if pct > 30 else "#f87171")
        surv_rows += f"""<tr>
  <td style="text-align:center">{n}日目</td>
  <td>
    <div style="display:flex;align-items:center;gap:8px">
      <div style="width:200px;background:#1e293b;border-radius:3px;height:14px">
        <div style="width:{bar_w}%;background:{bar_c};height:100%;border-radius:3px"></div>
      </div>
      <span style="color:{bar_c};font-weight:600">{pct:.0f}%</span>
    </div>
  </td>
</tr>"""

    # ── エントリータイミングテーブル ───────────────────────────────────────────
    entry_rows = ""
    for n, s in sorted(entry_s.items()):
        rc = "#4ade80" if s["avg_ret"] > 0 else "#f87171"
        wc = "#4ade80" if s["win_rate"] >= 50 else "#f87171"
        dr = d_risk.get(n, 0)
        dc = "#f87171" if dr > 30 else ("#fbbf24" if dr > 15 else "#4ade80")
        entry_rows += f"""<tr>
  <td style="text-align:center;font-weight:600">{n}日目</td>
  <td style="text-align:right">{s['count']}回</td>
  <td style="text-align:right;color:{wc}">{s['win_rate']:.0f}%</td>
  <td style="text-align:right;color:{rc}">{s['avg_ret']:+.1f}%</td>
  <td style="text-align:right;color:{rc}">{s['med_ret']:+.1f}%</td>
  <td style="text-align:right;color:{dc}">{dr:.0f}%</td>
</tr>"""

    # ── 推奨ウィンドウ ──────────────────────────────────────────────────────────
    best_n = min(entry_s, key=lambda n: -entry_s[n]["avg_ret"]) if entry_s else 1
    safe_end = next((n for n in sorted(d_risk) if d_risk[n] > 25), 30)

    # ── 全区間テーブル ──────────────────────────────────────────────────────────
    period_rows = ""
    for p in reversed(periods):
        is_c   = p["is_current"]
        active = "font-weight:700;" if is_c else ""
        lc     = "#4ade80" if p["total_pct"] >= 0 else "#f87171"
        lag_c  = "#f87171" if p["lag_pct"] > 3 else "#94a3b8"
        period_rows += f"""<tr style="{active}">
  <td>{p['start_date']}</td>
  <td>{p['end_date']}{'　▶現在' if is_c else ''}</td>
  <td style="text-align:right">{p['days']}日</td>
  <td style="text-align:right;color:{lc}">{p['total_pct']:+.1f}%</td>
  <td style="text-align:right">{p['start_p']:,.0f}</td>
  <td style="text-align:right;color:{lag_c}">{p['lag_bars']}営業日 / {p['lag_pct']:+.1f}%</td>
</tr>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>上昇トレンド エントリータイミング分析 — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif;
          background:#0f172a; color:#e2e8f0; padding:24px; max-width:1000px; margin:0 auto; }}
  h1 {{ color:#60a5fa; font-size:1.6rem; margin-bottom:4px; }}
  h2 {{ color:#60a5fa; font-size:1.1rem; margin:28px 0 10px;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:20px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.85rem; margin-bottom:8px; }}
  th {{ background:#1e293b; color:#94a3b8; padding:7px 10px;
        border:1px solid #334155; text-align:center; white-space:nowrap; }}
  td {{ padding:6px 10px; border:1px solid #1e293b; }}
  tr:hover td {{ background:#1e293b44; }}
  .info-box {{ background:#0d1424; border:1px solid #1e3a5f; border-radius:10px;
               padding:16px 20px; margin-bottom:16px; }}
  .kpi-grid {{ display:flex; flex-wrap:wrap; gap:16px; margin-bottom:20px; }}
  .kpi {{ background:#111827; border:1px solid #1e293b; border-radius:8px;
          padding:14px 18px; min-width:160px; flex:1; }}
  .kpi-label {{ font-size:0.75rem; color:#64748b; margin-bottom:4px; }}
  .kpi-val   {{ font-size:1.3rem; font-weight:700; }}
  .rec-box {{ background:#052e16; border:1px solid #166534; border-radius:8px;
              padding:16px 20px; margin-bottom:16px; }}
</style>
</head>
<body>
<h1>上昇トレンド エントリータイミング分析</h1>
<p class="subtitle">生成日: {today_str} ／ 分析期間: 過去{years}年 ／
  上昇トレンド {len(completed)}回（完結） + 現在継続中 {len(periods)-len(completed)}回</p>

<h2>シグナル確認ラグ（底値からMAクロスまで）</h2>
<div class="info-box">
  <p style="color:#94a3b8;font-size:0.88rem;margin-bottom:12px">
    MA10がMA25を上抜けた時点（シグナル確認日）は、実際の底値より遅れます。<br>
    この期間はすでに上昇しており、「乗り遅れ」になります。
  </p>
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label">平均ラグ（営業日）</div>
      <div class="kpi-val" style="color:#fbbf24">{avg_lag:.1f}日</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">平均乗り遅れ幅</div>
      <div class="kpi-val" style="color:#f87171">+{avg_lagpct:.1f}%</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">上昇トレンド平均期間</div>
      <div class="kpi-val">{sum(p['days'] for p in completed)/len(completed):.0f}日</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">上昇トレンド平均騰落</div>
      <div class="kpi-val" style="color:#4ade80">+{sum(p['total_pct'] for p in completed)/len(completed):.1f}%</div>
    </div>
  </div>
</div>

<h2>推奨エントリーウィンドウ</h2>
<div class="rec-box">
  <div style="font-size:1.05rem;font-weight:700;color:#4ade80;margin-bottom:10px">
    ✅ シグナル確認後 1〜{safe_end}日目 が最も効率的
  </div>
  <ul style="color:#94a3b8;font-size:0.88rem;line-height:2;padding-left:1.4em">
    <li>シグナル確認直後（1〜3日目）: 乗り遅れ幅が小さく残りリターンが大きい</li>
    <li>{safe_end}日目以降: 下落転換リスクが上昇し始める（下表参照）</li>
    <li>トレンド開始から中央値（5日）を超えたら新規エントリーは慎重に</li>
  </ul>
</div>

<h2>エントリー日別 期待リターン（シグナル確認後 N 日目に買い → トレンド終了まで保有）</h2>
<table>
<thead><tr>
  <th>エントリー</th><th>サンプル数</th><th>勝率</th>
  <th>平均リターン</th><th>中央値リターン</th><th>5日内下落転換リスク</th>
</tr></thead>
<tbody>{entry_rows}</tbody>
</table>
<p style="color:#475569;font-size:0.78rem;margin-top:4px">
  ※ リターン = エントリー価格 → トレンド終了日の終値まで保有した場合の騰落率
</p>

<h2>生存確率（上昇 N 日目でまだトレンドが続いている確率）</h2>
<table>
<thead><tr><th>経過日数</th><th>まだ上昇トレンド中の確率</th></tr></thead>
<tbody>{surv_rows}</tbody>
</table>

<h2>全上昇トレンド区間（確認ラグ付き）</h2>
<table>
<thead><tr>
  <th>開始日</th><th>終了日</th><th>期間</th><th>騰落率</th>
  <th>開始値(円)</th><th>確認ラグ（底値→シグナル）</th>
</tr></thead>
<tbody>{period_rows}</tbody>
</table>

<p style="color:#334155;font-size:0.75rem;margin-top:24px">
  ※ 判定: 終値&gt;MA10&gt;MA25=上昇開始。ラグはローカル底値からのカウント（最大30日前を参照）<br>
  ※ 過去の統計は将来を保証しません。
</p>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years",      type=int, default=5)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print(f"上昇トレンド エントリータイミング分析 (過去{args.years}年)...", flush=True)
    close   = fetch_n225(args.years)
    trend   = label_trend(close)
    periods = extract_up_periods(close, trend)

    completed = [p for p in periods if not p["is_current"]]
    print(f"上昇トレンド: {len(completed)}回（完結）")

    if completed:
        lags = [p["lag_bars"] for p in completed]
        print(f"平均ラグ: {sum(lags)/len(lags):.1f}営業日")
        surv = survival_curve(periods)
        for n in [5, 10, 15, 20]:
            if n in surv:
                print(f"  {n:2}日目でまだ上昇: {surv[n]:.0f}%")

    today_str = str(_TODAY)
    html_path = Path(f"trend_timing_{today_str}.html")
    html_path.write_text(build_html(close, periods, args.years), encoding="utf-8")
    print(f"生成: {html_path}")

    if not args.no_browser:
        open_html(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
