"""
daytrade_stop_short.py  ―  デイトレ・ショート (スイング逆指値ロジックのミラー)
==================================================================
【背景】
  スイングの逆指値ロング (close_prev + atr*em で買い) は、約定直後に
  一度下げて含み損になることが多い、というユーザー観察に基づく。
  そこで逆指値を「下抜けで売り」にミラーし、同日内で下落を取りにいく
  デイトレ・ショート戦略。

【スイングとの対応 (ミラー)】
  ロング: order = close_prev + atr_prev*em / 買い / stop=order-atr*sm / target=order+atr*tm
  ショート: order = close_prev - atr_prev*em / 空売り / stop=order+atr*sm / target=order-atr*tm

【エントリー】
  前日終値 close_prev と前日 (日足) ATR atr_prev から
    order_p  = close_prev - atr_prev*EM    (EM=0.0 → 前日終値ちょうど)
  当日のザラ場で安値が order_p 以下に下抜けたら逆指値売りで約定
  (寄りで既に order_p 以下なら寄り値で約定 = ギャップスルー)。
  ENTRY_CUTOFF まで。1日1トレード (スイング同様、1シグナル1ポジション)。

【決済 (同日)】
  損切り: order_p + atr_prev*SM   (上に逆行)
  目標:   order_p - atr_prev*TM   (下落達成)
  同一バーで両方ヒット時は「目標優先」(スイング §5 と同じ慣習)
  強制:   FORCE_CLOSE (14:55) で当日引け成行

【使い方】
  python daytrade_stop_short.py --source local --days 730 --budget 600000
  # WF/OOS 検証は scan_wf_strategies.py / build_watchlist_wf.py 経由
"""

from __future__ import annotations

import argparse
from datetime import datetime, time as dtime, timedelta, timezone

import numpy as np
import pandas as pd

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size

JST = timezone(timedelta(hours=9))

BUDGET       = 600_000
MAX_RISK     = 6_000
EM           = 0.0     # 逆指値オフセット (0 = 前日終値ちょうど)
SM           = 1.5     # 損切り = order + atr*SM
TM           = 3.0     # 目標   = order - atr*TM  (R:R = 2.0)
ATR_PERIOD   = 14
FORCE_CLOSE  = dtime(14, 55)
ENTRY_CUTOFF = dtime(14, 0)
STRAT_NAME   = "StopShort"


def _daily_atr(daily: dict, dates: list) -> dict:
    """日足 OHLC から ATR(14) を算出し、各日付に『前日の ATR』を返す。"""
    rows = []
    for d in dates:
        ddf = daily[d]
        rows.append((ddf["high"].max(), ddf["low"].min(),
                     float(ddf.iloc[-1]["close"])))
    highs = np.array([r[0] for r in rows], dtype=float)
    lows  = np.array([r[1] for r in rows], dtype=float)
    closes = np.array([r[2] for r in rows], dtype=float)
    prev_c = np.concatenate([[closes[0]], closes[:-1]])
    tr = np.maximum.reduce([highs - lows,
                            np.abs(highs - prev_c),
                            np.abs(lows - prev_c)])
    atr = pd.Series(tr).ewm(span=ATR_PERIOD, adjust=False).mean().to_numpy()
    # その日のシグナルに使うのは『前日まで』の ATR
    atr_prev = {dates[i]: (atr[i - 1] if i >= 1 else np.nan)
                for i in range(len(dates))}
    return atr_prev


def backtest_day(day_df, prev_close, atr_prev):
    if not prev_close or prev_close <= 0 or not atr_prev or atr_prev <= 0:
        return []
    if np.isnan(atr_prev):
        return []

    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times  = day_df.index
    n = len(day_df)
    if n < 3:
        return []

    order_p  = prev_close - atr_prev * EM
    stop_p   = order_p + atr_prev * SM
    target_p = order_p - atr_prev * TM
    if target_p <= 0 or stop_p <= order_p:
        return []

    trades = []
    state = "idle"
    entry_p = entry_dt = None
    qty = 0

    def _finish(exit_p, exit_dt, reason):
        pnl = (entry_p - exit_p) * qty          # ショート: 売り→買い戻し
        pct = (entry_p - exit_p) / entry_p * 100
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=exit_dt,
            entry_p=entry_p, exit_p=exit_p,
            stop_p=stop_p, target_p=target_p,
            qty=qty, pnl=pnl, pct=pct,
            strategy=STRAT_NAME, reason=reason, side="short",
        ))

    i = 0
    while i < n:
        t = times[i].time()
        hi, lo, cl, op = highs[i], lows[i], closes[i], opens[i]

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                _finish(cl, times[i], "引け強制")
                state = "idle"
                break
            hit_stop = hi >= stop_p
            hit_tgt  = lo <= target_p
            if hit_tgt and hit_stop:
                _finish(target_p, times[i], "目標達成")   # 同バー両ヒットは目標優先
                state = "idle"; i += 1; continue
            if hit_tgt:
                _finish(target_p, times[i], "目標達成")
                state = "idle"; i += 1; continue
            if hit_stop:
                _finish(stop_p, times[i], "損切り")
                state = "idle"; i += 1; continue
            i += 1
            continue

        # ── エントリー: 逆指値売りが下抜けで約定 ──
        if t >= ENTRY_CUTOFF:
            i += 1
            continue
        if lo <= order_p:
            # 寄りで既に下なら寄り値で約定 (ギャップスルー)、それ以外は order_p
            fill = min(order_p, op)
            q = calc_position_size(fill, stop_p, BUDGET, MAX_RISK)
            if q <= 0:
                i += 1
                continue
            entry_p = fill
            entry_dt = times[i]
            qty = q
            state = "in_pos"
            i += 1
            continue
        i += 1

    if state == "in_pos":
        _finish(closes[-1], times[-1], "引け強制")
    return trades


def backtest_symbol(sym, name, df, budget=BUDGET, max_risk=MAX_RISK):
    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < ATR_PERIOD + 2:
        return None
    atr_prev = _daily_atr(daily, dates)
    trades = []
    prev_close = None
    for d in dates:
        if prev_close is not None:
            trades.extend(backtest_day(daily[d], prev_close, atr_prev.get(d)))
        prev_close = float(daily[d].iloc[-1]["close"])
    return dict(symbol=sym, name=name, trades=trades)


def calc_stats(trades, budget=BUDGET):
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0, max_dd=dd)


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    parser = argparse.ArgumentParser(description="デイトレ・ショート (逆指値ミラー)")
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--detail", type=int, default=0,
                        help="先頭N件の個別トレード明細を表示 (戦略の妥当性確認用)")
    args = parser.parse_args()

    targets = [(s, s) for s in args.symbols] if args.symbols else DAYTRADE_SYMBOLS
    symbols = [s for s, _ in targets]
    print(f"{STRAT_NAME}: {len(targets)}銘柄 / {args.days}日 / 予算{args.budget:,}円 "
          f"(EM={EM} SM={SM} TM={TM})", flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    items = []
    for sym, name in targets:
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, name, fetched[sym], args.budget, MAX_RISK)
        if r:
            items.append(r)

    all_trades = sorted([t for it in items for t in it["trades"]],
                        key=lambda x: str(x.get("entry_dt", "")))
    stats = calc_stats(all_trades, args.budget)
    print(f"\n取引:{stats['n']}  勝率:{stats['win_rate']:.1f}%  PF:{_pf(stats['pf'])}  "
          f"損益:{stats['total_pnl']:+,.0f}  DD:{stats['max_dd']:+.1f}%")

    # ── 戦略の妥当性チェック: 決済理由の内訳 ──
    from collections import Counter
    reasons = Counter(t.get("reason", "?") for t in all_trades)
    tot = max(1, len(all_trades))
    print("\n[決済理由の内訳] ※引け強制が大半なら目標/損切りが遠すぎ(スケール不整合)")
    for rsn, cnt in reasons.most_common():
        print(f"  {rsn:<8} {cnt:>5}件 ({cnt/tot*100:>4.0f}%)")

    if args.detail:
        print(f"\n[サンプル明細 先頭{args.detail}件] entry→exit / stop / target / 理由")
        for t in all_trades[:args.detail]:
            ed = t["entry_dt"].strftime("%Y-%m-%d %H:%M") if t.get("entry_dt") else "?"
            print(f"  {ed}  {t['entry_p']:,.1f}→{t['exit_p']:,.1f}  "
                  f"stop {t['stop_p']:,.1f} / tgt {t['target_p']:,.1f}  "
                  f"qty{t['qty']}  {t['pnl']:+,.0f}  {t['reason']}")

    print(f"\n{'='*78}")
    print(f"  銘柄別サマリ ({len(items)}銘柄)")
    print("=" * 78)
    print(f"{'銘柄':<22} {'コード':<8} {'取引':>4} {'勝率':>5} {'PF':>7} "
          f"{'損益':>10} {'DD':>6}")
    print("-" * 78)
    rows = []
    for it in items:
        if not it["trades"]:
            continue
        ist = calc_stats(it["trades"], args.budget)
        rows.append((it["name"], it["symbol"], ist))
    rows.sort(key=lambda x: x[2]["total_pnl"], reverse=True)
    for name, sym, ist in rows:
        disp = name[:20] if len(name) <= 20 else name[:19] + "…"
        print(f"{disp:<22} {sym:<8} {ist['n']:>4} {ist['win_rate']:>4.0f}% "
              f"{_pf(ist['pf']):>7} {ist['total_pnl']:>+10,.0f} "
              f"{ist['max_dd']:>+5.1f}%")
    print("=" * 78)


if __name__ == "__main__":
    main()
