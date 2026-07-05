"""
nikkei_analysis_v2.py  ―  新WATCHLIST (2026-06-04 WF選定、価格上限なし) で日経分析を実行

nikkei_analysis.py と同じ分析を行うが、2026-06-04 WFスキャン (価格上限なし) で選定した
新WATCHLISTを使用する。nikkei_analysis.py 本体は変更しない (monkey-patch 方式)。

出力: nikkei_analysis_v2_{date}.html

使い方:
  python nikkei_analysis_v2.py                   # 過去5年 HTML生成 & ブラウザ表示
  python nikkei_analysis_v2.py --years 10        # 過去10年
  python nikkei_analysis_v2.py --date 2024-01-15 # 指定日時点の分析
  python nikkei_analysis_v2.py --no-browser      # HTML生成のみ
  python nikkei_analysis_v2.py --no-signals      # シグナルタブなし
  python nikkei_analysis_v2.py --no-pnl          # 損益タブなし
  python nikkei_analysis_v2.py --days 30         # 直近30日損益
  python nikkei_analysis_v2.py --v2only          # v2新WL conservative/aggressive のみ表示
  python nikkei_analysis_v2.py --v2only --aggressive  # aggressive 込みで v2新WL のみ
  python nikkei_analysis_v2.py --v2c             # v2新WL conservative のみ表示
"""
from __future__ import annotations

import argparse
import importlib as _importlib
import os
import sys
import webbrowser
from _open_html import open_html
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 1. TRADING_MODE を import 前に設定 ───────────────────────────────────────
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

# ── 2. シグナルモジュールを先にロード ────────────────────────────────────────
import check_signals_stop as _stop
import check_signals_breakout as _brk

# ── 3. 新 WATCHLIST (2026-06-04 WF選定、価格上限なし) ────────────────────────

NEW_STOP_WATCHLIST: list[tuple[str, str, str]] = [
    # ── A7: Walk-forward 選定 (2026-06-04, 価格上限なし) ──
    ("7003.T", "三井Ｅ＆Ｓ",                           "A7"),   # folds=2 19取引 WR79% PF4.39 DD5.4%
    ("6752.T", "パナソニックホールディングス",           "A7"),   # folds=2  9取引 WR100% PF10 DD1.8%  ※WR100%注意
    ("8360.T", "山梨中央銀行",                           "A7"),   # folds=2 15取引 WR80% PF5.96 DD5.6%
    ("4506.T", "住友ファーマ",                           "A7"),   # folds=2 12取引 WR75% PF3.94 DD7.8%
    ("6101.T", "ツガミ",                                 "A7"),   # folds=2 13取引 WR77% PF4.87 DD6.0%
    ("1814.T", "大末建設",                               "A7"),   # folds=2 12取引 WR75% PF4.93 DD9.2%
    ("8173.T", "上新電機",                               "A7"),   # folds=2 14取引 WR79% PF5.65 DD5.8%
    ("9956.T", "バローホールディングス",                 "A7"),   # folds=2 13取引 WR77% PF6.05 DD4.8%
    ("8544.T", "京葉銀行",                               "A7"),   # folds=2 13取引 WR77% PF4.51 DD9.3%
    ("5831.T", "しずおかフィナンシャルグループ",         "A7"),   # folds=2 12取引 WR75% PF5.17 DD6.3%
    # ── RSI2: Walk-forward 選定 (2026-06-04, 価格上限なし) ──
    ("3563.T", "ＦＯＯＤ＆ＬＩＦＥ ＣＯＭＰＡＮＩＥＳ", "RSI2"), # folds=2 13取引 WR85% PF7.29 DD6.2%
    ("8309.T", "三井住友トラスト・ホールディングス",     "RSI2"), # folds=2  8取引 WR100% PF10 DD3.3%  ※WR100%注意
    ("8061.T", "西華産業",                               "RSI2"), # folds=2  8取引 WR100% PF10 DD1.6%  ※WR100%注意
    ("5482.T", "愛知製鋼",                               "RSI2"), # folds=2 16取引 WR75% PF3.57 DD9.0%
    ("5821.T", "平河ヒューテック",                       "RSI2"), # folds=2 12取引 WR83% PF5.13 DD5.2%
    ("9960.T", "東テク",                                 "RSI2"), # folds=2 14取引 WR79% PF5.68 DD5.7%
    ("6501.T", "日立製作所",                             "RSI2"), # folds=2 11取引 WR82% PF7.34 DD5.3%
    ("6237.T", "イワキポンプ",                           "RSI2"), # folds=2 11取引 WR82% PF5.52 DD4.3%
    ("2540.T", "養命酒製造",                             "RSI2"), # folds=2 12取引 WR75% PF4.21 DD7.0%
    ("8393.T", "宮崎銀行",                               "RSI2"), # folds=2  6取引 WR100% PF10 DD2.6%  ※WR100%注意
    # ── MACD: Walk-forward 選定 (2026-06-04, 価格上限なし) ──
    ("6278.T", "ユニオンツール",                         "MACD"), # folds=2 16取引 WR81% PF5.57 DD7.2%
    ("9531.T", "東京瓦斯",                               "MACD"), # folds=2 17取引 WR71% PF4.29 DD7.9%
    ("1515.T", "日鉄鉱業",                               "MACD"), # folds=2 15取引 WR80% PF5.06 DD5.8%
    ("9072.T", "ニッコンホールディングス",               "MACD"), # folds=2 16取引 WR81% PF5.04 DD6.8%
    ("8377.T", "ほくほくフィナンシャルグループ",         "MACD"), # folds=2 12取引 WR75% PF4.76 DD8.3%
    ("7322.T", "三十三フィナンシャルグループ",           "MACD"), # folds=2 13取引 WR77% PF5.21 DD7.4%
    ("5482.T", "愛知製鋼",                               "MACD"), # folds=2 14取引 WR79% PF4.93 DD6.5%  ※RSI2と重複
    ("1417.T", "ミライト・ワン",                         "MACD"), # folds=2 13取引 WR77% PF3.89 DD9.5%
    ("7157.T", "ライフネット生命保険",                   "MACD"), # folds=2 11取引 WR73% PF3.62 DD8.7%
    ("6914.T", "オプテックス",                           "MACD"), # folds=2 12取引 WR75% PF4.23 DD7.1%
]

NEW_BRK_WATCHLIST: list[tuple[str, str, str]] = [
    # ── MOM: Walk-forward 選定 (2026-06-04, 価格上限なし) ──
    ("7242.T", "カヤバ",                                 "MOM"),  # folds=2 28取引 WR71% PF2.75 DD7.6%
    ("6762.T", "ＴＤＫ",                                 "MOM"),  # folds=2 27取引 WR75% PF4.95 DD7.8%
    ("8237.T", "松屋",                                   "MOM"),  # folds=2 35取引 WR75% PF2.93 DD8.1%
    ("7966.T", "リンテック",                             "MOM"),  # folds=2 22取引 WR67% PF1.68 DD13.2%
    ("4554.T", "富士製薬工業",                           "MOM"),  # folds=2 25取引 WR72% PF2.48 DD3.4%
    ("3964.T", "オークネット",                           "MOM"),  # folds=2 36取引 WR65% PF1.71 DD4.4%
    # ── DON: Walk-forward 選定 (2026-06-04, 価格上限なし) ──
    ("6875.T", "メガチップス",                           "DON"),  # folds=2 31取引 WR75% PF4.00 DD11.6%
    ("1515.T", "日鉄鉱業",                               "DON"),  # folds=2 25取引 WR68% PF2.59 DD10.9%
    # ── VOL: Walk-forward 選定 (2026-06-04, 価格上限なし) ──
    ("4461.T", "第一工業製薬",                           "VOL"),  # folds=2 12取引 WR85% PF7.17 DD8.4%
    ("7013.T", "ＩＨＩ",                                 "VOL"),  # folds=2 13取引 WR77% PF3.29 DD6.2%
    ("4390.T", "ＩＰＳ",                                 "VOL"),  # folds=2  9取引 WR78% PF4.21 DD4.8%
    ("9887.T", "松屋フーズホールディングス",             "VOL"),  # folds=2  7取引 WR90% PF7.73 DD3.6%
    ("6284.T", "日精エー・エス・ビー機械",               "VOL"),  # folds=2  7取引 WR71% PF2.16 DD10.1%
    ("1803.T", "清水建設",                               "VOL"),  # folds=2  8取引 WR63% PF3.94 DD3.4%
    ("1945.T", "東京エネシス",                           "VOL"),  # folds=2 21取引 WR67% PF1.88 DD10.7%
    ("9843.T", "ニトリホールディングス",                 "VOL"),  # folds=2  9取引 WR75% PF5.73 DD5.9%
    ("6952.T", "カシオ計算機",                           "VOL"),  # folds=2  7取引 WR71% PF3.80 DD1.3%
]

# ── aggressive 専用 WATCHLIST ────────────────────────────────────────────────
# 現在は空 → conservative と同じWLで代用（暫定）
# 更新手順:
#   python scan_walkforward.py --aggressive --symbols symbols_listed_prime.py
#   python build_watchlist.py --aggressive
#   → watchlist_proposal_aggressive_YYYY-MM-DD.py の
#     STOP_WATCHLIST / BRK_WATCHLIST をそれぞれ下にコピー
NEW_STOP_WATCHLIST_AGG: list[tuple[str, str, str]] = [
    # ── RSI2 Walk-forward 上位 10 (aggressive, 2026-06-05) ──
    ("6857.T", "アドバンテスト",                         "RSI2"),  # folds=2 pnl=+1,056,312 pf=10.0 wr=100% DD=0.0%  ※26,765円/株
    ("8014.T", "蝶理",                                   "RSI2"),  # folds=2 pnl=+126,762   pf=10.0 wr=100% DD=0.0%
    ("4631.T", "ＤＩＣ",                                 "RSI2"),  # folds=2 pnl=+66,664    pf=10.0 wr=100% DD=0.0%
    ("4004.T", "レゾナック・ホールディングス",           "RSI2"),  # folds=2 pnl=+298,340   pf=10.0 wr=100% DD=0.0%  ※17,315円/株
    ("8316.T", "三井住友フィナンシャルグループ",         "RSI2"),  # folds=2 pnl=+128,465   pf=9.73 wr=91.7% DD=2.14%
    ("2540.T", "養命酒製造",                             "RSI2"),  # folds=2 pnl=+106,032   pf=10.0 wr=70.8% DD=0.67%
    ("8061.T", "西華産業",                               "RSI2"),  # folds=2 pnl=+134,732   pf=6.7  wr=93.8% DD=4.94%
    ("6770.T", "アルプスアルパイン",                     "RSI2"),  # folds=2 pnl=+56,808    pf=6.3  wr=90.0% DD=2.67%
    ("5821.T", "平河ヒューテック",                       "RSI2"),  # folds=2 pnl=+76,349    pf=3.01 wr=73.3% DD=6.18%
    ("6788.T", "日本トリム",                             "RSI2"),  # folds=2 pnl=+66,979    pf=3.84 wr=66.7% DD=3.97%
    # ── A7 Walk-forward 上位 10 (aggressive, 2026-06-05) ──
    ("7003.T", "三井Ｅ＆Ｓ",                             "A7"),    # folds=2 pnl=+415,668   pf=6.36 wr=90.0% DD=7.74%
    ("4578.T", "大塚ホールディングス",                   "A7"),    # folds=2 pnl=+255,340   pf=2.92 wr=78.6% DD=11.91% ※10,580円/株
    ("6875.T", "メガチップス",                           "A7"),    # folds=2 pnl=+278,255   pf=3.09 wr=78.9% DD=11.4%  ※12,220円/株
    ("8360.T", "山梨中央銀行",                           "A7"),    # folds=2 pnl=+201,374   pf=2.29 wr=68.3% DD=14.08%
    ("8522.T", "名古屋銀行",                             "A7"),    # folds=2 pnl=+202,267   pf=5.73 wr=78.6% DD=11.69%
    ("1885.T", "東亜建設工業",                           "A7"),    # folds=2 pnl=+125,727   pf=3.98 wr=70.9% DD=3.8%
    ("8061.T", "西華産業",                               "A7"),    # folds=2 pnl=+117,720   pf=3.79 wr=83.3% DD=4.34%
    ("4229.T", "群栄化学工業",                           "A7"),    # folds=2 pnl=+80,123    pf=6.36 wr=87.5% DD=2.7%
    ("4506.T", "住友ファーマ",                           "A7"),    # folds=2 pnl=+91,109    pf=5.97 wr=83.3% DD=3.24%
    ("1814.T", "大末建設",                               "A7"),    # folds=2 pnl=+79,849    pf=6.18 wr=88.9% DD=4.42%
    # ── MACD Walk-forward 上位 10 (aggressive, 2026-06-05) ──
    ("8877.T", "エスリード",                             "MACD"),  # folds=2 pnl=+199,465   pf=3.54 wr=80.1% DD=7.31%
    ("4205.T", "日本ゼオン",                             "MACD"),  # folds=2 pnl=+53,242    pf=10.0 wr=100% DD=0.0%
    ("9024.T", "西武ホールディングス",                   "MACD"),  # folds=2 pnl=+153,627   pf=3.02 wr=77.1% DD=5.68%
    ("1417.T", "ミライト・ワン",                         "MACD"),  # folds=2 pnl=+100,411   pf=6.89 wr=92.9% DD=3.52%
    ("9072.T", "ニッコンホールディングス",               "MACD"),  # folds=2 pnl=+105,036   pf=6.96 wr=83.3% DD=4.02%
    ("1975.T", "朝日工業社",                             "MACD"),  # folds=2 pnl=+141,035   pf=3.49 wr=75.0% DD=9.25%
    ("8386.T", "百十四銀行",                             "MACD"),  # folds=2 pnl=+90,265    pf=6.59 wr=87.3% DD=3.23%
    ("7322.T", "三十三フィナンシャルグループ",           "MACD"),  # folds=2 pnl=+63,796    pf=10.0 wr=94.4% DD=1.08%
    ("7181.T", "かんぽ生命保険",                         "MACD"),  # folds=2 pnl=+45,259    pf=4.04 wr=75.6% DD=1.49%
    ("8387.T", "四国銀行",                               "MACD"),  # folds=2 pnl=+54,742    pf=5.9  wr=85.0% DD=5.64%
]
NEW_BRK_WATCHLIST_AGG: list[tuple[str, str, str]] = [
    # ── DON Walk-forward 上位 5 (aggressive, 2026-06-05) ──
    ("3659.T", "ネクソン",                               "DON"),   # folds=2 pnl=+120,200   pf=6.4  wr=83.3% DD=3.25%
    ("8386.T", "百十四銀行",                             "DON"),   # folds=2 pnl=+145,053   pf=3.23 wr=78.0% DD=9.21%
    ("1975.T", "朝日工業社",                             "DON"),   # folds=2 pnl=+112,866   pf=2.02 wr=74.2% DD=12.2%
    ("7419.T", "ノジマ",                                 "DON"),   # folds=2 pnl=+45,823    pf=2.76 wr=69.6% DD=3.47%
    ("9830.T", "トラスコ中山",                           "DON"),   # folds=2 pnl=+24,562    pf=1.36 wr=67.9% DD=5.75%
    # ── VOL Walk-forward 上位 10 (aggressive, 2026-06-05) ──
    ("7013.T", "ＩＨＩ",                                 "VOL"),   # folds=2 pnl=+183,530   pf=6.18 wr=85.7% DD=5.5%
    ("1975.T", "朝日工業社",                             "VOL"),   # folds=2 pnl=+98,066    pf=6.55 wr=90.0% DD=4.6%
    ("6779.T", "日本電波工業",                           "VOL"),   # folds=2 pnl=+156,964   pf=2.17 wr=64.1% DD=9.55%
    ("1952.T", "新日本空調",                             "VOL"),   # folds=2 pnl=+75,169    pf=6.3  wr=77.5% DD=3.37%
    ("3197.T", "すかいらーくホールディングス",           "VOL"),   # folds=2 pnl=+67,288    pf=5.76 wr=83.3% DD=3.18%
    ("4390.T", "ＩＰＳ",                                 "VOL"),   # folds=2 pnl=+81,962    pf=2.67 wr=77.5% DD=5.36%
    ("7181.T", "かんぽ生命保険",                         "VOL"),   # folds=2 pnl=+27,650    pf=6.83 wr=91.7% DD=1.18%
    ("6284.T", "日精エー・エス・ビー機械",               "VOL"),   # folds=2 pnl=+67,133    pf=1.6  wr=70.8% DD=11.35% ※9,210円/株
    ("6952.T", "カシオ計算機",                           "VOL"),   # folds=2 pnl=+21,161    pf=3.94 wr=70.8% DD=1.34%
    ("8086.T", "ニプロ",                                 "VOL"),   # folds=2 pnl=+27,207    pf=5.97 wr=85.0% DD=2.71%
    # ── MOM Walk-forward 上位 5 (aggressive, 2026-06-05) ──
    ("7242.T", "カヤバ",                                 "MOM"),   # folds=2 pnl=+206,176   pf=6.35 wr=91.3% DD=4.78%
    ("7013.T", "ＩＨＩ",                                 "MOM"),   # folds=2 pnl=+273,410   pf=2.2  wr=73.5% DD=13.76%
    ("8011.T", "三陽商会",                               "MOM"),   # folds=2 pnl=+152,407   pf=5.65 wr=76.8% DD=7.49%
    ("1975.T", "朝日工業社",                             "MOM"),   # folds=2 pnl=+122,789   pf=4.41 wr=71.9% DD=5.99%
    ("8016.T", "オンワードホールディングス",             "MOM"),   # folds=2 pnl=+30,794    pf=5.03 wr=84.5% DD=1.13%
]

# ── 4. v2 用 WF スコアファイルを読み込む ────────────────────────────────────
#   wf_scores.json     → nikkei_analysis.py 専用 (触らない)
#   wf_scores_v2.json  → nikkei_analysis_v2.py 専用
import json as _json

_WF_V2_PATH = Path("wf_scores_v2.json")
if _WF_V2_PATH.exists():
    with open(_WF_V2_PATH, encoding="utf-8") as _f:
        _WF_V2_SCORES: dict = _json.load(_f)
else:
    from compute_wf_scores import build_wf_scores as _bwf
    _WF_V2_SCORES = _bwf()

# ── 5. WATCHLIST と WF スコアを適用 ─────────────────────────────────────────
_stop.WATCHLIST  = list(NEW_STOP_WATCHLIST)
_brk.WATCHLIST   = list(NEW_BRK_WATCHLIST)
_stop._WF_SCORES = dict(_WF_V2_SCORES)
_brk._WF_SCORES  = dict(_WF_V2_SCORES)

# ── 6. importlib.reload フック (WATCHLIST と WF スコアを reload 後も維持) ────
#  nikkei_analysis.py は TRADING_MODE 切替のために _stop/_brk を reload() する。
#  reload すると WATCHLIST と _WF_SCORES が元に戻るため、フックで再適用する。
_orig_reload = _importlib.reload

def _watchlist_preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST  = list(NEW_STOP_WATCHLIST)
        result._WF_SCORES = dict(_WF_V2_SCORES)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST  = list(NEW_BRK_WATCHLIST)
        result._WF_SCORES = dict(_WF_V2_SCORES)
    return result

_importlib.reload = _watchlist_preserving_reload

# ── 7. nikkei_analysis を import (パッチ済み WATCHLIST/スコアが使われる) ──────
import nikkei_analysis as _na  # noqa: E402

# ── 8. reload フックを元に戻す ────────────────────────────────────────────────
_importlib.reload = _orig_reload

# ── 9. PNL タブのラベルと watchlist を v2 用に更新 ───────────────────────────
# aggressive 専用 WL が未設定（空）の場合は conservative と同じものを暫定使用
_stop_agg = NEW_STOP_WATCHLIST_AGG if NEW_STOP_WATCHLIST_AGG else list(NEW_STOP_WATCHLIST)
_brk_agg  = NEW_BRK_WATCHLIST_AGG  if NEW_BRK_WATCHLIST_AGG  else list(NEW_BRK_WATCHLIST)

for _cfg in _na._PNL_CONFIGS:
    if _cfg.get("label") == "既存版 conservative":
        _cfg["label"]   = "v2新WL conservative"
        _cfg["stop_wl"] = list(NEW_STOP_WATCHLIST)
        _cfg["brk_wl"]  = list(NEW_BRK_WATCHLIST)
    elif _cfg.get("label") == "既存版 aggressive":
        _cfg["label"]   = "v2新WL aggressive"
        _cfg["stop_wl"] = _stop_agg
        _cfg["brk_wl"]  = _brk_agg


# ═══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # 出力ファイル名に使う日付を先読み
    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--date",       type=str,  default=None)
    _p.add_argument("--no-browser", action="store_true")
    _p.add_argument("--v2c",        action="store_true",
                    help="v2新WL conservative のシグナル・損益のみ表示")
    _p.add_argument("--v2only",     action="store_true",
                    help="v2新WL conservative + aggressive のみ表示 (他設定を除外)")
    _known, _ = _p.parse_known_args()

    # --v2c / --v2only / --aggressive は nikkei_analysis.py が知らないので除去してから渡す
    _orig_argv = list(sys.argv)
    sys.argv = [a for a in sys.argv if a not in ("--v2c", "--v2only", "--aggressive")]

    if _known.v2c and "--config" not in sys.argv:
        sys.argv.append("--config")
        sys.argv.append("v2新WL conservative")

    # --v2only: _PNL_CONFIGS を v2新WL の2件のみに絞る
    if _known.v2only:
        _na._PNL_CONFIGS[:] = [
            c for c in _na._PNL_CONFIGS
            if c.get("label", "").startswith("v2新WL")
        ]

    # nikkei_analysis.main() 内でのブラウザ起動を抑制（リネーム後に自分で開く）
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")

    print("=" * 60)
    print("nikkei_analysis_v2: 新WATCHLIST (2026-06-04 WF選定、価格上限なし)")
    print(f"  逆指値B: {len(NEW_STOP_WATCHLIST)} 銘柄×戦略")
    print(f"  BRK    : {len(NEW_BRK_WATCHLIST)} 銘柄×戦略")
    print("=" * 60)

    _na.main()

    # sys.argv を元に戻す
    sys.argv[:] = _orig_argv

    # 出力ファイル名を決定
    JST      = timezone(timedelta(hours=9))
    date_str = _known.date if _known.date else str(datetime.now(JST).date())
    old_path = Path(f"nikkei_analysis_{date_str}.html")
    if _known.v2c:
        suffix = "_v2c"
    elif _known.v2only:
        suffix = "_v2only"
    else:
        suffix = ""
    new_path = Path(f"nikkei_analysis_v2{suffix}_{date_str}.html")

    if old_path.exists():
        old_path.replace(new_path)
        print(f"\nv2レポート生成完了: {new_path.resolve()}")
        if not _known.no_browser:
            open_html(new_path.resolve().as_uri())
    else:
        print(f"[WARN] {old_path} が見つかりませんでした (--date 指定時は日付を確認)")


if __name__ == "__main__":
    main()
