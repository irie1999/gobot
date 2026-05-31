"""prime銘柄のデータ網羅率を確認"""
import pickle
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"
today = datetime.now(JST).date()


def _yf_to_jq(yf_code: str) -> str:
    code = yf_code.replace(".T", "").strip()
    if len(code) == 4:
        return code + "0"
    return code


def get_first_date(pkl_path: Path) -> str | None:
    try:
        df = pickle.loads(pkl_path.read_bytes())
        if df is None or df.empty or "Date" not in df.columns:
            return None
        dates = df["Date"].astype(str).str[:10]
        return dates.min()
    except Exception:
        return None


# prime universe
from symbols_listed_all import SYMBOLS as PRIME
total = len(PRIME)

# 分類
has_data = []     # pkl 存在 (期間問わず)
missing = []      # pkl 未存在
duration_bins = defaultdict(int)  # 期間別カウント

for yf_code, name in PRIME:
    code5 = _yf_to_jq(yf_code)
    pkl = DATA_DIR / f"{code5}.pkl"
    if not pkl.exists():
        missing.append((yf_code, name))
        continue
    first = get_first_date(pkl)
    if first is None:
        missing.append((yf_code, name))
        continue
    has_data.append((yf_code, name, first))
    # 期間カテゴリ
    try:
        first_d = datetime.strptime(first, "%Y-%m-%d").date()
        days = (today - first_d).days
        if days >= 700:
            duration_bins["2年以上"] += 1
        elif days >= 365:
            duration_bins["1〜2年"] += 1
        elif days >= 180:
            duration_bins["6か月〜1年"] += 1
        elif days >= 90:
            duration_bins["3〜6か月"] += 1
        elif days >= 30:
            duration_bins["1〜3か月"] += 1
        else:
            duration_bins["1か月未満"] += 1
    except Exception:
        duration_bins["不明"] += 1

print("=" * 70)
print(f"  prime universe ({total}銘柄) のデータ状況")
print("=" * 70)
print(f"  pkl 存在: {len(has_data)}/{total} ({len(has_data)*100//total}%)")
print(f"  pkl 未取得: {len(missing)}/{total}")
print()
print("【期間別 内訳】")
for label in ["2年以上", "1〜2年", "6か月〜1年", "3〜6か月", "1〜3か月", "1か月未満"]:
    n = duration_bins.get(label, 0)
    bar = "█" * (n * 40 // max(1, max(duration_bins.values())))
    print(f"  {label:10}: {n:>5} 銘柄  {bar}")

print()
print("【取得期間別 winners抽出可能性】")
n_2y_plus = duration_bins.get("2年以上", 0)
n_1y_plus = n_2y_plus + duration_bins.get("1〜2年", 0)
n_6m_plus = n_1y_plus + duration_bins.get("6か月〜1年", 0)
n_3m_plus = n_6m_plus + duration_bins.get("3〜6か月", 0)
print(f"  730日 backtest 可能: {n_2y_plus} 銘柄  (PF信頼性 高)")
print(f"  365日 backtest 可能: {n_1y_plus} 銘柄  (推奨)")
print(f"  180日 backtest 可能: {n_6m_plus} 銘柄")
print(f"  90日  backtest 可能: {n_3m_plus} 銘柄  (新興含む)")

if missing:
    print()
    print(f"【未取得銘柄 (先頭20)】")
    for code, name in missing[:20]:
        print(f"  {code:10} {name}")
    if len(missing) > 20:
        print(f"  ... 他 {len(missing)-20}銘柄")
