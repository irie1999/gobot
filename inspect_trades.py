"""
inspect_trades.py  ―  指定銘柄×戦略の最近トレードを目視確認 (データ/戦略の妥当性チェック)

使い方:
  python inspect_trades.py 1605.T              # Donchian (既定)
  python inspect_trades.py 1605.T VWAP         # 戦略指定
  python inspect_trades.py 1605.T Donchian 20  # 直近20件
"""
import sys
import daytrade_data as d

STRATS = {
    "Donchian":     "daytrade_donchian",
    "GapReversal":  "daytrade_gap_reversal",
    "VWAP":         "daytrade_vwap_rebound",
    "OpenMomentum": "daytrade_opening_momentum",
    "MACDBreak":    "daytrade_macd_break",
    "StochATR":     "daytrade_stoch_atr",
    "VolSurge":     "daytrade_volsurge",
    "Pivot":        "daytrade_pivot",
    "RSI":          "daytrade_rsi",
}


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else "1605.T"
    strat = sys.argv[2] if len(sys.argv) > 2 else "Donchian"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 12

    mod = __import__(STRATS.get(strat, "daytrade_donchian"))
    df = d.load_intraday(sym, days=400, source="local")
    if df is None or df.empty:
        print(f"{sym}: データなし")
        return
    print(f"{sym}  {strat}  データ {len(df)}本  "
          f"{df.index[0].date()} 〜 {df.index[-1].date()}  "
          f"価格 {df['close'].iloc[-1]:.0f}円")
    try:
        r = mod.backtest_symbol(sym, sym, df)
    except TypeError:
        r = mod.backtest_symbol(sym, sym, df, 600000, 6000)
    trades = r["trades"] if r else []
    if not trades:
        print("  取引なし")
        return
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total = sum(t["pnl"] for t in trades)
    print(f"  全{len(trades)}取引  勝率{wins/len(trades)*100:.1f}%  "
          f"合計損益{total:+,.0f}円")
    print(f"  --- 直近{min(n, len(trades))}取引 ---")
    for t in trades[-n:]:
        ed = t.get("entry_dt")
        xd = t.get("exit_dt")
        ed_s = ed.strftime("%Y-%m-%d %H:%M") if ed is not None else "?"
        xd_s = xd.strftime("%H:%M") if xd is not None else "?"
        print(f"    {ed_s}-{xd_s}  "
              f"{t['entry_p']:.0f} -> {t['exit_p']:.0f}  "
              f"pnl {t['pnl']:+,.0f}  {t.get('reason','')}")


if __name__ == "__main__":
    main()
