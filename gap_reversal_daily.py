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
_BOUNCE: tuple = (np.array([0.0, np.inf]), np.array([0.0]),
                  np.array([0]))   # bounce_slopes() が実行時に設定

# 前夜に監視できる枠の検査。板の購読上限は50銘柄、前夜に寄指を置く方式なら
# 予算で決まる (建玉30万・資金400万なら13件)。
TOPN_LIST = [13, 25, 50, 100, 200]
TOPN_MAX = 200
_TOPN_THR: dict = {}

# kabu の /ranking は 値上がり率/値下がり率 の **上位50件** を返す。
# ⛔ 2026-08-30 実測。「通常30件」は誤りだった (Count/Limit/Size/Page/Offset の
#   5種すべてで 50件のまま = 50が固定の上限)。判定は N=50 の行で読むこと。
# 生の騰落率での並びなので、ATR正規化した条件と食い違う。その差を測る。
RANK_LIST = [10, 20, 30, 50]
RANK_MAX = 50
_RANK_THR: dict = {}

COST_BPS      = 30.0    # 往復コスト (bp)。呼値+板寄せ+引け成行の想定
TRADING_DAYS  = 245


# ══════════════════════════════════════════════════════════════════
# データ取得
# ══════════════════════════════════════════════════════════════════
# ローカル日足を読み込んだ場合の置き場 (yfinance を使わずに済ませる)
_LOCAL: dict[str, pd.DataFrame] | None = None

# N ルール (固定%の別実装) を同じパネルで測るためのフラグ。
# 候補行の keep 条件が ATR 正規化ベースなので、生%の条件で発火する行が
# 落ちることがある。--nrule のときだけ keep を広げる。
_NRULE: bool = False
N_PREV_THR = 0.01753     # ⛔ 1.75% ではない。丸めない
N_GAP_THR  = 0.01000
N_TOPN     = 50          # 売買代金20日平均の上位N銘柄
N_ONEWAY_BPS = 4.4       # 呼値1円 / 建値2,257円
# 日ごとのユニバース銘柄数 (年別の発火率を出すため)
_UNI_CNT: pd.Series | None = None

_NEED = ["open", "high", "low", "close"]

# 東証には値幅制限があるので、これを超えるギャップは物理的に起こり得ない。
# 1,000〜6,000円の株の制限値幅は最大でも 30% 程度。
MAX_GAP = 0.30

# データ破損の集計。ThreadPoolExecutor から触るのでロックを持つ。
_INTEGRITY: dict[str, int] = defaultdict(int)
_INTEGRITY_ROWS: list[dict] = []
_INTEGRITY_LOCK = threading.Lock()
_AUDIT_CAP = 400

# 年 × 除外理由 の件数。年が丸ごと消えたときに「どの条件が食べたか」を出す。
# ⛔ 指数に穴があると resid_gap / beta が NaN になり、エラーも警告も出さずに
#   その期間の銘柄行が全部落ちます。実際に 2017-07〜2019-07 で起きました。
_DROP: dict = defaultdict(lambda: defaultdict(int))
_DROP_LOCK = threading.Lock()


def _note_drop(year_counts: dict) -> None:
    with _DROP_LOCK:
        for y, d in year_counts.items():
            for k, v in d.items():
                _DROP[y][k] += v


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
    # ⛔ 先読み防止: 20日平均売買代金は **当日を含めてはいけません**。
    #   監視枠 (上位50) は前夜に選ぶので、当日の出来高は使えません。
    #   rolling(20).mean() は当日を含むので shift(1) が要ります。
    #   (2026-08-29 に検査で発見。§11「前夜の売買代金」が実際には
    #    当日の出来高を見ていました)
    d["turnover"] = (d["close"] * d["volume"]).rolling(20).mean().shift(1)
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
    j["next_open"] = j["open"].shift(-1)     # 持ち越し評価にだけ使う (先読み)
    j["next_close"] = j["close"].shift(-1)   # 同上 (D+2 も張り付いたかの判定)

    # 騰落率ランキング用の母集団。kabu の /ranking は価格帯で絞らないので、
    # ここも価格帯フィルタを掛ける前の生ギャップを使う。
    rk = j.loc[j["gap"].notna() & (j["gap"].abs() <= MAX_GAP), "gap"]
    rank_pool = pd.DataFrame({"date": rk.index, "gap": rk.to_numpy()})

    price_ok = j["close"].shift(1).between(MIN_PRICE, MAX_PRICE)
    ok = (
        j["atr"].notna() & j["beta"].notna() & j["o2c"].notna()
        & j["resid_gap"].notna() & j["resid_prev"].notna()
        & price_ok & (j["atr"] > 0)
    )
    # どの条件が行を落としたかを年別に数える (先に評価した条件を優先)
    yrs_all = pd.Series(j.index.year, index=j.index)
    reasons = [
        ("株価帯", ~price_ok),
        ("ATR", j["atr"].isna() | (j["atr"] <= 0)),
        ("β (指数の穴)", j["beta"].isna()),
        ("残差ギャップ (指数の穴)", j["resid_gap"].isna()),
        ("残差 前日 (指数の穴)", j["resid_prev"].isna()),
        ("o2c", j["o2c"].isna()),
    ]
    yc: dict = defaultdict(lambda: defaultdict(int))
    taken = pd.Series(False, index=j.index)
    for label, m in reasons:
        m = m & ~taken
        taken = taken | m
        if m.any():
            for y, c in yrs_all[m].value_counts().items():
                yc[int(y)][label] += int(c)
    for y, c in yrs_all[ok].value_counts().items():
        yc[int(y)]["残った"] += int(c)
    _note_drop(yc)
    # 値幅制限を超えるギャップ / 日中リターンは起こり得ない = データ破損
    # ⛔ ただし条件を「建てる前に分かる量」と「出口の量」に分けること。
    #   o2c は 始値→終値 = **損益そのもの**なので、採否に使うと結果に相関した
    #   選別になります。しかも 1,000円の株の値幅制限は ±300円 = ±30% なので、
    #   |o2c| > 30% は破損とは限らず、**ストップ安の日が消えます**。
    #   それは §10 で「決済できない恐れ」として数えた日そのものです。
    sane_pre = (j["gap"].abs() <= MAX_GAP) & (j["ret"].abs() <= MAX_GAP)
    sane_out = j["o2c"].abs() <= MAX_GAP          # ⛔ 出口を読む
    bad = j[ok & ~(sane_pre & sane_out)]
    if len(bad):
        _note_bad("gap", len(bad), bad.assign(symbol=symbol))
    n_out = int((ok & sane_pre & ~sane_out).sum())
    if n_out:
        _note_bad("出口(o2c)で除外", n_out)
    j = j[ok & sane_pre & sane_out]
    if j.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # 日次ユニバース集計 (α のベースライン)。フィルタ前の広い母集団を使う
    uni = pd.DataFrame({"date": j.index, "o2c": j["o2c"].to_numpy(),
                        "turnover": j["turnover"].to_numpy()})

    j["prev_z"] = j["resid_prev"] / j["atr"]
    j["gap_z"] = j["resid_gap"] / j["atr"]
    # 候補 = 本命(前日に大きな動き) + プラセボ用(動きなしだがギャップあり)
    liquid = j["turnover"] >= MIN_TURNOVER
    keep = liquid & ((j["prev_z"].abs() >= min_prev_atr) | (j["gap_z"].abs() >= 0.3))
    if _NRULE:
        # 生%の固定閾値でも発火しうる行を落とさない
        keep = keep | (liquid
                       & (j["prev_ret"].abs() >= N_PREV_THR)
                       & (j["gap"].abs() >= N_GAP_THR))
    cand = j[keep]
    if cand.empty:
        return pd.DataFrame(), uni, rank_pool

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
        "next_open": cand["next_open"].to_numpy(),
        "idx_gap": cand["idx_gap"].to_numpy(),
        "beta": cand["beta"].to_numpy(),
        "next_close": cand["next_close"].to_numpy(),
        "is_earn": earn_flag,
    })
    return out, uni, rank_pool


def universe_index(symbols: list[str], workers: int) -> pd.DataFrame:
    """ユニバース自身から市場ファクターを作る (等加重)。

    なぜ必要か: yfinance の ^N225 には穴があり、そこで beta と resid_gap が
    NaN になって **銘柄行が丸ごと消えます**。実測で 2017-07〜2019-07 の
    約2年が標本から抜けていました。ユニバースから作れば構造上穴が空きません。

    ⚠ 日経平均は株価加重、これは等加重なので厳密には別物です。β の水準が
      変わるので、^N225 で出した数字と直接は比較できません。ただし
      「市場成分を控除する」目的にはこちらの方が素直です (母集団と同じ銘柄)。
    """
    r_sum: dict = defaultdict(float)
    r_cnt: dict = defaultdict(int)
    g_sum: dict = defaultdict(float)
    g_cnt: dict = defaultdict(int)
    lock = threading.Lock()

    def one(sym: str) -> None:
        df = fetch_daily(sym)
        if df is None or len(df) < 250:
            return
        ret = df["close"].pct_change()
        gap = df["open"] / df["close"].shift(1) - 1.0
        # 破損行は市場ファクターを歪めるので落とす
        m = (ret.abs() <= MAX_GAP) & (gap.abs() <= MAX_GAP)
        ret, gap = ret[m], gap[m]
        with lock:
            for dt, v in ret.dropna().items():
                r_sum[dt] += float(v); r_cnt[dt] += 1
            for dt, v in gap.dropna().items():
                g_sum[dt] += float(v); g_cnt[dt] += 1

    print("  ユニバースから市場ファクターを作ります (等加重) ...")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, symbols))
    # 20銘柄未満の日は信頼できないので落とす
    idx = pd.DataFrame({
        "idx_ret": pd.Series({d: r_sum[d] / r_cnt[d]
                              for d in r_cnt if r_cnt[d] >= 20}),
        "idx_gap": pd.Series({d: g_sum[d] / g_cnt[d]
                              for d in g_cnt if g_cnt[d] >= 20}),
    }).sort_index().dropna()
    print(f"    {len(idx):,} 営業日 "
          f"({idx.index.min():%Y-%m-%d} 〜 {idx.index.max():%Y-%m-%d})")
    return idx


def build_panel(
    symbols: list[str], workers: int, use_earnings: bool, min_prev_atr: float,
    no_index: bool = False, index_from_universe: bool = False,
) -> tuple[pd.DataFrame, pd.Series]:
    if index_from_universe:
        idx = universe_index(symbols, workers)
        idx_df = None
        no_index = False
        return _build_panel_with(symbols, workers, use_earnings, min_prev_atr,
                                 idx, idx_df)
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

    # ⛔ 指数に穴があると、その期間の銘柄行が丸ごとパネルから消えます。
    #   resid_gap / resid_prev が NaN になり ok が False になるためで、
    #   **エラーも警告も出ずに1年が消える**。実際に yfinance の ^N225 で
    #   起きました。年別表から年が丸ごと欠けていたら真っ先にここを疑うこと。
    if len(idx):
        v = idx["idx_ret"].notna() & idx["idx_gap"].notna()
        by_year = v.groupby(idx.index.year).agg(["sum", "size"])
        thin = by_year[by_year["sum"] < 200]
        if len(thin):
            print("  ⛔ 指数の有効行が薄い年があります。"
                  "この年の銘柄行はパネルから丸ごと消えます:")
            for y, r in thin.iterrows():
                print(f"       {y}: 有効 {int(r['sum'])} / 行 {int(r['size'])}")
            zero_open = int((idx_df["open"].isna() | (idx_df["open"] <= 0)).sum()) \
                if idx_df is not None else 0
            print(f"     指数の始値が NaN か 0 の行: {zero_open:,} "
                  "(yfinance の ^N225 で起こります)")
            print("     → 指数を別ソースで用意するか --no-index で市場成分の控除を"
                  "省いてください")

    return _build_panel_with(symbols, workers, use_earnings, min_prev_atr,
                             idx, idx_df)


def _build_panel_with(symbols, workers, use_earnings, min_prev_atr, idx, idx_df):
    rows: list[pd.DataFrame] = []
    uni_sum: dict = defaultdict(float)
    uni_cnt: dict = defaultdict(int)
    # 日ごとの売買代金 上位 TOPN_MAX 位を保持する (前夜に監視できる枠の検査用)。
    # 全銘柄日を保持するとメモリが持たないので、日ごとに小さなヒープで持つ。
    import heapq
    top_heap: dict = defaultdict(list)
    # 騰落率ランキング用。日ごとに 下落上位RANK_MAX / 上昇上位RANK_MAX を持つ。
    dn_heap: dict = defaultdict(list)   # 最も負のギャップ (max-heap を符号反転で)
    up_heap: dict = defaultdict(list)   # 最も正のギャップ (min-heap)
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
                cand, uni, rkp = res
                if len(uni):
                    g = uni.groupby("date")["o2c"]
                    for dt, v in g.sum().items():
                        uni_sum[dt] += float(v)
                    for dt, v in g.count().items():
                        uni_cnt[dt] += int(v)
                    for dt, tv in zip(uni["date"].to_numpy(),
                                      uni["turnover"].to_numpy()):
                        if not np.isfinite(tv):
                            continue
                        h = top_heap[dt]
                        if len(h) < TOPN_MAX:
                            heapq.heappush(h, float(tv))
                        elif tv > h[0]:
                            heapq.heapreplace(h, float(tv))
                if len(rkp):
                    for dt, gv in zip(rkp["date"].to_numpy(),
                                      rkp["gap"].to_numpy()):
                        h = dn_heap[dt]        # 下落側: 小さいほど上位
                        if len(h) < RANK_MAX:
                            heapq.heappush(h, -float(gv))
                        elif -gv > h[0]:
                            heapq.heapreplace(h, -float(gv))
                        h = up_heap[dt]        # 上昇側: 大きいほど上位
                        if len(h) < RANK_MAX:
                            heapq.heappush(h, float(gv))
                        elif gv > h[0]:
                            heapq.heapreplace(h, float(gv))
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
    # 日ごとの「上位N位の売買代金」を閾値表にする
    global _TOPN_THR, _RANK_THR, _UNI_CNT
    _UNI_CNT = pd.Series({d: uni_cnt[d] for d in uni_cnt}).sort_index()
    _RANK_THR = {"dn": {}, "up": {}}
    for n in RANK_LIST:
        # その日の銘柄数が N 未満なら全部が上位N以内なので、閾値は緩い側に置く。
        # 下落側は gap <= thr で判定するので +大、上昇側は gap >= thr なので -大。
        _RANK_THR["dn"][n] = pd.Series(
            {dt: (-sorted(h, reverse=True)[n - 1] if len(h) >= n else 1e9)
             for dt, h in dn_heap.items()}).sort_index()
        _RANK_THR["up"][n] = pd.Series(
            {dt: (sorted(h, reverse=True)[n - 1] if len(h) >= n else -1e9)
             for dt, h in up_heap.items()}).sort_index()
    _TOPN_THR = {}
    for n in TOPN_LIST:
        _TOPN_THR[n] = pd.Series(
            {dt: (sorted(h, reverse=True)[n - 1] if len(h) >= n else 0.0)
             for dt, h in top_heap.items()}).sort_index()

    yr = panel["date"].dt.year.value_counts().sort_index()
    print("  パネルの年別候補行数 (0 や極端に少ない年があればそこで消えています):")
    print("    " + "  ".join(f"{y}:{c:,}" for y, c in yr.items()))
    if _DROP:
        # 残った行が極端に少ない年について、どの条件が食べたかを出す。
        # ⛔ 「指数の穴」が並ぶ年は、市場成分の控除ができずに消えた年です。
        tot = {y: sum(d.values()) for y, d in _DROP.items()}
        med = float(np.median([d.get("残った", 0) for d in _DROP.values()]) or 1)
        thin = sorted(y for y, d in _DROP.items()
                      if d.get("残った", 0) < med * 0.6)
        if thin:
            print("  年別の除外理由 (残った行が中央値の6割未満の年):")
            for y in thin:
                d = _DROP[y]
                parts = "  ".join(f"{k} {v:,}" for k, v in
                                  sorted(d.items(), key=lambda kv: -kv[1]))
                print(f"    {y}: 全 {tot[y]:,} 行 →  {parts}")
            print("    ⛔ 『指数の穴』が並ぶ年は、市場成分を控除できずに消えた年です。")
            print("       指数を差し替えるか --index-from-universe を使ってください。")
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
                 prev_thr: float | None, gap_thr: float) -> pd.DataFrame:
    """side (+1=買い / -1=空売り) を付与し、シグナル行だけ返す。

    ⚠ `prev_thr = 0.0` は「前日の条件なし」ではありません。
      `prev_z >= 0` を要求するので、**前日の動きがギャップと同符号**である
      ことを課しています。ギャップ事象の約半分が落ちます。
      本当に条件を課さない場合は `prev_thr = None` を渡してください
      (CLI では `--prev-thr none`)。
    """
    p = panel
    col_prev = "prev_ret" if raw else "prev_z"
    col_gap = "gap" if raw else "gap_z"
    if prev_thr is None:
        up = p[col_gap] >= gap_thr
        dn = p[col_gap] <= -gap_thr
    else:
        up = (p[col_prev] >= prev_thr) & (p[col_gap] >= gap_thr)
        dn = (p[col_prev] <= -prev_thr) & (p[col_gap] <= -gap_thr)

    sig = p[up | dn].copy()
    # 上げ側にギャップ → 空売り、下げ側 → 買い
    sig["side"] = np.where(sig["gap_z"] >= 0, -1.0, 1.0)
    return attach_pnl(sig, mkt)


def bounce_slopes(panel: pd.DataFrame, n_bins: int = 10) -> tuple:
    """|ギャップ| の帯域ごとに o2c を gap に回帰した傾きを返す。

    始値は gap の分子であり o2c の分母でもあるため、始値の測定ノイズだけで
    両者は機械的に負の相関を持つ。ただし **そのノイズ由来の傾きは小さい
    ギャップの帯域でしか効かない**。ノイズが分散に占める割合が高いのは
    小さいギャップの領域で、12% のギャップではノイズは誤差の数%にすぎない。

    したがって全体で1本の傾きを推定して大きいギャップに線形外挿すると、
    過剰に控除してしまう。帯域ごとに推定して、その帯域の傾きを使う。

    返り値: (帯域の境界 (n_bins+1 個), 帯域ごとの傾き, 帯域ごとの件数)
    """
    g = panel["gap"].to_numpy(dtype=float)
    y = panel["o2c"].to_numpy(dtype=float)
    ok = np.isfinite(g) & np.isfinite(y)
    g, y = g[ok], y[ok]
    if len(g) < 2000:
        return np.array([0.0, np.inf]), np.array([0.0]), np.array([len(g)])

    mag = np.abs(g)
    qs = np.quantile(mag, np.linspace(0, 1, n_bins + 1))
    qs[0], qs[-1] = 0.0, np.inf
    qs = np.unique(qs)
    slopes, counts = [], []
    for i in range(len(qs) - 1):
        m = (mag >= qs[i]) & (mag < qs[i + 1])
        gx, gy = g[m], y[m]
        if len(gx) < 200 or gx.std() == 0:
            slopes.append(0.0)
        else:
            slopes.append(float(np.cov(gx, gy)[0, 1] / gx.var()))
        counts.append(int(m.sum()))
    return qs, np.array(slopes), np.array(counts)


def noise_slope() -> float:
    """始値の測定ノイズだけで生じる傾きを推定する。

    始値に乗算ノイズ ε があると
        gap  = 真のギャップ + ε
        o2c  = 真の日中変化 - ε
    となり、|ギャップ| が小さい帯域ではノイズが分散を支配するので
    傾きは -1 に近づく。逆に大きいギャップの帯域ではノイズの寄与は
    無視できる。

    したがって **ノイズ由来の成分は、最小の帯域の傾きから推定する**。
    そこが負でなければ、始値ノイズは存在しない。

    シグナル自身の帯域の傾きを引いてしまうと、それは「反転はギャップに
    比例する」という効果そのものを差し引くことになり、比例する本物の
    効果まで消してしまう (2026-08 に修正)。
    """
    _, slopes, counts = _BOUNCE
    if len(slopes) < 3:
        return 0.0
    # 最小の2帯域。件数の少ない帯域は無視する
    cand = [sl for sl, c in zip(slopes[:2], counts[:2]) if c >= 200]
    if not cand:
        return 0.0
    return min(0.0, float(min(cand)))


def attach_pnl(sig: pd.DataFrame, mkt: pd.Series) -> pd.DataFrame:
    sig = sig.copy()
    sig["ret"] = sig["side"] * sig["o2c"]
    sig["mkt"] = sig["date"].map(mkt)
    sig["alpha"] = sig["ret"] - sig["side"] * sig["mkt"].fillna(0.0)
    # 微細構造 (始値ノイズ) で説明できる分を控除した α。
    # 傾きはその行の |ギャップ| が属する帯域のものを使う (線形外挿を避ける)。
    b = noise_slope()
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


# 東証の制限値幅 (基準値段の下限 → 値幅、円)。
# check_price_limit._TSE_LIMIT_TABLE 相当。このリポジトリには無いので自前で持つ。
_TSE_LIMIT_TABLE = [
    (0, 30), (100, 50), (200, 80), (500, 100), (700, 150),
    (1_000, 300), (1_500, 400), (2_000, 500), (3_000, 700),
    (5_000, 1_000), (7_000, 1_500), (10_000, 3_000), (15_000, 4_000),
    (20_000, 5_000), (30_000, 7_000), (50_000, 10_000), (70_000, 15_000),
    (100_000, 30_000), (150_000, 40_000), (200_000, 50_000),
    (300_000, 70_000), (500_000, 100_000), (700_000, 150_000),
    (1_000_000, 300_000),
]


def tse_price_limit(base: float) -> float:
    """基準値段 (前日終値) に対する1日の制限値幅 (円)。

    検算: 前日終値 1,530円 → 1,500〜2,000円未満の帯なので値幅 400円。
    ストップ高は 1,930円。実データで観測した 2767.T / 6480.T の
    「1,930円 / +26.14%」と一致する。

    ⚠ 連続ストップ時の値幅拡大 (制限値幅の4倍化など) は考慮していない。
      拡大が入った日は、ここで「ストップ」と判定されない可能性がある。
    """
    w = _TSE_LIMIT_TABLE[0][1]
    for lo, width in _TSE_LIMIT_TABLE:
        if base >= lo:
            w = width
        else:
            break
    return float(w)


def provenance_fields(script: str) -> dict:
    """記録ファイルに1行ぶん埋め込む出所。

    ⛔ **場所より出所です。** 2026-08-31 に「swingtrade が書いた記録を
    gobot-lss が採点して、たまたま列が合った」形になりました。次は合いません。
    書いた側の commit / ブランチ / スクリプト名 / CWD を記録に残せば、
    採点する側が不一致を検出できます。
    """
    import subprocess

    def _git(*a: str) -> str:
        try:
            return subprocess.run(["git", *a], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            return ""
    commit = _git("rev-parse", "--short", "HEAD") or "no-git"
    if _git("status", "--porcelain"):
        commit += "+dirty"          # ⛔ この記録は再現できません
    return {
        "src_script": script,
        "src_commit": commit,
        "src_branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "no-git",
        "src_cwd": str(Path.cwd()),
        "src_written_at": datetime.now().astimezone().isoformat(
            timespec="seconds"),
    }


def provenance(script: str) -> str:
    """出所を1行で返す。結果を貼るときは必ずこれを添える。

    別セッションの結果と取り違える事故を防ぐため、レポートの先頭で必ず印字する。
    誰が / どのコード @ どのコミット / いつ、が分かる形にする。
    """
    import subprocess
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        h = f"{h}{'+dirty' if dirty else ''}" if h else "no-git"
    except Exception:
        h = "no-git"
    return (f"出所: {script} @ {h}  /  "
            f"実行 {datetime.now().strftime('%Y-%m-%d %H:%M')}")


def _fmt(label: str, st: dict, width: int = 26) -> str:
    if st.get("n", 0) < 10:
        return f"  {label:<{width}} データ不足 (n={st.get('n', 0)})"
    return (f"  {label:<{width}} n={st['n']:>5}  平均={st['mean_bp']:>7.2f}bp  "
            f"t={st['t']:>6.2f}  SR={st['sharpe']:>5.2f}  "
            f"勝日={st['hit']*100:>5.1f}%  累計={st['cum_pct']:>8.1f}%")


# ══════════════════════════════════════════════════════════════════
# レポート
# ══════════════════════════════════════════════════════════════════
def _half_split(daily: pd.Series, w: pd.Series | None = None) -> str:
    """前半/後半の差を SE 付きで検定する。

    3点の単調性は偶然で出る (§16.6.35) ので、必ず差の検定で見る。
    w を渡すと日ごとの取引数で重み付けする。3件の年と47件の年を
    同じ重みで扱うと、標本の薄い年がそのまま結論を動かすため。
    """
    if len(daily) < 40:
        return "  (日数不足)"
    half = daily.index[len(daily) // 2]
    out = []
    for name, ww in (("等重み", None), ("取引数で重み付け", w)):
        if ww is None and name != "等重み":
            continue
        wgt = pd.Series(1.0, index=daily.index) if ww is None else ww.reindex(
            daily.index).fillna(0.0)
        res = []
        for m in (daily.index < half, daily.index >= half):
            x, u = daily[m], wgt[m]
            if len(x) < 6 or u.sum() <= 0:
                return "  (日数不足)"
            mu = float((u * x).sum() / u.sum())
            # 重み付き平均の頑健な標準誤差
            se = float(np.sqrt((u ** 2 * (x - mu) ** 2).sum()) / u.sum())
            res.append((mu, se))
        (m0, s0), (m1, s1) = res
        se = math.sqrt(s0 ** 2 + s1 ** 2)
        t = (m0 - m1) / se if se > 0 else float("nan")
        out.append(f"    {name:<18} 前半 {m0*10000:>7.1f}bp / 後半 {m1*10000:>7.1f}bp"
                   f"   差 {(m0-m1)*10000:>+7.1f}bp (SE {se*10000:.1f}, t={t:.2f})")
    out.append("    |t| < 2 は『直近が弱くない』ではなく"
               " **この標本では分からない** です")
    return "\n".join(out)


def _year_table(run: pd.DataFrame, col: str, cost_bps: float,
                max_names: int | None, panel: pd.DataFrame | None = None,
                drop_partial: bool = True) -> pd.Series:
    """年別の α/t を出し、日次系列を返す。

    ⚠ 発火日数の列を必ず出すこと。3日の年の +200bp と 47日の年の +50bp を
      並べて『昔は良かった』と読むのが最も起こりやすい誤読です。
    """
    dropped_year = None
    if drop_partial:
        # 最終年が途中までなら年別から外す (月が揃っていない年は比較できない)
        last = run["date"].max()
        if last.month < 12:
            dropped_year = int(last.year)
            run = run[run["date"].dt.year < last.year]
            print(f"    ⚠ {last.year} 年は {last:%Y-%m} までで未完なので"
                  " 年別から外しました (別記)")
    print(f"    {'年':<6}{'発火日':>7}{'取引':>7}{'発火率‰':>9}{'日次bp':>9}{'t':>7}"
          f"{'勝率':>7}{'累計%':>9}{'|gap|%':>9}{'ATR%':>7}")
    for y in sorted(run["date"].dt.year.unique()):
        g = run[run["date"].dt.year == y]
        d = to_daily(g, col, cost_bps, max_names)
        if not len(d):
            continue
        mu = float(d.mean())
        sd = float(d.std(ddof=1)) if len(d) > 1 else 0.0
        t = mu / (sd / math.sqrt(len(d))) if sd > 0 and len(d) > 1 else float("nan")
        rate = float("nan")
        if _UNI_CNT is not None and len(_UNI_CNT):
            cnt = float(_UNI_CNT[_UNI_CNT.index.year == y].sum())
            rate = len(g) / cnt * 1000 if cnt else float("nan")
        print(f"    {y:<6}{len(d):>7}{len(g):>7}{rate:>9.3f}{mu*10000:>9.1f}"
              f"{t:>7.2f}{float((d > 0).mean())*100:>6.0f}%{float(d.sum())*100:>9.2f}"
              f"{g['gap'].abs().median()*100:>9.2f}{g['atr'].median()*100:>7.2f}")
    if panel is not None:
        # 発火0の年が、候補行そのものが無い年 (データ欠落) かどうかの切り分け
        fired = set(run["date"].dt.year)
        yrs = sorted(set(panel["date"].dt.year))
        # ⚠ 未完で外した年は「発火0」ではないので混ぜない
        zero = [y for y in yrs if y not in fired and y != dropped_year]
        if zero:
            print("    発火0の年 (候補行 / 閾値までどれくらい近かったか):")
            for y in zero:
                pm = panel["date"].dt.year == y
                n = int(pm.sum())
                if not n or "gap_z" not in panel.columns:
                    print(f"      {y}: 候補行 {n:,}"
                          + ("  ⛔ 候補行ゼロ = データ欠落です" if not n else ""))
                    continue
                gz = panel.loc[pm, "gap_z"]
                cnts = "  ".join(
                    f"|z|>={k}: {int((gz.abs() >= k).sum()):,}"
                    for k in (1.5, 2.0, 2.5, 3.0))
                print(f"      {y}: 候補行 {n:,}  最大|z| {gz.abs().max():.2f}"
                      f"  {cnts}")
            print("      2.5 に件数があって 3.0 でゼロ → 惜しかっただけ。")
            print("      1.5 でもゼロ → その年の行が壊れているか消えています。")
            print("      候補行そのものがゼロ → ⛔ データ欠落。指数の穴を疑うこと")
    return to_daily(run, col, cost_bps, max_names)


def _tail_report(d_all: pd.Series) -> None:
    if len(d_all) < 20:
        print("    日数が足りません")
        return
    tot = float(d_all.sum())
    srt = d_all.sort_values(ascending=False)
    if abs(tot) < abs(float(d_all.abs().sum())) * 0.05:
        print("    ⚠ 総和がほぼゼロなので『寄与%』は発散します。"
              "パーセントではなく bp の絶対値で見てください。")
    for k in (1, 5, 10):
        if len(srt) > k:
            print(f"    上位{k:>2}日の寄与  "
                  f"{float(srt.head(k).sum())/tot*100 if tot else float('nan'):>6.1f}%")
    k10 = max(1, int(len(srt) * 0.10))
    print(f"    上位10%の日 ({k10}日) の寄与  "
          f"{float(srt.head(k10).sum())/tot*100 if tot else float('nan'):>6.1f}%")
    print(f"    日次の中央値  {float(d_all.median())*10000:>7.1f}bp")
    lo, hi = d_all.quantile(0.01), d_all.quantile(0.99)
    trm = d_all[(d_all >= lo) & (d_all <= hi)]
    st_t = stats(trm)
    print(f"    上下1%除去後  {st_t.get('mean_bp', float('nan')):>7.1f}bp"
          f"  t={st_t.get('t', float('nan')):>5.2f}  ({len(trm)}日)")
    print("    ⛔ 上下1%除去は『1ヶ月まるごと』の依存を検出できません。"
          "年別と併せて見ること。")


def nrule_signal(panel: pd.DataFrame, side: str) -> pd.DataFrame:
    """N ルール (固定%・売買代金上位N) をこのエンジンで表現する。

    仕様 (丸めない):
      前夜  前日リターン >= +1.753%  かつ 売買代金20日平均が その日の上位50位以内
      09:00 始値が前日終値の +100bp 以上 → 空売り
      決済  その日の終値。損切りも利確も置かない
    鏡像 (mirror) は符号を全部反転して買い。
    ⚠ 価格帯 1,000〜6,000円 はパネル構築時に適用済み。
    """
    p = panel
    if N_TOPN not in _TOPN_THR:
        raise SystemExit(f"売買代金の上位{N_TOPN}位の閾値表がありません")
    thr = p["date"].map(_TOPN_THR[N_TOPN])
    liq = p["turnover"] >= thr
    if side == "short":
        m = (p["prev_ret"] >= N_PREV_THR) & (p["gap"] >= N_GAP_THR) & liq
        sd = -1
    else:
        m = (p["prev_ret"] <= -N_PREV_THR) & (p["gap"] <= -N_GAP_THR) & liq
        sd = +1
    out = p[m].copy()
    out["side"] = sd
    out["raw"] = sd * out["o2c"]          # 生リターン (始値→終値)
    return out


def nrule_report(panel: pd.DataFrame, mkt: pd.Series, args) -> int:
    """N ルールと鏡像を、§13 と同じ形式で測る。"""
    print(provenance("gap_reversal_daily.py"))
    print(f"\nN ルール検証  前日 {N_PREV_THR*100:.3f}% / ギャップ "
          f"{N_GAP_THR*100:.2f}% / 売買代金 上位{N_TOPN}位 / 引け決済")
    print(f"母集団 {panel['symbol'].nunique()} 銘柄 / "
          f"{panel['date'].min():%Y-%m-%d} 〜 {panel['date'].max():%Y-%m-%d}")
    print(f"コスト 片道 {N_ONEWAY_BPS}bp (呼値1円 / 建値2,257円) → 往復 "
          f"{N_ONEWAY_BPS*2}bp")
    print("⚠ 直近が OOS だから信じられる、とは書きません。正しくは"
          " 『TRAIN では一度も使っていない期間』です。")

    # ── 件数が合わないときに、どの条件で落ちたかを特定する ──────────
    #  ⛔ 件数が参照値と桁で違うなら、成績の一致・不一致は論じられません。
    #    まずこの漏斗を見て、仕様差を特定してから数字を読むこと。
    print("\n■ 発火までの漏斗 (件数が合わないときはここで仕様差を特定する)")
    ndays = panel["date"].nunique()
    thr50 = panel["date"].map(_TOPN_THR[N_TOPN]) if N_TOPN in _TOPN_THR else None
    stages = [("パネルの候補行", pd.Series(True, index=panel.index))]
    if thr50 is not None:
        top = panel["turnover"] >= thr50
        stages.append((f"売買代金 上位{N_TOPN}位以内", top))
        stages.append(("  かつ 前日 |ret| >= 閾値",
                       top & (panel["prev_ret"].abs() >= N_PREV_THR)))
        stages.append(("  かつ ギャップ |gap| >= 閾値",
                       top & (panel["prev_ret"].abs() >= N_PREV_THR)
                       & (panel["gap"].abs() >= N_GAP_THR)))
    for lbl, m in stages:
        k = int(m.sum())
        print(f"    {lbl:<30} {k:>10,} 行  ({k/ndays:>6.2f} 行/営業日)")
    print(f"    営業日 {ndays:,} 日 / 月あたりに直すと "
          f"{stages[-1][1].sum()/ndays*20.7:.0f} 件/月 "
          "(20.7営業日/月)")
    print("    ★ 参照値と月あたり件数が合わなければ、成績は比較できません。")
    print("      上位50位の母集団がどこで数えられているか (株価帯で絞る前か後か)、")
    print("      流動性の下限、ユニバースの広さ — この3つを先に突き合わせること。")
    # 年別の発火件数。指数の穴で年が丸ごと落ちていないかの確認も兼ねる
    if thr50 is not None:
        fin = stages[-1][1]
        by_y = panel.loc[fin, "date"].dt.year.value_counts().sort_index()
        allow = panel["date"].dt.year.value_counts().sort_index()
        print("    年別 (発火 / パネル候補行):")
        print("      " + "  ".join(f"{y}:{by_y.get(y,0)}/{allow[y]:,}"
                                   for y in allow.index))

    sigs = {}
    for name, side in (("N (空売り)", "short"), ("鏡像 (買い)", "long")):
        sg = nrule_signal(panel, side)
        sigs[side] = sg
        print("\n" + "=" * 78)
        print(f"■ {name}   発火 {len(sg):,} 件 / "
              f"{sg['date'].nunique():,} 日")
        if len(sg) < 30:
            print("  件数が足りません")
            continue
        # α (同日ユニバース平均を控除) も併記する。円ベースの参照値とは
        # 単位が違うので、一致を見るのは発火日数と符号・順位のパターン。
        sg["alpha"] = sg["raw"] - sg["date"].map(mkt).fillna(0.0) * sg["side"]
        for lbl, col, cost in (("グロス", "raw", 0.0),
                               (f"往復 {N_ONEWAY_BPS*2}bp 控除後", "raw",
                                N_ONEWAY_BPS * 2),
                               ("α (同日ユニバース控除)", "alpha", 0.0)):
            st = stats(to_daily(sg, col, cost, None))
            print(_fmt(f"  {lbl}", st, width=26))
        print(f"  1件あたり グロス {sg['raw'].mean()*10000:.1f}bp / "
              f"往復控除後 {(sg['raw'].mean()-N_ONEWAY_BPS*2/10000)*10000:.1f}bp")

        print("\n  年別 (発火日数を必ず見ること)")
        d_all = _year_table(sg, "raw", N_ONEWAY_BPS * 2, None, panel=panel)
        print("\n  前半/後半の差の検定")
        print(_half_split(d_all, sg.groupby("date").size()))
        print("\n  裾の依存度")
        _tail_report(d_all)
        last = sg["date"].max()
        if last.month < 12:
            tailrows = sg[sg["date"].dt.year == last.year]
            dt = to_daily(tailrows, "raw", N_ONEWAY_BPS * 2, None)
            if len(dt):
                print(f"\n  別記 {last.year} 年 (未完, {last:%Y-%m} まで): "
                      f"{len(dt)}日 {len(tailrows)}件 "
                      f"{float(dt.mean())*10000:.1f}bp 累計 {float(dt.sum())*100:.2f}%")

    # ── 重なり ────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("■ こちらの買い側 (残差3ATR) と N鏡像 (固定%) の重なり")
    print("  同じものなら足し算になりません (置き換えであって追加ではない)")
    # ⚠ 見出し (§13) と同じ仕様に揃える。--prev-thr を省いたときに
    #   「前日条件なし」で走ると件数が倍近くなり、別の集合を比べることになる。
    pv = 0.0 if getattr(args, "prev_thr", None) is None else args.prev_thr
    gp = 3.0 if getattr(args, "gap_thr", None) is None else args.gap_thr
    print(f"  こちら側の仕様: 前日={'なし' if pv is None else pv} / "
          f"ギャップ={gp} ATR  (§13 の見出しと同じ)")
    mine = apply_signal(panel, mkt, args.raw, pv, gp)
    mine = mine[mine["side"] > 0]
    mir = sigs.get("long")
    if mir is None or not len(mine) or not len(mir):
        print("  片方が空です")
        return 0
    key = lambda d: set(zip(d["date"], d["symbol"]))
    a, b = key(mine), key(mir)
    both = a & b
    print(f"    こちらの買い側 {len(a):,} 件 / N鏡像 {len(b):,} 件 / "
          f"重なり {len(both):,} 件")
    print(f"    こちら側から見た重なり率 {len(both)/len(a)*100:.1f}% / "
          f"N鏡像から見て {len(both)/len(b)*100:.1f}%")
    da, db = set(mine["date"]), set(mir["date"])
    print(f"    発火日   こちら {len(da):,} 日 / N鏡像 {len(db):,} 日 / "
          f"両方 {len(da & db):,} 日")
    for lbl, d, k in (("こちらの買い側", mine, a), ("N鏡像", mir, b)):
        ov = pd.Series([(x, y) in both for x, y in zip(d["date"], d["symbol"])],
                       index=d.index)
        col = "alpha" if "alpha" in d.columns else "raw"
        for sub, m in (("重なった分", ov), ("片方だけ", ~ov)):
            if m.sum():
                print(f"    {lbl:<14}{sub:<10} {int(m.sum()):>6,}件  "
                      f"1件 {d.loc[m, col].mean()*10000:>7.1f}bp")
    print(f"    |ギャップ| 中央値   こちら {mine['gap'].abs().median()*100:.2f}% / "
          f"N鏡像 {mir['gap'].abs().median()*100:.2f}%")
    print("  ★ 重なり率が高ければ同じ現象の別表現です。低ければ別の現象で、")
    print("    そのときだけ足し算の余地があります。")
    print("  ⚠ ギャップの水準が一桁違うなら、そもそも別の事象です。重なり率が")
    print("    低いのは当然で、『別の現象だから足せる』の根拠にはなりません。")
    return 0


def shuffle_test(panel: pd.DataFrame, mkt: pd.Series, args) -> int:
    """⛔ 出口 (o2c) を採否に使っていないことを、実装に依存せず証明する。

    アサーションは「今のコードが読んでいない」ことしか保証しません。
    シャッフルは **選ばれた (date, symbol) の集合が変わらないこと** を
    1回の実行で示すので、条件が増えても効き続けます。

    ⚠ 比べるのは pnl ではなく **選ばれた集合**です。pnl を比べると
      「シャッフルしたので当然変わる」で終わります。
    ⚠ o2c が NaN の行は元から除外されているので、**NaN でない行の中で**
      シャッフルします (でないと偽陽性で検査が外れます)。
    """
    print(provenance("gap_reversal_daily.py"))
    print("\n■ シャッフル検査 — 出口 (o2c) が採否に入っていないか")
    pv = 0.0 if getattr(args, "prev_thr", None) is None else args.prev_thr
    gp = 3.0 if getattr(args, "gap_thr", None) is None else args.gap_thr

    def selected(p: pd.DataFrame) -> set:
        sg = apply_signal(p, mkt, args.raw, pv, gp)
        if args.side == "long":
            sg = sg[sg["side"] > 0]
        elif args.side == "short":
            sg = sg[sg["side"] < 0]
        sel = set(zip(sg["date"], sg["symbol"]))
        if _RANK_THR:                       # §12 の順位フィルタも通す
            n0 = 30 if 30 in RANK_LIST else RANK_LIST[-1]
            dn = sg["date"].map(_RANK_THR["dn"][n0])
            up = sg["date"].map(_RANK_THR["up"][n0])
            inn = pd.Series(np.where(sg["side"] > 0, sg["gap"] <= dn,
                                     sg["gap"] >= up), index=sg.index)
            sel = set(zip(sg.loc[inn, "date"], sg.loc[inn, "symbol"]))
        return sel

    base = selected(panel)
    rng = np.random.default_rng(0)
    ok = True
    for trial in range(3):
        p2 = panel.copy()
        m = p2["o2c"].notna()
        v = p2.loc[m, "o2c"].to_numpy().copy()
        rng.shuffle(v)
        p2.loc[m, "o2c"] = v
        s2 = selected(p2)
        same = s2 == base
        ok = ok and same
        print(f"    試行{trial+1}  選ばれた件数 {len(base):,} → {len(s2):,}"
              f"   差 {len(base ^ s2):,}   {'✅ 一致' if same else '⛔ 変化'}")
    print(f"\n  → {'✅ 出口は採否に入っていません' if ok else '⛔ 出口が採否に入っています'}")
    print("  ⚠ これはパネル構築 **後** の検査です。パネルを作る段階の除外条件")
    print("    (build_symbol_rows の sane) は別で、そちらは件数を印字しています。")
    print("    『データ破損として除外』の内訳に『出口(o2c)で除外』が出ていれば、")
    print("    そこは結果に相関した選別です。")
    return 0 if ok else 1


def report(panel: pd.DataFrame, mkt: pd.Series, args) -> None:
    prev_thr = args.prev_thr   # None なら前日の条件を課さない
    gap_thr = args.gap_thr if args.gap_thr is not None else (
        GAP_PCT if args.raw else GAP_ATR)

    global _BOUNCE
    _BOUNCE = bounce_slopes(panel)

    sig = apply_signal(panel, mkt, args.raw, prev_thr, gap_thr)
    side_lbl = ""
    if getattr(args, "side", "both") != "both":
        want = 1.0 if args.side == "long" else -1.0
        sig = sig[sig["side"] == want]
        side_lbl = ("  [買い側のみ]" if want > 0 else "  [空売り側のみ]")
    mode = "生%" if args.raw else "ATR単位"
    print("\n" + "=" * 78)
    print(provenance("gap_reversal_daily.py"))
    pl = "なし(同符号も要求しない)" if prev_thr is None else (
        f"{prev_thr} (0.0 は『同符号であること』の要求)" if prev_thr == 0.0
        else f"{prev_thr}")
    print(f"ギャップ反転 検証レポート  [{mode}]{side_lbl}  "
          f"前日={pl}  ギャップ={gap_thr}  コスト={args.cost_bps}bp 往復")
    print(f"期間 {panel['date'].min():%Y-%m-%d} 〜 {panel['date'].max():%Y-%m-%d}"
          f"   シグナル {len(sig):,} 件 "
          f"({len(sig)/max(len(mkt),1):.1f} 件/日)")
    print(f"母集団 {panel['symbol'].nunique():,} 銘柄 / {len(mkt):,} 営業日 / "
          f"候補 {len(panel):,} 行  "
          f"(株価 {MIN_PRICE:,.0f}〜{MAX_PRICE:,.0f}円, "
          f"20日平均売買代金 {MIN_TURNOVER/1e8:.0f}億円以上)")
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

    edges, slopes, counts = _BOUNCE
    # ── 指数の効き ────────────────────────────────────────
    #  ⛔ 残差化が弱まると resid_gap が生ギャップに近づき、
    #    (1) 生の騰落率ランキングでのカバー率が上がり
    #    (2) 市場調整の効果が減って α が下がる
    #    という **表と裏** が同時に起きます。カバー率の改善を成果として
    #    報告する前に、この2つが同じ変更から出ていないかを必ず見ること。
    print("\n  指数の効き (残差化がどれだけ効いているか)")
    cg = float(panel["resid_gap"].corr(panel["gap"]))
    rr = float(panel["resid_gap"].std() / panel["gap"].std())
    print(f"    corr(残差ギャップ, 生ギャップ) = {cg:.4f}"
          "   1.0 に近いほど残差化が効いていない")
    print(f"    std比 残差/生               = {rr:.4f}"
          "   1.0 に近いほど市場成分を引けていない")
    print(f"    |指数ギャップ| の中央値        = "
          f"{panel['idx_gap'].abs().median()*100:.3f}%  "
          f"/ 99%点 {panel['idx_gap'].abs().quantile(0.99)*100:.2f}%")
    # 発火が集中する日。暴落日の発火が消えていないかの確認。
    fd = sig.groupby("date").agg(
        n=("symbol", "size"), bp=("alpha", "mean"),
        ig=("idx_gap", "first")).sort_values("n", ascending=False)
    print("    発火が多い日 上位10 (暴落日の発火が消えていないかの確認)")
    print(f"      {'日付':<12}{'発火':>5}{'指数ギャップ%':>14}{'1件bp':>10}")
    for d, r in fd.head(10).iterrows():
        print(f"      {d:%Y-%m-%d}  {int(r['n']):>5}{r['ig']*100:>14.2f}"
              f"{r['bp']*10000:>10.1f}")
    print("    ★ 指数の定義を変えて総発火件数が減り、かつ暴落日の発火が")
    print("      消えているなら、カバー率の改善は『最も稼いでいた取引を")
    print("      取らなくなった』ことの言い換えです。改善ではありません。")

    print("\n  o2c ~ gap の回帰傾き (|ギャップ| の帯域別)")
    print("  始値は gap の分子かつ o2c の分母なので、始値の測定ノイズだけで")
    print("  両者は機械的に負相関します。ただしノイズが効くのは小さいギャップの")
    print("  帯域だけなので、全体で1本の傾きを大きいギャップに外挿すると")
    print("  控除しすぎます。帯域ごとに推定した傾きを各行に当てています。")
    print(f"    {'|ギャップ| 帯域':<22}{'傾き':>10}{'件数':>12}")
    for i in range(len(slopes)):
        lo = edges[i] * 100
        hi = edges[i + 1] * 100
        rng = f"{lo:5.2f}% 〜 {hi:6.2f}%" if np.isfinite(hi) else f"{lo:5.2f}% 以上   "
        print(f"    {rng:<22}{slopes[i]:>+10.4f}{counts[i]:>12,}")
    nb = noise_slope()
    print(f"\n  ノイズ由来と推定した傾き (最小帯域から) = {nb:+.4f}"
          f"  → 控除 {abs(nb) * sig['gap'].abs().mean() * 10000:.1f}bp")
    if nb > -0.005:
        print("  ★ 最小の帯域に負の傾きがありません = 始値の測定ノイズは実質ゼロ。")
        print("    微細構造では説明できないので、この経路での棄却はできません。")
        print("    (日本株の寄り付きは板寄せで単一の約定価格が付くため、これは自然)")
    else:
        print("  ★ 最小の帯域が強く負 = 始値ノイズの兆候。3行目が消えるなら打ち切り。")
    print("  注: シグナル自身の帯域の傾きを引いてはいけません。それは")
    print("      「反転はギャップに比例する」という効果そのものの控除になります。")

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

    # 月単位の寄与。裾1%除去は1トレード単位なので、1ヶ月まるごとの依存は
    # 検出できない。相場イベント (暴落月など) 1つで説明がつく戦略を弾く。
    print("\n  月別の寄与 (単月依存の検査)")
    d_g = to_daily(sig, "alpha", 0.0, args.max_names)      # グロスで見る
    mo = d_g.groupby(d_g.index.to_period("M")).sum()
    tot = float(mo.sum())
    if tot > 0 and len(mo) >= 12:
        top = mo.sort_values(ascending=False)
        print(f"    月数 {len(mo)}  /  プラスの月 {int((mo > 0).sum())} "
              f"({(mo > 0).mean()*100:.0f}%)")
        for i, (m, v) in enumerate(top.head(3).items(), 1):
            print(f"    寄与 第{i}位  {m}  {v*100:>7.1f}%  "
                  f"(グロス総和の {v/tot*100:>5.1f}%)")
        for k, lbl in ((1, "最大の1ヶ月"), (3, "上位3ヶ月")):
            drop = set(top.head(k).index)
            sub = sig[~sig["date"].dt.to_period("M").isin(drop)]
            st = stats(to_daily(sub, "alpha", args.cost_bps, args.max_names))
            print(_fmt(f"    {lbl}を除外", st, width=24))
        share1 = float(top.iloc[0]) / tot * 100
        if share1 > 40:
            print(f"    ⚠ 最大の1ヶ月がグロス総和の {share1:.0f}%。"
                  f"単月依存です。")
    else:
        print("    (月数が少ないか総和が非正のため省略)")

    # ── §3c 期間3分割 (サバイバーシップの検査) ───────────────────
    print("\n  期間分割 (サバイバーシップの検査)")
    print("  買い側は『下げたものを買う』ので、下げ続けて上場廃止になった銘柄が")
    print("  抜けている影響を最も強く受けます。古い期間ほど強く単調に減衰するなら")
    print("  サバイバーシップが効いています。フラットならバイアスは小さい。")
    bnds = [pd.Timestamp(x) for x in args.split_dates]
    edges = [sig["date"].min()] + bnds + [sig["date"].max() + pd.Timedelta(days=1)]
    print(f"    {'期間':<22}{'件数':>7}{'グロス':>10}{'±SE':>8}"
          f"{'日次bp':>9}{'日次t':>8}")
    segs = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        g = sig[(sig["date"] >= lo) & (sig["date"] < hi)]
        if len(g) < 10:
            print(f"    {lo:%Y-%m}〜{hi:%Y-%m}     件数不足 ({len(g)})")
            continue
        a = g["alpha"] * 10000
        gross, se = float(a.mean()), float(a.std(ddof=1) / math.sqrt(len(a)))
        st = stats(to_daily(g, "alpha", args.cost_bps, args.max_names))
        print(f"    {lo:%Y-%m}〜{hi:%Y-%m}      {len(g):>6,}"
              f"{gross:>10.1f}{se:>8.1f}"
              f"{st.get('mean_bp', float('nan')):>9.1f}"
              f"{st.get('t', float('nan')):>8.2f}")
        segs.append((gross, se, len(a), st.get("mean_bp", float("nan"))))

    if len(segs) >= 2:
        # 最古と最新の差の検定。単調性だけを見ると 3 点なら 1/6 の確率で
        # 偶然そう並ぶので、誤差を伴わない「単調減衰」の判定には意味がない。
        (g0, s0, n0, d0), (g1, s1, n1, d1) = segs[0], segs[-1]
        se_d = math.sqrt(s0 ** 2 + s1 ** 2)
        t_d = (g0 - g1) / se_d if se_d > 0 else 0.0
        print(f"\n    最古 − 最新 = {g0 - g1:+.1f}bp  (SE {se_d:.1f}, t = {t_d:.2f})")
        mono_gross = all(segs[i][0] > segs[i + 1][0] for i in range(len(segs) - 1))
        mono_daily = all(segs[i][3] > segs[i + 1][3] for i in range(len(segs) - 1))
        if abs(t_d) < 2.0:
            print("    → 差は誤差の範囲です。**この標本ではサバイバーシップを")
            print("       検出も棄却もできません** (検出力不足)。単調に見えても、")
            print(f"       {len(segs)} 点が偶然そう並ぶ確率は "
                  f"{1/math.factorial(len(segs))*100:.0f}% あります。")
        elif t_d > 0:
            print("    ⚠ 最古が最新より有意に強い。サバイバーシップの疑いがあります。")
        else:
            print("    → 最新の方が強く、サバイバーシップとは逆向きです。")
        print(f"    (参考: グロスは{'単調減衰' if mono_gross else '単調ではない'} / "
              f"日次bpは{'単調減衰' if mono_daily else '単調ではない'})")

    # ── §4 ギャップ幅の単調性 ─────────────────────────────────────
    print("\n【4】ギャップ幅(ATR単位)の分位別 α  ← 単調に増えなければノイズ")
    if prev_thr is None:
        cand = panel
    else:
        cand = (panel[panel["prev_z"].abs() >= prev_thr] if not args.raw
                else panel[panel["prev_ret"].abs() >= prev_thr])
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
    if prev_thr is None:
        pl = panel.iloc[0:0]
    elif args.raw:
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
        # トレード単位と日次で符号が食い違っていないかを検査する。
        # 食い違うなら、その方向の損益は特定の日への集中で決まっている。
        tr = g["alpha"] - args.cost_bps / 10000.0
        t_tr = (float(tr.mean() / (tr.std(ddof=1) / math.sqrt(len(tr))))
                if tr.std(ddof=1) > 0 else 0.0)
        print(f"      トレード単位 {tr.mean()*10000:>7.2f}bp (t={t_tr:>5.2f})  /  "
              f"日次 {st.get('mean_bp', 0):>7.2f}bp (t={st.get('t', 0):>5.2f})")
        print(f"      グロス {g['alpha'].mean()*10000:>6.1f}bp/件  "
              f"{len(g):,}件  平均ギャップ {g['gap'].abs().mean()*100:.2f}%")
        if tr.mean() * st.get("mean_bp", 0) < 0:
            print("      ⚠ トレード単位と日次で符号が逆転しています。"
                  "特定の日への集中で決まっています。")
    print("  → 買い側だけで成立するなら、空売りの制度的制約を回避できます。")
    print("     空売り側にしか無いなら、実弾の前に証券会社への照会が必須です。")

    # ── §10 決済できない恐れ (ストップ張り付き) ──────────────────
    print("\n【10】引けで決済できない恐れ (値幅制限)")
    print("  買い側はストップ安で売れない、空売り側はストップ高で買い戻せない。")
    print("  終値と前日終値から、その日がストップ値で引けたかを判定します。")
    px_prev = sig["open"] / (1.0 + sig["gap"])
    px_close = sig["open"] * (1.0 + sig["o2c"])
    lim = px_prev.map(tse_price_limit)
    up, dn = px_prev + lim, px_prev - lim
    tol = 0.01
    at_up = (px_close - up).abs() <= tol
    at_dn = (px_close - dn).abs() <= tol
    yrs = max((sig["date"].max() - sig["date"].min()).days / 365.25, 1e-9)

    rows = [
        ("買い側 (ストップ安で売れない)", sig["side"] > 0, at_dn),
        ("空売り側 (ストップ高で買い戻せない)", sig["side"] < 0, at_up),
    ]
    bad = pd.Series(False, index=sig.index)
    for name, side_m, hit_m in rows:
        n_side = int(side_m.sum())
        n_hit = int((side_m & hit_m).sum())
        bad |= side_m & hit_m
        per_month = n_hit / (yrs * 12)
        print(f"    {name:<34} {n_hit:>4} / {n_side:,} 件 "
              f"({n_hit/max(n_side,1)*100:>5.2f}%)  月 {per_month:.2f} 件")
    print(f"  参考: 始値がストップ値と一致 (建てられない恐れ) "
          f"{int(((sig['open'] - up).abs() <= tol).sum() + ((sig['open'] - dn).abs() <= tol).sum()):>4} 件")
    print("  ⚠ 終値がストップ値と一致でも板寄せは成立しているので、比例配分で")
    print("    決済できた可能性はあります。これは『決済できなかった恐れの上限』です。")
    print("  ⚠ 連続ストップ時の値幅拡大は考慮していないため、過小評価の側に振れます。")
    if bad.any():
        st_ok = stats(to_daily(sig[~bad], "alpha", args.cost_bps, args.max_names))
        st_bad = stats(to_daily(sig[bad], "alpha", args.cost_bps, args.max_names))
        print(_fmt("  該当を除いた成績", st_ok, width=26))
        print(_fmt("  該当のみの成績", st_bad, width=26))
        print(f"  ⛔ 『除外すると良くなる』は良い知らせではありません。"
              f"該当 {int(bad.sum())} 件は")
        print("     既に大負けしている日で、**どの日がそうなるかは事前に分かりません**。")
        print("     除外は運用できない操作です。意味はむしろ逆で、この頻度で")
        print("     『引けで決済できないかもしれない』場面が来るということです。")
        print("     決済できなければ翌日に持ち越して損失が拡大しうる (未モデル化)。")

    # ── §11 前夜の監視枠でどれだけ拾えるか ───────────────────────
    print("\n【11】前夜の売買代金 上位N位に入っていた割合 (監視枠の検査)")
    print("  どの銘柄が翌朝ギャップするかは前夜には分かりません。板の購読上限は")
    print("  50銘柄、前夜に寄指を置く方式なら予算で決まります (建玉30万・資金")
    print("  400万で13件)。件数だけでなく **利益の何%を拾えるか** も見ます。")
    if not _TOPN_THR:
        print("  (閾値表が未計算です)")
    else:
        tot_bp = float((sig["alpha"] * 10000).sum())
        print(f"    {'N':>5}{'カバー件数':>12}{'件数%':>9}"
              f"{'利益%':>9}{'1件bp':>9}{'漏れの1件bp':>13}")
        for n in TOPN_LIST:
            thr = sig["date"].map(_TOPN_THR[n])
            inn = sig["turnover"] >= thr
            k = int(inn.sum())
            if k == 0:
                print(f"    {n:>5}{0:>12}{0.0:>9.1f}{0.0:>9.1f}")
                continue
            bp_in = float((sig.loc[inn, "alpha"] * 10000).sum())
            m_in = float(sig.loc[inn, "alpha"].mean() * 10000)
            m_out = (float(sig.loc[~inn, "alpha"].mean() * 10000)
                     if (~inn).any() else float("nan"))
            print(f"    {n:>5}{k:>12,}{k/len(sig)*100:>9.1f}"
                  f"{(bp_in/tot_bp*100 if tot_bp else float('nan')):>9.1f}"
                  f"{m_in:>9.1f}{m_out:>13.1f}")
        print("  『件数%』と『利益%』が乖離するなら、売買代金の大きい銘柄ほど")
        print("  ギャップが小さいということです。半分拾っても利益は3割、が起こります。")

    # headroom (トリガーと値幅制限の余裕) の5分位。除外する前に測る。
    head = (lim - sig["gap"].abs() * px_prev) / px_prev * 100
    try:
        hq = pd.qcut(head, 5, duplicates="drop")
        print("\n  値幅制限までの余裕 (headroom) 別  ← 除外の前に測る")
        print(f"    {'headroom%':<22}{'件数':>7}{'グロスbp':>10}"
              f"{'決済できない%':>14}")
        for b, g in sig.assign(_h=head, _bad=bad).groupby(hq, observed=True):
            print(f"    {str(b):<22}{len(g):>7,}"
                  f"{g['alpha'].mean()*10000:>10.1f}"
                  f"{g['_bad'].mean()*100:>14.1f}")
        print("  headroom が小さいほど決済できない割合が高いなら除外の根拠。")
        print("  ただし bp も高いならトレードオフです。単純除外は損かもしれません。")
    except Exception as e:
        print(f"\n  headroom 別の集計に失敗: {e}")

    # 決済できなかった建玉を翌営業日の始値まで持ち越した場合。
    # 買い側でストップ安引け = その建玉を翌日に持ち越すだけで、一般信用の
    # 強制決済とは事情が違う。除外ではなく値付けをする。
    # ⛔ next_open は先読みなので、持ち越しの評価にだけ使う列です。
    if "next_open" in sig.columns and bool(bad.any()):
        b = sig[bad]
        ok2 = b["next_open"].notna() & (b["next_open"] > 0)
        if int(ok2.sum()) >= 5:
            bb = b[ok2]
            r_hold = bb["side"] * (bb["next_open"] / bb["open"] - 1.0)
            d = (r_hold - bb["ret"]) * 10000
            print(f"\n  持ち越しの評価 (決済できなかった {int(ok2.sum())} 件を"
                  f"翌営業日の始値まで持った場合)")
            print(f"    引けで決済できた前提 {bb['ret'].mean()*10000:>8.1f}bp")
            print(f"    翌日始値まで持ち越し {r_hold.mean()*10000:>8.1f}bp"
                  f"   差 {d.mean():>+8.1f}bp (中央 {d.median():+.1f})")
            print(f"    悪化した件数 {int((d < 0).sum())} / {len(d)} "
                  f"({(d < 0).mean()*100:.0f}%)")
            if "next_close" in bb.columns:
                c_d = bb["open"] * (1.0 + bb["o2c"])          # D の終値
                dn1 = c_d - c_d.map(tse_price_limit)          # D+1 のストップ安
                n2 = int(((bb["next_close"] - dn1).abs() <= 0.01).sum())
                print(f"    D+2 も張り付き (D+1 もストップ安引け): "
                      f"{n2} / {len(bb)} 件")
                print(f"      → 上の {r_hold.mean()*10000:.1f}bp は下限です"
                      if n2
                      else "      → ゼロ。D+1 の始値で決済できた前提で妥当です")

            sig["alpha_hold"] = sig["alpha"]
            sig.loc[bb.index, "alpha_hold"] = (sig.loc[bb.index, "alpha"]
                                               + (r_hold - bb["ret"]))
            print(_fmt("    持ち越しを織り込んだ全体",
                       stats(to_daily(sig, "alpha_hold", args.cost_bps,
                                      args.max_names)), width=24))
            print("    ⛔ next_open は先読みです。持ち越しの評価にだけ使います。")
        else:
            print(f"\n  持ち越しの評価: 該当が {int(ok2.sum())} 件で不足")

    # ── §12 生の騰落率ランキングで拾えるか ───────────────────────
    print("\n【12】生の騰落率ランキング 上位N位に入っていた割合")
    print("  kabu の /ranking は銘柄登録不要で 値上がり率/値下がり率 の"
          "**上位50件** を")
    print("  返します。ただし並びは **生の騰落率** で、こちらの条件は")
    print("  |残差ギャップ| >= 3×ATR20 です。低ボラ銘柄は小さいギャップでも発火")
    print("  するので、生の騰落率では上位に入りません。その食い違いを測ります。")
    if not _RANK_THR:
        print("  (閾値表が未計算です)")
    else:
        tot_bp = float((sig["alpha"] * 10000).sum())
        print(f"    {'N':>5}{'カバー件数':>12}{'件数%':>9}{'利益%':>9}"
              f"{'1件bp':>9}{'漏れの1件bp':>13}"
              f"{'日次bp':>10}{'t':>7}{'持越後bp':>10}{'t':>7}")
        for n in RANK_LIST:
            dn_thr = sig["date"].map(_RANK_THR["dn"][n])
            up_thr = sig["date"].map(_RANK_THR["up"][n])
            inn = np.where(sig["side"] > 0,
                           sig["gap"] <= dn_thr, sig["gap"] >= up_thr)
            inn = pd.Series(inn, index=sig.index)
            k = int(inn.sum())
            if k == 0:
                print(f"    {n:>5}{0:>12}{0.0:>9.1f}{0.0:>9.1f}")
                continue
            bp_in = float((sig.loc[inn, "alpha"] * 10000).sum())
            m_in = float(sig.loc[inn, "alpha"].mean() * 10000)
            m_out = (float(sig.loc[~inn, "alpha"].mean() * 10000)
                     if (~inn).any() else float("nan"))
            st_in = stats(to_daily(sig[inn], "alpha", args.cost_bps,
                                   args.max_names))
            hcol = "alpha_hold" if "alpha_hold" in sig.columns else "alpha"
            st_h = stats(to_daily(sig[inn], hcol, args.cost_bps, args.max_names))
            print(f"    {n:>5}{k:>12,}{k/len(sig)*100:>9.1f}"
                  f"{(bp_in/tot_bp*100 if tot_bp else float('nan')):>9.1f}"
                  f"{m_in:>9.1f}{m_out:>13.1f}"
                  f"{st_in.get('mean_bp', float('nan')):>10.1f}"
                  f"{st_in.get('t', float('nan')):>7.2f}"
                  f"{st_h.get('mean_bp', float('nan')):>10.1f}"
                  f"{st_h.get('t', float('nan')):>7.2f}")
        print("\n  ★ 判定に使うのは右端の2列 (持ち越し込み) です。")
        print("     t >= 3.5  → 暴落日を捨てたまま成立。執行の検証へ")
        print("     t 3.0-3.5 → 成立するが余裕なし。暴落日を拾えるかが効く")
        print("     t <  3.0  → 上位50では成立しない。閾値の設計から見直し")

        # 漏れる帯の性質。生の騰落率で並べたときに落ちるのは
        #   (a) 市場全体が暴落した日 (数百銘柄が大きく下げるので上位30に入らない)
        #   (b) 低ボラ銘柄 (小さいギャップでも 3ATR に達する)
        # のどちらか。両者は対策が全く違うので分けて見る。
        n0 = 30 if 30 in RANK_LIST else RANK_LIST[-1]
        dn_thr = sig["date"].map(_RANK_THR["dn"][n0])
        up_thr = sig["date"].map(_RANK_THR["up"][n0])
        inn = pd.Series(np.where(sig["side"] > 0, sig["gap"] <= dn_thr,
                                 sig["gap"] >= up_thr), index=sig.index)
        per_day = sig.groupby("date")["symbol"].transform("size")
        prof = sig.assign(_in=inn, _n=per_day)
        print(f"\n  N={n0} で漏れる帯の性質 (対策が違うので分けて見る)")
        print(f"    {'':<10}{'件数':>7}{'1件bp':>9}{'|指数ギャップ|%':>16}"
              f"{'β':>7}{'ATR%':>8}{'|ギャップ|%':>12}{'同日の発火数':>14}")
        for lbl, m in (("拾える", prof["_in"]), ("漏れる", ~prof["_in"])):
            g = prof[m]
            if not len(g):
                continue
            print(f"    {lbl:<10}{len(g):>7,}{g['alpha'].mean()*10000:>9.1f}"
                  f"{g['idx_gap'].abs().mean()*100:>16.2f}"
                  f"{g['beta'].mean():>7.2f}"
                  f"{g['atr'].mean()*100:>8.2f}"
                  f"{g['gap'].abs().mean()*100:>12.2f}"
                  f"{g['_n'].mean():>14.1f}")
        print("    ★ 暴落日は生ギャップ順位の上位が体系的に『発火しない銘柄』で")
        print("      埋まります。指数 -6.5% / ATR 3-4% の日なら、高β(1.5)は -15%で")
        print("      寄っても残差 -5.25% で発火せず順位は上位、低β(0.5)は -13%で")
        print("      残差 -9.75% で発火するのに順位は50位以下。順位と発火が逆相関。")
        print("    → **漏れた側の β が拾えた側より低ければ、この機構で確定**です。")
        print("    指数ギャップが大きい / 同日の発火数が多い → 暴落日で漏れている")
        print("      → 指数が大きく動いた日は ranking が使えないと分かるので、")
        print("        その日だけ別経路 (全銘柄スキャン等) を用意すれば足ります")
        print("    ATR が小さい → 低ボラ銘柄が漏れている")
        print("      → 生の騰落率では原理的に拾えないので、閾値の設計から見直しが要ります")

        # 漏れが特定の日に集中しているか
        miss_by_day = prof[~prof["_in"]].groupby("date").size().sort_values(
            ascending=False)
        if len(miss_by_day):
            tot_miss = int(miss_by_day.sum())
            top5 = int(miss_by_day.head(5).sum())
            print(f"\n    漏れ {tot_miss} 件のうち上位5日で {top5} 件 "
                  f"({top5/tot_miss*100:.0f}%)")
            for d, k in miss_by_day.head(5).items():
                ig = float(sig.loc[sig["date"] == d, "idx_gap"].iloc[0]) * 100
                print(f"      {d:%Y-%m-%d}  {k:>3}件  指数ギャップ {ig:+.2f}%")

        print("  判定: N=30 で 95%以上 → ranking で足りる (事前選択の問題が消える)")
        print("        70〜95% → 漏れる帯の性質を見る / 70%未満 → 適時開示へ")
        print("  ⚠ 母集団はこのキャッシュの銘柄のみです。実際の /ranking は")
        print("    株価帯で絞らない全プライム銘柄が競合するので、実運用の順位は")
        print("    ここより悪くなります (低位株ほど%で大きく動くため)。")

    # ------------------------------------------------------------------
    # 【13】順位フィルタ後・持越後の 年別 / 裾
    #  §3 と §8 は「全シグナル・持ち越し無視」で測っていました。実際に運用
    #  できるのは §12 の上位N に入り、かつ引けで決済できなかった日は翌朝まで
    #  持ち越した集合だけです。判定に使う数字はこちらに揃えます。
    # ------------------------------------------------------------------
    if _RANK_THR:
        n0 = 30 if 30 in RANK_LIST else RANK_LIST[-1]
        dn_thr = sig["date"].map(_RANK_THR["dn"][n0])
        up_thr = sig["date"].map(_RANK_THR["up"][n0])
        inn = pd.Series(np.where(sig["side"] > 0, sig["gap"] <= dn_thr,
                                 sig["gap"] >= up_thr), index=sig.index)
        hcol = "alpha_hold" if "alpha_hold" in sig.columns else "alpha"
        run = sig[inn]
        print(f"\n【13】順位フィルタ後 (N={n0}) ・持ち越し後の 年別と裾")
        if hcol == "alpha":
            print("  ⚠ 持ち越し列がありません (§10 で該当が無かったか未実行)。"
                  "持ち越し無視の数字と一致します。")
        st_all = stats(to_daily(run, hcol, args.cost_bps, args.max_names))
        print(f"  全体  {st_all.get('n', 0):>5}日  "
              f"{st_all.get('mean_bp', float('nan')):>7.2f}bp  "
              f"t={st_all.get('t', float('nan')):>5.2f}")
        # ⛔ 両側を混ぜた見出しは解釈できません。片側のエッジがもう片側の
        #   2〜4倍あることがあり、件数の多い弱い側に引きずられます。
        #   判定に使うのは実際に運用する側の行です。
        if run["side"].nunique() > 1:
            for lbl, m in (("  うち 買い側", run["side"] > 0),
                           ("  うち 空売り側", run["side"] < 0)):
                g = run[m]
                st = stats(to_daily(g, hcol, args.cost_bps, args.max_names))
                print(f"{lbl}  {st.get('n', 0):>5}日  {len(g):>5}件  "
                      f"{st.get('mean_bp', float('nan')):>7.2f}bp  "
                      f"t={st.get('t', float('nan')):>5.2f}")
            print("  ⛔ 上の『全体』は両側の混合です。片側だけを運用するなら"
                  "その行を見てください。")
            print("     件数の多い側が弱いと、全体は弱い側に引きずられます。")

        print("\n  年別 (この行が判定の対象)")
        print("  ⚠ 発火日数の列を必ず見ること。3日の年の +200bp と 47日の年の")
        print("    +50bp を並べて『昔は良かった』と読むのが最も起こりやすい誤読です。")
        print("    発火率‰ = 取引数 / その年のユニバース銘柄日数 × 1000。")
        print("    これが横ばいなら発火数の増加は母集団の成長で説明できます。")
        d_all = _year_table(run, hcol, args.cost_bps, args.max_names, panel=panel)

        # 方向別の年別。★ 直近で弱いのが『この戦略に固有の劣化』なのか
        #   『その年は買い側が弱かった』という市場の性質なのかを切り分ける。
        #   両側を同じエンジンで出さないとこの区別はできません。
        if run["side"].nunique() > 1:
            for lbl, m in (("買い側", run["side"] > 0),
                           ("空売り側", run["side"] < 0)):
                g = run[m]
                if len(g) < 30:
                    print(f"\n  【方向別】{lbl}: {len(g)} 件で不足")
                    continue
                print(f"\n  【方向別】{lbl} の年別 "
                      f"({len(g)} 件)")
                _year_table(g, hcol, args.cost_bps, args.max_names)
            print("    ★ 片側だけが直近で落ちているなら、それはその方向の"
                  "市場の性質です。")
            print("      両側とも落ちているなら、この戦略に固有の劣化を疑うこと。")
        else:
            sd = "買い側" if float(run["side"].iloc[0]) > 0 else "空売り側"
            print(f"\n  ⚠ {sd} だけで回しています。直近の強弱がこの戦略に固有か、"
                  "その方向の")
            print("    市場の性質かは、--side both で両側を出さないと切り分けられません。")

        print("\n  前半/後半の差の検定")
        print(_half_split(d_all, run.groupby("date").size()))

        print("\n  裾の依存度 (順位フィルタ後・持ち越し後)")
        _tail_report(d_all)

        # 計画値は標本の厚い近年を使う。全期間平均は薄い年に引っ張られる。
        # ⛔ 両側混在ならここも方向別に出す (混合の近年水準は意味を持たない)。
        if run["side"].nunique() > 1:
            y2 = run["date"].dt.year
            c2 = int(y2.max()) - 3
            print("    方向別の近年水準:")
            for lbl, ms in (("買い側", run["side"] > 0),
                            ("空売り側", run["side"] < 0)):
                for tag, mm in ((f"〜{c2-1}", y2 < c2), (f"{c2}〜", y2 >= c2)):
                    d = to_daily(run[ms & mm], hcol, args.cost_bps,
                                 args.max_names)
                    if len(d) >= 20:
                        print(f"      {lbl:<8}{tag:<8} {len(d):>4}日 "
                              f"{float(d.mean())*10000:>7.2f}bp")
        # ⛔ 近年の水準は **コスト控除後** です。エッジ/コスト比は
        #   グロスで測るので、期間ごとに両方出さないと基準と突き合わせられません。
        yr = run["date"].dt.year
        cut = int(yr.max()) - 3
        print(f"    {'期間':<10}{'日数':>6}{'件数':>7}{'net bp':>9}"
              f"{'1件グロスbp':>13}{'エッジ/コスト':>13}")
        for lbl, m in ((f"〜{cut-1}", yr < cut), (f"{cut}〜", yr >= cut)):
            g = run[m]
            d = to_daily(g, hcol, args.cost_bps, args.max_names)
            if len(d) < 20:
                continue
            gross = float(g[hcol].mean()) * 10000
            ratio = gross / args.cost_bps if args.cost_bps else float("inf")
            print(f"    {lbl:<10}{len(d):>6}{len(g):>7}"
                  f"{float(d.mean())*10000:>9.2f}{gross:>13.1f}{ratio:>13.2f}")
        print("    ★ 計画値には全期間平均ではなく、標本の厚い近年の水準を"
              "使ってください (保守側)。")
        print("    ⛔ エッジ/コスト比は **近年の行** で 3.0 を満たしているか"
              "を見ること。")
        print("       全期間で 4 を超えていても、近年が 2 なら実運用は近年の側です。")
        # 未完の年は年別表から外れて見えなくなるので別記する
        last = sig["date"].max()
        if last.month < 12:
            py = int(last.year)
            tailrows = sig[inn & (sig["date"].dt.year == py)]
            if len(tailrows):
                dt_ = to_daily(tailrows, hcol, args.cost_bps, args.max_names)
                if len(dt_):
                    print(f"\n    別記 {py} 年 (未完, {last:%Y-%m} まで): "
                          f"{len(dt_)}日 {len(tailrows)}件 "
                          f"{float(dt_.mean())*10000:.2f}bp 累計 "
                          f"{float(dt_.sum())*100:.2f}%")

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
    global _BOUNCE
    _BOUNCE = bounce_slopes(panel)

    def _parse(spec: str, default: list[float]) -> list[float]:
        if not spec:
            return default
        return [float(x) for x in spec.replace(",", " ").split()]

    prevs: list = [None] + _parse(args.grid_prev, [0.0, 0.5, 1.0, 1.25, 1.5, 2.0, 2.5])
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
            lbl = "なし" if pv is None else f"{pv:.2f}"
            print(f"   {lbl:>10}" + row)

    print("\n" + "=" * 78)
    print(provenance("gap_reversal_daily.py --grid"))
    print(f"閾値グリッド  行 = 前日の動き (ATR単位) / 列 = ギャップ (ATR単位)")
    print("  前日『なし』  = 前日の動きに一切条件を課さない")
    print("  前日 0.00     = 前日の動きがギャップと同符号であることを要求")
    print("                  (『条件なし』ではない。ギャップ事象の約半分が落ちる)")
    print("=" * 78)
    _table(f"[1] 日次 t (コスト {args.cost_bps}bp 控除後)", lambda c: c[0], ".2f")
    _table("[2] グロス α (1トレードあたり bp、コスト控除前)", lambda c: c[1], ".0f")
    _table("[3] シグナル件数", lambda c: c[2], "d")

    print("\n  読み方:")
    print("   - t の高い区画が『面』で広がっていれば本物。1区画だけ高く周囲が")
    print("     低いなら、その閾値はデータを見て選んだ偶然の可能性が高い。")
    print("   - 『なし』と 0.00 の差 = 同符号を要求することの効果。")
    print("   - 件数が3桁を切る区画は、t の値によらず信頼できない。")
    print("   - 端の列で単調増加が続いているなら、探索範囲が狭い。--grid-gap で伸ばす。")
    if cells:
        best = max(cells.items(), key=lambda kv: kv[1][0])
        (bp, bg), (bt, ba, bn) = best
        bps = "none" if bp is None else f"{bp}"
        print(f"\n  最良 t: 前日={bps} / ギャップ={bg} → "
              f"t={bt:.2f} / グロス={ba:.0f}bp / {bn}件")
        print(f"  そのまま実行するには: --prev-thr {bps} --gap-thr {bg}")
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
            "open": 2000.0, "is_earn": -1,
            "idx_gap": idx["idx_gap"].to_numpy(), "beta": 1.0,
            # 翌日は独立なランダム。持ち越し評価 (§10) の配管を通すため
            "next_open": 2000.0 * (1.0 + o2c) * (1.0 + rng.normal(0, 0.008, n)),
            "next_close": 2000.0 * (1.0 + o2c) * (1.0 + rng.normal(0, 0.015, n)),
        })
        keep = (np.abs(df["prev_z"]) >= 0.9) | (np.abs(df["gap_z"]) >= 0.3)
        rows.append(df[keep])
        for dt, v in zip(dates, o2c):
            uni_s[dt] += float(v); uni_c[dt] += 1

    panel = pd.concat(rows, ignore_index=True).sort_values(["date", "symbol"])
    mkt = pd.Series({d: uni_s[d] / uni_c[d] for d in uni_c}).sort_index()

    # 順位の閾値表 (§11-§13 を合成データでも通すため)
    global _RANK_THR, _TOPN_THR, _UNI_CNT
    _UNI_CNT = pd.Series({d: uni_c[d] for d in uni_c}).sort_index()
    _RANK_THR = {"dn": {}, "up": {}}
    for nn in RANK_LIST:
        _RANK_THR["dn"][nn] = panel.groupby("date")["gap"].apply(
            lambda g, nn=nn: (g.nsmallest(nn).iloc[-1] if len(g) >= nn else 1e9))
        _RANK_THR["up"][nn] = panel.groupby("date")["gap"].apply(
            lambda g, nn=nn: (g.nlargest(nn).iloc[-1] if len(g) >= nn else -1e9))
    _TOPN_THR = {nn: panel.groupby("date")["turnover"].apply(
        lambda g, nn=nn: (g.nlargest(nn).iloc[-1] if len(g) >= nn else 0.0))
        for nn in TOPN_LIST}

    args = argparse.Namespace(raw=False, prev_thr=1.0, gap_thr=0.5, cost_bps=0.0,
                              max_names=13, capital=4_000_000, side="both",
                              split_dates=["2019-01-01", "2022-01-01"])
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
    ap.add_argument("--prev-thr", dest="prev_thr",
                    help="前日の動きの閾値 (ATR単位)。'none' で条件を課さない。"
                         "0.0 は『同符号であること』を要求する点に注意")
    ap.add_argument("--gap-thr", dest="gap_thr", type=float)
    ap.add_argument("--split-dates", dest="split_dates", nargs="*",
                    default=["2013-01-01", "2020-01-01"],
                    help="期間分割の境界 (既定 2007-2012 / 2013-2019 / 2020-)")
    ap.add_argument("--side", choices=["both", "long", "short"], default="both",
                    help="買い側(long)/空売り側(short)だけに絞って全検定を通す")
    ap.add_argument("--cost-bps", dest="cost_bps", type=float, default=COST_BPS)
    ap.add_argument("--max-names", dest="max_names", type=int, default=13,
                    help="1日に建てられる最大銘柄数 (資金制約)")
    ap.add_argument("--capital", type=float, default=4_000_000)
    ap.add_argument("--earnings", action="store_true", help="決算日を取得して分離")
    ap.add_argument("--csv", help="候補パネルを CSV に書き出す")
    ap.add_argument("--index-from-universe", dest="index_from_universe",
                    action="store_true",
                    help="市場ファクターをユニバース自身から等加重で作る。"
                         "指数の穴で年が丸ごと消えるのを防ぐ")
    ap.add_argument("--shuffle-test", dest="shuffle_test", action="store_true",
                    help="出口 (o2c) が採否に入っていないことをシャッフルで検査")
    ap.add_argument("--nrule", action="store_true",
                    help="N ルール (固定パーセント・売買代金上位50) を"
                         "同じエンジンで測り、こちらの買い側との重なりも出す")
    ap.add_argument("--self-test", dest="self_test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    INDEX_SYM = args.index_symbol
    globals()["MAX_GAP"] = args.max_gap

    if isinstance(args.prev_thr, str):
        args.prev_thr = (None if args.prev_thr.strip().lower() in ("none", "なし")
                         else float(args.prev_thr))
    min_prev = (args.prev_thr if args.prev_thr is not None
                else 0.0)   # パネル保持は |gap_z|>=0.3 で担保されるので 0 でよい

    syms: list[str] = []
    if args.data_dir or args.data_file:
        syms = load_local(args.data_dir, args.data_file)
        syms = [x for x in syms if x != INDEX_SYM]
    elif not args.no_cache:
        syms, has_index = load_cache_dir(args.cache_dir, INDEX_SYM,
                                         want_index=not args.no_index)
        if syms and not has_index and not args.no_index \
                and not args.index_from_universe:
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
    if args.nrule:
        # 生パーセントの固定閾値で発火する行を候補から落とさない
        globals()["_NRULE"] = True
    keep_thr = 0.9 if args.grid else min_prev * (1.0 if args.raw else 0.9)
    panel, mkt = build_panel(syms, args.workers, args.earnings, keep_thr,
                             no_index=args.no_index,
                             index_from_universe=args.index_from_universe)
    if args.csv:
        panel.to_csv(args.csv, index=False)
        print(f"パネルを {args.csv} に保存")
    if args.audit:
        audit_report()
    if args.shuffle_test:
        return shuffle_test(panel, mkt, args)
    if args.grid:
        grid_scan(panel, mkt, args)
    elif args.nrule:
        return nrule_report(panel, mkt, args)
    else:
        report(panel, mkt, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
