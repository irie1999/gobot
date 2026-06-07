"""
nikkei_analysis_4config.py  ―  4設定版 日経平均 総合分析レポート

以下の4configを1枚のHTMLで分析する:
  v2新WL conservative  : 2026-06-04 WFスキャン (全価格帯, tm=3.0)
  v2新WL aggressive    : 2026-06-05 WFスキャン (全価格帯, tm=2.0)
  5k-WL conservative   : 2026-06-06 WFスキャン (≤5000円,  tm=3.0)
  5k-WL aggressive     : 2026-06-06 WFスキャン (≤5000円,  tm=2.0)

出力: nikkei_4config_{date}.html

使い方:
  python nikkei_analysis_4config.py                 # 過去5年 HTML & ブラウザ表示
  python nikkei_analysis_4config.py --years 10      # 過去10年
  python nikkei_analysis_4config.py --days 365      # 損益集計365日
  python nikkei_analysis_4config.py --no-browser    # HTML生成のみ
  python nikkei_analysis_4config.py --no-pnl        # 損益タブなし
  python nikkei_analysis_4config.py --workers 8     # 並列数

  # 銘柄×戦略の個別履歴確認
  python nikkei_analysis_4config.py --symbol 8387.T --strategy A7
  python nikkei_analysis_4config.py --symbol 8387.T --strategy RSI2 --days 180
  python nikkei_analysis_4config.py --symbol 8387.T --strategy MACD --aggressive
"""
from __future__ import annotations

import argparse
import copy as _copy
import importlib as _importlib
import os
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
from datetime import datetime, timedelta, timezone, date as _date_type
from pathlib import Path

import pandas as pd

JST    = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()

# ── TRADING_MODE を import 前に設定 ─────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")

# ── シグナル/バックテストモジュールをロード ─────────────────────────────────
import check_signals_stop     as _stop
import check_signals_breakout as _brk
import nikkei_analysis        as _na
from backtest_limit_entry import WORKERS as _DEF_WORKERS

# ── モード別パラメータをスナップショット ──────────────────────────────────────
_CON_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_CON_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
os.environ["TRADING_MODE"] = "aggressive"
_importlib.reload(_stop); _importlib.reload(_brk)
_AGG_STOP = _copy.deepcopy(_stop.STRATEGY_PARAMS)
_AGG_BRK  = _copy.deepcopy(_brk.STRATEGY_PARAMS)
os.environ["TRADING_MODE"] = "conservative"
_importlib.reload(_stop); _importlib.reload(_brk)

# ══════════════════════════════════════════════════════════════════════════════
# 5k-WL conservative  ―  check_signals_stop/breakout の現 WATCHLIST そのまま
# ══════════════════════════════════════════════════════════════════════════════
_5K_STOP_CON: list[tuple[str, str, str]] = list(_stop.WATCHLIST)
_5K_BRK_CON:  list[tuple[str, str, str]] = list(_brk.WATCHLIST)

# ══════════════════════════════════════════════════════════════════════════════
# 5k-WL aggressive  ―  2026-06-06 WFスキャン --aggressive --max-price 5000
# ══════════════════════════════════════════════════════════════════════════════
_5K_STOP_AGG: list[tuple[str, str, str]] = [
    # ── RSI2 (2026-06-06, aggressive, ≤5000円) ──
    ("8061.T", "西華産業",                           "RSI2"),  # PF6.81 WR93.8% DD=9.6%  Shrp4.01  17取引
    ("2540.T", "養命酒製造",                         "RSI2"),  # PF10.0 WR70.8% DD=1.1%  Shrp5.41  10取引
    ("6770.T", "アルプスアルパイン",                 "RSI2"),  # PF6.36 WR90.0% DD=8.2%  Shrp3.98   9取引
    ("8387.T", "四国銀行",                           "RSI2"),  # PF5.67 WR83.3% DD=6.7%  Shrp3.58  11取引
    ("1930.T", "北陸電気工事",                       "RSI2"),  # PF5.95 WR75.0% DD=5.6%  Shrp3.85   6取引
    ("5821.T", "平河ヒューテック",                   "RSI2"),  # PF2.92 WR73.3% DD=10.1% Shrp2.20   8取引
    ("8070.T", "東京産業",                           "RSI2"),  # PF4.04 WR75.0% DD=6.1%  Shrp2.84   9取引
    ("6788.T", "日本トリム",                         "RSI2"),  # PF5.65 WR72.2% DD=6.7%  Shrp1.85  15取引
    ("7167.T", "めぶきフィナンシャルグループ",       "RSI2"),  # PF5.79 WR83.3% DD=14.4% Shrp1.93   8取引
    ("4631.T", "ＤＩＣ",                             "RSI2"),  # PF10.0 WR100%  DD=0.0%  Shrp25.1   7取引
    # ── MACD (2026-06-06, aggressive, ≤5000円) ──
    ("8386.T", "百十四銀行",                         "MACD"),  # PF6.75 WR92.9% DD=7.0%  Shrp4.69  16取引
    ("7322.T", "三十三フィナンシャルグループ",       "MACD"),  # PF8.75 WR94.4% DD=7.6%  Shrp5.69  14取引
    ("1417.T", "ミライト・ワン",                     "MACD"),  # PF6.89 WR92.9% DD=5.6%  Shrp4.62  14取引
    ("7181.T", "かんぽ生命保険",                     "MACD"),  # PF4.03 WR75.6% DD=6.0%  Shrp3.19  16取引
    ("8387.T", "四国銀行",                           "MACD"),  # PF5.08 WR85.0% DD=12.2% Shrp2.55  20取引
    ("8877.T", "エスリード",                         "MACD"),  # PF3.54 WR80.1% DD=12.3% Shrp2.54  19取引
    ("9024.T", "西武ホールディングス",               "MACD"),  # PF3.02 WR77.1% DD=9.0%  Shrp2.47  11取引
    ("1975.T", "朝日工業社",                         "MACD"),  # PF3.16 WR75.0% DD=13.8% Shrp2.02  20取引
    ("4205.T", "日本ゼオン",                         "MACD"),  # PF10.0 WR100%  DD=0.0%  Shrp13.2  10取引
    ("6914.T", "オプテックスグループ",               "MACD"),  # PF2.48 WR77.3% DD=14.7% Shrp1.88  22取引
    # ── A7 (2026-06-06, aggressive, ≤5000円) ──
    ("7003.T", "三井Ｅ＆Ｓ",                         "A7"),    # PF6.36 WR90.0% DD=10.1% Shrp4.15  12取引
    ("8173.T", "Ｊｏｓｈｉｎ",                       "A7"),    # PF8.76 WR95.8% DD=4.0%  Shrp5.99  21取引
    ("8061.T", "西華産業",                           "A7"),    # PF6.20 WR88.9% DD=7.0%  Shrp3.58  18取引
    ("6331.T", "三菱化工機",                         "A7"),    # PF5.28 WR81.2% DD=6.6%  Shrp3.32  12取引
    ("1885.T", "東亜建設工業",                       "A7"),    # PF3.86 WR70.9% DD=11.8% Shrp2.62  16取引
    ("8336.T", "武蔵野銀行",                         "A7"),    # PF4.48 WR84.5% DD=6.7%  Shrp3.11  13取引
    ("8346.T", "東邦銀行",                           "A7"),    # PF4.08 WR77.4% DD=8.4%  Shrp2.64  13取引
    ("5831.T", "しずおかフィナンシャルグループ",     "A7"),    # PF6.71 WR87.5% DD=4.4%  Shrp4.14   9取引
    ("8387.T", "四国銀行",                           "A7"),    # PF7.20 WR91.7% DD=6.3%  Shrp3.18   9取引
    ("5482.T", "愛知製鋼",                           "A7"),    # PF3.48 WR71.2% DD=7.6%  Shrp2.62  13取引
]

_5K_BRK_AGG: list[tuple[str, str, str]] = [
    # ── DON (2026-06-06, aggressive, ≤5000円) ──
    ("3659.T", "ネクソン",                           "DON"),   # PF6.70 WR83.3% DD=9.7%  Shrp2.85  18取引
    # ── MOM (2026-06-06, aggressive, ≤5000円) ──
    ("8237.T", "松屋",                               "MOM"),   # PF4.58 WR85.8% DD=7.8%  Shrp3.35  35取引
    ("7242.T", "カヤバ",                             "MOM"),   # PF6.52 WR91.7% DD=7.4%  Shrp3.09  29取引
    ("8016.T", "オンワードホールディングス",         "MOM"),   # PF5.81 WR88.7% DD=9.0%  Shrp3.78  26取引
    ("1975.T", "朝日工業社",                         "MOM"),   # PF3.90 WR71.9% DD=8.7%  Shrp2.17  22取引
    ("8011.T", "三陽商会",                           "MOM"),   # PF4.74 WR69.3% DD=11.3% Shrp2.21  19取引
    # ── VOL (2026-06-06, aggressive, ≤5000円) ──
    ("7013.T", "ＩＨＩ",                             "VOL"),   # PF6.18 WR85.7% DD=12.7% Shrp3.45  13取引
    ("1952.T", "新日本空調",                         "VOL"),   # PF6.50 WR77.5% DD=5.4%  Shrp3.99   9取引
    ("7181.T", "かんぽ生命保険",                     "VOL"),   # PF6.93 WR91.7% DD=6.8%  Shrp3.91   8取引
    ("3197.T", "すかいらーくホールディングス",       "VOL"),   # PF5.76 WR83.3% DD=5.4%  Shrp3.74   7取引
    ("4390.T", "ＩＰＳ",                             "VOL"),   # PF2.75 WR77.5% DD=11.5% Shrp2.16   9取引
    ("6952.T", "カシオ計算機",                       "VOL"),   # PF6.79 WR70.8% DD=6.2%  Shrp3.16   7取引
    ("8086.T", "ニプロ",                             "VOL"),   # PF5.97 WR85.0% DD=13.9% Shrp1.78  12取引
    ("7322.T", "三十三フィナンシャルグループ",       "VOL"),   # PF2.43 WR77.5% DD=9.8%  Shrp1.76   9取引
    ("8016.T", "オンワードホールディングス",         "VOL"),   # PF5.68 WR85.0% DD=12.6% Shrp1.43  14取引
]

# ══════════════════════════════════════════════════════════════════════════════
# v2新WL conservative  ―  2026-06-04 WFスキャン (全価格帯, tm=3.0)
# ══════════════════════════════════════════════════════════════════════════════
_V2_STOP_CON: list[tuple[str, str, str]] = [
    # ── A7 (2026-06-04, conservative, 全価格帯) ──
    ("7003.T", "三井Ｅ＆Ｓ",                           "A7"),
    ("6752.T", "パナソニックホールディングス",           "A7"),
    ("8360.T", "山梨中央銀行",                           "A7"),
    ("4506.T", "住友ファーマ",                           "A7"),
    ("6101.T", "ツガミ",                                 "A7"),
    ("1814.T", "大末建設",                               "A7"),
    ("8173.T", "上新電機",                               "A7"),
    ("9956.T", "バローホールディングス",                 "A7"),
    ("8544.T", "京葉銀行",                               "A7"),
    ("5831.T", "しずおかフィナンシャルグループ",         "A7"),
    # ── RSI2 (2026-06-04, conservative, 全価格帯) ──
    ("3563.T", "ＦＯＯＤ＆ＬＩＦＥ ＣＯＭＰＡＮＩＥＳ", "RSI2"),
    ("8309.T", "三井住友トラスト・ホールディングス",     "RSI2"),
    ("8061.T", "西華産業",                               "RSI2"),
    ("5482.T", "愛知製鋼",                               "RSI2"),
    ("5821.T", "平河ヒューテック",                       "RSI2"),
    ("9960.T", "東テク",                                 "RSI2"),
    ("6237.T", "イワキポンプ",                           "RSI2"),
    ("2540.T", "養命酒製造",                             "RSI2"),
    ("8393.T", "宮崎銀行",                               "RSI2"),
    # ── MACD (2026-06-04, conservative, 全価格帯) ──
    ("9531.T", "東京瓦斯",                               "MACD"),
    ("1515.T", "日鉄鉱業",                               "MACD"),
    ("9072.T", "ニッコンホールディングス",               "MACD"),
    ("8377.T", "ほくほくフィナンシャルグループ",         "MACD"),
    ("7322.T", "三十三フィナンシャルグループ",           "MACD"),
    ("5482.T", "愛知製鋼",                               "MACD"),
    ("1417.T", "ミライト・ワン",                         "MACD"),
    ("7157.T", "ライフネット生命保険",                   "MACD"),
    ("6914.T", "オプテックス",                           "MACD"),
]

_V2_BRK_CON: list[tuple[str, str, str]] = [
    # ── MOM (2026-06-04, conservative, 全価格帯) ──
    ("7242.T", "カヤバ",                                 "MOM"),
    ("6762.T", "ＴＤＫ",                                 "MOM"),
    ("8237.T", "松屋",                                   "MOM"),
    ("7966.T", "リンテック",                             "MOM"),
    ("4554.T", "富士製薬工業",                           "MOM"),
    ("3964.T", "オークネット",                           "MOM"),
    # ── DON (2026-06-04, conservative, 全価格帯) ──
    ("1515.T", "日鉄鉱業",                               "DON"),
    # ── VOL (2026-06-04, conservative, 全価格帯) ──
    ("4461.T", "第一工業製薬",                           "VOL"),
    ("7013.T", "ＩＨＩ",                                 "VOL"),
    ("4390.T", "ＩＰＳ",                                 "VOL"),
    ("9887.T", "松屋フーズホールディングス",             "VOL"),
    ("1803.T", "清水建設",                               "VOL"),
    ("1945.T", "東京エネシス",                           "VOL"),
    ("6952.T", "カシオ計算機",                           "VOL"),
]

# ══════════════════════════════════════════════════════════════════════════════
# v2新WL aggressive  ―  2026-06-05 WFスキャン (全価格帯, tm=2.0)
# ══════════════════════════════════════════════════════════════════════════════
_V2_STOP_AGG: list[tuple[str, str, str]] = [
    # ── RSI2 (2026-06-05, aggressive, 全価格帯) ──
    ("8014.T", "蝶理",                                   "RSI2"),
    ("4631.T", "ＤＩＣ",                                 "RSI2"),
    ("8316.T", "三井住友フィナンシャルグループ",         "RSI2"),
    ("2540.T", "養命酒製造",                             "RSI2"),
    ("8061.T", "西華産業",                               "RSI2"),
    ("6770.T", "アルプスアルパイン",                     "RSI2"),
    ("5821.T", "平河ヒューテック",                       "RSI2"),
    ("6788.T", "日本トリム",                             "RSI2"),
    # ── A7 (2026-06-05, aggressive, 全価格帯) ──
    ("7003.T", "三井Ｅ＆Ｓ",                             "A7"),
    ("8360.T", "山梨中央銀行",                           "A7"),
    ("8522.T", "名古屋銀行",                             "A7"),
    ("1885.T", "東亜建設工業",                           "A7"),
    ("8061.T", "西華産業",                               "A7"),
    ("4229.T", "群栄化学工業",                           "A7"),
    ("4506.T", "住友ファーマ",                           "A7"),
    ("1814.T", "大末建設",                               "A7"),
    # ── MACD (2026-06-05, aggressive, 全価格帯) ──
    ("8877.T", "エスリード",                             "MACD"),
    ("4205.T", "日本ゼオン",                             "MACD"),
    ("9024.T", "西武ホールディングス",                   "MACD"),
    ("1417.T", "ミライト・ワン",                         "MACD"),
    ("9072.T", "ニッコンホールディングス",               "MACD"),
    ("1975.T", "朝日工業社",                             "MACD"),
    ("8386.T", "百十四銀行",                             "MACD"),
    ("7322.T", "三十三フィナンシャルグループ",           "MACD"),
    ("7181.T", "かんぽ生命保険",                         "MACD"),
    ("8387.T", "四国銀行",                               "MACD"),
]

_V2_BRK_AGG: list[tuple[str, str, str]] = [
    # ── DON (2026-06-05, aggressive, 全価格帯) ──
    ("3659.T", "ネクソン",                               "DON"),
    ("8386.T", "百十四銀行",                             "DON"),
    ("7419.T", "ノジマ",                                 "DON"),
    ("9830.T", "トラスコ中山",                           "DON"),
    # ── VOL (2026-06-05, aggressive, 全価格帯) ──
    ("7013.T", "ＩＨＩ",                                 "VOL"),
    ("6779.T", "日本電波工業",                           "VOL"),
    ("1952.T", "新日本空調",                             "VOL"),
    ("3197.T", "すかいらーくホールディングス",           "VOL"),
    ("4390.T", "ＩＰＳ",                                 "VOL"),
    ("7181.T", "かんぽ生命保険",                         "VOL"),
    ("6284.T", "日精エー・エス・ビー機械",               "VOL"),
    ("6952.T", "カシオ計算機",                           "VOL"),
    ("8086.T", "ニプロ",                                 "VOL"),
    # ── MOM (2026-06-05, aggressive, 全価格帯) ──
    ("7242.T", "カヤバ",                                 "MOM"),
    ("8011.T", "三陽商会",                               "MOM"),
    ("1975.T", "朝日工業社",                             "MOM"),
    ("8016.T", "オンワードホールディングス",             "MOM"),
]

print("=" * 70)
print("nikkei_analysis_4config: 4設定分析レポート")
print(f"  v2新WL conservative  逆指値B:{len(_V2_STOP_CON)} BRK:{len(_V2_BRK_CON)}")
print(f"  v2新WL aggressive    逆指値B:{len(_V2_STOP_AGG)} BRK:{len(_V2_BRK_AGG)}")
print(f"  5k-WL conservative   逆指値B:{len(_5K_STOP_CON)} BRK:{len(_5K_BRK_CON)}")
print(f"  5k-WL aggressive     逆指値B:{len(_5K_STOP_AGG)} BRK:{len(_5K_BRK_AGG)}")
print("=" * 70)

# ── 4設定定義 ────────────────────────────────────────────────────────────────
CONFIGS = [
    {
        "label":    "v2新WL conservative",
        "color":    "#06b6d4",
        "mode":     "conservative",
        "stop_wl":  _V2_STOP_CON,
        "brk_wl":   _V2_BRK_CON,
        "risk":     "低中",
        "note":     "全価格帯 WFスキャン (2026-06-04) × conservative。横ばい・下落でも安定。",
    },
    {
        "label":    "v2新WL aggressive",
        "color":    "#f39c12",
        "mode":     "aggressive",
        "stop_wl":  _V2_STOP_AGG,
        "brk_wl":   _V2_BRK_AGG,
        "risk":     "中",
        "note":     "全価格帯 WFスキャン (2026-06-05) × aggressive。上昇相場で最大効率。",
    },
    {
        "label":    "5k-WL conservative",
        "color":    "#3498db",
        "mode":     "conservative",
        "stop_wl":  _5K_STOP_CON,
        "brk_wl":   _5K_BRK_CON,
        "risk":     "低",
        "note":     "5000円以下 WFスキャン (2026-06-06) × conservative。1取引~100万円。全相場対応。",
    },
    {
        "label":    "5k-WL aggressive",
        "color":    "#e74c3c",
        "mode":     "aggressive",
        "stop_wl":  _5K_STOP_AGG,
        "brk_wl":   _5K_BRK_AGG,
        "risk":     "低中",
        "note":     "5000円以下 WFスキャン (2026-06-06) × aggressive。上昇相場で高回転。",
    },
]

RISK_COLOR = {"高": "#f87171", "中高": "#fb923c", "中": "#fbbf24", "低中": "#86efac", "低": "#4ade80"}
STATUS_META = {
    "✅ 推奨": ("推奨", "#4ade80", "#052e16", "#166534"),
    "⚠️ 注意": ("注意", "#fbbf24", "#2d1f00", "#92400e"),
    "❌ 停止": ("停止", "#f87171", "#2d0a0a", "#991b1b"),
}


def _judge_config(cfg: dict, r: dict) -> tuple[str, str, str]:
    """(status, reason, advice)  status= ✅推奨 / ⚠️注意 / ❌停止"""
    trend = r["trend"]
    vol   = r["vol_level"]
    mom5  = r["mom5"]
    mom20 = r["mom20"]
    above = r["above_ma200"]
    drop  = r["max_1d_drop"]
    mode  = cfg["mode"]
    crash = drop < -3.0

    if mode == "aggressive":
        if trend == "down" and vol == "high":
            return "❌ 停止", f"下落×高ボラ (Vol={r['vol']:.2f}%)", "conservative に切替え。シグナル数を絞る"
        if trend == "down":
            return "⚠️ 注意", f"下落トレンド (5日{mom5:+.1f}%)", "conservative への切替えを検討"
        if not above:
            return "⚠️ 注意", "日経 < MA200 (長期下落)", "conservative を優先。aggressive は半数以下に絞る"
        if trend == "up" and mom5 >= 2.0 and mom20 >= 3.0 and above:
            return "✅ 推奨", f"上昇 × 5日{mom5:+.1f}% / 20日{mom20:+.1f}%", "最も効率が良い局面"
        if trend == "up":
            return "✅ 推奨", f"上昇トレンド (5日{mom5:+.1f}%)", "上昇継続なら標準運用"
        return "⚠️ 注意", f"横ばい (5日{mom5:+.1f}%)", "シグナル数が少ない可能性。conservative 併用推奨"
    else:
        if trend == "down" and vol == "high" and crash:
            return "⚠️ 注意", f"下落×高ボラ×急落 (最大1日{drop:+.1f}%)", "シグナルを精査。損切り連発リスクあり"
        if trend == "down" and vol == "high":
            return "⚠️ 注意", f"下落×高ボラ (Vol={r['vol']:.2f}%)", "ポジション縮小を検討"
        return "✅ 推奨", f"トレンド={trend} / ボラ={vol}", "全相場で使用可能"


def _set_params(mode: str) -> None:
    if mode == "conservative":
        _stop.STRATEGY_PARAMS.update(_CON_STOP)
        _brk.STRATEGY_PARAMS.update(_CON_BRK)
    else:
        _stop.STRATEGY_PARAMS.update(_AGG_STOP)
        _brk.STRATEGY_PARAMS.update(_AGG_BRK)


# ════════════════════════════════════════════════════════════════════════════
# バックテスト実行とトレンドマッピング
# ════════════════════════════════════════════════════════════════════════════

def _run_backtests(years: int, workers: int) -> dict[str, list[dict]]:
    """設定ごとに全WATCHLISTをバックテストし、トレードログを返す。"""
    result: dict[str, list[dict]] = {}
    for cfg in CONFIGS:
        _set_params(cfg["mode"])
        trades: list[dict] = []
        with _TPE(max_workers=workers) as ex:
            futs = {}
            for sym, name, strat in cfg["stop_wl"]:
                futs[ex.submit(_stop.backtest_one, sym, name, strat)] = None
            for sym, name, strat in cfg["brk_wl"]:
                futs[ex.submit(_brk.backtest_one, sym, name, strat)] = None
            for fut in _asc(futs):
                try:
                    r = fut.result()
                    if not r:
                        continue
                    period_results = r.get("period_results", {})
                    if not period_results:
                        continue
                    max_period = max(period_results.keys())
                    for t in period_results[max_period].get("trade_log", []):
                        if t.get("reason") in (None, "発注中"):
                            continue
                        entry_dt = t.get("entry_dt")
                        if entry_dt is None:
                            continue
                        trades.append({
                            "symbol":    r.get("symbol", ""),
                            "name":      r.get("name", ""),
                            "strategy":  r.get("strategy", ""),
                            "entry_dt":  entry_dt,
                            "exit_dt":   t.get("exit_dt"),
                            "pnl":       t.get("pnl", 0),
                            "reason":    t.get("reason", ""),
                            "entry_p":   t.get("entry_p", 0),
                            "exit_p":    t.get("exit_p", 0),
                            "hold_days": t.get("hold_days", 0),
                        })
                except Exception:
                    pass
        result[cfg["label"]] = trades
    _set_params("conservative")
    return result


def _add_nikkei_trend(trades_by_cfg: dict[str, list[dict]], nk_trend: pd.Series) -> None:
    """各トレードに entry_dt 時点の日経トレンドを追加（in-place）"""
    idx = nk_trend.index
    for trades in trades_by_cfg.values():
        for t in trades:
            entry_dt = t["entry_dt"]
            ts  = pd.Timestamp(entry_dt).normalize()
            pos = idx.searchsorted(ts, side="right") - 1
            if 0 <= pos < len(nk_trend):
                t["nk_trend"] = nk_trend.iloc[pos]
            else:
                t["nk_trend"] = "sideways"


# ════════════════════════════════════════════════════════════════════════════
# HTML ビルダー
# ════════════════════════════════════════════════════════════════════════════

def _signal_judgment_html(r: dict) -> str:
    """タブ1: 4設定シグナル判定"""
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[r["trend"]]
    trend_ja    = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}[r["trend"]]
    vol_color   = {"high": "#f87171", "mid": "#fbbf24", "low": "#4ade80"}[r["vol_level"]]
    vol_ja      = {"high": "高ボラ", "mid": "中ボラ", "low": "低ボラ"}[r["vol_level"]]
    ma200_str   = f"{r['ma200']:,.0f}" if r.get("ma200") else "N/A"
    ma200_color = "#4ade80" if r["above_ma200"] else "#f87171"
    ma200_pos   = f'<span style="color:{ma200_color}">{"▲ 上" if r["above_ma200"] else "▼ 下"}</span>'
    m5c  = "#4ade80" if r["mom5"]  >= 0 else "#f87171"
    m20c = "#4ade80" if r["mom20"] >= 0 else "#f87171"
    dc   = "#f87171" if r["max_1d_drop"] < -3 else "#94a3b8"

    regime_items = [
        ("日経225",          f'<strong style="font-size:1.25rem">{r["cur"]:,.0f}円</strong>'),
        ("トレンド",         f'<span style="color:{trend_color};font-weight:700;font-size:1.05rem">{trend_ja}</span>'),
        ("ボラ (14日)",      f'<span style="color:{vol_color}">{vol_ja} ({r["vol"]:.2f}%)</span>'),
        ("5日騰落",          f'<span style="color:{m5c};font-weight:600">{r["mom5"]:+.2f}%</span>'),
        ("20日騰落",         f'<span style="color:{m20c};font-weight:600">{r["mom20"]:+.2f}%</span>'),
        ("MA200",            f'{ma200_str}円 → {ma200_pos}'),
        ("過去30日最大下落", f'<span style="color:{dc}">{r["max_1d_drop"]:+.2f}%</span>'),
    ]
    regime_html = "".join(
        f'<div class="regime-item"><span class="ri-label">{lb}</span>'
        f'<span class="ri-val">{vl}</span></div>'
        for lb, vl in regime_items
    )

    cards_html = ""
    for cfg in CONFIGS:
        status, reason, advice = _judge_config(cfg, r)
        lbl_ja, fg, bg, border = STATUS_META[status]
        rc     = RISK_COLOR.get(cfg["risk"], "#94a3b8")
        n_stop = len(cfg["stop_wl"])
        n_brk  = len(cfg["brk_wl"])
        cards_html += f"""
<div class="script-card" style="border-color:{border};background:{bg}">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <span class="badge" style="background:{border};color:{fg}">{lbl_ja}</span>
    <span style="font-weight:700;font-size:1.05rem;color:{cfg['color']}">{cfg['label']}</span>
    <span style="color:#64748b;font-size:0.8rem">{cfg['mode']} / 逆指値B:{n_stop}件 BRK:{n_brk}件</span>
    <span style="margin-left:auto;font-size:0.78rem;color:{rc}">リスク: {cfg['risk']}</span>
  </div>
  <div style="color:#94a3b8;font-size:0.82rem;margin-top:8px">{reason}</div>
  <div style="color:#64748b;font-size:0.78rem;margin-top:4px">→ {advice}</div>
  <div style="color:#475569;font-size:0.75rem;margin-top:6px;border-top:1px solid #1e293b;padding-top:6px">{cfg['note']}</div>
</div>"""

    return f"""
<div class="regime-box">
  <h3 style="margin-top:0;color:#e2e8f0">相場環境</h3>
  <div class="regime-grid">{regime_html}</div>
</div>
<h2>4設定シグナル判定</h2>
{cards_html}"""


def _trend_analysis_html(trades_by_cfg: dict[str, list[dict]]) -> str:
    """タブ2: トレンド×設定相性テーブル"""
    TREND_LABELS = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}
    TREND_COLORS = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}

    def _agg(trades):
        n = len(trades)
        if n == 0:
            return None
        wins  = [t for t in trades if t["pnl"] > 0]
        loses = [t for t in trades if t["pnl"] < 0]
        gp    = sum(t["pnl"] for t in wins)
        gl    = abs(sum(t["pnl"] for t in loses))
        pf    = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s  = "∞" if pf == float("inf") else f"{pf:.2f}"
        avg   = sum(t["pnl"] for t in trades) / n
        wr    = len(wins) / n * 100
        return {"n": n, "wr": wr, "pf": pf, "pf_s": pf_s, "avg": avg}

    def _cell(agg):
        if agg is None:
            return '<td style="color:#475569;text-align:center">—</td>'
        wr_c  = "#4ade80" if agg["wr"] >= 55 else ("#fbbf24" if agg["wr"] >= 45 else "#f87171")
        pf_c  = "#4ade80" if agg["pf"] >= 1.5 else ("#fbbf24" if agg["pf"] >= 1.0 else "#f87171")
        avg_c = "#4ade80" if agg["avg"] >= 0 else "#f87171"
        return (f'<td style="text-align:center;padding:8px 10px">'
                f'<div style="font-size:0.75rem;color:#94a3b8">{agg["n"]}取引</div>'
                f'<div><span style="color:{wr_c};font-weight:700">{agg["wr"]:.0f}%</span>'
                f' <span style="font-size:0.72rem;color:#64748b">勝率</span></div>'
                f'<div><span style="color:{pf_c};font-weight:700">PF {agg["pf_s"]}</span></div>'
                f'<div style="color:{avg_c};font-size:0.78rem">{agg["avg"]:+,.0f}円/取引</div>'
                f'</td>')

    rows = ""
    for trend in ["up", "sideways", "down"]:
        tc  = TREND_COLORS[trend]
        tl  = TREND_LABELS[trend]
        row = f'<tr><td style="color:{tc};font-weight:700;white-space:nowrap;padding:8px 12px">{tl}</td>'
        for cfg in CONFIGS:
            t_list = [t for t in trades_by_cfg.get(cfg["label"], []) if t.get("nk_trend") == trend]
            row += _cell(_agg(t_list))
        row += "</tr>"
        rows += row

    total_row = '<tr style="border-top:2px solid #334155"><td style="color:#94a3b8;padding:8px 12px;font-weight:700">全期間合計</td>'
    for cfg in CONFIGS:
        total_row += _cell(_agg(trades_by_cfg.get(cfg["label"], [])))
    total_row += "</tr>"

    headers = "".join(
        f'<th style="color:{c["color"]};padding:10px 12px">{c["label"]}</th>'
        for c in CONFIGS
    )

    return f"""
<h2>トレンド × 設定相性</h2>
<p style="color:#94a3b8;font-size:0.85rem">
  各セルは日経平均が「そのトレンド」だった日にエントリーしたトレードの成績。
  <span style="color:#4ade80">緑</span>: 勝率55%以上 / PF1.5以上、
  <span style="color:#fbbf24">黄</span>: 勝率45〜55% / PF1〜1.5、
  <span style="color:#f87171">赤</span>: それ以下。
</p>
<div style="overflow-x:auto">
<table>
  <thead>
    <tr>
      <th style="text-align:left;padding:10px 12px">日経トレンド</th>
      {headers}
    </tr>
  </thead>
  <tbody>
    {rows}
    {total_row}
  </tbody>
</table>
</div>
<h2>設定別 トレンド構成比</h2>
{_trend_breakdown_html(trades_by_cfg)}"""


def _trend_breakdown_html(trades_by_cfg: dict[str, list[dict]]) -> str:
    """各設定のエントリー分布（トレンド別）をバーで表示"""
    TREND_COLORS = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}
    TREND_JA     = {"up": "上昇", "down": "下落", "sideways": "横ばい"}

    cards = ""
    for cfg in CONFIGS:
        trades = trades_by_cfg.get(cfg["label"], [])
        if not trades:
            continue
        by_trend = defaultdict(list)
        for t in trades:
            by_trend[t.get("nk_trend", "sideways")].append(t)
        total = len(trades)
        bars  = ""
        for trend in ["up", "sideways", "down"]:
            n    = len(by_trend[trend])
            pct  = n / total * 100 if total > 0 else 0
            wins = sum(1 for t in by_trend[trend] if t["pnl"] > 0)
            wr   = wins / n * 100 if n > 0 else 0
            tc   = TREND_COLORS[trend]
            bars += f"""
<div style="margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;margin-bottom:4px">
    <span style="color:{tc};font-size:0.82rem;font-weight:600">{TREND_JA[trend]}</span>
    <span style="font-size:0.78rem;color:#94a3b8">{n}件 ({pct:.0f}%) / 勝率{wr:.0f}%</span>
  </div>
  <div style="background:#1e293b;border-radius:4px;height:8px;overflow:hidden">
    <div style="background:{tc};height:100%;width:{pct:.1f}%;border-radius:4px"></div>
  </div>
</div>"""
        cards += f"""
<div style="background:#0d1424;border:1px solid #1e3a5f;border-radius:10px;padding:16px;flex:1;min-width:220px">
  <div style="color:{cfg['color']};font-weight:700;margin-bottom:12px">{cfg['label']}</div>
  <div style="font-size:0.75rem;color:#64748b;margin-bottom:8px">合計 {total}取引</div>
  {bars}
</div>"""

    return f'<div style="display:flex;flex-wrap:wrap;gap:12px">{cards}</div>'


def _pnl_summary_html(trades_by_cfg: dict[str, list[dict]], days: int) -> str:
    """タブ3: 損益サマリー"""
    until = _TODAY
    since = until - timedelta(days=days)

    rows = ""
    for cfg in CONFIGS:
        trades = [t for t in trades_by_cfg.get(cfg["label"], [])
                  if t.get("exit_dt") is not None
                  and (t["exit_dt"].date() if hasattr(t["exit_dt"], "date") else t["exit_dt"]) >= since]
        n    = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        gp   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pnl  = sum(t["pnl"] for t in trades)
        wr   = wins / n * 100 if n else 0.0
        pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        pnl_c = "profit" if pnl >= 0 else "loss"
        wr_c  = "#4ade80" if wr >= 55 else ("#fbbf24" if wr >= 45 else "#f87171")
        rows += f"""
<tr>
  <td style="color:{cfg['color']};font-weight:700">{cfg['label']}</td>
  <td style="text-align:center">{cfg['mode']}</td>
  <td style="text-align:center">{n}</td>
  <td style="text-align:center"><span style="color:{wr_c}">{wr:.0f}%</span></td>
  <td style="text-align:center">{pf_s}</td>
  <td style="text-align:right" class="{pnl_c}">{pnl:+,.0f}円</td>
</tr>"""

    return f"""
<h2>直近{days}日 損益サマリー</h2>
<div style="overflow-x:auto">
<table>
  <thead>
    <tr>
      <th style="text-align:left">設定</th>
      <th>モード</th>
      <th>取引数</th>
      <th>勝率</th>
      <th>PF</th>
      <th>損益合計</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
</div>
<p style="color:#64748b;font-size:0.78rem">
  ※ 各設定は同一銘柄×戦略を含むため、取引数の単純合計はシグナル数より多くなります。
</p>"""


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background:#0a0f1e; color:#e2e8f0; font-family:'Helvetica Neue',Arial,sans-serif;
       padding:20px; max-width:1100px; margin:0 auto; font-size:14px; line-height:1.6; }
h1 { font-size:1.6rem; color:#f1f5f9; margin-bottom:6px; }
h2 { font-size:1.15rem; color:#cbd5e1; margin:20px 0 10px; border-left:3px solid #3b82f6;
     padding-left:10px; }
h3 { font-size:1rem; color:#94a3b8; }
.subtitle { color:#64748b; font-size:0.85rem; margin-bottom:16px; }
.tab-nav { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px; border-bottom:1px solid #1e293b;
           padding-bottom:10px; }
.tab-btn { background:#1e293b; border:none; color:#94a3b8; padding:8px 16px; border-radius:6px;
           cursor:pointer; font-size:0.85rem; transition:all .15s; }
.tab-btn:hover { background:#334155; color:#e2e8f0; }
.tab-btn.active { background:#3b82f6; color:#fff; font-weight:600; }
.tab-pane { display:none; }
.tab-pane.active { display:block; }
.regime-box { background:#0d1424; border:1px solid #1e3a5f; border-radius:10px;
              padding:18px; margin-bottom:16px; }
.regime-grid { display:flex; flex-wrap:wrap; gap:12px; }
.regime-item { display:flex; flex-direction:column; min-width:120px; }
.ri-label { font-size:0.72rem; color:#64748b; }
.ri-val   { font-size:0.95rem; font-weight:600; }
.script-card { border:1px solid; border-radius:10px; padding:14px 18px; margin-bottom:10px; }
.script-card:hover { filter:brightness(1.08); }
.badge { display:inline-block; padding:2px 10px; border-radius:99px; font-size:0.78rem; font-weight:700; }
table { width:100%; border-collapse:collapse; font-size:0.83rem; margin-bottom:8px; }
th { background:#1e293b; color:#94a3b8; padding:7px 10px; border:1px solid #334155;
     text-align:center; white-space:nowrap; }
td { padding:5px 10px; border:1px solid #1e293b; }
tr:hover td { filter:brightness(1.15); }
.profit { color:#4ade80; font-weight:700; }
.loss   { color:#f87171; font-weight:700; }
"""

JS = """
function switchTab(id) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelector('[data-tab="'+id+'"]').classList.add('active');
  document.getElementById(id).classList.add('active');
}
"""


def build_html(r: dict, trades_by_cfg: dict, nk_trend: pd.Series, close: pd.Series,
               years: int, ref_date, days: int, show_pnl: bool) -> str:
    trend_color = {"up": "#4ade80", "down": "#f87171", "sideways": "#fbbf24"}[r["trend"]]
    trend_ja    = {"up": "上昇 ▲", "down": "下落 ▼", "sideways": "横ばい →"}[r["trend"]]

    tab1 = _signal_judgment_html(r)
    tab2 = _trend_analysis_html(trades_by_cfg)
    tab3 = _pnl_summary_html(trades_by_cfg, days) if show_pnl else ""

    pnl_btn  = '<button class="tab-btn" data-tab="t3" onclick="switchTab(\'t3\')">💹 損益サマリー</button>' if show_pnl else ""
    pnl_pane = f'<div id="t3" class="tab-pane">{tab3}</div>' if show_pnl else ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日経平均 4設定分析 — {ref_date}</title>
<style>{CSS}</style>
</head>
<body>
<h1>日経平均 4設定分析レポート</h1>
<p class="subtitle">
  基準日: {ref_date} ／ 分析期間: {close.index[0].date()} 〜 {close.index[-1].date()} (過去{years}年) ／
  現在: <strong style="color:{trend_color}">{trend_ja} {r['cur']:,.0f}円</strong>
</p>
<div class="tab-nav">
  <button class="tab-btn active" data-tab="t1" onclick="switchTab('t1')">📊 シグナル判定</button>
  <button class="tab-btn"        data-tab="t2" onclick="switchTab('t2')">📈 トレンド×設定相性</button>
  {pnl_btn}
</div>
<div id="t1" class="tab-pane active">{tab1}</div>
<div id="t2" class="tab-pane">{tab2}</div>
{pnl_pane}
<script>{JS}</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════════════════
# 銘柄×戦略 個別履歴確認
# ════════════════════════════════════════════════════════════════════════════

def _build_history_html(symbol: str, name: str, strategy: str, days: int, mode: str,
                        trade_log: list) -> str:
    wins      = sum(1 for t in trade_log if t.get("pnl", 0) > 0)
    losses    = len(trade_log) - wins
    total_pnl = sum(t.get("pnl", 0) for t in trade_log)
    gross_p   = sum(t["pnl"] for t in trade_log if t.get("pnl", 0) > 0)
    gross_l   = sum(t["pnl"] for t in trade_log if t.get("pnl", 0) <= 0)
    wr        = wins / len(trade_log) * 100 if trade_log else 0
    pf        = abs(gross_p / gross_l) if gross_l != 0 else float("inf")
    avg       = total_pnl / len(trade_log) if trade_log else 0
    pf_str    = f"{pf:.2f}" if pf != float("inf") else "∞"

    reasons: dict[str, int] = {}
    for t in trade_log:
        r = t.get("reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    reason_chips = "".join(
        f'<span class="chip">{k}: {v}件</span>'
        for k, v in sorted(reasons.items())
    )

    rows = []
    for t in sorted(trade_log, key=lambda x: str(x.get("exit_dt", "")), reverse=True):
        pnl = t.get("pnl", 0)
        rsn = t.get("reason", "?")
        cls = "win" if pnl > 0 else "loss"
        rsn_cls = {"target": "tag-target", "stop": "tag-stop", "timeout": "tag-timeout"}.get(rsn, "")
        rows.append(f"""
        <tr class="{cls}">
          <td>{str(t.get("exit_dt","?"))[:10]}</td>
          <td>{str(t.get("signal_dt","?"))[:10]}</td>
          <td class="num">{t.get("entry_p",0):,.0f}</td>
          <td class="num">{t.get("exit_p",0):,.0f}</td>
          <td class="num">{t.get("qty",0):,}</td>
          <td class="num">{t.get("hold_days",0)}</td>
          <td class="num pnl">{pnl:+,.0f}円</td>
          <td><span class="tag {rsn_cls}">{rsn}</span></td>
        </tr>""")

    rows_html  = "\n".join(rows)
    mode_badge = "aggressive" if mode == "aggressive" else "conservative"
    mode_color = "#e74c3c" if mode == "aggressive" else "#3498db"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{symbol} × {strategy} バックテスト履歴</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Helvetica Neue",Arial,"Hiragino Kaku Gothic ProN",sans-serif;
          background:#0f172a; color:#e2e8f0; padding:24px; }}
  h1 {{ font-size:1.5rem; margin-bottom:4px; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:24px; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:12px;
            font-size:0.75rem; font-weight:bold; color:#fff;
            background:{mode_color}; margin-left:8px; vertical-align:middle; }}
  .cards {{ display:flex; gap:16px; flex-wrap:wrap; margin-bottom:24px; }}
  .card {{ background:#1e293b; border-radius:10px; padding:16px 24px; min-width:140px; }}
  .card .label {{ font-size:0.75rem; color:#64748b; margin-bottom:4px; }}
  .card .value {{ font-size:1.4rem; font-weight:bold; }}
  .card .value.pos {{ color:#22c55e; }}
  .card .value.neg {{ color:#ef4444; }}
  .chip {{ display:inline-block; background:#334155; border-radius:6px;
           padding:2px 8px; font-size:0.78rem; margin:2px; }}
  .reasons {{ margin-bottom:20px; color:#94a3b8; font-size:0.85rem; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.88rem; }}
  th {{ background:#1e293b; padding:8px 12px; text-align:left;
        color:#94a3b8; font-weight:600; border-bottom:1px solid #334155; position:sticky; top:0; }}
  td {{ padding:7px 12px; border-bottom:1px solid #1e293b; }}
  tr.win  td {{ background:#0f2a1a; }}
  tr.loss td {{ background:#2a0f0f; }}
  tr:hover td {{ filter:brightness(1.15); }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .pnl {{ font-weight:bold; }}
  tr.win  .pnl {{ color:#22c55e; }}
  tr.loss .pnl {{ color:#ef4444; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:4px;
          font-size:0.75rem; font-weight:bold; }}
  .tag-target  {{ background:#14532d; color:#86efac; }}
  .tag-stop    {{ background:#450a0a; color:#fca5a5; }}
  .tag-timeout {{ background:#27272a; color:#a1a1aa; }}
  .footer {{ margin-top:20px; color:#64748b; font-size:0.82rem; text-align:right; }}
</style>
</head>
<body>
<h1>{name} ({symbol}) × {strategy} <span class="badge">{mode_badge}</span></h1>
<div class="subtitle">直近 {days} 日間のバックテスト履歴</div>

<div class="cards">
  <div class="card">
    <div class="label">取引数</div>
    <div class="value">{len(trade_log)} 件</div>
    <div style="font-size:0.8rem;color:#64748b">{wins}勝 / {losses}敗</div>
  </div>
  <div class="card">
    <div class="label">勝率</div>
    <div class="value {'pos' if wr >= 50 else 'neg'}">{wr:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">PF</div>
    <div class="value {'pos' if pf >= 1.0 else 'neg'}">{pf_str}</div>
  </div>
  <div class="card">
    <div class="label">合計損益</div>
    <div class="value {'pos' if total_pnl >= 0 else 'neg'}">{total_pnl:+,.0f}円</div>
  </div>
  <div class="card">
    <div class="label">平均損益</div>
    <div class="value {'pos' if avg >= 0 else 'neg'}">{avg:+,.0f}円</div>
  </div>
</div>

<div class="reasons">決済理由: {reason_chips}</div>

<table>
  <thead>
    <tr>
      <th>決済日</th><th>シグナル日</th>
      <th class="num">約定値</th><th class="num">決済値</th>
      <th class="num">株数</th><th class="num">保有日</th>
      <th class="num">損益</th><th>決済理由</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>

<div class="footer">合計: {total_pnl:+,.0f}円 / {len(trade_log)}取引 &nbsp;|&nbsp; 生成: {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>
</body>
</html>"""


def _run_symbol_history(symbol: str, strategy: str, days: int, mode: str,
                        open_browser: bool = True) -> None:
    from backtest_limit_entry import fetch, run_limit_backtest

    if mode == "aggressive":
        stop_params = _stop.STRATEGY_PARAMS_AGGRESSIVE
        brk_params  = _brk.STRATEGY_PARAMS_AGGRESSIVE
    else:
        stop_params = _stop.STRATEGY_PARAMS_CONSERVATIVE
        brk_params  = _brk.STRATEGY_PARAMS_CONSERVATIVE
    all_params: dict = {**stop_params, **brk_params}

    if strategy not in all_params:
        print(f"ERROR: 戦略 '{strategy}' は不明です。利用可能: {', '.join(all_params)}")
        sys.exit(1)

    print(f"データ取得中: {symbol} ...")
    df = fetch(symbol, days + 60)
    if df is None or df.empty:
        print(f"ERROR: {symbol} のデータを取得できませんでした。")
        sys.exit(1)

    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        name = info.get("longName") or info.get("shortName") or symbol
    except Exception:
        name = symbol

    calc_fn, em, sm, tm = all_params[strategy]
    print("バックテスト実行中...")
    result = run_limit_backtest(
        symbol=symbol, name=name, df=df, calc_fn=calc_fn,
        entry_atr_mult=em, stop_atr_mult=sm, target_atr_mult=tm,
        backtest_days=days, strategy_name=strategy, entry_type="stop",
    )

    trade_log = result.get("trade_log", [])
    if not trade_log:
        print("この期間にトレードはありませんでした。")
        return

    html      = _build_history_html(symbol, name, strategy, days, mode, trade_log)
    date_str  = datetime.now().strftime("%Y-%m-%d")
    mode_sfx  = "_aggressive" if mode == "aggressive" else ""
    out_path  = Path(f"history_{symbol}_{strategy}{mode_sfx}_{date_str}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"HTML生成完了: {out_path.resolve()}")
    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())


# ════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="日経平均 4設定分析レポート / 銘柄別履歴確認")
    parser.add_argument("--years",      type=int,  default=5,    help="分析期間（年）")
    parser.add_argument("--date",       type=str,  default=None, help="基準日 YYYY-MM-DD")
    parser.add_argument("--days",       type=int,  default=90,   help="損益集計日数 / 銘柄履歴日数")
    parser.add_argument("--no-browser", action="store_true",     help="HTML生成のみ")
    parser.add_argument("--no-pnl",     action="store_true",     help="損益タブなし")
    parser.add_argument("--workers",    type=int,  default=_DEF_WORKERS, help="並列数")
    parser.add_argument("--symbol",     type=str,  default=None, help="銘柄コード (例: 8387.T)")
    parser.add_argument("--strategy",   type=str,  default=None, help="戦略名 (例: A7, RSI2, MACD)")
    parser.add_argument("--aggressive", action="store_true",     help="aggressive モード (--symbol 時のみ)")
    parser.add_argument("--mode",       choices=["conservative", "aggressive"], default=None)
    args = parser.parse_args()

    # 銘柄×戦略の個別履歴確認モード
    if args.symbol and args.strategy:
        if args.mode == "aggressive" or args.aggressive:
            mode = "aggressive"
        else:
            mode = os.environ.get("TRADING_MODE", "conservative")
        _run_symbol_history(
            symbol=args.symbol.upper(),
            strategy=args.strategy.upper(),
            days=args.days,
            mode=mode,
            open_browser=not args.no_browser,
        )
        return

    # 日経4設定分析モード
    if args.date:
        try:
            ref_date = _date_type.fromisoformat(args.date)
        except ValueError:
            print(f"[ERROR] --date の形式が不正: {args.date}")
            return
        if ref_date > _TODAY:
            print(f"[ERROR] 未来の日付は指定できません: {ref_date}")
            return
    else:
        ref_date = _TODAY

    print(f"日経平均 4設定分析 (基準日: {ref_date} / 過去{args.years}年)...", flush=True)

    close = _na.fetch_n225(args.years, end_date=ref_date if args.date else None)
    if args.date:
        close = close[close.index <= pd.Timestamp(ref_date)]
    if close.empty:
        print(f"[ERROR] {ref_date} 時点のデータが取得できません")
        return

    r        = _na.get_regime(close)
    nk_trend = _na.label_trend(close)

    print("バックテスト実行中...", flush=True)
    trades_by_cfg = _run_backtests(years=args.years, workers=args.workers)
    _add_nikkei_trend(trades_by_cfg, nk_trend)

    html = build_html(
        r=r, trades_by_cfg=trades_by_cfg, nk_trend=nk_trend, close=close,
        years=args.years, ref_date=ref_date, days=args.days, show_pnl=not args.no_pnl,
    )

    date_str = str(ref_date)
    out_path = Path(f"nikkei_4config_{date_str}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"\n4設定レポート生成完了: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
