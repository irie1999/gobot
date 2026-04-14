"""
forward_test.py  ―  シグナルのフォワードテスト (紙トレード) 記録 & 評価
=================================================================
バックテストの数字がどれだけ実運用に近いかを検証するため、
毎日のシグナルを記録し、後日その成績を実データで評価する。

【運用の流れ】

  毎日 (引け後):
    python forward_test.py --record
    → 本日のシグナル (run_signals と同じロジックで生成) を CSV に追記
    → 既存の未評価エントリーを最新データで再判定 (約定・決済の有無)

  1週間〜1ヶ月後:
    python forward_test.py --report
    → 記録済みシグナルの実績を集計し、バックテストの数字と比較した
       紙トレード結果を表示

【CSV スキーマ】 forward_test_log.csv
  record_date   : このシグナルを記録した日 (YYYY-MM-DD)
  symbol        : 銘柄コード
  name          : 銘柄名
  strategy      : MACD/A7/RSI2/DON/VOL/MOM
  family        : stop/breakout
  signal_date   : シグナル発生日
  signal_price  : シグナル時株価
  order_price   : 逆指値注文価格
  stop_price    : 損切り価格
  target_price  : 目標価格
  status        : pending / filled / expired / target / stop / timeout / holding
  fill_date     : 約定日 (未約定時は空)
  fill_price    : 約定価格 (未約定時は空)
  exit_date     : 決済日 (未決済時は空)
  exit_price    : 決済価格 (未決済時は空)
  pnl           : 損益 (円, スリッページ+手数料込み)
  updated_date  : 最終更新日

【使い方】
  python forward_test.py --record            # 記録 (毎日実行)
  python forward_test.py --record --no-update # 今日のシグナルだけ記録 (既存評価をしない)
  python forward_test.py --report            # 結果レポート
  python forward_test.py --report --days 30  # 過去30日のレコードのみ集計
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import check_signals_stop     as _stop
import check_signals_breakout as _brk

from backtest_limit_entry import (
    fetch,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY,
    ENTRY_EXPIRE, MAX_HOLD, FIXED_QTY, INITIAL_CASH,
)

JST   = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

# モードごとにログを分ける (conservative と aggressive を混ぜない)
# aggressive (デフォルト) は suffix なし、conservative は "_conservative"
_mode_suffix = f"_{_stop.TRADING_MODE}" if _stop.TRADING_MODE != "aggressive" else ""
LOG_FILE = Path(f"forward_test_log{_mode_suffix}.csv")

FIELDS = [
    "record_date", "symbol", "name", "strategy", "family",
    "signal_date", "signal_price", "order_price", "stop_price", "target_price",
    "status", "fill_date", "fill_price", "exit_date", "exit_price",
    "pnl", "updated_date",
]


# ── ログ I/O ─────────────────────────────────────────────────────
def load_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    with open(LOG_FILE, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_log(rows: list[dict]) -> None:
    with open(LOG_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in FIELDS})


# ── シグナル収集 (今日時点) ──────────────────────────────────────
def collect_today_signals() -> list[dict]:
    """check_signals_stop / breakout の全 WATCHLIST で今日のシグナルを収集。"""
    signals: list[dict] = []

    def _scan_group(mod, family: str):
        out = []
        with ThreadPoolExecutor(max_workers=_stop._DEFAULT_WORKERS) as ex:
            futs = {ex.submit(mod.check_signal_on_date,
                              sym, strat, None): (sym, name, strat)
                    for sym, name, strat in mod.WATCHLIST}
            for fut in as_completed(futs):
                sym, name, strat = futs[fut]
                try:
                    sig = fut.result()
                except Exception:
                    sig = None
                if not sig:
                    continue
                out.append(dict(
                    record_date=str(TODAY),
                    symbol=sym, name=name, strategy=strat, family=family,
                    signal_date=sig["signal_date"],
                    signal_price=float(sig["signal_price"]),
                    order_price=float(sig["order_price"]),
                    stop_price=float(sig["stop_price"]),
                    target_price=float(sig["target_price"]),
                    status="pending",
                    fill_date="", fill_price="",
                    exit_date="", exit_price="",
                    pnl="",
                    updated_date=str(TODAY),
                ))
        return out

    signals.extend(_scan_group(_stop, "stop"))
    signals.extend(_scan_group(_brk,  "breakout"))
    return signals


# ── 既存エントリーの評価 (最新データ) ────────────────────────────
def evaluate_entry(row: dict) -> dict:
    """
    1 つのログ行を最新データで再評価する。
    状態遷移: pending → filled → (target/stop/timeout/holding)
             pending → expired (期限切れ)
    """
    if row.get("status") in ("target", "stop", "timeout", "expired"):
        return row   # 終了状態は変更しない

    symbol       = row["symbol"]
    signal_date  = row["signal_date"]
    order_price  = float(row["order_price"])
    stop_price   = float(row["stop_price"])
    target_price = float(row["target_price"])

    df = fetch(symbol, 400)
    if df is None or len(df) < 5:
        return row

    try:
        import pandas as pd
        sig_ts = pd.Timestamp(signal_date)
    except Exception:
        return row

    # シグナル日の次の営業日以降を評価対象
    future = df[df.index > sig_ts]
    if len(future) == 0:
        return row

    # 評価ロジック (run_limit_backtest の簡略版)
    state = row.get("status") or "pending"
    fill_idx = None
    exit_idx = None
    exit_reason = None
    exit_price = None
    entry_p = None
    if state == "filled" or state == "holding":
        # 既に約定済み → future から再スタート
        try:
            fd = row.get("fill_date", "")
            if fd:
                fts = pd.Timestamp(fd)
                future = df[df.index > fts]
                entry_p = float(row.get("fill_price") or 0)
        except Exception:
            pass

    # ── pending: 約定判定 ──
    if state == "pending":
        for i, (dt, r) in enumerate(future.iterrows()):
            if i >= ENTRY_EXPIRE:
                state = "expired"
                row["status"] = "expired"
                row["updated_date"] = str(TODAY)
                return row
            hi = float(r["high"])
            if hi >= order_price:
                # 約定 (逆指値): スリッページ込みの entry_p
                entry_p = order_price * (1.0 + SLIPPAGE_STOP_PCT)
                row["fill_date"]  = dt.strftime("%Y-%m-%d")
                row["fill_price"] = round(entry_p, 2)
                state = "filled"
                fill_idx = i
                # 同日に決済可能か
                lo = float(r["low"])
                if hi >= target_price and lo <= stop_price:
                    exit_price = target_price
                    exit_reason = "target"
                elif hi >= target_price:
                    exit_price = target_price
                    exit_reason = "target"
                elif lo <= stop_price:
                    exit_price = stop_price * (1.0 - SLIPPAGE_STOP_PCT)
                    exit_reason = "stop"
                if exit_reason:
                    row["exit_date"]  = dt.strftime("%Y-%m-%d")
                    row["exit_price"] = round(exit_price, 2)
                    row["status"]     = exit_reason
                    fee = (entry_p + exit_price) * FIXED_QTY * FEE_PCT_ONE_WAY
                    row["pnl"]        = round((exit_price - entry_p) * FIXED_QTY - fee, 0)
                    row["updated_date"] = str(TODAY)
                    return row
                break

    # ── filled: 決済判定 (約定日の翌日以降) ──
    if state == "filled" and entry_p:
        try:
            fd  = row.get("fill_date") or ""
            fts = pd.Timestamp(fd)
            after_fill = df[df.index > fts]
        except Exception:
            after_fill = future

        hold_days = 0
        for j, (dt, r) in enumerate(after_fill.iterrows()):
            hold_days = j + 1
            hi = float(r["high"])
            lo = float(r["low"])
            cl = float(r["close"])
            if hi >= target_price and lo <= stop_price:
                exit_price = target_price
                exit_reason = "target"
            elif hi >= target_price:
                exit_price = target_price
                exit_reason = "target"
            elif lo <= stop_price:
                exit_price = stop_price * (1.0 - SLIPPAGE_STOP_PCT)
                exit_reason = "stop"
            elif hold_days >= MAX_HOLD:
                exit_price = cl
                exit_reason = "timeout"
            if exit_reason:
                row["exit_date"]  = dt.strftime("%Y-%m-%d")
                row["exit_price"] = round(exit_price, 2)
                row["status"]     = exit_reason
                fee = (entry_p + exit_price) * FIXED_QTY * FEE_PCT_ONE_WAY
                row["pnl"]        = round((exit_price - entry_p) * FIXED_QTY - fee, 0)
                row["updated_date"] = str(TODAY)
                return row

        # まだ保有中
        row["status"] = "holding"
        row["updated_date"] = str(TODAY)
        return row

    row["updated_date"] = str(TODAY)
    return row


# ── 記録モード ───────────────────────────────────────────────────
def cmd_record(update_existing: bool = True) -> None:
    print("=" * 78)
    print(f"フォワードテスト 記録モード ({TODAY})  モード: {_stop.TRADING_MODE}")
    print(f"ログファイル: {LOG_FILE}")
    print("=" * 78)

    log = load_log()
    print(f"既存ログ: {len(log)} 件")

    # 1. 既存エントリーの再評価
    if update_existing and log:
        print(f"\n既存エントリー再評価中...")
        updated = 0
        for i, row in enumerate(log):
            old_status = row.get("status", "")
            new_row    = evaluate_entry(dict(row))
            if new_row.get("status") != old_status:
                updated += 1
            log[i] = new_row
            if (i + 1) % 20 == 0:
                print(f"  進捗: {i+1}/{len(log)}", flush=True)
        print(f"  更新: {updated} 件")

    # 2. 本日のシグナル収集
    print(f"\n本日のシグナル収集中...")
    today_sigs = collect_today_signals()
    print(f"  {len(today_sigs)} 件のシグナルを検出")

    # 重複排除 (同じ record_date + symbol + strategy は記録しない)
    existing_keys = {
        (r["record_date"], r["symbol"], r["strategy"])
        for r in log
    }
    new_rows = [
        s for s in today_sigs
        if (s["record_date"], s["symbol"], s["strategy"]) not in existing_keys
    ]
    print(f"  新規追加: {len(new_rows)} 件 (重複除外後)")

    log.extend(new_rows)

    # 新規シグナルもすぐ評価
    if new_rows and update_existing:
        print(f"\n新規シグナルを評価中...")
        for i, row in enumerate(log[-len(new_rows):]):
            idx = len(log) - len(new_rows) + i
            log[idx] = evaluate_entry(dict(row))

    save_log(log)
    print(f"\n完了。ログ: {LOG_FILE.resolve()}  ({len(log)} 件)")


# ── レポートモード ───────────────────────────────────────────────
def cmd_report(days: int | None = None) -> None:
    log = load_log()
    if not log:
        print("[ERROR] ログが空です。先に  python forward_test.py --record  を実行してください。",
              file=sys.stderr)
        sys.exit(1)

    print("=" * 78)
    print(f"フォワードテスト レポート ({TODAY})  モード: {_stop.TRADING_MODE}")
    print(f"ログファイル: {LOG_FILE}")
    print("=" * 78)

    # 期間フィルター
    if days is not None:
        cutoff = str(TODAY - timedelta(days=days))
        log = [r for r in log if r.get("record_date", "") >= cutoff]
        print(f"対象期間: 過去 {days} 日 ({cutoff}〜{TODAY})")

    print(f"ログ件数: {len(log)}")

    # 重複除去: 同じ (symbol, strategy, signal_date) は最新1件のみ評価
    unique: dict[tuple, dict] = {}
    for r in log:
        key = (r.get("symbol", ""), r.get("strategy", ""),
               r.get("signal_date", ""))
        cur = unique.get(key)
        if cur is None or r.get("updated_date", "") > cur.get("updated_date", ""):
            unique[key] = r
    rows = list(unique.values())
    print(f"ユニークシグナル: {len(rows)} 件")

    # ── 集計 (ファミリー別 → 戦略別) ──
    def _float(v, d=0.0):
        try:
            return float(v) if v != "" else d
        except (ValueError, TypeError):
            return d

    def _agg(filtered):
        total = len(filtered)
        pending  = sum(1 for r in filtered if r.get("status") == "pending")
        expired  = sum(1 for r in filtered if r.get("status") == "expired")
        filled   = sum(1 for r in filtered if r.get("status") in ("target", "stop", "timeout", "holding", "filled"))
        target   = sum(1 for r in filtered if r.get("status") == "target")
        stopped  = sum(1 for r in filtered if r.get("status") == "stop")
        timeout  = sum(1 for r in filtered if r.get("status") == "timeout")
        holding  = sum(1 for r in filtered if r.get("status") == "holding")
        closed   = target + stopped + timeout
        wins     = sum(1 for r in filtered
                       if r.get("status") in ("target", "stop", "timeout")
                       and _float(r.get("pnl")) > 0)
        total_pnl = sum(_float(r.get("pnl")) for r in filtered
                        if r.get("status") in ("target", "stop", "timeout"))
        win_rate  = (wins / closed * 100) if closed > 0 else 0
        fill_rate = (filled / total * 100) if total > 0 else 0
        return dict(
            total=total, pending=pending, expired=expired,
            filled=filled, target=target, stop=stopped,
            timeout=timeout, holding=holding, closed=closed,
            wins=wins, total_pnl=total_pnl,
            win_rate=win_rate, fill_rate=fill_rate,
        )

    print("\n" + "=" * 78)
    print("ファミリー別集計")
    print("=" * 78)

    for family_label, family_key in [("逆指値B", "stop"), ("ブレイクアウト", "breakout")]:
        filtered = [r for r in rows if r.get("family") == family_key]
        if not filtered:
            continue
        a = _agg(filtered)
        print(f"\n[{family_label}]")
        print(f"  総シグナル数  : {a['total']}")
        print(f"  約定          : {a['filled']} ({a['fill_rate']:.1f}%)")
        print(f"    └ 目標達成  : {a['target']}")
        print(f"    └ 損切り    : {a['stop']}")
        print(f"    └ タイムカット: {a['timeout']}")
        print(f"    └ 保有中    : {a['holding']}")
        print(f"  期限切れ      : {a['expired']}")
        print(f"  保留          : {a['pending']}")
        print(f"  決済済み勝率  : {a['win_rate']:.1f}% ({a['wins']}/{a['closed']})")
        print(f"  決済済み損益  : {a['total_pnl']:+,.0f} 円")

    # ── 戦略別詳細 ──
    print("\n" + "=" * 78)
    print("戦略別詳細")
    print("=" * 78)
    print(f"  {'戦略':<8}{'総数':>6}{'約定':>6}{'勝':>5}{'負':>5}{'勝率':>8}"
          f"{'fill率':>8}{'損益':>14}")
    print("  " + "-" * 66)
    strategies = sorted({r.get("strategy", "") for r in rows})
    for strat in strategies:
        if not strat:
            continue
        filtered = [r for r in rows if r.get("strategy") == strat]
        a = _agg(filtered)
        losses = a["closed"] - a["wins"]
        print(f"  {strat:<8}{a['total']:>6}{a['filled']:>6}"
              f"{a['wins']:>5}{losses:>5}{a['win_rate']:>7.1f}%"
              f"{a['fill_rate']:>7.1f}%{a['total_pnl']:>+14,.0f}")

    # ── バックテスト数字との比較 (注意喚起) ──
    print("\n" + "=" * 78)
    print("バックテスト数字との比較")
    print("=" * 78)
    print("バックテスト (run_signals.py / verify_watchlist.py) との乖離を見て、")
    print("実運用モデルの精度を検証してください。")
    print("  - 勝率: バックテスト値から -10 〜 -30% 劣化が目安")
    print("  - PF  : バックテスト値から -30 〜 -60% 劣化が目安")
    print("  - fill率 が低い (< 50%): 逆指値の注文価格が厳しすぎる可能性")
    print()


# ── メイン ───────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="フォワードテスト (紙トレード記録)")
    parser.add_argument("--record",    action="store_true",
                        help="本日のシグナルを記録 & 既存エントリーを再評価")
    parser.add_argument("--no-update", action="store_true",
                        help="--record 時に既存エントリーの再評価をスキップ")
    parser.add_argument("--report",    action="store_true",
                        help="集計レポートを表示")
    parser.add_argument("--days",      type=int, default=None,
                        help="--report 時に集計対象日数を制限 (デフォルト: 全期間)")
    # モード選択 (実際の切替は import 前に sys.argv で行う)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--aggressive",   action="store_true",
                            help="積極利確モード (tm=1.5, デフォルト)")
    mode_group.add_argument("--conservative", action="store_true",
                            help="標準モード (tm=3.0, 旧デフォルト)")
    args = parser.parse_args()

    if not args.record and not args.report:
        parser.print_help()
        print("\n[ERROR] --record または --report を指定してください。", file=sys.stderr)
        sys.exit(1)

    if args.record:
        cmd_record(update_existing=not args.no_update)

    if args.report:
        cmd_report(days=args.days)


if __name__ == "__main__":
    main()
