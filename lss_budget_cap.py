"""lss_budget_cap.py — lss を「予算より多めに発注(over-subscribe)＋約定累計が予算到達で残りを取消」で出す。

背景(sim_portfolio_lss.py の検証結果, 2026-08-01):
  予算400万に対し金額ベース約定率が低く(実測~50%)、平均約定額<予算=予算が遊んでいる。
  BT降順で M×予算ぶん注文を出し、約定累計が予算に達したら残りの未発動逆指値をキャンセルすると、
  平常日は予算ぴったり埋まり(=稼働率↑・OOS損益↑)、急落日だけ上限で頭打ちになる。
  → 本スクリプトはその「発注 + 上限キャンセル監視」を live で行う。

方式:
  1) 今日の lss シグナルを収集(kabu_send_lss と同一ロジック)。lss_trades.csv の bt を付与し BT降順。
  2) 価格レンジ(既定1000-6000=実運用 daily と統一)で絞り、BT降順に信用新規売り逆指値を発注。
     注文額(トリガー×株数)の累計が『予算 × --budget-multiple』に達するまで出す(over-subscribe)。
  3) 監視ループ: get_orders で自分の注文の約定数量(CumQty)を集計。約定額の累計が『予算』に達したら
     まだ約定していない(CumQty=0)未発動注文をキャンセル(=上限管理)。予算未達なら注文は残す。

【安全設計】(kabu_send_lss と同じポリシー)
  - 既定 dry-run(発注しない・監視しない・発注プランを表示するだけ)。--execute で実発注。
  - --execute でも既定デモ(18081)。本番(18080)は --prod 明示必須。
  - 逆指値売りは即約定回避のためトリガーを現値-1ティックに引下げ、-3%指値下限ガードを付ける。

【重要・トークン競合】(§18.4)
  発注サーバ(order_server)や lss_exit_watcher と同時に動かさないこと(kabu 有効トークンは1つ)。
  本スクリプトは『朝の発注+上限監視』フェーズ用。約定が落ち着いたら停止し、決済監視(watcher)に
  切り替える。監視は --monitor-until(既定10:30)または予算到達で終了する。全日キャップは将来
  watcher 側へ統合(それまでは監視終了後に約定した分は予算を超えうる=倍率を上げ過ぎない運用で吸収)。

使い方:
  python lss_budget_cap.py                                   # dry-run: 発注プランを表示(接続なし)
  python lss_budget_cap.py --budget 4000000 --budget-multiple 2.0
  python lss_budget_cap.py --execute                         # デモ口座に発注+上限監視
  python lss_budget_cap.py --execute --prod                  # 本番(要明示)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time as _time
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import backtest_limit_entry as ble                                  # noqa: E402
from kabu_api import KabuClient, CASH_MARGIN_OPEN                   # noqa: E402
from backtest_limit_entry import tick_size, round_to_tick          # noqa: E402
# lss シグナル収集は kabu_send_lss と完全共有(ロジック二重化を避ける)
from kabu_send_lss import _load_symbols, _lss_signal_today, _jq_to_yf, DEFAULT_SM, DEFAULT_TM  # noqa: E402

JST = timezone(timedelta(hours=9))
FIXED_QTY = ble.FIXED_QTY


def _norm(sym):
    return str(sym).upper().removesuffix(".T").split(".")[0]


def _load_bt(trades_csv: str) -> dict:
    """lss_trades.csv の bt を {(sym,strat): bt} で返す(レポートと一致)。無ければ空。"""
    p = Path(trades_csv)
    if not p.exists():
        print(f"[warn] BTソースCSVが無い: {p}（BT=0 として全シグナルを末尾扱い）", file=sys.stderr)
        return {}
    bt = {}
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        sym = _norm(r.get("symbol") or r.get("code") or "")
        strat = str(r.get("strategy") or "").strip()
        if not sym or not strat:
            continue
        try:
            b = float(r.get("bt") or 0)
        except Exception:
            b = 0.0
        bt[(sym, strat)] = max(bt.get((sym, strat), -1e9), b)
    return bt


def _parse_hhmm(s: str, default: dtime) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


def main() -> int:
    ap = argparse.ArgumentParser(
        description="lss を over-subscribe 発注＋約定累計が予算到達で残りを取消(上限管理)")
    ap.add_argument("--execute", action="store_true", help="実発注+監視(未指定=dry-run:プラン表示のみ)")
    ap.add_argument("--prod", action="store_true", help="本番(18080)。未指定=デモ(18081)")
    ap.add_argument("--budget", type=float, default=4_000_000.0, help="予算(円)。約定累計がこれに達したら残り注文を取消")
    ap.add_argument("--budget-multiple", type=float, default=2.0,
                    help="予算の何倍ぶん注文を出すか(over-subscribe)。実測fill率~50%なら2.0で予算ちょうど埋まる")
    ap.add_argument("--min-price", type=float, default=1000.0, help="対象最低株価(実運用 daily=1000)")
    ap.add_argument("--max-price", type=float, default=6000.0, help="対象最高株価(実運用 daily=6000)")
    ap.add_argument("--bt-min", type=float, default=0.0, help="BT下限(§18.2は BT30以上・降順。既定0=全件をBT降順)")
    ap.add_argument("--trades-csv", type=str, default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"),
                    help="BT付与元CSV(=レポート一致)")
    ap.add_argument("--symbols-file", default=None, help="(code,name,strategy) の SELECTED を持つ .py")
    ap.add_argument("--qty", type=int, default=FIXED_QTY, help=f"株数(既定{FIXED_QTY})")
    ap.add_argument("--sm", type=float, default=DEFAULT_SM, help=f"損切ATR倍(既定{DEFAULT_SM})")
    ap.add_argument("--tm", type=float, default=DEFAULT_TM, help=f"利確ATR倍(既定{DEFAULT_TM})")
    ap.add_argument("--days", type=int, default=60, help="シグナル判定の日足ルックバック(既定60)")
    ap.add_argument("--gap-guard", type=float,
                    default=getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03),
                    help="発動後の指値下限ガード率(-この%%超の窓開けは約定させない)")
    ap.add_argument("--no-gap-guard", action="store_true", help="下限ガードを外す(発動後成行)")
    ap.add_argument("--margin-type", type=int, default=3, help="信用区分 3=一般デイトレ(既定)")
    ap.add_argument("--poll-sec", type=float, default=10.0, help="約定監視の間隔秒(既定10)")
    ap.add_argument("--monitor-until", type=str, default="10:30",
                    help="監視を打ち切る時刻 HH:MM(既定10:30)。予算到達か この時刻で監視終了")
    ap.add_argument("--aggressive", action="store_true")
    ap.add_argument("--conservative", action="store_true")
    args = ap.parse_args()

    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注+監視★" if args.execute else "dry-run(プラン表示のみ)"
    cap = args.budget * args.budget_multiple
    now = datetime.now(JST)
    print("=" * 72)
    print(f"lss 予算キャップ発注  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label} / 接続先: {env_label} / 信用新規売り(デイトレ{args.margin_type})")
    print(f"予算 {args.budget/1e4:.0f}万 × 倍率 {args.budget_multiple:.1f} = 注文枠 {cap/1e4:.0f}万ぶん / "
          f"価格{args.min_price:.0f}-{args.max_price:.0f} / BT≥{args.bt_min:.0f}降順 / 株数{args.qty}")
    print("=" * 72)
    if args.execute:
        print("⚠ order_server / lss_exit_watcher と同時起動しないこと(トークン競合)。朝の発注フェーズ専用。")

    # ── シグナル収集 → BT付与 → 価格フィルタ → BT降順 ──
    pairs = _load_symbols(args.symbols_file)
    bt_map = _load_bt(args.trades_csv)
    print("本日の lss シグナルを収集中...")
    signals = []
    for (code, name, strat) in pairs:
        sig = _lss_signal_today(code, name, strat, args.sm, args.tm, args.days)
        if not sig:
            continue
        op = float(sig["order_price"])
        if not (args.min_price <= op <= args.max_price):
            continue
        sig["bt"] = bt_map.get((_norm(sig["symbol"]), strat), 0.0)
        if sig["bt"] < args.bt_min:
            continue
        signals.append(sig)

    if not signals:
        print("本日の lss シグナル(条件内)なし。終了します。")
        return 0
    signals.sort(key=lambda s: s["bt"], reverse=True)

    # ── over-subscribe: 注文額の累計が cap に達するまで BT降順で採用 ──
    plan = []
    placed_not = 0.0
    for s in signals:
        if placed_not >= cap:
            break
        note = s["order_price"] * args.qty
        plan.append(s)
        placed_not += note

    print(f"\nシグナル {len(signals)}件 → over-subscribe 発注プラン {len(plan)}件 "
          f"(注文額累計 {placed_not/1e4:.0f}万 / 枠 {cap/1e4:.0f}万)")
    print("-" * 72)
    for i, s in enumerate(plan, 1):
        print(f"  {i:>2}. BT{s['bt']:>3.0f} {s['symbol']} {s['name']} [{s['strategy']}] "
              f"@≤{s['order_price']:,.0f} 損切¥{s['stop_price']:,.0f}/利確¥{s['target_price']:,.0f} "
              f"(注文額 {s['order_price']*args.qty/1e4:.0f}万)")
    print("-" * 72)

    if not args.execute:
        print("dry-run のため発注・監視しません。--execute で発注+上限監視を行います。")
        print(f"※ 実発注時は約定累計が {args.budget/1e4:.0f}万 に達した時点で未発動の残り注文を自動キャンセルします。")
        return 0

    # ── 実発注 ──
    cli = KabuClient(prod=args.prod, dry_run=False, margin_type=args.margin_type)
    try:
        cli.connect()
    except Exception as e:
        print(f"✗ kabu 接続失敗: {e}")
        return 1

    placed = []   # {order_id, symbol, trigger, qty, notional}
    for s in plan:
        sym = _norm(s["symbol"])
        trig = round_to_tick(float(s["order_price"]))
        # 即約定回避: トリガーが現値以上だと弾かれる → 現値-1ティックに引下げ
        try:
            cur = cli.get_current_price(sym)
        except Exception:
            cur = None
        if cur and cur > 0:
            if trig >= cur:
                trig = round_to_tick(cur - tick_size(cur))
        else:
            trig = round_to_tick(trig - tick_size(trig))
        after = None if args.no_gap_guard else round_to_tick(trig * (1.0 - args.gap_guard))
        res = cli.send_stop_sell(sym, qty=args.qty, trigger_price=trig,
                                 cash_margin=CASH_MARGIN_OPEN, after_hit_price=after)
        oid = res.get("OrderId")
        if res.get("Result") == 0 and oid:
            placed.append({"order_id": oid, "symbol": sym, "trigger": float(trig),
                           "qty": args.qty, "notional": float(trig) * args.qty})
            print(f"  ✓ {sym} {s['name']} 発注 OrderId={oid} @≤{trig:,.0f}")
        else:
            print(f"  ✗ {sym} {s['name']} 発注失敗: {res}")

    if not placed:
        print("発注成功0件。監視を行いません。")
        return 1
    print(f"\n発注完了 {len(placed)}件。約定累計が {args.budget/1e4:.0f}万 到達で残りを取消。監視開始…")

    # ── 上限監視ループ ──
    until = _parse_hhmm(args.monitor_until, dtime(10, 30))
    by_id = {p["order_id"]: p for p in placed}
    cancelled = set()
    while True:
        nowt = datetime.now(JST)
        if nowt.time() >= until:
            print(f"[監視終了] {args.monitor_until} 到達。残り注文はそのまま(予算未達=遊休許容)。")
            break
        try:
            orders = cli.get_orders()
        except Exception as e:
            print(f"  ⚠ get_orders 失敗(継続): {e}")
            _time.sleep(args.poll_sec)
            continue
        # 自分の注文の約定数量を集計
        cum_by_id = {}
        state_by_id = {}
        for o in orders:
            oid = str(o.get("ID") or o.get("OrderId") or "")
            if oid in by_id:
                cum_by_id[oid] = float(o.get("CumQty", 0) or 0)
                state_by_id[oid] = int(o.get("State", 0) or 0)
        filled_notional = sum(min(cum_by_id.get(pid, 0.0), p["qty"]) * p["trigger"]
                              for pid, p in by_id.items())
        nfill = sum(1 for pid in by_id if cum_by_id.get(pid, 0.0) > 0)
        print(f"  [{nowt:%H:%M:%S}] 約定 {nfill}/{len(placed)}件  約定額 {filled_notional/1e4:.0f}万 "
              f"/ 予算 {args.budget/1e4:.0f}万")
        if filled_notional >= args.budget:
            # 予算到達 → 未約定(CumQty=0)かつ未終了の残りをキャンセル
            print(f"  ★予算 {args.budget/1e4:.0f}万 到達。未発動の残り注文をキャンセルします。")
            for pid, p in by_id.items():
                if pid in cancelled:
                    continue
                if cum_by_id.get(pid, 0.0) > 0:
                    continue                        # 約定済(または一部約定)は残す
                if state_by_id.get(pid, 0) >= 5:
                    continue                        # 既に終了(取消/失効)
                r = cli.cancel_order(pid)
                cancelled.add(pid)
                print(f"    取消 {p['symbol']} OrderId={pid}: Result={r.get('Result')}")
            print(f"[監視終了] 上限キャンセル完了({len(cancelled)}件取消)。決済監視(watcher)に切り替えてください。")
            break
        _time.sleep(args.poll_sec)

    print("─" * 72)
    print("【EXIT】lss は同日決済。約定分は lss_exit_watcher で日中OCO+引け決済してください。")
    print("  ※ 本スクリプト停止後に watcher を起動(トークン競合回避)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
