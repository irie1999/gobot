"""
run_signals_prime.py  ―  プライム全銘柄 Walk-forward 選定版
======================================================================
2倍レバ・4銘柄同時保有 (1ポジション105万円・上限10,500円/株) 条件で
プライム全銘柄 (~1551銘柄) を Walk-forward スキャンした結果 45 銘柄を使用。
除外基準: 取引回数が年2〜4回以下の低頻度銘柄・損益マイナス銘柄は手動除外。
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
# sm=1.5/tm=2.0, Sharpe>=1.5, --per-strategy 8, プライム全銘柄(~1495) スキャン結果
STOP_WATCHLIST: list[tuple[str, str, str]] = [
    # ── MACD (逆指値B) ──
    ("9110.T", "ＮＳユナイテッド海運",           "MACD"),  # 8,140円 folds=2 PnL=+287,059 PF=7.25 WR=84%
    ("7013.T", "ＩＨＩ",                         "MACD"),  # 2,814円 folds=2 PnL=+195,987 PF=10.0 WR=93%
    ("7721.T", "東京計器",                       "MACD"),  # 7,330円 folds=2 PnL=+403,925 PF=3.51 WR=69%
    ("6101.T", "ツガミ",                         "MACD"),  # 6,020円 folds=2 PnL=+282,186 PF=5.76 WR=81%
    ("6976.T", "太陽誘電",                       "MACD"),  # 6,739円 folds=3 PnL=+283,890 PF=3.59 WR=77%
    ("1964.T", "中外炉工業",                     "MACD"),  # 4,655円 folds=3 PnL=+173,016 PF=8.31 WR=95%
    ("1515.T", "日鉄鉱業",                       "MACD"),  # 2,605円 folds=2 PnL=+192,984 PF=7.23 WR=87%
    ("9024.T", "西武ホールディングス",           "MACD"),  # 3,791円 folds=3 PnL=+200,799 PF=5.91 WR=87%
    # ── A7 (逆指値B) ──
    ("7003.T", "三井Ｅ＆Ｓ",                     "A7"),    # 5,526円 folds=2 PnL=+376,868 PF=7.57 WR=92%
    ("6508.T", "明電舎",                         "A7"),    # 9,450円 folds=2 PnL=+308,840 PF=5.87 WR=86%
    ("6101.T", "ツガミ",                         "A7"),    # 6,020円 folds=2 PnL=+222,465 PF=7.77 WR=91%
    ("6268.T", "ナブテスコ",                     "A7"),    # 5,757円 folds=2 PnL=+171,133 PF=7.22 WR=89%
    ("8061.T", "西華産業",                       "A7"),    # 3,065円 folds=2 PnL=+122,299 PF=10.0 WR=93%
    ("8227.T", "しまむら",                       "A7"),    # 3,144円 folds=2 PnL=+193,639 PF=5.43 WR=85%
    ("4506.T", "住友ファーマ",                   "A7"),    # 1,620円 folds=2 PnL=+106,618 PF=7.12 WR=83%
    ("1814.T", "大末建設",                       "A7"),    # 3,350円 folds=2 PnL=+111,705 PF=8.54 WR=84%
    # ── RSI2 (逆指値B) ──
    ("7003.T", "三井Ｅ＆Ｓ",                     "RSI2"),  # 5,526円 folds=2 PnL=+332,314 PF=7.86 WR=92%
    ("1942.T", "関電工",                         "RSI2"),  # 7,366円 folds=2 PnL=+117,564 PF=10.0 WR=100%
    ("5631.T", "日本製鋼所",                     "RSI2"),  # 8,895円 folds=2 PnL=+332,789 PF=3.42 WR=66%
    ("9869.T", "加藤産業",                       "RSI2"),  # 5,940円 folds=2 PnL=+148,344 PF=4.94 WR=82%
    ("9068.T", "丸全昭和運輸",                   "RSI2"),  # 7,780円 folds=3 PnL=+139,735 PF=4.90 WR=81%
    ("4631.T", "ＤＩＣ",                         "RSI2"),  # 3,696円 folds=2 PnL=+73,647  PF=7.68 WR=89%
]

BRK_WATCHLIST: list[tuple[str, str, str]] = [
    # ── DON (ブレイクアウト) ──
    ("8015.T", "豊田通商",                       "DON"),   # 7,096円 folds=2 PnL=+278,393 PF=5.27 WR=80%
    ("9065.T", "山九",                           "DON"),   # 8,570円 folds=2 PnL=+259,281 PF=5.38 WR=87%
    ("1515.T", "日鉄鉱業",                       "DON"),   # 2,605円 folds=2 PnL=+239,726 PF=7.95 WR=89%
    ("6525.T", "ＫＯＫＵＳＡＩ　ＥＬＥＣＴＲＩＣ",  "DON"),   # 6,989円 folds=2 PnL=+277,707 PF=2.67 WR=71%
    ("8360.T", "山梨中央銀行",                   "DON"),   # 5,460円 folds=2 PnL=+218,776 PF=3.30 WR=78%
    ("1815.T", "鉄建建設",                       "DON"),   # 5,060円 folds=2 PnL=+165,620 PF=3.41 WR=78%
    ("3106.T", "倉敷紡績",                       "DON"),   # 9,790円 folds=2 PnL=+204,258 PF=2.15 WR=71%
    ("1871.T", "ピーエス・コンストラクション",   "DON"),   # 2,730円 folds=2 PnL=+115,079 PF=7.97 WR=95%
    # ── VOL (ブレイクアウト) ──
    ("7013.T", "ＩＨＩ",                         "VOL"),   # 2,814円 folds=3 PnL=+181,237 PF=10.0 WR=92%
    ("7721.T", "東京計器",                       "VOL"),   # 7,330円 folds=3 PnL=+275,663 PF=2.85 WR=76%
    ("1515.T", "日鉄鉱業",                       "VOL"),   # 2,605円 folds=2 PnL=+155,042 PF=5.50 WR=87%
    ("6268.T", "ナブテスコ",                     "VOL"),   # 5,757円 folds=2 PnL=+138,372 PF=6.70 WR=81%
    ("1964.T", "中外炉工業",                     "VOL"),   # 4,655円 folds=2 PnL=+139,084 PF=7.23 WR=87%
    ("1663.T", "Ｋ＆Ｏエナジーグループ",         "VOL"),   # 4,620円 folds=2 PnL=+138,226 PF=5.10 WR=85%
    ("6323.T", "ローツェ",                       "VOL"),   # 3,998円 folds=2 PnL=+121,260 PF=7.20 WR=91%
    # ── MOM (ブレイクアウト) ──
    ("1515.T", "日鉄鉱業",                       "MOM"),   # 2,605円 folds=2 PnL=+286,110 PF=5.18 WR=83%
    ("6101.T", "ツガミ",                         "MOM"),   # 6,020円 folds=2 PnL=+269,572 PF=5.30 WR=79%
    ("7013.T", "ＩＨＩ",                         "MOM"),   # 2,814円 folds=3 PnL=+203,023 PF=5.99 WR=86%
    ("7242.T", "カヤバ",                         "MOM"),   # 4,650円 folds=2 PnL=+133,634 PF=7.11 WR=85%
    ("8360.T", "山梨中央銀行",                   "MOM"),   # 5,460円 folds=2 PnL=+183,280 PF=5.37 WR=81%
    ("8343.T", "秋田銀行",                       "MOM"),   # 5,540円 folds=2 PnL=+159,299 PF=3.61 WR=75%
    ("8016.T", "オンワードホールディングス",     "MOM"),   #   732円 folds=2 PnL=+22,906  PF=10.0 WR=100%
    ("6481.T", "ＴＨＫ",                         "MOM"),   # 7,216円 folds=2 PnL=+145,005 PF=2.53 WR=72%
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
