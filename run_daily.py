#!/usr/bin/env python3
"""
日次レポート実行スクリプト（Windows/Mac/Linux共通）
15:30 JST 以降は --force を自動付与（引け後の最新データで再計算）
それ以前は --force なし（当日キャッシュがあれば即返し）

使い方:
    python run_daily.py
    python run_daily.py --days 365   # 追加オプションも渡せる
"""
import subprocess
import sys
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
now = datetime.now(JST)
threshold = now.replace(hour=15, minute=30, second=0, microsecond=0)

if now >= threshold:
    force = ["--force"]
    print(f"[run_daily] {now.strftime('%H:%M')} JST — 15:30以降のため --force を付与して実行")
else:
    force = []
    print(f"[run_daily] {now.strftime('%H:%M')} JST — 15:30前のため --force なし（キャッシュ優先）")

cmd = [
    sys.executable,
    "run_signals_holdout_all.py",
    "--both",
    "--min-price", "1000",
    "--price-ranges", "6000,10000",
    "--no-wf-history",
] + force + sys.argv[1:]

print(f"[run_daily] 実行: {' '.join(cmd)}\n")
subprocess.run(cmd)
