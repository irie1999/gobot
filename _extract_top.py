"""統合バックテスト結果から上位銘柄を抽出"""
import os
from pathlib import Path
for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip('"')
        break

from daytrade_symbols import DAYTRADE_SYMBOLS
from daytrade_data import load_intraday_batch, split_by_day, calc_position_size
from daytrade_integrated import backtest_symbol, calc_stats

budget = 600_000
max_risk = 6_000
targets = DAYTRADE_SYMBOLS
symbols = [s for s, _ in targets]

print(f"データ取得中 ({len(symbols)}銘柄)...")
fetched = load_intraday_batch(symbols, 60, source="yfinance")
fetched = {s: df for s, df in fetched.items()
           if float(df.iloc[-1]["close"]) <= budget / 100}

results = []
for sym, name in targets:
    if sym not in fetched:
        continue
    r = backtest_symbol(sym, name, fetched[sym], budget, max_risk)
    if r and r["trades"]:
        s = calc_stats(r["trades"], budget)
        if s["n"] >= 5 and s["pf"] >= 1.3:
            results.append({"sym": sym, "name": name, **s})

results.sort(key=lambda x: x["total_pnl"], reverse=True)

print(f"\n上位20銘柄 (取引≥5 & PF≥1.3 → 損益順)")
print(f"{'#':>2} {'銘柄名':<20} {'コード':<10} {'株価':>6} {'取引':>4} "
      f"{'勝率':>5} {'PF':>5} {'損益':>10}")
print("-" * 75)
for i, r in enumerate(results[:20], 1):
    price = float(fetched[r["sym"]].iloc[-1]["close"])
    print(f"{i:>2} {r['name']:<20} {r['sym']:<10} {price:>5,.0f} "
          f"{r['n']:>4} {r['win_rate']:>4.0f}% {r['pf']:>5.2f} "
          f"{r['total_pnl']:>+10,.0f}")

print(f"\n推薦: 上位5銘柄に集中")
top5 = results[:5]
for r in top5:
    print(f"  ★ {r['name']} ({r['sym']}) PF={r['pf']:.2f} 損益={r['total_pnl']:+,.0f}")
