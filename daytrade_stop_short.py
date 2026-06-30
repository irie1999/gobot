"""
daytrade_stop_short.py  ―  デイトレ・ショート (スイング逆指値ロジックのミラー)
==================================================================
【背景】
  スイングの逆指値ロング (close_prev + atr*em で買い) は、約定直後に
  一度下げて含み損になることが多い、というユーザー観察に基づく。
  そこで逆指値を「下抜けで売り」にミラーし、同日内で下落を取りにいく
  デイトレ・ショート戦略。

【設計の経緯】
  当初スイングの逆指値(日足ATR×1.5/3.0)をそのままミラーしたが、日足ATRは
  同日決済には遠すぎ、87%が引け強制になりエッジが出ず、さらにショートは上方
  スパイクに無防備で口座が吹き飛ぶ破綻を確認。そこで同日決済(デイトレ)向けに
  intraday %スケールへ再設計した。

【エントリー】
  前日終値 close_prev から
    order_p = close_prev × (1 - EM_PCT)    (0.3% 下を下抜け＝下方ブレイク確認)
  当日ザラ場の安値が order_p 以下になったら逆指値売りで約定
  (寄りで既に下なら寄り値で約定 = ギャップスルー)。ENTRY_CUTOFF まで。1日1トレード。
  寄りが前日終値から±MAX_GAP_PCT 超の異常日はスキップ (スパイク対策)。

【決済 (同日)】
  損切り: 約定 × (1 + STOP_PCT)   (上に逆行)
  目標:   約定 × (1 - TARGET_PCT) (下落達成、R:R 2.0)
  損切り/目標は引け強制より先に判定しテールを必ず止める。
  同一バー両ヒットは「損切り優先」(ショートは保守側)。
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
# 同日決済(デイトレ)向けの intraday スケール (%ベース)。
# 日足ATR×(1.5/3.0) のスイング値は同日には遠すぎ、ほぼ引け強制になり破綻したため変更。
EM_PCT       = 0.003   # 前日終値 ×(1-0.3%) を下抜けで逆指値売り (下方ブレイク確認)
STOP_PCT     = 0.004   # 損切り = 約定 ×(1+0.4%)  (上に逆行)
TARGET_PCT   = 0.008   # 目標   = 約定 ×(1-0.8%)  (R:R = 2.0)
MAX_GAP_PCT  = 0.05    # 寄りが前日終値から±5%超は異常(スパイク/特殊)としてスキップ
FORCE_CLOSE  = dtime(14, 55)
ENTRY_CUTOFF = dtime(14, 0)
STRAT_NAME   = "StopShort"


def backtest_day(day_df, prev_close):
    if not prev_close or prev_close <= 0:
        return []

    opens  = day_df["open"].to_numpy(dtype=float)
    highs  = day_df["high"].to_numpy(dtype=float)
    lows   = day_df["low"].to_numpy(dtype=float)
    closes = day_df["close"].to_numpy(dtype=float)
    times  = day_df.index
    n = len(day_df)
    if n < 3:
        return []

    # 寄りが前日終値から極端に乖離する日は異常データの疑い → スキップ (ショートのテール対策)
    if abs(opens[0] - prev_close) / prev_close > MAX_GAP_PCT:
        return []

    order_p = prev_close * (1 - EM_PCT)

    trades = []
    state = "idle"
    entry_p = stop_p = target_p = entry_dt = None
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
            # ── 損切り/目標を引け強制より先に判定 (テールを必ず止める) ──
            hit_stop = hi >= stop_p
            hit_tgt  = lo <= target_p
            if hit_stop:                                   # 同バー両ヒットは損切り優先(保守)
                _finish(stop_p, times[i], "損切り")
                state = "idle"; i += 1; continue
            if hit_tgt:
                _finish(target_p, times[i], "目標達成")
                state = "idle"; i += 1; continue
            if t >= FORCE_CLOSE:
                _finish(cl, times[i], "引け強制")
                state = "idle"; break
            i += 1
            continue

        # ── エントリー: 逆指値売りが下抜けで約定 ──
        if t >= ENTRY_CUTOFF:
            i += 1
            continue
        if lo <= order_p:
            fill = min(order_p, op)         # 寄りで既に下ならギャップスルーで寄り値
            stop0 = fill * (1 + STOP_PCT)
            q = calc_position_size(fill, stop0, BUDGET, MAX_RISK)
            if q <= 0:
                i += 1
                continue
            entry_p = fill
            stop_p = stop0
            target_p = fill * (1 - TARGET_PCT)
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
    if len(dates) < 2:
        return None
    trades = []
    prev_close = None
    for d in dates:
        if prev_close is not None:
            trades.extend(backtest_day(daily[d], prev_close))
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
          f"(下抜け{EM_PCT*100:.1f}% 損切+{STOP_PCT*100:.1f}% 目標-{TARGET_PCT*100:.1f}%)",
          flush=True)

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
