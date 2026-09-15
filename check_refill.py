#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""未約定だった注文が **その後 指値まで戻ったか** を調べる（読むだけ・発注しない）。

★ なぜ要るのか（2026-09-15）
  N の注文は「全銘柄が寄ったらループを抜けて取消」していたため、実質
  09:03:10 で取消されていた（N2 の不具合 / N3 で 09:10 に修正）。
  そこで残る問いが

      「09:10 まで待っていれば、あの4件は約定したのか？」

  ところが **板の記録はポーリングが止まった時点で終わっている**ので、
  09:03〜09:10 の気配は残っていない。分足が入るまで正確には言えない。

★ ただし **日足の高値** で片側だけは今夜わかる:
      その日の高値 < 指値  →  一日中 戻っていない
                          →  **09:10 まで待っても約定しなかった**（確定）
      その日の高値 ≥ 指値  →  どこかで戻っている。ただし **いつ**かは
                              日足では分からない（09:10 までとは限らない）

  つまり「待っても無駄だった」は確定できるが、「待てば約定した」は
  確定できない。**片側だけの検査**であることを忘れないこと。

⛔ 発注しない。yfinance の日足を読むだけ。日々の手順には入れない。

使い方:
    python check_refill.py --date 2026-09-15 --symbols 4768,2413,4452,6702
    python check_refill.py --date 2026-09-15            # k_paper から合格を全部
"""
from __future__ import annotations

# ⛔ Windows で `> out.txt` にリダイレクトすると cp932 になり記号で落ちる
import console_safe  # noqa: F401

import argparse
import csv as _csv
import datetime as _dt
import sys
from pathlib import Path

ap = argparse.ArgumentParser(
    description="未約定の注文がその後 指値まで戻ったかを日足で調べる")
ap.add_argument("--date", default="", help="YYYY-MM-DD（既定 今日）")
ap.add_argument("--symbols", default="",
                help="カンマ区切り。省略すると k_paper_<日付>.csv の合格を全部")
ap.add_argument("--limit", default="",
                help="指値をカンマ区切りで明示（省略時は k_paper の始値）")
a = ap.parse_args()

_d = a.date or f"{_dt.date.today():%Y-%m-%d}"
_ymd = _d.replace("-", "")

# ── 指値（= 始値）を k_paper から拾う ────────────────────────────────
_px: dict = {}
_kp = Path(f"k_paper_{_ymd}.csv")
if _kp.exists():
    with open(_kp, encoding="utf-8-sig", newline="") as f:
        for r in _csv.DictReader(f):
            try:
                if str(r.get("pass_gap") or "") in ("1", "True", "true"):
                    _o = float(r.get("open_p") or 0)
                    if _o > 0:
                        _px[str(r["symbol"]).upper().replace(".T", "")] = _o
            except Exception:
                pass
    print(f"[k_paper] {_kp.name} から合格 {len(_px)}銘柄")
else:
    print(f"⚠ {_kp.name} がありません（--symbols と --limit で明示してください）")

_syms = [s.strip().upper().replace(".T", "")
         for s in a.symbols.split(",") if s.strip()] or sorted(_px)
if a.limit:
    _lim = [float(x) for x in a.limit.split(",") if x.strip()]
    if len(_lim) != len(_syms):
        sys.exit("[error] --limit の数が --symbols と合いません")
    _px.update(dict(zip(_syms, _lim)))
_syms = [s for s in _syms if s in _px]
if not _syms:
    sys.exit("[error] 対象がありません")

# ── 日足 ──────────────────────────────────────────────────────────
import backtest_limit_entry as _ble                           # noqa: E402

print(f"\n{'=' * 74}")
print(f"■ {_d} — 未約定は『戻らなかった』のか（日足の高値で片側だけ検査）")
print(f"{'=' * 74}")
print(f"  {'銘柄':8}{'指値(始値)':>12}{'当日高値':>11}{'高値−指値':>11}"
      f"{'当日安値':>11}{'終値':>10}  判定")
_never = _maybe = _err = 0
for _s in _syms:
    _lim0 = _px[_s]
    try:
        df = _ble.fetch(f"{_s}.T", 10)
    except Exception as e:                                    # noqa: BLE001
        print(f"  {_s:8}  ⛔ 取得失敗 {type(e).__name__}: {e}")
        _err += 1
        continue
    if df is None or len(df) == 0:
        print(f"  {_s:8}  ⛔ 日足なし")
        _err += 1
        continue
    _row = df[df.index.astype(str).str[:10] == _d]
    if len(_row) == 0:
        # ⚠ 引け後でもキャッシュが古いと当日が入っていない(§13.8)。
        #   15:40 をまたぐと自動で取り直されるので、時間をおいて再実行。
        print(f"  {_s:8}  ⚠ {_d} の足がまだキャッシュにありません"
              f"（最新 {str(df.index[-1])[:10]} / 15:40 以降に再実行）")
        _err += 1
        continue
    _h = float(_row["high"].iloc[0])
    _l = float(_row["low"].iloc[0])
    _c = float(_row["close"].iloc[0])
    _dbp = (_h - _lim0) / _lim0 * 1e4
    if _h < _lim0 - 1e-9:
        _v = "✅ 一日中 戻っていない → **待っても約定しなかった**"
        _never += 1
    else:
        _v = "⚠ どこかで戻っている（09:10 までかは日足では不明）"
        _maybe += 1
    print(f"  {_s:8}{_lim0:>12,.1f}{_h:>11,.1f}{_dbp:>+10.1f}bp"
          f"{_l:>11,.1f}{_c:>10,.1f}  {_v}")

print(f"\n  ✅ 戻っていない（待っても無駄だった）  **{_never}件**")
print(f"  ⚠ 戻っている（時刻は不明）            {_maybe}件")
if _err:
    print(f"  ⛔ 調べられなかった                   {_err}件")
print(f"\n  ⛔ **これは片側だけの検査です。**「待っても無駄だった」は確定できますが、"
      f"\n     「待てば約定した」は日足では確定できません（戻った時刻が分からない）。")
print(f"  ▶ 正確に知るには 1分足が入ってから:"
      f"\n       python analyze_fill_1m.py --days 5 --workers 4")
