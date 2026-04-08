"""
scan_entry_compare.py  ―  エントリー方式 A/B/C 銘柄スキャン＆比較
=================================================================
1800銘柄を3つのエントリー方式でスキャンし、成績の良い銘柄を選定して比較する。

  A: 指値     — 安値 ≤ 終値      （翌日少しでも下がれば買う）
  B: 逆指値   — 高値 ≥ 終値      （翌日少しでも上がれば買う）
  C: 逆指値+  — 高値 ≥ 終値+ATR×0.1（翌日少し勢いが出てから買う）

【使い方】
  python scan_entry_compare.py                # 全戦略・全パターン
  python scan_entry_compare.py --macd         # MACDのみ
  python scan_entry_compare.py --top 20       # 上位20銘柄
  python scan_entry_compare.py --workers 8
  python scan_entry_compare.py --min-score 25

【出力】
  テキスト形式（ターミナル）+ HTMLレポート
"""

from __future__ import annotations

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)

JST            = timezone(timedelta(hours=9))
PERIODS        = [30, 90, 180, 365]
PERIOD_WEIGHTS = {30: 4.0, 90: 3.0, 180: 2.0, 365: 1.0}

# ── エントリーパターン定義 ────────────────────────────────────────
# (entry_type, entry_atr_mult, label)
PATTERNS = {
    "A": ("limit", 0.0,  "指値(終値以下)"),
    "B": ("stop",  0.0,  "逆指値(終値以上)"),
    "C": ("stop",  0.1,  "逆指値+ATR×0.1"),
}

# ── 戦略定義 (calc_fn, stop_mult, target_mult) ────────────────────
STRATEGY_DEFS = {
    "MACD": (calc_macd, 1.5, 3.0),
    "A7":   (calc_a7,   1.5, 3.0),
    "RSI2": (calc_rsi2, 2.0, 4.0),
}


def _load_symbols() -> list[tuple[str, str]]:
    p = Path("symbols_listed_all.py")
    if p.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("_sym", p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        syms = list(mod.SYMBOLS)
        print(f"  ユニバース: symbols_listed_all.py ({len(syms)}銘柄)")
        return syms
    from symbols_all import SYMBOLS as _S
    syms = list(_S)
    print(f"  ユニバース: symbols_all.py ({len(syms)}銘柄)")
    return syms


def _period_score(r: dict) -> float:
    if r["trades"] < 2:
        return 0.0
    pf    = min(r["pf"], 5.0) if r["pf"] != float("inf") else 5.0
    bonus = 5.0 if r["trades"] >= 3 else 0.0
    return pf * 10.0 + r["win_rate"] * 0.5 + bonus


def _weighted_score(period_results: dict) -> float:
    total_w = sum(PERIOD_WEIGHTS[p] for p in PERIODS if period_results.get(p))
    if total_w == 0:
        return 0.0
    s = sum(_period_score(period_results[p]) * PERIOD_WEIGHTS[p]
            for p in PERIODS if period_results.get(p))
    return round(s / total_w, 1)


# ── 1銘柄 × 1戦略 × 1パターン ×4期間 ─────────────────────────────
def _backtest_one(symbol: str, name: str, strategy: str, pattern: str,
                  df) -> dict | None:
    calc_fn, sm, tm = STRATEGY_DEFS[strategy]
    etype, em, _    = PATTERNS[pattern]

    period_results: dict[int, dict] = {}
    for days in PERIODS:
        r = run_limit_backtest(symbol, name, df, calc_fn,
                               em, sm, tm, days, strategy,
                               entry_type=etype)
        if r and r["trades"] >= 1:
            period_results[days] = r

    if not period_results:
        return None

    score = _weighted_score(period_results)
    if score <= 0:
        return None

    rep = (period_results.get(365) or period_results.get(180) or
           period_results.get(90)  or period_results.get(30))

    return dict(
        symbol=symbol, name=name, strategy=strategy, pattern=pattern,
        score=score, period_results=period_results,
        trades=rep["trades"], win_rate=rep["win_rate"],
        pf=rep["pf"], total_pnl=rep["total_pnl"],
    )


# ── 全銘柄スキャン ────────────────────────────────────────────────
def scan_all(symbols: list[tuple[str, str]],
             strategies: list[str],
             patterns: list[str],
             workers: int) -> dict[str, dict[str, list[dict]]]:
    """
    Returns: results[strategy][pattern] = list[dict]
    """
    results: dict[str, dict[str, list[dict]]] = {
        st: {pt: [] for pt in patterns} for st in strategies
    }

    def _proc(sym_name: tuple[str, str]):
        symbol, name = sym_name
        df = fetch(symbol, max(PERIODS))
        if df is None:
            return []
        out = []
        for st in strategies:
            for pt in patterns:
                r = _backtest_one(symbol, name, st, pt, df)
                if r:
                    out.append(r)
        return out

    total = len(symbols)
    print(f"\nスキャン開始: {total}銘柄 × {len(strategies)}戦略 × {len(patterns)}パターン", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_proc, sn): sn for sn in symbols}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                for r in (fut.result() or []):
                    results[r["strategy"]][r["pattern"]].append(r)
            except Exception:
                pass
            if done % 100 == 0 or done == total:
                print(f"  {done}/{total} 完了", flush=True)

    # スコア降順ソート
    for st in strategies:
        for pt in patterns:
            results[st][pt].sort(key=lambda x: x["score"], reverse=True)

    return results


# ── テキスト出力 ──────────────────────────────────────────────────
def _pf_str(pf: float) -> str:
    return "  ∞  " if pf == float("inf") else f"{pf:5.2f}"


def _cell(r: dict | None) -> str:
    if not r:
        return " " * 28
    return (f"{r['trades']:3d}回 {r['win_rate']:5.1f}% "
            f"PF{_pf_str(r['pf'])} {r['total_pnl']:+8,.0f}円")


def print_text_report(results: dict, top_n: int, min_score: float,
                      strategies: list[str], patterns: list[str]) -> None:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    w = 110

    print()
    print("=" * w)
    print(f"  エントリー方式 A/B/C 比較スキャン結果  生成日: {today}")
    print(f"  A=指値(終値以下)  B=逆指値(終値以上)  C=逆指値+ATR×0.1")
    print(f"  スコア: PF×10 + 勝率×0.5 + (取引≥3で+5) の4期間加重平均")
    print("=" * w)

    for st in strategies:
        for pt in patterns:
            pat_label = PATTERNS[pt][2]
            filtered = [r for r in results[st][pt] if r["score"] >= min_score][:top_n]
            print()
            print(f"━━━ {st}戦略 × パターン{pt}({pat_label}) "
                  f"上位{len(filtered)}銘柄 (スコア{min_score:.0f}以上) ━━━")
            print(f"{'順位':<4} {'コード':<10} {'銘柄名':<22} {'スコア':>6}  "
                  f"{'30日':^28} {'90日':^28} {'180日':^28} {'365日':^28}")
            print("-" * w)
            for i, r in enumerate(filtered, 1):
                pr = r["period_results"]
                print(f"{i:<4} {r['symbol']:<10} {r['name']:<22} {r['score']:>6.1f}  "
                      f"{_cell(pr.get(30))}  {_cell(pr.get(90))}  "
                      f"{_cell(pr.get(180))}  {_cell(pr.get(365))}")

    # ── パターン別サマリー比較
    print()
    print("=" * w)
    print("  【パターン別サマリー比較（上位30銘柄の集計）】")
    print(f"  {'戦略':<6} {'パターン':<28} {'銘柄数':>6} {'平均スコア':>10} "
          f"{'取引数':>6} {'勝率':>7} {'PF':>6} {'損益合計':>12}")
    print("  " + "-" * 85)
    for st in strategies:
        for pt in patterns:
            pat_label = PATTERNS[pt][2]
            top = [r for r in results[st][pt] if r["score"] >= min_score][:top_n]
            if not top:
                continue
            all_trades = [t for r in top
                          for t in (r["period_results"].get(365) or
                                    r["period_results"].get(180) or
                                    r["period_results"].get(90) or
                                    r["period_results"].get(30) or {}).get("trade_log", [])]
            n   = len(all_trades)
            w_  = sum(1 for t in all_trades if t["pnl"] > 0)
            wr  = w_ / n * 100 if n else 0
            gp  = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
            gl  = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
            pf  = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
            tp  = sum(t["pnl"] for t in all_trades)
            avg_score = sum(r["score"] for r in top) / len(top)
            print(f"  {st:<6} {pt}:{pat_label:<24} {len(top):>6} {avg_score:>10.1f} "
                  f"{n:>6} {wr:>6.1f}% {_pf_str(pf):>6} {tp:>+12,.0f}円")
    print("=" * w)


# ── HTML出力 ──────────────────────────────────────────────────────
def build_html(results: dict, top_n: int, min_score: float,
               strategies: list[str], patterns: list[str]) -> str:
    today = datetime.now(JST).strftime("%Y-%m-%d")

    # サマリーテーブル
    summary_rows = ""
    for st in strategies:
        for pt in patterns:
            pat_label = PATTERNS[pt][2]
            top = [r for r in results[st][pt] if r["score"] >= min_score][:top_n]
            if not top:
                continue
            all_trades = [t for r in top
                          for t in (r["period_results"].get(365) or
                                    r["period_results"].get(180) or
                                    r["period_results"].get(90) or
                                    r["period_results"].get(30) or {}).get("trade_log", [])]
            n   = len(all_trades)
            w_  = sum(1 for t in all_trades if t["pnl"] > 0)
            wr  = w_ / n * 100 if n else 0
            gp  = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
            gl  = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
            pf  = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
            tp  = sum(t["pnl"] for t in all_trades)
            avg_score = sum(r["score"] for r in top) / len(top)
            tp_cls = "profit" if tp >= 0 else "loss"
            summary_rows += f"""
        <tr>
          <td><span class="tag tag-{st.lower()}">{st}</span></td>
          <td><span class="pat pat-{pt.lower()}">{pt}: {pat_label}</span></td>
          <td>{len(top)}</td>
          <td>{avg_score:.1f}</td>
          <td>{n}</td>
          <td>{wr:.1f}%</td>
          <td>{_pf_str(pf).strip()}</td>
          <td class="{tp_cls}">{tp:+,.0f}円</td>
        </tr>"""

    # 銘柄別ランキング（戦略×パターン）
    ranking_sections = ""
    for st in strategies:
        for pt in patterns:
            pat_label = PATTERNS[pt][2]
            top = [r for r in results[st][pt] if r["score"] >= min_score][:top_n]
            rows = ""
            for i, r in enumerate(top, 1):
                pr = r["period_results"]
                def _td(p):
                    d = pr.get(p)
                    if not d:
                        return "<td>-</td><td>-</td><td>-</td><td>-</td>"
                    pc = "profit" if d["total_pnl"] >= 0 else "loss"
                    return (f"<td>{d['trades']}</td><td>{d['win_rate']:.0f}%</td>"
                            f"<td>{_pf_str(d['pf']).strip()}</td>"
                            f"<td class='{pc}'>{d['total_pnl']:+,.0f}</td>")
                rows += f"""
            <tr>
              <td>{i}</td>
              <td class="sym">{r['symbol']}<br><small>{r['name']}</small></td>
              <td>{r['score']:.1f}</td>
              {_td(30)}{_td(90)}{_td(180)}{_td(365)}
            </tr>"""
            ranking_sections += f"""
      <h3>{st}戦略 × パターン{pt} <span class="pat pat-{pt.lower()}">{pat_label}</span></h3>
      <table>
        <thead>
          <tr>
            <th rowspan="2">#</th><th rowspan="2">銘柄</th><th rowspan="2">スコア</th>
            <th colspan="4">30日</th><th colspan="4">90日</th>
            <th colspan="4">180日</th><th colspan="4">365日</th>
          </tr>
          <tr>{"<th>回</th><th>勝率</th><th>PF</th><th>損益</th>" * 4}</tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>エントリー方式 A/B/C 比較 — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.5rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 10px; font-size:1.1rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  h3 {{ color:#cbd5e1; margin:20px 0 8px; font-size:0.95rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:20px; font-size:0.85rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:12px; font-size:0.78rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:5px 6px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:4px 6px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .tag {{ display:inline-block; padding:1px 6px; border-radius:99px; font-size:0.72rem; font-weight:600; }}
  .tag-macd {{ background:#1d4ed8; color:#bfdbfe; }}
  .tag-a7   {{ background:#065f46; color:#a7f3d0; }}
  .tag-rsi2 {{ background:#7c3aed; color:#ddd6fe; }}
  .pat {{ display:inline-block; padding:1px 8px; border-radius:4px; font-size:0.75rem; font-weight:600; }}
  .pat-a {{ background:#b45309; color:#fef3c7; }}
  .pat-b {{ background:#0e7490; color:#cffafe; }}
  .pat-c {{ background:#4d7c0f; color:#ecfccb; }}
</style>
</head>
<body>
<h1>エントリー方式 A/B/C 比較スキャン</h1>
<p class="subtitle">
  生成日: {today} ／ スコア{min_score:.0f}以上 上位{top_n}銘柄<br>
  <span class="pat pat-a">A: 指値（終値以下で買う）</span>&nbsp;
  <span class="pat pat-b">B: 逆指値（終値以上で買う）</span>&nbsp;
  <span class="pat pat-c">C: 逆指値+ATR×0.1</span>
</p>

<h2>パターン別サマリー比較</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>パターン</th><th>銘柄数</th><th>平均スコア</th>
    <th>取引数</th><th>勝率</th><th>PF</th><th>損益合計</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>銘柄別ランキング</h2>
{ranking_sections}

</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="エントリー方式 A/B/C 比較スキャン")
    parser.add_argument("--macd",       action="store_true")
    parser.add_argument("--a7",         action="store_true")
    parser.add_argument("--rsi2",       action="store_true")
    parser.add_argument("--pattern",    choices=["A","B","C"], nargs="+",
                        help="実行パターン指定 (省略時=全て)")
    parser.add_argument("--top",        type=int,   default=30)
    parser.add_argument("--min-score",  type=float, default=20.0)
    parser.add_argument("--workers",    type=int,   default=_DEFAULT_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    all_strats = not (args.macd or args.a7 or args.rsi2)
    strategies = []
    if args.macd or all_strats: strategies.append("MACD")
    if args.a7   or all_strats: strategies.append("A7")
    if args.rsi2 or all_strats: strategies.append("RSI2")

    patterns = args.pattern if args.pattern else ["A", "B", "C"]

    symbols = _load_symbols()
    results = scan_all(symbols, strategies, patterns, args.workers)

    print_text_report(results, args.top, args.min_score, strategies, patterns)

    today    = datetime.now(JST).strftime("%Y-%m-%d")
    out_path = Path(f"scan_entry_compare_{today}.html")
    html     = build_html(results, args.top, args.min_score, strategies, patterns)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
