"""
analyze_trade_quality.py  ─  スコア×損益品質の分析
=======================================================
365日バックテストの全トレードを収集し:
  1. スコア帯別の 損切り / 目標達成 / タイムカット 内訳
  2. 損切りが連続している期間の検出・集計

Usage:
  python analyze_trade_quality.py               # conservative (デフォルト)
  python analyze_trade_quality.py --aggressive  # aggressive モード
  python analyze_trade_quality.py --days 180    # 期間変更
  python analyze_trade_quality.py --html        # HTMLレポートも出力
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
import itertools

import pandas as pd


# ── モード設定 (import より前に env をセット) ──────────────────────
def _setup_mode() -> str:
    mode = "aggressive" if "--aggressive" in sys.argv else \
           os.getenv("TRADING_MODE", "conservative")
    os.environ["TRADING_MODE"] = mode
    return mode


_MODE = _setup_mode()

import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import (
    fetch, run_limit_backtest,
    WORKERS, MAX_HOLD, ENTRY_EXPIRE, JST,
)
from datetime import timedelta


# ── CLI ────────────────────────────────────────────────────────────
def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days",       type=int, default=365)
    p.add_argument("--workers",    type=int, default=WORKERS)
    p.add_argument("--aggressive", action="store_true")
    p.add_argument("--html",       action="store_true")
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


# ── 全 WATCHLIST をバックテストして trade_log を収集 ──────────────
_PERIODS = [30, 90, 180, 365]


def _build_period_results(full_result: dict) -> dict:
    """run_limit_backtest の flat 結果から period_results を構築。"""
    today = datetime.now(JST).date()
    period_results: dict[int, dict] = {}
    for p in _PERIODS:
        cutoff = today - timedelta(days=p)
        sub = [t for t in full_result["trade_log"]
               if t["signal_dt"].date() >= cutoff]
        if not sub:
            continue
        wins = sum(1 for t in sub if t["pnl"] > 0)
        gp   = sum(t["pnl"] for t in sub if t["pnl"] > 0)
        gl   = abs(sum(t["pnl"] for t in sub if t["pnl"] < 0))
        pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        period_results[p] = dict(
            trades=len(sub), wins=wins,
            win_rate=wins / len(sub) * 100,
            pf=pf,
            total_pnl=sum(t["pnl"] for t in sub),
        )
    return period_results


def _run_one(sym, name, strategy, days, calc_fn, em, sm, tm):
    """1 銘柄分のバックテスト。エラー時は None。"""
    try:
        df_raw = fetch(sym, days + 60)
        if df_raw is None or len(df_raw) < 10:
            return None
        result = run_limit_backtest(
            sym, name, df_raw, calc_fn,
            em, sm, tm, days, strategy,
            entry_type=_stop.ENTRY_TYPE,
        )
        if not result or not result.get("trade_log"):
            return None
        # スコア計算
        from check_signals_stop import calc_recommend_score
        period_results = _build_period_results(result)
        score, rank = calc_recommend_score(period_results)

        return dict(
            symbol=sym, name=name, strategy=strategy,
            score=score, rank=rank,
            trades=result["trade_log"],
        )
    except Exception as e:
        print(f"\n  [ERROR] {sym} {strategy}: {e}")
        return None


def collect_all_trades(days: int, workers: int) -> list[dict]:
    """全WATCHLIST × 全戦略のトレードを収集して返す。"""
    tasks = []
    # stop 系
    for sym, name, strat in _stop.WATCHLIST:
        fn, em, sm, tm = _stop.STRATEGY_PARAMS[strat]
        tasks.append((sym, name, strat, days, fn, em, sm, tm))
    # breakout 系
    for sym, name, strat in _brk.WATCHLIST:
        fn, em, sm, tm = _brk.STRATEGY_PARAMS[strat]
        tasks.append((sym, name, strat, days, fn, em, sm, tm))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, *t): t for t in tasks}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if r:
                results.append(r)
            print(f"\r  バックテスト中 {i}/{len(tasks)} ...", end="", flush=True)
    print()
    return results


# ── 分析1: スコア帯別 損益内訳 ─────────────────────────────────────
SCORE_BINS = [(80, 100, "★★★ (80-100)"),
              (60,  79, "★★  (60-79)"),
              (40,  59, "★   (40-59)"),
              ( 0,  39, "△   (0-39)")]

SCORE_FINE = [(90, 100), (80, 89), (70, 79), (60, 69),
              (50, 59),  (40, 49), (30, 39), (20, 29), (0, 19)]


def analyze_score_vs_reason(all_results: list[dict]) -> dict:
    """スコア帯ごとに 目標達成/損切り/タイムカット の件数と P&L を集計。"""
    # 帯別集計
    bins: dict[str, dict] = {label: defaultdict(list) for _, _, label in SCORE_BINS}
    fine: dict[tuple, dict] = {(lo, hi): defaultdict(list) for lo, hi in SCORE_FINE}

    for r in all_results:
        score = r["score"]
        label = next((lbl for lo, hi, lbl in SCORE_BINS if lo <= score <= hi), "△")
        key_fine = next((k for k in SCORE_FINE if k[0] <= score <= k[1]), (0, 19))

        for t in r["trades"]:
            reason = t.get("reason", "?")
            pnl    = t.get("pnl", 0)
            bins[label]["all_pnl"].append(pnl)
            bins[label][reason].append(pnl)
            fine[key_fine]["all_pnl"].append(pnl)
            fine[key_fine][reason].append(pnl)

    return {"bins": bins, "fine": fine}


def _fmt_bin(label, data):
    total = len(data.get("all_pnl", []))
    if total == 0:
        return f"  {label:25s}   ─ (取引なし)"

    tgt  = data.get("目標達成", [])
    stp  = data.get("損切り",   [])
    tc   = data.get("タイムカット", [])

    win_n  = len(tgt)
    stp_n  = len(stp)
    tc_n   = len(tc)
    other  = total - win_n - stp_n - tc_n

    win_r  = win_n / total * 100
    stp_r  = stp_n / total * 100
    tc_r   = tc_n  / total * 100

    avg_pnl = sum(data["all_pnl"]) / total
    avg_win = sum(tgt) / win_n if win_n else 0
    avg_stp = sum(stp) / stp_n if stp_n else 0

    gross_p = sum(x for x in data["all_pnl"] if x > 0)
    gross_l = abs(sum(x for x in data["all_pnl"] if x < 0))
    pf = (gross_p / gross_l) if gross_l > 0 else float("inf")
    pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"

    lines = [
        f"  {label:25s}  {total:5d}取引  勝率{win_r:5.1f}%  PF{pf_s:>6s}  平均{avg_pnl:+8,.0f}円",
        f"    {'目標達成':8s} {win_n:5d}件({win_r:4.1f}%)  平均{avg_win:+8,.0f}円",
        f"    {'損切り':8s}   {stp_n:5d}件({stp_r:4.1f}%)  平均{avg_stp:+8,.0f}円",
        f"    {'タイムカット':8s} {tc_n:5d}件({tc_r:4.1f}%)",
    ]
    if other > 0:
        lines.append(f"    {'その他':8s}     {other:5d}件")
    return "\n".join(lines)


def print_score_analysis(analysis):
    print("\n" + "=" * 65)
    print("【分析1】 スコア帯別 損益内訳")
    print("=" * 65)
    for _, _, label in SCORE_BINS:
        print(_fmt_bin(label, analysis["bins"][label]))
        print()

    print("-" * 65)
    print("  スコア10点刻み:")
    for lo, hi in SCORE_FINE:
        data = analysis["fine"][(lo, hi)]
        total = len(data.get("all_pnl", []))
        if total == 0:
            continue
        tgt_n = len(data.get("目標達成", []))
        stp_n = len(data.get("損切り",   []))
        win_r = tgt_n / total * 100
        avg   = sum(data["all_pnl"]) / total
        gp    = sum(x for x in data["all_pnl"] if x > 0)
        gl    = abs(sum(x for x in data["all_pnl"] if x < 0))
        pf    = gp / gl if gl > 0 else float("inf")
        pf_s  = f"{pf:.2f}" if pf != float("inf") else "∞"
        bar   = "█" * int(win_r / 5)
        print(f"  {lo:3d}-{hi:3d}点: {total:4d}取引  勝{win_r:5.1f}%  PF{pf_s:>6s}  平均{avg:+8,.0f}円  {bar}")


# ── 分析2: 損切り連続期間 ─────────────────────────────────────────
def analyze_consecutive_stops(all_results: list[dict]) -> list[dict]:
    """
    全銘柄のトレードを日付でソートし、
    「損切りが N 連続した期間」を検出する。
    """
    # 全トレードを1本のリストに集約
    all_trades = []
    for r in all_results:
        for t in r["trades"]:
            all_trades.append({
                "symbol":   r["symbol"],
                "name":     r["name"],
                "strategy": r["strategy"],
                "score":    r["score"],
                "rank":     r["rank"],
                "exit_dt":  t.get("exit_dt"),
                "entry_dt": t.get("entry_dt"),
                "pnl":      t.get("pnl", 0),
                "reason":   t.get("reason", "?"),
            })

    if not all_trades:
        return []

    # 決済日でソート
    all_trades.sort(key=lambda x: x["exit_dt"] or date.min)

    # 連敗ランを検出
    runs = []
    cur_run = []
    for t in all_trades:
        if t["reason"] == "損切り":
            cur_run.append(t)
        else:
            if len(cur_run) >= 3:
                runs.append(list(cur_run))
            cur_run = []
    if len(cur_run) >= 3:
        runs.append(cur_run)

    # ランを連敗数で降順ソート
    runs.sort(key=lambda x: len(x), reverse=True)
    return runs


def print_consecutive_analysis(runs: list[list[dict]]):
    print("\n" + "=" * 65)
    print("【分析2】 損切り連続期間 (3連続以上)")
    print("=" * 65)
    if not runs:
        print("  3連続損切りは検出されませんでした。")
        return

    # 連敗長の分布
    dist = defaultdict(int)
    for run in runs:
        dist[len(run)] += 1

    print("  連敗長の分布:")
    for n in sorted(dist.keys(), reverse=True):
        bar = "█" * dist[n]
        print(f"    {n:2d}連敗: {dist[n]:3d}回  {bar}")
    print()

    # 上位10件の詳細
    print(f"  上位{min(10, len(runs))}件の詳細:")
    for i, run in enumerate(runs[:10], 1):
        start = run[0]["exit_dt"]
        end   = run[-1]["exit_dt"]
        total_loss = sum(t["pnl"] for t in run)
        syms = list({f"{t['symbol']}[{t['strategy']}]" for t in run})
        print(f"  [{i:2d}] {len(run)}連敗  {start} ～ {end}  合計損失{total_loss:+,.0f}円")
        for t in run:
            print(f"       {t['exit_dt']}  {t['symbol']} {t['name']}  "
                  f"[{t['strategy']}] スコア{t['score']}  {t['pnl']:+,.0f}円")
        print()

    # 連敗ランとスコアの関係
    print("  連敗中のスコア帯内訳:")
    score_in_run = defaultdict(int)
    for run in runs:
        for t in run:
            label = next((lbl for lo, hi, lbl in SCORE_BINS if lo <= t["score"] <= hi), "△")
            score_in_run[label] += 1
    total_in_run = sum(score_in_run.values())
    for _, _, label in SCORE_BINS:
        n = score_in_run[label]
        pct = n / total_in_run * 100 if total_in_run > 0 else 0
        bar = "█" * int(pct / 3)
        print(f"    {label:25s}: {n:4d}件 ({pct:5.1f}%)  {bar}")


# ── HTML 出力 ─────────────────────────────────────────────────────
def build_html(analysis, runs, days, mode) -> str:
    bins  = analysis["bins"]
    fine  = analysis["fine"]

    rows_bin = ""
    for _, _, label in SCORE_BINS:
        d = bins[label]
        total = len(d.get("all_pnl", []))
        if total == 0:
            continue
        tgt = len(d.get("目標達成", []))
        stp = len(d.get("損切り",   []))
        tc  = len(d.get("タイムカット", []))
        wr  = tgt / total * 100
        gp  = sum(x for x in d["all_pnl"] if x > 0)
        gl  = abs(sum(x for x in d["all_pnl"] if x < 0))
        pf  = gp / gl if gl > 0 else float("inf")
        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        avg = sum(d["all_pnl"]) / total
        avg_w = sum(d.get("目標達成", [])) / tgt if tgt else 0
        avg_s = sum(d.get("損切り",   [])) / stp if stp else 0
        rows_bin += f"""
<tr>
  <td>{label}</td>
  <td>{total:,}</td>
  <td>{wr:.1f}%</td>
  <td>{pf_s}</td>
  <td class="{'pos' if avg > 0 else 'neg'}">{avg:+,.0f}</td>
  <td>{tgt:,} ({tgt/total*100:.1f}%)</td>
  <td>{stp:,} ({stp/total*100:.1f}%)</td>
  <td>{tc:,} ({tc/total*100:.1f}%)</td>
  <td class="{'pos' if avg_w > 0 else 'neg'}">{avg_w:+,.0f}</td>
  <td class="neg">{avg_s:+,.0f}</td>
</tr>"""

    rows_fine = ""
    for lo, hi in SCORE_FINE:
        d = fine[(lo, hi)]
        total = len(d.get("all_pnl", []))
        if total == 0:
            continue
        tgt = len(d.get("目標達成", []))
        stp = len(d.get("損切り",   []))
        wr  = tgt / total * 100
        gp  = sum(x for x in d["all_pnl"] if x > 0)
        gl  = abs(sum(x for x in d["all_pnl"] if x < 0))
        pf  = gp / gl if gl > 0 else float("inf")
        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        avg = sum(d["all_pnl"]) / total
        color = "#27ae60" if wr >= 70 else "#f39c12" if wr >= 55 else "#e74c3c"
        rows_fine += f"""
<tr>
  <td>{lo}–{hi}点</td>
  <td>{total:,}</td>
  <td style="color:{color};font-weight:bold">{wr:.1f}%</td>
  <td>{pf_s}</td>
  <td class="{'pos' if avg > 0 else 'neg'}">{avg:+,.0f}</td>
  <td>{stp:,} ({stp/total*100:.1f}%)</td>
</tr>"""

    # 連敗テーブル
    rows_runs = ""
    if runs:
        for i, run in enumerate(runs[:20], 1):
            start = run[0]["exit_dt"]
            end   = run[-1]["exit_dt"]
            tl    = sum(t["pnl"] for t in run)
            avg_s = sum(t["score"] for t in run) / len(run)
            details = " / ".join(f"{t['symbol']}[{t['strategy']}]" for t in run)
            rows_runs += f"""
<tr>
  <td>{i}</td>
  <td>{len(run)}</td>
  <td>{start}</td>
  <td>{end}</td>
  <td class="neg">{tl:+,.0f}</td>
  <td>{avg_s:.0f}</td>
  <td style="font-size:0.85em">{details}</td>
</tr>"""

    dist_rows = ""
    if runs:
        dist = defaultdict(int)
        for r in runs:
            dist[len(r)] += 1
        for n in sorted(dist.keys(), reverse=True):
            dist_rows += f"<tr><td>{n}連敗</td><td>{dist[n]}回</td></tr>"

    today = date.today().isoformat()
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>トレード品質分析 {today}</title>
<style>
body {{ font-family: 'Helvetica Neue', sans-serif; margin: 20px; background: #f8f9fa; color: #222; }}
h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 8px; }}
h2 {{ color: #2c3e50; margin-top: 40px; border-left: 4px solid #3498db; padding-left: 10px; }}
h3 {{ color: #555; margin-top: 24px; }}
.info {{ background: #fff; padding: 12px 20px; border-radius: 8px; margin-bottom: 20px;
         border: 1px solid #ddd; font-size: 0.9em; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 12px; background: #fff;
         border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
th {{ background: #2c3e50; color: #fff; padding: 8px 12px; text-align: center; font-size: .85em; }}
td {{ padding: 7px 12px; border-bottom: 1px solid #eee; text-align: right; font-size: .88em; }}
td:first-child {{ text-align: left; }}
tr:hover {{ background: #f0f8ff; }}
.pos {{ color: #27ae60; font-weight: bold; }}
.neg {{ color: #e74c3c; font-weight: bold; }}
</style>
</head>
<body>
<h1>トレード品質分析</h1>
<div class="info">
  期間: 直近 {days} 日  |  モード: {mode}  |  生成日: {today}
</div>

<h2>1. スコア帯別 損益内訳</h2>
<h3>ランク別</h3>
<table>
<tr>
  <th>ランク</th><th>取引数</th><th>勝率</th><th>PF</th><th>平均損益</th>
  <th>目標達成</th><th>損切り</th><th>タイムカット</th>
  <th>平均利益</th><th>平均損失</th>
</tr>
{rows_bin}
</table>

<h3>スコア10点刻み</h3>
<table>
<tr>
  <th>スコア帯</th><th>取引数</th><th>勝率</th><th>PF</th><th>平均損益</th><th>損切り件数</th>
</tr>
{rows_fine}
</table>

<h2>2. 損切り連続期間 (全銘柄合算, 3連続以上)</h2>
<p style="font-size:.9em;color:#666">
  ※全銘柄のトレードを決済日順に並べ、損切りが連続している期間を抽出。<br>
  　複数銘柄が同日に損切りになった場合、その日付の内部順序は未定義。
</p>

<h3>連敗長の分布</h3>
<table style="width:300px">
<tr><th>連敗長</th><th>発生回数</th></tr>
{dist_rows or '<tr><td colspan="2">3連敗以上なし</td></tr>'}
</table>

<h3>上位20件の詳細</h3>
<table>
<tr>
  <th>#</th><th>連敗数</th><th>開始日</th><th>終了日</th>
  <th>合計損失</th><th>平均スコア</th><th>銘柄/戦略</th>
</tr>
{rows_runs or '<tr><td colspan="7">3連敗以上なし</td></tr>'}
</table>

</body>
</html>"""


# ── メイン ────────────────────────────────────────────────────────
def main():
    args = _parse_args()
    print(f"モード: {_MODE}  期間: {args.days}日  workers: {args.workers}")
    print("全 WATCHLIST バックテスト中…")

    all_results = collect_all_trades(args.days, args.workers)
    n_trades = sum(len(r["trades"]) for r in all_results)
    print(f"  → {len(all_results)}銘柄  総取引数: {n_trades:,}")

    if n_trades == 0:
        print("取引データがありません (yfinance にアクセスできないか、WATCHLIST が空)")
        return

    analysis = analyze_score_vs_reason(all_results)
    runs = analyze_consecutive_stops(all_results)

    print_score_analysis(analysis)
    print_consecutive_analysis(runs)

    if args.html:
        html = build_html(analysis, runs, args.days, _MODE)
        suffix = "_aggressive" if _MODE == "aggressive" else ""
        fname = f"trade_quality_analysis{suffix}_{date.today().isoformat()}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTMLを出力: {fname}")
        if not args.no_browser:
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(fname)}")


if __name__ == "__main__":
    main()
