"""
scan_walkforward_daytrade.py  ―  デイトレ多戦略 Walk-forward スキャナ
==================================================================
逆指値ロング (scan_walkforward.py) を5分足デイトレ用に移植。

【戦略】
  DON / MACD / RSI2 / A7 / VOL / MOM の6戦略

【WF構造】 (基準日=本日、単位=暦日)
  Fold 1: TRAIN 540〜360日前 / TEST 360〜180日前
  Fold 2: TRAIN 360〜180日前 / TEST 180〜90日前
  Fold 3: TRAIN 180〜90日前  / TEST 90〜0日前

【合格条件】
  TRAIN: trades>=10, PF>=1.3, win_rate>=50%, total_pnl>0
  TEST : trades>=5,  PF>=1.1, win_rate>=45%, total_pnl>0
  選定 : 3 fold中 TRAIN+TEST 両方合格が 2 以上

【出力】
  walkforward_daytrade_<STRATEGY>_<YYYY-MM-DD>.csv

【使い方】
  python scan_walkforward_daytrade.py
  python scan_walkforward_daytrade.py --strategy MACD
  python scan_walkforward_daytrade.py --universe prime --workers 4
  python scan_walkforward_daytrade.py --max-price 6000 --min-price 1000
"""

from __future__ import annotations

import argparse
import csv
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrade_data import load_intraday_batch
from daytrade_engine_5m import backtest_symbol_5m, calc_stats
from daytrade_strategies_5m import STRATEGIES
from daytrade_strategies_5m_short import STRATEGIES_SHORT

# 統合戦略マップ
ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}

JST = timezone(timedelta(hours=9))

# Walk-forward fold (デイトレ向けに短く再設定)
FOLDS = [
    ("Fold1", 540, 360, 360, 180),   # TRAIN 540-360, TEST 360-180
    ("Fold2", 360, 180, 180, 90),    # TRAIN 360-180, TEST 180-90
    ("Fold3", 180, 90,  90,  0),     # TRAIN 180-90, TEST 90-0
]

# 合格条件 (デフォルト: 標準モード)
# 勝率は緩く (30%+) - トレンドフォロー戦略は勝率低くてもPFが高ければOK
# 重視するのは PF と損益
PASS_TRAIN_STRICT_DEFAULT = dict(trades=5, pf=1.2, win_rate=30, pnl=0)
PASS_TEST_STRICT_DEFAULT  = dict(trades=3, pf=1.0, win_rate=25, pnl=0)
MIN_FOLDS_DEFAULT         = 2

PASS_TRAIN = PASS_TRAIN_STRICT_DEFAULT
PASS_TEST  = PASS_TEST_STRICT_DEFAULT
MIN_FOLDS  = MIN_FOLDS_DEFAULT

# 緩和モード (--lenient): さらに緩い
PASS_TRAIN_LENIENT = dict(trades=3, pf=1.0, win_rate=20, pnl=0)
PASS_TEST_LENIENT  = dict(trades=2, pf=0.9, win_rate=20, pnl=0)
MIN_FOLDS_LENIENT  = 1  # 1 fold以上で合格

# 厳格モード (--strict): カーブフィット排除重視
PASS_TRAIN_STRICT = dict(trades=10, pf=1.5, win_rate=35, pnl=0)
PASS_TEST_STRICT  = dict(trades=5,  pf=1.2, win_rate=30, pnl=0)
MIN_FOLDS_STRICT  = 3  # 3 fold全部


def slice_trades(trades, start_days_ago, end_days_ago, today):
    """トレードを start-end 日数前の期間でフィルタ。"""
    start = today - timedelta(days=start_days_ago)
    end = today - timedelta(days=end_days_ago)
    out = []
    for t in trades:
        dt = t.get("entry_dt")
        if not hasattr(dt, "date"):
            continue
        d = dt.date()
        if start <= d < end:
            out.append(t)
    return out


def evaluate_fold(trades, today, train_start, train_end, test_start, test_end,
                  budget):
    """1 fold分の TRAIN/TEST 統計を計算。"""
    train_trades = slice_trades(trades, train_start, train_end, today)
    test_trades = slice_trades(trades, test_start, test_end, today)
    return calc_stats(train_trades, budget), calc_stats(test_trades, budget)


def pass_condition(stats, cond):
    """合格条件チェック。"""
    return (stats["n"] >= cond["trades"]
            and stats["pf"] >= cond["pf"]
            and stats["win_rate"] >= cond["win_rate"]
            and stats["total_pnl"] > cond["pnl"])


def scan_one(sym, name, df, strategy_name, strategy_fn, today, budget, max_risk,
              pass_train=None, pass_test=None, min_folds=None,
              cache=None):
    """1銘柄を全期間でbacktest → 3 fold WF評価。

    pass_train/test/min_folds 省略時はグローバル PASS_TRAIN/PASS_TEST/MIN_FOLDS を使用。
    cache: backtest結果キャッシュ (dict)
    """
    if pass_train is None:
        pass_train = PASS_TRAIN
    if pass_test is None:
        pass_test = PASS_TEST
    if min_folds is None:
        min_folds = MIN_FOLDS

    # キャッシュチェック
    cache_key = f"{strategy_name}::{sym}"
    if cache is not None and cache_key in cache:
        trades = cache[cache_key]["trades"]
        all_stats = cache[cache_key]["all_stats"]
    else:
        r = backtest_symbol_5m(sym, name, df, strategy_fn,
                                strategy_params={"name": strategy_name},
                                budget=budget, max_risk=max_risk)
        if not r or not r["trades"]:
            if cache is not None:
                cache[cache_key] = {"trades": [], "all_stats": None}
            return None
        trades = r["trades"]
        all_stats = calc_stats(trades, budget)
        if cache is not None:
            cache[cache_key] = {"trades": trades, "all_stats": all_stats}

    if not trades:
        return None

    fold_results = []
    pass_count = 0
    for fold_name, ts, te, vs, ve in FOLDS:
        train_stats, test_stats = evaluate_fold(
            trades, today, ts, te, vs, ve, budget)
        train_ok = pass_condition(train_stats, pass_train)
        test_ok = pass_condition(test_stats, pass_test)
        if train_ok and test_ok:
            pass_count += 1
        fold_results.append({
            "fold": fold_name,
            "train_n": train_stats["n"],
            "train_pf": train_stats["pf"],
            "train_wr": train_stats["win_rate"],
            "train_pnl": train_stats["total_pnl"],
            "test_n": test_stats["n"],
            "test_pf": test_stats["pf"],
            "test_wr": test_stats["win_rate"],
            "test_pnl": test_stats["total_pnl"],
            "train_pass": train_ok,
            "test_pass": test_ok,
        })

    if pass_count < min_folds:
        return None

    latest_price = float(df.iloc[-1]["close"]) if not df.empty else 0
    return {
        "symbol": sym,
        "name": name,
        "strategy": strategy_name,
        "latest_price": latest_price,
        "pass_folds": pass_count,
        "total_trades": all_stats["n"],
        "total_pf": all_stats["pf"],
        "total_win_rate": all_stats["win_rate"],
        "total_pnl": all_stats["total_pnl"],
        "total_dd": all_stats["max_dd"],
        "sharpe": all_stats["sharpe"],
        "max_losing_streak": all_stats["max_losing_streak"],
        "folds": fold_results,
    }


def main():
    parser = argparse.ArgumentParser(description="デイトレ多戦略WFスキャナ")
    parser.add_argument("--strategy", default="all",
                        help="all/long/short/個別戦略名 (DON/MACD/RSI2/A7/VOL/MOM"
                             "/DON_S/MACD_S/RSI2_S/A7_S/VOL_S/MOM_S)")
    parser.add_argument("--universe", default="prime",
                        choices=["prime", "winners"])
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--budget", type=int, default=200_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--max-price", type=int, default=10_000)
    parser.add_argument("--min-price", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--cache-dir", default=".cache_scan_daytrade",
                        help="backtest結果キャッシュディレクトリ")
    parser.add_argument("--no-cache", action="store_true",
                        help="キャッシュ無効化")
    parser.add_argument("--all-modes", action="store_true",
                        help="標準/緩和/厳格3モード一気に評価 (キャッシュ活用)")
    parser.add_argument("--lenient", action="store_true",
                        help="緩和モード: PF1.0+, 1 fold合格でOK")
    parser.add_argument("--strict", action="store_true",
                        help="厳格モード: PF1.5+, 3 fold全部合格")
    parser.add_argument("--diagnostic", action="store_true",
                        help="診断: 全銘柄の trade数/PF を表示")
    args = parser.parse_args()

    # モード設定 + ラベル (ファイル名に使う)
    global PASS_TRAIN, PASS_TEST, MIN_FOLDS
    if args.lenient:
        PASS_TRAIN = PASS_TRAIN_LENIENT
        PASS_TEST = PASS_TEST_LENIENT
        MIN_FOLDS = MIN_FOLDS_LENIENT
        mode_label = "lenient"
        print("[INFO] 緩和モード: TRAIN PF>=1.0, TEST PF>=0.9, 1 fold合格")
    elif args.strict:
        PASS_TRAIN = PASS_TRAIN_STRICT
        PASS_TEST = PASS_TEST_STRICT
        MIN_FOLDS = MIN_FOLDS_STRICT
        mode_label = "strict"
        print("[INFO] 厳格モード: TRAIN PF>=1.5, TEST PF>=1.2, 3 fold全合格")
    else:
        mode_label = "standard"
        print(f"[INFO] 標準モード: TRAIN PF>={PASS_TRAIN['pf']} 勝率>={PASS_TRAIN['win_rate']}%, "
              f"TEST PF>={PASS_TEST['pf']} 勝率>={PASS_TEST['win_rate']}%, "
              f"{MIN_FOLDS}/3 fold合格")

    # ユニバース
    if args.universe == "prime":
        from symbols_listed_all import SYMBOLS
        targets = SYMBOLS
    else:
        from daytrade_donchian_winners import SYMBOLS
        targets = SYMBOLS

    if args.limit > 0:
        targets = targets[:args.limit]

    today = datetime.now(JST).date()
    if args.strategy == "all":
        strategies = list(ALL_STRATEGIES.keys())
    elif args.strategy == "long":
        strategies = list(STRATEGIES.keys())
    elif args.strategy == "short":
        strategies = list(STRATEGIES_SHORT.keys())
    else:
        strategies = [args.strategy]

    print(f"=" * 70)
    print(f"  デイトレ Walk-Forward スキャン")
    print(f"=" * 70)
    print(f"  ユニバース: {args.universe} ({len(targets)}銘柄)")
    print(f"  戦略: {strategies}")
    print(f"  期間: {args.days}日 / 価格: {args.min_price:,}-{args.max_price:,}円")
    print(f"  Folds: {len(FOLDS)} / 合格基準: TRAIN+TEST 両方 ≥{MIN_FOLDS} fold")

    # データロード
    print(f"\n[Step 1] データロード", flush=True)
    symbols = [s for s, _ in targets]
    fetched = load_intraday_batch(symbols, args.days, source="local")
    # 価格フィルタ
    before = len(fetched)
    if args.max_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) <= args.max_price}
    if args.min_price > 0:
        fetched = {s: df for s, df in fetched.items()
                   if float(df.iloc[-1]["close"]) >= args.min_price}
    print(f"  ロード: {before}銘柄 → 価格フィルタ後: {len(fetched)}銘柄")
    targets = [(s, n) for s, n in targets if s in fetched]

    # 各戦略でスキャン
    out_dir = Path("walkforward_daytrade_results")
    out_dir.mkdir(exist_ok=True)

    # キャッシュ
    cache_dir = Path(args.cache_dir)
    if not args.no_cache:
        cache_dir.mkdir(exist_ok=True)

    # --all-modes: 3モード全部評価
    if args.all_modes:
        mode_configs = [
            ("standard", PASS_TRAIN_STRICT_DEFAULT, PASS_TEST_STRICT_DEFAULT,
             MIN_FOLDS_DEFAULT),
            ("lenient", PASS_TRAIN_LENIENT, PASS_TEST_LENIENT, MIN_FOLDS_LENIENT),
            ("strict", PASS_TRAIN_STRICT, PASS_TEST_STRICT, MIN_FOLDS_STRICT),
        ]
    else:
        mode_configs = [(mode_label, PASS_TRAIN, PASS_TEST, MIN_FOLDS)]

    # サマリーログ (全戦略集約)
    summary_path = out_dir / f"summary_{mode_label}_{today}.md"
    summary_lines = [
        f"# scan_walkforward_daytrade サマリー",
        f"",
        f"- **モード**: {mode_label}",
        f"- **生成日**: {today}",
        f"- **対象**: {len(targets)}銘柄",
        f"- **戦略**: {', '.join(strategies)}",
        f"- **TRAIN条件**: trades≥{PASS_TRAIN['trades']}, PF≥{PASS_TRAIN['pf']}, 勝率≥{PASS_TRAIN['win_rate']}%",
        f"- **TEST条件**: trades≥{PASS_TEST['trades']}, PF≥{PASS_TEST['pf']}, 勝率≥{PASS_TEST['win_rate']}%",
        f"- **合格基準**: {MIN_FOLDS}/3 fold合格",
        f"",
        f"## 戦略別 合格数",
        f"",
        f"| 戦略 | 合格数 | Top損益 | Top PF |",
        f"|---|---|---|---|",
    ]

    strategy_results = {}  # 各戦略の結果保存 (現在モード用)
    all_modes_results = {m: {} for m, _, _, _ in mode_configs}  # all-modes用

    for strat in strategies:
        print(f"\n[戦略 {strat}] スキャン開始 ({len(targets)}銘柄)", flush=True)
        strat_fn = ALL_STRATEGIES[strat]
        t0 = _time.time()

        # 戦略×銘柄 backtest結果キャッシュ
        bt_cache = {}
        cache_file = cache_dir / f"bt_{strat}_{today}.pkl"
        if not args.no_cache and cache_file.exists():
            try:
                import pickle as _pickle
                bt_cache = _pickle.loads(cache_file.read_bytes())
                print(f"  キャッシュ復元: {len(bt_cache)}件", flush=True)
            except Exception:
                bt_cache = {}

        # 1パスでbacktest → 全モード評価
        results_by_mode = {m: [] for m, _, _, _ in mode_configs}

        def _work(sym, name):
            try:
                # 各モードで scan_one を呼ぶ (キャッシュ共有でbacktestは1回だけ)
                mode_rs = {}
                for mname, pt, pst, mf in mode_configs:
                    r = scan_one(sym, name, fetched[sym], strat, strat_fn,
                                  today, args.budget, args.max_risk,
                                  pass_train=pt, pass_test=pst, min_folds=mf,
                                  cache=bt_cache)
                    if r:
                        mode_rs[mname] = r
                return mode_rs
            except Exception:
                return {}

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_work, s, n): (s, n) for s, n in targets}
            for i, fut in enumerate(as_completed(futures), 1):
                mode_rs = fut.result()
                for mname, r in mode_rs.items():
                    results_by_mode[mname].append(r)
                if i % 100 == 0 or i == len(targets):
                    mode_status = " / ".join(
                        f"{m}:{len(results_by_mode[m])}" for m, _, _, _ in mode_configs
                    )
                    print(f"  {i}/{len(targets)} ({_time.time()-t0:.1f}s, "
                          f"{mode_status})", flush=True)

        # キャッシュ保存
        if not args.no_cache and bt_cache:
            try:
                import pickle as _pickle
                cache_file.write_bytes(_pickle.dumps(bt_cache))
                print(f"  キャッシュ保存: {len(bt_cache)}件", flush=True)
            except Exception:
                pass

        # all_modes 用に結果を保存
        for mname, _, _, _ in mode_configs:
            all_modes_results[mname][strat] = results_by_mode[mname]

        # 現在モード (--mode指定) の results を採用
        results = results_by_mode[mode_label] if mode_label in results_by_mode \
                  else results_by_mode[mode_configs[0][0]]
        strategy_results[strat] = results

        # CSV出力 (all-modes なら各モード別)
        for mname, _, _, _ in mode_configs:
            rs = results_by_mode.get(mname, [])
            if not rs:
                print(f"\n  ✗ {strat}/{mname}: 合格なし")
                continue
            rs.sort(key=lambda x: -x["total_pnl"])
            csv_path = out_dir / f"walkforward_{strat}_{mname}_{today}.csv"
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                fieldnames = ["symbol", "name", "strategy", "latest_price",
                              "pass_folds", "total_trades", "total_pf",
                              "total_win_rate", "total_pnl", "total_dd",
                              "sharpe", "max_losing_streak"]
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                for r in rs:
                    w.writerow({k: r.get(k, "") for k in fieldnames})
            print(f"\n  ★ {strat}/{mname}: {len(rs)}銘柄合格 → {csv_path.name}")
            if mname == mode_label or args.all_modes:
                # Top 5 表示
                print(f"     Top 5:")
                for r in rs[:5]:
                    disp = r["name"][:18]
                    pf_str = "∞" if r["total_pf"] == float("inf") else f"{r['total_pf']:.2f}"
                    print(f"       {disp:<18} {r['symbol']:<8} PF{pf_str} "
                          f"勝{r['total_win_rate']:>3.0f}% {r['total_pnl']:>+10,.0f} {r['pass_folds']}/3")

    # サマリーログ書出し (all-modes なら各モード別サマリ)
    output_modes = [m for m, _, _, _ in mode_configs] if args.all_modes else [mode_label]

    grand_total = 0
    if args.all_modes:
        # all-modes: 3列で表示
        summary_lines[12] = "| 戦略 | standard | lenient | strict |"
        summary_lines[13] = "|---|---|---|---|"
        for strat in strategies:
            ns = []
            for mname in ["standard", "lenient", "strict"]:
                rs = all_modes_results.get(mname, {}).get(strat, [])
                ns.append(str(len(rs)))
                grand_total += len(rs)
            summary_lines.append(f"| {strat} | {ns[0]} | {ns[1]} | {ns[2]} |")
    else:
        for strat in strategies:
            rs = strategy_results.get(strat, [])
            grand_total += len(rs)
            if rs:
                top_pnl = rs[0]["total_pnl"]
                top_pf_v = rs[0]["total_pf"]
                top_pf = "∞" if top_pf_v == float("inf") else f"{top_pf_v:.2f}"
                summary_lines.append(f"| {strat} | {len(rs)} | {top_pnl:+,.0f}円 | {top_pf} |")
            else:
                summary_lines.append(f"| {strat} | **0** | — | — |")

    summary_lines.extend([
        f"",
        f"**合計合格パターン数: {grand_total}**",
        f"",
        f"---",
        f"",
        f"## 戦略別 Top 10 詳細",
        f"",
    ])

    # all-modes: 各モード毎に詳細
    if args.all_modes:
        summary_lines.extend([f"## モード別 Top 5 詳細", ""])
        for mname in ["standard", "lenient", "strict"]:
            summary_lines.extend([f"### モード: {mname}", ""])
            for strat in strategies:
                rs = all_modes_results.get(mname, {}).get(strat, [])
                if not rs:
                    summary_lines.append(f"- **{strat}**: 0銘柄")
                    continue
                rs.sort(key=lambda x: -x["total_pnl"])
                summary_lines.append(f"- **{strat}** ({len(rs)}銘柄合格)")
                for i, r in enumerate(rs[:5], 1):
                    pf_v = r.get("total_pf", 0)
                    pf = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
                    summary_lines.append(
                        f"  {i}. {r.get('name','')} ({r.get('symbol','')}) "
                        f"PF{pf} 勝{r.get('total_win_rate',0):.0f}% "
                        f"{r.get('total_pnl',0):+,.0f}円 {r.get('pass_folds',0)}/3"
                    )
            summary_lines.append("")

        # 個別モードサマリも出力
        for mname in ["standard", "lenient", "strict"]:
            mode_summary_path = out_dir / f"summary_{mname}_{today}.md"
            mode_lines = [
                f"# scan_walkforward_daytrade [{mname}]",
                f"- 生成: {today}", "",
                f"## 戦略別 合格数", "",
                f"| 戦略 | 合格 | Top損益 | Top PF |", f"|---|---|---|---|",
            ]
            for strat in strategies:
                rs = all_modes_results.get(mname, {}).get(strat, [])
                if rs:
                    rs.sort(key=lambda x: -x["total_pnl"])
                    pf_v = rs[0].get("total_pf", 0)
                    pf = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
                    mode_lines.append(
                        f"| {strat} | {len(rs)} | {rs[0].get('total_pnl', 0):+,.0f} | {pf} |"
                    )
                else:
                    mode_lines.append(f"| {strat} | 0 | — | — |")
            mode_lines.extend(["", f"## 戦略別 Top 10", ""])
            for strat in strategies:
                rs = all_modes_results.get(mname, {}).get(strat, [])
                mode_lines.append(f"### {strat} ({len(rs)}銘柄)")
                if not rs:
                    mode_lines.append("(なし)")
                    continue
                mode_lines.append("| # | 銘柄 | コード | PF | 勝率 | 損益 | fold |")
                mode_lines.append("|---|---|---|---|---|---|---|")
                for i, r in enumerate(rs[:10], 1):
                    pf_v = r.get("total_pf", 0)
                    pf = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
                    mode_lines.append(
                        f"| {i} | {r.get('name','')[:18]} | {r.get('symbol','')} | "
                        f"{pf} | {r.get('total_win_rate',0):.0f}% | "
                        f"{r.get('total_pnl',0):+,.0f} | {r.get('pass_folds',0)}/3 |"
                    )
                mode_lines.append("")
            mode_summary_path.write_text("\n".join(mode_lines), encoding="utf-8")
            print(f"  📄 {mname}サマリー: {mode_summary_path.name}")

        # 超コンパクト版 (チャット貼付け用、各モード1ファイル)
        for mname in ["standard", "lenient", "strict"]:
            compact_path = out_dir / f"compact_{mname}_{today}.txt"
            compact_lines = [
                f"=== {mname.upper()} ({today}) ===",
                "",
            ]
            total_count = 0
            for strat in strategies:
                rs = all_modes_results.get(mname, {}).get(strat, [])
                total_count += len(rs)
            compact_lines.append(f"合計合格: {total_count}件")
            compact_lines.append("")
            # 戦略別 1行サマリ
            compact_lines.append("【戦略別合格数】")
            for strat in strategies:
                rs = all_modes_results.get(mname, {}).get(strat, [])
                if rs:
                    rs.sort(key=lambda x: -x["total_pnl"])
                    pf_v = rs[0].get("total_pf", 0)
                    pf = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
                    compact_lines.append(
                        f"  {strat:7}: {len(rs):>3}件 "
                        f"Top: {rs[0].get('name','')[:14]} PF{pf} "
                        f"{rs[0].get('total_pnl',0):+,.0f}円"
                    )
                else:
                    compact_lines.append(f"  {strat:7}:   0件")
            compact_lines.append("")
            # 戦略別 Top 5 (1行ずつ)
            compact_lines.append("【戦略別 Top 5】")
            for strat in strategies:
                rs = all_modes_results.get(mname, {}).get(strat, [])
                if not rs:
                    continue
                compact_lines.append(f"  [{strat}]")
                for i, r in enumerate(rs[:5], 1):
                    pf_v = r.get("total_pf", 0)
                    pf = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
                    compact_lines.append(
                        f"    {i}. {r.get('name','')[:18]:<18} "
                        f"({r.get('symbol','')}) "
                        f"PF{pf:<5} 勝{r.get('total_win_rate',0):>3.0f}% "
                        f"{r.get('total_pnl',0):>+12,.0f}円 "
                        f"{r.get('pass_folds',0)}/3"
                    )
            compact_path.write_text("\n".join(compact_lines), encoding="utf-8")
            print(f"  📋 コンパクト: {compact_path.name}")

        # 比較表 (3モードを1ファイルで)
        compare_path = out_dir / f"compare_modes_{today}.txt"
        compare_lines = [
            f"=== 3モード比較 ({today}) ===",
            "",
            f"{'戦略':>7}  {'standard':>10} {'lenient':>10} {'strict':>10}",
            "  " + "-" * 50,
        ]
        for strat in strategies:
            ns_s = len(all_modes_results.get("standard", {}).get(strat, []))
            ns_l = len(all_modes_results.get("lenient", {}).get(strat, []))
            ns_st = len(all_modes_results.get("strict", {}).get(strat, []))
            compare_lines.append(
                f"  {strat:>7}: {ns_s:>10} {ns_l:>10} {ns_st:>10}"
            )
        ts = sum(len(all_modes_results.get("standard", {}).get(s, [])) for s in strategies)
        tl = sum(len(all_modes_results.get("lenient", {}).get(s, [])) for s in strategies)
        tst = sum(len(all_modes_results.get("strict", {}).get(s, [])) for s in strategies)
        compare_lines.append("  " + "-" * 50)
        compare_lines.append(f"  {'合計':>7}: {ts:>10} {tl:>10} {tst:>10}")
        compare_path.write_text("\n".join(compare_lines), encoding="utf-8")
        print(f"  📊 比較: {compare_path.name}")
        print(f"\n  💬 チャット貼付け用: compact_*.txt または compare_modes_*.txt")
        return  # 早期return (個別strat の詳細は既に書出し済み)

    for strat in strategies:
        rs = strategy_results.get(strat, [])
        summary_lines.append(f"### {strat} ({len(rs)}銘柄合格)")
        summary_lines.append("")
        if not rs:
            summary_lines.append("(合格銘柄なし)")
            summary_lines.append("")
            continue
        summary_lines.append("| # | 銘柄 | コード | 価格 | 取引 | 勝率 | PF | 損益 | DD | Sharpe | fold |")
        summary_lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(rs[:10], 1):
            name = r.get("name", "")[:18]
            sym = r.get("symbol", "")
            price = r.get("latest_price", 0)
            n = r.get("total_trades", 0)
            wr = r.get("total_win_rate", 0)
            pf_v = r.get("total_pf", 0)
            pf = "∞" if pf_v == float("inf") else f"{pf_v:.2f}"
            pnl = r.get("total_pnl", 0)
            dd = r.get("total_dd", 0)
            sharpe = r.get("sharpe", 0)
            folds = r.get("pass_folds", 0)
            summary_lines.append(
                f"| {i} | {name} | {sym} | {price:,.0f} | {n} | {wr:.0f}% | "
                f"{pf} | {pnl:+,.0f} | {dd:+.1f}% | {sharpe:.2f} | {folds}/3 |"
            )
        summary_lines.append("")

    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")
    print(f"\n{'='*70}")
    print(f"  ✅ 完了: 合計 {grand_total} 銘柄 (合格パターン)")
    print(f"  📄 サマリー: {summary_path}")
    print(f"  📁 CSV: {out_dir}/")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
