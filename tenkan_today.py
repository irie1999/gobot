"""tenkan_today.py — 指定日の「lss未約定 → ロング転換」を5分足で計算する。

転換タブ(レポート)は oos_raw_fold*.csv 由来なので当月は含まれない。
本日/直近日の転換がどうだったかを、その場で5分足から計算して表示する。

売買ルール(レポートの転換と同一):
  買い: 09:09 以降の最初バーの OPEN
  売り: 11:30 以前の最後バーの OPEN (前場引け相当)
  スリッページ 0.05% / 手数料 片道 0.1% / 100株固定

対象銘柄の指定方法(いずれか):
  1) --orders-csv orders_20260805.csv   ← .\\fills --save が出すCSVから未約定の売り注文を自動抽出
  2) --symbols 3036,3132,6237           ← 直接指定
  3) --signals-csv placed_orders_2026-08-05.csv  ← 発注記録CSVから

使い方(swingtrade フォルダで):
  python tenkan_today.py --orders-csv orders_20260805.csv
  python tenkan_today.py --date 2026-08-05 --symbols 3036,3132,6237,6247,6277,7745,8032,8061,8137,8362,8368
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

ap = argparse.ArgumentParser(description="指定日の lss未約定 → ロング転換を5分足で計算")
ap.add_argument("--date", type=str, default=None, help="対象日 YYYY-MM-DD (既定=今日JST)")
ap.add_argument("--symbols", type=str, default="", help="カンマ区切りの銘柄コード (例: 3036,3132)")
ap.add_argument("--orders-csv", type=str, default="",
                help=".\\fills --save が出す orders_<yyyyMMdd>.csv。未約定の売り注文を自動抽出")
ap.add_argument("--signals-csv", type=str, default="",
                help="placed_orders_<日付>.csv 等。symbol 列から抽出")
ap.add_argument("--all", action="store_true",
                help="--orders-csv 使用時、約定済みも含めて全売り注文を対象にする(参考値)")
args = ap.parse_args()

TARGET = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
          else datetime.now(JST).date())


# ── 共通シミュレーション実装を使う (レポートの転換タブと完全に同一ロジック) ──
import tenkan_sim as _tks

BUY_TIME, SELL_TIME = _tks.BUY_TIME, _tks.SELL_TIME
SLIP, FEE, QTY = _tks.SLIP, _tks.FEE, _tks.QTY
DIR5, DIR1 = _tks.find_minute_dirs()


def simulate(code: str, d):
    """(結果dict, エラー文字列) を返す。"""
    res = _tks.simulate(code, d)
    if res is None:
        if not _tks.has_any_bars(code):
            return None, "分足ファイルなし"
        return None, f"{d} のバーが無い(未取得/休場)"
    return {"buy": res["buy_p"], "sell": res["sell_p"], "pnl": res["pnl"],
            "bt": res["buy_t"], "st": res["sell_t"]}, ""


# ── 対象銘柄の決定 ──────────────────────────────────────────────────────────
syms: list[tuple[str, str]] = []   # (code, name)

if args.orders_csv:
    p = Path(args.orders_csv)
    if not p.exists():
        print(f"[ERROR] {p} が見つかりません")
        sys.exit(1)
    with open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("side") != "売":
                continue
            filled = int(float(r.get("cum_qty") or 0))
            if not args.all and filled > 0:
                continue     # 約定した=lssが成立 → 転換の対象外
            code = str(r.get("code", "")).strip()
            if code and code not in [c for c, _ in syms]:
                syms.append((code, str(r.get("name", ""))))
    print(f"[抽出] {p.name} から "
          f"{'全売り注文' if args.all else '未約定の売り注文'} {len(syms)}銘柄")

if args.signals_csv:
    p = Path(args.signals_csv)
    if p.exists():
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                code = str(r.get("symbol") or r.get("code") or "").split(".")[0].strip()
                if code and code not in [c for c, _ in syms]:
                    syms.append((code, str(r.get("name", ""))))
        print(f"[抽出] {p.name} から {len(syms)}銘柄")

if args.symbols:
    for s in args.symbols.split(","):
        code = s.strip().split(".")[0]
        if code and code not in [c for c, _ in syms]:
            syms.append((code, ""))

if not syms:
    print("[ERROR] 対象銘柄がありません。--orders-csv / --symbols / --signals-csv のいずれかを指定してください。")
    sys.exit(1)

# ── 実行 ────────────────────────────────────────────────────────────────────
print("=" * 76)
print(f"転換シミュレーション  対象日 {TARGET}  ({len(syms)}銘柄)")
print(f"買い: {BUY_TIME.strftime('%H:%M')}以降の最初バーOPEN / "
      f"売り: {SELL_TIME.strftime('%H:%M')}以前の最後バーOPEN / {QTY}株固定")
print(f"5分足DIR: {DIR5}")
print("=" * 76)
print(f"{'コード':>6} {'銘柄':<14}{'買値':>9}{'売値':>9}{'買':>6}{'売':>6}{'損益':>10}  備考")
print("-" * 76)

rows = []
skipped = []
for code, name in sorted(syms):
    res, err = simulate(code, TARGET)
    if res is None:
        skipped.append((code, name, err))
        print(f"{code:>6} {name[:14]:<14}{'—':>9}{'—':>9}{'—':>6}{'—':>6}{'—':>10}  {err}")
        continue
    rows.append((code, name, res))
    print(f"{code:>6} {name[:14]:<14}{res['buy']:>9,.1f}{res['sell']:>9,.1f}"
          f"{res['bt']:>6}{res['st']:>6}{res['pnl']:>+10,.0f}")

print("-" * 76)
if rows:
    n = len(rows)
    w = sum(1 for _, _, r in rows if r["pnl"] > 0)
    tot = sum(r["pnl"] for _, _, r in rows)
    gp = sum(r["pnl"] for _, _, r in rows if r["pnl"] > 0)
    gl = abs(sum(r["pnl"] for _, _, r in rows if r["pnl"] < 0))
    pf = gp / gl if gl > 0 else float("inf")
    print(f"{'合計':>6} {n}銘柄  勝率 {w / n * 100:.1f}% ({w}W/{n - w}L)  "
          f"PF {'∞' if pf == float('inf') else f'{pf:.2f}'}  "
          f"損益 {tot:+,.0f}円  (利益 +{gp:,.0f} / 損失 -{gl:,.0f})")
    print(f"       1銘柄あたり平均 {tot / n:+,.0f}円  /  投入資金 {sum(r['buy'] for _, _, r in rows) * QTY:,.0f}円")
else:
    print("  計算できた銘柄がありません。")

if skipped:
    print(f"\nスキップ {len(skipped)}銘柄: " + ", ".join(c for c, _, _ in skipped))
    print("  ※ 当日の5分足がまだ取得できていない可能性があります。")
    print("     python daily_fetch_minute.py などでデータを更新してから再実行してください。")
