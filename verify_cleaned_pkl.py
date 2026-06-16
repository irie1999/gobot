"""
verify_cleaned_pkl.py - クリーン後 pkl が yfinance と一致するか検証

【目的】
clean_pkl_outliers.py で破損バーを除去した pkl が、yfinance の
正しい値と一致するかを確認する。一致すれば、その pkl の全期間 (約26ヶ月)
を信頼できるデータとして使用可能。

【使い方】
  # 1銘柄を検証
  python verify_cleaned_pkl.py 2910.T

  # watchlist 27銘柄を一括検証
  python verify_cleaned_pkl.py --from-watchlist daytrade_winners_2026-06-16.py

  # 許容差を厳しくする (デフォルト 1.0円)
  python verify_cleaned_pkl.py 2910.T --tolerance 0.5
"""

from __future__ import annotations

import argparse
import importlib.util
import pickle
from pathlib import Path

import pandas as pd

from daytrade_data import _load_yfinance_batch, normalize_minute_df


def yf_to_jquants(yf_code: str) -> str:
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 4 and code.isdigit():
        return code + "0"
    return code


def load_pkl_normalized(path: Path) -> pd.DataFrame:
    """pkl を読み込んで正規化形式 (DatetimeIndex + ohlcv) で返す."""
    raw = pickle.loads(path.read_bytes())
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame()
    return normalize_minute_df(raw)


def compare_one(ticker: str, pkl_dir: Path, days: int = 60,
                tolerance: float = 1.0) -> dict:
    """1銘柄について pkl と yfinance を比較."""
    code5 = yf_to_jquants(ticker)
    pkl_path = pkl_dir / f"{code5}.pkl"
    if not pkl_path.exists():
        return {"ticker": ticker, "error": "pkl 不在"}

    # pkl 読込
    pkl_df = load_pkl_normalized(pkl_path)
    if pkl_df.empty:
        return {"ticker": ticker, "error": "pkl 空"}

    # yfinance 取得
    try:
        yf_result = _load_yfinance_batch([ticker], days, quiet=True)
    except Exception as e:
        return {"ticker": ticker, "error": f"yfinance fail: {e}"}
    if ticker not in yf_result:
        return {"ticker": ticker, "error": "yfinance 取得失敗"}
    yf_df = yf_result[ticker]

    # 共通 timestamp で比較
    common = pkl_df.index.intersection(yf_df.index)
    if len(common) == 0:
        return {
            "ticker": ticker,
            "error": "共通バーなし",
            "pkl_range": (pkl_df.index.min(), pkl_df.index.max()),
            "yf_range": (yf_df.index.min(), yf_df.index.max()),
        }

    pkl_sub = pkl_df.loc[common, "close"]
    yf_sub = yf_df.loc[common, "close"]

    diff = (pkl_sub - yf_sub).abs()
    n_match = (diff <= tolerance).sum()
    n_total = len(common)
    max_diff = float(diff.max())
    mean_diff = float(diff.mean())
    mismatch_pct = (n_total - n_match) / n_total * 100 if n_total else 0

    # 大きく差があるバー (上位5件)
    mismatches = []
    if max_diff > tolerance:
        bad = diff[diff > tolerance].sort_values(ascending=False).head(5)
        for ts, d in bad.items():
            mismatches.append({
                "time": ts.strftime("%Y-%m-%d %H:%M"),
                "pkl": float(pkl_sub.loc[ts]),
                "yf": float(yf_sub.loc[ts]),
                "diff": float(d),
            })

    return {
        "ticker": ticker,
        "pkl_total_bars": len(pkl_df),
        "yf_total_bars": len(yf_df),
        "common_bars": n_total,
        "match_bars": n_match,
        "mismatch_pct": mismatch_pct,
        "max_diff": max_diff,
        "mean_diff": mean_diff,
        "pkl_range": (pkl_df.index.min(), pkl_df.index.max()),
        "yf_range": (yf_df.index.min(), yf_df.index.max()),
        "mismatches": mismatches,
    }


def load_watchlist_symbols(wl_path: Path) -> list[str]:
    spec = importlib.util.spec_from_file_location("wl", wl_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = getattr(mod, "SYMBOLS", [])
    syms = set()
    for e in raw:
        if isinstance(e, tuple) and len(e) >= 1:
            syms.add(e[0])
    return sorted(syms)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="*", help="銘柄 (例: 2910.T)")
    p.add_argument("--from-watchlist", default=None,
                   help="watchlist .py から SYMBOLS 一括検証")
    p.add_argument("--pkl-dir", default="data/minute_5m",
                   help="pkl 格納ディレクトリ")
    p.add_argument("--days", type=int, default=60,
                   help="yfinance 取得日数 (デフォルト 60)")
    p.add_argument("--tolerance", type=float, default=1.0,
                   help="価格一致許容差 (円, デフォルト 1.0)")
    p.add_argument("--verbose", action="store_true",
                   help="不一致バーの詳細表示")
    args = p.parse_args()

    tickers = list(args.tickers)
    if args.from_watchlist:
        wl = Path(args.from_watchlist)
        if wl.exists():
            tickers.extend(load_watchlist_symbols(wl))
        else:
            print(f"[error] watchlist 不在: {wl}")
            return
    tickers = sorted(set(tickers))
    if not tickers:
        print("[error] 銘柄を指定してください")
        return

    pkl_dir = Path(args.pkl_dir)
    print(f"検証: {len(tickers)}銘柄 / 許容差 ±{args.tolerance}円 / "
          f"yfinance {args.days}日")
    print()

    perfect = []   # 100% 一致
    minor = []     # 95% 以上一致
    major = []     # 95% 未満一致
    errors = []

    for ticker in tickers:
        r = compare_one(ticker, pkl_dir, args.days, args.tolerance)
        if "error" in r:
            print(f"  [ERR] {ticker}: {r['error']}")
            errors.append(r)
            continue

        match_pct = r["match_bars"] / r["common_bars"] * 100
        if match_pct == 100:
            mark = "✓"
            perfect.append(r)
        elif match_pct >= 95:
            mark = "△"
            minor.append(r)
        else:
            mark = "✗"
            major.append(r)

        print(f"  {mark} {ticker}: pkl {r['pkl_total_bars']:>6,}本 / "
              f"共通 {r['common_bars']:>4,}本中 {r['match_bars']:>4,}本一致 "
              f"({match_pct:5.1f}%) / 最大差 {r['max_diff']:>6.1f}円")

        if args.verbose and r["mismatches"]:
            for m in r["mismatches"]:
                print(f"      {m['time']}  pkl={m['pkl']:.1f}  "
                      f"yf={m['yf']:.1f}  diff={m['diff']:.1f}")

    print()
    print(f"━━━ 結果サマリ ━━━")
    print(f"  ✓ 完全一致 (100%):    {len(perfect):>3}銘柄")
    print(f"  △ 軽微な差異 (≥95%):  {len(minor):>3}銘柄")
    print(f"  ✗ 重大な差異 (<95%):  {len(major):>3}銘柄")
    print(f"  ERR エラー:           {len(errors):>3}銘柄")
    print()

    if major:
        print(f"重大な差異がある銘柄 (再クリーンアップ推奨):")
        for r in major:
            print(f"  {r['ticker']}: 一致率 "
                  f"{r['match_bars']/r['common_bars']*100:.1f}%")
        print()

    if len(perfect) + len(minor) == len(tickers) - len(errors):
        print("✅ pkl は yfinance と一致 → 全期間バックテストに使用可能")
    elif len(major) > 0:
        print("⚠️ 一部 pkl に重大な差異あり → クリーンアップ実施を推奨:")
        print("   python clean_pkl_outliers.py --from-watchlist "
              "<watchlist> --apply")


if __name__ == "__main__":
    main()
