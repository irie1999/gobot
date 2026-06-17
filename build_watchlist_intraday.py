"""
build_watchlist_intraday.py  ―  デイトレ版 Walk-forward 結果から WATCHLIST 構築
==================================================================================
scan_walkforward_intraday.py が出力した CSV を読み込み、
リスク指標でフィルターした上で上位 N 銘柄を選定し、
check_signals_daytrade.py の WATCHLIST に貼り付け可能な Python コードを出力する。

【フィルター条件】 (デフォルト)
  folds_passed        >= 2   : 全 fold で TRAIN+TEST 両合格
  max_drawdown_pct    <= 15  : 最大DD 15%以下
  max_consecutive_losses <= 5: 最大連敗 5 以下
  sharpe              >= 0.0 : Sharpe 非負
  total_test_pnl       > 0   : TEST 期間合計損益プラス

【デイトレ追加フィルター】
  max_avg_hold_minutes <= 0  : 平均保有時間上限 (分, 0=制限なし)
                               例: --max-avg-hold-minutes 90 → 90分以内のみ

【ランキング】
  composite_score = total_test_pnl × (1 + max(sharpe, 0))

【使い方】
  python build_watchlist_intraday.py                   # デフォルト: 上位20銘柄
  python build_watchlist_intraday.py --top 30
  python build_watchlist_intraday.py --max-dd 10
  python build_watchlist_intraday.py --min-sharpe 0.3
  python build_watchlist_intraday.py --max-avg-hold-minutes 90
  python build_watchlist_intraday.py --max-price 6000
  python build_watchlist_intraday.py --min-price 1000  # 低位株除外
  python build_watchlist_intraday.py --strategy PREV_CLOSE_BREAK
  python build_watchlist_intraday.py --date 2026-06-17  # 特定日の CSV を使用

【出力】
  watchlist_proposal_intraday_<YYYY-MM-DD>.py  … WATCHLIST 提案 (貼り付け用)
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _float(v, default=0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def apply_filters(
    rows: list[dict],
    min_folds: int = 2,
    max_dd_pct: float = 15.0,
    max_consec_losses: int = 5,
    min_sharpe: float = 0.0,
    max_price: float = 0.0,
    min_price: float = 0.0,
    min_trades: int = 0,
    max_avg_hold_minutes: float = 0.0,
) -> list[dict]:
    survivors = []
    for r in rows:
        if _int(r.get("folds_passed", 0)) < min_folds:
            continue
        if _float(r.get("max_drawdown_pct", 0)) > max_dd_pct:
            continue
        if _int(r.get("max_consecutive_losses", 0)) > max_consec_losses:
            continue
        if _float(r.get("sharpe", 0)) < min_sharpe:
            continue
        if _float(r.get("total_test_pnl", 0)) <= 0:
            continue
        if min_trades > 0 and _int(r.get("total_test_trades", 0)) < min_trades:
            continue
        if max_avg_hold_minutes > 0:
            hold_m = _float(r.get("avg_hold_minutes", 0))
            if hold_m > 0 and hold_m > max_avg_hold_minutes:
                continue
        if max_price > 0:
            price = _float(r.get("latest_price", 0))
            if price > 0 and price > max_price:
                continue
        if min_price > 0:
            price = _float(r.get("latest_price", 0))
            if price > 0 and price < min_price:
                continue
        survivors.append(r)
    return survivors


def composite_score(r: dict) -> float:
    pnl    = _float(r.get("total_test_pnl", 0))
    sharpe = _float(r.get("sharpe", 0))
    return pnl * (1.0 + max(sharpe, 0.0))


def _find_latest_csv(
    input_dir: Path, strategy: str, mode_suffix: str
) -> Path | None:
    """最新日付の CSV を自動検出。"""
    pattern = f"walkforward_intraday_{strategy}{mode_suffix}_*.csv"
    candidates = sorted(input_dir.glob(pattern), reverse=True)
    return candidates[0] if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="デイトレ版 Walk-forward 結果から WATCHLIST 構築"
    )
    parser.add_argument("--top",        type=int, default=20,
                        help="選定する銘柄数 (デフォルト20)")
    parser.add_argument("--strategy",   type=str, default=None,
                        help="戦略名 (省略時は全 CSV を統合)")
    parser.add_argument("--min-folds",  type=int, default=2)
    parser.add_argument("--max-dd",     type=float, default=15.0,
                        help="MaxDD 上限 (%%) デフォルト15")
    parser.add_argument("--max-consec-losses", type=int, default=5)
    parser.add_argument("--min-sharpe", type=float, default=0.0)
    parser.add_argument("--min-trades", type=int, default=0,
                        help="TEST 期間の最低取引回数 (0=制限なし)")
    parser.add_argument("--max-avg-hold-minutes", type=float, default=0.0,
                        help="平均保有時間の上限 (分, 0=制限なし)")
    parser.add_argument("--max-price",  type=float, default=0.0)
    parser.add_argument("--min-price",  type=float, default=0.0,
                        help="最新終値の下限 (低位株除外)")
    parser.add_argument("--budget",     type=float, default=0.0,
                        help="総予算 (円). FIXED_QTY=100株換算で --max-price と同義")
    parser.add_argument("--input-dir",  type=Path, default=Path("walkforward_results"))
    parser.add_argument("--date",       type=str, default=None,
                        help="読み込む CSV の日付 YYYY-MM-DD (省略時は最新ファイルを自動検出)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true")
    mode_group.add_argument("--conservative", action="store_true")
    args = parser.parse_args()

    mode_suffix = "_aggressive" if args.aggressive else ""

    effective_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        effective_max_price = args.budget / 100.0

    print("=" * 78)
    print(f"デイトレ WATCHLIST 構築")
    print(f"  フィルター: folds>={args.min_folds}  "
          f"MaxDD<={args.max_dd}%  "
          f"連敗<={args.max_consec_losses}  "
          f"Sharpe>={args.min_sharpe}")
    if args.min_trades > 0:
        print(f"  取引回数  : TEST期間 {args.min_trades}回以上")
    if args.max_avg_hold_minutes > 0:
        print(f"  保有時間  : 平均 {args.max_avg_hold_minutes:.0f}分以下")
    if effective_max_price > 0:
        print(f"  価格上限  : {effective_max_price:,.0f}円/株")
    if args.min_price > 0:
        print(f"  価格下限  : {args.min_price:,.0f}円/株")
    print(f"  選定数    : 上位 {args.top} 銘柄")
    print("=" * 78)

    # ── CSV 収集 ──
    all_rows: list[dict] = []
    input_dir = args.input_dir

    # 対象戦略を決定
    if args.strategy:
        target_strategies = [args.strategy]
    else:
        # intraday CSV を全件スキャン
        pattern = f"walkforward_intraday_*{mode_suffix}_*.csv"
        csv_files = sorted(input_dir.glob(pattern), reverse=True)
        seen_strategies: set[str] = set()
        for p in csv_files:
            # ファイル名から戦略名を抽出
            stem = p.stem  # walkforward_intraday_STRATEGY[_mode]_DATE
            parts = stem.split("_")
            # parts: ["walkforward", "intraday", STRATEGY, ..., DATE]
            # DATE は末尾 (YYYY-MM-DD = 10文字)
            if len(parts) < 4:
                continue
            strategy_part = parts[2]
            if strategy_part not in seen_strategies:
                seen_strategies.add(strategy_part)
        target_strategies = list(seen_strategies) if seen_strategies else ["PREV_CLOSE_BREAK"]

    print(f"対象戦略: {', '.join(target_strategies)}")

    for strategy in target_strategies:
        if args.date:
            csv_path = input_dir / f"walkforward_intraday_{strategy}{mode_suffix}_{args.date}.csv"
        else:
            csv_path = _find_latest_csv(input_dir, strategy, mode_suffix)
            if csv_path is None:
                print(f"[WARN] {strategy} の CSV が見つかりません: {input_dir}")
                continue

        rows = load_csv(csv_path)
        if not rows:
            print(f"[WARN] CSV が空 or 存在しません: {csv_path}")
            continue

        print(f"\n  {strategy}: {csv_path.name} → {len(rows)}行")
        all_rows.extend(rows)

    if not all_rows:
        print("\n[ERROR] 有効な CSV が見つかりませんでした。")
        print("  先に  python scan_walkforward_intraday.py  を実行してください。")
        return

    # ── フィルター & ランキング ──
    filtered = apply_filters(
        all_rows,
        min_folds=args.min_folds,
        max_dd_pct=args.max_dd,
        max_consec_losses=args.max_consec_losses,
        min_sharpe=args.min_sharpe,
        max_price=effective_max_price,
        min_price=args.min_price,
        min_trades=args.min_trades,
        max_avg_hold_minutes=args.max_avg_hold_minutes,
    )
    filtered.sort(key=composite_score, reverse=True)

    # 同一銘柄の重複排除 (複数戦略で同じ銘柄が入る場合、最高スコアを採用)
    seen_symbols: set[str] = set()
    deduped: list[dict] = []
    for r in filtered:
        sym = r.get("symbol", "")
        if sym not in seen_symbols:
            seen_symbols.add(sym)
            deduped.append(r)

    top = deduped[: args.top]

    print(f"\n全候補={len(all_rows)}  フィルター通過={len(filtered)}  "
          f"重複排除後={len(deduped)}  選定={len(top)}")

    if not top:
        print("\n[INFO] 条件を満たす銘柄がありません。フィルターを緩和してください。")
        print("  例: --max-dd 20 --min-sharpe -0.5 --min-folds 1")
        return

    # ── 選定銘柄の表示 ──
    print(f"\n{'銘柄':<10}{'名前':<22}{'戦略':<18}"
          f"{'株価':>8}{'fld':>5}{'trades':>8}{'TEST_PnL':>12}"
          f"{'PF':>7}{'WR':>8}{'MaxDD%':>9}{'連敗':>6}{'Shrp':>7}{'保有分':>8}")
    print("-" * 120)
    for r in top:
        price = _float(r.get("latest_price", 0))
        print(f"{r.get('symbol',''):<10}{r.get('name','')[:20]:<22}"
              f"{r.get('strategy',''):<18}"
              f"{price:>8,.0f}"
              f"{_int(r.get('folds_passed',0)):>5}"
              f"{_int(r.get('total_test_trades',0)):>8}"
              f"{_float(r.get('total_test_pnl',0)):>+12,.0f}"
              f"{_float(r.get('avg_test_pf',0)):>7.2f}"
              f"{_float(r.get('avg_test_wr',0)):>7.1f}%"
              f"{_float(r.get('max_drawdown_pct',0)):>9.1f}"
              f"{_int(r.get('max_consecutive_losses',0)):>6}"
              f"{_float(r.get('sharpe',0)):>7.2f}"
              f"{_float(r.get('avg_hold_minutes',0)):>7.0f}分")

    # ── WATCHLIST Python コード生成 ──
    lines = [
        f'"""',
        f'watchlist_proposal_intraday_{TODAY}.py',
        f'',
        f'scan_walkforward_intraday.py の結果から自動生成',
        f'生成日: {TODAY}',
        f'フィルター: folds>={args.min_folds} MaxDD<={args.max_dd}% '
        f'連敗<={args.max_consec_losses} Sharpe>={args.min_sharpe}',
        f'モード: {"aggressive" if args.aggressive else "conservative"}',
        f'"""',
        f'',
        f'# check_signals_daytrade.py の WATCHLIST に貼り付けてください',
        f'INTRADAY_WATCHLIST = [',
    ]

    for r in top:
        sym   = r.get("symbol", "")
        name  = r.get("name", "")
        strat = r.get("strategy", "")
        folds = r.get("folds_passed", "")
        price = _float(r.get("latest_price", 0))
        cost  = price * 100
        pnl   = _float(r.get("total_test_pnl", 0))
        pf    = r.get("avg_test_pf", "")
        wr    = r.get("avg_test_wr", "")
        dd    = r.get("max_drawdown_pct", "")
        cl    = r.get("max_consecutive_losses", "")
        shrp  = r.get("sharpe", "")
        hold  = _float(r.get("avg_hold_minutes", 0))
        price_str = f"{price:,.0f}円(100株={cost:,.0f}円)" if price > 0 else "価格不明"
        lines.append(
            f'    ("{sym}", "{name}"),  '
            f'# {price_str} strategy={strat} folds={folds} '
            f'pnl={pnl:+,.0f} pf={pf} wr={wr}% '
            f'DD={dd}% 連敗{cl} Shrp{shrp} 保有{hold:.0f}分'
        )

    lines += [
        "]",
        "",
        "# 貼り付け先: check_signals_daytrade.py の WATCHLIST = [...]",
        "# 確認: python check_signals_daytrade.py --signal-only",
        "",
    ]

    proposal_path = Path(f"watchlist_proposal_intraday_{TODAY}.py")
    with open(proposal_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nWATCHLIST提案ファイル: {proposal_path.resolve()}")
    print("\n次ステップ:")
    print("  1. 上記ファイルを確認")
    print("  2. check_signals_daytrade.py の WATCHLIST を差し替え")
    print("  3. python check_signals_daytrade.py --signal-only  で動作確認")


if __name__ == "__main__":
    main()
