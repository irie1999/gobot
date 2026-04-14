"""Donchian監視銘柄選定スクリプト"""
import os
from pathlib import Path
import pandas as pd

# .env
for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        break

df = pd.read_csv("scan_donchian_result.csv")

# フィルタ条件:
# - 取引数 ≥ 10 (統計的に意味のある数)
# - PF ≥ 2.0 (高品質)
# - PF < 50 (極端な数値を除外)
# - 株価 ≤ 6000円 (予算60万で購入可能)
# - 損益 > 0
g = df[
    (df["n"] >= 10)
    & (df["pf"] >= 2.0)
    & (df["pf"] < 50)
    & (df["price"] <= 6000)
    & (df["total_pnl"] > 0)
].sort_values("pf", ascending=False)

print(f"フィルタ後 (取引≥10 / PF 2.0-50 / 株価≤6000): {len(g)}銘柄")
print()

# J-Quants から銘柄名取得
names = {}
try:
    import jquantsapi
    cli = jquantsapi.ClientV2(api_key=os.environ["JQUANTS_API_KEY"])
    master = cli.get_eq_master()
    if master is not None and not master.empty:
        for _, row in master.iterrows():
            code = str(row.get("Code", ""))
            name = str(row.get("CoName", ""))
            if len(code) >= 4 and name:
                yf_code = code[:4] + ".T"
                names[yf_code] = name
        print(f"銘柄名取得: {len(names)}件")
        print()
except Exception as e:
    print(f"銘柄名取得失敗: {e}")

# 上位30銘柄を表示
print(f"{'#':>3} {'銘柄名':<25} {'コード':<10} {'株価':>6} "
      f"{'取引':>4} {'勝率':>5} {'PF':>6} {'損益':>10}")
print("-" * 80)
for i, (_, r) in enumerate(g.head(30).iterrows(), 1):
    name = names.get(r["symbol"], r["symbol"])[:22]
    print(f"{i:>3} {name:<25} {r['symbol']:<10} {r['price']:>5,.0f} "
          f"{r['n']:>4} {r['wr']:>4.0f}% {r['pf']:>5.2f} "
          f"{r['total_pnl']:>+10,.0f}")

# 上位20銘柄を daytrade_symbols.py に保存
top_n = 20
top = g.head(top_n)
lines = [
    '"""Donchianスキャン選定銘柄 (PF>=2.0 & 取引>=10 & 株価<=6000)"""\n\n',
    "DAYTRADE_SYMBOLS = [\n",
]
for _, r in top.iterrows():
    sym = r["symbol"]
    name = names.get(sym, sym)
    lines.append(f'    ("{sym}", "{name}"),\n')
lines.append("]\n")

with open("daytrade_symbols.py", "w", encoding="utf-8") as f:
    f.writelines(lines)

print()
print(f"daytrade_symbols.py を更新しました ({top_n}銘柄)")

# kabu_daytrade_bot.py 用のWATCH_SYMBOLS形式も出力
print()
print("=" * 80)
print("kabu_daytrade_bot.py 用 WATCH_SYMBOLS (コピペ用):")
print("=" * 80)
print("WATCH_SYMBOLS = [")
for _, r in top.iterrows():
    sym4 = r["symbol"].replace(".T", "")
    name = names.get(r["symbol"], r["symbol"])
    print(f'    ("{sym4}", "{name}"),')
print("]")
