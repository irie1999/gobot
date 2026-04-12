"""
kabu_daytrade_bot.py  ―  kabuステーション連携デイトレ自動売買ボット
==================================================================
バックテスト済みの3戦略 (ORB / Pivot / RSI) を使い、
kabuステーション REST API 経由で実際に売買する。

【前提】
  - kabuステーション が起動中 (localhost:18080 or 18081)
  - .env に KABU_API_PASSWORD=... を設定
  - 取引口座が有効

【動作フロー】
  8:55  起動 → トークン取得
  9:00  前場開始 → 5分足を蓄積開始
  9:30  ORB シグナル監視開始 (OR 確定後)
  9:00~ Pivot/RSI シグナルも同時監視
  シグナル発生 → 成行注文 (1日1ポジ制限)
  保有中 → 損切り/目標/トレーリング監視
  14:55 強制決済
  15:00 日次レポート表示 → 終了

【使い方】
  python kabu_daytrade_bot.py                    # 本番 (実注文)
  python kabu_daytrade_bot.py --dry-run          # ドライラン (注文しない)
  python kabu_daytrade_bot.py --symbol 8306      # 特定銘柄
  python kabu_daytrade_bot.py --budget 600000    # 予算指定

【設定】
  .env:
    KABU_API_PASSWORD=your_password
    KABU_BASE_URL=http://localhost:18080   # 本番
    # KABU_BASE_URL=http://localhost:18081 # デモ
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── .env ローダ ─────────────────────────────────────────────
def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
            return
        except Exception:
            pass


_load_dotenv()


# ─────────────────────────────────────────────────────────────
# kabuステーション API クライアント
# ─────────────────────────────────────────────────────────────

class KabuClient:
    """kabuステーション REST API クライアント。"""

    def __init__(self, password: str | None = None,
                 base_url: str | None = None):
        self.password = password or os.environ.get("KABU_API_PASSWORD", "")
        self.base_url = base_url or os.environ.get(
            "KABU_BASE_URL", "http://localhost:18080")
        self.token: str | None = None

        if not self.password:
            raise RuntimeError(
                "KABU_API_PASSWORD が必要です。.env に設定してください。")

    def refresh_token(self) -> None:
        url = f"{self.base_url}/kabusapi/token"
        r = requests.post(url, json={"APIPassword": self.password},
                          headers={"Content-Type": "application/json"},
                          timeout=10)
        r.raise_for_status()
        self.token = r.json()["Token"]
        log.info("kabu トークン取得成功")

    def _h(self) -> dict:
        return {"X-API-KEY": self.token, "Content-Type": "application/json"}

    def get_board(self, symbol: str, exchange: int = 1) -> dict:
        """板情報 (現在値含む) を取得。"""
        url = f"{self.base_url}/kabusapi/board/{symbol}@{exchange}"
        r = requests.get(url, headers=self._h(), timeout=10)
        if r.status_code == 401:
            self.refresh_token()
            r = requests.get(url, headers=self._h(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_price(self, symbol: str, exchange: int = 1) -> float:
        """現在値。"""
        data = self.get_board(symbol, exchange)
        price = data.get("CurrentPrice") or data.get("CalcPrice")
        if price is None:
            raise ValueError(f"価格取得失敗: {symbol}")
        return float(price)

    def send_order(self, symbol: str, side: str, qty: int,
                   exchange: int = 1, order_type: int = 2,
                   price: float = 0) -> dict:
        """
        注文送信。
        side: "2"=買い, "1"=売り
        order_type: 1=指値, 2=成行
        """
        url = f"{self.base_url}/kabusapi/sendorder"
        body = {
            "Password": self.password,
            "Symbol": symbol,
            "Exchange": exchange,
            "SecurityType": 1,    # 株式
            "Side": side,
            "CashMargin": 1,      # 現物
            "DelivType": 2,       # お預り金
            "FundType": "AA",
            "AccountType": 4,     # 特定
            "Qty": qty,
            "FrontOrderType": order_type,
            "Price": price,
            "ExpireDay": 0,       # 当日
        }
        r = requests.post(url, headers=self._h(), json=body, timeout=10)
        if r.status_code == 401:
            self.refresh_token()
            r = requests.post(url, headers=self._h(), json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_positions(self) -> list[dict]:
        """保有ポジション一覧。"""
        url = f"{self.base_url}/kabusapi/positions"
        r = requests.get(url, headers=self._h(), timeout=10)
        if r.status_code == 401:
            self.refresh_token()
            r = requests.get(url, headers=self._h(), timeout=10)
        r.raise_for_status()
        return r.json() or []


# ─────────────────────────────────────────────────────────────
# 5分足バッファ
# ─────────────────────────────────────────────────────────────

class BarBuffer:
    """リアルタイム価格から5分足を構築するバッファ。"""

    def __init__(self):
        self.bars: list[dict] = []
        self._current_bar: dict | None = None
        self._bar_start: datetime | None = None

    def update(self, price: float, now: datetime) -> dict | None:
        """価格を入力し、5分足が確定したら返す。"""
        bar_time = now.replace(
            minute=(now.minute // 5) * 5, second=0, microsecond=0)

        if self._bar_start is None or bar_time > self._bar_start:
            # 新しいバー開始
            completed = self._current_bar
            self._current_bar = {
                "time": bar_time, "open": price, "high": price,
                "low": price, "close": price,
            }
            self._bar_start = bar_time
            if completed:
                self.bars.append(completed)
                return completed
            return None
        else:
            # 既存バー更新
            self._current_bar["high"] = max(self._current_bar["high"], price)
            self._current_bar["low"] = min(self._current_bar["low"], price)
            self._current_bar["close"] = price
            return None

    @property
    def closes(self) -> list[float]:
        return [b["close"] for b in self.bars]

    @property
    def highs(self) -> list[float]:
        return [b["high"] for b in self.bars]

    @property
    def lows(self) -> list[float]:
        return [b["low"] for b in self.bars]

    @property
    def or_range(self) -> tuple[float, float, float]:
        """9:00-9:30 のオープニングレンジ (hi, lo, width)。"""
        or_bars = [b for b in self.bars
                   if b["time"].time() < dtime(9, 30)]
        if not or_bars:
            return 0, 0, 0
        hi = max(b["high"] for b in or_bars)
        lo = min(b["low"] for b in or_bars)
        return hi, lo, hi - lo


# ─────────────────────────────────────────────────────────────
# シグナル判定
# ─────────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """直近の RSI を計算。"""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(-period, 0)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_g = sum(gains) / period if gains else 0
    avg_l = sum(losses) / period if losses else 0
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def check_signals(buf: BarBuffer, prev_day: dict | None,
                  now: datetime) -> dict | None:
    """
    3戦略のシグナルを同時チェック。優先度: ORB > Pivot > RSI。
    Returns: {"strategy": str, "stop": float, "target": float} or None
    """
    bars = buf.bars
    if len(bars) < 3:
        return None

    t = now.time()
    if t >= dtime(11, 0):  # 前場限定
        return None

    price = bars[-1]["close"]
    prev_close = bars[-2]["close"]

    # ── ORB (9:30 以降) ─────────────────────────────
    if t >= dtime(9, 30):
        or_hi, or_lo, or_w = buf.or_range
        if or_w > 0 and price > or_hi and price > prev_close:
            target = price + or_w * 1.5
            stop = or_lo
            if price > stop:
                return {"strategy": "ORB", "stop": stop,
                        "target": target}

    # ── Pivot (前日データ必要) ───────────────────────
    if prev_day and prev_day.get("pp") and prev_day.get("s1"):
        pp = prev_day["pp"]
        s1 = prev_day["s1"]
        if pp > s1 > 0:
            bar_lo = bars[-1]["low"]
            if (bar_lo <= s1 * 1.002
                    and price > s1
                    and price > prev_close):
                target = pp
                stop = s1 - (pp - s1) * 0.5
                if price > stop and target > price:
                    return {"strategy": "Pivot", "stop": stop,
                            "target": target}

    # ── RSI ─────────────────────────────────────────
    rsi = calc_rsi(buf.closes)
    if rsi is not None and rsi <= 30:
        if price > prev_close and price > bars[-1]["open"]:
            stop = price * (1 - 0.005)
            target = price * (1 + 0.015)
            return {"strategy": "RSI", "stop": stop,
                    "target": target}

    return None


# ─────────────────────────────────────────────────────────────
# ポジション管理
# ─────────────────────────────────────────────────────────────

class Position:
    """アクティブポジションの追跡。"""

    def __init__(self, symbol: str, entry_p: float, qty: int,
                 stop: float, target: float, strategy: str):
        self.symbol = symbol
        self.entry_p = entry_p
        self.qty = qty
        self.stop = stop
        self.target = target
        self.orig_stop = stop
        self.strategy = strategy
        self.trailing = False
        self.entry_time = datetime.now(JST)

    def check_exit(self, price: float) -> str | None:
        """決済条件をチェック。Returns: 理由 or None。"""
        # トレーリング: 含み益50%で建値撤退
        if not self.trailing and self.target > self.entry_p:
            progress = (price - self.entry_p) / (self.target - self.entry_p)
            if progress >= 0.5:
                self.stop = self.entry_p
                self.trailing = True
                log.info("  → トレーリング発動 (建値撤退)")

        if price >= self.target:
            return "目標達成"
        if price <= self.stop:
            return "建値撤退" if self.trailing else "損切り"
        return None


# ─────────────────────────────────────────────────────────────
# メインループ
# ─────────────────────────────────────────────────────────────

def calc_position_size(entry_p: float, stop_p: float,
                       budget: int, max_risk: int) -> int:
    risk = abs(entry_p - stop_p)
    if risk <= 0:
        return 100
    qty = int(max_risk / risk / 100) * 100
    qty = min(qty, int(budget / entry_p / 100) * 100)
    return max(100, qty)


def run_bot(args) -> None:
    client = KabuClient()
    client.refresh_token()

    symbol = args.symbol
    budget = args.budget
    max_risk = args.max_risk
    dry_run = args.dry_run
    poll = args.poll

    buf = BarBuffer()
    position: Position | None = None
    trades: list[dict] = []
    prev_day: dict | None = None

    # 前日のピボット計算 (前日終値は初回価格取得時に推定)
    try:
        board = client.get_board(symbol)
        prev_close = board.get("PreviousClose") or board.get("CalcPrice")
        prev_high = board.get("PreviousHigh") or prev_close
        prev_low = board.get("PreviousLow") or prev_close
        if prev_close and prev_high and prev_low:
            pp = (prev_high + prev_low + prev_close) / 3
            s1 = 2 * pp - prev_high
            prev_day = {"pp": pp, "s1": s1}
            log.info("前日ピボット: PP=%.0f S1=%.0f", pp, s1)
    except Exception as e:
        log.warning("前日データ取得失敗: %s", e)

    mode = "DRY RUN" if dry_run else "本番"
    log.info("=" * 50)
    log.info("  kabu デイトレボット [%s]", mode)
    log.info("  銘柄: %s / 予算: %s円 / リスク: %s円/trade",
             symbol, f"{budget:,}", f"{max_risk:,}")
    log.info("=" * 50)

    while True:
        now = datetime.now(JST)
        t = now.time()

        # 15:00 以降は終了
        if t >= dtime(15, 0):
            log.info("大引け → 終了")
            break

        # 9:00 前は待機
        if t < dtime(8, 59):
            time.sleep(30)
            continue

        # 昼休み (11:30-12:30)
        if dtime(11, 30) <= t < dtime(12, 30):
            time.sleep(30)
            continue

        try:
            price = client.get_price(symbol)
        except Exception as e:
            log.error("価格取得失敗: %s", e)
            time.sleep(poll)
            continue

        # 5分足更新
        completed_bar = buf.update(price, now)
        if completed_bar:
            log.info("5m足確定: %s O=%.0f H=%.0f L=%.0f C=%.0f",
                     completed_bar["time"].strftime("%H:%M"),
                     completed_bar["open"], completed_bar["high"],
                     completed_bar["low"], completed_bar["close"])

        # ── 保有中: 決済チェック ─────────────────────
        if position:
            # 14:55 強制決済
            if t >= dtime(14, 55):
                reason = "引け強制"
                log.info("★ 強制決済 (%s) 現在値=%.0f", reason, price)
                if not dry_run:
                    try:
                        result = client.send_order(
                            symbol, side="1", qty=position.qty)
                        log.info("  注文結果: %s", result)
                    except Exception as e:
                        log.error("  注文エラー: %s", e)
                pnl = (price - position.entry_p) * position.qty
                trades.append({
                    "strategy": position.strategy,
                    "entry_p": position.entry_p, "exit_p": price,
                    "qty": position.qty, "pnl": pnl, "reason": reason,
                    "entry_time": position.entry_time,
                    "exit_time": now,
                })
                log.info("  損益: %+,.0f円", pnl)
                position = None
                continue

            reason = position.check_exit(price)
            if reason:
                log.info("★ 決済 (%s) 現在値=%.0f", reason, price)
                if not dry_run:
                    try:
                        result = client.send_order(
                            symbol, side="1", qty=position.qty)
                        log.info("  注文結果: %s", result)
                    except Exception as e:
                        log.error("  注文エラー: %s", e)
                pnl = (price - position.entry_p) * position.qty
                trades.append({
                    "strategy": position.strategy,
                    "entry_p": position.entry_p, "exit_p": price,
                    "qty": position.qty, "pnl": pnl, "reason": reason,
                    "entry_time": position.entry_time,
                    "exit_time": now,
                })
                log.info("  損益: %+,.0f円", pnl)
                position = None

        # ── 未保有: シグナルチェック ─────────────────
        elif position is None and t < dtime(11, 0):
            sig = check_signals(buf, prev_day, now)
            if sig:
                qty = calc_position_size(
                    price, sig["stop"], budget, max_risk)
                log.info("★ シグナル [%s] 価格=%.0f 損切=%.0f "
                         "目標=%.0f 株数=%d",
                         sig["strategy"], price, sig["stop"],
                         sig["target"], qty)
                if not dry_run:
                    try:
                        result = client.send_order(
                            symbol, side="2", qty=qty)
                        log.info("  注文結果: %s", result)
                    except Exception as e:
                        log.error("  注文エラー: %s", e)
                        time.sleep(poll)
                        continue

                position = Position(
                    symbol, price, qty,
                    sig["stop"], sig["target"], sig["strategy"])

        time.sleep(poll)

    # ── 日次レポート ────────────────────────────────
    print()
    print("=" * 50)
    print(f"  日次レポート  {now.strftime('%Y-%m-%d')}  [{mode}]")
    print("=" * 50)
    if trades:
        total_pnl = sum(t["pnl"] for t in trades)
        for t in trades:
            mark = "○" if t["pnl"] > 0 else "●"
            print(f"  {mark} [{t['strategy']:<6}] "
                  f"{t['entry_p']:,.0f}→{t['exit_p']:,.0f} "
                  f"{t['pnl']:+,.0f}円 ({t['reason']})")
        print(f"\n  合計: {total_pnl:+,.0f}円  "
              f"{len(trades)}トレード")
    else:
        print("  トレードなし")
    print("=" * 50)


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="kabuステーション デイトレ自動売買ボット")
    parser.add_argument("--symbol", default="8306",
                        help="銘柄コード (デフォルト: 8306 三菱UFJ)")
    parser.add_argument("--budget", type=int, default=600_000)
    parser.add_argument("--max-risk", type=int, default=6_000)
    parser.add_argument("--poll", type=int, default=10,
                        help="価格取得間隔 (秒)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ドライラン (注文を送信しない)")
    args = parser.parse_args()

    try:
        run_bot(args)
    except KeyboardInterrupt:
        log.info("Ctrl+C → 終了")
    except Exception as e:
        log.error("致命的エラー: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
