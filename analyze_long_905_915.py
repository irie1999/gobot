"""
analyze_long_905_915.py  — 未約定lssを9:05ロング転換→9:15売却シミュレーション

使い方 (Windowsで実行):
  python analyze_long_905_915.py

前提:
  - OOS CSVファイル群が ./oos_raw_fold*.csv にあること (または --csv-glob で指定)
  - stock_5min フォルダが隣接 or MINUTE_5M_DIR 環境変数で指定済み

出力:
  - コンソールに集計結果
  - long_905_sim_result.html (ブラウザで確認)
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

# ─── 9:05→9:15 ロングシミュレーション ────────────────────────────────────────

def sim_long_905(sym: str, d: date) -> dict | None:
    """
    その日の未約定ショート銘柄について:
      9:05 open で買い → 9:15 open で売り
    Returns: {buy_p, sell_p, pnl, gap_pct} or None
    """
    df5 = load_5m(sym)
    if df5 is None:
        return None

    day_bars = df5[df5.index.date == d].copy()
    if len(day_bars) < 4:
        return None

    bar_times = [t.time() for t in day_bars.index]

    # 9:05 バー（寄り後2本目）と 9:15 バー（寄り後4本目）を探す
    t905 = dtime(9, 5)
    t915 = dtime(9, 15)
    i905 = i915 = None
    for i, bt in enumerate(bar_times):
        if bt == t905:
            i905 = i
        if bt == t915:
            i915 = i

    # 見つからなければ index で代替
    if i905 is None:
        i905 = 1  # 2本目
    if i915 is None:
        i915 = 3  # 4本目

    if i915 >= len(day_bars):
        return None

    buy_p  = float(day_bars.iloc[i905]["open"])   # 9:05 open で成行買い
    sell_p = float(day_bars.iloc[i915]["open"])   # 9:15 open で成行売り

    if buy_p <= 0 or sell_p <= 0:
        return None

    # 寄り値（gap確認用）
    open0 = float(day_bars.iloc[0]["open"]) if len(day_bars) > 0 else buy_p
    gap_pct = (open0 / buy_p - 1) * 100  # 9:05時点から見た寄り値差(参考)

    buy_eff  = buy_p  * (1.0 + SLIP)  # 成行買いは高くなる
    sell_eff = sell_p * (1.0 - SLIP)  # 成行売りは安くなる
    fee = (buy_eff + sell_eff) * QTY * FEE
    pnl = (sell_eff - buy_eff) * QTY - fee

    return dict(buy_p=buy_p, sell_p=sell_p, pnl=pnl, open0=open0)

# ─── メイン ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-glob", default="oos_raw_fold*.csv",
                    help="OOS CSVのglob (デフォルト: oos_raw_fold*.csv)")
    ap.add_argument("--bt-min", type=float, default=0,
                    help="BTスコア下限フィルター (デフォルト: 0=全件)")
    ap.add_argument("--no-browser", action="store_true")
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

    # シミュレーション
    rows = []
    for _, rec in unfilled.iterrows():
        sym = rec["symbol"]
        d   = rec["entry_date"]
        bt  = rec["bt_score"]
        r = sim_long_905(sym, d)
        if r is None:
            rows.append(dict(sym=sym, date=d, bt=bt, result="no_data"))
        else:
            rows.append(dict(
                sym=sym, date=d, bt=bt,
                buy_p=r["buy_p"], sell_p=r["sell_p"],
                pnl=r["pnl"],
                win=r["pnl"] > 0,
                result="ok",
            ))

    result_df = pd.DataFrame(rows)
    ok_df = result_df[result_df["result"] == "ok"].copy()
    nd = (result_df["result"] == "no_data").sum()
    print(f"計算成功: {len(ok_df)} / {len(unfilled)} 件 (no_data: {nd})")

    if len(ok_df) == 0:
        print("データなし - stock_5min の pkl が読めていない可能性")
        return

    # ─ 集計 ─
    n   = len(ok_df)
    tot = ok_df["pnl"].sum()
    wr  = ok_df["win"].mean() * 100
    avg = ok_df["pnl"].mean()

    print(f"\n=== 9:05 ロング転換 → 9:15 売却 シミュレーション ===")
    print(f"件数:     {n}")
    print(f"総損益:   {tot:+,.0f} 円")
    print(f"勝率:     {wr:.1f}%")
    print(f"平均損益: {avg:+,.0f} 円/件")
    print(f"最大勝ち: {ok_df['pnl'].max():+,.0f} 円")
    print(f"最大負け: {ok_df['pnl'].min():+,.0f} 円")

    # BT帯別
    print("\n--- BTスコア帯別 ---")
    bins   = [0, 40, 60, 80, 101]
    labels = ["<40", "40-59", "60-79", "80+"]
    ok_df["bt_band"] = pd.cut(ok_df["bt"], bins=bins, labels=labels, right=False)
    for band, grp in ok_df.groupby("bt_band", observed=True):
        if len(grp) == 0:
            continue
        print(f"  BT{band}: {len(grp)}件  PnL={grp['pnl'].sum():+,.0f}円  勝率{grp['win'].mean()*100:.1f}%")

    # 月別
    print("\n--- 月別 ---")
    ok_df["month"] = pd.to_datetime(ok_df["date"]).dt.to_period("M")
    for m, grp in ok_df.groupby("month"):
        print(f"  {m}: {len(grp)}件  {grp['pnl'].sum():+,.0f}円  勝率{grp['win'].mean()*100:.1f}%")

    # 日別上下5日
    daily = ok_df.groupby("date")["pnl"].sum()
    print(f"\n--- 日別 (損失ワースト5) ---")
    for d, v in daily.nsmallest(5).items():
        print(f"  {d}: {v:+,.0f}円")
    print(f"--- 日別 (利益ベスト5) ---")
    for d, v in daily.nlargest(5).items():
        print(f"  {d}: {v:+,.0f}円")

    # ─ HTML出力 ─
    _build_html(ok_df, tot, wr, n, avg, args)

def _build_html(ok_df, tot, wr, n, avg, args):
    monthly = ok_df.groupby(pd.to_datetime(ok_df["date"]).dt.to_period("M")).agg(
        trades=("pnl", "count"), pnl=("pnl", "sum"), wr=("win", "mean")
    ).reset_index()
    monthly["wr"] = (monthly["wr"] * 100).round(1)

    # BT帯別
    bins   = [0, 40, 60, 80, 101]
    labels = ["<40", "40-59", "60-79", "80+"]
    ok_df2 = ok_df.copy()
    ok_df2["bt_band"] = pd.cut(ok_df2["bt"], bins=bins, labels=labels, right=False)
    bt_tbl = ok_df2.groupby("bt_band", observed=True).agg(
        trades=("pnl", "count"), pnl=("pnl", "sum"), wr=("win", "mean")
    ).reset_index()
    bt_tbl["wr"] = (bt_tbl["wr"] * 100).round(1)

    color = "green" if tot >= 0 else "red"

    rows_monthly = ""
    for _, r in monthly.iterrows():
        c = "green" if r["pnl"] >= 0 else "red"
        rows_monthly += f"<tr><td>{r['month']}</td><td>{int(r['trades'])}</td><td style='color:{c}'>{r['pnl']:+,.0f}</td><td>{r['wr']}%</td></tr>\n"

    rows_bt = ""
    for _, r in bt_tbl.iterrows():
        c = "green" if r["pnl"] >= 0 else "red"
        rows_bt += f"<tr><td>BT{r['bt_band']}</td><td>{int(r['trades'])}</td><td style='color:{c}'>{r['pnl']:+,.0f}</td><td>{r['wr']}%</td></tr>\n"

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>9:05→9:15 ロング転換シミュレーション</title>
<style>
body{{font-family:sans-serif;padding:20px;}}
h1{{font-size:1.4em;}}
.summary{{display:flex;gap:20px;margin:20px 0;}}
.card{{border:1px solid #ddd;border-radius:8px;padding:16px;min-width:120px;text-align:center;}}
.card .val{{font-size:1.8em;font-weight:bold;color:{color};}}
.card .lbl{{font-size:.8em;color:#666;}}
table{{border-collapse:collapse;margin:10px 0;}}
th,td{{border:1px solid #ddd;padding:6px 12px;text-align:right;}}
th{{background:#f5f5f5;text-align:center;}}
td:first-child{{text-align:left;}}
</style></head><body>
<h1>9:05 ロング転換 → 9:15 売却 シミュレーション</h1>
<p>filled=0（未約定ショート）銘柄を9:05に成行ロング、9:15に成行売り（スリッページ0.05%+手数料0.1%往復）</p>
<div class="summary">
  <div class="card"><div class="val">{tot:+,.0f}</div><div class="lbl">総損益 (円)</div></div>
  <div class="card"><div class="val">{wr:.1f}%</div><div class="lbl">勝率</div></div>
  <div class="card"><div class="val">{n}</div><div class="lbl">件数</div></div>
  <div class="card"><div class="val">{avg:+,.0f}</div><div class="lbl">平均損益/件</div></div>
</div>

<h2>月別</h2>
<table><tr><th>月</th><th>件数</th><th>損益(円)</th><th>勝率</th></tr>
{rows_monthly}</table>

<h2>BTスコア帯別</h2>
<table><tr><th>BT帯</th><th>件数</th><th>損益(円)</th><th>勝率</th></tr>
{rows_bt}</table>

<h2>取引明細</h2>
{ok_df[['date','sym','bt','buy_p','sell_p','pnl']].sort_values('date').to_html(index=False, float_format=lambda x: f'{x:,.0f}')}
</body></html>"""

    outfile = "long_905_sim_result.html"
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML出力: {outfile}")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(outfile)

if __name__ == "__main__":
    main()
