"""
debug_quarantine.py  ―  quarantine_5m から銘柄を自動ピックして WF 診断を実行

使い方:
  python debug_quarantine.py                    # 先頭5銘柄 × 全戦略
  python debug_quarantine.py --n 3              # 先頭3銘柄
  python debug_quarantine.py --strategy GAP_DOWN
  python debug_quarantine.py --symbol 1301.T    # 銘柄指定
  python debug_quarantine.py --list             # quarantine_5m の銘柄一覧だけ表示
"""

import argparse
import subprocess
import sys

from daytrade_data import quarantine_symbols

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n",        type=int,   default=5,   help="先頭N銘柄 (デフォルト5)")
    parser.add_argument("--strategy", type=str,   default=None,
                        help="戦略名 (省略時=全戦略, 例: GAP_DOWN)")
    parser.add_argument("--symbol",   type=str,   default=None,
                        help="銘柄コード指定 (省略時はquarantine先頭N件)")
    parser.add_argument("--list",     action="store_true",
                        help="quarantine_5m の銘柄一覧を表示して終了")
    parser.add_argument("--folds-short", action="store_true",
                        help="短縮fold (1年未満データ向け)")
    args = parser.parse_args()

    syms = quarantine_symbols()

    if args.list:
        print(f"quarantine_5m: {len(syms)} 銘柄")
        for s in syms:
            print(f"  {s}")
        return

    if not syms and args.symbol is None:
        print("[ERROR] quarantine_5m にデータが見つかりません")
        print("  data/quarantine_5m/*.pkl が存在するか確認してください")
        sys.exit(1)

    targets = [args.symbol] if args.symbol else syms[:args.n]

    strategy_args = ["--strategy", args.strategy] if args.strategy else []
    folds_args    = ["--folds-short"] if args.folds_short else []

    for sym in targets:
        print(f"\n{'='*60}")
        print(f"  診断: {sym}")
        print(f"{'='*60}")
        cmd = [
            sys.executable, "scan_walkforward_intraday.py",
            "--debug-symbol", sym,
            "--source", "local",
        ] + strategy_args + folds_args
        subprocess.run(cmd)


if __name__ == "__main__":
    main()
