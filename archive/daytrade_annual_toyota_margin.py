"""
年次デイトレ バックテスト【信用取引版・高レバレッジ】— 直近 1 年間
銘柄  : トヨタ自動車 (7203.T)
データ: yfinance 5 分足を 59 日チャンクで取得（ネット遮断時は合成データ）
        ※ yfinance は 1 分足が直近 7 日まで / 5 分足は直近 60 日まで
        　 1 年分は 59 日ずつ分割取得し結合する

戦略  : マルチシグナル（MA クロス + RSI + Bollinger Bands）
        ※ 週次版と同一条件。5 分足なので MAX_HOLD_BARS=6（6×5分=30分）

■ 信用取引ルール（週次版と同一）:
  - 初期委託保証金 : 1,000 万円
  - 委託保証金率   : 10%  → 実質レバレッジ 10 倍
  - 維持率         : 5% 未満で強制ロスカット
  - ロング / ショート 両対応
  - 日中決済（金利・貸株料 0 円）
  - 引けまでに未決済なら強制決済

⚠ 注意: 高レバレッジは利益と損失が共に拡大します。
         実際の取引では証券会社の規約・規制を必ず確認してください。
"""

import math
import random
from collections import deque
from datetime import date, datetime, timedelta

# ── パラメータ ──────────────────────────────────────────────
SYMBOL     = "7203.T"
YEAR_START = "2025-03-21"   # 直近 1 年の開始日
YEAR_END   = "2026-03-21"   # 本日
INTERVAL   = "5m"           # 5 分足（yfinance: 直近 60 日まで対応）
CHUNK_DAYS = 59             # 1 回の取得日数

# MA クロス（週次版と同じ）
SHORT_PERIOD   = 3
LONG_PERIOD    = 10

# RSI
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 68

# Bollinger Bands
BB_PERIOD = 20
BB_K      = 2.0

# 信用取引パラメータ（週次版と同じ）
INITIAL_MARGIN   = 10_000_000   # 1,000 万円
MARGIN_RATE      = 0.10         # 10% → レバレッジ 10 倍
MAINTENANCE_RATE = 0.05         # 5% 未満でロスカット
QTY              = 3_000        # 1 回取引株数
MAX_HOLD_BARS    = 6            # 6 本 × 5 分 = 30 分タイムカット


# ── 営業日リスト生成（土日除外のみ、祝日はデータなし時スキップ） ──
def trading_days(start: str, end: str) -> list[str]:
    base  = datetime.strptime(start, "%Y-%m-%d").date()
    stop  = datetime.strptime(end,   "%Y-%m-%d").date()
    days  = []
    cur   = base
    while cur <= stop:
        if cur.weekday() < 5:   # 月〜金
            days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


# ── 保証金チェック / 維持率計算 ──────────────────────────────
def can_open(margin_cash: float, price: float) -> bool:
    return margin_cash >= price * QTY * MARGIN_RATE


def calc_maintenance(margin_cash: float, price: float,
                     entry_price: float, direction: str) -> float:
    unreal = (price - entry_price) if direction == "long" else (entry_price - price)
    pos_val = price * QTY
    return (margin_cash + unreal * QTY) / pos_val * 100 if pos_val else 0.0


# ── 1. yfinance チャンク取得 ─────────────────────────────────
def fetch_all_yfinance(all_dates: list[str]) -> dict[str, list[dict]]:
    import yfinance as yf
    import pandas as pd

    # 59 日ごとに分割
    starts = []
    i = 0
    while i < len(all_dates):
        starts.append(all_dates[i])
        i += CHUNK_DAYS

    result: dict[str, list[dict]] = {}
    date_set = set(all_dates)

    for chunk_start in starts:
        idx   = all_dates.index(chunk_start)
        chunk = all_dates[idx: idx + CHUNK_DAYS]
        s     = chunk[0]
        e     = (datetime.strptime(chunk[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

        print(f"  [yfinance] チャンク取得: {s} 〜 {chunk[-1]}", end="  ", flush=True)
        try:
            df = yf.download(SYMBOL, start=s, end=e, interval=INTERVAL,
                             auto_adjust=True, progress=False)
        except Exception as ex:
            print(f"→ 失敗({ex}), スキップ")
            continue

        if df.empty:
            print("→ データなし")
            continue

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)

        n_new = 0
        for d_str in chunk:
            if d_str not in date_set:
                continue
            day = datetime.strptime(d_str, "%Y-%m-%d").date()
            sub = df[df.index.date == day]
            if sub.empty:
                continue
            result[d_str] = [
                {"dt":     row.Index.to_pydatetime(),
                 "open":   float(row.Open),
                 "high":   float(row.High),
                 "low":    float(row.Low),
                 "close":  float(row.Close),
                 "volume": int(row.Volume)}
                for row in sub.itertuples()
            ]
            n_new += 1
        print(f"→ {n_new} 日取得")

    if not result:
        raise RuntimeError("yfinance: 全チャンクでデータ取得失敗")
    return result


# ── 2. 合成データ（フォールバック） ──────────────────────────
def generate_day_bars_5m(date_str: str, open_price: float, seed: int) -> list[dict]:
    """5 分足の合成バーを生成"""
    rng  = random.Random(seed)
    y, m, d = map(int, date_str.split("-"))
    sessions = [
        (datetime(y, m, d,  9,  0), datetime(y, m, d, 11, 30)),
        (datetime(y, m, d, 12, 30), datetime(y, m, d, 15, 30)),
    ]
    price = open_price
    bars  = []
    for (start, end) in sessions:
        dt = start
        while dt < end:
            change = rng.gauss(0.1, 8.0)   # 5 分足なので変動大きめ
            if dt.hour == 9 and dt.minute < 15:  change *= 2.0
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


def generate_annual_synthetic(dates: list[str]) -> dict[str, list[dict]]:
    result = {}
    price  = 3_560.0
    for i, d in enumerate(dates):
        seed = int(d.replace("-", "")) + i * 7
        bars = generate_day_bars_5m(d, price, seed)
        if bars:
            price = bars[-1]["close"]
            result[d] = bars
    return result


def load_all_bars(all_dates: list[str]) -> tuple[dict[str, list[dict]], str]:
    print(f"\n[データ取得] {SYMBOL}  {YEAR_START} 〜 {YEAR_END}  interval={INTERVAL}")
    try:
        data   = fetch_all_yfinance(all_dates)
        source = f"yfinance ({SYMBOL} {INTERVAL}, チャンク取得)"
    except Exception as e:
        print(f"[警告] yfinance 全チャンク失敗: {e}")
        print("[フォールバック] 合成データを使用\n")
        data   = generate_annual_synthetic(all_dates)
        source = "合成データ (seed 固定, 5 分足)"
    total = sum(len(v) for v in data.values())
    print(f"[完了] {len(data)} 営業日 / {total:,} 本\n")
    return data, source


# ── 3. インジケーター ─────────────────────────────────────────
class IndicatorEngine:
    def __init__(self):
        maxlen = max(LONG_PERIOD, BB_PERIOD, RSI_PERIOD + 1)
        self._prices: deque[float] = deque(maxlen=maxlen)
        self._prev_sig: str | None = None
        self._prev_close: float | None = None
        self._gains:  deque[float] = deque(maxlen=RSI_PERIOD)
        self._losses: deque[float] = deque(maxlen=RSI_PERIOD)

    def reset(self):
        self._prices.clear()
        self._prev_sig   = None
        self._prev_close = None
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
        return {"ma_signal": ma_signal, "rsi": self._rsi(),
                "bb_lower": bb_lower, "bb_upper": bb_upper}


# ── 4. シグナル判定 ───────────────────────────────────────────
def long_entry(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "up": return True
    rsi = ind["rsi"]
    if rsi is not None and rsi < RSI_OVERSOLD: return True
    bb_l = ind["bb_lower"]
    if bb_l is not None and price <= bb_l and (ind["rsi"] or 50) < 50: return True
    return False

def short_entry(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "down": return True
    rsi = ind["rsi"]
    if rsi is not None and rsi > RSI_OVERBOUGHT: return True
    bb_u = ind["bb_upper"]
    if bb_u is not None and price >= bb_u and (ind["rsi"] or 50) > 50: return True
    return False

def long_exit(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "down": return True
    rsi = ind["rsi"]
    if rsi is not None and rsi > RSI_OVERBOUGHT: return True
    bb_u = ind["bb_upper"]
    if bb_u is not None and price >= bb_u: return True
    return False

def short_exit(ind: dict, price: float) -> bool:
    if ind["ma_signal"] == "up": return True
    rsi = ind["rsi"]
    if rsi is not None and rsi < RSI_OVERSOLD: return True
    bb_l = ind["bb_lower"]
    if bb_l is not None and price <= bb_l: return True
    return False


# ── 5. 1 日分バックテスト ────────────────────────────────────
def backtest_day(bars: list[dict], margin_cash: float) -> tuple[list[dict], float]:
    engine    = IndicatorEngine()
    direction: str | None = None
    entry_p   = None
    entry_dt  = None
    hold_bars = 0
    trades    = []

    for bar in bars:
        price = bar["close"]
        dt    = bar["dt"]
        ind   = engine.feed(price)

        if direction is not None:
            hold_bars += 1
            exit_reason = None
            ratio = calc_maintenance(margin_cash, price, entry_p, direction)
            if ratio < MAINTENANCE_RATE * 100:
                exit_reason = f"ロスカット(維持率{ratio:.1f}%)"
            elif hold_bars >= MAX_HOLD_BARS:
                exit_reason = f"タイムカット({MAX_HOLD_BARS*5}分)"
            elif direction == "long"  and long_exit(ind, price):
                exit_reason = "シグナル(L)"
            elif direction == "short" and short_exit(ind, price):
                exit_reason = "シグナル(S)"

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
                direction = entry_p = entry_dt = None
                hold_bars = 0

        if direction is None:
            if long_entry(ind, price) and can_open(margin_cash, price):
                direction, entry_p, entry_dt, hold_bars = "long", price, dt, 0
            elif short_entry(ind, price) and can_open(margin_cash, price):
                direction, entry_p, entry_dt, hold_bars = "short", price, dt, 0

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
MONTH_JP = ["1月", "2月", "3月", "4月", "5月", "6月",
            "7月", "8月", "9月", "10月", "11月", "12月"]

def report(annual_result: dict, source: str):
    W   = 76
    lev = 1 / MARGIN_RATE
    print("\n" + "=" * W)
    print(f"  年次デイトレ バックテスト【信用取引版】  {YEAR_START} 〜 {YEAR_END}  {SYMBOL}")
    print("=" * W)
    print(f"  データ      : {source}")
    print(f"  戦略        : MA({SHORT_PERIOD}/{LONG_PERIOD}) + RSI({RSI_PERIOD}) + BB({BB_PERIOD},{BB_K}σ)")
    print(f"  足種        : {INTERVAL}  /  タイムカット {MAX_HOLD_BARS}本({MAX_HOLD_BARS*5}分)")
    print(f"  委託保証金率: {MARGIN_RATE*100:.0f}%  /  レバレッジ: {lev:.0f} 倍")
    print(f"  維持率下限  : {MAINTENANCE_RATE*100:.0f}% 未満で強制ロスカット")
    print(f"  初期保証金  : {INITIAL_MARGIN:,.0f} 円  /  取引株数 {QTY:,} 株")
    print("=" * W)

    all_trades:  list[dict] = []
    monthly:     dict[str, dict] = {}  # "YYYY-MM" → 集計dict
    prev_margin  = float(INITIAL_MARGIN)
    prev_values  = [INITIAL_MARGIN]

    # ── 日別ループ ─────────────────────────────────────────────
    for date_str, info in annual_result.items():
        trades  = info["trades"]
        margin  = info["final_margin"]
        day_pnl = margin - prev_margin
        ym      = date_str[:7]   # "YYYY-MM"

        if ym not in monthly:
            monthly[ym] = {
                "trades": [], "start_margin": prev_margin,
                "end_margin": margin, "trading_days": 0,
            }
        monthly[ym]["trades"].extend(trades)
        monthly[ym]["end_margin"]  = margin
        monthly[ym]["trading_days"] += 1

        all_trades.extend(trades)
        for t in trades:
            prev_values.append(t["margin"])
        prev_margin = margin

    # ── 月別サマリー ───────────────────────────────────────────
    print(f"\n{'月':>7}  {'営業日':>4}  {'取引':>4}  {'勝率':>5}  "
          f"{'月間損益':>12}  {'保証金残高':>14}")
    print("-" * W)
    for ym, m in monthly.items():
        trd    = m["trades"]
        n_days = m["trading_days"]
        wins   = [t for t in trd if t["pnl"] > 0]
        wr     = len(wins) / len(trd) * 100 if trd else 0.0
        mpnl   = m["end_margin"] - m["start_margin"]
        sign   = "+" if mpnl >= 0 else ""
        y, mo  = ym.split("-")
        label  = f"{y}/{MONTH_JP[int(mo)-1]}"
        print(f"  {label:>8}  {n_days:>4}日  {len(trd):>4}回  "
              f"{wr:>4.0f}%  {sign}{mpnl:>12,.0f}円  "
              f"{m['end_margin']:>14,.0f}円")

    # ── 年間サマリー ───────────────────────────────────────────
    final   = prev_margin
    total_p = final - INITIAL_MARGIN
    ret_pct = total_p / INITIAL_MARGIN * 100
    eff_ret = total_p / (INITIAL_MARGIN * lev) * 100

    all_wins   = [t for t in all_trades if t["pnl"] > 0]
    all_losses = [t for t in all_trades if t["pnl"] <= 0]
    n_all      = len(all_trades)
    n_long     = sum(1 for t in all_trades if t["direction"] == "long")
    n_short    = sum(1 for t in all_trades if t["direction"] == "short")
    win_r      = len(all_wins) / n_all * 100 if all_trades else 0.0
    avg_w      = sum(t["pnl"] for t in all_wins)   / len(all_wins)   if all_wins   else 0.0
    avg_l      = sum(t["pnl"] for t in all_losses) / len(all_losses) if all_losses else 0.0
    payoff     = abs(avg_w / avg_l) if avg_l else float("inf")

    # 最大ドローダウン
    peak = prev_values[0]; max_dd = 0.0
    for v in prev_values:
        peak   = max(peak, v)
        max_dd = min(max_dd, (v - peak) / peak * 100)

    # 連続勝ち/負け
    streak_w = streak_l = cur_w = cur_l = 0
    for t in all_trades:
        if t["pnl"] > 0:
            cur_w += 1; cur_l = 0
            streak_w = max(streak_w, cur_w)
        else:
            cur_l += 1; cur_w = 0
            streak_l = max(streak_l, cur_l)

    n_days_traded = len(annual_result)
    avg_trades_day = n_all / n_days_traded if n_days_traded else 0

    print("\n" + "=" * W)
    print("  【年間サマリー】（信用取引・デイトレ）")
    print("=" * W)
    print(f"  初期保証金           : {INITIAL_MARGIN:>14,.0f} 円")
    print(f"  最終保証金残高       : {final:>14,.0f} 円")
    print(f"  年間損益             : {total_p:>+14,.0f} 円")
    print(f"  保証金リターン       : {ret_pct:>+13.2f} %")
    print(f"  実運用額リターン     : {eff_ret:>+13.2f} %  (÷{lev:.0f}倍)")
    print("-" * W)
    print(f"  営業日数             : {n_days_traded:>14} 日")
    print(f"  総トレード数         : {n_all:>14} 回")
    print(f"  1日平均トレード数    : {avg_trades_day:>14.1f} 回")
    print(f"  うちロング(L)        : {n_long:>14} 回")
    print(f"  うちショート(S)      : {n_short:>14} 回")
    print(f"  勝ち / 負け          : {len(all_wins):>6} 勝 / {len(all_losses)} 敗")
    print(f"  年間勝率             : {win_r:>13.1f} %")
    print(f"  平均利益             : {avg_w:>+14,.0f} 円")
    print(f"  平均損失             : {avg_l:>+14,.0f} 円")
    print(f"  ペイオフ比           : {payoff:>14.2f}")
    print(f"  最大ドローダウン     : {max_dd:>+13.2f} %")
    print(f"  最大連続勝ち         : {streak_w:>14} 回")
    print(f"  最大連続負け         : {streak_l:>14} 回")
    print("=" * W)

    # 月別損益バーチャート
    print("\n--- 月別損益チャート ---")
    mpnls   = [(ym, m["end_margin"] - m["start_margin"]) for ym, m in monthly.items()]
    max_abs = max(abs(p) for _, p in mpnls) or 1
    for ym, pnl in mpnls:
        w    = int(abs(pnl) / max_abs * 35)
        bar  = ("▪" if pnl >= 0 else "▫") * w
        sign = "+" if pnl >= 0 else ""
        y, mo = ym.split("-")
        print(f"  {y}/{MONTH_JP[int(mo)-1]:>4}  {sign}{pnl:>12,.0f}円  |{bar}")


# ── 7. エントリーポイント ────────────────────────────────────
if __name__ == "__main__":
    all_dates = trading_days(YEAR_START, YEAR_END)
    print(f"対象営業日: {len(all_dates)} 日（{YEAR_START} 〜 {YEAR_END}）")

    all_bars, source = load_all_bars(all_dates)

    annual_result: dict[str, dict] = {}
    margin_cash = float(INITIAL_MARGIN)

    print("バックテスト実行中...")
    for date_str in all_dates:
        if date_str not in all_bars:
            continue   # 祝日・データなしはスキップ
        bars             = all_bars[date_str]
        trades, margin_cash = backtest_day(bars, margin_cash)
        annual_result[date_str] = {
            "trades":       trades,
            "final_margin": margin_cash,
            "n_bars":       len(bars),
        }

    report(annual_result, source)
