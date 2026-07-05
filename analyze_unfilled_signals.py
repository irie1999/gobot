"""
analyze_unfilled_signals.py — 非約定シグナルの「その後」を追跡する
=====================================================================

逆指値シグナルは「翌日以降に株価が前日終値+ATR×em まで上がったとき」だけ約定する。
ENTRY_EXPIRE(=3) 営業日以内に発動しなかったシグナル = **非約定シグナル** は、
現状どのバックテストにも記録されず捨てられている。

このスクリプトはその「捨てられたシグナル」を拾い、約定日(発火しなかった)後
N営業日(既定10)の株価推移を追って、以下を定量化する:

  ① 株価推移: 非約定後に上がったか/下がったか (=乗り遅れ vs 正しく回避)
  ② 仮約定損益: 「もし非約定でも signal日終値で成行参入していたら」の損益
                (stop/target/タイムカットを N日内で適用)

【仮説の検証】
  非約定シグナルの多数が "その後マイナス" なら → 逆指値のフィルター効果が証明される。
  逆に多数が "その後プラス" なら → 逆指値が遅すぎて取り逃している証拠 → em の調整余地。

使い方:
  python analyze_unfilled_signals.py                  # 365日, conservative, 追跡10日
  python analyze_unfilled_signals.py --days 180
  python analyze_unfilled_signals.py --track 15       # 追跡日数を変更
  python analyze_unfilled_signals.py --aggressive
  python analyze_unfilled_signals.py --family stop    # 逆指値Bのみ
  python analyze_unfilled_signals.py --family breakout
  python analyze_unfilled_signals.py --workers 8

※ ロング(逆指値買い)シグナルのみ対象。約定条件は hi >= order_p (entry_type=stop)。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# --aggressive は import 前に環境変数へ
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

import numpy as np
import pandas as pd

from backtest_limit_entry import (
    fetch, ENTRY_EXPIRE, MAX_HOLD, WORKERS as _DEFAULT_WORKERS,
    MIN_PRICE, MAX_PRICE, MAX_ATR_RATIO,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY,
)


def load_targets(family: str):
    """WATCHLIST から (symbol, name, strategy, calc_fn, em, sm, tm) を作る。"""
    targets = []
    if family in ("all", "stop"):
        import check_signals_stop as _stop
        for sym, name, strat in _stop.WATCHLIST:
            calc_fn, em, sm, tm = _stop.STRATEGY_PARAMS[strat]
            targets.append((sym, name, strat, calc_fn, em, sm, tm))
    if family in ("all", "breakout"):
        import check_signals_breakout as _brk
        for sym, name, strat in _brk.WATCHLIST:
            calc_fn, em, sm, tm = _brk.STRATEGY_PARAMS[strat]
            targets.append((sym, name, strat, calc_fn, em, sm, tm))
    return targets


def analyze_one(target, days: int, track: int) -> dict | None:
    """1銘柄×1戦略の非約定シグナルを追跡する。"""
    sym, name, strat, calc_fn, em, sm, tm = target
    df = fetch(sym)
    if df is None or df.empty:
        return None
    try:
        df = calc_fn(df)
    except Exception:
        return None
    if "entry_sig" not in df.columns or "atr" not in df.columns:
        return None

    # 対象期間: 直近 days 日。ただし追跡 track 日ぶんの余白を残す
    n = len(df)
    cutoff_idx = 0
    if days and days < n:
        # ざっくり: 末尾 days 本を対象 (営業日 ≈ days*0.7 だが単純化で days 本)
        cutoff_idx = max(0, n - days)

    hi = df["high"].to_numpy()
    cl = df["close"].to_numpy()
    atr = df["atr"].to_numpy()
    sig = df["entry_sig"].fillna(False).to_numpy()

    res = {"signals": 0, "filled": 0, "unfilled": 0,
           "uf_up": 0, "uf_down": 0, "uf_flat": 0,
           "uf_ret_sum": 0.0,            # 非約定後 track日終値リターン合計(%)
           "hypo_trades": 0, "hypo_wins": 0,
           "hypo_pnl": 0.0,              # 仮約定(signal終値成行)損益合計(円)
           "hypo_target": 0, "hypo_stop": 0, "hypo_timeout": 0}

    # シグナルは「prev 行(i-1)に entry_sig」→ 約定判定は i 以降
    for i in range(max(1, cutoff_idx), n):
        if not sig[i - 1]:
            continue
        atr_prev = atr[i - 1]
        close_prev = cl[i - 1]
        if not (atr_prev > 0) or pd.isna(close_prev):
            continue
        if not (MIN_PRICE <= close_prev <= MAX_PRICE):
            continue
        if atr_prev / close_prev > MAX_ATR_RATIO:
            continue

        order_p = close_prev + atr_prev * em
        sl      = order_p - atr_prev * sm
        tp      = order_p + atr_prev * tm
        if not (order_p > 0 and sl > 0 and tp > order_p):
            continue

        res["signals"] += 1

        # 約定判定: ENTRY_EXPIRE 営業日以内に hi >= order_p か
        expire = min(i + ENTRY_EXPIRE, n - 1)
        filled = any(hi[j] >= order_p for j in range(i, expire + 1))
        if filled:
            res["filled"] += 1
            continue

        # ── 非約定シグナル ──
        res["unfilled"] += 1
        # 追跡開始は約定ウィンドウ終了の翌日 (= expire+1)。データ端で打ち切り
        track_start = expire
        track_end = min(track_start + track, n - 1)
        if track_end <= track_start:
            continue

        # ① 株価推移: signal終値 → track_end終値のリターン%
        ret = (cl[track_end] - close_prev) / close_prev * 100.0
        res["uf_ret_sum"] += ret
        if ret > 1.0:
            res["uf_up"] += 1
        elif ret < -1.0:
            res["uf_down"] += 1
        else:
            res["uf_flat"] += 1

        # ② 仮約定損益: signal翌日(i)の終値で成行参入したと仮定し、
        #    track日内で stop/target/タイムカットを適用
        ep = round(cl[i] * (1.0 + SLIPPAGE_STOP_PCT))   # 成行=逆指値同等の不利スリッページ
        hold_end = min(i + MAX_HOLD, n - 1)
        exit_p = cl[hold_end]
        reason = "timeout"
        for j in range(i + 1, hold_end + 1):
            if hi[j] >= tp:
                exit_p = tp
                reason = "target"
                break
            if cl[j] <= sl:                              # close方式損切り
                exit_p = round(sl * (1.0 - SLIPPAGE_STOP_PCT))
                reason = "stop"
                break
        fee = (ep + exit_p) * 100 * FEE_PCT_ONE_WAY
        pnl = (exit_p - ep) * 100 - fee
        res["hypo_trades"] += 1
        res["hypo_pnl"] += pnl
        if pnl > 0:
            res["hypo_wins"] += 1
        res[f"hypo_{reason}"] += 1

    res["strategy"] = strat
    return res


def main() -> int:
    ap = argparse.ArgumentParser(
        description="非約定シグナルのその後を追跡しフィルター効果を検証する")
    ap.add_argument("--days", type=int, default=365, help="対象期間 (既定365)")
    ap.add_argument("--track", type=int, default=10, help="非約定後の追跡営業日 (既定10)")
    ap.add_argument("--family", choices=["all", "stop", "breakout"], default="all")
    ap.add_argument("--aggressive", action="store_true")
    ap.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    args = ap.parse_args()

    mode = os.getenv("TRADING_MODE", "conservative")
    targets = load_targets(args.family)
    print("=" * 78)
    print(f"非約定シグナル追跡  期間={args.days}日  追跡={args.track}営業日  "
          f"モード={mode}  対象={len(targets)}銘柄×戦略 ({args.family})")
    print("  逆指値が3営業日以内に発動しなかったシグナルの「その後」を集計")
    print("=" * 78)

    by_strat = defaultdict(lambda: defaultdict(float))
    overall = defaultdict(float)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(analyze_one, t, args.days, args.track): t for t in targets}
        done = 0
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            print(f"\r  進捗 {done}/{len(targets)}", end="", flush=True)
            if not r:
                continue
            strat = r.pop("strategy")
            for k, v in r.items():
                by_strat[strat][k] += v
                overall[k] += v
    print()

    def _report(label: str, a: dict):
        sig = a["signals"]; fil = a["filled"]; uf = a["unfilled"]
        if sig == 0:
            return
        fill_pct = fil / sig * 100
        print(f"\n■ {label}  シグナル{int(sig)}  約定{int(fil)}({fill_pct:.0f}%)  "
              f"非約定{int(uf)}")
        if uf == 0:
            print("  非約定なし")
            return
        up, down, flat = int(a["uf_up"]), int(a["uf_down"]), int(a["uf_flat"])
        avg_ret = a["uf_ret_sum"] / uf
        print(f"  ① 非約定後{args.track}日の株価: "
              f"上昇{up}({up/uf*100:.0f}%) / 下落{down}({down/uf*100:.0f}%) / "
              f"横ばい{flat}  平均リターン{avg_ret:+.2f}%")
        ht = int(a["hypo_trades"])
        if ht:
            hwr = a["hypo_wins"] / ht * 100
            print(f"  ② 仮約定(signal終値成行で参入)損益: {a['hypo_pnl']:>+12,.0f}円  "
                  f"勝率{hwr:.0f}%  "
                  f"(目標{int(a['hypo_target'])}/損切{int(a['hypo_stop'])}/"
                  f"timeout{int(a['hypo_timeout'])})")

    strat_order = ["RSI2", "MACD", "A7", "DON", "VOL", "MOM"]
    for strat in [s for s in strat_order if s in by_strat]:
        _report(strat, by_strat[strat])

    print("\n" + "=" * 78)
    _report("全体合計", overall)
    print("=" * 78)

    # 結論コメント
    uf = overall["unfilled"]
    if uf:
        down_pct = overall["uf_down"] / uf * 100
        hypo_pnl = overall["hypo_pnl"]
        print(f"\n【結論】非約定シグナルの {down_pct:.0f}% は{args.track}日後に下落。")
        if hypo_pnl < 0:
            print(f"  仮に約定していた場合の損益は {hypo_pnl:+,.0f}円 (赤字)。")
            print("  → 逆指値が約定を見送ったのは正解。フィルター効果あり ✓")
        else:
            print(f"  仮に約定していた場合の損益は {hypo_pnl:+,.0f}円 (黒字)。")
            print("  → 非約定でも入れていれば勝てた。em を下げて約定しやすくする余地あり。")
    print("\n※ 仮約定損益はスリッページ(+0.5%)・手数料(往復0.2%)込み・100株固定で計算。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
