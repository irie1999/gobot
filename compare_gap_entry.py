"""
compare_gap_entry.py  ─  指値上限 vs 成行 の比較バックテスト
=============================================================
既存のシグナルスクリプトは一切変更せず、LIMIT_ENTRY_MARGIN_PCT を
一時的に差し替えることで2パターンを比較します。

  指値モード (sashine) : LIMIT_ENTRY_MARGIN_PCT = 0.03  (現行: +3% 上限)
  成行モード (nariyuki): LIMIT_ENTRY_MARGIN_PCT = 9.99  (上限なし = 始値で必ず約定)

Usage:
  python compare_gap_entry.py               # conservative, 365日
  python compare_gap_entry.py --aggressive  # aggressive モード
  python compare_gap_entry.py --days 180    # 期間変更
  python compare_gap_entry.py --cap 0.05    # 指値上限を 5% で比較
  python compare_gap_entry.py --html        # HTML レポートも出力
  python compare_gap_entry.py --no-browser
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from collections import defaultdict

# ── モード設定 ──────────────────────────────────────────────────────
def _setup_mode() -> str:
    mode = "aggressive" if "--aggressive" in sys.argv else \
           os.getenv("TRADING_MODE", "conservative")
    os.environ["TRADING_MODE"] = mode
    return mode

_MODE = _setup_mode()

import backtest_limit_entry as _bt
import check_signals_stop   as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import WORKERS, fetch, run_limit_backtest


# ── CLI ─────────────────────────────────────────────────────────────
def _parse():
    p = argparse.ArgumentParser()
    p.add_argument("--days",       type=int,   default=365)
    p.add_argument("--workers",    type=int,   default=WORKERS)
    p.add_argument("--cap",        type=float, default=0.03,
                   help="指値上限(デフォルト 0.03 = +3%%)。現行設定と比較される。")
    p.add_argument("--aggressive", action="store_true")
    p.add_argument("--html",       action="store_true")
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


# ── 1銘柄バックテスト (LIMIT_ENTRY_MARGIN_PCT を外から指定) ─────────
def _run_one(sym, name, strat, days, calc_fn, em, sm, tm, cap: float):
    """
    cap を backtest_limit_entry モジュールに一時設定してバックテスト実行。
    スレッド内で直接書き換えるため、並列実行時は cap 別に逐次処理する。
    (同一 cap グループ内は並列 OK)
    """
    try:
        df_raw = fetch(sym, days + 60)
        if df_raw is None or len(df_raw) < 10:
            return None
        result = run_limit_backtest(
            sym, name, df_raw, calc_fn,
            em, sm, tm, days, strat,
            entry_type="stop",
        )
        return result
    except Exception as e:
        print(f"\n  [ERROR] {sym} {strat}: {e}")
        return None


def _run_all(tasks, cap: float, workers: int, label: str) -> list[dict]:
    """全タスクを指定 cap で実行。cap はモジュール定数を一時書き換えして使う。"""
    original = _bt.LIMIT_ENTRY_MARGIN_PCT
    _bt.LIMIT_ENTRY_MARGIN_PCT = cap          # ← 一時差し替え
    results = []
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_one, *t, cap): t for t in tasks}
            for i, fut in enumerate(as_completed(futs), 1):
                r = fut.result()
                if r:
                    results.append(r)
                print(f"\r  [{label}] {i}/{len(tasks)} ...", end="", flush=True)
        print()
    finally:
        _bt.LIMIT_ENTRY_MARGIN_PCT = original  # ← 必ず戻す
    return results


# ── 集計 ──────────────────────────────────────────────────────────────
def _aggregate(results: list[dict]) -> dict:
    """全銘柄の合計統計を返す。"""
    trades = signals = wins = 0
    gross_p = gross_l = total_pnl = total_fee = 0.0
    logs = []
    for r in results:
        signals   += r.get("signals", 0)
        trades    += r.get("trades", 0)
        wins      += r.get("wins", 0)
        total_pnl += r.get("total_pnl", 0)
        total_fee += r.get("total_fee", 0)
        for t in r.get("trade_log", []):
            logs.append(t)
            if t["pnl"] > 0:
                gross_p += t["pnl"]
            else:
                gross_l += abs(t["pnl"])

    pf       = gross_p / gross_l if gross_l > 0 else float("inf")
    win_rate = wins / trades * 100 if trades > 0 else 0.0
    fill_r   = trades / signals * 100 if signals > 0 else 0.0

    # reason 内訳
    reasons = defaultdict(int)
    for t in logs:
        reasons[t.get("reason", "?")] += 1

    return dict(
        signals=signals, trades=trades, wins=wins,
        win_rate=win_rate, pf=pf,
        total_pnl=total_pnl, total_fee=total_fee,
        fill_rate=fill_r,
        reasons=dict(reasons),
        trade_log=logs,
    )


def _strategy_breakdown(results: list[dict]) -> dict:
    """戦略別の集計。"""
    by_strat: dict[str, list] = defaultdict(list)
    for r in results:
        by_strat[r["strategy"]].append(r)
    out = {}
    for strat, rs in sorted(by_strat.items()):
        out[strat] = _aggregate(rs)
    return out


# ── テキスト表示 ──────────────────────────────────────────────────────
def _pf_str(pf):
    return f"{pf:.2f}" if pf != float("inf") else "∞"


def print_comparison(sashine: dict, nariyuki: dict, cap: float, days: int):
    print()
    print("=" * 70)
    print(f"  指値 vs 成行 比較  ({days}日  モード: {_MODE})")
    print(f"  指値上限: +{cap*100:.1f}%  vs  成行(上限なし)")
    print("=" * 70)

    rows = [
        ("シグナル数",      f"{sashine['signals']:,}",    f"{nariyuki['signals']:,}"),
        ("約定数",          f"{sashine['trades']:,}",     f"{nariyuki['trades']:,}"),
        ("約定率",          f"{sashine['fill_rate']:.1f}%", f"{nariyuki['fill_rate']:.1f}%"),
        ("勝率",            f"{sashine['win_rate']:.1f}%", f"{nariyuki['win_rate']:.1f}%"),
        ("PF",              _pf_str(sashine['pf']),       _pf_str(nariyuki['pf'])),
        ("総損益",          f"{sashine['total_pnl']:+,.0f}円", f"{nariyuki['total_pnl']:+,.0f}円"),
        ("手数料合計",      f"{sashine['total_fee']:+,.0f}円", f"{nariyuki['total_fee']:+,.0f}円"),
    ]

    header = f"  {'':20s} {'指値+'+str(int(cap*100))+'%':>18s} {'成行':>18s}"
    print(header)
    print("  " + "-" * 58)
    for label, sv, nv in rows:
        arrow = ""
        print(f"  {label:20s} {sv:>18s} {nv:>18s} {arrow}")

    # 損益差分
    diff = nariyuki['total_pnl'] - sashine['total_pnl']
    print()
    print(f"  成行の方が {'多い' if diff > 0 else '少ない'}: {abs(diff):+,.0f}円")

    # reason 内訳
    print()
    print("  ─ 決済理由内訳 ─")
    all_reasons = set(sashine['reasons']) | set(nariyuki['reasons'])
    for reason in ["目標達成", "損切り", "タイムカット"]:
        if reason not in all_reasons:
            continue
        sn = sashine['reasons'].get(reason, 0)
        nn = nariyuki['reasons'].get(reason, 0)
        st = sashine['trades'] or 1
        nt = nariyuki['trades'] or 1
        print(f"  {reason:10s}  指値: {sn:4d}件({sn/st*100:4.1f}%)  "
              f"成行: {nn:4d}件({nn/nt*100:4.1f}%)")

    # 戦略別
    print()
    print("  ─ 戦略別 損益差 ─")
    s_strat = _strategy_breakdown([r for r in [] ])  # placeholder


def print_strategy_comparison(s_results, n_results):
    """戦略別に比較表示。"""
    s_by = _strategy_breakdown(s_results)
    n_by = _strategy_breakdown(n_results)
    all_strats = sorted(set(s_by) | set(n_by))

    print()
    print(f"  {'戦略':8s} {'指値 PF':>8s} {'成行 PF':>8s} "
          f"{'指値 勝率':>9s} {'成行 勝率':>9s} "
          f"{'指値 損益':>12s} {'成行 損益':>12s} {'差分':>12s}")
    print("  " + "-" * 80)
    for strat in all_strats:
        s = s_by.get(strat, {})
        n = n_by.get(strat, {})
        s_pf  = _pf_str(s.get("pf", 0))
        n_pf  = _pf_str(n.get("pf", 0))
        s_wr  = f"{s.get('win_rate',0):.1f}%"
        n_wr  = f"{n.get('win_rate',0):.1f}%"
        s_pnl = s.get("total_pnl", 0)
        n_pnl = n.get("total_pnl", 0)
        diff  = n_pnl - s_pnl
        sign  = "↑" if diff > 0 else "↓"
        print(f"  {strat:8s} {s_pf:>8s} {n_pf:>8s} "
              f"{s_wr:>9s} {n_wr:>9s} "
              f"{s_pnl:>+12,.0f} {n_pnl:>+12,.0f} {sign}{abs(diff):>10,.0f}")


# ── HTML 出力 ─────────────────────────────────────────────────────────
def _build_html(s_agg, n_agg, s_results, n_results, cap, days) -> str:
    s_strat = _strategy_breakdown(s_results)
    n_strat = _strategy_breakdown(n_results)
    all_strats = sorted(set(s_strat) | set(n_strat))

    diff_total = n_agg["total_pnl"] - s_agg["total_pnl"]
    diff_color = "#27ae60" if diff_total > 0 else "#e74c3c"

    def td(val, cls=""):
        return f'<td class="{cls}">{val}</td>'

    def pnl_td(v):
        cls = "pos" if v > 0 else "neg" if v < 0 else ""
        return td(f"{v:+,.0f}円", cls)

    # サマリー行
    summary_rows = ""
    for label, sv, nv in [
        ("約定数",   f"{s_agg['trades']:,}",  f"{n_agg['trades']:,}"),
        ("約定率",   f"{s_agg['fill_rate']:.1f}%", f"{n_agg['fill_rate']:.1f}%"),
        ("勝率",     f"{s_agg['win_rate']:.1f}%",  f"{n_agg['win_rate']:.1f}%"),
        ("PF",       _pf_str(s_agg['pf']),    _pf_str(n_agg['pf'])),
    ]:
        summary_rows += f"<tr><td>{label}</td><td>{sv}</td><td>{nv}</td></tr>\n"

    # reason 行
    reason_rows = ""
    for reason in ["目標達成", "損切り", "タイムカット"]:
        sn = s_agg["reasons"].get(reason, 0)
        nn = n_agg["reasons"].get(reason, 0)
        st = s_agg["trades"] or 1
        nt = n_agg["trades"] or 1
        reason_rows += (
            f"<tr><td>{reason}</td>"
            f"<td>{sn}件 ({sn/st*100:.1f}%)</td>"
            f"<td>{nn}件 ({nn/nt*100:.1f}%)</td></tr>\n"
        )

    # 戦略別行
    strat_rows = ""
    for strat in all_strats:
        s = s_strat.get(strat, {})
        n = n_strat.get(strat, {})
        diff = n.get("total_pnl", 0) - s.get("total_pnl", 0)
        strat_rows += (
            f"<tr>"
            f"<td>{strat}</td>"
            f"<td>{_pf_str(s.get('pf',0))}</td>"
            f"<td>{_pf_str(n.get('pf',0))}</td>"
            f"<td>{s.get('win_rate',0):.1f}%</td>"
            f"<td>{n.get('win_rate',0):.1f}%</td>"
            + pnl_td(s.get("total_pnl", 0))
            + pnl_td(n.get("total_pnl", 0))
            + f'<td style="color:{diff_color};font-weight:bold">{diff:+,.0f}円</td>'
            + "</tr>\n"
        )

    today = date.today().isoformat()
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>指値 vs 成行 比較 {today}</title>
<style>
body {{ font-family: 'Helvetica Neue', sans-serif; margin: 24px; background: #f8f9fa; color: #222; }}
h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 8px; }}
h2 {{ color: #2c3e50; margin-top: 36px; border-left: 4px solid #3498db; padding-left: 10px; }}
.info {{ background:#fff; padding:12px 20px; border-radius:8px; margin-bottom:20px;
         border:1px solid #ddd; font-size:.9em; }}
.diff-box {{ background:#fff; border-radius:10px; padding:20px 28px; margin:20px 0;
             border:2px solid {diff_color}; display:inline-block; }}
.diff-box .num {{ font-size:2em; font-weight:bold; color:{diff_color}; }}
table {{ border-collapse:collapse; width:100%; margin-top:12px; background:#fff;
         border-radius:8px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
th {{ background:#2c3e50; color:#fff; padding:8px 14px; text-align:center; font-size:.85em; }}
td {{ padding:7px 14px; border-bottom:1px solid #eee; text-align:right; font-size:.88em; }}
td:first-child {{ text-align:left; }}
tr:hover {{ background:#f0f8ff; }}
.pos {{ color:#27ae60; font-weight:bold; }}
.neg {{ color:#e74c3c; font-weight:bold; }}
.sashine {{ background:#ebf5fb; }}
.nariyuki {{ background:#fef9e7; }}
</style>
</head>
<body>
<h1>指値 vs 成行 比較バックテスト</h1>
<div class="info">
  期間: 直近 {days}日 ｜ モード: {_MODE} ｜ 生成日: {today}<br>
  <b>指値</b>: 逆指値トリガー後、始値が +{cap*100:.0f}% 以内なら約定、超えたらスキップ<br>
  <b>成行</b>: 逆指値トリガー後、始値がどんなに高くても約定（ギャップアップも全部拾う）
</div>

<div class="diff-box">
  成行の総損益 − 指値の総損益 =
  <span class="num">{diff_total:+,.0f}円</span>
  &nbsp;{'(成行の方が有利)' if diff_total > 0 else '(指値の方が有利)'}
</div>

<h2>全体サマリー</h2>
<table style="width:500px">
<tr>
  <th>項目</th>
  <th class="sashine">指値 +{cap*100:.0f}% (現行)</th>
  <th class="nariyuki">成行 (ギャップ全拾い)</th>
</tr>
{summary_rows}
<tr>
  <td><b>総損益</b></td>
  <td class="sashine {'pos' if s_agg['total_pnl']>0 else 'neg'}">{s_agg['total_pnl']:+,.0f}円</td>
  <td class="nariyuki {'pos' if n_agg['total_pnl']>0 else 'neg'}">{n_agg['total_pnl']:+,.0f}円</td>
</tr>
</table>

<h2>決済理由内訳</h2>
<table style="width:500px">
<tr>
  <th>決済理由</th>
  <th class="sashine">指値 +{cap*100:.0f}%</th>
  <th class="nariyuki">成行</th>
</tr>
{reason_rows}
</table>

<h2>戦略別比較</h2>
<table>
<tr>
  <th>戦略</th>
  <th>指値 PF</th><th>成行 PF</th>
  <th>指値 勝率</th><th>成行 勝率</th>
  <th>指値 損益</th><th>成行 損益</th>
  <th>差分</th>
</tr>
{strat_rows}
</table>

</body>
</html>"""


# ── メイン ──────────────────────────────────────────────────────────
def main():
    args = _parse()
    cap = args.cap

    # 全 WATCHLIST のタスクを構築
    tasks = []
    for sym, name, strat in _stop.WATCHLIST:
        fn, em, sm, tm = _stop.STRATEGY_PARAMS[strat]
        tasks.append((sym, name, strat, args.days, fn, em, sm, tm))
    for sym, name, strat in _brk.WATCHLIST:
        fn, em, sm, tm = _brk.STRATEGY_PARAMS[strat]
        tasks.append((sym, name, strat, args.days, fn, em, sm, tm))

    print(f"モード: {_MODE}  期間: {args.days}日  銘柄数: {len(tasks)}")
    print(f"指値上限: +{cap*100:.1f}%  vs  成行(上限なし)")
    print()

    # ── 指値モード ──
    print("▶ 指値モード バックテスト中…")
    s_results = _run_all(tasks, cap=cap,  workers=args.workers, label="指値")

    # ── 成行モード ──
    print("▶ 成行モード バックテスト中…")
    n_results = _run_all(tasks, cap=9.99, workers=args.workers, label="成行")

    if not s_results and not n_results:
        print("データが取得できませんでした (ネットワーク接続を確認してください)")
        return

    s_agg = _aggregate(s_results)
    n_agg = _aggregate(n_results)

    print_comparison(s_agg, n_agg, cap, args.days)
    print_strategy_comparison(s_results, n_results)

    if args.html:
        html = _build_html(s_agg, n_agg, s_results, n_results, cap, args.days)
        suffix = "_aggressive" if _MODE == "aggressive" else ""
        fname = f"compare_gap_entry{suffix}_{date.today().isoformat()}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML出力: {fname}")
        if not getattr(args, "no_browser", False):
            import webbrowser
            webbrowser.open(f"file://{os.path.abspath(fname)}")


if __name__ == "__main__":
    main()
