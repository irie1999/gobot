#!/usr/bin/env python3
"""
日経225 日足データ 一括ダウンロード・更新スクリプト

使い方:
  python fetch_all.py              # 全銘柄ダウンロード（デフォルト5年分）
  python fetch_all.py --years 10   # 10年分
  python fetch_all.py --force      # 最新キャッシュも強制更新

初回は全銘柄をダウンロードして .rsi2_cache/{symbol}.pkl に保存します。
2回目以降は前回の続き（差分）のみ取得して追記します。
ネットワークエラーは自動リトライ（最大3回）します。
rsi2.py は自動的にこのキャッシュを優先使用します。
"""

import argparse
import pickle
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from rsi2 import SYMBOLS, _CACHE_DIR

WORKERS      = 1    # 並列数（1=直列: yfinance の内部キャッシュ競合を防ぐ）
MAX_RETRY    = 3    # ダウンロード失敗時のリトライ回数
RETRY_DELAY  = 3    # リトライ間隔（秒）
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


# ── ダウンロード（リトライあり） ────────────────────────────

def _normalize(raw: pd.DataFrame) -> pd.DataFrame | None:
    """列名正規化・重複除去・列選択を行う共通処理。"""
    raw.columns = [str(c).lower() for c in raw.columns]
    raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]  # 重複列を削除
    need = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
    if len(need) < 5:
        return None
    raw = raw[need].dropna()
    return raw if not raw.empty else None


def _download(symbol: str, dl_start: str, dl_end: str) -> pd.DataFrame | None:
    """yfinanceでダウンロード。失敗時は最大 MAX_RETRY 回リトライ。"""
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            raw = yf.download(symbol, start=dl_start, end=dl_end,
                              interval="1d", auto_adjust=False, progress=False,
                              multi_level_index=False)
            if not raw.empty:
                raw = _normalize(raw)
                if raw is not None:
                    return raw
            last_err = "empty"
        except Exception as e:
            last_err = str(e)
        if attempt < MAX_RETRY:
            time.sleep(RETRY_DELAY * attempt)
    return None  # 全リトライ失敗


# ── 1銘柄の更新処理 ─────────────────────────────────────────

def _detect_split(existing: pd.DataFrame, new_raw: pd.DataFrame,
                  threshold: float = 0.15) -> bool:
    """キャッシュ末尾と新規データ先頭の価格乖離から株式分割を検知する。
    threshold: 15%以上乖離していれば分割とみなす（配当・通常変動と区別）
    """
    if existing is None or existing.empty or new_raw is None or new_raw.empty:
        return False
    # 重複する日付がある場合は比較できないのでスキップ
    overlap = existing.index.intersection(new_raw.index)
    if overlap.empty:
        return False
    old_close = float(existing.loc[overlap[-1], "close"])
    new_close = float(new_raw.loc[overlap[-1], "close"])
    if old_close <= 0 or new_close <= 0:
        return False
    ratio = max(old_close, new_close) / min(old_close, new_close)
    return ratio > (1 + threshold)



def _update(symbol: str, years: int, force: bool) -> tuple[str, str]:
    existing = _load(symbol)

    if existing is not None and not existing.empty:
        last_date = existing.index[-1]
        # 直近7日以内（土日・祝日考慮）なら最新とみなしてスキップ
        if not force and last_date >= _TODAY - timedelta(days=7):
            return symbol, f"最新 ({last_date.strftime('%Y-%m-%d')} 計{len(existing)}行)"
        # 差分のみ取得（重複1日分含めて分割検知に使う）
        dl_start = last_date.strftime("%Y-%m-%d")
        mode = "差分"
    else:
        # 初回: 指定年数分を取得
        dl_start = (_TODAY - timedelta(days=years * 365 + 30)).strftime("%Y-%m-%d")
        mode = "新規"

    dl_end = (_TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    raw = _download(symbol, dl_start, dl_end)

    if raw is None:
        if existing is not None and not existing.empty:
            return symbol, f"{mode}（新データなし）"
        return symbol, "エラー: ダウンロード失敗"

    # ── 株式分割検知: 価格が15%以上乖離 → 全件再取得 ──────────
    if mode == "差分" and _detect_split(existing, raw):
        full_start = (_TODAY - timedelta(days=years * 365 + 30)).strftime("%Y-%m-%d")
        raw = _download(symbol, full_start, dl_end)
        if raw is None:
            return symbol, "エラー: 分割検知後の再取得失敗"
        mode = "分割再取得"
        existing = None  # キャッシュを全て置き換える

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


# ── メイン ──────────────────────────────────────────────────

def _run(targets: list[tuple[str, str]], years: int, force: bool,
         total: int, done_offset: int) -> tuple[int, int, int, list]:
    """targets を並列ダウンロードして (ok, skip, err, results) を返す。"""
    done = done_offset
    ok = skip = err = 0
    results = []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_update, sym, years, force): (sym, name)
                for sym, name in targets}
        for fut in as_completed(futs):
            sym, name = futs[fut]
            _, status = fut.result()
            done += 1

            if "エラー" in status:
                err += 1
                icon = "✗"
            elif "最新" in status or "新データなし" in status:
                skip += 1
                icon = "–"
            else:
                ok += 1
                icon = "✓"

            results.append((icon, sym, name, status))
            print(f"  [{done:3d}/{total}] {icon} {sym:8s}  {name[:14]:14s}  {status}")

    return ok, skip, err, results


def main() -> None:
    ap = argparse.ArgumentParser(
        description="日経225 日足データ 一括ダウンロード・更新")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS,
                    help=f"初回ダウンロード期間（年）。デフォルト: {DEFAULT_YEARS}")
    ap.add_argument("--force", action="store_true",
                    help="最新キャッシュも強制更新する")
    args = ap.parse_args()

    _CACHE_DIR.mkdir(exist_ok=True)

    # 既存キャッシュを全削除してから新規ダウンロード
    existing_files = list(_CACHE_DIR.glob("*.pkl"))
    if existing_files:
        for f in existing_files:
            f.unlink()
        print(f"既存キャッシュ削除: {len(existing_files)}ファイル")

    print(f"日経225 日足データ ダウンロード  {len(SYMBOLS)}銘柄 / {args.years}年分")
    print(f"保存先: {_CACHE_DIR.resolve()}")
    print(f"並列数: {WORKERS}  リトライ: {MAX_RETRY}回\n")

    # ── 第1ラウンド ─────────────────────────────────────────
    ok, skip, err, results = _run(SYMBOLS, args.years, args.force,
                                  total=len(SYMBOLS), done_offset=0)

    # ── エラー銘柄を再試行（最大2ラウンド追加） ───────────────
    for round_n in range(2, 4):
        failed = [(sym, name) for icon, sym, name, _ in results if icon == "✗"]
        if not failed:
            break
        print(f"\n  ─── リトライ {round_n - 1}回目: {len(failed)}銘柄 ───")
        time.sleep(5)
        r_ok, r_skip, r_err, r_results = _run(
            failed, args.years, args.force,
            total=len(failed), done_offset=0)
        ok   += r_ok
        skip += r_skip
        err   = r_err  # 最終ラウンドのエラー数で上書き
        # resultsのエラー分を更新結果で置き換え
        retry_map = {sym: (icon, status) for icon, sym, _, status in r_results}
        results = [(icon if sym not in retry_map else retry_map[sym][0],
                    sym, name,
                    status if sym not in retry_map else retry_map[sym][1])
                   for icon, sym, name, status in results]

    # ── 最終サマリー ────────────────────────────────────────
    print()
    print(f"完了  更新: {ok}銘柄  スキップ: {skip}銘柄  エラー: {err}銘柄")

    final_errors = [(sym, name, status) for icon, sym, name, status in results
                    if icon == "✗"]
    if final_errors:
        print(f"\n未取得銘柄 ({len(final_errors)}銘柄):")
        for sym, name, status in final_errors:
            print(f"  {sym}  {name}  {status}")


if __name__ == "__main__":
    main()
