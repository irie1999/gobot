"""
run_signals_merged.py  ―  既存 + Walk-forward 銘柄 統合シグナルレポート
=======================================================================
既存 WATCHLIST (check_signals_stop / check_signals_breakout) と
Walk-forward 選定銘柄 (2026-05-08) を合算し、スコア降順で表示する。

既存ファイルは一切変更しない。

【使い方】
  python run_signals_merged.py                   # 全期間(365日) HTMLレポート
  python run_signals_merged.py --days 90         # 直近90日
  python run_signals_merged.py --date 2026-04-08 # 任意日シグナル確認
  python run_signals_merged.py --signal-only     # シグナルのみ表示
  python run_signals_merged.py --no-browser      # HTML生成のみ
  python run_signals_merged.py --aggressive      # 積極利確モード
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# TRADING_MODE を import 前に設定
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_stop     as _stop
import check_signals_breakout as _brk
import check_signals_short    as _short
from run_signals import _run_group, build_combined_html, _extract_style, _extract_body

JST = timezone(timedelta(hours=9))

# ── Walk-forward 選定銘柄 (2026-05-08, budget=50万) ──────────────────────────
_WF_STOP: list[tuple[str, str, str]] = [
    ("1515.T", "日鉄鉱業",       "MACD"),
    ("3741.T", "セック",         "MACD"),
    ("4301.T", "アミューズ",     "MACD"),
    ("6754.T", "アンリツ",       "MACD"),
    ("3946.T", "トーモク",       "MACD"),
    ("6473.T", "ジェイテクト",   "MACD"),
    ("1975.T", "朝日工業社",     "MACD"),
    ("7282.T", "豊田合成",       "MACD"),
    ("6376.T", "日機装",         "MACD"),
    ("3245.T", "ディア・ライフ", "MACD"),
    ("8061.T", "西華産業",       "A7"),
    ("1814.T", "大末建設",       "A7"),
    ("5831.T", "しずおかFG",     "A7"),
    ("2540.T", "養命酒製造",     "A7"),
    ("1803.T", "清水建設",       "A7"),
    ("8237.T", "松屋",           "A7"),
    ("8795.T", "T&DHD",          "A7"),
    ("1885.T", "東亜建設工業",   "A7"),
    ("6376.T", "日機装",         "A7"),
    ("4385.T", "メルカリ",       "A7"),
    ("7011.T", "三菱重工業",     "RSI2"),
    ("8551.T", "北日本銀行",     "RSI2"),
    ("9305.T", "ヤマタネ",       "RSI2"),
    ("7012.T", "川崎重工業",     "RSI2"),
    ("6644.T", "大崎電気工業",   "RSI2"),
    ("8060.T", "キヤノンMJ",     "RSI2"),
    ("4631.T", "DIC",            "RSI2"),
    ("5541.T", "大平洋金属",     "RSI2"),
    ("9418.T", "U-NEXT HD",      "RSI2"),
    ("7936.T", "アシックス",     "RSI2"),
]

_WF_BRK: list[tuple[str, str, str]] = [
    ("5715.T", "古河機械金属",       "DON"),
    ("8386.T", "百十四銀行",         "DON"),
    ("3741.T", "セック",             "DON"),
    ("1938.T", "日本リーテック",     "DON"),
    ("4028.T", "石原産業",           "DON"),
    ("5482.T", "愛知製鋼",           "DON"),
    ("1815.T", "鉄建建設",           "DON"),
    ("6309.T", "巴工業",             "DON"),
    ("7981.T", "タカラスタンダード", "DON"),
    ("7231.T", "トピー工業",         "DON"),
    ("1515.T", "日鉄鉱業",           "VOL"),
    ("6332.T", "月島HD",             "VOL"),
    ("2726.T", "パルグループHD",     "VOL"),
    ("3946.T", "トーモク",           "VOL"),
    ("4554.T", "富士製薬工業",       "VOL"),
    ("6323.T", "ローツェ",           "VOL"),
    ("8237.T", "松屋",               "VOL"),
    ("7231.T", "トピー工業",         "VOL"),
    ("8935.T", "FJネクストHD",       "VOL"),
    ("6143.T", "ソディック",         "VOL"),
    ("1515.T", "日鉄鉱業",           "MOM"),
    ("8387.T", "四国銀行",           "MOM"),
    ("8237.T", "松屋",               "MOM"),
    ("9412.T", "スカパーJSAT HD",    "MOM"),
    ("4554.T", "富士製薬工業",       "MOM"),
    ("1961.T", "三機工業",           "MOM"),
    ("7453.T", "良品計画",           "MOM"),
    ("7389.T", "あいちFG",           "MOM"),
    ("1938.T", "日本リーテック",     "MOM"),
    ("1975.T", "朝日工業社",         "MOM"),
]


def _build_merged_html(stop_html: str, brk_html: str, srt_html: str,
                       wf_stop: list, wf_brk: list) -> str:
    """WF バッジ付きの統合HTML を生成する。既存ファイルは変更しない。"""
    from datetime import datetime
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    stop_css  = _extract_style(stop_html)
    brk_css   = _extract_style(brk_html)
    srt_css   = _extract_style(srt_html) if srt_html else ""
    stop_body = _extract_body(stop_html)
    brk_body  = _extract_body(brk_html)
    srt_body  = _extract_body(srt_html) if srt_html else ""

    extra_css = "\n".join(
        line for line in (brk_css + "\n" + srt_css).splitlines()
        if any(k in line for k in ("tag-don", "tag-vol", "tag-mom",
                                   "tag-macd_s", "tag-a7_s", "tag-rsi2_s"))
    )

    # WF シンボルセット (symbol のみで判定 — 戦略は複数ありうる)
    wf_syms = sorted({s for s, _, _ in (wf_stop + wf_brk)})
    wf_syms_js = "[" + ",".join(f'"{s}"' for s in wf_syms) + "]"

    srt_tab_btn  = '<button class="tab-btn" onclick="switchTab(2)">ショート逆指値（A7_S）</button>' if srt_html else ""
    srt_tab_pane = f'<div id="tc2" class="tab-pane" style="display:none">\n{srt_body}\n</div>' if srt_html else ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>統合シグナルレポート（既存+WF） — {today_str}</title>
<style>
{stop_css}
{extra_css}
.tab-nav{{display:flex;gap:0;background:#090b14;padding:10px 16px 0;border-bottom:2px solid #252840;position:sticky;top:0;z-index:200;flex-wrap:wrap}}
.tab-btn{{background:#16192a;color:#94a3b8;border:1px solid #252840;border-bottom:none;padding:9px 22px;margin-right:4px;border-radius:6px 6px 0 0;cursor:pointer;font-size:13px;font-family:inherit;transition:background .15s,color .15s}}
.tab-btn:hover{{background:#1e2235;color:#dde1ec}}
.tab-btn.active{{background:#0f1117;color:#38bdf8;border-color:#38bdf8;border-bottom-color:#0f1117}}
.tab-pane{{min-height:60vh}}
.wf-badge{{display:inline-block;background:#7c3aed;color:#fff;font-size:10px;font-weight:700;
           padding:1px 5px;border-radius:3px;margin-left:5px;vertical-align:middle;letter-spacing:.5px}}
.wf-row td:first-child{{border-left:3px solid #7c3aed !important}}
.legend-bar{{background:#0f1117;border:1px solid #252840;border-radius:6px;
             padding:8px 16px;margin:10px 0 4px;font-size:12px;color:#94a3b8;display:flex;gap:20px;align-items:center}}
.legend-bar .wf-badge{{font-size:11px}}
</style>
</head>
<body>
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab(0)">逆指値B（MACD / A7 / RSI2）</button>
  <button class="tab-btn"        onclick="switchTab(1)">ブレイクアウト（DON / VOL / MOM）</button>
  {srt_tab_btn}
</div>
<div id="tc0" class="tab-pane">
{stop_body}
</div>
<div id="tc1" class="tab-pane" style="display:none">
{brk_body}
</div>
{srt_tab_pane}
<script>
var WF_SYMS = {wf_syms_js};

function switchTab(n){{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{b.classList.toggle('active',i===n);}});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{t.style.display=i===n?'block':'none';}});
}}

// 全テーブル行をスキャンして WF シンボルを含む行にバッジを付ける
function markWFRows() {{
  document.querySelectorAll('table tr').forEach(function(tr) {{
    var first = tr.cells[0];
    if (!first) return;
    var text = first.innerText || first.textContent || '';
    for (var i = 0; i < WF_SYMS.length; i++) {{
      if (text.indexOf(WF_SYMS[i]) !== -1) {{
        tr.classList.add('wf-row');
        // バッジがまだなければ追加
        if (!first.querySelector('.wf-badge')) {{
          var badge = document.createElement('span');
          badge.className = 'wf-badge';
          badge.textContent = 'WF';
          badge.title = 'Walk-forward 選定銘柄';
          first.appendChild(badge);
        }}
        break;
      }}
    }}
  }});

  // 凡例を各タブペインの先頭に挿入
  document.querySelectorAll('.tab-pane').forEach(function(pane) {{
    if (pane.querySelector('.legend-bar')) return;
    var leg = document.createElement('div');
    leg.className = 'legend-bar';
    leg.innerHTML = '<span><span class="wf-badge">WF</span> Walk-forward 選定銘柄（新規）</span>'
                  + '<span style="color:#555">｜</span>'
                  + '<span>バッジなし = 既存銘柄</span>';
    pane.insertBefore(leg, pane.firstChild);
  }});
}}

document.addEventListener('DOMContentLoaded', markWFRows);
</script>
</body>
</html>"""


def _merged_watchlist(existing: list, walkforward: list) -> list:
    """既存 + Walk-forward を重複なしで合算する。"""
    seen = set()
    merged = []
    for entry in existing + walkforward:
        key = (entry[0], entry[2])  # (symbol, strategy)
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    return merged


def _run_group_with_list(mod, watchlist, sig_date, workers: int) -> list[dict]:
    """指定WATCHLISTでバックテスト + シグナル確認。"""
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in watchlist}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass

    order = {(s, st): i for i, (s, _, st) in enumerate(watchlist)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    for item in all_items:
        item["today_sig"] = mod.check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)
    return all_items


def main() -> None:
    parser = argparse.ArgumentParser(description="既存+Walk-forward 統合シグナルレポート")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None)
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int, default=_stop._DEFAULT_WORKERS)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="積極利確モード (tm=1.5, 目標+4.5%%)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="標準モード (tm=3.0, 目標+9%%, デフォルト)")
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

    # 合算 WATCHLIST を生成
    merged_stop = _merged_watchlist(_stop.WATCHLIST, _WF_STOP)
    merged_brk  = _merged_watchlist(_brk.WATCHLIST,  _WF_BRK)

    n_stop = len(merged_stop)
    n_brk  = len(merged_brk)
    n_srt  = len(_short.WATCHLIST)
    print(f"統合シグナル 開始  モード: {_stop.TRADING_MODE}")
    print(f"  逆指値B : {n_stop}銘柄 (既存{len(_stop.WATCHLIST)} + WF{len(_WF_STOP)} - 重複)")
    print(f"  BRK    : {n_brk}銘柄 (既存{len(_brk.WATCHLIST)} + WF{len(_WF_BRK)} - 重複)")
    print(f"  ショート: {n_srt}銘柄", flush=True)

    # 3グループを並列実行
    with ThreadPoolExecutor(max_workers=3) as outer:
        fut_stop  = outer.submit(_run_group_with_list, _stop,  merged_stop,        sig_date, args.workers)
        fut_brk   = outer.submit(_run_group_with_list, _brk,   merged_brk,         sig_date, args.workers)
        fut_short = outer.submit(_run_group,           _short,                      sig_date, args.workers)
    stop_items  = fut_stop.result()
    brk_items   = fut_brk.result()
    short_items = fut_short.result()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 92)
    print(f"  統合シグナル  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
    print("=" * 92)

    all_sigs: list[tuple] = []
    for item in stop_items:
        if item["today_sig"]:
            score, rank = _stop.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "逆指値B"))
    for item in brk_items:
        if item["today_sig"]:
            score, rank = _brk.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "BRK"))
    for item in short_items:
        if item["today_sig"]:
            score, rank = _short.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "ショート"))
    all_sigs.sort(key=lambda x: x[1], reverse=True)

    print(f"\n【シグナル ({date_label})】 {len(all_sigs)}件")
    if all_sigs:
        print(f"  {'銘柄':<12} {'名前':<24} {'戦略':<6} {'種別':<8} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値':>8} {'指値上限':>8} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 134)
        for item, score, rank, kind in all_sigs:
            sig = item["today_sig"]
            limit_entry = sig.get("limit_entry_price", sig["order_price"])
            src = "★WF" if any(item["symbol"] == s and item["strategy"] == st
                               for s, _, st in (_WF_STOP + _WF_BRK)) else "既存"
            print(f"  {item['symbol']:<12} {item['name']:<24} {item['strategy']:<6} {kind:<8}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>8,.0f}"
                  f" {limit_entry:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点 [{src}]")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    print(f"\nHTMLレポート生成中...", flush=True)
    stop_html  = _stop.build_html(stop_items,   args.days, date_label)
    brk_html   = _brk.build_html(brk_items,     args.days, date_label)
    short_html = _short.build_html(short_items,  args.days, date_label)

    mode_suffix = f"_{_stop.TRADING_MODE}" if _stop.TRADING_MODE != "conservative" else ""
    date_suffix = args.date if args.date else today
    out_path    = Path(f"signals_merged{mode_suffix}_{date_suffix}.html")
    out_path.write_text(
        _build_merged_html(stop_html, brk_html, short_html, _WF_STOP, _WF_BRK),
        encoding="utf-8"
    )
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
