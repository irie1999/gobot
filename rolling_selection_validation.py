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
  python rolling_selection_validation.py --both --price-ranges 6000,10000 --min-price 1000
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TRADING_MODE", "conservative")

import pandas as _pd
import scan_walkforward as _swf
import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import fetch, run_limit_backtest, INITIAL_CASH
from risk_metrics import enrich_backtest_result

JST = timezone(timedelta(hours=9))

# レポート(run_signals_holdout_all)の選定に使う戦略セットと同一
LONG_STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]
SHORT_STRATS = ["A7_S", "RSI2_S", "MACD_S", "DON_S", "MOM_S", "GAP_S"]

# con / agg の STRATEGY_DEFS (レポートは両モードを選定してunion)
CON_DEFS = _swf.STRATEGY_DEFS_CONSERVATIVE
AGG_DEFS = _swf.STRATEGY_DEFS_AGGRESSIVE


def _wf_asof(sym: str, strat: str, asof: date, defs: dict) -> dict | None:
    """レポートと同じ FOLDS(2年/2fold) を基準日 asof に当てて WF 選定指標を計算。
    holdout=today-asof で FOLDS をずらすのと数学的に等価(未来遮断)。MaxDD/Sharpe は
    全testfoldのtrade_logを結合して enrich_backtest_result で計算(walkforward_one と同一)。"""
    if strat not in defs:
        return None
    calc_fn, em, sm, tm, family, entry_type = defs[strat]
    need = (_swf.TODAY - asof).days + 800
    df = fetch(sym, need)
    if df is None or len(df) < 400:
        return None
    df = df[df.index <= _pd.Timestamp(asof)].copy()
    if len(df) < 200:
        return None
    try:
        price = float(df.iloc[-1]["close"])
    except Exception:
        return None
    if price <= 0:
        return None
    test_results: list[dict] = []
    folds_passed = 0
    for _fname, ts, te, vs, ve in _swf.FOLDS:
        tr = _swf._run_window_abs(sym, sym, df, calc_fn, em, sm, tm,
                                  asof - timedelta(days=ts), asof - timedelta(days=te),
                                  strat, entry_type=entry_type)
        vr = _swf._run_window_abs(sym, sym, df, calc_fn, em, sm, tm,
                                  asof - timedelta(days=vs), asof - timedelta(days=ve),
                                  strat, entry_type=entry_type)
        if _swf._passes_train(tr) and _swf._passes_test(vr):
            folds_passed += 1
        if vr:
            test_results.append(vr)
    if not test_results:
        return None
    # walkforward_one と同一: 全testfoldのtrade_logを結合して1回リスク指標を計算
    all_test_trades: list[dict] = []
    tpnl = 0.0
    for r in test_results:
        all_test_trades.extend(r.get("trade_log", []))
        tpnl += r.get("total_pnl", 0.0)
    agg = enrich_backtest_result({"trade_log": all_test_trades}, INITIAL_CASH)
    return dict(symbol=sym, strategy=strat, folds_passed=folds_passed,
                total_test_pnl=tpnl, latest_price=price,
                max_drawdown_pct=agg.get("max_drawdown_pct", 0.0),
                sharpe=agg.get("sharpe", 0.0))


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


def _auto_universe_file() -> str | None:
    """レポートと同じユニバース(プライム優先)を自動検出。"""
    for name in ("symbols_listed_prime.py", "symbols_listed_all.py",
                 "symbols_listed_standard.py", "symbols_all.py"):
        if os.path.exists(name):
            return name
    return None


def _candidate_pairs(symbols_file: str | None, limit: int,
                     strats: list) -> tuple[list[tuple[str, str]], str]:
    """検証する (銘柄, 戦略) と使ったユニバース名を返す。
    strats はレポートと同一の戦略セット。
    symbols_file 未指定なら symbols_listed_prime.py を自動検出(=レポートと同じ母集団)。"""
    strats = [s for s in strats if s in _swf.STRATEGY_DEFS]
    src = symbols_file or _auto_universe_file()
    if src:
        syms = _load_symbols_file(src)
        if limit and limit > 0:
            syms = syms[:limit]
        pairs = [(s, st) for s in syms for st in strats]
        return pairs, src
    # フォールバック: 監視銘柄union(母集団がレポートと違う=真の一致ではない)
    syms = set()
    for mod in (_stop, _brk):
        for sym, _name, _strat in getattr(mod, "WATCHLIST", []):
            syms.add(sym if str(sym).endswith(".T") else f"{sym}.T")
    pairs = sorted((s, st) for s in syms for st in strats)
    if limit and limit > 0:
        pairs = pairs[:limit]
    return pairs, "監視union(※母集団がレポートと不一致)"


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


def _forward_trades(sym: str, strat: str, hist_days: int, defs: dict) -> list[dict]:
    """(sym,strat) を通しバックテストし trade_log を返す(フォワード集計用)。"""
    if strat not in defs:
        return []
    calc_fn, em, sm, tm, family, entry_type = defs[strat]
    df = fetch(sym, hist_days)
    if df is None or getattr(df, "empty", True):
        return []
    r = run_limit_backtest(sym, sym, df, calc_fn, em, sm, tm, hist_days, strat,
                           entry_type=entry_type)
    return r.get("trade_log", []) if r else []


def _write_html(path: str, result: dict, base_dates: list, all_months: list, meta: str):
    """現行レポート風(ダークテーマ・色付き)のロールフォワードOOS表をHTML出力。"""
    def _cell(pl):
        p = sum(pl); w = sum(1 for x in pl if x > 0); n = len(pl)
        c = "#4ade80" if p > 0 else ("#f87171" if p < 0 else "#94a3b8")
        return (f'<td style="text-align:right;color:{c};font-weight:700">{p:+,.0f}'
                f'<br><span style="font-size:.68rem;color:#94a3b8">{n}件 {w}勝</span></td>')
    month_h = "".join(f"<th>{m[5:]}月</th>" for m in all_months)
    rows = ""
    diag_t = diag_n = diag_w = 0
    for D in base_dates:
        dm = D.strftime("%Y-%m")
        r = result[D]
        cells = ""
        allp = []
        for m in all_months:
            if m <= dm:
                cells += '<td style="color:#334155;text-align:center">·</td>'
                continue
            pl = r["fwd"].get(m, [])
            if not pl:
                cells += '<td style="color:#475569;text-align:center">—</td>'
                continue
            allp.extend(pl)
            cells += _cell(pl)
        nxt = next((m for m in all_months if m > dm), None)
        if nxt and r["fwd"].get(nxt):
            dpl = r["fwd"][nxt]
            diag_t += sum(dpl); diag_n += len(dpl); diag_w += sum(1 for x in dpl if x > 0)
        tp = sum(allp); tn = len(allp); tw = sum(1 for x in allp if x > 0)
        twr = tw / tn * 100 if tn else 0
        tc = "#4ade80" if tp > 0 else ("#f87171" if tp < 0 else "#94a3b8")
        rows += (f'<tr><td style="text-align:left;white-space:nowrap">{dm} まで選定'
                 f'<br><span style="font-size:.65rem;color:#94a3b8">{len(r["sel"])}件</span></td>'
                 f'{cells}<td style="text-align:right;color:{tc};font-weight:700">{tp:+,.0f}円'
                 f'<br><span style="font-size:.65rem;color:#94a3b8">{tn}件 {twr:.0f}%</span></td></tr>')
    dwr = diag_w / diag_n * 100 if diag_n else 0
    dc = "#4ade80" if diag_t > 0 else "#f87171"
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>ロールフォワードOOS検証</title>
<style>
 body{{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',Meiryo,sans-serif;padding:18px}}
 h2{{color:#60a5fa}} .meta{{color:#94a3b8;font-size:.82rem;margin:4px 0 12px;line-height:1.6}}
 table{{border-collapse:collapse;margin-top:10px}}
 th,td{{border:1px solid #334155;padding:5px 9px;font-size:.8rem;white-space:nowrap}}
 th{{background:#1e293b;color:#cbd5e1}} .note{{color:#94a3b8;font-size:.76rem;margin-top:8px;line-height:1.6}}
 .summary{{background:#1e293b;border-left:4px solid {dc};padding:10px 14px;margin:10px 0;font-size:.95rem}}
</style></head><body>
<h2>★ ロールフォワードOOS検証（後知恵ゼロ / 現行と同一選定を各基準月で再現）</h2>
<p class="meta">{meta}</p>
<div class="summary"><b>翌月OOS合計（非重複・これが最重要）:</b>
 <b style="color:{dc};font-size:1.2rem">{diag_t:+,.0f}円</b> &nbsp; {diag_n}件 勝率{dwr:.0f}%
 <br><span style="color:#94a3b8;font-size:.8rem">各基準月末で選定→翌月だけのOOSを合算(重複なし)。これがプラスなら選定は時期を越えて機能。</span></div>
<div style="overflow-x:auto"><table>
<thead><tr><th style="text-align:left">選定基準月</th>{month_h}<th>全OOS計</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p class="note">各セル = OOS損益(取引数/勝ち)。<span style="color:#334155">·</span>=基準月以前(選定に使用) / —=取引なし。<br>
 <b>「全OOS計」列は各行で期間が重複するので足し算しないこと。</b>非重複で信頼できるのは上の「翌月OOS合計」。</p>
</body></html>"""
    Path(path).write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None,
                    help="候補ユニバース.py。未指定は symbols_listed_prime.py 自動検出(=レポートと同じ母集団)")
    ap.add_argument("--short", action="store_true", help="ショート側のみ(SHORT_STRATS)")
    ap.add_argument("--both", action="store_true", help="ロング+ショート同時(レポート --both 準拠)")
    ap.add_argument("--start", default="2026-01", help="最古の基準月 (YYYY-MM)")
    ap.add_argument("--per-strategy", type=int, default=10, help="戦略あたり選定数(レポート準拠)")
    ap.add_argument("--max-dd", type=float, default=15.0, help="MaxDD上限パーセント(レポート準拠)")
    ap.add_argument("--min-folds", type=int, default=2, help="folds_passed下限(scan_walkforward CSV準拠=2)")
    ap.add_argument("--price-ranges", default=None,
                    help="価格上限をカンマ区切りで複数(例 6000,10000)。各上限で選定しunion(レポート準拠)")
    ap.add_argument("--max-price", type=float, default=0.0, help="価格上限(--price-ranges 未指定時)")
    ap.add_argument("--min-price", type=float, default=0.0)
    ap.add_argument("--hist-days", type=int, default=730, help="フォワード用バックテスト履歴日数")
    ap.add_argument("--workers", type=int, default=3, help="並列数(裏でデイトレ稼働中は控えめ推奨)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--con-only", action="store_true", help="conservativeのみ(既定はcon+agg両方)")
    ap.add_argument("--agg-only", action="store_true", help="aggressiveのみ(既定はcon+agg両方)")
    ap.add_argument("--no-browser", action="store_true", help="HTMLをブラウザで開かない")
    args = ap.parse_args()

    today = datetime.now(JST).date()
    _sy, _sm = map(int, args.start.split("-"))
    base_dates = [d for d in _month_ends(date(_sy, _sm, 1), today) if (today - d).days >= 5]
    if args.both:
        strats, side = LONG_STRATS + SHORT_STRATS, "LONG+SHORT"
    elif args.short:
        strats, side = SHORT_STRATS, "SHORT"
    else:
        strats, side = LONG_STRATS, "LONG"
    # モード: レポートは con+agg 両方を選定してunion。既定で両方。
    if args.con_only:
        modes = [("con", CON_DEFS)]
    elif args.agg_only:
        modes = [("agg", AGG_DEFS)]
    else:
        modes = [("con", CON_DEFS), ("agg", AGG_DEFS)]
    # 価格上限リスト(レポート --price-ranges 準拠): 各上限で選定しunion
    if args.price_ranges:
        ceilings = [float(x) for x in args.price_ranges.split(",") if x.strip()]
    else:
        ceilings = [args.max_price]
    pairs, src = _candidate_pairs(args.symbols, args.limit, strats)

    def _composite(r):
        return float(r.get("total_test_pnl", 0)) * (1.0 + max(float(r.get("sharpe", 0)), 0.0))

    print("=" * 74)
    print(f"完全版ロールフォワード(現在と同じWF選定を各基準月で再現) / {side}")
    print(f"モード: {[m for m, _ in modes]} (レポートは両方union) / ユニバース: {src}")
    print(f"戦略: {strats}")
    print(f"価格上限: {ceilings} (min={args.min_price}) / per_strategy={args.per_strategy} "
          f"MaxDD≤{args.max_dd} folds≥{args.min_folds}")
    print(f"候補 {len(pairs)}×{len(modes)}モード / 基準月 {base_dates[0]}〜{base_dates[-1]}({len(base_dates)}件)")
    print(f"※ 重い。裏でデイトレ稼働中は workers={args.workers}(控えめ)。まず --limit で確認を")
    print("=" * 74)

    # ── フォワード用 trade_log: (sym, strat, mode) ごとに1回 ──
    fwd_tasks = [(s, st, mn, dfs) for s, st in pairs for mn, dfs in modes]
    fwd_logs: dict[tuple, list] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_forward_trades, s, st, args.hist_days, dfs): (s, st, mn)
                for s, st, mn, dfs in fwd_tasks}
        done = 0
        for fut in as_completed(futs):
            k = futs[fut]; done += 1
            try:
                fwd_logs[k] = fut.result()
            except Exception:
                fwd_logs[k] = []
            if done % 100 == 0 or done == len(fwd_tasks):
                print(f"  forward backtest {done}/{len(fwd_tasks)}", flush=True)

    # ── 各基準月: 各モードで as-of選定 → (sym,strat,mode) union → フォワード集計 ──
    result = {}
    for D in base_dates:
        selected_set = set()
        for mn, dfs in modes:
            asof_rows: list[dict] = []
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(_wf_asof, s, st, D, dfs): (s, st) for s, st in pairs}
                for fut in as_completed(futs):
                    try:
                        r = fut.result()
                    except Exception:
                        r = None
                    if r:
                        asof_rows.append(r)
            # 選定フィルタ (scan_walkforward CSV + _load_wl_from_csv 準拠)
            base_elig = [r for r in asof_rows
                         if int(r.get("folds_passed", 0)) >= args.min_folds
                         and float(r.get("total_test_pnl", 0)) > 0
                         and float(r.get("max_drawdown_pct", 0)) <= args.max_dd
                         and (args.min_price <= 0 or float(r.get("latest_price", 0)) >= args.min_price)]
            for ceil in ceilings:
                elig = [r for r in base_elig
                        if ceil <= 0 or float(r.get("latest_price", 0)) <= ceil]
                by_strat: dict = defaultdict(list)
                for r in elig:
                    by_strat[r["strategy"]].append(r)
                for st, rows in by_strat.items():
                    rows.sort(key=_composite, reverse=True)
                    for r in rows[:args.per_strategy]:
                        selected_set.add((r["symbol"], r["strategy"], mn))
        # フォワード集計 (signal_dt > D)。con/agg は別設定として両方計上(レポート準拠)
        fwd = defaultdict(list)
        for (sym, strat, mn) in selected_set:
            for t in fwd_logs.get((sym, strat, mn), []):
                sd = t.get("signal_dt")
                sd = sd.date() if hasattr(sd, "date") else sd
                if sd is None or sd <= D or t.get("reason") in ("発注中", "保有中"):
                    continue
                fwd[sd.strftime("%Y-%m")].append(t["pnl"])
        result[D] = {"sel": sorted(selected_set), "fwd": fwd}
        print(f"  基準 {D}: 選定 {len(selected_set)}(銘柄×戦略×モード)", flush=True)

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
            cells.append(f"{p:+,.0f}({len(pl)}/{w})".rjust(15))
        tp = sum(allp); tn = len(allp); tw = sum(1 for x in allp if x > 0)
        twr = tw / tn * 100 if tn else 0
        print(f"{str(D):<12}{len(r['sel']):>5} | " + " ".join(cells) +
              f" | {tp:+,.0f} ({tn}件 {twr:.0f}%)".rjust(22))
    print("-" * len(hdr))
    print("各セル: OOS損益(取引数/勝ち)  ·=基準月以前(選定に使用)  —=取引なし")
    print("選定=その基準月末までのデータだけでWF選定(未来遮断)。現在と同じ選定条件を各月で再現。")

    # ── HTML出力(現行レポート風・文字化けしない) ──
    _meta = (f"{side} / モード {[m for m, _ in modes]} / ユニバース {src} / "
             f"戦略 {strats}<br>価格上限 {ceilings} (min={args.min_price}) / "
             f"per_strategy={args.per_strategy} MaxDD≤{args.max_dd} folds≥{args.min_folds} / "
             f"基準月 {base_dates[0]}〜{base_dates[-1]}")
    _html_path = f"rolling_oos_{today}.html"
    _write_html(_html_path, result, base_dates, all_months, _meta)
    print(f"\nHTML出力: {os.path.abspath(_html_path)}")

    # ── JSONキャッシュ (run_signals_holdout_all の詳細分析タブが読む) ──
    _cache = {
        "generated": str(today), "meta": _meta,
        "months": list(all_months),
        "base_months": [D.strftime("%Y-%m") for D in base_dates],
        "cells": {},
    }
    for D in base_dates:
        dm = D.strftime("%Y-%m")
        r = result[D]
        fwd = {}
        for m, pl in r["fwd"].items():
            fwd[m] = {"pnl": sum(pl), "n": len(pl),
                      "w": sum(1 for x in pl if x > 0)}
        _cache["cells"][dm] = {"sel": len(r["sel"]), "fwd": fwd}
    try:
        Path("rolling_oos_cache.json").write_text(
            json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
        print(f"キャッシュ出力: {os.path.abspath('rolling_oos_cache.json')} "
              f"(run_signals_holdout_all の詳細分析タブが読みます)")
    except Exception as _e:
        print(f"[WARN] キャッシュ書込失敗: {_e}")
    if not args.no_browser:
        try:
            webbrowser.open("file://" + os.path.abspath(_html_path))
        except Exception:
            pass


if __name__ == "__main__":
    main()
