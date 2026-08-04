"""sweep_lss_oos_monthly.py
lss提案ファイルを時系列に累積マージし、各月のOOS成績を1本のCSVに集積。

自動検出した lss_proposal_YYYY-MM.py を時系列順に並べ、累積フォールドを生成:
  Fold 1: [2024-09]              → OOS月 = 2024-10 のトレードだけ集計
  Fold 2: [2024-09, 2024-10]    → OOS月 = 2024-11 のトレードだけ集計
  Fold 3: [2024-09〜11]         → OOS月 = 2024-12 のトレードだけ集計
  ...
  Fold N-1: 全月 - 1            → OOS月 = 最新月 のトレードだけ集計

各フォールドで run_signals_holdout_all.py を実行し、LSS_OOS_BUDGET_CSV で
月別成績を取得。OOS月のみに絞って最終CSVに集積する。

使い方:
  python sweep_lss_oos_monthly.py --workers 8
  python sweep_lss_oos_monthly.py --min-price 1000 --max-price 6000 --workers 8
  python sweep_lss_oos_monthly.py --proposals lss_proposal_2024-09.py lss_proposal_2024-10.py ...
  python sweep_lss_oos_monthly.py --out oos_sweep_result.csv
"""
from __future__ import annotations
import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional


# ── 基準月ユーティリティ ───────────────────────────────────────────────────────

def _extract_yyyymm(path: str) -> Optional[str]:
    """ファイル名から YYYY-MM を抽出。"""
    m = re.search(r"(\d{4}-\d{2})", Path(path).name)
    return m.group(1) if m else None


def _next_month(yyyymm: str) -> str:
    """YYYY-MM の翌月を返す。2024-12 → 2025-01。"""
    y, mo = int(yyyymm[:4]), int(yyyymm[5:7])
    return f"{y + 1}-01" if mo == 12 else f"{y}-{mo + 1:02d}"


def _months_ago(yyyymm: str, today: date = date.today()) -> int:
    """yyyymm が today から何ヶ月前か(概算)。"""
    y, mo = int(yyyymm[:4]), int(yyyymm[5:7])
    return (today.year - y) * 12 + (today.month - mo)


# ── マージロジック (merge_lss_proposals.py の内部実装を再利用) ────────────────

def _merge_proposals(paths: list[str], oos_all: bool = True, out: str = "") -> str:
    """merge_lss_proposals.py を呼び出して一時マージファイルを生成し、パスを返す。"""
    cmd = [sys.executable, str(Path(__file__).parent / "merge_lss_proposals.py")]
    cmd += paths
    if oos_all:
        cmd += ["--oos-all"]
    cmd += ["--out", out]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(f"[WARN] merge_lss_proposals 失敗:\n{result.stderr}", flush=True)
    return out


# ── バックテスト実行 ───────────────────────────────────────────────────────────

def _run_backtest(
    proposal_path: str,
    oos_csv_out: str,
    days: int,
    min_price: int,
    max_price: int,
    workers: int,
    extra_args: list[str],
) -> bool:
    """run_signals_holdout_all.py を実行し、OOS予算CSVを生成。成否を返す。"""
    env = os.environ.copy()
    env["LSS_OOS_BUDGET_CSV"] = oos_csv_out
    env["LSS_OOS_BUDGET_DAYS"] = str(days)

    cmd = [
        sys.executable,
        str(Path(__file__).parent / "run_signals_holdout_all.py"),
        "--long-stop-short",
        "--no-browser",
        "--no-analysis",
        "--no-news",
        "--no-risk",
        "--no-serve",          # 発注サーバを起動しない(スイープ中は不要)
        "--force",
        "--days", str(days),
        "--lss-proposal", proposal_path,
        "--min-price", str(min_price),
        "--price-ranges", f"{max_price},0",
        "--workers", str(workers),
    ] + extra_args

    print(f"  → run_signals_holdout_all.py --long-stop-short --no-serve --days {days} ...", flush=True)
    # text=False で Windows SJIS コンソールの文字コードエラーを回避
    result = subprocess.run(cmd, env=env)
    return result.returncode == 0


# ── CSV後処理 ─────────────────────────────────────────────────────────────────

def _filter_oos_month(csv_path: str, oos_month: str) -> list[dict]:
    """csvからoos_month(YYYY-MM)のみ抽出して返す。"""
    if not Path(csv_path).exists():
        return []
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("month", "").startswith(oos_month):
                rows.append(row)
    return rows


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="lss OOSスイープ: 累積フォールド月別成績集積")
    ap.add_argument("--proposals", nargs="*",
                    help="lss_proposal_YYYY-MM.py ファイル群。省略時はカレントディレクトリを自動検索。")
    ap.add_argument("--out", type=str, default="",
                    help="出力CSVファイル名。省略時は oos_sweep_YYYY-MM-DD.csv 。")
    ap.add_argument("--days", type=int, default=730,
                    help="バックテスト期間(日数)。OOS開始月から現在までをカバーできる値(既定730)。")
    ap.add_argument("--min-price", type=int, default=1000)
    ap.add_argument("--max-price", type=int, default=6000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--start-from", type=str, default="",
                    help="訓練に使う最初の基準月(YYYY-MM)。これより前の提案ファイルを無視。"
                         "例: --start-from 2025-09 → 2025-09以降のファイルのみ訓練に使用。")
    ap.add_argument("--fold-from", type=str, default="",
                    help="スイープ開始フォールドのOOS月(YYYY-MM)。指定月以降のフォールドだけ実行。")
    ap.add_argument("--fold-to", type=str, default="",
                    help="スイープ終了フォールドのOOS月(YYYY-MM)。指定月以前のフォールドだけ実行。")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="一時マージファイル・一時CSVを削除せず残す(デバッグ用)。")
    args = ap.parse_args()

    # 提案ファイルを収集
    if args.proposals:
        proposal_files = sorted(args.proposals)
    else:
        proposal_files = sorted(
            str(p) for p in Path(".").glob("lss_proposal_????-??.py")
        )
        if not proposal_files:
            print("[ERROR] lss_proposal_YYYY-MM.py が見つかりません。"
                  "--proposals で明示指定してください。", file=sys.stderr)
            sys.exit(1)

    # YYYY-MM でソート
    dated = [(f, _extract_yyyymm(f)) for f in proposal_files]
    dated = [(f, ym) for f, ym in dated if ym]
    dated.sort(key=lambda x: x[1])

    # --start-from: 指定月より前の提案ファイルを除外
    if args.start_from:
        before = [(f, ym) for f, ym in dated if ym < args.start_from]
        dated = [(f, ym) for f, ym in dated if ym >= args.start_from]
        if before:
            print(f"[sweep] --start-from {args.start_from}: 以下を訓練から除外:", flush=True)
            for f, ym in before:
                print(f"  (除外) {ym}: {f}", flush=True)

    if len(dated) < 2:
        print(f"[ERROR] フォールド生成には最低2ファイル必要。"
              f"--start-from 適用後={len(dated)}件", file=sys.stderr)
        sys.exit(1)

    print(f"[sweep] 提案ファイル {len(dated)} 件使用:", flush=True)
    for f, ym in dated:
        print(f"  {ym}: {f}", flush=True)
    print(flush=True)

    # 出力パス
    out_csv = args.out or f"oos_sweep_{date.today()}.csv"
    out_path = Path(out_csv)
    if out_path.exists():
        out_path.unlink()  # 既存ファイルは削除してゼロから書き直す

    # フォールドごとに実行
    all_rows: list[dict] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="lss_sweep_"))
    print(f"[sweep] 一時ファイルディレクトリ: {tmp_dir}", flush=True)

    for i in range(len(dated)):
        # Fold i: proposals[0..i] をマージ → OOS月 = proposals[i+1] の月
        if i == len(dated) - 1:
            # 最後のファイルはOOS月が「今月」になるが今月の完全なデータはないのでスキップ
            oos_month = _next_month(dated[i][1])
            today_ym = date.today().strftime("%Y-%m")
            if oos_month >= today_ym:
                print(f"[fold {i+1}/{len(dated)}] OOS月={oos_month} は今月/未来のためスキップ", flush=True)
                continue

        oos_month = _next_month(dated[i][1])
        fold_label = f"fold{i+1:02d}_{oos_month}"

        # 範囲フィルター
        if args.fold_from and oos_month < args.fold_from:
            print(f"[fold {i+1}] OOS={oos_month} < --fold-from={args.fold_from} スキップ", flush=True)
            continue
        if args.fold_to and oos_month > args.fold_to:
            print(f"[fold {i+1}] OOS={oos_month} > --fold-to={args.fold_to} スキップ", flush=True)
            continue

        print(f"\n{'='*60}", flush=True)
        print(f"[fold {i+1}/{len(dated)}] 訓練月: {dated[0][1]}〜{dated[i][1]} → OOS月: {oos_month}", flush=True)

        # マージ提案ファイル生成
        fold_files = [f for f, _ in dated[: i + 1]]
        merged_path = str(tmp_dir / f"lss_merged_{fold_label}.py")
        if len(fold_files) == 1:
            # 1ファイルの場合はマージ不要。ただし START_DATES を付けるために
            # merge_lss_proposals.py で自己マージ(2回同じファイル指定)
            fold_files = fold_files * 2
        _merge_proposals(fold_files, oos_all=True, out=merged_path)
        if not Path(merged_path).exists():
            print(f"[WARN] マージファイル生成失敗: {merged_path} → このフォールドをスキップ", flush=True)
            continue

        # バックテスト実行
        tmp_csv = str(tmp_dir / f"tmp_{fold_label}.csv")
        ok = _run_backtest(
            proposal_path=merged_path,
            oos_csv_out=tmp_csv,
            days=args.days,
            min_price=args.min_price,
            max_price=args.max_price,
            workers=args.workers,
            extra_args=[],
        )
        if not ok:
            print(f"[WARN] バックテスト失敗: fold={fold_label}", flush=True)

        # OOS月のみ抽出
        fold_rows = _filter_oos_month(tmp_csv, oos_month)
        print(f"[fold {i+1}] OOS月={oos_month}: {len(fold_rows)} 行抽出", flush=True)
        for row in fold_rows:
            row["oos_month"] = oos_month
            row["train_months"] = f"{dated[0][1]}〜{dated[i][1]}"
            row["fold"] = i + 1
        all_rows.extend(fold_rows)

        # 一時ファイル削除
        if not args.keep_tmp:
            for p in [merged_path, tmp_csv]:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass

    # 一時ディレクトリ削除
    if not args.keep_tmp:
        try:
            tmp_dir.rmdir()
        except Exception:
            pass

    if not all_rows:
        print("\n[ERROR] OOS行が1件も収集できませんでした。提案ファイル・バックテスト設定を確認してください。")
        sys.exit(1)

    # 最終CSV出力
    flds = ["fold", "train_months", "oos_month", "holdout_days", "config",
            "sim_type", "month", "trades", "wins", "win_rate_pct", "pnl",
            "budget_man", "min_bt"]
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=flds, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)

    print(f"\n{'='*60}", flush=True)
    print(f"[sweep完了] {out_csv}: {len(all_rows)} 行", flush=True)

    # サマリー表示
    from collections import defaultdict
    by_sim: dict = defaultdict(lambda: {"months": 0, "trades": 0, "pnl": 0.0, "wins": 0})
    for row in all_rows:
        st = row.get("sim_type", "?")
        e = by_sim[st]
        e["months"] += 1
        e["trades"] += int(row.get("trades", 0) or 0)
        e["pnl"] += float(row.get("pnl", 0) or 0)
        e["wins"] += int(row.get("wins", 0) or 0)
    print("\n── OOSスイープ集計サマリー ──")
    print(f"{'シム種別':<24} {'月数':>4} {'取引':>6} {'勝率':>6} {'損益':>10}")
    for st, e in sorted(by_sim.items()):
        wr = e["wins"] / e["trades"] * 100 if e["trades"] else 0
        print(f"{st:<24} {e['months']:>4}月 {e['trades']:>6}件 {wr:>5.1f}% {e['pnl']:>+10,.0f}円")
    print(f"\n→ {Path(out_csv).resolve()}", flush=True)


if __name__ == "__main__":
    main()
