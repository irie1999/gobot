"""
run_signals_nolimit.py  ―  プライム全銘柄 Walk-forward 選定版（株価制限なし）
======================================================================
プライム全銘柄 (~1800銘柄) を株価制限なしで Walk-forward スキャンした結果
戦略あたり最大15候補を使用。バックテスト後に365日PnL≤0の銘柄を自動除外。
パラメータ: sm=1.5 / tm=2.0 (損切-4.5% / 目標+6% / 高回転)
フィルター: min-trades 10 / max-avg-hold 7.0 / Sharpe >= 0.0

【使い方】
  python run_signals_nolimit.py                   # 全期間(365日) HTMLレポート
  python run_signals_nolimit.py --days 90         # 直近90日
  python run_signals_nolimit.py --date 2026-05-08 # 任意日シグナル確認
  python run_signals_nolimit.py --signal-only     # シグナルのみ表示
  python run_signals_nolimit.py --no-browser      # HTML生成のみ
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
import check_signals_short_breakout as _sbrk
from run_signals import _extract_style, _extract_body, get_regime_html, _auto_update_regime_cache
from _signal_funds import collect_fund_rows, fund_html as _fund_html, print_fund_summary, filter_items

# ── パラメータ上書き (sm=1.5 / tm=2.0 / 高回転) ──────────────────────────────
for _k, _v in list(_stop.STRATEGY_PARAMS.items()):
    _stop.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 1.5, 2.0)
for _k, _v in list(_brk.STRATEGY_PARAMS.items()):
    _brk.STRATEGY_PARAMS[_k] = (_v[0], _v[1], 1.5, 2.0)

_OPT_LABEL = "sm=1.5 / tm=2.0 (損切-4.5% / 目標+6% / 高回転)"

JST = timezone(timedelta(hours=9))

# ── Walk-forward 候補 CANDIDATES (2026-05-13, 株価制限なし) ─────────────────────
# build_watchlist.py --min-trades 10 --max-avg-hold 7.0 --per-strategy 15
# バックテスト後に365日PnL≤0の銘柄を自動除外するため、多めに候補を用意
STOP_CANDIDATES: list[tuple[str, str, str]] = [
    # ── MACD (逆指値B) ── 73銘柄通過 上位15
    ("6387.T", "サムコ",                         "MACD"),  # folds=2 trades=18 PnL=+835,301   PF=4.94 WR=75.2%
    ("6278.T", "ユニオンツール",                 "MACD"),  # folds=2 trades=14 PnL=+719,254   PF=4.79 WR=71.9%
    ("4062.T", "イビデン",                       "MACD"),  # folds=2 trades=14 PnL=+622,837   PF=5.89 WR=80.0%
    ("6101.T", "ツガミ",                         "MACD"),  # folds=2 trades=16 PnL=+343,916   PF=8.81 WR=95.2%
    ("7236.T", "ティラド",                       "MACD"),  # folds=2 trades=16 PnL=+409,965   PF=3.94 WR=75.0%
    ("6525.T", "ＫＯＫＵＳＡＩ　ＥＬＥＣＴＲＩＣ",  "MACD"),  # folds=2 trades=14 PnL=+371,792   PF=3.36 WR=71.7%
    ("6875.T", "メガチップス",                   "MACD"),  # folds=2 trades=13 PnL=+301,425   PF=5.55 WR=75.6%
    ("7013.T", "ＩＨＩ",                         "MACD"),  # folds=2 trades=11 PnL=+205,306   PF=7.26 WR=83.3%
    ("6941.T", "山一電機",                       "MACD"),  # folds=2 trades=14 PnL=+372,115   PF=2.99 WR=71.0%
    ("3741.T", "セック",                         "MACD"),  # folds=2 trades=15 PnL=+222,375   PF=7.58 WR=82.2%
    ("5334.T", "日本特殊陶業",                   "MACD"),  # folds=2 trades=12 PnL=+225,224   PF=4.66 WR=72.2%
    ("8050.T", "セイコーグループ",               "MACD"),  # folds=2 trades=10 PnL=+197,198   PF=7.17 WR=72.2%
    ("6284.T", "日精エー・エス・ビー機械",         "MACD"),  # folds=3 trades=10 PnL=+206,364   PF=3.79 WR=69.4%
    ("6976.T", "太陽誘電",                       "MACD"),  # folds=3 trades=16 PnL=+249,813   PF=3.32 WR=70.0%
    ("1812.T", "鹿島建設",                       "MACD"),  # folds=2 trades=14 PnL=+193,037   PF=4.84 WR=72.4%
    # ── A7 (逆指値B) ── 49銘柄通過 上位15
    ("6101.T", "ツガミ",                         "A7"),    # folds=2 trades=14 PnL=+312,914   PF=8.16 WR=88.9%
    ("7003.T", "三井Ｅ＆Ｓ",                     "A7"),    # folds=2 trades=13 PnL=+385,295   PF=5.33 WR=80.6%
    ("2692.T", "伊藤忠食品",                     "A7"),    # folds=2 trades=16 PnL=+412,858   PF=4.76 WR=62.2%
    ("6103.T", "オークマ",                       "A7"),    # folds=2 trades=11 PnL=+209,298   PF=7.58 WR=93.3%
    ("7173.T", "東京きらぼしフィナンシャルグループ","A7"),  # folds=2 trades=17 PnL=+367,438   PF=2.84 WR=65.8%
    ("8361.T", "大垣共立銀行",                   "A7"),    # folds=2 trades=12 PnL=+215,440   PF=7.30 WR=78.3%
    ("8360.T", "山梨中央銀行",                   "A7"),    # folds=2 trades=14 PnL=+242,540   PF=3.85 WR=78.3%
    ("8061.T", "西華産業",                       "A7"),    # folds=2 trades=12 PnL=+157,704   PF=7.12 WR=76.7%
    ("1814.T", "大末建設",                       "A7"),    # folds=2 trades=13 PnL=+133,978   PF=8.62 WR=83.3%
    ("2737.T", "トーメンデバイス",               "A7"),    # folds=2 trades=11 PnL=+260,994   PF=2.61 WR=72.2%
    ("5803.T", "フジクラ",                       "A7"),    # folds=3 trades=13 PnL=+167,919   PF=5.27 WR=73.3%
    ("4216.T", "旭有機材",                       "A7"),    # folds=2 trades=12 PnL=+140,849   PF=4.86 WR=82.2%
    ("4091.T", "日本酸素ホールディングス",         "A7"),    # folds=2 trades=10 PnL=+165,370   PF=3.63 WR=69.4%
    ("5831.T", "しずおかフィナンシャルグループ",   "A7"),    # folds=2 trades=10 PnL=+79,904    PF=8.02 WR=93.3%
    ("2540.T", "養命酒製造",                     "A7"),    # folds=2 trades=12 PnL=+111,046   PF=5.41 WR=70.0%
    # ── RSI2 (逆指値B) ── 12銘柄通過 全12
    ("5801.T", "古河電気工業",                   "RSI2"),  # folds=2 trades=10 PnL=+1,161,433 PF=7.22 WR=88.9%
    ("9869.T", "加藤産業",                       "RSI2"),  # folds=2 trades=12 PnL=+272,813   PF=5.85 WR=82.2%
    ("8877.T", "エスリード",                     "RSI2"),  # folds=3 trades=12 PnL=+256,071   PF=7.12 WR=83.3%
    ("7003.T", "三井Ｅ＆Ｓ",                     "RSI2"),  # folds=2 trades=10 PnL=+269,037   PF=5.06 WR=69.4%
    ("7011.T", "三菱重工業",                     "RSI2"),  # folds=2 trades=12 PnL=+187,381   PF=5.32 WR=83.3%
    ("7012.T", "川崎重工業",                     "RSI2"),  # folds=3 trades=13 PnL=+162,918   PF=4.89 WR=78.3%
    ("8551.T", "北日本銀行",                     "RSI2"),  # folds=2 trades=10 PnL=+154,308   PF=4.81 WR=69.4%
    ("6644.T", "大崎電気工業",                   "RSI2"),  # folds=2 trades=11 PnL=+74,020    PF=7.59 WR=82.2%
    ("3496.T", "アズーム",                       "RSI2"),  # folds=2 trades=12 PnL=+120,365   PF=4.46 WR=63.9%
    ("7936.T", "アシックス",                     "RSI2"),  # folds=3 trades=12 PnL=+116,315   PF=3.25 WR=58.9%
    ("5262.T", "日本ヒューム",                   "RSI2"),  # folds=2 trades=13 PnL=+55,117    PF=1.90 WR=58.3%
    ("6745.T", "ホーチキ",                       "RSI2"),  # folds=2 trades=10 PnL=+20,278    PF=4.51 WR=80.6%
]

BRK_CANDIDATES: list[tuple[str, str, str]] = [
    # ── DON (ブレイクアウト) ── 43銘柄通過 上位15
    ("4062.T", "イビデン",                       "DON"),   # folds=2 trades=19 PnL=+721,256   PF=5.88 WR=75.6%
    ("5715.T", "古河機械金属",                   "DON"),   # folds=2 trades=13 PnL=+288,899   PF=7.79 WR=85.0%
    ("8360.T", "山梨中央銀行",                   "DON"),   # folds=3 trades=18 PnL=+304,402   PF=7.05 WR=77.5%
    ("5535.T", "ミガロホールディングス",           "DON"),   # folds=2 trades=11 PnL=+149,141   PF=4.21 WR=45.8%
    ("2579.T", "コカ・コーラ ボトラーズジャパン", "DON"),   # folds=2 trades=11 PnL=+114,148   PF=4.34 WR=72.2%
    ("2802.T", "味の素",                         "DON"),   # folds=3 trades=14 PnL=+148,598   PF=3.46 WR=71.1%
    ("1938.T", "日本リーテック",                 "DON"),   # folds=2 trades=14 PnL=+116,917   PF=5.43 WR=80.6%
    ("8386.T", "百十四銀行",                     "DON"),   # folds=3 trades=18 PnL=+95,880    PF=5.81 WR=88.6%
    ("1815.T", "鉄建建設",                       "DON"),   # folds=2 trades=17 PnL=+138,865   PF=3.03 WR=70.0%
    ("6284.T", "日精エー・エス・ビー機械",         "DON"),   # folds=2 trades=10 PnL=+144,459   PF=4.88 WR=75.0%
    ("6268.T", "ナブテスコ",                     "DON"),   # folds=2 trades=15 PnL=+133,022   PF=2.78 WR=66.7%
    ("1893.T", "五洋建設",                       "DON"),   # folds=2 trades=18 PnL=+92,132    PF=5.35 WR=76.3%
    ("4506.T", "住友ファーマ",                   "DON"),   # folds=2 trades=15 PnL=+108,758   PF=2.63 WR=67.2%
    ("1871.T", "ピーエス・コンストラクション",     "DON"),   # folds=2 trades=13 PnL=+87,334    PF=5.25 WR=75.6%
    ("5482.T", "愛知製鋼",                       "DON"),   # folds=2 trades=14 PnL=+90,828    PF=4.55 WR=66.7%
    # ── VOL (ブレイクアウト) ── 13銘柄通過 全13
    ("5802.T", "住友電気工業",                   "VOL"),   # folds=3 trades=12 PnL=+543,686   PF=6.14 WR=80.6%
    ("4062.T", "イビデン",                       "VOL"),   # folds=3 trades=11 PnL=+520,446   PF=8.10 WR=88.9%
    ("1515.T", "日鉄鉱業",                       "VOL"),   # folds=2 trades=13 PnL=+185,014   PF=5.92 WR=83.3%
    ("6268.T", "ナブテスコ",                     "VOL"),   # folds=2 trades=10 PnL=+157,963   PF=6.36 WR=77.8%
    ("3741.T", "セック",                         "VOL"),   # folds=2 trades=12 PnL=+165,275   PF=5.52 WR=75.0%
    ("5715.T", "古河機械金属",                   "VOL"),   # folds=2 trades=10 PnL=+154,774   PF=7.35 WR=91.7%
    ("3946.T", "トーモク",                       "VOL"),   # folds=2 trades=10 PnL=+81,084    PF=7.31 WR=80.6%
    ("1975.T", "朝日工業社",                     "VOL"),   # folds=3 trades=10 PnL=+107,423   PF=5.04 WR=80.6%
    ("1942.T", "関電工",                         "VOL"),   # folds=2 trades=10 PnL=+122,212   PF=4.97 WR=83.3%
    ("6323.T", "ローツェ",                       "VOL"),   # folds=2 trades=11 PnL=+109,444   PF=4.46 WR=75.6%
    ("3099.T", "三越伊勢丹ホールディングス",       "VOL"),   # folds=2 trades=10 PnL=+75,858    PF=2.81 WR=69.4%
    ("8237.T", "松屋",                           "VOL"),   # folds=2 trades=10 PnL=+62,597    PF=4.41 WR=72.2%
    ("7231.T", "トピー工業",                     "VOL"),   # folds=2 trades=10 PnL=+57,537    PF=2.86 WR=69.4%
    # ── MOM (ブレイクアウト) ── 14銘柄通過 全14
    ("6101.T", "ツガミ",                         "MOM"),   # folds=2 trades=15 PnL=+329,185   PF=7.39 WR=88.9%
    ("1515.T", "日鉄鉱業",                       "MOM"),   # folds=2 trades=16 PnL=+302,592   PF=7.61 WR=86.7%
    ("9412.T", "スカパーＪＳＡＴ",               "MOM"),   # folds=2 trades=16 PnL=+157,454   PF=3.41 WR=75.5%
    ("8387.T", "四国銀行",                       "MOM"),   # folds=2 trades=14 PnL=+95,988    PF=5.81 WR=83.3%
    ("8522.T", "名古屋銀行",                     "MOM"),   # folds=2 trades=14 PnL=+133,652   PF=2.75 WR=70.0%
    ("8360.T", "山梨中央銀行",                   "MOM"),   # folds=2 trades=19 PnL=+131,975   PF=4.62 WR=74.6%
    ("8237.T", "松屋",                           "MOM"),   # folds=3 trades=16 PnL=+92,578    PF=2.70 WR=73.8%
    ("1961.T", "三機工業",                       "MOM"),   # folds=2 trades=15 PnL=+76,378    PF=4.39 WR=72.2%
    ("7453.T", "良品計画",                       "MOM"),   # folds=2 trades=12 PnL=+87,491    PF=2.60 WR=63.3%
    ("9305.T", "ヤマタネ",                       "MOM"),   # folds=2 trades=11 PnL=+72,261    PF=1.57 WR=42.2%
    ("7389.T", "あいちフィナンシャルグループ",     "MOM"),   # folds=2 trades=15 PnL=+48,770    PF=3.80 WR=77.5%
    ("5901.T", "東洋製罐グループホールディングス", "MOM"),   # folds=2 trades=11 PnL=+50,011    PF=1.99 WR=61.1%
    ("9991.T", "ジェコス",                       "MOM"),   # folds=2 trades=14 PnL=+32,501    PF=2.14 WR=64.4%
    ("8016.T", "オンワードホールディングス",       "MOM"),   # folds=2 trades=11 PnL=+14,588    PF=4.84 WR=80.6%
]

# 後方互換: build_html等が参照する旧変数名
STOP_WATCHLIST = STOP_CANDIDATES
BRK_WATCHLIST  = BRK_CANDIDATES

# WFバッジ用シンボルセット (候補銘柄すべて)
_WF_SYMS = sorted({s for s, _, _ in (STOP_CANDIDATES + BRK_CANDIDATES)})


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

    # 365日バックテストでPnL≤0 または取引回数<5の銘柄を自動除外
    all_items = [
        item for item in all_items
        if (item.get("period_results") or {}).get(365, {}).get("total_pnl", 0) > 0
        and (item.get("period_results") or {}).get(365, {}).get("trades", 0) >= 5
    ]

    order = {(s, st): i for i, (s, _, st) in enumerate(watchlist)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    for item in all_items:
        item["today_sig"] = mod.check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)
    return all_items




def _build_html(stop_html: str, brk_html: str, srt_html: str = "",
                sbrk_html: str = "",
                fund_rows: list | None = None, show_days: int = 365) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    stop_css  = _extract_style(stop_html)
    brk_css   = _extract_style(brk_html)
    srt_css   = _extract_style(srt_html) if srt_html else ""
    sbrk_css  = _extract_style(sbrk_html) if sbrk_html else ""
    stop_body = _extract_body(stop_html)
    brk_body  = _extract_body(brk_html)
    srt_body  = _extract_body(srt_html) if srt_html else ""
    sbrk_body = _extract_body(sbrk_html) if sbrk_html else ""

    extra_css = "\n".join(
        line for line in (brk_css + "\n" + srt_css + "\n" + sbrk_css).splitlines()
        if any(k in line for k in ("tag-don", "tag-vol", "tag-mom",
                                   "tag-macd_s", "tag-a7_s", "tag-rsi2_s",
                                   "tag-don_s", "tag-mom_s", "tag-gap_s"))
    )
    srt_tab_btn  = '<button class="tab-btn" onclick="switchTab(2)">ショート逆指値（A7_S）</button>' if srt_html else ""
    srt_tab_pane = f'<div id="tc2" class="tab-pane" style="display:none">\n{srt_body}\n</div>' if srt_html else ""
    sbrk_tab_btn  = '<button class="tab-btn" onclick="switchTab(3)">ショートBRK（DON_S/MOM_S/GAP_S）</button>' if sbrk_html else ""
    sbrk_tab_pane = f'<div id="tc3" class="tab-pane" style="display:none">\n{sbrk_body}\n</div>' if sbrk_html else ""

    wf_syms_js  = "[" + ",".join(f'"{s}"' for s in _WF_SYMS) + "]"
    funds_block = _fund_html(fund_rows or [], show_days)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Nolimit WF シグナルレポート — {today_str}</title>
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
</style>
</head>
<body>
{get_regime_html()}
<div class="mode-banner">🔓 NOLIMIT WF — {_OPT_LABEL} / 株価制限なし・プライム全銘柄 Walk-forward 選定 2026-05-12</div>
{funds_block}
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab(0)">逆指値B（MACD / A7 / RSI2）</button>
  <button class="tab-btn"        onclick="switchTab(1)">ブレイクアウト（DON / VOL / MOM）</button>
  {srt_tab_btn}
  {sbrk_tab_btn}
</div>
<div id="tc0" class="tab-pane">{stop_body}</div>
<div id="tc1" class="tab-pane" style="display:none">{brk_body}</div>
{srt_tab_pane}
{sbrk_tab_pane}
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
    parser = argparse.ArgumentParser(description="プライム全銘柄 WF シグナルレポート（株価制限なし）")
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--date",        type=str, default=None)
    parser.add_argument("--no-browser",  action="store_true")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--workers",     type=int, default=_stop._DEFAULT_WORKERS)
    parser.add_argument("--funds",       action="store_true",
                        help="指定期間のシグナル銘柄すべてに投資する場合の必要資金を表示")
    args = parser.parse_args()
    _auto_update_regime_cache(args.workers)

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"
    print(f"Nolimit WF シグナル 開始  パラメータ: {_OPT_LABEL}")
    print(f"  逆指値B : {len(STOP_WATCHLIST)}銘柄")
    print(f"  BRK    : {len(BRK_WATCHLIST)}銘柄")
    print(f"  株価制限なし・プライム全銘柄 Walk-forward 選定 60銘柄", flush=True)

    with ThreadPoolExecutor(max_workers=4) as outer:
        fut_stop  = outer.submit(_run_group_with_list, _stop,  STOP_WATCHLIST,    sig_date, args.workers)
        fut_brk   = outer.submit(_run_group_with_list, _brk,   BRK_WATCHLIST,     sig_date, args.workers)
        fut_short = outer.submit(_run_group_with_list, _short, _short.WATCHLIST,  sig_date, args.workers)
        fut_sbrk  = outer.submit(_run_group_with_list, _sbrk,  _sbrk.WATCHLIST,   sig_date, args.workers)
    stop_items  = filter_items(fut_stop.result())
    brk_items   = filter_items(fut_brk.result())
    short_items = filter_items(fut_short.result())
    sbrk_items  = filter_items(fut_sbrk.result())

    today = datetime.now(JST).strftime("%Y-%m-%d")
    print()
    print("=" * 92)
    print(f"  Nolimit WF シグナル  {today}  ({args.days}日表示)  シグナル確認日: {date_label}")
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
    for item in sbrk_items:
        if item["today_sig"]:
            score, rank = _sbrk.calc_recommend_score(item["period_results"])
            all_sigs.append((item, score, rank, "ショートBRK"))
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

    # ── --funds: 指定期間のシグナル銘柄・必要資金集計 ──────────────────
    fund_rows: list[dict] = []
    if args.funds:
        fund_rows = collect_fund_rows([stop_items, brk_items, short_items, sbrk_items], args.days)
        print_fund_summary(fund_rows, args.days)

    print(f"\nHTMLレポート生成中...", flush=True)
    _cmd = "python run_signals_nolimit.py"
    stop_html  = _stop.build_html(stop_items,   args.days, date_label, run_cmd=_cmd)
    brk_html   = _brk.build_html(brk_items,     args.days, date_label, run_cmd=_cmd)
    short_html = _short.build_html(short_items,  args.days, date_label, run_cmd=_cmd)
    sbrk_html  = _sbrk.build_html(sbrk_items,   args.days, date_label, run_cmd=_cmd)

    html_fund_rows = fund_rows if args.funds else None

    date_suffix = args.date if args.date else today
    out_path    = Path(f"signals_nolimit_{date_suffix}.html")
    out_path.write_text(
        _build_html(stop_html, brk_html, short_html, sbrk_html,
                    fund_rows=html_fund_rows, show_days=args.days),
        encoding="utf-8"
    )
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
