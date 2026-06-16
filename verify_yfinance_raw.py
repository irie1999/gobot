"""
verify_yfinance_raw.py - yfinance 生データ (auto_adjust=False) を確認

【目的】
yfinance_update.py を auto_adjust=False に変更した修正が正しいか、
実際にデータ取得して値を確認する。pkl 上書き前に必ず実行。

【使い方】
  # 2910 (ロック・フィールド) の最近60日を確認
  python verify_yfinance_raw.py 2910.T

  # 特定日を確認
  python verify_yfinance_raw.py 2910.T --date 2026-06-09

  # 27銘柄一括確認 (watchlist から)
  python verify_yfinance_raw.py --from-watchlist daytrade_winners_2026-06-16.py
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def fetch_raw(ticker: str, days: int = 60):
    """yfinance 生データ取得 (auto_adjust=False)."""
    import yfinance as yf
    df = yf.download(
        ticker, period=f"{days}d", interval="5m",
        auto_adjust=False, progress=False, threads=False,
    )
    if df is None or df.empty:
        return None
    # MultiIndex columns flatten
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    # tz → JST
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    return df


def fetch_adjusted(ticker: str, days: int = 60):
    """yfinance 調整済み (auto_adjust=True) も取得して比較."""
    import yfinance as yf
    df = yf.download(
        ticker, period=f"{days}d", interval="5m",
        auto_adjust=True, progress=False, threads=False,
    )
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    return df


def show_day(df, date_str: str, label: str):
    """指定日のバーを表示."""
    sub = df[df.index.strftime("%Y-%m-%d") == date_str]
    if sub.empty:
        print(f"  [{label}] {date_str}: バーなし")
        return None
    print(f"  [{label}] {date_str}: {len(sub)}本 / "
          f"範囲 {sub['Close'].min():.1f} 〜 {sub['Close'].max():.1f} / "
          f"中央値 {sub['Close'].median():.1f}")
    return sub


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
    p.add_argument("tickers", nargs="*",
                   help="銘柄コード (例: 2910.T)")
    p.add_argument("--from-watchlist", default=None,
                   help="watchlist .py から SYMBOLS 一括取得")
    p.add_argument("--date", default=None,
                   help="特定日のみ表示 (YYYY-MM-DD)")
    p.add_argument("--days", type=int, default=60,
                   help="取得日数 (デフォルト 60)")
    p.add_argument("--compare", action="store_true",
                   help="auto_adjust=True/False の両方取得して比較")
    p.add_argument("--save-sample", default=None,
                   help="生データを CSV に保存 (デバッグ用)")
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

    print(f"対象: {len(tickers)}銘柄 / days={args.days} / "
          f"compare={args.compare}")
    print()

    for ticker in tickers:
        print(f"━━━ {ticker} ━━━")
        try:
            raw = fetch_raw(ticker, args.days)
        except Exception as e:
            print(f"  [error] fetch fail: {e}")
            continue
        if raw is None:
            print(f"  [error] データなし")
            continue

        print(f"  raw (auto_adjust=False): {len(raw)}本 / "
              f"期間 {raw.index.min().strftime('%Y-%m-%d')} 〜 "
              f"{raw.index.max().strftime('%Y-%m-%d')}")
        print(f"  Close 全体範囲: {raw['Close'].min():.1f} 〜 "
              f"{raw['Close'].max():.1f}")

        if args.date:
            sub = show_day(raw, args.date, "raw")
            if sub is not None and len(sub) > 0:
                # 先頭5本詳細
                print(f"     先頭5本:")
                for ts, row in sub.head(5).iterrows():
                    print(f"       {ts.strftime('%H:%M')} O={row['Open']:.1f} "
                          f"H={row['High']:.1f} L={row['Low']:.1f} "
                          f"C={row['Close']:.1f}")

        if args.compare:
            try:
                adj = fetch_adjusted(ticker, args.days)
                if adj is not None:
                    print(f"  adj (auto_adjust=True):  {len(adj)}本 / "
                          f"範囲 {adj['Close'].min():.1f} 〜 "
                          f"{adj['Close'].max():.1f}")
                    if args.date:
                        show_day(adj, args.date, "adj")
                    # 差分検出
                    common_idx = raw.index.intersection(adj.index)
                    if len(common_idx) > 0:
                        diff = (raw.loc[common_idx, "Close"] -
                                adj.loc[common_idx, "Close"]).abs()
                        max_diff = diff.max()
                        if max_diff > 0.5:
                            print(f"  ⚠️ raw と adj に差分あり: "
                                  f"最大 {max_diff:.1f}円 (調整発生)")
                            # 差分のある日付
                            diff_dates = raw.loc[
                                common_idx[diff > 0.5]
                            ].index.strftime("%Y-%m-%d").unique()
                            print(f"     差分日: {list(diff_dates)[:10]}")
                        else:
                            print(f"  ✓ raw と adj はほぼ一致 "
                                  f"(最大差 {max_diff:.1f}円)")
            except Exception as e:
                print(f"  [warn] adjusted 取得失敗: {e}")

        if args.save_sample:
            out = Path(args.save_sample)
            out.parent.mkdir(parents=True, exist_ok=True)
            raw.to_csv(out)
            print(f"  CSV保存: {out}")

        print()


if __name__ == "__main__":
    main()
