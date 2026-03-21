"""
スイングトレード バックテスト【Telegram通知テスト版】
銘柄  : 味の素 (2802.T)
目的  : 先週の日次データを1日ずつ再生し、買いシグナル発生時に Telegram 通知を送る
        → 本番稼働前の動作確認・通知テスト用

■ 事前準備:
  環境変数に Token と Chat ID をセット
    export TELEGRAM_BOT_TOKEN="xxxx"
    export TELEGRAM_CHAT_ID="123456789"

■ 実行方法:
  python swing_backtest_notify.py
"""

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

# ── 設定 ─────────────────────────────────────────────────────────
SYMBOL       = "2802.T"
SYMBOL_NAME  = "味の素"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# 先週（月〜金）の範囲を自動計算
today = datetime.today()
# 直近の月曜日を起点に「先週月曜〜先週金曜」を算出
last_monday = today - timedelta(days=today.weekday() + 7)
last_friday = last_monday + timedelta(days=4)
BACKTEST_START = last_monday.strftime("%Y-%m-%d")
BACKTEST_END   = last_friday.strftime("%Y-%m-%d")

# ── 戦略パラメータ（swing_notify.py と同一） ────────────────────
EMA_FAST      = 5
EMA_MID       = 20
EMA_SLOW      = 50
RSI_PERIOD    = 14
RSI_ENTRY     = 55
RSI_EXIT      = 60
BB_PERIOD     = 20
BB_K          = 2.0
ATR_PERIOD    = 14
ATR_STOP_MULT = 1.5
INITIAL_CASH  = 500_000
LOT_SIZE      = 100
MAX_QTY       = 500


# ── Telegram 送信 ────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        print("\n[Telegram未設定] コンソールに出力します:")
        print("=" * 50)
        print(message)
        print("=" * 50)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        print("  → Telegram 送信成功")
        return True
    except requests.exceptions.RequestException as e:
        print(f"  → Telegram 送信失敗: {e}")
        return False


# ── データ取得（インジケーター計算に必要な十分な過去データも含む） ──
def fetch_data() -> pd.DataFrame:
    import yfinance as yf

    # インジケーター計算のため先週より十分前から取得（200日分）
    fetch_start = (datetime.strptime(BACKTEST_START, "%Y-%m-%d")
                   - timedelta(days=200)).strftime("%Y-%m-%d")
    fetch_end   = (datetime.strptime(BACKTEST_END, "%Y-%m-%d")
                   + timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"[yfinance] {SYMBOL} データ取得中 ({fetch_start} 〜 {BACKTEST_END})...")
    df = yf.download(SYMBOL, start=fetch_start, end=fetch_end,
                     interval="1d", auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError("データが取得できませんでした")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    print(f"  取得成功: {len(df)} 本 ({df.index[0].date()} 〜 {df.index[-1].date()})\n")
    return df


# ── インジケーター計算 ─────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

    df["ema_fast"] = c.ewm(span=EMA_FAST,  adjust=False).mean()
    df["ema_mid"]  = c.ewm(span=EMA_MID,   adjust=False).mean()
    df["ema_slow"] = c.ewm(span=EMA_SLOW,  adjust=False).mean()

    df["ema_cross_up"] = (
        (df["ema_fast"] > df["ema_mid"]) &
        (df["ema_fast"].shift(1) <= df["ema_mid"].shift(1))
    )

    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

    sma        = c.rolling(BB_PERIOD).mean()
    std        = c.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = sma + BB_K * std
    df["bb_lower"] = sma - BB_K * std
    df["bb_band"]  = BB_K * std

    prev_c = c.shift(1)
    tr     = pd.concat([h - l,
                        (h - prev_c).abs(),
                        (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── シグナル判定 ───────────────────────────────────────────────
def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    trend_up  = df["close"] > df["ema_slow"]
    sig_rsi   = df["rsi"] < RSI_ENTRY
    sig_cross = df["ema_cross_up"]
    sig_bb    = df["close"] <= df["bb_lower"] + df["bb_band"]

    df["entry_sig"] = trend_up & (sig_rsi | sig_cross | sig_bb)
    df["exit_sig"]  = (
        (df["rsi"]      > RSI_EXIT) |
        (df["close"]    >= df["bb_upper"]) |
        (df["ema_fast"] < df["ema_mid"])
    )
    return df


# ── 推奨株数計算 ───────────────────────────────────────────────
def calc_qty(cash: float, atr: float) -> int:
    risk_amount = cash * 0.03
    stop_dist   = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    raw_qty = int(risk_amount / stop_dist)
    lots    = min(raw_qty // LOT_SIZE, MAX_QTY // LOT_SIZE)
    return lots * LOT_SIZE


# ── 買いシグナル通知メッセージ生成 ─────────────────────────────
def build_buy_message(row: pd.Series, date_str: str) -> str:
    close    = row["close"]
    atr      = row["atr"]
    stop     = close - atr * ATR_STOP_MULT
    qty      = calc_qty(INITIAL_CASH, atr)
    cost_est = close * qty

    reasons = []
    if row["rsi"] < RSI_ENTRY:
        reasons.append(f"RSI低値({row['rsi']:.1f} < {RSI_ENTRY})")
    if row["ema_cross_up"]:
        reasons.append("EMAゴールデンクロス")
    if row["close"] <= row["bb_lower"] + row["bb_band"]:
        reasons.append("BB下限タッチ")
    reason_str = " / ".join(reasons) if reasons else "複合"

    return (
        f"\n【買いシグナル発生】{SYMBOL_NAME}({SYMBOL})\n"
        f"[バックテスト再現]\n"
        f"─────────────────────\n"
        f"シグナル日  : {date_str}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"シグナル理由: {reason_str}\n"
        f"EMA{EMA_SLOW}上  : {'✓ 上昇トレンド中' if row['close'] > row['ema_slow'] else '✗'}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 成行買い推奨\n"
        f"推奨株数    : {qty} 株\n"
        f"概算コスト  : {cost_est:,.0f} 円\n"
        f"ストップ目安: {stop:,.0f} 円 (ATR×{ATR_STOP_MULT})\n"
        f"ATR         : {atr:.1f} 円"
    )


# ── メイン ─────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  スイングトレード バックテスト Telegram 通知テスト")
    print(f"  銘柄   : {SYMBOL_NAME} ({SYMBOL})")
    print(f"  期間   : {BACKTEST_START} 〜 {BACKTEST_END}（先週）")
    print("=" * 60)

    # トークン確認
    if not TELEGRAM_BOT_TOKEN:
        print("\n[警告] TELEGRAM_BOT_TOKEN が未設定です。")
        print("  コンソールにプレビュー表示します。\n")
    else:
        print(f"\n  Telegram Bot Token: 設定済み")
        print(f"  Chat ID: {TELEGRAM_CHAT_ID}\n")

    # データ取得・インジケーター計算
    df = fetch_data()
    df = add_indicators(df)
    df = add_signals(df)

    # 先週分のみ抽出
    mask = (df.index >= BACKTEST_START) & (df.index <= BACKTEST_END)
    week_df = df[mask]

    if week_df.empty:
        print(f"先週（{BACKTEST_START} 〜 {BACKTEST_END}）のデータがありません。")
        print("休場週の可能性があります。")
        sys.exit(1)

    print(f"先週の営業日: {len(week_df)} 日\n")
    print("-" * 60)

    buy_count = 0

    for dt, row in week_df.iterrows():
        date_str = dt.strftime("%Y-%m-%d(%a)")

        # NaN チェック
        if any(pd.isna([row["rsi"], row["ema_slow"], row["atr"],
                        row["bb_upper"], row["bb_lower"]])):
            print(f"{date_str}  インジケーター計算中（データ不足）")
            continue

        entry = row["entry_sig"]
        exit_ = row["exit_sig"]

        status = []
        if entry:
            status.append("買いシグナル")
        if exit_:
            status.append("売りシグナル")
        if not status:
            status.append("シグナルなし")

        print(f"{date_str}  終値:{row['close']:,.0f}円  RSI:{row['rsi']:.1f}  "
              f"→ {' / '.join(status)}")

        # 買いシグナル発生 → Telegram 通知
        if entry:
            buy_count += 1
            msg = build_buy_message(row, date_str)
            send_telegram(msg)

    print("-" * 60)
    print(f"\n先週の買いシグナル発生回数: {buy_count} 回")

    if buy_count == 0:
        print("\n先週は買いシグナルが発生しませんでした。")
        print("動作テストとして「テスト通知」を送ります...")
        test_msg = (
            f"\n【テスト通知】{SYMBOL_NAME}({SYMBOL})\n"
            f"─────────────────────\n"
            f"バックテスト期間 {BACKTEST_START} 〜 {BACKTEST_END}\n"
            f"先週は買いシグナルなし\n"
            f"Telegram 通知の動作確認完了 ✓"
        )
        send_telegram(test_msg)

    print("\n完了。スマホの Telegram を確認してください。")


if __name__ == "__main__":
    main()
