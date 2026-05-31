"""
extract_and_report.py  ―  winners抽出 + 取引明細HTML を一気に実行
==================================================================
1コマンドで:
  1. prime ユニバースから winners 抽出 (daytrade_donchian_winners.py 生成)
  2. winners の取引明細HTML レポート生成
  3. ブラウザで自動オープン

【使い方】
  python extract_and_report.py                          # 30日抽出 + 30日レポート
  python extract_and_report.py --extract-days 30 --report-days 30
  python extract_and_report.py --min-pf 1.5 --min-trades 15 --min-pnl 10000
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd, label):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  $ {' '.join(cmd)}")
    print('='*70, flush=True)
    result = subprocess.run(cmd, cwd=Path(__file__).resolve().parent)
    if result.returncode != 0:
        print(f"\n[error] {label} 失敗 (exit code: {result.returncode})")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="winners抽出 + 取引明細HTML 一括実行")
    parser.add_argument("--extract-days", type=int, default=30,
                        help="winners抽出期間 (デフォルト: 30日)")
    parser.add_argument("--report-days", type=int, default=30,
                        help="レポート期間 (デフォルト: 30日)")
    parser.add_argument("--min-pf", type=float, default=1.5,
                        help="抽出: 最低PF (デフォルト: 1.5)")
    parser.add_argument("--min-trades", type=int, default=15,
                        help="抽出: 最低取引数 (デフォルト: 15)")
    parser.add_argument("--min-pnl", type=int, default=10000,
                        help="抽出: 最低損益円 (デフォルト: 10,000)")
    parser.add_argument("--universe", default="prime",
                        choices=["prime", "n225", "watch"],
                        help="抽出元ユニバース (デフォルト: prime)")
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # Step 1: 30日 winners 抽出
    run_cmd([
        sys.executable, "daytrade_donchian_compare.py",
        "--universe", args.universe,
        "--days", str(args.extract_days),
        "--extract-winners",
        "--min-pf", str(args.min_pf),
        "--min-trades", str(args.min_trades),
        "--min-pnl", str(args.min_pnl),
        "--source", "local",
        "--no-html",
    ], f"[Step 1/2] winners抽出 ({args.universe} {args.extract_days}日)")

    # Step 2: winners 取引明細レポート
    cmd2 = [
        sys.executable, "winners_detail_report.py",
        "--days", str(args.report_days),
        "--budget", str(args.budget),
        "--max-risk", str(args.max_risk),
    ]
    if args.no_browser:
        cmd2.append("--no-browser")
    run_cmd(cmd2, f"[Step 2/2] 取引明細HTML レポート ({args.report_days}日)")

    print("\n" + "="*70)
    print("  ✓ 全処理完了")
    print("="*70)


if __name__ == "__main__":
    main()
