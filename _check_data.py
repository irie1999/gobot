"""ローカルpklの中身を確認"""
import pickle
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

# 日立製作所 (6501) で確認
pkl_path = DATA_DIR / "65010.pkl"
if not pkl_path.exists():
    print(f"ファイルなし: {pkl_path}")
else:
    df = pickle.loads(pkl_path.read_bytes())
    print(f"銘柄: 65010 日立製作所")
    print(f"総行数: {len(df)}")
    print(f"列名: {list(df.columns)}")
    print()
    if "Date" in df.columns:
        # 最新10日分を確認
        dates = sorted(set(str(d)[:10] for d in df["Date"].tolist()))
        print(f"データ日付範囲: {dates[0]} 〜 {dates[-1]}")
        print(f"日数: {len(dates)}")
        print(f"最新10日:")
        for d in dates[-10:]:
            count = len(df[df["Date"].astype(str).str[:10] == d])
            print(f"  {d}: {count}本")
