"""
直近 60 日デイトレ バックテスト【現物取引版】
銘柄  : トヨタ自動車 (7203.T)
データ: yfinance 5 分足・一括取得（直近 60 日以内なのでチャンク不要）
戦略  : マルチシグナル（MA クロス + RSI + Bollinger Bands）

■ 現物取引ルール:
  - 初期資金     : 100 万円
  - 売買単位     : 100 株（1 単元）
  - 取引株数     : 買付可能な最大単元数（上限 500 株）
  - ロングのみ（現物はショート不可）
  - 日中決済（デイトレ）
  - 引けまでに未決済なら強制決済
  - 手数料       : 0 円（ネット証券の無料プラン想定）
  - 信用・レバレッジなし

⚠ 注意: バックテストは過去データに基づくものであり、将来の利益を保証しません。
"""

import math
import random
from collections import deque
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL     = "7203.T"
END_DATE   = "2026-03-21"
DAYS_AGO   = 7
START_DATE = (datetime.strptime(END_DATE, "%Y-%m-%d")
              - timedelta(days=DAYS_AGO)).strftime("%Y-%m-%d")
INTERVAL   = "1m"

# MA クロス
SHORT_PERIOD   = 3
LONG_PERIOD    = 10

# RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 68

# Bollinger Bands
BB_PERIOD = 20
BB_K      = 2.0

# 現物取引パラメータ
INITIAL_CASH = 1_000_000   # 100 万円
LOT_SIZE     = 100          # 1 単元 = 100 株
MAX_QTY      = 500          # 最大取引株数（5 単元）
MAX_HOLD_BARS = 30          # 30 本 × 1 分 = 30 分タイムカット


# ── 取引株数計算（買付余力内の最大単元数） ──────────────────
def calc_qty(cash: float, price: float) -> int:
    lots = min(int(cash / (price * LOT_SIZE)), MAX_QTY // LOT_SIZE)
    return lots * LOT_SIZE


# ── 営業日リスト ──────────────────────────────────────────────
def trading_days(start: str, end: str) -> list[str]:
    base = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end,   "%Y-%m-%d")
    days, cur = [], base
    while cur <= stop:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


# ── yfinance 一括取得 ─────────────────────────────────────────
def fetch_yfinance(start: str, end: str) -> dict[str, list[dict]]:
    import yfinance as yf
    import pandas as pd

    end_next = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[yfinance] {SYMBOL}  {start} 〜 {end}  interval={INTERVAL} を取得中...")
    df = yf.download(SYMBOL, start=start, end=end_next, interval=INTERVAL,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError("yfinance: データが空でした")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)

    result: dict[str, list[dict]] = {}
    for day_date in sorted(set(df.index.date)):
        d_str = day_date.strftime("%Y-%m-%d")
        sub   = df[df.index.date == day_date]
        if sub.empty:
            continue
        result[d_str] = [
            {"dt":     row.Index.to_pydatetime(),
             "open":   float(row.Open),  "high": float(row.High),
             "low":    float(row.Low),   "close": float(row.Close),
             "volume": int(row.Volume)}
            for row in sub.itertuples()
        ]

    total = sum(len(v) for v in result.values())
    print(f"[yfinance] 取得成功: {len(result)} 日 / {total:,} 本\n")
    return result


# ── 合成データ（フォールバック） ──────────────────────────────
def generate_day_bars_5m(date_str: str, open_price: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    y, m, d = map(int, date_str.split("-"))
    sessions = [
        (datetime(y, m, d,  9,  0), datetime(y, m, d, 11, 30)),
        (datetime(y, m, d, 12, 30), datetime(y, m, d, 15, 30)),
    ]
    price, bars = open_price, []
    for start, end in sessions:
        dt = start
        while dt < end:
            change = random.Random(seed + int(dt.timestamp())).gauss(0.1, 8.0)
            if dt.hour == 9  and dt.minute < 15:  change *= 2.0
            if dt.hour == 15 and dt.minute >= 15: change *= 1.5
            price = max(2_000.0, min(5_000.0, price + change))
            o = price + rng.gauss(0, 3)
            h = max(o, price) + abs(rng.gauss(0, 5))
            l = min(o, price) - abs(rng.gauss(0, 5))
            bars.append({
                "dt": dt, "open": round(o, 1), "high": round(h, 1),
                "low": round(l, 1), "close": round(price, 1),
                "volume": max(0, int(rng.gauss(300_000, 80_000))),
            })
            dt += timedelta(minutes=5)
    return bars


def load_bars(dates: list[str]) -> tuple[dict[str, list[dict]], str]:
    data   = fetch_yfinance(START_DATE, END_DATE)
    source = f"yfinance ({SYMBOL} {INTERVAL})"
    return data, source


# ── インジケーター ────────────────────────────────────────────
class IndicatorEngine:
    def __init__(self):
        maxlen = max(LONG_PERIOD, BB_PERIOD, RSI_PERIOD + 1)
        self._prices: deque[float] = deque(maxlen=maxlen)
        self._prev_sig:   str | None   = None
        self._prev_close: float | None = None
        self._gains:  deque[float] = deque(maxlen=RSI_PERIOD)
        self._losses: deque[float] = deque(maxlen=RSI_PERIOD)

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
        return {"ma_signal": ma_signal, "rsi": self._rsi(),
                "bb_lower": bb_lower, "bb_upper": bb_upper}


# ── シグナル判定（ロングのみ） ────────────────────────────────
def long_entry(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "up": return True
    rsi = ind["rsi"]
    if rsi is not None and rsi < RSI_OVERSOLD: return True
    bb_l = ind["bb_lower"]
    if bb_l is not None and price <= bb_l and (ind["rsi"] or 50) < 50: return True
    return False

def long_exit(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "down": return True
    rsi = ind["rsi"]
    if rsi is not None and rsi > RSI_OVERBOUGHT: return True
    bb_u = ind["bb_upper"]
    if bb_u is not None and price >= bb_u: return True
    return False


# ── 1 日分バックテスト ────────────────────────────────────────
def backtest_day(bars: list[dict], cash: float) -> tuple[list[dict], float]:
    engine    = IndicatorEngine()
    in_pos    = False
    entry_p   = 0.0
    entry_dt  = None
    hold_bars = 0
    qty       = 0
    trades    = []

    for bar in bars:
        price = bar["close"]
        dt    = bar["dt"]
        ind   = engine.feed(price)

        if in_pos:
            hold_bars += 1
            exit_reason = None
            if hold_bars >= MAX_HOLD_BARS:
                exit_reason = f"タイムカット({MAX_HOLD_BARS*5}分)"
            elif long_exit(ind, price):
                exit_reason = "シグナル"

            if exit_reason:
                pnl  = (price - entry_p) * qty
                cash += price * qty          # 売却代金を回収
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     dt,
                    "entry_price": entry_p,
                    "exit_price":  price,
                    "qty":         qty,
                    "pnl":         pnl,
                    "cash":        cash,
                    "note":        exit_reason,
                })
                in_pos = False

        if not in_pos and long_entry(ind, price):
            qty = calc_qty(cash, price)
            if qty > 0:
                cash    -= price * qty      # 購入代金を拘束
                entry_p  = price
                entry_dt = dt
                hold_bars = 0
                in_pos   = True

    if in_pos:
        b    = bars[-1]
        pnl  = (b["close"] - entry_p) * qty
        cash += b["close"] * qty
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     b["dt"],
            "entry_price": entry_p,
            "exit_price":  b["close"],
            "qty":         qty,
            "pnl":         pnl,
            "cash":        cash,
            "note":        "引け強制決済",
        })

    return trades, cash


# ── レポート ──────────────────────────────────────────────────
DAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

def report(result: dict, source: str):
    W = 74
    print("=" * W)
    print(f"  直近 {DAYS_AGO} 日デイトレ バックテスト【現物取引版】  {SYMBOL} (トヨタ自動車)")
    print("=" * W)
    print(f"  期間     : {START_DATE} 〜 {END_DATE}")
    print(f"  データ   : {source}")
    print(f"  戦略     : MA({SHORT_PERIOD}/{LONG_PERIOD}) + RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_K}σ)")
    print(f"  足種     : {INTERVAL}  /  タイムカット {MAX_HOLD_BARS}本({MAX_HOLD_BARS*5}分)")
    print(f"  取引形態 : 現物取引（ロングのみ）  /  最大 {MAX_QTY} 株・{LOT_SIZE} 株単位")
    print(f"  初期資金 : {INITIAL_CASH:,.0f} 円")
    print("=" * W)

    all_trades: list[dict] = []
    prev_cash = float(INITIAL_CASH)
    asset_vals = [float(INITIAL_CASH)]

    # ── 日別明細 ─────────────────────────────────────────────
    print(f"\n{'日付':10}  {'曜':1}  {'本数':>4}  {'取引':>4}  "
          f"{'勝率':>5}  {'日次損益':>12}  {'資金残高':>14}")
    print("-" * W)

    for date_str, info in result.items():
        trades  = info["trades"]
        cash    = info["final_cash"]
        n_bars  = info["n_bars"]
        day_pnl = cash - prev_cash
        dow     = DAY_JP[datetime.strptime(date_str, "%Y-%m-%d").weekday()]

        wins = [t for t in trades if t["pnl"] > 0]
        wr   = len(wins) / len(trades) * 100 if trades else 0.0
        sign = "+" if day_pnl >= 0 else ""

        print(f"  {date_str}  {dow}  {n_bars:>4}本  {len(trades):>4}回  "
              f"{wr:>4.0f}%  {sign}{day_pnl:>12,.0f}円  {cash:>14,.0f}円")

        all_trades.extend(trades)
        for t in trades:
            asset_vals.append(t["cash"])
        prev_cash = cash

    # ── 全体サマリー ──────────────────────────────────────────
    final   = prev_cash
    total_p = final - INITIAL_CASH
    ret_pct = total_p / INITIAL_CASH * 100

    all_wins   = [t for t in all_trades if t["pnl"] > 0]
    all_losses = [t for t in all_trades if t["pnl"] <= 0]
    n_all      = len(all_trades)
    win_r      = len(all_wins) / n_all * 100 if all_trades else 0.0
    avg_w      = sum(t["pnl"] for t in all_wins)   / len(all_wins)   if all_wins   else 0.0
    avg_l      = sum(t["pnl"] for t in all_losses) / len(all_losses) if all_losses else 0.0
    payoff     = abs(avg_w / avg_l) if avg_l else float("inf")
    n_days     = len(result)
    avg_tr_day = n_all / n_days if n_days else 0

    peak = asset_vals[0]; max_dd = 0.0
    for v in asset_vals:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    streak_w = streak_l = cur_w = cur_l = 0
    for t in all_trades:
        if t["pnl"] > 0:
            cur_w += 1; cur_l = 0; streak_w = max(streak_w, cur_w)
        else:
            cur_l += 1; cur_w = 0; streak_l = max(streak_l, cur_l)

    print("\n" + "=" * W)
    print(f"  【{DAYS_AGO} 日間サマリー】（現物取引・デイトレ）")
    print("=" * W)
    print(f"  初期資金             : {INITIAL_CASH:>14,.0f} 円")
    print(f"  最終資金残高         : {final:>14,.0f} 円")
    print(f"  期間損益             : {total_p:>+14,.0f} 円")
    print(f"  資金リターン         : {ret_pct:>+13.2f} %")
    print("-" * W)
    print(f"  営業日数             : {n_days:>14} 日")
    print(f"  総トレード数         : {n_all:>14} 回")
    print(f"  1日平均トレード数    : {avg_tr_day:>14.1f} 回")
    print(f"  勝ち / 負け          : {len(all_wins):>6} 勝 / {len(all_losses)} 敗")
    print(f"  勝率                 : {win_r:>13.1f} %")
    print(f"  平均利益             : {avg_w:>+14,.0f} 円")
    print(f"  平均損失             : {avg_l:>+14,.0f} 円")
    print(f"  ペイオフ比           : {payoff:>14.2f}")
    print(f"  最大ドローダウン     : {max_dd:>+13.2f} %")
    print(f"  最大連続勝ち         : {streak_w:>14} 回")
    print(f"  最大連続負け         : {streak_l:>14} 回")
    print("=" * W)

    # 日別損益バーチャート
    items     = list(result.items())
    pnls      = []
    prev_c    = float(INITIAL_CASH)
    for _, info in items:
        pnls.append(info["final_cash"] - prev_c)
        prev_c = info["final_cash"]
    max_abs = max(abs(p) for p in pnls) if pnls else 1

    print(f"\n--- 日別損益チャート（直近 {DAYS_AGO} 日） ---")
    for (date_str, _), pnl in zip(items, pnls):
        dow  = DAY_JP[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
        w    = max(1, int(abs(pnl) / max_abs * 35))
        bar  = ("▪" if pnl >= 0 else "▫") * w
        sign = "+" if pnl >= 0 else ""
        print(f"  {date_str} {dow}  {sign}{pnl:>9,.0f}円  |{bar}")


# ── エントリーポイント ────────────────────────────────────────
if __name__ == "__main__":
    dates = trading_days(START_DATE, END_DATE)
    print(f"対象営業日候補: {len(dates)} 日（{START_DATE} 〜 {END_DATE}）")

    all_bars, source = load_bars(dates)

    result: dict[str, dict] = {}
    cash = float(INITIAL_CASH)

    print("バックテスト実行中...")
    for date_str in dates:
        if date_str not in all_bars:
            continue
        bars        = all_bars[date_str]
        trades, cash = backtest_day(bars, cash)
        result[date_str] = {
            "trades":     trades,
            "final_cash": cash,
            "n_bars":     len(bars),
        }
    print(f"完了: {len(result)} 営業日処理\n")

    report(result, source)
