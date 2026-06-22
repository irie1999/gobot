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
        pane_display = "block" if i == 0 else "none"
        tab_panes.append(
            f'<div id="ho-{tid}" class="ho-outer-pane" style="display:{pane_display}">'
            f'<h2>{as_of.year}年1月1日 起点 — 歴史 WF 選定 × OOS 損益</h2>'
            f'{summary_html}'
            f'{inner}'
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
            "as_of":      as_of,
            "ho_results": ho_results,
            "oos_html":   "",
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
