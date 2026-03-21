"""
Telegram 接続診断スクリプト（swing_notify_top10 に依存しない単体テスト）

使い方:
  export TELEGRAM_BOT_TOKEN="..."
  export TELEGRAM_CHAT_ID="..."
  python test_telegram_direct.py
"""

import os
import sys
import requests

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID",   "")

print("=" * 60)
print("  Telegram 接続診断")
print("=" * 60)
print(f"  TELEGRAM_BOT_TOKEN : {'設定済み (...' + TOKEN[-8:] + ')' if TOKEN else '❌ 未設定'}")
print(f"  TELEGRAM_CHAT_ID   : {CHAT_ID if CHAT_ID else '❌ 未設定'}")
print()

if not TOKEN or not CHAT_ID:
    print("環境変数を設定してください:")
    print('  export TELEGRAM_BOT_TOKEN="7123456789:AAFxxx..."')
    print('  export TELEGRAM_CHAT_ID="123456789"')
    sys.exit(1)

# ── Step 1: Bot 情報確認 ─────────────────────────────────────
print("Step 1: Bot 接続確認 (getMe) ...")
try:
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=10)
    j = r.json()
    if j.get("ok"):
        bot_info = j["result"]
        print(f"  ✅ OK: @{bot_info['username']} ({bot_info['first_name']})")
    else:
        print(f"  ❌ 失敗: {j}")
        print()
        print("  → TELEGRAM_BOT_TOKEN が間違っている可能性があります")
        print("    @BotFather で /token を使って正しいトークンを確認してください")
        sys.exit(1)
except Exception as e:
    print(f"  ❌ 通信エラー: {e}")
    sys.exit(1)

# ── Step 2: テキスト送信 ────────────────────────────────────
print()
print("Step 2: テキストメッセージ送信 (sendMessage) ...")
try:
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": "🔔 Telegram 接続テスト OK！\ngobot からの診断メッセージです。"},
        timeout=10,
    )
    j = r.json()
    if j.get("ok"):
        print(f"  ✅ 送信成功 (message_id={j['result']['message_id']})")
    else:
        print(f"  ❌ 失敗: {j}")
        print()
        err = j.get("description", "")
        if "chat not found" in err or "Bad Request" in err:
            print("  → TELEGRAM_CHAT_ID が間違っています")
            print("    Bot にメッセージを送った後、以下で Chat ID を確認してください:")
            print(f"    https://api.telegram.org/bot{TOKEN}/getUpdates")
        elif "Forbidden" in err:
            print("  → Bot がブロックされているか、Chat ID が間違っています")
            print("    Telegram で Bot にメッセージを送ってから再試行してください")
        sys.exit(1)
except Exception as e:
    print(f"  ❌ 通信エラー: {e}")
    sys.exit(1)

# ── Step 3: ボタン付きメッセージ送信 ───────────────────────
print()
print("Step 3: ボタン付きメッセージ送信 (inline_keyboard) ...")
try:
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": "🧪 ボタン動作テスト\n下のボタンをタップしてください",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ 買い注文済み（テスト）", "callback_data": "BUY_DONE:TEST:100:100:90"},
                    {"text": "❌ キャンセル",              "callback_data": "CANCEL"},
                ]]
            },
        },
        timeout=10,
    )
    j = r.json()
    if j.get("ok"):
        print(f"  ✅ 送信成功 (message_id={j['result']['message_id']})")
    else:
        print(f"  ❌ 失敗: {j}")
        sys.exit(1)
except Exception as e:
    print(f"  ❌ 通信エラー: {e}")
    sys.exit(1)

# ── Step 4: コールバック待機 ────────────────────────────────
print()
print("Step 4: ボタンタップ待機（30秒）...")
print("  → Telegram でボタンをタップしてください")

offset  = None
deadline = __import__("time").time() + 30

import time
while time.time() < deadline:
    remaining = int(deadline - time.time())
    try:
        params = {"timeout": min(10, remaining), "allowed_updates": ["callback_query"]}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params=params, timeout=15)
        r.raise_for_status()
        for upd in r.json().get("result", []):
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if cq:
                data = cq["data"]
                print(f"  ✅ コールバック受信: {data}")
                # ローディング解除
                requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
                    json={"callback_query_id": cq["id"], "text": "✅ 受信確認！"},
                    timeout=5,
                )
                print()
                print("=" * 60)
                print("  全テスト PASS ✅")
                print("  swing_notify_top10.py の通知は正常に動作します")
                print("=" * 60)
                sys.exit(0)
    except Exception as e:
        print(f"  ⚠ ポーリングエラー: {e}")
        time.sleep(2)

print("  ⏰ タイムアウト（ボタンがタップされませんでした）")
print()
print("=" * 60)
print("  Step 1-3 は成功しています")
print("  Telegram アプリでメッセージを確認してボタンをタップしてみてください")
print("=" * 60)
