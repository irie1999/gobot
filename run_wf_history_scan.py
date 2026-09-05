"""
WF歴史検証スキャン専用スクリプト。
ユニバース全体をスキャンして指定日時点での銘柄選定 → OOS検証HTMLを生成。
通常シグナル分析は一切行わない。
"""
import argparse
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

parser = argparse.ArgumentParser(description="WF歴史検証スキャン（ユニバース全体）")
parser.add_argument("--wf-until", required=True,
                    help="WF選定基準日 (YYYY-MM-DD). この日以前のデータのみで銘柄選定")
parser.add_argument("--min-price", type=float, default=1000.0,
                    help="最低株価フィルタ (デフォルト: 1000円)")
parser.add_argument("--max-price", type=float, default=6000.0,
                    help="最高株価フィルタ (デフォルト: 6000円)")
parser.add_argument("--workers", type=int, default=1,
                    help="並列数 (デフォルト: 1)")
parser.add_argument("--universe", type=str, default=None,
                    help="ユニバースファイル (例: symbols_all.py=N225, デフォルト: symbols_listed_prime.py)")
parser.add_argument("--no-browser", action="store_true",
                    help="HTMLを生成するがブラウザを開かない")
args = parser.parse_args()

try:
    wf_until_date = date.fromisoformat(args.wf_until)
except ValueError:
    print(f"[ERROR] --wf-until の日付形式が不正: {args.wf_until} (YYYY-MM-DD)")
    sys.exit(1)

print(f"WF歴史検証スキャン開始")
print(f"  基準日    : {wf_until_date}")
print(f"  株価範囲  : {args.min_price:,.0f}〜{args.max_price:,.0f}円")
print(f"  workers   : {args.workers}")
print(f"  ユニバース: {args.universe or '自動検出 (symbols_listed_prime.py 優先)'}")
print(flush=True)

import nikkei_analysis as _na

html_body, _ = _na._wf_history_html(
    wf_until_date,
    workers=args.workers,
    max_price=args.max_price,
    min_price=args.min_price,
    universe_path=args.universe,
)

# ── HTML出力 ──────────────────────────────────────────────────────────
today_str = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
out_path = Path(f"wf_history_{wf_until_date}_{today_str}.html")

full_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WF歴史検証 {wf_until_date}</title>
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
print(f"\n出力: {out_path.resolve()}", flush=True)

if not args.no_browser:
    try:
        from _open_html import open_html
        open_html(out_path.resolve())
    except Exception:
        import webbrowser
        webbrowser.open(out_path.resolve().as_uri())
