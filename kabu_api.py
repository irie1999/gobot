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
EXCHANGE_DERIV_DAY = 23      # 大阪(日中セッション) ← 先物は **08:45 開始**
EXCHANGE_DERIV_NIGHT = 24    # 大阪(夜間セッション)

SIDE_SELL = "1"              # 売
SIDE_BUY = "2"              # 買

CASH_GENBUTSU = 1            # 現物
CASH_MARGIN_OPEN = 2         # 信用新規
CASH_MARGIN_CLOSE = 3        # 信用返済

# 執行条件 (FrontOrderType)
# 出典: kabuステーションAPI リファレンス v1.5「注文発注（現物・信用）」
#   https://kabucom.github.io/kabusapi/ptal/index.html
#   10=成行 13=寄成(前場) 14=寄成(後場) 15=引成(前場) 16=引成(後場) 17=IOC成行
#   20=指値 21=寄指(前場) 22=寄指(後場) 23=引指(前場) 24=引指(後場)
#   25=不成(前場) 26=不成(後場) 27=IOC指値 / 30=逆指値
#   ※ 成行系は Price=0、指値系は Price=発注したい金額、逆指値は AfterHitPrice で指定。
FOT_MARKET = 10              # 成行
FOT_MOO = 13                 # 寄成 (前場)
FOT_MOC = 16                 # 引成 (後場の引け成行) ← close 損切りで使用
FOT_LIMIT = 20               # 指値 (ザラ場でも約定する通常の指値)
FOT_STOP = 30                # 逆指値 ← 現行 lss のエントリーで使用
# 寄指(前場) = **寄付の板寄せだけで約定し、寄らなければ失効する指値**。
# H案(前日終値±Nティックの指値売り・板寄せのみ)の新規売りで使う。
# 通常の指値(20)で代用してはいけない: そちらはザラ場でも約定してしまい、
# ザラ場到達ぶんは実測で板寄せの1/4〜1/5しか稼がない(2026-08-10 の .\hsweep)。
# 環境変数 KABU_FOT_LIMIT_MOO で上書き可(後場の 22 を試す等)。
FOT_LIMIT_MOO = int(os.environ.get("KABU_FOT_LIMIT_MOO", "21") or 21)

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
                 order_exchange: int = EXCHANGE_SOR,
                 margin_type: int = 1):
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
        # 信用取引区分: 1=制度信用 / 2=一般信用(長期) / 3=一般信用(デイトレ)。
        # 制度信用の空売りは貸借銘柄限定。lss(同日決済ショート)は非貸借銘柄も売れるよう
        # 一般信用デイトレ(3)を使う(kabu_send_lss / order_server の lss / lss_exit_watcher)。
        self.margin_trade_type = margin_type
        self._password = password or self._password_from_env(prod)
        self._token: str | None = None
        self._registered: set[tuple[str, int]] = set()  # /board 用 銘柄登録済み
        # register() の直近の失敗理由。429(レート制限)か 400(不正コード)かを
        # 呼び出し側が区別するために使う(2026-08-24)。
        self.last_register_error = ""

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
        last_exc = None
        for i in range(retries):
            try:
                r = requests.get(url, headers=self._headers(),
                                 params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as _re:
                # ⛔ **タイムアウト/接続断もリトライする**(2026-08-17 追加)。
                #    以前は 429 しか再試行しておらず、read timeout はそのまま
                #    送出していた。実測(41銘柄・並列8)で **24件が read timeout**
                #    になり、その銘柄は丸ごと取れなかった。K は 09:00 の始値を
                #    全候補ぶん取れないと判定できないので、ここで落ちるのは致命的。
                last_exc = _re
                self.n_timeout = getattr(self, "n_timeout", 0) + 1
                wait = 0.5 * (i + 1)
                if not getattr(self, "quiet_429", False):
                    print(f"  ⚠ 通信エラー: {wait:.1f}秒待って再試行 "
                          f"({i+1}/{retries}) {url} — {_re.__class__.__name__}")
                time.sleep(wait)
                continue
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()
            last = r
            # ★ 429 の回数を数える。並列数をいくつにすべきかは
            #   「429 が出ない最大の並列数」で決めるしかないので、
            #   数えられないと測定できない(2026-08-16)。
            self.n_429 = getattr(self, "n_429", 0) + 1
            wait = 1.5 * (i + 1)
            if not getattr(self, "quiet_429", False):
                print(f"  ⚠ 429 レート制限: {wait:.1f}秒待って再試行 "
                      f"({i+1}/{retries}) {url}")
            time.sleep(wait)
        # ここに来たら最後まで 429 か 通信エラー。呼び出し側で扱えるよう送出。
        if last is not None:
            last.raise_for_status()
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"GET 失敗: {url}")

    # ── 情報取得 (GET) ───────────────────────────────────────
    def register(self, symbol: int | str, exchange: int = EXCHANGE_TOSHO) -> bool:
        """/board で時価を取る前に必要な銘柄登録 (PUT /register)。

        kabu API は登録していない銘柄の /board が空になる仕様。
        一度登録した銘柄は self._registered にキャッシュして再登録を避ける。

        ⛔⛔ **成否を返すこと**(2026-08-24 修正)。以前は例外を握り潰して
          print だけしていたので、**呼び出し側は失敗を検知できなかった**。
          log_preopen_board の「429 が3回続いたら打ち切る」ガードが
          そのせいで一度も発火せず、44連続で 429 を叩き続けて
          レート制限を悪化させた(2026-08-24 の朝、09:00 の発注に影響した)。
          失敗の中身は self.last_register_error に入れる(429 の判別用)。

        返り値: True=登録済み / False=失敗(理由は last_register_error)。
        """
        key = (str(symbol), exchange)
        if key in self._registered:
            return True
        url = f"{self.base_url}/kabusapi/register"
        body = {"Symbols": [{"Symbol": str(symbol), "Exchange": exchange}]}
        try:
            r = requests.put(url, headers=self._headers(with_content=True),
                             json=body, timeout=self.timeout)
            r.raise_for_status()
            self._registered.add(key)
            self.last_register_error = ""
            return True
        except Exception as e:
            self.last_register_error = str(e)
            print(f"  ⚠ {symbol}: 銘柄登録(register)失敗 ({e})")
            return False

    def register_many(self, symbols, exchange: int = EXCHANGE_TOSHO) -> dict:
        """複数銘柄を **1回の PUT で** まとめて登録する。

        ⛔ register() は1銘柄ずつ往復するので、41銘柄なら41回のHTTP往復に
           なる。K(09:00の始値確認)は数秒が勝負なので、そこで数秒使うと
           エッジがそのまま削れる。API の Symbols は配列なので1回で済む。

        返り値: API のレスポンス(登録済み一覧)。失敗時は {}。
        ⚠ 登録上限(50銘柄)を超えるぶんは API 側で弾かれる。何件受理されたかは
           返り値の RegistList の長さで確認すること(黙って切られても
           気づけるように、呼び出し側で必ず数えること)。
        """
        _new = [s for s in symbols if (str(s), exchange) not in self._registered]
        if not _new:
            return {}
        url = f"{self.base_url}/kabusapi/register"
        body = {"Symbols": [{"Symbol": str(s), "Exchange": exchange}
                            for s in _new]}
        # ⛔ **/register にもレート制限がある**(2026-08-16 実測)。上限プローブの
        #    直後に登録すると 429 が返り、そこから連鎖して以降の測定が全部
        #    壊れた。登録→全解除を短時間で繰り返す用途(バッチ回し)では必ず出る。
        #    ⚠ 400(上限超過など)は**再試行しても無駄**なので即あきらめる。
        _data = {}
        for _try in range(5):
            try:
                r = requests.put(url, headers=self._headers(with_content=True),
                                 json=body, timeout=self.timeout)
                if r.status_code == 429:
                    _w = 1.0 * (_try + 1)
                    print(f"  ⚠ register レート制限(429): {_w:.1f}秒待って再試行 "
                          f"({_try + 1}/5)")
                    time.sleep(_w)
                    continue
                r.raise_for_status()
                _data = r.json() if r.content else {}
                break
            except Exception as e:
                if "429" in str(e) and _try < 4:
                    time.sleep(1.0 * (_try + 1))
                    continue
                print(f"  ⚠ 銘柄一括登録(register)失敗 ({len(_new)}件): {e}")
                return {}
        else:
            print(f"  ⛔ register が 429 のまま5回失敗しました ({len(_new)}件)")
            return {}
        # 実際に受理されたものだけをキャッシュに入れる(上限で切られる可能性)
        _ok = {str(x.get("Symbol")) for x in (_data.get("RegistList") or [])}
        for s in _new:
            if not _ok or str(s) in _ok:
                self._registered.add((str(s), exchange))
        return _data

    def unregister(self, symbol: int | str, exchange: int = EXCHANGE_TOSHO) -> None:
        """銘柄登録を解除 (PUT /unregister)。登録上限(50銘柄)対策で使う。"""
        key = (str(symbol), exchange)
        url = f"{self.base_url}/kabusapi/unregister"
        body = {"Symbols": [{"Symbol": str(symbol), "Exchange": exchange}]}
        try:
            r = requests.put(url, headers=self._headers(with_content=True),
                             json=body, timeout=self.timeout)
            r.raise_for_status()
        except Exception:
            pass
        finally:
            self._registered.discard(key)

    def unregister_all(self) -> bool:
        """登録銘柄を **全解除** (PUT /unregister/all)。

        登録上限を実測するとき、前の試行の残りが混ざると数が合わない。
        1件ずつ解除すると往復が銘柄数ぶん要るので専用APIを使う。
        成功したら True。
        """
        url = f"{self.base_url}/kabusapi/unregister/all"
        try:
            r = requests.put(url, headers=self._headers(with_content=True),
                             timeout=self.timeout)
            r.raise_for_status()
        except Exception as e:
            print(f"  ⚠ 全解除(unregister/all)失敗: {e}")
            return False
        finally:
            self._registered.clear()
        return True

    def get_board(self, symbol: int | str, exchange: int = EXCHANGE_TOSHO) -> dict:
        """時価情報 (CurrentPrice 等) を取得。事前に銘柄登録を行う。"""
        self.register(symbol, exchange)
        url = f"{self.base_url}/kabusapi/board/{symbol}@{exchange}"
        return self._get_json(url)

    def resolve_future(self, deriv: str = "NK225mini", month: int = 0) -> str:
        """日経225先物の銘柄コードを解決する(/symbolname/future)。

        deriv  … "NK225" / "NK225mini" / "NK225micro" など
        month  … 0=直近限月(kabu の DerivMonth=0 が『中心限月』)
        取れなければ "" を返す。**照会のみ。発注しない。**
        """
        url = f"{self.base_url}/kabusapi/symbolname/future"
        try:
            j = self._get_json(url, params={"FutureCode": deriv,
                                            "DerivMonth": month})
            return str((j or {}).get("Symbol") or "")
        except Exception:
            return ""

    def get_symbol(self, symbol: int | str, exchange: int = EXCHANGE_TOSHO) -> dict:
        """銘柄マスタ照会 (/symbol)。信用売建可否(MarginSell)・信用買建可否(MarginBuy)・
        貸借区分などを含む。空売り可否の判定に使う(読み取りのみ・発注しない)。

        kabu は未登録銘柄の /symbol に 400 を返すため、事前に銘柄登録する。
        新規に登録した銘柄は照会後に解除し、登録上限(50銘柄)を埋めないようにする。"""
        key = (str(symbol), exchange)
        newly = key not in self._registered
        self.register(symbol, exchange)
        try:
            url = f"{self.base_url}/kabusapi/symbol/{symbol}@{exchange}"
            return self._get_json(url)
        finally:
            if newly:
                self.unregister(symbol, exchange)

    def is_margin_sellable(self, symbol: int | str,
                           exchange: int = EXCHANGE_TOSHO) -> bool | None:
        """信用売建(空売り)が可能か。/symbol の MarginSell を見る。
        取得失敗/フラグ不明時は None(判定不能)を返す。"""
        try:
            info = self.get_symbol(symbol, exchange)
        except Exception:
            return None
        v = info.get("MarginSell")
        return bool(v) if v is not None else None

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

    def get_ranking(self, ranking_type: str = "1",
                    exchange_division: str = "ALL") -> list[dict]:
        """ランキング取得 (GET /ranking)。読み取り専用・発注には無関係。

        ザラ場中は「今この瞬間」のランキング、引け後は当日引けのランキングを返す。
        過去日の“ザラ場”ランキングは kabu 側に保存が無いため取得できない。

        ranking_type (Type):
          "1"=値上がり率 "2"=値下がり率 "3"=売買高上位 "4"=売買代金上位
          "5"=TICK回数 "6"=急上昇 "7"=急降下  (他は kabu ドキュメント参照)
        exchange_division:
          "ALL"=全市場 / "T"=東証全体 / "TP"=プライム / "TS"=スタンダード
          / "TG"=グロース / "M"=名証 など

        返り値: Ranking 配列 (通常 最大30件)。各要素は No/Symbol/SymbolName/
        CurrentPrice/ChangeRatio(騰落率) 等を含む。
        """
        url = f"{self.base_url}/kabusapi/ranking"
        data = self._get_json(url, params={"Type": ranking_type,
                                           "ExchangeDivision": exchange_division})
        if isinstance(data, dict):
            return data.get("Ranking", []) or []
        return []

    # ── 発注 (POST) ──────────────────────────────────────────
    def _post_order(self, body: dict, label: str, quiet: bool = False) -> dict:
        """sendorder の共通処理。dry_run なら送信せず内容を表示。
        quiet=True のとき失敗時のエラー表示を抑制する(利確補完など、既に注文が
        あって毎回同じエラーで失敗する定期発注のログ氾濫を防ぐ用)。"""
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
            if not quiet:
                print(f"  ✗ {label} HTTP {r.status_code}: {res}")
                body_display = dict(body, Password="***")
                print(f"    送信ボディ: {body_display}")
            return {"Result": -1, "_http_status": r.status_code, **(
                res if isinstance(res, dict) else {})}
        if res.get("Result") != 0:
            if not quiet:
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
            # 制度信用(1)は空売りが貸借銘柄限定。非貸借銘柄も売る lss は一般信用
            # デイトレ(3)を self.margin_trade_type で指定する。
            body["MarginTradeType"] = self.margin_trade_type
            if cash_margin == CASH_MARGIN_OPEN:
                body["DelivType"] = 0     # 信用新規: 指定なし
                body["FundType"] = "11"   # 信用新規のみ FundType 必要
            else:
                body["DelivType"] = 2     # 信用返済: お預り金 (公式サンプル準拠)
        return body

    def send_stop_buy(self, symbol: int | str, qty: int, trigger_price: float,
                      cash_margin: int = CASH_GENBUTSU,
                      after_hit_price: float | None = None,
                      close_positions: list[dict] | None = None,
                      omit_close_positions: bool = False) -> dict:
        """逆指値買い (trigger_price 以上で発火)。新規エントリー or 信用返済(買戻し)。

        逆指値シグナルの order_price をそのまま trigger_price に渡す。
        after_hit_price=None なら発火後は成行 (実運用の既定)。
        値を渡すと発火後は指値 (時間外テストなど成行が弾かれる場面で使う)。

        cash_margin=CASH_MARGIN_CLOSE(3) にすると『ショートの損切(買戻し)を上に
        置きっぱなしにする逆指値買い返済』になる(lss の日中OCOの損切側で使う)。
        close_positions を渡すとその建玉(HoldID)を返済対象にする。
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
            "UnderOver": OVER,          # 以上 (ブレイク方向 / ショート損切=上抜けで発火)
            "AfterHitOrderType": after_type,
            "AfterHitPrice": after_price,
        }
        # 信用返済(ショートの損切=買戻し)の ClosePositions
        if cash_margin == CASH_MARGIN_CLOSE and not omit_close_positions:
            if close_positions is not None:
                body["ClosePositions"] = close_positions
            elif self.dry_run:
                body["ClosePositions"] = [{"HoldID": "(実行時に自動取得)", "Qty": qty}]
            else:
                cp = self._build_close_positions(symbol, qty, SIDE_BUY)
                if not cp:
                    print(f"  ⚠ {symbol}: 返済対象の売建玉が見つかりません。逆指値買戻しをスキップ。")
                    return {"Result": -1, "Message": "建玉なし"}
                body["ClosePositions"] = cp
        _kind = ("信用返済(逆指値買戻)" if cash_margin == CASH_MARGIN_CLOSE
                 else "信用新規" if cash_margin == CASH_MARGIN_OPEN else "現物")
        return self._post_order(body, f"逆指値買い {symbol} x{qty} @≥{trigger_price:.0f} ({_kind})")

    def send_buy(self, symbol: int | str, qty: int, price: float | None = None,
                 cash_margin: int = CASH_MARGIN_OPEN,
                 order_type: str = "market",
                 close_positions: list[dict] | None = None,
                 expire_day: int | None = None,
                 omit_close_positions: bool = False,
                 quiet: bool = False) -> dict:
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
        return self._post_order(body, f"{label} ({kind})", quiet=quiet)

    def send_sell(self, symbol: int | str, qty: int, price: float | None = None,
                  cash_margin: int = CASH_MARGIN_CLOSE,
                  order_type: str = "market",
                  close_positions: list[dict] | None = None,
                  expire_day: int | None = None,
                  omit_close_positions: bool = False,
                  quiet: bool = False) -> dict:
        """普通の売り注文 (現物売り or 信用返済売り)。

        order_type:
          "market" = 成行 / "limit" = 指値 (price 必須) / "moo" = 寄成
          "limit_moo" = 寄指 (寄付のみ有効な指値。price 必須) ← H案の新規売り
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
        elif order_type == "limit_moo":
            # 寄指 = 寄付の板寄せだけで約定し、寄らなければ失効する指値。
            # H案は「前日終値(±Nティック)で売れたら建てる、寄らなければ建てない」
            # なので、ザラ場到達で約定する通常の指値(FOT_LIMIT)では**別物**になる。
            # ザラ場到達ぶんは実測で板寄せの1/4〜1/5しか稼がず、条件によっては
            # マイナス(2026-08-10 の hsweep)。必ずこちらを使うこと。
            if price is None:
                raise ValueError("order_type='limit_moo' には price が必要です。")
            if not FOT_LIMIT_MOO:
                raise RuntimeError(
                    "寄指の FrontOrderType が未設定です(FOT_LIMIT_MOO=0)。\n"
                    "  仕様では 21=寄指(前場)。環境変数 KABU_FOT_LIMIT_MOO で"
                    "上書きされていないか確認してください。")
            body["FrontOrderType"] = FOT_LIMIT_MOO
            body["Price"] = round(price)
            # Exchange: 東証+(27)。寄指は**東証の寄付の板寄せ**に乗せる注文で、
            #   SOR(9) だと複数市場に回りうる = 板寄せという前提が崩れる。
            #   既存の寄成(MOO)も SOR では通らず 27 を使っている。
            body["Exchange"] = EXCHANGE_TOKYO_PLUS
            # ⛔ ExpireDay に _next_trading_day_int() を使ってはいけない。
            #    あれは時刻を見ずに「今日+1営業日」を返すので、**朝に発注すると
            #    翌日の寄り**になり当日に間に合わない(lss は朝8:45発注)。
            #    仕様では 0 が「引けまでの間=当日 / 引け後=翌取引所営業日」を
            #    自動判定する(API リファレンス v1.5)。寄り前に出す H では 0 が正しい。
            #    ⚠ ザラ場中に出すとその日の寄りは既に終わっているので、当日扱いだと
            #      約定機会が無い。発注は必ず寄り前(または引け後)に行うこと。
            #    呼び出し側が expire_day を明示した場合はそれを尊重する。
            if expire_day is None:
                body["ExpireDay"] = 0
            label = f"寄指売り {symbol} x{qty} @{round(price)}"
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

        # 信用新規(2)が「現物売」と表示されていた(表示のみのバグ)。lss/H は信用新規売り
        # なので、発注ログでどちらか読み違えないよう区別する。
        kind = ("信用返済" if cash_margin == CASH_MARGIN_CLOSE else
                "信用新規売" if cash_margin == CASH_MARGIN_OPEN else "現物売")
        return self._post_order(body, f"{label} ({kind})", quiet=quiet)

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

    def cancel_order(self, order_id: str, retries: int = 5) -> dict:
        """注文取消。

        429 (API実行回数制限) は **一過性** なので指数バックオフで再試行する。
        429 をリトライせず失敗を返すと、呼び出し側 (cancel_open_sell_orders 等)
        が「取消失敗 → 損切り発注を中断」してしまい、建玉が無防備になる事故に
        つながる (2026-07 DIC 4631 で実際に発生)。_get_json と同じ方針で待って再試行。

        429 以外の失敗 (HTTP エラー / kabu エラーコード) は **例外を送出しない**。
        既に State=4(訂正取消送信中) や終了(State=5) の注文を取消すと kabu が
        エラーを返すが、これは「もう取消/終了している」だけで後続の損切り成行を
        止める理由にはならない。呼び出し側が続行できるよう Result!=0 の dict を返す。
        """
        if self.dry_run:
            print(f"  [dry-run] 取消: {order_id}")
            return {"Result": 0, "_dry_run": True}
        url = f"{self.base_url}/kabusapi/cancelorder"
        body = {"OrderId": order_id, "Password": self._password}
        last_429 = None
        for i in range(retries):
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
            if r.status_code == 429:
                # レート制限は一過性 → 待って再試行 (取消は必ず通したい)
                last_429 = {"Result": -1, "_http_status": 429,
                            **(res if isinstance(res, dict) else {})}
                if i < retries - 1:
                    wait = 1.5 * (i + 1)
                    print(f"  ⚠ 取消 429 レート制限: {wait:.1f}秒待って再試行 "
                          f"({i+1}/{retries}) {order_id}")
                    time.sleep(wait)
                    continue
                print(f"  ✗ 取消 429 が {retries}回続き取消できませんでした: {order_id}")
                return last_429
            if not r.ok:
                print(f"  ⚠ 取消 HTTP {r.status_code} (続行, 既に取消/終了済みの可能性): "
                      f"{order_id}: {res}")
                return {"Result": -1, "_http_status": r.status_code, **(
                    res if isinstance(res, dict) else {})}
            if isinstance(res, dict) and res.get("Result") not in (0, None):
                print(f"  ⚠ 取消 失敗 (続行): {order_id}: {res}")
            return res if isinstance(res, dict) else {"Result": -1, "_raw": res}
        return last_429 or {"Result": -1, "_http_status": 429}


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
