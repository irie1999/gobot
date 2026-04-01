"""
一括キャッシュ事前取得スクリプト
──────────────────────────────────────────────────────────────
backtest_macd_scan_v2 / backtest_stoch_atr_trail_v2 / rsi2_hv_v2 が共有する
.rsi2_cache/*.pkl を事前にまとめてダウンロード・保存する。

実行後は --signal スキャンがキャッシュから読み込むため大幅に高速化される。

■ 実行方法:
  python prefetch_cache.py                  # symbols_listed_prime.py があればそれを使用
  python prefetch_cache.py --universe 225   # 日経225のみ
  python prefetch_cache.py --workers 32     # 並列数を増やす（デフォルト: 16）
  python prefetch_cache.py --refresh        # 既存キャッシュを強制更新

■ キャッシュ保存先: .rsi2_cache/
■ 有効期限: 3日（土日祝を考慮）
"""

import argparse
import importlib.util
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

# ── キャッシュ設定 ────────────────────────────────────────────
CACHE_DIR   = Path(".rsi2_cache")
BUF_DAYS    = 230    # MA200 + 余裕（210行以上確保するため）
DL_PERIOD   = "2y"   # yfinance の period 指定（約500営業日 > BUF_DAYS）
WORKERS     = 4      # 並列数（yfinance はスレッドセーフでないため少なめに設定）


# ── 銘柄リスト取得 ────────────────────────────────────────────

def _load_symbols(universe: str | None) -> list[tuple[str, str]]:
    """universe 指定に応じて銘柄リストを返す。"""
    # 明示指定なし → symbols_listed_*.py を自動検出
    if universe is None:
        for candidate in ["symbols_listed_prime.py",
                          "symbols_listed_standard.py",
                          "symbols_listed_all.py"]:
            p = Path(candidate)
            if p.exists():
                spec = importlib.util.spec_from_file_location("_listed", p)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                print(f"  銘柄ユニバース: {candidate} ({len(mod.SYMBOLS)}銘柄)")
                return mod.SYMBOLS

    if universe in (None, "225"):
        from symbols_all import SYMBOLS as _S
        print(f"  銘柄ユニバース: 日経225 ({len(_S)}銘柄)")
        return _S

    # prime / standard / all
    fname = {
        "prime":    "symbols_listed_prime.py",
        "standard": "symbols_listed_standard.py",
        "all":      "symbols_listed_all.py",
    }[universe]
    p = Path(fname)
    if not p.exists():
        print(f"  エラー: {fname} が見つかりません。")
        print(f"  先に python fetch_listed_symbols.py --market {universe} を実行してください。")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("_listed", p)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    print(f"  銘柄ユニバース: {fname} ({len(mod.SYMBOLS)}銘柄)")
    return mod.SYMBOLS


# ── キャッシュ確認 ────────────────────────────────────────────

JST = timezone(timedelta(hours=9))


def _is_fresh(path: Path) -> bool:
    """キャッシュが今日（JST）にダウンロードされていれば新鮮と判定。
    yfinanceのデータ遅延（1〜2日）を考慮し、データの日付ではなく
    ファイルの更新日時で判断する。"""
    if not path.exists():
        return False
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=JST)
        if mtime.date() != datetime.now(JST).date():
            return False
        with open(path, "rb") as f:
            df = pickle.load(f)
        return len(df) >= 210
    except Exception:
        return False


# ── 1銘柄ダウンロード ─────────────────────────────────────────

def _fetch_one(symbol: str, name: str, refresh: bool) -> str:
    """1銘柄をダウンロードしてキャッシュ保存。戻り値はステータス文字列。"""
    cache_path = CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"

    if not refresh and _is_fresh(cache_path):
        return "skip"

    try:
        # Ticker.history() を使用（単一銘柄ダウンロードに適しており、並列でも安全）
        # period= はyfinanceサーバー基準で切り捨てが起こるため、明示的なstart/endを使用
        _now_jst = datetime.now(JST)
        dl_start = (_now_jst - timedelta(days=730)).strftime("%Y-%m-%d")
        dl_end   = (_now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
        ticker = yf.Ticker(symbol)
        raw = ticker.history(start=dl_start, end=dl_end, interval="1d", auto_adjust=False)
        if raw.empty:
            return "empty"
        # タイムゾーン付きインデックスをnaiveに変換
        if raw.index.tz is not None:
            raw.index = raw.index.tz_convert(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        # history() は 'dividends', 'stock splits' も返すので必要列だけ選択
        available = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
        if len(available) < 5:
            return "empty"
        raw = raw[available].dropna()

        if len(raw) < 210:
            return "short"

        # 価格の変動があるか検証（全て同じ値ならデータ異常）
        price_range = float(raw["close"].max() - raw["close"].min())
        if price_range < 0.01:
            return "error:no_price_variation"

        df = pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)

        with open(cache_path, "wb") as f:
            pickle.dump(df, f)
        return f"ok:{df.index[-1].date()}"

    except Exception as e:
        return f"error:{e}"


# ── メイン ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="バックテスト用キャッシュを一括取得")
    parser.add_argument("--universe", default=None,
                        choices=["225", "prime", "standard", "all"],
                        help="対象銘柄 (default: symbols_listed_*.py を自動検出、なければ225)")
    parser.add_argument("--workers", type=int, default=WORKERS,
                        help=f"並列ダウンロード数 (default: {WORKERS})")
    parser.add_argument("--refresh", action="store_true",
                        help="既存キャッシュを無視して強制再取得")
    args = parser.parse_args()

    symbols = _load_symbols(args.universe)
    total   = len(symbols)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # スキップ予定数を事前カウント
    skip_count = sum(
        1 for sym, _ in symbols
        if not args.refresh and _is_fresh(CACHE_DIR / f"{sym.replace('.', '_')}.pkl")
    )
    dl_count = total - skip_count

    print(f"\n  キャッシュ保存先: {CACHE_DIR.resolve()}")
    print(f"  取得対象: {dl_count}銘柄 / スキップ(キャッシュ済): {skip_count}銘柄")
    print(f"  並列数: {args.workers}")
    if args.refresh:
        print("  ※ --refresh: 既存キャッシュを強制更新")
    print()

    if dl_count == 0:
        print("  全銘柄のキャッシュが最新です。--signal をそのまま実行できます。")
        return

    # 並列ダウンロード
    results = {"ok": 0, "skip": 0, "empty": 0, "short": 0, "error": 0}
    latest_dates: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_fetch_one, sym, name, args.refresh): (sym, name)
            for sym, name in symbols
        }
        for future in as_completed(futures):
            sym, name = futures[future]
            status = future.result()

            if status.startswith("ok"):
                results["ok"] += 1
                # "ok:2026-04-01" 形式から日付を取得
                if ":" in status:
                    latest_dates.append(status.split(":", 1)[1])
            elif status == "skip":
                results["skip"] += 1
            elif status == "empty":
                results["empty"] += 1
            elif status == "short":
                results["short"] += 1
            else:
                results["error"] += 1

            done += 1
            if done % 50 == 0 or done == total:
                pct = done / total * 100
                bar = "█" * (done * 30 // total) + "░" * (30 - done * 30 // total)
                print(f"  [{bar}] {done:4d}/{total} ({pct:.0f}%)  "
                      f"保存:{results['ok']}  スキップ:{results['skip']}  "
                      f"失敗:{results['empty'] + results['short'] + results['error']}",
                      end="\r", flush=True)

    print()  # 改行
    print(f"\n  完了!")
    print(f"    新規保存  : {results['ok']}銘柄")
    if latest_dates:
        print(f"    最新日付  : {max(latest_dates)}（ダウンロード済み銘柄の最新終値日）")
    print(f"    スキップ  : {results['skip']}銘柄（キャッシュ済）")
    print(f"    データなし: {results['empty'] + results['short']}銘柄")
    print(f"    エラー    : {results['error']}銘柄")
    print(f"\n  次回の --signal スキャンはキャッシュから読み込むため高速に動作します。")


if __name__ == "__main__":
    main()
