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


def trades_in_window(preset="wide", start="2021-01-01", end="2023-12-31",
                     symbol=None, sideways_only=True,
                     since="2014-01-01", fee=0.0003, slip=0.003) -> list[dict]:
    """指定期間に約定した実トレードを1件ずつ返す(既定=横ばいレジームのみ)。"""
    import meanrev_lab as ml
    from datetime import date
    p = PRESETS[preset]
    since_d = datetime.strptime(since, "%Y-%m-%d").date()
    s_ts, e_ts = pd.Timestamp(start), pd.Timestamp(end)
    bt_days = (date.today() - since_d).days + 400
    reg = ml._regime_series(15, strict=True)
    wl = [(s, n) for s, n in WATCHLIST if symbol is None or s == symbol]
    out = []
    for sym, name in wl:
        df = fetch(sym, bt_days, min_start_date=since_d)
        if df is None or len(df) < 260:
            continue
        d2 = calc_sto(df)
        sig_long = d2["entry_sig"].fillna(False)
        sig_short = pd.Series(False, index=df.index)
        stock_ok = (d2["stock_er"] < STOCK_ER_MAX).to_numpy()
        trs = ml._run_mr(df, sig_long, sig_short, "long", "bounce",
                         "atr" if preset == "atr1R" else "wide",
                         0.0, p["sm"], p["tm"], p["max_hold"],
                         fee, slip, 0.0, stock_ok)
        for t in trs:
            edt = pd.Timestamp(t["entry_dt"])
            if not (s_ts <= edt <= e_ts):
                continue
            if sideways_only and reg is not None and reg.asof(pd.Timestamp(t["sig_dt"])) != "sideways":
                continue
            out.append({**t, "symbol": sym, "name": name})
    out.sort(key=lambda t: pd.Timestamp(t["entry_dt"]))
    return out


def build_html(trades: list[dict], meta: dict) -> str:
    """損益タブ風の HTML(KPIカード + 銘柄別サマリー + 取引明細)を返す。"""
    n = len(trades)
    win = sum(1 for t in trades if t["pnl"] > 0)
    gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    tot = gp + gl
    pf = gp / -gl if gl < 0 else (float("inf") if gp > 0 else 0.0)
    wr = win / n * 100 if n else 0.0
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"

    def _card(label, val, color="#e2e8f0"):
        return (f'<div class="card"><div class="card-l">{label}</div>'
                f'<div class="card-v" style="color:{color}">{val}</div></div>')

    cards = "".join([
        _card("総取引数", f"{n:,}件"),
        _card("勝率", f"{wr:.1f}%", "#4ade80" if wr >= 55 else "#e2e8f0"),
        _card("利益合計", f"+{gp:,.0f}円", "#4ade80"),
        _card("損失合計", f"{gl:,.0f}円", "#f87171"),
        _card("合計損益", f"{tot:+,.0f}円", "#4ade80" if tot >= 0 else "#f87171"),
        _card("PF", pf_s, "#4ade80" if pf >= 1 else "#f87171"),
        _card("勝ち/負け", f"{win}W / {n-win}L"),
    ])

    # 銘柄別サマリー
    by_sym: dict = {}
    for t in trades:
        k = (t["symbol"], t["name"])
        d = by_sym.setdefault(k, [0, 0, 0.0])
        d[0] += 1; d[1] += 1 if t["pnl"] > 0 else 0; d[2] += t["pnl"]
    sym_rows = ""
    for (sym, name), (c, w, p) in sorted(by_sym.items(), key=lambda kv: -kv[1][2]):
        col = "#4ade80" if p >= 0 else "#f87171"
        sym_rows += (f'<tr><td>{sym}</td><td>{name}</td><td class="r">{c}</td>'
                     f'<td class="r">{w/c*100:.0f}%</td>'
                     f'<td class="r" style="color:{col}">{p:+,.0f}</td></tr>')

    # ── 月別・日別集計 (決済日基準) + 必要資金(同時保有ピーク) ──
    def _pnl_cell(v):
        c = "#4ade80" if v >= 0 else "#f87171"
        return f'<span style="color:{c}">{v:+,.0f}円</span>'

    # 決済日ごと集計
    by_day: dict = {}      # 'YYYY-MM-DD' -> [n, win, pnl]
    by_month: dict = {}    # 'YYYY-MM'    -> [n, win, gp, gl]
    for t in trades:
        xd = pd.Timestamp(t["exit_dt"])
        dk = xd.strftime("%Y-%m-%d"); mk = xd.strftime("%Y-%m")
        d = by_day.setdefault(dk, [0, 0, 0.0])
        d[0] += 1; d[1] += 1 if t["pnl"] > 0 else 0; d[2] += t["pnl"]
        m = by_month.setdefault(mk, [0, 0, 0.0, 0.0])
        m[0] += 1; m[1] += 1 if t["pnl"] > 0 else 0
        if t["pnl"] > 0: m[2] += t["pnl"]
        else: m[3] += t["pnl"]

    # 必要資金(同時保有ピーク): 各エントリー日で同時保有中の建玉数と評価額を数え、月別に最大を取る
    peak_by_month: dict = {}   # 'YYYY-MM' -> [max_cnt, max_cap]
    for t in trades:
        d0 = pd.Timestamp(t["entry_dt"])
        openpos = [u for u in trades
                   if pd.Timestamp(u["entry_dt"]) <= d0 <= pd.Timestamp(u["exit_dt"])]
        cnt = len(openpos)
        cap = sum(u["entry_p"] * u["qty"] for u in openpos)
        mk = d0.strftime("%Y-%m")
        pk = peak_by_month.setdefault(mk, [0, 0.0])
        if cnt > pk[0]:
            pk[0] = cnt; pk[1] = cap

    max_abs = max((abs(m[2] + m[3]) for m in by_month.values()), default=1) or 1
    months_desc = sorted(by_month.keys(), reverse=True)

    # 月次テーブル
    mrows = ""
    for mk in months_desc:
        c, w, gpp, gll = by_month[mk]
        mt = gpp + gll
        bar_w = int(abs(mt) / max_abs * 90)
        bcol = "#16a34a" if mt >= 0 else "#dc2626"
        pk = peak_by_month.get(mk, [0, 0.0])
        cap_str = f"{pk[1]:,.0f}円" if pk[0] else "—"
        mtcol = "#4ade80" if mt >= 0 else "#f87171"
        mrows += (
            f'<tr><td><b>{mk.replace("-","/")}月</b></td><td class="r">{c}件</td>'
            f'<td class="r">{w/c*100:.0f}%</td>'
            f'<td class="r prof">+{gpp:,.0f}円</td>'
            f'<td class="r loss">{gll:,.0f}円</td>'
            f'<td class="r"><span style="color:{mtcol};font-weight:700">{mt:+,.0f}円</span>'
            f' <span style="display:inline-block;height:9px;width:{bar_w}px;'
            f'background:{bcol};border-radius:2px;vertical-align:middle"></span></td>'
            f'<td class="r" style="color:#38bdf8">{cap_str}'
            f' <span style="font-size:0.68rem;color:#64748b">×{pk[0]}</span></td></tr>')

    # 月ごとの取引明細(折りたたみ) — 買った日/売った日/買値/売値/保有/結果/損益
    _rj = {"target": ("利確", "#4ade80"), "stop": ("損切", "#f87171"),
           "timecut": ("時間切", "#94a3b8")}

    def _detail_row(t):
        rlbl, rcol = _rj.get(t["reason"], (t["reason"], "#94a3b8"))
        pcol = "#4ade80" if t["pnl"] >= 0 else "#f87171"
        e = pd.Timestamp(t["entry_dt"]).strftime("%Y-%m-%d")
        x = pd.Timestamp(t["exit_dt"]).strftime("%Y-%m-%d")
        gain = "#4ade80" if t["exit_p"] >= t["entry_p"] else "#f87171"
        return (
            f'<tr><td>{e}</td><td>{x}</td><td>{t["symbol"]}</td>'
            f'<td>{t["name"]}</td><td class="r">{t["entry_p"]:,}</td>'
            f'<td class="r" style="color:{gain}">{t["exit_p"]:,}</td>'
            f'<td class="r">{t["qty"]}</td><td class="r">{t["hold"]}日</td>'
            f'<td style="color:{rcol};font-weight:600">{rlbl}</td>'
            f'<td class="r" style="color:{pcol};font-weight:700">{t["pnl"]:+,.0f}</td></tr>')

    _dhead = ('<thead><tr><th>買った日</th><th>売った日</th><th>銘柄</th><th>名前</th>'
              '<th class="r">買値</th><th class="r">売値</th><th class="r">株数</th>'
              '<th class="r">保有</th><th>結果</th><th class="r">損益(円)</th></tr></thead>')
    month_detail = ""
    for mk in months_desc:
        c, w, gpp, gll = by_month[mk]
        mt = gpp + gll
        mtc = "#4ade80" if mt >= 0 else "#f87171"
        mtr = sorted([t for t in trades
                      if pd.Timestamp(t["exit_dt"]).strftime("%Y-%m") == mk],
                     key=lambda x: pd.Timestamp(x["exit_dt"]), reverse=True)
        rows = "".join(_detail_row(t) for t in mtr)
        month_detail += (
            f'<details class="mblock"><summary>{mk.replace("-","/")}　'
            f'{c}件 {w/c*100:.0f}%　'
            f'<span class="prof">+{gpp:,.0f}</span> '
            f'<span class="loss">{gll:,.0f}</span> = '
            f'<span style="color:{mtc};font-weight:700">{mt:+,.0f}円</span></summary>'
            f'<div class="wrap" style="margin:4px 10px 8px"><table>{_dhead}'
            f'<tbody>{rows}</tbody></table></div></details>')

    bt = meta.get("bt", {})
    bt_line = ""
    if bt:
        bt_line = (f'横ばい全期間BT: {bt.get("n",0)}件 PF{bt.get("pf","-")} '
                   f'損益{bt.get("pnl",0):+,.0f}円')

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>指数ETF横ばい戦略 {meta.get('period','')}</title>
<style>
  body {{ background:#0a0e1a; color:#e2e8f0; font-family:'Segoe UI',sans-serif; margin:0; padding:12px 16px; font-size:13px; }}
  h1 {{ font-size:1.05rem; margin:0 0 2px; }}
  .sub {{ color:#94a3b8; font-size:0.72rem; margin-bottom:10px; }}
  .cards {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }}
  .card {{ background:#111827; border:1px solid #1e293b; border-radius:7px; padding:5px 11px; min-width:82px; }}
  .card-l {{ color:#94a3b8; font-size:0.68rem; margin-bottom:1px; }}
  .card-v {{ font-size:1.05rem; font-weight:700; }}
  h2 {{ font-size:0.85rem; margin:12px 0 5px; border-left:3px solid #3b82f6; padding-left:8px; }}
  table {{ border-collapse:collapse; width:100%; font-size:0.74rem; }}
  th,td {{ padding:2px 8px; border-bottom:1px solid #16202f; text-align:left; white-space:nowrap; }}
  th {{ color:#94a3b8; font-weight:600; background:#0d1424; position:sticky; top:0; }}
  td.r,th.r {{ text-align:right; }}
  tr:hover td {{ background:#0d1424; }}
  .wrap {{ max-height:52vh; overflow:auto; border:1px solid #1e293b; border-radius:7px; }}
  .mblock {{ background:#111827; border:1px solid #1e293b; border-radius:6px; margin:4px 0; }}
  .mblock summary {{ cursor:pointer; padding:6px 12px; font-size:0.78rem; }}
  .mblock summary:hover {{ background:#0d1424; }}
  .prof {{ color:#4ade80; }}
  .loss {{ color:#f87171; }}
  td.prof {{ color:#4ade80; background:rgba(22,163,74,0.12); }}
  td.loss {{ color:#f87171; background:rgba(220,38,38,0.12); }}
</style></head><body>
<h1>🟡 横ばい相場用 指数ETF戦略（STOロングbounce）</h1>
<div class="sub">期間 {meta.get('period','')} ／ {meta.get('scope','')} ／ preset={meta.get('preset','')}
  ／ コスト 信用0.03%＋スリッページ0.30%<br>{bt_line}</div>
<div class="cards">{cards}</div>
<h2>月別損益（決済日基準）</h2>
<div class="wrap"><table>
<thead><tr><th>月</th><th class="r">件数</th><th class="r">勝率</th>
<th class="r prof">利益</th><th class="r loss">損失</th>
<th class="r">損益合計</th><th class="r">必要資金<br><span style="font-size:0.7rem">同時保有ピーク</span></th></tr></thead>
<tbody>{mrows}</tbody></table></div>
<h2>月別 取引詳細（月をクリックで展開）</h2>
{month_detail}
<h2>銘柄別サマリー（損益降順）</h2>
<div class="wrap"><table>
<thead><tr><th>銘柄</th><th>名前</th><th class="r">取引</th><th class="r">勝率</th><th class="r">損益(円)</th></tr></thead>
<tbody>{sym_rows}</tbody></table></div>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="判定日 YYYY-MM-DD (既定=最新)")
    ap.add_argument("--preset", default="wide", choices=["wide", "atr1R"])
    ap.add_argument("--backtest", action="store_true", help="非相関ユニバースで直近成績を再計算")
    ap.add_argument("--trades", action="store_true", help="過去の横ばい局面の実トレードを1件ずつ表示")
    ap.add_argument("--html", action="store_true", help="損益タブ風HTMLで出力(--trades期間を使用)")
    ap.add_argument("--no-browser", action="store_true", help="--html でブラウザ自動起動しない")
    ap.add_argument("--from", dest="dfrom", default="2021-01-01", help="--trades/--html 開始日")
    ap.add_argument("--to", dest="dto", default="2023-12-31", help="--trades/--html 終了日")
    ap.add_argument("--symbol", default=None, help="対象を1銘柄に絞る(例 1321.T)")
    ap.add_argument("--all-regime", action="store_true", help="横ばい以外も含める")
    ap.add_argument("--force-signal", action="store_true",
                    help="レジームゲートを無視して条件成立銘柄を表示(検証用)")
    args = ap.parse_args()

    if args.html:
        tr = trades_in_window(args.preset, args.dfrom, args.dto, args.symbol,
                              sideways_only=not args.all_regime)
        bt = backtest(args.preset)["sideways"]
        pf_s = "∞" if bt["pf"] == float("inf") else f"{bt['pf']:.2f}"
        meta = {"period": f"{args.dfrom}〜{args.dto}",
                "scope": "全レジーム" if args.all_regime else "横ばいのみ",
                "preset": args.preset,
                "bt": {"n": bt["n"], "pf": pf_s, "pnl": bt["pnl"]}}
        html = build_html(tr, meta)
        fname = f"signals_index_{args.dfrom}_{args.dto}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML出力: {fname} ({len(tr)}件)")
        if not args.no_browser:
            try:
                from _open_html import open_html
                open_html(fname)
            except Exception:
                import webbrowser
                webbrowser.open(fname)
        return

    if args.trades:
        tr = trades_in_window(args.preset, args.dfrom, args.dto, args.symbol,
                              sideways_only=not args.all_regime)
        scope = "全レジーム" if args.all_regime else "横ばいのみ"
        print(f"=== 過去トレード明細 {args.dfrom}〜{args.dto} / {scope} / preset={args.preset}"
              f"{' / '+args.symbol if args.symbol else ''} ===")
        if not tr:
            print("  該当トレードなし")
            return
        print(f"{'約定日':<12}{'決済日':<12}{'銘柄':<9}{'名前':<15}{'買値':>8}{'売値':>8}"
              f"{'保有':>4}{'結果':>8}{'損益':>10}")
        print("-" * 96)
        tot = 0.0; win = 0
        _rj = {"target": "利確", "stop": "損切", "timecut": "時間切"}
        for t in tr:
            tot += t["pnl"]; win += 1 if t["pnl"] > 0 else 0
            e = pd.Timestamp(t["entry_dt"]).strftime("%Y-%m-%d")
            x = pd.Timestamp(t["exit_dt"]).strftime("%Y-%m-%d")
            print(f"{e:<12}{x:<12}{t['symbol']:<9}{t['name'][:14]:<15}"
                  f"{t['entry_p']:>8,}{t['exit_p']:>8,}{t['hold']:>4}"
                  f"{_rj.get(t['reason'], t['reason']):>8}{t['pnl']:>+10,.0f}")
        n = len(tr)
        print("-" * 96)
        print(f"合計 {n}件 / 勝率 {win/n*100:.0f}% / 損益 {tot:+,.0f}円 / 平均 {tot/n:+,.0f}円/件")
        return

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
