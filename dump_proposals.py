"""dump_proposals.py — 各 lss_proposal_YYYY-MM.py の SELECTED を1つのCSVに吸い出す。

optimize_merge.py が「任意の基準月組み合わせ」を再構築するための材料。
(base, code, strat) を proposals_selected.csv に出力するだけ。バックテストはしない。

使い方(あなたの機械・swingtradeフォルダで):
  python dump_proposals.py
  → proposals_selected.csv (base,code,strat) を出力
"""
from __future__ import annotations
import csv, glob, re, runpy
from pathlib import Path

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

with open("proposals_selected.csv", "w", newline="", encoding="utf-8-sig") as fp:
    w = csv.writer(fp)
    w.writerow(["base", "code", "strat"])
    w.writerows(rows)
print(f"[出力] proposals_selected.csv  {len(rows)}行  (基準月 {len(set(r[0] for r in rows))}種)")
