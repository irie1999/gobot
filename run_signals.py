"""
run_signals.py  ―  逆指値シグナル統合レポート
==================================================
check_signals_stop.py（MACD / A7 / RSI2 逆指値B） +
check_signals_breakout.py（DON / VOL / MOM ブレイクアウト）
を1コマンドで実行し、タブ付きHTMLに統合する。

【使い方】
  python run_signals.py                    # 全期間(365日) HTMLレポート (aggressive)
  python run_signals.py --days 90          # 直近90日
  python run_signals.py --date 2026-04-08  # 任意日シグナル確認
  python run_signals.py --signal-only      # シグナルのみ表示
  python run_signals.py --no-browser       # HTML生成のみ（ブラウザ起動しない）
  python run_signals.py --conservative     # 旧モード (tm=3.0 目標+9%)

※ デフォルトは aggressive モード (2026-04-14 以降)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 (check_signals_* が読み取る) ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_stop     as _stop
import check_signals_breakout as _brk

JST = timezone(timedelta(hours=9))


# ── グループ単位でバックテスト + シグナル確認 ────────────────────────────
def _run_group(mod, sig_date, workers: int) -> list[dict]:
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in mod.WATCHLIST}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass

    order = {(s, st): i for i, (s, _, st) in enumerate(mod.WATCHLIST)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    for item in all_items:
        item["today_sig"] = mod.check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)
    return all_items


# ── 統合タブHTML生成 ─────────────────────────────────────────────────────
def _extract_body(html: str) -> str:
    m = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else html


def _extract_style(html: str) -> str:
    m = re.search(r'<style[^>]*>(.*?)</style>', html, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def build_combined_html(stop_html: str, brk_html: str) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    stop_css  = _extract_style(stop_html)
    brk_css   = _extract_style(brk_html)
    stop_body = _extract_body(stop_html)
    brk_body  = _extract_body(brk_html)

    # ブレイクアウト固有のタグ色（stop_css には含まれない）だけ追加
    extra_css = "\n".join(
        line for line in brk_css.splitlines()
        if any(k in line for k in ("tag-don", "tag-vol", "tag-mom"))
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>逆指値シグナル統合レポート — {today_str}</title>
<style>
{stop_css}
{extra_css}
.tab-nav{{display:flex;gap:0;background:#090b14;padding:10px 16px 0;border-bottom:2px solid #252840;position:sticky;top:0;z-index:200;flex-wrap:wrap}}
.tab-btn{{background:#16192a;color:#94a3b8;border:1px solid #252840;border-bottom:none;padding:9px 22px;margin-right:4px;border-radius:6px 6px 0 0;cursor:pointer;font-size:13px;font-family:inherit;transition:background .15s,color .15s}}
.tab-btn:hover{{background:#1e2235;color:#dde1ec}}
.tab-btn.active{{background:#0f1117;color:#38bdf8;border-color:#38bdf8;border-bottom-color:#0f1117}}
.tab-pane{{min-height:60vh}}
</style>
</head>
<body>
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab(0)">逆指値B（MACD / A7 / RSI2）</button>
  <button class="tab-btn"        onclick="switchTab(1)">ブレイクアウト（DON / VOL / MOM）</button>
</div>
<div id="tc0" class="tab-pane">
{stop_body}
</div>
<div id="tc1" class="tab-pane" style="display:none">
{brk_body}
</div>
<script>
function switchTab(n){{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{b.classList.toggle('active',i===n);}});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{t.style.display=i===n?'block':'none';}});
}}
</script>
</body>
</html>"""


# ── メイン ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="逆指値シグナル統合レポート")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None,
                        help="シグナル確認日 YYYY-MM-DD（省略時=本日）")
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true",
                        help="シグナルのみ表示（HTML生成をスキップ）")
    parser.add_argument("--workers",     type=int, default=_stop._DEFAULT_WORKERS)
    # モード選択 (実際の切替は import 前に sys.argv で行う)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="積極利確モード (tm=1.5, 目標+4.5%, デフォルト)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="標準モード (tm=3.0, 目標+9%, 旧デフォルト)")
    args = parser.parse_args()

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"
    n_total = len(_stop.WATCHLIST) + len(_brk.WATCHLIST)
    print(f"逆指値シグナル統合 開始 ({n_total}銘柄) シグナル確認日: {date_label}  モード: {_stop.TRADING_MODE}", flush=True)
    print(f"  逆指値B: {len(_stop.WATCHLIST)}銘柄  /  ブレイクアウト: {len(_brk.WATCHLIST)}銘柄", flush=True)

    # 両グループを並列実行
    with ThreadPoolExecutor(max_workers=2) as outer:
        fut_stop = outer.submit(_run_group, _stop, sig_date, args.workers)
        fut_brk  = outer.submit(_run_group, _brk,  sig_date, args.workers)
    stop_items = fut_stop.result()
    brk_items  = fut_brk.result()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 92)
    print(f"  逆指値シグナル統合  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
    print("=" * 92)

    # 全シグナルをスコア降順でまとめて表示
    all_sigs: list[tuple] = []
    for item in stop_items:
        if item["today_sig"]:
            score, rank = _stop.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "逆指値B"))
    for item in brk_items:
        if item["today_sig"]:
            score, rank = _brk.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "BRK"))
    all_sigs.sort(key=lambda x: x[1], reverse=True)

    print(f"\n【シグナル ({date_label})】 {len(all_sigs)}件")
    if all_sigs:
        print(f"  {'銘柄':<12} {'名前':<24} {'戦略':<6} {'種別':<8} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値':>8} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 124)
        for item, score, rank, kind in all_sigs:
            sig = item["today_sig"]
            print(f"  {item['symbol']:<12} {item['name']:<24} {item['strategy']:<6} {kind:<8}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    show_days = args.days
    print(f"\nHTMLレポート生成中...", flush=True)
    stop_html = _stop.build_html(stop_items, show_days, date_label)
    brk_html  = _brk.build_html(brk_items,  show_days, date_label)

    date_suffix = args.date if args.date else today
    # aggressive (デフォルト) は suffix なし、conservative は "_conservative"
    mode_suffix = f"_{_stop.TRADING_MODE}" if _stop.TRADING_MODE != "aggressive" else ""
    out_path    = Path(f"signals_combined{mode_suffix}_{date_suffix}.html")
    out_path.write_text(build_combined_html(stop_html, brk_html), encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
