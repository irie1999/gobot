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
  python budget_sim.py                             # デフォルト: 4.2M円, 30/90/180/365日
  python budget_sim.py --cash 2100000 --lev 2      # 現物210万×レバ2倍
  python budget_sim.py --days 30 90 365            # 期間を指定
  python budget_sim.py --days 365                  # 単一期間
  python budget_sim.py --workers 4                 # バックテスト並列数
  python budget_sim.py --scripts run_signals run_signals_prime  # 特定スクリプトのみ
  python budget_sim.py --no-browser                # HTML生成のみ（ブラウザ起動なし）
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
    {"name": "run_signals",            "label": "① 標準",          "color": "#60a5fa"},
    {"name": "run_signals_merged",     "label": "② 統合WF",        "color": "#818cf8"},
    {"name": "run_signals_aggressive", "label": "③ Aggressive",    "color": "#fb923c"},
    {"name": "run_signals_prime",      "label": "④ Prime WF",      "color": "#34d399"},
    {"name": "run_signals_nolimit",    "label": "⑤ Nolimit WF",    "color": "#f472b6"},
]

DEFAULT_DAYS = [30, 90, 180, 365]
MEDALS      = ["🥇", "🥈", "🥉", "4位", "5位"]
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


def _pnl_color(pnl: float) -> str:
    return "#34d399" if pnl >= 0 else "#f87171"


def _pnl_bg(pnl: float) -> str:
    return "rgba(52,211,153,.10)" if pnl >= 0 else "rgba(248,113,113,.10)"


def _bar(value: float, max_val: float, width: int = 20) -> str:
    if max_val <= 0:
        return " " * width
    filled = int(width * min(value / max_val, 1.0))
    return "█" * filled + "░" * (width - filled)


def _accent(label: str) -> str:
    cfg = next((c for c in SCRIPT_CONFIGS if c["label"] == label), {})
    return cfg.get("color", "#60a5fa")


# ─────────────────────────────────────────────────────────────────────────────
# HTML 生成: 単一期間
# ─────────────────────────────────────────────────────────────────────────────
def _single_period_section(results: list[dict], days: int, budget: float) -> str:
    """単一期間の詳細テーブルHTML（<section>要素として返す）。"""
    sorted_r = sorted(results, key=lambda r: r.get("total_pnl", 0), reverse=True)
    max_pnl = max(abs(r.get("total_pnl", 0)) for r in sorted_r) or 1

    def _row(i: int, r: dict) -> str:
        pnl    = r.get("total_pnl", 0)
        wr     = r.get("win_rate", 0)
        n_ex   = r.get("n_executed", 0)
        n_cl   = r.get("n_closed", 0)
        n_hld  = r.get("n_holding", 0)
        avg    = pnl / n_cl if n_cl > 0 else 0
        skip_b = r.get("n_skipped_budget", 0)
        skip_p = r.get("n_skipped_price", 0)
        avg_req = r.get("avg_required", 0)
        medal  = MEDALS[i] if i < len(MEDALS) else f"{i+1}位"
        acc    = _accent(r["label"])
        pcol   = _pnl_color(pnl)
        bar_pct = int(100 * min(abs(pnl) / max_pnl, 1.0))
        bg = "background:#0d1520" if i == 0 else ""
        return (f'<tr style="{bg}">'
                f'<td style="text-align:center;font-size:16px">{medal}</td>'
                f'<td><span style="color:{acc};font-weight:700">{r["label"]}</span></td>'
                f'<td style="text-align:right">{n_ex}</td>'
                f'<td style="text-align:right">{n_cl}</td>'
                f'<td style="text-align:right;color:#64748b">{n_hld}</td>'
                f'<td style="text-align:right;color:{"#34d399" if wr>=55 else "#94a3b8"};'
                f'font-weight:{"700" if wr>=55 else "400"}">{wr:.1f}%</td>'
                f'<td style="text-align:right;color:{pcol};font-weight:700">{pnl:+,.0f}円</td>'
                f'<td style="text-align:right;color:{_pnl_color(avg)}">{avg:+,.0f}円</td>'
                f'<td style="text-align:right;color:#94a3b8">{avg_req:,.0f}円</td>'
                f'<td style="text-align:right;color:#64748b">{skip_b}/{skip_p}</td>'
                f'<td style="min-width:100px;padding:8px 12px">'
                f'<div style="background:#1e2235;border-radius:4px;height:8px;overflow:hidden">'
                f'<div style="background:{pcol};height:8px;width:{bar_pct}%;border-radius:4px"></div>'
                f'</div></td>'
                f'</tr>')

    rows_html = "\n".join(_row(i, r) for i, r in enumerate(sorted_r))
    best = sorted_r[0]
    acc  = _accent(best["label"])

    return f"""
<div class="section">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap">
    <h2 style="margin:0">過去 {days} 日間の比較</h2>
    <div style="background:#0f1117;border:1px solid {acc};border-radius:8px;
                padding:8px 16px;display:flex;align-items:center;gap:12px">
      <span style="color:#64748b;font-size:11px">🏆 1位</span>
      <span style="color:{acc};font-weight:700">{best['label']}</span>
      <span style="color:{_pnl_color(best.get('total_pnl',0))};font-weight:800;font-size:16px">
        {best.get('total_pnl',0):+,.0f}円
      </span>
    </div>
  </div>
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
        <th style="text-align:right">スキップ(資/株)</th>
        <th>損益バー</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>"""


# ─────────────────────────────────────────────────────────────────────────────
# HTML 生成: 複数期間マトリクス
# ─────────────────────────────────────────────────────────────────────────────
def _matrix_section(all_results: dict[int, list[dict]], days_list: list[int]) -> str:
    """全スクリプト × 全期間のマトリクステーブル。"""
    # スクリプトの順序を ① ② ③ ④ ⑤ に固定
    label_order = [c["label"] for c in SCRIPT_CONFIGS]
    all_labels = []
    for d in days_list:
        for r in all_results.get(d, []):
            if r["label"] not in all_labels:
                all_labels.append(r["label"])
    labels = [l for l in label_order if l in all_labels]

    # 期間ごとの最大PnL（バー幅計算用）
    max_pnl_per_days = {
        d: max((abs(r.get("total_pnl", 0)) for r in all_results.get(d, [])), default=1) or 1
        for d in days_list
    }

    # 各スクリプトの期間ごとの順位
    rank_map: dict[tuple, int] = {}
    for d in days_list:
        sorted_r = sorted(all_results.get(d, []), key=lambda r: r.get("total_pnl", 0), reverse=True)
        for i, r in enumerate(sorted_r):
            rank_map[(r["label"], d)] = i + 1

    # スクリプトごとの「平均順位」でソート（小さいほど優秀）
    def avg_rank(label: str) -> float:
        ranks = [rank_map.get((label, d), 99) for d in days_list]
        return sum(ranks) / len(ranks)

    labels = sorted(labels, key=avg_rank)

    # ヘッダー
    day_headers = "".join(
        f'<th colspan="2" style="text-align:center;border-left:1px solid #252840">{d}日</th>'
        for d in days_list
    )
    day_subheads = "".join(
        '<th style="text-align:right;border-left:1px solid #252840">損益</th>'
        '<th style="text-align:right">勝率</th>'
        for _ in days_list
    )

    # 行
    def _matrix_row(label: str) -> str:
        acc = _accent(label)
        # 総合スコア（平均順位）
        ar = avg_rank(label)
        medal_idx = int(ar) - 1
        overall = MEDALS[medal_idx] if 0 <= medal_idx < len(MEDALS) else ""

        cells = ""
        for d in days_list:
            result = next((r for r in all_results.get(d, []) if r["label"] == label), None)
            if result is None:
                cells += '<td style="text-align:right;color:#334155;border-left:1px solid #252840">—</td><td>—</td>'
                continue
            pnl  = result.get("total_pnl", 0)
            wr   = result.get("win_rate", 0)
            rank = rank_map.get((label, d), 99)
            pcol = _pnl_color(pnl)
            rank_badge = ""
            if rank == 1:
                rank_badge = '<span style="font-size:14px">🥇</span>'
            elif rank == 2:
                rank_badge = '<span style="font-size:14px">🥈</span>'
            elif rank == 3:
                rank_badge = '<span style="font-size:14px">🥉</span>'
            max_p = max_pnl_per_days[d]
            bar_pct = int(60 * min(abs(pnl) / max_p, 1.0))
            bar_html = (f'<div style="display:inline-block;background:#1e2235;'
                        f'border-radius:3px;height:6px;width:60px;vertical-align:middle;margin-left:4px;overflow:hidden">'
                        f'<div style="background:{pcol};height:6px;width:{bar_pct}px;border-radius:3px"></div>'
                        f'</div>')
            cells += (f'<td style="text-align:right;border-left:1px solid #1a1d2e;white-space:nowrap">'
                      f'{rank_badge}<span style="color:{pcol};font-weight:700">{pnl:+,.0f}円</span>'
                      f'{bar_html}</td>'
                      f'<td style="text-align:right;color:{"#34d399" if wr>=55 else "#94a3b8"}">{wr:.1f}%</td>')

        # 連続1位カウント
        top_count = sum(1 for d in days_list if rank_map.get((label, d), 99) == 1)
        top2_count = sum(1 for d in days_list if rank_map.get((label, d), 99) <= 2)
        consistency_col = "#34d399" if top2_count >= len(days_list) - 1 else "#94a3b8"
        consistency = f"{top_count}回1位 / {top2_count}回2位以内"

        bg = "background:#0d1520" if avg_rank(label) <= 1.5 else ""
        return (f'<tr style="{bg}">'
                f'<td><span style="color:{acc};font-weight:700">{label}</span></td>'
                f'{cells}'
                f'<td style="text-align:center;color:{consistency_col};font-size:12px">{consistency}</td>'
                f'</tr>')

    rows_html = "\n".join(_matrix_row(label) for label in labels)

    return f"""
<div class="section">
  <h2>📊 マルチ期間 比較マトリクス</h2>
  <p style="color:#64748b;font-size:12px;margin-bottom:16px">
    複数期間を通じて安定して上位のスクリプトが最も信頼できます。
    「連続1位/2位以内」の多いスクリプトを選ぶのがおすすめです。
  </p>
  <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th rowspan="2">スクリプト</th>
          {day_headers}
          <th rowspan="2" style="text-align:center">安定性</th>
        </tr>
        <tr>{day_subheads}</tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <div style="margin-top:10px;color:#475569;font-size:11px">
    ※ 🥇🥈🥉 = その期間での順位 ／ 勝率55%以上は緑色表示
  </div>
</div>"""


# ─────────────────────────────────────────────────────────────────────────────
# メインHTML
# ─────────────────────────────────────────────────────────────────────────────
def build_html(all_results: dict[int, list[dict]], budget: float, days_list: list[int],
               cash: float | None, lev: float | None) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    budget_label = f"{budget/10000:.0f}万円"
    if cash is not None and lev is not None:
        budget_label += f"（現物{cash/10000:.0f}万 × レバ{lev:.1f}倍）"

    is_multi = len(days_list) > 1

    # 代表期間（最長）の結果でヘッダー用ベスト算出
    rep_days  = max(days_list)
    rep_results = sorted(all_results.get(rep_days, []),
                         key=lambda r: r.get("total_pnl", 0), reverse=True)
    best     = rep_results[0] if rep_results else {}
    best_col = _accent(best.get("label", ""))
    best_pnl = best.get("total_pnl", 0)
    diff_str = ""
    if len(rep_results) >= 2:
        diff = best_pnl - rep_results[1].get("total_pnl", 0)
        diff_str = f'<span style="color:#94a3b8;font-size:13px">2位との差: <strong style="color:#f59e0b">{diff:+,.0f}円</strong></span>'

    days_label = " / ".join(f"{d}日" for d in days_list)

    # セクション組み立て
    matrix_html = _matrix_section(all_results, days_list) if is_multi else ""
    period_sections = "\n".join(
        _single_period_section(all_results[d], d, budget)
        for d in sorted(days_list, reverse=True)
    )

    # スキップ銘柄（代表期間）
    def _skipped_section(r: dict) -> str:
        top = r.get("top_skipped", [])
        if not top:
            return ""
        acc  = _accent(r["label"])
        rows = "".join(
            f'<tr><td>{t["symbol"]}</td><td>{t["name"]}</td>'
            f'<td style="text-align:right;color:#f59e0b;font-weight:700">{t["score"]:.0f}点</td>'
            f'<td style="text-align:right">{t["required"]:,.0f}円</td>'
            f'<td>{t["entry_dt"]}</td></tr>'
            for t in top
        )
        return f"""
<details style="margin-bottom:12px">
  <summary style="cursor:pointer;padding:10px 14px;background:#16192a;border-radius:6px;
                  color:{acc};font-weight:700;font-size:14px;list-style:none">
    ▶ {r['label']} — 資金不足スキップ上位 {len(top)}件
  </summary>
  <table style="margin-top:8px;width:auto">
    <thead><tr><th>銘柄コード</th><th>銘柄名</th>
      <th style="text-align:right">スコア</th>
      <th style="text-align:right">必要資金</th>
      <th>約定予定日</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</details>"""

    skipped_html = "\n".join(_skipped_section(r) for r in rep_results[:3])

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>資金効率比較レポート — {today_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#090b14;color:#dde1ec;font-family:'Segoe UI',Hiragino Sans,sans-serif;
      font-size:14px;line-height:1.6}}
h2{{font-size:18px;font-weight:700;color:#dde1ec;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%}}
th{{background:#16192a;color:#64748b;font-size:11px;font-weight:600;letter-spacing:.5px;
    padding:8px 12px;text-align:left;border-bottom:1px solid #252840;white-space:nowrap}}
td{{padding:9px 12px;border-bottom:1px solid #1a1d2e;vertical-align:middle}}
tr:hover td{{background:#111520}}
.section{{padding:28px 40px;border-bottom:1px solid #1a1d2e}}
</style>
</head>
<body>

<!-- ヘッダー -->
<div style="background:linear-gradient(135deg,#0d1117 0%,#111827 100%);
            padding:32px 40px;border-bottom:2px solid #252840">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:16px">
    <div>
      <div style="color:#64748b;font-size:12px;letter-spacing:1px;margin-bottom:6px">
        BUDGET SIMULATION REPORT
      </div>
      <h1 style="font-size:26px;font-weight:800;color:#dde1ec;letter-spacing:-0.5px">
        資金効率 スクリプト比較レポート
      </h1>
      <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:20px;color:#94a3b8;font-size:13px">
        <span>📅 {today_str}</span>
        <span>📆 {days_label}</span>
        <span>💰 {budget_label}</span>
        <span>📊 {len(rep_results)} スクリプト</span>
      </div>
    </div>
    <div style="background:#0f1117;border:1px solid {best_col};border-radius:10px;
                padding:14px 20px;min-width:220px">
      <div style="color:#64748b;font-size:11px;letter-spacing:.5px;margin-bottom:4px">
        🏆 最優秀（{rep_days}日）
      </div>
      <div style="color:{best_col};font-size:15px;font-weight:700">{best.get('label','—')}</div>
      <div style="color:#34d399;font-size:22px;font-weight:800;margin-top:4px">
        {best_pnl:+,.0f}円
      </div>
      <div style="margin-top:6px">{diff_str}</div>
    </div>
  </div>
</div>

{matrix_html}

{period_sections}

<!-- 資金不足スキップ銘柄（代表期間） -->
<div class="section">
  <h2>資金不足でスキップした高スコアシグナル（{rep_days}日・上位3スクリプト）</h2>
  <p style="color:#64748b;font-size:12px;margin-bottom:16px">
    スコアが高いのに資金不足で投資できなかった銘柄。資金を増やすと取れるシグナルです。
  </p>
  {skipped_html if skipped_html.strip() else '<p style="color:#475569">なし</p>'}
</div>

<div style="padding:20px 40px;color:#334155;font-size:11px;text-align:center">
  生成日時: {datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")} JST ／
  期間: {days_label} ／ 資金: {budget_label}
</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="資金制約付きシグナル比較シミュレーター")
    parser.add_argument("--cash",       type=float, default=2_100_000)
    parser.add_argument("--lev",        type=float, default=2.0)
    parser.add_argument("--budget",     type=float, default=None,
                        help="利用可能資金を直接指定 (--cash/--lev を上書き)")
    parser.add_argument("--days",       type=int,   nargs="+", default=DEFAULT_DAYS,
                        help=f"比較する期間(日)。複数指定可 (デフォルト: {DEFAULT_DAYS})")
    parser.add_argument("--workers",    type=int,   default=4)
    parser.add_argument("--scripts",    nargs="+",
                        choices=[c["name"] for c in SCRIPT_CONFIGS],
                        default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    budget   = args.budget if args.budget is not None else args.cash * args.lev
    cash_arg = None if args.budget is not None else args.cash
    lev_arg  = None if args.budget is not None else args.lev
    days_list = sorted(set(args.days))

    target_scripts = (
        [c for c in SCRIPT_CONFIGS if c["name"] in args.scripts]
        if args.scripts else SCRIPT_CONFIGS
    )

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"\n{'='*80}")
    print(f"  資金制約付き シグナル比較シミュレーション")
    print(f"  日付 : {today_str}")
    print(f"  期間 : {' / '.join(f'{d}日' for d in days_list)}")
    print(f"  資金 : {budget/10000:.0f}万円"
          + (f"  (現物{args.cash/10000:.0f}万 × レバ{args.lev}倍)"
             if args.budget is None else ""))
    print(f"  戦略 : スコア高い順に資金内で投資、決済後は資金復活")
    print(f"{'='*80}")

    # 全 (days, script) の組み合わせを実行
    all_results: dict[int, list[dict]] = {}
    for days in days_list:
        print(f"\n━━ 期間: {days}日 ━━")
        period_results: list[dict] = []
        for sc in target_scripts:
            print(f"  [{sc['label']}] 実行中...", flush=True)
            r = run_worker(sc["name"], budget, days, args.workers)
            if r is None:
                continue
            r["label"] = sc["label"]
            period_results.append(r)
            print(f"    約定{r.get('n_executed',0)}件  "
                  f"PnL:{r.get('total_pnl',0):+,.0f}円  "
                  f"勝率:{r.get('win_rate',0):.1f}%")
        period_results.sort(key=lambda r: r.get("total_pnl", 0), reverse=True)
        all_results[days] = period_results

    if not any(all_results.values()):
        print("\n  結果なし (全スクリプトでエラー)")
        return

    # ── コンソール サマリー ──────────────────────────────────────────────────
    for days in days_list:
        results = all_results[days]
        if not results:
            continue
        max_pnl = max(abs(r.get("total_pnl", 0)) for r in results) or 1
        print(f"\n{'='*80}")
        print(f"  【{days}日】比較サマリー  資金: {budget/10000:.0f}万円")
        print(f"{'='*80}")
        print(f"  {'順位 スクリプト':<30} {'約定':>5} {'決済':>5} {'勝率':>6} "
              f"{'損益合計':>12} {'平均/件':>9}  バー")
        print(f"  {'-'*85}")
        medals_txt = ["🥇", "🥈", "🥉"] + ["   "] * 10
        for i, r in enumerate(results):
            n_cl  = r.get("n_closed", 0)
            pnl   = r.get("total_pnl", 0)
            avg   = pnl / n_cl if n_cl > 0 else 0
            wr    = r.get("win_rate", 0)
            bar   = _bar(abs(pnl), max_pnl)
            bar_s = f"[{bar}]" if pnl >= 0 else f"[{bar}]▼"
            print(f"  {medals_txt[i]} {r['label']:<27} "
                  f"{r.get('n_executed',0):>5} {n_cl:>5} {wr:>5.1f}% "
                  f"{pnl:>12,.0f}円 {avg:>8,.0f}円  {bar_s}")

    # ── HTML 生成 ────────────────────────────────────────────────────────────
    html     = build_html(all_results, budget, days_list, cash_arg, lev_arg)
    out_path = Path(f"budget_sim_{today_str}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
