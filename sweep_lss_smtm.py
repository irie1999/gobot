"""sweep_lss_smtm.py — 損切り(sm)/利確(tm)を TRAIN/TEST に分けてスイープする。

なぜ必要か
-----------
lss の決済は 損切ATR=0.1 / 利確ATR=1.0 = 名目 1:10。ところが実測(lss_exit_breakdown.py)では

    目標達成の平均勝ち  +5,246円  → 1 ATR x 100株 ≒ 5,246円
    損切りの平均       -3,091円  → 名目 0.1ATR = 約 -520円 のはずが **約6倍深い**

損切りだけで -10,387,629円 を出しており、目標達成 +7,777,625 と引け +2,196,498 を
食い潰している。**0.1 を狙っても 0.6 で約定するなら、0.1 という設定自体に意味が無い。**

実際、`--sm 0.3 --tm 1.5` で回すと、現行設定ではマイナスだった期間
(2025-04〜2026-04)が net現実 +1,623,458円 になった。ただしこれは1点の観測なので、
**グリッドで TRAIN/TEST を分けて確かめる必要がある**。

  ⛔ 単に一番良い (sm, tm) を選ぶのは in-sample フィット。CLAUDE.md 18.10 で
     同じ失敗をしている(TRAIN改善・OOS悪化)。必ず TRAIN で選んで TEST で確認する。

やること
--------
グリッドの各 (sm, tm) について compare_lss_rules.py を2回走らせる:

  TRAIN … --days <train-days> --holdout-days <holdout>   (直近を除いた期間)
  TEST  … --days <holdout>                                (その直近だけ)

`base` 行の net現実 / net保守 を拾って並べる。**TRAIN の最良点が TEST でも
生きているか**が唯一の判断材料。TEST がフラットならパラメータに意味は無い。

使い方
------
  python sweep_lss_smtm.py                       # 既定グリッド(4x3=12点)
  python sweep_lss_smtm.py --sm-list 0.1,0.2,0.3,0.5 --tm-list 1.0,1.5,2.0
  python sweep_lss_smtm.py --holdout-days 120    # TEST に使う直近日数
  python sweep_lss_smtm.py --dry-run             # 実行せずコマンドだけ表示

途中で止めても、結果は sweep_lss_smtm_results.csv に都度追記されるので
**再実行すれば済んだ組み合わせは飛ばす**(重いので再開できるようにしてある)。
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser(description="lss の sm/tm を TRAIN/TEST に分けてスイープ")
ap.add_argument("--sm-list", type=str, default="0.1,0.2,0.3,0.5",
                help="損切りATR倍率のリスト(カンマ区切り)")
ap.add_argument("--tm-list", type=str, default="1.0,1.5,2.0",
                help="利確ATR倍率のリスト(カンマ区切り)")
ap.add_argument("--train-days", type=int, default=365, help="TRAIN の遡及日数")
ap.add_argument("--holdout-days", type=int, default=120,
                help="TEST に回す直近日数。TRAIN からはこの日数が除外される")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--out", type=str, default="sweep_lss_smtm_results.csv")
ap.add_argument("--dry-run", action="store_true", help="実行せずコマンドだけ表示")
ap.add_argument("--extra", type=str, default="", help="compare_lss_rules に渡す追加引数")
args = ap.parse_args()

SMS = [float(x) for x in args.sm_list.split(",") if x.strip()]
TMS = [float(x) for x in args.tm_list.split(",") if x.strip()]
OUT = Path(args.out)

_COLS = ["sm", "tm", "phase", "trades", "win_rate", "pf",
         "target", "stop", "close", "net_real", "net_cons"]


def _load_done() -> dict:
    done = {}
    if OUT.exists():
        try:
            for r in csv.DictReader(open(OUT, encoding="utf-8-sig")):
                done[(r["sm"], r["tm"], r["phase"])] = r
        except Exception:
            pass
    return done


def _save(rows: list[dict]) -> None:
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _run(sm: float, tm: float, phase: str) -> dict | None:
    """compare_lss_rules を1回走らせ、base 行の成績を返す。"""
    cmd = [sys.executable, "compare_lss_rules.py",
           "--min-price", str(args.min_price), "--max-price", str(args.max_price),
           "--bt-min", "0", "--workers", str(args.workers),
           "--sm", str(sm), "--tm", str(tm)]
    if phase == "TRAIN":
        cmd += ["--days", str(args.train_days), "--holdout-days", str(args.holdout_days)]
    else:
        cmd += ["--days", str(args.holdout_days)]
    if args.extra.strip():
        cmd += args.extra.split()

    print(f"\n{'=' * 70}\n▶ sm={sm} tm={tm} [{phase}]  {' '.join(cmd[1:])}", flush=True)
    if args.dry_run:
        return None

    _before = {p: p.stat().st_mtime for p in Path(".").glob("compare_lss_rules_*.csv")}
    t0 = time.time()
    try:
        pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    except Exception as e:
        print(f"  [error] 実行に失敗: {e}")
        return None
    if pr.returncode != 0:
        print(f"  [error] compare_lss_rules が異常終了 (rc={pr.returncode})")
        print((pr.stderr or pr.stdout or "")[-1500:])
        return None

    # 新しく書かれた/更新された CSV を探す
    cands = [p for p in Path(".").glob("compare_lss_rules_*.csv")
             if p.stat().st_mtime > _before.get(p, 0)]
    if not cands:
        print("  [error] 出力CSVが見つかりません。compare_lss_rules の出力を確認してください")
        print((pr.stdout or "")[-800:])
        return None
    csv_p = max(cands, key=lambda p: p.stat().st_mtime)

    base = None
    try:
        for r in csv.DictReader(open(csv_p, encoding="utf-8-sig")):
            if str(r.get("rule", "")).startswith("base("):
                base = r
                break
    except Exception as e:
        print(f"  [error] {csv_p} を読めません: {e}")
        return None
    if base is None:
        print(f"  [error] {csv_p} に base 行がありません")
        return None

    # 上書きされないよう (sm, tm, phase) 付きで退避
    keep = Path(f"sweep_smtm/{csv_p.stem}_sm{sm}_tm{tm}_{phase}.csv")
    keep.parent.mkdir(exist_ok=True)
    try:
        keep.write_bytes(csv_p.read_bytes())
    except Exception:
        pass

    def _f(k):
        try:
            return float(str(base.get(k, 0)).replace(",", "") or 0)
        except ValueError:
            return 0.0

    row = {"sm": sm, "tm": tm, "phase": phase,
           "trades": int(_f("trades")), "win_rate": _f("win_rate"), "pf": _f("pf"),
           "target": int(_f("target")), "stop": int(_f("stop")), "close": int(_f("close")),
           "net_real": _f("net_realistic_gap"), "net_cons": _f("net_conservative_slip")}
    print(f"  → {int(row['trades']):,}件 勝率{row['win_rate']:.0f}% PF{row['pf']:.2f} "
          f"net現実 {row['net_real']:+,.0f} / net保守 {row['net_cons']:+,.0f}  "
          f"({time.time() - t0:.0f}秒)", flush=True)
    return row


done = _load_done()
rows = list(done.values())
print(f"[スイープ] sm {SMS} × tm {TMS} = {len(SMS) * len(TMS)}点 × TRAIN/TEST = "
      f"{len(SMS) * len(TMS) * 2}回")
print(f"  TRAIN: 直近{args.holdout_days}日を除いた{args.train_days}日")
print(f"  TEST : 直近{args.holdout_days}日")
if done:
    print(f"  済み {len(done)}件は飛ばします ({OUT})")

for sm in SMS:
    for tm in TMS:
        for phase in ("TRAIN", "TEST"):
            if (str(sm), str(tm), phase) in done:
                continue
            r = _run(sm, tm, phase)
            if r:
                rows.append(r)
                _save(rows)

if args.dry_run:
    sys.exit(0)

# ── 結果表 ────────────────────────────────────────────────────────────
idx = {(float(r["sm"]), float(r["tm"]), r["phase"]): r for r in rows}
print(f"\n{'=' * 92}")
print("■ sm/tm スイープ結果 (base ルールの net現実)")
print(f"{'=' * 92}")
for phase in ("TRAIN", "TEST"):
    print(f"\n  [{phase}]")
    _hdr = "sm＼tm"
    print(f"  {_hdr:<10}" + "".join(f"{t:>16.1f}" for t in TMS))
    print("  " + "-" * (10 + 16 * len(TMS)))
    for sm in SMS:
        cells = ""
        for tm in TMS:
            r = idx.get((sm, tm, phase))
            cells += f"{float(r['net_real']):>+16,.0f}" if r else f"{'—':>16}"
        print(f"  {sm:<10.2f}{cells}")

# TRAIN の最良点が TEST でどうなるか
tr = [(k, v) for k, v in idx.items() if k[2] == "TRAIN"]
if tr:
    best = max(tr, key=lambda kv: float(kv[1]["net_real"]))
    bsm, btm, _ = best[0]
    te = idx.get((bsm, btm, "TEST"))
    cur = idx.get((0.1, 1.0, "TEST"))
    print(f"\n{'─' * 92}")
    print(f"  TRAIN の最良点: sm={bsm} tm={btm}  → TRAIN {float(best[1]['net_real']):+,.0f}円")
    if te:
        print(f"                                  → TEST  {float(te['net_real']):+,.0f}円 "
              f"(net保守 {float(te['net_cons']):+,.0f}円)")
    if cur:
        print(f"  現行 sm=0.1 tm=1.0 の TEST      → {float(cur['net_real']):+,.0f}円")
        if te:
            _d = float(te["net_real"]) - float(cur["net_real"])
            print(f"  差                              → {_d:+,.0f}円 "
                  f"{'✅ 改善' if _d > 0 else '❌ 悪化'}")
    print()
    print("  ⚠ TEST の表が『どこも似た値』ならパラメータに意味は無い(ノイズ)。")
    print("    TRAIN の最良点だけ TEST でも突出しているときにだけ採用を検討する。")
    print("    採用前に sim_portfolio_lss.py(予算・上限キャンセル込み)で必ず再検証すること。")
    print("    単体の期待値がプラスでも予算が有限だと機会費用でマイナスになる(CLAUDE.md 18.10)。")
