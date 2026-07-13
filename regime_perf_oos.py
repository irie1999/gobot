"""
regime_perf_oos.py — 現行(順張り・ロング)戦略の "本物の" レジーム状態別成績。

look-ahead を排除したローリング再選定 Walk-forward:
  各基準月で as-of 選定した WATCHLIST を、その直後の N ヶ月(=次の再選定まで)だけ
  運用(OOS)し、全基準月ぶんを繋ぎ合わせて大局レジーム状態別に集計する。

  regime_perf.py … 現WATCHLISTを過去全体に適用(in-sample bias, 過大評価)
  これ(oos版) … 各時点で選んだ銘柄の直後の未来だけ = 実運用と同じ(バイアスなし)

前提: 先に各基準月の as-of スキャンを回して CSV を用意する:
  foreach($b in "2018-06-30","2019-06-30",...,"2025-06-30"){
    python scan_walkforward.py --family both --as-of $b --max-price 6000 --workers 8
  }
  → walkforward_results/walkforward_<戦略>_<基準日>.csv が出力される

使い方:
  python regime_perf_oos.py --bases 2018-06-30,2019-06-30,2020-06-30,2021-06-30,2022-06-30,2023-06-30,2024-06-30,2025-06-30
  python regime_perf_oos.py            # walkforward_results から基準日を自動検出
  python regime_perf_oos.py --interval-months 12 --per-strategy 10 --max-price 6000
"""
import argparse
import csv
import glob
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument("--bases", default=None, help="基準日をカンマ区切りで(省略時はCSVから自動検出)")
ap.add_argument("--interval-months", type=int, default=12, help="各基準月のOOS窓(=再選定間隔)")
ap.add_argument("--per-strategy", type=int, default=10)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--max-dd", type=float, default=15.0)
ap.add_argument("--years", type=int, default=15, help="日経レジーム系列の取得年数")
ap.add_argument("--aggressive", action="store_true")
args = ap.parse_args()
if args.aggressive:
    os.environ["TRADING_MODE"] = "aggressive"

import numpy as np
import pandas as pd
import backtest_limit_entry as ble
import nikkei_analysis as na

# 現行ライブが使うロング6戦略(scan --family both の一部)
STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]
MODE_SUFFIX = "_aggressive" if args.aggressive else ""

_mods = {}
for _n in ("check_signals_stop", "check_signals_breakout"):
    try:
        _mods[_n] = __import__(_n)
    except Exception:
        pass


def mod_for(strat):
    for o in _mods.values():
        if strat in getattr(o, "STRATEGY_PARAMS", {}):
            return o
    return None


def nikkei_state_series(years: int) -> pd.Series | None:
    c = na.fetch_n225(years)
    if c is None or len(c) < 260:
        return None
    c = c.sort_index()
    ma200 = c.rolling(200).mean()
    slope = ma200.pct_change(20) * 100
    er = (c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, np.nan)
    above = c >= ma200
    st = pd.Series(index=c.index, dtype=object)
    for i in range(len(c)):
        e, s, a, m = er.iloc[i], slope.iloc[i], above.iloc[i], ma200.iloc[i]
        if not np.isfinite(m) or not np.isfinite(e):
            st.iloc[i] = "?"; continue
        if e < 0.20:
            st.iloc[i] = "range" if (e < 0.15 and abs(s) < 0.5) else "wait"
        elif a and s > 0:
            st.iloc[i] = "up"
        elif (not a) and s < 0:
            st.iloc[i] = "down"
        else:
            st.iloc[i] = "wait"
    return st


LABELS = {"up": "🟢 上げ", "wait": "🟡 横ばい待ち", "range": "🟠 真横ばい", "down": "🔴 下げ"}


def _detect_bases() -> list[str]:
    """walkforward_results から (全STRATが揃っている) 基準日を検出。"""
    found: dict[str, set] = defaultdict(set)
    for strat in STRATS:
        for f in glob.glob(f"walkforward_results/walkforward_{strat}{MODE_SUFFIX}_*.csv"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
            if m:
                found[m.group(1)].add(strat)
    # holdout系(_holdoutNNd_)を除外
    bases = [d for d, ss in found.items()
             if len(ss) >= 3 and not any("holdout" in os.path.basename(x)
                 for x in glob.glob(f"walkforward_results/*_{d}.csv"))]
    return sorted(bases)


def _select_watchlist(base: str) -> dict:
    """基準日 base の as-of CSV から WATCHLIST を選定(TEST損益>0 / DD<=15 / 価格帯 / top-N)。"""
    wl = {}
    for strat in STRATS:
        pat = f"walkforward_results/walkforward_{strat}{MODE_SUFFIX}_{base}.csv"
        for f in glob.glob(pat):
            if "holdout" in os.path.basename(f):
                continue
            try:
                rows = list(csv.DictReader(open(f, encoding="utf-8")))
            except Exception:
                continue
            filt = [r for r in rows
                    if float(r.get("total_test_pnl", 0) or 0) > 0
                    and float(r.get("max_drawdown_pct", 999) or 999) <= args.max_dd
                    and args.min_price <= float(r.get("latest_price", 0) or 0) <= args.max_price]
            filt.sort(key=lambda r: -float(r.get("total_test_pnl", 0) or 0))
            for r in filt[: args.per_strategy]:
                wl[(r["symbol"], strat)] = r.get("name", "")
    return wl


def main():
    st = nikkei_state_series(args.years)
    if st is None:
        print("[ERROR] 日経データ取得失敗"); return

    bases = ([b.strip() for b in args.bases.split(",")] if args.bases else _detect_bases())
    if not bases:
        print("[なし] 基準日CSVが見つかりません。先に scan_walkforward --as-of を回してください。")
        return

    mode = "aggressive" if args.aggressive else "conservative"
    print(f"ローリング再選定OOS / mode={mode} / 再選定間隔{args.interval_months}ヶ月 "
          f"/ 価格{args.min_price:.0f}-{args.max_price:.0f} / top{args.per_strategy}")
    print(f"基準月: {', '.join(bases)}\n")

    today = ble._TODAY
    buckets: dict = defaultdict(list)     # state -> [pnl,...]
    per_base: list = []                   # (base, n, pnl)

    for base in bases:
        bd = datetime.strptime(base, "%Y-%m-%d").date()
        oos_end = bd + timedelta(days=int(args.interval_months * 30.44))
        wl = _select_watchlist(base)
        if not wl:
            print(f"  {base}: 選定0件 (CSVなし/条件外) → スキップ")
            continue
        warm = bd - timedelta(days=760)
        bt_days = (today - warm).days
        b_n = 0; b_pnl = 0.0
        for (sym, strat), name in wl.items():
            mod = mod_for(strat)
            if mod is None:
                continue
            params = mod.STRATEGY_PARAMS.get(strat)
            if not params:
                continue
            cf, em, sm, tm = params
            df = ble.fetch(sym, bt_days, min_start_date=warm)
            if df is None:
                continue
            df = df[df.index <= pd.Timestamp(oos_end)]
            if len(df) < 210:
                continue
            res = ble.run_limit_backtest(sym, name, df, cf, em, sm, tm, bt_days, strat,
                                         entry_type=getattr(mod, "ENTRY_TYPE", "stop"))
            if not res:
                continue
            for t in res["trade_log"]:
                if t.get("reason") in ("発注中", "保有中") or t.get("signal_dt") is None:
                    continue
                sd = t["signal_dt"].date() if hasattr(t["signal_dt"], "date") else t["signal_dt"]
                # 基準月以降(=OOS)のシグナルのみ採用
                if not (bd <= sd <= oos_end):
                    continue
                state = st.asof(pd.Timestamp(sd))
                if state and state != "?":
                    buckets[state].append(float(t["pnl"]))
                b_n += 1; b_pnl += float(t["pnl"])
        per_base.append((base, b_n, b_pnl))
        print(f"  {base}→+{args.interval_months}ヶ月: 選定{len(wl)}件 / OOS取引{b_n} / 損益{b_pnl:+,.0f}")

    # ── 状態別集計 ──
    print(f"\n{'状態':<14}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}{'平均/件':>11}")
    print("-" * 62)
    for k in ["up", "wait", "range", "down"]:
        pl = buckets.get(k, [])
        n = len(pl)
        if n == 0:
            print(f"{LABELS[k]:<14}{0:>6}{'—':>7}{'—':>7}{'—':>15}{'—':>11}")
            continue
        wins = [p for p in pl if p > 0]
        gp = sum(wins); gl = -sum(p for p in pl if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        tot = sum(pl)
        print(f"{LABELS[k]:<14}{n:>6}{len(wins)/n*100:>6.0f}%{pf_s:>7}{tot:>+15,.0f}{tot/n:>+11,.0f}")
    print("-" * 62)
    print("これは look-ahead を排除した OOS 成績(各時点で選定→直後だけ運用)。")
    print("『🟡 横ばい待ち』の行が、今のような調整局面での現行戦略の本物の期待値。")


if __name__ == "__main__":
    main()
