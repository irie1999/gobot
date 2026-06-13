"""
forward_test_daytrade.py  ―  デイトレ フォワードテスト (ペーパートレード継続評価)
==================================================================
逆指値ロング (forward_test.py) を5分足デイトレ用に移植。

【目的】
WATCHLIST + 戦略の組み合わせを、過去データではなく
「直近N日 + 今日」の実シグナルで評価する。
in-sample bias がない真のフォワード検証。

【使い方】
  python forward_test_daytrade.py                  # 直近30日
  python forward_test_daytrade.py --days 60
  python forward_test_daytrade.py --watchlist daytrade_watchlist_macd.py
  python forward_test_daytrade.py --strategy MACD
"""

from __future__ import annotations

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m
from daytrade_strategies_5m import STRATEGIES
from daytrade_strategies_5m_short import STRATEGIES_SHORT
from risk_metrics_5m import enrich_stats

JST = timezone(timedelta(hours=9))

ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}


def _pf(v):
    if v == float("inf"):
        return "∞"
    return f"{v:.2f}"


def _color_pf(v):
    if v >= 1.5:
        return "#4ade80"
    if v >= 1.0:
        return "#facc15"
    return "#f87171"


def forward_test_one(symbol, name, strategy, days):
    """1銘柄×1戦略を直近days日でフォワードテスト。"""
    fn = ALL_STRATEGIES.get(strategy)
    if fn is None:
        return None
    fetched = load_intraday_batch([symbol], days, source="local")
    df = fetched.get(symbol)
    if df is None or df.empty:
        return None
    r = backtest_symbol_5m(symbol, name, df, fn,
                            strategy_params={"name": strategy})
    if not r:
        return None

    today = datetime.now(JST).date()
    cutoff = today - timedelta(days=days)
    forward_trades = [t for t in r["trades"]
                      if hasattr(t.get("entry_dt"), "date")
                      and t["entry_dt"].date() >= cutoff]
    stats = enrich_stats(forward_trades)
    return dict(
        symbol=symbol, name=name, strategy=strategy,
        days=days, trades=forward_trades, stats=stats,
        last_price=float(df.iloc[-1]["close"]) if not df.empty else 0,
    )


def main():
    parser = argparse.ArgumentParser(description="デイトレ フォワードテスト")
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--strategy", default=None,
                        help="特定戦略のみ実行 (省略でWATCHLISTの strategy 列を使用)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # WATCHLIST
    from check_signals_daytrade import _load_watchlist
    watchlist = _load_watchlist(args.watchlist)
    if not watchlist:
        print(f"[error] WATCHLIST 空")
        return

    targets = []
    for entry in watchlist:
        if len(entry) == 3:
            sym, name, strat = entry
        else:
            sym, name = entry[:2]
            strat = "DON"
        if args.strategy and strat != args.strategy:
            continue
        targets.append((sym, name, strat))

    print(f"フォワードテスト: {len(targets)}件 / 直近{args.days}日 / workers={args.workers}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(forward_test_one, s, n, st, args.days): (s, st)
                for s, n, st in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r:
                results.append(r)
            if i % 20 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)}", flush=True)

    # 損益でソート
    results.sort(key=lambda x: -x["stats"]["total_pnl"])

    # サマリ
    total_n = sum(r["stats"]["n"] for r in results)
    total_pnl = sum(r["stats"]["total_pnl"] for r in results)
    winners = [r for r in results if r["stats"]["total_pnl"] > 0]
    print(f"\n結果: 取引{total_n} / 損益{total_pnl:+,.0f}円 / "
          f"プラス銘柄{len(winners)}/{len(results)}")

    # HTML
    today = datetime.now(JST).strftime("%Y-%m-%d")
    rows = ""
    for r in results:
        s = r["stats"]
        if s["n"] == 0:
            continue
        pc = "profit" if s["total_pnl"] >= 0 else "loss"
        rows += f"""
<tr>
  <td class="sym">{r["name"]}<br><small class="code">{r["symbol"]}</small></td>
  <td>{r["strategy"]}</td>
  <td>{r["last_price"]:,.0f}</td>
  <td>{s["n"]}</td>
  <td>{s["win_rate"]:.0f}%</td>
  <td style="color:{_color_pf(s["pf"])}">{_pf(s["pf"])}</td>
  <td class="{pc}">{s["total_pnl"]:+,.0f}</td>
  <td class="loss">{s["max_dd"]:+.1f}%</td>
  <td>{s["sharpe"]:.2f}</td>
  <td>{s["max_losing_streak"]}</td>
  <td>{s["avg_hold_min"]:.0f}分</td>
</tr>"""

    css = """
body { font-family: "Segoe UI", "Hiragino Sans", sans-serif;
       background: #0f172a; color: #e2e8f0; padding: 20px; }
h1 { color: #10b981; font-size: 1.5rem; }
.subtitle { color: #94a3b8; margin-bottom: 20px; font-size: 0.85rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th { background: #1e293b; color: #94a3b8; padding: 6px 8px;
     text-align: center; border: 1px solid #334155; }
td { padding: 5px 8px; border: 1px solid #1e293b;
     text-align: right; white-space: nowrap; }
.sym { text-align: left; font-weight: 600; }
.code { color: #64748b; font-size: 0.72rem; }
.profit { color: #4ade80; }
.loss { color: #f87171; }
.box { background: #1e293b; padding: 14px; border-radius: 8px;
       margin-bottom: 14px; display: flex; gap: 24px; }
.box .it { text-align: center; }
.box .lb { color: #94a3b8; font-size: 0.72rem; }
.box .vl { font-size: 1.3rem; font-weight: 700; }
"""

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>フォワードテスト — {today}</title>
<style>{css}</style></head><body>
<h1>🔮 デイトレ フォワードテスト ({args.days}日)</h1>
<p class="subtitle">生成: {today} / 対象: {len(targets)}件 / 結果: {len(results)}件</p>
<div class="box">
  <div class="it"><div class="lb">取引数</div><div class="vl">{total_n}</div></div>
  <div class="it"><div class="lb">総損益</div>
    <div class="vl {'profit' if total_pnl >= 0 else 'loss'}">{total_pnl:+,.0f}円</div></div>
  <div class="it"><div class="lb">プラス銘柄</div>
    <div class="vl profit">{len(winners)}</div></div>
  <div class="it"><div class="lb">勝率 (銘柄)</div>
    <div class="vl">{len(winners)*100//max(len(results),1)}%</div></div>
</div>
<table>
  <thead><tr>
    <th>銘柄</th><th>戦略</th><th>現値</th><th>取引</th><th>勝率</th><th>PF</th>
    <th>損益</th><th>DD</th><th>Sharpe</th><th>連敗</th><th>平均保有</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</body></html>"""

    out = Path(f"forward_test_daytrade_{today}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\n生成: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
