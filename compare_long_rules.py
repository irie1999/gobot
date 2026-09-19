"""compare_long_rules.py — ロングデイトレ(逆指値買い=上ブレイク・同日決済)の
損切(sm)×利確(tm) ATR幅を総当りグリッドで比較し、最適な同日TP/SL幅を見つける A/B 検証ツール。

compare_lss_rules.py(lss=ショート)の**ロング鏡像**。lss が 0.1/1.0 を見つけたのと
同じ手法で、ロング用の最適 sm/tm を実データ(5分足 first-touch)で探す。
損切り約定を 楽観(line)/現実(gap=窓埋め)/保守(+スリッページ) の3モデルで下限測定する。

なぜ必要か:
  scan_long_daytrade の既定 sm=0.1 / tm=1.0 は lss(ショート)の最適値を流用した
  プレースホルダーで、ロングでは未検証。同日ロングとショートは値動きの非対称性
  (ドリフト方向・寄りギャップ・ヒゲの深さ)が違うため、別途スイープが必要。

手順:
  1) まず scan_long_daytrade を仮の 0.1/1.0 で1回回して long_proposal / long_watchlist_proposal を作る
  2) 本ツールでその選定ペアに対し sm×tm を総当り → OOS(現実/保守)で最良セルを選ぶ
  3) その最適 sm/tm で scan_long_daytrade を回し直す(選定も sm/tm 依存のため)

使い方(あなたの機械で。5分足データが必要):
  python compare_long_rules.py --days 240 --min-price 1000 --max-price 6000
  python compare_long_rules.py --symbols-file long_watchlist_proposal_2026-07-26.py --bt-min 30
  python compare_long_rules.py --by-month                    # 月別内訳も出す
  python compare_long_rules.py --sm-grid 0.1,0.2,0.3 --tm-grid 0.5,1.0,1.5
  python compare_long_rules.py --stop-delay-bars 1           # delay1(寄1本目stopなし)版で総当り
出力: コンソール グリッド表(net現実/PF) + 上位セル詳細 + compare_long_rules_<date>.csv
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="ロングデイトレ 損切×利確(sm×tm)グリッドのnet損益A/B比較")
ap.add_argument("--symbols-file", type=str, default=None,
                help="選定ファイル(long_watchlist_proposal_*.py / long_proposal_*.py の SELECTED)。"
                     "無指定なら holdout_selected_symbols.py → long_watchlist_proposal_*.py を自動検出")
ap.add_argument("--days", type=int, default=240)
ap.add_argument("--sm-grid", type=str, default="0.1,0.15,0.2,0.3,0.5",
                help="損切ATR倍率の候補(カンマ区切り)")
ap.add_argument("--tm-grid", type=str, default="0.5,0.75,1.0,1.5,2.0",
                help="利確ATR倍率の候補(カンマ区切り)")
ap.add_argument("--ref-sm", type=float, default=1.0,
                help="ATR逆算用の基準sm(エンジンを1回だけこの値で回して order_stop から ATR を復元)")
ap.add_argument("--ref-tm", type=float, default=1.0, help="ATR逆算用の基準tm(検証には無関係)")
ap.add_argument("--stop-delay-bars", type=int, default=0,
                help="約定バーから何本 損切りを無効化するか(0=約定バーから有効=base / 1=delay1)")
ap.add_argument("--slip", type=float, default=0.0, help="(未使用・互換のため)")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--bt-min", type=float, default=0.0,
                help="ロングBTスコア下限で母集団を絞る(実際に投資する集団)。30=BT30以上/0=全部。"
                     "各(銘柄,戦略)の base(ref-sm/tm・楽観fill)成績を直近365日6期間で calc_recommend_score")
ap.add_argument("--by-month", action="store_true", help="最良セルの月別内訳も出力")
ap.add_argument("--stop-slip", type=float, default=0.005,
                help="保守モデルの損切りスリッページ(既定0.5%%)。実スリップ大の想定なら 0.01/0.02 に上げる")
args = ap.parse_args()

import backtest_limit_entry as ble
from backtest_limit_entry import floor_to_tick, round_to_tick, tick_size
from check_signals_stop import PERIODS as _BT_PERIODS, calc_recommend_score as _calc_bt
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import long_entry_fill_5m, long_pnl

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False

QTY = ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY
_GAP_LIMIT = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
_ON_CLOSE = getattr(ble, "_INTRADAY_5M_ON_CLOSE", False)
_STOP_SLIP = args.stop_slip
TODAY = pd.Timestamp.now().normalize()

SM_GRID = [float(x) for x in args.sm_grid.split(",") if x.strip()]
TM_GRID = [float(x) for x in args.tm_grid.split(",") if x.strip()]
# セル = (sm, tm)。表示名 "sm0.10/tm1.00"。
CELLS = [(sm, tm) for sm in SM_GRID for tm in TM_GRID]


def _cell_name(sm, tm) -> str:
    return f"sm{sm:.2f}/tm{tm:.2f}"


def _load_selected() -> list[tuple]:
    path = args.symbols_file
    if path is None:
        for cand in ("holdout_selected_symbols.py",):
            if Path(cand).exists():
                path = cand
                break
    if path is None:
        props = sorted(Path(".").glob("long_watchlist_proposal_*.py")) \
            or sorted(Path(".").glob("long_proposal_*.py"))
        if props:
            path = str(props[-1])
    if path is None or not Path(path).exists():
        sys.exit("[error] 選定ファイルが見つかりません(--symbols-file で指定)")
    spec = importlib.util.spec_from_file_location("_sel_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = getattr(mod, "SELECTED", None) or getattr(mod, "SELECTED_PAIRS", None)
    if rows is None:
        sys.exit(f"[error] {path} に SELECTED がありません")
    out = [(str(r[0]), str(r[1]) if len(r) >= 3 else "", str(r[-1])) for r in rows if len(r) >= 2]
    print(f"[選定] {path} から {len(out)} ペア読込")
    return out


def _day_ohlc(df_raw, fd):
    try:
        drow = df_raw.loc[df_raw.index.normalize() == pd.Timestamp(fd)]
        if len(drow):
            return (float(drow["open"].iloc[0]), float(drow["low"].iloc[0]),
                    float(drow["high"].iloc[0]), float(drow["close"].iloc[0]))
    except Exception:
        pass
    return (None, None, None, None)


def _bt_score(trades) -> int:
    """(約定日, base-line pnl) から ロングBTスコアを算出(レポートのBT定義に近い近似)。"""
    if not trades:
        return 0
    pr = {}
    for P in _BT_PERIODS:
        cutoff = TODAY - pd.Timedelta(days=P)
        sub = [p for (d, p) in trades if pd.Timestamp(d) >= cutoff]
        if not sub:
            continue
        wins = sum(1 for p in sub if p > 0)
        gp = sum(p for p in sub if p > 0)
        gl = -sum(p for p in sub if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[P] = {"trades": len(sub), "win_rate": wins / len(sub) * 100,
                 "pf": pf, "total_pnl": sum(sub)}
    return _calc_bt(pr)[0]


def _exit_long(opens, highs, lows, closes, ei, stop_from, stop_p, target_p, day_close):
    """ロング(逆指値買い)の同日決済を first-touch。損切=下(lows<=stop) / 利確=上(highs>=target)。
    同バー損切り優先(悲観)。stop_from(>=ei) からタイト損切りを効かせる(delay対応)。
    損切り約定3通り: line=楽観(stopちょうど) / real=窓埋め(次足始値, 下ギャップ=不利) / slip=real-スリップ。
    戻り: (reason, ex_line, ex_real, ex_slip, exit_idx)。"""
    n = len(highs)

    def _stop_fills(px, j):
        line = float(px)
        real = line if j == ei else min(line, float(opens[j]))   # 次足以降は始値で埋める(下=不利)
        return line, real, real * (1.0 - _STOP_SLIP)

    for j in range(ei, n):
        # タイト損切り(下): 武装済み(j>=stop_from)なら下落時に発火。同バー優先。
        if j >= stop_from:
            sh = closes[j] <= stop_p if _ON_CLOSE else lows[j] <= stop_p
            if sh:
                px = float(closes[j]) if _ON_CLOSE else stop_p
                ln, rl, sl = _stop_fills(px, j)
                return "stop", ln, rl, sl, j
        th = closes[j] >= target_p if _ON_CLOSE else highs[j] >= target_p
        if th:
            tp = float(closes[j]) if _ON_CLOSE else target_p
            return "target", tp, tp, tp, j
    cx = day_close if (day_close and day_close > 0) else float(closes[-1])
    return "close", cx, cx, cx, n - 1


def _empty_agg():
    return {_cell_name(sm, tm): {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                                 "pnl_line": 0.0, "pnl_real": 0.0, "pnl_slip": 0.0,
                                 "tgt": 0, "stop": 0, "close": 0}
            for (sm, tm) in CELLS}


def _scan_symbol(sym: str, name: str, strats: list[str]) -> dict:
    """1銘柄・全戦略のロングトレードを sm×tm 各セルで再計算し、セル別集計partialを返す。"""
    agg = _empty_agg()
    mon = {}   # (cell, month)->pnl_real
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return {"agg": agg, "mon": mon}
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return {"agg": agg, "mon": mon}
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return {"agg": agg, "mon": mon}
    if df_raw is None or df_raw.empty:
        return {"agg": agg, "mon": mon}

    _bt_window = max(args.days, max(_BT_PERIODS))
    for strat in strats:
        mod = mod_for(strat)
        params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
        if not params:
            continue
        cf = params[0]
        try:
            df_ind = cf(df_raw.copy())
            # ロング逆指値買いでトレードを1回だけ生成(ATRを order_stop から逆算するため ref-sm を使う)
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.ref_sm, args.ref_tm, args.days + 420, strat,
                                       entry_type="stop", max_hold=0)
        except Exception:
            continue
        if not r:
            continue
        local = _empty_agg()
        local_mon: dict = {}
        bt_trades: list = []   # (約定日, base-line pnl) 直近365日 → ロングBTスコア用(基準セル=ref)
        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            olp = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)   # ロング: 損切=下
            otp = float(t.get("order_target", 0) or 0)  # ロング: 利確=上
            if olp <= 0 or osp <= 0 or otp <= 0 or olp < args.min_price or olp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=_bt_window):
                continue
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            atr = (olp - osp) / args.ref_sm if args.ref_sm > 0 else 0.0
            if atr <= 0:
                continue
            d_open, d_low, d_high, d_close = _day_ohlc(df_raw, fd)
            # トリガー = 前日終値+1tick(買い逆指値)。約定価格は上ブレイク現実モデル。
            base_lp = round_to_tick(olp)
            trigger = float(round_to_tick(base_lp + tick_size(base_lp)))
            entry_fill = long_entry_fill_5m(db, trigger, entry_gap_limit=_GAP_LIMIT,
                                            day_open=d_open, day_high=d_high)
            if entry_fill is None:
                continue
            opens = db["open"].to_numpy(dtype=float)
            highs = db["high"].to_numpy(dtype=float)
            lows = db["low"].to_numpy(dtype=float)
            closes = db["close"].to_numpy(dtype=float)
            n = len(highs)
            # 約定バー ei(5分足がトリガー未達なら日足高値救済で ei=0)
            ei = None
            for j in range(n):
                if highs[j] >= trigger:
                    ei = j
                    break
            if ei is None:
                if d_high is not None and d_high > 0 and d_high >= trigger:
                    ei = 0
                else:
                    continue
            # BTスコア用: base(ref-sm/tm・楽観fill)の pnl を365日ぶん記録
            _sp_ref = float(floor_to_tick(olp - atr * args.ref_sm))
            _tp_ref = float(floor_to_tick(olp + atr * args.ref_tm))
            rb, exl, _, _, _ = _exit_long(opens, highs, lows, closes, ei, ei,
                                          _sp_ref, _tp_ref, d_close)
            bt_trades.append((fd, long_pnl(entry_fill, exl, rb, QTY, FEE_ONE_WAY, 0.0)))
            # セル比較は --days 窓のトレードだけ
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=args.days):
                continue
            mon_key = str(fd)[:7]
            stop_from = ei + max(0, args.stop_delay_bars)
            for (sm, tm) in CELLS:
                sp = float(floor_to_tick(olp - atr * sm))   # 損切=下
                tp = float(floor_to_tick(olp + atr * tm))   # 利確=上
                if sp <= 0 or tp <= entry_fill:
                    continue
                reason, ex_line, ex_real, ex_slip, _ei2 = _exit_long(
                    opens, highs, lows, closes, ei, stop_from, sp, tp, d_close)
                pnl_line = long_pnl(entry_fill, ex_line, reason, QTY, FEE_ONE_WAY, 0.0)
                pnl_real = long_pnl(entry_fill, ex_real, reason, QTY, FEE_ONE_WAY, 0.0)
                pnl_slip = long_pnl(entry_fill, ex_slip, reason, QTY, FEE_ONE_WAY, 0.0)
                a = local[_cell_name(sm, tm)]
                a["n"] += 1
                a["pnl_line"] += pnl_line
                a["pnl_real"] += pnl_real
                a["pnl_slip"] += pnl_slip
                a["tgt" if reason == "target" else ("close" if reason == "close" else "stop")] += 1
                if pnl_real > 0:
                    a["win"] += 1
                    a["gp"] += pnl_real
                else:
                    a["gl"] += -pnl_real
                if args.by_month:
                    _k = (_cell_name(sm, tm), mon_key)
                    local_mon[_k] = local_mon.get(_k, 0.0) + pnl_real
        # ロングBTスコアで母集団を絞る
        if args.bt_min > 0 and _bt_score(bt_trades) < args.bt_min:
            continue
        for cname, a in local.items():
            t2 = agg[cname]
            for k in a:
                t2[k] += a[k]
        for k, v in local_mon.items():
            mon[k] = mon.get(k, 0.0) + v
    return {"agg": agg, "mon": mon}


def _pf(gp, gl):
    return gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)


def _pf_s(gp, gl):
    v = _pf(gp, gl)
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    sel = _load_selected()
    by_sym: dict[str, dict] = {}
    for code, name, strat in sel:
        d = by_sym.setdefault(code, {"name": name, "strats": []})
        if strat not in d["strats"]:
            d["strats"].append(strat)
        if name and not d["name"]:
            d["name"] = name
    syms = list(by_sym.items())
    if args.limit:
        syms = syms[:args.limit]
    _btlab = f" / BT{args.bt_min:.0f}以上に絞る" if args.bt_min > 0 else " / 全部(BTフィルタ無し)"
    _dly = f" / delay{args.stop_delay_bars}" if args.stop_delay_bars > 0 else ""
    print(f"[比較] {len(syms)}銘柄 / 遡及{args.days}日 / 価格{args.min_price:.0f}〜{args.max_price:.0f}円 "
          f"/ {len(CELLS)}セル(sm{len(SM_GRID)}×tm{len(TM_GRID)}){_btlab}{_dly}")

    total = _empty_agg()
    mon_total: dict = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_symbol, code, v["name"], v["strats"]): code
                for code, v in syms}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                res = fut.result()
            except Exception:
                continue
            for cname, a in res["agg"].items():
                t = total[cname]
                for k in ("n", "win", "gp", "gl", "pnl_line", "pnl_real", "pnl_slip",
                          "tgt", "stop", "close"):
                    t[k] += a[k]
            for k, v in res["mon"].items():
                mon_total[k] = mon_total.get(k, 0.0) + v
            if done % 50 == 0:
                print(f"  ...{done}/{len(syms)}銘柄", flush=True)

    # ── グリッド表(net現実) ──
    print("\n" + "=" * 100)
    print("ロングデイトレ 損切×利確グリッド — net現実(次足損切りは窓埋め=下ギャップ約定)[円]")
    print("=" * 100)
    _hdr = f"{'損切sm＼利確tm':<14}" + "".join(f"{tm:>13.2f}" for tm in TM_GRID)
    print(_hdr)
    for sm in SM_GRID:
        row = f"{sm:<14.2f}"
        for tm in TM_GRID:
            row += f"{total[_cell_name(sm, tm)]['pnl_real']:>+13,.0f}"
        print(row)

    # ── グリッド表(PF・現実) ──
    print("\n【PF(現実)】")
    print(f"{'損切sm＼利確tm':<14}" + "".join(f"{tm:>13.2f}" for tm in TM_GRID))
    for sm in SM_GRID:
        row = f"{sm:<14.2f}"
        for tm in TM_GRID:
            t = total[_cell_name(sm, tm)]
            row += f"{_pf_s(t['gp'], t['gl']):>13}"
        print(row)

    # ── 上位セル詳細(net現実 降順) ──
    ranked = sorted(CELLS, key=lambda c: -total[_cell_name(*c)]["pnl_real"])
    print("\n【上位セル詳細】net楽観=stopちょうど / net現実=窓埋め / net保守=現実-0.5%")
    print(f"{'セル':<16}{'件数':>6}{'勝率':>6}{'PF':>6}{'利確':>5}{'損切':>5}{'引け':>5}"
          f"{'net楽観':>13}{'net現実':>13}{'net保守':>13}")
    for (sm, tm) in ranked[:8]:
        t = total[_cell_name(sm, tm)]
        n = t["n"] or 1
        print(f"{_cell_name(sm, tm):<16}{t['n']:>6}{t['win']/n*100:>5.0f}%"
              f"{_pf_s(t['gp'], t['gl']):>6}{t['tgt']:>5}{t['stop']:>5}{t['close']:>5}"
              f"{t['pnl_line']:>+13,.0f}{t['pnl_real']:>+13,.0f}{t['pnl_slip']:>+13,.0f}")
    _best = ranked[0]
    _bt = total[_cell_name(*_best)]
    _pop = f"BT{args.bt_min:.0f}以上" if args.bt_min > 0 else "全選定ペア"
    print(f"\n★ 最良(net現実): {_cell_name(*_best)} → net現実 {_bt['pnl_real']:+,.0f}円 / "
          f"net保守 {_bt['pnl_slip']:+,.0f}円 / PF {_pf_s(_bt['gp'], _bt['gl'])} "
          f"({_bt['n']}件, 母集団={_pop})")
    print("  ※ 選定も sm/tm 依存。この最良値で scan_long_daytrade を回し直すと選定銘柄も最適化される。")

    # ── 月別(最良セル) ──
    if args.by_month:
        months = sorted({m for (_, m) in mon_total})
        print(f"\n【月別ネット損益(現実)・最良セル {_cell_name(*_best)}】")
        _bn = _cell_name(*_best)
        print("".join(f"{m[2:]:>10}" for m in months))
        print("".join(f"{mon_total.get((_bn, m), 0.0):>10,.0f}" for m in months))

    # ── CSV(全セル) ──
    date_s = str(TODAY.date())
    rows = []
    for (sm, tm) in CELLS:
        t = total[_cell_name(sm, tm)]
        n = t["n"] or 1
        rows.append({"sm": sm, "tm": tm, "trades": t["n"],
                     "win_rate": round(t["win"] / n * 100, 1),
                     "pf": round(_pf(t["gp"], t["gl"]), 2),
                     "target": t["tgt"], "stop": t["stop"], "close": t["close"],
                     "net_optimistic": round(t["pnl_line"], 0),
                     "net_realistic": round(t["pnl_real"], 0),
                     "net_conservative": round(t["pnl_slip"], 0)})
    out = Path(f"compare_long_rules_{date_s}.csv")
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n[出力] {out}")


if __name__ == "__main__":
    main()
