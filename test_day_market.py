"""test_day_market.py — 「lssの日次損益は日経/先物の動きに連動しているか」を測る。

背景(ユーザー): 日によって lss(ロング銘柄ショート)の合計損益が大きくぶれる。
  その差が『その日の日経(=寄り前は先物で予告される)』の動きで説明できるなら、
  上げ気配の日はショートを見送り/ロングのデイトレに回す、という対策を打ちたい。

test_gap_filter.py との違い:
  - あちらは『個々のトレードを寄りギャップ閾で見送る』フィルター評価。
  - こちらは『その日の lss 合計損益』を1点として、日経の各指標との
    相関・バケット別成績を出す(=日単位の連動を見る)。

日経(^N225)の3指標:
  gap%      = (当日始値 - 前日終値) / 前日終値 * 100   ← 寄りで判明(先物ナイトが予告)= 対策に使える
  intraday% = (当日終値 - 当日始値) / 当日始値 * 100    ← 当日の値動き(事後)
  day%      = (当日終値 - 前日終値) / 前日終値 * 100     ← 全日(事後)

やること:
  1) 各 (symbol,strategy) を run_limit_backtest → D+1エントリー(現行) → 5分足first-touchで損益。
  2) 損益を『エントリー日』で合計 → 日次 lss 損益系列。
  3) 日経3指標を日付で紐付け。
  4) 相関(Pearson) と バケット別(gap/intraday/day)の日次成績を出す。
  5) base-month で TRAIN/TEST(OOS)分割。TEST が本命。

使い方:
  python test_day_market.py --proposal lss_proposal_2026-06.py --base-month 2026-06 \
      --source local --workers 8
  (12月基準でも: --proposal lss_proposal_2025-12.py --base-month 2025-12)
"""
from __future__ import annotations
import argparse
import runpy
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

ap = argparse.ArgumentParser(description="lss日次損益と日経/先物の連動を測る")
ap.add_argument("--proposal", required=True)
ap.add_argument("--base-month", type=str, default="2026-06")
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


def _nikkei_by_date() -> dict:
    """^N225 の {date: (gap%, intraday%, day%)} を返す(yfinance)。"""
    try:
        import yfinance as yf
        df = yf.download("^N225", period="3y", interval="1d",
                         auto_adjust=False, progress=False)
        if df is None or df.empty:
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        op = df["Open"].astype(float)
        cl = df["Close"].astype(float)
        prev = cl.shift(1)
        gap = (op - prev) / prev * 100.0
        intr = (cl - op) / op * 100.0
        day = (cl - prev) / prev * 100.0
        out = {}
        for ts in df.index:
            g, i, d = gap.get(ts), intr.get(ts), day.get(ts)
            if pd.notna(g) and pd.notna(i) and pd.notna(d):
                out[ts.date()] = (float(g), float(i), float(d))
        return out
    except Exception as e:
        print(f"[warn] ^N225 取得失敗: {e}")
        return {}


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
    """1銘柄1戦略の [(entry_date, pnl, is_test)] を返す(現行D+1エントリー)。"""
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
        ef = short_entry_fill_5m(db, lp, False, entry_gap_limit=0.03)
        if ef is None:
            continue
        xp, reason, _e, _x = short_exit_5m(db, lp, stop_p, target_p, False)
        if reason in ("no_5m", "no_entry"):
            continue
        pnl = short_pnl(ef, xp, reason, QTY, FEE_ONE_WAY, args.slip)
        out.append((fd, pnl, pd.Timestamp(fd) > BASE_END))
    return out


def _fmt_corr(x):
    return "n/a" if x != x else f"{x:+.3f}"


def _bucket_report(rows, key_idx, label, edges, edge_labels):
    """rows=[(date,pnl,gap,intr,day)]。key_idx: 2=gap,3=intr,4=day。
    edges で日経指標をバケット分割し、日次lss成績を出す。"""
    # 日単位に集約(その日の lss 合計 pnl と、日経指標=同じ日は同値)
    day_pnl, day_key = {}, {}
    for (d, pnl, g, i, dd) in rows:
        day_pnl[d] = day_pnl.get(d, 0.0) + pnl
        day_key[d] = (g, i, dd)[key_idx - 2]
    days = sorted(day_pnl)
    print(f"\n■ {label} バケット別 lss日次成績 (TEST/OOS, {len(days)}営業日)")
    print(f"{'区分':>12} | {'日数':>5} {'勝ち日':>6} {'合計損益':>12} {'平均/日':>10} {'中央/日':>10}")
    print("-" * 68)
    for lo, hi, lab in zip([None] + edges, edges + [None], edge_labels):
        sel = []
        for d in days:
            k = day_key[d]
            if (lo is None or k >= lo) and (hi is None or k < hi):
                sel.append(day_pnl[d])
        if not sel:
            print(f"{lab:>12} | {0:>5} {'-':>6} {'-':>12} {'-':>10} {'-':>10}")
            continue
        s = pd.Series(sel)
        winday = (s > 0).mean() * 100
        print(f"{lab:>12} | {len(sel):>5} {winday:>5.0f}% {s.sum():>+12,.0f} "
              f"{s.mean():>+10,.0f} {s.median():>+10,.0f}")


def main():
    nikkei = _nikkei_by_date()
    if not nikkei:
        print("[error] 日経が取れませんでした(yfinance ^N225)。中断。")
        return
    pairs = _load_pairs(args.proposal)
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] proposal={args.proposal} {len(pairs)}ペア / base={args.base_month} "
          f"(翌月以降=OOS) / 日経日数={len(nikkei)}")

    rows_all = []   # (date, pnl, gap, intr, day, is_test)
    miss = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_one, s, n, st): 1 for (s, n, st) in pairs}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  ...{done}/{len(pairs)}", flush=True)
            try:
                rr = fut.result()
            except Exception:
                continue
            for fd, pnl, is_test in rr:
                nk = nikkei.get(fd)
                if nk is None:
                    miss += 1
                    continue
                rows_all.append((fd, pnl, nk[0], nk[1], nk[2], is_test))

    test = [(d, p, g, i, dd) for (d, p, g, i, dd, t) in rows_all if t]
    print(f"\n収集: 全{len(rows_all)}トレード(日経紐付け不可 {miss}件除外) / "
          f"TEST(OOS)トレード={len(test)}件")
    if not test:
        print("[error] OOSトレードが0件。base-monthかproposalを確認。")
        return

    # 日次集約(相関用)
    day_pnl, day_nk = {}, {}
    for (d, p, g, i, dd) in test:
        day_pnl[d] = day_pnl.get(d, 0.0) + p
        day_nk[d] = (g, i, dd)
    days = sorted(day_pnl)
    dfp = pd.Series([day_pnl[d] for d in days])
    dgap = pd.Series([day_nk[d][0] for d in days])
    dint = pd.Series([day_nk[d][1] for d in days])
    dday = pd.Series([day_nk[d][2] for d in days])

    print("\n" + "=" * 68)
    print(f"■ 日次lss損益 vs 日経 相関 (Pearson, TEST/OOS {len(days)}営業日)")
    print("=" * 68)
    print(f"  寄りギャップ% (前日終値→始値, 寄り前に判明=対策に使える) : {_fmt_corr(dfp.corr(dgap))}")
    print(f"  当日値幅%     (始値→終値, 事後)                        : {_fmt_corr(dfp.corr(dint))}")
    print(f"  全日%         (前日終値→終値, 事後)                    : {_fmt_corr(dfp.corr(dday))}")
    print("  ※ 負の相関 = 日経が上げるほど lss(ショート)は負ける、を意味する。")
    print(f"  参考: 日次lss損益  合計{dfp.sum():+,.0f} / 平均{dfp.mean():+,.0f} / "
          f"勝ち日{(dfp>0).mean()*100:.0f}%")

    rows_t = [(d, day_pnl[d], day_nk[d][0], day_nk[d][1], day_nk[d][2]) for d in days]
    # gap / intraday / day バケット
    _bucket_report(rows_t, 2, "寄りギャップ%(先物代理・対策に使える)",
                   [-1.0, -0.3, 0.0, 0.3, 1.0],
                   ["< -1%", "-1〜-0.3", "-0.3〜0", "0〜0.3", "0.3〜1", ">= 1%"])
    _bucket_report(rows_t, 3, "当日値幅%(事後)",
                   [-1.5, -0.5, 0.0, 0.5, 1.5],
                   ["< -1.5%", "-1.5〜-0.5", "-0.5〜0", "0〜0.5", "0.5〜1.5", ">= 1.5%"])
    _bucket_report(rows_t, 4, "全日%(事後)",
                   [-1.5, -0.5, 0.0, 0.5, 1.5],
                   ["< -1.5%", "-1.5〜-0.5", "-0.5〜0", "0〜0.5", "0.5〜1.5", ">= 1.5%"])

    print("\n判定の読み方:")
    print("  - 寄りギャップ% との相関が明確に負 & 上ギャップ帯の合計損益がマイナス")
    print("    → 上げ気配の日はショート不利。その日はロングに回す/見送りが有効。")
    print("  - 相関が弱い(±0.1未満)なら、日経で日を選ぶ効果は薄い(銘柄選定の方が効く)。")
    print("  - 当日値幅/全日は事後指標。連動の裏取り用で、発注判断には使えない点に注意。")


if __name__ == "__main__":
    main()
