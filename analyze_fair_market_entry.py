"""
analyze_fair_market_entry.py ― 「翌日(T+1)成行買い」フェア比較 (後知恵なし・スイング)
================================================================================
⑭の②(無条件成行)は『Aが約定した取引だけ』を対象にしていたため後知恵バイアスが
あった(=結局ブレイクした銘柄だけを安く買っていた)。本スクリプトは全シグナル
(約定=ブレイク成功 + 失効=不発 の両方)を自前で列挙し、後知恵なしで比較する。

比較(同じシグナル母集団・同じ保有/決済ロジック):
  A = 逆指値ブレイク(現行): T+1に高値>=トリガで約定。届かなければ取引なし(失効)。
  F = T+1成行(全シグナル): 失効するものも含め、T+1の指定時刻に成行で必ず買う。

ENTRY_EXPIRE=1(逆指値は翌日のみ有効)なので A も F も T+1 が舞台。決済は close方式
損切り(§16)・MAX_HOLD・目標/損切りは同一。5分足は T+1 の各時刻の株価取得のみに使用。

使い方:
  python analyze_fair_market_entry.py                    # WATCHLIST・全期間
  python analyze_fair_market_entry.py --days 60          # 直近60日のシグナル
  python analyze_fair_market_entry.py --all-minute --workers 8   # 5分足がある全銘柄
  python analyze_fair_market_entry.py --short            # ショート
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
os.environ.setdefault("TRADING_MODE", "conservative")

import numpy as np
import pandas as pd

from backtest_limit_entry import (
    fetch, tick_size, default_stop_mode, default_max_hold,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, FIXED_QTY,
    MIN_PRICE, MAX_PRICE, MAX_ATR_RATIO,
)
from analyze_open_confirm_entry import _price_at, universe_from_pkl

JST = timezone(timedelta(hours=9))
_TIMES = [0, 15, 30, 45, 60, 90, 120, 150, 180, 240]


def _round_tick(p, up=True):
    t = tick_size(float(p))
    return int(math.ceil(p / t)) * t if up else int(p // t) * t


def _pnl(entry_p, exit_p, qty, is_short):
    fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
    return (entry_p - exit_p) * qty - fee if is_short else (exit_p - entry_p) * qty - fee


def _sim_hold(H, L, C, O, entry_idx, entry_p, sp, tp, is_short, max_hold, stop_mode):
    """entry_idx(=T+1)で買った後の決済。engine準拠(目標優先・close損切り・タイムカット)。"""
    n = len(C)
    last = min(entry_idx + max_hold, n - 1)
    for j in range(entry_idx, last + 1):
        hi, lo, cl, op = H[j], L[j], C[j], O[j]
        hold_days = j - entry_idx
        hit_tgt = (hi >= tp) if not is_short else (lo <= tp)
        if stop_mode == "close":
            hit_stp = (cl <= sp) if not is_short else (cl >= sp)
        else:
            hit_stp = (lo <= sp) if not is_short else (hi >= sp)
        if hit_tgt:
            return tp, "目標達成"
        if hit_stp:
            if stop_mode == "close":
                xp = cl * (1 - SLIPPAGE_STOP_PCT) if not is_short else cl * (1 + SLIPPAGE_STOP_PCT)
            elif not is_short:
                xp = op if op <= sp else sp * (1 - SLIPPAGE_STOP_PCT)
            else:
                xp = op if op >= sp else sp * (1 + SLIPPAGE_STOP_PCT)
            return xp, "損切り"
        if hold_days >= max_hold:
            return cl, "タイムカット"
    return C[last], "打切"


def _one(mod, sym, name, strat, cutoff, times, minute_dir, is_short):
    """1(銘柄,戦略)の全シグナルを列挙し、A(逆指値)とF(各時刻成行)のpnlを返す。
    返り値: list[dict(rec_score, a_pnl(or None), f_by={cm:pnl}, strat)]"""
    params = mod.STRATEGY_PARAMS.get(strat)
    if not params:
        return []
    calc_fn, em, sm, tm = params
    df = fetch(sym, 400)
    if df is None or df.empty:
        return []
    try:
        bt = mod.backtest_one(sym, name, strat)
        rec, _ = mod.calc_recommend_score(bt.get("period_results", {})) if bt else (0, "")
    except Exception:
        rec = 0
    try:
        dfc = calc_fn(df.copy())
    except Exception:
        return []
    if "entry_sig" not in dfc or "atr" not in dfc:
        return []
    idx = dfc.index
    H = dfc["high"].values; L = dfc["low"].values
    C = dfc["close"].values; O = dfc["open"].values
    A = dfc["atr"].values; S = dfc["entry_sig"].values
    stop_mode = default_stop_mode(strat, is_short)
    max_hold = default_max_hold(strat)
    n = len(dfc)
    out = []
    for i in range(n - 1):
        if not bool(S[i]):
            continue
        atr_i, cl_i = A[i], C[i]
        if not np.isfinite(atr_i) or atr_i <= 0 or cl_i <= 0:
            continue
        if not (MIN_PRICE <= cl_i <= MAX_PRICE) or atr_i / cl_i > MAX_ATR_RATIO:
            continue
        sdate = idx[i].date() if hasattr(idx[i], "date") else idx[i]
        if cutoff and sdate < cutoff:
            continue
        if not is_short:
            order_p = cl_i + atr_i * em
            sp = order_p - atr_i * sm
            tp = order_p + atr_i * tm
            if not (order_p > 0 and sp > 0 and tp > order_p):
                continue
        else:
            order_p = cl_i - atr_i * em
            sp = order_p + atr_i * sm
            tp = order_p - atr_i * tm
            if not (order_p > 0 and tp > 0 and tp < order_p):
                continue
        ei = i + 1                       # T+1
        tp1 = idx[ei]
        # ── A: 逆指値(T+1のみ) ──
        a_pnl = None
        if not is_short:
            filled = H[ei] >= order_p
            if filled:
                ea = O[ei] if O[ei] >= order_p else round(order_p * (1 + SLIPPAGE_STOP_PCT))
                xp, _r = _sim_hold(H, L, C, O, ei, ea, sp, tp, is_short, max_hold, stop_mode)
                a_pnl = _pnl(ea, xp, FIXED_QTY, is_short)
        else:
            filled = L[ei] <= order_p
            if filled:
                ea = O[ei] if O[ei] <= order_p else round(order_p * (1 - SLIPPAGE_STOP_PCT))
                xp, _r = _sim_hold(H, L, C, O, ei, ea, sp, tp, is_short, max_hold, stop_mode)
                a_pnl = _pnl(ea, xp, FIXED_QTY, is_short)
        # ── F: T+1 各時刻に成行(全シグナル) ──
        f_by = {}
        for cm in times:
            p = _price_at(sym, tp1, cm, minute_dir)
            if p is None or not (0.6 * order_p <= p <= 1.6 * order_p):
                continue
            ef = _round_tick(p * (1 + SLIPPAGE_STOP_PCT), up=True) if not is_short \
                else _round_tick(p * (1 - SLIPPAGE_STOP_PCT), up=False)
            xp, _r = _sim_hold(H, L, C, O, ei, ef, sp, tp, is_short, max_hold, stop_mode)
            f_by[cm] = _pnl(ef, xp, FIXED_QTY, is_short)
        out.append(dict(rec_score=rec, a_pnl=a_pnl, f_by=f_by, strat=strat))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default="0,15,30,45,60,90,120,150,180,240")
    ap.add_argument("--all-minute", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--days", type=int, default=0, help="直近N日のシグナルに限定(0=全期間)")
    ap.add_argument("--minute-dir", default=None)
    ap.add_argument("--short", action="store_true")
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    times = [int(x) for x in args.sweep.split(",") if x.strip()]
    cutoff = (datetime.now(JST).date() - timedelta(days=args.days)) if args.days else None

    if args.short:
        import check_signals_short as m1
        import check_signals_short_breakout as m2
        mods = [m1, m2]
        is_short = True
    else:
        import check_signals_stop as m1
        import check_signals_breakout as m2
        mods = [m1, m2]
        is_short = False

    if args.all_minute:
        universe = universe_from_pkl()
        if args.limit:
            universe = universe[:args.limit]
        pairs = [(mod, s, s, st) for s in universe
                 for mod in mods for st in getattr(mod, "STRATEGY_PARAMS", {})]
    else:
        pairs = [(mod, sym, name, strat) for mod in mods
                 for sym, name, strat in getattr(mod, "WATCHLIST", [])]

    print("=" * 78)
    print(f"T+1成行 フェア比較(後知恵なし)  {'ショート' if is_short else 'ロング'}  "
          f"mode={os.environ.get('TRADING_MODE')}")
    print(f"対象: {'5分足がある全銘柄' if args.all_minute else 'WATCHLIST'} "
          f"{len({p[1] for p in pairs})}銘柄 × 全戦略"
          + (f" / 直近{args.days}日" if cutoff else " / 全期間"))
    print("=" * 78)

    records = []
    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(_one, m, s, n, st, cutoff, times, args.minute_dir, is_short)
                    for (m, s, n, st) in pairs]
            done = 0
            for fu in as_completed(futs):
                records.extend(fu.result() or [])
                done += 1
                if done % 200 == 0:
                    print(f"  ...{done}/{len(pairs)}", flush=True)
    else:
        for (m, s, n, st) in pairs:
            records.extend(_one(m, s, n, st, cutoff, times, args.minute_dir, is_short) or [])

    if not records:
        print("シグナルが取れませんでした(5分足/データ確認)。")
        return

    def _report(recs, label):
        n_sig = len(recs)
        a_filled = [r for r in recs if r["a_pnl"] is not None]
        a_total = sum(r["a_pnl"] for r in a_filled)
        a_win = sum(1 for r in a_filled if r["a_pnl"] > 0)
        print(f"\n【{label}】 シグナル {n_sig}件")
        print(f"  A(逆指値ブレイク): 約定{len(a_filled)}件(fill率{len(a_filled)/n_sig*100:.0f}%) "
              f"勝率{a_win/len(a_filled)*100:.1f}% 損益 {a_total:+,.0f}円"
              if a_filled else "  A: 約定なし")
        print(f"  {'確認時刻':>8}{'F件':>7}{'F勝率':>8}{'F損益':>14}{'A(同母集団)':>14}{'vs A':>14}")
        best = None
        for cm in times:
            fr = [r for r in recs if cm in r["f_by"]]
            if not fr:
                continue
            f_total = sum(r["f_by"][cm] for fr_ in [fr] for r in fr_)
            f_win = sum(1 for r in fr if r["f_by"][cm] > 0)
            # 同じ母集団(5分足が取れたrec)でのA損益
            a_same = sum((r["a_pnl"] or 0) for r in fr)
            vs = f_total - a_same
            hhmm = f"09:{cm:02d}" if cm < 60 else f"{9+cm//60}:{cm%60:02d}"
            mark = ""
            if best is None or vs > best[1]:
                best = (cm, vs)
            print(f"  {hhmm:>8}{len(fr):>7}{f_win/len(fr)*100:>7.1f}%"
                  f"{f_total:>+14,.0f}{a_same:>+14,.0f}{vs:>+14,.0f}")
        if best:
            bh = f"09:{best[0]:02d}" if best[0] < 60 else f"{9+best[0]//60}:{best[0]%60:02d}"
            verdict = "✅Fが有効(要フォワード)" if best[1] > 0 else "❌全時刻Aに劣る→T+1成行は非推奨"
            print(f"  → 最良 {bh}: vs A {best[1]:+,.0f}円  {verdict}")

    _report(records, "全部")
    _report([r for r in records if (r["rec_score"] or 0) >= 60], "BT60以上")
    _report([r for r in records if (r["rec_score"] or 0) >= 70], "BT70以上")

    print("\n読み方: A=逆指値(ブレイク成功分だけ約定)。F=T+1指定時刻に全シグナル成行。")
    print("        『A(同母集団)』はFが買えた同じシグナル群をAで取った損益。vs A=F-A。")
    print("        vs A がプラスならT+1成行が優位。失効(不発)分も含むので後知恵なし。")


if __name__ == "__main__":
    main()
