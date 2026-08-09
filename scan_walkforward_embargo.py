"""
scan_walkforward_embargo.py  ―  エンバーゴ期間付きウォークフォワードスキャン
=====================================================================================
scan_walkforward.py の改良版。TRAIN と TEST の境界に「エンバーゴ期間」を挿入し、
テクニカル指標を通じた情報漏洩（ルックアヘッドバイアス）を軽減する。

【エンバーゴとは】
TRAIN が終了してから TEST が開始するまでの「バッファー期間」。

  通常のWFA:   TRAIN [============================] TEST [===============]
                                                   ↑
                                          境界がピッタリ接触
                                          MA などの指標が「滲み出す」

  エンバーゴあり: TRAIN [=====================]  EMBARGO[///]  TEST [===============]
                                                 ←7日→
                TRAIN末端の指標値がTEST開始に「知れわたる」可能性を遮断

具体例 (embargo=7):
  元:   TRAIN [今日-730日〜今日-370日] / TEST [今日-370日〜今日-180日]
  変更: TRAIN [今日-730日〜今日-377日] / TEST [今日-370日〜今日-180日]
        ← TRAINが7日短くなり、377〜370日前の7日間は使われない

【scan_walkforward.py との違い】
  - --embargo-days N オプションを追加 (デフォルト: 7)
  - TRAIN の終端を embargo_days 日だけ前倒し (te += embargo_days)
  - CSV ファイル名に _embargo{N}d サフィックスを追加
  - それ以外は scan_walkforward.py と完全に同一

【使い方】
  python scan_walkforward_embargo.py                     # エンバーゴ7日 (デフォルト)
  python scan_walkforward_embargo.py --embargo-days 14  # エンバーゴ2週間
  python scan_walkforward_embargo.py --embargo-days 0   # エンバーゴなし (元と同等)
  python scan_walkforward_embargo.py --family stop --budget 600000
  python scan_walkforward_embargo.py --workers 8 --aggressive
  python scan_walkforward_embargo.py --limit 50         # デバッグ用: 先頭50銘柄のみ

【出力】
  walkforward_results/walkforward_<STRATEGY>_embargo<N>d_<date>.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os as _os_pre
if "--aggressive" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "conservative"

from backtest_limit_entry import (
    fetch,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)
import scan_walkforward as _wf
from scan_walkforward import (
    load_universe,
    STRATEGY_DEFS,
    TRADING_MODE,
    TRAIN_MIN_TRADES, TRAIN_MIN_PF, TRAIN_MIN_WR,
    TEST_MIN_TRADES, TEST_MIN_PF, TEST_MIN_WR,
)
from risk_metrics import enrich_backtest_result

import pandas as pd

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

INITIAL_CASH = 500_000


def _build_folds(embargo_days: int, holdout_days: int) -> list[tuple]:
    """
    エンバーゴとホールドアウトを適用した FOLDS を生成する。

    TRAIN の終端 (te) を embargo_days 増やすことで、
    TRAIN末端〜TEST開始 の間に embargo_days 日の空白を作る。
    """
    # scan_walkforward の BASE FOLDS を参照
    # (scan_walkforward.pyの定義と同期させるためモジュールからコピー)
    base = [
        ("fold1", 730, 370, 370, 180),
        ("fold2", 550, 180, 180,   0),
    ]

    result = []
    for fold_name, ts, te, vs, ve in base:
        # エンバーゴ: TRAIN末端を embargo_days 前倒し
        te_emb = te + embargo_days
        # ホールドアウト: 全境界を後方にシフト
        result.append((
            fold_name,
            ts + holdout_days,
            te_emb + holdout_days,
            vs + holdout_days,
            ve + holdout_days,
        ))
    return result


# ─── 1 ウィンドウ分のバックテスト (scan_walkforward._run_window と同一) ───
def _run_window(symbol: str, name: str, full_df: pd.DataFrame,
                calc_fn, em: float, sm: float, tm: float,
                start_days_ago: int, end_days_ago: int,
                strategy_name: str, entry_type: str = "stop") -> dict | None:
    window_end   = TODAY - timedelta(days=end_days_ago)
    window_start = TODAY - timedelta(days=start_days_ago)
    df_trimmed = full_df[full_df.index <= pd.Timestamp(window_end)].copy()
    if len(df_trimmed) < 60:
        return None
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


def walkforward_one_emb(symbol: str, name: str, strategy_name: str,
                        folds: list[tuple], max_price: float = 0.0) -> dict | None:
    """1銘柄 × 1戦略 × 全fold のウォークフォワード (エンバーゴ適用版)。"""
    calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strategy_name]

    full_df = fetch(symbol, 800)
    if full_df is None or len(full_df) < 400:
        return None

    try:
        latest_price = float(full_df.iloc[-1]["close"])
    except Exception:
        return None
    if latest_price <= 0:
        return None
    if max_price > 0 and latest_price > max_price:
        return None

    folds_passed   = 0
    train_results: list[dict] = []
    test_results:  list[dict] = []

    for fold_name, ts, te, vs, ve in folds:
        train_r = _run_window(symbol, name, full_df, calc_fn, em, sm, tm,
                              ts, te, strategy_name, entry_type=entry_type)
        test_r  = _run_window(symbol, name, full_df, calc_fn, em, sm, tm,
                              vs, ve, strategy_name, entry_type=entry_type)

        pass_train = _passes_train(train_r)
        pass_test  = _passes_test(test_r)
        if pass_train and pass_test:
            folds_passed += 1

        if train_r:
            train_results.append(train_r)
        if test_r:
            test_results.append(test_r)

    if not test_results:
        return None

    all_test_trades: list[dict] = []
    total_test_pnl  = 0.0
    total_test_tr   = 0
    for r in test_results:
        all_test_trades.extend(r.get("trade_log", []))
        total_test_pnl += r.get("total_pnl", 0.0)
        total_test_tr  += r.get("trades", 0)

    filled = [t for t in all_test_trades if t.get("hold_days", 0) > 0]
    avg_hold_days = (
        round(sum(t["hold_days"] for t in filled) / len(filled), 1) if filled else 0.0
    )

    agg = enrich_backtest_result({"trade_log": all_test_trades}, INITIAL_CASH)

    def _cap_pf(p):
        return 10.0 if p == float("inf") else min(p, 10.0)

    avg_test_pf = sum(_cap_pf(r.get("pf", 0)) for r in test_results) / max(len(test_results), 1)
    avg_test_wr = sum(r.get("win_rate", 0) for r in test_results) / max(len(test_results), 1)

    train_pnl_sum = sum(r.get("total_pnl", 0.0) for r in train_results)
    degradation = (
        (train_pnl_sum - total_test_pnl) / train_pnl_sum * 100
        if train_pnl_sum > 0 else 0.0
    )

    return dict(
        symbol=symbol, name=name, strategy=strategy_name, family=family,
        latest_price=round(latest_price, 0),
        folds_passed=folds_passed,
        total_test_trades=total_test_tr,
        avg_hold_days=avg_hold_days,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="エンバーゴ付きウォークフォワードスキャナー",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python scan_walkforward_embargo.py --embargo-days 7    # デフォルト
  python scan_walkforward_embargo.py --embargo-days 14   # 2週間バッファー
  python scan_walkforward_embargo.py --embargo-days 0    # バッファーなし
  python scan_walkforward_embargo.py --family stop --budget 600000
        """,
    )
    parser.add_argument("--embargo-days", type=int, default=7,
                        help="TRAIN終了〜TEST開始のバッファー日数 (デフォルト: 7)")
    parser.add_argument("--family",
                        choices=["stop", "breakout", "short", "short_brk", "all", "both"],
                        default="both")
    parser.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    parser.add_argument("--top",     type=int, default=30)
    parser.add_argument("--symbols", type=str, default=None)
    parser.add_argument("--limit",   type=int, default=0)
    parser.add_argument("--max-price", type=float, default=0.0)
    parser.add_argument("--budget",  type=float, default=0.0)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true")
    mode_group.add_argument("--conservative", action="store_true")
    parser.add_argument("--holdout-days", type=int, default=0)
    args = parser.parse_args()

    embargo_days = args.embargo_days
    holdout_days = args.holdout_days

    folds = _build_folds(embargo_days, holdout_days)

    effective_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        effective_max_price = args.budget / 100.0

    _FAMILY_STRATS = {
        "stop":      ["MACD", "A7", "RSI2"],
        "breakout":  ["DON", "VOL", "MOM"],
        "short":     ["A7_S"],
        "short_brk": ["DON_S", "MOM_S", "GAP_S"],
        "both":      ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"],
        "all":       ["MACD", "A7", "RSI2", "DON", "VOL", "MOM",
                      "A7_S", "DON_S", "MOM_S", "GAP_S"],
    }
    strategies = _FAMILY_STRATS[args.family]

    try:
        symbols, universe_name = load_universe(args.symbols)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.limit > 0:
        symbols = symbols[: args.limit]

    print("=" * 78)
    print(f"エンバーゴ付きウォークフォワードスキャン開始")
    print(f"  基準日      : {TODAY}")
    print(f"  ユニバース  : {universe_name} ({len(symbols)} 銘柄)")
    print(f"  戦略        : {', '.join(strategies)}")
    print(f"  エンバーゴ  : {embargo_days} 日 (TRAIN末端を{embargo_days}日前倒しして空白を作る)")
    print(f"  Workers     : {args.workers}")
    print(f"  モード      : {TRADING_MODE}")
    if holdout_days > 0:
        holdout_end_date = TODAY - timedelta(days=holdout_days)
        print(f"  ホールドアウト: 直近 {holdout_days} 日 ({holdout_end_date} 以降をテスト対象外)")
    if effective_max_price > 0:
        print(f"  価格上限    : {effective_max_price:,.0f}円/株")
    print("=" * 78)
    print("Fold 構造 (エンバーゴ適用後):")
    for fold_name, ts, te, vs, ve in folds:
        train_s = (TODAY - timedelta(days=ts)).isoformat()
        train_e = (TODAY - timedelta(days=te)).isoformat()
        emb_s   = (TODAY - timedelta(days=te)).isoformat()
        emb_e   = (TODAY - timedelta(days=vs)).isoformat()
        test_s  = (TODAY - timedelta(days=vs)).isoformat()
        test_e  = (TODAY - timedelta(days=ve)).isoformat()
        print(f"  {fold_name}: TRAIN {train_s}〜{train_e}  "
              f"[{emb_s}〜{emb_e} エンバーゴ]  "
              f"TEST {test_s}〜{test_e}")
    print("=" * 78)

    total_tasks = len(symbols) * len(strategies) * len(folds) * 2
    print(f"合計バックテスト数: {total_tasks:,}")
    print("=" * 78)

    out_dir = Path("walkforward_results")
    out_dir.mkdir(exist_ok=True)

    mode_suffix    = f"_{TRADING_MODE}" if TRADING_MODE != "conservative" else ""
    holdout_suffix = f"_holdout{holdout_days}d" if holdout_days > 0 else ""
    embargo_suffix = f"_embargo{embargo_days}d"

    for strategy in strategies:
        print(f"\n=== {strategy} ===")
        results: list[dict] = []

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(walkforward_one_emb, sym, name, strategy, folds, effective_max_price): sym
                for sym, name in symbols
            }
            done = 0
            progress_every = max(len(symbols) // 20, 25)
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                    if r:
                        results.append(r)
                except Exception:
                    pass
                if done % progress_every == 0:
                    print(f"  進捗: {done}/{len(symbols)}  候補: {len(results)}",
                          flush=True)

        survivors = [r for r in results if r["folds_passed"] >= 2]
        survivors.sort(key=lambda r: r["total_test_pnl"], reverse=True)
        print(f"\n  {strategy}: 全候補={len(results)}  2fold以上通過={len(survivors)}")

        csv_path = out_dir / (
            f"walkforward_{strategy}{mode_suffix}{holdout_suffix}"
            f"{embargo_suffix}_{TODAY}.csv"
        )
        fields = [
            "symbol", "name", "strategy", "family", "latest_price",
            "folds_passed",
            "total_test_trades", "avg_hold_days", "total_test_pnl", "total_train_pnl",
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

        n = min(args.top, len(survivors))
        if n == 0:
            print("  (合格銘柄なし)")
            continue
        print(f"\n  ▼ {strategy} 上位 {n}:")
        print(f"  {'銘柄':<10}{'名前':<22}"
              f"{'株価':>8}{'fld':>5}{'trades':>8}{'TEST_PnL':>12}"
              f"{'PF':>7}{'WR':>8}{'MaxDD%':>9}{'連敗':>6}{'Shrp':>7}")
        print("  " + "-" * 108)
        for r in survivors[:n]:
            print(f"  {r['symbol']:<10}{r['name'][:20]:<22}"
                  f"{r.get('latest_price', 0):>8,.0f}"
                  f"{r['folds_passed']:>5}{r['total_test_trades']:>8}"
                  f"{r['total_test_pnl']:>+12,.0f}"
                  f"{r['avg_test_pf']:>7.2f}{r['avg_test_wr']:>7.1f}%"
                  f"{r['max_drawdown_pct']:>9.1f}"
                  f"{r['max_consecutive_losses']:>6}"
                  f"{r['sharpe']:>7.2f}")

    print(f"\n完了。CSV は {out_dir.resolve()} に保存されました。")
    print(f"次ステップ: python build_watchlist.py (CSVに _embargo{embargo_days}d が含まれます)")


if __name__ == "__main__":
    main()
