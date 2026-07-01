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

import datetime
import os
import time
from typing import Any

import requests


def _next_trading_day_int() -> int:
    """次の営業日を YYYYMMDD の int で返す (祝日考慮なし)。MOO の ExpireDay に使う。"""
    d = datetime.date.today() + datetime.timedelta(days=1)
    while d.weekday() >= 5:  # 5=土, 6=日
        d += datetime.timedelta(days=1)
    return int(d.strftime("%Y%m%d"))

# ── kabu API 定数 ──────────────────────────────────────────────
EXCHANGE_TOSHO = 1            # 市場コード: 東証 (board/register/positions 等の照会用)
# 2026/02 最良執行方針対応で、新規発注の Exchange=1(東証) は廃止。
# 新規注文は 9(SOR) か 27(東証＋) を使う。照会系は従来通り 1 でよい。
EXCHANGE_SOR = 9             # SOR (スマートオーダールーティング) ← 発注の既定
EXCHANGE_TOKYO_PLUS = 27     # 東証＋ (Tokyo+)

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
AFTERHIT_LIMIT = 2           # AfterHitOrderType: 2=指値

DEMO_URL = "http://localhost:18081"
PROD_URL = "http://localhost:18080"


class KabuClient:
    """kabuステーション REST API クライアント。"""

    def __init__(self, prod: bool = False, dry_run: bool = True,
                 password: str | None = None, timeout: float = 10.0,
                 order_exchange: int = EXCHANGE_SOR):
        """
        Parameters
        ----------
        prod : bool
            True で本番(18080)、False でデモ(18081)。既定デモ。
        dry_run : bool
            True なら発注系メソッドは API を叩かず内容を表示するだけ。
        password : str | None
            API パスワード。None なら環境変数から取得する:
              本番(prod=True)  → KABU_API_PASSWORD_PROD
              デモ(prod=False) → KABU_API_PASSWORD_DEMO
            上記が無ければ KABU_API_PASSWORD にフォールバック (後方互換)。
        timeout : float
            HTTP タイムアウト秒。
        """
        self.prod = prod
        self.dry_run = dry_run
        self.base_url = PROD_URL if prod else DEMO_URL
        self.timeout = timeout
        self.order_exchange = order_exchange   # 発注の市場コード (9=SOR / 27=東証＋)
        self._password = password or self._password_from_env(prod)
        self._token: str | None = None
        self._registered: set[tuple[str, int]] = set()  # /board 用 銘柄登録済み

    @staticmethod
    def _password_from_env(prod: bool) -> str | None:
        """本番/デモで環境変数を使い分ける。無ければ共通変数にフォールバック。"""
        key = "KABU_API_PASSWORD_PROD" if prod else "KABU_API_PASSWORD_DEMO"
        return os.environ.get(key) or os.environ.get("KABU_API_PASSWORD")

    # ── 接続/認証 ────────────────────────────────────────────
    @property
    def env_label(self) -> str:
        return "本番(18080)" if self.prod else "デモ(18081)"

    def connect(self) -> str:
        """トークンを取得して以降のリクエストに使う。"""
        if not self._password:
            env_key = "KABU_API_PASSWORD_PROD" if self.prod else "KABU_API_PASSWORD_DEMO"
            raise RuntimeError(
                f"API パスワードがありません。環境変数 {env_key} "
                "(または KABU_API_PASSWORD) を設定するか KabuClient(password=...) "
                "を渡してください。")
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

    def _get_json(self, url: str, params: dict | None = None,
                  retries: int = 5):
        """GET して JSON を返す。429(レート制限)は指数バックオフで再試行。

        kabuステーション API は短時間に叩くと 429 を返す。建玉取得(/positions)や
        注文取得(/orders)が 429 で例外送出すると損切り成行の発注全体が落ちるため、
        ここで待って再試行する。最終的に失敗したら raise_for_status で送出する。
        """
        last = None
        for i in range(retries):
            r = requests.get(url, headers=self._headers(),
                             params=params, timeout=self.timeout)
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()
            last = r
            wait = 1.5 * (i + 1)
            print(f"  ⚠ 429 レート制限: {wait:.1f}秒待って再試行 ({i+1}/{retries}) {url}")
            time.sleep(wait)
        # ここに来たら最後まで 429。呼び出し側で扱えるよう送出。
        if last is not None:
            last.raise_for_status()
        raise RuntimeError(f"GET 失敗: {url}")

    # ── 情報取得 (GET) ───────────────────────────────────────
    def register(self, symbol: int | str, exchange: int = EXCHANGE_TOSHO) -> None:
        """/board で時価を取る前に必要な銘柄登録 (PUT /register)。

        kabu API は登録していない銘柄の /board が空になる仕様。
        一度登録した銘柄は self._registered にキャッシュして再登録を避ける。
        """
        key = (str(symbol), exchange)
        if key in self._registered:
            return
        url = f"{self.base_url}/kabusapi/register"
        body = {"Symbols": [{"Symbol": str(symbol), "Exchange": exchange}]}
        try:
            r = requests.put(url, headers=self._headers(with_content=True),
                             json=body, timeout=self.timeout)
            r.raise_for_status()
            self._registered.add(key)
        except Exception as e:
            print(f"  ⚠ {symbol}: 銘柄登録(register)失敗 ({e})")

    def get_board(self, symbol: int | str, exchange: int = EXCHANGE_TOSHO) -> dict:
        """時価情報 (CurrentPrice 等) を取得。事前に銘柄登録を行う。"""
        self.register(symbol, exchange)
        url = f"{self.base_url}/kabusapi/board/{symbol}@{exchange}"
        return self._get_json(url)

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
        data = self._get_json(url, params={"product": product})
        return data if isinstance(data, list) else []

    def get_margin_positions(self, symbol: int | str | None = None) -> list[dict]:
        """信用建玉一覧。symbol を指定するとその銘柄のみを返す。"""
        positions = self.get_positions(product=2)
        if symbol is not None:
            positions = [p for p in positions
                         if str(p.get("Symbol", "")) == str(symbol)]
        return positions

    def _build_close_positions(self, symbol: int | str, qty: int,
                                kabu_side: str) -> list[dict]:
        """信用返済の ClosePositions リストを建玉一覧から自動生成する (FIFO)。

        ロング決済 (kabu_side=SIDE_SELL) → 買い建玉 (Side="2") を使う
        ショート決済 (kabu_side=SIDE_BUY)  → 売り建玉 (Side="1") を使う

        ClosePositions.HoldID には positions API の ExecutionID をそのまま使う。
        事前に cancel_open_close_orders() で建玉の拘束を解除してから呼ぶこと。
        """
        target_side = "2" if kabu_side == SIDE_SELL else "1"
        margin_positions = self.get_margin_positions(symbol)
        candidates = [p for p in margin_positions
                      if str(p.get("Side", "")) == target_side]

        close_list: list[dict] = []
        remaining = qty
        for p in candidates:
            hold_id = p.get("ExecutionID", "")
            if not hold_id:
                print(f"  ⚠ {symbol}: 建玉に ExecutionID が見つかりません。")
                continue
            leaves = int(p.get("LeavesQty") or 0)
            use_qty = min(remaining, leaves)
            if use_qty > 0:
                close_list.append({"HoldID": hold_id, "Qty": use_qty})
                remaining -= use_qty
            if remaining <= 0:
                break
        return close_list

    def cancel_open_close_orders(self, symbol: int | str,
                                  side: str = SIDE_SELL) -> list[str]:
        """同一銘柄の未約定 信用返済注文を一括取消して建玉を解放する。

        send_moc / send_sell が 信用返済(CashMargin=3) を発注する前に呼ぶ。
        建玉が別の返済注文に拘束されていると 4001005 になるため。
        """
        ACTIVE_STATES = {1, 2, 3, 4}  # 受付/待機/発注中/一部約定
        cancelled: list[str] = []
        try:
            orders = self.get_orders()
        except Exception as e:
            print(f"  ⚠ get_orders 失敗 (既存注文取消スキップ): {e}")
            return cancelled
        for order in orders:
            if str(order.get("Symbol", "")) != str(symbol):
                continue
            if order.get("CashMargin") != CASH_MARGIN_CLOSE:
                continue
            if order.get("Side") != side:
                continue
            state = int(order.get("OrderState") or order.get("State") or 0)
            if state not in ACTIVE_STATES:
                continue
            order_id = order.get("ID", "")
            if not order_id:
                continue
            print(f"  ↩ 既存返済注文を取消: {order_id} (State={state})")
            res = self.cancel_order(order_id)
            if res.get("Result") == 0 or res.get("_dry_run"):
                cancelled.append(order_id)
        return cancelled

    def get_cash(self) -> dict:
        """買付余力。"""
        url = f"{self.base_url}/kabusapi/wallet/cash"
        return self._get_json(url)

    def get_orders(self) -> list[dict]:
        """注文一覧。"""
        url = f"{self.base_url}/kabusapi/orders"
        data = self._get_json(url)
        return data if isinstance(data, list) else []

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
        # エラー時も kabu の error code/message を見たいので body を読む
        try:
            res = r.json()
        except Exception:
            res = {"_raw": r.text}
        if not r.ok:
            print(f"  ✗ {label} HTTP {r.status_code}: {res}")
            body_display = dict(body, Password="***")
            print(f"    送信ボディ: {body_display}")
            return {"Result": -1, "_http_status": r.status_code, **(
                res if isinstance(res, dict) else {})}
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
            "Exchange": self.order_exchange,   # 9=SOR / 27=東証＋ (1=東証は新規発注で廃止)
            "SecurityType": 1,        # 株式
            "Side": side,
            "CashMargin": cash_margin,
            "AccountType": 4,         # 特定
            "Qty": qty,
            "ExpireDay": 0,           # 当日
        }
        if cash_margin == CASH_GENBUTSU:
            if side == SIDE_BUY:
                body["DelivType"] = 2   # 現物買は お預り金
                body["FundType"] = "AA"  # 信用代用 (現物買は FundType 必須)
            else:
                body["DelivType"] = 0   # 現物売は 指定なし
                body["FundType"] = "  "  # 現物売は半角スペース2つ
        else:
            # 信用新規(2) / 信用返済(3) 共通
            body["MarginTradeType"] = 1   # 制度信用
            if cash_margin == CASH_MARGIN_OPEN:
                body["DelivType"] = 0     # 信用新規: 指定なし
                body["FundType"] = "11"   # 信用新規のみ FundType 必要
            else:
                body["DelivType"] = 2     # 信用返済: お預り金 (公式サンプル準拠)
        return body

    def send_stop_buy(self, symbol: int | str, qty: int, trigger_price: float,
                      cash_margin: int = CASH_GENBUTSU,
                      after_hit_price: float | None = None) -> dict:
        """逆指値買いエントリー (trigger_price 以上で買い)。

        逆指値シグナルの order_price をそのまま trigger_price に渡す。
        after_hit_price=None なら発火後は成行 (実運用の既定)。
        値を渡すと発火後は指値 (時間外テストなど成行が弾かれる場面で使う)。
        """
        body = self._base_order(symbol, SIDE_BUY, qty, cash_margin)
        body["FrontOrderType"] = FOT_STOP
        body["Price"] = 0
        if after_hit_price is None:
            after_type, after_price = AFTERHIT_MARKET, 0
        else:
            after_type, after_price = AFTERHIT_LIMIT, round(after_hit_price)
        body["ReverseLimitOrder"] = {
            "TriggerSec": TRIGGER_AFTER_ORDER,
            "TriggerPrice": round(trigger_price),
            "UnderOver": OVER,          # 以上 (ブレイク方向)
            "AfterHitOrderType": after_type,
            "AfterHitPrice": after_price,
        }
        return self._post_order(body, f"逆指値買い {symbol} x{qty} @≥{trigger_price:.0f}")

    def send_buy(self, symbol: int | str, qty: int, price: float | None = None,
                 cash_margin: int = CASH_MARGIN_OPEN,
                 order_type: str = "market",
                 close_positions: list[dict] | None = None,
                 expire_day: int | None = None,
                 omit_close_positions: bool = False) -> dict:
        """買い注文 (新規エントリー or 信用返済の買戻し)。

        order_type:
          "market" = 成行 (ザラ場中のみ。時間外は kabu に弾かれる)
          "limit"  = 指値 (price 必須。その価格以下で約定)
          "moo"    = 寄成 (翌寄付きの成行。時間外でも発注できる)
        cash_margin:
          CASH_MARGIN_OPEN(2) = 信用新規 (既定) / CASH_GENBUTSU(1) = 現物
          CASH_MARGIN_CLOSE(3) = 信用返済(買戻し)。ショート利確/決済に使う
        """
        body = self._base_order(symbol, SIDE_BUY, qty, cash_margin)
        if expire_day is not None:
            body["ExpireDay"] = expire_day
        if order_type == "limit":
            if price is None:
                raise ValueError("order_type='limit' には price が必要です。")
            body["FrontOrderType"] = FOT_LIMIT
            body["Price"] = round(price)
            label = f"指値買い {symbol} x{qty} @{round(price)}"
        elif order_type == "moo":
            body["FrontOrderType"] = FOT_MOO
            body["Price"] = 0
            body["Exchange"] = EXCHANGE_TOKYO_PLUS  # MOO は SOR(9) 非対応
            body["ExpireDay"] = _next_trading_day_int()  # MOO は翌営業日を指定
            label = f"寄成買い {symbol} x{qty}"
        else:  # market
            body["FrontOrderType"] = FOT_MARKET
            body["Price"] = 0
            label = f"成行買い {symbol} x{qty}"

        # 信用返済(買戻し)の ClosePositions (省略時は API の自動割当て)
        if cash_margin == CASH_MARGIN_CLOSE and not omit_close_positions:
            if close_positions is not None:
                body["ClosePositions"] = close_positions
            elif self.dry_run:
                body["ClosePositions"] = [{"HoldID": "(実行時に自動取得)", "Qty": qty}]
            else:
                try:
                    self.cancel_open_close_orders(symbol, SIDE_BUY)
                    cp = self._build_close_positions(symbol, qty, SIDE_BUY)
                except Exception as e:
                    # 建玉/注文取得が 429 等で失敗しても損切り(買戻し)は止めない。
                    print(f"  ⚠ {symbol}: 建玉取得に失敗 ({e}) → ClosePositions 省略で発注続行")
                    cp = None
                if cp:
                    body["ClosePositions"] = cp
                elif cp is not None:
                    print(f"  ⚠ {symbol}: 返済対象の売建玉が見つかりません。発注をスキップします。")
                    return {"Result": -1, "Message": "建玉なし"}
                # cp is None (取得失敗): ClosePositions を付けず自動割当てに任せる

        kind = ("信用返済(買戻)" if cash_margin == CASH_MARGIN_CLOSE
                else "信用新規" if cash_margin == CASH_MARGIN_OPEN else "現物")
        return self._post_order(body, f"{label} ({kind})")

    def send_sell(self, symbol: int | str, qty: int, price: float | None = None,
                  cash_margin: int = CASH_MARGIN_CLOSE,
                  order_type: str = "market",
                  close_positions: list[dict] | None = None,
                  expire_day: int | None = None,
                  omit_close_positions: bool = False) -> dict:
        """普通の売り注文 (現物売り or 信用返済売り)。

        order_type:
          "market" = 成行 / "limit" = 指値 (price 必須) / "moo" = 寄成
        expire_day: 0=当日 / YYYYMMDD=指定日 / None=デフォルト(0)
        omit_close_positions: True にすると ClosePositions を送らず API の自動割当てに任せる
        """
        body = self._base_order(symbol, SIDE_SELL, qty, cash_margin)
        if expire_day is not None:
            body["ExpireDay"] = expire_day
        if order_type == "limit":
            if price is None:
                raise ValueError("order_type='limit' には price が必要です。")
            body["FrontOrderType"] = FOT_LIMIT
            body["Price"] = round(price)
            label = f"指値売り {symbol} x{qty} @{round(price)}"
        elif order_type == "moo":
            body["FrontOrderType"] = FOT_MOO
            body["Price"] = 0
            body["Exchange"] = EXCHANGE_TOKYO_PLUS  # MOO は SOR(9) 非対応
            body["ExpireDay"] = _next_trading_day_int()  # MOO は翌営業日を指定 (0=当日は無効)
            label = f"寄成売り {symbol} x{qty}"
        else:  # market
            body["FrontOrderType"] = FOT_MARKET
            body["Price"] = 0
            label = f"成行売り {symbol} x{qty}"

        # 信用返済の ClosePositions (省略時は API の自動割当て)
        if cash_margin == CASH_MARGIN_CLOSE and not omit_close_positions:
            if close_positions is not None:
                body["ClosePositions"] = close_positions
            elif self.dry_run:
                body["ClosePositions"] = [{"HoldID": "(実行時に自動取得)", "Qty": qty}]
            else:
                # 既存の返済注文を取消して建玉を解放してから発注する
                try:
                    self.cancel_open_close_orders(symbol, SIDE_SELL)
                    cp = self._build_close_positions(symbol, qty, SIDE_SELL)
                except Exception as e:
                    # 建玉/注文取得が 429 等で失敗しても損切りは止めない。
                    # ClosePositions を省略して API の自動割当てに任せる。
                    print(f"  ⚠ {symbol}: 建玉取得に失敗 ({e}) → ClosePositions 省略で発注続行")
                    cp = None
                if cp:
                    body["ClosePositions"] = cp
                elif cp is not None:
                    # 取得は成功したが該当建玉ゼロ → 発注しても無駄
                    print(f"  ⚠ {symbol}: 返済対象の信用建玉が見つかりません。発注をスキップします。")
                    return {"Result": -1, "Message": "建玉なし"}
                # cp is None (取得失敗): ClosePositions を付けず自動割当てに任せる

        kind = "信用返済" if cash_margin == CASH_MARGIN_CLOSE else "現物売"
        return self._post_order(body, f"{label} ({kind})")

    def send_stop_sell(self, symbol: int | str, qty: int, trigger_price: float,
                       cash_margin: int = CASH_GENBUTSU,
                       after_hit_price: float | None = None) -> dict:
        """逆指値売り (trigger_price 以下で発動)。

        after_hit_price=None なら発火後は成行 (損切り/intraday用の既定)。
        値を渡すと発火後は指値 (ショート新規の下限ガード用。-3%超の窓開けは約定しない)。
        """
        body = self._base_order(symbol, SIDE_SELL, qty, cash_margin)
        body["FrontOrderType"] = FOT_STOP
        body["Price"] = 0
        if after_hit_price is None:
            after_type, after_price = AFTERHIT_MARKET, 0
        else:
            after_type, after_price = AFTERHIT_LIMIT, round(after_hit_price)
        body["ReverseLimitOrder"] = {
            "TriggerSec": TRIGGER_AFTER_ORDER,
            "TriggerPrice": round(trigger_price),
            "UnderOver": UNDER,         # 以下 (下落で発動)
            "AfterHitOrderType": after_type,
            "AfterHitPrice": after_price,
        }
        _lbl = "逆指値売り" if after_hit_price is None else "逆指値売り→指値"
        return self._post_order(body, f"{_lbl} {symbol} x{qty} @≤{trigger_price:.0f}")

    def send_moc(self, symbol: int | str, qty: int, side: str = "sell",
                 cash_margin: int = CASH_GENBUTSU,
                 close_positions: list[dict] | None = None,
                 exchange: int | None = None) -> dict:
        """引け成行 (MOC) 決済。close 方式の損切りで使う。

        side            : "sell"=売り決済(ロング) / "buy"=買い戻し(ショート)。
        cash_margin     : CASH_GENBUTSU(1)=現物 / CASH_MARGIN_CLOSE(3)=信用返済。
        close_positions : 信用返済時の建玉 ID リスト [{"HoldID": ..., "Qty": ...}, ...]。
                          None なら API から自動取得 (dry-run では疑似値を入れる)。
        exchange        : 取引所コード。MOC は SOR(9) 非対応なので東証＋(27)を既定にする。
        """
        kabu_side = SIDE_SELL if side == "sell" else SIDE_BUY
        # MOC は SOR(9) 非対応。東証＋(27) を既定とし self.order_exchange を無視する。
        moc_exchange = exchange if exchange is not None else EXCHANGE_TOKYO_PLUS
        body = self._base_order(symbol, kabu_side, qty, cash_margin)
        body["Exchange"] = moc_exchange
        body["FrontOrderType"] = FOT_MOC
        body["Price"] = 0

        if cash_margin == CASH_MARGIN_CLOSE:
            if close_positions is not None:
                body["ClosePositions"] = close_positions
            elif self.dry_run:
                # dry-run では建玉 API を叩けないので仮値を入れる
                body["ClosePositions"] = [{"HoldID": "(実行時に自動取得)", "Qty": qty}]
            else:
                # 既存の返済注文を取消して建玉を解放してから発注する
                self.cancel_open_close_orders(symbol, kabu_side)
                cp = self._build_close_positions(symbol, qty, kabu_side)
                if not cp:
                    print(f"  ⚠ {symbol}: 返済対象の信用建玉が見つかりません。発注をスキップします。")
                    return {"Result": -1, "Message": "建玉なし"}
                body["ClosePositions"] = cp

        return self._post_order(body, f"引け成行 {symbol} x{qty} ({side})")

    def cancel_order(self, order_id: str) -> dict:
        """注文取消。

        取消の失敗 (HTTP エラー / kabu エラーコード) は **例外を送出しない**。
        既に State=4(訂正取消送信中) や終了(State=5) の注文を取消すと kabu が
        エラーを返すが、これは「もう取消/終了している」だけで後続の損切り成行を
        止める理由にはならない。呼び出し側 (cancel_open_close_orders → send_moc)
        が続行できるよう、失敗時は Result!=0 の dict を返してログのみ出す。
        """
        if self.dry_run:
            print(f"  [dry-run] 取消: {order_id}")
            return {"Result": 0, "_dry_run": True}
        url = f"{self.base_url}/kabusapi/cancelorder"
        body = {"OrderId": order_id, "Password": self._password}
        try:
            r = requests.put(url, headers=self._headers(with_content=True),
                             json=body, timeout=self.timeout)
        except Exception as e:
            print(f"  ⚠ 取消 通信失敗 (続行): {order_id}: {e}")
            return {"Result": -1, "_exception": str(e)}
        try:
            res = r.json()
        except Exception:
            res = {"_raw": r.text}
        if not r.ok:
            print(f"  ⚠ 取消 HTTP {r.status_code} (続行, 既に取消/終了済みの可能性): "
                  f"{order_id}: {res}")
            return {"Result": -1, "_http_status": r.status_code, **(
                res if isinstance(res, dict) else {})}
        if isinstance(res, dict) and res.get("Result") not in (0, None):
            print(f"  ⚠ 取消 失敗 (続行): {order_id}: {res}")
        return res if isinstance(res, dict) else {"Result": -1, "_raw": res}


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
