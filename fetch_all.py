#!/usr/bin/env python3
"""
日経225 日足データ 一括ダウンロード・更新スクリプト

使い方:
  python fetch_all.py              # 全銘柄ダウンロード（デフォルト5年分）
  python fetch_all.py --years 10   # 10年分
  python fetch_all.py --update     # 差分のみ更新（既存データの続きを取得）

初回は全銘柄をダウンロードして .rsi2_cache/{symbol}.pkl に保存します。
2回目以降は前回の続き（差分）のみ取得して追記します。
rsi2.py は自動的にこのキャッシュを優先使用します。
"""

import argparse
import pickle
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from rsi2 import SYMBOLS, _CACHE_DIR

WORKERS = 16
DEFAULT_YEARS = 5
_TODAY = pd.Timestamp(datetime.now().date())


# ── キャッシュ操作 ──────────────────────────────────────────

def _path(symbol: str) -> Path:
    return _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"


def _load(symbol: str) -> pd.DataFrame | None:
    p = _path(symbol)
    if p.exists():
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            p.unlink(missing_ok=True)
    return None


def _save(symbol: str, df: pd.DataFrame) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    with open(_path(symbol), "wb") as f:
        pickle.dump(df, f)


# ── 1銘柄の更新処理 ─────────────────────────────────────────

def _update(symbol: str, years: int, force: bool) -> tuple[str, str]:
    existing = _load(symbol)

    if existing is not None and not existing.empty:
        last_date = existing.index[-1]
        # 直近7日以内（土日・祝日考慮）なら最新とみなしてスキップ
        if not force and last_date >= _TODAY - timedelta(days=7):
            return symbol, f"最新 ({last_date.strftime('%Y-%m-%d')} 計{len(existing)}行)"
        # 差分のみ取得
        dl_start = (last_date + timedelta(days=1)).strftime("%Y-%m-%d")
        mode = "差分"
    else:
        # 初回: 指定年数分を取得
        dl_start = (_TODAY - timedelta(days=years * 365 + 30)).strftime("%Y-%m-%d")
        mode = "新規"

    dl_end = (_TODAY + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        raw = yf.download(symbol, start=dl_start, end=dl_end,
                          interval="1d", auto_adjust=True, progress=False)
        if raw.empty:
            if existing is not None:
                return symbol, f"{mode}（新データなし）"
            return symbol, "データなし"

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw[["open", "high", "low", "close", "volume"]].dropna()

        new_df = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)

        if existing is not None and not existing.empty:
            combined = pd.concat([existing, new_df])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.sort_index(inplace=True)
        else:
            combined = new_df

        _save(symbol, combined)
        return symbol, f"{mode} +{len(new_df)}行 → 計{len(combined)}行"

    except Exception as e:
        return symbol, f"エラー: {e}"


# ── メイン ──────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="日経225 日足データ 一括ダウンロード・更新")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS,
                    help=f"初回ダウンロード期間（年）。デフォルト: {DEFAULT_YEARS}")
    ap.add_argument("--force", action="store_true",
                    help="最新キャッシュも強制更新する")
    args = ap.parse_args()

    _CACHE_DIR.mkdir(exist_ok=True)
    print(f"日経225 日足データ ダウンロード  {len(SYMBOLS)}銘柄 / {args.years}年分")
    print(f"保存先: {_CACHE_DIR.resolve()}\n")

    done = ok = skip = err = 0
    results: list[tuple[str, str, str]] = []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_update, sym, args.years, args.force): (sym, name)
                for sym, name in SYMBOLS}
        for fut in as_completed(futs):
            sym, name = futs[fut]
            _, status = fut.result()
            done += 1

            if "エラー" in status or status == "データなし":
                err += 1
                icon = "✗"
            elif "最新" in status:
                skip += 1
                icon = "–"
            else:
                ok += 1
                icon = "✓"

            results.append((icon, sym, name, status))
            print(f"  [{done:3d}/{len(SYMBOLS)}] {icon} {sym:8s}  {name[:14]:14s}  {status}")

    print()
    print(f"完了  更新: {ok}銘柄  スキップ: {skip}銘柄  エラー: {err}銘柄")
    if err:
        print("\nエラー銘柄:")
        for icon, sym, name, status in results:
            if icon == "✗":
                print(f"  {sym}  {name}  {status}")


if __name__ == "__main__":
    main()
