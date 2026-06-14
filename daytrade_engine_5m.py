"""
daytrade_engine_5m.py  ―  5分足デイトレ共通バックテストエンジン
==================================================================
逆指値ロング (backtest_limit_entry.py) を5分足デイトレ用に移植。

【特徴】
- スリッページ・手数料反映 (実運用想定)
- 任意の戦略 (DON/MACD/RSI2/A7/VOL/MOM) を受け取り
- ATR×em/sm/tm の逆指値ロジック (entry/stop/target)
- 引け強制決済 (14:55)
- トレード単位の集計

【使い方】
  from daytrade_engine_5m import backtest_symbol_5m
  from daytrade_strategies_5m import STRATEGIES

  result = backtest_symbol_5m(
      sym="7203.T", name="トヨタ", df=df,
      strategy_fn=STRATEGIES["MACD"],
      strategy_params={"em": 0.0, "sm": 1.5, "tm": 3.0},
  )
"""

from __future__ import annotations

from datetime import time as dtime, timedelta

import numpy as np
import pandas as pd

from daytrade_data import split_by_day, calc_position_size
from daytrade_strategies_5m import (
    ENTRY_START, ENTRY_CUTOFF, FORCE_CLOSE, WARMUP_BARS,
    atr_from_bars,
)

# ── 実運用コスト ─────────────────────────────────────────────
# デイトレ信用 (auカブコム/SBI 信用デイトレ) は手数料0円
# 5分足は1分足より流動性高くスリッページも小さい
SLIPPAGE_STOP_PCT = 0.001   # 0.1% (5分足デイトレ・高流動性想定)
FEE_PCT_ONE_WAY   = 0.0     # 信用デイトレは手数料無料 (現物は 0.001)

# ── 改善オプション (ENV VAR で制御) ──────────────────────────
# 全て 0=無効 / 1=有効 (or 整数値)
import os as _os
# 1. Slow Stop: stop到達からN本連続で stop の外にある場合のみ決済 (0=即決済)
STOP_CONFIRM_BARS = int(_os.environ.get("DAYTRADE_STOP_CONFIRM", "0"))
# 2. 同日損切上限: その日 N 回 stop に当たったら、以降のシグナルを無視 (0=無制限)
MAX_DAILY_STOPS = int(_os.environ.get("DAYTRADE_MAX_STOPS", "0"))
# 3. ブレークイーブントレール: +1×ATR 利益乗ったら stop を建値に移動 (0=無効, 1=有効)
TRAIL_TO_BREAKEVEN = int(_os.environ.get("DAYTRADE_TRAIL_BE", "0"))
# 4. 戦略別エントリー時刻フィルタ (ENV: "MOM_S:0930-1030,RSI2_S:1000-1300")
_strat_filter_env = _os.environ.get("DAYTRADE_STRAT_TIMES", "")
STRATEGY_TIME_FILTERS = {}
if _strat_filter_env:
    for part in _strat_filter_env.split(","):
        if ":" in part:
            n, r = part.split(":", 1)
            if "-" in r:
                s, e = r.split("-")
                STRATEGY_TIME_FILTERS[n.strip()] = (
                    dtime(int(s[:2]), int(s[2:])),
                    dtime(int(e[:2]), int(e[2:])),
                )

# 5. 出来高ブースト要求 (例: 1.5 で 20本平均の1.5倍以上の出来高が必要)
MIN_VOL_RATIO = float(_os.environ.get("DAYTRADE_MIN_VOL_RATIO", "0"))
# 6. ATR % レンジ (低ボラ/高ボラ 排除)
MIN_ATR_PCT = float(_os.environ.get("DAYTRADE_MIN_ATR_PCT", "0"))
MAX_ATR_PCT = float(_os.environ.get("DAYTRADE_MAX_ATR_PCT", "0"))  # 0=無制限
# 7. 連敗停止: 連続損切N回で当該銘柄を pause_days 日休止
PAUSE_AFTER_LOSSES = int(_os.environ.get("DAYTRADE_PAUSE_LOSSES", "0"))
PAUSE_DAYS = int(_os.environ.get("DAYTRADE_PAUSE_DAYS", "3"))
# 8. リスクリワード(RR)最低基準 (sm < RR_MIN なら そもそも入らない)
MIN_RR = float(_os.environ.get("DAYTRADE_MIN_RR", "0"))


def _apply_slippage_buy(price):
    """買い約定時の不利スリッページ。"""
    return price * (1 + SLIPPAGE_STOP_PCT)


def _apply_fee(amount):
    """手数料控除 (絶対値)。"""
    return amount * FEE_PCT_ONE_WAY


# 案A: 前日末尾を当日 warmup に流用するためのバー数
# 各戦略の最大ルックバック (A7 MA30=150min, MACD 17+5=110min) +余裕
# 60バー (300分=5時間) あれば全戦略カバー
PREV_TAIL_BARS = 60


def backtest_day_strategy(day_df, strategy_fn, strategy_params,
                            budget, max_risk,
                            atr_period=14,
                            prev_tail_df=None,
                            persist_state=None):
    """1日分の5分足DFで指定戦略をbacktest。

    prev_tail_df: 前日末尾の数十バー (warmup用)。指定時、指標は連結データで
                  計算し当日0バー目から発火可能。エントリーは当日のみ。
    """
    # 前日末尾と連結 (warmup 用)
    if prev_tail_df is not None and len(prev_tail_df) > 0:
        ext_df = pd.concat([prev_tail_df, day_df])
        day_start = len(prev_tail_df)
    else:
        ext_df = day_df
        day_start = 0

    opens   = ext_df["open"].to_numpy(dtype=float)
    highs   = ext_df["high"].to_numpy(dtype=float)
    lows    = ext_df["low"].to_numpy(dtype=float)
    closes  = ext_df["close"].to_numpy(dtype=float)
    volumes = ext_df["volume"].to_numpy(dtype=float)
    times   = ext_df.index
    n = len(ext_df)

    # 当日が短すぎる場合スキップ
    if (n - day_start) < 3:
        return []
    # 前日無し + 当日のみで warmup 不足
    if day_start == 0 and n < WARMUP_BARS + 2:
        return []

    # ATR を連結データで計算
    atr_arr = atr_from_bars(highs, lows, closes, atr_period)

    trades = []
    state = "idle"
    side = "long"   # "long" or "short"
    entry_p = stop_p = target_p = 0.0
    entry_dt = None
    qty = 0

    def _finish(exit_p, exit_dt, reason):
        # 異常値ガード: entry/exit の比率が ±30% 超なら無効化 (株式分割等のデータ破損)
        if entry_p > 0:
            ratio = exit_p / entry_p
            if ratio < 0.7 or ratio > 1.3:
                return  # 異常値はトレード記録しない
        # スリッページ・手数料反映 (Long: 買→売, Short: 売→買戻し)
        if side == "long":
            gross = (exit_p - entry_p) * qty
        else:
            gross = (entry_p - exit_p) * qty
        fees = _apply_fee(entry_p * qty) + _apply_fee(exit_p * qty)
        pnl = gross - fees
        pct = abs((exit_p - entry_p) / entry_p * 100) if entry_p > 0 else 0
        if side == "short" and exit_p > entry_p:
            pct = -pct
        elif side == "long" and exit_p < entry_p:
            pct = -pct
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=exit_dt,
            entry_p=entry_p, exit_p=exit_p,
            stop_p=stop_p, target_p=target_p,
            qty=qty, pnl=pnl, pct=pct, side=side,
            strategy=strategy_params.get("name", "?"), reason=reason,
        ))
        # 改善7: 連敗カウントと pause 判定 (persist_state 経由で銘柄跨ぎ管理)
        if PAUSE_AFTER_LOSSES > 0 and persist_state is not None:
            if pnl <= 0:
                persist_state["consec_losses"] = persist_state.get("consec_losses", 0) + 1
                if persist_state["consec_losses"] >= PAUSE_AFTER_LOSSES:
                    persist_state["pause_until"] = (exit_dt.date()
                                                     + timedelta(days=PAUSE_DAYS))
                    persist_state["consec_losses"] = 0
            else:
                persist_state["consec_losses"] = 0

    # 改善2: 同日損切カウント
    day_stop_count = 0
    last_date = None
    # 改善1: Slow Stop 用
    stop_hit_bars = 0
    # 改善3: BEトレール用
    trailed_to_be = False
    # 改善4: 戦略別時刻フィルタ
    strat_window = STRATEGY_TIME_FILTERS.get(strategy_params.get("name", ""))
    # entry時のATR (建値トレールに必要)
    entry_atr = 0.0
    entry_sm = 0.0

    # prev_tail があれば当日 0 バー目から、無ければ WARMUP_BARS バー目から
    i = day_start if day_start > 0 else WARMUP_BARS
    while i < n:
        t = times[i].time()
        # 日付変更で daily カウンタリセット
        cur_date = times[i].date()
        if cur_date != last_date:
            day_stop_count = 0
            last_date = cur_date

        if state == "in_pos":
            if t >= FORCE_CLOSE:
                _finish(closes[i], times[i], "引け強制")
                break
            # 改善3: +1×ATR 利益乗ったら stop を建値に移動
            if TRAIL_TO_BREAKEVEN and not trailed_to_be and entry_atr > 0:
                if side == "long" and highs[i] >= entry_p + entry_atr:
                    stop_p = entry_p   # 建値へ移動 (損切ゼロ)
                    trailed_to_be = True
                elif side == "short" and lows[i] <= entry_p - entry_atr:
                    stop_p = entry_p
                    trailed_to_be = True

            if side == "long":
                # 目標 (上) 達成優先
                if highs[i] >= target_p:
                    _finish(target_p, times[i], "目標達成")
                    state = "idle"
                    stop_hit_bars = 0
                    trailed_to_be = False
                    i += 1
                    continue
                # 改善1: Slow Stop - stop超えバーが連続したら決済
                if lows[i] <= stop_p:
                    if STOP_CONFIRM_BARS > 0:
                        # close が stop の外にある場合のみカウント
                        if closes[i] <= stop_p:
                            stop_hit_bars += 1
                            if stop_hit_bars >= STOP_CONFIRM_BARS:
                                _finish(stop_p, times[i], "損切り")
                                day_stop_count += 1
                                state = "idle"
                                stop_hit_bars = 0
                                trailed_to_be = False
                                i += 1
                                continue
                        # close が建値方向に戻った場合はカウントリセット
                        else:
                            stop_hit_bars = 0
                    else:
                        _finish(stop_p, times[i], "損切り")
                        day_stop_count += 1
                        state = "idle"
                        trailed_to_be = False
                        i += 1
                        continue
                else:
                    # stop を再び抜けた = カウントリセット
                    stop_hit_bars = 0
            else:  # short
                if lows[i] <= target_p:
                    _finish(target_p, times[i], "目標達成")
                    state = "idle"
                    stop_hit_bars = 0
                    trailed_to_be = False
                    i += 1
                    continue
                if highs[i] >= stop_p:
                    if STOP_CONFIRM_BARS > 0:
                        if closes[i] >= stop_p:
                            stop_hit_bars += 1
                            if stop_hit_bars >= STOP_CONFIRM_BARS:
                                _finish(stop_p, times[i], "損切り")
                                day_stop_count += 1
                                state = "idle"
                                stop_hit_bars = 0
                                trailed_to_be = False
                                i += 1
                                continue
                        else:
                            stop_hit_bars = 0
                    else:
                        _finish(stop_p, times[i], "損切り")
                        day_stop_count += 1
                        state = "idle"
                        trailed_to_be = False
                        i += 1
                        continue
                else:
                    stop_hit_bars = 0
            i += 1
            continue

        # idle: 時刻フィルタ (グローバル)
        if t < ENTRY_START or t >= ENTRY_CUTOFF:
            i += 1
            continue
        # 改善4: 戦略別時刻フィルタ
        if strat_window is not None:
            ws, we = strat_window
            if t < ws or t >= we:
                i += 1
                continue
        # 改善2: 同日損切上限を超えたら以降エントリーしない
        if MAX_DAILY_STOPS > 0 and day_stop_count >= MAX_DAILY_STOPS:
            i += 1
            continue
        # 改善6: ATR % が範囲外なら入らない (低ボラ/高ボラ除外)
        if MIN_ATR_PCT > 0 or MAX_ATR_PCT > 0:
            cp = closes[i]
            atr_pct = (atr_arr[i] / cp * 100) if cp > 0 else 0
            if MIN_ATR_PCT > 0 and atr_pct < MIN_ATR_PCT:
                i += 1
                continue
            if MAX_ATR_PCT > 0 and atr_pct > MAX_ATR_PCT:
                i += 1
                continue
        # 改善5: 出来高ブースト要求
        if MIN_VOL_RATIO > 0:
            v_avg = volumes[max(0, i-20):i].mean()
            if v_avg > 0 and volumes[i] < v_avg * MIN_VOL_RATIO:
                i += 1
                continue
        # 改善7: pause_until 日まで休止
        if (persist_state is not None
                and persist_state.get("pause_until")
                and cur_date < persist_state["pause_until"]):
            i += 1
            continue

        # シグナル判定
        try:
            sig = strategy_fn(opens, highs, lows, closes, volumes,
                              i, atr_arr, **{k: v for k, v in
                              strategy_params.items() if k != "name"})
        except Exception:
            sig = None

        if sig is None:
            i += 1
            continue

        action, atr_val, em, sm, tm = sig
        if i + 1 >= n:
            break

        # 翌バー寄付で entry
        next_open = opens[i + 1]

        if action == "entry":
            # 買い (long): order_p = next_open + ATR*em (em=0で寄付)
            order_p = next_open + atr_val * em
            if highs[i + 1] < order_p:
                i += 1
                continue
            entry_p_raw = max(order_p, opens[i + 1])
            entry_p = _apply_slippage_buy(entry_p_raw)
            stop_p = entry_p - atr_val * sm
            target_p = entry_p + atr_val * tm
            side = "long"
        elif action == "entry_short":
            # 空売り (short): order_p = next_open - ATR*em
            order_p = next_open - atr_val * em
            if lows[i + 1] > order_p:
                i += 1
                continue
            entry_p_raw = min(order_p, opens[i + 1])
            # 空売り側のスリッページ (約定価格が不利に下がる)
            entry_p = entry_p_raw * (1 - SLIPPAGE_STOP_PCT)
            stop_p = entry_p + atr_val * sm
            target_p = entry_p - atr_val * tm
            side = "short"
        else:
            i += 1
            continue

        entry_dt = times[i + 1]

        # 株数計算 (long/short どちらも同様)
        risk_per_share = abs(entry_p - stop_p)
        qty = calc_position_size(entry_p, stop_p, budget, max_risk)
        if qty <= 0:
            i += 1
            continue

        state = "in_pos"
        # 改善1/3用: エントリー時のATRと sm を保存
        entry_atr = atr_val
        entry_sm = sm
        stop_hit_bars = 0
        trailed_to_be = False
        i += 2

    if state == "in_pos":
        _finish(closes[-1], times[-1], "引け強制")

    return trades


def backtest_symbol_5m(sym, name, df, strategy_fn, strategy_params=None,
                       budget=200_000, max_risk=1_000):
    """全期間を1日単位で backtest。

    案A: 前日末尾を翌日 warmup に流用 → 寄付き直後 (9:00 など) から
    指標を計算可能にし、ENTRY_START を早めても実エントリーが発生する。
    """
    if strategy_params is None:
        strategy_params = {}
    if "name" not in strategy_params:
        strategy_params["name"] = strategy_fn.__name__.replace("signal_", "").upper()

    daily = split_by_day(df)
    dates = sorted(daily.keys())
    if len(dates) < 2:
        return None

    trades = []
    prev_tail = None  # 前日末尾の PREV_TAIL_BARS バー
    # 改善7: 銘柄単位の連敗状態 (日跨ぎ管理)
    persist_state = {"consec_losses": 0, "pause_until": None}
    for d in dates:
        day_df = daily[d]
        day_trades = backtest_day_strategy(day_df, strategy_fn,
                                            strategy_params, budget, max_risk,
                                            prev_tail_df=prev_tail,
                                            persist_state=persist_state)
        trades.extend(day_trades)
        # 次の日の warmup 用に末尾を保存
        prev_tail = day_df.tail(PREV_TAIL_BARS)

    return dict(symbol=sym, name=name, trades=trades)


def calc_stats(trades, budget=200_000):
    """統計算出 (スリッページ・手数料反映済みPF/勝率)。"""
    n = len(trades)
    if n == 0:
        return dict(n=0, wins=0, win_rate=0.0, pf=0.0, total_pnl=0.0,
                    avg_win=0.0, avg_loss=0.0, max_dd=0.0, sharpe=0.0,
                    max_losing_streak=0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gp / gl if gl > 0 else float("inf")
    eq, peak, dd = budget, budget, 0.0
    rets = []
    streak = max_streak = 0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        d = (eq - peak) / peak * 100
        if d < dd:
            dd = d
        rets.append(t["pnl"] / budget)
        if t["pnl"] <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    # Sharpe (年率換算は省略、平均/標準偏差)
    mean_ret = np.mean(rets) if rets else 0
    std_ret = np.std(rets) if rets else 1
    sharpe = mean_ret / std_ret * np.sqrt(252) if std_ret > 0 else 0
    return dict(n=n, wins=len(wins), win_rate=len(wins)/n*100, pf=pf,
                total_pnl=sum(t["pnl"] for t in trades),
                avg_win=gp/len(wins) if wins else 0.0,
                avg_loss=-gl/len(losses) if losses else 0.0,
                max_dd=dd, sharpe=sharpe,
                max_losing_streak=max_streak)
