"""
スイングトレード バックテスト【直近1ヶ月・現物取引版】
銘柄  : トヨタ自動車 (7203.T)
データ: yfinance 日足
戦略  : マルチシグナル（MA クロス + RSI + Bollinger Bands）

■ 仕組み:
  - ウォームアップ期間（WARMUP_DAYS）分の過去データでインジケーターを初期化
  - 直近1ヶ月（DAYS_AGO）のみ売買対象

■ 現物取引ルール:
  - 初期資金     : 100 万円
  - 売買単位     : 100 株（1 単元）
  - 取引株数     : 買付可能な最大単元数（上限 500 株）
  - ロングのみ（現物はショート不可）
  - タイムカット : MAX_HOLD_DAYS 営業日超過で強制決済
  - 手数料       : 0 円（ネット証券の無料プラン想定）
  - 信用・レバレッジなし

■ 実行方法:
  pip install yfinance pandas
  python swing_backtest_1month.py

⚠ 注意: バックテストは過去データに基づくものであり、将来の利益を保証しません。
"""

import math
from collections import deque
from datetime import datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL      = "7203.T"
END_DATE    = datetime.today().strftime("%Y-%m-%d")
DAYS_AGO    = 30                    # バックテスト対象期間（1ヶ月）
WARMUP_DAYS = 90                    # インジケーター初期化用の追加取得日数
START_DATE  = (datetime.strptime(END_DATE, "%Y-%m-%d")
               - timedelta(days=DAYS_AGO)).strftime("%Y-%m-%d")
FETCH_START = (datetime.strptime(END_DATE, "%Y-%m-%d")
               - timedelta(days=DAYS_AGO + WARMUP_DAYS)).strftime("%Y-%m-%d")
INTERVAL    = "1d"

# MA クロス（日足スイング用）
SHORT_PERIOD   = 5
LONG_PERIOD    = 20

# RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70

# Bollinger Bands
BB_PERIOD = 20
BB_K      = 2.0

# 現物取引パラメータ（デイトレと同設定）
INITIAL_CASH  = 1_000_000   # 100 万円
LOT_SIZE      = 100         # 1 単元 = 100 株
MAX_QTY       = 500         # 最大取引株数（5 単元）
MAX_HOLD_DAYS = 10          # 最大保有営業日数（1ヶ月内に収める）


# ── 取引株数計算 ──────────────────────────────────────────────
def calc_qty(cash: float, price: float) -> int:
    lots = min(int(cash / (price * LOT_SIZE)), MAX_QTY // LOT_SIZE)
    return lots * LOT_SIZE


# ── yfinance 日足取得 ─────────────────────────────────────────
def fetch_bars() -> tuple[list[dict], list[dict]]:
    """(ウォームアップ用バー, バックテスト対象バー) を返す"""
    import yfinance as yf
    import pandas as pd

    end_next = (datetime.strptime(END_DATE, "%Y-%m-%d")
                + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[yfinance] {SYMBOL}  {FETCH_START} 〜 {END_DATE}  interval={INTERVAL} を取得中"
          f"（ウォームアップ{WARMUP_DAYS}日 + 対象{DAYS_AGO}日）...")
    df = yf.download(SYMBOL, start=FETCH_START, end=end_next,
                     interval=INTERVAL, auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError("yfinance: データが空でした（ネット接続を確認してください）")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)

    all_bars = [
        {"dt":     row.Index.to_pydatetime(),
         "open":   float(row.Open),  "high":  float(row.High),
         "low":    float(row.Low),   "close": float(row.Close),
         "volume": int(row.Volume)}
        for row in df.itertuples()
    ]

    cutoff   = datetime.strptime(START_DATE, "%Y-%m-%d")
    warmup   = [b for b in all_bars if b["dt"] < cutoff]
    target   = [b for b in all_bars if b["dt"] >= cutoff]
    print(f"[yfinance] 取得成功: 全{len(all_bars)}本  "
          f"（ウォームアップ{len(warmup)}本 / 対象{len(target)}本）\n")
    return warmup, target


# ── インジケーター ────────────────────────────────────────────
class IndicatorEngine:
    def __init__(self):
        maxlen = max(LONG_PERIOD, BB_PERIOD, RSI_PERIOD + 1)
        self._prices: deque[float] = deque(maxlen=maxlen)
        self._gains:  deque[float] = deque(maxlen=RSI_PERIOD)
        self._losses: deque[float] = deque(maxlen=RSI_PERIOD)
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
        return {"ma_signal": ma_signal, "rsi": self._rsi(),
                "bb_lower": bb_lower, "bb_upper": bb_upper}


# ── シグナル判定 ──────────────────────────────────────────────
def long_entry(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "up":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi < RSI_OVERSOLD:
        return True
    bb_l = ind["bb_lower"]
    if bb_l is not None and price <= bb_l and (ind["rsi"] or 50) < 50:
        return True
    return False

def long_exit(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "down":
        return True
    rsi = ind["rsi"]
    if rsi is not None and rsi > RSI_OVERBOUGHT:
        return True
    bb_u = ind["bb_upper"]
    if bb_u is not None and price >= bb_u:
        return True
    return False


# ── バックテスト ──────────────────────────────────────────────
def backtest(warmup: list[dict], bars: list[dict]) -> tuple[list[dict], list[float]]:
    engine = IndicatorEngine()

    # ウォームアップ：インジケーターを育てるが売買しない
    for bar in warmup:
        engine.feed(bar["close"])

    in_pos    = False
    entry_p   = 0.0
    entry_dt  = None
    hold_days = 0
    qty       = 0
    cash      = float(INITIAL_CASH)
    trades    = []
    equity    = [cash]

    for bar in bars:
        price = bar["close"]
        dt    = bar["dt"]
        ind   = engine.feed(price)

        if in_pos:
            hold_days += 1
            exit_reason = None
            if hold_days >= MAX_HOLD_DAYS:
                exit_reason = f"タイムカット({MAX_HOLD_DAYS}日)"
            elif long_exit(ind, price):
                exit_reason = "シグナル"

            if exit_reason:
                pnl  = (price - entry_p) * qty
                cash += price * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     dt,
                    "entry_price": entry_p,
                    "exit_price":  price,
                    "qty":         qty,
                    "hold_days":   hold_days,
                    "pnl":         pnl,
                    "cash":        cash,
                    "note":        exit_reason,
                })
                in_pos = False
                equity.append(cash)

        if not in_pos and long_entry(ind, price):
            qty = calc_qty(cash, price)
            if qty > 0:
                cash     -= price * qty
                entry_p   = price
                entry_dt  = dt
                hold_days = 0
                in_pos    = True

    # 最終バーで未決済を強制クローズ
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
            "hold_days":   hold_days,
            "pnl":         pnl,
            "cash":        cash,
            "note":        "最終日強制決済",
        })
        equity.append(cash)

    return trades, equity


# ── レポート ──────────────────────────────────────────────────
DAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

def report(trades: list[dict], equity: list[float], n_bars: int):
    W     = 80
    final   = equity[-1] if equity else INITIAL_CASH
    total_p = final - INITIAL_CASH
    ret_pct = total_p / INITIAL_CASH * 100

    print("=" * W)
    print(f"  スイングトレード バックテスト【直近1ヶ月・現物取引版】  {SYMBOL} (トヨタ自動車)")
    print("=" * W)
    print(f"  期間       : {START_DATE} 〜 {END_DATE}（直近{DAYS_AGO}日）")
    print(f"  営業日数   : {n_bars} 日")
    print(f"  データ     : yfinance 日足")
    print(f"  戦略       : MA({SHORT_PERIOD}/{LONG_PERIOD}) + RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_K}σ)")
    print(f"  タイムカット: 最大 {MAX_HOLD_DAYS} 営業日")
    print(f"  取引形態   : 現物（ロングのみ）/ 最大{MAX_QTY}株・{LOT_SIZE}株単位")
    print(f"  初期資金   : {INITIAL_CASH:,.0f} 円")
    print("=" * W)

    # ── トレード一覧 ────────────────────────────────────────
    if not trades:
        print("\n  ※ 期間内にトレードは発生しませんでした（データ不足の可能性あり）")
    else:
        print(f"\n{'#':>3}  {'エントリー':12}  {'エグジット':12}  "
              f"{'保有':>4}  {'株数':>4}  {'取得値':>7}  {'決済値':>7}  "
              f"{'損益':>10}  {'資金残高':>13}  メモ")
        print("-" * W)
        for i, t in enumerate(trades, 1):
            e_dow = DAY_JP[t["entry_dt"].weekday()]
            x_dow = DAY_JP[t["exit_dt"].weekday()]
            sign  = "+" if t["pnl"] >= 0 else ""
            print(
                f"  {i:>2}  "
                f"{t['entry_dt'].strftime('%m/%d')}({e_dow})        "
                f"{t['exit_dt'].strftime('%m/%d')}({x_dow})        "
                f"{t['hold_days']:>3}日  "
                f"{t['qty']:>4}株  "
                f"{t['entry_price']:>7,.0f}  "
                f"{t['exit_price']:>7,.0f}  "
                f"{sign}{t['pnl']:>9,.0f}円  "
                f"{t['cash']:>13,.0f}円  "
                f"{t['note']}"
            )

    # ── サマリー ─────────────────────────────────────────────
    n      = len(trades)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_r  = len(wins) / n * 100 if n else 0.0
    avg_w  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_l  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0
    payoff = abs(avg_w / avg_l) if avg_l else float("inf")
    avg_h  = sum(t["hold_days"] for t in trades) / n if n else 0.0

    peak = equity[0]; max_dd = 0.0
    for v in equity:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    streak_w = streak_l = cur_w = cur_l = 0
    for t in trades:
        if t["pnl"] > 0:
            cur_w += 1; cur_l = 0; streak_w = max(streak_w, cur_w)
        else:
            cur_l += 1; cur_w = 0; streak_l = max(streak_l, cur_l)

    print("\n" + "=" * W)
    print(f"  【直近1ヶ月サマリー】（スイングトレード・現物）")
    print("=" * W)
    print(f"  初期資金             : {INITIAL_CASH:>14,.0f} 円")
    print(f"  最終資金残高         : {final:>14,.0f} 円")
    print(f"  期間損益             : {total_p:>+14,.0f} 円")
    print(f"  資金リターン         : {ret_pct:>+13.2f} %")
    print("-" * W)
    print(f"  総トレード数         : {n:>14} 回")
    print(f"  勝ち / 負け          : {len(wins):>6} 勝 / {len(losses)} 敗")
    print(f"  勝率                 : {win_r:>13.1f} %")
    print(f"  平均利益             : {avg_w:>+14,.0f} 円")
    print(f"  平均損失             : {avg_l:>+14,.0f} 円")
    print(f"  ペイオフ比           : {payoff:>14.2f}")
    print(f"  平均保有日数         : {avg_h:>13.1f} 日")
    print(f"  最大ドローダウン     : {max_dd:>+13.2f} %")
    print(f"  最大連続勝ち         : {streak_w:>14} 回")
    print(f"  最大連続負け         : {streak_l:>14} 回")
    print("=" * W)

    # ── 損益バーチャート ─────────────────────────────────────
    if trades:
        max_abs = max(abs(t["pnl"]) for t in trades) or 1
        print(f"\n--- トレード別損益チャート ---")
        for i, t in enumerate(trades, 1):
            w    = max(1, int(abs(t["pnl"]) / max_abs * 35))
            bar  = ("▪" if t["pnl"] >= 0 else "▫") * w
            sign = "+" if t["pnl"] >= 0 else ""
            print(f"  #{i:>2} {t['entry_dt'].strftime('%m/%d')}→"
                  f"{t['exit_dt'].strftime('%m/%d')}  "
                  f"({t['hold_days']:>2}日)  "
                  f"{sign}{t['pnl']:>9,.0f}円  |{bar}")


# ── エントリーポイント ────────────────────────────────────────
if __name__ == "__main__":
    warmup, bars = fetch_bars()
    print(f"バックテスト実行中... ({len(bars)} 営業日)\n")
    trades, equity = backtest(warmup, bars)
    print(f"完了: {len(trades)} トレード\n")
    report(trades, equity, len(bars))
