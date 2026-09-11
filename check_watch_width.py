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
_QTY = 100               # 100株固定
_BUDGET = 4_000_000.0    # 予算400万


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
    """その日の **日足の始値と終値** を symbol -> (open, close) で返す。

    ★ N は「寄りで売って引けで買い戻す」だけ(損切りも利確も無い / §18.55)。
      だから **この2つだけで損益が出る**。5分足も板も要らない。

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
                _sub = _df[_t] if len(_b) > 1 else _df
                _so, _sc = _sub["Open"], _sub["Close"]
            except Exception:                                    # noqa: BLE001
                continue
            for _ix, _v in _so.items():
                if str(getattr(_ix, "date", lambda: _ix)())[:10] != ymd:
                    continue
                try:
                    _o, _c = float(_v), float(_sc.loc[_ix])
                except (TypeError, ValueError, KeyError):
                    break
                if _o > 0 and _c > 0:
                    _out[_t.removesuffix(".T")] = (_o, _c)
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
    # ⛔⛔ **銘柄コードの書式を揃える**(2026-09-11)。n_signals は `.T` 付きで、
    #   k_paper と日足の引き当てキーは `.T` 無し。初版はここを合わせておらず、
    #   照合①(k_paper のキー)は通るのに集計②(n_signals のキー)が1件も
    #   引き当てられず、**全部 0件** と出ていた。
    for _r in _rows:
        _r["_sym"] = str(_r.get("symbol") or "").strip().removesuffix(".T")

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
    _board = {str(r.get("symbol") or "").strip().removesuffix(".T"):
              _num(r.get("open_p"))
              for r in _read(_kp) if _num(r.get("open_p")) > 0}
    # ⛔⛔ **n_signals に51位以下が入っているとは限らない**(2026-09-11)。
    #   n_paper.py --collect は `watched_n または shadow_n` の行しか書かず、
    #   --shadow-watch の既定は **0 = 残さない**。つまり既定では上位50件だけ。
    #   初版はここを確かめずに「候補全件が入っている」と決めつけ、12営業日
    #   すべて「51位以下 0件」という **無意味な結果**を出した。
    _nsh = sum(1 for r in _rows if _num(r.get("shadow_n")) > 0)
    _wmax = max((_num(r.get("rank_n")) for r in _rows
                 if _num(r.get("watched_n")) > 0), default=0)
    print(f"\n[{ymd}] n_signals {len(_rows)}件"
          f"(うち watched {len(_rows) - _nsh}件 / shadow {_nsh}件)"
          f" / 当日 板で読めた {len(_board)}件")
    if not _nsh and len(_rows) <= max(50, _wmax):
        print(f"  ⛔ **51位以下が CSV にありません**(--shadow-watch が 0 の日)。"
              f"この日は上位 {len(_rows)}件しか測れません")
    print("  日足の始値を取得中…", flush=True)
    _day = _yf_opens([r["_sym"] for r in _rows], ymd)
    if not _day:
        print("  ⛔ 日足の始値が取れませんでした")
        return None

    _diff, _miss = [], 0
    for _s, _bp in _board.items():
        _dp = _day.get(_s)
        if not _dp:
            _miss += 1
            continue
        _diff.append((abs(_dp[0] - _bp) / _bp * 1e4, _s, _bp, _dp[0]))
    if not _diff:
        print("  ⛔ 突き合わせられる銘柄がありません")
        return None
    _diff.sort()
    _n1 = sum(1 for d, *_ in _diff if d <= 1.0)
    _n5 = sum(1 for d, *_ in _diff if d <= 5.0)
    print(f"  [照合①始値] 板 vs 日足 {len(_diff)}件: "
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

    # ── ①' 終値も照合する。**損益の買い戻し側**はこれで代用するので ──────
    #   N の決済は引け成行(MOC)= 引けの板寄せ。日足の終値と一致するはず。
    #   実約定は fills_<日付>.csv に残っている(その日に約定があれば)。
    _fl = Path(f"fills_{ymd.replace('-', '')}.csv")
    _fd = ([(str(r.get("symbol") or "").strip().removesuffix(".T"),
             _num(r.get("exit(買戻)"))) for r in _read(_fl)]
           if _fl.exists() else [])
    _fd = [(s, v) for s, v in _fd if s and v > 0]
    if _fd:
        _cd = [(abs(_day[s][1] - v) / v * 1e4, s, v, _day[s][1])
               for s, v in _fd if s in _day]
        if _cd:
            _cd.sort()
            _c5 = sum(1 for d, *_ in _cd if d <= 5.0)
            print(f"  [照合②終値] 実買戻 vs 日足 {len(_cd)}件: "
                  f"5bp以内 {_c5}件 ({_c5 / len(_cd) * 100:.0f}%)"
                  f" / 最大 {_cd[-1][0]:.1f}bp")
            if _c5 < len(_cd):
                _d, _s, _r, _y = _cd[-1]
                print(f"     ⚠ {_s} 実買戻 {_r:,.1f} vs 日足終値 {_y:,.1f}"
                      f" ({_d:+.1f}bp)")
    else:
        print("  ⚠ 終値の照合はできません(その日の約定が無い)。"
              "**損益は日足の終値で代用した値**")

    # ── ② 帯別に「N の条件を満たした件数」を数える ───────────────────
    #   条件は k_open_confirm._mk_row と同じ:
    #     gap = (始値 − 前日終値)/前日終値×1e4 ≥ 100bp  かつ
    #     1,000 ≤ 始値 ≤ 6,000
    #   ★ 損益は **(始値 − 終値) × 100株**。N は途中で何もしないので
    #     これがそのまま1件の損益になる(滑りは無視)。
    _hit = []          # 合格した銘柄 (順位, 銘柄, 始値, 終値, 損益)
    _edges = [0] + sorted(bands) + [len(_rows)]
    _tab = []
    for _a, _b in zip(_edges, _edges[1:]):
        if _a >= len(_rows):
            break
        _seg = [r for r in _rows if _a < r["_rank"] <= min(_b, len(_rows))]
        if not _seg:
            continue
        _have = _px = 0
        _ok: list = []
        for _r in _seg:
            _s = _r["_sym"]
            _oc, _pc = _day.get(_s), _num(_r.get("prev_close"))
            if not _oc or _pc <= 0:
                continue
            _op, _cl = _oc
            _have += 1
            if not (_MIN_PX <= _op <= _MAX_PX):
                continue
            _px += 1
            if (_op - _pc) / _pc * 1e4 < _GAP_BP:
                continue
            _pl = (_op - _cl) * _QTY             # ショート: 始値で売り 終値で買戻
            _ok.append(_pl)
            _r["_op"], _r["_hit"] = _op, 1       # 帯ごとの投入額を出すため
            _hit.append((_r["_rank"], _s, _op, _cl, _pl))
        # (帯のラベル, 候補, 価格帯内, 合格した銘柄の損益, 同 投入額)
        _tab.append((f"{_a + 1}〜{min(_b, len(_rows))}", len(_seg), _px,
                     _ok, [r["_op"] * _QTY for r in _seg
                           if (r.get("_op") or 0) > 0 and r.get("_hit")]))
    print(f"\n  {'順位帯':<10}{'候補':>6}{'価格帯内':>9}{'+100bp':>8}"
          f"{'合格率':>8}{'損益':>12}{'bp/件':>9}")
    for _lb, _n, _p, _ok, _inv in _tab:
        _rate = (len(_ok) / _p * 100) if _p else 0.0
        if not _ok:
            print(f"  {_lb:<10}{_n:>6}{_p:>9}{0:>8}{_rate:>7.1f}%"
                  f"{'—':>12}{'—':>9}")
            continue
        _sum, _iv = sum(_ok), sum(_inv)
        _bp = (_sum / _iv * 1e4) if _iv else 0.0
        print(f"  {_lb:<10}{_n:>6}{_p:>9}{len(_ok):>8}{_rate:>7.1f}%"
              f"{_sum:>+12,.0f}{_bp:>+8.1f}")
    _t50 = [h for h in _hit if h[0] <= 50]
    _ext = [h for h in _hit if h[0] > 50]
    print(f"\n  ★ 上位50件(いまの運用) {len(_t50)}件 {sum(h[4] for h in _t50):+,.0f}円"
          f" → 51位以下に **あと {len(_ext)}件 {sum(h[4] for h in _ext):+,.0f}円**")

    # ── ③ 予算400万・流動性降順で埋めたら (実運用に近い形) ────────────
    #   ⛔ §18.10: 「全部買えるなら得」と「予算内でどれを買うか」は別問題。
    #     N は100株固定なので 1件あたり 始値×100 を使う。
    print(f"\n  {'watch':<10}{'建てた':>7}{'投入':>12}{'損益':>12}"
          f"{'予算で落ちた':>13}")
    for _w in [50] + [b for b in sorted(bands) if b > 50] + [len(_rows)]:
        if _w > len(_rows) and _w != len(_rows):
            continue
        _use = 0.0
        _got: list = []
        _drop = 0
        for _rk, _s, _op, _cl, _pl in sorted(_hit):
            if _rk > _w:
                continue
            _need = _op * _QTY
            if _use + _need > _BUDGET:
                _drop += 1
                continue
            _use += _need
            _got.append(_pl)
        _lbl = "無制限" if _w >= len(_rows) else str(_w)
        print(f"  {_lbl:<10}{len(_got):>7}{_use:>12,.0f}"
              f"{sum(_got):>+12,.0f}{_drop:>13}")
    return {"date": ymd, "top50": len(_t50), "extra": len(_ext),
            "cand": len(_rows), "pnl50": sum(h[4] for h in _t50),
            "pnlx": sum(h[4] for h in _ext), "shadow": _nsh}


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
        print(f"  {'日付':<12}{'n_signals':>10}{'shadow':>8}"
              f"{'上位50':>8}{'51位以下':>10}{'損益(51位以下)':>16}")
        for _r in _all:
            print(f"  {_r['date']:<12}{_r['cand']:>10}{_r['shadow']:>8}"
                  f"{_r['top50']:>8}{_r['extra']:>10}{_r['pnlx']:>+16,.0f}")
        _t = sum(r["top50"] for r in _all)
        _e = sum(r["extra"] for r in _all)
        _px = sum(r["pnlx"] for r in _all)
        print(f"  {'合計':<12}{sum(r['cand'] for r in _all):>10}"
              f"{sum(r['shadow'] for r in _all):>8}{_t:>8}{_e:>10}"
              f"{_px:>+16,.0f}")
        # ⛔ shadow が1件も無いなら 51位以下は **測れていない**。
        #   0件を「いなかった」と読ませないこと。
        if not any(r["shadow"] for r in _all):
            print(f"\n  ⛔ **どの日も shadow が 0件**。n_signals に51位以下が"
                  f"入っていないので、『51位以下 {_e}件』は")
            print(f"     **測れていないだけ**で『いなかった』ではない。")
            print(f"     明日から collect に **--shadow-watch 150** を付けると"
                  f"51〜150位が CSV に残り、測れるようになる")
            print(f"     (発注対象は1件も変わらない。shadow_n は watched_n の"
                  f"外側だけに立ち、k_open_confirm は watched_n で絞る)")
        else:
            print(f"\n  ★ {len(_all)}営業日で 上位50件が {_t}件、"
                  f"51位以下に {_e}件 / {_px:+,.0f}円")
            if _t:
                print(f"     watch を広げれば件数は **{_e / _t:.1f}倍**")
            print("  ⚠ 件数だけで決めないこと。51位以下は流動性が低く、"
                  "§18.70⑥ では枠が埋まる日に大きく負けている。")
            print("     そして広げるぶん **発注が遅れる**"
                  "(切替に数秒 = §18.44 で 0.26bp/秒)")
    print("=" * 74)


if __name__ == "__main__":
    main()
