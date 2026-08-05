"""
optimize_params.py  ―  sm / tm グリッドサーチ
======================================================
stop_atr_mult (sm) と target_atr_mult (tm) の組み合わせを
総当たりでバックテストし、最適パラメータを探す。

【使い方】
  python optimize_params.py                   # 全戦略・365日
  python optimize_params.py --strategy MACD   # MACD のみ
  python optimize_params.py --days 180        # 直近180日
  python optimize_params.py --workers 8       # 並列数
  python optimize_params.py --top 20          # 上位20件表示
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from itertools import product

os.environ["TRADING_MODE"] = "conservative"  # WATCHLIST読み込み用(モード関係なし)

from backtest_limit_entry import (
    fetch, calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)
from scan_breakout_entry import calc_donchian, calc_vol_breakout, calc_momentum
import check_signals_stop     as _stop
import check_signals_breakout as _brk

JST = timezone(timedelta(hours=9))

# ── テスト対象の sm / tm グリッド ──────────────────────────────────────────
# tm >= sm の組み合わせのみ有効（R倍率 >= 1.0）
SM_RANGE = [0.5, 1.0, 1.5, 2.0, 2.5]
TM_RANGE = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]

GRID = [(sm, tm) for sm, tm in product(SM_RANGE, TM_RANGE) if tm >= sm]

# ── 戦略定義 ──────────────────────────────────────────────────────────────
ALL_STRATEGIES = {
    "MACD": (calc_macd,        "stop"),
    "A7":   (calc_a7,          "stop"),
    "RSI2": (calc_rsi2,        "stop"),
    "DON":  (calc_donchian,    "stop"),
    "VOL":  (calc_vol_breakout,"stop"),
    "MOM":  (calc_momentum,    "stop"),
}

# 各戦略のWATCHLIST (既存銘柄)
WATCHLISTS = {
    "MACD": [(s, n) for s, n, st in _stop.WATCHLIST if st == "MACD"],
    "A7":   [(s, n) for s, n, st in _stop.WATCHLIST if st == "A7"],
    "RSI2": [(s, n) for s, n, st in _stop.WATCHLIST if st == "RSI2"],
    "DON":  [(s, n) for s, n, st in _brk.WATCHLIST  if st == "DON"],
    "VOL":  [(s, n) for s, n, st in _brk.WATCHLIST  if st == "VOL"],
    "MOM":  [(s, n) for s, n, st in _brk.WATCHLIST  if st == "MOM"],
}


def _backtest_combo(symbol, name, calc_fn, entry_type, sm, tm, days, df):
    """1銘柄 × 1(sm,tm) のバックテスト。"""
    try:
        r = run_limit_backtest(symbol, name, df, calc_fn,
                               0.0, sm, tm, days, "OPT",
                               entry_type=entry_type)
        return r
    except Exception:
        return None


def run_grid_search(strategy: str, days: int, workers: int) -> list[dict]:
    """1戦略 × 全(sm,tm) のグリッドサーチ。"""
    calc_fn, entry_type = ALL_STRATEGIES[strategy]
    watchlist = WATCHLISTS[strategy]

    # データを事前取得（銘柄ごとに1回だけ）
    print(f"  データ取得中 ({len(watchlist)}銘柄)...", flush=True)
    dfs = {}
    for sym, name in watchlist:
        df = fetch(sym, days + 60)
        if df is not None:
            dfs[sym] = (name, df)

    print(f"  グリッドサーチ {len(GRID)}組 × {len(dfs)}銘柄...", flush=True)

    # (sm, tm) ごとに全銘柄を集計
    results = []
    tasks = [(sm, tm, sym, name, df) for sm, tm in GRID
             for sym, (name, df) in dfs.items()]

    combo_results: dict[tuple, list] = {(sm, tm): [] for sm, tm in GRID}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(_backtest_combo, sym, name, calc_fn, entry_type, sm, tm, days, df): (sm, tm)
            for sm, tm, sym, name, df in tasks
        }
        for fut in as_completed(futs):
            sm_tm = futs[fut]
            r = fut.result()
            if r and r.get("trades", 0) > 0:
                combo_results[sm_tm].append(r)

    # 集計
    for (sm, tm), rs in combo_results.items():
        if not rs:
            continue
        total_trades = sum(r["trades"] for r in rs)
        total_wins   = sum(r["wins"]   for r in rs)
        total_pnl    = sum(r["total_pnl"] for r in rs)
        total_loss   = sum(r.get("total_loss", 0) for r in rs)
        win_rate     = total_wins / total_trades if total_trades else 0
        pf           = (total_pnl / abs(total_loss)) if total_loss < 0 else float("inf")
        r_ratio      = tm / sm
        # 期待値スコア: win_rate * tm - (1-win_rate) * sm
        ev           = win_rate * tm - (1 - win_rate) * sm

        results.append(dict(
            sm=sm, tm=tm,
            r_ratio=round(r_ratio, 2),
            trades=total_trades,
            wins=total_wins,
            win_rate=round(win_rate * 100, 1),
            total_pnl=round(total_pnl),
            pf=round(min(pf, 99.9), 2),
            ev=round(ev, 3),
        ))

    results.sort(key=lambda x: x["total_pnl"], reverse=True)
    return results


def _pf_str(pf: float) -> str:
    return "∞" if pf >= 99.9 else f"{pf:.2f}"


def print_results(strategy: str, results: list[dict], top: int) -> None:
    print(f"\n{'='*80}")
    print(f"  {strategy}  上位{min(top, len(results))}件  (全{len(results)}組)")
    print(f"{'='*80}")
    print(f"  {'sm':>5} {'tm':>5} {'R':>5}  {'取引':>5} {'勝率':>7} {'PF':>7} "
          f"{'損益合計':>12}  {'期待値':>7}  備考")
    print("  " + "-" * 72)

    for i, r in enumerate(results[:top]):
        # 現行モードのマーク
        mark = ""
        if r["sm"] == 1.0 and r["tm"] == 1.5:
            mark = " ← aggressive(現行)"
        elif r["sm"] == 1.5 and r["tm"] == 3.0:
            mark = " ← conservative(現行)"

        pf_s = _pf_str(r["pf"])
        print(f"  {r['sm']:>5.1f} {r['tm']:>5.1f} {r['r_ratio']:>5.1f}"
              f"  {r['trades']:>5} {r['win_rate']:>6.1f}%"
              f" {pf_s:>7} {r['total_pnl']:>+12,}  EV={r['ev']:>6.3f}{mark}")

    # ベスト3をハイライト
    print(f"\n  ★ 損益トップ3:")
    for r in results[:3]:
        print(f"    sm={r['sm']} / tm={r['tm']} → "
              f"損益 {r['total_pnl']:+,}円 / 勝率 {r['win_rate']}%% / PF {_pf_str(r['pf'])} / EV={r['ev']}")

    # 期待値トップ
    ev_top = sorted(results, key=lambda x: x["ev"], reverse=True)[:3]
    print(f"\n  ★ 期待値トップ3:")
    for r in ev_top:
        print(f"    sm={r['sm']} / tm={r['tm']} → "
              f"EV={r['ev']} / 損益 {r['total_pnl']:+,}円 / 勝率 {r['win_rate']}%%")


def main() -> None:
    parser = argparse.ArgumentParser(description="sm/tm グリッドサーチ 最適パラメータ探索")
    parser.add_argument("--strategy", type=str, default=None,
                        choices=list(ALL_STRATEGIES.keys()),
                        help="対象戦略 (省略時=全戦略)")
    parser.add_argument("--days",    type=int, default=365)
    parser.add_argument("--top",     type=int, default=15,
                        help="表示上位件数")
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    args = parser.parse_args()

    strategies = [args.strategy] if args.strategy else list(ALL_STRATEGIES.keys())

    print(f"パラメータ最適化 開始")
    print(f"  対象戦略 : {', '.join(strategies)}")
    print(f"  グリッド : sm={SM_RANGE}  tm={TM_RANGE}")
    print(f"  有効組数 : {len(GRID)} 組 (tm >= sm のみ)")
    print(f"  期間     : {args.days}日")

    all_best: list[tuple[str, dict]] = []

    for strat in strategies:
        print(f"\n=== {strat} ({len(WATCHLISTS[strat])}銘柄) ===")
        results = run_grid_search(strat, args.days, args.workers)
        print_results(strat, results, args.top)
        if results:
            all_best.append((strat, results[0]))

    # 全戦略まとめ
    if len(strategies) > 1:
        print(f"\n{'='*80}")
        print("  【全戦略まとめ】損益最大のパラメータ")
        print(f"{'='*80}")
        print(f"  {'戦略':<6} {'sm':>5} {'tm':>5} {'R':>5}  {'勝率':>7} {'PF':>7} {'損益合計':>12}  {'期待値':>7}")
        print("  " + "-" * 60)
        for strat, r in all_best:
            print(f"  {strat:<6} {r['sm']:>5.1f} {r['tm']:>5.1f} {r['r_ratio']:>5.1f}"
                  f"  {r['win_rate']:>6.1f}% {_pf_str(r['pf']):>7}"
                  f" {r['total_pnl']:>+12,}  EV={r['ev']:>6.3f}")


if __name__ == "__main__":
    main()
