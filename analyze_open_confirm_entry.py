"""
analyze_open_confirm_entry.py ― 「寄り30分確認」エントリーの5分足検証 (スイング)
================================================================================
逆指値ブレイク買い(現行=A)と「寄り付きN分後に前日終値=トリガを超えていたら買う」
(確認エントリー=B) を、実5分足で比較する。

■ 検証の勘所（なぜ日足バックテストを再利用できるか）
  損切り/目標は『シグナル由来の絶対価格』なので、エントリー価格を変えても
  「いつ・どの価格で決済するか」は変わらない。つまり5分足が必要なのは
  『各トレードのエントリー日の 09:00+N分 時点の株価』を知るためだけ。
  保有〜決済は既存の run_limit_backtest の結果(exit_p/reason)をそのまま使う。

■ A(現行 逆指値) と B(確認) の違い
  A: 高値>=トリガで約定。約定価格 entry_p は trade_log の値(ギャップ/スリッページ込み)。
  B: 約定日の 09:30 時点株価 P を見る。
       P >  トリガ → 買う。約定価格 = P*(1+スリッページ)。決済は A と同一。
       P <= トリガ → 当日は見送り(=このトレードをスキップ)。
     → 寄りのスパイクで即反落したダマシ(9:30にはトリガ割れ)を除外できる。
     → 代償: 本物のブレイクは 9:30 の高い株価で買うため建値が不利になりうる。

■ 5分足データ
  1) --minute-dir DIR を指定すると DIR/{code}.csv (datetime,open,high,low,close) を読む
     (お手元のデイトレ5分足を流用したい場合に使用。長期履歴が使える)
  2) 無指定なら yfinance で 5分足を取得 (interval=5m は約60日しか遡れない点に注意)

■ 最適な確認時刻を探す
  「9:30固定」ではなく、寄りから何分後に確認するのが最良かを複数時刻でスイープし、
  A(現行)比で損益が最大の時刻を提示する。--sweep で候補時刻(分)を指定。

使い方:
  python analyze_open_confirm_entry.py                       # 既定スイープ(0,15,30,...,240分)
  python analyze_open_confirm_entry.py --sweep 15,30,60,90   # 候補時刻を絞る
  python analyze_open_confirm_entry.py --confirm-min 30      # 単一時刻だけ詳細評価
  python analyze_open_confirm_entry.py --margin-pct 0.3      # トリガ+0.3%超で確認(ダマシ厳格化)
  python analyze_open_confirm_entry.py --minute-dir data/minute_5m   # デイトレ5分足を流用
  python analyze_open_confirm_entry.py --aggressive
  python analyze_open_confirm_entry.py --days 60             # 直近何日のトレードを対象にするか

注意: この環境では yfinance 未導入・5分足データ無しのため、ローカルでの実行を想定。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --aggressive を import 前に反映
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
os.environ.setdefault("TRADING_MODE", "conservative")

import pandas as pd

import check_signals_stop as _stop
import check_signals_breakout as _brk
from backtest_limit_entry import (
    run_limit_backtest, fetch, tick_size,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, FIXED_QTY,
)

JST = timezone(timedelta(hours=9))


def _round_tick_up(p: float) -> int:
    t = tick_size(float(p))
    import math
    return int(math.ceil(p / t)) * t


# ── 5分足ローダー ────────────────────────────────────────────────────────────
_MIN_CACHE: dict[str, pd.DataFrame | None] = {}


def _load_minute(symbol: str, minute_dir: str | None) -> pd.DataFrame | None:
    """5分足を JST index の DataFrame(open/high/low/close) で返す。失敗時 None。"""
    if symbol in _MIN_CACHE:
        return _MIN_CACHE[symbol]
    df = None
    code = symbol.replace(".T", "")
    # 1) ローカル CSV (デイトレ5分足流用)
    if minute_dir:
        for cand in (f"{code}.csv", f"{symbol}.csv", f"{code}.T.csv"):
            p = Path(minute_dir) / cand
            if p.exists():
                try:
                    d = pd.read_csv(p)
                    tcol = next((c for c in d.columns
                                 if c.lower() in ("datetime", "date", "timestamp", "time")), None)
                    if tcol is None:
                        break
                    d[tcol] = pd.to_datetime(d[tcol])
                    d = d.set_index(tcol)
                    d.columns = [c.lower() for c in d.columns]
                    df = d[["open", "high", "low", "close"]].copy()
                except Exception as e:
                    print(f"  ⚠ {symbol}: CSV読込失敗 {e}")
                break
    # 2) yfinance フォールバック
    if df is None:
        try:
            import yfinance as yf
            raw = yf.download(symbol, interval="5m", period="60d",
                              progress=False, auto_adjust=False)
            if raw is not None and not raw.empty:
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = [c[0] for c in raw.columns]
                raw.columns = [c.lower() for c in raw.columns]
                df = raw[["open", "high", "low", "close"]].copy()
        except Exception as e:
            print(f"  ⚠ {symbol}: yfinance 5分足取得失敗 {e}")
            df = None
    # index を JST に統一
    if df is not None and not df.empty:
        idx = pd.DatetimeIndex(df.index)
        try:
            idx = idx.tz_convert(JST) if idx.tz is not None else idx.tz_localize("Asia/Tokyo")
        except Exception:
            try:
                idx = idx.tz_localize("UTC").tz_convert(JST)
            except Exception:
                pass
        df.index = idx
    _MIN_CACHE[symbol] = df
    return df


def _price_at(symbol: str, day, confirm_min: int, minute_dir: str | None):
    """entry_dt(日付)の 09:00+confirm_min 時点の株価。取れなければ None。"""
    df = _load_minute(symbol, minute_dir)
    if df is None or df.empty:
        return None
    d = day.date() if hasattr(day, "date") else day
    try:
        sub = df[(df.index.date == d)]
    except Exception:
        return None
    if sub.empty:
        return None
    target_t = (datetime(d.year, d.month, d.day, 9, 0, tzinfo=JST)
                + timedelta(minutes=confirm_min))
    # target 時刻以降で最初のバーの open を「その時点の株価」とみなす
    after = sub[sub.index >= target_t]
    if not after.empty:
        return float(after.iloc[0]["open"])
    # target 時刻のバーが無ければ当日最後のバーの close
    return float(sub.iloc[-1]["close"])


# ── 対象トレード収集 ─────────────────────────────────────────────────────────
def _collect_trades(days: int):
    """スイングWATCHLIST全戦略をバックテストし、決済済みロング取引を返す。"""
    fams = [
        (_stop, getattr(_stop, "WATCHLIST", []), _stop.STRATEGY_PARAMS,
         getattr(_stop, "ENTRY_TYPE", "stop")),
        (_brk, getattr(_brk, "WATCHLIST", []), _brk.STRATEGY_PARAMS,
         getattr(_brk, "ENTRY_TYPE", "stop")),
    ]
    cutoff = datetime.now(JST).date() - timedelta(days=days)
    out = []
    for mod, wl, params, etype in fams:
        for sym, name, strat in wl:
            if strat not in params:
                continue
            calc_fn, em, sm, tm = params[strat]
            df = fetch(sym, 400)
            if df is None:
                continue
            r = run_limit_backtest(sym, name, df, calc_fn, em, sm, tm,
                                   365, strat, entry_type=etype)
            if not r:
                continue
            for t in r.get("trade_log", []):
                if t.get("reason") in ("発注中", "保有中"):
                    continue
                edt = t.get("entry_dt")
                if edt is None:
                    continue
                edate = edt.date() if hasattr(edt, "date") else edt
                if edate < cutoff:
                    continue
                out.append(dict(
                    symbol=sym, name=name, strategy=strat,
                    entry_dt=edt, entry_p=t["entry_p"], exit_p=t["exit_p"],
                    trigger=t.get("order_limit"), qty=t.get("qty", FIXED_QTY),
                    reason=t.get("reason", ""), pnl_a=t["pnl"],
                ))
    return out


def _pnl_long(entry_p: float, exit_p: float, qty: int) -> float:
    fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY
    return (exit_p - entry_p) * qty - fee


def _stat(lst, key):
    n = len(lst)
    if n == 0:
        return (0, 0.0, 0.0)
    w = sum(1 for x in lst if (x[key] or 0) > 0)
    pnl = sum((x[key] or 0) for x in lst)
    return (n, w / n * 100, pnl)


def _eval_time(rows, confirm_min, margin_pct):
    """指定した確認時刻での B 成績を算出。rows は price_map に各時刻の株価を持つ。"""
    taken, skipped, both_prem = [], [], []
    for r in rows:
        p = r["price_map"].get(confirm_min)
        if p is None:
            continue
        thr = r["trigger"] * (1.0 + margin_pct / 100.0)
        if p > thr:
            entry_b = _round_tick_up(p * (1.0 + SLIPPAGE_STOP_PCT))
            pnl_b = _pnl_long(entry_b, r["exit_p"], r["qty"])
            taken.append({**r, "pnl_b": pnl_b})
            both_prem.append(entry_b - r["entry_p"])
        else:
            skipped.append(r)
    bn, bwr, bpnl = _stat(taken, "pnl_b")
    sn, swr, spnl = _stat(skipped, "pnl_a")
    prem = (sum(both_prem) / len(both_prem)) if both_prem else 0.0
    return dict(confirm_min=confirm_min, taken=taken, skipped=skipped,
                bn=bn, bwr=bwr, bpnl=bpnl, sn=sn, swr=swr, spnl=spnl, prem=prem)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default="0,15,30,45,60,90,120,150,180,240",
                    help="評価する確認時刻(寄り09:00からの分)をカンマ区切り。"
                         "最良の時刻を探す。0=寄り(09:00)そのもの")
    ap.add_argument("--confirm-min", type=int, default=None,
                    help="単一時刻だけ詳細評価したい場合に指定 (--sweep を無視)")
    ap.add_argument("--margin-pct", type=float, default=0.0,
                    help="確認しきい値: トリガ×(1+margin%%)超で買う (既定0=トリガ超)")
    ap.add_argument("--minute-dir", default=None,
                    help="5分足CSVディレクトリ ({code}.csv)。無指定はyfinance(60日)")
    ap.add_argument("--days", type=int, default=400,
                    help="対象トレードのエントリー日を直近何日に絞るか (既定400=ほぼ全期間)")
    ap.add_argument("--aggressive", action="store_true")
    args = ap.parse_args()

    if args.confirm_min is not None:
        times = [args.confirm_min]
    else:
        times = [int(x) for x in args.sweep.split(",") if x.strip() != ""]

    mode = os.environ.get("TRADING_MODE", "conservative")
    print("=" * 82)
    print(f"寄り後 確認エントリー検証(最適時刻スイープ)  mode={mode}  "
          f"margin={args.margin_pct}%")
    print(f"確認時刻(分): {times}   5分足={'CSV:'+args.minute_dir if args.minute_dir else 'yfinance(60日)'}")
    print("=" * 82)

    trades = _collect_trades(args.days)
    print(f"対象決済済みロング取引: {len(trades)}件 (WATCHLIST全戦略)")

    # 各トレードについて、全候補時刻の株価をまとめて取得 (5分足は1回ロード)
    rows, no_data = [], 0
    for t in trades:
        trig = t["trigger"]
        if not trig or trig <= 0:
            continue
        pm = {}
        for cm in times:
            p = _price_at(t["symbol"], t["entry_dt"], cm, args.minute_dir)
            if p is not None:
                pm[cm] = p
        if not pm:
            no_data += 1
            continue
        rows.append(dict(**t, price_map=pm))

    cover = len(rows)
    print(f"5分足が取得できた取引: {cover}件 / データ無し {no_data}件"
          f"{' (yfinanceは約60日のみ)' if not args.minute_dir else ''}\n")
    if cover == 0:
        print("※ 5分足が1件も取れませんでした。--minute-dir でCSVを指定するか、"
              "yfinance導入・直近60日での実行をご確認ください。")
        return

    an, awr, apnl = _stat(rows, "pnl_a")
    print("── ベースライン ──────────────────────────────────────────────")
    print(f"  A(現行 逆指値): {an:3d}件  勝率{awr:5.1f}%  損益 {apnl:+12,.0f}円\n")

    # 時刻スイープ
    results = [_eval_time(rows, cm, args.margin_pct) for cm in times]
    print("── 確認時刻スイープ (B=その時刻に前日終値超えを確認して買う) ──────────")
    print(f"  {'確認時刻':>8}{'B件':>5}{'B勝率':>7}{'B損益':>13}"
          f"{'vs A':>13}{'見送件':>6}{'見送A損益':>13}{'建値差':>8}")
    best = None
    for r in results:
        hhmm = f"09:{r['confirm_min']:02d}" if r['confirm_min'] < 60 else f"{9 + r['confirm_min']//60}:{r['confirm_min']%60:02d}"
        vs = r["bpnl"] - apnl
        print(f"  {hhmm:>8}{r['bn']:>5}{r['bwr']:>6.1f}%{r['bpnl']:>+13,.0f}"
              f"{vs:>+13,.0f}{r['sn']:>6}{r['spnl']:>+13,.0f}{r['prem']:>+8.0f}")
        if best is None or r["bpnl"] > best["bpnl"]:
            best = r

    bh = best["confirm_min"]
    hhmm = f"09:{bh:02d}" if bh < 60 else f"{9 + bh//60}:{bh%60:02d}"
    print(f"\n★ 最良の確認時刻: {hhmm} (寄り+{bh}分)  "
          f"B損益 {best['bpnl']:+,.0f}円 (A比 {best['bpnl']-apnl:+,.0f}円)")

    # 最良時刻の戦略別内訳
    print("\n── 最良時刻の戦略別 (A損益 / B損益 / 見送り件数) ──────────────")
    print(f"  {'戦略':<8}{'A件':>5}{'A勝率':>7}{'A損益':>12}"
          f"{'B件':>5}{'B勝率':>7}{'B損益':>12}{'見送':>5}")
    taken_by = {id(x): x for x in best["taken"]}
    strats = sorted({r["strategy"] for r in rows})
    for s in strats:
        rs = [r for r in rows if r["strategy"] == s]
        ts = [taken_by[id(r)] for r in rs if id(r) in taken_by]
        a_n, a_wr, a_p = _stat(rs, "pnl_a")
        b_n, b_wr, b_p = _stat(ts, "pnl_b")
        print(f"  {s:<8}{a_n:>5}{a_wr:>6.1f}%{a_p:>+12,.0f}"
              f"{b_n:>5}{b_wr:>6.1f}%{b_p:>+12,.0f}{a_n-b_n:>5}")

    print("\n読み方:")
    print("  ・『vs A』が最も大きい確認時刻が最適。プラスなら寄り確認が有効")
    print("  ・『見送A損益』が大きくマイナス → その時刻の見送りが効いている(ダマシ除外)")
    print("  ・『建値差』(円/株, B-A) が大きい → 本物のブレイクを高値掴み(出遅れコスト)")
    print("  ※ yfinanceは約60日のみ。判断には --minute-dir で長期5分足を強く推奨")
    print("  ※ 最適時刻は過信禁物(過学習)。数ヶ月のフォワードで確認を推奨")


if __name__ == "__main__":
    main()
