#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""発注の窓（既定 08:40〜09:30）が開くまで待つだけのスクリプト。

⛔⛔ **kabu には一切接続しません。発注もしません。時計を見て眠るだけ**です。
  (import しているのは argparse / datetime / sys / time だけ)

★ なぜ .bat の**先頭**に置くのか (2026-09-01)
  待機を k_open_confirm.py の中でやると、日付をまたいだときに壊れます:
    ・`_dt.date.today()` は **import 時に確定**する。22:44 に起動して翌 08:40 まで
      待つと、日付は 09-01 のまま
    ・`_mk_row` は板の OpeningPriceTime が「今日」でなければ **stale_open=1** に
      して始値を捨てる(前日の始値で誤発注しないための安全装置)。日付が
      ずれていると **全銘柄が捨てられ、1件も合格しない**
    ・出力ファイル名(k_paper_<日付>.csv 等)も前日のものになる
  → **待ってから、候補づくりも本体も起動する**。そうすればすべて当日の
    日付で走ります。

使い方:
    python wait_window.py                     # 次の 08:40 まで待つ
    python wait_window.py --until 08:40 --window-end 09:30
    python wait_window.py --max-hours 14      # これ以上待つなら中止(既定14時間)

終了コード:
    0 = 窓に入った(または最初から窓の中)。呼び出し元は続行してよい
    2 = 待たずに中止した(待ち時間が --max-hours を超える等)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time

ap = argparse.ArgumentParser(
    description="発注の窓が開くまで待つ。kabu には接続しない")
ap.add_argument("--until", default="08:40",
                help="窓の開始 HH:MM (既定 08:40)")
ap.add_argument("--window-end", default="09:30",
                help="窓の終了 HH:MM (既定 09:30)。この中なら即 続行")
ap.add_argument("--max-hours", type=float, default=14.0,
                help="これを超える待機は中止する(既定14時間)")
ap.add_argument("--weekend", action="store_true",
                help="土日も待つ(既定は平日までスキップ)")
a = ap.parse_args()


def _hm(s: str) -> tuple:
    _h, _m = (int(x) for x in str(s).split(":"))
    return _h, _m


def _next_open(now: _dt.datetime) -> _dt.datetime | None:
    """次に窓が開く時刻。窓の中なら None(=待たない)。"""
    _oh, _om = _hm(a.until)
    _ch, _cm = _hm(a.window_end)
    _lo = now.replace(hour=_oh, minute=_om, second=0, microsecond=0)
    _hi = now.replace(hour=_ch, minute=_cm, second=0, microsecond=0)
    if _lo <= now <= _hi:
        return None                      # いま窓の中
    _t = _lo if now < _lo else _lo + _dt.timedelta(days=1)
    if not a.weekend:                    # 土(5)日(6)は飛ばす
        while _t.weekday() >= 5:
            _t += _dt.timedelta(days=1)
    return _t


def main() -> int:
    _now = _dt.datetime.now()
    _t = _next_open(_now)
    if _t is None:
        print(f"[窓] いま {_now:%H:%M:%S} は窓（{a.until}〜{a.window_end}）の中です。"
              f"待たずに続行します", flush=True)
        return 0

    _left = (_t - _now).total_seconds()
    if _left > a.max_hours * 3600:
        print(f"\n⛔ 次に窓が開くのは {_t:%m/%d %H:%M} で、**{_left / 3600:.1f}時間** 先です。\n"
              f"   {a.max_hours:.0f}時間を超えるので中止します（日付や時刻の指定ミスを疑って\n"
              f"   ください）。それでも待つなら --max-hours を伸ばしてください。",
              flush=True)
        return 2

    _same = "今日" if _t.date() == _now.date() else f"{_t:%m/%d}(翌営業日)"
    print(f"\n[窓] いま {_now:%H:%M:%S} は窓（{a.until}〜{a.window_end}）の外です。\n"
          f"     **{_same} {_t:%H:%M} まで {_left / 3600:.1f}時間 待ちます**"
          f"（中断は Ctrl+C）。\n"
          f"     ⚠ 待ってから候補づくりを始めるので、当日の日付で走ります。\n"
          f"     ⚠ PC がスリープすると待機も止まります。電源設定を確認してください。",
          flush=True)

    while True:
        _left = (_t - _dt.datetime.now()).total_seconds()
        if _left <= 0:
            break
        # 残り10分未満は1分ごと / 1時間未満は5分ごと / それ以上は30分ごと
        _step = 60.0 if _left <= 600 else (300.0 if _left <= 3600 else 1800.0)
        time.sleep(min(_step, _left))
        _r = (_t - _dt.datetime.now()).total_seconds()
        if _r > 0:
            _u = f"{_r / 3600:.1f}時間" if _r > 3600 else f"{_r / 60:.0f}分"
            print(f"     … 残り {_u}  ({_dt.datetime.now():%H:%M:%S})", flush=True)

    print(f"[窓] {_dt.datetime.now():%m/%d %H:%M:%S} — 窓に入りました。続行します\n",
          flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⛔ 中断しました。発注は行っていません。", flush=True)
        sys.exit(2)
