"""analyze_fill_rate.py — 実注文の約定率を測り、事前発注をいくら出すべきか決める。

背景:
  lssは予算400万に対し、約定するのは一部。約定率が分からないと『予算の何倍まで
  注文を出すか(over-subscribe倍率)』が決められない。
  倍率 = 予算 ÷ 金額ベース約定率。倍率が低すぎると資金が遊び、高すぎると
  上限キャンセルが間に合わず予算超過のリスクが出る。

やること:
  .\\fills が残す orders_<yyyyMMdd>.csv を全部読み、日ごとに
    ・売り注文の件数ベース約定率
    ・金額ベース約定率(約定額 ÷ 注文額)
    ・その日 予算を埋めるのに必要だった倍率
  を出し、全期間の分布(中央値/平均/最悪日)から推奨倍率を提示する。

使い方(swingtrade フォルダで):
  python analyze_fill_rate.py
  python analyze_fill_rate.py --budget 4000000
  python analyze_fill_rate.py --dir . --budget 4000000 --qty 100
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from datetime import datetime
from pathlib import Path

ap = argparse.ArgumentParser(description="実注文の約定率から over-subscribe 倍率を決める")
ap.add_argument("--dir", type=str, default=".", help="orders_*.csv の検索先")
ap.add_argument("--budget", type=float, default=4_000_000.0, help="1日の予算(円)")
ap.add_argument("--qty", type=int, default=100, help="1銘柄あたり株数(既定100)")
args = ap.parse_args()

files = sorted(Path(args.dir).glob("orders_*.csv"))
if not files:
    print(f"[ERROR] {Path(args.dir).resolve()} に orders_*.csv がありません。")
    print("        引け後に .\\fills を実行すると orders_<日付>.csv が残ります。")
    sys.exit(1)

days: list[dict] = []
for fp in files:
    stem = fp.stem.replace("orders_", "")
    if len(stem) != 8 or not stem.isdigit():
        continue
    try:
        d = datetime.strptime(stem, "%Y%m%d").date()
    except Exception:
        continue
    try:
        with open(fp, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        continue

    n_ord = n_fill = 0
    amt_ord = amt_fill = 0.0
    seen: set = set()
    for r in rows:
        if str(r.get("side", "")).strip() != "売":
            continue
        code = str(r.get("code", "")).strip()
        key = (code, str(r.get("recv", "")))
        if key in seen:
            continue
        seen.add(key)
        try:
            oq = int(float(r.get("order_qty") or 0))
            cq = int(float(r.get("cum_qty") or 0))
            op = float(r.get("order_price") or 0)
            fp_ = float(r.get("fill_price") or 0)
        except Exception:
            continue
        if oq <= 0 or op <= 0:
            continue
        n_ord += 1
        # 注文額は『指値下限』ではなくトリガー相当で見たいが、CSVにあるのは注文値。
        # 実運用の注文値は -3% 指値下限なので 0.97 で割り戻してトリガー相当にする。
        amt_ord += (op / 0.97) * oq
        if cq > 0:
            n_fill += 1
            amt_fill += (fp_ if fp_ > 0 else op) * cq

    if n_ord == 0:
        continue
    days.append({
        "date": d, "n_ord": n_ord, "n_fill": n_fill,
        "amt_ord": amt_ord, "amt_fill": amt_fill,
        "fr_n": n_fill / n_ord * 100,
        "fr_amt": (amt_fill / amt_ord * 100) if amt_ord > 0 else 0.0,
    })

if not days:
    print("[ERROR] 売り注文を含む orders_*.csv が見つかりませんでした。")
    sys.exit(1)

print("=" * 88)
print(f"実注文の約定率  ({len(days)}営業日 / 予算 {args.budget:,.0f}円)")
print("=" * 88)
print(f"{'日付':<12}{'売注文':>7}{'約定':>6}{'件数率':>8}"
      f"{'注文額':>13}{'約定額':>13}{'金額率':>8}{'必要倍率':>9}")
print("-" * 88)
for r in days:
    need = args.budget / (r["fr_amt"] / 100) / args.budget if r["fr_amt"] > 0 else 0
    print(f"{str(r['date']):<12}{r['n_ord']:>7}{r['n_fill']:>6}{r['fr_n']:>7.1f}%"
          f"{r['amt_ord']:>12,.0f}円{r['amt_fill']:>12,.0f}円{r['fr_amt']:>7.1f}%"
          f"{need:>8.2f}倍")
print("-" * 88)

fr_n = [r["fr_n"] for r in days]
fr_a = [r["fr_amt"] for r in days]
tot_o = sum(r["amt_ord"] for r in days)
tot_f = sum(r["amt_fill"] for r in days)
print(f"{'全期間':<12}{sum(r['n_ord'] for r in days):>7}"
      f"{sum(r['n_fill'] for r in days):>6}"
      f"{sum(r['n_fill'] for r in days) / sum(r['n_ord'] for r in days) * 100:>7.1f}%"
      f"{tot_o:>12,.0f}円{tot_f:>12,.0f}円{tot_f / tot_o * 100:>7.1f}%")

print(f"\n【金額ベース約定率の分布】")
print(f"  中央値 {statistics.median(fr_a):.1f}%  / 平均 {statistics.mean(fr_a):.1f}%"
      f"  / 最低 {min(fr_a):.1f}%  / 最高 {max(fr_a):.1f}%")
if len(fr_a) >= 2:
    print(f"  標準偏差 {statistics.stdev(fr_a):.1f}pt")

print(f"\n【推奨 over-subscribe 倍率】lss_budget_cap.py --budget-multiple に渡す値")
print("-" * 60)
med = statistics.median(fr_a)
mean_ = statistics.mean(fr_a)
worst = min(fr_a)
for lb, v in [("中央値ベース(標準)", med), ("平均ベース", mean_),
              ("最悪日ベース(保守)", worst)]:
    if v <= 0:
        continue
    print(f"  {lb:<20} 約定率{v:>5.1f}% → 倍率 {100 / v:>5.2f}倍 "
          f"(発注額 {args.budget * 100 / v:,.0f}円)")

print(f"\n【判断の目安】")
print(f"  ・倍率は『中央値ベース』から始めるのが無難。平常日は予算が埋まり、")
print(f"    急落日(約定率が跳ねる日)は上限キャンセルで頭打ちになる。")
print(f"  ・最悪日ベースまで上げると、平常日に予算を大きく超える注文を出すことになり、")
print(f"    上限キャンセルが間に合わないリスクが増える。")
print(f"  ・営業日が {len(days)}日 しかない場合はまだ判断材料が薄い。")
print(f"    最低10営業日、できれば20営業日ぶん貯めてから決めること。")
if len(days) < 10:
    print(f"\n  ⚠ 現在 {len(days)}営業日ぶんしかありません。毎日 .\\fills を実行して貯めてください。")
