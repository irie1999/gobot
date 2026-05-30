"""データ保存状況を確認するスクリプト"""
from pathlib import Path
import datetime
import pickle

base = Path('.')

print('=== CSVファイル (walkforward以外) ===')
csvs = [f for f in sorted(base.rglob('*.csv')) if 'walkforward' not in str(f)]
if csvs:
    for f in csvs:
        print(f'  {f}  ({f.stat().st_size//1024}KB)')
else:
    print('  (なし)')

print('\n=== PKLキャッシュ (.rsi2_cache) 日足データ ===')
cache = base / '.rsi2_cache'
if cache.exists():
    files = sorted(cache.glob('*.pkl'))
    print(f'  ファイル数: {len(files)}件')
    if files:
        times = [f.stat().st_mtime for f in files]
        oldest = datetime.datetime.fromtimestamp(min(times))
        newest = datetime.datetime.fromtimestamp(max(times))
        print(f'  最古作成日: {oldest.strftime("%Y-%m-%d")}')
        print(f'  最新作成日: {newest.strftime("%Y-%m-%d")}')
        # サンプルで1件開いてデータ期間を確認
        sample = files[0]
        try:
            with open(sample, 'rb') as f:
                df = pickle.load(f)
            print(f'  サンプル({sample.stem}): {df.index[0].date()} 〜 {df.index[-1].date()} ({len(df)}行)')
        except Exception as e:
            print(f'  サンプル読み込みエラー: {e}')
else:
    print('  (なし)')

print('\n=== data/minute_5m 5分足データ ===')
minute_dir = base / 'data' / 'minute_5m'
if minute_dir.exists():
    csvs = sorted(minute_dir.glob('*.csv'))
    pkls = sorted(minute_dir.glob('*.pkl'))
    print(f'  CSVファイル数: {len(csvs)}件')
    print(f'  PKLファイル数: {len(pkls)}件')

    # 全PKLのデータ期間を確認
    oldest_dt = None
    newest_dt = None
    sample_shown = 0
    for pkl_file in pkls:
        try:
            with open(pkl_file, 'rb') as f:
                df = pickle.load(f)
            if hasattr(df, 'index') and len(df) > 0:
                start = df.index[0]
                end   = df.index[-1]
                if hasattr(start, 'date'):
                    start = start.date()
                    end   = end.date()
                if oldest_dt is None or start < oldest_dt:
                    oldest_dt = start
                if newest_dt is None or end > newest_dt:
                    newest_dt = end
                if sample_shown < 3:
                    print(f'  {pkl_file.stem}: {start} 〜 {end} ({len(df)}件)')
                    sample_shown += 1
        except Exception:
            pass

    if oldest_dt:
        print(f'\n  ★ データ期間: {oldest_dt} 〜 {newest_dt}')
        days = (newest_dt - oldest_dt).days
        print(f'  ★ 期間日数: {days}日（約{days//30}ヶ月）')

    # CSVのサンプルも確認
    if csvs and sample_shown == 0:
        import csv as csvmod
        sample_csv = csvs[0]
        try:
            with open(sample_csv, encoding='utf-8') as f:
                rows = list(csvmod.reader(f))
            if len(rows) > 2:
                print(f'  CSVサンプル({sample_csv.stem}): {rows[1][0]} 〜 {rows[-1][0]} ({len(rows)-1}件)')
        except Exception as e:
            print(f'  CSVサンプルエラー: {e}')
else:
    print('  (なし)')

print('\n=== その他のデータファイル ===')
found = False
for ext in ['*.parquet', '*.h5', '*.db', '*.sqlite']:
    for f in sorted(base.rglob(ext)):
        if '.git' not in str(f):
            print(f'  {f}  ({f.stat().st_size//1024}KB)')
            found = True
if not found:
    print('  (なし)')
