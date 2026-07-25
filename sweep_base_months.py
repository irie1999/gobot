"""sweep_base_months.py — lss基準月の3本組を1つずつスライドし、各マージの
『400万×BT 月別成績』を長期ウィンドウで比較する。

やること:
  1. 手元の lss_proposal_YYYY-MM.py を検出し、連続する W本(既定3)のスライド窓を作る。
     例: [2025-03,06,09] → [2025-06,09,12] → [2025-09,12,2026-03] → [2025-12,2026-03,06] ...
  2. 各窓について:
       a. merge_lss_proposals.py でマージ → lss_proposal_sweep_<oldest>_<newest>.py
       b. run_signals_holdout_all.py を lss単独(--long-stop-short --lss-proposal)で実行。
          表示ウィンドウは「その窓の最も古い基準月〜今日」(--days 自動)。delay1(LSS_STOP_DELAY_BARS=1)。
          環境変数 LSS_BUDGET_MONTHLY_CSV に月別P&Lを書かせる(nikkei_analysis 側で出力)。
  3. 各窓の月別CSVを集約 → 月×窓 の比較表をコンソール表示 + sweep_base_comparison_<date>.csv。
  4. 各窓の本体HTMLを --output-suffix _sweep_<label> で保存(上書き回避)。
     signals_holdout_all*_sweep_<label>.html を開き、『400万円×BT降順×日別』タブで
     月別＋取引詳細(フル装飾)を確認できる。CSVは横比較・HTMLは1窓の詳細確認、と使い分ける。

使い方(あなたの機械で。5分足データが必要):
  python sweep_base_months.py                       # 全連続3本組を自動でスイープ
  python sweep_base_months.py --window 3            # 窓サイズ(既定3)
  python sweep_base_months.py --days 0              # --days: 0=各窓の最古基準月から自動 / >0=全窓共通の固定
  python sweep_base_months.py --min-price 1000 --max-price 6000
  python sweep_base_months.py --skip-run            # レポート実行を飛ばし既存CSVだけ集約
  python sweep_base_months.py --only 2025-03,2025-06,2025-09  # この組だけ

注意:
  ・BTキャッシュは銘柄単位で共有されるので、2窓目以降は速い(初回のみ重い)。
  ・古い基準月を含む窓は、その窓の一部が in-sample 寄りになる(選定に使った期間を含む)ので、
    OOSとしての信頼度は新しい窓ほど高い。比較の目安として使う。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss基準月スライドの400万×BT月別比較")
ap.add_argument("--window", type=int, default=3, help="1マージに含める連続基準月の本数(既定3)")
ap.add_argument("--days", type=int, default=0, help="表示日数。0=各窓の最古基準月から自動 / >0=全窓固定")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--skip-run", action="store_true", help="レポート実行を飛ばし既存CSVのみ集約")
ap.add_argument("--only", type=str, default="", help="この基準月だけを1マージに(カンマ区切り 例 2025-03,2025-06,2025-09)")
ap.add_argument("--no-delay", action="store_true", help="delay1を使わない(LSS_STOP_DELAY_BARS=0)")
ap.add_argument("--no-open", action="store_true", help="生成HTMLをブラウザで自動オープンしない")
args = ap.parse_args()

import webbrowser

TODAY = date.today()
_PY = sys.executable or "python"


def _find_bases() -> list[str]:
    """lss_proposal_YYYY-MM.py を検出して YYYY-MM 昇順で返す。"""
    out = []
    for p in Path(".").glob("lss_proposal_*.py"):
        m = re.fullmatch(r"lss_proposal_(\d{4}-\d{2})\.py", p.name)
        if m:
            out.append(m.group(1))
    return sorted(set(out))


def _windows(bases: list[str], w: int) -> list[list[str]]:
    """連続する w 本のスライド窓。"""
    return [bases[i:i + w] for i in range(0, len(bases) - w + 1)] if len(bases) >= w else []


def _days_from(base_ym: str) -> int:
    """基準月の月初〜今日の日数(+バッファ)。"""
    y, m = map(int, base_ym.split("-"))
    return (TODAY - date(y, m, 1)).days + 10


def _label(win: list[str]) -> str:
    return f"{win[0]}_{win[-1]}"


def _run_one(win: list[str]) -> Path | None:
    """1窓: マージ→lssレポート実行。月別CSVのパスを返す。"""
    lab = _label(win)
    merged = Path(f"lss_proposal_sweep_{lab}.py")
    csv_out = Path(f"sweep_monthly_{lab}.csv")
    files = [f"lss_proposal_{ym}.py" for ym in win]
    for f in files:
        if not Path(f).exists():
            print(f"  [skip] {f} が無い → 窓 {lab} をスキップ")
            return None
    if args.skip_run:
        return csv_out if csv_out.exists() else None
    # 1) マージ
    print(f"\n=== 窓 {lab} : {' + '.join(win)} ===")
    print(f"  [merge] {' '.join(files)} → {merged.name}")
    r = subprocess.run([_PY, "merge_lss_proposals.py", *files, "--out", str(merged)])
    if r.returncode != 0:
        print(f"  [!] merge 失敗 → スキップ")
        return None
    # 2) lssレポート実行(単独・delay1・月別CSV出力)
    days = args.days if args.days > 0 else _days_from(win[0])
    env = dict(os.environ)
    env["LSS_BUDGET_MONTHLY_CSV"] = str(csv_out)
    env["LSS_STOP_DELAY_BARS"] = "0" if args.no_delay else "1"
    cmd = [_PY, "run_signals_holdout_all.py",
           "--long-stop-short", "--lss-proposal", str(merged),
           "--min-price", str(args.min_price), "--max-price", str(args.max_price),
           "--days", str(days), "--no-browser", "--no-serve", "--force",
           "--no-news", "--no-risk", "--no-analysis", "--workers", str(args.workers),
           "--output-suffix", f"_sweep_{lab}"]   # --no-serve=発注サーバを起動しない(重要)
    print(f"  [run] --days {days} (最古基準月 {win[0]} から) / delay{'0' if args.no_delay else '1'}")
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        print(f"  [!] レポート実行 失敗(code {r.returncode})")
        return csv_out if csv_out.exists() else None
    # 生成された本体HTML(400万×BTタブ入り)のパスを表示
    _htmls = sorted(Path(".").glob(f"signals_holdout_all*_sweep_{lab}.html"))
    if _htmls:
        _hp = _htmls[-1]
        print(f"  [html] {_hp.name}  ← 開いて『万円×BT降順×日別』タブを見る")
        if not args.no_open:
            try:
                webbrowser.open(_hp.resolve().as_uri())   # いつものレポートHTMLを自動で開く
            except Exception as _oe:
                print(f"  [!] 自動オープン失敗({_oe}) → 手動で開いてください: {_hp}")
    return csv_out if csv_out.exists() else None


def _aggregate(results: dict[str, Path]):
    """窓ラベル→月別CSV を 月×窓 の比較表に。"""
    frames = {}
    for lab, path in results.items():
        if path and path.exists():
            df = pd.read_csv(path)
            frames[lab] = df.set_index("month")
    if not frames:
        print("\n集約できるCSVがありません。")
        return
    all_months = sorted({m for df in frames.values() for m in df.index})
    labels = list(frames.keys())

    print("\n" + "=" * (14 + 15 * len(labels)))
    print("【400万×BT 月別ネット損益】(基準月マージ別) 単位:円")
    print("=" * (14 + 15 * len(labels)))
    print(f"{'月':<10}" + "".join(f"{lab:>15}" for lab in labels))
    for m in all_months:
        row = f"{m:<10}"
        for lab in labels:
            v = frames[lab]["pnl"].get(m)
            row += f"{'':>15}" if pd.isna(v) else f"{v:>+15,.0f}"
        print(row)
    print("-" * (14 + 15 * len(labels)))
    # 合計・勝率(各窓の全月合計)
    print(f"{'合計pnl':<10}" + "".join(f"{frames[lab]['pnl'].sum():>+15,.0f}" for lab in labels))
    def _wr(df):
        tw = (df["win_rate"] / 100 * df["trades"]).sum()
        tn = df["trades"].sum()
        return tw / tn * 100 if tn else 0
    print(f"{'件数':<10}" + "".join(f"{int(frames[lab]['trades'].sum()):>15,}" for lab in labels))
    print(f"{'勝率%':<10}" + "".join(f"{_wr(frames[lab]):>15.1f}" for lab in labels))

    # CSV
    comp = pd.DataFrame({lab: frames[lab]["pnl"] for lab in labels}).reindex(all_months)
    out = Path(f"sweep_base_comparison_{TODAY}.csv")
    comp.to_csv(out, encoding="utf-8-sig")
    print(f"\n[出力] {out}")
    print("※ 新しい基準月の窓ほど OOS 信頼度が高い。古い窓は一部 in-sample を含む点に注意。")
    print("※ 各窓は『最古基準月〜今日』を表示。共通の月だけ横比較すると公平。")


def main():
    if args.only:
        wins = [[x.strip() for x in args.only.split(",") if x.strip()]]
    else:
        bases = _find_bases()
        if not bases:
            sys.exit("[error] lss_proposal_YYYY-MM.py が見つかりません")
        print(f"[検出] 基準月: {', '.join(bases)}")
        wins = _windows(bases, args.window)
        if not wins:
            sys.exit(f"[error] 基準月が {args.window} 本未満")
    print(f"[スイープ] {len(wins)} 窓: " + " / ".join(_label(w) for w in wins))
    results: dict[str, Path] = {}
    for win in wins:
        results[_label(win)] = _run_one(win)
    _aggregate(results)


if __name__ == "__main__":
    main()
