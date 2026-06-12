"""
position_stop_guard.py — 実建玉ベースの close 方式 損切りガード
================================================================

close_stop_guard.py が forward_test_log.csv を保有源にするのに対し、
本スクリプトは **kabuステーションの実建玉 (get_positions)** を保有源にする。

【核心: 建玉とシグナルのマッチング】
  実建玉は「銘柄コード・取得単価・数量」しか持たないため、どのシグナル
  (どの戦略・どの日) で入ったのかが分からない。そこで:

    1. 各建玉の銘柄について、WATCHLIST にある全戦略で過去 N 営業日分の
       シグナルを再計算する (check_signals_stop / breakout と同一の数式)。
    2. 建玉の **取得単価がシグナルの逆指値価格 (order_price) と一致** する
       シグナルを探す (許容差 --tolerance %)。
    3. 一致したシグナルの stop_price を、その建玉の損切りラインとして採用する。
    4. 一致するシグナルが無い建玉は「システム由来か確認できない」ため
       **売り判断をしない** (安全側)。

  これにより「シグナルの逆指値と必ず一致していることを確認してから売る」
  という要件を満たす。

【判定と発注】
  - 現在値 (kabu /board ≒ 引け値) <= stop_price (ロング) なら損切り抵触。
  - 抵触した建玉を引け成行 (MOC) で決済する (close 方式 §16)。
  - 現物は現物売り、信用は信用返済 (ClosePositions 自動取得)。

【安全設計】
  - デフォルト dry-run。--execute のときだけ実発注。
  - --execute でも接続先は既定デモ(18081)。本番は --prod 明示。
  - 一致しない建玉・損切り未抵触の建玉には何もしない。

【運用タイミング】
  毎営業日 14:50〜14:55 JST に実行 (引け成行が当日引けで約定する)。

使い方:
  python position_stop_guard.py --prod                 # dry-run (判定のみ)
  python position_stop_guard.py --prod --execute       # 本番に引け成行を発注
  python position_stop_guard.py --prod --tolerance 2   # 取得単価の一致許容差 2%
  python position_stop_guard.py --prod --lookback 30   # シグナル探索 30営業日
  python position_stop_guard.py --prod --sell-type market  # 引け成行でなく成行
  python position_stop_guard.py --aggressive --prod    # aggressive のシグナルで判定
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

# ── TRADING_MODE を import 前に設定 (各スクリプトと同じ作法) ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

from kabu_api import (KabuClient, CASH_GENBUTSU, CASH_MARGIN_CLOSE,  # noqa: E402
                      SIDE_BUY, SIDE_SELL)
from backtest_limit_entry import (  # noqa: E402
    fetch, round_to_tick, LIMIT_ENTRY_MARGIN_PCT)

import check_signals_stop as _stop          # noqa: E402
import check_signals_breakout as _brk        # noqa: E402

JST = timezone(timedelta(hours=9))


# ────────────────────────────────────────────────────────────
# WATCHLIST から 銘柄→[(戦略, モジュール)] と 銘柄→名前 を構築
# ────────────────────────────────────────────────────────────
def build_symbol_index() -> tuple[dict, dict]:
    """両 WATCHLIST を走査して
      strat_map: "2586" -> [("RSI2", _stop), ("DON", _brk), ...]
      name_map : "2586" -> "銘柄名"
    を返す。symbol は kabu 形式 (.T なし) をキーにする。
    """
    strat_map: dict[str, list[tuple[str, object]]] = {}
    name_map: dict[str, str] = {}
    for module in (_stop, _brk):
        for sym_t, name, strategy in module.WATCHLIST:
            code = sym_t.replace(".T", "")
            strat_map.setdefault(code, []).append((strategy, module))
            name_map.setdefault(code, name)
    return strat_map, name_map


# ────────────────────────────────────────────────────────────
# 過去 N 営業日のシグナルを再計算 (check_signal_on_date と同一数式)
# ────────────────────────────────────────────────────────────
def recent_signals(module, symbol_t: str, strategy: str,
                   lookback: int) -> list[dict]:
    """直近 lookback 営業日に出たエントリーシグナルを列挙する。

    check_signals_stop/breakout.check_signal_on_date と同じ式:
      order_p = close_prev + atr*em ; stop = order_p - atr*sm ; target = order_p + atr*tm
    を、1 回の fetch+calc で範囲計算する (毎日 1 回呼ぶより効率的)。
    """
    calc_fn, em, sm, tm = module.STRATEGY_PARAMS[strategy]
    df = fetch(symbol_t, 365)
    if df is None or len(df) < 5:
        return []
    try:
        df = calc_fn(df)
    except Exception:
        return []

    out: list[dict] = []
    n = len(df)
    for i in range(max(0, n - lookback), n):
        row = df.iloc[i]
        if not bool(row.get("entry_sig", False)):
            continue
        atr_v = float(row.get("atr", 0))
        if atr_v <= 0:
            continue
        close_prev = float(row["close"])
        order_p = close_prev + atr_v * em
        sl = order_p - atr_v * sm
        tp = order_p + atr_v * tm
        limit_entry = order_p * (1.0 + LIMIT_ENTRY_MARGIN_PCT)
        sig_dt = df.index[i]
        out.append({
            "strategy": strategy,
            "signal_date": (sig_dt.strftime("%Y-%m-%d")
                            if hasattr(sig_dt, "strftime") else str(sig_dt)),
            "order_price": round_to_tick(order_p),
            "limit_entry_price": round_to_tick(limit_entry),
            "stop_price": round_to_tick(sl),
            "target_price": round_to_tick(tp),
        })
    return out


def match_signal(acq_price: float, signals: list[dict],
                 tolerance_pct: float) -> dict | None:
    """取得単価に最も近い order_price のシグナルを返す (許容差内のみ)。

    逆指値買いは order_price 以上で約定するため、取得単価は通常
    [order_price, limit_entry_price] の帯に入る。許容差 tolerance_pct を
    両側に見て、最も近いものを採用する。
    """
    best = None
    best_diff = None
    for s in signals:
        op = s["order_price"]
        if op <= 0:
            continue
        diff = abs(acq_price - op) / op * 100.0
        # 約定帯 [order_price, limit_entry_price] 内なら一致とみなす(差0扱い)
        in_band = op <= acq_price <= max(op, s["limit_entry_price"])
        eff_diff = 0.0 if in_band else diff
        if eff_diff <= tolerance_pct and (best_diff is None or eff_diff < best_diff):
            best = dict(s, match_diff_pct=round(diff, 2), in_band=in_band)
            best_diff = eff_diff
    return best


# ────────────────────────────────────────────────────────────
# 建玉の読み込み (kabu 実建玉)
# ────────────────────────────────────────────────────────────
def load_kabu_positions(cli: KabuClient, product: int) -> list[dict]:
    """kabu の実建玉を正規化して返す。product: 0=すべて 1=現物 2=信用。"""
    raw = cli.get_positions(product=product)
    out: list[dict] = []
    for p in raw:
        leaves = int(float(p.get("LeavesQty") or 0))
        if leaves <= 0:
            continue
        side = str(p.get("Side", ""))   # "1"=売(ショート) "2"=買(ロング)
        # 現物(product=1)はロングのみ。信用は Side で判定。
        is_margin = product == 2 or (product == 0 and str(p.get("MarginTradeType", "")) not in ("", "0"))
        out.append({
            "symbol": str(p.get("Symbol", "")).strip(),
            "acq_price": float(p.get("Price") or 0),
            "qty": leaves,
            "is_short": side == SIDE_SELL,
            "is_margin": is_margin,
            "profit_loss": p.get("ProfitLoss"),
        })
    return out


# ────────────────────────────────────────────────────────────
# 決済発注
# ────────────────────────────────────────────────────────────
def close_position(cli: KabuClient, pos: dict, sell_type: str) -> bool:
    """建玉を決済する。sell_type: moc=引け成行 / market=成行 / limit は未対応。"""
    side = "buy" if pos["is_short"] else "sell"
    cm = CASH_MARGIN_CLOSE if pos["is_margin"] else CASH_GENBUTSU
    label = "信用返済" if pos["is_margin"] else "現物"
    print(f"    → {label} {('成行' if sell_type=='market' else '引け成行')}({side})")
    if sell_type == "market":
        kabu_side = SIDE_BUY if pos["is_short"] else SIDE_SELL
        # 成行決済: 信用は send_sell が ClosePositions を自動取得
        if pos["is_short"]:
            # ショート買い戻しの成行は send_buy(返済) になるが現状ロング前提のため未使用
            res = cli.send_buy(pos["symbol"], qty=pos["qty"],
                               cash_margin=cm, order_type="market")
        else:
            res = cli.send_sell(pos["symbol"], qty=pos["qty"],
                                cash_margin=cm, order_type="market")
    else:
        res = cli.send_moc(pos["symbol"], qty=pos["qty"], side=side, cash_margin=cm)
    return res.get("Result") == 0


# ────────────────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="実建玉ベースの close 方式 損切りガード (シグナル一致確認付き)")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run 判定のみ)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--tolerance", type=float, default=3.0,
                    help="取得単価とシグナル逆指値の一致許容差(%%) 既定3.0")
    ap.add_argument("--lookback", type=int, default=25,
                    help="シグナルを遡って探索する営業日数 既定25 (MAX_HOLD+余裕)")
    ap.add_argument("--product", choices=["all", "cash", "margin"], default="all",
                    help="対象建玉: all(既定) / cash(現物) / margin(信用)")
    ap.add_argument("--sell-type", choices=["moc", "market"], default="moc",
                    help="決済方法: moc=引け成行(既定) / market=成行")
    ap.add_argument("--aggressive", action="store_true",
                    help="aggressive のシグナルで判定")
    ap.add_argument("--conservative", action="store_true",
                    help="conservative のシグナルで判定 (既定)")
    args = ap.parse_args()

    product = {"all": 0, "cash": 1, "margin": 2}[args.product]
    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"
    mode = os.environ.get("TRADING_MODE", "conservative")

    now = datetime.now(JST)
    print("=" * 70)
    print(f"実建玉 close 損切りガード  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label}  /  接続先: {env_label}  /  トレード: {mode}")
    print(f"一致許容差: {args.tolerance}%  探索: 過去{args.lookback}営業日  "
          f"決済: {'引け成行' if args.sell_type=='moc' else '成行'}")
    print("=" * 70)

    # kabu は建玉取得に接続が必須 (dry-run でも connect する。発注のみ dry_run 制御)
    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    try:
        cli.connect()
        print(f"✓ 接続成功 ({cli.env_label})\n")
    except Exception as e:
        print(f"✗ kabu 接続失敗: {e}")
        return 1

    positions = load_kabu_positions(cli, product)
    if not positions:
        print("保有建玉なし。終了します。")
        return 0
    print(f"保有建玉: {len(positions)} 件\n")

    strat_map, name_map = build_symbol_index()

    breached: list[dict] = []
    for pos in positions:
        code = pos["symbol"]
        name = name_map.get(code, "")
        symbol_t = f"{code}.T"
        acq = pos["acq_price"]

        # この銘柄を監視している全戦略のシグナルを集める
        entries = strat_map.get(code, [])
        all_sigs: list[dict] = []
        for strategy, module in entries:
            all_sigs.extend(recent_signals(module, symbol_t, strategy, args.lookback))

        if not entries:
            print(f"  ⚠ {code} {name}: WATCHLIST 外の建玉 → 売り判断しません "
                  f"(取得単価={acq:.1f})")
            continue
        if not all_sigs:
            print(f"  ⚠ {code} {name}: 直近{args.lookback}営業日にシグナルなし → "
                  f"売り判断しません (取得単価={acq:.1f})")
            continue

        matched = match_signal(acq, all_sigs, args.tolerance)
        if matched is None:
            print(f"  ⚠ {code} {name}: 取得単価={acq:.1f} に一致するシグナルなし "
                  f"(候補 order={[s['order_price'] for s in all_sigs]}) → 売り判断しません")
            continue

        # 現在値 (≒引け値) を取得
        price = cli.get_current_price(code)
        if price is None:
            print(f"  ? {code} {name}: 現在値取得不可 → スキップ")
            continue

        sp = matched["stop_price"]
        hit = (price >= sp) if pos["is_short"] else (price <= sp)
        side_label = "信用" if pos["is_margin"] else "現物"
        mark = "🔴損切り" if hit else "  保有継続"
        band = "帯内" if matched["in_band"] else f"差{matched['match_diff_pct']}%"
        print(f"  {mark}  {code} {name} [{matched['strategy']}/{side_label}] "
              f"取得={acq:.1f}↔逆指値={matched['order_price']:.1f}({band}) "
              f"現在値={price:.1f} 損切り={sp:.1f} "
              f"(シグナル{matched['signal_date']})")
        if hit:
            breached.append(dict(pos, name=name, stop_price=sp,
                                 strategy=matched["strategy"]))

    print()
    if not breached:
        print("損切りライン抵触なし。発注なし。")
        return 0

    print(f"損切り抵触: {len(breached)} 件")
    if not args.execute:
        print("dry-run のため発注しません。--execute で実発注します。")
        return 0

    print(f"{('引け成行' if args.sell_type=='moc' else '成行')}決済を "
          f"{env_label} に発注します...")
    ok = 0
    for pos in breached:
        print(f"・{pos['symbol']} {pos['name']} x{pos['qty']}")
        if close_position(cli, pos, args.sell_type):
            ok += 1
    print(f"\n発注完了: {ok}/{len(breached)} 件成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
