"""
extract_winners_intersect.py  ―  30日 ∩ 90日 ハイブリッド抽出
==================================================================
prime全銘柄から「30日でも90日でも勝てる銘柄」を抽出。
カーブフィット回避 + 直近相場対応 + 最大カバー範囲 を両立。

【ロジック】
  Step 1: 30日 backtest → 全1,500銘柄の30日PF/取引数算出
  Step 2: 90日 backtest → 全1,500銘柄の90日PF/取引数算出
  Step 3: 両方で閾値クリアした銘柄を抽出
  Step 4: daytrade_donchian_winners.py 生成 + 取引明細HTML

【閾値 (デフォルト)】
  30日: PF ≥ 1.5, 取引 ≥ 20, 損益 ≥ 10,000
  90日: PF ≥ 1.3, 取引 ≥ 30, 損益 ≥ 30,000

【使い方】
  python extract_winners_intersect.py
  python extract_winners_intersect.py --no-html
  python extract_winners_intersect.py --min-pf-30d 1.3 --min-pf-90d 1.2
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats

JST = timezone(timedelta(hours=9))


def _load_prime():
    try:
        from symbols_listed_all import SYMBOLS
        return SYMBOLS
    except ImportError:
        sys.exit("symbols_listed_all.py が見つかりません")


def run_backtest(targets, days, budget, max_risk, label):
    """全銘柄を期間days でbacktest → 銘柄→stats辞書。"""
    symbols = [s for s, _ in targets]
    print(f"\n[{label}] {len(symbols)}銘柄 × {days}日 backtest 開始", flush=True)
    fetched = load_intraday_batch(symbols, days, source="local")
    max_price = budget / 100
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    print(f"  予算フィルタ後: {len(fetched)}銘柄", flush=True)

    t0 = time.time()
    result = {}
    for i, (sym, name) in enumerate(targets, 1):
        if sym not in fetched:
            continue
        r = backtest_symbol(sym, name, fetched[sym], budget, max_risk)
        if r and r["trades"]:
            stats = calc_stats(r["trades"], budget)
            result[sym] = (name, stats)
        if i % 200 == 0 or i == len(targets):
            print(f"    {i}/{len(targets)} 処理済み ({time.time()-t0:.1f}s)",
                  flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description="30日 ∩ 90日 winners 抽出")
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--min-pf-30d", type=float, default=1.5)
    parser.add_argument("--min-trades-30d", type=int, default=20)
    parser.add_argument("--min-pnl-30d", type=int, default=10_000)
    parser.add_argument("--min-pf-90d", type=float, default=1.3)
    parser.add_argument("--min-trades-90d", type=int, default=30)
    parser.add_argument("--min-pnl-90d", type=int, default=30_000)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print("=" * 80)
    print("  30日 ∩ 90日 ハイブリッド winners 抽出")
    print("=" * 80)
    print(f"  予算: {args.budget:,}円 / リスク: {args.max_risk:,}円")
    print(f"  30日: PF≥{args.min_pf_30d}, 取引≥{args.min_trades_30d}, "
          f"損益≥{args.min_pnl_30d:,}")
    print(f"  90日: PF≥{args.min_pf_90d}, 取引≥{args.min_trades_90d}, "
          f"損益≥{args.min_pnl_90d:,}")

    targets = _load_prime()
    print(f"\nprime universe: {len(targets)}銘柄")

    # Step 1: 30日 backtest
    r30 = run_backtest(targets, 30, args.budget, args.max_risk, "Step 1/3: 30日")

    # Step 2: 90日 backtest
    r90 = run_backtest(targets, 90, args.budget, args.max_risk, "Step 2/3: 90日")

    # Step 3: 両方クリアの銘柄を抽出
    print(f"\n[Step 3/3] intersection 抽出", flush=True)
    winners = []
    for sym, (name, s30) in r30.items():
        if sym not in r90:
            continue
        n90, s90 = r90[sym]
        # 30日条件
        if not (s30["pf"] >= args.min_pf_30d
                and s30["n"] >= args.min_trades_30d
                and s30["total_pnl"] >= args.min_pnl_30d):
            continue
        # 90日条件
        if not (s90["pf"] >= args.min_pf_90d
                and s90["n"] >= args.min_trades_90d
                and s90["total_pnl"] >= args.min_pnl_90d):
            continue
        winners.append((sym, name, s30, s90))

    # ソート: 30日損益降順
    winners.sort(key=lambda x: -x[2]["total_pnl"])

    print()
    print("=" * 95)
    print(f"  抽出結果: {len(winners)}銘柄 (30日でも90日でも勝てる銘柄)")
    print("=" * 95)
    print(f"{'銘柄':<22} {'コード':<10} "
          f"{'30d取引':>7} {'30dPF':>6} {'30d損益':>10}  "
          f"{'90d取引':>7} {'90dPF':>6} {'90d損益':>10}")
    print("-" * 95)
    for sym, name, s30, s90 in winners:
        disp = name[:20] if len(name) <= 20 else name[:19] + "…"
        pf30 = "∞" if s30["pf"] == float("inf") else f"{s30['pf']:.2f}"
        pf90 = "∞" if s90["pf"] == float("inf") else f"{s90['pf']:.2f}"
        print(f"{disp:<22} {sym:<10} "
              f"{s30['n']:>7} {pf30:>6} {s30['total_pnl']:>+10,.0f}  "
              f"{s90['n']:>7} {pf90:>6} {s90['total_pnl']:>+10,.0f}")
    print("=" * 95)
    print(f"\n30日合計損益: {sum(s30['total_pnl'] for _,_,s30,_ in winners):+,.0f}円")
    print(f"90日合計損益: {sum(s90['total_pnl'] for _,_,_,s90 in winners):+,.0f}円")

    # ファイル出力
    out = Path("daytrade_donchian_winners.py")
    lines = [
        '"""',
        f"Donchian winners (30日 ∩ 90日 intersection)",
        f"生成: extract_winners_intersect.py",
        f"対象: {len(winners)}銘柄",
        f"条件: 30日 PF≥{args.min_pf_30d}/取引≥{args.min_trades_30d}/損益≥{args.min_pnl_30d:,}",
        f"     × 90日 PF≥{args.min_pf_90d}/取引≥{args.min_trades_90d}/損益≥{args.min_pnl_90d:,}",
        '"""',
        "",
        "SYMBOLS: list[tuple[str, str]] = [",
    ]
    for sym, name, _, _ in winners:
        safe_name = name.replace('"', '\\"')
        lines.append(f'    ("{sym}", "{safe_name}"),')
    lines.append("]")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n生成: {out.resolve()}")

    # HTML レポート
    if not args.no_html and winners:
        print(f"\n取引明細HTML 生成中...", flush=True)
        cmd = [sys.executable, "winners_detail_report.py", "--days", "30",
               "--budget", str(args.budget), "--max-risk", str(args.max_risk)]
        if args.no_browser:
            cmd.append("--no-browser")
        subprocess.run(cmd)


if __name__ == "__main__":
    main()
