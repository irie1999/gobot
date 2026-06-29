"""
scan_wf_strategies.py  ―  独立デイトレ戦略の WalkForward(OOS) 検証
==================================================================
daytrade_*.py の各戦略 (backtest_symbol) を fold 期間でスライス実行し、
TRAIN で選び TEST(擬似未来) で検証する WalkForward スキャナー。

daytrade_compare.py は全期間 in-sample なので過学習を見抜けない。本スクリプトは
時期をずらした 2 fold で OOS 検証し、両 fold を通過した銘柄のみを候補とする。

【使い方】
  python scan_wf_strategies.py --limit 50 --workers 8            # 全戦略・先頭50銘柄
  python scan_wf_strategies.py --strategy Donchian               # 戦略指定
  python scan_wf_strategies.py --max-price 6000 --min-price 1000 # 予算フィルタ
  python scan_wf_strategies.py                                   # プライム全銘柄

出力: wf_strategies_<STRATEGY>_<date>.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday
from scan_walkforward_intraday import load_universe

# 独立戦略の backtest_symbol を登録
import daytrade_donchian
import daytrade_gap_reversal
import daytrade_vwap_rebound
import daytrade_opening_momentum
import daytrade_macd_break
import daytrade_stoch_atr
import daytrade_volsurge
import daytrade_pivot
import daytrade_rsi

STRATEGIES = {
    "Donchian":     daytrade_donchian.backtest_symbol,
    "GapReversal":  daytrade_gap_reversal.backtest_symbol,
    "VWAP":         daytrade_vwap_rebound.backtest_symbol,
    "OpenMomentum": daytrade_opening_momentum.backtest_symbol,
    "MACDBreak":    daytrade_macd_break.backtest_symbol,
    "StochATR":     daytrade_stoch_atr.backtest_symbol,
    "VolSurge":     daytrade_volsurge.backtest_symbol,
    "Pivot":        daytrade_pivot.backtest_symbol,
    "RSI":          daytrade_rsi.backtest_symbol,
}

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# fold 設計 (intraday WF と同じ): (名前, TRAIN開始, TRAIN終了, TEST開始, TEST終了) 日前
FOLDS = [
    ("fold1", 730, 370, 370, 180),
    ("fold2", 550, 180, 180, 0),
]
TRAIN_MIN_TRADES, TRAIN_MIN_PF, TRAIN_MIN_WR = 5, 1.1, 45.0
TEST_MIN_TRADES,  TEST_MIN_PF,  TEST_MIN_WR  = 3, 1.0, 40.0
FOLDS_PASS_REQUIRED = 2


def _run_strat(fn, sym, name, df):
    """戦略の backtest_symbol を呼ぶ (budget/max_risk 必須の戦略にも対応)。"""
    try:
        return fn(sym, name, df)
    except TypeError:
        return fn(sym, name, df, 600000, 6000)


def _stats(trades: list[dict]) -> dict | None:
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t["pnl"] > 0)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return dict(n=n, wr=wins / n * 100, pf=pf,
                pnl=sum(t["pnl"] for t in trades))


def _slice(df: pd.DataFrame, start_days: int, end_days: int) -> pd.DataFrame:
    s = TODAY - timedelta(days=start_days)
    e = TODAY - timedelta(days=end_days)
    d = df.index.date
    return df[(d >= s) & (d <= e)]


def _passes(st: dict | None, mt: int, mpf: float, mwr: float) -> bool:
    return bool(st and st["n"] >= mt and st["pf"] >= mpf
               and st["wr"] >= mwr and st["pnl"] > 0)


def _cap_pf(p):
    return 10.0 if p == float("inf") else min(p, 10.0)


def wf_one(fn, sym: str, name: str, df: pd.DataFrame) -> dict | None:
    """1銘柄×1戦略の WalkForward。両 fold 通過なら集計 dict、不合格なら None。"""
    folds_passed = 0
    test_stats: list[dict] = []
    train_pnl_total = 0.0
    for _, ts, te, vs, ve in FOLDS:
        df_tr = _slice(df, ts, te)
        df_te = _slice(df, vs, ve)
        if len(df_tr) < 50 or len(df_te) < 50:
            continue
        r_tr = _run_strat(fn, sym, name, df_tr)
        r_te = _run_strat(fn, sym, name, df_te)
        st_tr = _stats(r_tr["trades"]) if r_tr else None
        st_te = _stats(r_te["trades"]) if r_te else None
        if (_passes(st_tr, TRAIN_MIN_TRADES, TRAIN_MIN_PF, TRAIN_MIN_WR)
                and _passes(st_te, TEST_MIN_TRADES, TEST_MIN_PF, TEST_MIN_WR)):
            folds_passed += 1
        if st_tr:
            train_pnl_total += st_tr["pnl"]
        if st_te:
            test_stats.append(st_te)
    if folds_passed < FOLDS_PASS_REQUIRED or not test_stats:
        return None
    tot_test_pnl = sum(s["pnl"] for s in test_stats)
    tot_test_tr = sum(s["n"] for s in test_stats)
    avg_pf = sum(_cap_pf(s["pf"]) for s in test_stats) / len(test_stats)
    avg_wr = sum(s["wr"] for s in test_stats) / len(test_stats)
    try:
        latest = float(df["close"].iloc[-1])
    except Exception:
        latest = 0.0
    return dict(symbol=sym, name=name, latest_price=round(latest, 0),
                folds_passed=folds_passed, total_test_trades=tot_test_tr,
                total_test_pnl=round(tot_test_pnl, 0),
                total_train_pnl=round(train_pnl_total, 0),
                avg_test_pf=round(avg_pf, 2), avg_test_wr=round(avg_wr, 1))


def main() -> None:
    ap = argparse.ArgumentParser(description="独立デイトレ戦略の WalkForward 検証")
    ap.add_argument("--strategy", default=None,
                    choices=list(STRATEGIES.keys()) + ["ALL"])
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--data-days", type=int, default=800)
    ap.add_argument("--max-price", type=float, default=0.0)
    ap.add_argument("--min-price", type=float, default=0.0)
    ap.add_argument("--budget", type=float, default=0.0)
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    eff_max = args.max_price or (args.budget / 100.0 if args.budget else 0.0)
    strategies = ([args.strategy] if args.strategy and args.strategy != "ALL"
                  else list(STRATEGIES.keys()))

    symbols, uni = load_universe(args.symbols)
    if args.limit > 0:
        symbols = symbols[:args.limit]

    print("=" * 78)
    print("独立戦略 WalkForward 検証")
    print(f"  基準日: {TODAY}  ユニバース: {uni} ({len(symbols)}銘柄)")
    print(f"  戦略: {', '.join(strategies)}  並列: {args.workers}")
    print(f"  合格: TRAIN n>={TRAIN_MIN_TRADES} PF>={TRAIN_MIN_PF} WR>={TRAIN_MIN_WR}"
          f" / TEST n>={TEST_MIN_TRADES} PF>={TEST_MIN_PF} WR>={TEST_MIN_WR}"
          f" / {FOLDS_PASS_REQUIRED}fold")
    for nm, ts, te, vs, ve in FOLDS:
        print(f"  {nm}: TRAIN {TODAY-timedelta(days=ts)}〜{TODAY-timedelta(days=te)}"
              f" / TEST {TODAY-timedelta(days=vs)}〜{TODAY-timedelta(days=ve)}")
    print("=" * 78)

    def worker(sym, name):
        df = load_intraday(sym, days=args.data_days, source="local")
        if df is None or df.empty or len(df) < 100:
            return {}
        if eff_max > 0 or args.min_price > 0:
            try:
                lp = float(df["close"].iloc[-1])
            except Exception:
                return {}
            if eff_max > 0 and lp > eff_max:
                return {}
            if args.min_price > 0 and lp < args.min_price:
                return {}
        out = {}
        for s in strategies:
            try:
                r = wf_one(STRATEGIES[s], sym, name, df)
            except Exception:
                r = None
            if r:
                out[s] = r
        return out

    per_strategy: dict[str, list[dict]] = {s: [] for s in strategies}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(worker, s, n): s for s, n in symbols}
        for fut in as_completed(futs):
            done += 1
            try:
                d = fut.result()
            except Exception:
                d = {}
            for s, r in d.items():
                per_strategy[s].append(r)
            if done % max(len(symbols) // 20, 25) == 0:
                tot = sum(len(v) for v in per_strategy.values())
                print(f"  進捗 {done}/{len(symbols)}  候補(延べ) {tot}", flush=True)

    out_dir = Path("walkforward_results")
    out_dir.mkdir(exist_ok=True)
    fields = ["symbol", "name", "latest_price", "folds_passed",
              "total_test_trades", "total_test_pnl", "total_train_pnl",
              "avg_test_pf", "avg_test_wr"]
    for s in strategies:
        rows = sorted(per_strategy[s],
                      key=lambda r: (-r["folds_passed"], -r["total_test_pnl"]))
        csv_path = out_dir / f"wf_strategies_{s}_{TODAY}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\n=== {s}: 合格 {len(rows)}銘柄  → {csv_path.name} ===")
        for r in rows[:args.top]:
            print(f"  {r['symbol']:<9}{r['name'][:18]:<20}"
                  f"{r['latest_price']:>7,.0f}  fld{r['folds_passed']}"
                  f"  取引{r['total_test_trades']:>4}"
                  f"  TEST損益{r['total_test_pnl']:>+11,.0f}"
                  f"  PF{r['avg_test_pf']:>5.2f}  WR{r['avg_test_wr']:>5.1f}%")
        if not rows:
            print("  (合格銘柄なし)")

    print(f"\n完了。CSV は {out_dir.resolve()}")


if __name__ == "__main__":
    main()
