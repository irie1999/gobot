#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N の**エントリー滑り**を、寄り時刻で分けて見る（1回だけ走らせる調査用）。

⛔ **日々の手順には入れないこと**（手順は .\\nexec / .\\fills / n_paper --close の3つだけ）。
   これは「30件が何を測っているか」を理解するための1回きりの道具です。

★ 何を見るのか (2026-09-03 の実測から出た仮説)
    7384  09:00:50 に寄る →   0.0bp（**始値ちょうど**で約定）
    7389  09:00:00 に寄る → -12.1bp
  §18.44 で「N の +100bp 帯は 09:00:00 に寄るので板寄せに参加できない」と
  構造として整理しました。その裏返しで、**遅く寄る銘柄なら発注が間に合う**
  のではないか、という仮説です。

⚠ **判定はしません。** 17件前後では平均は決まりません(§18.24 の作法)。
  見るのは「寄り時刻で分かれるか」という**構造**だけ。分かれていれば
  30件そろった後に発注ルールを考える材料になり、分かれていなければ
  「滑りは銘柄ごとの振れ」ということが分かります。

読むもの (どちらも既存。何も発注しません):
    orders_<yyyyMMdd>.csv   .\\fills が出す全注文一覧（実約定値）
    k_paper_<yyyyMMdd>.csv  朝の板読み（始値・寄り時刻）

使い方:
    python analyze_entry_slip.py
    python analyze_entry_slip.py --since 2026-09-01
"""
from __future__ import annotations

import argparse
import csv as _csv
import glob
import os
import re

ap = argparse.ArgumentParser(
    description="N のエントリー滑りを寄り時刻で分けて見る（調査用・発注しない）")
ap.add_argument("--since", default="", help="この日以降だけ (YYYY-MM-DD)")
ap.add_argument("--dir", default=".", help="CSV のあるフォルダ")
a = ap.parse_args()


def _rows(path: str) -> list:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(_csv.DictReader(f))
    except Exception:
        return []


def _pick(r: dict, *names) -> str:
    """列名の揺れを吸収して最初に見つかった値を返す。"""
    for n in names:
        v = str(r.get(n, "") or "").strip()
        if v and v not in ("—", "-", "nan"):
            return v
    return ""


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


def _code4(s: str) -> str:
    return re.sub(r"\.T$", "", str(s).strip())


def _secs(hms: str) -> int:
    """'09:00:50' や '090050' → 秒。取れなければ -1。"""
    s = re.sub(r"[^0-9]", "", str(hms))
    if len(s) >= 6:
        s = s[-6:] if len(s) > 6 else s
        try:
            return int(s[0:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:6])
        except Exception:
            return -1
    if len(s) == 4:
        try:
            return int(s[0:2]) * 3600 + int(s[2:4]) * 60
        except Exception:
            return -1
    return -1


def _hms(sec: int) -> str:
    if sec < 0:
        return "??:??:??"
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ── 日付ごとに orders と k_paper を突き合わせる ─────────────────────────
_od = {}
for p in sorted(glob.glob(os.path.join(a.dir, "orders_2*.csv"))):
    m = re.search(r"orders_(\d{8})\.csv$", os.path.basename(p))
    if m:
        _od[m.group(1)] = p

if not _od:
    raise SystemExit("orders_<日付>.csv が見つかりません。先に .\\fills を実行してください")

_recs, _no_board = [], []
for _ymd in sorted(_od):
    _iso = f"{_ymd[0:4]}-{_ymd[4:6]}-{_ymd[6:8]}"
    if a.since and _iso < a.since:
        continue

    # 板の記録（始値・寄り時刻）。無い日は突合できないので数えるだけ。
    _bd = {}
    for _bp in (os.path.join(a.dir, f"k_paper_{_ymd}.csv"),
                os.path.join(a.dir, f"n_paper_{_ymd}.csv")):
        for r in _rows(_bp):
            _sym = _code4(_pick(r, "symbol", "code"))
            if _sym:
                _bd[_sym] = r
        if _bd:
            break

    for r in _rows(_od[_ymd]):
        # 売り(エントリー)で、実際に約定したものだけ
        if "売" not in _pick(r, "side", "売買"):
            continue
        _fill = _f(_pick(r, "fill_price", "約定値"))
        if _fill <= 0:
            continue
        _cd = _code4(_pick(r, "code", "symbol", "コード"))
        _b = _bd.get(_cd)
        if not _b:
            _no_board.append((_iso, _cd))
            continue
        _open = _f(_pick(_b, "open_p", "open", "始値"))
        if _open <= 0:
            continue
        # ショートなので「高く売れていればプラス」
        _slip = (_fill - _open) / _open * 1e4
        _ot = _secs(_pick(_b, "open_time", "OpeningPriceTime", "寄り時刻"))
        _recs.append({
            "date": _iso, "code": _cd, "open_sec": _ot,
            "open_p": _open, "fill": _fill, "slip": _slip,
            "gap_bp": (_open - _f(_pick(_b, "prev_close", "前日終値"))) /
                      max(_f(_pick(_b, "prev_close", "前日終値")), 1e-9) * 1e4,
        })

if not _recs:
    raise SystemExit("突合できた売り注文がありません（k_paper_<日付>.csv も必要です）")

print()
print("=" * 78)
print("■ N のエントリー滑り（実約定 vs 始値）— **調査用。判定はしません**")
print("=" * 78)
print(f"{'日付':<12}{'銘柄':>6}{'寄り時刻':>11}{'ギャップ':>9}"
      f"{'始値':>10}{'実約定':>10}{'滑りbp':>9}")
print("-" * 78)
for r in sorted(_recs, key=lambda x: (x["date"], x["open_sec"])):
    print(f"{r['date']:<12}{r['code']:>6}{_hms(r['open_sec']):>11}"
          f"{r['gap_bp']:>+9.0f}{r['open_p']:>10,.1f}{r['fill']:>10,.1f}"
          f"{r['slip']:>+9.1f}")

# ── ★ 本題: 寄り時刻で分かれるか ────────────────────────────────
_t0 = 9 * 3600           # 09:00:00
_a = [r for r in _recs if 0 <= r["open_sec"] <= _t0]     # 09:00:00 ちょうど
_b = [r for r in _recs if r["open_sec"] > _t0]           # それ以降
_u = [r for r in _recs if r["open_sec"] < 0]             # 時刻が取れない

print()
print("■ ★ 寄り時刻で分けると")
print(f"{'':>22}{'件数':>6}{'件数加重bp':>12}{'最小':>9}{'最大':>9}")
print("-" * 60)
for _lbl, _g in (("09:00:00 ちょうど", _a), ("09:00:01 以降", _b),
                 ("時刻が取れない", _u)):
    if not _g:
        print(f"{_lbl:>22}{0:>6}{'—':>12}{'—':>9}{'—':>9}")
        continue
    _m = sum(x["slip"] for x in _g) / len(_g)
    print(f"{_lbl:>22}{len(_g):>6}{_m:>+12.1f}"
          f"{min(x['slip'] for x in _g):>+9.1f}"
          f"{max(x['slip'] for x in _g):>+9.1f}")

if _a and _b:
    _d = (sum(x["slip"] for x in _b) / len(_b)
          - sum(x["slip"] for x in _a) / len(_a))
    print()
    print(f"  差（遅く寄る − 09:00:00 ちょうど）= {_d:+.1f}bp")
    print(f"  ⚠ n={len(_a)} vs {len(_b)}。**この差で結論を出さないこと**。")
    print(f"     見るのは向きだけ：遅く寄るほうが有利なら、板寄せに")
    print(f"     間に合っているという §18.44 の構造と整合します")

# ── §18.49 の点推定（毎日ここだけ見れば進捗が分かる） ──────────────
print()
print("■ §18.49 の点推定")
_days = sorted({r["date"] for r in _recs})
print(f"{'日付':<12}{'件数':>6}{'件数加重bp':>12}")
print("-" * 32)
for _d in _days:
    _g = [r for r in _recs if r["date"] == _d]
    print(f"{_d:<12}{len(_g):>6}"
          f"{sum(x['slip'] for x in _g) / len(_g):>+12.1f}")
print("-" * 32)
_all = sum(r["slip"] for r in _recs) / len(_recs)
_dm = sum(sum(x["slip"] for x in _recs if x["date"] == d)
          / len([x for x in _recs if x["date"] == d]) for d in _days) / len(_days)
print(f"{'件数加重(点推定)':<12}{len(_recs):>6}{_all:>+12.1f}")
print(f"{'日平均(参考)':<12}{len(_days):>6}{_dm:>+12.1f}")
print()
print(f"  進捗 {len(_recs)} / 30 件  ・  損益分岐 -15.0bp")
print(f"  ⛔ 点推定は **件数加重**（§18.49 で先に宣言済み）。")
print(f"     日平均は1件の日と7件の日を同じ重みにするので使いません")
if _no_board:
    print()
    print(f"  ⚠ 板の記録が無くて突合できなかった注文 {len(_no_board)}件: "
          f"{', '.join(f'{d} {c}' for d, c in _no_board[:6])}")
print()
