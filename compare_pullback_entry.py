"""
compare_pullback_entry.py  ―  逆指値ブレイク買い vs 押し目指値買い (スイング)
==================================================================
観察(データ確認済): 逆指値ロングは約定後ほぼ必ず一度押す
  (96.5%が初日に下落、平均初日安値 -1.5%)、それでも数日で65〜78%が戻して勝つ。

→ ブレイクで高値掴み(逆指値)をやめ、押した所を指値で安く買えば、
  同じ勝ちトレードをより良い価格で拾えるのでは? を同一シグナルで比較する。

【比較】 MACD/A7/RSI2/DON/VOL/MOM の同じシグナルで entry だけ変える:
  - baseline : 逆指値ブレイク買い  lp = 前日終値          (現行・entry_type=stop, em=0)
  - 押し目   : 指値買い            lp = 前日終値 - ATR×PB  (entry_type=limit, em=PB)
  損切り/目標は約定価格基準の同じ ATR×(sm/tm) を維持 (R:R不変・より安く入る)。

  トレードオフ:
    + 約定価格が安くなる → 1勝ちあたり利益増・損切り幅内に収まりやすい (PF↑)
    - 押さない日(そのまま上昇)は約定せず取り逃す → fill率↓ (勝ちも取り逃す)
  この差し引きで 総損益/PF が改善するか を見る。

使い方:
  python compare_pullback_entry.py                       # WATCHLIST全戦略・365日
  python compare_pullback_entry.py --days 180
  python compare_pullback_entry.py --pullbacks 0.3 0.5 1.0 1.5
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

from backtest_limit_entry import fetch, run_limit_backtest
import check_signals_stop as _stop
import check_signals_breakout as _brk

# 戦略 -> (calc_fn, em, sm, tm)
PARAMS = {}
PARAMS.update(_stop.STRATEGY_PARAMS)     # MACD / A7 / RSI2
PARAMS.update(_brk.STRATEGY_PARAMS)      # DON / VOL / MOM
WATCHLIST = list(_stop.WATCHLIST) + list(_brk.WATCHLIST)


def _agg(results: list[dict]) -> dict:
    signals = sum(r.get("signals", 0) for r in results)
    trades = [t for r in results for t in r.get("trade_log", [])
              if t.get("reason") != "発注中"]
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return dict(
        signals=signals, filled=n,
        fill_rate=n / signals * 100 if signals else 0.0,
        win_rate=len(wins) / n * 100 if n else 0.0,
        pf=pf, total_pnl=sum(t["pnl"] for t in trades),
        avg_win=gp / len(wins) if wins else 0.0,
        avg_hold=sum(t.get("hold_days", 0) for t in trades) / n if n else 0.0,
    )


def _run_variant(label, entry_type, em, bt_days, workers):
    def _one(item):
        code, name, strat = item
        calc_fn, _em0, sm, tm = PARAMS[strat]
        df = fetch(code, bt_days)
        if df is None:
            return None
        return run_limit_backtest(code, name, df, calc_fn, em, sm, tm,
                                  bt_days, strat, entry_type=entry_type)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_one, WATCHLIST):
            if r:
                results.append(r)
    st = _agg(results)
    st["label"] = label
    return st


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    ap = argparse.ArgumentParser(description="押し目指値買い vs 逆指値ブレイク買い")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--pullbacks", type=float, nargs="+", default=[0.3, 0.5, 1.0])
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    print(f"押し目買い検証: WATCHLIST {len(WATCHLIST)}件 / {args.days}日 / "
          f"mode={_stop.TRADING_MODE}", flush=True)

    rows = []
    print("  [1] 逆指値ブレイク買い (現行) ...", flush=True)
    rows.append(_run_variant("逆指値ブレイク(現行)", "stop", 0.0,
                             args.days, args.workers))
    for i, pb in enumerate(args.pullbacks, 2):
        print(f"  [{i}] 押し目指値買い -{pb}ATR ...", flush=True)
        rows.append(_run_variant(f"押し目指値 -{pb}ATR", "limit", pb,
                                 args.days, args.workers))

    base = rows[0]["total_pnl"]
    print("\n" + "=" * 100)
    print(f"  エントリー方式 比較 (同一シグナル / {args.days}日 / WATCHLIST{len(WATCHLIST)}件 / "
          f"mode={_stop.TRADING_MODE})")
    print("=" * 100)
    print(f"  {'方式':<22}{'ｼｸﾞﾅﾙ':>6}{'約定':>6}{'fill率':>7}"
          f"{'勝率':>6}{'PF':>7}{'総損益':>13}{'対現行':>12}{'平均利益':>10}{'保有':>6}")
    print("  " + "-" * 98)
    for st in rows:
        diff = st["total_pnl"] - base
        diff_s = "—" if st is rows[0] else f"{diff:+,.0f}"
        print(f"  {st['label']:<22}{st['signals']:>6}{st['filled']:>6}"
              f"{st['fill_rate']:>6.0f}%{st['win_rate']:>5.0f}%"
              f"{_pf(st['pf']):>7}{st['total_pnl']:>+13,.0f}{diff_s:>12}"
              f"{st['avg_win']:>+10,.0f}{st['avg_hold']:>5.1f}日")
    print("=" * 100)
    print("読み方:")
    print("  ・押し目指値は fill率↓(押さない日は取り逃す) だが 約定価格↓(安く買える)")
    print("  ・『対現行』がプラス かつ PFも改善 なら、押し目買いで“より安く”取れている")
    print("  ・マイナスなら、取り逃す勝ちトレードが安く買う利益を上回る → 現行(逆指値)が優位")
    print("=" * 100)


if __name__ == "__main__":
    main()
