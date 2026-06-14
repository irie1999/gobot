"""
fetch_missing_prime.py  ―  pkl 未取得の prime 銘柄を新規取得
==================================================================
symbols_listed_all.py に含まれるが data/minute_5m に pkl がない銘柄について、
yfinance から最大60日分の5分足を取得して新規 pkl を作成する。

【目的】
prime 1552銘柄のうち pkl 未存在の約664銘柄を補完して、
winners 抽出の母集団を拡大する。

【使い方】
  python fetch_missing_prime.py                # 全未取得銘柄
  python fetch_missing_prime.py --limit 50     # 50銘柄だけテスト
  python fetch_missing_prime.py --workers 5    # 並列5 (デフォルト3)
  python fetch_missing_prime.py --list-only    # 未取得リスト表示のみ
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).resolve().parent / "data" / "minute_5m"


def _yf_to_jq(yf_code: str) -> str:
    """7203.T → 72030"""
    code = yf_code.replace(".T", "").strip()
    if len(code) == 4 and code.isdigit():
        return code + "0"
    if len(code) == 4 and not code.isdigit():
        # "171A" のようなコード → "171A0"
        return code + "0"
    return code


def yf_df_to_jquants(df, code5):
    """yfinance形式 → J-Quants V2形式 (yfinance_update.py と同じ)"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [c[0] if isinstance(c, tuple) else c for c in out.columns]
    if out.columns.duplicated().any():
        out = out.loc[:, ~out.columns.duplicated()]
    if out.index.tz is not None:
        out.index = out.index.tz_convert("Asia/Tokyo").tz_localize(None)

    out["DateTime"] = out.index
    out["Date"] = out.index.strftime("%Y-%m-%d")
    out["Time"] = out.index.strftime("%H:%M")
    out["Code"] = code5

    rename = {}
    for src, dst in [("Open", "O"), ("High", "H"), ("Low", "L"),
                     ("Close", "C"), ("Volume", "Vo")]:
        if src in out.columns:
            rename[src] = dst
        elif src.lower() in out.columns:
            rename[src.lower()] = dst
    out = out.rename(columns=rename)
    if "O" in out.columns and "Vo" in out.columns and "Va" not in out.columns:
        out["Va"] = pd.to_numeric(out["O"], errors="coerce") * \
                    pd.to_numeric(out["Vo"], errors="coerce")

    cols = ["DateTime", "Date", "Time", "Code", "O", "H", "L", "C", "Vo", "Va"]
    cols = [c for c in cols if c in out.columns]
    out = out[cols].reset_index(drop=True)

    for c in ["O", "H", "L", "C", "Vo", "Va"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "C" in out.columns:
        out = out.dropna(subset=["C"])
    return out


def fetch_one(yf_code, days):
    """1銘柄を新規取得して pkl 作成。"""
    import yfinance as yf
    code5 = _yf_to_jq(yf_code)
    pkl_path = DATA_DIR / f"{code5}.pkl"
    try:
        df = yf.download(yf_code, period=f"{days}d", interval="5m",
                         progress=False, threads=False, auto_adjust=True)  # 株式分割対応
        if df is None or df.empty:
            return ("err", 0, yf_code, "yfinance empty")
        conv = yf_df_to_jquants(df, code5)
        if conv.empty:
            return ("err", 0, yf_code, "convert empty")
        pkl_path.write_bytes(pickle.dumps(conv))
        return ("ok", len(conv), yf_code, "")
    except Exception as e:
        return ("err", 0, yf_code, f"{type(e).__name__}: {str(e)[:60]}")


def main():
    parser = argparse.ArgumentParser(description="未取得 prime 銘柄を新規取得")
    parser.add_argument("--days", type=int, default=60,
                        help="取得日数 (1-60、yfinance上限)")
    parser.add_argument("--workers", type=int, default=3,
                        help="並列スレッド数 (デフォルト3)")
    parser.add_argument("--limit", type=int, default=0,
                        help="処理銘柄数の上限 (テスト用)")
    parser.add_argument("--list-only", action="store_true",
                        help="未取得銘柄をリスト表示するだけ (取得しない)")
    args = parser.parse_args()

    if args.days > 60:
        args.days = 60

    # prime universe 読み込み
    try:
        from symbols_listed_all import SYMBOLS as PRIME
    except ImportError:
        print("[error] symbols_listed_all.py がありません")
        sys.exit(1)

    # 未取得銘柄を特定
    missing = []
    existing = 0
    for yf_code, name in PRIME:
        code5 = _yf_to_jq(yf_code)
        pkl = DATA_DIR / f"{code5}.pkl"
        if pkl.exists():
            existing += 1
        else:
            missing.append((yf_code, name))

    print(f"prime universe: {len(PRIME)}銘柄")
    print(f"  既存pkl: {existing}銘柄")
    print(f"  未取得 : {len(missing)}銘柄")

    if args.list_only:
        print(f"\n【未取得銘柄リスト】")
        for yf_code, name in missing[:50]:
            print(f"  {yf_code:10} {name}")
        if len(missing) > 50:
            print(f"  ... 他 {len(missing)-50}銘柄")
        return

    if not missing:
        print("\n全銘柄取得済み")
        return

    if args.limit > 0:
        missing = missing[:args.limit]
        print(f"テスト制限: {args.limit}銘柄")

    print(f"\nyfinance 新規取得開始 (days={args.days}, workers={args.workers})")
    print(f"目安時間: {len(missing) // args.workers // 30}〜"
          f"{len(missing) // args.workers // 15}分")
    print()

    ok = err = 0
    total_bars = 0
    completed = 0
    err_samples = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_one, code, args.days): code
                   for code, _ in missing}
        for future in as_completed(futures):
            completed += 1
            try:
                status, bars, code, msg = future.result()
                if status == "ok":
                    ok += 1
                    total_bars += bars
                else:
                    err += 1
                    if len(err_samples) < 10:
                        err_samples.append(f"{code}: {msg}")
            except Exception as e:
                err += 1
            if completed % 50 == 0 or completed == len(missing):
                print(f"  {completed}/{len(missing)} 完了 "
                      f"(取得:{ok} エラー:{err} +バー:{total_bars:,})",
                      flush=True)

    print()
    print("=" * 60)
    print(f"  新規取得完了")
    print(f"  処理: {len(missing)}銘柄")
    print(f"  成功: {ok}  エラー: {err}")
    print(f"  追加バー: {total_bars:,}")
    if err_samples:
        print(f"\n  エラー例 (最大10件):")
        for s in err_samples:
            print(f"    {s}")
    print("=" * 60)
    print(f"\n次のステップ: winners 再抽出")
    print(f"  python winners_by_regime.py")


if __name__ == "__main__":
    main()
