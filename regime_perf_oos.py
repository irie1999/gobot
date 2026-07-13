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
ap.add_argument("--monthly", action="store_true", help="OOSトレードを月別にも集計して表示")
ap.add_argument("--min-bt", type=float, default=0.0,
                help="このBTスコア以上のOOSトレードだけで状態別/月別を集計(例70)。BT帯別表は常に表示")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--max-dd", type=float, default=15.0)
ap.add_argument("--years", type=int, default=15, help="日経レジーム系列の取得年数")
ap.add_argument("--fee", type=float, default=None,
                help="片道手数料を上書き(例0=無料)。既定は0.001(往復0.2%)")
ap.add_argument("--slip", type=float, default=None,
                help="逆指値買い/損切りスリッページを上書き(例0.002=0.2%)。既定0.005")
ap.add_argument("--aggressive", action="store_true")
ap.add_argument("--short", action="store_true",
                help="ショート戦略で検証(A7_S/RSI2_S/MACD_S/DON_S/MOM_S/GAP_S)")
args = ap.parse_args()
if args.aggressive:
    os.environ["TRADING_MODE"] = "aggressive"

import numpy as np
import pandas as pd
import backtest_limit_entry as ble
import nikkei_analysis as na

# コスト上書き(あなたの実コストで測る用)。run_limit_backtest はモジュール定数を
# 実行時に参照するので、ここで書き換えれば全バックテストに反映される。
if args.fee is not None:
    ble.FEE_PCT_ONE_WAY = args.fee
if args.slip is not None:
    ble.SLIPPAGE_STOP_PCT = args.slip

# 検証対象の戦略群(ロング=--family both / ショート=--family short+short_brk)
if args.short:
    STRATS = ["A7_S", "RSI2_S", "MACD_S", "DON_S", "MOM_S", "GAP_S"]
    _MOD_NAMES = ("check_signals_short", "check_signals_short_breakout")
else:
    STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]
    _MOD_NAMES = ("check_signals_stop", "check_signals_breakout")
MODE_SUFFIX = "_aggressive" if args.aggressive else ""

_mods = {}
for _n in _MOD_NAMES:
    try:
        _mods[_n] = __import__(_n)
    except Exception as _e:
        print(f"[WARN] {_n} の読み込み失敗: {_e}")


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


def asof_bt(full, mod, sd, periods=(30, 90, 180, 365)):
    """シグナル日 sd 以前に決済したトレードだけで当時BTスコアを計算(先読みなし)。"""
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
                n += 1; tot += pnl
                if pnl > 0:
                    w += 1; gp += pnl
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


def _agg(rows):
    """(pnl の list) → 件数/勝率/PF/損益/平均 の表示文字列タプル。"""
    n = len(rows)
    if n == 0:
        return (0, "—", "—", "—", "—")
    wins = [p for p in rows if p > 0]
    gp = sum(wins); gl = -sum(p for p in rows if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    tot = sum(rows)
    return (n, f"{len(wins)/n*100:.0f}%", pf_s, f"{tot:+,.0f}", f"{tot/n:+,.0f}")


def main():
    st = nikkei_state_series(args.years)
    if st is None:
        print("[ERROR] 日経データ取得失敗"); return

    bases = ([b.strip() for b in args.bases.split(",")] if args.bases else _detect_bases())
    if not bases:
        print("[なし] 基準日CSVが見つかりません。先に scan_walkforward --as-of を回してください。")
        return

    mode = "aggressive" if args.aggressive else "conservative"
    direction = "ショート" if args.short else "ロング"
    print(f"ローリング再選定OOS [{direction}] / mode={mode} / 再選定間隔{args.interval_months}ヶ月 "
          f"/ 価格{args.min_price:.0f}-{args.max_price:.0f} / top{args.per_strategy}")
    print(f"コスト: 手数料片道{ble.FEE_PCT_ONE_WAY*100:.2f}% / "
          f"スリッページ{ble.SLIPPAGE_STOP_PCT*100:.2f}%")
    print(f"基準月: {', '.join(bases)}\n")

    today = ble._TODAY
    TR: list = []          # 全OOSトレード: {"pnl","state","month","bt"}
    per_base: list = []    # (base, n, pnl)

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
            full = res["trade_log"]
            for t in full:
                if t.get("reason") in ("発注中", "保有中") or t.get("signal_dt") is None:
                    continue
                sd = t["signal_dt"].date() if hasattr(t["signal_dt"], "date") else t["signal_dt"]
                # 基準月以降(=OOS)のシグナルのみ採用
                if not (bd <= sd <= oos_end):
                    continue
                state = st.asof(pd.Timestamp(sd))
                bt = asof_bt(full, mod, sd)   # 当時BTスコア(先読みなし)
                TR.append({"pnl": float(t["pnl"]),
                           "state": state if (state and state != "?") else None,
                           "month": pd.Timestamp(sd).strftime("%Y-%m"),
                           "bt": bt})
                b_n += 1; b_pnl += float(t["pnl"])
        per_base.append((base, b_n, b_pnl))
        print(f"  {base}→+{args.interval_months}ヶ月: 選定{len(wl)}件 / OOS取引{b_n} / 損益{b_pnl:+,.0f}")

    # ── BTフィルタ(状態別/月別に適用) ──
    def _pnls(rows):
        return [r["pnl"] for r in rows]
    filt = [r for r in TR if args.min_bt <= 0 or (r["bt"] is not None and r["bt"] >= args.min_bt)]
    bt_note = f" (BT≥{args.min_bt:.0f}のみ)" if args.min_bt > 0 else ""

    # ── 状態別集計 ──
    print(f"\n【状態別{bt_note}】")
    print(f"{'状態':<14}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}{'平均/件':>11}")
    print("-" * 62)
    for k in ["up", "wait", "range", "down"]:
        n, wr, pf_s, tot, avg = _agg(_pnls([r for r in filt if r["state"] == k]))
        print(f"{LABELS[k]:<14}{n:>6}{wr:>7}{pf_s:>7}{tot:>15}{avg:>11}")
    print("-" * 62)
    print("これは look-ahead を排除した OOS 成績(各時点で選定→直後だけ運用)。")

    # ── BT帯別集計(常に全トレードで表示) ──
    print(f"\n【BTスコア帯別(当時BT・先読みなし)】")
    print(f"{'BT帯':<14}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}{'平均/件':>11}")
    print("-" * 62)
    _bt_bands = [("BT≥70", 70, 1e9), ("BT60-69", 60, 70), ("BT40-59", 40, 60),
                 ("BT<40", -1e9, 40), ("スコアなし", None, None)]
    for lbl, lo, hi in _bt_bands:
        if lo is None:
            rows = [r for r in TR if r["bt"] is None]
        else:
            rows = [r for r in TR if r["bt"] is not None and lo <= r["bt"] < hi]
        n, wr, pf_s, tot, avg = _agg(_pnls(rows))
        print(f"{lbl:<14}{n:>6}{wr:>7}{pf_s:>7}{tot:>15}{avg:>11}")
    print("-" * 62)
    print("↑ BT≥70/≥60 だけOOSでプラスなら『高BTに絞れば本物』。全帯マイナスなら選定自体が過学習。")

    if args.monthly:
        by_month: dict = defaultdict(list)
        for r in filt:
            by_month[r["month"]].append(r["pnl"])
        print(f"\n【月別{bt_note}】")
        print(f"{'月':<9}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}")
        print("-" * 46)
        for mk in sorted(by_month):
            pl = by_month[mk]
            n = len(pl)
            wins = [p for p in pl if p > 0]
            gp = sum(wins); gl = -sum(p for p in pl if p <= 0)
            pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
            pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
            print(f"{mk:<9}{n:>6}{len(wins)/n*100:>6.0f}%{pf_s:>7}{sum(pl):>+15,.0f}")
        print("-" * 46)
        print("↑ 各月の『本物のOOS』損益。レポート月別(現WATCHLISTを過去に当てた値=汚染)と")
        print("  比べると、holdout短設定の in-sample 分がどれだけ数字を盛っていたかが分かる。")


if __name__ == "__main__":
    main()
