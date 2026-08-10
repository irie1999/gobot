r"""compare_budget_raw.py — 2つの生トレードCSVを、同じ予算シミュに通して**対応をとって**比べる。

なぜ必要か
----------
`sweep_oos_budget.py` を2回走らせて月平均を並べても、差がノイズかどうか分からない。
A と B が **同じ日・同じ銘柄集団**を扱っているなら、月ごとに対応をとった検定
(paired t)のほうが桁違いに検出力が高い。相場全体の上下は両方に同じだけ効くので
差し引きで消える。

18.24 の作法「帯を作ってから判定する」は、比較対象が独立なとき用。
対応が取れるならペアで測るほうが正確。

使い方
------
  python compare_budget_raw.py --a "oos_raw_fold*.csv" --b e_raw.csv
  python compare_budget_raw.py --a "oos_raw_fold*.csv" --b e_raw.csv --budgets 400,600,800
  python compare_budget_raw.py --a ... --b ... --label-a 現行lss --label-b E寄り成行

⚠ 発注順・BT下限・転換の扱いは sim_oos_budget と同一の関数を使う(乖離防止)。
   シムタイプは実運用に最も近い通常予算(不約定も枠を消費)のみ。
"""
from __future__ import annotations

import argparse
import glob as _glob
import statistics as _st
import sys
from collections import defaultdict

try:
    from sim_oos_budget import _bt_pass, _order_key, load_raw_csv
except Exception as e:  # pragma: no cover
    sys.exit(f"[error] sim_oos_budget を import できません: {e}")

ap = argparse.ArgumentParser(description="2つの生CSVを同じ予算シミュで対応比較する")
ap.add_argument("--a", required=True, help="基準の生CSV(グロブ可)")
ap.add_argument("--b", required=True, help="比較対象の生CSV(グロブ可)")
ap.add_argument("--label-a", default="A")
ap.add_argument("--label-b", default="B")
ap.add_argument("--budgets", default="400,600,800")
ap.add_argument("--bt-min-a", type=float, default=30.0)
ap.add_argument("--bt-min-b", type=float, default=0.0,
                help="B 側のBT下限。E のように bt_score を持たない生CSVは 0")
a = ap.parse_args()


def _load(pat: str) -> list[dict]:
    fs = sorted(_glob.glob(pat)) if any(c in pat for c in "*?[") else [pat]
    if not fs:
        sys.exit(f"[error] {pat} に一致するファイルがありません")
    out: list[dict] = []
    for f in fs:
        out.extend(load_raw_csv(f))
    return out


def _sim_by_month(rows: list[dict], budget: float, bt_min: float) -> dict:
    """通常予算(不約定も枠消費)。OOS月ごとの損益と件数を返す。"""
    groups: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[(r["fold"], r["oos_month"])][r["entry_date"]].append(r)
    pnl: dict[str, float] = defaultdict(float)
    cnt: dict[str, int] = defaultdict(int)
    for (fold, om), by_date in groups.items():
        for _dt, day in by_date.items():
            cand = [t for t in day
                    if t.get("strategy") != "転換" and _bt_pass(t, bt_min)]
            cand.sort(key=_order_key)
            used = 0.0
            for t in cand:
                cost = t["entry_p"] * 100
                if cost <= 0:
                    cost = budget
                if used + cost > budget:
                    break
                used += cost
                if t["filled"] == 1:
                    pnl[om] += t["pnl"]
                    cnt[om] += 1
    return {"pnl": dict(pnl), "cnt": dict(cnt)}


A, B = _load(a.a), _load(a.b)
print(f"[入力] {a.label_a}: {len(A):,}行 (BT>={a.bt_min_a:.0f})  /  "
      f"{a.label_b}: {len(B):,}行 (BT>={a.bt_min_b:.0f})")

for bstr in [x for x in a.budgets.split(",") if x.strip()]:
    bm = float(bstr)
    ra = _sim_by_month(A, bm * 10_000, a.bt_min_a)
    rb = _sim_by_month(B, bm * 10_000, a.bt_min_b)
    months = sorted(set(ra["pnl"]) | set(rb["pnl"]))
    if not months:
        continue
    print(f"\n■ 予算 {bm:,.0f}万円 — 月ごとに対応をとった比較")
    print(f"  {'月':<10}{a.label_a + '件':>9}{a.label_a:>13}"
          f"{a.label_b + '件':>9}{a.label_b:>13}{'差 (B−A)':>13}")
    diffs = []
    for m in months:
        pa, pb = ra["pnl"].get(m, 0.0), rb["pnl"].get(m, 0.0)
        diffs.append(pb - pa)
        print(f"  {m:<10}{ra['cnt'].get(m, 0):>9,}{pa:>+13,.0f}"
              f"{rb['cnt'].get(m, 0):>9,}{pb:>+13,.0f}{pb - pa:>+13,.0f}")
    ta = sum(ra["pnl"].values())
    tb = sum(rb["pnl"].values())
    print(f"  {'合計':<10}{sum(ra['cnt'].values()):>9,}{ta:>+13,.0f}"
          f"{sum(rb['cnt'].values()):>9,}{tb:>+13,.0f}{tb - ta:>+13,.0f}")

    n = len(diffs)
    mu = _st.mean(diffs)
    sd = _st.stdev(diffs) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else 0.0
    t = mu / se if se > 0 else 0.0
    lo, hi = mu - 1.96 * se, mu + 1.96 * se
    win = sum(1 for x in diffs if x > 0)
    print(f"\n  対応のある検定 (月ごとの差 B−A / n={n})")
    print(f"    平均 {mu:+,.0f}円/月   σ {sd:,.0f}   **t = {t:+.2f}**")
    print(f"    95%CI  {lo:+,.0f} 〜 {hi:+,.0f} 円/月")
    print(f"    B が勝った月  {win}/{n}")
    if lo > 0:
        print(f"    → **信頼区間が全域プラス。B が優れていると言える。**")
    elif abs(t) < 2:
        print(f"    → t が2に届かない。この月数では**差を検出できていない**。")
    else:
        print(f"    → t は2を超えるが CI がゼロをまたぐ。判断保留。")

print(f"\n{'─' * 78}")
print("■ 読み方")
print(f"{'─' * 78}")
print("  ・A と B が同じ日・同じ銘柄集団を扱うなら、対応のある検定が最も検出力が高い。")
print("    相場全体の上下は両方に同じだけ効くので差し引きで消える。")
print("  ・**独立に月平均を並べて比べてはいけない**。月次σが10万円規模あるので、")
print("    5万円の差は独立比較では埋もれるが、対応をとれば見える(逆もある)。")
print("  ・勝った月の数も見ること。合計が1〜2ヶ月の大勝で作られていないか。")
print("  ⚠ このシムは slip=0。B の決済理由に stop が多いなら、実運用では成行の")
print("    滑りをより多く払う(18.17)。同じ損益なら stop の少ないほうが実は良い。")
