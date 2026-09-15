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


# ⛔⛔ **呼値の表で決め打ちしない**(2026-09-15 に間違えた)。
#   sweep_oos_budget._tick は「3,000超5,000以下 = 5円」の通常銘柄の表だが、
#   TOPIX100 などは呼値が細かい。実データがそれを示している:
#       4768 指値 3,679 / 高値 3,690  → 差 11円(5の倍数でない)
#       9602 始値 1,672.5             → 0.5円 刻み
#   表を当てると「2ティックしか余裕がない」と誤警告する。
#   → ティック数を推定せず、**余裕を 円 と bp でそのまま出す**。
_THIN_BP = 10.0          # これ以下なら「行列で刺さらないかも」と注意する

ap = argparse.ArgumentParser(
    description="未約定の注文がその後 指値まで戻ったかを日足で調べる")
ap.add_argument("--date", default="", help="YYYY-MM-DD（既定 今日）")
ap.add_argument("--symbols", default="",
                help="カンマ区切り。省略すると k_paper_<日付>.csv の合格を全部")
ap.add_argument("--limit", default="",
                help="指値をカンマ区切りで明示（省略時は k_paper の始値）")
ap.add_argument("--bars", default="auto", choices=["auto", "1", "5", "0"],
                help="分足で時刻まで詰める（auto=1分→5分の順に探す / 0=日足だけ）")
ap.add_argument("--until", default="09:10",
                help="この時刻までに戻れば約定だったとみなす（= --poll-until）")
ap.add_argument("--cancel", default="",
                help="実際に取消した時刻 HH:MM:SS（説明に使うだけ）")
ap.add_argument("--selftest", action="store_true",
                help="合成バーで判定ロジックだけ検算して終わる")
a = ap.parse_args()

_d = a.date or f"{_dt.date.today():%Y-%m-%d}"
_ymd = _d.replace("-", "")


# ══════════════════════════════════════════════════════════════════════
# 分足での判定（ここが本命。日足より1段 強い答えが出る）
# ══════════════════════════════════════════════════════════════════════
# ⛔⛔ **寄りバーで「約定した」とは言えない**（analyze_fill_1m の落とし穴②）。
#   板寄せの約定（＝始値そのもの）が必ず入るので、寄りバーの高値は定義上
#   いつも 始値以上 = 指値以上。→ 判定は **次のバー以降**で行う。
#
# ⛔ ただし **片側だけは情報がある**（2026-09-15 に取り違えた）。
#     寄りバーの高値 == 始値 → 板寄せ以外に指値以上の約定が無い → 戻っていない
#     寄りバーの高値 >  始値 → **指値以上の約定が実際にあった**。時刻は不明
#   後者を「戻っていない」と書くと嘘になる。実例 2026-09-15 の未約定4件は
#   全部これで、5分足の窓内・窓後はどちらも指値未満なのに **日足高値は指値超**
#   （＝その高値は寄りバーに入っていた）。→ 三分類にする。
#
#   5分足だと寄りバーが 09:00〜09:05 を丸ごと飲むので、取消(09:03:10)〜09:05 が
#   そこに埋もれる。**この区間を分離できるのは1分足だけ。**

def _bar_verdict(post, lim: float, t_until, conv_end: bool, minute: int,
                 open_hi=None):
    """寄りバーを除いたバー列から『--until までに指値へ戻ったか』を決める。

    post     : [(label(datetime.time), high(float)), ...] 時刻昇順
    conv_end : ラベルが **バーの終端**なら True、**開始**なら False
    open_hi  : 寄りバーの高値（分かるなら）。指値より **厳密に上**なら
               その中に指値以上の約定があったので「判定不能」になる
    返り値   : (判定, 窓内の最高値 or None, 最初に触れた窓の終端 or None,
                窓より後の最高値 or None)
      fill     … --until までに戻った → N3 なら約定していた
      unknown  … 戻っていないが **寄りバーの中に指値以上の約定がある**
      no_late  … 戻ったのは --until より後
      no_never … 一日中 戻っていない
    """
    _eps = 1e-9
    _in_hi = _af_hi = None
    _hit = None
    for _t, _h in post:
        # そのバーが覆う区間の**終端**。ここが --until 以内なら「窓の中」。
        if conv_end:
            _we = _t
        else:
            _we = (_dt.datetime.combine(_dt.date(2000, 1, 1), _t)
                   + _dt.timedelta(minutes=minute)).time()
        if _we <= t_until:
            _in_hi = _h if _in_hi is None else max(_in_hi, _h)
            if _hit is None and _h >= lim - _eps:
                _hit = _we
        else:
            _af_hi = _h if _af_hi is None else max(_af_hi, _h)
    if _hit is not None:
        return "fill", _in_hi, _hit, _af_hi
    # ⛔ 「戻っていない」と言う前に **寄りバーの中身**を見る（上のコメント）
    if open_hi is not None and open_hi > lim + _eps:
        return "unknown", _in_hi, None, _af_hi
    if _af_hi is not None and _af_hi >= lim - _eps:
        return "no_late", _in_hi, None, _af_hi
    return "no_never", _in_hi, None, _af_hi


if a.selftest:
    _T = _dt.time
    _U = _T(9, 10)
    _cases = [
        # (name, post, lim, conv_end, minute, open_hi, expect)
        #   open_hi=指値 は「寄りバーは板寄せだけ」＝情報なしの意味
        ("5分/終端ラベル: 09:10バーで戻る",
         [(_T(9, 10), 105.0), (_T(9, 15), 99.0)], 104.0, True, 5, 104.0,
         "fill"),
        ("5分/終端ラベル: 戻るのは09:15",
         [(_T(9, 10), 100.0), (_T(9, 15), 108.0)], 104.0, True, 5, 104.0,
         "no_late"),
        ("5分/終端ラベル: 一日中 届かない",
         [(_T(9, 10), 100.0), (_T(9, 15), 101.0)], 104.0, True, 5, 104.0,
         "no_never"),
        ("5分/開始ラベル: 09:05バー(=09:05-09:10)で戻る",
         [(_T(9, 5), 105.0)], 104.0, False, 5, 104.0, "fill"),
        ("5分/開始ラベル: 09:10バーは窓の外",
         [(_T(9, 5), 100.0), (_T(9, 10), 105.0)], 104.0, False, 5, 104.0,
         "no_late"),
        ("1分/開始ラベル: 09:07に戻る",
         [(_T(9, m), 100.0) for m in range(1, 7)]
         + [(_T(9, 7), 105.0)], 104.0, False, 1, 104.0, "fill"),
        ("1分/開始ラベル: 09:11は窓の外(終端09:12)",
         [(_T(9, 11), 105.0)], 104.0, False, 1, 104.0, "no_late"),
        ("境界: ちょうど指値に触れる",
         [(_T(9, 10), 104.0)], 104.0, True, 5, 104.0, "fill"),
        # ★ 2026-09-15 の実例。窓内・窓後は指値未満なのに寄りバーが指値超
        ("寄りバーに指値超の約定 → 判定不能",
         [(_T(9, 10), 100.0), (_T(9, 15), 101.0)], 104.0, True, 5, 110.0,
         "unknown"),
        ("寄りバーが指値ちょうど(板寄せだけ) → 戻っていない",
         [(_T(9, 10), 100.0)], 104.0, True, 5, 104.0, "no_never"),
        ("判定不能より『窓内で戻った』が優先",
         [(_T(9, 10), 105.0)], 104.0, True, 5, 110.0, "fill"),
        ("open_hi 不明(None)なら従来どおり",
         [(_T(9, 10), 100.0)], 104.0, True, 5, None, "no_never"),
    ]
    _ng = 0
    print("■ 判定ロジックの検算（合成バー）")
    for _n, _p, _l, _ce, _mi, _oh, _ex in _cases:
        _got = _bar_verdict(_p, _l, _U, _ce, _mi, _oh)[0]
        _ok = _got == _ex
        _ng += 0 if _ok else 1
        print(f"  {'✅' if _ok else '⛔'} {_n:42} → {_got} (期待 {_ex})")
    print(f"\n  {len(_cases) - _ng}/{len(_cases)} 合格")
    sys.exit(0 if _ng == 0 else 1)

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
        # ⛔ **日足の高値は寄り付きの分を含む**(2026-09-15 に気づいた)。
        #   高値が 09:00:0x に付いていたら「戻った」ではなく「最初から
        #   高かった」だけ。N の注文は寄りの数秒後に出すので、その高値を
        #   拾えたとは限らない。→ 日足では **区別できない**。
        # ⛔ さらに「触れた ≠ 約定した」。板の行列(価格・時間優先)があるので、
        #   高値が指値ちょうど〜1ティック上なら、そこで出来た株数が少なければ
        #   自分の売り指値は刺さらない。
        _v = "⚠ どこかで戻っている（**時刻不明・寄り付きを含む**）"
        if _dbp <= _THIN_BP:
            _v += (f" / ⛔ 余裕 {_h - _lim0:,.1f}円 だけ"
                   f" = 行列で刺さらないかも")
        _maybe += 1
    print(f"  {_s:8}{_lim0:>12,.1f}{_h:>11,.1f}{_dbp:>+10.1f}bp"
          f"{_l:>11,.1f}{_c:>10,.1f}  {_v}")

print(f"\n  ✅ 戻っていない（待っても無駄だった）  **{_never}件**")
print(f"  ⚠ 戻っている（時刻は不明）            {_maybe}件")
if _err:
    print(f"  ⛔ 調べられなかった                   {_err}件")
print(f"\n  ⛔ **これは片側だけの検査です。** 確定できるのは「待っても無駄だった」だけ。")
print(f"     「戻っている」側は、次の3つを日足では区別できません:")
print(f"       ① **高値が寄り付きの分かもしれない** — 日足の高値は 09:00 を含む。"
      f"\n          N の注文は寄りの数秒後なので、その高値は拾えていない可能性")
print(f"       ② **時刻が分からない** — 09:10 までとは限らない（14:00 かも）")
print(f"       ③ **触れた ≠ 約定した** — 板の行列があるので、高値が指値ちょうど"
      f"\n          〜1ティック上なら自分の売り指値は刺さらないことがある")

# ══════════════════════════════════════════════════════════════════════
# 分足があるなら ①② は詰められる
# ══════════════════════════════════════════════════════════════════════
if a.bars == "0":
    print(f"\n  ▶ 分足で時刻まで詰めるなら --bars auto")
    sys.exit(0)

try:
    import tenkan_sim as _ts                                    # noqa: E402
    _D5, _D1 = _ts.find_minute_dirs()
except Exception as _te:                                        # noqa: BLE001
    print(f"\n  ⚠ 分足モジュールを読めません: {_te}")
    sys.exit(0)

_want = [1, 5] if a.bars == "auto" else [int(a.bars)]
_avail = [m for m in _want if (_D1 if m == 1 else _D5) is not None]
if not _avail:
    print(f"\n  ⚠ 分足フォルダが見つかりません"
          f"（5分足 {_D5} / 1分足 {_D1}）。"
          f"\n     ここはコンテナ等で分足が無い環境です。"
          f"Windows の実機なら自動で見つかります")
    sys.exit(0)

_hh, _mm = (int(x) for x in a.until.split(":")[:2])
_UNTIL = _dt.time(_hh, _mm)
_dobj = _dt.date(*(int(x) for x in _d.split("-")))

# ── その日のバーを読む ────────────────────────────────────────────
_MIN = 0
_day: dict = {}
for _m in _avail:
    _got = {}
    for _s in _syms:
        try:
            _df = _ts._bars_one(f"{_s}.T", _m)
        except Exception:                                       # noqa: BLE001
            _df = None
        if _df is None:
            continue
        try:
            _b = _df[_df.index.date == _dobj]
        except Exception:                                       # noqa: BLE001
            continue
        if len(_b) < 2:
            continue
        # ★ 寄りバー = その日の最初の **出来高 > 0** のバー
        if "volume" in _b.columns:
            _nz = _b.index[_b["volume"].fillna(0) > 0]
            if len(_nz):
                _b = _b.loc[_nz[0]:]
        if len(_b) >= 2:
            _got[_s] = _b
    if len(_got) >= max(1, len(_syms) // 2):
        _MIN, _day = _m, _got
        break
    if _got and not _day:
        _MIN, _day = _m, _got

if not _day:
    print(f"\n  ⚠ {_d} の分足が1銘柄も入っていません"
          f"（J-Quants は当日ぶんが翌営業日以降になります）")
    sys.exit(0)

# ── ラベルが「バーの開始」か「終端」かを **データから** 決める ──────────
# ⛔ 決め打ちしない。CLAUDE.md §18.50 に「5分足の先頭バーが 09:05 で 90%」と
#   あり、§18.32 の「09:00 でない日が 13%」と食い違ったまま未解決だった。
#   ほぼ全銘柄が 09:00 に寄る以上、先頭が 09:05 なら **終端ラベル**
#   (09:00-09:05 のバーを 09:05 と呼ぶ) でなければ辻褄が合わない。
from collections import Counter as _Cnt                         # noqa: E402
_first = _Cnt(str(_b.index[0].time())[:5] for _b in _day.values())
_mode, _mn = _first.most_common(1)[0]
_END = _mode == f"09:{_MIN:02d}"        # 先頭が 09:05(5分) / 09:01(1分) なら終端
if not _END and _mode != "09:00":
    print(f"\n  ⚠ 先頭バーの最頻値が {_mode} で、09:00 とも "
          f"09:{_MIN:02d} とも違います。**開始ラベル**として扱います")

print(f"\n{'=' * 74}")
print(f"■ {_d} — {_MIN}分足で『{a.until} までに指値へ戻ったか』を見る")
print(f"{'=' * 74}")
print(f"  読めた {len(_day)}/{len(_syms)}銘柄 / 先頭バーの最頻値 {_mode}"
      f"（{_mn}銘柄）→ ラベルは **バーの{'終端' if _END else '開始'}**")
if a.cancel:
    print(f"  実際の取消 {a.cancel}"
          + (f" / ⛔ {_MIN}分足では "
             f"{a.cancel[:5]}〜09:{(int(a.cancel[3:5]) // _MIN + 1) * _MIN:02d} "
             f"が寄りバーに埋もれて見えません" if _MIN == 5 else ""))
print(f"\n  {'銘柄':8}{'指値':>10}{'寄りバー高値':>13}{'窓内 高値':>11}"
      f"{'窓内−指値':>11}{'戻った':>8}{'窓後 高値':>11}  判定")
print(f"  （「戻った」は**遅くともこの時刻まで**に指値へ届いたという意味。"
      f"バー1本ぶんの幅がある）")

_f = _un = _nl = _nn = _nb = 0
for _s in _syms:
    _lim0 = _px[_s]
    _b = _day.get(_s)
    if _b is None:
        print(f"  {_s:8}  ⚠ {_MIN}分足なし（または2本未満）")
        _nb += 1
        continue
    # ⛔ 寄りバーは **判定には使わない**（板寄せの約定が入るので高値が必ず
    #    指値以上）。ただし「指値より厳密に上」なら板寄せ以外の約定が
    #    あった証拠になるので、_bar_verdict に渡して判定不能を出させる。
    _ohi = float(_b["high"].iloc[0])
    _post = [(t.time(), float(h))
             for t, h in zip(_b.index[1:], _b["high"].iloc[1:])]
    _vd, _ih, _ht, _ah = _bar_verdict(_post, _lim0, _UNTIL, _END, _MIN, _ohi)
    _dd = f"{(_ih - _lim0) / _lim0 * 1e4:+.1f}bp" if _ih is not None else "—"
    if _vd == "fill":
        _msg = f"✅ **{a.until} までに戻っている → N3 なら約定していた**"
        _f += 1
    elif _vd == "unknown":
        _msg = (f"⚠ **判定不能** — 寄りバーに指値超の約定 "
                f"(+{_ohi - _lim0:,.1f}円) がある。時刻が分からない")
        _un += 1
    elif _vd == "no_late":
        _msg = f"⛔ 戻ったのは {a.until} より後 → **待っても約定しなかった**"
        _nl += 1
    else:
        _msg = f"⛔ 一日中 戻っていない → **待っても約定しなかった**"
        _nn += 1
    print(f"  {_s:8}{_lim0:>10,.1f}{_ohi:>13,.1f}"
          f"{(f'{_ih:,.1f}' if _ih is not None else '—'):>11}{_dd:>11}"
          f"{(str(_ht)[:5] if _ht else '—'):>8}"
          f"{(f'{_ah:,.1f}' if _ah is not None else '—'):>11}  {_msg}")

print(f"\n  ✅ N3 なら約定していた      **{_f}件**")
print(f"  ⚠ 判定不能                 **{_un}件**"
      f"（寄りバーの中に指値以上の約定がある。時刻は{_MIN}分足では出ない）")
print(f"  ⛔ 待っても約定しなかった    {_nl + _nn}件"
      f"（{a.until} より後に戻った {_nl} / 一日中戻らず {_nn}）")
if _nb:
    print(f"  ⚠ 分足が無くて調べられない  {_nb}件")

print(f"\n  ⚠ 読み方の注意:")
if _un:
    print(f"    ① **判定不能 {_un}件 の読み方**（ここがいちばん大事）")
    print(f"       寄りバーの中に指値以上の約定がある。しかし注文は生きていたのに"
          f"\n       約定しなかった。つまりその約定は次のどちらかで起きた:")
    print(f"         (a) 発注が板に乗る前の数秒（寄り直後）")
    print(f"         (b) **取消したあと**（そこなら N3 では約定していた）")
    if _MIN == 5:
        print(f"       ⛔ 5分足では寄りバーが 09:00〜09:05 を丸ごと飲むので"
              f"(a)と(b)を分けられません。"
              f"\n          **1分足なら分かれます**（寄りの1本目だけ落とせばよい）")
print(f"    ② **触れた ≠ 約定した**。板の行列(価格・時間優先)があるので、"
      f"\n       高値が指値ちょうどなら自分の売り指値は刺さらないことがあります")
print(f"    ③ これは **1日ぶん**です。約定率の判定は 2年で:"
      f"\n         python analyze_fill_1m.py --days 760 --workers 8")
