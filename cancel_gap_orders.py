r"""cancel_gap_orders.py — 寄り深ギャップの lss 逆指値を『確実に』取り消す(#3)。

背景(2026-07-31, analyze_gap_fills の OOS検証):
  lss逆指値売りが「寄りでトリガーより -X% 超も下に飛ぶ」と、実運用では -X% 下限指値が板に残り、
  価格が下限に戻った時に下限で約定してしまう。この深ギャップ約定は **net 損(OOS PF0.41)**。
  バックテストは -X% 超を『約定不可』として除外(_INTRADAY_5M_ENTRY_GAP_LIMIT)しているので、
  実運用も「寄りで -X% 超下がった lss逆指値は寄り直後に取り消す」ことでバックテストと一致させ、
  かつ負ける約定を回避する。X = バックテストのガードと同値(既定 2%)。

安全設計(誤取消防止):
  * 既定 dry-run(--execute のときだけ実取消)。接続先は既定デモ、本番は --prod 明示。
  * 取り消すのは「①信用新規(CashMargin=2) ②売り(Side=1) ③未約定(State∈{1,2,3,4})
    ④今日の lss 発注記録(ordered_signals_lss.csv / placed_orders_<date>.csv)に載っている銘柄
    ⑤現在値 < トリガー×(1-gap)」を **すべて** 満たす逆指値だけ。
    メインショート(_S)・ロング・手動注文・現物には一切触らない。
  * トリガーは lss 記録の『最新発注日の order_price/entry』を使う(watcher の照合と同一ロジック)。
  * 確実性: --watch 秒だけループ + cancel_order のリトライ内蔵 + 毎ループ get_orders で
    「まだ active なら再取消」= 取消が通るまで粘る。取消後 active から消えたら done。

使い方(寄り直後 09:00〜09:05 に実行):
  python cancel_gap_orders.py                       # dry-run(判定だけ・何も取り消さない)
  python cancel_gap_orders.py --execute             # デモ口座で取消
  python cancel_gap_orders.py --execute --prod      # 本番口座で取消
  python cancel_gap_orders.py --execute --prod --watch 300   # 5分間ループ(=確実)
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
ACTIVE_STATES = {1, 2, 3, 4}   # 受付/待機/発注中/一部約定(=まだ取消可能)。5=終了は対象外。


def _trigger_for(recs: list[dict]) -> float:
    """lss記録(同一銘柄)の中で『最新発注日』の order_price(=トリガー)を返す。
    lssトリガー=前日終値-1tick は日々変わるので、最新日の値を使う(watcher の照合と同じ)。"""
    best = None
    for r in recs:
        e = float(r.get("entry", 0) or 0)
        if e <= 0:
            continue
        d = str(r.get("date", ""))
        if best is None or d > best[0]:
            best = (d, e)
    return best[1] if best else 0.0


def _sweep(cli, lss_map, gap, done, dry) -> tuple[int, int]:
    """1回ぶん: active な深ギャップ lss 新規売り逆指値を取り消す。
    Returns: (取消対象件数, 取消送信できた件数)。done は取消確認済み OrderId の集合(更新)。"""
    from kabu_api import SIDE_SELL, CASH_MARGIN_OPEN
    try:
        orders = cli.get_orders()
    except Exception as e:
        if "401" in str(e) or "Unauthorized" in str(e):
            try:
                print("  [再接続] トークン失効(401) → 再接続してリトライ")
                cli.connect(); orders = cli.get_orders()
            except Exception as e2:
                print(f"  ⚠ get_orders 失敗(再接続後も): {e2}"); return 0, 0
        else:
            print(f"  ⚠ get_orders 失敗: {e}"); return 0, 0

    # 現在 active な OrderId(取消が通ったか検証用)
    active_ids = set()
    for o in orders:
        st = int(o.get("OrderState") or o.get("State") or 0)
        if st in ACTIVE_STATES and o.get("ID"):
            active_ids.add(o.get("ID"))
    # 前ループで取消送信したものが active から消えていれば確定
    for oid in list(done):
        if oid not in active_ids:
            done[oid] = "gone"

    n_target = n_sent = 0
    for o in orders:
        oid = o.get("ID", "")
        if not oid or done.get(oid) == "gone":
            continue
        if o.get("Side") != SIDE_SELL:
            continue
        if int(o.get("CashMargin") or 0) != CASH_MARGIN_OPEN:   # 信用新規売りのみ
            continue
        st = int(o.get("OrderState") or o.get("State") or 0)
        if st not in ACTIVE_STATES:
            continue
        sym = str(o.get("Symbol", "")).upper().removesuffix(".T").split(".")[0]
        recs = lss_map.get(sym)
        if not recs:
            continue   # lss記録に無い銘柄 → 触らない(メインショート/手動等の保護)
        trigger = _trigger_for(recs)
        if trigger <= 0:
            continue
        floor = trigger * (1.0 - gap)
        cur = cli.get_current_price(sym)
        if not cur or cur <= 0:
            print(f"  [{sym}] 現在値取得不可 → 次ループで再試行")
            continue
        if cur >= floor:
            continue   # まだ -X% 下限より上 = 正常約定の可能性 → 取り消さない
        # 深ギャップ確定: 現在値 < トリガー-X%
        n_target += 1
        nm = recs[0].get("name", "")
        print(f"  [深ギャップ] {sym} {nm} 現在{cur:,.0f} < 下限{floor:,.0f} "
              f"(トリガー{trigger:,.0f} の -{gap*100:.1f}%) OrderId={oid} State={st}")
        if dry:
            print("    → [dry-run] 実行時はこの注文を取り消します")
            continue
        res = cli.cancel_order(oid)
        if res.get("Result") == 0 or res.get("_dry_run"):
            n_sent += 1
            done.setdefault(oid, "sent")
            print("    → 取消送信OK(次ループで消えたか検証)")
        else:
            print(f"    → 取消失敗: {res} (次ループで再試行)")
    return n_target, n_sent


def _budget_sweep(cli, lss_map, done, dry) -> tuple[int, int]:
    """予算到達時の上限管理: active かつ『未発動(CumQty==0)』の lss 新規売り逆指値を全取り消す。
    深ギャップ(_sweep)と違い価格条件は付けず、残っている未約定 lss エントリーを一掃する
    (=約定累計が予算に達したら『それ以外は取消』)。一部約定(CumQty>0)は残す。
    Returns: (取消対象件数, 取消送信できた件数)。done は取消確認済み OrderId 集合(更新)。"""
    from kabu_api import SIDE_SELL, CASH_MARGIN_OPEN
    try:
        orders = cli.get_orders()
    except Exception as e:
        if "401" in str(e) or "Unauthorized" in str(e):
            try:
                print("  [再接続] トークン失効(401) → 再接続してリトライ")
                cli.connect(); orders = cli.get_orders()
            except Exception as e2:
                print(f"  ⚠ get_orders 失敗(再接続後も): {e2}"); return 0, 0
        else:
            print(f"  ⚠ get_orders 失敗: {e}"); return 0, 0

    active_ids = set()
    for o in orders:
        st = int(o.get("OrderState") or o.get("State") or 0)
        if st in ACTIVE_STATES and o.get("ID"):
            active_ids.add(o.get("ID"))
    for oid in list(done):
        if oid not in active_ids:
            done[oid] = "gone"

    n_target = n_sent = 0
    for o in orders:
        oid = o.get("ID", "")
        if not oid or done.get(oid) == "gone":
            continue
        if o.get("Side") != SIDE_SELL:
            continue
        if int(o.get("CashMargin") or 0) != CASH_MARGIN_OPEN:   # 信用新規売りのみ
            continue
        st = int(o.get("OrderState") or o.get("State") or 0)
        if st not in ACTIVE_STATES:
            continue
        if float(o.get("CumQty", 0) or 0) > 0:
            continue   # 一部約定は残す(未発動=CumQty0 のみ取消)
        sym = str(o.get("Symbol", "")).upper().removesuffix(".T").split(".")[0]
        recs = lss_map.get(sym)
        if not recs:
            continue   # lss記録に無い銘柄 → 触らない(メインショート/手動等の保護)
        n_target += 1
        nm = recs[0].get("name", "")
        print(f"  [予算到達取消] {sym} {nm} 未発動lss新規売り逆指値 OrderId={oid} State={st}")
        if dry:
            print("    → [dry-run] 実行時はこの注文を取り消します")
            continue
        res = cli.cancel_order(oid)
        if res.get("Result") == 0 or res.get("_dry_run"):
            n_sent += 1
            done.setdefault(oid, "sent")
            print("    → 取消送信OK(次ループで消えたか検証)")
        else:
            print(f"    → 取消失敗: {res} (次ループで再試行)")
    return n_target, n_sent


def main():
    ap = argparse.ArgumentParser(description="寄り深ギャップの lss 逆指値を確実に取り消す")
    ap.add_argument("--execute", action="store_true", help="実際に取り消す(既定 dry-run)")
    ap.add_argument("--prod", action="store_true", help="本番口座(既定デモ)")
    ap.add_argument("--gap", type=float, default=None,
                    help="下限ガード率。既定=バックテストの _INTRADAY_5M_ENTRY_GAP_LIMIT(=2%%)")
    ap.add_argument("--all-dates", dest="all_dates", action="store_true", default=True,
                    help="lss記録を全日付から読む(既定ON。前夜発注の日付ズレに強い)")
    ap.add_argument("--today-only", dest="all_dates", action="store_false",
                    help="lss記録を今日の発注分だけに限定する")
    ap.add_argument("--watch", type=int, default=0,
                    help="この秒数ループして確実に取り消す(既定0=1回のみ。寄りは 240〜300 推奨)")
    ap.add_argument("--interval", type=int, default=15, help="ループ間隔秒(既定15)")
    ap.add_argument("--margin-type", type=int, default=3,
                    help="信用区分(照会のみなら影響小。既定3=一般デイトレ)")
    args = ap.parse_args()

    import backtest_limit_entry as ble
    from kabu_api import KabuClient
    from lss_exit_watcher import _load_lss_orders

    gap = args.gap if args.gap is not None else getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.02)
    today = datetime.now(JST).strftime("%Y-%m-%d")

    mode = "実取消" if args.execute else "dry-run(判定のみ)"
    env = "本番18080" if args.prod else "デモ18081"
    print(f"[cancel] モード: {mode} / 接続先: {env} / 下限ガード: -{gap*100:.1f}% "
          f"/ watch: {args.watch}s")

    lss_map = _load_lss_orders(today, args.all_dates)
    if not lss_map:
        print("[cancel] lss発注記録が見つからない → 対象なし。終了。")
        return
    print(f"[cancel] lss発注記録: {len(lss_map)}銘柄 を照合対象に")

    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    cli.margin_trade_type = args.margin_type
    try:
        cli.connect()
    except Exception as e:
        print(f"[cancel] kabu接続失敗: {e}")
        return

    done: dict = {}   # OrderId -> "sent"/"gone"
    deadline = time.time() + max(0, args.watch)
    loop = 0
    while True:
        loop += 1
        nt, ns = _sweep(cli, lss_map, gap, done, dry=not args.execute)
        gone = sum(1 for v in done.values() if v == "gone")
        print(f"  [{datetime.now(JST):%H:%M:%S} #{loop}] 深ギャップ{nt}件 / 取消送信{ns}件 "
              f"/ 取消確定{gone}件")
        if time.time() >= deadline:
            break
        time.sleep(max(5, args.interval))

    gone = sum(1 for v in done.values() if v == "gone")
    pend = sum(1 for v in done.values() if v == "sent")
    if args.execute:
        print(f"[cancel] 完了。取消確定 {gone}件 / 送信済み未確定 {pend}件"
              + ("(--watch を長くすると確定まで見届けられます)" if pend else ""))
    else:
        print("[cancel] dry-run 完了。実際に取り消すには --execute を付けてください。")


if __name__ == "__main__":
    main()
