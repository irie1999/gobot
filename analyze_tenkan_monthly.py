"""analyze_tenkan_monthly.py — 転換トレード OOS 月別集計

run_signals_holdout_all.py の転換シミュロジックを切り出して、
月別の件数・勝率・損益をターミナルに出力する独立スクリプト。

使い方:
  python analyze_tenkan_monthly.py                        # BT≥0, 全期間
  python analyze_tenkan_monthly.py --bt-min 30            # BT≥30 のみ
  python analyze_tenkan_monthly.py --from-month 2024-12   # 2024-12 以降（既定）
  python analyze_tenkan_monthly.py --from-month 2025-02   # 2月以降
  python analyze_tenkan_monthly.py --bt-min 30 --csv tenkan_monthly.csv  # CSV出力

oos_raw_fold*.csv は swingtrade フォルダ(CWD)に必要。
5分足データは MINUTE_5M_DIR 環境変数 or 自動検出 (stock_5min フォルダ)。
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import pickle
import sys
from collections import defaultdict
from datetime import date, time
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="転換トレード OOS 月別集計")
ap.add_argument("--bt-min",      type=float, default=0.0,   help="BTスコア下限（既定0=全件）")
ap.add_argument("--from-month",  type=str,   default="2024-12",
                help="集計開始年月 YYYY-MM（既定 2024-12）")
ap.add_argument("--to-month",    type=str,   default="",    help="集計終了年月 YYYY-MM（省略=全期間）")
ap.add_argument("--source-dir",  type=str,   default=".",   help="oos_raw_fold*.csv の検索ディレクトリ（既定=CWD）")
ap.add_argument("--5m-dir",      dest="min5m_dir", type=str, default="",
                help="5分足 pkl ディレクトリ（省略=自動検出）")
ap.add_argument("--csv",         type=str,   default="",    help="月別結果を CSV 保存するパス")
ap.add_argument("--detail",      action="store_true",       help="銘柄レベルの詳細も出力")
ap.add_argument("--workers",     type=int,   default=4)
args = ap.parse_args()

# ── 5分足ディレクトリを解決 ──────────────────────────────────────────────────
def _resolve_5m_dir() -> Path | None:
    if args.min5m_dir:
        p = Path(args.min5m_dir)
        return p if p.exists() else None
    env = os.environ.get("MINUTE_5M_DIR", "").strip()
    if env:
        p = Path(env)
        if p.exists():
            return p
    here = Path(__file__).resolve().parent
    for cand in [
        here / "data" / "stock_5min",
        here.parent / "stock_5min",
        here.parent / "stock_5min" / "data" / "stock_5min",
    ]:
        try:
            if cand.exists() and any(cand.glob("*.pkl")):
                return cand
        except Exception:
            pass
    return None

DIR_5M = _resolve_5m_dir()
print(f"5分足DIR: {DIR_5M}", flush=True)

# ── OOS CSV を読み込む ───────────────────────────────────────────────────────
src_dir = Path(args.source_dir)
oos_csvs = sorted(src_dir.glob("oos_raw_fold*.csv"))
if not oos_csvs:
    print(f"[ERROR] {src_dir} に oos_raw_fold*.csv が見つかりません。CWD を確認してください。", file=sys.stderr)
    sys.exit(1)
print(f"OOS CSV: {len(oos_csvs)}件", flush=True)

all_df = pd.concat([pd.read_csv(f) for f in oos_csvs], ignore_index=True)
all_df["entry_date"] = pd.to_datetime(all_df["entry_date"]).dt.date

# filled=0 = ショート未約定 = 転換候補
unfl_df = all_df[all_df["filled"] == 0].copy()
print(f"OOS全件: {len(all_df)} / 未約定(filled=0): {len(unfl_df)}", flush=True)

# BTスコアフィルター
if args.bt_min > 0 and "bt_score" in unfl_df.columns:
    before = len(unfl_df)
    unfl_df = unfl_df[unfl_df["bt_score"].fillna(0) >= args.bt_min]
    print(f"BT≥{args.bt_min} フィルター後: {len(unfl_df)}件 (除外 {before - len(unfl_df)}件)", flush=True)

# 月フィルター
from_ym = args.from_month[:7] if args.from_month else ""
to_ym   = args.to_month[:7]   if args.to_month   else ""

def _ym(d: date) -> str:
    return d.strftime("%Y-%m")

if from_ym:
    unfl_df = unfl_df[unfl_df["entry_date"].apply(_ym) >= from_ym]
if to_ym:
    unfl_df = unfl_df[unfl_df["entry_date"].apply(_ym) <= to_ym]
print(f"月フィルター後: {len(unfl_df)}件 ({from_ym or '開始なし'} ～ {to_ym or '現在'})", flush=True)

if len(unfl_df) == 0:
    print("[INFO] 対象トレードが 0 件です。終了。")
    sys.exit(0)

# ── 5分足シミュレーション ────────────────────────────────────────────────────
BUY_T   = time(9, 9)
SELL_T  = time(11, 30)
SLIP    = 0.0005
FEE     = 0.001
QTY     = 100

_cache: dict = {}

def _yf2jq(sym: str) -> str:
    return sym.upper().replace(".T", "") + "0"

def _load_pkl(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        df.columns = [c.lower() for c in df.columns]
        try:
            import pytz
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("Asia/Tokyo")
            else:
                df.index = df.index.tz_convert("Asia/Tokyo")
        except ImportError:
            pass
        return df
    except Exception:
        return None

def _bars(sym: str) -> pd.DataFrame | None:
    if sym in _cache:
        return _cache[sym]
    code = _yf2jq(sym)
    df2 = None
    if DIR_5M:
        df2 = _load_pkl(DIR_5M / f"{code}.pkl")
    _cache[sym] = df2
    return df2

def _sim(sym: str, d: date) -> tuple | None:
    """(pnl, entry_p, exit_p) or None"""
    df3 = _bars(sym)
    if df3 is None:
        return None
    try:
        day3 = df3[df3.index.date == d]
    except Exception:
        return None
    if len(day3) < 3:
        return None
    bar_ts = [t3.time() for t3 in day3.index]
    # 買い: BUY_T 以降の最初バー OPEN
    bp = None
    for i3, bt3 in enumerate(bar_ts):
        if bt3 >= BUY_T:
            bp = float(day3.iloc[i3]["open"])
            break
    # 売り: SELL_T 以前の最後バー OPEN
    sp = None
    for i3 in range(len(bar_ts) - 1, -1, -1):
        if bar_ts[i3] <= SELL_T:
            sp = float(day3.iloc[i3]["open"])
            break
    if not bp or not sp:
        return None
    be = bp * (1 + SLIP)
    se = sp * (1 - SLIP)
    pnl = (se - be) * QTY - (be + se) * QTY * FEE
    return pnl, be, se

# ── 全件シミュ ───────────────────────────────────────────────────────────────
print(f"\nシミュ実行中 ({len(unfl_df)}件)...", flush=True)

records = []
no_data = 0
for _, row in unfl_df.iterrows():
    res = _sim(str(row["symbol"]), row["entry_date"])
    if res is None:
        no_data += 1
        continue
    pnl, ep, xp = res
    records.append({
        "ym":       _ym(row["entry_date"]),
        "date":     row["entry_date"],
        "symbol":   str(row.get("symbol", "")),
        "name":     str(row.get("name", "")),
        "bt":       float(row.get("bt_score", 0) or 0),
        "pnl":      pnl,
        "entry_p":  ep,
        "exit_p":   xp,
        "win":      1 if pnl > 0 else 0,
    })

print(f"シミュ完了: 成功={len(records)}件 / データ不足スキップ={no_data}件", flush=True)

if not records:
    print("[INFO] 有効なトレードが 0 件でした。")
    sys.exit(0)

# ── 月別集計 ─────────────────────────────────────────────────────────────────
by_month: dict[str, list] = defaultdict(list)
for r in records:
    by_month[r["ym"]].append(r)

months = sorted(by_month.keys())

print("\n" + "="*72)
print(f"【転換トレード 月別集計】  BT≥{args.bt_min}  {from_ym or '全期間'} ～ {to_ym or '現在'}")
print("="*72)
header = f"{'月':<9} {'件数':>5} {'勝率':>7} {'総損益':>11} {'平均損益':>9} {'勝':>4} {'負':>4}"
print(header)
print("-"*72)

total_n = total_win = 0
total_pnl = 0.0
csv_rows = []
for ym in months:
    rs  = by_month[ym]
    n   = len(rs)
    wins = sum(r["win"] for r in rs)
    losses = n - wins
    pnl = sum(r["pnl"] for r in rs)
    wr  = wins / n * 100 if n else 0
    avg = pnl / n if n else 0
    sign = "+" if pnl >= 0 else ""
    print(f"{ym:<9} {n:>5} {wr:>6.1f}% {sign}{pnl:>10,.0f}円 {avg:>+9,.0f}円 {wins:>4} {losses:>4}")
    total_n   += n
    total_win += wins
    total_pnl += pnl
    csv_rows.append({"month": ym, "trades": n, "win": wins, "loss": losses,
                     "win_rate": round(wr, 1), "total_pnl": round(pnl, 0),
                     "avg_pnl": round(avg, 0)})

print("-"*72)
total_wr  = total_win / total_n * 100 if total_n else 0
total_avg = total_pnl / total_n if total_n else 0
sign = "+" if total_pnl >= 0 else ""
print(f"{'合計':<9} {total_n:>5} {total_wr:>6.1f}% {sign}{total_pnl:>10,.0f}円 {total_avg:>+9,.0f}円 "
      f"{total_win:>4} {total_n-total_win:>4}")
print("="*72)

# PF
total_win_pnl  = sum(r["pnl"] for r in records if r["pnl"] > 0)
total_loss_pnl = sum(abs(r["pnl"]) for r in records if r["pnl"] < 0)
pf = total_win_pnl / total_loss_pnl if total_loss_pnl > 0 else float("inf")
print(f"PF: {pf:.2f}  勝利総益: {total_win_pnl:+,.0f}円  損失総損: {-total_loss_pnl:+,.0f}円")
print()

# ── 詳細: BT帯別 ────────────────────────────────────────────────────────────
if "bt_score" in unfl_df.columns:
    print("【BT帯別集計（全期間）】")
    print(f"{'BT帯':<12} {'件数':>5} {'勝率':>7} {'総損益':>11} {'PF':>6}")
    print("-"*50)
    bt_bands = [(0, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 100)]
    for lo, hi in bt_bands:
        rs2 = [r for r in records if lo <= r["bt"] < hi]
        if not rs2:
            continue
        n2   = len(rs2)
        w2   = sum(r["win"] for r in rs2)
        p2   = sum(r["pnl"] for r in rs2)
        wr2  = w2 / n2 * 100 if n2 else 0
        win_p = sum(r["pnl"] for r in rs2 if r["pnl"] > 0)
        los_p = sum(abs(r["pnl"]) for r in rs2 if r["pnl"] < 0)
        pf2  = win_p / los_p if los_p > 0 else float("inf")
        print(f"BT{lo:>2}-{hi:<3}   {n2:>5} {wr2:>6.1f}% {p2:>+11,.0f}円 {pf2:>6.2f}")
    print()

# ── 詳細: 銘柄別 ─────────────────────────────────────────────────────────────
if args.detail:
    print("【銘柄別集計（全期間）】")
    by_sym: dict[str, list] = defaultdict(list)
    for r in records:
        by_sym[r["symbol"]].append(r)
    sym_rows = []
    for sym, rs in by_sym.items():
        n3 = len(rs)
        w3 = sum(r["win"] for r in rs)
        p3 = sum(r["pnl"] for r in rs)
        nm = rs[0]["name"]
        sym_rows.append((p3, sym, nm, n3, w3))
    sym_rows.sort(reverse=True)
    print(f"{'銘柄':<10} {'名前':<16} {'件数':>4} {'勝率':>7} {'総損益':>10}")
    print("-"*60)
    for p3, sym, nm, n3, w3 in sym_rows:
        print(f"{sym:<10} {nm[:14]:<16} {n3:>4} {w3/n3*100:>6.1f}% {p3:>+10,.0f}円")
    print()

# ── CSV 出力 ─────────────────────────────────────────────────────────────────
if args.csv:
    out_path = Path(args.csv)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["month","trades","win","loss","win_rate","total_pnl","avg_pnl"])
        w.writeheader()
        w.writerows(csv_rows)
    print(f"[CSV] {out_path} に保存しました ({len(csv_rows)}行)")
