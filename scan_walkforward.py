"""
scan_walkforward.py  ―  Walk-forward 方式による銘柄スキャン
=================================================================
ユニバース (デフォルト=プライム市場 ~1800銘柄) × 6戦略
  (MACD/A7/RSI2 逆指値B + DON/VOL/MOM ブレイクアウト)
に対し、非重複の 3 fold Walk-forward バックテストを実行し、
TRAIN (選定用) で勝ち、かつ TEST (検証用) でも勝つ銘柄を抽出する。

【ユニバース】
  優先順位 (--symbols 未指定時):
    1. symbols_listed_prime.py    (プライム市場 ~1800銘柄)
    2. symbols_listed_all.py      (全上場 ~4000銘柄)
    3. symbols_listed_standard.py (プライム+スタンダード)
    4. symbols_all.py             (日経225, 225銘柄)

  生成方法:
    python fetch_listed_symbols.py --market prime   # プライム取得 (推奨)
    python fetch_listed_symbols.py --market all     # 全市場

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
  # 準備: ユニバースを取得 (初回のみ)
  python fetch_listed_symbols.py --market prime

  # 本番スキャン
  python scan_walkforward.py                      # 全戦略 (6つ)
  python scan_walkforward.py --family stop        # 逆指値Bのみ
  python scan_walkforward.py --family breakout    # ブレイクアウトのみ
  python scan_walkforward.py --workers 8
  python scan_walkforward.py --top 50             # 表示する上位N
  python scan_walkforward.py --symbols symbols_listed_all.py   # 明示指定
  python scan_walkforward.py --limit 50           # 先頭50銘柄だけ (デバッグ)

  # 予算フィルター
  python scan_walkforward.py --budget 600000      # 60万円で100株買える銘柄のみ
  python scan_walkforward.py --max-price 6000     # 株価6000円以下のみ (上と同義)
  python scan_walkforward.py --budget 300000 --workers 8  # 30万円予算で並列8

  # モード (デフォルト=conservative, tm=3.0 目標+9%)
  python scan_walkforward.py --aggressive         # 積極利確 (tm=1.5 目標+4.5%)

注意:
  - backtest_limit_entry.run_limit_backtest を内部で使う。 _TODAY は触らない
    (スレッドセーフ) ので、df を事前にトリミングして backtest_days パラメータで
    ウィンドウを制御している。
  - TRAIN と TEST は時期をずらした非重複ウィンドウ。TEST は擬似的な "未来データ" 扱い。
  - 1800銘柄 × 6戦略 × 3fold × 2(train/test) = 約6.5万回のバックテスト。
    初回は yfinance からのデータDLが走るため、1〜2時間かかる場合があります。
    2回目以降は .rsi2_cache/ のキャッシュで高速化されます。
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# ── TRADING_MODE を import 前に設定 (モジュールトップで env var を読む) ──
import os as _os_pre
if "--aggressive" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    _os_pre.environ["TRADING_MODE"] = "conservative"

from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)
from scan_breakout_entry import (
    calc_donchian, calc_vol_breakout, calc_momentum,
)
from risk_metrics import enrich_backtest_result

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# ── ユニバース (銘柄リスト) 自動検出 ─────────────────────────────
# 優先順位: --symbols 指定 > symbols_listed_prime.py > symbols_listed_all.py >
#           symbols_listed_standard.py > symbols_all.py (日経225)
# symbols_listed_*.py は fetch_listed_symbols.py で生成する:
#   python fetch_listed_symbols.py --market prime   # プライム ~1800銘柄
#   python fetch_listed_symbols.py --market all     # 全市場 ~4000銘柄
_UNIVERSE_CANDIDATES = [
    "symbols_listed_prime.py",
    "symbols_listed_all.py",
    "symbols_listed_standard.py",
    "symbols_all.py",
]


def load_universe(explicit_path: str | None = None) -> tuple[list[tuple[str, str]], str]:
    """
    ユニバースを読み込んで (symbols, source_name) を返す。
    explicit_path が指定されていればそれを最優先。
    """
    import importlib.util

    candidates: list[str] = []
    if explicit_path:
        candidates.append(explicit_path)
    candidates.extend(_UNIVERSE_CANDIDATES)

    for cand in candidates:
        p = Path(cand)
        if not p.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            syms = [tuple(t) for t in mod.SYMBOLS]
            return syms, p.name
        except Exception as e:
            print(f"[WARN] {cand} の読み込みに失敗: {e}", file=sys.stderr)
            continue

    raise RuntimeError(
        "ユニバースファイルが見つかりません。以下のいずれかを用意してください:\n"
        "  - python fetch_listed_symbols.py --market prime  (推奨, 約1800銘柄)\n"
        "  - python fetch_listed_symbols.py --market all    (全上場, 約4000銘柄)\n"
        "  - symbols_all.py (日経225)"
    )


# ── 戦略定義 (プリセット切替) ─────────────────────────────────
# (calc_fn, entry_atr_mult, stop_atr_mult, target_atr_mult, family)
# TRADING_MODE=aggressive のとき積極利確プリセットを使う
STRATEGY_DEFS_CONSERVATIVE: dict[str, tuple] = {
    "MACD": (calc_macd,        0.0, 1.5, 3.0, "stop"),
    "A7":   (calc_a7,          0.0, 1.5, 3.0, "stop"),
    "RSI2": (calc_rsi2,        0.0, 2.0, 4.0, "stop"),
    "DON":  (calc_donchian,    0.0, 1.5, 3.0, "breakout"),
    "VOL":  (calc_vol_breakout,0.0, 1.5, 3.0, "breakout"),
    "MOM":  (calc_momentum,    0.0, 1.5, 3.0, "breakout"),
}
STRATEGY_DEFS_AGGRESSIVE: dict[str, tuple] = {
    "MACD": (calc_macd,        0.0, 1.0, 1.5, "stop"),
    "A7":   (calc_a7,          0.0, 1.0, 1.5, "stop"),
    "RSI2": (calc_rsi2,        0.0, 1.2, 1.8, "stop"),
    "DON":  (calc_donchian,    0.0, 1.0, 1.5, "breakout"),
    "VOL":  (calc_vol_breakout,0.0, 1.0, 1.5, "breakout"),
    "MOM":  (calc_momentum,    0.0, 1.0, 1.5, "breakout"),
}

import os as _os
# デフォルトは conservative (標準)。--aggressive で積極利確。
TRADING_MODE = _os.getenv("TRADING_MODE", "conservative").lower()
if TRADING_MODE == "aggressive":
    STRATEGY_DEFS = STRATEGY_DEFS_AGGRESSIVE
else:
    STRATEGY_DEFS = STRATEGY_DEFS_CONSERVATIVE
    TRADING_MODE = "conservative"

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
def walkforward_one(symbol: str, name: str, strategy_name: str,
                    max_price: float = 0.0) -> dict | None:
    calc_fn, em, sm, tm, family = STRATEGY_DEFS[strategy_name]

    full_df = fetch(symbol, 800)   # Walk-forward には ~2年のデータが必要
    if full_df is None or len(full_df) < 400:
        return None

    # 最新終値 (予算フィルター & CSV出力用)
    try:
        latest_price = float(full_df.iloc[-1]["close"])
    except Exception:
        return None
    if latest_price <= 0:
        return None

    # 価格フィルター: max_price > 0 のとき最新終値 > max_price の銘柄は除外
    if max_price > 0 and latest_price > max_price:
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
        latest_price=round(latest_price, 0),
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
    parser.add_argument("--symbols", type=str, default=None,
                        help="ユニバースファイル (省略時は symbols_listed_prime.py → "
                             "symbols_listed_all.py → symbols_all.py の順で自動検出)")
    parser.add_argument("--limit",   type=int, default=0,
                        help="ユニバースを先頭 N 件に制限 (デバッグ用, 0=制限なし)")
    parser.add_argument("--max-price", type=float, default=0.0,
                        help="最新終値の上限 (円/株). 0=制限なし")
    parser.add_argument("--budget",  type=float, default=0.0,
                        help="総予算 (円). 100株買える銘柄に絞る = --max-price (予算/100). "
                             "--max-price と併用時は --max-price 優先")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="積極利確モード (tm=1.5, 目標+4.5%%)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="標準モード (tm=3.0, 目標+9%%, デフォルト)")
    args = parser.parse_args()

    # budget → max_price 換算 (FIXED_QTY=100 株)
    effective_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        effective_max_price = args.budget / 100.0

    if args.family == "stop":
        strategies = ["MACD", "A7", "RSI2"]
    elif args.family == "breakout":
        strategies = ["DON", "VOL", "MOM"]
    else:
        strategies = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]

    # ユニバース読み込み
    try:
        symbols, universe_name = load_universe(args.symbols)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.limit > 0:
        symbols = symbols[: args.limit]

    print("=" * 78)
    print(f"Walk-forward スキャン開始")
    print(f"  基準日    : {TODAY}")
    print(f"  ユニバース: {universe_name} ({len(symbols)} 銘柄)")
    print(f"  戦略      : {', '.join(strategies)}")
    print(f"  Folds     : {len(FOLDS)}")
    print(f"  Workers   : {args.workers}")
    print(f"  モード    : {TRADING_MODE}")
    if effective_max_price > 0:
        budget_str = f" (予算 {args.budget:,.0f}円)" if args.budget > 0 else ""
        print(f"  価格上限  : {effective_max_price:,.0f}円/株{budget_str}")
    else:
        print(f"  価格上限  : なし")
    print("=" * 78)
    print("Fold 構造:")
    for name, ts, te, vs, ve in FOLDS:
        train_s = (TODAY - timedelta(days=ts)).isoformat()
        train_e = (TODAY - timedelta(days=te)).isoformat()
        test_s  = (TODAY - timedelta(days=vs)).isoformat()
        test_e  = (TODAY - timedelta(days=ve)).isoformat()
        print(f"  {name}: TRAIN {train_s}〜{train_e}  /  TEST {test_s}〜{test_e}")
    print("=" * 78)

    # 所要時間の目安を表示
    total_tasks = len(symbols) * len(strategies) * len(FOLDS) * 2
    print(f"合計バックテスト数: {total_tasks:,} "
          f"({len(symbols)}銘柄 × {len(strategies)}戦略 × {len(FOLDS)}fold × 2(train/test))")
    print("=" * 78)

    out_dir = Path("walkforward_results")
    out_dir.mkdir(exist_ok=True)

    for strategy in strategies:
        print(f"\n=== {strategy} ===")
        results: list[dict] = []

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(walkforward_one, sym, name, strategy,
                              effective_max_price): sym
                    for sym, name in symbols}
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

        # ── CSV 保存 (全候補) ──
        # conservative (デフォルト) は suffix なし、aggressive は "_aggressive"
        mode_suffix = f"_{TRADING_MODE}" if TRADING_MODE != "conservative" else ""
        csv_path = out_dir / f"walkforward_{strategy}{mode_suffix}_{TODAY}.csv"
        fields = [
            "symbol", "name", "strategy", "family", "latest_price",
            "folds_passed",
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
    print(f"次ステップ: python build_watchlist.py")


if __name__ == "__main__":
    main()
