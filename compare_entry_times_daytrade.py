"""
compare_entry_times_daytrade.py  ―  ENTRY_START 複数パターン比較
==================================================================
holdout_periods_report_daytrade.py を ENTRY_START の異なる値で
連続実行し、最適な寄付き開始時刻を発見する。

【検証パターン】
  09:00 (寄付き直後、最大ボラ獲得)
  09:15 (15分待ち)
  09:30 (現行デフォルト、保守)
  09:45 (落ち着き待ち)

【出力】
  holdout_periods_<variant>_<HHMM>_<日付>.html (各パターン別)
  compare_entry_times_<variant>_<日付>.md (比較サマリ)

【使い方】
  python compare_entry_times_daytrade.py                  # 全パターン (ロング+ショート)
  python compare_entry_times_daytrade.py --short          # ショートのみ
  python compare_entry_times_daytrade.py --long-only      # ロングのみ
  python compare_entry_times_daytrade.py --times 09:00,09:30  # 指定時刻のみ
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

DEFAULT_TIMES = ["09:00", "09:15", "09:30", "09:45"]


def run_once(entry_start, extra_args):
    """1パターン実行。"""
    env = os.environ.copy()
    env["DAYTRADE_ENTRY_START"] = entry_start
    today = datetime.now(JST).strftime("%Y-%m-%d")
    hhmm = entry_start.replace(":", "")

    # holdout_periods_<variant>_<日付>.html を退避するため、生成後にリネーム
    cmd = [sys.executable, "holdout_periods_report_daytrade.py",
           "--force", "--no-browser"] + extra_args
    print(f"\n{'='*70}")
    print(f"  ENTRY_START = {entry_start}")
    print(f"  CMD: {' '.join(cmd)}")
    print(f"{'='*70}", flush=True)

    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"[error] ENTRY_START={entry_start} 失敗", file=sys.stderr)
        return None

    # 生成ファイルをリネーム (variant suffix判定)
    variant = ""
    if "--short" in extra_args:
        variant = "_short"
    elif "--long-only" in extra_args:
        variant = "_long"

    src = Path(f"holdout_periods{variant}_{today}.html")
    if not src.exists():
        print(f"[warn] 出力ファイル {src} が見つかりません")
        return None

    dst = Path(f"holdout_periods{variant}_{hhmm}_{today}.html")
    src.rename(dst)
    print(f"  → {dst}")
    return dst


def main():
    parser = argparse.ArgumentParser(
        description="ENTRY_START 複数パターン比較")
    parser.add_argument("--times", default=None,
                        help=f"カンマ区切り (例: 09:00,09:15,09:30)。"
                             f"省略時は {','.join(DEFAULT_TIMES)}")
    parser.add_argument("--short", action="store_true",
                        help="ショート戦略のみで全パターン検証")
    parser.add_argument("--long-only", action="store_true",
                        help="ロング戦略のみで全パターン検証")
    parser.add_argument("--max-price", type=int, default=10_000)
    parser.add_argument("--min-price", type=int, default=0)
    args = parser.parse_args()

    if args.short and args.long_only:
        print("[error] --short と --long-only は同時指定できません")
        return

    times = args.times.split(",") if args.times else DEFAULT_TIMES
    times = [t.strip() for t in times if t.strip()]

    extra_args = []
    if args.short:
        extra_args.append("--short")
    if args.long_only:
        extra_args.append("--long-only")
    if args.max_price:
        extra_args.extend(["--max-price", str(args.max_price)])
    if args.min_price > 0:
        extra_args.extend(["--min-price", str(args.min_price)])

    today = datetime.now(JST).strftime("%Y-%m-%d")
    variant_label = "short" if args.short else "long" if args.long_only else "all"

    print(f"ENTRY_START 比較検証")
    print(f"  パターン: {times}")
    print(f"  variant: {variant_label}")
    print(f"  価格帯: {args.min_price:,}-{args.max_price:,}円")

    results = {}
    for t in times:
        out_path = run_once(t, extra_args)
        results[t] = out_path

    # 比較サマリ MD
    summary = Path(f"compare_entry_times_{variant_label}_{today}.md")
    lines = [
        f"# ENTRY_START 比較 ({variant_label})",
        f"",
        f"生成: {today}",
        f"パターン: {', '.join(times)}",
        f"",
        f"## 出力ファイル",
        f"",
    ]
    for t in times:
        p = results.get(t)
        status = str(p) if p else "(失敗)"
        lines.append(f"- **{t}**: `{status}`")
    lines.extend([
        f"",
        f"## 比較方法",
        f"",
        f"各HTMLを並べて開き、以下を比較:",
        f"",
        f"1. 直近30日タブの **合格率/合格者TEST損益**",
        f"2. 全6タブ★合格銘柄の数",
        f"3. 取引数の変化 (寄付き早めると増えるはず)",
        f"4. DD (Drawdown) の悪化有無",
        f"",
        f"## 評価指標",
        f"",
        f"| 時刻 | 評価ポイント |",
        f"|---|---|",
        f"| 09:00 | ボラ最大、取引数最多、スリッページリスク大 |",
        f"| 09:15 | バランス型、寄付き最初の15分は避ける |",
        f"| 09:30 | 保守的、寄付きノイズ排除 (現行デフォルト) |",
        f"| 09:45 | より保守、流動性確保後 |",
    ])
    summary.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n生成: {summary}")
    print(f"  HTMLを並べて開いて比較してください")


if __name__ == "__main__":
    main()
