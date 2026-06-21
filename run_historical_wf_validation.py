#!/usr/bin/env python3
"""
run_historical_wf_validation.py — 歴史WF銘柄選定 × OOS検証レポート
=====================================================================

指定年（デフォルト2021〜今年直前）の各1月1日を起点として：
  1. その日から HO180d 前までのデータで現行 FOLDS 構造（2fold）と同一の WF 銘柄選定
  2. 起点〜今日まで（OOS期間）のバックテストで損益を検証
  3. 期間別タブ付き HTML を生成（run_signals_holdout_all.py 形式に準拠）

【目的】
  銘柄選定パイプライン自体の有効性を複数の歴史的起点で検証する。
  「選定時に知らないデータで本当に機能したか」をメタ WalkForward で確認。

【使い方】
  python run_historical_wf_validation.py
  python run_historical_wf_validation.py --start-year 2022
  python run_historical_wf_validation.py --max-price 6000 --min-price 1000
  python run_historical_wf_validation.py --workers 8
  python run_historical_wf_validation.py --force          # 強制再スキャン
  python run_historical_wf_validation.py --scan-only      # スキャンのみ（HTML不要）
  python run_historical_wf_validation.py --no-browser

【キャッシュ設計】
  walkforward_results/asof_YYYY-MM-DD/walkforward_STRATEGY_YYYY-MM-DD.csv
  CSV が存在すればスキャンをスキップ（--force で強制再スキャン）。

【フォールド構造（現行と同一）】
  HO_cutoff = as_of - 180d
  fold1: TRAIN [cutoff-730d, cutoff-370d] / TEST [cutoff-370d, cutoff-180d]  (12M/6M)
  fold2: TRAIN [cutoff-550d, cutoff-180d] / TEST [cutoff-180d, cutoff]       (12M/6M)
  ↑ scan_walkforward.FOLDS と同じ構造、as_of 基準にシフトしただけ
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# TRADING_MODE を import 前に設定
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

import pandas as pd

# ── バックテストエンジンと scan_walkforward の再利用 ──────────────────────────
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
    TRAIN_MIN_TRADES,
    TEST_MIN_TRADES,
)

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

HOLDOUT_DAYS  = 180  # as_of の何日前まで WF 選定に使うか（現行 HO180d と同じ）
PER_STRATEGY  = 10   # WATCHLIST: 1 戦略あたり最大選定銘柄数
MIN_FOLDS     = 2    # folds_passed の最低値

STOP_STRATS     = {"MACD", "A7", "RSI2"}
BREAKOUT_STRATS = {"DON", "VOL", "MOM"}
ALL_STRATS      = list(STOP_STRATS) + list(BREAKOUT_STRATS)

RESULTS_DIR = Path("walkforward_results")

_CSV_FIELDS = [
    "symbol", "name", "strategy", "family",
    "latest_price", "folds_passed",
    "total_test_trades", "avg_hold_days",
    "total_test_pnl", "avg_test_pf", "avg_test_wr",
    "max_drawdown_pct", "max_consecutive_losses", "sharpe",
    "recovery_factor", "train_to_test_degradation_pct",
]

# ── 設定カラー（as_of 年別）─────────────────────────────────────────
_YEAR_COLORS = {
    2021: "#1565c0",
    2022: "#2e7d32",
    2023: "#f57f17",
    2024: "#6a1b9a",
    2025: "#c62828",
    2026: "#00695c",
}


# ────────────────────────────────────────────────────────────
# as-of 日付の自動生成
# ────────────────────────────────────────────────────────────
def _gen_asof_dates(start_year: int = 2021, end_year: int | None = None) -> list[date]:
    """start_year 〜 (end_year-1) の各 1/1 を返す（今年は評価期間が短いため除外）"""
    if end_year is None:
        end_year = TODAY.year
    dates = [date(y, 1, 1) for y in range(start_year, end_year)]
    # 少なくとも 180 日以上前の as_of のみ有効（OOS期間確保）
    return [d for d in dates if (TODAY - d).days >= 180]


# ────────────────────────────────────────────────────────────
# フォールド絶対日付の生成（現行 FOLDS をシフト）
# ────────────────────────────────────────────────────────────
def _make_folds_abs(as_of: date, holdout_days: int = HOLDOUT_DAYS) -> list[tuple]:
    """現行 FOLDS 構造を as_of 基準に変換した絶対日付フォールドを返す。

    Returns list of (name, train_start, train_end, test_start, test_end)
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
def _scan_symbol_asof(
    symbol: str, name: str, strategy_name: str,
    folds_abs: list[tuple],
    as_of: date,
    max_price: float = 0.0,
    min_price: float = 0.0,
) -> dict | None:
    """as_of 基準の WF バックテスト（1 銘柄 × 1 戦略）"""
    if strategy_name not in STRATEGY_DEFS:
        return None
    calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strategy_name]

    # 最も古いフォールドの TRAIN 開始日より前から取得
    earliest = min(ts for _, ts, te, vs, ve in folds_abs)
    since = earliest - timedelta(days=90)
    fetch_days = (TODAY - since).days + 60

    df_full = fetch(symbol, fetch_days, min_start_date=since)
    if df_full is None or len(df_full) < 60:
        return None

    # as_of 以降を除外（未来データ漏洩防止）
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

    for fold_name, train_s, train_e, test_s, test_e in folds_abs:
        train_r = _run_window_abs(symbol, name, df_full, calc_fn, em, sm, tm,
                                  train_s, train_e, strategy_name,
                                  entry_type=entry_type)
        test_r  = _run_window_abs(symbol, name, df_full, calc_fn, em, sm, tm,
                                  test_s, test_e, strategy_name,
                                  entry_type=entry_type)

        if _passes_train(train_r) and _passes_test(test_r):
            folds_passed += 1
        if train_r:
            train_results.append(train_r)
        if test_r:
            test_results.append(test_r)

    if not test_results:
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

    avg_test_pf = (
        sum(_cap_pf(r.get("pf", 0)) for r in test_results) / max(len(test_results), 1)
    )
    avg_test_wr = (
        sum(r.get("win_rate", 0) for r in test_results) / max(len(test_results), 1)
    )

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
def _csv_path(as_of: date, strategy: str) -> Path:
    return RESULTS_DIR / f"asof_{as_of}" / f"walkforward_{strategy}_{as_of}.csv"


def _save_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ────────────────────────────────────────────────────────────
# as_of 全銘柄スキャン（キャッシュ付き）
# ────────────────────────────────────────────────────────────
def _scan_asof(
    as_of: date,
    symbols: list[tuple[str, str]],
    strategies: list[str],
    workers: int,
    max_price: float,
    min_price: float,
    force: bool,
) -> dict[str, list[dict]]:
    """as_of 基準のフルスキャン。CSV キャッシュがあればスキップ。"""
    folds_abs = _make_folds_abs(as_of)
    results: dict[str, list[dict]] = {}
    need_scan: list[str] = []

    for strat in strategies:
        path = _csv_path(as_of, strat)
        if not force and path.exists():
            rows = _load_csv(path)
            results[strat] = rows
            print(f"  [cache] {strat}: {len(rows)}件 ← {path.name}")
        else:
            need_scan.append(strat)
            results[strat] = []

    if not need_scan:
        return results

    print(f"  [scan] {len(need_scan)}戦略 × {len(symbols)}銘柄 をスキャン...")
    tasks = [(sym, nm, strat) for sym, nm in symbols for strat in need_scan]
    batch: dict[str, list[dict]] = {s: [] for s in need_scan}

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futs = {
            exe.submit(
                _scan_symbol_asof, sym, nm, strat, folds_abs, as_of, max_price, min_price
            ): (sym, nm, strat)
            for sym, nm, strat in tasks
        }
        done = 0
        total = len(futs)
        for fut in as_completed(futs):
            sym, nm, strat = futs[fut]
            done += 1
            try:
                r = fut.result()
                if r and r["folds_passed"] >= MIN_FOLDS:
                    batch[strat].append(r)
            except Exception:
                pass
            if done % 500 == 0 or done == total:
                total_ok = sum(len(v) for v in batch.values())
                print(f"    {done}/{total} 完了 (合格: {total_ok}件)")

    for strat in need_scan:
        rows = batch[strat]
        _save_csv(rows, _csv_path(as_of, strat))
        results[strat] = rows
        print(f"  [saved] {strat}: {len(rows)}件合格")

    return results


# ────────────────────────────────────────────────────────────
# スキャン結果から WATCHLIST を構築
# ────────────────────────────────────────────────────────────
def _float(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def _build_watchlist(
    results: dict[str, list[dict]],
    per_strategy: int,
    min_price: float,
    max_price: float,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """スキャン結果から stop / breakout の WATCHLIST ペアを返す"""
    stop_seen: dict[tuple[str, str], float] = {}
    brk_seen:  dict[tuple[str, str], float] = {}

    for strat, rows in results.items():
        if strat not in STOP_STRATS and strat not in BREAKOUT_STRATS:
            continue

        # フィルター（build_watchlist.py と同じ基準）
        filtered = []
        for r in rows:
            lp = _float(r.get("latest_price", 0))
            if min_price > 0 and lp < min_price:
                continue
            if max_price > 0 and lp > max_price:
                continue
            if _float(r.get("total_test_pnl", 0)) <= 0:
                continue
            if _float(r.get("max_drawdown_pct", 100)) > 15:
                continue
            filtered.append(r)

        # ランキング: composite_score = test_pnl × (1 + max(sharpe, 0))
        filtered.sort(
            key=lambda r: _float(r.get("total_test_pnl", 0)) * (1 + max(_float(r.get("sharpe", 0)), 0)),
            reverse=True,
        )
        for r in filtered[:per_strategy]:
            pair = (str(r["symbol"]), str(r.get("name", "")))
            score = _float(r.get("total_test_pnl", 0))
            if strat in STOP_STRATS:
                if pair not in stop_seen or score > stop_seen[pair]:
                    stop_seen[pair] = score
            else:
                if pair not in brk_seen or score > brk_seen[pair]:
                    brk_seen[pair] = score

    return sorted(stop_seen.keys()), sorted(brk_seen.keys())


# ────────────────────────────────────────────────────────────
# OOS 損益 HTML 生成
# ────────────────────────────────────────────────────────────
def _eval_oos_html(
    as_of: date,
    stop_wl: list[tuple[str, str]],
    brk_wl:  list[tuple[str, str]],
    workers: int,
    label: str,
    color: str,
) -> str:
    """as_of〜今日の OOS バックテスト結果 HTML を生成"""
    import nikkei_analysis as _na

    oos_days = (TODAY - as_of).days

    # _PNL_CONFIGS を一時的にこの as_of のウォッチリストで上書き
    orig_configs = list(_na._PNL_CONFIGS)
    cfg = {
        "label":   label,
        "color":   color,
        "mode":    "conservative",
        "sm_tm":   None,
        "stop_wl": stop_wl,
        "brk_wl":  brk_wl,
    }
    try:
        _na._PNL_CONFIGS[:] = [cfg]
        html = _na._tab5_pnl_html(oos_days, workers, skip_timing9=True)
    finally:
        _na._PNL_CONFIGS[:] = orig_configs

    return html


# ────────────────────────────────────────────────────────────
# HTML 結合
# ────────────────────────────────────────────────────────────
def _build_combined_html(periods: list[dict]) -> str:
    """全 as-of 期間を 1 つの HTML にまとめる（タブ切替）"""

    def _tab_id(p):
        return f"asof_{p['as_of']}"

    tab_btns  = []
    tab_panes = []

    for i, p in enumerate(periods):
        as_of    = p["as_of"]
        oos_days = (TODAY - as_of).days
        stop_cnt = len(p["stop_wl"])
        brk_cnt  = len(p["brk_wl"])
        tid      = _tab_id(p)
        color    = p.get("color", "#1565c0")
        active   = "active" if i == 0 else ""
        display  = "block" if i == 0 else "none"

        # WF 選定に使った期間の計算
        cutoff       = as_of - timedelta(days=HOLDOUT_DAYS)
        fold_start   = cutoff - timedelta(days=730)  # fold1 TRAIN 開始
        fold_end     = cutoff                         # fold2 TEST 終了

        tab_btns.append(
            f'<button id="btn_{tid}" class="hist-tab-btn {active}" '
            f'style="border-color:{color}" onclick="switchHistTab(\'{tid}\')">'
            f'<strong>{as_of.year}年選定</strong><br>'
            f'<small>OOS {oos_days}日 ({oos_days//365}年{(oos_days%365)//30}ヶ月)</small>'
            f'</button>'
        )

        wl_info = (
            f'<div class="wl-info">'
            f'  <b>銘柄選定期間:</b> {fold_start} 〜 {fold_end} (WF 2fold / HO{HOLDOUT_DAYS}d)<br>'
            f'  <b>OOS 評価期間:</b> {as_of} 〜 {TODAY} ({oos_days}日)<br>'
            f'  <b>選定銘柄数:</b> Stop {stop_cnt}銘柄 / Breakout {brk_cnt}銘柄'
            f'</div>'
        )

        inner = p.get("oos_html", "<p>評価結果なし（銘柄 0 件）</p>")
        tab_panes.append(
            f'<div id="pane_{tid}" class="hist-tab-pane" style="display:{display}">'
            f'{wl_info}'
            f'{inner}'
            f'</div>'
        )

    # JS: タブ切替に使う ID リスト
    all_ids = [_tab_id(p) for p in periods]
    pane_ids_js = "[" + ",".join(f"'pane_{tid}'" for tid in all_ids) + "]"
    btn_ids_js  = "[" + ",".join(f"'btn_{tid}'"  for tid in all_ids) + "]"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>歴史WF検証レポート {TODAY}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
       margin: 0; padding: 0; background: #f0f2f5; color: #222; }}
.page-header {{ background: linear-gradient(135deg,#1a237e,#283593);
               color: #fff; padding: 18px 24px; }}
.page-header h1 {{ margin: 0; font-size: 1.35em; letter-spacing: .02em; }}
.page-header p  {{ margin: 6px 0 0; font-size: .88em; opacity: .82; }}
.hist-tab-nav {{ display: flex; flex-wrap: wrap; gap: 6px;
                padding: 12px 16px; background: #e8eaf6;
                border-bottom: 2px solid #3949ab; }}
.hist-tab-btn {{ padding: 8px 16px; border: 2px solid #3949ab; border-radius: 8px;
                background: #fff; cursor: pointer; font-size: .85em; text-align: center;
                line-height: 1.4; transition: background .15s; }}
.hist-tab-btn.active {{ background: #3949ab; color: #fff; }}
.hist-tab-btn:hover:not(.active) {{ background: #e8eaf6; }}
.hist-tab-pane {{ padding: 0; }}
.wl-info {{ background: #fff; border: 1px solid #c5cae9; border-radius: 6px;
            margin: 12px 16px 0; padding: 12px 16px; font-size: .9em; line-height: 1.7; }}
</style>
</head>
<body>
<div class="page-header">
  <h1>📊 歴史 WF 銘柄選定 × OOS 検証レポート</h1>
  <p>生成日: {TODAY} ／ 各年1月1日を起点に現行 WF 構造（2fold / HO{HOLDOUT_DAYS}d）で銘柄選定 → 起点〜今日の OOS 損益を検証</p>
</div>
<div class="hist-tab-nav">
{"".join(tab_btns)}
</div>
{"".join(tab_panes)}
<script>
(function() {{
  var panes = {pane_ids_js};
  var btns  = {btn_ids_js};
  window.switchHistTab = function(tabId) {{
    panes.forEach(function(id) {{ document.getElementById(id).style.display = 'none'; }});
    btns.forEach(function(id)  {{ document.getElementById(id).classList.remove('active'); }});
    document.getElementById('pane_' + tabId).style.display = 'block';
    document.getElementById('btn_'  + tabId).classList.add('active');
  }};
}})();
</script>
</body>
</html>"""


# ────────────────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="歴史 WF 銘柄選定 × OOS 検証レポートを生成する")
    ap.add_argument("--start-year",   type=int, default=2021,
                    help="検証開始年（デフォルト: 2021）")
    ap.add_argument("--end-year",     type=int, default=None,
                    help="検証終了年（除く）。デフォルト: 今年")
    ap.add_argument("--workers",      type=int, default=_DEFAULT_WORKERS)
    ap.add_argument("--max-price",    type=float, default=0.0,
                    help="最新終値の上限（円）")
    ap.add_argument("--min-price",    type=float, default=0.0,
                    help="最新終値の下限（円）")
    ap.add_argument("--budget",       type=float, default=0.0,
                    help="予算（円）。100株買える銘柄のみ（= --max-price 予算/100）")
    ap.add_argument("--per-strategy", type=int,   default=PER_STRATEGY,
                    help=f"1 戦略あたり最大選定銘柄数（デフォルト: {PER_STRATEGY}）")
    ap.add_argument("--symbols",      type=str,   default=None,
                    help="ユニバースファイル（省略時: symbols_listed_prime.py 等を自動検出）")
    ap.add_argument("--limit",        type=int,   default=0,
                    help="銘柄数上限（デバッグ用）")
    ap.add_argument("--force",        action="store_true",
                    help="キャッシュを無視して強制再スキャン")
    ap.add_argument("--scan-only",    action="store_true",
                    help="スキャンのみ実行（OOS 評価 / HTML 生成をスキップ）")
    ap.add_argument("--no-browser",   action="store_true",
                    help="HTML 生成後にブラウザを開かない")
    ap.add_argument("--aggressive",   action="store_true",
                    help="積極利確モード（TRADING_MODE=aggressive）")
    args = ap.parse_args()

    # budget → max_price 換算
    max_price = args.max_price
    if args.budget > 0 and max_price == 0:
        max_price = args.budget / 100.0
    min_price = args.min_price

    # as-of 日付生成
    asof_dates = _gen_asof_dates(args.start_year, args.end_year)
    if not asof_dates:
        print("[ERROR] 有効な as-of 日付がありません。--start-year を確認してください。")
        sys.exit(1)

    # ユニバース読み込み
    symbols, src = load_universe(args.symbols)
    if args.limit > 0:
        symbols = symbols[:args.limit]

    print(f"歴史 WF 検証: {len(asof_dates)} 期間 × {len(ALL_STRATS)} 戦略")
    print(f"as-of 日付: {[str(d) for d in asof_dates]}")
    print(f"ユニバース: {len(symbols)} 銘柄 ({src})")
    print(f"OOS 評価: 各 as-of 〜 今日 ({TODAY})")
    if max_price:
        print(f"価格フィルター: {min_price}〜{max_price}円")
    print()

    # nikkei_analysis を事前にインポート（reload による WATCHLIST 上書きを防ぐ）
    import check_signals_stop     as _stop
    import check_signals_breakout as _brk
    _orig_stop_wl = list(_stop.WATCHLIST)
    _orig_brk_wl  = list(_brk.WATCHLIST)
    import nikkei_analysis as _na
    _stop.WATCHLIST[:] = _orig_stop_wl
    _brk.WATCHLIST[:]  = _orig_brk_wl
    _na._SIGNALS_AVAILABLE = True

    periods: list[dict] = []
    for i, as_of in enumerate(asof_dates):
        oos_days = (TODAY - as_of).days
        color    = _YEAR_COLORS.get(as_of.year, "#455a64")
        label    = f"asof_{as_of}"

        print(f"{'='*60}")
        print(f"▼ {as_of} 起点（OOS {oos_days}日 ≒ {oos_days//365}年{(oos_days%365)//30}ヶ月）")
        print(f"{'='*60}")

        # ── Step 1: スキャン ────────────────────────────────────────
        results = _scan_asof(
            as_of, symbols, ALL_STRATS, args.workers, max_price, min_price, args.force
        )

        # ── Step 2: WATCHLIST 構築 ──────────────────────────────────
        stop_wl, brk_wl = _build_watchlist(
            results, args.per_strategy, min_price, max_price
        )
        print(f"  選定: Stop {len(stop_wl)}銘柄 / Breakout {len(brk_wl)}銘柄")

        period: dict = {
            "as_of":   as_of,
            "stop_wl": stop_wl,
            "brk_wl":  brk_wl,
            "color":   color,
            "oos_html": "",
        }

        # ── Step 3: OOS 損益評価 ────────────────────────────────────
        if not args.scan_only and (stop_wl or brk_wl):
            print(f"  OOS 損益評価中（{oos_days}日間）...", flush=True)
            period["oos_html"] = _eval_oos_html(
                as_of, stop_wl, brk_wl, args.workers, label, color
            )
        elif not stop_wl and not brk_wl:
            print("  ⚠ 選定銘柄 0 件のため OOS 評価をスキップ")

        periods.append(period)
        print()

    if args.scan_only:
        print("--scan-only のため HTML 生成をスキップしました。")
        return

    # ── HTML 生成 ────────────────────────────────────────────────
    print("HTML 生成中...")
    mode_suffix = "_aggressive" if os.getenv("TRADING_MODE") == "aggressive" else ""
    out_path    = f"historical_wf_validation{mode_suffix}_{TODAY}.html"
    html        = _build_combined_html(periods)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n出力: {out_path}")
    if not args.no_browser:
        webbrowser.open(str(Path(out_path).resolve()))


if __name__ == "__main__":
    main()
