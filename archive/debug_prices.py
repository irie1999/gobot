#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
買値・売値の診断スクリプト (Windows対応版)
原因特定のために実行してください。

使い方:
  python debug_prices.py          # 7751.T (キャノン) で確認
  python debug_prices.py 7267.T   # ホンダで確認
"""

import sys
import yfinance as yf
import pandas as pd
from pathlib import Path

symbol = sys.argv[1] if len(sys.argv) > 1 else "7751.T"

print("=" * 60)
print(f"診断対象: {symbol}")
print("=" * 60)

# ── 診断1: auto_adjust=False の実データ確認 ──────────────────
print("\n[診断1] auto_adjust=False でのデータ (rsi2_hv.py の設定)")
raw_false = yf.download(symbol, period="5d", interval="1d",
                        auto_adjust=False, progress=False)

last_open = None
if raw_false.empty:
    print("  ERROR: データ取得失敗")
else:
    if isinstance(raw_false.columns, pd.MultiIndex):
        raw_false.columns = raw_false.columns.get_level_values(0)
    raw_false.columns = [str(c).lower() for c in raw_false.columns]

    show_cols = [c for c in ["open", "close", "adj close"] if c in raw_false.columns]
    print(f"  列名: {raw_false.columns.tolist()}")
    print(f"\n  直近5日間 ({', '.join(show_cols)}):")
    print(raw_false[show_cols].to_string())
    last_open = float(raw_false["open"].iloc[-1])
    print(f"\n  最新の始値 (Open): {last_open:,.0f}円")

# ── 診断2: auto_adjust=True との比較 ────────────────────────
print("\n[診断2] auto_adjust=True との比較 (修正前の設定)")
raw_true = yf.download(symbol, period="5d", interval="1d",
                       auto_adjust=True, progress=False)

if not raw_true.empty and last_open is not None:
    if isinstance(raw_true.columns, pd.MultiIndex):
        raw_true.columns = raw_true.columns.get_level_values(0)
    raw_true.columns = [str(c).lower() for c in raw_true.columns]

    show_t = [c for c in ["open", "close", "adj close"] if c in raw_true.columns]
    print(f"  列名: {raw_true.columns.tolist()}")
    print(f"\n  直近5日間 ({', '.join(show_t)}):")
    print(raw_true[show_t].to_string())
    last_open_t = float(raw_true["open"].iloc[-1])
    print(f"\n  最新の始値 (Open, adj): {last_open_t:,.0f}円")

    ratio = last_open_t / last_open if last_open > 0 else 0
    print(f"\n  比較:")
    print(f"    auto_adjust=False: {last_open:,.0f}円  <- 現在の設定 (正しいはず)")
    print(f"    auto_adjust=True:  {last_open_t:,.0f}円  <- 修正前の設定")
    print(f"    比率 (True/False): {ratio:.4f}")
    if abs(ratio - 1) > 0.01:
        print(f"  WARNING: {abs(1-ratio)*100:.1f}%の差があります")
        print(f"  旧コードでは {last_open_t:,.0f}円 が表示されていた")

# ── 診断3: バックテストの買値の定義 ─────────────────────────
print("\n[診断3] バックテストの買値・売値の定義")
print("""
  バックテストのロジック:
    シグナル発生日: RSI(2) <= 閾値の日
    買値(entry_p) : シグナル翌日の「始値 (Open)」
    エントリー日  : シグナル翌日の日付

  例: 2月10日にRSI<=10シグナル → 2月11日の「始値」で購入
      出力: entry_dt=2月11日, 買値=2月11日の始値

  Yahoo Financeでの確認方法:
    1. finance.yahoo.com で銘柄コードを検索
    2. Historical Data タブをクリック
    3. entry_dt の行の「Open」列の値と比較
    ※「Close」や「Adj Close」ではなく「Open」を確認
""")

# ── 診断4: キャッシュファイルの確認 ──────────────────────────
print("[診断4] キャッシュファイルの確認")
cache_dir = Path(".rsi2_cache")
if cache_dir.exists():
    pkl_files = list(cache_dir.glob("*.pkl"))
    print(f"  キャッシュファイル数: {len(pkl_files)}")
    if pkl_files:
        print("  WARNING: 古いキャッシュが存在します。削除してください:")
        print("  Windows: del .rsi2_cache\\*.pkl")
        print("  Mac/Linux: rm .rsi2_cache/*.pkl")
        for f in sorted(pkl_files)[:5]:
            print(f"    {f}")
    else:
        print("  キャッシュなし (問題なし)")
else:
    print("  キャッシュなし (問題なし)")

# ── 診断5: コードのauto_adjust設定確認 (Windows対応) ──────────
print("\n[診断5] rsi2_hv.py の auto_adjust 設定確認")
try:
    with open("rsi2_hv.py", encoding="utf-8") as f:
        lines = f.readlines()
    found = False
    for i, line in enumerate(lines, 1):
        if "auto_adjust" in line:
            print(f"  行{i:3d}: {line.rstrip()}")
            found = True
    if not found:
        print("  auto_adjust の記述が見つかりません")
except FileNotFoundError:
    print("  rsi2_hv.py が見つかりません。このスクリプトと同じフォルダで実行してください")
except Exception as e:
    print(f"  確認失敗: {e}")

print("\n" + "=" * 60)
print("確認ポイント:")
print("  [診断1] Open値 = Yahoo Finance の同日「Open」と一致するか")
print("  [診断5] auto_adjust=False になっているか (True なら要修正)")
print("=" * 60)
