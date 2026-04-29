"""
4つの指値バックテストを一括実行する。

使い方:
  python run_backtest_all.py                    # 全上場銘柄（デフォルト）
  python run_backtest_all.py --universe 225     # 日経225
  python run_backtest_all.py --universe watch   # 監視銘柄
  python run_backtest_all.py --days 180         # バックテスト期間変更
  python run_backtest_all.py --no-browser       # ブラウザ自動起動なし
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = [
    "backtest_limit_oco.py",
    "backtest_adaptive_mr.py",
    "backtest_donchian_pullback.py",
    "backtest_bb_volume.py",
]

LABELS = [
    "指値OCO",
    "アダプティブ平均回帰",
    "ドンチャン・ブレイクアウト",
    "BB下限＋出来高急増",
]


def main():
    parser = argparse.ArgumentParser(description="4つの指値バックテストを一括実行")
    parser.add_argument("--universe", default="all", choices=["watch", "225", "all"],
                        help="銘柄ユニバース: watch / 225 / all（デフォルト: all）")
    parser.add_argument("--days", type=int, default=None,
                        help="バックテスト期間（日）")
    parser.add_argument("--no-browser", action="store_true",
                        help="ブラウザを自動で開かない")
    args = parser.parse_args()

    base_cmd = [sys.executable]
    extra = ["--universe", args.universe]
    if args.days:
        extra += ["--days", str(args.days)]
    if args.no_browser:
        extra.append("--no-browser")

    total = len(SCRIPTS)
    results = []

    print(f"\n{'='*60}")
    print(f"  指値バックテスト一括実行  universe={args.universe}")
    print(f"{'='*60}\n")

    for i, (script, label) in enumerate(zip(SCRIPTS, LABELS), 1):
        if not Path(script).exists():
            print(f"[{i}/{total}] {label} — {script} が見つかりません。スキップ。\n")
            continue

        print(f"[{i}/{total}] {label} ({script})")
        print(f"{'─'*60}")
        start = time.time()

        result = subprocess.run(
            base_cmd + [script] + extra,
            text=True,
        )

        elapsed = time.time() - start
        status = "完了" if result.returncode == 0 else f"エラー(code={result.returncode})"
        results.append((label, script, status, elapsed))
        print(f"\n  → {status}  ({elapsed:.1f}秒)\n")

    print(f"\n{'='*60}")
    print("  実行サマリー")
    print(f"{'='*60}")
    for label, script, status, elapsed in results:
        mark = "✓" if status == "完了" else "✗"
        print(f"  {mark} {label:<24} {status}  ({elapsed:.1f}秒)")
    print()


if __name__ == "__main__":
    main()
