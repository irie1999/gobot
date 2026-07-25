"""dump_proposals.py — 各 lss_proposal_YYYY-MM.py の SELECTED を1つのCSVに吸い出す。

optimize_merge.py / マージ最適化が「任意の基準月組み合わせ」を再構築するための材料。
(base, code, strat) を proposals_selected.csv に出力するだけ。バックテストはしない。

使い方(あなたの機械・swingtradeフォルダで):
  python dump_proposals.py
  → C:\\Users\\to732\\OneDrive\\ドキュメント\\tmp\\基準月まとめ\\proposals_selected.csv に出力
  python dump_proposals.py --out-dir D:\\somewhere   # 出力先を変える
"""
from __future__ import annotations
import argparse, csv, glob, re, runpy
from pathlib import Path

_DEFAULT_OUT = r"C:\Users\to732\OneDrive\ドキュメント\tmp\基準月まとめ"

ap = argparse.ArgumentParser(description="lss_proposal の SELECTED を1CSVに吸い出す")
ap.add_argument("--out-dir", type=str, default=_DEFAULT_OUT, help="CSV出力フォルダ")
args = ap.parse_args()

out_dir = Path(args.out_dir)
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / "proposals_selected.csv"

rows = []
for f in sorted(glob.glob("lss_proposal_????-??.py")):
    m = re.search(r"(\d{4}-\d{2})", Path(f).name)
    if not m:
        continue
    base = m.group(1)
    try:
        sel = runpy.run_path(f).get("SELECTED") or []
    except Exception as e:
        print(f"[skip] {f}: {e}")
        continue
    for r in sel:
        if len(r) >= 3:
            rows.append((base, str(r[0]), str(r[2])))
    print(f"[読込] {f}: {len(sel)}件 (base={base})")

with open(out_path, "w", newline="", encoding="utf-8-sig") as fp:
    w = csv.writer(fp)
    w.writerow(["base", "code", "strat"])
    w.writerows(rows)
print(f"[出力] {out_path}  {len(rows)}行  (基準月 {len(set(r[0] for r in rows))}種)")
