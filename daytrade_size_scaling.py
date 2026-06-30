"""
daytrade_size_scaling.py  ―  サイズ/資金を上げたら月利益とDDがどうなるか試算
==================================================================
デイトレの月利益が小さいのは「投入資金が小さい」から。資金(budget)と
1トレード最大リスク(max_risk)を同倍率で上げると、利益もDDもほぼ線形に増える。
それを実バックテストで試算し、許容できるDDの範囲を見極める。

各スケール k について:
  budget   = base_budget × k
  max_risk = base_risk  × k
で daytrade_watchlist の全(銘柄×戦略)を再バックテストし、
  月平均利益 / 総損益 / MaxDD% / 平均ポジション円 を集計する。

★注意: バックテストは流動性(約定の滑り)を完全にはモデル化しない。
  小型株に大きいサイズを入れると実際は約定が滑って利益が削れる。
  下の「平均ポジション円」が大きくなりすぎたら現実味が下がる。

使い方:
  python daytrade_size_scaling.py
  python daytrade_size_scaling.py --scales 1 2 3 5 10 --days 365
"""
from __future__ import annotations

import argparse
import importlib
from collections import defaultdict
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

STRAT_MODULES = {
    "Donchian": "daytrade_donchian", "GapReversal": "daytrade_gap_reversal",
    "VWAP": "daytrade_vwap_rebound", "RSI": "daytrade_rsi",
    "Pivot": "daytrade_pivot", "StopShort": "daytrade_stop_short",
}


def main():
    ap = argparse.ArgumentParser(description="デイトレ サイズ拡大 試算")
    ap.add_argument("--scales", type=float, nargs="+", default=[1, 2, 3, 5, 10])
    ap.add_argument("--base-budget", type=int, default=600_000)
    ap.add_argument("--base-risk", type=int, default=6_000)
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()

    try:
        wl = importlib.import_module("daytrade_watchlist").WATCHLIST
    except Exception:
        print("daytrade_watchlist.py が無いか壊れています。"
              "build_watchlist_wf.py を先に実行してください。")
        return
    from daytrade_data import load_intraday
    from risk_metrics import calc_max_drawdown

    # df を一度だけロード
    pairs = []   # (mod, code, name, df)
    for strat, syms in wl.items():
        mod_name = STRAT_MODULES.get(strat)
        if not mod_name:
            continue
        mod = importlib.import_module(mod_name)
        for code, name in syms:
            sym = code if str(code).endswith(".T") else f"{code}.T"
            try:
                df = load_intraday(sym, days=args.days, source="local")
            except Exception:
                df = None
            if df is not None and not df.empty:
                pairs.append((mod, sym, name, df))
    if not pairs:
        print("データが読めませんでした。")
        return
    print(f"サイズ試算: {len(pairs)}(銘柄×戦略) / {args.days}日 / "
          f"base予算{args.base_budget:,} risk{args.base_risk:,}", flush=True)

    rows = []
    for k in args.scales:
        budget = int(args.base_budget * k)
        risk = int(args.base_risk * k)
        trades = []
        for mod, sym, name, df in pairs:
            try:
                r = mod.backtest_symbol(sym, name, df, budget, risk)
            except Exception:
                r = None
            if r:
                trades.extend(r.get("trades", []))
        trades.sort(key=lambda t: t.get("entry_dt") or datetime.now())
        if not trades:
            rows.append((k, budget, 0, 0.0, 0.0, 0.0, 0))
            continue
        total = sum(t.get("pnl", 0) for t in trades)
        months = defaultdict(float)
        for t in trades:
            ed = t.get("entry_dt")
            if ed:
                months[(ed.year, ed.month)] += t.get("pnl", 0)
        nmonths = max(1, len(months))
        monthly = total / nmonths
        _, dd_pct = calc_max_drawdown(trades, budget)
        avg_pos = sum(t.get("entry_p", 0) * t.get("qty", 0) for t in trades) / len(trades)
        rows.append((k, budget, len(trades), monthly, total, dd_pct, avg_pos))

    print("\n" + "=" * 92)
    print("  サイズ拡大 試算 (資金とリスクを同倍率で拡大)")
    print("=" * 92)
    print(f"  {'倍率':>5}{'予算':>12}{'取引':>6}{'月平均利益':>13}"
          f"{'総損益':>14}{'MaxDD%':>9}{'平均ポジ円':>13}")
    print("  " + "-" * 88)
    for k, budget, n, monthly, total, dd, avgpos in rows:
        print(f"  {k:>4.0f}x{budget:>12,}{n:>6}{monthly:>+13,.0f}"
              f"{total:>+14,.0f}{dd:>8.1f}%{avgpos:>13,.0f}")
    print("=" * 92)
    print("読み方:")
    print("  ・月平均利益とMaxDDはほぼ同倍率で増える(エッジは変わらず規模だけ拡大)")
    print("  ・『平均ポジ円』が大きいほど流動性リスク大(実際は約定が滑り利益が削れる)")
    print("  ・許容できるMaxDD(円)から逆算して、現実的な倍率を選ぶ")
    print("=" * 92)


if __name__ == "__main__":
    main()
