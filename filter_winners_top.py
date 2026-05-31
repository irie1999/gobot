"""
filter_winners_top.py  ―  既存winnersファイルから損益上位N銘柄を抽出
==================================================================
winners_by_regime.py で生成された大きな winners ファイルから、
損益上位N銘柄だけを抽出して新しい winners ファイルを生成。

【使い方】
  python filter_winners_top.py --regime bull --top 30
  python filter_winners_top.py --regime sideways --top 20
  python filter_winners_top.py --regime bear --top 15
  python filter_winners_top.py --regime bull --top 30 --output daytrade_donchian_winners.py
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats


REGIME_FILES = {
    "bull": "daytrade_donchian_winners_bull.py",
    "sideways": "daytrade_donchian_winners_sideways.py",
    "bear": "daytrade_donchian_winners_bear.py",
    "default": "daytrade_donchian_winners.py",
}


def main():
    parser = argparse.ArgumentParser(description="winnersから上位N銘柄抽出")
    parser.add_argument("--regime", required=True,
                        choices=["bull", "sideways", "bear", "default"])
    parser.add_argument("--top", type=int, default=30,
                        help="上位何銘柄を取るか (デフォルト30)")
    parser.add_argument("--days", type=int, default=730,
                        help="ランキング用の backtest 期間")
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--output", default=None,
                        help="出力ファイル (デフォルト: daytrade_donchian_winners.py)")
    parser.add_argument("--sort-by", choices=["pnl", "pf"], default="pnl",
                        help="ソートキー: pnl=損益順, pf=PF順 (デフォルト: pnl)")
    args = parser.parse_args()

    # ソース読み込み
    src_path = REGIME_FILES[args.regime]
    import importlib.util
    spec = importlib.util.spec_from_file_location("src", src_path)
    src = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(src)
    targets = src.SYMBOLS
    print(f"ソース: {src_path} ({len(targets)}銘柄)")

    # backtest して損益順にソート
    print(f"\n{len(targets)}銘柄を {args.days}日 backtest 中...", flush=True)
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    targets = [(s, n) for s, n in targets if s in fetched]

    t0 = time.time()
    ranked = []
    for i, (sym, name) in enumerate(targets, 1):
        r = backtest_symbol(sym, name, fetched[sym], args.budget, args.max_risk)
        if r and r["trades"]:
            stats = calc_stats(r["trades"], args.budget)
            ranked.append((sym, name, stats))
        if i % 100 == 0:
            print(f"  {i}/{len(targets)} ({time.time()-t0:.1f}s)", flush=True)

    # ソート
    if args.sort_by == "pnl":
        ranked.sort(key=lambda x: -x[2]["total_pnl"])
    else:
        ranked.sort(key=lambda x: -x[2]["pf"] if x[2]["pf"] != float("inf") else -999)

    # 上位N
    top = ranked[:args.top]

    # 出力
    out_path = Path(args.output or "daytrade_donchian_winners.py")
    lines = [
        '"""',
        f"Donchian winners ({args.regime} top {args.top})",
        f"生成: filter_winners_top.py --regime {args.regime} --top {args.top}",
        f"ソート: {args.sort_by}",
        f"対象: {len(top)}銘柄",
        '"""',
        "",
        "SYMBOLS: list[tuple[str, str]] = [",
    ]
    for sym, name, _ in top:
        safe = name.replace('"', '\\"')
        lines.append(f'    ("{sym}", "{safe}"),')
    lines.append("]")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print()
    print("=" * 80)
    print(f"  Top {args.top} 銘柄 (ソート: {args.sort_by})")
    print("=" * 80)
    print(f"{'順':>3} {'銘柄':<22} {'コード':<10} {'取引':>4} {'勝率':>5} "
          f"{'PF':>5} {'損益':>11} {'DD':>6}")
    print("-" * 80)
    for i, (sym, name, stats) in enumerate(top, 1):
        disp = name[:20] if len(name) <= 20 else name[:19] + "…"
        pf = "∞" if stats["pf"] == float("inf") else f"{stats['pf']:.2f}"
        print(f"{i:>3} {disp:<22} {sym:<10} {stats['n']:>4} "
              f"{stats['win_rate']:>4.0f}% {pf:>5} "
              f"{stats['total_pnl']:>+11,.0f} {stats['max_dd']:>+5.1f}%")
    print()
    print(f"出力: {out_path.resolve()}")


if __name__ == "__main__":
    main()
