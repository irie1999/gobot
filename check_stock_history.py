#!/usr/bin/env python3
"""
特定銘柄×戦略のバックテスト履歴を表示するスクリプト。

使い方:
  python check_stock_history.py --symbol 8387.T --strategy A7
  python check_stock_history.py --symbol 8387.T --strategy RSI2 --days 180
  python check_stock_history.py --symbol 8387.T --strategy MACD --aggressive
  python check_stock_history.py --symbol 8387.T --strategy A7 --mode conservative
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime

# ── モード設定 (check_signals_*.py と同じ方法) ─────────────────────────────
import os
parser_pre = argparse.ArgumentParser(add_help=False)
parser_pre.add_argument("--aggressive", action="store_true")
parser_pre.add_argument("--mode", choices=["conservative", "aggressive"], default=None)
_args_pre, _ = parser_pre.parse_known_args()
if _args_pre.mode == "aggressive" or _args_pre.aggressive:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

# ── インポート ─────────────────────────────────────────────────────────────
from backtest_limit_entry import fetch, run_limit_backtest, BACKTEST_DAYS

# 全戦略のパラメータを集約
from check_signals_stop import STRATEGY_PARAMS as STOP_PARAMS
from check_signals_breakout import STRATEGY_PARAMS as BRK_PARAMS

ALL_PARAMS: dict[str, tuple] = {**STOP_PARAMS, **BRK_PARAMS}
STOP_STRATEGIES   = set(STOP_PARAMS.keys())
BREAKOUT_STRATEGIES = set(BRK_PARAMS.keys())


def main() -> None:
    parser = argparse.ArgumentParser(description="銘柄×戦略のバックテスト履歴表示")
    parser.add_argument("--symbol",   required=True, help="銘柄コード (例: 8387.T)")
    parser.add_argument("--strategy", required=True, choices=list(ALL_PARAMS.keys()),
                        help="戦略名: MACD / A7 / RSI2 / DON / VOL / MOM")
    parser.add_argument("--days",     type=int, default=BACKTEST_DAYS,
                        help=f"バックテスト期間 (デフォルト: {BACKTEST_DAYS}日)")
    parser.add_argument("--aggressive", action="store_true", help="aggressiveモード")
    parser.add_argument("--mode", choices=["conservative", "aggressive"], default=None)
    args = parser.parse_args()

    symbol   = args.symbol.upper()
    strategy = args.strategy.upper()
    days     = args.days
    mode     = os.environ.get("TRADING_MODE", "conservative")

    print(f"\n{'='*60}")
    print(f"  {symbol}  ×  {strategy}  バックテスト履歴")
    print(f"  期間: 直近 {days} 日  /  モード: {mode}")
    print(f"{'='*60}\n")

    # データ取得
    print(f"データ取得中: {symbol} ...")
    df = fetch(symbol, days + 60)  # 余裕をもって取得
    if df is None or df.empty:
        print(f"ERROR: {symbol} のデータを取得できませんでした。")
        sys.exit(1)

    # 銘柄名取得
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        name = info.get("longName") or info.get("shortName") or symbol
    except Exception:
        name = symbol

    # バックテスト実行
    calc_fn, em, sm, tm = ALL_PARAMS[strategy]
    entry_type = "stop"  # 全戦略とも逆指値

    print(f"バックテスト実行中...")
    result = run_limit_backtest(
        symbol=symbol,
        name=name,
        df=df,
        calc_fn=calc_fn,
        entry_atr_mult=em,
        stop_atr_mult=sm,
        target_atr_mult=tm,
        backtest_days=days,
        strategy_name=strategy,
        entry_type=entry_type,
    )

    trade_log = result.get("trade_log", [])
    summary   = result.get("summary", {})

    if not trade_log:
        print("この期間にトレードはありませんでした。\n")
        return

    # ── サマリー表示 ────────────────────────────────────────────────────────
    wins   = sum(1 for t in trade_log if t.get("pnl", 0) > 0)
    losses = len(trade_log) - wins
    total_pnl = sum(t.get("pnl", 0) for t in trade_log)
    gross_p   = sum(t["pnl"] for t in trade_log if t.get("pnl", 0) > 0)
    gross_l   = sum(t["pnl"] for t in trade_log if t.get("pnl", 0) <= 0)
    wr  = wins / len(trade_log) * 100 if trade_log else 0
    pf  = abs(gross_p / gross_l) if gross_l != 0 else float("inf")
    avg = total_pnl / len(trade_log) if trade_log else 0

    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"

    print(f"【サマリー】")
    print(f"  取引数  : {len(trade_log)} 件  ({wins}勝 / {losses}敗)")
    print(f"  勝率    : {wr:.1f}%")
    print(f"  PF      : {pf_str}")
    print(f"  合計損益: {total_pnl:+,.0f} 円")
    print(f"  平均損益: {avg:+,.0f} 円/取引")
    print()

    # 決済理由別集計
    reasons: dict[str, int] = {}
    for t in trade_log:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    reason_str = "  ".join(f"{k}:{v}件" for k, v in sorted(reasons.items()))
    print(f"  決済理由: {reason_str}\n")

    # ── 取引明細 ────────────────────────────────────────────────────────────
    print(f"{'決済日':<10}  {'約定値':>7}  {'決済値':>7}  {'株数':>6}  {'保有':>4}  {'損益':>10}  決済理由    エントリー日")
    print("-" * 80)

    for t in sorted(trade_log, key=lambda x: str(x.get("exit_dt", "")), reverse=True):
        ed   = str(t.get("exit_dt",   "?"))[:10]
        ep   = t.get("entry_p", 0)
        xp   = t.get("exit_p",  0)
        qty  = t.get("qty",     0)
        hold = t.get("hold_days", 0)
        pnl  = t.get("pnl",    0)
        rsn  = t.get("reason", "?")
        nd   = str(t.get("signal_dt", "?"))[:10]

        pnl_str = f"{pnl:+,.0f}円"
        print(f"{ed:<10}  {ep:>7,.0f}  {xp:>7,.0f}  {qty:>5}株  {hold:>3}日  {pnl_str:>10}  {rsn:<10}  {nd}")

    print()
    print(f"合計: {total_pnl:+,.0f} 円  /  {len(trade_log)} 取引")
    print()


if __name__ == "__main__":
    main()
