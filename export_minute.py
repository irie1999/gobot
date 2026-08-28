r"""export_minute.py — 5分足を「必要な銘柄日だけ」書き出す(他の環境へ渡す用)

既定は **全量**(削らない)
-------------------------
1,540銘柄 × 約490営業日 × 約66バー = **約5,000万行 / 数GB**。

そのままだと ① 一度に concat すると RAM 3GB超 ② 1ファイル2GB超 になるので、
**銘柄をまとめて処理したら都度ファイルへ吐きます**(`--part-symbols`、既定150)。
読む側は glob で1つにまとめられます。

サイズを落としたいときだけ、日足で先に銘柄日を絞れます(この戦略は**同日決済**
なので、候補かどうかは前日の日足だけで決まります):

  --min-abs-ret1 0     **既定。全量** 約5,000万行 / 数GB
  --min-abs-ret1 1.0   約1,400万行(150〜250MB)
  --min-abs-ret1 1.75  約 600万行( 70〜100MB) ⛔ 現行ルールの条件そのもの

⚠ 1.75% で切ると渡した先が別の切り口を思いついても検証できません。
  絞るなら 1.0% までにしてください。

`--float32` でサイズがほぼ半分になります(株価6,000円までなら float32 で正確)。

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
  python export_minute.py --limit 50             # ★まず50銘柄で見当をつける
  python export_minute.py --float32              # 全量。150銘柄ごとに分割
  python export_minute.py --part-symbols 100     # 1ファイルをもっと小さく
  python export_minute.py --min-abs-ret1 1.0     # サイズを落としたいとき
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
    ap.add_argument("--min-abs-ret1", type=float, default=0.0,
                    help="この|前日リターン|%%以上の銘柄日だけ出す。"
                         "**既定0=全量**(削らずに渡す)")
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(お試し)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="minute_5m.parquet")
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--part-symbols", type=int, default=150,
                    help="この銘柄数ごとに1ファイルへ分ける。0=1ファイル。"
                         "⛔ 全量は約5,000万行で、まとめると RAM 3GB超・"
                         "1ファイル2GB超になります")
    ap.add_argument("--float32", action="store_true",
                    help="価格・出来高を float32 で書く(サイズがほぼ半分)。"
                         "株価6,000円までなら float32 で正確に表せます")
    ap.add_argument("--compression", default="zstd",
                    choices=["zstd", "snappy", "gzip", "brotli"],
                    help="parquet の圧縮。zstd が最も小さい")
    a = ap.parse_args()

    if not DATA_DIR.exists():
        sys.exit(f"[error] 5分足が見つかりません: {DATA_DIR}\n"
                 f"  環境変数 MINUTE_5M_DIR で場所を指定できます")
    syms = _load_symbols(a.symbols)
    if a.limit > 0:
        syms = syms[:a.limit]
    print(f"[info] 銘柄 {len(syms):,} / 遡り {a.days:,}日 / 5分足 {DATA_DIR}")
    print(f"[info] 絞り込み … "
          + ("**なし(全量)**" if a.min_abs_ret1 <= 0
             else f"|前日リターン| >= {a.min_abs_ret1:.2f}%")
          + f" / float32={'ON' if a.float32 else 'OFF'} / {a.compression}")
    if a.min_abs_ret1 <= 0:
        print(f"  ⚠ 全量は約5,000万行・数GBになります。"
              f"{a.part_symbols or len(syms)}銘柄ごとにファイルを分けます")

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

    # ── 書き出し ────────────────────────────────────────────────
    #   ⛔ 全量は約5,000万行。まとめて concat すると RAM 3GB超・1ファイル2GB超に
    #     なるので、**銘柄をまとめて処理したら都度ファイルへ吐く**。
    _base = Path(a.out)
    _stem = _base.with_suffix("").name
    _dir = _base.parent
    _ext = ".csv.gz" if a.csv or _base.suffix != ".parquet" else ".parquet"
    _bs = a.part_symbols if a.part_symbols > 0 else len(syms)
    _F32 = ["open", "high", "low", "close", "volume", "d_close"]

    def _write(df: pd.DataFrame, path: Path) -> None:
        if a.float32:
            for c in _F32:
                if c in df.columns:
                    df[c] = df[c].astype("float32")
        if _ext == ".csv.gz":
            df.to_csv(path, index=False, compression="gzip", encoding="utf-8")
            return
        try:
            df.to_parquet(path, index=False, compression=a.compression)
        except Exception as e:
            print(f"[warn] parquet で書けません({e}) → csv.gz にします")
            df.to_csv(path.with_suffix(".csv.gz"), index=False,
                      compression="gzip", encoding="utf-8")

    _files: list[Path] = []
    _tot, _sd, _ng, _syms_ok = 0, 0, 0, 0
    _tmin, _tmax = None, None
    _cols: list[str] = []
    _parts = (len(syms) + _bs - 1) // _bs
    for _p in range(_parts):
        _batch = syms[_p * _bs:(_p + 1) * _bs]
        _rows = []
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for r in ex.map(_one, _batch):
                if r is None:
                    _ng += 1
                else:
                    _rows.append(r)
        if not _rows:
            print(f"  part {_p + 1}/{_parts}: 0行(全部取れず)", flush=True)
            continue
        df = pd.concat(_rows).rename_axis("datetime").reset_index()
        del _rows
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values(["datetime", "symbol"], ignore_index=True)
        _name = (f"{_stem}{_ext}" if _parts == 1
                 else f"{_stem}_part{_p + 1:02d}{_ext}")
        _path = _dir / _name
        _write(df, _path)
        _files.append(_path)
        _tot += len(df)
        _sd += df.groupby([df["datetime"].dt.date, "symbol"]).ngroups
        _syms_ok += df["symbol"].nunique()
        _cols = list(df.columns)
        _tmin = df["datetime"].min() if _tmin is None else min(_tmin, df["datetime"].min())
        _tmax = df["datetime"].max() if _tmax is None else max(_tmax, df["datetime"].max())
        _mb = _path.stat().st_size / 1024 / 1024 if _path.exists() else 0.0
        print(f"  part {_p + 1}/{_parts}: {_path.name}  {len(df):,}行  "
              f"{_mb:,.1f} MB  (累計 {_tot:,}行)", flush=True)
        del df

    if not _files:
        sys.exit("[error] 1銘柄も取れませんでした")

    _mb = sum(f.stat().st_size for f in _files if f.exists()) / 1024 / 1024
    print(f"\n[done] {len(_files)}ファイル  合計 {_mb:,.1f} MB")
    print(f"  行数       {_tot:,}")
    print(f"  銘柄日     {_sd:,}")
    print(f"  銘柄       {_syms_ok:,}(取れず {_ng:,})")
    print(f"  期間       {_tmin} 〜 {_tmax}")
    print(f"  列         {', '.join(_cols)}")
    if len(_files) > 1:
        print(f"\n  読み込みは glob で1つにまとめられます:")
        print(f"    import pandas as pd, glob")
        print(f"    df = pd.concat(pd.read_parquet(f) "
              f"for f in sorted(glob.glob('{_stem}_part*{_ext}')))")
        print(f"  ⚠ 全量なら合計で数GBです。列や期間を絞って読むほうが確実です")
    print(f"\n  ⚠ **行は落としていません**。`d_close`(その日の日足終値)を付けたので、")
    print(f"     受け取った側で『5分足の終値の中央値 ÷ d_close』を見て、")
    print(f"     大きくずれる銘柄日(分割が未調整)を外してください(30%が目安)。")
    print(f"  ⚠ 昼休み(11:30〜12:30)にバーはありません。等間隔を仮定しないこと。")
    print(f"  ⚠ 先頭バーが 09:00 とは限りません(09:05 始まりの銘柄日があります)。")


if __name__ == "__main__":
    main()
