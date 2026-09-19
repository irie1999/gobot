"""
複数基準日WF歴史検証 - クロス期間比較レポート生成。

複数の独立した時間軸でシグナルの再現性・安定性を検証する。
全期間でOOS損益がプラス → シグナルが本物の証拠。

使い方:
  # デフォルト: 6ヶ月間隔で直近4期間を自動生成
  python run_wf_multi.py --min-price 1000 --max-price 6000

  # 基準日を明示指定
  python run_wf_multi.py --wf-dates 2024-06-21,2024-12-21,2025-06-21,2025-12-21

  # 期間数を変える (6ヶ月間隔 × N期間)
  python run_wf_multi.py --periods 6
"""
import argparse
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

parser = argparse.ArgumentParser(description="複数基準日WF歴史検証（クロス期間比較）")
parser.add_argument("--wf-dates",
                    help="カンマ区切り基準日 (例: 2024-06-21,2024-12-21,...)")
parser.add_argument("--periods", type=int, default=4,
                    help="自動生成する期間数・6ヶ月間隔 (デフォルト: 4)")
parser.add_argument("--min-price", type=float, default=1000.0)
parser.add_argument("--max-price", type=float, default=10000.0)
parser.add_argument("--workers", type=int, default=1)
parser.add_argument("--universe", type=str, default=None)
parser.add_argument("--no-browser", action="store_true")
args = parser.parse_args()

# ── 基準日リストの決定 ─────────────────────────────────────────────────────
if args.wf_dates:
    try:
        dates = [date.fromisoformat(d.strip()) for d in args.wf_dates.split(",")]
    except ValueError as e:
        print(f"[ERROR] 日付形式エラー: {e}")
        sys.exit(1)
else:
    # 6ヶ月（183日）間隔で N 期間さかのぼる
    today = date.today()
    dates = []
    d = today
    for _ in range(args.periods):
        d = d - timedelta(days=183)
        dates.insert(0, d)

print("=" * 60)
print("複数基準日 WF歴史検証")
print(f"  基準日数  : {len(dates)}件")
print(f"  基準日    : {', '.join(str(d) for d in dates)}")
print(f"  株価範囲  : {args.min_price:,.0f}〜{args.max_price:,.0f}円")
print(f"  workers   : {args.workers}")
print("=" * 60, flush=True)

import nikkei_analysis as _na

_today = datetime.now(timezone(timedelta(hours=9))).date()
_today_str = _today.isoformat()

# ── 各基準日でスキャン実行 ───────────────────────────────────────────────
results = []  # (wf_date, stats_36, html_path)

for i, wf_date in enumerate(dates):
    print(f"\n[{i+1}/{len(dates)}] 基準日: {wf_date}", flush=True)
    out_path = Path(f"wf_history_{wf_date}.html")

    html_body, stats = _na._wf_history_html(
        wf_date,
        workers=args.workers,
        max_price=args.max_price,
        min_price=args.min_price,
        universe_path=args.universe,
    )

    full_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WF歴史検証 {wf_date}</title>
<style>
  body {{ background:#0f172a; color:#e2e8f0; font-family:'Segoe UI',sans-serif;
         margin:0; padding:20px; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    out_path.write_text(full_html, encoding="utf-8")
    print(f"  → 保存: {out_path.resolve()}", flush=True)
    results.append((wf_date, stats, out_path))

# ── クロス期間比較HTMLを生成 ─────────────────────────────────────────────
def _pf_str(pf):
    if pf == float("inf"):
        return "∞"
    return f"{pf:.2f}"

def _verdict(pnl, pf, n):
    if n == 0:
        return '<span style="color:#64748b">データなし</span>'
    if pnl > 0 and pf >= 1.0:
        return '<span style="color:#4ade80;font-size:1.2rem">✓</span>'
    if pnl > 0:
        return '<span style="color:#fbbf24;font-size:1.2rem">△</span>'
    return '<span style="color:#f87171;font-size:1.2rem">✗</span>'

_th = "background:#1e293b;color:#94a3b8;padding:8px 14px;text-align:center;border:1px solid #334155;font-size:0.82rem"
_td = "padding:8px 14px;border:1px solid #1e293b;text-align:right;font-size:0.85rem"
_tdl = "padding:8px 14px;border:1px solid #1e293b;text-align:left;font-size:0.85rem"

summary_rows = ""
all_positive = True

for wf_date, stats, out_path in results:
    oos_days = (_today - wf_date).days
    s_all   = stats.get("2_all_0",   {"n": 0, "wins": 0, "wr": 0, "pf": 0, "pnl": 0})
    s_long  = stats.get("2_long_0",  {"n": 0, "wins": 0, "wr": 0, "pf": 0, "pnl": 0})
    s_short = stats.get("2_short_0", {"n": 0, "wins": 0, "wr": 0, "pf": 0, "pnl": 0})

    pnl   = s_all["pnl"]
    pc    = "#4ade80" if pnl > 0 else "#f87171"
    lpc   = "#4ade80" if s_long["pnl"]  > 0 else "#f87171"
    spc   = "#4ade80" if s_short["pnl"] > 0 else "#f87171"
    verd  = _verdict(pnl, s_all["pf"], s_all["n"])
    if pnl <= 0:
        all_positive = False

    summary_rows += f"""<tr>
      <td style="{_tdl}"><a href="{out_path.name}" style="color:#38bdf8">{wf_date}</a></td>
      <td style="{_td}">{oos_days}日<br><span style="color:#64748b;font-size:0.75rem">（{oos_days//30}ヶ月）</span></td>
      <td style="{_td}">{s_all["n"]}件</td>
      <td style="{_td}">{s_all["wr"]:.1f}%</td>
      <td style="{_td}">{_pf_str(s_all["pf"])}</td>
      <td style="{_td};color:{pc};font-weight:bold">{pnl:+,.0f}円</td>
      <td style="{_td};color:{lpc}">{s_long["pnl"]:+,.0f}円</td>
      <td style="{_td};color:{spc}">{s_short["pnl"]:+,.0f}円</td>
      <td style="{_td};text-align:center">{verd}</td>
    </tr>\n"""

overall_color = "#4ade80" if all_positive else "#fbbf24"
overall_msg = "✓ 全期間プラス — シグナルに再現性あり" if all_positive else "△ 一部マイナス期間あり — 継続検証を推奨"

detail_links = "".join(
    f'<a href="{p.name}" style="color:#38bdf8;margin-right:16px;font-size:0.85rem">{d} の詳細 →</a>'
    for d, _, p in results
)

multi_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WF歴史検証 クロス期間比較 {_today_str}</title>
<style>
  body {{ background:#0f172a; color:#e2e8f0; font-family:'Segoe UI',sans-serif;
         margin:0; padding:20px; }}
  a {{ text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
<div style="max-width:1100px;margin:0 auto">
  <h2 style="color:#38bdf8;margin:0 0 4px">WF歴史検証 クロス期間比較</h2>
  <p style="color:#64748b;font-size:0.82rem;margin:0 0 20px">
    複数の独立した基準日でウォークフォワード選定 → OOS検証。
    全期間でプラスならシグナルの再現性あり（フォワードテストと等価）。
    ≥2fold通過・WFスコアフィルタなし（すべて）の集計。
  </p>

  <!-- 総合判定 -->
  <div style="background:#1e293b;border-left:4px solid {overall_color};
              padding:12px 20px;margin-bottom:24px;border-radius:0 6px 6px 0">
    <span style="color:{overall_color};font-size:1.1rem;font-weight:bold">{overall_msg}</span>
  </div>

  <!-- 比較テーブル -->
  <div style="overflow-x:auto;margin-bottom:24px">
  <table style="border-collapse:collapse;min-width:800px;width:100%">
    <thead><tr>
      <th style="{_th}">基準日<br><span style="font-size:0.72rem;color:#64748b">（詳細リンク）</span></th>
      <th style="{_th}">OOS期間</th>
      <th style="{_th}">取引数<br><span style="font-size:0.72rem;color:#64748b">≥2fold</span></th>
      <th style="{_th}">勝率</th>
      <th style="{_th}">PF</th>
      <th style="{_th}">損益（全体）</th>
      <th style="{_th}">損益（ロング）</th>
      <th style="{_th}">損益（ショート）</th>
      <th style="{_th}">判定</th>
    </tr></thead>
    <tbody>
{summary_rows}
    </tbody>
  </table>
  </div>

  <div style="color:#64748b;font-size:0.8rem;margin-bottom:8px">各期間の詳細レポート:</div>
  <div style="margin-bottom:24px">{detail_links}</div>

  <div style="background:#1e293b;border-radius:6px;padding:14px 18px;font-size:0.82rem;color:#94a3b8">
    <strong style="color:#e2e8f0">判定基準</strong><br>
    ✓ = OOS損益プラス かつ PF≥1.0 &nbsp;|&nbsp;
    △ = OOS損益プラスだが PF&lt;1.0 &nbsp;|&nbsp;
    ✗ = OOS損益マイナス
  </div>
</div>
</body>
</html>"""

multi_path = Path(f"wf_multi_{_today_str}.html")
multi_path.write_text(multi_html, encoding="utf-8")
print(f"\n比較レポート: {multi_path.resolve()}", flush=True)

if not args.no_browser:
    try:
        from _open_html import open_html
        open_html(multi_path.resolve())
    except Exception:
        import webbrowser
        webbrowser.open(multi_path.resolve().as_uri())
