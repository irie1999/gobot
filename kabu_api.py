"""
kabu_api.py — kabuステーション REST API 連携クライアント
=========================================================

逆指値シグナル運用 (run_signals / check_signals_*) の発注・建玉管理を
kabuステーション API に流し込むための共通クライアント。

これまで `kabu_token.py` (トークン取得のみ) と各スクリプトに散らばっていた
発注処理を 1 つの `KabuClient` に集約する。

【設計方針】
  - デモ(18081) / 本番(18080) を切替。既定はデモ (誤発注防止)。
  - dry_run=True なら発注系メソッドは API を叩かず内容を表示するだけ。
  - 逆指値買いエントリー / 引け成行(MOC)決済 / 損切り逆指値 を高水準メソッドで提供。
  - 現物 (CashMargin=1) / 信用新規 (2) / 信用返済 (3) を引数で切替。

【参照 API】
  https://kabucom.github.io/kabusapi/ptal/index.html

  POST /token           トークン取得
  GET  /board/{sym@ex}  時価 (CurrentPrice 等)
  GET  /positions       建玉一覧
  GET  /wallet/cash     買付余力
  POST /sendorder       発注
  GET  /orders          注文一覧
  PUT  /cancelorder     取消

使い方:
  from kabu_api import KabuClient
  cli = KabuClient(prod=False, dry_run=True)   # デモ + dry-run
  cli.connect()                                 # トークン取得
  price = cli.get_board(7203)["CurrentPrice"]
  cli.send_stop_buy(7203, qty=100, trigger_price=3000)   # 逆指値買い
  cli.send_moc(7203, qty=100, side="sell")               # 引け成行決済
"""

from __future__ import annotations

import os
from typing import Any

import requests

# ── kabu API 定数 ──────────────────────────────────────────────
EXCHANGE_TOSHO = 1            # 市場コード: 東証

SIDE_SELL = "1"              # 売
SIDE_BUY = "2"              # 買

CASH_GENBUTSU = 1            # 現物
CASH_MARGIN_OPEN = 2         # 信用新規
CASH_MARGIN_CLOSE = 3        # 信用返済

# 執行条件 (FrontOrderType)
FOT_MARKET = 10              # 成行
FOT_MOO = 13                 # 寄成
FOT_MOC = 16                 # 引成 (引け成行) ← close 損切りで使用
FOT_LIMIT = 20               # 指値
FOT_STOP = 30                # 逆指値 ← エントリーで使用

# 逆指値 (ReverseLimitOrder) 用
TRIGGER_AFTER_ORDER = 1      # TriggerSec: 1=発注後
UNDER = 1                    # UnderOver: 1=以下
OVER = 2                     # UnderOver: 2=以上
AFTERHIT_MARKET = 1          # AfterHitOrderType: 1=成行

DEMO_URL = "http://localhost:18081"
PROD_URL = "http://localhost:18080"


class KabuClient:
    """kabuステーション REST API クライアント。"""

    def __init__(self, prod: bool = False, dry_run: bool = True,
                 password: str | None = None, timeout: float = 10.0):
        """
        Parameters
        ----------
        prod : bool
            True で本番(18080)、False でデモ(18081)。既定デモ。
        dry_run : bool
            True なら発注系メソッドは API を叩かず内容を表示するだけ。
        password : str | None
            API パスワード。None なら環境変数 KABU_API_PASSWORD を使う。
        timeout : float
            HTTP タイムアウト秒。
        """
        self.prod = prod
        self.dry_run = dry_run
        self.base_url = PROD_URL if prod else DEMO_URL
        self.timeout = timeout
        self._password = password or os.environ.get("KABU_API_PASSWORD")
        self._token: str | None = None

    # ── 接続/認証 ────────────────────────────────────────────
    @property
    def env_label(self) -> str:
        return "本番(18080)" if self.prod else "デモ(18081)"

    def connect(self) -> str:
        """トークンを取得して以降のリクエストに使う。"""
        if not self._password:
            raise RuntimeError(
                "API パスワードがありません。環境変数 KABU_API_PASSWORD を設定するか "
                "KabuClient(password=...) を渡してください。")
        url = f"{self.base_url}/kabusapi/token"
        r = requests.post(url, json={"APIPassword": self._password},
                          headers={"Content-Type": "application/json"},
                          timeout=self.timeout)
        r.raise_for_status()
        token = r.json().get("Token")
        if not token:
            raise RuntimeError(f"トークン取得失敗: {r.json()}")
        self._token = token
        return token

    def _headers(self, with_content: bool = False) -> dict:
        if not self._token:
            raise RuntimeError("未接続です。先に connect() を呼んでください。")
        h = {"X-API-KEY": self._token}
        if with_content:
            h["Content-Type"] = "application/json"
        return h

    # ── 情報取得 (GET) ───────────────────────────────────────
    def get_board(self, symbol: int | str, exchange: int = EXCHANGE_TOSHO) -> dict:
        """時価情報 (CurrentPrice 等) を取得。"""
        url = f"{self.base_url}/kabusapi/board/{symbol}@{exchange}"
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_current_price(self, symbol: int | str,
                          exchange: int = EXCHANGE_TOSHO) -> float | None:
        """現在値だけ取り出す。取得失敗時は None。"""
        try:
            v = self.get_board(symbol, exchange).get("CurrentPrice")
            return float(v) if v is not None else None
        except Exception:
            return None

    def get_positions(self, product: int = 0) -> list[dict]:
        """建玉一覧。product: 0=すべて 1=現物 2=信用 3=先物 4=OP。"""
        url = f"{self.base_url}/kabusapi/positions"
        r = requests.get(url, headers=self._headers(),
                         params={"product": product}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_cash(self) -> dict:
        """買付余力。"""
        url = f"{self.base_url}/kabusapi/wallet/cash"
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_orders(self) -> list[dict]:
        """注文一覧。"""
        url = f"{self.base_url}/kabusapi/orders"
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── 発注 (POST) ──────────────────────────────────────────
    def _post_order(self, body: dict, label: str) -> dict:
        """sendorder の共通処理。dry_run なら送信せず内容を表示。"""
        if self.dry_run:
            print(f"  [dry-run] {label}: {body}")
            return {"Result": 0, "OrderId": "DRYRUN", "_dry_run": True}
        body = dict(body, Password=self._password)
        url = f"{self.base_url}/kabusapi/sendorder"
        r = requests.post(url, headers=self._headers(with_content=True),
                          json=body, timeout=self.timeout)
        r.raise_for_status()
        res = r.json()
        if res.get("Result") != 0:
            print(f"  ✗ {label} 発注失敗: {res}")
        else:
            print(f"  ✓ {label} 発注成功 OrderId={res.get('OrderId')}")
        return res

    def _base_order(self, symbol: int | str, side: str, qty: int,
                    cash_margin: int) -> dict:
        """発注ボディの共通部分。"""
        body: dict[str, Any] = {
            "Symbol": str(symbol),
            "Exchange": EXCHANGE_TOSHO,
            "SecurityType": 1,        # 株式
            "Side": side,
            "CashMargin": cash_margin,
            "AccountType": 4,         # 特定
            "Qty": qty,
            "ExpireDay": 0,           # 当日
        }
        if cash_margin == CASH_GENBUTSU:
            body["DelivType"] = 2     # 現物買は お預り金
        else:
            body["DelivType"] = 0 if cash_margin == CASH_MARGIN_OPEN else 2
            body["MarginTradeType"] = 1  # 制度信用
        return body

    def send_stop_buy(self, symbol: int | str, qty: int, trigger_price: float,
                      cash_margin: int = CASH_GENBUTSU) -> dict:
        """逆指値買いエントリー (trigger_price 以上で成行買い)。

        逆指値シグナルの order_price をそのまま trigger_price に渡す。
        """
        body = self._base_order(symbol, SIDE_BUY, qty, cash_margin)
        body["FrontOrderType"] = FOT_STOP
        body["Price"] = 0
        body["ReverseLimitOrder"] = {
            "TriggerSec": TRIGGER_AFTER_ORDER,
            "TriggerPrice": round(trigger_price),
            "UnderOver": OVER,          # 以上 (ブレイク方向)
            "AfterHitOrderType": AFTERHIT_MARKET,
            "AfterHitPrice": 0,
        }
        return self._post_order(body, f"逆指値買い {symbol} x{qty} @≥{trigger_price:.0f}")

    def send_stop_sell(self, symbol: int | str, qty: int, trigger_price: float,
                       cash_margin: int = CASH_GENBUTSU) -> dict:
        """損切り逆指値 (trigger_price 以下で成行売り)。ザラ場 intraday 損切り用。"""
        body = self._base_order(symbol, SIDE_SELL, qty, cash_margin)
        body["FrontOrderType"] = FOT_STOP
        body["Price"] = 0
        body["ReverseLimitOrder"] = {
            "TriggerSec": TRIGGER_AFTER_ORDER,
            "TriggerPrice": round(trigger_price),
            "UnderOver": UNDER,         # 以下 (下落で損切り)
            "AfterHitOrderType": AFTERHIT_MARKET,
            "AfterHitPrice": 0,
        }
        return self._post_order(body, f"損切り逆指値 {symbol} x{qty} @≤{trigger_price:.0f}")

    def send_moc(self, symbol: int | str, qty: int, side: str = "sell",
                 cash_margin: int = CASH_GENBUTSU) -> dict:
        """引け成行 (MOC) 決済。close 方式の損切りで使う。

        side: "sell"=売り決済(ロング決済) / "buy"=買い戻し(ショート決済)。
        """
        kabu_side = SIDE_SELL if side == "sell" else SIDE_BUY
        body = self._base_order(symbol, kabu_side, qty, cash_margin)
        body["FrontOrderType"] = FOT_MOC
        body["Price"] = 0
        return self._post_order(body, f"引け成行 {symbol} x{qty} ({side})")

    def cancel_order(self, order_id: str) -> dict:
        """注文取消。"""
        if self.dry_run:
            print(f"  [dry-run] 取消: {order_id}")
            return {"Result": 0, "_dry_run": True}
        url = f"{self.base_url}/kabusapi/cancelorder"
        body = {"OrderId": order_id, "Password": self._password}
        r = requests.put(url, headers=self._headers(with_content=True),
                         json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    # 簡易疎通テスト (デモ口座, dry-run なので発注はしない)
    import argparse
    ap = argparse.ArgumentParser(description="kabu API クライアント疎通テスト")
    ap.add_argument("--prod", action="store_true", help="本番口座(18080)")
    ap.add_argument("--symbol", default="7203", help="時価を取る銘柄コード")
    args = ap.parse_args()

    cli = KabuClient(prod=args.prod, dry_run=True)
    try:
        cli.connect()
        print(f"接続成功 ({cli.env_label})")
        board = cli.get_board(args.symbol)
        print(f"{args.symbol} 現在値: {board.get('CurrentPrice')}")
        print("dry-run 発注テスト:")
        cli.send_stop_buy(args.symbol, qty=100, trigger_price=9999)
        cli.send_moc(args.symbol, qty=100, side="sell")
    except requests.exceptions.ConnectionError:
        print("接続エラー: kabuステーションが起動しているか確認してください。")
    except Exception as e:
        print(f"エラー: {e}")
