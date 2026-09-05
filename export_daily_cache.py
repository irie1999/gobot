"""export_daily_cache.py — 日足キャッシュを1つの Parquet にまとめる / 戻す

★ 何のためか (2026-08-26)
────────────────────────────────────────────────────────────────────
Claude のコンテナは **外部の市場データを取れない**(ネットワークポリシーで
Yahoo Finance が 403)。ローカルの `.rsi2_cache/` を持ち込めば、日足だけで
完結する分析(analyze_gap_edge のスイープなど)を向こうで回せる。

  ローカル →  python export_daily_cache.py --export   → daily_cache.parquet
              git add daily_cache.parquet && git commit && git push
  向こう   →  python export_daily_cache.py --import   → .rsi2_cache/ を復元
              **GOBOT_OFFLINE=1** を付けて分析を回す

⛔ **GOBOT_OFFLINE=1 が必須。** `fetch` は「最新バーが今日か」でキャッシュの
   鮮度を見る(§13.8)ので、スナップショットは必ず「古い」と判定され、
   再ダウンロードを試みて **全銘柄 None** になる。この env を立てると
   鮮度判定を飛ばし、ダウンロードも試みない。

  例:  GOBOT_OFFLINE=1 python analyze_gap_edge.py --days 4200 --split 2020-09-01 --explore

⛔ **5分足・1分足は対象外**。500MB〜1GB あって git に入らない。
   日足で完結する分析にだけ使う。

⛔ **pickle をそのまま git に入れないこと。** 毎日1行増えるだけで
   ファイル全体が変わり、git は binary の差分圧縮が効かないので
   履歴が爆発する。Parquet 1ファイルなら圧縮も効く。

⚠ 運用は **スナップショット**。毎日コミットしない。
   分析のために1回入れて、終わったら消す(または履歴から外す)。

使い方
------
  # サイズだけ見積もる (書き出さない)
  python export_daily_cache.py --estimate

  # 書き出す
  python export_daily_cache.py --export
  python export_daily_cache.py --export --min-date 2015-01-01   # 期間を絞る
  python export_daily_cache.py --export --symbols-file symbols_listed_prime.py

  # 復元する (Claude 側)
  python export_daily_cache.py --import
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="日足キャッシュを Parquet にまとめる/戻す")
ap.add_argument("--export", action="store_true", help="`.rsi2_cache/` → Parquet")
ap.add_argument("--import", dest="do_import", action="store_true",
                help="Parquet → `.rsi2_cache/`")
ap.add_argument("--estimate", action="store_true",
                help="サイズだけ見積もる(書き出さない)")
ap.add_argument("--out", type=str, default="daily_cache.parquet")
ap.add_argument("--cache-dir", type=str, default=".rsi2_cache")
ap.add_argument("--min-date", type=str, default="",
                help="この日以降だけ書き出す(yyyy-MM-dd)。サイズを削るとき")
ap.add_argument("--symbols-file", type=str, default="",
                help="銘柄を絞る(symbols_*.py)。既定はキャッシュにある全部")
ap.add_argument("--compression", type=str, default="zstd",
                choices=["zstd", "snappy", "gzip", "brotli"])
a = ap.parse_args()

_CACHE = Path(a.cache_dir)
_OUT = Path(a.out)


def _mb(n: float) -> str:
    """小さいと 0MB と出て何も分からないので、1MB 未満は KB で見せる。"""
    return f"{n / 1e3:,.0f}KB" if n < 1e6 else f"{n / 1e6:,.1f}MB"


def _wanted() -> set | None:
    if not a.symbols_file:
        return None
    try:
        _ns: dict = {}
        exec(Path(a.symbols_file).read_text(encoding="utf-8"), _ns)
        for _k in ("SYMBOLS", "WATCHLIST", "SELECTED", "symbols"):
            _v = _ns.get(_k)
            if isinstance(_v, (list, tuple, set)) and _v:
                out = set()
                for x in _v:
                    s = x[0] if isinstance(x, (list, tuple)) else x
                    s = str(s).strip()
                    out.add(s if s.endswith(".T") else f"{s}.T")
                return out
    except Exception as e:
        sys.exit(f"[error] {a.symbols_file} を読めません: {e}")
    return None


def do_export(dry: bool) -> None:
    if not _CACHE.exists():
        sys.exit(f"[error] {_CACHE} がありません")
    _pk = sorted(_CACHE.glob("*.pkl"))
    if not _pk:
        sys.exit(f"[error] {_CACHE} に .pkl がありません")
    _keep = _wanted()
    _raw = sum(f.stat().st_size for f in _pk)
    print(f"[export] {_CACHE} に {len(_pk):,}ファイル / "
          f"生サイズ {_mb(_raw)}", flush=True)
    if _keep:
        print(f"  {a.symbols_file} の {len(_keep):,}銘柄に絞ります", flush=True)
    _cut = pd.Timestamp(a.min_date) if a.min_date else None

    _frames, _skip, _rows = [], 0, 0
    for i, f in enumerate(_pk, 1):
        if i % 300 == 0:
            print(f"  … {i}/{len(_pk)}", flush=True)
        # ファイル名は `7203_T.pkl` 形式(fetch が '.'→'_' で保存する)
        _sym = f.stem.replace("_T", ".T")
        if _keep and _sym not in _keep:
            _skip += 1
            continue
        try:
            with open(f, "rb") as fh:
                df = pickle.load(fh)
        except Exception:
            _skip += 1
            continue
        if df is None or len(df) == 0:
            _skip += 1
            continue
        try:
            df = df.copy()
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            if _cut is not None:
                df = df[df.index >= _cut]
            if df.empty:
                _skip += 1
                continue
            _cols = [c for c in ("open", "high", "low", "close", "volume")
                     if c in df.columns]
            if not _cols:
                _skip += 1
                continue
            df = df[_cols].astype("float32")     # ★ float32 で半分にする
            df.insert(0, "symbol", _sym)
            df.index.name = "date"
            _frames.append(df.reset_index())
            _rows += len(df)
        except Exception:
            _skip += 1
    if not _frames:
        sys.exit("[error] 書き出せる銘柄がありません")
    _all = pd.concat(_frames, ignore_index=True)
    print(f"\n  対象 {len(_frames):,}銘柄 / {_rows:,}行 "
          f"(スキップ {_skip:,})", flush=True)
    if _all["date"].notna().any():
        print(f"  期間 {_all['date'].min().date()} 〜 {_all['date'].max().date()}",
              flush=True)
    if dry:
        # メモリ上のサイズから圧縮後を概算(実測は --export で)
        _mem = _all.memory_usage(deep=True).sum()
        print(f"\n  メモリ上 {_mb(_mem)}")
        print(f"  → Parquet+{a.compression} なら **{_mb(_mem * 0.25)}"
              f"〜{_mb(_mem * 0.45)}** 程度の見込み")
        print(f"\n  ⚠ GitHub は 1ファイル100MB が上限。超えるなら "
              f"--min-date で期間を絞るか --symbols-file で銘柄を絞ってください")
        return
    # ⚠ Parquet には pyarrow か fastparquet が要る。無ければ
    #   **pickle + gzip** に落とす(標準ライブラリだけで動く)。
    #   圧縮率は Parquet に劣るが、依存を増やさないことを優先する。
    _out = _OUT
    try:
        _all.to_parquet(_out, compression=a.compression, index=False)
    except Exception as _pe:
        _out = _OUT.with_suffix(".pkl.gz")
        print(f"\n  ⚠ Parquet を書けません({type(_pe).__name__})。"
              f"**pickle+gzip** に切り替えます → {_out}", flush=True)
        print(f"     (Parquet にしたいなら `pip install pyarrow`)", flush=True)
        import gzip as _gz
        with _gz.open(_out, "wb", compresslevel=6) as _fh:
            pickle.dump(_all, _fh, protocol=4)
    _OUT_REAL = _out
    _sz = _out.stat().st_size
    print(f"\n  → {_OUT_REAL}  **{_mb(_sz)}** "
          f"(生 {_mb(_raw)} の {_sz / _raw * 100:.0f}%)", flush=True)
    if _sz > 90e6:
        print(f"  ⛔ **90MB を超えています**。GitHub の上限(100MB)に近いので、"
              f"--min-date か --symbols-file で絞ってください", flush=True)
    elif _sz > 40e6:
        print(f"  ⚠ 40MB 超。push に時間がかかります。"
              f"**毎日コミットしないこと**(履歴が膨らみます)", flush=True)
    print(f"\n  次:  git add {_OUT_REAL} && git commit -m \"data: 日足スナップショット\" "
          f"&& git push", flush=True)


def do_import() -> None:
    # Parquet が無ければ pickle+gzip を探す(export 側のフォールバックと対)
    _src = _OUT if _OUT.exists() else _OUT.with_suffix(".pkl.gz")
    if not _src.exists():
        sys.exit(f"[error] {_OUT} も {_OUT.with_suffix('.pkl.gz')} もありません")
    print(f"[import] {_src} ({_mb(_src.stat().st_size)}) を読み込み中…",
          flush=True)
    if _src.suffix == ".gz":
        import gzip as _gz
        with _gz.open(_src, "rb") as _fh:
            _all = pickle.load(_fh)
    else:
        _all = pd.read_parquet(_src)
    _CACHE.mkdir(exist_ok=True)
    _n = 0
    for _sym, _g in _all.groupby("symbol", sort=False):
        try:
            df = _g.drop(columns=["symbol"]).set_index("date").sort_index()
            # 既存コードは float64 / datetime64[ns] 前提。Parquet は us で戻る
            df.index = pd.DatetimeIndex(df.index).as_unit("ns")
            df.index.name = "date"
            df = df.astype("float64")
            with open(_CACHE / f"{str(_sym).replace('.', '_')}.pkl", "wb") as fh:
                pickle.dump(df, fh)
            _n += 1
        except Exception:
            pass
    print(f"  → {_CACHE} に {_n:,}銘柄を復元しました", flush=True)
    if _all["date"].notna().any():
        print(f"  期間 {_all['date'].min().date()} 〜 "
              f"**{_all['date'].max().date()}** まで", flush=True)
    print(f"\n  ⛔ 分析は **GOBOT_OFFLINE=1** を付けて回すこと。"
          f"付けないと `fetch` がスナップショットを『古い』と判定し、"
          f"再ダウンロードを試みて **全銘柄 None** になります(§13.8)。", flush=True)
    print(f"     例: GOBOT_OFFLINE=1 python analyze_gap_edge.py "
          f"--days 4200 --split 2020-09-01 --explore", flush=True)
    print(f"\n  ⚠ 最新日は上記で止まります。**当日の発注判断には使わないこと。**",
          flush=True)


if a.estimate:
    do_export(dry=True)
elif a.export:
    do_export(dry=False)
elif a.do_import:
    do_import()
else:
    ap.print_help()
