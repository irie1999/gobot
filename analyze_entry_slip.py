#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N の**エントリー滑り**を、寄ってからの遅れ秒で分けて見る（調査用・発注しない）。

⛔ **日々の手順には入れないこと**（手順は .\\nexec / .\\fills / n_paper --close の3つ）。
   これは「30件が何を測っているか」を理解するための道具です。

★ 何を見るのか
  滑りの説明変数は「09:00ちょうどに寄ったか」ではなく、
  **寄ってから何秒後に検知/約定できたか** です。遅寄り銘柄でも、
  寄った直後に検知できれば滑らないからです。
      seen_lag = seen_ts   - open_time   （検知の遅れ）
      fill_lag = fill_time - open_time   （約定の遅れ）

★ 3つの量を混ぜないこと
  ① 純粋な滑り     … 約定した銘柄の 実約定 vs 始値
  ② 選択損失       … 合格したのに建てられなかったぶん（不約定・ガード・予算）
  ③ 理想との差     … ①+② = バックテスト(始値で全部建てる)との差
  いまは約定率100%なので②はゼロですが、-3%ガードや予算で落ちると出ます。

⛔ **N と J を混ぜない。** 判別は日付ではなく `ordered_signals_n.csv`
   （N 専用の台帳）で行います。2026-08-21 は J の運用日で、たまたま
   k_paper が無かったから混ざらなかっただけでした。

読むもの（どれも既存。何も発注しません）:
    ordered_signals_n.csv    N の発注台帳（これが N の定義）
    orders_<yyyyMMdd>.csv    .\\fills が出す全注文一覧（実約定値・約定時刻）
    k_paper_<yyyyMMdd>.csv   朝の板読み（始値・寄り時刻・検知時刻）

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
    description="N のエントリー滑りを遅れ秒で分けて見る（調査用・発注しない）")
ap.add_argument("--since", default="", help="この日以降だけ (YYYY-MM-DD)")
ap.add_argument("--dir", default=".", help="CSV のあるフォルダ")
ap.add_argument("--no-ledger", action="store_true",
                help="台帳での N 判別をやめる（⛔ J が混ざる。検証用）")
a = ap.parse_args()


def _rows(path: str) -> list:
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(_csv.DictReader(f))
    except Exception:
        return []


def _pick(r: dict, *names) -> str:
    for n in names:
        v = str(r.get(n, "") or "").strip()
        if v and v not in ("—", "-", "nan", "None"):
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
    """時刻文字列 → 秒。取れなければ -1。

    ⛔ 数字だけ抜いて末尾6桁、はダメ(2026-09-03 に踏んだ)。kabu の
       OpeningPriceTime は `2026-09-03T09:00:50+09:00` で来るので、
       末尾6桁は `+09:00` を含んだ `500900` になり `50:09:00` が出る。
       時刻の**位置**を正規表現で当てること。
    """
    s = str(hms).strip()
    if not s:
        return -1
    m = re.search(r"[T ](\d{1,2}):(\d{2}):(\d{2})", s)      # ISO
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?", s)     # 先頭が時刻
    if m:
        return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                + int(m.group(3) or 0))
    d = re.sub(r"[^0-9]", "", s)                            # 数字だけ
    if len(d) == 14:
        d = d[8:14]
    elif len(d) == 12:
        d = d[8:12]
    if len(d) == 6:
        try:
            return int(d[0:2]) * 3600 + int(d[2:4]) * 60 + int(d[4:6])
        except Exception:
            return -1
    if len(d) == 4:
        try:
            return int(d[0:2]) * 3600 + int(d[2:4]) * 60
        except Exception:
            return -1
    return -1


def _hms(sec: int) -> str:
    return "??:??:??" if sec < 0 else \
        f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _lag(a_sec: int, b_sec: int) -> int:
    """a - b。どちらか欠ければ -9999。"""
    return a_sec - b_sec if (a_sec >= 0 and b_sec >= 0) else -9999


# ── ⛔ N の定義：台帳に載っている (日付, 銘柄) だけ ────────────────────
_LEDGER = os.path.join(a.dir, "ordered_signals_n.csv")
_n_keys, _ledger_ok = set(), False
if not a.no_ledger:
    for r in _rows(_LEDGER):
        _st = _pick(r, "strategy")
        if _st and _st.upper() != "N":
            continue
        _d = _pick(r, "date")
        _s = _code4(_pick(r, "symbol", "code"))
        if _d and _s:
            _n_keys.add((_d[:10], _s))
    _ledger_ok = bool(_n_keys)

# ── 日付ごとに orders と k_paper を突き合わせる ─────────────────────────
_od = {}
for p in sorted(glob.glob(os.path.join(a.dir, "orders_2*.csv"))):
    m = re.search(r"orders_(\d{8})\.csv$", os.path.basename(p))
    if m:
        _od[m.group(1)] = p
if not _od:
    raise SystemExit("orders_<日付>.csv が見つかりません。先に .\\fills を実行してください")

_recs, _no_board, _bad_ts, _not_n, _missed = [], [], [], [], []
for _ymd in sorted(_od):
    _iso = f"{_ymd[0:4]}-{_ymd[4:6]}-{_ymd[6:8]}"
    if a.since and _iso < a.since:
        continue

    _bd = {}
    for _bp in (os.path.join(a.dir, f"k_paper_{_ymd}.csv"),
                os.path.join(a.dir, f"n_paper_{_ymd}.csv")):
        for r in _rows(_bp):
            _sym = _code4(_pick(r, "symbol", "code"))
            if _sym:
                _bd[_sym] = r
        if _bd:
            break

    # ② 選択損失: 合格したのに建てられなかった銘柄
    for _sym, _b in _bd.items():
        if _pick(_b, "pass_gap") not in ("1", "True", "true"):
            continue
        if _pick(_b, "ordered") in ("1", "True", "true"):
            continue
        _missed.append((_iso, _sym,
                        _pick(_b, "guard_ng") or _pick(_b, "stale_open") or "予算/上限"))

    for r in _rows(_od[_ymd]):
        if "売" not in _pick(r, "side", "売買"):
            continue
        _fill = _f(_pick(r, "fill_price", "約定値"))
        if _fill <= 0:
            continue
        _cd = _code4(_pick(r, "code", "symbol", "コード"))
        # ⛔ N の台帳に無い注文は落とす（J が混ざるのを防ぐ）
        if _ledger_ok and (_iso, _cd) not in _n_keys:
            _not_n.append((_iso, _cd))
            continue
        _b = _bd.get(_cd)
        if not _b:
            _no_board.append((_iso, _cd))
            continue
        _open = _f(_pick(_b, "open_p", "open", "始値"))
        if _open <= 0:
            continue

        _raw_t = _pick(_b, "open_time", "OpeningPriceTime", "寄り時刻")
        _ot = _secs(_raw_t)
        if _ot >= 0 and not (9 * 3600 <= _ot <= 15 * 3600 + 30 * 60):
            _bad_ts.append((_iso, _cd, _raw_t))
            _ot = -1
        _st = _secs(_pick(_b, "seen_ts", "ts"))
        _ft = _secs(_pick(r, "fill_time_s", "fill_time", "約定"))
        _pc = _f(_pick(_b, "prev_close", "前日終値"))

        _recs.append({
            "date": _iso, "code": _cd,
            "open_sec": _ot, "seen_lag": _lag(_st, _ot), "fill_lag": _lag(_ft, _ot),
            "open_p": _open, "fill": _fill,
            "slip": (_fill - _open) / _open * 1e4,   # ショート: 高く売れたら+
            "gap_bp": (_open - _pc) / max(_pc, 1e-9) * 1e4 if _pc > 0 else 0.0,
        })

if not _recs:
    raise SystemExit("突合できた N の売り注文がありません "
                     "（ordered_signals_n.csv と k_paper_<日付>.csv が要ります）")

_days = sorted({r["date"] for r in _recs})

print()
print("=" * 82)
print("■ N のエントリー滑り（実約定 vs 始値）— **調査用。判定はしません**")
print("=" * 82)
print(f"  N の判別: " + (
    f"**ordered_signals_n.csv**（{len(_n_keys)}件の台帳）" if _ledger_ok else
    "⛔ **台帳が無いので全売り注文を N とみなしています**（J が混ざりえます）"))
print(f"  採用 {len(_recs)}件 / 除外(N以外) {len(_not_n)}件 / "
      f"板の記録なし {len(_no_board)}件")
print()
print(f"{'日付':<12}{'銘柄':>6}{'寄り':>10}{'検知遅れ':>9}{'約定遅れ':>9}"
      f"{'ギャップ':>8}{'始値':>10}{'実約定':>10}{'滑りbp':>9}")
print("-" * 82)
for r in sorted(_recs, key=lambda x: (x["date"], x["open_sec"])):
    _sl = f"{r['seen_lag']:+d}s" if r["seen_lag"] > -9000 else "—"
    _fl = f"{r['fill_lag']:+d}s" if r["fill_lag"] > -9000 else "—"
    print(f"{r['date']:<12}{r['code']:>6}{_hms(r['open_sec']):>10}"
          f"{_sl:>9}{_fl:>9}{r['gap_bp']:>+8.0f}"
          f"{r['open_p']:>10,.1f}{r['fill']:>10,.1f}{r['slip']:>+9.1f}")


def _group(lbl: str, key: str, edges: list) -> None:
    """遅れ秒の帯ごとに滑りを出す。"""
    print()
    print(f"■ ★ {lbl} で分けると")
    print(f"{'':>18}{'件数':>6}{'件数加重bp':>12}{'最小':>9}{'最大':>9}")
    print("-" * 56)
    _lo = -1
    for _hi in edges + [10 ** 9]:
        _g = [r for r in _recs if r[key] > -9000 and _lo < r[key] <= _hi]
        _nm = (f"〜{_hi}s" if _lo < 0 else
               (f"{_lo + 1}s〜" if _hi > 10 ** 8 else f"{_lo + 1}〜{_hi}s"))
        if _g:
            _m = sum(x["slip"] for x in _g) / len(_g)
            print(f"{_nm:>18}{len(_g):>6}{_m:>+12.1f}"
                  f"{min(x['slip'] for x in _g):>+9.1f}"
                  f"{max(x['slip'] for x in _g):>+9.1f}")
        else:
            print(f"{_nm:>18}{0:>6}{'—':>12}{'—':>9}{'—':>9}")
        _lo = _hi
    _u = [r for r in _recs if r[key] <= -9000]
    if _u:
        print(f"{'取れない':>18}{len(_u):>6}"
              f"{sum(x['slip'] for x in _u) / len(_u):>+12.1f}"
              f"{min(x['slip'] for x in _u):>+9.1f}"
              f"{max(x['slip'] for x in _u):>+9.1f}")


_group("検知の遅れ（seen_ts − 寄り時刻）", "seen_lag", [0, 10, 30, 60])
_group("約定の遅れ（fill_time − 寄り時刻）", "fill_lag", [0, 10, 30, 60])

# ── 3量の分離 ──────────────────────────────────────────────────
print()
print("■ 3つの量")
_slip = sum(r["slip"] for r in _recs) / len(_recs)
print(f"  ① 純粋な滑り（約定した {len(_recs)}件）        {_slip:>+8.1f}bp")
if _missed:
    print(f"  ② 選択損失（合格したが建てられず {len(_missed)}件） "
          f"⚠ 損益は未計測（終値が要る）")
    for _d, _s, _w in _missed[:5]:
        print(f"       {_d} {_s}  理由: {_w}")
else:
    print(f"  ② 選択損失                          "
          f"**0件**（合格した銘柄は全部建てられている）")
print(f"  ③ 理想との差 = ①+②")

# ── §18.49 の点推定 ────────────────────────────────────────────
print()
print("■ §18.49 の点推定")
print(f"{'日付':<12}{'件数':>6}{'件数加重bp':>12}")
print("-" * 32)
for _d in _days:
    _g = [r for r in _recs if r["date"] == _d]
    print(f"{_d:<12}{len(_g):>6}{sum(x['slip'] for x in _g) / len(_g):>+12.1f}")
print("-" * 32)
_dm = sum(sum(x["slip"] for x in _recs if x["date"] == d)
          / len([x for x in _recs if x["date"] == d]) for d in _days) / len(_days)
print(f"{'件数加重(点推定)':<12}{len(_recs):>6}{_slip:>+12.1f}")
print(f"{'日平均(参考)':<12}{len(_days):>6}{_dm:>+12.1f}")
print()
print(f"  進捗 **{len(_recs)} / 30 件**  ・  独立な営業日 **{len(_days)}日**"
      f"  ・  損益分岐 -15.0bp")
print(f"  ⛔ 点推定は **件数加重**（§18.49 で先に宣言済み）。日平均は")
print(f"     1件の日と7件の日を同じ重みにするので使いません")
print(f"  ⚠ 同日相関があるので、信頼区間の実効サンプルは**件数ではなく日数**です")

if _not_n:
    print()
    print(f"  [除外] N の台帳に無い売り注文 {len(_not_n)}件: "
          f"{', '.join(f'{d} {c}' for d, c in _not_n[:6])}")
if _no_board:
    print()
    print(f"  ⚠ 板の記録が無くて突合できなかった注文 {len(_no_board)}件: "
          f"{', '.join(f'{d} {c}' for d, c in _no_board[:6])}")
if _bad_ts:
    print()
    print(f"  ⛔ 場外の寄り時刻 {len(_bad_ts)}件 — 時刻の列を読み違えています")
    for _d, _c, _v in _bad_ts[:5]:
        print(f"       {_d} {_c}: {_v!r}")
print()
