"""
jquants_fetch.py  ―  J-Quants V2 株価取得ユーティリティ
==================================================================
公式 jquantsapi ライブラリ (jquants-api-client) をラップし、
既存の backtest コードと互換の DataFrame を返す。

【セットアップ】
  pip install jquants-api-client

  .env に以下を記載:
    JQUANTS_API_KEY=ダッシュボードで取得した API Key

【主な機能】
  - fetch_daily(symbol, days)            日足 (yfinance互換形式)
  - fetch_daily_batch(symbols, days)     日足バッチ
  - fetch_intraday(symbol, days, "5m")   5分足 (yfinance互換形式)
  - fetch_intraday_batch(symbols, days)  5分足バッチ
  - yf_to_jquants("7203.T") → "72030"   コード変換

【CLI】
  python jquants_fetch.py 7203.T --days 90              # 日足
  python jquants_fetch.py 7203.T --intraday --days 60   # 5分足
  python jquants_fetch.py --watchlist --intraday --days 60  # 60銘柄5分足
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))
CACHE_ROOT = Path.home() / ".jquants_cache"
DAILY_CACHE_DIR = CACHE_ROOT / "daily"
MINUTE_CACHE_DIR = CACHE_ROOT / "minute"

for d in [CACHE_ROOT, DAILY_CACHE_DIR, MINUTE_CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# .env ローダ (python-dotenv 不要)
# ─────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    # swingtrade だけでなく近隣フォルダ(kabu station直下・daytrading・stock_5min)
    # の .env も探索する。鍵が別プロジェクト側にあっても見つける。
    here = Path(__file__).resolve().parent
    candidates = [
        Path.cwd() / ".env",
        here / ".env",
        here.parent / ".env",
        here.parent / "daytrading" / ".env",
        here.parent / "stock_5min" / ".env",
    ]
    seen = set()
    for p in candidates:
        try:
            p = p.resolve()
        except Exception:
            continue
        if p in seen or not p.exists():
            continue
        seen.add(p)
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass
        if os.environ.get("JQUANTS_API_KEY"):
            return


_load_dotenv()


# ─────────────────────────────────────────────────────────────
# 公式クライアント (シングルトン)
# ─────────────────────────────────────────────────────────────

_CLIENT = None


def get_client():
    """jquantsapi.ClientV2 のシングルトン。"""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        import jquantsapi
    except ImportError:
        print("[ERROR] jquants-api-client が未インストールです。\n"
              "  pip install jquants-api-client", file=sys.stderr)
        sys.exit(1)
    api_key = os.environ.get("JQUANTS_API_KEY")
    if not api_key:
        print("[ERROR] JQUANTS_API_KEY が設定されていません。\n"
              "  .env ファイルまたは環境変数に設定してください。",
              file=sys.stderr)
        sys.exit(1)
    _CLIENT = jquantsapi.ClientV2(api_key=api_key)
    return _CLIENT


# ─────────────────────────────────────────────────────────────
# 銘柄コード変換
# ─────────────────────────────────────────────────────────────

def yf_to_jquants(yf_code: str) -> str:
    """7203.T → 72030"""
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 4 and code.isdigit():
        return code + "0"
    return code


def jquants_to_yf(jq_code: str) -> str:
    """72030 → 7203.T"""
    code = str(jq_code).strip()
    if len(code) == 5 and code.endswith("0") and code.isdigit():
        return code[:4] + ".T"
    return code + ".T"


# ─────────────────────────────────────────────────────────────
# DataFrame 正規化 (→ yfinance 互換)
# ─────────────────────────────────────────────────────────────

def _normalize(df: pd.DataFrame, dt_col: str = "Date") -> pd.DataFrame:
    """
    公式クライアントの返す DataFrame を
    既存 backtest コードが期待する形式に変換。

    出力: Index=DatetimeIndex(tz-naive), columns=[open,high,low,close,volume]
    """
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()

    # ── DateTime 解決 ─────────────────────────────────────
    if dt_col in df.columns:
        df[dt_col] = pd.to_datetime(df[dt_col])
    elif "DateTime" in df.columns:
        dt_col = "DateTime"
        df[dt_col] = pd.to_datetime(df[dt_col])
    elif "Date" in df.columns and "Time" in df.columns:
        dt_col = "DateTime"
        df[dt_col] = pd.to_datetime(
            df["Date"].astype(str) + " " + df["Time"].astype(str))
    else:
        return pd.DataFrame()

    # ── OHLCV 解決 ────────────────────────────────────────
    # 公式クライアントは AdjustmentClose 等を返す
    rename_map = {"O": "Open", "H": "High", "L": "Low",
                  "C": "Close", "Vo": "Volume"}
    df = df.rename(columns=rename_map)

    # 調整済み列を優先
    ohlcv: dict[str, str] | None = None
    for cols in [
        ("AdjustmentOpen", "AdjustmentHigh", "AdjustmentLow",
         "AdjustmentClose", "AdjustmentVolume"),
        ("Open", "High", "Low", "Close", "Volume"),
        ("open", "high", "low", "close", "volume"),
    ]:
        if all(c in df.columns for c in cols):
            ohlcv = dict(zip(cols, ["open", "high", "low", "close", "volume"]))
            break
    if ohlcv is None:
        return pd.DataFrame()

    out = pd.DataFrame({
        new: pd.to_numeric(df[old], errors="coerce").values
        for old, new in ohlcv.items()
    }, index=pd.to_datetime(df[dt_col]).values)

    out = out.dropna(subset=["close"])
    out.index.name = dt_col
    out = out.sort_index()
    return out


# ─────────────────────────────────────────────────────────────
# キャッシュ
# ─────────────────────────────────────────────────────────────

def _cache_path(cache_dir: Path, jq_code: str) -> Path:
    return cache_dir / f"{jq_code}.pkl"


def _is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=JST)
    return mtime.date() == datetime.now(JST).date()


def _load_pkl(path: Path) -> pd.DataFrame | None:
    try:
        return pickle.loads(path.read_bytes())
    except Exception:
        return None


def _save_pkl(path: Path, df: pd.DataFrame) -> None:
    try:
        path.write_bytes(pickle.dumps(df))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 日足取得
# ─────────────────────────────────────────────────────────────

def fetch_daily(symbol: str, days: int = 365,
                use_cache: bool = True) -> pd.DataFrame | None:
    jq_code = yf_to_jquants(symbol)
    cp = _cache_path(DAILY_CACHE_DIR, jq_code)

    if use_cache and _is_fresh(cp):
        cached = _load_pkl(cp)
        if cached is not None and not cached.empty:
            cutoff = pd.Timestamp(datetime.now(JST).date() - timedelta(days=days))
            out = cached[cached.index >= cutoff]
            if not out.empty:
                return out

    from dateutil import tz as dtz
    now = datetime.now(tz=dtz.gettz("Asia/Tokyo"))
    start = now - timedelta(days=days + 30)

    try:
        cli = get_client()
        raw = cli.get_eq_bars_daily_range(start_dt=start, end_dt=now)
    except Exception as e:
        print(f"  [err] {symbol}: {e}", file=sys.stderr)
        return None

    if raw is None or raw.empty:
        return None

    # 銘柄フィルタ (get_eq_bars_daily_range は全銘柄返す可能性)
    if "Code" in raw.columns:
        raw = raw[raw["Code"].astype(str).str.startswith(jq_code[:4])].copy()

    df = _normalize(raw, dt_col="Date")
    if df.empty:
        return None

    if use_cache:
        _save_pkl(cp, df)

    cutoff = pd.Timestamp(datetime.now(JST).date() - timedelta(days=days))
    df = df[df.index >= cutoff]
    return df if not df.empty else None


def fetch_daily_batch(symbols: list[str], days: int = 365,
                      use_cache: bool = True) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        df = fetch_daily(sym, days=days, use_cache=use_cache)
        if df is not None and not df.empty:
            result[sym] = df
        print(f"  [{i:>3}/{total}] {sym}: "
              f"{len(df) if df is not None else 0}本", flush=True)
    print(f"  取得成功: {len(result)}/{total}銘柄", flush=True)
    return result


# ─────────────────────────────────────────────────────────────
# 分足取得 (アドオン)
# ─────────────────────────────────────────────────────────────

def fetch_intraday(symbol: str, days: int = 60,
                   interval: str = "5m",
                   use_cache: bool = True) -> pd.DataFrame | None:
    """
    J-Quants 公式クライアントで分足を取得。

    interval: "1m", "5m", "15m"
    """
    jq_code = yf_to_jquants(symbol)
    cp = _cache_path(MINUTE_CACHE_DIR, f"{jq_code}_{interval}")

    if use_cache and _is_fresh(cp):
        cached = _load_pkl(cp)
        if cached is not None and not cached.empty:
            cutoff = pd.Timestamp(datetime.now(JST).date() - timedelta(days=days))
            out = cached[cached.index >= cutoff]
            if not out.empty:
                return out

    from dateutil import tz as dtz
    now = datetime.now(tz=dtz.gettz("Asia/Tokyo"))
    start = now - timedelta(days=days)

    cli = get_client()

    # interval に応じて公式メソッドを選択
    method_map = {
        "1m":  "get_eq_bars_minute",
        "5m":  "get_eq_bars_5minute",
        "15m": "get_eq_bars_15minute",
    }
    method_name = method_map.get(interval, "get_eq_bars_5minute")
    fetch_method = getattr(cli, method_name, None)

    if fetch_method is None:
        print(f"  [err] {method_name} が jquantsapi に存在しません",
              file=sys.stderr)
        return None

    # 30日チャンクで取得 (大量データ対応)
    all_dfs: list[pd.DataFrame] = []
    current = start
    chunk_idx = 0
    while current < now:
        chunk_end = min(current + timedelta(days=30), now)
        chunk_idx += 1
        from_d = current.strftime("%Y%m%d")
        to_d = chunk_end.strftime("%Y%m%d")
        try:
            raw = fetch_method(
                code=jq_code, from_yyyymmdd=from_d, to_yyyymmdd=to_d,
            )
            if raw is not None and not raw.empty:
                # 銘柄フィルタ
                if "Code" in raw.columns:
                    raw = raw[raw["Code"].astype(str).str.startswith(
                        jq_code[:4])].copy()
                if not raw.empty:
                    all_dfs.append(raw)
                    print(f"    chunk {chunk_idx}: "
                          f"{current.strftime('%Y-%m-%d')}〜"
                          f"{chunk_end.strftime('%Y-%m-%d')} → "
                          f"{len(raw)}行", flush=True)
        except Exception as e:
            print(f"    chunk {chunk_idx}: ERROR {e}", file=sys.stderr)
        current = chunk_end + timedelta(days=1)

    if not all_dfs:
        print(f"  [warn] {symbol}: 分足データなし", file=sys.stderr)
        return None

    combined = pd.concat(all_dfs, ignore_index=True)
    df = _normalize(combined)
    if df.empty:
        return None

    if use_cache:
        _save_pkl(cp, df)

    return df


def fetch_intraday_batch(symbols: list[str], days: int = 60,
                         interval: str = "5m",
                         use_cache: bool = True) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i:>3}/{total}] {sym} ({interval}) 取得中...", flush=True)
        df = fetch_intraday(sym, days=days, interval=interval,
                            use_cache=use_cache)
        if df is not None and not df.empty:
            result[sym] = df
            n_days = len(set(df.index.date)) if hasattr(df.index[0], "date") else 0
            print(f"  [{i:>3}/{total}] {sym}: {len(df)}本 / {n_days}営業日",
                  flush=True)
        else:
            print(f"  [{i:>3}/{total}] {sym}: データなし", flush=True)
    print(f"  取得成功: {len(result)}/{total}銘柄", flush=True)
    return result


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="J-Quants V2 株価取得ユーティリティ (公式クライアント使用)")
    parser.add_argument("symbols", nargs="*",
                        help="銘柄コード (例: 7203.T 9984.T)")
    parser.add_argument("--days", type=int, default=365, help="取得日数")
    parser.add_argument("--intraday", action="store_true",
                        help="分足モード (アドオン契約必要)")
    parser.add_argument("--interval", choices=["1m", "5m", "15m"],
                        default="5m", help="分足の間隔")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--watchlist", action="store_true",
                        help="daytrade_symbols.DAYTRADE_SYMBOLS 全銘柄")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    if args.watchlist:
        try:
            from daytrade_symbols import DAYTRADE_SYMBOLS
        except ImportError:
            print("[ERROR] daytrade_symbols.py が見つかりません",
                  file=sys.stderr)
            sys.exit(1)
        symbols = [s for s, _ in DAYTRADE_SYMBOLS]
    elif args.symbols:
        symbols = args.symbols
    else:
        parser.print_help()
        sys.exit(1)

    use_cache = not args.no_cache

    # ── 分足モード ────────────────────────────────────────
    if args.intraday:
        if len(symbols) == 1:
            sym = symbols[0]
            print(f"分足 ({args.interval}) 取得: {sym} "
                  f"({yf_to_jquants(sym)}) / {args.days}日")
            df = fetch_intraday(sym, days=args.days,
                                interval=args.interval, use_cache=use_cache)
            if df is None or df.empty:
                print("データなし")
                return
            n_days = len(set(df.index.date))
            print(f"\n取得結果: {len(df)}本 ({args.interval})")
            print(f"  期間: {df.index[0]} 〜 {df.index[-1]}  "
                  f"({n_days}営業日)")
            if args.summary:
                print(f"  最新終値: {df.iloc[-1]['close']:,.0f}")
                print(f"  最高値: {df['high'].max():,.0f}")
                print(f"  最安値: {df['low'].min():,.0f}")
                print(f"  バー数/日: {len(df) / max(n_days, 1):.0f}")
            else:
                print()
                print(df.head(5).to_string())
                print("...")
                print(df.tail(5).to_string())
        else:
            print(f"分足 ({args.interval}) バッチ: {len(symbols)}銘柄 "
                  f"/ {args.days}日")
            dfs = fetch_intraday_batch(symbols, days=args.days,
                                        interval=args.interval,
                                        use_cache=use_cache)
            print(f"\n{'銘柄':<10} {'本数':>8} {'営業日':>5}")
            print("-" * 30)
            for sym in symbols:
                if sym not in dfs:
                    print(f"  {sym:<10} {'---':>8}")
                    continue
                df = dfs[sym]
                nd = len(set(df.index.date))
                print(f"  {sym:<10} {len(df):>8} {nd:>5}")
        return

    # ── 日足モード (デフォルト) ────────────────────────────
    if len(symbols) == 1:
        sym = symbols[0]
        print(f"日足取得: {sym} ({yf_to_jquants(sym)}) / {args.days}日")
        df = fetch_daily(sym, days=args.days, use_cache=use_cache)
        if df is None or df.empty:
            print("データなし")
            return
        print(f"\n取得結果: {len(df)}本 "
              f"({df.index[0].date()} 〜 {df.index[-1].date()})")
        if args.summary:
            print(f"  終値: {df.iloc[-1]['close']:,.0f}")
            print(f"  最高値: {df['high'].max():,.0f}")
            print(f"  最安値: {df['low'].min():,.0f}")
        else:
            print()
            print(df.head(5).to_string())
            print("...")
            print(df.tail(5).to_string())
    else:
        print(f"日足バッチ: {len(symbols)}銘柄 / {args.days}日")
        dfs = fetch_daily_batch(symbols, days=args.days,
                                use_cache=use_cache)
        print(f"\n{'銘柄':<10} {'本数':>5} {'終値':>10}")
        print("-" * 30)
        for sym in symbols:
            if sym not in dfs:
                print(f"  {sym:<10} {'---':>5}")
                continue
            df = dfs[sym]
            print(f"  {sym:<10} {len(df):>5} "
                  f"{df.iloc[-1]['close']:>10,.0f}")


if __name__ == "__main__":
    main()
