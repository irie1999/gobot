r"""analyze_gap_fills.py — lssの「−3%指値ガード」を再検討する診断ツール。

背景(2026-07-31 実弾で判明):
  lssの現行バックテストは「寄りがトリガーより -3% 超も下に飛んだら約定不可(除外)」
  (`sameday5m_firsttouch.short_entry_fill_5m`, entry_gap_limit=0.03 → None)。
  しかし実運用(kabu)は「トリガー+−3%下限指値」が板に残り、価格が下限まで戻れば下限で約定する。
  実際 2026-07-31 は 6701/7267/7616/9616 の4件が「寄りは−3%超に飛び→下限に戻って約定」しており、
  バックテストの取引明細には出ないのに実弾では約定(+2,242円)していた。

この乖離を歴史データで定量化する。2つの約定モデルをガード閾値でスイープ比較:
  A) 約定不可モデル(現行): 寄りが trigger×(1-G) 未満 → 約定不可(除外)。
  B) 下限約定モデル(実運用寄り): 寄りが下限未満でも、その後5分足が下限に戻れば下限で約定。
各Gで lss全体の 損益/取引数/勝率/PF を出し、さらに「深ギャップ(現行-3%で除外)を下限約定させた
増分」を深さ別(−3〜5% / −5〜10% / −10%超)に集計する。

判断:
  * 深ギャップ下限約定の増分が net プラス → 実運用に合わせ B モデル化+ガードを最適Gへ。
    その際 **バックテストのガード(_INTRADAY_5M_ENTRY_GAP_LIMIT) と 実発注の−X%下限指値は必ず同値に
    揃える**(片方だけ変えると乖離が再発)。
  * 増分が net マイナス/誤差 → 現行の−3%約定不可(保守)が正しい。実運用は下限で頭打ちなので実害小。

使い方(あなたの機械で・5分足が必要):
  python analyze_gap_fills.py --source local --workers 8
  python analyze_gap_fills.py --symbols holdout_selected_symbols.py   # 実運用の選定WLで測る(推奨)
  python analyze_gap_fills.py --days 500 --refresh-cache
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss −3%指値ガードの再検討(約定不可 vs 下限約定)")
ap.add_argument("--symbols", type=str, default="",
                help="選定ファイル(SELECTED/WATCHLIST=[(code,name,strat),...])。"
                     "既定は holdout_selected_symbols.py を自動検出→無ければ stock_5min 全銘柄×6戦略。")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(lss既定0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(lss既定1.0)")
ap.add_argument("--stop-delay-bars", type=int, default=1,
                help="損切り遅延(既定1=delay1=実運用watcherと一致)")
ap.add_argument("--days", type=int, default=500, help="遡及日数")
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(デバッグ)")
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--qty", type=int, default=None)
ap.add_argument("--slip", type=float, default=0.0, help="不利スリップ(lss既定0)")
ap.add_argument("--fee", type=float, default=None)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--no-cache", action="store_true")
ap.add_argument("--refresh-cache", action="store_true")
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import available_local_symbols, load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_pnl

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY if args.fee is None else args.fee
DELAY = max(0, int(args.stop_delay_bars))

# ガード閾値スイープ (None = 無制限=ガードなし)
GUARDS = [0.02, 0.03, 0.05, 0.10, None]
GLABEL = {0.02: "2%", 0.03: "3%", 0.05: "5%", 0.10: "10%", None: "無制限"}
CURRENT_GUARD = 0.03   # 現行の実運用/バックテストのガード


def _jq_to_yf(code: str) -> str:
    c = code.strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _strategies() -> list[str]:
    return ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _load_symbols():
    """--symbols か holdout_selected_symbols.py を (code,name,strat) で。無ければ None。"""
    import importlib.util
    cand = args.symbols
    if not cand:
        for f in ("holdout_selected_symbols.py", "lss_proposal_cumul.py"):
            if Path(f).exists():
                cand = f
                break
    if not cand or not Path(cand).exists():
        return None
    spec = importlib.util.spec_from_file_location("_symmod", cand)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for attr in ("SELECTED", "WATCHLIST", "SYMBOLS", "STOP_WATCHLIST"):
        v = getattr(m, attr, None)
        if v:
            out = []
            for row in v:
                if len(row) >= 3:
                    out.append((_jq_to_yf(str(row[0])), str(row[1]), str(row[2])))
            if out:
                print(f"[info] 選定ファイル {cand} から {len(out)}ペア読込")
                return out
    return None


def _name_map() -> dict:
    try:
        import symbols_listed_prime as _p
        return {c: n for (c, n) in getattr(_p, "SYMBOLS", [])}
    except Exception:
        return {}


def _short_exit(highs, lows, closes, fbar, stop_p, target_p):
    n = len(highs)
    ss = fbar + DELAY
    for j in range(fbar, n):
        if j >= ss and highs[j] >= stop_p:   # 同バー損切り優先(v13・delay対応)
            return stop_p, "stop"
        if lows[j] <= target_p:
            return target_p, "target"
    return closes[-1], "close"


def _one(sig, guard, allow_floor):
    """1シグナル×(guard,モデル)の損益。約定しなければ None。"""
    highs, lows, closes = sig["highs"], sig["lows"], sig["closes"]
    o, trigger = sig["o"], sig["trigger"]
    stop_p, target_p = sig["stop"], sig["target"]
    floor = trigger * (1.0 - guard) if guard is not None else 0.0
    n = len(highs)
    fill = fbar = None
    if o >= trigger:                      # 寄りがトリガー超 → ザラ場で下抜けた瞬間に約定
        for j in range(n):
            if lows[j] <= trigger:
                fill, fbar = trigger, j
                break
    elif o >= floor:                      # 寄りギャップダウンだがガード内 → 寄り(始値)約定
        fill, fbar = o, 0
    else:                                 # 深ギャップ(寄りが下限未満)
        if allow_floor:                   # 下限指値: 価格が下限に戻れば下限で約定
            for j in range(n):
                if highs[j] >= floor:
                    fill, fbar = floor, j
                    break
        # allow_floor=False は約定不可(現行) → fill=None のまま
    if fill is None:
        return None
    xp, reason = _short_exit(highs, lows, closes, fbar, stop_p, target_p)
    return short_pnl(fill, xp, reason, QTY, FEE_ONE_WAY, args.slip)


def _scan_symbol(sym, name, strats):
    """1銘柄・全戦略のlssシグナルを集め、各シグナルの5分足配列と価格を返す(生データ)。"""
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
    daily_open = {}
    for d in df_raw.index:
        dd = d.date() if hasattr(d, "date") else d
        try:
            daily_open[dd] = float(df_raw.loc[d, "open"])
        except Exception:
            pass

    for strat in strats:
        mod = mod_for(strat)
        params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
        if not params:
            continue
        cf = params[0]
        try:
            df_ind = cf(df_raw.copy())
        except Exception:
            continue
        try:
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.sm, args.tm, args.days + 420, strat,
                                       entry_type="stop_sell", max_hold=0)
        except Exception:
            continue
        if not r:
            continue
        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            lp = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if lp <= 0 or osp <= 0 or otp <= 0:
                continue
            if lp < args.min_price or lp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            o = daily_open.get(fd)
            if o is None or o <= 0:
                o = float(db["open"].iloc[0])
            out.append({
                "trigger": lp, "stop": max(osp, otp), "target": min(osp, otp),
                "o": o, "gap": (lp - o) / lp if lp > 0 else 0.0,
                "highs": db["high"].to_numpy(dtype=float),
                "lows": db["low"].to_numpy(dtype=float),
                "closes": db["close"].to_numpy(dtype=float),
            })
    return out


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "pnl": sum(pnls), "pf": pf, "wr": len(wins) / n * 100}


def _fmt(s):
    if not s:
        return "  取引なし"
    _pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"{s['n']:>6}件  損益{s['pnl']:>+13,.0f}  勝率{s['wr']:>4.0f}%  PF{_pf:>5}")


def main():
    picks = _load_symbols()
    if picks:
        seen = set(); pairs = []
        for (yf, nm, st) in picks:
            if (yf, st) not in seen:
                seen.add((yf, st)); pairs.append((yf, nm, st))
        by_sym = {}
        for (yf, nm, st) in pairs:
            by_sym.setdefault(yf, {"name": nm, "strats": []})
            by_sym[yf]["strats"].append(st)
        universe = list(by_sym.keys())
        get_strats = lambda s: by_sym[s]["strats"]
        get_name = lambda s: by_sym[s]["name"]
        src_note = f"選定WL {len(pairs)}ペア / {len(universe)}銘柄"
    else:
        seen = set(); universe = []
        for _s in available_local_symbols():
            yf = _jq_to_yf(_s)
            if yf not in seen:
                seen.add(yf); universe.append(yf)
        names = _name_map()
        allstr = _strategies()
        get_strats = lambda s: allstr
        get_name = lambda s: names.get(s, s)
        src_note = f"stock_5min 全{len(universe)}銘柄 × 6戦略"
    if args.limit > 0:
        universe = universe[:args.limit]
    if not universe:
        print("[error] 対象銘柄なし。", file=sys.stderr)
        return
    print(f"[info] 対象: {src_note} / sm={args.sm} tm={args.tm} delay={DELAY} / "
          f"{'摩擦なし' if (args.slip==0 and FEE_ONE_WAY==0) else f'slip{args.slip*100:.2f}%/手数料{FEE_ONE_WAY*100:.2f}%'}")

    import hashlib as _h, pickle as _pk
    _cd = Path(".gapfill_cache")
    _key = _h.md5("|".join(str(x) for x in [
        "gapv1", getattr(ble, "_BT_LOGIC_VER", "?"), args.sm, args.tm, args.source,
        args.days, args.limit, args.min_price, args.max_price, QTY,
        _h.md5(",".join(sorted(universe)).encode()).hexdigest(),
    ]).encode()).hexdigest()[:16]
    _cf = _cd / f"sigs_{_key}.pkl"

    sigs = None
    if _cf.exists() and not args.no_cache and not args.refresh_cache:
        try:
            sigs = _pk.loads(_cf.read_bytes())
            print(f"[cache] シグナル再利用: {_cf} ({len(sigs)}件) ※--refresh-cacheで再計算")
        except Exception:
            sigs = None
    if sigs is None:
        sigs = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_scan_symbol, s, get_name(s), get_strats(s)): s for s in universe}
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 50 == 0:
                    print(f"  ...{done}/{len(universe)}銘柄", flush=True)
                try:
                    sigs += fut.result()
                except Exception:
                    continue
        if not args.no_cache:
            try:
                _cd.mkdir(exist_ok=True)
                _cf.write_bytes(_pk.dumps(sigs))
                print(f"[cache] シグナル保存: {_cf} ({len(sigs)}件)")
            except Exception as _e:
                print(f"[cache] 保存失敗({_e})")

    if not sigs:
        print("[error] シグナル0件。5分足在庫や期間を確認。", file=sys.stderr)
        return

    print(f"\n全lssシグナル {len(sigs)}件 で検証\n" + "=" * 78)

    # ① ガード閾値スイープ: A)約定不可(現行) vs B)下限約定
    print("① ガード閾値スイープ  (lss全体の同日損益)")
    print("-" * 78)
    print(f"  {'ガード':<6}{'A:約定不可(現行方式)':<34}{'B:下限約定(実運用寄り)'}")
    for g in GUARDS:
        a = _stat([p for s in sigs if (p := _one(s, g, False)) is not None])
        b = _stat([p for s in sigs if (p := _one(s, g, True)) is not None])
        _ag = "   ∞ " if g is None else f"{GLABEL[g]:>4} "
        print(f"  {_ag}{_fmt(a):<34}{_fmt(b)}")
    print("  (Aは深ギャップ除外・Bは下限で約定。同ガードでB>Aなら下限約定が寄与)")

    # ② 現行-3%で「除外されている深ギャップ」を下限約定させた増分(深さ別)
    print("\n② 現行-3%ガードで除外中の『深ギャップ』を下限約定させた増分損益")
    print("-" * 78)
    buckets = {"−3〜5%": (0.03, 0.05), "−5〜10%": (0.05, 0.10), "−10%超": (0.10, 9.9)}
    total_deep = []
    for label, (lo, hi) in buckets.items():
        pnls = []
        for s in sigs:
            g = s["gap"]
            if lo < g <= hi:                # 寄りが trigger より lo〜hi 下(=現行-3%で除外域)
                # 実発注の-3%下限で約定(価格が-3%下限に戻れば約定)。これが現行除外分の増分。
                p3 = _one(s, CURRENT_GUARD, True)
                if p3 is not None:
                    pnls.append(p3)
        st = _stat(pnls)
        total_deep += pnls
        print(f"  深さ {label:<8}: {_fmt(st)}")
    print(f"  {'合計(深ギャップ下限約定)':<12}: {_fmt(_stat(total_deep))}")
    print("  → 合計がプラス: 実運用に合わせ下限約定をモデル化する価値。マイナス/誤差: 現行-3%約定不可が正しい。")

    print("\n" + "=" * 78)
    print("判断メモ: ①でB>Aが安定 & ②合計がプラスなら、_INTRADAY_5M_ENTRY_GAP_LIMIT と")
    print("実発注の−X%下限指値を最適ガードに揃える(両者必ず同値)。差が誤差なら現行-3%維持。")


if __name__ == "__main__":
    main()
