"""
全戦略シグナル一括スキャン
────────────────────────────────────────────────────────────────────────
MACD / A7（ストキャスティクス+ATR）/ RSI(2) の --signal を並列実行する。

使い方:
  python scan_all.py                   # 全戦略・監視銘柄でスキャン
  python scan_all.py --all             # 全戦略・日経225全銘柄
  python scan_all.py --macd            # MACDのみ
  python scan_all.py --a7              # A7のみ
  python scan_all.py --rsi2            # RSI2のみ
  python scan_all.py --mode hv         # RSI2を高ボラモード強制
  python scan_all.py --no-ibs          # RSI2のIBSフィルターなし
  python scan_all.py --no-consec       # RSI2の連続RSI確認なし
  python scan_all.py --vix             # RSI2のVIXフィルター有効化
"""


import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import io

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")



def run_script(label: str, cmd: list[str]) -> tuple[str, str, str]:
    """サブプロセスを実行し (label, stdout, stderr) を返す。"""
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return label, result.stdout, result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="全戦略シグナル一括スキャン（並列実行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python scan_all.py                   # 全戦略・監視銘柄
  python scan_all.py --all             # 全戦略・日経225全銘柄
  python scan_all.py --macd            # MACDのみ
  python scan_all.py --a7              # A7のみ
  python scan_all.py --rsi2            # RSI2のみ
  python scan_all.py --mode hv         # RSI2を高ボラモード強制
  python scan_all.py --no-ibs          # RSI2のIBSフィルターなし
""")
    # 戦略選択
    parser.add_argument("--macd",      action="store_true", help="MACDのみ実行")
    parser.add_argument("--a7",        action="store_true", help="A7のみ実行")
    parser.add_argument("--rsi2",      action="store_true", help="RSI2のみ実行")
    # 共通
    parser.add_argument("--all",       action="store_true", help="日経225全銘柄（全戦略に渡す）")
    # RSI2専用
    parser.add_argument("--mode",      choices=["auto", "normal", "hv"], default=None)
    parser.add_argument("--no-ibs",    dest="no_ibs",    action="store_true")
    parser.add_argument("--no-consec", dest="no_consec", action="store_true")
    parser.add_argument("--vix",       action="store_true")
    args = parser.parse_args()

    py = sys.executable

    # ── 実行する戦略を決定 ──────────────────────────────────
    run_macd = run_a7 = run_rsi2 = True
    if args.macd or args.a7 or args.rsi2:
        run_macd  = args.macd
        run_a7    = args.a7
        run_rsi2  = args.rsi2

    # ── 各コマンドを組み立て ────────────────────────────────
    tasks: list[tuple[str, list[str]]] = []

    if run_macd:
        cmd = [py, "backtest_macd_scan.py", "--signal"]
        if args.all:
            cmd.append("--all")
        tasks.append(("MACD", cmd))

    if run_a7:
        cmd = [py, "backtest_stoch_atr_trail.py", "--signal"]
        if args.all:
            cmd.append("--all")
        tasks.append(("A7", cmd))

    if run_rsi2:
        cmd = [py, "rsi2_hv.py", "--signal"]
        if args.all:
            cmd.append("--all")
        if args.mode:
            cmd += ["--mode", args.mode]
        if args.no_ibs:
            cmd.append("--no-ibs")
        if args.no_consec:
            cmd.append("--no-consec")
        if args.vix:
            cmd.append("--vix")
        tasks.append(("RSI2", cmd))

    # ── 並列実行 ────────────────────────────────────────────
    today = datetime.today().strftime("%Y-%m-%d")
    mode_s = "日経225全銘柄" if args.all else "各戦略監視銘柄"
    strats = " / ".join(label for label, _ in tasks)

    print()
    print("╔" + "═" * 70 + "╗")
    print(f"║  全戦略シグナル一括スキャン  {today}  {' ' * max(0, 32 - len(today))}║")
    print(f"║  実行戦略: {strats:<58}║")
    print(f"║  対象: {mode_s:<62}║")
    print("╚" + "═" * 70 + "╝")
    print(f"\n  {len(tasks)}戦略を並列スキャン中...\n")

    results: dict[str, tuple[str, int]] = {}
    order: list[str] = []

    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = {ex.submit(run_script, label, cmd): label
                   for label, cmd in tasks}
        for fut in as_completed(futures):
            label, stdout, rc = fut.result()
            results[label] = (stdout, rc)
            status = "✔" if rc == 0 else "✘"
            print(f"  {status} {label} 完了")

    # ── 結果表示（元の順番で） ──────────────────────────────
    print()
    for label, _ in tasks:
        stdout, rc = results[label]
        print()
        print("╔" + "═" * 70 + "╗")
        print(f"║  ■ {label} 戦略シグナル{' ' * max(0, 62 - len(label))}║")
        print("╚" + "═" * 70 + "╝")
        if rc != 0:
            print(f"  ✘ エラーが発生しました（終了コード: {rc}）")
        else:
            # 先頭の空行を除去して出力
            print(stdout.strip())
        print()

    # ── フッター ────────────────────────────────────────────
    print("═" * 72)
    print(f"  スキャン完了  {today}")
    print(f"  HTMLレポートは各戦略のフォルダに保存されています。")
    print(f"  ポジション管理: python portfolio.py")
    print("═" * 72)
    print()


if __name__ == "__main__":
    main()
