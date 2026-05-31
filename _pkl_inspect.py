"""pkl ファイルの状態を詳細確認"""
import pickle
import os
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

# 検査対象 (winners の一部)
test_files = ["58030.pkl", "16050.pkl", "64070.pkl", "53320.pkl"]

print(f"検査対象: {DATA_DIR}\n")
print(f"{'ファイル':12} {'サイズ':>10} {'更新時刻':>20} {'行数':>8} {'最古日':>20} {'最新日':>20}")
print("-" * 100)

for fname in test_files:
    pkl = DATA_DIR / fname
    if not pkl.exists():
        print(f"{fname:12} NOT FOUND")
        continue

    stat = pkl.stat()
    size = stat.st_size
    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

    try:
        df = pickle.loads(pkl.read_bytes())
        nrows = len(df)
        if "Date" in df.columns and "Time" in df.columns:
            # 最古・最新の日時を組み立て
            first = f"{df.iloc[0]['Date']} {df.iloc[0]['Time']}"
            last = f"{df.iloc[-1]['Date']} {df.iloc[-1]['Time']}"
        elif "DateTime" in df.columns:
            first = str(df.iloc[0]['DateTime'])[:19]
            last = str(df.iloc[-1]['DateTime'])[:19]
        else:
            first = "?"
            last = "?"
        print(f"{fname:12} {size:>10,} {mtime:>20} {nrows:>8} {str(first)[:19]:>20} {str(last)[:19]:>20}")
    except Exception as e:
        print(f"{fname:12} ERR: {e}")

# 検査: pkl内に05月のデータがあるか?
print("\n" + "=" * 100)
print("5803.T (フジクラ) のpkl内、2026-05のデータ有無:")
pkl = DATA_DIR / "58030.pkl"
if pkl.exists():
    df = pickle.loads(pkl.read_bytes())
    if "Date" in df.columns:
        dates = df["Date"].astype(str).str[:10]
        may_count = (dates >= "2026-05-01").sum()
        apr_count = ((dates >= "2026-04-01") & (dates < "2026-05-01")).sum()
        print(f"  4月のバー数: {apr_count}")
        print(f"  5月のバー数: {may_count}")
        unique_dates = sorted(dates.unique())
        last_5 = unique_dates[-5:]
        print(f"  最新5日付: {last_5}")
