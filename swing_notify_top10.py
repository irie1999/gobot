"""
スイングトレード 上位10銘柄 一括監視 + Telegram 通知スクリプト
────────────────────────────────────────────────────────────
対象  : scan_best_stock.py で選定した利益率上位10銘柄
戦略  : swing_notify.py と同一ロジック（EMA+RSI+BB+ATR）
通知  : 買い/売り/ストップ シグナル発生時に Telegram 送信

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
  # 即時シグナルチェック（全10銘柄）
  python swing_notify_top10.py now

  # シグナルなしでも日次レポートを送信
  python swing_notify_top10.py now --daily

  # スケジューラー起動（毎営業日 15:35 に自動チェック）
  python swing_notify_top10.py schedule

■ ポジション管理（翌朝注文執行後に手動更新）:
  python swing_notify_top10.py position show
  python swing_notify_top10.py position set 8604.T 850 100 810
  python swing_notify_top10.py position clear 8604.T
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

# ── 監視銘柄（scan_best_stock.py 直近1年 利益率上位10銘柄） ──────
WATCH_LIST = [
    ("8604.T", "野村HD"),
    ("2802.T", "味の素"),
    ("2914.T", "JT"),
    ("7203.T", "トヨタ自動車"),
    ("8001.T", "伊藤忠商事"),
    ("8306.T", "三菱UFJ"),
    ("6752.T", "パナソニックHD"),
    ("8002.T", "丸紅"),
    ("9020.T", "JR東日本"),
    ("6902.T", "デンソー"),
]

# ── Telegram Bot（環境変数推奨） ──────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "your_bot_token_here")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "your_chat_id_here")

# ── ポジションファイル保存ディレクトリ ────────────────────────────
POSITION_DIR = Path(__file__).parent / "positions_top10"

# ── スケジュール実行時刻（大引け後） ─────────────────────────────
RUN_TIME = "15:35"

# ── 戦略パラメータ（scan_best_stock.py と同一） ──────────────────
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
        logging.FileHandler("swing_notify_top10.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Telegram 送信 ────────────────────────────────────────────────
def send_telegram(message: str) -> bool:
    if TELEGRAM_BOT_TOKEN == "your_bot_token_here":
        log.warning("TELEGRAM_BOT_TOKEN が未設定。コンソール出力のみ。")
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
        log.info("Telegram 送信成功")
        return True
    except requests.exceptions.RequestException as e:
        log.error("Telegram 送信失敗: %s", e)
        return False


# ── データ取得 ────────────────────────────────────────────────────
def fetch_data(symbol: str) -> pd.DataFrame:
    import yfinance as yf

    log.info("[yfinance] %s データ取得中...", symbol)
    df = yf.download(symbol, period="max", interval="1d",
                     auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"{symbol}: データが空（ネット接続を確認）")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna()
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

    sma = c.rolling(BB_PERIOD).mean()
    std = c.rolling(BB_PERIOD).std(ddof=0)
    df["bb_upper"] = sma + BB_K * std
    df["bb_lower"] = sma - BB_K * std
    df["bb_band"]  = BB_K * std

    prev_c = c.shift(1)
    tr = pd.concat([h - l,
                    (h - prev_c).abs(),
                    (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    return df


# ── シグナル判定 ───────────────────────────────────────────────
def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    trend_up = df["close"] > df["ema_slow"]
    df["entry_sig"] = trend_up & (
        (df["rsi"] < RSI_ENTRY) |
        df["ema_cross_up"] |
        (df["close"] <= df["bb_lower"] + df["bb_band"])
    )
    df["exit_sig"] = (
        (df["rsi"]      > RSI_EXIT) |
        (df["close"]    >= df["bb_upper"]) |
        (df["ema_fast"] < df["ema_mid"])
    )
    return df


# ── 推奨株数計算 ───────────────────────────────────────────────
def calc_qty(cash: float, atr: float) -> int:
    stop_dist = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    raw = int(cash * RISK_PER_TRADE / stop_dist)
    lots = min(raw // LOT_SIZE, MAX_QTY // LOT_SIZE)
    return lots * LOT_SIZE


# ── ポジション管理 ─────────────────────────────────────────────
def _pos_file(symbol: str) -> Path:
    POSITION_DIR.mkdir(exist_ok=True)
    return POSITION_DIR / f"pos_{symbol.replace('.', '_')}.json"


def load_position(symbol: str) -> dict:
    f = _pos_file(symbol)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"in_pos": False, "entry_price": 0.0, "stop_price": 0.0,
            "qty": 0, "entry_dt": None}


def save_position(symbol: str, pos: dict) -> None:
    _pos_file(symbol).write_text(
        json.dumps(pos, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info("[%s] ポジション保存: %s", symbol, pos)


# ── シグナル理由テキスト ───────────────────────────────────────
def _entry_reasons(row: pd.Series) -> str:
    reasons = []
    if row["rsi"] < RSI_ENTRY:
        reasons.append(f"RSI低値({row['rsi']:.1f}<{RSI_ENTRY})")
    if row["ema_cross_up"]:
        reasons.append("EMAゴールデンクロス")
    if row["close"] <= row["bb_lower"] + row["bb_band"]:
        reasons.append("BB下限タッチ")
    return " / ".join(reasons) if reasons else "複合"


def _exit_reasons(row: pd.Series) -> str:
    reasons = []
    if row["rsi"] > RSI_EXIT:
        reasons.append(f"RSI高値({row['rsi']:.1f}>{RSI_EXIT})")
    if row["close"] >= row["bb_upper"]:
        reasons.append("BB上限タッチ")
    if row["ema_fast"] < row["ema_mid"]:
        reasons.append("EMAデッドクロス")
    return " / ".join(reasons) if reasons else "複合"


# ── メッセージ生成 ─────────────────────────────────────────────
def build_buy_message(symbol: str, name: str,
                      row: pd.Series, today: str, cash: float) -> str:
    close    = row["close"]
    atr      = row["atr"]
    stop     = close - atr * ATR_STOP_MULT
    qty      = calc_qty(cash, atr)
    cost_est = close * qty
    return (
        f"\n【買いシグナル発生】{name}({symbol})\n"
        f"─────────────────────\n"
        f"シグナル日  : {today}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"シグナル理由: {_entry_reasons(row)}\n"
        f"EMA{EMA_SLOW}上 : {'✓ 上昇トレンド中' if close > row['ema_slow'] else '✗'}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 成行買い推奨\n"
        f"推奨株数    : {qty} 株\n"
        f"概算コスト  : {cost_est:,.0f} 円\n"
        f"ストップ目安: {stop:,.0f} 円 (ATR×{ATR_STOP_MULT})\n"
        f"ATR         : {atr:.1f} 円"
    )


def build_sell_message(symbol: str, name: str,
                       row: pd.Series, pos: dict, today: str) -> str:
    close    = row["close"]
    pnl_est  = (close - pos["entry_price"]) * pos["qty"] if pos["qty"] > 0 else 0
    sign     = "+" if pnl_est >= 0 else ""
    entry_dt = pos.get("entry_dt") or "不明"
    return (
        f"\n【売りシグナル発生】{name}({symbol})\n"
        f"─────────────────────\n"
        f"シグナル日  : {today}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"シグナル理由: {_exit_reasons(row)}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 成行売り推奨\n"
        f"保有株数    : {pos['qty']} 株\n"
        f"取得値      : {pos['entry_price']:,.0f} 円\n"
        f"含み損益    : {sign}{pnl_est:,.0f} 円\n"
        f"取得日      : {entry_dt}"
    )


def build_stop_message(symbol: str, name: str,
                       row: pd.Series, pos: dict, today: str) -> str:
    close    = row["close"]
    pnl_est  = (close - pos["entry_price"]) * pos["qty"] if pos["qty"] > 0 else 0
    sign     = "+" if pnl_est >= 0 else ""
    return (
        f"\n【ストップ到達】{name}({symbol})\n"
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


def build_no_signal_message(symbol: str, name: str,
                             row: pd.Series, pos: dict, today: str) -> str:
    close = row["close"]
    if pos["in_pos"]:
        pnl_est = (close - pos["entry_price"]) * pos["qty"]
        sign    = "+" if pnl_est >= 0 else ""
        return (
            f"\n【日次レポート】{name}({symbol})\n"
            f"─────────────────────\n"
            f"日付    : {today}\n"
            f"終値    : {close:,.0f} 円\n"
            f"RSI     : {row['rsi']:.1f}\n"
            f"シグナル: なし（ポジション保持）\n"
            f"含み損益: {sign}{pnl_est:,.0f} 円"
        )
    return (
        f"\n【日次レポート】{name}({symbol})\n"
        f"─────────────────────\n"
        f"日付    : {today}\n"
        f"終値    : {close:,.0f} 円\n"
        f"RSI     : {row['rsi']:.1f}\n"
        f"シグナル: なし（待機中）"
    )


# ── 1銘柄チェック ─────────────────────────────────────────────
def check_one(symbol: str, name: str, daily_report: bool = False) -> None:
    log.info("[%s %s] チェック開始", symbol, name)

    try:
        df = fetch_data(symbol)
    except Exception as e:
        log.error("[%s] データ取得失敗: %s", symbol, e)
        send_telegram(f"\n[エラー] {name}({symbol}) データ取得失敗\n{e}")
        return

    df = add_indicators(df)
    df = add_signals(df)

    row   = df.iloc[-1]
    today = df.index[-1].strftime("%Y-%m-%d(%a)")

    if any(pd.isna([row["rsi"], row["ema_slow"], row["atr"],
                    row["bb_upper"], row["bb_lower"]])):
        log.warning("[%s] インジケーター計算不足", symbol)
        return

    pos = load_position(symbol)
    log.info("[%s] 終値:%.1f  RSI:%.1f  EMA%d:%.1f  ATR:%.1f  ポジション:%s",
             symbol, row["close"], row["rsi"], EMA_SLOW, row["ema_slow"], row["atr"],
             "保有中" if pos["in_pos"] else "なし")

    message = None

    if pos["in_pos"]:
        if row["low"] <= pos["stop_price"]:
            log.info("[%s] ストップ到達: 安値%.1f <= ストップ%.1f",
                     symbol, row["low"], pos["stop_price"])
            message = build_stop_message(symbol, name, row, pos, today)
        elif row["exit_sig"]:
            log.info("[%s] 売りシグナル発生", symbol)
            message = build_sell_message(symbol, name, row, pos, today)
    else:
        if row["entry_sig"]:
            log.info("[%s] 買いシグナル発生", symbol)
            message = build_buy_message(symbol, name, row, today, INITIAL_CASH)

    if message:
        send_telegram(message)
    elif daily_report:
        send_telegram(build_no_signal_message(symbol, name, row, pos, today))
    else:
        log.info("[%s] シグナルなし → 通知なし", symbol)


# ── 全銘柄チェック ─────────────────────────────────────────────
def check_all(daily_report: bool = False) -> None:
    now = datetime.now()
    if now.weekday() >= 5:
        log.info("土日のためスキップ")
        return

    log.info("===== 全%d銘柄 シグナルチェック開始 =====", len(WATCH_LIST))
    for symbol, name in WATCH_LIST:
        check_one(symbol, name, daily_report=daily_report)
        time.sleep(1.0)  # API レート制限回避
    log.info("===== チェック完了 =====")


# ── ポジション操作コマンド ─────────────────────────────────────
def cmd_set_position(symbol: str, price: float, qty: int, stop: float) -> None:
    name = dict(WATCH_LIST).get(symbol, symbol)
    pos = {
        "in_pos":      True,
        "entry_price": price,
        "stop_price":  stop,
        "qty":         qty,
        "entry_dt":    datetime.now().strftime("%Y-%m-%d"),
    }
    save_position(symbol, pos)
    print(f"[{symbol} {name}] ポジション登録: {qty}株 @ {price:,.0f}円  "
          f"ストップ: {stop:,.0f}円")


def cmd_clear_position(symbol: str) -> None:
    name = dict(WATCH_LIST).get(symbol, symbol)
    pos = {"in_pos": False, "entry_price": 0.0, "stop_price": 0.0,
           "qty": 0, "entry_dt": None}
    save_position(symbol, pos)
    print(f"[{symbol} {name}] ポジションをクリアしました")


def cmd_show_positions() -> None:
    print("\n現在のポジション一覧:")
    print("-" * 60)
    for symbol, name in WATCH_LIST:
        pos = load_position(symbol)
        if pos["in_pos"]:
            print(f"  {symbol:8}  {name:16}  "
                  f"{pos['qty']}株 @ {pos['entry_price']:,.0f}円  "
                  f"ストップ: {pos['stop_price']:,.0f}円  "
                  f"取得日: {pos.get('entry_dt', '不明')}")
        else:
            print(f"  {symbol:8}  {name:16}  待機中")
    print("-" * 60)


# ── CLI エントリーポイント ─────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="スイングトレード 上位10銘柄 一括監視 + Telegram 通知")
    sub = parser.add_subparsers(dest="cmd")

    p_now = sub.add_parser("now", help="今すぐ全銘柄チェック")
    p_now.add_argument("--daily", action="store_true",
                       help="シグナルなしでも日次レポートを送信")

    sub.add_parser("schedule", help=f"スケジューラー起動（毎営業日 {RUN_TIME}）")

    p_pos = sub.add_parser("position", help="ポジション管理")
    pos_sub = p_pos.add_subparsers(dest="pos_cmd")
    pos_sub.add_parser("show", help="全銘柄のポジションを表示")

    p_clear = pos_sub.add_parser("clear", help="ポジションをクリア（売り執行後）")
    p_clear.add_argument("symbol", help="銘柄コード（例: 8604.T）")

    p_set = pos_sub.add_parser("set", help="ポジションを登録（買い執行後）")
    p_set.add_argument("symbol", help="銘柄コード（例: 8604.T）")
    p_set.add_argument("price",  type=float, help="取得価格")
    p_set.add_argument("qty",    type=int,   help="株数")
    p_set.add_argument("stop",   type=float, help="ストップロス価格")

    args = parser.parse_args()

    if args.cmd is None or args.cmd == "schedule":
        log.info("スケジューラー起動: 毎営業日 %s にチェック実行", RUN_TIME)
        schedule.every().day.at(RUN_TIME).do(check_all)
        while True:
            schedule.run_pending()
            time.sleep(30)

    elif args.cmd == "now":
        check_all(daily_report=getattr(args, "daily", False))

    elif args.cmd == "position":
        if args.pos_cmd == "show":
            cmd_show_positions()
        elif args.pos_cmd == "clear":
            cmd_clear_position(args.symbol)
        elif args.pos_cmd == "set":
            cmd_set_position(args.symbol, args.price, args.qty, args.stop)
        else:
            p_pos.print_help()
