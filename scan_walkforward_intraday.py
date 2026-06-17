"""
scan_walkforward_intraday.py  ―  デイトレ版 Walk-forward 銘柄スキャン
=======================================================================
ユニバース (~1800銘柄) × デイトレ戦略 (PREV_CLOSE_BREAK など) に対し、
非重複の 2 fold Walk-forward バックテストを実行し、TRAIN (選定用) で勝ち、
かつ TEST (検証用) でも勝つ銘柄を抽出する。

【データ】
  source="local"    : data/minute_5m/*.pkl (推奨・デフォルト)
  source="yfinance" : 最大60日のため fold 設計が変わる
  source="auto"     : ローカル優先 → yfinance

【Walk-forward 構造】 (基準日=本日, 単位=暦日)
  Fold 1: TRAIN 365〜180日前  /  TEST 180〜90日前   (6M train / 3M test)
  Fold 2: TRAIN 270〜90日前   /  TEST  90〜  0日前  (6M train / 3M test)

  --folds-short を指定 (yfinance 限定):
  Fold 1: TRAIN 90〜45日前  /  TEST 45〜20日前
  Fold 2: TRAIN 60〜20日前  /  TEST 20〜  0日前

【合格条件】
  TRAIN: trades>=5, PF>=1.3, win_rate>=50%, total_pnl>0
  TEST : trades>=3, PF>=1.1, win_rate>=45%, total_pnl>0
  選定 : 2 fold のうち TRAIN+TEST 両方合格が 2 (両方クリア)

【出力】
  walkforward_results/walkforward_intraday_<STRATEGY>_<YYYY-MM-DD>.csv

【使い方】
  # 準備: ユニバースを取得 (初回のみ、swing版と共用可)
  python fetch_listed_symbols.py --market prime

  # 本番スキャン (ローカルpklデータ必須)
  python scan_walkforward_intraday.py                    # 全戦略
  python scan_walkforward_intraday.py --strategy PREV_CLOSE_BREAK
  python scan_walkforward_intraday.py --workers 8
  python scan_walkforward_intraday.py --limit 50         # デバッグ (先頭50銘柄)
  python scan_walkforward_intraday.py --source local     # ローカルのみ
  python scan_walkforward_intraday.py --max-price 6000   # 株価上限

  # モード
  python scan_walkforward_intraday.py --aggressive       # 積極利確 (tm=2.0)

  # yfinance (期間短縮)
  python scan_walkforward_intraday.py --source yfinance --folds-short

次ステップ:
  python build_watchlist_intraday.py   # CSV → WATCHLIST 提案
  (または既存 build_watchlist.py を --csv で指定)
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from backtest_intraday import (
    run_intraday_backtest,
    BACKTEST_DAYS,
)
from daytrade_data import load_intraday, DATA_DIR
from risk_metrics import enrich_backtest_result

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

INITIAL_CASH = 500_000

# ── ユニバース自動検出 (swing版と同じ優先順位) ──────────────────
_UNIVERSE_CANDIDATES = [
    "symbols_listed_prime.py",
    "symbols_listed_all.py",
    "symbols_listed_standard.py",
    "symbols_all.py",
]


def load_universe(explicit_path: str | None = None) -> tuple[list[tuple[str, str]], str]:
    import importlib.util
    candidates = ([explicit_path] if explicit_path else []) + _UNIVERSE_CANDIDATES
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
    raise RuntimeError(
        "ユニバースファイルが見つかりません。\n"
        "  python fetch_listed_symbols.py --market prime  (推奨)"
    )


# ── 戦略定義 ────────────────────────────────────────────────────
# (entry_atr_mult, stop_atr_mult, target_atr_mult)
STRATEGY_DEFS_CONSERVATIVE: dict[str, tuple[float, float, float]] = {
    "PREV_CLOSE_BREAK": (0.0, 1.5, 3.0),
}
STRATEGY_DEFS_AGGRESSIVE: dict[str, tuple[float, float, float]] = {
    "PREV_CLOSE_BREAK": (0.0, 1.5, 2.0),
}

import os as _os
_trading_mode = _os.getenv("TRADING_MODE", "conservative").lower()
if "--aggressive" in sys.argv:
    _trading_mode = "aggressive"
STRATEGY_DEFS = (STRATEGY_DEFS_AGGRESSIVE if _trading_mode == "aggressive"
                 else STRATEGY_DEFS_CONSERVATIVE)
TRADING_MODE = _trading_mode

# ── Walk-forward fold 定義 ───────────────────────────────────────
# 通常 (ローカルデータ前提, 365日以上のデータがある前提)
FOLDS_NORMAL: list[tuple[str, int, int, int, int]] = [
    ("fold1", 365, 180, 180, 90),   # TRAIN 6M / TEST 3M
    ("fold2", 270,  90,  90,  0),   # TRAIN 6M / TEST 3M
]

# 短縮版 (yfinance 60日制限用)
FOLDS_SHORT: list[tuple[str, int, int, int, int]] = [
    ("fold1", 90, 45, 45, 20),      # TRAIN 45日 / TEST 25日
    ("fold2", 60, 20, 20,  0),      # TRAIN 40日 / TEST 20日
]

# デフォルト
FOLDS = FOLDS_NORMAL

# ── 合格閾値 ─────────────────────────────────────────────────────
TRAIN_MIN_TRADES = 5
TRAIN_MIN_PF     = 1.3
TRAIN_MIN_WR     = 50.0
TEST_MIN_TRADES  = 3
TEST_MIN_PF      = 1.1
TEST_MIN_WR      = 45.0

# 2 fold 中に TRAIN+TEST 合格が必要な最低数
FOLDS_PASS_REQUIRED = 2   # 両方クリアが必須


# ── 1 ウィンドウ分のバックテスト ─────────────────────────────────

def _run_window(
    symbol: str, name: str, df_all: pd.DataFrame,
    em: float, sm: float, tm: float,
    start_days_ago: int, end_days_ago: int,
    strategy_name: str,
) -> dict | None:
    """
    指定ウィンドウ [today-start_days_ago, today-end_days_ago] でバックテスト。

    スイング版と同じトリック:
      - df を window_end までトリミング (未来データ遮断)
      - backtest_days = window_start からの日数
      → run_intraday_backtest の today-backtest_days が window_start になる
    """
    window_end   = TODAY - timedelta(days=end_days_ago)
    window_start = TODAY - timedelta(days=start_days_ago)

    # df を window_end までトリミング
    df_trimmed = df_all[df_all.index.date <= window_end].copy()
    if len(df_trimmed) < 50:
        return None

    backtest_days = (TODAY - window_start).days
    if backtest_days <= 0:
        return None

    try:
        result = run_intraday_backtest(
            symbol, name, df_trimmed,
            entry_atr_mult=em, stop_atr_mult=sm, target_atr_mult=tm,
            backtest_days=backtest_days,
            strategy_name=strategy_name,
        )
    except Exception:
        return None

    trades = result.get("all_trades", [])
    if not trades:
        return {"trades": 0, "wins": 0, "pf": 0.0, "win_rate": 0.0,
                "total_pnl": 0.0, "trade_log": []}

    wins = sum(1 for t in trades if t["pnl"] > 0)
    gp   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {
        "trades":    len(trades),
        "wins":      wins,
        "win_rate":  wins / len(trades) * 100,
        "pf":        pf,
        "total_pnl": sum(t["pnl"] for t in trades),
        "trade_log": trades,
    }


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


# ── 1 銘柄 × 1 戦略 ─────────────────────────────────────────────

def walkforward_one(
    symbol: str, name: str, strategy_name: str,
    max_price: float = 0.0, source: str = "local",
    data_days: int = 400,
) -> dict | None:
    """
    1銘柄 × 1戦略の Walk-forward を実行。
    合格条件を満たせば dict を、満たさなければ None を返す。
    """
    em, sm, tm = STRATEGY_DEFS[strategy_name]

    # データ取得 (Walk-forward 全期間分)
    df_all = load_intraday(symbol, days=data_days, source=source)
    if df_all is None or df_all.empty or len(df_all) < 50:
        return None

    # 最新終値 (予算フィルター & CSV出力用)
    try:
        latest_price = float(df_all["close"].iloc[-1])
    except Exception:
        return None
    if latest_price <= 0:
        return None
    if max_price > 0 and latest_price > max_price:
        return None

    folds_passed  = 0
    train_results: list[dict] = []
    test_results:  list[dict] = []

    for fold_name, ts, te, vs, ve in FOLDS:
        train_r = _run_window(symbol, name, df_all, em, sm, tm,
                              ts, te, strategy_name)
        test_r  = _run_window(symbol, name, df_all, em, sm, tm,
                              vs, ve, strategy_name)

        if _passes_train(train_r) and _passes_test(test_r):
            folds_passed += 1
        if train_r and train_r["trades"] > 0:
            train_results.append(train_r)
        if test_r and test_r["trades"] > 0:
            test_results.append(test_r)

    if folds_passed < FOLDS_PASS_REQUIRED:
        return None
    if not test_results:
        return None

    # ── TEST 期間の集約 ──
    all_test_trades: list[dict] = []
    total_test_pnl  = 0.0
    total_test_tr   = 0
    for r in test_results:
        all_test_trades.extend(r.get("trade_log", []))
        total_test_pnl += r.get("total_pnl", 0.0)
        total_test_tr  += r.get("trades", 0)

    # 平均保有時間 (分) — デイトレ特有のメトリクス
    hold_minutes_list = []
    for t in all_test_trades:
        try:
            delta = (t["exit_dt"] - t["entry_dt"]).total_seconds() / 60
            hold_minutes_list.append(delta)
        except Exception:
            pass
    avg_hold_minutes = (
        round(sum(hold_minutes_list) / len(hold_minutes_list), 1)
        if hold_minutes_list else 0.0
    )

    # リスク指標 (risk_metrics を流用)
    agg = enrich_backtest_result({"trade_log": all_test_trades}, INITIAL_CASH)

    def _cap_pf(p):
        return 10.0 if p == float("inf") else min(p, 10.0)

    avg_test_pf = sum(_cap_pf(r.get("pf", 0)) for r in test_results) / len(test_results)
    avg_test_wr = sum(r.get("win_rate", 0) for r in test_results) / len(test_results)

    # TRAIN との損益劣化率
    train_pnl_sum = sum(r.get("total_pnl", 0.0) for r in train_results)
    if train_pnl_sum > 0:
        degradation = (train_pnl_sum - total_test_pnl) / train_pnl_sum * 100
    else:
        degradation = 0.0

    return dict(
        symbol=symbol,
        name=name,
        strategy=strategy_name,
        family="intraday",
        latest_price=round(latest_price, 0),
        folds_passed=folds_passed,
        total_test_trades=total_test_tr,
        avg_hold_days=round(avg_hold_minutes / 60 / 6.5, 2),  # 日換算 (参考)
        avg_hold_minutes=avg_hold_minutes,
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
    parser = argparse.ArgumentParser(
        description="デイトレ版 Walk-forward 銘柄スキャナー"
    )
    parser.add_argument("--strategy", default=None,
                        choices=list(STRATEGY_DEFS_CONSERVATIVE.keys()),
                        help="戦略名 (省略時は全戦略)")
    parser.add_argument("--source",   default="local",
                        choices=["local", "auto", "yfinance"],
                        help="データソース (デフォルト: local)")
    parser.add_argument("--workers",  type=int, default=4,
                        help="並列数")
    parser.add_argument("--top",      type=int, default=30,
                        help="表示する上位N (CSVは全件保存)")
    parser.add_argument("--symbols",  type=str, default=None,
                        help="ユニバースファイル (省略時は自動検出)")
    parser.add_argument("--limit",    type=int, default=0,
                        help="ユニバースを先頭N件に制限 (デバッグ用)")
    parser.add_argument("--max-price",type=float, default=0.0,
                        help="最新終値の上限 (円/株)")
    parser.add_argument("--budget",   type=float, default=0.0,
                        help="総予算 (円). FIXED_QTY=100株換算で --max-price と同義")
    parser.add_argument("--folds-short", action="store_true",
                        help="短縮fold設計 (yfinance 60日制限対応)")
    parser.add_argument("--data-days", type=int, default=400,
                        help="1銘柄あたりのデータ取得期間 (暦日, デフォルト400)")
    parser.add_argument("--train-pf", type=float, default=None)
    parser.add_argument("--train-wr", type=float, default=None)
    parser.add_argument("--test-pf",  type=float, default=None)
    parser.add_argument("--test-wr",  type=float, default=None)
    parser.add_argument("--folds-pass-required", type=int, default=None,
                        help="合格に必要なfold数 (デフォルト: 2=全fold通過)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true")
    mode_group.add_argument("--conservative", action="store_true")
    args = parser.parse_args()

    # ── 閾値上書き ─────────────────────────────────────────────
    global TRAIN_MIN_PF, TRAIN_MIN_WR, TEST_MIN_PF, TEST_MIN_WR, FOLDS, FOLDS_PASS_REQUIRED
    if args.train_pf is not None:
        TRAIN_MIN_PF = args.train_pf
    if args.train_wr is not None:
        TRAIN_MIN_WR = args.train_wr
    if args.test_pf is not None:
        TEST_MIN_PF = args.test_pf
    if args.test_wr is not None:
        TEST_MIN_WR = args.test_wr
    if args.folds_pass_required is not None:
        FOLDS_PASS_REQUIRED = args.folds_pass_required

    if args.folds_short:
        FOLDS = FOLDS_SHORT

    effective_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        effective_max_price = args.budget / 100.0

    strategies = ([args.strategy] if args.strategy
                  else list(STRATEGY_DEFS.keys()))

    # ユニバース読み込み
    try:
        symbols, universe_name = load_universe(args.symbols)
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.limit > 0:
        symbols = symbols[:args.limit]

    # ローカルデータの存在確認
    if args.source in ("local", "auto"):
        if not DATA_DIR.exists():
            print(f"[WARN] ローカルデータディレクトリが見つかりません: {DATA_DIR}",
                  file=sys.stderr)
            print("[WARN] --source yfinance または --folds-short を検討してください",
                  file=sys.stderr)
        else:
            pkl_count = len(list(DATA_DIR.glob("*.pkl")))
            print(f"[INFO] ローカルデータ: {DATA_DIR} ({pkl_count}件の.pkl)")

    print("=" * 78)
    print("デイトレ Walk-forward スキャン開始")
    print(f"  基準日    : {TODAY}")
    print(f"  ユニバース: {universe_name} ({len(symbols)} 銘柄)")
    print(f"  戦略      : {', '.join(strategies)}")
    print(f"  データソース: {args.source} (取得期間: {args.data_days}日)")
    print(f"  Folds     : {len(FOLDS)} ({'短縮版' if args.folds_short else '通常版'})")
    print(f"  Workers   : {args.workers}")
    print(f"  モード    : {TRADING_MODE}")
    print(f"  合格必要fold数: {FOLDS_PASS_REQUIRED}/{len(FOLDS)}")
    if effective_max_price > 0:
        print(f"  価格上限  : {effective_max_price:,.0f}円/株")
    print("=" * 78)
    print("Fold 構造:")
    for name, ts, te, vs, ve in FOLDS:
        ts_d = (TODAY - timedelta(days=ts)).isoformat()
        te_d = (TODAY - timedelta(days=te)).isoformat()
        vs_d = (TODAY - timedelta(days=vs)).isoformat()
        ve_d = (TODAY - timedelta(days=ve)).isoformat() if ve > 0 else TODAY.isoformat()
        print(f"  {name}: TRAIN {ts_d}〜{te_d}  /  TEST {vs_d}〜{ve_d}")
    print(f"合格閾値: TRAIN trades>={TRAIN_MIN_TRADES} PF>={TRAIN_MIN_PF} "
          f"WR>={TRAIN_MIN_WR}%  "
          f"TEST trades>={TEST_MIN_TRADES} PF>={TEST_MIN_PF} WR>={TEST_MIN_WR}%")
    print("=" * 78)

    out_dir = Path("walkforward_results")
    out_dir.mkdir(exist_ok=True)

    mode_suffix = f"_{TRADING_MODE}" if TRADING_MODE != "conservative" else ""

    for strategy in strategies:
        print(f"\n=== {strategy} ===")
        results: list[dict] = []

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(
                    walkforward_one, sym, sym_name, strategy,
                    effective_max_price, args.source, args.data_days,
                ): sym
                for sym, sym_name in symbols
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
                if done % progress_every == 0 or done == len(symbols):
                    print(f"  進捗: {done}/{len(symbols)}  候補: {len(results)}",
                          flush=True)

        results.sort(key=lambda r: (-r["folds_passed"], -r["total_test_pnl"]))

        print(f"\n  {strategy}: 全候補={len(results)}")

        # ── CSV 保存 ──
        fold_suffix = "_short" if args.folds_short else ""
        csv_path = out_dir / f"walkforward_intraday_{strategy}{mode_suffix}{fold_suffix}_{TODAY}.csv"
        fields = [
            "symbol", "name", "strategy", "family", "latest_price",
            "folds_passed",
            "total_test_trades", "avg_hold_days", "avg_hold_minutes",
            "total_test_pnl", "total_train_pnl",
            "avg_test_pf", "avg_test_wr",
            "max_drawdown_pct", "max_consecutive_losses", "sharpe",
            "recovery_factor", "train_to_test_degradation_pct",
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in results:
                writer.writerow({k: r.get(k, "") for k in fields})
        print(f"  CSV: {csv_path}")

        # ── トップN表示 ──
        n = min(args.top, len(results))
        if n == 0:
            print("  (合格銘柄なし)")
            continue
        print(f"\n  ▼ {strategy} 上位 {n}:")
        print(f"  {'銘柄':<10}{'名前':<22}"
              f"{'株価':>8}{'fld':>5}{'trades':>8}{'TEST_PnL':>12}"
              f"{'PF':>7}{'WR':>8}{'MaxDD%':>9}{'連敗':>6}{'Shrp':>7}{'保有分':>8}")
        print("  " + "-" * 116)
        for r in results[:n]:
            print(f"  {r['symbol']:<10}{r['name'][:20]:<22}"
                  f"{r.get('latest_price', 0):>8,.0f}"
                  f"{r['folds_passed']:>5}{r['total_test_trades']:>8}"
                  f"{r['total_test_pnl']:>+12,.0f}"
                  f"{r['avg_test_pf']:>7.2f}{r['avg_test_wr']:>7.1f}%"
                  f"{r['max_drawdown_pct']:>9.1f}"
                  f"{r['max_consecutive_losses']:>6}"
                  f"{r['sharpe']:>7.2f}"
                  f"{r.get('avg_hold_minutes', 0):>7.0f}分")

    print(f"\n完了。CSV は {out_dir.resolve()} に保存されました。")
    print(f"次ステップ: python build_watchlist_intraday.py")


if __name__ == "__main__":
    main()
