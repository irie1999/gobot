"""
kabuステーション REST API 自動売買ボット
戦略: 移動平均クロス（短期MA > 長期MA → 買い、短期MA < 長期MA → 売り）
"""

import time
import logging
import requests
from collections import deque
from datetime import datetime

# ---- 設定 ----
API_PASSWORD = "your_password_here"   # kabuステーションのAPIパスワード
BASE_URL = "http://localhost:18081"   # デモ: 18081 / 本番: 18080

SYMBOL = "9433"          # 銘柄コード（例: KDDI）
EXCHANGE = 1             # 取引所 (1=東証)
QTY = 100                # 注文数量（株）

SHORT_PERIOD = 5         # 短期MA期間（足数）
LONG_PERIOD = 25         # 長期MA期間（足数）
POLL_INTERVAL = 10       # 価格取得間隔（秒）

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---- API クライアント ----

class KabuClient:
    def __init__(self, password: str, base_url: str = BASE_URL):
        self.base_url = base_url
        self.password = password
        self.token: str | None = None

    def refresh_token(self) -> None:
        url = f"{self.base_url}/kabusapi/token"
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"APIPassword": self.password},
            timeout=10,
        )
        resp.raise_for_status()
        self.token = resp.json()["Token"]
        log.info("トークン取得成功")

    def _headers(self) -> dict:
        return {"X-API-KEY": self.token, "Content-Type": "application/json"}

    def get_price(self, symbol: str, exchange: int = 1) -> float:
        """現在値を取得する"""
        url = f"{self.base_url}/kabusapi/board/{symbol}@{exchange}"
        resp = requests.get(url, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # CurrentPrice が None の場合は CalcPrice にフォールバック
        price = data.get("CurrentPrice") or data.get("CalcPrice")
        if price is None:
            raise ValueError(f"価格取得失敗: {data}")
        return float(price)

    def send_order(
        self,
        symbol: str,
        exchange: int,
        side: str,   # "2"=買い, "1"=売り
        qty: int,
        order_type: int = 2,    # 2=成行
        price: float = 0,
    ) -> dict:
        """注文を送信する"""
        url = f"{self.base_url}/kabusapi/sendorder"
        body = {
            "Password": self.password,
            "Symbol": symbol,
            "Exchange": exchange,
            "SecurityType": 1,   # 1=株式
            "Side": side,
            "CashMargin": 1,     # 1=現物
            "DelivType": 2,      # 2=お預り金
            "FundType": "AA",    # AA=お預り金
            "AccountType": 4,    # 4=特定
            "Qty": qty,
            "FrontOrderType": order_type,
            "Price": price,
            "ExpireDay": 0,      # 0=当日
        }
        resp = requests.post(url, headers=self._headers(), json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_positions(self, symbol: str) -> list[dict]:
        """建玉・保有株を取得する"""
        url = f"{self.base_url}/kabusapi/positions"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={"Symbol": symbol, "Side": "2"},  # Side=2: 買い建玉
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json() or []


# ---- 移動平均クロス戦略 ----

class MACrossStrategy:
    def __init__(self, short_period: int, long_period: int):
        self.short_period = short_period
        self.long_period = long_period
        self.prices: deque[float] = deque(maxlen=long_period)
        self.prev_signal: str | None = None  # "buy" | "sell" | None

    def update(self, price: float) -> str | None:
        """
        価格を追加しシグナルを返す。
        Returns: "buy" | "sell" | None
        """
        self.prices.append(price)
        if len(self.prices) < self.long_period:
            log.info(
                "データ蓄積中 (%d/%d) 現在値: %.2f",
                len(self.prices), self.long_period, price,
            )
            return None

        short_ma = sum(list(self.prices)[-self.short_period:]) / self.short_period
        long_ma = sum(self.prices) / self.long_period
        log.info("現在値: %.2f  短期MA(%d): %.2f  長期MA(%d): %.2f",
                 price, self.short_period, short_ma, self.long_period, long_ma)

        signal = "buy" if short_ma > long_ma else "sell"

        if signal != self.prev_signal:
            self.prev_signal = signal
            return signal  # クロス発生
        return None


# ---- メインループ ----

def run_bot():
    client = KabuClient(API_PASSWORD, BASE_URL)
    strategy = MACrossStrategy(SHORT_PERIOD, LONG_PERIOD)

    client.refresh_token()
    has_position = False

    log.info("自動売買開始 銘柄:%s 数量:%d 短期MA:%d 長期MA:%d",
             SYMBOL, QTY, SHORT_PERIOD, LONG_PERIOD)

    while True:
        try:
            price = client.get_price(SYMBOL, EXCHANGE)
            signal = strategy.update(price)

            if signal == "buy" and not has_position:
                log.info("★ 買いシグナル → 成行買い注文")
                result = client.send_order(SYMBOL, EXCHANGE, side="2", qty=QTY)
                log.info("注文結果: %s", result)
                has_position = True

            elif signal == "sell" and has_position:
                log.info("★ 売りシグナル → 成行売り注文")
                result = client.send_order(SYMBOL, EXCHANGE, side="1", qty=QTY)
                log.info("注文結果: %s", result)
                has_position = False

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                log.warning("トークン期限切れ → 再取得")
                client.refresh_token()
            else:
                log.error("HTTPエラー: %s", e)

        except requests.exceptions.ConnectionError:
            log.error("接続エラー: kabuステーションが起動しているか確認してください")

        except Exception as e:
            log.error("予期しないエラー: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_bot()
