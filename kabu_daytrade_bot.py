"""
kabu_daytrade_bot.py  ―  ORB専用 kabuステーション自動売買ボット
==================================================================
バックテスト実績 PF 1.32 / 勝率 52.6% の ORB 戦略で、
kabuステーション REST API 経由で実売買する。

【ORB戦略】
  1. 9:00-9:30 のオープニングレンジ (OR) を記録
  2. 9:30 以降、5分足終値が OR 高値を上抜け → 買い
  3. 損切り: OR 安値
  4. 目標: エントリー + OR幅 × 1.5
  5. トレーリング: 含み益50%で建値撤退に切替
  6. 14:55 強制決済
  7. 1日1ポジ制限

【前提】
  - kabuステーション が起動中
  - .env に設定:
      KABU_API_PASSWORD=your_password
      KABU_BASE_URL=http://localhost:18080  (本番)

【使い方】
  python kabu_daytrade_bot.py --dry-run --symbol 8306   # ドライラン
  python kabu_daytrade_bot.py --symbol 8306              # 本番
  python kabu_daytrade_bot.py --symbol 8306 --budget 600000
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import requests

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── .env ────────────────────────────────────────────────────
def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
        return

_load_dotenv()

# ── ORB パラメータ ──────────────────────────────────────────
OR_MINUTES     = 30       # オープニングレンジ: 9:00-9:30
GAP_MAX_PCT    = 2.0      # ギャップフィルター: 前日比2%超は見送り
TARGET_K       = 1.5      # 目標 = エントリー + OR幅 × K
TRAILING_TRIGGER = 0.5    # 含み益50%でトレーリング発動
FORCE_CLOSE_TIME = dtime(14, 55)
ENTRY_CUTOFF     = dtime(11, 0)  # 前場のみ


# ─────────────────────────────────────────────────────────────
# kabuステーション API
# ─────────────────────────────────────────────────────────────

class KabuClient:
    def __init__(self):
        self.password = os.environ.get("KABU_API_PASSWORD", "")
        self.base_url = os.environ.get("KABU_BASE_URL", "http://localhost:18080")
        self.token = None
        if not self.password:
            raise RuntimeError("KABU_API_PASSWORD を .env に設定してください")

    def refresh_token(self):
        r = requests.post(
            f"{self.base_url}/kabusapi/token",
            json={"APIPassword": self.password},
            headers={"Content-Type": "application/json"}, timeout=10)
        r.raise_for_status()
        self.token = r.json()["Token"]
        log.info("トークン取得成功")

    def _h(self):
        return {"X-API-KEY": self.token, "Content-Type": "application/json"}

    def get_board(self, symbol, exchange=1):
        r = requests.get(f"{self.base_url}/kabusapi/board/{symbol}@{exchange}",
                         headers=self._h(), timeout=10)
        if r.status_code == 401:
            self.refresh_token()
            r = requests.get(f"{self.base_url}/kabusapi/board/{symbol}@{exchange}",
                             headers=self._h(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_price(self, symbol, exchange=1):
        data = self.get_board(symbol, exchange)
        price = data.get("CurrentPrice") or data.get("CalcPrice")
        if price is None:
            raise ValueError(f"価格取得失敗: {symbol}")
        return float(price)

    def buy(self, symbol, qty, exchange=1):
        return self._order(symbol, "2", qty, exchange)

    def sell(self, symbol, qty, exchange=1):
        return self._order(symbol, "1", qty, exchange)

    def _order(self, symbol, side, qty, exchange=1):
        body = {
            "Password": self.password, "Symbol": symbol,
            "Exchange": exchange, "SecurityType": 1, "Side": side,
            "CashMargin": 1, "DelivType": 2, "FundType": "AA",
            "AccountType": 4, "Qty": qty,
            "FrontOrderType": 2, "Price": 0, "ExpireDay": 0,
        }
        r = requests.post(f"{self.base_url}/kabusapi/sendorder",
                          headers=self._h(), json=body, timeout=10)
        if r.status_code == 401:
            self.refresh_token()
            r = requests.post(f"{self.base_url}/kabusapi/sendorder",
                              headers=self._h(), json=body, timeout=10)
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────────────────────
# 5分足バッファ + ORB判定
# ─────────────────────────────────────────────────────────────

class ORBTracker:
    """5分足の構築とORBシグナル判定を一体化。"""

    def __init__(self):
        self.bars: list[dict] = []
        self._cur: dict | None = None
        self._bar_start = None
        self.or_hi = 0.0
        self.or_lo = 0.0
        self.or_w = 0.0
        self.or_confirmed = False
        self.signal_fired = False  # 1日1回制限

    def update(self, price: float, now: datetime) -> dict | None:
        """価格を入力。5分足確定時にバーを返す。"""
        bar_time = now.replace(minute=(now.minute // 5) * 5,
                               second=0, microsecond=0)
        if self._bar_start is None or bar_time > self._bar_start:
            completed = self._cur
            self._cur = {"time": bar_time, "open": price,
                         "high": price, "low": price, "close": price}
            self._bar_start = bar_time
            if completed:
                self.bars.append(completed)
                self._update_or(completed)
                return completed
            return None
        self._cur["high"] = max(self._cur["high"], price)
        self._cur["low"] = min(self._cur["low"], price)
        self._cur["close"] = price
        return None

    def _update_or(self, bar: dict):
        """OR (9:00-9:30) を更新。"""
        if bar["time"].time() < dtime(9, 30):
            if self.or_hi == 0:
                self.or_hi = bar["high"]
                self.or_lo = bar["low"]
            else:
                self.or_hi = max(self.or_hi, bar["high"])
                self.or_lo = min(self.or_lo, bar["low"])
            self.or_w = self.or_hi - self.or_lo
        elif not self.or_confirmed and self.or_w > 0:
            self.or_confirmed = True
            log.info("OR確定: 高値=%.0f 安値=%.0f 幅=%.0f",
                     self.or_hi, self.or_lo, self.or_w)

    def check_signal(self, now: datetime,
                     prev_close: float | None = None) -> dict | None:
        """ORBシグナルを判定 (ギャップフィルター付き)。"""
        if self.signal_fired or not self.or_confirmed:
            return None
        # ギャップフィルター
        if prev_close and prev_close > 0 and self.bars:
            open_p = self.bars[0]["open"]
            gap = abs(open_p - prev_close) / prev_close * 100
            if gap > GAP_MAX_PCT:
                if not self.signal_fired:
                    self.signal_fired = True  # この日は取引しない
                    log.info("ギャップ %.1f%% > %.1f%% → 本日見送り",
                             gap, GAP_MAX_PCT)
                return None
        if now.time() < dtime(9, 30) or now.time() >= ENTRY_CUTOFF:
            return None
        if len(self.bars) < 2:
            return None

        price = self.bars[-1]["close"]
        prev = self.bars[-2]["close"]

        if price > self.or_hi and price > prev:
            self.signal_fired = True
            return {
                "stop": self.or_lo,
                "target": price + self.or_w * TARGET_K,
            }
        return None


# ─────────────────────────────────────────────────────────────
# ポジション管理
# ─────────────────────────────────────────────────────────────

class Position:
    def __init__(self, entry_p, qty, stop, target):
        self.entry_p = entry_p
        self.qty = qty
        self.stop = stop
        self.target = target
        self.trailing = False
        self.entry_time = datetime.now(JST)

    def check_exit(self, price: float) -> str | None:
        if not self.trailing and self.target > self.entry_p:
            progress = (price - self.entry_p) / (self.target - self.entry_p)
            if progress >= TRAILING_TRIGGER:
                self.stop = self.entry_p
                self.trailing = True
                log.info("  トレーリング発動 → 建値撤退に切替")

        if price >= self.target:
            return "目標達成"
        if price <= self.stop:
            return "建値撤退" if self.trailing else "損切り"
        return None


# ─────────────────────────────────────────────────────────────
# ポジションサイジング
# ─────────────────────────────────────────────────────────────

def calc_qty(entry_p, stop_p, budget, max_risk):
    risk = abs(entry_p - stop_p)
    if risk <= 0:
        return 100
    qty = int(max_risk / risk / 100) * 100
    qty = min(qty, int(budget / entry_p / 100) * 100)
    return max(100, qty)


# ─────────────────────────────────────────────────────────────
# メインループ
# ─────────────────────────────────────────────────────────────

def run(args):
    client = KabuClient()
    client.refresh_token()

    symbol = args.symbol
    budget = args.budget
    max_risk = args.max_risk
    dry = args.dry_run
    poll = args.poll

    tracker = ORBTracker()
    pos: Position | None = None
    trades: list[dict] = []
    prev_close: float | None = None

    # 前日終値を取得 (ギャップフィルター用)
    try:
        board = client.get_board(symbol)
        prev_close = board.get("PreviousClose")
        if prev_close:
            prev_close = float(prev_close)
            log.info("前日終値: %.0f", prev_close)
    except Exception:
        pass

    mode = "DRY RUN" if dry else "本番"
    log.info("=" * 50)
    log.info("  ORB デイトレボット [%s]", mode)
    log.info("  銘柄: %s / 予算: %s円 / リスク: %s円",
             symbol, f"{budget:,}", f"{max_risk:,}")
    log.info("  OR: 9:00-9:30 / 目標: OR幅×%.1f / Gap≤%.1f%% / 前場限定",
             TARGET_K, GAP_MAX_PCT)
    log.info("=" * 50)

    while True:
        now = datetime.now(JST)
        t = now.time()

        if t >= dtime(15, 0):
            log.info("大引け → 終了")
            break
        if t < dtime(8, 59):
            time.sleep(30)
            continue
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
        bar = tracker.update(price, now)
        if bar:
            log.info("5m: %s O=%.0f H=%.0f L=%.0f C=%.0f",
                     bar["time"].strftime("%H:%M"),
                     bar["open"], bar["high"], bar["low"], bar["close"])

        # ── 保有中 → 決済チェック ───────────────────
        if pos:
            if t >= FORCE_CLOSE_TIME:
                reason = "引け強制"
            else:
                reason = pos.check_exit(price)

            if reason:
                pnl = (price - pos.entry_p) * pos.qty
                log.info("★ 決済 [%s] %.0f→%.0f  %+,.0f円",
                         reason, pos.entry_p, price, pnl)
                if not dry:
                    try:
                        res = client.sell(symbol, pos.qty)
                        log.info("  注文: %s", res)
                    except Exception as e:
                        log.error("  注文失敗: %s", e)
                trades.append({
                    "entry": pos.entry_p, "exit": price,
                    "qty": pos.qty, "pnl": pnl, "reason": reason,
                    "entry_time": pos.entry_time, "exit_time": now,
                })
                pos = None

        # ── 未保有 → シグナルチェック ─────────────────
        elif pos is None:
            sig = tracker.check_signal(now, prev_close=prev_close)
            if sig:
                qty = calc_qty(price, sig["stop"], budget, max_risk)
                log.info("★ ORBシグナル! 価格=%.0f OR高値=%.0f "
                         "損切=%.0f 目標=%.0f 株数=%d",
                         price, tracker.or_hi,
                         sig["stop"], sig["target"], qty)
                if not dry:
                    try:
                        res = client.buy(symbol, qty)
                        log.info("  注文: %s", res)
                    except Exception as e:
                        log.error("  注文失敗: %s", e)
                        time.sleep(poll)
                        continue
                pos = Position(price, qty, sig["stop"], sig["target"])

        time.sleep(poll)

    # ── 日次レポート ──────────────────────────────
    print()
    print("=" * 50)
    print(f"  ORBデイトレ日次レポート [{mode}]")
    print(f"  {now.strftime('%Y-%m-%d')}  銘柄: {symbol}")
    print("=" * 50)
    if tracker.or_confirmed:
        print(f"  OR: 高値={tracker.or_hi:,.0f}  "
              f"安値={tracker.or_lo:,.0f}  幅={tracker.or_w:,.0f}")
    if trades:
        total = sum(t["pnl"] for t in trades)
        for t in trades:
            m = "○" if t["pnl"] > 0 else "●"
            print(f"  {m} {t['entry']:,.0f}→{t['exit']:,.0f}  "
                  f"{t['pnl']:+,.0f}円  ({t['reason']})")
        print(f"\n  合計: {total:+,.0f}円")
    else:
        print("  トレードなし")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="ORB デイトレ自動売買")
    parser.add_argument("--symbol", default="8306",
                        help="銘柄コード (デフォルト: 8306 三菱UFJ)")
    parser.add_argument("--budget", type=int, default=600_000)
    parser.add_argument("--max-risk", type=int, default=6_000)
    parser.add_argument("--poll", type=int, default=10, help="取得間隔(秒)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ドライラン (注文しない)")
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("Ctrl+C → 終了")


if __name__ == "__main__":
    main()
