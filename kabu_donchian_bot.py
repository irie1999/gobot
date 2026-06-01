"""
kabu_donchian_bot.py  ―  Donchian (ultra_freq_v2) 自動売買ボット
==================================================================
複数銘柄を同時監視し、過去8本(40分)の高値ブレイク+陽線でエントリー。
最大同時保有数を制限してリスク管理を自動化。

【動作】
  9:00-9:40   ウォームアップ (8本=40分のバー蓄積)
  9:40-14:30  Donchianブレイク監視 → 検出順にエントリー
              (同時保有 MAX_CONCURRENT_POSITIONS まで)
  保有中     損切り / 目標 / 複層トレーリング監視
  14:55      強制決済
  15:00      日次レポート

【パラメータ】(daytrade_donchian.py の ultra_freq_v2 と同期)
  DON_PERIOD=8  TARGET_R=2.0  GAP_MAX_PCT=4.0
  STOP_MAX_PCT=2.0  ENTRY_CUTOFF=14:30
  TRAIL_STEPS=[(0.50, 0.2), (0.80, 0.5)]
  BUDGET=600,000  MAX_RISK=3,000  MAX_CONCURRENT=5

【監視銘柄】
  daytrade_donchian_winners.py から自動読み込み
  (--extract-winners で生成した勝ち銘柄リスト)

【使い方】
  python kabu_donchian_bot.py --dry-run         # ドライラン (注文しない)
  python kabu_donchian_bot.py                   # 本番 (実発注)
  python kabu_donchian_bot.py --max-concurrent 3 # 同時保有3まで

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


# ── .env 読み込み ────────────────────────────────────────────
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


# セクター分散モジュール (任意)
try:
    from sector_map import can_add_sector
    SECTOR_FILTER_AVAILABLE = True
except ImportError:
    SECTOR_FILTER_AVAILABLE = False


# ── Donchian (ultra_freq_v2) パラメータ ──────────────────────
DON_PERIOD       = 8
TARGET_R         = 2.0
GAP_MAX_PCT      = 4.0
STOP_MAX_PCT     = 2.0
TRAIL_STEPS      = [(0.50, 0.2), (0.80, 0.5)]
FORCE_CLOSE_TIME = dtime(14, 50)  # 14:55の引け強制決済前に余裕を持って能動決済
ENTRY_CUTOFF     = dtime(14, 30)
ENTRY_START      = dtime(9, 30)   # この時刻以降エントリー可 (寄付直後の混乱回避)
WARMUP_BARS      = 8
BUDGET           = 600_000
MAX_RISK         = 3_000
MAX_CONCURRENT_POSITIONS = 5

# 連敗時のサイズ縮小
LOSING_STREAK_HALVE     = 3   # 3連敗で qty 半減
LOSING_STREAK_PAUSE     = 5   # 5連敗で当日終了

# 市況フィルタ (日経225のMA20)
NIKKEI_SYMBOL    = "1321"     # 日経225連動型ETF (NEXT FUNDS)
NIKKEI_MA_PERIOD = 20         # MA20 で判定 (5分足ベースで100分平均)

STOP_LABELS = ("損切り", "+0.2Rロック", "+0.5Rロック")


# ── 監視銘柄 (winners リストから読み込み) ──────────────────
def _load_watch_symbols():
    try:
        from daytrade_donchian_winners import SYMBOLS as DON_SYMBOLS
        return [(s.replace(".T", ""), n) for s, n in DON_SYMBOLS]
    except ImportError:
        log.warning("daytrade_donchian_winners.py が見つかりません。")
        log.warning("先に勝ち銘柄抽出を実行してください:")
        log.warning("  python daytrade_donchian_compare.py --universe n225 "
                    "--extract-winners --target-preset ultra_freq")
        return []


def _load_demo_symbols(max_count=40):
    """デモモード用: 日経225全銘柄から先頭N銘柄を読み込み (動作確認用)。"""
    try:
        from symbols_all import SYMBOLS as N225_SYMBOLS
        symbols = [(s.replace(".T", ""), n) for s, n in N225_SYMBOLS[:max_count]]
        log.warning("DEMO MODE: 日経225先頭%d銘柄を監視対象に設定", len(symbols))
        return symbols
    except ImportError:
        log.warning("symbols_all.py が見つかりません。winnersを使用")
        return _load_watch_symbols()


WATCH_SYMBOLS = _load_watch_symbols()


# ─────────────────────────────────────────────────────────────
# kabuステーション API (kabu_daytrade_bot.py から流用)
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
        """銘柄登録 (1銘柄ずつ試行、検証環境で取扱外の銘柄はスキップ)。

        APIは累積登録銘柄リストを返すため、対象銘柄が
        RegistList に含まれているかをチェック。
        APIレート制限回避のため各リクエスト間に小さなsleep。

        Returns:
            (registered, failed): 登録成功/失敗の銘柄コードリスト
        """
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
                    # APIは累積で全登録済み銘柄を返すので、対象が含まれているか確認
                    if any(item.get("Symbol") == s for item in items):
                        registered.append(s)
                    else:
                        failed.append(s)
                else:
                    failed.append(s)
            except Exception:
                failed.append(s)
            time.sleep(0.2)  # API レート制限回避 (1秒に5リクエストまで)
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
        """買い注文。margin=True で信用新規買い (デイトレ信用)。"""
        if margin:
            return self._margin_order(symbol, "2", qty, exchange, open_pos=True)
        return self._order(symbol, "2", qty, exchange)

    def sell(self, symbol, qty, exchange=1, margin=False):
        """売り注文。margin=True で信用返済売り (デイトレ信用)。"""
        if margin:
            return self._margin_order(symbol, "1", qty, exchange, open_pos=False)
        return self._order(symbol, "1", qty, exchange)

    def _order(self, symbol, side, qty, exchange=1):
        """現物注文。"""
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
        """信用注文 (デイトレ信用 = 一般信用短期)。

        open_pos=True : 新規建て (CashMargin=2)
        open_pos=False: 返済    (CashMargin=3, 古い順から)
        """
        body = {
            "Password": self.password,
            "Symbol": symbol,
            "Exchange": exchange,
            "SecurityType": 1,
            "Side": side,
            "CashMargin": 2 if open_pos else 3,
            "MarginTradeType": 3,    # 一般信用(短期=デイトレ信用)
            "DelivType": 0 if open_pos else 2,
            "AccountType": 4,
            "Qty": qty,
            "FrontOrderType": 2,     # 成行
            "Price": 0,
            "ExpireDay": 0,
        }
        if not open_pos:
            # 返済時: 古い順から決済 (1ポジ運用なら同じ建玉のみ)
            body["ClosePositionOrder"] = 0
        r = requests.post(f"{self.base_url}/kabusapi/sendorder",
                          headers=self._h(), json=body, timeout=10)
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────────────────────
# Donchian トラッカー
# ─────────────────────────────────────────────────────────────

class DonchianTracker:
    """1銘柄分の5分足ローリングウィンドウとブレイク判定。"""

    def __init__(self, symbol: str, name: str):
        self.symbol = symbol
        self.name = name
        self.prev_close: float | None = None
        self.gap_skip = False
        self._gap_checked = False
        # 確定済み5分足: list of dict(time, open, high, low, close)
        self.bars: list[dict] = []
        # 進行中バー
        self._cur_bar_start: datetime | None = None
        self._cur_open: float | None = None
        self._cur_high: float = 0.0
        self._cur_low: float = 0.0
        self._cur_close: float = 0.0
        # 直近確定バーをシグナル評価済みかどうか (重複発火防止)
        self._last_signaled_bar: datetime | None = None

    def update(self, price: float, now: datetime):
        """価格を入力し、5分足を更新。"""
        bar_time = now.replace(minute=(now.minute // 5) * 5,
                               second=0, microsecond=0)
        if self._cur_bar_start is None:
            self._cur_bar_start = bar_time
            self._cur_open = price
            self._cur_high = price
            self._cur_low = price
            self._cur_close = price
        elif bar_time > self._cur_bar_start:
            # 前バーを確定して履歴に追加
            self.bars.append({
                "time": self._cur_bar_start,
                "open": self._cur_open,
                "high": self._cur_high,
                "low": self._cur_low,
                "close": self._cur_close,
            })
            # 新バーを開始
            self._cur_bar_start = bar_time
            self._cur_open = price
            self._cur_high = price
            self._cur_low = price
            self._cur_close = price
        else:
            self._cur_high = max(self._cur_high, price)
            self._cur_low = min(self._cur_low, price)
            self._cur_close = price

    def check_gap(self, price: float):
        """初回価格取得時にギャップ確認。"""
        if self._gap_checked:
            return
        self._gap_checked = True
        if self.prev_close and self.prev_close > 0:
            gap = abs(price - self.prev_close) / self.prev_close * 100
            if gap > GAP_MAX_PCT:
                self.gap_skip = True
                log.info("  %s %s: ギャップ %.1f%% → 見送り",
                         self.symbol, self.name, gap)

    def check_signal(self) -> dict | None:
        """直近確定バーでブレイクシグナル判定。"""
        if self.gap_skip or len(self.bars) < WARMUP_BARS + 1:
            return None
        last = self.bars[-1]

        # 重複発火防止
        if self._last_signaled_bar == last["time"]:
            return None

        # 時刻チェック
        if last["time"].time() >= ENTRY_CUTOFF:
            return None

        # 過去 DON_PERIOD 本 (last bar 除く)
        history = self.bars[-(DON_PERIOD + 1):-1]
        if len(history) < DON_PERIOD:
            return None
        don_hi = max(b["high"] for b in history)
        don_lo = min(b["low"] for b in history)

        # ブレイク条件: close > don_hi & 陽線
        if last["close"] > don_hi and last["close"] > last["open"]:
            self._last_signaled_bar = last["time"]
            entry_p = last["close"]  # 即時成行→近似でlast close を採用
            stop_floor = entry_p * (1 - STOP_MAX_PCT / 100)
            stop = max(don_lo, stop_floor)
            if entry_p <= stop:
                return None
            target = entry_p + (entry_p - stop) * TARGET_R
            return {"entry_p": entry_p, "stop": stop, "target": target,
                    "don_lo": don_lo, "don_hi": don_hi,
                    "bar_time": last["time"]}
        return None


# ─────────────────────────────────────────────────────────────
# ポジション (複層トレーリング)
# ─────────────────────────────────────────────────────────────

class Position:
    def __init__(self, symbol, name, entry_p, qty, stop, target):
        self.symbol = symbol
        self.name = name
        self.entry_p = entry_p
        self.qty = qty
        self.orig_stop = stop
        self.stop = stop
        self.target = target
        self.trail_level = 0
        self.entry_time = datetime.now(JST)

    def check_exit(self, price):
        """価格更新で決済要否を判定。Noneなら継続、文字列なら決済理由。"""
        if price <= self.stop:
            return STOP_LABELS[min(self.trail_level, len(STOP_LABELS) - 1)]
        if price >= self.target:
            return "目標達成"
        # 複層トレーリング
        if self.target > self.entry_p:
            risk = self.entry_p - self.orig_stop
            progress = (price - self.entry_p) / (self.target - self.entry_p)
            for idx, (trigger, lock_r) in enumerate(TRAIL_STEPS):
                if progress >= trigger and self.trail_level <= idx:
                    new_stop = self.entry_p + risk * lock_r
                    if new_stop > self.stop:
                        self.stop = new_stop
                        self.trail_level = idx + 1
                        labels = ("(初期)", "建値", "+0.3Rロック")
                        log.info("  → トレーリング更新 [%s] stop=%.0f",
                                 labels[min(self.trail_level, 2)], self.stop)
        return None


# ─────────────────────────────────────────────────────────────
# サイジング
# ─────────────────────────────────────────────────────────────

def calc_qty(entry_p, stop_p, budget, max_risk, size_factor=1.0):
    """サイズ計算。size_factor=0.5 でハーフサイズ(連敗時用)。"""
    risk_per_share = abs(entry_p - stop_p)
    if risk_per_share <= 0:
        return 100
    effective_risk = max_risk * size_factor
    qty_by_risk = int(effective_risk / risk_per_share / 100) * 100
    qty_by_budget = int(budget / entry_p / 100) * 100
    return max(100, min(qty_by_risk, qty_by_budget))


# ─────────────────────────────────────────────────────────────
# 市況フィルタ (日経MA20)
# ─────────────────────────────────────────────────────────────

class MarketRegimeFilter:
    """日経225ETF(1321)のMA20で相場環境を判定。
    現在価格 < MA20 ならエントリー停止 (下落トレンド回避)。"""

    def __init__(self, client, symbol=NIKKEI_SYMBOL, ma_period=NIKKEI_MA_PERIOD):
        self.client = client
        self.symbol = symbol
        self.ma_period = ma_period
        self.prices: list[float] = []   # 5分足のclose
        self._cur_bar_start: datetime | None = None
        self._cur_close: float | None = None
        self._registered = False
        self._last_status: bool | None = None  # True=取引可, False=取引停止

    def setup(self):
        """日経ETFを registerして、初期化。"""
        try:
            body = {"Symbols": [{"Symbol": self.symbol, "Exchange": 1}]}
            r = requests.put(f"{self.client.base_url}/kabusapi/register",
                             headers=self.client._h(), json=body, timeout=10)
            if r.status_code == 200:
                self._registered = True
                log.info("市況フィルタ用ETF (%s) を登録", self.symbol)
        except Exception as e:
            log.warning("市況フィルタETF登録失敗: %s (フィルタ無効化)", e)

    def update(self, now: datetime):
        """価格を取得して 5分足 を更新。"""
        if not self._registered:
            return
        try:
            price, _ = self.client.get_price(self.symbol)
            if price is None:
                return
        except Exception:
            return

        bar_time = now.replace(minute=(now.minute // 5) * 5,
                               second=0, microsecond=0)
        if self._cur_bar_start is None or bar_time > self._cur_bar_start:
            if self._cur_close is not None:
                self.prices.append(self._cur_close)
                if len(self.prices) > self.ma_period * 2:
                    self.prices = self.prices[-self.ma_period * 2:]
            self._cur_bar_start = bar_time
            self._cur_close = price
        else:
            self._cur_close = price

    def is_bullish(self) -> bool:
        """現在価格 >= MA20 なら True (取引可)。データ不足時は True (フィルタ無効)。"""
        if not self._registered:
            return True
        if len(self.prices) < self.ma_period:
            return True   # データ不足時はフィルタ無効
        if self._cur_close is None:
            return True
        ma = sum(self.prices[-self.ma_period:]) / self.ma_period
        bullish = self._cur_close >= ma
        if self._last_status != bullish:
            self._last_status = bullish
            status = "✓ 取引可" if bullish else "✗ 取引停止 (下落トレンド)"
            log.info("市況フィルタ: 日経 %.1f / MA20 %.1f → %s",
                     self._cur_close, ma, status)
        return bullish


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
        log.warning("登録失敗 (検証環境で取扱外の可能性): %d銘柄 → %s",
                    len(failed), failed[:10] if len(failed) > 10 else failed)
    # 登録成功した銘柄のみ監視対象に
    active_symbols = [(s, n) for s, n in WATCH_SYMBOLS if s in registered]
    if not active_symbols:
        log.error("登録できた銘柄が0件 → 終了")
        return

    dry = args.dry_run
    poll = args.poll
    budget = args.budget
    max_risk = args.max_risk
    max_concurrent = args.max_concurrent
    margin = args.margin

    trackers = {s: DonchianTracker(s, n) for s, n in active_symbols}
    positions: dict[str, Position] = {}
    trades: list[dict] = []
    losing_streak = 0   # 連敗カウンタ (負け→+1, 勝ち→0にreset)
    paused_today = False  # 連敗 LOSING_STREAK_PAUSE 達成で当日停止

    # 市況フィルタ初期化 (--no-market-filter で無効化可)
    market_filter = None
    if not args.no_market_filter:
        market_filter = MarketRegimeFilter(client)
        market_filter.setup()

    # 前日終値を取得
    for sym, tracker in trackers.items():
        try:
            _, prev = client.get_price(sym)
            tracker.prev_close = prev
        except Exception:
            pass

    mode = "DRY RUN" if dry else "本番"
    trade_type = "デイトレ信用" if margin else "現物"
    log.info("=" * 60)
    log.info("  Donchian (ultra_freq) %d銘柄監視ボット [%s / %s]",
             len(active_symbols), mode, trade_type)
    log.info("  予算: %s円 / リスク: %s円/取引 / 最大同時保有: %d",
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

        # 市況フィルタ用の日経価格更新
        if market_filter is not None:
            market_filter.update(now)

        # 全銘柄の価格更新
        for sym, tracker in trackers.items():
            try:
                price, _ = client.get_price(sym)
                if price is None:
                    continue
                tracker.check_gap(price)
                tracker.update(price, now)
            except Exception as e:
                log.debug("  %s 価格取得失敗: %s", sym, e)

        # ── 保有ポジションの決済チェック ──────────────
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
                log.info(f"★ 決済 [{reason}] {pos.symbol} {pos.name}  "
                         f"{pos.entry_p:.0f}→{price:.0f}  qty={pos.qty}  "
                         f"{pnl:+,.0f}円")
                if not dry:
                    try:
                        res = client.sell(pos.symbol, pos.qty, margin=margin)
                        log.info("  注文: %s", res)
                    except Exception as e:
                        log.error("  注文失敗: %s", e)
                trades.append({
                    "symbol": pos.symbol, "name": pos.name,
                    "entry": pos.entry_p, "exit": price,
                    "qty": pos.qty, "pnl": pnl, "reason": reason,
                    "entry_time": pos.entry_time.strftime("%H:%M:%S"),
                    "exit_time": now.strftime("%H:%M:%S"),
                })
                to_close.append(sym)

                # 連敗カウンタ更新
                if pnl > 0:
                    if losing_streak > 0:
                        log.info("  → 連敗リセット (%d→0)", losing_streak)
                    losing_streak = 0
                else:
                    losing_streak += 1
                    if losing_streak >= LOSING_STREAK_PAUSE:
                        paused_today = True
                        log.warning("  ★ %d連敗達成 → 当日エントリー停止",
                                    losing_streak)
                    elif losing_streak >= LOSING_STREAK_HALVE:
                        log.info("  → 連敗 %d 件、次回ハーフサイズ",
                                 losing_streak)
        for sym in to_close:
            del positions[sym]

        # ── 新規シグナルチェック (空きスロットがあれば) ─────
        # フィルタ条件:
        #   - 同時保有数余裕 < max_concurrent
        #   - 時刻 ENTRY_START 〜 ENTRY_CUTOFF
        #   - 連敗5以上で当日停止 (paused_today)
        #   - 市況フィルタ通過 (日経MA20上)
        market_ok = market_filter.is_bullish() if market_filter else True
        if (len(positions) < max_concurrent
                and ENTRY_START <= t < ENTRY_CUTOFF
                and not paused_today
                and market_ok):
            # 連敗時のサイズ縮小判定
            size_factor = 0.5 if losing_streak >= LOSING_STREAK_HALVE else 1.0

            for sym, tracker in trackers.items():
                if sym in positions:
                    continue
                if len(positions) >= max_concurrent:
                    break
                # セクター分散制限 (--no-sector-filter で無効化可)
                if (SECTOR_FILTER_AVAILABLE and not args.no_sector_filter
                        and not can_add_sector(sym, list(positions.keys()),
                                                max_per_sector=1)):
                    log.debug("  %s: 同一セクター既保有 → スキップ", sym)
                    continue
                sig = tracker.check_signal()
                if sig:
                    qty = calc_qty(sig["entry_p"], sig["stop"],
                                   budget, max_risk, size_factor=size_factor)
                    size_note = " [連敗中ハーフサイズ]" if size_factor < 1.0 else ""
                    log.info("★ Donchianブレイク! %s %s  価格=%.0f "
                             "8本高値=%.0f 損切=%.0f 目標=%.0f 株数=%d%s",
                             sym, tracker.name, sig["entry_p"],
                             sig["don_hi"], sig["stop"], sig["target"], qty,
                             size_note)
                    if not dry:
                        try:
                            res = client.buy(sym, qty, margin=margin)
                            log.info("  注文: %s", res)
                        except Exception as e:
                            log.error("  注文失敗: %s", e)
                            continue
                    pos = Position(sym, tracker.name, sig["entry_p"], qty,
                                   sig["stop"], sig["target"])
                    positions[sym] = pos
                    log.info("  → 保有中: %d/%d", len(positions), max_concurrent)

        time.sleep(poll)

    # ── 日次レポート ───────────────────────────────
    print()
    print("=" * 65)
    print(f"  Donchian (ultra_freq) {len(active_symbols)}銘柄監視 日次レポート [{mode}]")
    print(f"  {now.strftime('%Y-%m-%d')}")
    print("=" * 65)

    if trades:
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total = sum(t["pnl"] for t in trades)
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        gp = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in losses))
        pf = gp / gl if gl > 0 else float("inf")

        print(f"\n  【サマリ】")
        print(f"    取引数: {len(trades)}  勝率: {win_rate:.1f}%  "
              f"PF: {pf:.2f}  合計損益: {total:+,.0f}円")

        print(f"\n  【トレード明細】")
        for tr in trades:
            m = "○" if tr["pnl"] > 0 else "●"
            print(f"    {m} {tr['name']} ({tr['symbol']})  "
                  f"{tr['entry_time']}→{tr['exit_time']}  "
                  f"{tr['entry']:,.0f}→{tr['exit']:,.0f}  "
                  f"qty={tr['qty']:,}  "
                  f"{tr['pnl']:+,.0f}円  ({tr['reason']})")
    else:
        print("\n  トレードなし")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(
        description="Donchian (ultra_freq) 自動売買ボット")
    parser.add_argument("--budget", type=int, default=BUDGET)
    parser.add_argument("--max-risk", type=int, default=MAX_RISK)
    parser.add_argument("--max-concurrent", type=int,
                        default=MAX_CONCURRENT_POSITIONS,
                        help="同時保有銘柄数の上限 (デフォルト: 5)")
    parser.add_argument("--poll", type=int, default=10, help="取得間隔(秒)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ドライラン (注文を発注しない)")
    parser.add_argument("--margin", action="store_true",
                        help="信用取引(デイトレ信用 = 一般信用短期)で発注。"
                             "未指定なら現物取引")
    parser.add_argument("--demo", action="store_true",
                        help="デモモード: ブレイク条件を大幅に緩和し、"
                             "監視銘柄も日経225先頭から拡大。"
                             "すぐシグナルが出る代わりに精度は落ちる(検証用)")
    parser.add_argument("--demo-count", type=int, default=40,
                        help="デモモード時の監視銘柄数 (日経225先頭からN銘柄, デフォルト40)")
    parser.add_argument("--no-market-filter", action="store_true",
                        help="市況フィルタ(日経MA20)を無効化")
    parser.add_argument("--no-sector-filter", action="store_true",
                        help="セクター分散制限(同一セクター上限1)を無効化")
    parser.add_argument("--entry-start", default="09:30",
                        help="エントリー開始時刻 HH:MM (デフォルト 09:30)")
    args = parser.parse_args()

    # entry-start CLIフラグを反映
    try:
        h, m = args.entry_start.split(":")
        global ENTRY_START
        ENTRY_START = dtime(int(h), int(m))
    except Exception:
        log.warning("--entry-start の形式不正 (HH:MM必須)、デフォルト09:30を使用")

    if args.demo:
        global DON_PERIOD, WARMUP_BARS, ENTRY_CUTOFF, TARGET_R, GAP_MAX_PCT, STOP_MAX_PCT
        global WATCH_SYMBOLS
        DON_PERIOD = 3       # 3本(15分)高値ブレイク
        WARMUP_BARS = 3      # 9:15以降エントリー可
        ENTRY_CUTOFF = dtime(14, 50)
        TARGET_R = 1.0       # R:R 1:1 で素早く決済
        GAP_MAX_PCT = 6.0
        STOP_MAX_PCT = 1.5
        # 監視銘柄も拡大: winners 21銘柄 → 日経225先頭40銘柄
        WATCH_SYMBOLS = _load_demo_symbols(max_count=args.demo_count)
        log.warning("=" * 60)
        log.warning("  ★ DEMO MODE 有効 (条件緩和でシグナル多発) ★")
        log.warning("  DON=3, WARMUP=3, R:R=1.0, GAP=6.0%%, STOP=1.5%%")
        log.warning("  監視銘柄: %d銘柄 (日経225先頭から)", len(WATCH_SYMBOLS))
        log.warning("  実運用では使わないでください")
        log.warning("=" * 60)
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("Ctrl+C → 終了")


if __name__ == "__main__":
    main()
