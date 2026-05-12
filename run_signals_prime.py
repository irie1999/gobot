"""
run_signals_prime.py  ―  プライム全銘柄 Walk-forward 選定版
======================================================================
2倍レバ・4銘柄同時保有 (1ポジション105万円・上限10,500円/株) 条件で
プライム全銘柄 (~1551銘柄) を Walk-forward スキャンした結果 48 銘柄を使用。
パラメータ: sm=1.5 / tm=2.0 (損切-4.5% / 目標+6% / 高回転)
フィルター: Sharpe >= 1.5 / 戦略あたり8銘柄

既存の check_signals_*.py / run_signals.py / run_signals_aggressive.py
は一切変更しない。

【使い方】
  python run_signals_prime.py                   # 全期間(365日) HTMLレポート
  python run_signals_prime.py --days 90         # 直近90日
  python run_signals_prime.py --date 2026-05-08 # 任意日シグナル確認
  python run_signals_prime.py --signal-only     # シグナルのみ表示
  python run_signals_prime.py --no-browser      # HTML生成のみ
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["TRADING_MODE"] = "aggressive"

import check_signals_stop     as _stop
import check_signals_breakout as _brk
import check_signals_short    as _short
from run_signals import _extract_style, _extract_body

# ── パラメータ上書き (sm=1.5 / tm=2.0 / 高回転) ──────────────────────────────
for _k, _v in list(_stop.STRATEGY_PARAMS.items()):
    _stop.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 1.5, 2.0)
for _k, _v in list(_brk.STRATEGY_PARAMS.items()):
    _brk.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 1.5, 2.0)

_OPT_LABEL = "sm=1.5 / tm=2.0 (損切-4.5% / 目標+6% / 高回転)"

JST = timezone(timedelta(hours=9))

# ── Walk-forward 選定 WATCHLIST (2026-05-12, budget=105万・上限10,500円/株) ────
# 2倍レバ・4銘柄同時保有 (1ポジション105万円) 条件。全銘柄が予算内。
STOP_WATCHLIST: list[tuple[str, str, str]] = [
    # ── MACD (逆指値B) ──
    ("1762.T", "高松コンストラクショングループ", "MACD"),  # 3,625円 folds=2 PnL=+119,648 PF=10.0 WR=100%
    ("6976.T", "太陽誘電",                       "MACD"),  # 6,739円 folds=3 PnL=+264,392 PF=5.69 WR=83%
    ("6284.T", "日精エー・エス・ビー機械",       "MACD"),  # 8,240円 folds=3 PnL=+175,165 PF=5.19 WR=83%
    ("1515.T", "日鉄鉱業",                       "MACD"),  # 2,605円 folds=3 PnL=+149,035 PF=7.21 WR=87%
    ("4680.T", "ラウンドワン",                   "MACD"),  #   863円 folds=2 PnL=+36,869  PF=10.0 WR=100%
    ("6101.T", "ツガミ",                         "MACD"),  # 6,020円 folds=2 PnL=+194,354 PF=4.71 WR=75%
    ("6508.T", "明電舎",                         "MACD"),  # 9,450円 folds=2 PnL=+208,440 PF=3.40 WR=71%
    ("9024.T", "西武ホールディングス",           "MACD"),  # 3,791円 folds=3 PnL=+149,609 PF=7.50 WR=91%
    # ── A7 (逆指値B) ──
    ("6508.T", "明電舎",                         "A7"),    # 9,450円 folds=2 PnL=+278,865 PF=5.82 WR=86%
    ("9602.T", "東宝",                           "A7"),    # 1,388円 folds=2 PnL=+48,658  PF=6.67 WR=67%
    ("8061.T", "西華産業",                       "A7"),    # 3,065円 folds=2 PnL=+112,064 PF=10.0 WR=94%
    ("5105.T", "ＴＯＹＯ ＴＩＲＥ",             "A7"),    # 3,759円 folds=2 PnL=+89,036  PF=7.79 WR=93%
    ("3046.T", "ジンズホールディングス",         "A7"),    # 7,770円 folds=2 PnL=+150,260 PF=4.02 WR=56%
    ("6268.T", "ナブテスコ",                     "A7"),    # 5,757円 folds=2 PnL=+112,459 PF=6.47 WR=84%
    ("1964.T", "中外炉工業",                     "A7"),    # 4,655円 folds=2 PnL=+99,813  PF=7.23 WR=81%
    ("6101.T", "ツガミ",                         "A7"),    # 6,020円 folds=2 PnL=+128,700 PF=6.73 WR=86%
    # ── RSI2 (逆指値B) ──
    ("7003.T", "三井Ｅ＆Ｓ",                     "RSI2"),  # 5,526円 folds=2 PnL=+300,669 PF=7.96 WR=92%
    ("1942.T", "関電工",                         "RSI2"),  # 7,366円 folds=2 PnL=+106,337 PF=10.0 WR=100%
    ("4733.T", "オービックビジネスコンサルタント","RSI2"),  # 6,068円 folds=2 PnL=+179,071 PF=7.42 WR=72%
    ("9869.T", "加藤産業",                       "RSI2"),  # 5,940円 folds=2 PnL=+158,203 PF=7.66 WR=93%
    ("9697.T", "カプコン",                       "RSI2"),  # 3,415円 folds=2 PnL=+140,591 PF=4.37 WR=58%
    ("5631.T", "日本製鋼所",                     "RSI2"),  # 8,895円 folds=3 PnL=+264,333 PF=2.79 WR=64%
    ("9305.T", "ヤマタネ",                       "RSI2"),  # 2,024円 folds=2 PnL=+62,306  PF=6.67 WR=67%
    ("9068.T", "丸全昭和運輸",                   "RSI2"),  # 7,780円 folds=3 PnL=+125,427 PF=4.97 WR=81%
]

BRK_WATCHLIST: list[tuple[str, str, str]] = [
    # ── DON (ブレイクアウト) ──
    ("6508.T", "明電舎",                         "DON"),   # 9,450円 folds=2 PnL=+212,556 PF=4.57 WR=76%
    ("8377.T", "ほくほくフィナンシャルグループ", "DON"),   # 6,376円 folds=2 PnL=+192,072 PF=3.91 WR=81%
    ("6268.T", "ナブテスコ",                     "DON"),   # 5,757円 folds=2 PnL=+180,525 PF=4.38 WR=75%
    ("5393.T", "ニチアス",                       "DON"),   # 3,796円 folds=2 PnL=+99,582  PF=8.38 WR=93%
    ("1975.T", "朝日工業社",                     "DON"),   # 3,855円 folds=2 PnL=+128,270 PF=5.93 WR=86%
    ("9024.T", "西武ホールディングス",           "DON"),   # 3,791円 folds=2 PnL=+140,739 PF=3.56 WR=55%
    ("6474.T", "不二越",                         "DON"),   # 5,570円 folds=2 PnL=+133,140 PF=3.28 WR=80%
    ("9984.T", "ソフトバンクグループ",           "DON"),   # 5,932円 folds=3 PnL=+176,910 PF=4.62 WR=80%
    # ── VOL (ブレイクアウト) ──
    ("6268.T", "ナブテスコ",                     "VOL"),   # 5,757円 folds=2 PnL=+174,873 PF=10.0 WR=100%
    ("5838.T", "楽天銀行",                       "VOL"),   # 6,247円 folds=3 PnL=+229,167 PF=5.72 WR=80%
    ("8050.T", "セイコーグループ",               "VOL"),   # 6,280円 folds=2 PnL=+158,663 PF=7.30 WR=87%
    ("6254.T", "野村マイクロ・サイエンス",       "VOL"),   # 4,450円 folds=2 PnL=+137,282 PF=3.57 WR=73%
    ("6532.T", "ベイカレント",                   "VOL"),   # 5,082円 folds=2 PnL=+150,399 PF=3.76 WR=53%
    ("8059.T", "第一実業",                       "VOL"),   # 3,185円 folds=2 PnL=+72,156  PF=7.47 WR=92%
    ("7721.T", "東京計器",                       "VOL"),   # 7,330円 folds=3 PnL=+161,775 PF=2.48 WR=71%
    ("1515.T", "日鉄鉱業",                       "VOL"),   # 2,605円 folds=3 PnL=+105,525 PF=5.76 WR=86%
    # ── MOM (ブレイクアウト) ──
    ("1515.T", "日鉄鉱業",                       "MOM"),   # 2,605円 folds=2 PnL=+262,189 PF=8.60 WR=92%
    ("7003.T", "三井Ｅ＆Ｓ",                     "MOM"),   # 5,526円 folds=2 PnL=+326,732 PF=2.52 WR=70%
    ("8360.T", "山梨中央銀行",                   "MOM"),   # 5,460円 folds=2 PnL=+173,468 PF=3.79 WR=76%
    ("7013.T", "ＩＨＩ",                         "MOM"),   # 2,814円 folds=3 PnL=+137,648 PF=3.54 WR=74%
    ("1975.T", "朝日工業社",                     "MOM"),   # 3,855円 folds=3 PnL=+114,345 PF=4.07 WR=78%
    ("6752.T", "パナソニック ホールディングス",  "MOM"),   # 3,440円 folds=3 PnL=+104,023 PF=3.28 WR=75%
    ("1812.T", "鹿島建設",                       "MOM"),   # 6,237円 folds=2 PnL=+112,756 PF=2.77 WR=76%
    ("8037.T", "カメイ",                         "MOM"),   # 3,385円 folds=2 PnL=+67,361  PF=8.12 WR=85%
]

# WFバッジ用シンボルセット (48銘柄すべて)
_WF_SYMS = sorted({s for s, _, _ in (STOP_WATCHLIST + BRK_WATCHLIST)})


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

    wf_syms_js = "[" + ",".join(f'"{s}"' for s in _WF_SYMS) + "]"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Prime WF シグナルレポート — {today_str}</title>
<style>
{stop_css}
{extra_css}
.tab-nav{{display:flex;gap:0;background:#090b14;padding:10px 16px 0;border-bottom:2px solid #252840;position:sticky;top:0;z-index:200;flex-wrap:wrap}}
.tab-btn{{background:#16192a;color:#94a3b8;border:1px solid #252840;border-bottom:none;padding:9px 22px;margin-right:4px;border-radius:6px 6px 0 0;cursor:pointer;font-size:13px;font-family:inherit;transition:background .15s,color .15s}}
.tab-btn:hover{{background:#1e2235;color:#dde1ec}}
.tab-btn.active{{background:#0f1117;color:#34d399;border-color:#34d399;border-bottom-color:#0f1117}}
.tab-pane{{min-height:60vh}}
.mode-banner{{background:#064e3b;color:#a7f3d0;padding:6px 16px;font-size:12px;font-weight:700;letter-spacing:.5px}}
.wf-badge{{display:inline-block;background:#34d399;color:#000;font-size:10px;font-weight:700;
           padding:1px 5px;border-radius:3px;margin-left:5px;vertical-align:middle;letter-spacing:.5px}}
.wf-row td:first-child{{border-left:3px solid #34d399 !important}}
</style>
</head>
<body>
<div class="mode-banner">🌿 PRIME WF — {_OPT_LABEL} / 2倍レバ・4銘柄同時保有 (上限10,500円/株) Walk-forward 選定 2026-05-12</div>
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

function markRows() {{
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
          first.appendChild(badge);
        }}
        break;
      }}
    }}
  }});
}}

document.addEventListener('DOMContentLoaded', markRows);
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="プライム全銘柄 WF シグナルレポート")
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
    print(f"Prime WF シグナル 開始  パラメータ: {_OPT_LABEL}")
    print(f"  逆指値B : {len(STOP_WATCHLIST)}銘柄")
    print(f"  BRK    : {len(BRK_WATCHLIST)}銘柄")
    print(f"  上限10,500円/株 (2倍レバ・4銘柄同時 105万円/ポジション)", flush=True)

    with ThreadPoolExecutor(max_workers=3) as outer:
        fut_stop  = outer.submit(_run_group_with_list, _stop,  STOP_WATCHLIST,    sig_date, args.workers)
        fut_brk   = outer.submit(_run_group_with_list, _brk,   BRK_WATCHLIST,     sig_date, args.workers)
        fut_short = outer.submit(_run_group_with_list, _short, _short.WATCHLIST,  sig_date, args.workers)
    stop_items  = fut_stop.result()
    brk_items   = fut_brk.result()
    short_items = fut_short.result()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 92)
    print(f"  Prime WF シグナル  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
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
    out_path    = Path(f"signals_prime_{date_suffix}.html")
    out_path.write_text(_build_html(stop_html, brk_html, short_html), encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
