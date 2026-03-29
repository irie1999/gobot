#!/usr/bin/env python3
"""
買値・売値の診断スクリプト
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
print("\n【診断1】auto_adjust=False でのデータ (rsi2_hv.py の設定)")
raw_false = yf.download(symbol, period="5d", interval="1d",
                        auto_adjust=False, progress=False)

if raw_false.empty:
    print("  ❌ データ取得失敗")
else:
    if isinstance(raw_false.columns, pd.MultiIndex):
        raw_false.columns = raw_false.columns.get_level_values(0)
    raw_false.columns = [str(c).lower() for c in raw_false.columns]
    cols_available = raw_false.columns.tolist()

    print(f"  列名: {cols_available}")
    show_cols = [c for c in ["open", "close", "adj close"] if c in cols_available]
    print(f"\n  直近5日間 ({', '.join(show_cols)}):")
    print(raw_false[show_cols].to_string())
    last_open = float(raw_false["open"].iloc[-1])
    print(f"\n  最新の始値 (Open): {last_open:,.0f}円")

# ── 診断2: auto_adjust=True との比較 ────────────────────────
print("\n【診断2】auto_adjust=True との比較 (修正前の設定)")
raw_true = yf.download(symbol, period="5d", interval="1d",
                       auto_adjust=True, progress=False)

if not raw_true.empty:
    if isinstance(raw_true.columns, pd.MultiIndex):
        raw_true.columns = raw_true.columns.get_level_values(0)
    raw_true.columns = [str(c).lower() for c in raw_true.columns]

    print(f"  列名: {raw_true.columns.tolist()}")
    show_t = [c for c in ["open", "close"] if c in raw_true.columns]
    print(f"\n  直近5日間 ({', '.join(show_t)}):")
    print(raw_true[show_t].to_string())
    last_open_t = float(raw_true["open"].iloc[-1])
    print(f"\n  最新の始値 (Open, adj): {last_open_t:,.0f}円")

    if not raw_false.empty:
        ratio = last_open_t / last_open if last_open > 0 else 0
        print(f"\n  auto_adjust比較: {last_open:,.0f} vs {last_open_t:,.0f}")
        print(f"  比率 (True/False): {ratio:.4f}")
        if abs(ratio - 1) > 0.01:
            print(f"  ⚠️  1%以上の差があります。auto_adjust=True が過去に原因だった可能性あり")

# ── 診断3: バックテストの買値が実際に何の価格か ───────────────
print("\n【診断3】バックテストのエントリー価格の定義")
print("""
  バックテストのロジック:
    ・シグナル発生日: RSI(2) ≤ 閾値の「前日」
    ・買値(entry_p): シグナル翌日の「始値 (Open)」← ここが買値
    ・エントリー日(entry_dt): シグナル翌日の日付

  つまり:
    2月10日にRSI≤10のシグナル → 2月11日の「始値」で購入
    → 出力には entry_dt=2月11日, 買値=2月11日の始値 が表示される

  ✅ 実データとの照合方法:
    Yahoo Finance Japan でシンボルを検索
    → 「履歴データ」タブ
    → entry_dt (エントリー日) の行の「始値」と比較
""")

# ── 診断4: キャッシュファイルの確認 ──────────────────────────
print("【診断4】キャッシュファイルの確認")
cache_dir = Path(".rsi2_cache")
if cache_dir.exists():
    pkl_files = list(cache_dir.glob("*.pkl"))
    print(f"  キャッシュファイル数: {len(pkl_files)}")
    if pkl_files:
        print("  ⚠️  古いキャッシュが存在します。以下のコマンドで削除:")
        print("      rm .rsi2_cache/*.pkl")
        for f in sorted(pkl_files)[:5]:
            print(f"      {f}")
else:
    print("  キャッシュなし (問題なし)")

# ── 診断5: コードのauto_adjust設定確認 ──────────────────────
print("\n【診断5】現在のコードのauto_adjust設定")
try:
    with open("rsi2_hv.py") as f:
        content = f.read()
    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        if "auto_adjust" in line and "download" in lines[max(0,i-3):i+1][-1] if i >= 3 else "download" in line:
            print(f"  行{i}: {line.strip()}")
        elif "auto_adjust" in line and "yf.download" in content[max(0,content.find(line)-200):content.find(line)]:
            print(f"  行{i}: {line.strip()}")
except Exception as e:
    print(f"  確認失敗: {e}")

import subprocess
result = subprocess.run(["grep", "-n", "auto_adjust", "rsi2_hv.py"], capture_output=True, text=True)
print("\n  rsi2_hv.py の auto_adjust 設定一覧:")
for line in result.stdout.strip().split("\n"):
    print(f"    {line}")

print("\n" + "=" * 60)
print("✅ 上記の「診断3」の方法で Yahoo Finance Japan と照合してください")
print("   entry_dt の「始値」と買値が一致すれば正常です")
print("=" * 60)
