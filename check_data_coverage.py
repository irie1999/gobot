"""
check_data_coverage.py  ―  ローカル5分足データの取得期間サマリ
==================================================================
data/minute_5m/*.pkl の全銘柄について、データ取得期間を一覧表示。

【表示内容】
  - 総銘柄数
  - 全体の最古日 〜 最新日
  - 銘柄ごとの開始日/最終日のバラつき
  - 最新日の分布 (今日のデータがある銘柄数など)
  - サンプル銘柄の詳細

【使い方】
  python check_data_coverage.py              # サマリ表示
  python check_data_coverage.py --detail 10  # 上位10銘柄の詳細
  python check_data_coverage.py --stale 7    # 最終日が7日以上前の銘柄を列挙
"""

from __future__ import annotations

import argparse
import pickle
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"


def get_date_range(pkl_path: Path) -> tuple[str | None, str | None, int]:
    """pkl の (最古日, 最新日, 行数) を返す。"""
    try:
        df = pickle.loads(pkl_path.read_bytes())
        if df is None or df.empty or "Date" not in df.columns:
            return None, None, 0
        dates = df["Date"].astype(str).str[:10]
        return dates.min(), dates.max(), len(df)
    except Exception:
        return None, None, 0


def main():
    parser = argparse.ArgumentParser(description="ローカル5分足データ取得期間サマリ")
    parser.add_argument("--detail", type=int, default=0,
                        help="先頭N銘柄の詳細表示")
    parser.add_argument("--stale", type=int, default=0,
                        help="最終日がN日以上前の銘柄を列挙")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        print(f"[ERROR] {DATA_DIR} がありません")
        return

    pkl_files = sorted(DATA_DIR.glob("*.pkl"))
    print(f"対象ディレクトリ: {DATA_DIR}")
    print(f"pklファイル数: {len(pkl_files)}")
    print()

    if not pkl_files:
        print("データなし")
        return

    print("読み込み中...", flush=True)
    first_dates = []
    last_dates = []
    row_counts = []
    bad_files = []
    per_file = []  # (code, first, last, rows)

    for i, pkl in enumerate(pkl_files, 1):
        f, l, n = get_date_range(pkl)
        if f is None:
            bad_files.append(pkl.name)
            continue
        first_dates.append(f)
        last_dates.append(l)
        row_counts.append(n)
        per_file.append((pkl.stem, f, l, n))
        if i % 500 == 0:
            print(f"  {i}/{len(pkl_files)} 処理済み", flush=True)

    print()
    print("=" * 65)
    print("  ローカル5分足データ サマリ")
    print("=" * 65)
    print(f"  有効ファイル数: {len(first_dates)}/{len(pkl_files)}")
    print(f"  破損 or 空: {len(bad_files)}")
    if bad_files[:5]:
        print(f"    (例): {bad_files[:5]}")
    print()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"  本日 (JST): {today}")
    print(f"  全体の最古日: {min(first_dates)}")
    print(f"  全体の最新日: {max(last_dates)}")
    print()

    # 開始日の分布 (上位5)
    print("  【開始日の分布 (代表的な5パターン)】")
    fc = Counter(first_dates).most_common(5)
    for d, c in fc:
        print(f"    {d}: {c}銘柄")
    print()

    # 最終日の分布 (上位5)
    print("  【最終日の分布 (代表的な5パターン)】")
    lc = Counter(last_dates).most_common(5)
    for d, c in lc:
        print(f"    {d}: {c}銘柄")
    print()

    # 行数の分布
    avg_rows = sum(row_counts) // len(row_counts)
    print(f"  【行数】")
    print(f"    平均: {avg_rows:,}行/銘柄")
    print(f"    最大: {max(row_counts):,}行")
    print(f"    最小: {min(row_counts):,}行")
    print(f"    合計: {sum(row_counts):,}行")
    print()

    # 古いデータ警告
    if args.stale > 0:
        cutoff = (datetime.now(JST) - timedelta(days=args.stale)).strftime("%Y-%m-%d")
        stale = [x for x in per_file if x[2] < cutoff]
        print(f"  【最終日が {args.stale} 日以上前の銘柄: {len(stale)}件】")
        for code, f, l, n in stale[:20]:
            print(f"    {code}: {l} ({n:,}行)")
        if len(stale) > 20:
            print(f"    ... 他 {len(stale)-20}銘柄")
        print()

    # 詳細表示
    if args.detail > 0:
        print(f"  【先頭{args.detail}銘柄の詳細】")
        for code, f, l, n in per_file[:args.detail]:
            print(f"    {code}: {f} 〜 {l}  {n:,}行")
        print()

    print("=" * 65)


if __name__ == "__main__":
    main()
