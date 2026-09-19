"""
ショート ホールドアウト一括スキャン (保有期限7日)
────────────────────────────────────────────────────────────────────────
ショート2ファミリー (short / short_brk) を 6 つのホールドアウト設定
(HO30/60/90/120/150/180d) で一括 Walk-forward スキャンするラッパー。

保有期限は backtest_limit_entry.MAX_HOLD = 7 で設定済みなので、
ここでは価格帯とホールドアウト日数だけ指定する。

使い方:
  python scan_holdout_short.py                       # 1000-10000円, 6設定 x 2ファミリー
  python scan_holdout_short.py --workers 8           # 並列数
  python scan_holdout_short.py --min-price 1000 --max-price 10000

注意:
  --max-price は「スキャン対象の上限」。CSV には上限までの全銘柄が入り、
  run_signals_holdout_all.py --price-ranges 6000,10000 が各上限で再フィルタする。
  よって 10000 で1回スキャンすれば 6000 と 10000 の両レンジをカバーできる。
  python scan_holdout_short.py --no-relax            # --relax-short を付けない
  python scan_holdout_short.py --holdouts 30 90 180  # 設定を絞る

出力:
  walkforward_results/walkforward_<STRATEGY>_holdout<N>d_<date>-7d.csv
  (-7d = MAX_HOLD。スキャン後に自動でタグ付けされ、ホールドアウト7日用として区別できる)

スキャン後の流れ:
  python build_watchlist.py --budget 600000          # WATCHLIST 提案生成
  python verify_watchlist.py                         # 検証
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import backtest_limit_entry as _bte

SHORT_FAMILIES = ["short", "short_brk"]
SHORT_STRATEGIES = ["A7_S", "MACD_S", "RSI2_S", "DON_S", "MOM_S", "GAP_S"]
HOLDOUTS = [30, 60, 90, 120, 150, 180]


def _tag_max_hold(wf_dir: Path) -> int:
    """生成済みショート holdout CSV に保有期限サフィックス (-7d 等) を付ける。

    scan_walkforward.py は ..._holdout{N}d_{date}.csv で出力し MAX_HOLD を
    名前に残さないため、ここで -{MAX_HOLD}d を追記してホールドアウト7日用
    として区別できるようにする。既に -Nd が付いているものはスキップ。
    """
    mh = _bte.MAX_HOLD
    renamed = 0
    for k in SHORT_STRATEGIES:
        for p in wf_dir.glob(f"walkforward_{k}_holdout*d_*.csv"):
            if re.search(r"-\d+d$", p.stem):
                continue  # 既にサフィックス済み
            dst = p.with_name(f"{p.stem}-{mh}d.csv")
            p.rename(dst)
            print(f"  rename: {p.name} -> {dst.name}")
            renamed += 1
    return renamed


def main() -> None:
    p = argparse.ArgumentParser(description="ショート ホールドアウト一括スキャン (MAX_HOLD=7)")
    p.add_argument("--min-price", type=float, default=1000.0)
    p.add_argument("--max-price", type=float, default=10000.0)
    p.add_argument("--workers", type=int, default=0, help="0 なら scan_walkforward の既定")
    p.add_argument("--no-relax", action="store_true", help="--relax-short を付けない")
    p.add_argument("--holdouts", type=int, nargs="+", default=HOLDOUTS,
                   help="ホールドアウト日数のリスト (既定: 30 60 90 120 150 180)")
    p.add_argument("--aggressive", action="store_true")
    args = p.parse_args()

    py = sys.executable
    total = len(args.holdouts) * len(SHORT_FAMILIES)
    n = 0
    for ho in args.holdouts:
        for fam in SHORT_FAMILIES:
            n += 1
            cmd = [py, "scan_walkforward.py",
                   "--family", fam,
                   "--holdout-days", str(ho),
                   "--min-price", str(args.min_price),
                   "--max-price", str(args.max_price)]
            if not args.no_relax:
                cmd.append("--relax-short")
            if args.workers > 0:
                cmd += ["--workers", str(args.workers)]
            if args.aggressive:
                cmd.append("--aggressive")
            print(f"\n[{n}/{total}] HO{ho}d  family={fam}")
            print("  " + " ".join(cmd))
            rc = subprocess.run(cmd).returncode
            if rc != 0:
                print(f"  ✘ 終了コード {rc} で失敗。続行します。")

    n_tag = _tag_max_hold(Path("walkforward_results"))
    print(f"\n=== 全スキャン完了  (-{_bte.MAX_HOLD}d を {n_tag} 本にタグ付け) ===")
    print("次の手順:")
    print("  python build_watchlist.py --budget 600000")
    print("  python verify_watchlist.py")


if __name__ == "__main__":
    main()
