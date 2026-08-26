"""export_intraday_cache.py — 5分足を「必要な銘柄日だけ」持ち出す

★ 何のためか (2026-08-26)
────────────────────────────────────────────────────────────────────
日足は `export_daily_cache.py` で丸ごと持ち出せた。5分足は **全部だと git に
入らない**(1,540銘柄 × 2年 = 数百MB〜1GB)。

そこで **全部は諦めて、分析が実際に触る銘柄日だけ**を切り出す。
N の候補は1日あたり26件程度(§18.54)なので、2年ぶんでも1万数千銘柄日。
1銘柄日 = 66本 × 4〜5列しかないので、**数十MB に収まる**。

  ローカル →  python export_intraday_cache.py --estimate
              (まず実データを測る。↑これが「何MBになるか」の答え)
              python analyze_gap_edge.py --days 800 --min-gap-bp 0 --out gap_rows.csv
              python export_intraday_cache.py --export --pairs gap_rows.csv
              git add intraday_5m*.parquet && git commit && git push
  向こう   →  python export_intraday_cache.py --import
              MINUTE_5M_DIR=stock_5min_subset python ...

⛔ **これは部分データ。** 復元先を `stock_5min_subset` にして、中に何が
   入っているかの manifest を必ず書く。本物の `stock_5min` に混ぜないこと
   (「データが無い」と「その日は対象外」の区別がつかなくなる)。

使い方
------
  # ① 実データを測る (書き出さない)
  python export_intraday_cache.py --estimate

  # ② 絞ったらいくらになるか (--pairs は analyze_gap_edge --out のCSV)
  python export_intraday_cache.py --estimate --pairs gap_rows.csv

  # ③ 書き出す
  python export_intraday_cache.py --export --pairs gap_rows.csv

  # ④ 復元する (Claude 側)
  python export_intraday_cache.py --import
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="5分足を必要な銘柄日だけ持ち出す")
ap.add_argument("--estimate", action="store_true",
                help="実データを測る(書き出さない)")
ap.add_argument("--export", action="store_true", help="Parquet に書き出す")
ap.add_argument("--import", dest="do_import", action="store_true",
                help="Parquet → 部分キャッシュを復元")
ap.add_argument("--pairs", type=str, default="",
                help="銘柄日を絞るCSV(analyze_gap_edge --out の出力。"
                     "symbol / date 列を見る)")
ap.add_argument("--ret1-min", type=float, default=1.753,
                help="--pairs を N の条件で絞る: 前日リターン%%の下限")
ap.add_argument("--gap-min", type=float, default=100.0,
                help="同: ギャップbp の下限。0 で無効(ギャップ前の候補も全部)")
ap.add_argument("--watch", type=int, default=0,
                help="同: 1日あたり流動性上位N件だけ(0=絞らない)。"
                     "0 のままにすると閾値を後から動かせる")
ap.add_argument("--pad-days", type=int, default=0,
                help="各銘柄日の前後に何営業日ぶん余分に入れるか(既定0)")
ap.add_argument("--data-dir", type=str, default="",
                help="5分足の場所。既定は daytrade_data の自動検出")
ap.add_argument("--out-prefix", type=str, default="intraday_5m")
ap.add_argument("--out-dir", type=str, default="stock_5min_subset",
                help="--import の復元先")
ap.add_argument("--max-mb", type=float, default=90.0,
                help="1ファイルの上限MB(GitHub は100MB)。超えたら分割する")
ap.add_argument("--compression", type=str, default="zstd",
                choices=["zstd", "snappy", "gzip", "brotli"])
ap.add_argument("--sample", type=int, default=30,
                help="--estimate で実測に使う銘柄数")
a = ap.parse_args()

# ⛔ volume を落とす案は棄却した(2026-08-26)。実測で 11% しか減らないのに、
#    daytrade_data._load_local が volume 列の無い pickle を読めず **yfinance に
#    フォールバックして黙って None** になる。11% のために「データがあるのに
#    無いことになる」経路を増やす価値はない。
_COLS = ["open", "high", "low", "close", "volume"]


def _resolve_dir() -> Path:
    if a.data_dir:
        return Path(a.data_dir)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import daytrade_data as _dd          # 自動検出をそのまま借りる
        return Path(_dd.DATA_DIR)
    except Exception:
        env = os.environ.get("MINUTE_5M_DIR")
        if env:
            return Path(env)
        here = Path(__file__).resolve().parent
        for c in (here / "data" / "minute_5m", here.parent / "stock_5min"):
            if c.exists() and any(c.glob("*.pkl")):
                return c
        return here / "data" / "minute_5m"


def _mb(n: float) -> str:
    return f"{n / 1e3:,.0f}KB" if n < 1e6 else f"{n / 1e6:,.1f}MB"


def _jq(code: str) -> str:
    """7203.T → 72030 (ファイル名は J-Quants の5桁)"""
    c = str(code).strip().upper().replace(".T", "")
    return c + "0" if len(c) == 4 and c.isdigit() else c


def _norm(raw) -> pd.DataFrame | None:
    """pickle をそのまま読んで DatetimeIndex + 小文字OHLCV にする。

    ⚠ daytrade_data.normalize_minute_df は resample までやるが、ここでは
       **保存されている粒度のまま**出す(向こうで同じ経路に載せるため)。
    """
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None
    df = raw.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        for _k in ("DateTime", "datetime", "Datetime"):
            if _k in df.columns:
                df.index = pd.to_datetime(df[_k], errors="coerce")
                break
        else:
            if "Date" in df.columns and "Time" in df.columns:
                df.index = pd.to_datetime(
                    df["Date"].astype(str) + " " + df["Time"].astype(str),
                    errors="coerce")
            else:
                return None
    try:
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
    except Exception:
        pass
    df = df[df.index.notna()]
    if df.empty:
        return None
    _low = {str(c).lower(): c for c in df.columns}
    _pick = {}
    for c in ("open", "high", "low", "close", "volume"):
        if c in _low:
            _pick[c] = _low[c]
    if not {"open", "high", "low", "close"} <= set(_pick):
        return None
    out = pd.DataFrame({k: pd.to_numeric(df[v], errors="coerce")
                        for k, v in _pick.items()})
    out = out.dropna(subset=["close"]).sort_index()
    return out if not out.empty else None


def _wanted_pairs() -> dict[str, set] | None:
    """--pairs の CSV から {jq_code: {date, ...}} を作る。"""
    if not a.pairs:
        return None
    p = Path(a.pairs)
    if not p.exists():
        sys.exit(f"[error] {p} がありません")
    df = pd.read_csv(p)
    _cols = {str(c).lower(): c for c in df.columns}
    if "symbol" not in _cols or "date" not in _cols:
        sys.exit(f"[error] {p} に symbol / date 列がありません "
                 f"(あるのは: {list(df.columns)[:12]})")
    n0 = len(df)
    _msg = [f"{n0:,}行"]
    if "ret1" in _cols and a.ret1_min > -1e9:
        df = df[pd.to_numeric(df[_cols["ret1"]], errors="coerce") >= a.ret1_min]
        _msg.append(f"前日リターン ≥{a.ret1_min}% → {len(df):,}")
    if "gap_bp" in _cols and a.gap_min > 0:
        df = df[pd.to_numeric(df[_cols["gap_bp"]], errors="coerce") >= a.gap_min]
        _msg.append(f"ギャップ ≥{a.gap_min:.0f}bp → {len(df):,}")
    if a.watch > 0 and "liq" in _cols:
        df = (df.sort_values(_cols["liq"], ascending=False, na_position="last")
                .groupby(_cols["date"], sort=False).head(a.watch))
        _msg.append(f"流動性 上位{a.watch} → {len(df):,}")
    print(f"[pairs] {p}: " + " / ".join(_msg), flush=True)
    if df.empty:
        sys.exit("[error] 絞り込みで0件になりました")
    out: dict[str, set] = {}
    _d = pd.to_datetime(df[_cols["date"]], errors="coerce").dt.normalize()
    for _s, _dt in zip(df[_cols["symbol"]].astype(str), _d):
        if pd.isna(_dt):
            continue
        out.setdefault(_jq(_s), set()).add(_dt)
    if a.pad_days > 0:
        for _k, _v in out.items():
            _ex = set()
            for _t in _v:
                for _o in range(-a.pad_days, a.pad_days + 1):
                    _ex.add(_t + pd.Timedelta(days=_o))
            out[_k] = _ex
    print(f"        → {len(out):,}銘柄 / "
          f"{sum(len(v) for v in out.values()):,}銘柄日", flush=True)
    return out


# ──────────────────────────────────────────────────────────────────
_DIR = _resolve_dir()


def _iter_frames(keep: dict[str, set] | None, files: list[Path]):
    """(jq_code, 絞り込み後のDataFrame) を1銘柄ずつ返す。メモリに全部載せない。"""
    for f in files:
        _code = f.stem
        if keep is not None and _code not in keep:
            continue
        try:
            df = _norm(pickle.loads(f.read_bytes()))
        except Exception:
            continue
        if df is None:
            continue
        if keep is not None:
            _days = keep[_code]
            df = df[pd.Series(df.index.normalize(), index=df.index).isin(_days)]
            if df.empty:
                continue
        _c = [c for c in _COLS if c in df.columns]
        df = df[_c].astype("float32")
        df.index.name = "dt"
        yield _code, df


def do_estimate() -> None:
    _pk = sorted(_DIR.glob("*.pkl"))
    if not _pk:
        sys.exit(f"[error] {_DIR} に .pkl がありません\n"
                 f"        --data-dir で場所を指定してください")
    _raw = sum(f.stat().st_size for f in _pk)
    print(f"[5分足] {_DIR}")
    print(f"  {len(_pk):,}ファイル / 生サイズ **{_mb(_raw)}**\n", flush=True)

    _keep = _wanted_pairs()

    # ── 実測サンプル ────────────────────────────────────────────
    _n = min(a.sample, len(_pk))
    _step = max(1, len(_pk) // _n)
    _samp = _pk[::_step][:_n]
    print(f"  {_n}銘柄をサンプルして中身を測ります…", flush=True)
    _rows = _days = _ok = 0
    _lo = _hi = None
    _frames = []
    for _code, df in _iter_frames(None, _samp):
        _ok += 1
        _rows += len(df)
        _dn = pd.Series(df.index.normalize()).nunique()
        _days += _dn
        _lo = df.index[0] if _lo is None else min(_lo, df.index[0])
        _hi = df.index[-1] if _hi is None else max(_hi, df.index[-1])
        _t = df.copy()
        _t.insert(0, "symbol", _code)
        _frames.append(_t.reset_index())
    if not _frames:
        sys.exit("[error] サンプルを1件も読めませんでした")
    print(f"    読めた {_ok}/{_n}銘柄 / {_rows:,}本 / {_days:,}銘柄日")
    print(f"    期間 {_lo.date()} 〜 {_hi.date()}")
    print(f"    1銘柄日あたり **{_rows / max(_days, 1):.0f}本**"
          f"(東証の5分足は 09:00-11:30 + 12:30-15:30 で 66本)")

    # ★ 推測しない。サンプルを実際に Parquet にして圧縮率を測る。
    _s = pd.concat(_frames, ignore_index=True)
    import tempfile
    _tmp = Path(tempfile.gettempdir()) / "_gobot_5m_probe.parquet"
    _fallback = False
    try:
        _s.to_parquet(_tmp, compression=a.compression, index=False)
    except Exception:
        _fallback = True
        import gzip as _gz
        _tmp = Path(tempfile.gettempdir()) / "_gobot_5m_probe.pkl.gz"
        with _gz.open(_tmp, "wb", compresslevel=6) as _fh:
            pickle.dump(_s, _fh, protocol=4)
    _per_row = _tmp.stat().st_size / max(len(_s), 1)
    _per_day = _per_row * (_rows / max(_days, 1))
    try:
        _tmp.unlink()
    except Exception:
        pass
    _fmt = ("pickle+gzip(pyarrow が無い)" if _fallback
            else f"Parquet+{a.compression}")
    print(f"\n  実測圧縮率({_fmt} / {len(_COLS)}列 float32):")
    print(f"    1本 {_per_row:.1f}バイト / **1銘柄日 {_per_day / 1e3:.1f}KB**")

    # ── 全部だといくらか ────────────────────────────────────────
    _tot_days = _days / max(_ok, 1) * len(_pk)
    _full = _tot_days * _per_day
    print(f"\n  ■ 全部持ち出すと")
    print(f"    {len(_pk):,}銘柄 × {_days / max(_ok, 1):.0f}日 "
          f"≒ {_tot_days:,.0f}銘柄日 → **{_mb(_full)}**")
    if _full > 100e6:
        print(f"    ⛔ GitHub の上限(1ファイル100MB)を超えます。"
              f"{math.ceil(_full / (a.max_mb * 1e6))}分割すれば入りますが、"
              f"リポジトリが重くなるので勧めません")

    # ── 絞るといくらか ──────────────────────────────────────────
    if _keep is not None:
        _kd = sum(len(v) for v in _keep.values())
        _kn = _kd * _per_day
        print(f"\n  ■ --pairs で絞ると")
        print(f"    {len(_keep):,}銘柄 / {_kd:,}銘柄日 → **{_mb(_kn)}**"
              f"  (全部の {_kn / max(_full, 1) * 100:.1f}%)")
        if _kn <= a.max_mb * 1e6:
            print(f"    ✅ 1ファイルに収まります。--export してください")
        else:
            print(f"    ⚠ {math.ceil(_kn / (a.max_mb * 1e6))}分割になります")
    else:
        print(f"\n  ▶ 次: analyze_gap_edge の --out で銘柄日のCSVを作り、")
        print(f"       --pairs に渡すとどれだけ小さくなるか出ます")
        print(f"       python analyze_gap_edge.py --days 800 --min-gap-bp 0 "
              f"--out gap_rows.csv --workers 8")
        print(f"       python export_intraday_cache.py --estimate "
              f"--pairs gap_rows.csv")
    print(f"\n  ⚠ これ以上small くする手は無い。列を減らす案(volume を落とす)は"
          f"11%しか減らないうえ daytrade_data が読めなくなるので棄却済み。"
          f"**削るなら銘柄日を減らす**(--pairs の条件を厳しくする)。")


def _write_part(frames: list[pd.DataFrame], idx: int) -> tuple[Path, int]:
    _all = pd.concat(frames, ignore_index=True)
    _p = Path(f"{a.out_prefix}_{idx:02d}.parquet")
    try:
        _all.to_parquet(_p, compression=a.compression, index=False)
    except Exception as _pe:
        _p = Path(f"{a.out_prefix}_{idx:02d}.pkl.gz")
        import gzip as _gz
        with _gz.open(_p, "wb", compresslevel=6) as _fh:
            pickle.dump(_all, _fh, protocol=4)
        if idx == 1:
            print(f"  ⚠ Parquet を書けません({type(_pe).__name__})。"
                  f"pickle+gzip に切り替えます", flush=True)
    return _p, len(_all)


def do_export() -> None:
    _pk = sorted(_DIR.glob("*.pkl"))
    if not _pk:
        sys.exit(f"[error] {_DIR} に .pkl がありません")
    _keep = _wanted_pairs()
    if _keep is None:
        print("⚠ --pairs が無いので **全部**書き出します。"
              "数百MBになる可能性があります(--estimate で先に測ってください)",
              flush=True)
    print(f"[export] {_DIR} から書き出し中…", flush=True)

    _cap = a.max_mb * 1e6
    # ⛔ 行数予算を「見込み」で始めてはいけない。1パート目だけ未較正のまま走り、
    #    上限を大きく超えたファイルができる(2026-08-26 の実装で実際に踏んだ)。
    #    最初の _PROBE_ROWS 本が溜まった時点で **一度だけ temp に書いて実測**し、
    #    そこから予算を決める。以後は各パートの実サイズで補正する。
    _PROBE_ROWS = 50_000
    _rows_budget = _PROBE_ROWS      # 較正するまでは probe 分だけ溜める
    _calibrated = False
    _frames, _acc, _part = [], 0, 1
    _files, _rows_tot, _syms = [], 0, 0
    _manifest: dict[str, list[str]] = {}

    def _probe(frames: list[pd.DataFrame]) -> int:
        """溜まった分を temp に1回書いて バイト/本 を実測し、行数予算を返す。"""
        import tempfile
        _s = pd.concat(frames, ignore_index=True)
        _t = Path(tempfile.gettempdir()) / "_gobot_5m_cal.parquet"
        try:
            _s.to_parquet(_t, compression=a.compression, index=False)
        except Exception:
            import gzip as _gz
            _t = Path(tempfile.gettempdir()) / "_gobot_5m_cal.pkl.gz"
            with _gz.open(_t, "wb", compresslevel=6) as _fh:
                pickle.dump(_s, _fh, protocol=4)
        _per = _t.stat().st_size / max(len(_s), 1)
        try:
            _t.unlink()
        except Exception:
            pass
        _b = max(20_000, int(_cap * 0.92 / max(_per, 1e-9)))
        print(f"  [較正] 実測 {_per:.1f}バイト/本 → 1ファイル {_b:,}本 まで",
              flush=True)
        return _b

    def _flush():
        nonlocal _frames, _acc, _part, _rows_budget, _calibrated
        if not _frames:
            return
        _p, _n = _write_part(_frames, _part)
        _sz = _p.stat().st_size
        _files.append((_p, _sz, _n))
        print(f"  → {_p.name}  {_mb(_sz)} / {_n:,}本", flush=True)
        # 実測から次パートの行数予算を補正する(推測しない)
        _per = _sz / max(_n, 1)
        _rows_budget = max(20_000, int(_cap * 0.92 / max(_per, 1e-9)))
        _calibrated = True
        _frames, _acc = [], 0
        _part += 1

    for i, (_code, df) in enumerate(_iter_frames(_keep, _pk), 1):
        _syms += 1
        _rows_tot += len(df)
        _t = df.copy()
        _t.insert(0, "symbol", _code)
        _frames.append(_t.reset_index())
        _acc += len(df)
        _manifest[_code] = sorted({str(d.date()) for d in df.index.normalize()})
        if not _calibrated and _acc >= _PROBE_ROWS:
            _rows_budget = _probe(_frames)
            _calibrated = True
        if _calibrated and _acc >= _rows_budget:
            _flush()
        if i % 200 == 0:
            print(f"  … {i}銘柄 / {_rows_tot:,}本", flush=True)
    _flush()

    if not _files:
        sys.exit("[error] 書き出せる銘柄がありません")
    _mf = Path(f"{a.out_prefix}_manifest.json")
    _mf.write_text(json.dumps({
        "note": "部分データ。ここに無い銘柄日は『データ無し』ではなく『対象外』",
        "columns": _COLS,
        "symbols": len(_manifest),
        "symbol_days": sum(len(v) for v in _manifest.values()),
        "parts": [p.name for p, _, _ in _files],
        "pairs_source": a.pairs or "(全部)",
        "filters": {"ret1_min": a.ret1_min, "gap_min": a.gap_min,
                    "watch": a.watch, "pad_days": a.pad_days},
        "days": _manifest,
    }, ensure_ascii=False), encoding="utf-8")
    _tot = sum(s for _, s, _ in _files)
    print(f"\n  {_syms:,}銘柄 / {_rows_tot:,}本 / "
          f"{sum(len(v) for v in _manifest.values()):,}銘柄日")
    print(f"  {len(_files)}ファイル 計 **{_mb(_tot)}** (+ {_mf.name})")
    _over = [p.name for p, s, _ in _files if s > 100e6]
    if _over:
        print(f"  ⛔ **100MB を超えたファイルがあります**: {_over}\n"
              f"     GitHub が受け付けません。--max-mb を下げてやり直してください")
    print(f"\n  次:  git add {a.out_prefix}_*.parquet {a.out_prefix}_*.pkl.gz "
          f"{_mf.name} 2>/dev/null; git commit -m \"data: 5分足の部分スナップショット\"")


def do_import() -> None:
    _parts = sorted(list(Path(".").glob(f"{a.out_prefix}_*.parquet"))
                    + list(Path(".").glob(f"{a.out_prefix}_*.pkl.gz")))
    if not _parts:
        sys.exit(f"[error] {a.out_prefix}_*.parquet / *.pkl.gz がありません")
    _out = Path(a.out_dir)
    _out.mkdir(exist_ok=True)
    print(f"[import] {len(_parts)}ファイル "
          f"({_mb(sum(p.stat().st_size for p in _parts))}) を読み込み中…",
          flush=True)
    _buf: dict[str, list[pd.DataFrame]] = {}
    for p in _parts:
        if p.suffix == ".gz":
            import gzip as _gz
            with _gz.open(p, "rb") as _fh:
                _df = pickle.load(_fh)
        else:
            _df = pd.read_parquet(p)
        for _sym, _g in _df.groupby("symbol", sort=False):
            _buf.setdefault(str(_sym), []).append(_g.drop(columns=["symbol"]))
    _n, _days = 0, 0
    for _sym, _gs in _buf.items():
        try:
            df = pd.concat(_gs, ignore_index=True).set_index("dt").sort_index()
            df.index = pd.DatetimeIndex(df.index).as_unit("ns")
            df = df.astype("float64")       # 既存コードは float64 前提
            with open(_out / f"{_sym}.pkl", "wb") as fh:
                pickle.dump(df, fh)
            _n += 1
            _days += pd.Series(df.index.normalize()).nunique()
        except Exception:
            pass
    print(f"  → {_out} に {_n:,}銘柄 / {_days:,}銘柄日 を復元しました")
    _mf = Path(f"{a.out_prefix}_manifest.json")
    if _mf.exists():
        print(f"  中身の一覧: {_mf}")
    print(f"\n  ⛔ **これは部分データです。**")
    print(f"     ここに無い銘柄日は『データが無い』のではなく『持ち出さなかった』"
          f"だけ。全銘柄を前提にした集計をすると、黙って母集団が縮みます(§18.50)。")
    print(f"\n  使うとき:  MINUTE_5M_DIR={_out} python <script>")
    print(f"  ⚠ 本物の stock_5min と混ぜないこと。")


if a.estimate:
    do_estimate()
elif a.export:
    do_export()
elif a.do_import:
    do_import()
else:
    ap.print_help()
