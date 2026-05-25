"""
analyze_nikkei_trend.py  ―  日経平均トレンド期間分析 + エントリータイミング分析

MA10 / MA25 のクロスを使ってトレンド転換を検出し、
上昇・下落それぞれの期間の長さ・騰落率を集計する。
さらに、上昇トレンド内のエントリータイミング（いつ入っていつ出るか）も分析。HTMLレポートを生成。

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
    """各日のトレンドラベルを返す: 'up' / 'down' / 'sideways'"""
    ma10 = close.rolling(10).mean()
    ma25 = close.rolling(25).mean()
    trend = pd.Series("sideways", index=close.index)
    trend[(close > ma10) & (ma10 > ma25)] = "up"
    trend[(close < ma10) & (ma10 < ma25)] = "down"
    return trend


# ─────────────────────────────────────────────────────────────────────────────
# トレンド期間抽出
# ─────────────────────────────────────────────────────────────────────────────

def extract_periods(close: pd.Series, trend: pd.Series) -> list[dict]:
    """連続するトレンド区間を抽出（横ばい含む）。期間内の最安値・最大下落幅も記録。"""
    periods = []
    cur_trend = None
    start_idx = None

    def _make(cur_trend, start_idx, end_idx, is_current=False):
        start_price = float(close.iloc[start_idx])
        end_price   = float(close.iloc[end_idx])
        start_date  = close.index[start_idx].date()
        end_date    = _TODAY if is_current else close.index[end_idx].date()
        days        = (end_date - start_date).days
        pct         = (end_price / start_price - 1) * 100
        seg         = close.iloc[start_idx:end_idx + 1]
        min_price   = float(seg.min())
        max_price   = float(seg.max())
        max_drop    = (min_price / start_price - 1) * 100
        max_rise    = (max_price / start_price - 1) * 100
        return {
            "trend": cur_trend, "start": start_date, "end": end_date,
            "days": days, "pct": pct,
            "start_price": start_price, "end_price": end_price,
            "min_price": min_price, "max_price": max_price,
            "max_drop": max_drop, "max_rise": max_rise,
            "is_current": is_current,
        }

    for i in range(len(trend)):
        t = trend.iloc[i]
        if t != cur_trend:
            if cur_trend is not None:
                periods.append(_make(cur_trend, start_idx, i - 1))
            cur_trend = t
            start_idx = i

    if cur_trend is not None and start_idx is not None:
        periods.append(_make(cur_trend, start_idx, len(trend) - 1, is_current=True))

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


# ─────────────────────────────────────────────────────────────────────────────
# エントリータイミング分析
# ─────────────────────────────────────────────────────────────────────────────

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
                _append_up(close, start_idx, i - 1, periods)
            cur_trend = t
            start_idx = i

    if cur_trend == "up" and start_idx is not None:
        _append_up(close, start_idx, n - 1, periods, is_current=True)

    return periods


def _append_up(close, start_idx, end_idx, periods, is_current=False):
    start_p   = float(close.iloc[start_idx])
    end_p     = float(close.iloc[end_idx])
    start_date = close.index[start_idx].date()
    end_date   = _TODAY if is_current else close.index[end_idx].date()
    days      = (end_date - start_date).days
    total_pct = (end_p / start_p - 1) * 100

    look_back    = max(0, start_idx - 30)
    pre_seg      = close.iloc[look_back:start_idx + 1]
    true_low_idx = int(pre_seg.values.argmin())
    true_low_p   = float(pre_seg.iloc[true_low_idx])
    lag_bars     = start_idx - (look_back + true_low_idx)
    lag_pct      = (start_p / true_low_p - 1) * 100

    daily_rets = {}
    for n_days in [1, 3, 5, 10, 15, 20, 30]:
        idx = start_idx + n_days
        if idx <= end_idx:
            daily_rets[n_days] = (float(close.iloc[idx]) / start_p - 1) * 100
        else:
            daily_rets[n_days] = None

    periods.append({
        "start_date":  start_date,
        "end_date":    end_date,
        "start_p":     start_p,
        "end_p":       end_p,
        "days":        days,
        "total_pct":   total_pct,
        "true_low_p":  true_low_p,
        "lag_bars":    lag_bars,
        "lag_pct":     lag_pct,
        "daily_rets":  daily_rets,
        "is_current":  is_current,
        "n_bars":      end_idx - start_idx + 1,
    })


def survival_curve(periods: list[dict]) -> dict[int, float]:
    completed = [p for p in periods if not p["is_current"]]
    if not completed:
        return {}
    result = {}
    for n in range(1, 51):
        still_up = sum(1 for p in completed if p["n_bars"] > n)
        result[n] = still_up / len(completed) * 100
    return result


def entry_stats(periods: list[dict]) -> dict[int, dict]:
    completed = [p for p in periods if not p["is_current"]]
    result = {}
    for n in [1, 3, 5, 10, 15, 20, 30]:
        valid = [p for p in completed if p["daily_rets"].get(n) is not None]
        if not valid:
            continue
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


def downtrend_risk(periods: list[dict]) -> dict[int, float]:
    risk = {}
    completed = [p for p in periods if not p["is_current"]]
    for n in [1, 3, 5, 10, 15, 20, 30]:
        total = sum(1 for p in completed if p["n_bars"] > n)
        fell  = sum(1 for p in completed if p["n_bars"] > n and p["n_bars"] <= n + 5)
        if total > 0:
            risk[n] = fell / total * 100
    return risk


# ─────────────────────────────────────────────────────────────────────────────
# HTML生成
# ─────────────────────────────────────────────────────────────────────────────

def build_html(close: pd.Series, trend: pd.Series, periods: list[dict],
               up_periods_timing: list[dict], years: int) -> str:
    up_periods   = [p for p in periods if p["trend"] == "up"]
    down_periods = [p for p in periods if p["trend"] == "down"]
    su = calc_stats(up_periods)
    sd = calc_stats(down_periods)
    today_str   = str(_TODAY)
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
        is_last  = p.get("is_current", False)
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

    # ── エントリータイミング分析 ───────────────────────────────────────────────
    surv    = survival_curve(up_periods_timing)
    entry_s = entry_stats(up_periods_timing)
    d_risk  = downtrend_risk(up_periods_timing)

    completed_up = [p for p in up_periods_timing if not p["is_current"]]
    lags         = [p["lag_bars"] for p in completed_up if p["lag_bars"] >= 0]
    lag_pcts     = [p["lag_pct"]  for p in completed_up if p["lag_pct"] >= 0]
    avg_lag      = sum(lags) / len(lags) if lags else 0
    avg_lagpct   = sum(lag_pcts) / len(lag_pcts) if lag_pcts else 0
    avg_up_days  = sum(p["days"] for p in completed_up) / len(completed_up) if completed_up else 0
    avg_up_pct   = sum(p["total_pct"] for p in completed_up) / len(completed_up) if completed_up else 0

    safe_end = next((n for n in sorted(d_risk) if d_risk[n] > 25), 30)

    # 生存曲線テーブル
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

    # エントリータイミングテーブル
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

    # 全上昇区間テーブル（ラグ付き）
    timing_rows = ""
    for p in reversed(up_periods_timing):
        is_c   = p["is_current"]
        active = "font-weight:700;" if is_c else ""
        lc     = "#4ade80" if p["total_pct"] >= 0 else "#f87171"
        lag_c  = "#f87171" if p["lag_pct"] > 3 else "#94a3b8"
        timing_rows += f"""<tr style="{active}">
  <td>{p['start_date']}</td>
  <td>{p['end_date']}{'　▶現在' if is_c else ''}</td>
  <td style="text-align:right">{p['days']}日</td>
  <td style="text-align:right;color:{lc}">{p['total_pct']:+.1f}%</td>
  <td style="text-align:right">{p['start_p']:,.0f}</td>
  <td style="text-align:right;color:{lag_c}">{p['lag_bars']}営業日 / {p['lag_pct']:+.1f}%</td>
</tr>"""

    # ── 現在の上昇トレンド状況（エントリータイミング視点）─────────────────────
    cur_up = next((p for p in reversed(up_periods_timing) if p["is_current"]), None)
    cur_up_block = ""
    if cur_up:
        cur_days   = cur_up["days"]
        surv_now   = surv.get(cur_days, None)
        dr_now     = d_risk.get(min(cur_days, max(d_risk.keys()) if d_risk else 30), 0)
        surv_str   = f"{surv_now:.0f}%" if surv_now is not None else "—"
        surv_c     = "#4ade80" if (surv_now or 0) > 60 else ("#fbbf24" if (surv_now or 0) > 30 else "#f87171")
        dr_c       = "#f87171" if dr_now > 30 else ("#fbbf24" if dr_now > 15 else "#4ade80")
        status_msg = ""
        if cur_days <= safe_end:
            status_msg = f'<span style="color:#4ade80">✅ 推奨エントリーウィンドウ内（〜{safe_end}日目）</span>'
        else:
            status_msg = f'<span style="color:#f87171">⚠️ {safe_end}日目を超過 — 新規エントリーは慎重に</span>'
        cur_up_block = f"""
<div class="info-box" style="border-color:#166534">
  <div style="font-weight:700;color:#4ade80;margin-bottom:10px">📈 現在の上昇トレンド（エントリータイミング）</div>
  <div class="stat-grid">
    <div class="stat-item"><span class="stat-label">開始日</span><span class="stat-val">{cur_up['start_date']}</span></div>
    <div class="stat-item"><span class="stat-label">経過日数</span><span class="stat-val">{cur_days}日</span></div>
    <div class="stat-item"><span class="stat-label">開始値</span><span class="stat-val">{cur_up['start_p']:,.0f}円</span></div>
    <div class="stat-item"><span class="stat-label">現在値</span><span class="stat-val">{cur_price:,.0f}円</span></div>
    <div class="stat-item"><span class="stat-label">確認ラグ</span><span class="stat-val">{cur_up['lag_bars']}営業日</span></div>
    <div class="stat-item"><span class="stat-label">乗り遅れ幅</span><span class="stat-val" style="color:#fbbf24">+{cur_up['lag_pct']:.1f}%</span></div>
    <div class="stat-item"><span class="stat-label">まだ継続確率</span><span class="stat-val" style="color:{surv_c}">{surv_str}</span></div>
    <div class="stat-item"><span class="stat-label">5日内転換リスク</span><span class="stat-val" style="color:{dr_c}">{dr_now:.0f}%</span></div>
  </div>
  <div style="margin-top:12px;padding:8px 12px;background:#0f172a;border-radius:6px;font-size:0.88rem">
    {status_msg}
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日経平均トレンド分析 — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif;
          background:#0f172a; color:#e2e8f0; padding:24px; max-width:1060px; margin:0 auto; }}
  h1 {{ color:#60a5fa; font-size:1.6rem; margin-bottom:4px; }}
  h2 {{ color:#60a5fa; font-size:1.1rem; margin:28px 0 12px;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:24px; }}
  .stat-grid {{ display:flex; flex-wrap:wrap; gap:10px; }}
  .stat-item {{ display:flex; flex-direction:column; min-width:100px; }}
  .stat-label {{ font-size:0.72rem; color:#64748b; }}
  .stat-val   {{ font-size:1rem; font-weight:600; }}
  .current-box {{ border:1px solid; border-radius:10px; padding:18px; margin-bottom:16px; }}
  .info-box {{ background:#0d1424; border:1px solid #1e3a5f; border-radius:10px;
               padding:16px 20px; margin-bottom:16px; }}
  .kpi-grid {{ display:flex; flex-wrap:wrap; gap:16px; margin-bottom:20px; }}
  .kpi {{ background:#111827; border:1px solid #1e293b; border-radius:8px;
          padding:14px 18px; min-width:160px; flex:1; }}
  .kpi-label {{ font-size:0.75rem; color:#64748b; margin-bottom:4px; }}
  .kpi-val   {{ font-size:1.3rem; font-weight:700; }}
  .rec-box {{ background:#052e16; border:1px solid #166534; border-radius:8px;
              padding:16px 20px; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.83rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:7px 10px;
        border:1px solid #334155; text-align:center; white-space:nowrap; }}
  td {{ padding:5px 10px; border:1px solid #1e293b; }}
  tr:hover td {{ filter:brightness(1.15); }}
  .section-sep {{ border-top:2px solid #1e293b; margin:36px 0 28px; }}
</style>
</head>
<body>
<h1>日経平均 トレンド分析 + エントリータイミング</h1>
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

<div class="section-sep"></div>

<h1 style="margin-bottom:4px">上昇トレンド エントリータイミング分析</h1>
<p class="subtitle">上昇トレンド {len(completed_up)}回（完結） + 現在継続中 {len(up_periods_timing)-len(completed_up)}回</p>

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
      <div class="kpi-val">{avg_up_days:.0f}日</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">上昇トレンド平均騰落</div>
      <div class="kpi-val" style="color:#4ade80">+{avg_up_pct:.1f}%</div>
    </div>
  </div>
</div>

{cur_up_block}

<h2>推奨エントリーウィンドウ</h2>
<div class="rec-box">
  <div style="font-size:1.05rem;font-weight:700;color:#4ade80;margin-bottom:10px">
    ✅ シグナル確認後 1〜{safe_end}日目 が最も効率的
  </div>
  <ul style="color:#94a3b8;font-size:0.88rem;line-height:2;padding-left:1.4em">
    <li>シグナル確認直後（1〜3日目）: 乗り遅れ幅が小さく残りリターンが大きい</li>
    <li>{safe_end}日目以降: 下落転換リスクが上昇し始める（下表参照）</li>
    <li>トレンド開始から中央値（{su.get('med_days',0)}日）を超えたら新規エントリーは慎重に</li>
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
<table style="max-width:460px">
<thead><tr><th>経過日数</th><th>まだ上昇トレンド中の確率</th></tr></thead>
<tbody>{surv_rows}</tbody>
</table>

<h2>全上昇トレンド区間（確認ラグ付き）</h2>
<table>
<thead><tr>
  <th>開始日</th><th>終了日</th><th>期間</th><th>騰落率</th>
  <th>開始値(円)</th><th>確認ラグ（底値→シグナル）</th>
</tr></thead>
<tbody>{timing_rows}</tbody>
</table>

<p style="color:#334155;font-size:0.75rem;margin-top:24px">
  ※ 判定: 終値&gt;MA10&gt;MA25=上昇(▲) ／ 終値&lt;MA10&lt;MA25=下落(▼) ／ それ以外=横ばい(→ MAが交差中の移行期間)<br>
  ※ 「中央値まであと〇日」「まだ継続確率」は過去の統計であり、将来のトレンド継続を保証しません。
</p>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="日経平均トレンド期間分析")
    parser.add_argument("--years",      type=int, default=5)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print(f"日経平均トレンド分析 (過去{args.years}年)...", flush=True)
    close   = fetch_n225(args.years)
    trend   = label_trend(close)
    periods = extract_periods(close, trend)
    up_timing = extract_up_periods(close, trend)

    up_periods   = [p for p in periods if p["trend"] == "up"]
    down_periods = [p for p in periods if p["trend"] == "down"]

    print_stats("上昇トレンド", up_periods)
    print_stats("下落トレンド", down_periods)

    if periods:
        last = periods[-1]
        trend_ja = {"up": "上昇", "down": "下落", "sideways": "横ばい"}[last["trend"]]
        print(f"\n現在: {trend_ja} {last['days']}日継続中 ({last['pct']:+.1f}%)")

    today_str = str(_TODAY)
    html_path = Path(f"nikkei_trend_{today_str}.html")
    html_path.write_text(build_html(close, trend, periods, up_timing, args.years), encoding="utf-8")
    print(f"生成: {html_path}")

    if not args.no_browser:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
