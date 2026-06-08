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

os.environ["TRADING_MODE"] = "aggressive"
_importlib.reload(_stop); _importlib.reload(_brk)
_AGG_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_AGG_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
os.environ["TRADING_MODE"] = "conservative"
_importlib.reload(_stop); _importlib.reload(_brk)

# nikkei_analysis をロード (_tab5_pnl_html を再利用するため)
# WATCHLIST を保持するために reload ガードを設定
_stop.WATCHLIST = []; _brk.WATCHLIST = []
_orig_reload_guard = _importlib.reload
def _guard_reload(module):
    result = _orig_reload_guard(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST  = list(_stop.WATCHLIST)
        result._WF_SCORES = dict(getattr(_stop, "_WF_SCORES", {}))
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST  = list(_brk.WATCHLIST)
        result._WF_SCORES = dict(getattr(_brk, "_WF_SCORES", {}))
    return result
_importlib.reload = _guard_reload
import nikkei_analysis as _na  # noqa: E402
_importlib.reload = _orig_reload_guard


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


def _load_watchlist_for_period(holdout_days: int,
                               mode: str = "conservative") -> tuple[list, list, str]:
    """
    指定モードの WATCHLIST をCSVから読み込む。
    CSVがない場合は --no-scan でなければ自動スキャンを実行する。
    """
    strategies_stop = ["MACD", "A7", "RSI2"]
    strategies_brk  = ["DON", "VOL", "MOM"]
    stop_wl: list = []; brk_wl: list = []; csv_date = str(TODAY)

    for strategies, target_wl in [(strategies_stop, stop_wl), (strategies_brk, brk_wl)]:
        for strategy in strategies:
            csv_path = _find_holdout_csv(strategy, holdout_days, args.wf_dir, mode)

            if csv_path is None:
                if args.no_scan:
                    print(f"  [SKIP] {strategy}/{mode} holdout{holdout_days}d CSV なし (--no-scan)")
                    continue
                print(f"  [SCAN] {strategy}/{mode} holdout{holdout_days}d CSV なし → スキャン実行")
                rows = _run_scan(strategy, holdout_days, effective_max_price,
                                 args.workers, args.wf_dir)
                csv_path = _find_holdout_csv(strategy, holdout_days, args.wf_dir, mode)
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


# BTスコア構成要素タイプ (wr_n > pf_n → 高WR型、pf_n > wr_n → 高PF型、tie → 安定/取引数型)
BT_TYPES = ["高WR", "高PF", "安定", "取引数"]


def _decompose_score(pr: dict) -> dict:
    """BTスコアの4構成要素を計算して返す。
    戻り値: wr_pts, pf_pts, stable_pts, trades_pts, avg_wr, avg_pf, bt_type
    """
    results = [r for r in pr.values() if r and r.get("trades", 0) > 0]
    if not results:
        return {"wr_pts": 0, "pf_pts": 0, "stable_pts": 0, "trades_pts": 0,
                "avg_wr": 0.0, "avg_pf": 0.0, "stable": 0.0, "t_trades": 0,
                "bt_type": "不明"}

    avg_wr   = sum(r["win_rate"] for r in results) / len(results)
    avg_pf   = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10)
                   for r in results) / len(results)
    stable   = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades = sum(r["trades"] for r in results)

    wr_pts     = avg_wr * 0.4          # max 40pts
    pf_pts     = (avg_pf / 10) * 30   # max 30pts
    stable_pts = stable * 20           # max 20pts
    trades_pts = min(t_trades / 20, 1) * 10  # max 10pts

    # 正規化 (各コンポーネントの最大値で割る)
    wr_n     = wr_pts / 40
    pf_n     = pf_pts / 30
    stable_n = stable_pts / 20
    trades_n = trades_pts / 10

    # 支配的なコンポーネント = スコアへの「相対的な貢献度」が最大
    bt_type = max(
        [("高WR", wr_n), ("高PF", pf_n), ("安定", stable_n), ("取引数", trades_n)],
        key=lambda x: x[1]
    )[0]

    return {
        "wr_pts": wr_pts, "pf_pts": pf_pts, "stable_pts": stable_pts, "trades_pts": trades_pts,
        "avg_wr": avg_wr, "avg_pf": avg_pf, "stable": stable, "t_trades": t_trades,
        "wr_n": wr_n, "pf_n": pf_n, "stable_n": stable_n, "trades_n": trades_n,
        "bt_type": bt_type,
    }


def _collect_trades(stop_wl: list, brk_wl: list,
                    holdout_days: int, mode: str = "conservative") -> list[dict]:
    """バックテスト実行 → holdout期間内のトレードにBTスコア・modeを付けて返す。"""
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
        trade_log    = pr[max(pr.keys())].get("trade_log", [])
        rec_score, _ = _stop.calc_recommend_score(pr)
        comp         = _decompose_score(pr)

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
            trades.append({
                "symbol": sym, "strategy": strat, "mode": mode,
                "rec_score": rec_score, "pnl": t.get("pnl", 0),
                "signal_dt": signal_dt,
                "bt_type":    comp["bt_type"],
                "wr_pts":     comp["wr_pts"],
                "pf_pts":     comp["pf_pts"],
                "stable_pts": comp["stable_pts"],
                "trades_pts": comp["trades_pts"],
                "avg_wr":     comp["avg_wr"],
                "avg_pf":     comp["avg_pf"],
            })
    return trades


# ── メイン処理 ───────────────────────────────────────────────────────────────
all_results: dict[int, dict] = {}  # all_results[period] = {bands, con_kpi, agg_kpi, combined_kpi}
pnl_html_per_period: dict[int, str] = {}  # 期間ごとの損益タブHTML

def _empty_stats() -> dict:
    return {"n":0,"wins":0,"wr":0,"pf":0,"pnl":0,"gp":0,"gl":0}

for holdout_days in periods:
    print(f"\n{'='*60}")
    print(f"  ホールドアウト {holdout_days}日")
    print(f"{'='*60}")

    # conservative
    stop_con, brk_con, csv_date_con = _load_watchlist_for_period(holdout_days, "conservative")
    # aggressive
    stop_agg, brk_agg, csv_date_agg = _load_watchlist_for_period(holdout_days, "aggressive")

    if not stop_con and not brk_con and not stop_agg and not brk_agg:
        print(f"  [SKIP] WATCHLIST が空 → この期間をスキップ")
        all_results[holdout_days] = {}
        continue

    print(f"  CON WATCHLIST: 逆指値B={len(stop_con)}件 BRK={len(brk_con)}件 (CSV: {csv_date_con})")
    print(f"  AGG WATCHLIST: 逆指値B={len(stop_agg)}件 BRK={len(brk_agg)}件 (CSV: {csv_date_agg})")

    wf_scores = _build_wf_scores(holdout_days, args.wf_dir)
    _stop._WF_SCORES = dict(wf_scores)
    _brk._WF_SCORES  = dict(wf_scores)

    # conservative バックテスト
    _stop.STRATEGY_PARAMS.update(_CON_STOP)
    _brk.STRATEGY_PARAMS.update(_CON_BRK)
    print(f"  [conservative] バックテスト実行中...", flush=True)
    con_trades = _collect_trades(stop_con, brk_con, holdout_days, "conservative")
    print(f"  [conservative] 完了: {len(con_trades)}トレード")

    # aggressive バックテスト
    _stop.STRATEGY_PARAMS.update(_AGG_STOP)
    _brk.STRATEGY_PARAMS.update(_AGG_BRK)
    print(f"  [aggressive]   バックテスト実行中...", flush=True)
    agg_trades = _collect_trades(stop_agg, brk_agg, holdout_days, "aggressive")
    print(f"  [aggressive]   完了: {len(agg_trades)}トレード")

    # conservative に戻す
    _stop.STRATEGY_PARAMS.update(_CON_STOP)
    _brk.STRATEGY_PARAMS.update(_CON_BRK)

    # 重複除外 combined (同一 symbol+strategy+signal_dt は最初のconfigのみ)
    seen_combined: set = set()
    combined_trades: list[dict] = []
    for t in con_trades + agg_trades:
        k = (t["symbol"], t["strategy"], t["signal_dt"])
        if k not in seen_combined:
            seen_combined.add(k)
            combined_trades.append(t)

    # BT帯集計 (combinedベース)
    band_results: dict[str, dict] = {}
    for lo, hi, lbl in BT_BANDS:
        band = [t for t in combined_trades if t.get("rec_score") is not None
                and lo <= t["rec_score"] < hi]
        band_results[lbl] = _band_stats(band) if band else _empty_stats()
    band_results["BT60+"] = _band_stats(
        [t for t in combined_trades if (t.get("rec_score") or 0) >= 60]
    ) if combined_trades else _empty_stats()
    band_results["ALL"] = _band_stats(combined_trades) if combined_trades else _empty_stats()

    all_results[holdout_days] = {
        "bands":           band_results,
        "con":             _band_stats(con_trades)      if con_trades      else _empty_stats(),
        "agg":             _band_stats(agg_trades)      if agg_trades      else _empty_stats(),
        "combined":        _band_stats(combined_trades) if combined_trades else _empty_stats(),
        "con_wl":          (len(stop_con), len(brk_con)),
        "agg_wl":          (len(stop_agg), len(brk_agg)),
        "combined_trades": list(combined_trades),  # 構成要素分析用
    }

    # 損益タブHTML生成 (_na._tab5_pnl_html を使って nikkei_analysis と同じ内容を生成)
    _label_p = f"holdout{holdout_days}d"
    _na._PNL_CONFIGS[:] = [
        {"label": f"{_label_p} conservative", "color": "#3498db",
         "mode": "conservative", "sm_tm": None,
         "stop_wl": list(stop_con), "brk_wl": list(brk_con)},
        {"label": f"{_label_p} aggressive",   "color": "#e74c3c",
         "mode": "aggressive",   "sm_tm": None,
         "stop_wl": list(stop_agg), "brk_wl": list(brk_agg)},
    ]
    print(f"  [損益タブHTML] holdout{holdout_days}d 生成中 (workers={args.workers})...", flush=True)
    try:
        pnl_html_per_period[holdout_days] = _na._tab5_pnl_html(holdout_days, args.workers)
        print(f"  [損益タブHTML] holdout{holdout_days}d 完了")
    except Exception as e:
        print(f"  [損益タブHTML] holdout{holdout_days}d エラー: {e}")
        pnl_html_per_period[holdout_days] = f'<p style="color:#f87171;padding:20px">HTML生成エラー: {e}</p>'
    # conservative に戻す
    _stop.STRATEGY_PARAMS.update(_CON_STOP)
    _brk.STRATEGY_PARAMS.update(_CON_BRK)


# ── コンソール表示 ─────────────────────────────────────────────────────────────
def _pf_s(v) -> str: return "∞" if v == float("inf") else f"{v:.2f}"

valid_periods = [p for p in periods if all_results.get(p)]

print(f"\n\n{'='*80}")
print(f"  ホールドアウト結果バッチ集計  {TODAY}")
print(f"{'='*80}")

# 損益サマリー横断表
print(f"\n{'期間':<8} {'CON 損益':>14} {'AGG 損益':>14} {'合計(重複除外)':>16} {'件数':>6} {'勝率':>6}")
print("-" * 70)
for p in valid_periods:
    r   = all_results[p]
    con = r.get("con", {}); agg = r.get("agg", {}); cmb = r.get("combined", {})
    print(f"  {p:>3}d  "
          f"  {r['con']['pnl']:>+12,.0f}円"
          f"  {r['agg']['pnl']:>+12,.0f}円"
          f"  {r['combined']['pnl']:>+14,.0f}円"
          f"  {r['combined']['n']:>5}件"
          f"  {r['combined']['wr']:>5.1f}%")

# BT帯横断表
col_w = 13
print(f"\n{'BT帯':<10}", end="")
for p in valid_periods:
    print(f"  {str(p)+'d 損益':>{col_w}}", end="")
print()
print("-" * (10 + len(valid_periods) * (col_w + 2)))

for lo, hi, lbl in BT_BANDS:
    print(f"{lbl:<10}", end="")
    for p in valid_periods:
        s = all_results[p].get("bands", {}).get(lbl, {}); pnl=s.get("pnl",0); n=s.get("n",0)
        cell = f"{pnl:+,.0f}円({n}件)" if n else "—"
        print(f"  {cell:>{col_w}}", end="")
    print()
for key in ("BT60+", "ALL"):
    print(f"{key:<10}", end="")
    for p in valid_periods:
        s = all_results[p].get("bands", {}).get(key, {}); pnl=s.get("pnl",0); n=s.get("n",0)
        cell = f"{pnl:+,.0f}円({n}件)" if n else "—"
        print(f"  {cell:>{col_w}}", end="")
    print()

# 期間別詳細
print(f"\n{'='*80}")
for p in valid_periods:
    r   = all_results[p]
    cmb = r.get("combined", {}); con = r.get("con", {}); agg = r.get("agg", {})
    print(f"\n--- {p}日ホールドアウト ---")
    print(f"  conservative : {con['n']:3}件 / 勝率{con['wr']:.1f}% / PF{_pf_s(con['pf'])} / {con['pnl']:+,.0f}円")
    print(f"  aggressive   : {agg['n']:3}件 / 勝率{agg['wr']:.1f}% / PF{_pf_s(agg['pf'])} / {agg['pnl']:+,.0f}円")
    print(f"  重複除外合計 : {cmb['n']:3}件 / 勝率{cmb['wr']:.1f}% / PF{_pf_s(cmb['pf'])} / {cmb['pnl']:+,.0f}円")
    print(f"  {'BT帯':<10} {'件数':>4} {'勝率':>6} {'PF':>5} {'損益':>14}")
    bands = r.get("bands", {})
    for lo, hi, lbl in BT_BANDS:
        s = bands.get(lbl, {}); n = s.get("n", 0)
        if n == 0: continue
        pnl = s.get("pnl", 0)
        mark = " ✅" if pnl > 0 else (" ❌" if pnl < -10000 else " △")
        print(f"  {lbl:<10} {n:>4} {s['wr']:>5.1f}% {_pf_s(s['pf']):>5} {pnl:>+13,.0f}円{mark}")
    s60 = bands.get("BT60+", {}); n60 = s60.get("n", 0)
    if n60:
        print(f"  {'BT60+ 計':<10} {n60:>4} {s60['wr']:>5.1f}% {_pf_s(s60['pf']):>5} {s60['pnl']:>+13,.0f}円")

# ── ② BTスコア構成要素別分析（コンソール）────────────────────────────────────
print(f"\n\n{'='*80}")
print("  ② BTスコア構成要素別分析（BT60+ 限定）")
print(f"{'='*80}")
type_colors_label = {"高WR": "WR主導", "高PF": "PF主導", "安定": "安定主導", "取引数": "取引主導"}
col_w2 = 18
print(f"\n{'タイプ':<10}", end="")
for p in valid_periods:
    print(f"  {str(p)+'d':>{col_w2}}", end="")
print()
print("-" * (10 + len(valid_periods) * (col_w2 + 2)))
for bt_type in BT_TYPES:
    print(f"{bt_type+'型':<10}", end="")
    for p in valid_periods:
        trades_p = all_results[p].get("combined_trades", [])
        subset = [t for t in trades_p
                  if (t.get("rec_score") or 0) >= 60 and t.get("bt_type") == bt_type]
        s = _band_stats(subset) if subset else _empty_stats()
        n = s.get("n", 0)
        cell = f"{s['pnl']:+,.0f}円({n}件)" if n else "—"
        print(f"  {cell:>{col_w2}}", end="")
    print()

print(f"\n--- 高WR型 vs 高PF型 比較（BT60+）---")
print(f"{'期間':<8}  {'高WR型':>20}  {'高PF型':>20}")
for p in valid_periods:
    trades_p = all_results[p].get("combined_trades", [])
    bt60  = [t for t in trades_p if (t.get("rec_score") or 0) >= 60]
    wr_g  = _band_stats([t for t in bt60 if (t.get("wr_pts",0)/40) >= (t.get("pf_pts",0)/30)])
    pf_g  = _band_stats([t for t in bt60 if (t.get("wr_pts",0)/40) <  (t.get("pf_pts",0)/30)])
    def _s(s): return f"{s['pnl']:+,.0f}円/{s['n']}件/{s['wr']:.0f}%" if s.get("n") else "—"
    print(f"  {p:>3}d    {_s(wr_g):>20}  {_s(pf_g):>20}")


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
                s = all_results[p].get("bands", {}).get(lbl, {})
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
            s = all_results[p].get("bands", {}).get(key, {}); n = s.get("n", 0); pnl = s.get("pnl", 0)
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

    # ── HTML 生成 ──────────────────────────────────────────────────────────
    def _cell(s: dict) -> str:
        n=s.get("n",0); pnl=s.get("pnl",0); wr=s.get("wr",0); pf=s.get("pf",0)
        if n == 0:
            return '<td style="color:#475569;text-align:center">—</td>'
        pf_s = "∞" if pf==float("inf") else f"{pf:.2f}"
        col  = "#4ade80" if pnl>0 else ("#f87171" if pnl<-10000 else "#fbbf24")
        return (f'<td style="text-align:right">'
                f'<span style="color:{col};font-weight:700">{pnl:+,.0f}円</span>'
                f'<br><span style="color:#94a3b8;font-size:0.75rem">'
                f'{wr:.0f}% / {n}件 / PF{pf_s}</span></td>')

    def _kpi_cell(s: dict, label: str, color: str) -> str:
        n=s.get("n",0); pnl=s.get("pnl",0); wr=s.get("wr",0); pf=s.get("pf",0)
        pf_s = "∞" if pf==float("inf") else f"{pf:.2f}"
        pc = "#4ade80" if pnl>=0 else "#f87171"
        return (f'<td style="text-align:right">'
                f'<span style="color:{color};font-size:0.75rem">{label}</span><br>'
                f'<span style="color:{pc};font-weight:700">{pnl:+,.0f}円</span>'
                f'<br><span style="color:#94a3b8;font-size:0.72rem">'
                f'{wr:.0f}% / {n}件 / PF{pf_s}</span></td>')

    # 損益サマリー横断表
    summary_rows = ""
    for p in valid_periods:
        r = all_results[p]
        con=r.get("con",{}); agg=r.get("agg",{}); cmb=r.get("combined",{})
        con_wl=r.get("con_wl",(0,0)); agg_wl=r.get("agg_wl",(0,0))
        cmb_pc="#4ade80" if cmb.get("pnl",0)>=0 else "#f87171"
        summary_rows += (
            f'<tr><td style="font-weight:700;color:#60a5fa">{p}d</td>'
            f'<td style="color:#94a3b8;font-size:0.78rem">逆B{con_wl[0]}/BRK{con_wl[1]}</td>'
            + _kpi_cell(con, "conservative", "#3498db")
            + _kpi_cell(agg, "aggressive",   "#e74c3c")
            + f'<td style="text-align:right;border-left:2px solid #3b82f6">'
            f'<span style="color:{cmb_pc};font-weight:700">{cmb.get("pnl",0):+,.0f}円</span>'
            f'<br><span style="color:#94a3b8;font-size:0.72rem">'
            f'{cmb.get("wr",0):.0f}% / {cmb.get("n",0)}件 / PF{_pf_s(cmb.get("pf",0))}</span>'
            f'</td></tr>\n'
        )

    # BT帯横断表 HTML
    th_cols = "".join(f'<th>{p}d</th>' for p in valid_periods)
    cross_rows_html = ""
    for lo, hi, lbl in BT_BANDS + [(60, 101, "BT60+"), (0, 101, "ALL")]:
        key = "BT60+" if lo==60 and hi==101 else ("ALL" if lo==0 and hi==101 else lbl)
        if key in ("BT60+", "ALL"):
            style = ' style="border-top:2px solid #3b82f6;background:#0d1424"'
            lbl_html = f'<td style="color:#60a5fa;font-weight:700">▶ {key}</td>'
        elif lo in (60, 80):
            style = ' style="border-top:2px solid #334155"'
            lbl_html = f'<td style="font-weight:700">{lbl}</td>'
        else:
            style = ""; lbl_html = f'<td>{lbl}</td>'
        cells = "".join(_cell(all_results[p].get("bands",{}).get(key, {})) for p in valid_periods)
        cross_rows_html += f"<tr{style}>{lbl_html}{cells}</tr>\n"

    # 期間別詳細 HTML
    detail_html = ""
    for p in valid_periods:
        r   = all_results[p]
        cmb = r.get("combined",{}); con=r.get("con",{}); agg=r.get("agg",{})
        bands = r.get("bands", {})
        cmb_pc = "#4ade80" if cmb.get("pnl",0)>=0 else "#f87171"
        detail_rows = ""
        for lo, hi, lbl in BT_BANDS:
            s=bands.get(lbl,{}); n=s.get("n",0)
            if n==0: continue
            pnl=s.get("pnl",0); wr=s.get("wr",0); pf=s.get("pf",0)
            pf_s="∞" if pf==float("inf") else f"{pf:.2f}"
            col="#4ade80" if pnl>0 else ("#f87171" if pnl<-10000 else "#fbbf24")
            bdr=' style="border-top:2px solid #334155"' if lo in (60,80) else ""
            detail_rows += (
                f'<tr{bdr}><td style="font-weight:700">{lbl}</td>'
                f'<td style="text-align:right">{n}</td>'
                f'<td style="text-align:right">{wr:.1f}%</td>'
                f'<td style="text-align:right">{pf_s}</td>'
                f'<td style="text-align:right;color:{col};font-weight:700">{pnl:+,.0f}円</td></tr>\n'
            )
        s60=bands.get("BT60+",{}); n60=s60.get("n",0)
        if n60:
            p60=s60.get("pnl",0); c60="#4ade80" if p60>=0 else "#f87171"
            detail_rows += (
                f'<tr style="border-top:2px solid #3b82f6;background:#0d1424">'
                f'<td style="color:#60a5fa;font-weight:700">BT60+ 計</td>'
                f'<td style="text-align:right;color:#60a5fa">{n60}</td>'
                f'<td style="text-align:right;color:#60a5fa">{s60["wr"]:.1f}%</td>'
                f'<td style="text-align:right;color:#60a5fa">{_pf_s(s60["pf"])}</td>'
                f'<td style="text-align:right;color:{c60};font-weight:700">{p60:+,.0f}円</td></tr>\n'
            )
        detail_html += f"""
<h2>{p}日ホールドアウト
  <span style="font-size:0.85rem;font-weight:400;color:#94a3b8">
    重複除外: {cmb.get('n',0)}件 /
    <span style="color:{cmb_pc}">{cmb.get('pnl',0):+,.0f}円</span>
  </span>
</h2>
<div style="display:flex;gap:16px;margin-bottom:10px;flex-wrap:wrap">
  <div style="background:#1a2a3a;padding:10px 16px;border-radius:6px;border-left:3px solid #3498db">
    <div style="color:#94a3b8;font-size:0.78rem">conservative</div>
    <div style="color:{"#4ade80" if con.get("pnl",0)>=0 else "#f87171"};font-weight:700;font-size:1.05rem">{con.get("pnl",0):+,.0f}円</div>
    <div style="color:#94a3b8;font-size:0.75rem">{con.get("n",0)}件 / 勝率{con.get("wr",0):.1f}% / PF{_pf_s(con.get("pf",0))}</div>
  </div>
  <div style="background:#2a1a1a;padding:10px 16px;border-radius:6px;border-left:3px solid #e74c3c">
    <div style="color:#94a3b8;font-size:0.78rem">aggressive</div>
    <div style="color:{"#4ade80" if agg.get("pnl",0)>=0 else "#f87171"};font-weight:700;font-size:1.05rem">{agg.get("pnl",0):+,.0f}円</div>
    <div style="color:#94a3b8;font-size:0.75rem">{agg.get("n",0)}件 / 勝率{agg.get("wr",0):.1f}% / PF{_pf_s(agg.get("pf",0))}</div>
  </div>
</div>
<table>
  <thead><tr><th style="text-align:left">BT帯</th>
    <th style="text-align:right">件数</th><th style="text-align:right">勝率</th>
    <th style="text-align:right">PF</th><th style="text-align:right">損益</th>
  </tr></thead>
  <tbody>{detail_rows}</tbody>
</table>"""

    filter_note = ""
    if effective_max_price > 0:
        filter_note = f" / 株価上限 {effective_max_price:,.0f}円"

    # ── タブ付きHTML生成 ──────────────────────────────────────────────────────
    # サマリータブ内容: スクリプト別サマリー + BT帯横断表 + 期間別詳細
    summary_tab_html = f"""
<h2>スクリプト別サマリー（全期間）</h2>
<table>
  <thead><tr>
    <th style="text-align:left">期間</th>
    <th>WATCHLIST</th>
    <th>conservative</th>
    <th>aggressive</th>
    <th style="border-left:2px solid #3b82f6">重複除外合計</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>BT帯別 損益横断表</h2>
<p class="footnote" style="margin-bottom:8px">
  各セル: 損益 / 勝率 / 件数 / PF。境界線 = BT60(重要閾値) / BT80
</p>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th style="text-align:left">BT帯</th>{th_cols}
  </tr></thead>
  <tbody>{cross_rows_html}</tbody>
</table>
</div>

<h2>期間別詳細（BT帯）</h2>
{detail_html}

<p class="footnote" style="margin-top:24px">
  実運用フィルター基準: BTスコア≥60 が全期間で一貫してプラス。
  conservative優先。BT0-29は最大損失源のため除外推奨。
</p>"""

    # ── ② BTスコア構成要素分析 HTML ───────────────────────────────────────────
    def _comp_cross_html() -> str:
        """BT帯 × タイプ × 期間 のクロス集計HTML。"""
        type_colors = {"高WR": "#3b82f6", "高PF": "#f59e0b", "安定": "#10b981", "取引数": "#a855f7"}

        # 全期間を横断して「BT60+ × タイプ別 × 期間」の表を作る
        # Row = タイプ、Col = 期間
        type_cross_rows = ""
        for bt_type in BT_TYPES:
            col = type_colors.get(bt_type, "#94a3b8")
            type_cross_rows += f'<tr><td style="color:{col};font-weight:700">{bt_type}型</td>\n'
            for p in valid_periods:
                trades_p = all_results[p].get("combined_trades", [])
                subset = [t for t in trades_p
                          if (t.get("rec_score") or 0) >= 60 and t.get("bt_type") == bt_type]
                s = _band_stats(subset) if subset else _empty_stats()
                n = s.get("n", 0)
                pnl = s.get("pnl", 0)
                pf_v = s.get("pf", 0)
                if n == 0:
                    type_cross_rows += '<td style="color:#475569;text-align:center">—</td>'
                else:
                    pc = "#4ade80" if pnl > 0 else "#f87171"
                    pf_s = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
                    type_cross_rows += (
                        f'<td style="text-align:right">'
                        f'<span style="color:{pc};font-weight:700">{pnl:+,.0f}円</span>'
                        f'<br><span style="color:#94a3b8;font-size:0.72rem">{s["wr"]:.0f}%/{n}件/PF{pf_s}</span>'
                        f'</td>'
                    )
            type_cross_rows += "</tr>\n"

        # BT帯 × タイプ の詳細表（最新180d期間）
        bt_type_detail = ""
        for period_key in valid_periods:
            trades_p = all_results[period_key].get("combined_trades", [])
            if not trades_p:
                continue
            bt_type_detail += f'<h3 style="margin-top:20px;color:#94a3b8">{period_key}日ホールドアウト</h3>\n'
            bt_type_detail += '<table>\n<thead><tr>'
            bt_type_detail += '<th style="text-align:left">BT帯</th>'
            for bt_type in BT_TYPES:
                col = type_colors.get(bt_type, "#94a3b8")
                bt_type_detail += f'<th style="color:{col}">{bt_type}型</th>'
            bt_type_detail += '</tr></thead>\n<tbody>\n'

            for lo, hi, lbl in [(60, 70, "60-69"), (70, 80, "70-79"), (80, 101, "80-100"), (60, 101, "BT60+")]:
                is_summary = (lo == 60 and hi == 101)
                row_style = ' style="border-top:2px solid #3b82f6;background:#0d1424"' if is_summary else ""
                lbl_style = 'color:#60a5fa;font-weight:700' if is_summary else 'font-weight:700'
                bt_type_detail += f'<tr{row_style}><td style="{lbl_style}">{lbl}</td>'
                band_trades = [t for t in trades_p
                               if (t.get("rec_score") or 0) >= lo and (t.get("rec_score") or 0) < hi]
                for bt_type in BT_TYPES:
                    subset = [t for t in band_trades if t.get("bt_type") == bt_type]
                    s = _band_stats(subset) if subset else _empty_stats()
                    n = s.get("n", 0)
                    if n == 0:
                        bt_type_detail += '<td style="color:#475569;text-align:center">—</td>'
                    else:
                        pnl = s.get("pnl", 0)
                        pf_v = s.get("pf", 0)
                        pc = "#4ade80" if pnl > 0 else "#f87171"
                        pf_s = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
                        col = type_colors.get(bt_type, "#94a3b8")
                        bt_type_detail += (
                            f'<td style="text-align:right">'
                            f'<span style="color:{pc};font-weight:700">{pnl:+,.0f}円</span>'
                            f'<br><span style="color:#94a3b8;font-size:0.72rem">'
                            f'{s["wr"]:.0f}%/{n}件/PF{pf_s}</span></td>'
                        )
                bt_type_detail += '</tr>\n'
            bt_type_detail += '</tbody></table>\n'

        # WR vs PF 比較（BT60+限定）
        wr_vs_pf_rows = ""
        labels = [("高WR型", "WR比率 ≥ PF比率"), ("高PF型", "PF比率 > WR比率")]
        for period_key in valid_periods:
            trades_p = all_results[period_key].get("combined_trades", [])
            bt60 = [t for t in trades_p if (t.get("rec_score") or 0) >= 60]
            wr_grp  = [t for t in bt60 if (t.get("wr_pts", 0)/40) >= (t.get("pf_pts", 0)/30)]
            pf_grp  = [t for t in bt60 if (t.get("wr_pts", 0)/40) <  (t.get("pf_pts", 0)/30)]
            s_wr = _band_stats(wr_grp) if wr_grp else _empty_stats()
            s_pf = _band_stats(pf_grp) if pf_grp else _empty_stats()

            def _mini(s: dict, c: str) -> str:
                n = s.get("n", 0)
                if n == 0: return "—"
                pc = "#4ade80" if s["pnl"] > 0 else "#f87171"
                pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
                return f'<span style="color:{pc};font-weight:700">{s["pnl"]:+,.0f}円</span><br><span style="color:#94a3b8;font-size:0.72rem">{s["wr"]:.0f}%/{n}件/PF{pf_s}</span>'

            wr_vs_pf_rows += (
                f'<tr><td style="color:#60a5fa;font-weight:700">{period_key}d</td>'
                f'<td style="text-align:right">{_mini(s_wr, "#3b82f6")}</td>'
                f'<td style="text-align:right">{_mini(s_pf, "#f59e0b")}</td></tr>\n'
            )

        th_p = "".join(f'<th>{p}d</th>' for p in valid_periods)
        return f"""
<h2>② BTスコア構成要素別分析</h2>
<p class="footnote">
  BTスコア = 勝率(max40pt) + PF(max30pt) + 安定性(max20pt) + 取引数(max10pt)。
  同じスコアでも「勝率主導」か「PF主導」かで実際の損益が異なる可能性を検証。<br>
  <b>タイプ判定</b>: 各要素を最大値で正規化し、最も高い要素を「支配型」とする。
  例: wr_pts/40 = 0.8 / pf_pts/30 = 0.5 → 高WR型
</p>

<h3>BT60+ × タイプ別 損益（全期間横断）</h3>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th style="text-align:left">タイプ</th>{th_p}
  </tr></thead>
  <tbody>{type_cross_rows}</tbody>
</table>
</div>

<h3>高WR型 vs 高PF型（BT60+ 限定 / WR比率 vs PF比率）</h3>
<p class="footnote">
  「高WR型」= wr_pts/40 ≥ pf_pts/30。「高PF型」= pf_pts/30 > wr_pts/40。<br>
  ユーザーの問い「勝率高め・PF低め vs PF高め・取引数少」に直接対応する分類。
</p>
<table>
  <thead><tr>
    <th>期間</th>
    <th style="color:#3b82f6">高WR型</th>
    <th style="color:#f59e0b">高PF型</th>
  </tr></thead>
  <tbody>{wr_vs_pf_rows}</tbody>
</table>

<h3>BT帯 × タイプ別 詳細（期間別）</h3>
{bt_type_detail}

<p class="footnote" style="margin-top:16px">
  ※ 「安定型」= 安定性(全期間プラス率)が最も高い銘柄。「取引数型」= 取引回数が多く安定型・WR/PF型ではない銘柄。
</p>"""

    comp_tab_html = _comp_cross_html()

    # タブナビゲーション
    tab_btns = '<button class="tab-btn active" data-tab="t_summary" onclick="switchTab(\'t_summary\')">📊 サマリー</button>\n'
    tab_panes = f'<div id="t_summary" class="tab-pane active">{summary_tab_html}</div>\n'
    tab_btns  += '  <button class="tab-btn" data-tab="t_comp" onclick="switchTab(\'t_comp\')">② 構成要素</button>\n'
    tab_panes += f'<div id="t_comp" class="tab-pane">{comp_tab_html}</div>\n'
    for p in valid_periods:
        tab_id = f"t_p{p}"
        tab_btns  += f'  <button class="tab-btn" data-tab="{tab_id}" onclick="switchTab(\'{tab_id}\')">{p}日</button>\n'
        pane_html = pnl_html_per_period.get(p, '<p style="color:#64748b;padding:20px">データなし</p>')
        tab_panes += f'<div id="{tab_id}" class="tab-pane">{pane_html}</div>\n'

    html_out = Path(f"holdout_batch_results_{TODAY}.html")
    html_out.write_text(f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ホールドアウト損益バッチ集計 {TODAY}</title>
<style>
{_na.CSS}
/* タブ横スクロール対応 */
.tab-nav {{ flex-wrap: nowrap !important; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
.tab-btn {{ padding: 7px 14px !important; font-size: 0.85rem !important; white-space: nowrap; }}
</style>
</head>
<body>
<h1>ホールドアウト損益バッチ集計</h1>
<p class="subtitle">集計日: {TODAY} / MaxDD≤{args.max_dd}%{filter_note} / workers={args.workers}</p>

<div class="tab-nav">
{tab_btns}</div>

{tab_panes}

<script>{_na.JS}</script>
</body>
</html>""", encoding="utf-8")
    print(f"[HTML] {html_out.resolve()}")

    if not args.no_save:
        from _open_html import open_html
        open_html(html_out)

print(f"\n完了。")
