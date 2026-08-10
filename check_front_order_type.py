#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""kabu の執行条件 (FrontOrderType) の番号を **実データから** 確定する。照会のみ。

なぜ必要か
----------
H案(前日終値の指値・寄付のみ有効 = 寄指)を実発注するには FrontOrderType の
番号が要る。kabu_api.py が持っているのは以下の5つだけで、**寄指は未定義**:

    10=成行 / 13=寄成 / 16=引成 / 20=指値 / 30=逆指値

CLAUDE.md の方針どおり **番号は推測しない**。ここで実際の kabu から取る。

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

from kabu_api import (KabuClient, DEMO_URL, PROD_URL,
                      FOT_MARKET, FOT_MOO, FOT_MOC, FOT_LIMIT, FOT_STOP)

# kabu_api.py が現に使っている番号。ここに無いものが「未確定」。
KNOWN = {
    FOT_MARKET: "成行",
    FOT_MOO: "寄成",
    FOT_MOC: "引成",
    FOT_LIMIT: "指値",
    FOT_STOP: "逆指値",
}

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
    print("\n【kabu_api.py が現に使っている番号】")
    for k in sorted(KNOWN):
        print(f"  {k:>4} = {KNOWN[k]}")
    print("  ※ **寄指(寄付のみ有効な指値)は未定義**。これを確定するのが目的です。\n")

    ok = try_swagger(base)
    found = {} if a.no_orders else try_orders(a.prod)

    unknown = sorted(k for k in found if k not in KNOWN)
    print("── 結論 ───────────────────────────────────────────────────")
    if unknown:
        print(f"  未知の番号を検出: {unknown}")
        print("  この中に寄指があるかは、その注文を kabu ステーションの注文照会画面で")
        print("  見て執行条件を突き合わせれば確定します。")
    if ok:
        print("  swagger に定義が出ています。上の enum / description を正としてください。")
    if not ok and not unknown:
        print("  ⚠ **確定できませんでした。**")
        print()
        print("  ③ 手動で1件出して読む(最も確実。数分で終わります):")
        print("     1. kabuステーションの通常の発注画面で、**約定しない価格**の")
        print("        寄指を1件入れる(例: 現値からかけ離れた指値の売り)。")
        print("        ⚠ 板寄せで約定しうるので、必ず現値から十分離すこと。")
        print("     2. python check_front_order_type.py"
              f"{' --prod' if a.prod else ''}")
        print("        → ②の表に **未知の番号** が1つ増える。それが寄指。")
        print("     3. kabuステーションの画面からその注文を取消す。")
        print("     4. 出た番号を kabu_api.py の FOT_LIMIT_MOO に設定する。")
    print()
    print("  参考: 執行条件の一覧は kabuステーションAPIリファレンスに載っています。")
    print("        https://kabucom.github.io/kabusapi/reference/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
