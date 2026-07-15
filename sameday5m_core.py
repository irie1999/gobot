"""sameday5m_core.py — 同日決済(日計り)を5分足で正確に検証する共通コア。

verify_sameday_intraday.py(スタンドアロンCLI)と nikkei_analysis.py(HTMLレポートの
5分足TP/SLタブ)の両方から使う。first-touch のような「バグの温床」を1箇所に集約して
二重実装を避ける。

設計(バグ回避の要点):
  * 約定判定は既存 run_limit_backtest(唯一の約定ロジック)に委譲。5分足で作り直すのは
    「決済価格・決済理由」だけ。
  * ショートの損切=エントリーより上 / 利確=下。日足エンジンが返す order_stop/order_target
    の上側=損切・下側=利確 とだけ解釈(mirror/lss の符号反転を個別実装しない)。
  * first-touch は約定バーの次バー以降のみ(約定前ヒットの先読み回避)。同時タッチは
    保守的に損切り優先。
  * エントリーは注文価格(order_limit)ちょうどで計上(ミラーの幻スリッページ排除)。
  * 5分足が無い日は勝手に日足へフォールバックしない(集計から除外)。
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

import backtest_limit_entry as ble
from daytrade_data import load_intraday, split_by_day


def mod_for(strat: str):
    """戦略名→シグナルモジュール(ロング側。mirror/lss はロング銘柄が対象)。"""
    import check_signals_stop as _stop
    import check_signals_breakout as _brk
    if strat in getattr(_brk, "STRATEGY_PARAMS", {}):
        return _brk
    return _stop


def short_exit_5m(day_bars: pd.DataFrame, entry_p: float,
                  stop_p: float, target_p: float, is_rise_trigger: bool):
    """約定日の5分足からショートの決済(価格・理由・時刻)を first-touch で求める。

    Returns: (exit_price, reason, entry_ts, exit_ts)
      reason ∈ {"target","stop","close","no_entry","no_5m"}
    """
    if day_bars is None or day_bars.empty:
        return None, "no_5m", None, None
    highs = day_bars["high"].to_numpy(dtype=float)
    lows = day_bars["low"].to_numpy(dtype=float)
    closes = day_bars["close"].to_numpy(dtype=float)
    times = day_bars.index
    n = len(highs)

    # 1) 約定バー(トリガー到達)
    ei = None
    for j in range(n):
        if is_rise_trigger:
            if highs[j] >= entry_p:   # 上昇して指値売りに到達(mirror)
                ei = j
                break
        else:
            if lows[j] <= entry_p:    # 下落して逆指値売りに到達(lss)
                ei = j
                break
    if ei is None:
        return None, "no_entry", None, None

    ent_ts = times[ei]
    # 2) 約定バーの次バー以降で first-touch(約定前ヒットの先読み回避)
    for j in range(ei + 1, n):
        if highs[j] >= stop_p:        # 上抜け=損切(同時タッチも優先)
            return stop_p, "stop", ent_ts, times[j]
        if lows[j] <= target_p:       # 下抜け=利確
            return target_p, "target", ent_ts, times[j]
    # 3) どちらも当たらなければ引け
    return float(closes[-1]), "close", ent_ts, times[-1]


def short_exit_daily(hi: float, lo: float, cl: float, entry_p: float,
                     stop_p: float, target_p: float, is_rise_trigger: bool,
                     tie: str = "stop"):
    """日足近似の決済(5分足との比較用)。tie="stop"=保守(下限)/"target"=楽観(上限)。"""
    if is_rise_trigger:
        if hi < entry_p:
            return None, "no_entry"
    else:
        if lo > entry_p:
            return None, "no_entry"
    hit_stop = hi >= stop_p
    hit_tgt = lo <= target_p
    if tie == "target":
        if hit_tgt:
            return target_p, "target"
        if hit_stop:
            return stop_p, "stop"
    else:
        if hit_stop:
            return stop_p, "stop"
        if hit_tgt:
            return target_p, "target"
    return cl, "close"


def short_pnl(entry_p: float, exit_p: float, reason: str,
              qty: int, fee_one_way: float, slip: float) -> float:
    """ショート損益(円)。買い戻し(exit)は損切り時のみ不利スリッページ。
    エントリーは注文価格ちょうど(ミラーの幻スリッページ排除)。"""
    exit_eff = exit_p * (1.0 + slip) if reason == "stop" else exit_p
    fee = (entry_p + exit_eff) * qty * fee_one_way
    return (entry_p - exit_eff) * qty - fee


def _collect_fills(sym, name, strat, is_mirror, sm, tm, days,
                   df_ind, ident_fn, since):
    """指定 sm/tm で日足バックテストし、約定(決済済み)の一覧を返す。
    各要素: (fill_date, lp, stop_p, target_p)。lp=注文価格 / stop=上側 / target=下側。"""
    et = "stop" if is_mirror else "stop_sell"
    try:
        r = ble.run_limit_backtest(sym, name, df_ind, ident_fn, 0.0, sm, tm,
                                   days + 420, strat, entry_type=et, max_hold=0)
    except Exception:
        return []
    if not r:
        return []
    out = []
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
        fd = edt.date() if hasattr(edt, "date") else edt
        out.append((fd, lp, max(osp, otp), min(osp, otp)))
    return out


def prepare_symbol(sym, name, strat, days, source):
    """1銘柄の日足(指標計算済み)と5分足(日別分割)を用意。重い5分足ロードは1回だけ。
    Returns: (df_raw, df_ind, ident_fn, by_day, em) または None(データ無し)。"""
    mod = mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return None
    cf, em, _sm, _tm = params
    try:
        df_raw = ble.fetch(sym, days + 420)
    except Exception:
        return None
    if df_raw is None or df_raw.empty:
        return None
    try:
        df_ind = cf(df_raw.copy())
    except Exception:
        return None
    try:
        m5 = load_intraday(sym, days=days + 5, source=source)
    except Exception:
        m5 = None
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    return df_raw, df_ind, (lambda d: d), by_day, em


def sweep_symbol(sym, name, strat, is_mirror, cells, days, source,
                 min_price, max_price, qty, fee_one_way, slip):
    """1銘柄について grid の各(sm,tm)で5分足決済損益リストを返す。
    5分足・指標は1回だけ用意して各マスで使い回す。"""
    out = {c: [] for c in cells}
    prep = prepare_symbol(sym, name, strat, days, source)
    if prep is None:
        return out
    df_raw, df_ind, ident_fn, by_day, em = prep
    if not by_day:
        return out
    is_rise = is_mirror
    since = ble._TODAY - timedelta(days=days)
    for (sm, tm) in cells:
        for fd, lp, stop_p, target_p in _collect_fills(
                sym, name, strat, is_mirror, sm, tm, days, df_ind, ident_fn, since):
            if lp < min_price or lp > max_price:
                continue
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            xp, reason, _e, _x = short_exit_5m(db, lp, stop_p, target_p, is_rise)
            if reason in ("no_5m", "no_entry"):
                continue
            out[(sm, tm)].append(short_pnl(lp, xp, reason, qty, fee_one_way, slip))
    return out


def verify_symbol(sym, name, strat, is_mirror, sm, tm, days, source,
                  min_price, max_price, qty, fee_one_way, slip):
    """1銘柄・単一(sm,tm)で5分足の取引明細(dict list)を返す。詳細表示用。"""
    res = {"trades": [], "no_5m": 0, "no_entry": 0}
    prep = prepare_symbol(sym, name, strat, days, source)
    if prep is None:
        return res
    df_raw, df_ind, ident_fn, by_day, em = prep
    is_rise = is_mirror
    since = ble._TODAY - timedelta(days=days)
    daily_rows = {d.date(): df_raw.loc[d] for d in df_raw.index}
    for fd, lp, stop_p, target_p in _collect_fills(
            sym, name, strat, is_mirror, sm, tm, days, df_ind, ident_fn, since):
        if lp < min_price or lp > max_price:
            continue
        # 日足の楽観/保守バウンド(同じコストモデル)
        pnl_opt = pnl_cons = None
        drow = daily_rows.get(fd)
        if drow is not None:
            _hi, _lo, _cl = float(drow["high"]), float(drow["low"]), float(drow["close"])
            oxp, ore = short_exit_daily(_hi, _lo, _cl, lp, stop_p, target_p, is_rise, "target")
            cxp, cre = short_exit_daily(_hi, _lo, _cl, lp, stop_p, target_p, is_rise, "stop")
            if ore != "no_entry":
                pnl_opt = short_pnl(lp, oxp, ore, qty, fee_one_way, slip)
            if cre != "no_entry":
                pnl_cons = short_pnl(lp, cxp, cre, qty, fee_one_way, slip)
        db = by_day.get(fd)
        if db is None or len(db) < 2:
            res["no_5m"] += 1
            continue
        xp, reason, ent_ts, ext_ts = short_exit_5m(db, lp, stop_p, target_p, is_rise)
        if reason == "no_5m":
            res["no_5m"] += 1
            continue
        if reason == "no_entry":
            res["no_entry"] += 1
            continue
        pnl = short_pnl(lp, xp, reason, qty, fee_one_way, slip)
        res["trades"].append({
            "sym": sym, "name": name, "strat": strat, "date": str(fd),
            "entry": lp, "exit": xp, "reason": reason,
            "entry_ts": ent_ts, "exit_ts": ext_ts,
            "stop_p": stop_p, "target_p": target_p,
            "pnl_5m": pnl,
            "pnl_opt": pnl_opt if pnl_opt is not None else pnl,
            "pnl_cons": pnl_cons if pnl_cons is not None else pnl,
        })
    return res


def cell_stat(pnls: list):
    """損益リスト→{n,pnl,pf,wr}。空なら None。"""
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "pnl": sum(pnls), "pf": pf, "wr": len(wins) / n * 100}
