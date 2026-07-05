"""
daily_fetch_minute_yf.py  ―  yfinance で5分足を取得し minute_5m を更新 (J-Quants不要)
==================================================================
daily_fetch_minute.py は J-Quants API キーが必要だが、これは yfinance から
直近5分足を取得して data/minute_5m/*.pkl を更新する (APIキー不要)。

制約:
  - yfinance の5分足は『直近60日まで』(Yahoo の仕様)。それ以前は quarantine_5m
    (J-Quants 長期) が _load_local_full のマージで補完する。
  - minute_5m を yfinance 単一ソースに置き換える (内部スケール段差を避けるため)。
    quarantine との境界スケールは既存の _scaled_pre_minute が吸収する。
  - 上書き後、minute_5m の mtime が新しくなるので正規化キャッシュは自動再生成。

対象銘柄:
  既定 = daytrade_watchlist.py の WATCHLIST 全銘柄。
  --all  = data/minute_5m に既存の全銘柄。
  --symbols 7203.T 6758.T ... = 明示指定。

使い方:
  python daily_fetch_minute_yf.py                 # WATCHLIST を直近60日で更新
  python daily_fetch_minute_yf.py --days 60 --all
  python daily_fetch_minute_yf.py --symbols 6141.T 1982.T
"""
from __future__ import annotations

import argparse
import importlib
import pickle
import sys
from pathlib import Path

from daytrade_data import (_load_yfinance_batch, yf_to_jquants, jq_to_yf,
                           DATA_DIR)


def _watchlist_symbols() -> list[str]:
    try:
        wl = importlib.import_module("daytrade_watchlist").WATCHLIST
    except Exception:
        return []
    syms: list[str] = []
    for _strat, lst in wl.items():
        for code, _name in lst:
            s = code if str(code).endswith(".T") else f"{code}.T"
            if s not in syms:
                syms.append(s)
    return syms


def _existing_symbols() -> list[str]:
    return sorted(jq_to_yf(p.stem) for p in DATA_DIR.glob("*.pkl"))


def main():
    ap = argparse.ArgumentParser(description="yfinanceで5分足minute_5mを更新")
    ap.add_argument("--days", type=int, default=60,
                    help="取得日数 (yfinance上限60)")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--all", action="store_true",
                    help="minute_5m に既存の全銘柄を更新")
    ap.add_argument("--batch", type=int, default=20, help="yfinance一括取得数")
    args = ap.parse_args()

    if args.symbols:
        syms = [s if str(s).endswith(".T") else f"{s}.T" for s in args.symbols]
    elif args.all:
        syms = _existing_symbols()
    else:
        syms = _watchlist_symbols()
        if not syms:
            print("[ERROR] daytrade_watchlist.py が無いか空です。"
                  "--symbols か --all を使ってください。", file=sys.stderr)
            sys.exit(1)

    days = min(args.days, 60)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"yfinance 5分足更新: {len(syms)}銘柄 / 直近{days}日 → {DATA_DIR}", flush=True)

    ok = 0
    fail = []
    for i in range(0, len(syms), args.batch):
        chunk = syms[i:i + args.batch]
        batch = _load_yfinance_batch(chunk, days)
        for sym in chunk:
            df = batch.get(sym)
            if df is None or df.empty:
                fail.append(sym)
                continue
            jq = yf_to_jquants(sym)
            out = DATA_DIR / f"{jq}.pkl"
            try:
                # DatetimeIndex + 小文字OHLCV。normalize_minute_df が再読込で対応。
                out.write_bytes(pickle.dumps(df))
                ok += 1
                d0 = df.index[0].date()
                d1 = df.index[-1].date()
                print(f"  {sym:<9} {len(df):>5}本  {d0} 〜 {d1}", flush=True)
            except Exception as e:
                print(f"  {sym:<9} 保存失敗: {e}", file=sys.stderr)
                fail.append(sym)
        print(f"  ...{min(i + args.batch, len(syms))}/{len(syms)}", flush=True)

    print(f"\n完了: {ok}銘柄更新 / 失敗{len(fail)}銘柄", flush=True)
    if fail:
        print(f"  失敗: {', '.join(fail[:20])}{' ...' if len(fail) > 20 else ''}")
    print("→ daytrade_report.py / verify_*.py を再実行すれば直近まで反映されます。")


if __name__ == "__main__":
    main()
