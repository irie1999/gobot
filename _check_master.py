"""J-Quants の銘柄マスターの列名とデータ形式を確認"""
import os
from pathlib import Path

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

import jquantsapi
cli = jquantsapi.ClientV2(api_key=os.environ["JQUANTS_API_KEY"])
df = cli.get_eq_master()

print("columns:", list(df.columns))
print()
print("先頭3行:")
print(df.head(3).to_string())
print()
print("Code列サンプル:", df["Code"].head(5).tolist())

# 名前っぽい列を探す
for col in df.columns:
    if "name" in col.lower() or "company" in col.lower() or "銘柄" in col:
        print(f"\n{col}列サンプル:", df[col].head(3).tolist())
