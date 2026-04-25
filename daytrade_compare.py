"""
daytrade_compare.py  ―  4戦略比較バックテスト (前日選定対応)
==================================================================
Donchian / GapReversal / VWAP / OpenMomentum を同一データ・同一期間で
一括バックテストし、結果を比較表示。

【使い方】
  # 既存: 全銘柄に対してデイトレ実行
  python daytrade_compare.py --universe n225

  # 前日終値ベースで翌日候補を選定 → 当日デイトレ (walk-forward)
  python daytrade_compare.py --universe n225 --selector simple --top-n 20
  python daytrade_compare.py --universe n225 --selector all --top-n 20

  --selector:
    none     : 全銘柄に対して実行 (デフォルト)
    simple   : 前日陽線+1〜3% / 出来高急増 / 高値引け
    multi    : 20MA上 + RSI50〜75 + MACD>0 + 出来高急増
    breakout : 20日高値ブレイク + 出来高急増
    all      : 上記4モードを一括実行・比較
"""

from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch

from daytrade_donchian import (
    backtest_symbol as don_bt, calc_stats as don_stats,
    backtest_donchian_day as don_day,
)
from daytrade_gap_reversal import (
    backtest_symbol as gr_bt, calc_stats as gr_stats,
    backtest_day as gr_day,
)
from daytrade_vwap_rebound import (
    backtest_symbol as vwap_bt, calc_stats as vwap_stats,
    backtest_day as vwap_day,
)
from daytrade_opening_momentum import (
    backtest_symbol as om_bt, calc_stats as om_stats,
    backtest_day as om_day,
)

from daytrade_eod_selector import build_daily, select_for_date, SELECTORS

JST = timezone(timedelta(hours=9))
BUDGET = 600_000
MAX_RISK = 6_000


def _load_universe(name):
    if name == "watch":
        return DAYTRADE_SYMBOLS
    if name == "n225":
        from symbols_all import SYMBOLS
        return SYMBOLS
    raise ValueError(f"unknown universe: {name}")


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


# ─────────────────────────────────────────────────────────────
# 戦略実行 (全銘柄モード / walk-forwardモード)
# ─────────────────────────────────────────────────────────────

def run_strategy(name, bt_fn, stats_fn, fetched, targets, budget):
    """全銘柄 × 全日 で戦略実行 (selector=none)"""
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

    return dict(name=name, stats=stats, items=items,
                all_trades=all_trades, trade_days=len(trade_dates))


def run_strategy_walkforward(name, day_fn, stats_fn,
                             fetched, name_map, daily_data,
                             score_fn, top_n, budget, max_risk,
                             all_dates):
    """walk-forward: 各営業日について前日選定→当日デイトレ実行"""
    items_by_sym = {}

    for i, d in enumerate(all_dates):
        if i == 0:
            continue   # 前日データ必須
        prev_d = all_dates[i - 1]
        candidates = select_for_date(name_map, daily_data, score_fn, prev_d, top_n)
        for sym, sname, _score in candidates:
            df = fetched.get(sym)
            if df is None:
                continue
            day_df = df[df.index.date == d]
            if day_df.empty or len(day_df) < 5:
                continue
            prev_day_df = df[df.index.date == prev_d]
            if prev_day_df.empty:
                continue
            prev_close = float(prev_day_df.iloc[-1]["close"])
            day_trades = day_fn(day_df, prev_close, budget, max_risk)
            if not day_trades:
                continue
            if sym not in items_by_sym:
                items_by_sym[sym] = dict(symbol=sym, name=sname, trades=[])
            items_by_sym[sym]["trades"].extend(day_trades)

    items = list(items_by_sym.values())
    all_trades = sorted([t for it in items for t in it["trades"]],
                        key=lambda x: str(x.get("entry_dt", "")))
    stats = stats_fn(all_trades, budget)

    trade_dates = set()
    for t in all_trades:
        dt = t.get("entry_dt")
        if hasattr(dt, "date"):
            trade_dates.add(dt.date())

    return dict(name=name, stats=stats, items=items,
                all_trades=all_trades, trade_days=len(trade_dates))


# 4戦略 (whole_symbol_fn, day_fn, stats_fn)
STRATEGIES = [
    ("Donchian",       don_bt,  don_day,  don_stats),
    ("GapReversal",    gr_bt,   gr_day,   gr_stats),
    ("VWAPリバウンド",  vwap_bt, vwap_day, vwap_stats),
    ("寄付モメンタム",  om_bt,   om_day,   om_stats),
]


def run_mode(mode_name, score_fn, fetched, name_map, daily_data,
             targets, top_n, budget, all_dates):
    """1モードで4戦略実行。score_fn=Noneなら全銘柄、Noneでなければwalk-forward"""
    results = []
    for sname, whole_fn, day_fn, stats_fn in STRATEGIES:
        print(f"  [{sname}] ...", end="", flush=True)
        if score_fn is None:
            r = run_strategy(sname, whole_fn, stats_fn, fetched, targets, budget)
        else:
            r = run_strategy_walkforward(sname, day_fn, stats_fn,
                                         fetched, name_map, daily_data,
                                         score_fn, top_n, budget, MAX_RISK,
                                         all_dates)
        s = r["stats"]
        print(f" 取引:{s['n']:>4}  勝率:{s['win_rate']:>4.1f}%  "
              f"PF:{_pf(s['pf']):>5}  損益:{s['total_pnl']:>+10,.0f}")
        results.append(r)
    return results


# ─────────────────────────────────────────────────────────────
# 表示
# ─────────────────────────────────────────────────────────────

def print_comparison(mode_name, results, total_days):
    print(f"\n{'='*90}")
    print(f"  [{mode_name}] 4戦略比較 ({total_days}営業日)")
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


def print_mode_summary(modes_results, total_days):
    """全モードの戦略別 PF/損益 サマリ表示。"""
    if len(modes_results) <= 1:
        return
    print(f"\n{'='*90}")
    print(f"  モード別サマリ (PF / 損益)")
    print("=" * 90)
    strat_names = [r["name"] for r in modes_results[0][1]]
    header = f"{'モード':<12}" + "".join(f"{n:>18}" for n in strat_names)
    print(header)
    print("-" * 90)
    for mode_name, results in modes_results:
        row = f"{mode_name:<12}"
        for r in results:
            s = r["stats"]
            row += f"  {_pf(s['pf']):>5} / {s['total_pnl']:>+9,.0f}"
        print(row)
    print("=" * 90)


# ─────────────────────────────────────────────────────────────
# HTML出力
# ─────────────────────────────────────────────────────────────

def _comp_table(results, total_days):
    rows = ""
    for r in results:
        s = r["stats"]
        cls = "profit" if s["total_pnl"] >= 0 else "loss"
        day_rate = f"{r['trade_days']/total_days*100:.0f}%" if total_days > 0 else "-"
        rows += f"""<tr>
          <td class="sym">{r['name']}</td>
          <td>{s['n']}</td><td>{s['win_rate']:.1f}%</td>
          <td>{_pf(s['pf'])}</td>
          <td class="{cls}">{s['total_pnl']:+,.0f}</td>
          <td class="loss">{s['max_dd']:+.1f}%</td>
          <td>{r['trade_days']}/{total_days}</td>
          <td>{day_rate}</td></tr>"""
    return f"""<table><thead><tr><th>戦略</th><th>取引</th><th>勝率</th><th>PF</th>
        <th>損益</th><th>DD</th><th>取引日</th><th>日率</th></tr></thead>
        <tbody>{rows}</tbody></table>"""


def _strat_sections(results, budget):
    from daytrade_donchian import calc_stats
    out = ""
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
        out += f"""
        <h3>{r['name']}</h3>
        <table><thead><tr><th>銘柄</th><th>取引</th><th>勝率</th><th>PF</th>
        <th>損益</th><th>平均利益</th><th>平均損失</th><th>DD</th></tr></thead>
        <tbody>{sym_rows}</tbody></table>"""
    return out


def build_html(modes_results, total_days, days, budget, source, top_n):
    """複数モードを1つのHTMLにまとめる。"""
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # モード横断サマリ (PF/損益マトリクス)
    matrix_html = ""
    if len(modes_results) > 1:
        strat_names = [r["name"] for r in modes_results[0][1]]
        header = "<tr><th>モード</th>" + "".join(f"<th>{n}<br>PF / 損益</th>" for n in strat_names) + "</tr>"
        body = ""
        for mode_name, results in modes_results:
            row = f"<tr><td class='sym'>{mode_name}</td>"
            for r in results:
                s = r["stats"]
                cls = "profit" if s["total_pnl"] >= 0 else "loss"
                row += f"<td class='{cls}'>{_pf(s['pf'])}<br>{s['total_pnl']:+,.0f}</td>"
            row += "</tr>"
            body += row
        matrix_html = f"""
        <h2>モード横断サマリ</h2>
        <table><thead>{header}</thead><tbody>{body}</tbody></table>"""

    # 各モードの詳細
    sections = ""
    for mode_name, results in modes_results:
        sections += f"<h2>{mode_name}</h2>"
        sections += _comp_table(results, total_days)
        sections += _strat_sections(results, budget)

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>4戦略比較 — {today}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI","Hiragino Sans",sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}}
h1{{color:#10b981;margin-bottom:4px;font-size:1.5rem}}
.sub{{color:#94a3b8;margin-bottom:20px;font-size:.85rem}}
h2{{color:#10b981;margin:24px 0 10px;font-size:1.15rem;border-left:4px solid #10b981;padding-left:10px}}
h3{{color:#a7f3d0;margin:14px 0 6px;font-size:.95rem}}
table{{width:100%;border-collapse:collapse;margin-bottom:14px;font-size:.82rem}}
th{{background:#1e293b;color:#94a3b8;padding:6px 8px;text-align:center;border:1px solid #334155;white-space:nowrap}}
td{{padding:5px 8px;border:1px solid #1e293b;text-align:right;white-space:nowrap}}
.sym{{text-align:left;font-weight:600;min-width:140px}}
.code{{color:#64748b;font-weight:400;font-size:.75rem}}
.profit{{color:#4ade80}}.loss{{color:#f87171}}
</style></head><body>
<h1>4戦略比較レポート (前日選定対応)</h1>
<p class="sub">生成:{today} / {days}日 / source:{source} / 予算:{budget:,}円 /
 {total_days}営業日 / TOP{top_n}</p>
{matrix_html}
{sections}
</body></html>"""


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="4戦略比較バックテスト")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--universe", choices=["watch", "n225"], default="watch",
                        help="銘柄ユニバース: watch=DAYTRADE_SYMBOLS(20), n225=日経225全銘柄")
    parser.add_argument("--selector",
                        choices=["none", "simple", "multi", "breakout", "all"],
                        default="none",
                        help="前日終値ベースの翌日候補セレクター")
    parser.add_argument("--top-n", type=int, default=20,
                        help="セレクターが選ぶ銘柄数 (selector=noneでは無視)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    targets = _load_universe(args.universe)
    symbols = [s for s, _ in targets]
    print(f"4戦略比較 [universe={args.universe} / selector={args.selector} / top={args.top_n}]: "
          f"{len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円",
          flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    name_map = dict(targets)
    print(f"  予算フィルタ後: {len(fetched)}銘柄\n", flush=True)

    all_dates = set()
    for df in fetched.values():
        all_dates.update(df.index.date)
    all_dates = sorted(all_dates)
    total_days = len(all_dates)

    # 5分足 → 日足 (selectorで使用)
    daily_data = build_daily(fetched) if args.selector != "none" else {}

    # 実行モードリスト
    if args.selector == "none":
        modes = [("全銘柄", None)]
    elif args.selector == "all":
        modes = [
            ("全銘柄", None),
            ("simple", SELECTORS["simple"]),
            ("multi", SELECTORS["multi"]),
            ("breakout", SELECTORS["breakout"]),
        ]
    else:
        modes = [(args.selector, SELECTORS[args.selector])]

    modes_results = []
    for mode_name, score_fn in modes:
        print(f"\n=== モード: {mode_name} ===", flush=True)
        results = run_mode(mode_name, score_fn, fetched, name_map, daily_data,
                           targets, args.top_n, args.budget, all_dates)
        print_comparison(mode_name, results, total_days)
        modes_results.append((mode_name, results))

    # モード横断サマリ
    print_mode_summary(modes_results, total_days)

    # HTML出力
    stamp = datetime.now(JST).strftime('%Y%m%d')
    out = Path(f"daytrade_compare_{args.universe}_{args.selector}_{stamp}.html")
    out.write_text(build_html(modes_results, total_days, args.days,
                              args.budget, args.source, args.top_n),
                   encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
