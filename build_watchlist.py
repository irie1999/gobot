"""
build_watchlist.py  ―  Walk-forward 結果から新 WATCHLIST を構築
=================================================================
scan_walkforward.py が出力した CSV を読み込み、
リスク指標 (MaxDD / 最大連敗 / Sharpe) でフィルターした上で
戦略ごとに上位 N 銘柄を選定し、
check_signals_stop.py / check_signals_breakout.py の WATCHLIST に
貼り付け可能な Python コードを出力する。

【フィルター条件】 (デフォルト)
  folds_passed        >= 2   : 3 fold 中 2 fold 以上で TRAIN+TEST 両合格
  max_drawdown_pct    <= 15  : 最大DD 15%以下
  max_consecutive_losses <= 5: 最大連敗 5 以下
  sharpe              >= 0.0 : Sharpe 非負
  total_test_pnl       > 0   : TEST 期間合計損益プラス

【ランキング】
  composite_score = total_test_pnl × (1 + max(sharpe, 0))
  (損益を基準に Sharpe で加点)

【使い方】
  python build_watchlist.py                       # デフォルト: 戦略あたり10銘柄
  python build_watchlist.py --per-strategy 8
  python build_watchlist.py --max-dd 10
  python build_watchlist.py --min-sharpe 0.3
  python build_watchlist.py --input-dir walkforward_results

【予算フィルター】 (CSV に latest_price がある場合のみ有効)
  python build_watchlist.py --budget 600000       # 60万円で100株買える銘柄のみ
  python build_watchlist.py --max-price 6000      # 株価6000円以下 (上と同義)

【出力】
  watchlist_proposal_<YYYY-MM-DD>.py  … WATCHLIST 提案 (貼り付け用)
  標準出力                              … フィルター通過状況のサマリー
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


def apply_filters(rows: list[dict],
                  min_folds: int,
                  max_dd_pct: float,
                  max_consec_losses: int,
                  min_sharpe: float,
                  max_price: float = 0.0,
                  min_price: float = 0.0,
                  min_trades: int = 0,
                  max_avg_hold: float = 0.0) -> list[dict]:
    """フィルターを適用した行のみ返す。"""
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
        # 取引回数フィルター
        if min_trades > 0 and _int(r.get("total_test_trades", 0)) < min_trades:
            continue
        # 平均保有日数フィルター (CSV に avg_hold_days がある場合のみ有効)
        if max_avg_hold > 0:
            hold = _float(r.get("avg_hold_days", 0))
            if hold > 0 and hold > max_avg_hold:
                continue
        # 価格フィルター: latest_price > max_price の銘柄を除外
        # (CSV に latest_price が無い古いファイルはスキップしない)
        if max_price > 0:
            price = _float(r.get("latest_price", 0))
            if price > 0 and price > max_price:
                continue
        # 最低株価フィルター: latest_price < min_price の銘柄を除外
        # (低位株のtick・流動性・貸借/逆日歩リスク回避用)
        if min_price > 0:
            price = _float(r.get("latest_price", 0))
            if price > 0 and price < min_price:
                continue
        survivors.append(r)
    return survivors


def composite_score(r: dict) -> float:
    """損益 × (1 + Sharpe) 複合スコア。"""
    pnl    = _float(r.get("total_test_pnl", 0))
    sharpe = _float(r.get("sharpe", 0))
    return pnl * (1.0 + max(sharpe, 0.0))


def format_watchlist_block(rows: list[dict], title: str) -> str:
    lines = [f"    # ── {title} ──"]
    for r in rows:
        sym    = r.get("symbol", "")
        name   = r.get("name", "")
        strat  = r.get("strategy", "")
        folds  = r.get("folds_passed", "")
        price  = _float(r.get("latest_price", 0))
        cost   = price * 100  # 100株購入コスト
        pnl    = _float(r.get("total_test_pnl", 0))
        pf     = r.get("avg_test_pf", "")
        wr     = r.get("avg_test_wr", "")
        dd     = r.get("max_drawdown_pct", "")
        cl     = r.get("max_consecutive_losses", "")
        shrp   = r.get("sharpe", "")
        price_str = f"{price:,.0f}円(100株={cost:,.0f}円)" if price > 0 else "価格不明"
        lines.append(
            f'    ("{sym}", "{name}", "{strat}"),'
            f"  # {price_str} folds={folds} pnl={pnl:+,.0f} pf={pf} wr={wr}% "
            f"DD={dd}% 連敗{cl} Shrp{shrp}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward 結果から WATCHLIST 構築")
    parser.add_argument("--per-strategy", type=int, default=10,
                        help="戦略ごとに選定する銘柄数 (デフォルト10)")
    parser.add_argument("--min-folds",   type=int, default=2)
    parser.add_argument("--max-dd",      type=float, default=15.0,
                        help="MaxDD 上限 (%%) デフォルト15")
    parser.add_argument("--max-consec-losses", type=int, default=5)
    parser.add_argument("--min-sharpe",    type=float, default=0.0)
    parser.add_argument("--min-trades",    type=int,   default=0,
                        help="TEST 期間の最低取引回数 (0=制限なし)")
    parser.add_argument("--max-avg-hold",  type=float, default=0.0,
                        help="TEST 期間の平均保有日数 上限 (0=制限なし). "
                             "scan_walkforward.py を再実行して avg_hold_days カラムが必要")
    parser.add_argument("--max-price",   type=float, default=0.0,
                        help="最新終値の上限 (円/株). 0=制限なし")
    parser.add_argument("--min-price",   type=float, default=0.0,
                        help="最新終値の下限 (円/株). 低位株を除外 (例: 1000). "
                             "tick・流動性・貸借/逆日歩リスク回避用. 再スキャン不要")
    parser.add_argument("--budget",      type=float, default=0.0,
                        help="総予算 (円). 100株買える銘柄のみに絞る。"
                             "--max-price と併用時は --max-price 優先")
    parser.add_argument("--input-dir",   type=Path, default=Path("walkforward_results"))
    parser.add_argument("--date",        type=str, default=str(TODAY),
                        help="読み込む CSV の日付 (デフォルト本日)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="Aggressive モードの CSV を読み込む (_aggressive suffix)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="Conservative モードの CSV を読み込む (suffix なし, デフォルト)")
    parser.add_argument("--holdout-days", type=int, default=0,
                        help="scan_walkforward.py --holdout-days N で生成した CSV を読み込む")
    parser.add_argument("--embargo-days", type=int, default=0,
                        help="scan_walkforward_embargo.py --embargo-days N で生成した CSV を読み込む (0=通常の scan_walkforward CSV)")
    args = parser.parse_args()

    mode_suffix    = "_aggressive" if args.aggressive else ""
    holdout_suffix = f"_holdout{args.holdout_days}d" if args.holdout_days > 0 else ""
    embargo_suffix = f"_embargo{args.embargo_days}d"  if args.embargo_days > 0 else ""

    # budget → max_price 換算 (FIXED_QTY=100 株)
    effective_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        effective_max_price = args.budget / 100.0

    strategies_stop      = {"MACD", "A7", "RSI2"}
    strategies_brk       = {"DON", "VOL", "MOM"}
    strategies_short     = {"A7_S", "MACD_S", "RSI2_S"}
    strategies_short_brk = {"DON_S", "MOM_S", "GAP_S"}
    all_strats = (
        list(strategies_stop) + list(strategies_brk)
        + list(strategies_short) + list(strategies_short_brk)
    )

    print("=" * 78)
    print(f"WATCHLIST 構築  基準日: {args.date}")
    print(f"  フィルター: folds>={args.min_folds}  "
          f"MaxDD<={args.max_dd}%  "
          f"連敗<={args.max_consec_losses}  "
          f"Sharpe>={args.min_sharpe}")
    if args.min_trades > 0:
        print(f"  取引回数  : TEST期間 {args.min_trades}回以上")
    if args.max_avg_hold > 0:
        print(f"  保有日数  : 平均 {args.max_avg_hold}日以下")
    if effective_max_price > 0:
        budget_str = f" (予算 {args.budget:,.0f}円)" if args.budget > 0 else ""
        print(f"  価格上限  : {effective_max_price:,.0f}円/株{budget_str}")
    if args.min_price > 0:
        print(f"  価格下限  : {args.min_price:,.0f}円/株 (低位株除外)")
    print(f"  選定数    : 戦略あたり {args.per_strategy} 銘柄")
    print("=" * 78)

    stop_blocks:      list[str] = []
    brk_blocks:       list[str] = []
    short_blocks:     list[str] = []
    short_brk_blocks: list[str] = []

    total_candidates = 0
    total_selected   = 0

    for strategy in all_strats:
        csv_path = args.input_dir / f"walkforward_{strategy}{mode_suffix}{holdout_suffix}{embargo_suffix}_{args.date}.csv"
        rows = load_csv(csv_path)
        if not rows:
            print(f"[WARN] CSV not found: {csv_path}")
            continue

        filtered = apply_filters(
            rows,
            min_folds=args.min_folds,
            max_dd_pct=args.max_dd,
            max_consec_losses=args.max_consec_losses,
            min_sharpe=args.min_sharpe,
            max_price=effective_max_price,
            min_price=args.min_price,
            min_trades=args.min_trades,
            max_avg_hold=args.max_avg_hold,
        )
        filtered.sort(key=composite_score, reverse=True)
        top = filtered[: args.per_strategy]

        print(f"\n=== {strategy} ===")
        print(f"  全候補={len(rows)}  フィルター通過={len(filtered)}  選定={len(top)}")

        if top:
            has_hold = any(r.get("avg_hold_days") for r in top)
            hold_hdr = f"{'保有日':>7}" if has_hold else ""
            print(f"  {'銘柄':<10}{'名前':<22}{'株価':>8}{'100株':>10}"
                  f"{'fld':>5}{'取引':>6}{'PnL':>12}"
                  f"{'PF':>7}{'WR':>8}{'DD%':>8}{'連敗':>6}{'Shrp':>7}{hold_hdr}")
            print("  " + "-" * (108 + (7 if has_hold else 0)))
            for r in top:
                price = _float(r.get("latest_price", 0))
                cost  = price * 100
                hold_val = (f"{_float(r.get('avg_hold_days',0)):>7.1f}"
                            if has_hold else "")
                print(f"  {r['symbol']:<10}{r['name'][:20]:<22}"
                      f"{price:>8,.0f}{cost:>10,.0f}"
                      f"{r['folds_passed']:>5}{_int(r.get('total_test_trades',0)):>6}"
                      f"{_float(r['total_test_pnl']):>+12,.0f}"
                      f"{r['avg_test_pf']:>7}{r['avg_test_wr']:>7}%"
                      f"{r['max_drawdown_pct']:>8}"
                      f"{r['max_consecutive_losses']:>6}"
                      f"{r['sharpe']:>7}{hold_val}")

        total_candidates += len(rows)
        total_selected   += len(top)

        block = format_watchlist_block(top, f"{strategy} Walk-forward 上位 {len(top)}")
        if strategy in strategies_stop:
            stop_blocks.append(block)
        elif strategy in strategies_brk:
            brk_blocks.append(block)
        elif strategy in strategies_short:
            short_blocks.append(block)
        elif strategy in strategies_short_brk:
            short_brk_blocks.append(block)

    # ── Python コード出力 ──
    out_path = Path(f"watchlist_proposal{mode_suffix}{holdout_suffix}_{args.date}.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('"""\n')
        f.write(f"新 WATCHLIST 提案 (生成日: {args.date})\n")
        f.write("scan_walkforward.py → build_watchlist.py の結果\n\n")
        f.write(f"フィルター: folds>={args.min_folds}, MaxDD<={args.max_dd}%, "
                f"連敗<={args.max_consec_losses}, Sharpe>={args.min_sharpe}\n")
        if effective_max_price > 0:
            budget_str = f" (予算 {args.budget:,.0f}円)" if args.budget > 0 else ""
            f.write(f"価格上限  : {effective_max_price:,.0f}円/株{budget_str}\n")
        f.write(f"選定数    : 戦略あたり {args.per_strategy} 銘柄\n")
        f.write(f"全候補数  : {total_candidates}  選定数 {total_selected}\n")
        f.write('"""\n\n')

        f.write("# ============================================================\n")
        f.write("# check_signals_stop.py の WATCHLIST に貼り付け (逆指値B)\n")
        f.write("# ============================================================\n")
        f.write("STOP_WATCHLIST: list[tuple[str, str, str]] = [\n")
        f.write("\n".join(stop_blocks))
        f.write("\n]\n\n")

        f.write("# ============================================================\n")
        f.write("# check_signals_breakout.py の WATCHLIST に貼り付け (ブレイクアウト)\n")
        f.write("# ============================================================\n")
        f.write("BRK_WATCHLIST: list[tuple[str, str, str]] = [\n")
        f.write("\n".join(brk_blocks))
        f.write("\n]\n\n")

        f.write("# ============================================================\n")
        f.write("# check_signals_short.py の WATCHLIST に貼り付け (ショート逆指値)\n")
        f.write("# ============================================================\n")
        f.write("SHORT_WATCHLIST: list[tuple[str, str, str]] = [\n")
        f.write("\n".join(short_blocks))
        f.write("\n]\n\n")

        f.write("# ============================================================\n")
        f.write("# check_signals_short_breakout.py の WATCHLIST に貼り付け (ショートBRK)\n")
        f.write("# ============================================================\n")
        f.write("SHORT_BRK_WATCHLIST: list[tuple[str, str, str]] = [\n")
        f.write("\n".join(short_brk_blocks))
        f.write("\n]\n")

    print(f"\n" + "=" * 78)
    print(f"完了。提案ファイル: {out_path.resolve()}")
    print(f"  選定総数: {total_selected} 銘柄")
    print("=" * 78)
    print(f"\n次ステップ:")
    print(f"  1. {out_path.name} を開き、STOP_WATCHLIST / BRK_WATCHLIST を確認")
    print(f"  2. check_signals_stop.py の WATCHLIST を置き換え")
    print(f"  3. check_signals_breakout.py の WATCHLIST を置き換え")
    print(f"  3b. check_signals_short.py の WATCHLIST を SHORT_WATCHLIST で置き換え")
    print(f"  3c. check_signals_short_breakout.py の WATCHLIST を SHORT_BRK_WATCHLIST で置き換え")
    print(f"  4. python run_signals.py --days 365 で比較検証")


if __name__ == "__main__":
    main()
