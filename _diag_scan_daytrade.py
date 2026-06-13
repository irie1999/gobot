"""
_diag_scan_daytrade.py  ―  scan_walkforward_daytrade 診断
==================================================================
合格0だった原因を特定。

確認項目:
1. 各戦略がそもそもシグナルを発生させているか
2. 取引が成立しているか
3. PFが閾値以下なら、平均値はいくつか
4. fold別の取引数分布

【使い方】
  python _diag_scan_daytrade.py
  python _diag_scan_daytrade.py --strategy DON
  python _diag_scan_daytrade.py --limit 100
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import numpy as np

from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m, calc_stats
from daytrade_strategies_5m import STRATEGIES
from daytrade_strategies_5m_short import STRATEGIES_SHORT
from scan_walkforward_daytrade import FOLDS, slice_trades

JST = timezone(timedelta(hours=9))

ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}


def diagnose_one(sym, name, df, strategy, today):
    """1銘柄の診断情報を取得。"""
    fn = ALL_STRATEGIES[strategy]
    r = backtest_symbol_5m(sym, name, df, fn, {"name": strategy})
    if not r:
        return None
    trades = r["trades"]
    if not trades:
        return {"sym": sym, "name": name, "n_total": 0,
                "fold_stats": [], "n_pos_days": 0}

    # 全体
    all_stats = calc_stats(trades)

    # Fold別
    fold_stats = []
    for fold_name, ts, te, vs, ve in FOLDS:
        train = slice_trades(trades, ts, te, today)
        test = slice_trades(trades, vs, ve, today)
        train_s = calc_stats(train)
        test_s = calc_stats(test)
        fold_stats.append({
            "name": fold_name,
            "train_n": train_s["n"],
            "train_pf": train_s["pf"],
            "train_wr": train_s["win_rate"],
            "train_pnl": train_s["total_pnl"],
            "test_n": test_s["n"],
            "test_pf": test_s["pf"],
            "test_wr": test_s["win_rate"],
            "test_pnl": test_s["total_pnl"],
        })

    return {
        "sym": sym, "name": name,
        "n_total": all_stats["n"],
        "pf_total": all_stats["pf"],
        "wr_total": all_stats["win_rate"],
        "pnl_total": all_stats["total_pnl"],
        "fold_stats": fold_stats,
    }


def main():
    parser = argparse.ArgumentParser(description="scan診断")
    parser.add_argument("--strategy", default="DON")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-price", type=int, default=6000)
    parser.add_argument("--min-price", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    from symbols_listed_all import SYMBOLS
    targets = SYMBOLS[:args.limit] if args.limit > 0 else SYMBOLS

    print(f"診断: {args.strategy} / {len(targets)}銘柄 / 期間{args.days}日")

    # データロード
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    fetched = {s: df for s, df in fetched.items()
               if args.min_price <= float(df.iloc[-1]["close"]) <= args.max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  価格フィルタ後: {len(targets)}銘柄\n")

    today = datetime.now(JST).date()
    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(diagnose_one, s, n, fetched[s], args.strategy, today):
                s for s, n in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass
            if i % 50 == 0:
                print(f"  {i}/{len(targets)} ({time.time()-t0:.1f}s)", flush=True)

    print(f"\n結果集計 ({len(results)}銘柄):")

    # 取引数分布
    counts = [r["n_total"] for r in results]
    if counts:
        print(f"\n[全体取引数]")
        print(f"  最大: {max(counts)}")
        print(f"  平均: {np.mean(counts):.1f}")
        print(f"  中央値: {np.median(counts):.0f}")
        print(f"  0取引: {sum(1 for c in counts if c == 0)}/{len(counts)}")
        print(f"  ≥5取引: {sum(1 for c in counts if c >= 5)}/{len(counts)}")
        print(f"  ≥20取引: {sum(1 for c in counts if c >= 20)}/{len(counts)}")

    # PF分布
    pfs = [r["pf_total"] for r in results
           if r["n_total"] > 0 and r["pf_total"] != float("inf")]
    if pfs:
        print(f"\n[全体PF]")
        print(f"  最大: {max(pfs):.2f}")
        print(f"  平均: {np.mean(pfs):.2f}")
        print(f"  中央値: {np.median(pfs):.2f}")
        print(f"  ≥1.0: {sum(1 for p in pfs if p >= 1.0)}/{len(pfs)}")
        print(f"  ≥1.3: {sum(1 for p in pfs if p >= 1.3)}/{len(pfs)}")
        print(f"  ≥1.5: {sum(1 for p in pfs if p >= 1.5)}/{len(pfs)}")

    # Fold別取引数
    for fold_idx in range(3):
        print(f"\n[Fold{fold_idx+1}]")
        train_n = [r["fold_stats"][fold_idx]["train_n"]
                   for r in results if r["fold_stats"]]
        test_n = [r["fold_stats"][fold_idx]["test_n"]
                  for r in results if r["fold_stats"]]
        if train_n:
            print(f"  TRAIN 取引数 中央値: {np.median(train_n):.0f} "
                  f"/ 最大: {max(train_n)} / ≥3: {sum(1 for n in train_n if n >= 3)}")
            print(f"  TEST  取引数 中央値: {np.median(test_n):.0f} "
                  f"/ 最大: {max(test_n)} / ≥2: {sum(1 for n in test_n if n >= 2)}")

    # Top 10
    sorted_r = sorted(results, key=lambda r: -r["pnl_total"])
    print(f"\n[Top 10 (全期間損益順)]")
    print(f"  {'銘柄':<20} {'コード':<8} {'取引':>5} {'PF':>5} {'勝率':>5} {'損益':>10}")
    for r in sorted_r[:10]:
        disp = r["name"][:18]
        pf_str = "∞" if r["pf_total"] == float("inf") else f"{r['pf_total']:.2f}"
        print(f"  {disp:<20} {r['sym']:<8} {r['n_total']:>5} "
              f"{pf_str:>5} {r['wr_total']:>4.0f}% {r['pnl_total']:>+10,.0f}")


if __name__ == "__main__":
    main()
