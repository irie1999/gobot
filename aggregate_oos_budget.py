"""aggregate_oos_budget.py — run_oos_folds.py が出す『レポート自身の予算タブ』を集計する。

なぜ必要か
-----------
lss の OOS 検証には経路が2つあり、値がズレることがある:

  A) 生CSV (oos_raw_fold*.csv) を sim_oos_budget.py で **再シミュ** した値
  B) レポート自身の予算タブ (LSS_OOS_BUDGET_CSV) = **`.\\daily` と同じ `_run_budget_sim`**

**ズレたら B が正**。実際に発注リストを作っているのは B のコードだから。
A は「発注ルールを差し替えて安く何度も試す」ためのもので、絶対値の正解ではない。

このスクリプトは B を読み、各フォールドの **OOS月だけ**(訓練期間の月は捨てる)を
積み上げて、BT閾値ごと・シムタイプごとの成績を出す。

  Fold 1: 訓練 2025-09        → OOS月 2025-10 の行だけ採用
  Fold 2: 訓練 2025-09〜10    → OOS月 2025-11 の行だけ採用
  ...

使い方:
  python run_oos_folds.py --workers 8            # oos_budget_folds.csv ができる
  python aggregate_oos_budget.py                 # 既定 oos_budget_folds.csv
  python aggregate_oos_budget.py --csv oos_budget_folds.csv --sim-type 通常予算
  python aggregate_oos_budget.py --by-month      # 月別の明細も出す
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser(description="run_oos_folds のレポート予算タブを集計")
ap.add_argument("--csv", default="oos_budget_folds.csv",
                help="run_oos_folds.py --budget-csv で出したCSV")
ap.add_argument("--sim-type", default="",
                help="シムタイプで絞る(例: 通常予算)。空=全部")
ap.add_argument("--by-month", action="store_true", help="月別の明細も出す")
ap.add_argument("--manifest", default="oos_folds_manifest.csv",
                help="run_oos_folds.py が出す台帳(fold,train_months,oos_month,raw_csv)。"
                     "**これがあれば最優先で使う**。ファイル名からの推測より確実")
ap.add_argument("--raw-glob", default="oos_raw_fold*.csv",
                help="台帳が無いときのフォールバック。ファイル名から fold→oos_month を推測する。"
                     "⚠ 開始月の違う過去実行のCSVが同居していると誤った月を拾う")
args = ap.parse_args()

p = Path(args.csv)
if not p.exists():
    sys.exit(f"[error] {p} がありません。先に `python run_oos_folds.py` を実行してください\n"
             f"        (--budget-csv を空にして実行した場合は出力されません)")

# ── fold → OOS月 の対応表 ────────────────────────────────────
# 予算CSVには fold 列が無い(レポートは自分がどのフォールドか知らない)。
# 生CSVのファイル名 oos_raw_fold<NN>_<YYYY-MM>.csv から対応を作る。
oos_months: list[str] = []
_mf = Path(args.manifest) if args.manifest else None
if _mf is not None and _mf.exists():
    for r in csv.DictReader(open(_mf, encoding="utf-8-sig")):
        mo = str(r.get("oos_month", "")).strip()
        if mo:
            oos_months.append(mo)
    print(f"[台帳] {_mf} から OOS月 {len(oos_months)}件: {', '.join(oos_months)}")
    # 途中から再開(--fold-from)した run の台帳は、その回に走らせたフォールドしか
    # 載らない。気付かずに集計すると『一部の月だけ』の結果を全体だと誤読するので警告。
    _n_raw = len(list(Path(".").glob(args.raw_glob)))
    if _n_raw > len(oos_months) + 1:
        print(f"  [!] 台帳{len(oos_months)}件に対し {args.raw_glob} が {_n_raw}件あります。")
        print(f"      --fold-from で途中再開した場合、台帳は再開ぶんしか載りません。")
        print(f"      全フォールドを集計したいなら台帳を消して再実行してください"
              f"(ファイル名からの推測に切り替わります):")
        print(f"        del {_mf}  →  python aggregate_oos_budget.py --by-month")
else:
    for f in sorted(Path(".").glob(args.raw_glob)):
        m = re.search(r"fold(\d+)_(\d{4})-?(\d{2})", f.name)
        if m:
            oos_months.append(f"{m.group(2)}-{m.group(3)}")
    if oos_months:
        print(f"[!] 台帳 {args.manifest} が無いのでファイル名から推測しました: "
              f"{', '.join(sorted(set(oos_months)))}")
        print(f"    開始月の違う過去実行の {args.raw_glob} が残っていると誤った月が混ざります。"
              f"心当たりがあれば古いCSVを退避してから再集計してください")
    else:
        print(f"[!] 台帳も {args.raw_glob} も見つからないので、CSV内の全月を対象にします")
oos_months = sorted(set(oos_months))

rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
if not rows:
    sys.exit(f"[error] {p} が空です")


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


# 予算CSVはフォールドごとに追記されるので、同じ (month, sim_type, min_bt) が
# 複数フォールドぶん入る。OOS月として正しいのは「その月をOOSとしたフォールド」の
# 1行だけ。追記順=フォールド順なので、各キーの **最初の出現** を採る。
seen: set = set()
picked: list[dict] = []
for r in rows:
    mo = str(r.get("month", "")).strip()
    if oos_months and mo not in oos_months:
        continue
    key = (mo, r.get("sim_type", ""), r.get("min_bt", ""))
    if key in seen:
        continue
    seen.add(key)
    picked.append(r)

if not picked:
    sys.exit("[error] OOS月に一致する行がありませんでした。--raw-glob を確認してください")

sim_types = sorted({r.get("sim_type", "") for r in picked})
if args.sim_type:
    sim_types = [s for s in sim_types if s == args.sim_type] or sim_types
bts = sorted({int(_f(r.get("min_bt"))) for r in picked})

print("=" * 84)
print(f"OOS集計(レポート自身の予算タブ = .\\daily と同じ計算経路)  {p.name}")
print(f"  OOS月 {len(set(r['month'] for r in picked))}件: "
      f"{', '.join(sorted(set(r['month'] for r in picked)))}")
print("=" * 84)

for st in sim_types:
    print(f"\n■ {st}")
    print(f"  {'BT閾値':<9}{'月数':>5}{'取引':>8}{'勝率':>7}{'合計損益':>15}{'月平均':>13}")
    print("  " + "-" * 58)
    for bt in bts:
        sub = [r for r in picked
               if r.get("sim_type") == st and int(_f(r.get("min_bt"))) == bt]
        if not sub:
            continue
        n_tr = sum(int(_f(r.get("trades"))) for r in sub)
        n_wi = sum(int(_f(r.get("wins"))) for r in sub)
        pnl = sum(_f(r.get("pnl")) for r in sub)
        wr = (n_wi / n_tr * 100) if n_tr else 0.0
        print(f"  BT>={bt:<6}{len(sub):>5}{n_tr:>8}{wr:>6.1f}%"
              f"{pnl:>+15,.0f}{pnl / max(len(sub), 1):>+13,.0f}")

if args.by_month:
    for st in sim_types:
        print(f"\n■ 月別: {st}")
        hdr = "".join(f"{('BT' + str(b)):>14}" for b in bts)
        print(f"  {'月':<9}{hdr}")
        print("  " + "-" * (9 + 14 * len(bts)))
        by_mo: dict = defaultdict(dict)
        for r in picked:
            if r.get("sim_type") != st:
                continue
            by_mo[r["month"]][int(_f(r.get("min_bt")))] = _f(r.get("pnl"))
        for mo in sorted(by_mo):
            print(f"  {mo:<9}" + "".join(f"{by_mo[mo].get(b, 0):>+14,.0f}" for b in bts))
        print("  " + "-" * (9 + 14 * len(bts)))
        tot = {b: sum(by_mo[m].get(b, 0) for m in by_mo) for b in bts}
        print(f"  {'合計':<9}" + "".join(f"{tot[b]:>+14,.0f}" for b in bts))

print("\n" + "─" * 84)
print("※ この数字が `.\\daily` の損益タブと一致するはず。ズレる場合はレポート側の設定"
      "(LSS_BT_TAB_MIN / LSS_STOP_DELAY_BARS / 予算)が daily.bat と違う。")
print("※ sim_oos_budget.py(生CSVの再シミュ)と食い違ったら **こちらが正**。")
