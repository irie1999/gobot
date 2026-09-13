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

import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))


# ── ローカルデータ(J-Quants 5分足)のパス ────────────────────────────
# データは別プロジェクト(daytrading フォルダ)に置かれていることが多い。
# swingtrade から実行しても見つかるよう、以下の順で自動解決する:
#   1. 環境変数 MINUTE_5M_DIR (最優先。恒久固定したいとき)
#   2. <このファイルの隣>/data/minute_5m
#   3. 隣接する daytrading プロジェクトの data/minute_5m
#      (例: ...\kabu station\swingtrade と ...\kabu station\daytrading が兄弟)
def _resolve_data_dir() -> Path:
    env = os.environ.get("MINUTE_5M_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    # 「完璧な」J-Quants 5分足は隣接の stock_5min フォルダに置かれている。
    # (例: ...\kabu station\swingtrade と ...\kabu station\stock_5min が兄弟)
    candidates = [
        here / "data" / "minute_5m",
        here.parent / "stock_5min",
        here.parent / "stock_5min" / "data" / "minute_5m",
        here.parent / "daytrading" / "data" / "minute_5m",
        here.parent / "daytrade" / "data" / "minute_5m",
    ]
    for c in candidates:
        try:
            if c.exists() and any(c.glob("*.pkl")):
                return c
        except Exception:
            continue
    return candidates[0]   # 見つからなければ従来の既定 (空でも従来通り動く)


DATA_DIR = _resolve_data_dir()


# ─────────────────────────────────────────────────────────────
# 銘柄コード変換
# ─────────────────────────────────────────────────────────────

def yf_to_jquants(yf_code: str) -> str:
    """7203.T → 72030"""
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 4 and code.isdigit():
        return code + "0"
    return code


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
        dt_series = pd.to_datetime(df["DateTime"])
    elif "Date" in df.columns and "Time" in df.columns:
        dt_series = pd.to_datetime(
            df["Date"].astype(str) + " " + df["Time"].astype(str),
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

    # ── 幻のバー(出来高0 かつ OHLC が全部同値)を捨てる ────────────────────
    # yfinance の日本株分足は、寄り前後に **出来高0・OHLCすべて前日終値** の
    # 合成バーを出す(2026-08-12 実測: 4208 の 09:00 が O=H=L=C=3,554 / V=0)。
    # これは取引ではないので、残しておくと3つの害がある:
    #   ① eh_trades.require_open_bar が「先頭バーが09:00か」で母集団を選ぶので、
    #      **幻のバーがある銘柄だけ通過**する(寄りは見えていないのに)。
    #      実測でこれが 633銘柄中355件の除外を左右していた。
    #   ② stop_delay_bars(delay1) の起点が1本ずれる。幻ありは09:05武装、
    #      幻なしは09:10武装 = **同じ delay1 が別の処理**になっていた(18.9)。
    #      その差が 18.32 の「+1,583円/件・2.7倍」の説明になりうる。
    #   ③ 存在しない価格(前日終値)で損切り/利確の判定が走りうる。
    # 取引が1件も無いバーなので、落として失う情報は無い。
    # 切り戻し: set LSS_KEEP_PHANTOM_BARS=1
    if str(os.environ.get("LSS_KEEP_PHANTOM_BARS", "")).strip() not in ("1", "true", "yes"):
        _ph = ((out["volume"].fillna(0) <= 0)
               & (out["open"] == out["high"])
               & (out["open"] == out["low"])
               & (out["open"] == out["close"]))
        if bool(_ph.any()):
            out = out[~_ph]

    out.index.name = "DateTime"
    return out.sort_index()


def resample_to_5m(df: pd.DataFrame) -> pd.DataFrame:
    """1分足 → 5分足。既に5分足なら不要。"""
    if df.empty:
        return df
    # 平均バー間隔を推定
    if len(df) >= 2:
        avg_gap = (df.index[-1] - df.index[0]).total_seconds() / (len(df) - 1)
        if avg_gap >= 250:  # 既に ~5分足
            return df
    return df.resample("5min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])


# ─────────────────────────────────────────────────────────────
# ソース1: ローカル保存データ (data/minute_5m/*.pkl)
# ─────────────────────────────────────────────────────────────

def _load_local(symbol: str, days: int) -> pd.DataFrame | None:
    """ローカル pickle から読み込み → 正規化 → 期間フィルタ。"""
    jq_code = yf_to_jquants(symbol)
    pkl_path = DATA_DIR / f"{jq_code}.pkl"
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

    df = resample_to_5m(df)

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
    if df.empty:
        return {}
    # groupby は O(n) — 旧実装の O(n_dates × n_rows) より大幅に高速
    return {date: grp for date, grp in df.groupby(df.index.date) if len(grp) >= 5}


def available_local_symbols() -> list[str]:
    """ローカルに保存されている銘柄コード一覧。"""
    if not DATA_DIR.exists():
        return []
    return sorted(f.stem for f in DATA_DIR.glob("*.pkl"))


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
    args = parser.parse_args()

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
