"""
compare_entry_fill.py — 「指値を下にセット」案を実データで検証する
=====================================================================

ユーザー仮説:
  「シグナル＝今後上がる確率が高い。なら逆指値ではなく "下のほうの指値" で
   仕込めば、わざわざ一度上がってから買う逆指値より安く拾えて得では?」

これに対する反論 (要検証):
  1. 押し目を待つ指値は **約定しない** ことが多い (フィル率が落ちる)
  2. ブレイクアウト系 (MACD/DON/VOL/MOM) は発火後に押し戻しが来にくい

本スクリプトは現行 WATCHLIST (check_signals_stop / check_signals_breakout) の
銘柄×戦略を、同一期間・同一データで以下のエントリー方式で回し、
**フィル率と損益を横並びで比較** して上記 1/2 を定量確認する。

  STOP   : 逆指値 em=0.0  (現行。高値 >= 前日終値 で約定 = 確認してから買う)
  LIM0   : 指値   em=0.0  (前日終値ちょうどの指値。安値 <= 終値 で約定)
  LIM-0.5: 指値   em=-0.5 (前日終値 -0.5ATR の指値 = ちょい下で待つ)
  LIM-1.0: 指値   em=-1.0 (前日終値 -1.0ATR の指値 = しっかり下で待つ)

損切り/目標は各戦略の (sm, tm) を共通使用するので、違いはエントリー方式だけ。

使い方:
  python compare_entry_fill.py                 # 365日, conservative
  python compare_entry_fill.py --days 180
  python compare_entry_fill.py --aggressive
  python compare_entry_fill.py --family stop   # 逆指値Bのみ
  python compare_entry_fill.py --family breakout
  python compare_entry_fill.py --workers 8

注意: フィル率は「ENTRY_EXPIRE 日以内に注文価格にタッチしたか」。
      指値を下げるほどフィル率が落ちる = 仮説1の確認。
      ブレイクアウト系のフィル率が特に低い = 仮説2の確認。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# --aggressive を import 前に環境変数へ (各モジュールがトップで読むため)
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

from backtest_limit_entry import fetch, run_limit_backtest, WORKERS as _DEFAULT_WORKERS

# エントリー方式: (label, entry_type, entry_atr_mult)
ENTRY_CONFIGS = [
    ("STOP",    "stop",  0.0),    # 現行 (逆指値・確認買い)
    ("LIM0",    "limit", 0.0),    # 終値ちょうどの指値
    ("LIM-0.5", "limit", -0.5),   # 終値 -0.5ATR の指値
    ("LIM-1.0", "limit", -1.0),   # 終値 -1.0ATR の指値
]


def load_targets(family: str) -> list[tuple[str, str, str, object, float, float]]:
    """WATCHLIST から (symbol, name, strategy, calc_fn, sm, tm) のリストを作る。"""
    targets: list[tuple] = []
    if family in ("all", "stop"):
        import check_signals_stop as _stop
        for sym, name, strat in _stop.WATCHLIST:
            calc_fn, _em, sm, tm = _stop.STRATEGY_PARAMS[strat]
            targets.append((sym, name, strat, calc_fn, sm, tm))
    if family in ("all", "breakout"):
        import check_signals_breakout as _brk
        for sym, name, strat in _brk.WATCHLIST:
            calc_fn, _em, sm, tm = _brk.STRATEGY_PARAMS[strat]
            targets.append((sym, name, strat, calc_fn, sm, tm))
    return targets


def run_one(target: tuple, days: int) -> dict | None:
    """1銘柄×1戦略を全エントリー方式で回し、方式別の result dict を返す。"""
    sym, name, strat, calc_fn, sm, tm = target
    df = fetch(sym)
    if df is None or df.empty:
        return None
    out: dict[str, dict] = {}
    for label, etype, em in ENTRY_CONFIGS:
        r = run_limit_backtest(sym, name, df, calc_fn,
                               em, sm, tm, days, strat,
                               entry_type=etype)
        out[label] = r or {}
    return {"symbol": sym, "name": name, "strategy": strat, "results": out}


def _agg_blank() -> dict:
    return {"signals": 0, "filled": 0, "wins": 0, "trades": 0,
            "total_pnl": 0.0, "hold_sum": 0.0}


def _accumulate(agg: dict, r: dict) -> None:
    agg["signals"]   += r.get("signals", 0)
    agg["filled"]    += r.get("filled", 0)
    agg["wins"]      += r.get("wins", 0)
    agg["trades"]    += r.get("trades", 0)
    agg["total_pnl"] += r.get("total_pnl", 0.0)
    agg["hold_sum"]  += r.get("avg_hold", 0.0) * r.get("filled", 0)


def _fmt_row(label: str, a: dict) -> str:
    sig, fil = a["signals"], a["filled"]
    fill_pct = fil / sig * 100 if sig else 0.0
    wr       = a["wins"] / fil * 100 if fil else 0.0
    avg_hold = a["hold_sum"] / fil if fil else 0.0
    pnl      = a["total_pnl"]
    return (f"  {label:<8} シグナル{sig:>4}  約定{fil:>4}  "
            f"フィル率{fill_pct:5.1f}%  勝率{wr:5.1f}%  "
            f"平均保有{avg_hold:4.1f}日  損益{pnl:>12,.0f}円")


def main() -> int:
    ap = argparse.ArgumentParser(description="指値(下置き) vs 逆指値 のフィル率・損益比較")
    ap.add_argument("--days", type=int, default=365, help="バックテスト期間 (既定365)")
    ap.add_argument("--family", choices=["all", "stop", "breakout"], default="all")
    ap.add_argument("--aggressive", action="store_true", help="aggressive プリセット")
    ap.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    args = ap.parse_args()

    mode = os.getenv("TRADING_MODE", "conservative")
    targets = load_targets(args.family)
    print("=" * 78)
    print(f"エントリー方式比較  期間={args.days}日  モード={mode}  "
          f"対象={len(targets)}銘柄×戦略 ({args.family})")
    print("  STOP=逆指値(現行) / LIM0=終値指値 / LIM-0.5,-1.0=終値より下に置く指値")
    print("=" * 78)

    # 戦略別・方式別の集計
    by_strat: dict[str, dict[str, dict]] = defaultdict(
        lambda: {lbl: _agg_blank() for lbl, _, _ in ENTRY_CONFIGS})
    overall: dict[str, dict] = {lbl: _agg_blank() for lbl, _, _ in ENTRY_CONFIGS}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, t, args.days): t for t in targets}
        done = 0
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            if not res:
                continue
            strat = res["strategy"]
            for label in (lbl for lbl, _, _ in ENTRY_CONFIGS):
                r = res["results"].get(label) or {}
                _accumulate(by_strat[strat][label], r)
                _accumulate(overall[label], r)
            print(f"\r  進捗 {done}/{len(targets)}", end="", flush=True)
    print()

    # 戦略別出力 (mean-reversion な RSI2 と breakout 系の差を見やすく)
    strat_order = ["RSI2", "MACD", "A7", "DON", "VOL", "MOM"]
    for strat in [s for s in strat_order if s in by_strat] + \
                 [s for s in by_strat if s not in strat_order]:
        print(f"\n■ {strat}")
        for label, _, _ in ENTRY_CONFIGS:
            print(_fmt_row(label, by_strat[strat][label]))

    print("\n" + "=" * 78)
    print("■ 全体合計")
    for label, _, _ in ENTRY_CONFIGS:
        print(_fmt_row(label, overall[label]))
    print("=" * 78)

    # 仮説の自動判定コメント
    stop_fill = (overall["STOP"]["filled"] / overall["STOP"]["signals"] * 100
                 if overall["STOP"]["signals"] else 0)
    lim1_fill = (overall["LIM-1.0"]["filled"] / overall["LIM-1.0"]["signals"] * 100
                 if overall["LIM-1.0"]["signals"] else 0)
    print(f"\n【仮説1: 下置き指値はフィル率が落ちる】")
    print(f"  逆指値STOP フィル率 {stop_fill:.1f}%  →  -1.0ATR指値 {lim1_fill:.1f}% "
          f"(差 {stop_fill - lim1_fill:+.1f}pt)")
    print(f"\n【仮説2: ブレイクアウト系ほど押し戻しが来にくい (=下指値が刺さらない)】")
    print(f"  戦略別の LIM-1.0 フィル率を比較 (低いほど押し戻しが少ない):")
    for strat in [s for s in strat_order if s in by_strat]:
        a = by_strat[strat]["LIM-1.0"]
        fp = a["filled"] / a["signals"] * 100 if a["signals"] else 0.0
        print(f"    {strat:<5} {fp:5.1f}%")
    print("\n※ 損益は STOP のスリッページ込み (逆指値買い+0.5%)。指値はスリッページ0。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
