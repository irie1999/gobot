"""analyze_partial_exit_lss.py — lss損切りトレードの「利確手前反発」パターン定量調査。

#10: 「下落→利確届かず→反発損切り」= 損切りトレードの中で、ある程度ターゲットに向かって
から反転して損切りになったものを定量化する。

測定値: MFE%(最大有利変動 / ターゲット距離)
  lss はショートなので:
    MFE    = entry_p - min(約定後の5分足 low)   ← 下がるほど有利
    target_dist = entry_p - target_price         ← ターゲットまでの距離
    MFE%   = MFE / target_dist * 100

損切りトレードに対して MFE% を計算し、「X%以上まで達したが損切り」の件数・損失額を集計。

前提: `lss_trades.csv` が存在(.\\daily で LSS_TRADES_CSV=lss_trades.csv して生成済み)。
      stock_5min フォルダのローカル5分足データが使える環境で実行すること。

使い方:
  set LSS_TRADES_CSV=lss_trades.csv
  python analyze_partial_exit_lss.py
  python analyze_partial_exit_lss.py lss_trades.csv --bt-min 30
  python analyze_partial_exit_lss.py lss_trades.csv --bt-min 40 --workers 8
  python analyze_partial_exit_lss.py lss_trades.csv --mfe-thresh 30,50,70,90
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss損切りトレードのMFE%(利確手前到達度)分析")
ap.add_argument("csv", nargs="?",
                default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"),
                help="lss_trades.csv のパス(既定=LSS_TRADES_CSV or lss_trades.csv)")
ap.add_argument("--bt-min", type=float, default=30.0, help="BTスコア下限(既定30)")
ap.add_argument("--mfe-thresh", type=str, default="20,30,50,70,90",
                help="集計するMFE%%閾値(カンマ区切り。既定=20,30,50,70,90)")
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--source", choices=["auto", "local"], default="local")
args = ap.parse_args()

from daytrade_data import load_intraday_batch, split_by_day, DATA_DIR

THRESHOLDS = [float(x) for x in args.mfe_thresh.split(",") if x.strip()]

print(f"[info] DATA_DIR = {DATA_DIR}  (存在: {DATA_DIR.exists()})", flush=True)


def _load_csv() -> pd.DataFrame:
    p = Path(args.csv)
    if not p.exists():
        print(f"[error] {p} が見つかりません。先に .\\daily で LSS_TRADES_CSV=lss_trades.csv を実行してください。",
              file=sys.stderr)
        sys.exit(1)
    df = pd.read_csv(p, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    for col in ["entry_p", "stop_price", "target_price", "bt", "pnl"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _parse_time(hhmm: str) -> int:
    """'09:05' -> 545 (minutes since midnight)"""
    try:
        h, m = str(hhmm).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _build_cache(symbols: list[str]) -> dict[str, dict]:
    """銘柄リストの5分足を一括ロードして {sym: {date: DataFrame}} のキャッシュを返す。"""
    print(f"[キャッシュ構築] {len(symbols)}銘柄の5分足を読み込み中...", flush=True)

    # load_intraday_batch はローカルpklをシリアル読込みするが1銘柄1回で済む
    # 銘柄数が多い場合はThreadPoolExecutorで並列化
    def _load_one(sym: str):
        from daytrade_data import load_intraday
        return sym, load_intraday(sym, days=800, source=args.source)

    raw: dict[str, pd.DataFrame | None] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_load_one, sym): sym for sym in symbols}
        for fut in as_completed(futs):
            sym, df = fut.result()
            raw[sym] = df

    cache: dict[str, dict] = {}
    failed = 0
    for sym, df in raw.items():
        if df is not None and not df.empty:
            cache[sym] = split_by_day(df)
        else:
            failed += 1

    print(f"[info] 5分足ロード完了: {len(cache)}/{len(symbols)}銘柄  "
          f"(失敗/データなし: {failed}銘柄)", flush=True)

    if cache:
        # 最初のシンボルで利用可能日範囲を表示
        first_sym = next(iter(cache))
        keys = sorted(cache[first_sym].keys())
        if keys:
            print(f"[info] データ例 {first_sym}: {keys[0]} 〜 {keys[-1]}  ({len(keys)}日)",
                  flush=True)
    return cache


def _mfe_fast(row: pd.Series, cache: dict[str, dict]) -> float | None:
    """キャッシュから1トレードのMFE%を計算(I/Oなし)。"""
    sym = str(row.get("symbol", "")).strip()
    entry_date_raw = str(row.get("entry_date", "")).strip()
    entry_p = row.get("entry_p")
    target_p = row.get("target_price")
    entry_time = str(row.get("entry_time", "")).strip()

    if not sym or not entry_date_raw or pd.isna(entry_p) or pd.isna(target_p):
        return None

    target_dist = entry_p - target_p   # ショートなので entry > target
    if target_dist <= 0:
        return None

    by_day = cache.get(sym)
    if by_day is None:
        return None

    try:
        entry_date = pd.Timestamp(entry_date_raw).date()
    except Exception:
        return None

    day_bars = by_day.get(entry_date)
    if day_bars is None or day_bars.empty:
        return None

    # entry_time 以降のバーに限定(約定前のバーを除外)
    entry_min = _parse_time(entry_time) if entry_time and ":" in entry_time else 0
    if entry_min > 0:
        bar_times = day_bars.index.map(lambda t: t.hour * 60 + t.minute)
        day_bars = day_bars[bar_times >= entry_min]
    if day_bars.empty:
        return None

    min_low = day_bars["low"].min()
    mfe = entry_p - min_low   # ショートで下がるほどプラス
    return mfe / target_dist * 100


def main():
    df = _load_csv()
    print(f"[info] 総トレード数: {len(df)}")

    # BT下限フィルター
    if "bt" in df.columns:
        df = df[df["bt"] >= args.bt_min]
        print(f"[info] BT>={args.bt_min}: {len(df)}件")

    # 損切りトレードのみ
    if "reason" not in df.columns:
        print("[error] reason 列がありません", file=sys.stderr)
        sys.exit(1)
    stops = df[df["reason"] == "損切り"].copy()
    all_done = df[~df["reason"].isin(["発注中", "保有中"])].copy()

    total_done = len(all_done)
    total_stops = len(stops)
    total_pnl = all_done["pnl"].sum() if "pnl" in all_done.columns else 0
    stop_pnl = stops["pnl"].sum() if "pnl" in stops.columns else 0

    print(f"\n■ 全決済トレード: {total_done}件  総損益: {total_pnl:+,.0f}円")
    print(f"■ 損切りトレード: {total_stops}件 ({total_stops/total_done*100:.1f}%)  損切り損失計: {stop_pnl:+,.0f}円")

    if total_stops == 0:
        print("損切りトレードがゼロです。")
        return

    # ── 銘柄別に5分足を一括キャッシュ(高速化の核心) ──
    unique_syms = sorted(stops["symbol"].dropna().unique().tolist())
    cache = _build_cache(unique_syms)

    if not cache:
        print("[warn] 5分足データが1銘柄もロードできませんでした。")
        print(f"       DATA_DIR={DATA_DIR} に *.pkl ファイルが存在するか確認してください。")
        return

    # ── MFE計算(キャッシュ参照のみ、I/Oなし) ──
    print(f"\n[MFE計算] {total_stops}件 × キャッシュ参照...", flush=True)
    mfe_list: list[float | None] = []
    date_miss = 0
    for row in stops.itertuples(index=False):
        r = pd.Series(row._asdict())
        val = _mfe_fast(r, cache)
        mfe_list.append(val)
        if val is None:
            # 5分足はあるが日付が見つからないケース
            sym = str(row.symbol).strip()
            if sym in cache:
                try:
                    ed = pd.Timestamp(str(row.entry_date)).date()
                    if ed not in cache[sym]:
                        date_miss += 1
                except Exception:
                    pass

    stops = stops.copy()
    stops["mfe_pct"] = mfe_list
    valid = stops.dropna(subset=["mfe_pct"])
    print(f"[info] MFE計算成功: {len(valid)}/{total_stops}件")

    if date_miss > 0:
        print(f"[info] 日付不一致(5分足の期間外): {date_miss}件 "
              f"(2024-07以前のトレードは5分足データなし)", flush=True)

    if valid.empty:
        print("[warn] MFE計算可能なトレードがゼロ。")
        print("       entry_date が 2024-07 以前か、stock_5min に該当銘柄がない可能性。")
        # 診断: 最初の数件を表示
        sample = stops.head(5)
        print("\n[診断] 最初の5件の損切りトレード:")
        for _, r in sample.iterrows():
            sym = str(r.get("symbol", "")).strip()
            ed_raw = str(r.get("entry_date", ""))
            in_cache = sym in cache
            date_ok = False
            if in_cache:
                try:
                    ed = pd.Timestamp(ed_raw).date()
                    date_ok = ed in cache[sym]
                except Exception:
                    pass
            print(f"  {sym}  {ed_raw}  5分足キャッシュ:{in_cache}  日付OK:{date_ok}")
        return

    # ── 全体分布 ──
    print("\n" + "=" * 65)
    print("■ MFE% 分布 (損切りトレードがターゲット方向に何%進んだか)")
    print("=" * 65)
    print(f"  平均MFE%  : {valid['mfe_pct'].mean():.1f}%")
    print(f"  中央値MFE%: {valid['mfe_pct'].median():.1f}%")
    print(f"  最大MFE%  : {valid['mfe_pct'].max():.1f}%")
    bins = [-999, 0, 20, 30, 50, 70, 90, 100, 9999]
    labels = ["<0%(逆行のみ)", "0-20%", "20-30%", "30-50%", "50-70%", "70-90%", "90-100%", ">100%(超過)"]
    valid = valid.copy()
    valid["mfe_bin"] = pd.cut(valid["mfe_pct"], bins=bins, labels=labels, right=False)
    dist = valid.groupby("mfe_bin", observed=True)["pnl"].agg(["count", "sum"])
    dist.columns = ["件数", "損益計"]
    dist["割合%"] = dist["件数"] / len(valid) * 100
    print(f"\n{'区分':<18} {'件数':>6} {'割合%':>7} {'損益計':>12}")
    print("-" * 48)
    for label, row2 in dist.iterrows():
        print(f"  {str(label):<16} {int(row2['件数']):>6} {row2['割合%']:>6.1f}% {row2['損益計']:>+12,.0f}円")

    # ── 閾値別集計(「X%以上まで達したが損切り」) ──
    print("\n" + "=" * 65)
    print("■ 「MFE >= X% 達したが損切り」= ターゲット手前で反転したトレード")
    print("=" * 65)
    print(f"  {'閾値':>6}  {'件数':>6}  {'全損切%':>8}  {'損失合計':>12}  {'平均損失':>10}")
    print("-" * 60)
    for thr in THRESHOLDS:
        subset = valid[valid["mfe_pct"] >= thr]
        cnt = len(subset)
        pct_of_stops = cnt / len(valid) * 100
        pnl_sum = subset["pnl"].sum()
        avg_pnl = pnl_sum / cnt if cnt > 0 else 0
        print(f"  {thr:>5.0f}%  {cnt:>6}  {pct_of_stops:>7.1f}%  {pnl_sum:>+12,.0f}円  {avg_pnl:>+10,.0f}円")

    # ── 戦略別 (MFE>=50%) ──
    print("\n" + "=" * 65)
    print("■ 戦略別 (MFE >= 50%以上達して損切り)")
    print("=" * 65)
    subset50 = valid[valid["mfe_pct"] >= 50.0]
    if not subset50.empty and "strategy" in subset50.columns:
        sg = subset50.groupby("strategy")["pnl"].agg(["count", "sum", "mean"])
        sg.columns = ["件数", "損益計", "平均"]
        sg = sg.sort_values("損益計")
        print(f"  {'戦略':<10} {'件数':>6} {'損益計':>12} {'平均':>10}")
        print("-" * 45)
        for strat, row2 in sg.iterrows():
            print(f"  {strat:<10} {int(row2['件数']):>6} {row2['損益計']:>+12,.0f}円 {row2['平均']:>+10,.0f}円")
    else:
        print("  (MFE>=50%の損切りなし)")

    # ── 結論メモ ──
    cnt_50 = len(valid[valid["mfe_pct"] >= 50.0])
    pnl_50 = valid[valid["mfe_pct"] >= 50.0]["pnl"].sum()
    print("\n" + "=" * 65)
    print("■ 要約")
    print("=" * 65)
    print(f"  損切り {total_stops}件中 {cnt_50}件({cnt_50/total_stops*100:.1f}%)が")
    print(f"  ターゲット距離の50%以上下落後に反転して損切り。")
    print(f"  その損失合計: {pnl_50:+,.0f}円")
    print()
    print("  → この数が多い場合の対策候補:")
    print("     ・利確を手前(50-70%地点)に引き上げる(部分利確)")
    print("     ・delay1 の効果で損切りを遅らせる(既に適用済み)")
    print("     ・トレーリングストップ(MFE>X%で損切りをbreak-evenに移動)")


if __name__ == "__main__":
    main()
