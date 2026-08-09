"""監視銘柄の5分足データ最新日を確認"""
import pickle
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

try:
    from daytrade_symbols import DAYTRADE_SYMBOLS
    targets = [(s.replace(".T", "") + "0", s) for s, _ in DAYTRADE_SYMBOLS]
except ImportError:
    targets = []

if not targets:
    print("daytrade_symbols.py が見つかりません")
    raise SystemExit(1)

print(f"{'コード':<10} {'銘柄':<8} {'最新日':<12} {'バー数':>6}")
print("-" * 45)

for code5, sym in targets:
    pkl = DATA_DIR / f"{code5}.pkl"
    if not pkl.exists():
        print(f"{sym:<10} {code5:<8} ファイルなし")
        continue
    df = pickle.loads(pkl.read_bytes())
    if df is None or df.empty:
        print(f"{sym:<10} {code5:<8} データなし")
        continue
    last = str(df["Date"].max())[:10] if "Date" in df.columns else "不明"
    print(f"{sym:<10} {code5:<8} {last:<12} {len(df):>6}本")
