"""データ保存状況を確認するスクリプト"""
from pathlib import Path
import datetime
import pickle
import csv as csvmod

base = Path('.')

# ──────────────────────────────────────────────────────────
print('=== CSVファイル (walkforward以外) ===')
csvs = [f for f in sorted(base.rglob('*.csv')) if 'walkforward' not in str(f) and '.git' not in str(f)]
if csvs:
    for f in csvs[:10]:
        print(f'  {f}  ({f.stat().st_size//1024}KB)')
    if len(csvs) > 10:
        print(f'  ... 他 {len(csvs)-10} 件')
else:
    print('  (なし)')

# ──────────────────────────────────────────────────────────
print('\n=== PKLキャッシュ (.rsi2_cache) 日足データ ===')
cache = base / '.rsi2_cache'
if cache.exists():
    files = sorted(cache.glob('*.pkl'))
    print(f'  ファイル数: {len(files)}件')
    if files:
        times = [f.stat().st_mtime for f in files]
        oldest = datetime.datetime.fromtimestamp(min(times))
        newest = datetime.datetime.fromtimestamp(max(times))
        print(f'  最古更新日: {oldest.strftime("%Y-%m-%d")}')
        print(f'  最新更新日: {newest.strftime("%Y-%m-%d")}')
        sample = files[0]
        try:
            with open(sample, 'rb') as f:
                df = pickle.load(f)
            idx = df.index
            if hasattr(idx[0], 'date'):
                start, end = idx[0].date(), idx[-1].date()
            else:
                start, end = idx[0], idx[-1]
            print(f'  サンプル({sample.stem}): {start} 〜 {end} ({len(df)}行)')
        except Exception as e:
            print(f'  サンプル読み込みエラー: {e}')
else:
    print('  (なし)')

# ──────────────────────────────────────────────────────────
print('\n=== data/ フォルダ構成 ===')
data_dir = base / 'data'
if data_dir.exists():
    for sub in sorted(data_dir.iterdir()):
        if sub.is_dir():
            files_in = list(sub.rglob('*'))
            files_only = [f for f in files_in if f.is_file()]
            size_total = sum(f.stat().st_size for f in files_only) // 1024
            print(f'  {sub.name}/  ({len(files_only)}ファイル, {size_total}KB)')
        else:
            print(f'  {sub.name}  ({sub.stat().st_size//1024}KB)')
else:
    print('  (なし)')

# ──────────────────────────────────────────────────────────
print('\n=== data/minute_5m  5分足データ ===')
minute_dir = base / 'data' / 'minute_5m'
if minute_dir.exists():
    csvs_m = sorted(minute_dir.glob('*.csv'))
    pkls_m = sorted(minute_dir.glob('*.pkl'))
    print(f'  CSVファイル数: {len(csvs_m)}件')
    print(f'  PKLファイル数: {len(pkls_m)}件')

    oldest_dt = None
    newest_dt = None
    error_count = 0

    def _get_date_range_pkl(path):
        with open(path, 'rb') as f:
            df = pickle.load(f)
        if df is None or len(df) == 0:
            return None, None
        idx = df.index
        # DatetimeIndex かチェック
        if hasattr(idx, 'dtype') and 'datetime' in str(idx.dtype):
            s = idx[0]
            e = idx[-1]
        elif hasattr(idx[0], 'date'):
            s = idx[0]
            e = idx[-1]
        else:
            # カラムから探す
            for col in ['datetime', 'DateTime', 'date', 'Date', 'timestamp']:
                if col in df.columns:
                    s = df[col].iloc[0]
                    e = df[col].iloc[-1]
                    break
            else:
                return None, None
        if hasattr(s, 'date'):
            s = s.date()
            e = e.date()
        return s, e

    shown = 0
    for pkl_file in pkls_m[:5]:  # 最初の5件をサンプル表示
        try:
            s, e = _get_date_range_pkl(pkl_file)
            if s:
                print(f'    {pkl_file.stem}: {s} 〜 {e}')
                shown += 1
                if oldest_dt is None or s < oldest_dt: oldest_dt = s
                if newest_dt is None or e > newest_dt: newest_dt = e
        except Exception as ex:
            error_count += 1

    # 残りは日付だけ収集
    for pkl_file in pkls_m[5:]:
        try:
            s, e = _get_date_range_pkl(pkl_file)
            if s:
                if oldest_dt is None or s < oldest_dt: oldest_dt = s
                if newest_dt is None or e > newest_dt: newest_dt = e
        except Exception:
            error_count += 1

    if oldest_dt:
        print(f'\n  ★ データ期間: {oldest_dt} 〜 {newest_dt}')
        if hasattr(oldest_dt, '__sub__'):
            try:
                days = (newest_dt - oldest_dt).days
                print(f'  ★ 期間日数  : {days}日（約{days//30}ヶ月）')
            except Exception:
                pass
    if error_count:
        print(f'  ※ 読み込みエラー: {error_count}件')

    # CSV も確認
    if csvs_m and oldest_dt is None:
        sample_csv = csvs_m[0]
        try:
            with open(sample_csv, encoding='utf-8') as f:
                rows = list(csvmod.reader(f))
            if len(rows) > 2:
                print(f'  CSV先頭({sample_csv.stem}): {rows[1][0]} 〜 {rows[-1][0]} ({len(rows)-1}件)')
        except Exception as e:
            print(f'  CSV読み込みエラー: {e}')
else:
    print('  (なし)')

# ──────────────────────────────────────────────────────────
print('\n=== data/raw  生データ (J-Quantsなど) ===')
raw_dir = base / 'data' / 'raw'
if raw_dir.exists():
    all_raw = [f for f in raw_dir.rglob('*') if f.is_file()]
    print(f'  総ファイル数: {len(all_raw)}件')
    # 拡張子別集計
    from collections import Counter
    exts = Counter(f.suffix for f in all_raw)
    for ext, cnt in exts.most_common(5):
        print(f'    {ext or "(no ext)"}: {cnt}件')
    # 最古/最新ファイル
    if all_raw:
        times_raw = [(f.stat().st_mtime, f) for f in all_raw]
        newest_f = max(times_raw, key=lambda x: x[0])[1]
        oldest_f = min(times_raw, key=lambda x: x[0])[1]
        print(f'  最古ファイル: {oldest_f.name}  ({datetime.datetime.fromtimestamp(oldest_f.stat().st_mtime).date()})')
        print(f'  最新ファイル: {newest_f.name}  ({datetime.datetime.fromtimestamp(newest_f.stat().st_mtime).date()})')
else:
    print('  (なし)')

# ──────────────────────────────────────────────────────────
print('\n=== その他のデータファイル ===')
found = False
for ext in ['*.parquet', '*.h5', '*.db', '*.sqlite']:
    for f in sorted(base.rglob(ext)):
        if '.git' not in str(f):
            print(f'  {f}  ({f.stat().st_size//1024}KB)')
            found = True
if not found:
    print('  (なし)')
