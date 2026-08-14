r"""fetch_sectors.py — 銘柄のセクター(業種)を yfinance から取ってキャッシュする。

なぜ要るか (2026-08-14)
-----------------------
発注リストが銀行に偏る日がある(2026-08-14 の実発注は7件中4件が銀行系)。
CLAUDE.md §13.8 に「セクター制約なし。地銀7-8銘柄のような集中が起きうる。
出力CSVにセクター情報は無いので目視で確認するしかない」と書いたまま
未対応だったので、まず **どれくらい偏っているか** を測れるようにする。

⛔ 証券コードの帯では分類できない
  8301-8599 が銀行、という古い割当は新規上場に当てはまらない。
  実例: 楽天銀行 5838 / 京都フィナンシャルグループ 5844 は「ゴム製品」帯。
  近年の上場は業種と無関係な空き番号が割り当てられるため、
  **コードから業種を推定してはいけない**。

これは「選定軸」ではなく「分散軸」
----------------------------------
BT・属性・流動性・戦略一致は **期待値を上げる** ために掃いて全部ヌルだった
(§18.12 / §18.13 / §18.24)。セクターは目的が違い、**σ を下げる** ためのもの。
§18.30 で「σ を削るなら市場ヘッジではなく1件ごとのバラつきを叩くしかない」と
結論しており、金額均等(§18.36 の宿題)と同じ系統になる。

使い方
------
  python fetch_sectors.py                 # lss_trades*.csv の銘柄を取る
  python fetch_sectors.py --symbols 5838,8331,8304
  python fetch_sectors.py --refetch       # キャッシュを無視して取り直す
  python fetch_sectors.py --report        # 取得済みの内訳を表示するだけ

出力: sectors.json  {"5838.T": {"sector": "Financial Services",
                                "industry": "Banks - Regional", "name": "..."}}
1銘柄1リクエストなので数百銘柄で数分かかる。キャッシュがあるものは飛ばす。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_BASE = Path(__file__).resolve().parent
_OUT = _BASE / "sectors.json"

ap = argparse.ArgumentParser(description="銘柄のセクターを yfinance から取得")
ap.add_argument("--symbols", default=None, help="カンマ区切り(省略時は CSV から集める)")
ap.add_argument("--csv-glob", default="lss_trades*.csv",
                help="銘柄を集める CSV のグロブ")
ap.add_argument("--refetch", action="store_true", help="キャッシュを無視して取り直す")
ap.add_argument("--report", action="store_true", help="取得済みの内訳を出すだけ")
ap.add_argument("--sleep", type=float, default=0.15, help="銘柄間の小休止秒")
args = ap.parse_args()


def _norm(sym: str) -> str:
    s = str(sym).strip().upper()
    if s.endswith(".T"):
        return s
    if len(s) == 5 and s.isdigit() and s[-1] == "0":
        return s[:4] + ".T"
    return s + ".T"


def _load() -> dict:
    if _OUT.exists():
        try:
            with open(_OUT, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _targets() -> list[str]:
    if args.symbols:
        return [_norm(x) for x in str(args.symbols).split(",") if x.strip()]
    seen = set()
    for fp in sorted(glob.glob(str(_BASE / args.csv_glob))):
        try:
            with open(fp, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    sym = str(r.get("symbol") or "").strip()
                    if sym:
                        seen.add(_norm(sym))
        except Exception:
            continue
    return sorted(seen)


def _report(cache: dict) -> int:
    if not cache:
        print("sectors.json が空です。先に取得してください。")
        return 1
    from collections import Counter
    c = Counter(v.get("industry") or v.get("sector") or "不明"
                for v in cache.values())
    cs = Counter(v.get("sector") or "不明" for v in cache.values())
    print(f"■ セクター内訳  {len(cache)}銘柄")
    print("=" * 62)
    print("  【セクター】")
    for k, n in cs.most_common():
        print(f"    {k:<34}{n:>4}銘柄 {n / len(cache) * 100:>5.1f}%")
    print()
    print("  【業種(industry) 上位20】")
    for k, n in c.most_common(20):
        print(f"    {k:<34}{n:>4}銘柄 {n / len(cache) * 100:>5.1f}%")
    _bank = sum(n for k, n in c.items() if "Bank" in str(k))
    if _bank:
        print()
        print(f"  ★ 銀行(industry に Bank を含む): {_bank}銘柄 "
              f"({_bank / len(cache) * 100:.1f}%)")
    return 0


def main() -> int:
    cache = _load()
    if args.report:
        return _report(cache)

    syms = _targets()
    if not syms:
        print(f"[error] 対象銘柄が見つかりません({args.csv_glob} / --symbols)。")
        return 1
    todo = [s for s in syms if args.refetch or s not in cache]
    print(f"■ セクター取得  対象 {len(syms)}銘柄 / 未取得 {len(todo)}銘柄")
    print(f"  出力: {_OUT}")
    if not todo:
        print("  すべてキャッシュ済み。--refetch で取り直せます。")
        print()
        return _report(cache)

    import yfinance as yf
    ok = fail = 0
    for i, sym in enumerate(todo, 1):
        try:
            info = yf.Ticker(sym).info or {}
            sec = info.get("sector") or ""
            ind = info.get("industry") or ""
            nm = info.get("shortName") or info.get("longName") or ""
            if not (sec or ind):
                fail += 1
            else:
                cache[sym] = {"sector": sec, "industry": ind, "name": nm}
                ok += 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"  {sym}: {type(e).__name__}: {e}")
        if i % 25 == 0 or i == len(todo):
            print(f"    {i}/{len(todo)} … 取得{ok} / 失敗{fail}", flush=True)
            with open(_OUT, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
        if args.sleep > 0:
            time.sleep(args.sleep)

    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\n  取得 {ok} / 失敗 {fail} / 累計 {len(cache)}銘柄 → {_OUT}")
    print()
    return _report(cache)


if __name__ == "__main__":
    raise SystemExit(main())
