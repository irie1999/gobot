"""
Telegram ライブ通知テスト
────────────────────────────────────────────────────────────
実際に Telegram へ通知を送り、ボタンをタップして動作を確認します。
yfinance への接続は不要です（モックデータを使用）。

■ 事前準備:
  export TELEGRAM_BOT_TOKEN="your_token"
  export TELEGRAM_CHAT_ID="your_chat_id"

■ 実行方法:
  python test_telegram_live.py          # 3種類の通知を順番に送信
  python test_telegram_live.py --buy    # 買いシグナル通知のみ
  python test_telegram_live.py --sell   # 売りシグナル通知のみ
  python test_telegram_live.py --stop   # ストップ通知のみ
"""

import argparse
import os
import sys
import time
import tempfile
from pathlib import Path
from datetime import datetime

import pandas as pd

# ── 環境変数チェック ──────────────────────────────────────────────
TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID",   "")

if not TOKEN or not CHAT_ID:
    print("=" * 60)
    print("  エラー: 環境変数が未設定です")
    print("=" * 60)
    print()
    print("以下を実行してから再度お試しください:\n")
    print('  export TELEGRAM_BOT_TOKEN="7123456789:AAFxxx..."')
    print('  export TELEGRAM_CHAT_ID="123456789"')
    print()
    print("Bot Token の取得方法:")
    print("  1. Telegram で @BotFather を開く")
    print("  2. /newbot を送信してトークンを取得")
    print()
    print("Chat ID の取得方法:")
    print("  1. 作成した Bot にメッセージを送る")
    print("  2. ブラウザで以下を開く:")
    print(f"     https://api.telegram.org/bot<TOKEN>/getUpdates")
    print('     → "chat":{"id": の数字が Chat ID')
    sys.exit(1)

# ── swing_notify_top10 のインポート ──────────────────────────────
import swing_notify_top10 as bot

# テスト用の一時ポジションディレクトリ
_tmpdir = tempfile.TemporaryDirectory()
bot.POSITION_DIR = Path(_tmpdir.name)

# ── テスト用モックデータ ──────────────────────────────────────────
SYMBOL  = "8604.T"
NAME    = "野村HD（テスト）"
TODAY   = datetime.now().strftime("%Y-%m-%d(%a)")

MOCK_ROW_BUY = pd.Series({
    "close": 850.0, "low": 840.0, "rsi": 52.0, "atr": 20.0,
    "ema_slow": 820.0, "ema_fast": 848.0, "ema_mid": 840.0,
    "bb_upper": 920.0, "bb_lower": 780.0, "bb_band": 70.0,
    "ema_cross_up": False, "entry_sig": True, "exit_sig": False,
})
MOCK_ROW_SELL = pd.Series({
    "close": 920.0, "low": 910.0, "rsi": 68.0, "atr": 20.0,
    "ema_slow": 820.0, "ema_fast": 910.0, "ema_mid": 890.0,
    "bb_upper": 920.0, "bb_lower": 780.0, "bb_band": 70.0,
    "ema_cross_up": False, "entry_sig": False, "exit_sig": True,
})
MOCK_ROW_STOP = pd.Series({
    "close": 805.0, "low": 803.0, "rsi": 42.0, "atr": 20.0,
    "ema_slow": 820.0, "ema_fast": 808.0, "ema_mid": 815.0,
    "bb_upper": 920.0, "bb_lower": 780.0, "bb_band": 70.0,
    "ema_cross_up": False, "entry_sig": False, "exit_sig": False,
})
MOCK_POS = {
    "in_pos": True, "entry_price": 850.0, "stop_price": 810.0,
    "qty": 100, "entry_dt": "2025-03-01",
}


# ── コールバック待機 ───────────────────────────────────────────────
def wait_for_callback(expected_prefix: str, timeout: int = 60) -> bool:
    """
    ボタンタップ（コールバッククエリ）を最大 timeout 秒待機する。
    タップされたら handle_callback を呼び出して True を返す。
    タイムアウトしたら False を返す。
    """
    import requests

    print(f"\n  ⏳ Telegram のボタンをタップしてください（{timeout}秒以内）...")
    offset    = None
    deadline  = time.time() + timeout

    while time.time() < deadline:
        remaining = int(deadline - time.time())
        try:
            url    = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {
                "timeout":         min(10, remaining),
                "allowed_updates": ["callback_query"],
            }
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                cq     = update.get("callback_query")
                if cq:
                    data = cq.get("data", "")
                    if data.startswith(expected_prefix):
                        print(f"  ✅ コールバック受信: {data}")
                        bot.handle_callback(cq)
                        return True
                    else:
                        # 他のボタン（前のテストの残り）は読み捨て
                        bot.answer_callback_query(cq["id"], "")

        except Exception as e:
            print(f"  ⚠ ポーリングエラー: {e}")
            time.sleep(2)

    print("  ⏰ タイムアウト（ボタンはタップされませんでした）")
    return False


# ── 各テスト ────────────────────────────────────────────────────
def test_buy_signal():
    print("\n" + "=" * 60)
    print("  【テスト 1】買いシグナル通知 + BUY_DONE ボタン")
    print("=" * 60)
    msg, buttons = bot.build_buy_message(SYMBOL, NAME, MOCK_ROW_BUY, TODAY)
    bot.send_telegram_keyboard(msg, buttons)
    print("  📨 Telegram に買いシグナル通知を送信しました")
    print(f"  ボタン: {buttons[0][0]['text']}")

    tapped = wait_for_callback("BUY_DONE:")
    if tapped:
        pos = bot.load_position(SYMBOL)
        print(f"\n  📋 ポジション登録結果:")
        print(f"     in_pos      : {pos['in_pos']}")
        print(f"     entry_price : {pos['entry_price']:,.0f} 円")
        print(f"     qty         : {pos['qty']} 株")
        print(f"     stop_price  : {pos['stop_price']:,.0f} 円")
        print(f"     entry_dt    : {pos['entry_dt']}")
    return tapped


def test_sell_signal():
    print("\n" + "=" * 60)
    print("  【テスト 2】売りシグナル通知 + SELL_DONE ボタン")
    print("=" * 60)
    bot.save_position(SYMBOL, MOCK_POS)
    msg, buttons = bot.build_sell_message(SYMBOL, NAME, MOCK_ROW_SELL, MOCK_POS, TODAY)
    bot.send_telegram_keyboard(msg, buttons)
    print("  📨 Telegram に売りシグナル通知を送信しました")
    print(f"  ボタン: {buttons[0][0]['text']}")

    tapped = wait_for_callback("SELL_DONE:")
    if tapped:
        pos = bot.load_position(SYMBOL)
        print(f"\n  📋 ポジションクリア結果:")
        print(f"     in_pos : {pos['in_pos']}")
        print(f"     qty    : {pos['qty']} 株")
    return tapped


def test_stop_signal():
    print("\n" + "=" * 60)
    print("  【テスト 3】ストップ到達通知 + STOP_DONE ボタン")
    print("=" * 60)
    bot.save_position(SYMBOL, MOCK_POS)
    msg, buttons = bot.build_stop_message(SYMBOL, NAME, MOCK_ROW_STOP, MOCK_POS, TODAY)
    bot.send_telegram_keyboard(msg, buttons)
    print("  📨 Telegram にストップ到達通知を送信しました")
    print(f"  ボタン: {buttons[0][0]['text']}")

    tapped = wait_for_callback("STOP_DONE:")
    if tapped:
        pos = bot.load_position(SYMBOL)
        print(f"\n  📋 ポジションクリア結果:")
        print(f"     in_pos : {pos['in_pos']}")
        print(f"     qty    : {pos['qty']} 株")
    return tapped


# ── メイン ──────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Telegram ライブ通知テスト")
    parser.add_argument("--buy",  action="store_true", help="買いシグナル通知のみ")
    parser.add_argument("--sell", action="store_true", help="売りシグナル通知のみ")
    parser.add_argument("--stop", action="store_true", help="ストップ通知のみ")
    args = parser.parse_args()

    run_all  = not (args.buy or args.sell or args.stop)
    results  = {}

    print("=" * 60)
    print("  Telegram ライブ通知テスト")
    print(f"  Bot Token : ...{TOKEN[-10:]}")
    print(f"  Chat ID   : {CHAT_ID}")
    print("=" * 60)
    print("  ※ モックデータ使用（yfinance 接続なし）")
    print("  ※ 通知はすべて「野村HD（テスト）」で送信")

    if run_all or args.buy:
        results["buy"]  = test_buy_signal()
        if run_all:
            time.sleep(2)

    if run_all or args.sell:
        results["sell"] = test_sell_signal()
        if run_all:
            time.sleep(2)

    if run_all or args.stop:
        results["stop"] = test_stop_signal()

    # ── 結果サマリー ────────────────────────────────────────────
    if results:
        print("\n" + "=" * 60)
        print("  テスト結果サマリー")
        print("=" * 60)
        labels = {"buy": "買いシグナル (BUY_DONE) ", "sell": "売りシグナル (SELL_DONE)", "stop": "ストップ   (STOP_DONE) "}
        all_ok = True
        for key, ok in results.items():
            mark = "✅ PASS" if ok else "⏰ SKIP（タイムアウト）"
            print(f"  {labels[key]} : {mark}")
            if not ok:
                all_ok = False
        print("=" * 60)
        sys.exit(0 if all_ok else 1)

    _tmpdir.cleanup()
