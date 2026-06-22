#!/usr/bin/env python3
"""
run_historical_wf_validation.py — 歴史WF銘柄選定 × OOS検証レポート
=====================================================================

指定年（デフォルト2021〜今年直前）の各1月1日を起点として：
  1. HO30d〜HO180d の 6 ホールドアウト設定それぞれで WF 銘柄選定
     （現行 FOLDS 構造・2fold/12M train/6M test と同一）
  2. 起点〜今日（OOS期間）のバックテストで損益を検証
  3. as-of 期間別タブ付き HTML を生成（run_signals_holdout_all.py 形式に準拠）

【目的】
  銘柄選定パイプライン自体の有効性を複数の歴史的起点で検証する。
  「選定時に知らなかったデータで本当に機能したか」をメタ WalkForward で確認。

【計算量】
  5 as-of 日付 × 6 HO 設定 = 30 スキャン（初回のみ、2回目以降は CSV キャッシュ）

【使い方】
  python run_historical_wf_validation.py
  python run_historical_wf_validation.py --start-year 2022
  python run_historical_wf_validation.py --max-price 6000 --min-price 1000
  python run_historical_wf_validation.py --workers 8
  python run_historical_wf_validation.py --force        # 強制再スキャン
  python run_historical_wf_validation.py --scan-only    # スキャンのみ
  python run_historical_wf_validation.py --no-browser

【キャッシュ】
  walkforward_results/asof_YYYY-MM-DD/walkforward_STRATEGY_holdout{N}d_YYYY-MM-DD.csv
  現行 run_signals_holdout_all.py の CSV 命名規則に合わせている。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import webbrowser
from _open_html import open_html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

# OpenBLAS/MKL が ThreadPoolExecutor の各ワーカー内でさらにスレッドを起動しないよう制限。
# これを pandas/numpy より前に設定しないと効果がない。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import pandas as pd

from backtest_limit_entry import (
    fetch,
    WORKERS as _DEFAULT_WORKERS,
)
from risk_metrics import enrich_backtest_result
from scan_walkforward import (
    STRATEGY_DEFS,
    FOLDS,
    _run_window_abs,
    _passes_train,
    _passes_test,
    load_universe,
    INITIAL_CASH,
)

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

PER_STRATEGY = 10
MIN_FOLDS    = 2

STOP_STRATS     = {"MACD", "A7", "RSI2"}
BREAKOUT_STRATS = {"DON", "VOL", "MOM"}
ALL_STRATS      = list(STOP_STRATS) + list(BREAKOUT_STRATS)

# 現行 run_signals_holdout_all.py と同じ HO 設定（色も合わせる）
HOLDOUT_CONFIGS: list[tuple[int, str, str]] = [
    (30,  "HO30d",  "#3b82f6"),
    (60,  "HO60d",  "#06b6d4"),
    (90,  "HO90d",  "#10b981"),
    (120, "HO120d", "#84cc16"),
    (150, "HO150d", "#f59e0b"),
    (180, "HO180d", "#ef4444"),
]

RESULTS_DIR = Path("walkforward_results")

_CSV_FIELDS = [
    "symbol", "name", "strategy", "family",
    "latest_price", "folds_passed",
    "total_test_trades", "avg_hold_days",
    "total_test_pnl", "avg_test_pf", "avg_test_wr",
    "max_drawdown_pct", "max_consecutive_losses", "sharpe",
    "recovery_factor", "train_to_test_degradation_pct",
]


# ────────────────────────────────────────────────────────────
# as-of 日付の自動生成
# ────────────────────────────────────────────────────────────
def _gen_asof_dates(start_year: int = 2021, end_year: int | None = None) -> list[date]:
    """start_year〜(end_year-1) の各 1/1 を返す。OOS 確保のため 180 日以上前のみ。"""
    if end_year is None:
        end_year = TODAY.year
    return [
        date(y, 1, 1)
        for y in range(start_year, end_year)
        if (TODAY - date(y, 1, 1)).days >= 180
    ]


# ────────────────────────────────────────────────────────────
# フォールド絶対日付の生成（現行 FOLDS を as_of 基準にシフト）
# ────────────────────────────────────────────────────────────
def _make_folds_abs(as_of: date, holdout_days: int) -> list[tuple]:
    """
    現行 FOLDS 構造（2fold/12M-train/6M-test）を as_of 基準の絶対日付に変換。

    cutoff = as_of - holdout_days
    fold1: TRAIN [cutoff-730d, cutoff-370d] / TEST [cutoff-370d, cutoff-180d]
    fold2: TRAIN [cutoff-550d, cutoff-180d] / TEST [cutoff-180d, cutoff]
    """
    cutoff = as_of - timedelta(days=holdout_days)
    return [
        (name,
         cutoff - timedelta(days=ts),
         cutoff - timedelta(days=te),
         cutoff - timedelta(days=vs),
         cutoff - timedelta(days=ve))
        for name, ts, te, vs, ve in FOLDS
    ]


# ────────────────────────────────────────────────────────────
# 1 銘柄 × 1 戦略のスキャン
# ────────────────────────────────────────────────────────────
def _scan_symbol(
    symbol: str, name: str, strategy_name: str,
    folds_abs: list[tuple],
    as_of: date,
    max_price: float,
    min_price: float,
) -> dict | None:
    """as_of 基準の WF バックテスト（1 銘柄 × 1 戦略）"""
    if strategy_name not in STRATEGY_DEFS:
        return None
    calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strategy_name]

    earliest = min(ts for _, ts, te, vs, ve in folds_abs)
    since    = earliest - timedelta(days=90)
    df_full  = fetch(symbol, (TODAY - since).days + 60, min_start_date=since)
    if df_full is None or len(df_full) < 60:
        return None

    df_full = df_full[df_full.index <= pd.Timestamp(as_of)].copy()
    if len(df_full) < 60:
        return None

    try:
        latest_price = float(df_full.iloc[-1]["close"])
    except Exception:
        return None
    if latest_price <= 0:
        return None
    if max_price > 0 and latest_price > max_price:
        return None
    if min_price > 0 and latest_price < min_price:
        return None

    folds_passed   = 0
    train_results: list[dict] = []
    test_results:  list[dict] = []

    for _, train_s, train_e, test_s, test_e in folds_abs:
        train_r = _run_window_abs(symbol, name, df_full, calc_fn, em, sm, tm,
                                  train_s, train_e, strategy_name, entry_type=entry_type)
        test_r  = _run_window_abs(symbol, name, df_full, calc_fn, em, sm, tm,
                                  test_s, test_e, strategy_name, entry_type=entry_type)
        if _passes_train(train_r) and _passes_test(test_r):
            folds_passed += 1
        if train_r:
            train_results.append(train_r)
        if test_r:
            test_results.append(test_r)

    if not test_results or folds_passed < MIN_FOLDS:
        return None

    all_test_trades: list[dict] = []
    total_test_pnl   = 0.0
    total_test_tr    = 0
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
    train_pnl   = sum(r.get("total_pnl", 0.0) for r in train_results)
    degradation = (train_pnl - total_test_pnl) / train_pnl * 100 if train_pnl > 0 else 0.0

    return dict(
        symbol=symbol, name=name, strategy=strategy_name, family=family,
        latest_price=round(latest_price, 0),
        folds_passed=folds_passed,
        total_test_trades=total_test_tr,
        avg_hold_days=avg_hold_days,
        total_test_pnl=round(total_test_pnl, 0),
        avg_test_pf=round(avg_test_pf, 2),
        avg_test_wr=round(avg_test_wr, 1),
        max_drawdown_pct=agg.get("max_drawdown_pct", 0.0),
        max_consecutive_losses=agg.get("max_consecutive_losses", 0),
        sharpe=agg.get("sharpe", 0.0),
        recovery_factor=agg.get("recovery_factor", 0.0),
        train_to_test_degradation_pct=round(degradation, 1),
    )


# ────────────────────────────────────────────────────────────
# CSV キャッシュ
# ────────────────────────────────────────────────────────────
def _csv_path(as_of: date, strategy: str, ho_days: int) -> Path:
    """現行システムの命名規則に合わせた CSV パス"""
    return (
        RESULTS_DIR / f"asof_{as_of}"
        / f"walkforward_{strategy}_holdout{ho_days}d_{as_of}.csv"
    )


def _save_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ────────────────────────────────────────────────────────────
# 1 as_of × 1 HO 設定のスキャン（キャッシュ付き）
# ────────────────────────────────────────────────────────────
def _scan_one_ho(
    as_of: date,
    ho_days: int,
    ho_label: str,
    symbols: list[tuple[str, str]],
    workers: int,
    max_price: float,
    min_price: float,
    force: bool,
) -> dict[str, list[dict]]:
    """指定 as_of × HO 設定のフルスキャン（全戦略）"""
    folds_abs = _make_folds_abs(as_of, holdout_days=ho_days)
    results: dict[str, list[dict]] = {}
    need_scan: list[str] = []

    for strat in ALL_STRATS:
        path = _csv_path(as_of, strat, ho_days)
        if not force and path.exists():
            rows = _load_csv(path)
            results[strat] = rows
        else:
            need_scan.append(strat)
            results[strat] = []

    cached_count = len(ALL_STRATS) - len(need_scan)
    if cached_count:
        total_cached = sum(len(results[s]) for s in ALL_STRATS if s not in need_scan)
        print(f"    [{ho_label}] cache {cached_count}戦略 ({total_cached}件合格)", end="")
        if need_scan:
            print(f" / scan {len(need_scan)}戦略", end="")
        print()

    if not need_scan:
        return results

    tasks = [(sym, nm, strat) for sym, nm in symbols for strat in need_scan]
    batch: dict[str, list[dict]] = {s: [] for s in need_scan}

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futs = {
            exe.submit(_scan_symbol, sym, nm, strat, folds_abs, as_of, max_price, min_price)
            : (strat,)
            for sym, nm, strat in tasks
        }
        done = 0
        total = len(futs)
        for fut in as_completed(futs):
            strat, = futs[fut]
            done += 1
            try:
                r = fut.result()
                if r:
                    batch[strat].append(r)
            except Exception:
                pass
            if done % 1000 == 0 or done == total:
                ok = sum(len(v) for v in batch.values())
                print(f"    [{ho_label}] {done}/{total} 完了 (合格: {ok}件)")

    for strat in need_scan:
        rows = batch[strat]
        _save_csv(rows, _csv_path(as_of, strat, ho_days))
        results[strat] = rows

    total_ok = sum(len(v) for v in results.values())
    print(f"    [{ho_label}] スキャン完了: {total_ok}件合格")
    return results


# ────────────────────────────────────────────────────────────
# スキャン結果から WATCHLIST を構築
# ────────────────────────────────────────────────────────────
def _flt(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _build_watchlist(
    results: dict[str, list[dict]],
    per_strategy: int,
    min_price: float,
    max_price: float,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    stop_seen: dict[tuple, float] = {}
    brk_seen:  dict[tuple, float] = {}

    for strat, rows in results.items():
        if strat not in STOP_STRATS and strat not in BREAKOUT_STRATS:
            continue
        filtered = [
            r for r in rows
            if _flt(r.get("total_test_pnl", 0)) > 0
            and _flt(r.get("max_drawdown_pct", 100)) <= 15
            and (min_price == 0 or _flt(r.get("latest_price", 0)) >= min_price)
            and (max_price == 0 or _flt(r.get("latest_price", 0)) <= max_price)
        ]
        filtered.sort(
            key=lambda r: _flt(r.get("total_test_pnl", 0)) * (1 + max(_flt(r.get("sharpe", 0)), 0)),
            reverse=True,
        )
        for r in filtered[:per_strategy]:
            triple = (str(r["symbol"]), str(r.get("name", "")), strat)
            score  = _flt(r.get("total_test_pnl", 0))
            if strat in STOP_STRATS:
                if score > stop_seen.get(triple, -1e9):
                    stop_seen[triple] = score
            else:
                if score > brk_seen.get(triple, -1e9):
                    brk_seen[triple] = score

    return sorted(stop_seen), sorted(brk_seen)


# ────────────────────────────────────────────────────────────
# BT スコア計算（as_of でトリム済みの df を使用）
# ────────────────────────────────────────────────────────────
def _compute_bt_score_at(sym: str, name: str, df: "pd.DataFrame", strat: str) -> int:
    """df を as_of でトリム済みとして BT スコアを計算（calc_recommend_score と同一式）"""
    if strat not in STRATEGY_DEFS:
        return 0
    calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strat]
    PERIODS = [30, 60, 90, 120, 150, 180]
    as_of_date = df.index[-1].date() if len(df) > 0 else TODAY

    pf_list, wr_list, pnl_list, trade_list = [], [], [], []
    for days in PERIODS:
        ws = as_of_date - timedelta(days=days)
        we = as_of_date
        try:
            r = _run_window_abs(sym, name, df, calc_fn, em, sm, tm,
                                ws, we, strat, entry_type=entry_type)
        except Exception:
            continue
        if r and r.get("trades", 0) > 0:
            raw_pf = r.get("pf", 0)
            capped  = 10.0 if raw_pf == float("inf") else min(raw_pf, 10.0)
            pf_list.append(capped)
            wr_list.append(r.get("win_rate", 0))
            pnl_list.append(r.get("total_pnl", 0))
            trade_list.append(r.get("trades", 0))

    if not pf_list:
        return 0
    avg_pf  = sum(pf_list) / len(pf_list)
    avg_wr  = sum(wr_list) / len(wr_list)
    t_trades = sum(trade_list)
    stable  = sum(1 for p in pnl_list if p > 0) / len(pnl_list)
    return round(avg_wr * 0.4 + (avg_pf / 10) * 30 + stable * 20 + min(t_trades / 20, 1) * 10)


# ────────────────────────────────────────────────────────────
# 年次 P&L 計算
# ────────────────────────────────────────────────────────────
def _compute_yearly_pnl_multi(
    ho_pnl_configs: list[dict],
    as_of: date,
    workers: int,
    thresholds: list[int] = (0, 70),
) -> dict[int, dict[int, dict]]:
    """全OOS期間（as_of〜今日）をシグナル日dedup付きで年次集計。

    BTスコアはローリング方式: 各年のシグナルに対し、その年の1月1日時点のBTスコアを使用。
    - 2021年のシグナル → 2021-01-01 時点のBTスコアで判定
    - 2022年のシグナル → 2022-01-01 時点のBTスコアで判定
    - 2023年のシグナル → 2023-01-01 時点のBTスコアで判定
    これにより「その時点で高スコアだった銘柄」を年ごとに評価できる。

    返り値: {threshold: {year: {pnl, trades, wins}}}
    """
    import threading

    years      = list(range(as_of.year, TODAY.year + 1))
    years_set  = set(years)
    since_date = as_of - timedelta(days=90)
    fetch_days = (TODAY - since_date).days + 60

    empty = {t: {y: {"pnl": 0.0, "trades": 0, "wins": 0} for y in years}
             for t in thresholds}

    # ユニーク (sym, strat) のみでバックテスト実行（計算は1回）
    unique_pairs: list[tuple[str, str, str]] = list({
        (sym, nm, strat)
        for cfg in ho_pnl_configs
        for sym, nm, strat in list(cfg.get("stop_wl", [])) + list(cfg.get("brk_wl", []))
    })
    if not unique_pairs:
        return empty

    need_bt = any(t > 0 for t in thresholds)
    # ローリング: {(sym, strat): {year: bt_score}}
    # 各年の1月1日時点（as_of 以降）のBTスコアを保持
    bt_scores_rolling: dict[tuple, dict[int, int]] = {}
    trade_logs: dict[tuple, list] = {}
    lock = threading.Lock()

    # 各年のBTスコア計算基準日（as_of 以降の各年1月1日）
    year_eval_dates = {
        y: max(date(y, 1, 1), as_of)
        for y in years
    }

    def _run_one(sym_nm_strat):
        sym, nm, strat = sym_nm_strat
        if strat not in STRATEGY_DEFS:
            return
        try:
            df = fetch(sym, fetch_days, min_start_date=since_date)
        except Exception:
            return
        if df is None or len(df) < 10:
            return

        # ローリングBTスコア: 各年1月1日時点のスコアを計算
        scores_by_year: dict[int, int] = {}
        if need_bt:
            for y, eval_dt in year_eval_dates.items():
                try:
                    df_at = df[df.index <= pd.Timestamp(eval_dt)]
                    scores_by_year[y] = _compute_bt_score_at(sym, nm, df_at, strat) if len(df_at) >= 10 else 0
                except Exception:
                    scores_by_year[y] = 0

        # 全OOS期間バックテスト（as_of〜TODAY）
        calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strat]
        try:
            r = _run_window_abs(sym, nm, df, calc_fn, em, sm, tm,
                                as_of, TODAY, strat, entry_type=entry_type)
        except Exception:
            return
        tlog = r.get("trade_log", []) if r else []

        with lock:
            bt_scores_rolling[(sym, strat)] = scores_by_year
            trade_logs[(sym, strat)] = tlog

    with ThreadPoolExecutor(max_workers=workers) as exe:
        list(exe.map(_run_one, unique_pairs))

    # 集計: シグナル日dedup（OOSタブと同じ注釈方式）
    # 同一 (sym, strat, signal_dt) は複数HO configにまたがっても1件
    # BTフィルターはトレードが属する年のBTスコアで判定（ローリング）
    result = {t: {y: {"pnl": 0.0, "trades": 0, "wins": 0, "win_pnl": 0.0, "loss_pnl": 0.0} for y in years}
              for t in thresholds}

    for t in thresholds:
        seen: set[tuple] = set()
        for cfg in ho_pnl_configs:
            for sym, nm, strat in list(cfg.get("stop_wl", [])) + list(cfg.get("brk_wl", [])):
                yr_scores = bt_scores_rolling.get((sym, strat), {})
                for trade in trade_logs.get((sym, strat), []):
                    sig_dt = trade.get("signal_dt")
                    if sig_dt is None:
                        continue
                    try:
                        sig_key = (sym, strat, pd.Timestamp(sig_dt).date())
                    except Exception:
                        continue
                    if sig_key in seen:
                        continue
                    seen.add(sig_key)

                    exit_dt = trade.get("exit_dt")
                    if exit_dt is None:
                        continue
                    try:
                        year = pd.Timestamp(exit_dt).year
                    except Exception:
                        continue
                    if year not in years_set:
                        continue

                    # ローリングBTフィルター: その年のBTスコアで判定
                    if t > 0:
                        bt_score = yr_scores.get(year, 0)
                        if bt_score < t:
                            continue

                    pnl = trade.get("pnl", 0.0)
                    result[t][year]["pnl"]    += pnl
                    result[t][year]["trades"] += 1
                    if pnl > 0:
                        result[t][year]["wins"]    += 1
                        result[t][year]["win_pnl"] += pnl
                    else:
                        result[t][year]["loss_pnl"] += pnl

    return result


# ────────────────────────────────────────────────────────────
# 年次成績推移 HTML
# ────────────────────────────────────────────────────────────
def _make_yearly_table(periods: list[dict], pnl_key: str) -> str:
    """年次成績マトリックステーブルの HTML を生成（pnl_key でデータキーを指定）"""
    all_years = sorted({
        year
        for p in periods
        for year in p.get(pnl_key, {}).keys()
    })
    if not all_years:
        return "<p class='subtitle'>データなし</p>"

    header_cells = "".join(
        f'<th style="padding:8px 14px;text-align:center;color:#94a3b8;'
        f'border-bottom:1px solid #334155">{y}年</th>'
        for y in all_years
    )

    rows_html = ""
    col_totals: dict[int, dict] = {y: {"pnl": 0.0, "trades": 0, "wins": 0} for y in all_years}

    for i, p in enumerate(periods):
        as_of     = p["as_of"]
        oos_days  = (TODAY - as_of).days
        yearly    = p.get(pnl_key, {})
        bg        = "#1e293b" if i % 2 == 0 else "#0f172a"

        oos_label = f"OOS {oos_days // 365}年{(oos_days % 365) // 30}ヶ月"
        row_hdr = (
            f'<td style="padding:8px 14px;color:#60a5fa;font-weight:bold;'
            f'white-space:nowrap;background:{bg}">'
            f'{as_of.year}年起点'
            f'<span style="display:block;font-size:0.75em;color:#94a3b8;font-weight:normal">'
            f'{oos_label}</span></td>'
        )

        cells = ""
        for y in all_years:
            if y < as_of.year:
                cells += (
                    f'<td style="text-align:center;color:#475569;'
                    f'padding:8px 14px;background:{bg}">—</td>'
                )
                continue
            data = yearly.get(y, {})
            pnl    = data.get("pnl", 0.0)
            trades = data.get("trades", 0)
            wins   = data.get("wins", 0)
            if trades == 0:
                cells += (
                    f'<td style="text-align:center;color:#475569;'
                    f'padding:8px 14px;background:{bg}">—</td>'
                )
                continue
            col_totals[y]["pnl"]    += pnl
            col_totals[y]["trades"] += trades
            col_totals[y]["wins"]   += wins
            wr = wins / trades * 100 if trades > 0 else 0.0
            man_yen = pnl / 10000
            color   = "#4ade80" if pnl >= 0 else "#f87171"
            sign    = "+" if pnl >= 0 else ""
            cells += (
                f'<td style="text-align:center;padding:8px 14px;background:{bg}">'
                f'<span style="color:{color};font-weight:bold">{sign}{man_yen:.1f}万</span>'
                f'<br><span style="color:#94a3b8;font-size:0.78em">{trades}件 {wr:.0f}%</span>'
                f'</td>'
            )

        rows_html += f'<tr>{row_hdr}{cells}</tr>\n'

    # 累計合計行
    total_cells = ""
    for y in all_years:
        d      = col_totals[y]
        pnl    = d["pnl"]
        trades = d["trades"]
        wins   = d["wins"]
        if trades == 0:
            total_cells += (
                '<td style="text-align:center;color:#475569;'
                'padding:8px 14px;background:#1e3a5f">—</td>'
            )
        else:
            wr      = wins / trades * 100
            man_yen = pnl / 10000
            color   = "#4ade80" if pnl >= 0 else "#f87171"
            sign    = "+" if pnl >= 0 else ""
            total_cells += (
                f'<td style="text-align:center;padding:8px 14px;background:#1e3a5f">'
                f'<span style="color:{color};font-weight:bold">{sign}{man_yen:.1f}万</span>'
                f'<br><span style="color:#94a3b8;font-size:0.78em">{trades}件 {wr:.0f}%</span>'
                f'</td>'
            )

    total_row = (
        f'<tr>'
        f'<td style="padding:8px 14px;color:#fbbf24;font-weight:bold;background:#1e3a5f">'
        f'累計合計</td>'
        f'{total_cells}'
        f'</tr>'
    )

    return (
        f'<div style="overflow-x:auto">'
        f'<table style="border-collapse:collapse;width:100%;font-size:0.88em">'
        f'<thead><tr>'
        f'<th style="padding:8px 14px;text-align:left;color:#94a3b8;border-bottom:1px solid #334155">'
        f'起点年</th>'
        f'{header_cells}'
        f'</tr></thead>'
        f'<tbody>'
        f'{rows_html}'
        f'{total_row}'
        f'</tbody>'
        f'</table>'
        f'</div>'
    )


def _yearly_analysis_html(periods: list[dict]) -> str:
    """年次成績推移（BT スコアフィルター 2 サブタブ）の HTML を生成"""
    table_all  = _make_yearly_table(periods, "yearly_pnl_all")
    table_bt70 = _make_yearly_table(periods, "yearly_pnl_70")

    return (
        f'<h2>起点年別 年次成績推移</h2>'
        f'<p class="subtitle">各起点年の全OOS期間（起点〜今日）を年ごとに集計。同一シグナル（銘柄+戦略+シグナル日）は重複除外 — OOSタブと同じ集計方式。</p>'
        f'<div style="margin-bottom:16px">'
        f'<button class="ho-period-btn ytab-btn active" onclick="switchYTab(\'all\',this)">全銘柄</button>'
        f'<button class="ho-period-btn ytab-btn" onclick="switchYTab(\'bt70\',this)">BT≥70</button>'
        f'</div>'
        f'<div id="ytab-all" style="display:block">{table_all}</div>'
        f'<div id="ytab-bt70" style="display:none">{table_bt70}</div>'
    )


# ────────────────────────────────────────────────────────────
# OOS 損益 HTML 生成（1 as_of × 6 HO configs を一括評価）
# ────────────────────────────────────────────────────────────
def _eval_oos_html(
    as_of: date,
    ho_configs: list[dict],
    workers: int,
) -> str:
    """as_of〜今日の OOS バックテスト結果 HTML を生成。6 HO 設定を一括注入。"""
    import nikkei_analysis as _na

    oos_days     = (TODAY - as_of).days
    orig_configs = list(_na._PNL_CONFIGS)
    try:
        _na._PNL_CONFIGS[:] = ho_configs
        html = _na._tab5_pnl_html(oos_days, workers, skip_timing9=True)
    finally:
        _na._PNL_CONFIGS[:] = orig_configs
    return html


# ────────────────────────────────────────────────────────────
# 各起点年タブ内の年次内訳テーブル
# ────────────────────────────────────────────────────────────
def _yearly_inline_html(period: dict) -> str:
    """起点年タブの中に表示する年次P&L内訳 + 全OOS期間合計サマリー"""
    as_of    = period["as_of"]
    oos_days = (TODAY - as_of).days

    def _cell(d: dict, bg: str = "") -> str:
        pnl      = d.get("pnl", 0.0)
        trades   = d.get("trades", 0)
        wins     = d.get("wins", 0)
        win_pnl  = d.get("win_pnl", 0.0)
        loss_pnl = d.get("loss_pnl", 0.0)
        if trades == 0:
            return f'<td style="padding:6px 12px;text-align:center;color:#475569{";background:"+bg if bg else ""}">—</td>'
        wr       = wins / trades * 100
        net_man  = pnl / 10000
        win_man  = win_pnl / 10000
        loss_man = loss_pnl / 10000
        net_color = "#4ade80" if pnl >= 0 else "#f87171"
        net_sign  = "+" if pnl >= 0 else ""
        return (
            f'<td style="padding:6px 12px;text-align:center{";background:"+bg if bg else ""}">'
            f'<span style="color:{net_color};font-weight:bold;font-size:0.95em">{net_sign}{net_man:.1f}万</span>'
            f'<br><span style="color:#4ade80;font-size:0.75em">+{win_man:.1f}万</span>'
            f'<span style="color:#94a3b8;font-size:0.75em"> / </span>'
            f'<span style="color:#f87171;font-size:0.75em">{loss_man:.1f}万</span>'
            f'<br><span style="color:#94a3b8;font-size:0.72em">{trades}件 {wr:.0f}%</span>'
            f'</td>'
        )

    yearly_all = period.get("yearly_pnl_all", {})
    yearly_70  = period.get("yearly_pnl_70",  {})
    yearly_80  = period.get("yearly_pnl_80",  {})
    yearly_90  = period.get("yearly_pnl_90",  {})
    years      = sorted(y for y in yearly_all if y >= as_of.year)
    if not years:
        return ""

    # 全OOS期間の合計（年次内訳の合算）
    def _total(yearly: dict) -> dict:
        return {
            "pnl":      sum(v.get("pnl", 0.0)      for v in yearly.values()),
            "trades":   sum(v.get("trades", 0)      for v in yearly.values()),
            "wins":     sum(v.get("wins", 0)        for v in yearly.values()),
            "win_pnl":  sum(v.get("win_pnl", 0.0)  for v in yearly.values()),
            "loss_pnl": sum(v.get("loss_pnl", 0.0) for v in yearly.values()),
        }

    tot_all = _total(yearly_all)
    tot_70  = _total(yearly_70)
    tot_80  = _total(yearly_80)
    tot_90  = _total(yearly_90)

    def _summary_card(label: str, d: dict, color: str) -> str:
        pnl    = d.get("pnl", 0.0)
        trades = d.get("trades", 0)
        wins   = d.get("wins", 0)
        wr     = wins / trades * 100 if trades else 0
        sign   = "+" if pnl >= 0 else ""
        c      = "#4ade80" if pnl >= 0 else "#f87171"
        return (
            f'<div style="background:#1e293b;border-radius:6px;padding:10px 16px;min-width:130px">'
            f'<div style="color:#94a3b8;font-size:0.78em;margin-bottom:4px">{label}</div>'
            f'<div style="color:{c};font-size:1.1em;font-weight:bold">{sign}{pnl/10000:.1f}万</div>'
            f'<div style="color:#94a3b8;font-size:0.78em">{trades}件 {wr:.0f}%</div>'
            f'</div>'
        )

    header = "".join(
        f'<th style="padding:6px 12px;text-align:center;color:#94a3b8;'
        f'border-bottom:1px solid #334155">{y}年</th>'
        for y in years
    )
    row_all = "".join(_cell(yearly_all.get(y, {})) for y in years)
    row_70  = "".join(_cell(yearly_70.get(y, {}))  for y in years)
    row_80  = "".join(_cell(yearly_80.get(y, {}))  for y in years)
    row_90  = "".join(_cell(yearly_90.get(y, {}))  for y in years)
    # 合計列
    row_all += _cell(tot_all, bg="#1e3a5f")
    row_70  += _cell(tot_70,  bg="#1e3a5f")
    row_80  += _cell(tot_80,  bg="#1e3a5f")
    row_90  += _cell(tot_90,  bg="#1e3a5f")

    return f"""
<div style="margin:16px 0 24px 0;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:16px">
  <h3 style="margin:0 0 8px 0;font-size:0.95em;color:#60a5fa">年次 P&amp;L 内訳
    <span style="font-size:0.8em;color:#94a3b8;font-weight:normal;margin-left:8px">
      全OOS期間 {oos_days}日（{as_of} 〜 {TODAY}）/ 同一シグナル重複除外
    </span>
  </h3>
  <p style="color:#f59e0b;font-size:0.8em;margin:0 0 12px 0">
    ⚠ 下の「直近{oos_days}日 取引損益」は内部制限により直近365日分のみの集計です。上の年次内訳が全OOS期間の正確な数値です。<br>
    📊 BT≥70 フィルターはローリング方式（各年1月1日時点のBTスコア）で適用しています。
  </p>
  <div style="overflow-x:auto">
    <table style="border-collapse:collapse;font-size:0.88em;width:auto">
      <thead>
        <tr>
          <th style="padding:6px 16px;text-align:left;color:#94a3b8;border-bottom:1px solid #334155;white-space:nowrap">フィルター</th>
          {header}
          <th style="padding:6px 16px;text-align:center;color:#fbbf24;border-bottom:1px solid #334155;background:#1e3a5f">合計</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td style="padding:6px 12px;color:#94a3b8;white-space:nowrap">全銘柄</td>
          {row_all}
        </tr>
        <tr style="border-top:1px solid #1e293b">
          <td style="padding:6px 12px;color:#94a3b8;white-space:nowrap">BT≥70<br><span style="font-size:0.72em;color:#64748b">各年1月1日時点</span></td>
          {row_70}
        </tr>
        <tr style="border-top:1px solid #1e293b">
          <td style="padding:6px 12px;color:#94a3b8;white-space:nowrap">BT≥80<br><span style="font-size:0.72em;color:#64748b">各年1月1日時点</span></td>
          {row_80}
        </tr>
        <tr style="border-top:1px solid #1e293b">
          <td style="padding:6px 12px;color:#fbbf24;white-space:nowrap">BT≥90<br><span style="font-size:0.72em;color:#64748b">各年1月1日時点</span></td>
          {row_90}
        </tr>
      </tbody>
    </table>
  </div>
</div>
"""


# ────────────────────────────────────────────────────────────
# HTML 結合
# ────────────────────────────────────────────────────────────
def _build_combined_html(periods: list[dict]) -> str:
    import nikkei_analysis as _na

    tab_btns  = []
    tab_panes = []

    for i, p in enumerate(periods):
        as_of    = p["as_of"]
        oos_days = (TODAY - as_of).days
        tid      = f"asof_{as_of}"
        active   = "active" if i == 0 else ""

        tab_btns.append(
            f'<button class="ho-outer-btn {active}" onclick="switchHoTab(\'{tid}\')">'
            f'{as_of.year}年起点'
            f'<span class="subtitle" style="display:block;font-size:0.75em;opacity:0.8">'
            f'OOS {oos_days}日 ({oos_days//365}年{(oos_days%365)//30}ヶ月)'
            f'</span></button>'
        )

        # 選定概要テーブル
        rows_html = "".join(
            f'<tr>'
            f'<td style="color:{c["color"]};font-weight:bold;padding:4px 10px">{lbl}</td>'
            f'<td style="padding:4px 10px">{len(c["stop_wl"])}</td>'
            f'<td style="padding:4px 10px">{len(c["brk_wl"])}</td>'
            f'<td style="padding:4px 10px;color:#94a3b8;font-size:0.85em">'
            f'{as_of - timedelta(days=ho_d)} 〜 {as_of - timedelta(days=0)}</td>'
            f'</tr>'
            for (ho_d, lbl, color), c in zip(
                HOLDOUT_CONFIGS, [p["ho_results"][lbl] for _, lbl, _ in HOLDOUT_CONFIGS]
            )
        )
        summary_html = (
            f'<p class="subtitle" style="margin-bottom:12px">'
            f'OOS 評価期間: {as_of} 〜 {TODAY}（{oos_days}日）</p>'
            f'<table style="border-collapse:collapse;margin-bottom:20px;font-size:0.85em">'
            f'<tr style="border-bottom:1px solid #334155">'
            f'<th style="padding:4px 10px;text-align:left;color:#94a3b8">HO設定</th>'
            f'<th style="padding:4px 10px;color:#94a3b8">Stop銘柄数</th>'
            f'<th style="padding:4px 10px;color:#94a3b8">Breakout銘柄数</th>'
            f'<th style="padding:4px 10px;color:#94a3b8;text-align:left">WF選定データ期間</th>'
            f'</tr>'
            f'{rows_html}'
            f'</table>'
        )

        inner = p.get("oos_html", "<p class='subtitle'>評価結果なし（選定銘柄 0 件）</p>")
        yearly_inline = _yearly_inline_html(p)
        pane_display = "block" if i == 0 else "none"
        tab_panes.append(
            f'<div id="ho-{tid}" class="ho-outer-pane" style="display:{pane_display}">'
            f'<h2>{as_of.year}年1月1日 起点 — 歴史 WF 選定 × OOS 損益</h2>'
            f'{summary_html}'
            f'{yearly_inline}'
            f'{inner}'
            f'</div>'
        )

    # 年次成績推移タブ
    tab_btns.append(
        '<button class="ho-outer-btn" onclick="switchHoTab(\'yearly\')">'
        '年次成績推移'
        '<span class="subtitle" style="display:block;font-size:0.75em;opacity:0.8">起点年別 年次P&L</span>'
        '</button>'
    )
    tab_panes.append(
        f'<div id="ho-yearly" class="ho-outer-pane" style="display:none">'
        f'{_yearly_analysis_html(periods)}'
        f'</div>'
    )

    # 現行 run_signals_holdout_all.py と同じ追加 CSS
    extra_css = """
.ho-outer-nav {
  display:flex; flex-wrap:wrap; gap:6px;
  margin-bottom:24px; border-bottom:2px solid #1e293b; padding-bottom:0;
}
.ho-outer-btn {
  padding:9px 22px; background:#1e293b; border:none; border-radius:6px 6px 0 0;
  color:#94a3b8; cursor:pointer; font-size:0.92rem; font-family:inherit;
  border-bottom:2px solid transparent; margin-bottom:-2px;
}
.ho-outer-btn:hover:not(.active) { background:#263349; color:#e2e8f0; }
.ho-outer-btn.active { background:#0f172a; color:#60a5fa;
  border-bottom:2px solid #60a5fa; font-weight:700; }
.ho-outer-pane { display:none; padding:12px 0; }
.ho-outer-pane.active { display:block; }
.ho-period-btn {
  background:#1e293b; border:1px solid #334155; color:#94a3b8;
  padding:5px 14px; border-radius:4px; cursor:pointer;
  font-size:0.82rem; margin-right:4px; transition:all .2s;
}
.ho-period-btn:hover { color:#e2e8f0; border-color:#64748b; }
.ho-period-btn.active { background:#3b82f6; color:#fff;
  border-color:#3b82f6; font-weight:700; }
"""

    extra_js = """
function switchHoTab(tab) {
  document.querySelectorAll('.ho-outer-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.ho-outer-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('ho-' + tab).style.display = 'block';
  (event.target.closest('.ho-outer-btn') || event.target).classList.add('active');
}
function switchHoPeriod(days) {
  document.querySelectorAll('.ho-period-pane').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.ho-period-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('hd' + days).style.display = 'block';
  (event.target.closest('.ho-period-btn') || event.target).classList.add('active');
}
function switchYTab(name, btn) {
  ['all','bt70'].forEach(function(n) {
    var el = document.getElementById('ytab-' + n);
    if (el) el.style.display = (n === name) ? 'block' : 'none';
  });
  document.querySelectorAll('.ytab-btn').forEach(function(b) { b.classList.remove('active'); });
  btn.classList.add('active');
}
"""

    n_configs = sum(
        len(c["stop_wl"]) + len(c["brk_wl"])
        for p in periods for c in p["ho_results"].values()
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>歴史WF検証レポート {TODAY}</title>
<style>
{_na.CSS}
{extra_css}
</style>
</head>
<body>
<h1>歴史 WF 銘柄選定 × OOS 検証レポート</h1>
<p class="subtitle">
  基準日: {TODAY} &nbsp;|&nbsp;
  各年1月1日を起点に HO30d〜HO180d × 6設定で WF 銘柄選定 → 起点〜今日の OOS 損益を検証 &nbsp;|&nbsp;
  {len(periods)} 起点 × 6 HO設定 = {len(periods)*6} スキャン &nbsp;|&nbsp;
  銘柄延べ {n_configs} 件
</p>
<div class="ho-outer-nav">
{"".join(tab_btns)}
</div>
{"".join(tab_panes)}
<script>
{extra_js}
</script>
</body>
</html>"""


# ────────────────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="歴史 WF 銘柄選定 × OOS 検証レポートを生成")
    ap.add_argument("--start-year",   type=int,   default=2021)
    ap.add_argument("--end-year",     type=int,   default=None)
    ap.add_argument("--workers",      type=int,   default=_DEFAULT_WORKERS)
    ap.add_argument("--max-price",    type=float, default=0.0)
    ap.add_argument("--min-price",    type=float, default=0.0)
    ap.add_argument("--budget",       type=float, default=0.0,
                    help="予算（円）。100株買える銘柄のみ")
    ap.add_argument("--per-strategy", type=int,   default=PER_STRATEGY)
    ap.add_argument("--symbols",      type=str,   default=None)
    ap.add_argument("--limit",        type=int,   default=0,
                    help="デバッグ用: 先頭N銘柄のみ")
    ap.add_argument("--force",        action="store_true", help="強制再スキャン")
    ap.add_argument("--scan-only",    action="store_true", help="スキャンのみ（HTML不要）")
    ap.add_argument("--no-browser",   action="store_true")
    ap.add_argument("--aggressive",   action="store_true")
    args = ap.parse_args()

    max_price = args.max_price or (args.budget / 100.0 if args.budget else 0.0)
    min_price = args.min_price

    asof_dates = _gen_asof_dates(args.start_year, args.end_year)
    if not asof_dates:
        print("[ERROR] 有効な as-of 日付がありません。")
        sys.exit(1)

    symbols, src = load_universe(args.symbols)
    if args.limit > 0:
        symbols = symbols[:args.limit]

    total_scans = len(asof_dates) * len(HOLDOUT_CONFIGS)
    print(f"歴史 WF 検証: {len(asof_dates)} as-of × {len(HOLDOUT_CONFIGS)} HO設定 = {total_scans} スキャン")
    print(f"as-of 日付 : {[str(d) for d in asof_dates]}")
    print(f"HO 設定    : {[lbl for _, lbl, _ in HOLDOUT_CONFIGS]}")
    print(f"ユニバース  : {len(symbols)} 銘柄 ({src})")
    print(f"OOS 評価   : 各 as-of 〜 今日 ({TODAY})")
    if max_price:
        print(f"価格フィルター: {min_price}〜{max_price}円")
    print()

    # nikkei_analysis を事前インポートして WATCHLIST 上書きを防ぐ
    import check_signals_stop     as _stop
    import check_signals_breakout as _brk
    _orig_stop = list(_stop.WATCHLIST)
    _orig_brk  = list(_brk.WATCHLIST)
    import nikkei_analysis as _na
    _stop.WATCHLIST[:] = _orig_stop
    _brk.WATCHLIST[:]  = _orig_brk
    _na._SIGNALS_AVAILABLE = True

    periods: list[dict] = []

    for as_of in asof_dates:
        oos_days = (TODAY - as_of).days
        print(f"{'='*60}")
        print(f"▼ {as_of} 起点（OOS {oos_days}日 ≒ {oos_days//365}年{(oos_days%365)//30}ヶ月）")
        print(f"{'='*60}")

        ho_results: dict[str, dict] = {}   # ho_label → {stop_wl, brk_wl, color}
        ho_pnl_configs: list[dict]  = []   # _PNL_CONFIGS に注入する設定リスト

        for ho_days, ho_label, color in HOLDOUT_CONFIGS:
            # ── スキャン ───────────────────────────────────────────
            results = _scan_one_ho(
                as_of, ho_days, ho_label,
                symbols, args.workers, max_price, min_price, args.force,
            )

            # ── WATCHLIST 構築 ─────────────────────────────────────
            stop_wl, brk_wl = _build_watchlist(
                results, args.per_strategy, min_price, max_price
            )
            print(f"    [{ho_label}] Stop {len(stop_wl)}銘柄 / Breakout {len(brk_wl)}銘柄")

            ho_results[ho_label] = {
                "stop_wl": stop_wl,
                "brk_wl":  brk_wl,
                "color":   color,
            }
            ho_pnl_configs.append({
                "label":   f"{as_of}_{ho_label}",
                "color":   color,
                "mode":    "conservative",
                "sm_tm":   None,
                "stop_wl": stop_wl,
                "brk_wl":  brk_wl,
            })

        period: dict = {
            "as_of":           as_of,
            "ho_results":      ho_results,
            "oos_html":        "",
            "yearly_pnl_all":  {},
            "yearly_pnl_70":   {},
            "yearly_pnl_80":   {},
            "yearly_pnl_90":   {},
        }

        # ── OOS 評価（6 HO 設定を一括）─────────────────────────────
        has_any = any(
            c["stop_wl"] or c["brk_wl"] for c in ho_results.values()
        )
        if not args.scan_only and has_any:
            print(f"  OOS 損益評価中（{oos_days}日間 / 6 HO設定）...", flush=True)
            period["oos_html"] = _eval_oos_html(as_of, ho_pnl_configs, args.workers)
        elif not has_any:
            print("  ⚠ 全 HO 設定で選定銘柄 0 件のため OOS 評価をスキップ")

        # ── 年次 P&L 計算（全OOS期間・シグナル日dedup）──
        has_any_wl = any(c["stop_wl"] or c["brk_wl"] for c in ho_results.values())
        if not args.scan_only and has_any_wl:
            n_items = len(unique_pairs) if 'unique_pairs' in dir() else "?"
            n_uniq = len({
                (sym, nm, strat)
                for cfg in ho_pnl_configs
                for sym, nm, strat in list(cfg.get("stop_wl", [])) + list(cfg.get("brk_wl", []))
            })
            print(
                f"  年次成績計算中（全OOS期間 {oos_days}日 / ユニーク{n_uniq}銘柄戦略・シグナル日dedup）...",
                flush=True,
            )
            try:
                multi = _compute_yearly_pnl_multi(
                    ho_pnl_configs, as_of, args.workers,
                    thresholds=[0, 70, 80, 90],
                )
                period["yearly_pnl_all"] = multi[0]
                period["yearly_pnl_70"]  = multi[70]
                period["yearly_pnl_80"]  = multi[80]
                period["yearly_pnl_90"]  = multi[90]
            except Exception as e:
                print(f"  [WARN] 年次 P&L 計算失敗: {e}")

        periods.append(period)
        print()

    if args.scan_only:
        print("--scan-only のため HTML 生成をスキップしました。")
        return

    print("HTML 生成中...")
    mode_suffix = "_aggressive" if os.getenv("TRADING_MODE") == "aggressive" else ""
    out_path    = f"historical_wf_validation{mode_suffix}_{TODAY}.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_build_combined_html(periods))

    print(f"\n出力: {out_path}")
    if not args.no_browser:
        open_html(out_path)


if __name__ == "__main__":
    main()
