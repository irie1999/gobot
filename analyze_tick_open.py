#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JPX 株式歩み値(ティック)から **寄り直後の減衰カーブ**を実測する（調査用）。

⛔ **日々の手順には入れない。** 発注もしない。CSV を読むだけ。

★ 何のために要るのか
  N の執行コストは、いま §18.44 の減衰カーブ（**5分足・J の母集団**）を
  **外挿**して見積もっている。実測したいのはこれ:

      「寄りから t 秒後に売ったら、始値より何 bp 不利になるか」
      を **流動性帯ごとに**（1〜50位 / 51〜100位 / 101〜150位 …）

  watch150 の追加枠は損益分岐が 9.6bp しかないので、ここが数 bp 違うと
  採否が変わる。ローテーションの判定遅延（kabu で実測済み）と掛け合わせて
  初めて「本当のコスト」が出る。

★ ショート視点の符号
      slip_bp = (price_t - open) / open * 10000
  N は**売る**ので、価格が下がる（負）と不利。§18.44 と同じ向き。
  （5分後 -10.4bp = 5分待つと 10.4bp 安く売ることになる）

⚠ 歩み値は **約定履歴であって板ではない**。
  「その価格で約定があった」ことしか分からず、「100株 売れたか」「最良買い
  気配はいくらか」は分からない。**執行価格の代理値**として使うこと。
  実際のショートは買い気配を叩くので、ここで出る数字より不利になる。

使い方
    # 1) 7z を展開してから CSV を渡す（.7z のままは読めない）
    python analyze_tick_open.py --csv "C:/…/stock_tick_202608*.csv"

    # 2) N の候補だけに絞る（前夜の候補リストがあれば）
    python analyze_tick_open.py --csv "…*.csv" --symbols n_signals_shadow.csv

    # 3) 窓を広げる／狭める（既定 09:00〜09:30）
    python analyze_tick_open.py --csv "…*.csv" --until 0915

出力は**テキストだけ**。そのまま貼れる大きさにしてある。
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
import time
from collections import defaultdict

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--csv", type=str, required=True,
                help="歩み値CSV / フォルダ / .7z / .zip。"
                     ".7z は py7zr があれば自動展開する")
ap.add_argument("--extract-to", type=str, default="",
                help=".7z の展開先（既定はアーカイブと同じ場所）。"
                     "⛔ 空き容量が足りないときは別ドライブを指定")
ap.add_argument("--from", dest="t_from", type=str, default="0900",
                help="窓の開始 HHMM（既定 0900）")
ap.add_argument("--until", type=str, default="0930",
                help="窓の終了 HHMM（既定 0930 / 遅寄りを拾うため広め）")
ap.add_argument("--offsets", type=str, default="1,5,10,30,60,300",
                help="始値から何秒後を測るか（秒・カンマ区切り）")
ap.add_argument("--bands", type=str, default="50,100,150,300",
                help="流動性帯の区切り（順位）")
ap.add_argument("--symbols", type=str, default="",
                help="銘柄を絞る。symbol 列を持つCSV（省略時は全銘柄）")
ap.add_argument("--out", type=str, default="",
                help="銘柄日ごとの明細もCSVに出す（省略時は集計のみ）")
ap.add_argument("--progress", type=int, default=5_000_000,
                help="何行ごとに進捗を出すか")
a = ap.parse_args()

# HHMM → HHMMSS は **×100**（秒を後ろに足す）。
# ⛔ ×10000 と書いて窓が 0件になった(2026-09-07)。0900→90000 が正しい。
_FROM = int(a.t_from) * 100
_UNTIL = int(a.until) * 100
_OFFS = [int(x) for x in a.offsets.split(",") if x.strip()]
_BANDS = [int(x) for x in a.bands.split(",") if x.strip()]

# ★ .7z をそのまま渡せるようにする(2026-09-07)。
#   ⚠ 568MB の圧縮が 4〜6GB に展開される。**空き容量を先に見る**。
#     7z は solid 圧縮なのでメモリ内ストリームは現実的でなく、
#     いったんディスクに出すしかない。
if a.csv.lower().endswith((".7z", ".zip")):
    import shutil
    _arc = a.csv
    _dst = a.extract_to or os.path.splitext(_arc)[0]
    _free = shutil.disk_usage(os.path.dirname(_arc) or ".").free
    _need = os.path.getsize(_arc) * 10          # LZMA2 の CSV は8〜10倍
    print(f"[展開] {os.path.basename(_arc)} "
          f"({os.path.getsize(_arc) / 1e9:.2f}GB) → {_dst}")
    print(f"       必要 約{_need / 1e9:.1f}GB / 空き {_free / 1e9:.1f}GB")
    if _free < _need:
        sys.exit(f"[error] **空き容量が足りません**。"
                 f"約{_need / 1e9:.1f}GB 必要ですが {_free / 1e9:.1f}GB しか"
                 f"ありません。\n"
                 f"        別ドライブに展開して --csv でそのフォルダを"
                 f"指定してください")
    if os.path.isdir(_dst) and glob.glob(os.path.join(_dst, "**", "*.csv"),
                                         recursive=True):
        print("       既に展開済みのようなので、そのまま使います")
    elif _arc.lower().endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(_arc) as _z:
            _z.extractall(_dst)
    else:
        try:
            import py7zr
        except ImportError:
            sys.exit("[error] .7z を読むには py7zr が要ります:\n"
                     "          pip install py7zr\n"
                     "        または 7-Zip で展開してフォルダを渡してください")
        _t9 = time.time()
        with py7zr.SevenZipFile(_arc, mode="r") as _z:
            _z.extractall(path=_dst)
        print(f"       展開 {time.time() - _t9:.0f}秒")
    a.csv = _dst

# ★ フォルダを渡されたら中を再帰的に探す。глоб に ** が無くても拾う。
#   ⛔ 「見つかりません」で終わらせず、**そこに何があるか**を出すこと
#     (2026-09-07: 展開先の名前が違って詰まった)。
_pat = a.csv
if os.path.isdir(_pat):
    _pat = os.path.join(_pat, "**", "*.csv")
_files = sorted(glob.glob(_pat, recursive=True))
if not _files and "*" in _pat:
    # 展開すると1つ深いフォルダに入ることが多いので下も探す。
    # ⛔⛔ **黙って別のファイルを読まないこと**(2026-09-07)。
    #   指定が空振りしたのに親を再帰検索して別ファイルを拾い、
    #   「頼んだものと違うデータで結果が出る」形になっていた。
    _alt = sorted(glob.glob(os.path.join(os.path.dirname(_pat) or ".",
                                         "**", "*.csv"), recursive=True))
    if _alt:
        print(f"⚠⚠ 指定 `{a.csv}` は **0件**でした。"
              f"親フォルダを再帰検索して {len(_alt)}件 見つけました:")
        for _f2 in _alt[:10]:
            print(f"     {_f2}")
        if len(_alt) > 10:
            print(f"     … 他 {len(_alt) - 10}件")
        print("   ⛔ **これで良いか確認してください。**"
              " 意図した月のデータですか?")
        if input("   続行しますか [y/N]: ").strip().lower() not in ("y", "yes"):
            sys.exit("中止しました。--csv を指定し直してください")
        _files = _alt
if not _files:
    _dir = os.path.dirname(_pat) or "."
    _up = _dir
    while _up and not os.path.isdir(_up):
        _up = os.path.dirname(_up)
    print(f"[error] CSV が見つかりません: {a.csv}")
    if _up and os.path.isdir(_up):
        print(f"\n  {_up} の中身:")
        try:
            for _e in sorted(os.listdir(_up))[:40]:
                _p = os.path.join(_up, _e)
                _sz = (f"{os.path.getsize(_p) / 1e6:,.1f}MB"
                       if os.path.isfile(_p) else "<フォルダ>")
                print(f"    {_e:<50} {_sz}")
        except Exception as _e2:
            print(f"    (読めません: {_e2})")
    print("\n  ⛔ .7z のままなら 7-Zip で展開してください。")
    print("     展開後は **フォルダごと渡せます**:")
    print('       python analyze_tick_open.py --csv "C:/…/展開したフォルダ"')
    sys.exit(1)
print(f"[info] {len(_files)}ファイル: "
      + ", ".join(os.path.basename(f) for f in _files[:3])
      + (" …" if len(_files) > 3 else ""))
print(f"[info] 窓 {a.t_from}〜{a.until} / 始値から {_OFFS} 秒後を測る")
print("[info] ⛔ 照会のみ。発注も外部送信もしません", flush=True)

_only: set = set()
if a.symbols:
    with open(a.symbols, encoding="utf-8-sig") as _f:
        for _r in csv.DictReader(_f):
            _c = str(_r.get("symbol") or _r.get("code") or "").strip()
            if _c:
                # 4桁(7203) でも 5桁(72030) でも受ける
                _only.add(_c.replace(".T", "").zfill(4)[:4])
    print(f"[info] 銘柄を {len(_only)}件 に絞ります（{a.symbols}）")


def _hhmmss(t: str) -> int:
    """time 列 → HHMMSS の int。新形式11桁(μs) / 旧形式9桁(ms) 両対応。"""
    s = t.strip()
    if not s.isdigit():
        return -1
    n = len(s)
    if n >= 11:            # 新形式: HHMMSS + マイクロ秒(6桁)
        return int(s.zfill(12)[:6])
    if n >= 9:             # 旧形式: HHMMSS + ミリ秒(3桁)
        return int(s.zfill(9)[:6])
    return -1


def _epoch_s(t: str) -> float:
    """time 列 → その日の 00:00 からの秒（小数）。"""
    s = t.strip()
    n = len(s)
    if n >= 11:
        s = s.zfill(12)
        base, frac = s[:6], int(s[6:]) / 1e6
    elif n >= 9:
        s = s.zfill(9)
        base, frac = s[:6], int(s[6:]) / 1e3
    else:
        return -1.0
    return (int(base[:2]) * 3600 + int(base[2:4]) * 60 + int(base[4:6])) + frac


# ── 銘柄日ごとの状態。**窓の外は turnover だけ足して捨てる** ──────────
class St:
    __slots__ = ("turn", "o_px", "o_t", "cur", "hit", "n", "bad_order")

    def __init__(self):
        self.turn = 0.0            # その日の売買代金（帯分けに使う）
        self.o_px = 0.0            # 始値（session1 の最初の約定）
        self.o_t = -1.0            # 始値の時刻（秒）
        self.cur = 0.0             # 直近の約定値
        self.hit: dict = {}        # offset秒 -> そのときの価格
        self.n = 0                 # 窓内の約定件数
        self.bad_order = 0         # 時刻が巻き戻った回数（並びの検査）


_st: dict = defaultdict(St)
_n_row = _n_win = _n_skip = 0
_hdr_new = _hdr_old = 0
_t0 = time.time()

for _fp in _files:
    with open(_fp, encoding="utf-8-sig", newline="") as _f:
        _rd = csv.reader(_f)
        try:
            _head = next(_rd)
        except StopIteration:
            continue
        _hl = [h.strip().lower() for h in _head]
        try:
            _i_date = _hl.index("date")
            _i_code = _hl.index("issue code")
            _i_time = _hl.index("time")
            _i_sess = _hl.index("session distinction")
            _i_px = _hl.index("price")
            _i_vol = _hl.index("trading volume")
        except ValueError as _e:
            sys.exit(f"[error] {os.path.basename(_fp)} の列が想定と違います: {_e}\n"
                     f"        見つかった列: {_head}")
        (_hdr_new if len(_hl) <= 12 else _hdr_old).__int__()  # 形だけ
        _is_old = "short selling regulation flag" in _hl
        _hdr_old += _is_old
        _hdr_new += (not _is_old)

        for _r in _rd:
            _n_row += 1
            if _n_row % a.progress == 0:
                print(f"  … {_n_row:,}行 / {time.time() - _t0:.0f}s "
                      f"（窓内 {_n_win:,}）", flush=True)
            try:
                _sess = _r[_i_sess].strip()
                if _sess != "1":
                    continue                    # 後場は見ない
                _code4 = _r[_i_code].strip()
                _c4 = _code4[:4] if len(_code4) >= 5 else _code4.zfill(4)
                if _only and _c4 not in _only:
                    continue
                _px = float(_r[_i_px])
                _vol = float(_r[_i_vol])
            except Exception:
                _n_skip += 1
                continue
            _k = (_r[_i_date].strip(), _c4)
            _s = _st[_k]
            _s.turn += _px * _vol               # ★ 帯分け用（窓の外も足す）
            _hs = _hhmmss(_r[_i_time])
            if _hs < _FROM or _hs > _UNTIL:
                continue
            _ts = _epoch_s(_r[_i_time])
            if _ts < 0:
                _n_skip += 1
                continue
            _n_win += 1
            _s.n += 1
            if _s.o_t < 0:                      # ★ 窓内の最初の約定 = 始値
                _s.o_px, _s.o_t, _s.cur = _px, _ts, _px
                continue
            if _ts < _s.o_t:
                _s.bad_order += 1               # 並びが時刻順でない
                continue
            _s.cur = _px
            _el = _ts - _s.o_t
            for _o in _OFFS:                    # その秒を跨いだ時点の値を記録
                if _o not in _s.hit and _el >= _o:
                    _s.hit[_o] = _px

print(f"[読了] {_n_row:,}行 / 窓内 {_n_win:,}行 / スキップ {_n_skip:,} "
      f"/ {time.time() - _t0:.0f}s")
print(f"[形式] 新(2021-08〜) {_hdr_new}ファイル / 旧 {_hdr_old}ファイル")
if not _st:
    sys.exit("[error] 対象データが0件です（窓・銘柄の指定を確認してください）")

# ── 日ごとに売買代金で順位をつけ、帯に分ける ─────────────────────────
_by_date: dict = defaultdict(list)
for (_d, _c), _s in _st.items():
    if _s.o_px > 0:
        _by_date[_d].append((_s.turn, _c, _s))

_rows: list = []
for _d, _lst in _by_date.items():
    _lst.sort(key=lambda x: -x[0])              # 売買代金 降順
    for _rank, (_turn, _c, _s) in enumerate(_lst, 1):
        _rows.append({"date": _d, "code": _c, "rank": _rank, "turn": _turn,
                      "open_px": _s.o_px, "open_t": _s.o_t, "n": _s.n,
                      "bad": _s.bad_order,
                      **{f"s{_o}": _s.hit.get(_o) for _o in _OFFS}})

_bad = sum(r["bad"] for r in _rows)
if _bad:
    print(f"⚠ 時刻が巻き戻った行 {_bad:,}件。ファイルが時刻順でない可能性")


def _band(_rk: int) -> str:
    _lo = 1
    for _b in _BANDS:
        if _rk <= _b:
            return f"{_lo}〜{_b}位"
        _lo = _b + 1
    return f"{_lo}位〜"


def _q(_v: list, _p: float):
    if not _v:
        return float("nan")
    _s = sorted(_v)
    return _s[min(len(_s) - 1, int(len(_s) * _p))]


print("\n" + "=" * 78)
print("■ 寄りからの減衰（ショート視点 / 負 = 始値より安く売ることになる）")
print("=" * 78)
print("  ⚠ 歩み値は約定履歴で板ではない。**執行価格の代理値**であり、")
print("     実際のショートは買い気配を叩くのでこれより不利になる。")
print(f"\n  {'帯':>12} {'銘柄日':>7} " + "".join(f"{f'+{o}s':>9}" for o in _OFFS))
_agg: dict = defaultdict(lambda: defaultdict(list))
for _r in _rows:
    _b = _band(_r["rank"])
    for _o in _OFFS:
        _p = _r.get(f"s{_o}")
        if _p and _r["open_px"] > 0:
            _agg[_b][_o].append((_p - _r["open_px"]) / _r["open_px"] * 1e4)
_order = []
_lo = 1
for _b in _BANDS:
    _order.append(f"{_lo}〜{_b}位")
    _lo = _b + 1
_order.append(f"{_lo}位〜")
for _b in _order:
    if _b not in _agg:
        continue
    _n = max(len(_agg[_b][_o]) for _o in _OFFS)
    print(f"  {_b:>12} {_n:>7,} "
          + "".join(f"{_q(_agg[_b][_o], 0.5):>+9.1f}" for _o in _OFFS)
          + "   ← 中央値bp")

print(f"\n  {'帯':>12} {'':>7} " + "".join(f"{f'+{o}s':>9}" for o in _OFFS))
for _b in _order:
    if _b not in _agg:
        continue
    print(f"  {_b:>12} {'':>7} "
          + "".join(f"{sum(_agg[_b][_o]) / max(1, len(_agg[_b][_o])):>+9.1f}"
                    for _o in _OFFS)
          + "   ← 平均bp")

# ── 寄り時刻の分布（遅寄りがどれだけあるか）──────────────────────────
print("\n■ 寄り時刻（session1 の最初の約定）")
for _b in _order:
    _ot = [r["open_t"] for r in _rows if _band(r["rank"]) == _b]
    if not _ot:
        continue

    def _hm(_s):
        return f"{int(_s // 3600):02d}:{int(_s % 3600 // 60):02d}:{_s % 60:04.1f}"
    _late = sum(1 for t in _ot if t > 9 * 3600 + 60)
    print(f"  {_b:>12} n={len(_ot):>6,}  中央 {_hm(_q(_ot, 0.5))}  "
          f"90%点 {_hm(_q(_ot, 0.9))}  "
          f"09:01より後 {_late:,}件 ({_late / len(_ot) * 100:.0f}%)")

# ── §18.44 の外挿との比較 ────────────────────────────────────────────
print("\n■ §18.44 の外挿との比較（N の母集団・5分足で -10.4bp @5分）")
_all5 = [v for _b in _agg for v in _agg[_b].get(300, [])]
if _all5:
    print(f"  実測（全帯・+300s）中央 {_q(_all5, 0.5):+.1f}bp / "
          f"平均 {sum(_all5) / len(_all5):+.1f}bp   （§18.44 は -10.4bp）")
    print("  ⚠ 母集団が違う（§18.44 は N の候補のみ）。--symbols で揃えること")

if a.out:
    with open(a.out, "w", newline="", encoding="utf-8-sig") as _f:
        _w = csv.DictWriter(_f, fieldnames=list(_rows[0]))
        _w.writeheader()
        _w.writerows(_rows)
    print(f"\n[保存] {os.path.abspath(a.out)}（{len(_rows):,}行）")
