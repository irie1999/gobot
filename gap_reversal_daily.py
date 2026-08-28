#!/usr/bin/env python3
"""
gap_reversal_daily.py — ギャップ反転(過剰反応の巻き戻し)を日足だけで検証する。

CLAUDE.md §16 参照。日中の価格の到達順序を必要としないので、
分足(直近2年)ではなく日足(2007年〜)の全期間で検証できる。

  前日リターン(t-1) が大きい → 当日(t)始値が同方向にギャップ → 当日の
  始値→終値で反対方向に戻る

という仮説を、以下の観点で検定する。

  1. 市場成分を控除した「残差ギャップ」で見る (単純ギャップは市場要因を含む)
  2. 決算前後を分離する (決算ギャップは反転せずドリフトするはず = プラセボ)
  3. 日次ポートフォリオに集約してから検定する (トレード単位はサンプル水増し)
  4. 同日の非シグナル銘柄との差 (α) を見る (勝率・絶対リターンは無意味)
  5. ギャップ幅の分位で単調性を見る (単調でなければノイズ)
  6. 往復コストを引いて生き残るか

使い方:
    python gap_reversal_daily.py --self-test          # 合成データで配管を確認
    python gap_reversal_daily.py --limit 200          # 200銘柄で試す
    python gap_reversal_daily.py                      # 全ユニバース
    python gap_reversal_daily.py --raw                # 1.75%/1.0% の固定閾値版
    python gap_reversal_daily.py --earnings           # 決算日を取得して分離
    python gap_reversal_daily.py --csv panel.csv      # 候補パネルを書き出す

注意:
    初回は yfinance から 2007年以降の日足を全銘柄ぶん取得するので時間がかかる。
    2回目以降は .gapmr_cache/ のキャッシュで数分。
"""
from __future__ import annotations

import argparse
import math
import pickle
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_DIR = Path(".gapmr_cache")
INDEX_SYM = "^N225"
START_DATE = "2007-01-01"

# ── デフォルト閾値 ────────────────────────────────────────────────
# ATR 単位 (相場環境に依存しないよう正規化)
PREV_MOVE_ATR = 1.0     # 前日リターン >= 1.0 × ATR20
GAP_ATR       = 0.5     # 残差ギャップ >= 0.5 × ATR20
# 生 % 単位 (--raw。N_CORE.md の叩き台の再現)
PREV_MOVE_PCT = 0.0175
GAP_PCT       = 0.010

BETA_WINDOW   = 120     # ローリングβの窓 (t-1 まででシフト済み)
ATR_PERIOD    = 20
MIN_PRICE     = 1000.0
MAX_PRICE     = 6000.0
MIN_TURNOVER  = 3e8     # 20日平均売買代金の下限 (円)。執行可能性のフィルタ
_BOUNCE_SLOPE = 0.0    # bounce_slope() が実行時に設定する

COST_BPS      = 30.0    # 往復コスト (bp)。呼値+板寄せ+引け成行の想定
TRADING_DAYS  = 245


# ══════════════════════════════════════════════════════════════════
# データ取得
# ══════════════════════════════════════════════════════════════════
# ローカル日足を読み込んだ場合の置き場 (yfinance を使わずに済ませる)
_LOCAL: dict[str, pd.DataFrame] | None = None

_NEED = ["open", "high", "low", "close"]

# 東証には値幅制限があるので、これを超えるギャップは物理的に起こり得ない。
# 1,000〜6,000円の株の制限値幅は最大でも 30% 程度。
MAX_GAP = 0.30

# データ破損の集計。ThreadPoolExecutor から触るのでロックを持つ。
_INTEGRITY: dict[str, int] = defaultdict(int)
_INTEGRITY_ROWS: list[dict] = []
_INTEGRITY_LOCK = threading.Lock()
_AUDIT_CAP = 400


def _note_bad(kind: str, n: int, rows: pd.DataFrame | None = None) -> None:
    if n <= 0:
        return
    with _INTEGRITY_LOCK:
        _INTEGRITY[kind] += n
        if rows is not None and len(_INTEGRITY_ROWS) < _AUDIT_CAP:
            for dt, r in rows.head(_AUDIT_CAP - len(_INTEGRITY_ROWS)).iterrows():
                _INTEGRITY_ROWS.append({
                    "kind": kind, "date": dt,
                    "symbol": r.get("symbol", "?"),
                    "prev_close": r.get("prev_close", float("nan")),
                    "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"],
                    "gap_pct": r.get("gap", float("nan")) * 100,
                })


def _normalize_daily(df: pd.DataFrame, name: str) -> pd.DataFrame | None:
    """列名・型・index を揃える。date/Date 列があれば index にする。"""
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated(keep="first")]
    for c in ("date", "datetime", "time", "日付"):
        if c in df.columns:
            df = df.set_index(pd.to_datetime(df[c])).drop(columns=[c])
            break
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, errors="coerce")
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df[df.index.notna()].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if any(c not in df.columns for c in _NEED):
        print(f"  skip {name}: 列 {_NEED} が揃っていません "
              f"({list(df.columns)[:8]})", file=sys.stderr)
        return None
    if "volume" not in df.columns:
        df["volume"] = 0.0
    out = df[_NEED + ["volume"]].apply(pd.to_numeric, errors="coerce")
    out = out.dropna(subset=_NEED)
    out = out[(out[_NEED] > 0).all(axis=1)]
    # OHLC の整合性。始値・終値は必ず 安値〜高値 の内側にある。
    # 外れている行はデータ破損 (yfinance の欠損・分割の未調整など)。
    ok = (out["high"] >= out["low"]) \
        & out["open"].between(out["low"], out["high"]) \
        & out["close"].between(out["low"], out["high"])
    bad = out[~ok]
    if len(bad):
        _note_bad("ohlc", len(bad), bad.assign(symbol=name))
    out = out[ok]
    return out if len(out) >= 250 else None


def _read_any(path: Path) -> pd.DataFrame | None:
    """csv / csv.gz / tsv / parquet / pkl を読む。"""
    n = path.name.lower()
    try:
        if n.endswith((".parquet", ".pq")):
            return pd.read_parquet(path)
        if n.endswith((".pkl", ".pickle")):
            with open(path, "rb") as f:
                return pickle.load(f)
        sep = "\t" if ".tsv" in n else ","
        return pd.read_csv(path, sep=sep)
    except Exception as e:
        print(f"  読み込み失敗 {path.name}: {e}", file=sys.stderr)
        return None


def load_local(data_dir: str | None, data_file: str | None) -> list[str]:
    """ローカル日足を _LOCAL に載せ、銘柄シンボルの一覧を返す。

    --data-dir : 銘柄ごとのファイル (<SYM>.csv / .csv.gz / .parquet / .pkl)。
                 ファイル名の拡張子を除いた部分が銘柄コードになる。
                 `7203_T` のような形は `7203.T` に読み替える。
    --data-file: 1本の長形式ファイル (symbol,date,open,high,low,close,volume)。
    どちらも列名は大文字小文字を問わない。日付は date/Date/datetime/index のどれでも可。
    """
    global _LOCAL
    store: dict[str, pd.DataFrame] = {}

    if data_file:
        raw = _read_any(Path(data_file))
        if raw is None:
            raise SystemExit(f"{data_file} を読めません")
        raw.columns = [str(c).strip().lower() for c in raw.columns]
        symcol = next((c for c in ("symbol", "code", "ticker", "銘柄コード")
                       if c in raw.columns), None)
        if symcol is None:
            raise SystemExit(
                f"{data_file} に銘柄列 (symbol/code/ticker) がありません")
        for sym, g in raw.groupby(symcol):
            nd = _normalize_daily(g.drop(columns=[symcol]), str(sym))
            if nd is not None:
                store[str(sym)] = nd

    if data_dir:
        d = Path(data_dir)
        if not d.is_dir():
            raise SystemExit(f"{data_dir} はディレクトリではありません")
        files = sorted(
            f for f in d.iterdir()
            if f.is_file() and f.name.lower().endswith(
                (".csv", ".csv.gz", ".tsv", ".parquet", ".pq", ".pkl", ".pickle"))
        )
        if not files:
            raise SystemExit(f"{data_dir} に日足ファイルが見つかりません")
        for f in files:
            raw = _read_any(f)
            if raw is None:
                continue
            stem = f.name
            for suf in (".csv.gz", ".csv", ".tsv", ".parquet", ".pq", ".pkl", ".pickle"):
                if stem.lower().endswith(suf):
                    stem = stem[: -len(suf)]
                    break
            nd = _normalize_daily(raw, stem)
            if nd is not None:
                store[stem] = nd

    if not store:
        raise SystemExit("ローカルデータを1銘柄も読み込めませんでした")

    # 7203_T / 7203 / 72030 のような表記ゆれを .T 形式にも引けるようにする
    alias: dict[str, pd.DataFrame] = {}
    for k, v in store.items():
        alias[k] = v
        if k.endswith("_T"):
            alias[k[:-2] + ".T"] = v
        elif k.isdigit() and len(k) == 4:
            alias[k + ".T"] = v
        elif k.isdigit() and len(k) == 5 and k.endswith("0"):
            alias[k[:4] + ".T"] = v
    _LOCAL = alias

    syms = sorted(store.keys())
    span = min(v.index[0] for v in store.values()), max(v.index[-1] for v in store.values())
    print(f"ローカル日足: {len(syms)} 銘柄 / {span[0]:%Y-%m-%d} 〜 {span[1]:%Y-%m-%d}")
    return syms


def load_cache_dir(cache_dir: str, index_symbol: str,
                   want_index: bool = True) -> tuple[list[str], bool]:
    """既存の日足キャッシュ (.rsi2_cache/) をそのまま読む。

    backtest_limit_entry.fetch が作る `<7203_T>.pkl` 形式。index が
    DatetimeIndex、列が小文字 open/high/low/close/volume の DataFrame。

    返り値は (銘柄リスト, 指数が見つかったか)。
    """
    global _LOCAL
    d = Path(cache_dir)
    files = sorted(d.glob("*.pkl")) if d.is_dir() else []
    if not files:
        return [], False

    store: dict[str, pd.DataFrame] = {}
    short = bad = 0
    for f in files:
        if f.name.startswith("earn_"):
            continue
        try:
            with open(f, "rb") as fh:
                raw = pickle.load(fh)
        except Exception:
            bad += 1
            continue
        if not isinstance(raw, pd.DataFrame):
            bad += 1
            continue
        nd = _normalize_daily(raw, f.stem)
        if nd is None:
            short += 1
            continue
        # 7203_T -> 7203.T  /  ^N225 はそのまま
        sym = f.stem if f.stem.startswith("^") else f.stem.replace("_", ".")
        store[sym] = nd

    if not store:
        return [], False

    _LOCAL = dict(store)
    for k, v in list(store.items()):
        if k.endswith(".T"):
            _LOCAL.setdefault(k[:-2] + "_T", v)

    has_index = index_symbol in store
    syms = sorted(k for k in store if k != index_symbol)
    print(f"日足キャッシュ {d}/ : {len(syms)} 銘柄 "
          f"(250本未満で除外 {short} / 読めず {bad})")
    describe_coverage(store, index_symbol)
    if not has_index and want_index:
        print(f"  ⚠ 指数 {index_symbol} がキャッシュにありません "
              f"(yfinance から取得を試みます。不可なら --no-index)")
    return syms, has_index


def describe_coverage(store: dict[str, pd.DataFrame], index_symbol: str) -> None:
    """履歴の開始日の分布を出す。短いキャッシュで長期結論を出さないため。"""
    starts = pd.Series([v.index[0] for k, v in store.items() if k != index_symbol])
    ends = pd.Series([v.index[-1] for k, v in store.items() if k != index_symbol])
    if starts.empty:
        return
    q = starts.quantile([0.1, 0.5, 0.9])
    print(f"  開始日: 最古 {starts.min():%Y-%m-%d} / 中央 {q[0.5]:%Y-%m-%d} "
          f"/ 90%点 {q[0.9]:%Y-%m-%d}")
    print(f"  最終日: 中央 {ends.median():%Y-%m-%d} / 最新 {ends.max():%Y-%m-%d}")
    per_year: dict[int, int] = defaultdict(int)
    for k, v in store.items():
        if k == index_symbol:
            continue
        for y, c in v.index.year.value_counts().items():
            per_year[int(y)] += int(c)
    if per_year:
        yrs = sorted(per_year)
        print("  年別の銘柄日数 (穴が無いかの確認):")
        line = []
        for y in yrs:
            line.append(f"{y}:{per_year[y]:,}")
            if len(line) == 5:
                print("    " + "  ".join(line))
                line = []
        if line:
            print("    " + "  ".join(line))
        med = sorted(per_year.values())[len(per_year) // 2]
        thin = [y for y in yrs if per_year[y] < med * 0.5]
        if thin:
            print(f"    ⚠ 中央値の半分未満の年: {thin} — キャッシュに穴があります")

    span_yrs = (ends.median() - q[0.5]).days / 365.25
    if span_yrs < 5:
        print(f"  ⚠ 中央値で約 {span_yrs:.1f} 年しかありません。年別の符号安定性 (§3) は")
        print(f"    参考程度にしかならないので、長期の結論を出す前に日足を取り直してください。")
    else:
        print(f"  → 中央値で約 {span_yrs:.1f} 年ぶん")


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.replace('.', '_').replace('^', 'IDX_')}.pkl"


def fetch_daily(symbol: str, start: str = START_DATE) -> pd.DataFrame | None:
    """日足を取得。長期履歴用の独自キャッシュ (.gapmr_cache/)。

    backtest_limit_entry.fetch はキャッシュの「開始日」を見ないため、
    長期検証には使えない。ここでは開始日も検証する。
    """
    if _LOCAL is not None:
        return _LOCAL.get(symbol)

    p = _cache_path(symbol)
    want_start = pd.Timestamp(start)
    if p.exists():
        try:
            with open(p, "rb") as f:
                df = pickle.load(f)
            if len(df) > 250 and df.index[0] <= want_start + pd.Timedelta(days=400):
                return df
        except Exception:
            pass

    try:
        import yfinance as yf
        raw = yf.Ticker(symbol).history(
            start=start,
            end=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d", auto_adjust=False, actions=False,
        )
    except Exception:
        return None
    if raw is None or raw.empty or len(raw) < 250:
        return None
    if raw.index.tz is not None:
        raw.index = raw.index.tz_localize(None)
    raw.columns = [str(c).lower() for c in raw.columns]
    raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
    need = ["open", "high", "low", "close"]
    if any(c not in raw.columns for c in need):
        return None
    if "volume" not in raw.columns:
        raw["volume"] = 0.0
    out = raw[need + ["volume"]].astype(float).dropna(subset=need)
    out = out[(out[need] > 0).all(axis=1)]
    if len(out) < 250:
        return None
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        with open(p, "wb") as f:
            pickle.dump(out, f)
    except Exception:
        pass
    return out


def load_universe(path: str | None = None, limit: int | None = None) -> list[str]:
    """銘柄ユニバースを読む。scan_walkforward と同じ優先順位。"""
    import importlib.util

    cands = [path] if path else [
        "symbols_listed_prime.py", "symbols_listed_all.py",
        "symbols_listed_standard.py", "symbols_all.py",
    ]
    for c in cands:
        if not c or not Path(c).exists():
            continue
        spec = importlib.util.spec_from_file_location("_uni", c)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        syms = [s[0] if isinstance(s, (tuple, list)) else s for s in mod.SYMBOLS]
        print(f"universe: {c} ({len(syms)} 銘柄)")
        return syms[:limit] if limit else syms
    raise SystemExit("銘柄リストが見つかりません (fetch_listed_symbols.py で生成してください)")


def fetch_earnings(symbol: str) -> set[pd.Timestamp] | None:
    """決算発表日の集合。取得できなければ None (=不明)。

    yfinance の get_earnings_dates は直近数年しか返さないので、古い期間は
    「不明」扱いになる。不明な銘柄日は除外せず、別グループとして集計する。
    """
    p = CACHE_DIR / f"earn_{symbol.replace('.', '_')}.pkl"
    if p.exists():
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    try:
        import yfinance as yf
        ed = yf.Ticker(symbol).get_earnings_dates(limit=200)
        if ed is None or len(ed) == 0:
            res = None
        else:
            idx = ed.index
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_convert("Asia/Tokyo").tz_localize(None)
            res = {pd.Timestamp(d).normalize() for d in idx}
    except Exception:
        res = None
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        with open(p, "wb") as f:
            pickle.dump(res, f)
    except Exception:
        pass
    return res


# ══════════════════════════════════════════════════════════════════
# パネル構築
# ══════════════════════════════════════════════════════════════════
def _atr_pct(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return (tr.rolling(period).mean() / c).replace([np.inf, -np.inf], np.nan)


def build_symbol_rows(
    df: pd.DataFrame,
    idx: pd.DataFrame,
    symbol: str,
    earn: set | None,
    min_prev_atr: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """1銘柄ぶんの (候補行, 日次ユニバース集計用の全行) を返す。

    候補行だけを保持するのはメモリ対策 (全行だと 1800銘柄 × 4600日)。
    ユニバース平均 o2c は日ごとの (合計, 件数) だけを持ち帰る。
    """
    d = df.copy()
    d["ret"] = d["close"].pct_change()
    d["o2c"] = d["close"] / d["open"] - 1.0
    d["gap"] = d["open"] / d["close"].shift(1) - 1.0
    d["atr"] = _atr_pct(d)
    d["turnover"] = (d["close"] * d["volume"]).rolling(20).mean()
    d["ibs_prev"] = (
        (d["close"] - d["low"]) / (d["high"] - d["low"]).replace(0, np.nan)
    ).shift(1)
    d["prev_ret"] = d["ret"].shift(1)

    # 市場成分の控除: ローリングβ (t-1 までの情報のみ)
    j = d.join(idx[["idx_ret", "idx_gap"]], how="left") if len(idx) else d.assign(
        idx_ret=0.0, idx_gap=0.0)
    if len(idx):
        cov = j["ret"].rolling(BETA_WINDOW).cov(j["idx_ret"])
        var = j["idx_ret"].rolling(BETA_WINDOW).var()
        beta = (cov / var.replace(0, np.nan)).shift(1).clip(-3, 3)
    else:
        # 指数なし: 市場成分を控除しない (resid_gap = 生ギャップ)
        beta = pd.Series(1.0, index=j.index)
    j["beta"] = beta
    j["resid_gap"] = j["gap"] - beta * j["idx_gap"]
    j["resid_prev"] = j["prev_ret"] - beta * j["idx_ret"].shift(1)
    j["prev_close"] = j["close"].shift(1)

    # 有効行
    ok = (
        j["atr"].notna() & j["beta"].notna() & j["o2c"].notna()
        & j["resid_gap"].notna() & j["resid_prev"].notna()
        & j["close"].shift(1).between(MIN_PRICE, MAX_PRICE)
        & (j["atr"] > 0)
    )
    # 値幅制限を超えるギャップ / 日中リターンは起こり得ない = データ破損
    sane = (j["gap"].abs() <= MAX_GAP) & (j["o2c"].abs() <= MAX_GAP) \
        & (j["ret"].abs() <= MAX_GAP)
    bad = j[ok & ~sane]
    if len(bad):
        _note_bad("gap", len(bad), bad.assign(symbol=symbol))
    j = j[ok & sane]
    if j.empty:
        return pd.DataFrame(), pd.DataFrame()

    # 日次ユニバース集計 (α のベースライン)。フィルタ前の広い母集団を使う
    uni = pd.DataFrame({"date": j.index, "o2c": j["o2c"].to_numpy()})

    j["prev_z"] = j["resid_prev"] / j["atr"]
    j["gap_z"] = j["resid_gap"] / j["atr"]
    # 候補 = 本命(前日に大きな動き) + プラセボ用(動きなしだがギャップあり)
    liquid = j["turnover"] >= MIN_TURNOVER
    keep = liquid & ((j["prev_z"].abs() >= min_prev_atr) | (j["gap_z"].abs() >= 0.3))
    cand = j[keep]
    if cand.empty:
        return pd.DataFrame(), uni

    if earn is None:
        earn_flag = np.full(len(cand), -1)          # -1 = 不明
    else:
        # 決算は t-2 の引け後 〜 t の朝 に出る想定。カレンダー日で 0〜3 日前を見る
        dates = pd.DatetimeIndex(cand.index).normalize()
        earn_flag = np.array([
            int(any((d - pd.Timedelta(days=k)) in earn for k in range(0, 4)))
            for d in dates
        ])

    out = pd.DataFrame({
        "date": cand.index,
        "symbol": symbol,
        "prev_ret": cand["prev_ret"].to_numpy(),
        "resid_prev": cand["resid_prev"].to_numpy(),
        "prev_z": cand["prev_z"].to_numpy(),
        "gap": cand["gap"].to_numpy(),
        "resid_gap": cand["resid_gap"].to_numpy(),
        "gap_z": cand["gap_z"].to_numpy(),
        "atr": cand["atr"].to_numpy(),
        "o2c": cand["o2c"].to_numpy(),
        "ibs_prev": cand["ibs_prev"].to_numpy(),
        "turnover": cand["turnover"].to_numpy(),
        "open": cand["open"].to_numpy(),
        "is_earn": earn_flag,
    })
    return out, uni


def build_panel(
    symbols: list[str], workers: int, use_earnings: bool, min_prev_atr: float,
    no_index: bool = False,
) -> tuple[pd.DataFrame, pd.Series]:
    idx_df = None if no_index else fetch_daily(INDEX_SYM)
    if idx_df is None:
        if not no_index:
            raise SystemExit(
                f"{INDEX_SYM} の日足がありません。指数ファイルを同梱するか "
                f"--index-symbol で名前を指定するか、--no-index で市場成分の控除を "
                f"省いてください (その場合 resid_gap = 生ギャップ になります)")
        # 指数なし: β=1・指数リターン0 として扱う (= 市場成分を控除しない)
        idx = pd.DataFrame(columns=["idx_ret", "idx_gap"], dtype=float)
        print("警告: 指数なしで実行します。§1 の α は同日ユニバース平均で代替されますが、"
              "ギャップの市場成分は控除されません。")
    else:
        idx = pd.DataFrame(index=idx_df.index)
        idx["idx_ret"] = idx_df["close"].pct_change()
        idx["idx_gap"] = idx_df["open"] / idx_df["close"].shift(1) - 1.0

    rows: list[pd.DataFrame] = []
    uni_sum: dict = defaultdict(float)
    uni_cnt: dict = defaultdict(int)
    done = failed = 0

    def work(sym: str):
        df = fetch_daily(sym)
        if df is None:
            return None
        earn = fetch_earnings(sym) if (use_earnings and _LOCAL is None) else None
        return build_symbol_rows(df, idx, sym, earn, min_prev_atr)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, s): s for s in symbols}
        for fu in as_completed(futs):
            done += 1
            try:
                res = fu.result()
            except Exception:
                res = None
            if res is None:
                failed += 1
            else:
                cand, uni = res
                if len(uni):
                    g = uni.groupby("date")["o2c"]
                    for dt, v in g.sum().items():
                        uni_sum[dt] += float(v)
                    for dt, v in g.count().items():
                        uni_cnt[dt] += int(v)
                if len(cand):
                    rows.append(cand)
            if done % 200 == 0:
                print(f"  ... {done}/{len(symbols)} (失敗 {failed})", file=sys.stderr)

    if not rows:
        raise SystemExit("候補が1件もありません")
    panel = pd.concat(rows, ignore_index=True).sort_values(["date", "symbol"])
    mkt = pd.Series(
        {d: uni_sum[d] / uni_cnt[d] for d in uni_cnt if uni_cnt[d] >= 20}
    ).sort_index()
    mkt.name = "mkt_o2c"
    print(f"候補 {len(panel):,} 行 / {panel['symbol'].nunique()} 銘柄 / "
          f"{len(mkt):,} 営業日 (取得失敗 {failed})")
    if _INTEGRITY:
        tot = sum(_INTEGRITY.values())
        print(f"データ破損として除外: {tot:,} 銘柄日 "
              f"(OHLC不整合 {_INTEGRITY.get('ohlc', 0):,} / "
              f"値幅制限超え {_INTEGRITY.get('gap', 0):,})")
        print(f"  始値・終値が安値〜高値の外にある行と、|ギャップ| や |日中変化| が "
              f"{MAX_GAP*100:.0f}% を超える行を除外しています。")
        print(f"  内訳を見るには --audit を付けてください。")
    return panel, mkt


# ══════════════════════════════════════════════════════════════════
# シグナル & 損益
# ══════════════════════════════════════════════════════════════════
def apply_signal(panel: pd.DataFrame, mkt: pd.Series, raw: bool,
                 prev_thr: float, gap_thr: float) -> pd.DataFrame:
    """side (+1=買い / -1=空売り) を付与し、シグナル行だけ返す。"""
    p = panel
    if raw:
        up = (p["prev_ret"] >= prev_thr) & (p["gap"] >= gap_thr)
        dn = (p["prev_ret"] <= -prev_thr) & (p["gap"] <= -gap_thr)
    else:
        up = (p["prev_z"] >= prev_thr) & (p["gap_z"] >= gap_thr)
        dn = (p["prev_z"] <= -prev_thr) & (p["gap_z"] <= -gap_thr)

    sig = p[up | dn].copy()
    # 上げ側にギャップ → 空売り、下げ側 → 買い
    sig["side"] = np.where(sig["gap_z"] >= 0, -1.0, 1.0)
    return attach_pnl(sig, mkt)


def bounce_slope(panel: pd.DataFrame) -> float:
    """全銘柄・全日で o2c を gap に回帰した傾き。

    始値は gap の分子であり o2c の分母でもあるため、始値の測定ノイズ
    (板寄せの偏り・気配のバウンス) だけで両者は機械的に負の相関を持つ。
    これは過剰反応とは無関係で、ランダムウォークでも必ず出る。
    シグナルの α からこの分を差し引かないと、微細構造のノイズを
    エッジと誤認する。
    """
    x = panel["gap"].to_numpy(dtype=float)
    y = panel["o2c"].to_numpy(dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 500 or x.std() == 0:
        return 0.0
    return float(np.cov(x, y)[0, 1] / x.var())


def attach_pnl(sig: pd.DataFrame, mkt: pd.Series) -> pd.DataFrame:
    sig = sig.copy()
    sig["ret"] = sig["side"] * sig["o2c"]
    sig["mkt"] = sig["date"].map(mkt)
    sig["alpha"] = sig["ret"] - sig["side"] * sig["mkt"].fillna(0.0)
    # 微細構造 (始値ノイズ) で説明できる分を控除した α
    b = _BOUNCE_SLOPE
    sig["alpha_x"] = sig["alpha"] - sig["side"] * (b * sig["gap"])
    return sig


def to_daily(sig: pd.DataFrame, col: str, cost_bps: float,
             max_names: int | None) -> pd.Series:
    """トレードを日次ポートフォリオに集約する。

    これが検定の単位。トレード単位で数えると、同じ日に出るシグナルが
    ほぼ同一のリスク要因を共有しているためサンプルが水増しされる。
    """
    s = sig
    if max_names:
        # 執行可能性順に上位のみ (売買代金)
        s = s.sort_values(["date", "turnover"], ascending=[True, False])
        s = s.groupby("date").head(max_names)
    net = s[col] - cost_bps / 10000.0
    return net.groupby(s["date"]).mean().sort_index()


def stats(daily: pd.Series) -> dict:
    n = len(daily)
    if n < 10:
        return {"n": n}
    mu, sd = float(daily.mean()), float(daily.std(ddof=1))
    t = mu / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {
        "n": n,
        "mean_bp": mu * 10000,
        "t": t,
        "sharpe": (mu / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0,
        "hit": float((daily > 0).mean()),
        "cum_pct": float(daily.sum()) * 100,
    }


def _fmt(label: str, st: dict, width: int = 26) -> str:
    if st.get("n", 0) < 10:
        return f"  {label:<{width}} データ不足 (n={st.get('n', 0)})"
    return (f"  {label:<{width}} n={st['n']:>5}  平均={st['mean_bp']:>7.2f}bp  "
            f"t={st['t']:>6.2f}  SR={st['sharpe']:>5.2f}  "
            f"勝日={st['hit']*100:>5.1f}%  累計={st['cum_pct']:>8.1f}%")


# ══════════════════════════════════════════════════════════════════
# レポート
# ══════════════════════════════════════════════════════════════════
def report(panel: pd.DataFrame, mkt: pd.Series, args) -> None:
    prev_thr = args.prev_thr if args.prev_thr is not None else (
        PREV_MOVE_PCT if args.raw else PREV_MOVE_ATR)
    gap_thr = args.gap_thr if args.gap_thr is not None else (
        GAP_PCT if args.raw else GAP_ATR)

    global _BOUNCE_SLOPE
    _BOUNCE_SLOPE = bounce_slope(panel)

    sig = apply_signal(panel, mkt, args.raw, prev_thr, gap_thr)
    mode = "生%" if args.raw else "ATR単位"
    print("\n" + "=" * 78)
    print(f"ギャップ反転 検証レポート  [{mode}]  "
          f"前日={prev_thr}  ギャップ={gap_thr}  コスト={args.cost_bps}bp 往復")
    print(f"期間 {panel['date'].min():%Y-%m-%d} 〜 {panel['date'].max():%Y-%m-%d}"
          f"   シグナル {len(sig):,} 件 "
          f"({len(sig)/max(len(mkt),1):.1f} 件/日)")
    print("=" * 78)

    if len(sig) < 50:
        print("シグナルが少なすぎます。閾値を緩めてください。")
        return

    # ── §1 生リターン vs α ────────────────────────────────────────
    print("\n【1】生リターン と α (同日ユニバース平均を控除)")
    print("  生リターンが正でも α が消えるなら、それは市場の日中ドリフトです。")
    d_ret = to_daily(sig, "ret", args.cost_bps, args.max_names)
    d_alp = to_daily(sig, "alpha", args.cost_bps, args.max_names)
    print(_fmt("生リターン", stats(d_ret)))
    print(_fmt("α (市場控除後)", stats(d_alp)))
    d_ax = to_daily(sig, "alpha_x", args.cost_bps, args.max_names)
    print(_fmt("α (始値ノイズも控除)", stats(d_ax)))
    print(f"  無条件回帰 o2c ~ gap の傾き = {_BOUNCE_SLOPE:+.4f}"
          f"  (平均ギャップ {sig['gap'].abs().mean()*100:.2f}% で "
          f"{abs(_BOUNCE_SLOPE) * sig['gap'].abs().mean() * 10000:.1f}bp 相当)")
    print("  始値は gap の分子かつ o2c の分母なので、始値の測定ノイズだけで")
    print("  両者は機械的に負相関します。ランダムウォークでも出るので、")
    print("  ★ 最後の行が消えるなら、それは過剰反応ではなく微細構造ノイズです。")

    # ── §2 サンプル水増しの実演 ───────────────────────────────────
    print("\n【2】検定単位の違い (トレード単位は t を水増しする)")
    tr = sig["alpha"] - args.cost_bps / 10000.0
    t_trade = float(tr.mean() / (tr.std(ddof=1) / math.sqrt(len(tr)))) if tr.std() > 0 else 0.0
    st = stats(d_alp)
    print(f"  トレード単位 t = {t_trade:>6.2f}  (n={len(tr):,})   ← 使ってはいけない")
    print(f"  日次集約   t = {st.get('t', 0):>6.2f}  (n={st.get('n', 0):,})   ← こちらが正しい")
    if abs(t_trade) > 0:
        print(f"  水増し率 = {abs(t_trade) / max(abs(st.get('t', 1e-9)), 1e-9):.1f}x")

    # ── §3 年別の符号安定性 ───────────────────────────────────────
    print("\n【3】年別 α (符号が安定しているか / 特定の年だけで稼いでいないか)")
    yr = d_alp.groupby(d_alp.index.year)
    pos = 0
    for y, s in yr:
        m = s.mean() * 10000
        pos += m > 0
        bar = "+" * min(int(abs(m) / 2), 30) if m > 0 else "-" * min(int(abs(m) / 2), 30)
        print(f"    {y}  n={len(s):>4}  {m:>7.2f}bp/日  {bar}")
    print(f"  → プラスの年: {pos}/{yr.ngroups}")

    # ── §4 ギャップ幅の単調性 ─────────────────────────────────────
    print("\n【4】ギャップ幅(ATR単位)の分位別 α  ← 単調に増えなければノイズ")
    cand = panel[panel["prev_z"].abs() >= prev_thr] if not args.raw else \
        panel[panel["prev_ret"].abs() >= prev_thr]
    c = cand.copy()
    c["side"] = np.where(c["gap_z"] >= 0, -1.0, 1.0)
    c = attach_pnl(c, mkt)
    c["gapmag"] = c["gap_z"].abs()
    try:
        c["q"] = pd.qcut(c["gapmag"], 5, labels=False, duplicates="drop")
    except ValueError:
        c["q"] = 0
    for q, g in c.groupby("q"):
        dq = to_daily(g, "alpha", args.cost_bps, None)
        s = stats(dq)
        rng = f"{g['gapmag'].min():.2f}〜{g['gapmag'].max():.2f} ATR"
        print(_fmt(f"Q{int(q)+1} ({rng})", s, width=26))

    # ── §5 プラセボ ───────────────────────────────────────────────
    print("\n【5】プラセボ (予想通りに壊れるか)")
    # (a) 前日の大きな動きなし + ギャップだけ
    if args.raw:
        flat = panel[panel["prev_ret"].abs() < prev_thr * 0.3]
        pl = flat[flat["gap"].abs() >= gap_thr]
    else:
        flat = panel[panel["prev_z"].abs() < prev_thr * 0.3]
        pl = flat[flat["gap_z"].abs() >= gap_thr]
    if len(pl) > 50:
        pl = pl.copy()
        pl["side"] = np.where(pl["gap_z"] >= 0, -1.0, 1.0)
        pl = attach_pnl(pl, mkt)
        print(_fmt("(a) 前日の動きなし+ギャップ", stats(to_daily(pl, "alpha", args.cost_bps, None)), 26))
        print("      → 本命より明確に弱いはず。同等なら『過剰反応の巻き戻し』という説明が誤り")
    else:
        print("  (a) サンプル不足")

    # (b) ギャップ方向を逆に取る
    rev = sig.copy()
    rev["side"] *= -1
    rev = attach_pnl(rev.drop(columns=["ret", "alpha", "mkt"]), mkt)
    print(_fmt("(b) 符号反転", stats(to_daily(rev, "alpha", args.cost_bps, args.max_names)), 26))

    # (c) 決算グループ
    if (sig["is_earn"] >= 0).any():
        print("\n  (c) 決算の有無で分離 (決算側は反転せずドリフト = 逆符号が予想)")
        for flag, name in [(0, "決算なし"), (1, "決算あり"), (-1, "決算情報なし")]:
            g = sig[sig["is_earn"] == flag]
            if len(g) > 50:
                print(_fmt(f"      {name}", stats(to_daily(g, "alpha", args.cost_bps, args.max_names)), 22))
    else:
        print("\n  (c) 決算日は未取得 (--earnings を付けると分離できます)")

    # ── §6 コスト感応度 ───────────────────────────────────────────
    print("\n【6】コスト感応度 (往復bp) — エッジがコストの3倍あるか")
    base = to_daily(sig, "alpha", 0.0, args.max_names)
    gross = base.mean() * 10000
    print(f"  コスト前の1トレードあたり α = {sig['alpha'].mean()*10000:.2f}bp")
    for cb in (0, 10, 20, 30, 50, 80):
        s = stats(to_daily(sig, "alpha", cb, args.max_names))
        mark = "  ← 現在の想定" if cb == args.cost_bps else ""
        print(_fmt(f"cost={cb}bp", s, 26) + mark)
    if args.cost_bps > 0:
        print(f"  エッジ/コスト比 = {abs(sig['alpha'].mean()*10000) / args.cost_bps:.2f}"
              f"  (3.0 未満なら実運用は見送り)")

    # ── §7 資金制約下の実効ポートフォリオ ─────────────────────────
    if args.max_names:
        print(f"\n【7】1日あたり最大 {args.max_names} 銘柄 (売買代金上位) に制限した場合")
        per_day = sig.groupby("date").size()
        print(f"  シグナル数/日: 中央値 {per_day.median():.0f} / 90%点 {per_day.quantile(.9):.0f} "
              f"/ 最大 {per_day.max():.0f}")
        cap = to_daily(sig, "alpha", args.cost_bps, args.max_names)
        allx = to_daily(sig, "alpha", args.cost_bps, None)
        print(_fmt("全件", stats(allx)))
        print(_fmt(f"上位{args.max_names}件", stats(cap)))
        # 資金換算: 日次平均は「その日のシグナルに資金を等分した」リターンなので
        # 資金を掛けるだけでよい (銘柄数を掛けると二重計上になる)。
        # 発火しない日は 0 なので、年換算は「年あたりの発火日数」で行う。
        n_years = max((sig["date"].max() - sig["date"].min()).days / 365.25, 1e-9)
        days_per_year = len(cap) / n_years
        per_active_day = float(cap.mean()) * args.capital
        print(f"  発火日 {len(cap):,}日 / 全{len(mkt):,}営業日 "
              f"({len(cap)/max(len(mkt),1)*100:.1f}%) = 年 {days_per_year:.0f} 日")
        print(f"  参考: 資金{args.capital/1e4:.0f}万円を発火日にフル投入した場合、"
              f"発火日あたり {per_active_day:,.0f}円")
        print(f"        年換算 {per_active_day * days_per_year:,.0f}円 "
              f"(= 発火日あたり × 年{days_per_year:.0f}日)")

    # ── §8 裾の依存度 ────────────────────────────────────────────
    print("\n【8】裾の依存度 (少数の日で稼いでいないか)")
    d = to_daily(sig, "alpha", args.cost_bps, args.max_names).sort_values()
    if len(d) >= 20:
        tot = float(d.sum())
        top5 = float(d.tail(5).sum())
        top10p = float(d.tail(max(len(d) // 10, 1)).sum())
        trimmed = d.iloc[int(len(d) * 0.01): len(d) - int(len(d) * 0.01)]
        print(f"  平均 {d.mean()*10000:>8.2f}bp  /  中央値 {d.median()*10000:>8.2f}bp")
        if tot > 0:
            print(f"  上位5日が総損益に占める割合   : {top5/tot*100:>6.1f}%")
            print(f"  上位10%の日が占める割合       : {top10p/tot*100:>6.1f}%"
                  + ("   (100%超 = 残りの日は合計でマイナス)"
                     if top10p / tot > 1 else ""))
            if top10p / tot > 0.9:
                print("  ⚠ 上位10%の日で総損益の9割以上。"
                      "少数の極端な日に依存しています。")
        else:
            print("  総損益がプラスでないため、上位日の寄与率は省略します。")
        st_tr = stats(trimmed)
        print(_fmt("上下1%を除いた後", st_tr))
        if st_tr.get("t", 0) < 2:
            print("  ⚠ 裾を落とすと t が 2 を切ります。エッジの実体は薄いです。")

    # ── §9 売買方向の分離 ────────────────────────────────────────
    print("\n【9】方向別 (制度的な制約が全く違うので必ず分けて見る)")
    print("  空売り側 = ギャップアップを売る。空売り規制・貸株の可否・増担保・")
    print("             値幅制限が効く。買い側 = ギャップダウンを買う。制約なし。")
    for sd, name in ((1.0, "買い側 (ギャップダウンを買う)"),
                     (-1.0, "空売り側 (ギャップアップを売る)")):
        g = sig[sig["side"] == sd]
        if len(g) < 30:
            print(f"  {name}: サンプル不足 ({len(g)}件)")
            continue
        st = stats(to_daily(g, "alpha", args.cost_bps, args.max_names))
        print(_fmt(name, st, width=30))
        print(f"      グロス {g['alpha'].mean()*10000:>6.1f}bp/件  "
              f"{len(g):,}件  平均ギャップ {g['gap'].abs().mean()*100:.2f}%")
    print("  → 買い側だけで成立するなら、空売りの制度的制約を回避できます。")
    print("     空売り側にしか無いなら、実弾の前に証券会社への照会が必須です。")

    print("\n" + "=" * 78)
    print("判定の目安: 日次t>3 かつ 年別で大半がプラス かつ 単調性あり かつ")
    print("            エッジ/コスト比>3 のとき初めて分足での執行検証に進む価値がある。")
    print("=" * 78)


def audit_report() -> None:
    """データ破損として除外した行を一覧する。原因の切り分け用。

    典型的な原因:
      - 株式分割の未調整 (比率がきれいな整数倍になる)
      - yfinance の欠損・誤植 (始値だけ桁が違う など)
    """
    if not _INTEGRITY_ROWS:
        print("\nデータ破損として除外した行はありません。")
        return
    rows = sorted(_INTEGRITY_ROWS,
                  key=lambda r: abs(r["gap_pct"]) if r["gap_pct"] == r["gap_pct"]
                  else 0, reverse=True)
    print("\n" + "=" * 78)
    print(f"データ破損として除外した行 (最大 {_AUDIT_CAP} 件を保持、"
          f"|ギャップ| 降順で上位30件)")
    print("=" * 78)
    print(f"  {'種別':<6}{'銘柄':<10}{'日付':<12}"
          f"{'前日終値':>12}{'始値':>12}{'高値':>12}{'安値':>12}{'終値':>12}"
          f"{'ギャップ%':>14}")
    def _n(v, w=12, d=1):
        return f"{'-':>{w}}" if v != v else f"{v:>{w},.{d}f}"

    for r in rows[:30]:
        dt = r["date"]
        ds = f"{dt:%Y-%m-%d}" if hasattr(dt, "year") else str(dt)[:10]
        print(f"  {r['kind']:<6}{str(r['symbol']):<10}{ds:<12}"
              f"{_n(r['prev_close'])}{_n(r['open'])}"
              f"{_n(r['high'])}{_n(r['low'])}{_n(r['close'])}"
              f"{_n(r['gap_pct'], 14)}")
    print("\n  始値だけが桁違いなら yfinance の誤植、OHLC 全体が整数倍なら")
    print("  株式分割の未調整です。該当銘柄のキャッシュを消して取り直してください。")
    print("=" * 78)


def grid_scan(panel: pd.DataFrame, mkt: pd.Series, args) -> None:
    """閾値の周辺を総当たりして「台地か尖りか」を見る。

    ある1点だけ成績が良く周囲が悪いなら、それはデータを何度も見た結果の
    偶然 (多重比較) である可能性が高い。エッジが本物なら、閾値を少し
    動かしても成績はなだらかに変化する。

    前日の動きに 0.0 (条件なし) の行を含めるのが要点。ギャップだけで
    同じ成績が出るなら、「前日に大きく動いた」という条件は不要である。

    パネルは |gap_z| >= 0.3 の行を必ず保持しているので、ギャップ閾値が
    0.3 以上の区画は前日閾値によらず完全な母集団になっている。
    """
    global _BOUNCE_SLOPE
    _BOUNCE_SLOPE = bounce_slope(panel)

    def _parse(spec: str, default: list[float]) -> list[float]:
        if not spec:
            return default
        return [float(x) for x in spec.replace(",", " ").split()]

    prevs = _parse(args.grid_prev, [0.0, 0.5, 1.0, 1.25, 1.5, 2.0, 2.5])
    gaps = _parse(args.grid_gap, [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0])
    if min(gaps) < 0.3:
        print("  ⚠ ギャップ閾値 0.3 未満の区画は母集団が不完全です "
              "(パネルの保持条件が |gap_z| >= 0.3)")

    cells: dict[tuple[float, float], tuple[float, float, int]] = {}
    for pv in prevs:
        for gp in gaps:
            sig = apply_signal(panel, mkt, args.raw, pv, gp)
            if len(sig) < 30:
                continue
            st = stats(to_daily(sig, "alpha", args.cost_bps, args.max_names))
            if st.get("n", 0) < 10:
                continue
            cells[(pv, gp)] = (st["t"], sig["alpha"].mean() * 10000, len(sig))

    def _table(title: str, pick, fmt: str) -> None:
        print(f"\n{title}")
        print("   前日\\ギャップ" + "".join(f"{g:>8.2f}" for g in gaps))
        for pv in prevs:
            row = "".join(
                (f"{pick(cells[(pv, g)]):>8{fmt}}" if (pv, g) in cells
                 else f"{'-':>8}")
                for g in gaps)
            print(f"   {pv:>10.2f}" + row)

    print("\n" + "=" * 78)
    print(f"閾値グリッド  行 = 前日の動き (ATR単位) / 列 = ギャップ (ATR単位)")
    print(f"前日 0.00 = 前日の動きを条件にしない")
    print("=" * 78)
    _table(f"[1] 日次 t (コスト {args.cost_bps}bp 控除後)", lambda c: c[0], ".2f")
    _table("[2] グロス α (1トレードあたり bp、コスト控除前)", lambda c: c[1], ".0f")
    _table("[3] シグナル件数", lambda c: c[2], "d")

    print("\n  読み方:")
    print("   - t の高い区画が『面』で広がっていれば本物。1区画だけ高く周囲が")
    print("     低いなら、その閾値はデータを見て選んだ偶然の可能性が高い。")
    print("   - 前日 0.00 の行が下の行と同じなら、前日の条件は効いていない。")
    print("   - 件数が3桁を切る区画は、t の値によらず信頼できない。")
    print("   - 端の列で単調増加が続いているなら、探索範囲が狭い。--grid-gap で伸ばす。")
    if cells:
        best = max(cells.items(), key=lambda kv: kv[1][0])
        (bp, bg), (bt, ba, bn) = best
        print(f"\n  最良 t: 前日={bp} / ギャップ={bg} → "
              f"t={bt:.2f} / グロス={ba:.0f}bp / {bn}件")
        print(f"  そのまま実行するには: "
              f"--prev-thr {bp} --gap-thr {bg}")
    print("=" * 78)


# ══════════════════════════════════════════════════════════════════
# セルフテスト (合成データで配管を確認)
# ══════════════════════════════════════════════════════════════════
def self_test() -> int:
    """既知のエッジを埋め込んだ合成データで、レポートがそれを検出できるか。"""
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2015-01-01", "2025-12-31")
    n = len(dates)
    idx = pd.DataFrame(index=dates)
    idx["idx_ret"] = rng.normal(0, 0.01, n)
    idx["idx_gap"] = rng.normal(0, 0.004, n)

    rows, uni_s, uni_c = [], defaultdict(float), defaultdict(int)
    for k in range(60):
        sym = f"T{k:04d}.T"
        ret = rng.normal(0, 0.02, n) + 1.0 * idx["idx_ret"].to_numpy()
        gap = rng.normal(0, 0.008, n) + 1.0 * idx["idx_gap"].to_numpy()
        o2c = rng.normal(0, 0.015, n)
        prev = np.roll(ret, 1)
        rg = gap - idx["idx_gap"].to_numpy()
        rp = prev - np.roll(idx["idx_ret"].to_numpy(), 1)
        atr = np.full(n, 0.02)
        # 埋め込むエッジ: 条件成立時に 25bp ぶん反転させる
        hit = (np.abs(rp) >= 1.0 * atr) & (np.abs(rg) >= 0.5 * atr) & (np.sign(rp) == np.sign(rg))
        o2c = o2c - np.sign(rg) * hit * 0.0025
        df = pd.DataFrame({
            "date": dates, "symbol": sym, "prev_ret": prev, "resid_prev": rp,
            "prev_z": rp / atr, "gap": gap, "resid_gap": rg, "gap_z": rg / atr,
            "atr": atr, "o2c": o2c, "ibs_prev": 0.5, "turnover": 1e9,
            "prev_close": 2000.0, "is_earn": -1,
        })
        keep = (np.abs(df["prev_z"]) >= 0.9) | (np.abs(df["gap_z"]) >= 0.3)
        rows.append(df[keep])
        for dt, v in zip(dates, o2c):
            uni_s[dt] += float(v); uni_c[dt] += 1

    panel = pd.concat(rows, ignore_index=True).sort_values(["date", "symbol"])
    mkt = pd.Series({d: uni_s[d] / uni_c[d] for d in uni_c}).sort_index()

    args = argparse.Namespace(raw=False, prev_thr=1.0, gap_thr=0.5, cost_bps=0.0,
                              max_names=13, capital=4_000_000)
    report(panel, mkt, args)

    sig = apply_signal(panel, mkt, False, 1.0, 0.5)
    st = stats(to_daily(sig, "alpha", 0.0, None))
    sx = stats(to_daily(sig, "alpha_x", 0.0, None))
    ok = st["t"] > 5 and st["mean_bp"] > 5 and sx["mean_bp"] > 5
    print(f"\nSELF-TEST: 埋め込みエッジ 25bp → 検出 {st['mean_bp']:.1f}bp (t={st['t']:.1f})"
          f" / 始値ノイズ控除後 {sx['mean_bp']:.1f}bp (t={sx['t']:.1f})")
    print("SELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    global INDEX_SYM
    ap = argparse.ArgumentParser(description="ギャップ反転の日足検証")
    ap.add_argument("--symbols", help="銘柄リスト .py")
    ap.add_argument("--data-dir", dest="data_dir",
                    help="ローカル日足のディレクトリ (<SYM>.csv/.parquet/.pkl)")
    ap.add_argument("--data-file", dest="data_file",
                    help="ローカル日足の長形式1ファイル (symbol,date,ohlcv)")
    ap.add_argument("--cache-dir", dest="cache_dir", default=".rsi2_cache",
                    help="既存の日足キャッシュ (既定 .rsi2_cache)。"
                         "存在すれば自動で使う")
    ap.add_argument("--no-cache", dest="no_cache", action="store_true",
                    help="キャッシュを使わず yfinance から取得する")
    ap.add_argument("--grid", action="store_true",
                    help="閾値を総当たりして台地か尖りかを見る")
    ap.add_argument("--grid-prev", dest="grid_prev", default="",
                    help="グリッドの前日閾値 (例 \"0,0.5,1,1.5,2\")")
    ap.add_argument("--grid-gap", dest="grid_gap", default="",
                    help="グリッドのギャップ閾値 (例 \"1,2,3,4,5\")")
    ap.add_argument("--audit", action="store_true",
                    help="データ破損として除外した行を一覧表示する")
    ap.add_argument("--max-gap", dest="max_gap", type=float, default=MAX_GAP,
                    help=f"これを超えるギャップ/日中変化はデータ破損として除外 "
                         f"(既定 {MAX_GAP})")
    ap.add_argument("--coverage", action="store_true",
                    help="データの被覆状況だけ出して終了する")
    ap.add_argument("--index-symbol", dest="index_symbol", default=INDEX_SYM,
                    help=f"市場成分の控除に使う指数 (既定 {INDEX_SYM})")
    ap.add_argument("--no-index", dest="no_index", action="store_true",
                    help="指数が無い場合。市場成分を控除せず生ギャップで検証する")
    ap.add_argument("--limit", type=int, help="先頭N銘柄だけ (デバッグ)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--raw", action="store_true", help="ATR正規化せず生パーセントの固定閾値を使う")
    ap.add_argument("--prev-thr", dest="prev_thr", type=float)
    ap.add_argument("--gap-thr", dest="gap_thr", type=float)
    ap.add_argument("--cost-bps", dest="cost_bps", type=float, default=COST_BPS)
    ap.add_argument("--max-names", dest="max_names", type=int, default=13,
                    help="1日に建てられる最大銘柄数 (資金制約)")
    ap.add_argument("--capital", type=float, default=4_000_000)
    ap.add_argument("--earnings", action="store_true", help="決算日を取得して分離")
    ap.add_argument("--csv", help="候補パネルを CSV に書き出す")
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    INDEX_SYM = args.index_symbol
    globals()["MAX_GAP"] = args.max_gap

    min_prev = (args.prev_thr if args.prev_thr is not None
                else (PREV_MOVE_PCT if args.raw else PREV_MOVE_ATR))

    syms: list[str] = []
    if args.data_dir or args.data_file:
        syms = load_local(args.data_dir, args.data_file)
        syms = [x for x in syms if x != INDEX_SYM]
    elif not args.no_cache:
        syms, has_index = load_cache_dir(args.cache_dir, INDEX_SYM,
                                         want_index=not args.no_index)
        if syms and not has_index and not args.no_index:
            # 指数だけ yfinance から取りに行く (_LOCAL に無ければ fetch_daily が
            # None を返すので、ここで明示的に取得して _LOCAL に載せる)
            saved = _LOCAL
            globals()["_LOCAL"] = None      # 一時的に yfinance 経路を使う
            idx_df = fetch_daily(INDEX_SYM)
            globals()["_LOCAL"] = saved
            if idx_df is not None:
                _LOCAL[INDEX_SYM] = idx_df
                print(f"  指数 {INDEX_SYM} を yfinance から取得しました")
            else:
                print(f"  指数 {INDEX_SYM} を取得できませんでした。"
                      f"--no-index を付けて再実行してください")
                return 1
    if not syms:
        syms = load_universe(args.symbols, args.limit)
    elif args.limit:
        syms = syms[: args.limit]

    if args.coverage:
        print(f"\n対象 {len(syms)} 銘柄。--coverage 指定のためここで終了します。")
        return 0
    keep_thr = 0.9 if args.grid else min_prev * (1.0 if args.raw else 0.9)
    panel, mkt = build_panel(syms, args.workers, args.earnings, keep_thr,
                             no_index=args.no_index)
    if args.csv:
        panel.to_csv(args.csv, index=False)
        print(f"パネルを {args.csv} に保存")
    if args.audit:
        audit_report()
    if args.grid:
        grid_scan(panel, mkt, args)
    else:
        report(panel, mkt, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
