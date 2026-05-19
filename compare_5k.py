"""
compare_5k.py  ―  5,000円以下構成スクリプト 横断比較レポート
================================================================
5,000円以下の銘柄で構成された全スクリプトを指定日・期間で比較し、
成績サマリーと本日シグナルを1つのHTMLに表示する。

比較対象スクリプト:
  1. 既存版       (run_signals.py)        — 元の厳選WATCHLIST
  2. WF 2026-05-19 (run_signals_wf.py)   — 今回選定のWalk-forward銘柄
  3. WF+既存 統合  (run_signals_merged.py)— WF 2026-05-08 + 既存を合算
  4. WF+既存 積極  (run_signals_aggressive.py) — WF 2026-05-12 + 既存 (aggressive params)

【使い方】
  python compare_5k.py                     # 全期間(365日)・本日シグナル
  python compare_5k.py --days 90           # 直近90日
  python compare_5k.py --date 2026-05-19   # 指定日シグナル
  python compare_5k.py --no-browser        # HTML生成のみ
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TRADING_MODE", "conservative")

import check_signals_stop     as _stop
import check_signals_breakout as _brk
from _signal_funds import filter_items

JST = timezone(timedelta(hours=9))

# ── 各スクリプトのWATCHLIST定義 ─────────────────────────────────────────────

def _get_orig_watchlists():
    """run_signals.py → 元のcheck_signals_* WATCHLIST"""
    import importlib, check_signals_stop as s, check_signals_breakout as b
    # git reset で戻した元のWATCHLIST
    return list(s.WATCHLIST), list(b.WATCHLIST)


def _get_wf_watchlists():
    """run_signals_wf.py → Walk-forward 2026-05-19"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_rsw", "run_signals_wf.py")
    m    = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return list(m._STOP_WATCHLIST), list(m._BRK_WATCHLIST)


def _get_merged_watchlists():
    """run_signals_merged.py → WF 2026-05-08 + 既存 統合"""
    import importlib.util, check_signals_stop as s, check_signals_breakout as b
    spec = importlib.util.spec_from_file_location("_rsm", "run_signals_merged.py")
    m    = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # merged_watchlist ロジックを再現
    def _merge(wf, orig):
        seen = set()
        out  = []
        for t in (list(wf) + list(orig)):
            key = (t[0], t[2])
            if key not in seen:
                seen.add(key)
                out.append(tuple(t))
        return out
    return _merge(m._WF_STOP, s.WATCHLIST), _merge(m._WF_BRK, b.WATCHLIST)


def _get_aggressive_watchlists():
    """run_signals_aggressive.py → WF 2026-05-12 + 既存 (aggressive params)"""
    import importlib.util
    orig_mode = os.environ.get("TRADING_MODE", "conservative")
    os.environ["TRADING_MODE"] = "aggressive"
    spec = importlib.util.spec_from_file_location("_rsa", "run_signals_aggressive.py")
    m    = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    os.environ["TRADING_MODE"] = orig_mode
    return list(m.STOP_WATCHLIST), list(m.BRK_WATCHLIST)


# ── バックテスト実行 ──────────────────────────────────────────────────────────

def _run_wl(mod, watchlist, sig_date, workers: int) -> list[dict]:
    """指定WATCHLISTでバックテスト + シグナル確認。"""
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(mod.backtest_one, sym, name, strat): (sym, strat)
                for sym, name, strat in watchlist}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    all_items.append(r)
            except Exception:
                pass

    order = {(s, st): i for i, (s, _, st) in enumerate(watchlist)}
    all_items.sort(key=lambda x: order.get((x["symbol"], x["strategy"]), 999))

    for item in all_items:
        item["today_sig"] = mod.check_signal_on_date(
            item["symbol"], item["strategy"], sig_date)
    return all_items


def run_config(label: str, stop_wl, brk_wl, stop_mod, brk_mod,
               sig_date, workers: int, days: int) -> dict:
    """1設定のバックテスト結果を返す。"""
    from backtest_limit_entry import compute_period_result

    # monkey-patch
    stop_mod.WATCHLIST = stop_wl
    brk_mod.WATCHLIST  = brk_wl

    items_stop = filter_items(_run_wl(stop_mod, stop_wl, sig_date, workers))
    items_brk  = filter_items(_run_wl(brk_mod,  brk_wl,  sig_date, workers))

    all_items = items_stop + items_brk

    # 期間別サマリー集計（全trade_logを集約してPFを正確に計算）
    period_summary = {}
    for d in [30, 90, 180, 365]:
        all_trades = []
        for it in all_items:
            pr = compute_period_result(it, d)
            if pr:
                all_trades.extend(pr.get("trade_log", []))
        trades     = len(all_trades)
        wins       = sum(1 for t in all_trades if t["pnl"] > 0)
        pnl        = sum(t["pnl"] for t in all_trades)
        gross_win  = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
        wr = wins / trades * 100 if trades > 0 else 0.0
        pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
        period_summary[d] = dict(trades=trades, wr=wr, pf=pf, pnl=pnl)

    # シグナル
    signals = [
        dict(
            symbol=it["symbol"],
            name=it["name"],
            strategy=it["strategy"],
            score=it.get("score", 0),
            rank=it.get("rank", "-"),
            sig=it["today_sig"],
            group="stop" if it["strategy"] in ("MACD", "A7", "RSI2") else "brk",
        )
        for it in all_items
        if it.get("today_sig")
    ]
    signals.sort(key=lambda x: x["score"], reverse=True)

    return dict(
        label=label,
        n_stop=len(stop_wl),
        n_brk=len(brk_wl),
        period_summary=period_summary,
        signals=signals,
    )


# ── HTML生成 ─────────────────────────────────────────────────────────────────

COLORS = ["#38bdf8", "#34d399", "#fb923c", "#f472b6"]
LABELS_SHORT = ["既存版", "WF-2026-05-19", "WF+既存統合", "WF+既存積極"]


def _pnl_color(v: float) -> str:
    if v > 0:
        return "#34d399"
    if v < 0:
        return "#f87171"
    return "#94a3b8"


def _fmt_pf(v: float) -> str:
    if v == float("inf"):
        return "∞"
    return f"{v:.2f}"


def build_html(results: list[dict], days: int, date_label: str) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # ── サマリーマトリクス ──
    def _matrix():
        rows = ""
        for i, r in enumerate(results):
            c   = COLORS[i]
            ps  = r["period_summary"]
            pr  = ps.get(days, {})
            # 全期間セル
            cells = ""
            for d in [30, 90, 180, 365]:
                p   = ps.get(d, {})
                t   = p.get("trades", 0)
                wr  = p.get("wr", 0.0)
                pf  = p.get("pf", 0.0)
                pnl = p.get("pnl", 0.0)
                pc  = _pnl_color(pnl)
                if t == 0:
                    cells += f'<td style="color:#4b5563;text-align:center">—</td>'
                else:
                    pf_s = _fmt_pf(pf)
                    cells += (
                        f'<td style="text-align:center">'
                        f'<div style="font-size:11px;color:#94a3b8">{t}取引</div>'
                        f'<div style="font-weight:700">{wr:.1f}%</div>'
                        f'<div style="font-size:11px">PF {pf_s}</div>'
                        f'<div style="color:{pc};font-weight:700">{pnl:+,.0f}円</div>'
                        f'</td>'
                    )
            rows += (
                f'<tr>'
                f'<td style="white-space:nowrap;padding:10px 14px">'
                f'  <span style="display:inline-block;width:10px;height:10px;'
                f'  border-radius:50%;background:{c};margin-right:6px"></span>'
                f'  <strong style="color:{c}">{r["label"]}</strong>'
                f'  <div style="font-size:11px;color:#64748b;margin-top:2px">'
                f'  逆指値B {r["n_stop"]}銘柄 / ブレイクアウト {r["n_brk"]}銘柄</div>'
                f'</td>'
                f'{cells}'
                f'</tr>\n'
            )
        return f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:16px">
  📊 成績比較マトリクス
  <span style="font-size:12px;color:#64748b;font-weight:400;margin-left:8px">
    （各セル: 取引数 / 勝率 / PF / 損益）
  </span>
</h2>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead>
<tr style="background:#1e2235">
  <th style="padding:10px 14px;text-align:left;color:#94a3b8;font-weight:600">スクリプト</th>
  <th style="padding:10px 14px;color:#94a3b8;text-align:center">30日</th>
  <th style="padding:10px 14px;color:#94a3b8;text-align:center">90日</th>
  <th style="padding:10px 14px;color:#94a3b8;text-align:center">180日</th>
  <th style="padding:10px 14px;color:#94a3b8;text-align:center">365日</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>"""

    # ── シグナル比較 ──
    def _signals_section():
        # 全シグナルを (symbol, strategy) でまとめ、どのスクリプトに出たか
        sig_map: dict[tuple, dict] = {}
        for i, r in enumerate(results):
            for s in r["signals"]:
                key = (s["symbol"], s["strategy"])
                if key not in sig_map:
                    sig_map[key] = dict(
                        symbol=s["symbol"], name=s["name"],
                        strategy=s["strategy"], score=s["score"],
                        rank=s["rank"], sig=s["sig"],
                        scripts=[]
                    )
                sig_map[key]["scripts"].append(i)

        if not sig_map:
            return '<p style="color:#64748b;padding:16px">本日のシグナルはありません。</p>'

        items = sorted(sig_map.values(), key=lambda x: x["score"], reverse=True)

        strat_colors = {
            "MACD": "#38bdf8", "A7": "#818cf8", "RSI2": "#fb923c",
            "DON":  "#34d399", "VOL": "#f472b6", "MOM":  "#facc15",
        }

        rows = ""
        for it in items:
            sig = it["sig"]
            sc  = strat_colors.get(it["strategy"], "#94a3b8")
            # スクリプトバッジ
            badges = ""
            for idx in it["scripts"]:
                badges += (
                    f'<span style="display:inline-block;padding:2px 8px;'
                    f'border-radius:10px;font-size:11px;font-weight:600;'
                    f'background:{COLORS[idx]}22;color:{COLORS[idx]};'
                    f'border:1px solid {COLORS[idx]}44;margin:1px">'
                    f'{LABELS_SHORT[idx]}</span>'
                )
            rows += (
                f'<tr style="border-bottom:1px solid #1e2235">'
                f'<td style="padding:10px 8px;white-space:nowrap">'
                f'  <div style="font-weight:600">{it["symbol"]}</div>'
                f'  <div style="font-size:11px;color:#94a3b8">{it["name"]}</div>'
                f'</td>'
                f'<td style="padding:10px 8px;text-align:center">'
                f'  <span style="background:{sc}22;color:{sc};padding:2px 8px;'
                f'  border-radius:4px;font-size:12px;font-weight:600">{it["strategy"]}</span>'
                f'</td>'
                f'<td style="padding:10px 8px;text-align:center;font-weight:700">'
                f'  {it["rank"]}{it["score"]}点'
                f'</td>'
                f'<td style="padding:10px 8px;text-align:right">'
                f'  {sig["order_price"]:,.0f}円'
                f'  <div style="font-size:10px;color:#64748b">逆指値</div>'
                f'</td>'
                f'<td style="padding:10px 8px;text-align:right;color:#f87171">'
                f'  {sig["stop_price"]:,.0f}円'
                f'  <div style="font-size:10px;color:#64748b">損切</div>'
                f'</td>'
                f'<td style="padding:10px 8px;text-align:right;color:#34d399">'
                f'  {sig["target_price"]:,.0f}円'
                f'  <div style="font-size:10px;color:#64748b">目標</div>'
                f'</td>'
                f'<td style="padding:10px 8px">{badges}</td>'
                f'</tr>\n'
            )

        return f"""
<h2 style="margin:32px 0 16px;color:#e2e8f0;font-size:16px">
  🔔 本日シグナル比較 ({date_label})
  <span style="font-size:12px;color:#64748b;font-weight:400;margin-left:8px">
    スコア降順・複数スクリプトで出た銘柄は全バッジ表示
  </span>
</h2>
<div style="overflow-x:auto">
<table style="width:100%;border-collapse:collapse;font-size:13px">
<thead>
<tr style="background:#1e2235">
  <th style="padding:10px 8px;text-align:left;color:#94a3b8">銘柄</th>
  <th style="padding:10px 8px;text-align:center;color:#94a3b8">戦略</th>
  <th style="padding:10px 8px;text-align:center;color:#94a3b8">スコア</th>
  <th style="padding:10px 8px;text-align:right;color:#94a3b8">逆指値</th>
  <th style="padding:10px 8px;text-align:right;color:#94a3b8">損切</th>
  <th style="padding:10px 8px;text-align:right;color:#94a3b8">目標</th>
  <th style="padding:10px 8px;color:#94a3b8">出現スクリプト</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
</div>"""

    # ── 凡例 ──
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;'
        f'margin-right:20px;font-size:12px">'
        f'<span style="width:10px;height:10px;border-radius:50%;background:{COLORS[i]};'
        f'flex-shrink:0"></span>'
        f'<span style="color:{COLORS[i]}">{r["label"]}</span>'
        f'<span style="color:#4b5563;font-size:11px">'
        f'({r["n_stop"]+r["n_brk"]}銘柄)</span>'
        f'</span>'
        for i, r in enumerate(results)
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>5,000円以下 スクリプト比較 — {today_str}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #0f1117;
  color: #dde1ec;
  font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  padding: 24px;
}}
h1 {{ font-size: 20px; color: #e2e8f0; margin-bottom: 6px; }}
table tr:nth-child(even) {{ background: #12151f; }}
table tr:hover {{ background: #1a1e2e; }}
table td, table th {{ border-bottom: 1px solid #1e2235; }}
</style>
</head>
<body>
<h1>5,000円以下 スクリプト横断比較</h1>
<p style="color:#64748b;font-size:13px;margin-bottom:20px">
  生成日: {today_str} ／ 比較期間: {days}日 ／ シグナル確認日: {date_label}
</p>
<div style="margin-bottom:24px">{legend}</div>

<div style="background:#12151f;border:1px solid #1e2235;border-radius:10px;padding:20px;margin-bottom:24px">
{_matrix()}
</div>

<div style="background:#12151f;border:1px solid #1e2235;border-radius:10px;padding:20px">
{_signals_section()}
</div>

</body>
</html>"""


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="5,000円以下スクリプト横断比較")
    parser.add_argument("--days",       type=int, default=365)
    parser.add_argument("--date",       type=str, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--workers",    type=int, default=4)
    args = parser.parse_args()

    if args.date:
        try:
            sig_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"[ERROR] --date 形式エラー: {args.date}", file=sys.stderr)
            sys.exit(1)
    else:
        sig_date = None

    date_label = args.date if args.date else "本日"

    # WATCHLISTを取得
    print("WATCHLISTを読み込み中...")
    orig_stop_wl, orig_brk_wl   = _get_orig_watchlists()
    wf_stop_wl,   wf_brk_wl     = _get_wf_watchlists()
    mgd_stop_wl,  mgd_brk_wl    = _get_merged_watchlists()

    # aggressive は TRADING_MODE=aggressive で取得
    agg_stop_wl, agg_brk_wl = _get_aggressive_watchlists()

    configs = [
        ("既存版 (run_signals)",          orig_stop_wl, orig_brk_wl, "conservative"),
        ("WF 2026-05-19 (run_signals_wf)", wf_stop_wl,   wf_brk_wl,   "conservative"),
        ("WF+既存 統合 (run_signals_merged)",  mgd_stop_wl,  mgd_brk_wl,  "conservative"),
        ("WF+既存 積極 (run_signals_aggressive)", agg_stop_wl, agg_brk_wl, "aggressive"),
    ]

    results = []
    for label, sw, bw, mode in configs:
        print(f"\n[{label}] バックテスト実行中... (逆指値B {len(sw)}銘柄 / BRK {len(bw)}銘柄)")
        # 各設定のモードを適用
        os.environ["TRADING_MODE"] = mode
        import importlib
        importlib.reload(_stop)
        importlib.reload(_brk)

        r = run_config(label, sw, bw, _stop, _brk, sig_date, args.workers, args.days)
        results.append(r)
        ps = r["period_summary"].get(args.days, {})
        print(f"  → {ps.get('trades',0)}取引 / 勝率{ps.get('wr',0):.1f}% / "
              f"PF{_fmt_pf(ps.get('pf',0))} / {ps.get('pnl',0):+,.0f}円  "
              f"シグナル{len(r['signals'])}件")

    # conservative に戻す
    os.environ["TRADING_MODE"] = "conservative"

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    html = build_html(results, args.days, date_label)
    out  = Path(f"compare_5k_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    print(f"\nHTML: {out}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
