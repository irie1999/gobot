"""
check_signals_index.py — 横ばい相場用 指数ETF 押し目反発買い (STOロングbounce)。
================================================================================
meanrev_lab.py の徹底検証(個別株全滅→指数ETF→厳密レンジ→非相関→OOS前後半)を
唯一通過した頑健構成を正式化したライブ用モジュール。

  戦略: STO(ストキャス%K<20)で売られすぎ → 翌日 前日高値を上抜けたら逆指値買い
        (bounce=反転確認) → +3ATRで利確 or 8日でタイムカット、損切りは終値-2ATR。
  対象: 日経225ETF(1321)・TOPIX ETF(1306)・セクター/REIT ETF (非相関ユニバース)
  発動: 大局レジーム=横ばい の時だけ (上げは現行の順張り戦略が担当)。

【検証エビデンス (meanrev_lab, 信用コスト0.03%/厳密レンジ/非相関11本)】
  横ばい PF 1.41 (+153万, 226件) / OOS 前半1.52(132件) / 後半1.30(94件) = ✓頑健
  ※ dip(ナイフ掴み)とショートはOOSで全滅。ロングのbounceだけが生き残った。

【使い方】
  python check_signals_index.py               # 今日の指数シグナル + 大局レジーム表示
  python check_signals_index.py --date 2020-04-30
  python check_signals_index.py --backtest    # 非相関ユニバースで直近成績(ラボ再現)
  python check_signals_index.py --force-signal # レジームゲートを無視して条件だけ表示(検証用)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from backtest_limit_entry import (
    fetch, round_to_tick, calc_qty,
    SLIPPAGE_STOP_PCT, FEE_PCT_ONE_WAY, LIMIT_ENTRY_MARGIN_PCT,
)
import nikkei_analysis as na

JST = timezone(timedelta(hours=9))

# ── 監視ユニバース (非相関: 日経1・TOPIX1・独立セクター/REIT・レバ1) ──
WATCHLIST: list[tuple[str, str]] = [
    ("1321.T", "日経225ETF"),
    ("1306.T", "TOPIX ETF"),
    ("1615.T", "TOPIX-17 銀行"),
    ("1617.T", "TOPIX-17 食品"),
    ("1621.T", "TOPIX-17 素材化学"),
    ("1625.T", "TOPIX-17 機械"),
    ("1627.T", "TOPIX-17 電機精密"),
    ("1631.T", "TOPIX-17 商社卸売"),
    ("1633.T", "TOPIX-17 不動産"),
    ("1343.T", "NEXT FUNDS REIT"),
    ("1570.T", "日経レバ(2倍)"),
]

# ── 戦略パラメータ (検証で頑健だった STOロングbounce) ──
#   (sm, tm, max_hold)   em はbounce(trig=前日高値)では未使用
#   wide  : +3ATR利確・8日保有  (OOS前後半とも最良: 前半1.52/後半1.30)
#   atr1R : +2ATR利確・6日保有  (こちらも✓頑健: 前半1.05/後半1.30)
PRESETS = {
    "wide":  dict(sm=2.0, tm=3.0, max_hold=8),
    "atr1R": dict(sm=2.0, tm=2.0, max_hold=6),
}
STO_THRESH   = 20.0    # %K がこの値未満で売られすぎ (低ボラETF向け緩和閾値)
STOCK_ER_MAX = 0.25    # 銘柄自身の60日効率比がこの値未満(=レンジ)の日だけ発注
ENTRY_EXPIRE = 3       # 逆指値の有効日数


# ── 指標 ─────────────────────────────────────────────────────────
def calc_sto(df: pd.DataFrame) -> pd.DataFrame:
    """ストキャス slow %K(14,3) + ATR(14) + 60日ER。entry_sig = %K<20。"""
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]
    ll = l.rolling(14).min()
    hh = h.rolling(14).max()
    k = (c - ll) / (hh - ll).replace(0, np.nan) * 100
    df["stok"] = k.rolling(3).mean()

    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=14, adjust=False).mean()

    er = (c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, np.nan)
    df["stock_er"] = er

    df["entry_sig"] = df["stok"] < STO_THRESH
    return df


# ── 大局レジーム (厳密: ER<0.15 かつ MA200傾き±0.5%以内 = 本当に横ばい) ──
def current_macro(strict: bool = True, target_date=None) -> dict | None:
    """日経の大局レジームを返す。strict=検証と同じ厳格定義。"""
    c = na.fetch_n225(4, end_date=target_date)
    if c is None or len(c) < 230:
        return None
    r = na.get_regime(c)
    er = r.get("er", 0.0)
    slope = r.get("slope200", 0.0)
    above = r.get("above_ma200", True)
    if strict:
        if er < 0.15 and abs(slope) < 0.5:
            macro = "sideways"
        elif above and slope > 0:
            macro = "up"
        elif (not above) and slope < 0:
            macro = "down"
        else:
            macro = "?"
    else:
        macro = r.get("macro", "?")
    return {"macro": macro, "er": er, "slope200": slope,
            "cur": r.get("cur"), "above_ma200": above}


def regime_ok(target_date=None) -> tuple[bool, dict | None]:
    """発動可否: 大局レジーム=横ばい のときだけ True。"""
    m = current_macro(strict=True, target_date=target_date)
    if m is None:
        return False, None
    return (m["macro"] == "sideways"), m


# ── ライブシグナル判定 ───────────────────────────────────────────
def check_signal_on_date(symbol: str, preset: str = "wide",
                         target_date=None) -> dict | None:
    """target_date(既定=最新足)で STOロングbounce のシグナルが出ているか。
    出ていれば逆指値買い(トリガー=当日高値)の注文パラメータを返す。"""
    p = PRESETS[preset]
    df = fetch(symbol, 365)
    if df is None or len(df) < 210:
        return None
    df = calc_sto(df)

    if target_date is None:
        row = df.iloc[-1]
    else:
        ts = pd.Timestamp(target_date)
        cands = df.index[df.index <= ts]
        if len(cands) < 1:
            return None
        row = df.loc[cands[-1]]

    sig = bool(row.get("entry_sig", False))
    atr = float(row.get("atr", 0) or 0)
    er = float(row.get("stock_er", 1.0) or 1.0)
    if not sig or atr <= 0 or er >= STOCK_ER_MAX:
        return None

    trig = float(row["high"])                       # bounce: 当日高値を上抜けで買い
    sp   = trig - atr * p["sm"]                      # 損切り(終値判定)
    tp   = trig + atr * p["tm"]                      # 利確(+3ATR)
    if trig <= 0 or sp <= 0 or tp <= trig:
        return None
    limit_entry = trig * (1.0 + LIMIT_ENTRY_MARGIN_PCT)
    qty = calc_qty(trig, sp)
    sig_dt = row.name
    return dict(
        symbol=symbol,
        order_price=round_to_tick(trig),
        limit_entry_price=round_to_tick(limit_entry),
        stop_price=round_to_tick(sp),
        target_price=round_to_tick(tp),
        signal_date=sig_dt.strftime("%Y-%m-%d") if hasattr(sig_dt, "strftime") else str(sig_dt),
        stok=round(float(row["stok"]), 1),
        stock_er=round(er, 2),
        atr=round(atr, 1),
        qty=qty,
        position_value=round(trig * qty),
        max_hold=p["max_hold"],
    )


# ── バックテスト (ラボエンジン再現) ──────────────────────────────
def backtest(preset: str = "wide", since: str = "2014-01-01",
             fee: float = 0.0003, slip: float = 0.003) -> dict:
    """WATCHLIST 全体を meanrev_lab のエンジンで再計算し、横ばい/全件を集計。"""
    import meanrev_lab as ml
    from datetime import date
    p = PRESETS[preset]
    since_d = datetime.strptime(since, "%Y-%m-%d").date()
    bt_days = (date.today() - since_d).days + 400
    reg = ml._regime_series(15, strict=True)
    side, allt = [], []
    for sym, _name in WATCHLIST:
        df = fetch(sym, bt_days, min_start_date=since_d)
        if df is None or len(df) < 260:
            continue
        d2 = calc_sto(df)
        sig_long = d2["entry_sig"].fillna(False)
        sig_short = pd.Series(False, index=df.index)
        s_er = d2["stock_er"]
        stock_ok = (s_er < STOCK_ER_MAX).to_numpy()
        trs = ml._run_mr(df, sig_long, sig_short, "long", "bounce",
                         "atr" if preset == "atr1R" else "wide",
                         0.0, p["sm"], p["tm"], p["max_hold"],
                         fee, slip, 0.0, stock_ok)
        for t in trs:
            allt.append(t)
            if reg is not None and reg.asof(pd.Timestamp(t["sig_dt"])) == "sideways":
                side.append(t)
    return {"sideways": ml._stats(side), "all": ml._stats(allt)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="判定日 YYYY-MM-DD (既定=最新)")
    ap.add_argument("--preset", default="wide", choices=["wide", "atr1R"])
    ap.add_argument("--backtest", action="store_true", help="非相関ユニバースで直近成績を再計算")
    ap.add_argument("--force-signal", action="store_true",
                    help="レジームゲートを無視して条件成立銘柄を表示(検証用)")
    args = ap.parse_args()

    td = None if args.date is None else datetime.strptime(args.date, "%Y-%m-%d").date()

    ok, m = regime_ok(td)
    print("=" * 60)
    if m is None:
        print("  [警告] 日経データ取得失敗 — レジーム判定不可")
    else:
        _lbl = {"sideways": "🟡 横ばい", "up": "🟢 上げ", "down": "🔴 下げ", "?": "― 移行/曖昧"}
        print(f"  大局レジーム: {_lbl.get(m['macro'], m['macro'])}"
              f"   ER {m['er']:.2f} / MA200傾き {m['slope200']:+.1f}%")
        print(f"  → 横ばい戦略(指数ETF押し目反発買い) 発動: {'●ON' if ok else '○OFF (横ばい待ち)'}")
    print("=" * 60)

    if args.backtest:
        r = backtest(args.preset)
        def _pf(v): return "∞" if v == float("inf") else f"{v:.2f}"
        print(f"\n[バックテスト preset={args.preset}] 非相関ユニバース {len(WATCHLIST)}本")
        for lbl, s in (("横ばい", r["sideways"]), ("全件", r["all"])):
            print(f"  {lbl}: {s['n']}件 勝率{s['wr']:.0f}% PF{_pf(s['pf'])} 損益{s['pnl']:+,.0f}円 平均保有{s['hold']:.1f}日")
        return

    if not ok and not args.force_signal:
        print("\n横ばいレジームではないため、指数ETF戦略は待機中です。")
        print("(条件だけ確認するには --force-signal)")
        return

    print(f"\n{'銘柄':<10}{'名前':<16}{'%K':>6}{'ER':>6}{'逆指値':>9}{'損切':>9}{'目標':>9}{'株数':>7}")
    print("-" * 74)
    hit = 0
    for sym, name in WATCHLIST:
        s = check_signal_on_date(sym, args.preset, td)
        if s is None:
            continue
        hit += 1
        print(f"{sym:<10}{name[:15]:<16}{s['stok']:>6.1f}{s['stock_er']:>6.2f}"
              f"{s['order_price']:>9,}{s['stop_price']:>9,}{s['target_price']:>9,}{s['qty']:>7}")
    if hit == 0:
        print("  該当なし (今日は売られすぎ反発待ちのETFなし)")
    else:
        print(f"\n{hit}銘柄がシグナル点灯。逆指値買い(トリガー=当日高値)で発注可能。")
        print("※ 損切りは終値ベース(引け成行ガード)。目標+3ATR or 8日でタイムカット。")


if __name__ == "__main__":
    main()
