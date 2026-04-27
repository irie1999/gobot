"""
_check_data_coverage.py  ―  ローカル5分足データの保有状況確認
==================================================================
data/minute_5m/*.pkl の銘柄数、期間、サンプルを表示。

【使い方】
  python _check_data_coverage.py                 # 全体サマリ
  python _check_data_coverage.py --detail        # 詳細(全銘柄リスト)
  python _check_data_coverage.py --sample 10     # サンプル10銘柄
"""

from __future__ import annotations

import argparse
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"


def _extract_dates(df: pd.DataFrame):
    """DataFrame から日時インデックス/カラムを抽出して開始・終了日を返す。"""
    if df is None or df.empty:
        return None, None

    # ① DatetimeIndex
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index[0], df.index[-1]

    # ② DateTime カラム (J-Quants V2)
    for col in ("DateTime", "datetime", "Date"):
        if col in df.columns:
            try:
                dt = pd.to_datetime(df[col], errors="coerce").dropna()
                if len(dt) > 0:
                    return dt.iloc[0], dt.iloc[-1]
            except Exception:
                continue

    # ③ Date + Time
    if "Date" in df.columns and "Time" in df.columns:
        try:
            dt = pd.to_datetime(df["Date"].astype(str) + " " + df["Time"].astype(str),
                                errors="coerce").dropna()
            if len(dt) > 0:
                return dt.iloc[0], dt.iloc[-1]
        except Exception:
            pass

    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--detail", action="store_true",
                        help="全銘柄の期間を表示")
    parser.add_argument("--sample", type=int, default=5,
                        help="サンプル表示銘柄数 (デフォルト 5)")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        print(f"[error] {DATA_DIR} が存在しません")
        sys.exit(1)

    files = sorted(DATA_DIR.glob("*.pkl"))
    if not files:
        print(f"[warn] {DATA_DIR} に銘柄データがありません")
        sys.exit(0)

    print(f"=" * 70)
    print(f"  5分足データ保有状況")
    print(f"=" * 70)
    print(f"  保存先     : {DATA_DIR}")
    print(f"  銘柄数     : {len(files):,}")
    print()

    # 全銘柄のメタデータを集計
    all_starts = []
    all_ends = []
    all_lengths = []
    sizes = []
    error_files = []

    for f in files:
        try:
            df = pickle.loads(f.read_bytes())
            if isinstance(df, pd.DataFrame) and not df.empty:
                start, end = _extract_dates(df)
                if start is not None and end is not None:
                    all_starts.append(start)
                    all_ends.append(end)
                    all_lengths.append(len(df))
                    sizes.append(f.stat().st_size)
                else:
                    error_files.append(f"{f.name} (日付なし)")
            else:
                error_files.append(f.name)
        except Exception as e:
            error_files.append(f"{f.name} ({e})")

    if not all_starts:
        print("[error] 有効なデータがありません")
        sys.exit(1)

    # 全体サマリ
    earliest_start = min(all_starts)
    latest_start = max(all_starts)
    earliest_end = min(all_ends)
    latest_end = max(all_ends)
    avg_bars = sum(all_lengths) / len(all_lengths)
    total_size_mb = sum(sizes) / 1024 / 1024

    print(f"  期間カバー範囲 (開始日)")
    print(f"    最古     : {earliest_start}")
    print(f"    最新     : {latest_start}")
    print(f"  期間カバー範囲 (終了日)")
    print(f"    最古     : {earliest_end}")
    print(f"    最新     : {latest_end}")
    print()
    print(f"  バー数")
    print(f"    最小     : {min(all_lengths):,}")
    print(f"    最大     : {max(all_lengths):,}")
    print(f"    平均     : {avg_bars:,.0f}")
    print()
    print(f"  ディスク使用")
    print(f"    合計     : {total_size_mb:,.1f} MB")
    print(f"    平均/銘柄: {total_size_mb/len(files)*1024:.1f} KB")
    print()

    if error_files:
        print(f"  ⚠️  読込エラー: {len(error_files)}銘柄")
        for f in error_files[:5]:
            print(f"      {f}")
        print()

    # サンプル銘柄 (最初/最後)
    print(f"=" * 70)
    print(f"  サンプル銘柄 (先頭{args.sample}件 + 末尾{args.sample}件)")
    print(f"=" * 70)
    print(f"{'銘柄コード':<10} {'開始':<20} {'終了':<20} {'本数':>10} {'ファイル(KB)':>10}")
    print("-" * 70)
    sample_files = files[:args.sample] + files[-args.sample:]
    for f in sample_files:
        try:
            df = pickle.loads(f.read_bytes())
            start, end = _extract_dates(df)
            start_s = str(start)[:19] if start else "N/A"
            end_s = str(end)[:19] if end else "N/A"
            print(f"{f.stem:<10} {start_s:<20} {end_s:<20} "
                  f"{len(df):>10,} {f.stat().st_size/1024:>10.1f}")
        except Exception:
            pass
    print()

    # 期間別の銘柄分布
    print(f"=" * 70)
    print(f"  期間別の銘柄分布 (最新更新日ベース)")
    print(f"=" * 70)
    end_date_dist = defaultdict(int)
    for end in all_ends:
        try:
            end_date_dist[pd.Timestamp(end).date()] += 1
        except Exception:
            pass
    for date in sorted(end_date_dist.keys(), reverse=True)[:10]:
        bar = "█" * min(50, end_date_dist[date] * 50 // len(files))
        print(f"  {date}: {end_date_dist[date]:>5,}  {bar}")
    print()

    if args.detail:
        print(f"=" * 70)
        print(f"  全銘柄詳細 ({len(files)}件)")
        print(f"=" * 70)
        for f in files:
            try:
                df = pickle.loads(f.read_bytes())
                start, end = _extract_dates(df)
                print(f"  {f.stem}: {start} 〜 {end}  ({len(df):,}本)")
            except Exception:
                pass


if __name__ == "__main__":
    main()
