"""
verify_sameday_long_dip.py  ―  「約定後すぐ押す→戻す」をデイトレ・ロングで取る検証
==================================================================
発想の転換:
  同日ショートは「押した後に戻る」せいで踏まれて失敗した(検証済)。
  ならその『戻り』こそロングで取れる。

  逆指値ロングが約定(ブレイク)した直後、ほぼ必ず一度押す(96.5%/平均-1.5%)。
  → その押し目を5分足で『指値買い』し、同日の戻りで決済する デイトレ・ロング。

【方法 (5分足・順番解決済)】
  1. 5分足→日足でスイング逆指値シグナル+約定ライン order_p を検出
  2. 約定の瞬間(5分足で高値が order_p 上抜け)を特定
  3. 約定『後』に価格が fill×(1-DIP%) まで押したら そこで指値買い(押し目)
  4. 同日 14:55 引けで決済 (戻りを取る)。LONG損益=(引け-押し目買値)/押し目買値
     ※ 押さなければ約定せず=ノートレード(損益0、機会損失のみ)

【出力】 押し目深さ DIP ごとに:
  - 約定率   = 約定後に DIP% まで押した割合 (= 押し目買いが成立した割合)
  - 平均損益 = 約定したトレードの平均% (引け決済)
  - 期待値/ｼｸﾞﾅﾙ = 約定率 × 平均損益 (出さなかった日=0込み)

使い方:
  python verify_sameday_long_dip.py --symbols-file holdout_selected_symbols.py --days 90
  python verify_sameday_long_dip.py --days 60 --limit 300
  python verify_sameday_long_dip.py --dips 0.5 1.0 1.5 2.0
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta, timezone

import numpy as np
import pandas as pd

from backtest_limit_entry import calc_macd, calc_a7, calc_rsi2, ENTRY_EXPIRE
from scan_breakout_entry import calc_donchian, calc_vol_breakout, calc_momentum
from daytrade_data import load_intraday, split_by_day
from symbols_listed_prime import SYMBOLS

JST = timezone(timedelta(hours=9))

STRATS = {
    "MACD": calc_macd, "A7": calc_a7, "RSI2": calc_rsi2,
    "DON": calc_donchian, "VOL": calc_vol_breakout, "MOM": calc_momentum,
}
EM = 0.0
DIPS = [0.5, 1.0, 1.5, 2.0]          # 押し目深さ(%)
FORCE_CLOSE = dtime(14, 55)
DATA_DAYS = 400


def _resample_daily(df5: pd.DataFrame) -> pd.DataFrame:
    g = df5.groupby(df5.index.date)
    daily = pd.DataFrame({
        "open":   g["open"].first(), "high": g["high"].max(),
        "low":    g["low"].min(),    "close": g["close"].last(),
        "volume": g["volume"].sum(),
    })
    daily.index = pd.to_datetime(daily.index)
    return daily


def _verify_symbol(sym, name, days, dips, allowed=None):
    try:
        df5 = load_intraday(sym, days=DATA_DAYS, source="local")
    except Exception:
        return []
    if df5 is None or df5.empty or len(df5) < 1000:
        return []
    daily = _resample_daily(df5)
    if len(daily) < 210:
        return []
    by5 = split_by_day(df5)
    dindex = list(daily.index)
    cutoff = datetime.now(JST).date() - timedelta(days=days)

    out = []
    for strat, calc_fn in STRATS.items():
        if allowed is not None and strat not in allowed:
            continue
        try:
            d = calc_fn(daily.copy())
        except Exception:
            continue
        sig = d["entry_sig"].to_numpy()
        atr = d["atr"].to_numpy()
        close = d["close"].to_numpy()
        for j in range(len(d) - 1):
            if not sig[j] or np.isnan(atr[j]) or atr[j] <= 0:
                continue
            if dindex[j].date() < cutoff:
                continue
            order_p = close[j] + atr[j] * EM
            if order_p <= 0:
                continue
            # 約定検出
            fill = None
            for k in range(j + 1, min(j + 1 + ENTRY_EXPIRE, len(dindex))):
                bars = by5.get(dindex[k].date())
                if bars is None or len(bars) < 3:
                    continue
                o = bars["open"].to_numpy(dtype=float)
                hi = bars["high"].to_numpy(dtype=float)
                lo = bars["low"].to_numpy(dtype=float)
                cl = bars["close"].to_numpy(dtype=float)
                tmv = bars.index
                if o[0] >= order_p:
                    fill = (0, o[0], lo, cl, tmv); break
                cross = np.where(hi >= order_p)[0]
                if len(cross) > 0:
                    fill = (int(cross[0]), order_p, lo, cl, tmv); break
            if fill is None:
                continue
            bi, fp, lo, cl, tmv = fill
            if fp <= 0:
                continue
            idx_after = [p for p in range(len(tmv))
                         if p > bi and tmv[p].time() <= FORCE_CLOSE]
            rec = {"sym": sym, "strat": strat}
            for D in dips:
                dip_level = fp * (1 - D / 100)
                ent = None
                for p in idx_after:
                    if lo[p] <= dip_level:
                        ent = p; break
                if ent is None:
                    rec[f"fill_{D}"] = False
                    rec[f"pnl_{D}"] = None        # 押さず=ノートレード
                else:
                    rec[f"fill_{D}"] = True
                    exit_close = cl[idx_after[-1]]
                    rec[f"pnl_{D}"] = (exit_close - dip_level) / dip_level * 100
            out.append(rec)
    return out


def _report(title, recs, dips):
    n = len(recs)
    if n == 0:
        print(f"\n【{title}】 シグナル 0件"); return
    print(f"\n【{title}】 約定シグナル {n}件 (= 逆指値ブレイクが約定した回数)")
    print(f"  {'押し目':<8}{'押し目約定率':>12}{'平均損益(約定時)':>16}"
          f"{'期待値/ｼｸﾞﾅﾙ':>14}{'合計(20万/件)':>15}")
    print("  " + "-" * 64)
    for D in dips:
        filled = [r[f"pnl_{D}"] for r in recs if r.get(f"fill_{D}")]
        fr = len(filled) / n * 100
        avg = sum(filled) / len(filled) if filled else 0.0
        exp = fr / 100 * avg                       # 出さない日=0込み
        yen = exp / 100 * 200_000 * n
        print(f"  -{D:<6.1f}{fr:>10.1f}%{avg:>+15.3f}%{exp:>+13.3f}%{yen:>+13,.0f}円")


def main():
    ap = argparse.ArgumentParser(description="押し目買い→同日戻り の5分足検証")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--dips", type=float, nargs="+", default=DIPS)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--strategy", choices=list(STRATS.keys()), default=None)
    ap.add_argument("--symbols-file", default=None,
                    help="SELECTED=[(code,name,strat),...] (holdout_selected_symbols.py)")
    args = ap.parse_args()

    if args.strategy:
        for s in list(STRATS.keys()):
            if s != args.strategy:
                STRATS.pop(s)

    sym_allowed: dict[str, set] = {}
    if args.symbols_file:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_selmod", args.symbols_file)
        _m = importlib.util.module_from_spec(spec); spec.loader.exec_module(_m)
        names = {}
        for code, nm, strat in _m.SELECTED:
            if args.strategy and strat != args.strategy:
                continue
            if strat not in STRATS:
                continue
            sym_allowed.setdefault(code, set()).add(strat)
            names[code] = nm
        universe = [(c, names[c]) for c in sym_allowed]
        src = f"holdout選定 {len(_m.SELECTED)}ペア"
    else:
        universe = SYMBOLS[:args.limit] if args.limit else SYMBOLS
        src = f"プライム{len(universe)}銘柄"

    print(f"押し目買い→戻り 5分足検証: {src} / 直近{args.days}日 / "
          f"戦略 {','.join(STRATS)} / 押し目 {args.dips}", flush=True)

    recs = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_verify_symbol, s, n, args.days, args.dips,
                          sym_allowed.get(s)): s for s, n in universe}
        for f in as_completed(futs):
            try:
                recs.extend(f.result())
            except Exception:
                pass
            done += 1
            if done % 100 == 0 or done == len(universe):
                print(f"  {done}/{len(universe)} 完了 (約定 {len(recs)}件)", flush=True)

    if not recs:
        print("約定シグナルがありませんでした。"); return

    _report("全戦略 合計", recs, args.dips)
    for strat in STRATS:
        _report(strat, [r for r in recs if r["strat"] == strat], args.dips)

    print("\n" + "=" * 70)
    print("読み方:")
    print("  ・押し目約定率=ブレイク約定後に その深さまで押した割合 (押し目買いの成立率)")
    print("  ・平均損益(約定時)=押し目で買い→14:55引け決済 のLONG平均% (戻りを取れたか)")
    print("  ・期待値/ｼｸﾞﾅﾙ がスリッページ(≒0.1%)超で残れば、『押して戻る』を取れている")
    print("=" * 70)


if __name__ == "__main__":
    main()
