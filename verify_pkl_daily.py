"""
verify_pkl_daily.py - pkl の正当性を yfinance 日足で検証
============================================================

【発想】
yfinance 5分足は60日制限だが、日足 (interval='1d') は2年分取得可能。
pkl の intraday 5分足を日次集計して、yfinance 日足の OHLC と比較すれば、
全期間 (26ヶ月) の正当性を検証できる。

【判定方法】
- pkl 内の各日のバーから daily OHLC を計算 (open=first, high=max, low=min, close=last)
- yfinance 日足から OHLC 取得
- 日次 High, Low, Close を比較
- 差が許容範囲を超えた日を「破損日」とマーク

【使い方】
  # 1銘柄を検証
  python verify_pkl_daily.py 2910.T

  # 全 watchlist 検証
  python verify_pkl_daily.py --from-watchlist daytrade_winners_2026-06-16.py

  # 破損日の詳細表示
  python verify_pkl_daily.py 2910.T --verbose

  # 許容差を厳しくする (デフォルト 2.0%)
  python verify_pkl_daily.py 2910.T --tolerance-pct 1.0
"""

from __future__ import annotations

import argparse
import importlib.util
import pickle
from pathlib import Path

import pandas as pd

from daytrade_data import normalize_minute_df


def yf_to_jquants(yf_code: str) -> str:
    code = yf_code.strip().upper().replace(".T", "")
    if len(code) == 4 and code.isdigit():
        return code + "0"
    return code


def fetch_yfinance_daily(ticker: str, period: str = "2y") -> pd.DataFrame:
    """yfinance 日足取得 (auto_adjust=False で生値)."""
    import yfinance as yf
    df = yf.download(
        ticker, period=period, interval="1d",
        auto_adjust=False, progress=False, threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    # Index を date 型に
    df.index = pd.to_datetime(df.index).date
    return df


def load_pkl_normalized(path: Path) -> pd.DataFrame:
    raw = pickle.loads(path.read_bytes())
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame()
    return normalize_minute_df(raw)


def aggregate_to_daily(intraday_df: pd.DataFrame) -> pd.DataFrame:
    """5分足 → 日足 (open=first, high=max, low=min, close=last)."""
    if intraday_df.empty:
        return pd.DataFrame()
    df = intraday_df.copy()
    df["date"] = df.index.date
    return df.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )


def compare_one(ticker: str, pkl_dir: Path,
                tolerance_pct: float = 2.0) -> dict:
    """1銘柄の pkl 日次集計 vs yfinance 日足を比較."""
    code5 = yf_to_jquants(ticker)
    pkl_path = pkl_dir / f"{code5}.pkl"
    if not pkl_path.exists():
        return {"ticker": ticker, "error": "pkl 不在"}

    pkl_df = load_pkl_normalized(pkl_path)
    if pkl_df.empty:
        return {"ticker": ticker, "error": "pkl 空"}

    pkl_daily = aggregate_to_daily(pkl_df)
    if pkl_daily.empty:
        return {"ticker": ticker, "error": "日次集計失敗"}

    try:
        yf_daily = fetch_yfinance_daily(ticker, "2y")
    except Exception as e:
        return {"ticker": ticker, "error": f"yfinance fail: {e}"}
    if yf_daily.empty:
        return {"ticker": ticker, "error": "yfinance 日足取得失敗"}

    yf_daily.columns = [c.lower() for c in yf_daily.columns]

    # 共通日付
    common_dates = sorted(set(pkl_daily.index) & set(yf_daily.index))
    if not common_dates:
        return {"ticker": ticker, "error": "共通日なし"}

    bad_days = []
    for date in common_dates:
        p = pkl_daily.loc[date]
        y = yf_daily.loc[date]
        y_close = float(y["close"])
        if y_close <= 0:
            continue
        tol = y_close * tolerance_pct / 100.0
        # High が yfinance より高すぎる or Low が低すぎる → 破損
        high_diff = float(p["high"]) - float(y["high"])
        low_diff = float(y["low"]) - float(p["low"])
        close_diff = abs(float(p["close"]) - y_close)
        if high_diff > tol or low_diff > tol or close_diff > tol:
            bad_days.append({
                "date": date,
                "pkl": {"O": float(p["open"]), "H": float(p["high"]),
                        "L": float(p["low"]), "C": float(p["close"])},
                "yf":  {"O": float(y["open"]), "H": float(y["high"]),
                        "L": float(y["low"]), "C": y_close},
                "high_diff": high_diff,
                "low_diff": low_diff,
                "close_diff": close_diff,
                "tol": tol,
            })

    n_total = len(common_dates)
    n_bad = len(bad_days)
    match_pct = (n_total - n_bad) / n_total * 100 if n_total else 0

    return {
        "ticker": ticker,
        "pkl_days": len(pkl_daily),
        "yf_days": len(yf_daily),
        "common_days": n_total,
        "bad_days_count": n_bad,
        "match_pct": match_pct,
        "bad_days": bad_days,
        "pkl_range": (min(pkl_daily.index), max(pkl_daily.index)),
        "yf_range": (min(yf_daily.index), max(yf_daily.index)),
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
    p.add_argument("--tolerance-pct", type=float, default=2.0,
                   help="許容差 (パーセント、デフォルト 2.0)")
    p.add_argument("--verbose", action="store_true",
                   help="破損日の詳細表示")
    p.add_argument("--max-bad-shown", type=int, default=10,
                   help="表示する破損日の最大数")
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
    print(f"検証: {len(tickers)}銘柄 / 許容差 ±{args.tolerance_pct}%")
    print()

    clean = []     # 100% 一致
    minor = []     # 99% 以上
    major = []     # 99% 未満
    errors = []

    for ticker in tickers:
        r = compare_one(ticker, pkl_dir, args.tolerance_pct)
        if "error" in r:
            print(f"  [ERR] {ticker}: {r['error']}")
            errors.append(r)
            continue

        mark = "✓" if r["match_pct"] == 100 else (
            "△" if r["match_pct"] >= 99 else "✗")
        if r["match_pct"] == 100:
            clean.append(r)
        elif r["match_pct"] >= 99:
            minor.append(r)
        else:
            major.append(r)

        print(f"  {mark} {ticker}: 共通 {r['common_days']:>4}日中 "
              f"破損 {r['bad_days_count']:>3}日 "
              f"({r['match_pct']:5.1f}% 一致)")

        if args.verbose and r["bad_days"]:
            for bd in r["bad_days"][:args.max_bad_shown]:
                print(f"      {bd['date']}  "
                      f"pkl[H={bd['pkl']['H']:.0f} L={bd['pkl']['L']:.0f} "
                      f"C={bd['pkl']['C']:.0f}]  "
                      f"yf[H={bd['yf']['H']:.0f} L={bd['yf']['L']:.0f} "
                      f"C={bd['yf']['C']:.0f}]  "
                      f"diff(H={bd['high_diff']:+.0f} L={bd['low_diff']:+.0f})")
            if len(r["bad_days"]) > args.max_bad_shown:
                print(f"      ... 他 {len(r['bad_days']) - args.max_bad_shown}日")

    print()
    print(f"━━━ 結果サマリ ━━━")
    print(f"  ✓ クリーン (100% 一致):  {len(clean):>3}銘柄")
    print(f"  △ 軽微 (≥99% 一致):      {len(minor):>3}銘柄")
    print(f"  ✗ 重大 (<99% 一致):       {len(major):>3}銘柄")
    print(f"  ERR エラー:               {len(errors):>3}銘柄")
    print()

    if major or minor:
        affected = major + minor
        print(f"破損日のある銘柄:")
        for r in sorted(affected, key=lambda x: x["match_pct"]):
            print(f"  {r['ticker']}: 破損 {r['bad_days_count']}日 "
                  f"({r['match_pct']:.1f}% 一致)")
        print()
        print("対処: python clean_pkl_outliers.py "
              "--from-watchlist <watchlist> --apply")

    if len(clean) == len(tickers) - len(errors):
        print("✅ 全 pkl が yfinance 日足と一致 → 全期間バックテスト OK")


if __name__ == "__main__":
    main()
