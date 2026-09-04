#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PTS(私設取引システム)の板が kabu で取れるかを確かめる。**照会のみ。**

⛔⛔ このスクリプトは **1円も発注しません**。
   `send_*` 系のメソッドを一切呼ばず、import もしていません。

★ なぜ要るのか
  N の判定は「09:00 の始値が前日終値 +100bp 以上」。09:00 に板を読むと
  kabu の登録上限50件が壁になり、それを超えると読み終わる頃に値幅が逃げる
  (§18.44: 299銘柄で3.6分 / その間 -30bp)。
  **前夜に PTS で読めるならこの時間制約が消える**ので、§18.45 で棄却した
  K(watch無制限)が復活しうる。

★ 用途を混同しないこと (§18.35b で一度間違えた)
    用途A … PTS だけで判定して発注     → 誤建てがそのまま損。高い精度が要る
    用途B … PTS で **登録する50件を選ぶ**。判定は 09:00 の始値
            → 誤選択は 09:00 で弾かれる = **ほぼ無料**。30%の精度でも足りる
  **本命はB。** だからこのツールは「PTS 価格が当たるか」ではなく
  **「PTS で並べた上位N件が、翌朝の合格銘柄をどれだけ捕まえるか」**を測る。

⛔ バックテストできない。PTS の履歴はどのデータにも無い(§18.35 と同じ)。
   **今日から貯めるしかない。**

使い方
    python check_pts.py --prod                     # 取れるかの確認だけ
    python check_pts.py --prod --symbols 7203,6758 # 銘柄を指定
    python check_pts.py --prod --save              # pts_<日付>.csv に追記
"""
from __future__ import annotations

import argparse
import csv
import datetime
import os
import sys

JST = datetime.timezone(datetime.timedelta(hours=9))

# ⛔ 発注系を一切 import しない。KabuClient は照会にだけ使う
from kabu_api import (KabuClient, EXCHANGE_TOSHO, EXCHANGE_SOR,   # noqa: E402
                      EXCHANGE_TOKYO_PLUS)

# 試す市場コード。**27(東証＋)が本命** = auカブコムの東証+PTS 統合板。
#   1 … 東証。日中しか値が付かないはず(比較の基準)
#   9 … SOR。発注用なので板が返らない可能性が高い
_EX = [(EXCHANGE_TOSHO, "東証"),
       (EXCHANGE_SOR, "SOR"),
       (EXCHANGE_TOKYO_PLUS, "東証＋(PTS込み?)")]

_KEYS = ["CurrentPrice", "CurrentPriceTime", "PreviousClose",
         "TradingVolume", "BidPrice", "AskPrice", "OpeningPrice"]


def _sess(now: datetime.datetime) -> str:
    """いま どのセッションか。PTS が動くのは夜間と早朝。"""
    t = now.hour * 60 + now.minute
    if 8 * 60 + 20 <= t < 9 * 60:
        return "PTSデイタイム前半(東証は未寄り)  ★ここで取れれば用途Bに直結"
    if 9 * 60 <= t < 15 * 60 + 30:
        return "東証ザラ場"
    if 15 * 60 + 30 <= t < 16 * 60 + 30:
        return "東証引け後 / PTS 準備中"
    if 16 * 60 + 30 <= t or t < 6 * 60:
        return "PTSナイトタイム  ★ここで取れれば時間制約が消える"
    return "場外"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prod", action="store_true",
                    help="本番(18080)。省略するとデモ(18081)")
    ap.add_argument("--symbols", default="",
                    help="カンマ区切り。省略すると当日の候補 → 主要銘柄")
    ap.add_argument("--max", type=int, default=5,
                    help="試す銘柄数(既定5)。**登録上限50件を無駄に消費しない**")
    ap.add_argument("--save", action="store_true",
                    help="pts_<日付>.csv に追記(夜ごとに貯めると比較できる)")
    a = ap.parse_args()

    _now = datetime.datetime.now(JST)
    print(f"[時刻] {_now:%Y-%m-%d %H:%M:%S} JST … {_sess(_now)}")

    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    if not syms:
        # 当日の候補を使えるなら使う(無ければ流動性の高い主要銘柄)
        _f = f"k_signals_{_now:%Y%m%d}.csv"
        if os.path.exists(_f):
            with open(_f, encoding="utf-8-sig") as fh:
                syms = [r.get("symbol", "").split(".")[0]
                        for r in csv.DictReader(fh)][:a.max]
            print(f"[銘柄] {_f} の先頭{len(syms)}件")
        if not syms:
            syms = ["7203", "6758", "9984", "8306", "6501"]
            print(f"[銘柄] 既定の主要5銘柄(候補ファイルが無いため)")
    syms = syms[:a.max]

    cli = KabuClient(prod=a.prod, dry_run=True)      # dry_run: 発注は物理的に不可
    cli.connect()
    print(f"[接続] {'本番' if a.prod else 'デモ'} / dry_run=True\n")

    rows = []
    for ex, lbl in _EX:
        print(f"── Exchange={ex} ({lbl}) ──")
        _ok = 0
        for s in syms:
            try:
                b = cli.get_board(s, ex) or {}
            except Exception as e:                    # noqa: BLE001
                print(f"   {s}  ⛔ {type(e).__name__}: {str(e)[:70]}")
                rows.append({"ts": _now.isoformat(), "exchange": ex,
                             "symbol": s, "error": type(e).__name__})
                continue
            _cp = b.get("CurrentPrice")
            _has = _cp is not None and float(_cp or 0) > 0
            _ok += 1 if _has else 0
            print(f"   {s}  " + ("✅" if _has else "⛔値なし") + "  "
                  + " / ".join(f"{k}={b.get(k)}" for k in _KEYS[:4]))
            rows.append({"ts": _now.isoformat(), "exchange": ex, "symbol": s,
                         **{k: b.get(k) for k in _KEYS}})
        print(f"   → 値が付いた {_ok}/{len(syms)}銘柄\n")

    # ★ 判定。**値が付いただけでは足りない**(東証の最終値をそのまま返している
    #   だけかもしれない)。時刻が動いているかを見ること
    print("★ 読み方")
    print("  1. Exchange=27 だけ CurrentPrice が付いて 1 が付かない → **PTS が取れている**")
    print("  2. どの Exchange も同じ値 → 東証の最終値を返しているだけ。PTS ではない")
    print("     `CurrentPriceTime` が **15:30 のまま**ならこれ")
    print("  3. 全部 値なし → kabu は PTS を配信していない。別ソースが要る")
    print("  ⚠ 夜間に複数回(例 17時/20時/23時)走らせて、**値が動くか**を必ず見ること。")
    print("     1回だけでは 1 と 2 を見分けられません")

    if a.save and rows:
        _p = f"pts_{_now:%Y%m%d}.csv"
        _new = not os.path.exists(_p)
        with open(_p, "a", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            if _new:
                w.writeheader()
            w.writerows(rows)
        print(f"\n[保存] {_p} に {len(rows)}行 追記")
    return 0


if __name__ == "__main__":
    sys.exit(main())
