"""
4戦略バックテスト 総合ランキング出力
======================================
■ 概要
  4つのバックテストをまとめて実行し、推奨銘柄ランキングをターミナルに出力する。
  ・資金フィルター : 株価 ≤ 5,000円（100株 × 5,000円 = 500,000円以内）
  ・スコア計算    : 30日損益 × 50% + 90日損益 × 25% + 180日損益 × 15% + 365日損益 × 10%
  ・各戦略ごとにTOP20を表示

■ 実行方法
  # ① 銘柄ダウンロード（初回のみ）
  python download_tse_symbols.py --market prime

  # ② ランキング実行（1800銘柄 × 4戦略）
  python run_ranking.py

  # ③ バックテストをスキップしてCSVのみ再集計
  python run_ranking.py --no-run
"""

import argparse
import csv
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST        = timezone(timedelta(hours=9))
MAX_PRICE  = 5_000        # 100株 × 5,000円 = 500,000円
PERIODS    = [30, 90, 180, 365]
WEIGHTS    = {30: 0.50, 90: 0.25, 180: 0.15, 365: 0.10}
TOP_N      = 20

STRATEGIES = [
    ("limit_oco",         "backtest_limit_oco.py",         "指値OCO戦略"),
    ("adaptive_mr",       "backtest_adaptive_mr.py",       "アダプティブMR戦略"),
    ("donchian_pullback", "backtest_donchian_pullback.py", "ドンチャン押し目戦略"),
    ("bb_volume",         "backtest_bb_volume.py",         "BB出来高戦略"),
]


def run_backtests(universe: str, days: int) -> None:
    """4戦略を順番に実行してCSVを生成する。"""
    for key, script, name in STRATEGIES:
        print(f"\n[{name}] バックテスト実行中... (universe={universe} / {days}日)", flush=True)
        result = subprocess.run(
            [sys.executable, script,
             "--universe", universe,
             "--days",     str(days),
             "--no-browser"],
            capture_output=False,
        )
        if result.returncode != 0:
            print(f"  ※ 警告: {script} が異常終了しました (code={result.returncode})")


def load_csv(key: str) -> list[dict]:
    """candidates_{key}.csv を読み込んで辞書リストを返す。"""
    f = Path(f"candidates_{key}.csv")
    if not f.exists():
        return []
    rows = []
    with open(f, newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for r in reader:
            try:
                entry = {
                    "symbol":     r["symbol"],
                    "name":       r["name"],
                    "last_close": float(r.get("last_close") or 0),
                }
                for d in PERIODS:
                    entry[f"{d}_n"]     = int(  r.get(f"{d}_n",     0) or 0)
                    entry[f"{d}_wr"]    = float(r.get(f"{d}_wr",    0) or 0)
                    entry[f"{d}_pf"]    = float(r.get(f"{d}_pf",    0) or 0)
                    entry[f"{d}_total"] = float(r.get(f"{d}_total", 0) or 0)
                rows.append(entry)
            except (ValueError, KeyError):
                continue
    return rows


def weighted_score(r: dict) -> float:
    """加重スコア: 30日×50% + 90日×25% + 180日×15% + 365日×10%"""
    return sum(WEIGHTS[d] * r[f"{d}_total"] for d in PERIODS)


def fmt_pnl(v: float, width: int = 9) -> str:
    if v == 0:
        return " " * (width - 1) + "—"
    return f"{v:>+{width},.0f}"


def print_ranking(key: str, name: str) -> None:
    data = load_csv(key)
    if not data:
        print(f"\n【{name}】データなし（candidates_{key}.csv が見つかりません）\n")
        return

    # 価格フィルター: 0 < 株価 ≤ MAX_PRICE
    filtered = [r for r in data if 0 < r["last_close"] <= MAX_PRICE]
    filtered.sort(key=weighted_score, reverse=True)
    top = filtered[:TOP_N]

    total_after_filter = len(filtered)
    print(f"\n{'─'*112}")
    print(f"  【{name}】推奨ランキング TOP{TOP_N}")
    print(f"  フィルター後: {total_after_filter}銘柄 / 全{len(data)}銘柄  "
          f"（株価 ≤ {MAX_PRICE:,}円 / 100株 ≤ {MAX_PRICE*100:,}円）")
    print(f"{'─'*112}")
    hdr = (f"  {'順':>3}  {'コード':10}  {'銘柄名':20}  {'株価':>6}  "
           f"{'30日損益':>9}  {'90日損益':>9}  {'180日損益':>9}  {'365日損益':>9}  {'スコア':>9}")
    print(hdr)
    print(f"  {'─'*107}")
    for i, r in enumerate(top, 1):
        s = weighted_score(r)
        print(f"  {i:>3}  {r['symbol']:10}  {r['name']:20}  {r['last_close']:>6,.0f}  "
              f"{fmt_pnl(r['30_total'])}  {fmt_pnl(r['90_total'])}  "
              f"{fmt_pnl(r['180_total'])}  {fmt_pnl(r['365_total'])}  {s:>+9,.0f}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="4戦略バックテスト 総合ランキング",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python run_ranking.py                        # 全実行（1800銘柄 / 365日）
  python run_ranking.py --universe 225         # 日経225のみ
  python run_ranking.py --days 180             # 180日まで評価
  python run_ranking.py --no-run               # バックテストをスキップ（CSV再集計のみ）
""")
    parser.add_argument("--universe", default="all",
                        choices=["watch", "225", "all"],
                        help="銘柄ユニバース（all = symbols_listed_all.py）")
    parser.add_argument("--days",     type=int, default=365,
                        help="バックテスト期間（日）")
    parser.add_argument("--no-run",   action="store_true",
                        help="バックテスト実行をスキップ（既存CSVのみ使用）")
    args = parser.parse_args()

    today = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    print(f"\n{'='*112}")
    print(f"  バックテスト総合ランキング  スキャン: {today}")
    print(f"  ウェイト: 30日 ×50%  90日 ×25%  180日 ×15%  365日 ×10%")
    print(f"  資金上限: {MAX_PRICE*100:,}円（100株 × {MAX_PRICE:,}円以下の銘柄のみ）")
    print(f"{'='*112}")

    if not args.no_run:
        run_backtests(args.universe, args.days)

    for key, _, name in STRATEGIES:
        print_ranking(key, name)

    print("完了。各HTMLレポートも参照してください。")


if __name__ == "__main__":
    main()
