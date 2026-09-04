#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**寄りで即発注 vs N秒待ってから発注** を板の記録から再現する（発注しない）。

★ 問い (2026-09-03 ユーザー発案)
  「逆に10秒くらい待って、落ちてきた銘柄だけ約定するようにするとかは？」

  N はギャップアップを売るので、寄り直後に**さらに上がる**銘柄は負け筋。
  少し待って「下げ始めた銘柄だけ」建てれば、その負け筋を避けられるのでは？

★ 3段に分解する（合意済みの設計）。混ぜると何が効いたか分からなくなる。

      待機コスト = 待ち全件 − 即時全件      … 待つと約定が不利になるぶん
      選別効果   = 待ち選別 − 待ち全件      … 下げた銘柄だけ選ぶぶん
      最終効果   = 待ち選別 − 即時全件      … 差し引きで得か損か

  「待つ」こと自体はコストです（§18.44 の実測で 1分 −15.8bp / 3分 −29.4bp）。
  選別効果がそれを上回って初めて採用に値します。

★ 先読みはありません。判定に使う価格と執行する価格は**同じ行の同じ板**です。
  「N秒後の板で成行を打ったらいくらか」を見て、それが条件を満たせば打つ。

⛔ **これは板モデルであって実約定ではありません。** 注文が届くまでの値動き、
  板寄せの気配、自分の注文が板を動かす効果は再現できません。

⛔⛔ **実現した待ち時間を必ず見ること。** 板読みは50銘柄で6〜30秒かかるので、
  「10秒待ち」と指定しても実際は30秒後の板かもしれません。名目ではなく
  実測の分布で読んでください（表Aに出します）。

⛔ 日々の手順には入れないこと（手順は .\\nexec / .\\fills / --close の3つ）。

⚠ `sim_entry_exec.py` と**母集団が違って当然**です。こちらは待ち時間の起点に
  `open_time` が要るので、それが読めない銘柄を落とします。件数を横に並べて
  比べないでください（落とした理由は毎回 全部 表示します）。

使い方:
    python sim_entry_wait.py
    python sim_entry_wait.py --since 2026-09-02
    python sim_entry_wait.py --waits 10,20,30,40,60 --drops 0,10,20,30,50
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob
import os
import random as _rnd
import re
import statistics as _st

from board_fill import code4 as _code4, f as _f, secs as _secs, \
    sell_fill as _sell_fill, true as _true

ap = argparse.ArgumentParser(
    description="寄りで即発注 vs N秒待ってから発注（板モデル・発注しない）")
ap.add_argument("--since", default="", help="この日以降だけ (YYYY-MM-DD)")
ap.add_argument("--dir", default=".", help="CSV のあるフォルダ")
ap.add_argument("--waits", default="10,20,30,40,60",
                help="待ち秒数。カンマ区切り")
ap.add_argument("--drops", default="0,10,20,30,50",
                help="『始値からこれだけ下げていたら売る』bp。0 = 始値以下なら売る")
ap.add_argument("--main-wait", type=float, default=10.0,
                help="★主判定の待ち秒数（既定10）。感度表の最良セルは採用しない")
ap.add_argument("--main-drop", type=float, default=0.0,
                help="★主判定の下落bp（既定0 = 始値以下）")
ap.add_argument("--qty", type=int, default=100, help="株数(既定100)")
ap.add_argument("--budget", type=float, default=200.0,
                help="★ 予算(万円)。既定200 = 実運用と同じ。"
                     "0 で予算制約なし(合格を全部建てる診断)。"
                     "⛔ 制約なしの表は『全部買えるなら得か』であって"
                     "『予算内でどれを買うか』ではない(§18.10)")
ap.add_argument("--nulls", type=int, default=200,
                help="★ 選別効果の帰無較正の本数。**同じ日・同じ件数を"
                     "ランダムに選ぶ**ことで『件数が減った効果』と"
                     "『条件そのものの効果』を分ける(既定200)")
ap.add_argument("--min-days", type=int, default=10,
                help="これ未満の営業日数なら『判定しない』と明示する(既定10)")
ap.add_argument("--save", default="wait_sim.csv",
                help="銘柄ごとの明細を保存する先（'' で保存しない）")
a = ap.parse_args()

_WAITS = [float(x) for x in a.waits.split(",") if x.strip()]
_DROPS = [float(x) for x in a.drops.split(",") if x.strip()]
if a.main_wait not in _WAITS:
    _WAITS.append(a.main_wait)
    _WAITS.sort()
if a.main_drop not in _DROPS:
    _DROPS.append(a.main_drop)
    _DROPS.sort()


def _rows(path: str) -> list:
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(_csv.DictReader(fh))
    except Exception:
        return []


def _hms(sec: float) -> str:
    _s = int(sec)
    return f"{_s // 3600:02d}:{_s % 3600 // 60:02d}:{_s % 60:02d}"


# ── 日ごとに n_quotes を読む ───────────────────────────────────────
_qd = {}
for p in sorted(glob.glob(os.path.join(a.dir, "n_quotes_2*.csv"))):
    m = re.search(r"n_quotes_(\d{8})\.csv$", os.path.basename(p))
    if m:
        _qd[m.group(1)] = p
if not _qd:
    raise SystemExit(
        "n_quotes_<日付>.csv が見つかりません。\n"
        "  朝の .\\nexec で板の記録が残るのは 2026-09-02 以降です")

_recs: list = []
_drop: list = []          # (日付, 銘柄, 落とした理由) — 保存則の検査に使う
# ★ 板を1周読むのにかかった秒数。(日付, 周) -> [最初の req_ts, 最後の resp_ts]
#   これが数秒なら「読み取り」は律速ではない = 遅れは判定〜発注の側にある。
#   §18.44 の実測: 場外ウォーム 6.3秒 / 本番 09:00 は 36.5秒(中央)〜95.9秒
_pollspan: dict = {}
_pollcnt: dict = {}


def _yen(px, close_p) -> float:
    """ショート: 売値 − 終値。約定しなければ 0（建てていないので）。"""
    return (px - close_p) * a.qty if (px and close_p > 0) else 0.0


for _ymd in sorted(_qd):
    _iso = f"{_ymd[0:4]}-{_ymd[4:6]}-{_ymd[6:8]}"
    if a.since and _iso < a.since:
        continue

    _cl = {}
    for r in _rows(os.path.join(a.dir, f"n_close_{_ymd}.csv")):
        _s = _code4(r.get("symbol") or r.get("code") or "")
        if _s:
            _cl[_s] = _f(r.get("close_p") or r.get("終値") or r.get("close"))

    _by: dict = {}
    for r in _rows(_qd[_ymd]):
        _s = _code4(r.get("symbol") or "")
        if _s:
            _by.setdefault(_s, []).append(r)
        # 1周の所要時間。**合格銘柄だけでなく読んだ全銘柄**で測る
        # (実運用で待たされるのは全体を読み切るまでなので)
        _pk = str(r.get("poll") or "").strip()
        _rq, _rp = _secs(r.get("req_ts")), _secs(r.get("resp_ts"))
        if _pk and _rq >= 0 and _rp >= 0:
            _key = (_iso, _pk)
            _sp = _pollspan.setdefault(_key, [_rq, _rp])
            _sp[0], _sp[1] = min(_sp[0], _rq), max(_sp[1], _rp)
            _pollcnt[_key] = _pollcnt.get(_key, 0) + 1

    for _s, _rs in _by.items():
        # 寄った後の行だけ（始値が入っていて、前日の始値でない）
        _live = [r for r in _rs
                 if _f(r.get("open_p")) > 0 and not _true(r.get("stale_open"))]
        if not _live:
            _drop.append((_iso, _s, "寄り後の行が無い"))
            continue
        _open = _f(_live[0].get("open_p"))
        if _open <= 0:
            _drop.append((_iso, _s, "始値が取れない"))
            continue
        # ⛔ 並べ替えは **resp_ts**（板が返ってきた時刻）で行う。poll 番号は
        #   50銘柄まとめて1つなので、銘柄ごとの時系列にはならない。
        _t: list = []
        for r in _live:
            _rt = _secs(r.get("resp_ts") or r.get("ts"))
            if _rt >= 0:
                _t.append((_rt, r))
        if not _t:
            _drop.append((_iso, _s, "resp_ts が読めない"))
            continue
        _t.sort(key=lambda x: x[0])
        _t0 = _secs(_live[0].get("open_time"))
        if _t0 < 0:
            _drop.append((_iso, _s, "open_time が読めない"))
            continue
        # 場外の時刻は捨てる（前日の値などが紛れ込むため）
        if not (9 * 3600 <= _t0 <= 15 * 3600 + 30 * 60):
            _drop.append((_iso, _s, f"open_time が場外 ({_live[0].get('open_time')})"))
            continue

        _rec = {"date": _iso, "code": _s, "open_p": _open,
                "close_p": _cl.get(_s, 0.0),
                # 発注順は **寄ったグループ順 → |ギャップ|降順**(ライブと同じ)。
                # そのために寄り時刻を秒で持つ。
                "open_s": _t0, "last_s": _t[-1][0],
                "gap_bp": _f(_live[0].get("gap_bp")),
                "pass_gap": 1 if any(_true(r.get("pass_gap")) for r in _live) else 0,
                "polls": len(_t)}

        # ① 即時 = 最初に観測できた板。**これも0秒ではない**ので遅れを記録する
        _rt0, _r0 = _t[0]
        _px0, _q0, _lv0 = _sell_fill(_r0, 0.0, a.qty)
        _rec["imm_lag_s"] = round(_rt0 - _t0, 2)
        _rec["imm_px"] = _px0
        _rec["imm_bp"] = ((_px0 - _open) / _open * 1e4) if _px0 else None
        _rec["imm_yen"] = _yen(_px0, _rec["close_p"])

        # ② 待ちごとの判定行 = resp_ts >= open_time + 待ち秒 を満たす最初の行
        for _w in _WAITS:
            _k = int(_w)
            _hit = next((x for x in _t if x[0] >= _t0 + _w), None)
            if _hit is None:
                _rec[f"w{_k}_lag_s"] = None
                _rec[f"w{_k}_px"] = None
                _rec[f"w{_k}_bp"] = None
                _rec[f"w{_k}_yen"] = None
                continue
            _rt, _r = _hit
            _px, _qq, _lv = _sell_fill(_r, 0.0, a.qty)
            _rec[f"w{_k}_lag_s"] = round(_rt - _t0, 2)
            _rec[f"w{_k}_px"] = _px
            _rec[f"w{_k}_bp"] = ((_px - _open) / _open * 1e4) if _px else None
            _rec[f"w{_k}_yen"] = _yen(_px, _rec["close_p"])
        _recs.append(_rec)

if not _recs:
    raise SystemExit("板から判定できる銘柄が1件もありませんでした")

_days = sorted({r["date"] for r in _recs})
_N = [r for r in _recs if r["pass_gap"] == 1]
_C = [r for r in _recs if r["pass_gap"] == 0]

print()
print("=" * 84)
print("■ 即時 vs N秒待ち（板モデル）— **板であって実約定ではありません**")
print("=" * 84)
print(f"  対象 {len(_recs)}件 / {len(_days)}営業日 "
      f"({_days[0]} 〜 {_days[-1]}) / {a.qty}株")
print(f"  N(合格 pass_gap=1) {len(_N)}件 / 対照群(pass_gap=0) {len(_C)}件")
if _drop:
    print(f"  落とした {len(_drop)}件: "
          + ", ".join(f"{c}({w})" for _, c, w in _drop[:4])
          + (" …" if len(_drop) > 4 else ""))
print(f"  ✅ 保存則: 対象 {len(_recs)} + 落とし {len(_drop)} = "
      f"{len(_recs) + len(_drop)}件")

# ── 表A: 実現した待ち時間（**名目を信じないための表**）────────────
print()
print("■ 表A ★ 実現した待ち時間 — **名目ではなくこちらで読むこと**")
print("     板読みは50銘柄で6〜30秒かかるので、指定より遅い板になります")
print(f"{'指定':>7}{'判定できた':>11}{'実現の中央':>11}{'最小':>8}{'p90':>8}"
      f"{'最大':>8}{'超過(中央)':>11}")
print("-" * 66)


def _lagline(lbl: str, key: str, nominal: float, pool: list) -> None:
    _v = [r[key] for r in pool if r.get(key) is not None]
    if not _v:
        print(f"{lbl:>7}{0:>11}{'—':>11}{'—':>8}{'—':>8}{'—':>8}{'—':>11}")
        return
    _v.sort()
    _med = _st.median(_v)
    _p90 = _v[min(len(_v) - 1, int(len(_v) * 0.9))]
    print(f"{lbl:>7}{len(_v):>11}{_med:>10.1f}s{_v[0]:>7.1f}s{_p90:>7.1f}s"
          f"{_v[-1]:>7.1f}s{_med - nominal:>+10.1f}s")


_lagline("即時", "imm_lag_s", 0.0, _recs)
for _w in _WAITS:
    _lagline(f"{int(_w)}秒", f"w{int(_w)}_lag_s", _w, _recs)

# ── 表A-2: 板を1周読むのにかかった秒数 ────────────────────────────
print()
print("■ 表A-2 ★ 板を1周読むのにかかった秒数（読んだ全銘柄で測る）")
print("     数秒なら『読み取り』は律速ではない = 遅れは判定〜発注の側にあります")
print(f"{'日付':<12}{'周':>4}{'銘柄/周':>8}{'1周 中央':>10}{'最小':>8}"
      f"{'最大':>8}{'初周':>8}")
print("-" * 58)
for _d in sorted({k[0] for k in _pollspan}):
    _sp = [(int(_p) if str(_p).isdigit() else 0, _pollspan[(_d, _p)][1] - _pollspan[(_d, _p)][0])
           for (_dd, _p) in _pollspan if _dd == _d]
    _sp.sort()
    _v = sorted(s for _, s in _sp)
    _cn = [_pollcnt[k] for k in _pollcnt if k[0] == _d]
    _first = _sp[0][1] if _sp else 0.0
    print(f"{_d:<12}{len(_v):>4}{int(_st.median(_cn)) if _cn else 0:>8}"
          f"{_st.median(_v):>9.1f}s{_v[0]:>7.1f}s{_v[-1]:>7.1f}s{_first:>7.1f}s")
print("  ⚠ 『初周』が 09:00 の1周目。**ここが実運用で効く数字**です")
print("     §18.44 の実測: 場外ウォーム 6.3秒 / 本番09:00 は 36.5秒(中央)〜95.9秒")

_immlag = [r["imm_lag_s"] for r in _recs if r.get("imm_lag_s") is not None]
if _immlag:
    _im = _st.median(_immlag)
    if _im > 5:
        print()
        print(f"  ⛔ **『即時』の時点で既に中央 {_im:.0f}秒 遅れています。**")
        print(f"     つまりこの比較の基準は『0秒』ではありません。"
              f"待機コストは**過小**に出ます")


# ── 3段分解 ──────────────────────────────────────────────────
_BUDGET = a.budget * 1e4


def _fill(rows: list, key: str) -> tuple:
    """予算の許す範囲で建てる。返り値 (損益, 建玉, 建てた件数)。

    ⛔ **落ちた銘柄の枠は後続に回る**(飛ばして次を試す)。これを再現しないと
      「合格を全部建てられたら」の診断にしかならない(§18.10)。
    ★ 順番はライブと同じ **寄ったグループ順 → |ギャップ|降順**。
    """
    _cash = _BUDGET if _BUDGET > 0 else float("inf")
    # ⛔ ローカル名を _yen にしない。同名の関数を隠して NameError になる
    _sy, _not, _n = 0.0, 0.0, 0
    for r in sorted(rows, key=lambda x: (x["open_s"], -abs(x["gap_bp"]))):
        if r.get(key) is None:
            continue                       # その腕では建てない銘柄
        _need = r["open_p"] * a.qty
        if _need > _cash:
            continue                       # 枠が足りない → 次の候補を試す
        _cash -= _need
        _sy += _yen(r[key], r["close_p"])
        _not += _need
        _n += 1
    return _sy, _not, _n


def _bp(yen: float, notional: float) -> float:
    """資金加重bp。円だけだと値がさ株の日が重くなり期間比較できない。"""
    return (yen / notional * 1e4) if notional > 0 else 0.0


def _decompose(pool: list, tag: str) -> list:
    """待ち × 下落閾値 の3段分解。対応（同じ銘柄集合）・予算制約つき。"""
    print()
    print("=" * 96)
    print(f"■ {tag}（{len(pool)}件）")
    print("=" * 96)
    if not pool:
        print("  対象なし")
        return []
    _byday: dict = {}
    for r in pool:
        _byday.setdefault(r["date"], []).append(r)
    _out = []
    for _w in _WAITS:
        _k = int(_w)
        # ⛔ 判定行が無い銘柄は **全ての腕から外す**（対応をとる）。
        #   0円扱いにすると「ログが途切れた」ことを「建てなかった」と
        #   混同し、待ちが有利に出る。
        _pd = {d: [r for r in rs if r.get(f"w{_k}_yen") is not None]
               for d, rs in _byday.items()}
        _pd = {d: rs for d, rs in _pd.items() if rs}
        if not _pd:
            continue
        _n = sum(len(v) for v in _pd.values())
        _iy = _in = 0.0
        _ay = _an = 0.0
        for _rs in _pd.values():
            _y, _nt, _ = _fill(_rs, "imm_px")
            _iy += _y
            _in += _nt
            _y, _nt, _ = _fill(_rs, f"w{_k}_px")
            _ay += _y
            _an += _nt
        for _d in _DROPS:
            # 選別: その板で成行を打った値が 始値から _d bp 以上 下げている
            _sy = _sn = 0.0
            _ns = 0
            _selcnt: dict = {}
            for _dt, _rs in _pd.items():
                _sel = [r for r in _rs if r[f"w{_k}_bp"] is not None
                        and r[f"w{_k}_bp"] <= -_d]
                _selcnt[_dt] = len(_sel)
                _y, _nt, _c = _fill(_sel, f"w{_k}_px")
                _sy += _y
                _sn += _nt
                _ns += _c
            # ★★ 帰無較正: **同じ日・同じ件数**をランダムに選ぶ。
            #   これで「12/14件に減った効果」と「10秒条件そのものの効果」を
            #   分ける。帯の中なら条件に意味は無い(件数を減らしただけ)。
            _band = []
            for _s in range(a.nulls):
                _g = _rnd.Random(_s)
                _t = 0.0
                for _dt, _rs in _pd.items():
                    _c = _selcnt.get(_dt, 0)
                    if _c <= 0:
                        continue
                    _pk = _g.sample(_rs, min(_c, len(_rs)))
                    _t += _fill(_pk, f"w{_k}_px")[0]
                _band.append(_t - _ay)
            _bm = _st.mean(_band) if _band else 0.0
            _bs = _st.pstdev(_band) if len(_band) > 1 else 0.0
            _pick = _sy - _ay
            _out.append({
                "tag": tag, "wait": _k, "drop": int(_d), "n": _n, "n_sel": _ns,
                "imm": _iy, "all": _ay, "sel": _sy,
                "imm_n": _in, "all_n": _an, "sel_n": _sn,
                "cost": _ay - _iy, "pick": _pick, "net": _sy - _iy,
                "z": ((_pick - _bm) / _bs) if _bs > 0 else 0.0,
                "band_lo": min(_band) if _band else 0.0,
                "band_hi": max(_band) if _band else 0.0})
    _nd = max(len(_days), 1)
    print(f"{'待ち':>6}{'下落':>6}{'建てた':>7}{'/母数':>6}"
          f"{'待機コスト':>11}{'選別効果':>11}{'帯のz':>8}"
          f"{'★最終効果':>11}{'★bp':>8}{'/日':>9}")
    print("-" * 88)
    for x in _out:
        _star = " ★" if (x["wait"] == int(a.main_wait)
                         and x["drop"] == int(a.main_drop)) else "  "
        # bp の分母は **即時全件の建玉** で統一する。そうしないと
        # 3段が足し算にならない(腕ごとに分母が違ってしまう)。
        print(f"{x['wait']:>5}s{x['drop']:>5}b{x['n_sel']:>7}{x['n']:>6}"
              f"{x['cost']:>+11,.0f}{x['pick']:>+11,.0f}{x['z']:>+8.2f}"
              f"{x['net']:>+11,.0f}{_bp(x['net'], x['imm_n']):>+8.1f}"
              f"{x['net'] / _nd:>+9,.0f}{_star}")
    print(f"  ⚠ 『帯のz』= 選別効果が **同日・同数ランダム除外**の帯から"
          f"どれだけ外れているか。|z|<2 なら『件数を減らしただけ』")
    if _BUDGET > 0:
        print(f"  ⚠ 予算 {a.budget:,.0f}万円で制約済み。"
              f"落ちた銘柄の枠は後続に回しています")
    return _out


# ── 表A-3: 判定できなかった件（除外に偏りがないか）─────────────────
#   ⛔ 母数16件が14件になった、その2件が「遅寄り」など特定の性質を持つなら、
#     除外そのものに偏りが出る。黙って減らさず、必ず中身を出す。
print()
print("■ 表A-3 ★ 判定できなかった件（除外に偏りがないか）")
print(f"{'待ち':>6}{'N母数':>7}{'判定可':>7}{'欠け':>6}"
      f"{'うち遅寄り':>11}{'欠けた銘柄(寄り / 最終観測)':>28}")
print("-" * 74)
for _w in _WAITS:
    _k = int(_w)
    _miss = [r for r in _N if r.get(f"w{_k}_yen") is None]
    _late = [r for r in _miss if r["open_s"] > 9 * 3600 + 60]
    _txt = ", ".join(
        f"{r['code']}({_hms(r['open_s'])[:5]}/{_hms(r['last_s'])[:5]})"
        for r in _miss[:3]) + (" …" if len(_miss) > 3 else "")
    print(f"{_k:>5}s{len(_N):>7}{len(_N) - len(_miss):>7}{len(_miss):>6}"
          f"{len(_late):>11}  {_txt}")
if any(r["open_s"] > 9 * 3600 + 60 for r in _N):
    _nl = sum(1 for r in _N if r["open_s"] > 9 * 3600 + 60)
    print(f"  ⚠ N の {_nl}/{len(_N)}件 が **遅寄り**(09:01以降)。"
          f"欠けた件がここに偏るなら、待ちの評価は"
          f"『早く寄った銘柄だけ』のものになります")

_res = _decompose(_N, "★ N（合格 pass_gap=1）— これが本番")
_resc = _decompose(_C, "対照群（pass_gap=0）— 合格していない銘柄。"
                       "ここでも同じ形が出るなら、効いているのは"
                       "『ギャップの条件』ではなく『待つこと』そのもの")

# ── 主判定 ────────────────────────────────────────────────────
_main = next((x for x in _res
              if x["wait"] == int(a.main_wait) and x["drop"] == int(a.main_drop)),
             None)
print()
print("=" * 84)
print(f"■ ★ 主判定 — {int(a.main_wait)}秒待ち / 始値から {int(a.main_drop)}bp 以上 下げていたら売る")
print("=" * 84)
if _main is None:
    print("  判定できませんでした（該当する行がありません）")
else:
    _nd = max(len(_days), 1)
    _dn = _main["imm_n"]                       # bp の共通分母 = 即時全件の建玉
    print(f"  母数 {_main['n']}件 → 建てた {_main['n_sel']}件 "
          f"({_main['n_sel'] / _main['n'] * 100:.0f}%) / "
          f"予算 {a.budget:,.0f}万円 / 投入(即時) {_dn:,.0f}円")
    print(f"    即時 全件        {_main['imm']:>+12,.0f}円 "
          f"({_bp(_main['imm'], _main['imm_n']):+.1f}bp)")
    print(f"    {int(a.main_wait)}秒待ち 全件    {_main['all']:>+12,.0f}円 "
          f"({_bp(_main['all'], _main['all_n']):+.1f}bp)  "
          f"→ 待機コスト {_main['cost']:>+10,.0f}円 "
          f"({_bp(_main['cost'], _dn):+.1f}bp)")
    print(f"    {int(a.main_wait)}秒待ち 選別    {_main['sel']:>+12,.0f}円 "
          f"({_bp(_main['sel'], _main['sel_n']):+.1f}bp)  "
          f"→ 選別効果   {_main['pick']:>+10,.0f}円 "
          f"({_bp(_main['pick'], _dn):+.1f}bp)")
    print(f"    {'':>16}{'':>19}  **最終効果 {_main['net']:>+10,.0f}円 "
          f"({_bp(_main['net'], _dn):+.1f}bp / {_main['net'] / _nd:+,.0f}円/日)**")
    print()
    print(f"  ★ 選別効果の帰無較正（同日・同数ランダム除外 {a.nulls}本）")
    print(f"      実測 {_main['pick']:>+10,.0f}円   "
          f"帯 {_main['band_lo']:>+10,.0f} 〜 {_main['band_hi']:>+10,.0f}   "
          f"**z = {_main['z']:+.2f}**")
    if abs(_main["z"]) < 2.0:
        print(f"      → **帯の中**。『10秒で下げた銘柄を選んだ』効果は無く、"
              f"件数が減っただけと区別できません")
    else:
        print(f"      → 帯の外。件数減では説明できない効果があります")

# ── 判定の作法 ────────────────────────────────────────────────
print()
if len(_days) < a.min_days:
    print(f"⛔⛔ **{len(_days)}営業日しかありません（{a.min_days}日未満）。"
          f"この数字で採否を決めないこと。**")
    print(f"     選別は母集団を割るので片側が数件になります。"
          f"始値ちょうどの指値は3件で有利に見え、16件で符号が反転しました。")
    print(f"     いまは『実装して貯める』段階です。")
else:
    print(f"  {len(_days)}営業日 / N {len(_N)}件。判定するなら以下を全部満たすこと:")
    print(f"    ・主判定セル（{int(a.main_wait)}秒 × {int(a.main_drop)}bp）で最終効果がプラス")
    print(f"    ・感度表が滑らか（孤立したセルだけ良いのは当てはめ）")
    print(f"    ・**対照群で同じ形が出ていない**（出るなら効いているのは待つこと自体）")
print()
print("  ⛔ 感度表の**最良セルを採用しないこと**。"
      f"{len(_WAITS)}×{len(_DROPS)}={len(_WAITS) * len(_DROPS)}通り試せば"
      f"どれかは良く出ます")
print("  ⚠ 待機コストは**過小**に出ます（表A のとおり『即時』が既に遅れているため）")
print("  ⚠ slip=0・手数料0 の理論値。決済側の滑りも入っていません")

# ── 保存 ────────────────────────────────────────────────────
if a.save:
    _cols = (["date", "code", "pass_gap", "open_p", "close_p", "gap_bp",
              "polls", "imm_lag_s", "imm_px", "imm_bp", "imm_yen"]
             + [f"w{int(w)}_{k}" for w in _WAITS
                for k in ("lag_s", "px", "bp", "yen")])
    try:
        with open(a.save, "w", newline="", encoding="utf-8-sig") as fh:
            w = _csv.DictWriter(fh, fieldnames=_cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(_recs, key=lambda x: (x["date"], x["code"])))
        print()
        print(f"  → {a.save}（{len(_recs)}行）")
    except Exception as e:
        print(f"  ⚠ 保存に失敗: {e}")
print()
