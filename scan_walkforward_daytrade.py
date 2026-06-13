"""
scan_walkforward_daytrade.py  ―  デイトレ多戦略 Walk-forward スキャナ
==================================================================
逆指値ロング (scan_walkforward.py) を5分足デイトレ用に移植。

【戦略】
  DON / MACD / RSI2 / A7 / VOL / MOM の6戦略

【WF構造】 (基準日=本日、単位=暦日)
  Fold 1: TRAIN 540〜360日前 / TEST 360〜180日前
  Fold 2: TRAIN 360〜180日前 / TEST 180〜90日前
  Fold 3: TRAIN 180〜90日前  / TEST 90〜0日前

【合格条件】
  TRAIN: trades>=10, PF>=1.3, win_rate>=50%, total_pnl>0
  TEST : trades>=5,  PF>=1.1, win_rate>=45%, total_pnl>0
  選定 : 3 fold中 TRAIN+TEST 両方合格が 2 以上

【出力】
  walkforward_daytrade_<STRATEGY>_<YYYY-MM-DD>.csv

【使い方】
  python scan_walkforward_daytrade.py
  python scan_walkforward_daytrade.py --strategy MACD
  python scan_walkforward_daytrade.py --universe prime --workers 4
  python scan_walkforward_daytrade.py --max-price 6000 --min-price 1000
"""

from __future__ import annotations

import argparse
import csv
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m, calc_stats
from daytrade_strategies_5m import STRATEGIES

JST = timezone(timedelta(hours=9))

# Walk-forward fold (デイトレ向けに短く再設定)
FOLDS = [
    ("Fold1", 540, 360, 360, 180),   # TRAIN 540-360, TEST 360-180
    ("Fold2", 360, 180, 180, 90),    # TRAIN 360-180, TEST 180-90
    ("Fold3", 180, 90,  90,  0),     # TRAIN 180-90, TEST 90-0
]

# 合格条件 (デイトレ向け緩和: 取引機会が多いため取引数閾値を上げる)
PASS_TRAIN = dict(trades=10, pf=1.3, win_rate=50, pnl=0)
PASS_TEST  = dict(trades=5,  pf=1.1, win_rate=45, pnl=0)
MIN_FOLDS  = 2  # 3 fold中 2 fold以上で合格


def slice_trades(trades, start_days_ago, end_days_ago, today):
    """トレードを start-end 日数前の期間でフィルタ。"""
    start = today - timedelta(days=start_days_ago)
    end = today - timedelta(days=end_days_ago)
    out = []
    for t in trades:
        dt = t.get("entry_dt")
        if not hasattr(dt, "date"):
            continue
        d = dt.date()
        if start <= d < end:
            out.append(t)
    return out


def evaluate_fold(trades, today, train_start, train_end, test_start, test_end,
                  budget):
    """1 fold分の TRAIN/TEST 統計を計算。"""
    train_trades = slice_trades(trades, train_start, train_end, today)
    test_trades = slice_trades(trades, test_start, test_end, today)
    return calc_stats(train_trades, budget), calc_stats(test_trades, budget)


def pass_condition(stats, cond):
    """合格条件チェック。"""
    return (stats["n"] >= cond["trades"]
            and stats["pf"] >= cond["pf"]
            and stats["win_rate"] >= cond["win_rate"]
            and stats["total_pnl"] > cond["pnl"])


def scan_one(sym, name, df, strategy_name, strategy_fn, today, budget, max_risk):
    """1銘柄を全期間でbacktest → 3 fold WF評価。"""
    r = backtest_symbol_5m(sym, name, df, strategy_fn,
                            strategy_params={"name": strategy_name},
                            budget=budget, max_risk=max_risk)
    if not r or not r["trades"]:
        return None

    trades = r["trades"]
    fold_results = []
    pass_count = 0
    for fold_name, ts, te, vs, ve in FOLDS:
        train_stats, test_stats = evaluate_fold(
            trades, today, ts, te, vs, ve, budget)
        train_ok = pass_condition(train_stats, PASS_TRAIN)
        test_ok = pass_condition(test_stats, PASS_TEST)
        if train_ok and test_ok:
            pass_count += 1
        fold_results.append({
            "fold": fold_name,
            "train_n": train_stats["n"],
            "train_pf": train_stats["pf"],
            "train_wr": train_stats["win_rate"],
            "train_pnl": train_stats["total_pnl"],
            "test_n": test_stats["n"],
            "test_pf": test_stats["pf"],
            "test_wr": test_stats["win_rate"],
            "test_pnl": test_stats["total_pnl"],
            "train_pass": train_ok,
            "test_pass": test_ok,
        })

    if pass_count < MIN_FOLDS:
        return None

    latest_price = float(df.iloc[-1]["close"]) if not df.empty else 0
    # 全期間集計 (情報用)
    all_stats = calc_stats(trades, budget)
    return {
        "symbol": sym,
        "name": name,
        "strategy": strategy_name,
        "latest_price": latest_price,
        "pass_folds": pass_count,
        "total_trades": all_stats["n"],
        "total_pf": all_stats["pf"],
        "total_win_rate": all_stats["win_rate"],
        "total_pnl": all_stats["total_pnl"],
        "total_dd": all_stats["max_dd"],
        "sharpe": all_stats["sharpe"],
        "max_losing_streak": all_stats["max_losing_streak"],
        "folds": fold_results,
    }


def main():
    parser = argparse.ArgumentParser(description="デイトレ多戦略WFスキャナ")
    parser.add_argument("--strategy", default="all",
                        choices=["all", "DON", "MACD", "RSI2", "A7", "VOL", "MOM"])
    parser.add_argument("--universe", default="prime",
                        choices=["prime", "winners"])
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--max-price", type=int, default=10_000)
    parser.add_argument("--min-price", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # ユニバース
    if args.universe == "prime":
        from symbols_listed_all import SYMBOLS
        targets = SYMBOLS
    else:
        from daytrade_donchian_winners import SYMBOLS
        targets = SYMBOLS

    if args.limit > 0:
        targets = targets[:args.limit]

    today = datetime.now(JST).date()
    strategies = list(STRATEGIES.keys()) if args.strategy == "all" else [args.strategy]

    print(f"=" * 70)
    print(f"  デイトレ Walk-Forward スキャン")
    print(f"=" * 70)
    print(f"  ユニバース: {args.universe} ({len(targets)}銘柄)")
    print(f"  戦略: {strategies}")
    print(f"  期間: {args.days}日 / 価格: {args.min_price:,}-{args.max_price:,}円")
    print(f"  Folds: {len(FOLDS)} / 合格基準: TRAIN+TEST 両方 ≥{MIN_FOLDS} fold")

    # データロード
    print(f"\n[Step 1] データロード", flush=True)
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    # 価格フィルタ
    before = len(fetched)
    if args.max_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) <= args.max_price}
    if args.min_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) >= args.min_price}
    print(f"  ロード: {before}銘柄 → 価格フィルタ後: {len(fetched)}銘柄")
    targets = [(s, n) for s, n in targets if s in fetched]

    # 各戦略でスキャン
    out_dir = Path("walkforward_daytrade_results")
    out_dir.mkdir(exist_ok=True)

    for strat in strategies:
        print(f"\n[戦略 {strat}] スキャン開始 ({len(targets)}銘柄)", flush=True)
        strat_fn = STRATEGIES[strat]
        t0 = _time.time()
        results = []

        def _work(sym, name):
            try:
                return scan_one(sym, name, fetched[sym], strat, strat_fn,
                                today, args.budget, args.max_risk)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_work, s, n): (s, n) for s, n in targets}
            for i, fut in enumerate(as_completed(futures), 1):
                r = fut.result()
                if r:
                    results.append(r)
                if i % 100 == 0 or i == len(targets):
                    print(f"  {i}/{len(targets)} ({_time.time()-t0:.1f}s, "
                          f"合格:{len(results)})", flush=True)

        # CSV出力
        if results:
            results.sort(key=lambda x: -x["total_pnl"])
            csv_path = out_dir / f"walkforward_{strat}_{today}.csv"
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                fieldnames = ["symbol", "name", "strategy", "latest_price",
                              "pass_folds", "total_trades", "total_pf",
                              "total_win_rate", "total_pnl", "total_dd",
                              "sharpe", "max_losing_streak"]
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for r in results:
                    w.writerow({k: r.get(k, "") for k in fieldnames})
            print(f"\n  ★ {strat}: {len(results)}銘柄合格 → {csv_path}")
            # Top 10 表示
            print(f"\n  Top 10 ({strat}):")
            print(f"  {'銘柄':<22} {'コード':<10} {'PF':>5} {'勝率':>5} "
                  f"{'損益':>10} {'fold':>5}")
            for r in results[:10]:
                disp = r["name"][:20] if len(r["name"]) <= 20 else r["name"][:19] + "…"
                pf_str = "∞" if r["total_pf"] == float("inf") else f"{r['total_pf']:.2f}"
                print(f"  {disp:<22} {r['symbol']:<10} {pf_str:>5} "
                      f"{r['total_win_rate']:>4.0f}% {r['total_pnl']:>+10,.0f} "
                      f"{r['pass_folds']}/3")
        else:
            print(f"\n  ✗ {strat}: 合格銘柄なし")


if __name__ == "__main__":
    main()
