r"""check_orders.py — 発注記録を **寄り前に** 検算する(照会もしない・CSVだけ読む)。

なぜ要るか (2026-08-13)
-----------------------
初日に6件の不具合が出たが、**全部 実弾が動いた後**に見つかった:
  ・発注ボタンが atr を渡しておらず、**4銘柄すべて損切りが無効化**された
  ・発注サーバが指値を黙って1ティック下げていた
  ・--entry-mode limit が逆指値で飛んでいた
どれも `ordered_signals_lss.csv` を1行ずつ見れば **発注直後に気付けた**。
寄りまでに気付けば注文を出し直せる。気付けなければ無防備で1日過ごす。

使い方
------
  python check_orders.py                 # 今日ぶん
  python check_orders.py --date 2026-08-13
  .\chk                                  # 同じ(bat)

kabu を叩かないので **watcher / 発注サーバと同時に実行してよい**(トークンを奪わない)。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

JST = timezone(timedelta(hours=9))
_BASE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser(description="発注記録を寄り前に検算する(CSVのみ・照会なし)")
ap.add_argument("--date", type=str, default=None, help="対象日 YYYY-MM-DD(既定=今日JST)")
ap.add_argument("--csv", type=str, default="ordered_signals_lss.csv")
ap.add_argument("--budget", type=float, default=4_000_000.0, help="予算(円)。超過を警告")
ap.add_argument("--limit-ticks", type=int,
                default=int(os.environ.get("LSS_H_LIMIT_TICKS", "-5") or -5),
                help="H の指値を前日終値から何ティックずらしたか(既定 -5)")
args = ap.parse_args()

_DATE = args.date or datetime.now(JST).strftime("%Y-%m-%d")


def _f(v) -> float:
    try:
        return float(str(v).strip() or 0)
    except ValueError:
        return 0.0


def _tick(p: float) -> float:
    try:
        from backtest_limit_entry import tick_size
        return float(tick_size(float(p)))
    except Exception:
        return 1.0


def main() -> int:
    p = Path(args.csv)
    if not p.exists():
        print(f"[!] {args.csv} がありません。まだ発注していないか、別の場所です。")
        return 1
    rows = []
    with open(p, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("record_date", ""))[:10] != _DATE:
                continue
            if str(r.get("family", "")).strip() != "lss":
                continue
            rows.append(r)
    print(f"■ 発注記録の検算  {_DATE}  ({args.csv})")
    print("=" * 74)
    if not rows:
        print(f"  {_DATE} の記録がありません。")
        print("  発注していないか、発注記録の書き込みに失敗しています")
        print("  (発注は通っているのに記録だけ失敗するケースがあるので、")
        print("   kabu ステーションの注文一覧も必ず目視すること)。")
        return 1

    err, warn = [], []
    total = 0.0
    seen: dict = {}
    print(f"{'コード':<7}{'方式':>8}{'指値':>9}{'損切':>9}{'利確':>9}"
          f"{'ATR':>8}{'株':>5}  判定")
    for r in rows:
        sym = str(r.get("symbol", "")).strip()
        mode = str(r.get("entry_mode", "") or "stop").strip()
        op, sp, tp = _f(r.get("order_price")), _f(r.get("stop_price")), _f(r.get("target_price"))
        atr, sm, tm = _f(r.get("atr")), _f(r.get("sm")), _f(r.get("tm"))
        qty = int(_f(r.get("qty")) or 0)
        bad = []

        # ① 注文方式。H 以外は出さない運用(2026-08-13)
        if mode not in ("limit", "auction"):
            bad.append(f"方式が {mode} = **逆指値**。H は limit のはず")

        # ② ATR/sm/tm。欠けると watcher が実約定価格から OCO を組み直せない
        #    → 注文価格基準の損切りが約定値より下に来て**無効化**される(初日の事故)
        if atr <= 0:
            bad.append("**ATR が 0** → watcher が OCO を組み直せない(損切り無効化の原因)")
        if sm <= 0 or tm <= 0:
            bad.append(f"sm/tm が不正 ({sm}/{tm})")

        # ③ ショートの向き
        if not (sp > op > tp > 0):
            bad.append(f"向きが不正: 損切{sp:,.0f} > 指値{op:,.0f} > 利確{tp:,.0f} でない")

        # ④ 幅が ATR×sm / ATR×tm と整合するか(前日終値基準・±1ティック許容)
        if atr > 0 and sm > 0 and tm > 0 and op > 0:
            cl = op - args.limit_ticks * _tick(op)      # 指値から前日終値を復元
            t = _tick(op)
            if abs((sp - cl) - atr * sm) > t + 0.51:
                bad.append(f"損切幅ズレ: 実{sp - cl:,.1f} vs ATR×sm {atr * sm:,.1f}")
            if abs((cl - tp) - atr * tm) > t + 0.51:
                bad.append(f"利確幅ズレ: 実{cl - tp:,.1f} vs ATR×tm {atr * tm:,.1f}")

        if qty <= 0:
            bad.append("株数が0")
        if sym in seen:
            warn.append(f"{sym}: 同じ銘柄が{seen[sym] + 1}件(枠を重複して使う)")
        seen[sym] = seen.get(sym, 0) + 1
        total += op * qty

        mark = "OK" if not bad else "NG"
        print(f"{sym:<7}{mode:>8}{op:>9,.0f}{sp:>9,.0f}{tp:>9,.0f}"
              f"{atr:>8,.1f}{qty:>5}  {mark}")
        for b in bad:
            print(f"        └ {b}")
            err.append(f"{sym}: {b}")

    print("-" * 74)
    print(f"  {len(rows)}件 / 想定額 {total:,.0f}円 (予算 {args.budget:,.0f}円)")
    if total > args.budget:
        warn.append(f"想定額が予算を {total - args.budget:,.0f}円 超過")

    for w in warn:
        print(f"  [warn] {w}")
    print()
    if err:
        print(f"⛔ **{len(err)}件の異常**。寄りまでに注文を出し直してください。")
        print("   ATR が 0 のときは、レポートの『100株 発注』ではなく")
        print("   python lss_budget_cap.py --execute --prod --entry-mode limit "
              "--limit-ticks -5 --budget-multiple 1.0 で出し直すのが確実です。")
        return 1
    print("✅ 異常なし。watcher は実約定価格から OCO を組み直せます。")
    print("   次: .\\watch を寄り前に起動し、15:30 まで止めないこと。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
