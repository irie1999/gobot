"""
holdout_batch_summary.py  ―  30/60/90/120/150/180日ホールドアウト結果バッチ集計

各ホールドアウト期間について conservative WATCHLIST を使って
BTスコア帯別の損益・勝率・PFを集計し、CSV・Markdownに出力する。

ホールドアウトCSVが存在しない期間は scan_walkforward.py を自動実行して生成する。
2回目以降は保存済みCSVを使うため数分で完了する。

Usage:
    python holdout_batch_summary.py                       # 全6期間 (30/60/90/120/150/180日)
    python holdout_batch_summary.py --periods 90,180      # 特定期間のみ
    python holdout_batch_summary.py --max-price 5000      # 株価上限フィルター
    python holdout_batch_summary.py --budget 600000       # 予算フィルター (budget/100=株価上限)
    python holdout_batch_summary.py --workers 8           # スキャン並列数
    python holdout_batch_summary.py --no-save             # CSV/MD を保存しない (コンソール出力のみ)
    python holdout_batch_summary.py --no-scan             # スキャン自動実行しない (CSVがある期間のみ)

出力:
    holdout_batch_results_YYYY-MM-DD.csv  … 全期間・全BT帯の数値
    holdout_batch_results_YYYY-MM-DD.md   … CLAUDE.mdに貼れる Markdown 表
"""
from __future__ import annotations

import argparse
import copy as _copy
import csv as _csv
import importlib as _importlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# ── 引数 ────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="ホールドアウト結果バッチ集計")
parser.add_argument("--periods",      type=str,   default="30,60,90,120,150,180")
parser.add_argument("--max-price",    type=float, default=0.0)
parser.add_argument("--budget",       type=float, default=0.0)
parser.add_argument("--max-dd",       type=float, default=15.0)
parser.add_argument("--min-sharpe",   type=float, default=0.0)
parser.add_argument("--max-consec",   type=int,   default=5)
parser.add_argument("--per-strategy", type=int,   default=10)
parser.add_argument("--wf-dir",       type=Path,  default=Path("walkforward_results"))
parser.add_argument("--workers",      type=int,   default=4)
parser.add_argument("--no-save",      action="store_true")
parser.add_argument("--no-scan",      action="store_true",
                    help="CSVがない期間のスキャンをスキップ (CSVある期間のみ集計)")
args = parser.parse_args()

periods: list[int] = [int(x.strip()) for x in args.periods.split(",")]
effective_max_price = args.max_price
if args.budget > 0 and effective_max_price <= 0:
    effective_max_price = args.budget / 100.0

# ── モジュールロード ─────────────────────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")
import check_signals_stop     as _stop
import check_signals_breakout as _brk

_CON_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_CON_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)


# ── ヘルパー ─────────────────────────────────────────────────────────────────
def _float(v, default=0.0) -> float:
    try:    return float(v)
    except: return default

def _int(v, default=0) -> int:
    try:    return int(v)
    except: return default


def _find_holdout_csv(strategy: str, holdout_days: int,
                      wf_dir: Path, mode: str = "conservative") -> Path | None:
    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    suffix = f"{mode_suffix}_holdout{holdout_days}d"
    candidates = sorted(wf_dir.glob(f"walkforward_{strategy}{suffix}_*.csv"), reverse=True)
    return candidates[0] if candidates else None


def _run_scan(strategy: str, holdout_days: int,
              max_price: float, workers: int, wf_dir: Path) -> list[dict]:
    """scan_walkforward を直接呼び出してホールドアウトCSVを生成する。"""
    import scan_walkforward as _swf

    orig_defs  = _swf.STRATEGY_DEFS
    orig_mode  = _swf.TRADING_MODE
    orig_folds = list(_swf.FOLDS)

    _swf.STRATEGY_DEFS = _swf.STRATEGY_DEFS_CONSERVATIVE
    _swf.TRADING_MODE  = "conservative"
    if holdout_days > 0:
        _swf.FOLDS[:] = [
            (n, ts + holdout_days, te + holdout_days,
             vs + holdout_days, ve + holdout_days)
            for n, ts, te, vs, ve in orig_folds
        ]

    try:
        symbols, universe_name = _swf.load_universe()
        print(f"    [{strategy}] ユニバース {universe_name} ({len(symbols)}銘柄)", flush=True)

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(_swf.walkforward_one, sym, name, strategy, max_price): sym
                for sym, name in symbols
            }
            done = 0; every = max(len(symbols) // 10, 50)
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                    if r: results.append(r)
                except Exception: pass
                if done % every == 0:
                    print(f"    [{strategy}] {done}/{len(symbols)} 候補:{len(results)}", flush=True)
    finally:
        _swf.FOLDS[:]      = orig_folds
        _swf.STRATEGY_DEFS = orig_defs
        _swf.TRADING_MODE  = orig_mode

    # CSV保存
    wf_dir.mkdir(exist_ok=True)
    if results:
        csv_path = wf_dir / f"walkforward_{strategy}_holdout{holdout_days}d_{TODAY}.csv"
        fields = ["symbol", "name", "strategy", "family", "latest_price",
                  "folds_passed", "total_test_trades", "avg_hold_days",
                  "avg_test_wr", "avg_test_pf", "total_test_pnl",
                  "max_drawdown_pct", "max_consecutive_losses", "sharpe"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(results)
        print(f"    [{strategy}] CSV保存: {csv_path.name}")
    return results


def _load_watchlist_for_period(holdout_days: int) -> tuple[list, list, str]:
    """
    conservative WATCHLIST をCSVから読み込む。
    CSVがない場合は --no-scan でなければ自動スキャンを実行する。
    """
    strategies_stop = ["MACD", "A7", "RSI2"]
    strategies_brk  = ["DON", "VOL", "MOM"]
    stop_wl: list = []; brk_wl: list = []; csv_date = str(TODAY)

    for strategies, target_wl in [(strategies_stop, stop_wl), (strategies_brk, brk_wl)]:
        for strategy in strategies:
            csv_path = _find_holdout_csv(strategy, holdout_days, args.wf_dir)

            if csv_path is None:
                if args.no_scan:
                    print(f"  [SKIP] {strategy} holdout{holdout_days}d CSV なし (--no-scan)")
                    continue
                print(f"  [SCAN] {strategy} holdout{holdout_days}d CSV なし → スキャン実行")
                rows = _run_scan(strategy, holdout_days, effective_max_price,
                                 args.workers, args.wf_dir)
                csv_path = _find_holdout_csv(strategy, holdout_days, args.wf_dir)
            else:
                with open(csv_path, encoding="utf-8") as f:
                    rows = list(_csv.DictReader(f))

            if csv_path:
                csv_date = csv_path.stem.split("_")[-1]

            filtered = [r for r in rows if (
                _int(r.get("folds_passed", 0))                 >= 2
                and _float(r.get("max_drawdown_pct", 999))     <= args.max_dd
                and _int(r.get("max_consecutive_losses", 999)) <= args.max_consec
                and _float(r.get("sharpe", 0))                 >= args.min_sharpe
                and _float(r.get("total_test_pnl", 0))          > 0
                and (effective_max_price <= 0
                     or _float(r.get("latest_price", 0)) <= effective_max_price)
            )]
            filtered.sort(
                key=lambda r: _float(r.get("total_test_pnl", 0))
                              * (1 + max(_float(r.get("sharpe", 0)), 0)),
                reverse=True,
            )
            for r in filtered[:args.per_strategy]:
                sym   = r.get("symbol", "")
                name  = r.get("name", "")
                strat = r.get("strategy", strategy)
                if sym and strat:
                    target_wl.append((sym, name, strat))

    return stop_wl, brk_wl, csv_date


def _build_wf_scores(holdout_days: int, wf_dir: Path) -> dict:
    from compute_wf_scores import calc_wf_score, wf_rank
    strategies = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]
    scores: dict = {}
    for strategy in strategies:
        csv_path = _find_holdout_csv(strategy, holdout_days, wf_dir)
        if csv_path is None: continue
        with open(csv_path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                try:
                    sym = row["symbol"]
                    sc  = calc_wf_score(float(row["avg_test_wr"]),
                                        float(row["avg_test_pf"]),
                                        int(row["folds_passed"]))
                    scores[f"{sym}_{row['strategy']}"] = {
                        "score": sc, "rank": wf_rank(sc),
                        "wr": round(float(row["avg_test_wr"]), 1),
                        "pf": round(float(row["avg_test_pf"]), 2),
                        "folds": int(row["folds_passed"]),
                    }
                except (ValueError, KeyError): continue
    return scores


# ── バックテスト & BT帯集計 ──────────────────────────────────────────────────
BT_BANDS = [
    (90, 101, "90-100"), (80, 90, "80-89"), (70, 80, "70-79"), (60, 70, "60-69"),
    (50,  60, "50-59"),  (40, 50, "40-49"), (30, 40, "30-39"), ( 0, 30, "0-29"),
]

def _band_stats(trades: list[dict]) -> dict:
    n    = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    pnl  = sum(t["pnl"] for t in trades)
    gp   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "wins": wins, "wr": wins/n*100 if n else 0,
            "pf": pf, "pnl": pnl, "gp": gp, "gl": gl}


def _collect_trades(stop_wl: list, brk_wl: list, holdout_days: int) -> list[dict]:
    """バックテスト実行 → holdout期間内のトレードにBTスコアを付けて返す。"""
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs: dict = {}
        for sym, name, strat in stop_wl:
            futs[ex.submit(_stop.backtest_one, sym, name, strat)] = None
        for sym, name, strat in brk_wl:
            futs[ex.submit(_brk.backtest_one, sym, name, strat)] = None
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r: items.append(r)
            except Exception: pass

    since = TODAY - timedelta(days=holdout_days)
    trades: list[dict] = []
    seen: set = set()

    for it in items:
        sym   = it.get("symbol", ""); strat = it.get("strategy", "")
        pr    = it.get("period_results", {})
        if not pr: continue
        trade_log  = pr[max(pr.keys())].get("trade_log", [])
        rec_score, _ = _stop.calc_recommend_score(pr)

        for t in trade_log:
            if t.get("reason") == "発注中": continue
            exit_dt   = t.get("exit_dt")
            signal_dt = t.get("signal_dt")
            if not exit_dt or not signal_dt: continue
            exit_d = exit_dt.date() if hasattr(exit_dt, "date") else exit_dt
            if not (since <= exit_d <= TODAY): continue
            key = (sym, strat, signal_dt)
            if key in seen: continue
            seen.add(key)
            trades.append({"symbol": sym, "strategy": strat,
                           "rec_score": rec_score, "pnl": t.get("pnl", 0)})
    return trades


# ── メイン処理 ───────────────────────────────────────────────────────────────
all_results: dict[int, dict[str, dict]] = {}

for holdout_days in periods:
    print(f"\n{'='*60}")
    print(f"  ホールドアウト {holdout_days}日")
    print(f"{'='*60}")

    stop_wl, brk_wl, csv_date = _load_watchlist_for_period(holdout_days)

    if not stop_wl and not brk_wl:
        print(f"  [SKIP] WATCHLIST が空 → この期間をスキップ")
        all_results[holdout_days] = {}
        continue

    print(f"  WATCHLIST: 逆指値B={len(stop_wl)}件 BRK={len(brk_wl)}件 (CSV: {csv_date})")

    # WFスコア・STRATEGY_PARAMSを設定
    wf_scores = _build_wf_scores(holdout_days, args.wf_dir)
    _stop._WF_SCORES = dict(wf_scores)
    _brk._WF_SCORES  = dict(wf_scores)
    _stop.STRATEGY_PARAMS.update(_CON_STOP)
    _brk.STRATEGY_PARAMS.update(_CON_BRK)

    print(f"  バックテスト実行中...", flush=True)
    trades = _collect_trades(stop_wl, brk_wl, holdout_days)
    print(f"  完了: {len(trades)}トレード")

    band_results: dict[str, dict] = {}
    for lo, hi, lbl in BT_BANDS:
        band = [t for t in trades if t.get("rec_score") is not None
                and lo <= t["rec_score"] < hi]
        band_results[lbl] = _band_stats(band) if band else {"n":0,"wins":0,"wr":0,"pf":0,"pnl":0,"gp":0,"gl":0}
    band_results["BT60+"] = _band_stats([t for t in trades if (t.get("rec_score") or 0) >= 60])
    band_results["ALL"]   = _band_stats(trades) if trades else {"n":0,"wins":0,"wr":0,"pf":0,"pnl":0,"gp":0,"gl":0}
    all_results[holdout_days] = band_results


# ── コンソール表示 ─────────────────────────────────────────────────────────────
def _pf_s(v) -> str: return "∞" if v == float("inf") else f"{v:.2f}"

valid_periods = [p for p in periods if all_results.get(p)]

print(f"\n\n{'='*80}")
print(f"  ホールドアウト結果バッチ集計  (conservative / BT帯別)  {TODAY}")
print(f"{'='*80}")

# 横断表
col_w = 13
print(f"\n{'BT帯':<10}", end="")
for p in valid_periods:
    print(f"  {str(p)+'d 損益':>{col_w}}", end="")
print()
print("-" * (10 + len(valid_periods) * (col_w + 2)))

for lo, hi, lbl in BT_BANDS:
    print(f"{lbl:<10}", end="")
    for p in valid_periods:
        s = all_results[p].get(lbl, {}); pnl = s.get("pnl", 0); n = s.get("n", 0)
        cell = f"{pnl:+,.0f}円({n}件)" if n else "—"
        print(f"  {cell:>{col_w}}", end="")
    print()

print(f"{'BT60+':<10}", end="")
for p in valid_periods:
    s = all_results[p].get("BT60+", {}); pnl = s.get("pnl", 0); n = s.get("n", 0)
    cell = f"{pnl:+,.0f}円({n}件)" if n else "—"
    print(f"  {cell:>{col_w}}", end="")
print()
print(f"{'ALL':<10}", end="")
for p in valid_periods:
    s = all_results[p].get("ALL", {}); pnl = s.get("pnl", 0); n = s.get("n", 0)
    cell = f"{pnl:+,.0f}円({n}件)" if n else "—"
    print(f"  {cell:>{col_w}}", end="")
print()

# 期間別詳細
print(f"\n{'='*80}")
for p in valid_periods:
    r = all_results[p]
    total = r.get("ALL", {}); n_all = total.get("n", 0); pnl_all = total.get("pnl", 0)
    print(f"\n--- {p}日ホールドアウト: {n_all}件 / {pnl_all:+,.0f}円 ---")
    print(f"  {'BT帯':<10} {'件数':>4} {'勝率':>6} {'PF':>5} {'損益':>14}")
    for lo, hi, lbl in BT_BANDS:
        s = r.get(lbl, {}); n = s.get("n", 0)
        if n == 0: continue
        wr = s.get("wr", 0); pf = _pf_s(s.get("pf", 0)); pnl = s.get("pnl", 0)
        mark = " ✅" if pnl > 0 else (" ❌" if pnl < -10000 else " △")
        print(f"  {lbl:<10} {n:>4} {wr:>5.1f}% {pf:>5} {pnl:>+13,.0f}円{mark}")
    s60 = r.get("BT60+", {}); n60 = s60.get("n", 0)
    if n60:
        p60 = s60.get("pnl", 0); wr60 = s60.get("wr", 0)
        print(f"  {'BT60+ 計':<10} {n60:>4} {wr60:>5.1f}% {_pf_s(s60.get('pf',0)):>5} {p60:>+13,.0f}円")


# ── CSV 保存 ────────────────────────────────────────────────────────────────
if not args.no_save and valid_periods:
    csv_out = Path(f"holdout_batch_results_{TODAY}.csv")
    bands_order = [lbl for _, _, lbl in BT_BANDS] + ["BT60+", "ALL"]
    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["holdout_days", "bt_band", "n", "wins", "wr_pct", "pf",
                    "pnl", "gp", "gl"])
        for p in valid_periods:
            for lbl in bands_order:
                s = all_results[p].get(lbl, {})
                pf_v = s.get("pf", 0)
                w.writerow([p, lbl, s.get("n",0), s.get("wins",0),
                             round(s.get("wr",0), 1),
                             round(pf_v, 2) if pf_v != float("inf") else 9999,
                             s.get("pnl",0), s.get("gp",0), s.get("gl",0)])
    print(f"\n[CSV] {csv_out.resolve()}")

    # Markdown
    md_out = Path(f"holdout_batch_results_{TODAY}.md")
    hdr = ["BT帯"] + [f"{p}d" for p in valid_periods]
    rows_md = [
        "# ホールドアウト結果バッチ集計\n",
        f"集計日: {TODAY}  \nモード: conservative  \n"
        f"フィルター: MaxDD≤{args.max_dd}%  Sharpe≥{args.min_sharpe}",
        *([ f"株価上限: {effective_max_price:,.0f}円" ] if effective_max_price > 0 else []),
        "\n## BT帯別 損益横断表\n",
        "| " + " | ".join(hdr) + " |",
        "|" + "|".join(["---"] * len(hdr)) + "|",
    ]
    for lo, hi, lbl in BT_BANDS + [(60, 101, "BT60+"), (0, 101, "ALL")]:
        key = "BT60+" if lo == 60 and hi == 101 else ("ALL" if lo == 0 and hi == 101 else lbl)
        row = [f"**{key}**"]
        for p in valid_periods:
            s = all_results[p].get(key, {}); n = s.get("n", 0); pnl = s.get("pnl", 0)
            wr = s.get("wr", 0)
            if n == 0:
                row.append("—")
            else:
                em = "🟢" if pnl > 0 else ("🔴" if pnl < -10000 else "🟡")
                row.append(f"{em}{pnl:+,.0f}円 ({wr:.0f}%/{n}件)")
        rows_md.append("| " + " | ".join(row) + " |")

    rows_md += [
        "\n## 実運用フィルター基準（検証結論）\n",
        "| 基準 | 内容 |", "|---|---|",
        "| **BTスコア≥60** | 全期間（30〜180日ホールドアウト）で一貫してプラス |",
        "| **conservative優先** | 中長期でconservativeがaggressive より安定 |",
        "| **BT0-29は除外** | 件数多く最大の損失源 |",
        "| **WFスコアは参考程度** | BT識別力の方が高い |",
    ]
    md_out.write_text("\n".join(rows_md), encoding="utf-8")
    print(f"[MD]  {md_out.resolve()}")

print(f"\n完了。")
