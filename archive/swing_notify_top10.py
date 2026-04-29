"""
スイングトレード 上位10銘柄 一括監視 + Telegram 通知スクリプト
────────────────────────────────────────────────────────────
対象  : scan_best_stock.py で選定した利益率上位10銘柄
戦略  : swing_notify.py と同一ロジック（EMA+RSI+BB+ATR）
通知  : 買い/売り/ストップ シグナル発生時に Telegram 送信

■ SBI証券との連携:
  SBI証券には自動発注APIがないため、以下のフローで半自動化します。
    1. 毎営業日15:35 → シグナルチェック → Telegram 通知
    2. 通知に「✅ 翌朝 成行買い済み」ボタンを表示
    3. SBI証券アプリで翌朝成行注文を手動実行
    4. Telegram のボタンをタップ → ポジションファイルが自動更新

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
  # スケジューラー起動（毎営業日 15:35 チェック + ボタン応答を常時待機）
  python swing_notify_top10.py schedule

  # 即時シグナルチェック（テスト用）
  python swing_notify_top10.py now

  # シグナルなしでも日次レポートを送信
  python swing_notify_top10.py now --daily

■ ポジション管理（緊急時・確認用）:
  python swing_notify_top10.py position show
  python swing_notify_top10.py position set 8604.T 850 100 810
  python swing_notify_top10.py position clear 8604.T
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
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
RISK_PER_TRADE  = 0.03   # 1トレードで許容する損失の割合（口座残高比）
INITIAL_CASH    = 500_000
# S株（1株単位）設定
MAX_COST_RATIO  = 0.10   # 1銘柄に投入する資金の上限（口座残高比）※10銘柄分散想定
MAX_QTY         = 3000   # 最大株数（上限ガード）

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

_WATCH_DICT = dict(WATCH_LIST)


# ── Telegram 送信 ────────────────────────────────────────────────
def _is_token_set() -> bool:
    return TELEGRAM_BOT_TOKEN not in ("your_bot_token_here", "")


def send_telegram(message: str) -> bool:
    """シンプルなテキストメッセージを送信する。"""
    if not _is_token_set():
        log.warning("TELEGRAM_BOT_TOKEN 未設定。コンソール出力のみ。")
        print("\n[Telegram プレビュー]\n" + message + "\n")
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


def send_telegram_keyboard(message: str, buttons: list[list[dict]]) -> bool:
    """インラインキーボードボタン付きメッセージを送信する。"""
    if not _is_token_set():
        log.warning("TELEGRAM_BOT_TOKEN 未設定。コンソール出力のみ。")
        print("\n[Telegram プレビュー（ボタン付き）]\n" + message)
        for row in buttons:
            for btn in row:
                print(f"  [{btn['text']}]")
        print()
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "reply_markup": {"inline_keyboard": buttons},
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Telegram 送信成功（ボタン付き）")
        return True
    except requests.exceptions.RequestException as e:
        log.error("Telegram 送信失敗: %s", e)
        return False


def answer_callback_query(callback_query_id: str, text: str = "✅ 完了") -> None:
    """ボタン押下後のローディングインジケーターを消す。"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(
            url,
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


# ── Telegram コールバック処理 ─────────────────────────────────────
def handle_callback(callback_query: dict) -> None:
    """
    ボタンタップ時のコールバックを処理する。
    callback_data フォーマット:
      BUY_DONE:{symbol}:{close:.0f}:{qty}:{stop:.0f}
      SELL_DONE:{symbol}
      STOP_DONE:{symbol}
    """
    data   = callback_query.get("data", "")
    cq_id  = callback_query["id"]
    parts  = data.split(":")
    action = parts[0] if parts else ""

    log.info("コールバック受信: %s", data)

    if action == "BUY_DONE" and len(parts) == 5:
        symbol = parts[1]
        try:
            price = float(parts[2])
            qty   = int(parts[3])
            stop  = float(parts[4])
        except ValueError:
            answer_callback_query(cq_id, "❌ データ解析エラー")
            return
        name = _WATCH_DICT.get(symbol, symbol)
        pos = {
            "in_pos":      True,
            "entry_price": price,
            "stop_price":  stop,
            "qty":         qty,
            "entry_dt":    datetime.now().strftime("%Y-%m-%d"),
        }
        save_position(symbol, pos)
        answer_callback_query(cq_id, "✅ ポジション登録完了")
        send_telegram(
            f"✅ ポジション自動登録\n"
            f"{name}({symbol})\n"
            f"─────────────────────\n"
            f"株数        : {qty} 株\n"
            f"取得価格    : {price:,.0f} 円（シグナル日終値）\n"
            f"ストップ    : {stop:,.0f} 円\n"
            f"登録日      : {pos['entry_dt']}\n"
            f"─────────────────────\n"
            f"※ 実際の取得価格と異なる場合は\n"
            f"  position set コマンドで修正してください"
        )
        log.info("[%s] ポジション自動登録: %d株 @ %.0f円", symbol, qty, price)

    elif action == "SELL_DONE" and len(parts) == 2:
        symbol = parts[1]
        name   = _WATCH_DICT.get(symbol, symbol)
        pos    = load_position(symbol)
        entry_price = pos.get("entry_price", 0)
        qty         = pos.get("qty", 0)
        clear_position(symbol)
        answer_callback_query(cq_id, "✅ ポジションクリア完了")
        send_telegram(
            f"✅ 売り注文済み → ポジションクリア\n"
            f"{name}({symbol})\n"
            f"─────────────────────\n"
            f"決済株数    : {qty} 株\n"
            f"取得価格    : {entry_price:,.0f} 円\n"
            f"クリア日    : {datetime.now().strftime('%Y-%m-%d')}"
        )
        log.info("[%s] ポジションクリア（売り注文済み）", symbol)

    elif action == "STOP_DONE" and len(parts) == 2:
        symbol = parts[1]
        name   = _WATCH_DICT.get(symbol, symbol)
        pos    = load_position(symbol)
        entry_price = pos.get("entry_price", 0)
        qty         = pos.get("qty", 0)
        clear_position(symbol)
        answer_callback_query(cq_id, "✅ ポジションクリア完了")
        send_telegram(
            f"✅ ストップロス執行済み → ポジションクリア\n"
            f"{name}({symbol})\n"
            f"─────────────────────\n"
            f"決済株数    : {qty} 株\n"
            f"取得価格    : {entry_price:,.0f} 円\n"
            f"クリア日    : {datetime.now().strftime('%Y-%m-%d')}"
        )
        log.info("[%s] ポジションクリア（ストップロス）", symbol)

    else:
        answer_callback_query(cq_id, "❓ 不明なアクション")
        log.warning("不明なコールバックデータ: %s", data)


def _reset_webhook() -> None:
    """
    Webhook を削除して pending updates をクリアする。
    409 Conflict（複数の getUpdates が競合）を防ぐために
    ポーリング開始前に必ず呼ぶ。
    """
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=10,
        )
        if r.json().get("ok"):
            log.info("Webhook 削除 / pending updates クリア完了")
        else:
            log.warning("deleteWebhook 応答: %s", r.json())
    except Exception as e:
        log.warning("deleteWebhook エラー（無視して続行）: %s", e)
    time.sleep(2)  # 他のポーリング接続が切れるのを待つ


def run_callback_handler() -> None:
    """
    Telegram のコールバッククエリをロングポーリングで受信し続けるスレッド。
    schedule モード時にバックグラウンドスレッドとして起動する。
    """
    _reset_webhook()
    log.info("コールバックハンドラー起動（ロングポーリング）")
    offset = None

    while True:
        try:
            url    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {
                "timeout":          30,
                "allowed_updates":  ["callback_query"],
            }
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=40)
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"])

        except requests.exceptions.RequestException as e:
            log.error("コールバックハンドラー エラー: %s", e)
            time.sleep(5)
        except Exception as e:
            log.error("コールバックハンドラー 予期しないエラー: %s", e)
            time.sleep(5)


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
    return df[["open", "high", "low", "close", "volume"]].dropna()


# ── インジケーター & シグナル計算 ─────────────────────────────────
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


# ── 推奨株数計算（S株 = 1株単位） ─────────────────────────────
def calc_qty(cash: float, atr: float, close: float = 0.0) -> int:
    """
    S株（1株単位）向け推奨株数。
    ① リスク基準 : 許容損失額 / ストップ幅
    ② コスト上限 : 口座残高 × MAX_COST_RATIO / 株価
    ①②の小さい方を採用し MAX_QTY で上限ガード。
    """
    stop_dist = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    # ① ATR リスク基準
    qty_by_risk = int(cash * RISK_PER_TRADE / stop_dist)
    # ② コスト上限基準
    if close > 0:
        qty_by_cost = int(cash * MAX_COST_RATIO / close)
    else:
        qty_by_cost = qty_by_risk
    qty = min(qty_by_risk, qty_by_cost, MAX_QTY)
    return max(qty, 1)   # 最低1株


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


def clear_position(symbol: str) -> None:
    save_position(symbol, {
        "in_pos": False, "entry_price": 0.0,
        "stop_price": 0.0, "qty": 0, "entry_dt": None,
    })


# ── シグナル理由テキスト ───────────────────────────────────────
def _entry_reasons(row: pd.Series) -> str:
    r = []
    if row["rsi"] < RSI_ENTRY:
        r.append(f"RSI低値({row['rsi']:.1f}<{RSI_ENTRY})")
    if row["ema_cross_up"]:
        r.append("EMAゴールデンクロス")
    if row["close"] <= row["bb_lower"] + row["bb_band"]:
        r.append("BB下限タッチ")
    return " / ".join(r) if r else "複合"


def _exit_reasons(row: pd.Series) -> str:
    r = []
    if row["rsi"] > RSI_EXIT:
        r.append(f"RSI高値({row['rsi']:.1f}>{RSI_EXIT})")
    if row["close"] >= row["bb_upper"]:
        r.append("BB上限タッチ")
    if row["ema_fast"] < row["ema_mid"]:
        r.append("EMAデッドクロス")
    return " / ".join(r) if r else "複合"


# ── メッセージ生成 ─────────────────────────────────────────────
def build_buy_message(symbol: str, name: str,
                      row: pd.Series, today: str) -> tuple[str, list]:
    close    = row["close"]
    atr      = row["atr"]
    stop     = close - atr * ATR_STOP_MULT
    qty      = calc_qty(INITIAL_CASH, atr, close)
    cost_est = close * qty
    # 株数の根拠を表示（リスク基準 or コスト上限で絞られたか）
    qty_risk = int(INITIAL_CASH * RISK_PER_TRADE / (atr * ATR_STOP_MULT)) if atr > 0 else 0
    qty_cost = int(INITIAL_CASH * MAX_COST_RATIO / close) if close > 0 else 0
    if qty_cost < qty_risk:
        qty_basis = f"コスト上限({MAX_COST_RATIO*100:.0f}%)"
    else:
        qty_basis = f"リスク管理({RISK_PER_TRADE*100:.0f}%損失許容)"
    msg = (
        f"\n【買いシグナル発生】{name}({symbol})\n"
        f"─────────────────────\n"
        f"シグナル日  : {today}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"シグナル理由: {_entry_reasons(row)}\n"
        f"EMA{EMA_SLOW}上 : {'✓ 上昇トレンド中' if close > row['ema_slow'] else '✗'}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ SBI証券 S株で成行買い（1株単位）\n"
        f"推奨株数    : {qty} 株  ← {qty_basis}\n"
        f"概算コスト  : {cost_est:,.0f} 円\n"
        f"ストップ目安: {stop:,.0f} 円 (ATR×{ATR_STOP_MULT})\n"
        f"ATR         : {atr:.1f} 円\n"
        f"─────────────────────\n"
        f"⬇ 注文執行後にボタンをタップ"
    )
    buttons = [[{
        "text": f"✅ S株買い済み（{qty}株）→ ポジション自動登録",
        "callback_data": f"BUY_DONE:{symbol}:{close:.0f}:{qty}:{stop:.0f}",
    }]]
    return msg, buttons


def build_sell_message(symbol: str, name: str,
                       row: pd.Series, pos: dict, today: str) -> tuple[str, list]:
    close   = row["close"]
    pnl_est = (close - pos["entry_price"]) * pos["qty"] if pos["qty"] > 0 else 0
    sign    = "+" if pnl_est >= 0 else ""
    msg = (
        f"\n【売りシグナル発生】{name}({symbol})\n"
        f"─────────────────────\n"
        f"シグナル日  : {today}\n"
        f"終値        : {close:,.0f} 円\n"
        f"─────────────────────\n"
        f"シグナル理由: {_exit_reasons(row)}\n"
        f"RSI         : {row['rsi']:.1f}\n"
        f"─────────────────────\n"
        f"★ 翌朝 SBI証券で成行売り\n"
        f"保有株数    : {pos['qty']} 株\n"
        f"取得値      : {pos['entry_price']:,.0f} 円\n"
        f"含み損益    : {sign}{pnl_est:,.0f} 円\n"
        f"取得日      : {pos.get('entry_dt', '不明')}\n"
        f"─────────────────────\n"
        f"⬇ 注文執行後にボタンをタップ"
    )
    buttons = [[{
        "text": "✅ 成行売り済み → ポジション自動クリア",
        "callback_data": f"SELL_DONE:{symbol}",
    }]]
    return msg, buttons


def build_stop_message(symbol: str, name: str,
                       row: pd.Series, pos: dict, today: str) -> tuple[str, list]:
    close   = row["close"]
    pnl_est = (close - pos["entry_price"]) * pos["qty"] if pos["qty"] > 0 else 0
    sign    = "+" if pnl_est >= 0 else ""
    msg = (
        f"\n【ストップ到達】{name}({symbol})\n"
        f"─────────────────────\n"
        f"シグナル日    : {today}\n"
        f"本日安値      : {row['low']:,.0f} 円\n"
        f"ストップ価格  : {pos['stop_price']:,.0f} 円\n"
        f"─────────────────────\n"
        f"⚠ 翌朝 SBI証券で成行売り（損切り）\n"
        f"保有株数      : {pos['qty']} 株\n"
        f"取得値        : {pos['entry_price']:,.0f} 円\n"
        f"概算損益      : {sign}{pnl_est:,.0f} 円\n"
        f"─────────────────────\n"
        f"⬇ 注文執行後にボタンをタップ"
    )
    buttons = [[{
        "text": "✅ ストップ執行済み → ポジション自動クリア",
        "callback_data": f"STOP_DONE:{symbol}",
    }]]
    return msg, buttons


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

    df  = add_indicators(df)
    df  = add_signals(df)
    row = df.iloc[-1]
    today = df.index[-1].strftime("%Y-%m-%d(%a)")

    if any(pd.isna([row["rsi"], row["ema_slow"], row["atr"],
                    row["bb_upper"], row["bb_lower"]])):
        log.warning("[%s] インジケーター計算不足", symbol)
        return

    pos = load_position(symbol)
    log.info("[%s] 終値:%.1f  RSI:%.1f  EMA%d:%.1f  ATR:%.1f  ポジション:%s",
             symbol, row["close"], row["rsi"], EMA_SLOW, row["ema_slow"],
             row["atr"], "保有中" if pos["in_pos"] else "なし")

    if pos["in_pos"]:
        if row["low"] <= pos["stop_price"]:
            log.info("[%s] ストップ到達: 安値%.1f <= ストップ%.1f",
                     symbol, row["low"], pos["stop_price"])
            msg, buttons = build_stop_message(symbol, name, row, pos, today)
            send_telegram_keyboard(msg, buttons)
        elif row["exit_sig"]:
            log.info("[%s] 売りシグナル発生", symbol)
            msg, buttons = build_sell_message(symbol, name, row, pos, today)
            send_telegram_keyboard(msg, buttons)
        elif daily_report:
            send_telegram(build_no_signal_message(symbol, name, row, pos, today))
        else:
            log.info("[%s] シグナルなし（保有中）", symbol)
    else:
        if row["entry_sig"]:
            log.info("[%s] 買いシグナル発生", symbol)
            msg, buttons = build_buy_message(symbol, name, row, today)
            send_telegram_keyboard(msg, buttons)
        elif daily_report:
            send_telegram(build_no_signal_message(symbol, name, row, pos, today))
        else:
            log.info("[%s] シグナルなし（待機中）", symbol)


# ── 全銘柄チェック ─────────────────────────────────────────────
def check_all(daily_report: bool = False) -> None:
    if datetime.now().weekday() >= 5:
        log.info("土日のためスキップ")
        return
    log.info("===== 全%d銘柄 シグナルチェック開始 =====", len(WATCH_LIST))
    for symbol, name in WATCH_LIST:
        check_one(symbol, name, daily_report=daily_report)
        time.sleep(1.0)
    log.info("===== チェック完了 =====")


# ── ポジション操作コマンド ─────────────────────────────────────
def cmd_set_position(symbol: str, price: float, qty: int, stop: float) -> None:
    name = _WATCH_DICT.get(symbol, symbol)
    save_position(symbol, {
        "in_pos":      True,
        "entry_price": price,
        "stop_price":  stop,
        "qty":         qty,
        "entry_dt":    datetime.now().strftime("%Y-%m-%d"),
    })
    print(f"[{symbol} {name}] ポジション登録: {qty}株 @ {price:,.0f}円  "
          f"ストップ: {stop:,.0f}円")


def cmd_clear_position(symbol: str) -> None:
    name = _WATCH_DICT.get(symbol, symbol)
    clear_position(symbol)
    print(f"[{symbol} {name}] ポジションをクリアしました")


def cmd_test_notify(signal_type: str = "buy") -> None:
    """
    モックデータで Telegram 通知を送り、ボタン動作を実機確認する。
    yfinance 不要。環境変数 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID が必要。
    """
    import pandas as pd

    if not _is_token_set():
        print("❌ TELEGRAM_BOT_TOKEN が未設定です。")
        print('  export TELEGRAM_BOT_TOKEN="..."')
        print('  export TELEGRAM_CHAT_ID="..."')
        return

    SYMBOL = "8604.T"
    NAME   = "野村HD（テスト）"
    TODAY  = datetime.now().strftime("%Y-%m-%d(%a)")

    # 野村HD の実勢に近いモック値（S株想定）
    CLOSE  = 850.0
    ATR    = 20.0
    STOP   = CLOSE - ATR * ATR_STOP_MULT
    QTY    = calc_qty(INITIAL_CASH, ATR, CLOSE)

    mock_row_buy = pd.Series({
        "close": CLOSE, "low": 840.0, "rsi": 52.0, "atr": ATR,
        "ema_slow": 820.0, "ema_fast": 848.0, "ema_mid": 840.0,
        "bb_upper": 920.0, "bb_lower": 780.0, "bb_band": 70.0,
        "ema_cross_up": False, "entry_sig": True, "exit_sig": False,
    })
    mock_row_sell = pd.Series({
        "close": 920.0, "low": 910.0, "rsi": 68.0, "atr": ATR,
        "ema_slow": 820.0, "ema_fast": 910.0, "ema_mid": 890.0,
        "bb_upper": 920.0, "bb_lower": 780.0, "bb_band": 70.0,
        "ema_cross_up": False, "entry_sig": False, "exit_sig": True,
    })
    mock_row_stop = pd.Series({
        "close": 805.0, "low": 803.0, "rsi": 42.0, "atr": ATR,
        "ema_slow": 820.0, "ema_fast": 808.0, "ema_mid": 815.0,
        "bb_upper": 920.0, "bb_lower": 780.0, "bb_band": 70.0,
        "ema_cross_up": False, "entry_sig": False, "exit_sig": False,
    })
    mock_pos = {
        "in_pos": True, "entry_price": CLOSE, "stop_price": STOP,
        "qty": QTY, "entry_dt": datetime.now().strftime("%Y-%m-%d"),
    }

    label_map = {"buy": "買いシグナル", "sell": "売りシグナル", "stop": "ストップ到達"}
    prefix_map = {"buy": "BUY_DONE", "sell": "SELL_DONE", "stop": "STOP_DONE"}

    print(f"\n{'='*60}")
    print(f"  Telegram 通知テスト: {label_map[signal_type]}")
    print(f"  ※ モックデータ使用（{NAME}）")
    print(f"{'='*60}")

    # 通知送信
    _reset_webhook()
    if signal_type == "buy":
        msg, buttons = build_buy_message(SYMBOL, NAME, mock_row_buy, TODAY)
    elif signal_type == "sell":
        msg, buttons = build_sell_message(SYMBOL, NAME, mock_row_sell, mock_pos, TODAY)
    else:
        msg, buttons = build_stop_message(SYMBOL, NAME, mock_row_stop, mock_pos, TODAY)

    ok = send_telegram_keyboard(msg, buttons)
    if not ok:
        print("  ❌ 送信失敗。ログを確認してください。")
        return

    print(f"  ✅ 通知送信完了")
    print(f"  ⏳ Telegram のボタンをタップしてください（60秒以内）...")

    # コールバック待機
    offset   = None
    deadline = time.time() + 60
    expected = prefix_map[signal_type]

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        try:
            url    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {
                "timeout":         min(10, remaining),
                "allowed_updates": ["callback_query"],
            }
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                cq = upd.get("callback_query")
                if cq:
                    data = cq.get("data", "")
                    if data.startswith(expected):
                        print(f"  ✅ コールバック受信: {data}")
                        handle_callback(cq)
                        print(f"\n  テスト完了 ✅")
                        return
                    else:
                        answer_callback_query(cq["id"], "")   # 読み捨て
        except Exception as e:
            log.warning("テスト待機エラー: %s", e)
            time.sleep(2)

    print("  ⏰ タイムアウト（ボタンがタップされませんでした）")


def cmd_show_positions() -> None:
    print("\n現在のポジション一覧:")
    print("-" * 65)
    for symbol, name in WATCH_LIST:
        pos = load_position(symbol)
        if pos["in_pos"]:
            print(f"  {symbol:8}  {name:16}  "
                  f"{pos['qty']}株 @ {pos['entry_price']:,.0f}円  "
                  f"ストップ: {pos['stop_price']:,.0f}円  "
                  f"取得日: {pos.get('entry_dt', '不明')}")
        else:
            print(f"  {symbol:8}  {name:16}  待機中")
    print("-" * 65)


# ── CLI エントリーポイント ─────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="スイングトレード 上位10銘柄 一括監視 + Telegram ボタン通知")
    sub = parser.add_subparsers(dest="cmd")

    p_now = sub.add_parser("now", help="今すぐ全銘柄チェック（テスト用）")
    p_now.add_argument("--daily", action="store_true",
                       help="シグナルなしでも日次レポートを送信")

    sub.add_parser(
        "schedule",
        help=f"スケジューラー起動（毎営業日 {RUN_TIME} チェック + ボタン応答を常時待機）"
    )

    p_test = sub.add_parser("test", help="Telegram 通知 + ボタン動作を実機確認")
    p_test.add_argument(
        "--type", choices=["buy", "sell", "stop"], default="buy",
        help="送信するシグナル種別（デフォルト: buy）",
    )

    p_pos = sub.add_parser("position", help="ポジション管理（緊急時・確認用）")
    pos_sub = p_pos.add_subparsers(dest="pos_cmd")
    pos_sub.add_parser("show", help="全銘柄のポジションを表示")

    p_clear = pos_sub.add_parser("clear", help="ポジションをクリア")
    p_clear.add_argument("symbol", help="銘柄コード（例: 8604.T）")

    p_set = pos_sub.add_parser("set", help="ポジションを手動登録")
    p_set.add_argument("symbol", help="銘柄コード（例: 8604.T）")
    p_set.add_argument("price",  type=float, help="取得価格")
    p_set.add_argument("qty",    type=int,   help="株数")
    p_set.add_argument("stop",   type=float, help="ストップロス価格")

    args = parser.parse_args()

    if args.cmd is None or args.cmd == "schedule":
        if not _is_token_set():
            print("警告: TELEGRAM_BOT_TOKEN が未設定です。通知は送信されません。")

        log.info("スケジューラー起動: 毎営業日 %s にチェック実行", RUN_TIME)
        log.info("Telegram コールバックハンドラー（ボタン応答）も同時起動")

        schedule.every().day.at(RUN_TIME).do(check_all)

        # コールバックハンドラーをバックグラウンドスレッドで起動
        if _is_token_set():
            cb_thread = threading.Thread(
                target=run_callback_handler, daemon=True, name="callback-handler"
            )
            cb_thread.start()
        else:
            log.warning("TELEGRAM_BOT_TOKEN 未設定のためコールバックハンドラーはスキップ")

        while True:
            schedule.run_pending()
            time.sleep(30)

    elif args.cmd == "now":
        check_all(daily_report=getattr(args, "daily", False))

    elif args.cmd == "test":
        cmd_test_notify(args.type)

    elif args.cmd == "position":
        if args.pos_cmd == "show":
            cmd_show_positions()
        elif args.pos_cmd == "clear":
            cmd_clear_position(args.symbol)
        elif args.pos_cmd == "set":
            cmd_set_position(args.symbol, args.price, args.qty, args.stop)
        else:
            p_pos.print_help()
