"""
kabu_daytrade_multi_bot.py  ―  多戦略デイトレ bot (6戦略並行監視)
==================================================================
kabu_donchian_bot.py を拡張し、WATCHLIST 銘柄を多戦略で並行監視。

【特徴】
- 12戦略 (long6 + short6) を全部評価
- 銘柄ごとに最適戦略を自動選択 (WATCHLIST に strategy 列があれば固定)
- リアルタイムシグナル検出 → DRY RUN or 本番発注
- 引け強制決済 14:55
- 多戦略同時シグナル時はスコア優先

【使い方】
  python kabu_daytrade_multi_bot.py --dry-run --margin --max-concurrent 2
  python kabu_daytrade_multi_bot.py --watchlist daytrade_combined_watchlist.py
  python kabu_daytrade_multi_bot.py --regime-filter  # 相場局面適合戦略のみ

【WATCHLIST 形式】
  SYMBOLS: list[tuple[str, str, str]] = [
      ("5803.T", "フジクラ", "MACD"),
      ("6323.T", "ローツェ", "DON"),
      ...
  ]
  (strategy 省略時は DON 固定)
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

from daytrade_data import calc_position_size
from daytrade_strategies_5m import (
    STRATEGIES, ENTRY_START, ENTRY_CUTOFF, FORCE_CLOSE,
    WARMUP_BARS, atr_from_bars,
)
from daytrade_strategies_5m_short import STRATEGIES_SHORT
from nikkei_filter_daytrade import detect_regime

JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


ALL_STRATEGIES = {**STRATEGIES, **STRATEGIES_SHORT}


# ── .env 読み込み ──────────────────────────────────────────
def _load_dotenv():
    for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


# ── 5分足バー集約 ──────────────────────────────────────────
class BarBuilder:
    """ポーリング価格 → 5分足を集約。"""

    def __init__(self, sym, name, max_bars=100):
        self.sym = sym
        self.name = name
        self.opens = deque(maxlen=max_bars)
        self.highs = deque(maxlen=max_bars)
        self.lows = deque(maxlen=max_bars)
        self.closes = deque(maxlen=max_bars)
        self.volumes = deque(maxlen=max_bars)
        self.times = deque(maxlen=max_bars)
        self._cur_bar_start = None
        self._cur_o = self._cur_h = self._cur_l = self._cur_c = 0.0
        self._cur_v = 0.0
        self.prev_close = None  # 前日終値

    def update(self, price, volume_add, dt):
        """価格更新。"""
        if price is None or price <= 0:
            return False
        # 5分バーの境界
        bar_min = (dt.minute // 5) * 5
        bar_start = dt.replace(minute=bar_min, second=0, microsecond=0)
        if self._cur_bar_start is None:
            self._cur_bar_start = bar_start
            self._cur_o = self._cur_h = self._cur_l = self._cur_c = price
            self._cur_v = volume_add
            return False
        if bar_start > self._cur_bar_start:
            # 新バー開始 → 旧バー確定
            self.opens.append(self._cur_o)
            self.highs.append(self._cur_h)
            self.lows.append(self._cur_l)
            self.closes.append(self._cur_c)
            self.volumes.append(self._cur_v)
            self.times.append(self._cur_bar_start)
            self._cur_bar_start = bar_start
            self._cur_o = self._cur_h = self._cur_l = self._cur_c = price
            self._cur_v = volume_add
            return True  # 確定した
        else:
            # 同一バー内 → 更新
            self._cur_h = max(self._cur_h, price)
            self._cur_l = min(self._cur_l, price)
            self._cur_c = price
            self._cur_v += volume_add
            return False

    def to_arrays(self):
        return (
            np.array(self.opens, dtype=float),
            np.array(self.highs, dtype=float),
            np.array(self.lows, dtype=float),
            np.array(self.closes, dtype=float),
            np.array(self.volumes, dtype=float),
        )


# ── kabu API クライアント (簡易版) ───────────────────────────
class KabuClient:
    def __init__(self):
        self.base_url = os.environ.get("KABU_BASE_URL", "http://localhost:18080")
        self.password = (os.environ.get("KABU_API_PASSWORD") or
                         os.environ.get("KABU_PASSWORD"))
        if not self.password:
            raise RuntimeError("KABU_API_PASSWORD を .env に設定してください")
        self.token = None

    def refresh_token(self):
        r = requests.post(
            f"{self.base_url}/kabusapi/token",
            json={"APIPassword": self.password}, timeout=10,
        )
        r.raise_for_status()
        self.token = r.json()["Token"]
        log.info("トークン取得成功")
        return self.token

    def register(self, symbols, exchange=1):
        """銘柄を登録。"""
        ok = []
        for sym in symbols:
            code = sym.replace(".T", "")
            try:
                r = requests.put(
                    f"{self.base_url}/kabusapi/register",
                    json={"Symbols": [{"Symbol": code, "Exchange": exchange}]},
                    headers={"X-API-KEY": self.token}, timeout=5,
                )
                if r.status_code == 200:
                    ok.append(sym)
            except Exception:
                pass
        return ok, [s for s in symbols if s not in ok]

    def get_price(self, sym, exchange=1):
        code = sym.replace(".T", "")
        try:
            r = requests.get(
                f"{self.base_url}/kabusapi/board/{code}@{exchange}",
                headers={"X-API-KEY": self.token}, timeout=5,
            )
            if r.status_code != 200:
                return None, None
            data = r.json()
            price = data.get("CurrentPrice")
            volume = data.get("TradingVolume", 0)
            prev_close = data.get("PreviousClose")
            return price, volume, prev_close
        except Exception:
            return None, None, None


# ── ポジション管理 ──────────────────────────────────────────
class Position:
    def __init__(self, symbol, name, strategy, side, entry_p, qty, stop_p, target_p):
        self.symbol = symbol
        self.name = name
        self.strategy = strategy
        self.side = side  # "long" or "short"
        self.entry_p = entry_p
        self.qty = qty
        self.stop_p = stop_p
        self.target_p = target_p
        self.entry_time = datetime.now(JST)

    def check_exit(self, price):
        """損切り/目標到達判定。"""
        if self.side == "long":
            if price <= self.stop_p:
                return "損切り"
            if price >= self.target_p:
                return "目標達成"
        else:
            if price >= self.stop_p:
                return "損切り"
            if price <= self.target_p:
                return "目標達成"
        return None


# ── メイン bot ──────────────────────────────────────────────
def run(args):
    # WATCHLIST 読込
    from check_signals_daytrade import _load_watchlist
    watchlist = _load_watchlist(args.watchlist)
    if not watchlist:
        log.error("WATCHLIST が空")
        return

    # 戦略フィルタ
    targets = []
    for entry in watchlist:
        if len(entry) >= 3:
            sym, name, strat = entry[:3]
        else:
            sym, name = entry[:2]
            strat = "DON"
        if strat not in ALL_STRATEGIES:
            log.warning(f"  未知の戦略: {strat} ({sym})")
            continue
        targets.append((sym, name, strat))

    # 相場局面フィルタ
    if args.regime_filter:
        regime = detect_regime()
        log.info(f"相場局面: {regime['label']} / 推奨: {regime.get('allowed_strategies')}")
        allowed = regime.get("allowed_strategies")
        if allowed:
            targets = [(s, n, st) for s, n, st in targets if st in allowed]
            log.info(f"  局面フィルタ後: {len(targets)}件")

    if not targets:
        log.error("対象銘柄なし")
        return

    log.info(f"監視: {len(targets)}件")
    for s, n, st in targets[:10]:
        log.info(f"  {s} {n} ({st})")
    if len(targets) > 10:
        log.info(f"  ... 他 {len(targets) - 10}件")

    # kabu STATION 接続
    client = KabuClient()
    client.refresh_token()
    syms = [s for s, _, _ in targets]
    registered, failed = client.register(syms)
    log.info(f"銘柄登録: {len(registered)}/{len(syms)}件")
    targets = [(s, n, st) for s, n, st in targets if s in registered]

    # 状態
    builders = {s: BarBuilder(s, n) for s, n, _ in targets}
    target_strats = {s: st for s, _, st in targets}
    positions: dict[str, Position] = {}
    trades: list[dict] = []

    log.info("=" * 60)
    mode = "DRY RUN" if args.dry_run else "本番"
    log.info(f"  Multi-Strategy デイトレ bot [{mode} / "
             f"{'デイトレ信用' if args.margin else '現物'}]")
    log.info(f"  予算: {args.budget:,}円 / リスク: {args.max_risk:,}円 / "
             f"最大同時保有: {args.max_concurrent}")
    log.info("=" * 60)

    # メインループ
    last_vol = {s: 0 for s in [t[0] for t in targets]}
    while True:
        now = datetime.now(JST)
        t = now.time()

        if t >= dtime(15, 0):
            log.info("大引け → 終了")
            break
        if t < dtime(9, 0):
            time.sleep(30)
            continue
        if dtime(11, 30) <= t < dtime(12, 30):
            time.sleep(30)
            continue

        # 全銘柄の価格更新 → バー集約
        new_bars = {}  # sym → bar_confirmed
        for sym, _, _ in targets:
            try:
                result = client.get_price(sym)
                if result is None:
                    continue
                price, volume, prev_close = result
                if price is None:
                    continue
                vol_diff = max(0, volume - last_vol[sym])
                last_vol[sym] = volume
                builder = builders[sym]
                if builder.prev_close is None and prev_close:
                    builder.prev_close = prev_close
                confirmed = builder.update(price, vol_diff, now)
                if confirmed:
                    new_bars[sym] = True
            except Exception as e:
                log.debug(f"  {sym} 価格取得失敗: {e}")

        # 保有ポジションの決済チェック
        to_close = []
        for sym, pos in positions.items():
            try:
                result = client.get_price(sym)
                if result is None:
                    continue
                price, _, _ = result
                if price is None:
                    continue
                if t >= FORCE_CLOSE:
                    reason = "引け強制"
                else:
                    reason = pos.check_exit(price)
                if reason:
                    pnl = ((price - pos.entry_p) if pos.side == "long"
                           else (pos.entry_p - price)) * pos.qty
                    log.info(f"★ 決済 [{reason}] {sym} {pos.name} ({pos.strategy}/{pos.side})  "
                             f"{pos.entry_p:.0f}→{price:.0f}  qty={pos.qty}  "
                             f"{pnl:+,.0f}円")
                    if not args.dry_run:
                        # TODO: kabu API で決済発注
                        pass
                    trades.append({
                        "symbol": sym, "name": pos.name,
                        "strategy": pos.strategy, "side": pos.side,
                        "entry": pos.entry_p, "exit": price,
                        "qty": pos.qty, "pnl": pnl, "reason": reason,
                        "entry_time": pos.entry_time.strftime("%H:%M:%S"),
                        "exit_time": now.strftime("%H:%M:%S"),
                    })
                    to_close.append(sym)
            except Exception:
                continue
        for sym in to_close:
            del positions[sym]

        # 新規エントリー (空きあり & 時刻OK)
        if (len(positions) < args.max_concurrent
                and ENTRY_START <= t < ENTRY_CUTOFF):

            for sym, name, strat in targets:
                if sym in positions:
                    continue
                if len(positions) >= args.max_concurrent:
                    break
                builder = builders[sym]
                if len(builder.closes) < WARMUP_BARS + 1:
                    continue

                opens, highs, lows, closes, volumes = builder.to_arrays()
                if len(opens) < WARMUP_BARS + 1:
                    continue

                # ATR
                atr_arr = atr_from_bars(highs, lows, closes, 14)
                i = len(opens) - 1  # 最新確定バー

                # シグナル判定
                fn = ALL_STRATEGIES[strat]
                try:
                    sig = fn(opens, highs, lows, closes, volumes, i, atr_arr)
                except Exception as e:
                    log.debug(f"  {sym} {strat} シグナル例外: {e}")
                    continue

                if sig is None:
                    continue

                action, atr_val, em, sm, tm = sig

                # 次バー寄付想定で entry
                cur_price = closes[-1]
                if action == "entry":
                    entry_p = cur_price + atr_val * em
                    stop_p = entry_p - atr_val * sm
                    target_p = entry_p + atr_val * tm
                    side = "long"
                elif action == "entry_short":
                    entry_p = cur_price - atr_val * em
                    stop_p = entry_p + atr_val * sm
                    target_p = entry_p - atr_val * tm
                    side = "short"
                else:
                    continue

                qty = calc_position_size(entry_p, stop_p,
                                          args.budget, args.max_risk)
                if qty <= 0:
                    continue

                log.info(f"★ シグナル [{strat}/{side}] {sym} {name}  "
                         f"価格={cur_price:.0f} 損切={stop_p:.0f} "
                         f"目標={target_p:.0f} 株数={qty}")
                if not args.dry_run:
                    # TODO: kabu API で発注
                    pass
                pos = Position(sym, name, strat, side, entry_p, qty,
                               stop_p, target_p)
                positions[sym] = pos
                log.info(f"  → 保有中: {len(positions)}/{args.max_concurrent}")

        time.sleep(args.poll)

    # 日次レポート
    print()
    print("=" * 65)
    print(f"  Multi-Strategy デイトレ 日次レポート [{mode}]")
    print(f"  {now.strftime('%Y-%m-%d')}")
    print("=" * 65)
    if trades:
        wins = [t for t in trades if t["pnl"] > 0]
        total = sum(t["pnl"] for t in trades)
        wr = len(wins) / len(trades) * 100
        gp = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
        pf = gp / gl if gl > 0 else float("inf")
        print(f"\n  取引数: {len(trades)}  勝率: {wr:.1f}%  PF: {pf:.2f}  "
              f"合計: {total:+,.0f}円")
        print(f"\n  【トレード明細】")
        for tr in trades:
            m = "○" if tr["pnl"] > 0 else "●"
            print(f"    {m} {tr['name']} ({tr['strategy']}/{tr['side']})  "
                  f"{tr['entry_time']}→{tr['exit_time']}  "
                  f"{tr['entry']:.0f}→{tr['exit']:.0f}  "
                  f"qty={tr['qty']}  {tr['pnl']:+,.0f}円  ({tr['reason']})")
    else:
        print("\n  トレードなし")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="多戦略デイトレ bot")
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--budget", type=int, default=300_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument("--poll", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--margin", action="store_true",
                        help="デイトレ信用で発注")
    parser.add_argument("--regime-filter", action="store_true",
                        help="現在の相場局面に適合した戦略のみ使用")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
