"""
compare_entry_times_daytrade.py  ―  ENTRY_START 複数パターン比較
==================================================================
holdout_periods_report_daytrade.py を ENTRY_START の異なる値で
連続実行し、最適な寄付き開始時刻を発見する。

【検証パターン】
  09:00 (寄付き直後、最大ボラ獲得)
  09:15 (15分待ち)
  09:30 (現行デフォルト、保守)
  09:45 (落ち着き待ち)

【出力】
  holdout_periods_<variant>_<HHMM>_<日付>.html (各パターン別)
  compare_entry_times_<variant>_<日付>.html (統合比較HTML)

【使い方】
  python compare_entry_times_daytrade.py --short                # ショート4パターン
  python compare_entry_times_daytrade.py --long-only            # ロング4パターン
  python compare_entry_times_daytrade.py --times 09:00,09:30    # 指定時刻のみ
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

DEFAULT_TIMES = ["10:45", "11:00", "12:30", "13:00"]
# 補足: 9:00-10:30 はほとんどの戦略のウォームアップ期間内 (各戦略100分前後の
# 当日データが必要) のため、その時刻範囲でENTRY_STARTを変えても結果は同じ。
# 意味のある検証範囲は「ウォームアップ完了後 ~ 後場開始」を中心に。


def run_once(entry_start, extra_args):
    """1パターン実行 → HTMLファイルパスとリネーム後の場所を返す。"""
    env = os.environ.copy()
    env["DAYTRADE_ENTRY_START"] = entry_start
    today = datetime.now(JST).strftime("%Y-%m-%d")
    hhmm = entry_start.replace(":", "")

    cmd = [sys.executable, "holdout_periods_report_daytrade.py",
           "--force", "--no-browser"] + extra_args
    print(f"\n{'='*70}")
    print(f"  ENTRY_START = {entry_start}")
    print(f"  CMD: {' '.join(cmd)}")
    print(f"{'='*70}", flush=True)

    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"[error] ENTRY_START={entry_start} 失敗", file=sys.stderr)
        return None

    variant = ""
    if "--short" in extra_args:
        variant = "_short"
    elif "--long-only" in extra_args:
        variant = "_long"

    src = Path(f"holdout_periods{variant}_{today}.html")
    if not src.exists():
        print(f"[warn] 出力ファイル {src} が見つかりません")
        return None

    dst = Path(f"holdout_periods{variant}_{hhmm}_{today}.html")
    src.rename(dst)
    print(f"  → {dst}")
    return dst


def extract_metrics(html_path):
    """HTMLから期間別メトリクス (合格率/合格者TEST損益/全体TEST損益) を抽出。

    各期間タブのサマリBOXに以下の数字が入っている:
      候補銘柄 / TEST合格 ★ / TEST不合格 / 合格率 / 合格者TEST損益 / 全体TEST損益
    """
    if not html_path or not html_path.exists():
        return None
    html = html_path.read_text(encoding="utf-8")

    metrics = {}  # period -> dict
    # 期間タブのペインを切り出して順に metrics を取る
    panes = re.findall(
        r'<div id="t-p(\d+)"[^>]*>.*?</div>\s*(?=<div id="t-p\d+"|<div id="t-detail")',
        html, re.DOTALL)
    if not panes:
        # fallback: 1日タブの場合の単純抽出
        panes = re.findall(r'<div id="t-p(\d+)"[^>]*>(.*?)</div>(?=\s*<div)',
                           html, re.DOTALL)

    # 1パスで全パターン: 期間ごとにBOX値を抽出
    # 「候補銘柄」「TEST合格 ★」「合格率」「合格者TEST損益」「全体TEST損益」の値
    # スパンが入り混じるので各タブの中の最初のBOXだけ取る
    panes_full = re.split(r'<div id="t-p(\d+)" class="tab-pane',
                          html)
    # panes_full[0] is preamble, then alternate (period_id, content)
    for k in range(1, len(panes_full), 2):
        period = int(panes_full[k])
        content = panes_full[k+1] if k+1 < len(panes_full) else ""
        # boxの最初を読む
        m = re.search(
            r'候補銘柄.*?<div class="vl">(\d+)</div>'
            r'.*?TEST合格.*?<div class="vl profit">(\d+)</div>'
            r'.*?TEST不合格.*?<div class="vl loss">(\d+)</div>'
            r'.*?合格率.*?<div class="vl">(\d+)%</div>'
            r'.*?合格者TEST損益.*?<div class="vl profit">([+\-,\d]+)円</div>'
            r'.*?全体TEST損益.*?<div class="vl[^"]*">([+\-,\d]+)円</div>',
            content, re.DOTALL)
        if not m:
            continue
        metrics[period] = {
            "candidates": int(m.group(1)),
            "star": int(m.group(2)),
            "fail": int(m.group(3)),
            "pass_rate": int(m.group(4)),
            "winner_pnl": int(m.group(5).replace(",", "")),
            "total_pnl": int(m.group(6).replace(",", "")),
        }
    return metrics


def build_summary_html(times, results, variant_label, today):
    """統合比較HTMLを生成。"""
    # ベスト判定 (各期間で合格者TEST損益が最大の time)
    PERIODS = [30, 60, 90, 120, 150, 180]
    best_per_period = {}
    for P in PERIODS:
        best_t = None
        best_v = None
        for t in times:
            m = results.get(t)
            if not m or P not in m:
                continue
            v = m[P]["winner_pnl"]
            if best_v is None or v > best_v:
                best_v = v
                best_t = t
        best_per_period[P] = best_t

    # 総合スコア: 全期間の winner_pnl 合計
    total_score = {t: sum(results.get(t, {}).get(P, {}).get("winner_pnl", 0)
                          for P in PERIODS)
                   for t in times}
    best_overall = max(total_score.items(), key=lambda x: x[1])[0] \
        if any(total_score.values()) else None

    # 比較表
    rows = ""
    for P in PERIODS:
        row = f'<tr><td><strong>直近{P}日</strong></td>'
        for t in times:
            m = results.get(t, {}).get(P)
            if not m:
                row += '<td class="na">—</td>'
                continue
            is_best = (t == best_per_period[P])
            bg = "#0d4d2f" if is_best else "#1e293b"
            mark = "👑" if is_best else ""
            row += (f'<td style="background:{bg}">'
                    f'{mark}<br>'
                    f'<strong>{m["winner_pnl"]:+,}円</strong><br>'
                    f'<small>{m["pass_rate"]}% 合格 ({m["star"]}/{m["candidates"]})</small>'
                    f'</td>')
        row += "</tr>"
        rows += row

    # 全体集計行
    total_row = '<tr style="border-top:3px solid #10b981"><td><strong>全6期間合計</strong></td>'
    for t in times:
        is_best = (t == best_overall)
        bg = "#0d4d2f" if is_best else "#1e293b"
        mark = "👑" if is_best else ""
        v = total_score.get(t, 0)
        total_row += (f'<td style="background:{bg}">{mark}<br>'
                      f'<strong style="font-size:1.1rem">{v:+,}円</strong></td>')
    total_row += '</tr>'

    # ヘッダー
    def _fname(t):
        hhmm = t.replace(":", "")
        var = f"_{variant_label}" if variant_label else ""
        return f"holdout_periods{var}_{hhmm}_{today}.html"
    th_cells = []
    for t in times:
        th_cells.append(
            f'<th>{t}<br><small style="color:#94a3b8">{_fname(t)}</small></th>')
    th = "<th>期間</th>" + "".join(th_cells)

    # 結論
    if best_overall:
        winner_pnl_total = total_score[best_overall]
        conclusion = (f'<div class="conclusion">'
                      f'🏆 <strong>最適 ENTRY_START: {best_overall}</strong> '
                      f'(全6期間 合格者TEST損益合計 <strong>{winner_pnl_total:+,}円</strong>)'
                      f'</div>')
    else:
        conclusion = '<div class="conclusion">⚠️ 全パターンでメトリクス取得失敗</div>'

    css = """
body { font-family:"Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a; color:#e2e8f0; padding:20px; margin:0; }
h1 { color:#10b981; margin:0 0 4px; font-size:1.5rem; }
.subtitle { color:#94a3b8; margin-bottom:20px; font-size:0.85rem; }
table { width:100%; border-collapse:collapse; margin:14px 0; font-size:0.85rem; }
th { background:#1e293b; color:#94a3b8; padding:10px;
     border:1px solid #334155; text-align:center; }
td { padding:12px 10px; border:1px solid #1e293b;
     text-align:center; line-height:1.4; }
td.na { color:#475569; }
.conclusion { background:#0d4d2f; color:#4ade80; padding:14px;
              border-radius:8px; margin:20px 0; font-size:1.1rem; }
.legend { background:#1e293b; padding:14px; border-radius:6px;
          margin:10px 0; color:#94a3b8; font-size:0.85rem; line-height:1.6; }
.legend strong { color:#e2e8f0; }
a { color:#60a5fa; text-decoration:none; }
a:hover { text-decoration:underline; }
"""
    var_disp = ({"short": "ショート", "long": "ロング"}
                .get(variant_label, "ロング+ショート"))

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>ENTRY_START 比較 — {today}</title>
<style>{css}</style></head><body>
<h1>📊 ENTRY_START 寄付き開始時刻 比較レポート [{var_disp}]</h1>
<p class="subtitle">生成: {today} / 検証パターン: {len(times)}個</p>

<div class="legend">
  <strong>📋 比較方法</strong><br>
  各 ENTRY_START で <code>holdout_periods_report_daytrade.py</code> を実行し、
  各期間 (直近30/60/90/120/150/180日) の <strong>合格者TEST損益</strong> を比較。<br>
  <strong>👑</strong> = その期間 (横方向) で最大損益を達成したパターン<br>
  <strong>全6期間合計</strong> = 各パターンの総合的な強さ指標
</div>

{conclusion}

<table>
  <thead><tr>{th}</tr></thead>
  <tbody>{rows}{total_row}</tbody>
</table>

<div class="legend">
  <strong>💡 詳細を見たい場合</strong><br>
  各パターン別の詳細HTMLは下記:
  <ul>
""" + "".join(
        f'<li>{t}: <a href="{_fname(t)}">{_fname(t)}</a></li>'
        for t in times) + """
  </ul>
</div>

<div class="legend">
  <strong>📖 解釈ガイド</strong><br>
  <ul>
    <li><strong>注意</strong>: 戦略のウォームアップが 100分前後あるため、9:00-10:30 の範囲で
        ENTRY_START を変えても結果は同じになります (実エントリーが10:40以降にしか発生しないため)。
        意味のある検証は <strong>ウォームアップ完了後〜後場開始</strong> の時間帯で。</li>
    <li><strong>10:45 が勝った</strong> = 早い時間帯のシグナルが優位 (寄付き後ボラの恩恵)</li>
    <li><strong>11:00 が勝った</strong> = 前場中盤が安定 (寄付きノイズ収束後)</li>
    <li><strong>12:30 が勝った</strong> = 後場開始の動きを捉える戦略が有効</li>
    <li><strong>13:00 が勝った</strong> = 後場ある程度落ち着いてからのエントリーが有利</li>
  </ul>
</div>

</body></html>"""


def main():
    parser = argparse.ArgumentParser(
        description="ENTRY_START 複数パターン比較")
    parser.add_argument("--times", default=None,
                        help=f"カンマ区切り (例: 09:00,09:15,09:30)。"
                             f"省略時は {','.join(DEFAULT_TIMES)}")
    parser.add_argument("--short", action="store_true",
                        help="ショート戦略のみで全パターン検証")
    parser.add_argument("--long-only", action="store_true",
                        help="ロング戦略のみで全パターン検証")
    parser.add_argument("--max-price", type=int, default=10_000)
    parser.add_argument("--min-price", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true",
                        help="統合比較HTMLを自動でブラウザ起動しない")
    args = parser.parse_args()

    if args.short and args.long_only:
        print("[error] --short と --long-only は同時指定できません")
        return

    times = (args.times.split(",") if args.times else DEFAULT_TIMES)
    times = [t.strip() for t in times if t.strip()]

    extra_args = []
    if args.short:
        extra_args.append("--short")
    if args.long_only:
        extra_args.append("--long-only")
    if args.max_price:
        extra_args.extend(["--max-price", str(args.max_price)])
    if args.min_price > 0:
        extra_args.extend(["--min-price", str(args.min_price)])

    today = datetime.now(JST).strftime("%Y-%m-%d")
    variant_label = "short" if args.short else "long" if args.long_only else ""

    print(f"ENTRY_START 比較検証")
    print(f"  パターン: {times}")
    print(f"  variant: {variant_label or 'all'}")
    print(f"  価格帯: {args.min_price:,}-{args.max_price:,}円")

    # 各パターン実行
    html_paths = {}
    for t in times:
        out_path = run_once(t, extra_args)
        html_paths[t] = out_path

    # メトリクス抽出
    print(f"\n{'='*70}")
    print(f"  メトリクス抽出 + 統合比較HTML生成")
    print(f"{'='*70}", flush=True)
    metrics_per_time = {}
    for t in times:
        p = html_paths.get(t)
        m = extract_metrics(p)
        metrics_per_time[t] = m
        if m:
            total = sum(v["winner_pnl"] for v in m.values())
            print(f"  {t}: 全6期間合計 {total:+,}円 (期間数: {len(m)})")
        else:
            print(f"  {t}: ✗ メトリクス取得失敗")

    # 統合比較HTML生成
    summary_html = build_summary_html(times, metrics_per_time,
                                       variant_label, today)
    suffix = f"_{variant_label}" if variant_label else ""
    out = Path(f"compare_entry_times{suffix}_{today}.html")
    out.write_text(summary_html, encoding="utf-8")
    print(f"\n統合比較HTML: {out.resolve()}")

    if not args.no_browser:
        try:
            from _open_html import open_html
            open_html(out.resolve())
        except Exception:
            import webbrowser
            webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
