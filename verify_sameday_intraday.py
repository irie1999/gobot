"""verify_sameday_intraday.py — 同日決済(日計り)を 5分足で正確に再検証。

日足バックテストは「その日に TP/SL のどちらが先に当たったか」を日足の高値/安値だけで
判定するため近似になる(特に 0.3ATR のような狭い幅では両方が1日の値幅に入りやすい)。
本ツールは、日足バックテストで確定した「エントリー(約定日・約定値・損切/利確価格)」を
そのまま使い、**決済だけを 5分足のザラ場で first-touch 判定**して正確な同日損益を出す。

対応: ロングミラー(指値空売り) / ロング銘柄ショート(逆指値空売り) の両方。

設計(バグ回避の要点):
  * シグナル検出・約定判定は既存の run_limit_backtest に完全に任せる(再実装しない)。
    → 唯一の約定ロジックと一致するので乖離が出ない。
  * 5分足で作り直すのは「決済価格と決済理由」だけ。
  * ショートの損切=エントリーより上 / 利確=下。日足エンジンが返す order_stop/order_target
    の **上側=損切・下側=利確** とだけ解釈する(mirror/lss の符号反転を個別実装しない)。
  * first-touch は既存 backtest_intraday_ranking._sim_tpsl_exit と同じ規則:
    同一バーで両方タッチは保守的に「損切り優先」。約定バー自身は決済に使わない
    (約定した後の値動きだけを見る=約定前ヒットの先読みを防ぐ)。
  * 5分足が無い日は「5分足なし」として集計から除外(勝手に日足へフォールバックしない)。

使い方:
  python verify_sameday_intraday.py --both --sm 0.3 --tm 0.3
  python verify_sameday_intraday.py --mirror --sm 0.3 --tm 0.3 --days 180
  python verify_sameday_intraday.py --lss --sm 0.5 --tm 0.3 --source local
  python verify_sameday_intraday.py --both --symbols-file holdout_selected_symbols.py
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── 引数 ─────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser(description="同日決済を5分足で正確に再検証(mirror/lss)")
ap.add_argument("--mirror", action="store_true", help="ロングミラー(指値空売り)を検証")
ap.add_argument("--lss", action="store_true", help="ロング銘柄ショート(逆指値空売り)を検証")
ap.add_argument("--both", action="store_true", help="mirror と lss の両方を検証")
ap.add_argument("--sm", type=float, default=0.3, help="損切ATR倍率(sm)。日足スイープの最適値を入れる")
ap.add_argument("--tm", type=float, default=0.3, help="利確ATR倍率(tm)")
ap.add_argument("--days", type=int, default=180, help="検証期間(日)。シグナルをこの範囲に絞る")
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto",
                help="5分足のソース(local=stock_5min のみ / auto=local→yfinance)")
ap.add_argument("--symbols-file", type=str, default=None,
                help="(code,name,strategy) の SELECTED を持つ .py。既定は holdout_selected_symbols.py→WATCHLIST")
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--qty", type=int, default=None, help="株数(既定=FIXED_QTY=100)")
ap.add_argument("--slip", type=float, default=0.0,
                help="損切り買い戻しの不利スリッページ(例0.005=0.5%%)。既定0=摩擦なし。"
                     "※エントリーは指値/約定価格ちょうどで計上(ミラーの幻の利益を防ぐ)")
ap.add_argument("--fee", type=float, default=None,
                help="片道手数料率(既定=FEE_PCT_ONE_WAY)。0で手数料なし")
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--no-html", action="store_true",
                help="HTML(5分足ベースの取引明細)を生成しない(コンソール集計のみ)")
ap.add_argument("--no-browser", action="store_true", help="生成HTMLを自動で開かない")
ap.add_argument("--sweep", action="store_true",
                help="5分足ベースで (損切ATR × 利確ATR) を総当りし最適な同日幅を探す")
ap.add_argument("--sm-list", type=str, default="0.1,0.15,0.2,0.3,0.5,0.75,1.0",
                help="スイープの損切ATR倍率(カンマ区切り)")
ap.add_argument("--tm-list", type=str, default="0.1,0.15,0.2,0.3,0.5,0.75,1.0",
                help="スイープの利確ATR倍率(カンマ区切り)")
ap.add_argument("--by-regime", action="store_true",
                help="日経レジーム(上げ/横ばい/下げ)別・月別に損益を分解する(B: レジーム頑健性)。"
                     "--days を大きく(例730)して複数レジームで検証する用途。")
ap.add_argument("--capital-cap", type=float, default=0.0,
                help="予算(円)。指定すると『その資金の範囲で実際に拾える取引だけ』の実現損益を"
                     "5分足の約定/決済時刻でザラ場の同時保有を判定して算出。資金切れ時は"
                     "後から来たシグナルをスキップ(時刻順=先着優先)。例 4500000")
args = ap.parse_args()

if not (args.mirror or args.lss or args.both):
    args.both = True   # 何も指定なければ両方

# ── バックテストエンジン(唯一の約定ロジック) ────────────────────────────────
import backtest_limit_entry as ble
from daytrade_data import load_intraday, split_by_day
import sameday5m_core as _c5   # first-touch 等の共通コア(二重実装を避ける)

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY if args.fee is None else args.fee

# first-touch / 損益計算は共通コアに委譲(バグの温床を1箇所に集約)
_mod_for = _c5.mod_for


def _short_pnl(entry_p, exit_p, reason):
    return _c5.short_pnl(entry_p, exit_p, reason, QTY, FEE_ONE_WAY, args.slip)


def _load_symbols() -> list[tuple]:
    """(code, name, strategy) の一覧。--symbols-file → holdout_selected_symbols.py → WATCHLIST。"""
    cand = []
    if args.symbols_file:
        cand.append(args.symbols_file)
    cand += ["holdout_selected_symbols.py"]
    for path in cand:
        p = Path(path)
        if not p.exists():
            continue
        ns: dict = {}
        try:
            exec(p.read_text(encoding="utf-8"), ns)
        except Exception as e:
            print(f"[warn] {path} 読み込み失敗: {e}", file=sys.stderr)
            continue
        sel = ns.get("SELECTED")
        if sel:
            out = [(c, n, s) for (c, n, s) in sel]
            print(f"[info] {path} から {len(out)}ペア読み込み")
            return out
    # フォールバック: WATCHLIST
    import check_signals_stop as _stop
    import check_signals_breakout as _brk
    out = list(_stop.WATCHLIST) + list(_brk.WATCHLIST)
    print(f"[info] WATCHLIST から {len(out)}ペア(フォールバック)")
    return out


# ── 1銘柄の検証 ──────────────────────────────────────────────────────────────
def _verify_one(sym: str, name: str, strat: str, is_rise_trigger: bool) -> dict:
    """日足バックテストで約定を確定 → 各約定を5分足で決済し直す。"""
    res = {"trades": [], "no_5m": 0, "no_entry": 0}
    mod = _mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return res
    cf, em, _sm, _tm = params
    try:
        df = ble.fetch(sym, args.days + 420)   # スコア窓ぶん余分に取る
    except Exception:
        return res
    if df is None or df.empty:
        return res
    et = getattr(mod, "ENTRY_TYPE", "stop")
    try:
        r = ble.run_limit_backtest(sym, name, df, cf, em, _sm, _tm,
                                   args.days + 420, strat, entry_type=et)
    except Exception:
        return res
    if not r:
        return res

    since = ble._TODAY - timedelta(days=args.days)
    # 約定日(entry_dt)ぶんの5分足を1回だけロード
    fill_dates = set()
    trades = []
    for t in r.get("trade_log", []):
        if t.get("reason") in ("発注中", "保有中"):
            continue
        edt = t.get("entry_dt")
        sdt = t.get("signal_dt")
        if edt is None or sdt is None:
            continue
        sd = sdt.date() if hasattr(sdt, "date") else sdt
        if sd < since:
            continue
        ep = float(t.get("entry_p", 0) or 0)
        if ep <= 0 or ep < args.min_price or ep > args.max_price:
            continue
        trades.append(t)
        fill_dates.add(edt.date() if hasattr(edt, "date") else edt)
    if not trades:
        return res

    # 5分足(全期間)を1回ロードして日別分割(銘柄あたり1回に抑える)
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        m5 = None
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}

    # 約定日ぶんの日足行(日足近似の比較用)を辞書化
    daily_rows = {d.date(): df.loc[d] for d in df.index}

    for t in trades:
        edt = t.get("entry_dt")
        fill_d = edt.date() if hasattr(edt, "date") else edt
        # エントリーは「注文価格(order_limit=lp)ちょうど」で計上する。
        # 日足エンジンの entry_p はロング側スリッページ込み → ミラーだと売値が上がって
        # 幻の利益になるため使わない(§ phantom slippage)。
        lp = float(t.get("order_limit", 0) or 0)
        osp = float(t.get("order_stop", 0) or 0)
        otp = float(t.get("order_target", 0) or 0)
        if lp <= 0 or osp <= 0 or otp <= 0:
            continue
        # ショート: 上側=損切 / 下側=利確 (mirror/lss とも同じ解釈で符号反転を排除)
        stop_p   = max(osp, otp)
        target_p = min(osp, otp)

        # 日足近似の上下限(同じコストモデル)。5分足の正確値はこの間に入るはず。
        drow = daily_rows.get(fill_d)
        pnl_opt = pnl_cons = None
        if drow is not None:
            _hi, _lo, _cl = float(drow["high"]), float(drow["low"]), float(drow["close"])
            oxp, oreason = _c5.short_exit_daily(_hi, _lo, _cl, lp, stop_p, target_p,
                                             is_rise_trigger, tie="target")
            cxp, creason = _c5.short_exit_daily(_hi, _lo, _cl, lp, stop_p, target_p,
                                             is_rise_trigger, tie="stop")
            if oreason != "no_entry":
                pnl_opt = _short_pnl(lp, oxp, oreason)
            if creason != "no_entry":
                pnl_cons = _short_pnl(lp, cxp, creason)

        day_bars = by_day.get(fill_d)
        if day_bars is None or len(day_bars) < 2:
            res["no_5m"] += 1
            continue
        exit_p, reason, ent_ts, ext_ts = _c5.short_exit_5m(
            day_bars, lp, stop_p, target_p, is_rise_trigger)
        if reason == "no_5m":
            res["no_5m"] += 1
            continue
        if reason == "no_entry":
            res["no_entry"] += 1
            continue
        pnl_5m = _short_pnl(lp, exit_p, reason)
        res["trades"].append({
            "sym": sym, "name": name, "strat": strat, "date": str(fill_d),
            "entry": lp, "exit": exit_p, "reason": reason,
            "entry_ts": ent_ts, "exit_ts": ext_ts,
            "stop_p": stop_p, "target_p": target_p,
            "pnl_5m": pnl_5m,
            "pnl_opt":  pnl_opt if pnl_opt is not None else pnl_5m,
            "pnl_cons": pnl_cons if pnl_cons is not None else pnl_5m,
        })
    return res


# ── 集計 & 表示 ──────────────────────────────────────────────────────────────
def _run_mode(mode: str, symbols: list[tuple]):
    is_mirror = (mode == "mirror")
    # エンジンのモードフラグを設定(同日決済 + 損切/利確幅の適用)
    ble._MIRROR_PNL = is_mirror
    ble._ENTRY_TYPE_FORCE = None if is_mirror else "stop_sell"
    ble._MAX_HOLD_FORCE = 0
    ble._SM_FORCE = args.sm
    ble._TM_FORCE = args.tm
    is_rise_trigger = is_mirror   # mirror=指値空売り(上昇で約定) / lss=逆指値空売り(下落で約定)

    label = "ロングミラー(指値空売り)" if is_mirror else "ロング銘柄ショート(逆指値空売り)"
    print("=" * 72)
    print(f"■ {label}  同日決済 5分足検証  sm={args.sm} / tm={args.tm} / 直近{args.days}日")
    print("=" * 72)

    all_trades = []
    no_5m = no_entry = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_verify_one, c, n, s, is_rise_trigger): (c, s)
                for (c, n, s) in symbols}
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception:
                continue
            all_trades += r["trades"]
            no_5m += r["no_5m"]
            no_entry += r["no_entry"]

    if not all_trades:
        print("  検証できる取引がありません(5分足データが無い可能性)。")
        print(f"  (5分足なし {no_5m}件 / 約定不成立 {no_entry}件)")
        return {"label": label, "trades": [], "no_5m": no_5m, "no_entry": no_entry}

    def _agg(trades, key):
        pnls = [t[key] for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        return n, sum(pnls), (len(wins) / n * 100 if n else 0), pf

    n, tot5, wr5, pf5 = _agg(all_trades, "pnl_5m")
    _, topt, wropt, pfopt = _agg(all_trades, "pnl_opt")
    _, tcons, wrcons, pfcons = _agg(all_trades, "pnl_cons")
    reasons = {"target": 0, "stop": 0, "close": 0}
    for t in all_trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    def _pf(x):
        return "∞" if x == float("inf") else f"{x:.2f}"

    _cost = "摩擦なし" if (args.slip == 0 and FEE_ONE_WAY == 0) else \
        f"損切スリップ{args.slip*100:.2f}% / 手数料片道{FEE_ONE_WAY*100:.2f}%"
    print(f"  検証取引数        : {n}件  (5分足なし {no_5m}件 / 約定不成立 {no_entry}件は除外) / {_cost}")
    print(f"  ── 日足・楽観(利確優先): 損益 {topt:+,.0f}円 / 勝率 {wropt:.1f}% / PF {_pf(pfopt)}  ← 上限")
    print(f"  ★ 5分足(正確・実際順) : 損益 {tot5:+,.0f}円 / 勝率 {wr5:.1f}% / PF {_pf(pf5)}")
    print(f"  ── 日足・保守(損切優先): 損益 {tcons:+,.0f}円 / 勝率 {wrcons:.1f}% / PF {_pf(pfcons)}  ← 下限")
    print(f"  決済理由(5分足)   : 利確 {reasons.get('target',0)} / "
          f"損切 {reasons.get('stop',0)} / 引け {reasons.get('close',0)}")
    _span = topt - tcons
    if _span > 0:
        _pos = (tot5 - tcons) / _span * 100
        print(f"  → 日足の楽観↔保守の幅 {_span:,.0f}円。実際(5分足)は下限から {_pos:.0f}% の位置。"
              f"({'楽観寄り=利確が先に来やすい' if _pos>=50 else '保守寄り=損切が先に来やすい'})")
    print()
    return {
        "label": label, "trades": all_trades, "no_5m": no_5m, "no_entry": no_entry,
        "n": n, "tot5": tot5, "wr5": wr5, "pf5": pf5,
        "topt": topt, "tcons": tcons,
        "reasons": reasons, "cost": _cost,
    }


# ── 5分足ベースのスイープ ────────────────────────────────────────────────────
def _sweep_one(sym: str, name: str, strat: str, is_mirror: bool,
               cells: list) -> dict:
    """1銘柄について、grid の各(sm,tm)で5分足決済の損益リストを返す。
    エントリーは sm/tm 非依存(前日終値到達で約定)なので 5分足は1回だけロード。
    指標計算も1回だけ(precompute)して各マスで使い回す。"""
    out = {c: [] for c in cells}
    mod = _mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return out
    cf, em, _sm, _tm = params
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return out
    if df_raw is None or df_raw.empty:
        return out
    try:
        df_ind = cf(df_raw.copy())    # 指標を1回だけ計算 → 各マスは素通しで使い回す
    except Exception:
        return out
    _ident = lambda d: d
    et = "stop" if is_mirror else "stop_sell"
    is_rise = is_mirror

    # 5分足を1回だけロード(この銘柄の最も重い処理)
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        m5 = None
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return out   # 5分足が無ければ検証不能

    daily_rows = {d.date(): df_raw.loc[d] for d in df_raw.index}
    since = ble._TODAY - timedelta(days=args.days)

    for (sm, tm) in cells:
        try:
            r = ble.run_limit_backtest(sym, name, df_ind, _ident, em, sm, tm,
                                       args.days + 420, strat,
                                       entry_type=et, max_hold=0)
        except Exception:
            continue
        if not r:
            continue
        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            sdt = t.get("signal_dt"); edt = t.get("entry_dt")
            if sdt is None or edt is None:
                continue
            sd = sdt.date() if hasattr(sdt, "date") else sdt
            if sd < since:
                continue
            lp = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if lp <= 0 or osp <= 0 or otp <= 0:
                continue
            if lp < args.min_price or lp > args.max_price:
                continue
            stop_p = max(osp, otp); target_p = min(osp, otp)
            fill_d = edt.date() if hasattr(edt, "date") else edt
            db = by_day.get(fill_d)
            if db is None or len(db) < 2:
                continue
            xp, reason, _e, _x = _c5.short_exit_5m(db, lp, stop_p, target_p, is_rise)
            if reason in ("no_5m", "no_entry"):
                continue
            out[(sm, tm)].append(_short_pnl(lp, xp, reason))
    return out


def _run_sweep(mode: str, symbols: list[tuple], sm_list, tm_list) -> dict:
    is_mirror = (mode == "mirror")
    # スイープはパラメータを run_limit_backtest に直接渡すので、グローバル上書きは無効化
    ble._MIRROR_PNL = False
    ble._ENTRY_TYPE_FORCE = None
    ble._MAX_HOLD_FORCE = None
    ble._SM_FORCE = None
    ble._TM_FORCE = None

    cells = [(sm, tm) for tm in tm_list for sm in sm_list]
    label = "ロングミラー(指値空売り)" if is_mirror else "ロング銘柄ショート(逆指値空売り)"
    print("=" * 72)
    print(f"■ {label}  5分足スイープ  損切ATR{sm_list} × 利確ATR{tm_list} / 直近{args.days}日")
    print("=" * 72)

    agg = {c: [] for c in cells}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_sweep_one, c, n, s, is_mirror, cells): c
                for (c, n, s) in symbols}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 25 == 0:
                print(f"  ...{done}/{len(symbols)}銘柄", flush=True)
            try:
                r = fut.result()
            except Exception:
                continue
            for c, pnls in r.items():
                agg[c] += pnls

    def _cell_stat(pnls):
        n = len(pnls)
        if n == 0:
            return None
        wins = [p for p in pnls if p > 0]
        gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        return {"n": n, "pnl": sum(pnls), "pf": pf, "wr": len(wins) / n * 100}

    stats = {c: _cell_stat(agg[c]) for c in cells}
    best = None
    for c in cells:
        st = stats[c]
        if st and (best is None or st["pnl"] > stats[best]["pnl"]):
            best = c

    def _pf(x):
        return "∞" if x == float("inf") else f"{x:.2f}"

    # コンソール: グリッド(行=sm, 列=tm)
    hdr = "損切＼利確".ljust(10) + "".join(f"tm{tm}".rjust(13) for tm in tm_list)
    print("\n" + hdr)
    print("-" * len(hdr))
    for sm in sm_list:
        row = f"sm{sm}".ljust(10)
        for tm in tm_list:
            st = stats[(sm, tm)]
            if not st:
                row += "—".rjust(13)
            else:
                mark = "*" if best == (sm, tm) else " "
                row += f"{st['pnl']:+,.0f}{mark}".rjust(13)
        print(row)
    print("-" * len(hdr))
    if best:
        b = stats[best]
        print(f"★ 最良(5分足): 損切ATR={best[0]} / 利確ATR={best[1]} → "
              f"{b['pnl']:+,.0f}円 (PF{_pf(b['pf'])} / 勝率{b['wr']:.0f}% / {b['n']}取引)")
    print()
    return {"mode": mode, "label": label, "sm_list": sm_list, "tm_list": tm_list,
            "stats": stats, "best": best}


def _build_sweep_html(results: list) -> str:
    def _pf(x):
        return "∞" if x == float("inf") else f"{x:.2f}"
    blocks = ""
    for R in results:
        sm_list, tm_list, stats, best = R["sm_list"], R["tm_list"], R["stats"], R["best"]
        th = 'padding:6px 10px;text-align:center;color:#94a3b8;font-size:0.75rem'
        head = f'<th style="{th};text-align:left">損切ATR＼利確ATR</th>' + "".join(
            f'<th style="{th}">tm={tm}</th>' for tm in tm_list)
        body = ""
        for sm in sm_list:
            cells = f'<td style="{th};text-align:left;color:#e2e8f0;font-weight:700">sm={sm}</td>'
            for tm in tm_list:
                st = stats.get((sm, tm))
                if not st:
                    cells += f'<td style="{th}">—</td>'; continue
                is_best = (best == (sm, tm))
                pc = "#4ade80" if st["pnl"] > 0 else "#f87171" if st["pnl"] < 0 else "#94a3b8"
                bg = "background:#0e3320;" if is_best else ("background:#101826;" if st["pnl"] > 0 else "")
                star = " ★" if is_best else ""
                cells += (f'<td style="padding:5px 8px;text-align:center;{bg}">'
                          f'<div style="color:{pc};font-weight:700">{st["pnl"]:+,.0f}{star}</div>'
                          f'<div style="color:#64748b;font-size:0.66rem">PF{_pf(st["pf"])}/{st["wr"]:.0f}%</div></td>')
            body += f"<tr>{cells}</tr>"
        bline = ""
        if best:
            b = stats[best]
            bline = (f'<p style="margin:8px 0;color:#c4b5fd">★ 最良(5分足): '
                     f'<b>損切ATR={best[0]} / 利確ATR={best[1]}</b> → '
                     f'<b style="color:{"#4ade80" if b["pnl"]>=0 else "#f87171"}">{b["pnl"]:+,.0f}円</b> '
                     f'(PF{_pf(b["pf"])} / 勝率{b["wr"]:.0f}% / {b["n"]}取引)</p>')
        blocks += (f'<div style="margin:16px 0;padding:16px 20px;background:#1e293b;'
                   f'border-radius:8px;border-left:3px solid #a78bfa">'
                   f'<h3 style="color:#c4b5fd;margin:0 0 6px">🎯 {R["label"]} — 5分足スイープ</h3>'
                   f'{bline}'
                   f'<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.82rem">'
                   f'<thead><tr style="border-bottom:1px solid #334155">{head}</tr></thead>'
                   f'<tbody>{body}</tbody></table></div></div>')
    return (f'<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">'
            f'<title>5分足 同日TP/SLスイープ sm/tm 総当り</title>'
            f'<style>body{{background:#0f172a;color:#e2e8f0;font-family:sans-serif;padding:20px}}'
            f'table{{background:#0f172a}}</style></head><body>'
            f'<h1 style="color:#a78bfa">🕔 5分足ベース 同日TP/SL 最適化スイープ</h1>'
            f'<p style="color:#94a3b8">直近{args.days}日 / 5分足の実際の値動き順で first-touch 判定'
            f'(約定前ヒットの先読み回避・同時タッチは損切優先)。'
            f'エントリーは注文価格ちょうど(ミラーの幻スリッページ排除)。'
            f'{"損切スリップ"+format(args.slip*100,".2f")+"%" if args.slip else "摩擦なし(スリッページ0)"}。</p>'
            f'{blocks}</body></html>')


def _build_detail_html(results: list) -> str:
    """単一sm/tmモード: 5分足ベースの取引明細(決済日降順)をHTML化。"""
    def _ts(x):
        try:
            return pd.Timestamp(x).strftime("%m/%d %H:%M")
        except Exception:
            return "—"
    def _pf(x):
        return "∞" if x == float("inf") else f"{x:.2f}"
    blocks = ""
    for R in results:
        if not R or not R.get("trades"):
            continue
        trs = sorted(R["trades"], key=lambda t: (t["date"], str(t.get("exit_ts"))), reverse=True)
        rmap = {"target": ("利確", "#4ade80"), "stop": ("損切", "#f87171"),
                "close": ("引け", "#94a3b8")}
        rows = ""
        for t in trs:
            rlbl, rcol = rmap.get(t["reason"], (t["reason"], "#94a3b8"))
            pnl = t["pnl_5m"]; pc = "#4ade80" if pnl >= 0 else "#f87171"
            rows += (
                f'<tr>'
                f'<td>{t["date"]}</td>'
                f'<td style="text-align:left">{t["sym"]}<br>'
                f'<span style="color:#64748b;font-size:0.7rem">{t.get("name","")}</span></td>'
                f'<td>{t["strat"]}</td>'
                f'<td style="color:#94a3b8">{_ts(t.get("entry_ts"))}</td>'
                f'<td style="text-align:right">{t["entry"]:,.0f}</td>'
                f'<td style="text-align:right;color:#f87171">{t["stop_p"]:,.0f}</td>'
                f'<td style="text-align:right;color:#4ade80">{t["target_p"]:,.0f}</td>'
                f'<td style="color:#94a3b8">{_ts(t.get("exit_ts"))}</td>'
                f'<td style="text-align:right">{t["exit"]:,.0f}</td>'
                f'<td style="color:{rcol};font-weight:700">{rlbl}</td>'
                f'<td style="text-align:right;color:{pc};font-weight:700">{pnl:+,.0f}</td>'
                f'</tr>')
        tot = R.get("tot5", 0); n = R.get("n", len(trs)); wr = R.get("wr5", 0); pf = R.get("pf5", 0)
        rc = R.get("reasons", {})
        summary = (f'<p style="font-size:0.95rem">決済 <b>{n}件</b> / 勝率 '
                   f'<b>{wr:.1f}%</b> / PF <b>{_pf(pf)}</b> / 損益 '
                   f'<b style="color:{"#4ade80" if tot>=0 else "#f87171"}">{tot:+,.0f}円</b> '
                   f'&nbsp;<span style="color:#64748b">利確{rc.get("target",0)}/'
                   f'損切{rc.get("stop",0)}/引け{rc.get("close",0)} '
                   f'・5分足なし{R.get("no_5m",0)}/約定不成立{R.get("no_entry",0)}</span></p>')
        blocks += (
            f'<div style="margin:18px 0">'
            f'<h2 style="color:#c4b5fd">🕔 {R["label"]} — 5分足ベース取引明細</h2>'
            f'{summary}'
            f'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:0.8rem">'
            f'<thead><tr style="border-bottom:1px solid #334155;color:#94a3b8">'
            f'<th>決済日</th><th style="text-align:left">銘柄</th><th>戦略</th>'
            f'<th>約定時刻</th><th>約定値</th><th style="color:#f87171">損切</th>'
            f'<th style="color:#4ade80">利確</th><th>決済時刻</th><th>決済値</th>'
            f'<th>理由</th><th>損益</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></div>')
    _cost = "摩擦なし" if (args.slip == 0 and FEE_ONE_WAY == 0) else \
        f"損切スリップ{args.slip*100:.2f}% / 手数料片道{FEE_ONE_WAY*100:.2f}%"
    return (f'<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">'
            f'<title>5分足 同日取引明細 sm{args.sm}/tm{args.tm}</title>'
            f'<style>body{{background:#0f172a;color:#e2e8f0;font-family:sans-serif;padding:20px}}'
            f'td,th{{padding:5px 8px;border-bottom:1px solid #1e293b;text-align:center}}</style></head><body>'
            f'<h1 style="color:#a78bfa">🕔 5分足ベース 同日取引明細 (損切ATR={args.sm} / 利確ATR={args.tm})</h1>'
            f'<p style="color:#94a3b8">直近{args.days}日 / 実際の値動き順で first-touch 判定・'
            f'エントリーは注文価格ちょうど・{_cost}</p>{blocks}</body></html>')


# ── B: 日経レジーム別・月別 分解 ─────────────────────────────────────────────
def _load_regime_map(days: int):
    """日経のトレンドラベル (date→'up'/'sideways'/'down') を返す。取得失敗時 None。
    label_trend/fetch_n225 は nikkei_analysis の既存実装を再利用(二重実装を避ける)。"""
    try:
        from nikkei_analysis import fetch_n225, label_trend
        years = max(1, (days + 400) // 365 + 1)
        close = fetch_n225(years)
        trend = label_trend(close)
        trend.index = pd.to_datetime(trend.index).tz_localize(None).normalize()
        return trend.sort_index()
    except Exception as e:
        print(f"[warn] 日経レジーム取得失敗 ({e}) → レジーム分解をスキップ", file=sys.stderr)
        return None


def _agg_pnls(pnls: list):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "pnl": sum(pnls), "wr": len(wins) / n * 100, "pf": pf}


def _print_group(title: str, groups: dict, order=None):
    def _pf(x):
        return "∞" if x == float("inf") else f"{x:.2f}"
    print(f"  【{title}】")
    keys = order if order else sorted(groups.keys())
    for k in keys:
        st = groups.get(k)
        if not st:
            continue
        print(f"    {str(k):<14}{st['n']:>5}件  勝率{st['wr']:>4.0f}%  "
              f"PF{_pf(st['pf']):>6}  {st['pnl']:>+13,.0f}円  (平均{st['pnl']/st['n']:>+8,.0f})")


def _capital_capped(trades: list, cap: float):
    """予算cap円の範囲で実際に拾える取引だけの実現損益を、ザラ場の同時保有で判定。
    各取引は entry_ts で建て exit_ts で解放。エントリー時に deployed+約定額 > cap なら
    そのシグナルはスキップ(=時刻順で先着優先)。同日でも時刻でザラ場の重なりを正確に見る。"""
    from collections import defaultdict
    by_day: dict = defaultdict(list)
    for t in trades:
        by_day[t["date"]].append(t)
    cap_pnl = 0.0
    cap_n = skip_n = 0
    for _d, ts in by_day.items():
        evts = []
        for t in ts:
            cost = float(t["entry"]) * QTY
            evts.append((str(t.get("entry_ts")), 0, cost, id(t), t))   # 0=建て
            evts.append((str(t.get("exit_ts")),  1, cost, id(t), t))   # 1=解放
        evts.sort(key=lambda e: (e[0], e[1]))
        deployed = 0.0
        accepted: set = set()
        for _tm, order, cost, tid, t in evts:
            if order == 0:      # 建て
                if deployed + cost <= cap:
                    deployed += cost
                    accepted.add(tid)
                    cap_pnl += float(t["pnl_5m"])
                    cap_n += 1
                else:
                    skip_n += 1
            else:               # 解放(建てた分のみ)
                if tid in accepted:
                    deployed -= cost
    return cap_pnl, cap_n, skip_n


def _print_capital_cap(results: list, cap: float):
    def _pf(x):
        return "∞" if x == float("inf") else f"{x:.2f}"
    for R in results:
        trs = (R or {}).get("trades") or []
        if not trs:
            continue
        full_pnl = sum(float(t["pnl_5m"]) for t in trs)
        full_n = len(trs)
        cap_pnl, cap_n, skip_n = _capital_capped(trs, cap)
        rate = cap_pnl / full_pnl * 100 if full_pnl else 0.0
        print("─" * 72)
        print(f"■ {R['label']} — 予算キャップ試算 (資金 ¥{cap:,.0f} / 株数{QTY})")
        print(f"  全取引        : {full_n:>5}件  実現損益 {full_pnl:>+13,.0f}円 (資金無制限)")
        print(f"  ¥{cap:,.0f}以内: {cap_n:>5}件  実現損益 {cap_pnl:>+13,.0f}円  "
              f"(資金切れでスキップ {skip_n}件)")
        print(f"  → 予算内で全体の {rate:.0f}% の損益を捕捉"
              + ("(ほぼ取りこぼしなし)" if rate >= 90 else
                 f"(残り{100-rate:.0f}%はピーク日の資金切れで取りこぼし)"))
        print()


def _regime_breakdown(results: list, regime):
    """各モードの all_trades を 日経レジーム別 / 月別 に分解して表示。"""
    _JA = {"up": "🟢上げ", "sideways": "🟡横ばい", "down": "🔴下げ", "?": "―不明"}
    for R in results:
        trs = (R or {}).get("trades") or []
        if not trs:
            continue
        print("─" * 72)
        print(f"■ {R['label']} — レジーム別 / 月別 (5分足・{len(trs)}取引)")
        by_reg: dict = {}
        by_mon: dict = {}
        for t in trs:
            d = pd.Timestamp(t["date"]).normalize()
            lbl = "?"
            if regime is not None:
                try:
                    v = regime.asof(d)          # その日以前の直近ラベル(祝日/週末対策)
                    if isinstance(v, str):
                        lbl = v
                except Exception:
                    lbl = "?"
            by_reg.setdefault(lbl, []).append(t["pnl_5m"])
            by_mon.setdefault(d.strftime("%Y-%m"), []).append(t["pnl_5m"])
        reg_named = {_JA.get(k, k): _agg_pnls(v) for k, v in by_reg.items()}
        mon_stat = {k: _agg_pnls(v) for k, v in by_mon.items()}
        _print_group("日経レジーム別", reg_named,
                     order=["🟢上げ", "🟡横ばい", "🔴下げ", "―不明"])
        _print_group("月別", mon_stat, order=sorted(mon_stat.keys()))
        # 頑健性の一言判定: 全レジームでプラスか
        pos = [k for k, st in reg_named.items() if st and st["pnl"] > 0 and k != "―不明"]
        neg = [k for k, st in reg_named.items() if st and st["pnl"] <= 0 and k != "―不明"]
        if pos and not neg:
            print(f"  → 頑健: 判定できた全レジームでプラス ({'/'.join(pos)})")
        elif neg:
            print(f"  → 注意: マイナスのレジームあり ({'/'.join(neg)})。"
                  f"特定相場依存の可能性 → 期間を延ばして再確認を")
        print()


def main():
    symbols = _load_symbols()
    seen = set(); uniq = []
    for c, n, s in symbols:
        if (c, s) not in seen:
            seen.add((c, s)); uniq.append((c, n, s))
    print(f"[info] 対象 {len(uniq)}ペア / 5分足ソース={args.source} / 株数={QTY}")

    modes = ["mirror", "lss"] if (args.both or (args.mirror and args.lss)) else \
            (["mirror"] if args.mirror else ["lss"])

    if args.sweep:
        sm_list = [float(x) for x in args.sm_list.split(",") if x.strip()]
        tm_list = [float(x) for x in args.tm_list.split(",") if x.strip()]
        results = [_run_sweep(m, uniq, sm_list, tm_list) for m in modes]
        if not args.no_html:
            _mtag = "".join(m[0] for m in modes)
            out = Path(f"sameday5m_sweep_{_mtag}_{ble._TODAY}.html")
            out.write_text(_build_sweep_html(results), encoding="utf-8")
            print(f"[HTML] 5分足スイープ → {out.resolve()}")
            if not args.no_browser:
                try:
                    from _open_html import open_html
                    open_html(out.resolve())
                except Exception:
                    pass
    else:
        results = [_run_mode(m, uniq) for m in modes]
        if args.capital_cap > 0:
            _print_capital_cap(results, args.capital_cap)
        if args.by_regime:
            regime = _load_regime_map(args.days)
            _regime_breakdown(results, regime)
        if not args.no_html:
            html = _build_detail_html([r for r in results if r])
            _mtag = "".join(m[0] for m in modes)   # mirror→m / lss→l
            out = Path(f"sameday5m_detail_{_mtag}_sm{args.sm}_tm{args.tm}_{ble._TODAY}.html")
            out.write_text(html, encoding="utf-8")
            print(f"[HTML] 5分足 取引明細 → {out.resolve()}")
            if not args.no_browser:
                try:
                    from _open_html import open_html
                    open_html(out.resolve())
                except Exception:
                    pass

    print("注意: 5分足は約定バーの次バー以降で first-touch 判定(約定前ヒットの先読み回避)。")
    print("      同一バーで損切・利確が両方タッチした場合は保守的に損切り優先で計上。")


if __name__ == "__main__":
    main()
