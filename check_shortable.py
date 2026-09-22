"""check_shortable.py — 銘柄コードから「信用売建(空売り)可否」を kabu に照会する。

kabu ステーションの銘柄マスタ照会(/symbol)を使い、MarginSell(信用売建可否) 等を
表示する。発注は一切しない読み取り専用ツール。lss で「売れない銘柄」を事前に洗い出す。

使い方:
  python check_shortable.py 4662 7203 9449       # 指定銘柄を照会(デモ18081)
  python check_shortable.py 4662 --prod           # 本番(18080)で照会
  python check_shortable.py --from-csv            # ordered_signals_lss.csv の銘柄を照会
  python check_shortable.py --proposal            # 最新 lss_watchlist_proposal_*.py の選定銘柄を照会
  python check_shortable.py --proposal foo.py     # 指定した提案ファイルの選定銘柄を照会

前提: kabu ステーションが起動・ログイン済みで、KABU_API_PASSWORD が設定済み。
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob
import sys
import time as _time
from datetime import datetime
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


def _symbols_from_proposal(path: str | None) -> list[str]:
    """lss_watchlist_proposal_*.py の SELECTED=[(code,name,strat),...] から銘柄を集める。"""
    if path:
        p = Path(path)
    else:
        cands = sorted(_BASE.glob("lss_watchlist_proposal_*.py"))
        if not cands:
            print("  [!] lss_watchlist_proposal_*.py が見つかりません(scan_lss_universe で生成)。")
            return []
        p = cands[-1]
        print(f"  提案ファイル自動検出: {p.name}")
    if not p.exists():
        print(f"  [!] {p} が見つかりません。")
        return []
    try:
        ns: dict = {}
        exec(compile(p.read_text(encoding="utf-8"), str(p), "exec"), ns)
    except Exception as e:
        print(f"  [!] 提案ファイル読込失敗: {e}")
        return []
    sel = ns.get("SELECTED") or ns.get("LSS_WATCHLIST") or []
    out: list[str] = []
    seen: set[str] = set()
    for row in sel:
        c = _bare(row[0]) if isinstance(row, (list, tuple)) and row else ""
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="銘柄の信用売建(空売り)可否を kabu に照会")
    ap.add_argument("symbols", nargs="*", help="銘柄コード(複数可)。例: 4662 7203")
    ap.add_argument("--prod", action="store_true", help="本番(18080)に接続(既定デモ18081)")
    ap.add_argument("--from-csv", action="store_true",
                    help="ordered_signals_lss.csv / placed_orders_*.csv の銘柄を対象にする")
    ap.add_argument("--proposal", nargs="?", const="__AUTO__", default=None,
                    help="lss_watchlist_proposal_*.py の選定銘柄を対象(省略時は最新を自動検出)")
    ap.add_argument("--raw", action="store_true",
                    help="/symbol の生レスポンス(全フィールド)を表示する(フラグ名確認用)")
    args = ap.parse_args()

    codes = [_bare(s) for s in args.symbols if _bare(s)]
    if args.from_csv:
        codes += [c for c in _symbols_from_csv() if c not in codes]
    if args.proposal is not None:
        _pp = None if args.proposal == "__AUTO__" else args.proposal
        codes += [c for c in _symbols_from_proposal(_pp) if c not in codes]
    if not codes:
        print("銘柄を指定してください(例: python check_shortable.py 4662)、"
              "または --from-csv / --proposal を付けてください。")
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

    # 結果を CSV に逐次保存(途中で止めても残る)。20分照会を無駄にしないため。
    _date = datetime.now().strftime("%Y-%m-%d")
    _csv_path = _BASE / f"shortable_status_{_date}.csv"
    _csv_f = _csv_path.open("w", newline="", encoding="utf-8-sig")
    _csv_w = _csv.writer(_csv_f)
    _csv_w.writerow(["code", "name", "margin_buy", "margin_sell", "verdict"])

    print("=" * 68)
    print(f"信用売建(空売り)可否チェック  接続先: {env}  対象{len(codes)}銘柄")
    print(f"結果CSV: {_csv_path.name} (逐次保存)")
    print("=" * 68)
    print(f"{'コード':<7}{'銘柄名':<16}{'買建':<5}{'売建':<5}{'判定'}")
    print("-" * 68)

    ng: list[str] = []      # 空売り不可
    ok: list[str] = []      # 空売り可
    unknown: list[str] = []  # 判定不能
    for c in codes:
        try:
            info = cli.get_symbol(c)
        except Exception as e:
            print(f"{c:<7}{'(照会失敗)':<16}{'?':<5}{'?':<5}照会エラー: {e}")
            unknown.append(c)
            _csv_w.writerow([c, "", "", "", f"照会エラー:{e}"]); _csv_f.flush()
            continue
        if args.raw:
            import json as _json
            print(f"--- {c} /symbol 生レスポンス ---")
            print(_json.dumps(info, ensure_ascii=False, indent=2))
        name = str(info.get("SymbolName") or info.get("DisplayName") or "")[:14]
        mb = info.get("MarginBuy")
        ms = info.get("MarginSell")

        def _mk(v):
            return "○" if v is True else "×" if v is False else "?"

        if ms is True:
            verdict = "空売り可"; ok.append(c)
        elif ms is False:
            verdict = "空売り不可(非貸借/取扱なし)"; ng.append(c)
        else:
            verdict = "不明(MarginSellフラグ無し)"; unknown.append(c)
        print(f"{c:<7}{name:<16}{_mk(mb):<5}{_mk(ms):<5}{verdict}")
        _csv_w.writerow([c, name, _mk(mb), _mk(ms), verdict]); _csv_f.flush()
        _time.sleep(0.3)   # レート制限(429)緩和: 1銘柄=登録+照会+解除の3コール

    _csv_f.close()
    # 空売り不可リストを Python ファイルにも保存(選定の除外リストとして import できる)
    _ng_path = _BASE / "not_shortable.py"
    try:
        _ng_path.write_text(
            '"""not_shortable.py — check_shortable が検出した空売り不可(非貸借/取扱なし)銘柄。\n'
            f'生成日: {_date} / 接続先: {env}。lss 選定・発注の除外リストに使う(月1で再生成推奨)。"""\n'
            f"NOT_SHORTABLE = {sorted(ng)!r}\n", encoding="utf-8")
    except Exception as e:
        print(f"  [!] not_shortable.py 保存失敗: {e}")

    print("-" * 68)
    _tot = len(codes)
    _rate = (len(ok) / _tot * 100) if _tot else 0
    print(f"照会{_tot}銘柄: 空売り可 {len(ok)} / 不可 {len(ng)} / 不明 {len(unknown)}"
          f"  → 空売り可能率 {_rate:.0f}%")
    print(f"結果保存: {_csv_path.name} / 除外リスト: not_shortable.py ({len(ng)}銘柄)")
    if ng:
        print(f"空売り不可 → {', '.join(ng)}")
        print("  これらは lss の選定/発注から除外すべき候補です。")
    if unknown:
        print(f"判定不能 → {', '.join(unknown)}  (デモの銘柄マスタが不完全な可能性。--prod で再確認)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
