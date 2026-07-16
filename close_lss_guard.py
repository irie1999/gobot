"""
close_lss_guard.py — lss(逆指値空売り・同日決済)の引け成行買戻しを自動化する
==============================================================================

lss は「その日の引けで買い戻して手仕舞う」同日決済戦略。エントリー
(kabu_send_lss.py / レポートのlss発注ボタン)は逆指値売りだけを出すので、
このスクリプトが引け前に走って、約定済みの lss 売建を **引け成行(MOC)買戻し**
で決済する(＝close_stop_guard の lss 版)。

【超重要・誤決済防止】
  あなたの --both 運用には「ショート」タブ(多日保有・戦略名 *_S)の売建も混じる。
  lss(同日決済)とメインショート(多日保有)はどちらも売建なので、単純に売建を
  全部買い戻すとメインショートまで即決済してしまう。そこでこのスクリプトは:

    ① 今日 lss として発注したと『記録に残る』銘柄だけを対象にする
       - ordered_signals_lss.csv (kabu_send_lss.py が記録)
       - placed_orders_<日付>.csv で side=short かつ戦略が *_S でない行
         (=レポートlssタブの発注。メインショートは *_S なので除外)
    ② かつ kabu に実際の売建がある(=約定確認)
    ③ かつ 建玉の平均約定値が lss発注価格に近い(--tol 既定8%。
       同一銘柄のメインショート建玉との取り違えを防ぐ)

  ロング(買建)とメインショート(*_S)は絶対に触らない。触らなかった建玉は
  すべて明細に「スキップ(理由)」として表示する。

【安全設計】(close_stop_guard.py と同じポリシー)
  - デフォルト dry-run。--execute のときだけ実発注。
  - --execute でも接続先は既定デモ(18081)。本番(18080)は --prod 明示必須。

【運用タイミング】毎営業日 14:50〜14:57 JST (引け直前)に実行。
  引け成行(MOC)なので 15:00 の大引けで約定する。

使い方:
  python close_lss_guard.py                    # dry-run: 買戻し対象を表示するだけ
  python close_lss_guard.py --execute          # デモ口座に引け成行買戻しを発注
  python close_lss_guard.py --execute --prod   # 本番口座 (要明示)
  python close_lss_guard.py --all-dates        # 過去日の取りこぼしlss建玉も対象
  python close_lss_guard.py --tol 0.12         # 価格一致許容を±12%に緩める
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from kabu_api import KabuClient, CASH_MARGIN_CLOSE

JST = timezone(timedelta(hours=9))
_BASE = Path(__file__).resolve().parent


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _load_lss_orders(today: str, all_dates: bool) -> dict[str, list[dict]]:
    """今日(既定)発注した lss 銘柄を symbol -> [{entry, qty, strategy, name, date}] で返す。

    ソース:
      A) ordered_signals_lss.csv       — kabu_send_lss.py が記録 (family=lss/side=short)
      B) placed_orders_<日付>.csv       — order_server(レポートlssボタン)。
                                          side=short かつ 戦略が *_S でない行のみ = lss
                                          (*_S はメインショートなので除外)
    all_dates=False なら record_date/placed_at が today の行だけ。
    """
    out: dict[str, list[dict]] = {}

    def _add(sym, entry, qty, strat, name, d):
        sym = str(sym).upper().removesuffix(".T").split(".")[0]
        if not sym:
            return
        out.setdefault(sym, []).append(
            {"entry": entry, "qty": qty, "strategy": strat, "name": name, "date": d})

    # A) ordered_signals_lss.csv
    p = _BASE / "ordered_signals_lss.csv"
    if p.exists():
        try:
            for r in csv.DictReader(open(p, encoding="utf-8")):
                if str(r.get("family", "")).strip() != "lss":
                    continue
                d = str(r.get("record_date", "")).strip()
                if not all_dates and d != today:
                    continue
                _add(r.get("symbol"), _num(r.get("order_price")),
                     int(_num(r.get("qty")) or 100), r.get("strategy", ""),
                     r.get("name", ""), d)
        except Exception as e:
            print(f"  ⚠ ordered_signals_lss.csv 読込失敗 ({e})")

    # B) placed_orders_<date>.csv (order_server の発注ログ)
    for fp in sorted(glob.glob(str(_BASE / "placed_orders_*.csv"))):
        try:
            for r in csv.DictReader(open(fp, encoding="utf-8")):
                if str(r.get("side", "")).strip() != "short":
                    continue
                strat = str(r.get("strategy", "")).strip()
                if strat.upper().endswith("_S"):
                    continue   # メインショート(多日保有) → lssではない。除外
                d = str(r.get("placed_at", "")).strip()[:10]
                if not all_dates and d != today:
                    continue
                _add(r.get("symbol"), _num(r.get("entry")),
                     int(_num(r.get("qty")) or 100), strat,
                     r.get("name", ""), d)
        except Exception:
            continue

    return out


def _match_lss(sym: str, avg_price: float, lss_map: dict[str, list[dict]],
               tol: float) -> dict | None:
    """kabu売建(sym, 平均約定値=avg_price)が lss発注記録に一致するか。
    一致すれば発注記録(dict)を返す。価格が全記録から tol 超なら None。"""
    cands = lss_map.get(sym)
    if not cands:
        return None
    if avg_price <= 0:
        return cands[0]   # 価格不明なら記録があれば採用(記録優先)
    best, best_diff = None, 1e9
    for c in cands:
        e = c["entry"]
        if e <= 0:
            if best is None:
                best, best_diff = c, tol   # 価格不明の記録は許容ぎりぎりで採用
            continue
        diff = abs(avg_price - e) / e
        if diff < best_diff:
            best, best_diff = c, diff
    if best is not None and best_diff <= tol:
        return best
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="lss(同日決済)の引け成行買戻しを自動化する")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--all-dates", action="store_true",
                    help="今日以外の日付で発注したlss建玉も対象にする(取りこぼし救済)")
    ap.add_argument("--tol", type=float, default=0.08,
                    help="建玉の平均約定値とlss発注価格の一致許容(既定0.08=±8%%)")
    args = ap.parse_args()

    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    print("=" * 66)
    print(f"lss 引け成行買戻しガード  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label}  /  接続先: {env_label}  /  信用返済(買戻し)")
    print(f"対象: {'全日付のlss建玉' if args.all_dates else '本日発注のlss建玉'}"
          f"  /  価格一致許容: ±{args.tol*100:.0f}%")
    print("=" * 66)

    # ── 今日 lss として発注した銘柄 ──
    lss_map = _load_lss_orders(today, args.all_dates)
    if not lss_map:
        print("lss発注の記録が見つかりません(ordered_signals_lss.csv / placed_orders_*.csv)。")
        print("→ 決済対象なし。終了します。")
        return 0
    _n_rec = sum(len(v) for v in lss_map.values())
    print(f"lss発注記録: {len(lss_map)}銘柄 / {_n_rec}件\n")

    # ── kabu 建玉を取得(=約定確認) ──
    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    if args.execute:
        try:
            cli.connect()
        except Exception as e:
            print(f"✗ kabu 接続失敗: {e}")
            return 1
        try:
            positions = cli.get_positions(product=2)   # 信用建玉
        except Exception as e:
            print(f"✗ 建玉取得失敗: {e}")
            return 1
    else:
        # dry-run: kabu に接続せず、記録から擬似建玉を作って挙動を見せる
        print("(dry-run: kabuに接続しないため、lss発注記録を『約定済み売建』とみなして"
              "買戻し内容をプレビューします)")
        positions = [{
            "Symbol": s, "Side": "1", "LeavesQty": c[0]["qty"],
            "AveragePrice": c[0]["entry"], "SymbolName": c[0]["name"],
            "ExecutionID": "(dry-run)",
        } for s, c in lss_map.items()]

    # ── 買戻し対象を選別 ──
    to_close: list[dict] = []
    skipped: list[str] = []
    for kp in positions:
        sym = str(kp.get("Symbol", "")).upper().removesuffix(".T").split(".")[0]
        side = str(kp.get("Side", ""))
        leaves = int(kp.get("LeavesQty") or kp.get("Qty") or 0)
        name = (kp.get("SymbolName") or kp.get("Name") or sym)
        if leaves <= 0:
            continue
        if side != "1":
            # 買建(ロング)は絶対に触らない
            skipped.append(f"  ・{sym} {name}: 買建(ロング) {leaves}株 → スキップ(対象外)")
            continue
        avg = _num(kp.get("AveragePrice") or kp.get("Price"))
        rec = _match_lss(sym, avg, lss_map, args.tol)
        if rec is None:
            # 売建だが lss記録に無い/価格不一致 = メインショート(*_S)等 → 触らない
            skipped.append(
                f"  ・{sym} {name}: 売建 {leaves}株 平均{avg:,.0f} "
                f"→ スキップ(lss発注記録に一致なし=メインショート等)")
            continue
        hold_id = str(kp.get("ExecutionID") or kp.get("HoldID") or "").strip()
        to_close.append({
            "sym": sym, "name": name, "qty": leaves, "avg": avg,
            "hold_id": hold_id, "strategy": rec.get("strategy", ""),
            "entry": rec.get("entry", 0),
        })

    # ── スキップ内訳(慎重確認用) ──
    if skipped:
        print("── 触らない建玉(ロング/メインショート) ──")
        for s in skipped:
            print(s)
        print()

    if not to_close:
        print("買戻し対象の lss 建玉はありません。終了します。")
        return 0

    print(f"── 引け成行で買戻す lss 建玉: {len(to_close)}件 ──")
    ok = 0
    for t in to_close:
        cp = ([{"HoldID": t["hold_id"], "Qty": t["qty"]}]
              if (t["hold_id"] and t["hold_id"] != "(dry-run)") else None)
        print(f"  ▸ {t['sym']} {t['name']} [{t['strategy']}] "
              f"売建{t['qty']}株 平均{t['avg']:,.0f}(発注{t['entry']:,.0f}) "
              f"→ 引け成行買戻し")
        res = cli.send_moc(t["sym"], qty=t["qty"], side="buy",
                           cash_margin=CASH_MARGIN_CLOSE, close_positions=cp)
        if (res.get("Result") == 0) or res.get("_dry_run"):
            ok += 1

    print(f"\n引け成行買戻し発注: {ok}/{len(to_close)} 件成功")
    if not args.execute:
        print("dry-run のため実発注していません。内容が正しければ --execute で発注します。")
    else:
        print("※ MOC(引け成行)は 15:00 の大引けで約定します。約定は kabu で確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
