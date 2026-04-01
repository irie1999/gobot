"""
直近の株価を確認するデバッグスクリプト
使い方: python check_prices.py
"""
import yfinance as yf
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
now_jst = datetime.now(JST)
print(f"現在時刻（JST）: {now_jst.strftime('%Y-%m-%d %H:%M')}")
print()

symbols = [
    ("7013.T", "IHI"),
    ("7011.T", "三菱重工業"),
]

for sym, name in symbols:
    print(f"── {name} ({sym}) ──")
    try:
        ticker = yf.Ticker(sym)
        dl_start = (now_jst - timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end   = (now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
        df = ticker.history(start=dl_start, end=dl_end, interval="1d", auto_adjust=True)
        if df.empty:
            print("  データなし")
        else:
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            # 全列を表示（Open/High/Low/Close/Volume等）
            print("[全列（直近5日）]")
            print(df.tail(5).to_string())
            # NaNを除いた最終行（有効データ）
            valid = df[df["Close"].notna()]
            if not valid.empty:
                last = valid.iloc[-1]
                print(f"\n  → 有効な最新日付: {valid.index[-1].date()}")
                print(f"     Close={last['Close']:.0f}  Open={last['Open']:.0f}  Volume={last['Volume']:.0f}")
            else:
                print("  → 有効データなし")
    except Exception as e:
        print(f"  エラー: {e}")
    print()
