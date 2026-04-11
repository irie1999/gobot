"""
scan_walkforward.py  ―  Walk-forward 方式による銘柄スキャン
=================================================================
225銘柄 × 6戦略 (MACD/A7/RSI2 逆指値B + DON/VOL/MOM ブレイクアウト) に対し、
非重複の 3 fold Walk-forward バックテストを実行し、
TRAIN (選定用) で勝ち、かつ TEST (検証用) でも勝つ銘柄を抽出する。

【Walk-forward 構造】 (基準日=本日, 単位=暦日)
  Fold 1: TRAIN 730〜540日前  /  TEST 540〜360日前
  Fold 2: TRAIN 540〜360日前  /  TEST 360〜180日前
  Fold 3: TRAIN 360〜180日前  /  TEST 180〜  0日前

【合格条件】
  TRAIN: trades>=3, PF>=1.5, win_rate>=55%, total_pnl>0
  TEST : trades>=2, PF>=1.2, win_rate>=45%, total_pnl>0
  選定 : 3 folds 中 TRAIN+TEST 両方合格が 2 以上

【出力】
  walkforward_results/walkforward_<STRATEGY>_<YYYY-MM-DD>.csv

【使い方】
  python scan_walkforward.py                      # 全戦略 (6つ)
  python scan_walkforward.py --family stop        # 逆指値Bのみ
  python scan_walkforward.py --family breakout    # ブレイクアウトのみ
  python scan_walkforward.py --workers 8
  python scan_walkforward.py --top 50             # 表示する上位N

注意:
  - backtest_limit_entry.run_limit_backtest を内部で使う。 _TODAY は触らない
    (スレッドセーフ) ので、df を事前にトリミングして backtest_days パラメータで
    ウィンドウを制御している。
  - TRAIN と TEST は時期をずらした非重複ウィンドウ。TEST は擬似的な "未来データ" 扱い。
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)
from scan_breakout_entry import (
    calc_donchian, calc_vol_breakout, calc_momentum,
)
from symbols_all import SYMBOLS
from risk_metrics import enrich_backtest_result

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# ── 戦略定義 ─────────────────────────────────────────────────────
# (calc_fn, entry_atr_mult, stop_atr_mult, target_atr_mult, family)
STRATEGY_DEFS: dict[str, tuple] = {
    "MACD": (calc_macd,        0.0, 1.5, 3.0, "stop"),
    "A7":   (calc_a7,          0.0, 1.5, 3.0, "stop"),
    "RSI2": (calc_rsi2,        0.0, 2.0, 4.0, "stop"),
    "DON":  (calc_donchian,    0.0, 1.5, 3.0, "breakout"),
    "VOL":  (calc_vol_breakout,0.0, 1.5, 3.0, "breakout"),
    "MOM":  (calc_momentum,    0.0, 1.5, 3.0, "breakout"),
}

# ── Walk-forward fold 定義 (days ago from today) ──
# (name, train_start, train_end, test_start, test_end)  すべて "今日からN日前"
FOLDS: list[tuple[str, int, int, int, int]] = [
    ("fold1", 730, 540, 540, 360),
    ("fold2", 540, 360, 360, 180),
    ("fold3", 360, 180, 180,   0),
]

# ── 合格閾値 ─────────────────────────────────────────────────────
TRAIN_MIN_TRADES = 3
TRAIN_MIN_PF     = 1.5
TRAIN_MIN_WR     = 55.0
TEST_MIN_TRADES  = 2
TEST_MIN_PF      = 1.2
TEST_MIN_WR      = 45.0

INITIAL_CASH = 500_000


# ── 1 ウィンドウ分のバックテスト ─────────────────────────────────
def _run_window(symbol: str, name: str, full_df: pd.DataFrame,
                calc_fn, em: float, sm: float, tm: float,
                start_days_ago: int, end_days_ago: int,
                strategy_name: str, entry_type: str = "stop") -> dict | None:
    """
    指定ウィンドウ [today-start, today-end] で run_limit_backtest を実行。

    スレッドセーフのため _TODAY は触らず、df を end_days_ago までトリミングして
    backtest_days で start_days_ago を指定する。
    """
    window_end   = TODAY - timedelta(days=end_days_ago)
    window_start = TODAY - timedelta(days=start_days_ago)

    # ウィンドウ終了日までに df をトリミング (未来データを見ない)
    df_trimmed = full_df[full_df.index <= pd.Timestamp(window_end)].copy()
    if len(df_trimmed) < 60:
        return None

    # run_limit_backtest 内部の cutoff = _TODAY - backtest_days を
    # window_start に一致させる
    backtest_days = (TODAY - window_start).days
    if backtest_days <= 0:
        return None

    try:
        result = run_limit_backtest(
            symbol, name, df_trimmed, calc_fn,
            em, sm, tm, backtest_days,
            strategy_name, entry_type=entry_type,
        )
    except Exception:
        return None

    return enrich_backtest_result(result, INITIAL_CASH)


# ── 合否判定 ─────────────────────────────────────────────────────
def _passes_train(r: dict | None) -> bool:
    if not r:
        return False
    return (r.get("trades", 0)   >= TRAIN_MIN_TRADES
        and r.get("pf", 0)       >= TRAIN_MIN_PF
        and r.get("win_rate", 0) >= TRAIN_MIN_WR
        and r.get("total_pnl", 0) > 0)


def _passes_test(r: dict | None) -> bool:
    if not r:
        return False
    return (r.get("trades", 0)   >= TEST_MIN_TRADES
        and r.get("pf", 0)       >= TEST_MIN_PF
        and r.get("win_rate", 0) >= TEST_MIN_WR
        and r.get("total_pnl", 0) > 0)


# ── 1 銘柄 × 1 戦略 × 3 fold ─────────────────────────────────────
def walkforward_one(symbol: str, name: str, strategy_name: str) -> dict | None:
    calc_fn, em, sm, tm, family = STRATEGY_DEFS[strategy_name]

    full_df = fetch(symbol, 800)   # Walk-forward には ~2年のデータが必要
    if full_df is None or len(full_df) < 400:
        return None

    folds_passed  = 0
    train_results: list[dict] = []
    test_results:  list[dict] = []
    fold_detail:   list[dict] = []

    for fold_name, ts, te, vs, ve in FOLDS:
        train_r = _run_window(symbol, name, full_df, calc_fn, em, sm, tm,
                              ts, te, strategy_name)
        test_r  = _run_window(symbol, name, full_df, calc_fn, em, sm, tm,
                              vs, ve, strategy_name)

        pass_train = _passes_train(train_r)
        pass_test  = _passes_test(test_r)
        if pass_train and pass_test:
            folds_passed += 1

        if train_r:
            train_results.append(train_r)
        if test_r:
            test_results.append(test_r)

        fold_detail.append(dict(
            fold=fold_name, pass_train=pass_train, pass_test=pass_test,
            train_trades=train_r["trades"] if train_r else 0,
            train_pf=train_r["pf"] if train_r else 0,
            train_wr=train_r["win_rate"] if train_r else 0,
            train_pnl=train_r["total_pnl"] if train_r else 0,
            test_trades=test_r["trades"] if test_r else 0,
            test_pf=test_r["pf"] if test_r else 0,
            test_wr=test_r["win_rate"] if test_r else 0,
            test_pnl=test_r["total_pnl"] if test_r else 0,
        ))

    if not test_results:
        return None

    # ── TEST 期間の集約 (3 fold の trade_log を全結合してリスク指標計算) ──
    all_test_trades: list[dict] = []
    total_test_pnl  = 0.0
    total_test_tr   = 0
    for r in test_results:
        all_test_trades.extend(r.get("trade_log", []))
        total_test_pnl += r.get("total_pnl", 0.0)
        total_test_tr  += r.get("trades", 0)

    # 結合トレードログからリスク指標
    agg = enrich_backtest_result({"trade_log": all_test_trades}, INITIAL_CASH)

    # TEST fold の PF / WR 平均 (∞は10キャップ)
    def _cap_pf(p):
        return 10.0 if p == float("inf") else min(p, 10.0)

    avg_test_pf = sum(_cap_pf(r.get("pf", 0)) for r in test_results) / max(len(test_results), 1)
    avg_test_wr = sum(r.get("win_rate", 0) for r in test_results) / max(len(test_results), 1)

    # TRAIN との劣化率 (overfit 検出用)
    train_pnl_sum = sum(r.get("total_pnl", 0.0) for r in train_results)
    test_pnl_sum  = total_test_pnl
    if train_pnl_sum > 0:
        degradation = (train_pnl_sum - test_pnl_sum) / train_pnl_sum * 100
    else:
        degradation = 0.0

    return dict(
        symbol=symbol, name=name, strategy=strategy_name, family=family,
        folds_passed=folds_passed,
        total_test_trades=total_test_tr,
        total_test_pnl=round(total_test_pnl, 0),
        total_train_pnl=round(train_pnl_sum, 0),
        avg_test_pf=round(avg_test_pf, 2),
        avg_test_wr=round(avg_test_wr, 1),
        max_drawdown_pct=agg.get("max_drawdown_pct", 0.0),
        max_consecutive_losses=agg.get("max_consecutive_losses", 0),
        sharpe=agg.get("sharpe", 0.0),
        recovery_factor=agg.get("recovery_factor", 0.0),
        train_to_test_degradation_pct=round(degradation, 1),
    )


# ── メイン ───────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward 銘柄スキャナー")
    parser.add_argument("--family",  choices=["stop", "breakout", "both"], default="both")
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    parser.add_argument("--top",     type=int, default=30,
                        help="表示する上位N (CSVは全件保存)")
    args = parser.parse_args()

    if args.family == "stop":
        strategies = ["MACD", "A7", "RSI2"]
    elif args.family == "breakout":
        strategies = ["DON", "VOL", "MOM"]
    else:
        strategies = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]

    print("=" * 78)
    print(f"Walk-forward スキャン開始")
    print(f"  基準日   : {TODAY}")
    print(f"  ユニバース: {len(SYMBOLS)} 銘柄")
    print(f"  戦略     : {', '.join(strategies)}")
    print(f"  Folds    : {len(FOLDS)}")
    print(f"  Workers  : {args.workers}")
    print("=" * 78)
    print("Fold 構造:")
    for name, ts, te, vs, ve in FOLDS:
        train_s = (TODAY - timedelta(days=ts)).isoformat()
        train_e = (TODAY - timedelta(days=te)).isoformat()
        test_s  = (TODAY - timedelta(days=vs)).isoformat()
        test_e  = (TODAY - timedelta(days=ve)).isoformat()
        print(f"  {name}: TRAIN {train_s}〜{train_e}  /  TEST {test_s}〜{test_e}")
    print("=" * 78)

    out_dir = Path("walkforward_results")
    out_dir.mkdir(exist_ok=True)

    for strategy in strategies:
        print(f"\n=== {strategy} ===")
        results: list[dict] = []

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(walkforward_one, sym, name, strategy): sym
                    for sym, name in SYMBOLS}
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                    if r:
                        results.append(r)
                except Exception:
                    pass
                if done % 25 == 0:
                    print(f"  進捗: {done}/{len(SYMBOLS)}  候補: {len(results)}",
                          flush=True)

        survivors = [r for r in results if r["folds_passed"] >= 2]
        survivors.sort(key=lambda r: r["total_test_pnl"], reverse=True)

        print(f"\n  {strategy}: 全候補={len(results)}  2fold以上通過={len(survivors)}")

        # ── CSV 保存 (全候補) ──
        csv_path = out_dir / f"walkforward_{strategy}_{TODAY}.csv"
        fields = [
            "symbol", "name", "strategy", "family", "folds_passed",
            "total_test_trades", "total_test_pnl", "total_train_pnl",
            "avg_test_pf", "avg_test_wr",
            "max_drawdown_pct", "max_consecutive_losses", "sharpe",
            "recovery_factor", "train_to_test_degradation_pct",
        ]
        results.sort(key=lambda r: (-r["folds_passed"], -r["total_test_pnl"]))
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in results:
                writer.writerow({k: r.get(k, "") for k in fields})
        print(f"  CSV: {csv_path}")

        # ── トップN表示 ──
        n = min(args.top, len(survivors))
        if n == 0:
            print("  (合格銘柄なし)")
            continue
        print(f"\n  ▼ {strategy} 上位 {n}:")
        print(f"  {'銘柄':<10}{'名前':<22}"
              f"{'fld':>5}{'trades':>8}{'TEST_PnL':>12}"
              f"{'PF':>7}{'WR':>8}{'MaxDD%':>9}{'連敗':>6}{'Shrp':>7}")
        print("  " + "-" * 100)
        for r in survivors[:n]:
            print(f"  {r['symbol']:<10}{r['name'][:20]:<22}"
                  f"{r['folds_passed']:>5}{r['total_test_trades']:>8}"
                  f"{r['total_test_pnl']:>+12,.0f}"
                  f"{r['avg_test_pf']:>7.2f}{r['avg_test_wr']:>7.1f}%"
                  f"{r['max_drawdown_pct']:>9.1f}"
                  f"{r['max_consecutive_losses']:>6}"
                  f"{r['sharpe']:>7.2f}")

    print(f"\n完了。CSV は {out_dir.resolve()} に保存されました。")
    print(f"次ステップ: python build_watchlist.py")


if __name__ == "__main__":
    main()
