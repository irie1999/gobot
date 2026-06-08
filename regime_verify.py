from _open_html import open_html
"""
regime_verify.py  ―  相場環境 × 戦略パフォーマンス検証
==================================================
過去365日のN225日次レジーム（ボラ×トレンド）と
バックテスト取引をクロス集計して、

  「高ボラ×レンジなら本当に RSI2/A7 が強いのか？」
  「上昇×高ボラなら本当に DON/VOL が強いのか？」

を実データで検証する。

使い方:
  python regime_verify.py              # 全スクリプト統合（デフォルト）
  python regime_verify.py --workers 8
  python regime_verify.py --no-browser

対象ウォッチリスト: 以下6スクリプトの銘柄を (sym, strat) でユニーク統合
  run_signals.py / run_signals_wf.py / run_signals_prime.py /
  run_signals_nolimit.py / run_signals_aggressive.py / run_signals_merged.py

出力: regime_verify_YYYY-MM-DD.html
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

os.environ["TRADING_MODE"] = "conservative"
import check_signals_stop     as _stop
import check_signals_breakout as _brk

from backtest_limit_entry import WORKERS as _DEF_WORKERS

# ── 全スクリプトのウォッチリストを読み込む ──────────────────────────────────
import run_signals_wf as _wf_mod
_WF_STOP = list(_wf_mod._STOP_WATCHLIST)
_WF_BRK  = list(_wf_mod._BRK_WATCHLIST)

import run_signals_prime as _prime_mod
_PRIME_STOP = list(_prime_mod.STOP_WATCHLIST)
_PRIME_BRK  = list(_prime_mod.BRK_WATCHLIST)

import run_signals_nolimit as _nolimit_mod
_NOLIMIT_STOP = list(_nolimit_mod.STOP_WATCHLIST)
_NOLIMIT_BRK  = list(_nolimit_mod.BRK_WATCHLIST)

import run_signals_aggressive as _agg_mod
_AGG_STOP = list(_agg_mod.STOP_WATCHLIST)
_AGG_BRK  = list(_agg_mod.BRK_WATCHLIST)

import run_signals_merged as _merged_mod
_MERGED_WF_STOP = list(_merged_mod._WF_STOP)
_MERGED_WF_BRK  = list(_merged_mod._WF_BRK)

# conservative に戻す
_stop.STRATEGY_PARAMS.update({k: v for k, v in _stop.STRATEGY_PARAMS.items()})

def _all_unique(lists: list[list]) -> list[tuple]:
    """複数リストを結合し (sym, strat) でユニーク化（名前は最初に現れたものを使用）"""
    seen: set[tuple] = set()
    result: list[tuple] = []
    for lst in lists:
        for item in lst:
            key = (item[0], item[2])   # (sym, strat)
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result

ALL_STOP_WL = _all_unique([
    list(_stop.WATCHLIST),
    _WF_STOP, _PRIME_STOP, _NOLIMIT_STOP, _AGG_STOP, _MERGED_WF_STOP,
])
ALL_BRK_WL = _all_unique([
    list(_brk.WATCHLIST),
    _WF_BRK, _PRIME_BRK, _NOLIMIT_BRK, _AGG_BRK, _MERGED_WF_BRK,
])

# ── 理論マッピング: (trend, vol_level) → 相性が良い戦略セット ────────────────
THEORY: dict[tuple, set] = {
    ("up",       "high"):     {"DON", "VOL", "MACD"},
    ("up",       "mid"):      {"MACD", "DON", "VOL", "MOM", "A7"},
    ("up",       "low"):      {"MACD", "MOM", "A7"},
    ("sideways", "high"):     {"RSI2", "A7"},
    ("sideways", "mid"):      {"MACD", "A7", "RSI2"},
    ("sideways", "low"):      {"MACD", "A7", "MOM"},
    ("down",     "high"):     {"RSI2"},
    ("down",     "mid"):      {"RSI2", "A7"},
    ("down",     "low"):      {"RSI2", "A7"},
}

REGIME_LABEL = {
    ("up",       "high"):  "上昇×高ボラ",
    ("up",       "mid"):   "上昇×中ボラ",
    ("up",       "low"):   "上昇×低ボラ",
    ("sideways", "high"):  "レンジ×高ボラ ★今",
    ("sideways", "mid"):   "レンジ×中ボラ",
    ("sideways", "low"):   "レンジ×低ボラ",
    ("down",     "high"):  "下落×高ボラ",
    ("down",     "mid"):   "下落×中ボラ",
    ("down",     "low"):   "下落×低ボラ",
}

REGIME_ORDER = [
    ("up",       "high"), ("up",       "mid"), ("up",       "low"),
    ("sideways", "high"), ("sideways", "mid"), ("sideways", "low"),
    ("down",     "high"), ("down",     "mid"), ("down",     "low"),
]

STRATEGIES  = ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"]

# ── N225 日次レジームカレンダー ─────────────────────────────────────────────
def build_regime_calendar(days: int) -> dict[date, tuple]:
    import pandas as pd
    try:
        import yfinance as yf
        df = yf.download("^N225", period=f"{days + 60}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return {}
        close = df["Close"].squeeze()
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]

        vol14 = close.pct_change().rolling(14).std() * 100
        ma10  = close.rolling(10).mean()
        ma25  = close.rolling(25).mean()

        calendar: dict[date, tuple] = {}
        for idx in close.index:
            d   = idx.date() if hasattr(idx, "date") else idx
            v   = vol14.loc[idx]
            c   = float(close.loc[idx])
            m10 = ma10.loc[idx]
            m25 = ma25.loc[idx]
            if pd.isna(v) or pd.isna(m10) or pd.isna(m25):
                continue
            v, m10, m25 = float(v), float(m10), float(m25)

            vol_level = "high" if v > 1.5 else ("mid" if v > 0.8 else "low")
            if c > m10 and m10 > m25:
                trend = "up"
            elif c < m10 and m10 < m25:
                trend = "down"
            else:
                trend = "sideways"
            calendar[d] = (trend, vol_level)
        return calendar
    except Exception as e:
        print(f"[WARN] N225取得失敗: {e}")
        return {}

# ── バックテスト実行 & 取引抽出 ──────────────────────────────────────────────
def collect_trades(workers: int) -> list[dict]:
    # 全スクリプトのウォッチリストを統合（ユニーク済み）、conservative モードで統一
    stop_items = ALL_STOP_WL
    brk_items  = ALL_BRK_WL
    print(f"  stop: {len(stop_items)}件 / brk: {len(brk_items)}件 (全スクリプト統合)", flush=True)

    trades: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs: dict = {}
        for sym, name, strat in stop_items:
            futs[ex.submit(_stop.backtest_one, sym, name, strat)] = strat
        for sym, name, strat in brk_items:
            futs[ex.submit(_brk.backtest_one, sym, name, strat)] = strat

        for fut in as_completed(futs):
            try:
                r = fut.result()
                if not r:
                    continue
                pr = r.get("period_results", {})
                if not pr:
                    continue
                tlog = pr[max(pr.keys())].get("trade_log", [])
                for tr in tlog:
                    ets = tr.get("entry_dt")
                    xts = tr.get("exit_dt")
                    if ets is None or xts is None:
                        continue
                    entry_d = ets.date() if hasattr(ets, "date") else ets
                    trades.append({
                        "symbol":   r.get("symbol", ""),
                        "strategy": r.get("strategy", ""),
                        "entry_d":  entry_d,
                        "pnl":      tr.get("pnl", 0),
                        "reason":   tr.get("reason", ""),
                    })
            except Exception:
                pass
    return trades

# ── 集計 ─────────────────────────────────────────────────────────────────────
def aggregate(trades: list[dict],
              calendar: dict[date, tuple]) -> dict[tuple, dict]:
    stats: dict = defaultdict(lambda: {
        "n": 0, "wins": 0, "gross_p": 0.0, "gross_l": 0.0, "pnl": 0.0
    })
    for tr in trades:
        regime = calendar.get(tr["entry_d"])
        if regime is None:
            continue
        key = (regime[0], regime[1], tr["strategy"])
        s   = stats[key]
        s["n"]   += 1
        s["pnl"] += tr["pnl"]
        if tr["pnl"] > 0:
            s["wins"]    += 1
            s["gross_p"] += tr["pnl"]
        else:
            s["gross_l"] += abs(tr["pnl"])
    return dict(stats)

def pf(s: dict) -> float:
    return s["gross_p"] / s["gross_l"] if s["gross_l"] > 0 else (
        99.0 if s["gross_p"] > 0 else 0.0)

def wr(s: dict) -> float:
    return s["wins"] / s["n"] * 100 if s["n"] > 0 else 0.0

# ── レジーム分布集計 ─────────────────────────────────────────────────────────
def regime_distribution(calendar: dict[date, tuple]) -> dict[tuple, int]:
    dist: dict[tuple, int] = defaultdict(int)
    for v in calendar.values():
        dist[v] += 1
    return dict(dist)

# ── HTML生成 ─────────────────────────────────────────────────────────────────
MIN_N = 3  # 件数がこれ以下はデータ不足と見なす

def _cell(stats: dict, trend: str, vol: str, strat: str) -> str:
    key  = (trend, vol, strat)
    s    = stats.get(key, {"n": 0})
    n    = s["n"]
    is_theory = strat in THEORY.get((trend, vol), set())
    theory_mark = "⭕" if is_theory else ""

    if n < MIN_N:
        bg = "#1e293b"
        inner = f'<span style="color:#475569">— ({n}件)</span>'
        bdr   = ""
    else:
        p    = pf(s)
        w    = wr(s)
        pnl  = s["pnl"]
        p_s  = f"{p:.2f}" if p < 99 else "∞"
        w_cls = "profit" if w >= 50 else "loss"
        p_cls = "profit" if p >= 1.0 else "loss"

        if is_theory:
            if p >= 1.2:
                bg  = "#052e16"   # 理論通りかつ高PF → 濃い緑
                bdr = "border:2px solid #4ade80;"
            elif p >= 1.0:
                bg  = "#0f2a1a"
                bdr = "border:2px solid #86efac;"
            else:
                bg  = "#2d0a0a"   # 理論通りだが低PF → 濃い赤
                bdr = "border:2px solid #f87171;"
        else:
            bg  = "#0f172a"
            bdr = ""

        inner = (f'<div style="font-size:0.7rem;color:#94a3b8">{theory_mark} {n}件</div>'
                 f'<div class="{w_cls}" style="font-size:0.8rem;font-weight:600">{w:.0f}%</div>'
                 f'<div class="{p_cls}" style="font-size:0.75rem">PF {p_s}</div>'
                 f'<div style="font-size:0.68rem;color:#64748b">{pnl:+,.0f}円</div>')

    return f'<td style="background:{bg};{bdr}padding:6px 4px;text-align:center;vertical-align:top">{inner}</td>'

def _theory_summary(stats: dict) -> str:
    match_pfs:  list[float] = []
    other_pfs:  list[float] = []
    match_wrs:  list[float] = []
    other_wrs:  list[float] = []

    for regime, good_strats in THEORY.items():
        trend, vol = regime
        for strat in STRATEGIES:
            key = (trend, vol, strat)
            s   = stats.get(key, {"n": 0})
            if s["n"] < MIN_N:
                continue
            p = pf(s)
            w = wr(s)
            if p >= 99:
                p = 5.0
            if strat in good_strats:
                match_pfs.append(p)
                match_wrs.append(w)
            else:
                other_pfs.append(p)
                other_wrs.append(w)

    if not match_pfs or not other_pfs:
        return "<p>データ不足</p>"

    avg_match_pf = sum(match_pfs) / len(match_pfs)
    avg_other_pf = sum(other_pfs) / len(other_pfs)
    avg_match_wr = sum(match_wrs) / len(match_wrs)
    avg_other_wr = sum(other_wrs) / len(other_wrs)

    diff_pf = avg_match_pf - avg_other_pf
    diff_wr = avg_match_wr - avg_other_wr
    verdict_pf = "✅ 理論通り有利" if diff_pf > 0.1 else ("⚠️ 差が小さい" if diff_pf > 0 else "❌ 理論が当てはまらない")
    verdict_wr = "✅ 理論通り有利" if diff_wr > 2 else ("⚠️ 差が小さい" if diff_wr > 0 else "❌ 理論が当てはまらない")

    return f"""
<table style="width:auto;min-width:500px">
<thead><tr>
  <th style="text-align:left">区分</th><th>平均PF</th><th>平均勝率</th><th>サンプル数</th>
</tr></thead>
<tbody>
<tr>
  <td class="sym">⭕ 理論適合（相性が良いとされる組み合わせ）</td>
  <td class="profit" style="text-align:center;font-weight:700">{avg_match_pf:.2f}</td>
  <td class="profit" style="text-align:center">{avg_match_wr:.1f}%</td>
  <td style="text-align:center">{len(match_pfs)}件</td>
</tr>
<tr>
  <td class="sym">　 理論非適合（相性が悪いとされる組み合わせ）</td>
  <td style="text-align:center">{avg_other_pf:.2f}</td>
  <td style="text-align:center">{avg_other_wr:.1f}%</td>
  <td style="text-align:center">{len(other_pfs)}件</td>
</tr>
<tr style="background:#1e293b">
  <td class="sym">差分</td>
  <td style="text-align:center;font-weight:700">{diff_pf:+.2f}</td>
  <td style="text-align:center">{diff_wr:+.1f}%</td>
  <td></td>
</tr>
</tbody>
</table>
<p style="margin-top:10px;font-size:0.85rem">
  PF判定: <strong>{verdict_pf}</strong>　　勝率判定: <strong>{verdict_wr}</strong>
</p>"""

def build_html(stats: dict, dist: dict[tuple, int],
               today_str: str, total_trades: int) -> str:
    total_days = sum(dist.values())

    # ── ヒートマップ ────────────────────────────────────────────────────
    heatmap_rows = []
    for regime in REGIME_ORDER:
        trend, vol = regime
        label  = REGIME_LABEL.get(regime, f"{trend}/{vol}")
        n_days = dist.get(regime, 0)
        pct    = n_days / total_days * 100 if total_days else 0
        good   = THEORY.get(regime, set())
        good_s = " / ".join(sorted(good)) if good else "—"

        cells = "".join(_cell(stats, trend, vol, st) for st in STRATEGIES)

        is_now = regime == ("sideways", "high")
        row_style = "background:#0d1f0d;" if is_now else ""
        now_mark  = " 🔴NOW" if is_now else ""

        heatmap_rows.append(f"""<tr style="{row_style}">
  <td style="white-space:nowrap;font-weight:600;color:#e2e8f0">{label}{now_mark}</td>
  <td style="text-align:center;font-size:0.75rem;color:#94a3b8">{n_days}日<br>({pct:.0f}%)</td>
  <td style="font-size:0.75rem;color:#38bdf8">{good_s}</td>
  {cells}
</tr>""")

    # ── 理論検証サマリー ─────────────────────────────────────────────────
    theory_summary = _theory_summary(stats)

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>相場環境 × 戦略 検証レポート — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a;
          color:#e2e8f0; padding:24px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.1rem;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  .subtitle {{ color:#94a3b8; margin-bottom:20px; font-size:0.9rem; }}
  table {{ border-collapse:collapse; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:7px 10px; border:1px solid #334155;
        white-space:nowrap; text-align:center; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; }}
  .sym {{ text-align:left; font-weight:600; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .legend {{ display:flex; gap:16px; flex-wrap:wrap; margin:12px 0;
             font-size:0.78rem; color:#94a3b8; }}
  .leg-item {{ display:flex; align-items:center; gap:6px; }}
  .leg-box  {{ width:14px; height:14px; border-radius:3px; flex-shrink:0; }}
</style>
</head>
<body>
<h1>相場環境 × 戦略 パフォーマンス検証</h1>
<p class="subtitle">
  生成日: {today_str} ／ 分析取引数: {total_trades:,}件 ／
  対象: 全6スクリプト統合ウォッチリスト stop{len(ALL_STOP_WL)}件+brk{len(ALL_BRK_WL)}件 (conservative)
</p>

<h2>理論検証サマリー</h2>
<p style="color:#94a3b8;font-size:0.82rem;margin-bottom:10px">
  「相場環境に合った戦略を使う」というセオリーが実データで裏付けられるか検証します。
</p>
{theory_summary}

<h2>相場環境 × 戦略 ヒートマップ</h2>
<div class="legend">
  <span class="leg-item"><span class="leg-box" style="background:#052e16;border:2px solid #4ade80"></span>⭕理論適合 かつ PF≥1.2（推奨）</span>
  <span class="leg-item"><span class="leg-box" style="background:#2d0a0a;border:2px solid #f87171"></span>⭕理論適合 だが PF&lt;1.0（要注意）</span>
  <span class="leg-item"><span class="leg-box" style="background:#0f172a"></span>理論非適合（参考）</span>
  <span class="leg-item"><span class="leg-box" style="background:#1e293b"></span>データ不足（{MIN_N}件未満）</span>
  <span style="color:#f87171;font-weight:700">🔴NOW = 現在の相場環境</span>
</div>
<div style="overflow-x:auto">
<table style="width:auto">
<thead><tr>
  <th style="text-align:left">相場環境</th>
  <th>出現日数</th>
  <th style="text-align:left">理論上の<br>推奨戦略</th>
  {"".join(f"<th>{s}</th>" for s in STRATEGIES)}
</tr></thead>
<tbody>
{"".join(heatmap_rows)}
</tbody>
</table>
</div>

<p style="color:#334155;font-size:0.72rem;margin-top:24px">
  ※ 各セルは 勝率 / PF / 損益。件数が{MIN_N}件未満は「—」。
  entry_dt の日経225レジームで分類。バックテスト値であり将来の利益を保証しません。
</p>
</body>
</html>"""

# ── メイン ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="相場環境×戦略 検証レポート")
    parser.add_argument("--workers",    type=int, default=_DEF_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    import webbrowser
    today     = datetime.now(JST).date()
    today_str = today.strftime("%Y-%m-%d")

    print(f"相場環境×戦略 検証レポート — {today_str}", flush=True)

    print("Step 1: N225 日次レジームカレンダー作成中...", flush=True)
    calendar = build_regime_calendar(365)
    print(f"  {len(calendar)}日分のレジーム取得", flush=True)

    print("Step 2: バックテスト実行中...", flush=True)
    trades = collect_trades(args.workers)
    print(f"  {len(trades):,}件の取引を取得", flush=True)

    print("Step 3: 集計中...", flush=True)
    stats = aggregate(trades, calendar)
    dist  = regime_distribution(calendar)

    # 簡易テキスト表示
    print("\n" + "=" * 70)
    print("  相場環境 × 戦略 パフォーマンス")
    print("=" * 70)
    print(f"  {'相場環境':<18} {'戦略':<6} {'件数':>5} {'勝率':>6} {'PF':>6}  理論")
    print("  " + "-" * 60)
    for regime in REGIME_ORDER:
        trend, vol = regime
        label = REGIME_LABEL.get(regime, f"{trend}/{vol}")
        good  = THEORY.get(regime, set())
        for strat in STRATEGIES:
            key = (trend, vol, strat)
            s   = stats.get(key, {"n": 0})
            if s["n"] < MIN_N:
                continue
            p = pf(s)
            w = wr(s)
            theory_mark = "⭕" if strat in good else "  "
            p_str = f"{p:.2f}" if p < 99 else "∞"
            print(f"  {label:<18} {strat:<6} {s['n']:>5} {w:>5.1f}% {p_str:>6}  {theory_mark}")
        print()

    # ── キャッシュ保存 (シグナルHTMLバナー用) ─────────────────────────────
    import json
    cache: dict = {"updated": today_str, "regimes": {}}
    for regime in REGIME_ORDER:
        trend, vol = regime
        key_str = f"{trend}_{vol}"
        cache["regimes"][key_str] = {}
        for strat in STRATEGIES:
            s = stats.get((trend, vol, strat), {"n": 0})
            if s["n"] > 0:
                cache["regimes"][key_str][strat] = {
                    "n":      s["n"],
                    "wr":     round(wr(s), 1),
                    "pf":     round(min(pf(s), 99.0), 2),
                    "theory": strat in THEORY.get(regime, set()),
                }
    Path("regime_cache.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"キャッシュ保存: regime_cache.json ({today_str})", flush=True)

    print("HTML生成中...", flush=True)
    out = Path(f"regime_verify_{today_str}.html")
    out.write_text(build_html(stats, dist, today_str, len(trades)), encoding="utf-8")
    print(f"HTML: {out.resolve()}")

    if not args.no_browser:
        open_html(out.resolve().as_uri())


if __name__ == "__main__":
    main()
