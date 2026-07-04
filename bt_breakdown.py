"""
bt_breakdown.py ― 指定銘柄×戦略の BTスコア内訳を窓ごとに表示する診断ツール
==============================================================================
BTスコア(calc_recommend_score)が、どの期間窓(30/90/180/365日)の成績で
決まっているかを可視化する。「なぜこの銘柄はBTが低い/高いのか」を調べる用。

使い方:
  python bt_breakdown.py 8343.T DON          # 戦略を指定
  python bt_breakdown.py 3046.T              # 戦略省略 → 全6戦略のBTを一覧
  python bt_breakdown.py 3046.T ALL          # 同上
  python bt_breakdown.py 6136.T A7 --name オーエスジー

戦略の自動振り分け:
  DON / VOL / MOM        → ブレイクアウト (check_signals_breakout)
  MACD / A7 / RSI2       → 逆指値B       (check_signals_stop)

※ レポートに出る「BT:○○」は"今日シグナルが出た戦略"の値。どの戦略か分から
   ないときは戦略を省略して全戦略のBTを一覧し、値が一致するものを探す。
"""
from __future__ import annotations

import argparse
import sys

BREAKOUT = {"DON", "VOL", "MOM"}
STOP = {"MACD", "A7", "RSI2"}
ALL_STRATS = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]


def _module_for(strat: str):
    if strat in BREAKOUT:
        import check_signals_breakout as mod
        return mod
    if strat in STOP:
        import check_signals_stop as mod
        return mod
    return None


def _score_of(symbol: str, strat: str, name: str):
    """(score, rank, period_results) を返す。取得失敗時は (None, None, None)。"""
    mod = _module_for(strat)
    if mod is None:
        return None, None, None
    r = mod.backtest_one(symbol, name, strat)
    if r is None:
        return None, None, None
    pr = r["period_results"]
    score, rank = mod.calc_recommend_score(pr)
    return score, rank, pr


def _print_all(symbol: str, name: str) -> None:
    print("=" * 66)
    print(f"  {symbol}  全戦略の BTスコア一覧")
    print("=" * 66)
    print(f"  {'戦略':>6} | {'BT':>4} | ランク | 各窓の取引数(30/90/180/365)")
    print("  " + "-" * 62)
    for strat in ALL_STRATS:
        score, rank, pr = _score_of(symbol, strat, name)
        if score is None:
            print(f"  {strat:>6} |   -- | (データ/シグナルなし)")
            continue
        counts = "/".join(str(pr.get(d, {}).get("trades", 0))
                          for d in (30, 90, 180, 365))
        print(f"  {strat:>6} | {score:>4} | {rank:<4} | {counts}")
    print("  " + "-" * 62)
    print("  詳細内訳を見たい戦略: python bt_breakdown.py "
          f"{symbol} <戦略名>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="銘柄コード (例 8343.T)")
    ap.add_argument("strategy", nargs="?", default="ALL",
                    help="戦略 (DON/VOL/MOM/MACD/A7/RSI2)。省略で全戦略一覧")
    ap.add_argument("--name", default="", help="銘柄名 (表示用・任意)")
    args = ap.parse_args()

    name = args.name or args.symbol
    strat = args.strategy.upper()

    if strat == "ALL":
        _print_all(args.symbol, name)
        return

    mod = _module_for(strat)
    if mod is None:
        print(f"未知の戦略: {strat} (DON/VOL/MOM/MACD/A7/RSI2 のいずれか)")
        sys.exit(1)

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
