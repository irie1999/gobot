"""
scan_walkforward.py  ―  Walk-forward 方式による銘柄スキャン
=================================================================
ユニバース (デフォルト=プライム市場 ~1800銘柄) × 10戦略
  (MACD/A7/RSI2 逆指値B + DON/VOL/MOM ブレイクアウト
   + A7_S/DON_S/MOM_S/GAP_S 空売り)
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
  python scan_walkforward.py                      # 全長戦略 (6つ, --family both)
  python scan_walkforward.py --family all         # 全10戦略 (空売り含む)
  python scan_walkforward.py --family stop        # 逆指値Bのみ
  python scan_walkforward.py --family breakout    # ブレイクアウトのみ
  python scan_walkforward.py --family short       # 空売りA7_Sのみ
  python scan_walkforward.py --family short_brk   # 空売りブレイクアウトのみ
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
from check_signals_short import calc_a7_short, calc_rsi2_short, calc_macd_short
from check_signals_short_breakout import (
    calc_donchian_short, calc_momentum_short, calc_gap_short,
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
# (calc_fn, entry_atr_mult, stop_atr_mult, target_atr_mult, family, entry_type)
# TRADING_MODE=aggressive のとき積極利確プリセットを使う
STRATEGY_DEFS_CONSERVATIVE: dict[str, tuple] = {
    "MACD":  (calc_macd,          0.0, 1.5, 3.0, "stop",      "stop"),
    "A7":    (calc_a7,            0.0, 1.5, 3.0, "stop",      "stop"),
    "RSI2":  (calc_rsi2,          0.0, 2.0, 4.0, "stop",      "stop"),
    "DON":   (calc_donchian,      0.0, 1.5, 3.0, "breakout",  "stop"),
    "VOL":   (calc_vol_breakout,  0.0, 1.5, 3.0, "breakout",  "stop"),
    "MOM":   (calc_momentum,      0.0, 1.5, 3.0, "breakout",  "stop"),
    "A7_S":  (calc_a7_short,      0.0, 1.5, 2.5, "short",     "stop_sell"),  # turnover最適 tm=2.5
    "RSI2_S":(calc_rsi2_short,    0.0, 1.5, 2.5, "short",     "stop_sell"),  # turnover最適 tm=2.5
    "MACD_S":(calc_macd_short,    0.0, 1.5, 2.0, "short",     "stop_sell"),  # turnover最適 tm=2.0
    "DON_S": (calc_donchian_short,0.0, 1.5, 3.0, "short_brk", "stop_sell"),  # turnover最適 tm=3.0 据置
    "MOM_S": (calc_momentum_short,0.0, 1.5, 3.0, "short_brk", "stop_sell"),
    "GAP_S": (calc_gap_short,     0.0, 2.0, 1.5, "short_brk", "stop_sell"),  # sweep最適
}
STRATEGY_DEFS_AGGRESSIVE: dict[str, tuple] = {
    "MACD":  (calc_macd,          0.0, 1.5, 2.0, "stop",      "stop"),
    "A7":    (calc_a7,            0.0, 1.5, 2.0, "stop",      "stop"),
    "RSI2":  (calc_rsi2,          0.0, 1.5, 2.0, "stop",      "stop"),
    "DON":   (calc_donchian,      0.0, 1.5, 2.0, "breakout",  "stop"),
    "VOL":   (calc_vol_breakout,  0.0, 1.5, 2.0, "breakout",  "stop"),
    "MOM":   (calc_momentum,      0.0, 1.5, 2.0, "breakout",  "stop"),
    "A7_S":  (calc_a7_short,      0.0, 1.5, 2.0, "short",     "stop_sell"),
    "RSI2_S":(calc_rsi2_short,    0.0, 1.5, 1.5, "short",     "stop_sell"),  # sweep最適
    "MACD_S":(calc_macd_short,    0.0, 1.5, 2.0, "short",     "stop_sell"),
    "DON_S": (calc_donchian_short,0.0, 1.5, 2.0, "short_brk", "stop_sell"),
    "MOM_S": (calc_momentum_short,0.0, 1.5, 2.0, "short_brk", "stop_sell"),
    "GAP_S": (calc_gap_short,     0.0, 2.0, 1.5, "short_brk", "stop_sell"),  # sweep最適
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
    ("fold1", 730, 370, 370, 180),  # TRAIN 12M / TEST 6M
    ("fold2", 550, 180, 180,   0),  # TRAIN 12M / TEST 6M
]

# ── 歴史WF用 Fold 定義（as_of_date 基準 / TRAIN 2年 / TEST 1年 × 3fold）──────
# 現行 FOLDS とは完全独立。--wf-until 使用時のみ参照。現行WATCHLISTに影響なし。
# TRAIN 2yr×3fold: 最大遡及 1825日（5年分）。TRAIN 3yr×4fold から削減してデータ取得を軽量化。
FOLDS_HISTORICAL: list[tuple[str, int, int, int, int]] = [
    ("fold1", 2190, 1460, 1460, 1095),  # TRAIN 2yr: -6yr~-4yr, TEST 1yr: -4yr~-3yr (最古)
    ("fold2", 1825, 1095, 1095,  730),  # TRAIN 2yr: -5yr~-3yr, TEST 1yr: -3yr~-2yr (中間)
    ("fold3", 1460,  730,  730,  365),  # TRAIN 2yr: -4yr~-2yr, TEST 1yr: -2yr~-1yr (最新)
]
# ※ TEST期間が時系列順・非重複: fold1→fold2→fold3 が連続
# ※ as_of直前1年(0〜-1yr)はどのfoldにも使わず、以降がOOS
_HIST_MAX_LOOKBACK = 2190  # FOLDS_HISTORICAL の最大遡及日数（as_of_date からの日数）

# ── 合格閾値 ─────────────────────────────────────────────────────
TRAIN_MIN_TRADES = 5   # 12M TRAINに合わせて引き上げ
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


def _run_window_abs(symbol: str, name: str, full_df,
                    calc_fn, em: float, sm: float, tm: float,
                    window_start, window_end,
                    strategy_name: str, entry_type: str = "stop") -> dict | None:
    """絶対日付でウィンドウを指定する _run_window の変形（歴史WF用）。

    _TODAY を変更せず、df を window_end でトリムし、
    backtest_days = (TODAY - window_start).days にすることで
    run_limit_backtest 内の cutoff = _TODAY - backtest_days = window_start が成立する。
    """
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


def walkforward_one_asof(symbol: str, name: str, strategy_name: str,
                         as_of_date,
                         max_price: float = 0.0) -> dict | None:
    """as_of_date 基準の FOLDS_HISTORICAL（4fold/TRAIN3年/TEST1年）で WF バックテスト。

    現行の FOLDS / WATCHLIST / walkforward_one には一切影響しない。
    _TODAY を変更せず df トリムで実現（スレッドセーフ）。
    """
    if strategy_name not in STRATEGY_DEFS:
        return None
    calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strategy_name]

    since = as_of_date - timedelta(days=_HIST_MAX_LOOKBACK + 200)
    df_full = fetch(symbol, int((TODAY - since).days * 1.1) + 60,
                    min_start_date=since)
    if df_full is None or len(df_full) < 400:
        return None

    # as_of_date 以降は使わない（未来データ漏洩防止）
    df_full = df_full[df_full.index <= pd.Timestamp(as_of_date)].copy()
    if len(df_full) < 200:
        return None

    try:
        latest_price = float(df_full.iloc[-1]["close"])
    except Exception:
        return None
    if latest_price <= 0 or (max_price > 0 and latest_price > max_price):
        return None

    folds_passed = 0
    train_results: list[dict] = []
    test_results:  list[dict] = []
    fold_detail:   list[dict] = []

    for fold_name, ts, te, vs, ve in FOLDS_HISTORICAL:
        window_train_start = as_of_date - timedelta(days=ts)
        window_train_end   = as_of_date - timedelta(days=te)
        window_test_start  = as_of_date - timedelta(days=vs)
        window_test_end    = as_of_date - timedelta(days=ve)

        train_r = _run_window_abs(symbol, name, df_full, calc_fn, em, sm, tm,
                                  window_train_start, window_train_end,
                                  strategy_name, entry_type=entry_type)
        test_r  = _run_window_abs(symbol, name, df_full, calc_fn, em, sm, tm,
                                  window_test_start, window_test_end,
                                  strategy_name, entry_type=entry_type)

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
            train_pf=round(train_r["pf"], 2) if train_r else 0.0,
            train_wr=round(train_r["win_rate"], 1) if train_r else 0.0,
            train_pnl=train_r["total_pnl"] if train_r else 0.0,
            test_trades=test_r["trades"] if test_r else 0,
            test_pf=round(test_r["pf"], 2) if test_r else 0.0,
            test_wr=round(test_r["win_rate"], 1) if test_r else 0.0,
            test_pnl=test_r["total_pnl"] if test_r else 0.0,
        ))

    if not test_results:
        return None

    total_test_pnl = sum(r.get("total_pnl", 0.0) for r in test_results)
    total_test_tr  = sum(r.get("trades", 0) for r in test_results)
    avg_test_pf    = sum(r.get("pf", 0) for r in test_results) / len(test_results)
    avg_test_wr    = sum(r.get("win_rate", 0) for r in test_results) / len(test_results)

    return dict(
        symbol=symbol, name=name, strategy=strategy_name,
        latest_price=latest_price,
        folds_passed=folds_passed,
        fold_detail=fold_detail,
        total_test_trades=total_test_tr,
        total_test_pnl=total_test_pnl,
        avg_test_pf=round(avg_test_pf, 2),
        avg_test_wr=round(avg_test_wr, 1),
    )


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
    calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strategy_name]

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

    # 平均保有日数 (約定済みトレードのみ)
    filled_trades = [t for t in all_test_trades if t.get("hold_days", 0) > 0]
    avg_hold_days = (
        round(sum(t["hold_days"] for t in filled_trades) / len(filled_trades), 1)
        if filled_trades else 0.0
    )

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


# ── メイン ───────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward 銘柄スキャナー")
    parser.add_argument("--family",  choices=["stop", "breakout", "short", "short_brk", "all", "both"], default="both")
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
    holdout_group = parser.add_mutually_exclusive_group()
    holdout_group.add_argument("--holdout-days", type=int, default=0,
                               help="直近N日をホールドアウト除外。Fold境界を全て+N日ずらす (例: 65)")
    holdout_group.add_argument("--holdout-end", type=str, default=None,
                               help="ホールドアウト終了日 YYYY-MM-DD。その日以降をテスト対象外にする")
    parser.add_argument("--train-pf", type=float, default=None,
                        help="TRAIN期間の合格PF閾値 (デフォルト 1.5。空売り系は 1.1 推奨)")
    parser.add_argument("--test-pf",  type=float, default=None,
                        help="TEST期間の合格PF閾値 (デフォルト 1.2。空売り系は 1.0 推奨)")
    parser.add_argument("--train-wr", type=float, default=None,
                        help="TRAIN期間の合格勝率閾値%% (デフォルト 55。空売り系は 45 推奨)")
    parser.add_argument("--test-wr",  type=float, default=None,
                        help="TEST期間の合格勝率閾値%% (デフォルト 45。空売り系は 40 推奨)")
    parser.add_argument("--relax-short", action="store_true",
                        help="空売り系 (--family short/short_brk) で閾値を自動緩和 "
                             "(TRAIN PF≥1.1/WR≥45%%, TEST PF≥1.0/WR≥40%%)")
    args = parser.parse_args()

    # ── 合格閾値の上書き ───────────────────────────────────────────
    global TRAIN_MIN_PF, TRAIN_MIN_WR, TEST_MIN_PF, TEST_MIN_WR
    is_short_family = args.family in ("short", "short_brk")
    if getattr(args, "relax_short", False) and is_short_family:
        TRAIN_MIN_PF = 1.1
        TRAIN_MIN_WR = 45.0
        TEST_MIN_PF  = 1.0
        TEST_MIN_WR  = 40.0
    if getattr(args, "train_pf", None) is not None:
        TRAIN_MIN_PF = args.train_pf
    if getattr(args, "train_wr", None) is not None:
        TRAIN_MIN_WR = args.train_wr
    if getattr(args, "test_pf", None) is not None:
        TEST_MIN_PF = args.test_pf
    if getattr(args, "test_wr", None) is not None:
        TEST_MIN_WR = args.test_wr

    # ── ホールドアウト計算 ──────────────────────────────────────────
    holdout_days = 0
    if args.holdout_days > 0:
        holdout_days = args.holdout_days
    elif args.holdout_end:
        holdout_end_date = datetime.strptime(args.holdout_end, "%Y-%m-%d").date()
        holdout_days = (TODAY - holdout_end_date).days
        if holdout_days < 0:
            print(f"[ERROR] --holdout-end {args.holdout_end} は未来日です", file=sys.stderr)
            sys.exit(1)

    if holdout_days > 0:
        holdout_end_date = TODAY - timedelta(days=holdout_days)
        FOLDS[:] = [
            (n, ts + holdout_days, te + holdout_days,
                vs + holdout_days, ve + holdout_days)
            for n, ts, te, vs, ve in FOLDS
        ]

    # budget → max_price 換算 (FIXED_QTY=100 株)
    effective_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        effective_max_price = args.budget / 100.0

    _FAMILY_STRATS = {
        "stop":      ["MACD", "A7", "RSI2"],
        "breakout":  ["DON", "VOL", "MOM"],
        "short":     ["A7_S", "MACD_S", "RSI2_S"],
        "short_brk": ["DON_S", "MOM_S", "GAP_S"],
        "both":      ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"],
        "all":       ["MACD", "A7", "RSI2", "DON", "VOL", "MOM",
                      "A7_S", "MACD_S", "RSI2_S", "DON_S", "MOM_S", "GAP_S"],
    }
    strategies = _FAMILY_STRATS[args.family]

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
    if holdout_days > 0:
        print(f"  ホールドアウト: 直近 {holdout_days} 日 ({holdout_end_date} 以降をテスト対象外)")
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
        holdout_suffix = f"_holdout{holdout_days}d" if holdout_days > 0 else ""
        csv_path = out_dir / f"walkforward_{strategy}{mode_suffix}{holdout_suffix}_{TODAY}.csv"
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
