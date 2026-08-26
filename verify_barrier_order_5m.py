"""verify_barrier_order_5m.py — 日足の『損切り優先(悲観)』がどれだけ外れるかを5分足で測る

★ 何のためか (2026-08-26)
────────────────────────────────────────────────────────────────────
§18.55 は損切り/利確のスイープを **日足** で回すことにした。5分足は
2024-07 が最古(§18.6)で TRAIN(2015-2020)に1本も無いため、それしか手が無い。

日足では「高値と安値のどちらが先か」が分からないので、両方に触れた日は
**損切り優先(悲観)** で処理する。ここで測るのは **その悲観がどれだけ外れるか**。

  悲観     両方タッチ → 全部 損切り
  真実     5分足の順序で決める(同一5分足の中で両方なら決められない)
  楽観     両方タッチ → 全部 利確

⛔ **これは手法の検査であって、設定を選ぶ探索ではない。**
   どの (損切, 利確) が儲かったかは **出さない**。出した瞬間に §18.55 の
   事前宣言(日足で判定 → ✅ が出たら5分足で検証)が壊れる。
   出すのは「悲観と真実の差」だけ。

使い方
------
  python verify_barrier_order_5m.py
  python verify_barrier_order_5m.py --data-dir stock_5min_subset
  python verify_barrier_order_5m.py --sm-list 0.25,0.5,1.0 --tm-list 0.5,1.0,2.0
"""
from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

ap = argparse.ArgumentParser(description="日足の悲観仮定の誤差を5分足で測る")
ap.add_argument("--data-dir", type=str, default="stock_5min_subset",
                help="5分足の場所。既定は export_intraday_cache --import の復元先")
ap.add_argument("--atr", action="store_true", default=True,
                help="幅を **ATR倍** で解釈する(既定)。日足スイープ"
                     "(analyze_gap_edge --sweep-barrier)が ATR 倍なので合わせる")
ap.add_argument("--pct", dest="atr", action="store_false",
                help="幅を %% で解釈する")
ap.add_argument("--sm-list", type=str, default="0.1,0.3,0.5,1.0",
                help="損切り幅(ショートなので建値の上)")
ap.add_argument("--tm-list", type=str, default="0.0,1.0,2.0",
                help="利確幅(建値の下)。0 = 利確なし")
ap.add_argument("--min-bars", type=int, default=10,
                help="この本数未満の日は捨てる(半日立会など)")
ap.add_argument("--wide-pct", type=float, default=30.0,
                help="1日の値幅がこれを超えたら分割汚染の疑いとして報告する")
a = ap.parse_args()

_D = Path(a.data_dir)
if not _D.exists() or not any(_D.glob("*.pkl")):
    raise SystemExit(f"[error] {_D} に .pkl がありません\n"
                     f"        python export_intraday_cache.py --import してください")
SM = [float(x) for x in a.sm_list.split(",") if x.strip()]
TM = [float(x) for x in a.tm_list.split(",") if x.strip()]
_INF = 10 ** 9

# ⛔ 幅の単位を取り違えると対応がズレる(2026-08-26 に実際にやった)。
#    日足スイープは **ATR倍**。ここを % で測ると利確が3倍狭くなり、
#    両方タッチ率が 5% → 49% と一桁変わって、悲観バイアスを過大評価する。
#    ATR は日足が要るので、**日中レンジの中央値**で代理する。
_ATR_PCT = 1.0

# acc[(s,t)] = [悲観, 楽観, 真実(不明は悲観側), 真実(不明は楽観側),
#               両方タッチ, 損切りが先, 5分でも不明]
_acc: dict[tuple, list] = {}
_n = 0
_first: dict[str, int] = {}
_wide: list[tuple] = []
_days: list[tuple] = []          # (high[], low[], open, close)

for f in sorted(_D.glob("*.pkl")):
    try:
        df = pickle.load(open(f, "rb"))
    except Exception:
        continue
    if not isinstance(df, pd.DataFrame) or df.empty:
        continue
    for _d, g in df.groupby(df.index.normalize()):
        if len(g) < a.min_bars:
            continue
        o = float(g["open"].iloc[0])
        if not (o > 0):
            continue
        _n += 1
        _k0 = g.index[0].strftime("%H:%M")
        _first[_k0] = _first.get(_k0, 0) + 1
        hi = g["high"].to_numpy(float)
        lo = g["low"].to_numpy(float)
        cl = float(g["close"].iloc[-1])
        _hh, _ll = float(hi.max()), float(lo.min())
        if (_hh - _ll) / o * 100.0 > a.wide_pct:
            _wide.append((f.stem, str(_d.date()), o, _hh, _ll,
                          (_hh - _ll) / o * 100.0))
        _days.append((hi, lo, o, cl))

if _n == 0:
    raise SystemExit("[error] 読める銘柄日がありません")

# ── 幅の単位を決める ────────────────────────────────────────────
if a.atr:
    _ATR_PCT = float(np.median([(h.max() - l.min()) / o * 100.0
                                for h, l, o, _ in _days]))
    _U = f"×ATR"
else:
    _ATR_PCT = 1.0
    _U = "%"

for hi, lo, o, cl in _days:
        _cbp = (o - cl) / o * 1e4          # 引けまで持った場合の bp(ショート)
        for s in SM:
            _sp = s * _ATR_PCT
            _hs = hi >= o * (1 + _sp / 100)
            _is = int(np.argmax(_hs)) if _hs.any() else _INF
            for t in TM:
                _tp = t * _ATR_PCT
                _ht = (lo <= o * (1 - _tp / 100)) if _tp > 0 else np.zeros_like(lo, bool)
                _it = int(np.argmax(_ht)) if _ht.any() else _INF
                _sbp, _tbp = -_sp * 100.0, _tp * 100.0
                _both = _is != _INF and _it != _INF
                _amb5 = _both and _is == _it       # 同一5分足の中で両方
                if _is == _INF and _it == _INF:
                    _pes = _opt = _trp = _tro = _cbp
                elif not _both:
                    _pes = _opt = _trp = _tro = (_tbp if _is == _INF else _sbp)
                elif _amb5:
                    # 日足も5分足も決められない。両極を持つ
                    _pes, _opt, _trp, _tro = _sbp, _tbp, _sbp, _tbp
                else:
                    # 日足は決められないが5分足なら決まる
                    _win = _sbp if _is < _it else _tbp
                    _pes, _opt, _trp, _tro = _sbp, _tbp, _win, _win
                _v = _acc.setdefault((s, t), [0.0] * 4 + [0, 0, 0])
                _v[0] += _pes; _v[1] += _opt; _v[2] += _trp; _v[3] += _tro
                _v[4] += int(_both)
                _v[5] += int(_both and not _amb5 and _is < _it)
                _v[6] += int(_amb5)

print(f"{'=' * 78}")
print(f"■ 母集団 — {_D} / {_n:,}銘柄日")
print(f"{'=' * 78}")
if a.atr:
    print(f"  幅の単位 = **ATR倍**(日足スイープに合わせる)。"
          f"ATR% は日中レンジの中央値 **{_ATR_PCT:.2f}%** で代理")
    print(f"  ⛔ ここを %% で測ると日足スイープと対応がズレる。"
          f"2026-08-26 に実際にやって、悲観バイアスを一桁 過大評価した")
else:
    print(f"  幅の単位 = **%%**")
print(f"  ⚠ これは export_intraday_cache で切り出した **部分データ**。"
      f"manifest の条件で絞られている")

print(f"\n{'=' * 78}\n■ 日足がどれだけ判別不能か\n{'=' * 78}")
print(f"{'損切':>6}{'利確':>7}{'両方タッチ':>16}{'うち損切が先':>16}"
      f"{'5分でも不明':>15}")
print("-" * 62)
for s in SM:
    for t in TM:
        v = _acc[(s, t)]
        nb, st, am = v[4], v[5], v[6]
        print(f"{s:>6.2f}{t:>7.2f}{nb:>9,}({nb / _n * 100:>4.1f}%)"
              f"{st:>10,}({st / max(nb, 1) * 100:>3.0f}%)"
              f"{am:>9,}({am / max(nb, 1) * 100:>3.0f}%)")

print(f"\n{'=' * 78}\n■ 悲観はどれだけ低く出るか (bp/銘柄日)\n{'=' * 78}")
print(f"  ⛔ 水準(どの設定が良いか)は出さない。**差だけ**(§18.55 の事前宣言)")
print(f"\n{'損切':>6}{'利確':>7}{'真実−悲観':>14}{'楽観−悲観':>13}"
      f"{'真実の幅':>20}")
print("-" * 62)
_gaps = []
for s in SM:
    for t in TM:
        v = _acc[(s, t)]
        p, o_, tp, to = [x / _n for x in v[:4]]
        _gaps.append(tp - p)
        print(f"{s:>6.2f}{t:>7.2f}{tp - p:>+12.1f}bp{o_ - p:>+11.1f}bp"
              f"{tp - p:>+11.1f} 〜{to - p:>+7.1f}bp")
print(f"\n  真実−悲観の範囲: **{min(_gaps):+.1f} 〜 {max(_gaps):+.1f}bp**")
print(f"  → 日足の悲観仮定は、この分だけ **低く出る**(安全側)。")
if max(_gaps) < 5.0:
    print(f"     ✅ **小さい**。日足の判定はほぼそのまま読んでよい。")
    print(f"        利確なし(tm0)は両方タッチが構造的に0なので **バイアスも0**。")
else:
    print(f"     ⚠ エッジ(§18.53 は +11.8bp)と同じオーダー。"
          f"日足の判定は **合格の向きにしか使えない**")

print(f"\n{'=' * 78}\n■ データの素性\n{'=' * 78}")
print(f"  先頭バーの時刻(= その銘柄が寄った時刻):")
for _k, _v in sorted(_first.items(), key=lambda x: -x[1])[:6]:
    print(f"    {_k}  {_v:>7,} ({_v / _n * 100:>5.1f}%)")
_at9 = _first.get("09:00", 0)
print(f"  → **09:00 に寄っているのは {_at9 / _n * 100:.1f}%**。"
      f"残りは特別気配などで遅れて寄っている")
print(f"     ⚠ 09:00 に板を見て発注する方式は、この分を取りこぼす"
      f"(§18.44 は watch50 で 2% だった = 流動性で絞れば減る)")
print(f"\n  1日の値幅が {a.wide_pct:.0f}%超(§18.27 の分割汚染の疑い): "
      f"{len(_wide):,}件 ({len(_wide) / _n * 100:.2f}%)")
for w in sorted(_wide, key=lambda x: -x[5])[:5]:
    print(f"    {w[0]} {w[1]}  始値{w[2]:,.0f} 高{w[3]:,.0f} "
          f"安{w[4]:,.0f}  値幅{w[5]:.0f}%")
print(f"  ⚠ これは代用の検査。本物の分割ガード(intraday_integrity)は"
      f"**日足の終値と突き合わせる**ので、日足が要る")
