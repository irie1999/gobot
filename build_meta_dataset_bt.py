"""
build_meta_dataset_bt.py — バックテスト由来のメタモデル訓練データ生成
=======================================================================

forward_test (紙トレード) が数百件溜まるのを待たずに、**既存のバックテスト
trade_log** から一気にメタモデル用の教師データを作る。

【発想】 (CLAUDE.md §16 のメタモデル TODO に対応)
  run_limit_backtest は WATCHLIST/ユニバース × 戦略の「1トレードずつの結果」
  (trade_log) をすでに吐いている。これを全ユニバースで回せば、今すぐ
  数百〜数千件の「勝った/負けた」ラベル付きトレードが手に入る。
  → forward で数ヶ月待つ必要がない。

【リーク防止 (最重要)】
  特徴量は meta_features.py 経由で **シグナル日「まで」のデータのみ** から計算。
  約定後の値動き (MAE/MFE/exit/hold_days) は特徴量にしない (それは y 側)。

  さらにユーザー案の「ホールドアウトをフォワード期間にする」を実装:
    --holdout-days 90 のとき、シグナル日が直近90日内のトレードを split=test、
    それ以前を split=train とマークする (時系列分割)。
    → test は学習に使わず「擬似的な未来」として精度検証に使う。

【ターゲット】
  y = 1  pnl > 0,  y = 0  pnl <= 0
  対象: reason が 目標達成 / 損切り / タイムカット の決済済みトレードのみ
        (保有中 / 発注中 は除外)

使い方:
  python build_meta_dataset_bt.py                          # WATCHLIST (速い, 動作確認)
  python build_meta_dataset_bt.py --universe               # 全ユニバース (~1800, 数十分〜)
  python build_meta_dataset_bt.py --universe --workers 8
  python build_meta_dataset_bt.py --universe --limit 200   # 先頭200銘柄 (デバッグ)
  python build_meta_dataset_bt.py --family all             # 空売り含む全12戦略
  python build_meta_dataset_bt.py --holdout-days 60        # 直近60日を test split に
  python build_meta_dataset_bt.py --aggressive             # aggressive プリセット
  python build_meta_dataset_bt.py --backtest-days 1000     # バックテスト期間を伸ばす
  python build_meta_dataset_bt.py --out meta_train_bt.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

from backtest_limit_entry import fetch, run_limit_backtest
import meta_features as _mf
# 戦略定義・ユニバースローダは scan_walkforward から再利用 (プリセット/モード込み)
from scan_walkforward import (
    STRATEGY_DEFS, TRADING_MODE, load_universe,
    _DEFAULT_WORKERS,
)

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# 決済済み (= y を確定できる) reason
SETTLED_REASONS = {"目標達成", "損切り", "タイムカット"}

_FAMILY_STRATS = {
    "stop":      ["MACD", "A7", "RSI2"],
    "breakout":  ["DON", "VOL", "MOM"],
    "short":     ["A7_S", "MACD_S", "RSI2_S"],
    "short_brk": ["DON_S", "MOM_S", "GAP_S"],
    "both":      ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"],
    "all":       ["MACD", "A7", "RSI2", "DON", "VOL", "MOM",
                  "A7_S", "MACD_S", "RSI2_S", "DON_S", "MOM_S", "GAP_S"],
}

OUT_FIELDS = [
    # 識別子 (訓練には使わないが追跡用)
    "symbol", "name", "strategy", "family",
    "signal_date", "entry_date", "exit_date", "reason",
    # 特徴量 (すべて signal_date 時点で確定)
    "signal_month", "signal_dow",
    "regime", "atr_ratio", "vol_ratio",
    "rr_ratio", "stop_pct", "target_pct",
    "entry_type",
    # メタ情報
    "pnl", "hold_days",
    # 時系列分割ラベル & ターゲット
    "split", "y",
]

# N225 をプロセス内で一度だけ取得 (スレッド共有・読み取り専用)
_N225 = None


def _get_n225():
    global _N225
    if _N225 is None:
        _N225 = fetch("^N225", 1100)
    return _N225


def _ts_to_date(ts) -> str:
    try:
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return str(ts)[:10]


def process_symbol(symbol: str, name: str, strategy: str,
                   backtest_days: int, test_cutoff) -> list[dict]:
    """1 銘柄 × 1 戦略のバックテストを回し、決済済みトレードを特徴量行に変換。"""
    calc_fn, em, sm, tm, family, entry_type = STRATEGY_DEFS[strategy]

    full_df = fetch(symbol, max(backtest_days + 250, 800))
    if full_df is None or len(full_df) < 210:
        return []

    try:
        result = run_limit_backtest(
            symbol, name, full_df.copy(), calc_fn,
            em, sm, tm, backtest_days, strategy, entry_type=entry_type,
        )
    except Exception:
        return []

    n225 = _get_n225()
    out: list[dict] = []
    for t in result.get("trade_log", []):
        reason = t.get("reason")
        if reason not in SETTLED_REASONS:
            continue   # 保有中 / 発注中 は y を確定できないので除外
        pnl = t.get("pnl")
        if pnl is None:
            continue

        signal_ts = t.get("signal_dt")
        signal_date = _ts_to_date(signal_ts)

        # ── 特徴量 (signal_date 時点) ──
        regime = _mf.compute_regime(n225, signal_ts)
        atr_ratio, vol_ratio = _mf.compute_atr_vol(full_df, signal_ts)
        rr = _mf.rr_features(
            t.get("order_limit", 0.0),
            t.get("order_stop", 0.0),
            t.get("order_target", 0.0),
        )

        signal_month, signal_dow = "", ""
        try:
            d = signal_ts.to_pydatetime() if hasattr(signal_ts, "to_pydatetime") \
                else datetime.strptime(signal_date, "%Y-%m-%d")
            signal_month = d.month
            signal_dow   = d.weekday()
        except Exception:
            pass

        # ── 時系列分割 (ホールドアウト = 擬似フォワード) ──
        try:
            sd = signal_ts.date() if hasattr(signal_ts, "date") \
                else datetime.strptime(signal_date, "%Y-%m-%d").date()
        except Exception:
            sd = None
        split = "train"
        if test_cutoff is not None and sd is not None and sd >= test_cutoff:
            split = "test"

        out.append({
            "symbol": symbol, "name": name,
            "strategy": strategy, "family": family,
            "signal_date": signal_date,
            "entry_date": _ts_to_date(t.get("entry_dt")),
            "exit_date":  _ts_to_date(t.get("exit_dt")),
            "reason": reason,
            "signal_month": signal_month, "signal_dow": signal_dow,
            "regime": regime,
            "atr_ratio": "" if atr_ratio is None else atr_ratio,
            "vol_ratio": "" if vol_ratio is None else vol_ratio,
            "rr_ratio":   "" if rr["rr_ratio"]   is None else rr["rr_ratio"],
            "stop_pct":   "" if rr["stop_pct"]   is None else rr["stop_pct"],
            "target_pct": "" if rr["target_pct"] is None else rr["target_pct"],
            "entry_type": entry_type,
            "pnl": round(float(pnl)),
            "hold_days": t.get("hold_days", 0),
            "split": split,
            "y": 1 if pnl > 0 else 0,
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="バックテスト由来のメタモデル訓練データ生成")
    ap.add_argument("--universe", action="store_true",
                    help="全ユニバース (symbols_listed_prime 等) を対象 (既定: WATCHLIST)")
    ap.add_argument("--symbols", default=None,
                    help="ユニバースファイルを明示指定 (--universe 時のみ有効)")
    ap.add_argument("--family", choices=list(_FAMILY_STRATS.keys()), default="both",
                    help="対象戦略グループ (既定: both = 6ロング戦略)")
    ap.add_argument("--backtest-days", type=int, default=730,
                    help="バックテスト期間 (日, 既定: 730)")
    ap.add_argument("--holdout-days", type=int, default=90,
                    help="直近N日を split=test にする (擬似フォワード, 既定: 90)")
    ap.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    ap.add_argument("--limit", type=int, default=0,
                    help="銘柄を先頭N件に制限 (デバッグ用, 0=制限なし)")
    ap.add_argument("--out", default=None,
                    help="出力 CSV (既定: meta_train_bt[_aggressive].csv)")
    ap.add_argument("--aggressive",   action="store_true")
    ap.add_argument("--conservative", action="store_true")
    args = ap.parse_args()

    strategies = _FAMILY_STRATS[args.family]
    test_cutoff = (TODAY - timedelta(days=args.holdout_days)
                   if args.holdout_days > 0 else None)

    # ── 銘柄リスト ──
    if args.universe:
        try:
            symbols, src = load_universe(args.symbols)
        except RuntimeError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # WATCHLIST (stop + breakout) を symbol,name で重複排除
        import check_signals_stop as _stop
        import check_signals_breakout as _brk
        seen: dict[str, str] = {}
        for sym, nm, _strat in list(_stop.WATCHLIST) + list(_brk.WATCHLIST):
            seen.setdefault(sym, nm)
        symbols = list(seen.items())
        src = "WATCHLIST (stop+breakout)"

    if args.limit > 0:
        symbols = symbols[: args.limit]

    suffix   = "_aggressive" if TRADING_MODE == "aggressive" else ""
    out_path = Path(args.out) if args.out else Path(f"meta_train_bt{suffix}.csv")

    print("=" * 78)
    print("メタモデル訓練データ生成 (バックテスト由来)")
    print(f"  基準日        : {TODAY}")
    print(f"  ユニバース    : {src} ({len(symbols)} 銘柄)")
    print(f"  戦略          : {', '.join(strategies)}")
    print(f"  バックテスト  : 直近 {args.backtest_days} 日")
    if test_cutoff:
        print(f"  test split    : {test_cutoff} 以降のシグナル (直近 {args.holdout_days} 日)")
    print(f"  モード        : {TRADING_MODE}")
    print(f"  Workers       : {args.workers}")
    print(f"  出力          : {out_path}")
    print("=" * 78)

    _get_n225()   # スレッド起動前に N225 を確定

    all_rows: list[dict] = []
    tasks = [(sym, name, strat) for sym, name in symbols for strat in strategies]
    print(f"タスク数: {len(tasks):,} ({len(symbols)}銘柄 × {len(strategies)}戦略)\n")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_symbol, sym, name, strat,
                          args.backtest_days, test_cutoff): (sym, strat)
                for sym, name, strat in tasks}
        done = 0
        progress_every = max(len(tasks) // 20, 25)
        for fut in as_completed(futs):
            done += 1
            try:
                all_rows.extend(fut.result())
            except Exception:
                pass
            if done % progress_every == 0:
                print(f"  進捗: {done}/{len(tasks)}  収集トレード: {len(all_rows)}",
                      flush=True)

    if not all_rows:
        print("\n[WARNING] トレードが 0 件でした。ユニバース/期間を確認してください。")
        return

    # 時系列順にソート
    all_rows.sort(key=lambda r: (r["signal_date"], r["symbol"], r["strategy"]))

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        for r in all_rows:
            writer.writerow({k: r.get(k, "") for k in OUT_FIELDS})

    # ── サマリー ──
    n = len(all_rows)
    n_win = sum(r["y"] for r in all_rows)
    n_train = sum(1 for r in all_rows if r["split"] == "train")
    n_test  = n - n_train
    pnl_total = sum(r["pnl"] for r in all_rows)

    print(f"\n保存: {out_path.resolve()}  ({n:,} トレード)")
    print(f"\n【データセット概要】")
    print(f"  総トレード   : {n:,}")
    print(f"  勝ち (y=1)   : {n_win:,}  ({n_win/n*100:.1f}%)")
    print(f"  負け (y=0)   : {n-n_win:,}  ({(n-n_win)/n*100:.1f}%)")
    print(f"  合計損益     : {pnl_total:+,.0f} 円")
    print(f"  train split  : {n_train:,} 件 ({test_cutoff} より前)")
    print(f"  test  split  : {n_test:,} 件 (擬似フォワード = 直近 {args.holdout_days} 日)")

    # regime / カバレッジ
    from collections import Counter
    rc = Counter(r["regime"] for r in all_rows if r["regime"])
    if rc:
        print(f"\n  regime 分布  : {dict(rc)}")
    n_atr = sum(1 for r in all_rows if r["atr_ratio"] != "")
    n_vol = sum(1 for r in all_rows if r["vol_ratio"] != "")
    print(f"  atr_ratio    : {n_atr}/{n} ({n_atr/n*100:.0f}%)")
    print(f"  vol_ratio    : {n_vol}/{n} ({n_vol/n*100:.0f}%)")

    # 戦略別
    from collections import defaultdict
    sd = defaultdict(list)
    for r in all_rows:
        sd[r["strategy"]].append(r["y"])
    print(f"\n  {'戦略':<8}{'件数':>8}{'勝率':>8}")
    print("  " + "-" * 26)
    for strat, ys in sorted(sd.items()):
        wr = sum(ys) / len(ys) * 100 if ys else 0
        print(f"  {strat:<8}{len(ys):>8}{wr:>7.1f}%")

    if n_test < 30:
        print(f"\n  ⚠ test split が {n_test} 件と少なめです。--holdout-days を増やすか")
        print(f"    --universe で銘柄数を増やしてください。")
    if n >= 300:
        print(f"\n  ✓ {n:,} 件 — メタモデル訓練に十分です。")
        print(f"    次: LightGBM で split=train を学習 → split=test で精度評価。")
    else:
        print(f"\n  ℹ️  推奨 300件以上。--universe で全銘柄を対象にすると一気に増えます。")


if __name__ == "__main__":
    main()
