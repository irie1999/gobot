"""
シグナルモニター & ポジション管理ツール
────────────────────────────────────────
バックテスト上位16銘柄について、最新の日足データから
ストキャスティクスシグナルを判定し、売買判断を表示します。

■ 起動方法:
  python signal_monitor.py

■ コマンド一覧:
  s / signal    シグナル再取得・表示
  l / list      ポジション一覧
  b <コード>    買い記録   例: b 7203.T
  x <コード>    売り記録   例: x 7203.T
  h / help      ヘルプ表示
  q / quit      終了
"""

import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── 監視銘柄（バックテスト上位16銘柄） ──────────────────────
WATCH_LIST = [
    ("8604.T",  "野村HD"),
    ("2802.T",  "味の素"),
    ("7203.T",  "トヨタ自動車"),
    ("2914.T",  "JT"),
    ("8001.T",  "伊藤忠商事"),
    ("8306.T",  "三菱UFJ"),
    ("6752.T",  "パナソニックHD"),
    ("8002.T",  "丸紅"),
    ("9020.T",  "JR東日本"),
    ("6902.T",  "デンソー"),
    ("6702.T",  "富士通"),
    ("7751.T",  "キヤノン"),
    ("6326.T",  "クボタ"),
    ("8058.T",  "三菱商事"),
    ("8316.T",  "三井住友FG"),
    ("8031.T",  "三井物産"),
]

# ── ストキャスティクス パラメータ ─────────────────────────────
STOCH_K_PERIOD   = 14
STOCH_SMOOTH     = 3
STOCH_D_PERIOD   = 3
STOCH_OVERSOLD   = 30
STOCH_OVERBOUGHT = 70
ATR_PERIOD       = 14
ATR_STOP_MULT    = 2.0

# ── ポジション保存ファイル ─────────────────────────────────────
POSITION_FILE = os.path.join(os.path.dirname(__file__), "positions.json")


# ── ストキャスティクス計算 ────────────────────────────────────
def calc_stochastic(df: pd.DataFrame) -> pd.DataFrame:
    h = df["high"]
    l = df["low"]
    c = df["close"]

    lowest_low   = l.rolling(STOCH_K_PERIOD).min()
    highest_high = h.rolling(STOCH_K_PERIOD).max()
    denom        = highest_high - lowest_low

    fast_k = (c - lowest_low) / denom.replace(0, np.nan) * 100
    slow_k = fast_k.rolling(STOCH_SMOOTH).mean()
    slow_d = slow_k.rolling(STOCH_D_PERIOD).mean()

    df = df.copy()
    df["stoch_k"] = slow_k
    df["stoch_d"] = slow_d

    prev_k = slow_k.shift(1)
    prev_d = slow_d.shift(1)

    df["golden_cross"] = (slow_k > slow_d) & (prev_k <= prev_d)
    df["dead_cross"]   = (slow_k < slow_d) & (prev_k >= prev_d)
    df["entry_sig"]    = df["golden_cross"] & (slow_k < STOCH_OVERBOUGHT)
    df["exit_sig"]     = df["dead_cross"]

    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── 1銘柄のシグナル取得 ───────────────────────────────────────
def fetch_signal(symbol: str, name: str) -> dict | None:
    try:
        df = yf.download(symbol, period="60d", interval="1d",
                         auto_adjust=True, progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna()
    except Exception:
        return None

    min_rows = STOCH_K_PERIOD + STOCH_SMOOTH + STOCH_D_PERIOD
    if len(df) < min_rows:
        return None

    df = calc_stochastic(df)
    last = df.iloc[-1]

    if pd.isna(last["stoch_k"]) or pd.isna(last["stoch_d"]):
        return None

    k     = float(last["stoch_k"])
    d     = float(last["stoch_d"])
    close = float(last["close"])
    atr   = float(last["atr"]) if not pd.isna(last["atr"]) else 0.0
    date  = df.index[-1].strftime("%Y-%m-%d")

    # シグナル判定
    entry = bool(last["entry_sig"])
    exit_ = bool(last["exit_sig"])

    if entry:
        signal = "BUY"
    elif exit_:
        signal = "SELL"
    elif k < STOCH_OVERSOLD:
        signal = "WATCH_BUY"   # 過売り圏 → もうすぐ買いシグナル候補
    elif k > STOCH_OVERBOUGHT:
        signal = "WATCH_SELL"  # 過買い圏 → もうすぐ売りシグナル候補
    else:
        signal = "HOLD"

    stop = close - atr * ATR_STOP_MULT

    return {
        "symbol": symbol,
        "name":   name,
        "date":   date,
        "close":  close,
        "k":      k,
        "d":      d,
        "atr":    atr,
        "stop":   stop,
        "signal": signal,
    }


# ── ポジション管理 ────────────────────────────────────────────
def load_positions() -> dict:
    if os.path.exists(POSITION_FILE):
        with open(POSITION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_positions(positions: dict) -> None:
    with open(POSITION_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def do_buy(symbol: str, signals: dict, positions: dict) -> None:
    """買い記録"""
    sym_upper = symbol.upper()
    # .T を補完
    if not sym_upper.endswith(".T") and sym_upper.isdigit():
        sym_upper += ".T"

    info = signals.get(sym_upper)
    if info is None:
        # シグナル取得済みでなければ直接取得
        for s, n in WATCH_LIST:
            if s == sym_upper:
                print(f"  {n}({sym_upper}) のデータを取得中...")
                info = fetch_signal(sym_upper, n)
                break

    if info is None:
        print(f"  ✗ {sym_upper} のデータが取得できません。")
        return

    if sym_upper in positions:
        pos = positions[sym_upper]
        print(f"  ⚠ {info['name']}({sym_upper}) はすでにポジションあり "
              f"（取得価格: {pos['entry_price']:,.0f}円 × {pos['qty']}株）")
        ans = input("    上書きしますか？ [y/N]: ").strip().lower()
        if ans != "y":
            return

    price = info["close"]
    stop  = info["stop"]
    qty   = 1  # 1株単位

    positions[sym_upper] = {
        "name":        info["name"],
        "entry_price": price,
        "entry_date":  datetime.now().strftime("%Y-%m-%d"),
        "qty":         qty,
        "stop_price":  round(stop, 1),
    }
    save_positions(positions)
    print(f"  ✔ 買い記録: {info['name']}({sym_upper})")
    print(f"    取得価格 : {price:,.1f}円 × {qty}株")
    print(f"    ストップ : {stop:,.1f}円  (ATR×{ATR_STOP_MULT} = {info['atr']:,.1f}円)")


def do_sell(symbol: str, signals: dict, positions: dict) -> None:
    """売り記録"""
    sym_upper = symbol.upper()
    if not sym_upper.endswith(".T") and sym_upper.isdigit():
        sym_upper += ".T"

    if sym_upper not in positions:
        print(f"  ✗ {sym_upper} のポジションはありません。")
        return

    pos   = positions[sym_upper]
    name  = pos["name"]
    entry = pos["entry_price"]
    qty   = pos["qty"]

    # 現在価格をシグナルから取得
    info  = signals.get(sym_upper)
    if info:
        exit_price = info["close"]
        pnl        = (exit_price - entry) * qty
        pnl_str    = f"{pnl:+,.0f}円"
        pct        = (exit_price - entry) / entry * 100
        pct_str    = f"{pct:+.1f}%"
    else:
        exit_price = None
        pnl_str    = "（価格不明）"
        pct_str    = ""

    del positions[sym_upper]
    save_positions(positions)

    print(f"  ✔ 売り記録: {name}({sym_upper})")
    print(f"    取得価格 : {entry:,.1f}円  →  ", end="")
    if exit_price:
        print(f"現在価格: {exit_price:,.1f}円")
        print(f"    損益     : {pnl_str}  ({pct_str})")
    else:
        print("（現在価格取得失敗）")


def show_positions(positions: dict, signals: dict) -> None:
    """ポジション一覧表示"""
    if not positions:
        print("  （保有ポジションなし）")
        return

    print()
    print(f"  {'銘柄':<22} {'取得日':<12} {'取得価格':>8} {'現在値':>8} "
          f"{'損益':>10} {'損益率':>7} {'ストップ':>9}")
    print("  " + "─" * 85)

    total_pnl = 0.0
    for sym, pos in positions.items():
        name      = pos["name"]
        entry     = pos["entry_price"]
        entry_dt  = pos["entry_date"]
        qty       = pos["qty"]
        stop      = pos["stop_price"]
        info      = signals.get(sym)
        if info:
            cur  = info["close"]
            pnl  = (cur - entry) * qty
            pct  = (cur - entry) / entry * 100
            cur_str = f"{cur:>8,.1f}"
            pnl_str = f"{pnl:>+10,.0f}"
            pct_str = f"{pct:>+6.1f}%"
            total_pnl += pnl
        else:
            cur_str = "   取得中"
            pnl_str = "          "
            pct_str = "      "

        label = f"{name}({sym})"
        print(f"  {label:<22} {entry_dt:<12} {entry:>8,.1f} {cur_str} "
              f"{pnl_str} {pct_str} {stop:>9,.1f}")

    print("  " + "─" * 85)
    print(f"  合計損益: {total_pnl:+,.0f}円")


# ── シグナル表示 ──────────────────────────────────────────────
SIGNAL_LABEL = {
    "BUY":        "★ BUY      ← 買いシグナル！",
    "SELL":       "▼ SELL     ← 売りシグナル！",
    "WATCH_BUY":  "◎ WATCH    過売り圏（買い候補）",
    "WATCH_SELL": "△ WATCH    過買い圏（売り候補）",
    "HOLD":       "  HOLD",
}

SIGNAL_ORDER = {"BUY": 0, "SELL": 1, "WATCH_BUY": 2, "WATCH_SELL": 3, "HOLD": 4}


def show_signals(signals: dict, positions: dict) -> None:
    items = sorted(signals.values(), key=lambda x: SIGNAL_ORDER[x["signal"]])

    date_str = items[0]["date"] if items else datetime.now().strftime("%Y-%m-%d")
    print()
    print(f"  ══════════════════════════════════════════════════════════")
    print(f"  ストキャスティクス シグナル一覧  [{date_str}]")
    print(f"  ══════════════════════════════════════════════════════════")
    print(f"  {'#':<3} {'銘柄':<22} {'終値':>7} {'%K':>6} {'%D':>6} "
          f"{'ストップ':>8}  シグナル")
    print(f"  {'─'*3} {'─'*22} {'─'*7} {'─'*6} {'─'*6} {'─'*8}  {'─'*28}")

    for i, s in enumerate(items, 1):
        sym   = s["symbol"]
        pos_mark = " [保有中]" if sym in positions else ""
        label = f"{s['name']}({sym})"
        sig   = SIGNAL_LABEL[s["signal"]]
        print(f"  {i:<3} {label:<22} {s['close']:>7,.1f} "
              f"{s['k']:>6.1f} {s['d']:>6.1f} {s['stop']:>8,.1f}  {sig}{pos_mark}")

    print()
    buy_count  = sum(1 for s in signals.values() if s["signal"] == "BUY")
    sell_count = sum(1 for s in signals.values() if s["signal"] == "SELL")
    print(f"  買いシグナル: {buy_count}件  売りシグナル: {sell_count}件")
    print()


# ── ヘルプ ────────────────────────────────────────────────────
def show_help() -> None:
    print("""
  ─── コマンド一覧 ───────────────────────────────────────────
  s  / signal          シグナル再取得・表示
  l  / list            ポジション一覧
  b  <コード>          買い記録    例: b 7203.T  または  b 7203
  x  <コード>          売り記録    例: x 7203.T  または  x 7203
  h  / help            このヘルプを表示
  q  / quit / exit     終了
  ────────────────────────────────────────────────────────────
  ※ コードは .T なしの数字4桁でも入力可（自動補完）
""")


# ── メインループ ──────────────────────────────────────────────
def fetch_all_signals() -> dict:
    print("\n  データ取得中...")
    signals = {}
    total = len(WATCH_LIST)
    for i, (sym, name) in enumerate(WATCH_LIST, 1):
        print(f"  [{i:2d}/{total}] {name}({sym}) ...", end=" ", flush=True)
        result = fetch_signal(sym, name)
        if result:
            signals[sym] = result
            sig = result["signal"]
            k   = result["k"]
            d   = result["d"]
            print(f"%K={k:.1f} %D={d:.1f}  → {sig}")
        else:
            print("スキップ")
    return signals


def main() -> None:
    print("=" * 62)
    print("  シグナルモニター & ポジション管理  (1株単位)")
    print("  対象: バックテスト上位16銘柄")
    print("  'h' でヘルプ表示")
    print("=" * 62)

    positions = load_positions()
    signals   = fetch_all_signals()
    show_signals(signals, positions)

    if positions:
        print("  ─── 保有ポジション ───")
        show_positions(positions, signals)

    while True:
        try:
            raw = input("\n  コマンド> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  終了します。")
            break

        if not raw:
            continue

        parts = raw.split()
        cmd   = parts[0].lower()

        if cmd in ("q", "quit", "exit"):
            print("  終了します。")
            break

        elif cmd in ("h", "help"):
            show_help()

        elif cmd in ("s", "signal"):
            signals = fetch_all_signals()
            show_signals(signals, positions)

        elif cmd in ("l", "list"):
            positions = load_positions()
            print()
            print("  ─── 保有ポジション ───")
            show_positions(positions, signals)

        elif cmd in ("b", "buy"):
            if len(parts) < 2:
                print("  使い方: b <銘柄コード>   例: b 7203.T")
            else:
                positions = load_positions()
                do_buy(parts[1], signals, positions)

        elif cmd in ("x", "sell"):
            if len(parts) < 2:
                print("  使い方: x <銘柄コード>   例: x 7203.T")
            else:
                positions = load_positions()
                do_sell(parts[1], signals, positions)

        else:
            print(f"  ✗ 不明なコマンド: {cmd}  ('h' でヘルプ)")


if __name__ == "__main__":
    main()
