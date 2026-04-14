"""
kabu_daytrade_bot.py  ―  ORB 10銘柄同時監視 自動売買ボット
==================================================================
10銘柄を同時に監視し、最初にORBブレイクした銘柄1つだけに注文。

【動作】
  9:00-9:30  10銘柄の OR (高値・安値) を記録
  9:30~      全銘柄のブレイクを監視
  ブレイク発生 → 最初の1銘柄だけ成行買い (残りは無視)
  保有中 → 損切り/目標/トレーリング監視
  14:55 → 強制決済
  15:00 → 日次レポート

【使い方】
  python kabu_daytrade_bot.py --dry-run        # ドライラン
  python kabu_daytrade_bot.py                  # 本番

【設定】
  .env:
    KABU_API_PASSWORD=your_password
    KABU_BASE_URL=http://localhost:18080
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

# .env
def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        return

_load_dotenv()

# ── パラメータ ──────────────────────────────────────────────
OR_MINUTES       = 30
GAP_MAX_PCT      = 2.0
TARGET_K         = 1.5
TRAILING_TRIGGER = 0.5
FORCE_CLOSE_TIME = dtime(14, 55)
ENTRY_CUTOFF     = dtime(11, 0)
BUDGET           = 600_000
MAX_RISK         = 6_000

# ── 監視銘柄 (2年スキャン上位30) ────────────────────────────
WATCH_SYMBOLS = [
    ("6501", "日立製作所"),
    ("6266", "タツモ"),
    ("6315", "TOWA"),
    ("6701", "NEC"),
    ("4684", "オービック"),
    ("4973", "日本高純度化学"),
    ("8022", "ミズノ"),
    ("3817", "SRA HD"),
    ("9984", "ソフトバンクG"),
    ("8012", "長瀬産業"),
    ("4967", "小林製薬"),
    ("6330", "東洋エンジ"),
    ("7181", "かんぽ生命"),
    ("7012", "川崎重工"),
    ("1414", "ショーボンド"),
    ("6465", "ホシザキ"),
    ("5332", "TOTO"),
    ("8522", "名古屋銀行"),
    ("1518", "三井松島HD"),
    ("7013", "IHI"),
    ("8056", "BIPROGY"),
    ("8267", "イオン"),
    ("6324", "日本精機"),
    ("6590", "芝浦メカトロ"),
    ("7322", "三十三FG"),
    ("8830", "住友不動産"),
    ("7011", "三菱重工業"),
    ("4502", "武田薬品"),
    ("7911", "TOPPAN HD"),
    ("4183", "三井化学"),
]


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
        r = requests.post(f"{self.base_url}/kabusapi/token",
                          json={"APIPassword": self.password},
                          headers={"Content-Type": "application/json"}, timeout=10)
        r.raise_for_status()
        self.token = r.json()["Token"]
        log.info("トークン取得成功")

    def _h(self):
        return {"X-API-KEY": self.token, "Content-Type": "application/json"}

    def register(self, symbols):
        """銘柄登録 (板情報取得に必要)。"""
        body = {"Symbols": [{"Symbol": s, "Exchange": 1} for s in symbols]}
        r = requests.put(f"{self.base_url}/kabusapi/register",
                         headers=self._h(), json=body, timeout=10)
        r.raise_for_status()

    def get_price(self, symbol, exchange=1):
        r = requests.get(f"{self.base_url}/kabusapi/board/{symbol}@{exchange}",
                         headers=self._h(), timeout=10)
        if r.status_code == 401:
            self.refresh_token()
            r = requests.get(f"{self.base_url}/kabusapi/board/{symbol}@{exchange}",
                             headers=self._h(), timeout=10)
        r.raise_for_status()
        data = r.json()
        price = data.get("CurrentPrice") or data.get("CalcPrice")
        prev = data.get("PreviousClose")
        return float(price) if price else None, float(prev) if prev else None

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
# 銘柄ごとの OR トラッカー
# ─────────────────────────────────────────────────────────────

class SymbolTracker:
    """1銘柄の OR 計算 + ブレイク判定。"""

    def __init__(self, symbol: str, name: str):
        self.symbol = symbol
        self.name = name
        self.or_hi = 0.0
        self.or_lo = 0.0
        self.or_w = 0.0
        self.or_confirmed = False
        self.signal_fired = False
        self.prev_close: float | None = None
        self.gap_skip = False
        self.prices: list[float] = []
        self.bar_prices: list[float] = []
        self._bar_start = None

    def update(self, price: float, now: datetime):
        """価格を入力し、5分足とORを更新。"""
        bar_time = now.replace(minute=(now.minute // 5) * 5,
                               second=0, microsecond=0)
        if self._bar_start is None or bar_time > self._bar_start:
            if self.bar_prices:
                bar_hi = max(self.bar_prices)
                bar_lo = min(self.bar_prices)
                bar_cl = self.bar_prices[-1]
                self.prices.append(bar_cl)
                if self._bar_start and self._bar_start.time() < dtime(9, 30):
                    if self.or_hi == 0:
                        self.or_hi, self.or_lo = bar_hi, bar_lo
                    else:
                        self.or_hi = max(self.or_hi, bar_hi)
                        self.or_lo = min(self.or_lo, bar_lo)
                    self.or_w = self.or_hi - self.or_lo
                elif not self.or_confirmed and self.or_w > 0:
                    self.or_confirmed = True
            self.bar_prices = [price]
            self._bar_start = bar_time
        else:
            self.bar_prices.append(price)

    def check_gap(self, price: float):
        """ギャップフィルター (初回価格取得時に呼ぶ)。"""
        if self.prev_close and self.prev_close > 0:
            gap = abs(price - self.prev_close) / self.prev_close * 100
            if gap > GAP_MAX_PCT:
                self.gap_skip = True
                log.info("  %s %s: ギャップ %.1f%% → 見送り",
                         self.symbol, self.name, gap)

    def check_signal(self, price: float) -> dict | None:
        """ORBシグナル判定。"""
        if self.signal_fired or not self.or_confirmed or self.gap_skip:
            return None
        if len(self.prices) < 2:
            return None
        if price > self.or_hi and price > self.prices[-1]:
            self.signal_fired = True
            stop = self.or_lo
            target = price + self.or_w * TARGET_K
            if price <= stop:
                return None
            return {"stop": stop, "target": target}
        return None


# ─────────────────────────────────────────────────────────────
# ポジション / サイジング
# ─────────────────────────────────────────────────────────────

class Position:
    def __init__(self, symbol, name, entry_p, qty, stop, target):
        self.symbol = symbol
        self.name = name
        self.entry_p = entry_p
        self.qty = qty
        self.stop = stop
        self.target = target
        self.trailing = False
        self.entry_time = datetime.now(JST)

    def check_exit(self, price):
        if not self.trailing and self.target > self.entry_p:
            if (price - self.entry_p) / (self.target - self.entry_p) >= TRAILING_TRIGGER:
                self.stop = self.entry_p
                self.trailing = True
                log.info("  → トレーリング発動 (建値撤退)")
        if price >= self.target:
            return "目標達成"
        if price <= self.stop:
            return "建値撤退" if self.trailing else "損切り"
        return None


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

    # 銘柄登録
    syms = [s for s, _ in WATCH_SYMBOLS]
    client.register(syms)
    log.info("銘柄登録: %d銘柄", len(syms))

    dry = args.dry_run
    poll = args.poll
    budget = args.budget
    max_risk = args.max_risk

    # トラッカー初期化
    trackers = {s: SymbolTracker(s, n) for s, n in WATCH_SYMBOLS}
    pos: Position | None = None
    trades: list[dict] = []

    # 前日終値を取得
    for sym, tracker in trackers.items():
        try:
            _, prev = client.get_price(sym)
            tracker.prev_close = prev
        except Exception:
            pass

    mode = "DRY RUN" if dry else "本番"
    log.info("=" * 55)
    log.info("  ORB 10銘柄同時監視ボット [%s]", mode)
    log.info("  予算: %s円 / リスク: %s円", f"{budget:,}", f"{max_risk:,}")
    log.info("  監視: %s", ", ".join(f"{n}({s})" for s, n in WATCH_SYMBOLS))
    log.info("=" * 55)

    first_update = True

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

        # 全銘柄の価格を取得
        for sym, tracker in trackers.items():
            try:
                price, _ = client.get_price(sym)
                if price is None:
                    continue
                if first_update:
                    tracker.check_gap(price)
                tracker.update(price, now)

                # OR 確定ログ (1回だけ)
                if tracker.or_confirmed and tracker.or_w > 0:
                    if not hasattr(tracker, "_or_logged"):
                        log.info("  OR確定 %s %s: H=%.0f L=%.0f W=%.0f",
                                 sym, tracker.name,
                                 tracker.or_hi, tracker.or_lo, tracker.or_w)
                        tracker._or_logged = True

            except Exception as e:
                log.debug("  %s 価格取得失敗: %s", sym, e)

        first_update = False

        # ── 保有中 → 決済チェック ───────────────────
        if pos:
            try:
                price, _ = client.get_price(pos.symbol)
                if price is None:
                    time.sleep(poll)
                    continue
            except Exception:
                time.sleep(poll)
                continue

            if t >= FORCE_CLOSE_TIME:
                reason = "引け強制"
            else:
                reason = pos.check_exit(price)

            if reason:
                pnl = (price - pos.entry_p) * pos.qty
                log.info("★ 決済 [%s] %s %s  %.0f→%.0f  %+,.0f円",
                         reason, pos.symbol, pos.name,
                         pos.entry_p, price, pnl)
                if not dry:
                    try:
                        res = client.sell(pos.symbol, pos.qty)
                        log.info("  注文: %s", res)
                    except Exception as e:
                        log.error("  注文失敗: %s", e)
                trades.append({
                    "symbol": pos.symbol, "name": pos.name,
                    "entry": pos.entry_p, "exit": price,
                    "qty": pos.qty, "pnl": pnl, "reason": reason,
                })
                pos = None

        # ── 未保有 → 10銘柄のシグナルチェック ──────
        elif pos is None and t >= dtime(9, 30) and t < ENTRY_CUTOFF:
            for sym, tracker in trackers.items():
                try:
                    price, _ = client.get_price(sym)
                    if price is None:
                        continue
                except Exception:
                    continue

                sig = tracker.check_signal(price)
                if sig:
                    qty = calc_qty(price, sig["stop"], budget, max_risk)
                    log.info("★ ORBシグナル! %s %s  価格=%.0f "
                             "OR高値=%.0f 損切=%.0f 目標=%.0f 株数=%d",
                             sym, tracker.name, price,
                             tracker.or_hi, sig["stop"], sig["target"], qty)
                    if not dry:
                        try:
                            res = client.buy(sym, qty)
                            log.info("  注文: %s", res)
                        except Exception as e:
                            log.error("  注文失敗: %s", e)
                            continue
                    pos = Position(sym, tracker.name, price, qty,
                                   sig["stop"], sig["target"])
                    # 最初の1銘柄だけ → 残りは無視
                    for _, tr in trackers.items():
                        tr.signal_fired = True
                    break

        time.sleep(poll)

    # ── 日次レポート ──────────────────────────────
    print()
    print("=" * 55)
    print(f"  ORB 10銘柄同時監視 日次レポート [{mode}]")
    print(f"  {now.strftime('%Y-%m-%d')}")
    print("=" * 55)

    # OR情報
    print("\n  【OR (9:00-9:30)】")
    for sym, tracker in trackers.items():
        if tracker.or_confirmed:
            gap = "見送" if tracker.gap_skip else ""
            print(f"    {tracker.name:<14} {sym}  "
                  f"H={tracker.or_hi:>8,.0f}  L={tracker.or_lo:>8,.0f}  "
                  f"W={tracker.or_w:>6,.0f}  {gap}")

    if trades:
        total = sum(t["pnl"] for t in trades)
        print("\n  【トレード】")
        for t in trades:
            m = "○" if t["pnl"] > 0 else "●"
            print(f"    {m} {t['name']} ({t['symbol']})  "
                  f"{t['entry']:,.0f}→{t['exit']:,.0f}  "
                  f"{t['pnl']:+,.0f}円  ({t['reason']})")
        print(f"\n  合計: {total:+,.0f}円")
    else:
        print("\n  トレードなし")
    print("=" * 55)


def main():
    parser = argparse.ArgumentParser(
        description="ORB 10銘柄同時監視 デイトレボット")
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--max-risk", type=int, default=MAX_RISK)
    parser.add_argument("--poll", type=int, default=10, help="取得間隔(秒)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("Ctrl+C → 終了")


if __name__ == "__main__":
    main()
