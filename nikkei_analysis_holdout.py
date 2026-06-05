"""
nikkei_analysis_holdout.py  ―  ホールドアウトWF選定WATCHLISTで日経分析

scan_walkforward.py --holdout-days N で生成した CSV を読み込み、
フィルタリング・ランキングした銘柄リストで nikkei_analysis.py と
同じ分析を実行する (monkey-patch 方式)。

使い方:
  python nikkei_analysis_holdout.py                      # 最新のホールドアウトCSVを自動検出
  python nikkei_analysis_holdout.py --holdout-days 65   # 65日ホールドアウトCSVを使用
  python nikkei_analysis_holdout.py --per-strategy 8    # 戦略ごとの選定数 (デフォルト10)
  python nikkei_analysis_holdout.py --max-dd 10         # MaxDD上限10% (デフォルト15)
  python nikkei_analysis_holdout.py --days 65           # 直近65日の損益表示
  python nikkei_analysis_holdout.py --no-browser        # HTML生成のみ
  python nikkei_analysis_holdout.py --aggressive        # aggressiveモードで実行

流れ:
  1. walkforward_results/ のホールドアウトCSVを読み込む
  2. フィルタリング (MaxDD / 連敗 / Sharpe / PnL) で銘柄を絞る
  3. composite_score = total_test_pnl × (1 + sharpe) でランキング
  4. 上位N銘柄を WATCHLIST に設定して nikkei_analysis.py を実行
  5. 出力: nikkei_analysis_holdout{N}d_{date}.html
"""
from __future__ import annotations

import argparse
import csv
import importlib as _importlib
import os
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# ── 1. TRADING_MODE を import 前に設定 ────────────────────────────────────────
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

# ── 2. シグナルモジュールを先にロード ──────────────────────────────────────────
import check_signals_stop as _stop
import check_signals_breakout as _brk
from compute_wf_scores import calc_wf_score, wf_rank


# ── 3. ヘルパー関数 ────────────────────────────────────────────────────────────

def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _composite_score(r: dict) -> float:
    pnl    = _float(r.get("total_test_pnl", 0))
    sharpe = _float(r.get("sharpe", 0))
    return pnl * (1.0 + max(sharpe, 0.0))


def _find_holdout_csv(strategy: str, holdout_days: int, wf_dir: Path) -> Path | None:
    """ホールドアウトCSVを検索して返す。見つからなければ None。"""
    if holdout_days > 0:
        suffix = f"_holdout{holdout_days}d"
        # 今日の日付から探す → 日付違いも glob でカバー
        candidates = sorted(wf_dir.glob(f"walkforward_{strategy}{suffix}_*.csv"), reverse=True)
    else:
        # 自動検出: _holdout*d_ パターンの最新CSV
        candidates = sorted(wf_dir.glob(f"walkforward_{strategy}_holdout*d_*.csv"), reverse=True)
    return candidates[0] if candidates else None


def load_holdout_watchlists(
    holdout_days: int,
    per_strategy: int,
    max_dd: float,
    max_consec: int,
    min_sharpe: float,
    min_folds: int,
    wf_dir: Path,
) -> tuple[list[tuple], list[tuple], int, str]:
    """
    ホールドアウトCSVを読み込み、STOP/BRK watchlist を返す。
    Returns: (stop_wl, brk_wl, detected_holdout_days, csv_date_str)
    """
    strategies_stop = ["MACD", "A7", "RSI2"]
    strategies_brk  = ["DON", "VOL", "MOM"]

    stop_wl: list[tuple[str, str, str]] = []
    brk_wl:  list[tuple[str, str, str]] = []
    detected_n = holdout_days
    csv_date   = str(TODAY)

    for strats, target_list in [(strategies_stop, stop_wl), (strategies_brk, brk_wl)]:
        for strategy in strats:
            csv_path = _find_holdout_csv(strategy, holdout_days, wf_dir)
            if csv_path is None:
                print(f"[WARN] ホールドアウトCSV not found: {strategy} (holdout={holdout_days}d)")
                continue

            # ファイル名から holdout日数と日付を抽出
            stem = csv_path.stem  # e.g. walkforward_MACD_holdout65d_2026-06-05
            parts = stem.split("_")
            csv_date = parts[-1]
            for p in parts:
                if p.startswith("holdout") and p.endswith("d"):
                    try:
                        detected_n = int(p[7:-1])
                    except ValueError:
                        pass

            with open(csv_path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            # フィルタリング
            filtered = [r for r in rows if (
                _int(r.get("folds_passed", 0))          >= min_folds
                and _float(r.get("max_drawdown_pct", 999)) <= max_dd
                and _int(r.get("max_consecutive_losses", 999)) <= max_consec
                and _float(r.get("sharpe", 0))           >= min_sharpe
                and _float(r.get("total_test_pnl", 0))    > 0
            )]
            filtered.sort(key=_composite_score, reverse=True)

            for r in filtered[:per_strategy]:
                sym   = r.get("symbol", "")
                name  = r.get("name", "")
                strat = r.get("strategy", "")
                if sym and strat:
                    target_list.append((sym, name, strat))

    return stop_wl, brk_wl, detected_n, csv_date


def build_wf_scores_from_holdout(holdout_days: int, csv_date: str, wf_dir: Path) -> dict:
    """
    ホールドアウトCSVから WFスコア辞書を構築する。
    format: {"sym_strat": {"score": int, "rank": str, "wr": float, "pf": float, ...}}
    """
    strategies = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]
    scores: dict = {}

    for strategy in strategies:
        csv_path = _find_holdout_csv(strategy, holdout_days, wf_dir)
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


# ── 4. 引数を先読み ────────────────────────────────────────────────────────────
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--holdout-days",  type=int,   default=0)
_pre.add_argument("--per-strategy",  type=int,   default=10)
_pre.add_argument("--max-dd",        type=float, default=15.0)
_pre.add_argument("--max-consec",    type=int,   default=5)
_pre.add_argument("--min-sharpe",    type=float, default=0.0)
_pre.add_argument("--wf-dir",        type=Path,  default=Path("walkforward_results"))
_pre.add_argument("--no-browser",    action="store_true")
_pre.add_argument("--date",          type=str,   default=None)
_pre.add_argument("--aggressive",    action="store_true")

# _pre_known: このスクリプト固有の引数
# _na_argv  : nikkei_analysis.py に渡す残りの引数
_pre_known, _na_argv = _pre.parse_known_args()
_wf_dir = _pre_known.wf_dir


# ── 5. ホールドアウトWATCHLISTを構築 ──────────────────────────────────────────
HOLDOUT_STOP_WL, HOLDOUT_BRK_WL, _HOLDOUT_N, _CSV_DATE = load_holdout_watchlists(
    holdout_days = _pre_known.holdout_days,
    per_strategy = _pre_known.per_strategy,
    max_dd       = _pre_known.max_dd,
    max_consec   = _pre_known.max_consec,
    min_sharpe   = _pre_known.min_sharpe,
    min_folds    = 2,
    wf_dir       = _wf_dir,
)

if not HOLDOUT_STOP_WL and not HOLDOUT_BRK_WL:
    print("[ERROR] ホールドアウトCSVから銘柄を1件も取得できませんでした。")
    print("        scan_walkforward.py --holdout-days N を先に実行してください。")
    sys.exit(1)

_WF_SCORES = build_wf_scores_from_holdout(_HOLDOUT_N, _CSV_DATE, _wf_dir)


# ── 6. WATCHLIST と WF スコアを適用 ──────────────────────────────────────────
_stop.WATCHLIST  = list(HOLDOUT_STOP_WL)
_brk.WATCHLIST   = list(HOLDOUT_BRK_WL)
_stop._WF_SCORES = dict(_WF_SCORES)
_brk._WF_SCORES  = dict(_WF_SCORES)


# ── 7. importlib.reload フック (reload 後もパッチを維持) ──────────────────────
_orig_reload = _importlib.reload


def _holdout_preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST  = list(HOLDOUT_STOP_WL)
        result._WF_SCORES = dict(_WF_SCORES)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST  = list(HOLDOUT_BRK_WL)
        result._WF_SCORES = dict(_WF_SCORES)
    return result


_importlib.reload = _holdout_preserving_reload


# ── 8. nikkei_analysis を import ──────────────────────────────────────────────
import nikkei_analysis as _na  # noqa: E402

# reload フックを元に戻す
_importlib.reload = _orig_reload


# ── 9. PNL タブのラベルと watchlist をホールドアウト用に更新 ────────────────────
_label = f"holdout{_HOLDOUT_N}d" if _HOLDOUT_N > 0 else "holdout"

for _cfg in _na._PNL_CONFIGS:
    if _cfg.get("label") == "既存版 conservative":
        _cfg["label"]   = f"{_label} conservative"
        _cfg["stop_wl"] = list(HOLDOUT_STOP_WL)
        _cfg["brk_wl"]  = list(HOLDOUT_BRK_WL)
    elif _cfg.get("label") == "既存版 aggressive":
        _cfg["label"]   = f"{_label} aggressive"
        _cfg["stop_wl"] = list(HOLDOUT_STOP_WL)
        _cfg["brk_wl"]  = list(HOLDOUT_BRK_WL)


# ═══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _orig_argv = list(sys.argv)

    # nikkei_analysis.py に渡す argv を組み立て
    sys.argv = [sys.argv[0]] + _na_argv

    # ブラウザ起動は自分でやる (リネーム後に開きたいため)
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")

    _holdout_end = TODAY - timedelta(days=_HOLDOUT_N) if _HOLDOUT_N > 0 else TODAY

    print("=" * 70)
    print(f"nikkei_analysis_holdout: ホールドアウトWF選定 WATCHLIST")
    print(f"  ホールドアウト   : {_holdout_end} 以降を除外 ({_HOLDOUT_N}日)")
    print(f"  CSVソース        : {_CSV_DATE} / holdout{_HOLDOUT_N}d")
    print(f"  逆指値B (STOP)   : {len(HOLDOUT_STOP_WL)} 銘柄×戦略")
    print(f"  BRK              : {len(HOLDOUT_BRK_WL)} 銘柄×戦略")
    print(f"  モード           : {os.environ.get('TRADING_MODE', 'conservative')}")
    print(f"  フィルター       : MaxDD<={_pre_known.max_dd}%  "
          f"連敗<={_pre_known.max_consec}  Sharpe>={_pre_known.min_sharpe}")
    print("=" * 70)

    _na.main()

    sys.argv[:] = _orig_argv

    # ── 出力ファイルをリネーム ──────────────────────────────────────────────
    date_str = _pre_known.date if _pre_known.date else str(TODAY)
    old_path = Path(f"nikkei_analysis_{date_str}.html")
    new_path = Path(f"nikkei_analysis_holdout{_HOLDOUT_N}d_{date_str}.html")

    if old_path.exists():
        old_path.replace(new_path)
        print(f"\nレポート生成完了: {new_path.resolve()}")
        if not _pre_known.no_browser:
            webbrowser.open(new_path.resolve().as_uri())
    else:
        print(f"[WARN] {old_path} が見つかりませんでした (--date 指定時は日付を確認)")


if __name__ == "__main__":
    main()
