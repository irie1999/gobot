"""
budget_sim.py ― 資金制約付き マルチスクリプト比較シミュレーター

各 run_signals*.py のシグナルを「スコア高い順・資金制約内」で投資した場合の
成績を比較し、最も利益が出るスクリプトを特定する。

前提:
  - 資金 = 現物額 × レバレッジ倍率
  - 1ポジション = signal_price × 100株（FIXED_QTY）
  - 同一日に複数シグナル → スコア降順で資金が許す限り約定
  - ポジションが閉じたら資金を解放して次のシグナルに使える

使い方:
  python budget_sim.py                         # デフォルト: 4.2M円, 365日
  python budget_sim.py --cash 2100000 --lev 2  # 現物210万×レバ2倍
  python budget_sim.py --days 90               # 直近90日
  python budget_sim.py --workers 4             # バックテスト並列数
  python budget_sim.py --scripts run_signals run_signals_prime  # 特定スクリプトのみ
  python budget_sim.py --no-browser            # HTML生成のみ（ブラウザ起動なし）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

SCRIPT_CONFIGS = [
    {"name": "run_signals",            "label": "① 標準(conservative)",       "color": "#60a5fa"},
    {"name": "run_signals_merged",     "label": "② 統合WF(conservative)",     "color": "#818cf8"},
    {"name": "run_signals_aggressive", "label": "③ Aggressive WF",            "color": "#fb923c"},
    {"name": "run_signals_prime",      "label": "④ Prime WF (株価制限なし)",  "color": "#34d399"},
    {"name": "run_signals_nolimit",    "label": "⑤ Nolimit WF (候補120銘柄)", "color": "#f472b6"},
]

MEDALS = ["🥇", "🥈", "🥉", "4位", "5位"]
MEDAL_COLORS = ["#f59e0b", "#94a3b8", "#b45309", "#64748b", "#64748b"]


def run_worker(script_name: str, budget: float, days: int, workers: int) -> dict | None:
    cmd = [
        sys.executable, "_budget_worker.py",
        "--script",  script_name,
        "--budget",  str(int(budget)),
        "--days",    str(days),
        "--workers", str(workers),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            env=env,
        )
        if r.returncode != 0:
            print(f"  [ERROR] {script_name}:\n{r.stderr[-400:]}", file=sys.stderr)
            return None
        stdout = (r.stdout or "").strip()
        if not stdout:
            print(f"  [ERROR] {script_name}: 出力なし (stderr={r.stderr[-200:]})",
                  file=sys.stderr)
            return None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        print(f"  [JSON ERROR] {script_name}: JSON 行なし", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] {script_name}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"  [JSON ERROR] {script_name}: {e}", file=sys.stderr)
        return None


def _bar(value: float, max_val: float, width: int = 20) -> str:
    if max_val <= 0:
        return " " * width
    filled = int(width * min(value / max_val, 1.0))
    return "█" * filled + "░" * (width - filled)


def _pnl_color(pnl: float) -> str:
    return "#34d399" if pnl >= 0 else "#f87171"


def _pnl_bg(pnl: float) -> str:
    return "rgba(52,211,153,.08)" if pnl >= 0 else "rgba(248,113,113,.08)"


def build_html(results: list[dict], budget: float, days: int,
               cash: float | None, lev: float | None) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    budget_label = f"{budget/10000:.0f}万円"
    if cash is not None and lev is not None:
        budget_label += f"（現物{cash/10000:.0f}万 × レバ{lev:.1f}倍）"

    max_pnl = max(abs(r.get("total_pnl", 0)) for r in results) or 1

    # ── ランキングカード ──────────────────────────────────────────────────────
    def _rank_card(i: int, r: dict) -> str:
        pnl    = r.get("total_pnl", 0)
        wr     = r.get("win_rate", 0)
        n_ex   = r.get("n_executed", 0)
        n_cl   = r.get("n_closed", 0)
        avg    = pnl / n_cl if n_cl > 0 else 0
        medal  = MEDALS[i] if i < len(MEDALS) else f"{i+1}位"
        mcol   = MEDAL_COLORS[i] if i < len(MEDAL_COLORS) else "#64748b"
        pcol   = _pnl_color(pnl)
        pbg    = _pnl_bg(pnl)
        cfg    = next((c for c in SCRIPT_CONFIGS if c["label"] == r["label"]), {})
        accent = cfg.get("color", "#60a5fa")
        bar_w  = int(160 * min(abs(pnl) / max_pnl, 1.0))
        bar_col = pcol
        skip_b = r.get("n_skipped_budget", 0)
        skip_p = r.get("n_skipped_price", 0)
        is_best = i == 0

        border = f"border:2px solid {accent}" if is_best else "border:1px solid #252840"
        shadow = f"box-shadow:0 0 20px {accent}44" if is_best else ""
        best_badge = '<div style="color:#34d399;font-size:10px;font-weight:700;letter-spacing:1px">★ BEST</div>' if is_best else ""

        return f"""
<div style="background:#0f1117;{border};{shadow};border-radius:12px;padding:20px 24px;min-width:260px;flex:1">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
    <span style="font-size:28px;line-height:1">{medal}</span>
    <div>
      <div style="color:{accent};font-size:13px;font-weight:700;letter-spacing:.5px">{r['label']}</div>
      {best_badge}
    </div>
  </div>
  <div style="background:{pbg};border-radius:8px;padding:12px 16px;margin-bottom:14px;text-align:center">
    <div style="color:#64748b;font-size:11px;margin-bottom:4px">損益合計</div>
    <div style="color:{pcol};font-size:28px;font-weight:800;letter-spacing:-1px">{pnl:+,.0f}円</div>
    <div style="margin-top:8px;background:#1e2235;border-radius:4px;height:8px;overflow:hidden">
      <div style="background:{bar_col};height:8px;width:{bar_w}px;border-radius:4px;transition:width .5s"></div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px">
    <div style="background:#16192a;border-radius:6px;padding:8px 10px">
      <div style="color:#64748b;font-size:10px">勝率</div>
      <div style="color:#dde1ec;font-weight:700;font-size:15px">{wr:.1f}%</div>
    </div>
    <div style="background:#16192a;border-radius:6px;padding:8px 10px">
      <div style="color:#64748b;font-size:10px">平均損益/件</div>
      <div style="color:{_pnl_color(avg)};font-weight:700;font-size:15px">{avg:+,.0f}円</div>
    </div>
    <div style="background:#16192a;border-radius:6px;padding:8px 10px">
      <div style="color:#64748b;font-size:10px">約定 / 決済済</div>
      <div style="color:#dde1ec;font-weight:700">{n_ex}件 / {n_cl}件</div>
    </div>
    <div style="background:#16192a;border-radius:6px;padding:8px 10px">
      <div style="color:#64748b;font-size:10px">スキップ（資金/株価）</div>
      <div style="color:#94a3b8;font-weight:700">{skip_b} / {skip_p}件</div>
    </div>
  </div>
</div>"""

    cards_html = "\n".join(_rank_card(i, r) for i, r in enumerate(results))

    # ── 比較テーブル行 ────────────────────────────────────────────────────────
    def _table_row(i: int, r: dict) -> str:
        pnl   = r.get("total_pnl", 0)
        wr    = r.get("win_rate", 0)
        n_ex  = r.get("n_executed", 0)
        n_cl  = r.get("n_closed", 0)
        n_hld = r.get("n_holding", 0)
        avg   = pnl / n_cl if n_cl > 0 else 0
        skip_b = r.get("n_skipped_budget", 0)
        skip_p = r.get("n_skipped_price", 0)
        avg_req = r.get("avg_required", 0)
        medal  = MEDALS[i] if i < len(MEDALS) else f"{i+1}位"
        cfg    = next((c for c in SCRIPT_CONFIGS if c["label"] == r["label"]), {})
        accent = cfg.get("color", "#60a5fa")
        pcol   = _pnl_color(pnl)
        bar_pct = int(100 * min(abs(pnl) / max_pnl, 1.0))
        bar_col = pcol
        bg = "background:#111827" if i == 0 else ""
        return f"""<tr style="{bg}">
  <td style="text-align:center;font-size:18px">{medal}</td>
  <td><span style="color:{accent};font-weight:700">{r['label']}</span></td>
  <td style="text-align:right">{n_ex}</td>
  <td style="text-align:right">{n_cl}</td>
  <td style="text-align:right">{n_hld}</td>
  <td style="text-align:right;color:{'#34d399' if wr >= 55 else '#94a3b8'};font-weight:{'700' if wr>=55 else '400'}">{wr:.1f}%</td>
  <td style="text-align:right;color:{pcol};font-weight:700">{pnl:+,.0f}円</td>
  <td style="text-align:right;color:{_pnl_color(avg)}">{avg:+,.0f}円</td>
  <td style="text-align:right;color:#94a3b8">{avg_req:,.0f}円</td>
  <td style="text-align:right;color:#64748b">{skip_b} / {skip_p}</td>
  <td style="min-width:120px;padding:8px 12px">
    <div style="background:#1e2235;border-radius:4px;height:10px;overflow:hidden">
      <div style="background:{bar_col};height:10px;width:{bar_pct}%;border-radius:4px"></div>
    </div>
  </td>
</tr>"""

    table_rows = "\n".join(_table_row(i, r) for i, r in enumerate(results))

    # ── スキップ銘柄テーブル ──────────────────────────────────────────────────
    def _skipped_section(r: dict) -> str:
        top = r.get("top_skipped", [])
        if not top:
            return ""
        cfg    = next((c for c in SCRIPT_CONFIGS if c["label"] == r["label"]), {})
        accent = cfg.get("color", "#60a5fa")
        rows = "".join(
            f"<tr>"
            f"<td>{t['symbol']}</td>"
            f"<td>{t['name']}</td>"
            f"<td style='text-align:right;color:#f59e0b;font-weight:700'>{t['score']:.0f}点</td>"
            f"<td style='text-align:right'>{t['required']:,.0f}円</td>"
            f"<td>{t['entry_dt']}</td>"
            f"</tr>"
            for t in top
        )
        return f"""
<details style="margin-bottom:12px">
  <summary style="cursor:pointer;padding:10px 14px;background:#16192a;border-radius:6px;
                  color:{accent};font-weight:700;font-size:14px;list-style:none">
    ▶ {r['label']} — 資金不足スキップ上位 {len(top)}件
  </summary>
  <table style="margin-top:8px;width:auto">
    <thead><tr>
      <th>銘柄コード</th><th>銘柄名</th>
      <th style="text-align:right">スコア</th>
      <th style="text-align:right">必要資金</th>
      <th>約定予定日</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</details>"""

    skipped_html = "\n".join(_skipped_section(r) for r in results[:3])

    # ── 勝者サマリー ──────────────────────────────────────────────────────────
    best = results[0]
    best_pnl  = best.get("total_pnl", 0)
    best_cfg  = next((c for c in SCRIPT_CONFIGS if c["label"] == best["label"]), {})
    best_col  = best_cfg.get("color", "#34d399")
    diff_str  = ""
    if len(results) >= 2:
        diff = best_pnl - results[1].get("total_pnl", 0)
        diff_str = f"<span style='color:#94a3b8;font-size:14px'>2位との差: <strong style='color:#f59e0b'>{diff:+,.0f}円</strong></span>"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>資金効率比較レポート — {today_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#090b14;color:#dde1ec;font-family:'Segoe UI',Hiragino Sans,sans-serif;font-size:14px;line-height:1.6}}
h2{{font-size:18px;font-weight:700;color:#dde1ec;margin-bottom:16px}}
h3{{font-size:15px;font-weight:700;color:#94a3b8;margin-bottom:12px;letter-spacing:.5px}}
table{{border-collapse:collapse;width:100%}}
th{{background:#16192a;color:#64748b;font-size:11px;font-weight:600;letter-spacing:.5px;
    padding:8px 12px;text-align:left;border-bottom:1px solid #252840;white-space:nowrap}}
td{{padding:10px 12px;border-bottom:1px solid #1a1d2e;vertical-align:middle}}
tr:hover td{{background:#111520}}
.section{{padding:32px 40px;border-bottom:1px solid #1a1d2e}}
.chip{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.5px}}
</style>
</head>
<body>

<!-- ヘッダー -->
<div style="background:linear-gradient(135deg,#0d1117 0%,#111827 100%);
            padding:32px 40px;border-bottom:2px solid #252840">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:16px">
    <div>
      <div style="color:#64748b;font-size:12px;letter-spacing:1px;margin-bottom:6px">BUDGET SIMULATION REPORT</div>
      <h1 style="font-size:26px;font-weight:800;color:#dde1ec;letter-spacing:-0.5px">
        資金効率 スクリプト比較レポート
      </h1>
      <div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:16px;color:#94a3b8;font-size:13px">
        <span>📅 {today_str}</span>
        <span>📆 過去 {days} 日間</span>
        <span>💰 {budget_label}</span>
        <span>📊 {len(results)} スクリプト比較</span>
      </div>
    </div>
    <div style="background:#0f1117;border:1px solid {best_col};border-radius:10px;padding:14px 20px;min-width:220px">
      <div style="color:#64748b;font-size:11px;letter-spacing:.5px;margin-bottom:4px">🏆 最優秀スクリプト</div>
      <div style="color:{best_col};font-size:15px;font-weight:700">{best['label']}</div>
      <div style="color:#34d399;font-size:22px;font-weight:800;margin-top:4px">{best_pnl:+,.0f}円</div>
      <div style="margin-top:6px">{diff_str}</div>
    </div>
  </div>
</div>

<!-- ランキングカード -->
<div class="section">
  <h2>順位別パフォーマンス</h2>
  <div style="display:flex;flex-wrap:wrap;gap:16px">
    {cards_html}
  </div>
</div>

<!-- 比較テーブル -->
<div class="section">
  <h2>詳細比較テーブル</h2>
  <div style="overflow-x:auto">
    <table>
      <thead><tr>
        <th style="text-align:center">順位</th>
        <th>スクリプト</th>
        <th style="text-align:right">約定</th>
        <th style="text-align:right">決済済</th>
        <th style="text-align:right">保有中</th>
        <th style="text-align:right">勝率</th>
        <th style="text-align:right">損益合計</th>
        <th style="text-align:right">平均/件</th>
        <th style="text-align:right">平均必要資金</th>
        <th style="text-align:right">スキップ(資金/株価)</th>
        <th>損益バー</th>
      </tr></thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>
  <div style="margin-top:12px;color:#475569;font-size:11px">
    ※ スキップ(資金) = 資金不足で投資できなかった件数 ／ スキップ(株価) = 1ポジション必要額が総資金を超える件数
  </div>
</div>

<!-- 資金不足スキップ銘柄 -->
<div class="section">
  <h2>資金不足でスキップした高スコアシグナル（上位3スクリプト）</h2>
  <p style="color:#64748b;font-size:12px;margin-bottom:16px">
    スコアが高いのに資金不足で投資できなかった銘柄です。資金を増やすと追加で取れるシグナルです。
  </p>
  {skipped_html if skipped_html.strip() else '<p style="color:#475569">なし</p>'}
</div>

<!-- 結論 -->
<div class="section">
  <h2>結論・推奨</h2>
  <div style="background:#0f1117;border:2px solid {best_col};border-radius:12px;padding:24px 28px;max-width:700px">
    <div style="color:#64748b;font-size:12px;letter-spacing:.5px;margin-bottom:8px">
      利用可能資金 {budget_label} ／ 過去 {days} 日間
    </div>
    <div style="font-size:17px;font-weight:700;color:#dde1ec;margin-bottom:12px">
      最も利益が出るスクリプト:
      <span style="color:{best_col}">{best['label']}</span>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:20px;font-size:13px">
      <div>損益合計: <strong style="color:#34d399;font-size:18px">{best_pnl:+,.0f}円</strong></div>
      <div>約定: <strong>{best.get('n_executed',0)}件</strong></div>
      <div>勝率: <strong>{best.get('win_rate',0):.1f}%</strong></div>
    </div>
    {f'<div style="margin-top:12px;color:#94a3b8;font-size:13px">2位 [{results[1]["label"]}] との差: <strong style="color:#f59e0b">{best_pnl - results[1].get("total_pnl",0):+,.0f}円</strong></div>' if len(results) >= 2 else ''}
  </div>
  {"".join(f'<div style="margin-top:12px;color:#fb923c;font-size:13px">⚠ [{r["label"]}]: {r.get("n_skipped_price",0)}件が1ポジション必要額 &gt; {budget/10000:.0f}万円のため購入不可</div>' for r in results if r.get("n_skipped_price",0) > 0)}
</div>

<div style="padding:20px 40px;color:#334155;font-size:11px;text-align:center">
  生成日時: {datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")} JST ／
  期間: 過去{days}日 ／ 資金: {budget_label}
</div>

</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="資金制約付きシグナル比較シミュレーター")
    parser.add_argument("--cash",       type=float, default=2_100_000,
                        help="現物金額 (円, デフォルト 210万)")
    parser.add_argument("--lev",        type=float, default=2.0,
                        help="レバレッジ倍率 (デフォルト 2倍)")
    parser.add_argument("--budget",     type=float, default=None,
                        help="利用可能資金を直接指定 (--cash/--lev を上書き)")
    parser.add_argument("--days",       type=int,   default=365)
    parser.add_argument("--workers",    type=int,   default=4)
    parser.add_argument("--scripts",    nargs="+",
                        choices=[c["name"] for c in SCRIPT_CONFIGS],
                        default=None, help="比較するスクリプトを限定")
    parser.add_argument("--no-browser", action="store_true",
                        help="HTML生成のみ（ブラウザ自動起動なし）")
    args = parser.parse_args()

    budget = args.budget if args.budget is not None else args.cash * args.lev
    cash_arg = None if args.budget is not None else args.cash
    lev_arg  = None if args.budget is not None else args.lev

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"\n{'='*80}")
    print(f"  資金制約付き シグナル比較シミュレーション")
    print(f"  日付 : {today_str}")
    print(f"  期間 : 過去 {args.days} 日")
    print(f"  資金 : {budget/10000:.0f}万円"
          + (f"  (現物{args.cash/10000:.0f}万 × レバ{args.lev}倍)"
             if args.budget is None else ""))
    print(f"  戦略 : スコア高い順に資金内で投資、決済後は資金復活")
    print(f"{'='*80}")

    target_scripts = (
        [c for c in SCRIPT_CONFIGS if c["name"] in args.scripts]
        if args.scripts else SCRIPT_CONFIGS
    )

    results: list[dict] = []
    for sc in target_scripts:
        print(f"\n  [{sc['label']}] 実行中...", flush=True)
        r = run_worker(sc["name"], budget, args.days, args.workers)
        if r is None:
            continue
        r["label"] = sc["label"]
        results.append(r)
        n_ex = r.get("n_executed", 0)
        n_cl = r.get("n_closed", 0)
        pnl  = r.get("total_pnl", 0)
        wr   = r.get("win_rate", 0)
        skip = r.get("n_skipped_budget", 0)
        skip_p = r.get("n_skipped_price", 0)
        print(f"    約定 {n_ex}件 (決済済 {n_cl}件) / 資金不足スキップ {skip}件"
              f" / 予算超過スキップ {skip_p}件")
        print(f"    PnL: {pnl:+,.0f}円  勝率: {wr:.1f}%")

    if not results:
        print("\n  結果なし (全スクリプトでエラー)")
        return

    # ── コンソール比較テーブル ────────────────────────────────────────────────
    results.sort(key=lambda r: r.get("total_pnl", 0), reverse=True)
    max_pnl = max(abs(r.get("total_pnl", 0)) for r in results) or 1

    print(f"\n\n{'='*80}")
    print(f"  比較結果サマリー（過去{args.days}日・利用可能資金 {budget/10000:.0f}万円）")
    print(f"{'='*80}")
    print(f"  {'順位 スクリプト':<34} {'約定':>5} {'決済':>5} {'勝率':>6} "
          f"{'損益合計':>12} {'1件平均':>9}  損益バー")
    print(f"  {'-'*90}")

    medals_txt = ["🥇", "🥈", "🥉"] + ["   "] * 10
    for i, r in enumerate(results):
        n_ex  = r.get("n_executed", 0)
        n_cl  = r.get("n_closed", 0)
        pnl   = r.get("total_pnl", 0)
        avg   = pnl / n_cl if n_cl > 0 else 0
        wr    = r.get("win_rate", 0)
        bar   = _bar(pnl, max_pnl) if pnl > 0 else _bar(abs(pnl), max_pnl)
        bar_s = f"[{bar}]" if pnl >= 0 else f"[{bar}] ▼"
        print(f"  {medals_txt[i]} {r['label']:<31} {n_ex:>5} {n_cl:>5} {wr:>5.1f}% "
              f"{pnl:>12,.0f}円 {avg:>8,.0f}円  {bar_s}")

    # ── 資金制約で見逃した高スコアシグナル ────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  資金不足でスキップした高スコアシグナル Top（各スクリプト）")
    print(f"{'='*80}")
    for r in results[:3]:
        top = r.get("top_skipped", [])
        if not top:
            continue
        print(f"\n  ▶ {r['label']}")
        print(f"    {'銘柄コード':<12} {'銘柄名':<22} {'スコア':>5} {'必要資金':>10} {'約定日'}")
        for t in top:
            print(f"    {t['symbol']:<12} {t['name']:<22} {t['score']:>5.0f}点 "
                  f"{t['required']:>8,.0f}円  {t['entry_dt']}")

    # ── 結論 ───────────────────────────────────────────────────────────────
    best = results[0]
    print(f"\n{'='*80}")
    print(f"  ★ 結論（利用可能資金 {budget/10000:.0f}万円 / 過去{args.days}日）")
    print(f"{'='*80}")
    print(f"  最も利益が出るスクリプト: {best['label']}")
    print(f"    → PnL {best.get('total_pnl', 0):+,.0f}円"
          f"  (約定{best.get('n_executed',0)}件 / 勝率{best.get('win_rate',0):.1f}%)")

    if len(results) >= 2:
        diff = best.get("total_pnl", 0) - results[1].get("total_pnl", 0)
        print(f"    → 2位 [{results[1]['label']}] との差: {diff:+,.0f}円")

    for r in results:
        np_ = r.get("n_skipped_price", 0)
        if np_ > 0:
            print(f"\n  ⚠ [{r['label']}]: {np_}件が1ポジション必要額 > {budget/10000:.0f}万円"
                  f" のため購入不可（run_signals.py など株価上限ありのスクリプトを推奨）")

    print(f"{'='*80}\n")

    # ── HTML 生成 ──────────────────────────────────────────────────────────
    html = build_html(results, budget, args.days, cash_arg, lev_arg)
    out_path = Path(f"budget_sim_{today_str}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
