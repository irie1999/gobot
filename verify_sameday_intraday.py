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
args = ap.parse_args()

if not (args.mirror or args.lss or args.both):
    args.both = True   # 何も指定なければ両方

# ── バックテストエンジン(唯一の約定ロジック) ────────────────────────────────
import backtest_limit_entry as ble
from daytrade_data import load_intraday, split_by_day

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY if args.fee is None else args.fee


# ── 銘柄リスト読み込み ───────────────────────────────────────────────────────
def _mod_for(strat: str):
    """戦略名→シグナルモジュール(ロング側のみ。mirror/lss はロング銘柄が対象)。"""
    import check_signals_stop as _stop
    import check_signals_breakout as _brk
    if strat in getattr(_brk, "STRATEGY_PARAMS", {}):
        return _brk
    return _stop


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


# ── 5分足 first-touch 決済(ショート) ────────────────────────────────────────
def _short_exit_5m(day_bars: pd.DataFrame, entry_p: float,
                   stop_p: float, target_p: float, is_rise_trigger: bool):
    """約定日の5分足からショートの決済価格・理由を first-touch で求める。

    Args:
      day_bars       : その約定日の5分足(open/high/low/close, 昇順)
      entry_p        : 約定価格(=order_p)。エントリーバーの特定に使う
      stop_p         : ショートの損切(上側)
      target_p       : ショートの利確(下側)
      is_rise_trigger: True=価格が上昇してentry_pに到達で約定(mirror・指値空売り)
                       False=価格が下落してentry_pに到達で約定(lss・逆指値空売り)

    Returns: (exit_price, reason)  reason ∈ {"target","stop","close","no_entry"}
      約定バーが見つからなければ ("", "no_entry")。
    """
    if day_bars is None or day_bars.empty:
        return None, "no_5m"
    highs = day_bars["high"].to_numpy(dtype=float)
    lows  = day_bars["low"].to_numpy(dtype=float)
    closes = day_bars["close"].to_numpy(dtype=float)
    n = len(highs)

    # 1) 約定バーを特定(トリガー到達で約定)
    ei = None
    for j in range(n):
        if is_rise_trigger:
            if highs[j] >= entry_p:   # 上昇して指値売りに到達
                ei = j
                break
        else:
            if lows[j] <= entry_p:    # 下落して逆指値売りに到達
                ei = j
                break
    if ei is None:
        return None, "no_entry"

    # 2) 約定バーの次バー以降で first-touch(約定前ヒットの先読みを避ける)
    for j in range(ei + 1, n):
        hit_stop = highs[j] >= stop_p      # 損切り(上抜け)= 同一バー両方でも優先
        hit_tgt  = lows[j] <= target_p     # 利確(下抜け)
        if hit_stop:
            return stop_p, "stop"
        if hit_tgt:
            return target_p, "target"
    # 3) どちらも当たらなければ引け(その日の最終バー終値)
    return float(closes[-1]), "close"


def _short_exit_daily(hi: float, lo: float, cl: float, entry_p: float,
                      stop_p: float, target_p: float, is_rise_trigger: bool,
                      tie: str = "stop"):
    """日足近似の決済(5分足との比較用)。日足の high/low だけでは損切と利確の
    どちらが先か分からないので、tie で優先を切替える:
      tie="stop"   : 同時タッチは損切り優先(保守=悲観側の下限)
      tie="target" : 同時タッチは利確優先(=日足エンジンの同日決済と同じ楽観側の上限)
    Returns: (exit_price, reason) reason∈{"target","stop","close","no_entry"}"""
    if is_rise_trigger:
        if hi < entry_p:
            return None, "no_entry"
    else:
        if lo > entry_p:
            return None, "no_entry"
    hit_stop = hi >= stop_p     # 上抜け=損切
    hit_tgt  = lo <= target_p   # 下抜け=利確
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


def _short_pnl(entry_p: float, exit_p: float, reason: str) -> float:
    """ショート損益(円)。買い戻し(exit)は損切り時のみ不利スリッページを乗せる。
    エントリーは指値/約定価格ちょうど(ミラーの幻スリッページを排除)。"""
    exit_eff = exit_p * (1.0 + args.slip) if reason == "stop" else exit_p
    fee = (entry_p + exit_eff) * QTY * FEE_ONE_WAY
    return (entry_p - exit_eff) * QTY - fee


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
            oxp, oreason = _short_exit_daily(_hi, _lo, _cl, lp, stop_p, target_p,
                                             is_rise_trigger, tie="target")
            cxp, creason = _short_exit_daily(_hi, _lo, _cl, lp, stop_p, target_p,
                                             is_rise_trigger, tie="stop")
            if oreason != "no_entry":
                pnl_opt = _short_pnl(lp, oxp, oreason)
            if creason != "no_entry":
                pnl_cons = _short_pnl(lp, cxp, creason)

        day_bars = by_day.get(fill_d)
        if day_bars is None or len(day_bars) < 2:
            res["no_5m"] += 1
            continue
        exit_p, reason = _short_exit_5m(day_bars, lp, stop_p, target_p,
                                        is_rise_trigger)
        if reason == "no_5m":
            res["no_5m"] += 1
            continue
        if reason == "no_entry":
            res["no_entry"] += 1
            continue
        pnl_5m = _short_pnl(lp, exit_p, reason)
        res["trades"].append({
            "sym": sym, "strat": strat, "date": str(fill_d),
            "entry": lp, "exit": exit_p, "reason": reason,
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
        return

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


def main():
    symbols = _load_symbols()
    # 重複(code,strategy)排除
    seen = set(); uniq = []
    for c, n, s in symbols:
        if (c, s) not in seen:
            seen.add((c, s)); uniq.append((c, n, s))
    print(f"[info] 対象 {len(uniq)}ペア / 5分足ソース={args.source} / 株数={QTY}")

    modes = []
    if args.both or (args.mirror and args.lss):
        modes = ["mirror", "lss"]
    elif args.mirror:
        modes = ["mirror"]
    elif args.lss:
        modes = ["lss"]
    for m in modes:
        _run_mode(m, uniq)

    print("注意: 5分足は約定バーの次バー以降で first-touch 判定(約定前ヒットの先読み回避)。")
    print("      同一バーで損切・利確が両方タッチした場合は保守的に損切り優先で計上。")


if __name__ == "__main__":
    main()
