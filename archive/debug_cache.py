#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
キャッシュ vs 実データ 比較診断スクリプト

使い方:
  python debug_cache.py          # 7751.T (キヤノン) で確認
  python debug_cache.py 7267.T   # ホンダで確認

買値が実データと一致しない原因を特定します。
"""

import pickle
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "7751.T"
cache_key = symbol.replace(".", "_")
cache_dir = Path(".rsi2_cache")

print("=" * 65)
print(f"診断対象: {symbol}")
print("=" * 65)

# ── 診断1: 永続キャッシュの内容確認 ────────────────────────────
print("\n[診断1] 永続キャッシュ (.rsi2_cache/{}.pkl)".format(cache_key))
pkl_path = cache_dir / f"{cache_key}.pkl"

cached_df = None
if not pkl_path.exists():
    print("  キャッシュなし → rsi2.py は直接ダウンロードを使用")
else:
    try:
        with open(pkl_path, "rb") as f:
            cached_df = pickle.load(f)
        print(f"  行数: {len(cached_df)}")
        print(f"  期間: {cached_df.index[0].date()} ～ {cached_df.index[-1].date()}")
        print(f"  列名: {cached_df.columns.tolist()}")
        # 直近10行を表示
        recent = cached_df.tail(10)
        print(f"\n  直近10行 (open / close):")
        for dt, row in recent.iterrows():
            print(f"    {dt.date()}  open={row['open']:>10,.1f}  close={row['close']:>10,.1f}")
    except Exception as e:
        print(f"  読み込みエラー: {e}")

# ── 診断2: 一時キャッシュの一覧 ─────────────────────────────────
print(f"\n[診断2] 一時キャッシュファイルの確認")
if cache_dir.exists():
    tmp_files = [f for f in cache_dir.glob(f"{cache_key}_*.pkl")]
    if tmp_files:
        print(f"  一時キャッシュ: {len(tmp_files)} ファイル")
        for f in sorted(tmp_files):
            print(f"    {f.name}")
    else:
        print("  一時キャッシュなし")
else:
    print("  キャッシュディレクトリなし")

# ── 診断3: 実際のダウンロードとの比較 ──────────────────────────
print(f"\n[診断3] 実際のダウンロードデータ (auto_adjust=False, period=1y)")
try:
    raw = yf.download(symbol, period="1y", interval="1d",
                      auto_adjust=False, progress=False)
    if raw.empty:
        print("  ERROR: データ取得失敗")
    else:
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]

        print(f"  行数: {len(raw)}")
        print(f"  期間: {raw.index[0].date()} ～ {raw.index[-1].date()}")
        print(f"  列名: {raw.columns.tolist()}")

        recent_dl = raw.tail(10)
        print(f"\n  直近10行 (open / close):")
        for dt, row in recent_dl.iterrows():
            print(f"    {dt.date()}  open={row['open']:>10,.1f}  close={row['close']:>10,.1f}")

        # キャッシュと比較（共通する日付があれば）
        if cached_df is not None:
            common = cached_df.index.intersection(raw.index)
            if len(common) > 0:
                print(f"\n  [キャッシュ vs 実データ 比較] 共通日 {len(common)} 行")
                mismatch = 0
                for dt in common[-20:]:  # 直近20日を比較
                    c_open = float(cached_df.loc[dt, "open"])
                    d_open = float(raw.loc[dt, "open"])
                    ratio  = c_open / d_open if d_open > 0 else 0
                    flag   = " ← !! 不一致 !!" if abs(ratio - 1) > 0.02 else ""
                    if flag:
                        mismatch += 1
                    print(f"    {dt.date()}  cache={c_open:>10,.1f}  download={d_open:>10,.1f}  ratio={ratio:.3f}{flag}")
                if mismatch == 0:
                    print("  → キャッシュと実データは一致しています")
                else:
                    print(f"\n  !! {mismatch} 日で不一致 — キャッシュが壊れています !!")
                    print("  修正方法: del .rsi2_cache\\*.pkl  (Windowsの場合)")
            else:
                print("  共通する日付がありません")
except Exception as e:
    print(f"  ダウンロードエラー: {e}")

# ── 診断4: rsi2_hv.py の fetch() 動作確認 ──────────────────────
print(f"\n[診断4] rsi2_hv.py の fetch() 動作確認")
try:
    raw2 = yf.download(symbol, period="3y", interval="1d",
                       auto_adjust=False, progress=False)
    if raw2.empty:
        print("  ERROR: データ取得失敗 (period=3y)")
    else:
        if isinstance(raw2.columns, pd.MultiIndex):
            raw2.columns = raw2.columns.get_level_values(0)
        raw2.columns = [str(c).lower() for c in raw2.columns]
        # rsi2_hv.py の fetch() は dedup をしない — 問題があるか確認
        dup_cols = [c for c in raw2.columns if list(raw2.columns).count(c) > 1]
        if dup_cols:
            print(f"  !! 重複列あり: {dup_cols}  ← rsi2_hv.py で問題になる可能性")
        else:
            print(f"  重複列なし: OK")

        raw2 = raw2.loc[:, ~raw2.columns.duplicated(keep="first")]
        raw2 = raw2[["open", "high", "low", "close", "volume"]].dropna()
        print(f"  行数: {len(raw2)}  期間: {raw2.index[0].date()} ～ {raw2.index[-1].date()}")
        # open の統計
        print(f"  open 最小={raw2['open'].min():,.0f}  最大={raw2['open'].max():,.0f}  "
              f"直近={raw2['open'].iloc[-1]:,.0f}")
        suspicious = raw2[raw2["open"] > 10000]  # 日経レベルの疑わしい値
        if not suspicious.empty:
            print(f"\n  !! open > 10,000 の行 ({len(suspicious)} 行) — 日経レベルの値が混入:")
            for dt, row in suspicious.head(10).iterrows():
                print(f"    {dt.date()}  open={row['open']:,.0f}")
        else:
            print(f"  open値に日経レベルの異常値なし: OK")
except Exception as e:
    print(f"  エラー: {e}")

print("\n" + "=" * 65)
print("まとめ:")
print("  [診断1] キャッシュの open 値が実際の株価と一致するか確認")
print("  [診断3] '!! 不一致 !!' が出ていればキャッシュ削除が必要")
print("  [診断4] open > 10,000 の行があれば yfinance のデータ取得バグ")
print()
print("  修正コマンド (Windows):")
print("    del .rsi2_cache\\*.pkl")
print("  修正コマンド (Mac/Linux):")
print("    rm .rsi2_cache/*.pkl")
print("=" * 65)
