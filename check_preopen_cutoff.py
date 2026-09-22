#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_preopen_cutoff.py — 寄り前変数が **締切までに確定しているか** を機械的に確かめる。

⛔ 発注しない。ネットにも出ない。**時刻の table を読むだけ。**

────────────────────────────────────────────────────────────────────
★ なぜ要るのか (2026-09-19 に 3件 続けて踏んだ)
────────────────────────────────────────────────────────────────────
  日足の「終値」が何時に確定するかは **取引所ごとに違い、夏時間で動く**。
  JST に直して締切と比べないと、1〜60秒の先読みが黙って混ざる。

  ① analyze_preopen_futures.pre15_ret
       08:59 始まりの1分足の **終値** = 08:59:59。
       FUTURES_FREEZE.md の締切 08:58:59 より **60秒 後**。
  ② BTC-USD の日足
       UTC 日付で区切られるので 23:59:59 UTC = **08:59:59 JST**。
       ①とまったく同じ 60秒。別のデータ源から同じ誤りが出る。
  ③ ^NZ50 / ^AORD — ⚠ **先読みではなく「古すぎる」**
       NZX は前日 12:45〜13:45 JST に引ける。締切の **20時間前**で安全だが、
       その後に欧州も米国も動いている。つまり前日の NZ 終値は
       **S&P500 の終値より古い情報**で、上乗せがほぼ無い。
       当日ぶん(06:00〜08:58 JST)が欲しいなら **分足**が要る。
       ⛔ 「東京より早く開く」は理由にならなかった。

  ★ 一般化: 見るのは2つ。
       (a) **締切までに引けるか**  … 間に合わなければ先読み
       (b) **締切の何時間前に引けるか** … 長いほど、後から動いた市場に
            情報を追い越されている(= 足しても何も増えない)

────────────────────────────────────────────────────────────────────
使い方
────────────────────────────────────────────────────────────────────
    python check_preopen_cutoff.py
    python check_preopen_cutoff.py --cutoff 08:58:59
    python check_preopen_cutoff.py --cutoff 09:00:00   # 実運用の締切で見る

  ⚠ 夏時間で変わるので **1月と7月の両方**で判定し、悪い側を採る。
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# ── 各系列の「日足バーが実際に閉じる時刻」────────────────────────────
#   tz         … 取引所のタイムゾーン(夏時間はここが吸収する)
#   close      … 現地の引け時刻
#   label      … その日足バーに付く日付が、引けた日と同じか(0)、翌日か(+1)
#                 CME 系は 17:00 ET に引けて **その日付**が付くので 0。
#                 UTC 日付で区切る暗号資産は 23:59:59 UTC に閉じて 0。
#   kind       … "overnight" = 夜間のリスク選好を測る系列。**古いと無価値**
#                 (後に開いた欧米の終値に情報を追い越されるため)
#                 "domestic"  = 日本市場そのものの前日の状態。
#                 ⛔ こちらは古くても無価値ではない。欧米の終値には
#                   「どのサイズ・セクターが動いたか」が入っていないので、
#                   追い越されようがない。日経を捨てろとは言えない。
_SERIES = {
    # 米国株(既存の41変数)
    "^GSPC":      ("America/New_York", time(16, 0),  0, "S&P500", "overnight"),
    "^IXIC":      ("America/New_York", time(16, 0),  0, "NASDAQ", "overnight"),
    "^SOX":       ("America/New_York", time(16, 0),  0, "SOX半導体", "overnight"),
    "^VIX":       ("America/New_York", time(16, 15), 0, "VIX", "overnight"),
    "^TNX":       ("America/New_York", time(16, 0),  0, "米10年債", "overnight"),
    # 欧州(既存)
    "^GDAXI":     ("Europe/Berlin",    time(17, 30), 0, "DAX", "overnight"),
    "^STOXX50E":  ("Europe/Berlin",    time(17, 30), 0, "ユーロSTOXX50", "overnight"),
    # アジア(既存)
    "^KS11":      ("Asia/Seoul",       time(15, 30), 0, "KOSPI", "overnight"),
    "^N225":      ("Asia/Tokyo",       time(15, 30), 0, "日経平均", "domestic"),
    # 24時間もの(既存)。CME は 17:00 ET 引け
    "JPY=X":      ("America/New_York", time(17, 0),  0, "USDJPY", "overnight"),
    "DX-Y.NYB":   ("America/New_York", time(17, 0),  0, "ドル指数", "overnight"),
    "CL=F":       ("America/New_York", time(17, 0),  0, "WTI原油", "overnight"),
    "GC=F":       ("America/New_York", time(17, 0),  0, "金", "overnight"),
    "ES=F":       ("America/New_York", time(17, 0),  0, "S&P500先物", "overnight"),
    "NKD=F":      ("America/New_York", time(17, 0),  0, "日経225先物(CME)", "overnight"),
    # ── ここから 2026-09-19 の候補 ──────────────────────────────
    "^TOPX":      ("Asia/Tokyo",       time(15, 30), 0, "★TOPIX", "domestic"),
    "2516.T":     ("Asia/Tokyo",       time(15, 30), 0, "★グロース250(ETF)", "domestic"),
    "^NZ50":      ("Pacific/Auckland", time(16, 45), 0, "★ニュージーランド", "overnight"),
    "^AORD":      ("Australia/Sydney", time(16, 0),  0, "★オーストラリア", "overnight"),
    "BTC-USD":    ("UTC",              time(23, 59), 0, "★ビットコイン", "overnight"),
    "^BVSP":      ("America/Sao_Paulo", time(17, 0), 0, "★ブラジル", "overnight"),
}


def _close_jst(tz: str, close: time, label_off: int, day: datetime) -> datetime:
    """`day` という日付が付いた日足バーが、JST の何時に閉じたかを返す。"""
    # バーの日付 day に対し、実際に引けたのは day - label_off 日
    d = (day - timedelta(days=label_off)).date()
    local = datetime.combine(d, close, tzinfo=ZoneInfo(tz))
    return local.astimezone(JST)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cutoff", default="08:58:59",
                    help="判定の締切(JST)。既定 08:58:59 = FUTURES_FREEZE.md")
    a = ap.parse_args()
    hh, mm, ss = (int(x) for x in a.cutoff.split(":"))
    cut = time(hh, mm, ss)

    print("=" * 78)
    print(f" 寄り前変数の締切チェック — 締切 {a.cutoff} JST")
    print("=" * 78)
    print("\n  `_prev_bars(k < d)` は **前営業日の日足**を取る。")
    print("  そのバーが JST の何時に閉じるかを、冬(1月)と夏(7月)の両方で見る。\n")
    print(f"  {'ティッカー':<12}{'名前':<18}{'冬 JST':>11}{'夏 JST':>11}"
          f"{'古さ':>7}  判定")
    print("  " + "-" * 76)

    ng, stale = [], []
    rows = []
    for tkr, (tz, cl, off, name, kind) in _SERIES.items():
        outs = []
        for probe in (datetime(2025, 1, 15), datetime(2025, 7, 15)):
            # 取引日 d の朝に使えるのは、日付 d-1 が付いたバー
            end = _close_jst(tz, cl, off, probe)
            # そのバーの終わりが「翌営業日の朝の締切」より前か
            deadline = datetime.combine((probe + timedelta(days=1)).date(),
                                        cut, tzinfo=JST)
            outs.append((end, end <= deadline, (deadline - end).total_seconds()))
        (e1, ok1, g1), (e2, ok2, g2) = outs
        ok = ok1 and ok2
        # ★ 古さ = 締切の何時間前に確定したか。大きいほど後続市場に追い越される
        age = max(g1, g2) / 3600.0
        rows.append((tkr, name, e1, e2, age, ok, kind))
        if not ok:
            ng.append((tkr, name, e1, e2))

    # 古い順に並べると「追い越されている系列」が上に来る
    for tkr, name, e1, e2, age, ok, kind in sorted(rows, key=lambda r: -r[4]):
        mark = "✅" if ok else "⛔ 間に合わない"
        if ok and kind == "domestic":
            mark = "✅ 国内(前日の状態そのもの。古さは問題にならない)"
        elif ok and age > 12.0:
            mark = "⚠ **古い**(後続市場に追い越されている)"
            stale.append((tkr, name, age))
        print(f"  {tkr:<12}{name:<18}{e1.strftime('%m/%d %H:%M'):>11}"
              f"{e2.strftime('%m/%d %H:%M'):>11}{age:>6.1f}h  {mark}")

    if stale:
        print("\n  ⚠ 『古い』= 確定してから締切までに、欧州や米国が動いている。")
        print("     その情報は後続市場の終値に既に織り込まれているので、")
        print("     変数として足しても **独立した情報にならない**")
        print("     (検定数だけ増える)。当日ぶんが欲しいなら分足が要る。")

    print("\n" + "=" * 78)
    if ng:
        print(f" ⛔ {len(ng)}件が締切に間に合いません")
        print("=" * 78)
        for tkr, name, e1, e2 in ng:
            print(f"\n  {name} ({tkr})")
            print(f"    前営業日ぶんのバーが閉じるのは 冬 {e1.strftime('%H:%M')} / "
                  f"夏 {e2.strftime('%H:%M')} JST")
            print(f"    → 締切 {a.cutoff} を過ぎている = **日足は使えない**")
            print("    → 当日の値が欲しいなら **分足**が要る。"
                  "2営業日前まで下げれば日足でも安全だが、情報は1日古くなる")
    else:
        print(" ✅ 全系列が締切までに確定しています")
    print("=" * 78)
    print("\n★ 作法: 新しい系列を足したら **必ずこれを通す**。")
    print("  「東京より早く開く」は理由にならない。**早く引けるか**で決まる。")


if __name__ == "__main__":
    main()
