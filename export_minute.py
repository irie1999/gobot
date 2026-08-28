r"""export_minute.py — 5分足を「必要な銘柄日だけ」書き出す(他の環境へ渡す用)

なぜ全量ではないのか
--------------------
1,540銘柄 × 約490営業日 × 約66バー = **約5,000万行**(500MB〜1GB)。
そのまま渡すのは現実的ではありません。

一方でこの戦略は **同日決済**なので、必要なのは「候補になった銘柄日」の分だけです。
候補かどうかは前日の日足だけで決まる(前日リターン)ので、日足で先に絞ってから
その日の分足だけを切り出せば、桁が2つ落ちます。

  |前日リターン| >= 1.0%   → 約1,400万行(150〜250MB)
  |前日リターン| >= 1.75%  → 約 600万行( 70〜100MB)

⚠ **既定は 1.0%(緩め)** にしてあります。1.75% で切ると現行ルールの条件そのものに
  なってしまい、渡した先が別の切り口を思いついても検証できなくなるためです。

⛔ 株式分割の扱い(渡した先が必ず踏みます)
------------------------------------------
日足は分割を**遡及調整**しますが、**5分足は保存したときのまま**です。
分割した銘柄では、分割前の日に「5分足が日足の2〜15倍」という状態になります。
値幅制限があるので現実にはあり得ない値動きに見えます。

このスクリプトは **行を落としません**。代わりに各行へ **`d_close`(その日の日足終値)**
を付けます。受け取った側が

    その日の5分足の終値の中央値 ÷ d_close

を見て、大きくずれる銘柄日を自分で外せます(30%が目安)。
こちらで落としてしまうと、それ自体が前処理=先入観になるので付けるだけにしています。

使い方
------
  python export_minute.py                        # 既定 |前日リターン|>=1.0% / 730日
  python export_minute.py --min-abs-ret1 1.75    # 現行ルールと同じ幅まで絞る
  python export_minute.py --min-abs-ret1 0       # 全量(⛔ 500MB〜1GB)
  python export_minute.py --days 400 --workers 8
  python export_minute.py --csv                  # parquet が使えないとき

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
from daytrade_data import DATA_DIR, load_intraday
# ⛔ daytrade_data.split_by_day は **バーが5本未満の日を黙って捨てます**。
#   出来高の薄い銘柄日が理由も分からず消えるので、ここでは自前で分けます
#   (捨てるかどうかは受け取った側の判断であって、輸出側で決めることではない)。


def _load_symbols(path: str) -> list[str]:
    mod = importlib.import_module(Path(path).stem)
    for name in ("SYMBOLS", "SYMBOL_LIST", "ALL_SYMBOLS"):
        v = getattr(mod, name, None)
        if not v:
            continue
        return [x[0] if isinstance(x, (tuple, list)) else str(x) for x in v]
    sys.exit(f"[error] {path} に銘柄リストが見つかりません")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="symbols_listed_prime.py")
    ap.add_argument("--days", type=int, default=730,
                    help="遡る日数(暦日)。分足は2年ローリングなので730が上限相当")
    ap.add_argument("--min-abs-ret1", type=float, default=1.0,
                    help="この|前日リターン|%%以上の銘柄日だけ出す。0=全量")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(お試し)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="minute_5m.parquet")
    ap.add_argument("--csv", action="store_true")
    a = ap.parse_args()

    if not DATA_DIR.exists():
        sys.exit(f"[error] 5分足が見つかりません: {DATA_DIR}\n"
                 f"  環境変数 MINUTE_5M_DIR で場所を指定できます")
    syms = _load_symbols(a.symbols)
    if a.limit > 0:
        syms = syms[:a.limit]
    print(f"[info] 銘柄 {len(syms):,} / 遡り {a.days:,}日 / "
          f"|前日リターン| >= {a.min_abs_ret1:.2f}% / 5分足 {DATA_DIR}")

    def _one(sym: str):
        # ── ① 日足で「どの日を出すか」を決める ────────────────────
        try:
            d = fetch(sym, max(a.days + 60, 400))
        except Exception:
            return None
        if d is None or len(d) < 3:
            return None
        _r1 = (d["close"] / d["close"].shift(1) - 1.0) * 100.0
        # D の |前日リターン| で D+1 を選ぶ(建てるのは翌営業日なので1つずらす)
        _keep = _r1.shift(1).abs() >= a.min_abs_ret1
        if a.min_abs_ret1 <= 0:
            _keep = _r1.notna()
        _cut = pd.Timestamp.today().normalize() - pd.Timedelta(days=a.days)
        _days = {x.date() for x in d.index[_keep.fillna(False) & (d.index >= _cut)]}
        if not _days:
            return None
        _dc = {x.date(): float(v) for x, v in d["close"].items()}

        # ── ② その日の5分足だけ切り出す ──────────────────────────
        try:
            m = load_intraday(sym, days=a.days + 5, source="local")
        except Exception:
            return None
        if m is None or m.empty:
            return None
        out = []
        for _dd, _g in m.groupby(m.index.date):
            if _dd not in _days or _g is None or _g.empty:
                continue
            _t = _g[["open", "high", "low", "close", "volume"]].copy()
            _t.insert(0, "symbol", sym)
            # ★ 分割未調整の検出用。落とさずに付けるだけ(前処理=先入観を避ける)
            _t["d_close"] = _dc.get(_dd, float("nan"))
            out.append(_t)
        return pd.concat(out) if out else None

    rows, ng = [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(_one, syms), 1):
            if r is None:
                ng += 1
            else:
                rows.append(r)
            if i % 100 == 0:
                _n = sum(len(x) for x in rows)
                print(f"  … {i:,}/{len(syms):,}(取れず {ng:,} / "
                      f"{_n:,}行)", flush=True)

    if not rows:
        sys.exit("[error] 1銘柄も取れませんでした")

    df = pd.concat(rows).rename_axis("datetime").reset_index()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values(["datetime", "symbol"], ignore_index=True)

    out = Path(a.out)
    if a.csv or out.suffix != ".parquet":
        out = Path(str(out.with_suffix("")) + ".csv.gz")
        df.to_csv(out, index=False, compression="gzip", encoding="utf-8")
    else:
        try:
            df.to_parquet(out, index=False, compression="snappy")
        except Exception as e:
            print(f"[warn] parquet で書けません({e}) → csv.gz にします")
            out = Path(str(out.with_suffix("")) + ".csv.gz")
            df.to_csv(out, index=False, compression="gzip", encoding="utf-8")

    _sd = df.groupby([df["datetime"].dt.date, "symbol"]).ngroups
    print(f"\n[done] {out}  {out.stat().st_size / 1024 / 1024:,.1f} MB")
    print(f"  行数       {len(df):,}")
    print(f"  銘柄日     {_sd:,}")
    print(f"  銘柄       {df['symbol'].nunique():,}(取れず {ng:,})")
    print(f"  期間       {df['datetime'].min()} 〜 {df['datetime'].max()}")
    print(f"  列         {', '.join(df.columns)}")
    print(f"\n  ⚠ **行は落としていません**。`d_close`(その日の日足終値)を付けたので、")
    print(f"     受け取った側で『5分足の終値の中央値 ÷ d_close』を見て、")
    print(f"     大きくずれる銘柄日(分割が未調整)を外してください(30%が目安)。")
    print(f"  ⚠ 昼休み(11:30〜12:30)にバーはありません。等間隔を仮定しないこと。")
    print(f"  ⚠ 先頭バーが 09:00 とは限りません(09:05 始まりの銘柄日があります)。")


if __name__ == "__main__":
    main()
