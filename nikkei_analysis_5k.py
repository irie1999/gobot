"""
nikkei_analysis_5k.py  ―  5000円以下WF選定WATCHLIST (2026-06-06) で日経分析を実行

conservative: check_signals_stop/breakout の現WATCHLIST (conservative WFスキャン選定)
aggressive  : 別途 scan_walkforward.py --aggressive で選定した専用WATCHLIST

nikkei_analysis.py 本体は一切変更しない。

出力: nikkei_analysis_5k_{date}.html

使い方:
  python nikkei_analysis_5k.py                    # 過去5年 HTML生成 & ブラウザ表示
  python nikkei_analysis_5k.py --years 10         # 過去10年
  python nikkei_analysis_5k.py --days 365         # 損益集計365日
  python nikkei_analysis_5k.py --no-browser       # HTML生成のみ
  python nikkei_analysis_5k.py --no-signals       # シグナルタブなし
  python nikkei_analysis_5k.py --no-pnl           # 損益タブなし
  python nikkei_analysis_5k.py --5k-only          # conservative + aggressive の2configのみ表示
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

os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop as _stop
import check_signals_breakout as _brk

# ── conservative WATCHLIST (conservative WFスキャン選定, 2026-06-06) ──────────
#   check_signals_stop.py / check_signals_breakout.py の現 WATCHLIST をそのまま使用
_STOP_WL_CON: list[tuple[str, str, str]] = list(_stop.WATCHLIST)
_BRK_WL_CON:  list[tuple[str, str, str]] = list(_brk.WATCHLIST)

# ── aggressive WATCHLIST (aggressive WFスキャン選定, 2026-06-06, max-price≤5000) ─
#   scan_walkforward.py --aggressive --max-price 5000 の上位結果
#   tm=2.0 (目標+6%) で選定。小さな値動きで確実に目標到達する銘柄を優先。

_STOP_WL_AGG: list[tuple[str, str, str]] = [
    # ── RSI2: aggressive WF選定 (2026-06-06, max-price≤5000) ──
    ("8061.T", "西華産業",                           "RSI2"),  # PF6.81 WR93.8% DD=9.6%  Shrp4.01  17取引
    ("2540.T", "養命酒製造",                         "RSI2"),  # PF10.0 WR70.8% DD=1.1%  Shrp5.41  10取引
    ("6770.T", "アルプスアルパイン",                 "RSI2"),  # PF6.36 WR90.0% DD=8.2%  Shrp3.98   9取引
    ("8387.T", "四国銀行",                           "RSI2"),  # PF5.67 WR83.3% DD=6.7%  Shrp3.58  11取引
    ("1930.T", "北陸電気工事",                       "RSI2"),  # PF5.95 WR75.0% DD=5.6%  Shrp3.85   6取引
    ("5821.T", "平河ヒューテック",                   "RSI2"),  # PF2.92 WR73.3% DD=10.1% Shrp2.20   8取引
    ("8070.T", "東京産業",                           "RSI2"),  # PF4.04 WR75.0% DD=6.1%  Shrp2.84   9取引
    ("6788.T", "日本トリム",                         "RSI2"),  # PF5.65 WR72.2% DD=6.7%  Shrp1.85  15取引
    ("7167.T", "めぶきフィナンシャルグループ",       "RSI2"),  # PF5.79 WR83.3% DD=14.4% Shrp1.93   8取引
    ("4631.T", "ＤＩＣ",                             "RSI2"),  # PF10.0 WR100%  DD=0.0%  Shrp25.1   7取引 ※少取引注意
    # ── MACD: aggressive WF選定 (2026-06-06, max-price≤5000) ──
    ("8386.T", "百十四銀行",                         "MACD"),  # PF6.75 WR92.9% DD=7.0%  Shrp4.69  16取引
    ("7322.T", "三十三フィナンシャルグループ",       "MACD"),  # PF8.75 WR94.4% DD=7.6%  Shrp5.69  14取引
    ("1417.T", "ミライト・ワン",                     "MACD"),  # PF6.89 WR92.9% DD=5.6%  Shrp4.62  14取引
    ("7181.T", "かんぽ生命保険",                     "MACD"),  # PF4.03 WR75.6% DD=6.0%  Shrp3.19  16取引
    ("8387.T", "四国銀行",                           "MACD"),  # PF5.08 WR85.0% DD=12.2% Shrp2.55  20取引
    ("8877.T", "エスリード",                         "MACD"),  # PF3.54 WR80.1% DD=12.3% Shrp2.54  19取引
    ("9024.T", "西武ホールディングス",               "MACD"),  # PF3.02 WR77.1% DD=9.0%  Shrp2.47  11取引
    ("1975.T", "朝日工業社",                         "MACD"),  # PF3.16 WR75.0% DD=13.8% Shrp2.02  20取引
    ("4205.T", "日本ゼオン",                         "MACD"),  # PF10.0 WR100%  DD=0.0%  Shrp13.2  10取引 ※少取引注意
    ("6914.T", "オプテックスグループ",               "MACD"),  # PF2.48 WR77.3% DD=14.7% Shrp1.88  22取引
    # ── A7: aggressive WF選定 (2026-06-06, max-price≤5000) ──
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

_BRK_WL_AGG: list[tuple[str, str, str]] = [
    # ── DON: aggressive WF選定 (2026-06-06, max-price≤5000) ──
    ("3659.T", "ネクソン",                           "DON"),   # PF6.70 WR83.3% DD=9.7%  Shrp2.85  18取引
    # ── MOM: aggressive WF選定 (2026-06-06, max-price≤5000) ──
    ("8237.T", "松屋",                               "MOM"),   # PF4.58 WR85.8% DD=7.8%  Shrp3.35  35取引
    ("7242.T", "カヤバ",                             "MOM"),   # PF6.52 WR91.7% DD=7.4%  Shrp3.09  29取引
    ("8016.T", "オンワードホールディングス",         "MOM"),   # PF5.81 WR88.7% DD=9.0%  Shrp3.78  26取引
    ("1975.T", "朝日工業社",                         "MOM"),   # PF3.90 WR71.9% DD=8.7%  Shrp2.17  22取引
    ("8011.T", "三陽商会",                           "MOM"),   # PF4.74 WR69.3% DD=11.3% Shrp2.21  19取引
    # ── VOL: aggressive WF選定 (2026-06-06, max-price≤5000) ──
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

print("=" * 65)
print("nikkei_analysis_5k: 5000円以下WF WATCHLIST (2026-06-06 選定)")
print(f"  conservative 逆指値B: {len(_STOP_WL_CON)} 銘柄×戦略")
print(f"  conservative BRK    : {len(_BRK_WL_CON)} 銘柄×戦略")
print(f"  aggressive   逆指値B: {len(_STOP_WL_AGG)} 銘柄×戦略")
print(f"  aggressive   BRK    : {len(_BRK_WL_AGG)} 銘柄×戦略")
print("=" * 65)

# ── importlib.reload フック ───────────────────────────────────────────────────
#   nikkei_analysis.py は TRADING_MODE 切替のために _stop/_brk を reload() する。
#   reload 後も conservative WL を維持する (aggressive WL は PNL_CONFIGS で個別指定)
_orig_reload = _importlib.reload

def _wl_preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST = list(_STOP_WL_CON)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST = list(_BRK_WL_CON)
    return result

_importlib.reload = _wl_preserving_reload

import nikkei_analysis as _na  # noqa: E402

_importlib.reload = _orig_reload

# ── PNL タブのラベルと WATCHLIST を 5k 用に更新 ───────────────────────────────
#   conservative: conservative WFスキャン選定
#   aggressive  : aggressive WFスキャン選定 (別銘柄構成・別パラメータ)
for _cfg in _na._PNL_CONFIGS:
    if _cfg.get("label") == "既存版 conservative":
        _cfg["label"]   = "5k-WL conservative"
        _cfg["stop_wl"] = list(_STOP_WL_CON)
        _cfg["brk_wl"]  = list(_BRK_WL_CON)
    elif _cfg.get("label") == "既存版 aggressive":
        _cfg["label"]   = "5k-WL aggressive"
        _cfg["stop_wl"] = list(_STOP_WL_AGG)
        _cfg["brk_wl"]  = list(_BRK_WL_AGG)


# ═══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--date",       type=str,  default=None)
    _p.add_argument("--no-browser", action="store_true")
    _p.add_argument("--5k-only",    action="store_true", dest="only5k",
                    help="5k-WL conservative + aggressive の2configのみ表示")
    _known, _ = _p.parse_known_args()

    _orig_argv = list(sys.argv)
    sys.argv = [a for a in sys.argv if a not in ("--5k-only", "--aggressive")]

    if _known.only5k:
        _na._PNL_CONFIGS[:] = [
            c for c in _na._PNL_CONFIGS
            if c.get("label", "").startswith("5k-WL")
        ]

    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")

    _na.main()

    sys.argv[:] = _orig_argv

    JST      = timezone(timedelta(hours=9))
    date_str = _known.date if _known.date else str(datetime.now(JST).date())
    old_path = Path(f"nikkei_analysis_{date_str}.html")
    suffix   = "_5konly" if _known.only5k else ""
    new_path = Path(f"nikkei_analysis_5k{suffix}_{date_str}.html")

    if old_path.exists():
        old_path.replace(new_path)
        print(f"\n5kレポート生成完了: {new_path.resolve()}")
        if not _known.no_browser:
            open_html(new_path.resolve().as_uri())
    else:
        print(f"[WARN] {old_path} が見つかりません (--date 指定時は日付を確認)")


if __name__ == "__main__":
    main()
