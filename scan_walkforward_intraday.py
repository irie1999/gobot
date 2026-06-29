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
from daytrade_data import load_intraday, DATA_DIR, quarantine_symbols
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
# entry_atr_mult の意味は戦略によって異なる:
#   PREV_CLOSE_BREAK: order_p = prev_close + atr × em
#   GAP_UP          : ギャップ最小幅 = atr × em (0.0 = 任意ギャップ)
#   ORB30           : 未使用
#   PREV_HIGH_BREAK : 未使用
# 倍率は「日足ATR基準」(backtest_intraday の stop_basis="daily")。
# 日足ATR≈価格の2-3% なので sm=0.5→stop約1-1.5%, tm=1.0→target約2-3% (2R設定)。
# (旧: 5分足ATR基準で sm=1.5/tm=3.0 → stopが0.6%と狭すぎノイズで即損切りだった)
STRATEGY_DEFS_CONSERVATIVE: dict[str, tuple[float, float, float]] = {
    "PREV_CLOSE_BREAK": (0.0, 0.5, 1.0),
    "GAP_UP":           (0.0, 0.5, 1.0),  # ギャップアップ日限定 (選別的)
    "GAP_DOWN":         (0.0, 0.5, 1.0),  # ギャップダウン日限定 mean-reversion (目標=ギャップフィル)
    "ORB30":            (0.0, 0.5, 1.0),  # 30分Opening Range Breakout
    "PREV_HIGH_BREAK":  (0.0, 0.5, 1.0),  # 前日高値ブレイク
}
STRATEGY_DEFS_AGGRESSIVE: dict[str, tuple[float, float, float]] = {
    "PREV_CLOSE_BREAK": (0.0, 0.5, 0.75),
    "GAP_UP":           (0.0, 0.5, 0.75),
    "GAP_DOWN":         (0.0, 0.5, 0.75),
    "ORB30":            (0.0, 0.5, 0.75),
    "PREV_HIGH_BREAK":  (0.0, 0.5, 0.75),
}

import os as _os
_trading_mode = _os.getenv("TRADING_MODE", "conservative").lower()
if "--aggressive" in sys.argv:
    _trading_mode = "aggressive"
STRATEGY_DEFS = (STRATEGY_DEFS_AGGRESSIVE if _trading_mode == "aggressive"
                 else STRATEGY_DEFS_CONSERVATIVE)
TRADING_MODE = _trading_mode

# ── Walk-forward fold 定義 ───────────────────────────────────────
# 通常 (quarantine_5m の2年データ前提, スイング版と同一設計)
FOLDS_NORMAL: list[tuple[str, int, int, int, int]] = [
    ("fold1", 730, 370, 370, 180),  # TRAIN 12M / TEST 6M
    ("fold2", 550, 180, 180,   0),  # TRAIN 12M / TEST 6M
]

# 短縮版 (ローカルデータが1年未満 or yfinance 60日制限用)
FOLDS_SHORT: list[tuple[str, int, int, int, int]] = [
    ("fold1", 365, 180, 180, 90),   # TRAIN 6M / TEST 3M
    ("fold2", 270,  90,  90,  0),   # TRAIN 6M / TEST 3M
]

# デフォルト
FOLDS = FOLDS_NORMAL

# ── 合格閾値 ─────────────────────────────────────────────────────
# デイトレは1日1トレード上限 + 強制決済があるため、スイング版より閾値を緩和
TRAIN_MIN_TRADES = 5
TRAIN_MIN_PF     = 1.1    # スイング版 1.3 → デイトレ緩和
TRAIN_MIN_WR     = 45.0   # スイング版 50% → デイトレ緩和
TEST_MIN_TRADES  = 3
TEST_MIN_PF      = 1.0    # スイング版 1.1 → デイトレ緩和
TEST_MIN_WR      = 40.0   # スイング版 45% → デイトレ緩和

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
    max_price: float = 0.0, min_price: float = 0.0,
    source: str = "local", data_days: int = 800,
) -> dict | None:
    """
    1銘柄 × 1戦略の Walk-forward を実行。
    合格条件を満たせば dict を、満たさなければ None を返す。
    """
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
    if min_price > 0 and latest_price < min_price:
        return None

    return _walkforward_with_df(symbol, name, strategy_name, df_all, latest_price)


def _walkforward_with_df(
    symbol: str, name: str, strategy_name: str,
    df_all: "pd.DataFrame", latest_price: float,
) -> dict | None:
    """事前ロード済み df で 1銘柄×1戦略の Walk-forward を実行 (load を含まない)。"""
    em, sm, tm = STRATEGY_DEFS[strategy_name]

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


def walkforward_symbol(
    symbol: str, name: str, strategy_list: list[str],
    max_price: float = 0.0, min_price: float = 0.0,
    source: str = "local", data_days: int = 800,
) -> dict[str, dict]:
    """
    1銘柄のデータを **1回だけ** ロードし、全戦略を回す (メモリ&I/O削減)。
    返り値: {strategy_name: result_dict}  (合格戦略のみ)
    """
    import gc as _gc
    df_all = load_intraday(symbol, days=data_days, source=source)
    if df_all is None or df_all.empty or len(df_all) < 50:
        return {}
    try:
        latest_price = float(df_all["close"].iloc[-1])
    except Exception:
        return {}
    if latest_price <= 0:
        return {}
    if max_price > 0 and latest_price > max_price:
        return {}
    if min_price > 0 and latest_price < min_price:
        return {}

    out: dict[str, dict] = {}
    for strat in strategy_list:
        try:
            r = _walkforward_with_df(symbol, name, strat, df_all, latest_price)
        except Exception:
            r = None
        if r:
            out[strat] = r
    # 大きな df を即解放 (他アプリのメモリ圧迫を防ぐ)
    del df_all
    _gc.collect()
    return out


# ── メイン ───────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="デイトレ版 Walk-forward 銘柄スキャナー"
    )
    parser.add_argument("--strategy", default=None,
                        choices=list(STRATEGY_DEFS_CONSERVATIVE.keys()) + ["ALL"],
                        help="戦略名 (省略時は全戦略). ALL=全戦略")
    parser.add_argument("--source",   default="local",
                        choices=["local", "auto", "yfinance"],
                        help="データソース (デフォルト: local)")
    parser.add_argument("--workers",  type=int, default=2,
                        help="並列数 (大きいほど高速だがメモリ消費増。"
                             "他アプリが落ちる場合は 1〜2 に下げる)")
    parser.add_argument("--top",      type=int, default=30,
                        help="表示する上位N (CSVは全件保存)")
    parser.add_argument("--symbols",  type=str, default=None,
                        help="ユニバースファイル (省略時は自動検出)")
    parser.add_argument("--symbols-from-data", action="store_true",
                        help="ユニバースをローカルデータ (quarantine_5m + minute_5m) から自動生成")
    parser.add_argument("--limit",    type=int, default=0,
                        help="ユニバースを先頭N件に制限 (デバッグ用)")
    parser.add_argument("--max-price", type=float, default=0.0,
                        help="最新終値の上限 (円/株)")
    parser.add_argument("--min-price", type=float, default=0.0,
                        help="最新終値の下限 (円/株). 低位株除外 (例: 1000)")
    parser.add_argument("--budget",    type=float, default=0.0,
                        help="総予算 (円). FIXED_QTY=100株換算で --max-price と同義")
    parser.add_argument("--folds-short", action="store_true",
                        help="短縮fold設計 (yfinance 60日制限対応)")
    parser.add_argument("--data-days", type=int, default=800,
                        help="1銘柄あたりのデータ取得期間 (暦日, デフォルト800=約2年)")
    parser.add_argument("--train-pf", type=float, default=None)
    parser.add_argument("--train-wr", type=float, default=None)
    parser.add_argument("--test-pf",  type=float, default=None)
    parser.add_argument("--test-wr",  type=float, default=None)
    parser.add_argument("--train-trades", type=int, default=None)
    parser.add_argument("--test-trades",  type=int, default=None)
    parser.add_argument("--folds-pass-required", type=int, default=None,
                        help="合格に必要なfold数 (デフォルト: 2=全fold通過)")
    parser.add_argument("--relaxed", action="store_true",
                        help="閾値を大幅緩和して『通る銘柄が存在するか』を確認する "
                             "(TRAIN PF>=1.0/WR>=35pct, TEST PF>=0.9/WR>=30pct, fold 1/2)")
    parser.add_argument("--debug-symbol", type=str, default=None,
                        help="指定銘柄の診断 (例: 7203.T). fold別の実際のWR/PFを表示")
    parser.add_argument("--diagnose", action="store_true",
                        help="全銘柄×全戦略のトレードを集計し決済理由(目標達成/損切り/"
                             "引け強制)の内訳を表示 (なぜ負けるかの定量分析)")
    parser.add_argument("--gap-min-pct", type=float, default=None,
                        help="GAP_DOWN 最小ギャップ率 (例:0.015=1.5pct). 浅い下げを除外")
    parser.add_argument("--gap-max-pct", type=float, default=None,
                        help="GAP_DOWN 最大ギャップ率 (例:0.06=6pct). 深い急落を除外")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true")
    mode_group.add_argument("--conservative", action="store_true")
    args = parser.parse_args()

    # ── 閾値上書き ─────────────────────────────────────────────
    global TRAIN_MIN_PF, TRAIN_MIN_WR, TEST_MIN_PF, TEST_MIN_WR, FOLDS, FOLDS_PASS_REQUIRED
    global TRAIN_MIN_TRADES, TEST_MIN_TRADES
    if args.relaxed:
        # 「そもそも通る銘柄が存在するか」を確認するための大幅緩和プリセット
        TRAIN_MIN_PF, TRAIN_MIN_WR = 1.0, 35.0
        TEST_MIN_PF,  TEST_MIN_WR  = 0.9, 30.0
        FOLDS_PASS_REQUIRED = 1
    if args.train_pf is not None:
        TRAIN_MIN_PF = args.train_pf
    if args.train_wr is not None:
        TRAIN_MIN_WR = args.train_wr
    if args.test_pf is not None:
        TEST_MIN_PF = args.test_pf
    if args.test_wr is not None:
        TEST_MIN_WR = args.test_wr
    if args.train_trades is not None:
        TRAIN_MIN_TRADES = args.train_trades
    if args.test_trades is not None:
        TEST_MIN_TRADES = args.test_trades
    if args.folds_pass_required is not None:
        FOLDS_PASS_REQUIRED = args.folds_pass_required

    # GAP_DOWN ギャップ幅フィルタの閾値上書き (backtest_intraday のモジュール定数)
    import backtest_intraday as _bti
    if args.gap_min_pct is not None:
        _bti.GAP_DOWN_MIN_PCT = args.gap_min_pct
    if args.gap_max_pct is not None:
        _bti.GAP_DOWN_MAX_PCT = args.gap_max_pct
    print(f"  GAP_DOWNギャップ幅フィルタ: "
          f"{_bti.GAP_DOWN_MIN_PCT*100:.1f}pct 〜 {_bti.GAP_DOWN_MAX_PCT*100:.1f}pct")

    if args.folds_short:
        FOLDS = FOLDS_SHORT

    effective_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        effective_max_price = args.budget / 100.0

    strategies = ([args.strategy] if args.strategy and args.strategy != "ALL"
                  else list(STRATEGY_DEFS.keys()))

    # ── デバッグモード: 指定銘柄の fold 別詳細表示 ────────────────
    if args.debug_symbol:
        sym = args.debug_symbol
        print(f"\n=== 診断モード: {sym} ===")
        print(f"  合格閾値: TRAIN WR>={TRAIN_MIN_WR}% PF>={TRAIN_MIN_PF}  "
              f"TEST WR>={TEST_MIN_WR}% PF>={TEST_MIN_PF}")
        df_all = load_intraday(sym, days=args.data_days, source=args.source)
        if df_all is None or df_all.empty:
            print(f"  [ERROR] データなし ({sym})")
            return
        print(f"  データ: {len(df_all)}本  "
              f"{df_all.index[0].date()} 〜 {df_all.index[-1].date()}")

        def _fmt(r: dict | None) -> str:
            if not r or r.get("trades", 0) == 0:
                return "データ不足/0取引"
            pf_str = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "∞"
            return (f"trades={r['trades']:3d}  WR={r['win_rate']:5.1f}%  "
                    f"PF={pf_str}  PnL={r['total_pnl']:+,.0f}円")

        def _dump_worst(r: dict | None, n: int = 3) -> None:
            """損益絶対値が大きい順に上位nトレードの明細を表示 (異常バー特定用)。"""
            if not r:
                return
            log = r.get("trade_log", [])
            if not log:
                return
            worst = sorted(log, key=lambda t: abs(t.get("pnl", 0)), reverse=True)[:n]
            for t in worst:
                ed = t.get("entry_dt")
                xd = t.get("exit_dt")
                ed_s = ed.strftime("%Y-%m-%d %H:%M") if ed is not None else "?"
                xd_s = xd.strftime("%H:%M") if xd is not None else "?"
                print(f"        ◆ pnl={t.get('pnl',0):+,.0f}円  "
                      f"entry={t.get('entry_p')}→exit={t.get('exit_p')}  "
                      f"atr5m={t.get('atr_5m')}  stop={t.get('stop_p')} "
                      f"tgt={t.get('target_p')}  {t.get('reason')}  "
                      f"{ed_s}〜{xd_s}")

        for strat in strategies:
            em, sm, tm = STRATEGY_DEFS[strat]
            print(f"\n  【{strat}】 em={em} sm={sm} tm={tm}")
            for fold_name, ts, te, vs, ve in FOLDS:
                train_r = _run_window(sym, sym, df_all, em, sm, tm, ts, te, strat)
                test_r  = _run_window(sym, sym, df_all, em, sm, tm, vs, ve, strat)
                ts_d = (TODAY - timedelta(days=ts)).isoformat()
                te_d = (TODAY - timedelta(days=te)).isoformat()
                vs_d = (TODAY - timedelta(days=vs)).isoformat()
                ve_d = (TODAY - timedelta(days=ve)).isoformat() if ve > 0 else TODAY.isoformat()
                ok_t = "✓" if _passes_train(train_r) else "✗"
                ok_v = "✓" if _passes_test(test_r)  else "✗"
                print(f"    {fold_name} TRAIN{ok_t} [{ts_d}〜{te_d}]: {_fmt(train_r)}")
                print(f"    {fold_name} TEST {ok_v} [{vs_d}〜{ve_d}]: {_fmt(test_r)}")
                # 異常検知: 1トレードあたり平均|pnl|が大きい窓は明細をダンプ
                for lbl, r in (("TRAIN", train_r), ("TEST", test_r)):
                    if r and r.get("trades", 0) > 0:
                        avg_abs = abs(r["total_pnl"]) / r["trades"]
                        if avg_abs > 20000:  # 100株なら1取引2万円超は異常
                            print(f"      ⚠ {lbl} 異常明細 (avg|pnl|={avg_abs:,.0f}円/取引):")
                            _dump_worst(r)
        return

    # ユニバース読み込み
    if args.symbols_from_data:
        from daytrade_data import available_local_symbols
        raw_codes = available_local_symbols()  # JQuants形式 (例: 72030)
        # quarantine_symbols() で yfinance 形式に変換済みの分 + available_local のマージ
        q_syms = set(quarantine_symbols())
        a_syms = set(f"{c[:4]}.T" for c in raw_codes if len(c) == 5 and c.isdigit())
        all_syms = sorted(q_syms | a_syms)
        symbols = [(s, s) for s in all_syms]
        universe_name = "local_data (quarantine_5m + minute_5m)"
        print(f"[INFO] --symbols-from-data: {len(symbols)}銘柄 (ローカルデータから自動生成)")
    else:
        try:
            symbols, universe_name = load_universe(args.symbols)
        except RuntimeError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)

    if args.limit > 0:
        symbols = symbols[:args.limit]

    # ── 定量診断モード: 決済理由の内訳を集計 ──────────────────────
    if args.diagnose:
        from collections import defaultdict
        import gc as _gc
        print(f"\n=== 定量診断: {len(symbols)}銘柄 × {len(strategies)}戦略 ===")
        print("  各トレードの決済理由を集計し『なぜ負けるか』を分析します。\n")
        # strategy -> reason -> {n, pnl, pct, hold_min}
        agg: dict = {s: defaultdict(lambda: {"n": 0, "pnl": 0.0,
                     "pct": 0.0, "hold": 0.0}) for s in strategies}

        def _diag_one(sym: str, sym_name: str) -> dict:
            """1銘柄をロードし全戦略の決済理由を集計して返す (並列ワーカー)。"""
            df = load_intraday(sym, days=args.data_days, source=args.source)
            if df is None or df.empty or len(df) < 50:
                return {}
            local: dict = {}
            for strat in strategies:
                em, sm, tm = STRATEGY_DEFS[strat]
                try:
                    res = run_intraday_backtest(
                        sym, sym_name, df, em, sm, tm,
                        backtest_days=args.data_days, strategy_name=strat)
                except Exception:
                    continue
                for t in res.get("all_trades", []):
                    reason = t.get("reason", "?")
                    a = local.setdefault(strat, {}).setdefault(
                        reason, {"n": 0, "pnl": 0.0, "pct": 0.0, "hold": 0.0})
                    a["n"] += 1
                    a["pnl"] += t.get("pnl", 0.0)
                    a["pct"] += t.get("pct", 0.0)
                    try:
                        a["hold"] += (t["exit_dt"] - t["entry_dt"]).total_seconds() / 60
                    except Exception:
                        pass
            del df
            return local

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_diag_one, s, n): s for s, n in symbols}
            for fut in as_completed(futs):
                done += 1
                try:
                    local = fut.result()
                except Exception:
                    local = {}
                for strat, rows in local.items():
                    for reason, a in rows.items():
                        g = agg[strat][reason]
                        g["n"] += a["n"]; g["pnl"] += a["pnl"]
                        g["pct"] += a["pct"]; g["hold"] += a["hold"]
                if done % 25 == 0:
                    print(f"  進捗: {done}/{len(symbols)}", flush=True)
                    _gc.collect()

        for strat in strategies:
            em, sm, tm = STRATEGY_DEFS[strat]
            rows = agg[strat]
            total_n = sum(r["n"] for r in rows.values())
            total_pnl = sum(r["pnl"] for r in rows.values())
            print(f"\n  【{strat}】 em={em} sm={sm} tm={tm}  "
                  f"全{total_n}取引  総PnL={total_pnl:+,.0f}円")
            if total_n == 0:
                print("    取引なし")
                continue
            print(f"    {'決済理由':<10}{'件数':>7}{'割合':>7}"
                  f"{'平均pnl':>11}{'平均%':>8}{'平均保有分':>10}{'合計pnl':>13}")
            print("    " + "-" * 64)
            # 件数の多い順
            for reason in sorted(rows, key=lambda k: -rows[k]["n"]):
                r = rows[reason]
                if r["n"] == 0:
                    continue
                print(f"    {reason:<10}{r['n']:>7}{r['n']/total_n*100:>6.1f}%"
                      f"{r['pnl']/r['n']:>+11,.0f}{r['pct']/r['n']:>+8.2f}"
                      f"{r['hold']/r['n']:>9.0f}分{r['pnl']:>+13,.0f}")
        print("\n完了。")
        return

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
    if args.min_price > 0:
        print(f"  価格下限  : {args.min_price:,.0f}円/株")
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

    # ── 銘柄ごとに1回だけロードして全戦略を回す (メモリ&I/O を約6倍削減) ──
    import gc as _gc
    per_strategy: dict[str, list[dict]] = {s: [] for s in strategies}
    print(f"\n=== スキャン中 ({len(symbols)}銘柄 × {len(strategies)}戦略) ===")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(
                walkforward_symbol, sym, sym_name, strategies,
                effective_max_price, args.min_price,
                args.source, args.data_days,
            ): sym
            for sym, sym_name in symbols
        }
        done = 0
        n_cand = 0
        progress_every = max(len(symbols) // 20, 25)
        for fut in as_completed(futs):
            done += 1
            try:
                d = fut.result()  # {strategy: result}
                for strat, r in d.items():
                    per_strategy[strat].append(r)
                    n_cand += 1
            except Exception:
                pass
            if done % progress_every == 0 or done == len(symbols):
                print(f"  進捗: {done}/{len(symbols)}  候補(延べ): {n_cand}",
                      flush=True)
                _gc.collect()  # 完了済み future のメモリを定期的に解放

    fold_suffix = "_short" if args.folds_short else ""
    for strategy in strategies:
        results = per_strategy[strategy]
        results.sort(key=lambda r: (-r["folds_passed"], -r["total_test_pnl"]))

        print(f"\n=== {strategy} ===")
        print(f"  {strategy}: 全候補={len(results)}")

        # ── CSV 保存 ──
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
