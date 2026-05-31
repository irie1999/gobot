"""winners 各銘柄の最終日確認"""
from daytrade_donchian_winners import SYMBOLS
from daytrade_data import _load_local
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
today = datetime.now(JST).date()

print(f"今日: {today}")
print(f"対象: {len(SYMBOLS)}銘柄")
print()
print(f"{'コード':8} {'銘柄':18} {'最終日':18} {'経過'}")
print("-" * 60)

stale_count = 0
for sym, name in SYMBOLS:
    df = _load_local(sym, 60)
    if df is None or df.empty:
        print(f"{sym:8} {name[:16]:18} NONE                 -")
        stale_count += 1
        continue
    last = df.index[-1]
    days_ago = (today - last.date()).days
    mark = "★OK" if days_ago <= 3 else ("OK" if days_ago <= 7 else "✗古い")
    print(f"{sym:8} {name[:16]:18} {str(last)[:16]:18} {days_ago:3}日前 {mark}")
    if days_ago > 7:
        stale_count += 1

print()
print(f"古いデータ (7日以上前): {stale_count}/{len(SYMBOLS)}銘柄")
