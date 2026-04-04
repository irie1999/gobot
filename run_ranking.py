"""
4戦略バックテスト 総合レポート & ランキング
=============================================
■ 概要
  4つのバックテストをまとめて実行し、
  ・全戦略を1つのタブ付きHTMLに統合して自動表示
  ・ターミナルに推奨銘柄ランキングを出力
    - 資金フィルター: 株価 ≤ 5,000円（100株 ≤ 500,000円）
    - スコア: 30日損益×50% + 90日×25% + 180日×15% + 365日×10%

■ 実行方法
  # ① 銘柄ダウンロード（初回のみ）
  python download_tse_symbols.py --market prime

  # ② ランキング実行（1800銘柄 × 4戦略）
  python run_ranking.py

  # ③ バックテストをスキップして集計・表示のみ
  python run_ranking.py --no-run
"""

import argparse
import csv
import re
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST       = timezone(timedelta(hours=9))
MAX_PRICE = 5_000
PERIODS   = [30, 90, 180, 365]
WEIGHTS   = {30: 0.50, 90: 0.25, 180: 0.15, 365: 0.10}
TOP_N     = 20

STRATEGIES = [
    ("limit_oco",         "backtest_limit_oco.py",         "指値OCO戦略"),
    ("adaptive_mr",       "backtest_adaptive_mr.py",       "アダプティブMR戦略"),
    ("donchian_pullback", "backtest_donchian_pullback.py", "ドンチャン押し目戦略"),
    ("bb_volume",         "backtest_bb_volume.py",         "BB出来高戦略"),
]

HTML_GLOBS = {
    "limit_oco":         "watchlist_limit_oco_*.html",
    "adaptive_mr":       "watchlist_adaptive_mr_*.html",
    "donchian_pullback": "watchlist_donchian_*.html",
    "bb_volume":         "watchlist_bb_vol_*.html",
}


# ── バックテスト実行 ──────────────────────────────────────────────
def run_backtests(universe: str, days: int) -> None:
    for key, script, name in STRATEGIES:
        print(f"\n[{name}] バックテスト実行中... (universe={universe} / {days}日)", flush=True)
        result = subprocess.run(
            [sys.executable, script,
             "--universe", universe,
             "--days",     str(days),
             "--no-browser"],
            capture_output=False,
        )
        if result.returncode != 0:
            print(f"  ※ 警告: {script} が異常終了しました (code={result.returncode})")


# ── 最新HTMLを探す ─────────────────────────────────────────────────
def get_latest_html(glob_pattern: str) -> Path | None:
    files = sorted(Path(".").glob(glob_pattern),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


# ── 統合HTML生成 ──────────────────────────────────────────────────
def build_combined_html(tabs: list[tuple[str, str | None]], scan_dt: str) -> Path:
    """
    tabs: list of (strategy_name, html_path_or_None)
    統合タブHTMLを生成して返す。
    """
    tab_bodies   = []
    merged_css   = []
    seen_css     = set()

    for name, html_path in tabs:
        if not html_path or not Path(html_path).exists():
            tab_bodies.append(
                f'<p style="color:#f87171;padding:24px">レポートが見つかりません: {name}<br>'
                f'バックテストを先に実行してください。</p>')
            continue

        content = Path(html_path).read_text(encoding="utf-8")

        # スタイルを収集（重複除去）
        for s in re.findall(r'<style[^>]*>(.*?)</style>', content, re.DOTALL | re.IGNORECASE):
            key = s.strip()
            if key not in seen_css:
                seen_css.add(key)
                merged_css.append(key)

        # body内容を抽出し、script タグは除去（後で1つだけ書く）
        m = re.search(r'<body[^>]*>(.*?)</body>', content, re.DOTALL | re.IGNORECASE)
        body = m.group(1) if m else content
        body = re.sub(r'<script[^>]*>.*?</script>', '', body,
                      flags=re.DOTALL | re.IGNORECASE)
        tab_bodies.append(body.strip())

    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # タブボタン
    tab_btns = "\n".join(
        f'<button class="tab-btn{"  active" if i == 0 else ""}" '
        f'onclick="switchTab({i})">{name}</button>'
        for i, (name, _) in enumerate(tabs)
    )

    # タブコンテンツ
    _panes = []
    for i, body in enumerate(tab_bodies):
        _style = "" if i == 0 else ' style="display:none"'
        _panes.append(f'<div id="tc{i}" class="tab-pane"{_style}>\n{body}\n</div>')
    tab_contents = "\n".join(_panes)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>バックテスト総合レポート {today_str}</title>
<style>
{"".join(merged_css)}
*{{box-sizing:border-box;margin:0;padding:0}}
.tab-nav{{
  display:flex;gap:0;background:#090b14;padding:10px 16px 0;
  border-bottom:2px solid #252840;position:sticky;top:0;z-index:200;
  flex-wrap:wrap
}}
.tab-btn{{
  background:#16192a;color:#94a3b8;
  border:1px solid #252840;border-bottom:none;
  padding:9px 22px;margin-right:4px;
  border-radius:6px 6px 0 0;cursor:pointer;
  font-size:13px;font-family:inherit;
  transition:background .15s,color .15s
}}
.tab-btn:hover{{background:#1e2235;color:#dde1ec}}
.tab-btn.active{{
  background:#0f1117;color:#38bdf8;
  border-color:#38bdf8;border-bottom-color:#0f1117
}}
.tab-pane{{min-height:60vh}}
</style>
</head>
<body>
<div class="tab-nav">
{tab_btns}
</div>
{tab_contents}
<script>
function switchTab(n){{
  document.querySelectorAll('.tab-btn').forEach(function(b,i){{
    b.classList.toggle('active',i===n);
  }});
  document.querySelectorAll('.tab-pane').forEach(function(t,i){{
    t.style.display=i===n?'block':'none';
  }});
}}
function toggleDetail(id){{
  var r=document.getElementById('d_'+id);
  if(!r)return;
  var open=r.style.display==='table-row';
  r.style.display=open?'none':'table-row';
  var sym=r.previousElementSibling;
  if(sym){{
    var td=sym.querySelector('td');
    if(td)td.textContent=td.textContent.replace(
      /^[\u25b6\u25bc]\u00a0/,
      ''+(open?'\u25b6\u00a0':'\u25bc\u00a0')
    );
  }}
}}
</script>
</body>
</html>"""

    out = Path(f"report_combined_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    return out


# ── CSV 読み込み & ランキング ─────────────────────────────────────
def load_csv(key: str) -> list[dict]:
    f = Path(f"candidates_{key}.csv")
    if not f.exists():
        return []
    rows = []
    with open(f, newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for r in reader:
            try:
                entry = {
                    "symbol":     r["symbol"],
                    "name":       r["name"],
                    "last_close": float(r.get("last_close") or 0),
                }
                for d in PERIODS:
                    entry[f"{d}_n"]     = int(  r.get(f"{d}_n",     0) or 0)
                    entry[f"{d}_wr"]    = float(r.get(f"{d}_wr",    0) or 0)
                    entry[f"{d}_pf"]    = float(r.get(f"{d}_pf",    0) or 0)
                    entry[f"{d}_total"] = float(r.get(f"{d}_total", 0) or 0)
                rows.append(entry)
            except (ValueError, KeyError):
                continue
    return rows


def weighted_score(r: dict) -> float:
    return sum(WEIGHTS[d] * r[f"{d}_total"] for d in PERIODS)


def fmt_pnl(v: float, width: int = 9) -> str:
    if v == 0:
        return " " * (width - 1) + "—"
    return f"{v:>+{width},.0f}"


def print_ranking(key: str, name: str) -> None:
    data = load_csv(key)
    if not data:
        print(f"\n【{name}】データなし（candidates_{key}.csv が見つかりません）")
        return

    filtered = [r for r in data if 0 < r["last_close"] <= MAX_PRICE]
    filtered.sort(key=weighted_score, reverse=True)
    top = filtered[:TOP_N]

    print(f"\n{'─'*112}")
    print(f"  【{name}】推奨ランキング TOP{TOP_N}")
    print(f"  フィルター後: {len(filtered)}銘柄 / 全{len(data)}銘柄"
          f"  （株価 ≤ {MAX_PRICE:,}円 / 100株 ≤ {MAX_PRICE*100:,}円）")
    print(f"{'─'*112}")
    print(f"  {'順':>3}  {'コード':10}  {'銘柄名':20}  {'株価':>6}  "
          f"{'30日損益':>9}  {'90日損益':>9}  {'180日損益':>9}  {'365日損益':>9}  {'スコア':>9}")
    print(f"  {'─'*107}")
    for i, r in enumerate(top, 1):
        s = weighted_score(r)
        print(f"  {i:>3}  {r['symbol']:10}  {r['name']:20}  {r['last_close']:>6,.0f}  "
              f"{fmt_pnl(r['30_total'])}  {fmt_pnl(r['90_total'])}  "
              f"{fmt_pnl(r['180_total'])}  {fmt_pnl(r['365_total'])}  {s:>+9,.0f}")
    print()


# ── メイン ────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="4戦略バックテスト 総合レポート & ランキング",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python run_ranking.py                   # 全実行（1800銘柄 / 365日）
  python run_ranking.py --universe 225    # 日経225のみ（高速）
  python run_ranking.py --days 90        # 90日まで評価
  python run_ranking.py --no-run         # バックテストをスキップ（統合HTMLのみ再生成）
""")
    parser.add_argument("--universe",   default="all", choices=["watch", "225", "all"])
    parser.add_argument("--days",       type=int, default=365)
    parser.add_argument("--no-run",     action="store_true", help="バックテストをスキップ")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを自動起動しない")
    args = parser.parse_args()

    scan_dt = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    print(f"\n{'='*112}")
    print(f"  バックテスト総合レポート  {scan_dt}")
    print(f"  ウェイト: 30日×50%  90日×25%  180日×15%  365日×10%")
    print(f"  資金上限: {MAX_PRICE*100:,}円（100株 × {MAX_PRICE:,}円以下）")
    print(f"{'='*112}")

    if not args.no_run:
        run_backtests(args.universe, args.days)

    # ── ランキング出力 ────────────────────────────────────────────
    for key, _, name in STRATEGIES:
        print_ranking(key, name)

    # ── 統合HTML生成 & 自動起動 ───────────────────────────────────
    print("\n統合HTMLを生成中...")
    tabs = []
    for key, _, name in STRATEGIES:
        html_path = get_latest_html(HTML_GLOBS[key])
        tabs.append((name, str(html_path) if html_path else None))
        if html_path:
            print(f"  ✓ {name}: {html_path.name}")
        else:
            print(f"  ✗ {name}: HTMLなし")

    combined = build_combined_html(tabs, scan_dt)
    print(f"\n統合HTML: {combined.resolve()}")

    if not args.no_browser:
        webbrowser.open(f"file://{combined.resolve()}")


if __name__ == "__main__":
    main()
