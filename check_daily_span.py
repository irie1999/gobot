#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日足が実際に何年分あるかを数える（調査用・発注しない）。

★ なぜ要るのか（2026-09-07）

  「`.\\nlong 7000` が最長？」→「コード上の上限は無い」と答えたが、
  **では実際に何年分あるのか**は誰も測っていなかった。推測で語らない
  （§18.38「速度も推測で潰さない」と同じ話）。

  2つは別の問いなので、両方出す:

  | 問い | どう測るか |
  |---|---|
  | ① いま手元にあるのは何年分か | `.rsi2_cache/*.pkl` の最古バーを数える |
  | ② yfinance が出せるのは何年分か | 数銘柄だけ長期リクエストして実測（`--probe`） |

  ⛔ ①だけ見て「5年しか無い」と結論しないこと。キャッシュは
     **これまでに要求した窓ぶん**しか無いので、`.\\nlong 7000` を
     一度も回していなければ短くて当然。②が本当の天井。

使い方
    python check_daily_span.py                 # ① キャッシュの実測
    python check_daily_span.py --probe 20      # ①+② 20銘柄を長期リクエスト
    python check_daily_span.py --probe 20 --probe-years 30

⚠ `--probe` は yfinance を叩く（1銘柄あたり1リクエスト）。
   **キャッシュは書き換えない**（`yf.Ticker().history` を直接呼ぶだけ）。
   場中に大量に叩くとレート制限に当たるので、20〜50件で十分。
"""
from __future__ import annotations

import argparse
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--probe", type=int, default=0,
                help="yfinance に長期リクエストして実際の配信開始日を測る銘柄数（0=しない）")
ap.add_argument("--probe-years", type=float, default=30.0,
                help="--probe で遡る年数（既定30年）")
ap.add_argument("--cache-dir", default="",
                help="日足キャッシュの場所を明示（既定: GOBOT_CACHE_DIR → .rsi2_cache）")
a = ap.parse_args()

TODAY = date.today()
# ★ .\nlong で指定しうる日数。到達年と、そこまで届く銘柄が何%あるかを出す
MARKS = [(2000, "5.5年"), (4200, "11.5年"), (7000, "19年"),
         (9000, "24.6年"), (11000, "30年")]


def _cache_dir() -> Path:
    """日足キャッシュの場所。

    ⛔ 先に **環境変数を見る**。backtest_limit_entry の import は yfinance を
      要求するので、入っていない環境（このコンテナなど）では失敗し、
      `GOBOT_CACHE_DIR` を渡していても既定に落ちて黙って別の場所を見る。
    """
    if a.cache_dir:
        return Path(a.cache_dir)
    import os
    _env = os.environ.get("GOBOT_CACHE_DIR", "").strip()
    if _env:
        return Path(_env)
    try:
        import backtest_limit_entry as ble
        return Path(ble._CACHE_DIR)
    except Exception:
        return Path(".rsi2_cache")


def _oldest(p: Path):
    """pkl の最古バー日付。読めなければ None。"""
    try:
        with open(p, "rb") as f:
            df = pickle.load(f)
        if df is None or not len(df):
            return None, 0
        i0 = df.index[0]
        return (i0.date() if hasattr(i0, "date") else i0), len(df)
    except Exception:
        return None, 0


def _pct(v, n):
    return f"{v / n * 100:5.1f}%" if n else "  n/a"


def main() -> int:
    cdir = _cache_dir()
    print("=" * 74)
    print(f"■ ① いま手元にある日足  {cdir}")
    print("=" * 74)
    if not cdir.exists():
        print(f"  ⛔ {cdir} がありません。")
        print("     Windows 側で実行してください（このコンテナにはキャッシュが無い）。")
    else:
        files = sorted(cdir.glob("*.pkl"))
        spans, bad = [], 0
        for p in files:
            od, n = _oldest(p)
            if od is None:
                bad += 1
                continue
            spans.append(((TODAY - od).days, od, p.stem, n))
        spans.sort()
        n = len(spans)
        if not n:
            print(f"  ⛔ 読めた銘柄が 0 件でした（ファイル {len(files):,} / 読めず {bad}）")
        else:
            print(f"  銘柄 {n:,} 件（読めず {bad}）")
            print()
            print(f"  {'':>10} {'遡れる日数':>10} {'最古バー':>12} {'年':>6}")
            for lab, q in (("最短", 0.0), ("10%点", 0.10), ("中央", 0.50),
                           ("90%点", 0.90), ("最長", 1.0)):
                d, od, sym, _ = spans[min(n - 1, int((n - 1) * q))]
                print(f"  {lab:>10} {d:>10,} {str(od):>12} {d / 365.25:>6.1f}")
            print()
            print("  ★ .\\nlong の日数まで **届いている銘柄の割合**")
            print(f"  {'指定':>7} {'到達':>8} {'届く銘柄':>10} {'割合':>7}")
            for days, yrs in MARKS:
                k = sum(1 for d, *_ in spans if d >= days)
                tgt = TODAY - timedelta(days=days)
                print(f"  {days:>7,} {yrs:>8} {k:>10,} {_pct(k, n):>7}"
                      f"   （{tgt} まで）")
            print()
            print("  ⛔ ここが短くても『データが無い』ではありません。キャッシュは")
            print("     **これまでに要求した窓ぶん**しか持たないので、`.\\nlong 7000` を")
            print("     一度も回していなければ短くて当然です。天井は②で測ります。")

    if a.probe <= 0:
        print()
        print("  ▶ 本当の天井（yfinance が出せる年数）を測るには:")
        print("       python check_daily_span.py --probe 20")
        return 0

    # ── ② yfinance の実測 ──
    print()
    print("=" * 74)
    print(f"■ ② yfinance が実際に出せる年数（{a.probe}銘柄 / {a.probe_years:.0f}年 要求）")
    print("=" * 74)
    try:
        import yfinance as yf
    except Exception as e:                                        # noqa: BLE001
        print(f"  ⛔ yfinance を import できません: {e}")
        return 1
    try:
        from daytrade_data import available_local_symbols as _als
        import newgap_core as _ng
        syms = sorted({_ng._newgap_yf(s) for s in _als()})
    except Exception as e:                                        # noqa: BLE001
        print(f"  ⛔ 銘柄リストを作れません: {e}")
        return 1
    if not syms:
        print("  ⛔ 銘柄が 0 件です（5分足フォルダが見えていない）")
        return 1
    # 端に寄らないよう等間隔で抜く（コード順=業種順に近いので先頭だけだと偏る）
    step = max(1, len(syms) // a.probe)
    pick = syms[::step][:a.probe]
    start = (TODAY - timedelta(days=int(a.probe_years * 365.25))).strftime("%Y-%m-%d")
    end = (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    got, fail = [], []
    print(f"  {start} 〜 を要求します…")
    for s in pick:
        try:
            raw = yf.Ticker(s).history(start=start, end=end, interval="1d",
                                       auto_adjust=False, actions=False)
            if raw is None or raw.empty:
                fail.append((s, "空"))
                continue
            i0 = raw.index[0]
            od = i0.date() if hasattr(i0, "date") else i0
            got.append(((TODAY - od).days, od, s, len(raw)))
        except Exception as e:                                    # noqa: BLE001
            fail.append((s, type(e).__name__))
    got.sort()
    if not got:
        print(f"  ⛔ 1件も取れませんでした（失敗 {len(fail)}）")
        for s, why in fail[:5]:
            print(f"     {s}: {why}")
        return 1
    n = len(got)
    print(f"  取れた {n} / 要求 {len(pick)}（失敗 {len(fail)}）")
    print()
    print(f"  {'':>10} {'遡れる日数':>10} {'最古バー':>12} {'年':>6}  銘柄")
    for lab, q in (("最短", 0.0), ("10%点", 0.10), ("中央", 0.50),
                   ("90%点", 0.90), ("最長", 1.0)):
        d, od, sym, _ = got[min(n - 1, int((n - 1) * q))]
        print(f"  {lab:>10} {d:>10,} {str(od):>12} {d / 365.25:>6.1f}  {sym}")
    print()
    print("  ★ .\\nlong の日数まで **配信がある銘柄の割合**（これが本当の天井）")
    print(f"  {'指定':>7} {'到達':>8} {'届く銘柄':>10} {'割合':>7}")
    for days, yrs in MARKS:
        k = sum(1 for d, *_ in got if d >= days)
        print(f"  {days:>7,} {yrs:>8} {k:>10,} {_pct(k, n):>7}")
    print()
    print("  ⚠ 割合が下がる = **その窓では銘柄が減る**。エラーは出ません。")
    print("     減った銘柄は『当時 上場していなかった』側なので、窓を伸ばすほど")
    print("     母集団が「長く生き残った会社」に寄ります（生存バイアス）。")
    print("  ⚠ そして §18.55 の執行コスト(1円=4.4bp)は **現行の呼値表**です。")
    print("     2014/2015 の細分化より前は呼値が粗く、当時の実コストは数倍。")
    print("     窓の古い部分ほどネットのエッジを過大に見せます。")
    if fail:
        print()
        print(f"  取れなかった {len(fail)} 件（先頭5）:")
        for s, why in fail[:5]:
            print(f"     {s}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
