"""check_5m_range.py — ローカル5分足データ(stock_5min)の日付範囲を調べる。

lss(同日決済)は5分足が必須。5分足の無い月はlss集計から除外されるので、
「どこまで遡れるか」を確認する。サンプル銘柄のpklを読んで最古/最新日を出す。

使い方(swingtradeフォルダで):  python check_5m_range.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import pandas as pd
import daytrade_data as dd


def main() -> int:
    print("=" * 60)
    print(f"5分足データ場所: {dd.DATA_DIR}")
    if not dd.DATA_DIR.exists():
        print("  → フォルダが見つかりません。MINUTE_5M_DIR / stock_5min を確認してください。")
        return 1
    syms = dd.available_local_symbols()
    print(f"銘柄数(pklファイル): {len(syms)}")
    if not syms:
        print("  → pklが1つもありません。")
        return 1

    # 先頭最大60銘柄をサンプルして日付範囲を集計
    sample = syms[:60]
    firsts, lasts, ndays = [], [], []
    for s in sample:
        try:
            df = pd.read_pickle(dd.DATA_DIR / f"{s}.pkl")
            idx = pd.to_datetime(df.index)
            firsts.append(idx.min())
            lasts.append(idx.max())
            ndays.append(idx.normalize().nunique())
        except Exception:
            continue
    if not firsts:
        print("  → pklを読めませんでした(形式が想定外)。")
        return 1

    print("-" * 60)
    print(f"サンプル {len(firsts)} 銘柄の日付範囲:")
    print(f"  最も古い開始日(全銘柄で最古) : {min(firsts).date()}")
    print(f"  最も古い開始日(全銘柄で最新) : {max(firsts).date()}  ← ここより前は"
          "『データがある銘柄が少ない』可能性")
    print(f"  最新日                        : {max(lasts).date()}")
    print(f"  1銘柄あたりの営業日数(中央値) : {int(pd.Series(ndays).median())}日")
    print("-" * 60)
    # 月別カバレッジ: 各月に何銘柄がデータを持つか(先頭サンプルで概算)
    from collections import Counter
    month_cov: Counter = Counter()
    for s in sample:
        try:
            df = pd.read_pickle(dd.DATA_DIR / f"{s}.pkl")
            months = set(pd.to_datetime(df.index).strftime("%Y-%m"))
            for m in months:
                month_cov[m] += 1
        except Exception:
            continue
    print("月別カバレッジ(サンプル銘柄のうちデータがある数):")
    for m in sorted(month_cov):
        bar = "#" * min(month_cov[m], 60)
        print(f"  {m}: {month_cov[m]:>3}銘柄 {bar}")
    print("=" * 60)
    print("→ カバレッジが薄い/無い月は、lss がその月を集計できません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
