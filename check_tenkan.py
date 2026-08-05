"""check_tenkan.py — 転換トレードが0件になる原因を数秒で切り分ける診断ツール。

run_signals_holdout_all.py の転換ブロックと同じ入力・同じ判定を再現して、
どの段階で件数が落ちているかを表示する。バックテストは一切走らせないので即終了する。

使い方(swingtrade フォルダで):
  python check_tenkan.py
  python check_tenkan.py --min-price 1000 --price-ranges 6000,0   # daily と同条件
  python check_tenkan.py --source-dir ..\\gobot                    # CSVが別フォルダのとき
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

ap = argparse.ArgumentParser(description="転換0件の原因を切り分ける")
ap.add_argument("--source-dir", type=str, default=".",
                help="oos_raw_fold*.csv の検索先 (既定=カレントディレクトリ)")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--price-ranges", type=str, default="6000,0")
ap.add_argument("--max-price", type=float, default=10000.0)
args = ap.parse_args()

print("=" * 68)
print("転換トレード 診断")
print("=" * 68)
print(f"CWD        : {os.getcwd()}")
print(f"検索ディレクトリ : {Path(args.source_dir).resolve()}")

# ── ① CSV の有無 ───────────────────────────────────────────────────────────
csvs = sorted(Path(args.source_dir).glob("oos_raw_fold*.csv"))
print(f"\n① oos_raw_fold*.csv : {len(csvs)}件")
if not csvs:
    print("   ✗ 見つかりません。これが0件なら転換は必ず0件になります。")
    print("     → run_oos_folds.py をこのフォルダで実行するか、CSVをコピーしてください。")
    sys.exit(1)
for c in csvs:
    print(f"     {c.name}")

try:
    import pandas as pd
except ImportError:
    print("\n[ERROR] pandas が必要です")
    sys.exit(1)

df = pd.concat([pd.read_csv(f) for f in csvs], ignore_index=True)
df["entry_date"] = pd.to_datetime(df["entry_date"]).dt.date
print(f"\n② 全レコード : {len(df)}件")
print(f"   filled=1 (lss約定)     : {(df.filled == 1).sum()}件")
print(f"   filled=0 (転換候補)     : {(df.filled == 0).sum()}件")
print(f"   strategy='転換' の行    : {(df.strategy == '転換').sum()}件  ← CSV既存の完成済み転換")

# ── ③ 価格範囲の導出 (run_signals_holdout_all と同じロジック) ──────────────
min_p = args.min_price if args.min_price and args.min_price > 0 else 0.0
max_p = 0.0
pr_given = False
if args.price_ranges:
    try:
        vals = [float(x.strip()) for x in str(args.price_ranges).split(",") if x.strip()]
        if vals:
            pr_given = True
            max_p = next((p for p in vals if p > 0), 0.0)
    except Exception:
        pr_given = False
        max_p = 0.0
if not pr_given and max_p <= 0:
    mx = args.max_price or 0.0
    max_p = mx if 0 < mx < 100000 else 0.0
print(f"\n③ 価格範囲 : {min_p:g}〜{'無制限' if max_p <= 0 else format(max_p, 'g')}円")


def price_ok(ep: float) -> bool:
    if ep <= 0:
        return True
    if min_p > 0 and ep < min_p:
        return False
    if max_p > 0 and ep > max_p:
        return False
    return True


# ── ④ 再シミュ対象 (filled=0) ──────────────────────────────────────────────
unf = df[df.filled == 0].copy()
unf_ok = unf[unf["entry_p"].fillna(0).astype(float).map(price_ok)]
print(f"\n④ 再シミュ対象 (filled=0)")
print(f"   価格フィルター前 : {len(unf)}件")
print(f"   価格フィルター後 : {len(unf_ok)}件  (除外 {len(unf) - len(unf_ok)}件)")
print(f"   ユニーク(銘柄,日) : {len(set(zip(unf_ok.symbol, unf_ok.entry_date)))}組")
print("   ※ ここから更に『分足データが手元にある分』だけが再シミュで生成されます。")

# ── ⑤ CSV既存行からの補完 ────────────────────────────────────────────────
tk = df[df.strategy == "転換"].copy()
tk_ok = tk[tk["entry_p"].fillna(0).astype(float).map(price_ok)]
print(f"\n⑤ CSV既存の転換行からの補完 (分足データ不要)")
print(f"   価格フィルター前 : {len(tk)}件")
print(f"   価格フィルター後 : {len(tk_ok)}件  (除外 {len(tk) - len(tk_ok)}件)")

if len(tk_ok) > 0:
    n = len(tk_ok)
    w = int((tk_ok.pnl > 0).sum())
    gp = tk_ok[tk_ok.pnl > 0].pnl.sum()
    gl = abs(tk_ok[tk_ok.pnl < 0].pnl.sum())
    pf = gp / gl if gl > 0 else float("inf")
    print(f"\n   → 分足が全く無くても、最低 {n}件 は表示されるはずです")
    print(f"      勝率 {w / n * 100:.1f}%  PF {'∞' if pf == float('inf') else f'{pf:.2f}'}  "
          f"損益 {tk_ok.pnl.sum():+,.0f}円")
    print(f"      期間 {tk_ok.entry_date.min()} 〜 {tk_ok.entry_date.max()}")
    tk_ok = tk_ok.copy()
    tk_ok["m"] = tk_ok["entry_date"].astype(str).str[:7]
    print(f"\n   月別:")
    for m, g in tk_ok.groupby("m"):
        print(f"      {m}  {len(g):>3}件  {g.pnl.sum():>+10,.0f}円")

# ── ⑥ コード側の修正が入っているか ────────────────────────────────────────
print(f"\n⑥ run_signals_holdout_all.py の修正状態")
src_path = Path(__file__).resolve().parent / "run_signals_holdout_all.py"
if src_path.exists():
    src = src_path.read_text(encoding="utf-8", errors="ignore")
    has_fix = "_lrv_pr_given" in src
    has_bug = "for p in _price_list if p > 0" in src
    print(f"   価格範囲の修正 (_lrv_pr_given) : {'✓ あり' if has_fix else '✗ なし ← 古いコード'}")
    print(f"   旧バグ (_price_list 参照)      : {'✗ 残っている' if has_bug else '✓ 解消済み'}")
    print(f"   CSV補完 (_lrv_csv_tk)          : "
          f"{'✓ あり' if '_lrv_csv_tk' in src else '✗ なし ← 古いコード'}")
else:
    print(f"   [WARN] {src_path} が見つかりません")

print("\n" + "=" * 68)
if len(tk_ok) > 0:
    print(f"判定: CSVは正常。転換は最低 {len(tk_ok)}件 出るはずです。")
    print("      レポートで0件なら、実行中のコードが古いか、CWDが違います。")
else:
    print("判定: CSV側で全て除外されています。価格範囲を確認してください。")
print("=" * 68)
