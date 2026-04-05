"""
strategy_compare.py  ―  5戦略 横断バックテスト比較 ＆ 監視リスト生成
=====================================================
銘柄群すべてに5戦略をそれぞれ適用し、
戦略単位で集計・スコアリングして優先度を評価する。

【バックテスト実行】
  python strategy_compare.py --universe all --workers 6
  ※ 結果は自動的に .rsi2_cache/sc_results.pkl に保存される

【監視リスト生成（バックテスト済みキャッシュから）】
  python strategy_compare.py --build-watchlist
  python strategy_compare.py --build-watchlist --top 30 --min-pf 1.5 --min-trades 3

事前準備（--universe all を使う場合）:
  python download_tse_symbols.py   # 東証プライム銘柄リスト生成（1回だけ）

注意: 1800銘柄×5戦略の実行には数時間かかります。
"""

from __future__ import annotations

import argparse
import math
import pickle
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

_CACHE_PATH = Path(".rsi2_cache") / "sc_results.pkl"

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
# バックテスト実行メイン
# ─────────────────────────────────────────────────────────────────────

def _run_backtest_main(args) -> None:
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

    # キャッシュ保存
    _CACHE_PATH.parent.mkdir(exist_ok=True)
    cache_data = {"all_results": all_results, "stocks": stocks,
                  "days": args.days, "saved_at": datetime.now(JST).isoformat()}
    try:
        with open(_CACHE_PATH, "wb") as f:
            pickle.dump(cache_data, f)
        print(f"  キャッシュ保存: {_CACHE_PATH}")
    except Exception as e:
        print(f"  ※ キャッシュ保存失敗: {e}")

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


# ─────────────────────────────────────────────────────────────────────
# 監視リスト生成 ― バックテストHTML ヘルパー
# ─────────────────────────────────────────────────────────────────────

def _wl_equity_svg(all_trades: list[dict]) -> str:
    """全トレードの累積損益折れ線SVGを返す。"""
    sorted_t = sorted(all_trades, key=lambda t: t.get("exit_dt") or "")
    cum = 0.0
    pts = [(0, 0.0)]
    for t in sorted_t:
        cum += t["pnl"]
        pts.append((len(pts), cum))

    if len(pts) < 2:
        return "<p style='color:#4b5563'>データ不足</p>"

    W, H, PAD = 900, 200, 30
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mn, mx = min(ys), max(ys)
    rng = mx - mn or 1

    def px(i): return PAD + (i / max(xs)) * (W - 2 * PAD)
    def py(v): return H - PAD - ((v - mn) / rng) * (H - 2 * PAD)

    coords = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
    zero_y = py(0)
    color  = "#3fb950" if cum >= 0 else "#f85149"

    return (
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px">'
        f'<line x1="{PAD}" y1="{zero_y:.1f}" x2="{W-PAD}" y2="{zero_y:.1f}" '
        f'stroke="#21262d" stroke-width="1"/>'
        f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>'
        f'<text x="{PAD}" y="{H-5}" fill="#8b949e" font-size="11">0円</text>'
        f'<text x="{W-PAD}" y="20" fill="{color}" font-size="11" text-anchor="end">'
        f'{cum:+,.0f}円</text>'
        f'</svg>'
    )


def _wl_trade_rows(trades: list[dict]) -> str:
    """強い押し目トレードの行HTMLを返す。"""
    if not trades:
        return "<tr><td colspan='11' style='color:#4b5563;text-align:center'>トレードなし</td></tr>"
    rows = ""
    for t in sorted(trades, key=lambda t: t.get("exit_dt") or ""):
        pnl     = t["pnl"]
        cls     = "up" if pnl > 0 else "dn"
        entry_d = str(t.get("entry_dt", ""))[:10]
        exit_d  = str(t.get("exit_dt",  ""))[:10]
        sig_d   = str(t.get("signal_dt", ""))[:10]
        sig_cl  = t.get("signal_close", 0) or 0
        entry_p = t.get("entry_p", 0) or t.get("limit_p", 0)
        exit_p  = t.get("exit_p",  0)
        stop_p  = t.get("stop_p",  0)
        target  = t.get("target_p", 0)
        atr     = t.get("atr",     0)
        hold    = t.get("hold",    "—")
        reason  = t.get("reason",  "—")

        def _v(v): return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != 0
        risk   = entry_p - stop_p if _v(stop_p) else 0
        rr_val = (target - entry_p) / risk if (risk > 0 and _v(target)) else None
        rr_s   = f"{rr_val:.1f}R" if rr_val else "—"
        tgt_s  = f"¥{target:,.0f}" if _v(target) else "—"
        stp_s  = f"¥{stop_p:,.0f}" if _v(stop_p) else "—"
        sig_s  = f"¥{sig_cl:,.0f}" if sig_cl else "—"

        rows += (
            f"<tr>"
            f"<td>{sig_d}<div class='sub'>{sig_s}</div></td>"
            f"<td>{entry_d}</td>"
            f"<td>{exit_d}</td>"
            f"<td class='up'>¥{entry_p:,.0f}<div class='sub'>指値</div></td>"
            f"<td class='dn'>{stp_s}<div class='sub'>{'リスク¥' + f'{risk:,.0f}' if risk else ''}</div></td>"
            f"<td class='up'>{tgt_s}<div class='sub'>{rr_s}</div></td>"
            f"<td>¥{exit_p:,.0f}</td>"
            f"<td class='{cls}'>{pnl:+,.0f}円</td>"
            f"<td>{hold}日</td>"
            f"<td class='neu' style='font-size:.78em'>¥{atr:,.0f}</td>"
            f"<td>{reason}</td>"
            f"</tr>"
        )
    return rows


def _build_watchlist_backtest_html(
    selected: list[dict],
    sp_results: dict[str, list[dict]],
    days: int,
    n_universe: int,
) -> str:
    """選定銘柄の強い押し目バックテスト結果をHTML形式で返す。"""
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    # 銘柄ごとに stats を計算
    results = []
    all_trades: list[dict] = []
    for r in selected:
        sym    = r["symbol"]
        nm     = r["name"]
        trades = sp_results.get(sym, [])
        s      = _stats(trades)
        results.append(dict(symbol=sym, name=nm, trades=trades, stats=s))
        all_trades.extend(trades)

    total_s = _stats(all_trades)

    def pf_str(pf): return "∞" if pf == float("inf") else f"{pf:.2f}"

    # サマリーテーブル
    sum_rows = ""
    for r in sorted(results, key=lambda x: (-x["stats"]["total"], x["symbol"])):
        s       = r["stats"]
        cls     = "up" if s["total"] > 0 else ("dn" if s["total"] < 0 else "neu")
        avg_cls = "up" if s["avg"] > 0 else "dn"
        pf_val  = 0 if s["pf"] == float("inf") else s["pf"]
        sum_rows += (
            f"<tr>"
            f"<td>{r['symbol']}</td>"
            f"<td>{r['name']}</td>"
            f"<td data-v='{s['n']}'>{s['n']}</td>"
            f"<td>{s['wins']}</td>"
            f"<td data-v='{s['wr']:.1f}'>{s['wr']:.1f}%</td>"
            f"<td data-v='{pf_val:.2f}'>{pf_str(s['pf'])}</td>"
            f"<td data-v='{s['total']:.0f}' class='{cls}'>{s['total']:+,.0f}円</td>"
            f"<td class='{avg_cls}'>{s['avg']:+,.0f}円</td>"
            f"<td class='up'>{s['max_win']:+,.0f}円</td>"
            f"<td class='dn'>{s['max_loss']:+,.0f}円</td>"
            f"<td class='dn' data-v='{s['max_dd']:.0f}'>▼{s['max_dd']:,.0f}円</td>"
            f"<td data-v='{s['max_consec_loss']}'>{s['max_consec_loss']}連敗</td>"
            f"</tr>"
        )
    tc = "up" if total_s["total"] > 0 else ("dn" if total_s["total"] < 0 else "neu")
    sum_rows += (
        f"<tr style='font-weight:600;border-top:2px solid #21262d'>"
        f"<td colspan='2'>合計 ({len(results)}銘柄)</td>"
        f"<td>{total_s['n']}</td>"
        f"<td>{total_s['wins']}</td>"
        f"<td>{total_s['wr']:.1f}%</td>"
        f"<td>{pf_str(total_s['pf'])}</td>"
        f"<td class='{tc}'>{total_s['total']:+,.0f}円</td>"
        f"<td class='{'up' if total_s['avg']>0 else 'dn'}'>{total_s['avg']:+,.0f}円</td>"
        f"<td class='up'>{total_s['max_win']:+,.0f}円</td>"
        f"<td class='dn'>{total_s['max_loss']:+,.0f}円</td>"
        f"<td class='dn'>▼{total_s['max_dd']:,.0f}円</td>"
        f"<td>{total_s['max_consec_loss']}連敗</td>"
        f"</tr>"
    )

    # トレード詳細（銘柄別折りたたみ）
    details = ""
    for r in sorted(results, key=lambda x: (-x["stats"]["total"], x["symbol"])):
        s = r["stats"]
        if s["n"] == 0:
            continue
        trade_rows = _wl_trade_rows(r["trades"])
        cls = "up" if s["total"] > 0 else "dn"
        details += f"""
<details>
  <summary>
    ▶ {r['symbol']} {r['name']} &nbsp;
    {s['n']}件 / 勝率{s['wr']:.0f}% / PF{pf_str(s['pf'])} /
    <span class='{cls}'>{s['total']:+,.0f}円</span>
  </summary>
  <table style="margin-top:6px">
    <thead><tr>
      <th>シグナル日<br><span style='font-weight:normal;font-size:.85em'>(終値)</span></th>
      <th>約定日</th><th>決済日</th>
      <th>指値<br><span style='font-weight:normal;font-size:.85em'>(注文)</span></th>
      <th>損切<br><span style='font-weight:normal;font-size:.85em'>(逆指値)</span></th>
      <th>利確目標</th>
      <th>決済価格</th><th>損益</th><th>保有日数</th><th>ATR</th><th>理由</th>
    </tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</details>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>強い押し目 監視銘柄バックテスト {days}日</title>
<style>{_CSS}
details>summary{{cursor:pointer;padding:10px;background:#161b22;
                border-radius:6px;margin-bottom:4px;list-style:none}}
details>summary::-webkit-details-marker{{display:none}}
</style>
</head>
<body>
<h1>強い押し目 監視銘柄バックテスト
  <span style="font-size:.75em;color:#8b949e">
    {len(selected)}銘柄 / 直近{days}日 / ユニバース{n_universe}銘柄から選定 / 生成: {now_str}
  </span>
</h1>
<p style="font-size:.85em;color:#4b5563;margin-bottom:16px">
  ※ キャッシュデータから強い押し目上位銘柄のバックテスト結果を表示 / ロット100株固定
</p>

<h2>累積損益</h2>
{_wl_equity_svg(all_trades)}

<h2>銘柄別サマリー</h2>
<table>
<thead><tr>
  <th>コード</th><th>銘柄名</th><th>取引数</th><th>勝ち</th>
  <th>勝率</th><th>PF</th><th>合計損益</th><th>平均損益</th>
  <th>最大利益</th><th>最大損失</th><th>最大DD</th><th>最大連敗</th>
</tr></thead>
<tbody>{sum_rows}</tbody>
</table>

<h2>銘柄別トレード詳細（クリックで展開）</h2>
{details}
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────
# 監視リスト生成（キャッシュから上位銘柄を抽出）
# ─────────────────────────────────────────────────────────────────────

def build_watchlist(top: int, min_pf: float, min_trades: int, no_browser: bool) -> None:
    """キャッシュから強い押し目の上位銘柄を抽出して新しい監視リストを生成する。"""
    if not _CACHE_PATH.exists():
        print("キャッシュが見つかりません。先に以下を実行してください：")
        print("  python strategy_compare.py --universe all --workers 6")
        return

    print(f"\n{'='*65}")
    print(f"  監視リスト生成（強い押し目 上位銘柄）")
    print(f"{'='*65}\n")

    with open(_CACHE_PATH, "rb") as f:
        cache = pickle.load(f)

    all_results: dict = cache["all_results"]
    stocks: list      = cache["stocks"]
    days: int         = cache["days"]
    saved_at: str     = cache.get("saved_at", "不明")
    print(f"  キャッシュ: {len(stocks)}銘柄 / {days}日 / 保存日時: {saved_at[:16]}\n")

    strat_key = "strong_pullback"
    stock_dict = {sym: nm for sym, nm in stocks}

    # 銘柄ごとに統計を計算
    ranked: list[dict] = []
    for sym, trades in all_results.get(strat_key, {}).items():
        if not trades:
            continue
        s = _stats(trades)
        if s["n"] < min_trades:
            continue
        pf = s["pf"]
        if pf == float("inf"):
            pf_val = 99.0  # ∞ は最高評価
        else:
            pf_val = pf
        if pf_val < min_pf:
            continue
        ranked.append(dict(
            symbol  = sym,
            name    = stock_dict.get(sym, sym),
            n       = s["n"],
            wr      = s["wr"],
            pf      = s["pf"],
            pf_val  = pf_val,
            total   = s["total"],
            avg     = s["avg"],
            max_dd  = s["max_dd"],
            max_cl  = s["max_consec_loss"],
        ))

    # PF 降順でソート
    ranked.sort(key=lambda x: (-x["pf_val"], -x["total"]))
    selected = ranked[:top]

    print(f"  フィルタ: 最小取引数 {min_trades}件 / 最小PF {min_pf:.1f}")
    print(f"  該当銘柄: {len(ranked)}件 → 上位{len(selected)}銘柄を選定\n")
    print(f"  {'コード':<10}  {'銘柄名':<24}  {'取引':>4}  {'勝率':>6}  {'PF':>6}  "
          f"{'合計損益':>10}  {'平均':>8}  {'最大DD':>10}  {'連敗':>4}")
    print("  " + "─" * 90)
    for r in selected:
        pf_s = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"  {r['symbol']:<10}  {r['name']:<24}  {r['n']:>4}件  {r['wr']:>5.1f}%  "
              f"{pf_s:>6}  {r['total']:>+10,.0f}円  {r['avg']:>+8,.0f}円  "
              f"▼{r['max_dd']:>9,.0f}円  {r['max_cl']:>4}連敗")

    # watchlist_sp.py として出力
    out_py  = Path("watchlist_sp.py")
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    lines   = [
        '"""',
        f"強い押し目 監視リスト（自動生成）",
        f"生成日時: {now_str}",
        f"元データ: {len(stocks)}銘柄 / 直近{days}日 / PF≥{min_pf} / 取引≥{min_trades}件",
        f"選定: 上位{len(selected)}銘柄（PF降順）",
        '"""',
        "",
        "WATCHLIST_SP: list[tuple[str, str]] = [",
    ]
    for r in selected:
        lines.append(f'    ("{r["symbol"]}", "{r["name"]}"),')
    lines += ["]", ""]
    out_py.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  監視リスト保存: {out_py.resolve()}")

    # HTML レポート出力
    _css = """
body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;
     margin:0;padding:20px;font-size:14px}
h1{font-size:1.3em;border-bottom:1px solid #21262d;padding-bottom:10px}
h2{font-size:1em;color:#58a6ff;margin:20px 0 8px}
table{border-collapse:collapse;width:100%}
th{background:#161b22;color:#8b949e;padding:7px 10px;text-align:left;
   border-bottom:2px solid #21262d;font-size:.85em}
td{padding:6px 10px;border-bottom:1px solid #21262d}
tr:hover td{background:#161b22}
.up{color:#3fb950}.dn{color:#f85149}.sub{color:#8b949e;font-size:.82em}
"""
    rows = ""
    for i, r in enumerate(selected, 1):
        pf_s  = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        cls   = "up" if r["total"] > 0 else "dn"
        rows += (
            f"<tr>"
            f"<td style='font-weight:700'>{i}</td>"
            f"<td>{r['symbol']}</td>"
            f"<td>{r['name']}</td>"
            f"<td>{r['n']}件</td>"
            f"<td>{r['wr']:.1f}%</td>"
            f"<td>{pf_s}</td>"
            f"<td class='{cls}'>{r['total']:+,.0f}円</td>"
            f"<td class='{cls}'>{r['avg']:+,.0f}円</td>"
            f"<td class='dn'>▼{r['max_dd']:,.0f}円</td>"
            f"<td>{r['max_cl']}連敗</td>"
            f"</tr>"
        )
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>強い押し目 監視リスト</title>
<style>{_css}</style></head>
<body>
<h1>強い押し目 監視リスト（{len(selected)}銘柄）
  <span style="font-size:.75em;color:#8b949e">
    元データ: {len(stocks)}銘柄 / 直近{days}日 / 生成: {now_str}
  </span>
</h1>
<p style="font-size:.82em;color:#4b5563">
  フィルタ: PF≥{min_pf} / 最小取引{min_trades}件 / 上位{len(selected)}銘柄（PF降順）<br>
  監視リストファイル: <code>watchlist_sp.py</code>
</p>
<table>
<thead><tr>
  <th>#</th><th>コード</th><th>銘柄名</th><th>取引数</th>
  <th>勝率</th><th>PF</th><th>合計損益</th><th>平均損益</th>
  <th>最大DD</th><th>最大連敗</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""
    today_s  = datetime.now(JST).strftime("%Y%m%d")
    out_html = Path(f"watchlist_sp_{today_s}.html")
    out_html.write_text(html, encoding="utf-8")
    print(f"  HTMLレポート（ランキング）: {out_html.resolve()}")
    if not no_browser:
        webbrowser.open(f"file://{out_html.resolve()}")

    # バックテスト詳細HTML（選定銘柄の取引履歴＆エクイティカーブ）
    sp_results: dict[str, list[dict]] = all_results.get(strat_key, {})
    bt_html      = _build_watchlist_backtest_html(selected, sp_results, days, len(stocks))
    out_bt_html  = Path(f"watchlist_sp_backtest_{today_s}.html")
    out_bt_html.write_text(bt_html, encoding="utf-8")
    print(f"  HTMLレポート（バックテスト）: {out_bt_html.resolve()}")
    if not no_browser:
        webbrowser.open(f"file://{out_bt_html.resolve()}")

    print(f"\n{'='*65}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="5戦略 横断バックテスト比較 ＆ 監視リスト生成")
    parser.add_argument("--universe",        default="watch", choices=["watch", "all"])
    parser.add_argument("--days",            type=int, default=365)
    parser.add_argument("--workers",         type=int, default=8)
    parser.add_argument("--no-browser",      action="store_true")
    # 監視リスト生成モード
    parser.add_argument("--build-watchlist", action="store_true",
                        help="キャッシュから強い押し目上位銘柄を抽出して監視リスト生成")
    parser.add_argument("--top",             type=int,   default=30,
                        help="選定銘柄数（デフォルト30）")
    parser.add_argument("--min-pf",          type=float, default=1.5,
                        help="最小PFフィルタ（デフォルト1.5）")
    parser.add_argument("--min-trades",      type=int,   default=3,
                        help="最小取引数フィルタ（デフォルト3件）")
    args = parser.parse_args()

    if args.build_watchlist:
        build_watchlist(args.top, args.min_pf, args.min_trades, args.no_browser)
        return

    _run_backtest_main(args)


if __name__ == "__main__":
    main()
