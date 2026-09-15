#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`n_quotes_<日付>.csv` の板を読む共通部品。**照会のみ。発注しない。**

★ なぜ切り出すのか
  板の読み方 (`sell_fill`) が `sim_entry_exec.py` と `sim_entry_wait.py` の
  両方に要ります。コピーすると必ず片方だけ直って食い違うので、ここ1箇所に
  置きます (CLAUDE.md §10 の `check_signal_on_date` がコピペで残った前例)。

⛔ kabu の命名では **Buy1 が最良"買い"気配** = こちらが売れる一番高い値。
  過去にコメントの向きが逆のまま1年放置された前例があるので、
  `sell1_price > buy1_price` (スプレッドが正) をデータ側で検証できるよう
  `spread_ok()` を置いてあります。
"""
from __future__ import annotations

import re

_LEVELS = 10


def f(v) -> float:
    """数値化。取れなければ 0.0。"""
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


def i(v) -> int:
    return int(f(v))


def code4(s: str) -> str:
    """`7203.T` → `7203`。"""
    return re.sub(r"\.T$", "", str(s).strip())


def true(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes")


def secs(hms) -> float:
    """時刻文字列 → 秒 (小数可)。取れなければ -1.0。

    ⛔ 数字だけ抜いて末尾6桁、はダメ (2026-09-03 に踏んだ)。kabu の
       OpeningPriceTime は `2026-09-03T09:00:50+09:00` で来るので、末尾6桁は
       `+09:00` を含んだ `500900` になり `50:09:00` が出る。**位置**を
       正規表現で当てること。
    ★ `resp_ts` は `HH:MM:SS.mmm` (ミリ秒つき) なので小数で返す。
    """
    s = str(hms).strip()
    if not s:
        return -1.0
    m = re.search(r"[T ](\d{1,2}):(\d{2}):(\d{2})(\.\d+)?", s)        # ISO
    if not m:
        m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})(\.\d+)?", s)        # 先頭が時刻
    if m:
        return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                + int(m.group(3)) + float(m.group(4) or 0))
    return -1.0


def sell_fill(row: dict, limit_px: float = 0.0, qty: int = 100) -> tuple:
    """売り注文が板でいくらになるか。

    売り指値は **指値以上でしか約定しない**ので、Buy1 から高値側に順に
    `price >= limit_px` の段だけ食える。`limit_px=0` なら成行。

    返り値: (約定単価, 約定できた数量, 使った段数)。
    ⛔⛔ 数量に届かなければ単価は **None**。途中までの加重平均を返しては
      いけない (実際より有利な値になり、成行の評価が甘くなる)。
      板は10段しか配信されないので、届かない = 「建てられない」ではなく
      **「上位10段だけでは値段を算定できない」**という意味。
    """
    got, cost, lv = 0, 0.0, 0
    for n in range(1, _LEVELS + 1):
        p = f(row.get(f"buy{n}_price"))
        q = i(row.get(f"buy{n}_qty"))
        if p <= 0 or q <= 0 or p < limit_px:
            break
        take = min(qty - got, q)
        cost += p * take
        got += take
        lv = n
        if got >= qty:
            break
    return ((cost / got if got >= qty else None), got, lv)


def spread_ok(row: dict) -> bool | None:
    """`sell1 > buy1` (最良売り気配 > 最良買い気配) か。

    板の命名の向きをデータ側で検証するための自己検査。片方でも欠ければ None。
    """
    b, s = f(row.get("buy1_price")), f(row.get("sell1_price"))
    if b <= 0 or s <= 0:
        return None
    return s > b
