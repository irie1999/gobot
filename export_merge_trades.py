"""export_merge_trades.py — lss基準月の連続W本(既定3)マージごとに、全取引(切り捨て無し)の
CSVと、いつもの形式のHTMLレポートを指定フォルダに書き出す。

HTMLの取引明細は直近1500件に切られるが、CSVは done_trades(全決済済み・全BT)を丸ごと
LSS_TRADES_CSV で出力する(nikkei_analysis 側の全取引CSV機能を使う)。1マージ=1CSV+1HTML。
HTMLは run_signals_holdout_all の通常出力(いつもの形式)を out-dir にコピーする(--no-html で抑制)。

やること:
  1. 手元の lss_proposal_YYYY-MM.py を検出(--from/--to で範囲を絞れる)。
  2. 連続W本のスライド窓を作る。例: [2025-09,10,11] → [2025-10,11,12] → ...
  3. 各窓: merge_lss_proposals でマージ → run_signals_holdout_all を lss単独で実行。
     env LSS_TRADES_CSV=<out-dir>/trades_<最古>_<最新>.csv に全取引を書かせる。

使い方(あなたの機械で。5分足データが必要):
  python export_merge_trades.py
  python export_merge_trades.py --from 2025-09 --window 3
  python export_merge_trades.py --out-dir "C:\\Users\\to732\\OneDrive\\ドキュメント\\tmp\\基準月まとめ"
  python export_merge_trades.py --only 2026-04,2026-05,2026-06
  python export_merge_trades.py --no-delay          # delay1を使わない(既定はdelay1=本番同等)

CSVの列:
  entry_date, exit_date, symbol, name, strategy, bt, wf, reason, order_limit,
  entry_p, exit_p, stop_price, target_price, qty, hold_days, liquidity, mode, pnl
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

_DEFAULT_OUT = r"C:\Users\to732\OneDrive\ドキュメント\tmp\基準月まとめ"

ap = argparse.ArgumentParser(description="lss基準月マージごとに全取引CSVを出力")
ap.add_argument("--window", type=int, default=3, help="1マージに含める連続基準月の本数(既定3)")
ap.add_argument("--cumulative", action="store_true",
                help="累積マージ: --from から各基準月Bまでの全基準月をマージ(9月〜B)。"
                     "各ファイルの初OOS月=Bの翌月。高BT投資で在庫が枯れないよう母集団を最大化する検証用。")
ap.add_argument("--cumulative-min", type=int, default=3,
                help="累積マージの最小本数(既定3。これ未満の短い累積は作らない)")
ap.add_argument("--from", dest="frm", type=str, default="2025-09", help="この基準月以降だけ対象(YYYY-MM)")
ap.add_argument("--to", dest="to", type=str, default="", help="この基準月まで対象(YYYY-MM)")
ap.add_argument("--only", type=str, default="", help="この基準月だけを1マージに(カンマ区切り)")
ap.add_argument("--out-dir", type=str, default=_DEFAULT_OUT, help="CSV出力フォルダ")
ap.add_argument("--days", type=int, default=0, help="表示日数。0=各窓の最古基準月から自動 / >0=固定")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--no-delay", action="store_true", help="delay1を使わない(LSS_STOP_DELAY_BARS=0)")
ap.add_argument("--fast", action="store_true",
                help="詳細分析タブを省いて高速化(--no-analysis)。既定はフル=いつもの形式のHTML")
ap.add_argument("--no-html", action="store_true", help="HTMLを出さずCSVだけ(既定はHTMLもout-dirにコピー)")
args = ap.parse_args()

TODAY = date.today()
_PY = sys.executable or "python"


def _find_bases() -> list[str]:
    out = []
    for p in Path(".").glob("lss_proposal_*.py"):
        m = re.fullmatch(r"lss_proposal_(\d{4}-\d{2})\.py", p.name)
        if m:
            out.append(m.group(1))
    return sorted(set(out))


def _windows(bases: list[str], w: int) -> list[list[str]]:
    return [bases[i:i + w] for i in range(0, len(bases) - w + 1)] if len(bases) >= w else []


def _days_from(base_ym: str) -> int:
    y, m = map(int, base_ym.split("-"))
    return (TODAY - date(y, m, 1)).days + 10


def _label(win: list[str]) -> str:
    return f"{win[0]}_{win[-1]}"


def _cumul_windows(bases: list[str], min_n: int) -> list[list[str]]:
    """累積窓: bases[0..i] を i=min_n-1 から末尾まで。各窓は先頭からの累積マージ。"""
    return [bases[:i + 1] for i in range(min_n - 1, len(bases))]


def _run_one(win: list[str], out_dir: Path, name_prefix: str = "trades_") -> Path | None:
    lab = _label(win)
    merged = Path(f"lss_proposal_mergetrades_{lab}.py")
    csv_out = out_dir / f"{name_prefix}{lab}.csv"
    files = [f"lss_proposal_{ym}.py" for ym in win]
    for f in files:
        if not Path(f).exists():
            print(f"  [skip] {f} が無い → 窓 {lab} をスキップ")
            return None
    print(f"\n=== 窓 {lab} : {' + '.join(win)} ===")
    print(f"  [merge] {' '.join(files)} → {merged.name}")
    r = subprocess.run([_PY, "merge_lss_proposals.py", *files, "--out", str(merged)])
    if r.returncode != 0:
        print(f"  [!] merge 失敗 → スキップ")
        return None
    days = args.days if args.days > 0 else _days_from(win[0])
    env = dict(os.environ)
    env["LSS_TRADES_CSV"] = str(csv_out)
    env["LSS_STOP_DELAY_BARS"] = "0" if args.no_delay else "1"
    suffix = f"_{name_prefix.rstrip('_')}_{lab}"   # 例 _trades_cumul_2025-09_2026-04
    cmd = [_PY, "run_signals_holdout_all.py",
           "--long-stop-short", "--lss-proposal", str(merged),
           "--min-price", str(args.min_price), "--max-price", str(args.max_price),
           "--days", str(days), "--no-browser", "--no-serve", "--force",
           "--no-news", "--no-risk", "--workers", str(args.workers),
           "--output-suffix", suffix]
    if args.fast:
        cmd.append("--no-analysis")   # 詳細分析タブを省いて高速化(既定はフル=いつもの形式)
    print(f"  [run] --days {days} (最古基準月 {win[0]} から) / delay{'0' if args.no_delay else '1'}"
          + (" / fast" if args.fast else " / フル分析"))
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        print(f"  [!] レポート実行 失敗(code {r.returncode})")
    if csv_out.exists():
        print(f"  [csv] {csv_out}")
    else:
        print(f"  [!] CSVが生成されませんでした: {csv_out}")
    # 生成HTML(いつもの形式)を out_dir にコピーして CSV と同じ場所にまとめる。
    if not args.no_html:
        import shutil
        _all = sorted(Path(".").glob("signals_holdout_all*.html"),
                      key=lambda p: p.stat().st_mtime)
        _matched = [p for p in _all if suffix in p.name]
        # suffix一致が最優先。無ければ「この実行後に更新された最新HTML」を拾う(名前ズレ保険)。
        _pick = _matched if _matched else ([_all[-1]] if _all else [])
        if not _pick:
            print(f"  [!] HTMLが見つかりません(CWD に signals_holdout_all*.html 無し)。"
                  f"レポート生成が失敗した可能性 → 上のログを確認してください。")
        for _hp in _pick:
            tag = "" if suffix in _hp.name else "  ⚠suffix不一致(最新HTMLを採用)"
            try:
                _dst = out_dir / _hp.name
                shutil.copy2(_hp, _dst)
                print(f"  [html] {_dst}{tag}")
            except Exception as _he:
                print(f"  [!] HTMLコピー失敗({_he}) → 元ファイルは {_hp.resolve()}")
    return csv_out if csv_out.exists() else None


def main():
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[出力先] {out_dir}")
    name_prefix = "trades_"
    if args.only:
        wins = [[x.strip() for x in args.only.split(",") if x.strip()]]
    else:
        bases = _find_bases()
        if not bases:
            sys.exit("[error] lss_proposal_YYYY-MM.py が見つかりません")
        if args.frm:
            bases = [b for b in bases if b >= args.frm]
        if args.to:
            bases = [b for b in bases if b <= args.to]
        if not bases:
            sys.exit(f"[error] --from {args.frm} --to {args.to} で対象基準月がゼロ")
        print(f"[検出] 基準月: {', '.join(bases)}")
        if args.cumulative:
            name_prefix = "trades_cumul_"
            wins = _cumul_windows(bases, args.cumulative_min)
            if not wins:
                sys.exit(f"[error] 基準月が {args.cumulative_min} 本未満")
            print(f"[累積モード] {args.frm} から各基準月まで累積マージ")
        else:
            wins = _windows(bases, args.window)
            if not wins:
                sys.exit(f"[error] 基準月が {args.window} 本未満")
    print(f"[窓] {len(wins)}件: " + " / ".join(_label(w) for w in wins))
    made = []
    for win in wins:
        p = _run_one(win, out_dir, name_prefix)
        if p:
            made.append(p)
    print(f"\n[完了] {len(made)}/{len(wins)} 窓のCSVを {out_dir} に出力")
    for p in made:
        print(f"  {p.name}")


if __name__ == "__main__":
    main()
