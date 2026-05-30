"""データ保存状況を確認するスクリプト"""
from pathlib import Path
import datetime

base = Path('.')

print('=== CSVファイル ===')
csvs = sorted(base.rglob('*.csv'))
if csvs:
    for f in csvs:
        print(f'  {f}  ({f.stat().st_size//1024}KB)')
else:
    print('  (なし)')

print('\n=== PKLキャッシュ (.rsi2_cache) ===')
cache = base / '.rsi2_cache'
if cache.exists():
    files = sorted(cache.glob('*.pkl'))
    print(f'  ファイル数: {len(files)}件')
    if files:
        times = [f.stat().st_mtime for f in files]
        oldest = datetime.datetime.fromtimestamp(min(times))
        newest = datetime.datetime.fromtimestamp(max(times))
        print(f'  最古: {oldest.strftime("%Y-%m-%d")}')
        print(f'  最新: {newest.strftime("%Y-%m-%d")}')
else:
    print('  (なし)')

print('\n=== dataフォルダ ===')
data = base / 'data'
if data.exists():
    for f in sorted(data.rglob('*'))[:30]:
        size = f'({f.stat().st_size//1024}KB)' if f.is_file() else ''
        print(f'  {f}  {size}')
else:
    print('  (なし)')

print('\n=== その他のデータファイル ===')
for ext in ['*.parquet', '*.h5', '*.db', '*.sqlite']:
    for f in sorted(base.rglob(ext)):
        print(f'  {f}  ({f.stat().st_size//1024}KB)')
