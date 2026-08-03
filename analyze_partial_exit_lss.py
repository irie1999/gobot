"""analyze_partial_exit_lss.py — lss損切りトレードの「利確手前反発」パターン定量調査。

#10: 「下落→利確届かず→反発損切り」= 損切りトレードの中で、ある程度ターゲットに向かって
から反転して損切りになったものを定量化する。

MFE (Maximum Favorable Excursion) を R倍数で計測:
  lss はショートなので:
    MFE      = entry_p - min(約定後の5分足 low)   ← 下がるほど有利
    stop_dist = exit_p - entry_p                  ← 実際の損切り幅(常に正)
    MFE_R    = MFE / stop_dist                   ← 何R分有利に動いたか

  lss は通常 2R設定(target = 2×stop_dist)なので:
    MFE_R >= 2.0 → ターゲット到達済みで損切り
    MFE_R >= 1.0 → 損切り幅と同じだけ有利に動いてから損切り
    MFE_R < 0    → 最初から逆行(一度も有利にならなかった)

使い方:
  python analyze_partial_exit_lss.py lss_trades.csv --bt-min 30 --workers 8
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss損切りトレードのMFE R倍数分析")
ap.add_argument("csv", nargs="?",
                default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"),
                help="lss_trades.csv のパス")
ap.add_argument("--bt-min", type=float, default=30.0, help="BTスコア下限(既定30)")
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--source", choices=["auto", "local"], default="local")
args = ap.parse_args()

from daytrade_data import load_intraday, split_by_day, DATA_DIR

print(f"[info] DATA_DIR = {DATA_DIR}  (存在: {DATA_DIR.exists()})", flush=True)


def _load_csv() -> pd.DataFrame:
    p = Path(args.csv)
    if not p.exists():
        print(f"[error] {p} が見つかりません", file=sys.stderr)
        sys.exit(1)
    df = pd.read_csv(p, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    for col in ["entry_p", "exit_p", "stop_price", "target_price", "bt", "pnl"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _parse_time(hhmm: str) -> int:
    try:
        h, m = str(hhmm).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _build_cache(symbols: list[str]) -> dict[str, dict]:
    print(f"[キャッシュ構築] {len(symbols)}銘柄の5分足を読み込み中...", flush=True)

    def _load_one(sym: str):
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

    print(f"[info] 5分足ロード完了: {len(cache)}/{len(symbols)}銘柄  (失敗: {failed}銘柄)", flush=True)
    if cache:
        first_sym = next(iter(cache))
        keys = sorted(cache[first_sym].keys())
        if keys:
            print(f"[info] データ例 {first_sym}: {keys[0]} 〜 {keys[-1]}  ({len(keys)}日)", flush=True)
    return cache


def _day_min_low(sym: str, entry_date_raw: str, entry_time: str, cache: dict) -> float | None:
    """5分足から約定以降の日中最安値を返す。取得失敗時は None。"""
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

    entry_min = _parse_time(entry_time) if entry_time and ":" in str(entry_time) else 0
    if entry_min > 0:
        bar_times = day_bars.index.map(lambda t: t.hour * 60 + t.minute)
        day_bars = day_bars[bar_times >= entry_min]
    if day_bars.empty:
        return None

    val = day_bars["low"].min()
    return float(val) if pd.notna(val) else None


def main():
    df = _load_csv()
    print(f"[info] 総トレード数: {len(df)}")

    if "bt" in df.columns:
        df = df[df["bt"] >= args.bt_min]
        print(f"[info] BT>={args.bt_min}: {len(df)}件")

    if "reason" not in df.columns:
        print("[error] reason 列がありません", file=sys.stderr)
        sys.exit(1)

    stops = df[df["reason"] == "損切り"].copy()
    all_done = df[~df["reason"].isin(["発注中", "保有中"])].copy()

    total_done = len(all_done)
    total_stops = len(stops)
    total_pnl = all_done["pnl"].sum() if "pnl" in all_done.columns else 0
    stop_pnl  = stops["pnl"].sum()  if "pnl" in stops.columns  else 0

    print(f"\n■ 全決済トレード: {total_done}件  総損益: {total_pnl:+,.0f}円")
    print(f"■ 損切りトレード: {total_stops}件 ({total_stops/total_done*100:.1f}%)  "
          f"損切り損失計: {stop_pnl:+,.0f}円")

    if total_stops == 0:
        print("損切りトレードがゼロです。")
        return

    # ── 銘柄別5分足キャッシュ ──
    unique_syms = sorted(stops["symbol"].dropna().unique().tolist())
    cache = _build_cache(unique_syms)
    if not cache:
        print(f"[error] 5分足データが0銘柄。DATA_DIR={DATA_DIR} を確認してください。")
        return

    # ── MFE計算 ──────────────────────────────────────────────────────
    print(f"\n[MFE計算] {total_stops}件 × キャッシュ参照...", flush=True)

    mfe_r_list   : list[float | None] = []
    mfe_abs_list : list[float | None] = []
    ok = 0
    fail_no_data = 0
    fail_no_stop = 0

    for row in stops.itertuples(index=False):
        sym        = str(getattr(row, "symbol", "")).strip()
        entry_date = str(getattr(row, "entry_date", "")).strip()
        entry_p    = float(getattr(row, "entry_p",  0) or 0)
        exit_p     = float(getattr(row, "exit_p",   0) or 0)
        entry_time = str(getattr(row, "entry_time", "") or "")

        # stop_dist = 実際の損切り幅(short なので exit > entry → 正)
        stop_dist = exit_p - entry_p

        min_low = _day_min_low(sym, entry_date, entry_time, cache)
        if min_low is None or entry_p <= 0:
            fail_no_data += 1
            mfe_r_list.append(None)
            mfe_abs_list.append(None)
            continue

        mfe = entry_p - min_low   # 下がるほど正(ショート有利)

        if stop_dist <= 0:
            # exit_p <= entry_p: データ異常(short 損切りなら必ず exit > entry)
            fail_no_stop += 1
            mfe_r_list.append(None)
            mfe_abs_list.append(None)
            continue

        mfe_r_list.append(mfe / stop_dist)
        mfe_abs_list.append(mfe / entry_p * 100)
        ok += 1

    stops = stops.copy()
    stops["mfe_r"]   = mfe_r_list
    stops["mfe_pct"] = mfe_abs_list   # entry比%

    print(f"[info] MFE計算成功: {ok}/{total_stops}件", flush=True)
    if fail_no_data:
        print(f"[info]  5分足なし/日付なし: {fail_no_data}件")
    if fail_no_stop:
        print(f"[info]  stop_dist異常(exit≤entry): {fail_no_stop}件")

    valid = stops.dropna(subset=["mfe_r"])
    if valid.empty:
        print("[warn] MFE計算できたトレードがゼロ。")
        _show_sample_debug(stops, cache)
        return

    # ── MFE R倍数の分布 ──────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("■ MFE Rレシオ分布  (lss 2R設定: R>=2.0 ≈ターゲット到達済み)")
    print("  MFE_R = (entry - 日中最安値) / (exit - entry)")
    print("        R<0: 最初から逆行    R=1: 損切幅と同じ有利動き")
    print("        R=2: ターゲット相当  R>2: ターゲット超過")
    print("=" * 65)
    print(f"  平均R  : {valid['mfe_r'].mean():.2f}")
    print(f"  中央値R: {valid['mfe_r'].median():.2f}")
    print(f"  最大R  : {valid['mfe_r'].max():.2f}")

    bins   = [-99, 0, 0.5, 1.0, 1.5, 2.0, 3.0, 99]
    labels = ["R<0(逆行のみ)", "0≤R<0.5", "0.5≤R<1", "1≤R<1.5", "1.5≤R<2", "2≤R<3", "R≥3(大幅超過)"]
    valid = valid.copy()
    valid["r_bin"] = pd.cut(valid["mfe_r"], bins=bins, labels=labels, right=False)
    dist = valid.groupby("r_bin", observed=True)["pnl"].agg(["count", "sum"])
    dist.columns = ["件数", "損益計"]
    dist["割合%"] = dist["件数"] / len(valid) * 100

    print(f"\n{'区分':<16} {'件数':>6} {'割合%':>7} {'損益計':>13}")
    print("-" * 48)
    for label, row2 in dist.iterrows():
        print(f"  {str(label):<14} {int(row2['件数']):>6} {row2['割合%']:>6.1f}% "
              f"{row2['損益計']:>+12,.0f}円")

    # ── 閾値別集計 ──────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("■ 「R >= X まで達したが損切り」= 有利に動いてから反転したトレード")
    print("=" * 65)
    print(f"  {'閾値':>6}  {'件数':>6}  {'全損切%':>8}  {'損失合計':>13}  {'平均損失':>10}")
    print("-" * 62)
    for thr in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        sub = valid[valid["mfe_r"] >= thr]
        cnt = len(sub)
        pct = cnt / len(valid) * 100
        pnl_s = sub["pnl"].sum()
        avg   = pnl_s / cnt if cnt > 0 else 0
        label = f"R>={thr:.1f}"
        label += " ≈目標到達" if thr >= 2.0 else ""
        print(f"  {label:<16} {cnt:>6}  {pct:>7.1f}%  {pnl_s:>+13,.0f}円  {avg:>+10,.0f}円")

    # ── 戦略別 (R>=2.0: ターゲット到達) ──────────────────────────────
    print("\n" + "=" * 65)
    print("■ 戦略別 (R >= 2.0 = ターゲット到達済みで損切り)")
    print("=" * 65)
    sub2 = valid[valid["mfe_r"] >= 2.0]
    if not sub2.empty and "strategy" in sub2.columns:
        sg = sub2.groupby("strategy")["pnl"].agg(["count", "sum", "mean"])
        sg.columns = ["件数", "損益計", "平均"]
        sg = sg.sort_values("損益計")
        print(f"  {'戦略':<10} {'件数':>6} {'損益計':>13} {'平均':>10}")
        print("-" * 46)
        for strat, r2 in sg.iterrows():
            print(f"  {strat:<10} {int(r2['件数']):>6} {r2['損益計']:>+13,.0f}円 "
                  f"{r2['平均']:>+10,.0f}円")
    else:
        print("  (R>=2.0の損切りなし)")

    # ── 要約 ─────────────────────────────────────────────────────────
    cnt_2 = len(valid[valid["mfe_r"] >= 2.0])
    pnl_2 = valid[valid["mfe_r"] >= 2.0]["pnl"].sum()
    cnt_1 = len(valid[valid["mfe_r"] >= 1.0])
    pnl_1 = valid[valid["mfe_r"] >= 1.0]["pnl"].sum()

    print("\n" + "=" * 65)
    print("■ 要約")
    print("=" * 65)
    print(f"  損切り {total_stops}件中:")
    print(f"    R>=1(損切幅以上有利に動いた) : {cnt_1}件({cnt_1/total_stops*100:.1f}%)  損失: {pnl_1:+,.0f}円")
    print(f"    R>=2(ターゲット到達後に損切り): {cnt_2}件({cnt_2/total_stops*100:.1f}%)  損失: {pnl_2:+,.0f}円")
    # ── 早期利確シミュレーション ──────────────────────────────────────
    print("\n" + "=" * 65)
    print("■ 早期利確シミュレーション（損切りトレードを早期利確に変えたら？）")
    print("  前提: 各R地点まで達したらそのR倍の利益で確定（手数料誤差を無視）")
    print("  alt_pnl = R × |actual_loss|  ※ stop_dist × qty = |pnl|を利用")
    print("=" * 65)
    print(f"  {'ルール':<22} {'対象件数':>8} {'代替損益合計':>15} {'現在比改善':>13}")
    print("-" * 64)

    actual_stop_pnl = valid["pnl"].sum()

    for thr in [1.0, 1.2, 1.5, 1.8, 2.0]:
        reached     = valid[valid["mfe_r"] >= thr]
        not_reached = valid[valid["mfe_r"] <  thr]
        alt_reached     = (reached["pnl"].abs() * thr).sum()
        alt_not_reached = not_reached["pnl"].sum()
        total_alt  = alt_reached + alt_not_reached
        improvement = total_alt - actual_stop_pnl
        label = f"R={thr:.1f}で早期利確"
        if thr == 2.0:
            label += "(現行目標)"
        print(f"  {label:<22} {len(reached):>7}件  {total_alt:>+14,.0f}円  {improvement:>+12,.0f}円")

    # break-even ストップ (R>=1.0 で BE に移動 → 最悪 0 で引き分け)
    be_trades    = valid[valid["mfe_r"] >= 1.0]
    no_be_trades = valid[valid["mfe_r"] <  1.0]
    total_alt_be = no_be_trades["pnl"].sum()   # BE達した分は±0
    improvement_be = total_alt_be - actual_stop_pnl
    print(f"  {'R=1.0 BE移動ストップ':<22} {len(be_trades):>7}件  {total_alt_be:>+14,.0f}円  {improvement_be:>+12,.0f}円")

    print(f"\n  ※ 現在の損切り損益合計: {actual_stop_pnl:+,.0f}円")
    print(f"  ※ 勝ちトレードは含まない（損切り {total_stops} 件のみの試算）")

    print("\n" + "=" * 65)
    print("■ 実装の難易度")
    print("=" * 65)
    print("  A) ターゲット倍率を 2R → 1.5R に下げる（最も簡単）")
    print("     → scan_lss_universe / nikkei_analysis のパラメータを1行変更")
    print("     → ただし既存の勝ちトレードも1.5Rで切るので合計効果は別途検証必要")
    print()
    print("  B) R=1.0 到達でストップをBE移動（中程度）")
    print("     → backtest_limit_entry / sameday5m_firsttouch を改修")
    print("     → ライブ: lss_exit_watcher に「BE移動」ロジックを追加")
    print()
    print("  C) R=1.5 早期利確 + R=0.5でBE移動（最強だが複雑）")
    print("     → 部分利確は現エンジン非対応。大改修が必要。")


def _show_sample_debug(stops: pd.DataFrame, cache: dict) -> None:
    sample = stops.head(5)
    print("\n[診断] 最初の5件の損切りトレード:")
    for _, r in sample.iterrows():
        sym      = str(r.get("symbol", "")).strip()
        ed       = str(r.get("entry_date", ""))
        ep       = r.get("entry_p")
        xp       = r.get("exit_p")
        in_cache = sym in cache
        date_ok  = False
        if in_cache:
            try:
                date_ok = pd.Timestamp(ed).date() in cache[sym]
            except Exception:
                pass
        print(f"  {sym}  {ed}  entry_p={ep!r}  exit_p={xp!r}  "
              f"cache={in_cache}  date={date_ok}")


if __name__ == "__main__":
    main()
