"""
line_alert_daytrade.py  ―  デイトレ LINE通知
==================================================================
逆指値ロング (line_alert.py) を5分足デイトレ用に移植・簡略化。

【役割】
- シグナル発生時に LINE Notify or LINE Messaging API 経由で通知
- bot や signals_export と連携

【セットアップ】
.env に追加:
  LINE_NOTIFY_TOKEN=xxxxx       # LINE Notify (簡易、廃止予定)
  LINE_CHANNEL_TOKEN=xxxxx      # LINE Messaging API (推奨)
  LINE_USER_ID=Uxxxxxxxxxxxx    # 通知先 ユーザーID

【使い方】
  from line_alert_daytrade import send_signal_alert

  send_signal_alert(
      symbol="5803.T", name="フジクラ", strategy="MACD",
      entry_p=4500, stop_p=4470, target_p=4560, qty=100,
  )

  # コマンドラインから直接送信
  python line_alert_daytrade.py --test
  python line_alert_daytrade.py --message "今日のシグナル: 3件"
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import requests


def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def send_notify(message):
    """LINE Notify でメッセージ送信。"""
    token = os.environ.get("LINE_NOTIFY_TOKEN")
    if not token:
        print("[warn] LINE_NOTIFY_TOKEN 未設定")
        return False
    try:
        r = requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {token}"},
            data={"message": message},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[error] LINE Notify失敗: {e}")
        return False


def send_messaging(message):
    """LINE Messaging API でユーザーにメッセージ送信。"""
    token = os.environ.get("LINE_CHANNEL_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        return False
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "to": user_id,
                "messages": [{"type": "text", "text": message}],
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"[error] LINE Messaging失敗: {e}")
        return False


def send(message):
    """利用可能なAPIで送信 (Messaging優先)。"""
    if send_messaging(message):
        return True
    return send_notify(message)


def format_signal_alert(symbol, name, strategy, side, entry_p, stop_p,
                        target_p, qty, score=None, rank=None):
    """シグナル通知用フォーマット。"""
    side_label = "買い" if side == "long" else "空売り"
    score_str = f" {rank}{score}" if score else ""
    risk = abs(entry_p - stop_p) * qty
    reward = abs(target_p - entry_p) * qty
    rr = reward / risk if risk > 0 else 0
    return (
        f"📊 デイトレ シグナル{score_str}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"銘柄: {name} ({symbol})\n"
        f"戦略: {strategy} ({side_label})\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 注文: {entry_p:,.0f}円 × {qty}株\n"
        f"🛑 損切: {stop_p:,.0f}円 (-{abs(entry_p-stop_p):,.0f}円)\n"
        f"🎯 目標: {target_p:,.0f}円 (+{abs(target_p-entry_p):,.0f}円)\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💵 リスク: {risk:,.0f}円\n"
        f"📈 利益: {reward:,.0f}円\n"
        f"⚖️ RR: 1:{rr:.1f}"
    )


def send_signal_alert(symbol, name, strategy, side, entry_p, stop_p,
                      target_p, qty, score=None, rank=None):
    """シグナル通知 (一括ラッパ)。"""
    msg = format_signal_alert(symbol, name, strategy, side, entry_p, stop_p,
                              target_p, qty, score, rank)
    return send(msg)


def send_exit_alert(symbol, name, strategy, side, entry_p, exit_p, qty,
                    pnl, reason):
    """決済通知。"""
    side_label = "買い" if side == "long" else "空売り"
    emoji = "🎉" if pnl > 0 else "💔" if pnl < 0 else "➖"
    msg = (
        f"{emoji} デイトレ 決済\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"銘柄: {name} ({symbol})\n"
        f"戦略: {strategy} ({side_label})\n"
        f"理由: {reason}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"買値: {entry_p:,.0f}円\n"
        f"決済値: {exit_p:,.0f}円\n"
        f"株数: {qty}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💴 損益: {pnl:+,.0f}円"
    )
    return send(msg)


def send_daily_report(date_str, n_trades, wins, win_rate, pf, total_pnl):
    """日次レポート通知。"""
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"
    emoji = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"
    msg = (
        f"{emoji} デイトレ 日次レポート\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"日付: {date_str}\n"
        f"取引数: {n_trades}\n"
        f"勝率: {win_rate:.1f}% ({wins}/{n_trades})\n"
        f"PF: {pf_str}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💴 損益: {total_pnl:+,.0f}円"
    )
    return send(msg)


def main():
    parser = argparse.ArgumentParser(description="LINE 通知")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--message", default=None)
    args = parser.parse_args()

    if args.test:
        ok = send("🧪 デイトレ通知テスト")
        print(f"テスト送信: {'成功' if ok else '失敗'}")
    elif args.message:
        ok = send(args.message)
        print(f"送信: {'成功' if ok else '失敗'}")
    else:
        # サンプル
        msg = format_signal_alert("5803.T", "フジクラ", "MACD", "long",
                                   4500, 4470, 4560, 100, 75, "★★")
        print(msg)


if __name__ == "__main__":
    main()
