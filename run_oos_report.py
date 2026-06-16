"""
run_oos_report.py  ―  デイトレ OOS レポート 毎日実行スクリプト

【実行順序】
  STEP 1: scan_walkforward_daytrade.py --all-periods   (銘柄選定)
  STEP 2: gen_watchlist_from_walkforward.py --all-periods  (watchlist 生成)
  STEP 3: simple_report_all_shifts.py                  (OOS レポート生成)

【使い方】
  python run_oos_report.py               # フル実行 (全 STEP)
  python run_oos_report.py --skip-scan   # STEP 1 スキップ (watchlist 再利用)
  python run_oos_report.py --report-only # STEP 3 のみ (watchlist 生成済みの場合)

【カスタマイズ】
  スクリプト冒頭の SCAN_ARGS / GEN_ARGS / REPORT_ARGS を変更してください。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# ─── 各ステップの引数 (必要に応じて変更) ───────────────────────────
SCAN_ARGS = [
    "--all-periods",
    "--folds", "2",
    "--swing-thresholds",
    "--max-price", "6000",
    "--min-price", "1000",
]

GEN_ARGS = [
    "--all-periods",
]

REPORT_ARGS = [
    "--min-robustness", "2",
    "--min-oos-pf", "1.0",
    "--no-open",
]
# ────────────────────────────────────────────────────────────────────


def run_step(label: str, script: str, extra_args: list[str]) -> bool:
    """1ステップ実行。失敗したら False を返す。"""
    cmd = [sys.executable, script] + extra_args
    print(f"\n{'='*60}", flush=True)
    print(f"[{label}] {' '.join(cmd)}", flush=True)
    print(f"{'='*60}", flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[ERROR] {label} が終了コード {result.returncode} で失敗しました",
              flush=True)
        return False
    print(f"\n[OK] {label} 完了", flush=True)
    return True


def main():
    p = argparse.ArgumentParser(
        description="デイトレ OOS レポート 毎日実行スクリプト"
    )
    p.add_argument("--skip-scan", action="store_true",
                   help="STEP 1 (scan_walkforward) をスキップ")
    p.add_argument("--skip-gen", action="store_true",
                   help="STEP 2 (gen_watchlist) をスキップ")
    p.add_argument("--report-only", action="store_true",
                   help="STEP 3 のみ実行 (--skip-scan --skip-gen の短縮形)")
    p.add_argument("--open", action="store_true",
                   help="レポート生成後ブラウザで開く")
    args = p.parse_args()

    if args.report_only:
        args.skip_scan = True
        args.skip_gen = True

    report_args = [a for a in REPORT_ARGS if a != "--no-open"]
    if not args.open:
        report_args.append("--no-open")

    start = datetime.now(JST)
    print(f"開始: {start.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    if not args.skip_scan:
        ok = run_step("STEP 1: 銘柄選定 (scan_walkforward_daytrade)",
                      "scan_walkforward_daytrade.py", SCAN_ARGS)
        if not ok:
            sys.exit(1)

    if not args.skip_gen:
        ok = run_step("STEP 2: watchlist 生成 (gen_watchlist_from_walkforward)",
                      "gen_watchlist_from_walkforward.py", GEN_ARGS)
        if not ok:
            sys.exit(1)

    ok = run_step("STEP 3: OOS レポート生成 (simple_report_all_shifts)",
                  "simple_report_all_shifts.py", report_args)
    if not ok:
        sys.exit(1)

    elapsed = (datetime.now(JST) - start).total_seconds()
    print(f"\n完了: 所要時間 {elapsed/60:.1f} 分", flush=True)


if __name__ == "__main__":
    main()
