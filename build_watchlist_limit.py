"""
build_watchlist_limit.py  ―  指値エントリー戦略 (MACD/A7/RSI2) 銘柄選定
=========================================================================
全銘柄を4期間(30/90/180/365日)でバックテストし、スコアで上位銘柄を選定。
結果はテキスト形式で出力（Claude等に貼り付けて選定依頼する用途向け）。

【使い方】
  python build_watchlist_limit.py               # 全戦略・全銘柄・上位30件
  python build_watchlist_limit.py --top 50      # 上位50件
  python build_watchlist_limit.py --macd        # MACDのみ
  python build_watchlist_limit.py --a7          # A7のみ
  python build_watchlist_limit.py --rsi2        # RSI2のみ
  python build_watchlist_limit.py --workers 8   # 並列数
  python build_watchlist_limit.py --min-score 30  # スコア30以上のみ

【スコア計算】
  各期間: PF×10 + 勝率×0.5 + (取引数≥3なら+5) の加重平均
  重み: 30日×4 / 90日×3 / 180日×2 / 365日×1

【出力形式】テキスト（コンソール） → そのままClaude等に貼り付け可能
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# backtest_limit_entry から必要な関数・定数をインポート
from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)

JST              = timezone(timedelta(hours=9))
PERIODS          = [30, 90, 180, 365]
PERIOD_WEIGHTS   = {30: 4.0, 90: 3.0, 180: 2.0, 365: 1.0}
STRATEGY_DEFS    = {
    "MACD": (calc_macd, 0.0, 1.5, 3.0),
    "A7":   (calc_a7,   0.0, 1.5, 3.0),
    "RSI2": (calc_rsi2, 0.5, 2.0, 4.0),
}


def _load_symbols() -> list[tuple[str, str]]:
    p = Path("symbols_listed_all.py")
    if p.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("_listed_all", p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        syms = list(mod.SYMBOLS)
        print(f"  銘柄ユニバース: symbols_listed_all.py ({len(syms)}銘柄)")
        return syms
    from symbols_all import SYMBOLS as _S
    syms = list(_S)
    print(f"  銘柄ユニバース: symbols_all.py ({len(syms)}銘柄)")
    return syms


def _period_score(r: dict) -> float:
    """1期間の結果からスコアを計算。"""
    if r["trades"] < 2:
        return 0.0
    pf  = min(r["pf"], 5.0) if r["pf"] != float("inf") else 5.0
    wr  = r["win_rate"]
    tr  = r["trades"]
    bonus = 5.0 if tr >= 3 else 0.0
    return pf * 10.0 + wr * 0.5 + bonus


def _weighted_score(period_results: dict[int, dict]) -> float:
    """4期間の加重平均スコア。"""
    total_w = sum(PERIOD_WEIGHTS[p] for p in PERIODS if period_results.get(p))
    if total_w == 0:
        return 0.0
    score = sum(
        _period_score(period_results[p]) * PERIOD_WEIGHTS[p]
        for p in PERIODS if period_results.get(p)
    ) / total_w
    return round(score, 1)


def backtest_symbol(symbol: str, name: str,
                    calc_fn, entry_m: float, stop_m: float, target_m: float,
                    strategy: str) -> dict | None:
    """1銘柄×4期間バックテスト。最長期間のデータを1回取得して再利用。"""
    df = fetch(symbol, max(PERIODS))
    if df is None:
        return None

    period_results: dict[int, dict] = {}
    for days in PERIODS:
        r = run_limit_backtest(symbol, name, df, calc_fn,
                               entry_m, stop_m, target_m, days, strategy)
        if r and r["trades"] >= 1:
            period_results[days] = r

    if not period_results:
        return None

    score = _weighted_score(period_results)
    if score <= 0:
        return None

    # 最新期間(365日)の代表値
    rep = period_results.get(365) or period_results.get(180) or \
          period_results.get(90)  or period_results.get(30)

    return dict(
        symbol=symbol,
        name=name,
        strategy=strategy,
        score=score,
        period_results=period_results,
        # 代表値 (365日)
        trades=rep["trades"],
        wins=rep["wins"],
        win_rate=rep["win_rate"],
        pf=rep["pf"],
        total_pnl=rep["total_pnl"],
        fill_rate=rep["fill_rate"],
    )


def run_strategy(symbols: list[tuple[str, str]], strategy: str,
                 workers: int) -> list[dict]:
    calc_fn, em, sm, tm = STRATEGY_DEFS[strategy]
    results: list[dict] = []

    def _proc(sn):
        sym, name = sn
        return backtest_symbol(sym, name, calc_fn, em, sm, tm, strategy)

    print(f"\n[{strategy}] スキャン中... ({len(symbols)}銘柄)", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_proc, sn): sn for sn in symbols}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass
            if done % 100 == 0 or done == len(symbols):
                print(f"  {strategy}: {done}/{len(symbols)}", flush=True)

    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  => 有効銘柄: {len(results)}件")
    return results


def _pf_str(pf: float) -> str:
    if pf == float("inf"):
        return "  ∞  "
    return f"{pf:5.2f}"


def _period_cell(r: dict) -> str:
    if not r:
        return "  -    -      -     -"
    pf  = _pf_str(r["pf"])
    wr  = f"{r['win_rate']:5.1f}%"
    pnl = f"{r['total_pnl']:+8,.0f}"
    tr  = f"{r['trades']:3d}回"
    return f"{tr} {wr} PF{pf} {pnl}円"


def print_text_report(all_results: dict[str, list[dict]],
                      top_n: int, min_score: float) -> None:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 100)
    print(f"  指値エントリー バックテスト 銘柄選定結果  生成日: {today}")
    print(f"  4期間スコア: 30日×4 / 90日×3 / 180日×2 / 365日×1")
    print(f"  エントリー: MACD=終値指値 / A7=終値指値 / RSI2=終値-ATR×0.5")
    print(f"  損切: ATR×1.5(MACD/A7) / ATR×2.0(RSI2)  目標: ATR×3.0(MACD/A7) / ATR×4.0(RSI2)")
    print("=" * 100)

    for strategy, results in all_results.items():
        filtered = [r for r in results if r["score"] >= min_score][:top_n]
        print()
        print(f"━━━ {strategy} 戦略 上位{len(filtered)}銘柄 (スコア{min_score:.0f}以上) ━━━")
        print(f"{'順位':<4} {'銘柄コード':<10} {'銘柄名':<24} {'スコア':>6}  "
              f"{'30日':^32} {'90日':^32} {'180日':^32} {'365日':^32}")
        print("-" * 140)

        for i, r in enumerate(filtered, 1):
            pr = r["period_results"]
            c30  = _period_cell(pr.get(30,  {}))
            c90  = _period_cell(pr.get(90,  {}))
            c180 = _period_cell(pr.get(180, {}))
            c365 = _period_cell(pr.get(365, {}))
            print(f"{i:<4} {r['symbol']:<10} {r['name']:<24} {r['score']:>6.1f}  "
                  f"{c30}  {c90}  {c180}  {c365}")

    print()
    print("=" * 100)
    print("【貼り付け用メモ】")
    print("上記結果をもとに、監視銘柄を選定してください。")
    print("選定基準の参考: スコア35以上 / 365日勝率50%以上 / PF1.5以上 / 取引数5回以上")
    print("=" * 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="指値エントリー銘柄選定")
    parser.add_argument("--macd",      action="store_true")
    parser.add_argument("--a7",        action="store_true")
    parser.add_argument("--rsi2",      action="store_true")
    parser.add_argument("--top",       type=int,   default=30)
    parser.add_argument("--min-score", type=float, default=20.0)
    parser.add_argument("--workers",   type=int,   default=_DEFAULT_WORKERS)
    args = parser.parse_args()

    run_macd = args.macd or (not args.macd and not args.a7 and not args.rsi2)
    run_a7   = args.a7   or (not args.macd and not args.a7 and not args.rsi2)
    run_rsi2 = args.rsi2 or (not args.macd and not args.a7 and not args.rsi2)

    symbols = _load_symbols()
    workers = args.workers

    strategies_to_run = []
    if run_macd: strategies_to_run.append("MACD")
    if run_a7:   strategies_to_run.append("A7")
    if run_rsi2: strategies_to_run.append("RSI2")

    all_results: dict[str, list[dict]] = {}
    for strat in strategies_to_run:
        all_results[strat] = run_strategy(symbols, strat, workers)

    print_text_report(all_results, args.top, args.min_score)


if __name__ == "__main__":
    main()
