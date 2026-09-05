#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板の記録から **成行 vs 指値幅** を再現する（ペーパー・発注しない）。

★ なぜこれが要るのか
  実弾は1日1〜2件しか出ないので、指値幅を実約定だけで決めようとすると
  何ヶ月もかかります。ところが `n_quotes_<日付>.csv` には
  **合格した全銘柄の板10段が毎周**残っているので、

      ・成行100株ならいくらで売れたか      （mkt100 = Buy1 から消化）
      ・指値 0/50/100/300/500bp なら約定したか、いくらで売れたか
      ・09:10 までに最も有利だった価格      （= 約定可能な上限）
      ・約定しなかった銘柄を引けまで持っていたら          （n_close の終値）

  が **毎日タダで、全候補について**計算できます。実弾は「この板モデルが
  実約定と合うか」を校正する少数標本として使えば足ります。

⛔ **これは板モデルであって実約定ではありません。** 板に無いもの
  （注文が届くまでの数百ミリ秒の値動き、板寄せの気配、自分の注文が板を
  動かす効果）は再現できません。実約定との差は `analyze_entry_slip.py`
  で測ります。

⛔ 日々の手順には入れないこと（手順は .\\nexec / .\\fills / --close の3つ）。

使い方:
    python sim_entry_exec.py
    python sim_entry_exec.py --since 2026-09-01
    python sim_entry_exec.py --bps 0,50,100,300,500 --save exec_sim.csv
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob
import os
import re

ap = argparse.ArgumentParser(
    description="板の記録から成行 vs 指値幅を再現する（発注しない）")
ap.add_argument("--since", default="", help="この日以降だけ (YYYY-MM-DD)")
ap.add_argument("--dir", default=".", help="CSV のあるフォルダ")
ap.add_argument("--bps", default="0,50,100,300,500",
                help="試す指値幅。始値からの**下げ幅** bp。カンマ区切り。"
                     "**負なら始値より上**(例 -50 は始値+50bp の指値)。"
                     "⚠ 上に置くと『寄り後に上がった銘柄』でだけ約定する"
                     "逆選択になる(N は下げる銘柄に賭けているので不利のはず)")
ap.add_argument("--qty", type=int, default=100, help="株数(既定100)")
ap.add_argument("--save", default="exec_sim.csv",
                help="銘柄ごとの明細を保存する先（'' で保存しない）")
a = ap.parse_args()

_BPS = [float(x) for x in a.bps.split(",") if x.strip()]


# ⛔ 板の読み方は board_fill に1本化してある。ここにコピーを作らないこと
#   (2本に分かれると必ず片方だけ直って食い違う)。
from board_fill import (code4 as _code4, f as _f, i as _i,  # noqa: E402
                        sell_fill as _sell_fill, true as _true)


def _rows(path: str) -> list:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(_csv.DictReader(f))
    except Exception:
        return []


# ── 日ごとに n_quotes を読む ───────────────────────────────────────
_qd = {}
for p in sorted(glob.glob(os.path.join(a.dir, "n_quotes_2*.csv"))):
    m = re.search(r"n_quotes_(\d{8})\.csv$", os.path.basename(p))
    if m:
        _qd[m.group(1)] = p
if not _qd:
    raise SystemExit(
        "n_quotes_<日付>.csv が見つかりません。\n"
        "  朝の .\\nexec で板の記録が残るのは 2026-09-02 以降です"
        "（計測器の移植 56428ee より後）")

_recs, _skip = [], []
for _ymd in sorted(_qd):
    _iso = f"{_ymd[0:4]}-{_ymd[4:6]}-{_ymd[6:8]}"
    if a.since and _iso < a.since:
        continue

    # 終値（約定しなかった銘柄を引けまで持ったら、を出すのに要る）
    _cl = {}
    for r in _rows(os.path.join(a.dir, f"n_close_{_ymd}.csv")):
        _s = _code4(r.get("symbol") or r.get("code") or "")
        if _s:
            _cl[_s] = _f(r.get("close_p") or r.get("終値") or r.get("close"))

    # 銘柄ごとに全周を集める
    _by = {}
    for r in _rows(_qd[_ymd]):
        _s = _code4(r.get("symbol") or "")
        if _s:
            _by.setdefault(_s, []).append(r)

    for _s, _rs in _by.items():
        # 寄った後の行だけ（始値が入っていて、前日の始値でない）
        _live = [r for r in _rs
                 if _f(r.get("open_p")) > 0 and not _true(r.get("stale_open"))]
        if not _live:
            _skip.append((_iso, _s, "寄り後の行が無い"))
            continue
        if not any(_true(r.get("pass_gap")) for r in _live):
            continue                       # 合格していない銘柄は対象外
        _live.sort(key=lambda r: (_i(r.get("poll")), str(r.get("ts") or "")))
        _first = _live[0]
        _open = _f(_first.get("open_p"))
        if _open <= 0:
            continue

        _rec = {"date": _iso, "code": _s, "open_p": _open,
                "gap_bp": _f(_first.get("gap_bp")),
                "polls": len(_live), "close_p": _cl.get(_s, 0.0)}

        # ① 初回観測時の成行(= 指値0 と同じ計算だが、意味が違うので別に出す)
        _px, _q, _lv = _sell_fill(_first, 0.0, a.qty)
        _rec["mkt_px"] = _px
        _rec["mkt_bp"] = ((_px - _open) / _open * 1e4) if _px else None
        _rec["mkt_lv"] = _lv
        # ⚠ 記録済みの mkt100_px と一致するはず（自己検査）
        _m0 = _f(_first.get("mkt100_px"))
        _rec["mkt_chk"] = (abs(_m0 - (_px or 0)) < 0.01) if (_m0 > 0 and _px) else None

        # ② 指値幅ごと。初回で約定しなければ以降の周も試す（09:10まで）
        for _bp in _BPS:
            _lim = _open * (1 - _bp / 1e4)
            _hit, _hpx, _hpoll = False, None, None
            for r in _live:
                _p, _qq, _l = _sell_fill(r, _lim, a.qty)
                if _p is not None:
                    _hit, _hpx, _hpoll = True, _p, _i(r.get("poll"))
                    break
            _rec[f"lim{int(_bp)}_hit"] = _hit
            _rec[f"lim{int(_bp)}_px"] = _hpx
            _rec[f"lim{int(_bp)}_bp"] = ((_hpx - _open) / _open * 1e4) if _hpx else None
            _rec[f"lim{int(_bp)}_poll"] = _hpoll

        # ③ 09:10 までで最も有利だった約定価格（= 到達可能な上限）
        _best, _bpoll = None, None
        for r in _live:
            _p, _qq, _l = _sell_fill(r, 0.0, a.qty)
            if _p is not None and (_best is None or _p > _best):
                _best, _bpoll = _p, _i(r.get("poll"))
        _rec["best_px"] = _best
        _rec["best_bp"] = ((_best - _open) / _open * 1e4) if _best else None
        _rec["best_poll"] = _bpoll
        _recs.append(_rec)

if not _recs:
    raise SystemExit("合格した銘柄の板が1件も見つかりませんでした")

_days = sorted({r["date"] for r in _recs})

print()
print("=" * 88)
print("■ 板から再現した執行（ペーパー）— **板モデルであって実約定ではありません**")
print("=" * 88)
print(f"  対象 {len(_recs)}件 / {len(_days)}営業日 "
      f"({_days[0]} 〜 {_days[-1]}) / {a.qty}株")

# 自己検査: 記録済み mkt100_px と一致するか
_chk = [r for r in _recs if r["mkt_chk"] is False]
if _chk:
    print(f"  ⛔ **記録済み mkt100_px と食い違う {len(_chk)}件** — "
          f"板の読み方が違います")
    for r in _chk[:3]:
        print(f"       {r['date']} {r['code']}")
else:
    print(f"  ✅ 成行の計算は記録済み mkt100_px と一致（板の読み方は同じ）")

print()
print(f"{'日付':<12}{'銘柄':>6}{'ギャップ':>8}{'始値':>10}{'成行':>10}"
      f"{'成行bp':>8}{'段':>3}{'最良bp':>8}{'周':>4}")
print("-" * 88)
for r in sorted(_recs, key=lambda x: (x["date"], x["code"])):
    _mp = f"{r['mkt_px']:,.1f}" if r["mkt_px"] else "—"
    _mb = f"{r['mkt_bp']:+.1f}" if r["mkt_bp"] is not None else "—"
    _bb = f"{r['best_bp']:+.1f}" if r["best_bp"] is not None else "—"
    print(f"{r['date']:<12}{r['code']:>6}{r['gap_bp']:>+8.0f}"
          f"{r['open_p']:>10,.1f}{_mp:>10}{_mb:>8}{r['mkt_lv']:>3}"
          f"{_bb:>8}{r['best_poll'] if r['best_poll'] else '—':>4}")

# ── ★ 指値幅ごとの比較 ────────────────────────────────────────
print()
print("■ ★ 指値幅ごと（始値を基準に、下が有利・上が不利）")
print(f"{'幅':>9}{'約定':>5}{'約定率':>7}{'約定bp':>8}"
      f"{'実装差bp':>9}{'★実損益/日':>11}{'★合計':>11}")
print("-" * 62)


def _line(lbl: str, hits: list, miss: list, yen: list) -> None:
    """約定したぶんの滑り・実装差・**実際に取れた損益**を出す。

    ⛔ 実装差だけで選ぶと『1件も建てない幅』が最良になる。
       「建てなかったので損しなかった」と「儲かった」を区別できないため。
       ★実損益 は約定した件の (売値 − 終値) × 株数 なので、
       建てなければ 0 になり、この罠にかからない。**判定はこちらで行う。**
    """
    _n = len(hits) + len(miss)
    _hb = sum(hits) / len(hits) if hits else None
    _impl = ((sum(hits) - sum(miss)) / _n) if _n else None
    _tot = sum(yen)
    print(f"{lbl:>9}{len(hits):>5}{len(hits) / _n * 100 if _n else 0:>6.0f}%"
          f"{(f'{_hb:+.1f}' if _hb is not None else '—'):>8}"
          f"{(f'{_impl:+.1f}' if _impl is not None else '—'):>9}"
          f"{_tot / max(len(_days), 1):>+11,.0f}{_tot:>+11,.0f}")


def _yen_of(px, close_p) -> float:
    """ショート: 売値 − 終値。約定しなければ 0（建てていないので）。"""
    return (px - close_p) * a.qty if (px and close_p > 0) else 0.0


# 成行
_h = [r["mkt_bp"] for r in _recs if r["mkt_bp"] is not None]
_m = [((r["open_p"] - r["close_p"]) / r["open_p"] * 1e4)
      for r in _recs if r["mkt_bp"] is None and r["close_p"] > 0]
_y = [_yen_of(r["mkt_px"], r["close_p"]) for r in _recs]
_line("成行", _h, _m, _y)
for _bp in _BPS:
    _k = int(_bp)
    _h = [r[f"lim{_k}_bp"] for r in _recs if r.get(f"lim{_k}_hit")]
    _m = [((r["open_p"] - r["close_p"]) / r["open_p"] * 1e4)
          for r in _recs if not r.get(f"lim{_k}_hit") and r["close_p"] > 0]
    # 始値を基準に、下に置いたら「始値-Nbp」、上なら「始値+Nbp」
    _y = [_yen_of(r.get(f"lim{_k}_px"), r["close_p"]) for r in _recs]
    _line(("始値" if _k == 0 else
           (f"始値-{_k}" if _k > 0 else f"始値+{-_k}")), _h, _m, _y)

print()
print("  ⛔ **判定は『★実損益/日』で行うこと。実装差bp では選ばない。**")
print("     実装差は『建てなかったので損しなかった』と『儲かった』を区別できず、")
print("     **1件も約定しない幅が最良に見えます**（実際にそう出ます）")
print("  ⚠ 約定bp だけ見るのも誤り。『約定しにくい幅ほど良い』と読めてしまう")
print("     （下げた銘柄＝勝つはずだった銘柄が落ちるので）")
print("  ⚠ ★実損益 = 約定した件の (売値 − 終値) × 株数。建てなければ 0。")
print("     ⛔ これは **slip=0・手数料0** の理論値で、決済側の滑りも入っていません")

# ── 保存 ────────────────────────────────────────────────────
if a.save:
    _cols = ["date", "code", "open_p", "close_p", "gap_bp", "polls",
             "mkt_px", "mkt_bp", "mkt_lv", "mkt_chk",
             "best_px", "best_bp", "best_poll"] + [
        f"lim{int(b)}_{k}" for b in _BPS for k in ("hit", "px", "bp", "poll")]
    try:
        with open(a.save, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=_cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(_recs, key=lambda x: (x["date"], x["code"])))
        print()
        print(f"  → {a.save}（{len(_recs)}行）")
    except Exception as e:
        print(f"  ⚠ 保存に失敗: {e}")

if _skip:
    print()
    print(f"  ⚠ 板が使えなかった {len(_skip)}件: "
          f"{', '.join(f'{d} {c}({w})' for d, c, w in _skip[:5])}")
print()
