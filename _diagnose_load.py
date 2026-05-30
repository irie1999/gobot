"""ローカルpklが正常に読めない原因を診断"""
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

print(f"=== DATA_DIR: {DATA_DIR} ===")
all_pkls = sorted(DATA_DIR.glob("*.pkl"))
print(f"pklファイル数: {len(all_pkls)}")
print(f"先頭10件: {[p.name for p in all_pkls[:10]]}")
print(f"末尾10件: {[p.name for p in all_pkls[-10:]]}")

# ファイル名の長さ分布
from collections import Counter
name_len = Counter(len(p.stem) for p in all_pkls)
print(f"ファイル名(拡張子なし)の長さ分布: {dict(name_len)}")

# Toyota系列を探す
toyota_candidates = [p for p in all_pkls if "7203" in p.stem]
print(f"\n7203 関連ファイル: {[p.name for p in toyota_candidates]}")

# 最初の1ファイルで診断
if all_pkls:
    test_pkl = all_pkls[0]
    print(f"\n=== 診断対象: {test_pkl.name} ===")
    raw = pickle.loads(test_pkl.read_bytes())
    print(f"[1] 生pkl:")
    print(f"  type: {type(raw).__name__}")
    print(f"  shape: {raw.shape if hasattr(raw, 'shape') else 'N/A'}")
    print(f"  columns: {list(raw.columns) if hasattr(raw, 'columns') else 'N/A'}")
    if hasattr(raw, 'columns'):
        print(f"  columns重複: {raw.columns.duplicated().any()}")
    print(f"  index type: {type(raw.index).__name__}")
    print(f"  head:\n{raw.head(3)}")

    # normalize 経由で確認
    from daytrade_data import normalize_minute_df, resample_to_5m, _load_local, yf_to_jquants

    # ファイル名から yfinance code を逆引き (72030 → 7203.T)
    stem = test_pkl.stem
    if len(stem) == 5 and stem.endswith("0") and stem[:4].isdigit():
        yf_code = stem[:4] + ".T"
    elif len(stem) == 4 and stem.isdigit():
        yf_code = stem + ".T"
    else:
        yf_code = stem + ".T"
    print(f"\n  逆引き yf_code: {yf_code}")
    print(f"  yf_to_jquants({yf_code}): {yf_to_jquants(yf_code)}")

    print(f"\n[2] normalize:")
    try:
        df = normalize_minute_df(raw)
        print(f"  shape: {df.shape}, index type: {type(df.index).__name__}")
        if len(df) > 0:
            print(f"  index range: {df.index[0]} 〜 {df.index[-1]}")
    except Exception as e:
        import traceback
        print(f"  ✗ {type(e).__name__}: {e}")
        traceback.print_exc()

    print(f"\n[3] _load_local({yf_code}, 60):")
    r = _load_local(yf_code, 60)
    print(f"  result: {type(r).__name__ if r is not None else 'None'}")
    if r is not None:
        print(f"  shape: {r.shape}")
