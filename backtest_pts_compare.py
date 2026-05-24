"""
backtest_pts_compare.py ― TSE逆指値 vs PTS戦略 比較バックテスト

【2戦略の違い】
TSE戦略 (現行):
  ・始値 > order_p × (1 + LIMIT_ENTRY_MARGIN_PCT) → 不約定（ギャップ過大）
  ・始値 ∈ [order_p, 上限] → 始値で約定
  ・場中トリガー → order_p × 1.005

PTS戦略 (新案/楽観的近似):
  ・始値 >= order_p → order_p で約定（PTS夜間でギャップなし購入）
  ・場中トリガー → order_p × 1.005（TSEと同じ）
  ・ギャップ上限なし（PTS約定後の深夜悪材料リスクは過小評価）

Usage:
  python backtest_pts_compare.py
  python backtest_pts_compare.py --days 180
  python backtest_pts_compare.py --aggressive
"""
from __future__ import annotations
import argparse
import sys
import os

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

import numpy as np
import pandas as pd
from datetime import timedelta, timezone, datetime

from backtest_limit_entry import (
    fetch, ENTRY_EXPIRE, MAX_HOLD, FIXED_QTY,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, LIMIT_ENTRY_MARGIN_PCT,
    MIN_PRICE, MAX_PRICE, MAX_ATR_RATIO,
)
from check_signals_stop     import WATCHLIST as STOP_WL, STRATEGY_PARAMS as STOP_PARAMS
from check_signals_breakout import WATCHLIST as BRK_WL,  STRATEGY_PARAMS as BRK_PARAMS

JST    = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()


def _run(df: pd.DataFrame, calc_fn, em: float, sm: float, tm: float,
         mode: str, backtest_days: int) -> list[dict]:
    """
    mode: "tse" or "pts"
    """
    try:
        df = calc_fn(df)
    except Exception:
        return []

    cutoff = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df = df[df.index >= cutoff].copy()
    if len(df) < 5:
        return []

    trades: list[dict] = []
    state        = "idle"
    limit_price  = stop_price = target_price = 0.0
    expire_idx   = signal_idx = hold_start = days_to_fill = 0
    signal_dt    = None
    entry_p      = entry_dt = None
    qty          = 0

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        dt   = df.index[i]

        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        atr_prev = float(prev.get("atr", np.nan))
        if pd.isna(atr_prev) or atr_prev <= 0:
            continue

        # ── IDLE → PENDING ──────────────────────────────────────────
        if state == "idle":
            if not bool(prev.get("entry_sig", False)):
                continue
            close_prev = float(prev["close"])
            if close_prev < MIN_PRICE or close_prev > MAX_PRICE:
                continue
            if atr_prev / close_prev > MAX_ATR_RATIO:
                continue

            lp = close_prev + atr_prev * em
            sp = lp - atr_prev * sm
            tp = lp + atr_prev * tm
            if lp <= 0 or sp <= 0 or tp <= lp:
                continue

            limit_price, stop_price, target_price = lp, sp, tp
            expire_idx   = i + ENTRY_EXPIRE
            signal_idx   = i
            days_to_fill = 0
            signal_dt    = df.index[i - 1]
            state        = "pending"
            # fall through

        # ── PENDING → IN_POS ────────────────────────────────────────
        if state == "pending":
            if i > expire_idx:
                state = "idle"

            elif hi >= limit_price:
                # 約定価格を決定
                if op >= limit_price:
                    # 寄り付きギャップアップ
                    if mode == "tse":
                        if op > limit_price * (1.0 + LIMIT_ENTRY_MARGIN_PCT):
                            # 指値上限超え → 不約定
                            state = "idle"
                            continue
                        entry_p = op                          # TSE: 始値で約定
                    else:
                        entry_p = limit_price                 # PTS: order_p で約定
                else:
                    # 場中トリガー（両モード共通）
                    entry_p = limit_price * (1.0 + SLIPPAGE_STOP_PCT)

                entry_dt     = dt
                hold_start   = i
                days_to_fill = i - signal_idx
                qty          = FIXED_QTY
                state        = "in_pos"

                # 同日決済チェック
                hit_tgt = hi >= target_price
                hit_stp = lo <= stop_price
                if hit_tgt:
                    exit_p, exit_reason = target_price, "目標達成"
                elif hit_stp:
                    exit_reason = "損切り"
                    exit_p = op if op <= stop_price else stop_price * (1.0 - SLIPPAGE_STOP_PCT)
                else:
                    exit_p = exit_reason = None

                if exit_p is not None:
                    if entry_p * 0.1 <= exit_p <= entry_p * 10.0:
                        fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
                        trades.append(dict(
                            entry_p=entry_p, exit_p=exit_p,
                            pnl=(exit_p - entry_p) * qty - fee,
                            hold_days=0, reason=exit_reason,
                            gap_up=(op >= limit_price),
                        ))
                    state = "idle"
                continue

        # ── IN_POS: OCO決済 ──────────────────────────────────────────
        if state == "in_pos":
            hold_days = i - hold_start
            hit_tgt = hi >= target_price
            hit_stp = lo <= stop_price

            if hit_tgt:
                exit_p, exit_reason = target_price, "目標達成"
            elif hit_stp:
                exit_reason = "損切り"
                exit_p = op if op <= stop_price else stop_price * (1.0 - SLIPPAGE_STOP_PCT)
            elif hold_days >= MAX_HOLD:
                exit_p, exit_reason = cl, "タイムカット"
            else:
                exit_p = exit_reason = None

            if exit_p is not None:
                if entry_p * 0.1 <= exit_p <= entry_p * 10.0:
                    fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
                    trades.append(dict(
                        entry_p=entry_p, exit_p=exit_p,
                        pnl=(exit_p - entry_p) * qty - fee,
                        hold_days=hold_days, reason=exit_reason,
                        gap_up=(op >= limit_price),
                    ))
                state = "idle"
            continue

    return trades


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, wins=0, wr=0.0, pnl=0.0, pf=0.0, avg_entry=0.0)
    n    = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    pnl  = sum(t["pnl"] for t in trades)
    gp   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    avg  = sum(t["entry_p"] for t in trades) / n
    return dict(n=n, wins=wins, wr=wins / n * 100, pnl=pnl, pf=pf, avg_entry=avg)


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def main():
    parser = argparse.ArgumentParser(description="TSE逆指値 vs PTS比較バックテスト")
    parser.add_argument("--days",       type=int,  default=365)
    parser.add_argument("--aggressive", action="store_true")
    args = parser.parse_args()

    mode = "aggressive" if args.aggressive else "conservative"
    print(f"比較バックテスト: {args.days}日 / モード: {mode}")
    print(f"TSE上限マージン: +{LIMIT_ENTRY_MARGIN_PCT*100:.0f}%")
    print()

    all_wl = ([(s, n, st, STOP_PARAMS) for s, n, st in STOP_WL] +
              [(s, n, st, BRK_PARAMS)  for s, n, st in BRK_WL])

    tse_all: list[dict] = []
    pts_all: list[dict] = []
    strat_tse: dict[str, list] = {}
    strat_pts: dict[str, list] = {}

    print(f"分析中... ({len(all_wl)}銘柄)", flush=True)

    for sym, name, strat, params in all_wl:
        calc_fn, em, sm, tm = params[strat]
        df = fetch(sym, args.days + 60)
        if df is None or len(df) < 10:
            continue

        tse_trades = _run(df.copy(), calc_fn, em, sm, tm, "tse", args.days)
        pts_trades = _run(df.copy(), calc_fn, em, sm, tm, "pts", args.days)

        tse_all.extend(tse_trades)
        pts_all.extend(pts_trades)
        strat_tse.setdefault(strat, []).extend(tse_trades)
        strat_pts.setdefault(strat, []).extend(pts_trades)

    ts = _stats(tse_all)
    ps = _stats(pts_all)

    W = 68
    print("=" * W)
    print(f"=== TSE逆指値(+{LIMIT_ENTRY_MARGIN_PCT*100:.0f}%) vs PTS戦略 比較結果 ({args.days}日) ===")
    print("=" * W)
    print(f"{'':22} {'TSE逆指値':>12} {'PTS':>12} {'差(PTS-TSE)':>14}")
    print("-" * W)
    print(f"{'約定数':22} {ts['n']:>12,} {ps['n']:>12,} {ps['n']-ts['n']:>+14,}")
    print(f"{'勝率':22} {ts['wr']:>11.1f}% {ps['wr']:>11.1f}% {ps['wr']-ts['wr']:>+13.1f}%")
    print(f"{'PF':22} {_pf_str(ts['pf']):>12} {_pf_str(ps['pf']):>12}")
    print(f"{'総損益':22} {ts['pnl']:>11,.0f}円 {ps['pnl']:>11,.0f}円 {ps['pnl']-ts['pnl']:>+13,.0f}円")
    print(f"{'平均約定価格':22} {ts['avg_entry']:>11,.1f}円 {ps['avg_entry']:>11,.1f}円 {ps['avg_entry']-ts['avg_entry']:>+13,.1f}円")

    # PTS追加約定分析（TSEが不約定でPTSが約定した取引）
    pts_extra = [t for t in pts_all if t.get("gap_up")]
    tse_gapup = [t for t in tse_all  if t.get("gap_up")]
    # PTS追加 = PTS全gap_up - TSE gap_up (近似)
    pts_extra_only_n = ps["n"] - ts["n"]

    print()
    print("【PTSが追加約定する分の内訳（ギャップアップ時）】")
    print(f"  TSEのギャップアップ約定 : {len(tse_gapup)}件")
    print(f"  PTSのギャップアップ約定 : {len(pts_extra)}件")
    print(f"  PTS追加約定（TSEがミス）: {pts_extra_only_n:+d}件")

    # PTS gap-up約定の損益
    if pts_extra:
        gup_s = _stats(pts_extra)
        print(f"  PTS gap-up約定の勝率    : {gup_s['wr']:.1f}%  損益: {gup_s['pnl']:+,.0f}円")

    print()
    print("【戦略別比較】")
    header = f"{'戦略':6} {'TSE件':>7} {'PTS件':>7} {'TSE損益':>11} {'PTS損益':>11} {'差':>11} {'TSE勝率':>8} {'PTS勝率':>8}"
    print(header)
    print("-" * len(header))
    for strat in sorted(strat_tse.keys()):
        ts_s = _stats(strat_tse.get(strat, []))
        ps_s = _stats(strat_pts.get(strat, []))
        diff = ps_s["pnl"] - ts_s["pnl"]
        print(f"{strat:6} {ts_s['n']:>7,} {ps_s['n']:>7,} "
              f"{ts_s['pnl']:>11,.0f} {ps_s['pnl']:>11,.0f} "
              f"{diff:>+11,.0f} {ts_s['wr']:>7.1f}% {ps_s['wr']:>7.1f}%")

    print()
    print("【判定】")
    pnl_diff = ps["pnl"] - ts["pnl"]
    wr_diff  = ps["wr"]  - ts["wr"]
    if pnl_diff > 0 and wr_diff >= 0:
        print(f"  ✅ PTS戦略が優位  損益+{pnl_diff:,.0f}円 / 勝率{wr_diff:+.1f}%")
        print(f"     → ギャップアップ時に安く買える効果がプラスに働いています")
    elif pnl_diff > 0 and wr_diff < 0:
        print(f"  △  PTS戦略が損益では優位 (+{pnl_diff:,.0f}円) だが勝率は低下 ({wr_diff:+.1f}%)")
        print(f"     → 追加約定の大きな利益が平均を押し上げているが、ばらつきが大きい")
    else:
        print(f"  ❌ TSE戦略が優位  PTS比 損益{pnl_diff:+,.0f}円 / 勝率{wr_diff:+.1f}%")
        print(f"     → ギャップアップ大の日に買うと逆転するケースが多い")

    print()
    print("【注意】PTS戦略は楽観的近似です")
    print("  ・「PTS約定後に深夜悪材料→TSE始値が大幅安」のリスクを過小評価")
    print("  ・実際のPTS成績はこの数値より悪化する可能性があります")
    print("  ・正確な検証はフォワードテストでのみ可能です")


if __name__ == "__main__":
    main()
