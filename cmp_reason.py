"""base と delay1 の決済理由内訳を比べる。sweep_lss_smtm が出した2つのCSVを読むだけ。

仮説: TRAIN窓で delay1 の『close(引け決済=損切りが一度も発火しなかった)』が
      異常に多いなら、損切りが armしていない = 実質ノーストップになっている。
      §18.1「損切りを引けまで持つ = -21% 論外」と整合する。
"""
import csv, sys
from pathlib import Path

def load(p):
    d = {}
    if not Path(p).exists():
        sys.exit(f"[error] {p} が見つかりません")
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        d[(r["sm"], r["tm"], r["phase"])] = r
    return d

B, D = load("sweep_lss_smtm_results_base.csv"), load("sweep_lss_smtm_results_delay1.csv")
print(f"  {'窓':<10}{'件数':>7}{'':>3}{'target':>16}{'stop':>16}{'close(引け)':>18}")
print(f"  {'':<10}{'':>7}{'':>3}{'base→delay1':>16}{'base→delay1':>16}{'base→delay1':>18}")
print("  " + "-" * 74)
for ph in ("TRAIN60", "TEST60", "TRAIN120", "TEST120", "TRAIN180", "TEST180"):
    k = ("0.1", "1.0", ph)
    if k not in B or k not in D:
        continue
    b, d = B[k], D[k]
    n = int(b["trades"])
    def cell(f):
        x, y = int(b[f]), int(d[f])
        return f"{x:,}→{y:,}"
    print(f"  {ph:<10}{n:>7,}{'':>3}{cell('target'):>16}{cell('stop'):>16}"
          f"{cell('close'):>18}")
    _bc, _dc = int(b["close"]), int(d["close"])
    if n:
        print(f"  {'':<10}{'':>7}{'':>3}{'':>16}{'':>16}"
              f"{f'{_bc/n*100:.0f}% → {_dc/n*100:.0f}%':>18}")
