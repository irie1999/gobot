"""test_entry_delay.py — lss のエントリーをシグナルの N 日後にずらすと成績がどう変わるか比較。

仮説(ユーザー): シグナル日は上がりやすい→翌日(現行D+1)はまだ勢いが残る→D+2/D+3で勢いが
尽きて下げやすい→ショートは1〜2日遅らせた方が良いのでは。

やり方(エンジンを流用):
  1) 各 (symbol, strategy) で run_limit_backtest を回し、シグナルと元のD+1エントリー
     (order_limit=トリガー / order_stop / order_target) を取得。
  2) 各トレードについて、遅延 k(=0,1,2,3...) ぶんエントリー日を後ろにずらす:
       - 新エントリー日 edt_k = 日足indexで元エントリー日の k 営業日後
       - 新トリガー lp_k = 元トリガー + ((edt_k の前日終値) - (元の前日終値=シグナル日終値))
         → 「前日終値 -1tick」の関係を保ったまま、直近終値ベースに更新
       - 損切/利確幅は元の絶対幅を維持(sm/tm=ATR基準・日々ほぼ一定の近似)
       - 新エントリー日の5分足で first-touch 約定/決済 → 損益
  3) base-month で TRAIN(≤基準月)/TEST(>基準月=OOS) に分割。OOSの成績を遅延別に比較。

近似の注意: 損切/利確幅は元トレードの絶対値を流用(ATR再計算はしない)。トリガーは直近終値に
更新。厳密なATR再計算版が必要なら engine 改修が要るが、まず傾向を見るのに十分。

使い方:
  python test_entry_delay.py --proposal lss_proposal_2026-06.py --base-month 2026-06 \
      --delays 0,1,2,3 --source local --workers 8
  (--base-month の翌月以降が OOS。--delays はカンマ区切り。0=現行D+1)
"""
from __future__ import annotations
import argparse
import runpy
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import pandas as pd

ap = argparse.ArgumentParser(description="lss エントリー遅延(0/1/2/3日)の成績比較")
ap.add_argument("--proposal", required=True, help="lss proposal(.py, SELECTED=[(code,name,strat),...])")
ap.add_argument("--base-month", type=str, default="2026-06", help="TRAIN終端月。翌月以降=OOS")
ap.add_argument("--delays", type=str, default="0,1,2,3", help="遅延日数(営業日)カンマ区切り。0=現行D+1")
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--days", type=int, default=800)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--slip", type=float, default=0.0)
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(デバッグ)")
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_exit_5m, short_pnl, short_entry_fill_5m

ble._INTRADAY_5M = False          # run_limit_backtest は日足でシグナル/トリガーだけ出す
ble._ENTRY_TYPE_FORCE = None
QTY = ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY
BASE_END = pd.Period(args.base_month, "M").end_time.normalize()
DELAYS = [int(x) for x in args.delays.split(",") if x.strip() != ""]


def _close_col(df):
    return "close" if "close" in df.columns else ("Close" if "Close" in df.columns else None)


def _load_pairs(path):
    ns = runpy.run_path(path)
    sel = ns.get("SELECTED") or []
    out = []
    seen = set()
    for row in sel:
        if len(row) >= 3:
            k = (str(row[0]), str(row[2]))
            if k not in seen:
                seen.add(k)
                out.append((str(row[0]), str(row[1]), str(row[2])))
    return out


def _scan_one(sym, name, strat):
    """1銘柄1戦略。各遅延の (train_pnls, test_pnls) を返す。"""
    res = {k: {"train": [], "test": []} for k in DELAYS}
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return res
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return res
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return res
    if df_raw is None or df_raw.empty:
        return res
    mod = mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return res
    cf = params[0]
    try:
        df = cf(df_raw.copy())
    except Exception:
        return res
    cc = _close_col(df)
    if cc is None:
        return res
    closes = df[cc]
    idx = df.index
    try:
        r = ble.run_limit_backtest(sym, name, df, lambda d: d, 0.0,
                                   args.sm, args.tm, args.days + 420, strat,
                                   entry_type="stop_sell", max_hold=0)
    except Exception:
        return res
    if not r:
        return res
    for t in r.get("trade_log", []):
        if t.get("reason") in ("発注中", "保有中"):
            continue
        edt = t.get("entry_dt")
        lp = float(t.get("order_limit", 0) or 0)
        osp = float(t.get("order_stop", 0) or 0)
        otp = float(t.get("order_target", 0) or 0)
        if lp <= 0 or osp <= 0 or otp <= 0 or edt is None:
            continue
        if lp < args.min_price or lp > args.max_price:
            continue
        stop_w = max(osp, otp) - lp          # 損切(上)幅
        tgt_w = lp - min(osp, otp)           # 利確(下)幅
        try:
            pos = idx.get_loc(edt)
        except Exception:
            continue
        if isinstance(pos, slice) or not isinstance(pos, int):
            continue
        if pos < 1:
            continue
        base_prior = float(closes.iloc[pos - 1])   # 元の前日終値(=シグナル日終値)
        for k in DELAYS:
            j = pos + k
            if j >= len(idx) or (j - 1) < 0:
                continue
            edt_k = idx[j]
            fd_k = edt_k.date() if hasattr(edt_k, "date") else edt_k
            db = by_day.get(fd_k)
            if db is None or len(db) < 2:
                continue
            prior_k = float(closes.iloc[j - 1])     # 遅延後の前日終値
            lp_k = lp + (prior_k - base_prior)      # トリガーを直近終値ベースに更新(-1tick関係を保持)
            if lp_k <= 0:
                continue
            stop_k = lp_k + stop_w
            tgt_k = lp_k - tgt_w
            if tgt_k <= 0:
                continue
            ef = short_entry_fill_5m(db, lp_k, False, entry_gap_limit=0.03)
            if ef is None:
                continue
            xp, reason, _e, _x = short_exit_5m(db, lp_k, stop_k, tgt_k, False)
            if reason in ("no_5m", "no_entry"):
                continue
            pnl = short_pnl(ef, xp, reason, QTY, FEE_ONE_WAY, args.slip)
            bucket = "train" if pd.Timestamp(fd_k) <= BASE_END else "test"
            res[k][bucket].append(pnl)
    return res


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return dict(n=0, wr=0.0, pf=0.0, pnl=0, avg=0.0)
    wins = [p for p in pnls if p > 0]
    gp = sum(wins)
    gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return dict(n=n, wr=len(wins) / n * 100, pf=pf, pnl=sum(pnls), avg=sum(pnls) / n)


def main():
    pairs = _load_pairs(args.proposal)
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] proposal={args.proposal} {len(pairs)}ペア / base={args.base_month} "
          f"(翌月以降=OOS) / delays={DELAYS} / sm={args.sm} tm={args.tm}")
    agg = {k: {"train": [], "test": []} for k in DELAYS}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_one, s, n, st): (s, st) for (s, n, st) in pairs}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  ...{done}/{len(pairs)}", flush=True)
            try:
                r = fut.result()
            except Exception:
                continue
            for k in DELAYS:
                agg[k]["train"] += r[k]["train"]
                agg[k]["test"] += r[k]["test"]

    def _pf(x):
        return "inf" if x == float("inf") else f"{x:.2f}"

    print("\n" + "=" * 78)
    print("■ エントリー遅延比較 — TEST(OOS=基準月の翌月以降) が本命")
    print("=" * 78)
    print(f"{'遅延':>6} {'エントリー':>10} | {'件数':>6}{'勝率':>7}{'PF':>7}{'総損益':>14}{'平均/件':>9}")
    for k in DELAYS:
        s = _stat(agg[k]["test"])
        lbl = f"D+{k+1}" + ("(現行)" if k == 0 else "")
        print(f"{k:>4}日 {lbl:>10} | {s['n']:>6}{s['wr']:>6.0f}%{_pf(s['pf']):>7}"
              f"{s['pnl']:>+14,.0f}{s['avg']:>+9,.0f}")
    print("\n(参考) TRAIN(=基準月以前・in-sample):")
    for k in DELAYS:
        s = _stat(agg[k]["train"])
        print(f"  遅延{k}日: {s['n']}件 勝率{s['wr']:.0f}% PF{_pf(s['pf'])} 損益{s['pnl']:+,.0f}")
    print("\n判定: TEST(OOS)の総損益/PFが最大の遅延が最良。0日(現行D+1)を上回るなら遅延に価値あり。")


if __name__ == "__main__":
    main()
