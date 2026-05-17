"""
budget_sim.py ― 資金制約付き マルチスクリプト比較シミュレーター

各 run_signals*.py のシグナルを「スコア高い順・資金制約内」で投資した場合の
成績を比較し、最も利益が出るスクリプトを特定する。

前提:
  - 資金 = 現物額 × レバレッジ倍率
  - 1ポジション = signal_price × 100株（FIXED_QTY）
  - 同一日に複数シグナル → スコア降順で資金が許す限り約定
  - ポジションが閉じたら資金を解放して次のシグナルに使える

使い方:
  python budget_sim.py                         # デフォルト: 4.2M円, 365日
  python budget_sim.py --cash 2100000 --lev 2  # 現物210万×レバ2倍
  python budget_sim.py --days 90               # 直近90日
  python budget_sim.py --workers 4             # バックテスト並列数
  python budget_sim.py --scripts run_signals run_signals_prime  # 特定スクリプトのみ
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

SCRIPT_CONFIGS = [
    {"name": "run_signals",            "label": "① 標準(conservative)",       "color": ""},
    {"name": "run_signals_merged",     "label": "② 統合WF(conservative)",     "color": ""},
    {"name": "run_signals_aggressive", "label": "③ Aggressive WF",            "color": ""},
    {"name": "run_signals_prime",      "label": "④ Prime WF (株価制限なし)",  "color": ""},
    {"name": "run_signals_nolimit",    "label": "⑤ Nolimit WF (候補120銘柄)", "color": ""},
]


def run_worker(script_name: str, budget: float, days: int, workers: int) -> dict | None:
    cmd = [
        sys.executable, "_budget_worker.py",
        "--script",  script_name,
        "--budget",  str(int(budget)),
        "--days",    str(days),
        "--workers", str(workers),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            print(f"  [ERROR] {script_name}: {r.stderr[-300:]}", file=sys.stderr)
            return None
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] {script_name}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"  [JSON ERROR] {script_name}: {e}\n  stdout={r.stdout[:200]}", file=sys.stderr)
        return None


def _bar(value: float, max_val: float, width: int = 20) -> str:
    if max_val <= 0:
        return " " * width
    filled = int(width * min(value / max_val, 1.0))
    return "█" * filled + "░" * (width - filled)


def main() -> None:
    parser = argparse.ArgumentParser(description="資金制約付きシグナル比較シミュレーター")
    parser.add_argument("--cash",    type=float, default=2_100_000,
                        help="現物金額 (円, デフォルト 210万)")
    parser.add_argument("--lev",     type=float, default=2.0,
                        help="レバレッジ倍率 (デフォルト 2倍)")
    parser.add_argument("--budget",  type=float, default=None,
                        help="利用可能資金を直接指定 (--cash/--lev を上書き)")
    parser.add_argument("--days",    type=int,   default=365)
    parser.add_argument("--workers", type=int,   default=4)
    parser.add_argument("--scripts", nargs="+",
                        choices=[c["name"] for c in SCRIPT_CONFIGS],
                        default=None, help="比較するスクリプトを限定")
    args = parser.parse_args()

    budget = args.budget if args.budget is not None else args.cash * args.lev

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"\n{'='*80}")
    print(f"  資金制約付き シグナル比較シミュレーション")
    print(f"  日付 : {today_str}")
    print(f"  期間 : 過去 {args.days} 日")
    print(f"  資金 : {budget/10000:.0f}万円"
          + (f"  (現物{args.cash/10000:.0f}万 × レバ{args.lev}倍)"
             if args.budget is None else ""))
    print(f"  戦略 : スコア高い順に資金内で投資、決済後は資金復活")
    print(f"{'='*80}")

    target_scripts = (
        [c for c in SCRIPT_CONFIGS if c["name"] in args.scripts]
        if args.scripts else SCRIPT_CONFIGS
    )

    results: list[dict] = []
    for sc in target_scripts:
        print(f"\n  [{sc['label']}] 実行中...", flush=True)
        r = run_worker(sc["name"], budget, args.days, args.workers)
        if r is None:
            continue
        r["label"] = sc["label"]
        results.append(r)
        n_ex = r.get("n_executed", 0)
        n_cl = r.get("n_closed", 0)
        pnl  = r.get("total_pnl", 0)
        wr   = r.get("win_rate", 0)
        skip = r.get("n_skipped_budget", 0)
        skip_p = r.get("n_skipped_price", 0)
        print(f"    約定 {n_ex}件 (決済済 {n_cl}件) / 資金不足スキップ {skip}件"
              f" / 予算超過スキップ {skip_p}件")
        print(f"    PnL: {pnl:+,.0f}円  勝率: {wr:.1f}%")

    if not results:
        print("\n  結果なし (全スクリプトでエラー)")
        return

    # ── 比較テーブル ───────────────────────────────────────────────────────
    results.sort(key=lambda r: r.get("total_pnl", 0), reverse=True)
    max_pnl = max(abs(r.get("total_pnl", 0)) for r in results) or 1

    print(f"\n\n{'='*80}")
    print(f"  比較結果サマリー（過去{args.days}日・利用可能資金 {budget/10000:.0f}万円）")
    print(f"{'='*80}")
    print(f"  {'順位 スクリプト':<34} {'約定':>5} {'決済':>5} {'勝率':>6} "
          f"{'損益合計':>12} {'1件平均':>9}  損益バー")
    print(f"  {'-'*90}")

    medals = ["🥇", "🥈", "🥉"] + ["   "] * 10
    for i, r in enumerate(results):
        n_ex  = r.get("n_executed", 0)
        n_cl  = r.get("n_closed", 0)
        pnl   = r.get("total_pnl", 0)
        avg   = pnl / n_cl if n_cl > 0 else 0
        wr    = r.get("win_rate", 0)
        bar   = _bar(pnl, max_pnl) if pnl > 0 else _bar(abs(pnl), max_pnl)
        bar_s = f"[{bar}]" if pnl >= 0 else f"[{bar}] ▼"
        print(f"  {medals[i]} {r['label']:<31} {n_ex:>5} {n_cl:>5} {wr:>5.1f}% "
              f"{pnl:>12,.0f}円 {avg:>8,.0f}円  {bar_s}")

    # ── 資金制約で見逃した高スコアシグナル ─────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  資金不足でスキップした高スコアシグナル Top（各スクリプト）")
    print(f"{'='*80}")
    for r in results[:3]:  # 上位3スクリプト
        top = r.get("top_skipped", [])
        if not top:
            continue
        print(f"\n  ▶ {r['label']}")
        print(f"    {'銘柄コード':<12} {'銘柄名':<22} {'スコア':>5} {'必要資金':>10} {'約定日'}")
        for t in top:
            print(f"    {t['symbol']:<12} {t['name']:<22} {t['score']:>5.0f}点 "
                  f"{t['required']:>8,.0f}円  {t['entry_dt']}")

    # ── 結論 ───────────────────────────────────────────────────────────────
    best = results[0]
    worst = results[-1]
    print(f"\n{'='*80}")
    print(f"  ★ 結論（利用可能資金 {budget/10000:.0f}万円 / 過去{args.days}日）")
    print(f"{'='*80}")
    print(f"  最も利益が出るスクリプト: {best['label']}")
    print(f"    → PnL {best.get('total_pnl', 0):+,.0f}円"
          f"  (約定{best.get('n_executed',0)}件 / 勝率{best.get('win_rate',0):.1f}%)")

    if len(results) >= 2:
        diff = best.get("total_pnl", 0) - results[1].get("total_pnl", 0)
        print(f"    → 2位 [{results[1]['label']}] との差: {diff:+,.0f}円")

    # 予算超過銘柄の警告
    for r in results:
        np_ = r.get("n_skipped_price", 0)
        if np_ > 0:
            print(f"\n  ⚠ [{r['label']}]: {np_}件が1ポジション必要額 > {budget/10000:.0f}万円"
                  f" のため購入不可（run_signals.py など株価上限ありのスクリプトを推奨）")

    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
