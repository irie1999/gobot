"""
シグナルモニター & ポジション管理ツール
────────────────────────────────────────
スキャン利益率ランキング上位30銘柄について、最新の日足データから
ストキャスティクスシグナルを判定し、売買判断を表示します。

■ 起動方法:
  python signal_monitor.py

■ コマンド一覧:
  s / signal              シグナル再取得・表示
  l / list                ポジション一覧
  b <コード>              買い記録         例: b 7203.T
  u <コード> <約定価格>   取得価格を更新   例: u 7203.T 3250
  x <コード>              売り記録         例: x 7203.T
  h / help                ヘルプ表示
  q / quit                終了
"""

import json
import os
import sys
import tempfile
import webbrowser
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── 監視銘柄（スキャン利益率ランキング上位30銘柄） ──────────
WATCH_LIST = [
    ("6503.T",  "三菱電機"),           #  1位 +38.1%
    ("8604.T",  "野村HD"),             #  2位 +36.0%
    ("4043.T",  "トクヤマ"),           #  3位 +34.7%
    ("3105.T",  "日清紡HD"),           #  4位 +31.7%
    ("9502.T",  "中部電力"),           #  5位 +31.3%
    ("1963.T",  "日揮HD"),             #  6位 +30.2%
    ("2802.T",  "味の素"),             #  7位 +28.6%
    ("6383.T",  "ダイフク"),           #  8位 +28.1%
    ("1803.T",  "清水建設"),           #  9位 +26.9%
    ("4506.T",  "住友ファーマ"),       # 10位 +24.8%
    ("7203.T",  "トヨタ自動車"),       # 11位 +24.3%
    ("2914.T",  "日本たばこ産業"),     # 12位 +23.1%
    ("8001.T",  "伊藤忠商事"),         # 13位 +23.1%
    ("8306.T",  "三菱UFJフィナンシャルG"), # 14位 +22.0%
    ("6752.T",  "パナソニックHD"),     # 15位 +21.7%
    ("8002.T",  "丸紅"),               # 16位 +20.6%
    ("6981.T",  "村田製作所"),         # 17位 +20.4%
    ("8830.T",  "住友不動産"),         # 18位 +20.1%
    ("9020.T",  "東日本旅客鉄道"),     # 19位 +20.1%
    ("1605.T",  "INPEX"),              # 20位 +19.9%
    ("8309.T",  "三井住友トラストHD"), # 21位 +19.1%
    ("6902.T",  "デンソー"),           # 22位 +18.4%
    ("7731.T",  "ニコン"),             # 23位 +18.1%
    ("4631.T",  "DIC"),                # 24位 +17.8%
    ("5713.T",  "住友金属鉱山"),       # 25位 +17.6%
    ("8802.T",  "三菱地所"),           # 26位 +17.5%
    ("3289.T",  "東急不動産HD"),       # 27位 +17.2%
    ("9532.T",  "大阪ガス"),           # 28位 +17.2%
    ("5707.T",  "東邦亜鉛"),           # 29位 +16.1%
    ("5741.T",  "UACJ"),               # 30位 +16.0%
]

# ── ストキャスティクス パラメータ（A7: トレンドフィルター付き）──
STOCH_K_PERIOD   = 14
STOCH_SMOOTH     = 3
STOCH_D_PERIOD   = 3
STOCH_OVERSOLD   = 30
STOCH_OVERBOUGHT = 70
ATR_PERIOD       = 14
ATR_STOP_MULT    = 1.5          # 初期ストップロス倍率
ATR_TRAIL_MULT   = 2.0          # トレイリングストップ倍率
MA_TREND_PERIOD  = 75           # トレンドフィルター移動平均期間

# ── ポジションサイジング パラメータ（S株：1株単位） ──────────
INITIAL_CASH   = 500_000   # 運用資金（円）
RISK_PER_TRADE = 0.02      # 1トレードあたり許容損失率 2%
MAX_QTY        = 9999      # 最大購入株数

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

    # 75日移動平均線（トレンドフィルター）
    ma75 = c.rolling(MA_TREND_PERIOD).mean()
    df["ma75"] = ma75

    prev_k = slow_k.shift(1)
    prev_d = slow_d.shift(1)

    df["golden_cross"] = (slow_k > slow_d) & (prev_k <= prev_d)
    df["dead_cross"]   = (slow_k < slow_d) & (prev_k >= prev_d)
    # A7: トレンドフィルター（終値 > 75MA）追加
    df["entry_sig"]    = df["golden_cross"] & (slow_k < STOCH_OVERBOUGHT) & (c > ma75)
    df["exit_sig"]     = df["dead_cross"]

    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── 1銘柄のシグナル取得 ───────────────────────────────────────
def fetch_signal(symbol: str, name: str) -> dict | None:
    try:
        # 75日MA計算に十分なデータを取得するため6ヶ月分取得
        df = yf.download(symbol, period="6mo", interval="1d",
                         auto_adjust=True, progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna()
    except Exception:
        return None

    min_rows = MA_TREND_PERIOD + STOCH_K_PERIOD + STOCH_SMOOTH + STOCH_D_PERIOD
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
    ma75  = float(last["ma75"]) if not pd.isna(last["ma75"]) else 0.0
    date  = df.index[-1].strftime("%Y-%m-%d")
    trend_up = close > ma75 if ma75 > 0 else False

    # シグナル判定（A7: トレンドフィルター考慮）
    entry = bool(last["entry_sig"])
    exit_ = bool(last["exit_sig"])

    if entry:
        signal = "BUY"
    elif exit_:
        signal = "SELL"
    elif k < STOCH_OVERSOLD and trend_up:
        signal = "WATCH_BUY"   # 過売り圏かつ上昇トレンド → 買いシグナル待ち
    elif k < STOCH_OVERSOLD and not trend_up:
        signal = "WATCH_BUY↓"  # 過売り圏だがトレンド下向き → 75MA回復待ち
    elif k > STOCH_OVERBOUGHT:
        signal = "WATCH_SELL"  # 過買い圏 → 売りシグナル待ち
    else:
        signal = "HOLD"

    # トレイリングストップ価格（参考値：現在の終値ベース）
    trail_stop = close - atr * ATR_TRAIL_MULT
    stop_dist  = atr * ATR_STOP_MULT
    risk_amt   = INITIAL_CASH * RISK_PER_TRADE
    if stop_dist > 0:
        qty = min(int(risk_amt / stop_dist), MAX_QTY)
    else:
        qty = 1
    qty = max(qty, 1)

    return {
        "symbol":    symbol,
        "name":      name,
        "date":      date,
        "close":     close,
        "k":         k,
        "d":         d,
        "atr":       atr,
        "ma75":      ma75,
        "trend_up":  trend_up,
        "stop":      trail_stop,
        "signal":    signal,
        "qty":       qty,
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


def do_buy(symbol: str, signals: dict, positions: dict, qty_input: str | None = None, price_input: str | None = None) -> None:
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

    atr       = info["atr"]
    stop_dist = atr * ATR_STOP_MULT
    risk_amt  = INITIAL_CASH * RISK_PER_TRADE

    # 取得価格：引数指定 > 現在終値
    if price_input is not None:
        try:
            price = float(price_input.replace(",", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            print(f"  ✗ 取得価格が不正です: {price_input}  （正の数値を入力してください）")
            return
        price_note = "（指定価格）"
    else:
        price = info["close"]
        price_note = "（現在終値）"

    stop = price - atr * ATR_STOP_MULT

    # 株数：引数指定 > ATRベース自動計算
    if qty_input is not None:
        try:
            qty = int(qty_input.replace(",", ""))
            if qty <= 0:
                raise ValueError
        except ValueError:
            print(f"  ✗ 株数が不正です: {qty_input}  （正の整数を入力してください）")
            return
        qty_note = f"（指定株数）"
    else:
        if stop_dist > 0:
            qty = min(int(risk_amt / stop_dist), MAX_QTY)
        else:
            qty = 1
        qty = max(qty, 1)
        qty_note = f"（ATRベース推奨）"

    cost = price * qty

    positions[sym_upper] = {
        "name":        info["name"],
        "entry_price": price,
        "entry_date":  datetime.now().strftime("%Y-%m-%d"),
        "qty":         qty,
        "stop_price":  round(stop, 1),
    }
    save_positions(positions)
    print(f"  ✔ 買い記録: {info['name']}({sym_upper})")
    print(f"    取得価格 : {price:,.1f}円 {price_note} × {qty}株 {qty_note}  （購入金額: {cost:,.0f}円）")
    print(f"    ストップ : {stop:,.1f}円  (ATR={atr:,.1f}円 × {ATR_STOP_MULT})")
    print(f"    許容損失 : {risk_amt:,.0f}円 （資金{INITIAL_CASH:,}円 × {RISK_PER_TRADE*100:.0f}%） ※S株")


def do_update_price(symbol: str, new_price_str: str, signals: dict, positions: dict) -> None:
    """約定価格でポジションの取得価格を更新する"""
    sym_upper = symbol.upper()
    if not sym_upper.endswith(".T") and sym_upper.isdigit():
        sym_upper += ".T"

    if sym_upper not in positions:
        print(f"  ✗ {sym_upper} のポジションはありません。先に 'b {symbol}' で登録してください。")
        return

    try:
        new_price = float(new_price_str.replace(",", ""))
        if new_price <= 0:
            raise ValueError
    except ValueError:
        print(f"  ✗ 価格が不正です: {new_price_str}  （正の数値を入力してください）")
        return

    pos       = positions[sym_upper]
    old_price = pos["entry_price"]
    name      = pos["name"]

    # ATRを使ってストップ価格を再計算（シグナルデータがあれば）
    info = signals.get(sym_upper)
    if info and info["atr"] > 0:
        new_stop = round(new_price - info["atr"] * ATR_STOP_MULT, 1)
    else:
        # ATRが取れない場合は旧ストップとの差分を維持
        diff = pos["stop_price"] - old_price
        new_stop = round(new_price + diff, 1)

    pos["entry_price"] = new_price
    pos["stop_price"]  = new_stop
    save_positions(positions)

    print(f"  ✔ 取得価格を更新: {name}({sym_upper})")
    print(f"    取得価格 : {old_price:,.1f}円  →  {new_price:,.1f}円")
    print(f"    ストップ : {new_stop:,.1f}円", end="")
    if info and info["atr"] > 0:
        print(f"  (ATR×{ATR_STOP_MULT} = {info['atr']:,.1f}円)")
    else:
        print()


def do_sell(symbol: str, signals: dict, positions: dict, qty_input: str | None = None) -> None:
    """売り記録"""
    sym_upper = symbol.upper()
    if not sym_upper.endswith(".T") and sym_upper.isdigit():
        sym_upper += ".T"

    if sym_upper not in positions:
        print(f"  ✗ {sym_upper} のポジションはありません。")
        return

    pos        = positions[sym_upper]
    name       = pos["name"]
    entry      = pos["entry_price"]
    held_qty   = pos["qty"]

    # 売り株数：引数指定 > 全売り
    if qty_input is not None:
        try:
            sell_qty = int(qty_input.replace(",", ""))
            if sell_qty <= 0:
                raise ValueError
        except ValueError:
            print(f"  ✗ 株数が不正です: {qty_input}  （正の整数を入力してください）")
            return
        if sell_qty > held_qty:
            print(f"  ✗ 保有株数({held_qty}株)を超えています: {sell_qty}株")
            return
    else:
        sell_qty = held_qty  # 全売り

    # 現在価格をシグナルから取得
    info  = signals.get(sym_upper)
    if info:
        exit_price = info["close"]
        pnl        = (exit_price - entry) * sell_qty
        pnl_str    = f"{pnl:+,.0f}円"
        pct        = (exit_price - entry) / entry * 100
        pct_str    = f"{pct:+.1f}%"
    else:
        exit_price = None
        pnl_str    = "（価格不明）"
        pct_str    = ""

    remaining = held_qty - sell_qty
    if remaining > 0:
        pos["qty"] = remaining
        save_positions(positions)
    else:
        del positions[sym_upper]
        save_positions(positions)

    print(f"  ✔ 売り記録: {name}({sym_upper})  {sell_qty}株売り", end="")
    print(f"  （残: {remaining}株）" if remaining > 0 else "  （全売り）")
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
    "BUY":         "★ BUY      ← 買いシグナル！（75MA上・トレンド確認済）",
    "SELL":        "▼ SELL     ← 売りシグナル！",
    "WATCH_BUY":   "◎ WATCH↑  過売り圏・上昇トレンド（買い候補）",
    "WATCH_BUY↓":  "○ WATCH↓  過売り圏・75MA下（トレンド回復待ち）",
    "WATCH_SELL":  "△ WATCH    過買い圏（売り候補）",
    "HOLD":        "  HOLD",
}

SIGNAL_ORDER = {"BUY": 0, "SELL": 1, "WATCH_BUY": 2, "WATCH_BUY↓": 3, "WATCH_SELL": 4, "HOLD": 5}


def show_signals(signals: dict, positions: dict) -> None:
    items = sorted(signals.values(), key=lambda x: SIGNAL_ORDER[x["signal"]])

    date_str = items[0]["date"] if items else datetime.now().strftime("%Y-%m-%d")
    print()
    print(f"  ══════════════════════════════════════════════════════════════════════════")
    print(f"  A7 シグナル一覧  [{date_str}]  ※トレンドフィルター付き・S株（1株単位）")
    print(f"  ══════════════════════════════════════════════════════════════════════════")
    print(f"  {'#':<3} {'銘柄':<22} {'終値':>7} {'75MA':>8} {'TRD':>4} {'%K':>6} {'%D':>6} "
          f"{'トレイル':>8} {'推奨株数':>8}  シグナル")
    print(f"  {'─'*3} {'─'*22} {'─'*7} {'─'*8} {'─'*4} {'─'*6} {'─'*6} {'─'*8} {'─'*8}  {'─'*46}")

    for i, s in enumerate(items, 1):
        sym      = s["symbol"]
        sig      = s["signal"]
        label    = f"{s['name']}({sym})"
        sig_lbl  = SIGNAL_LABEL[sig]
        ma75_str = f"{s['ma75']:>8,.1f}" if s.get("ma75", 0) > 0 else f"{'─':>8}"
        trd_str  = "↑" if s.get("trend_up") else "↓"

        # 推奨株数: BUY/WATCH_BUY → 新規買い推奨株数, SELL → 保有株数, それ以外 → -
        if sig in ("BUY", "WATCH_BUY"):
            qty_str = f"{s['qty']:>6}株買"
        elif sig == "SELL" and sym in positions:
            held = positions[sym]["qty"]
            qty_str = f"{held:>6}株売"
        else:
            qty_str = f"{'─':>7} "

        pos_mark = " [保有中]" if sym in positions else ""
        print(f"  {i:<3} {label:<22} {s['close']:>7,.1f} {ma75_str} {trd_str:>4} "
              f"{s['k']:>6.1f} {s['d']:>6.1f} {s['stop']:>8,.1f} {qty_str:>8}  {sig_lbl}{pos_mark}")

    print()
    buy_count  = sum(1 for s in signals.values() if s["signal"] == "BUY")
    sell_count = sum(1 for s in signals.values() if s["signal"] == "SELL")
    watch_up   = sum(1 for s in signals.values() if s["signal"] == "WATCH_BUY")
    watch_dn   = sum(1 for s in signals.values() if s["signal"] == "WATCH_BUY↓")
    print(f"  買いシグナル: {buy_count}件  売りシグナル: {sell_count}件  "
          f"WATCH↑: {watch_up}件  WATCH↓: {watch_dn}件（75MA回復待ち）")
    print(f"  ※ トレイルストップ = 終値 − ATR × {ATR_TRAIL_MULT}  推奨株数 = 資金{INITIAL_CASH:,}円 × {RISK_PER_TRADE*100:.0f}% ÷ (ATR × {ATR_STOP_MULT})")
    print()


# ── HTML レポート生成 & 自動オープン ──────────────────────────
SIGNAL_BG = {
    "BUY":         "#fff3cd",  # 黄
    "SELL":        "#f8d7da",  # 赤
    "WATCH_BUY":   "#d1ecf1",  # 水色
    "WATCH_BUY↓":  "#e8e8e8",  # グレー（75MA下）
    "WATCH_SELL":  "#fce8d5",  # オレンジ
    "HOLD":        "#ffffff",
}
SIGNAL_BADGE = {
    "BUY":         ('<span style="background:#fd7e14;color:#fff;padding:2px 8px;border-radius:4px;font-weight:bold;">★ BUY</span>', "買いシグナル！（75MA上・トレンド確認済）"),
    "SELL":        ('<span style="background:#dc3545;color:#fff;padding:2px 8px;border-radius:4px;font-weight:bold;">▼ SELL</span>', "売りシグナル！"),
    "WATCH_BUY":   ('<span style="background:#17a2b8;color:#fff;padding:2px 8px;border-radius:4px;">◎ WATCH↑</span>', "過売り圏・上昇トレンド（買い候補）"),
    "WATCH_BUY↓":  ('<span style="background:#888;color:#fff;padding:2px 8px;border-radius:4px;">○ WATCH↓</span>', "過売り圏・75MA下（トレンド回復待ち）"),
    "WATCH_SELL":  ('<span style="background:#fd7e14;color:#fff;padding:2px 8px;border-radius:4px;">△ WATCH</span>', "過買い圏（売り候補）"),
    "HOLD":        ('<span style="color:#888;">HOLD</span>', ""),
}


def generate_html(signals: dict, positions: dict) -> str:
    items = sorted(signals.values(), key=lambda x: SIGNAL_ORDER[x["signal"]])
    date_str = items[0]["date"] if items else datetime.now().strftime("%Y-%m-%d")
    buy_count  = sum(1 for s in signals.values() if s["signal"] == "BUY")
    sell_count = sum(1 for s in signals.values() if s["signal"] == "SELL")

    rows = []
    for i, s in enumerate(items, 1):
        sym  = s["symbol"]
        sig  = s["signal"]
        bg   = SIGNAL_BG[sig]
        badge, note = SIGNAL_BADGE[sig]

        if sig in ("BUY", "WATCH_BUY"):
            qty_html = f'<span style="color:#007bff;font-weight:bold;">{s["qty"]}株買</span>'
        elif sig == "SELL" and sym in positions:
            held = positions[sym]["qty"]
            qty_html = f'<span style="color:#dc3545;font-weight:bold;">{held}株売</span>'
        else:
            qty_html = '<span style="color:#ccc;">─</span>'

        pos_badge = ' <span style="background:#6c757d;color:#fff;font-size:11px;padding:1px 5px;border-radius:3px;">保有中</span>' if sym in positions else ""
        trend_html = ('<span style="color:#28a745;font-weight:bold;">↑</span>'
                      if s.get("trend_up") else
                      '<span style="color:#dc3545;">↓</span>')
        ma75_val = s.get("ma75", 0)
        ma75_html = f"{ma75_val:,.1f}" if ma75_val > 0 else "─"

        rows.append(f"""
        <tr style="background:{bg};">
          <td style="text-align:center;">{i}</td>
          <td>{s["name"]}<br><small style="color:#666;">{sym}</small>{pos_badge}</td>
          <td style="text-align:right;">{s["close"]:,.1f}</td>
          <td style="text-align:right;">{ma75_html}</td>
          <td style="text-align:center;">{trend_html}</td>
          <td style="text-align:right;">{s["k"]:.1f}</td>
          <td style="text-align:right;">{s["d"]:.1f}</td>
          <td style="text-align:right;">{s["stop"]:,.1f}</td>
          <td style="text-align:center;">{qty_html}</td>
          <td>{badge} {note}</td>
        </tr>""")

    rows_html = "\n".join(rows)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── ポジションテーブル ──
    pos_rows = []
    total_pnl = 0.0
    for sym, pos in positions.items():
        entry    = pos["entry_price"]
        qty      = pos["qty"]
        stop     = pos["stop_price"]
        entry_dt = pos["entry_date"]
        info     = signals.get(sym)
        if info:
            cur  = info["close"]
            pnl  = (cur - entry) * qty
            pct  = (cur - entry) / entry * 100
            total_pnl += pnl
            cur_str  = f"{cur:,.1f}"
            pnl_col  = "#28a745" if pnl >= 0 else "#dc3545"
            pnl_str  = f'<span style="color:{pnl_col};font-weight:bold;">{pnl:+,.0f}円</span>'
            pct_str  = f'<span style="color:{pnl_col};">{pct:+.1f}%</span>'
        else:
            cur_str = "─"
            pnl_str = "─"
            pct_str = "─"
        sig_now = signals.get(sym, {}).get("signal", "")
        sig_badge = SIGNAL_BADGE.get(sig_now, ("", ""))
        pos_rows.append(f"""
        <tr>
          <td>{pos["name"]}<br><small style="color:#666;">{sym}</small></td>
          <td style="text-align:center;">{entry_dt}</td>
          <td style="text-align:right;">{entry:,.1f}</td>
          <td style="text-align:right;">{cur_str}</td>
          <td style="text-align:right;">{qty}</td>
          <td style="text-align:right;">{pnl_str}</td>
          <td style="text-align:right;">{pct_str}</td>
          <td style="text-align:right;">{stop:,.1f}</td>
          <td>{sig_badge[0]}</td>
        </tr>""")

    total_col = "#28a745" if total_pnl >= 0 else "#dc3545"
    if pos_rows:
        pos_rows.append(f"""
        <tr style="background:#f8f9fa;font-weight:bold;">
          <td colspan="5" style="text-align:right;">合計損益</td>
          <td style="text-align:right;color:{total_col};">{total_pnl:+,.0f}円</td>
          <td colspan="3"></td>
        </tr>""")
        pos_section = f"""
  <h2 style="font-size:16px;margin-top:32px;margin-bottom:8px;">保有ポジション ({len(positions)}件)</h2>
  <table>
    <thead>
      <tr>
        <th>銘柄</th><th>取得日</th><th>取得価格</th><th>現在値</th><th>株数</th>
        <th>損益</th><th>損益率</th><th>ストップ</th><th>現シグナル</th>
      </tr>
    </thead>
    <tbody>{"".join(pos_rows)}</tbody>
  </table>"""
    else:
        pos_section = '<p style="color:#888;margin-top:32px;">保有ポジションなし</p>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>シグナルモニター [{date_str}]</title>
  <style>
    body {{ font-family: "Hiragino Sans", "Meiryo", sans-serif; background:#f5f5f5; margin:0; padding:20px; }}
    h1 {{ font-size:20px; margin-bottom:4px; }}
    .meta {{ color:#666; font-size:13px; margin-bottom:16px; }}
    .summary {{ display:flex; gap:12px; margin-bottom:16px; }}
    .badge {{ padding:6px 16px; border-radius:6px; font-weight:bold; font-size:14px; }}
    .badge-buy  {{ background:#fff3cd; border:1px solid #fd7e14; color:#fd7e14; }}
    .badge-sell {{ background:#f8d7da; border:1px solid #dc3545; color:#dc3545; }}
    table {{ border-collapse:collapse; width:100%; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.1); }}
    th {{ background:#343a40; color:#fff; padding:10px 12px; text-align:left; font-size:13px; white-space:nowrap; }}
    td {{ padding:9px 12px; border-bottom:1px solid #eee; font-size:13px; white-space:nowrap; }}
    tr:last-child td {{ border-bottom:none; }}
    .note {{ font-size:11px; color:#888; margin-top:12px; }}
  </style>
</head>
<body>
  <h1>A7 シグナル一覧（トレンドフィルター付きストキャスティクス + ATRトレイリングストップ）</h1>
  <div class="meta">基準日: {date_str}　|　生成: {generated}　|　対象: 上位30銘柄（S株・1株単位）　|　トレンドフィルター: {MA_TREND_PERIOD}日MA　|　トレイル倍率: ATR×{ATR_TRAIL_MULT}</div>
  <div class="summary">
    <div class="badge badge-buy">★ 買いシグナル: {buy_count} 件</div>
    <div class="badge badge-sell">▼ 売りシグナル: {sell_count} 件</div>
  </div>
  <table>
    <thead>
      <tr>
        <th>#</th><th>銘柄</th><th>終値</th><th>75MA</th><th>TRD</th><th>%K</th><th>%D</th>
        <th>トレイルStop</th><th>推奨株数</th><th>シグナル</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  <p class="note">※ 推奨株数 = 資金 {INITIAL_CASH:,}円 × {RISK_PER_TRADE*100:.0f}% ÷ (ATR × {ATR_STOP_MULT})</p>
  {pos_section}
</body>
</html>"""


def open_html_report(signals: dict, positions: dict) -> None:
    html = generate_html(signals, positions)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", delete=False,
        encoding="utf-8", prefix="signal_monitor_"
    )
    tmp.write(html)
    tmp.close()
    print(f"  HTMLレポートを開きます: {tmp.name}")
    webbrowser.open(f"file://{tmp.name}")


# ── ヘルプ ────────────────────────────────────────────────────
def show_help() -> None:
    print("""
  ─── コマンド一覧 ───────────────────────────────────────────
  s  / signal                  シグナル再取得・表示
  l  / list                    ポジション一覧
  b  <コード> [株数] [取得価格]  買い記録         例: b 7203.T  または  b 7203 50 3250  （省略→ATR推奨・現在終値）
  u  <コード> <約定価格>       取得価格を更新   例: u 7203.T 3250  または  u 7203 3250
  x  <コード> [株数]           売り記録         例: x 7203.T  または  x 7203 30  （株数省略→全売り）
  h  / help                    このヘルプを表示
  q  / quit / exit             終了
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
    print("  対象: スキャン利益率ランキング上位30銘柄")
    print("  'h' でヘルプ表示")
    print("=" * 62)

    positions = load_positions()
    signals   = fetch_all_signals()
    show_signals(signals, positions)
    open_html_report(signals, positions)

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
            open_html_report(signals, positions)

        elif cmd in ("l", "list"):
            positions = load_positions()
            print()
            print("  ─── 保有ポジション ───")
            show_positions(positions, signals)

        elif cmd in ("b", "buy"):
            if len(parts) < 2:
                print("  使い方: b <銘柄コード> [株数] [取得価格]   例: b 7203.T  または  b 7203 50 3250")
            else:
                positions = load_positions()
                qty_arg   = parts[2] if len(parts) >= 3 else None
                price_arg = parts[3] if len(parts) >= 4 else None
                do_buy(parts[1], signals, positions, qty_arg, price_arg)

        elif cmd in ("x", "sell"):
            if len(parts) < 2:
                print("  使い方: x <銘柄コード> [株数]   例: x 7203.T  または  x 7203 30")
            else:
                positions = load_positions()
                qty_arg = parts[2] if len(parts) >= 3 else None
                do_sell(parts[1], signals, positions, qty_arg)

        elif cmd in ("u", "update"):
            if len(parts) < 3:
                print("  使い方: u <銘柄コード> <約定価格>   例: u 7203.T 3250")
            else:
                positions = load_positions()
                do_update_price(parts[1], parts[2], signals, positions)

        else:
            print(f"  ✗ 不明なコマンド: {cmd}  ('h' でヘルプ)")


if __name__ == "__main__":
    main()
