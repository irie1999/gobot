"""転換損益 CSV エクスポーター

OOS CSV (oos_raw_fold*.csv) の filled=0 行を対象に
09:09成行買い → 11:30前場引け成行売り のシミュレーションを行い
tenkan_pnl_{date}.csv を出力する。

使い方:
    python export_tenkan_pnl.py
    python export_tenkan_pnl.py --min-price 1000 --max-price 6000
    python export_tenkan_pnl.py --bt-min 30          # BT30以上のみ
    python export_tenkan_pnl.py --output my_result.csv
"""

import argparse
import csv
import glob
import os
import pickle
import sys
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("pandas が必要です: pip install pandas")
    sys.exit(1)


# ─── 設定 ────────────────────────────────────────────────────────────────────
BUY_TIME  = time(9, 9)    # 09:09以降の最初バーのOPENで買い
SELL_TIME = time(11, 30)  # 11:30以前の最後バーのOPENで売り
SLIP_PCT  = 0.0005        # 往復スリッページ 0.05%
FEE_PCT   = 0.001         # 手数料 片道 0.1%
QTY       = 100           # 単元株数


# ─── 5分足/1分足 ディレクトリ検出 ──────────────────────────────────────────
def _find_minute_dirs() -> tuple[Path | None, Path | None]:
    dir_5m: Path | None = None
    dir_1m: Path | None = None

    # 1分足
    env1m = os.environ.get("MINUTE_1M_DIR", "").strip()
    if env1m:
        dir_1m = Path(env1m)
    else:
        default_1m = Path.home() / ".jquants_cache" / "minute"
        if default_1m.exists():
            dir_1m = default_1m

    # 5分足
    env5m = os.environ.get("MINUTE_5M_DIR", "").strip()
    if env5m:
        dir_5m = Path(env5m)
    if dir_5m is None or not dir_5m.exists():
        here = Path(__file__).resolve().parent
        for candidate in [
            here / "data" / "stock_5min",
            here.parent / "stock_5min",
            here.parent / "stock_5min" / "data" / "stock_5min",
            Path("C:/Users/to732/OneDrive/ドキュメント/kabu station/stock_5min"),
        ]:
            try:
                if candidate.exists() and any(candidate.glob("*.pkl")):
                    dir_5m = candidate
                    break
            except Exception:
                pass

    return dir_5m, dir_1m


# ─── pkl 読み込み ──────────────────────────────────────────────────────────
def _load_pkl(pkl_path: Path) -> pd.DataFrame | None:
    if not pkl_path.exists():
        return None
    try:
        with open(pkl_path, "rb") as f:
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


def _sym_to_code(sym: str) -> str:
    return sym.upper().replace(".T", "") + "0"


# ─── キャッシュ付き足データ取得 ───────────────────────────────────────────
_bar_cache: dict[str, pd.DataFrame | None] = {}

def _get_bars(sym: str, dir_5m: Path | None, dir_1m: Path | None) -> pd.DataFrame | None:
    if sym in _bar_cache:
        return _bar_cache[sym]
    code = _sym_to_code(sym)
    df = None
    if dir_1m:
        df = _load_pkl(dir_1m / f"{code}_1m.pkl")
    if df is None and dir_5m:
        df = _load_pkl(dir_5m / f"{code}.pkl")
    _bar_cache[sym] = df
    return df


# ─── 転換シミュレーション ─────────────────────────────────────────────────
def simulate_tenkan(sym: str, d: date, dir_5m: Path | None, dir_1m: Path | None
                    ) -> tuple[float, float, float] | None:
    """(pnl, entry_p, exit_p) または None"""
    df = _get_bars(sym, dir_5m, dir_1m)
    if df is None:
        return None
    try:
        day_df = df[df.index.date == d]
    except Exception:
        return None
    if len(day_df) < 3:
        return None

    bar_times = [t.time() for t in day_df.index]

    # 買い: BUY_TIME 以降の最初バーの open
    buy_p = None
    for i, t in enumerate(bar_times):
        if t >= BUY_TIME:
            buy_p = float(day_df.iloc[i]["open"])
            break

    # 売り: SELL_TIME 以前の最後バーの open（後場除外）
    sell_p = None
    for i in range(len(bar_times) - 1, -1, -1):
        if bar_times[i] <= SELL_TIME:
            sell_p = float(day_df.iloc[i]["open"])
            break

    if not buy_p or not sell_p:
        return None

    actual_buy  = buy_p  * (1 + SLIP_PCT)
    actual_sell = sell_p * (1 - SLIP_PCT)
    pnl = (actual_sell - actual_buy) * QTY - (actual_buy + actual_sell) * QTY * FEE_PCT
    return pnl, actual_buy, actual_sell


# ─── メイン ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="転換損益 CSV エクスポーター")
    ap.add_argument("--min-price", type=float, default=1000.0,
                    help="エントリー価格下限 (default: 1000)")
    ap.add_argument("--max-price", type=float, default=6000.0,
                    help="エントリー価格上限 (default: 6000)")
    ap.add_argument("--bt-min", type=float, default=0.0,
                    help="BT スコア下限 (default: 0 = 全件)")
    ap.add_argument("--output", type=str, default="",
                    help="出力ファイル名 (default: tenkan_pnl_YYYY-MM-DD.csv)")
    args = ap.parse_args()

    today_str = datetime.now().strftime("%Y-%m-%d")
    out_path = args.output or f"tenkan_pnl_{today_str}.csv"

    # OOS CSV 読み込み
    csvs = sorted(glob.glob("oos_raw_fold*.csv"))
    if not csvs:
        print("[ERROR] oos_raw_fold*.csv が見つかりません。")
        print("  gobot フォルダで実行してください。")
        sys.exit(1)

    print(f"OOS CSV: {len(csvs)} 件 読み込み中...", flush=True)
    all_df = pd.concat([pd.read_csv(f) for f in csvs], ignore_index=True)
    all_df["entry_date"] = pd.to_datetime(all_df["entry_date"]).dt.date
    print(f"  全レコード: {len(all_df)}件", flush=True)

    # filled=0 (lss未約定) だけ抽出
    unfl_df = all_df[all_df["filled"] == 0].copy()
    print(f"  filled=0 (転換候補): {len(unfl_df)}件", flush=True)

    # BT スコアフィルター
    if args.bt_min > 0 and "bt_score" in unfl_df.columns:
        before = len(unfl_df)
        unfl_df = unfl_df[unfl_df["bt_score"] >= args.bt_min]
        print(f"  BT≥{args.bt_min:g} フィルター後: {len(unfl_df)}件 (除外: {before - len(unfl_df)}件)", flush=True)

    # 価格フィルター
    if "entry_p" in unfl_df.columns:
        before = len(unfl_df)
        ep = unfl_df["entry_p"].fillna(0).astype(float)
        mask = pd.Series(True, index=unfl_df.index)
        if args.min_price > 0:
            mask &= (ep == 0) | (ep >= args.min_price)
        if args.max_price > 0:
            mask &= (ep == 0) | (ep <= args.max_price)
        unfl_df = unfl_df[mask]
        print(f"  価格フィルター [{args.min_price:g}〜{args.max_price:g}] 後: {len(unfl_df)}件 (除外: {before - len(unfl_df)}件)", flush=True)

    # 分足データ DIR
    dir_5m, dir_1m = _find_minute_dirs()
    print(f"  5分足DIR: {dir_5m}", flush=True)
    print(f"  1分足DIR: {dir_1m}", flush=True)
    if dir_5m is None and dir_1m is None:
        print("[ERROR] 分足データディレクトリが見つかりません。")
        print("  MINUTE_5M_DIR 環境変数を設定してください。")
        sys.exit(1)

    # シミュレーション実行
    print(f"\nシミュレーション実行中 ({len(unfl_df)}件)...", flush=True)
    rows = []
    no_data = 0
    for idx, (_, rw) in enumerate(unfl_df.iterrows(), 1):
        if idx % 200 == 0:
            print(f"  進捗: {idx}/{len(unfl_df)}件", flush=True)
        res = simulate_tenkan(rw["symbol"], rw["entry_date"], dir_5m, dir_1m)
        if res is None:
            no_data += 1
            continue
        pnl, ep, xp = res
        bt_score = float(rw.get("bt_score", 0) or 0)
        if   bt_score >= 80: rank = "★★★"
        elif bt_score >= 60: rank = "★★"
        elif bt_score >= 40: rank = "★"
        else:                rank = "△"
        rows.append({
            "date":        str(rw["entry_date"]),
            "oos_month":   str(rw.get("oos_month", "")),
            "fold":        rw.get("fold", ""),
            "symbol":      rw.get("symbol", ""),
            "name":        rw.get("name", ""),
            "strategy":    rw.get("strategy", ""),
            "bt_score":    round(bt_score, 1),
            "rank":        rank,
            "lss_entry_p": round(float(rw.get("entry_p", 0) or 0), 1),
            "tenkan_buy_p":  round(ep, 1),
            "tenkan_sell_p": round(xp, 1),
            "pnl":         round(pnl, 0),
            "win":         1 if pnl > 0 else 0,
        })

    print(f"\n完了: {len(rows)}件シミュレーション / データなしスキップ: {no_data}件", flush=True)

    if not rows:
        print("[WARN] 結果が 0 件です。分足データを確認してください。")
        sys.exit(0)

    # 明細 CSV 出力
    fieldnames = ["date", "oos_month", "fold", "symbol", "name", "strategy",
                  "bt_score", "rank", "lss_entry_p", "tenkan_buy_p", "tenkan_sell_p",
                  "pnl", "win"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["date"]):
            w.writerow(r)

    print(f"\n[出力] {out_path}  ({len(rows)}件)")

    # ─── サマリー表示 ───────────────────────────────────────────────────────
    total_pnl = sum(r["pnl"] for r in rows)
    wins       = sum(r["win"] for r in rows)
    losses     = len(rows) - wins
    win_pnl    = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    loss_pnl   = sum(abs(r["pnl"]) for r in rows if r["pnl"] < 0)
    wr         = round(wins / len(rows) * 100, 1) if rows else 0
    pf         = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else 999.0

    print("\n─── 全体サマリー ───────────────────────────────────────")
    print(f"  取引数: {len(rows)}件  勝率: {wr}%  PF: {pf}")
    print(f"  総損益: {total_pnl:,.0f}円  勝ち: {win_pnl:,.0f}円  負け: {-loss_pnl:,.0f}円")

    # 月別サマリー
    by_month: dict[str, list] = defaultdict(list)
    for r in rows:
        by_month[r["oos_month"]].append(r)

    print("\n─── 月別サマリー ───────────────────────────────────────")
    print(f"{'月':<10} {'取引':>5} {'勝率':>7} {'PF':>6} {'損益':>10}")
    print("─" * 45)
    for m in sorted(by_month.keys()):
        mrs  = by_month[m]
        mn   = len(mrs)
        mw   = sum(r["win"] for r in mrs)
        mp   = sum(r["pnl"] for r in mrs)
        mwr  = round(mw / mn * 100, 1) if mn else 0
        mwpt = sum(r["pnl"] for r in mrs if r["pnl"] > 0)
        mlpt = sum(abs(r["pnl"]) for r in mrs if r["pnl"] < 0)
        mpf  = round(mwpt / mlpt, 2) if mlpt > 0 else 999.0
        print(f"{m:<10} {mn:>5}  {mwr:>6.1f}%  {mpf:>5.2f}  {mp:>10,.0f}円")

    print("─" * 45)
    print(f"{'合計':<10} {len(rows):>5}  {wr:>6.1f}%  {pf:>5.2f}  {total_pnl:>10,.0f}円")

    # BT別サマリー
    bt_bands = [(80, 9999, "BT80+"), (60, 79, "BT60-79"), (40, 59, "BT40-59"),
                (30, 39, "BT30-39"), (0, 29, "BT0-29")]
    print("\n─── BT スコア帯別サマリー ───────────────────────────────")
    print(f"{'帯':<10} {'取引':>5} {'勝率':>7} {'PF':>6} {'損益':>10}")
    print("─" * 45)
    for lo, hi, label in bt_bands:
        brs = [r for r in rows if lo <= r["bt_score"] <= hi]
        if not brs:
            continue
        bn   = len(brs)
        bw   = sum(r["win"] for r in brs)
        bp   = sum(r["pnl"] for r in brs)
        bwr  = round(bw / bn * 100, 1)
        bwpt = sum(r["pnl"] for r in brs if r["pnl"] > 0)
        blpt = sum(abs(r["pnl"]) for r in brs if r["pnl"] < 0)
        bpf  = round(bwpt / blpt, 2) if blpt > 0 else 999.0
        print(f"{label:<10} {bn:>5}  {bwr:>6.1f}%  {bpf:>5.2f}  {bp:>10,.0f}円")


if __name__ == "__main__":
    main()
