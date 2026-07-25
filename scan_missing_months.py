"""scan_missing_months.py — 十分なデータがある基準月について、未選定の月だけ銘柄選定を実行。

既に lss_proposal_YYYY-MM.py がある月はスキップし、無い月だけ scan_lss_universe を回して
lss_proposal_YYYY-MM.py を生成する。月次の基準月プールを埋める用途。

使い方(あなたの機械で。5分足データが必要):
  python scan_missing_months.py                       # 2025-09〜先月、未選定の月だけ選定
  python scan_missing_months.py --from 2024-12        # もっと古くから(TRAINは短いが)
  python scan_missing_months.py --to 2026-06
  python scan_missing_months.py --dry-run             # 何を実行/スキップするか表示だけ
  python scan_missing_months.py --workers 8

注意:
  ・scan_lss_universe は月ごとに5分足をロードするので、月数×ロードで時間がかかる(数十分〜数時間)。
    途中で止めても、既に出来た月は次回スキップされる(=レジューム可能)。
  ・選定設定は既存 proposal と揃える(sm=0.1/tm=1.0/source=local、delay1は選定には適用しない)。
    ＝既存の四半期 proposal と同じ土俵の"月次版"を作る。
  ・「十分なデータ」の目安は 2025-09 以降(TRAIN 14ヶ月+)。2024-12/2025-03 は5分足が短く弱い
    (--from で明示すれば作れるが、選定は弱くなる)。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

_PY = sys.executable or "python"


def _default_to() -> str:
    """先月(最後に完了した月)。"""
    t = date.today()
    y, m = (t.year - 1, 12) if t.month == 1 else (t.year, t.month - 1)
    return f"{y:04d}-{m:02d}"


ap = argparse.ArgumentParser(description="未選定の基準月だけ銘柄選定(scan_lss_universe)を実行")
ap.add_argument("--from", dest="frm", default="2025-09",
                help="開始基準月 YYYY-MM(既定2025-09=データ十分)。2024-12まで下げると短TRAINで弱い")
ap.add_argument("--to", default=_default_to(), help="終了基準月 YYYY-MM(既定=先月)")
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--dry-run", action="store_true", help="実行せず、対象/スキップの一覧だけ表示")
args = ap.parse_args()


def _months(frm: str, to: str) -> list[str]:
    y, m = map(int, frm.split("-"))
    ey, em = map(int, to.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def main():
    months = _months(args.frm, args.to)
    todo, skip = [], []
    for ym in months:
        if Path(f"lss_proposal_{ym}.py").exists():
            skip.append(ym)
        else:
            todo.append(ym)
    print(f"[対象範囲] {args.frm} 〜 {args.to} ({len(months)}ヶ月)")
    print(f"[既存=スキップ] {', '.join(skip) if skip else 'なし'}")
    print(f"[未選定=これから選定] {', '.join(todo) if todo else 'なし'}")
    if not todo:
        print("→ すべて選定済み。やることはありません。")
        return
    if args.dry_run:
        print("\n[dry-run] 実行はしません。上記の未選定月を選定します。")
        for ym in todo:
            print(f"  python scan_lss_universe.py --base-month {ym} --sm {args.sm} "
                  f"--tm {args.tm} --source {args.source} --workers {args.workers} "
                  f"--out lss_proposal_{ym}.py")
        return

    print(f"\n[選定開始] {len(todo)}ヶ月。1ヶ月ずつ scan_lss_universe を実行します。")
    print("  (途中で止めても、出来た月は次回スキップ=レジューム可)")
    done = 0
    for ym in todo:
        out = f"lss_proposal_{ym}.py"
        print(f"\n===== [{done + 1}/{len(todo)}] 基準月 {ym} を選定中 → {out} =====", flush=True)
        cmd = [_PY, "scan_lss_universe.py", "--base-month", ym,
               "--sm", str(args.sm), "--tm", str(args.tm),
               "--source", args.source, "--workers", str(args.workers),
               "--out", out]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"  [!] {ym} の選定に失敗(code {r.returncode})。次の月へ進みます。")
            continue
        if Path(out).exists():
            done += 1
            print(f"  [OK] {out} を生成")
        else:
            print(f"  [!] {ym}: {out} が生成されませんでした(要確認)")
    print(f"\n[完了] {done}/{len(todo)} ヶ月の選定を生成しました。")
    print("次: マージや sweep_base_months.py / .\\daily --lss-proposal で評価してください。")


if __name__ == "__main__":
    main()
