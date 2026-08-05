"""
analyze_long_905_915.py  — 未約定lssのロング転換: 買い時刻×売り時刻 2次元スイープ

使い方 (Windowsで実行):
  python analyze_long_905_915.py

  # BTスコアフィルター
  python analyze_long_905_915.py --bt-min 60

前提:
  - OOS CSVファイル群が ./oos_raw_fold*.csv にあること
  - stock_1min フォルダが stock_5min の隣 or MINUTE_1M_DIR 環境変数で指定済み
  - 1分足がなければ5分足にフォールバック
"""
from __future__ import annotations

import argparse
import glob
import os
import pickle
import sys
from datetime import date, time as dtime
from pathlib import Path

import pandas as pd

# ─── パス設定 ────────────────────────────────────────────────────────────────

def _resolve_dir(env_var: str, subfolder: str, globs: list[str]) -> Path:
    env = os.environ.get(env_var)
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidates = [
        here / "data" / subfolder,
        here.parent / subfolder,
        here.parent / subfolder / "data" / subfolder,
    ]
    for c in candidates:
        try:
            if c.exists() and any(p for g in globs for p in c.glob(g)):
                return c
        except Exception:
            continue
    return candidates[1]  # fallback: 隣接フォルダ

DATA_DIR_1M = _resolve_dir("MINUTE_1M_DIR", "stock_1min", ["*.pkl"])
DATA_DIR_5M = _resolve_dir("MINUTE_5M_DIR", "stock_5min", ["*.pkl"])

QTY  = 100
FEE  = 0.001    # 片道0.1%
SLIP = 0.0005   # 0.05% (成行)

# ─── データロード ──────────────────────────────────────────────────────────────

_cache: dict[str, pd.DataFrame | None] = {}

def _yf_to_jq(sym: str) -> str:
    return sym.upper().replace(".T", "") + "0"

def _load_pkl(sym: str, data_dir: Path) -> pd.DataFrame | None:
    code = _yf_to_jq(sym)
    pkl = data_dir / f"{code}.pkl"
    if not pkl.exists():
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
        return df
    except Exception as e:
        print(f"  [warn] {sym} load error: {e}", file=sys.stderr)
        return None

def load_bars(sym: str) -> pd.DataFrame | None:
    """1分足を優先し、なければ5分足を使う。"""
    key = sym
    if key in _cache:
        return _cache[key]
    df = _load_pkl(sym, DATA_DIR_1M)
    if df is None:
        df = _load_pkl(sym, DATA_DIR_5M)
    _cache[key] = df
    return df

# ─── スイープ定義 ──────────────────────────────────────────────────────────────

# 買い候補時刻 (「この時刻のbar openで成行買い」= その時点で未約定と判断)
BUY_TIMES = [
    dtime(9,  1), dtime(9,  2), dtime(9,  3), dtime(9,  4),
    dtime(9,  5), dtime(9,  6), dtime(9,  7), dtime(9,  8),
    dtime(9,  9), dtime(9, 10), dtime(9, 15), dtime(9, 20),
    dtime(9, 25), dtime(9, 30),
]

# 売り候補時刻 (この時刻のbar openで成行売り)
SELL_TIMES = [
    dtime(9, 30), dtime(9, 45), dtime(10, 0), dtime(10, 30),
    dtime(11, 0), dtime(11, 30),
]

def _bar_open_at(day_bars: pd.DataFrame, target: dtime) -> float | None:
    """その日のbarsから target 時刻のopen価格を返す。なければ None。"""
    bar_times = [t.time() for t in day_bars.index]
    for i, bt in enumerate(bar_times):
        if bt == target:
            return float(day_bars.iloc[i]["open"])
    return None

def sim_sweep(sym: str, d: date) -> dict | None:
    """
    (buy_time, sell_time) の全組み合わせで損益を計算。
    Returns: {(buy_time, sell_time): pnl} or None
    """
    df = load_bars(sym)
    if df is None:
        return None
    day_bars = df[df.index.date == d]
    if len(day_bars) < 5:
        return None

    result = {}
    for bt in BUY_TIMES:
        buy_p = _bar_open_at(day_bars, bt)
        if buy_p is None or buy_p <= 0:
            continue
        buy_eff = buy_p * (1.0 + SLIP)
        for st in SELL_TIMES:
            if st <= bt:
                continue
            sell_p = _bar_open_at(day_bars, st)
            if sell_p is None or sell_p <= 0:
                result[(bt, st)] = None
                continue
            sell_eff = sell_p * (1.0 - SLIP)
            fee = (buy_eff + sell_eff) * QTY * FEE
            result[(bt, st)] = (sell_eff - buy_eff) * QTY - fee
    return result

# ─── メイン ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-glob", default="oos_raw_fold*.csv")
    ap.add_argument("--bt-min", type=float, default=0,
                    help="BTスコア下限フィルター (デフォルト: 0=全件)")
    args = ap.parse_args()

    # CSV読み込み
    csv_files = sorted(glob.glob(args.csv_glob))
    if not csv_files:
        print(f"[error] CSV not found: {args.csv_glob}")
        sys.exit(1)
    print(f"CSV: {len(csv_files)} ファイル")

    dfs = [pd.read_csv(f) for f in csv_files]
    all_df = pd.concat(dfs, ignore_index=True)
    all_df["entry_date"] = pd.to_datetime(all_df["entry_date"]).dt.date

    unfilled = all_df[all_df["filled"] == 0].copy()
    if args.bt_min > 0:
        unfilled = unfilled[unfilled["bt_score"] >= args.bt_min]

    print(f"未約定レコード: {len(unfilled)} 件 (BT≥{args.bt_min})")
    print(f"1分足DIR: {DATA_DIR_1M}  (存在: {DATA_DIR_1M.exists()})")
    print(f"5分足DIR: {DATA_DIR_5M}  (存在: {DATA_DIR_5M.exists()})")

    # シミュレーション
    # pnl_tbl[(buy_time, sell_time)] = [pnl, pnl, ...]
    from collections import defaultdict
    pnl_tbl: dict[tuple, list[float]] = defaultdict(list)
    no_data = 0

    for _, rec in unfilled.iterrows():
        sym = rec["symbol"]
        d   = rec["entry_date"]
        r = sim_sweep(sym, d)
        if r is None:
            no_data += 1
            continue
        for key, pnl in r.items():
            if pnl is not None:
                pnl_tbl[key].append(pnl)

    n_ok = len(unfilled) - no_data
    print(f"計算成功: {n_ok} / {len(unfilled)} 件\n")

    # ─ 2次元サマリー: 買い時刻(行) × 売り時刻(列) の総損益 ─
    header = "買い＼売り  " + "  ".join(f"{st.strftime('%H:%M'):>7}" for st in SELL_TIMES)
    print(header)
    print("-" * len(header))

    best_pnl = float("-inf")
    best_key = None

    for bt in BUY_TIMES:
        row = f"  買{bt.strftime('%H:%M')}  "
        for st in SELL_TIMES:
            key = (bt, st)
            vals = pnl_tbl.get(key, [])
            if not vals:
                row += f"{'---':>9}  "
            else:
                tot = sum(vals)
                row += f"{tot:>+9,.0f}  "
                if tot > best_pnl:
                    best_pnl = tot
                    best_key = key
        print(row)

    # ─ 最良の組み合わせの詳細 ─
    if best_key:
        bt, st = best_key
        vals = pnl_tbl[best_key]
        n = len(vals)
        wr = sum(1 for v in vals if v > 0) / n * 100
        print(f"\n★ 最良: 買い{bt.strftime('%H:%M')} → 売り{st.strftime('%H:%M')}")
        print(f"  件数={n}  総損益={sum(vals):+,.0f}円  勝率={wr:.1f}%  平均={sum(vals)/n:+.0f}円")

    # ─ 買い時刻別サマリー (売り固定=11:00 or 最良売り時刻) ─
    fixed_sell = st  # 最良売り時刻
    print(f"\n--- 買い時刻別サマリー (売り固定 {fixed_sell.strftime('%H:%M')}) ---")
    print(f"{'買い':>8}  {'件数':>5}  {'総損益':>10}  {'勝率':>6}  {'平均':>7}")
    for bt in BUY_TIMES:
        key = (bt, fixed_sell)
        vals = pnl_tbl.get(key, [])
        if not vals:
            continue
        n = len(vals)
        tot = sum(vals)
        wr = sum(1 for v in vals if v > 0) / n * 100
        marker = " ★" if (bt, fixed_sell) == best_key else ""
        print(f"  買{bt.strftime('%H:%M')}  {n:>5}件  {tot:>+10,.0f}円  {wr:>5.1f}%  {tot/n:>+6.0f}円{marker}")

if __name__ == "__main__":
    main()
