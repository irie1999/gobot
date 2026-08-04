"""run_oos_folds.py
daily.bat ベースの OOS 累積フォールド実行スクリプト。

daily.bat の実行内容（参考）:
  python merge_lss_proposals.py lss_proposal_2025-09.py ... --out lss_proposal_cumul.py
  python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0
      --no-analysis --lss-proposal lss_proposal_cumul.py --long-base 2026-06-30
      --no-mirror --default-tab lss --force --no-news --no-risk --workers 8

このスクリプトは merge の提案ファイルを 1 つずつ増やして実行する。
  Fold1: lss_proposal_2025-09.py のみ → OOS = 2025-10
  Fold2: 2025-09 + 2025-10          → OOS = 2025-11
  ...
--long-base は訓練終了月の月末日を自動計算。

使い方:
  python run_oos_folds.py
  python run_oos_folds.py --workers 4
  python run_oos_folds.py --fold-from 2026-03   # 特定フォールドだけ再実行
"""
import argparse
import calendar
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


def month_end(yyyymm: str) -> str:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]}"


def next_month(yyyymm: str) -> str:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return f"{y+1}-01" if m == 12 else f"{y}-{m+1:02d}"


def extract_ym(path: Path) -> str:
    m = re.search(r"(\d{4}-\d{2})", path.name)
    return m.group(1) if m else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--fold-from", type=str, default="",
                    help="このOOS月以降のフォールドだけ実行 (例: 2026-03)")
    ap.add_argument("--fold-to", type=str, default="",
                    help="このOOS月以前のフォールドだけ実行 (例: 2026-06)")
    args = ap.parse_args()

    # lss_proposal_YYYY-MM.py を自動収集・ソート
    dated = sorted(
        [(p, extract_ym(p)) for p in Path(".").glob("lss_proposal_????-??.py") if extract_ym(p)],
        key=lambda x: x[1],
    )
    if len(dated) < 2:
        print("[ERROR] lss_proposal_YYYY-MM.py が2件以上必要です。")
        sys.exit(1)

    today_ym = date.today().strftime("%Y-%m")
    out_raw = f"oos_raw_{date.today().strftime('%Y%m%d')}.csv"

    # daily.bat と同一の環境変数
    env = os.environ.copy()
    env["LSS_CLOSESTOP_RESWEEP"] = ""
    env["LSS_GUARD_ONLY"] = ""
    env["LSS_STOP_DELAY_BARS"] = "1"
    env["LSS_BT_TAB_MIN"] = "40"
    env.pop("LSS_REALISTIC_ENTRY", None)

    print(f"提案ファイル {len(dated)} 件検出:")
    for p, ym in dated:
        print(f"  {ym}: {p.name}")

    for i in range(len(dated) - 1):
        train_end_ym = dated[i][1]
        oos_ym = next_month(train_end_ym)

        if oos_ym >= today_ym:
            print(f"\n[fold {i+1}] OOS={oos_ym} は今月以降のためスキップ")
            continue
        if args.fold_from and oos_ym < args.fold_from:
            print(f"[fold {i+1}] OOS={oos_ym} < --fold-from={args.fold_from} スキップ")
            continue
        if args.fold_to and oos_ym > args.fold_to:
            print(f"[fold {i+1}] OOS={oos_ym} > --fold-to={args.fold_to} スキップ")
            continue

        long_base = month_end(train_end_ym)
        train_files = [str(p) for p, _ in dated[:i + 1]]
        merged = f"lss_proposal_fold{i+1:02d}.py"

        print(f"\n{'='*60}")
        print(f"[fold {i+1}] 訓練: {dated[0][1]}〜{train_end_ym}  OOS: {oos_ym}  long-base: {long_base}")

        # Step 1: merge (daily.bat の1行目と同じ)
        subprocess.run(
            [sys.executable, "merge_lss_proposals.py"] + train_files + ["--out", merged],
            check=True,
        )

        # Step 2: run (daily.bat の2行目と同じ、--lss-proposal と --long-base のみ変更)
        fold_env = env.copy()
        fold_env["LSS_OOS_RAW_CSV"] = out_raw
        fold_env["LSS_OOS_MONTH"] = oos_ym
        fold_env["LSS_OOS_FOLD"] = str(i + 1)
        fold_env["LSS_OOS_TRAIN_MONTHS"] = f"{dated[0][1]}〜{train_end_ym}"

        subprocess.run([
            sys.executable, "run_signals_holdout_all.py",
            "--both",
            "--min-price", "1000",
            "--price-ranges", "6000,0",
            "--no-analysis",
            "--lss-proposal", merged,
            "--long-base", long_base,
            "--no-mirror",
            "--default-tab", "lss",
            "--force",
            "--no-news",
            "--no-risk",
            "--workers", str(args.workers),
            "--no-browser",
            "--no-serve",
        ], env=fold_env)

        Path(merged).unlink(missing_ok=True)

    print(f"\n{'='*60}")
    print(f"完了。生トレードCSV: {out_raw}")


if __name__ == "__main__":
    main()
