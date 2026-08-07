"""verify_fills.py — kabuの実注文/実約定(get_orders)を取得して当日の取引を集計する。

2つのセクションを出力:
  ① 全注文一覧 — 対象日に出した注文をすべて表示(約定した/しなかった を問わず)。
     注文株数・注文値・状態(全約定/一部約定/未約定/取消・失効/期限切れ)・約定株数/値/時刻。
     → 「注文は出したが約定しなかった」ものが一目で分かる。
  ② 結果 — ①のうち 約定して決済(買戻し)まで済んだ往復取引の実損益・約定時刻。
     レポート/バックテストの想定値と突き合わせて実運用の乖離(§18.7)を測れる。

使い方(あなたの機械・本番口座 / KABU_API_PASSWORD 設定済み):
  .\fills                                          # ← 日常はこれ (= --prod --save)
  .\fills --date 20260728                          # 指定日

  python verify_fills.py --prod                    # 今日の全注文+結果
  python verify_fills.py --prod --save             # CSV2種を日付つきで自動保存
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
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
ap.add_argument("--trades-csv", type=str,
                default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"),
                help="バックテスト(レポート)の全取引CSV。対象日の取引を実約定と並べて比較する。"
                     "既定 lss_trades.csv。--no-compare で比較をスキップ")
ap.add_argument("--no-compare", action="store_true",
                help="バックテストとの比較セクションを出さない")
ap.add_argument("--save", action="store_true",
                help="CSV2種を日付つきファイル名で自動保存 "
                     "(fills_<日付>.csv / orders_<日付>.csv)。--csv/--orders-csv より優先度低")
ap.add_argument("--slip-log", type=str, default="slip_daily_log.csv",
                help="実約定とテストの乖離を1日1行で貯める累積ログ(既定 slip_daily_log.csv)。"
                     "同じ日を再実行してもその行が上書きされるだけで重複しない")
ap.add_argument("--no-slip-log", action="store_true",
                help="累積ログを書かない(表示のみ)")
args = ap.parse_args()

FEE = args.fee
_DATE = args.date or datetime.now(_JST).strftime("%Y%m%d")

# --save: 明示指定が無い側だけ日付つきの既定名を割り当てる
if args.save:
    _d = _DATE if len(str(_DATE)) == 8 else datetime.now(_JST).strftime("%Y%m%d")
    if not args.csv:
        args.csv = f"fills_{_d}.csv"
    if not args.orders_csv:
        args.orders_csv = f"orders_{_d}.csv"


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
                                          "Price", "RecvTime", "ExpireDay", "ExpireDate")}
            print(_json.dumps(slim, ensure_ascii=False))
            for d in (o.get("Details") or []):
                print("   Detail:", _json.dumps(d, ensure_ascii=False))
        print("=== [debug] ここまで ===\n")

    # ── 全注文一覧(対象日に出した注文をすべて。未約定・取消・失効も含む) ──────────
    # 「注文は出したが約定しなかった」ものも見えるように、注文単位で状態を表示する。
    names = {}
    order_rows = []
    for o in orders:
        # 対象日に「有効だった」注文を全部拾う。判定は3通りのOR:
        #   ① 注文日(RecvTime)が対象日
        #   ② 対象日に約定している
        #   ③ 有効期限(ExpireDay)が対象日  ← 前日夜に発注して翌営業日を期限にした注文
        # ③が無いと、前日夜に出して当日ずっと約定しなかった注文が丸ごと漏れ、
        # 約定率が100%と誤表示される(実測: 2026-08-06)。
        if not args.no_date:
            _ok = _match_date(_order_date(o))
            if not _ok:
                _ok = any(_match_date(t) for _, _, t in _executions(o))
            if not _ok:
                _exp = _digits(o.get("ExpireDay") or o.get("ExpireDate") or "")
                _ok = bool(_exp) and _exp == _DATE_DIG
            if not _ok:
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
    # 売り(エントリー)だけの内訳も出す。lssの約定率はこちらが本体。
    _s_all = [r for r in order_rows if r["side"] == "売"]
    _s_fill = sum(1 for r in _s_all if r["cum_qty"] > 0)
    print(f"=== 全注文一覧 (対象日 {_DATE} に有効だった注文: {_n_all}件 / "
          f"約定 {_n_fill}件・未約定 {_n_nofill}件) ===")
    if _s_all:
        print(f"  うち売り(エントリー): {len(_s_all)}件 / 約定 {_s_fill}件・"
              f"未約定 {len(_s_all) - _s_fill}件  → 約定率 "
              f"{_s_fill / len(_s_all) * 100:.1f}%")
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

    # ── ③ バックテスト(レポート)の同日取引と突合 ───────────────────────────
    if not args.no_compare:
        _compare_with_backtest(rows, order_rows)


def _bt_trades_for_date(path: str, ymd: str) -> list[dict]:
    """lss_trades.csv から対象日(entry_date)の取引を読む。無ければ空リスト。"""
    p = Path(path)
    if not p.exists():
        return []
    want = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    out = []
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in _csv.DictReader(f):
                d = str(r.get("entry_date") or "")[:10]
                if d != want:
                    continue
                try:
                    out.append({
                        "symbol": str(r.get("symbol", "")).split(".")[0],
                        "name": str(r.get("name", "")),
                        "strategy": str(r.get("strategy", "")),
                        "bt": float(r.get("bt", 0) or 0),
                        "entry_p": float(r.get("entry_p", 0) or 0),
                        "exit_p": float(r.get("exit_p", 0) or 0),
                        "reason": str(r.get("reason", "")),
                        "pnl": float(r.get("pnl", 0) or 0),
                        "qty": int(float(r.get("qty", 0) or 0)),
                    })
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _compare_with_backtest(real_rows: list, order_rows: list) -> None:
    """実約定 vs バックテスト(レポート)の同日取引を並べて比較する。"""
    bt = _bt_trades_for_date(args.trades_csv, _DATE_DIG)
    print()
    print("=" * 78)
    if not bt:
        print(f"=== バックテスト比較: スキップ ===")
        print(f"  {args.trades_csv} に {_DATE} の取引が見つかりません。")
        print(f"  daily.bat / dailyfast.bat は LSS_TRADES_CSV=lss_trades.csv を既定で設定します。")
        print(f"  出ていない場合は git pull してから .\\daily を1回流してください")
        print(f"  (対象日の取引はレポート生成時点までしか入りません。当日ぶんが要るなら"
              f"引け後に .\\daily → .\\fills の順で)。")
        return

    # 同一銘柄が複数戦略で出た場合はBT最高の1件に統合(実運用=1銘柄1ポジション)
    by_sym: dict = {}
    for t in bt:
        e = by_sym.get(t["symbol"])
        if e is None or t["bt"] > e["bt"]:
            by_sym[t["symbol"]] = t

    print(f"=== バックテスト(レポート)の {_DATE} 取引 : {len(by_sym)}銘柄 ===")
    print(f"{'コード':>6} {'銘柄':<12}{'戦略':>8}{'BT':>5}{'約定値':>9}{'決済値':>9}"
          f"{'株数':>5}{'損益':>10}  理由")
    _bt_tot = 0.0
    for s in sorted(by_sym):
        t = by_sym[s]
        _bt_tot += t["pnl"]
        print(f"{s:>6} {t['name'][:12]:<12}{t['strategy']:>8}{t['bt']:>5.0f}"
              f"{t['entry_p']:>9,.1f}{t['exit_p']:>9,.1f}{t['qty']:>5}"
              f"{t['pnl']:>+10,.0f}  {t['reason']}")
    _bt_w = sum(1 for t in by_sym.values() if t["pnl"] > 0)
    print(f"\n[テスト 合計] {_bt_tot:+,.0f}円  ({len(by_sym)}銘柄 / 勝ち{_bt_w} 負け{len(by_sym)-_bt_w})")

    # ── 突合 ──
    real_done = {r["symbol"]: r for r in real_rows if r["qty"] > 0}
    ordered = {r["code"] for r in order_rows if r.get("side") == "売"}
    both = sorted(set(real_done) & set(by_sym))
    real_only = sorted(set(real_done) - set(by_sym))
    bt_only = sorted(set(by_sym) - set(real_done))

    print()
    print("=" * 78)
    print("=== 突合: 実約定 vs テスト ===")
    if both:
        print(f"\n▼ 両方にある {len(both)}銘柄 (これが本当の乖離)")
        print(f"{'コード':>6} {'銘柄':<12}{'実約定値':>10}{'テスト':>9}{'滑り':>8}"
              f"{'実決済':>10}{'テスト':>9}{'実損益':>10}{'テスト':>10}{'差':>10}")
        _d_tot = 0.0
        for s in both:
            r, t = real_done[s], by_sym[s]
            slip = ((r["entry(売)"] - t["entry_p"]) / t["entry_p"] * 100
                    if t["entry_p"] else 0.0)
            d = r["pnl"] - t["pnl"]
            _d_tot += d
            print(f"{s:>6} {t['name'][:12]:<12}{r['entry(売)']:>10,.1f}{t['entry_p']:>9,.1f}"
                  f"{slip:>+7.2f}%{r['exit(買戻)']:>10,.1f}{t['exit_p']:>9,.1f}"
                  f"{r['pnl']:>+10,.0f}{t['pnl']:>+10,.0f}{d:>+10,.0f}")
        print(f"{'計':>6} {'':<12}{'':>10}{'':>9}{'':>8}{'':>10}{'':>9}"
              f"{sum(real_done[s]['pnl'] for s in both):>+10,.0f}"
              f"{sum(by_sym[s]['pnl'] for s in both):>+10,.0f}{_d_tot:>+10,.0f}")

    if bt_only:
        # BT降順(=発注優先順)で並べる。件数が多いので上位だけ出して残りは要約。
        _lst = sorted(bt_only, key=lambda s: -by_sym[s]["bt"])
        _o = sum(1 for s in _lst if s in ordered)
        _n = len(_lst) - _o
        _p = sum(by_sym[s]["pnl"] for s in _lst)
        print(f"\n▼ テストにあるが実約定なし {len(_lst)}銘柄 (BT降順=発注優先順)")
        _SHOW = 15
        for s in _lst[:_SHOW]:
            t = by_sym[s]
            tag = "発注済(未約定)" if s in ordered else "発注していない"
            print(f"{s:>6} {t['name'][:12]:<12}{t['strategy']:>8}{t['bt']:>5.0f}"
                  f"{t['pnl']:>+10,.0f}  {tag}")
        if len(_lst) > _SHOW:
            _rest = _lst[_SHOW:]
            print(f"  ... 他 {len(_rest)}銘柄 (想定損益合計 "
                  f"{sum(by_sym[s]['pnl'] for s in _rest):+,.0f}円)")
        print(f"  → 想定損益 {_p:+,.0f}円 を取り逃し "
              f"(発注済だが未約定 {_o}件 / そもそも発注していない {_n}件)")

        # BT帯別: どの帯を発注できていないかが分かると予算設計に直結する
        print(f"\n  【BT帯別の取り逃し】発注枠を増やすとどの帯が拾えるか")
        print(f"  {'BT帯':<10}{'銘柄':>5}{'想定損益':>12}{'1件あたり':>11}")
        for lo, hi, lb in [(70, 999, "BT70+"), (60, 69, "BT60-69"), (50, 59, "BT50-59"),
                           (40, 49, "BT40-49"), (20, 39, "BT20-39"), (0, 19, "BT0-19")]:
            g = [s for s in _lst if lo <= by_sym[s]["bt"] <= hi]
            if not g:
                continue
            gp = sum(by_sym[s]["pnl"] for s in g)
            print(f"  {lb:<10}{len(g):>5}{gp:>+11,.0f}円{gp / len(g):>+10,.0f}円")

    if real_only:
        print(f"\n▼ 実約定したがテストに無い {len(real_only)}銘柄")
        for s in real_only:
            r = real_done[s]
            print(f"{s:>6} {r['name'][:12]:<12}{r['pnl']:>+10,.0f}")
        print("  → テストが『約定しない/シグナルなし』と判定した銘柄。実際は約定した。")

    _r_tot = sum(r["pnl"] for r in real_done.values())
    print()
    print("-" * 78)
    print(f"[実約定] {len(real_done)}銘柄 {_r_tot:+,.0f}円   "
          f"[テスト] {len(by_sym)}銘柄 {_bt_tot:+,.0f}円   "
          f"[差] {_r_tot - _bt_tot:+,.0f}円")
    if ordered:
        print(f"[約定率] 実 {len(real_done)}/{len(ordered)}件 "
              f"({len(real_done) / len(ordered) * 100:.1f}%)  vs  "
              f"テスト {len(by_sym)}件が約定判定")
    else:
        # エントリーの売り注文が対象日に無い = 前日夜に発注してRecvTimeが前日になっている
        # ケース。約定率は算出できないので、その旨を出す(0除算で落ちない)。
        print(f"[約定率] 算出不可: 対象日 {_DATE} に『売り』注文が1件もありません。")
        print(f"         エントリーの逆指値売りは前営業日の夜に発注されている可能性があります。")
        print(f"         前日で確認: .\\fills --date <前営業日>  /  日付を見ない: .\\fills --no-date")

    if not args.no_slip_log:
        _append_slip_log(both, real_done, by_sym, ordered, _r_tot, _bt_tot)


# ── 乖離の日次累積ログ ────────────────────────────────────────────────────
# なぜ必要か: 1日の突合だけでは「たまたま悪い2件があった」のか「毎日そうなのか」が
# 分からない。lss の月の期待値は +37,647円(CLAUDE.md 18.12)しかないので、1日
# -2,900円 の乖離が常態なら月 -58,000円 になり期待値が消える。10営業日ぶん貯めれば
# それが確定する。tenkan_daily_log.csv と同じ思想(1日1行・同じ日は上書き)。
_SLIP_COLS = ["date", "突合", "実損益", "テスト損益", "差",
              "エントリー滑り", "決済滑り", "平均エントリー滑り%",
              "実件数", "実損益_全", "テスト件数", "テスト損益_全", "差_全",
              "発注件数", "約定率%"]


def _append_slip_log(both, real_done, by_sym, ordered, r_tot, bt_tot) -> None:
    """当日の乖離を1行にして累積ログへ upsert し、2日以上あれば累計を表示する。

    差の内訳(空売りなので符号に注意):
      エントリー滑り = (実売値 - テスト売値) x 株数   高く売れていればプラス
      決済滑り       = (テスト買戻値 - 実買戻値) x 株数 安く買い戻せていればプラス
    どちらが効いているかで打ち手が変わる(エントリー=発注価格、決済=損切りの出し方)。
    """
    _e_slip = _x_slip = 0.0
    _r_both = _t_both = 0.0
    _pcts: list[float] = []
    for s in both:
        r, t = real_done[s], by_sym[s]
        q = r["qty"] or 100
        if t["entry_p"]:
            _e_slip += (r["entry(売)"] - t["entry_p"]) * q
            _pcts.append((r["entry(売)"] - t["entry_p"]) / t["entry_p"] * 100)
        if t["exit_p"]:
            _x_slip += (t["exit_p"] - r["exit(買戻)"]) * q
        _r_both += r["pnl"]
        _t_both += t["pnl"]

    _ymd = f"{_DATE_DIG[:4]}-{_DATE_DIG[4:6]}-{_DATE_DIG[6:8]}" if len(_DATE_DIG) == 8 else str(_DATE)
    row = {
        "date": _ymd,
        "突合": len(both),
        "実損益": round(_r_both),
        "テスト損益": round(_t_both),
        "差": round(_r_both - _t_both),
        "エントリー滑り": round(_e_slip),
        "決済滑り": round(_x_slip),
        "平均エントリー滑り%": round(sum(_pcts) / len(_pcts), 3) if _pcts else 0.0,
        "実件数": len(real_done),
        "実損益_全": round(r_tot),
        "テスト件数": len(by_sym),
        "テスト損益_全": round(bt_tot),
        "差_全": round(r_tot - bt_tot),
        "発注件数": len(ordered),
        "約定率%": round(len(real_done) / len(ordered) * 100, 1) if ordered else 0.0,
    }

    p = Path(args.slip_log)
    hist: dict = {}
    if p.exists():
        try:
            with open(p, encoding="utf-8-sig", newline="") as f:
                for r0 in _csv.DictReader(f):
                    if r0.get("date"):
                        hist[r0["date"]] = r0
        except Exception:
            hist = {}
    hist[_ymd] = row            # 同じ日を再実行したら上書き(重複しない)
    try:
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_SLIP_COLS, extrasaction="ignore")
            w.writeheader()
            for d in sorted(hist):
                w.writerow(hist[d])
    except Exception as e:
        print(f"\n[!] 累積ログを書けませんでした: {e}")
        return

    def _f(v) -> float:
        try:
            return float(str(v).replace(",", "").strip() or 0)
        except ValueError:
            return 0.0

    print()
    print("=" * 78)
    print(f"=== 累積: 実約定 vs テストの乖離 ({p.name} / {len(hist)}営業日) ===")
    print(f"  {'日付':<12}{'突合':>5}{'実損益':>10}{'テスト':>10}{'差':>10}"
          f"{'エントリ滑り':>12}{'決済滑り':>10}{'約定率':>8}")
    for d in sorted(hist):
        h = hist[d]
        print(f"  {d:<12}{int(_f(h.get('突合'))):>5}{_f(h.get('実損益')):>+10,.0f}"
              f"{_f(h.get('テスト損益')):>+10,.0f}{_f(h.get('差')):>+10,.0f}"
              f"{_f(h.get('エントリー滑り')):>+12,.0f}{_f(h.get('決済滑り')):>+10,.0f}"
              f"{_f(h.get('約定率%')):>7.1f}%")

    n_d = len(hist)
    n_b = sum(int(_f(h.get("突合"))) for h in hist.values())
    d_tot = sum(_f(h.get("差")) for h in hist.values())
    e_tot = sum(_f(h.get("エントリー滑り")) for h in hist.values())
    x_tot = sum(_f(h.get("決済滑り")) for h in hist.values())
    print("  " + "-" * 76)
    print(f"  {'合計':<12}{n_b:>5}{sum(_f(h.get('実損益')) for h in hist.values()):>+10,.0f}"
          f"{sum(_f(h.get('テスト損益')) for h in hist.values()):>+10,.0f}{d_tot:>+10,.0f}"
          f"{e_tot:>+12,.0f}{x_tot:>+10,.0f}")
    print()
    print(f"  1日あたりの乖離   {d_tot / n_d:>+12,.0f}円"
          f"   → 月20営業日換算 {d_tot / n_d * 20:>+12,.0f}円")
    if n_b:
        print(f"  1件あたりの乖離   {d_tot / n_b:>+12,.0f}円   (突合 {n_b}件)")
    print(f"  内訳: エントリー {e_tot / n_d:>+10,.0f}円/日   決済 {x_tot / n_d:>+10,.0f}円/日")
    print()
    if n_d < 10:
        print(f"  ※ まだ {n_d}営業日。10営業日ぶん貯まるまでは判断材料になりません"
              f"(1件の外れ値で符号が反転します)")
    else:
        print(f"  ※ 月20営業日換算の乖離を lss の月期待値(+37,647円 / CLAUDE.md 18.12)と"
              f"比べてください。")
        print(f"     これを超えているなら、バックテストが黒字でも実運用は赤字です。")


if __name__ == "__main__":
    main()
