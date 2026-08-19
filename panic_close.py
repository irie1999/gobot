"""
panic_close.py — 一般信用(デイトレ)の売建を『引け成行(MOC)』で必ず閉じる保険
================================================================================

⛔⛔ **これは戦略ではない。事故の最終防衛線。**

2026-08-19、lss_exit_watcher が naive/aware の比較1つ(TypeError)で起動2秒で
落ち、誰も再起動しなかったため 8建玉が終日ノーガードのまま持ち越された。
実損は「無防備だったこと」ではなく **引け決済が飛んだこと** で出た:

    強制決済手数料 2,200円/銘柄 × 8 = 17,600円  +  夜間ギャップの全リスク

一般信用(デイトレ)は **当日中の反対売買が前提**の建玉で、返済期日が当日。
翌営業日を期限とする注文(寄成)は Code45『注文期日指定が返済期日を超過』で
弾かれるため、**大引けを過ぎたら自分では閉じられない**。

★ ところが 引成(MOC / FrontOrderType=16) は **朝のうちに出しておいても
  大引けで約定する**。つまり「決済のために生きたプロセスが要る」という前提は
  引け決済に限れば不要で、**注文を板に置いてしまえばプロセスは死んでよい**。

このスクリプトはそれをやる。watcher が動かせないと分かった時点で走らせれば、
最悪でも『その日の引けで閉じる』ところまでは保証される。

対象の絞り方 (ここが安全性の要)
--------------------------------
  ・Side = 1 (売建) のみ。買建(ロング)には一切触らない
  ・MarginTradeType = 3 (**一般信用デイトレ**) のみ
      → この区分は定義上「当日中に閉じなければならない」建玉なので、
        閉じることが常に正しい。制度信用(1)・一般長期(2)は多日保有が
        ありうるので **絶対に触らない**
  ・LeavesQty > 0 (残玉があるものだけ)

  発注記録(ordered_signals_lss.csv)との突合は **しない**。記録が壊れている
  ときこそ動いてほしい保険なので、記録に依存させない。区分だけで判定する。

安全設計 (他のスクリプトと同じポリシー)
----------------------------------------
  ・既定 dry-run。--execute のときだけ実発注
  ・--execute でも接続先は既定デモ(18081)。本番(18080)は --prod 明示必須
  ・既存の返済注文は取り消してから出す(建玉拘束 4001005 を避ける)
    → **損切り逆指値も消える**。引けで必ず閉じることを優先する判断

使い方
------
  python panic_close.py                     # dry-run(何が対象か見るだけ)
  python panic_close.py --prod              # 本番口座を見る (照会のみ)
  python panic_close.py --prod --execute    # ★実発注(引成で買戻し)
  python panic_close.py --prod --execute --immediate   # 引成でなく即時成行

⚠ **watcher と同時に走らせないこと**(kabu の有効トークンは1つ)。
   morning_test は watcher の再起動をあきらめた後にだけ呼ぶ。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta

from kabu_api import KabuClient, CASH_MARGIN_CLOSE

JST = timezone(timedelta(hours=9))

# 一般信用(デイトレ)。この区分だけが「当日返済が前提」なので対象にできる。
MARGIN_DAYTRADE = "3"
FEE_PER_SYMBOL = 2200          # 強制決済手数料(税込) / 銘柄。持ち越したときの実費


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def select_targets(positions: list[dict], margin_types: set[str]) -> list[dict]:
    """閉じるべき建玉を選ぶ。**ここを緩めないこと**(誤決済の入口)。"""
    out = []
    for p in positions:
        if str(p.get("Side", "")) != "1":          # 売建のみ
            continue
        qty = int(p.get("LeavesQty") or p.get("Qty") or 0)
        if qty <= 0:                               # 決済済み
            continue
        if str(p.get("MarginTradeType") or "") not in margin_types:
            continue
        out.append({
            "sym": str(p.get("Symbol", "")).upper().removesuffix(".T"),
            "name": p.get("SymbolName") or "",
            "qty": qty,
            "avg": _num(p.get("AveragePrice") or p.get("Price")),
            "mt": str(p.get("MarginTradeType") or ""),
        })
    # 同一銘柄は合算する。kabu の信用返済は銘柄単位で建玉を自動選択し、
    # 発注時に既存の返済注文を取り消すので、建玉ごとに出すと片方が消える。
    agg: dict = {}
    for t in out:
        a = agg.get(t["sym"])
        if a is None:
            agg[t["sym"]] = dict(t)
        else:
            a["qty"] += t["qty"]
    return list(agg.values())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="一般信用(デイトレ)の売建を引け成行で必ず閉じる保険")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--immediate", action="store_true",
                    help="引成(既定)ではなく即時成行で閉じる")
    ap.add_argument("--include-all-margin", action="store_true",
                    help="⛔ 制度信用・一般長期の売建も対象にする(既定OFF)。"
                         "多日保有のショートを誤決済しうるので、"
                         "本当に全部閉じたいときだけ")
    args = ap.parse_args()

    _mt = ({"1", "2", "3"} if args.include_all_margin else {MARGIN_DAYTRADE})
    now = datetime.now(JST)
    print("=" * 74)
    print(f"panic_close — デイトレ売建の強制クローズ  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {'★実発注★' if args.execute else 'dry-run (発注なし)'}"
          f"  /  接続先: {'本番(18080)' if args.prod else 'デモ(18081)'}")
    print(f"決済方法: {'即時成行' if args.immediate else '引け成行(MOC / 大引けで約定)'}")
    print(f"対象区分: {'制度+一般長期+デイトレ (⛔全部)' if args.include_all_margin else '一般信用(デイトレ)のみ'}")
    print("=" * 74)

    cli = KabuClient(prod=args.prod, dry_run=not args.execute,
                     margin_type=int(MARGIN_DAYTRADE))
    try:
        cli.connect()
    except Exception as e:
        print(f"⛔ kabu 接続失敗: {e}")
        print("  → kabuステーションが起動・ログイン済みか確認してください。")
        return 1

    try:
        positions = cli.get_positions(product=2)     # 信用建玉
    except Exception as e:
        print(f"⛔ 建玉取得失敗: {e}")
        return 1

    targets = select_targets(positions, _mt)
    if not targets:
        print("\n✅ 対象の売建はありません。何もしません。")
        return 0

    _amt = sum(t["avg"] * t["qty"] for t in targets)
    print(f"\n対象 {len(targets)}銘柄 / 建玉総額 約 {_amt:,.0f}円")
    print(f"  ⚠ このまま持ち越すと強制決済手数料 "
          f"{len(targets) * FEE_PER_SYMBOL:,}円 "
          f"({FEE_PER_SYMBOL:,}円 × {len(targets)}銘柄) がかかります")
    for t in targets:
        print(f"    {t['sym']} {t['name']} 売建{t['qty']}株 平均{t['avg']:,.1f}")

    if not args.execute:
        print("\n⛔ dry-run です。実際に閉じるには --execute を付けてください。")
        print("   本番口座なら --prod も必要です。")
        return 0

    print(f"\n── 買戻し発注 ──")
    ok = 0
    for t in targets:
        # 建玉の信用区分に合わせないと Code 8。対象は区分で絞ってあるので揃う。
        cli.margin_trade_type = int(t["mt"] or MARGIN_DAYTRADE)
        try:
            if args.immediate:
                # 既存の返済注文を取り消してから成行(建玉拘束を避ける)
                cli.cancel_open_close_orders(t["sym"], side="2")
                res = cli.send_buy(t["sym"], qty=t["qty"], order_type="market",
                                   cash_margin=CASH_MARGIN_CLOSE)
            else:
                # send_moc は内部で cancel_open_close_orders を呼ぶ
                res = cli.send_moc(t["sym"], qty=t["qty"], side="buy",
                                   cash_margin=CASH_MARGIN_CLOSE)
        except Exception as e:
            print(f"  ⛔ {t['sym']} 発注で例外: {e}")
            continue
        if res.get("Result") == 0 or res.get("_dry_run"):
            ok += 1
            print(f"  ✅ {t['sym']} {t['name']} {t['qty']}株 買戻し発注")
        else:
            print(f"  ⛔ {t['sym']} 発注失敗: {res}")

    print(f"\n買戻し発注: {ok}/{len(targets)} 件成功")
    if ok < len(targets):
        print("⛔⛔ **失敗した銘柄は手で返済してください**。"
              "デイトレ信用は当日返済が前提です。")
        return 1
    if not args.immediate:
        print("※ 引成(MOC)は 15:25-15:30 のクロージング・オークションで約定します。")
        print("  ⚠ 15:24 を過ぎてから出すと間に合いません。その場合は --immediate を。")
    print("⚠ 約定は kabuステーションの注文照会で必ず確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
