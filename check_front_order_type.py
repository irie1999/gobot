#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""kabu の執行条件 (FrontOrderType) を実データで確認する。照会のみ。

⚠ 2026-08-10: **番号は仕様表で確定済み**(kabuステーションAPI リファレンス v1.5
   「注文発注（現物・信用）」)。寄指(前場)=21。kabu_api.FOT_LIMIT_MOO に設定済み。

    10=成行  13=寄成(前場) 14=寄成(後場) 15=引成(前場) 16=引成(後場) 17=IOC成行
    20=指値  21=寄指(前場) 22=寄指(後場) 23=引指(前場) 24=引指(後場)
    25=不成(前場) 26=不成(後場) 27=IOC指値 / 30=逆指値

このツールは以下の用途で残している:
  ・実際に出した注文がどの執行条件で通ったかを **実データで** 突き合わせる
    (寄指の発注が本当に 21 で受理され、板寄せで約定しているかの確認)
  ・kabu 側の仕様変更・kabuステーションのバージョン差を検知する

やること (上から順に試す。どれか1つ当たれば確定)
-------------------------------------------------
  1. swagger (OpenAPI) 定義を取りにいく。kabuステーションが配信していれば
     FrontOrderType の enum と説明がそのまま載っている。**認証もトークンも不要**。
  2. 注文照会 (GET /orders) の実データから FrontOrderType の分布を出す。
     過去に出した注文の種類ぶんだけ番号が分かる。
  3. 1も2も足りなければ、手順を表示する
     (kabuステーションの画面から寄指を1件出す → 本スクリプトで番号を読む → 取消)。

使い方
------
    python check_front_order_type.py            # デモ(18081)
    python check_front_order_type.py --prod     # 本番(18080)
    python check_front_order_type.py --no-orders  # swagger だけ見る(トークン不要)

⛔ 照会のみ。**絶対に発注しない。**
⚠ kabu の有効トークンは1つ。発注サーバ / lss_exit_watcher 稼働中は 401 になる
  (§18.5.1)。その場合は --no-orders なら swagger だけ見られる(トークン不要)。
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from kabu_api import KabuClient, DEMO_URL, PROD_URL, FOT_LIMIT_MOO

# kabuステーションAPI リファレンス v1.5「注文発注（現物・信用）」の全一覧。
KNOWN = {
    10: "成行",
    13: "寄成(前場)",
    14: "寄成(後場)",
    15: "引成(前場)",
    16: "引成(後場)",
    17: "IOC成行",
    20: "指値",
    21: "寄指(前場)",
    22: "寄指(後場)",
    23: "引指(前場)",
    24: "引指(後場)",
    25: "不成(前場)",
    26: "不成(後場)",
    27: "IOC指値",
    30: "逆指値",
}
# gobot が実際に使っているもの(それ以外は参照用)。
USED = {10: "成行", 13: "寄成", 16: "引成(close損切り)", 20: "指値",
        30: "逆指値(現行lssのエントリー)", FOT_LIMIT_MOO: "寄指 ← H案の新規売り"}

# swagger のどこに置かれているか分からないので候補を順に試す。
SWAGGER_PATHS = [
    "/kabusapi/swagger/v1/swagger.json",
    "/swagger/v1/swagger.json",
    "/kabusapi/swagger.json",
    "/swagger.json",
    "/kabusapi/swagger/v1/swagger.yaml",
    "/swagger/v1/swagger.yaml",
]


def _walk(obj, path=""):
    """入れ子の dict/list を全部たどって (パス, ノード) を吐く。"""
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def try_swagger(base: str, timeout: float = 5.0) -> bool:
    """swagger から FrontOrderType の定義を探して表示。見つかれば True。"""
    print("── ① swagger (OpenAPI) 定義を探す ──────────────────────────")
    doc = None
    for p in SWAGGER_PATHS:
        url = base + p
        try:
            r = requests.get(url, timeout=timeout)
        except Exception as e:
            print(f"  {p:<40} 接続不可 ({type(e).__name__})")
            continue
        if r.status_code != 200:
            print(f"  {p:<40} HTTP {r.status_code}")
            continue
        try:
            doc = r.json()
        except Exception:
            # yaml はパースせず、本文から素朴に拾う
            body = r.text
            if "FrontOrderType" in body:
                print(f"  {p:<40} ✅ 取得(YAML)。FrontOrderType 周辺を抜き出します:")
                _dump_yaml_around(body)
                return True
            print(f"  {p:<40} 取得したが FrontOrderType が無い")
            continue
        print(f"  {p:<40} ✅ 取得(JSON)")
        break

    if doc is None:
        print("  → swagger は取れませんでした。②へ。\n")
        return False

    hits = []
    for path, node in _walk(doc):
        if "FrontOrderType" not in path:
            continue
        # enum / description / x-enum系 を持つノードだけ拾う
        if any(k in node for k in ("enum", "description", "x-enumNames",
                                   "x-enum-varnames")):
            hits.append((path, node))

    if not hits:
        print("  → swagger 内に FrontOrderType の enum/説明がありません。②へ。\n")
        return False

    print("\n  【swagger が持っている FrontOrderType の定義】")
    for path, node in hits:
        print(f"\n  {path}")
        desc = node.get("description")
        if desc:
            for line in str(desc).splitlines():
                if line.strip():
                    print(f"    {line.rstrip()}")
        en = node.get("enum")
        if en:
            print(f"    enum: {en}")
        for k in ("x-enumNames", "x-enum-varnames"):
            if node.get(k):
                print(f"    {k}: {node[k]}")
    print()
    return True


def _dump_yaml_around(body: str, ctx: int = 30):
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if "FrontOrderType" in ln:
            lo, hi = max(0, i - 2), min(len(lines), i + ctx)
            print("    " + "-" * 60)
            for j in range(lo, hi):
                print(f"    {lines[j].rstrip()}")
            break


def try_orders(prod: bool) -> dict:
    """注文照会から FrontOrderType の実測分布を出す。返り値 {番号: 件数}。"""
    print("── ② 注文照会 (GET /orders) の実データ ─────────────────────")
    cli = KabuClient(prod=prod, dry_run=True)
    try:
        cli.connect()
    except Exception as e:
        print(f"  接続できません: {e}")
        print("  ⚠ kabu の有効トークンは1つです。発注サーバ / lss_exit_watcher が"
              "動いていると 401 になります(§18.5.1)。片方を止めて再実行してください。\n")
        return {}
    try:
        orders = cli.get_orders()
    except Exception as e:
        print(f"  注文照会に失敗: {e}\n")
        return {}

    if not orders:
        print("  注文が1件もありません。③へ。\n")
        return {}

    cnt: dict = {}
    sample: dict = {}
    for o in orders:
        fot = o.get("FrontOrderType")
        if fot is None:
            continue
        cnt[fot] = cnt.get(fot, 0) + 1
        sample.setdefault(fot, o)

    print(f"  注文 {len(orders):,}件 から {len(cnt)}種類の FrontOrderType を検出\n")
    print(f"  {'番号':>6}  {'件数':>6}  判定")
    for fot in sorted(cnt):
        name = KNOWN.get(fot)
        if name:
            mark = f"既知 = {name}"
        else:
            o = sample[fot]
            mark = ("★ **未知** — kabu_api.py に定義がありません。"
                    f"参考: 価格={o.get('Price')} 銘柄={o.get('Symbol')} "
                    f"数量={o.get('OrderQty')}")
        print(f"  {fot:>6}  {cnt[fot]:>6}  {mark}")
    print()
    return cnt


def main() -> int:
    ap = argparse.ArgumentParser(
        description="kabu の FrontOrderType を実データから確定する(照会のみ)")
    ap.add_argument("--prod", action="store_true", help="本番(18080)。既定はデモ(18081)")
    ap.add_argument("--no-orders", action="store_true",
                    help="注文照会をせず swagger だけ見る(トークン不要)")
    a = ap.parse_args()

    base = PROD_URL if a.prod else DEMO_URL
    print("=" * 74)
    print(f"kabu 執行条件(FrontOrderType)の確認  接続先 {base}"
          f"  {'本番' if a.prod else 'デモ'}")
    print("  ⛔ 照会のみ。発注は一切しません。")
    print("=" * 74)
    print("\n【執行条件の一覧 (API リファレンス v1.5)】")
    for k in sorted(KNOWN):
        mark = f"   ← gobot: {USED[k]}" if k in USED else ""
        print(f"  {k:>4} = {KNOWN[k]}{mark}")
    print(f"\n  kabu_api.FOT_LIMIT_MOO = {FOT_LIMIT_MOO} "
          f"({KNOWN.get(FOT_LIMIT_MOO, '**一覧にない番号**')})")
    if FOT_LIMIT_MOO != 21:
        print("  ⚠ 既定は 21=寄指(前場)。環境変数 KABU_FOT_LIMIT_MOO で"
              "上書きされています。")
    print()

    ok = try_swagger(base)
    found = {} if a.no_orders else try_orders(a.prod)

    unknown = sorted(k for k in found if k not in KNOWN)
    print("── 結論 ───────────────────────────────────────────────────")
    if unknown:
        print(f"  ⚠ **一覧にない番号を検出: {unknown}**")
        print("     kabuステーションのバージョンで執行条件が増えている可能性。")
        print("     リファレンスを確認してください。")
    if FOT_LIMIT_MOO in found:
        print(f"  ✅ 寄指({FOT_LIMIT_MOO}) の注文が {found[FOT_LIMIT_MOO]}件 通っています。")
        print("     H案の発注が実際に受理されていることの確認になります。")
    elif found:
        print(f"  ・寄指({FOT_LIMIT_MOO}) の注文はまだありません(H案は未発注)。")
    if ok:
        print("  ・swagger からも定義を取得できました(上の enum を参照)。")
    print()
    print("  出典: kabuステーションAPI リファレンス v1.5「注文発注（現物・信用）」")
    print("        https://kabucom.github.io/kabusapi/ptal/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
