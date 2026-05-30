"""5分足データの詳細確認スクリプト"""
from pathlib import Path
import pickle
import csv as csvmod
import datetime
from collections import defaultdict

minute_dir = Path('data/minute_5m')

if not minute_dir.exists():
    print("data/minute_5m が見つかりません")
    exit()

pkls = sorted(minute_dir.glob('*.pkl'))
csvs = sorted(minute_dir.glob('*.csv'))

print(f"PKL: {len(pkls)}件  CSV: {len(csvs)}件\n")

# ── PKL の形式を調査 ──────────────────────────────────────────
print("=== PKL 形式調査 (最初の20件) ===")
formats = defaultdict(int)
oldest_pkl = None
newest_pkl = None
error_samples = []

for pkl_file in pkls:
    try:
        with open(pkl_file, 'rb') as f:
            df = pickle.load(f)

        if df is None or len(df) == 0:
            formats['empty'] += 1
            continue

        idx = df.index

        # 形式判定
        if hasattr(idx, 'dtype') and 'datetime' in str(idx.dtype):
            fmt = 'DatetimeIndex'
            s = idx[0]
            e = idx[-1]
            if hasattr(s, 'date'):
                s, e = s.date(), e.date()
        elif hasattr(idx[0], 'date'):
            fmt = 'DatetimeIndex(tz)'
            s = idx[0].date()
            e = idx[-1].date()
        else:
            # カラムから探す
            found = False
            for col in ['datetime', 'DateTime', 'date', 'Date', 'timestamp', 'Datetime']:
                if col in df.columns:
                    vals = df[col]
                    s = str(vals.iloc[0])[:10]
                    e = str(vals.iloc[-1])[:10]
                    fmt = f'column:{col}'
                    found = True
                    break
            if not found:
                fmt = f'unknown(idx={type(idx[0]).__name__},cols={list(df.columns)[:3]})'
                s, e = None, None

        formats[fmt] += 1

        if s and e:
            try:
                sd = datetime.date.fromisoformat(str(s))
                ed = datetime.date.fromisoformat(str(e))
                if oldest_pkl is None or sd < oldest_pkl:
                    oldest_pkl = sd
                if newest_pkl is None or ed > newest_pkl:
                    newest_pkl = ed
            except Exception:
                pass

    except Exception as ex:
        formats[f'error:{type(ex).__name__}'] += 1
        if len(error_samples) < 3:
            error_samples.append((pkl_file.name, str(ex)[:80]))

print("形式別カウント:")
for fmt, cnt in sorted(formats.items(), key=lambda x: -x[1]):
    print(f"  {fmt}: {cnt}件")

if error_samples:
    print("\nエラーサンプル:")
    for fname, err in error_samples:
        print(f"  {fname}: {err}")

if oldest_pkl:
    print(f"\nPKL データ期間: {oldest_pkl} 〜 {newest_pkl}")

# ── CSV の期間調査 ─────────────────────────────────────────────
print("\n=== CSV 期間調査 ===")
oldest_csv = None
newest_csv = None
csv_errors = 0
csv_shown = 0

for csv_file in csvs:
    try:
        with open(csv_file, encoding='utf-8', newline='') as f:
            reader = csvmod.reader(f)
            header = next(reader)
            rows = list(reader)

        if len(rows) < 2:
            continue

        # 日付カラム候補
        first_row = rows[0]
        last_row = rows[-1]

        # 先頭カラムが日付っぽいか確認
        s_str = first_row[0]
        e_str = last_row[0]

        try:
            # "2024-04-12 09:00:00" 形式
            sd = datetime.date.fromisoformat(s_str[:10])
            ed = datetime.date.fromisoformat(e_str[:10])

            if oldest_csv is None or sd < oldest_csv:
                oldest_csv = sd
            if newest_csv is None or ed > newest_csv:
                newest_csv = ed

            if csv_shown < 5:
                print(f"  {csv_file.stem}: {sd} 〜 {ed} ({len(rows)}行)")
                csv_shown += 1
        except ValueError:
            csv_errors += 1

    except Exception as ex:
        csv_errors += 1

if oldest_csv:
    days = (newest_csv - oldest_csv).days
    print(f"\nCSV データ期間: {oldest_csv} 〜 {newest_csv} ({days}日 / 約{days//30}ヶ月)")
if csv_errors:
    print(f"CSV 読み込みエラー: {csv_errors}件")

# ── まとめ ──────────────────────────────────────────────────
print("\n=== まとめ ===")
all_oldest = [d for d in [oldest_pkl, oldest_csv] if d]
all_newest = [d for d in [newest_pkl, newest_csv] if d]
if all_oldest:
    total_oldest = min(all_oldest)
    total_newest = max(all_newest)
    days = (total_newest - total_oldest).days
    print(f"★ 全体データ期間: {total_oldest} 〜 {total_newest}")
    print(f"★ 期間日数      : {days}日（約{days//30}ヶ月）")
