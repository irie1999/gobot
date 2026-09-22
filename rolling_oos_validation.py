"""
rolling_oos_validation.py ― 後知恵ゼロの厳密ロールフォワードOOS検証
==============================================================================
既存の walkforward CSV も「今日のBTスコア」も一切使わない。各基準月について、
その時点(=基準月末)までのデータだけで BTスコアを計算して銘柄を選別し、
基準月より後(=完全な未知データ)の成績を集計する。

レポート内蔵のロールフォワードは「07-01選定のwatchlistを窓で切る」「BTは今日の値」
という省略があり、後知恵バイアスが混入していた。本スクリプトはそれを排除する:

  1. 候補ユニバースの各 (銘柄 × 戦略) を、長期(既定730日)で 1回だけバックテスト。
  2. その trade_log を基準日 D で2分割:
       - exit_dt ≤ D の決済トレード → D 時点の period_results → その時点BT (point-in-time)
       - signal_dt > D のトレード      → フォワードOOS (D 以降の未知データ)
  3. 各基準月 D について「D時点BT ≥ 閾値」の銘柄だけを選び、
     フォワードOOSを月次集計。BTは必ず D 以前のデータのみで計算されるので後知恵なし。

【使い方】
  # 監視銘柄union(高速・既定)で BT≥70、基準=各月末
  python rolling_oos_validation.py --bt-min 70

  # 全上場から選定(本来の姿・重い)。まず --limit で試す
  python rolling_oos_validation.py --symbols symbols_listed_prime.py --limit 300 --bt-min 70
  python rolling_oos_validation.py --symbols symbols_listed_prime.py --bt-min 70 --workers 8

  # aggressive モード / 閾値変更 / 期間変更
  python rolling_oos_validation.py --aggressive --bt-min 60 --hist-days 900
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

# --aggressive を import 前に環境変数へ (check_signals_* がトップで読む)
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import fetch, run_limit_backtest

JST = timezone(timedelta(hours=9))
PERIODS = [30, 90, 180, 365]   # BTスコアの窓 (check_signals と同一)


def _load_symbols_file(path: str) -> list[str]:
    """SYMBOLS=[(code,name),...] or [code,...] の .py から yfコード配列を返す。"""
    try:
        spec = importlib.util.spec_from_file_location("uni", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[WARN] {path} 読込失敗: {e}")
        return []
    for attr in ("SYMBOLS", "symbols"):
        lst = getattr(mod, attr, None)
        if lst:
            out = []
            for x in lst:
                c = str(x[0] if isinstance(x, (tuple, list)) else x)
                out.append(c if c.endswith(".T") else f"{c}.T")
            return out
    return []


def _candidate_pairs(symbols_file: str | None, limit: int) -> list[tuple[str, str]]:
    """検証する (銘柄, 戦略) の候補。symbols_file 指定時は全戦略×全銘柄。
    未指定なら監視銘柄union (高速)。"""
    pairs: list[tuple[str, str]] = []
    if symbols_file:
        syms = _load_symbols_file(symbols_file)
        if limit and limit > 0:
            syms = syms[:limit]
        strto = list(_stop.STRATEGY_PARAMS.keys()) + list(_brk.STRATEGY_PARAMS.keys())
        for s in syms:
            for st in strto:
                pairs.append((s, st))
    else:
        # 監視銘柄union (候補プール。選定自体は各基準月のBTで都度やり直す)
        for mod in (_stop, _brk):
            for sym, _name, strat in getattr(mod, "WATCHLIST", []):
                pairs.append((sym if str(sym).endswith(".T") else f"{sym}.T", strat))
        pairs = sorted(set(pairs))
        if limit and limit > 0:
            pairs = pairs[:limit]
    return pairs


def _params_and_mod(strat: str):
    if strat in _brk.STRATEGY_PARAMS:
        return _brk.STRATEGY_PARAMS[strat], _brk
    if strat in _stop.STRATEGY_PARAMS:
        return _stop.STRATEGY_PARAMS[strat], _stop
    return None, None


def _pit_period_results(trade_log: list[dict], asof: date) -> dict:
    """asof 時点で"既に分かっている"決済トレード(exit_dt ≤ asof)だけを使い、
    30/90/180/365日窓の period_results を作る(=その時点BTの入力)。"""
    pr: dict[int, dict] = {}
    for days in PERIODS:
        lo = asof - timedelta(days=days)
        sub = []
        for t in trade_log:
            ed = t.get("exit_dt")
            ed = ed.date() if hasattr(ed, "date") else ed
            if ed is None or t.get("reason") in ("発注中", "保有中"):
                continue
            if lo <= ed <= asof:
                sub.append(t)
        n = len(sub)
        if n == 0:
            continue
        wins = sum(1 for t in sub if t["pnl"] > 0)
        gp = sum(t["pnl"] for t in sub if t["pnl"] > 0)
        gl = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[days] = {"trades": n, "win_rate": wins / n * 100,
                    "pf": pf, "total_pnl": sum(t["pnl"] for t in sub)}
    return pr


def _backtest_one(sym: str, strat: str, hist_days: int):
    """(sym, strat) を hist_days 分バックテストし trade_log を返す。失敗時 None。"""
    (calc_fn, em, sm, tm), mod = _params_and_mod(strat)
    if calc_fn is None:
        return None
    df = fetch(sym, hist_days)
    if df is None or getattr(df, "empty", True):
        return None
    r = run_limit_backtest(sym, sym, df, calc_fn, em, sm, tm, hist_days, strat,
                           entry_type=mod.ENTRY_TYPE)
    if not r:
        return None
    return r.get("trade_log", [])


def _month_ends(start: date, end: date) -> list[date]:
    """start〜end の各月末日(=基準日)を返す。"""
    outs = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        # 翌月1日 - 1日 = 月末
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        me = date(ny, nm, 1) - timedelta(days=1)
        if me <= end:
            outs.append(me)
        y, m = ny, nm
    return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None,
                    help="候補ユニバースの.py (例 symbols_listed_prime.py)。未指定は監視銘柄union")
    ap.add_argument("--bt-min", type=int, default=70, help="選定するBTスコア下限(その時点BT)")
    ap.add_argument("--hist-days", type=int, default=730, help="1銘柄のバックテスト履歴日数")
    ap.add_argument("--start", default="2026-01", help="最古の基準月 (YYYY-MM)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="候補を先頭N件に制限(動作確認)")
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    today = datetime.now(JST).date()
    # 基準月: start 〜 前月末 (今月は途中なので基準にしない)
    _sy, _sm = map(int, args.start.split("-"))
    base_dates = _month_ends(date(_sy, _sm, 1), today)
    # 直近の基準は「フォワードが最低数日ある」ように前月末まで
    base_dates = [d for d in base_dates if (today - d).days >= 5]

    pairs = _candidate_pairs(args.symbols, args.limit)
    mode = "aggressive" if args.aggressive else "conservative"
    print("=" * 70)
    print(f"厳密ロールフォワードOOS検証 (後知恵なし) / mode={mode} / BT≥{args.bt_min}")
    print(f"候補: {len(pairs)} (銘柄×戦略) / 履歴{args.hist_days}日 / "
          f"基準月: {base_dates[0]}〜{base_dates[-1]} ({len(base_dates)}件)")
    print("=" * 70)

    # 各 (sym,strat) を1回バックテスト → trade_log をメモリに保持
    logs: dict[tuple, list] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_backtest_one, s, st, args.hist_days): (s, st) for s, st in pairs}
        for fut in as_completed(futs):
            key = futs[fut]
            done += 1
            try:
                tl = fut.result()
            except Exception:
                tl = None
            if tl:
                logs[key] = tl
            if done % 50 == 0 or done == len(pairs):
                print(f"  backtest {done}/{len(pairs)} (有効 {len(logs)})", flush=True)

    # 基準月 × フォワード月 の集計
    # base -> {"sel": set, "fwd": {month: [pnl,...]}}
    from collections import defaultdict
    result = {}
    for D in base_dates:
        selected = []
        fwd = defaultdict(list)
        for (sym, strat), tl in logs.items():
            pr = _pit_period_results(tl, D)
            if not pr:
                continue
            score, _rank = _brk.calc_recommend_score(pr)  # stop/breakout 同一実装
            if score < args.bt_min:
                continue
            selected.append((sym, strat))
            for t in tl:
                sd = t.get("signal_dt")
                sd = sd.date() if hasattr(sd, "date") else sd
                if sd is None or sd <= D or t.get("reason") in ("発注中", "保有中"):
                    continue
                fwd[sd.strftime("%Y-%m")].append(t["pnl"])
        result[D] = {"sel": selected, "fwd": fwd}

    # 出力
    all_months = sorted({m for d in base_dates for m in result[d]["fwd"]})
    print()
    hdr = f"{'基準月末':<12}{'選定数':>6} | " + " ".join(f"{m[5:]+'月':>13}" for m in all_months) + f" | {'OOS計':>18}"
    print(hdr)
    print("-" * len(hdr))
    for D in base_dates:
        r = result[D]
        sel_n = len(r["sel"])
        cells = []
        allp = []
        for m in all_months:
            if m <= D.strftime("%Y-%m"):
                cells.append(f"{'·':>13}")
                continue
            pl = r["fwd"].get(m, [])
            if not pl:
                cells.append(f"{'—':>13}")
                continue
            p = sum(pl); w = sum(1 for x in pl if x > 0)
            allp.extend(pl)
            cells.append(f"{p:+>8,.0f}({len(pl)}/{w})".rjust(13))
        tp = sum(allp); tn = len(allp); tw = sum(1 for x in allp if x > 0)
        twr = tw / tn * 100 if tn else 0
        print(f"{str(D):<12}{sel_n:>6} | " + " ".join(cells) +
              f" | {tp:+>10,.0f} ({tn}件 {twr:.0f}%)")
    print("-" * len(hdr))
    print("各セル: OOS損益(取引数/勝ち)  ·=基準月以前  —=取引なし")
    print(f"選定数 = その基準月末時点のBT≥{args.bt_min}を満たした(銘柄×戦略)数。"
          f"全て D 以前のデータのみでBT計算=後知恵なし。")


if __name__ == "__main__":
    main()
