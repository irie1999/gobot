"""
jquants_fetch.py  ―  J-Quants 高レベル取得ユーティリティ
==================================================================
既存の backtest_limit_entry.py:fetch() と同じ形式 (lowercase カラム /
tz-naive datetime index) の DataFrame を返す yfinance 互換インターフェース。

【主な機能】
  1. 銘柄コード変換      : "7203.T" ↔ "72030"
  2. 日足取得            : fetch_daily(symbol, days)
  3. バッチ日足取得      : fetch_daily_batch(symbols, days)
  4. ディスクキャッシュ  : ~/.jquants_cache/daily/<code>.pkl
                          当日のデータのみ再取得、過去データは再利用
  5. 分割調整           : AdjustmentOpen/High/Low/Close/Volume を使用

【使い方】
  from jquants_fetch import fetch_daily, fetch_daily_batch

  # 単一銘柄
  df = fetch_daily("7203.T", days=365)
  print(df.head())

  # 複数銘柄
  dfs = fetch_daily_batch(["7203.T", "9984.T", "8306.T"], days=90)
  for sym, df in dfs.items():
      print(sym, df.shape)

【CLI】
  python jquants_fetch.py 7203.T               # トヨタ1年
  python jquants_fetch.py 7203.T --days 90     # 90日
  python jquants_fetch.py 7203.T 9984.T 8306.T # 複数銘柄バッチ
  python jquants_fetch.py --watchlist          # DAYTRADE_SYMBOLS を全取得
"""

from __future__ import annotations

import argparse
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from jquants_client import JQuantsClient, JQuantsError

JST = timezone(timedelta(hours=9))
CACHE_ROOT = Path.home() / ".jquants_cache"
DAILY_CACHE_DIR = CACHE_ROOT / "daily"
DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────
# 銘柄コード変換 (yfinance ↔ J-Quants)
# ─────────────────────────────────────────────────────────────

def yf_to_jquants(yf_code: str) -> str:
    """
    "7203.T" → "72030"

    日本の東証上場株式は 2022年の市場再編以降 5桁コードに統一。
    ほとんどの銘柄は 4桁コード + チェックデジット "0"。
    すでに 5桁の場合はそのまま返す。
    """
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 5 and code.isdigit():
        return code
    if len(code) == 4 and code.isdigit():
        return code + "0"
    # それ以外 (英字含む etc.) はそのまま
    return code


def jquants_to_yf(jq_code: str) -> str:
    """
    "72030" → "7203.T"

    末尾の "0" (チェックデジット) を取り除いて ".T" を付加。
    """
    code = str(jq_code).strip()
    if len(code) == 5 and code.endswith("0") and code.isdigit():
        return code[:4] + ".T"
    return code + ".T"


# ─────────────────────────────────────────────────────────────
# DataFrame 正規化 (J-Quants → yfinance 互換)
# ─────────────────────────────────────────────────────────────

def _normalize_daily(df: pd.DataFrame, use_adjusted: bool = True) -> pd.DataFrame:
    """
    J-Quants daily_quotes レスポンスを
    既存の backtest コードが期待する形式に変換。

    既存形式 (backtest_limit_entry.py:fetch() 互換):
      - Index: DatetimeIndex (tz-naive)
      - Columns: [open, high, low, close, volume] (lowercase)

    V1 列名 (Open, High, Low, Close, Volume, AdjustmentClose) と
    V2 列名 (小文字バリアントや変形) の両方に対応する。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # Date カラム名のバリアントに対応
    date_col = None
    for cand in ["Date", "date", "tradingDate", "TradingDate"]:
        if cand in df.columns:
            date_col = cand
            break
    if date_col is None:
        return pd.DataFrame()
    df[date_col] = pd.to_datetime(df[date_col])

    # カラム名マッピング候補 (優先度順)
    # 調整済み列と未調整列の両バリアントを網羅
    candidates_adjusted = [
        ("AdjustmentOpen",   "AdjustmentHigh",   "AdjustmentLow",
         "AdjustmentClose",  "AdjustmentVolume"),
        ("adjustmentOpen",   "adjustmentHigh",   "adjustmentLow",
         "adjustmentClose",  "adjustmentVolume"),
        ("adj_open",         "adj_high",         "adj_low",
         "adj_close",        "adj_volume"),
    ]
    candidates_raw = [
        ("Open",   "High",   "Low",   "Close",   "Volume"),
        ("open",   "high",   "low",   "close",   "volume"),
    ]

    col_map: dict[str, str] | None = None
    if use_adjusted:
        for cols in candidates_adjusted:
            if all(c in df.columns for c in cols):
                col_map = dict(zip(cols, ["open", "high", "low", "close", "volume"]))
                break
    if col_map is None:
        for cols in candidates_raw:
            if all(c in df.columns for c in cols):
                col_map = dict(zip(cols, ["open", "high", "low", "close", "volume"]))
                break
    if col_map is None:
        # 最後の手段: close 1列があれば何とか使う
        close_col = next((c for c in df.columns
                          if c.lower() in ("close", "adjustmentclose", "adj_close")),
                         None)
        if close_col is None:
            return pd.DataFrame()
        out = pd.DataFrame({"close": pd.to_numeric(df[close_col], errors="coerce")},
                           index=df[date_col].values)
        out.index.name = "Date"
        return out.dropna(subset=["close"])

    out = pd.DataFrame({
        new: pd.to_numeric(df[old], errors="coerce")
        for old, new in col_map.items()
    }, index=df[date_col].values)
    out = out.dropna(subset=["close"])
    out.index.name = "Date"
    return out


# ─────────────────────────────────────────────────────────────
# キャッシュ
# ─────────────────────────────────────────────────────────────

def _cache_path(jq_code: str) -> Path:
    return DAILY_CACHE_DIR / f"{jq_code}.pkl"


def _load_cache(jq_code: str) -> pd.DataFrame | None:
    p = _cache_path(jq_code)
    if not p.exists():
        return None
    try:
        df = pickle.loads(p.read_bytes())
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    except Exception as e:
        print(f"  [cache] load error {jq_code}: {e}", file=sys.stderr)
    return None


def _save_cache(jq_code: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    try:
        _cache_path(jq_code).write_bytes(pickle.dumps(df))
    except Exception as e:
        print(f"  [cache] save error {jq_code}: {e}", file=sys.stderr)


def _cache_is_fresh(jq_code: str) -> bool:
    """当日の日付で既にキャッシュが更新されていれば fresh。"""
    p = _cache_path(jq_code)
    if not p.exists():
        return False
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=JST)
    return mtime.date() == datetime.now(JST).date()


# ─────────────────────────────────────────────────────────────
# 取得関数
# ─────────────────────────────────────────────────────────────

_CLIENT: JQuantsClient | None = None


def get_client() -> JQuantsClient:
    """プロセス内でクライアントをシングルトン化 (認証キャッシュ共有)。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = JQuantsClient()
    return _CLIENT


def fetch_daily(
    symbol: str,
    days: int = 365,
    use_cache: bool = True,
    use_adjusted: bool = True,
) -> pd.DataFrame | None:
    """
    指定銘柄の日足を取得。yfinance 互換の DataFrame を返す。

    Args:
        symbol     : "7203.T" または "72030"
        days       : 取得日数 (営業日ではなく暦日)
        use_cache  : True ならキャッシュを優先、当日分のみ差分取得
        use_adjusted: True なら AdjustmentClose 等を使用 (推奨)

    Returns:
        DataFrame with columns [open, high, low, close, volume]
        or None if fetch failed.
    """
    jq_code = yf_to_jquants(symbol)

    # キャッシュチェック
    if use_cache and _cache_is_fresh(jq_code):
        cached = _load_cache(jq_code)
        if cached is not None:
            df_norm = _normalize_daily(cached, use_adjusted=use_adjusted)
            # days 分に絞り込む
            cutoff = pd.Timestamp(datetime.now(JST).date() - timedelta(days=days))
            df_norm = df_norm[df_norm.index >= cutoff]
            return df_norm if not df_norm.empty else None

    # API から取得
    now = datetime.now(JST)
    from_d = (now - timedelta(days=days + 30)).strftime("%Y-%m-%d")  # バッファ +30日
    to_d = now.strftime("%Y-%m-%d")

    try:
        cli = get_client()
        raw = cli.get_daily_quotes(code=jq_code, from_date=from_d, to_date=to_d)
    except JQuantsError as e:
        print(f"  [err] {symbol} ({jq_code}): {e}", file=sys.stderr)
        return None

    if raw.empty:
        print(f"  [warn] {symbol}: データなし", file=sys.stderr)
        return None

    # キャッシュ保存 (生データのまま)
    if use_cache:
        _save_cache(jq_code, raw)

    # 正規化
    df_norm = _normalize_daily(raw, use_adjusted=use_adjusted)
    cutoff = pd.Timestamp(now.date() - timedelta(days=days))
    df_norm = df_norm[df_norm.index >= cutoff]
    return df_norm if not df_norm.empty else None


def fetch_daily_batch(
    symbols: list[str],
    days: int = 365,
    use_cache: bool = True,
    use_adjusted: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    複数銘柄の日足を取得。

    J-Quants の daily_quotes API は銘柄単位の取得だが、内部でシングルトン
    クライアントを使い認証を1回だけに抑える。
    並列化はしない (API の rate limit 保護のため)。

    Returns:
        {symbol: DataFrame} (yfinance 形式の symbol をキーとする)
    """
    result: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        df = fetch_daily(sym, days=days, use_cache=use_cache,
                         use_adjusted=use_adjusted)
        if df is not None and not df.empty:
            result[sym] = df
        print(f"  [{i:>3}/{total}] {sym}: "
              f"{len(df) if df is not None else 0}本", flush=True)
    print(f"  取得成功: {len(result)}/{total}銘柄", flush=True)
    return result


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def _format_head_tail(df: pd.DataFrame, n: int = 5) -> str:
    return "\n".join([
        df.head(n).to_string(),
        "...",
        df.tail(n).to_string(),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="J-Quants 高レベル日足取得ユーティリティ"
    )
    parser.add_argument("symbols", nargs="*", help="銘柄コード (例: 7203.T 9984.T)")
    parser.add_argument("--days", type=int, default=365, help="取得日数")
    parser.add_argument("--no-cache", action="store_true", help="キャッシュ無視")
    parser.add_argument("--no-adjust", action="store_true", help="分割調整なしで取得")
    parser.add_argument("--watchlist", action="store_true",
                        help="daytrade_symbols.DAYTRADE_SYMBOLS を全取得")
    parser.add_argument("--summary", action="store_true",
                        help="詳細表示せずサマリーのみ")
    args = parser.parse_args()

    if args.watchlist:
        try:
            from daytrade_symbols import DAYTRADE_SYMBOLS
        except ImportError:
            print("[ERROR] daytrade_symbols.py が見つかりません", file=sys.stderr)
            sys.exit(1)
        symbols = [s for s, _ in DAYTRADE_SYMBOLS]
    elif args.symbols:
        symbols = args.symbols
    else:
        parser.print_help()
        sys.exit(1)

    use_cache = not args.no_cache
    use_adjust = not args.no_adjust

    if len(symbols) == 1:
        sym = symbols[0]
        print(f"取得: {sym} ({yf_to_jquants(sym)}) / {args.days}日")
        df = fetch_daily(sym, days=args.days, use_cache=use_cache,
                         use_adjusted=use_adjust)
        if df is None or df.empty:
            print("データなし")
            return
        print(f"\n取得結果: {len(df)}本 "
              f"({df.index[0].date()} 〜 {df.index[-1].date()})")
        if args.summary:
            print(f"  始値:  {df.iloc[0]['open']:,.0f}")
            print(f"  終値:  {df.iloc[-1]['close']:,.0f}")
            print(f"  最高値: {df['high'].max():,.0f}")
            print(f"  最安値: {df['low'].min():,.0f}")
            print(f"  平均出来高: {df['volume'].mean():,.0f}")
        else:
            print()
            print(_format_head_tail(df))
        return

    # バッチ取得
    print(f"バッチ取得: {len(symbols)}銘柄 / {args.days}日")
    dfs = fetch_daily_batch(symbols, days=args.days,
                            use_cache=use_cache, use_adjusted=use_adjust)

    print()
    print(f"{'銘柄':<10} {'本数':>5} {'開始':<12} {'終了':<12} "
          f"{'最新終値':>10} {'平均出来高':>15}")
    print("-" * 72)
    for sym in symbols:
        if sym not in dfs:
            print(f"  {sym:<10} {'---':>5} (取得失敗)")
            continue
        df = dfs[sym]
        print(f"  {sym:<10} {len(df):>5} "
              f"{str(df.index[0].date()):<12} "
              f"{str(df.index[-1].date()):<12} "
              f"{df.iloc[-1]['close']:>10,.0f} "
              f"{df['volume'].mean():>15,.0f}")


if __name__ == "__main__":
    main()
