"""
scan_breakout_entry.py  ―  逆指値ブレイクアウト戦略 銘柄スキャン
=================================================================
逆指値（上抜けで買う）と相性の良いブレイクアウト系3戦略で
1800銘柄をスキャンし、成績の良い銘柄を選定する。

【戦略】
  DON  : ドンチャン高値ブレイク  — 終値 > 20日高値 + MA50上位
  VOL  : 出来高急増ブレイク     — 10日高値更新 + 出来高2×平均
  MOM  : モメンタムブレイク     — ROC(10日)>5% + MA25>MA75

【エントリー】逆指値: 高値 ≥ 前日終値 で約定（上がったら買う）
【決済】      OCO: 目標+ATR×3.0 / 損切-ATR×1.5 / タイムカット15日

【使い方】
  python scan_breakout_entry.py               # 全戦略
  python scan_breakout_entry.py --don         # ドンチャンのみ
  python scan_breakout_entry.py --top 30 --workers 8
  python scan_breakout_entry.py --min-score 20
"""

from __future__ import annotations

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_limit_entry import (
    fetch,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)

JST            = timezone(timedelta(hours=9))
PERIODS        = [30, 90, 180, 365]
PERIOD_WEIGHTS = {30: 4.0, 90: 3.0, 180: 2.0, 365: 1.0}

# ── ブレイクアウト戦略 ────────────────────────────────────────────

def calc_donchian(df: pd.DataFrame) -> pd.DataFrame:
    """
    ドンチャン高値ブレイクアウト
    シグナル: 終値 > 15日高値（前日時点）AND 終値 > 50日MA
    ※ 旧20日→15日に変更（シグナル頻度向上）
    """
    c, h, l = df["close"], df["high"], df["low"]

    # ATR(14)
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=14, adjust=False).mean()

    # ドンチャン上限（前日時点の15日高値）
    high15 = h.rolling(15).max().shift(1)

    # トレンドフィルター
    ma50 = c.rolling(50).mean()

    df["atr"]       = atr
    df["entry_sig"] = (c > high15) & (c > ma50)
    return df


def calc_vol_breakout(df: pd.DataFrame) -> pd.DataFrame:
    """
    出来高急増ブレイクアウト
    シグナル: 終値 > 5日高値（前日時点）AND 出来高 > 20日平均×1.5
    ※ 旧10日高値・2.0x→5日高値・1.5x に変更（シグナル頻度向上）
    """
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # ATR(14)
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=14, adjust=False).mean()

    # 5日高値ブレイク + 出来高1.5倍
    high5   = h.rolling(5).max().shift(1)
    vol_ma  = v.rolling(20).mean()

    df["atr"]       = atr
    df["entry_sig"] = (c > high5) & (v > vol_ma * 1.5)
    return df


def calc_momentum(df: pd.DataFrame) -> pd.DataFrame:
    """
    モメンタムブレイクアウト
    シグナル: ROC(10日) > 3% AND MA25 > MA75 AND 出来高 > 20日平均×1.2
    ※ 旧5%→3% に変更（シグナル頻度向上）
    """
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # ATR(14)
    prev_c = c.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(span=14, adjust=False).mean()

    # モメンタム指標
    roc   = c.pct_change(10) * 100          # 10日間変化率
    ma25  = c.rolling(25).mean()
    ma75  = c.rolling(75).mean()
    vol_ma = v.rolling(20).mean()

    df["atr"]       = atr
    df["entry_sig"] = (roc > 3.0) & (ma25 > ma75) & (v > vol_ma * 1.2)
    return df


# ── 戦略定義 (calc_fn, entry_atr_mult, stop_mult, target_mult, label)
STRATEGY_DEFS = {
    "DON": (calc_donchian,   0.0, 1.5, 3.0, "ドンチャン高値ブレイク"),
    "VOL": (calc_vol_breakout, 0.0, 1.5, 3.0, "出来高急増ブレイク"),
    "MOM": (calc_momentum,   0.0, 1.5, 3.0, "モメンタムブレイク"),
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


def _backtest_symbol(symbol: str, name: str, strategy: str, df) -> dict | None:
    calc_fn, em, sm, tm, _ = STRATEGY_DEFS[strategy]

    period_results: dict[int, dict] = {}
    for days in PERIODS:
        r = run_limit_backtest(
            symbol, name, df, calc_fn,
            em, sm, tm, days, strategy,
            entry_type="stop",      # 逆指値（上抜けで買う）
        )
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
        symbol=symbol, name=name, strategy=strategy,
        score=score, period_results=period_results,
        trades=rep["trades"], win_rate=rep["win_rate"],
        pf=rep["pf"], total_pnl=rep["total_pnl"],
    )


def scan_all(symbols: list[tuple[str, str]],
             strategies: list[str],
             workers: int,
             max_price: float | None = None) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {st: [] for st in strategies}

    def _proc(sym_name):
        symbol, name = sym_name
        df = fetch(symbol, max(PERIODS))
        if df is None:
            return []
        # 株価フィルター: 最新終値 > max_price の銘柄をスキップ
        if max_price is not None and float(df["close"].iloc[-1]) > max_price:
            return []
        out = []
        for st in strategies:
            r = _backtest_symbol(symbol, name, st, df)
            if r:
                out.append(r)
        return out

    total = len(symbols)
    print(f"\nスキャン開始: {total}銘柄 × {len(strategies)}戦略", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_proc, sn): sn for sn in symbols}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                for r in (fut.result() or []):
                    results[r["strategy"]].append(r)
            except Exception:
                pass
            if done % 100 == 0 or done == total:
                print(f"  {done}/{total} 完了", flush=True)

    for st in strategies:
        results[st].sort(key=lambda x: x["score"], reverse=True)

    return results


def _pf_str(pf: float) -> str:
    return "  ∞  " if pf == float("inf") else f"{pf:5.2f}"


def _cell(r: dict | None) -> str:
    if not r:
        return " " * 30
    return (f"{r['trades']:3d}回 {r['win_rate']:5.1f}% "
            f"PF{_pf_str(r['pf'])} {r['total_pnl']:+8,.0f}円")


def print_text_report(results: dict, top_n: int, min_score: float,
                      strategies: list[str]) -> None:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    w = 115

    print()
    print("=" * w)
    print(f"  逆指値ブレイクアウト戦略 スキャン結果  生成日: {today}")
    print(f"  エントリー: 逆指値（高値 ≥ 前日終値）/ 決済: ATR×1.5損切 / ATR×3.0目標 / 15日タイムカット")
    print(f"  スコア: PF×10 + 勝率×0.5 + (取引≥3で+5) の4期間加重平均")
    print("=" * w)

    for st in strategies:
        label = STRATEGY_DEFS[st][4]
        filtered = [r for r in results[st] if r["score"] >= min_score][:top_n]
        print()
        print(f"━━━ {st}戦略({label}) 上位{len(filtered)}銘柄 (スコア{min_score:.0f}以上) ━━━")
        print(f"{'順位':<4} {'コード':<10} {'銘柄名':<22} {'スコア':>6}  "
              f"{'30日':^30} {'90日':^30} {'180日':^30} {'365日':^30}")
        print("-" * w)
        for i, r in enumerate(filtered, 1):
            pr = r["period_results"]
            print(f"{i:<4} {r['symbol']:<10} {r['name']:<22} {r['score']:>6.1f}  "
                  f"{_cell(pr.get(30))}  {_cell(pr.get(90))}  "
                  f"{_cell(pr.get(180))}  {_cell(pr.get(365))}")

    # サマリー比較
    print()
    print("=" * w)
    print("  【戦略別サマリー（上位銘柄の合算）】")
    print(f"  {'戦略':<8} {'説明':<24} {'銘柄数':>6} {'平均スコア':>10} "
          f"{'取引数':>6} {'勝率':>7} {'PF':>6} {'損益合計':>13}")
    print("  " + "-" * 80)
    for st in strategies:
        label = STRATEGY_DEFS[st][4]
        top = [r for r in results[st] if r["score"] >= min_score][:top_n]
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
        avg = sum(r["score"] for r in top) / len(top)
        print(f"  {st:<8} {label:<24} {len(top):>6} {avg:>10.1f} "
              f"{n:>6} {wr:>6.1f}% {_pf_str(pf):>6} {tp:>+13,.0f}円")
    print("=" * w)
    print("\n【貼り付け用メモ】")
    print("上記結果をもとに監視銘柄を選定してください。")
    print("選定基準の参考: スコア30以上 / 勝率55%以上 / PF2.0以上 / 取引数5回以上")


def build_html(results: dict, top_n: int, min_score: float,
               strategies: list[str]) -> str:
    today = datetime.now(JST).strftime("%Y-%m-%d")

    summary_rows = ""
    for st in strategies:
        label = STRATEGY_DEFS[st][4]
        top = [r for r in results[st] if r["score"] >= min_score][:top_n]
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
        avg = sum(r["score"] for r in top) / len(top)
        tc  = "profit" if tp >= 0 else "loss"
        summary_rows += f"""
        <tr>
          <td><span class="tag tag-{st.lower()}">{st}</span></td>
          <td>{label}</td>
          <td>{len(top)}</td><td>{avg:.1f}</td><td>{n}</td>
          <td>{wr:.1f}%</td><td>{_pf_str(pf).strip()}</td>
          <td class="{tc}">{tp:+,.0f}円</td>
        </tr>"""

    ranking_sections = ""
    for st in strategies:
        label = STRATEGY_DEFS[st][4]
        top   = [r for r in results[st] if r["score"] >= min_score][:top_n]
        rows  = ""
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
      <h3><span class="tag tag-{st.lower()}">{st}</span> {label}</h3>
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
<title>逆指値ブレイクアウト スキャン — {today}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.5rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 10px; font-size:1.1rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  h3 {{ color:#cbd5e1; margin:18px 0 8px; font-size:0.95rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:20px; font-size:0.85rem; line-height:1.6; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:12px; font-size:0.78rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:5px 6px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:4px 6px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-don {{ background:#0e7490; color:#cffafe; }}
  .tag-vol {{ background:#92400e; color:#fef3c7; }}
  .tag-mom {{ background:#4d7c0f; color:#ecfccb; }}
</style>
</head>
<body>
<h1>逆指値ブレイクアウト戦略 スキャン</h1>
<p class="subtitle">
  生成日: {today} ／ スコア{min_score:.0f}以上 上位{top_n}銘柄<br>
  エントリー: <strong>逆指値</strong>（高値 ≥ 前日終値 で約定）<br>
  決済: ATR×1.5 損切 ／ ATR×3.0 目標 ／ 15日タイムカット
</p>

<h2>戦略別サマリー</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>説明</th><th>銘柄数</th><th>平均スコア</th>
    <th>取引数</th><th>勝率</th><th>PF</th><th>損益合計</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>銘柄ランキング</h2>
{ranking_sections}

</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="逆指値ブレイクアウト銘柄スキャン")
    parser.add_argument("--don",        action="store_true", help="ドンチャンのみ")
    parser.add_argument("--vol",        action="store_true", help="出来高ブレイクのみ")
    parser.add_argument("--mom",        action="store_true", help="モメンタムのみ")
    parser.add_argument("--top",        type=int,   default=30)
    parser.add_argument("--min-score",  type=float, default=20.0)
    parser.add_argument("--max-price",  type=float, default=None,
                        help="最大株価フィルター (例: 5000 → 100株で50万円以下)")
    parser.add_argument("--workers",    type=int,   default=_DEFAULT_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    all_strats = not (args.don or args.vol or args.mom)
    strategies = []
    if args.don or all_strats: strategies.append("DON")
    if args.vol or all_strats: strategies.append("VOL")
    if args.mom or all_strats: strategies.append("MOM")

    if args.max_price:
        print(f"  株価フィルター: ≤ {args.max_price:,.0f}円（100株 ≤ {args.max_price*100:,.0f}円）", flush=True)

    symbols = _load_symbols()
    results = scan_all(symbols, strategies, args.workers, args.max_price)

    print_text_report(results, args.top, args.min_score, strategies)

    today    = datetime.now(JST).strftime("%Y-%m-%d")
    out_path = Path(f"breakout_scan_{today}.html")
    html     = build_html(results, args.top, args.min_score, strategies)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
