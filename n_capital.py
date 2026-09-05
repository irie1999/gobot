#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新方式N の **資金スイープだけ**を回す。`.\\dailyfast` を通さない。

★ なぜ要るのか (2026-09-06 ユーザー指摘「dailyfast が長い」)
  `.\\dailyfast` は 5分足の lss/J/K タブが重い。N は **日足しか使わない**ので、
  資金の話を見るのにあれを丸ごと回すのは無駄。ここは日足のスキャンと
  後処理だけを行う(HTMLもタブも作らない)。

⛔ 計算は `newgap_core.py` に集約してある。**このファイルに数式を書かない。**
  レポート(`nikkei_analysis.py`)も同じものを import するので、
  ここの数字と `.\\dailyfast` の N タブは定義上一致する。

使い方
    python n_capital.py                      # 11.5年(既定)
    python n_capital.py --days 730           # 2年だけ(速い)
    python n_capital.py --out n_cap.txt      # ファイルにも書く
    python n_capital.py --limit 200          # 銘柄を絞る(動作確認用)
    python n_capital.py --no-price-band      # 建値の帯を外す

⚠ 初回は yfinance のキャッシュが短ければ再取得が走る(§18.53)。
  実際に取れた期間を必ず印字するので、要求の窓に届いたか確認すること。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import newgap_core as C

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--days", type=int, default=4200,
                help="遡及日数(既定4200=約11.5年)")
ap.add_argument("--min-price", type=float, default=1000.0,
                help="建値の下限(前日終値で判定)。0で無制限")
ap.add_argument("--max-price", type=float, default=6000.0,
                help="建値の上限。0で無制限")
ap.add_argument("--no-price-band", action="store_true",
                help="建値の帯を外す(--min-price 0 --max-price 0 と同じ)")
ap.add_argument("--workers", type=int, default=C._NG_WORKERS)
ap.add_argument("--limit", type=int, default=0,
                help="銘柄数の上限(動作確認用。0=全部)")
ap.add_argument("--out", type=str, default="",
                help="テキストにも書き出す(既定は画面のみ)")
a = ap.parse_args()

if a.no_price_band:
    a.min_price, a.max_price = 0.0, 0.0
_LO = float(a.min_price or 0.0)
_HI = float(a.max_price) if a.max_price and a.max_price > 0 else 1e12

# ── 銘柄ユニバース。N タブと同じ経路(5分足のファイル名)を使う ───────
try:
    from daytrade_data import available_local_symbols
    _syms = sorted({C._newgap_yf(s) for s in available_local_symbols()})
except Exception as _e:                                   # noqa: BLE001
    sys.exit(f"[error] 銘柄リストが取れません: {type(_e).__name__}: {_e}")
if a.limit > 0:
    _syms = _syms[:a.limit]
if not _syms:
    sys.exit("[error] 銘柄が0件です(stock_5min の場所を確認)")

print(f"[info] 銘柄 {len(_syms):,} / 遡及 {a.days}日({a.days / 365.25:.1f}年) / "
      f"建値 " + ("制限なし" if _LO <= 0 and _HI >= 1e11
                  else f"{_LO:,.0f}〜{_HI:,.0f}円"))
print(f"[info] 条件: 前日 ≥+{C._NG_RET1:.3f}% / watch{C._NG_WATCH} / "
      f"ギャップ ≥+{C._NG_GAP_BP:.0f}bp / {C._NG_QTY}株固定")
print(f"[info] ⛔ 5分足も lss のバックテストも使いません(日足だけ)", flush=True)

# ── スキャン。**価格帯は掛けずに**取り、あとで前日終値で絞る ─────────
#   ⛔ D+1 の始値で切ると watch50 の顔ぶれが未来で決まる(2026-09-06 修正済み)
_t0 = time.time()
_rows: list = []
_done = 0
with ThreadPoolExecutor(max_workers=max(1, a.workers)) as _ex:
    _fs = {_ex.submit(C._newgap_scan_one, s, a.days, 0.0, 1e12): s
           for s in _syms}
    for _f in as_completed(_fs):
        try:
            _rows.extend(_f.result() or [])
        except Exception:                                 # noqa: BLE001
            pass
        _done += 1
        if _done % 200 == 0:
            print(f"  … {_done:,}/{len(_syms):,}銘柄 / {len(_rows):,}銘柄日",
                  flush=True)
if not _rows:
    sys.exit("[error] スキャン結果が空です")

# ★★ 実際に取れた期間。要求した窓が取れたと思い込まない(§18.53)
_ds = sorted({r["date"] for r in _rows})
_yrs = (dt.date.fromisoformat(_ds[-1]) - dt.date.fromisoformat(_ds[0])).days / 365.25
print(f"\n[窓] **{_ds[0]}〜{_ds[-1]}**  {_yrs:.1f}年 / {len(_ds):,}営業日 / "
      f"{len(_rows):,}銘柄日 / {len(_rows) / len(_ds):.0f}銘柄per日 "
      f"({time.time() - _t0:.0f}s)")
if _yrs * 365.25 < a.days * 0.8:
    print(f"  ⛔ **要求した {a.days}日 に届いていません**。"
          f"yfinance のキャッシュが短い可能性。"
          f"$env:GOBOT_REFRESH_DATA=\"1\" で強制再取得")

# 価格帯は **前日終値**(前夜に分かる値)で掛ける
if _LO > 0 or _HI < 1e11:
    _n0 = len(_rows)
    _rows = [r for r in _rows
             if _LO <= float(r.get("prev_close") or r["entry_p"]) <= _HI]
    print(f"[帯] 前日終値 {_LO:,.0f}〜{_HI:,.0f}円 で "
          f"{_n0:,} → {len(_rows):,}銘柄日")

_out = C._newgap_capital_sweep(_rows, "short")
_hd = ["=" * 78, "■ ★ 資金が増えたらどうなるか（新方式N・100株固定）", "=" * 78,
       f"  窓 {_ds[0]}〜{_ds[-1]} ({_yrs:.1f}年 / {len(_ds):,}営業日)",
       f"  条件 前日 ≥+{C._NG_RET1:.3f}% / watch{C._NG_WATCH} / "
       f"ギャップ ≥+{C._NG_GAP_BP:.0f}bp / slip=0",
       "  ⛔ 答えるのは1つ: **律速は 予算 か watch50 か**。",
       "     『効いた日』が小さければ資金を増やしても何も起きない。",
       "     watch50 は kabu の登録上限なので外せない(§18.44/§18.45)。", ""]
print("\n" + "\n".join(_hd + _out))
if a.out:
    import os
    with open(a.out, "w", encoding="utf-8") as _f:
        _f.write("\n".join(_hd + _out) + "\n")
    print(f"\n[保存] {os.path.abspath(a.out)}")
