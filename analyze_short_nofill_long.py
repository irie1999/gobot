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
  python analyze_short_nofill_long.py --oos --asof-bt --days 400 --sweep          # 損切×利確 一括探索
  python analyze_short_nofill_long.py --oos --asof-bt --days 400 --short --sweep  # #6b: 9:05成行ショート

#6(ロング)は却下確定(全損切/利確組でPF<1)。#6b は --short で「未約定→9:05成行ショート」を検証
(フェードを空売りで拾う。買いの完全ミラー)。--short 時は (A)(B)(C)・スイープすべてショート方向。

出力: BT帯別に「9:05ロング/ショート(同日決済)」の 件数/合計/勝率/PF/1件あたり。
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
ap.add_argument("--long-stop-pct", type=float, default=0.0,
                help="9:05ロングの損切り幅(例0.02=-2%%)。0=ATRミラー(ショートのatr*smを反転)を使う")
ap.add_argument("--long-target-pct", type=float, default=0.0,
                help="9:05ロングの利確幅(例0.03=+3%%)。0=ATRミラー(ショートのatr*tmを反転)を使う")
ap.add_argument("--long-stop-delay-bars", type=int, default=1,
                help="delay1: 約定足(9:05足)から何本は損切りを効かせないか(既定1=ショートと同じ)。0=即損切り")
ap.add_argument("--long-max-gap", type=float, default=0.0,
                help="上ギャップ過大の見送り(ショートの下-3%%ガードの反転)。例0.03=9:05が前日終値+3%%超なら見送り。0=無効")
ap.add_argument("--sweep", action="store_true",
                help="損切%%×利確%% のグリッドを一括検証(9:05)。どの損切/利確が最適か・そもそも成立するかを一度に見る")
ap.add_argument("--short", action="store_true",
                help="#6b: ロングでなく『未約定→9:05成行ショート』を検証(買いの完全ミラー)。フェードを空売りで拾う")
ap.add_argument("--oos", action="store_true",
                help="純OOSのみ: 各ペアの選定基準月(SOURCE_BASES)より後の日だけ集計")
ap.add_argument("--oos-proposal", type=str, default="lss_proposal_cumul.py")
ap.add_argument("--asof-bt", action="store_true",
                help="BTを各取引時点までのデータだけで算出(先読みなし=真OOS)。既定は今日基準BT(ペア単位)")
args = ap.parse_args()

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick, floor_to_tick, round_to_tick, tick_size
from check_signals_stop import PERIODS as _BT_PERIODS, calc_recommend_score as _calc_bt
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m, short_pnl, long_pnl

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._INTRADAY_5M = False
QTY = ble.FIXED_QTY
FEE = ble.FEE_PCT_ONE_WAY
_GAP = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
TODAY = pd.Timestamp.now().normalize()
_DIR = "ショート" if args.short else "ロング"      # 表示用の方向ワード
_BUYSELL = "売り→買い" if args.short else "買い→売り"

# BT帯
_BT_BANDS = [(0, 30, "0-29"), (30, 40, "30-39"), (40, 50, "40-49"),
             (50, 60, "50-59"), (60, 200, "60+")]


def _bt_band(bt: float) -> str:
    for lo, hi, lab in _BT_BANDS:
        if lo <= bt < hi:
            return lab
    return "60+"


# --sweep 用: 損切%%×利確%% グリッド(9:05ロング)。フェード母集団で成立する組があるか探索。
_SWEEP_STOPS = [0.005, 0.008, 0.012, 0.02]
_SWEEP_TGTS = [0.005, 0.008, 0.012, 0.02, 0.03]
_SWEEP_COMBOS = [(s, t) for s in _SWEEP_STOPS for t in _SWEEP_TGTS]


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
    """[(bt, pnl_prev, pnl_open, pnl_0905, oos_ok)] を返す。ショートが寄りで未約定の日のみ。"""
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
        recs, series = [], []   # recs=(fd, pnl_prev, pnl_open, pnl_0905, in_win, oos_ok)
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
            # 寄りで未約定 → ロング。エントリー時点を3つ試算して比較する:
            #  (A) 前日終値→大引け : あなたの直感「未約定株は結局プラス」の直接検証(理論値)
            #  (B) 寄り(9:00)→大引け: 寄りで成行ロング(実際に買える最速)
            #  (C) 9:05→大引け     : 2本目始値で成行ロング(#6の元案)
            # (A)(B)は日足のみ(yfinance・信頼できる)、(C)は5分足始値。決済は全て公式大引け。
            # グリッチガード: 1日で±25%超の値動き=壊れた足(データエラー)として除外(None)。
            if not (d_close and d_close > 0):
                continue    # 大引けが取れない日は判定不能
            n_nofill += 1
            base = float(round_to_tick(olp))          # 前日終値(注文基準)
            e0905 = float(db["open"].iloc[1])          # 9:05=2本目始値
            # (A)(B) は日足close-only(信頼度高・ベンチマーク)
            pnl_prev = _guarded_pnl(base, d_close)
            pnl_open = _guarded_pnl(d_open, d_close)
            # (C) 9:05ロング: 損切/利確付き(--long-stop-pct/--long-target-pct)。
            #     未指定なら引けまで=タイムカットのみ。決済理由(target/stop/close)も記録。
            pnl_0905 = None; r_0905 = None
            _e_ok = (e0905 > 0 and (not d_open or d_open <= 0
                                    or abs(e0905 / d_open - 1.0) <= 0.25))   # 5分足始値グリッチ除外
            # ギャップ過大の見送り(ロングのみ: 上に走りすぎたチェイス回避。ショートは対象外)
            if (not args.short) and args.long_max_gap > 0 and base > 0 and (e0905 / base - 1.0) > args.long_max_gap:
                _e_ok = False
            if _e_ok:
                # 損切/利確の"距離"を決める。既定=ATRミラー(ショートのstop/target距離=atr*sm/atr*tm)。
                #   ショートの order: stop=base+atr*sm(=osp) / target=base-atr*tm(=otp)
                #   → sdist=osp-base(=atr*sm), tdist=base-otp(=atr*tm)。方向は _st_prices が処理。
                if args.long_stop_pct > 0 or args.long_target_pct > 0:
                    _sdist = e0905 * args.long_stop_pct if args.long_stop_pct > 0 else 0.0
                    _tdist = e0905 * args.long_target_pct if args.long_target_pct > 0 else 0.0
                else:
                    _sdist = osp - base    # atr*sm
                    _tdist = base - otp    # atr*tm
                _sp, _tp = _st_prices(e0905, _sdist, _tdist)
                _xp_l, r_0905 = _exit_st(db, 1, _sp, _tp, d_close, args.long_stop_delay_bars)
                if _xp_l > 0:
                    pnl_0905 = _dir_pnl(e0905, _xp_l, r_0905)
            # --sweep: 損切%×利確% グリッドの各組で 9:05(ロング/ショート)の損益を試算(entry=e0905 固定)
            sweep_pnls = None
            if args.sweep and _e_ok:
                sweep_pnls = []
                for (_s, _t) in _SWEEP_COMBOS:
                    _csp, _ctp = _st_prices(e0905, e0905 * _s, e0905 * _t)
                    _cxp, _crs = _exit_st(db, 1, _csp, _ctp, d_close, args.long_stop_delay_bars)
                    sweep_pnls.append(_dir_pnl(e0905, _cxp, _crs) if _cxp > 0 else None)
            in_win = pd.Timestamp(fd) >= TODAY - pd.Timedelta(days=args.days)
            oos_ok = True
            if args.oos:
                lbe = _OOS_BASES.get((sym.split(".")[0], strat))
                oos_ok = (lbe is not None) and (pd.Timestamp(fd) > lbe)
            recs.append((fd, pnl_prev, pnl_open, pnl_0905, r_0905, sweep_pnls, in_win, oos_ok))

        # 各未約定ロング取引に BT を付与(asof=先読みなし / 既定=今日基準・ペア単位)
        ser = sorted(series, key=lambda x: pd.Timestamp(x[0]))
        pair_bt = _bt_from_series(ser) if not args.asof_bt else None
        for (fd, pnl_prev, pnl_open, pnl_0905, r_0905, sweep_pnls, in_win, oos_ok) in recs:
            if not in_win or (args.oos and not oos_ok):
                continue
            bt = _bt_from_series(ser, asof=fd) if args.asof_bt else pair_bt
            if args.sweep:
                out.append((bt, sweep_pnls, oos_ok))
            else:
                out.append((bt, pnl_prev, pnl_open, pnl_0905, r_0905, oos_ok))
    return out, n_sig, n_nofill


def _long_exit_st(db, start_idx, sp, tp, d_close, stop_delay_bars):
    """9:05ロングの同日決済(利確/損切り/タイムカット)を5分足 first-touch で。
    sp=損切り価格(下) / tp=利確価格(上)。同バーは損切り優先(悲観・ショートと同じ)。
    delay1: 約定足(start_idx)から stop_delay_bars 本は損切りを効かせない(ショートと同じ)。利確は常時。
    sp/tp とも None なら引けまで=タイムカットのみ。
    Returns: (exit_price, reason∈{target,stop,close})。"""
    cl = float(d_close) if (d_close and d_close > 0) else float(db["close"].iloc[-1])
    if sp is None and tp is None:
        return cl, "close"
    highs = db["high"].to_numpy(dtype=float)
    lows = db["low"].to_numpy(dtype=float)
    stop_start = int(start_idx) + max(0, int(stop_delay_bars))
    for j in range(int(start_idx), len(highs)):
        if sp is not None and j >= stop_start and lows[j] <= sp:   # 下抜け=損切(遅延後・同バー優先)
            return sp, "stop"
        if tp is not None and highs[j] >= tp:                      # 上抜け=利確(遅延の影響なし)
            return tp, "target"
    return cl, "close"                                              # 引けまで未達=タイムカット


def _short_exit_st(db, start_idx, sp, tp, d_close, stop_delay_bars):
    """9:05ショートの同日決済。sp=損切り価格(上) / tp=利確価格(下)。同バーは損切り優先(悲観)。
    delay: 約定足から stop_delay_bars 本は損切り無効。利確は常時。Returns (exit_price, reason)。"""
    cl = float(d_close) if (d_close and d_close > 0) else float(db["close"].iloc[-1])
    if sp is None and tp is None:
        return cl, "close"
    highs = db["high"].to_numpy(dtype=float)
    lows = db["low"].to_numpy(dtype=float)
    stop_start = int(start_idx) + max(0, int(stop_delay_bars))
    for j in range(int(start_idx), len(highs)):
        if sp is not None and j >= stop_start and highs[j] >= sp:   # 上抜け=損切(遅延後・同バー優先)
            return sp, "stop"
        if tp is not None and lows[j] <= tp:                        # 下抜け=利確
            return tp, "target"
    return cl, "close"


def _st_prices(entry, sdist, tdist):
    """損切り距離 sdist / 利確距離 tdist(ともに>0の円)から、方向に応じた(損切価格, 利確価格)。
    ロング: 損切=下(floor)/利確=上(floor)。ショート: 損切=上(ceil)/利確=下(ceil)。"""
    if args.short:
        sp = float(ceil_to_tick(entry + sdist)) if sdist > 0 else None
        tp = float(ceil_to_tick(entry - tdist)) if tdist > 0 else None
    else:
        sp = float(floor_to_tick(entry - sdist)) if sdist > 0 else None
        tp = float(floor_to_tick(entry + tdist)) if tdist > 0 else None
    return sp, tp


def _exit_st(db, start_idx, sp, tp, d_close, delay):
    return (_short_exit_st if args.short else _long_exit_st)(db, start_idx, sp, tp, d_close, delay)


def _dir_pnl(entry, exit_, reason):
    fn = short_pnl if args.short else long_pnl
    return fn(entry, exit_, reason, QTY, FEE, args.long_slip)


def _guarded_pnl(entry, exit_):
    """成行→大引け決済の損益(円)。ロング=買い→売り / ショート=売り→買い。
    データエラー(1日±25%超/非正値)は None で除外。"""
    if entry is None or exit_ is None or entry <= 0 or exit_ <= 0:
        return None
    if abs(exit_ / entry - 1.0) > 0.25:
        return None
    fee = (entry + exit_) * QTY * FEE
    if args.short:
        return (entry - exit_) * QTY - fee    # 売り→買い(ショート)
    return (exit_ - entry) * QTY - fee         # 買い→売り(ロング)


def _stat(pnls):
    pnls = [p for p in pnls if p is not None]
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

    # ── --sweep: 損切%×利確% グリッドを一括検証(9:05ロング) ──────────────────────
    if args.sweep:
        _sw30 = [[] for _ in _SWEEP_COMBOS]   # combo -> pnl list (BT≥30)
        _sw40 = [[] for _ in _SWEEP_COMBOS]   # combo -> pnl list (BT≥40)
        _sig = _nf = 0
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
                _sig += n_sig; _nf += n_nf
                for (bt, sweep_pnls, oos_ok) in rows:
                    if not sweep_pnls:
                        continue
                    for ci, p in enumerate(sweep_pnls):
                        if p is None:
                            continue
                        if bt >= 30:
                            _sw30[ci].append(p)
                        if bt >= 40:
                            _sw40[ci].append(p)
        print("\n" + "=" * 60)
        print(f"母集団: 全シグナル {_sig} 件 / 寄りで未約定 {_nf} 件"
              + (f" ({_nf/_sig*100:.1f}%)" if _sig else ""))
        print(f"期間: 直近{args.days}日" + ("  | 純OOSのみ" if args.oos else "")
              + ("  | BT=先読みなし(asof)" if args.asof_bt else "  | BT=今日基準")
              + f"  | delay{args.long_stop_delay_bars}"
              + (f"  | 上ギャップ{args.long_max_gap*100:.0f}%見送り" if args.long_max_gap > 0 else ""))
        for _lab, _sw in (("BT≥30", _sw30), ("BT≥40", _sw40)):
            print(f"\n=== 損切×利確 スイープ [{_lab}] (9:05{_DIR}・PF降順) ===")
            print(f"{'損切%':>7}{'利確%':>7}{'件数':>7}{'合計損益':>13}{'1件平均':>10}{'勝率':>8}{'PF':>7}")
            _tbl = []
            for ci, (s, t) in enumerate(_SWEEP_COMBOS):
                st = _stat(_sw[ci])
                if st:
                    _tbl.append((s, t, *st))
            for (s, t, n, tot, per, wr, pf) in sorted(_tbl, key=lambda x: -(x[6] if x[6] != float("inf") else 9e9)):
                print(f"{s*100:>6.1f}%{t*100:>6.1f}%{n:>7}{tot:>+13,.0f}{per:>+10,.0f}{wr:>7.1f}%{_pf(pf):>7}")
        print(f"\n読み方: {_DIR}。PF>1.2の組が件数十分＆隣接組も揃えば有望。全組<1.2なら不成立。")
        print("       一部の組だけプラスでも、件数が少なければ過学習(たまたま)を疑うこと。")
        return

    prev_by_band: dict = defaultdict(list)   # (A) 前日終値→大引け
    open_by_band: dict = defaultdict(list)   # (B) 寄り(9:00)→大引け
    n0905_by_band: dict = defaultdict(list)  # (C) 9:05→大引け(損切/利確付き)
    reason_mix: dict = defaultdict(list)     # (C) BT≥30 の決済理由内訳
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
            for (bt, pnl_prev, pnl_open, pnl_0905, r_0905, oos_ok) in rows:
                lab = _bt_band(bt)
                prev_by_band[lab].append(pnl_prev)
                open_by_band[lab].append(pnl_open)
                n0905_by_band[lab].append(pnl_0905)
                if bt >= 30 and pnl_0905 is not None and r_0905:
                    reason_mix[r_0905].append(pnl_0905)

    print("\n" + "=" * 60)
    print(f"母集団: 全シグナル {tot_sig} 件 / うち寄りでショート未約定(=ロング対象) {tot_nofill} 件"
          f" ({tot_nofill/tot_sig*100:.1f}%)" if tot_sig else "シグナルなし")
    print(f"期間: 直近{args.days}日" + ("  | 純OOSのみ" if args.oos else "")
          + ("  | BT=先読みなし(asof)" if args.asof_bt else "  | BT=今日基準(ペア単位)"))
    print(f"※ 全て『{_BUYSELL}(同日大引け決済)』。±25%超の異常足は除外済み。方向={_DIR}")
    if args.long_stop_pct > 0 or args.long_target_pct > 0:
        _st_lab = f"損切-{args.long_stop_pct*100:.1f}% / 利確+{args.long_target_pct*100:.1f}%"
    else:
        _st_lab = f"ATRミラー(sm={args.sm}/tm={args.tm}を反転)"
    _st_lab += f" / delay{args.long_stop_delay_bars}"
    if args.long_max_gap > 0:
        _st_lab += f" / 上ギャップ{args.long_max_gap*100:.0f}%見送り"
    _print_table(f"(A) 前日終値→大引け [{_DIR}・理論値]", prev_by_band)
    _print_table(f"(B) 寄り(9:00)→大引け [{_DIR}・実際に建てられる最速]", open_by_band)
    _print_table(f"(C) 9:05{_DIR} [{_st_lab}]", n0905_by_band)
    # (C) の決済理由内訳(BT≥30)
    print(f"\n--- (C) 9:05{_DIR} BT≥30 決済理由の内訳 [{_st_lab}] ---")
    print(f"{'理由':>8}{'件数':>7}{'合計損益':>13}{'1件平均':>10}")
    _rmix_all = []
    for _rk, _ja in (("target", "目標達成"), ("stop", "損切り"), ("close", "タイムカット")):
        _v = reason_mix.get(_rk, [])
        _rmix_all += _v
        if _v:
            print(f"{_ja:>8}{len(_v):>7}{sum(_v):>+13,.0f}{sum(_v)/len(_v):>+10,.0f}")
        else:
            print(f"{_ja:>8}{0:>7}{'—':>13}{'—':>10}")
    if _rmix_all:
        print(f"{'合計':>8}{len(_rmix_all):>7}{sum(_rmix_all):>+13,.0f}"
              f"{sum(_rmix_all)/len(_rmix_all):>+10,.0f}")
    print("\n読み方: (A)がプラスで(C)がマイナスなら『寄りで急騰→フェード』型=買うなら寄り(B)。")
    print("       損切/利確を入れると(C)の内訳(目標達成/損切り/タイムカット)が分かれる。")
    print("       (A)(B)は日足ベースで信頼度高。(C)は5分足ベース(±25%グリッチ・5分始値乖離を除外)。")
    print("注意: 実スリッページ・成行の入口ズレは概算。SystemErrorが出たら --workers 1 で再実行。")


if __name__ == "__main__":
    main()
