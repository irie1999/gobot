"""
daytrade_donchian.py  ―  デイトレ戦略: Donchian 20本高値ブレイク
==================================================================
【戦略】
  過去20本 (100分) の最高値を更新したバーで順張り買い。
  トレンド継続を狙う古典的ブレイクアウト。

【エントリー条件】
  1. 現バー終値 > 直近20本 (現バー除く) の最高値
  2. 陽線 (close > open)
  3. 時刻 11:00 まで
  4. 当日ポジションなし
  5. ギャップ ≤ 2%

【決済】
  損切り: 直近20本の最安値
  目標: エントリー + (エントリー - 損切り) × 1.5 (R:R = 1.5:1)
  トレーリング: 含み益50%で建値撤退
  強制: 14:55

【使い方】
  python daytrade_donchian.py --source yfinance --days 60 --budget 600000
  python daytrade_donchian.py --source local --days 730 --budget 600000
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

# パラメータ
DEFAULT_DAYS     = 60
BUDGET           = 600_000
MAX_RISK         = 6_000
DON_PERIOD       = 20         # 過去何本の高値
TARGET_R         = 1.5
GAP_MAX_PCT      = 2.0
TRAILING_TRIGGER = 0.5
FORCE_CLOSE      = dtime(14, 55)
ENTRY_CUTOFF     = dtime(11, 0)
WARMUP           = 20


def backtest_donchian_day(day_df: pd.DataFrame, prev_close=None):
    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times  = day_df.index
    n = len(day_df)

    if n < WARMUP + 2:
        return None

    open_p = opens[0]
    if prev_close and prev_close > 0:
        if abs(open_p - prev_close) / prev_close * 100 > GAP_MAX_PCT:
            return None

    state = "idle"
    entry_p = stop_p = target_p = 0.0
    entry_dt = exit_dt = None
    exit_p = None
    reason = None
    qty = 0
    trailing = False

    i = WARMUP
    while i < n:
        t = times[i].time()
        hi, lo, cl, op = highs[i], lows[i], closes[i], opens[i]

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                exit_p, exit_dt, reason = cl, times[i], "引け強制"
                break
            if not trailing and target_p > entry_p:
                if (cl - entry_p) / (target_p - entry_p) >= TRAILING_TRIGGER:
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

        # Donchian条件: 過去20本の最高値を更新 + 陽線
        don_hi = highs[i - DON_PERIOD:i].max()
        don_lo = lows[i - DON_PERIOD:i].min()
        if cl > don_hi and cl > op:
            if i + 1 >= n:
                break
            entry_p = opens[i + 1]
            entry_dt = times[i + 1]
            stop_p = don_lo
            if entry_p <= stop_p:
                i += 1
                continue
            target_p = entry_p + (entry_p - stop_p) * TARGET_R
            qty = calc_position_size(entry_p, stop_p, BUDGET, MAX_RISK)
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
    pct = (exit_p - entry_p) / entry_p * 100
    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=entry_p, exit_p=exit_p,
        stop_p=stop_p, target_p=target_p,
        qty=qty, pnl=pnl, pct=pct,
        strategy="Donchian", reason=reason,
    )


def backtest_symbol(sym, name, df, budget=BUDGET, max_risk=MAX_RISK):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None
    trades = []
    prev_close = None
    for d in dates:
        t = backtest_donchian_day(daily[d], prev_close)
        if t:
            trades.append(t)
        prev_close = float(daily[d].iloc[-1]["close"])
    return dict(symbol=sym, name=name, trades=trades)


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0, max_dd=dd)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def build_html(items, stats, days, budget, source):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    s = stats
    cls = "profit" if s["total_pnl"] >= 0 else "loss"

    rows = ""
    for it in sorted(items, key=lambda x: sum(t["pnl"] for t in x["trades"]), reverse=True):
        if not it["trades"]:
            continue
        ist = calc_stats(it["trades"], budget)
        c = "profit" if ist["total_pnl"] >= 0 else "loss"
        rows += f'<tr><td class="sym">{it["name"]}<br><small class="code">{it["symbol"]}</small></td>'
        rows += f'<td>{ist["n"]}</td><td>{ist["win_rate"]:.0f}%</td><td>{_pf(ist["pf"])}</td>'
        rows += f'<td class="{c}">{ist["total_pnl"]:+,.0f}</td>'
        rows += f'<td class="profit">{ist["avg_win"]:+,.0f}</td>'
        rows += f'<td class="loss">{ist["avg_loss"]:+,.0f}</td>'
        rows += f'<td class="loss">{ist["max_dd"]:+.1f}%</td></tr>'

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>Donchian Breakout — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #10b981;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600;min-width:140px}}
.code{{color:#64748b;font-weight:400;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
.box{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:14px;display:flex;gap:24px;flex-wrap:wrap}}
.box .it{{text-align:center}}.box .lb{{color:#94a3b8;font-size:.75rem}}.box .vl{{font-size:1.3rem;font-weight:700}}
</style></head><body>
<h1>デイトレ戦略: Donchian {DON_PERIOD}本高値ブレイク</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円<br>
エントリー: 5分足{DON_PERIOD}本高値更新+陽線 → 翌バー寄付買い<br>
損切り: {DON_PERIOD}本安値 / 目標: R:R {TARGET_R}:1 / トレーリング / 前場限定</p>
<div class="box">
<div class="it"><div class="lb">総損益</div><div class="vl {cls}">{s["total_pnl"]:+,.0f}円</div></div>
<div class="it"><div class="lb">取引</div><div class="vl">{s["n"]}</div></div>
<div class="it"><div class="lb">勝率</div><div class="vl">{s["win_rate"]:.1f}%</div></div>
<div class="it"><div class="lb">PF</div><div class="vl">{_pf(s["pf"])}</div></div>
<div class="it"><div class="lb">DD</div><div class="vl loss">{s["max_dd"]:+.1f}%</div></div></div>
<h2>銘柄別</h2><table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Donchian デイトレ")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    targets = [(s, s) for s in args.symbols] if args.symbols else DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"Donchian: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円", flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
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
                        key=lambda x: str(x.get("entry_dt", "")))
    stats = calc_stats(all_trades, args.budget)
    print(f"\n取引:{stats['n']}  勝率:{stats['win_rate']:.1f}%  PF:{_pf(stats['pf'])}  "
          f"損益:{stats['total_pnl']:+,.0f}  DD:{stats['max_dd']:+.1f}%")

    out = Path(f"daytrade_donchian_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(items, stats, args.days, args.budget, args.source),
                   encoding="utf-8")
    print(f"HTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
