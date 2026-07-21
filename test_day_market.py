"""test_day_market.py — 「lssの日次損益は"寄り前に分かる"市場指標に連動しているか」を測る。

背景(ユーザー): 日によって lss(ロング銘柄ショート)の合計損益が大きくぶれる。
  その差が『9:00の寄り前に確定している指標(米国株・先物・ドル円)』で説明できるなら、
  上げ気配の日はショートを見送り/ロングのデイトレに回す、という対策を打ちたい。
  重要: 発注は9:00の寄り前に間に合わせたい。よって指標は"寄り前に確定"していること。

指標(すべて9:00より前に判明 → 発注判断に使える):
  cme_nikkei  NIY=F  CME日経先物(円建て,朝6時) = 現物ギャップの直接予告「シカゴ日経比」
  sp500_fut   ES=F   S&P500先物(24h, 寄り直前まで連続) = 一番"寄り前"に近いリスクオン/オフ
  usdjpy      JPY=X  ドル円(24h) 円安→株高
  sp500       ^GSPC  S&P500現物 前夜終値(朝5時)
  nasdaq      ^IXIC  Nasdaq現物 前夜終値(朝6時)
  vix         ^VIX   VIX 前夜終値
  (参考) cash_gap: 現物日経の当日寄りギャップ%。9:00にしか確定しない=発注には間に合わない。
                   連動の"答え合わせ"用に併記するだけ。

先読み防止(重要):
  各 JST営業日 D について「D の寄りより厳密に前に確定した最新バー」を使う。
  pandas merge_asof(backward, allow_exact_matches=False) で D 未満の最新値を紐付け。
  cash_gap だけは同日9:00の値(=事後参考)。

やること:
  1) 各 (symbol,strategy) を run_limit_backtest → D+1エントリー(現行) → 5分足first-touchで損益。
  2) 損益を『エントリー日』で合計 → 日次 lss 損益系列。
  3) 各指標の日次変化%を"寄り前asof"で紐付け。
  4) 相関(Pearson) と バケット別(寄り前指標)の日次成績を出す。
  5) base-month で TRAIN/TEST(OOS)分割。TEST が本命。

使い方:
  python test_day_market.py --proposal lss_proposal_2026-06.py --base-month 2026-06 \
      --source local --workers 8
  (12月基準でも: --proposal lss_proposal_2025-12.py --base-month 2025-12)
  指標を絞る: --proxies cme_nikkei,sp500_fut,usdjpy
"""
from __future__ import annotations
import argparse
import runpy
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# 寄り前に確定する指標(ticker) — 名前: (yfticker, 表示ラベル)
PROXY_DEFS = {
    "cme_nikkei": ("NIY=F", "CME日経先物%(前夜,寄り前)"),
    "sp500_fut":  ("ES=F",  "S&P500先物%(寄り直前)"),
    "usdjpy":     ("JPY=X", "ドル円%(前夜)"),
    "sp500":      ("^GSPC", "S&P500現物%(前夜終値)"),
    "nasdaq":     ("^IXIC", "Nasdaq現物%(前夜終値)"),
    "vix":        ("^VIX",  "VIX%(前夜)"),
}

ap = argparse.ArgumentParser(description="lss日次損益と寄り前市場指標の連動を測る")
ap.add_argument("--proposal", required=True)
ap.add_argument("--base-month", type=str, default="2026-06")
ap.add_argument("--proxies", type=str,
                default="cme_nikkei,sp500_fut,usdjpy,sp500,nasdaq,vix",
                help="使う寄り前指標(カンマ区切り): " + ",".join(PROXY_DEFS))
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
USE_PROXIES = [p.strip() for p in args.proxies.split(",")
               if p.strip() in PROXY_DEFS]


def _dl_pct(ticker: str) -> pd.Series:
    """ticker の日次終値変化%(前バー比)を date昇順の Series(index=Timestamp)で返す。"""
    import yfinance as yf
    df = yf.download(ticker, period="3y", interval="1d",
                     auto_adjust=False, progress=False)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    cl = df["Close"].astype(float)
    pct = cl.pct_change() * 100.0
    pct = pct.dropna()
    pct.index = pd.to_datetime(pct.index).normalize()
    return pct


def _nikkei_cash_gap() -> dict:
    """^N225 現物の当日寄りギャップ% {date: gap%}(=9:00確定, 事後参考)。"""
    try:
        import yfinance as yf
        df = yf.download("^N225", period="3y", interval="1d",
                         auto_adjust=False, progress=False)
        if df is None or df.empty:
            return {}
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        op = df["Open"].astype(float)
        prev = df["Close"].astype(float).shift(1)
        gap = (op - prev) / prev * 100.0
        return {ts.date(): float(g) for ts, g in gap.items() if pd.notna(g)}
    except Exception as e:
        print(f"[warn] ^N225 現物取得失敗: {e}")
        return {}


def _asof_before(pct: pd.Series, jst_dates: list) -> dict:
    """各 JST営業日 D に『D より厳密に前の最新 pct』を紐付け(先読み防止)。
    D の寄り前に確定していた最新の指標変化を表す。"""
    if pct.empty:
        return {}
    left = pd.DataFrame({"d": pd.to_datetime(jst_dates)}).sort_values("d")
    right = pd.DataFrame({"d": pct.index, "v": pct.values}).sort_values("d")
    merged = pd.merge_asof(left, right, on="d", direction="backward",
                           allow_exact_matches=False)
    out = {}
    for _, row in merged.iterrows():
        if pd.notna(row["v"]):
            out[row["d"].date()] = float(row["v"])
    return out


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


def _fmt_corr(x):
    return "n/a" if x != x else f"{x:+.3f}"


def _bucket_report(day_pnl: dict, day_val: dict, label, edges, edge_labels):
    days = [d for d in sorted(day_pnl) if d in day_val]
    print(f"\n■ {label} バケット別 lss日次成績 (TEST/OOS, {len(days)}営業日)")
    print(f"{'区分':>12} | {'日数':>5} {'勝ち日':>6} {'合計損益':>12} {'平均/日':>10} {'中央/日':>10}")
    print("-" * 68)
    for lo, hi, lab in zip([None] + edges, edges + [None], edge_labels):
        sel = [day_pnl[d] for d in days
               if (lo is None or day_val[d] >= lo) and (hi is None or day_val[d] < hi)]
        if not sel:
            print(f"{lab:>12} | {0:>5} {'-':>6} {'-':>12} {'-':>10} {'-':>10}")
            continue
        s = pd.Series(sel)
        print(f"{lab:>12} | {len(sel):>5} {(s>0).mean()*100:>5.0f}% "
              f"{s.sum():>+12,.0f} {s.mean():>+10,.0f} {s.median():>+10,.0f}")


def main():
    print(f"[info] 寄り前指標をDL中: {', '.join(PROXY_DEFS[p][0] for p in USE_PROXIES)} ...")
    proxy_pct = {}
    for p in USE_PROXIES:
        tk = PROXY_DEFS[p][0]
        try:
            s = _dl_pct(tk)
        except Exception as e:
            print(f"  [warn] {tk} 取得失敗: {e}")
            s = pd.Series(dtype=float)
        proxy_pct[p] = s
        print(f"  {tk:>7}: {len(s)}バー")
    cash_gap = _nikkei_cash_gap()

    pairs = _load_pairs(args.proposal)
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] proposal={args.proposal} {len(pairs)}ペア / base={args.base_month} "
          f"(翌月以降=OOS)")

    rows_all = []   # (date, pnl, is_test)
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_one, s, n, st): 1 for (s, n, st) in pairs}
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(f"  ...{done}/{len(pairs)}", flush=True)
            try:
                rr = fut.result()
            except Exception:
                continue
            rows_all.extend(rr)

    test = [(d, p) for (d, p, t) in rows_all if t]
    print(f"\n収集: 全{len(rows_all)}トレード / TEST(OOS)トレード={len(test)}件")
    if not test:
        print("[error] OOSトレードが0件。base-monthかproposalを確認。")
        return

    # 日次集約
    day_pnl = {}
    for (d, p) in test:
        day_pnl[d] = day_pnl.get(d, 0.0) + p
    days = sorted(day_pnl)
    dfp = pd.Series([day_pnl[d] for d in days], index=pd.to_datetime(days))

    # 各指標を"寄り前asof"で日付紐付け
    day_val = {}   # proxy -> {date: pct}
    for p in USE_PROXIES:
        day_val[p] = _asof_before(proxy_pct[p], days)
    day_val["cash_gap"] = {d: cash_gap[d] for d in days if d in cash_gap}

    print("\n" + "=" * 74)
    print(f"■ 日次lss損益 vs 寄り前指標 相関 (Pearson, TEST/OOS {len(days)}営業日)")
    print("=" * 74)
    print("  ※ 負の相関 = その指標が上がる(株高気配)ほど lss(ショート)は負ける。")
    for p in USE_PROXIES:
        dv = day_val[p]
        common = [d for d in days if d in dv]
        if len(common) < 5:
            print(f"  {PROXY_DEFS[p][1]:<26}: データ不足({len(common)})")
            continue
        a = pd.Series([day_pnl[d] for d in common])
        b = pd.Series([dv[d] for d in common])
        print(f"  {PROXY_DEFS[p][1]:<26}: {_fmt_corr(a.corr(b))}  (n={len(common)})")
    # cash_gap 参考
    dv = day_val["cash_gap"]
    common = [d for d in days if d in dv]
    if len(common) >= 5:
        a = pd.Series([day_pnl[d] for d in common])
        b = pd.Series([dv[d] for d in common])
        print(f"  {'(参考)現物ギャップ%(9:00確定=事後)':<26}: {_fmt_corr(a.corr(b))}  (n={len(common)})")
    print(f"\n  参考: 日次lss損益  合計{dfp.sum():+,.0f} / 平均{dfp.mean():+,.0f} / "
          f"勝ち日{(dfp>0).mean()*100:.0f}%")

    # 寄り前指標のバケット(発注判断に使える指標を優先)
    _prefer = ["cme_nikkei", "sp500_fut", "usdjpy"]
    for p in _prefer:
        if p in USE_PROXIES and len(day_val[p]) >= 10:
            _bucket_report(day_pnl, day_val[p], PROXY_DEFS[p][1],
                           [-1.0, -0.3, 0.0, 0.3, 1.0],
                           ["< -1%", "-1〜-0.3", "-0.3〜0", "0〜0.3", "0.3〜1", ">= 1%"])

    print("\n判定の読み方:")
    print("  - 寄り前指標(CME日経/S&P先物/ドル円)との相関が明確に負 &")
    print("    上げ帯(0.3%〜, 1%〜)の合計損益がマイナス")
    print("    → その日は株高気配 = ショート不利。ロングに回す/見送りが有効。")
    print("  - 相関が弱い(±0.1未満)なら、寄り前指標で日を選ぶ効果は薄い。")
    print("  - 寄り前指標の相関が(参考)現物ギャップと同符号なら、先物で代替できる裏付け。")


if __name__ == "__main__":
    main()
