"""
scan_walkforward_intraday.py ― デイトレ版 WalkForward 銘柄選定
================================================================
スイングの scan_walkforward.py をデイトレ(5分足)にそっくり移植したもの。

【考え方】 スイングと同一:
  - WalkForward: TRAIN(選定) で勝ち、TEST(擬似未来) でも勝つ銘柄を抽出
  - BTスコア: 勝率×0.4 + PF/10×30 + 安定性×20 + 取引数×10
  - ホールドアウト: 直近N日を選定から除外して OOS 検証
  - fold合格条件: TRAIN PF>=1.5/WR>=55%, TEST PF>=1.2/WR>=45%

【スイングとの違い】
  - 日足 → 5分足 (data/minute_5m, daytrade_data.load_intraday)
  - 戦略は既存の daytrade_*.py の backtest_symbol をそのまま流用
  - 各戦略は (symbol, name, df) を受け取り {trades:[{entry_dt, pnl, ...}]} を返す
  - run_limit_backtest を窓ごとに再実行する代わりに、戦略を1回走らせて
    全トレードを取得し、entry_dt で TRAIN/TEST 窓に振り分けるだけ (高速)

【使い方】
  python scan_walkforward_intraday.py                       # 全ローカル銘柄 × 全戦略
  python scan_walkforward_intraday.py --strategy ORB DON    # 戦略を絞る
  python scan_walkforward_intraday.py --limit 50            # 先頭50銘柄(デバッグ)
  python scan_walkforward_intraday.py --budget 600000       # 100株買える銘柄のみ
  python scan_walkforward_intraday.py --holdout-days 60     # 直近60日を選定から除外(OOS)
  python scan_walkforward_intraday.py --days 730 --workers 8
  # → walkforward_results/walkforward_intraday_<STRAT>_<date>.csv が出力される
"""
from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday, available_local_symbols

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()
OUT_DIR = Path(__file__).resolve().parent / "walkforward_results"

# ── 戦略レジストリ (既存 daytrade_*.py の backtest_symbol を流用) ───────────────
# すべて bt_fn(sym, name, df) -> {"trades": [{"entry_dt", "pnl", ...}]} | None
def _load_strategies() -> dict:
    reg: dict = {}
    _specs = [
        ("ORB",   "daytrade_orb",              "backtest_symbol"),
        ("DON",   "daytrade_donchian",         "backtest_symbol"),
        ("MACD",  "daytrade_macd_break",       "backtest_symbol"),
        ("STOCH", "daytrade_stoch_atr",        "backtest_symbol"),
        ("VOL",   "daytrade_volsurge",         "backtest_symbol"),
        ("GAP",   "daytrade_gap_reversal",     "backtest_symbol"),
        ("VWAP",  "daytrade_vwap_rebound",     "backtest_symbol"),
        ("MOM",   "daytrade_opening_momentum", "backtest_symbol"),
    ]
    import importlib
    for key, mod_name, fn_name in _specs:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                reg[key] = fn
        except Exception as e:
            print(f"  [warn] 戦略 {key} ({mod_name}) 読込失敗: {e}")
    return reg


# ── WalkForward fold 定義 (days ago from asof) ────────────────────────────────
# スイングと同じ: TRAIN 約12ヶ月 / TEST 約6ヶ月 の非重複ウィンドウ
#   (name, train_start, train_end, test_start, test_end)   ※すべて「N日前」
FOLDS: list[tuple[str, int, int, int, int]] = [
    ("fold1", 730, 370, 370, 180),  # TRAIN -730〜-370 / TEST -370〜-180
    ("fold2", 550, 180, 180,   0),  # TRAIN -550〜-180 / TEST -180〜  0
]

# ── 合格閾値 (スイングと同一) ──────────────────────────────────────────────────
TRAIN_MIN_TRADES = 5
TRAIN_MIN_PF     = 1.5
TRAIN_MIN_WR     = 55.0
TEST_MIN_TRADES  = 2
TEST_MIN_PF      = 1.2
TEST_MIN_WR      = 45.0
MIN_FOLDS_PASS   = 2     # この数以上の fold で TRAIN+TEST 両方合格 → 選定


# ── 統計 ──────────────────────────────────────────────────────────────────────
def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "pf": 0.0, "total_pnl": 0.0}
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) <= 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "win_rate": len(wins) / n * 100,
            "pf": pf, "total_pnl": sum(t["pnl"] for t in trades)}


def _passes_train(s: dict) -> bool:
    return (s["n"] >= TRAIN_MIN_TRADES and s["pf"] >= TRAIN_MIN_PF
            and s["win_rate"] >= TRAIN_MIN_WR and s["total_pnl"] > 0)


def _passes_test(s: dict) -> bool:
    return (s["n"] >= TEST_MIN_TRADES and s["pf"] >= TEST_MIN_PF
            and s["win_rate"] >= TEST_MIN_WR and s["total_pnl"] > 0)


def _trade_date(t: dict):
    """トレードのエントリー日 (date) を取り出す。"""
    dt = t.get("entry_dt") or t.get("exit_dt")
    if dt is None:
        return None
    return dt.date() if hasattr(dt, "date") else dt


def _window(trades: list[dict], asof, start_days: int, end_days: int) -> list[dict]:
    """entry_dt が [asof-start_days, asof-end_days) の範囲のトレードを返す。"""
    lo = asof - timedelta(days=start_days)
    hi = asof - timedelta(days=end_days)
    out = []
    for t in trades:
        d = _trade_date(t)
        if d is not None and lo <= d < hi:
            out.append(t)
    return out


# ── BTスコア (スイング check_signals_stop.calc_recommend_score を移植) ──────────
# 取引数は重複窓を足さず実数(最長窓=180日)で数える (スイングのバグ修正を継承)
def calc_bt_score(trades: list[dict], asof) -> tuple[int, str]:
    """直近365日を 30/60/90/120/150/180日 にスライスして BTスコアを算出。"""
    period_results = {}
    for days in (30, 60, 90, 120, 150, 180):
        sub = _window(trades, asof, days, 0)
        if sub:
            period_results[days] = _stats(sub)
    results = [r for r in period_results.values() if r and r["n"] > 0]
    if not results:
        return 0, "-"
    avg_wr = sum(r["win_rate"] for r in results) / len(results)
    avg_pf = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10)
                 for r in results) / len(results)
    stable = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades = max((r["n"] for r in results), default=0)   # 実数(重複なし)
    score = round(avg_wr * 0.4 + (avg_pf / 10) * 30
                  + stable * 20 + min(t_trades / 20, 1) * 10)
    rank = ("★★★" if score >= 80 else "★★" if score >= 60
            else "★" if score >= 40 else "△")
    return score, rank


# ── 1銘柄1戦略の WalkForward ──────────────────────────────────────────────────
def walkforward_one(sym: str, name: str, strat: str, bt_fn,
                    df: pd.DataFrame, holdout_days: int) -> dict | None:
    """戦略を1回走らせ、トレードを fold 窓に振り分けて WF 判定。"""
    try:
        res = bt_fn(sym, name, df)
    except Exception as e:
        return {"symbol": sym, "name": name, "strategy": strat, "error": str(e)}
    trades = (res or {}).get("trades", []) if isinstance(res, dict) else []
    if not trades:
        return None

    # ホールドアウト: 直近 holdout_days を選定から外す = asof を遡らせる
    asof = TODAY - timedelta(days=holdout_days)

    folds_passed = 0
    fold_detail = []
    for fn, ts, te, vs, ve in FOLDS:
        tr = _stats(_window(trades, asof, ts, te))
        va = _stats(_window(trades, asof, vs, ve))
        pt, pv = _passes_train(tr), _passes_test(va)
        if pt and pv:
            folds_passed += 1
        fold_detail.append((fn, tr, va, pt, pv))

    # BTスコア (ホールドアウト end までのトレードで算出)
    bt_trades = [t for t in trades if (_trade_date(t) or asof) <= asof]
    bt_score, bt_rank = calc_bt_score(bt_trades, asof)

    # 全体統計 (テスト期間トータル) と最新価格
    test_total = _stats([t for t in trades
                         if (_trade_date(t) or TODAY) >= asof - timedelta(days=180)])
    try:
        latest_price = float(df.iloc[-1]["close"])
    except Exception:
        latest_price = 0.0

    return {
        "symbol": sym, "name": name, "strategy": strat,
        "folds_passed": folds_passed,
        "bt_score": bt_score, "bt_rank": bt_rank,
        "n_trades": len(trades),
        "test_trades": test_total["n"],
        "test_pf": round(test_total["pf"], 2) if test_total["pf"] != float("inf") else 99.9,
        "test_wr": round(test_total["win_rate"], 1),
        "test_pnl": round(test_total["total_pnl"]),
        "latest_price": round(latest_price, 1),
    }


# ── メイン ────────────────────────────────────────────────────────────────────
def _worker(task):
    sym, name, df, strategies, holdout = task
    rows = []
    for strat, bt_fn in strategies.items():
        r = walkforward_one(sym, name, strat, bt_fn, df, holdout)
        if r and "error" not in r:
            rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="デイトレ版 WalkForward 銘柄選定 (5分足)")
    ap.add_argument("--strategy", nargs="*", default=None,
                    help="対象戦略 (省略=全戦略)。例: --strategy ORB DON MACD")
    ap.add_argument("--days", type=int, default=730, help="読み込む分足日数 (既定730=2年)")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄のみ (デバッグ)")
    ap.add_argument("--budget", type=int, default=0, help="この金額で100株買える銘柄のみ")
    ap.add_argument("--max-price", type=float, default=0.0, help="株価上限 (budgetと同義)")
    ap.add_argument("--holdout-days", type=int, default=0,
                    help="直近N日を選定から除外 (OOS検証用)")
    ap.add_argument("--min-folds", type=int, default=MIN_FOLDS_PASS,
                    help=f"選定に必要な合格fold数 (既定{MIN_FOLDS_PASS})")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--source", default="local", help="local / yfinance / auto")
    args = ap.parse_args()

    strategies = _load_strategies()
    if args.strategy:
        strategies = {k: v for k, v in strategies.items()
                      if k in {s.upper() for s in args.strategy}}
    if not strategies:
        print("[ERROR] 有効な戦略がありません"); return 1
    print(f"対象戦略: {', '.join(strategies)}")

    max_price = args.max_price or (args.budget / 100 if args.budget else 0.0)

    syms = available_local_symbols()
    if not syms:
        print("[ERROR] data/minute_5m に銘柄データがありません。"
              "download_all_minute.py で取得してください。"); return 1
    # available_local_symbols は J-Quants コード(例 72030)。.T 形式へ戻す。
    def _to_yf(code: str) -> str:
        c = str(code)
        return (c[:-1] + ".T") if (c.endswith("0") and len(c) == 5) else (c + ".T")
    universe = [(_to_yf(c), _to_yf(c)) for c in syms]
    if args.limit:
        universe = universe[:args.limit]
    print(f"ユニバース: {len(universe)} 銘柄 / holdout={args.holdout_days}日 / "
          f"max_price={max_price or '無制限'}")

    # ── 各銘柄: 分足を1回ロード → 全戦略を実行 ──
    tasks = []
    print("分足ロード中...", flush=True)
    for i, (sym, name) in enumerate(universe):
        try:
            df = load_intraday(sym, days=args.days, source=args.source)
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        if max_price > 0:
            try:
                if float(df.iloc[-1]["close"]) > max_price:
                    continue
            except Exception:
                pass
        tasks.append((sym, name, df, strategies, args.holdout_days))
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(universe)} ロード済み", flush=True)
    print(f"バックテスト対象: {len(tasks)} 銘柄", flush=True)

    all_rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_worker, t) for t in tasks]
        for fut in as_completed(futs):
            all_rows.extend(fut.result() or [])
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(tasks)} 完了", flush=True)

    # ── 戦略別に CSV 出力 ──
    OUT_DIR.mkdir(exist_ok=True)
    ho_suffix = f"_holdout{args.holdout_days}d" if args.holdout_days else ""
    cols = ["symbol", "name", "strategy", "folds_passed", "bt_score", "bt_rank",
            "n_trades", "test_trades", "test_pf", "test_wr", "test_pnl", "latest_price"]
    total_selected = 0
    for strat in strategies:
        rows = [r for r in all_rows if r["strategy"] == strat]
        rows.sort(key=lambda r: (-r["folds_passed"], -r["bt_score"]))
        path = OUT_DIR / f"walkforward_intraday_{strat}{ho_suffix}_{TODAY}.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in cols})
        selected = [r for r in rows if r["folds_passed"] >= args.min_folds]
        total_selected += len(selected)
        print(f"  [{strat}] {len(rows)}銘柄評価 → 選定 {len(selected)}銘柄 "
              f"(folds>={args.min_folds}) → {path.name}")
        for r in selected[:10]:
            print(f"      {r['symbol']} {r['name']} folds={r['folds_passed']} "
                  f"BT={r['bt_score']}{r['bt_rank']} test:PF{r['test_pf']}/"
                  f"WR{r['test_wr']}%/{r['test_trades']}件")

    print(f"\n完了: {len(tasks)}銘柄 × {len(strategies)}戦略 → 選定合計 {total_selected}件")
    print(f"出力先: {OUT_DIR}/walkforward_intraday_*_{TODAY}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
