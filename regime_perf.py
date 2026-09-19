"""
regime_perf.py — 現行(順張り)戦略の過去成績を、日経の大局レジーム状態別に集計する。

「横ばい待ち」の時に現行戦略がどんな成績だったかを数字で確認するツール。
状態は相場環境タブ/横ばい戦略ゲートと同じ判定を使い、以下4つに分ける:

  🟢 上げ        : ER≥0.20 かつ MA200上&傾き+  (現行の得意ゾーン)
  🟡 横ばい待ち  : 緩い横ばい(ER<0.20)だが厳密ゲートOFF(ER≥0.15 or |傾き|≥0.5%)
                   = 上昇内の高ボラ調整。現行が最も苦手にしやすい局面
  🟠 真横ばい    : 厳密ゲートON(ER<0.15 かつ |傾き|<0.5%) = 指数ETF逆張りの土俵
  🔴 下げ        : ER≥0.20 かつ MA200下&傾き-

現行 WATCHLIST(check_signals_stop + check_signals_breakout)を長期バックテストし、
各トレードをシグナル日の日経状態でタグ付けして勝率/PF/損益を集計する。
※ 現在のWATCHLISTを過去全体にかけるので in-sample bias あり(状態間の相対比較の参考として見る)。

使い方:
  python regime_perf.py                       # 過去12年・conservative
  python regime_perf.py --years 8
  python regime_perf.py --aggressive
  python regime_perf.py --min-price 1000 --max-price 10000
"""
import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--years", type=int, default=12)
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--aggressive", action="store_true")
args = ap.parse_args()
if args.aggressive:
    os.environ["TRADING_MODE"] = "aggressive"

import backtest_limit_entry as ble
import nikkei_analysis as na
import check_signals_stop as _stop
import check_signals_breakout as _brk


def nikkei_state_series(years: int) -> pd.Series | None:
    """各日の日経大局レジーム状態(up/wait/range/down)を先読みなしで返す。"""
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
        if e < 0.20:                                   # 緩い横ばい
            st.iloc[i] = "range" if (e < 0.15 and abs(s) < 0.5) else "wait"
        elif a and s > 0:
            st.iloc[i] = "up"
        elif (not a) and s < 0:
            st.iloc[i] = "down"
        else:
            st.iloc[i] = "wait"
    return st


LABELS = {
    "up":    ("🟢 上げ",       "#4ade80"),
    "wait":  ("🟡 横ばい待ち", "#fbbf24"),
    "range": ("🟠 真横ばい",   "#fb923c"),
    "down":  ("🔴 下げ",       "#f87171"),
}


def main():
    since = datetime.now().date() - timedelta(days=args.years * 365)
    bt_days = (datetime.now().date() - since).days + 400
    st = nikkei_state_series(args.years)
    if st is None:
        print("[ERROR] 日経データ取得失敗"); return
    _sc = st.value_counts()
    mode = "aggressive" if args.aggressive else "conservative"
    print(f"現行戦略 レジーム状態別成績 / 過去{args.years}年 / mode={mode}")
    print(f"日経状態日数: 上げ{_sc.get('up',0)} / 横ばい待ち{_sc.get('wait',0)} "
          f"/ 真横ばい{_sc.get('range',0)} / 下げ{_sc.get('down',0)}\n")

    # 現行 WATCHLIST を集約(stop + breakout)
    wl = [(s, n, strat, _stop) for s, n, strat in _stop.WATCHLIST] + \
         [(s, n, strat, _brk) for s, n, strat in _brk.WATCHLIST]

    buckets: dict = defaultdict(list)   # state -> [pnl,...]
    done = 0
    for sym, name, strat, mod in wl:
        done += 1
        if done % 20 == 0:
            print(f"  ...{done}/{len(wl)}", flush=True)
        params = mod.STRATEGY_PARAMS.get(strat)
        if not params:
            continue
        cf, em, sm, tm = params
        df = ble.fetch(sym, bt_days, min_start_date=since)
        if df is None or len(df) < 260:
            continue
        if not (args.min_price <= float(df["close"].iloc[-1]) <= args.max_price):
            continue
        res = ble.run_limit_backtest(sym, name, df, cf, em, sm, tm, bt_days, strat,
                                     entry_type=getattr(mod, "ENTRY_TYPE", "stop"))
        if not res:
            continue
        for t in res["trade_log"]:
            if t.get("reason") in ("発注中", "保有中") or t.get("signal_dt") is None:
                continue
            sd = pd.Timestamp(t["signal_dt"])
            try:
                state = st.asof(sd)
            except Exception:
                state = "?"
            if state and state != "?":
                buckets[state].append(float(t["pnl"]))

    print(f"\n{'状態':<14}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}{'平均/件':>11}")
    print("-" * 62)
    order = ["up", "wait", "range", "down"]
    for k in order:
        pl = buckets.get(k, [])
        n = len(pl)
        lbl = LABELS[k][0]
        if n == 0:
            print(f"{lbl:<14}{0:>6}{'—':>7}{'—':>7}{'—':>15}{'—':>11}")
            continue
        wins = [p for p in pl if p > 0]
        gp = sum(wins); gl = -sum(p for p in pl if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        tot = sum(pl)
        print(f"{lbl:<14}{n:>6}{len(wins)/n*100:>6.0f}%{pf_s:>7}{tot:>+15,.0f}{tot/n:>+11,.0f}")
    print("-" * 62)
    print("読み方: 『横ばい待ち』の行が現行の苦手局面の実成績。上げ行と比べてどれだけ")
    print("        劣化するか(勝率↓/PF↓/損益)で、今のような調整局面での期待値が分かる。")
    print("※ 現WATCHLISTを過去全体に適用(in-sample bias)。相対比較の参考値として見ること。")


if __name__ == "__main__":
    main()
