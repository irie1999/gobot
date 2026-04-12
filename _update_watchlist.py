"""scan結果からwatchlistを生成する (銘柄名付き)"""
import os
from pathlib import Path
import pandas as pd

# .env 読み込み
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

df = pd.read_csv("scan_orb_result.csv")
g = df[(df["n"] >= 10) & (df["pf"] < 100) & (df["pf"] >= 1.5)]
g = g.sort_values("pf", ascending=False)
print(f"{len(g)}銘柄")

# J-Quants から銘柄名を取得
names = {}
try:
    import jquantsapi
    api_key = os.environ.get("JQUANTS_API_KEY")
    if api_key:
        cli = jquantsapi.ClientV2(api_key=api_key)
        master = cli.get_eq_master()
        if master is not None and not master.empty:
            for _, row in master.iterrows():
                code = str(row.get("Code", ""))
                name = str(row.get("CompanyName", ""))
                if len(code) >= 4 and name:
                    yf_code = code[:4] + ".T"
                    names[yf_code] = name
            print(f"  銘柄名: {len(names)}件取得")
except Exception as e:
    print(f"  銘柄名取得失敗: {e}")

lines = [
    '"""ORBスキャン選定銘柄 (PF>=1.5 & 取引>=10)"""\n\n',
    "DAYTRADE_SYMBOLS = [\n",
]
for _, r in g.iterrows():
    sym = r.symbol
    name = names.get(sym, sym)
    lines.append(f'    ("{sym}", "{name}"),\n')
lines.append("]\n")

with open("daytrade_symbols.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
print(f"daytrade_symbols.py を更新しました ({len(g)}銘柄)")
