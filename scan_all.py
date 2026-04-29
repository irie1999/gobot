"""
全戦略シグナル一括スキャン
────────────────────────────────────────────────────────────────────────
MACD / A7（ストキャスティクス+ATR）/ RSI(2) のシグナルを一括実行し、
1つのHTMLレポートにまとめて出力する。

使い方:
  python scan_all.py                        # 全戦略・全銘柄V2（今日）
  python scan_all.py --date 2026-04-04      # 指定日のシグナル判定
  python scan_all.py --225                  # 全戦略・日経225 V1（監視リスト対象）
  python scan_all.py --all                  # V1（日経225）+ V2（全銘柄）両方実行
  python scan_all.py --macd                 # MACDのみ
  python scan_all.py --a7                   # A7のみ
  python scan_all.py --rsi2                 # RSI2のみ

株価キャッシュ:
  .rsi2_cache/ に銘柄ごとの株価を保存。平日は毎日再ダウンロード。
  本日終値を取得するには 15:30（JST）以降に実行してください。
"""

import argparse
import subprocess
import sys
import io
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _warn_if_before_close() -> None:
    """15:30（JST）前に実行した場合、本日終値が未確定である旨を警告する。"""
    now_jst = datetime.now(JST)
    if now_jst.weekday() < 5:  # 平日のみ
        market_close = now_jst.replace(hour=15, minute=30, second=0, microsecond=0)
        if now_jst < market_close:
            print(f"  ⚠  現在時刻 {now_jst.strftime('%H:%M')} JST — 東証は15:30に閉場します。")
            print(f"     本日（{now_jst.strftime('%m/%d')}）の終値はまだ確定していないため、")
            print(f"     前営業日の終値が使用されます。")
            print(f"     15:30以降に再実行すると本日終値が反映されます。")
            print()



def _run_scan(py: str, flags: list[str], label: str) -> int:
    """select_symbols_v2.py を実行して終了コードを返す（ブラウザは開かない）"""
    cmd = [py, "select_symbols_v2.py"] + flags + ["--no-browser"]
    print(f"\n  ▶ {label} スキャン開始")
    print("  " + "─" * 60)
    result = subprocess.run(cmd, text=True, encoding="utf-8")
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="全戦略シグナル一括スキャン → HTMLレポート出力",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python scan_all.py              # 全戦略・全銘柄V2
  python scan_all.py --225        # 全戦略・日経225 V1
  python scan_all.py --all        # V1（日経225）+ V2（全銘柄）両方
  python scan_all.py --macd       # MACDのみ
  python scan_all.py --a7         # A7のみ
  python scan_all.py --rsi2       # RSI2のみ
""")
    parser.add_argument("--225",  dest="nikkei225", action="store_true",
                        help="日経225 V1モジュールで実行")
    parser.add_argument("--all",  dest="run_all",   action="store_true",
                        help="V1（日経225）+ V2（全銘柄）両方実行")
    parser.add_argument("--macd", action="store_true", help="MACDのみ実行")
    parser.add_argument("--a7",   action="store_true", help="A7のみ実行")
    parser.add_argument("--rsi2", action="store_true", help="RSI2のみ実行")
    parser.add_argument("--date", type=str, default=None,
                        help="シグナル判定日を指定（例: 2026-04-04）省略時は今日")
    args = parser.parse_args()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    py    = sys.executable

    if not args.date:
        _warn_if_before_close()

    # 戦略フラグ（共通）
    strat_flags = []
    if args.macd:  strat_flags.append("--macd")
    if args.a7:    strat_flags.append("--a7")
    if args.rsi2:  strat_flags.append("--rsi2")
    if args.date:  strat_flags += ["--date", args.date]

    print()
    print("╔" + "═" * 70 + "╗")
    if args.run_all:
        label = "V1（日経225）+ V2（全銘柄）"
    elif args.nikkei225:
        label = "V1（日経225）"
    else:
        label = "V2（全銘柄）"
    date_label = args.date if args.date else today
    print(f"║  全戦略シグナル一括スキャン  {date_label}  {' ' * max(0, 32 - len(date_label))}║")
    print(f"║  対象: {label:<62}║")
    print("╚" + "═" * 70 + "╝")
    print()

    html_files = []

    if args.run_all:
        # ── V1（日経225）実行 ──────────────────────────────────
        rc = _run_scan(py, ["--v1"] + strat_flags, "V1 日経225")
        if rc != 0:
            print(f"\n  ✘ V1 エラー（終了コード: {rc}）\n")
            sys.exit(rc)
        v1_files = sorted(Path(".").glob(f"select_v1_report_{today}.html"))
        if v1_files:
            html_files.append(("V1 日経225", v1_files[0]))

        # ── V2（全銘柄）実行 ──────────────────────────────────
        rc = _run_scan(py, strat_flags, "V2 全銘柄")
        if rc != 0:
            print(f"\n  ✘ V2 エラー（終了コード: {rc}）\n")
            sys.exit(rc)
        v2_files = sorted(Path(".").glob(f"select_v2_report_{today}.html"))
        if v2_files:
            html_files.append(("V2 全銘柄", v2_files[0]))

    else:
        # ── 既存の単独実行 ─────────────────────────────────────
        flags = strat_flags[:]
        if args.nikkei225:
            flags = ["--v1"] + flags
        rc = _run_scan(py, flags, label)
        if rc != 0:
            print(f"\n  ✘ エラーが発生しました（終了コード: {rc}）\n")
            sys.exit(rc)
        ver = "v1" if args.nikkei225 else "v2"
        files = sorted(Path(".").glob(f"select_{ver}_report_{today}.html"))
        if not files:
            files = sorted(Path(".").glob(f"select_*report*.html"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            html_files.append((label, files[0]))

    print()
    print("═" * 72)
    print(f"  スキャン完了  {today}")
    for lbl, p in html_files:
        print(f"  HTMLレポート [{lbl}]: {p.resolve()}")
    print(f"  ポジション管理: python portfolio.py --web")
    print("═" * 72)
    print()

    # HTML を順番に開く
    for _, p in html_files:
        webbrowser.open(p.resolve().as_uri())


if __name__ == "__main__":
    main()
