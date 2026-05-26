"""
line_alert.py — 保有銘柄の含み益アラートをLINE通知

【事前設定】
  /home/user/gobot/.env に以下を記載:
    LINE_CHANNEL_TOKEN=your_channel_access_token
    LINE_USER_ID=your_line_user_id

【使い方】
  1. SBI証券の保有株/信用建玉ページをコピーして sbi_paste.txt に貼り付ける
  2. python line_alert.py を実行

  python line_alert.py                    # 含み益+3%以上で通知（デフォルト）
  python line_alert.py --profit 5         # 含み益+5%以上に変更
  python line_alert.py --test             # テスト送信
  python line_alert.py --dry-run          # 通知せず結果だけ表示
  python line_alert.py --show             # 現在の保有状況だけ表示

【通知タイミング】
  エントリー価格から +3% / +5% / +8% / +10% / +15% / +20% を超えるたびに1回通知。
  損からの回復も同じ条件で判定（+3%到達で通知）。

【定期実行（cron）例】
  0 15 * * 1-5 cd /home/user/gobot && python line_alert.py >> line_alert.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

JST          = timezone(timedelta(hours=9))
_TODAY       = datetime.now(JST).date()
LINE_API_URL = "https://api.line.me/v2/bot/message/push"
PASTE_FILE   = Path(__file__).parent / "sbi_paste.txt"
DEDUP_FILE   = Path(__file__).parent / ".line_alert_sent.json"

# 通知する含み益ステップ（%）— これらを超えた段階で1回ずつ通知
PROFIT_STEPS = [3, 5, 8, 10, 15, 20]


# ─────────────────────────────────────────────────────────────────────────────
# .env 読み込み
# ─────────────────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
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
        if key and key not in os.environ:
            os.environ[key] = val

_load_dotenv()


# ─────────────────────────────────────────────────────────────────────────────
# LINE 送信
# ─────────────────────────────────────────────────────────────────────────────

def send_line(token: str, user_id: str, text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[DRY-RUN] LINE送信:\n{text}\n{'─'*40}")
        return True
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body    = {"to": user_id, "messages": [{"type": "text", "text": text}]}
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
            today_str = str(_TODAY)
            return {k: v for k, v in data.items() if k.startswith(today_str)}
        except Exception:
            pass
    return {}

def _save_dedup(data: dict) -> None:
    DEDUP_FILE.write_text(json.dumps(data, ensure_ascii=False))

def _dedup_key(symbol: str, step: int) -> str:
    return f"{_TODAY}|{symbol}|+{step}%"


# ─────────────────────────────────────────────────────────────────────────────
# SBI ペーストテキスト解析
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """全角英数字を半角に変換"""
    return unicodedata.normalize("NFKC", s).strip()


def parse_sbi_paste(text: str) -> list[dict]:
    """
    SBI証券の保有株/信用建玉ページのコピーテキストから保有情報を抽出する。

    抽出ロジック:
    - 4桁の銘柄コード + "メールアラート" で各銘柄ブロックの開始を検出
    - "(株数)\t単価" パターン（例: "(100)\t4,065"）で取得/建単価を取得
    - 複数ポジションがある場合は加重平均
    """
    margin_pos = text.find("信用建玉一覧")
    lines      = text.split("\n")
    code_re    = re.compile(r"(\d{4})\s+メールアラート")

    # 各銘柄コードの行インデックスを収集
    code_lines: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = code_re.search(line)
        if m:
            code_lines.append((i, m.group(1)))

    holdings: dict[str, dict] = {}

    for idx, (li, code) in enumerate(code_lines):
        # 名前: コード行の1行前（空行・ヘッダー行を除く）
        name = code
        for j in range(li - 1, max(-1, li - 4), -1):
            cand = _normalize(lines[j])
            if cand and not re.search(
                r"メールアラート|評価|取引|銘柄|保有|取得|建玉|一覧|ページ|表示|ダウンロード|株式|信用|預り|担保",
                cand
            ):
                name = cand
                break

        # ブロック末尾: 次の銘柄コード行 or 20行以内
        end = code_lines[idx + 1][0] if idx + 1 < len(code_lines) else li + 20
        block = "\n".join(lines[li:end])

        # 現物/信用 判定
        char_pos  = sum(len(l) + 1 for l in lines[:li])
        is_margin = (margin_pos >= 0 and char_pos > margin_pos)
        type_     = "信用" if is_margin else "現物"

        # 取得/建単価: "(100)\t価格" パターン
        # ※ (31％) のような括弧は \d+ に % が含まれるのでマッチしない
        entry_prices = [
            float(p.replace(",", ""))
            for p in re.findall(r"\(\d+\)\t([\d,]+)", block)
        ]
        if not entry_prices:
            continue

        # 株数: ブロック内の (100) 直前の数値、または行数 × 100
        qty_per_pos = 100
        qty_m = re.search(r"メールアラート[^\n]*\t(\d+)", lines[li])
        if qty_m:
            qty_per_pos = int(qty_m.group(1))

        total_qty = qty_per_pos * len(entry_prices)
        avg_price = sum(entry_prices) / len(entry_prices)

        if code not in holdings:
            holdings[code] = {
                "symbol":      code,
                "name":        name,
                "qty":         total_qty,
                "entry_price": round(avg_price, 1),
                "type":        type_,
            }
        else:
            # 同一コードが複数セクションにある場合は上書き（信用優先）
            if is_margin:
                holdings[code]["type"]        = "信用"
                holdings[code]["entry_price"] = round(avg_price, 1)
                holdings[code]["qty"]         = total_qty

    return list(holdings.values())


def load_holdings_from_paste(path: Path = PASTE_FILE) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    return parse_sbi_paste(text)


# ─────────────────────────────────────────────────────────────────────────────
# 株価取得
# ─────────────────────────────────────────────────────────────────────────────

def fetch_current_price(symbol: str) -> float | None:
    ticker = symbol if symbol.endswith(".T") else f"{symbol}.T"
    try:
        df = yf.download(ticker, period="2d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        close = close.dropna()
        return float(close.iloc[-1]) if len(close) >= 1 else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 含み益ステップ判定
# ─────────────────────────────────────────────────────────────────────────────

def triggered_steps(profit_pct: float, threshold: float,
                    dedup: dict, symbol: str) -> list[int]:
    """
    通知すべき含み益ステップのリストを返す。
    - profit_pct >= threshold かつ まだ本日通知していないステップのみ
    """
    steps = [s for s in PROFIT_STEPS if s >= threshold]
    result = []
    for step in steps:
        if profit_pct >= step and _dedup_key(symbol, step) not in dedup:
            result.append(step)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="保有銘柄の含み益をLINE通知")
    parser.add_argument("--profit",   type=float, default=3.0,
                        help="何%%以上の含み益で通知 (デフォルト: 3.0)")
    parser.add_argument("--paste",    type=str,   default=str(PASTE_FILE),
                        help=f"SBIペーストファイルのパス (デフォルト: {PASTE_FILE})")
    parser.add_argument("--test",     action="store_true", help="テスト送信")
    parser.add_argument("--dry-run",  action="store_true", help="送信せず結果だけ表示")
    parser.add_argument("--show",     action="store_true", help="保有状況だけ表示（送信なし）")
    args = parser.parse_args()

    token   = os.environ.get("LINE_CHANNEL_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")

    if not args.dry_run and not args.show and not args.test:
        if not token or not user_id:
            print("【設定が必要です】")
            print(".env ファイルに以下を設定してください:\n")
            print("  LINE_CHANNEL_TOKEN=your_channel_access_token")
            print("  LINE_USER_ID=your_line_user_id")
            sys.exit(1)

    # テスト送信
    if args.test:
        msg = (f"✅ LINE通知テスト\n"
               f"gobot line_alert.py の接続確認です。\n"
               f"実行日時: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
        ok = send_line(token, user_id, msg, dry_run=args.dry_run)
        print("テスト送信 " + ("成功" if ok else "失敗"))
        return

    # 保有銘柄を読み込む
    paste_path = Path(args.paste)
    holdings   = load_holdings_from_paste(paste_path)

    if not holdings:
        print(f"保有銘柄が見つかりません。{paste_path} にSBIの保有株/建玉ページをペーストしてください。")
        return

    print(f"保有銘柄: {len(holdings)}件  (含み益 ≥{args.profit}% で通知)")
    print()

    dedup    = _load_dedup()
    sent_any = False

    for h in holdings:
        symbol      = h["symbol"]
        name        = h["name"]
        entry_price = h["entry_price"]
        qty         = h["qty"]
        type_       = h["type"]

        current = fetch_current_price(symbol)
        if current is None:
            print(f"  {symbol} {name}  ← 価格取得失敗")
            continue

        profit_pct = (current / entry_price - 1) * 100
        pnl_yen    = (current - entry_price) * qty
        status     = f"{profit_pct:+.1f}%  {pnl_yen:+,.0f}円"

        print(f"  {symbol} {name} [{type_}]  "
              f"建値 {entry_price:,.0f}円 → 現在 {current:,.0f}円  {status}")

        if args.show:
            continue

        # 通知すべきステップを確認
        steps = triggered_steps(profit_pct, args.profit, dedup, symbol)
        for step in steps:
            entry_display = f"{entry_price:,.0f}"
            current_display = f"{current:,.0f}"
            pnl_display = f"{pnl_yen:+,.0f}"
            msg = (
                f"{'💰' if profit_pct >= 5 else '📈'} 含み益アラート +{step}%達成\n"
                f"銘柄: {name}（{symbol}）[{type_}]\n"
                f"建値: {entry_display}円 → 現在: {current_display}円\n"
                f"含み益: {profit_pct:+.1f}%  {pnl_display}円\n"
                f"({qty}株 保有)"
            )
            ok = send_line(token, user_id, msg, dry_run=args.dry_run)
            if ok:
                dedup[_dedup_key(symbol, step)] = str(datetime.now(JST))
                sent_any = True
                print(f"    → +{step}% アラート送信 ✓")
            else:
                print(f"    → +{step}% 送信失敗 ✗")

    _save_dedup(dedup)
    print()

    if not args.show and not sent_any:
        print(f"通知なし（含み益 ≥{args.profit}% の銘柄なし / 本日送信済み）")
    print("完了")


if __name__ == "__main__":
    main()
