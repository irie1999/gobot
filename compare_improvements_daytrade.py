"""
compare_improvements_daytrade.py  ―  改善案A/B/C/D 比較
==================================================================
holdout_periods_report_daytrade.py を以下の改善案で連続実行し、
ベースラインとの差分を比較する。

【検証する改善】
  A. Slow Stop (STOP_CONFIRM_BARS=1)
     stopタッチ後 1バー (5分) 待ち、それでも回復しなければ決済
  B. 同日損切ロック (MAX_DAILY_STOPS=2)
     1日2回 stop引かれたら以降のシグナル無視
  C. ブレークイーブントレール (TRAIL_TO_BREAKEVEN=1)
     +1×ATR 利益乗ったら stop を建値に移動
  D. 戦略別時刻フィルタ
     MOM_S:09:30-10:30, RSI2_S:10:00-13:00, MACD_S:09:30-14:00, A7_S:10:30-14:00

【パターン】
  baseline: なし
  +A
  +B
  +C
  +D
  +ABCD (全部入り)

【出力】
  holdout_periods_<variant>_<改善>_<日付>.html
  compare_improvements_<variant>_<日付>.html (統合)

【使い方】
  python compare_improvements_daytrade.py --short --max-price 6000 --min-price 1000
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

_STRAT_TIMES = (
    "MOM:0930-1030,MOM_S:0930-1030,"
    "RSI2:1000-1300,RSI2_S:1000-1300,"
    "MACD:0930-1400,MACD_S:0930-1400,"
    "A7:1030-1400,A7_S:1030-1400"
)

# 改善設定 (label, env_dict)
IMPROVEMENTS = [
    ("baseline", {}),
    ("A_slow_stop", {"DAYTRADE_STOP_CONFIRM": "1"}),
    ("B_daily_lock", {"DAYTRADE_MAX_STOPS": "2"}),
    ("C_trail_be", {"DAYTRADE_TRAIL_BE": "1"}),
    ("D_strat_times", {"DAYTRADE_STRAT_TIMES": _STRAT_TIMES}),
    ("E_vol_boost", {"DAYTRADE_MIN_VOL_RATIO": "1.5"}),
    ("F_atr_range", {"DAYTRADE_MIN_ATR_PCT": "0.5", "DAYTRADE_MAX_ATR_PCT": "3.0"}),
    ("G_pause_losses", {"DAYTRADE_PAUSE_LOSSES": "3", "DAYTRADE_PAUSE_DAYS": "5"}),
    ("ABCDEFG_all", {
        "DAYTRADE_STOP_CONFIRM": "1",
        "DAYTRADE_MAX_STOPS": "2",
        "DAYTRADE_TRAIL_BE": "1",
        "DAYTRADE_STRAT_TIMES": _STRAT_TIMES,
        "DAYTRADE_MIN_VOL_RATIO": "1.5",
        "DAYTRADE_MIN_ATR_PCT": "0.5",
        "DAYTRADE_MAX_ATR_PCT": "3.0",
        "DAYTRADE_PAUSE_LOSSES": "3",
        "DAYTRADE_PAUSE_DAYS": "5",
    }),
]


def run_once(label, env_overrides, extra_args):
    """1パターン実行 → HTMLパス返す。"""
    env = os.environ.copy()
    # 既存の改善ENVをクリアしてから上書き
    for k in ("DAYTRADE_STOP_CONFIRM", "DAYTRADE_MAX_STOPS",
              "DAYTRADE_TRAIL_BE", "DAYTRADE_STRAT_TIMES",
              "DAYTRADE_MIN_VOL_RATIO", "DAYTRADE_MIN_ATR_PCT",
              "DAYTRADE_MAX_ATR_PCT", "DAYTRADE_PAUSE_LOSSES",
              "DAYTRADE_PAUSE_DAYS"):
        env.pop(k, None)
    env.update(env_overrides)
    today = datetime.now(JST).strftime("%Y-%m-%d")

    cmd = [sys.executable, "holdout_periods_report_daytrade.py",
           "--force", "--no-browser"] + extra_args
    print(f"\n{'='*70}")
    print(f"  改善: {label}")
    print(f"  ENV: {env_overrides}")
    print(f"{'='*70}", flush=True)

    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"[error] {label} 失敗", file=sys.stderr)
        return None

    variant = ""
    if "--short" in extra_args:
        variant = "_short"
    elif "--long-only" in extra_args:
        variant = "_long"

    src = Path(f"holdout_periods{variant}_{today}.html")
    if not src.exists():
        return None
    dst = Path(f"holdout_periods{variant}_{label}_{today}.html")
    src.replace(dst)
    print(f"  → {dst}")
    return dst


def extract_metrics(html_path):
    """HTMLから期間別メトリクスを抽出。"""
    if not html_path or not html_path.exists():
        return None
    html = html_path.read_text(encoding="utf-8")
    metrics = {}
    panes_full = re.split(r'<div id="t-p(\d+)" class="tab-pane', html)
    for k in range(1, len(panes_full), 2):
        period = int(panes_full[k])
        content = panes_full[k+1] if k+1 < len(panes_full) else ""
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


def build_summary_html(labels, results, variant_label, today):
    """統合比較HTMLを生成。"""
    PERIODS = [30, 60, 90, 120, 150, 180]

    # ベースラインとの差分
    base = results.get("baseline", {})
    total_scores = {lab: sum(results.get(lab, {}).get(P, {}).get("winner_pnl", 0)
                              for P in PERIODS) for lab in labels}
    base_total = total_scores.get("baseline", 0)

    # ベスト判定
    best_overall = max(total_scores.items(), key=lambda x: x[1])[0] \
        if any(total_scores.values()) else None

    # ヘッダー
    th_cells = ["<th>期間</th>"]
    for lab in labels:
        v = total_scores.get(lab, 0)
        delta = v - base_total
        if lab != "baseline":
            delta_color = "#4ade80" if delta >= 0 else "#f87171"
            delta_str = f"<br><small style='color:{delta_color}'>{delta:+,}円 vs baseline</small>"
        else:
            delta_str = ""
        is_best = (lab == best_overall)
        bg_style = " style='background:#0d4d2f'" if is_best else ""
        crown = "👑 " if is_best else ""
        head = f"<th{bg_style}>{crown}{lab}{delta_str}</th>"
        th_cells.append(head)
    th = "".join(th_cells)

    # 行
    rows_html = ""
    for P in PERIODS:
        row = f'<tr><td><strong>直近{P}日</strong></td>'
        base_pnl = base.get(P, {}).get("winner_pnl", 0)
        for lab in labels:
            m = results.get(lab, {}).get(P)
            if not m:
                row += '<td class="na">—</td>'
                continue
            v = m["winner_pnl"]
            delta = v - base_pnl
            color = "#4ade80" if v >= 0 else "#f87171"
            delta_color = "#4ade80" if delta > 0 else "#f87171" if delta < 0 else "#94a3b8"
            delta_label = ("Δ " + format(delta, "+,")) if lab != "baseline" else "基準"
            row += (f'<td>'
                    f'<strong style="color:{color}">{v:+,}</strong><br>'
                    f'<small>{m["pass_rate"]}% ({m["star"]}/{m["candidates"]})</small><br>'
                    f'<small style="color:{delta_color}">{delta_label}</small>'
                    f'</td>')
        row += "</tr>"
        rows_html += row

    # 合計行
    total_row = '<tr style="border-top:3px solid #10b981"><td><strong>全6期間合計</strong></td>'
    for lab in labels:
        v = total_scores.get(lab, 0)
        delta = v - base_total
        is_best = (lab == best_overall)
        bg = "#0d4d2f" if is_best else "#1e293b"
        mark = "👑" if is_best else ""
        if lab != "baseline":
            dc = "#4ade80" if delta >= 0 else "#f87171"
            delta_str = f"<br><small style='color:{dc}'>({delta:+,})</small>"
        else:
            delta_str = ""
        total_row += (f'<td style="background:{bg}">{mark}<br>'
                      f'<strong style="font-size:1.1rem">{v:+,}円</strong>'
                      f'{delta_str}</td>')
    total_row += '</tr>'

    if best_overall:
        winner_pnl_total = total_scores[best_overall]
        improvement = winner_pnl_total - base_total
        imp_pct = (improvement / abs(base_total) * 100) if base_total else 0
        conclusion = (
            f'<div class="conclusion">'
            f'🏆 <strong>最良改善: {best_overall}</strong>'
            f' (合格者TEST損益合計 <strong>{winner_pnl_total:+,}円</strong>, '
            f'baseline比 <strong>{improvement:+,}円 ({imp_pct:+.1f}%)</strong>)'
            f'</div>')
    else:
        conclusion = '<div class="conclusion">⚠️ メトリクス取得失敗</div>'

    css = """
body { font-family:"Segoe UI","Hiragino Sans",sans-serif;
       background:#0f172a; color:#e2e8f0; padding:20px; margin:0; }
h1 { color:#10b981; margin:0 0 4px; font-size:1.4rem; }
.subtitle { color:#94a3b8; margin-bottom:14px; font-size:0.82rem; }
table { width:100%; border-collapse:collapse; margin:10px 0; font-size:0.8rem; }
th { background:#1e293b; color:#94a3b8; padding:8px 6px;
     border:1px solid #334155; text-align:center; }
td { padding:10px 6px; border:1px solid #1e293b;
     text-align:center; line-height:1.4; }
td.na { color:#475569; }
.conclusion { background:#0d4d2f; color:#4ade80; padding:12px;
              border-radius:8px; margin:14px 0; font-size:1.05rem; }
.legend { background:#1e293b; padding:12px; border-radius:6px;
          margin:8px 0; color:#94a3b8; font-size:0.82rem; line-height:1.6; }
.legend strong { color:#e2e8f0; }
.legend code { background:#0f172a; padding:1px 6px; border-radius:3px; }
"""
    var_disp = ({"short": "ショート", "long": "ロング"}
                .get(variant_label, "ロング+ショート"))

    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<title>改善案比較 — {today}</title>
<style>{css}</style></head><body>
<h1>🔬 改善案 A/B/C/D 比較 [{var_disp}]</h1>
<p class="subtitle">生成: {today} / パターン: {len(labels)}個</p>

<div class="legend">
  <strong>📋 検証する改善</strong><br>
  ▸ <strong>A. Slow Stop</strong>: stopタッチ後1バー (5分) 待って再判定。即死を防ぐ<br>
  ▸ <strong>B. 同日損切ロック</strong>: 1日2回損切したら以降エントリー無効<br>
  ▸ <strong>C. BEトレール</strong>: +1×ATR乗ったら stop を建値に移動<br>
  ▸ <strong>D. 戦略別時刻フィルタ</strong>: MOM_S 9:30-10:30 / RSI2_S 10:00-13:00 etc<br>
  ▸ <strong>E. 出来高ブースト</strong>: 20本平均×1.5倍以上の出来高がある時のみエントリー<br>
  ▸ <strong>F. ATR%レンジ</strong>: ATR が 0.5%〜3.0% のときのみ取引 (低ボラ/過ボラ除外)<br>
  ▸ <strong>G. 連敗一時停止</strong>: 3連敗で5日間その銘柄/戦略を休止<br>
  ▸ <strong>ABCDEFG</strong>: 全部入り
</div>

{conclusion}

<table>
  <thead><tr>{th}</tr></thead>
  <tbody>{rows_html}{total_row}</tbody>
</table>

<div class="legend">
  <strong>📖 解釈</strong><br>
  <ul>
    <li><strong>baseline比 +X円</strong>: 改善で得た追加損益</li>
    <li><strong>合計が大きいほど良い</strong>: 6期間トータルで強い</li>
    <li><strong>合格率の変化</strong>も重要 (取引数減っても勝率上がれば◎)</li>
    <li><strong>ABCD全部入りが必ずしも最良ではない</strong> ─ 個別の効きが分かる</li>
  </ul>
</div>

</body></html>"""


def main():
    parser = argparse.ArgumentParser(description="改善案A/B/C/D比較")
    parser.add_argument("--short", action="store_true")
    parser.add_argument("--long-only", action="store_true")
    parser.add_argument("--max-price", type=int, default=10_000)
    parser.add_argument("--min-price", type=int, default=0)
    parser.add_argument("--patterns", default=None,
                        help="カンマ区切り (例: baseline,A_slow_stop,ABCD_all)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.short and args.long_only:
        print("[error] --short と --long-only 同時指定不可")
        return

    extra_args = []
    if args.short:
        extra_args.append("--short")
    if args.long_only:
        extra_args.append("--long-only")
    extra_args.extend(["--max-price", str(args.max_price),
                        "--min-price", str(args.min_price)])

    today = datetime.now(JST).strftime("%Y-%m-%d")
    variant_label = "short" if args.short else "long" if args.long_only else ""

    # パターン選択
    if args.patterns:
        selected_labels = [s.strip() for s in args.patterns.split(",")]
        patterns = [(lab, env) for lab, env in IMPROVEMENTS if lab in selected_labels]
    else:
        patterns = IMPROVEMENTS

    print(f"改善案比較")
    print(f"  パターン: {[p[0] for p in patterns]}")
    print(f"  variant: {variant_label or 'all'}")

    # 実行
    html_paths = {}
    for label, env in patterns:
        out_path = run_once(label, env, extra_args)
        html_paths[label] = out_path

    # メトリクス抽出
    print(f"\n{'='*70}")
    print(f"  メトリクス抽出 + 統合比較HTML生成")
    print(f"{'='*70}", flush=True)
    metrics_per = {}
    for label, _ in patterns:
        p = html_paths.get(label)
        m = extract_metrics(p)
        metrics_per[label] = m
        if m:
            total = sum(v["winner_pnl"] for v in m.values())
            print(f"  {label}: 合計 {total:+,}円")
        else:
            print(f"  {label}: ✗ 失敗")

    # 統合HTML
    labels = [lab for lab, _ in patterns]
    summary_html = build_summary_html(labels, metrics_per, variant_label, today)
    suffix = f"_{variant_label}" if variant_label else ""
    out = Path(f"compare_improvements{suffix}_{today}.html")
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
