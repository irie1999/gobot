"""
nikkei_analysis_holdout_2config.py  ―  ホールドアウトWF 2設定比較レポート

conservative と aggressive でそれぞれ独立した銘柄選定 (WFスキャン) を行い、
異なる構成銘柄・異なるパラメータで比較分析するレポートを生成する。

  conservative: scan_walkforward.py --holdout-days N          のCSVを使用
  aggressive  : scan_walkforward.py --holdout-days N --aggressive のCSVを使用

CSVが存在しない場合は自動でスキャンを実行して保存する。

使い方:
  python nikkei_analysis_holdout_2config.py --holdout-days 30
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --days 30
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --days 365
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --max-price 10000
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --budget 500000
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --workers 8
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --no-browser
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --max-dd 10 --min-sharpe 0.5
  python nikkei_analysis_holdout_2config.py --holdout-days 30 --no-save-csv  # CSV保存しない

出力: nikkei_analysis_holdout2cfg_{N}d_{date}.html
"""
from __future__ import annotations

import argparse
import copy as _copy
import csv
import importlib as _importlib
import os
import re as _re
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, ThreadPoolExecutor as _TPE
from concurrent.futures import as_completed, as_completed as _asc
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()


# ══════════════════════════════════════════════════════════════════════════════
# 1. 引数先読み (import より前)
# ══════════════════════════════════════════════════════════════════════════════
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--holdout-days",  type=int,   default=0)
_pre.add_argument("--per-strategy",  type=int,   default=10)
_pre.add_argument("--max-dd",        type=float, default=15.0)
_pre.add_argument("--max-consec",    type=int,   default=5)
_pre.add_argument("--min-sharpe",    type=float, default=0.0)
_pre.add_argument("--wf-dir",        type=Path,  default=Path("walkforward_results"))
_pre.add_argument("--no-browser",    action="store_true")
_pre.add_argument("--date",          type=str,   default=None)
_pre.add_argument("--max-price",     type=float, default=0.0,
                  help="株価上限 (円/株)。0=制限なし")
_pre.add_argument("--budget",        type=float, default=0.0,
                  help="予算 (円)。--max-price budget/100 と同義")
_pre.add_argument("--workers",       type=int,   default=4,
                  help="スキャン・バックテスト並列数")
_pre.add_argument("--no-save-csv",   action="store_true",
                  help="自動スキャン結果をCSV保存しない")
_pre_known, _na_argv = _pre.parse_known_args()
_wf_dir = _pre_known.wf_dir

_effective_max_price = _pre_known.max_price
if _pre_known.budget > 0 and _pre_known.max_price == 0:
    _effective_max_price = _pre_known.budget / 100.0


# ══════════════════════════════════════════════════════════════════════════════
# 2. モジュールロード & パラメータスナップショット
# ══════════════════════════════════════════════════════════════════════════════
os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop     as _stop
import check_signals_breakout as _brk
from compute_wf_scores import calc_wf_score, wf_rank

_CON_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_CON_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
os.environ["TRADING_MODE"] = "aggressive"
_importlib.reload(_stop); _importlib.reload(_brk)
_AGG_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_AGG_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
os.environ["TRADING_MODE"] = "conservative"
_importlib.reload(_stop); _importlib.reload(_brk)


# ══════════════════════════════════════════════════════════════════════════════
# 3. CSV・スキャンヘルパー
# ══════════════════════════════════════════════════════════════════════════════

def _float(v, default=0.0) -> float:
    try:    return float(v)
    except: return default

def _int(v, default=0) -> int:
    try:    return int(v)
    except: return default

def _composite_score(r: dict) -> float:
    return _float(r.get("total_test_pnl", 0)) * (1.0 + max(_float(r.get("sharpe", 0)), 0.0))


def _find_holdout_csv(strategy: str, holdout_days: int, wf_dir: Path,
                      mode: str = "conservative") -> Path | None:
    """指定モード・ホールドアウト日数に対応するCSVを検索する。"""
    mode_suffix = f"_{mode}" if mode != "conservative" else ""
    if holdout_days > 0:
        suffix = f"{mode_suffix}_holdout{holdout_days}d"
        candidates = sorted(wf_dir.glob(f"walkforward_{strategy}{suffix}_*.csv"), reverse=True)
    else:
        if mode == "conservative":
            # aggressive パターン (_aggressive_holdout) は除外
            candidates = [
                c for c in sorted(wf_dir.glob(f"walkforward_{strategy}_holdout*d_*.csv"), reverse=True)
                if "_aggressive_" not in c.name
            ]
        else:
            candidates = sorted(
                wf_dir.glob(f"walkforward_{strategy}_{mode}_holdout*d_*.csv"), reverse=True
            )
    return candidates[0] if candidates else None


def _rows_from_csv(csv_path: Path) -> tuple[list[dict], int, str]:
    stem  = csv_path.stem
    parts = stem.split("_")
    csv_date   = parts[-1]
    detected_n = 0
    for p in parts:
        if p.startswith("holdout") and p.endswith("d"):
            try: detected_n = int(p[7:-1])
            except ValueError: pass
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows, detected_n, csv_date


def _run_scan_for_strategy(strategy: str, holdout_days: int, max_price: float,
                            workers: int, wf_dir: Path, save_csv: bool,
                            mode: str = "conservative") -> list[dict]:
    """scan_walkforward.walkforward_one を直接呼び出してスキャン実行。"""
    import scan_walkforward as _swf

    # モードに対応したSTRATEGY_DEFSを設定
    orig_defs = _swf.STRATEGY_DEFS
    orig_mode = _swf.TRADING_MODE
    if mode == "aggressive":
        _swf.STRATEGY_DEFS = _swf.STRATEGY_DEFS_AGGRESSIVE
        _swf.TRADING_MODE  = "aggressive"
    else:
        _swf.STRATEGY_DEFS = _swf.STRATEGY_DEFS_CONSERVATIVE
        _swf.TRADING_MODE  = "conservative"

    orig_folds = list(_swf.FOLDS)
    if holdout_days > 0:
        _swf.FOLDS[:] = [
            (n, ts + holdout_days, te + holdout_days,
             vs + holdout_days, ve + holdout_days)
            for n, ts, te, vs, ve in orig_folds
        ]

    try:
        symbols, universe_name = _swf.load_universe()
        print(f"  [{strategy}/{mode}] ユニバース: {universe_name} ({len(symbols)} 銘柄)", flush=True)

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(_swf.walkforward_one, sym, name, strategy, max_price): sym
                for sym, name in symbols
            }
            done = 0
            every = max(len(symbols) // 10, 50)
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                    if r: results.append(r)
                except Exception:
                    pass
                if done % every == 0:
                    print(f"  [{strategy}/{mode}] {done}/{len(symbols)} 候補:{len(results)}", flush=True)
    finally:
        _swf.FOLDS[:] = orig_folds
        _swf.STRATEGY_DEFS = orig_defs
        _swf.TRADING_MODE  = orig_mode

    passed = sum(1 for r in results if r["folds_passed"] >= 2)
    print(f"  [{strategy}/{mode}] 完了: 候補={len(results)} 2fold通過={passed}")

    if save_csv and results:
        mode_suffix    = f"_{mode}" if mode != "conservative" else ""
        holdout_suffix = f"_holdout{holdout_days}d" if holdout_days > 0 else ""
        csv_path = wf_dir / f"walkforward_{strategy}{mode_suffix}{holdout_suffix}_{TODAY}.csv"
        fields = [
            "symbol", "name", "strategy", "family", "latest_price",
            "folds_passed", "total_test_trades", "avg_hold_days",
            "total_test_pnl", "total_train_pnl", "avg_test_pf", "avg_test_wr",
            "max_drawdown_pct", "max_consecutive_losses", "sharpe",
            "recovery_factor", "train_to_test_degradation_pct",
        ]
        wf_dir.mkdir(exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"  [{strategy}/{mode}] CSV保存: {csv_path}")

    return results


def _load_watchlist_for_mode(
    mode: str,
    holdout_days: int,
    per_strategy: int,
    max_dd: float,
    max_consec: int,
    min_sharpe: float,
    min_folds: int,
    wf_dir: Path,
    max_price: float = 0.0,
    workers: int = 4,
    save_csv: bool = True,
) -> tuple[list[tuple], list[tuple], int, str]:
    """指定モードのWATCHLISTを返す。CSVなければ自動スキャン。"""
    strategies_stop = ["MACD", "A7", "RSI2"]
    strategies_brk  = ["DON", "VOL", "MOM"]

    stop_wl: list[tuple[str, str, str]] = []
    brk_wl:  list[tuple[str, str, str]] = []
    detected_n = holdout_days
    csv_date   = str(TODAY)

    for strats, target_list in [(strategies_stop, stop_wl), (strategies_brk, brk_wl)]:
        for strategy in strats:
            csv_path = _find_holdout_csv(strategy, holdout_days, wf_dir, mode)

            if csv_path is None:
                print(f"[INFO] CSVなし ({strategy}/{mode}, holdout={holdout_days}d) → スキャン開始")
                rows = _run_scan_for_strategy(
                    strategy, holdout_days, max_price, workers, wf_dir, save_csv, mode
                )
                csv_date   = str(TODAY)
                detected_n = holdout_days
            else:
                rows, detected_n, csv_date = _rows_from_csv(csv_path)

            filtered = [r for r in rows if (
                _int(r.get("folds_passed", 0))                 >= min_folds
                and _float(r.get("max_drawdown_pct", 999))     <= max_dd
                and _int(r.get("max_consecutive_losses", 999)) <= max_consec
                and _float(r.get("sharpe", 0))                 >= min_sharpe
                and _float(r.get("total_test_pnl", 0))          > 0
                and (max_price <= 0 or _float(r.get("latest_price", 0)) <= max_price)
            )]
            filtered.sort(key=_composite_score, reverse=True)

            for r in filtered[:per_strategy]:
                sym   = r.get("symbol", "")
                name  = r.get("name", "")
                strat = r.get("strategy", strategy)
                if sym and strat:
                    target_list.append((sym, name, strat))

    return stop_wl, brk_wl, detected_n, csv_date


def _build_wf_scores_for_mode(mode: str, holdout_days: int,
                               csv_date: str, wf_dir: Path) -> dict:
    """指定モードのCSVからWFスコア辞書を構築する。"""
    strategies = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]
    scores: dict = {}
    for strategy in strategies:
        csv_path = _find_holdout_csv(strategy, holdout_days, wf_dir, mode)
        if csv_path is None:
            continue
        with open(csv_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    sym   = row["symbol"]
                    wr    = float(row["avg_test_wr"])
                    pf    = float(row["avg_test_pf"])
                    folds = int(row["folds_passed"])
                    score = calc_wf_score(wr, pf, folds)
                    key   = f"{sym}_{strategy}"
                    scores[key] = {
                        "score":    score,
                        "rank":     wf_rank(score),
                        "wr":       round(wr, 1),
                        "pf":       round(pf, 2),
                        "folds":    folds,
                        "csv_date": csv_date,
                    }
                except (ValueError, KeyError):
                    continue
    return scores


# ══════════════════════════════════════════════════════════════════════════════
# 4. WATCHLIST ロード (conservative / aggressive 独立)
# ══════════════════════════════════════════════════════════════════════════════

_kw = dict(
    holdout_days  = _pre_known.holdout_days,
    per_strategy  = _pre_known.per_strategy,
    max_dd        = _pre_known.max_dd,
    max_consec    = _pre_known.max_consec,
    min_sharpe    = _pre_known.min_sharpe,
    min_folds     = 2,
    wf_dir        = _wf_dir,
    max_price     = _effective_max_price,
    workers       = _pre_known.workers,
    save_csv      = not _pre_known.no_save_csv,
)

print("=" * 70)
print("nikkei_analysis_holdout_2config: 2設定 (conservative / aggressive)")
print(f"  ホールドアウト  : 直近 {_pre_known.holdout_days} 日")
print(f"  フィルター      : MaxDD<={_pre_known.max_dd}% 連敗<={_pre_known.max_consec} Sharpe>={_pre_known.min_sharpe}")
if _effective_max_price > 0:
    print(f"  株価上限        : {_effective_max_price:,.0f}円")
print("=" * 70)

print("\n--- conservative スキャン/ロード ---")
STOP_CON, BRK_CON, _HOLDOUT_N, _CSV_DATE_CON = _load_watchlist_for_mode(
    mode="conservative", **_kw
)
print("\n--- aggressive スキャン/ロード ---")
STOP_AGG, BRK_AGG, _,          _CSV_DATE_AGG = _load_watchlist_for_mode(
    mode="aggressive", **_kw
)

if not STOP_CON and not BRK_CON and not STOP_AGG and not BRK_AGG:
    print("[ERROR] 銘柄を1件も取得できませんでした。フィルター条件を確認してください。")
    sys.exit(1)

_WF_SCORES_CON = _build_wf_scores_for_mode("conservative", _HOLDOUT_N, _CSV_DATE_CON, _wf_dir)
_WF_SCORES_AGG = _build_wf_scores_for_mode("aggressive",   _HOLDOUT_N, _CSV_DATE_AGG, _wf_dir)
_WF_SCORES = {**_WF_SCORES_CON, **_WF_SCORES_AGG}

_label = f"holdout{_HOLDOUT_N}d" if _HOLDOUT_N > 0 else "holdout"

print(f"\n  conservative 逆指値B:{len(STOP_CON)} BRK:{len(BRK_CON)}")
print(f"  aggressive   逆指値B:{len(STOP_AGG)} BRK:{len(BRK_AGG)}")
print("=" * 70)


# ══════════════════════════════════════════════════════════════════════════════
# 5. nikkei_analysis をロード (conservative WL で初期化)
# ══════════════════════════════════════════════════════════════════════════════

_stop.WATCHLIST  = list(STOP_CON)
_brk.WATCHLIST   = list(BRK_CON)
_stop._WF_SCORES = dict(_WF_SCORES)
_brk._WF_SCORES  = dict(_WF_SCORES)

_orig_reload = _importlib.reload

def _preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST  = list(STOP_CON)
        result._WF_SCORES = dict(_WF_SCORES)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST  = list(BRK_CON)
        result._WF_SCORES = dict(_WF_SCORES)
    return result

_importlib.reload = _preserving_reload
import nikkei_analysis as _na  # noqa: E402
_importlib.reload = _orig_reload


# ══════════════════════════════════════════════════════════════════════════════
# 6. _PNL_CONFIGS を2設定に差し替え
# ══════════════════════════════════════════════════════════════════════════════

_na._PNL_CONFIGS[:] = [
    {
        "label":   f"{_label} conservative",
        "color":   "#3498db",
        "mode":    "conservative",
        "sm_tm":   None,
        "stop_wl": list(STOP_CON),
        "brk_wl":  list(BRK_CON),
    },
    {
        "label":   f"{_label} aggressive",
        "color":   "#e74c3c",
        "mode":    "aggressive",
        "sm_tm":   None,
        "stop_wl": list(STOP_AGG),
        "brk_wl":  list(BRK_AGG),
    },
]

CONFIGS = _na._PNL_CONFIGS


# ══════════════════════════════════════════════════════════════════════════════
# 7. シグナル判定カード
# ══════════════════════════════════════════════════════════════════════════════

def _judge_config(cfg: dict, r: dict) -> tuple[str, str, str]:
    trend = r["trend"]; vol = r["vol_level"]
    mom5  = r["mom5"];  mom20 = r["mom20"]
    above = r["above_ma200"]; drop = r["max_1d_drop"]
    mode  = cfg["mode"]; crash = drop < -3.0

    if mode == "aggressive":
        if trend == "down" and vol == "high":
            return "❌ 停止", f"下落×高ボラ (Vol={r['vol']:.2f}%)", "conservative に切替え"
        if trend == "down":
            return "⚠️ 注意", f"下落トレンド (5日{mom5:+.1f}%)", "conservative への切替えを検討"
        if not above:
            return "⚠️ 注意", "日経 < MA200 (長期下落)", "conservative を優先"
        if trend == "up" and mom5 >= 2.0 and mom20 >= 3.0:
            return "✅ 推奨", f"上昇×5日{mom5:+.1f}%/20日{mom20:+.1f}%", "最も効率が良い局面"
        if trend == "up":
            return "✅ 推奨", f"上昇トレンド (5日{mom5:+.1f}%)", "上昇継続なら標準運用"
        return "⚠️ 注意", f"横ばい (5日{mom5:+.1f}%)", "conservative 併用推奨"
    else:
        if trend == "down" and vol == "high" and crash:
            return "⚠️ 注意", f"下落×高ボラ×急落 (最大1日{drop:+.1f}%)", "ポジション縮小を検討"
        if trend == "down" and vol == "high":
            return "⚠️ 注意", f"下落×高ボラ (Vol={r['vol']:.2f}%)", "ポジション縮小を検討"
        return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "全相場で使用可能"


_STATUS_META = {
    "✅ 推奨": ("推奨", "#4ade80", "#052e16", "#166534"),
    "⚠️ 注意": ("注意", "#fbbf24", "#2d1f00", "#92400e"),
    "❌ 停止": ("停止", "#f87171", "#2d0a0a", "#991b1b"),
}
_RISK_COLOR = {"高": "#f87171", "中高": "#fb923c", "中": "#fbbf24", "低中": "#86efac", "低": "#4ade80"}


def _2cfg_section_html(r: dict) -> str:
    """t1タブに注入する2設定評価カードセクション。"""
    holdout_end = TODAY - timedelta(days=_HOLDOUT_N) if _HOLDOUT_N > 0 else TODAY
    cards = ""
    for cfg in CONFIGS:
        status, reason, advice = _judge_config(cfg, r)
        lbl_ja, fg, bg, border = _STATUS_META[status]
        label  = cfg["label"]
        color  = cfg["color"]
        mode   = cfg["mode"]
        risk   = "低中" if mode == "conservative" else "中"
        rc     = _RISK_COLOR[risk]
        n_stop = len(cfg["stop_wl"])
        n_brk  = len(cfg["brk_wl"])
        note = (
            f"holdout{_HOLDOUT_N}d WFスキャン ({_CSV_DATE_CON}) × conservative。"
            f"{holdout_end}以前のデータで選定。"
            if mode == "conservative" else
            f"holdout{_HOLDOUT_N}d WFスキャン ({_CSV_DATE_AGG}) × aggressive。"
            f"conservative と異なる銘柄構成。"
        )
        cards += f"""
<div class="script-card" style="border-color:{border};background:{bg}">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span class="badge" style="background:{border};color:{fg}">{lbl_ja}</span>
    <span style="font-weight:700;font-size:1.05rem;color:{color}">{label}</span>
    <span style="color:#64748b;font-size:0.8rem">{mode} / 逆指値B:{n_stop}件 BRK:{n_brk}件</span>
    <span style="margin-left:auto;font-size:0.78rem;color:{rc}">リスク: {risk}</span>
  </div>
  <div style="color:#94a3b8;font-size:0.82rem;margin-top:8px">{reason}</div>
  <div style="color:#64748b;font-size:0.78rem;margin-top:4px">→ {advice}</div>
  <div style="color:#475569;font-size:0.75rem;margin-top:6px;border-top:1px solid #1e293b;padding-top:6px">{note}</div>
</div>"""
    return f"\n<h2>2設定シグナル判定 (holdout{_HOLDOUT_N}d)</h2>\n{cards}\n"


# ══════════════════════════════════════════════════════════════════════════════
# 8. トレンド×相性バックテスト
# ══════════════════════════════════════════════════════════════════════════════

def _set_params(mode: str) -> None:
    if mode == "conservative":
        _stop.STRATEGY_PARAMS.update(_CON_STOP)
        _brk.STRATEGY_PARAMS.update(_CON_BRK)
    else:
        _stop.STRATEGY_PARAMS.update(_AGG_STOP)
        _brk.STRATEGY_PARAMS.update(_AGG_BRK)


def _run_backtests(workers: int) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for cfg in CONFIGS:
        _set_params(cfg["mode"])
        trades: list[dict] = []
        with _TPE(max_workers=workers) as ex:
            futs: dict = {}
            for sym, name, strat in cfg["stop_wl"]:
                futs[ex.submit(_stop.backtest_one, sym, name, strat)] = None
            for sym, name, strat in cfg["brk_wl"]:
                futs[ex.submit(_brk.backtest_one, sym, name, strat)] = None
            for fut in _asc(futs):
                try:
                    r = fut.result()
                    if not r:
                        continue
                    period_results = r.get("period_results", {})
                    if not period_results:
                        continue
                    max_period = max(period_results.keys())
                    for t in period_results[max_period].get("trade_log", []):
                        if t.get("reason") in (None, "発注中"):
                            continue
                        entry_dt = t.get("entry_dt")
                        if entry_dt is None:
                            continue
                        trades.append({"entry_dt": entry_dt, "pnl": t.get("pnl", 0)})
                except Exception:
                    pass
        result[cfg["label"]] = trades
    _set_params("conservative")
    return result


def _add_nikkei_trend(trades_by_cfg: dict[str, list[dict]],
                      nk_trend: pd.Series) -> None:
    idx = nk_trend.index
    for trades in trades_by_cfg.values():
        for t in trades:
            ts  = pd.Timestamp(t["entry_dt"]).normalize()
            pos = idx.searchsorted(ts, side="right") - 1
            t["nk_trend"] = nk_trend.iloc[pos] if 0 <= pos < len(nk_trend) else "sideways"


# ══════════════════════════════════════════════════════════════════════════════
# 9. トレンド×相性 HTML
# ══════════════════════════════════════════════════════════════════════════════

def _tab7_trend_html(trades_by_cfg: dict[str, list[dict]]) -> str:
    TREND_LABELS = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}
    TREND_COLORS = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}

    def _agg(trades):
        n = len(trades)
        if n == 0:
            return None
        wins  = [t for t in trades if t["pnl"] > 0]
        loses = [t for t in trades if t["pnl"] < 0]
        gp    = sum(t["pnl"] for t in wins)
        gl    = abs(sum(t["pnl"] for t in loses))
        pf    = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
        avg   = sum(t["pnl"] for t in trades) / n
        wr    = len(wins) / n * 100
        return {"n": n, "wr": wr, "pf": pf, "pf_s": pf_s, "avg": avg}

    def _cell(agg):
        if agg is None:
            return '<td style="color:#475569;text-align:center">—</td>'
        wr_c  = "#4ade80" if agg["wr"] >= 55 else ("#fbbf24" if agg["wr"] >= 45 else "#f87171")
        pf_c  = "#4ade80" if agg["pf"] >= 1.5 else ("#fbbf24" if agg["pf"] >= 1.0 else "#f87171")
        avg_c = "#4ade80" if agg["avg"] >= 0 else "#f87171"
        return (
            f'<td style="text-align:center;padding:8px 10px">'
            f'<div style="font-size:0.75rem;color:#94a3b8">{agg["n"]}取引</div>'
            f'<div><span style="color:{wr_c};font-weight:700">{agg["wr"]:.0f}%</span>'
            f'<span style="font-size:0.72rem;color:#64748b"> 勝率</span></div>'
            f'<div><span style="color:{pf_c};font-weight:700">PF {agg["pf_s"]}</span></div>'
            f'<div style="color:{avg_c};font-size:0.78rem">{agg["avg"]:+,.0f}円/取引</div>'
            f'</td>'
        )

    rows = ""
    for trend in ["up", "sideways", "down"]:
        tc  = TREND_COLORS[trend]
        tl  = TREND_LABELS[trend]
        row = (f'<tr><td style="color:{tc};font-weight:700;white-space:nowrap;'
               f'padding:8px 12px">{tl}</td>')
        for cfg in CONFIGS:
            t_list = [t for t in trades_by_cfg.get(cfg["label"], [])
                      if t.get("nk_trend") == trend]
            row += _cell(_agg(t_list))
        row += "</tr>"
        rows += row

    total_row = ('<tr style="border-top:2px solid #334155">'
                 '<td style="color:#94a3b8;padding:8px 12px;font-weight:700">全期間合計</td>')
    for cfg in CONFIGS:
        total_row += _cell(_agg(trades_by_cfg.get(cfg["label"], [])))
    total_row += "</tr>"

    headers = "".join(
        f'<th style="color:{cfg["color"]};padding:10px 12px">{cfg["label"]}</th>'
        for cfg in CONFIGS
    )

    def _breakdown_cards():
        TREND_JA = {"up": "上昇", "down": "下落", "sideways": "横ばい"}
        cards = ""
        for cfg in CONFIGS:
            trades = trades_by_cfg.get(cfg["label"], [])
            if not trades:
                continue
            by_t  = defaultdict(list)
            for t in trades:
                by_t[t.get("nk_trend", "sideways")].append(t)
            total = len(trades)
            bars  = ""
            for trend in ["up", "sideways", "down"]:
                n    = len(by_t[trend])
                pct  = n / total * 100 if total else 0
                wins = sum(1 for t in by_t[trend] if t["pnl"] > 0)
                wr   = wins / n * 100 if n else 0
                tc   = TREND_COLORS[trend]
                bars += f"""
<div style="margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;margin-bottom:4px">
    <span style="color:{tc};font-size:0.82rem;font-weight:600">{TREND_JA[trend]}</span>
    <span style="font-size:0.78rem;color:#94a3b8">{n}件 ({pct:.0f}%) / 勝率{wr:.0f}%</span>
  </div>
  <div style="background:#1e293b;border-radius:4px;height:8px;overflow:hidden">
    <div style="background:{tc};height:100%;width:{pct:.1f}%;border-radius:4px"></div>
  </div>
</div>"""
            cards += f"""
<div style="background:#0d1424;border:1px solid #1e3a5f;border-radius:10px;
            padding:16px;flex:1;min-width:220px">
  <div style="color:{cfg['color']};font-weight:700;margin-bottom:12px">{cfg['label']}</div>
  <div style="font-size:0.75rem;color:#64748b;margin-bottom:8px">合計 {total}取引</div>
  {bars}
</div>"""
        return f'<div style="display:flex;flex-wrap:wrap;gap:12px">{cards}</div>'

    return f"""
<h2>トレンド × 設定相性</h2>
<p style="color:#94a3b8;font-size:0.85rem">
  各セルは日経平均が「そのトレンド」だった日にエントリーしたトレードの成績。
  <span style="color:#4ade80">緑</span>: 勝率55%以上 / PF1.5以上、
  <span style="color:#fbbf24">黄</span>: 勝率45〜55% / PF1〜1.5、
  <span style="color:#f87171">赤</span>: それ以下。
</p>
<div style="overflow-x:auto">
<table>
  <thead>
    <tr>
      <th style="text-align:left;padding:10px 12px">日経トレンド</th>
      {headers}
    </tr>
  </thead>
  <tbody>{rows}{total_row}</tbody>
</table>
</div>
<h2>設定別 トレンド構成比</h2>
{_breakdown_cards()}"""


# ══════════════════════════════════════════════════════════════════════════════
# 10. HTML 注入
# ══════════════════════════════════════════════════════════════════════════════

def _inject_tabs(html: str, cfg_section_html: str, tab7_html: str) -> str:
    # t1 に2設定セクションを注入
    html = _re.sub(
        r'(<h2>[^<]*時点の推奨コマンド)',
        cfg_section_html.replace('\\', '\\\\') + r'\1',
        html, count=1,
    )
    # tab-nav に t7 ボタンを追加
    TAB_NAV_END = '\n</div>\n\n<div id="t1"'
    if TAB_NAV_END in html:
        new_btn = '\n  <button class="tab-btn" data-tab="t7" onclick="switchTab(\'t7\')">📊 トレンド×相性</button>'
        html = html.replace(TAB_NAV_END, new_btn + TAB_NAV_END, 1)
    # t7 ペインを <script> 直前に追加
    SCRIPT_TAG = '\n\n<script>'
    if SCRIPT_TAG in html:
        html = html.replace(
            SCRIPT_TAG,
            f'\n<div id="t7" class="tab-pane">{tab7_html}</div>' + SCRIPT_TAG, 1
        )
    # タブが2行にならないよう CSS 修正
    css_fix = (
        '\n<style>'
        '\n.tab-nav { flex-wrap: nowrap !important; overflow-x: auto; -webkit-overflow-scrolling: touch; }'
        '\n.tab-btn { padding: 7px 13px !important; font-size: 0.8rem !important; white-space: nowrap; }'
        '\n</style>'
    )
    html = html.replace('</head>', css_fix + '\n</head>', 1)
    return html


# ══════════════════════════════════════════════════════════════════════════════
# 11. エントリーポイント
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    try:
        from backtest_limit_entry import WORKERS as _def_w
    except ImportError:
        _def_w = 4

    _orig_argv = list(sys.argv)

    # nikkei_analysis.py に渡す argv を組み立て
    sys.argv = [sys.argv[0]] + _na_argv
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")

    _holdout_end = TODAY - timedelta(days=_HOLDOUT_N) if _HOLDOUT_N > 0 else TODAY

    print("=" * 70)
    print(f"nikkei_analysis_holdout_2config: 2設定分析レポート生成")
    print(f"  ホールドアウト  : {_holdout_end} 以降を除外 ({_HOLDOUT_N}日)")
    print(f"  conservative    : 逆指値B {len(STOP_CON)}件 / BRK {len(BRK_CON)}件")
    print(f"  aggressive      : 逆指値B {len(STOP_AGG)}件 / BRK {len(BRK_AGG)}件")
    print("=" * 70)

    _na.main()
    sys.argv[:] = _orig_argv

    date_str = _pre_known.date if _pre_known.date else str(TODAY)
    old_path = Path(f"nikkei_analysis_{date_str}.html")
    if not old_path.exists():
        print(f"[WARN] {old_path} が見つかりません")
        return

    base_html = old_path.read_text(encoding="utf-8")

    # トレンド×相性タブ生成
    print(f"\nトレンド×相性バックテスト実行中 (workers={_pre_known.workers})...", flush=True)
    try:
        close    = _na.fetch_n225(_pre_known.__dict__.get("years", 5))
        r        = _na.get_regime(close)
        nk_trend = _na.label_trend(close)
    except Exception as e:
        print(f"[WARN] 日経データ取得失敗 ({e})")
        close, r, nk_trend = None, None, None

    if r is not None:
        trades_by_cfg = _run_backtests(_pre_known.workers)
        _add_nikkei_trend(trades_by_cfg, nk_trend)
        cfg_section = _2cfg_section_html(r)
        tab7_html   = _tab7_trend_html(trades_by_cfg)
    else:
        cfg_section = ""
        tab7_html   = "<p style='color:#64748b;padding:20px'>データ取得失敗のためスキップ</p>"

    new_html = _inject_tabs(base_html, cfg_section, tab7_html)
    new_path = Path(f"nikkei_analysis_holdout2cfg_{_HOLDOUT_N}d_{date_str}.html")
    old_path.replace(new_path) if new_html == base_html else None
    new_path.write_text(new_html, encoding="utf-8")
    if old_path.exists() and old_path != new_path:
        old_path.unlink(missing_ok=True)

    print(f"\nレポート生成完了: {new_path.resolve()}")
    if not _pre_known.no_browser:
        webbrowser.open(new_path.resolve().as_uri())


if __name__ == "__main__":
    main()
