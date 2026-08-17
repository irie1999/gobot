r"""show_k_oco.py — その朝の K の合格銘柄について、約定値・損切り・利確を出す

⛔ 照会のみ。CSV を2つ読むだけで、ネットワークも kabu も使わない。

■ なぜ要るか (2026-08-17)

`k_paper_<日付>.csv` は 09:00 の **始値**を持っているが ATR を持たない。
ATR は `k_signals_<日付>.csv`(--collect の出力)にある。
K の OCO は **実約定価格(=始値)** を基準に置くので、両方を突き合わせないと
損切り・利確が出ない。

    約定値 = 始値(板寄せで確定)
    損切り = 始値 + sm × ATR   ← ショートなので **上**
    利確   = 始値 − tm × ATR   ← **下**

⚠ シグナル時点の stop_price / target_price は **逆指値トリガー(前日終値基準)**
  で計算されたもので、K の OCO とは**別物**。混同しないこと(§18.32)。

■ 使い方

    python show_k_oco.py                      # 今日のぶん
    python show_k_oco.py --date 20260817
    python show_k_oco.py --all                # 不合格も含めて全部
    python show_k_oco.py --sm 0.5 --tm 1.0    # 決済パラメータ(既定はKの設定)

★ 2026-08-17 以降に取った k_paper には atr / stop_k / target_k が
  直接入っているので、そのときは k_open_confirm の出力にそのまま出る。
  本ツールは **それ以前のCSV** と、後から別パラメータで見たいとき用。
"""
from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
from pathlib import Path

ap = argparse.ArgumentParser(
    description="K の合格銘柄の 約定値/損切り/利確 を出す(照会のみ)")
ap.add_argument("--date", type=str, default="",
                help="yyyyMMdd。既定は今日")
ap.add_argument("--paper", type=str, default="", help="k_paper_<日付>.csv")
ap.add_argument("--signals", type=str, default="", help="k_signals_<日付>.csv")
ap.add_argument("--sm", type=float, default=0.5, help="損切ATR倍率(K の設定)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(K の設定)")
ap.add_argument("--all", action="store_true", help="不合格も含めて全部出す")
ap.add_argument("--j-only", action="store_true", help="J(選定あり)だけ")
args = ap.parse_args()

_d = args.date or f"{_dt.date.today():%Y%m%d}"
_paper = Path(args.paper or f"k_paper_{_d}.csv")
_sigs = Path(args.signals or f"k_signals_{_d}.csv")
for _p in (_paper, _sigs):
    if not _p.exists():
        raise SystemExit(f"[error] {_p} がありません")


def _f(r: dict, k: str) -> float:
    try:
        return float(r.get(k) or 0)
    except Exception:
        return 0.0


# ── ATR と戦略名を銘柄ごとに拾う ───────────────────────────────────────
_atr: dict = {}
_strats: dict = {}
_names: dict = {}
for r in _csv.DictReader(open(_sigs, encoding="utf-8-sig")):
    _s = str(r.get("symbol") or "").upper().removesuffix(".T").split(".")[0]
    if not _s:
        continue
    _a = _f(r, "atr")
    # 同じ銘柄が複数戦略で出る。ATR は銘柄固有なので最初の非ゼロを採る。
    if _a > 0 and not _atr.get(_s):
        _atr[_s] = _a
    _names.setdefault(_s, str(r.get("name") or ""))
    _strats.setdefault(_s, []).append(str(r.get("strategy") or ""))

_rows = list(_csv.DictReader(open(_paper, encoding="utf-8-sig")))
_sel = _rows if args.all else [r for r in _rows if str(r.get("pass_gap")) == "1"]
if args.j_only:
    _sel = [r for r in _sel if str(r.get("in_j")) == "1"]
_sel.sort(key=lambda r: -_f(r, "gap_bp"))

print(f"""
{'=' * 100}
■ K の合格銘柄 — {_paper.name}  (損切 = 始値 + {args.sm}ATR / 利確 = 始値 − {args.tm}ATR)
{'=' * 100}""")
print(f"  {'銘柄':<7}{'J':>2}{'名称':<14}{'戦略':<10}{'前日終値':>9}"
      f"{'約定(始値)':>11}{'ATR':>8}{'損切(上)':>10}{'利確(下)':>10}"
      f"{'株数':>7}{'投入額':>11}{'損失上限':>10}{'利益上限':>10}")

_tot_y = _tot_l = _tot_g = 0.0
_miss = []
for r in _sel:
    _s = str(r.get("symbol") or "")
    _op = _f(r, "open_p")
    _a = _atr.get(_s, 0.0)
    _q = int(_f(r, "lots_k")) * 100
    if _a <= 0 or _op <= 0:
        _miss.append(_s)
        continue
    _stop = _op + _a * args.sm
    _tgt = _op - _a * args.tm
    # ショート: 損切りは上(損)、利確は下(益)。数量ぶんの上限額。
    _loss = (_op - _stop) * _q
    _gain = (_op - _tgt) * _q
    _y = _f(r, "yen_k")
    _tot_y += _y
    _tot_l += _loss
    _tot_g += _gain
    _st = "/".join(sorted(set(_strats.get(_s) or [])))[:9]
    print(f"  {_s:<7}{'✓' if str(r.get('in_j')) == '1' else '':>2}"
          f"{_names.get(_s, '')[:13]:<14}{_st:<10}"
          f"{_f(r, 'prev_close'):>9,.1f}{_op:>11,.1f}{_a:>8,.1f}"
          f"{_stop:>10,.1f}{_tgt:>10,.1f}"
          f"{_q:>6,}株{_y:>10,.0f}円{_loss:>+10,.0f}{_gain:>+10,.0f}")

print(f"  {'-' * 98}")
print(f"  {len(_sel) - len(_miss)}件 / 投入 {_tot_y:,.0f}円"
      f" / 全部損切りなら {_tot_l:+,.0f}円"
      f" / 全部利確なら {_tot_g:+,.0f}円")
if _miss:
    print(f"  ⚠ ATR か始値が取れず計算できません: {', '.join(_miss)}")
print(f"""
  ⛔ **発注していません**。その朝 K なら何をどこに置いたかの記録です。
  ⚠ シグナル時点の stop_price / target_price は **逆指値トリガー(前日終値基準)**
     で計算されたもので、上の損切/利確とは **別物**(§18.32)。
  ★ 損失上限/利益上限は「そのラインちょうどで約定したら」の額。実際は
     損切りは成行なので滑る(§18.22 で ±260円/件 と検証済み)。
""")
