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
"""
from __future__ import annotations

import argparse
import importlib as _importlib
import os
import sys
import webbrowser
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

# ── 9. PNL タブのラベルを v2 用に更新 ────────────────────────────────────────
for _cfg in _na._PNL_CONFIGS:
    if _cfg.get("label") == "既存版 conservative":
        _cfg["label"] = "v2新WL conservative"
    elif _cfg.get("label") == "既存版 aggressive":
        _cfg["label"] = "v2新WL aggressive"


# ═══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # 出力ファイル名に使う日付を先読み
    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--date",       type=str,  default=None)
    _p.add_argument("--no-browser", action="store_true")
    _known, _ = _p.parse_known_args()

    # nikkei_analysis.main() 内でのブラウザ起動を抑制（リネーム後に自分で開く）
    _orig_argv = list(sys.argv)
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
    new_path = Path(f"nikkei_analysis_v2_{date_str}.html")

    if old_path.exists():
        old_path.replace(new_path)
        print(f"\nv2レポート生成完了: {new_path.resolve()}")
        if not _known.no_browser:
            webbrowser.open(new_path.resolve().as_uri())
    else:
        print(f"[WARN] {old_path} が見つかりませんでした (--date 指定時は日付を確認)")


if __name__ == "__main__":
    main()
