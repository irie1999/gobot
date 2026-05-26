"""
line_alert.py — 保有銘柄の急上昇・目標価格接近をLINE通知

【事前設定】
  以下の環境変数を ~/.bashrc または .env に追加:
    export LINE_CHANNEL_TOKEN="your_channel_access_token"
    export LINE_USER_ID="your_line_user_id"

  LINE Messaging API チャンネル作成手順:
    1. https://developers.line.biz/ でチャンネル作成 (Messaging API)
    2. チャンネルアクセストークン (長期) を発行 → LINE_CHANNEL_TOKEN
    3. ボットと友達追加し、User ID を取得 → LINE_USER_ID
       (取得方法: https://api.line.me/v2/profile にGETリクエスト)

【使い方】
  python line_alert.py                    # 前日比+5%以上 または 目標価格90%到達で通知
  python line_alert.py --threshold 3      # 前日比+3%以上に変更
  python line_alert.py --near-target 95   # 目標価格95%到達で通知
  python line_alert.py --from-entry 10    # エントリー比+10%以上でも通知
  python line_alert.py --log aggressive   # aggressive版ログを使用
  python line_alert.py --test             # テスト送信（保有銘柄チェックなし）
  python line_alert.py --dry-run          # 通知せず結果のみ表示

【定期実行（cron）の例】
  # 平日15時（引け後）に毎日実行
  0 15 * * 1-5 cd /home/user/gobot && python line_alert.py >> line_alert.log 2>&1
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

JST = timezone(timedelta(hours=9))
_TODAY = datetime.now(JST).date()

LINE_API_URL = "https://api.line.me/v2/bot/message/push"
DEDUP_FILE   = Path(".line_alert_sent.json")   # 同日重複送信防止ファイル

FIXED_QTY = 100  # forward_test と同じ


def _load_dotenv() -> None:
    """.env ファイルが存在すれば環境変数として読み込む（python-dotenv 不要）"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:   # 既存の環境変数は上書きしない
            os.environ[key] = val


_load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# LINE 送信
# ─────────────────────────────────────────────────────────────────────────────

def send_line(token: str, user_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[DRY-RUN] LINE送信:\n{text}\n")
        return True
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    body = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    try:
        resp = requests.post(LINE_API_URL, headers=headers, json=body, timeout=10)
        if resp.status_code == 200:
            return True
        print(f"[LINE ERROR] {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[LINE ERROR] {e}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 重複送信防止
# ─────────────────────────────────────────────────────────────────────────────

def _load_dedup() -> dict:
    if DEDUP_FILE.exists():
        try:
            data = json.loads(DEDUP_FILE.read_text())
            # 今日以外のキーは削除してファイルを軽量化
            today_str = str(_TODAY)
            return {k: v for k, v in data.items() if k.startswith(today_str)}
        except Exception:
            pass
    return {}


def _save_dedup(data: dict) -> None:
    DEDUP_FILE.write_text(json.dumps(data, ensure_ascii=False))


def _dedup_key(symbol: str, strategy: str, reason: str) -> str:
    return f"{_TODAY}|{symbol}|{strategy}|{reason}"


# ─────────────────────────────────────────────────────────────────────────────
# 保有銘柄の読み込み
# ─────────────────────────────────────────────────────────────────────────────

def load_holdings(log_path: str) -> list[dict]:
    """forward_test_log.csv から status=holding のポジションを取得"""
    path = Path(log_path)
    if not path.exists():
        print(f"[WARN] ログファイルが見つかりません: {path}")
        return []
    holdings = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") == "holding":
                holdings.append(row)
    return holdings


# ─────────────────────────────────────────────────────────────────────────────
# 株価取得
# ─────────────────────────────────────────────────────────────────────────────

def fetch_price_info(symbol: str) -> dict | None:
    """直近2日の日足から現在値・前日比を取得"""
    ticker = symbol if symbol.endswith(".T") else f"{symbol}.T"
    try:
        df = yf.download(ticker, period="5d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
            high  = high.iloc[:, 0]
        close = close.dropna()
        high  = high.dropna()
        if len(close) < 2:
            return None
        cur       = float(close.iloc[-1])
        prev      = float(close.iloc[-2])
        today_hi  = float(high.iloc[-1])
        daily_pct = (cur / prev - 1) * 100
        return {
            "close":     cur,
            "prev":      prev,
            "today_hi":  today_hi,
            "daily_pct": daily_pct,
            "date":      close.index[-1].date(),
        }
    except Exception as e:
        print(f"[WARN] {symbol} 価格取得失敗: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# アラート判定
# ─────────────────────────────────────────────────────────────────────────────

def check_alerts(row: dict, price: dict, *,
                 threshold_pct: float,
                 near_target_pct: float | None,
                 from_entry_pct: float | None) -> list[dict]:
    """
    アラート条件を判定し、該当するアラートのリストを返す。
    各アラートは {"reason": str, "message": str} の形式。
    """
    alerts = []
    symbol   = row["symbol"]
    name     = row.get("name", symbol)
    strategy = row.get("strategy", "")
    entry_p  = float(row.get("fill_price") or row.get("order_price") or 0)
    target_p = float(row.get("target_price") or 0)
    stop_p   = float(row.get("stop_price") or 0)
    cur      = price["close"]
    hi       = price["today_hi"]
    daily    = price["daily_pct"]

    # 前日比で急上昇
    if daily >= threshold_pct:
        pnl = (cur - entry_p) * FIXED_QTY if entry_p else 0
        alerts.append({
            "reason": "spike",
            "message": (
                f"📈 急上昇アラート\n"
                f"銘柄: {name}（{symbol}）[{strategy}]\n"
                f"前日比: +{daily:.1f}%\n"
                f"現在値: {cur:,.0f}円  前日終値: {price['prev']:,.0f}円\n"
                f"エントリー: {entry_p:,.0f}円  含み益: {pnl:+,.0f}円\n"
                f"目標価格: {target_p:,.0f}円  損切価格: {stop_p:,.0f}円"
            ),
        })

    # エントリーからの上昇率
    if from_entry_pct is not None and entry_p > 0:
        gain = (cur / entry_p - 1) * 100
        if gain >= from_entry_pct:
            pnl = (cur - entry_p) * FIXED_QTY
            alerts.append({
                "reason": "from_entry",
                "message": (
                    f"💰 含み益アラート（エントリー比+{gain:.1f}%）\n"
                    f"銘柄: {name}（{symbol}）[{strategy}]\n"
                    f"エントリー: {entry_p:,.0f}円 → 現在: {cur:,.0f}円\n"
                    f"含み益: {pnl:+,.0f}円\n"
                    f"目標価格: {target_p:,.0f}円"
                ),
            })

    # 目標価格への接近
    if near_target_pct is not None and target_p > 0 and entry_p > 0:
        reach = (cur / target_p) * 100
        reach_hi = (hi / target_p) * 100
        if reach_hi >= near_target_pct:
            dist_pct = (target_p - cur) / cur * 100
            pnl = (cur - entry_p) * FIXED_QTY
            alerts.append({
                "reason": "near_target",
                "message": (
                    f"🎯 目標価格接近アラート（{reach_hi:.0f}%到達）\n"
                    f"銘柄: {name}（{symbol}）[{strategy}]\n"
                    f"目標: {target_p:,.0f}円  現在: {cur:,.0f}円  あと{dist_pct:.1f}%\n"
                    f"エントリー: {entry_p:,.0f}円  含み益: {pnl:+,.0f}円"
                ),
            })

    return alerts


# ─────────────────────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="保有銘柄の急上昇をLINE通知")
    parser.add_argument("--threshold",   type=float, default=5.0,
                        help="前日比何%%以上で通知 (デフォルト: 5.0)")
    parser.add_argument("--near-target", type=float, default=90.0, metavar="PCT",
                        help="目標価格の何%%到達で通知 (デフォルト: 90.0, 0で無効)")
    parser.add_argument("--from-entry",  type=float, default=None, metavar="PCT",
                        help="エントリー比何%%上昇で通知 (デフォルト: 無効)")
    parser.add_argument("--log",         type=str, default="forward_test_log.csv",
                        help="ログファイルパス")
    parser.add_argument("--test",        action="store_true",
                        help="テスト通知を送信（銘柄チェックなし）")
    parser.add_argument("--dry-run",     action="store_true",
                        help="通知せず結果のみ表示")
    args = parser.parse_args()

    token   = os.environ.get("LINE_CHANNEL_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")

    if not args.dry_run:
        if not token or not user_id:
            print("【設定が必要です】")
            print("以下の環境変数を設定してから実行してください:\n")
            print("  export LINE_CHANNEL_TOKEN='your_channel_access_token'")
            print("  export LINE_USER_ID='your_line_user_id'\n")
            print("LINE Developers (https://developers.line.biz/) でチャンネルを作成し、")
            print("チャンネルアクセストークン(長期)を発行してください。")
            sys.exit(1)

    # テスト送信
    if args.test:
        msg = (f"✅ LINE通知テスト\n"
               f"gobot line_alert.py の接続確認です。\n"
               f"実行日時: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
        ok = send_line(token, user_id, msg, dry_run=args.dry_run)
        print("テスト送信 " + ("成功" if ok else "失敗"))
        return

    near_target = args.near_target if args.near_target > 0 else None

    # 保有銘柄を読み込む
    holdings = load_holdings(args.log)
    if not holdings:
        print(f"保有銘柄なし（{args.log}）")
        return
    print(f"保有銘柄: {len(holdings)}件")

    dedup    = _load_dedup()
    sent_any = False

    for row in holdings:
        symbol = row.get("symbol", "")
        if not symbol:
            continue
        print(f"  {symbol} {row.get('name','')} [{row.get('strategy','')}] 価格確認中...", end=" ")
        price = fetch_price_info(symbol)
        if price is None:
            print("取得失敗")
            continue
        print(f"{price['close']:,.0f}円 (前日比 {price['daily_pct']:+.1f}%)")

        alerts = check_alerts(
            row, price,
            threshold_pct=args.threshold,
            near_target_pct=near_target,
            from_entry_pct=args.from_entry,
        )

        for alert in alerts:
            key = _dedup_key(symbol, row.get("strategy", ""), alert["reason"])
            if key in dedup:
                print(f"    → [{alert['reason']}] 本日送信済みのためスキップ")
                continue
            ok = send_line(token, user_id, alert["message"], dry_run=args.dry_run)
            if ok:
                dedup[key] = str(datetime.now(JST))
                sent_any = True
                print(f"    → [{alert['reason']}] LINE送信 ✓")
            else:
                print(f"    → [{alert['reason']}] LINE送信 ✗")

    _save_dedup(dedup)

    if not sent_any:
        print(f"\n本日の通知なし（前日比 ≥{args.threshold}% の急上昇なし）")
    print("完了")


if __name__ == "__main__":
    main()
