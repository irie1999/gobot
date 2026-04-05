"""
strategy_compare.py  ―  5戦略 横断バックテスト比較
=====================================================
銘柄群すべてに5戦略をそれぞれ適用し、
戦略単位で集計・スコアリングして優先度を評価する。

使い方:
  python strategy_compare.py                        # 22銘柄 / 365日
  python strategy_compare.py --universe all         # 東証プライム全銘柄（要事前生成）
  python strategy_compare.py --days 180             # 期間変更
  python strategy_compare.py --no-browser

事前準備（--universe all を使う場合）:
  python download_tse_symbols.py   # 東証プライム銘柄リスト生成（1回だけ）

注意: 1800銘柄×5戦略の実行には数時間かかります。
      戦略ごとに途中経過が表示されます。
"""

from __future__ import annotations

import argparse
import math
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import backtest_adaptive_mr       as _amr
import backtest_strong_pullback   as _sp
import backtest_donchian_pullback as _don
import backtest_bb_volume         as _bbv
import backtest_limit_oco         as _oco

JST = timezone(timedelta(hours=9))

# ── デフォルト22銘柄 ─────────────────────────────────────────────
_STOCKS_22: list[tuple[str, str]] = [
    ("7012.T", "川崎重工業"),
    ("7013.T", "IHI"),
    ("5981.T", "東京製綱"),
    ("9044.T", "南海電鉄"),
    ("4118.T", "カネカ"),
    ("7952.T", "河合楽器"),
    ("8795.T", "T&Dホールディングス"),
    ("9042.T", "阪急阪神HD"),
    ("5702.T", "大紀アルミニウム工業所"),
    ("9503.T", "関西電力"),
    ("5333.T", "日本碍子"),
    ("1605.T", "INPEX"),
    ("6361.T", "荏原製作所"),
    ("8802.T", "三菱地所"),
    ("2809.T", "キユーピー"),
    ("6058.T", "ベクトル"),
    ("5844.T", "京都フィナンシャルグループ"),
    ("6963.T", "ローム"),
    ("7389.T", "あいちフィナンシャルグループ"),
    ("5741.T", "UACJ"),
    ("9742.T", "アイネス"),
    ("6282.T", "オイレス工業"),
]


def _load_stocks(universe: str) -> list[tuple[str, str]]:
    """銘柄リストを返す。universe='all' なら symbols_listed_all.py を使用。"""
    if universe == "all":
        p = Path("symbols_listed_all.py")
        if not p.exists():
            print("  ※ symbols_listed_all.py が見つかりません。")
            print("  先に: python download_tse_symbols.py  を実行してください。")
            raise SystemExit(1)
        import importlib.util
        spec = importlib.util.spec_from_file_location("_listed_all", p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        stocks = list(mod.SYMBOLS)
        print(f"  銘柄ユニバース: 東証プライム全銘柄 ({len(stocks)}銘柄)")
        return stocks
    print(f"  銘柄ユニバース: 監視22銘柄")
    return _STOCKS_22

STRATEGIES: list[tuple[str, str]] = [
    ("adaptive_mr",    "アダプティブMR"),
    ("strong_pullback","強い押し目"),
    ("donchian",       "ドンチャン押し目"),
    ("bb_volume",      "BB出来高"),
    ("limit_oco",      "指値OCO"),
]

STRATEGY_COLOR = {
    "adaptive_mr":    "#38bdf8",
    "strong_pullback":"#4ade80",
    "donchian":       "#fb923c",
    "bb_volume":      "#a78bfa",
    "limit_oco":      "#f472b6",
}

_CSS = """
body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;
     margin:0;padding:20px;font-size:14px}
h1{font-size:1.4em;border-bottom:1px solid #21262d;padding-bottom:10px;margin-bottom:20px}
h2{font-size:1.1em;color:#58a6ff;margin:24px 0 10px}
table{border-collapse:collapse;width:100%;margin-bottom:24px}
th{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;
   text-align:left;border-bottom:2px solid #21262d;font-size:.85em}
td{padding:7px 10px;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:#161b22}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.sub{color:#8b949e;font-size:.82em;margin-top:2px}
.badge{border:1px solid currentColor;border-radius:4px;padding:1px 8px;font-size:.85em}
.rank1{background:#2d2a00;border-left:3px solid #f0c000}
.rank2{background:#1a1f2e;border-left:3px solid #94a3b8}
.rank3{background:#1a1a1a;border-left:3px solid #b87333}
details>summary{cursor:pointer;padding:8px 12px;background:#161b22;
                border-radius:6px;margin-bottom:4px}
"""


# ─────────────────────────────────────────────────────────────────────
# バックテスト実行
# ─────────────────────────────────────────────────────────────────────

def _run(symbol: str, key: str, days: int) -> list[dict]:
    try:
        if key == "adaptive_mr":
            df = _amr.fetch(symbol, days)
            if df is None: return []
            return _amr.backtest_amr(_amr.calc(df), days)
        elif key == "strong_pullback":
            df = _sp.fetch(symbol, days)
            if df is None: return []
            return _sp.backtest_strong_pullback(_sp.calc(df), days)
        elif key == "donchian":
            df = _don.fetch(symbol, days)
            if df is None: return []
            return _don.backtest_donchian(_don.calc(df), days)
        elif key == "bb_volume":
            df = _bbv.fetch(symbol, days)
            if df is None: return []
            return _bbv.backtest_bb_vol(_bbv.calc(df), days)
        elif key == "limit_oco":
            df = _oco.fetch(symbol, days)
            if df is None: return []
            return _oco.backtest_limit(_oco.calc(df), days, _oco.DEFAULT_PARAMS)
    except Exception as e:
        print(f"  ※ {symbol} [{key}] エラー: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────
# 統計
# ─────────────────────────────────────────────────────────────────────

def _stats(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, wins=0, wr=0.0, pf=0.0, total=0.0,
                    avg=0.0, max_win=0.0, max_loss=0.0,
                    max_dd=0.0, max_consec_loss=0, active_stocks=0)

    sorted_t = sorted(trades, key=lambda t: t.get("exit_dt") or "")
    pnls     = [t["pnl"] for t in sorted_t]
    wins     = [p for p in pnls if p > 0]
    loss     = [p for p in pnls if p <= 0]
    gross_w  = sum(wins)
    gross_l  = abs(sum(loss))
    pf       = gross_w / gross_l if gross_l > 0 else float("inf")

    # 最大ドローダウン
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # 最大連敗
    max_c = cur_c = 0
    for p in pnls:
        if p <= 0:
            cur_c += 1
            max_c  = max(max_c, cur_c)
        else:
            cur_c = 0

    # トレードが発生した銘柄数
    syms = {t.get("symbol", "") for t in trades if t.get("symbol")}

    return dict(
        n              = len(pnls),
        wins           = len(wins),
        wr             = len(wins) / len(pnls) * 100,
        pf             = pf,
        total          = sum(pnls),
        avg            = sum(pnls) / len(pnls),
        max_win        = max(pnls),
        max_loss       = min(pnls),
        max_dd         = max_dd,
        max_consec_loss= max_c,
        active_stocks  = len(syms),
    )


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────────────
# スコアリング
# ─────────────────────────────────────────────────────────────────────

def _score_table(strat_stats: dict[str, dict], days: int) -> str:
    """5指標を相対ランクで採点し、戦略優先度テーブルを返す。"""

    keys = [k for k, s in strat_stats.items() if s["n"] > 0]
    if not keys:
        return "<p>データなし</p>"

    def rank_scores(metric_fn, higher_is_better: bool) -> dict[str, int]:
        vals = sorted(keys, key=lambda k: metric_fn(strat_stats[k]),
                      reverse=higher_is_better)
        n = len(vals)
        return {k: max(1, round(5 - i * 4 / max(n - 1, 1)))
                for i, k in enumerate(vals)}

    sc_wr  = rank_scores(lambda s: s["wr"],              True)
    sc_pf  = rank_scores(lambda s: min(s["pf"], 10),     True)
    sc_avg = rank_scores(lambda s: s["avg"],             True)
    sc_dd  = rank_scores(lambda s: s["max_dd"],          False)
    sc_cl  = rank_scores(lambda s: s["max_consec_loss"], False)

    ranked = sorted(keys,
                    key=lambda k: -(sc_wr[k]+sc_pf[k]+sc_avg[k]+sc_dd[k]+sc_cl[k]))

    def bar(score: int) -> str:
        filled = "■" * score + "□" * (5 - score)
        color  = "#3fb950" if score >= 4 else ("#f0883e" if score >= 2 else "#f85149")
        return f"<span style='color:{color};letter-spacing:1px'>{filled}</span> {score}点"

    medals = ["🥇", "🥈", "🥉", "4位", "5位"]
    rank_cls = ["rank1", "rank2", "rank3", "", ""]
    rows = ""
    for i, k in enumerate(ranked):
        s     = strat_stats[k]
        color = STRATEGY_COLOR.get(k, "#94a3b8")
        nm    = dict(STRATEGIES).get(k, k)
        total = sc_wr[k]+sc_pf[k]+sc_avg[k]+sc_dd[k]+sc_cl[k]
        rc    = rank_cls[i] if i < len(rank_cls) else ""
        rows += (
            f"<tr class='{rc}'>"
            f"<td style='font-weight:700;font-size:1.1em'>{medals[i]}</td>"
            f"<td><span class='badge' style='color:{color}'>{nm}</span></td>"
            f"<td>{s['n']}件<div class='sub'>{s['active_stocks']}銘柄</div></td>"
            f"<td>{bar(sc_wr[k])}<div class='sub'>{s['wr']:.1f}%</div></td>"
            f"<td>{bar(sc_pf[k])}<div class='sub'>PF {_pf_str(s['pf'])}</div></td>"
            f"<td>{bar(sc_avg[k])}<div class='sub'>{s['avg']:+,.0f}円/件</div></td>"
            f"<td>{bar(sc_dd[k])}<div class='sub'>▼{s['max_dd']:,.0f}円</div></td>"
            f"<td>{bar(sc_cl[k])}<div class='sub'>{s['max_consec_loss']}連敗</div></td>"
            f"<td style='font-weight:700;font-size:1.2em' class='up'>{total}/25</td>"
            f"</tr>"
        )

    return f"""<table>
<thead><tr>
  <th>順位</th><th>戦略</th><th>取引数</th>
  <th>勝率</th><th>PF</th><th>平均損益</th>
  <th>最大DD<br><span style='font-weight:normal;font-size:.85em'>低い方が良</span></th>
  <th>最大連敗<br><span style='font-weight:normal;font-size:.85em'>少ない方が良</span></th>
  <th>合計</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<p style='font-size:.8em;color:#4b5563'>
  ※ 22銘柄すべてに各戦略を適用 / 相対ランクで1〜5点採点（25点満点）/ 直近{days}日
</p>"""


# ─────────────────────────────────────────────────────────────────────
# 銘柄×戦略 ヒートマップ
# ─────────────────────────────────────────────────────────────────────

def _heatmap(all_results: dict[str, dict[str, list[dict]]]) -> str:
    """銘柄×戦略の合計損益ヒートマップテーブル。"""
    strat_keys = [k for k, _ in STRATEGIES]

    strat_name = dict(STRATEGIES)
    default_color = "#94a3b8"
    hdrs = "".join(
        f"<th><span class='badge' style='color:{STRATEGY_COLOR.get(k, default_color)}'>"
        f"{strat_name[k]}</span></th>"
        for k in strat_keys
    )

    rows = ""
    for sym, nm in STOCKS:
        cells = ""
        for k in strat_keys:
            trades = all_results.get(k, {}).get(sym, [])
            if not trades:
                cells += "<td style='color:#4b5563'>—</td>"
                continue
            total = sum(t["pnl"] for t in trades)
            n     = len(trades)
            cls   = "up" if total > 0 else "dn"
            cells += (
                f"<td class='{cls}'>{total:+,.0f}円"
                f"<div class='sub'>{n}件</div></td>"
            )
        rows += f"<tr><td>{sym}</td><td>{nm}</td>{cells}</tr>"

    return f"""<table>
<thead><tr>
  <th>コード</th><th>銘柄名</th>{hdrs}
</tr></thead>
<tbody>{rows}</tbody>
</table>"""


# ─────────────────────────────────────────────────────────────────────
# HTML生成
# ─────────────────────────────────────────────────────────────────────

def build_html(strat_stats: dict, all_results: dict, days: int, n_sym: int = 22) -> str:
    now_str    = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    score_html = _score_table(strat_stats, days)
    heat_html  = _heatmap(all_results) if all_results else "<p style='color:#4b5563'>※ 銘柄数が多いためヒートマップは省略</p>"

    # 戦略別サマリー行
    summary_rows = ""
    for k, nm in STRATEGIES:
        s     = strat_stats.get(k, _stats([]))
        color = STRATEGY_COLOR.get(k, "#94a3b8")
        cls   = "up" if s["total"] > 0 else ("dn" if s["total"] < 0 else "neu")
        avg_c = "up" if s["avg"] > 0 else "dn"
        summary_rows += (
            f"<tr>"
            f"<td><span class='badge' style='color:{color}'>{nm}</span></td>"
            f"<td>{s['n']}件</td>"
            f"<td>{s['wr']:.1f}%</td>"
            f"<td>{_pf_str(s['pf'])}</td>"
            f"<td class='{cls}'>{s['total']:+,.0f}円</td>"
            f"<td class='{avg_c}'>{s['avg']:+,.0f}円</td>"
            f"<td class='dn'>▼{s['max_dd']:,.0f}円</td>"
            f"<td>{s['max_consec_loss']}連敗</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>5戦略比較 {days}日</title>
<style>{_CSS}</style>
</head>
<body>
<h1>5戦略 横断バックテスト比較
  <span style="font-size:.75em;color:#8b949e">
    {n_sym}銘柄 × 5戦略 &nbsp;|&nbsp; 直近{days}日 &nbsp;|&nbsp; 生成: {now_str}
  </span>
</h1>
<p style="font-size:.85em;color:#4b5563;margin-bottom:16px">
  ※ 同じ{n_sym}銘柄に全戦略を適用して公平比較 / ロット100株固定
</p>

<h2>▶ 戦略優先度スコアリング</h2>
{score_html}

<h2>▶ 戦略別集計サマリー</h2>
<table>
<thead><tr>
  <th>戦略</th><th>取引数</th><th>勝率</th><th>PF</th>
  <th>合計損益</th><th>平均損益</th><th>最大DD</th><th>最大連敗</th>
</tr></thead>
<tbody>{summary_rows}</tbody>
</table>

<h2>▶ 銘柄×戦略 損益ヒートマップ</h2>
<p style="font-size:.82em;color:#4b5563">各マスに合計損益（件数）を表示。どの戦略がどの銘柄に向いているか一目で確認。</p>
{heat_html}

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="5戦略 横断バックテスト比較")
    parser.add_argument("--universe",   default="watch",
                        choices=["watch", "all"],
                        help="watch=22銘柄(デフォルト) / all=東証プライム全銘柄")
    parser.add_argument("--days",       type=int, default=365)
    parser.add_argument("--workers",    type=int, default=8,
                        help="並列ワーカー数（大規模時は4〜6推奨）")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    stocks  = _load_stocks(args.universe)
    n_sym   = len(stocks)
    n_total = n_sym * len(STRATEGIES)
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    print(f"\n{'='*65}")
    print(f"  5戦略 横断バックテスト比較")
    print(f"  {n_sym}銘柄 × 5戦略 = {n_total}組  直近{args.days}日")
    print(f"  生成: {now_str}")
    if args.universe == "all":
        print(f"  ※ 大規模実行: 完了まで数時間かかる場合があります")
    print(f"{'='*65}\n")

    # 全組み合わせを並列実行（戦略ごとに進捗表示）
    all_results: dict[str, dict[str, list[dict]]] = {k: {} for k, _ in STRATEGIES}
    strat_counts: dict[str, int] = {k: 0 for k, _ in STRATEGIES}

    tasks = [(sym, k) for sym, _ in stocks for k, _ in STRATEGIES]

    def _task(sym: str, k: str):
        trades = _run(sym, k, args.days)
        for t in trades:
            t["symbol"] = sym
        return sym, k, trades

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_task, sym, k): (sym, k) for sym, k in tasks}
        done = 0
        report_interval = max(n_sym // 5, 10)  # 20%ごとに進捗表示
        for f in as_completed(futs):
            sym, k, trades = f.result()
            all_results[k][sym] = trades
            strat_counts[k] += 1
            done += 1
            if done % report_interval == 0 or done == n_total:
                pct = done / n_total * 100
                elapsed = datetime.now(JST).strftime("%H:%M:%S")
                print(f"  [{elapsed}] 進捗: {done}/{n_total} ({pct:.0f}%)")

    # 戦略ごとに集計・表示
    strat_stats: dict[str, dict] = {}
    print(f"\n  {'戦略':<16}  {'取引':>5}  {'勝率':>6}  {'PF':>6}  "
          f"{'合計損益':>12}  {'最大DD':>10}  {'連敗':>4}")
    print("  " + "─" * 68)
    for k, nm in STRATEGIES:
        all_trades = [t for trades in all_results[k].values() for t in trades]
        s = _stats(all_trades)
        strat_stats[k] = s
        pf_s = _pf_str(s["pf"])
        print(f"  {nm:<16}  {s['n']:>5}件  {s['wr']:>5.1f}%  {pf_s:>6}  "
              f"{s['total']:>+12,.0f}円  ▼{s['max_dd']:>9,.0f}円  {s['max_consec_loss']:>4}連敗")

    # HTML出力（ヒートマップは大規模時は省略）
    html    = build_html(strat_stats, all_results if n_sym <= 100 else {}, args.days, n_sym)
    today_s = datetime.now(JST).strftime("%Y%m%d")
    suffix  = f"_{args.universe}" if args.universe != "watch" else ""
    out     = Path(f"strategy_compare{suffix}_{today_s}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(f"file://{out.resolve()}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
