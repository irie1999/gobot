"""
nikkei_analysis_5k.py  ―  5000円以下WF選定WATCHLIST (2026-06-06) で日経分析を実行

check_signals_stop.py / check_signals_breakout.py の現 WATCHLIST
(2026-06-06 WFスキャン結果, max-price≤5000円, 目標ポジション100万円/200株以上) を
そのまま使用する。nikkei_analysis.py 本体は一切変更しない。

出力: nikkei_analysis_5k_{date}.html

使い方:
  python nikkei_analysis_5k.py                    # 過去5年 HTML生成 & ブラウザ表示
  python nikkei_analysis_5k.py --years 10         # 過去10年
  python nikkei_analysis_5k.py --days 365         # 損益集計365日
  python nikkei_analysis_5k.py --no-browser       # HTML生成のみ
  python nikkei_analysis_5k.py --no-signals       # シグナルタブなし
  python nikkei_analysis_5k.py --no-pnl           # 損益タブなし
  python nikkei_analysis_5k.py --aggressive       # 積極利確モード
  python nikkei_analysis_5k.py --5k-only          # conservative + aggressive の2configのみ表示
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

# ── 3. 現 WATCHLIST をスナップショット保存 ───────────────────────────────────
#   check_signals_stop.WATCHLIST / check_signals_breakout.WATCHLIST は
#   2026-06-06 WFスキャン結果 (max-price≤5000円, 目標ポジション100万円/200株以上)
_STOP_WL = list(_stop.WATCHLIST)
_BRK_WL  = list(_brk.WATCHLIST)

print("=" * 65)
print("nikkei_analysis_5k: 5000円以下WF WATCHLIST (2026-06-06 選定)")
print(f"  逆指値B : {len(_STOP_WL)} 銘柄×戦略  (RSI2×10 / MACD×5 / A7×10)")
print(f"  BRK     : {len(_BRK_WL)} 銘柄×戦略  (DON×1 / MOM×5 / VOL×7)")
print(f"  ポジション: ~100万円 / 取引  (200株以上, 5000円以下銘柄)")
print("=" * 65)

# ── 4. importlib.reload フック ───────────────────────────────────────────────
#   nikkei_analysis.py は TRADING_MODE 切替のために _stop/_brk を reload() する。
#   reload すると WATCHLIST が元に戻るため、フックで再適用する。
_orig_reload = _importlib.reload

def _wl_preserving_reload(module):
    result = _orig_reload(module)
    if getattr(module, "__name__", "") == "check_signals_stop":
        result.WATCHLIST = list(_STOP_WL)
    elif getattr(module, "__name__", "") == "check_signals_breakout":
        result.WATCHLIST = list(_BRK_WL)
    return result

_importlib.reload = _wl_preserving_reload

# ── 5. nikkei_analysis を import ────────────────────────────────────────────
import nikkei_analysis as _na  # noqa: E402

# ── 6. reload フックを元に戻す ────────────────────────────────────────────────
_importlib.reload = _orig_reload

# ── 7. PNL タブのラベルを 5k 用に更新 ────────────────────────────────────────
for _cfg in _na._PNL_CONFIGS:
    if _cfg.get("label") == "既存版 conservative":
        _cfg["label"]   = "5k-WL conservative"
        _cfg["stop_wl"] = list(_STOP_WL)
        _cfg["brk_wl"]  = list(_BRK_WL)
    elif _cfg.get("label") == "既存版 aggressive":
        _cfg["label"]   = "5k-WL aggressive"
        _cfg["stop_wl"] = list(_STOP_WL)
        _cfg["brk_wl"]  = list(_BRK_WL)


# ═══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # 先読みパーサー (nikkei_analysis.py が知らない独自オプションを処理)
    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--date",       type=str,  default=None)
    _p.add_argument("--no-browser", action="store_true")
    _p.add_argument("--5k-only",    action="store_true", dest="only5k",
                    help="5k-WL conservative + aggressive の2configのみ表示 (他は非表示)")
    _known, _ = _p.parse_known_args()

    # nikkei_analysis.py が知らないオプションを除去してから渡す
    _orig_argv = list(sys.argv)
    sys.argv = [a for a in sys.argv if a not in ("--5k-only", "--aggressive")]

    # --5k-only: _PNL_CONFIGS を 5k-WL の2件のみに絞る
    if _known.only5k:
        _na._PNL_CONFIGS[:] = [
            c for c in _na._PNL_CONFIGS
            if c.get("label", "").startswith("5k-WL")
        ]

    # HTML生成後にリネームするためブラウザ起動を一時停止
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")

    _na.main()

    # sys.argv を元に戻す
    sys.argv[:] = _orig_argv

    # 出力ファイルをリネーム
    JST      = timezone(timedelta(hours=9))
    date_str = _known.date if _known.date else str(datetime.now(JST).date())
    old_path = Path(f"nikkei_analysis_{date_str}.html")
    suffix   = "_5konly" if _known.only5k else ""
    new_path = Path(f"nikkei_analysis_5k{suffix}_{date_str}.html")

    if old_path.exists():
        old_path.replace(new_path)
        print(f"\n5kレポート生成完了: {new_path.resolve()}")
        if not _known.no_browser:
            webbrowser.open(new_path.resolve().as_uri())
    else:
        print(f"[WARN] {old_path} が見つかりません (--date 指定時は日付を確認)")


if __name__ == "__main__":
    main()
