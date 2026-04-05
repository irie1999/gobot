"""
check_signals.py  ―  監視銘柄の本日シグナルチェック
=====================================================
毎営業日の引け後に実行する。
各銘柄を担当戦略で判定し、翌日の指値注文情報を出力する。

使い方:
  python check_signals.py           # 通常実行
  python check_signals.py --verbose # 指標値も表示
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

# ── 各戦略モジュールをインポート ──────────────────────────────────
import backtest_adaptive_mr       as _amr
import backtest_strong_pullback   as _sp
import backtest_donchian_pullback as _don
import backtest_bb_volume         as _bbv
import backtest_limit_oco         as _oco

JST = timezone(timedelta(hours=9))

# ── 監視リスト定義 ────────────────────────────────────────────────
# (symbol, name, [戦略キー, ...])
# 戦略キーは複数指定可。シグナルが出た戦略をすべて表示する。
WATCHLIST: list[tuple[str, str, list[str]]] = [
    ("7012.T", "川崎重工業",             ["strong_pullback", "limit_oco"]),
    ("1605.T", "INPEX",                  ["adaptive_mr", "donchian"]),
    ("5844.T", "京都フィナンシャルグループ", ["donchian", "strong_pullback"]),
    ("6361.T", "荏原製作所",             ["adaptive_mr"]),
    ("5981.T", "東京製綱",               ["strong_pullback"]),
    ("5741.T", "UACJ",                   ["bb_volume"]),
    ("7013.T", "IHI",                    ["strong_pullback", "donchian"]),
    ("9044.T", "南海電鉄",               ["strong_pullback"]),
]

STRATEGY_NAMES = {
    "adaptive_mr":    "アダプティブMR",
    "strong_pullback":"強い押し目",
    "donchian":       "ドンチャン押し目",
    "bb_volume":      "BB出来高",
    "limit_oco":      "指値OCO",
}

LOT = 100  # 株


def _rr(entry: float, stop: float, target: float) -> str:
    risk   = entry - stop
    reward = target - entry
    if risk <= 0:
        return "—"
    return f"{reward/risk:.1f}R"


def _pct(a: float, b: float) -> str:
    if b == 0:
        return "—"
    return f"{(a - b) / b * 100:+.2f}%"


def check_symbol(symbol: str, name: str, strategies: list[str], verbose: bool) -> list[dict]:
    """1銘柄を複数戦略でチェックし、シグナルが出た戦略の結果リストを返す。"""
    # 各戦略共通の価格データは365日分で取得
    results = []

    for key in strategies:
        try:
            if key == "adaptive_mr":
                df = _amr.fetch(symbol, 365)
                if df is None:
                    continue
                df  = _amr.calc(df)
                sig = _amr._has_signal_today(df)
                if sig:
                    results.append({
                        "key":    key,
                        "close":  sig["close"],
                        "entry":  sig["limit_price"],
                        "stop":   sig["stop_loss"],
                        "target": sig["limit_price"] + sig["atr"] * 3.0,  # 3R目安
                        "atr":    sig["atr"],
                        "regime": sig.get("regime", "—"),
                        "note":   f"RSI2={sig['rsi2']:.1f} IBS={sig.get('ibs', float('nan')):.2f} レジーム={sig.get('regime','—')}",
                        "expire": sig.get("expire_days", 3),
                    })

            elif key == "strong_pullback":
                df = _sp.fetch(symbol, 365)
                if df is None:
                    continue
                df  = _sp.calc(df)
                sig = _sp._has_signal_today(df)
                if sig:
                    results.append({
                        "key":    key,
                        "close":  sig["close"],
                        "entry":  sig["limit_price"],
                        "stop":   sig["stop"],
                        "target": sig["target"],
                        "atr":    sig["atr"],
                        "regime": "—",
                        "note":   f"RSI5={sig['rsi5']:.1f}",
                        "expire": 3,
                    })

            elif key == "donchian":
                df = _don.fetch(symbol, 365)
                if df is None:
                    continue
                df  = _don.calc(df)
                sig = _don._has_signal_today(df)
                if sig:
                    vol_s = f"出来高比={sig['vol_ratio']:.1f}x" if not math.isnan(sig.get('vol_ratio', float('nan'))) else ""
                    results.append({
                        "key":    key,
                        "close":  sig["close"],
                        "entry":  sig["limit_price"],
                        "stop":   sig["stop"],
                        "target": sig["target"],
                        "atr":    sig["atr"],
                        "regime": "—",
                        "note":   f"DC高値={sig['don_high']:.0f} {vol_s}",
                        "expire": 3,
                    })

            elif key == "bb_volume":
                df = _bbv.fetch(symbol, 365)
                if df is None:
                    continue
                df  = _bbv.calc(df)
                sig = _bbv._has_signal_today(df)
                if sig:
                    vol_s = f"出来高比={sig['vol_ratio']:.1f}x" if not math.isnan(sig.get('vol_ratio', float('nan'))) else ""
                    results.append({
                        "key":    key,
                        "close":  sig["close"],
                        "entry":  sig["limit_price"],
                        "stop":   sig["stop"],
                        "target": sig["target"],
                        "atr":    sig["atr"],
                        "regime": "—",
                        "note":   f"BB下限={sig['bb_lower']:.0f} BB中心={sig['bb_mid']:.0f} {vol_s}",
                        "expire": 3,
                    })

            elif key == "limit_oco":
                df = _oco.fetch(symbol, 365)
                if df is None:
                    continue
                df  = _oco.calc(df)
                sig = _oco._has_signal_today(df, _oco.DEFAULT_PARAMS)
                if sig:
                    results.append({
                        "key":    key,
                        "close":  sig["close"],
                        "entry":  sig["limit_price"],
                        "stop":   sig["stop_loss"],
                        "target": sig["profit_target"],
                        "atr":    sig["atr"],
                        "regime": "—",
                        "note":   f"RSI2={sig['rsi2']:.1f} IBS={sig['ibs']:.2f}",
                        "expire": max(sig.get("expire_days", [3])) if isinstance(sig.get("expire_days"), list) else sig.get("expire_days", 3),
                    })

        except Exception as e:
            print(f"  ※ {symbol} [{key}] エラー: {e}", file=sys.stderr)

    return results


def print_signal(symbol: str, name: str, res: dict, verbose: bool) -> None:
    strat   = STRATEGY_NAMES.get(res["key"], res["key"])
    entry   = res["entry"]
    stop    = res["stop"]
    target  = res["target"]
    close   = res["close"]
    risk    = entry - stop
    reward  = target - entry
    lot_risk = risk * LOT

    print(f"  ★ {symbol}  {name}")
    print(f"     戦略     : {strat}")
    print(f"     現在値   : ¥{close:,.0f}")
    print(f"     指値     : ¥{entry:,.0f}  ({_pct(entry, close)} from 現在値)")
    print(f"     損切     : ¥{stop:,.0f}  (リスク ¥{risk:,.0f} / ロット ¥{lot_risk:,.0f})")
    print(f"     目標     : ¥{target:,.0f}  RR={_rr(entry, stop, target)}")
    print(f"     有効期限 : {res['expire']}営業日")
    if verbose:
        print(f"     指標     : {res['note']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="監視銘柄シグナルチェック")
    parser.add_argument("--verbose", "-v", action="store_true", help="指標値も表示")
    args = parser.parse_args()

    now = datetime.now(JST)
    print(f"\n{'='*72}")
    print(f"  監視銘柄シグナルチェック  {now.strftime('%Y-%m-%d %H:%M JST')}")
    print(f"  対象: {len(WATCHLIST)}銘柄 / ロットサイズ: {LOT}株")
    print(f"{'='*72}\n")

    active_signals: list[tuple[str, str, dict]] = []
    no_signal: list[tuple[str, str]] = []

    for symbol, name, strategies in WATCHLIST:
        results = check_symbol(symbol, name, strategies, args.verbose)
        if results:
            for r in results:
                active_signals.append((symbol, name, r))
        else:
            no_signal.append((symbol, name))

    # ── シグナルあり ─────────────────────────────────────────────
    if active_signals:
        print(f"【シグナル発生】 {len(active_signals)}件")
        print("─" * 72)
        for symbol, name, res in active_signals:
            print_signal(symbol, name, res, args.verbose)
    else:
        print("【シグナル発生なし】\n")

    # ── シグナルなし ─────────────────────────────────────────────
    if no_signal:
        names_str = "  " + "\n  ".join(
            f"{sym}  {nm}" for sym, nm in no_signal
        )
        print(f"【待機中（シグナルなし）】")
        print(names_str)
        print()

    # ── 翌日の注意事項 ─────────────────────────────────────────
    print(f"{'─'*72}")
    print("  注意:")
    print("  ・指値注文は翌営業日の寄り付き前に設定してください")
    print("  ・有効期限内に約定しなければキャンセル")
    print("  ・損切は必ず同時に逆指値注文を入れてください（OCO推奨）")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
