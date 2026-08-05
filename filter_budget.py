"""
filter_budget.py  ―  予算内で購入可能な監視銘柄の選定
======================================================
強い押し目（strong_pullback）の監視銘柄17銘柄から、
指定予算で100株購入できる銘柄を絞り込み、バックテスト成績も表示する。

【使い方】
  python filter_budget.py                    # デフォルト50万円
  python filter_budget.py --budget 300000    # 30万円以内
  python filter_budget.py --budget 1000000   # 100万円以内
  python filter_budget.py --no-browser       # HTML生成のみ
"""

from __future__ import annotations

import argparse
import webbrowser
from _open_html import open_html
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

import backtest_strong_pullback as _sp

JST      = timezone(timedelta(hours=9))
LOT_SIZE = 100

# ── 監視リスト（17銘柄）────────────────────────────────────────
WATCHLIST: list[tuple[str, str]] = [
    ("6146.T", "ディスコ"),
    ("6871.T", "日本マイクロニクス"),
    ("7236.T", "ティラド"),
    ("7012.T", "川崎重工業"),
    ("5108.T", "ブリヂストン"),
    ("4025.T", "多木化学"),
    ("8218.T", "コメリ"),
    ("3946.T", "トーモク"),
    ("6183.T", "ベルシステム２４ホールディングス"),
    ("3636.T", "三菱総合研究所"),
    ("8153.T", "モスフードサービス"),
    ("8194.T", "ライフコーポレーション"),
    ("9046.T", "神戸電鉄"),
    ("9405.T", "朝日放送グループホールディングス"),
    ("8081.T", "カナデン"),
    ("6454.T", "マックス"),
    ("6196.T", "ストライク"),
]


def fetch_latest_prices(symbols: list[str]) -> dict[str, float]:
    """現在の株価を一括取得"""
    tickers = yf.Tickers(" ".join(symbols))
    prices: dict[str, float] = {}
    for sym in symbols:
        try:
            hist = tickers.tickers[sym].history(period="5d")
            if not hist.empty:
                prices[sym] = float(hist["Close"].iloc[-1])
        except Exception:
            pass
    return prices


def run_backtest(symbols: list[str], days: int = 365) -> dict[str, dict]:
    """各銘柄のバックテスト結果を取得"""
    results: dict[str, dict] = {}
    for sym, _ in WATCHLIST:
        if sym not in symbols:
            continue
        try:
            df = _sp.fetch_ohlcv(sym, days + 400)
            if df is None or df.empty:
                continue
            trades = _sp.backtest_strong_pullback(df, days)
            stats  = _sp.calc_stats(trades)
            results[sym] = {"trades": trades, "stats": stats}
        except Exception:
            pass
    return results


def build_html(
    affordable: list[dict],
    unaffordable: list[dict],
    budget: int,
    days: int,
) -> str:
    today_str = datetime.now(tz=JST).strftime("%Y-%m-%d")

    def fmt_pf(stats: dict) -> str:
        n = stats.get("n", 0)
        if n == 0:
            return "―"
        pf = stats.get("pf", 0)
        if pf == float("inf"):
            return "∞"
        return f"{pf:.2f}"

    def fmt_wr(stats: dict) -> str:
        n = stats.get("n", 0)
        if n == 0:
            return "―"
        return f"{stats.get('win_rate', 0):.0f}%"

    def fmt_total(stats: dict) -> str:
        n = stats.get("n", 0)
        if n == 0:
            return "―"
        total = stats.get("total_pnl", 0)
        sign  = "+" if total >= 0 else ""
        return f"{sign}{total:,.0f}円"

    def row_cls(stats: dict) -> str:
        n     = stats.get("n", 0)
        total = stats.get("total_pnl", 0)
        if n == 0:
            return "no-trade"
        return "profit" if total >= 0 else "loss"

    def build_rows(items: list[dict]) -> str:
        rows = ""
        for i, item in enumerate(items, 1):
            sym    = item["symbol"]
            name   = item["name"]
            price  = item["price"]
            cost   = item["cost"]
            stats  = item.get("stats", {})
            n      = stats.get("n", 0)
            cls    = row_cls(stats)
            rows += f"""
        <tr class="{cls}">
          <td class="num">{i}</td>
          <td><a href="https://finance.yahoo.co.jp/quote/{sym}" target="_blank">{sym}</a></td>
          <td>{name}</td>
          <td class="num">{price:,.0f}円</td>
          <td class="num">{cost:,.0f}円</td>
          <td class="num">{n}回</td>
          <td class="num">{fmt_wr(stats)}</td>
          <td class="num">{fmt_pf(stats)}</td>
          <td class="num">{fmt_total(stats)}</td>
        </tr>"""
        return rows

    affordable_rows   = build_rows(affordable)
    unaffordable_rows = build_rows(unaffordable)

    unaffordable_section = ""
    if unaffordable:
        unaffordable_rows_html = build_rows(unaffordable)
        unaffordable_section = f"""
    <h2 style="margin-top:40px; color:#ef4444;">予算オーバー銘柄（{len(unaffordable)}銘柄）</h2>
    <p style="color:#94a3b8; font-size:0.9em;">100株購入に予算（{budget:,}円）を超える銘柄</p>
    <table>
      <thead>
        <tr>
          <th>#</th><th>コード</th><th>銘柄名</th>
          <th>現在値</th><th>必要資金(100株)</th>
          <th>取引回数</th><th>勝率</th><th>PF</th><th>合計損益({days}日)</th>
        </tr>
      </thead>
      <tbody>
        {unaffordable_rows_html}
      </tbody>
    </table>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>予算別銘柄選定 | 強い押し目</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0f172a; color: #e2e8f0; font-family: 'Segoe UI', sans-serif;
           font-size: 14px; padding: 24px; }}
    h1 {{ font-size: 1.4em; color: #38bdf8; margin-bottom: 6px; }}
    h2 {{ font-size: 1.1em; color: #34d399; margin-bottom: 8px; }}
    .meta {{ color: #64748b; font-size: 0.85em; margin-bottom: 24px; }}
    .summary-box {{
      background: #1e293b; border-radius: 8px; padding: 16px 24px;
      display: inline-block; margin-bottom: 24px;
    }}
    .summary-box span {{ color: #38bdf8; font-size: 1.3em; font-weight: bold; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
    thead tr {{ background: #1e293b; }}
    th {{ padding: 8px 12px; text-align: center; color: #94a3b8;
          font-weight: 600; border-bottom: 1px solid #334155;
          white-space: nowrap; position: sticky; top: 0; z-index: 1; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #1e293b; white-space: nowrap; }}
    td.num {{ text-align: right; }}
    tr.profit:hover {{ background: #1e293b; }}
    tr.loss   {{ opacity: 0.75; }}
    tr.loss:hover {{ background: #1e293b; }}
    tr.no-trade {{ opacity: 0.5; }}
    a {{ color: #38bdf8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{
      display: inline-block; padding: 2px 10px; border-radius: 12px;
      font-size: 0.8em; font-weight: bold; margin-left: 8px;
    }}
    .badge-ok   {{ background: #064e3b; color: #34d399; }}
    .badge-over {{ background: #450a0a; color: #ef4444; }}
  </style>
</head>
<body>
  <h1>予算別銘柄選定 ― 強い押し目（strong_pullback）</h1>
  <div class="meta">生成日時: {today_str} ／ バックテスト期間: 直近{days}日 ／ 予算: {budget:,}円</div>

  <div class="summary-box">
    予算 <span>{budget:,}円</span> で購入可能：
    <span style="color:#34d399;">{len(affordable)}銘柄</span>
    ／ 全17銘柄中
  </div>

  <h2>購入可能銘柄（{len(affordable)}銘柄）
    <span class="badge badge-ok">100株 ≤ {budget:,}円</span>
  </h2>
  <table>
    <thead>
      <tr>
        <th>#</th><th>コード</th><th>銘柄名</th>
        <th>現在値</th><th>必要資金(100株)</th>
        <th>取引回数</th><th>勝率</th><th>PF</th><th>合計損益({days}日)</th>
      </tr>
    </thead>
    <tbody>
      {affordable_rows}
    </tbody>
  </table>

  {unaffordable_section}
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="予算内で購入可能な強い押し目銘柄を選定")
    parser.add_argument("--budget",     type=int, default=500_000, help="予算（円）デフォルト500000")
    parser.add_argument("--days",       type=int, default=365,     help="バックテスト日数 (default: 365)")
    parser.add_argument("--no-browser", action="store_true",       help="ブラウザを開かない")
    args = parser.parse_args()

    budget = args.budget
    days   = args.days

    print(f"予算: {budget:,}円  /  バックテスト: 直近{days}日")
    print(f"現在の株価を取得中...")

    symbols = [sym for sym, _ in WATCHLIST]
    prices  = fetch_latest_prices(symbols)

    # 予算で分類
    affordable:   list[tuple[str, str, float]] = []
    unaffordable: list[tuple[str, str, float]] = []

    for sym, name in WATCHLIST:
        price = prices.get(sym)
        if price is None:
            print(f"  ⚠ {sym} 価格取得失敗")
            continue
        cost = price * LOT_SIZE
        if cost <= budget:
            affordable.append((sym, name, price))
        else:
            unaffordable.append((sym, name, price))

    print(f"\n購入可能: {len(affordable)}銘柄  /  予算オーバー: {len(unaffordable)}銘柄")
    print("\nバックテスト実行中...")

    all_syms = [s for s, _, _ in affordable + unaffordable]
    bt_results = run_backtest(all_syms, days)

    def make_items(lst: list[tuple[str, str, float]]) -> list[dict]:
        items = []
        for sym, name, price in lst:
            stats = bt_results.get(sym, {}).get("stats", {})
            items.append(dict(
                symbol=sym, name=name, price=price,
                cost=price * LOT_SIZE, stats=stats,
            ))
        # 合計損益の高い順にソート
        items.sort(key=lambda x: x["stats"].get("total_pnl", 0), reverse=True)
        return items

    affordable_items   = make_items(affordable)
    unaffordable_items = make_items(unaffordable)

    # ターミナル出力
    print(f"\n{'─'*70}")
    print(f"{'コード':<10} {'銘柄名':<22} {'現在値':>8} {'必要資金':>10} {'取引':>4} {'勝率':>5} {'PF':>5} {'合計損益':>12}")
    print(f"{'─'*70}")
    for item in affordable_items:
        stats = item["stats"]
        n     = stats.get("n", 0)
        wr    = f"{stats.get('win_rate',0):.0f}%" if n else "―"
        pf    = f"{stats.get('pf',0):.2f}"         if n else "―"
        total = f"{stats.get('total_pnl',0):+,.0f}" if n else "―"
        print(f"{item['symbol']:<10} {item['name']:<22} {item['price']:>8,.0f} {item['cost']:>10,.0f} {n:>4} {wr:>5} {pf:>5} {total:>12}")

    if unaffordable_items:
        print(f"\n予算オーバー銘柄:")
        for item in unaffordable_items:
            print(f"  {item['symbol']:<10} {item['name']:<22}  現在値: {item['price']:,.0f}円  必要: {item['cost']:,.0f}円")

    # HTML出力
    html = build_html(affordable_items, unaffordable_items, budget, days)
    out  = Path(f"budget_{budget//10000}万円.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML出力: {out}")

    if not args.no_browser:
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
