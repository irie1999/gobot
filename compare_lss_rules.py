"""compare_lss_rules.py — lss(逆指値空売り・同日決済)の決済/エントリー・ルール案を
ネット損益(勝ち+負け)・勝率・PFで比較する A/B 検証ツール。

analyze_lss_losses.py は「負けたトレード」だけを形状分析するが、本ツールは
**全トレード(勝ち・負け・引け)を各ルール案で再計算してネット期待値を比較**する。
ルール変更が本当に総損益を改善するかを判定するのが目的。

比較するルール案:
  base           : 現行 v13(約定バーから損切り・sm=0.1)。レポートと同じ。
  delay1         : 寄りの1本目(約定バー)は損切りを効かせない(利確のみ)。2本目以降から損切り。
                   = ライブで「寄り5分後に逆指値損切りを設置」。同バーのヒゲ刈り回避。
  delay2         : 約定バー+次の1本(=寄り10分)まで損切りなし。3本目から損切り。
  gap<-1.0%見送り: 寄りがトリガーより1.0%以上ギャップダウンしたトレードは発注しない。
  gap<-1.5/2.0% : 同上、しきい値違い。
  delay1+gap1.5  : 両方併用。
  sm0.2 / sm0.3  : 損切り幅を広げる(約定バーから)。
  delay1+sm0.2   : 寄り1本目なし + 損切り0.2。

使い方(あなたの機械で。5分足データが必要):
  python compare_lss_rules.py --days 240 --min-price 1000 --max-price 6000
  python compare_lss_rules.py --by-month           # 月別内訳も出す
出力: コンソール比較表 + compare_lss_rules_<date>.csv
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lssルール案のネット損益A/B比較")
ap.add_argument("--symbols-file", type=str, default=None)
ap.add_argument("--days", type=int, default=240)
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--slip", type=float, default=0.0)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--bt-min", type=float, default=0.0,
                help="BTスコア下限で母集団を絞る(実際に投資する集団)。30=BT30以上/70=BT70以上/0=全部。"
                     "各(銘柄,戦略)の現行base(v13)成績を直近365日で6期間スライスし calc_recommend_score で算出")
ap.add_argument("--by-month", action="store_true", help="月別内訳も出力")
args = ap.parse_args()

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick, round_to_tick, tick_size
from check_signals_stop import PERIODS as _BT_PERIODS, calc_recommend_score as _calc_bt
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_pnl

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
TODAY = pd.Timestamp.now().normalize()

# ルール案の定義。stop_off_bars=約定バーから何本 損切りを無効にするか(0=約定バーから有効=base)。
# sm2=損切りATR倍率(None=args.sm)。gap_skip=このギャップ率(負値)未満のギャップダウンは見送り。
RULES = [
    {"name": "base(v13 sm0.1)",       "stop_off": 0, "sm2": None, "gap_skip": None},
    {"name": "delay1(寄1本目stopなし)", "stop_off": 1, "sm2": None, "gap_skip": None},
    {"name": "delay2(寄2本目からstop)", "stop_off": 2, "sm2": None, "gap_skip": None},
    {"name": "delay3(寄3本目=15分後)",   "stop_off": 3, "sm2": None, "gap_skip": None},
    {"name": "delay4(寄4本目=20分後)",   "stop_off": 4, "sm2": None, "gap_skip": None},
    {"name": "gap<-1.0%見送り",        "stop_off": 0, "sm2": None, "gap_skip": -0.010},
    {"name": "gap<-1.5%見送り",        "stop_off": 0, "sm2": None, "gap_skip": -0.015},
    {"name": "gap<-2.0%見送り",        "stop_off": 0, "sm2": None, "gap_skip": -0.020},
    {"name": "delay1+gap<-1.5%",       "stop_off": 1, "sm2": None, "gap_skip": -0.015},
    {"name": "sm0.2(base)",            "stop_off": 0, "sm2": 0.2,  "gap_skip": None},
    {"name": "sm0.3(base)",            "stop_off": 0, "sm2": 0.3,  "gap_skip": None},
    {"name": "delay1+sm0.2",           "stop_off": 1, "sm2": 0.2,  "gap_skip": None},
]


def _load_selected() -> list[tuple]:
    path = args.symbols_file
    if path is None:
        for cand in ("holdout_selected_symbols.py", "holdout_selected_symbols_short.py"):
            if Path(cand).exists():
                path = cand
                break
    if path is None:
        props = sorted(Path(".").glob("lss_watchlist_proposal_*.py"))
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


def _prices(order_limit, order_stop, order_target):
    base = round_to_tick(order_limit)
    trigger = float(round_to_tick(base - tick_size(base)))
    stop_p = float(ceil_to_tick(max(order_stop, order_target)))
    target_p = float(ceil_to_tick(min(order_stop, order_target)))
    return trigger, stop_p, target_p


def _bt_score(trades) -> int:
    """(約定日, base-line pnl) のリストから BTスコアを算出(レポートのBTに近い近似)。
    直近365日を PERIODS(30/90/180/365)でスライスし勝率/PF/安定/取引数で採点。
    現行base(v13, stopちょうど=engineと同じ楽観fill)の成績で計算=レポートのBT定義に一致。"""
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


_STOP_SLIP = 0.005   # 保守モデルの追加スリッページ(0.5%)


def _exit(opens, highs, lows, closes, ei, stop_from, stop_p, target_p, day_close):
    """約定バー ei から利確、stop_from(>=ei) から損切りを first-touch。損切り優先。
    損切り発火時の買い戻し価格を3通り返す(live下限測定用):
      line = stop ちょうど(楽観)。
      real = 現実。同バー損切り(j==ei)は stop で約定(始値/高値は約定前の値を含むため不可)。
             次足以降(j>ei)は max(stop, その足の始値)=窓を空けて超えたら始値約定(delay1の
             無保護窓リスクを捕捉)。逆指値はライン越えの"瞬間"に成行約定するので高値は使わない。
      slip = real + 0.5%スリッページ(保守)。
    利確・引けは3通りとも同値。戻り: (reason, ex_line, ex_real, ex_slip)。"""
    n = len(highs)
    for j in range(ei, n):
        if j >= stop_from:
            sh = closes[j] >= stop_p if _ON_CLOSE else highs[j] >= stop_p
            if sh:
                ex_line = float(closes[j]) if _ON_CLOSE else stop_p
                # 同バーは約定前価格の混入を避け stop で約定。次足以降は窓埋め(始値)を考慮。
                ex_real = ex_line if j == ei else max(ex_line, float(opens[j]))
                ex_slip = ex_real * (1.0 + _STOP_SLIP)
                return "stop", ex_line, ex_real, ex_slip
        th = closes[j] <= target_p if _ON_CLOSE else lows[j] <= target_p
        if th:
            tp = float(closes[j]) if _ON_CLOSE else target_p
            return "target", tp, tp, tp
    cx = day_close if (day_close and day_close > 0) else float(closes[-1])
    return "close", cx, cx, cx


def _scan_symbol(sym: str, name: str, strats: list[str]) -> dict:
    """1銘柄・全戦略のlssトレードを各ルールで再計算し、ルール別の集計partialを返す。"""
    # pnl は損切り約定モデル別に3通り集計: line(楽観)/open(現実・窓埋め)/high(最悪)。
    # 勝率/PF は open(現実)ベースで表示。
    agg = {r["name"]: {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                       "pnl_line": 0.0, "pnl_real": 0.0, "pnl_slip": 0.0,
                       "tgt": 0, "stop": 0, "close": 0} for r in RULES}
    mon = {}  # (rule,month)->pnl (by-month用)
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

    # BTスコアは直近365日で算出するので、ルール比較窓(--days)より長く見る必要がある。
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
        # この (銘柄,戦略) 分をローカル集計し、BTスコアが閾値を満たしたら agg に合流する。
        local = {ru["name"]: {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                              "pnl_line": 0.0, "pnl_real": 0.0, "pnl_slip": 0.0,
                              "tgt": 0, "stop": 0, "close": 0} for ru in RULES}
        local_mon: dict = {}
        bt_trades: list = []   # (約定日, base-lineのpnl) 直近365日 → BTスコア算出用
        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            olp = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if olp <= 0 or osp <= 0 or otp <= 0 or olp < args.min_price or olp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=_bt_window):
                continue
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            trigger, stop_p, target_p = _prices(olp, osp, otp)
            d_open, d_low, d_high, d_close = _day_ohlc(df_raw, fd)
            entry_fill = short_entry_fill_5m(db, trigger, False, entry_gap_limit=_GAP_LIMIT,
                                             day_open=d_open, day_low=d_low, day_high=d_high)
            if entry_fill is None:
                continue
            gap_pct = (entry_fill - trigger) / trigger if trigger > 0 else 0.0
            atr = (osp - olp) / args.sm if args.sm > 0 else 0.0
            opens = db["open"].to_numpy(dtype=float)
            highs = db["high"].to_numpy(dtype=float)
            lows = db["low"].to_numpy(dtype=float)
            closes = db["close"].to_numpy(dtype=float)
            n = len(lows)
            # 約定バー ei(5分足がトリガー未達なら日足安値救済で ei=0)
            ei = None
            for j in range(n):
                if lows[j] <= trigger:
                    ei = j
                    break
            if ei is None:
                if d_low is not None and d_low > 0 and d_low <= trigger:
                    ei = 0
                else:
                    continue
            # BTスコア用: base(現行v13, stopちょうど=engine楽観fill)の pnl を365日ぶん記録
            rb, exl, _, _ = _exit(opens, highs, lows, closes, ei, ei, stop_p, target_p, d_close)
            bt_trades.append((fd, short_pnl(entry_fill, exl, rb, QTY, FEE_ONE_WAY, 0.0)))
            # ルール比較は --days 窓のトレードだけ
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=args.days):
                continue
            mon_key = str(fd)[:7]
            for rule in RULES:
                if rule["gap_skip"] is not None and gap_pct <= rule["gap_skip"]:
                    continue  # ギャップダウン見送り = このトレードは発注しない
                if rule["sm2"] is not None and atr > 0:
                    sp = float(ceil_to_tick(olp + atr * rule["sm2"]))
                else:
                    sp = stop_p
                stop_from = ei + rule["stop_off"]
                reason, ex_line, ex_real, ex_slip = _exit(
                    opens, highs, lows, closes, ei, stop_from, sp, target_p, d_close)
                pnl_line = short_pnl(entry_fill, ex_line, reason, QTY, FEE_ONE_WAY, 0.0)
                pnl_real = short_pnl(entry_fill, ex_real, reason, QTY, FEE_ONE_WAY, 0.0)
                pnl_slip = short_pnl(entry_fill, ex_slip, reason, QTY, FEE_ONE_WAY, 0.0)
                a = local[rule["name"]]
                a["n"] += 1
                a["pnl_line"] += pnl_line
                a["pnl_real"] += pnl_real
                a["pnl_slip"] += pnl_slip
                a[reason if reason in ("stop", "close") else "tgt"] += 1
                if pnl_real > 0:
                    a["win"] += 1
                    a["gp"] += pnl_real
                else:
                    a["gl"] += -pnl_real
                if args.by_month:
                    local_mon[(rule["name"], mon_key)] = \
                        local_mon.get((rule["name"], mon_key), 0.0) + pnl_real
        # BTスコアで母集団を絞る(実際に投資する集団)。閾値未満は集計に含めない。
        if args.bt_min > 0 and _bt_score(bt_trades) < args.bt_min:
            continue
        for rname, a in local.items():
            t2 = agg[rname]
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
    print(f"[比較] {len(syms)}銘柄 / 遡及{args.days}日 / 価格{args.min_price:.0f}〜{args.max_price:.0f}円 "
          f"/ {len(RULES)}ルール{_btlab}")

    total = {r["name"]: {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                         "pnl_line": 0.0, "pnl_real": 0.0, "pnl_slip": 0.0,
                         "tgt": 0, "stop": 0, "close": 0} for r in RULES}
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
            for name, a in res["agg"].items():
                t = total[name]
                for k in ("n", "win", "gp", "gl", "pnl_line", "pnl_real", "pnl_slip",
                          "tgt", "stop", "close"):
                    t[k] += a[k]
            for k, v in res["mon"].items():
                mon_total[k] = mon_total.get(k, 0.0) + v
            if done % 50 == 0:
                base = total["base(v13 sm0.1)"]
                print(f"  ...{done}/{len(syms)}銘柄  base net(現実) {base['pnl_real']:+,.0f}円 "
                      f"({base['n']}件)")

    base = total["base(v13 sm0.1)"]
    print("\n" + "=" * 104)
    print("lss ルール案 ネット比較 — 損切り約定を 楽観(line)/現実(gap)/保守(+0.5%) で下限測定")
    print("=" * 104)
    print(f"{'ルール':<22}{'件数':>6}{'勝率':>6}{'PF':>6}{'利確':>5}{'損切':>5}{'引け':>5}"
          f"{'net楽観':>13}{'net現実':>13}{'net保守':>13}{'base比(現実)':>14}")
    for rule in RULES:
        t = total[rule["name"]]
        n = t["n"] or 1
        wr = t["win"] / n * 100
        d = t["pnl_real"] - base["pnl_real"]
        tag = " ←現状" if rule["name"].startswith("base") else ""
        print(f"{rule['name']:<22}{t['n']:>6}{wr:>5.0f}%{_pf_s(t['gp'], t['gl']):>6}"
              f"{t['tgt']:>5}{t['stop']:>5}{t['close']:>5}"
              f"{t['pnl_line']:>+13,.0f}{t['pnl_real']:>+13,.0f}{t['pnl_slip']:>+13,.0f}"
              f"{d:>+14,.0f}{tag}")
    print("\n※ net楽観=stopちょうど / net現実=次足損切りは窓埋め(始値約定)・同バーはstop / net保守=現実+0.5%")
    _pop = f"BT{args.bt_min:.0f}以上(実際に投資する集団)" if args.bt_min > 0 else "全選定ペア(BTフィルタ無し)"
    print(f"※ 見るポイント: delay1 の『net保守』が base の『net現実』を上回れば、liveでも堅牢。"
          f"base vs delay1 の『net現実』差が期待できる現実的な改善幅。母集団={_pop}。")

    # ── 月別(--by-month) ──
    if args.by_month:
        months = sorted({m for (_, m) in mon_total})
        print("\n【月別ネット損益】")
        print(f"{'ルール':<22}" + "".join(f"{m[2:]:>10}" for m in months))
        for rule in RULES:
            row = "".join(f"{mon_total.get((rule['name'], m), 0.0):>10,.0f}" for m in months)
            print(f"{rule['name']:<22}{row}")

    # ── CSV ──
    date_s = str(TODAY.date())
    rows = []
    for rule in RULES:
        t = total[rule["name"]]
        n = t["n"] or 1
        rows.append({"rule": rule["name"], "trades": t["n"], "win_rate": round(t["win"]/n*100, 1),
                     "pf": round(_pf(t["gp"], t["gl"]), 2), "target": t["tgt"], "stop": t["stop"],
                     "close": t["close"], "net_optimistic_line": round(t["pnl_line"], 0),
                     "net_realistic_gap": round(t["pnl_real"], 0),
                     "net_conservative_slip": round(t["pnl_slip"], 0),
                     "vs_base_realistic": round(t["pnl_real"] - base["pnl_real"], 0)})
    pd.DataFrame(rows).to_csv(Path(f"compare_lss_rules_{date_s}.csv"), index=False,
                              encoding="utf-8-sig")
    print(f"\n[出力] compare_lss_rules_{date_s}.csv")


if __name__ == "__main__":
    main()
