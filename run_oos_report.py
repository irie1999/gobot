"""
run_oos_report.py  ―  デイトレ OOS レポート 毎日実行スクリプト

銘柄選定 (daytrade_winners_*_shift*d.py) は固定。
毎回 simple_report_all_shifts.py でレポートを再生成するだけ。

【使い方】
  python run_oos_report.py               # レポート生成
  python run_oos_report.py --open        # 生成後ブラウザで開く
  python run_oos_report.py --refresh     # バックテストキャッシュをクリアして再計算

【カスタマイズ】
  スクリプト冒頭の REPORT_ARGS を変更してください。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# ─── レポート引数 (必要に応じて変更) ────────────────────────────────
REPORT_ARGS = [
    "--min-robustness", "2",
    "--min-oos-pf", "1.0",
]
# ────────────────────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(
        description="デイトレ OOS レポート 毎日実行スクリプト"
    )
    p.add_argument("--open", action="store_true",
                   help="レポート生成後ブラウザで開く")
    p.add_argument("--refresh", action="store_true",
                   help="バックテストキャッシュをクリアして再計算")
    args = p.parse_args()

    extra = list(REPORT_ARGS)
    if not args.open:
        extra.append("--no-open")
    if args.refresh:
        extra.append("--refresh-cache")

    cmd = [sys.executable, "simple_report_all_shifts.py"] + extra
    start = datetime.now(JST)
    print(f"開始: {start.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"実行: {' '.join(cmd)}", flush=True)

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[ERROR] 終了コード {result.returncode}", flush=True)
        sys.exit(1)

    elapsed = (datetime.now(JST) - start).total_seconds()
    print(f"\n完了: 所要時間 {elapsed/60:.1f} 分", flush=True)


if __name__ == "__main__":
    main()
