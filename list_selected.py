"""
list_selected.py — 指定基準日の選定銘柄(WFテスト合格)を戦略別に一覧表示する。

使い方:
  python list_selected.py 2018-06-30
  python list_selected.py 2018-06-30 --holdout 30 --top 3
"""
import argparse
import csv
import glob

ap = argparse.ArgumentParser()
ap.add_argument("date", help="基準日 YYYY-MM-DD (例: 2018-06-30)")
ap.add_argument("--holdout", type=int, default=30, help="holdout日数 (既定30)")
ap.add_argument("--top", type=int, default=3, help="戦略ごとの表示数 (既定3)")
args = ap.parse_args()

pat = f"walkforward_results/*_holdout{args.holdout}d_{args.date}.csv"
files = sorted(glob.glob(pat))
if not files:
    print(f"[なし] {pat} に一致するCSVがありません。")
    raise SystemExit(1)

print(f"=== 選定銘柄 (基準日 {args.date} / holdout{args.holdout}d / TEST損益>0 & folds>=2) ===\n")
print(f"{'戦略':<10}{'銘柄':<10}{'名前':<16}{'選定TEST損益':>13}{'PF':>6}{'勝率':>6}{'fold':>5}")
print("-" * 70)
for f in files:
    stem = f.split("walkforward_")[1]
    strat = stem.split("_holdout")[0]
    try:
        rows = list(csv.DictReader(open(f, encoding="utf-8")))
    except Exception:
        continue
    ok = [r for r in rows
          if float(r.get("total_test_pnl", 0) or 0) > 0
          and float(r.get("folds_passed", 0) or 0) >= 2]
    ok.sort(key=lambda r: -float(r.get("total_test_pnl", 0) or 0))
    for r in ok[: args.top]:
        print(f"{strat:<10}{r.get('symbol',''):<10}{(r.get('name','') or '')[:15]:<16}"
              f"{float(r.get('total_test_pnl',0) or 0):>+13.0f}"
              f"{float(r.get('avg_test_pf',0) or 0):>6.2f}"
              f"{float(r.get('avg_test_wr',0) or 0):>5.0f}%"
              f"{int(float(r.get('folds_passed',0) or 0)):>5}")

print("\n→ この中から1つ選び:  python dump_history.py --symbol <銘柄> --strategy <戦略>")
