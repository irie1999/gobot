"""
lss_audit_*.csv から 損切り5%以上のトレードをHTMLで表示する。
usage: python show_large_stops.py [csvfile] [--threshold 5.0]
csvfile 省略時は最新の lss_audit_*.csv を自動検出。
"""
import sys, glob, webbrowser, argparse
from pathlib import Path
import pandas as pd

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile", nargs="?", default=None)
    ap.add_argument("--threshold", type=float, default=5.0, help="損切り閾値%(デフォルト5.0)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.csvfile:
        csv_path = Path(args.csvfile)
    else:
        files = sorted(glob.glob("lss_audit_*.csv"), key=lambda p: Path(p).stat().st_mtime, reverse=True)
        if not files:
            print("lss_audit_*.csv が見つかりません。先に --audit <rule> を実行してください。")
            sys.exit(1)
        csv_path = Path(files[0])
        print(f"[自動検出] {csv_path}")

    df = pd.read_csv(csv_path, encoding="utf-8-sig")

    # 損切り % = -pnl_現実 / (entry * 100) * 100
    # 短売り: pnl = (entry - exit) * 100 → exit = entry - pnl/100
    # 損失率 = (exit - entry) / entry = -pnl / (entry*100)
    df["loss_pct"] = (-df["pnl_現実"] / (df["entry"] * 100) * 100).round(2)

    stop_rows = df[df["reason"].isin(["損切り", "ハード損切り"])].copy()
    large = stop_rows[stop_rows["loss_pct"] >= args.threshold].sort_values("loss_pct", ascending=False)

    rule_name = csv_path.stem.replace("lss_audit_", "")
    total_stops = len(stop_rows)

    print(f"\nルール: {rule_name}")
    print(f"損切り全{total_stops}件 → {args.threshold:.0f}%以上: {len(large)}件")

    # ─── HTML生成 ───
    rows_html = ""
    for _, r in large.iterrows():
        stop_p = r.get("stop", "")
        tgt_p  = r.get("target", "")
        pnl_楽  = r.get("pnl_report_楽観", r.get("pnl_楽観", ""))
        color = "#ffe0e0" if r["reason"] == "損切り" else "#ffd0d0"
        rows_html += f"""
        <tr style="background:{color}">
          <td>{r['date']}</td>
          <td><b>{r['sym']}</b></td>
          <td>{r.get('strat','')}</td>
          <td style="text-align:right">{r['entry']:,.0f}</td>
          <td style="text-align:right">{stop_p:,.0f}</td>
          <td style="text-align:right; color:red; font-weight:bold">{r['loss_pct']:.1f}%</td>
          <td style="text-align:right">{r['MAE最大逆行%']:.1f}%</td>
          <td style="text-align:right">{r['pnl_現実']:+,.0f}</td>
          <td style="text-align:right; color:#888">{r.get('現実−楽観', 0):+,.0f}</td>
          <td>{r['reason']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>損切り{args.threshold:.0f}%以上 [{rule_name}]</title>
<style>
body {{ font-family: 'Meiryo', sans-serif; padding: 20px; font-size: 13px; background:#f5f5f5; }}
h2 {{ color: #333; }}
.summary {{ background: #fff3cd; padding: 12px 18px; border-radius: 6px; margin-bottom: 16px; line-height:1.8; }}
table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 4px #ccc; }}
th {{ background: #333; color: white; padding: 8px 10px; text-align: left; }}
td {{ padding: 6px 10px; border-bottom: 1px solid #eee; }}
tr:hover {{ filter: brightness(0.95); }}
.note {{ font-size:11px; color:#666; margin-top:8px; }}
</style>
</head>
<body>
<h2>損切り {args.threshold:.0f}%以上トレード — ルール:「{rule_name}」</h2>
<div class="summary">
  対象CSV: {csv_path.name}<br>
  損切り全件: <b>{total_stops}</b> 件 &nbsp;|&nbsp;
  {args.threshold:.0f}%以上: <b style="color:red">{len(large)}</b> 件
  &nbsp;({len(large)/total_stops*100:.1f}%)<br>
  合計損失(現実): <b style="color:red">{large['pnl_現実'].sum():+,.0f}円</b>
</div>
<table>
  <thead>
    <tr>
      <th>日付</th><th>銘柄</th><th>戦略</th>
      <th>約定価格</th><th>損切り基準値</th>
      <th>損失%<br>(現実)</th>
      <th>MAE%<br>(最大逆行)</th>
      <th>pnl現実</th>
      <th>現実−楽観</th>
      <th>理由</th>
    </tr>
  </thead>
  <tbody>
    {rows_html if rows_html else '<tr><td colspan="10" style="padding:20px;color:green;text-align:center">該当なし</td></tr>'}
  </tbody>
</table>
<p class="note">
  損失% = -pnl現実 / (約定価格×100株) × 100<br>
  MAE% = 約定〜決済まで価格がショートに逆行した最大値<br>
  現実−楽観 = マイナスほど実際の損失が楽観モデルより大きかった
</p>
</body>
</html>"""

    out = Path(f"large_stops_{rule_name}.html")
    out.write_text(html, encoding="utf-8")
    print(f"[出力] {out}")
    if not args.no_browser:
        webbrowser.open(str(out.resolve()))

if __name__ == "__main__":
    main()
