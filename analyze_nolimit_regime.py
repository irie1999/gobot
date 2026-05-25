"""
analyze_nolimit_regime.py  ―  株価制限なし × 日経平均 相関分析

株価制限なしバックテストの月次損益と、その期間の日経平均騰落率を照合して
「どの程度の日経上昇なら使用してよいか」の閾値を導き出す。

Usage:
    python analyze_nolimit_regime.py
    python analyze_nolimit_regime.py --days 730
"""
from __future__ import annotations
import argparse
import os
import sys

os.environ["TRADING_MODE"] = "aggressive"

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import timedelta, timezone, datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))

import check_signals_stop     as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import run_limit_backtest, fetch, WORKERS

# sm=1.5 / tm=2.0 (株価制限なしと同じパラメータ)
for _k, _v in list(_stop.STRATEGY_PARAMS.items()):
    _stop.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 1.5, 2.0)
for _k, _v in list(_brk.STRATEGY_PARAMS.items()):
    _brk.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 1.5, 2.0)

import run_signals_nolimit as _nolimit
STOP_WL = list(_nolimit.STOP_WATCHLIST)
BRK_WL  = list(_nolimit.BRK_WATCHLIST)

JST    = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()


def fetch_n225(days: int) -> pd.Series:
    """日経平均の日足終値を返す (DatetimeIndex)"""
    df = yf.Ticker("^N225").history(period=f"{days + 10}d")
    if df.empty:
        return pd.Series(dtype=float)
    s = df["Close"]
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s.sort_index()


def collect_all_trades(backtest_days: int) -> list[dict]:
    """全銘柄のtrade_logを集める"""
    all_trades: list[dict] = []
    all_wl = [(sym, name, strat, _stop.STRATEGY_PARAMS) for sym, name, strat in STOP_WL] + \
             [(sym, name, strat, _brk.STRATEGY_PARAMS)  for sym, name, strat in BRK_WL]

    def _one(sym, name, strat, params):
        calc_fn, em, sm, tm = params[strat]
        df = fetch(sym, backtest_days + 60)
        if df is None or len(df) < 10:
            return []
        try:
            result = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm,
                                        backtest_days=backtest_days,
                                        strategy_name=strat,
                                        entry_type="stop")
        except Exception:
            return []
        trades = result.get("trade_log", [])
        for t in trades:
            t["sym"]   = sym
            t["strat"] = strat
        return trades

    print(f"バックテスト実行中... ({len(all_wl)}銘柄)", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_one, sym, name, strat, params): None
                for sym, name, strat, params in all_wl}
        for fut in as_completed(futs):
            all_trades.extend(fut.result())

    return all_trades


def make_monthly_pnl(trades: list[dict]) -> pd.DataFrame:
    """exit_dt を月でグルーピングして月次損益を集計"""
    rows = []
    for t in trades:
        exit_dt = t.get("exit_dt")
        if exit_dt is None:
            continue
        exit_date = pd.to_datetime(exit_dt).date()
        rows.append({"month": exit_date.replace(day=1), "pnl": t["pnl"]})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.groupby("month")["pnl"].agg(["sum", "count", lambda x: (x > 0).sum()]).rename(
        columns={"sum": "pnl", "count": "trades", "<lambda_0>": "wins"})


def make_weekly_pnl(trades: list[dict]) -> pd.DataFrame:
    """週次損益を集計 (月曜始まり)"""
    rows = []
    for t in trades:
        exit_dt = t.get("exit_dt")
        if exit_dt is None:
            continue
        exit_date = pd.to_datetime(exit_dt).date()
        week_start = exit_date - timedelta(days=exit_date.weekday())
        rows.append({"week": week_start, "pnl": t["pnl"]})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.groupby("week")["pnl"].agg(["sum", "count", lambda x: (x > 0).sum()]).rename(
        columns={"sum": "pnl", "count": "trades", "<lambda_0>": "wins"})


def n225_return_for_period(n225: pd.Series, start: date, end: date) -> float:
    """期間中の日経平均騰落率(%)"""
    sub = n225[n225.index >= pd.Timestamp(start)]
    sub = sub[sub.index <= pd.Timestamp(end)]
    if len(sub) < 2:
        return 0.0
    return (sub.iloc[-1] / sub.iloc[0] - 1) * 100


def n225_prior_return(n225: pd.Series, ref_date: date, lookback_days: int) -> float:
    """ref_date の lookback_days 前からの日経騰落率(%)"""
    ref_ts  = pd.Timestamp(ref_date)
    past_ts = ref_ts - timedelta(days=lookback_days)
    sub = n225[n225.index <= ref_ts]
    if sub.empty:
        return 0.0
    end_val = sub.iloc[-1]
    sub2 = n225[n225.index >= past_ts]
    sub2 = sub2[sub2.index <= ref_ts]
    if sub2.empty:
        return 0.0
    return (end_val / sub2.iloc[0] - 1) * 100


def main():
    parser = argparse.ArgumentParser(description="株価制限なし × 日経平均 相関分析")
    parser.add_argument("--days", type=int, default=730)
    args = parser.parse_args()

    print(f"分析期間: {args.days}日")
    print()

    trades = collect_all_trades(args.days)
    if not trades:
        print("取引データなし")
        return

    print(f"総取引数: {len(trades)}件")
    print()

    n225 = fetch_n225(args.days + 30)
    if n225.empty:
        print("日経データ取得失敗")
        return

    # ── 月次分析 ──────────────────────────────────────────────────────────────
    monthly = make_monthly_pnl(trades)
    if monthly.empty:
        print("月次データなし")
        return

    rows = []
    for month, row in monthly.iterrows():
        month_end = (pd.Timestamp(month) + pd.offsets.MonthEnd(0)).date()
        # その月の日経騰落
        n225_m = n225_return_for_period(n225, month, month_end)
        # エントリー前1ヶ月の日経騰落（先行指標）
        n225_prior20 = n225_prior_return(n225, month, 20)
        n225_prior5  = n225_prior_return(n225, month, 5)
        rows.append({
            "month":       month,
            "pnl":         row["pnl"],
            "trades":      int(row["trades"]),
            "wr":          row["wins"] / row["trades"] * 100,
            "n225_month":  n225_m,
            "n225_prior20": n225_prior20,
            "n225_prior5":  n225_prior5,
        })

    df_m = pd.DataFrame(rows).sort_values("month")

    print("=" * 80)
    print("【月次 損益 × 日経平均騰落率】")
    print("=" * 80)
    print(f"{'月':10} {'損益':>12} {'取引':>5} {'勝率':>6} {'当月日経':>9} {'前20日日経':>10} {'前5日日経':>9}")
    print("-" * 80)
    for _, r in df_m.iterrows():
        sign = "★" if r["pnl"] > 100_000 else ("▼" if r["pnl"] < -50_000 else " ")
        print(f"{str(r['month'])[:7]:10} {r['pnl']:>+12,.0f}円 {int(r['trades']):>5} "
              f"{r['wr']:>5.1f}% {r['n225_month']:>+8.1f}% {r['n225_prior20']:>+9.1f}% "
              f"{r['n225_prior5']:>+8.1f}%  {sign}")

    # ── 日経騰落率 × 損益の相関分析 ──────────────────────────────────────────
    print()
    print("=" * 70)
    print("【日経5日騰落率 × 月次損益 グループ分け】")
    print("=" * 70)

    bins_prior5 = [(-99, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 99)]
    print(f"{'日経5日騰落':15} {'月数':>5} {'合計損益':>14} {'平均損益':>12} {'勝ち月率':>8}")
    print("-" * 60)
    for lo, hi in bins_prior5:
        sub = df_m[(df_m["n225_prior5"] >= lo) & (df_m["n225_prior5"] < hi)]
        if sub.empty:
            continue
        total = sub["pnl"].sum()
        avg   = sub["pnl"].mean()
        pos_rate = (sub["pnl"] > 0).mean() * 100
        label = f"{lo:+d}%〜{hi:+d}%"
        marker = " ✅" if avg > 50_000 else (" ❌" if avg < -50_000 else "")
        print(f"{label:15} {len(sub):>5} {total:>+13,.0f}円 {avg:>+11,.0f}円 {pos_rate:>7.0f}%{marker}")

    print()
    print("【日経20日騰落率 × 月次損益 グループ分け】")
    print("-" * 60)
    bins_prior20 = [(-99, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 99)]
    print(f"{'日経20日騰落':16} {'月数':>5} {'合計損益':>14} {'平均損益':>12} {'勝ち月率':>8}")
    print("-" * 60)
    for lo, hi in bins_prior20:
        sub = df_m[(df_m["n225_prior20"] >= lo) & (df_m["n225_prior20"] < hi)]
        if sub.empty:
            continue
        total = sub["pnl"].sum()
        avg   = sub["pnl"].mean()
        pos_rate = (sub["pnl"] > 0).mean() * 100
        label = f"{lo:+d}%〜{hi:+d}%"
        marker = " ✅" if avg > 50_000 else (" ❌" if avg < -50_000 else "")
        print(f"{label:16} {len(sub):>5} {total:>+13,.0f}円 {avg:>+11,.0f}円 {pos_rate:>7.0f}%{marker}")

    # ── MA200 位置との関係 ──────────────────────────────────────────────────
    print()
    print("【日経 vs MA200 × 月次損益】")
    print("-" * 60)
    n225_df = n225.to_frame("close")
    n225_df["ma200"] = n225_df["close"].rolling(200).mean()
    above_rows = []
    below_rows = []
    for _, r in df_m.iterrows():
        ts = pd.Timestamp(r["month"])
        row_n = n225_df[n225_df.index <= ts]
        if row_n.empty or pd.isna(row_n.iloc[-1]["ma200"]):
            continue
        if row_n.iloc[-1]["close"] >= row_n.iloc[-1]["ma200"]:
            above_rows.append(r["pnl"])
        else:
            below_rows.append(r["pnl"])

    def _summary(label, vals):
        if not vals:
            print(f"  {label}: データなし")
            return
        total = sum(vals)
        avg   = total / len(vals)
        pos   = sum(1 for v in vals if v > 0) / len(vals) * 100
        print(f"  {label}: {len(vals)}ヶ月  合計{total:>+12,.0f}円  平均{avg:>+9,.0f}円  勝ち月率{pos:.0f}%")

    _summary("日経 > MA200 (上昇トレンド)", above_rows)
    _summary("日経 < MA200 (下降トレンド)", below_rows)

    # ── 相関係数 ──────────────────────────────────────────────────────────────
    print()
    corr5  = df_m["pnl"].corr(df_m["n225_prior5"])
    corr20 = df_m["pnl"].corr(df_m["n225_prior20"])
    corrm  = df_m["pnl"].corr(df_m["n225_month"])
    print(f"相関係数: 損益 vs 日経5日騰落  = {corr5:+.3f}")
    print(f"相関係数: 損益 vs 日経20日騰落 = {corr20:+.3f}")
    print(f"相関係数: 損益 vs 当月日経騰落 = {corrm:+.3f}")

    # ── 判定ルール提案 ────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("【推奨判定ルール（データから導出）】")
    print("=" * 70)

    # 5日騰落の閾値を探す
    thresholds = [-5, -3, -2, -1, 0, 1, 2, 3, 5]
    best_thr = 0
    best_pos = 0.0
    for thr in thresholds:
        sub = df_m[df_m["n225_prior5"] >= thr]
        if len(sub) >= 3:
            pos = (sub["pnl"] > 0).mean()
            if pos > best_pos:
                best_pos = pos
                best_thr = thr

    use_sub    = df_m[df_m["n225_prior5"] >= best_thr]
    avoid_sub  = df_m[df_m["n225_prior5"] < best_thr]
    use_avg    = use_sub["pnl"].mean() if len(use_sub) > 0 else 0
    avoid_avg  = avoid_sub["pnl"].mean() if len(avoid_sub) > 0 else 0

    print(f"  日経5日騰落 >= {best_thr:+d}% の月: {len(use_sub)}ヶ月  "
          f"平均{use_avg:>+9,.0f}円  勝ち月率{best_pos*100:.0f}%  → 使用推奨 ✅")
    if len(avoid_sub) > 0:
        avoid_pos = (avoid_sub["pnl"] > 0).mean() * 100
        print(f"  日経5日騰落  < {best_thr:+d}% の月: {len(avoid_sub)}ヶ月  "
              f"平均{avoid_avg:>+9,.0f}円  勝ち月率{avoid_pos:.0f}%  → 使用注意 ⚠️")

    print()
    today_n225_5  = n225_prior_return(n225, _TODAY, 5)
    today_n225_20 = n225_prior_return(n225, _TODAY, 20)
    n225_latest   = n225.iloc[-1] if not n225.empty else 0
    n225_ma200    = n225.rolling(200).mean().iloc[-1] if len(n225) >= 200 else float("nan")
    above_ma200   = n225_latest >= n225_ma200 if not pd.isna(n225_ma200) else True

    print(f"  本日の日経5日騰落  : {today_n225_5:+.2f}%")
    print(f"  本日の日経20日騰落 : {today_n225_20:+.2f}%")
    print(f"  日経 vs MA200      : {'上 ✅' if above_ma200 else '下 ❌'} ({n225_latest:,.0f} vs MA200 {n225_ma200:,.0f})")
    print()
    use_today = (today_n225_5 >= best_thr) and above_ma200
    if use_today:
        print(f"  → 本日の判定: 株価制限なし 使用推奨 ✅")
    else:
        print(f"  → 本日の判定: 使用注意 ⚠️（条件未達）")


if __name__ == "__main__":
    main()
