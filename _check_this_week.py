"""ローカルpklの最新データ確認"""
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"

try:
    from daytrade_symbols import DAYTRADE_SYMBOLS
    SYMBOLS = [(s.replace(".T", ""), n) for s, n in DAYTRADE_SYMBOLS]
except ImportError:
    SYMBOLS = []

today = datetime.now(JST).strftime("%Y-%m-%d")
print(f"今日: {today}")
print(f"確認対象: {len(SYMBOLS)}銘柄")
print("=" * 70)

# 今週の月曜日を計算
now = datetime.now(JST)
weekday = now.weekday()  # 0=Mon, 6=Sun
days_since_mon = weekday if weekday < 5 else weekday  # 週末なら先週末起点
this_monday = (now - timedelta(days=weekday)).strftime("%Y-%m-%d")
print(f"今週月曜: {this_monday}")
print()

results = []
for code4, name in SYMBOLS:
    code5 = code4 + "0"
    pkl_path = DATA_DIR / f"{code5}.pkl"
    if not pkl_path.exists():
        results.append((name, code4, "ファイルなし", 0))
        continue

    try:
        df = pickle.loads(pkl_path.read_bytes())
        if "Date" not in df.columns or df.empty:
            results.append((name, code4, "データなし", 0))
            continue

        # 日付リスト
        dates = sorted(set(str(d)[:10] for d in df["Date"].tolist()))
        latest = dates[-1]

        # 今週のデータ確認
        this_week_dates = [d for d in dates if d >= this_monday]

        results.append((name, code4, latest, len(this_week_dates)))
    except Exception as e:
        results.append((name, code4, f"エラー: {str(e)[:30]}", 0))

# 出力
print(f"{'銘柄':<18} {'コード':<6} {'最新日':<12} {'今週日数':>6}")
print("-" * 55)
for name, code, latest, week_days in results:
    marker = "●" if latest == "ファイルなし" else ("○" if week_days >= 4 else "△")
    print(f"  {marker} {name:<16} {code:<6} {latest:<12} {week_days:>3}日分")

# サマリー
print()
print("=" * 70)
total = len(results)
no_data = sum(1 for _, _, l, _ in results if l in ("ファイルなし", "データなし"))
full_week = sum(1 for _, _, _, w in results if w >= 5)
partial = sum(1 for _, _, _, w in results if 0 < w < 5)

print(f"  全体: {total}銘柄")
print(f"  今週5日分取得済み: {full_week}銘柄")
print(f"  今週一部取得: {partial}銘柄")
print(f"  データなし: {no_data}銘柄")

# 最新日の分布
latest_dates = {}
for _, _, l, _ in results:
    if l not in ("ファイルなし", "データなし"):
        latest_dates[l] = latest_dates.get(l, 0) + 1

print()
print("最新日の分布:")
for d in sorted(latest_dates.keys(), reverse=True):
    print(f"  {d}: {latest_dates[d]}銘柄")
