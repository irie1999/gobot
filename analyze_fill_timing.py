"""
analyze_fill_timing.py ― 逆指値シグナルの約定率・約定タイミング分析 (スイング)
================================================================================
シグナルが出た逆指値注文(ENTRY_EXPIRE=3営業日有効)が、
  ・全体で何%約定するか (fill率)
  ・約定したうち『翌日(T+1)/2日目(T+2)/3日目(T+3)』の内訳
を、WATCHLIST(または5分足がある全銘柄)×全戦略のバックテストから集計する。

日足バックテストのみ使用(5分足不要)。

使い方:
  python analyze_fill_timing.py                 # WATCHLIST・conservative
  python analyze_fill_timing.py --aggressive
  python analyze_fill_timing.py --all-minute --workers 8   # 5分足がある全銘柄
  python analyze_fill_timing.py --all-minute --limit 500 --workers 8
  python analyze_fill_timing.py --days 60       # 直近60日のシグナルに限定
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import run_limit_backtest, fetch, ENTRY_EXPIRE

JST = timezone(timedelta(hours=9))


def _pairs(universe):
    if universe is None:
        out = []
        for mod in (_stop, _brk):
            for sym, name, strat in getattr(mod, "WATCHLIST", []):
                out.append((mod, sym, name, strat))
        return out
    out = []
    for sym in universe:
        for strat in getattr(_stop, "STRATEGY_PARAMS", {}):
            out.append((_stop, sym, sym, strat))
        for strat in getattr(_brk, "STRATEGY_PARAMS", {}):
            out.append((_brk, sym, sym, strat))
    return out


def _one(mod, sym, name, strat, cutoff):
    """1(銘柄,戦略): (signals, 約定明細[days_to_fill,signal_date,strategy]) を返す。"""
    params = mod.STRATEGY_PARAMS.get(strat)
    if not params:
        return 0, []
    calc_fn, em, sm, tm = params
    df = fetch(sym, 400)
    if df is None:
        return 0, []
    etype = getattr(mod, "ENTRY_TYPE", "stop")
    r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm, 365, strat,
                           entry_type=etype)
    if not r:
        return 0, []
    fills = []
    for t in r.get("trade_log", []):
        if t.get("reason") == "発注中":       # 未約定(まだ期限内)は約定にカウントしない
            continue
        sdt = t.get("signal_dt")
        sd = sdt.date() if hasattr(sdt, "date") else sdt
        if cutoff and sd and sd < cutoff:
            continue
        fills.append((t.get("days_to_fill", 0), strat))
    # signals も cutoff で正確に絞るのは難しいので、cutoff未指定時のみ signals を使う
    return r.get("signals", 0), fills


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-minute", action="store_true",
                    help="WATCHLISTでなく5分足がある全銘柄を対象")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--days", type=int, default=0,
                    help="直近N日のシグナルに限定(0=全期間365日)")
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    universe = None
    if args.all_minute:
        from analyze_open_confirm_entry import universe_from_pkl
        universe = universe_from_pkl()
        if args.limit and args.limit > 0:
            universe = universe[:args.limit]

    cutoff = (datetime.now(JST).date() - timedelta(days=args.days)) if args.days else None
    pairs = _pairs(universe)
    mode = os.environ.get("TRADING_MODE", "conservative")

    print("=" * 70)
    print(f"逆指値 約定率・約定タイミング分析  mode={mode}  "
          f"有効期限={ENTRY_EXPIRE}営業日")
    scope = f"5分足がある全銘柄 {len(universe)}銘柄" if universe else "WATCHLIST"
    print(f"対象: {scope} × 全戦略"
          + (f" / 直近{args.days}日のシグナル" if cutoff else " / 直近365日"))
    print("=" * 70)

    total_signals = 0
    dtf_all: dict[int, int] = defaultdict(int)
    by_strat_sig: dict[str, int] = defaultdict(int)
    by_strat_fill: dict[str, int] = defaultdict(int)
    by_strat_t1: dict[str, int] = defaultdict(int)

    def _acc(sig, fills):
        nonlocal total_signals
        total_signals += sig
        for dtf, strat in fills:
            dtf_all[dtf] += 1
            by_strat_fill[strat] += 1
            if dtf == 1:
                by_strat_t1[strat] += 1

    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, m, s, n, st, cutoff): st
                    for (m, s, n, st) in pairs}
            done = 0
            for fu in as_completed(futs):
                sig, fills = fu.result()
                _acc(sig, fills)
                by_strat_sig[futs[fu]] += sig
                done += 1
                if done % 200 == 0:
                    print(f"  ...{done}/{len(pairs)}", flush=True)
    else:
        for (m, s, n, st) in pairs:
            sig, fills = _one(m, s, n, st, cutoff)
            _acc(sig, fills)
            by_strat_sig[st] += sig

    total_filled = sum(dtf_all.values())
    # cutoff指定時は signals をシグナル日で絞れないため fill率は概算になる旨を注記
    print(f"\nシグナル総数: {total_signals}"
          + ("  (※--days指定時のsignalsは全期間ベースの概算)" if cutoff else ""))
    print(f"約定総数    : {total_filled}")
    if total_signals > 0 and not cutoff:
        print(f"約定率(fill率): {total_filled / total_signals * 100:.1f}%  "
              f"(= 約定 {total_filled} / シグナル {total_signals})")
        print(f"失効(期限{ENTRY_EXPIRE}日内に未約定): "
              f"{total_signals - total_filled} 件 "
              f"({(total_signals - total_filled) / total_signals * 100:.1f}%)")

    expired = (total_signals - total_filled) if (total_signals and not cutoff) else None

    print("\n── シグナル→約定 内訳 (シグナル全体を100%として) ──────────")
    for d in sorted(dtf_all):
        label = {1: "翌日(T+1)約定", 2: "2日目(T+2)約定", 3: "3日目(T+3)約定"}.get(d, f"{d}日目約定")
        n = dtf_all[d]
        pct_fill = n / total_filled * 100 if total_filled else 0
        pct_sig = n / total_signals * 100 if total_signals and not cutoff else None
        line = f"  {label:<14}: {n:>6}件  (約定内{pct_fill:5.1f}%"
        line += f" / 全シグナルの{pct_sig:5.1f}%)" if pct_sig is not None else ")"
        print(line)
    if expired is not None:
        print(f"  {'失効(未約定)':<14}: {expired:>6}件  "
              f"(全シグナルの{expired / total_signals * 100:5.1f}%)  "
              f"← 3営業日以内に前日終値超えに届かず=ブレイク不発/下落")

    _t1 = dtf_all.get(1, 0)
    if total_signals and not cutoff:
        print(f"\n★ 全シグナルのうち: 翌日約定 {_t1 / total_signals * 100:.1f}% / "
              f"約定計 {total_filled / total_signals * 100:.1f}% / "
              f"失効 {expired / total_signals * 100:.1f}%")

    print("\n── 戦略別 (fill率 / 失効率 / 翌日約定) ────────────────────")
    print(f"  {'戦略':<8}{'シグナル':>8}{'約定':>7}{'失効':>7}{'fill率':>8}{'失効率':>8}{'翌日/約定':>9}")
    for st in sorted(by_strat_fill, key=lambda s: -by_strat_fill[s]):
        sig = by_strat_sig.get(st, 0)
        fil = by_strat_fill.get(st, 0)
        t1 = by_strat_t1.get(st, 0)
        exp = (sig - fil) if (sig and not cutoff) else None
        fr = f"{fil / sig * 100:.1f}%" if sig and not cutoff else "-"
        er = f"{exp / sig * 100:.1f}%" if exp is not None and sig else "-"
        t1r = f"{t1 / fil * 100:.1f}%" if fil else "-"
        exp_s = str(exp) if exp is not None else "-"
        print(f"  {st:<8}{sig:>8}{fil:>7}{exp_s:>7}{fr:>8}{er:>8}{t1r:>9}")

    print("\n読み方:")
    print("  ・fill率=シグナルのうち約定した割合。失効率=約定せず期限切れの割合(=不発/下落)。")
    print("  ・翌日約定=約定の中でT+1に約定した割合。逆指値は前日終値超えで発火するため")
    print("    寄り(T+1朝)に集中しやすい。")
    print("  ・失効が多い戦略=シグナルが出ても伸びずに終わることが多い(空振りコスト小だが機会損失)。")


if __name__ == "__main__":
    main()
