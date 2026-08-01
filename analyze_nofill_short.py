r"""analyze_nofill_short.py — #6b の信頼版検証: 「lss逆指値が9:05までに未発動なら9:05成行ショート」。

背景: lss(ロング候補を逆指値売り)が寄り〜9:05に未約定=株が前日終値まで下げず持ちこたえた日。
その日に9:05成行でショートを入れると取れるか(=lssの空振り日への上乗せ)。当時 analyze_short_nofill_long
(再実装ツール)で PF1.73 と出たが、同系ツールは本番と2度食い違った(ガード2%/鏡像)ため信頼できない。
本ツールは analyze_gap_bt / compare_lss_stop_1m と同じ『本番同等の信号+クリーン1分足』で測り直す。

方式(本番同等):
  * 信号=エンジン run_limit_backtest(stop_sell) の trade_log + nofill_log(=約定+不約定の全lss信号)。
    各信号の order_limit(トリガー=前日終値-1tick)/order_stop(上)/order_target(下)/entry_dt(約定日)を使う。
  * その約定日の1分足で「09:00〜09:05未満に 安値<=トリガー があったか」= lssが9:05までに発動したか判定。
    発動あり → lssが処理=対象外。発動なし(=9:05時点で未約定) → #6b候補。
  * #6b候補は 09:05の始値で成行ショート。損切/利確は元信号の距離を9:05約定値に移設
    (stop05=entry05+(order_stop-order_limit) / target05=entry05-(order_limit-order_target))。
    決済は1分足first-touch+delay1(現行lssと同じ)。損益=short_pnl。
  * ★成行ショートは実スリッページ(規制/薄板 §18.3)が逆指値より大きい。--slip で入れて評価すること
    (既定0=現行lssと同基準だが楽観。0.2〜0.3%も必ず見る)。

BTは lss_trades.csv の bt列(=レポート一致)、BT>=--bt-min(既定40)。OOS(--base-month)分割。
前提: fetch_1m_all 済(時刻付き1分足) + .\daily で lss_trades.csv。

使い方:
  set LSS_TRADES_CSV=lss_trades.csv & python analyze_nofill_short.py --bt-min 40 --workers 8
  python analyze_nofill_short.py --bt-min 40 --slip 0.002   # 実スリッページ込み(本命の見方)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import time as dtime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="#6b 検証: lss未約定(9:05まで)→9:05成行ショート(本番同等)")
ap.add_argument("--trades-csv", type=str,
                default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"))
ap.add_argument("--bt-min", type=float, default=40.0)
ap.add_argument("--base-month", type=str, default="2026-01",
                help="OOS分割(以前=TRAIN、以後=TEST)。空=全期間のみ")
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--stop-delay-bars", type=int, default=1, help="損切り遅延(既定1=delay1)")
ap.add_argument("--slip", type=float, default=0.0,
                help="成行ショートの不利スリップ(既定0=現行lssと同基準だが楽観。0.002=0.2%%も見る)")
ap.add_argument("--days", type=int, default=500)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--qty", type=int, default=None)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--no-cache", action="store_true")
ap.add_argument("--refresh-cache", action="store_true")
args = ap.parse_args()

import backtest_limit_entry as ble
from jquants_fetch import _load_pkl, _cache_path, MINUTE_CACHE_DIR, yf_to_jquants
from daytrade_data import split_by_day
from sameday5m_core import mod_for

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._INTRADAY_5M = False

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE = ble.FEE_PCT_ONE_WAY
DELAY = max(0, int(args.stop_delay_bars))
_0905 = dtime(9, 5)


def _norm(sym): return str(sym).upper().removesuffix(".T").split(".")[0]


def _jq_to_yf(code):
    c = str(code).strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _load_bt_pairs():
    p = Path(args.trades_csv)
    if not p.exists():
        print(f"[error] BTソースCSVが無い: {p}", file=sys.stderr); return {}
    bt = {}; name = {}
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        sym = _norm(r.get("symbol") or r.get("code") or "")
        strat = str(r.get("strategy") or "").strip()
        if not sym or not strat:
            continue
        try:
            b = float(r.get("bt") or 0)
        except Exception:
            b = 0.0
        bt[(sym, strat)] = max(bt.get((sym, strat), -1e9), b)
        if r.get("name"):
            name[sym] = r.get("name")
    _load_bt_pairs.name = name
    return {k: v for k, v in bt.items() if v >= args.bt_min}


def _load_1m(sym_yf):
    try:
        return _load_pkl(_cache_path(MINUTE_CACHE_DIR, f"{yf_to_jquants(sym_yf)}_1m"))
    except Exception:
        return None


def _short_exit(highs, lows, closes, opens, stop_p, target_p):
    """9:05ショートの同日決済(1分足first-touch+delay1・現実的exit)。bar0=09:05約定バー。"""
    n = len(highs)
    stop_from = DELAY
    for j in range(n):
        if lows[j] <= target_p:               # 利確(下)優先(バー内)
            return target_p, "target"
        if j >= stop_from and highs[j] >= stop_p:
            if DELAY > 0 and j == stop_from and lows[j] > stop_p:
                return max(stop_p, float(opens[j])), "stop"   # 無保護窓で通過済→実価格
            return stop_p, "stop"
    return float(closes[-1]), "close"


def _pnl(entry_p, exit_p, qty):
    entry_eff = entry_p * (1.0 - args.slip)   # 成行ショート: 安く売る=不利
    fee = (entry_eff + exit_p) * qty * FEE
    return (entry_eff - exit_p) * qty - fee


def _collect(sym_yf, strat):
    """1(銘柄,戦略)の #6b トレード(9:05成行ショート)を集める。
    ★修正(2026-08-01): エンジンのfill結果でなく df_ind の entry_sig(全シグナル)から直接拾う。
      (旧: _INTRADAY_5M=False だと nofill_log が空=ギャップアップ等の本当の不約定が欠落していた)"""
    out = []
    params = getattr(mod_for(strat), "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return out
    cf = params[0]
    m1 = _load_1m(sym_yf)
    if m1 is None or m1.empty:
        return out
    by_day = split_by_day(m1)
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym_yf, args.days + 420)
        df_ind = cf(df_raw.copy())
    except Exception:
        return out
    if df_ind is None or df_ind.empty or "entry_sig" not in df_ind.columns:
        return out
    one_dates = sorted(by_day.keys())
    from datetime import timedelta as _td
    since = ble._TODAY - _td(days=args.days)
    sig = df_ind[df_ind["entry_sig"].fillna(False)]
    for S, row in sig.iterrows():
        try:
            Sdate = S.date() if hasattr(S, "date") else S
        except Exception:
            continue
        if Sdate < since:
            continue
        prev_close = float(row.get("close", 0) or 0)
        atr = float(row.get("atr", 0) or 0)
        if prev_close <= 0 or atr <= 0 or atr != atr:   # NaN除外
            continue
        trigger = prev_close                  # lss注文価格(em=0)。-1tickは<0.1%で無視
        # エントリー日 = シグナル日の翌1分足営業日
        edate = next((d for d in one_dates if d > Sdate), None)
        if edate is None:
            continue
        db = by_day.get(edate)
        if db is None or len(db) < 2:
            continue
        pre = db[db.index.time < _0905]
        if not pre.empty and float(pre["low"].min()) <= trigger:
            continue   # 9:05までに安値がトリガー到達=lssが約定 → #6b対象外
        post = db[db.index.time >= _0905]
        if post.empty or len(post) < 2:
            continue
        day_open = float(db["open"].iloc[0])
        entry05 = float(post["open"].iloc[0])
        if entry05 <= 0:
            continue
        gap = (day_open - prev_close) / prev_close if prev_close > 0 else 0.0
        stop05 = entry05 + atr * args.sm       # ショート: 損切=上
        target05 = entry05 - atr * args.tm     # 利確=下
        xp, _rsn = _short_exit(post["high"].to_numpy(float), post["low"].to_numpy(float),
                               post["close"].to_numpy(float), post["open"].to_numpy(float),
                               stop05, target05)
        out.append({"fd": pd.Timestamp(edate), "pnl": _pnl(entry05, xp, QTY), "gap": gap})
    return out


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "pnl": sum(pnls), "pf": pf, "wr": len(wins) / n * 100}


def _fmt(s):
    if not s:
        return "  該当なし"
    _pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"{s['n']:>6}件  損益{s['pnl']:>+13,.0f}  勝率{s['wr']:>4.0f}%  PF{_pf:>5}  1件平均{s['pnl']/s['n']:>+7,.0f}"


def main():
    keep = _load_bt_pairs()
    if not keep:
        print("[error] BT対象ペアが0。", file=sys.stderr); return
    names = getattr(_load_bt_pairs, "name", {})
    pairs = sorted(keep.keys())
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] BT{args.bt_min:.0f}以上 {len(pairs)}ペア / sm={args.sm} tm={args.tm} delay={DELAY} "
          f"slip{args.slip*100:.2f}% fee{FEE*100:.2f}% / #6b=9:05成行ショート(本番同等)")

    import hashlib as _h, pickle as _pk
    _cd = Path(".nofillshort_cache")
    _key = _h.md5("|".join(str(x) for x in [
        "nfsv1", getattr(ble, "_BT_LOGIC_VER", "?"), args.sm, args.tm, DELAY, args.slip,
        args.days, args.bt_min, args.limit, QTY,
        _h.md5(",".join(f"{s}:{t}" for s, t in pairs).encode()).hexdigest(),
    ]).encode()).hexdigest()[:16]
    _cf = _cd / f"trades_{_key}.pkl"

    trades = None
    if _cf.exists() and not args.no_cache and not args.refresh_cache:
        try:
            trades = _pk.loads(_cf.read_bytes())
            print(f"[cache] 再利用: {_cf} ({len(trades)}件) ※--refresh-cacheで再計算")
        except Exception:
            trades = None
    if trades is None:
        trades = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_collect, _jq_to_yf(s), t): (s, t) for (s, t) in pairs}
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 100 == 0:
                    print(f"  ...{done}/{len(pairs)}ペア", flush=True)
                try:
                    trades += fut.result()
                except Exception:
                    continue
        if not args.no_cache:
            try:
                _cd.mkdir(exist_ok=True)
                _cf.write_bytes(_pk.dumps(trades))
                print(f"[cache] 保存: {_cf} ({len(trades)}件)")
            except Exception as _e:
                print(f"[cache] 保存失敗({_e})")

    if not trades:
        print("[error] #6bトレード0件。1分足キャッシュ/CSVを確認。", file=sys.stderr); return

    def _report(ts, title):
        print("\n" + "=" * 78)
        print(f"【{title}】 #6b(9:05成行ショート) {len(ts)}件")
        print("-" * 78)
        print("  " + _fmt(_stat([t["pnl"] for t in ts])))

    def _gbucket(g):
        p = g * 100
        if p < 0:   return "<0%(下げたが未約定)"
        if p < 1:   return "0〜+1%"
        if p < 2:   return "+1〜+2%"
        if p < 3:   return "+2〜+3%"
        if p < 5:   return "+3〜+5%"
        return "+5%超(大ギャップUP)"
    _GORDER = ["<0%(下げたが未約定)", "0〜+1%", "+1〜+2%", "+2〜+3%", "+3〜+5%", "+5%超(大ギャップUP)"]

    def _gap_report(ts, title):
        from collections import defaultdict
        agg = defaultdict(list)
        for t in ts:
            agg[_gbucket(t.get("gap", 0.0))].append(t["pnl"])
        print("\n" + "=" * 78)
        print(f"【{title}】ギャップ帯別(始値の前日終値比) #6b内訳  計{len(ts)}件")
        print("-" * 78)
        for b in _GORDER:
            pn = agg.get(b)
            if not pn:
                continue
            print(f"  {b:<20} {_fmt(_stat(pn))}")

    _report(trades, f"BT{args.bt_min:.0f}以上・全期間(in-sample含む)")
    _gap_report(trades, f"BT{args.bt_min:.0f}以上・全期間")
    bm = args.base_month.strip()
    if bm:
        try:
            be = pd.Period(bm, "M").end_time.normalize()
            te = [t for t in trades if t["fd"] > be]
            _report([t for t in trades if t["fd"] <= be], f"TRAIN ≤{be.date()} (in-sample)")
            _report(te, f"TEST >{be.date()} (OOS)")
            _gap_report(te, f"BT{args.bt_min:.0f}以上・TEST(OOS)")
        except Exception as _e:
            print(f"[warn] base-month分割スキップ({_e})")

    print("\n" + "=" * 78)
    print("判断: TEST(OOS)が明確にプラス(PF>1.2目安)なら #6b は上乗せ価値あり。")
    print(f"  ★ただし成行ショートは実スリッページが大きい。--slip 0.002〜0.003 でも残るか必ず確認")
    print(f"    (現在 slip={args.slip*100:.2f}%)。OOSがマイナス/slip込みで消えるなら却下。")


if __name__ == "__main__":
    main()
