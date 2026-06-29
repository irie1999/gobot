"""
backtest_intraday.py  ―  デイトレ バックテストエンジン
==================================================================
スイング版 backtest_limit_entry.py のデイトレ対応版。
5分足データを使った日中バックテスト。

【概要】
  - 当日中のみ有効 (ENTRY_EXPIRE=当日)
  - 15:25 強制決済
  - stop_mode="intraday" 固定 (ザラ場高値/安値でOCO判定)
  - ATR: 5分足 14期間 EWM (全バー横断、日またぎ込み)

【戦略: PREV_CLOSE_BREAK (前日終値ブレイク)】
  - シグナル: 毎営業日 (バックテスト期間の全取引日)
  - エントリー: 当日の5分足 high >= order_p で逆指値発動
  - order_p  = prev_close + atr × em  (em=0.0 → prev_close そのまま)
  - stop_p   = entry_p  - atr × sm  (エントリー実価格基準)
  - target_p = entry_p  + atr × tm  (エントリー実価格基準)
  - 強制決済: 15:25 の最初のバー終値

【使い方 (ライブラリとして)】
  from backtest_intraday import run_intraday_backtest, calc_recommend_score
  from daytrade_data import load_intraday_batch

  dfs = load_intraday_batch(["7203.T"], days=180, source="auto")
  result = run_intraday_backtest(
      "7203.T", "トヨタ", dfs["7203.T"],
      entry_atr_mult=0.0, stop_atr_mult=1.5, target_atr_mult=3.0,
      backtest_days=180,
  )
  score, rank = calc_recommend_score(result["period_results"])
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))

# ── 定数 ────────────────────────────────────────────────────────
ATR_PERIOD        = 14              # 5分足ATR期間
FIXED_QTY         = 100             # 固定株数
SLIPPAGE_STOP_PCT = 0.002           # 逆指値スリッページ 0.2%
FEE_PCT_ONE_WAY   = 0.0003          # 手数料 片道 0.03% (日計り想定)
ENTRY_CUTOFF      = dtime(14, 30)   # これ以降は新規エントリーしない
FORCE_CLOSE       = dtime(15, 25)   # 強制決済時刻
AM_START          = dtime(9, 0)
AM_END            = dtime(11, 30)
PM_START          = dtime(12, 30)
PM_END            = dtime(15, 30)
PERIODS           = [30, 60, 90, 120, 150, 180]  # BTスコア計算期間 (暦日)
MIN_PRICE         = 100.0
MAX_PRICE         = 100_000.0
BACKTEST_DAYS     = 180             # デフォルトバックテスト期間

# GAP_DOWN ギャップ幅フィルタ (寄りギャップダウン率 = (前日終値-始値)/前日終値)
#   浅すぎ(<MIN)=ノイズでエッジ無し / 深すぎ(>MAX)=悪材料急落で埋まらず大損。
#   適度な売られすぎだけ拾うことで期待値を改善する。0.0/1.0 で実質無効。
GAP_DOWN_MIN_PCT  = 0.015           # 最小ギャップ率 1.5%
GAP_DOWN_MAX_PCT  = 0.06            # 最大ギャップ率 6%


def _is_trading_time(t: dtime) -> bool:
    """東証の立会時間か判定。"""
    return (AM_START <= t < AM_END) or (PM_START <= t < PM_END)


# ── ATR 計算 ───────────────────────────────────────────────────

def calc_atr_5m(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.DataFrame:
    """
    5分足 True Range の EWM ATR を計算。
    日をまたいでも前終値を使って True Range を計算するため、
    overnight gap が ATR に反映される。
    """
    df = df.copy()
    prev_c = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_c).abs(),
        (df["low"]  - prev_c).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=period, adjust=False).mean()
    return df


def calc_atr_daily(df_5m: pd.DataFrame, period: int = 14) -> dict:
    """
    5分足を日足にリサンプルし、日足 True Range の EWM ATR を計算。
    返り値: {date: その日終値時点の日足ATR}。

    stop/target を「日次ボラ」で測るために使う。5分足ATRは1バー分の
    値幅しかなく、デイトレのstopとしては狭すぎてノイズで即狩られるため。
    """
    if df_5m is None or df_5m.empty:
        return {}
    daily = df_5m.resample("1D").agg({
        "high": "max", "low": "min", "close": "last",
    }).dropna(subset=["close"])
    if len(daily) < 2:
        return {}
    prev_c = daily["close"].shift(1)
    tr = pd.concat([
        daily["high"] - daily["low"],
        (daily["high"] - prev_c).abs(),
        (daily["low"]  - prev_c).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    return {ts.date(): float(v) for ts, v in atr.items() if pd.notna(v)}


# ── 日次グループ化 ─────────────────────────────────────────────

def _get_daily_groups(
    df_5m: pd.DataFrame,
) -> list[tuple[date, float, float, float, float, pd.DataFrame]]:
    """
    5分足 DataFrame を日付でグループ化し、各日について
    (日付, 前日終値, 前日高値, 前日末5分足ATR, 前日末日足ATR,
     その日の立会時間5分足 DataFrame) を返す。

    最初の取引日はprev_closeがないためスキップ。
    """
    df_5m = calc_atr_5m(df_5m)
    daily_atr_map = calc_atr_daily(df_5m)

    # 立会時間マスクを全体に対して1回だけ計算 (ベクトル化)
    times = df_5m.index.time
    trade_mask = pd.Series(
        [(AM_START <= t < AM_END) or (PM_START <= t < PM_END) for t in times],
        index=df_5m.index)

    # 日付ごとに1回だけ groupby (旧実装の O(バー数×日数) を O(バー数) に)
    g_full = {d: g for d, g in df_5m.groupby(df_5m.index.date)}
    dates = sorted(g_full.keys())

    result = []
    for i in range(1, len(dates)):
        d      = dates[i]
        prev_d = dates[i - 1]

        prev_bars = g_full.get(prev_d)
        if prev_bars is None or prev_bars.empty:
            continue

        prev_close = float(prev_bars["close"].iloc[-1])
        prev_high  = float(prev_bars["high"].max())
        prev_atr   = float(prev_bars["atr"].iloc[-1])
        prev_atr_d = daily_atr_map.get(prev_d, 0.0)

        if pd.isna(prev_close) or pd.isna(prev_atr) or prev_atr <= 0:
            continue

        cur = g_full.get(d)
        if cur is None:
            continue
        # 立会時間外をフィルタ (事前計算したマスクを使う)
        day_df = cur[trade_mask.loc[cur.index].values]
        if len(day_df) < 3:
            continue

        result.append((d, prev_close, prev_high, prev_atr, prev_atr_d, day_df))
    return result


# ── 1日分バックテスト ──────────────────────────────────────────

def _simulate_day(
    day_df: pd.DataFrame,
    order_p: float,
    atr: float,
    stop_atr_mult: float,
    target_atr_mult: float,
    target_price_override: float | None = None,
) -> dict | None:
    """
    1日分の日中バックテストシミュレーション。

    エントリー条件: high >= order_p (逆指値買い)
    stop/target はエントリー実価格から計算 (ギャップアップ対応)。
    決済優先順位:
      1. 損切り/目標が同バーで両方ヒット → 損切り優先 (保守的)
      2. 目標達成 → target_p で決済
      3. 損切り   → stop_p × (1 - SLIPPAGE) で決済
      4. FORCE_CLOSE 以降 → 終値で強制決済

    Returns: trade dict or None
    """
    state    = "idle"
    entry_p  = 0.0
    stop_p   = 0.0
    target_p = 0.0
    entry_dt: pd.Timestamp | None = None

    bars = list(day_df.itertuples())

    for i, bar in enumerate(bars):
        ts = bar.Index
        op = float(bar.open)
        hi = float(bar.high)
        lo = float(bar.low)
        cl = float(bar.close)
        t  = ts.time()

        if state == "in_pos":
            # 強制決済: FORCE_CLOSE 以降の最初のバーで終値決済
            if t >= FORCE_CLOSE:
                exit_p  = cl
                exit_dt = ts
                reason  = "引け強制"
                state   = "closed"
                break

            # OCO: 同バーで両方ヒットしたら損切り優先 (保守的)
            hit_stp = lo <= stop_p
            hit_tgt = hi >= target_p

            if hit_stp and hit_tgt:
                exit_p  = stop_p * (1.0 - SLIPPAGE_STOP_PCT)
                exit_dt = ts
                reason  = "損切り"
                state   = "closed"
                break
            if hit_stp:
                exit_p  = stop_p * (1.0 - SLIPPAGE_STOP_PCT)
                exit_dt = ts
                reason  = "損切り"
                state   = "closed"
                break
            if hit_tgt:
                exit_p  = target_p
                exit_dt = ts
                reason  = "目標達成"
                state   = "closed"
                break
            continue

        # idle: エントリー判定
        if t >= ENTRY_CUTOFF:
            break

        if hi < order_p:
            continue

        # 逆指値発動: ギャップアップ → 寄付値, 通常 → order_p + スリッページ
        if op >= order_p:
            ep = op
        else:
            ep = order_p * (1.0 + SLIPPAGE_STOP_PCT)

        entry_p  = ep
        entry_dt = ts
        state    = "in_pos"

        # stop/target をエントリー実価格から計算 (ギャップアップ対応)
        stop_p = entry_p - atr * stop_atr_mult
        if target_price_override is not None and target_price_override > entry_p:
            target_p = target_price_override
        else:
            target_p = entry_p + atr * target_atr_mult

        # 約定と同バーで決済チェック
        hit_stp = lo <= stop_p
        hit_tgt = hi >= target_p

        if hit_stp and hit_tgt:
            exit_p  = stop_p * (1.0 - SLIPPAGE_STOP_PCT)
            exit_dt = ts
            reason  = "損切り"
            state   = "closed"
            break
        if hit_stp:
            exit_p  = stop_p * (1.0 - SLIPPAGE_STOP_PCT)
            exit_dt = ts
            reason  = "損切り"
            state   = "closed"
            break
        if hit_tgt:
            exit_p  = target_p
            exit_dt = ts
            reason  = "目標達成"
            state   = "closed"
            break

    # 日中に決済されなかった → 最終バーで強制決済
    if state == "in_pos":
        last    = bars[-1]
        exit_p  = float(last.close)
        exit_dt = last.Index
        reason  = "引け強制"
        state   = "closed"

    if state != "closed" or entry_dt is None:
        return None

    qty = FIXED_QTY
    fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
    pnl = (exit_p - entry_p) * qty - fee
    pct = (exit_p - entry_p) / entry_p * 100 if entry_p > 0 else 0.0

    return dict(
        entry_dt=entry_dt, exit_dt=exit_dt,
        entry_p=round(entry_p, 1), exit_p=round(exit_p, 1),
        qty=qty,
        pnl=round(pnl, 0), pct=round(pct, 3),
        fee=round(fee, 0),
        reason=reason,
        order_p=round(order_p, 1),
        stop_p=round(stop_p, 1),
        target_p=round(target_p, 1),
    )


# ── ヘルパー ────────────────────────────────────────────────────

def _empty_result(symbol: str, name: str, strategy_name: str) -> dict:
    empty_pr = {d: {"trades": 0, "wins": 0, "losses": 0,
                    "win_rate": 0.0, "pf": 0.0, "total_pnl": 0.0,
                    "trade_log": []}
                for d in PERIODS}
    return dict(symbol=symbol, name=name, strategy=strategy_name,
                period_results=empty_pr, all_trades=[])


def _period_stats(trades: list[dict], days: int) -> dict:
    """指定カレンダー日数内のトレードを集計。"""
    today  = datetime.now(JST).date()
    cutoff = today - timedelta(days=days)
    sub    = [t for t in trades if t["entry_dt"].date() >= cutoff]
    n      = len(sub)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "pf": 0.0, "total_pnl": 0.0, "trade_log": []}
    wins   = sum(1 for t in sub if t["pnl"] > 0)
    losses = n - wins
    gp     = sum(t["pnl"] for t in sub if t["pnl"] > 0)
    gl     = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
    pf     = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return dict(
        trades=n, wins=wins, losses=losses,
        win_rate=wins / n * 100,
        pf=pf,
        total_pnl=sum(t["pnl"] for t in sub),
        trade_log=sub,
    )


# ── メインバックテスト ─────────────────────────────────────────

def run_intraday_backtest(
    symbol: str,
    name: str,
    df_5m: pd.DataFrame,
    entry_atr_mult: float = 0.0,
    stop_atr_mult: float  = 1.5,
    target_atr_mult: float = 3.0,
    backtest_days: int = BACKTEST_DAYS,
    strategy_name: str = "PREV_CLOSE_BREAK",
    stop_basis: str = "daily",
    daily_groups: list | None = None,
) -> dict:
    """
    5分足を使ったデイトレバックテスト。

    stop_basis:
      "daily" (既定): stop/target を日足ATR基準で測る。5分足ATRは1バー分
                      しかなくstopが価格の0.6%程度と狭すぎ、ノイズで即狩られて
                      勝率が崩壊する (76-89%が4-10分で損切り) ため日次ボラに変更。
      "5m"          : 旧挙動 (stop/target も5分足ATR基準)。比較・後方互換用。
    entry の order_p は従来どおり5分足ATR基準 (em=0.0なら影響なし)。

    strategy_name:
      PREV_CLOSE_BREAK : 前日終値ブレイク (毎日エントリー)
      GAP_UP           : ギャップアップ日のみ (始値 > 前日終値 + ATR×em)
      GAP_DOWN         : ギャップダウン日限定 mean-reversion (始値 < 前日終値 - ATR×em)
                         エントリー=始値, 目標=前日終値 (ギャップフィル), 損切り=始値-ATR×sm
      ORB30            : Opening Range Breakout (最初の30分高値ブレイク)
      PREV_HIGH_BREAK  : 前日高値ブレイク (PREV_CLOSE_BREAKより高い閾値)

    entry_atr_mult:
      PREV_CLOSE_BREAK: order_p = prev_close + atr × em
      GAP_UP          : ギャップ最小幅 = atr × em (0.0 → 任意ギャップ)
      ORB30           : 未使用 (常に30分高値)
      PREV_HIGH_BREAK : 未使用 (常に前日高値)
    """
    if df_5m is None or df_5m.empty:
        return _empty_result(symbol, name, strategy_name)

    # daily_groups を渡されたら再計算しない (同一銘柄×同一窓で全戦略共有=高速化)
    if daily_groups is None:
        # バックテスト期間にカット (余裕を持って +ATR_PERIOD バー分を含める)
        today   = datetime.now(JST).date()
        cutoff  = pd.Timestamp(today - timedelta(days=backtest_days + 10))
        df_5m   = df_5m[df_5m.index >= cutoff].copy()

        if len(df_5m) < ATR_PERIOD + 5:
            return _empty_result(symbol, name, strategy_name)

        daily_groups = _get_daily_groups(df_5m)
    if not daily_groups:
        return _empty_result(symbol, name, strategy_name)

    today = datetime.now(JST).date()

    trades: list[dict] = []

    for d, prev_close, prev_high, atr, atr_daily, day_df in daily_groups:
        # バックテスト期間内のみ
        if d < today - timedelta(days=backtest_days):
            continue

        if prev_close < MIN_PRICE or prev_close > MAX_PRICE:
            continue

        # stop/target サイジング用ATR (basis に応じて切替)
        if stop_basis == "daily" and atr_daily and atr_daily > 0:
            size_atr = atr_daily
        else:
            size_atr = atr

        # ── 戦略別エントリー条件 ──────────────────────────────
        target_override: float | None = None

        if strategy_name == "PREV_CLOSE_BREAK":
            order_p = prev_close + atr * entry_atr_mult
            sim_df  = day_df

        elif strategy_name == "GAP_UP":
            # 始値が前日終値 + ATR×em を上回るギャップアップ日のみ
            day_open = float(day_df.iloc[0]["open"])
            if day_open <= prev_close + atr * entry_atr_mult:
                continue
            # order_p = prev_close にすることで 1本目から即エントリー
            order_p = prev_close
            sim_df  = day_df

        elif strategy_name == "GAP_DOWN":
            # 始値が前日終値 - ATR×em を下回るギャップダウン日のみ (mean reversion)
            day_open = float(day_df.iloc[0]["open"])
            if day_open >= prev_close - atr * entry_atr_mult:
                continue
            # ギャップ幅フィルタ: 適度な売られすぎ (MIN〜MAX) だけ拾う。
            # 深い急落(悪材料)は埋まらず大損、浅い下げはエッジ無し。
            gap_pct = (prev_close - day_open) / prev_close if prev_close > 0 else 0.0
            if gap_pct < GAP_DOWN_MIN_PCT or gap_pct > GAP_DOWN_MAX_PCT:
                continue
            # order_p = day_open にすることで 1本目から即エントリー
            order_p = day_open
            sim_df  = day_df
            # 目標 = 前日終値 (ギャップフィル)
            target_override = prev_close

        elif strategy_name == "ORB30":
            # 最初の30分 (6本 × 5min) の高値をブレイクしたらエントリー
            orb_bars = day_df.iloc[:6]
            if len(orb_bars) < 3:
                continue
            order_p = float(orb_bars["high"].max())
            # ORB期間後のバーでシミュレーション
            sim_df = day_df.iloc[6:]
            if len(sim_df) < 2:
                continue

        elif strategy_name == "PREV_HIGH_BREAK":
            # 前日高値ブレイク (PREV_CLOSE_BREAKより選別的)
            order_p = prev_high
            sim_df  = day_df

        else:
            order_p = prev_close + atr * entry_atr_mult
            sim_df  = day_df

        trade = _simulate_day(sim_df, order_p, size_atr, stop_atr_mult, target_atr_mult,
                              target_price_override=target_override)
        if trade is None:
            continue

        ep = trade["entry_p"]
        trade["signal_dt"]     = pd.Timestamp(d)
        trade["prev_close"]    = prev_close
        trade["atr_5m"]        = round(atr, 2)
        trade["atr_daily"]     = round(atr_daily, 2)
        trade["size_atr"]      = round(size_atr, 2)
        trade["stop_loss_pct"] = round(size_atr * stop_atr_mult / ep * 100, 2) if ep > 0 else 0.0
        trades.append(trade)

    period_results = {d: _period_stats(trades, d) for d in PERIODS}

    return dict(
        symbol=symbol, name=name, strategy=strategy_name,
        period_results=period_results,
        all_trades=trades,
    )


# ── BTスコア計算 (スイング版と同一計算式) ───────────────────────

def calc_recommend_score(period_results: dict) -> tuple[int, str]:
    """
    バックテスト成績からおすすめスコア (0-100) とランクを計算。
    スイング版 check_signals_stop.calc_recommend_score と同一計算式。

      勝率     : 最大40点 (avg_wr [0-100] × 0.4)
      PF       : 最大30点 (avg_pf/10 × 30, PF=10キャップ、∞は10扱い)
      期間安定性: 最大20点 (プラス期間数 / 有効期間数 × 20)
      取引回数  : 最大10点 (合計20取引で満点)
    """
    results = [r for r in period_results.values()
               if r and r.get("trades", 0) > 0]
    if not results:
        return 0, "-"

    avg_wr   = sum(r["win_rate"] for r in results) / len(results)
    avg_pf   = sum(min(r["pf"] if r["pf"] != float("inf") else 10, 10)
                   for r in results) / len(results)
    stable   = sum(1 for r in results if r["total_pnl"] > 0) / len(results)
    t_trades = sum(r["trades"] for r in results)

    score = round(
        avg_wr * 0.4
        + (avg_pf / 10) * 30
        + stable * 20
        + min(t_trades / 20, 1) * 10
    )
    rank = "★★★" if score >= 80 else "★★" if score >= 60 else "★" if score >= 40 else "△"
    return score, rank


def apply_atr_penalty(score: int, stop_loss_pct: float) -> tuple[int, str]:
    """
    損切り幅が広い時にスコアを減点。スイング版と同一ロジック。
    基準7%以下: ペナルティなし。7%超: 線形減点、最大50%減 (37%超で頭打ち)。
    """
    if stop_loss_pct <= 7.0:
        return score, ""
    multiplier = max(0.5, 1.0 - (stop_loss_pct - 7.0) / 30.0)
    return round(score * multiplier), f"{stop_loss_pct:.1f}%"


# ── CLI (単体テスト用) ─────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="デイトレ バックテストエンジン テスト")
    parser.add_argument("symbols", nargs="*", default=["7203.T"], help="銘柄コード")
    parser.add_argument("--days",     type=int,   default=180,   help="バックテスト期間 (暦日)")
    parser.add_argument("--sm",       type=float, default=1.5,   help="損切り倍率 (stop_atr_mult)")
    parser.add_argument("--tm",       type=float, default=3.0,   help="目標倍率  (target_atr_mult)")
    parser.add_argument("--strategy", default="PREV_CLOSE_BREAK",
                        choices=["PREV_CLOSE_BREAK", "GAP_UP", "GAP_DOWN", "ORB30", "PREV_HIGH_BREAK"],
                        help="戦略名")
    parser.add_argument("--source", default="auto",
                        choices=["auto", "local", "yfinance"],
                        help="データソース")
    args = parser.parse_args()

    from daytrade_data import load_intraday_batch

    print(f"銘柄: {args.symbols}  期間: {args.days}日  sm={args.sm}  tm={args.tm}")
    dfs = load_intraday_batch(args.symbols, days=args.days, source=args.source)

    for sym in args.symbols:
        df = dfs.get(sym)
        if df is None or df.empty:
            print(f"  {sym}: データなし")
            continue

        result = run_intraday_backtest(
            sym, sym, df,
            entry_atr_mult=0.0, stop_atr_mult=args.sm, target_atr_mult=args.tm,
            backtest_days=args.days, strategy_name=args.strategy,
        )
        score, rank = calc_recommend_score(result["period_results"])
        print(f"\n  {sym}  BTスコア={score} ({rank})")

        for d in PERIODS:
            pr = result["period_results"].get(d, {})
            n  = pr.get("trades", 0)
            if n == 0:
                continue
            wr  = pr.get("win_rate", 0)
            pf  = pr.get("pf", 0)
            pnl = pr.get("total_pnl", 0)
            pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
            print(f"    {d:3d}日: {n:3d}取引  勝率{wr:.0f}%  PF{pf_str}  損益{pnl:+,.0f}円")

        print(f"  全取引: {len(result['all_trades'])}件")
        if result["all_trades"]:
            last = result["all_trades"][-1]
            print(f"  最終: {last['entry_dt'].date()} {last['reason']} "
                  f"損益{last['pnl']:+,.0f}円")
