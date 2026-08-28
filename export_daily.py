r"""export_daily.py — 日足を1ファイルに書き出す(他の環境へ渡す用)

なぜ日足だけで足りるのか
------------------------
N / 鏡像は **寄りで建てて引けで閉じるだけ**で、損切りも利確も置きません。
つまり「その日の高値と安値のどちらが先に付いたか」を知る必要がありません。
使う値は全部 日足の中にあります:

  前日リターン   終値 ÷ 前々日終値
  ギャップ判定   始値 ÷ 前日終値
  建値           始値
  決済           終値
  流動性         終値 × 出来高 の20日平均
  価格帯         始値

→ **このファイル1つで、渡した先の環境だけで検証が完結します。**

⛔ 5分足は渡せません(現実的でない)
   1,540銘柄 × 約500営業日 × 約60バー = **4,600万行**。数百MB〜1GB になります。
   5分足が要るのは「同一日内の順序」が必要な案(損切りを入れる/途中で降りる/
   寄り直後の数分を測る)だけなので、まずは日足で案を出してもらってください。

★ 意図的に「生のまま」出します
   前日リターン・ギャップ・売買代金といった**派生列は作りません**。
   こちらの前処理を入れると、それ自体が渡した先への先入観になります。
   出すのは date, symbol, open, high, low, close, volume の7列だけです。

使い方
------
  python export_daily.py                          # 既定 19年 / プライム全銘柄
  python export_daily.py --days 4200              # 11.6年ぶん
  python export_daily.py --limit 300              # 先頭300銘柄(お試し)
  python export_daily.py --out daily.parquet
  python export_daily.py --csv                    # parquet が使えないとき

⚠ キャッシュ(.rsi2_cache)に無い銘柄は yfinance から落とします。
  初回は時間がかかります。普段 .\daily を回していれば大半は温まっています。
⚠ 照会のみ。発注はしません。
"""
from __future__ import annotations

import argparse
import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from backtest_limit_entry import fetch


def _load_symbols(path: str) -> list[str]:
    mod = importlib.import_module(Path(path).stem)
    for name in ("SYMBOLS", "SYMBOL_LIST", "ALL_SYMBOLS"):
        v = getattr(mod, name, None)
        if not v:
            continue
        # ("7203.T", "トヨタ") のタプルでも、文字列だけでも受ける
        return [x[0] if isinstance(x, (tuple, list)) else str(x) for x in v]
    sys.exit(f"[error] {path} に銘柄リストが見つかりません")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="symbols_listed_prime.py")
    ap.add_argument("--days", type=int, default=7000,
                    help="遡る日数(暦日)。既定7000 ≒ 19年")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(お試し)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="daily_ohlcv.parquet")
    ap.add_argument("--csv", action="store_true",
                    help="parquet ではなく csv.gz で書く")
    a = ap.parse_args()

    syms = _load_symbols(a.symbols)
    if a.limit > 0:
        syms = syms[:a.limit]
    print(f"[info] 銘柄 {len(syms):,} / 遡り {a.days:,}日 / 並列 {a.workers}")

    _min = (pd.Timestamp.today().normalize()
            - pd.Timedelta(days=a.days)).date()

    def _one(sym: str):
        try:
            df = fetch(sym, a.days, min_start_date=_min)
        except Exception:
            return None
        if df is None or df.empty:
            return None
        out = df[["open", "high", "low", "close", "volume"]].copy()
        out.insert(0, "symbol", sym)
        return out

    rows, ng = [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(_one, syms), 1):
            if r is None:
                ng += 1
            else:
                rows.append(r)
            if i % 200 == 0:
                print(f"  … {i:,}/{len(syms):,}(取れず {ng:,})", flush=True)

    if not rows:
        sys.exit("[error] 1銘柄も取れませんでした")

    df = pd.concat(rows).rename_axis("date").reset_index()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["date", "symbol"], ignore_index=True)

    out = Path(a.out)
    if a.csv or out.suffix != ".parquet":
        out = out.with_suffix("") if out.suffix == ".parquet" else out
        out = Path(str(out) + (".csv.gz" if not str(out).endswith(".csv.gz")
                               else ""))
        df.to_csv(out, index=False, compression="gzip", encoding="utf-8")
    else:
        try:
            df.to_parquet(out, index=False, compression="snappy")
        except Exception as e:
            print(f"[warn] parquet で書けません({e}) → csv.gz にします")
            out = Path(str(out.with_suffix("")) + ".csv.gz")
            df.to_csv(out, index=False, compression="gzip", encoding="utf-8")

    _mb = out.stat().st_size / 1024 / 1024
    print(f"\n[done] {out}  {_mb:,.1f} MB")
    print(f"  行数     {len(df):,}")
    print(f"  銘柄     {df['symbol'].nunique():,}(取れず {ng:,})")
    print(f"  期間     {df['date'].min()} 〜 {df['date'].max()}"
          f"({df['date'].nunique():,}営業日)")
    print(f"  列       {', '.join(df.columns)}")
    print(f"\n  ⚠ 派生列(前日リターン・ギャップ・売買代金)は**入れていません**。")
    print(f"     前処理を入れると、それ自体が渡した先への先入観になります。")
    print(f"  ⚠ 株式分割は **遡及調整済み**です(日足のみ。5分足は未調整)。")


if __name__ == "__main__":
    main()
