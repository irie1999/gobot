"""lss_budget_cap.py — lss を「予算より多めに発注(over-subscribe)＋約定累計が予算到達で残りを取消」で出す。

背景(sim_portfolio_lss.py の検証結果, 2026-08-01):
  予算400万に対し金額ベース約定率が低く(実測~50%)、平均約定額<予算=予算が遊んでいる。
  流動性降順で M×予算ぶん注文を出し、約定累計が予算に達したら残りの未発動逆指値をキャンセルすると、
  平常日は予算ぴったり埋まり(=稼働率↑・OOS損益↑)、急落日だけ上限で頭打ちになる。
  → 本スクリプトはその「発注 + 上限キャンセル監視」を live で行う。

方式:
  1) 今日の lss シグナルを収集(kabu_send_lss と同一ロジック)。並びは lss_order_rank(流動性降順)。
  2) 価格レンジ(既定1000-6000=実運用 daily と統一)で絞り、**流動性降順**に信用新規売りを発注。
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
from kabu_send_lss import (_load_symbols, _lss_signal_today, _jq_to_yf,   # noqa: E402
                           _log_ordered, DEFAULT_SM, DEFAULT_TM)

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
    # ⛔ 以前は (sym,strat) ごとの **期間最大** を採っていた。半年前に60だが今は10、
    #    というペアが 60 で通ってしまう。レポートの予算タブは各取引の as-of BT
    #    (その時点までの実績)で判定するので、今日の発注に使うべきは **最新** の値。
    #    (18.21 で sim_portfolio_lss の同じ実装を先読みとして直したのと同じ話。
    #     こちらは全部過去データなので先読みではないが、レポートと食い違う)
    bt: dict = {}
    seen: dict = {}
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        sym = _norm(r.get("symbol") or r.get("code") or "")
        strat = str(r.get("strategy") or "").strip()
        if not sym or not strat:
            continue
        try:
            b = float(r.get("bt") or 0)
        except Exception:
            b = 0.0
        d = str(r.get("entry_date") or r.get("exit_date") or "")
        k = (sym, strat)
        if k not in seen or d >= seen[k]:
            seen[k] = d
            bt[k] = b
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
                    help="予算の何倍ぶん注文を出すか(over-subscribe)。実測fill率~50%%なら2.0で予算ちょうど埋まる")
    ap.add_argument("--min-price", type=float, default=1000.0, help="対象最低株価(実運用 daily=1000)")
    ap.add_argument("--max-price", type=float, default=6000.0, help="対象最高株価(実運用 daily=6000)")
    # ⛔ 既定0 = **BTで絞らない**(2026-08-12 確定)。BTは8回測って8回とも
    #    識別力ゼロだった(勝率フラット / 閾値に対して非単調 / 閾値の walk-forward は
    #    固定最良の24.9%・勝ち月1/10)。効いて見えた唯一の場面は先読みだった。
    #    レポート側も LSS_NO_BT_FILTER=1 で床を外してあるので、これで
    #    『シグナルタブに出た銘柄=発注する銘柄=検証している銘柄』が一致する。
    ap.add_argument("--bt-min", type=float, default=0.0,
                    help="BT下限。既定0=絞らない(BTに識別力が無いため)")
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
    # ── エントリー方式 (2026-08-10 追加) ──────────────────────────
    ap.add_argument("--entry-mode", choices=["stop", "limit", "auction"], default="limit",
                    help="limit=**既定/採用中のH案**の指値売り(寄りが上なら板寄せ、日中に"
                         "上がってきても約定。10ヶ月OOSで最良・walk-forwardでも選ばれ続けた) / "
                         "auction=H案の寄指売り(寄付の板寄せだけ) / "
                         "stop=旧lssの逆指値売り。⛔ lssからは発注しない方針なので、"
                         "stop で --execute するには --allow-stop-entry が必要")
    ap.add_argument("--allow-stop-entry", action="store_true",
                    help="⛔ 旧lss(逆指値売り)で実発注することを明示的に許可する。"
                         "通常は使わない(採用しているのは H だけ)")
    ap.add_argument("--limit-ticks", type=int, default=-5,
                    help="limit / auction のとき、指値を前日終値から何ティックずらすか"
                         "(既定 -5)。下げるほど約定は増えるが売り値は不利。"
                         "TRAIN/TEST とも -10..-1 が平坦で 0 以上は明確に劣る(.\\hsweep)")
    ap.add_argument("--size-mode", choices=["fixed", "atr"], default="fixed",
                    help="fixed=100株固定(現行) / atr=**ATR均等**。損切りで失う額"
                         "(損切り幅×株数)を --size-target 円に揃える。100株固定だと建玉が"
                         "10万〜60万と6倍ばらつき、高い株が走った日だけ損失が突出する。"
                         "10ヶ月OOSで 円件 +747→+775 / 前半2位・後半2位で安定")
    ap.add_argument("--size-target", type=float, default=1200.0,
                    help="atr のとき、1銘柄で損切りにかかったときに失う額の目標(円)。既定1200")
    ap.add_argument("--aggressive", action="store_true")
    ap.add_argument("--conservative", action="store_true")
    args = ap.parse_args()
    # H の2形態。どちらも「前日終値±Nティックの指値売り」で、違いは
    # **ザラ場到達を取るか**だけ。OCO はどちらも実約定価格基準(watcher が再計算)。
    _auction = (args.entry_mode == "auction")   # 寄指(21) = 寄付の板寄せのみ
    _limit = (args.entry_mode == "limit")       # 指値(20) = ザラ場でも約定
    _isH = _auction or _limit

    # ⛔ lss(逆指値売り)からは発注しない (2026-08-12 ユーザー決定)。
    if args.execute and not _isH and not args.allow_stop_entry:
        print("⛔ 発注中止: --entry-mode stop は旧lss(逆指値売り)です。")
        print("   採用しているのは H(指値売り)だけなので、既定の --entry-mode limit を"
              "使ってください。")
        print("   どうしても逆指値で出す日だけ --allow-stop-entry を付けます。")
        return 1

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
    # ⛔ BTソースが無いのに --bt-min>0 だと、全シグナルが BT=0 で落ちて
    #    **1件も発注しない**。朝の発注でこれを黙ってやられると気づけないので、
    #    ここで止める(--bt-min 0 を明示すれば従来どおり全件を出せる)。
    if args.bt_min > 0 and not bt_map:
        print(f"[中止] BTソース {args.trades_csv} が読めないのに --bt-min "
              f"{args.bt_min:.0f} が指定されています。全シグナルが BT=0 で落ちます。\n"
              f"  対処: .\\daily を先に実行して {args.trades_csv} を作るか、"
              f"--bt-min 0 で BT フィルタを外してください。", file=sys.stderr)
        return 1
    print("本日の lss シグナルを収集中...")
    signals = []
    _bt_cut = 0        # BT下限で落とした件数(黙って減らさない)
    for (code, name, strat) in pairs:
        sig = _lss_signal_today(code, name, strat, args.sm, args.tm, args.days)
        if not sig:
            continue
        op = float(sig["order_price"])
        if not (args.min_price <= op <= args.max_price):
            continue
        sig["bt"] = bt_map.get((_norm(sig["symbol"]), strat), 0.0)
        if sig["bt"] < args.bt_min:
            _bt_cut += 1
            continue
        signals.append(sig)

    if _bt_cut:
        print(f"  BT<{args.bt_min:.0f} で {_bt_cut}件を除外 "
              f"(残り{len(signals)}件)。--bt-min 0 で外せます")
    if not signals:
        print("本日の lss シグナル(条件内)なし。終了します。")
        return 0
    # 発注順は lss_order_rank に集約(レポートと同じ並びになるようにする)。
    # 既定=流動性(売買代金)降順。旧既定のBT降順は 18.21 でランダム6本すべてを
    # 下回ると実測(z=-2.22)。LSS_ORDER_RANK=bt で旧挙動に戻せる。
    try:
        import lss_order_rank as _lor
        signals = _lor.sort_signals(signals)
        print(f"[{_lor.describe()}]")
    except Exception as _le:
        print(f"[warn] lss_order_rank 不可({_le}) → BT降順にフォールバック")
        signals.sort(key=lambda s: s["bt"], reverse=True)

    # ── 実際に出す注文価格を先に確定させる ────────────────────────
    # dry-run のプランと実発注が食い違うと「何を出すか」を事前に確認できない。
    # (2026-08-10: auction のとき表示は前日終値のまま、発注だけ -5tick ずれていた)
    #   stop    : 前日終値。発注直前に現値と比べて -1tick 下げることがある(即約定回避)
    #   auction : 前日終値 + limit_ticks × 呼値。現値は見ない(寄指はザラ場で約定しない)
    for s in signals:
        _pc = float(s["order_price"])
        s["_ord_p"] = float(round_to_tick(
            _pc + args.limit_ticks * tick_size(_pc)) if _isH else round_to_tick(_pc))
        # ATR均等: リスク(損切り幅×株数)を --size-target に揃える。
        # ⚠ 単元100株なので高い株は減らせない(1ロットが既に目標超)。1〜10ロット。
        _q = args.qty
        if args.size_mode == "atr":
            _a = float(s.get("atr", 0) or 0)
            _r1 = _a * args.sm * 100
            if _r1 > 0:
                _q = 100 * max(1, min(10, int(round(args.size_target / _r1))))
        s["_qty"] = int(_q)

    # ── over-subscribe: 注文額の累計が cap に達するまで上位から採用 ──
    plan = []
    placed_not = 0.0
    for s in signals:
        note = s["_ord_p"] * s["_qty"]
        if _isH:
            # ⛔ 寄指は寄付で**一斉に約定**するので、枠を超えた分を後から取り消せない。
            #    逆指値のように「1件だけ超過を許す」と、その超過がそのまま建玉になる
            #    (実測 2026-08-10: 枠400万に対し注文額累計 433万)。
            #    超えるものは入れずに次へ(安い銘柄ならまだ枠に入るので break しない)。
            if placed_not + note > cap:
                continue
        elif placed_not >= cap:
            break
        plan.append(s)
        placed_not += note

    print(f"\nシグナル {len(signals)}件 → "
          f"{'発注プラン' if _isH else 'over-subscribe 発注プラン'} {len(plan)}件 "
          f"(注文額累計 {placed_not/1e4:.0f}万 / 枠 {cap/1e4:.0f}万)")
    if _isH:
        print(f"エントリー方式: **{'寄指(寄付の板寄せのみ)' if _auction else '指値(板寄せ+ザラ場到達)'}** "
              f"指値 = 前日終値{args.limit_ticks:+d}ティック")
    print("-" * 72)
    for i, s in enumerate(plan, 1):
        if _isH:
            # H の OCO は **実約定価格(寄り値)基準** で watcher が組み直す。
            # 前日終値基準の stop/target をそのまま出すと誤解を招くので幅で表示。
            _a = float(s.get("atr", 0) or 0)
            _oco = (f"損切+{_a*args.sm:,.0f}/利確-{_a*args.tm:,.0f}(約定値から)"
                    if _a > 0 else "損切/利確=約定値から再計算")
            _px = f"@{'寄指' if _auction else '指値'}{s['_ord_p']:,.0f}"
        else:
            _oco = f"損切¥{s['stop_price']:,.0f}/利確¥{s['target_price']:,.0f}"
            _px = f"@≤{s['_ord_p']:,.0f}"
        print(f"  {i:>2}. BT{s['bt']:>3.0f} {s['symbol']} {s['name']} [{s['strategy']}] "
              f"{_px} {_oco} ({s['_qty']}株 / 注文額 {s['_ord_p']*s['_qty']/1e4:.0f}万)")
    print("-" * 72)
    if _isH and args.budget_multiple > 1.0:
        print(f"⚠ **寄指で倍率{args.budget_multiple:.1f}は危険です。**")
        print("   寄指は寄付の板寄せで**一斉に約定**します。逆指値のように日中かけて順次")
        print("   発動するわけではないので、『約定累計が予算に達したら残りを取消す』という")
        print(f"   over-subscribe の前提が成り立たず、最大 {cap/1e4:.0f}万 が同時に建ちえます。")
        print("   バックテスト(E/Hタブ)も発注額ベースで予算ぶんしか出さない = 倍率1.0相当。")
        print("   → 寄指では --budget-multiple 1.0 を推奨します。")
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

    # ⛔⛔ **記録は発注より先に書く**(2026-08-19)。旧実装は発注が成功してから
    #   書いていたので、その間にプロセスが落ちると **注文だけが板に残り
    #   watcher はそれを知らない** = 無防備で引けまで(今日の実損と同じ型)。
    #   非対称: 記録が無くて注文がある=実損 / 記録があって注文が無い=無害。
    def _log_st(s, trig, st):
        try:
            _log_ordered(s, args.prod, s["_qty"],
                         entry_mode=args.entry_mode, order_price=trig,
                         status=st)
        except Exception as _le:
            print(f"    ⚠ 発注記録({st})の書き込み失敗: {_le}")

    for s in plan:
        sym = _norm(s["symbol"])
        # 注文価格はプラン作成時に確定済み(dry-run の表示と一致させるため)。
        trig = float(s["_ord_p"])
        # ⛔ ここは _auction ではなく _isH で分岐する(2026-08-12 修正)。
        #    旧コードは `if _auction:` だったので、--entry-mode limit が else に落ちて
        #    **逆指値売り(send_stop_sell)** で発注されていた。プラン表示は「指値」なのに
        #    実際は現行lssと同じ注文が飛ぶ = H を試したつもりで lss を出す事故。
        #    直下の order_type が ("limit_moo" if _auction else "limit") になっている
        #    とおり、この分岐はもともと両方を通す想定だった。
        if _isH:
            # ── H案: 寄指売り(寄付の板寄せのみ) / 指値売り(板寄せ+ザラ場) ────
            # 現値との比較(即約定回避)はしない。寄指はザラ場では約定しないので
            # 「現値より上の指値」を出すのがむしろ正しい。
            # 下限ガード(after_hit_price)も逆指値専用なので無い。
            #   auction = 寄指(21): 寄付の板寄せだけ。寄らなければ失効
            #   limit   = 指値(20): 寄りが上なら板寄せ、日中に上がってきても約定
            _log_st(s, trig, "pending")
            res = cli.send_sell(sym, qty=s["_qty"], price=trig,
                                order_type=("limit_moo" if _auction else "limit"),
                                cash_margin=CASH_MARGIN_OPEN)
        else:
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
            # trig はここで確定するので、記録はこの直前に書く
            _log_st(s, trig, "pending")
            res = cli.send_stop_sell(sym, qty=s["_qty"], trigger_price=trig,
                                     cash_margin=CASH_MARGIN_OPEN, after_hit_price=after)
        oid = res.get("OrderId")
        if res.get("Result") == 0 and oid:
            placed.append({"order_id": oid, "symbol": sym, "trigger": float(trig),
                           "qty": s["_qty"], "notional": float(trig) * s["_qty"]})
            print(f"  ✓ {sym} {s['name']} 発注 OrderId={oid} "
                  f"{'@寄指' if _auction else '@≤'}{trig:,.0f}")
            # watcher(.\watch) が決済できるよう ordered_signals_lss.csv に残す。
            # ⛔ auction は OCO を **実約定価格(寄り値)基準** で組み直す必要がある
            #    (板寄せの約定値は指値以上になるので、指値基準の stop だと
            #     約定値より下に来て即損切りになる)。そのため atr/sm/tm も一緒に
            #    書き、watcher 側で再計算する。
            _log_st(s, trig, "ordered")
        else:
            print(f"  ✗ {sym} {s['name']} 発注失敗: {res}")
            _log_st(s, trig, "failed")   # 上で書いた pending を打ち消す

    if not placed:
        print("発注成功0件。監視を行いません。")
        return 1
    print(f"\n発注完了 {len(placed)}件。約定累計が {args.budget/1e4:.0f}万 到達で残りを取消。監視開始…")
    # ★ 発注直後に検算(.\chk と同じ関数)。ここで気付けば寄りまでに出し直せる。
    try:
        from check_orders import verify_row as _vr
        _ng = 0
        for _p2 in placed:
            _s3 = next((x for x in plan if _norm(x["symbol"]) == _p2["symbol"]), None)
            if _s3 is None:
                continue
            _bad = _vr(float(_p2["trigger"]), float(_s3.get("stop_price", 0) or 0),
                       float(_s3.get("target_price", 0) or 0),
                       float(_s3.get("atr", 0) or 0), float(args.sm), float(args.tm),
                       int(_p2["qty"]), args.entry_mode, args.limit_ticks)
            if _bad:
                _ng += 1
                print(f"  ⛔ {_p2['symbol']}: " + " / ".join(_bad))
        print("  [検算] 異常なし" if _ng == 0 else
              f"  [検算] ⛔ {_ng}件に問題。寄りまでに出し直すこと")
    except Exception as _ve:
        print(f"  [検算] スキップ({_ve})")

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
