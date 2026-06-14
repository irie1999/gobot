"""
build_watchlist_daytrade.py  ―  WF CSV → WATCHLIST 生成
==================================================================
逆指値ロング (build_watchlist.py) を5分足デイトレ用に移植。

scan_walkforward_daytrade.py が生成した CSV を読み、
ランキング・フィルタして WATCHLIST 用 Python ファイルを生成。

【使い方】
  python build_watchlist_daytrade.py                # 全戦略のWATCHLIST
  python build_watchlist_daytrade.py --strategy MACD
  python build_watchlist_daytrade.py --top 30 --max-price 6000
  python build_watchlist_daytrade.py --output daytrade_donchian_winners.py --strategy DON --top 30
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

STRATEGIES_LONG = ["DON", "MACD", "RSI2", "A7", "VOL", "MOM"]
STRATEGIES_SHORT = ["DON_S", "MACD_S", "RSI2_S", "A7_S", "VOL_S", "MOM_S"]


def _float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _composite_score(r):
    """総合スコア = 損益 × (1 + Sharpe)"""
    pnl = _float(r.get("total_pnl", 0))
    sharpe = _float(r.get("sharpe", 0))
    return pnl * (1.0 + max(sharpe, 0.0))


def find_latest_csv(strategy, wf_dir, mode=None):
    """最新の walkforward_<strategy>[_<mode>]_<date>.csv を取得。

    mode を指定すれば そのモードのファイルだけ。
    指定なしなら 全モード のうち最新ファイルを返す (mode明示推奨)。
    """
    if mode:
        pattern = f"walkforward_{strategy}_{mode}_*.csv"
    else:
        pattern = f"walkforward_{strategy}_*.csv"
    cands = sorted(wf_dir.glob(pattern), reverse=True)
    return cands[0] if cands else None


def load_csv(csv_path, max_price, min_price, min_pf, min_trades):
    """CSV読み込み + フィルタ。"""
    if not csv_path or not csv_path.exists():
        return []
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        price = _float(r.get("latest_price", 0))
        pf = _float(r.get("total_pf", 0))
        n = int(_float(r.get("total_trades", 0)))
        pnl = _float(r.get("total_pnl", 0))
        dd = _float(r.get("total_dd", 0))
        if max_price > 0 and price > max_price:
            continue
        if min_price > 0 and price < min_price:
            continue
        if pf < min_pf:
            continue
        if n < min_trades:
            continue
        if pnl <= 0:
            continue
        out.append(r)
    out.sort(key=_composite_score, reverse=True)
    return out


def write_watchlist(rows, out_path, strategy_label, top=None,
                    include_strategy=False):
    """WATCHLIST を Python ファイルとして出力。

    include_strategy=True なら (symbol, name, strategy) の3要素タプル形式。
    """
    if top:
        rows = rows[:top]
    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    lines = [
        '"""',
        f"デイトレ WATCHLIST ({strategy_label})",
        f"生成: build_watchlist_daytrade.py",
        f"生成日時: {today}",
        f"対象: {len(rows)}銘柄",
        '"""',
        "",
        "SYMBOLS: list[tuple[str, str, str]] = [" if include_strategy
        else "SYMBOLS: list[tuple[str, str]] = [",
    ]
    for r in rows:
        sym = r.get("symbol", "")
        name = r.get("name", "").replace('"', '\\"')
        if not sym:
            continue
        if include_strategy:
            strat = r.get("_via", r.get("strategy", "DON"))
            lines.append(f'    ("{sym}", "{name}", "{strat}"),  '
                         f'# PF={r.get("total_pf","")} '
                         f'損益={r.get("total_pnl","")}')
        else:
            lines.append(f'    ("{sym}", "{name}"),  '
                         f'# PF={r.get("total_pf","")} '
                         f'損益={r.get("total_pnl","")}')
    lines.append("]")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="WF CSV → WATCHLIST 生成")
    parser.add_argument("--strategy", default="all",
                        help="all/long/short/個別戦略名 (DON/MACD/...)")
    parser.add_argument("--wf-dir", type=Path,
                        default=Path("walkforward_daytrade_results"))
    parser.add_argument("--mode", default=None,
                        choices=[None, "standard", "lenient", "strict"],
                        help="どのスキャンモードのCSV を使うか (省略時は最新)")
    parser.add_argument("--top", type=int, default=30,
                        help="戦略ごとの上位N銘柄 (デフォルト30)")
    parser.add_argument("--max-price", type=int, default=6000)
    parser.add_argument("--min-price", type=int, default=1000)
    parser.add_argument("--min-pf", type=float, default=1.2)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--output", type=str, default=None,
                        help="出力ファイル名 (省略時は戦略別)")
    parser.add_argument("--combined", action="store_true",
                        help="全戦略を1ファイルに結合 (重複除去)")
    args = parser.parse_args()

    # 戦略リスト決定
    if args.strategy == "all":
        strategies = STRATEGIES_LONG + STRATEGIES_SHORT
    elif args.strategy == "long":
        strategies = STRATEGIES_LONG
    elif args.strategy == "short":
        strategies = STRATEGIES_SHORT
    else:
        strategies = [args.strategy]

    if args.combined:
        # 全戦略から重複除去で1つのWATCHLIST
        all_syms = {}  # symbol → (name, best_score)
        per_strat = {}
        for strat in strategies:
            csv_path = find_latest_csv(strat, args.wf_dir, args.mode)
            if not csv_path:
                print(f"[skip] {strat}: CSV なし")
                continue
            rows = load_csv(csv_path, args.max_price, args.min_price,
                            args.min_pf, args.min_trades)
            print(f"  {strat}: {csv_path.name} → {len(rows)}件通過")
            for r in rows[:args.top]:
                sym = r.get("symbol", "")
                if not sym:
                    continue
                score = _composite_score(r)
                if sym not in all_syms or score > all_syms[sym][1]:
                    all_syms[sym] = (r.get("name", ""), score, strat, r)
            per_strat[strat] = rows[:args.top]
        # ソート
        merged = sorted(all_syms.items(), key=lambda x: -x[1][1])
        out_path = Path(args.output or "daytrade_combined_watchlist.py")
        rows_out = []
        for sym, (name, score, strat, r) in merged:
            r["_via"] = strat
            rows_out.append(r)
        write_watchlist(rows_out, out_path, f"統合 ({len(strategies)}戦略)",
                        top=args.top, include_strategy=True)
        print(f"\n統合 WATCHLIST: {len(rows_out[:args.top])}銘柄 → {out_path}")
        # 戦略別 内訳表示
        strat_count = {}
        for r in rows_out[:args.top]:
            s = r.get("_via", "?")
            strat_count[s] = strat_count.get(s, 0) + 1
        print(f"  戦略内訳: " + ", ".join(
            f"{s}:{n}" for s, n in sorted(strat_count.items(),
                                            key=lambda x: -x[1])))
    else:
        # 戦略ごとに別ファイル
        for strat in strategies:
            csv_path = find_latest_csv(strat, args.wf_dir, args.mode)
            if not csv_path:
                print(f"[skip] {strat}: CSV なし")
                continue
            rows = load_csv(csv_path, args.max_price, args.min_price,
                            args.min_pf, args.min_trades)
            out_path = Path(args.output or
                            f"daytrade_watchlist_{strat.lower()}.py")
            write_watchlist(rows, out_path, strat, top=args.top)
            print(f"  {strat}: {len(rows[:args.top])}銘柄 → {out_path}")


if __name__ == "__main__":
    main()
