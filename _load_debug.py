"""normalize_minute_df の各ステップを実行して問題箇所特定"""
import pickle
import pandas as pd
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

# フジクラ
pkl_path = DATA_DIR / "58030.pkl"
print(f"対象: {pkl_path.name} (フジクラ)")
raw = pickle.loads(pkl_path.read_bytes())
print(f"\n[1] 生pkl 状態:")
print(f"  shape: {raw.shape}")
print(f"  columns: {list(raw.columns)}")
print(f"  Date dtype: {raw['Date'].dtype if 'Date' in raw.columns else 'N/A'}")
print(f"  Time dtype: {raw['Time'].dtype if 'Time' in raw.columns else 'N/A'}")
print(f"  Date 最古3件: {raw['Date'].head(3).tolist()}")
print(f"  Date 最新3件: {raw['Date'].tail(3).tolist()}")
print(f"  Time 最古3件: {raw['Time'].head(3).tolist()}")
print(f"  Time 最新3件: {raw['Time'].tail(3).tolist()}")

# Date列を文字列化テスト
print(f"\n[2] Date文字列化テスト:")
date_str = raw['Date'].astype(str)
print(f"  最古3: {date_str.head(3).tolist()}")
print(f"  最新3: {date_str.tail(3).tolist()}")
date_part = date_str.str[:10]
print(f"  str[:10] 最古3: {date_part.head(3).tolist()}")
print(f"  str[:10] 最新3: {date_part.tail(3).tolist()}")

# 連結
print(f"\n[3] 連結テスト:")
combined = date_part + " " + raw["Time"].astype(str)
print(f"  最古3: {combined.head(3).tolist()}")
print(f"  最新3: {combined.tail(3).tolist()}")

# pd.to_datetime
print(f"\n[4] pd.to_datetime:")
dt_series = pd.to_datetime(combined, format="%Y-%m-%d %H:%M", errors="coerce")
print(f"  最古3: {dt_series.head(3).tolist()}")
print(f"  最新3: {dt_series.tail(3).tolist()}")
print(f"  NaT数: {dt_series.isna().sum()}/{len(dt_series)}")

# normalize_minute_df を直接呼ぶ
print(f"\n[5] normalize_minute_df 直接呼出:")
from daytrade_data import normalize_minute_df
df = normalize_minute_df(raw)
print(f"  shape: {df.shape}")
print(f"  index type: {type(df.index).__name__}")
if not df.empty:
    print(f"  最古: {df.index[0]}")
    print(f"  最新: {df.index[-1]}")

# _load_local 呼出
print(f"\n[6] _load_local('5803.T', 60):")
from daytrade_data import _load_local
df2 = _load_local("5803.T", 60)
if df2 is None:
    print("  None")
else:
    print(f"  shape: {df2.shape}")
    print(f"  最古: {df2.index[0]}")
    print(f"  最新: {df2.index[-1]}")
