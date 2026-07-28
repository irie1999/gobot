"""analyze_fill_time.py — lssの「約定した時刻」別に成績を集計し、時間カットオフの是非を検証。

仮説(#9): 寄り付き付近で約定(=早く崩れた)は継続しやすく成績良、10時以降など
ザラ場後半に遅れて約定(=寄りでは崩れず後から崩れた)はだまし下げ→反発で刈られやすい。

約定時刻は5分足first-touch(short_exit_5m が返す ent_ts=約定バー時刻)から取得。
compare_lss_rules.py と同じ母集団・BTフィルタ・現実的約定モデルを使う。

使い方(あなたの機械・5分足データが必要):
  python analyze_fill_time.py --days 240 --min-price 1000 --max-price 6000 --bt-min 30
  python analyze_fill_time.py --bt-min 40 --by-month
出力: 時間帯別の 件数/勝率/PF/1取引/損切り率 + 「◯時までに約定した分だけ」の時間カットオフ表
      + compare_fill_time_<date>.csv
"""
from __future__ import annotations
import argparse
import importlib.util
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss 約定時刻別の成績と時間カットオフ検証")
ap.add_argument("--symbols-file", type=str, default=None)
ap.add_argument("--days", type=int, default=240)
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--bt-min", type=float, default=30.0, help="BTスコア下限で母集団を絞る(実投資集団)")
ap.add_argument("--stop-slip", type=float, default=0.005, help="現実モデルの損切りスリッページ")
ap.add_argument("--oos", action="store_true",
                help="純OOSのみ集計: 各ペアの選定基準月(SOURCE_BASES)より後の約定だけ使う")
ap.add_argument("--oos-proposal", type=str, default="lss_proposal_cumul.py",
                help="SOURCE_BASES を持つ提案ファイル(既定 lss_proposal_cumul.py)。--oos時に参照")
args = ap.parse_args()

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick, round_to_tick, tick_size
from check_signals_stop import PERIODS as _BT_PERIODS, calc_recommend_score as _calc_bt
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m, short_pnl

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._INTRADAY_5M = False
QTY = ble.FIXED_QTY
FEE = ble.FEE_PCT_ONE_WAY
_GAP = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
TODAY = pd.Timestamp.now().normalize()

# 時間帯バケット(分= 9:00基準の経過ではなく実時刻のHH*60+MM)
def _bucket(minute_of_day: int) -> str:
    if minute_of_day < 9 * 60 + 15:   return "1) 寄り 09:00-09:15"
    if minute_of_day < 9 * 60 + 30:   return "2) 09:15-09:30"
    if minute_of_day < 10 * 60:       return "3) 09:30-10:00"
    if minute_of_day < 10 * 60 + 30:  return "4) 10:00-10:30"
    if minute_of_day < 11 * 60 + 30:  return "5) 10:30-前引"
    if minute_of_day < 13 * 60 + 30:  return "6) 後場-13:30"
    return "7) 13:30-大引"

_BUCKETS = ["1) 寄り 09:00-09:15", "2) 09:15-09:30", "3) 09:30-10:00", "4) 10:00-10:30",
            "5) 10:30-前引", "6) 後場-13:30", "7) 13:30-大引"]
# 時間カットオフのしきい(この時刻までに約定した分だけ採用)
_CUTOFFS = [(9, 15), (9, 30), (10, 0), (10, 30), (11, 30), (15, 30)]


def _month_end(m: int) -> pd.Timestamp:
    """基準月ラベル(月番号)→月末日。累積2025-09..2026-06前提: 月>=7=2025年 / 月<=6=2026年。"""
    m = int(m)
    year = 2025 if m >= 7 else 2026
    return pd.Period(f"{year}-{m:02d}", "M").end_time.normalize()


_OOS_BASES: dict = {}   # (code_norm, strat) -> 選定最新基準月の月末(これより後がOOS)


def _load_oos_bases(path: str) -> dict:
    """提案ファイルの SOURCE_BASES から (code, strat)->最新基準月末 を作る。"""
    p = Path(path)
    if not p.exists():
        sys.exit(f"[error] --oos 用の {path} が無い。SOURCE_BASES を持つ提案ファイルを指定してください")
    ns: dict = {}
    exec(p.read_text(encoding="utf-8"), ns)
    sb = ns.get("SOURCE_BASES") or {}
    out = {}
    for k, lab in sb.items():
        months = [int(x) for x in str(lab).replace(" ", "").split("/") if x.strip().isdigit()]
        if not months:
            continue
        # キー正規化: (code, strat)。code は .T 無しに揃える。
        code = str(k[0]).upper().removesuffix(".T").split(".")[0]
        out[(code, str(k[1]))] = max(_month_end(m) for m in months)
    print(f"[OOS] {path} の SOURCE_BASES {len(out)}ペアを読込 → 各ペアの基準月より後のみ集計")
    return out


def _load_selected() -> list[tuple]:
    path = args.symbols_file
    if path is None:
        for c in ("holdout_selected_symbols.py",):
            if Path(c).exists():
                path = c
                break
    if path is None:
        props = sorted(Path(".").glob("lss_proposal_cumul.py")) \
            or sorted(Path(".").glob("lss_watchlist_proposal_*.py")) \
            or sorted(Path(".").glob("lss_proposal_*.py"))
        if props:
            path = str(props[-1])
    if path is None or not Path(path).exists():
        sys.exit("[error] 選定ファイルが見つかりません(--symbols-file で指定)")
    spec = importlib.util.spec_from_file_location("_sel", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
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


def _bt_score(trades) -> int:
    if not trades:
        return 0
    pr = {}
    for P in _BT_PERIODS:
        cut = TODAY - pd.Timedelta(days=P)
        sub = [p for (d, p) in trades if pd.Timestamp(d) >= cut]
        if not sub:
            continue
        wins = sum(1 for p in sub if p > 0)
        gp = sum(p for p in sub if p > 0); gl = -sum(p for p in sub if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[P] = {"trades": len(sub), "win_rate": wins / len(sub) * 100, "pf": pf, "total_pnl": sum(sub)}
    return _calc_bt(pr)[0]


def _scan_symbol(sym, name, strats):
    """(minute_of_day, pnl, reason) のリストを返す(BT下限を満たす戦略のみ)。"""
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
    _bt_window = max(args.days, max(_BT_PERIODS))
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
        local, bt_trades = [], []
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
            # 純OOS: このペアの選定最新基準月より後の約定だけ(時刻分析用のlocalに影響、BT算出は全期間のまま)
            _oos_ok = True
            if args.oos:
                _lbe = _OOS_BASES.get((sym.split(".")[0], strat))
                _oos_ok = (_lbe is not None) and (pd.Timestamp(fd) > _lbe)
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            trig, stop_p, tgt_p = _prices(olp, osp, otp)
            d_open = d_low = d_high = d_close = None
            try:
                drow = df_raw.loc[df_raw.index.normalize() == pd.Timestamp(fd)]
                if len(drow):
                    d_open = float(drow["open"].iloc[0]); d_low = float(drow["low"].iloc[0])
                    d_high = float(drow["high"].iloc[0]); d_close = float(drow["close"].iloc[0])
            except Exception:
                pass
            entry_fill = short_entry_fill_5m(db, trig, False, entry_gap_limit=_GAP,
                                             day_open=d_open, day_low=d_low, day_high=d_high)
            if entry_fill is None:
                continue
            gap_entry = abs(entry_fill - trig) > 1e-9
            xp, reason, ent_ts, _ext = short_exit_5m(db, trig, stop_p, tgt_p, False,
                                                     include_entry_bar=gap_entry,
                                                     day_low=d_low, day_high=d_high, day_close=d_close)
            if reason in ("no_5m", "no_entry") or ent_ts is None:
                continue
            pnl = short_pnl(entry_fill, xp, reason, QTY, FEE, args.stop_slip if reason == "stop" else 0.0)
            # BTスコア用は全期間(直近365)、時刻分析は --days 窓
            bt_trades.append((fd, short_pnl(entry_fill, xp, reason, QTY, FEE, 0.0)))
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=args.days):
                continue
            if args.oos and not _oos_ok:      # 純OOS指定時: 基準月以前(in-sample)は時刻分析から除外
                continue
            ts = pd.Timestamp(ent_ts)
            mod_day = ts.hour * 60 + ts.minute
            local.append((fd, mod_day, pnl, reason))
        if args.bt_min > 0 and _bt_score(bt_trades) < args.bt_min:
            continue
        out.extend(local)
    return out


def _stat(items):
    """items=[(pnl,reason)] → (n, total, per, wr, pf, stop率)"""
    n = len(items)
    if n == 0:
        return None
    pnls = [p for p, _ in items]
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    stops = sum(1 for _, r in items if r == "stop")
    return (n, sum(pnls), sum(pnls) / n, len(wins) / n * 100, pf, stops / n * 100)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    global _OOS_BASES
    if args.oos:
        _OOS_BASES = _load_oos_bases(args.oos_proposal)
    sel = _load_selected()
    by_sym = defaultdict(lambda: {"name": "", "strats": []})
    for code, name, strat in sel:
        d = by_sym[code]
        if strat not in d["strats"]:
            d["strats"].append(strat)
        if name and not d["name"]:
            d["name"] = name
    syms = list(by_sym.items())
    if args.limit:
        syms = syms[:args.limit]
    print(f"[分析] {len(syms)}銘柄 / 遡及{args.days}日 / 価格{args.min_price:.0f}-{args.max_price:.0f} "
          f"/ BT{args.bt_min:.0f}以上")

    all_items = []   # (date, minute_of_day, pnl, reason)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_symbol, c, v["name"], v["strats"]): c for c, v in syms}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                all_items.extend(fut.result())
            except Exception:
                pass
            if done % 50 == 0:
                print(f"  ...{done}/{len(syms)}", flush=True)

    if not all_items:
        print("[結果] 該当トレードなし(5分足在庫・しきい値を確認)")
        return

    # 時間帯別
    buckets = defaultdict(list)
    for fd, mday, pnl, reason in all_items:
        buckets[_bucket(mday)].append((pnl, reason))
    print(f"\n=== 約定時刻別(BT{args.bt_min:.0f}以上・{len(all_items)}トレード) ===")
    print(f"{'時間帯':<18}{'件数':>6}{'総損益':>12}{'1取引':>8}{'勝率':>6}{'PF':>6}{'損切率':>7}")
    for b in _BUCKETS:
        s = _stat(buckets.get(b, []))
        if not s:
            continue
        n, tot, per, wr, pf, stopr = s
        print(f"{b:<18}{n:>6}{tot:>+12,.0f}{per:>+8,.0f}{wr:>5.0f}%{_pf(pf):>6}{stopr:>6.0f}%")

    # 時間カットオフ(この時刻までに約定した分だけ採用)
    print(f"\n=== 時間カットオフ(◯時までに約定した分だけ採用) ===")
    print(f"{'締切':<10}{'件数':>6}{'総損益':>12}{'1取引':>8}{'勝率':>6}{'PF':>6}")
    for h, m in _CUTOFFS:
        lim = h * 60 + m
        items = [(p, r) for fd, md, p, r in all_items if md < lim]
        s = _stat(items)
        if not s:
            continue
        n, tot, per, wr, pf, _ = s
        print(f"{f'〜{h:02d}:{m:02d}':<10}{n:>6}{tot:>+12,.0f}{per:>+8,.0f}{wr:>5.0f}%{_pf(pf):>6}")

    # ── 月別 頑健性: 寄り(〜09:15) vs 遅い(09:15以降) が全月で一貫するか ──
    _CUT = 9 * 60 + 15
    mon = defaultdict(lambda: {"e": [], "l": []})
    for fd, mday, pnl, reason in all_items:
        ym = str(fd)[:7]
        (mon[ym]["e"] if mday < _CUT else mon[ym]["l"]).append(pnl)
    print(f"\n=== 月別: 寄り(〜09:15) vs 遅い(09:15以降) の純損益(頑健性確認) ===")
    print(f"{'月':>9}{'寄り件':>6}{'寄り損益':>12}{'遅い件':>6}{'遅い損益':>12}"
          f"{'遅い1取引':>10}{'切る効果':>11}")
    n_late_neg = n_early_pos = n_tot = 0
    for ym in sorted(mon):
        e, l = mon[ym]["e"], mon[ym]["l"]
        es, ls = sum(e), sum(l)
        lp = ls / len(l) if l else 0.0
        n_tot += 1
        n_late_neg += 1 if ls < 0 else 0            # 遅いがマイナス=切ると得
        n_early_pos += 1 if es > 0 else 0           # 寄りがプラス
        print(f"{ym:>9}{len(e):>6}{es:>+12,.0f}{len(l):>6}{ls:>+12,.0f}"
              f"{lp:>+10,.0f}{-ls:>+11,.0f}")
    print(f"  → 寄りがプラスだった月: {n_early_pos}/{n_tot} / "
          f"遅いがマイナス(=09:15カットオフが得)だった月: {n_late_neg}/{n_tot}")

    date_s = str(TODAY.date())
    rows = []
    for b in _BUCKETS:
        s = _stat(buckets.get(b, []))
        if s:
            n, tot, per, wr, pf, stopr = s
            rows.append({"bucket": b, "trades": n, "pnl": round(tot), "per": round(per),
                         "win_rate": round(wr, 1), "pf": round(pf, 2) if pf != float("inf") else "inf",
                         "stop_rate": round(stopr, 1)})
    pd.DataFrame(rows).to_csv(Path(f"compare_fill_time_{date_s}.csv"), index=False, encoding="utf-8-sig")
    print(f"\n[出力] compare_fill_time_{date_s}.csv")


if __name__ == "__main__":
    main()
