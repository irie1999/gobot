r"""probe_yf_5m.py — yfinance を **素で** 叩いて、寄り(09:00-09:05)が本当に無いのか確かめる。

なぜ要るか (2026-08-12)
-----------------------
stock_5min は yfinance 製で、09:00 のバーが無い(or 出来高0・OHLC同値のダミー)。
ただしそれを確認したのは自作ラッパー(daytrade_data.load_intraday)越しだった。
normalize_minute_df / resample_to_5m / タイムゾーン処理のどこかで落としている
可能性が残るので、**yf.download をそのまま呼んで生の返り値を見る**。

見るところ:
  1. 5分足の生 index に 09:00 があるか (tz付きのまま)
  2. **1分足** に 09:00-09:04 があるか
     → あれば「Yahoo は寄りのデータを持っているが5分足の集計で落としている」
       = 1分足から5分足を組み直せば埋まる(直近7日だけだが検証にはなる)
  3. 日足の始値と、分足の最初の値が合うか

使い方
------
  python probe_yf_5m.py --symbols 4661.T,4208.T --date 2026-08-12
"""
from __future__ import annotations

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ap = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--symbols", required=True, help="カンマ区切り (例 4661.T,4208.T)")
ap.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
ap.add_argument("--rows", type=int, default=8, help="出す本数(既定8)")
args = ap.parse_args()

try:
    import pandas as pd
    import yfinance as yf
except Exception as e:
    sys.exit(f"[error] yfinance を読めません: {e}")

print(f"yfinance {getattr(yf, '__version__', '?')}")
tgt = pd.to_datetime(args.date).date()


def _probe(sym: str, interval: str, period: str):
    print(f"\n  --- {sym} interval={interval} period={period} ---")
    try:
        df = yf.download(sym, period=period, interval=interval,
                         auto_adjust=False, progress=False, prepost=False)
    except Exception as e:
        print(f"    取得失敗: {e}")
        return
    if df is None or df.empty:
        print("    空")
        return
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    print(f"    index tz = {df.index.tz}  / 全{len(df):,}本")
    idx = df.index
    # tz付きなら東京時間に直してから当日を抜く(ここを間違えると1本ズレる)
    try:
        loc = idx.tz_convert("Asia/Tokyo") if idx.tz is not None else idx
    except Exception:
        loc = idx
    d = df[[t.date() == tgt for t in loc]]
    lt = [t for t in loc if t.date() == tgt]
    if len(d) == 0:
        days = sorted({t.date() for t in loc})[-3:]
        print(f"    {tgt} のバーなし (直近: {', '.join(str(x) for x in days)})")
        return
    print(f"    {tgt}: {len(d)}本 / 先頭 {lt[0].strftime('%H:%M')} "
          f"/ 末尾 {lt[-1].strftime('%H:%M')}")
    for i in range(min(args.rows, len(d))):
        r = d.iloc[i]
        print(f"      {lt[i].strftime('%H:%M')} "
              f"O{float(r['Open']):>9,.1f} H{float(r['High']):>9,.1f} "
              f"L{float(r['Low']):>9,.1f} C{float(r['Close']):>9,.1f} "
              f"V{float(r['Volume']):>10,.0f}")
    w = [i for i, t in enumerate(lt) if "09:00" <= t.strftime("%H:%M") < "09:05"]
    if w:
        sub = d.iloc[w]
        print(f"    → 09:00-09:04 は {len(w)}本あり "
              f"高値{float(sub['High'].max()):,.1f} / 安値{float(sub['Low'].min()):,.1f}")
    else:
        print("    → 09:00-09:04 のバーなし")


for s in [x.strip() for x in args.symbols.split(",") if x.strip()]:
    sym = s if s.upper().endswith(".T") else f"{s}.T"
    print(f"\n{'=' * 70}\n■ {sym}  {args.date}\n{'=' * 70}")
    try:
        dd = yf.download(sym, period="10d", interval="1d",
                         auto_adjust=False, progress=False)
        if isinstance(dd.columns, pd.MultiIndex):
            dd.columns = dd.columns.get_level_values(0)
        dd.index = pd.to_datetime(dd.index).normalize()
        k = pd.Timestamp(tgt)
        if k in dd.index:
            r = dd.loc[k]
            print(f"  日足: 始{float(r['Open']):,.1f} 高{float(r['High']):,.1f} "
                  f"安{float(r['Low']):,.1f} 終{float(r['Close']):,.1f}")
    except Exception as e:
        print(f"  日足を取れません: {e}")
    _probe(sym, "5m", "30d")
    _probe(sym, "1m", "7d")      # 1分足は7日が上限。今日が入っていれば決定的

print("\n" + "─" * 70)
print("読み方:")
print("  * 5分足に09:00が無く、1分足に09:00-09:04がある")
print("      → Yahoo は寄りのデータを持っているが5分足の集計で落としている。")
print("        1分足から5分足を組み直せば埋まる(ただし1分足は直近7日のみ)。")
print("  * どちらにも無い")
print("      → Yahoo は寄りの板寄せを分足系列に入れていない。yfinance では埋まらない。")
print("  * index tz が None なのに時刻がズレている")
print("      → こちらの変換の問題。normalize_minute_df を見直す。")
