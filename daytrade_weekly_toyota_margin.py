"""
週次デイトレ バックテスト【信用取引版】— 2026/03/16 週（月〜金）
銘柄  : トヨタ自動車 (7203.T)
データ: yfinance 1 分足（ネット遮断時は合成データで自動フォールバック）
戦略  : マルチシグナル（MA クロス + RSI + Bollinger Bands）

■ 信用取引ルール:
  - 委託保証金率   : 30%（最低）→ 実質レバレッジ最大 3.3 倍
  - 維持率         : 20% 未満で強制ロスカット（追証ではなく即決済）
  - 買い建て / 売り建て 両対応（ロング・ショート）
  - 日中決済（デイトレ）のため貸株料・金利は 0 円
  - 建玉は 1 方向のみ（ロングかショートのどちらか一本）
  - 引けまでに未決済なら強制決済
  - 初期資金は保証金として毎日繰り越し
"""

import math
import random
from collections import deque
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL       = "7203.T"
WEEK_START   = "2026-03-16"   # 月曜日
INTERVAL     = "1m"

# MA クロス
SHORT_PERIOD   = 3
LONG_PERIOD    = 10

# RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35   # 以下 → ロング買いシグナル
RSI_OVERBOUGHT = 68   # 以上 → ショート売りシグナル

# Bollinger Bands
BB_PERIOD = 20
BB_K      = 2.0

# 信用取引パラメータ
INITIAL_MARGIN      = 1_000_000  # 初期委託保証金（円）
MARGIN_RATE         = 0.30       # 委託保証金率（30%）→ レバレッジ ≈ 3.33 倍
MAINTENANCE_RATE    = 0.20       # 維持率（20% 未満で強制ロスカット）
QTY                 = 300        # 1 回の取引株数（レバレッジ活用で現物の 3 倍）
MAX_HOLD_BARS       = 30         # タイムカット（分）
COMMISSION_RATE     = 0.00       # 信用手数料率（デイトレ無料想定）


# ── 取引可能か判定（保証金残高ベース） ───────────────────────
def can_open(margin_cash: float, price: float, qty: int) -> bool:
    """委託保証金率を満たせるか確認"""
    required_margin = price * qty * MARGIN_RATE
    return margin_cash >= required_margin


def maintenance_ratio(margin_cash: float, price: float, qty: int,
                      entry_price: float, direction: str) -> float:
    """
    維持率 = (保証金 + 建玉評価損益) / 建玉時価 × 100
    direction: "long" or "short"
    """
    if direction == "long":
        unrealized = (price - entry_price) * qty
    else:
        unrealized = (entry_price - price) * qty
    position_value = price * qty
    ratio = (margin_cash + unrealized) / position_value * 100 if position_value else 0
    return ratio


# ── 分析対象週 ────────────────────────────────────────────────
def trading_days(week_start: str) -> list[str]:
    base = datetime.strptime(week_start, "%Y-%m-%d")
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]


# ── 1. yfinance データ取得 ────────────────────────────────────
def fetch_week_yfinance(dates: list[str]) -> dict[str, list[dict]]:
    import yfinance as yf
    import pandas as pd

    start = dates[0]
    end   = (datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[yfinance] {SYMBOL}  {start} 〜 {end}  interval={INTERVAL} を取得中...")
    df = yf.download(SYMBOL, start=start, end=end, interval=INTERVAL,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError("yfinance: データが空でした")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)

    result: dict[str, list[dict]] = {}
    for date_str in dates:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
        sub = df[df.index.date == day]
        if sub.empty:
            continue
        result[date_str] = [
            {"dt": row.Index.to_pydatetime(),
             "open": float(row.Open), "high": float(row.High),
             "low": float(row.Low),   "close": float(row.Close),
             "volume": int(row.Volume)}
            for row in sub.itertuples()
        ]
    if not result:
        raise RuntimeError("yfinance: 対象週にデータがありませんでした")
    print(f"[yfinance] 取得成功: {len(result)} 日 / {sum(len(v) for v in result.values())} 本\n")
    return result


# ── 2. 合成データ（フォールバック） ──────────────────────────
def generate_day_bars(date_str: str, open_price: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    year, month, day = map(int, date_str.split("-"))
    sessions = [
        (datetime(year, month, day,  9,  0), datetime(year, month, day, 11, 30)),
        (datetime(year, month, day, 12, 30), datetime(year, month, day, 15, 30)),
    ]
    price = open_price
    bars  = []
    for (start, end) in sessions:
        dt = start
        while dt < end:
            change = rng.gauss(0.02, 3.0)
            if dt.hour == 9  and dt.minute < 15: change *= 2.0
            if dt.hour == 15 and dt.minute >= 15: change *= 1.5
            price = max(3_300.0, min(3_900.0, price + change))
            o = price + rng.gauss(0, 1.5)
            h = max(o, price) + abs(rng.gauss(0, 2))
            l = min(o, price) - abs(rng.gauss(0, 2))
            bars.append({"dt": dt, "open": round(o, 1), "high": round(h, 1),
                         "low": round(l, 1), "close": round(price, 1),
                         "volume": max(0, int(rng.gauss(60_000, 15_000)))})
            dt += timedelta(minutes=1)
    return bars


def generate_week_synthetic(dates: list[str]) -> dict[str, list[dict]]:
    result = {}
    price  = 3_560.0
    for i, d in enumerate(dates):
        seed = int(d.replace("-", "")) + i
        bars = generate_day_bars(d, price, seed)
        if bars:
            price = bars[-1]["close"]
            result[d] = bars
    return result


def load_week_bars(dates: list[str]) -> tuple[dict[str, list[dict]], str]:
    try:
        data   = fetch_week_yfinance(dates)
        source = f"yfinance ({SYMBOL} {INTERVAL})"
    except Exception as e:
        print(f"[警告] yfinance 取得失敗: {e}")
        print("[フォールバック] 合成データを使用します\n")
        data   = generate_week_synthetic(dates)
        source = "合成データ (seed 固定, 1 分足)"
    return data, source


# ── 3. インジケーター ─────────────────────────────────────────
class IndicatorEngine:
    def __init__(self):
        self._prices: deque[float] = deque(maxlen=max(LONG_PERIOD, BB_PERIOD, RSI_PERIOD + 1))
        self._prev_sig: str | None = None
        self._prev_close: float | None = None
        self._gains:  deque[float] = deque(maxlen=RSI_PERIOD)
        self._losses: deque[float] = deque(maxlen=RSI_PERIOD)

    def reset(self):
        self._prices.clear()
        self._prev_sig    = None
        self._prev_close  = None
        self._gains.clear()
        self._losses.clear()

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
        return {
            "ma_signal": ma_signal,
            "rsi":       self._rsi(),
            "bb_lower":  bb_lower,
            "bb_upper":  bb_upper,
        }


# ── 4. シグナル判定（ロング / ショート） ────────────────────
def long_entry_signal(ind: dict, price: float) -> bool:
    """ロング（買い建て）エントリー条件"""
    if ind["ma_signal"] == "up":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi < RSI_OVERSOLD:
        return True
    bb_l = ind["bb_lower"]
    if bb_l is not None and price <= bb_l and (ind["rsi"] or 50) < 50:
        return True
    return False


def short_entry_signal(ind: dict, price: float) -> bool:
    """ショート（売り建て）エントリー条件"""
    if ind["ma_signal"] == "down":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi > RSI_OVERBOUGHT:
        return True
    bb_u = ind["bb_upper"]
    if bb_u is not None and price >= bb_u and (ind["rsi"] or 50) > 50:
        return True
    return False


def long_exit_signal(ind: dict, price: float) -> bool:
    """ロング決済条件"""
    if ind["ma_signal"] == "down":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi > RSI_OVERBOUGHT:
        return True
    bb_u = ind["bb_upper"]
    if bb_u is not None and price >= bb_u:
        return True
    return False


def short_exit_signal(ind: dict, price: float) -> bool:
    """ショート決済条件"""
    if ind["ma_signal"] == "up":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi < RSI_OVERSOLD:
        return True
    bb_l = ind["bb_lower"]
    if bb_l is not None and price <= bb_l:
        return True
    return False


# ── 5. 1 日分バックテスト（信用取引） ────────────────────────
def backtest_day_margin(bars: list[dict], margin_cash: float) -> tuple[list[dict], float]:
    """
    margin_cash : 保証金残高（P&L をここに加減算）
    Returns     : (trades, 翌日繰越保証金)
    """
    engine    = IndicatorEngine()
    direction: str | None = None   # "long" or "short"
    entry_p   = None
    entry_dt  = None
    hold_bars = 0
    trades    = []

    for bar in bars:
        price = bar["close"]
        dt    = bar["dt"]
        ind   = engine.feed(price)

        # ── ポジションあり ────────────────────────────────────
        if direction is not None:
            hold_bars += 1
            exit_reason = None

            # 維持率チェック → 強制ロスカット
            ratio = maintenance_ratio(margin_cash, price, QTY, entry_p, direction)
            if ratio < MAINTENANCE_RATE * 100:
                exit_reason = f"強制ロスカット(維持率{ratio:.1f}%)"

            elif hold_bars >= MAX_HOLD_BARS:
                exit_reason = f"タイムカット({MAX_HOLD_BARS}分)"

            elif direction == "long"  and long_exit_signal(ind, price):
                exit_reason = "シグナル決済(L)"

            elif direction == "short" and short_exit_signal(ind, price):
                exit_reason = "シグナル決済(S)"

            if exit_reason:
                pnl = ((price - entry_p) if direction == "long"
                       else (entry_p - price)) * QTY
                margin_cash += pnl
                trades.append({
                    "direction":   direction,
                    "entry_dt":    entry_dt,
                    "exit_dt":     dt,
                    "entry_price": entry_p,
                    "exit_price":  price,
                    "pnl":         pnl,
                    "margin":      margin_cash,
                    "note":        exit_reason,
                })
                direction = None
                entry_p   = None
                entry_dt  = None
                hold_bars = 0

        # ── ポジションなし ─────────────────────────────────────
        if direction is None:
            if long_entry_signal(ind, price) and can_open(margin_cash, price, QTY):
                direction = "long"
                entry_p   = price
                entry_dt  = dt
                hold_bars = 0

            elif short_entry_signal(ind, price) and can_open(margin_cash, price, QTY):
                direction = "short"
                entry_p   = price
                entry_dt  = dt
                hold_bars = 0

    # ── 引け強制決済 ──────────────────────────────────────────
    if direction is not None:
        b   = bars[-1]
        pnl = ((b["close"] - entry_p) if direction == "long"
               else (entry_p - b["close"])) * QTY
        margin_cash += pnl
        trades.append({
            "direction":   direction,
            "entry_dt":    entry_dt,
            "exit_dt":     b["dt"],
            "entry_price": entry_p,
            "exit_price":  b["close"],
            "pnl":         pnl,
            "margin":      margin_cash,
            "note":        "引け強制決済",
        })

    return trades, margin_cash


# ── 6. レポート ──────────────────────────────────────────────
DAY_JP = ["月", "火", "水", "木", "金"]

def report(week_result: dict, source: str):
    W = 72
    lev = 1 / MARGIN_RATE
    print("=" * W)
    print(f"  週次デイトレ バックテスト【信用取引版】  {WEEK_START} 週  {SYMBOL} (トヨタ)")
    print("=" * W)
    print(f"  データ      : {source}")
    print(f"  戦略        : MA({SHORT_PERIOD}/{LONG_PERIOD}) + RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_K}σ)")
    print(f"  タイムカット: {MAX_HOLD_BARS} 分  /  取引株数 {QTY} 株")
    print(f"  委託保証金率: {MARGIN_RATE*100:.0f}%  /  実質レバレッジ: {lev:.2f} 倍")
    print(f"  維持率下限  : {MAINTENANCE_RATE*100:.0f}% 未満で強制ロスカット")
    print(f"  初期保証金  : {INITIAL_MARGIN:,.0f} 円")
    print("=" * W)

    all_trades = []
    prev_margin = INITIAL_MARGIN

    for i, (date_str, info) in enumerate(week_result.items()):
        dow     = DAY_JP[i]
        trades  = info["trades"]
        margin  = info["final_margin"]
        n_bars  = info["n_bars"]
        day_pnl = margin - prev_margin

        longs  = [t for t in trades if t["direction"] == "long"]
        shorts = [t for t in trades if t["direction"] == "short"]
        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_r  = len(wins) / len(trades) * 100 if trades else 0.0

        sign = "+" if day_pnl >= 0 else ""
        print(f"\n【{dow}】{date_str}  ({n_bars} 本)  "
              f"計 {len(trades)} 回（L:{len(longs)} S:{len(shorts)}）  勝率 {win_r:.0f}%")
        print(f"  保証金: {prev_margin:>10,.0f} 円 → {margin:>10,.0f} 円  "
              f"({sign}{day_pnl:,.0f} 円)")

        if trades:
            print(f"  {'方向':2} {'エントリー':5} {'決済':5}  {'買値':>7} {'売値':>7}  {'損益':>9}  結果")
            print(f"  " + "-" * 62)
            for t in trades:
                mark = "○" if t["pnl"] > 0 else "●"
                dr   = "L" if t["direction"] == "long" else "S"
                note = t.get("note", "")
                print(
                    f"  {dr}  "
                    f"{t['entry_dt'].strftime('%H:%M')}  "
                    f"{t['exit_dt'].strftime('%H:%M')}  "
                    f"{t['entry_price']:>7.1f} "
                    f"{t['exit_price']:>7.1f}  "
                    f"{t['pnl']:>+9,.0f}  {mark} {note}"
                )
        else:
            print("  （トレードなし）")

        all_trades.extend(trades)
        prev_margin = margin

    # ── 週間サマリー ─────────────────────────────────────────
    final    = prev_margin
    total_p  = final - INITIAL_MARGIN
    ret_pct  = total_p / INITIAL_MARGIN * 100
    eff_ret  = total_p / (INITIAL_MARGIN * lev) * 100   # 実運用額ベース

    all_wins   = [t for t in all_trades if t["pnl"] > 0]
    all_losses = [t for t in all_trades if t["pnl"] <= 0]
    n_all      = len(all_trades)
    n_long     = len([t for t in all_trades if t["direction"] == "long"])
    n_short    = len([t for t in all_trades if t["direction"] == "short"])
    win_r_all  = len(all_wins) / n_all * 100 if all_trades else 0.0
    avg_w      = sum(t["pnl"] for t in all_wins)   / len(all_wins)   if all_wins   else 0.0
    avg_l      = sum(t["pnl"] for t in all_losses) / len(all_losses) if all_losses else 0.0
    payoff     = abs(avg_w / avg_l) if avg_l else float("inf")

    # 最大ドローダウン（保証金ベース）
    asset_vals = [INITIAL_MARGIN] + [t["margin"] for t in all_trades]
    peak = asset_vals[0]; max_dd = 0.0
    for v in asset_vals:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    print("\n" + "=" * W)
    print("  【週間サマリー】（信用取引・デイトレ）")
    print("=" * W)
    print(f"  初期保証金           : {INITIAL_MARGIN:>12,.0f} 円")
    print(f"  最終保証金残高       : {final:>12,.0f} 円")
    print(f"  週間損益             : {total_p:>+12,.0f} 円")
    print(f"  保証金リターン       : {ret_pct:>+11.2f} %")
    print(f"  実運用額リターン     : {eff_ret:>+11.2f} %  (÷{lev:.1f}倍)")
    print("-" * W)
    print(f"  総トレード数         : {n_all:>12} 回")
    print(f"  うち ロング(L)       : {n_long:>12} 回")
    print(f"  うち ショート(S)     : {n_short:>12} 回")
    print(f"  勝ち / 負け          : {len(all_wins):>5} 勝 / {len(all_losses)} 敗")
    print(f"  週間勝率             : {win_r_all:>11.1f} %")
    print(f"  平均利益             : {avg_w:>+12,.0f} 円")
    print(f"  平均損失             : {avg_l:>+12,.0f} 円")
    print(f"  ペイオフ比           : {payoff:>12.2f}")
    print(f"  最大ドローダウン     : {max_dd:>+11.2f} %")
    print("=" * W)

    # 日別損益バーチャート
    print("\n--- 日別損益チャート ---")
    day_pnls = [info["final_margin"] - (INITIAL_MARGIN if i == 0 else
                list(week_result.values())[i-1]["final_margin"])
                for i, info in enumerate(week_result.values())]
    max_abs = max(abs(p) for p in day_pnls) or 1
    for i, (date_str, pnl) in enumerate(zip(week_result.keys(), day_pnls)):
        w    = int(abs(pnl) / max_abs * 30)
        bar  = ("+" if pnl >= 0 else "-") * w
        sign = "+" if pnl >= 0 else ""
        print(f"  {DAY_JP[i]} {date_str}  {sign}{pnl:>7,.0f} 円  |{bar}")


# ── 7. エントリーポイント ────────────────────────────────────
if __name__ == "__main__":
    dates     = trading_days(WEEK_START)
    week_bars, source = load_week_bars(dates)

    week_result: dict[str, dict] = {}
    margin_cash = float(INITIAL_MARGIN)

    for date_str in dates:
        if date_str not in week_bars:
            print(f"[スキップ] {date_str}: データなし（祝日等）")
            continue
        bars             = week_bars[date_str]
        trades, margin_cash = backtest_day_margin(bars, margin_cash)
        week_result[date_str] = {
            "trades":       trades,
            "final_margin": margin_cash,
            "n_bars":       len(bars),
        }

    report(week_result, source)
