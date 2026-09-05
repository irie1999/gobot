"""check_signal_debug.py — 指定銘柄のDON/VOLTF等のシグナル条件を"生の数字"で表示する診断。

なぜ作ったか:
  損益タブの明細トレードは backtest_one 由来で、BTキャッシュ(bt_v13sd1_YYYY-MM-DD.pkl)から
  復元される。このキャッシュは日付トークンで1日中使い回すので、朝(場中の暫定バー)で作られた
  キャッシュが引け後も再利用され得る。一方このスクリプトは fetch() で"今の"日足を取り直し、
  各戦略の entry_sig を最新バーまで計算して表示する。
  → 「確定データで07/28にVOLTFが点灯するのか」を、憶測でなく数字で確認できる。

使い方(引け後・あなたの機械):
  python check_signal_debug.py 7003            # 既定 DON,VOL,VOLTF,MOM,MACDTF を7バー表示
  python check_signal_debug.py 7003 --bars 12  # 直近12バー
  python check_signal_debug.py 7003 --strats DON,VOLTF

各行: 日付 / 終値 / high15 / high5 / MA50 / 出来高 / 20日平均×1.5 / entry_sig
  DON  の点灯 = 終値>high15 かつ 終値>MA50
  VOLTF の点灯 = 終値>high5 かつ 出来高>(20日平均×1.5) かつ 終値>MA50
"""
from __future__ import annotations
import argparse

import pandas as pd

from backtest_limit_entry import fetch
import scan_breakout_entry as _sb

# 戦略名 → calc関数 (check_signals_breakout.STRATEGY_PARAMS と同じ実体)
_CALC = {
    "DON":   _sb.calc_donchian,
    "VOL":   _sb.calc_vol_breakout,
    "VOLTF": _sb.calc_vol_breakout_tf,
    "MOM":   getattr(_sb, "calc_momentum", None),
    "MACDTF": getattr(_sb, "calc_macd_tf", None),
}

ap = argparse.ArgumentParser(description="銘柄のシグナル条件を生の数字で表示(確定データ)")
ap.add_argument("symbol", help="銘柄コード(例 7003 または 7003.T)")
ap.add_argument("--bars", type=int, default=7, help="表示するバー数(直近)")
ap.add_argument("--strats", type=str, default="DON,VOL,VOLTF,MOM,MACDTF")
args = ap.parse_args()

sym = args.symbol if args.symbol.endswith(".T") else f"{args.symbol}.T"
df0 = fetch(sym, 365)
if df0 is None or len(df0) < 60:
    raise SystemExit(f"[error] {sym} のデータが取得できない/短すぎる")

latest = df0.index[-1]
print(f"[{sym}] fetch最新バー = {latest.strftime('%Y-%m-%d')}  (これが確定日か確認: 引け後なら当日)")
print(f"        終値={float(df0['close'].iloc[-1]):,.1f}  出来高={float(df0['volume'].iloc[-1]):,.0f}\n")

for strat in [s.strip().upper() for s in args.strats.split(",") if s.strip()]:
    fn = _CALC.get(strat)
    if fn is None:
        print(f"--- {strat}: calc関数が見つからないのでスキップ ---")
        continue
    d = fn(df0.copy())
    c, h, v = d["close"], d["high"], d["volume"]
    # 表示用の中間値を再計算(entry_sig の内訳を見せる)
    d["high15"]     = h.rolling(15).max().shift(1)
    d["high5"]      = h.rolling(5).max().shift(1)
    d["ma50"]       = c.rolling(50).mean()
    d["volx1.5"]    = v.rolling(20).mean() * 1.5
    cols = ["close", "high15", "high5", "ma50", "volume", "volx1.5", "entry_sig"]
    show = d[cols].tail(args.bars).copy()
    # 数値を見やすく
    for col in ["close", "high15", "high5", "ma50"]:
        show[col] = show[col].map(lambda x: f"{x:,.0f}" if pd.notna(x) else "—")
    for col in ["volume", "volx1.5"]:
        show[col] = show[col].map(lambda x: f"{x:,.0f}" if pd.notna(x) else "—")
    print(f"===== {strat} 直近{args.bars}バー ({fn.__name__}) =====")
    print(show.to_string())
    last = d.iloc[-1]
    sig = bool(last.get("entry_sig", False))
    print(f"  → 最新バー({latest.strftime('%m/%d')}) entry_sig = {sig}"
          + ("  ★点灯" if sig else "  (点灯せず)"))
    if strat in ("VOLTF", "VOL"):
        _c = float(last["close"]); _h5 = float(last.get("high5", float('nan')))
        _v = float(last["volume"]); _vx = float(last.get("volx1.5", float('nan')))
        _ma = float(last.get("ma50", float('nan')))
        print(f"     内訳: 終値>{_h5:,.0f}(5日高値)? {_c>_h5} / "
              f"出来高{_v:,.0f}>{_vx:,.0f}(20日平均×1.5)? {_v>_vx}"
              + (f" / 終値>{_ma:,.0f}(MA50)? {_c>_ma}" if strat == "VOLTF" else ""))
    print()
