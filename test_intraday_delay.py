"""test_intraday_delay.py — lssの『寄りから N分待って約定』の効果をOOSで検証。

背景(ユーザー): 寄り付き直後の5分は最もボラが荒く、タイトな損切り(0.1 ATR)が寄りのヒゲで
  刈られやすい(例: 資生堂は寄り3,200約定→直後の跳ね3,226で損切り。5分待てば跳ねを回避し
  下落を取れて大きな利益だった)。→ 逆指値の発動を N分遅らせると成績が上がるか?

モデル: 各トレードの約定日5分足で、最初の D 本(=D×5分)をスキップしてから逆指値を有効化。
  ・約定 = スキップ後の最初のバーから short_entry_fill_5m(day_open=スキップ後の先頭バー始値)。
  ・決済 = そのバー以降で short_exit_5m(ギャップ約定なら約定バーから)。
  ・D=0 が現行(寄りから即)。D=1/2/3 = 5/10/15分待ち。

使い方:
  python test_intraday_delay.py --proposal lss_proposal_merged3.py --base-month 2026-06 \
      --source local --workers 8
  (delays を変える: --delays 0,1,2,3,4)

注意: 提案の全ペアを無フィルターで流した母集団。BT/予算フィルタなし。相対比較用。
"""
from __future__ import annotations
import argparse
import runpy
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

ap = argparse.ArgumentParser(description="lssの寄りからN分待ち約定の効果をOOS検証")
ap.add_argument("--proposal", required=True)
ap.add_argument("--base-month", type=str, default="2026-06", help="この月以降=OOS")
ap.add_argument("--delays", type=str, default="0,1,2,3", help="遅延バー数(1=5分)カンマ区切り")
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--days", type=int, default=800)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--slip", type=float, default=0.0)
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_exit_5m, short_pnl, short_entry_fill_5m

ble._INTRADAY_5M = False
ble._ENTRY_TYPE_FORCE = None
QTY = ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY
BASE_END = pd.Period(args.base_month, "M").end_time.normalize()
DELAYS = [int(x) for x in args.delays.split(",") if x.strip() != ""]


def _load_pairs(path):
    ns = runpy.run_path(path)
    sel = ns.get("SELECTED") or []
    out, seen = [], set()
    for row in sel:
        if len(row) >= 3:
            k = (str(row[0]), str(row[2]))
            if k not in seen:
                seen.add(k)
                out.append((str(row[0]), str(row[1]), str(row[2])))
    return out


def _scan_one(sym, name, strat):
    """1銘柄1戦略: 各トレードの各遅延Dの [(delay, pnl, is_test)] を返す。"""
    out = []
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return out
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return out
    if df_raw is None or df_raw.empty:
        return out
    mod = mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return out
    try:
        df = params[0](df_raw.copy())
    except Exception:
        return out
    try:
        r = ble.run_limit_backtest(sym, name, df, lambda d: d, 0.0,
                                   args.sm, args.tm, args.days + 420, strat,
                                   entry_type="stop_sell", max_hold=0)
    except Exception:
        return out
    if not r:
        return out
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
        fd = edt.date() if hasattr(edt, "date") else edt
        db = by_day.get(fd)
        if db is None or len(db) < 2:
            continue
        stop_p, target_p = max(osp, otp), min(osp, otp)
        is_test = pd.Timestamp(fd) > BASE_END
        for D in DELAYS:
            if len(db) - D < 2:
                continue
            db_d = db.iloc[D:]                       # 最初のD本(=寄りからD×5分)をスキップ
            _day_open = float(db_d["open"].iloc[0])  # 発動時点(=D本目)の始値を寄り扱い
            ef = short_entry_fill_5m(db_d, lp, False, entry_gap_limit=0.03, day_open=_day_open)
            if ef is None:
                continue
            _gap = abs(ef - lp) > 1e-9
            xp, reason, _e, _x = short_exit_5m(db_d, lp, stop_p, target_p, False,
                                               include_entry_bar=_gap)
            if reason in ("no_5m", "no_entry"):
                continue
            pnl = short_pnl(ef, xp, reason, QTY, FEE_ONE_WAY, args.slip)
            out.append((D, pnl, is_test))
    return out


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return dict(n=0, wr=0.0, pf=0.0, pnl=0, avg=0.0)
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return dict(n=n, wr=len(wins) / n * 100, pf=pf, pnl=sum(pnls), avg=sum(pnls) / n)


def _pf(x):
    return "inf" if x == float("inf") else f"{x:.2f}"


def main():
    pairs = _load_pairs(args.proposal)
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] proposal={args.proposal} {len(pairs)}ペア / base={args.base_month} "
          f"(翌月以降=OOS) / 遅延={DELAYS}本(×5分)")

    by_delay = defaultdict(lambda: defaultdict(list))   # D -> period -> [pnl]
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_one, s, n, st): 1 for (s, n, st) in pairs}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  ...{done}/{len(pairs)}", flush=True)
            try:
                rows = fut.result()
            except Exception:
                continue
            for (D, pnl, is_test) in rows:
                key = "OOS" if is_test else "IS"
                by_delay[D][key].append(pnl)
                by_delay[D]["全期間"].append(pnl)

    _hdr = f"{'遅延':>8} | {'件数':>6}{'勝率':>6}{'PF':>6}{'損益':>12}{'平均/件':>9}"
    print("\n" + "=" * 70)
    print("■ 寄りからの待ち時間別 lss成績 (D×5分待ってから約定)")
    print("=" * 70)
    print("  ★本命は OOS。件数が遅延で減る(待つ間に約定機会を逃す分)のも重要な指標。")
    for per in ("OOS", "IS", "全期間"):
        print(f"\n-- {per} --\n{_hdr}\n" + "-" * 60)
        for D in sorted(by_delay):
            s = _stat(by_delay[D].get(per, []))
            _lbl = "即(0分)" if D == 0 else f"{D*5}分待ち"
            print(f"{_lbl:>8} | {s['n']:>6}{s['wr']:>5.0f}%{_pf(s['pf']):>6}"
                  f"{s['pnl']:>+12,.0f}{s['avg']:>+9,.0f}")

    print("\n判定の読み方:")
    print("  ・OOSで『5分/10分待ち』の総損益・平均/件が『即(0分)』を上回れば、待ちに効果あり。")
    print("  ・ただし件数が大きく減るなら『約定機会を逃している』=総損益では不利なことも。")
    print("  ・平均/件(1トレードの質)が上がっても件数減で総損益が下がるなら、両にらみで判断。")
    print("  ※ 無フィルター母集団。実運用はBT/予算で更に絞る。")


if __name__ == "__main__":
    main()
