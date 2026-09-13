"""
dump_history.py — 過去検証の中身を1銘柄単位で丸裸にする検証ツール。

レポート(run_signals_holdout_all)と同じエンジンで、1銘柄×1戦略の全トレードを
1件ずつ表示する。各トレードには「当時のBTスコア(先読みなし)」を付ける。
最後に年別集計(全部 / BT70以上)を出す。

目的:
  - 各トレードの entry/exit 価格を実チャートと照合 → バックテストが本物か検証
  - 当時BTスコアがシグナル日以前の実績だけで計算されているか検証
  - 「2017(選定=in-sample)が良く、2018(OOS)が悪い」= オーバーフィットか検証

使い方:
  python dump_history.py --symbol 7203 --strategy MACD
  python dump_history.py --symbol 6501 --strategy A7 --mode aggressive --days 3334
"""
import argparse
import os
from collections import defaultdict
from datetime import timedelta

ap = argparse.ArgumentParser(description="1銘柄×1戦略の全トレード＋当時BTスコアをダンプ")
ap.add_argument("--symbol", required=True, help="銘柄コード (例: 7203 または 7203.T)")
ap.add_argument("--strategy", required=True, help="戦略名 (例: MACD, A7, RSI2, DON, VOLTF, MOM, A7_S ...)")
ap.add_argument("--mode", default="conservative", choices=["conservative", "aggressive"])
ap.add_argument("--days", type=int, default=3334,
                help="バックテスト日数(既定3334≒2017年5月から。BTスコアは直近1年分の実績で計算)")
args = ap.parse_args()

# TRADING_MODE は signal モジュール import より前に設定する必要がある
os.environ["TRADING_MODE"] = args.mode

import backtest_limit_entry as ble

# ── 戦略名から対応モジュールを特定 ────────────────────────────────────────────
mod = None
for _name in ("check_signals_stop", "check_signals_breakout",
              "check_signals_short", "check_signals_short_breakout"):
    try:
        _o = __import__(_name)
    except Exception:
        continue
    if args.strategy in getattr(_o, "STRATEGY_PARAMS", {}):
        mod = _o
        break
if mod is None:
    print(f"[ERROR] 戦略 {args.strategy} が見つかりません。")
    raise SystemExit(1)

calc_fn, em, sm, tm = mod.STRATEGY_PARAMS[args.strategy]
etype = getattr(mod, "ENTRY_TYPE", "stop_sell" if args.strategy.endswith("_S") else "stop")


def asof_bt(full_log, asof_date, periods=(30, 90, 180, 365)):
    """シグナル日(asof_date)より前に決済したトレードだけで当時BTスコアを計算(先読みなし)。
    レポートの nikkei_analysis._asof_bt_score と同一ロジック。"""
    pr = {}
    for p in periods:
        lo = asof_date - timedelta(days=p)
        n = w = 0
        gp = gl = tot = 0.0
        for t in full_log:
            if t.get("reason") in ("発注中", "保有中"):
                continue
            ed = t.get("exit_dt")
            if ed is None:
                continue
            edd = ed.date() if hasattr(ed, "date") else ed
            if lo <= edd < asof_date:
                pnl = float(t.get("pnl", 0.0))
                n += 1
                tot += pnl
                if pnl > 0:
                    w += 1
                    gp += pnl
                else:
                    gl += -pnl
        if n == 0:
            continue
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[p] = {"trades": n, "win_rate": w / n * 100.0, "pf": pf, "total_pnl": tot}
    if not pr:
        return None
    try:
        return mod.calc_recommend_score(pr)[0]
    except Exception:
        return None


sym = args.symbol if args.symbol.endswith(".T") else args.symbol + ".T"
df = ble.fetch(sym, args.days)
if df is None:
    print(f"[ERROR] {sym} のデータ取得失敗、またはスケール断裂(未調整分割)で除外された銘柄です。")
    raise SystemExit(1)

print(f"データ期間: {df.index[0].date()} 〜 {df.index[-1].date()} ({len(df)}バー)")
res = ble.run_limit_backtest(sym, sym, df, calc_fn, em, sm, tm, args.days,
                             args.strategy, entry_type=etype)
log = [t for t in (res or {}).get("trade_log", [])
       if t.get("reason") not in ("発注中", "保有中") and t.get("exit_dt")]

print(f"\n=== {sym} / {args.strategy} / {args.mode} / 決済 {len(log)} トレード ===\n")
rows = []
for t in log:
    sd = t["signal_dt"].date()
    bt = asof_bt(log, sd)
    rows.append((sd, t, bt))

print(f"{'シグナル日':<12}{'約定日':<12}{'entry':>8} {'決済日':<12}{'exit':>8} "
      f"{'損益':>9} {'当時BT':>6}  理由")
print("-" * 90)
for sd, t, bt in rows:
    print(f"{str(sd):<12}{str(t['entry_dt'].date()):<12}{t.get('entry_p', 0):>8.0f} "
          f"{str(t['exit_dt'].date()):<12}{t.get('exit_p', 0):>8.0f} "
          f"{t['pnl']:>9.0f} {('--' if bt is None else str(bt)):>6}  {t.get('reason', '')}")


def summarize(keep):
    d = defaultdict(lambda: [0, 0, 0.0])   # year -> [wins, n, pnl]
    for sd, t, bt in rows:
        if keep(bt):
            y = sd.strftime("%Y")
            d[y][1] += 1
            d[y][2] += t["pnl"]
            if t["pnl"] > 0:
                d[y][0] += 1
    return d


_all = summarize(lambda b: True)
_bt70 = summarize(lambda b: b is not None and b >= 70)
print("\n=== 年別集計:  全部  |  BT70以上(当時スコア) ===")
print(f"{'年':<6}{'全件':>6}{'勝率':>6}{'損益':>13}   |{'BT70件':>7}{'勝率':>6}{'損益':>13}")
print("-" * 66)
for y in sorted(set(_all) | set(_bt70)):
    a = _all.get(y, [0, 0, 0])
    b = _bt70.get(y, [0, 0, 0])
    awr = a[0] / a[1] * 100 if a[1] else 0
    bwr = b[0] / b[1] * 100 if b[1] else 0
    print(f"{y:<6}{a[1]:>6}{awr:>5.0f}%{a[2]:>+13.0f}   |{b[1]:>7}{bwr:>5.0f}%{b[2]:>+13.0f}")
