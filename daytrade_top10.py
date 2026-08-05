"""
daytrade_top10.py  ―  厳選10銘柄 ORB シグナル & バックテスト
==================================================================
スキャンで選定した上位10銘柄専用。
シグナル確認 + バックテスト + HTML を1コマンドで実行。

【使い方】
  python daytrade_top10.py                              # バックテスト + HTML
  python daytrade_top10.py --days 60 --budget 600000    # 期間・予算指定
  python daytrade_top10.py --source local --days 365    # ローカルデータ
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from _open_html import open_html
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

# ── 厳選10銘柄 ──────────────────────────────────────────────
TOP10 = [
    ("7011.T", "三菱重工業"),
    ("9984.T", "ソフトバンクG"),
    ("7911.T", "TOPPAN HD"),
    ("4183.T", "三井化学"),
    ("4107.T", "伊勢化学"),
    ("6800.T", "ヨコオ"),
    ("7685.T", "BuySell Tech"),
    ("1975.T", "朝日工業社"),
    ("1835.T", "東鉄工業"),
    ("5832.T", "ちゅうぎんFG"),
]

# ── ORBパラメータ (最適化済み) ────────────────────────────────
OR_MINUTES       = 30
TARGET_K         = 1.5
GAP_MAX_PCT      = 2.0
TRAILING_TRIGGER = 0.5
FORCE_CLOSE      = dtime(14, 55)
ENTRY_CUTOFF     = dtime(11, 0)
AM_START         = dtime(9, 0)
BUDGET           = 600_000
MAX_RISK         = 6_000


# ─────────────────────────────────────────────────────────────
# ORB バックテスト (1日分)
# ─────────────────────────────────────────────────────────────

def backtest_orb_day(day_df, prev_close=None):
    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=OR_MINUTES)).time()
    or_bars = day_df[day_df.index.time < or_end]
    rest = day_df[day_df.index.time >= or_end]
    if len(or_bars) < 2 or rest.empty:
        return None

    or_hi = float(or_bars["high"].max())
    or_lo = float(or_bars["low"].min())
    or_w = or_hi - or_lo
    if or_w <= 0:
        return None

    open_p = float(or_bars.iloc[0]["open"])
    if prev_close and prev_close > 0:
        if abs(open_p - prev_close) / prev_close * 100 > GAP_MAX_PCT:
            return None

    stop_p = or_lo
    state = "idle"
    entry_p = target_p = 0.0
    entry_dt = exit_dt = None
    exit_p = None
    reason = None
    qty = 0
    trailing = False

    bars = list(rest.itertuples())
    for i, bar in enumerate(bars):
        ts = bar.Index
        t = ts.time()
        hi, lo, cl = float(bar.high), float(bar.low), float(bar.close)

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                exit_p, exit_dt, reason = cl, ts, "引け強制"
                break
            if not trailing and target_p > entry_p:
                if (cl - entry_p) / (target_p - entry_p) >= TRAILING_TRIGGER:
                    stop_p = entry_p
                    trailing = True
            if lo <= stop_p and hi >= target_p:
                exit_p, exit_dt = stop_p, ts
                reason = "建値撤退" if trailing else "損切り"
                break
            if hi >= target_p:
                exit_p, exit_dt, reason = target_p, ts, "目標達成"
                break
            if lo <= stop_p:
                exit_p, exit_dt = stop_p, ts
                reason = "建値撤退" if trailing else "損切り"
                break
            continue

        if t >= ENTRY_CUTOFF:
            break
        if cl > or_hi and i + 1 < len(bars):
            entry_p = float(bars[i + 1].open)
            entry_dt = bars[i + 1].Index
            target_p = entry_p + or_w * TARGET_K
            if entry_p <= stop_p:
                break
            qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)
            state = "in_pos"
            trailing = False
            nhi, nlo = float(bars[i+1].high), float(bars[i+1].low)
            if nlo <= stop_p:
                exit_p, exit_dt = stop_p, bars[i+1].Index
                reason = "損切り"
                break
            if nhi >= target_p:
                exit_p, exit_dt, reason = target_p, bars[i+1].Index, "目標達成"
                break

    if state == "in_pos" and exit_p is None:
        exit_p = float(bars[-1].close)
        exit_dt = bars[-1].Index
        reason = "引け強制"

    if exit_p is None or entry_dt is None or entry_p <= 0:
        return None

    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=entry_p, exit_p=exit_p,
        stop_p=or_lo, target_p=target_p,
        or_hi=or_hi, or_lo=or_lo, or_w=or_w,
        qty=qty, pnl=pnl, pct=pct, reason=reason,
    )


def backtest_symbol(sym, name, df):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None
    trades = []
    prev_close = None
    for d in dates:
        t = backtest_orb_day(daily[d], prev_close)
        if t:
            trades.append(t)
        prev_close = float(daily[d].iloc[-1]["close"])
    return dict(symbol=sym, name=name, trades=trades)


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0, equity=[budget])
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    curve = [eq]
    for t in trades:
        eq += t["pnl"]
        curve.append(eq)
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0,
                max_dd=dd, equity=curve)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


# ─────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────

def build_html(items, stats, days, budget, source):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    s = stats
    cls = "profit" if s["total_pnl"] >= 0 else "loss"

    # エクイティカーブ
    eq = s.get("equity", [budget])
    svg = ""
    if len(eq) > 1:
        w, h = 800, 200
        mn, mx = min(eq), max(eq)
        rng = mx - mn if mx > mn else 1
        pts = " ".join(f"{i/(len(eq)-1)*w:.0f},{h-(v-mn)/rng*(h-20)-10:.0f}"
                       for i, v in enumerate(eq))
        by = h - (budget - mn) / rng * (h - 20) - 10
        svg = f"""<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:800px;height:200px;background:#0f172a;border:1px solid #334155;border-radius:6px">
          <line x1="0" y1="{by:.0f}" x2="{w}" y2="{by:.0f}" stroke="#334155" stroke-dasharray="4"/>
          <polyline points="{pts}" fill="none" stroke="#4ade80" stroke-width="2"/>
          <text x="5" y="15" fill="#94a3b8" font-size="11">資金推移</text>
          <text x="5" y="{by-5:.0f}" fill="#94a3b8" font-size="10">{budget:,.0f}</text>
          <text x="{w-80}" y="15" fill="{'#4ade80' if eq[-1]>=budget else '#f87171'}" font-size="12">{eq[-1]:,.0f}円</text>
        </svg>"""

    # 銘柄テーブル
    sym_rows = ""
    for it in sorted(items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        ts = it["trades"]
        if not ts:
            continue
        ist = calc_stats(ts, budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        sym_rows += f"""<tr>
          <td class="sym">{it['name']}<br><small class="code">{it['symbol']}</small>
            <span class="tag">ORB</span></td>
          <td>{ist['n']}</td><td>{ist['win_rate']:.0f}%</td>
          <td>{_pf(ist['pf'])}</td>
          <td class="{c}">{ist['total_pnl']:+,.0f}円</td>
          <td class="profit">{ist['avg_win']:+,.0f}</td>
          <td class="loss">{ist['avg_loss']:+,.0f}</td>
          <td class="loss">{ist['max_dd']:+.1f}%</td></tr>"""

    # トレード明細
    details = ""
    for it in sorted(items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        if not it["trades"]:
            continue
        tp = sum(t["pnl"] for t in it["trades"])
        tc = "profit" if tp >= 0 else "loss"
        trows = ""
        for t in sorted(it["trades"], key=lambda x: str(x.get("entry_dt", ""))):
            pc = "profit" if t["pnl"] > 0 else "loss"
            ed = t["entry_dt"].strftime("%Y-%m-%d %H:%M") if hasattr(t["entry_dt"], "strftime") else str(t["entry_dt"])
            xd = t["exit_dt"].strftime("%H:%M") if hasattr(t["exit_dt"], "strftime") else str(t["exit_dt"])
            trows += f"""<tr><td>{ed}</td><td>{xd}</td><td>{t['qty']}</td>
              <td>{t['or_hi']:,.0f}</td><td>{t['or_lo']:,.0f}</td><td>{t['or_w']:,.0f}</td>
              <td>{t['entry_p']:,.1f}</td><td class="loss">{t['stop_p']:,.1f}</td>
              <td class="profit">{t['target_p']:,.1f}</td><td>{t['exit_p']:,.1f}</td>
              <td class="{pc}">{t['pnl']:+,.0f}</td><td class="{pc}">{t['pct']:+.2f}%</td>
              <td>{t['reason']}</td></tr>"""
        details += f"""<details class="ts">
          <summary><strong>{it['name']}</strong> <small class="code">{it['symbol']}</small>
            <span class="{tc}">{tp:+,.0f}円</span> <small>({len(it['trades'])}取引)</small></summary>
          <table><thead><tr><th>Entry</th><th>Exit</th><th>株数</th>
            <th>OR高値</th><th>OR安値</th><th>OR幅</th>
            <th>買値</th><th>損切</th><th>目標</th><th>決済値</th>
            <th>損益</th><th>%</th><th>理由</th></tr></thead>
          <tbody>{trows}</tbody></table></details>"""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>厳選10銘柄 ORB — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#60a5fa;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#60a5fa;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #60a5fa;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
tr:hover td{{background:#1e293b}}
.sym{{text-align:left;font-weight:600;min-width:140px}}
.code{{color:#64748b;font-weight:400;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.tag{{display:inline-block;padding:1px 7px;border-radius:99px;font-size:.72rem;font-weight:600;background:#1d4ed8;color:#bfdbfe}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center}}.box .lb{{color:#94a3b8;font-size:.75rem}}.box .vl{{font-size:1.3rem;font-weight:700}}
details.ts{{margin-bottom:6px;background:#1e293b;border-radius:6px}}
details.ts summary{{cursor:pointer;padding:8px 12px;font-size:.85rem;list-style:none}}
details.ts summary::-webkit-details-marker{{display:none}}
details.ts summary::before{{content:"▶ ";font-size:.7rem;color:#94a3b8}}
details.ts[open] summary::before{{content:"▼ "}}
details.ts table{{margin:0 12px 6px;width:calc(100% - 24px)}}
</style></head><body>
<h1>厳選10銘柄 ORB バックテスト</h1>
<p class="sub">生成: {today} ／ 期間: {days}日 ／ source: {source} ／ 予算: {budget:,}円<br>
OR: 9:00-9:30 ／ 目標: OR幅×{TARGET_K} ／ ギャップ≤{GAP_MAX_PCT}% ／ トレーリング ／ 前場限定</p>

<div class="box">
  <div class="it"><div class="lb">総損益</div><div class="vl {cls}">{s['total_pnl']:+,.0f}円</div></div>
  <div class="it"><div class="lb">取引数</div><div class="vl">{s['n']}</div></div>
  <div class="it"><div class="lb">勝率</div><div class="vl">{s['win_rate']:.1f}%</div></div>
  <div class="it"><div class="lb">PF</div><div class="vl">{_pf(s['pf'])}</div></div>
  <div class="it"><div class="lb">最大DD</div><div class="vl loss">{s['max_dd']:+.1f}%</div></div>
  <div class="it"><div class="lb">最終資金</div><div class="vl {'profit' if eq[-1]>=budget else 'loss'}">{eq[-1]:,.0f}円</div></div>
</div>

<h2>資金推移</h2>
{svg}

<h2>銘柄別サマリー</h2>
<table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
<tbody>{sym_rows}</tbody></table>

<h2>個別トレード一覧</h2>
{details}
</body></html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="厳選10銘柄 ORB バックテスト")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    targets = TOP10
    symbols = [s for s, _ in targets]
    print(f"厳選10銘柄 ORB: {args.days}日 / 予算{args.budget:,}円", flush=True)
    print(f"  銘柄: {', '.join(n for _, n in targets)}", flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    print(f"  取得: {len(fetched)}/{len(symbols)}銘柄", flush=True)

    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, name, fetched[sym])
        if r:
            items.append(r)

    all_trades = sorted(
        [t for it in items for t in it["trades"]],
        key=lambda x: str(x.get("entry_dt", "")))
    stats = calc_stats(all_trades, args.budget)

    # コンソール
    print()
    print("=" * 65)
    print(f"  厳選10銘柄 ORB  {args.days}日")
    print("=" * 65)
    print(f"  取引: {stats['n']}  勝率: {stats['win_rate']:.1f}%  "
          f"PF: {_pf(stats['pf'])}  損益: {stats['total_pnl']:+,.0f}円  "
          f"DD: {stats['max_dd']:+.1f}%")
    print()
    for it in sorted(items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        ist = calc_stats(it["trades"], args.budget)
        print(f"  {it['name']:<16} {it['symbol']:<10} {ist['n']:>3}回 "
              f"{ist['win_rate']:>4.0f}% PF{_pf(ist['pf']):>5} "
              f"{ist['total_pnl']:>+10,.0f}")
    print("=" * 65)

    # HTML
    out = Path(f"daytrade_top10_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(items, stats, args.days, args.budget, args.source),
                   encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
