"""ローカルpklが正常に読めない原因を診断"""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

# トヨタ (7203) で診断
test_code = "72030"
pkl_path = DATA_DIR / f"{test_code}.pkl"
print(f"=== 診断: {pkl_path} ===")
print(f"存在: {pkl_path.exists()}")

if not pkl_path.exists():
    print("ERROR: ファイルなし"); exit(1)

# 1. 生pkl読み込み
raw = pickle.loads(pkl_path.read_bytes())
print(f"\n[1] 生pkl:")
print(f"  type: {type(raw).__name__}")
print(f"  shape: {raw.shape if hasattr(raw, 'shape') else 'N/A'}")
print(f"  columns: {list(raw.columns) if hasattr(raw, 'columns') else 'N/A'}")
print(f"  columns重複: {raw.columns.duplicated().any() if hasattr(raw, 'columns') else 'N/A'}")
print(f"  index type: {type(raw.index).__name__}")
print(f"  head:\n{raw.head(3)}")

# 2. normalize
from daytrade_data import normalize_minute_df, resample_to_5m, _load_local
print(f"\n[2] normalize_minute_df:")
try:
    df = normalize_minute_df(raw)
    print(f"  → type: {type(df).__name__}")
    print(f"  → shape: {df.shape}")
    print(f"  → index type: {type(df.index).__name__}")
    print(f"  → index head: {list(df.index[:3])}")
    print(f"  → columns: {list(df.columns)}")
except Exception as e:
    print(f"  ✗ ERROR: {type(e).__name__}: {e}")
    import traceback; traceback.print_exc()

# 3. resample
print(f"\n[3] resample_to_5m:")
try:
    df2 = resample_to_5m(df)
    print(f"  → shape: {df2.shape}")
    print(f"  → index type: {type(df2.index).__name__}")
except Exception as e:
    print(f"  ✗ ERROR: {type(e).__name__}: {e}")

# 4. 期間フィルタ
print(f"\n[4] 期間フィルタ:")
try:
    cutoff = pd.Timestamp(datetime.now(JST).date() - timedelta(days=60))
    print(f"  cutoff: {cutoff} ({type(cutoff).__name__})")
    print(f"  index[0]: {df2.index[0]} ({type(df2.index[0]).__name__})")
    print(f"  index[-1]: {df2.index[-1]}")
    df3 = df2[df2.index >= cutoff]
    print(f"  → filtered shape: {df3.shape}")
except Exception as e:
    print(f"  ✗ ERROR: {type(e).__name__}: {e}")
    import traceback; traceback.print_exc()

# 5. _load_local全体
print(f"\n[5] _load_local 全体:")
result = _load_local("7203.T", 60)
print(f"  → result: {type(result).__name__ if result is not None else 'None'}")
if result is not None:
    print(f"  → shape: {result.shape}")
