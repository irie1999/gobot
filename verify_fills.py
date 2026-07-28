"""verify_fills.py — kabuの実約定(get_orders の Details, RecType=8=約定)を取得し、
銘柄別に「実際の売り約定(lssエントリー)/買戻し(決済)/実損益/約定時刻」を集計する。

これで実運用の乖離(実スリッページ・実現損益・約定時刻)をデータで測れる(§18.7)。
レポート/バックテストの想定値(約定値・決済値・損益)と突き合わせて比較する土台。

使い方(あなたの機械・本番口座 / KABU_API_PASSWORD 設定済み):
  python verify_fills.py --prod                    # 今日の実約定を集計
  python verify_fills.py --prod --date 20260728    # 指定日(ExecutionDayで絞る)
  python verify_fills.py --prod --csv fills.csv     # CSV保存
  python verify_fills.py --prod --expected signals_expected.csv  # 想定値CSVと乖離比較

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
ap.add_argument("--csv", type=str, default=None, help="実約定サマリーのCSV保存先")
ap.add_argument("--expected", type=str, default=None,
                help="想定値CSV(列: symbol,entry,exit,pnl)。あれば乖離を並べて表示")
ap.add_argument("--fee", type=float, default=None, help="片道手数料率(既定=FEE_PCT_ONE_WAY)")
ap.add_argument("--debug", action="store_true",
                help="約定が0件のとき等、生の注文/Details構造を先頭数件ダンプして原因調査")
ap.add_argument("--no-date", action="store_true", help="日付で絞らず全約定を集計")
args = ap.parse_args()

FEE = FEE_PCT_ONE_WAY if args.fee is None else args.fee
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

    # 銘柄×売買 で約定を集約(対象日のみ)
    sells = defaultdict(lambda: {"qty": 0.0, "notional": 0.0, "times": []})
    buys = defaultdict(lambda: {"qty": 0.0, "notional": 0.0, "times": []})
    names = {}
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
