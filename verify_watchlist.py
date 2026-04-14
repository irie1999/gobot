"""
verify_watchlist.py  ―  WATCHLIST 提案ファイルの検証バックテスト
=================================================================
build_watchlist.py が生成した watchlist_proposal_<date>.py を読み込み、
check_signals_stop / check_signals_breakout のバックテストを
「提案された新 WATCHLIST」で実行し、統合 HTML を生成してブラウザで開く。

既存の check_signals_stop.py / check_signals_breakout.py の WATCHLIST 定数は
書き換えず、ランタイムで monkey-patch する方式。検証結果を見て納得してから
手動で WATCHLIST を差し替える運用を想定。

【使い方】
  python verify_watchlist.py                              # 最新の提案ファイルを自動検出
  python verify_watchlist.py --proposal watchlist_proposal_2026-04-11.py
  python verify_watchlist.py --days 180                   # 直近180日
  python verify_watchlist.py --date 2026-04-10            # 任意日シグナル確認
  python verify_watchlist.py --no-browser                 # HTML生成のみ
  python verify_watchlist.py --workers 8
  python verify_watchlist.py --stop-only                  # 逆指値Bのみ
  python verify_watchlist.py --brk-only                   # ブレイクアウトのみ

【出力】
  signals_verification_<date>.html  (signals_combined と区別するため別名)

【動作】
  1. watchlist_proposal_*.py を importlib で読み込み
  2. STOP_WATCHLIST / BRK_WATCHLIST を check_signals_stop.WATCHLIST /
     check_signals_breakout.WATCHLIST に monkey-patch
  3. run_signals._run_group と build_combined_html を再利用してバックテスト & HTML 生成
  4. ブラウザで HTML を開く

注意:
  - monkey-patch はモジュール属性を上書きするだけなので、元の WATCHLIST 定数は
    このプロセス内でのみ置き換わる (ファイルは書き換えない)
  - 検証結果は walkforward の数字より悪くなる可能性あり。run_signals と同じ
    バックテスト条件 (backtest_limit_entry.run_limit_backtest) で評価するので、
    walkforward 選定時と少し条件が違うため
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_stop     as _stop
import check_signals_breakout as _brk
from run_signals import _run_group, build_combined_html

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()


# ── 提案ファイル読み込み ─────────────────────────────────────────
def load_proposal(path: Path) -> tuple[list[tuple[str, str, str]],
                                       list[tuple[str, str, str]]]:
    """
    watchlist_proposal_*.py を読み込んで
    (STOP_WATCHLIST, BRK_WATCHLIST) のタプルを返す。
    """
    if not path.exists():
        raise FileNotFoundError(f"提案ファイルが見つかりません: {path}")

    spec = importlib.util.spec_from_file_location("_proposal", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    stop_wl = [tuple(t) for t in getattr(mod, "STOP_WATCHLIST", [])]
    brk_wl  = [tuple(t) for t in getattr(mod, "BRK_WATCHLIST",  [])]

    if not stop_wl and not brk_wl:
        raise ValueError(
            f"{path} に STOP_WATCHLIST / BRK_WATCHLIST が見つかりません。"
        )
    return stop_wl, brk_wl


def auto_detect_proposal() -> Path | None:
    """最新の watchlist_proposal_*.py を自動検出。"""
    candidates = sorted(Path(".").glob("watchlist_proposal_*.py"), reverse=True)
    return candidates[0] if candidates else None


# ── 戦略別サマリー表示 ───────────────────────────────────────────
def _print_strategy_summary(label: str, items: list[dict], show_days: int) -> None:
    print(f"\n[{label}]  全 {len(items)} 銘柄×戦略")
    by_strat: dict[str, dict] = {}
    for item in items:
        strat = item["strategy"]
        if strat not in by_strat:
            by_strat[strat] = {"trades": 0, "wins": 0, "pnl": 0.0, "gp": 0.0, "gl": 0.0}
        pr = item["period_results"].get(show_days) or {}
        if pr:
            by_strat[strat]["trades"] += pr["trades"]
            by_strat[strat]["wins"]   += pr["wins"]
            by_strat[strat]["pnl"]    += pr["total_pnl"]
            for t in pr.get("trade_log", []):
                if t["pnl"] > 0:
                    by_strat[strat]["gp"] += t["pnl"]
                else:
                    by_strat[strat]["gl"] += abs(t["pnl"])

    if not by_strat:
        print("  (データなし)")
        return

    print(f"  {'戦略':<6}{'取引':>6}{'勝':>6}{'勝率':>8}{'PF':>8}{'損益':>14}")
    print("  " + "-" * 50)
    total = {"trades": 0, "wins": 0, "pnl": 0.0, "gp": 0.0, "gl": 0.0}
    for strat, s in sorted(by_strat.items()):
        wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
        pf = s["gp"] / s["gl"] if s["gl"] > 0 else (float("inf") if s["gp"] > 0 else 0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        print(f"  {strat:<6}{s['trades']:>6}{s['wins']:>6}"
              f"{wr:>7.1f}%{pf_s:>8}{s['pnl']:>+14,.0f}")
        for k in total:
            total[k] += s[k]
    wr = total["wins"] / total["trades"] * 100 if total["trades"] > 0 else 0
    pf = total["gp"] / total["gl"] if total["gl"] > 0 else (float("inf") if total["gp"] > 0 else 0)
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    print("  " + "-" * 50)
    print(f"  {'計':<6}{total['trades']:>6}{total['wins']:>6}"
          f"{wr:>7.1f}%{pf_s:>8}{total['pnl']:>+14,.0f}")


# ── メイン ───────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="WATCHLIST 提案ファイル検証バックテスト")
    parser.add_argument("--proposal",    type=Path, default=None,
                        help="watchlist_proposal_*.py のパス (省略時は最新を自動検出)")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None,
                        help="シグナル確認日 YYYY-MM-DD (省略時=本日)")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--workers",     type=int, default=_stop._DEFAULT_WORKERS)
    parser.add_argument("--stop-only",   action="store_true",
                        help="逆指値B (MACD/A7/RSI2) のみ検証")
    parser.add_argument("--brk-only",    action="store_true",
                        help="ブレイクアウト (DON/VOL/MOM) のみ検証")
    # モード選択 (実際の切替は import 前に sys.argv で行う)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="積極利確モード (tm=1.5, 目標+4.5%)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="標準モード (tm=3.0, デフォルト)")
    args = parser.parse_args()

    # ── 提案ファイル決定 ──
    if args.proposal is None:
        detected = auto_detect_proposal()
        if detected is None:
            print("[ERROR] watchlist_proposal_*.py が見つかりません。", file=sys.stderr)
            print("  先に  python build_watchlist.py  を実行してください。",
                  file=sys.stderr)
            sys.exit(1)
        args.proposal = detected
        print(f"自動検出: {args.proposal}")

    try:
        stop_wl, brk_wl = load_proposal(args.proposal)
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    # ── WATCHLIST を monkey-patch ──
    original_stop = list(_stop.WATCHLIST)
    original_brk  = list(_brk.WATCHLIST)

    if not args.brk_only:
        _stop.WATCHLIST = stop_wl
    if not args.stop_only:
        _brk.WATCHLIST  = brk_wl

    # ── シグナル確認日 ──
    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None
    date_label = args.date if args.date else "本日"

    # ── 概要表示 ──
    print("=" * 78)
    print(f"WATCHLIST 検証バックテスト  モード: {_stop.TRADING_MODE}")
    print(f"  提案ファイル: {args.proposal}")
    print(f"  逆指値B     : {len(stop_wl)} 銘柄×戦略"
          f"{' (スキップ)' if args.brk_only else ''}")
    print(f"  ブレイクアウト: {len(brk_wl)} 銘柄×戦略"
          f"{' (スキップ)' if args.stop_only else ''}")
    print(f"  期間         : {args.days} 日")
    print(f"  シグナル確認日: {date_label}")
    print(f"  Workers      : {args.workers}")
    print("=" * 78)
    print(f"\nバックテスト実行中...", flush=True)

    # ── 並列バックテスト (両グループ) ──
    stop_items: list[dict] = []
    brk_items:  list[dict] = []

    with ThreadPoolExecutor(max_workers=2) as outer:
        fut_stop = None
        fut_brk  = None
        if not args.brk_only:
            fut_stop = outer.submit(_run_group, _stop, sig_date, args.workers)
        if not args.stop_only:
            fut_brk  = outer.submit(_run_group, _brk,  sig_date, args.workers)
        if fut_stop:
            stop_items = fut_stop.result()
        if fut_brk:
            brk_items  = fut_brk.result()

    # ── コンソールサマリー ──
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    print("\n" + "=" * 78)
    print(f"  検証結果  {today_str}  (期間: {args.days}日)")
    print("=" * 78)

    if not args.brk_only:
        _print_strategy_summary("逆指値B", stop_items, args.days)
    if not args.stop_only:
        _print_strategy_summary("ブレイクアウト", brk_items, args.days)

    print("\n  ※ この数字も Walk-forward の数字より楽観的になる可能性があります。")
    print("     本番運用では勝率・PF とも更に 10-20% 程度落ちると見積もってください。")

    # ── 本日シグナル一覧 ──
    all_sigs: list[tuple] = []
    for item in stop_items:
        if item.get("today_sig"):
            score, rank = _stop.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "逆指値B"))
    for item in brk_items:
        if item.get("today_sig"):
            score, rank = _brk.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "BRK"))
    all_sigs.sort(key=lambda x: x[1], reverse=True)

    print(f"\n【シグナル ({date_label})】 {len(all_sigs)}件")
    if all_sigs:
        print(f"  {'銘柄':<12}{'名前':<22}{'戦略':<6}{'種別':<8}"
              f"{'シグナル日':<12}{'信号株価':>8}{'現在値':>8}"
              f"{'逆指値':>8}{'損切り':>8}{'目標':>8}  スコア")
        print("  " + "-" * 120)
        for item, score, rank, kind in all_sigs:
            sig = item["today_sig"]
            print(f"  {item['symbol']:<12}{item['name'][:20]:<22}{item['strategy']:<6}{kind:<8}"
                  f"{sig['signal_date']:<12}{sig['signal_price']:>8,.0f}"
                  f"{sig['current_price']:>8,.0f}{sig['order_price']:>8,.0f}"
                  f"{sig['stop_price']:>8,.0f}{sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    # ── HTML 生成 ──
    print(f"\nHTMLレポート生成中...", flush=True)
    show_days = args.days
    stop_html = _stop.build_html(stop_items, show_days, date_label) if stop_items else ""
    brk_html  = _brk.build_html(brk_items,  show_days, date_label) if brk_items  else ""

    # 片方しか無い場合はフルHTMLを直接出力
    date_suffix = args.date if args.date else today_str
    mode_suffix = f"_{_stop.TRADING_MODE}" if _stop.TRADING_MODE != "conservative" else ""
    out_path    = Path(f"signals_verification{mode_suffix}_{date_suffix}.html")

    if stop_html and brk_html:
        combined = build_combined_html(stop_html, brk_html)
    elif stop_html:
        combined = stop_html
    elif brk_html:
        combined = brk_html
    else:
        print("[ERROR] HTMLコンテンツが生成されませんでした", file=sys.stderr)
        # WATCHLIST を元に戻してから終了
        _stop.WATCHLIST = original_stop
        _brk.WATCHLIST  = original_brk
        sys.exit(1)

    out_path.write_text(combined, encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    # ── WATCHLIST を元に戻す (念のため) ──
    _stop.WATCHLIST = original_stop
    _brk.WATCHLIST  = original_brk

    # ── ブラウザ起動 ──
    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())

    print("\n完了。")
    print("  比較したい場合:  python run_signals.py --days {days}  (旧 WATCHLIST)"
          .format(days=args.days))
    print("  採用する場合  :  watchlist_proposal_*.py の内容を check_signals_*.py に貼り付け")


if __name__ == "__main__":
    main()
