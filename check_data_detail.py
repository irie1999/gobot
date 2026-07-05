"""data/ フォルダの詳細確認スクリプト"""
from pathlib import Path
import pickle
import csv as csvmod
import datetime

base = Path('.')
data_dir = base / 'data'

if not data_dir.exists():
    print("data/ フォルダが見つかりません")
    exit()

def try_read_csv_dates(path, max_rows=None):
    """CSVの先頭・末尾日付を取得"""
    try:
        with open(path, encoding='utf-8', newline='', errors='replace') as f:
            reader = csvmod.reader(f)
            rows = list(reader)
        if max_rows:
            rows = rows[:max_rows+1]
        if len(rows) < 2:
            return None, None, len(rows)-1
        header = rows[0]
        data = rows[1:]
        first = data[0][0] if data else ''
        last  = data[-1][0] if data else ''
        return first[:19], last[:19], len(data)
    except Exception as e:
        return None, None, f"error:{e}"

# ─────────────────────────────────────────
print("=" * 60)
print(" data/ フォルダ 詳細調査")
print("=" * 60)

for sub in sorted(data_dir.iterdir()):
    if not sub.is_dir():
        continue

    all_files = [f for f in sub.rglob('*') if f.is_file()]
    exts = {}
    for f in all_files:
        ext = f.suffix.lower()
        exts[ext] = exts.get(ext, 0) + 1
    total_kb = sum(f.stat().st_size for f in all_files) // 1024

    print(f"\n{'─'*60}")
    print(f"  📁 {sub.name}/  ({len(all_files)}ファイル, {total_kb:,}KB)")
    for ext, cnt in sorted(exts.items(), key=lambda x: -x[1]):
        print(f"     {ext or '(拡張子なし)'}: {cnt}件")

    # ── CSV の中身を確認 ──────────────────────────────
    csvs = sorted(sub.rglob('*.csv'))
    if csvs:
        print(f"\n  CSVサンプル (最初の5件):")
        oldest, newest = None, None
        for cf in csvs[:5]:
            s, e, cnt = try_read_csv_dates(cf)
            size_kb = cf.stat().st_size // 1024
            rel = cf.relative_to(data_dir)
            print(f"    {rel}  ({size_kb}KB, {cnt}行)")
            if s:
                print(f"      期間: {s} 〜 {e}")
                try:
                    sd = datetime.date.fromisoformat(s[:10])
                    ed = datetime.date.fromisoformat(e[:10])
                    if oldest is None or sd < oldest: oldest = sd
                    if newest is None or ed > newest: newest = ed
                except Exception:
                    pass

        # 全CSVの期間集計
        if len(csvs) > 5:
            for cf in csvs[5:]:
                s, e, _ = try_read_csv_dates(cf)
                if s:
                    try:
                        sd = datetime.date.fromisoformat(s[:10])
                        ed = datetime.date.fromisoformat(e[:10])
                        if oldest is None or sd < oldest: oldest = sd
                        if newest is None or ed > newest: newest = ed
                    except Exception:
                        pass

        if oldest:
            days = (newest - oldest).days
            print(f"\n  ★ CSV 全体期間: {oldest} 〜 {newest}  ({days}日 / 約{days//30}ヶ月)")

    # ── PKL の中身を確認 ──────────────────────────────
    pkls = sorted(sub.rglob('*.pkl'))
    if pkls:
        print(f"\n  PKLサンプル (最初の3件):")
        oldest, newest = None, None
        error_count = 0
        for pf in pkls[:3]:
            try:
                with open(pf, 'rb') as f:
                    obj = pickle.load(f)
                size_kb = pf.stat().st_size // 1024
                rel = pf.relative_to(data_dir)
                if hasattr(obj, 'shape'):
                    # DataFrame
                    idx = obj.index
                    if hasattr(idx, 'dtype') and 'datetime' in str(idx.dtype):
                        s = idx[0].date() if hasattr(idx[0], 'date') else idx[0]
                        e = idx[-1].date() if hasattr(idx[-1], 'date') else idx[-1]
                        print(f"    {rel}  ({size_kb}KB, {len(obj)}行): {s} 〜 {e}")
                        if oldest is None or s < oldest: oldest = s
                        if newest is None or e > newest: newest = e
                    else:
                        print(f"    {rel}  ({size_kb}KB, shape={obj.shape}, cols={list(obj.columns)[:4]})")
                else:
                    print(f"    {rel}  ({size_kb}KB, type={type(obj).__name__})")
            except Exception as ex:
                error_count += 1
                print(f"    {pf.name}: エラー {ex}")

        if error_count:
            print(f"    ※ エラー {error_count}件")

# ─────────────────────────────────────────
print(f"\n{'─'*60}")
print("  src/fx フォルダ確認")
fx_dir = base / 'src' / 'fx'
if fx_dir.exists():
    for f in sorted(fx_dir.rglob('*.py')):
        print(f"  {f.relative_to(base)}  ({f.stat().st_size//1024}KB)")
else:
    print("  src/fx が見つかりません")
