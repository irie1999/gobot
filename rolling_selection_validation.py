"""
rolling_selection_validation.py ― 現在と同じ選定を各基準月で再現する完全版ロールフォワード
==============================================================================
「現在の選定パイプラインと全く同じ設定で、データ打ち切りだけ基準月末にずらして
選定し直し、その先(=未知データ)の成績を比較する」ための厳密検証。

- 選定: scan_walkforward.walkforward_one_asof(as_of=基準月末) で、その時点までの
  データだけで WF 選定指標を計算 (未来データ遮断)。build_watchlist と同じフィルタ
  (folds_passed≥2 / total_test_pnl>0 / 価格) で戦略ごと上位を選ぶ。
  ※ walkforward_one_asof は MaxDD/Sharpe を返さないため、その2条件のみ近似で省略。
     並べ替えは total_test_pnl(composite の主成分)で代用。
- フォワード: 選定した (銘柄×戦略) を通しでバックテストし、基準月より後(signal_dt>M)
  のトレードだけを月次集計 = 完全な OOS。
- これを各基準月で行い、比較表を出力。既存CSVも今日のBTも使わない=後知恵なし。

【重さ】 walkforward_one_asof は 1候補あたり 4fold×2窓×約3年 を計算するため重い。
  監視銘柄union(既定)なら数分。--symbols で全上場を指定すると数時間規模。
  まず --limit で少数・少数基準月で動作確認してから全量推奨。

【使い方】
  python rolling_selection_validation.py                      # 監視union / 既定
  python rolling_selection_validation.py --start 2026-03 --per-strategy 10
  python rolling_selection_validation.py --symbols symbols_listed_prime.py --limit 200 --workers 8
  python rolling_selection_validation.py --aggressive --max-price 6000 --min-price 1000
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

import scan_walkforward as _swf
import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import fetch, run_limit_backtest

JST = timezone(timedelta(hours=9))


def _load_symbols_file(path: str) -> list[str]:
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
    """検証する (銘柄, 戦略)。symbols_file 指定時は全上場×全戦略(重い)。
    未指定は監視銘柄union(候補プール)。戦略は STRATEGY_DEFS にあるものだけ。"""
    strats = [s for s in _swf.STRATEGY_DEFS.keys()]
    pairs: list[tuple[str, str]] = []
    if symbols_file:
        syms = _load_symbols_file(symbols_file)
        if limit and limit > 0:
            syms = syms[:limit]
        for s in syms:
            for st in strats:
                pairs.append((s, st))
    else:
        for mod in (_stop, _brk):
            for sym, _name, strat in getattr(mod, "WATCHLIST", []):
                s = sym if str(sym).endswith(".T") else f"{sym}.T"
                if strat in _swf.STRATEGY_DEFS:
                    pairs.append((s, strat))
        pairs = sorted(set(pairs))
        if limit and limit > 0:
            pairs = pairs[:limit]
    return pairs


def _month_ends(start: date, end: date) -> list[date]:
    outs = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        me = date(ny, nm, 1) - timedelta(days=1)
        if me <= end:
            outs.append(me)
        y, m = ny, nm
    return outs


def _forward_trades(sym: str, strat: str, hist_days: int) -> list[dict]:
    """(sym,strat) を通しバックテストし trade_log を返す(フォワード集計用)。"""
    if strat not in _swf.STRATEGY_DEFS:
        return []
    calc_fn, em, sm, tm, family, entry_type = _swf.STRATEGY_DEFS[strat]
    df = fetch(sym, hist_days)
    if df is None or getattr(df, "empty", True):
        return []
    r = run_limit_backtest(sym, sym, df, calc_fn, em, sm, tm, hist_days, strat,
                           entry_type=entry_type)
    return r.get("trade_log", []) if r else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="候補ユニバース.py(全上場)。未指定は監視union")
    ap.add_argument("--start", default="2026-01", help="最古の基準月 (YYYY-MM)")
    ap.add_argument("--per-strategy", type=int, default=10, help="戦略あたり選定数(build_watchlist準拠)")
    ap.add_argument("--min-folds", type=int, default=2, help="folds_passed 下限(build_watchlist準拠)")
    ap.add_argument("--max-price", type=float, default=0.0)
    ap.add_argument("--min-price", type=float, default=0.0)
    ap.add_argument("--hist-days", type=int, default=730, help="フォワード用バックテスト履歴日数")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    today = datetime.now(JST).date()
    _sy, _sm = map(int, args.start.split("-"))
    base_dates = [d for d in _month_ends(date(_sy, _sm, 1), today) if (today - d).days >= 5]
    pairs = _candidate_pairs(args.symbols, args.limit)
    mode = "aggressive" if args.aggressive else "conservative"

    print("=" * 74)
    print(f"完全版ロールフォワード(現在と同じWF選定を各基準月で再現) / mode={mode}")
    print(f"候補 {len(pairs)}(銘柄×戦略) / 基準月 {base_dates[0]}〜{base_dates[-1]}({len(base_dates)}件)"
          f" / per_strategy={args.per_strategy} folds≥{args.min_folds}")
    print("※ walkforward_one_asof は重い。監視unionで数分 / 全上場は数時間")
    print("=" * 74)

    # ── フォワード用 trade_log を候補ごとに1回だけ取得 ──
    fwd_logs: dict[tuple, list] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_forward_trades, s, st, args.hist_days): (s, st) for s, st in pairs}
        done = 0
        for fut in as_completed(futs):
            k = futs[fut]; done += 1
            try:
                fwd_logs[k] = fut.result()
            except Exception:
                fwd_logs[k] = []
            if done % 50 == 0 or done == len(pairs):
                print(f"  forward backtest {done}/{len(pairs)}", flush=True)

    # ── 各基準月: as-of選定 → フォワード集計 ──
    result = {}
    for D in base_dates:
        # as-of D の WF 指標を全候補で計算
        asof_rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_swf.walkforward_one_asof, s, s, st, D): (s, st)
                    for s, st in pairs}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r:
                    asof_rows.append(r)
        # build_watchlist 準拠フィルタ
        elig = []
        for r in asof_rows:
            if int(r.get("folds_passed", 0)) < args.min_folds:
                continue
            if float(r.get("total_test_pnl", 0)) <= 0:
                continue
            price = float(r.get("latest_price", 0))
            if args.max_price > 0 and price > args.max_price:
                continue
            if args.min_price > 0 and price < args.min_price:
                continue
            elig.append(r)
        # 戦略ごと total_test_pnl 降順 上位 per_strategy (composite の主成分で代用)
        by_strat: dict = defaultdict(list)
        for r in elig:
            by_strat[r["strategy"]].append(r)
        selected = []
        for st, rows in by_strat.items():
            rows.sort(key=lambda x: float(x.get("total_test_pnl", 0)), reverse=True)
            for r in rows[:args.per_strategy]:
                selected.append((r["symbol"], r["strategy"]))
        # フォワード集計 (signal_dt > D)
        fwd = defaultdict(list)
        for (sym, strat) in selected:
            for t in fwd_logs.get((sym, strat), []):
                sd = t.get("signal_dt")
                sd = sd.date() if hasattr(sd, "date") else sd
                if sd is None or sd <= D or t.get("reason") in ("発注中", "保有中"):
                    continue
                fwd[sd.strftime("%Y-%m")].append(t["pnl"])
        result[D] = {"sel": selected, "fwd": fwd}
        print(f"  基準 {D}: 選定 {len(selected)}銘柄×戦略", flush=True)

    # ── 出力 ──
    all_months = sorted({m for D in base_dates for m in result[D]["fwd"]})
    print()
    hdr = f"{'基準月末':<12}{'選定':>5} | " + " ".join(f"{m[5:]+'月':>15}" for m in all_months) + f" | {'OOS計':>20}"
    print(hdr)
    print("-" * len(hdr))
    for D in base_dates:
        r = result[D]
        cells = []
        allp = []
        dm = D.strftime("%Y-%m")
        for m in all_months:
            if m <= dm:
                cells.append(f"{'·':>15}")
                continue
            pl = r["fwd"].get(m, [])
            if not pl:
                cells.append(f"{'—':>15}")
                continue
            p = sum(pl); w = sum(1 for x in pl if x > 0)
            allp.extend(pl)
            cells.append(f"{p:+>9,.0f}({len(pl)}/{w})".rjust(15))
        tp = sum(allp); tn = len(allp); tw = sum(1 for x in allp if x > 0)
        twr = tw / tn * 100 if tn else 0
        print(f"{str(D):<12}{len(r['sel']):>5} | " + " ".join(cells) +
              f" | {tp:+>11,.0f} ({tn}件 {twr:.0f}%)")
    print("-" * len(hdr))
    print("各セル: OOS損益(取引数/勝ち)  ·=基準月以前(選定に使用)  —=取引なし")
    print("選定=その基準月末までのデータだけでWF選定(未来遮断)。現在と同じ選定条件を各月で再現。")


if __name__ == "__main__":
    main()
