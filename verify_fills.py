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
ap.add_argument("--lss-compare", action="store_true",
                help="突合相手を **現行lss(逆指値)** の明細に固定する。既定は "
                     "<csv>_H.csv (H=指値売り) があればそちらを優先する。"
                     "実発注が H なのに lss と突合すると、寄りで上に飛んで戻らなかった"
                     "銘柄が『テストに無い』と誤報告され、エントリー方式の差が"
                     "『滑り』として計上される(2026-08-13)")
ap.add_argument("--save", action="store_true",
                help="CSV2種を日付つきファイル名で自動保存 "
                     "(fills_<日付>.csv / orders_<日付>.csv)。--csv/--orders-csv より優先度低")
ap.add_argument("--slip-log", type=str, default="slip_daily_log.csv",
                help="実約定とテストの乖離を1日1行で貯める累積ログ(既定 slip_daily_log.csv)。"
                     "同じ日を再実行してもその行が上書きされるだけで重複しない")
ap.add_argument("--no-slip-log", action="store_true",
                help="累積ログを書かない(表示のみ)")
# ⛔ 持ち越し決済のあった日は既定で累積ログに入れない(§18.37 の測定が壊れるため)。
#   それでも記録したいときだけ明示する。
ap.add_argument("--force-slip-log", action="store_true",
                help="持ち越し決済があっても累積ログに記録する(既定は除外)")
args = ap.parse_args()

FEE = args.fee
# 突合相手の方式(J / H / lss)。累積ログに残し、**方式ごとに別集計**する
# (§18.24: 別々の条件の数字を混ぜない)。2026-08-18 に実発注が H → J に
# 変わったので、それ以前の H の行とは合算しない。
_CMP_MODE = "H"
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
    # ⛔⛔ **日跨ぎの決済(持ち越し→翌日返済)も往復として拾う**(2026-08-20)。
    #   旧実装は『同じ対象日に 売り と 買い が両方ある』ときだけ往復とみなし、
    #   前日に建てて当日返済した取引を **qty=0 → 損益 +0円** として黙って
    #   捨てていた。2026-08-19 の watcher 障害で8銘柄を持ち越し、翌朝
    #   強制決済(寄成)されたとき、実損 -13,120円 が **+0円 と表示**された。
    #   損失が数字に出ない = 今日の一連の障害とまったく同じ形なので直す。
    #   → 対象日より **前** の売り約定も別に集めておき、当日 買いだけの銘柄は
    #     そちらを建値として突き合わせる。
    prev_sells = defaultdict(lambda: {"qty": 0.0, "notional": 0.0, "times": []})
    for o in orders:
        sym = str(o.get("Symbol") or "").split(".")[0]
        names[sym] = str(o.get("SymbolName") or "")
        side = str(o.get("Side"))            # "1"=売 "2"=買
        for px, qty, t in _executions(o):
            if not _match_date(t):
                # 対象日より前の売り = 持ち越し玉の建値候補
                if side == "1" and _digits(t) and _digits(t)[:8] < _DATE_DIG:
                    prev_sells[sym]["qty"] += qty
                    prev_sells[sym]["notional"] += px * qty
                    prev_sells[sym]["times"].append(t)
                continue
            book = sells if side == "1" else buys
            book[sym]["qty"] += qty
            book[sym]["notional"] += px * qty
            book[sym]["times"].append(t)

    exp = _load_expected(args.expected) if args.expected else {}

    rows = []
    tot_net = 0.0
    n_carry = 0
    for sym in sorted(set(sells) | set(buys)):
        s, b = sells.get(sym), buys.get(sym)
        # ★ 当日に売りが無く買いだけある = **前日以前に建てた玉の返済**。
        #   前日以前の売り約定を建値として使う(持ち越し決済)。
        _carried = False
        if (not s or not s["qty"]) and b and b["qty"] and prev_sells.get(sym, {}).get("qty"):
            s = prev_sells[sym]
            _carried = True
            n_carry += 1
        avg_sell = (s["notional"] / s["qty"]) if (s and s["qty"]) else 0.0
        avg_buy = (b["notional"] / b["qty"]) if (b and b["qty"]) else 0.0
        qty = min(s["qty"] if s else 0, b["qty"] if b else 0)
        et = _hhmm(min(s["times"])) if (s and s["times"]) else "—"   # 売り=エントリー時刻
        if _carried and s["times"]:
            _d = _digits(min(s["times"]))[:8]
            et = f"{_d[4:6]}/{_d[6:8]}"       # 建てた日を出す(当日でないと分かるように)
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
                     "entry_t": et, "exit_t": xt, "pnl": round(net, 0),
                     "pct": round(pct, 2), "carried": int(_carried)})

    # 表示
    print("=== 結果 (上の注文のうち 約定して決済まで済んだ取引の実損益) ===")
    print(f"{'コード':>6} {'銘柄':<12}{'株数':>5}{'実売り':>9}{'実買戻':>9}"
          f"{'約定':>6}{'決済':>6}{'実損益':>10}{'%':>7}" + ("   |想定損益  乖離" if exp else ""))
    for r in rows:
        line = (f"{r['symbol']:>6} {r['name'][:12]:<12}{r['qty']:>5}"
                f"{r['entry(売)']:>9,.1f}{r['exit(買戻)']:>9,.1f}"
                f"{r['entry_t']:>6}{r['exit_t']:>6}{r['pnl']:>+10,.0f}{r['pct']:>+6.2f}%"
                + ("  ⛔持越決済" if r.get("carried") else ""))
        if exp:
            e = exp.get(r["symbol"], {})
            ep = e.get("pnl")
            if ep is not None:
                line += f"   |{ep:>+9,.0f} {r['pnl']-ep:>+9,.0f}"
        print(line)

    print(f"\n[実損益 合計] {tot_net:+,.0f}円  (往復完了 {sum(1 for r in rows if r['qty']>0)}銘柄)")
    if n_carry:
        _cn = sum(r["pnl"] for r in rows if r.get("carried"))
        print(f"""
{'=' * 78}
⛔⛔ **持ち越し決済が {n_carry}銘柄 あります** (前日以前に建てて当日返済)
{'=' * 78}
  この {n_carry}銘柄の実損益: {_cn:+,.0f}円
  ⚠ J は **同日決済**の戦略です。持ち越しは設計外 = 何かが起きた日です
    (watcher が落ちた / 引け決済が飛んだ 等)。
  ⛔ **一般信用(デイトレ)を持ち越した場合は 強制決済手数料 2,200円/銘柄**が
     別途かかります(この {n_carry}銘柄なら {n_carry * 2200:,}円)。上の実損益には
     **含まれていません**。
  ⛔ **この日の数字を J の成績・スリッページ測定に混ぜないでください。**
     戦略の損益ではなく事故の費用です(§18.37 の測定が壊れます)。
{'=' * 78}""")
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


def _j_missing_reason(p: Path, ymd: str) -> list[str]:
    """J の明細に対象日が無い理由を、推測ではなく **中身を見て** 説明する。"""
    _out = []
    if not p.exists():
        return [f"{p.name} がありません。.\\dailyfast を1回流してください"]
    _ds: set = set()
    _n = 0
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in _csv.DictReader(f):
                _n += 1
                _d = str(r.get("entry_date") or "")[:10]
                if _d:
                    _ds.add(_d)
    except Exception as e:
        return [f"{p.name} を読めません: {e}"]
    _sd = sorted(_ds)
    _out.append(f"ファイルは {_n:,}行 / 日付 {len(_sd)}日ぶん"
                + (f"（最新 {_sd[-1]}）" if _sd else ""))
    _want = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    if _sd and _sd[-1] < _want:
        _out.append(f"⛔ **当日({_want})の行がまだありません**。")
        _out.append("   J(09:00確認)は **5分足の09:00バー** から始値を取ります。")
        _out.append("   当日ぶんの5分足を yfinance で埋めた場合、yfinance は")
        _out.append("   09:00-09:05 のバーを持たない(§18.38)ので、全銘柄が")
        _out.append("   『遅寄り』扱いになり J は1件も建てません。")
        _out.append("   → **翌営業日に J-Quants の5分足が入ってから** "
                    ".\\dailyfast を流し直すと突合できます。")
    elif _want in _ds:
        _out.append(f"⚠ {_want} の行はあるのに拾えていません。列名を確認してください")
    else:
        _out.append(f"⚠ {_want} だけが抜けています(前後の日はあります)。"
                    "その日の5分足か合格判定を確認してください")
    return _out


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
                        # 発注順は **流動性(売買代金)降順**(18.21/18.37)。BTは
                        # アルゴリズムから外したので並びにも使わない。
                        "liq": float(r.get("liquidity", 0) or 0),
                        "entry_p": float(r.get("entry_p", 0) or 0),
                        "exit_p": float(r.get("exit_p", 0) or 0),
                        # 指値そのもの。**約定の滑りと分けて見る**ために要る。
                        # 指値が違えば約定値も違うが、それは滑りではなく
                        # 『ライブとバックテストで注文が別物』という別の問題
                        # (2026-08-14: 3197 で実 3,205 / テスト 3,230 と 25円ズレ、
                        #  それを『エントリー滑り -0.77%』と誤表示していた)。
                        "order_limit": float(r.get("order_limit", 0) or 0),
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
    # ⛔ 突合相手は **実際に出している注文方式** の明細でなければ意味がない。
    #    実発注は H(前日終値-5tick の指値売り)なのに、既定の lss_trades.csv は
    #    現行lss(逆指値売り)の明細だった(2026-08-13 発覚)。lss は『前日終値-1tick
    #    まで下がったら約定』なので、寄りで上に飛んで戻らなかった銘柄は約定しない。
    #    実際 7186/9508/5844 は H では寄りで約定したのに lss 明細に無く、
    #    『テストに無い』と誤報告された。1605 も lss 3,710 vs H 3,730 を比べて
    #    滑り +0.54% と出ていたが、これは滑りではなくエントリー方式の差。
    _tp = Path(args.trades_csv)
    _hp = _tp.with_name(_tp.stem + "_H" + _tp.suffix)
    # ★★ 実発注は **J(09:00確認)** に変わった (2026-08-18)。突合相手も J に
    #   しないと意味がない。J の明細はレポートが lss_trades_K.csv に出す
    #   (推奨変種 = H寄り確認…資金均等。ファイル名の K は歴史的な名前で、
    #    中身は J の推奨設定)。⛔ H(前日終値-5tick の指値)とは **建てる条件も
    #   約定値も違う**ので、H と突合すると滑りが全部でたらめになる。
    #   J → H → (--lss-compare のときだけ lss) の順に探す。
    _jp = _tp.with_name(_tp.stem + "_K" + _tp.suffix)
    global _CMP_MODE
    if args.lss_compare:
        _CMP_MODE = "lss"
        bt = _bt_trades_for_date(args.trades_csv, _DATE_DIG)
        _src_label = f"{_tp.name} ⚠ 現行lss(逆指値)。--lss-compare 指定"
    else:
        bt = _bt_trades_for_date(str(_jp), _DATE_DIG) if _jp.exists() else []
        if bt:
            _CMP_MODE = "J"
            _src_label = f"{_jp.name} (J=09:00確認。**実発注と同じ方式**)"
        else:
            # ⛔ **なぜ J が使えないのか**を必ず言う。黙って H に落ちると、
            #   H の指値(前日終値-5tick)と J の指値(始値×0.995)を比べて
            #   『指値のズレ』という誤警告が出る(2026-08-18 に実際に出た:
            #    9107 実1,100 vs テスト3,045 と表示されたが、J の指値は
            #    3,119×0.995=3,103→3,100 で **ライブが正しかった**)。
            _CMP_MODE = "H(J実発注)"
            bt = _bt_trades_for_date(str(_hp), _DATE_DIG) if _hp.exists() else []
            _src_label = f"{_hp.name} (H=指値売り) ⚠ 実発注は J なので参考値"
            _why = _j_missing_reason(_jp, _DATE_DIG)
            print()
            print("=" * 78)
            print(f"⛔ J の明細({_jp.name})に {_DATE} の取引がありません。")
            for _ln in _why:
                print(f"   {_ln}")
            print( "   → 下の『指値のズレ』は **H の指値と比べたもの**なので")
            print( "     無視してください(方式が違うだけで、ライブは正しい)。")
        if not bt:
            # ⛔ lss へフォールバックしない。方式が違う明細と突合すると
            #    『テストに無い』『滑り』が全部でたらめになる(2026-08-13)。
            print()
            print("=" * 78)
            print("=== バックテスト比較: スキップ (J/H の明細がありません) ===")
            print(f"  {_jp.name} も {_hp.name} も無い、または {_DATE} の取引が")
            print( "  ありません。実発注は **J(09:00確認 / 始値を見てから発注)** なので、")
            print( "  lss(逆指値)や H(前日終値-5tick)の明細と突合しても意味がありません")
            print( "  (方式が違うので別の銘柄・別の約定値になる)。")
            print( "  → git pull してから .\\jfast を1回流してください。")
            print(f"     J の明細は {_jp.name} に出ます")
            print( "     ⛔ LSS_TRADES_CSV を別名に逃がして走らせた場合は、その名前の")
            print( "        _K.csv を --trades-csv で指してください")
            print( "  どうしても lss と比べたいときだけ .\\fills --lss-compare")
            return
    print()
    print("=" * 78)
    print(f"[突合相手] {_src_label}")
    if not bt:
        print(f"=== バックテスト比較: スキップ ===")
        print(f"  {args.trades_csv} に {_DATE} の取引が見つかりません。")
        print(f"  daily.bat / dailyfast.bat は LSS_TRADES_CSV=lss_trades.csv を既定で設定します。")
        print(f"  出ていない場合は git pull してから .\\daily を1回流してください")
        print(f"  (対象日の取引はレポート生成時点までしか入りません。当日ぶんが要るなら"
              f"引け後に .\\daily → .\\fills の順で)。")
        return

    # ★ 『約定せず』は合計・一覧から外すが、診断用に控えておく。
    #    実約定したのにテストに無い銘柄が
    #      (a) テストは母集団に持っていて「約定しない」と判定した  → 約定モデルの誤り
    #      (b) そもそも母集団に無い(バックテストがシグナルを出していない)
    #    のどちらなのかで、直す場所がまったく違う(2026-08-13)。
    _nofill = {t["symbol"]: t for t in bt if str(t.get("reason")) == "約定せず"}
    bt = [t for t in bt if str(t.get("reason")) != "約定せず"]
    if _nofill:
        print(f"  (うち H が『約定せず』と判定 {len(_nofill)}件 は合計から除外)")

    # ⛔ 並び・統合に BT を使わない(18.21 で BT降順はランダム帯の外=有意に悪いと
    #    実測 / 18.37 で「BTは一切使わない」に確定 / 2026-08-12 ユーザー決定)。
    #    実発注(lss_budget_cap → lss_order_rank)とまったく同じキーで並べる。
    #    ここがズレると「発注していない銘柄」を見て誤った予算判断をする
    #    (2026-08-14: BT88 の銘柄が未発注と出て、枠不足に見えた)。
    try:
        import lss_order_rank as _lor
        def _ordk(t):
            return _lor.sort_key(t.get("bt"), t.get("liq"), t.get("symbol", ""))
        _ord_lbl = _lor.describe()
    except Exception:
        def _ordk(t):
            return (0 if float(t.get("liq") or 0) > 0 else 1,
                    -float(t.get("liq") or 0), str(t.get("symbol", "")))
        _ord_lbl = "lss発注順: 流動性(売買代金)降順(フォールバック)"

    # 同一銘柄が複数戦略で出たら発注順が先の1件に統合(実運用=1銘柄1ポジション)
    by_sym: dict = {}
    for t in bt:
        e = by_sym.get(t["symbol"])
        if e is None or _ordk(t) < _ordk(e):
            by_sym[t["symbol"]] = t
    # その日の発注順位(#1 が最優先)。実発注のリストと同じ並びになる。
    _rank = {s: i + 1 for i, s in
             enumerate(sorted(by_sym, key=lambda s: _ordk(by_sym[s])))}

    print(f"=== バックテスト(レポート)の {_DATE} 取引 : {len(by_sym)}銘柄 ===")
    print(f"  {_ord_lbl}")
    print(f"{'コード':>6} {'銘柄':<12}{'戦略':>8}{'発注順':>6}{'売買代金':>10}"
          f"{'約定値':>9}{'決済値':>9}{'株数':>5}{'損益':>10}  理由")
    _bt_tot = 0.0
    for s in sorted(by_sym, key=lambda x: _rank[x]):
        t = by_sym[s]
        _bt_tot += t["pnl"]
        _lq = float(t.get("liq") or 0)
        print(f"{s:>6} {t['name'][:12]:<12}{t['strategy']:>8}"
              f"{('#' + str(_rank[s])):>6}"
              f"{(f'{_lq / 1e8:,.1f}億' if _lq else '—'):>10}"
              f"{t['entry_p']:>9,.1f}{t['exit_p']:>9,.1f}{t['qty']:>5}"
              f"{t['pnl']:>+10,.0f}  {t['reason']}")
    _bt_w = sum(1 for t in by_sym.values() if t["pnl"] > 0)
    print(f"\n[テスト 合計] {_bt_tot:+,.0f}円  ({len(by_sym)}銘柄 / 勝ち{_bt_w} 負け{len(by_sym)-_bt_w})")

    # ── 突合 ──
    # ⛔ **持ち越し決済(carried)は突合から外す**(2026-08-20)。
    #    建てたのは前営業日以前なので、対象日のテスト明細と突き合わせても
    #    必ず食い違う。実際 08-20 は 08-19 に建てた8銘柄が
    #    『テストの母集団に無い(バックテストがシグナルを出していない)』と
    #    誤報告された(バックテストは 08-19 側にちゃんと持っている)。
    #    同じ銘柄が両日に出ていれば『両方にある』に化けて滑りまで捏造する。
    real_all = {r["symbol"]: r for r in real_rows if r["qty"] > 0}
    carried = {s: r for s, r in real_all.items() if r.get("carried")}
    real_done = {s: r for s, r in real_all.items() if not r.get("carried")}
    ordered = {r["code"] for r in order_rows if r.get("side") == "売"}
    both = sorted(set(real_done) & set(by_sym))
    real_only = sorted(set(real_done) - set(by_sym))
    bt_only = sorted(set(by_sym) - set(real_done))

    print()
    print("=" * 78)
    print("=== 突合: 実約定 vs テスト ===")
    if carried:
        _cd = sorted({r["entry_t"] for r in carried.values()})
        print(f"\n⛔ 持ち越し決済 {len(carried)}銘柄 は突合から除外しました "
              f"(建てたのは {'/'.join(_cd)})")
        print(f"   {' '.join(sorted(carried))}")
        print(f"   → この銘柄の突合は建てた日で見ること: "
              f".\\fills --date <建てた日>")
    if both:
        # ⛔ **損益の額を直接引き算してはいけない**(2026-08-18)。
        #    テストは予算400万の資金均等(300株など)、少額テストの実運用は
        #    予算60万で100株。株数が違えば損益の額も当然違うので、その差は
        #    『乖離』ではなく『サイズの差』。比較していいのは **1株あたり**だけ。
        #    (この日: 2734 テスト -3,600円/300株 = -12円/株 に対し
        #     実運用 -1,200円/100株 = -12円/株 で **完全一致**。額だけ見ると
        #     2,400円ズレて見える)
        _ps = lambda v, q: (float(v) / q if q else 0.0)     # noqa: E731
        _qmix = any(int(real_done[s]["qty"]) != int(by_sym[s]["qty"]) for s in both)
        print(f"\n▼ 両方にある {len(both)}銘柄 (これが本当の乖離)")
        print(f"  ★ 損益は **1株あたり** で比べます(株数が違うと額は比較できません)")
        print(f"{'コード':>6} {'銘柄':<12}{'株数実/テ':>11}{'実約定値':>10}{'テスト':>9}"
              f"{'滑り':>8}{'実決済':>10}{'テスト':>9}"
              f"{'実円/株':>9}{'テ円/株':>9}{'差/株':>8}")
        _d_ps = 0.0
        for s in both:
            r, t = real_done[s], by_sym[s]
            slip = ((r["entry(売)"] - t["entry_p"]) / t["entry_p"] * 100
                    if t["entry_p"] else 0.0)
            _rq, _tq = int(r["qty"] or 0), int(t["qty"] or 0)
            _rp, _tp = _ps(r["pnl"], _rq), _ps(t["pnl"], _tq)
            _d_ps += _rp - _tp
            print(f"{s:>6} {t['name'][:12]:<12}"
                  f"{(str(_rq) + '/' + str(_tq)):>11}"
                  f"{r['entry(売)']:>10,.1f}{t['entry_p']:>9,.1f}"
                  f"{slip:>+7.2f}%{r['exit(買戻)']:>10,.1f}{t['exit_p']:>9,.1f}"
                  f"{_rp:>+9,.1f}{_tp:>+9,.1f}{_rp - _tp:>+8,.1f}")
        print(f"{'平均':>6} {'':<12}{'':>11}{'':>10}{'':>9}{'':>8}{'':>10}{'':>9}"
              f"{sum(_ps(real_done[s]['pnl'], real_done[s]['qty']) for s in both) / len(both):>+9,.1f}"
              f"{sum(_ps(by_sym[s]['pnl'], by_sym[s]['qty']) for s in both) / len(both):>+9,.1f}"
              f"{_d_ps / len(both):>+8,.1f}")
        print(f"  (参考: 額の合計 実 {sum(real_done[s]['pnl'] for s in both):+,.0f}円 / "
              f"テスト {sum(by_sym[s]['pnl'] for s in both):+,.0f}円"
              + ("  ⛔ **株数が違うので この2つは比較できません**" if _qmix else "")
              + ")")
        if _qmix:
            print(f"  → 少額テスト(予算60万)は 100株、レポート(予算400万)は資金均等。")
            print(f"    ⛔ **少額テストの実損益をレポートの損益と比べないでください。**")
            print(f"    値がさ株は少額では1単元も建たないので、銘柄の顔ぶれ自体が"
                  f"違います(構造バイアス)。比較できるのは上の 円/株 だけ。")
        # 差/株 の合計は乖離の総量。0 に近ければ約定モデルは正しい。
        _d_tot = _d_ps

        # ── 指値そのもののズレ ────────────────────────────────────
        # ⛔ 上の「滑り」は (実約定値 - テスト約定値) なので、**指値が違う**だけでも
        #    滑りとして出てしまう。それは執行の問題ではなく『ライブとバックテストで
        #    注文が別物』という設定・データの問題で、直す場所がまったく違う
        #    (2026-08-14: 3197 が実 3,205 / テスト 3,230 の 25円ズレなのに
        #     『エントリー滑り -0.77%』と表示され、約定モデルの問題に見えた)。
        _ordp = {r["code"]: r["order_price"] for r in order_rows
                 if r.get("side") == "売" and float(r.get("order_price") or 0) > 0}
        _mis = []
        for s in both:
            _rl = float(_ordp.get(s) or 0)
            _tl = float(by_sym[s].get("order_limit") or 0)
            if _rl > 0 and _tl > 0 and abs(_rl - _tl) >= 0.5:
                _mis.append((s, _rl, _tl))
        if _mis:
            print(f"\n  ⛔ 【指値のズレ】{len(_mis)}銘柄 — ライブとバックテストで"
                  f"**注文そのものが別物**です(滑りではありません)")
            print(f"  {'コード':>6} {'銘柄':<12}{'実際の指値':>10}{'テスト':>9}"
                  f"{'差':>8}{'株数ぶん':>10}")
            for s, _rl, _tl in _mis:
                print(f"  {s:>6} {by_sym[s]['name'][:12]:<12}{_rl:>10,.1f}"
                      f"{_tl:>9,.1f}{_rl - _tl:>+8,.1f}"
                      f"{(_rl - _tl) * by_sym[s]['qty']:>+10,.0f}円")
            print(f"  → 前日終値・呼値・LSS_H_LIMIT_TICKS のどれかが食い違っています。"
                  f"§18.9 の鉄則(バックテストとライブを揃える)に反するので要調査。")
        elif _ordp:
            print(f"\n  ✅ 指値は全銘柄で一致(ライブとバックテストが同じ注文を出している)")

    if bt_only:
        # 発注順(=流動性降順)で並べる。件数が多いので上位だけ出して残りは要約。
        _lst = sorted(bt_only, key=lambda s: _rank[s])
        _o = sum(1 for s in _lst if s in ordered)
        _n = len(_lst) - _o
        _p = sum(by_sym[s]["pnl"] for s in _lst)
        print(f"\n▼ テストにあるが実約定なし {len(_lst)}銘柄 (発注順)")
        _SHOW = 15
        for s in _lst[:_SHOW]:
            t = by_sym[s]
            tag = "発注済(未約定)" if s in ordered else "発注していない"
            print(f"{s:>6} {t['name'][:12]:<12}{t['strategy']:>8}"
                  f"{('#' + str(_rank[s])):>6}"
                  f"{t['pnl']:>+10,.0f}  {tag}")
        if len(_lst) > _SHOW:
            _rest = _lst[_SHOW:]
            print(f"  ... 他 {len(_rest)}銘柄 (想定損益合計 "
                  f"{sum(by_sym[s]['pnl'] for s in _rest):+,.0f}円)")
        print(f"  → 想定損益 {_p:+,.0f}円 を取り逃し "
              f"(発注済だが未約定 {_o}件 / そもそも発注していない {_n}件)")

        # 発注順位帯別: 枠を増やすと **次に何が入るか** が分かる。
        # ⛔ BT帯では出さない。BTは発注順に一切使っていないので、BTで束ねても
        #    「枠を増やしたら拾える集団」にならない(18.12/18.21/18.37)。
        #    枠は発注順で上から埋まるので、順位で束ねるのが唯一正しい切り方。
        print(f"\n  【発注順位帯別の取り逃し】枠を増やすと上から順に拾える")
        print(f"  {'順位帯':<10}{'銘柄':>5}{'想定損益':>12}{'1件あたり':>11}")
        _n_all = len(by_sym)
        for lo, hi in [(1, 10), (11, 20), (21, 30), (31, 50), (51, 10**6)]:
            g = [s for s in _lst if lo <= _rank[s] <= hi]
            if not g:
                continue
            gp = sum(by_sym[s]["pnl"] for s in g)
            _lb = f"#{lo}-{min(hi, _n_all)}" if hi < 10**6 else f"#{lo}以降"
            print(f"  {_lb:<10}{len(g):>5}{gp:>+11,.0f}円{gp / len(g):>+10,.0f}円")

    if real_only:
        print(f"\n▼ 実約定したがテストに無い {len(real_only)}銘柄")
        _n_model, _n_pop = 0, 0
        for s in real_only:
            r = real_done[s]
            _nf = _nofill.get(s)
            if _nf:
                _n_model += 1
                _why = (f"H は『約定せず』と判定 "
                        f"(指値{_nf.get('entry_p') or '-'}) ← **約定モデルの誤り**")
            else:
                _n_pop += 1
                _why = "テストの母集団に無い(バックテストがシグナルを出していない)"
            print(f"{s:>6} {r['name'][:12]:<12}{r['pnl']:>+10,.0f}  {_why}")
        if _n_model:
            print(f"  ・{_n_model}件: **H の約定判定が実際とズレている**。"
                  f"寄りが指値以上なら板寄せで約定するはずなので、"
                  f"ギャップガード(±3%)や5分足の欠落を疑う")
        if _n_pop:
            print(f"  ・{_n_pop}件: **損益タブの母集団に無い**。シグナルタブ(ライブ判定 "
                  f"check_signal_on_date)は出したのに、損益タブ(バックテスト "
                  f"run_limit_backtest)が当日の取引として持っていない。")
            print(f"    当日ぶんは決着していないと『発注中』扱いで明細から外れる"
                  f"(nikkei_analysis: pending_trades=[])。翌営業日に .\\daily を"
                  f"流し直すと入るはずなので、まずそれで消えるか確認すること。")

    _r_tot = sum(r["pnl"] for r in real_done.values())
    print()
    print("-" * 78)
    print(f"[実約定] {len(real_done)}銘柄 {_r_tot:+,.0f}円   "
          f"[テスト] {len(by_sym)}銘柄 {_bt_tot:+,.0f}円   "
          f"[差] {_r_tot - _bt_tot:+,.0f}円")
    if carried:
        _ct = sum(r["pnl"] for r in carried.values())
        print(f"         ※ 上の[実約定]に **持ち越し {len(carried)}銘柄 "
              f"{_ct:+,.0f}円 は含みません**(突合の対象外)。"
              f"当日の実損益の総額は上の『[実損益 合計]』を見ること")
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

    # ⛔⛔ **持ち越し決済のあった日は乖離ログに入れない**(2026-08-20)。
    #   slip_daily_log.csv は「J の実運用がバックテストからどれだけ劣化するか」を
    #   測るためのもの(§18.37)。持ち越し = 同日決済という設計が破れた日なので、
    #   混ぜると測定そのものが壊れる。事故の費用は戦略の損益ではない。
    _n_carry = sum(1 for r in real_rows if r.get("carried"))
    if _n_carry:
        print(f"\n⛔ 持ち越し決済 {_n_carry}銘柄 があるので "
              f"**この日は slip_daily_log.csv に記録しません**。\n"
              f"   J は同日決済の戦略なので、持ち越した日の数字を混ぜると"
              f"実スリッページの測定(§18.37)が壊れます。\n"
              f"   どうしても記録するなら --force-slip-log。")
    if not args.no_slip_log and (not _n_carry or args.force_slip_log):
        _append_slip_log(both, real_done, by_sym, ordered, _r_tot, _bt_tot)


# ── 乖離の日次累積ログ ────────────────────────────────────────────────────
# なぜ必要か: 1日の突合だけでは「たまたま悪い2件があった」のか「毎日そうなのか」が
# 分からない。lss の月の期待値は +37,647円(CLAUDE.md 18.12)しかないので、1日
# -2,900円 の乖離が常態なら月 -58,000円 になり期待値が消える。10営業日ぶん貯めれば
# それが確定する。tenkan_daily_log.csv と同じ思想(1日1行・同じ日は上書き)。
#
# ⛔ **額の列(実損益/テスト損益/差/エントリー滑り/決済滑り)は株数に比例する**。
#    少額テスト(100株)と本番サイズ(資金均等)を混ぜて足すと意味を失うので、
#    日をまたいで比べるときは **/株** の列を見ること(2026-08-18 に追加)。
_SLIP_COLS = ["date", "方式", "突合", "実損益", "テスト損益", "差",
              "差/株", "エントリー滑り/株", "決済滑り/株",
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
    _e_ps = _x_ps = _d_ps = 0.0
    _r_both = _t_both = 0.0
    _pcts: list[float] = []
    for s in both:
        r, t = real_done[s], by_sym[s]
        q = r["qty"] or 100
        if t["entry_p"]:
            _e_slip += (r["entry(売)"] - t["entry_p"]) * q
            _e_ps += r["entry(売)"] - t["entry_p"]
            _pcts.append((r["entry(売)"] - t["entry_p"]) / t["entry_p"] * 100)
        if t["exit_p"]:
            _x_slip += (t["exit_p"] - r["exit(買戻)"]) * q
            _x_ps += t["exit_p"] - r["exit(買戻)"]
        _d_ps += (r["pnl"] / q) - (t["pnl"] / (t["qty"] or 100))
        _r_both += r["pnl"]
        _t_both += t["pnl"]
    _n_b = max(1, len(both))

    _ymd = f"{_DATE_DIG[:4]}-{_DATE_DIG[4:6]}-{_DATE_DIG[6:8]}" if len(_DATE_DIG) == 8 else str(_DATE)
    row = {
        "date": _ymd,
        "方式": _CMP_MODE,
        "突合": len(both),
        "実損益": round(_r_both),
        "テスト損益": round(_t_both),
        "差": round(_r_both - _t_both),
        # ★ 株数に依存しない列。日をまたいだ比較はこちらで行う。
        "差/株": round(_d_ps / _n_b, 2),
        "エントリー滑り/株": round(_e_ps / _n_b, 2),
        "決済滑り/株": round(_x_ps / _n_b, 2),
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
    # ⛔ 集計は **いまの方式(H)の行だけ**。08/05〜08/07 は lss(逆指値)の実発注を
    #    v16前の楽観モデルと突合したもので、混ぜると『決済滑り -10,012円/日』の
    #    ような、いまのモデルとは無関係な数字が出る(2026-08-13)。
    #    方式列が無い古い行は lss(旧) 扱いにして集計から外す。
    def _mode_of(h) -> str:
        return str(h.get("方式") or "").strip() or "lss(旧)"

    _cur = [d for d in sorted(hist) if _mode_of(hist[d]) == _CMP_MODE]
    _old = [d for d in sorted(hist) if _mode_of(hist[d]) != _CMP_MODE]
    print(f"=== 累積: 実約定 vs テストの乖離 ({p.name} / {_CMP_MODE} {len(_cur)}営業日) ===")
    print(f"  {'日付':<12}{'方式':>8}{'突合':>5}{'実損益':>10}{'テスト':>10}{'差':>10}"
          f"{'エントリ滑り':>12}{'決済滑り':>10}{'約定率':>8}")
    for d in sorted(hist):
        h = hist[d]
        _m = _mode_of(h)
        _mk = "" if _m == _CMP_MODE else "  ← 集計対象外"
        print(f"  {d:<12}{_m:>8}{int(_f(h.get('突合'))):>5}{_f(h.get('実損益')):>+10,.0f}"
              f"{_f(h.get('テスト損益')):>+10,.0f}{_f(h.get('差')):>+10,.0f}"
              f"{_f(h.get('エントリー滑り')):>+12,.0f}{_f(h.get('決済滑り')):>+10,.0f}"
              f"{_f(h.get('約定率%')):>7.1f}%{_mk}")
    if _old:
        print(f"  ※ {len(_old)}日ぶんは別方式(または v16前)なので集計から除外: "
              f"{', '.join(_old)}")
    if not _cur:
        print(f"  {_CMP_MODE} の行がまだありません。累計は次回から出ます。")
        print()
        return

    _cv = [hist[d] for d in _cur]
    n_d = len(_cv)
    n_b = sum(int(_f(h.get("突合"))) for h in _cv)
    d_tot = sum(_f(h.get("差")) for h in _cv)
    e_tot = sum(_f(h.get("エントリー滑り")) for h in _cv)
    x_tot = sum(_f(h.get("決済滑り")) for h in _cv)
    print("  " + "-" * 76)
    print(f"  {'合計(' + _CMP_MODE + ')':<12}{'':>8}{n_b:>5}"
          f"{sum(_f(h.get('実損益')) for h in _cv):>+10,.0f}"
          f"{sum(_f(h.get('テスト損益')) for h in _cv):>+10,.0f}{d_tot:>+10,.0f}"
          f"{e_tot:>+12,.0f}{x_tot:>+10,.0f}")
    print()
    # ★★ **株数に依存しない集計**。少額テストと本番サイズが混ざっても意味を保つ。
    #    額の列は株数に比例するので、サイズを変えた日をまたぐと足せない。
    _wb = [(int(_f(h.get("突合"))), _f(h.get("差/株")),
            _f(h.get("エントリー滑り/株")), _f(h.get("決済滑り/株")))
           for h in _cv if int(_f(h.get("突合"))) > 0 and h.get("差/株") not in (None, "")]
    if _wb:
        _nb2 = sum(w[0] for w in _wb)
        print(f"  ★ 1株あたり(サイズ非依存 / 突合 {_nb2}件・{len(_wb)}営業日)")
        print(f"     乖離        {sum(w[0] * w[1] for w in _wb) / _nb2:>+9,.2f} 円/株")
        print(f"     エントリー  {sum(w[0] * w[2] for w in _wb) / _nb2:>+9,.2f} 円/株"
              f"   決済 {sum(w[0] * w[3] for w in _wb) / _nb2:>+9,.2f} 円/株")
        print()
    print(f"  【額】※ 株数が同じ日どうしでしか比べられません")
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
