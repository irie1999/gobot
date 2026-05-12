"""
run_signals_aggressive.py  ―  Aggressive モード専用 シグナルレポート
======================================================================
Walk-forward 選定銘柄 (2026-05-12, budget=50万, aggressive) を使用。
常に aggressive モード (tm=1.5, 目標+4.5%) で動作する。

既存の check_signals_*.py / run_signals.py は一切変更しない。

【使い方】
  python run_signals_aggressive.py                   # 全期間(365日) HTMLレポート
  python run_signals_aggressive.py --days 90         # 直近90日
  python run_signals_aggressive.py --date 2026-05-08 # 任意日シグナル確認
  python run_signals_aggressive.py --signal-only     # シグナルのみ表示
  python run_signals_aggressive.py --no-browser      # HTML生成のみ
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 必ず aggressive モードで動作
os.environ["TRADING_MODE"] = "aggressive"

import check_signals_stop     as _stop
import check_signals_breakout as _brk
import check_signals_short    as _short
from run_signals import _extract_style, _extract_body

# ── パラメータ上書き (optimize_params.py 最適化結果: sm=2.0 / tm=3.0) ────────
# aggressive のデフォルト (sm=1.0/tm=1.5) は損切が早く損失過多。
# 全6戦略でグリッドサーチした結果 sm=2.0/tm=3.0 が最良 PnL / EV を示した。
# check_signals_*.py は変更せず、ここだけ monkey-patch。
for _k, _v in list(_stop.STRATEGY_PARAMS.items()):
    _stop.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 2.0, 3.0)
for _k, _v in list(_brk.STRATEGY_PARAMS.items()):
    _brk.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 2.0, 3.0)

_OPT_LABEL = "sm=2.0 / tm=3.0 (optimize_params 最適値)"

JST = timezone(timedelta(hours=9))

# ── Walk-forward 選定 WATCHLIST (2026-05-12, aggressive, budget=50万) ────────
STOP_WATCHLIST: list[tuple[str, str, str]] = [
    # ── MACD (逆指値B) ──
    ("1762.T", "高松コンストラクショングループ", "MACD"),
    ("1515.T", "日鉄鉱業",                    "MACD"),
    ("4680.T", "ラウンドワン",                 "MACD"),
    ("9024.T", "西武ホールディングス",          "MACD"),
    ("6141.T", "DMG森精機",                   "MACD"),
    ("5830.T", "いよぎんホールディングス",      "MACD"),
    ("5832.T", "ちゅうぎんフィナンシャルG",    "MACD"),
    ("3778.T", "さくらインターネット",          "MACD"),
    ("7013.T", "IHI",                         "MACD"),
    ("8020.T", "兼松",                         "MACD"),
    # ── A7 (逆指値B) ──
    ("9602.T", "東宝",                         "A7"),
    ("8061.T", "西華産業",                     "A7"),
    ("5105.T", "TOYO TIRE",                   "A7"),
    ("1964.T", "中外炉工業",                   "A7"),
    ("8227.T", "しまむら",                     "A7"),
    ("3186.T", "ネクステージ",                 "A7"),
    ("9412.T", "スカパーJSAT HD",              "A7"),
    ("8129.T", "東邦ホールディングス",          "A7"),
    ("6209.T", "リケンNPR",                   "A7"),
    ("4819.T", "デジタルガレージ",             "A7"),
    # ── RSI2 (逆指値B) ──
    ("9697.T", "カプコン",                     "RSI2"),
    ("9305.T", "ヤマタネ",                     "RSI2"),
    ("4931.T", "新日本製薬",                   "RSI2"),
    ("3104.T", "富士紡ホールディングス",        "RSI2"),
    ("4631.T", "DIC",                         "RSI2"),
    ("8337.T", "千葉興業銀行",                 "RSI2"),
    ("9324.T", "安田倉庫",                     "RSI2"),
    ("5541.T", "大平洋金属",                   "RSI2"),
    ("5262.T", "日本ヒューム",                 "RSI2"),
    ("8133.T", "伊藤忠エネクス",               "RSI2"),
]

BRK_WATCHLIST: list[tuple[str, str, str]] = [
    # ── DON (ブレイクアウト) ──
    ("5393.T", "ニチアス",                     "DON"),
    ("1975.T", "朝日工業社",                   "DON"),
    ("9024.T", "西武ホールディングス",          "DON"),
    ("5535.T", "ミガロホールディングス",        "DON"),
    ("3994.T", "マネーフォワード",              "DON"),
    ("6254.T", "野村マイクロ・サイエンス",      "DON"),
    ("6332.T", "月島ホールディングス",          "DON"),
    ("7717.T", "ブイ・テクノロジー",            "DON"),
    ("7972.T", "イトーキ",                     "DON"),
    ("6997.T", "日本ケミコン",                 "DON"),
    # ── VOL (ブレイクアウト) ──
    ("6254.T", "野村マイクロ・サイエンス",      "VOL"),
    ("8059.T", "第一実業",                     "VOL"),
    ("1515.T", "日鉄鉱業",                    "VOL"),
    ("6264.T", "マルマエ",                     "VOL"),
    ("4047.T", "関東電化工業",                 "VOL"),
    ("4676.T", "フジ・メディア・HD",            "VOL"),
    ("5074.T", "テスホールディングス",          "VOL"),
    ("6315.T", "TOWA",                        "VOL"),
    ("2060.T", "フィード・ワン",               "VOL"),
    ("6564.T", "ミダックホールディングス",      "VOL"),
    # ── MOM (ブレイクアウト) ──
    ("1515.T", "日鉄鉱業",                    "MOM"),
    ("7013.T", "IHI",                         "MOM"),
    ("1975.T", "朝日工業社",                   "MOM"),
    ("6752.T", "パナソニックHD",               "MOM"),
    ("8037.T", "カメイ",                       "MOM"),
    ("9412.T", "スカパーJSAT HD",              "MOM"),
    ("8059.T", "第一実業",                     "MOM"),
    ("1938.T", "日本リーテック",               "MOM"),
    ("8386.T", "百十四銀行",                   "MOM"),
    ("9697.T", "カプコン",                     "MOM"),
]


def _merged(wf: list, existing: list) -> list:
    """WF銘柄を先頭に、既存銘柄を重複なしで追加する。"""
    seen = {(s, st) for s, _, st in wf}
    result = list(wf)
    for entry in existing:
        key = (entry[0], entry[2])
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


# 既存WATCHLISTと合算（WF銘柄が先頭、既存は後ろに追加）
STOP_WATCHLIST = _merged(STOP_WATCHLIST, _stop.WATCHLIST)
BRK_WATCHLIST  = _merged(BRK_WATCHLIST,  _brk.WATCHLIST)


def _run_group_with_list(mod, watchlist, sig_date, workers: int) -> list[dict]:
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


def _build_html(stop_html: str, brk_html: str, srt_html: str = "") -> str:
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
    srt_tab_btn  = '<button class="tab-btn" onclick="switchTab(2)">ショート逆指値（A7_S）</button>' if srt_html else ""
    srt_tab_pane = f'<div id="tc2" class="tab-pane" style="display:none">\n{srt_body}\n</div>' if srt_html else ""

    # WFシンボルセット (JS埋め込み用)
    wf_syms = sorted({s for s, _, _ in (STOP_WATCHLIST[:30] + BRK_WATCHLIST[:30])})
    wf_syms_js = "[" + ",".join(f'"{s}"' for s in wf_syms) + "]"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Aggressive シグナルレポート — {today_str}</title>
<style>
{stop_css}
{extra_css}
.tab-nav{{display:flex;gap:0;background:#090b14;padding:10px 16px 0;border-bottom:2px solid #252840;position:sticky;top:0;z-index:200;flex-wrap:wrap}}
.tab-btn{{background:#16192a;color:#94a3b8;border:1px solid #252840;border-bottom:none;padding:9px 22px;margin-right:4px;border-radius:6px 6px 0 0;cursor:pointer;font-size:13px;font-family:inherit;transition:background .15s,color .15s}}
.tab-btn:hover{{background:#1e2235;color:#dde1ec}}
.tab-btn.active{{background:#0f1117;color:#f59e0b;border-color:#f59e0b;border-bottom-color:#0f1117}}
.tab-pane{{min-height:60vh}}
.mode-banner{{background:#78350f;color:#fde68a;padding:6px 16px;font-size:12px;font-weight:700;letter-spacing:.5px}}
.wf-badge{{display:inline-block;background:#f59e0b;color:#000;font-size:10px;font-weight:700;
           padding:1px 5px;border-radius:3px;margin-left:5px;vertical-align:middle;letter-spacing:.5px}}
.wf-row td:first-child{{border-left:3px solid #f59e0b !important}}
.legend-bar{{background:#0f1117;border:1px solid #252840;border-radius:6px;
             padding:8px 16px;margin:10px 0 4px;font-size:12px;color:#94a3b8;display:flex;gap:20px;align-items:center}}
</style>
</head>
<body>
<div class="mode-banner">⚡ AGGRESSIVE MODE — {_OPT_LABEL} / Walk-forward 選定 2026-05-12</div>
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab(0)">逆指値B（MACD / A7 / RSI2）</button>
  <button class="tab-btn"        onclick="switchTab(1)">ブレイクアウト（DON / VOL / MOM）</button>
  {srt_tab_btn}
</div>
<div id="tc0" class="tab-pane">{stop_body}</div>
<div id="tc1" class="tab-pane" style="display:none">{brk_body}</div>
{srt_tab_pane}
<script>
var WF_SYMS = {wf_syms_js};

function switchTab(n){{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{b.classList.toggle('active',i===n);}});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{t.style.display=i===n?'block':'none';}});
}}

function markWFRows() {{
  document.querySelectorAll('table tr').forEach(function(tr) {{
    var first = tr.cells[0];
    if (!first) return;
    var text = first.innerText || first.textContent || '';
    for (var i = 0; i < WF_SYMS.length; i++) {{
      if (text.indexOf(WF_SYMS[i]) !== -1) {{
        tr.classList.add('wf-row');
        if (!first.querySelector('.wf-badge')) {{
          var badge = document.createElement('span');
          badge.className = 'wf-badge';
          badge.textContent = 'WF';
          badge.title = 'Walk-forward 選定銘柄 (aggressive 2026-05-12)';
          first.appendChild(badge);
        }}
        break;
      }}
    }}
  }});

  document.querySelectorAll('.tab-pane').forEach(function(pane) {{
    if (pane.querySelector('.legend-bar')) return;
    var leg = document.createElement('div');
    leg.className = 'legend-bar';
    leg.innerHTML = '<span><span class="wf-badge">WF</span> Walk-forward 選定銘柄（新規・aggressive用）</span>'
                  + '<span style="color:#555">｜</span>'
                  + '<span>バッジなし = 既存銘柄</span>';
    pane.insertBefore(leg, pane.firstChild);
  }});
}}

document.addEventListener('DOMContentLoaded', markWFRows);
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggressive モード専用シグナルレポート")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None)
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int, default=_stop._DEFAULT_WORKERS)
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
    n_srt = len(_short.WATCHLIST)
    print(f"Aggressive シグナル 開始  モード: {_stop.TRADING_MODE}")
    print(f"  逆指値B : {len(STOP_WATCHLIST)}銘柄 (WF 2026-05-12)")
    print(f"  BRK    : {len(BRK_WATCHLIST)}銘柄 (WF 2026-05-12)")
    print(f"  ショート: {n_srt}銘柄", flush=True)

    with ThreadPoolExecutor(max_workers=3) as outer:
        fut_stop  = outer.submit(_run_group_with_list, _stop,  STOP_WATCHLIST, sig_date, args.workers)
        fut_brk   = outer.submit(_run_group_with_list, _brk,   BRK_WATCHLIST,  sig_date, args.workers)
        fut_short = outer.submit(_run_group_with_list, _short, _short.WATCHLIST, sig_date, args.workers)
    stop_items  = fut_stop.result()
    brk_items   = fut_brk.result()
    short_items = fut_short.result()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 92)
    print(f"  Aggressive シグナル  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
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
        print(f"  {'銘柄':<12} {'名前':<28} {'戦略':<6} {'種別':<8} {'シグナル日':<12} "
              f"{'信号株価':>8} {'現在値':>8} {'逆指値':>8} {'損切り':>8} {'目標':>8} スコア")
        print("  " + "-" * 130)
        for item, score, rank, kind in all_sigs:
            sig = item["today_sig"]
            print(f"  {item['symbol']:<12} {item['name']:<28} {item['strategy']:<6} {kind:<8}"
                  f" {sig['signal_date']:<12} {sig['signal_price']:>8,.0f}"
                  f" {sig['current_price']:>8,.0f} {sig['order_price']:>8,.0f}"
                  f" {sig['stop_price']:>8,.0f} {sig['target_price']:>8,.0f}"
                  f"  {rank}{score}点")
    else:
        print("  (なし)")

    if args.signal_only:
        return

    print(f"\nHTMLレポート生成中...", flush=True)
    stop_html  = _stop.build_html(stop_items,   args.days, date_label)
    brk_html   = _brk.build_html(brk_items,     args.days, date_label)
    short_html = _short.build_html(short_items,  args.days, date_label)

    date_suffix = args.date if args.date else today
    out_path    = Path(f"signals_aggressive_{date_suffix}.html")
    out_path.write_text(_build_html(stop_html, brk_html, short_html), encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
