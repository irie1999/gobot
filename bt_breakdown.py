"""
bt_breakdown.py ― 指定銘柄×戦略の BTスコア内訳を窓ごとに表示する診断ツール
==============================================================================
BTスコア(calc_recommend_score)が、どの期間窓(30/90/180/365日)の成績で
決まっているかを可視化する。「なぜこの銘柄はBTが低い/高いのか」を調べる用。

使い方:
  python bt_breakdown.py 8343.T DON
  python bt_breakdown.py 3046.T MACD
  python bt_breakdown.py 6136.T A7 --name オーエスジー

戦略の自動振り分け:
  DON / VOL / MOM        → ブレイクアウト (check_signals_breakout)
  MACD / A7 / RSI2       → 逆指値B       (check_signals_stop)
"""
from __future__ import annotations

import argparse
import sys

BREAKOUT = {"DON", "VOL", "MOM"}
STOP = {"MACD", "A7", "RSI2"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="銘柄コード (例 8343.T)")
    ap.add_argument("strategy", help="戦略 (DON/VOL/MOM/MACD/A7/RSI2)")
    ap.add_argument("--name", default="", help="銘柄名 (表示用・任意)")
    args = ap.parse_args()

    strat = args.strategy.upper()
    if strat in BREAKOUT:
        import check_signals_breakout as mod
    elif strat in STOP:
        import check_signals_stop as mod
    else:
        print(f"未知の戦略: {strat} (DON/VOL/MOM/MACD/A7/RSI2 のいずれか)")
        sys.exit(1)

    name = args.name or args.symbol
    r = mod.backtest_one(args.symbol, name, strat)
    if r is None:
        print(f"{args.symbol} {strat}: バックテスト結果なし (データ取得失敗?)")
        sys.exit(1)

    pr = r["period_results"]
    print("=" * 66)
    print(f"  {args.symbol} {strat}  BTスコア内訳 (窓ごと)")
    print("=" * 66)
    print(f"  {'窓':>5} | {'取引':>4} | {'勝率':>6} | {'PF':>6} | {'損益':>12}")
    print("  " + "-" * 62)
    for d in sorted(pr):
        x = pr[d]
        pf_s = mod._pf_str(x["pf"])
        print(f"  {d:>4}d | {x['trades']:>4} | {x['win_rate']:>5.1f}% | "
              f"{pf_s:>6} | {x['total_pnl']:>+12,.0f}")

    # スコア成分の分解 (calc_recommend_score と同じ計算)
    res = [v for v in pr.values() if v and v.get("trades", 0) > 0]
    print("  " + "-" * 62)
    if not res:
        print("  有効な窓なし (全窓で取引0件) → スコア0")
        return
    avg_wr = sum(v["win_rate"] for v in res) / len(res)
    avg_pf = sum(min(v["pf"] if v["pf"] != float("inf") else 10, 10)
                 for v in res) / len(res)
    stable = sum(1 for v in res if v["total_pnl"] > 0) / len(res)
    t_trades = max(v["trades"] for v in res)
    print(f"  有効窓数: {len(res)}  (取引0件の窓は平均から除外)")
    print(f"  平均勝率  {avg_wr:5.1f}%  → {avg_wr * 0.4:5.1f}点 / 40")
    print(f"  平均PF    {avg_pf:5.2f}   → {(avg_pf / 10) * 30:5.1f}点 / 30")
    print(f"  期間安定性 {stable:5.2f}   → {stable * 20:5.1f}点 / 20  "
          f"(プラス窓 {sum(1 for v in res if v['total_pnl'] > 0)}/{len(res)})")
    print(f"  取引数    {t_trades:>3}     → {min(t_trades / 20, 1) * 10:5.1f}点 / 10")

    score, rank = mod.calc_recommend_score(pr)
    print("  " + "-" * 62)
    print(f"  BTスコア = {score}  {rank}")

    # ATRペナルティの有無も参考表示
    if res:
        try:
            sig = mod.check_signal_on_date(args.symbol, strat, target_date=None)
            slp = sig.get("stop_loss_pct") if sig else None
            if slp is not None:
                adj, note = mod.apply_atr_penalty(score, slp)
                if note:
                    print(f"  ※ ATRペナルティ: 損切り幅{note} → {adj}")
                else:
                    print(f"  ※ ATRペナルティなし (損切り幅 {slp:.1f}% ≤ 7%)")
        except Exception:
            pass


if __name__ == "__main__":
    main()
