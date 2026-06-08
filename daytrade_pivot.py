"""
daytrade_pivot.py  ―  デイトレ戦略④: Pivot Point Bounce (ピボット反発)
==================================================================
【戦略概要】
  前日のOHLCからピボットポイント (PP/S1/R1) を計算し、
  当日の5分足で S1 (サポート) 付近での反発を捉える逆張り戦略。
  ORB (順張り) と逆相関が期待できる。

【ピボット計算】
  PP = (前日High + 前日Low + 前日Close) / 3
  S1 = 2 × PP - 前日High    (第1サポート)
  R1 = 2 × PP - 前日Low     (第1レジスタンス)

【エントリー条件 (すべて満たす)】
  1. 当日安値が S1 × (1 - TOL) 以下にタッチ (サポート到達)
  2. 現在バー終値 > S1                      (反発確認)
  3. 現在バー終値 > 前バー終値               (上昇確認)
  4. 時刻 < ENTRY_CUTOFF
  5. 当日ポジションなし

【決済】
  - 目標: PP (ピボットポイントまで戻る)
  - 損切り: S1 - (PP - S1) × STOP_K
  - トレーリング: 含み益50%で建値撤退
  - 強制: 14:55
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

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

# パラメータ
DEFAULT_DAYS   = 60
BUDGET         = 600_000
MAX_RISK       = 6_000
PIVOT_TOL      = 0.002    # S1 タッチ判定の許容率 (0.2%)
STOP_K         = 0.5      # 損切り = S1 - (PP-S1) × K
TRAILING_TRIGGER = 0.5
FORCE_CLOSE    = dtime(14, 55)
ENTRY_CUTOFF   = dtime(14, 0)
WARMUP_BARS    = 6

DEFAULT_SYMBOLS = DAYTRADE_SYMBOLS


def calc_pivot(prev_day_df: pd.DataFrame) -> dict | None:
    """前日の OHLC からピボットポイントを計算。"""
    if prev_day_df is None or prev_day_df.empty:
        return None
    h = float(prev_day_df["high"].max())
    l = float(prev_day_df["low"].min())
    c = float(prev_day_df.iloc[-1]["close"])
    if h <= 0 or l <= 0 or c <= 0:
        return None
    pp = (h + l + c) / 3
    s1 = 2 * pp - h
    r1 = 2 * pp - l
    return {"pp": pp, "s1": s1, "r1": r1}


def backtest_pivot_day(day_df: pd.DataFrame, pivot: dict,
                       budget: int, max_risk: int) -> dict | None:
    """1日分のピボット反発バックテスト。"""
    pp = pivot["pp"]
    s1 = pivot["s1"]

    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times  = day_df.index
    n = len(day_df)

    if n < WARMUP_BARS + 2 or s1 <= 0 or pp <= s1:
        return None

    state = "idle"
    entry_p = stop_p = target_p = 0.0
    entry_dt = exit_dt = None
    exit_p = None
    reason = None
    qty = 0
    trailing = False

    i = WARMUP_BARS
    while i < n:
        t  = times[i].time()
        hi = highs[i]
        lo = lows[i]
        cl = closes[i]

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                exit_p, exit_dt, reason = cl, times[i], "引け強制"
                break
            if not trailing and (cl - entry_p) / (target_p - entry_p) >= TRAILING_TRIGGER:
                stop_p = entry_p
                trailing = True
            if lo <= stop_p and hi >= target_p:
                exit_p, exit_dt = stop_p, times[i]
                reason = "建値撤退" if trailing else "損切り"
                break
            if hi >= target_p:
                exit_p, exit_dt, reason = target_p, times[i], "目標達成"
                break
            if lo <= stop_p:
                exit_p, exit_dt = stop_p, times[i]
                reason = "建値撤退" if trailing else "損切り"
                break
            i += 1
            continue

        if t >= ENTRY_CUTOFF:
            break

        # S1 タッチ + 反発
        if lo <= s1 * (1 + PIVOT_TOL) and cl > s1 and cl > closes[i - 1]:
            if i + 1 >= n:
                break
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            target_p = pp
            stop_p = s1 - (pp - s1) * STOP_K
            if entry_p <= stop_p or target_p <= entry_p:
                i += 1
                continue
            qty = calc_position_size(entry_p, stop_p, budget, max_risk)
            state = "in_pos"
            trailing = False
            i += 2
            continue
        i += 1

    if state == "in_pos" and exit_p is None:
        exit_p, exit_dt, reason = closes[-1], times[-1], "引け強制"

    if exit_p is None or entry_dt is None:
        return None

    pnl = (exit_p - entry_p) * qty
    pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt, entry_p=entry_p, exit_p=exit_p,
        stop_p=stop_p, target_p=target_p, qty=qty,
        pnl=pnl, pct=pct, strategy="Pivot", reason=reason,
        pp=pp, s1=s1,
    )


def backtest_symbol(symbol, name, df, budget, max_risk):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None
    trades = []
    for i in range(1, len(dates)):
        prev_df = daily[dates[i - 1]]
        curr_df = daily[dates[i]]
        pivot = calc_pivot(prev_df)
        if pivot is None:
            continue
        t = backtest_pivot_day(curr_df, pivot, budget, max_risk)
        if t:
            trades.append(t)
    return dict(symbol=symbol, name=name, trades=trades)


def _calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    eq, peak, max_dd = budget, budget, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = (eq - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0, max_dd=max_dd)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def build_html(items, stats, days, budget, source):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    s = stats
    cls = "profit" if s["total_pnl"] >= 0 else "loss"
    rows = ""
    for it in sorted(items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        ts = it["trades"]
        if not ts:
            continue
        ist = _calc_stats(ts, budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        rows += f'<tr><td class="sym">{it["symbol"]}<br><small>{it["name"]}</small></td>'
        rows += f'<td>{ist["n"]}</td><td>{ist["win_rate"]:.0f}%</td><td>{_pf(ist["pf"])}</td>'
        rows += f'<td class="{c}">{ist["total_pnl"]:+,.0f}</td>'
        rows += f'<td class="profit">{ist["avg_win"]:+,.0f}</td>'
        rows += f'<td class="loss">{ist["avg_loss"]:+,.0f}</td>'
        rows += f'<td class="loss">{ist["max_dd"]:+.1f}%</td></tr>'
    tdetails = ""
    for it in sorted(items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        if not it["trades"]:
            continue
        tp = sum(t["pnl"] for t in it["trades"])
        tc = "profit" if tp >= 0 else "loss"
        tr = ""
        for t in sorted(it["trades"], key=lambda x: str(x.get("entry_dt",""))):
            pc = "profit" if t["pnl"] > 0 else "loss"
            ed = t["entry_dt"].strftime("%Y-%m-%d %H:%M") if hasattr(t["entry_dt"],"strftime") else str(t["entry_dt"])
            xd = t["exit_dt"].strftime("%H:%M") if hasattr(t["exit_dt"],"strftime") else str(t["exit_dt"])
            tr += f'<tr><td>{ed}</td><td>{xd}</td><td>{t["qty"]}</td>'
            tr += f'<td>{t["entry_p"]:,.1f}</td><td class="loss">{t["stop_p"]:,.1f}</td>'
            tr += f'<td class="profit">{t["target_p"]:,.1f}</td><td>{t["exit_p"]:,.1f}</td>'
            tr += f'<td class="{pc}">{t["pnl"]:+,.0f}</td><td class="{pc}">{t["pct"]:+.2f}%</td>'
            tr += f'<td>{t["reason"]}</td></tr>'
        tdetails += f'<details class="ts"><summary><strong>{it["symbol"]} {it["name"]}</strong> '
        tdetails += f'<span class="{tc}">{tp:+,.0f}円</span> <small>{len(it["trades"])}取引</small></summary>'
        tdetails += f'<table><thead><tr><th>Entry</th><th>Exit</th><th>株数</th><th>買値</th>'
        tdetails += f'<th>損切</th><th>目標</th><th>決済値</th><th>損益</th><th>%</th><th>理由</th>'
        tdetails += f'</tr></thead><tbody>{tr}</tbody></table></details>'
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>Pivot Point Bounce — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#22d3ee;margin-bottom:4px;font-size:1.5rem}}.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#22d3ee;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #22d3ee;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:5px 7px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:4px 7px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
tr:hover td{{background:#1e293b}}.sym{{text-align:left;font-weight:600;min-width:100px}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center}}.box .lb{{color:#94a3b8;font-size:.75rem}}.box .vl{{font-size:1.3rem;font-weight:700}}
details.ts{{margin-bottom:6px;background:#1e293b;border-radius:6px}}
details.ts summary{{cursor:pointer;padding:8px 12px;font-size:.85rem;list-style:none}}
details.ts summary::-webkit-details-marker{{display:none}}
details.ts summary::before{{content:"▶ ";font-size:.7rem;color:#94a3b8}}
details.ts[open] summary::before{{content:"▼ "}}
details.ts table{{margin:0 12px 6px;width:calc(100% - 24px)}}</style></head><body>
<h1>デイトレ戦略④: Pivot Point Bounce</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円<br>
エントリー: S1タッチ後の反発 → 目標PP / 損切S1下方 / トレーリング / 前場限定</p>
<div class="box">
<div class="it"><div class="lb">総損益</div><div class="vl {cls}">{s["total_pnl"]:+,.0f}円</div></div>
<div class="it"><div class="lb">取引</div><div class="vl">{s["n"]}</div></div>
<div class="it"><div class="lb">勝率</div><div class="vl">{s["win_rate"]:.1f}%</div></div>
<div class="it"><div class="lb">PF</div><div class="vl">{_pf(s["pf"])}</div></div>
<div class="it"><div class="lb">DD</div><div class="vl loss">{s["max_dd"]:+.1f}%</div></div></div>
<h2>銘柄別</h2><table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead><tbody>{rows}</tbody></table>
<h2>個別トレード</h2>{tdetails}</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Pivot Point Bounce デイトレ")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto","local","yfinance"], default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.source == "yfinance" and args.days > 60:
        args.days = 60
    targets = [(s, s) for s in args.symbols] if args.symbols else DEFAULT_SYMBOLS
    symbols_list = [s for s, _ in targets]
    print(f"Pivot反発: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円", flush=True)
    fetched = load_intraday_batch(symbols_list, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items() if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)
    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, name, fetched[sym], args.budget, MAX_RISK)
        if r:
            items.append(r)
    all_trades = sorted([t for it in items for t in it["trades"]],
                        key=lambda x: str(x.get("entry_dt","")))
    stats = _calc_stats(all_trades, args.budget)
    print(f"\n取引:{stats['n']}  勝率:{stats['win_rate']:.1f}%  PF:{_pf(stats['pf'])}  "
          f"損益:{stats['total_pnl']:+,.0f}  DD:{stats['max_dd']:+.1f}%")
    out = Path(f"daytrade_pivot_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(items, stats, args.days, args.budget, args.source), encoding="utf-8")
    print(f"HTML: {out.resolve()}")
    if not args.no_browser:
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
