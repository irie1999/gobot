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
    ap.add_argument("--manifest", type=str, default="oos_folds_manifest.csv",
                    help="このrunが作ったフォールドの台帳(fold,train_months,oos_month,raw_csv)。"
                         "集計側がファイル名から推測せずに済む。空文字で無効")
    ap.add_argument("--budget-csv", type=str, default="oos_budget_folds.csv",
                    help="レポート自身の予算タブ(月別P&L)を追記するCSV。**.\\daily と同じ計算経路**"
                         "なので、生CSVを sim_oos_budget.py で再シミュした値とズレたときの正解はこちら。"
                         "空文字で無効")
    ap.add_argument("--bt-tiers", type=str, default="30,40,50,60,70",
                    help="予算CSVに出すBT閾値(カンマ区切り)。1回の実行で複数層を比較できる")
    ap.add_argument("--days", type=int, default=730, help="レポートの集計窓(日)")
    ap.add_argument("--start-from", type=str, default="",
                    help="訓練に使う最初の基準月 (例: --start-from 2025-09)")
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
    if args.start_from:
        dated = [(p, ym) for p, ym in dated if ym >= args.start_from]
        print(f"--start-from {args.start_from} 以降の {len(dated)} 件を使用")

    if len(dated) < 2:
        print("[ERROR] lss_proposal_YYYY-MM.py が2件以上必要です。")
        sys.exit(1)

    today_ym = date.today().strftime("%Y-%m")

    # daily.bat と同一の環境変数
    env = os.environ.copy()
    env["LSS_CLOSESTOP_RESWEEP"] = ""
    env["LSS_GUARD_ONLY"] = ""
    env["LSS_STOP_DELAY_BARS"] = "2"   # ライブと揃える(CLAUDE.md §18.9)
    env["LSS_BT_TAB_MIN"] = "40"
    # ★ レポート自身の予算タブ(= .\daily と同じ _run_budget_sim)の月別P&Lを出させる。
    #    生CSV(LSS_OOS_RAW_CSV)を sim_oos_budget.py で再シミュした値とは経路が違うので、
    #    両者がズレたら **こちらが正**(実際に発注リストを作っているコードだから)。
    if args.budget_csv:
        # レポートは追記するので、実行前に必ず消す。残っていると前回の行が混ざり、
        # 集計側(aggregate_oos_budget.py)が『各月の最初の出現』を採る仕様のため
        # 古いフォールドの値を拾ってしまう。
        _bp = Path(args.budget_csv)
        if _bp.exists():
            if args.fold_from or args.fold_to:
                print(f"[!] {_bp} が既にあります。--fold-from/--fold-to での部分実行なので"
                      f"残します(集計時は月の重複に注意)")
            else:
                _bp.unlink()
                print(f"[初期化] 既存の {_bp} を削除(レポートは追記するため)")
        env["LSS_OOS_BUDGET_CSV"] = args.budget_csv
    # ★ このrunが作ったフォールドの台帳。集計側はファイル名から推測せずこれを使う。
    #   同じフォルダに開始月の違う過去実行の oos_raw_fold*.csv が残っていると、
    #   ファイル名由来の fold→OOS月 対応が壊れる(実測: fold01_2025-01 と
    #   fold01_2025-10 が同居していた)。
    manifest = Path(args.manifest) if args.manifest else None
    if manifest is not None and not (args.fold_from or args.fold_to):
        manifest.write_text("fold,train_months,oos_month,raw_csv\n", encoding="utf-8")
        env["LSS_OOS_BUDGET_DAYS"] = str(args.days)   # days が一致しないと出力されない
        env["LSS_OOS_BUDGET_BT_TIERS"] = args.bt_tiers
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
        out_raw = f"oos_raw_fold{i+1:02d}_{oos_ym}.csv"

        print(f"\n{'='*60}")
        print(f"[fold {i+1}] 訓練: {dated[0][1]}〜{train_end_ym}  OOS: {oos_ym}  long-base: {long_base}")

        # Step 1: merge (daily.bat の1行目と同じ)
        # 1ファイルの場合は merge_lss_proposals.py が2件必要なので同じファイルを2回渡す
        merge_files = train_files if len(train_files) >= 2 else train_files * 2
        subprocess.run(
            [sys.executable, "merge_lss_proposals.py"] + merge_files + ["--out", merged],
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
            "--days", str(args.days),
            "--workers", str(args.workers),
            "--no-browser",
            "--no-serve",
        ], env=fold_env)

        Path(merged).unlink(missing_ok=True)

        if manifest is not None:
            with open(manifest, "a", encoding="utf-8") as _mf:
                _mf.write(f"{i + 1},{dated[0][1]}〜{train_end_ym},{oos_ym},{out_raw}\n")

    print(f"\n{'='*60}")
    print("完了。生成されたOOS CSV:")
    for f in sorted(Path(".").glob("oos_raw_fold*.csv")):
        print(f"  {f.name}")
    if args.budget_csv and Path(args.budget_csv).exists():
        print(f"\n  {args.budget_csv}  ← レポート自身の予算タブ(.\\daily と同じ経路)")
        print(f"  集計:  python aggregate_oos_budget.py --csv {args.budget_csv}")
    if manifest is not None and manifest.exists():
        print(f"  台帳:  {manifest}  (このrunのfold→OOS月。古いCSVが混ざっても集計は安全)")


if __name__ == "__main__":
    main()
