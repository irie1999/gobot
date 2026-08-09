"""
analyze_trend_vs_trades.py  ―  実トレード × 日経トレンド 突合分析

ショート / ロングのバックテスト結果を日経平均トレンドと突合し、
トレンド別（▲上昇 / →横ばい / ▼下落）の勝率・損益を集計する。

Usage:
    python analyze_trend_vs_trades.py --short --days 180
    python analyze_trend_vs_trades.py --long  --days 180
    python analyze_trend_vs_trades.py --short --long --days 180   # 両方
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import pandas as pd
import yfinance as yf

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# ── import 前に TRADING_MODE を設定 ──────────────────────────────────────────
os.environ.setdefault("TRADING_MODE", "conservative")

import backtest_limit_entry as _bt


# ─────────────────────────────────────────────────────────────────────────────
# 1. 日経トレンドラベル取得
# ─────────────────────────────────────────────────────────────────────────────

def get_nikkei_trend(days: int) -> dict:
    """各営業日の日経トレンドを {date: 'up'/'down'/'sideways'} で返す。"""
    df = yf.download("^N225", period=f"{days + 60}d", interval="1d",
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        print("⚠️ 日経データ取得失敗")
        return {}
    close = df["Close"].squeeze()
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    close = close.dropna().sort_index()

    ma10 = close.rolling(10).mean()
    ma25 = close.rolling(25).mean()

    trend = {}
    cutoff = pd.Timestamp(TODAY - timedelta(days=days))
    for dt, c, m10, m25 in zip(close.index, close, ma10, ma25):
        if pd.isna(m10) or pd.isna(m25):
            continue
        if dt < cutoff:
            continue
        if c > m10 and m10 > m25:
            trend[dt.date()] = "up"
        elif c < m10 and m10 < m25:
            trend[dt.date()] = "down"
        else:
            trend[dt.date()] = "sideways"
    return trend


# ─────────────────────────────────────────────────────────────────────────────
# 2. バックテスト実行してトレードログ収集
# ─────────────────────────────────────────────────────────────────────────────

def collect_trades(module, days: int) -> list[dict]:
    """check_signals_stop / check_signals_short 等からトレードログを全件収集。"""
    watchlist  = module.WATCHLIST
    strategy_params = module.STRATEGY_PARAMS
    entry_type = getattr(module, "ENTRY_TYPE", "stop")

    all_trades = []
    for sym, name, strat in watchlist:
        if strat not in strategy_params:
            continue
        calc_fn, em, sm, tm = strategy_params[strat]
        try:
            result = _bt.run_limit_backtest(
                sym, calc_fn, em, sm, tm,
                entry_type=entry_type,
                backtest_days=days,
            )
        except Exception as e:
            print(f"  ⚠️ {sym} {strat}: {e}")
            continue
        # 全期間のトレードを収集
        for period_key, pr in result.get("period_results", {}).items():
            for t in pr.get("trade_log", []):
                all_trades.append({
                    **t,
                    "sym": sym,
                    "name": name,
                    "strat": strat,
                    "period": period_key,
                })
    # 重複排除: 同一トレードが複数期間に含まれる場合、最長期間のものを優先
    # entry_dt + sym + strat の組み合わせで重複排除
    seen = {}
    for t in all_trades:
        key = (t["sym"], t["strat"], t["entry_dt"])
        if key not in seen or t["period"] > seen[key]["period"]:
            seen[key] = t
    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# 3. トレンド突合・集計
# ─────────────────────────────────────────────────────────────────────────────

TREND_LABEL = {
    "up":       "▲ 上昇",
    "down":     "▼ 下落",
    "sideways": "→ 横ばい",
    None:       "不明",
}
TREND_COLOR = {
    "up":       "\033[32m",   # green
    "down":     "\033[31m",   # red
    "sideways": "\033[33m",   # yellow
    None:       "\033[90m",   # gray
}
RESET = "\033[0m"


def analyze(trades: list[dict], trend_map: dict, label: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}  トレード×日経トレンド 突合分析")
    print(f"{'='*60}")
    print(f"  総トレード数: {len(trades)}件")

    # signal_dt (シグナル発生日) = エントリー前日 → 当日のトレンドをエントリー判断基準とする
    buckets: dict[str | None, list[dict]] = defaultdict(list)
    unmapped = 0
    for t in trades:
        # signal_dt がある場合はそちらを使う（引け後シグナルなので前日終値でトレンド判定）
        sig_dt = t.get("signal_dt")
        if sig_dt:
            key_date = sig_dt if isinstance(sig_dt, type(TODAY)) else pd.Timestamp(sig_dt).date()
        else:
            entry_dt = t.get("entry_dt")
            key_date = entry_dt if isinstance(entry_dt, type(TODAY)) else pd.Timestamp(entry_dt).date()
        trend = trend_map.get(key_date)
        if trend is None:
            unmapped += 1
        buckets[trend].append(t)

    if unmapped:
        print(f"  ⚠️ トレンド未マッチ: {unmapped}件（期間外or日経データなし）")

    print(f"\n  {'トレンド':<14} {'件数':>5} {'勝率':>7} {'合計損益':>12} {'平均損益':>10} {'勝ち平均':>10} {'負け平均':>10}")
    print(f"  {'-'*14} {'-'*5} {'-'*7} {'-'*12} {'-'*10} {'-'*10} {'-'*10}")

    for trend_key in ["up", "sideways", "down", None]:
        ts = buckets.get(trend_key, [])
        if not ts:
            continue
        wins   = [t for t in ts if t["pnl"] > 0]
        losses = [t for t in ts if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in ts)
        avg_pnl   = total_pnl / len(ts)
        avg_win   = sum(t["pnl"] for t in wins)  / len(wins)  if wins   else 0
        avg_loss  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        wr        = len(wins) / len(ts) * 100

        pnl_sign = "+" if total_pnl >= 0 else ""
        col = TREND_COLOR.get(trend_key, "")
        lbl = TREND_LABEL.get(trend_key, "不明")
        print(f"  {col}{lbl:<14}{RESET} "
              f"{len(ts):>5} "
              f"{wr:>6.1f}% "
              f"{pnl_sign}{total_pnl:>10,.0f}円 "
              f"{avg_pnl:>+10,.0f}円 "
              f"{avg_win:>+10,.0f}円 "
              f"{avg_loss:>+10,.0f}円")

    # 決済理由別
    print(f"\n  ── 決済理由 × トレンド ──")
    print(f"  {'トレンド':<14} {'目標達成':>8} {'損切り':>8} {'タイムカット':>12}")
    for trend_key in ["up", "sideways", "down"]:
        ts = buckets.get(trend_key, [])
        if not ts:
            continue
        tgt  = sum(1 for t in ts if "目標" in (t.get("reason") or ""))
        stop = sum(1 for t in ts if "損切" in (t.get("reason") or ""))
        tc   = sum(1 for t in ts if "タイム" in (t.get("reason") or ""))
        col = TREND_COLOR.get(trend_key, "")
        lbl = TREND_LABEL.get(trend_key, "不明")
        print(f"  {col}{lbl:<14}{RESET} {tgt:>8} {stop:>8} {tc:>12}")

    # 戦略別
    strats = sorted(set(t["strat"] for t in trades))
    if len(strats) > 1:
        print(f"\n  ── 戦略 × トレンド 勝率 ──")
        header = f"  {'戦略':<12}"
        for trend_key in ["up", "sideways", "down"]:
            header += f"  {TREND_LABEL[trend_key]:>14}"
        print(header)
        for s in strats:
            row = f"  {s:<12}"
            for trend_key in ["up", "sideways", "down"]:
                ts = [t for t in buckets.get(trend_key, []) if t["strat"] == s]
                if ts:
                    wr = sum(1 for t in ts if t["pnl"] > 0) / len(ts) * 100
                    total = sum(t["pnl"] for t in ts)
                    sign = "+" if total >= 0 else ""
                    row += f"  {wr:>6.1f}% {sign}{total:>6,.0f}"
                else:
                    row += f"  {'—':>14}"
            print(row)

    print()


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="実トレード × 日経トレンド突合分析")
    parser.add_argument("--short",  action="store_true", help="ショート戦略を分析")
    parser.add_argument("--long",   action="store_true", help="ロング戦略を分析")
    parser.add_argument("--days",   type=int, default=180, help="分析期間（日）")
    args = parser.parse_args()

    if not args.short and not args.long:
        args.short = True  # デフォルトはショート

    print(f"日経平均トレンドデータ取得中...", flush=True)
    trend_map = get_nikkei_trend(args.days)
    print(f"  {len(trend_map)}営業日のトレンドデータ取得完了")

    if args.short:
        print("ショートバックテスト実行中...", flush=True)
        import check_signals_short as _short
        trades_short = collect_trades(_short, args.days)
        analyze(trades_short, trend_map, "ショート戦略 (逆指値売り)")

    if args.long:
        print("ロングバックテスト実行中...", flush=True)
        import check_signals_stop as _stop
        import check_signals_breakout as _brk

        trades_stop = collect_trades(_stop, args.days)
        trades_brk  = collect_trades(_brk, args.days)
        trades_long = trades_stop + trades_brk

        # 重複排除
        seen = {}
        for t in trades_long:
            key = (t["sym"], t["strat"], t["entry_dt"])
            if key not in seen:
                seen[key] = t
        trades_long = list(seen.values())

        analyze(trades_long, trend_map, "ロング戦略 (逆指値買い)")


if __name__ == "__main__":
    main()
