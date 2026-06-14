"""
fix_corrupted_pkls.py  ―  株式分割等で破損したpklを修復
==================================================================
1. data_sanity_check で異常検出
2. 破損 pkl を削除
3. auto_adjust=True で再取得 (分割調整済)

【使い方】
  python fix_corrupted_pkls.py                  # 検出のみ (dry-run)
  python fix_corrupted_pkls.py --execute        # 実行: 削除+再取得
  python fix_corrupted_pkls.py --execute --max-atr-pct 3.0
  python fix_corrupted_pkls.py --quarantine-only  # 隔離だけ、再取得しない
"""

from __future__ import annotations

import argparse
import pickle
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch, DATA_DIR, yf_to_jquants
from data_sanity_check import check_one

JST = timezone(timedelta(hours=9))


def refetch_one(yf_code, days=60):
    """1銘柄を yfinance から auto_adjust=True で再取得。"""
    import yfinance as yf
    from yfinance_update import yf_df_to_jquants
    code5 = yf_to_jquants(yf_code)
    pkl_path = DATA_DIR / f"{code5}.pkl"
    try:
        df = yf.download(yf_code, period=f"{days}d", interval="5m",
                          progress=False, threads=False, auto_adjust=True)
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
    parser = argparse.ArgumentParser(description="破損pkl修復")
    parser.add_argument("--execute", action="store_true",
                        help="検出のみではなく実行")
    parser.add_argument("--quarantine-only", action="store_true",
                        help="隔離のみ、再取得しない")
    parser.add_argument("--max-atr-pct", type=float, default=5.0)
    parser.add_argument("--max-gap-pct", type=float, default=30.0)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    # prime全銘柄
    from symbols_listed_all import SYMBOLS
    targets = SYMBOLS
    print(f"対象: {len(targets)}銘柄")

    # データロード
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    print(f"  ロード: {len(fetched)}銘柄")

    # サニティチェック
    print(f"\nサニティチェック ({args.max_atr_pct}%/{args.max_gap_pct}%基準)...")
    insane = []
    for sym, df in fetched.items():
        r = check_one(sym, df, args.max_atr_pct, args.max_gap_pct)
        if not r["sane"]:
            insane.append((sym, r))

    print(f"\n異常銘柄: {len(insane)}/{len(fetched)} ({len(insane)*100//max(1,len(fetched))}%)")

    if not args.execute:
        print("\n[dry-run] --execute で実行します")
        # サンプル表示
        for sym, r in sorted(insane, key=lambda x: -x[1]["atr_pct"])[:20]:
            print(f"  {sym:<10} ATR{r['atr_pct']:>6.1f}% / "
                  f"Gap{r['max_gap']:>6.0f}%")
        if len(insane) > 20:
            print(f"  ... 他 {len(insane)-20}銘柄")
        return

    # 隔離
    quarantine_dir = DATA_DIR.parent / "quarantine_5m"
    quarantine_dir.mkdir(exist_ok=True)
    print(f"\n隔離中 → {quarantine_dir}...")
    moved = 0
    for sym, _ in insane:
        code5 = yf_to_jquants(sym)
        src = DATA_DIR / f"{code5}.pkl"
        if src.exists():
            dst = quarantine_dir / f"{code5}.pkl"
            shutil.move(str(src), str(dst))
            moved += 1
    print(f"  {moved}銘柄 隔離完了")

    if args.quarantine_only:
        print("\n[隔離のみ] 再取得スキップ")
        return

    # 再取得 (auto_adjust=True)
    print(f"\n再取得中 (auto_adjust=True, workers={args.workers})...")
    insane_syms = [s for s, _ in insane]
    ok = err = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(refetch_one, s, args.days): s for s in insane_syms}
        for fut in as_completed(futures):
            completed += 1
            try:
                status, bars, sym, msg = fut.result()
                if status == "ok":
                    ok += 1
                else:
                    err += 1
            except Exception:
                err += 1
            if completed % 50 == 0 or completed == len(insane_syms):
                print(f"  {completed}/{len(insane_syms)} 完了 "
                      f"(成功:{ok} エラー:{err})", flush=True)

    print(f"\n再取得完了: 成功 {ok} / エラー {err}")

    # 再検査
    print(f"\n再検査中...")
    fetched2 = load_intraday_batch([s for s, _ in insane], args.days,
                                    source="local")
    still_insane = []
    for sym, df in fetched2.items():
        r = check_one(sym, df, args.max_atr_pct, args.max_gap_pct)
        if not r["sane"]:
            still_insane.append((sym, r))
    print(f"  まだ異常: {len(still_insane)}銘柄 (これらは隔離維持)")

    print(f"\n=== 結果 ===")
    print(f"  検出異常: {len(insane)}")
    print(f"  再取得成功: {ok}")
    print(f"  修復後も異常: {len(still_insane)}")
    print(f"  → クリーンになった銘柄: {ok - len(still_insane)}")


if __name__ == "__main__":
    main()
