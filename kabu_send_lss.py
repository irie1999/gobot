"""
kabu_send_lss.py — 今日の lss シグナルを「信用新規売りの逆指値」で発注する
============================================================================

lss(ロング銘柄ショート = 逆指値空売り)の当日シグナルを収集し、各シグナルの
注文価格(order_price = 前日終値ちょうど。em=0.0)で **信用新規売りの逆指値**
注文を kabuステーションに発注する。

  ロング(kabu_send_signals.py)   : 逆指値『買い』(高値≥注文価格で発動)
  lss  (このスクリプト)          : 逆指値『売り』(安値≤注文価格で発動 = 下落で約定)

lss は同日決済(max_hold=0)戦略なので、エントリー(この発注)が約定したら同日中に
買い戻して手仕舞う必要がある。買い戻し(引け成行 MOC)は建玉が立ってからでないと
出せないため、このスクリプトはまず **エントリー(新規売り)だけ** を出す。
決済(引け成行買戻し)は約定確認後に別途行う(§EXIT 参照)。

【安全設計】(kabu_send_signals.py と同じ)
  - デフォルト dry-run。--execute のときだけ実発注。
  - --execute でも接続先は既定デモ(18081)。本番(18080)は --prod を明示。
  - --symbol / --limit で銘柄数を絞れる(「一軒だけ」テスト発注に使う)。
  - -3% を超えて窓を開けて下落した場合は約定させない下限ガード(after_hit_price)。

使い方:
  # dry-run: 今日の lss シグアルを表示(発注しない)
  python kabu_send_lss.py

  # 一軒だけデモ口座に発注(まずこれで動作確認)
  python kabu_send_lss.py --limit 1 --execute

  # 銘柄を指定して一軒だけ
  python kabu_send_lss.py --symbol 7203 --execute

  # 本番口座に一軒だけ(要 --prod 明示)
  python kabu_send_lss.py --symbol 7203 --execute --prod

  # 銘柄リストを指定(既定は holdout_selected_symbols.py / lss 提案ファイル)
  python kabu_send_lss.py --symbols-file lss_watchlist_proposal_2026-07-15.py
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── TRADING_MODE を import 前に設定(各スクリプトと同じ作法) ──
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
elif "--conservative" in sys.argv:
    os.environ["TRADING_MODE"] = "conservative"

import backtest_limit_entry as ble           # noqa: E402
from sameday5m_core import mod_for            # noqa: E402
from kabu_api import KabuClient, CASH_MARGIN_OPEN  # noqa: E402

JST = timezone(timedelta(hours=9))
FIXED_QTY = ble.FIXED_QTY            # 100株固定(バックテストと同じ前提)
ORDERED_LOG = "ordered_signals_lss.csv"

# lss 既定の損切/利確 ATR 倍率(scan_lss_universe.py と同じ既定)。
DEFAULT_SM = 0.1     # 損切: 注文価格の少し上
DEFAULT_TM = 1.0     # 利確: 注文価格から ATR×1.0 下
# lss の戦略は run_signals_holdout_all が集計する 6 戦略に合わせる。
LSS_STRATEGIES = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _jq_to_yf(code: str) -> str:
    """J-Quants 5桁 → yfinance形式。72030 → 7203.T / 既に .T ならそのまま。"""
    c = str(code).strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _load_symbols(symbols_file: str | None) -> list[tuple]:
    """(code, name, strategy) の一覧。--symbols-file → lss提案 → holdout → WATCHLIST。"""
    cand: list[str] = []
    if symbols_file:
        cand.append(symbols_file)
    # 最新の lss 提案ファイルを自動検出
    props = sorted(Path(".").glob("lss_watchlist_proposal_*.py"))
    if props:
        cand.append(str(props[-1]))
    cand.append("holdout_selected_symbols.py")
    for path in cand:
        p = Path(path)
        if not p.exists():
            continue
        ns: dict = {}
        try:
            exec(p.read_text(encoding="utf-8"), ns)
        except Exception as e:
            print(f"[warn] {path} 読み込み失敗: {e}", file=sys.stderr)
            continue
        sel = ns.get("SELECTED")
        if sel:
            out = [(c, n, s) for (c, n, s) in sel]
            print(f"[info] {path} から {len(out)}ペア読み込み")
            return out
    # フォールバック: WATCHLIST
    import check_signals_stop as _stop
    import check_signals_breakout as _brk
    out = list(_stop.WATCHLIST) + list(_brk.WATCHLIST)
    print(f"[info] WATCHLIST から {len(out)}ペア(フォールバック)")
    return out


def _lss_signal_today(sym: str, name: str, strat: str,
                      sm: float, tm: float, days: int) -> dict | None:
    """当日引け後の lss シグナルを 1 件返す(無ければ None)。

    バックテストエンジン(run_limit_backtest, entry_type="stop_sell", max_hold=0)を
    直接回し、最新バー(=今日の終値)を signal_dt とする「発注中(未発動 pending)」
    注文を拾う。lp/sp/tp はバックテスト・レポートと完全に一致する。
    """
    mod = mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return None
    cf = params[0]                       # calc_fn(指標付与+entry_sig)
    yf_sym = _jq_to_yf(sym)
    try:
        df = ble.fetch(yf_sym, days + 420)   # スコア窓ぶん余分に取る
    except Exception:
        return None
    if df is None or df.empty:
        return None
    try:
        # em=0.0(前日終値ちょうど)/ 同日決済 max_hold=0 で lss を再現。
        r = ble.run_limit_backtest(yf_sym, name, df, cf, 0.0, sm, tm,
                                   days + 420, strat,
                                   entry_type="stop_sell", max_hold=0)
    except Exception:
        return None
    if not r:
        return None
    last_date = df.index[-1]
    last_d = last_date.date() if hasattr(last_date, "date") else last_date
    for t in r.get("trade_log", []):
        if t.get("reason") != "発注中":       # 未発動 pending のみ = 今日置く注文
            continue
        sdt = t.get("signal_dt")
        sd = sdt.date() if hasattr(sdt, "date") else sdt
        if sd != last_d:                      # 最新バー(今日)の新規シグナルだけ
            continue
        lp = float(t.get("order_limit", 0) or 0)   # 逆指値トリガー(=前日終値)
        sp = float(t.get("order_stop", 0) or 0)    # 損切(上側)
        tp = float(t.get("order_target", 0) or 0)  # 利確(下側)
        if lp <= 0 or sp <= 0 or tp <= 0:
            continue
        # kabu へ渡す銘柄コードは .T を外した数値コード(例 4911)。
        # yf_sym(4911.T)はバックテスト取得用で、発注時は数値コードでないと
        # 「銘柄が見つからない」(Code 4002001)になる。
        kabu_code = yf_sym.upper().removesuffix(".T").split(".")[0]
        return {
            "symbol": kabu_code, "name": name, "strategy": strat,
            "family": "lss",
            "order_price": lp, "stop_price": sp, "target_price": tp,
            "signal_date": str(sd),
        }
    return None


def _log_ordered(sig: dict, prod: bool, qty: int) -> None:
    """実発注に成功した lss シグナルを ordered_signals_lss.csv に追記する。"""
    import csv
    now = datetime.now(JST)
    row = {
        "record_date": now.strftime("%Y-%m-%d"),
        "ordered_at":  now.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":      str(sig.get("symbol", "")).upper().removesuffix(".T"),
        "name":        sig.get("name", ""),
        "strategy":    sig.get("strategy", ""),
        "family":      "lss",
        "side":        "short",
        "order_price": round(float(sig.get("order_price", 0) or 0)),
        "stop_price":  round(float(sig.get("stop_price", 0) or 0)),
        "target_price": round(float(sig.get("target_price", 0) or 0)),
        "qty":         qty,
        "prod":        int(bool(prod)),
        "cash_margin": CASH_MARGIN_OPEN,
    }
    p = Path(ORDERED_LOG)
    new = not p.exists()
    try:
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"  ⚠ 発注記録の書き込み失敗 ({e})")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="今日の lss シグナルを信用新規売りの逆指値で発注する")
    ap.add_argument("--execute", action="store_true",
                    help="実際に発注する (未指定なら dry-run)")
    ap.add_argument("--prod", action="store_true",
                    help="本番口座(18080)に接続 (未指定ならデモ18081)")
    ap.add_argument("--symbol", default=None,
                    help="この銘柄コードだけ発注 (例: 7203)。指定時は他を無視")
    ap.add_argument("--strat", default=None,
                    help="--symbol と併用: 戦略を固定 (例: RSI2)")
    ap.add_argument("--limit", type=int, default=None,
                    help="発注する最大件数 (「一軒だけ」なら --limit 1)")
    ap.add_argument("--symbols-file", default=None,
                    help="(code,name,strategy) の SELECTED を持つ .py")
    ap.add_argument("--budget", type=float, default=None,
                    help="1銘柄あたりの投入上限(円)。order_price×100 超の銘柄はスキップ")
    ap.add_argument("--qty", type=int, default=FIXED_QTY,
                    help=f"株数 (既定 {FIXED_QTY})")
    ap.add_argument("--sm", type=float, default=DEFAULT_SM,
                    help=f"損切ATR倍率 (既定 {DEFAULT_SM})")
    ap.add_argument("--tm", type=float, default=DEFAULT_TM,
                    help=f"利確ATR倍率 (既定 {DEFAULT_TM})")
    ap.add_argument("--days", type=int, default=60,
                    help="シグナル判定に使う日足ルックバック日数 (既定 60)")
    ap.add_argument("--gap-guard", type=float, default=0.03,
                    help="逆指値発動後の下限ガード率。-この%%超の窓開けは約定させない (既定 0.03=3%%)")
    ap.add_argument("--no-gap-guard", action="store_true",
                    help="下限ガードを外す (発動後は成行)")
    ap.add_argument("--intraday", action="store_true",
                    help="場中発注用: 現在値≤トリガーなら現在値-1ティックに引き下げ即約定を回避"
                         "(既定OFF=引け後運用でトリガー=終値のまま)")
    ap.add_argument("--aggressive", action="store_true", help="aggressive モード")
    ap.add_argument("--conservative", action="store_true", help="conservative モード (既定)")
    args = ap.parse_args()

    env_label = "本番(18080)" if args.prod else "デモ(18081)"
    mode_label = "★実発注★" if args.execute else "dry-run (発注なし)"

    now = datetime.now(JST)
    print("=" * 64)
    print(f"lss(逆指値空売り)発注  {now:%Y-%m-%d %H:%M JST}")
    print(f"モード: {mode_label}  /  接続先: {env_label}  /  信用新規売り")
    print(f"sm={args.sm} / tm={args.tm} / qty={args.qty} / ルックバック{args.days}日")
    print("=" * 64)

    # ── 対象銘柄 ──
    if args.symbol:
        strat = args.strat or "RSI2"
        pairs = [(args.symbol, args.symbol, strat)]
        print(f"[info] --symbol 指定: {args.symbol} / 戦略 {strat}")
    else:
        pairs = _load_symbols(args.symbols_file)

    print("本日の lss シグナルを収集中...")
    signals: list[dict] = []
    for (code, name, strat) in pairs:
        sig = _lss_signal_today(code, name, strat, args.sm, args.tm, args.days)
        if sig:
            signals.append(sig)

    if not signals:
        print("本日の lss シグナルなし。終了します。")
        return 0

    # 予算フィルター
    if args.budget:
        kept = []
        for s in signals:
            cost = s["order_price"] * args.qty
            if cost > args.budget:
                print(f"  skip {s['symbol']} {s['name']}: "
                      f"必要資金 {cost:,.0f}円 > 予算 {args.budget:,.0f}円")
                continue
            kept.append(s)
        signals = kept
    if not signals:
        print("予算条件を満たすシグナルなし。終了します。")
        return 0

    # 件数制限(「一軒だけ」)
    if args.limit is not None and args.limit >= 0:
        if len(signals) > args.limit:
            print(f"[info] {len(signals)}件中 先頭 {args.limit}件だけ発注します。")
        signals = signals[:args.limit]

    print(f"発注対象シグナル: {len(signals)} 件\n")

    cli = KabuClient(prod=args.prod, dry_run=not args.execute)
    if args.execute:
        try:
            cli.connect()
        except Exception as e:
            print(f"✗ kabu 接続失敗: {e}")
            return 1

    ok = 0
    for s in signals:
        sym, name = str(s["symbol"]).split(".")[0], s["name"]   # kabuは数値コード(.T無し)
        order_p, stop_p, tgt_p = s["order_price"], s["stop_price"], s["target_price"]
        # トリガーは呼値(ティック)に丸めた終値。kabu は無効な呼値を『下方向』に floor する
        # (例: 端数の3,024を送ると3,020に丸められる)ため、必ず最寄りの有効呼値に丸める
        # (資生堂4911の終値3,024.x → round_to_tick 3,025 = 実際の終値)。
        from backtest_limit_entry import tick_size, round_to_tick
        trig = round_to_tick(order_p)
        adj_note = ""
        # 【既定=引け後運用】トリガー=終値のまま。場中に発注して現在値≤トリガーだと
        # 即約定で弾かれる(kabu Code 100217)が、引け後発注では起きないので何もしない。
        # 場中に発注する場合のみ --intraday を付けると、現在値-1ティックに引き下げて
        # 即約定弾きを回避する(その分トリガーは終値からずれる)。
        if args.execute and getattr(args, "intraday", False):
            try:
                cur = cli.get_current_price(sym)
            except Exception:
                cur = None
            if cur and cur > 0 and trig >= cur:
                trig = round_to_tick(cur - tick_size(cur))
                adj_note = f" ※現値{cur:,.0f}≦トリガー→{trig:,.0f}に引下げ(--intraday)"
        # 発動後の指値下限(-3%)も呼値に丸める(端数だと kabu が下方向に丸めるため)。
        after = None if args.no_gap_guard else round_to_tick(trig * (1.0 - args.gap_guard))
        guard_label = "成行" if after is None else f"下限¥{after:,.0f}(-{args.gap_guard*100:.0f}%)"
        print(f"・{sym} {name} [{s['strategy']}] 新規売り逆指値 "
              f"@≤{trig:,.0f}{adj_note}  損切¥{stop_p:,.0f} / 利確¥{tgt_p:,.0f} / 発動後{guard_label}")
        res = cli.send_stop_sell(sym, qty=args.qty, trigger_price=trig,
                                 cash_margin=CASH_MARGIN_OPEN,
                                 after_hit_price=after)
        if res.get("Result") == 0:
            ok += 1
            if args.execute:
                _log_ordered(s, args.prod, args.qty)

    print(f"\nエントリー(新規売り)発注完了: {ok}/{len(signals)} 件成功")
    if not args.execute:
        print("dry-run のため実発注していません。--execute で発注します。")
    print("─" * 64)
    print("【EXIT 注意】lss は同日決済です。エントリーが約定したら、同日中に")
    print("  引け成行で買い戻して手仕舞ってください:")
    print("    cli.send_moc(<symbol>, qty, side='buy', cash_margin=CASH_MARGIN_CLOSE)")
    print("  ※ 買戻しは建玉が立ってからでないと出せないため、約定確認後に実行。")
    print("  ※ 別途 close_stop_guard 相当の引け決済スクリプトを用意予定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
