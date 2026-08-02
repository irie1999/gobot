"""
donchian_sweep.py  ―  Donchian パラメータスイープ (頻度 vs PF)
==================================================================
DON_PERIOD / エントリー時間 / ギャップ許容 の組合せを総当たりし、
シグナル頻度とPFのバランスを最適化する。
"""

from __future__ import annotations

import argparse
import pickle
from datetime import datetime, time as dtime, timedelta, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from replay_yesterday import (
    WATCH_SYMBOLS, BUDGET, MAX_RISK,
    load_day_data, get_prev_close, simulate_exit, calc_qty,
)
from replay_period import get_all_dates_in_range, calc_stats

JST = timezone(timedelta(hours=9))


# ── スイープ対象パラメータ ────────────────────────────────────
SWEEP_DON_PERIOD = [5, 10, 15, 20, 30]     # 短いほど頻度UP
SWEEP_TARGET_R = [1.0, 1.5, 2.0]            # リスクリワード比
SWEEP_ENTRY_CUTOFF = [dtime(11, 0), dtime(14, 0)]  # エントリー時間制限
SWEEP_GAP_MAX = [2.0, 3.0, 5.0]             # ギャップ許容%


def run_don_custom(day_df, prev_close, don_period, target_r,
                   entry_cutoff, gap_max):
    """カスタムパラメータで Donchian 実行。"""
    opens = day_df["open"].to_numpy(dtype=float)
    highs = day_df["high"].to_numpy(dtype=float)
    lows = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times = day_df.index
    n = len(day_df)

    if n < don_period + 2:
        return None

    if prev_close and prev_close > 0:
        if abs(opens[0] - prev_close) / prev_close * 100 > gap_max:
            return None

    for i in range(don_period, n):
        t = times[i].time()
        if t >= entry_cutoff:
            return None
        don_hi = highs[i - don_period:i].max()
        don_lo = lows[i - don_period:i].min()
        if closes[i] > don_hi and closes[i] > opens[i]:
            if i + 1 >= n:
                return None
            entry_p = opens[i + 1]
            stop_p = don_lo
            if entry_p <= stop_p:
                continue
            target_p = entry_p + (entry_p - stop_p) * target_r
            qty = calc_qty(entry_p, stop_p)
            return simulate_exit(day_df, i + 1, entry_p, times[i + 1],
                                 stop_p, target_p, qty, "Donchian")
    return None


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    parser = argparse.ArgumentParser(description="Donchianパラメータスイープ")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to", dest="to_date", default=None)
    args = parser.parse_args()

    # 期間決定
    if args.from_date and args.to_date:
        from_date, to_date = args.from_date, args.to_date
    else:
        from replay_yesterday import _find_latest_date
        to_date = args.to_date or _find_latest_date()
        from_dt = datetime.strptime(to_date, "%Y-%m-%d") - timedelta(days=args.days)
        from_date = from_dt.strftime("%Y-%m-%d")

    dates = get_all_dates_in_range(from_date, to_date)
    print(f"期間: {from_date} 〜 {to_date} ({len(dates)}営業日)")
    print(f"監視: {len(WATCH_SYMBOLS)}銘柄")

    # 全日全銘柄のデータを先にロード (高速化)
    print("データ読み込み中...", flush=True)
    data_cache = {}  # (code5, date) -> (df, prev_close)
    for code4, _ in WATCH_SYMBOLS:
        code5 = code4 + "0"
        for date in dates:
            df = load_day_data(code5, date)
            if df is not None and not df.empty:
                prev = get_prev_close(code5, date)
                data_cache[(code5, date)] = (df, prev)
    print(f"  キャッシュ: {len(data_cache)}件")

    # スイープ実行
    combos = list(product(SWEEP_DON_PERIOD, SWEEP_TARGET_R,
                          SWEEP_ENTRY_CUTOFF, SWEEP_GAP_MAX))
    print(f"\nスイープ: {len(combos)} 組合せ")

    results = []
    for ci, (don_period, target_r, entry_cutoff, gap_max) in enumerate(combos, 1):
        all_trades = []
        first_trades = []

        for date in dates:
            day_trades = []
            for code4, name in WATCH_SYMBOLS:
                code5 = code4 + "0"
                if (code5, date) not in data_cache:
                    continue
                df, prev = data_cache[(code5, date)]
                t = run_don_custom(df, prev, don_period, target_r,
                                   entry_cutoff, gap_max)
                if t:
                    t["date"] = date
                    t["symbol"] = code4
                    day_trades.append(t)

            all_trades.extend(day_trades)
            if day_trades:
                day_trades.sort(key=lambda x: str(x["entry_dt"]))
                first_trades.append(day_trades[0])

        all_s = calc_stats(all_trades)
        first_s = calc_stats(first_trades)

        results.append({
            "don_period": don_period,
            "target_r": target_r,
            "cutoff": entry_cutoff.strftime("%H:%M"),
            "gap_max": gap_max,
            # 全採用
            "all_n": all_s["n"],
            "all_wr": all_s["win_rate"],
            "all_pf": all_s["pf"],
            "all_pnl": all_s["total_pnl"],
            "all_dd": all_s["max_dd"],
            # 1日1銘柄
            "f_n": first_s["n"],
            "f_wr": first_s["win_rate"],
            "f_pf": first_s["pf"],
            "f_pnl": first_s["total_pnl"],
            "f_dd": first_s["max_dd"],
        })

        if ci % 10 == 0 or ci == len(combos):
            print(f"  {ci}/{len(combos)} 完了", flush=True)

    # PF順ソート
    results.sort(key=lambda x: (x["f_pf"] if x["f_pf"] < 1000 else 0,
                                 x["f_pnl"]), reverse=True)

    # 表示
    print()
    print("=" * 105)
    print(f"  Donchian パラメータスイープ ({len(dates)}営業日 / {len(WATCH_SYMBOLS)}銘柄)")
    print("=" * 105)
    print(f"  {'DON':>4} {'R':>5} {'締切':>6} {'Gap':>5} | "
          f"{'全N':>4} {'全WR':>5} {'全PF':>6} {'全損益':>10} | "
          f"{'1N':>4} {'1WR':>5} {'1PF':>6} {'1損益':>10} {'1DD':>7}")
    print("-" * 105)
    for r in results[:30]:
        print(f"  {r['don_period']:>4} {r['target_r']:>4.1f} {r['cutoff']:>6} "
              f"{r['gap_max']:>4.1f}% | "
              f"{r['all_n']:>4} {r['all_wr']:>4.0f}% {_pf(r['all_pf']):>6} "
              f"{r['all_pnl']:>+10,.0f} | "
              f"{r['f_n']:>4} {r['f_wr']:>4.0f}% {_pf(r['f_pf']):>6} "
              f"{r['f_pnl']:>+10,.0f} {r['f_dd']:>+6.1f}%")

    # 頻度重視ベスト
    print()
    print("=" * 105)
    print("  頻度ベスト5 (1日1銘柄取引数順, PF≥1.5条件)")
    print("=" * 105)
    freq_best = sorted(
        [r for r in results if r["f_pf"] >= 1.5 and r["f_pf"] < 1000],
        key=lambda x: x["f_n"], reverse=True
    )[:5]
    for r in freq_best:
        print(f"  DON={r['don_period']} R={r['target_r']} "
              f"締切{r['cutoff']} Gap{r['gap_max']}% → "
              f"{r['f_n']}取引 PF{_pf(r['f_pf'])} {r['f_pnl']:+,.0f}円 "
              f"DD{r['f_dd']:+.1f}%")

    # CSV保存
    df = pd.DataFrame(results)
    csv_path = Path("donchian_sweep_result.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\nCSV: {csv_path.resolve()}")


if __name__ == "__main__":
    main()
