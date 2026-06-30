"""
verify_sameday_short_intraday.py  ―  スイング逆指値シグナルの『同日ショート』5分足本検証
==================================================================
日足分析の限界(下げが約定の前か後か不明)を、5分足で完全に解決する決定版検証。

【検証する仮説】
  スイング逆指値ロングが約定した瞬間に空売りし、同日中に下値ターゲットで
  買い戻せば利益になるか? (約定『後』に本当に下げが来るか)

【方法 (順番の問題を解決)】
  1. 5分足を日足にリサンプルし、MACD/A7/RSI2 の逆指値ロングシグナルを検出
       order_p = 当日終値 + ATR*EM   (EM=0 → 前日終値)
  2. シグナル後 ENTRY_EXPIRE 日以内に、5分足で高値が order_p を上抜けた
     『最初のバー』= 約定の瞬間を特定 (寄りで既に上なら寄り値)
  3. その約定バー『より後』の5分足だけを見て同日ショートを評価:
       - 後続安値が order_p*(1-target) に届けば +target% で利確
       - 届かなければ 14:55 引けで買い戻し ((fill-close)/fill)
     → 下げが『約定後』に来た分だけを正しくカウント
  4. 直近 --days 日(5分足が揃う期間)の全約定で集計

【使い方】
  python verify_sameday_short_intraday.py --days 60 --workers 8
  python verify_sameday_short_intraday.py --days 60 --limit 200   # 先頭200銘柄(試し)
  python verify_sameday_short_intraday.py --strategy RSI2
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

# スイング逆指値 全6戦略 (stop: MACD/A7/RSI2 + breakout: DON/VOL/MOM) すべて em=0
STRATS = {
    "MACD": calc_macd, "A7": calc_a7, "RSI2": calc_rsi2,
    "DON": calc_donchian, "VOL": calc_vol_breakout, "MOM": calc_momentum,
}
EM = 0.0                       # 逆指値オフセット (0 = 前日終値ちょうど)
TARGETS = [0.5, 1.0, 1.5, 2.0]  # 同日ショートの下値利確ターゲット(%)
FORCE_CLOSE = dtime(14, 55)
DATA_DAYS = 400                # 日足シグナル計算に必要な履歴(5分足から作る)


def _resample_daily(df5: pd.DataFrame) -> pd.DataFrame:
    g = df5.groupby(df5.index.date)
    daily = pd.DataFrame({
        "open":   g["open"].first(),
        "high":   g["high"].max(),
        "low":    g["low"].min(),
        "close":  g["close"].last(),
        "volume": g["volume"].sum(),
    })
    daily.index = pd.to_datetime(daily.index)
    return daily


def _verify_symbol(sym: str, name: str, days: int) -> list[dict]:
    """1銘柄の全シグナル約定について同日ショート結果を返す。"""
    try:
        df5 = load_intraday(sym, days=DATA_DAYS, source="local")
    except Exception:
        return []
    if df5 is None or df5.empty or len(df5) < 1000:
        return []

    daily = _resample_daily(df5)
    if len(daily) < 210:
        return []
    by5 = split_by_day(df5)                 # date -> 5分足df
    dindex = list(daily.index)
    cutoff = datetime.now(JST).date() - timedelta(days=days)

    out = []
    for strat, calc_fn in STRATS.items():
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
            sig_date = dindex[j].date()
            if sig_date < cutoff:
                continue
            order_p = close[j] + atr[j] * EM
            if order_p <= 0:
                continue
            # ── 約定検出: 翌日以降 ENTRY_EXPIRE 日で 5分足高値が order_p を上抜け ──
            fill = None
            for k in range(j + 1, min(j + 1 + ENTRY_EXPIRE, len(dindex))):
                fdate = dindex[k].date()
                bars = by5.get(fdate)
                if bars is None or len(bars) < 3:
                    continue
                o = bars["open"].to_numpy(dtype=float)
                hi = bars["high"].to_numpy(dtype=float)
                lo = bars["low"].to_numpy(dtype=float)
                cl = bars["close"].to_numpy(dtype=float)
                tm = bars.index
                if o[0] >= order_p:                     # 寄りで既に上 → 寄り成り約定
                    fill = (fdate, 0, o[0], hi, lo, cl, tm)
                    break
                cross = np.where(hi >= order_p)[0]
                if len(cross) > 0:
                    bi = int(cross[0])
                    fill = (fdate, bi, order_p, hi, lo, cl, tm)
                    break
            if fill is None:
                continue
            fdate, bi, fp, hi, lo, cl, tm = fill
            if fp <= 0:
                continue
            # ── 約定『後』の5分足だけで同日ショート評価 (14:55まで) ──
            mask_after = [(p > bi) and (tm[p].time() <= FORCE_CLOSE)
                          for p in range(len(tm))]
            idx_after = [p for p, m in enumerate(mask_after) if m]
            if not idx_after:
                # 約定が引け間際 → カバーは即引け ≒ トントン
                cover_close = cl[bi]
            else:
                cover_close = cl[idx_after[-1]]
            lows_after = [lo[p] for p in idx_after]
            rec = {"sym": sym, "strat": strat, "fill_date": fdate, "fp": fp}
            for tg in TARGETS:
                lvl = fp * (1 - tg / 100)
                hit = any(l <= lvl for l in lows_after)
                rec[f"hit_{tg}"] = hit
                rec[f"pnl_{tg}"] = tg if hit else (fp - cover_close) / fp * 100
            rec["pnl_close"] = (fp - cover_close) / fp * 100   # 引け買戻しのみ
            out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser(description="同日ショート 5分足 本検証")
    ap.add_argument("--days", type=int, default=60,
                    help="対象シグナル期間 (直近N日。5分足が揃う範囲。既定60)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(試し)")
    ap.add_argument("--strategy", choices=list(STRATS.keys()), default=None)
    args = ap.parse_args()

    if args.strategy:
        for s in list(STRATS.keys()):
            if s != args.strategy:
                STRATS.pop(s)

    universe = SYMBOLS[:args.limit] if args.limit else SYMBOLS
    print(f"同日ショート 5分足検証: {len(universe)}銘柄 / 直近{args.days}日 / "
          f"戦略 {','.join(STRATS)} / EM={EM}", flush=True)

    recs: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_verify_symbol, s, n, args.days): s for s, n in universe}
        for f in as_completed(futs):
            try:
                recs.extend(f.result())
            except Exception:
                pass
            done += 1
            if done % 100 == 0 or done == len(universe):
                print(f"  {done}/{len(universe)} 完了 (約定 {len(recs)}件)", flush=True)

    if not recs:
        print("約定シグナルがありませんでした (5分足の期間/データを確認)。")
        return

    _report("全戦略 合計", recs)
    for strat in STRATS:
        _report(strat, [r for r in recs if r["strat"] == strat])

    print("\n" + "=" * 76)
    print("読み方:")
    print("  ・これは『約定後』の下げだけを5分足で正確に数えた値 (順番問題は解決済)")
    print("  ・平均期待値/件 がプラスでも、往復スリッページ(≒0.1%)+貸株料を引いて")
    print("    なお残るか で採否を判断する。")
    print("=" * 76)


def _report(title: str, recs: list[dict]):
    n = len(recs)
    if n == 0:
        print(f"\n【{title}】 約定 0件")
        return
    print(f"\n【{title}】 約定 {n}件")
    avg_close = sum(r["pnl_close"] for r in recs) / n
    print(f"  引け買戻しのみ(ターゲット無)     : 平均 {avg_close:+.3f}%/件")
    print(f"  {'ターゲット':<8}{'到達率(約定後)':>14}{'平均期待値/件':>14}"
          f"{'合計(20万/件概算)':>18}")
    print("  " + "-" * 56)
    for tg in TARGETS:
        hits = sum(1 for r in recs if r[f"hit_{tg}"])
        avg = sum(r[f"pnl_{tg}"] for r in recs) / n
        yen = avg / 100 * 200_000 * n
        print(f"  -{tg:<6.1f}{hits/n*100:>12.1f}%{avg:>+13.3f}%{yen:>+16,.0f}円")


if __name__ == "__main__":
    main()
