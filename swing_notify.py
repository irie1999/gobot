"""
スイングトレード シグナル通知スクリプト【Telegram Bot版】
銘柄  : 味の素 (2802.T)  ← SYMBOL を変えれば他銘柄でも動作
戦略  : swing_backtest_ajinomoto.py と同一ロジック
執行  : シグナル発生 → Telegram に通知 → 翌朝に人間が成行注文

■ 事前準備:
  1. Telegram Bot を作成
       Telegram で @BotFather を開き /newbot を送信
       → API Token が発行される（例: 7123456789:AAFxxx...）

  2. Chat ID を取得
       作成した Bot に何かメッセージを送った後、ブラウザで以下を開く:
       https://api.telegram.org/bot<TOKEN>/getUpdates
       → "chat":{"id": の数字が Chat ID（例: 123456789）

  3. 環境変数にセット
       export TELEGRAM_BOT_TOKEN="7123456789:AAFxxx..."
       export TELEGRAM_CHAT_ID="123456789"

  4. 依存ライブラリのインストール
       pip install yfinance pandas numpy requests schedule

■ 実行方法:
  # 即時シグナルチェック（手動実行・テスト用）
  python swing_notify.py now

  # スケジューラー起動（毎営業日 15:35 に自動チェック）
  python swing_notify.py schedule

■ ポジション管理（翌朝注文執行後に手動更新）:
  python swing_notify.py position set 2850 100 2710  # 買い執行後
  python swing_notify.py position clear              # 売り執行後
  python swing_notify.py position show               # 確認
"""

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import schedule

# ── 設定 ─────────────────────────────────────────────────────────
SYMBOL        = "2802.T"   # 味の素
SYMBOL_NAME   = "味の素"

# Telegram Bot（環境変数推奨）
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "your_bot_token_here")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "your_chat_id_here")

# ポジション状態ファイル
POSITION_FILE = Path(__file__).parent / "position.json"

# スケジュール実行時刻（大引け後）
RUN_TIME = "15:35"

# ── 戦略パラメータ（swing_backtest_ajinomoto.py と同一） ────────
EMA_FAST        = 5
EMA_MID         = 20
EMA_SLOW        = 50
RSI_PERIOD      = 14
RSI_ENTRY       = 55
RSI_EXIT        = 60
BB_PERIOD       = 20
BB_K            = 2.0
ATR_PERIOD      = 14
ATR_STOP_MULT   = 1.5
RISK_PER_TRADE  = 0.03
INITIAL_CASH    = 500_000
LOT_SIZE        = 100
MAX_QTY         = 500

# ── ロギング ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("swing_notify.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Telegram 送信 ────────────────────────────────────────────────
def send_line(message: str) -> bool:
    """Telegram Bot にメッセージを送信する。成功で True を返す。"""
    if TELEGRAM_BOT_TOKEN == "your_bot_token_here":
        log.warning("TELEGRAM_BOT_TOKEN が未設定です。コンソール出力のみ行います。")
        print("\n" + "=" * 50)
        print("[Telegram 通知プレビュー]")
        print(message)
        print("=" * 50 + "\n")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Telegram 通知送信成功")
        return True
    except requests.exceptions.RequestException as e:
        log.error("Telegram 通知失敗: %s", e)
        return False


# ── データ取得 ────────────────────────────────────────────────────
def fetch_data() -> pd.DataFrame:
    import yfinance as yf

    log.info("[yfinance] %s データ取得中...", SYMBOL)
    df = yf.download(SYMBOL, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError("データが空でした（ネット接続を確認してください）")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    log.info("取得成功: %d 本 (%s 〜 %s)",
             len(df), df.index[0].date(), df.index[-1].date())
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

    delta  = c.diff()
    gain   = delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    loss   = (-delta.clip(upper=0)).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

    sma         = c.rolling(BB_PERIOD).mean()
    std         = c.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = sma + BB_K * std
    df["bb_lower"] = sma - BB_K * std
    df["bb_band"]  = BB_K * std

    prev_c   = c.shift(1)
    tr       = pd.concat([h - l,
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
    risk_amount = cash * RISK_PER_TRADE
    stop_dist   = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    raw_qty = int(risk_amount / stop_dist)
    lots    = min(raw_qty // LOT_SIZE, MAX_QTY // LOT_SIZE)
    return lots * LOT_SIZE


# ── ポジション管理（JSON ファイル） ───────────────────────────
def load_position() -> dict:
    """ポジション情報を読み込む。ファイルがなければ「未保有」を返す。"""
    if POSITION_FILE.exists():
        try:
            return json.loads(POSITION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"in_pos": False, "entry_price": 0.0, "stop_price": 0.0,
            "qty": 0, "entry_dt": None}


def save_position(pos: dict) -> None:
    POSITION_FILE.write_text(
        json.dumps(pos, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info("ポジション保存: %s", pos)


# ── メッセージ生成 ─────────────────────────────────────────────
def _fmt_entry_reasons(row: pd.Series) -> str:
    reasons = []
    if row["rsi"] < RSI_ENTRY:
        reasons.append(f"RSI低値({row['rsi']:.1f} < {RSI_ENTRY})")
    if row["ema_cross_up"]:
        reasons.append("EMAゴールデンクロス")
    if row["close"] <= row["bb_lower"] + row["bb_band"]:
        reasons.append("BB下限タッチ")
    return " / ".join(reasons) if reasons else "複合"


def _fmt_exit_reasons(row: pd.Series) -> str:
    reasons = []
    if row["rsi"] > RSI_EXIT:
        reasons.append(f"RSI高値({row['rsi']:.1f} > {RSI_EXIT})")
    if row["close"] >= row["bb_upper"]:
        reasons.append("BB上限タッチ")
    if row["ema_fast"] < row["ema_mid"]:
        reasons.append("EMAデッドクロス")
    return " / ".join(reasons) if reasons else "複合"


def build_buy_message(row: pd.Series, today: str) -> str:
    close      = row["close"]
    atr        = row["atr"]
    stop       = close - atr * ATR_STOP_MULT
    qty        = calc_qty(INITIAL_CASH, atr)
    cost_est   = close * qty
    reasons    = _fmt_entry_reasons(row)

    return (
        f"\n【買いシグナル発生】{SYMBOL_NAME}({SYMBOL})\n"
        f"─────────────────────\n"
        f"シグナル日  : {today}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"シグナル理由: {reasons}\n"
        f"EMA{EMA_SLOW}上 : {'✓ 上昇トレンド中' if row['close'] > row['ema_slow'] else '✗'}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 成行買い推奨\n"
        f"推奨株数    : {qty} 株\n"
        f"概算コスト  : {cost_est:,.0f} 円\n"
        f"ストップ目安: {stop:,.0f} 円 (ATR×{ATR_STOP_MULT})\n"
        f"ATR         : {atr:.1f} 円"
    )


def build_sell_message(row: pd.Series, pos: dict, today: str) -> str:
    close    = row["close"]
    reasons  = _fmt_exit_reasons(row)
    pnl_est  = (close - pos["entry_price"]) * pos["qty"] if pos["qty"] > 0 else 0
    sign     = "+" if pnl_est >= 0 else ""
    entry_dt = pos.get("entry_dt") or "不明"

    return (
        f"\n【売りシグナル発生】{SYMBOL_NAME}({SYMBOL})\n"
        f"─────────────────────\n"
        f"シグナル日  : {today}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"シグナル理由: {reasons}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 成行売り推奨\n"
        f"保有株数    : {pos['qty']} 株\n"
        f"取得値      : {pos['entry_price']:,.0f} 円\n"
        f"含み損益    : {sign}{pnl_est:,.0f} 円\n"
        f"取得日      : {entry_dt}"
    )


def build_stop_message(row: pd.Series, pos: dict, today: str) -> str:
    close   = row["close"]
    pnl_est = (close - pos["entry_price"]) * pos["qty"] if pos["qty"] > 0 else 0
    sign    = "+" if pnl_est >= 0 else ""

    return (
        f"\n【ストップ到達】{SYMBOL_NAME}({SYMBOL})\n"
        f"─────────────────────\n"
        f"シグナル日    : {today}\n"
        f"本日安値      : {row['low']:,.0f} 円\n"
        f"ストップ価格  : {pos['stop_price']:,.0f} 円\n"
        f"─────────────────────\n"
        f"⚠ 翌朝 成行売り推奨（損切り）\n"
        f"保有株数      : {pos['qty']} 株\n"
        f"取得値        : {pos['entry_price']:,.0f} 円\n"
        f"概算損益      : {sign}{pnl_est:,.0f} 円"
    )


def build_no_signal_message(row: pd.Series, pos: dict, today: str) -> str:
    close = row["close"]
    if pos["in_pos"]:
        pnl_est = (close - pos["entry_price"]) * pos["qty"]
        sign    = "+" if pnl_est >= 0 else ""
        return (
            f"\n【日次レポート】{SYMBOL_NAME}({SYMBOL})\n"
            f"─────────────────────\n"
            f"日付    : {today}\n"
            f"終値    : {close:,.0f} 円\n"
            f"RSI     : {row['rsi']:.1f}\n"
            f"シグナル: なし（ポジション保持）\n"
            f"含み損益: {sign}{pnl_est:,.0f} 円"
        )
    return (
        f"\n【日次レポート】{SYMBOL_NAME}({SYMBOL})\n"
        f"─────────────────────\n"
        f"日付    : {today}\n"
        f"終値    : {close:,.0f} 円\n"
        f"RSI     : {row['rsi']:.1f}\n"
        f"シグナル: なし（待機中）"
    )


# ── メインチェック処理 ─────────────────────────────────────────
def check_and_notify(daily_report: bool = False) -> None:
    """
    シグナルチェックを実行し Telegram に通知する。
    daily_report=True の場合、シグナルなしでも日次レポートを送信。
    """
    now = datetime.now()
    if now.weekday() >= 5:
        log.info("土日のためスキップ")
        return

    log.info("シグナルチェック開始")

    try:
        df = fetch_data()
    except Exception as e:
        log.error("データ取得失敗: %s", e)
        send_line(f"\n[エラー] {SYMBOL_NAME} データ取得失敗\n{e}")
        return

    df = add_indicators(df)
    df = add_signals(df)

    # 最新の確定足（本日分）
    row   = df.iloc[-1]
    today = df.index[-1].strftime("%Y-%m-%d(%a)")

    if any(pd.isna([row["rsi"], row["ema_slow"], row["atr"],
                    row["bb_upper"], row["bb_lower"]])):
        log.warning("インジケーター計算不足（データ不足）")
        return

    pos = load_position()
    log.info("終値: %.1f  RSI: %.1f  EMA%d: %.1f  ATR: %.1f  "
             "ポジション: %s",
             row["close"], row["rsi"], EMA_SLOW, row["ema_slow"], row["atr"],
             "保有中" if pos["in_pos"] else "なし")

    message = None

    if pos["in_pos"]:
        # ── ストップロス判定 ────────────────────────────────
        if row["low"] <= pos["stop_price"]:
            log.info("ストップ到達: 安値 %.1f <= ストップ %.1f",
                     row["low"], pos["stop_price"])
            message = build_stop_message(row, pos, today)
            # ポジション状態はクリアせず「翌朝執行後」に手動更新を促す

        # ── シグナルエグジット ───────────────────────────────
        elif row["exit_sig"]:
            log.info("売りシグナル発生")
            message = build_sell_message(row, pos, today)

    else:
        # ── エントリーシグナル ───────────────────────────────
        if row["entry_sig"]:
            log.info("買いシグナル発生")
            message = build_buy_message(row, today)

    if message:
        send_line(message)
    elif daily_report:
        send_line(build_no_signal_message(row, pos, today))
    else:
        log.info("シグナルなし → 通知なし")

    log.info("シグナルチェック完了")


# ── ポジション操作コマンド ─────────────────────────────────────
def cmd_set_position(price: float, qty: int, stop: float) -> None:
    """買い注文執行後に手動でポジションを登録する"""
    pos = {
        "in_pos":       True,
        "entry_price":  price,
        "stop_price":   stop,
        "qty":          qty,
        "entry_dt":     datetime.now().strftime("%Y-%m-%d"),
    }
    save_position(pos)
    print(f"ポジション登録完了: {qty}株 @ {price:,.0f}円  ストップ: {stop:,.0f}円")


def cmd_clear_position() -> None:
    """売り注文執行後にポジションをクリアする"""
    pos = {"in_pos": False, "entry_price": 0.0, "stop_price": 0.0,
           "qty": 0, "entry_dt": None}
    save_position(pos)
    print("ポジションをクリアしました")


def cmd_show_position() -> None:
    """現在のポジションを表示する"""
    pos = load_position()
    if pos["in_pos"]:
        print(f"保有中: {pos['qty']}株 @ {pos['entry_price']:,.0f}円  "
              f"ストップ: {pos['stop_price']:,.0f}円  "
              f"取得日: {pos.get('entry_dt', '不明')}")
    else:
        print("ポジションなし（待機中）")


# ── CLI エントリーポイント ─────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="スイングトレード Telegram 通知ツール")
    sub = parser.add_subparsers(dest="cmd")

    # --now: 即時チェック
    p_now = sub.add_parser("now", help="今すぐシグナルチェックして通知")
    p_now.add_argument("--daily", action="store_true",
                       help="シグナルなしでも日次レポートを送信")

    # --schedule: スケジューラー起動
    sub.add_parser("schedule", help=f"スケジューラー起動（毎営業日 {RUN_TIME}）")

    # position サブコマンド
    p_pos = sub.add_parser("position", help="ポジション管理")
    pos_sub = p_pos.add_subparsers(dest="pos_cmd")
    pos_sub.add_parser("show", help="現在のポジションを表示")
    pos_sub.add_parser("clear", help="ポジションをクリア（売り執行後）")
    p_set = pos_sub.add_parser("set", help="ポジションを登録（買い執行後）")
    p_set.add_argument("price", type=float, help="取得価格")
    p_set.add_argument("qty",   type=int,   help="株数")
    p_set.add_argument("stop",  type=float, help="ストップロス価格")

    args = parser.parse_args()

    # デフォルト（引数なし）= スケジューラー起動
    if args.cmd is None or args.cmd == "schedule":
        log.info("スケジューラー起動: 毎営業日 %s にチェック実行", RUN_TIME)
        schedule.every().day.at(RUN_TIME).do(check_and_notify)
        while True:
            schedule.run_pending()
            time.sleep(30)

    elif args.cmd == "now":
        check_and_notify(daily_report=getattr(args, "daily", False))

    elif args.cmd == "position":
        if args.pos_cmd == "show":
            cmd_show_position()
        elif args.pos_cmd == "clear":
            cmd_clear_position()
        elif args.pos_cmd == "set":
            cmd_set_position(args.price, args.qty, args.stop)
        else:
            p_pos.print_help()
