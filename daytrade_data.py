"""
daytrade_data.py  ―  デイトレ戦略共通のデータローダー
==================================================================
3 戦略 (ORB / VWAP / VolSurge) で共通のデータ取得・ロード機能。
ローカル保存データ → J-Quants API → yfinance の優先順位で自動選択。

【使い方】
  from daytrade_data import load_intraday_batch

  # 自動 (ローカル優先、なければ yfinance)
  dfs = load_intraday_batch(["7203.T", "9984.T"], days=60)

  # ローカル固定 (download_all_minute.py で保存したデータ)
  dfs = load_intraday_batch(["7203.T"], days=60, source="local")

  # yfinance 固定
  dfs = load_intraday_batch(["7203.T"], days=60, source="yfinance")

【データソース】
  source="auto"     : ローカル → yfinance の順で自動選択
  source="local"    : data/minute_5m/*.pkl のみ
  source="yfinance" : yfinance API のみ
  source="jquants"  : J-Quants API (要契約)
"""

from __future__ import annotations

import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))

# ── ローカルデータのパス ────────────────────────────────────
# minute_5m   : 直近データ (日次更新, ~3ヶ月)  → 日次シグナル確認に使用
# quarantine_5m: 長期データ (蓄積済, ~2年)     → WalkForward銘柄選定に使用
# _load_local は両フォルダをマージして返す (重複は新しい方を優先)
DATA_DIR       = Path(__file__).resolve().parent / "data" / "minute_5m"
QUARANTINE_DIR = Path(__file__).resolve().parent / "data" / "quarantine_5m"


# ─────────────────────────────────────────────────────────────
# 銘柄コード変換
# ─────────────────────────────────────────────────────────────

def yf_to_jquants(yf_code: str) -> str:
    """7203.T → 72030"""
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 4 and code.isdigit():
        return code + "0"
    return code


def jq_to_yf(jq_code: str) -> str:
    """72030 → 7203.T"""
    code = str(jq_code).strip()
    if len(code) == 5 and code.isdigit():
        return code[:4] + ".T"
    return code


def quarantine_symbols() -> list[str]:
    """quarantine_5m フォルダに存在する銘柄コード一覧 (yfinance形式)。"""
    if not QUARANTINE_DIR.exists():
        return []
    return sorted(jq_to_yf(f.stem) for f in QUARANTINE_DIR.glob("*.pkl"))


# ─────────────────────────────────────────────────────────────
# 正規化: J-Quants / yfinance → 共通形式
# ─────────────────────────────────────────────────────────────

def normalize_minute_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    任意ソースの分足 DataFrame を共通形式に変換。

    出力:
      - Index: DatetimeIndex (tz-naive, JST前提)
      - Columns: [open, high, low, close, volume]  (小文字)
    """
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()

    # ── DateTime 構築 ─────────────────────────────────────
    if "DateTime" in df.columns:
        dt_series = pd.to_datetime(df["DateTime"], errors="coerce")
    elif "Date" in df.columns and "Time" in df.columns:
        # Date 列は "2024-04-12" / "2024-04-12 00:00:00" / Timestamp など
        # 形式がまちまち。まず日付部分だけに正規化してから Time と連結する。
        date_part = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        time_part = df["Time"].astype(str).str.strip()
        dt_series = pd.to_datetime(
            date_part + " " + time_part,
            format="%Y-%m-%d %H:%M",
            errors="coerce",
        )
    elif "datetime" in df.columns:
        dt_series = pd.to_datetime(df["datetime"])
    else:
        # yfinance 形式: index が datetime
        if isinstance(df.index, pd.DatetimeIndex):
            dt_series = df.index
        else:
            return pd.DataFrame()

    # ── OHLCV カラム解決 ──────────────────────────────────
    # J-Quants V2 省略名 → 正式名
    rename = {"O": "Open", "H": "High", "L": "Low",
              "C": "Close", "Vo": "Volume"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    ohlcv = None
    for cols in [
        ("AdjustmentOpen", "AdjustmentHigh", "AdjustmentLow",
         "AdjustmentClose", "AdjustmentVolume"),
        ("Open", "High", "Low", "Close", "Volume"),
        ("open", "high", "low", "close", "volume"),
    ]:
        if all(c in df.columns for c in cols):
            ohlcv = cols
            break

    if ohlcv is None:
        return pd.DataFrame()

    out = pd.DataFrame({
        "open":   pd.to_numeric(df[ohlcv[0]], errors="coerce").values,
        "high":   pd.to_numeric(df[ohlcv[1]], errors="coerce").values,
        "low":    pd.to_numeric(df[ohlcv[2]], errors="coerce").values,
        "close":  pd.to_numeric(df[ohlcv[3]], errors="coerce").values,
        "volume": pd.to_numeric(df[ohlcv[4]], errors="coerce").values,
    }, index=dt_series.values if hasattr(dt_series, "values") else dt_series)

    # tz-naive に統一
    if hasattr(out.index, "tz") and out.index.tz is not None:
        out.index = out.index.tz_convert("Asia/Tokyo").tz_localize(None)

    out = out.dropna(subset=["close"])
    out.index.name = "DateTime"
    out = out.sort_index()
    out = _drop_price_outliers(out)
    return out


def _drop_price_outliers(df: pd.DataFrame) -> pd.DataFrame:
    """
    破損バー (極端な価格) を除去する。

    5分足の close が局所中央値の 3倍超 / 1/3未満 になることは正常な値動きでは
    ありえない (yfinance 等の bad tick / 桁ズレ)。ローリング中央値 (外れ値に
    頑健) を基準に band 外のバーを落とす。また非正の値・high<low も除去。
    """
    if df is None or df.empty:
        return df
    # 基本サニティ: 正の価格・high>=low
    base = (df[["open", "high", "low", "close"]] > 0).all(axis=1) & (df["high"] >= df["low"])
    df = df[base]
    if len(df) < 20:
        return df
    # 許容帯: 5分足1本が局所中央値の 0.5倍未満/2.0倍超になることは正常市場では
    # ありえない (値幅制限)。安値方向の破損(寄りだけ1970円等)も捕まえるため、
    # close だけでなく high/low も基準にする (low<=open,close<=high なので
    # high/low を見れば全OHLCをカバー)。
    LO, HI = 0.5, 2.0
    cols = ["open", "high", "low", "close"]
    # ── 高速パス: 全体中央値に対して極端な値が無ければローリング計算をスキップ ──
    # open だけ破損するケース(寄りバー)があるため全OHLCの最小/最大で判定する。
    global_med = df["close"].median()
    if global_med > 0:
        gmin = df[cols].min().min() / global_med
        gmax = df[cols].max().max() / global_med
        if gmin >= LO and gmax <= HI:
            return df  # 外れ値なし → 重いローリング中央値は計算しない
    # ── 通常パス: 窓は十分大きく (~1ヶ月)。破損が数日連続ブロックでも窓内では
    #    少数派なので中央値は真の価格水準を保つ → ブロック破損も弾ける。
    #    トレンドはローリング中央値が追従するので誤除去しない。
    win = min(2001, len(df) // 2 * 2 + 1)
    med = df["close"].rolling(win, min_periods=50, center=True).median()
    med = med.bfill().ffill()
    # 全OHLC(open/high/low/close)が局所中央値の[LO,HI]倍内に収まるバーだけ残す。
    keep = pd.Series(True, index=df.index)
    for c in cols:
        r = df[c] / med
        rg = df[c] / global_med
        keep &= (r >= LO) & (r <= HI) & (rg >= 0.1) & (rg <= 10.0)
    return df[keep]


def inspect_pkl(symbol: str) -> None:
    """
    quarantine_5m / minute_5m の pkl ファイルを直接検査してデバッグ情報を表示。
    データが読み込めない原因を特定するためのユーティリティ。
    """
    import pickle as _pickle

    jq_code = yf_to_jquants(symbol)

    for label, dir_path in [("minute_5m", DATA_DIR), ("quarantine_5m", QUARANTINE_DIR)]:
        pkl_path = dir_path / f"{jq_code}.pkl"
        print(f"\n  [{label}] {pkl_path}")
        if not pkl_path.exists():
            print(f"    ファイルなし")
            continue

        try:
            raw = _pickle.loads(pkl_path.read_bytes())
        except Exception as e:
            print(f"    読み込み失敗: {e}")
            continue

        if not isinstance(raw, pd.DataFrame):
            print(f"    DataFrame でない: type={type(raw)}")
            continue

        print(f"    shape: {raw.shape}")
        print(f"    index type: {type(raw.index).__name__}")
        print(f"    columns(全): {list(raw.columns)}")
        if len(raw) >= 2 and isinstance(raw.index, pd.DatetimeIndex):
            print(f"    index[0]:  {raw.index[0]}")
            print(f"    index[-1]: {raw.index[-1]}")
            avg_gap = (raw.index[-1] - raw.index[0]).total_seconds() / (len(raw) - 1)
            print(f"    avg bar gap: {avg_gap:.0f}s ({avg_gap/60:.1f}min)")
        # DateTime列があればそこから期間を表示 (RangeIndex対応)
        for dtcol in ("DateTime", "Date", "datetime"):
            if dtcol in raw.columns:
                _dt = pd.to_datetime(raw[dtcol], errors="coerce").dropna()
                if not _dt.empty:
                    print(f"    {dtcol}列の範囲: {_dt.min()} 〜 {_dt.max()}")
                break
        print(f"    head(1):\n{raw.head(1).to_string()}")

        # normalize 試行
        df_norm = normalize_minute_df(raw)
        if df_norm.empty:
            print(f"    ★ normalize_minute_df → EMPTY (読み込み失敗!)")
            # 原因調査
            has_dt = any(c in raw.columns for c in ("DateTime","Date","datetime"))
            has_idx_dt = isinstance(raw.index, pd.DatetimeIndex)
            has_ohlcv_adj = all(c in raw.columns for c in ("AdjustmentOpen","AdjustmentClose"))
            has_ohlcv_cap = all(c in raw.columns for c in ("Open","Close"))
            has_ohlcv_low = all(c in raw.columns for c in ("open","close"))
            print(f"    DateTime列: {has_dt}  DatetimeIndex: {has_idx_dt}")
            print(f"    AdjustmentOHLC: {has_ohlcv_adj}  OHLC(大文字): {has_ohlcv_cap}  ohlc(小文字): {has_ohlcv_low}")
        else:
            print(f"    ✓ normalize_minute_df → {len(df_norm)}行 "
                  f"{df_norm.index[0]} 〜 {df_norm.index[-1]}")


# ─────────────────────────────────────────────────────────────
# ソース1: ローカル保存データ (data/minute_5m/*.pkl)
# ─────────────────────────────────────────────────────────────

def resample_to_5m(df: pd.DataFrame) -> pd.DataFrame:
    """1分足 → 5分足。既に5分足ならそのまま返す (J-Quants 5分足は no-op)。"""
    if df is None or df.empty:
        return df
    if len(df) >= 2:
        avg_gap = (df.index[-1] - df.index[0]).total_seconds() / (len(df) - 1)
        if avg_gap >= 250:   # 平均バー間隔 ~5分以上 → 既に5分足
            return df
    return df.resample("5min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])


def _load_pkl(pkl_path: Path) -> pd.DataFrame | None:
    """pkl ファイルを読み込んで正規化・5分足化して返す。"""
    if not pkl_path.exists():
        return None
    try:
        raw = pickle.loads(pkl_path.read_bytes())
    except Exception:
        return None
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None
    df = normalize_minute_df(raw)
    if df.empty:
        return None
    return resample_to_5m(df)


NORM_CACHE_DIR = Path(__file__).resolve().parent / ".daytrade_norm_cache"
# 正規化/外れ値除去ロジックを変えたらバンプする (古いキャッシュを自動無効化)。
_NORM_CACHE_VERSION = 3


def _load_local_full(jq_code: str) -> pd.DataFrame | None:
    """
    minute_5m + quarantine_5m をマージ・正規化した『全期間』df を返す
    (期間フィルタ前)。正規化結果をディスクキャッシュし、2回目以降を高速化。
    ソース pkl より新しいキャッシュがあればそれを使う。
    """
    recent_path = DATA_DIR / f"{jq_code}.pkl"
    long_path   = QUARANTINE_DIR / f"{jq_code}.pkl"
    src_mtimes = [p.stat().st_mtime for p in (recent_path, long_path) if p.exists()]
    if not src_mtimes:
        return None

    cache_path = NORM_CACHE_DIR / f"{jq_code}_v{_NORM_CACHE_VERSION}.pkl"
    # キャッシュが全ソースより新しければ採用
    if cache_path.exists() and cache_path.stat().st_mtime >= max(src_mtimes):
        try:
            return pickle.loads(cache_path.read_bytes())
        except Exception:
            pass  # 壊れていたら作り直す

    df_recent = _load_pkl(recent_path)
    df_long   = _load_pkl(long_path)
    if df_recent is None and df_long is None:
        return None
    if df_recent is not None and df_long is not None:
        combined = pd.concat([df_long, df_recent])
        combined = combined[~combined.index.duplicated(keep="last")]
        df = combined.sort_index()
    elif df_recent is not None:
        df = df_recent
    else:
        df = df_long

    try:
        NORM_CACHE_DIR.mkdir(exist_ok=True)
        cache_path.write_bytes(pickle.dumps(df))
    except Exception:
        pass
    return df


def _load_local(symbol: str, days: int) -> pd.DataFrame | None:
    """
    ローカル pickle から読み込み → 正規化 → 期間フィルタ。

    minute_5m (直近) と quarantine_5m (長期) の両方を読み込んでマージ
    (正規化済みをキャッシュ)。重複インデックスは新しいデータ (minute_5m) を優先。
    """
    jq_code = yf_to_jquants(symbol)
    df = _load_local_full(jq_code)
    if df is None or df.empty:
        return None

    # 期間フィルタ
    cutoff = pd.Timestamp(datetime.now(JST).date() - timedelta(days=days))
    df = df[df.index >= cutoff]
    return df if not df.empty else None


# ─────────────────────────────────────────────────────────────
# ソース2: yfinance
# ─────────────────────────────────────────────────────────────

def _load_yfinance_batch(symbols: list[str], days: int) -> dict[str, pd.DataFrame]:
    """yfinance でバッチ取得 (既存コードの移植)。"""
    try:
        import yfinance as yf
    except ImportError:
        print("[warn] yfinance が未インストール", file=sys.stderr)
        return {}

    if days > 60:
        print(f"[info] yfinance 5分足は最大60日 → {min(days, 60)}日に調整",
              file=sys.stderr)
        days = min(days, 60)

    try:
        df = yf.download(
            " ".join(symbols), period=f"{days}d", interval="5m",
            auto_adjust=False, progress=False, group_by="ticker", threads=True,
        )
    except Exception as e:
        print(f"[warn] yfinance download error: {e}", file=sys.stderr)
        return {}

    if df is None or df.empty:
        return {}

    result: dict[str, pd.DataFrame] = {}
    if isinstance(df.columns, pd.MultiIndex):
        for sym in symbols:
            if sym not in df.columns.get_level_values(0):
                continue
            sub = df[sym].copy()
            sub.columns = [str(c).lower() for c in sub.columns]
            sub = sub.dropna(subset=["close"])
            if sub.index.tz is not None:
                sub.index = sub.index.tz_convert("Asia/Tokyo").tz_localize(None)
            if not sub.empty:
                cols = [c for c in ["open", "high", "low", "close", "volume"]
                        if c in sub.columns]
                result[sym] = sub[cols]
    else:
        df.columns = [str(c).lower() for c in df.columns]
        df = df.dropna(subset=["close"])
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
        cols = [c for c in ["open", "high", "low", "close", "volume"]
                if c in df.columns]
        if not df.empty:
            result[symbols[0]] = df[cols]
    return result


# ─────────────────────────────────────────────────────────────
# 統合ローダー
# ─────────────────────────────────────────────────────────────

def load_intraday(symbol: str, days: int = 60,
                  source: str = "auto") -> pd.DataFrame | None:
    """
    1銘柄の5分足を取得。

    source:
      "auto"     : ローカル → yfinance の順で試行
      "local"    : data/minute_5m/ のみ
      "yfinance" : yfinance API のみ
    """
    if source in ("auto", "local"):
        df = _load_local(symbol, days)
        if df is not None:
            return df
        if source == "local":
            return None

    if source in ("auto", "yfinance"):
        batch = _load_yfinance_batch([symbol], days)
        return batch.get(symbol)

    return None


def load_intraday_batch(symbols: list[str], days: int = 60,
                        source: str = "auto") -> dict[str, pd.DataFrame]:
    """
    複数銘柄の5分足を取得。

    source="auto" の場合:
      1. まずローカルから全銘柄ロード試行
      2. ローカルにない銘柄のみ yfinance で取得
    """
    result: dict[str, pd.DataFrame] = {}
    remaining: list[str] = []

    if source in ("auto", "local"):
        for sym in symbols:
            df = _load_local(sym, days)
            if df is not None and not df.empty:
                result[sym] = df
            else:
                remaining.append(sym)

        if result:
            print(f"  ローカル: {len(result)}/{len(symbols)}銘柄ロード済み",
                  flush=True)

        if source == "local":
            if remaining:
                print(f"  [warn] {len(remaining)}銘柄がローカルに見つかりません",
                      flush=True)
            return result
    else:
        remaining = list(symbols)

    if remaining and source in ("auto", "yfinance"):
        print(f"  yfinance: {len(remaining)}銘柄を取得中...", flush=True)
        yf_result = _load_yfinance_batch(remaining, days)
        result.update(yf_result)
        found = len(yf_result)
        missed = len(remaining) - found
        if missed > 0:
            print(f"  [warn] {missed}銘柄がyfinanceでも取得失敗", flush=True)

    return result


# ─────────────────────────────────────────────────────────────
# ポジションサイジング
# ─────────────────────────────────────────────────────────────

def calc_position_size(entry_p: float, stop_p: float,
                       budget: int = 600_000,
                       max_risk: int = 6_000) -> int:
    """
    固定リスク額方式のポジションサイジング。

    1トレードの最大損失額を max_risk 円に固定し、
    損切り幅から逆算して株数を決定する。

    Args:
        entry_p : エントリー価格
        stop_p  : 損切り価格
        budget  : 投資資金 (円)
        max_risk: 1トレードの最大損失額 (円)

    Returns:
        株数 (100株単位、最低100株)
    """
    risk_per_share = abs(entry_p - stop_p)
    if risk_per_share <= 0:
        return 100

    # リスクから逆算した株数
    qty_by_risk = int(max_risk / risk_per_share / 100) * 100

    # 予算から逆算した最大株数
    qty_by_budget = int(budget / entry_p / 100) * 100

    qty = min(qty_by_risk, qty_by_budget)
    return max(100, qty)


def split_by_day(df: pd.DataFrame) -> dict:
    """DatetimeIndex の DataFrame を日付ごとに分割。"""
    result = {}
    for date in sorted(set(df.index.date)):
        sub = df[df.index.date == date]
        if len(sub) >= 5:
            result[date] = sub
    return result


def available_local_symbols() -> list[str]:
    """ローカルに保存されている銘柄コード一覧 (両フォルダの和集合)。"""
    codes: set[str] = set()
    if DATA_DIR.exists():
        codes.update(f.stem for f in DATA_DIR.glob("*.pkl"))
    if QUARANTINE_DIR.exists():
        codes.update(f.stem for f in QUARANTINE_DIR.glob("*.pkl"))
    return sorted(codes)


# ─────────────────────────────────────────────────────────────
# CLI (デバッグ / 確認用)
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="デイトレ データローダー")
    parser.add_argument("symbols", nargs="*", help="銘柄 (例: 7203.T)")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--source", choices=["auto", "local", "yfinance"],
                        default="auto")
    parser.add_argument("--list-local", action="store_true",
                        help="ローカルに保存済みの銘柄一覧を表示")
    parser.add_argument("--inspect", action="store_true",
                        help="pkl ファイルを直接検査 (データ読み込み問題の診断)")
    parser.add_argument("--survey", action="store_true",
                        help="quarantine_5m / minute_5m フォルダ全体の期間を一覧 (2年分データの所在確認)")
    parser.add_argument("--dump-day", type=str, default=None,
                        help="指定日(YYYY-MM-DD)の生バーを両ファイルから表示 (破損調査)")
    args = parser.parse_args()

    if args.dump_day:
        import pickle as _pickle
        target = args.symbols[0] if args.symbols else "1721.T"
        jq_code = yf_to_jquants(target)
        day = args.dump_day
        for label, dir_path in [("minute_5m", DATA_DIR), ("quarantine_5m", QUARANTINE_DIR)]:
            p = dir_path / f"{jq_code}.pkl"
            print(f"\n{'='*60}\n  [{label}] {p}  日={day}\n{'='*60}")
            if not p.exists():
                print("  ファイルなし"); continue
            raw = _pickle.loads(p.read_bytes())
            # 日付列を特定
            if "DateTime" in raw.columns:
                dts = pd.to_datetime(raw["DateTime"], errors="coerce")
            elif "Date" in raw.columns:
                dts = pd.to_datetime(raw["Date"], errors="coerce")
            else:
                print("  日付列なし"); continue
            mask = dts.dt.strftime("%Y-%m-%d") == day
            sub = raw[mask]
            print(f"  {day} のバー数: {len(sub)}")
            ohlc = [c for c in ["O","H","L","C","Open","High","Low","Close","Time"] if c in raw.columns]
            if len(sub):
                print(sub[ohlc].head(8).to_string())
            # その銘柄全体の価格レンジも表示 (スケール確認)
            ccol = "C" if "C" in raw.columns else ("Close" if "Close" in raw.columns else None)
            if ccol:
                cc = pd.to_numeric(raw[ccol], errors="coerce").dropna()
                print(f"  ファイル全体の close: min={cc.min():.0f} "
                      f"中央値={cc.median():.0f} max={cc.max():.0f}")
            # フィルタが使うローリング中央値と比率を当日について表示
            norm = normalize_minute_df(raw)  # 注: これは外れ値除去後
            # 外れ値除去前の生seriesでローリング中央値を再現
            if "DateTime" in raw.columns:
                idx = pd.to_datetime(raw["DateTime"], errors="coerce")
            else:
                idx = pd.to_datetime(raw["Date"].astype(str).str.slice(0, 10)
                                     + " " + raw["Time"].astype(str), errors="coerce")
            s = pd.Series(pd.to_numeric(raw[ccol], errors="coerce").values, index=idx)
            s = s[~s.index.isna()].dropna()
            s = s[~s.index.duplicated()].sort_index()
            win = min(2001, len(s) // 2 * 2 + 1)
            med = s.rolling(win, min_periods=50, center=True).median().bfill().ffill()
            day_mask = s.index.strftime("%Y-%m-%d") == day
            print(f"  --- ローリング中央値(win={win})と比率 (当日先頭5本) ---")
            for ts in s.index[day_mask][:5]:
                c = s.loc[ts]; m = med.loc[ts]
                r = c / m if m else 0
                flag = " ★除去対象" if (r < 0.5 or r > 2.0) else ""
                print(f"    {ts}  close={c:.0f}  med={m:.0f}  ratio={r:.3f}{flag}")
            # 当月の分布
            month = day[:7]
            mm = s[s.index.strftime("%Y-%m") == month]
            if len(mm):
                print(f"  {month}全体: min={mm.min():.0f} 中央値={mm.median():.0f} "
                      f"max={mm.max():.0f}  n<3000={int((mm<3000).sum())}/{len(mm)}")
            print(f"  normalize_minute_df後の当日バー数: "
                  f"{int((norm.index.strftime('%Y-%m-%d')==day).sum()) if not norm.empty else 0}")
        sys.exit(0)

    if args.survey:
        import pickle as _pickle
        import gc

        def _cheap_daterange(raw):
            """full normalize せず日付範囲だけ安価に取得 (メモリ節約)。
            return (first_ts, last_ts, nrows) or None。"""
            if not isinstance(raw, pd.DataFrame) or raw.empty:
                return None
            nrows = len(raw)
            # DateTime 列 or index から日付だけ拾う
            if "DateTime" in raw.columns:
                s = pd.to_datetime(raw["DateTime"], errors="coerce")
            elif "Date" in raw.columns:
                s = pd.to_datetime(raw["Date"].astype(str), errors="coerce")
            elif "datetime" in raw.columns:
                s = pd.to_datetime(raw["datetime"], errors="coerce")
            elif isinstance(raw.index, pd.DatetimeIndex):
                s = raw.index
            else:
                return None
            s = pd.Series(s).dropna()
            if s.empty:
                return None
            return (s.min(), s.max(), nrows)

        for label, dir_path in [("quarantine_5m (長期)", QUARANTINE_DIR),
                                 ("minute_5m (直近)", DATA_DIR)]:
            print(f"\n{'='*70}")
            print(f"  {label}: {dir_path}", flush=True)
            print(f"{'='*70}", flush=True)
            if not dir_path.exists():
                print("  フォルダなし", flush=True)
                continue
            files = sorted(dir_path.glob("*.pkl"))
            if not files:
                print("  pkl ファイルなし", flush=True)
                continue
            spans = []  # (days_span, code, first, last, nbars)
            total_bytes = 0
            print(f"  走査中... ({len(files)}ファイル)", flush=True)
            for i, f in enumerate(files, 1):
                total_bytes += f.stat().st_size
                try:
                    with open(f, "rb") as _fh:
                        raw = _pickle.load(_fh)
                    rng = _cheap_daterange(raw)
                    del raw
                    if rng is None:
                        spans.append((-1, f.stem, None, None, 0))
                    else:
                        first, last, nrows = rng
                        span_days = (last - first).days
                        spans.append((span_days, f.stem, first, last, nrows))
                except Exception as e:
                    spans.append((-2, f.stem, str(e)[:40], None, 0))
                if i % 200 == 0:
                    print(f"    ...{i}/{len(files)} 完了", flush=True)
                    gc.collect()
            ok = [s for s in spans if s[0] >= 0]
            bad = [s for s in spans if s[0] < 0]
            print(f"  ファイル数: {len(files)}  合計: {total_bytes/1024/1024:.1f}MB",
                  flush=True)
            if ok:
                max_span = max(s[0] for s in ok)
                over2y = [s for s in ok if s[0] >= 700]
                over1y = [s for s in ok if s[0] >= 350]
                print(f"  正常読込: {len(ok)}銘柄  最長期間: {max_span}日 "
                      f"(~{max_span/365:.1f}年)", flush=True)
                print(f"    ≥2年(700日): {len(over2y)}銘柄  "
                      f"≥1年(350日): {len(over1y)}銘柄", flush=True)
                print(f"  --- 期間が長い順 上位10 ---", flush=True)
                for span, code, first, last, n in sorted(
                        ok, key=lambda x: x[0], reverse=True)[:10]:
                    print(f"    {code:>6}  {span:>4}日  {n:>7}本  "
                          f"{first.date()} 〜 {last.date()}", flush=True)
            if bad:
                print(f"  ★ 読込失敗/空: {len(bad)}銘柄 "
                      f"(例: {', '.join(s[1] for s in bad[:5])})", flush=True)
        sys.exit(0)

    if args.inspect:
        targets = args.symbols if args.symbols else quarantine_symbols()[:3]
        for sym in targets:
            print(f"\n{'='*60}")
            print(f"  検査: {sym}")
            print(f"{'='*60}")
            inspect_pkl(sym)
        sys.exit(0)

    if args.list_local:
        codes = available_local_symbols()
        print(f"ローカル保存済み: {len(codes)}銘柄")
        for c in codes[:20]:
            pkl = DATA_DIR / f"{c}.pkl"
            size = pkl.stat().st_size / 1024
            print(f"  {c}  {size:.0f}KB")
        if len(codes) > 20:
            print(f"  ... 他 {len(codes) - 20}銘柄")
        sys.exit(0)

    if not args.symbols:
        parser.print_help()
        sys.exit(1)

    print(f"source={args.source}  days={args.days}")
    dfs = load_intraday_batch(args.symbols, days=args.days, source=args.source)

    for sym in args.symbols:
        if sym not in dfs:
            print(f"  {sym}: データなし")
            continue
        df = dfs[sym]
        n_days = len(set(df.index.date))
        print(f"  {sym}: {len(df)}本 / {n_days}営業日  "
              f"({df.index[0]} 〜 {df.index[-1]})")
        print(f"    {df.head(2).to_string()}")
