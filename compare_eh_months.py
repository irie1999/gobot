r"""compare_eh_months.py — 現行 / E / H を**月ごとに対応をとって**比べる。

前提: HTML(レポート)を正とする
--------------------------------
2026-08-10 の点検で、E/H の優位を測っていた `compare_budget_raw.py` は
**レポートとは別の予算シミュ実装**だと分かった(必要資金が注文額か約定額か /
予算超過時に continue(貪欲) か break か / 不約定の重複排除の有無 / BTの出所)。
`run_oos_folds.py` の docstring どおり **ズレたときの正解はレポート側**なので、
判断はこのスクリプトで行う。

入力は `LSS_OOS_BUDGET_CSV` が吐く月別CSV。これは **`.\daily` と同じ
`_run_budget_sim`** が書くので、3方式が完全に同じ経路・同じ予算規則・同じ
BT下限・同じ発注順(流動性降順)・同じ重複規則で並ぶ。

使い方
------
  # ① 月別CSVを出しながらレポートを回す(1回で3方式ぶん出る)
  set LSS_OOS_BUDGET_CSV=eh_months.csv
  set LSS_OOS_BUDGET_DAYS=365
  .\daily --no-serve --days 365
  set LSS_OOS_BUDGET_CSV=
  set LSS_OOS_BUDGET_DAYS=

  # ② 比較
  python compare_eh_months.py --csv eh_months.csv

  # フォールドごとに回した場合(月がフォールドをまたいで重複しない)
  python run_oos_folds.py --workers 8 --lss-only
  python compare_eh_months.py --csv oos_budget_folds.csv

⚠ `.\daily` の月別行は per-symbol START_DATES により **月ごとに『その月までに
   選定されたペアだけ』**になる。つまりローリングOOSと同じプールで、
   run_oos_folds を回さなくても月別行はそのままOOS(18.29)。

⚠ 単月で判断しないこと。プール構成を少し変えるだけで単月は符号ごと変わる
   (実測: 2026/02 が -17,075 → +123,305)。見るのは合計・円/件・月次t の3つ。
"""
from __future__ import annotations

import argparse
import csv
import statistics as _st
import sys
from collections import defaultdict

ap = argparse.ArgumentParser(description="現行 / E / H を月ごとに対応比較する")
ap.add_argument("--csv", default="eh_months.csv", help="LSS_OOS_BUDGET_CSV が吐いたCSV")
ap.add_argument("--min-bt", type=float, default=None,
                help="BT層を1つに絞る。省略時はCSVにある最小のBT層を使う")
ap.add_argument("--holdout-days", type=int, default=None,
                help="holdout_days を1つに絞る。省略時はCSVにある最大値")
ap.add_argument("--drop-months", default="",
                help="除外する月(カンマ区切り)。当月など営業日が揃わない月に使う 例: 2026-08")
a = ap.parse_args()

try:
    with open(a.csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
except Exception as e:
    sys.exit(f"[error] {a.csv} を読めません: {e}\n"
             f"        先に LSS_OOS_BUDGET_CSV を立てて .\\daily を回してください。")
if not rows:
    sys.exit(f"[error] {a.csv} が空です")


def _f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


# ── 層を1つに固定する(混ぜると同じ月が何度も出て二重計上になる) ──────────
_hd = (a.holdout_days if a.holdout_days is not None
       else max(int(_f(r.get("holdout_days"))) for r in rows))
_bts = sorted({_f(r.get("min_bt")) for r in rows})
_bt = a.min_bt if a.min_bt is not None else (_bts[0] if _bts else 0.0)
_drop = {x.strip() for x in a.drop_months.split(",") if x.strip()}
rows = [r for r in rows
        if int(_f(r.get("holdout_days"))) == _hd and _f(r.get("min_bt")) == _bt
        and r.get("month") not in _drop]
if not rows:
    sys.exit(f"[error] holdout_days={_hd} / min_bt={_bt:.0f} に一致する行がありません")

_budget = {_f(r.get("budget_man")) for r in rows}
print(f"[条件] holdout_days={_hd}  BT>={_bt:.0f}  予算={'/'.join(f'{b:,.0f}万' for b in sorted(_budget))}"
      + (f"  除外月={','.join(sorted(_drop))}" if _drop else ""))

# ── sim_type ごとに月別へ畳む。フォールドの重複は後勝ちで1件に ──────────
NAMES = {"通常予算": "現行", "通常予算_E": "E", "通常予算_H": "H"}
data: dict = {v: {} for v in NAMES.values()}
for r in rows:
    key = NAMES.get(r.get("sim_type", ""))
    if key is None:
        continue
    data[key][r["month"]] = {"n": int(_f(r.get("trades"))),
                             "w": int(_f(r.get("wins"))),
                             "p": _f(r.get("pnl"))}

have = [k for k in ("現行", "E", "H") if data[k]]
if "現行" not in have:
    sys.exit("[error] sim_type=通常予算 の行がありません")
if len(have) == 1:
    print("[WARN] E/H の行がありません。レポート側で E/H タブが作られていない可能性"
          "(LSS_EH_TAB=0 / 5分足が無い / 古いコード)。git pull して再実行してください。")

months = sorted({m for k in have for m in data[k]})

# ── 月別テーブル ──────────────────────────────────────────────────────────
W = 13
print(f"\n■ 月別 (予算シミュはレポート内蔵 _run_budget_sim = .\\daily と同一経路)")
hdr = f"  {'月':<9}"
for k in have:
    hdr += f"{k + '件':>8}{k + '勝率':>8}{k:>{W}}"
print(hdr)
for m in months:
    line = f"  {m:<9}"
    for k in have:
        d = data[k].get(m)
        if not d:
            line += f"{'—':>8}{'—':>8}{'—':>{W}}"
            continue
        wr = d["w"] / d["n"] * 100 if d["n"] else 0.0
        line += f"{d['n']:>8,}{wr:>7.0f}%{d['p']:>+{W},.0f}"
    print(line)
line = f"  {'合計':<9}"
tot = {}
for k in have:
    n = sum(d["n"] for d in data[k].values())
    w = sum(d["w"] for d in data[k].values())
    p = sum(d["p"] for d in data[k].values())
    tot[k] = (n, w, p)
    line += f"{n:>8,}{(w / n * 100 if n else 0):>7.0f}%{p:>+{W},.0f}"
print(line)

print(f"\n■ 水準")
print(f"  {'方式':<6}{'件数':>9}{'勝率':>8}{'合計':>14}{'円/件':>10}"
      f"{'月平均':>12}{'σ':>11}{'t':>8}{'プラス月':>10}")
stats = {}
for k in have:
    n, w, p = tot[k]
    vals = [data[k][m]["p"] for m in months if m in data[k]]
    mu = _st.mean(vals) if vals else 0.0
    sd = _st.stdev(vals) if len(vals) > 1 else 0.0
    se = sd / (len(vals) ** 0.5) if len(vals) > 1 else 0.0
    t = mu / se if se > 0 else 0.0
    stats[k] = (mu, sd, t)
    print(f"  {k:<6}{n:>9,}{(w / n * 100 if n else 0):>7.0f}%{p:>+14,.0f}"
          f"{(p / n if n else 0):>+10,.0f}{mu:>+12,.0f}{sd:>11,.0f}{t:>+8.2f}"
          f"{sum(1 for v in vals if v > 0):>7}/{len(vals)}")

# ── 対応のある検定 ────────────────────────────────────────────────────────
pairs = [(x, y) for x, y in (("E", "現行"), ("H", "現行"), ("H", "E"))
         if x in have and y in have]
if pairs:
    print(f"\n■ 対応のある検定 (月ごとの差。相場全体の上下は差し引きで消える)")
    print(f"  {'比較':<12}{'平均差/月':>13}{'σ':>11}{'t':>8}"
          f"{'95%CI(月)':>28}{'勝ち月':>9}")
for x, y in pairs:
    ms = [m for m in months if m in data[x] and m in data[y]]
    diffs = [data[x][m]["p"] - data[y][m]["p"] for m in ms]
    if len(diffs) < 2:
        continue
    mu = _st.mean(diffs)
    sd = _st.stdev(diffs)
    se = sd / (len(diffs) ** 0.5)
    t = mu / se if se > 0 else 0.0
    lo, hi = mu - 1.96 * se, mu + 1.96 * se
    win = sum(1 for d in diffs if d > 0)
    print(f"  {x + ' vs ' + y:<12}{mu:>+13,.0f}{sd:>11,.0f}{t:>+8.2f}"
          f"{f'{lo:+,.0f} 〜 {hi:+,.0f}':>28}{win:>6}/{len(diffs)}")
    if lo > 0:
        print(f"      → **信頼区間が全域プラス。{x} が {y} より優れていると言える。**")
    elif hi < 0:
        print(f"      → **信頼区間が全域マイナス。{x} は {y} より劣る。**")
    else:
        print(f"      → CIがゼロをまたぐ。この月数では**差を検出できていない**。")

print(f"\n{'─' * 78}")
print("■ 読み方")
print(f"{'─' * 78}")
print("  ・3方式とも同じ _run_budget_sim / 同じBT下限 / 同じ発注順(流動性降順) /")
print("    同じ重複規則 / 同じ決済(sm・tm・損切り遅延・v17)。**違うのは注文の出し方だけ**。")
print("  ・**単月で判断しない**。プール構成を少し変えるだけで単月は符号ごと変わる")
print("    (実測: 2026/02 が -17,075 → +123,305)。見るのは 合計・円/件・月次t。")
print("  ・当月は営業日が揃わないので --drop-months で外すこと(例 --drop-months 2026-08)。")
print("  ⚠ このシムは slip=0。E/H は決済の約7割が損切りで、実運用の滑りは")
print("    無保護窓明けの成行買い戻しに集中する(18.17)。同じ損益なら stop の")
print("    少ないほうが実は良い。最終判断は .\\fills の実測で。")
