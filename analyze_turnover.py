"""
analyze_turnover.py  ―  回転率(高速利確/損切)パラメータ比較分析
=================================================================
MAX_HOLD (最大保有日数) と ENTRY_EXPIRE (注文有効日数) をスイープして、
平均保有日数・PF・勝率・総損益・資金効率(1日あたり損益)を比較する。

「高回転で回したい」場合、目標倍率 tm だけでなく MAX_HOLD が
回転率の本当のボトルネックになる。このスクリプトで実測する。

【使い方】
  # ショート3戦略を MAX_HOLD=7/15 で比較 (tm はモジュールの最適値を使用)
  python analyze_turnover.py --family short --max-hold 7,15 --budget 600000 --limit 300

  # tm を強制指定して比較 (例: tm=1.5 固定)
  python analyze_turnover.py --family short --max-hold 7,15 --tm 1.5 --limit 300

  # さらに速いパラメータも (MAX_HOLD と ENTRY_EXPIRE 両方スイープ)
  python analyze_turnover.py --family short --max-hold 3,5,7,10,15 --entry-expire 2,3 --limit 300

  # ロングでも測れる
  python analyze_turnover.py --family stop --max-hold 7,15 --limit 300

【主要指標】
  平均保有日数  : 約定〜決済までの平均日数 (短いほど高回転)
  PF            : 総利益 / 総損失 (>1.0 で利益)
  勝率          : 目標達成 / 全決済
  総損益        : 全取引のコスト込み損益合計
  1日あたり損益 : 総損益 / 延べ保有日数 = 資金効率 (高回転の真の評価軸)
  年間取引数    : 取引数 / (期間/365) → 回転回数の目安
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# ── TRADING_MODE を import 前に設定 ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

import backtest_limit_entry as _bt
from backtest_limit_entry import fetch, run_limit_backtest
from scan_walkforward import load_universe

import check_signals_stop            as _stop
import check_signals_breakout        as _brk
import check_signals_short           as _short
import check_signals_short_breakout  as _sbrk

JST = timezone(timedelta(hours=9))

_FAMILY = {
    "stop":      (_stop,  ["MACD", "A7", "RSI2"]),
    "breakout":  (_brk,   ["DON", "VOL", "MOM"]),
    "short":     (_short, ["A7_S", "MACD_S", "RSI2_S"]),
    "short_brk": (_sbrk,  ["DON_S", "MOM_S", "GAP_S"]),
}

# family → watchlist_proposal の対応する属性名
_FAMILY_WL_ATTR = {
    "stop":      "STOP_WATCHLIST",
    "breakout":  "BRK_WATCHLIST",
    "short":     "SHORT_WATCHLIST",
    "short_brk": "SHORT_BRK_WATCHLIST",
}


def _load_watchlist_proposal(path: str, family: str) -> dict[str, list[tuple]]:
    """
    watchlist_proposal_*.py を読み込み、{戦略: [(symbol, name), ...]} を返す。
    family に対応する *_WATCHLIST 属性を使う。
    """
    import importlib.util
    from pathlib import Path as _P

    p = _P(path)
    spec = importlib.util.spec_from_file_location(p.stem.replace("-", "_"), p)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    attr = _FAMILY_WL_ATTR[family]
    wl   = getattr(mod, attr, None)
    if wl is None:
        raise RuntimeError(f"{path} に {attr} がありません")

    by_strat: dict[str, list[tuple]] = {}
    for sym, name, strat in wl:
        by_strat.setdefault(strat, []).append((sym, name))
    return by_strat


def _aggregate(trades: list[dict]) -> dict:
    """取引ログから集計指標を計算する。"""
    n = len(trades)
    if n == 0:
        return dict(trades=0, avg_hold=0.0, win_rate=0.0, pf=0.0,
                    total_pnl=0.0, pnl_per_day=0.0, total_hold_days=0,
                    n_target=0, n_stop=0, n_timeout=0)
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    total_hold = sum(t.get("hold_days", 0) for t in trades)
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    gp = sum(t["pnl"] for t in wins)
    gl = sum(abs(t["pnl"]) for t in trades if t.get("pnl", 0) <= 0)
    pf = (gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0)
    n_target  = sum(1 for t in trades if t.get("reason") == "目標達成")
    n_stop    = sum(1 for t in trades if t.get("reason") == "損切り")
    n_timeout = sum(1 for t in trades if t.get("reason") == "タイムカット")
    return dict(
        trades=n,
        avg_hold=total_hold / n,
        win_rate=len(wins) / n * 100,
        pf=pf,
        total_pnl=total_pnl,
        pnl_per_day=(total_pnl / total_hold) if total_hold > 0 else 0.0,
        total_hold_days=total_hold,
        n_target=n_target, n_stop=n_stop, n_timeout=n_timeout,
    )


def _run_symbol(sym, name, mod, strat, days, tm_override):
    calc_fn, em, sm, tm = mod.STRATEGY_PARAMS[strat]
    if tm_override is not None:
        tm = tm_override
    entry_type = getattr(mod, "ENTRY_TYPE", "stop")
    df = fetch(sym, days + 60)
    if df is None:
        return []
    try:
        r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm,
                               days, strat, entry_type=entry_type)
        return r.get("trade_log", []) or []
    except Exception:
        return []


def main() -> None:
    p = argparse.ArgumentParser(description="回転率パラメータ比較分析")
    p.add_argument("--family", choices=list(_FAMILY.keys()), default="short")
    p.add_argument("--max-hold", type=str, default="7,15",
                   help="比較する MAX_HOLD のカンマ区切りリスト (例: 3,5,7,10,15)")
    p.add_argument("--entry-expire", type=str, default=None,
                   help="比較する ENTRY_EXPIRE のカンマ区切り (省略時は現行値固定)")
    p.add_argument("--tm", type=float, default=None,
                   help="目標倍率を強制指定 (省略時は戦略ごとの最適値)")
    p.add_argument("--days", type=int, default=365, help="バックテスト期間")
    p.add_argument("--budget", type=float, default=0.0)
    p.add_argument("--max-price", type=float, default=0.0)
    p.add_argument("--limit", type=int, default=0, help="ユニバース先頭N銘柄に制限")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--symbols", type=str, default=None)
    p.add_argument("--watchlist", type=str, default=None,
                   help="watchlist_proposal_*.py を指定すると、選定済み銘柄だけを"
                        "戦略別に計測する (フル universe ではなく)")
    p.add_argument("--aggressive", action="store_true")
    args = p.parse_args()

    mod, strategies = _FAMILY[args.family]
    max_holds = [int(x) for x in args.max_hold.split(",")]
    expires   = ([int(x) for x in args.entry_expire.split(",")]
                 if args.entry_expire else [_bt.ENTRY_EXPIRE])

    eff_max_price = args.max_price
    if args.budget > 0 and args.max_price == 0:
        eff_max_price = args.budget / 100.0

    orig_max_hold = _bt.MAX_HOLD
    orig_expire   = _bt.ENTRY_EXPIRE

    # ── 銘柄ソース確定: watchlist proposal か、universe か ──
    # strat_symbols[strat] = [(sym, name), ...]
    strat_symbols: dict[str, list[tuple]] = {}
    if args.watchlist:
        by_strat = _load_watchlist_proposal(args.watchlist, args.family)
        # この family の戦略だけに絞る
        strategies = [s for s in strategies if by_strat.get(s)]
        for s in strategies:
            strat_symbols[s] = by_strat.get(s, [])
        src_label = f"watchlist: {args.watchlist}"
        n_total = sum(len(v) for v in strat_symbols.values())
    else:
        symbols, uni_name = load_universe(args.symbols)
        if args.limit > 0:
            symbols = symbols[: args.limit]
        # 価格フィルター
        if eff_max_price > 0:
            filtered = []
            for sym, name in symbols:
                df = fetch(sym, 5)
                if df is None or len(df) == 0:
                    continue
                if float(df.iloc[-1]["close"]) <= eff_max_price:
                    filtered.append((sym, name))
            symbols = filtered
        for s in strategies:
            strat_symbols[s] = symbols
        src_label = f"{uni_name} ({len(symbols)} 銘柄)"
        n_total = len(symbols)

    print("=" * 88)
    print(f"回転率パラメータ比較分析")
    print(f"  銘柄ソース : {src_label}")
    print(f"  family     : {args.family}  戦略: {', '.join(strategies)}")
    if args.watchlist:
        for s in strategies:
            print(f"    {s}: {len(strat_symbols[s])} 銘柄")
    print(f"  期間       : {args.days} 日")
    print(f"  tm         : {'戦略別最適値' if args.tm is None else args.tm}")
    print(f"  MAX_HOLD   : {max_holds}")
    print(f"  ENTRY_EXPIRE: {expires}")
    if eff_max_price > 0 and not args.watchlist:
        print(f"  価格上限   : {eff_max_price:,.0f}円/株")
    print("=" * 88)

    if n_total == 0:
        print("[ERROR] 対象銘柄が0件です。--watchlist の family 指定を確認してください。")
        sys.exit(1)

    # ── (MAX_HOLD, ENTRY_EXPIRE) × 全戦略でスイープ ──
    period_years = args.days / 365.0
    rows = []   # (max_hold, expire, strat or "ALL", agg)

    for me in max_holds:
        for ex in expires:
            _bt.MAX_HOLD     = me
            _bt.ENTRY_EXPIRE = ex

            combo_trades_all = []
            per_strat = {}
            for strat in strategies:
                strat_trades = []
                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    futs = [pool.submit(_run_symbol, sym, name, mod, strat,
                                        args.days, args.tm)
                            for sym, name in strat_symbols[strat]]
                    for f in as_completed(futs):
                        strat_trades.extend(f.result())
                per_strat[strat] = _aggregate(strat_trades)
                combo_trades_all.extend(strat_trades)

            rows.append((me, ex, "ALL", _aggregate(combo_trades_all)))
            for strat in strategies:
                rows.append((me, ex, strat, per_strat[strat]))

            a = rows[-1 - len(strategies)][3]  # ALL agg
            print(f"  MAX_HOLD={me:>2} EXPIRE={ex}: 取引{a['trades']:>5} "
                  f"平均保有{a['avg_hold']:>5.1f}日 PF{a['pf']:>5.2f} "
                  f"勝率{a['win_rate']:>5.1f}% 損益{a['total_pnl']:>+12,.0f} "
                  f"1日損益{a['pnl_per_day']:>+8,.0f}", flush=True)

    _bt.MAX_HOLD     = orig_max_hold
    _bt.ENTRY_EXPIRE = orig_expire

    # ── 結果テーブル ──
    print("\n" + "=" * 100)
    print("【詳細】 (ALL = 全戦略合算)")
    print("=" * 100)
    hdr = (f"{'MaxHold':>8}{'Expire':>7}{'戦略':>8}{'取引':>6}{'平均保有':>9}"
           f"{'勝率':>7}{'PF':>6}{'目標':>5}{'損切':>5}{'TC':>5}"
           f"{'総損益':>13}{'1日損益':>10}{'年間取引':>9}")
    print(hdr)
    print("-" * 100)
    for me, ex, strat, a in rows:
        yr_trades = a["trades"] / period_years if period_years > 0 else 0
        mark = " ★" if strat == "ALL" else ""
        print(f"{me:>8}{ex:>7}{strat:>8}{a['trades']:>6}"
              f"{a['avg_hold']:>8.1f}日{a['win_rate']:>6.1f}%{a['pf']:>6.2f}"
              f"{a['n_target']:>5}{a['n_stop']:>5}{a['n_timeout']:>5}"
              f"{a['total_pnl']:>+13,.0f}{a['pnl_per_day']:>+10,.0f}"
              f"{yr_trades:>8.0f}{mark}")

    print("\n【読み方】")
    print("  ・平均保有日数が短い   = 高回転 (資金が早く空く)")
    print("  ・1日損益 (総損益/延べ保有日数) = 資金効率。高回転の真の評価軸。")
    print("    → これが高い設定が「同じ資金で年間最も稼げる」")
    print("  ・MAX_HOLDを下げると タイムカット(TC)が増え、PF/勝率はやや低下しうる")
    print("  ・年間取引数 = 回転回数の目安 (同時保有数は考慮していない単純計算)")


if __name__ == "__main__":
    main()
