"""
signals_export_daytrade.py  ―  シグナル JSON エクスポート
==================================================================
逆指値ロング (run_signals_holdout_all.py の signals_latest.json書出し)
を5分足デイトレ用に移植。

【役割】
WATCHLIST + 戦略の組み合わせをスキャンし、現在発生中のシグナルを
JSON ファイルに保存。position_server / bot / 外部ツールから読み取り可能。

【使い方】
  python signals_export_daytrade.py
  python signals_export_daytrade.py --watchlist daytrade_watchlist_macd.py
  python signals_export_daytrade.py --output signals_daytrade_latest.json

【出力フォーマット】
  {
    "generated_at": "2026-06-13",
    "mode": "long",  # or "short"
    "regime": {"trend": "up", "vol": "mid", ...},
    "signals": [
      {
        "symbol": "5803.T",
        "name": "フジクラ",
        "strategy": "MACD",
        "order_p": 4500,
        "stop_p": 4470,
        "target_p": 4560,
        "qty": 100,
        "score": 75,
        "rank": "★★"
      },
      ...
    ]
  }
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_signals_daytrade import backtest_one, _load_watchlist
from nikkei_filter_daytrade import detect_regime
from daytrade_data import calc_position_size

JST = timezone(timedelta(hours=9))


def main():
    parser = argparse.ArgumentParser(description="シグナルJSON エクスポート")
    parser.add_argument("--watchlist", default=None)
    parser.add_argument("--output", default="signals_daytrade_latest.json")
    parser.add_argument("--budget", type=int, default=300_000)
    parser.add_argument("--max-risk", type=int, default=1_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--days", type=int, default=7,
                        help="直近何日のシグナルを対象とするか")
    args = parser.parse_args()

    watchlist = _load_watchlist(args.watchlist)
    if not watchlist:
        print(f"[error] WATCHLIST 空")
        return

    print(f"スキャン: {len(watchlist)}件 / workers={args.workers}")

    # 相場局面
    regime = detect_regime()

    # backtest (並列)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    items = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for entry in watchlist:
            if len(entry) >= 3:
                sym, name, strat = entry[:3]
            else:
                sym, name = entry[:2]
                strat = "DON"
            futs[ex.submit(backtest_one, sym, name, strat)] = (sym, strat)
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                items.append(r)

    # 直近シグナル抽出 + 整形
    today = datetime.now(JST).date()
    cutoff = today - timedelta(days=args.days)
    signals = []
    for it in items:
        for sig in it["recent_signals"]:
            entry_dt = sig.get("entry_dt")
            if not hasattr(entry_dt, "date"):
                continue
            if entry_dt.date() < cutoff:
                continue
            # 株数計算
            qty = calc_position_size(
                sig.get("entry_p", 0),
                sig.get("stop_p", 0),
                args.budget, args.max_risk)
            signals.append({
                "symbol": it["symbol"],
                "name": it["name"],
                "strategy": it["strategy"],
                "side": sig.get("side", "long"),
                "order_p": sig.get("entry_p", 0),
                "stop_p": sig.get("stop_p", 0),
                "target_p": sig.get("target_p", 0),
                "qty": qty,
                "entry_dt": str(entry_dt),
                "exit_dt": str(sig.get("exit_dt", "")),
                "pnl": sig.get("pnl", 0),
                "reason": sig.get("reason", ""),
                "score": it["score"],
                "rank": it["rank"],
            })

    # スコアでソート
    signals.sort(key=lambda x: -x["score"])

    # JSON 書出し
    out = Path(args.output)
    payload = {
        "generated_at": str(today),
        "generated_at_jst": datetime.now(JST).isoformat(),
        "watchlist": args.watchlist or "default",
        "regime": {
            "trend": regime["trend"],
            "vol": regime["vol"],
            "label": regime["label"],
            "nikkei": regime["nikkei"],
            "allowed_strategies": (sorted(regime.get("allowed_strategies"))
                                   if regime.get("allowed_strategies") else None),
        },
        "total_items": len(items),
        "total_signals": len(signals),
        "signals": signals,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n書出し: {out.resolve()} ({len(signals)}件)")


if __name__ == "__main__":
    main()
