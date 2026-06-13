"""
aggressive_params_daytrade.py  ―  conservative / aggressive モード切替
==================================================================
逆指値ロング (TRADING_MODE 環境変数) を5分足デイトレ用に移植。

【モード】
- conservative (デフォルト): 目標が遠い・安全運用
- aggressive: 目標近い・回転率重視

【パラメータ差分】
              sm    tm
  conservative 1.5  3.0    (目標+9%)
  aggressive   1.5  1.5    (目標+4.5%)
  (RSI2 は em=0/sm=2.0 で別計算)

【使い方】
  os.environ["TRADING_MODE"] = "aggressive"
  # その後 import check_signals_daytrade などすると aggressive で動作

  python script.py --aggressive   # 多くのスクリプトで対応
"""

from __future__ import annotations

import os


def get_mode():
    """現在のトレーディングモード取得。"""
    return os.environ.get("TRADING_MODE", "conservative").lower()


def is_aggressive():
    return get_mode() == "aggressive"


# 戦略別パラメータ (em/sm/tm)
PARAMS_CONSERVATIVE = {
    "DON":   {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "MACD":  {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "RSI2":  {"em": 0.0, "sm": 2.0, "tm": 4.0},
    "A7":    {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "VOL":   {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "MOM":   {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "DON_S": {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "MACD_S":{"em": 0.0, "sm": 1.5, "tm": 3.0},
    "RSI2_S":{"em": 0.0, "sm": 2.0, "tm": 4.0},
    "A7_S":  {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "VOL_S": {"em": 0.0, "sm": 1.5, "tm": 3.0},
    "MOM_S": {"em": 0.0, "sm": 1.5, "tm": 3.0},
}

PARAMS_AGGRESSIVE = {
    "DON":   {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "MACD":  {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "RSI2":  {"em": 0.0, "sm": 2.0, "tm": 2.0},
    "A7":    {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "VOL":   {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "MOM":   {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "DON_S": {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "MACD_S":{"em": 0.0, "sm": 1.5, "tm": 1.5},
    "RSI2_S":{"em": 0.0, "sm": 2.0, "tm": 2.0},
    "A7_S":  {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "VOL_S": {"em": 0.0, "sm": 1.5, "tm": 1.5},
    "MOM_S": {"em": 0.0, "sm": 1.5, "tm": 1.5},
}


def get_params(strategy):
    """戦略の現在モード パラメータを取得。"""
    if is_aggressive():
        return PARAMS_AGGRESSIVE.get(strategy, {"em": 0.0, "sm": 1.5, "tm": 1.5})
    return PARAMS_CONSERVATIVE.get(strategy, {"em": 0.0, "sm": 1.5, "tm": 3.0})


def parse_cli_mode(argv):
    """sys.argv から --aggressive / --conservative を読んで設定。"""
    if "--aggressive" in argv:
        os.environ["TRADING_MODE"] = "aggressive"
        return "aggressive"
    if "--conservative" in argv:
        os.environ["TRADING_MODE"] = "conservative"
        return "conservative"
    return get_mode()


if __name__ == "__main__":
    import sys
    mode = parse_cli_mode(sys.argv)
    print(f"現在モード: {mode}")
    print(f"\n戦略パラメータ:")
    for strat in PARAMS_CONSERVATIVE.keys():
        p = get_params(strat)
        print(f"  {strat:7}: em={p['em']:.1f} sm={p['sm']:.1f} tm={p['tm']:.1f}")
