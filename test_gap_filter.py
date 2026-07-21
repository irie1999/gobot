"""test_gap_filter.py — 「日経の寄りギャップが強い上げの日はlssショートを見送る」フィルター検証。

案(ユーザー): 寄り前に先物で今日の方向を判定 → 強い上げ気配の日は上げ継続しやすく、lss
ショートは『日中ディップで約定→上げ再開で損切り』が起きやすい。だからそういう日は見送る。

代理シグナル: その日の日経(^N225)の寄りギャップ% = (当日始値 - 前日終値) / 前日終値 * 100。
  これは9:00の寄りで判明し、先物ナイトが予告する値にほぼ一致(=発注判断のタイミングと同じ)。

やること:
  1) 各 (symbol,strategy) を run_limit_backtest → D+1エントリー(現行) → 5分足first-touchで損益。
  2) 各トレードのエントリー日の『日経寄りギャップ%』を紐付け。
  3) 閾値Tを振り、「gap >= T の日(=強い上げ)のショートを見送る」と成績がどう変わるか比較。
     - 採用(gap<T) の成績 と 見送り(gap>=T) の成績を並べる。
     - 見送り分が"負け組"なら、フィルターで採用分の総損益が上がる=フィルター有効。
  4) base-month で TRAIN/TEST(OOS)分割。TESTが本命。

使い方:
  python test_gap_filter.py --proposal lss_proposal_2026-03.py --base-month 2026-03 \
      --thresholds 0.3,0.5,1.0,1.5,2.0 --source local --workers 8
  (12月基準でも: --proposal lss_proposal_2025-12.py --base-month 2025-12)
"""
from __future__ import annotations
import argparse
import runpy
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

ap = argparse.ArgumentParser(description="日経寄りギャップで上げ日のlssショートを見送るフィルター検証")
ap.add_argument("--proposal", required=True)
ap.add_argument("--base-month", type=str, default="2026-03")
ap.add_argument("--thresholds", type=str, default="0.3,0.5,1.0,1.5,2.0",
                help="上ギャップ閾値%(カンマ区切り)。gap>=閾値の日は見送り。")
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--days", type=int, default=800)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--slip", type=float, default=0.0)
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_exit_5m, short_pnl, short_entry_fill_5m

ble._INTRADAY_5M = False
ble._ENTRY_TYPE_FORCE = None
QTY = ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY
BASE_END = pd.Period(args.base_month, "M").end_time.normalize()
THRS = [float(x) for x in args.thresholds.split(",") if x.strip() != ""]


def _nikkei_gap_by_date() -> dict:
    """^N225 の日次寄りギャップ% を {date: gap%} で返す(yfinance)。"""
    try:
        import yfinance as yf
        df = yf.download("^N225", period="3y", interval="1d",
                         auto_adjust=False, progress=False)
        if df is None or df.empty:
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        op = df["Open"].astype(float)
        cl = df["Close"].astype(float)
        prev = cl.shift(1)
        gap = (op - prev) / prev * 100.0
        out = {}
        for ts, g in gap.items():
            if pd.notna(g):
                out[ts.date()] = float(g)
        return out
    except Exception as e:
        print(f"[warn] ^N225 取得失敗: {e}")
        return {}


def _close_col(df):
    return "close" if "close" in df.columns else ("Close" if "Close" in df.columns else None)


def _load_pairs(path):
    ns = runpy.run_path(path)
    sel = ns.get("SELECTED") or []
    out, seen = [], set()
    for row in sel:
        if len(row) >= 3:
            k = (str(row[0]), str(row[2]))
            if k not in seen:
                seen.add(k)
                out.append((str(row[0]), str(row[1]), str(row[2])))
    return out


def _scan_one(sym, name, strat):
    """1銘柄1戦略の [(entry_date, pnl, is_test)] を返す(現行D+1エントリー)。"""
    out = []
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return out
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return out
    if df_raw is None or df_raw.empty:
        return out
    mod = mod_for(strat)
    params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return out
    try:
        df = params[0](df_raw.copy())
    except Exception:
        return out
    try:
        r = ble.run_limit_backtest(sym, name, df, lambda d: d, 0.0,
                                   args.sm, args.tm, args.days + 420, strat,
                                   entry_type="stop_sell", max_hold=0)
    except Exception:
        return out
    if not r:
        return out
    for t in r.get("trade_log", []):
        if t.get("reason") in ("発注中", "保有中"):
            continue
        edt = t.get("entry_dt")
        lp = float(t.get("order_limit", 0) or 0)
        osp = float(t.get("order_stop", 0) or 0)
        otp = float(t.get("order_target", 0) or 0)
        if lp <= 0 or osp <= 0 or otp <= 0 or edt is None:
            continue
        if lp < args.min_price or lp > args.max_price:
            continue
        fd = edt.date() if hasattr(edt, "date") else edt
        db = by_day.get(fd)
        if db is None or len(db) < 2:
            continue
        stop_p, target_p = max(osp, otp), min(osp, otp)
        ef = short_entry_fill_5m(db, lp, False, entry_gap_limit=0.03)
        if ef is None:
            continue
        xp, reason, _e, _x = short_exit_5m(db, lp, stop_p, target_p, False)
        if reason in ("no_5m", "no_entry"):
            continue
        pnl = short_pnl(ef, xp, reason, QTY, FEE_ONE_WAY, args.slip)
        out.append((fd, pnl, pd.Timestamp(fd) > BASE_END))
    return out


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return dict(n=0, wr=0.0, pf=0.0, pnl=0, avg=0.0)
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return dict(n=n, wr=len(wins) / n * 100, pf=pf, pnl=sum(pnls), avg=sum(pnls) / n)


def _pf(x):
    return "inf" if x == float("inf") else f"{x:.2f}"


def main():
    nikkei = _nikkei_gap_by_date()
    if not nikkei:
        print("[error] 日経ギャップが取れませんでした(yfinance ^N225)。中断。")
        return
    pairs = _load_pairs(args.proposal)
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] proposal={args.proposal} {len(pairs)}ペア / base={args.base_month} "
          f"(翌月以降=OOS) / 日経ギャップ日数={len(nikkei)} / 閾値={THRS}")
    trades = []   # (gap, pnl, is_test)
    miss = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_one, s, n, st): 1 for (s, n, st) in pairs}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  ...{done}/{len(pairs)}", flush=True)
            try:
                rows = fut.result()
            except Exception:
                continue
            for fd, pnl, is_test in rows:
                g = nikkei.get(fd)
                if g is None:
                    miss += 1
                    continue
                trades.append((g, pnl, is_test))

    test = [(g, p) for (g, p, t) in trades if t]
    print(f"\n収集: 全{len(trades)}トレード(日経ギャップ紐付け不可 {miss}件除外) / TEST(OOS)={len(test)}件")
    base = _stat([p for _, p in test])
    print("\n" + "=" * 84)
    print("■ 上ギャップ日フィルター — TEST(OOS) が本命。gap>=閾% の日を『見送り』")
    print("=" * 84)
    print(f"{'閾値':>7} | {'採用(gap<閾)':>26} | {'見送り(gap>=閾)':>26}")
    print(f"{'':>7} | {'件数':>6}{'勝率':>6}{'PF':>6}{'損益':>10} | {'件数':>6}{'勝率':>6}{'PF':>6}{'損益':>10}")
    print(f"{'無し':>7} | {base['n']:>6}{base['wr']:>5.0f}%{_pf(base['pf']):>6}{base['pnl']:>+10,.0f} | "
          f"{'-':>6}{'-':>6}{'-':>6}{'-':>10}   ← フィルタ無し(全採用)")
    for T in THRS:
        keep = [p for g, p in test if g < T]
        skip = [p for g, p in test if g >= T]
        sk = _stat(keep); ss = _stat(skip)
        mark = ""
        if sk["pnl"] > base["pnl"]:
            mark = "  ★採用分>無しフィルタ(見送りが負け組=有効)"
        print(f"{T:>6.2f}% | {sk['n']:>6}{sk['wr']:>5.0f}%{_pf(sk['pf']):>6}{sk['pnl']:>+10,.0f} | "
              f"{ss['n']:>6}{ss['wr']:>5.0f}%{_pf(ss['pf']):>6}{ss['pnl']:>+10,.0f}{mark}")
    print("\n判定: 『採用(gap<閾)』の総損益が『フィルタ無し』を上回る閾値があれば、")
    print("      = 見送った上ギャップ日のショートが負け組 = フィルター有効。")
    print("      『見送り(gap>=閾)』の損益が明確にマイナスなら、その日は避けるべき。")


if __name__ == "__main__":
    main()
