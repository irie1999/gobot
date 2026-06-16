"""
summarize_shifts.py - 各 shift_days の選定結果を貼り付けやすく要約

【目的】
scan_walkforward_daytrade.py --all-periods で生成された 6パターンの
shift CSV を読み込み、各 shift について Top 銘柄をコンパクトに表示。
チャット貼付け用に整形。

【使い方】
  # 全 shift をまとめて表示
  python summarize_shifts.py

  # 特定 mode のみ
  python summarize_shifts.py --mode standard

  # 特定 shift のみ
  python summarize_shifts.py --shifts 30 60

  # CSV にも出力
  python summarize_shifts.py --output summary_shifts.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WF_DIR = Path("walkforward_daytrade_results")
DEFAULT_SHIFTS = [30, 60, 90, 120, 150, 180]


def load_csv(path: Path) -> list[dict]:
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except Exception:
        return []
    return rows


def find_csvs(shift_days: int, mode: str | None = None,
              latest_date: str | None = None) -> list[Path]:
    """shift_days, mode に該当する CSV を全戦略分集める."""
    if latest_date:
        if shift_days > 0:
            pattern = (f"walkforward_*_*_{latest_date}_shift{shift_days}d.csv")
        else:
            pattern = f"walkforward_*_*_{latest_date}.csv"
    else:
        if shift_days > 0:
            pattern = f"walkforward_*_*_*_shift{shift_days}d.csv"
        else:
            pattern = "walkforward_*_*_*.csv"

    paths = []
    for fp in glob.glob(str(WF_DIR / pattern)):
        p = Path(fp)
        stem = p.stem
        # mode フィルタ
        if mode and f"_{mode}_" not in stem:
            continue
        # shift=0 のとき、_shift がついたものは除外
        if shift_days == 0 and "_shift" in stem:
            continue
        paths.append(p)
    return sorted(paths)


def detect_latest_date() -> str | None:
    """walkforward CSV の最新日付を検出."""
    dates = set()
    for fp in glob.glob(str(WF_DIR / "walkforward_*_*.csv")):
        stem = Path(fp).stem
        # _shift を取り除く
        if "_shift" in stem:
            stem = stem[:stem.rfind("_shift")]
        parts = stem.split("_")
        if len(parts) >= 4:
            dates.add(parts[-1])
    if not dates:
        return None
    return max(dates)


def parse_strategy(path: Path) -> tuple[str, str]:
    """ファイル名から (strategy, mode) を抽出.
    例: walkforward_DON_standard_2026-06-16_shift30d.csv → ('DON', 'standard')
    例: walkforward_DON_S_lenient_2026-06-16.csv         → ('DON_S', 'lenient')
    """
    stem = path.stem
    if "_shift" in stem:
        stem = stem[:stem.rfind("_shift")]
    # 末尾の _<date> 削除
    parts = stem.split("_")
    if len(parts) >= 4:
        # 最後が date、その前が mode
        mode = parts[-2]
        # それ以前 (walkforward + strat)
        strat = "_".join(parts[1:-2])
        return strat, mode
    return "?", "?"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--shifts", type=int, nargs="*", default=None,
                   help="表示する shift_days のリスト (省略時 30,60,90,120,150,180)")
    p.add_argument("--mode", default="standard",
                   choices=["standard", "lenient", "strict"],
                   help="表示する mode (デフォルト standard)")
    p.add_argument("--top", type=int, default=15,
                   help="各 shift で表示する Top N (デフォルト 15)")
    p.add_argument("--output", default=None,
                   help="CSV にも出力する場合のファイル名")
    p.add_argument("--latest-date", default=None,
                   help="特定日付の CSV のみ対象 (省略時自動検出)")
    args = p.parse_args()

    latest_date = args.latest_date or detect_latest_date()
    if not latest_date:
        print(f"[error] WF CSV が見つかりません ({WF_DIR})")
        return
    print(f"検出日付: {latest_date}", flush=True)

    shifts = args.shifts if args.shifts else DEFAULT_SHIFTS
    print(f"対象 shifts: {shifts} / mode: {args.mode} / top {args.top}\n",
          flush=True)

    all_rows = []  # (shift, strategy, symbol, name, pf, win_rate, pnl, folds)

    for shift in shifts:
        paths = find_csvs(shift, args.mode, latest_date)
        if not paths:
            print(f"━━━ shift={shift}日 (mode={args.mode}) ━━━")
            print(f"  CSV なし\n")
            continue

        # 各戦略 CSV から銘柄を集約
        all_picks = []
        for path in paths:
            strat, mode = parse_strategy(path)
            for r in load_csv(path):
                try:
                    pf = float(r.get("total_pf", "0") or 0)
                    trades = int(r.get("total_trades", "0") or 0)
                    pnl = float(r.get("total_pnl", "0") or 0)
                except Exception:
                    continue
                all_picks.append({
                    "shift": shift,
                    "strategy": strat,
                    "symbol": r.get("symbol", "").strip(),
                    "name": r.get("name", "").strip(),
                    "pf": pf,
                    "trades": trades,
                    "win_rate": float(r.get("total_win_rate", "0") or 0),
                    "pnl": pnl,
                    "folds": r.get("pass_folds", ""),
                })
                all_rows.append(all_picks[-1])

        # 銘柄×戦略 で重複排除 (PnL 降順で最良 1 件)
        # ただし表示は全戦略合算で PnL 降順
        # PnL 降順ソート
        all_picks.sort(key=lambda x: -x["pnl"])

        # 戦略別件数も計算
        by_strat = defaultdict(int)
        for pk in all_picks:
            by_strat[pk["strategy"]] += 1

        # サマリヘッダ
        n_pairs = len(all_picks)
        n_uniq_syms = len(set(pk["symbol"] for pk in all_picks))
        print(f"━━━ shift={shift}日 ({n_pairs}ペア / {n_uniq_syms}銘柄 / "
              f"mode={args.mode}) ━━━")
        # 戦略内訳
        strat_summary = ", ".join(f"{s}:{n}" for s, n in
                                   sorted(by_strat.items(), key=lambda x: -x[1]))
        print(f"  戦略内訳: {strat_summary}")
        print(f"  Top {args.top} (PnL 降順):")
        for pk in all_picks[:args.top]:
            print(f"    {pk['symbol']:<7} {pk['name'][:14]:<14} "
                  f"{pk['strategy']:<7} "
                  f"PF{pk['pf']:.2f} 勝{pk['win_rate']:.0f}% "
                  f"PnL{pk['pnl']:+,.0f} {pk['folds']}/3")
        print()

    # CSV 出力
    if args.output and all_rows:
        out = Path(args.output)
        with open(out, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[
                "shift", "strategy", "symbol", "name",
                "pf", "trades", "win_rate", "pnl", "folds",
            ])
            w.writeheader()
            for r in all_rows:
                w.writerow(r)
        print(f"[OK] CSV保存: {out.resolve()}")


if __name__ == "__main__":
    main()
