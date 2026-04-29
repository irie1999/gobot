"""
daytrade_orb_sweep.py  ―  ORB パラメータスイープ (グリッドサーチ)
==================================================================
OR時間 / 目標倍率 / OR幅フィルター / 出来高フィルター / ギャップフィルター
の全組み合わせを自動テストし、最適パラメータを発見する。

【使い方】
  python daytrade_orb_sweep.py --source yfinance --days 60 --budget 600000
  python daytrade_orb_sweep.py --source local --days 730 --budget 600000
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime, time as dtime, timedelta, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))
BUDGET = 600_000
MAX_RISK = 6_000
FORCE_CLOSE = dtime(14, 55)
ENTRY_CUTOFF = dtime(11, 0)
TRAILING_TRIGGER = 0.5
AM_START = dtime(9, 0)

# ── スイープ範囲 ────────────────────────────────────────────
SWEEP_OR_MINUTES   = [15, 30, 45, 60]
SWEEP_TARGET_K     = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]
SWEEP_OR_WIDTH_MIN = [0, 0.1, 0.2]       # OR幅 / 始値 の最小 % (0=フィルタなし)
SWEEP_OR_WIDTH_MAX = [999, 1.0, 0.8]     # OR幅 / 始値 の最大 % (999=フィルタなし)
SWEEP_GAP_MAX      = [999, 2.0, 1.0]     # 前日比ギャップ % 上限 (999=フィルタなし)


def backtest_orb_day(day_df, or_minutes, target_k, prev_close,
                     or_width_min_pct, or_width_max_pct, gap_max_pct,
                     budget, max_risk):
    """1日分のORBバックテスト (フィルター付き)。"""
    or_end = (datetime.combine(datetime.today(), AM_START)
              + timedelta(minutes=or_minutes)).time()

    or_bars = day_df[day_df.index.time < or_end]
    rest = day_df[day_df.index.time >= or_end]
    if len(or_bars) < 2 or rest.empty:
        return None

    or_hi = float(or_bars["high"].max())
    or_lo = float(or_bars["low"].min())
    or_w = or_hi - or_lo
    if or_w <= 0 or or_hi <= 0:
        return None

    open_price = float(or_bars.iloc[0]["open"])
    if open_price <= 0:
        return None

    # フィルターE: OR幅 (始値比)
    or_w_pct = or_w / open_price * 100
    if or_w_pct < or_width_min_pct or or_w_pct > or_width_max_pct:
        return None

    # フィルターD: ギャップ
    if prev_close and prev_close > 0:
        gap_pct = abs(open_price - prev_close) / prev_close * 100
        if gap_pct > gap_max_pct:
            return None

    # ORBロジック
    bars_list = list(rest.itertuples())
    state = "idle"
    entry_p = stop_p = target_p = 0.0
    entry_dt = exit_dt = None
    exit_p = None
    reason = None
    qty = 0
    trailing = False

    for i, bar in enumerate(bars_list):
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
        if cl > or_hi and i + 1 < len(bars_list):
            nxt = bars_list[i + 1]
            entry_p = float(nxt.open)
            entry_dt = nxt.Index
            stop_p = or_lo
            target_p = entry_p + or_w * target_k
            if entry_p <= stop_p:
                break
            qty = calc_position_size(entry_p, stop_p, budget, max_risk)
            state = "in_pos"
            trailing = False

            nhi, nlo = float(nxt.high), float(nxt.low)
            if nlo <= stop_p and nhi >= target_p:
                exit_p, exit_dt, reason = stop_p, nxt.Index, "損切り"
                break
            if nhi >= target_p:
                exit_p, exit_dt, reason = target_p, nxt.Index, "目標達成"
                break
            if nlo <= stop_p:
                exit_p, exit_dt, reason = stop_p, nxt.Index, "損切り"
                break
            continue

    if state == "in_pos" and exit_p is None:
        exit_p, exit_dt, reason = float(bars_list[-1].close), bars_list[-1].Index, "引け強制"

    if exit_p is None or entry_dt is None:
        return None

    pnl = (exit_p - entry_p) * qty
    return {"pnl": pnl, "reason": reason}


def run_sweep(fetched, targets, budget, max_risk):
    """全パラメータ組み合わせを実行。"""
    # 日付分割を事前計算
    sym_daily = {}
    for sym, name in targets:
        if sym not in fetched:
            continue
        daily = split_by_day(fetched[sym])
        if len(daily) >= 2:
            sym_daily[sym] = (name, daily)

    combos = list(product(
        SWEEP_OR_MINUTES, SWEEP_TARGET_K,
        SWEEP_OR_WIDTH_MIN, SWEEP_OR_WIDTH_MAX, SWEEP_GAP_MAX))

    print(f"スイープ: {len(combos)} 組み合わせ × {len(sym_daily)} 銘柄",
          flush=True)

    results = []
    for ci, (or_min, k, ow_min, ow_max, gap_max) in enumerate(combos):
        if ow_min >= ow_max:
            continue

        all_pnl = []
        wins = 0

        for sym, (name, daily) in sym_daily.items():
            dates = sorted(daily.keys())
            prev_close = None
            for di in range(len(dates)):
                day_df = daily[dates[di]]
                t = backtest_orb_day(
                    day_df, or_min, k, prev_close,
                    ow_min, ow_max, gap_max, budget, max_risk)
                if t:
                    all_pnl.append(t["pnl"])
                    if t["pnl"] > 0:
                        wins += 1
                prev_close = float(day_df.iloc[-1]["close"])

        n = len(all_pnl)
        if n < 10:
            continue

        gp = sum(p for p in all_pnl if p > 0)
        gl = abs(sum(p for p in all_pnl if p < 0))
        pf = gp / gl if gl > 0 else float("inf")
        total = sum(all_pnl)
        wr = wins / n * 100

        eq = budget
        peak = eq
        max_dd = 0.0
        for p in all_pnl:
            eq += p
            peak = max(peak, eq)
            dd = (eq - peak) / peak * 100
            if dd < max_dd:
                max_dd = dd

        results.append({
            "or_min": or_min, "target_k": k,
            "ow_min": ow_min, "ow_max": ow_max,
            "gap_max": gap_max,
            "n": n, "wins": wins, "wr": wr,
            "pf": pf, "total_pnl": total, "max_dd": max_dd,
        })

        if (ci + 1) % 50 == 0:
            print(f"  {ci+1}/{len(combos)} 完了", flush=True)

    return sorted(results, key=lambda x: x["pf"], reverse=True)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def build_html(results, days, source, budget):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    rows = ""
    for i, r in enumerate(results[:50]):
        cls = "profit" if r["total_pnl"] >= 0 else "loss"
        rank_cls = "profit" if r["pf"] >= 1.5 else ("" if r["pf"] >= 1.0 else "loss")
        filters = []
        if r["ow_min"] > 0:
            filters.append(f"OR幅≥{r['ow_min']}%")
        if r["ow_max"] < 999:
            filters.append(f"OR幅≤{r['ow_max']}%")
        if r["gap_max"] < 999:
            filters.append(f"Gap≤{r['gap_max']}%")
        filter_str = " / ".join(filters) if filters else "なし"

        rows += f"""<tr>
          <td>{i+1}</td>
          <td>{r['or_min']}分</td><td>{r['target_k']}</td>
          <td>{filter_str}</td>
          <td>{r['n']}</td><td>{r['wr']:.1f}%</td>
          <td class="{rank_cls}">{_pf(r['pf'])}</td>
          <td class="{cls}">{r['total_pnl']:+,.0f}</td>
          <td class="loss">{r['max_dd']:+.1f}%</td>
        </tr>"""

    # 現行パラメータの位置を検索
    current = next((i for i, r in enumerate(results)
                    if r["or_min"] == 30 and r["target_k"] == 1.5
                    and r["ow_min"] == 0 and r["gap_max"] == 999), -1)
    current_info = f"現行 (OR30分/K1.5/フィルタなし) は {current+1}位" if current >= 0 else ""

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>ORB パラメータスイープ — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#fbbf24;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#fbbf24;margin:24px 0 10px;font-size:1.1rem;border-left:3px solid #fbbf24;padding-left:10px}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
tr:hover td{{background:#1e293b}}.profit{{color:#4ade80}}.loss{{color:#f87171}}
tr:nth-child(1) td{{background:#1e3a1e}}
.note{{color:#fbbf24;font-size:.9rem;margin-bottom:12px}}</style></head><body>
<h1>ORB パラメータスイープ結果</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円 / {len(results)}組み合わせ</p>
<p class="note">PF順 Top 50。1位 (緑背景) が最適パラメータ。{current_info}</p>
<h2>ランキング</h2>
<table><thead><tr>
<th>#</th><th>OR時間</th><th>目標K</th><th>フィルター</th>
<th>取引</th><th>勝率</th><th>PF</th><th>損益</th><th>DD</th>
</tr></thead><tbody>{rows}</tbody></table>
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="ORB パラメータスイープ")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.source == "yfinance" and args.days > 60:
        args.days = 60

    targets = DAYTRADE_SYMBOLS
    symbols_list = [s for s, _ in targets]
    print(f"ORBスイープ: {len(targets)}銘柄 / {args.days}日", flush=True)

    fetched = load_intraday_batch(symbols_list, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    results = run_sweep(fetched, targets, args.budget, MAX_RISK)

    # コンソール Top 10
    print()
    print(f"{'#':>3} {'OR':>4} {'K':>5} {'N':>5} {'WR':>6} {'PF':>6} "
          f"{'損益':>12} {'DD':>7}  フィルター")
    print("-" * 75)
    for i, r in enumerate(results[:10]):
        f = []
        if r["ow_min"] > 0:
            f.append(f"幅≥{r['ow_min']}%")
        if r["ow_max"] < 999:
            f.append(f"幅≤{r['ow_max']}%")
        if r["gap_max"] < 999:
            f.append(f"Gap≤{r['gap_max']}%")
        print(f"{i+1:>3} {r['or_min']:>3}分 {r['target_k']:>5.2f} "
              f"{r['n']:>5} {r['wr']:>5.1f}% {_pf(r['pf']):>6} "
              f"{r['total_pnl']:>+12,.0f} {r['max_dd']:>+6.1f}%  "
              f"{' / '.join(f) if f else 'なし'}")

    out = Path(f"daytrade_orb_sweep_{datetime.now(JST).strftime('%Y%m%d')}.html")
    out.write_text(build_html(results, args.days, args.source, args.budget),
                   encoding="utf-8")
    print(f"\nHTMLレポート: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
