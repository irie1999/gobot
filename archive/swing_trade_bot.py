"""
スイングトレード 自動売買ボット【現物取引版】
銘柄    : トヨタ自動車 (7203)
API     : kabuステーション REST API
戦略    : MA(20/60) + RSI(14) + Bollinger Bands(20, 2σ)
足種    : 日足（終値ベース）
ポジション保有期間: 数日〜数週間（最大 MAX_HOLD_DAYS 営業日）

■ 現物取引ルール:
  - ロングのみ（買い→売り）
  - タイムカット: MAX_HOLD_DAYS 営業日超過で強制決済
  - 手数料: 0 円想定（ネット証券無料プラン）
  - 毎日 15:30（大引け後）に価格取得 → シグナル判定 → 注文

■ 起動方法:
  pip install requests schedule
  python swing_trade_bot.py

⚠ 注意: 本番運用前にデモ環境（port 18081）で十分テストしてください。
"""

import math
import logging
import time
from collections import deque
from datetime import datetime

import requests
import schedule

# ── 設定 ─────────────────────────────────────────────────────
API_PASSWORD  = "your_password_here"   # kabuステーション APIパスワード
BASE_URL      = "http://localhost:18081"  # デモ: 18081 / 本番: 18080

SYMBOL        = "7203"    # トヨタ自動車
EXCHANGE      = 1         # 1 = 東証
LOT_SIZE      = 100       # 1 単元
MAX_QTY       = 500       # 最大保有株数
MAX_HOLD_DAYS = 20        # 最大保有営業日数

# MA クロス（日足）
SHORT_PERIOD   = 20
LONG_PERIOD    = 60

# RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70

# Bollinger Bands
BB_PERIOD = 20
BB_K      = 2.0

# ── ロギング ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("swing_trade_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── kabuステーション API クライアント ─────────────────────────
class KabuClient:
    def __init__(self, password: str, base_url: str):
        self.base_url = base_url
        self.password = password
        self.token: str | None = None

    def refresh_token(self) -> None:
        resp = requests.post(
            f"{self.base_url}/kabusapi/token",
            headers={"Content-Type": "application/json"},
            json={"APIPassword": self.password},
            timeout=10,
        )
        resp.raise_for_status()
        self.token = resp.json()["Token"]
        log.info("トークン取得成功")

    def _headers(self) -> dict:
        return {"X-API-KEY": self.token, "Content-Type": "application/json"}

    def get_price(self) -> float:
        """現在値（終値）を取得する"""
        resp = requests.get(
            f"{self.base_url}/kabusapi/board/{SYMBOL}@{EXCHANGE}",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data  = resp.json()
        price = data.get("CurrentPrice") or data.get("CalcPrice")
        if price is None:
            raise ValueError(f"価格取得失敗: {data}")
        return float(price)

    def send_order(self, side: str, qty: int) -> dict:
        """成行注文を送信する  side: '2'=買い / '1'=売り"""
        resp = requests.post(
            f"{self.base_url}/kabusapi/sendorder",
            headers=self._headers(),
            json={
                "Password":       self.password,
                "Symbol":         SYMBOL,
                "Exchange":       EXCHANGE,
                "SecurityType":   1,    # 株式
                "Side":           side,
                "CashMargin":     1,    # 現物
                "DelivType":      2,    # お預り金
                "FundType":       "AA",
                "AccountType":    4,    # 特定口座
                "Qty":            qty,
                "FrontOrderType": 2,    # 成行
                "Price":          0,
                "ExpireDay":      0,    # 当日
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_wallet(self) -> float:
        """買付余力を取得する"""
        resp = requests.get(
            f"{self.base_url}/kabusapi/wallet/cash",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json().get("StockAccountWallet", 0))


# ── インジケーター エンジン ────────────────────────────────────
class IndicatorEngine:
    def __init__(self):
        maxlen = max(LONG_PERIOD, BB_PERIOD, RSI_PERIOD + 1)
        self._prices: deque[float]  = deque(maxlen=maxlen)
        self._gains:  deque[float]  = deque(maxlen=RSI_PERIOD)
        self._losses: deque[float]  = deque(maxlen=RSI_PERIOD)
        self._prev_close: float | None = None
        self._prev_sig:   str | None   = None

    def _ma(self, n: int) -> float | None:
        lst = list(self._prices)
        return sum(lst[-n:]) / n if len(lst) >= n else None

    def _rsi(self) -> float | None:
        if len(self._gains) < RSI_PERIOD:
            return None
        ag = sum(self._gains) / RSI_PERIOD
        al = sum(self._losses) / RSI_PERIOD
        return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

    def _bollinger(self) -> tuple[float | None, float | None]:
        lst = list(self._prices)
        if len(lst) < BB_PERIOD:
            return None, None
        chunk = lst[-BB_PERIOD:]
        mean  = sum(chunk) / BB_PERIOD
        std   = math.sqrt(sum((x - mean) ** 2 for x in chunk) / BB_PERIOD)
        return mean - BB_K * std, mean + BB_K * std

    def feed(self, price: float) -> dict:
        if self._prev_close is not None:
            diff = price - self._prev_close
            self._gains.append(max(diff, 0.0))
            self._losses.append(max(-diff, 0.0))
        self._prev_close = price
        self._prices.append(price)

        s = self._ma(SHORT_PERIOD)
        l = self._ma(LONG_PERIOD)
        ma_signal = None
        if s is not None and l is not None:
            sig = "up" if s > l else "down"
            if sig != self._prev_sig:
                self._prev_sig = sig
                ma_signal = sig

        bb_lower, bb_upper = self._bollinger()
        rsi = self._rsi()
        log.info(
            "終値: %.1f  MA%d: %s  MA%d: %s  RSI: %s  BB下: %s  BB上: %s",
            price,
            SHORT_PERIOD, f"{s:.1f}" if s else "-",
            LONG_PERIOD,  f"{l:.1f}" if l else "-",
            f"{rsi:.1f}"  if rsi else "-",
            f"{bb_lower:.1f}" if bb_lower else "-",
            f"{bb_upper:.1f}" if bb_upper else "-",
        )
        return {"ma_signal": ma_signal, "rsi": rsi,
                "bb_lower": bb_lower, "bb_upper": bb_upper}


# ── シグナル判定 ──────────────────────────────────────────────
def should_buy(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "up":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi < RSI_OVERSOLD:
        return True
    bb_l = ind["bb_lower"]
    if bb_l is not None and price <= bb_l and (ind["rsi"] or 50) < 50:
        return True
    return False

def should_sell(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "down":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi > RSI_OVERBOUGHT:
        return True
    bb_u = ind["bb_upper"]
    if bb_u is not None and price >= bb_u:
        return True
    return False


# ── ボット本体 ────────────────────────────────────────────────
class SwingBot:
    def __init__(self):
        self.client   = KabuClient(API_PASSWORD, BASE_URL)
        self.engine   = IndicatorEngine()
        self.in_pos   = False
        self.entry_p  = 0.0
        self.entry_dt: datetime | None = None
        self.hold_days = 0
        self.qty       = 0

    def _calc_qty(self, price: float) -> int:
        try:
            wallet = self.client.get_wallet()
        except Exception:
            wallet = 0.0
        lots = min(int(wallet / (price * LOT_SIZE)), MAX_QTY // LOT_SIZE)
        return lots * LOT_SIZE

    def _buy(self, price: float) -> None:
        qty = self._calc_qty(price)
        if qty <= 0:
            log.warning("買付余力不足 → スキップ")
            return
        result = self.client.send_order(side="2", qty=qty)
        self.in_pos    = True
        self.entry_p   = price
        self.entry_dt  = datetime.now()
        self.hold_days = 0
        self.qty       = qty
        pnl_est        = 0.0
        log.info("★ 買い注文  %.1f円 × %d株  結果: %s", price, qty, result)

    def _sell(self, price: float, reason: str) -> None:
        result  = self.client.send_order(side="1", qty=self.qty)
        pnl     = (price - self.entry_p) * self.qty
        held    = (datetime.now() - self.entry_dt).days if self.entry_dt else 0
        log.info(
            "★ 売り注文  %.1f円 × %d株  損益: %+,.0f円  保有: %d日  理由: %s  結果: %s",
            price, self.qty, pnl, held, reason, result,
        )
        self.in_pos = False
        self.qty    = 0

    def tick(self) -> None:
        """毎営業日 15:30 に呼び出す"""
        now = datetime.now()
        if now.weekday() >= 5:   # 土日はスキップ
            return

        try:
            self.client.refresh_token()
            price = self.client.get_price()
        except requests.exceptions.HTTPError as e:
            log.error("HTTPエラー: %s", e)
            return
        except requests.exceptions.ConnectionError:
            log.error("接続エラー: kabuステーションが起動しているか確認してください")
            return
        except Exception as e:
            log.error("価格取得失敗: %s", e)
            return

        ind = self.engine.feed(price)

        if self.in_pos:
            self.hold_days += 1
            if self.hold_days >= MAX_HOLD_DAYS:
                self._sell(price, f"タイムカット({MAX_HOLD_DAYS}日)")
            elif should_sell(ind, price):
                self._sell(price, "シグナル")
            else:
                log.info("ポジション保有中  取得値: %.1f  現在値: %.1f  保有: %d日",
                         self.entry_p, price, self.hold_days)

        if not self.in_pos and should_buy(ind, price):
            self._buy(price)

        if not self.in_pos:
            log.info("ポジションなし  待機中")


# ── エントリーポイント ─────────────────────────────────────────
if __name__ == "__main__":
    bot = SwingBot()
    log.info("=" * 60)
    log.info("スイングトレード自動売買ボット 起動")
    log.info("銘柄: %s  MA(%d/%d)  RSI(%d)  BB(%d,%.1fσ)  最大保有: %d日",
             SYMBOL, SHORT_PERIOD, LONG_PERIOD, RSI_PERIOD,
             BB_PERIOD, BB_K, MAX_HOLD_DAYS)
    log.info("毎営業日 15:30 に実行します")
    log.info("=" * 60)

    schedule.every().day.at("15:30").do(bot.tick)

    while True:
        schedule.run_pending()
        time.sleep(30)
