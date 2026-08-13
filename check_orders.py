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

def _parse_args():
    """⛔ **トップレベルで parse_args しないこと。**

    このモジュールは order_server(発注ボタン) と lss_budget_cap から
    `from check_orders import verify_row` で import される。トップレベルに
    argparse を置くと、import した瞬間に呼び出し側の sys.argv を parse しようと
    して SystemExit(2) で **相手のプロセスごと落ちる**。
    2026-08-13 に実際に発生: 発注は成功した直後に発注サーバが
    `unrecognized arguments: --execute --prod` で即死した。
    """
    ap = argparse.ArgumentParser(
        description="発注記録を寄り前に検算する(CSVのみ・照会なし)")
    ap.add_argument("--date", type=str, default=None,
                    help="対象日 YYYY-MM-DD(既定=今日JST)")
    ap.add_argument("--csv", type=str, default="ordered_signals_lss.csv")
    ap.add_argument("--budget", type=float, default=4_000_000.0,
                    help="予算(円)。超過を警告")
    ap.add_argument("--limit-ticks", type=int,
                    default=int(os.environ.get("LSS_H_LIMIT_TICKS", "-5") or -5),
                    help="H の指値を前日終値から何ティックずらしたか(既定 -5)")
    return ap.parse_args()


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


def verify_row(order_price: float, stop: float, target: float,
               atr: float, sm: float, tm: float, qty: int,
               entry_mode: str, limit_ticks: int = -5) -> list[str]:
    """発注1件を検算して、問題があれば日本語のメッセージ一覧を返す(空=OK)。

    ⛔ order_server(発注ボタン) / lss_budget_cap(コマンド) / .\chk の**3経路で共用**する。
       別々に書くと片方だけ直して片方が漏れる(初日はまさにそれで、
       🚀発注 は atr を渡していたのに『100株 発注』だけ渡していなかった)。
    """
    bad = []
    if entry_mode not in ("limit", "auction"):
        bad.append(f"方式が {entry_mode} = **逆指値**。H は limit のはず")
    if atr <= 0:
        bad.append("**ATR が 0** → watcher が実約定価格から OCO を組み直せない"
                   "(損切りが無効化される)")
    if sm <= 0 or tm <= 0:
        bad.append(f"sm/tm が不正 ({sm}/{tm})")
    if not (stop > order_price > target > 0):
        bad.append(f"向きが不正: 損切{stop:,.0f} > 指値{order_price:,.0f} > "
                   f"利確{target:,.0f} でない")
    if atr > 0 and sm > 0 and tm > 0 and order_price > 0:
        cl = order_price - limit_ticks * _tick(order_price)   # 指値から前日終値を復元
        t = _tick(order_price)
        if abs((stop - cl) - atr * sm) > t + 0.51:
            bad.append(f"損切幅ズレ: 実{stop - cl:,.1f} vs ATR×sm {atr * sm:,.1f}")
        if abs((cl - target) - atr * tm) > t + 0.51:
            bad.append(f"利確幅ズレ: 実{cl - target:,.1f} vs ATR×tm {atr * tm:,.1f}")
    if qty <= 0:
        bad.append("株数が0")
    return bad


def _load_rows(date_s: str, csv_path: str) -> list[dict]:
    """発注記録を **watcher と同じ2つのソース** から読む。

    ⛔ ordered_signals_lss.csv だけ見てはいけない。レポートの発注ボタン
       (order_server)は placed_orders_<date>.csv に書くので、ボタンで出した
       注文が丸ごと漏れる(2026-08-13 の初日ぶんがまさにこれ)。
       lss_exit_watcher._load_lss_orders と同じ2経路を見る。
    """
    import glob as _glob
    out = []
    p = Path(csv_path)
    if p.exists():
        with open(p, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if str(r.get("record_date", ""))[:10] != date_s:
                    continue
                if str(r.get("family", "")).strip() != "lss":
                    continue
                out.append({"src": p.name, "symbol": r.get("symbol", ""),
                            "mode": r.get("entry_mode", "") or "stop",
                            "op": _f(r.get("order_price")), "sp": _f(r.get("stop_price")),
                            "tp": _f(r.get("target_price")), "atr": _f(r.get("atr")),
                            "sm": _f(r.get("sm")), "tm": _f(r.get("tm")),
                            "qty": int(_f(r.get("qty")) or 0)})
    for fp in sorted(_glob.glob(str(_BASE / "placed_orders_*.csv"))):
        try:
            with open(fp, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if str(r.get("side", "")).strip() != "short":
                        continue
                    if str(r.get("placed_at", ""))[:10] != date_s:
                        continue
                    if str(r.get("strategy", "")).strip().upper().endswith("_S"):
                        continue     # メインショート(多日保有)は対象外
                    out.append({"src": Path(fp).name, "symbol": r.get("symbol", ""),
                                "mode": r.get("entry_mode", "") or "stop",
                                "op": _f(r.get("entry")), "sp": _f(r.get("stop")),
                                "tp": _f(r.get("target")), "atr": _f(r.get("atr")),
                                "sm": _f(r.get("sm")), "tm": _f(r.get("tm")),
                                "qty": int(_f(r.get("qty")) or 0)})
        except Exception:
            continue
    return out


def main() -> int:
    args = _parse_args()
    _DATE = args.date or datetime.now(JST).strftime("%Y-%m-%d")
    rows = _load_rows(_DATE, args.csv)
    print(f"■ 発注記録の検算  {_DATE}")
    print("=" * 78)
    if not rows:
        print(f"  {_DATE} の記録がありません(ordered_signals_lss.csv / placed_orders_*.csv)。")
        print("  発注していないか、発注記録の書き込みに失敗しています。")
        print("  ⚠ 発注は通っているのに記録だけ失敗するケースがあるので、")
        print("     kabu ステーションの注文一覧も必ず目視すること。")
        return 1

    err, warn = [], []
    total = 0.0
    seen: dict = {}
    print(f"{'コード':<7}{'方式':>8}{'指値':>9}{'損切':>9}{'利確':>9}"
          f"{'ATR':>8}{'株':>5}  {'出所':<22}判定")
    for r in rows:
        sym = str(r["symbol"]).strip()
        bad = verify_row(r["op"], r["sp"], r["tp"], r["atr"], r["sm"], r["tm"],
                         r["qty"], str(r["mode"]).strip(), args.limit_ticks)
        if sym in seen:
            warn.append(f"{sym}: 同じ銘柄が{seen[sym] + 1}件(枠を重複して使う)")
        seen[sym] = seen.get(sym, 0) + 1
        total += r["op"] * r["qty"]
        print(f"{sym:<7}{r['mode']:>8}{r['op']:>9,.0f}{r['sp']:>9,.0f}{r['tp']:>9,.0f}"
              f"{r['atr']:>8,.1f}{r['qty']:>5}  {r['src'][:22]:<22}"
              f"{'OK' if not bad else 'NG'}")
        for b in bad:
            print(f"        └ {b}")
            err.append(f"{sym}: {b}")

    print("-" * 78)
    print(f"  {len(rows)}件 / 想定額 {total:,.0f}円 (予算 {args.budget:,.0f}円)")
    if total > args.budget:
        warn.append(f"想定額が予算を {total - args.budget:,.0f}円 超過")
    for w in warn:
        print(f"  [warn] {w}")
    print()
    if err:
        print(f"⛔ **{len(err)}件の異常**。寄りまでに注文を出し直してください。")
        print("   ATR が 0 のときは python lss_budget_cap.py --execute --prod")
        print("   --entry-mode limit --limit-ticks -5 --budget-multiple 1.0 が確実です。")
        return 1
    print("✅ 異常なし。watcher は実約定価格から OCO を組み直せます。")
    print("   次: .\\watch を寄り前に起動し、15:30 まで止めないこと。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
