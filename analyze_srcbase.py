"""analyze_srcbase.py — マージ提案の「選定基準月ラベル」別に lss 損益を分解する。

問い(ユーザー):
  ・7月は「多くの基準月で選ばれた銘柄」(=投票数が多い)ほど成績が良いのか?
  ・7月以前は「3月・12月で選ばれた銘柄」が強いのか?

やること:
  1) マージ提案(lss_proposal_merged*.py)の SELECTED と SOURCE_BASES を読む。
  2) 各 (銘柄,戦略) を run_limit_backtest → D+1エントリー → 5分足first-touchで損益。
     (test_day_market.py と同一の現実的約定モデル: min(トリガー,始値)+-3%ガード)
  3) 各トレードに選定基準月ラベル(例 "12/3/6")と投票数(=基準月の数)を付与。
  4) 集計を3軸で出す:
     A) 投票数(1/2/3) × {7月 / 7月以前 / 全期間}
     B) 基準月メンバーシップ(12を含む/3を含む/6を含む) × {7月 / 7月以前}
     C) ラベル別(12/3/6, 12/3, 12, 3, 6, ...) × {7月 / 7月以前}
     D) 月次 × 投票数 の合計損益マトリクス

注意: これは提案の全ペアを無フィルターで流した「素の母集団」。BT30/予算フィルタは
  かけていない(=レポートの400万タブとは母集団が違う)。基準月ラベル間の相対比較用。

使い方:
  python analyze_srcbase.py --proposal lss_proposal_merged3.py --source local --workers 8
  (7月の境目を変える: --pivot-month 2026-07)
"""
from __future__ import annotations
import argparse
import runpy
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

ap = argparse.ArgumentParser(description="選定基準月ラベル別に lss 損益を分解")
ap.add_argument("--proposal", required=True, help="マージ提案(SOURCE_BASES を含む)")
ap.add_argument("--pivot-month", type=str, default="2026-07",
                help="この月=『7月』/ これ未満=『7月以前』として分割")
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
PIVOT = pd.Period(args.pivot_month, "M")


def _norm(code):
    return str(code).upper().removesuffix(".T").split(".")[0]


def _load_proposal(path):
    ns = runpy.run_path(path)
    sel = ns.get("SELECTED") or []
    src = ns.get("SOURCE_BASES") or {}
    if not src:
        raise SystemExit(f"[ERROR] {path} に SOURCE_BASES がありません。"
                         f"merge_lss_proposals.py で作り直してください。")
    pairs, seen = [], set()
    for row in sel:
        if len(row) >= 3:
            k = (str(row[0]), str(row[2]))
            if k not in seen:
                seen.add(k)
                pairs.append((str(row[0]), str(row[1]), str(row[2])))
    return pairs, src


PAIRS, SRC = _load_proposal(args.proposal)
# SRC のキーは (正規化コード, 戦略)。照合用に正規化して引く。
_SRC = {}
for k, v in SRC.items():
    if isinstance(k, tuple) and len(k) == 2:
        _SRC[(_norm(k[0]), str(k[1]))] = str(v)


def _label_of(sym, strat):
    return _SRC.get((_norm(sym), str(strat)), "?")


def _scan_one(sym, name, strat):
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
        out.append((fd, pnl))
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


def _row(tag, pnls):
    s = _stat(pnls)
    return (f"{tag:>14} | {s['n']:>6}{s['wr']:>5.0f}%{_pf(s['pf']):>6}"
            f"{s['pnl']:>+12,.0f}{s['avg']:>+9,.0f}")


def main():
    pairs = PAIRS[:args.limit] if args.limit > 0 else PAIRS
    print(f"[info] proposal={args.proposal} {len(pairs)}ペア / pivot={args.pivot_month} "
          f"(={args.pivot_month}を『7月』, 未満を『7月以前』)")
    # 投票分布
    _vd = defaultdict(int)
    for (s, n, st) in pairs:
        _vd[len(_label_of(s, st).split("/"))] += 1
    print("  投票数分布(ペア): " + " / ".join(f"{k}基準={_vd[k]}" for k in sorted(_vd, reverse=True)))

    # (label, is_july) -> [pnl], plus month grouping
    by_votes = defaultdict(lambda: defaultdict(list))    # votes -> period -> [pnl]
    by_member = defaultdict(lambda: defaultdict(list))   # base -> period -> [pnl]
    by_label = defaultdict(lambda: defaultdict(list))    # label -> period -> [pnl]
    by_month_votes = defaultdict(lambda: defaultdict(list))  # month -> votes -> [pnl]
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_one, s, n, st): (s, st) for (s, n, st) in pairs}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  ...{done}/{len(pairs)}", flush=True)
            s, st = futs[fut]
            try:
                rows = fut.result()
            except Exception:
                continue
            lab = _label_of(s, st)
            votes = len(lab.split("/"))
            bases = lab.split("/")
            for (fd, pnl) in rows:
                per = pd.Period(pd.Timestamp(fd), "M")
                pkey = "7月" if per == PIVOT else ("7月以前" if per < PIVOT else "7月以降")
                by_votes[votes][pkey].append(pnl)
                by_votes[votes]["全期間"].append(pnl)
                by_label[lab][pkey].append(pnl)
                by_label[lab]["全期間"].append(pnl)
                for b in bases:
                    by_member[b][pkey].append(pnl)
                    by_member[b]["全期間"].append(pnl)
                by_month_votes[str(per)][votes].append(pnl)

    _hdr = f"{'区分':>14} | {'件数':>6}{'勝率':>5}{'PF':>6}{'損益':>12}{'平均':>9}"

    print("\n" + "=" * 70)
    print("【A】投票数(いくつの基準月で選ばれたか)別")
    print("=" * 70)
    for per in ("7月", "7月以前", "全期間"):
        print(f"\n-- {per} --\n{_hdr}\n" + "-" * 60)
        for v in sorted(by_votes, reverse=True):
            print(_row(f"{v}基準で選定", by_votes[v].get(per, [])))

    print("\n" + "=" * 70)
    print("【B】基準月メンバーシップ別(その基準月に含まれる銘柄。かぶりは重複計上)")
    print("=" * 70)
    for per in ("7月", "7月以前"):
        print(f"\n-- {per} --\n{_hdr}\n" + "-" * 60)
        for b in sorted(by_member, key=lambda x: (len(x), x)):
            print(_row(f"{b}月を含む", by_member[b].get(per, [])))

    print("\n" + "=" * 70)
    print("【C】ラベル別(選ばれた基準月の組み合わせそのもの)")
    print("=" * 70)
    _labs = sorted(by_label, key=lambda L: (-len(by_label[L].get("全期間", [])), L))
    for per in ("7月", "7月以前"):
        print(f"\n-- {per} --\n{_hdr}\n" + "-" * 60)
        for L in _labs:
            print(_row(L, by_label[L].get(per, [])))

    print("\n" + "=" * 70)
    print("【D】月次 × 投票数 の合計損益(円)")
    print("=" * 70)
    _months = sorted(by_month_votes)
    _allv = sorted({v for m in by_month_votes for v in by_month_votes[m]}, reverse=True)
    print(f"{'月':>9} | " + " ".join(f"{v}基準".rjust(12) for v in _allv) + f"{'月計':>13}")
    print("-" * (11 + 13 * len(_allv) + 13))
    for m in _months:
        cells, mtot = [], 0.0
        for v in _allv:
            p = by_month_votes[m].get(v, [])
            cells.append(f"{sum(p):>+12,.0f}" if p else f"{'-':>12}")
            mtot += sum(p)
        print(f"{m:>9} | " + " ".join(cells) + f"{mtot:>+13,.0f}")

    print("\n判定の読み方:")
    print("  【A】7月で『3基準で選定』の損益/PFが『1基準』を上回れば、")
    print("       = 多くの基準月で選ばれた銘柄ほど7月に強い、が裏付けられる。")
    print("  【B】7月以前で『12月を含む』『3月を含む』の損益が『6月を含む』より高ければ、")
    print("       = 7月以前は12月・3月選定銘柄が強い、が裏付けられる。")
    print("  ※ これは無フィルター母集団。実運用はBT70/予算で更に絞る点に注意。")


if __name__ == "__main__":
    main()
