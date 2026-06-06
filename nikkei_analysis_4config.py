"""
nikkei_analysis_4config.py  ―  4config 統合レポート

以下の4configを1枚のHTMLで比較分析する:
  v2新WL conservative  : 2026-06-04 WFスキャン (全価格帯, tm=3.0)
  v2新WL aggressive    : 2026-06-05 WFスキャン (全価格帯, tm=2.0)
  5k-WL conservative   : 2026-06-06 WFスキャン (≤5000円,  tm=3.0)
  5k-WL aggressive     : 2026-06-06 WFスキャン (≤5000円,  tm=2.0)

nikkei_analysis.py 本体は一切変更しない。

使い方:
  python nikkei_analysis_4config.py                    # 過去365日 HTML生成 & ブラウザ表示
  python nikkei_analysis_4config.py --days 180         # 直近180日
  python nikkei_analysis_4config.py --no-browser       # HTML生成のみ
  python nikkei_analysis_4config.py --date 2026-01-01  # 指定日シグナル確認
"""
from __future__ import annotations

import argparse
import importlib as _importlib
import os
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop as _stop
import check_signals_breakout as _brk

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
    ("1975.T", "朝日工業社",                         "VOL"),   # PF6.55 WR90.0% DD=7.4%  Shrp3.57   9取引
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
    ("6501.T", "日立製作所",                             "RSI2"),
    ("6237.T", "イワキポンプ",                           "RSI2"),
    ("2540.T", "養命酒製造",                             "RSI2"),
    ("8393.T", "宮崎銀行",                               "RSI2"),
    # ── MACD (2026-06-04, conservative, 全価格帯) ──
    ("6278.T", "ユニオンツール",                         "MACD"),
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
    ("6875.T", "メガチップス",                           "DON"),
    ("1515.T", "日鉄鉱業",                               "DON"),
    # ── VOL (2026-06-04, conservative, 全価格帯) ──
    ("4461.T", "第一工業製薬",                           "VOL"),
    ("7013.T", "ＩＨＩ",                                 "VOL"),
    ("4390.T", "ＩＰＳ",                                 "VOL"),
    ("9887.T", "松屋フーズホールディングス",             "VOL"),
    ("6284.T", "日精エー・エス・ビー機械",               "VOL"),
    ("1803.T", "清水建設",                               "VOL"),
    ("1945.T", "東京エネシス",                           "VOL"),
    ("9843.T", "ニトリホールディングス",                 "VOL"),
    ("6952.T", "カシオ計算機",                           "VOL"),
]

# ══════════════════════════════════════════════════════════════════════════════
# v2新WL aggressive  ―  2026-06-05 WFスキャン (全価格帯, tm=2.0)
# ══════════════════════════════════════════════════════════════════════════════
_V2_STOP_AGG: list[tuple[str, str, str]] = [
    # ── RSI2 (2026-06-05, aggressive, 全価格帯) ──
    ("6857.T", "アドバンテスト",                         "RSI2"),
    ("8014.T", "蝶理",                                   "RSI2"),
    ("4631.T", "ＤＩＣ",                                 "RSI2"),
    ("4004.T", "レゾナック・ホールディングス",           "RSI2"),
    ("8316.T", "三井住友フィナンシャルグループ",         "RSI2"),
    ("2540.T", "養命酒製造",                             "RSI2"),
    ("8061.T", "西華産業",                               "RSI2"),
    ("6770.T", "アルプスアルパイン",                     "RSI2"),
    ("5821.T", "平河ヒューテック",                       "RSI2"),
    ("6788.T", "日本トリム",                             "RSI2"),
    # ── A7 (2026-06-05, aggressive, 全価格帯) ──
    ("7003.T", "三井Ｅ＆Ｓ",                             "A7"),
    ("4578.T", "大塚ホールディングス",                   "A7"),
    ("6875.T", "メガチップス",                           "A7"),
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
    ("1975.T", "朝日工業社",                             "DON"),
    ("7419.T", "ノジマ",                                 "DON"),
    ("9830.T", "トラスコ中山",                           "DON"),
    # ── VOL (2026-06-05, aggressive, 全価格帯) ──
    ("7013.T", "ＩＨＩ",                                 "VOL"),
    ("1975.T", "朝日工業社",                             "VOL"),
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
    ("7013.T", "ＩＨＩ",                                 "MOM"),
    ("8011.T", "三陽商会",                               "MOM"),
    ("1975.T", "朝日工業社",                             "MOM"),
    ("8016.T", "オンワードホールディングス",             "MOM"),
]

# ──────────────────────────────────────────────────────────────────────────────
print("=" * 70)
print("nikkei_analysis_4config: 4config 統合レポート")
print(f"  v2新WL conservative  逆指値B: {len(_V2_STOP_CON)}  BRK: {len(_V2_BRK_CON)}")
print(f"  v2新WL aggressive    逆指値B: {len(_V2_STOP_AGG)}  BRK: {len(_V2_BRK_AGG)}")
print(f"  5k-WL conservative   逆指値B: {len(_5K_STOP_CON)}  BRK: {len(_5K_BRK_CON)}")
print(f"  5k-WL aggressive     逆指値B: {len(_5K_STOP_AGG)}  BRK: {len(_5K_BRK_AGG)}")
print("=" * 70)

# ── reload フック: conservative シグナルタブは 5k-WL conservative を使用 ──
_orig_reload = _importlib.reload

def _watchlist_preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST = list(_5K_STOP_CON)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST = list(_5K_BRK_CON)
    return result

_importlib.reload = _watchlist_preserving_reload

import nikkei_analysis as _na  # noqa: E402

_importlib.reload = _orig_reload

# ── _PNL_CONFIGS を 4config に差し替え ────────────────────────────────────────
_na._PNL_CONFIGS[:] = [
    {
        "label":   "v2新WL conservative",
        "color":   "#06b6d4",
        "mode":    "conservative",
        "sm_tm":   None,
        "stop_wl": list(_V2_STOP_CON),
        "brk_wl":  list(_V2_BRK_CON),
    },
    {
        "label":   "v2新WL aggressive",
        "color":   "#f39c12",
        "mode":    "aggressive",
        "sm_tm":   None,
        "stop_wl": list(_V2_STOP_AGG),
        "brk_wl":  list(_V2_BRK_AGG),
    },
    {
        "label":   "5k-WL conservative",
        "color":   "#3498db",
        "mode":    "conservative",
        "sm_tm":   None,
        "stop_wl": list(_5K_STOP_CON),
        "brk_wl":  list(_5K_BRK_CON),
    },
    {
        "label":   "5k-WL aggressive",
        "color":   "#e74c3c",
        "mode":    "aggressive",
        "sm_tm":   None,
        "stop_wl": list(_5K_STOP_AGG),
        "brk_wl":  list(_5K_BRK_AGG),
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--date",       type=str,  default=None)
    _p.add_argument("--no-browser", action="store_true")
    _known, _ = _p.parse_known_args()

    _orig_argv = list(sys.argv)

    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")

    _na.main()

    sys.argv[:] = _orig_argv

    JST      = timezone(timedelta(hours=9))
    date_str = _known.date if _known.date else str(datetime.now(JST).date())
    old_path = Path(f"nikkei_analysis_{date_str}.html")
    new_path = Path(f"nikkei_analysis_4config_{date_str}.html")

    if old_path.exists():
        old_path.replace(new_path)
        print(f"\n4config レポート生成完了: {new_path.resolve()}")
        if not _known.no_browser:
            webbrowser.open(new_path.resolve().as_uri())
    else:
        print(f"[WARN] {old_path} が見つかりません (--date 指定時は日付を確認)")


if __name__ == "__main__":
    main()
