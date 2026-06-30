"""
analyze_firstday_drawdown.py  ―  スイング逆指値シグナルの『約定初日 含み損』分析
==================================================================
ユーザー仮説の検証:
  「逆指値ロングは約定した初日から含み損になることが多い。
   ならば下落を取りにいくショートにすれば勝てるのでは?」

本スクリプトは check_signals_stop の WATCHLIST × 戦略 (MACD/A7/RSI2) を
そのままの逆指値 (entry_type="stop") バックテストにかけ、約定した各トレードについて
  - 約定初日の【終値】が約定価格を下回ったか (= 初日終値で含み損か)
  - 約定初日の【安値】が約定価格をどれだけ下回ったか (= 初日のザラ場最大逆行)
を集計する。

さらに最重要のクロス集計:
  「初日に含み損だったトレードは、最終的に勝ったか負けたか」
  → 初日含み損でも結局『勝つ』なら、それをショートするのは誤り (踏まれる)。
  → 初日含み損が『負け』に直結するなら、ショート転換に妙味あり。

使い方:
  python analyze_firstday_drawdown.py                 # 全期間(365日)
  python analyze_firstday_drawdown.py --days 180      # 直近180日のシグナルのみ
  python analyze_firstday_drawdown.py --aggressive    # aggressive プリセット
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

# --aggressive を import 前に環境変数へ (check_signals_stop がトップで読む)
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"

import pandas as pd

from backtest_limit_entry import fetch, run_limit_backtest
import check_signals_stop as _stop

JST = timezone(timedelta(hours=9))


def _entry_day_ohlc(df: pd.DataFrame, entry_dt):
    """約定日 (entry_dt) の OHLC を返す。見つからなければ None。"""
    try:
        row = df.loc[entry_dt]
    except Exception:
        return None
    if isinstance(row, pd.DataFrame):       # 重複インデックス対策
        row = row.iloc[0]
    return float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])


def analyze(days: int):
    wl = _stop.WATCHLIST
    bt_days = max(_stop.PERIODS)
    cutoff = datetime.now(JST).date() - timedelta(days=days)

    rows = []   # 各約定トレードの初日情報
    print(f"逆指値バックテスト {len(wl)}銘柄 (mode={_stop.TRADING_MODE}, "
          f"対象=直近{days}日のシグナル)...", flush=True)

    for idx, (sym, name, strat) in enumerate(wl, 1):
        calc_fn, em, sm, tm = _stop.STRATEGY_PARAMS[strat]
        df = fetch(sym, bt_days)
        if df is None:
            continue
        r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm,
                               bt_days, strat, entry_type=_stop.ENTRY_TYPE)
        if not r:
            continue
        for t in r.get("trade_log", []):
            if t.get("reason") == "発注中":
                continue                      # 未約定は対象外
            sdt = t.get("signal_dt")
            if sdt is not None and hasattr(sdt, "date") and sdt.date() < cutoff:
                continue
            edt = t.get("entry_dt")
            ep = float(t.get("entry_p", 0) or 0)
            if edt is None or ep <= 0:
                continue
            ohlc = _entry_day_ohlc(df, edt)
            if ohlc is None:
                continue
            _o, _h, lo, cl = ohlc
            rows.append({
                "strat": strat, "sym": sym,
                "fd_close_pct": (cl - ep) / ep * 100,   # 初日終値の含み損益%
                "fd_low_pct":   (lo - ep) / ep * 100,   # 初日ザラ場の最大逆行%
                "underwater_close": cl < ep,            # 初日終値で含み損
                "touched_below":    lo < ep,            # 初日ザラ場で一度でも下回った
                "same_day_exit":    t.get("hold_days", 0) == 0,
                "final_win":        t.get("pnl", 0) > 0,
                "pnl":              float(t.get("pnl", 0) or 0),
                "mae_pct":          t.get("mae_pct"),
                "days_neg":         t.get("days_neg"),
                "hold_days":        t.get("hold_days", 0),
            })
        if idx % 6 == 0 or idx == len(wl):
            print(f"  {idx}/{len(wl)} 完了 (約定 {len(rows)}件)", flush=True)

    return rows


def _pct(n, d):
    return f"{n/d*100:5.1f}%" if d else "  ―  "


def _report_block(title, rows):
    n = len(rows)
    if n == 0:
        print(f"\n【{title}】 約定 0件")
        return
    uw = sum(1 for r in rows if r["underwater_close"])
    tb = sum(1 for r in rows if r["touched_below"])
    sd = sum(1 for r in rows if r["same_day_exit"])
    avg_fdc = sum(r["fd_close_pct"] for r in rows) / n
    avg_fdl = sum(r["fd_low_pct"] for r in rows) / n
    print(f"\n【{title}】 約定 {n}件")
    print(f"  初日【終値】で含み損        : {uw:>4}件 ({_pct(uw,n)})  平均初日終値 {avg_fdc:+.2f}%")
    print(f"  初日【ザラ場】で一度でも逆行: {tb:>4}件 ({_pct(tb,n)})  平均初日安値 {avg_fdl:+.2f}%")
    print(f"  初日中に決済(同日利確/損切): {sd:>4}件 ({_pct(sd,n)})")

    # ── 最重要クロス集計: 初日含み損 → 最終勝敗 ──
    uw_rows = [r for r in rows if r["underwater_close"]]
    ok_rows = [r for r in rows if not r["underwater_close"]]
    def _wr(rs):
        return sum(1 for r in rs if r["final_win"]) / len(rs) * 100 if rs else 0.0
    def _pnl(rs):
        return sum(r["pnl"] for r in rs)
    print(f"  ─ 初日終値が含み損だった {len(uw_rows)}件 → 最終勝率 {_wr(uw_rows):4.1f}% / "
          f"合計損益 {_pnl(uw_rows):+,.0f}円")
    print(f"  ─ 初日終値が含み益だった {len(ok_rows)}件 → 最終勝率 {_wr(ok_rows):4.1f}% / "
          f"合計損益 {_pnl(ok_rows):+,.0f}円")


def main():
    ap = argparse.ArgumentParser(description="逆指値 約定初日 含み損 分析")
    ap.add_argument("--days", type=int, default=365,
                    help="対象シグナル期間 (直近N日。既定365)")
    ap.add_argument("--aggressive", action="store_true", help="aggressive プリセット")
    args = ap.parse_args()

    rows = analyze(args.days)
    if not rows:
        print("約定トレードがありませんでした。")
        return

    print("\n" + "=" * 72)
    print(f"  逆指値 約定初日 含み損 分析  (mode={_stop.TRADING_MODE} / 直近{args.days}日)")
    print("=" * 72)
    _report_block("全戦略 合計", rows)
    for strat in ("MACD", "A7", "RSI2"):
        _report_block(strat, [r for r in rows if r["strat"] == strat])

    print("\n" + "=" * 72)
    print("読み方:")
    print("  ・『初日終値で含み損』が多い ＝ ユーザー観察を裏付け")
    print("  ・ただし重要なのは下のクロス集計:")
    print("     初日含み損でも【最終勝率が高い】なら → 結局戻すのでショートは踏まれる(逆効果)")
    print("     初日含み損が【最終的にも負け】なら   → 早期撤退 or ショート転換に妙味")
    print("=" * 72)


if __name__ == "__main__":
    main()
