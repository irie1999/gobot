"""
summarize_verify_log.py - verify_pkl_daily の結果を要約表示

【目的】
verify_pkl_daily の出力ログ (UTF-8) を読み込み、PowerShell の
表示エンコーディングを気にせずに要約を見られるようにする。

【使い方】
  python summarize_verify_log.py verify_all_log.txt
  python summarize_verify_log.py verify_all_log.txt --show-major
  python summarize_verify_log.py verify_all_log.txt --threshold 90
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Windows cp932 で UTF-8 文字を出力するため
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("logfile", help="verify_pkl_daily の出力ログ")
    p.add_argument("--show-major", action="store_true",
                   help="重大破損 (<99%) 銘柄の詳細表示")
    p.add_argument("--threshold", type=float, default=99.0,
                   help="重大破損とみなす一致率閾値 (デフォルト 99.0)")
    args = p.parse_args()

    path = Path(args.logfile)
    if not path.exists():
        print(f"[error] {path} 不在")
        return

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")

    # 行パターン: "  [ 234/1552] ✓ 1234.T: 共通  486日中 破損   0日 (100.0% 一致)"
    pattern = re.compile(
        r"\[\s*\d+/\d+\]\s+(\S+)\s+(\d+\.T):\s+共通\s+(\d+)日中\s+"
        r"破損\s+(\d+)日\s+\(\s*([\d.]+)%\s+一致\)"
    )
    err_pattern = re.compile(r"\[\s*\d+/\d+\]\s+\[ERR\]\s+(\d+\.T):\s+(.+)")

    clean: list = []
    minor: list = []
    major: list = []
    errors: list = []

    for line in lines:
        m = pattern.search(line)
        if m:
            mark = m.group(1)
            ticker = m.group(2)
            common_days = int(m.group(3))
            bad_days = int(m.group(4))
            match_pct = float(m.group(5))
            item = {
                "ticker": ticker,
                "common_days": common_days,
                "bad_days": bad_days,
                "match_pct": match_pct,
            }
            if match_pct >= 100.0:
                clean.append(item)
            elif match_pct >= args.threshold:
                minor.append(item)
            else:
                major.append(item)
            continue
        m = err_pattern.search(line)
        if m:
            errors.append({"ticker": m.group(1), "reason": m.group(2).strip()})

    total = len(clean) + len(minor) + len(major) + len(errors)
    print(f"========================================")
    print(f" 検証結果サマリ ({path.name})")
    print(f"========================================")
    print(f"  全銘柄数:         {total}")
    print(f"  ✓ クリーン (100%):   {len(clean):>5}銘柄")
    print(f"  △ 軽微 (>={args.threshold:.0f}%):     {len(minor):>5}銘柄")
    print(f"  ✗ 重大 (<{args.threshold:.0f}%):      {len(major):>5}銘柄")
    print(f"  ERR エラー:         {len(errors):>5}銘柄")
    print()

    if major:
        # 破損日数で降順ソート
        major_sorted = sorted(major, key=lambda x: -x["bad_days"])
        print(f"重大破損 (clean 推奨) Top 30:")
        for item in major_sorted[:30]:
            print(f"  {item['ticker']}: "
                  f"共通 {item['common_days']:>4}日中 "
                  f"破損 {item['bad_days']:>4}日 "
                  f"({item['match_pct']:5.1f}% 一致)")
        if len(major) > 30:
            print(f"  ... 他 {len(major) - 30}銘柄")
        print()

    if args.show_major and major:
        print(f"重大破損 全 {len(major)}銘柄:")
        for item in sorted(major, key=lambda x: -x["bad_days"]):
            print(f"  {item['ticker']}: 破損 {item['bad_days']}日 "
                  f"({item['match_pct']:.1f}%)")
        print()

    if errors:
        # エラー理由別集計
        from collections import Counter
        reasons = Counter(e["reason"] for e in errors)
        print(f"エラー内訳:")
        for reason, count in reasons.most_common():
            print(f"  {count:>4}件: {reason}")
        print()

    # 推奨アクション
    if major:
        print(f"━━━ 次のアクション ━━━")
        print(f"  Type A 重大破損が {len(major)}銘柄。clean を推奨:")
        print(f"  python clean_pkl_outliers.py --from-watchlist "
              f"symbols_listed_all.py --use-yfinance-daily --apply")


if __name__ == "__main__":
    main()
