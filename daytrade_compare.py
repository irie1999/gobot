"""
daytrade_compare.py  ―  4戦略比較バックテスト
==================================================================
Donchian / GapReversal / VWAP / OpenMomentum を同一データ・同一期間で
一括バックテストし、結果を比較表示。

【使い方】
  python daytrade_compare.py
  python daytrade_compare.py --days 60 --source local
  python daytrade_compare.py --universe n225        # 日経225全銘柄
  python daytrade_compare.py --universe watch       # daytrade_symbols.py の20銘柄
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch


def _load_universe(name):
    if name == "watch":
        return DAYTRADE_SYMBOLS
    if name == "n225":
        from symbols_all import SYMBOLS
        return SYMBOLS
    raise ValueError(f"unknown universe: {name}")

from daytrade_donchian import backtest_symbol as don_bt, calc_stats as don_stats
from daytrade_gap_reversal import backtest_symbol as gr_bt, calc_stats as gr_stats
from daytrade_vwap_rebound import backtest_symbol as vwap_bt, calc_stats as vwap_stats
from daytrade_opening_momentum import backtest_symbol as om_bt, calc_stats as om_stats

JST = timezone(timedelta(hours=9))
BUDGET = 600_000
MAX_RISK = 6_000


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def run_strategy(name, bt_fn, stats_fn, fetched, targets, budget):
    items = []
    for sym, sname in targets:
        if sym not in fetched:
            continue
        r = bt_fn(sym, sname, fetched[sym], budget, MAX_RISK)
        if r:
            items.append(r)

    all_trades = sorted(
        [t for it in items for t in it["trades"]],
        key=lambda x: str(x.get("entry_dt", ""))
    )
    stats = stats_fn(all_trades, budget)

    trade_dates = set()
    for t in all_trades:
        dt = t.get("entry_dt")
        if hasattr(dt, "date"):
            trade_dates.add(dt.date())

    return dict(
        name=name,
        stats=stats,
        items=items,
        all_trades=all_trades,
        trade_days=len(trade_dates),
    )


def print_comparison(results, total_days):
    print(f"\n{'='*90}")
    print(f"  4戦略比較 ({total_days}営業日)")
    print("=" * 90)
    print(f"{'戦略':<20} {'取引':>5} {'勝率':>6} {'PF':>7} "
          f"{'損益':>11} {'DD':>7} {'取引日':>5} {'日率':>5}")
    print("-" * 90)

    for r in results:
        s = r["stats"]
        day_rate = f"{r['trade_days']/total_days*100:.0f}%" if total_days > 0 else "-"
        print(f"{r['name']:<20} {s['n']:>5} {s['win_rate']:>5.1f}% "
              f"{_pf(s['pf']):>7} {s['total_pnl']:>+11,.0f} "
              f"{s['max_dd']:>+6.1f}% {r['trade_days']:>5} {day_rate:>5}")

    print("=" * 90)

    best = max(results, key=lambda x: x["stats"]["total_pnl"])
    most_freq = max(results, key=lambda x: x["trade_days"])
    best_pf = max(results, key=lambda x: x["stats"]["pf"]
                  if x["stats"]["pf"] < 100 else 0)

    print(f"\n  最高利益: {best['name']} ({best['stats']['total_pnl']:+,.0f}円)")
    print(f"  最高頻度: {most_freq['name']} ({most_freq['trade_days']}/{total_days}日)")
    print(f"  最高PF:   {best_pf['name']} (PF {_pf(best_pf['stats']['pf'])})")

    from daytrade_donchian import calc_stats
    for r in results:
        if not r["items"]:
            continue
        print(f"\n--- {r['name']} 銘柄別 TOP5 ---")
        rows = []
        for it in r["items"]:
            if not it["trades"]:
                continue
            ist = calc_stats(it["trades"], BUDGET)
            rows.append((it["name"], it["symbol"], ist))
        rows.sort(key=lambda x: x[2]["total_pnl"], reverse=True)
        for name, sym, ist in rows[:5]:
            disp = name[:18] if len(name) <= 18 else name[:17] + "…"
            print(f"  {disp:<20} {sym:<8} {ist['n']:>3}回 "
                  f"{ist['win_rate']:>4.0f}% PF{_pf(ist['pf']):>5} "
                  f"{ist['total_pnl']:>+10,.0f}")


def build_html(results, total_days, days, budget, source):
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    from daytrade_donchian import calc_stats

    comp_rows = ""
    for r in results:
        s = r["stats"]
        cls = "profit" if s["total_pnl"] >= 0 else "loss"
        day_rate = f"{r['trade_days']/total_days*100:.0f}%" if total_days > 0 else "-"
        comp_rows += f"""<tr>
          <td class="sym">{r['name']}</td>
          <td>{s['n']}</td><td>{s['win_rate']:.1f}%</td>
          <td>{_pf(s['pf'])}</td>
          <td class="{cls}">{s['total_pnl']:+,.0f}</td>
          <td class="loss">{s['max_dd']:+.1f}%</td>
          <td>{r['trade_days']}/{total_days}</td>
          <td>{day_rate}</td></tr>"""

    strat_sections = ""
    for r in results:
        if not r["items"]:
            continue
        rows = []
        for it in r["items"]:
            if not it["trades"]:
                continue
            ist = calc_stats(it["trades"], budget)
            rows.append((it["name"], it["symbol"], ist))
        rows.sort(key=lambda x: x[2]["total_pnl"], reverse=True)
        sym_rows = ""
        for name, sym, ist in rows:
            c = "profit" if ist["total_pnl"] >= 0 else "loss"
            sym_rows += f"""<tr>
              <td class="sym">{name}<br><small class="code">{sym}</small></td>
              <td>{ist['n']}</td><td>{ist['win_rate']:.0f}%</td>
              <td>{_pf(ist['pf'])}</td>
              <td class="{c}">{ist['total_pnl']:+,.0f}</td>
              <td class="profit">{ist['avg_win']:+,.0f}</td>
              <td class="loss">{ist['avg_loss']:+,.0f}</td>
              <td class="loss">{ist['max_dd']:+.1f}%</td></tr>"""
        strat_sections += f"""
        <h2>{r['name']}</h2>
        <table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
        <th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
        <tbody>{sym_rows}</tbody></table>"""

    best = max(results, key=lambda x: x["stats"]["total_pnl"])
    most_freq = max(results, key=lambda x: x["trade_days"])
    best_pf = max(results, key=lambda x: x["stats"]["pf"]
                  if x["stats"]["pf"] < 100 else 0)

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>4戦略比較 — {today}</title>
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
.winner{{background:#1e3a2f;border:1px solid #10b981;padding:12px;border-radius:8px;margin:16px 0;font-size:.9rem}}
</style></head><body>
<h1>4戦略比較レポート</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円 / {total_days}営業日</p>
<h2>戦略比較サマリ</h2>
<table><thead><tr><th>戦略</th><th>取引</th><th>勝率</th><th>PF</th>
<th>損益</th><th>DD</th><th>取引日</th><th>日率</th></tr></thead>
<tbody>{comp_rows}</tbody></table>
<div class="winner">
  <b>最高利益:</b> {best['name']} ({best['stats']['total_pnl']:+,.0f}円)<br>
  <b>最高頻度:</b> {most_freq['name']} ({most_freq['trade_days']}/{total_days}日)<br>
  <b>最高PF:</b> {best_pf['name']} (PF {_pf(best_pf['stats']['pf'])})
</div>
{strat_sections}
</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="4戦略比較バックテスト")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--universe", choices=["watch", "n225"], default="watch",
                        help="銘柄ユニバース: watch=DAYTRADE_SYMBOLS(20), n225=日経225全銘柄")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    targets = _load_universe(args.universe)
    symbols = [s for s, _ in targets]
    print(f"4戦略比較 [universe={args.universe}]: "
          f"{len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円",
          flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄\n", flush=True)

    all_dates = set()
    for df in fetched.values():
        all_dates.update(df.index.date)
    total_days = len(all_dates)

    strategies = [
        ("Donchian", don_bt, don_stats),
        ("GapReversal", gr_bt, gr_stats),
        ("VWAPリバウンド", vwap_bt, vwap_stats),
        ("寄付モメンタム", om_bt, om_stats),
    ]

    results = []
    for name, bt_fn, stats_fn in strategies:
        print(f"[{name}] 実行中...", flush=True)
        r = run_strategy(name, bt_fn, stats_fn, fetched, targets, args.budget)
        s = r["stats"]
        print(f"  → 取引:{s['n']}  勝率:{s['win_rate']:.1f}%  "
              f"PF:{_pf(s['pf'])}  損益:{s['total_pnl']:+,.0f}")
        results.append(r)

    print_comparison(results, total_days)

    # HTML出力
    stamp = datetime.now(JST).strftime('%Y%m%d')
    out = Path(f"daytrade_compare_{args.universe}_{stamp}.html")
    out.write_text(build_html(results, total_days, args.days, args.budget, args.source),
                   encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
