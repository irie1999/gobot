"""
run_margin.py  ―  信用取引版 逆指値シグナルレポート
=================================================================
run_signals.py と同じバックテスト・シグナル表示を行うが、
信用取引の条件 (手数料無料・金利・レバレッジ) で計算する。

【現物版 (run_signals.py) との違い】
  手数料    : 0 円 (SBI ゼロ革命想定)
  金利      : 年 2.8% (制度信用 買方金利) → 保有日数に応じて PnL から控除
  MAX_HOLD  : 10 日 (現物15日 → 金利節約)
  実効予算  : 保証金 × レバレッジ倍率 (デフォルト 1.5 倍)
  出力ファイル: signals_margin_<date>.html

【使い方】
  python run_margin.py                       # デフォルト (60万 × 1.5倍 = 90万)
  python run_margin.py --budget 600000       # 保証金 60万円
  python run_margin.py --leverage 2.0        # レバレッジ 2.0 倍
  python run_margin.py --days 180            # 直近180日
  python run_margin.py --date 2026-04-15     # 任意日シグナル
  python run_margin.py --no-browser          # ブラウザ起動しない
  python run_margin.py --interest 0.028      # 金利 年2.8%

【リスク警告】
  信用取引はレバレッジにより損失が拡大します。
  追証 (追加保証金) が発生する可能性があります。
  このスクリプトの出力は投資助言ではありません。
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 信用取引の定数をモジュール import 前に準備 ───────────────────
# (import 後に monkey-patch で上書きする)

import backtest_limit_entry as _ble

# 信用取引用デフォルト定数
MARGIN_FEE_PCT       = 0.0      # SBI 信用: 手数料無料
MARGIN_INTEREST_RATE = 0.028    # 年 2.8% (制度信用 買方金利)
MARGIN_MAX_HOLD      = 10       # 金利節約のため 15 → 10 日
MARGIN_SLIPPAGE      = 0.005    # スリッページは現物と同じ
DEFAULT_DEPOSIT      = 600_000  # 保証金 60 万円
DEFAULT_LEVERAGE     = 1.5      # レバレッジ 1.5 倍 (推奨)

JST = timezone(timedelta(hours=9))


def _patch_constants(max_hold: int, fee: float, initial_cash: int) -> None:
    """backtest_limit_entry のモジュール定数を信用取引用に上書き。"""
    _ble.FEE_PCT_ONE_WAY = fee
    _ble.MAX_HOLD        = max_hold
    _ble.INITIAL_CASH    = initial_cash


def _restore_constants(orig: dict) -> None:
    """元の定数に復元。"""
    for k, v in orig.items():
        setattr(_ble, k, v)


def _add_interest_to_trades(items: list[dict], annual_rate: float) -> None:
    """
    全トレードの PnL から信用金利を差し引く (インプレース)。
    金利 = エントリー価格 × 数量 × 日利 × 保有日数
    """
    daily_rate = annual_rate / 365.0
    for item in items:
        for period, pr in item.get("period_results", {}).items():
            if not pr:
                continue
            total_interest = 0.0
            for t in pr.get("trade_log", []):
                hold = max(int(t.get("hold_days", 0) or 0), 1)
                entry_p = float(t.get("entry_p", 0))
                qty     = int(t.get("qty", 100))
                interest = entry_p * qty * daily_rate * hold
                t["interest"]  = round(interest, 0)
                t["pnl"]      -= interest
                t["pnl"]       = round(t["pnl"], 0)
                total_interest += interest
            # 集計を再計算
            trades = pr.get("trade_log", [])
            if trades:
                pr["total_pnl"]  = sum(t["pnl"] for t in trades)
                pr["wins"]       = sum(1 for t in trades if t["pnl"] > 0)
                pr["losses"]     = sum(1 for t in trades if t["pnl"] <= 0)
                filled           = len(trades)
                pr["win_rate"]   = pr["wins"] / filled * 100 if filled > 0 else 0
                gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
                gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
                pr["pf"] = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
                pr["total_interest"] = round(total_interest, 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="信用取引版 逆指値シグナルレポート",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="⚠ 信用取引はレバレッジにより損失が拡大します。投資は自己責任で。")
    parser.add_argument("--days",      type=int,   default=365)
    parser.add_argument("--date",      type=str,   default=None,
                        help="シグナル確認日 YYYY-MM-DD")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--workers",   type=int,   default=_ble.WORKERS)
    parser.add_argument("--budget",    type=float, default=DEFAULT_DEPOSIT,
                        help=f"保証金 (円, デフォルト {DEFAULT_DEPOSIT:,.0f})")
    parser.add_argument("--leverage",  type=float, default=DEFAULT_LEVERAGE,
                        help=f"レバレッジ倍率 (デフォルト {DEFAULT_LEVERAGE})")
    parser.add_argument("--interest",  type=float, default=MARGIN_INTEREST_RATE,
                        help=f"年利 (デフォルト {MARGIN_INTEREST_RATE})")
    parser.add_argument("--max-hold",  type=int,   default=MARGIN_MAX_HOLD,
                        help=f"最大保有日数 (デフォルト {MARGIN_MAX_HOLD})")
    parser.add_argument("--signal-only", action="store_true",
                        help="シグナルのみ表示")
    args = parser.parse_args()

    effective_cash = int(args.budget * args.leverage)

    # ── 元の定数を退避 ──
    orig = {
        "FEE_PCT_ONE_WAY": _ble.FEE_PCT_ONE_WAY,
        "MAX_HOLD":        _ble.MAX_HOLD,
        "INITIAL_CASH":    _ble.INITIAL_CASH,
    }

    # ── 信用取引用に上書き ──
    _patch_constants(
        max_hold=args.max_hold,
        fee=MARGIN_FEE_PCT,
        initial_cash=effective_cash,
    )

    # ── import (定数パッチ後) ──
    import check_signals_stop     as _stop
    import check_signals_breakout as _brk
    from run_signals import _run_group, build_combined_html

    # check_signals 側の _INITIAL_CASH も上書き
    _stop._INITIAL_CASH = effective_cash
    _brk._INITIAL_CASH  = effective_cash

    # シグナル確認日
    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None
    date_label = args.date if args.date else "本日"

    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # ── ヘッダー表示 ──
    print("=" * 78)
    print(f"信用取引版 逆指値シグナル統合レポート")
    print(f"  基準日       : {today_str}")
    print(f"  保証金       : {args.budget:,.0f} 円")
    print(f"  レバレッジ   : {args.leverage:.1f} 倍")
    print(f"  実効購入力   : {effective_cash:,.0f} 円")
    print(f"  手数料       : 無料 (SBI 信用想定)")
    print(f"  金利 (年率)  : {args.interest*100:.2f}%")
    print(f"  MAX_HOLD     : {args.max_hold} 日 (現物版: 15日)")
    print(f"  シグナル確認 : {date_label}")
    print(f"  モード       : {_stop.TRADING_MODE}")
    print("=" * 78)
    print()
    print("⚠  信用取引はレバレッジにより損失が拡大します。")
    print("⚠  追証 (追加保証金) が発生する可能性があります。")
    print("=" * 78)

    n_total = len(_stop.WATCHLIST) + len(_brk.WATCHLIST)
    print(f"\nバックテスト実行中 ({n_total} 銘柄)...", flush=True)

    # ── 並列バックテスト ──
    with ThreadPoolExecutor(max_workers=2) as outer:
        fut_stop = outer.submit(_run_group, _stop, sig_date, args.workers)
        fut_brk  = outer.submit(_run_group, _brk,  sig_date, args.workers)
    stop_items = fut_stop.result()
    brk_items  = fut_brk.result()

    # ── 金利コストを PnL から控除 ──
    _add_interest_to_trades(stop_items, args.interest)
    _add_interest_to_trades(brk_items,  args.interest)

    # ── サマリー表示 ──
    print("\n" + "=" * 78)
    print(f"  信用取引版  {today_str}  ({args.days}日)  "
          f"保証金{args.budget:,.0f}円 × {args.leverage}倍")
    print("=" * 78)

    all_sigs: list[tuple] = []
    for label, items, mod in [("逆指値B", stop_items, _stop),
                               ("ブレイクアウト", brk_items, _brk)]:
        print(f"\n[{label}]")
        by_strat: dict[str, dict] = {}
        for item in items:
            strat = item["strategy"]
            if strat not in by_strat:
                by_strat[strat] = {"trades": 0, "wins": 0, "pnl": 0.0, "interest": 0.0}
            pr = item["period_results"].get(args.days) or {}
            if pr:
                by_strat[strat]["trades"] += pr.get("trades", 0)
                by_strat[strat]["wins"]   += pr.get("wins", 0)
                by_strat[strat]["pnl"]    += pr.get("total_pnl", 0)
                by_strat[strat]["interest"] += pr.get("total_interest", 0)

        print(f"  {'戦略':<6}{'取引':>6}{'勝率':>8}{'損益':>14}{'金利控除':>12}")
        print("  " + "-" * 50)
        for strat, s in by_strat.items():
            wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
            print(f"  {strat:<6}{s['trades']:>6}{wr:>7.1f}%"
                  f"{s['pnl']:>+14,.0f}{s['interest']:>12,.0f}")

        for item in items:
            if item.get("today_sig"):
                score, rank = mod.calc_recommend_score(item["period_results"])
                all_sigs.append((item, score, rank,
                                 "逆指値B" if label == "逆指値B" else "BRK"))

    all_sigs.sort(key=lambda x: x[1], reverse=True)

    print(f"\n【シグナル ({date_label})】 {len(all_sigs)}件")
    if all_sigs:
        print(f"  {'銘柄':<12}{'名前':<22}{'戦略':<6}{'逆指値':>8}"
              f"{'指値上限':>8}{'損切り':>8}{'目標':>8}  スコア")
        print("  " + "-" * 100)
        for item, score, rank, kind in all_sigs:
            sig = item["today_sig"]
            limit_entry = sig.get("limit_entry_price", sig["order_price"])
            print(f"  {item['symbol']:<12}{item['name'][:20]:<22}"
                  f"{item['strategy']:<6}"
                  f"{sig['order_price']:>8,.0f}{limit_entry:>8,.0f}"
                  f"{sig['stop_price']:>8,.0f}{sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    if args.signal_only:
        _restore_constants(orig)
        return

    # ── HTML 生成 ──
    print(f"\nHTMLレポート生成中...", flush=True)
    stop_html = _stop.build_html(stop_items, args.days, date_label)
    brk_html  = _brk.build_html(brk_items,  args.days, date_label)

    # HTML タイトルを信用取引版に書き換え
    combined = build_combined_html(stop_html, brk_html)
    combined = combined.replace(
        "逆指値シグナル統合レポート",
        f"【信用取引版】逆指値シグナルレポート "
        f"(保証金{args.budget/10000:.0f}万×{args.leverage}倍 "
        f"金利{args.interest*100:.1f}% MAX_HOLD={args.max_hold}日)",
    )

    date_suffix = args.date if args.date else today_str
    out_path = Path(f"signals_margin_{date_suffix}.html")
    out_path.write_text(combined, encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    # ── 定数を復元 ──
    _restore_constants(orig)

    # ── ブラウザ起動 ──
    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())

    print(f"\n完了。")
    print(f"  現物版と比較: python run_signals.py --days {args.days}")


if __name__ == "__main__":
    main()
