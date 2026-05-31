"""yfinance 直接テスト - 何が返ってくるか確認"""
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

symbols = ["5803.T", "1605.T", "6407.T", "5332.T", "6594.T"]
print(f"{'銘柄':10} {'件数':>6} {'最古':>20} {'最新':>20}")
print("-" * 60)

for sym in symbols:
    try:
        df = yf.download(sym, period="30d", interval="5m",
                         progress=False, threads=False, auto_adjust=False)
        if df.empty:
            print(f"{sym:10} {'EMPTY':>6}")
            continue
        # MultiIndex対策
        if hasattr(df.columns, "get_level_values"):
            cols = df.columns.tolist()
        print(f"{sym:10} {len(df):>6} {str(df.index[0])[:19]:>20} {str(df.index[-1])[:19]:>20}")
    except Exception as e:
        print(f"{sym:10} ERR: {type(e).__name__}: {str(e)[:50]}")

print()
print("各銘柄の最新バー (yfinance 直接):")
for sym in symbols:
    try:
        df = yf.download(sym, period="7d", interval="5m",
                         progress=False, threads=False, auto_adjust=False)
        if df.empty:
            continue
        print(f"\n{sym} 最後の3バー:")
        print(df.tail(3))
    except Exception as e:
        print(f"{sym} ERR: {e}")
