"""
daily_guard.py — 保有建玉のシグナル情報を自動取得して損切りガードを実行
=====================================================================

毎営業日 14:50 前後に実行するスクリプト。

【フロー】
  1. kabu API から保有建玉を取得
  2. 各銘柄を WATCHLIST (逆指値B / ブレイクアウト) で検索し
     stop_price / target_price を算出
  3. --log 指定 CSV に holding 行を登録・更新
  4. close_stop_guard.py を実行して損切り・利確・タイムカットを判定
     → 条件に合致すれば MOC 注文を自動発注

【WATCHLIST に無い銘柄】
  stop_price が CSV に設定済みならそのまま使用。
  未設定なら close_stop_guard の ATR 推定にフォールバック。

使い方:
  python daily_guard.py                          # dry-run（確認のみ）
  python daily_guard.py --execute                # デモ口座に実発注
  python daily_guard.py --execute --prod         # 本番口座に実発注
  python daily_guard.py --execute --prod --log my_positions.csv
  python daily_guard.py --show-positions         # 建玉一覧だけ表示
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path


# ── シグナル検索 ───────────────────────────────────────────────────────────────

def _find_signal(symbol: str, days_back: int = 5) -> dict | None:
    """WATCHLIST から symbol のシグナルを逆算する。

    check_signals_stop と check_signals_breakout の両方を検索し、
    直近 days_back 日以内のシグナルを返す。
    """
    import check_signals_stop     as _stop
    import check_signals_breakout as _brk

    sym_t = symbol if symbol.endswith(".T") else f"{symbol}.T"

    # どの戦略か WATCHLIST から探す
    candidates: list[tuple[str, str]] = []  # [(strategy, family), ...]
    for sym_w, _name, strat in _stop.WATCHLIST:
        if sym_w == sym_t:
            candidates.append((strat, "stop"))
    for sym_w, _name, strat in _brk.WATCHLIST:
        if sym_w == sym_t:
            candidates.append((strat, "brk"))

    if not candidates:
        return None

    # 直近 days_back 日で最新のシグナルを探す
    today = date.today()
    for delta in range(days_back):
        check_date = today - timedelta(days=delta)
        for strat, family in candidates:
            try:
                if family == "stop":
                    sig = _stop.check_signal_on_date(sym_t, strat, check_date)
                else:
                    sig = _brk.check_signal_on_date(sym_t, strat, check_date)
                if sig:
                    sig["strategy"] = strat
                    sig["family"]   = family
                    sig["signal_date"] = str(check_date)
                    return sig
            except Exception:
                continue
    return None


# ── kabu 建玉取得 ─────────────────────────────────────────────────────────────

def get_kabu_positions(prod: bool) -> list[dict]:
    from kabu_api import KabuClient
    cli = KabuClient(prod=prod, dry_run=False)
    cli.connect()
    raw = cli.get_positions(product=0)
    result = []
    for p in raw:
        sym   = str(p.get("Symbol", "")).strip()
        side  = str(p.get("Side", "2"))
        qty   = int(p.get("LeavesQty") or p.get("Qty") or 0)
        avg   = p.get("AveragePrice") or p.get("Price") or 0
        cur   = p.get("CurrentPrice") or 0
        cm    = int(p.get("SecurityType") or 1)  # 1=現物 2=信用
        name  = (p.get("SymbolName") or "")[:12]
        pnl   = p.get("Profit") or p.get("ProfitLoss") or 0
        result.append({
            "symbol":      sym,
            "name":        name,
            "side":        "short" if side == "1" else "long",
            "qty":         qty,
            "fill_price":  float(avg),
            "current_price": float(cur),
            "cash_margin": 3 if cm == 2 else 1,  # 信用→返済(3) 現物→1
            "pnl":         float(pnl),
        })
    return result


# ── CSV 更新 ─────────────────────────────────────────────────────────────────

FIELDNAMES = [
    "record_date", "symbol", "name", "strategy", "family",
    "signal_date", "signal_price", "order_price",
    "stop_price", "target_price", "status",
    "fill_date", "fill_price", "exit_date", "exit_price",
    "pnl", "updated_date", "side", "qty", "cash_margin", "bt_score",
]


def _read_csv(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = {k: "" for k in FIELDNAMES}
            row.update(r)
            w.writerow(row)


def update_csv(csv_path: str, positions: list[dict],
               signals: dict[str, dict | None], dry_run: bool) -> None:
    """kabu 建玉をもとに CSV を holding 状態に更新する。"""
    rows = _read_csv(csv_path)
    today_str = str(date.today())

    added = updated = skipped = 0

    for pos in positions:
        sym  = pos["symbol"]
        sig  = signals.get(sym)

        # 既存行を探す（symbol が一致する最新の holding/pending 行）
        existing_idx = None
        for i, r in enumerate(rows):
            if r.get("symbol") == sym and r.get("status") in ("holding", "pending", ""):
                existing_idx = i
                break

        stop_p   = sig["stop_price"]   if sig else None
        target_p = sig["target_price"] if sig else None
        strat    = sig.get("strategy", "?") if sig else "?"
        family   = sig.get("family", "")    if sig else ""
        sig_date = sig.get("signal_date", today_str) if sig else today_str
        sig_price = sig.get("signal_price", pos["fill_price"]) if sig else pos["fill_price"]

        if existing_idx is not None:
            # 既存行を更新（stop/target だけ上書き）
            r = rows[existing_idx]
            changed = False
            if stop_p and not r.get("stop_price"):
                r["stop_price"]   = f"{stop_p:.1f}"
                changed = True
            if target_p and not r.get("target_price"):
                r["target_price"] = f"{target_p:.1f}"
                changed = True
            r["status"]       = "holding"
            r["updated_date"] = today_str
            if changed or r.get("status") != "holding":
                updated += 1
                print(f"  ↻ {sym} {pos['name']}: CSV 更新 stop={stop_p:.0f if stop_p else '?'} target={target_p:.0f if target_p else '?'}")
            else:
                skipped += 1
                print(f"  ✓ {sym} {pos['name']}: 既存行あり（スキップ）stop={r.get('stop_price')} target={r.get('target_price')}")
        else:
            # 新規行を追加
            new_row = {
                "record_date":  today_str,
                "symbol":       sym,
                "name":         pos["name"],
                "strategy":     strat,
                "family":       family,
                "signal_date":  sig_date,
                "signal_price": f"{sig_price:.0f}" if sig_price else "",
                "order_price":  f"{sig['order_price']:.0f}" if sig and sig.get("order_price") else "",
                "stop_price":   f"{stop_p:.1f}" if stop_p else "",
                "target_price": f"{target_p:.1f}" if target_p else "",
                "status":       "holding",
                "fill_date":    today_str,
                "fill_price":   f"{pos['fill_price']:.1f}",
                "side":         pos["side"],
                "qty":          str(pos["qty"]),
                "cash_margin":  str(pos["cash_margin"]),
                "updated_date": today_str,
            }
            rows = [r for r in rows if r.get("symbol") != sym]  # 同銘柄の古い行を除去
            rows.append(new_row)
            added += 1
            stop_str   = f"{stop_p:.0f}"   if stop_p   else "ATR推定"
            target_str = f"{target_p:.0f}" if target_p else "ATR推定"
            print(f"  ✚ {sym} {pos['name']} [{strat}]: 新規追加 "
                  f"fill={pos['fill_price']:.0f} stop={stop_str} target={target_str}")

    if dry_run:
        print(f"\n  [dry-run] 追加={added} 更新={updated} スキップ={skipped} (CSV 未書込)")
    else:
        _write_csv(csv_path, rows)
        print(f"\n  追加={added} 更新={updated} スキップ={skipped} → {csv_path} に保存")


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="保有建玉のシグナル情報を取得して損切りガードを実行")
    ap.add_argument("--execute",       action="store_true", help="実際に発注する（デフォルト: dry-run）")
    ap.add_argument("--prod",          action="store_true", help="本番口座(18080)を使う")
    ap.add_argument("--log",           default="my_positions.csv", help="CSVパス")
    ap.add_argument("--days-back",     type=int, default=5,  help="シグナルを遡る日数（デフォルト: 5）")
    ap.add_argument("--show-positions", action="store_true", help="建玉一覧を表示して終了")
    ap.add_argument("--no-guard",      action="store_true", help="CSV 更新のみ・close_stop_guard は実行しない")
    args = ap.parse_args()

    dry_run   = not args.execute
    env_label = "本番(18080)" if args.prod else "デモ(18081)"

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}daily_guard 開始 ({env_label})")
    print(f"CSVファイル: {args.log}\n")

    # ── 1. kabu 建玉取得 ──
    print("=== 1. kabu 保有建玉を取得 ===")
    try:
        positions = get_kabu_positions(prod=args.prod)
    except Exception as e:
        print(f"✗ kabu 接続失敗: {e}")
        sys.exit(1)

    if not positions:
        print("  保有建玉なし。終了します。")
        return

    for pos in positions:
        pnl_str = f"  損益={pos['pnl']:+,.0f}円" if pos['pnl'] else ""
        print(f"  {pos['symbol']} {pos['name']} [{pos['side']}] "
              f"{pos['qty']}株 @{pos['fill_price']:.0f}円 現在値={pos['current_price']:.1f}{pnl_str}")

    if args.show_positions:
        return

    # ── 2. シグナル検索 ──
    print(f"\n=== 2. WATCHLIST でシグナルを検索（直近{args.days_back}日） ===")
    signals: dict[str, dict | None] = {}
    for pos in positions:
        sym = pos["symbol"]
        sig = _find_signal(sym, days_back=args.days_back)
        if sig:
            print(f"  ✓ {sym} {pos['name']}: [{sig['strategy']}] "
                  f"stop={sig['stop_price']:.0f} target={sig['target_price']:.0f} "
                  f"({sig['signal_date']})")
        else:
            print(f"  ⚠ {sym} {pos['name']}: WATCHLIST に見つからず（ATR推定にフォールバック）")
        signals[sym] = sig

    # ── 3. CSV 更新 ──
    print(f"\n=== 3. CSV 更新 ({args.log}) ===")
    update_csv(args.log, positions, signals, dry_run)

    if args.no_guard:
        print("\n--no-guard のため close_stop_guard をスキップ")
        if dry_run:
            print("dry-run です。実際に更新・発注するには --execute を付けてください。")
        return

    # ── 4. close_stop_guard 実行 ──
    print(f"\n=== 4. close_stop_guard 実行 ===")
    cmd = [sys.executable, "close_stop_guard.py", "--log", args.log]
    if args.execute:
        cmd.append("--execute")
    if args.prod:
        cmd.append("--prod")
    subprocess.run(cmd)

    if dry_run:
        print("\ndry-run です。実際に発注するには --execute を付けてください。")


if __name__ == "__main__":
    main()
