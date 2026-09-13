"""
verify_base.py — 指定基準月の選定WATCHLISTを「基準月〜+Nヶ月」だけ軽量バックテストし、
テキストで月別集計(全部 / BT70以上・当時スコア)を出す。HTMLを作らないので高速。

現在まで走らせると重い過去検証を、基準月+1年など短い窓でサッと確認する用途。
run_signals_holdout_all と同じ選定ロジック(全holdout union / top-N / 価格フィルタ)と
同じ当時BTスコア(先読みなし)を使う。

使い方:
  python verify_base.py 2019-12-31                    # 基準月から12ヶ月OOS(conservative)
  python verify_base.py 2019-12-31 --months 18
  python verify_base.py 2019-12-31 --mode aggressive
"""
import argparse
import csv
import glob
import os
from collections import defaultdict
from datetime import datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument("date", help="基準月 YYYY-MM-DD")
ap.add_argument("--months", type=int, default=12, help="基準月から先の集計月数(既定12)")
ap.add_argument("--mode", default="conservative", choices=["conservative", "aggressive"])
ap.add_argument("--min-price", type=float, default=1000)
ap.add_argument("--max-price", type=float, default=10000)
ap.add_argument("--per-strategy", type=int, default=10)
ap.add_argument("--long-only", action="store_true", help="ロング戦略のみ(ショート除外)")
args = ap.parse_args()
os.environ["TRADING_MODE"] = args.mode

base = datetime.strptime(args.date, "%Y-%m-%d").date()
end = base + timedelta(days=int(args.months * 30.44))

import pandas as pd
import backtest_limit_entry as ble

# run_signals_holdout_all が損益タブで使う戦略
STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]
if not args.long_only:
    STRATS += ["A7_S", "RSI2_S", "MACD_S", "DON_S", "MOM_S", "GAP_S"]
HOLDOUTS = [30, 60, 90, 120, 150, 180]
mode_suffix = "_aggressive" if args.mode == "aggressive" else ""

# 戦略→モジュール
_mods = {}
for _n in ("check_signals_stop", "check_signals_breakout",
           "check_signals_short", "check_signals_short_breakout"):
    try:
        _mods[_n] = __import__(_n)
    except Exception:
        pass


def mod_for(strat):
    for o in _mods.values():
        if strat in getattr(o, "STRATEGY_PARAMS", {}):
            return o
    return None


# ── WATCHLIST(基準月CSV・全holdout union・戦略ごと top-N) ──────────────────────
wl = {}  # (sym, strat) -> name
for strat in STRATS:
    for ho in HOLDOUTS:
        pat = f"walkforward_results/walkforward_{strat}{mode_suffix}_holdout{ho}d_{args.date}.csv"
        for f in glob.glob(pat):
            rows = list(csv.DictReader(open(f, encoding="utf-8")))
            filt = [r for r in rows
                    if float(r.get("total_test_pnl", 0) or 0) > 0
                    and float(r.get("max_drawdown_pct", 999) or 999) <= 15
                    and args.min_price <= float(r.get("latest_price", 0) or 0) <= args.max_price]
            filt.sort(key=lambda r: -float(r.get("total_test_pnl", 0) or 0))
            for r in filt[: args.per_strategy]:
                wl[(r["symbol"], strat)] = r.get("name", "")

print(f"基準月 {base} / OOS集計 {base}〜{end} ({args.months}ヶ月) / mode={args.mode} / WATCHLIST {len(wl)}件")
if not wl:
    print("[なし] 基準月のCSVが見つからないか、スキャン未完了です。")
    raise SystemExit(1)


def asof_bt(full, mod, sd, periods=(30, 90, 180, 365)):
    """シグナル日以前に決済したトレードだけで当時BTスコアを計算(先読みなし)。"""
    pr = {}
    for p in periods:
        lo = sd - timedelta(days=p)
        n = w = 0
        gp = gl = tot = 0.0
        for t in full:
            if t.get("reason") in ("発注中", "保有中"):
                continue
            ed = t.get("exit_dt")
            if ed is None:
                continue
            edd = ed.date() if hasattr(ed, "date") else ed
            if lo <= edd < sd:
                pnl = float(t.get("pnl", 0.0))
                n += 1
                tot += pnl
                if pnl > 0:
                    w += 1
                    gp += pnl
                else:
                    gl += -pnl
        if n == 0:
            continue
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[p] = {"trades": n, "win_rate": w / n * 100.0, "pf": pf, "total_pnl": tot}
    if not pr:
        return None
    try:
        return mod.calc_recommend_score(pr)[0]
    except Exception:
        return None


today = ble._TODAY
warm_start = base - timedelta(days=760)   # 当時BTの直近1年 + 指標warmup 用
bt_days = (today - warm_start).days

all_trades = []   # (signal_date, pnl, bt)
done = 0
for (sym, strat), name in wl.items():
    done += 1
    if done % 20 == 0:
        print(f"  ...{done}/{len(wl)}", flush=True)
    mod = mod_for(strat)
    if mod is None:
        continue
    params = mod.STRATEGY_PARAMS.get(strat)
    if not params:
        continue
    cf, em, sm, tm = params
    etype = getattr(mod, "ENTRY_TYPE", "stop_sell" if strat.endswith("_S") else "stop")
    df = ble.fetch(sym, bt_days, min_start_date=warm_start)
    if df is None:
        continue
    df = df[df.index <= pd.Timestamp(end)]
    if len(df) < 210:
        continue
    res = ble.run_limit_backtest(sym, name, df, cf, em, sm, tm, bt_days, strat, entry_type=etype)
    if not res:
        continue
    full = [t for t in res["trade_log"]
            if t.get("reason") not in ("発注中", "保有中") and t.get("exit_dt")]
    for t in full:
        sd = t["signal_dt"].date()
        if base <= sd <= end:   # 集計は基準月以降のシグナルのみ(=フォワードOOS)
            all_trades.append((sd, float(t["pnl"]), asof_bt(full, mod, sd)))


def agg(keep):
    m = defaultdict(lambda: [0, 0, 0.0])
    for sd, pnl, bt in all_trades:
        if keep(bt):
            k = sd.strftime("%Y-%m")
            m[k][1] += 1
            m[k][2] += pnl
            if pnl > 0:
                m[k][0] += 1
    return m


allm = agg(lambda b: True)
bt70 = agg(lambda b: b is not None and b >= 70)
print(f"\n{'月':<9}{'全件':>5}{'勝率':>6}{'損益':>13}   |{'BT70':>6}{'勝率':>6}{'損益':>13}")
print("-" * 64)
ta = [0, 0, 0.0]
tb = [0, 0, 0.0]
for k in sorted(set(allm) | set(bt70)):
    a = allm.get(k, [0, 0, 0])
    b = bt70.get(k, [0, 0, 0])
    for i in range(3):
        ta[i] += a[i]
        tb[i] += b[i]
    print(f"{k:<9}{a[1]:>5}{(a[0]/a[1]*100 if a[1] else 0):>5.0f}%{a[2]:>+13.0f}   |"
          f"{b[1]:>6}{(b[0]/b[1]*100 if b[1] else 0):>5.0f}%{b[2]:>+13.0f}")
print("-" * 64)
print(f"{'合計':<9}{ta[1]:>5}{(ta[0]/ta[1]*100 if ta[1] else 0):>5.0f}%{ta[2]:>+13.0f}   |"
      f"{tb[1]:>6}{(tb[0]/tb[1]*100 if tb[1] else 0):>5.0f}%{tb[2]:>+13.0f}")
