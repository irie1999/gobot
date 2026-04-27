"""
kabu_gap_reversal_bot.py  ―  GapReversal 自動売買ボット (並列稼働用)
==================================================================
Donchian (順張り) と異なる「ギャップダウン後の反発を狙う逆張り」戦略。
kabu_donchian_bot.py と並列実行することで、機会捕捉が増加する。

【戦略】
  寄付で前日比 -1〜-3% のギャップダウン → 反発を狙う逆張り買い
  - 当日安値の0.999倍をstop
  - 目標: 前日終値復帰
  - 50%進捗で建値撤退

【注意】
  - kabu_donchian_bot.py と同時起動の場合、APIレート制限に注意
  - poll間隔を長め(60秒推奨)に
  - 同じkabu STATIONアプリ&同じAPIパスワードを使用

【使い方】
  python kabu_gap_reversal_bot.py --dry-run --budget 200000 --max-risk 1000 \\
      --max-concurrent 1 --margin --poll 60
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
    format="%(asctime)s [%(levelname)s][GapRev] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


# ── パラメータ ──────────────────────────────────────────────
GAP_MIN_PCT      = 1.0    # 寄付ギャップ最小 (前日比-1%以上)
GAP_MAX_PCT      = 3.0    # 寄付ギャップ最大 (前日比-3%まで、暴落除外)
VOL_MULT         = 1.2    # 出来高フィルタ (直前5本平均×1.2)
STOP_MAX_PCT     = 2.0    # 損切り上限
TARGET_PCT       = 0.0    # 0=前日終値復帰、>0で固定%目標
TRAIL_TRIGGER    = 0.5    # 50%進捗で建値撤退
FORCE_CLOSE_TIME = dtime(14, 50)
ENTRY_CUTOFF     = dtime(11, 0)   # 寄付反発狙いなので前場限定
WARMUP_BARS      = 3
BUDGET           = 600_000
MAX_RISK         = 3_000
MAX_CONCURRENT   = 5


# ── 監視銘柄 ───────────────────────────────────────────────
def _load_watch_symbols():
    try:
        from daytrade_donchian_winners import SYMBOLS as DON_SYMBOLS
        return [(s.replace(".T", ""), n) for s, n in DON_SYMBOLS]
    except ImportError:
        log.warning("daytrade_donchian_winners.py が見つかりません")
        return []


WATCH_SYMBOLS = _load_watch_symbols()


# ─────────────────────────────────────────────────────────────
# kabuステーション API (kabu_donchian_bot.py と同じ設計)
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
        registered = []
        failed = []
        for s in symbols:
            try:
                body = {"Symbols": [{"Symbol": s, "Exchange": 1}]}
                r = requests.put(f"{self.base_url}/kabusapi/register",
                                 headers=self._h(), json=body, timeout=10)
                if r.status_code == 401:
                    self.refresh_token()
                    r = requests.put(f"{self.base_url}/kabusapi/register",
                                     headers=self._h(), json=body, timeout=10)
                if r.status_code == 200:
                    items = r.json().get("RegistList", [])
                    if any(item.get("Symbol") == s for item in items):
                        registered.append(s)
                    else:
                        failed.append(s)
                else:
                    failed.append(s)
            except Exception:
                failed.append(s)
            time.sleep(0.2)
        return registered, failed

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

    def buy(self, symbol, qty, exchange=1, margin=False):
        if margin:
            return self._margin_order(symbol, "2", qty, exchange, open_pos=True)
        return self._order(symbol, "2", qty, exchange)

    def sell(self, symbol, qty, exchange=1, margin=False):
        if margin:
            return self._margin_order(symbol, "1", qty, exchange, open_pos=False)
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
        r.raise_for_status()
        return r.json()

    def _margin_order(self, symbol, side, qty, exchange=1, open_pos=True):
        body = {
            "Password": self.password, "Symbol": symbol,
            "Exchange": exchange, "SecurityType": 1, "Side": side,
            "CashMargin": 2 if open_pos else 3,
            "MarginTradeType": 3, "DelivType": 0 if open_pos else 2,
            "AccountType": 4, "Qty": qty,
            "FrontOrderType": 2, "Price": 0, "ExpireDay": 0,
        }
        if not open_pos:
            body["ClosePositionOrder"] = 0
        r = requests.post(f"{self.base_url}/kabusapi/sendorder",
                          headers=self._h(), json=body, timeout=10)
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────────────────────
# GapReversal トラッカー
# ─────────────────────────────────────────────────────────────

class GapTracker:
    """1銘柄の寄付ギャップ判定 + 反発シグナル監視。"""

    def __init__(self, symbol: str, name: str):
        self.symbol = symbol
        self.name = name
        self.prev_close: float | None = None
        self.day_open: float | None = None
        self.gap_pct: float | None = None
        self.gap_qualified = False  # 対象銘柄か (-1〜-3%ギャップダウン)
        self.day_low: float = float("inf")
        self.bars: list[dict] = []
        self._cur_bar_start: datetime | None = None
        self._cur_open: float | None = None
        self._cur_high = 0.0
        self._cur_low = 0.0
        self._cur_close = 0.0
        self._cur_volume = 0
        self.signal_fired = False

    def update(self, price: float, now: datetime):
        bar_time = now.replace(minute=(now.minute // 5) * 5,
                               second=0, microsecond=0)
        if self._cur_bar_start is None:
            self._cur_bar_start = bar_time
            self._cur_open = price
            self._cur_high = price
            self._cur_low = price
            self._cur_close = price
            self.day_open = price
            # 寄付時にギャップ判定
            if self.prev_close and self.prev_close > 0:
                self.gap_pct = (price - self.prev_close) / self.prev_close * 100
                if -GAP_MAX_PCT <= self.gap_pct <= -GAP_MIN_PCT:
                    self.gap_qualified = True
                    log.info("  ギャップDown対象: %s %s gap=%.1f%%",
                             self.symbol, self.name, self.gap_pct)
        elif bar_time > self._cur_bar_start:
            self.bars.append({
                "time": self._cur_bar_start,
                "open": self._cur_open,
                "high": self._cur_high,
                "low": self._cur_low,
                "close": self._cur_close,
            })
            self._cur_bar_start = bar_time
            self._cur_open = price
            self._cur_high = price
            self._cur_low = price
            self._cur_close = price
        else:
            self._cur_high = max(self._cur_high, price)
            self._cur_low = min(self._cur_low, price)
            self._cur_close = price
        self.day_low = min(self.day_low, price)

    def check_signal(self) -> dict | None:
        """反発シグナル: 当日安値以降、寄付値超え。"""
        if self.signal_fired or not self.gap_qualified:
            return None
        if len(self.bars) < WARMUP_BARS or self.day_open is None:
            return None
        last = self.bars[-1]
        # 反発確認: 直近バーが寄付値超え + 陽線
        if last["close"] > self.day_open and last["close"] > last["open"]:
            self.signal_fired = True
            entry_p = last["close"]
            stop_p = max(self.day_low * 0.999,
                         entry_p * (1 - STOP_MAX_PCT / 100))
            target_p = self.prev_close   # 前日終値復帰目標
            if entry_p >= target_p or entry_p <= stop_p:
                return None
            return dict(entry_p=entry_p, stop=stop_p, target=target_p,
                        gap=self.gap_pct)
        return None


# ─────────────────────────────────────────────────────────────
# Position / サイジング (kabu_donchian_bot と同じ)
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
        """決済要否を判定。"""
        if price <= self.stop:
            return "建値撤退" if self.trailing else "損切り"
        if price >= self.target:
            return "目標達成"
        # 50%進捗で建値撤退
        if not self.trailing and self.target > self.entry_p:
            progress = (price - self.entry_p) / (self.target - self.entry_p)
            if progress >= TRAIL_TRIGGER:
                self.stop = self.entry_p
                self.trailing = True
                log.info("  → 建値撤退発動")
        return None


def calc_qty(entry_p, stop_p, budget, max_risk):
    risk_per_share = abs(entry_p - stop_p)
    if risk_per_share <= 0:
        return 100
    qty_by_risk = int(max_risk / risk_per_share / 100) * 100
    qty_by_budget = int(budget / entry_p / 100) * 100
    return max(100, min(qty_by_risk, qty_by_budget))


# ─────────────────────────────────────────────────────────────
# メインループ
# ─────────────────────────────────────────────────────────────

def run(args):
    if not WATCH_SYMBOLS:
        log.error("監視銘柄なし → 終了")
        sys.exit(1)

    client = KabuClient()
    client.refresh_token()

    syms = [s for s, _ in WATCH_SYMBOLS]
    registered, failed = client.register(syms)
    log.info("銘柄登録: %d/%d 銘柄成功", len(registered), len(syms))
    if failed:
        log.warning("登録失敗: %d銘柄 → %s", len(failed),
                    failed[:10] if len(failed) > 10 else failed)
    active = [(s, n) for s, n in WATCH_SYMBOLS if s in registered]
    if not active:
        log.error("登録できた銘柄が0件 → 終了")
        return

    dry = args.dry_run
    budget = args.budget
    max_risk = args.max_risk
    max_concurrent = args.max_concurrent
    margin = args.margin
    poll = args.poll

    trackers = {s: GapTracker(s, n) for s, n in active}
    positions: dict[str, Position] = {}
    trades: list[dict] = []

    # 前日終値取得
    for sym, tracker in trackers.items():
        try:
            _, prev = client.get_price(sym)
            tracker.prev_close = prev
        except Exception:
            pass

    mode = "DRY RUN" if dry else "本番"
    trade_type = "デイトレ信用" if margin else "現物"
    log.info("=" * 60)
    log.info("  GapReversal %d銘柄監視ボット [%s / %s]",
             len(active), mode, trade_type)
    log.info("  予算: %s円 / リスク: %s円 / 最大同時保有: %d",
             f"{budget:,}", f"{max_risk:,}", max_concurrent)
    log.info("=" * 60)

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

        # 全銘柄の価格更新
        for sym, tracker in trackers.items():
            try:
                price, _ = client.get_price(sym)
                if price is None:
                    continue
                tracker.update(price, now)
            except Exception as e:
                log.debug("  %s 価格取得失敗: %s", sym, e)

        # 保有ポジ決済チェック
        to_close = []
        for sym, pos in positions.items():
            try:
                price, _ = client.get_price(sym)
                if price is None:
                    continue
            except Exception:
                continue
            if t >= FORCE_CLOSE_TIME:
                reason = "引け強制"
            else:
                reason = pos.check_exit(price)
            if reason:
                pnl = (price - pos.entry_p) * pos.qty
                log.info("★ 決済 [%s] %s %s  %.0f→%.0f  qty=%d  %+,.0f円",
                         reason, pos.symbol, pos.name,
                         pos.entry_p, price, pos.qty, pnl)
                if not dry:
                    try:
                        client.sell(pos.symbol, pos.qty, margin=margin)
                    except Exception as e:
                        log.error("  注文失敗: %s", e)
                trades.append({
                    "symbol": pos.symbol, "name": pos.name,
                    "entry": pos.entry_p, "exit": price,
                    "qty": pos.qty, "pnl": pnl, "reason": reason,
                })
                to_close.append(sym)
        for sym in to_close:
            del positions[sym]

        # 新規シグナル (前場限定)
        if (len(positions) < max_concurrent
                and dtime(9, 5) <= t < ENTRY_CUTOFF):
            for sym, tracker in trackers.items():
                if sym in positions:
                    continue
                if len(positions) >= max_concurrent:
                    break
                sig = tracker.check_signal()
                if sig:
                    qty = calc_qty(sig["entry_p"], sig["stop"], budget, max_risk)
                    log.info("★ Gap反発! %s %s  価格=%.0f gap=%.1f%% "
                             "損切=%.0f 目標=%.0f 株数=%d",
                             sym, tracker.name, sig["entry_p"], sig["gap"],
                             sig["stop"], sig["target"], qty)
                    if not dry:
                        try:
                            client.buy(sym, qty, margin=margin)
                        except Exception as e:
                            log.error("  注文失敗: %s", e)
                            continue
                    pos = Position(sym, tracker.name, sig["entry_p"], qty,
                                   sig["stop"], sig["target"])
                    positions[sym] = pos
                    log.info("  → 保有中: %d/%d", len(positions), max_concurrent)

        time.sleep(poll)

    # 日次レポート
    print()
    print("=" * 65)
    print(f"  GapReversal {len(active)}銘柄監視 日次レポート [{mode}]")
    print(f"  {now.strftime('%Y-%m-%d')}")
    print("=" * 65)

    if trades:
        wins = [tr for tr in trades if tr["pnl"] > 0]
        total = sum(tr["pnl"] for tr in trades)
        win_rate = len(wins) / len(trades) * 100
        gp = sum(tr["pnl"] for tr in wins)
        gl = abs(sum(tr["pnl"] for tr in trades if tr["pnl"] <= 0))
        pf = gp / gl if gl > 0 else float("inf")
        print(f"\n  取引: {len(trades)}  勝率: {win_rate:.1f}%  "
              f"PF: {pf:.2f}  損益: {total:+,.0f}円")
        for tr in trades:
            m = "○" if tr["pnl"] > 0 else "●"
            print(f"  {m} {tr['name']} ({tr['symbol']})  "
                  f"{tr['entry']:,.0f}→{tr['exit']:,.0f}  "
                  f"{tr['pnl']:+,.0f}円 ({tr['reason']})")
    else:
        print("\n  トレードなし")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="GapReversal 自動売買ボット")
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--max-risk", type=int, default=MAX_RISK)
    parser.add_argument("--max-concurrent", type=int, default=MAX_CONCURRENT)
    parser.add_argument("--poll", type=int, default=60,
                        help="取得間隔(秒、Donchianとの並列実行を考慮し60推奨)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--margin", action="store_true",
                        help="信用取引(デイトレ信用)で発注")
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("Ctrl+C → 終了")


if __name__ == "__main__":
    main()
