r"""analyze_overnight_lss.py — 「終値で入って翌日売る」を lss のシグナル集団で測る。

問い
----
現行 lss = シグナル日Dの引け後に逆指値売りを出し、翌日D+1に
『前日終値-1ティックを割ったら』約定 → **同日決済**。

案 = D の引けでそのまま空売りし、D+1 に買い戻す(持ち越し)。
トリガー(下ブレイク待ち)を捨て、保有を夜またぎにする。

既にわかっていること
--------------------
§18.19 で **無条件の overnight ドリフトはゼロ** と実測済み
(604,626銘柄日 / 511営業日 / ショート -17円/件 / t=-0.14 / CI -277〜+242)。
夜間に取れるドリフトは存在しない。ただしこれは全銘柄・全日の測定で、
**lss のシグナルが出た銘柄に限った持ち越し**は測っていない。ここを埋める。

§18.19 のもう一つの結論: エッジは「下ブレイクへの反応」。終値で入るとその条件を
捨てるので、不利が予想される。予想が外れるかどうかを見る。

測るもの (すべて同じシグナル集団・100株・摩擦なし・ショート)
--------------------------------------------------------
  現行     : oos_raw の pnl (D+1 にトリガー約定 → 同日決済)
  A 引け→翌寄り  : D終値で空売り → D+1始値で買い戻し (純オーバーナイト)
  B 引け→翌引け  : D終値で空売り → D+1終値で買い戻し (夜+日中まるごと。無条件)
  C 翌寄り→翌引け: D+1始値で空売り → D+1終値で買い戻し (トリガー無しの日中)
  D 引け+OCO     : D終値で空売り → D+1を**現行と同じ決済**で処理
                   (損切 +sm×ATR / 利確 -tm×ATR / 未達なら引けタイムカット)
  E 翌寄り+OCO   : D+1始値で空売り → 同じ決済。**実装できるのはこちら**
  F 5分足寄り+OCO: E と同じだが 5分足の先頭バー始値で入る(データ整合の確認用)
  G 翌寄りロング  : D+1始値で **買い** → 損切-sm×ATR / 利確+tm×ATR。E の鏡像。
                   §18.18 の LDT はトリガー付き(前日終値+1ティックの逆指値買い)
                   だったので別物。トリガー無しのロングは未測定だった。

A/B/C は無条件決済なので、そのままでは現行と比べられない(現行はOCOで決済している)。
決済を揃えた D/E で判断する。A/B/C は内訳(夜 / 日中)を読むための分解。

⛔⛔ **D は実装できない(look-ahead)**
   lss のシグナルは **D の終値**で判定する(§6 引け後運用)。その終値で入るには
   終値が出る前に注文を出す必要があり、順序が成立しない。
   近似するなら 14:55 時点の値でシグナルを仮判定して MOC を出すことになるが、
   本ツールの D は『終値を見たうえで終値で約定する』完全な先読みを含む。
   **D は上限値であって、実現可能な成績ではない。**
   実装できるのは E(シグナルは D の終値で確定 → 翌朝の寄り成行)。

⚠ D は夜間のギャップを損切りできない。D+1 が損切りの上に飛んで始まったら
   `max(stop, バー始値)`(v16)で不利約定になる。これは現行(同日決済)には無い
   リスクで、まさにこの案の弱点そのものなので、モデルに含めてある。

使い方
------
  python analyze_overnight_lss.py --raw "oos_raw_fold*.csv"
  python analyze_overnight_lss.py --raw "oos_raw_fold*.csv" --workers 8 --by-month

⚠ 持ち越しには測定に出ないコストがある(逆日歩・一般信用デイトレが使えない・
   夜間のギャップで損切りが効かない・証拠金が2日拘束され資本回転が半減)。
   数字が並んでも、それだけで採用理由にはならない。
"""
from __future__ import annotations

import argparse
import glob as _glob
import statistics as _st
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

ap = argparse.ArgumentParser(description="lss シグナルでの持ち越しを測る")
ap.add_argument("--raw", required=True, help="生トレードCSV(グロブ可)")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--bt-min", type=float, default=0.0)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--by-month", action="store_true")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍(現行0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍(現行1.0)")
ap.add_argument("--stop-delay-bars", type=int, default=1,
                help="E(翌寄り成行)の損切り遅延。寄り約定→最初の5分足が閉じてから"
                     "逆指値を置くので live と同じ 1 が既定")
ap.add_argument("--d-stop-delay", dest="d_stop_delay", type=int, default=0,
                help="D(引け+OCO)の損切り遅延。前日引けから建玉があるので逆指値は"
                     "**寄り前に置ける** → 0 が既定(delay1 は約定バーに置けない"
                     "という機構的理由なので D には当てはまらない)")
ap.add_argument("--exclude-months", default="",
                help="除外する月(YYYY-MM, カンマ区切り)。配当落ちの影響を外す用途。"
                     "日本株は3月期末・9月中間に権利落ちが集中し、空売りは配当落調整金を"
                     "払うので、夜またぎの利益はその分だけ現実には存在しない")
ap.add_argument("--gap-guard", type=float, default=0.03,
                help="現行と同じ指値下限ガード。寄りが前日終値×(1-これ)を下回る"
                     "ギャップダウンは約定不可としてスキップ"
                     "(backtest_limit_entry._INTRADAY_5M_ENTRY_GAP_LIMIT=0.03 と同値)。"
                     "0=ガード無し")
ap.add_argument("--control", type=int, default=0,
                help="対照実験の件数。同じ銘柄・**シグナルが出ていない日**に同じ"
                     "OCOで寄り成行ショートする。ここでも同じだけ稼げるなら、"
                     "E の成績はエッジではなく OCO の払い出し形状の産物。"
                     "推奨: シグナル件数と同程度(例 4000)")
ap.add_argument("--control-seed", type=int, default=42)
ap.add_argument("--require-open-bar", action="store_true",
                help="5分足の先頭バーが 09:00 の日だけ使う。寄り成行で入るのに"
                     "先頭が 09:05 だと寄り直後の逆行が測られず、損切りが過小に出る")
ap.add_argument("--out-raw", default="",
                help="E の損益を生CSV形式(oos_raw と同じ列)で書き出す。"
                     "sim_oos_budget.py に食わせて**予算制約下**で現行と比較するため"
                     "(18.10: 全部買えるなら得 と 予算内でどれを買うか は別問題)")
ap.add_argument("--sweep-sm", default="",
                help="sm(損切ATR倍)のスイープ。例 0.1,0.3,0.5,1.0")
ap.add_argument("--sweep-tm", default="",
                help="tm(利確ATR倍)のスイープ。例 0.3,0.5,1.0,1.5,2.0。"
                     "--sweep-sm と併せて E(ショート)/G(ロング)の 円/件 表を出す。"
                     "『ロングは別のパラメータなら勝てるのでは』を直接確かめる用")
ap.add_argument("--no-oco", action="store_true",
                help="D(引け+OCO)を計算しない。5分足が無い環境向け")
a = ap.parse_args()

files = sorted(_glob.glob(a.raw)) if any(c in a.raw for c in "*?[") else [a.raw]
if not files:
    sys.exit(f"[error] {a.raw} に一致するファイルがありません")
d = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in files], ignore_index=True)
if "strategy" in d.columns:
    d = d[d["strategy"].astype(str) != "転換"]        # lss ではない(18.5.3)
if a.bt_min > 0 and "bt_score" in d.columns:
    d = d[pd.to_numeric(d["bt_score"], errors="coerce").fillna(0) >= a.bt_min]
d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
d = d[d["entry_date"].notna()].copy()
# 同じ(銘柄,日)は戦略が違っても同じ1トレードになる(トリガー/決済が同一)。
# 持ち越し案も同じなので、重複を排して1銘柄1日1件にする。
if a.exclude_months.strip():
    _ex = {m.strip() for m in a.exclude_months.split(",") if m.strip()}
    _n0 = len(d)
    d = d[~d["oos_month"].astype(str).isin(_ex)]
    print(f"[除外] 月 {sorted(_ex)} → {_n0:,}→{len(d):,}件")
# lss が実際に約定したか(=トリガーに届いたか)。E の内訳を割るのに使う。
_fill_any = (d.groupby(["symbol", "entry_date"])["filled"].max()
             if "filled" in d.columns else None)
# lss の実際の約定値 = min(トリガー, 始値)(§18.8)。E との建値差を出すのに使う。
_lss_ep = None
if "filled" in d.columns and "entry_p" in d.columns:
    _f1 = d[d["filled"] == 1]
    if len(_f1):
        _lss_ep = _f1.groupby(["symbol", "entry_date"])["entry_p"].max()
u = d.drop_duplicates(subset=["symbol", "entry_date"])[
    ["symbol", "entry_date", "oos_month"]].copy()
print(f"[入力] {len(files)}ファイル / シグナル {len(d):,}件 "
      f"→ 銘柄×日 {len(u):,}件 / {u['entry_date'].min().date()}〜"
      f"{u['entry_date'].max().date()}")

try:
    from backtest_limit_entry import fetch as _fetch
except Exception as e:
    sys.exit(f"[error] backtest_limit_entry を import できません: {e}")

_syms = sorted(u["symbol"].astype(str).unique())
print(f"[日足] {len(_syms):,}銘柄を取得中(キャッシュ利用)...")
_bars: dict = {}


def _load(sym: str):
    """日足を (o,h,l,c,atr) に整える。ATR は backtest_limit_entry と同じ
    TR の EMA(span=14) で作る(calc_macd 等と同一定義)。"""
    try:
        df = _fetch(sym, 900)
        if df is None or df.empty:
            return sym, None
        df = df.copy()
        df.index = pd.to_datetime(df.index).normalize()
        cols = {str(c).lower(): c for c in df.columns}
        if any(cols.get(x) is None for x in ("open", "high", "low", "close")):
            return sym, None
        out = df.rename(columns={cols["open"]: "o", cols["high"]: "h",
                                 cols["low"]: "l", cols["close"]: "c"})[
            ["o", "h", "l", "c"]].astype(float)
        pc = out["c"].shift(1)
        tr = pd.concat([out["h"] - out["l"], (out["h"] - pc).abs(),
                        (out["l"] - pc).abs()], axis=1).max(axis=1)
        out["atr"] = tr.ewm(span=14, adjust=False).mean()
        return sym, out
    except Exception:
        return sym, None


with ThreadPoolExecutor(max_workers=a.workers) as ex:
    for i, fut in enumerate(as_completed([ex.submit(_load, s) for s in _syms]), 1):
        s, df = fut.result()
        _bars[s] = df
        if i % 200 == 0:
            print(f"  ...{i}/{len(_syms)}", flush=True)

# ── D(引け+OCO) 用の5分足 ────────────────────────────────────────
_i5: dict = {}
if not a.no_oco:
    try:
        from daytrade_data import load_intraday as _li, split_by_day as _sbd
        from sameday5m_firsttouch import short_exit_5m as _x5
        from sameday5m_firsttouch import long_exit_5m as _l5
        from intraday_integrity import day_scale_ok as _ig_ok   # 18.27 分割未調整ガード
    except Exception as e:
        print(f"[warn] 5分足の経路が使えません({e}) → D(引け+OCO)はスキップ")
        a.no_oco = True
if not a.no_oco:
    print(f"[5分足] {len(_syms):,}銘柄を読込中...")

    def _load5(sym: str):
        try:
            m5 = _li(sym, days=900, source="local")
            return sym, (_sbd(m5) if m5 is not None and len(m5) else {})
        except Exception:
            return sym, {}

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, fut in enumerate(as_completed([ex.submit(_load5, s) for s in _syms]), 1):
            s5, day5 = fut.result()
            _i5[s5] = day5
            if i % 200 == 0:
                print(f"  ...{i}/{len(_syms)}", flush=True)

_bar0: dict = defaultdict(int)
_recs = []
_miss = 0
_no5 = 0
_reasons: dict = defaultdict(int)
for sym, ed, om in u[["symbol", "entry_date", "oos_month"]].itertuples(index=False):
    df = _bars.get(str(sym))
    if df is None:
        _miss += 1
        continue
    idx = df.index
    pos = idx.searchsorted(pd.Timestamp(ed))
    # entry_date(=D+1) の行と、その1本前(=D, シグナル日)が要る
    if pos <= 0 or pos >= len(idx) or idx[pos] != pd.Timestamp(ed):
        _miss += 1
        continue
    pc = float(df["c"].iloc[pos - 1])      # D の終値 = 入る値
    o1 = float(df["o"].iloc[pos])          # D+1 の始値
    c1 = float(df["c"].iloc[pos])          # D+1 の終値
    if not (pc > 0 and o1 > 0 and c1 > 0):
        _miss += 1
        continue
    q = a.qty
    rec = {
        "date": ed, "month": om, "symbol": sym, "entry_p": pc,
        "A_引け→翌寄り": (pc - o1) * q,     # ショート: 入った値 − 返した値
        "B_引け→翌引け": (pc - c1) * q,
        "C_翌寄り→翌引け": (o1 - c1) * q,
        "D_引け+OCO": None,
        "E_翌寄り+OCO": None,
        "F_5分足寄り+OCO": None,
        "G_翌寄りロング+OCO": None,
        "始値差bp": None,
        "lss約定": (int(_fill_any.get((sym, ed), 0)) if _fill_any is not None else -1),
        "bar0": "",
        "lss建値": (float(_lss_ep.get((sym, ed), 0) or 0)
                    if _lss_ep is not None else 0.0),
        "_o1": o1, "_c1": c1, "_dl": 0.0, "_dh": 0.0, "_atr": 0.0,
    }
    # ── D/E: 現行と同じ OCO で決済 ──
    if not a.no_oco:
        atr = float(df["atr"].iloc[pos - 1])       # D 時点の ATR
        day5 = _i5.get(str(sym), {}).get(pd.Timestamp(ed).date())
        if day5 is None or len(day5) == 0 or not (atr == atr and atr > 0):
            _no5 += 1
        elif not _ig_ok(day5, c1):                 # 5分足の分割未調整(18.27)
            _no5 += 1
        else:
            # ⛔ short_exit_5m は『トリガーで約定する』前提で、_start=ei
            #    (=最初に安値が entry_p に達したバー)から損切り・利確を見る。
            #    D/E は **寄りから建玉がある** ので、そのまま渡すと
            #    「寄りが建値より上に飛んで昼に戻ってきた」ケースで
            #    **朝の含み損の区間が丸ごと飛ばされる** = 負けだけが消える。
            #    entry_p に +inf を渡して ei=0 を強制し、必ず寄りから判定させる。
            #    stop_p / target_p は別引数なので、これで値は一切歪まない。
            try:
                _b0 = str(pd.Timestamp(day5.index[0]).strftime("%H:%M"))
            except Exception:
                _b0 = "??:??"
            _bar0[_b0] += 1
            rec["bar0"] = _b0
            if a.require_open_bar and _b0 != "09:00":
                _recs.append(rec)
                continue
            _INF = float("inf")
            _dl, _dh = float(df["l"].iloc[pos]), float(df["h"].iloc[pos])
            rec["_dl"], rec["_dh"], rec["_atr"] = _dl, _dh, atr
            # 現行と同じ指値下限ガード: 寄りが前日終値×(1-guard)を下回るギャップ
            # ダウンは約定不可(現行 lss も同じ理由でスキップする)。
            _gap_ng = (a.gap_guard > 0 and o1 < pc * (1.0 - a.gap_guard))
            # ⛔ E のエントリーは日足(yfinance)の始値、決済判定は5分足(J-Quants)。
            #    出所が違うので、この2つがずれていると差額がそのまま利益になる。
            #    F = 5分足の先頭バーの始値で入る版。E と F が乖離するなら、
            #    E の一部は『データ間のズレ』を拾っているだけ。
            try:
                _o5 = float(day5["open"].iloc[0])
            except Exception:
                _o5 = 0.0
            if _o5 > 0:
                rec["始値差bp"] = (o1 - _o5) / _o5 * 10_000
            # ロング側のガードは鏡像(+3%を超えるギャップアップは約定不可)
            _gap_ng_l = (a.gap_guard > 0 and o1 > pc * (1.0 + a.gap_guard))
            for _tag, _ep, _dly, _long in (
                    ("D_引け+OCO", pc, a.d_stop_delay, False),
                    ("E_翌寄り+OCO", o1, a.stop_delay_bars, False),
                    ("F_5分足寄り+OCO", _o5, a.stop_delay_bars, False),
                    ("G_翌寄りロング+OCO", o1, a.stop_delay_bars, True)):
                if _ep <= 0:
                    continue
                if not _long and _gap_ng and not _tag.startswith("D_"):
                    continue                      # 寄り約定はガードが効く
                if _long and _gap_ng_l:
                    continue
                if _long:
                    # ロング: 損切=下 / 利確=上。entry_p に -inf を渡して ei=0 を強制
                    # (寄りから建玉がある。short 側と同じ理由 = §18.32 のバグ①)
                    xp, why, _e5, _x5t = _l5(
                        day5, -_INF, _ep - atr * a.sm, _ep + atr * a.tm,
                        day_high=_dh, day_close=c1, stop_delay_bars=_dly)
                else:
                    xp, why, _e5, _x5t = _x5(
                        day5, _INF, _ep + atr * a.sm, _ep - atr * a.tm, False,
                        day_low=_dl, day_high=_dh, day_close=c1,
                        stop_delay_bars=_dly)
                if xp is None or why in ("no_5m", "no_entry"):
                    continue
                rec[_tag] = ((float(xp) - _ep) if _long else (_ep - float(xp))) * q
                _reasons[(_tag[0], why)] += 1
    _recs.append(rec)
if _no5:
    print(f"[warn] 5分足/ATRが揃わず D を計算できず {_no5:,}件")
if _miss:
    print(f"[warn] 日足が揃わず除外 {_miss:,}件")
r = pd.DataFrame(_recs)
if r.empty:
    sys.exit("[error] 計算できる行がありません")

_cur = d.drop_duplicates(subset=["symbol", "entry_date"])
_cur = _cur[_cur["filled"] == 1] if "filled" in _cur.columns else _cur
_cur_pnl = pd.to_numeric(_cur["pnl"], errors="coerce").fillna(0)

print(f"\n■ 同じシグナル集団での比較 ({len(r):,}銘柄日 / "
      f"{r['date'].nunique():,}営業日 / {a.qty}株 / 摩擦なし)")
print(f"  {'方式':<18}{'件数':>7}{'勝率':>7}{'合計':>14}{'円/件':>9}{'bp/件':>8}"
      f"{'日クラスタt':>11}")
print(f"  {'現行 lss (参考)':<18}{len(_cur_pnl):>7,}"
      f"{(_cur_pnl > 0).mean()*100:>6.1f}%{_cur_pnl.sum():>+14,.0f}"
      f"{_cur_pnl.mean():>+9,.0f}{'—':>8}{'—':>11}")
_res = {}
_cols = ["A_引け→翌寄り", "B_引け→翌引け", "C_翌寄り→翌引け"]
if not a.no_oco:
    _cols += ["D_引け+OCO", "E_翌寄り+OCO", "F_5分足寄り+OCO",
              "G_翌寄りロング+OCO"]
for col in _cols:
    v = r[col].dropna()
    if v.empty:
        continue
    sub = r.loc[v.index]
    bp = (v / (sub["entry_p"] * a.qty) * 10_000).mean()
    dm = sub.assign(_v=v).groupby("date")["_v"].mean()
    t = (dm.mean() / (dm.std(ddof=1) / (len(dm) ** 0.5))) if len(dm) > 1 else 0.0
    _res[col] = (v.sum(), v.mean(), bp, t)
    _mark = ("  ← 引け成行(要 look-ahead)" if col.startswith("D_") else
             "  ← 寄り成行(日足の始値)" if col.startswith("E_") else
             "  ← 寄り成行(5分足の始値)" if col.startswith("F_") else
             "  ← **ロング**(E の鏡像)" if col.startswith("G_") else "")
    print(f"  {col:<18}{len(v):>7,}{(v > 0).mean()*100:>6.1f}%{v.sum():>+14,.0f}"
          f"{v.mean():>+9,.0f}{bp:>+8.1f}{t:>+11.2f}{_mark}")
for _tag in ("D", "E", "F", "G"):
    _rs = {k[1]: v for k, v in _reasons.items() if k[0] == _tag}
    if _rs:
        _tot5 = sum(_rs.values())
        print(f"  {_tag} の決済理由: " + " / ".join(
            f"{k} {v:,}件({v / _tot5 * 100:.0f}%)" for k, v in
            sorted(_rs.items(), key=lambda x: -x[1])))

_a, _b, _c = (_res["A_引け→翌寄り"][1], _res["B_引け→翌引け"][1],
              _res["C_翌寄り→翌引け"][1])
print(f"\n  分解: B(夜+日中) = A(夜) + C(日中)  →  "
      f"{_b:+,.0f} ≒ {_a:+,.0f} + {_c:+,.0f}")
print(f"    夜のぶん {_a:+,.0f}円/件 (t={_res['A_引け→翌寄り'][3]:+.2f})  /  "
      f"日中のぶん {_c:+,.0f}円/件 (t={_res['C_翌寄り→翌引け'][3]:+.2f})")

if a.by_month:
    print(f"\n■ 月別 (円/件)")
    print(f"  {'月':<10}{'件数':>7}{'現行':>10}" +
          "".join(f"{c.split('_')[0]:>10}" for c in
                  ("A_", "B_", "C_")))
    _cm = _cur.assign(_p=_cur_pnl).groupby("oos_month")["_p"].agg(["size", "mean"])
    for m, g in r.groupby("month"):
        cur = _cm["mean"].get(m, float("nan"))
        print(f"  {str(m):<10}{len(g):>7,}"
              f"{(f'{cur:+,.0f}' if cur == cur else '—'):>10}"
              + "".join(f"{g[c].mean():>+10,.0f}" for c in
                        ("A_引け→翌寄り", "B_引け→翌引け", "C_翌寄り→翌引け")))

# ── sm/tm スイープ (E ショート / G ロング) ────────────────────────
# 「ロングは別のパラメータなら勝てるのでは」への直接の答え。
# ⚠ 理屈の上では、条件付きの日中ドリフトが負なら **任意の停止則でロングの期待値は
#    0以下**(optional stopping)。バリアは分布の形を変えるだけで平均の符号は変えない。
#    ただしそれは「1日通して平均が負」の話なので、午前に上げて午後に下げる形なら
#    早い利確で取れる余地は残る。だから実際に掃く。
if a.sweep_sm.strip() and a.sweep_tm.strip() and not a.no_oco:
    _sms = [float(x) for x in a.sweep_sm.split(",") if x.strip()]
    _tms = [float(x) for x in a.sweep_tm.split(",") if x.strip()]
    print(f"\n■ sm/tm スイープ (円/件 / 母集団 {len(r):,}銘柄日 / 摩擦なし)")
    print(f"  ※ 5分足とATRが揃った行のみ評価。--require-open-bar 指定時は"
          f"09:00始まりの日だけ(本体の E/G と同じ母集団)。")
    for _lab, _long in (("E ショート", False), ("G ロング", True)):
        print(f"\n  {_lab}   {'sm＼tm':>8}" +
              "".join(f"{t:>10.1f}" for t in _tms))
        for sm_ in _sms:
            line = f"  {'':<10}{sm_:>8.1f}"
            for tm_ in _tms:
                vals = []
                for _rc in _recs:
                    day5 = _i5.get(str(_rc["symbol"]), {}).get(_rc["date"].date())
                    if day5 is None or len(day5) == 0:
                        continue
                    _ep = _rc["_o1"]
                    _atr = _rc["_atr"]
                    if not (_ep > 0 and _atr > 0):
                        continue
                    if _long:
                        if a.gap_guard > 0 and _ep > _rc["entry_p"] * (1 + a.gap_guard):
                            continue
                        xp, why, _, _ = _l5(day5, -float("inf"), _ep - _atr * sm_,
                                            _ep + _atr * tm_, day_high=_rc["_dh"],
                                            day_close=_rc["_c1"],
                                            stop_delay_bars=a.stop_delay_bars)
                    else:
                        if a.gap_guard > 0 and _ep < _rc["entry_p"] * (1 - a.gap_guard):
                            continue
                        xp, why, _, _ = _x5(day5, float("inf"), _ep + _atr * sm_,
                                            _ep - _atr * tm_, False,
                                            day_low=_rc["_dl"], day_high=_rc["_dh"],
                                            day_close=_rc["_c1"],
                                            stop_delay_bars=a.stop_delay_bars)
                    if xp is None or why in ("no_5m", "no_entry"):
                        continue
                    vals.append(((float(xp) - _ep) if _long else (_ep - float(xp)))
                                * a.qty)
                line += f"{(sum(vals) / len(vals)) if vals else 0:>+10,.0f}"
            print(line)
    print(f"\n  → **G(ロング)がどの升目でもマイナスなら、パラメータの問題ではなく")
    print(f"     方向の問題**。C(バリア無しの素の測定)が日中 -29bp と言っているので、")
    print(f"     劣マルチンゲールでは任意の停止則でロングの期待値は0以下になる。")
    print(f"  ⚠ 一部の升目だけプラスでも採用しない。掃いた中の最良は必ず良く出る"
          f"(18.28 と同じ罠)。")

# ── 診断: 日足の始値 vs 5分足の先頭バーの始値 ──────────────────
if "始値差bp" in r.columns and r["始値差bp"].notna().any():
    _gd = r["始値差bp"].dropna()
    _e_, _f_ = r["E_翌寄り+OCO"].dropna(), r["F_5分足寄り+OCO"].dropna()
    print(f"\n■ 始値の出所ちがい (E=日足/yfinance の始値, F=5分足/J-Quants の先頭バー始値)")
    print(f"  差 (日足始値 − 5分足始値)  平均 {_gd.mean():+.1f}bp / "
          f"中央 {_gd.median():+.1f}bp / 一致 {(_gd.abs() < 1).mean()*100:.1f}%")
    if len(_e_) and len(_f_):
        print(f"  E {_e_.mean():+,.0f}円/件  vs  F {_f_.mean():+,.0f}円/件   "
              f"差 {_e_.mean() - _f_.mean():+,.0f}円/件")
    if abs(_gd.mean()) > 2:
        print(f"  ⛔ **平均で {_gd.mean():+.1f}bp ずれている。E はこのズレを利益として"
              f"計上している可能性がある。F を正とすること。**")
    else:
        print(f"  → ズレは無視できる。E と F の差も小さいはず。")

# ── なぜ E は現行より良いのか: 建値の差 と 持ち時間の差 に分ける ───────
if not a.no_oco and "lss建値" in r.columns:
    g = r[(r["lss建値"] > 0) & r["E_翌寄り+OCO"].notna()]
    if len(g):
        # ① 建値の差。現行の約定は min(トリガー, 始値) なので、E(始値)は必ず同じか高い。
        #    ショートは高く売るほど有利なので、この差は E の純粋な取り分。
        _pdiff = (g["_o1"] - g["lss建値"])
        _pyen = _pdiff * a.qty
        _pbp = (_pdiff / g["lss建値"] * 10_000)
        print(f"\n■ なぜ E は現行より良いのか ({len(g):,}件 = 両方が建てた取引)")
        print(f"  ① 建値の差 (E の始値 − 現行の約定値)")
        print(f"     平均 {_pdiff.mean():+.1f}円 = {_pbp.mean():+.1f}bp"
              f"  → **{_pyen.mean():+,.0f}円/件**")
        print(f"     E のほうが高く売れた割合 {(_pdiff > 0).mean()*100:.1f}%"
              f" / 同値 {(_pdiff.abs() < 0.01).mean()*100:.1f}%")
        # ② 残り = 持ち時間の差(9:00から持てる) + 決済経路の違い
        _eg = g["E_翌寄り+OCO"].mean()
        _cur_g = _cur.set_index(["symbol", "entry_date"])["pnl"] \
            if not _cur.empty else None
        _cm = g.apply(lambda x: float(_cur_g.get((x["symbol"], x["date"]), 0))
                      if _cur_g is not None else 0.0, axis=1)
        print(f"  ② 全体の差")
        print(f"     E {_eg:+,.0f}円/件  −  現行 {_cm.mean():+,.0f}円/件"
              f"  = **{_eg - _cm.mean():+,.0f}円/件**")
        print(f"  ③ 内訳")
        print(f"     建値の差で説明できる  {_pyen.mean():+,.0f}円/件"
              f"  ({_pyen.mean() / max(_eg - _cm.mean(), 1e-9) * 100:.0f}%)")
        print(f"     残り(=9:00から持てる持ち時間の差) "
              f"{_eg - _cm.mean() - _pyen.mean():+,.0f}円/件")
        print(f"  → 現行はトリガーに触れて初めて建玉が生まれるので、遅い約定ほど")
        print(f"     利確1.0ATRまでの残り時間が短い(§18.26: 〜09:05の約定 +1,517円/件 vs")
        print(f"     11:01〜 +505円/件)。E は9:00から日中ドリフトを丸ごと使える。")

# ── 対照実験: シグナルが出ていない日に同じ OCO を当てる ──────────────
# 「利確・損切を置けば何でも良く見えるのでは」への答え。損切0.1ATR/利確1.0ATR は
# 10:1 の宝くじ型なので、ドリフトが無くても払い出しの形だけで数字が動く余地がある。
# **同じ銘柄・同じ決済・シグナルが出ていない日**でも同じだけ稼げるなら、
# E の成績はエッジではなく OCO の形状(と測定の非対称)の産物ということになる。
if a.control > 0 and not a.no_oco:
    import random as _rnd
    _rg = _rnd.Random(a.control_seed)
    _sig_set = {(str(x), pd.Timestamp(y).date())
                for x, y in zip(u["symbol"], u["entry_date"])}
    _cv, _cd, _clv, _tried = [], [], [], 0
    _cands = [(sm, list(dd.keys())) for sm, dd in _i5.items() if dd]
    while len(_cv) < a.control and _tried < a.control * 60 and _cands:
        _tried += 1
        sm, days = _cands[_rg.randrange(len(_cands))]
        if not days:
            continue
        dt = days[_rg.randrange(len(days))]
        if (sm, dt) in _sig_set:
            continue
        df = _bars.get(sm)
        if df is None:
            continue
        idx = df.index
        pos = idx.searchsorted(pd.Timestamp(dt))
        if pos <= 0 or pos >= len(idx) or idx[pos] != pd.Timestamp(dt):
            continue
        pc_ = float(df["c"].iloc[pos - 1]); o_ = float(df["o"].iloc[pos])
        c_ = float(df["c"].iloc[pos]); atr_ = float(df["atr"].iloc[pos - 1])
        if not (pc_ > 0 and o_ > 0 and c_ > 0 and atr_ == atr_ and atr_ > 0):
            continue
        if a.gap_guard > 0 and o_ < pc_ * (1.0 - a.gap_guard):
            continue
        day5 = _i5[sm].get(dt)
        if day5 is None or len(day5) == 0 or not _ig_ok(day5, c_):
            continue
        xp, why, _e5, _x5t = _x5(day5, float("inf"), o_ + atr_ * a.sm,
                                 o_ - atr_ * a.tm, False,
                                 day_low=float(df["l"].iloc[pos]),
                                 day_high=float(df["h"].iloc[pos]),
                                 day_close=c_, stop_delay_bars=a.stop_delay_bars)
        if xp is None or why in ("no_5m", "no_entry"):
            continue
        _cv.append((o_ - float(xp)) * a.qty)
        _cd.append(pd.Timestamp(dt))
        if not (a.gap_guard > 0 and o_ > pc_ * (1.0 + a.gap_guard)):
            xl, wl, _, _ = _l5(day5, -float("inf"), o_ - atr_ * a.sm,
                               o_ + atr_ * a.tm, day_high=float(df["h"].iloc[pos]),
                               day_close=c_, stop_delay_bars=a.stop_delay_bars)
            if xl is not None and wl not in ("no_5m", "no_entry"):
                _clv.append((float(xl) - o_) * a.qty)
    if _cv:
        cs = pd.Series(_cv)
        cdm = pd.DataFrame({"d": _cd, "v": _cv}).groupby("d")["v"].mean()
        ct = (cdm.mean() / (cdm.std(ddof=1) / (len(cdm) ** 0.5))) if len(cdm) > 1 else 0.0
        _ev = r["E_翌寄り+OCO"].dropna()
        print(f"\n■ 対照実験 — シグナルが出ていない日に同じ OCO で寄り成行ショート")
        print(f"  {'区分':<26}{'件数':>7}{'勝率':>7}{'円/件':>9}{'日クラスタt':>11}")
        print(f"  {'E(シグナルあり)':<26}{len(_ev):>7,}"
              f"{(_ev > 0).mean()*100:>6.1f}%{_ev.mean():>+9,.0f}"
              f"{_res.get('E_翌寄り+OCO', (0,0,0,0))[3]:>+11.2f}")
        print(f"  {'対照(シグナルなしの日)':<26}{len(cs):>7,}"
              f"{(cs > 0).mean()*100:>6.1f}%{cs.mean():>+9,.0f}{ct:>+11.2f}")
        print(f"  差 (E − 対照)                              "
              f"{_ev.mean() - cs.mean():>+9,.0f}円/件")
        if _clv:
            _gvv = r["G_翌寄りロング+OCO"].dropna()
            cl = pd.Series(_clv)
            print(f"  {'G ロング(シグナルあり)':<26}{len(_gvv):>7,}"
                  f"{(_gvv > 0).mean()*100:>6.1f}%{_gvv.mean():>+9,.0f}")
            print(f"  {'対照ロング(シグナルなし)':<26}{len(cl):>7,}"
                  f"{(cl > 0).mean()*100:>6.1f}%{cl.mean():>+9,.0f}")
        if abs(cs.mean()) > abs(_ev.mean()) * 0.5:
            print(f"  ⛔ **対照でも同じだけ出ている。E の数字はシグナルのエッジではなく、")
            print(f"     0.1/1.0 という OCO の払い出し形状(と測定の非対称)の産物。**")
        else:
            print(f"  → 対照はほぼゼロ。E の数字は OCO の形状では説明できない。")

# ── 診断① 5分足の先頭バー時刻 ────────────────────────────────
if _bar0:
    _tb = sum(_bar0.values())
    print(f"\n■ 5分足の先頭バー時刻 (E は寄り約定なので、ここが 09:00 でないと"
          f"寄り直後の値動きが測れていない)")
    for k, v in sorted(_bar0.items(), key=lambda x: -x[1])[:5]:
        print(f"    {k}  {v:,}件 ({v / _tb * 100:.1f}%)")
    if not a.no_oco and "bar0" in r.columns:
        print(f"\n  E を先頭バー時刻で分解 (09:00 以外は寄り直後が未測定)")
        print(f"    {'先頭バー':<10}{'件数':>7}{'勝率':>7}{'円/件':>9}")
        for _lab, _sel in (("09:00", r["bar0"] == "09:00"),
                           ("09:00 以外", (r["bar0"] != "09:00") & (r["bar0"] != ""))):
            v = r.loc[_sel, "E_翌寄り+OCO"].dropna()
            if v.empty:
                continue
            print(f"    {_lab:<10}{len(v):>7,}{(v > 0).mean()*100:>6.1f}%"
                  f"{v.mean():>+9,.0f}")
        print(f"    → 09:00 以外が明らかに良いなら、寄り直後の逆行を取りこぼしている。"
              f"--require-open-bar で 09:00 のみに絞って再測定すること。")

# ── 診断② lss が約定した/しなかった で E を割る ────────────────
if "lss約定" in r.columns and (r["lss約定"] >= 0).any() and not a.no_oco:
    print(f"\n■ E を『lss がトリガーに届いたか』で分解")
    print(f"  {'区分':<22}{'件数':>7}{'勝率':>7}{'合計':>14}{'円/件':>9}"
          f"{'日クラスタt':>11}")
    for _lab, _sel in (("lss も約定した", r["lss約定"] == 1),
                       ("lss は不約定だった", r["lss約定"] == 0)):
        g = r[_sel]
        v = g["E_翌寄り+OCO"].dropna()
        if v.empty:
            continue
        dm = g.loc[v.index].assign(_v=v).groupby("date")["_v"].mean()
        t = (dm.mean() / (dm.std(ddof=1) / (len(dm) ** 0.5))) if len(dm) > 1 else 0.0
        print(f"  {_lab:<22}{len(v):>7,}{(v > 0).mean()*100:>6.1f}%"
              f"{v.sum():>+14,.0f}{v.mean():>+9,.0f}{t:>+11.2f}")
    print(f"  → 『lss は不約定だった』側にも同じだけ乗っているなら、E のエッジは"
          f"トリガーとは無関係。")
    print(f"     『lss も約定した』側に偏っているなら、E は現行の焼き直しに近い。")

if a.out_raw.strip():
    _o = r[r["E_翌寄り+OCO"].notna()].copy()
    _o = _o.assign(fold=1, train_months="", oos_month=_o["month"],
                   entry_date=_o["date"].dt.strftime("%Y-%m-%d"),
                   name="", strategy="E", bt_score=99.0,
                   entry_p=_o["entry_p"].round(1),
                   pnl=_o["E_翌寄り+OCO"].round(0), filled=1)
    _liqmap = (d.drop_duplicates(subset=["symbol"]).set_index("symbol")["liquidity"]
               if "liquidity" in d.columns else None)
    _o["liquidity"] = (_o["symbol"].map(_liqmap).fillna(0)
                       if _liqmap is not None else 0)
    _o[["fold", "train_months", "oos_month", "entry_date", "symbol", "name",
        "strategy", "bt_score", "entry_p", "pnl", "filled", "liquidity"]].to_csv(
        a.out_raw, index=False, encoding="utf-8-sig")
    print(f"\n[出力] {a.out_raw} ({len(_o):,}行)")
    print(f"  → python sim_oos_budget.py --raw {a.out_raw} --bt-mins 0 --budget 400")
    print(f"     で**予算制約下**の現行との比較ができる(18.10: 全部買えるなら得 と")
    print(f"     予算内でどれを買うか は別問題)。")

print(f"\n{'─'*78}")
print("■ 読み方")
print(f"{'─'*78}")
print("  ・A(夜だけ) が §18.19 の overnight と同じ向き(ゼロ近傍)なら、")
print("    lss のシグナルで条件付けても夜に取れるものは無い、が確認できる。")
print("  ・**判断は E(翌寄り+OCO) で行う**。D は終値を見てから終値で約定する")
print("    先読みを含むので実装できない(シグナルは D の終値で決まる)。")
print("    D は『上限値』として、E との差 = 先読みの寄与を読むために置いてある。")
print("  ・E が現行 lss に届かないなら、**トリガー(下ブレイク待ち)がエッジ**という")
print("    §18.19/18.20 の結論どおり。寄りで入ると条件を捨てることになる。")
print("  ・D の決済理由も見ること。stop の比率が現行より大幅に高いなら、")
print("    それは夜間ギャップで損切りラインを飛び越えられている(現行には無いリスク)。")
print("  ・日クラスタ t で見ること。同日決済でない B/C も、日ごとに相関する。")
print("  ・G(ロング)は E の鏡像。無条件のドリフトはゼロ(§18.19)なので、E がプラスで")
print("    G もプラスなら **OCO の払い出しが両方向で有利に出ている** = どこかに")
print("    測定の非対称がある。片方だけプラスが正常な形。")
print("  ⛔ **配当落ち**: 日本株は3月期末・9月中間に権利落ちが集中する。空売りは")
print("     配当落調整金を払うので、夜またぎ(A/B/D)がその分だけ現実には存在しない")
print("     利益を計上している。--exclude-months 2026-03 等で影響を測ること。")
print("     E は寄り後に入るので配当落ちの影響を受けない。")
print("  ⚠ 持ち越しには測定に出ないコストがある:")
print("     逆日歩 / 一般信用デイトレ(MarginTradeType 3)が使えない /")
print("     夜間のギャップで損切りが効かない / 証拠金が2日拘束され資本回転が半減。")
print("     現行の同日決済は予算を毎日リサイクルできるので、同じ 円/件 でも")
print("     **持ち越しは実質半分の成績**として比べること。")
