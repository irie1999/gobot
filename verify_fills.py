"""verify_fills.py — kabuの実注文/実約定(get_orders)を取得して当日の取引を集計する。

2つのセクションを出力:
  ① 全注文一覧 — 対象日に出した注文をすべて表示(約定した/しなかった を問わず)。
     注文株数・注文値・状態(全約定/一部約定/未約定/取消・失効/期限切れ)・約定株数/値/時刻。
     → 「注文は出したが約定しなかった」ものが一目で分かる。
  ② 結果 — ①のうち 約定して決済(買戻し)まで済んだ往復取引の実損益・約定時刻。
     レポート/バックテストの想定値と突き合わせて実運用の乖離(§18.7)を測れる。

使い方(あなたの機械・本番口座 / KABU_API_PASSWORD 設定済み):
  python verify_fills.py --prod                    # 今日の全注文+結果
  python verify_fills.py --prod --date 20260728    # 指定日
  python verify_fills.py --prod --orders-csv orders.csv  # 全注文一覧をCSV保存
  python verify_fills.py --prod --csv fills.csv     # 往復損益サマリーをCSV保存
  python verify_fills.py --prod --expected signals_expected.csv  # 想定値と乖離比較
  python verify_fills.py --prod --no-date          # 日付で絞らず全注文

照会のみ(発注しない)。発注サーバ/watcher 稼働中は 401(トークン取り合い)になることが
あるので、その時は片方を一瞬止めるか少し待って再実行。
"""
from __future__ import annotations
import argparse
import csv as _csv
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from kabu_api import KabuClient

try:
    from backtest_limit_entry import FEE_PCT_ONE_WAY
except Exception:
    FEE_PCT_ONE_WAY = 0.001

_JST = timezone(timedelta(hours=9))

ap = argparse.ArgumentParser(description="kabu実約定を集計(実損益・約定時刻)し想定と比較")
ap.add_argument("--prod", action="store_true", help="本番(18080)。未指定はデモ(18081)")
ap.add_argument("--date", type=str, default=None, help="対象日 yyyyMMdd(既定=今日JST)")
ap.add_argument("--csv", type=str, default=None, help="実約定サマリー(往復損益)のCSV保存先")
ap.add_argument("--orders-csv", type=str, default=None,
                help="全注文一覧(未約定・取消含む)のCSV保存先")
ap.add_argument("--expected", type=str, default=None,
                help="想定値CSV(列: symbol,entry,exit,pnl)。あれば乖離を並べて表示")
ap.add_argument("--fee", type=float, default=0.0, help="片道手数料率(既定=0。信用大口優遇プランは手数料無料)")
ap.add_argument("--debug", action="store_true",
                help="約定が0件のとき等、生の注文/Details構造を先頭数件ダンプして原因調査")
ap.add_argument("--no-date", action="store_true", help="日付で絞らず全約定を集計")
args = ap.parse_args()

FEE = args.fee
_DATE = args.date or datetime.now(_JST).strftime("%Y%m%d")


def _digits(s) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


_DATE_DIG = _digits(_DATE)


def _match_date(t: str) -> bool:
    """約定時刻 t が対象日か。ISO(2026-07-28T..)/yyyyMMddHHMMSS どちらでも桁で判定。
    時刻が空(取れない)場合は除外しない(=今日扱い)。--no-date で常にTrue。"""
    if args.no_date:
        return True
    dt = _digits(t)
    if not dt:
        return True
    return _DATE_DIG in dt


def _exec_time(d: dict) -> str:
    """約定時刻。ExecutionDay(yyyyMMddHHMMSS)優先、無ければ TransactTime。"""
    for k in ("ExecutionDay", "TransactTime", "RecvTime"):
        v = d.get(k)
        if v:
            return str(v)
    return ""


def _executions(o: dict):
    """1注文の約定(RecType=8)明細を [(price, qty, time)] で返す。"""
    out = []
    for d in (o.get("Details") or []):
        if int(d.get("RecType") or 0) != 8:      # 8=約定
            continue
        px = float(d.get("Price") or 0)
        qty = float(d.get("Qty") or 0)
        if px > 0 and qty > 0:
            out.append((px, qty, _exec_time(d)))
    return out


def _order_date(o: dict) -> str:
    """注文を出した日時。RecvTime 優先、無ければ Details の最初の受付/約定時刻。"""
    for k in ("RecvTime", "TransactTime"):
        v = o.get(k)
        if v:
            return str(v)
    for d in (o.get("Details") or []):
        t = _exec_time(d)
        if t:
            return t
    return ""


def _order_price(o: dict) -> float:
    """注文価格。通常は Price。逆指値(ReverseLimitOrder)なら TriggerPrice を拾う。"""
    p = float(o.get("Price") or 0)
    if p > 0:
        return p
    rl = o.get("ReverseLimitOrder")
    if isinstance(rl, dict):
        for k in ("TriggerPrice", "Price", "AfterHitPrice"):
            v = float(rl.get(k) or 0)
            if v > 0:
                return v
    return 0.0


def _order_status(o: dict) -> str:
    """注文の状態を日本語で返す。約定株数(CumQty)と注文株数(OrderQty)・State から判定。
    kabu State: 1=待機 2=処理中 3=処理済 4=訂正取消送信中 5=終了。
    CumQty=0 で終了なら未約定のまま失効/取消。"""
    oq = float(o.get("OrderQty") or 0)
    cq = float(o.get("CumQty") or 0)
    state = int(o.get("State") or o.get("OrderState") or 0)
    if oq > 0 and cq >= oq:
        return "全約定"
    if cq > 0:
        return f"一部約定({int(cq)}/{int(oq)})"
    # ここから未約定(CumQty=0)
    rectypes = {int(d.get("RecType") or 0) for d in (o.get("Details") or [])}
    if 6 in rectypes:
        return "取消/失効"
    if 3 in rectypes:
        return "期限切れ"
    if state == 5:
        return "未約定(終了)"
    return "未約定(有効)"


def _hhmm(t: str) -> str:
    """約定時刻文字列から HH:MM を抜く(表示用)。yyyyMMddHHMMSS / ISO どちらも対応。"""
    s = str(t)
    if len(s) >= 14 and s[:8].isdigit():          # yyyyMMddHHMMSS
        return f"{s[8:10]}:{s[10:12]}"
    if "T" in s and len(s) >= 16:                  # ISO 2026-07-28T10:03:...
        return s[11:16]
    return s[-8:-3] if len(s) >= 8 else s


def _load_expected(path):
    exp = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for r in _csv.DictReader(f):
                sym = str(r.get("symbol") or r.get("code") or "").split(".")[0]
                if sym:
                    exp[sym] = {k: float(r.get(k) or 0) for k in ("entry", "exit", "pnl")
                                if r.get(k) not in (None, "")}
    except Exception as e:
        print(f"[warn] 想定CSV読込失敗 {path}: {e}")
    return exp


def main():
    cli = KabuClient(prod=args.prod, dry_run=True)
    cli.connect()
    orders = cli.get_orders()
    print(f"[取得] 全注文 {len(orders)}件 / 接続先 {'本番18080' if args.prod else 'デモ18081'} "
          f"/ 対象日 {_DATE}\n")

    if args.debug:
        import json as _json
        # 約定がありそうな注文(CumQty>0 か Detailsに約定record)を優先して数件ダンプ
        cand = [o for o in orders if float(o.get("CumQty") or 0) > 0] or orders
        print("=== [debug] 先頭注文の生構造(RecType/価格/時刻フィールドの確認用) ===")
        for o in cand[:3]:
            slim = {k: o.get(k) for k in ("ID", "Symbol", "SymbolName", "Side",
                                          "State", "OrderState", "OrderQty", "CumQty",
                                          "Price", "RecvTime")}
            print(_json.dumps(slim, ensure_ascii=False))
            for d in (o.get("Details") or []):
                print("   Detail:", _json.dumps(d, ensure_ascii=False))
        print("=== [debug] ここまで ===\n")

    # ── 全注文一覧(対象日に出した注文をすべて。未約定・取消・失効も含む) ──────────
    # 「注文は出したが約定しなかった」ものも見えるように、注文単位で状態を表示する。
    names = {}
    order_rows = []
    for o in orders:
        if not (args.no_date or _match_date(_order_date(o))):
            continue
        sym = str(o.get("Symbol") or "").split(".")[0]
        names[sym] = str(o.get("SymbolName") or "")
        side = "売" if str(o.get("Side")) == "1" else "買"
        oq = int(float(o.get("OrderQty") or 0))
        cq = int(float(o.get("CumQty") or 0))
        # 約定時刻(あれば):この注文の約定明細の最初の時刻
        _ex = [t for _, _, t in _executions(o)]
        fill_t = _hhmm(min(_ex)) if _ex else "—"
        # 約定値(あれば):約定明細の平均
        _exf = [(px, q) for px, q, _ in _executions(o)]
        fill_p = (sum(px * q for px, q in _exf) / sum(q for _, q in _exf)) if _exf else 0.0
        order_rows.append({
            "code": sym, "name": names.get(sym, ""), "side": side,
            "order_qty": oq, "order_price": round(_order_price(o), 1),
            "status": _order_status(o), "cum_qty": cq,
            "fill_price": round(fill_p, 1), "fill_time": fill_t,
            "recv": _hhmm(_order_date(o)),
        })

    order_rows.sort(key=lambda r: (r["code"], r["side"]))
    _n_all = len(order_rows)
    _n_fill = sum(1 for r in order_rows if r["cum_qty"] > 0)
    _n_nofill = _n_all - _n_fill
    print(f"=== 全注文一覧 (対象日 {_DATE} に出した注文: {_n_all}件 / "
          f"約定 {_n_fill}件・未約定 {_n_nofill}件) ===")
    print(f"{'コード':>6} {'銘柄':<12}{'売買':>4}{'注文株':>7}{'注文値':>9}"
          f"{'発注':>6}{'状態':>14}{'約定株':>7}{'約定値':>9}{'約定':>6}")
    for r in order_rows:
        _fp = f"{r['fill_price']:,.1f}" if r['fill_price'] else "—"
        print(f"{r['code']:>6} {r['name'][:12]:<12}{r['side']:>4}{r['order_qty']:>7}"
              f"{r['order_price']:>9,.1f}{r['recv']:>6}{r['status']:>14}"
              f"{r['cum_qty']:>7}{_fp:>9}{r['fill_time']:>6}")
    if not order_rows:
        print("  (対象日に出した注文が見つかりませんでした。--no-date で全期間、--debug で生構造を確認)")
    if args.orders_csv and order_rows:
        with open(args.orders_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=list(order_rows[0].keys()))
            w.writeheader()
            w.writerows(order_rows)
        print(f"[出力] 全注文一覧 → {args.orders_csv}")
    print()

    # 銘柄×売買 で約定を集約(対象日のみ)
    sells = defaultdict(lambda: {"qty": 0.0, "notional": 0.0, "times": []})
    buys = defaultdict(lambda: {"qty": 0.0, "notional": 0.0, "times": []})
    for o in orders:
        sym = str(o.get("Symbol") or "").split(".")[0]
        names[sym] = str(o.get("SymbolName") or "")
        side = str(o.get("Side"))            # "1"=売 "2"=買
        for px, qty, t in _executions(o):
            if not _match_date(t):
                continue
            book = sells if side == "1" else buys
            book[sym]["qty"] += qty
            book[sym]["notional"] += px * qty
            book[sym]["times"].append(t)

    exp = _load_expected(args.expected) if args.expected else {}

    rows = []
    tot_net = 0.0
    for sym in sorted(set(sells) | set(buys)):
        s, b = sells.get(sym), buys.get(sym)
        avg_sell = (s["notional"] / s["qty"]) if (s and s["qty"]) else 0.0
        avg_buy = (b["notional"] / b["qty"]) if (b and b["qty"]) else 0.0
        qty = min(s["qty"] if s else 0, b["qty"] if b else 0)
        et = _hhmm(min(s["times"])) if (s and s["times"]) else "—"   # 売り=エントリー時刻
        xt = _hhmm(max(b["times"])) if (b and b["times"]) else "—"   # 買戻し=決済時刻
        if qty > 0:                                # 往復完了(lssショート: 売り→買戻し)
            gross = (avg_sell - avg_buy) * qty
            fee = (avg_sell + avg_buy) * qty * FEE
            net = gross - fee
            pct = (avg_sell - avg_buy) / avg_sell * 100 if avg_sell else 0.0
            tot_net += net
        else:                                      # 片側のみ(未決済/データ欠)
            net = pct = 0.0
        rows.append({"symbol": sym, "name": names.get(sym, ""), "qty": int(qty),
                     "entry(売)": round(avg_sell, 1), "exit(買戻)": round(avg_buy, 1),
                     "entry_t": et, "exit_t": xt, "pnl": round(net, 0), "pct": round(pct, 2)})

    # 表示
    print("=== 結果 (上の注文のうち 約定して決済まで済んだ取引の実損益) ===")
    print(f"{'コード':>6} {'銘柄':<12}{'株数':>5}{'実売り':>9}{'実買戻':>9}"
          f"{'約定':>6}{'決済':>6}{'実損益':>10}{'%':>7}" + ("   |想定損益  乖離" if exp else ""))
    for r in rows:
        line = (f"{r['symbol']:>6} {r['name'][:12]:<12}{r['qty']:>5}"
                f"{r['entry(売)']:>9,.1f}{r['exit(買戻)']:>9,.1f}"
                f"{r['entry_t']:>6}{r['exit_t']:>6}{r['pnl']:>+10,.0f}{r['pct']:>+6.2f}%")
        if exp:
            e = exp.get(r["symbol"], {})
            ep = e.get("pnl")
            if ep is not None:
                line += f"   |{ep:>+9,.0f} {r['pnl']-ep:>+9,.0f}"
        print(line)

    print(f"\n[実損益 合計] {tot_net:+,.0f}円  (往復完了 {sum(1 for r in rows if r['qty']>0)}銘柄)")
    if exp:
        matched = [(r, exp[r["symbol"]]) for r in rows
                   if r["symbol"] in exp and "pnl" in exp[r["symbol"]] and r["qty"] > 0]
        if matched:
            d_pnl = sum(r["pnl"] - e["pnl"] for r, e in matched)
            e_tot = sum(e["pnl"] for _, e in matched)
            print(f"[想定 合計] {e_tot:+,.0f}円  /  実−想定(乖離) {d_pnl:+,.0f}円  "
                  f"({'実の方が良い' if d_pnl>=0 else '実の方が悪い=劣化'})")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                                ["symbol", "name", "qty", "entry(売)", "exit(買戻)",
                                 "entry_t", "exit_t", "pnl", "pct"])
            w.writeheader()
            w.writerows(rows)
        print(f"\n[出力] {args.csv}")


if __name__ == "__main__":
    main()
