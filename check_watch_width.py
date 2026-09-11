#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watch を広げたら **その日 何件 増えたか** を事後に数える。

⛔⛔ **照会だけ。1件も発注しない。** kabu にも繋がない。読むのは
   n_signals_<日付>.csv(前夜の候補・全件) と k_paper_<日付>.csv(当日 読んだ
   50件の板) と、yfinance の日足だけ。

★ 何のためか (2026-09-11)
  N は前夜の候補(中央120件前後)のうち **流動性 上位50件しか読んでいない**
  (kabu の登録上限 / §18.44)。51位以下に合格銘柄が何件いたのかは、読んで
  いない以上その場では分からない。だが **始値は日足にも残る**ので、
  引け後なら数えられる。

  2026-09-09〜11 の実測が 2件 / 2件 / 0件 = 1.3件/日 で、バックテストの
  期待(TRAIN 7.2件/日 / 2026-08 の歩み値 9.1件/日)の 1/5〜1/7 しかない。
  「相場なのか、watch50 で切っているせいなのか」をこれで切り分ける。

★★ 先に **日足の始値が板の始値と一致するか**を確かめる
  一致しなければ、51位以下の数字は日足由来なので信用できない。
  当日 読んだ50件は両方 持っているので突き合わせられる。
  ⛔ 一致率が低いときは帯別の集計を **出さない**。推測で数えない。

使い方
    python check_watch_width.py                       # 今日
    python check_watch_width.py --date 2026-09-11
    python check_watch_width.py --glob "n_signals_*.csv"   # 貯まったぶん全部
    python check_watch_width.py --bands 50,100,150    # 帯の区切り
"""
from __future__ import annotations

# ⛔ Windows で `> out.txt` にリダイレクトすると stdout が cp932 になり、
#   ⛔ ⚠ ✅ で UnicodeEncodeError を出して落ちる。import だけで効く。
import console_safe  # noqa: F401

import argparse
import csv as _csv
import datetime as _dt
import glob as _glob
import re
from pathlib import Path

# N の条件 (k_open_confirm と同じ値。変えるときは両方)
_GAP_BP = 100.0          # 始値が前日終値 +100bp 以上
_MIN_PX = 1000.0         # 始値も 1,000〜6,000円
_MAX_PX = 6000.0


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _read(p: Path) -> list:
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            return list(_csv.DictReader(f))
    except Exception as e:                                       # noqa: BLE001
        print(f"[!] {p.name} を読めませんでした: {e}")
        return []


def _yf_opens(syms: list, ymd: str) -> dict:
    """その日の **日足の始値** を symbol -> open で返す。

    ⚠ yfinance は 1日ぶんを取りに行くと前後がずれることがあるので、
      前後2日を取って **日付で厳密に選ぶ**。
    """
    try:
        import yfinance as yf
    except Exception:                                            # noqa: BLE001
        print("[!] yfinance が入っていません (pip install yfinance)")
        return {}
    _d = _dt.date.fromisoformat(ymd)
    _tk = [f"{s}.T" if not str(s).endswith(".T") else str(s) for s in syms]
    _out: dict = {}
    _B = 200
    for _i in range(0, len(_tk), _B):
        _b = _tk[_i:_i + _B]
        try:
            _df = yf.download(_b, start=_d - _dt.timedelta(days=4),
                              end=_d + _dt.timedelta(days=1),
                              interval="1d", progress=False,
                              auto_adjust=False, group_by="ticker",
                              threads=True)
        except Exception as e:                                   # noqa: BLE001
            print(f"[!] yfinance の取得に失敗: {e}")
            return _out
        if _df is None or _df.empty:
            continue
        for _t in _b:
            try:
                _s = _df[_t]["Open"] if len(_b) > 1 else _df["Open"]
            except Exception:                                    # noqa: BLE001
                continue
            for _ix, _v in _s.items():
                if str(getattr(_ix, "date", lambda: _ix)())[:10] == ymd:
                    try:
                        _fv = float(_v)
                    except (TypeError, ValueError):
                        continue
                    if _fv > 0:
                        _out[_t.removesuffix(".T")] = _fv
                    break
        print(f"  … {min(_i + _B, len(_tk))}/{len(_tk)}銘柄", flush=True)
    return _out


def _one_day(ymd: str, bands: list) -> dict | None:
    _sig = Path(f"n_signals_{ymd.replace('-', '')}.csv")
    if not _sig.exists():
        _sig = Path(f"n_signals_{ymd}.csv")
    if not _sig.exists():
        print(f"[!] {ymd}: n_signals が見つかりません")
        return None
    _rows = [r for r in _read(_sig) if _num(r.get("prev_close")) > 0]
    if not _rows:
        print(f"[!] {ymd}: 候補が0件")
        return None

    # ── 順位。rank_n が無い古い CSV は流動性降順で振り直す ──────────────
    if any(str(r.get("rank_n") or "").strip() for r in _rows):
        _rows.sort(key=lambda r: _num(r.get("rank_n")) or 1e9)
    else:
        print("  ⚠ rank_n が無いので流動性降順で順位を振り直します")
        _rows.sort(key=lambda r: -_num(r.get("liquidity")))
    for _i, _r in enumerate(_rows, 1):
        _r["_rank"] = _i

    # ── ① 板の始値と日足の始値を突き合わせる ──────────────────────────
    _kp = Path(f"k_paper_{ymd.replace('-', '')}.csv")
    _board = {str(r.get("symbol") or "").strip(): _num(r.get("open_p"))
              for r in _read(_kp) if _num(r.get("open_p")) > 0}
    print(f"\n[{ymd}] 候補 {len(_rows)}件 / 当日 板で読めた {len(_board)}件")
    print("  日足の始値を取得中…", flush=True)
    _day = _yf_opens([str(r.get("symbol") or "").strip() for r in _rows], ymd)
    if not _day:
        print("  ⛔ 日足の始値が取れませんでした")
        return None

    _diff, _miss = [], 0
    for _s, _bp in _board.items():
        _dp = _day.get(_s)
        if not _dp:
            _miss += 1
            continue
        _diff.append((abs(_dp - _bp) / _bp * 1e4, _s, _bp, _dp))
    if not _diff:
        print("  ⛔ 突き合わせられる銘柄がありません")
        return None
    _diff.sort()
    _n1 = sum(1 for d, *_ in _diff if d <= 1.0)
    _n5 = sum(1 for d, *_ in _diff if d <= 5.0)
    print(f"  [照合] 板 vs 日足 {len(_diff)}件: "
          f"1bp以内 {_n1}件 ({_n1 / len(_diff) * 100:.0f}%) / "
          f"5bp以内 {_n5}件 ({_n5 / len(_diff) * 100:.0f}%)"
          + (f" / 日足が無い {_miss}件" if _miss else ""))
    for _d, _s, _b, _y in _diff[-3:]:
        if _d > 5.0:
            print(f"     ⚠ {_s} 板 {_b:,.1f} vs 日足 {_y:,.1f} ({_d:+.1f}bp)")
    if _n5 / len(_diff) < 0.95:
        # ⛔ 一致しないなら 51位以下の数字も信用できない。**出さない**
        print("  ⛔ **一致率が 95%未満**。日足の始値を代用できないので、"
              "帯別の集計は出しません")
        return None
    print("  ✅ 日足の始値を代用できます(51位以下もこれで数えます)")

    # ── ② 帯別に「N の条件を満たした件数」を数える ───────────────────
    #   条件は k_open_confirm._mk_row と同じ:
    #     gap = (始値 − 前日終値)/前日終値×1e4 ≥ 100bp  かつ
    #     1,000 ≤ 始値 ≤ 6,000
    _edges = [0] + sorted(bands) + [len(_rows)]
    _tab, _extra = [], 0
    for _a, _b in zip(_edges, _edges[1:]):
        if _a >= len(_rows):
            break
        _seg = [r for r in _rows if _a < r["_rank"] <= min(_b, len(_rows))]
        if not _seg:
            continue
        _have = _px = _ok = 0
        for _r in _seg:
            _s = str(_r.get("symbol") or "").strip()
            _op, _pc = _day.get(_s, 0.0), _num(_r.get("prev_close"))
            if _op <= 0 or _pc <= 0:
                continue
            _have += 1
            if not (_MIN_PX <= _op <= _MAX_PX):
                continue
            _px += 1
            if (_op - _pc) / _pc * 1e4 >= _GAP_BP:
                _ok += 1
        _tab.append((f"{_a + 1}〜{min(_b, len(_rows))}", len(_seg), _have,
                     _px, _ok))
        if _a >= 50:
            _extra += _ok
    print(f"\n  {'順位帯':<10}{'候補':>6}{'日足あり':>9}{'価格帯内':>9}"
          f"{'+100bp以上':>11}{'合格率':>8}")
    for _lb, _n, _hv, _p, _o in _tab:
        print(f"  {_lb:<10}{_n:>6}{_hv:>9}{_p:>9}{_o:>11}"
              f"{(_o / _p * 100 if _p else 0):>7.1f}%")
    _t50 = sum(o for lb, _n, _hv, _p, o in _tab
               if int(lb.split("〜")[0]) <= 50)
    print(f"\n  ★ 上位50件(いまの運用) {_t50}件 → "
          f"51位以下に **あと {_extra}件** いた")
    return {"date": ymd, "top50": _t50, "extra": _extra, "cand": len(_rows)}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="watch を広げたら何件 増えたかを事後に数える(照会のみ)")
    ap.add_argument("--date", default="", help="YYYY-MM-DD(既定 今日)")
    ap.add_argument("--glob", default="",
                    help='複数日まとめて。例 "n_signals_*.csv"')
    ap.add_argument("--bands", default="50,100,150",
                    help="順位の区切り(既定 50,100,150)")
    a = ap.parse_args()
    _bands = [int(x) for x in str(a.bands).split(",") if x.strip().isdigit()]

    if a.glob:
        _ds = sorted({m.group(1) for p in _glob.glob(a.glob)
                      for m in [re.search(r"(\d{8})", Path(p).name)] if m})
        _ds = [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in _ds]
    else:
        _ds = [a.date or f"{_dt.date.today()}"]

    print("=" * 74)
    print("watch を広げたら何件 増えたか (照会のみ・発注しません)")
    print(f"  条件: 始値が前日終値 +{_GAP_BP:.0f}bp 以上 / "
          f"始値 {_MIN_PX:,.0f}〜{_MAX_PX:,.0f}円")
    print("=" * 74)
    _all = [x for d in _ds if (x := _one_day(d, _bands))]
    if len(_all) > 1:
        print("\n" + "=" * 74)
        print(f"  {'日付':<12}{'候補':>6}{'上位50':>8}{'51位以下':>10}")
        for _r in _all:
            print(f"  {_r['date']:<12}{_r['cand']:>6}{_r['top50']:>8}"
                  f"{_r['extra']:>10}")
        _t = sum(r["top50"] for r in _all)
        _e = sum(r["extra"] for r in _all)
        print(f"  {'合計':<12}{sum(r['cand'] for r in _all):>6}{_t:>8}{_e:>10}")
        print(f"\n  ★ {len(_all)}営業日で 上位50件が {_t}件、"
              f"51位以下に {_e}件 いた")
        if _t:
            print(f"     watch100 以上にすれば **{_e / _t:.1f}倍** になる計算")
        print("  ⚠ これは『何件 建てられたか』であって損益ではない。"
              "51位以下は流動性が低く、§18.70⑥ では")
        print("     枠が埋まる日に大きく負けている。**件数だけで決めないこと**")
    print("=" * 74)


if __name__ == "__main__":
    main()
