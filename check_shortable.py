"""check_shortable.py — 銘柄コードから「信用売建(空売り)可否」を kabu に照会する。

kabu ステーションの銘柄マスタ照会(/symbol)を使い、MarginSell(信用売建可否) 等を
表示する。発注は一切しない読み取り専用ツール。lss で「売れない銘柄」を事前に洗い出す。

使い方:
  python check_shortable.py 4662 7203 9449       # 指定銘柄を照会(デモ18081)
  python check_shortable.py 4662 --prod           # 本番(18080)で照会
  python check_shortable.py --from-csv            # ordered_signals_lss.csv の銘柄を照会
  python check_shortable.py --watchlist           # lss選定(placed_orders/提案)の銘柄を照会

前提: kabu ステーションが起動・ログイン済みで、KABU_API_PASSWORD が設定済み。
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

# Windows(cp932)コンソールで表示が落ちないように
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

_BASE = Path(__file__).resolve().parent


def _bare(code: str) -> str:
    """'4662.T' 等を素の kabu コード '4662' にする。"""
    return str(code).upper().removesuffix(".T").split(".")[0].strip()


def _symbols_from_csv() -> list[str]:
    """ordered_signals_lss.csv + placed_orders_*.csv(side=short非_S) から銘柄を集める。"""
    import csv
    out: list[str] = []
    seen: set[str] = set()

    p = _BASE / "ordered_signals_lss.csv"
    if p.exists():
        try:
            with p.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    c = _bare(row.get("symbol") or row.get("code") or "")
                    if c and c not in seen:
                        seen.add(c); out.append(c)
        except Exception as e:
            print(f"  [!] ordered_signals_lss.csv 読込失敗: {e}")

    for fp in sorted(glob.glob(str(_BASE / "placed_orders_*.csv"))):
        try:
            with open(fp, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("side", "")).lower() != "short":
                        continue
                    if str(row.get("strategy", "")).upper().endswith("_S"):
                        continue   # メインショート(_S)は lss ではない
                    c = _bare(row.get("symbol") or "")
                    if c and c not in seen:
                        seen.add(c); out.append(c)
        except Exception as e:
            print(f"  [!] {Path(fp).name} 読込失敗: {e}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="銘柄の信用売建(空売り)可否を kabu に照会")
    ap.add_argument("symbols", nargs="*", help="銘柄コード(複数可)。例: 4662 7203")
    ap.add_argument("--prod", action="store_true", help="本番(18080)に接続(既定デモ18081)")
    ap.add_argument("--from-csv", action="store_true",
                    help="ordered_signals_lss.csv / placed_orders_*.csv の銘柄を対象にする")
    args = ap.parse_args()

    codes = [_bare(s) for s in args.symbols if _bare(s)]
    if args.from_csv:
        codes += [c for c in _symbols_from_csv() if c not in codes]
    if not codes:
        print("銘柄を指定してください(例: python check_shortable.py 4662)、"
              "または --from-csv を付けてください。")
        return 1

    from kabu_api import KabuClient
    env = "本番(18080)" if args.prod else "デモ(18081)"
    cli = KabuClient(prod=args.prod, dry_run=True)   # 読み取りのみ。発注はしない
    try:
        cli.connect()
    except Exception as e:
        print(f"[X] kabu 接続失敗: {e}")
        print("  (kabu ステーションの起動+ログインと KABU_API_PASSWORD が必要です)")
        return 1

    print("=" * 68)
    print(f"信用売建(空売り)可否チェック  接続先: {env}  対象{len(codes)}銘柄")
    print("=" * 68)
    print(f"{'コード':<7}{'銘柄名':<16}{'買建':<5}{'売建':<5}{'判定'}")
    print("-" * 68)

    ng: list[str] = []
    for c in codes:
        try:
            info = cli.get_symbol(c)
        except Exception as e:
            print(f"{c:<7}{'(照会失敗)':<16}{'?':<5}{'?':<5}照会エラー: {e}")
            continue
        name = str(info.get("SymbolName") or info.get("DisplayName") or "")[:14]
        mb = info.get("MarginBuy")
        ms = info.get("MarginSell")

        def _mk(v):
            return "○" if v is True else "×" if v is False else "?"

        if ms is True:
            verdict = "空売り可"
        elif ms is False:
            verdict = "空売り不可(非貸借/取扱なし)"; ng.append(c)
        else:
            verdict = "不明(MarginSellフラグ無し)"
        print(f"{c:<7}{name:<16}{_mk(mb):<5}{_mk(ms):<5}{verdict}")

    print("-" * 68)
    if ng:
        print(f"空売り不可: {len(ng)}銘柄 → {', '.join(ng)}")
        print("  これらは lss の選定/発注から除外すべき候補です。")
    else:
        print("空売り不可の銘柄はありませんでした(または全て判定不能)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
