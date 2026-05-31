"""
winners_by_regime.py  ―  レジーム別Donchian winners 抽出
==================================================================
日経のトレンド (上昇/横ばい/下落) ごとに、Donchianが機能する銘柄を抽出。
3つのwinnersファイルを生成:
  - daytrade_donchian_winners_bull.py     (上昇相場)
  - daytrade_donchian_winners_sideways.py (横ばい相場)
  - daytrade_donchian_winners_bear.py     (下落相場)

【ロジック】
  1. 日経225 日足取得 → 各日のレジーム判定 (MA10/MA25)
  2. prime全銘柄を730日backtest
  3. 各トレードを「エントリー日のレジーム」に分類
  4. 銘柄×レジームごとにPF集計
  5. 各レジームで PF≥1.3 & 取引≥20 の銘柄を抽出

【使い方】
  python winners_by_regime.py                  # 730日で抽出
  python winners_by_regime.py --days 365       # 365日
  python winners_by_regime.py --min-pf 1.5     # より厳しい基準
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch
from daytrade_donchian import backtest_symbol, calc_stats

JST = timezone(timedelta(hours=9))

REGIMES = ["up", "sideways", "down"]
REGIME_LABEL = {"up": "上昇 (Bull)", "sideways": "横ばい (Sideways)", "down": "下落 (Bear)"}
REGIME_FILE = {
    "up": "daytrade_donchian_winners_bull.py",
    "sideways": "daytrade_donchian_winners_sideways.py",
    "down": "daytrade_donchian_winners_bear.py",
}


def fetch_nikkei_regimes(years: int = 3) -> pd.Series:
    """日経225 日足から各日のレジームを判定。"""
    from nikkei_analysis import fetch_n225, label_trend
    print(f"  日経225 ({years}年分) を取得中...", flush=True)
    close = fetch_n225(years)
    trend = label_trend(close)
    print(f"  期間: {trend.index[0].date()} 〜 {trend.index[-1].date()}", flush=True)
    counts = trend.value_counts()
    print(f"  内訳: 上昇 {counts.get('up', 0)}日 / "
          f"横ばい {counts.get('sideways', 0)}日 / "
          f"下落 {counts.get('down', 0)}日", flush=True)
    return trend


def trades_by_regime(trades: list[dict], regime_map: pd.Series) -> dict:
    """トレードをエントリー日のレジームでグループ分け。"""
    result = {r: [] for r in REGIMES}
    for t in trades:
        dt = t.get("entry_dt")
        if not hasattr(dt, "date"):
            continue
        d = pd.Timestamp(dt.date()).normalize()
        if d not in regime_map.index:
            # 営業日でない or データ範囲外
            continue
        regime = regime_map[d]
        result[regime].append(t)
    return result


def write_winners(regime: str, items: list[tuple]) -> Path:
    """winners リストを Python ファイルに書き出す。"""
    out = Path(REGIME_FILE[regime])
    lines = [
        '"""',
        f"Donchian winners ({REGIME_LABEL[regime]}相場用)",
        f"生成: winners_by_regime.py",
        f"対象: {len(items)}銘柄",
        '"""',
        "",
        "SYMBOLS: list[tuple[str, str]] = [",
    ]
    for sym, name in items:
        safe_name = name.replace('"', '\\"')
        lines.append(f'    ("{sym}", "{safe_name}"),')
    lines.append("]")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _load_universe():
    """prime universeを読み込む。"""
    try:
        from symbols_listed_all import SYMBOLS
        return SYMBOLS
    except ImportError:
        raise SystemExit(
            "symbols_listed_all.py が存在しません。\n"
            "  python download_tse_symbols.py --market prime\n"
            "を先に実行してください"
        )


def main():
    parser = argparse.ArgumentParser(description="レジーム別 Donchian winners 抽出")
    parser.add_argument("--days", type=int, default=730,
                        help="backtest期間 (デフォルト: 730日)")
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--max-price", type=int, default=10_000,
                        help="この価格以下の銘柄のみ対象 (デフォルト: 10,000円。"
                             "予算とは独立して銘柄候補を広く取る)")
    parser.add_argument("--min-pf", type=float, default=1.3,
                        help="抽出: 最低PF (デフォルト: 1.3)")
    parser.add_argument("--min-trades", type=int, default=20,
                        help="抽出: 最低取引数 (デフォルト: 20)")
    parser.add_argument("--min-pnl", type=int, default=10000,
                        help="抽出: 最低損益円 (デフォルト: 10,000)")
    args = parser.parse_args()

    print("=" * 80)
    print(f"  レジーム別 Donchian winners 抽出 ({args.days}日)")
    print("=" * 80)

    # Step 1: 日経レジーム判定
    print("\n[Step 1/4] 日経225 レジーム判定", flush=True)
    years = max(1, args.days // 365 + 1)
    regime_map = fetch_nikkei_regimes(years)

    # Step 2: prime backtest
    print(f"\n[Step 2/4] prime universe ロード", flush=True)
    targets = _load_universe()
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    max_price = args.max_price
    before = len(fetched)
    fetched = {s: df for s, df in fetched.items()
               if float(df.iloc[-1]["close"]) <= max_price}
    targets = [(s, n) for s, n in targets if s in fetched]
    print(f"  対象: {len(targets)}銘柄  "
          f"(価格フィルタ ≤{max_price:,}円: {before}→{len(fetched)})",
          flush=True)

    print(f"\n[Step 3/4] 全銘柄 backtest 実行中...", flush=True)
    t0 = time.time()
    # 銘柄ごとのレジーム別集計
    by_regime: dict[str, list[tuple[str, str, dict]]] = {r: [] for r in REGIMES}

    for i, (sym, name) in enumerate(targets, 1):
        r = backtest_symbol(sym, name, fetched[sym], args.budget, args.max_risk)
        if not r or not r["trades"]:
            continue
        # トレードをレジーム別に分類
        regime_trades = trades_by_regime(r["trades"], regime_map)
        for regime, trades in regime_trades.items():
            if not trades:
                continue
            stats = calc_stats(trades, args.budget)
            by_regime[regime].append((sym, name, stats))
        if i % 50 == 0 or i == len(targets):
            print(f"    {i}/{len(targets)} 処理済み ({time.time()-t0:.1f}s)",
                  flush=True)

    # Step 4: 各レジームで winners 抽出 & 保存
    print(f"\n[Step 4/4] レジーム別 winners 抽出", flush=True)
    print()
    summary = []
    for regime in REGIMES:
        candidates = by_regime[regime]
        # フィルタ
        filtered = [
            (sym, name, stats) for sym, name, stats in candidates
            if stats["pf"] >= args.min_pf
            and stats["n"] >= args.min_trades
            and stats["total_pnl"] >= args.min_pnl
        ]
        # PFでソート (高い順)
        filtered.sort(key=lambda x: (-x[2]["total_pnl"], -x[2]["pf"]))

        items = [(sym, name) for sym, name, _ in filtered]
        if items:
            out = write_winners(regime, items)
            summary.append((regime, len(items), out, filtered))
        else:
            print(f"  [{REGIME_LABEL[regime]}] 該当銘柄なし")

    # サマリ表示
    print()
    print("=" * 80)
    print(f"  抽出結果サマリ (PF≥{args.min_pf} & 取引≥{args.min_trades} & 損益≥{args.min_pnl:,})")
    print("=" * 80)
    for regime, count, out, filtered in summary:
        print(f"\n【{REGIME_LABEL[regime]}】 {count}銘柄 → {out.name}")
        print(f"{'銘柄':<22} {'コード':<10} {'取引':>4} {'勝率':>5} {'PF':>5} {'損益':>10}")
        print("-" * 70)
        for sym, name, stats in filtered[:15]:
            disp = name[:20] if len(name) <= 20 else name[:19] + "…"
            pf_str = "∞" if stats["pf"] == float("inf") else f"{stats['pf']:.2f}"
            print(f"{disp:<22} {sym:<10} {stats['n']:>4} "
                  f"{stats['win_rate']:>4.0f}% {pf_str:>5} "
                  f"{stats['total_pnl']:>+10,.0f}")
        if len(filtered) > 15:
            print(f"... 他 {len(filtered)-15}銘柄")

    print()
    print("=" * 80)
    print("生成ファイル:")
    for regime, count, out, _ in summary:
        print(f"  - {out}  ({count}銘柄, {REGIME_LABEL[regime]}相場用)")
    print("=" * 80)


if __name__ == "__main__":
    main()
