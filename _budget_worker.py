"""
_budget_worker.py ― budget_sim.py のサブプロセスワーカー
1スクリプト分のバックテスト + 資金制約シミュレーションを実行し、JSON で出力する。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# Windows (cp932) 対策: stdout/stderr を UTF-8 に強制
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

JST = timezone(timedelta(hours=9))


def _run_group(mod, watchlist, workers: int) -> list[dict]:
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in watchlist}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    items.append(r)
            except Exception:
                pass
    return items


def _simulate(all_items_mods: list[tuple], show_days: int, budget: float) -> dict:
    """
    スコア降順・資金制約付きシミュレーション。

    同一日に複数シグナルがある場合、スコアが高い順に資金が許す限り約定させる。
    ポジションが閉じたら資金を解放。
    """
    from backtest_limit_entry import compute_period_result, FIXED_QTY

    # 全トレードをスコア付きで収集
    all_trades: list[dict] = []
    for items, mod in all_items_mods:
        for item in items:
            score, _ = mod.calc_recommend_score(item["period_results"])
            pr = compute_period_result(item, show_days)
            for t in pr.get("trade_log", []):
                if not t.get("entry_dt"):
                    continue  # 未約定シグナルはスキップ
                exit_dt = (t["exit_dt"].date() if t.get("exit_dt")
                           else datetime.now(JST).date())
                required = t["signal_price"] * FIXED_QTY
                all_trades.append({
                    "symbol":   item["symbol"],
                    "name":     item["name"],
                    "strategy": item["strategy"],
                    "score":    score,
                    "entry_dt": t["entry_dt"].date(),
                    "exit_dt":  exit_dt,
                    "required": required,
                    "pnl":      t.get("pnl", 0),
                    "reason":   t.get("reason") or "保有中",
                })

    # 約定日昇順 → 同日内はスコア降順
    all_trades.sort(key=lambda t: (t["entry_dt"], -t["score"]))

    available = budget
    open_pos: list[dict] = []
    executed: list[dict] = []
    skipped_budget: list[dict] = []  # 資金不足でスキップ
    skipped_price: list[dict] = []   # 1株価×100が予算超過

    for trade in all_trades:
        # 既に exit した ポジションの資金を解放
        still_open = []
        for pos in open_pos:
            if pos["exit_dt"] <= trade["entry_dt"]:
                available += pos["required"]
            else:
                still_open.append(pos)
        open_pos = still_open

        req = trade["required"]
        if req > budget:
            # 予算自体を超える銘柄（レバレッジ込みでも買えない）
            skipped_price.append(trade)
            continue

        if available >= req:
            available -= req
            open_pos.append(trade)
            executed.append(trade)
        else:
            skipped_budget.append(trade)

    closed = [t for t in executed if t["reason"] != "保有中"]
    total_pnl = sum(t["pnl"] for t in closed)
    wins   = sum(1 for t in closed if t["pnl"] > 0)
    losses = sum(1 for t in closed if t["pnl"] < 0)

    # スキップした高スコア銘柄 Top10（資金不足のもの）
    top_skipped = sorted(skipped_budget, key=lambda x: -x["score"])[:10]

    return {
        "n_executed":    len(executed),
        "n_closed":      len(closed),
        "n_holding":     len(executed) - len(closed),
        "n_skipped_budget": len(skipped_budget),
        "n_skipped_price":  len(skipped_price),
        "total_pnl":     total_pnl,
        "wins":          wins,
        "losses":        losses,
        "win_rate":      wins / (wins + losses) * 100 if (wins + losses) > 0 else 0.0,
        "avg_required":  (sum(t["required"] for t in executed) / len(executed)
                          if executed else 0.0),
        "top_skipped":   [
            {"symbol": t["symbol"], "name": t["name"], "score": t["score"],
             "required": int(t["required"]), "entry_dt": str(t["entry_dt"])}
            for t in top_skipped
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True, choices=[
        "run_signals", "run_signals_merged",
        "run_signals_aggressive", "run_signals_prime", "run_signals_nolimit",
    ])
    parser.add_argument("--budget",  type=float, default=4_200_000)
    parser.add_argument("--days",    type=int,   default=365)
    parser.add_argument("--workers", type=int,   default=4)
    args = parser.parse_args()

    script = args.script

    # ── TRADING_MODE を import 前に設定 ──────────────────────────────────
    if script in ("run_signals_nolimit", "run_signals_prime", "run_signals_aggressive"):
        os.environ["TRADING_MODE"] = "aggressive"
    else:
        os.environ["TRADING_MODE"] = "conservative"

    import check_signals_stop     as _stop
    import check_signals_breakout as _brk
    import check_signals_short    as _short

    # ── スクリプト固有パラメータ上書き ────────────────────────────────────
    if script in ("run_signals_nolimit", "run_signals_prime", "run_signals_aggressive"):
        for k, v in list(_stop.STRATEGY_PARAMS.items()):
            _stop.STRATEGY_PARAMS[k] = (v[0], v[1], 1.5, 2.0)
        for k, v in list(_brk.STRATEGY_PARAMS.items()):
            _brk.STRATEGY_PARAMS[k] = (v[0], v[1], 1.5, 2.0)

    # ── WATCHLIST 決定 ────────────────────────────────────────────────────
    if script == "run_signals":
        stop_wl = _stop.WATCHLIST
        brk_wl  = _brk.WATCHLIST

    elif script == "run_signals_merged":
        # run_signals_merged は TRADING_MODE を書き換えないので安全に import 可
        import run_signals_merged as _m
        stop_wl = _m._merged_watchlist(_stop.WATCHLIST, _m._WF_STOP)
        brk_wl  = _m._merged_watchlist(_brk.WATCHLIST,  _m._WF_BRK)

    elif script == "run_signals_aggressive":
        import run_signals_aggressive as _a
        # STOP_WATCHLIST は既存 + WF の合算済みリスト
        stop_wl = _a.STOP_WATCHLIST
        brk_wl  = _a.BRK_WATCHLIST

    elif script == "run_signals_prime":
        import run_signals_prime as _p
        stop_wl = _p.STOP_WATCHLIST
        brk_wl  = _p.BRK_WATCHLIST

    elif script == "run_signals_nolimit":
        import run_signals_nolimit as _n
        stop_wl = _n.STOP_WATCHLIST
        brk_wl  = _n.BRK_WATCHLIST

    srt_wl = _short.WATCHLIST

    # ── バックテスト実行（並列） ──────────────────────────────────────────
    print(f"[{script}] バックテスト開始: stop={len(stop_wl)}, brk={len(brk_wl)}, "
          f"srt={len(srt_wl)}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=3) as outer:
        f_stop  = outer.submit(_run_group, _stop,  stop_wl, args.workers)
        f_brk   = outer.submit(_run_group, _brk,   brk_wl,  args.workers)
        f_short = outer.submit(_run_group, _short,  srt_wl,  args.workers)
    stop_items  = f_stop.result()
    brk_items   = f_brk.result()
    short_items = f_short.result()

    print(f"[{script}] シミュレーション実行中...", file=sys.stderr, flush=True)
    result = _simulate(
        [(stop_items, _stop), (brk_items, _brk), (short_items, _short)],
        args.days, args.budget,
    )
    result["script"] = script

    # JSON を stdout に出力（parent が読む）
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
