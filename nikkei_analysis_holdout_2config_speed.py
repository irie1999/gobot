"""
nikkei_analysis_holdout_2config_speed.py — 速度重視版 nikkei_analysis_holdout_2config.py

nikkei_analysis_holdout_2config.py と全く同じ引数で実行できる。
以下の2点だけ異なる:
  1. BTスコアに速度ボーナス最大+10点を適用 (score_speed_patch)
  2. CSV銘柄選定スコアに速度係数を追加 (avg_hold_days が短い銘柄が上位)
  3. 出力ファイル名に _speed を付加

使い方 (nikkei_analysis_holdout_2config.py と同じ引数):
  python nikkei_analysis_holdout_2config_speed.py --holdout-days 60  --budget 1000000 --days 60
  python nikkei_analysis_holdout_2config_speed.py --holdout-days 90  --budget 1000000 --days 90  --workers 8
  python nikkei_analysis_holdout_2config_speed.py --holdout-days 150 --budget 1000000 --days 150 --workers 8
  python nikkei_analysis_holdout_2config_speed.py --holdout-days 180 --budget 1000000 --days 180 --workers 8

出力: nikkei_analysis_holdout2cfg_speed_{N}d_{date}.html
"""
from __future__ import annotations
import sys

# ── 速度ボーナスパッチを最初に適用 ─────────────────────────────────────────
import score_speed_patch  # noqa: F401

# ── 元モジュールをインポート（module-level コードが sys.argv を読む） ──────
import nikkei_analysis_holdout_2config as _m


# ── _composite_score を速度係数込みに差し替え ──────────────────────────────
def _composite_score_speed(r: dict) -> float:
    pnl      = float(r.get("total_test_pnl", 0) or 0)
    sharpe   = float(r.get("sharpe", 0) or 0)
    base     = pnl * (1.0 + max(sharpe, 0.0))
    avg_hold = float(r.get("avg_hold_days", 15) or 15.0)
    speed_mult = max(0.5, 2.0 - avg_hold / 15.0)  # 1日→×1.93 / 15日→×1.0
    return base * speed_mult

_m._composite_score = _composite_score_speed


if __name__ == "__main__":
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import argparse, shutil

    JST = timezone(timedelta(hours=9))
    TODAY = datetime.now(JST).date()

    # holdout_days と date フラグを先読み
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--holdout-days", type=int, default=0)
    _pre.add_argument("--date",         type=str, default=None)
    _pre.add_argument("--no-browser",   action="store_true")
    _known, _ = _pre.parse_known_args()

    holdout_n  = _known.holdout_days
    date_str   = _known.date or str(TODAY)
    no_browser = _known.no_browser

    # ブラウザ自動起動は自分でやるので元 main() には抑制
    if "--no-browser" not in sys.argv:
        sys.argv.append("--no-browser")

    _m.main()

    # 出力ファイルをリネーム → _speed 付きに
    normal = Path(f"nikkei_analysis_holdout2cfg_{holdout_n}d_{date_str}.html")
    speed  = Path(f"nikkei_analysis_holdout2cfg_speed_{holdout_n}d_{date_str}.html")

    if normal.exists():
        shutil.copy2(normal, speed)
        normal.unlink()
        print(f"速度重視版レポート: {speed.resolve()}")
    elif speed.exists():
        print(f"速度重視版レポート: {speed.resolve()}")
    else:
        print("[WARN] 出力ファイルが見つかりません")
        sys.exit(0)

    if not no_browser:
        from _open_html import open_html
        open_html(speed.resolve().as_uri())
