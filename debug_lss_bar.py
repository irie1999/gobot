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
ap.add_argument("--daily-open", type=float, default=None,
                help="日足始値(v6基準の約定価格確認用)。例: --daily-open 2869")
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
    print("判定: 約定せず(安値がトリガーに一度も達しない)= no_entry")
    raise SystemExit(0)

ent_ts = db.index[ei]
print(f"約定バー: #{ei} {ent_ts.strftime('%H:%M') if hasattr(ent_ts,'strftime') else ent_ts} "
      f"(この時点で初めて安値<=トリガー)")

# 約定前に損切りラインへ達していたか(=寄りの跳ねが約定前か)を明示
pre_spike = [j for j in range(0, ei + 1) if highs[j] >= stop]
if pre_spike:
    print(f"※ 約定バー(#{ei})以前に高値>=損切({stop:,.0f})のバーあり: {pre_spike} "
          f"→ これは『約定前の跳ね』なので損切り判定に使われない(正しい挙動)")

# 5分足の opens[0] と daily_open の対比を表示
_5m_open = float(db["open"].iloc[0]) if not db.empty else None
print(f"\n▼ ギャップ判定の基準値")
print(f"  5分足 opens[0]  = {_5m_open:,.0f} ({db.index[0].strftime('%H:%M') if hasattr(db.index[0],'strftime') else db.index[0]} バー)")
print(f"  ※ J-Quants 5分足は 09:05 始まりのため opens[0] は 09:00 寄り付き価格ではない")
print(f"    → v6以降は日足始値(daily_open)を基準に使う。--daily-open で指定可能")

# short_exit_5m と同じ判定を実行
ef_v5 = short_entry_fill_5m(db, trig, False, entry_gap_limit=0.03)
ef = ef_v5  # デフォルト(daily_open 未指定)
xp, reason, e_ts, x_ts = short_exit_5m(db, trig, stop, tgt, False)
print(f"\n▼ short_exit_5m 判定")
print(f"  約定価格(v5 = opens[0]基準) = {ef_v5 if ef_v5 is not None else 'なし(ギャップ過大)'}")
print(f"  決済理由 = {reason}  決済価格 = {xp}")
if x_ts is not None:
    print(f"  決済バー時刻 = {x_ts.strftime('%H:%M') if hasattr(x_ts,'strftime') else x_ts}")
if ef is not None:
    pnl = short_pnl(ef, xp, reason, 100, 0.001, 0.0)
    print(f"  100株損益(手数料込) = {pnl:+,.0f}円")

# --daily-open 引数があれば v6 基準でも計算
_daily_open_arg = getattr(args, "daily_open", None)
if _daily_open_arg is not None:
    ef_v6 = short_entry_fill_5m(db, trig, False, entry_gap_limit=0.03,
                                daily_open=_daily_open_arg)
    print(f"  約定価格(v6 = 日足始値{_daily_open_arg:,.0f}基準) = "
          f"{ef_v6 if ef_v6 is not None else 'なし(ギャップ過大)'}")
    if ef_v6 is not None:
        pnl6 = short_pnl(ef_v6, xp, reason, 100, 0.001, 0.0)
        print(f"  100株損益(v6/手数料込) = {pnl6:+,.0f}円")

print("\n読み方:")
print("  ・『◀約定』より後のバーに『↑損切ライン到達』が出れば損切りは正当。")
print("  ・損切ライン到達が『◀約定』より前のバーだけなら、約定後は損切りに達しておらず、")
print("    決済理由は target か close になるはず。もし stop なら要調査。")
print("  ・v6では日足始値 > トリガーならギャップダウンなし=約定価格=トリガー。")
