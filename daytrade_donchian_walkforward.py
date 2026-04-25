"""
daytrade_donchian_walkforward.py  ―  Donchianのウォークフォワード検証
==================================================================
過去N日(訓練期)で勝ち銘柄を抽出 → 次のM日(検証期)でその銘柄群の
パフォーマンスを測定。これを時系列でローリング実施することで、
overfitting / survivorship bias を排除した「本当に効くか」を検証。

【ロジック】
  i = train_days
  while i + test_days <= 営業日数:
    train_dates = all_dates[i - train_days : i]
    test_dates  = all_dates[i : i + test_days]
    ① train_dates で全銘柄バックテスト
    ② PF/取引数/損益で勝ち銘柄を抽出
    ③ test_dates で勝ち銘柄のみバックテスト
    ④ test_dates の取引を集計
    i += test_days

  最終: 全テスト期間の取引を集計してPF/損益/DDを評価

【使い方】
  python daytrade_donchian_walkforward.py --universe n225 --days 60 \\
      --train-days 30 --test-days 15
  python daytrade_donchian_walkforward.py --universe n225 \\
      --target-preset balanced --min-pf 1.3
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats
from daytrade_donchian_compare import PRESETS, _load_universe, extract_winners

JST = timezone(timedelta(hours=9))
BUDGET = 600_000
MAX_RISK = 6_000


def _pf(v):
    return "∞" if v == float("inf") else f"{v:.2f}"


def slice_data_by_dates(fetched, allowed_dates):
    """allowed_dates のセットに含まれる日のデータだけ抽出。"""
    allowed = set(allowed_dates)
    out = {}
    for sym, df in fetched.items():
        mask = [d in allowed for d in df.index.date]
        sub = df[mask]
        if not sub.empty:
            out[sym] = sub
    return out


def run_pass(fetched, name_map, params, budget, max_risk, target_syms=None):
    """指定銘柄(target_syms=None なら全銘柄)でバックテスト → items 返却。"""
    items = []
    for sym, df in fetched.items():
        if target_syms is not None and sym not in target_syms:
            continue
        r = backtest_symbol(sym, name_map.get(sym, sym), df,
                            budget, max_risk, params=params)
        if r:
            items.append(r)
    return items


def run_walkforward(fetched, name_map, params, all_dates,
                    train_days, test_days,
                    budget, max_risk,
                    min_pf, min_trades, min_pnl):
    windows = []
    overall_test_trades = []

    i = train_days
    while i + test_days <= len(all_dates):
        train_dates = all_dates[i - train_days:i]
        test_dates = all_dates[i:i + test_days]

        # ① 訓練期: 全銘柄でバックテスト
        train_data = slice_data_by_dates(fetched, train_dates)
        train_items = run_pass(train_data, name_map, params, budget, max_risk)
        train_result = dict(items=train_items)

        # ② 勝ち銘柄抽出
        winners = extract_winners(train_result, budget, min_pf, min_trades, min_pnl)
        winner_syms = {s for s, _, _ in winners}

        # ③ 検証期: 勝ち銘柄のみでバックテスト
        test_data = slice_data_by_dates(fetched, test_dates)
        test_items = run_pass(test_data, name_map, params, budget, max_risk,
                              target_syms=winner_syms)
        test_trades = [t for it in test_items for t in it["trades"]]
        test_pnl = sum(t["pnl"] for t in test_trades)
        test_n = len(test_trades)
        test_wins = sum(1 for t in test_trades if t["pnl"] > 0)
        test_winrate = (test_wins / test_n * 100) if test_n > 0 else 0

        windows.append(dict(
            train_start=train_dates[0],
            train_end=train_dates[-1],
            test_start=test_dates[0],
            test_end=test_dates[-1],
            n_winners=len(winners),
            n_winners_traded=len(test_items),
            test_trades=test_n,
            test_winrate=test_winrate,
            test_pnl=test_pnl,
            winners=winners,
        ))
        overall_test_trades.extend(test_trades)
        i += test_days

    overall_stats = calc_stats(overall_test_trades, budget)
    return windows, overall_stats, overall_test_trades


def print_walkforward(windows, overall_stats, total_test_days, params, target_preset):
    print(f"\n{'='*110}")
    print(f"  ウォークフォワード検証 [preset={target_preset}]")
    print("=" * 110)
    print(f"{'#':>2} {'Train期間':<26} {'Test期間':<26} "
          f"{'勝銘柄':>6} {'取引':>5} {'勝率':>6} {'損益':>10}")
    print("-" * 110)
    for idx, w in enumerate(windows, 1):
        train_str = f"{w['train_start']}〜{w['train_end']}"
        test_str = f"{w['test_start']}〜{w['test_end']}"
        wr = f"{w['test_winrate']:.1f}%" if w["test_trades"] > 0 else "-"
        print(f"{idx:>2} {train_str:<26} {test_str:<26} "
              f"{w['n_winners']:>4}→{w['n_winners_traded']:<2} "
              f"{w['test_trades']:>5} {wr:>6} {w['test_pnl']:>+10,.0f}")
    print("-" * 110)

    s = overall_stats
    print(f"\n  【全テスト期間集計 ({total_test_days}営業日)】")
    print(f"  取引: {s['n']}")
    print(f"  勝率: {s['win_rate']:.1f}%")
    print(f"  PF:   {_pf(s['pf'])}")
    print(f"  損益: {s['total_pnl']:+,.0f}円")
    print(f"  DD:   {s['max_dd']:+.1f}%")
    print(f"  平均利益: {s['avg_win']:+,.0f}円  /  平均損失: {s['avg_loss']:+,.0f}円")
    print("=" * 110)

    # 勝率/PFの判定
    if s["pf"] >= 1.5:
        print("\n  ✓ ウォークフォワードでも PF ≥ 1.5 を達成。本番運用候補。")
    elif s["pf"] >= 1.0:
        print("\n  △ ウォークフォワードで PF ≥ 1.0 だが余裕は少ない。"
              "実費用(手数料/スプレッド)で負けに転じる可能性あり。")
    else:
        print("\n  ✗ ウォークフォワードで PF < 1.0。"
              "過去結果は overfitting だった可能性が高い。")


def main():
    parser = argparse.ArgumentParser(description="Donchian ウォークフォワード検証")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"], default="auto")
    parser.add_argument("--universe", choices=["watch", "n225"], default="n225")
    parser.add_argument("--target-preset", default="ultra_freq",
                        choices=list(PRESETS.keys()))
    parser.add_argument("--train-days", type=int, default=30,
                        help="訓練期間(日数, デフォルト: 30)")
    parser.add_argument("--test-days", type=int, default=15,
                        help="検証期間(日数, デフォルト: 15)")
    parser.add_argument("--min-pf", type=float, default=1.5)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--min-pnl", type=int, default=0)
    args = parser.parse_args()

    targets = _load_universe(args.universe)
    symbols = [s for s, _ in targets]
    print(f"ウォークフォワード検証 [universe={args.universe} / "
          f"preset={args.target_preset} / "
          f"train={args.train_days}日 / test={args.test_days}日]: "
          f"{len(targets)}銘柄 / {args.days}日", flush=True)

    fetched = load_intraday_batch(symbols, args.days, source=args.source)
    max_price = args.budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    name_map = dict(targets)
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    all_dates = sorted({d for df in fetched.values() for d in df.index.date})
    print(f"  営業日: {len(all_dates)}日 ({all_dates[0]} 〜 {all_dates[-1]})", flush=True)

    if len(all_dates) < args.train_days + args.test_days:
        raise SystemExit(
            f"営業日が不足: {len(all_dates)}日 < "
            f"train({args.train_days}) + test({args.test_days})"
        )

    params = PRESETS[args.target_preset]
    print(f"\n  抽出基準: PF≥{args.min_pf} / 取引≥{args.min_trades} / 損益≥{args.min_pnl:,}\n",
          flush=True)

    windows, overall_stats, all_test_trades = run_walkforward(
        fetched, name_map, params, all_dates,
        args.train_days, args.test_days,
        args.budget, MAX_RISK,
        args.min_pf, args.min_trades, args.min_pnl,
    )

    total_test_days = sum(args.test_days for _ in windows)
    print_walkforward(windows, overall_stats, total_test_days, params, args.target_preset)


if __name__ == "__main__":
    main()
