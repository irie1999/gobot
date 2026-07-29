"""analyze_short_nofill_long.py  ―  #6 検証: ショート不約定 → 9:05 ロング

仮説(pending_ideas #6):
  lss は「強い(ロング候補)銘柄を逆指値売り」。ショートが寄り(bar0=09:00-09:05)で約定しない
  =株価がトリガー(前日終値-1tick)まで下がらず上に留まった=強い、の合図。
  → そのとき 9:05(2本目始値)で成行ロング、同日大引けで決済 すれば取れるのでは?

このスクリプトは analyze_fill_time.py と同じ自己完結パターン(コア非改変・自前5分足シミュ)。
既存の発注/選定/損益ロジックには一切触れない。

  bar0 でショート約定した(db.low[0] <= トリガー)     → ショート成立 = ロング対象外
  bar0 でショート未約定(db.low[0] >  トリガー)        → 9:05 成行ロング → 大引け決済
  ギャップダウン過大(始値 < トリガー×(1-3%))          → 弱い = 対象外(指値ガード相当)

使い方:
  python analyze_short_nofill_long.py --bt-min 30
  python analyze_short_nofill_long.py --oos                 # 各ペア選定基準月より後(純OOS)
  python analyze_short_nofill_long.py --asof-bt --oos       # 先読みなしBT × 純OOS(最も厳しい)
  python analyze_short_nofill_long.py --compare-short       # 同じ日にショートを続けた場合と比較

出力: BT帯別に「9:05ロング(同日決済)」の 件数/合計/勝率/PF/1件あたり。
"""
from __future__ import annotations
import argparse
import importlib.util
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="#6 ショート不約定→9:05ロング 検証")
ap.add_argument("--symbols-file", type=str, default=None)
ap.add_argument("--days", type=int, default=240)
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--bt-min", type=float, default=0.0,
                help="サマリー行のBT下限(既定0=全件)。BT帯別は常に全帯表示")
ap.add_argument("--long-slip", type=float, default=0.0,
                help="ロング成行約定の不利スリッページ(既定0)。保守側で見るなら0.005等")
ap.add_argument("--oos", action="store_true",
                help="純OOSのみ: 各ペアの選定基準月(SOURCE_BASES)より後の日だけ集計")
ap.add_argument("--oos-proposal", type=str, default="lss_proposal_cumul.py")
ap.add_argument("--asof-bt", action="store_true",
                help="BTを各取引時点までのデータだけで算出(先読みなし=真OOS)。既定は今日基準BT(ペア単位)")
ap.add_argument("--compare-short", action="store_true",
                help="同じ未約定日に『ショートを終日待って約定させた場合』の成績も併記(参考)")
args = ap.parse_args()

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick, round_to_tick, tick_size
from check_signals_stop import PERIODS as _BT_PERIODS, calc_recommend_score as _calc_bt
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import (short_entry_fill_5m, short_exit_5m, short_pnl,
                                  long_pnl)

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._INTRADAY_5M = False
QTY = ble.FIXED_QTY
FEE = ble.FEE_PCT_ONE_WAY
_GAP = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
TODAY = pd.Timestamp.now().normalize()

# BT帯
_BT_BANDS = [(0, 30, "0-29"), (30, 40, "30-39"), (40, 50, "40-49"),
             (50, 60, "50-59"), (60, 200, "60+")]


def _bt_band(bt: float) -> str:
    for lo, hi, lab in _BT_BANDS:
        if lo <= bt < hi:
            return lab
    return "60+"


def _month_end(m: int) -> pd.Timestamp:
    m = int(m)
    year = 2025 if m >= 7 else 2026
    return pd.Period(f"{year}-{m:02d}", "M").end_time.normalize()


_OOS_BASES: dict = {}


def _load_oos_bases(path: str) -> dict:
    if not Path(path).exists():
        sys.exit(f"[error] --oos 用の {path} が無い(SOURCE_BASES を持つ提案ファイルを指定)")
    spec = importlib.util.spec_from_file_location("_oos", path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    sb = getattr(m, "SOURCE_BASES", None) or {}
    out = {}
    for k, months in sb.items():
        code = str(k[0]).split(".")[0] if isinstance(k, (tuple, list)) else str(k).split(".")[0]
        strat = str(k[1]) if isinstance(k, (tuple, list)) and len(k) >= 2 else ""
        try:
            latest = max(_month_end(mm) for mm in months)
        except Exception:
            continue
        out[(code, strat)] = latest
    print(f"[OOS] {path} の SOURCE_BASES {len(out)}ペアを読込 → 各ペア基準月より後のみ集計")
    return out


def _load_selected() -> list[tuple]:
    path = args.symbols_file
    if path is None:
        props = sorted(Path(".").glob("lss_proposal_cumul.py")) \
            or sorted(Path(".").glob("lss_watchlist_proposal_*.py")) \
            or sorted(Path(".").glob("lss_proposal_*.py"))
        if props:
            path = str(props[-1])
    if path is None or not Path(path).exists():
        sys.exit("[error] 選定ファイルが見つかりません(--symbols-file で指定)")
    spec = importlib.util.spec_from_file_location("_sel", path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    rows = getattr(m, "SELECTED", None) or getattr(m, "SELECTED_PAIRS", None) or []
    out = [(str(r[0]), str(r[1]) if len(r) >= 3 else "", str(r[-1])) for r in rows if len(r) >= 2]
    print(f"[選定] {path} から {len(out)} ペア")
    return out


def _prices(olp, osp, otp):
    base = round_to_tick(olp)
    trig = float(round_to_tick(base - tick_size(base)))
    stop_p = float(ceil_to_tick(max(osp, otp)))
    tgt_p = float(ceil_to_tick(min(osp, otp)))
    return trig, stop_p, tgt_p


def _bt_from_series(series, asof=None) -> int:
    """series=[(date, baseline_pnl)] からBT算出。asof指定=その日より前だけ(先読みなし)。"""
    pr = {}
    for P in _BT_PERIODS:
        if asof is not None:
            a = pd.Timestamp(asof); lo = a - pd.Timedelta(days=P)
            sub = [p for (d, p) in series if lo <= pd.Timestamp(d) < a]
        else:
            cut = TODAY - pd.Timedelta(days=P)
            sub = [p for (d, p) in series if pd.Timestamp(d) >= cut]
        if not sub:
            continue
        wins = sum(1 for p in sub if p > 0)
        gp = sum(p for p in sub if p > 0); gl = -sum(p for p in sub if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[P] = {"trades": len(sub), "win_rate": wins / len(sub) * 100, "pf": pf, "total_pnl": sum(sub)}
    return _calc_bt(pr)[0] if pr else 0


def _scan_symbol(sym, name, strats):
    """[(bt, pnl_long, pnl_short_ctx, oos_ok)] を返す。ショートが寄りで未約定の日のみ。"""
    out = []
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return out, 0, 0
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return out, 0, 0
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return out, 0, 0
    if df_raw is None or df_raw.empty:
        return out, 0, 0
    _bt_window = max(args.days, max(_BT_PERIODS))
    n_sig = n_nofill = 0
    for strat in strats:
        mod = mod_for(strat)
        params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
        if not params:
            continue
        cf = params[0]
        try:
            df_ind = cf(df_raw.copy())
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.sm, args.tm, args.days + 420, strat,
                                       entry_type="stop_sell", max_hold=0)
        except Exception:
            continue
        if not r:
            continue
        recs, series = [], []   # recs=(fd, pnl_long, pnl_short_ctx, in_win, oos_ok)
        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            olp = float(t.get("order_limit", 0) or 0); osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if olp <= 0 or osp <= 0 or otp <= 0 or olp < args.min_price or olp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=_bt_window):
                continue
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            trig, stop_p, tgt_p = _prices(olp, osp, otp)
            # 日足OHLC(寄り欠落・引けギャップ補正用)
            d_open = d_low = d_high = d_close = None
            try:
                drow = df_raw.loc[df_raw.index.normalize() == pd.Timestamp(fd)]
                if len(drow):
                    d_open = float(drow["open"].iloc[0]); d_low = float(drow["low"].iloc[0])
                    d_high = float(drow["high"].iloc[0]); d_close = float(drow["close"].iloc[0])
            except Exception:
                pass
            n_sig += 1
            # BT算出用(全シグナルのショート baseline pnl、スリップ無し)。エンジンと同じ経路。
            _ef = short_entry_fill_5m(db, trig, False, entry_gap_limit=_GAP,
                                      day_open=d_open, day_low=d_low, day_high=d_high)
            if _ef is not None:
                _ge = abs(_ef - trig) > 1e-9
                _xp, _rs, _et, _ = short_exit_5m(db, trig, stop_p, tgt_p, False,
                                                 include_entry_bar=_ge, day_low=d_low,
                                                 day_high=d_high, day_close=d_close,
                                                 stop_delay_bars=1)
                if _rs not in ("no_5m", "no_entry") and _et is not None:
                    series.append((fd, short_pnl(_ef, _xp, _rs, QTY, FEE, 0.0)))

            # --- #6 判定: 寄り(bar0)でショートが約定したか ---
            o0 = float(db["open"].iloc[0]); l0 = float(db["low"].iloc[0])
            short_filled_bar0 = (l0 <= trig)
            gap_low = (o0 < trig * (1.0 - _GAP))    # ギャップダウン過大=弱い=対象外
            if short_filled_bar0 or gap_low:
                continue
            # 寄りで未約定 → 9:05(2本目始値)で成行ロング → 大引け(公式終値優先)で決済
            n_nofill += 1
            l_entry = float(db["open"].iloc[1])
            l_close = float(d_close) if (d_close and d_close > 0) else float(db["close"].iloc[-1])
            pnl_long = long_pnl(l_entry, l_close, "close", QTY, FEE, args.long_slip)
            # 参考: 同じ日にショートを終日待って約定させた場合(compare用)
            pnl_short_ctx = None
            if args.compare_short and _ef is not None and _rs not in ("no_5m", "no_entry"):
                pnl_short_ctx = short_pnl(_ef, _xp, _rs, QTY, FEE, 0.005 if _rs == "stop" else 0.0)
            in_win = pd.Timestamp(fd) >= TODAY - pd.Timedelta(days=args.days)
            oos_ok = True
            if args.oos:
                lbe = _OOS_BASES.get((sym.split(".")[0], strat))
                oos_ok = (lbe is not None) and (pd.Timestamp(fd) > lbe)
            recs.append((fd, pnl_long, pnl_short_ctx, in_win, oos_ok))

        # 各未約定ロング取引に BT を付与(asof=先読みなし / 既定=今日基準・ペア単位)
        ser = sorted(series, key=lambda x: pd.Timestamp(x[0]))
        pair_bt = _bt_from_series(ser) if not args.asof_bt else None
        for (fd, pnl_long, pnl_short_ctx, in_win, oos_ok) in recs:
            if not in_win or (args.oos and not oos_ok):
                continue
            bt = _bt_from_series(ser, asof=fd) if args.asof_bt else pair_bt
            out.append((bt, pnl_long, pnl_short_ctx, oos_ok))
    return out, n_sig, n_nofill


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return (n, sum(pnls), sum(pnls) / n, len(wins) / n * 100, pf)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def _print_table(title, by_band):
    print(f"\n=== {title} ===")
    print(f"{'BT帯':>8}{'件数':>7}{'合計損益':>13}{'1件平均':>10}{'勝率':>8}{'PF':>7}")
    cum30, cum40, allp = [], [], []
    for lo, hi, lab in _BT_BANDS:
        pnls = by_band.get(lab, [])
        allp += pnls
        if lo >= 30:
            cum30 += pnls
        if lo >= 40:
            cum40 += pnls
        s = _stat(pnls)
        if s:
            n, tot, per, wr, pf = s
            print(f"{lab:>8}{n:>7}{tot:>+13,.0f}{per:>+10,.0f}{wr:>7.1f}%{_pf(pf):>7}")
        else:
            print(f"{lab:>8}{0:>7}{'—':>13}{'—':>10}{'—':>8}{'—':>7}")
    for lab, pnls in (("BT≥30", cum30), ("BT≥40", cum40), ("全体", allp)):
        s = _stat(pnls)
        if s:
            n, tot, per, wr, pf = s
            print(f"{lab:>8}{n:>7}{tot:>+13,.0f}{per:>+10,.0f}{wr:>7.1f}%{_pf(pf):>7}")


def main():
    if args.oos:
        global _OOS_BASES
        _OOS_BASES = _load_oos_bases(args.oos_proposal)
    sel = _load_selected()
    if args.limit > 0:
        sel = sel[:args.limit]
    # (sym,name)->strats
    by_sym: dict = defaultdict(lambda: [None, []])
    for code, nm, strat in sel:
        sym = code if "." in code else f"{code}.T"
        by_sym[sym][0] = nm
        by_sym[sym][1].append(strat)

    long_by_band: dict = defaultdict(list)
    short_by_band: dict = defaultdict(list)
    tot_sig = tot_nofill = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_symbol, sym, nm_st[0] or "", nm_st[1]): sym
                for sym, nm_st in by_sym.items()}
        done = 0
        for fu in as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  ...{done}/{len(futs)} 銘柄", flush=True)
            try:
                rows, n_sig, n_nf = fu.result()
            except Exception:
                continue
            tot_sig += n_sig; tot_nofill += n_nf
            for (bt, pnl_long, pnl_short_ctx, oos_ok) in rows:
                lab = _bt_band(bt)
                long_by_band[lab].append(pnl_long)
                if pnl_short_ctx is not None:
                    short_by_band[lab].append(pnl_short_ctx)

    print("\n" + "=" * 60)
    print(f"母集団: 全シグナル {tot_sig} 件 / うち寄りでショート未約定(=ロング対象) {tot_nofill} 件"
          f" ({tot_nofill/tot_sig*100:.1f}%)" if tot_sig else "シグナルなし")
    print(f"期間: 直近{args.days}日" + ("  | 純OOSのみ" if args.oos else "")
          + ("  | BT=先読みなし(asof)" if args.asof_bt else "  | BT=今日基準(ペア単位)"))
    _print_table("#6 ショート不約定 → 9:05ロング(同日大引け決済)", long_by_band)
    if args.compare_short:
        _print_table("参考: 同じ未約定日にショートを終日待った場合", short_by_band)
    print("\n注意: ロングは9:05成行→大引け決済の単純版(損切/利確なし)。"
          "実運用のスリッページ・約定ズレは未反映(--long-slip で保守側に振れる)。")


if __name__ == "__main__":
    main()
