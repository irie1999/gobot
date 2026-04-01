"""
全戦略シグナル一括スキャン
────────────────────────────────────────────────────────────────────────
MACD / A7（ストキャスティクス+ATR）/ RSI(2) のシグナルを一括実行し、
1つのHTMLレポートにまとめて出力する。

使い方:
  python scan_all.py              # 全戦略・全銘柄V2（監視リスト対象）
  python scan_all.py --225        # 全戦略・日経225 V1（監視リスト対象）
  python scan_all.py --macd       # MACDのみ
  python scan_all.py --a7         # A7のみ
  python scan_all.py --rsi2       # RSI2のみ
"""

import argparse
import subprocess
import sys
import io
import webbrowser
from datetime import datetime
from pathlib import Path

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="全戦略シグナル一括スキャン → 1つのHTMLレポート出力",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python scan_all.py              # 全戦略・全銘柄V2（監視リスト対象）
  python scan_all.py --225        # 全戦略・日経225 V1（監視リスト対象）
  python scan_all.py --macd       # MACDのみ
  python scan_all.py --a7         # A7のみ
  python scan_all.py --rsi2       # RSI2のみ
""")
    parser.add_argument("--225",    dest="nikkei225", action="store_true",
                        help="日経225 V1モジュールで実行")
    parser.add_argument("--macd",   action="store_true", help="MACDのみ実行")
    parser.add_argument("--a7",     action="store_true", help="A7のみ実行")
    parser.add_argument("--rsi2",   action="store_true", help="RSI2のみ実行")
    args = parser.parse_args()

    today  = datetime.today().strftime("%Y-%m-%d")
    py     = sys.executable
    label  = "V1（日経225）" if args.nikkei225 else "V2（全銘柄）"

    # select_symbols_v2.py に委譲
    cmd = [py, "select_symbols_v2.py"]
    if args.nikkei225:
        cmd.append("--v1")
    if args.macd:
        cmd.append("--macd")
    if args.a7:
        cmd.append("--a7")
    if args.rsi2:
        cmd.append("--rsi2")

    print()
    print("╔" + "═" * 70 + "╗")
    print(f"║  全戦略シグナル一括スキャン  {today}  {' ' * max(0, 32 - len(today))}║")
    print(f"║  対象: {label:<62}║")
    print("╚" + "═" * 70 + "╝")
    print()

    result = subprocess.run(cmd, text=True, encoding="utf-8")

    if result.returncode != 0:
        print(f"\n  ✘ エラーが発生しました（終了コード: {result.returncode}）\n")
        sys.exit(result.returncode)

    # 生成された HTML を探して開く
    ver = "v1" if args.nikkei225 else "v2"
    html_files = sorted(Path(".").glob(f"select_{ver}_report_{today}.html"))
    if not html_files:
        # フォールバック: 日付なしで最新を探す
        html_files = sorted(Path(".").glob(f"select_*report*.html"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    if html_files:
        print(f"\n  HTMLレポート: {html_files[0].resolve()}")

    print()
    print("═" * 72)
    print(f"  スキャン完了  {today}")
    print(f"  ポジション管理: python portfolio.py --web")
    print("═" * 72)
    print()


if __name__ == "__main__":
    main()
