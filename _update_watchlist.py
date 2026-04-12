"""scan結果からwatchlistを生成する一時スクリプト"""
import pandas as pd

df = pd.read_csv("scan_orb_result.csv")
g = df[(df["n"] >= 10) & (df["pf"] < 100) & (df["pf"] >= 1.5)]
g = g.sort_values("pf", ascending=False)
print(f"{len(g)}銘柄")

lines = [
    '"""ORBスキャン選定銘柄 (PF>=1.5 & 取引>=10)"""\n\n',
    "DAYTRADE_SYMBOLS = [\n",
]
for _, r in g.iterrows():
    lines.append(f'    ("{r.symbol}", "{r.symbol}"),\n')
lines.append("]\n")

with open("daytrade_symbols.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
print("daytrade_symbols.py を更新しました")
