"""debug_lss_bar.py — lss1トレードの5分足 first-touch を1本ずつダンプして検証する。

用途: 「レポートで損切りだが、寄りの跳ねは約定前のはず」等の疑義を、実際の5分足で確認。
  約定バー(最初に安値<=トリガー)を特定し、その"次バー以降"で損切り(高値>=損切)/
  利確(安値<=利確)のどちらが先にタッチしたかを表示する(=short_exit_5m と同じ判定)。

使い方(レポートのハードオフ行の数値を渡す):
  python debug_lss_bar.py --symbol 2674 --date 2026-07-22 \
      --trigger 2862 --stop 2870 --target 2770 --source local

  trigger=約定値(逆指値/指値), stop=損切り(上), target=目標(下)。
"""
from __future__ import annotations
import argparse

import pandas as pd

ap = argparse.ArgumentParser(description="lss1トレードの5分足 first-touch を検証")
ap.add_argument("--symbol", required=True)
ap.add_argument("--date", required=True, help="約定日 YYYY-MM-DD")
ap.add_argument("--trigger", type=float, required=True, help="約定値(逆指値売りトリガー)")
ap.add_argument("--stop", type=float, required=True, help="損切り(上)")
ap.add_argument("--target", type=float, required=True, help="目標(下)")
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--days", type=int, default=10)
args = ap.parse_args()

from daytrade_data import load_intraday, split_by_day
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m, short_pnl

m5 = load_intraday(args.symbol, days=args.days, source=args.source)
if m5 is None or m5.empty:
    raise SystemExit(f"[error] {args.symbol} の5分足が取得できません(source={args.source})")
by_day = split_by_day(m5)
_want = pd.Timestamp(args.date).date()
db = by_day.get(_want)
if db is None or db.empty:
    raise SystemExit(f"[error] {args.date} の5分足がありません。取れた日: "
                     f"{sorted(str(d) for d in list(by_day)[-8:])}")

trig, stop, tgt = args.trigger, args.stop, args.target
lows = db["low"].to_numpy(dtype=float)
highs = db["high"].to_numpy(dtype=float)

# 日足OHLC(yfinance)を先に取得。約定判定は5分足で行うが、5分足は寄り(09:00-09:05)の
# バーが欠落することがある。日足の安値/高値は寄りの一瞬も含むので、「本当にトリガーへ
# 達したか」の最終証拠になる(5分足で未達でも日足安値≤トリガーなら現実は約定していた)。
_day_open = _day_low = _day_high = _day_close = None
try:
    import backtest_limit_entry as _ble
    _sym_t = args.symbol if args.symbol.upper().endswith(".T") else args.symbol + ".T"
    _ddf = _ble.fetch(_sym_t, 30)
    _drow = _ddf.loc[_ddf.index.normalize() == pd.Timestamp(_want)]
    if len(_drow):
        _day_open = float(_drow["open"].iloc[0])
        _day_low = float(_drow["low"].iloc[0])
        _day_high = float(_drow["high"].iloc[0])
        _day_close = float(_drow["close"].iloc[0])
except Exception as _e:
    print(f"[warn] 日足OHLC取得失敗: {_e}")

# 約定バー = 最初に安値<=トリガー
ei = None
for j in range(len(lows)):
    if lows[j] <= trig:
        ei = j
        break

print(f"\n=== {args.symbol} {args.date} lss(逆指値売り) ===")
print(f"トリガー(約定)={trig:,.0f} / 損切り(上)={stop:,.0f} / 目標(下)={tgt:,.0f}")
print(f"5分足 {len(db)}本\n")
print(f"{'#':>3} {'時刻':>8} {'始値':>8} {'高値':>8} {'安値':>8} {'終値':>8}  印")
for j in range(len(db)):
    t = db.index[j]
    ts = t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)
    mark = []
    if ei is not None and j == ei:
        mark.append("◀約定")
    if ei is not None and j > ei and highs[j] >= stop:
        mark.append("↑損切ライン到達")
    if ei is not None and j > ei and lows[j] <= tgt:
        mark.append("↓目標到達")
    o = float(db["open"].iloc[j]); h = highs[j]; l = lows[j]; c = float(db["close"].iloc[j])
    print(f"{j:>3} {ts:>8} {o:>8,.0f} {h:>8,.0f} {l:>8,.0f} {c:>8,.0f}  {' '.join(mark)}")

print()
if ei is None:
    _first_ts = db.index[0]
    _first_lbl = _first_ts.strftime('%H:%M') if hasattr(_first_ts, 'strftime') else str(_first_ts)
    print("判定(5分足): 約定せず(5分足の安値がトリガーに一度も達しない)= no_entry")
    print(f"  5分足の最安値 = {lows.min():,.0f} (トリガー {trig:,.0f})")
    print(f"  5分足の先頭バー時刻 = {_first_lbl}  ← 09:05なら 09:00-09:05 が欠落している")
    if _day_low is not None:
        print(f"\n日足OHLC(yfinance): 始値 {_day_open:,.0f} / 高値 {_day_high:,.0f} / 安値 {_day_low:,.0f}")
        if _day_low <= trig:
            print(f"  ⚠ 日足安値 {_day_low:,.0f} ≤ トリガー {trig:,.0f}")
            print("    → 現実は寄り(欠落バー)でトリガーに達し約定していた。日足安値フォールバックで救済:")
            _ef = short_entry_fill_5m(db, trig, False, entry_gap_limit=0.03,
                                      day_open=_day_open, day_low=_day_low, day_high=_day_high)
            if _ef is None:
                print("      → 指値下限ガード(-3%)超のギャップダウン = 約定不可(正しく見送り)。")
            else:
                _xp2, _rsn2, _e2, _x2 = short_exit_5m(db, trig, stop, tgt, False,
                                                      include_entry_bar=True,
                                                      day_low=_day_low, day_high=_day_high,
                                                      day_close=_day_close)
                _pnl2 = short_pnl(_ef, _xp2, _rsn2, 100, 0.001, 0.0)
                _xlbl = _x2.strftime('%H:%M') if hasattr(_x2, 'strftime') else str(_x2)
                print(f"      約定価格 = {_ef:,.0f} (=min(トリガー,日足始値))")
                print(f"      決済理由 = {_rsn2} / 決済価格 = {_xp2:,.0f} / 決済バー = {_xlbl}")
                print(f"      100株損益(手数料込) = {_pnl2:+,.0f}円  ← 修正後の計上値")
        else:
            print(f"  ✓ 日足安値 {_day_low:,.0f} > トリガー {trig:,.0f}")
            print("    → 実際に安値がトリガーに届いていない。約定しないのが正しい")
            print("      (チャートの目視は1〜2ティックの誤差が出る)。修正不要。")
    else:
        print("[warn] 日足安値が取得できず、欠落バー起因かの判定不可。")
    raise SystemExit(0)

ent_ts = db.index[ei]
print(f"約定バー: #{ei} {ent_ts.strftime('%H:%M') if hasattr(ent_ts,'strftime') else ent_ts} "
      f"(この時点で初めて安値<=トリガー)")

# 約定前に損切りラインへ達していたか(=寄りの跳ねが約定前か)を明示
pre_spike = [j for j in range(0, ei + 1) if highs[j] >= stop]
if pre_spike:
    print(f"※ 約定バー(#{ei})以前に高値>=損切({stop:,.0f})のバーあり: {pre_spike} "
          f"→ これは『約定前の跳ね』なので損切り判定に使われない(正しい挙動)")

# 日足の始値は冒頭で取得済み(_day_open)。約定は日足始値優先。
print(f"日足の始値(寄り) = {_day_open}")

# short_exit_5m と同じ判定を実行(約定は日足始値優先/欠落バーは日足安値で救済)
ef = short_entry_fill_5m(db, trig, False, entry_gap_limit=0.03, day_open=_day_open,
                         day_low=_day_low, day_high=_day_high)
# 寄り約定(ギャップ=約定価格がトリガーと異なる)なら約定バーから損切りを見る
_gap = (ef is not None) and (abs(ef - trig) > 1e-9)
print(f"寄り約定(ギャップ)= {_gap}  (True=約定バーから損切りチェック / False=次バーから)")
xp, reason, e_ts, x_ts = short_exit_5m(db, trig, stop, tgt, False, include_entry_bar=_gap,
                                       day_low=_day_low, day_high=_day_high,
                                       day_close=_day_close)
print(f"\n▼ short_exit_5m 判定")
print(f"  約定価格(現実モデル)= {ef if ef is not None else 'なし(ギャップ過大)'}")
print(f"  決済理由 = {reason}  決済価格 = {xp}")
if x_ts is not None:
    print(f"  決済バー時刻 = {x_ts.strftime('%H:%M') if hasattr(x_ts,'strftime') else x_ts}")
if ef is not None:
    pnl = short_pnl(ef, xp, reason, 100, 0.001, 0.0)
    print(f"  100株損益(手数料込) = {pnl:+,.0f}円")

print("\n読み方:")
print("  ・『◀約定』より後のバーに『↑損切ライン到達』が出れば損切りは正当。")
print("  ・損切ライン到達が『◀約定』より前のバーだけなら、約定後は損切りに達しておらず、")
print("    決済理由は target か close になるはず。もし stop なら要調査。")
