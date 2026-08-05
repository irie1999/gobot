"""
analyze_long_905_915.py  — 未約定lssを9:05ロング転換→複数時刻売却スイープ

使い方 (Windowsで実行):
  python analyze_long_905_915.py

前提:
  - OOS CSVファイル群が ./oos_raw_fold*.csv にあること (または --csv-glob で指定)
  - stock_5min フォルダが隣接 or MINUTE_5M_DIR 環境変数で指定済み

出力:
  - コンソールに集計結果 (売却時刻ごとのスイープ)
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import pickle
from datetime import date, time as dtime, timedelta
from pathlib import Path

import pandas as pd

# ─── パス設定 ───────────────────────────────────────────────────────────────

def _resolve_data_dir() -> Path:
    env = os.environ.get("MINUTE_5M_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidates = [
        here / "data" / "minute_5m",
        here.parent / "stock_5min",
        here.parent / "stock_5min" / "data" / "minute_5m",
    ]
    for c in candidates:
        if c.exists() and any(c.glob("*.pkl")):
            return c
    return candidates[0]

DATA_DIR = _resolve_data_dir()

QTY = 100
FEE  = 0.001    # 片道0.1%
SLIP = 0.0005   # 0.05% (成行ロング = 不利に買う)

# ─── 5分足ロード ──────────────────────────────────────────────────────────────

_5m_cache: dict[str, pd.DataFrame | None] = {}

def _yf_to_jq(sym: str) -> str:
    return sym.upper().replace(".T", "") + "0"

def load_5m(sym: str) -> pd.DataFrame | None:
    if sym in _5m_cache:
        return _5m_cache[sym]
    code = _yf_to_jq(sym)
    pkl = DATA_DIR / f"{code}.pkl"
    if not pkl.exists():
        _5m_cache[sym] = None
        return None
    try:
        with open(pkl, "rb") as f:
            df = pickle.load(f)
        df.columns = [c.lower() for c in df.columns]
        if df.index.tzinfo is None:
            import pytz
            df.index = df.index.tz_localize("Asia/Tokyo")
        else:
            df.index = df.index.tz_convert("Asia/Tokyo")
        _5m_cache[sym] = df
        return df
    except Exception as e:
        print(f"  [warn] {sym} load error: {e}")
        _5m_cache[sym] = None
        return None

# ─── 9:05 ロング転換・複数売却時刻スイープ ─────────────────────────────────────

# 売却候補時刻 (バー開始時刻 = その時刻のバーの open で売る)
SELL_TIMES = [
    dtime(9, 10),
    dtime(9, 15),
    dtime(9, 20),
    dtime(9, 25),
    dtime(9, 30),
    dtime(9, 40),
    dtime(9, 45),
    dtime(10, 0),
    dtime(10, 30),
    dtime(11, 0),
    dtime(11, 30),
]

def _find_bar(bar_times, target_time, fallback_idx):
    for i, bt in enumerate(bar_times):
        if bt == target_time:
            return i
    return fallback_idx

def sim_long_sweep(sym: str, d: date) -> dict | None:
    """
    9:05 open で買い → 各売却時刻の open で売った場合の損益を返す。
    Returns: {sell_time_str: pnl, ...} or None
    """
    df5 = load_5m(sym)
    if df5 is None:
        return None

    day_bars = df5[df5.index.date == d].copy()
    if len(day_bars) < 3:
        return None

    bar_times = [t.time() for t in day_bars.index]

    # 買いバー: 9:05 open
    i905 = _find_bar(bar_times, dtime(9, 5), 1)
    if i905 >= len(day_bars):
        return None
    buy_p = float(day_bars.iloc[i905]["open"])
    if buy_p <= 0:
        return None
    buy_eff = buy_p * (1.0 + SLIP)

    result = {}
    for st in SELL_TIMES:
        # 売りバーを探す
        i_sell = _find_bar(bar_times, st, None)
        if i_sell is None or i_sell >= len(day_bars) or i_sell <= i905:
            result[st] = None
            continue
        sell_p = float(day_bars.iloc[i_sell]["open"])
        if sell_p <= 0:
            result[st] = None
            continue
        sell_eff = sell_p * (1.0 - SLIP)
        fee = (buy_eff + sell_eff) * QTY * FEE
        result[st] = (sell_eff - buy_eff) * QTY - fee

    return result

# ─── メイン ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-glob", default="oos_raw_fold*.csv",
                    help="OOS CSVのglob (デフォルト: oos_raw_fold*.csv)")
    ap.add_argument("--bt-min", type=float, default=0,
                    help="BTスコア下限フィルター (デフォルト: 0=全件)")
    args = ap.parse_args()

    # CSV読み込み
    csv_files = glob.glob(args.csv_glob)
    if not csv_files:
        print(f"[error] CSV not found: {args.csv_glob}")
        sys.exit(1)
    print(f"CSV: {len(csv_files)} ファイル")

    dfs = [pd.read_csv(f) for f in sorted(csv_files)]
    all_df = pd.concat(dfs, ignore_index=True)
    all_df["entry_date"] = pd.to_datetime(all_df["entry_date"]).dt.date

    unfilled = all_df[all_df["filled"] == 0].copy()
    if args.bt_min > 0:
        unfilled = unfilled[unfilled["bt_score"] >= args.bt_min]

    print(f"未約定レコード: {len(unfilled)} 件 (BT≥{args.bt_min})")
    print(f"5分足ディレクトリ: {DATA_DIR}")
    if not DATA_DIR.exists():
        print(f"[error] DATA_DIRが存在しない: {DATA_DIR}")
        sys.exit(1)

    # シミュレーション (売却時刻スイープ)
    rows = []
    no_data = 0
    for _, rec in unfilled.iterrows():
        sym = rec["symbol"]
        d   = rec["entry_date"]
        bt  = rec["bt_score"]
        r = sim_long_sweep(sym, d)
        if r is None:
            no_data += 1
            continue
        row = dict(sym=sym, date=d, bt=bt)
        row.update({f"pnl_{st.strftime('%H%M')}": v for st, v in r.items()})
        rows.append(row)

    if not rows:
        print("データなし - stock_5min の pkl が読めていない可能性")
        return

    result_df = pd.DataFrame(rows)
    n_ok = len(result_df)
    print(f"計算成功: {n_ok} / {len(unfilled)} 件 (no_data: {no_data})")

    # ─ 売却時刻スイープ集計 ─
    print(f"\n=== 9:05 ロング転換 売却時刻スイープ ({n_ok}件) ===")
    print(f"{'売却時刻':>8}  {'有効件数':>6}  {'総損益':>10}  {'勝率':>6}  {'平均損益':>8}")
    print("-" * 52)

    best_time = None
    best_pnl = float("-inf")

    for st in SELL_TIMES:
        col = f"pnl_{st.strftime('%H%M')}"
        sub = result_df[col].dropna()
        if len(sub) == 0:
            continue
        tot  = sub.sum()
        wr   = (sub > 0).mean() * 100
        avg  = sub.mean()
        label = st.strftime("%H:%M")
        marker = " ←" if tot > best_pnl else ""
        print(f"  売り{label}  {len(sub):>5}件  {tot:>+10,.0f}円  {wr:>5.1f}%  {avg:>+7.0f}円{marker}")
        if tot > best_pnl:
            best_pnl = tot
            best_time = st

    # ─ 最良時刻での詳細 ─
    if best_time:
        col = f"pnl_{best_time.strftime('%H%M')}"
        sub = result_df[[col, "bt", "date"]].dropna(subset=[col]).copy()
        sub = sub.rename(columns={col: "pnl"})
        sub["win"] = sub["pnl"] > 0

        print(f"\n--- 最良: 売り{best_time.strftime('%H:%M')} の詳細 ---")

        # BT帯別
        bins   = [0, 40, 60, 80, 101]
        labels = ["<40", "40-59", "60-79", "80+"]
        sub["bt_band"] = pd.cut(sub["bt"], bins=bins, labels=labels, right=False)
        print("BTスコア帯別:")
        for band, grp in sub.groupby("bt_band", observed=True):
            if len(grp) == 0:
                continue
            print(f"  BT{band}: {len(grp)}件  {grp['pnl'].sum():+,.0f}円  勝率{grp['win'].mean()*100:.1f}%")

        # 月別
        sub["month"] = pd.to_datetime(sub["date"]).dt.to_period("M")
        print("月別:")
        for m, grp in sub.groupby("month"):
            print(f"  {m}: {len(grp)}件  {grp['pnl'].sum():+,.0f}円  勝率{grp['win'].mean()*100:.1f}%")

if __name__ == "__main__":
    main()
