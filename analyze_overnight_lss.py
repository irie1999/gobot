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


class _Tee:
    """コンソール出力をそのまま保持して HTML にも載せるための tee。
    ターミナルで見た内容と HTML の内容が食い違わないようにするのが目的。"""

    def __init__(self, base):
        self._b, self.buf = base, []

    def write(self, x):
        self._b.write(x)
        self.buf.append(x)
        return len(x)

    def flush(self):
        self._b.flush()

    def isatty(self):
        return getattr(self._b, "isatty", lambda: False)()


_TEE = _Tee(sys.stdout)
sys.stdout = _TEE

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
ap.add_argument("--out-variant", default="E", choices=["E", "H", "D", "F"],
                help="--out-raw で書き出す方式(既定 E)。H=mirror(前日終値で指値売り)")
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
ap.add_argument("--inject-html", default="",
                help="**既存のレポートHTMLにタブとして差し込む**。例: "
                     "--inject-html signals_holdout_all_both_2026-08-07.html。"
                     "レポート生成側は触らないので .\\dailyfast は重くならない。"
                     "元ファイルは .bak に退避。再実行すると差し替え")
ap.add_argument("--html", default="",
                help="E/H の成績をHTMLで出力するパス(例 eh_report.html)。"
                     "サマリー/月別/日別/取引明細のタブ付き")
ap.add_argument("--no-oco", action="store_true",
                help="D(引け+OCO)を計算しない。5分足が無い環境向け")
a = ap.parse_args()

files = sorted(_glob.glob(a.raw)) if any(c in a.raw for c in "*?[") else [a.raw]
if not files:
    sys.exit(f"[error] {a.raw} に一致するファイルがありません")
d = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in files], ignore_index=True)
# ── lss_trades.csv(レポート自身の取引ログ)も読めるようにする ──────────
#   oos_raw_fold*.csv は run_oos_folds.py が作るローリングOOSの生データで、
#   当月は含まれない(月が終わっていないため)。レポートの損益タブと同じ母集団・
#   同じ窓(当月込み)で見たいときは lss_trades.csv を渡す。
#   列が違うので正規化する:
#     filled     : reason が「約定せず」以外なら 1
#     oos_month  : entry_date の YYYY-MM
#     bt_score   : bt 列
#   ⚠ どちらを渡したかで母集団が変わる。lss_trades.csv はレポートの表示窓
#     (既定180日)ぶんしか無いので、10ヶ月の検定には oos_raw を使うこと。
_SRC = "oos_raw"
if "filled" not in d.columns and "reason" in d.columns:
    _SRC = "lss_trades"
    d["filled"] = (d["reason"].astype(str) != "約定せず").astype(int)
    if "oos_month" not in d.columns:
        d["oos_month"] = d["entry_date"].astype(str).str[:7]
    if "bt_score" not in d.columns and "bt" in d.columns:
        d["bt_score"] = pd.to_numeric(d["bt"], errors="coerce").fillna(0)
    print(f"[入力] lss_trades.csv 形式として読み込み "
          f"(約定 {int(d['filled'].sum()):,} / 不約定 {int((d['filled']==0).sum()):,})")
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
        "E0_9時から建値は現行": None,
        "H_前日終値で指値売り": None,
        "D2_夜間損切りが効く場合": None,
        "E建値": None, "E決済": None, "E理由": "", "E時刻": "",
        "H建値": None, "H決済": None, "H理由": "", "H時刻": "",
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
                if _tag.startswith("E_"):
                    rec["E建値"], rec["E決済"], rec["E理由"] = _ep, float(xp), why
                    try:
                        rec["E時刻"] = pd.Timestamp(_x5t).strftime("%H:%M")
                    except Exception:
                        pass
            # D2: **夜間の損切りが完璧に効いた場合の上限値**(PTS等で夜に返済できる想定)。
            #   D は寄りが損切りを超えて始まると max(stop, 始値) の不利約定になる。
            #   夜間に損切りできれば stop ちょうどで止められる、という最良ケース。
            #   これでも H に届かないなら、PTS を調べる価値は無い。
            #   ⚠ 実際には PTS の板が薄いので stop ちょうどで返済できる保証は無い。
            #      あくまで**上限**であって実現値ではない。
            _stop_d = pc + atr * a.sm
            if o1 >= _stop_d:
                rec["D2_夜間損切りが効く場合"] = (pc - _stop_d) * q   # = -sm×ATR×株数
                _reasons[("2", "stop夜間")] += 1
            # (o1 < stop なら D と同じ扱い。下の D の計算結果を後で流用する)
            # H(mirror): **前日終値で指値空売り**。上昇して前日終値に到達したら約定。
            #   寄りが既に前日終値以上なら板寄せで約定(=始値。前日終値より高い=有利)。
            #   到達しなければ**建てない**(=強い下げの日を取り逃がす。ここが弱点)。
            #   分解の示唆(『待つのは正しい』+『高く売るのが効く』)を両立させる形。
            #   ガードは mirror 側の鏡像(+3%超のギャップアップはスキップ / §18.8)。
            if not (a.gap_guard > 0 and o1 > pc * (1.0 + a.gap_guard)):
                _hp = o1 if o1 >= pc else pc      # 寄りが上なら板寄せ約定=始値
                xph, whyh, _, _xht = _x5(
                    day5, _hp, _hp + atr * a.sm, _hp - atr * a.tm, True,
                    day_low=_dl, day_high=_dh, day_close=c1,
                    stop_delay_bars=a.stop_delay_bars)
                if xph is not None and whyh not in ("no_5m", "no_entry"):
                    rec["H_前日終値で指値売り"] = (_hp - float(xph)) * q
                    rec["H建値"], rec["H決済"], rec["H理由"] = _hp, float(xph), whyh
                    try:
                        rec["H時刻"] = pd.Timestamp(_xht).strftime("%H:%M")
                    except Exception:
                        pass
                    _reasons[("H", whyh)] += 1
            # E0: **09:00から持つ**が、建値もバリアも**現行の約定値**から取る。
            #  現行 → E0  = 露出時刻だけの差(建値は同じ)
            #  E0   → E   = 建値だけの差(露出は同じ)
            #  この2段なら加算できる。建値とバリアが一緒に動く効果を混ぜない。
            _le = rec.get("lss建値") or 0.0
            if _le > 0:
                xp0, why0, _, _ = _x5(
                    day5, _INF, _le + atr * a.sm, _le - atr * a.tm, False,
                    day_low=_dl, day_high=_dh, day_close=c1,
                    stop_delay_bars=a.stop_delay_bars)
                if xp0 is not None and why0 not in ("no_5m", "no_entry"):
                    rec["E0_9時から建値は現行"] = (_le - float(xp0)) * q
    if rec.get("D2_夜間損切りが効く場合") is None:
        rec["D2_夜間損切りが効く場合"] = rec.get("D_引け+OCO")
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
    _cols += ["D_引け+OCO", "D2_夜間損切りが効く場合", "E_翌寄り+OCO",
              "F_5分足寄り+OCO", "G_翌寄りロング+OCO", "H_前日終値で指値売り"]
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
             "  ← D+夜間損切り(PTS想定の**上限**)" if col.startswith("D2") else
             "  ← 寄り成行(日足の始値)" if col.startswith("E_") else
             "  ← 寄り成行(5分足の始値)" if col.startswith("F_") else
             "  ← **ロング**(E の鏡像)" if col.startswith("G_") else
             "  ← mirror(待って高く売る)" if col.startswith("H_") else "")
    print(f"  {col:<18}{len(v):>7,}{(v > 0).mean()*100:>6.1f}%{v.sum():>+14,.0f}"
          f"{v.mean():>+9,.0f}{bp:>+8.1f}{t:>+11.2f}{_mark}")
for _tag in ("D", "E", "F", "G", "H"):
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
        print(f"     ⚠ この 円/件 は**そのぶん儲かる額ではない**。建値が変わると")
        print(f"        損切り・利確も同じだけ動くので、利確で終わればどちらも +1.0ATR")
        print(f"        ちょうどで差は消える。建値の差は『どの決済が発火するか』を")
        print(f"        変える形で効く。加算できないので下の2段で分ける。")
        print(f"  ② 2段の分解 (加算できる形)")
        _e0 = g["E0_9時から建値は現行"].dropna()
        if len(_e0):
            g0 = g.loc[_e0.index]
            _cm0 = g0.apply(lambda x: float(_cur_g.get((x["symbol"], x["date"]), 0))
                            if _cur_g is not None else 0.0, axis=1)
            _eg0 = g0["E_翌寄り+OCO"].mean()
            print(f"     現行            {_cm0.mean():>+9,.0f}円/件")
            print(f"     ↓ 露出時刻だけ変える(9:00から持つ。建値・バリアは現行のまま)")
            print(f"     E0              {_e0.mean():>+9,.0f}円/件"
                  f"   差 {_e0.mean() - _cm0.mean():>+9,.0f}")
            print(f"     ↓ 建値だけ変える(始値で入る。露出は同じ)")
            print(f"     E               {_eg0:>+9,.0f}円/件"
                  f"   差 {_eg0 - _e0.mean():>+9,.0f}")
            print(f"     合計                        "
                  f"{_eg0 - _cm0.mean():>+9,.0f}円/件")
            _d1, _d2 = _e0.mean() - _cm0.mean(), _eg0 - _e0.mean()
            print(f"  ③ どちらが効いているか")
            if _d1 > 0 and _d2 > 0:
                print(f"     両方プラス。露出 {_d1:+,.0f} / 建値 {_d2:+,.0f}")
            elif _d1 <= 0 < _d2:
                print(f"     **建値だけが効いている**({_d2:+,.0f})。9:00から持つこと自体は")
                print(f"     {_d1:+,.0f} で**不利**。朝の戻りで刈られるぶん、現行が待つのは")
                print(f"     理にかなっている。E の優位はまるごと『高く売れる』ことから来る。")
            elif _d2 <= 0 < _d1:
                print(f"     **露出時刻だけが効いている**({_d1:+,.0f})。建値の差は"
                      f"{_d2:+,.0f}。")
            else:
                print(f"     両方マイナス。E が勝つ理由が説明できない = 要精査。")

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

# ── H(mirror) の約定率 ─────────────────────────────────────────
if not a.no_oco and "H_前日終値で指値売り" in r.columns:
    _hv = r["H_前日終値で指値売り"]
    _hn, _tot = int(_hv.notna().sum()), len(r)
    print(f"\n■ H(mirror = 前日終値で指値空売り) の約定率")
    print(f"  約定 {_hn:,}/{_tot:,} ({_hn / max(_tot, 1) * 100:.1f}%)"
          f"   ※ E は寄りで必ず建つので実質100%")
    _miss = r[_hv.isna() & r["E_翌寄り+OCO"].notna()]
    if len(_miss):
        _mv = _miss["E_翌寄り+OCO"]
        print(f"  **H が取り逃がした日**  {len(_mv):,}件   "
              f"E ならこの日は {_mv.mean():+,.0f}円/件 稼げていた")
        print(f"    (合計 {_mv.sum():+,.0f}円)")
        print(f"    → 前日終値まで戻らない日 = そのまま下げ続けた日。")
        print(f"       高く売れるのと引き換えに、**一番おいしい日を落としている**。")
    _both = r[_hv.notna() & r["E_翌寄り+OCO"].notna()]
    if len(_both):
        print(f"  両方が建てた日 {len(_both):,}件   "
              f"H {_both['H_前日終値で指値売り'].mean():+,.0f} vs "
              f"E {_both['E_翌寄り+OCO'].mean():+,.0f} 円/件")
        print(f"    → ここが『高く売れる』ぶんの純粋な差。")

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
    _vcol = {"E": "E_翌寄り+OCO", "H": "H_前日終値で指値売り",
             "D": "D_引け+OCO", "F": "F_5分足寄り+OCO"}[a.out_variant]
    # ⛔ **約定した行だけ書いてはいけない。** sim_oos_budget の『通常予算』は
    #    不約定の注文も枠を消費するモデル(fill_budget=False)。約定分しか無いと
    #    「どれが約定するかを事前に知っていた」ことになり、空いた枠で他の銘柄を
    #    詰められてしまう = H(約定率73.8%)に大きく有利な先読みになる。
    #    5分足とATRが揃った行はすべて出し、未約定は filled=0 / pnl=0 とする。
    #    entry_p は**注文価格**(H=前日終値 / E,F=始値 / D=前日終値)。枠の消費は
    #    注文価格×100 で計算されるので、ここを実約定値ではなく注文値にする。
    _o = r[r["_atr"] > 0].copy()
    _ordp = (_o["entry_p"] if a.out_variant in ("H", "D") else _o["_o1"])
    _o = _o[_ordp > 0].copy()
    _ordp = _ordp[_ordp > 0]
    _o["_filled"] = _o[_vcol].notna().astype(int)
    _o["_pnl"] = _o[_vcol].fillna(0.0)
    _o["_ordp"] = _ordp
    print(f"  [out-raw] 注文 {len(_o):,}件 / 約定 {int(_o['_filled'].sum()):,}件 "
          f"({_o['_filled'].mean() * 100:.1f}%)  ※不約定も枠を消費する形で書き出す")
    _o = _o.assign(fold=1, train_months="", oos_month=_o["month"],
                   entry_date=_o["date"].dt.strftime("%Y-%m-%d"),
                   name="", strategy=a.out_variant, bt_score=99.0,
                   entry_p=_o["_ordp"].round(1),
                   pnl=_o["_pnl"].round(0), filled=_o["_filled"])
    _liqmap = (d.drop_duplicates(subset=["symbol"]).set_index("symbol")["liquidity"]
               if "liquidity" in d.columns else None)
    _o["liquidity"] = (_o["symbol"].map(_liqmap).fillna(0)
                       if _liqmap is not None else 0)
    _o[["fold", "train_months", "oos_month", "entry_date", "symbol", "name",
        "strategy", "bt_score", "entry_p", "pnl", "filled", "liquidity"]].to_csv(
        a.out_raw, index=False, encoding="utf-8-sig")
    print(f"\n[出力] {a.out_raw} ({len(_o):,}行 / 方式 {a.out_variant})")
    print(f"  → python sim_oos_budget.py --raw {a.out_raw} --bt-mins 0 --budget 400")
    print(f"     で**予算制約下**の現行との比較ができる(18.10: 全部買えるなら得 と")
    print(f"     予算内でどれを買うか は別問題)。")

print(f"\n{'─'*78}")
print("■ 読み方")
print(f"{'─'*78}")
print("  ・A(夜だけ) が §18.19 の overnight と同じ向き(ゼロ近傍)なら、")
print("    lss のシグナルで条件付けても夜に取れるものは無い、が確認できる。")
print("  ・**判断は E(翌寄り) / H(前日終値の指値) で行う**。D は終値を見てから終値で約定する")
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
print("  ⚠ 持ち越し(D/D2)には測定に出ないコストがある:")
print("     貸株料が日数ぶん / 一般信用デイトレ(MarginTradeType 3)が使えない /")
print("     逆日歩 / 夜間の相場全体のギャップリスクを100%被る /")
print("     権利落ち日をまたぐと配当落調整金(上の配当落ち参照)。")
print("     ※ **資金回転は半減しない**(2026-08-10 訂正)。引けで買い戻して同じ引けで")
print("        新規を建てれば毎日1回転し、E/H と同じ。回転数ではなくコストとリスクの差。")
print("  ・D2 は『寄りが損切りを超えて始まったら stop ちょうどで止められた』という")
print("    **達成不可能な上限**。ギャップの主因はニュースで、PTS の気配も同時に飛ぶので")
print("    夜間に逆指値を置いても飛んだ後の値段で約定する = 実際は D に近い。")
print("    D2 と D の差(=夜間ギャップのコスト)の大きさを見るためだけの数字。")

# ── E/H 成績セクションの HTML ────────────────────────────────────
# --html        : 単体HTMLとして書き出す
# --inject-html : **既存のレポート(signals_holdout_all_both_*.html)にタブとして差し込む**
#   レポート生成側(run_signals_holdout_all.py / nikkei_analysis.py)は一切触らない。
#   .\dailyfast を重くしないため、また毎朝使う発注リストを壊さないため。
#   差し込み先の構造(run_signals_holdout_all.py:3212-3217, :3135):
#     ナビ  <div class="ho-outer-nav"> ... <button class="ho-outer-btn"
#            onclick="switchHoTab('xxx')">…</button> ... </div>
#     ペイン <div id="ho-xxx" class="ho-outer-pane">…</div>
#     切替  switchHoTab(tab) が .ho-outer-pane の .active を付け替える
#   CSS/JS の衝突を避けるため、内側のタブは eh- 接頭辞で名前空間を分ける。
if a.html.strip() or a.inject_html.strip():
    import html as _hesc
    from pathlib import Path as _P

    # (銘柄, 日) -> 現行の損益。dict にするのは、重複キーがあると Series が返って
    # float() が落ちるため(_cur は dedup 済みだが、入力次第で崩れうる)。
    _cg = {}
    if not _cur.empty:
        for _s2, _d2, _p2 in zip(_cur["symbol"], _cur["entry_date"],
                                 pd.to_numeric(_cur["pnl"], errors="coerce")):
            _cg.setdefault((str(_s2), pd.Timestamp(_d2)), float(_p2))
    r["現行"] = [_cg.get((str(sm), pd.Timestamp(dt)), float("nan"))
                 for sm, dt in zip(r["symbol"], r["date"])]
    r["日"] = r["date"].dt.strftime("%Y-%m-%d")
    _V = [("現行", "現行"), ("E_翌寄り+OCO", "E"), ("H_前日終値で指値売り", "H")]

    def _agg(g):
        o = {}
        for col, lab in _V:
            v = g[col].dropna() if col in g.columns else g.get(col, pd.Series(dtype=float))
            o[lab] = (len(v), (v > 0).mean() * 100 if len(v) else 0.0,
                      v.sum(), v.mean() if len(v) else 0.0)
        return o

    def _cells(o):
        t = ""
        for _, lab in _V:
            n, wr, tot, per = o[lab]
            c = "ehp" if tot > 0 else ("ehn" if tot < 0 else "")
            t += (f'<td>{n:,}</td><td>{wr:.0f}%</td>'
                  f'<td class="{c}">{tot:+,.0f}</td><td class="{c}">{per:+,.0f}</td>')
        return t

    _all = _agg(r)
    _hd = "".join(f'<th colspan="4">{lab}</th>' for _, lab in _V)
    _sub = "".join('<th>件数</th><th>勝率</th><th>損益</th><th>円/件</th>' for _ in _V)
    _mrows = "".join(f'<tr><td class="ehk">{m}</td>{_cells(_agg(g))}</tr>'
                     for m, g in r.groupby("month"))
    _drows = "".join(f'<tr><td class="ehk">{dt}</td>{_cells(_agg(g))}</tr>'
                     for dt, g in r.groupby("日"))

    def _f(x, n=0):
        return "—" if x is None or x != x else f"{x:,.{n}f}"

    def _pn(v):
        if v is None or v != v:
            return '<td class="ehmut">—</td>'
        return f'<td class="{"ehp" if v > 0 else "ehn"}">{v:+,.0f}</td>'

    _trows = []
    for _, t in r.sort_values(["日", "symbol"]).iterrows():
        _trows.append(
            f'<tr><td class="ehk">{t["日"]}</td>'
            f'<td class="ehk">{_hesc.escape(str(t["symbol"]))}</td>'
            f'<td>{_f(t["entry_p"], 1)}</td>{_pn(t["現行"])}'
            f'<td>{_f(t["E建値"], 1)}</td><td>{_f(t["E決済"], 1)}</td>'
            f'<td class="ehmut">{t["E理由"] or "—"}</td>'
            f'<td class="ehmut">{t["E時刻"] or "—"}</td>{_pn(t["E_翌寄り+OCO"])}'
            f'<td>{_f(t["H建値"], 1)}</td><td>{_f(t["H決済"], 1)}</td>'
            f'<td class="ehmut">{t["H理由"] or "—"}</td>'
            f'<td class="ehmut">{t["H時刻"] or "—"}</td>'
            f'{_pn(t["H_前日終値で指値売り"])}</tr>')

    # ── レポートの他タブと同じ体裁で組む ────────────────────────────
    #   月テーブル(nikkei_analysis.py:10482 と同じ列・同じ色)＋
    #   月ブロック(.mg-block / .mg-header / toggleMG)＋日チップ(.edate-btn)。
    #   クラスはレポート側の CSS をそのまま使うので追加スタイルは最小限。
    # 当月は営業日が揃っていないので月テーブルで「未完了」と明示する
    # (件数・損益をそのまま他の月と並べると誤読される)
    from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
    _CUR_YM = _dt2.now(_tz2(_td2(hours=9))).strftime("%Y-%m")

    def _mtable(col):
        """方式1つぶんの月テーブル。列は既存の予算タブに合わせる。"""
        rows = ""
        for ym in sorted(r["month"].unique(), reverse=True):
            g = r[r["month"] == ym]
            v = g[col].dropna()
            if not len(v):
                continue
            gp = v[v > 0].sum()
            gl = abs(v[v <= 0].sum())
            pn = v.sum()
            wr = (v > 0).mean() * 100
            col_ = "#4ade80" if pn >= 0 else "#f87171"
            bw = min(abs(pn) / 300000 * 100, 100)
            bc = ("rgba(74,222,128,0.25)" if pn >= 0 else "rgba(248,113,113,0.25)")
            # 必要資金 = その月で最大の「同日の建玉合計」(同日決済なので当日分の合計)
            _cap = 0.0
            _sub = g.loc[v.index]
            for _d, _gd in _sub.groupby("日"):
                _cap = max(_cap, float((_gd["entry_p"] * a.qty).sum()))
            rows += (
                f'<tr><td style="font-weight:700;color:#e2e8f0;white-space:nowrap">'
                f'{ym[:4]}/{ym[5:7]}月'
                + ('<br><span style="font-size:0.64rem;color:#fbbf24;'
                   'font-weight:700">未完了</span>' if ym == _CUR_YM else "")
                + '</td>'
                f'<td style="text-align:right;color:#94a3b8">{len(v)}件</td>'
                f'<td style="text-align:right;color:#94a3b8">{wr:.0f}%</td>'
                f'<td style="text-align:right;color:#4ade80">+{gp:,.0f}円</td>'
                f'<td style="text-align:right;color:#f87171">-{gl:,.0f}円</td>'
                f'<td style="width:160px;position:relative;padding:4px 8px">'
                f'<div style="position:absolute;top:4px;bottom:4px;'
                f'left:{"50%" if pn >= 0 else f"calc(50% - {bw / 2:.1f}%)"};'
                f'width:{bw / 2:.1f}%;background:{bc};border-radius:2px"></div>'
                f'<span style="position:relative;font-weight:700;color:{col_}">'
                f'{pn:+,.0f}円</span></td>'
                f'<td style="text-align:right;font-weight:700;color:{col_}">'
                f'{v.mean():+,.0f}円</td>'
                f'<td style="text-align:right;color:#38bdf8;font-weight:700;'
                f'white-space:nowrap">{_cap:,.0f}円</td></tr>')
        return (f'<div style="margin-bottom:14px"><table '
                f'style="border-collapse:collapse;width:auto"><thead><tr>'
                f'<th style="text-align:left;color:#94a3b8;font-size:0.78rem;'
                f'padding:3px 8px">月</th>'
                f'<th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px">件数</th>'
                f'<th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px">勝率</th>'
                f'<th style="color:#4ade80;font-size:0.78rem;padding:3px 8px">利益</th>'
                f'<th style="color:#f87171;font-size:0.78rem;padding:3px 8px">損失</th>'
                f'<th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px;'
                f'text-align:center">損益合計</th>'
                f'<th style="color:#94a3b8;font-size:0.78rem;padding:3px 8px;'
                f'text-align:right">円/件</th>'
                f'<th style="color:#38bdf8;font-size:0.78rem;padding:3px 8px;'
                f'text-align:right">必要資金<br><small style="color:#64748b">'
                f'同日建玉ピーク</small></th>'
                f'</tr></thead><tbody>{rows}</tbody></table></div>')

    def _mblocks(col, key):
        """月ごとの折りたたみ + 日チップ。レポートの .mg-* / .edate-* をそのまま使う。"""
        out = ""
        for _i, ym in enumerate(sorted(r["month"].unique(), reverse=True)):
            g = r[r["month"] == ym]
            v = g[col].dropna()
            if not len(v):
                continue
            _sub = g.loc[v.index]
            pn = v.sum()
            pc = "#4ade80" if pn >= 0 else "#f87171"
            op = _i < 1
            chips = ""
            for _d, _gd in sorted(_sub.groupby("日"), reverse=True):
                _p = _gd[col].sum()
                _w = (_gd[col] > 0).mean() * 100
                _c = "#4ade80" if _p >= 0 else "#f87171"
                _cap = float((_gd["entry_p"] * a.qty).sum())
                chips += (f'<button class="edate-btn" style="cursor:default">'
                          f'<span class="edate-mm">{_d[5:7]}/{_d[8:10]}</span>'
                          f'<span class="edate-stat">{len(_gd)}件 {_w:.0f}%</span>'
                          f'<span style="color:{_c};font-weight:700">{_p:+,.0f}</span>'
                          f'<span style="color:#38bdf8;font-size:0.6rem">'
                          f'要¥{_cap:,.0f}</span></button>')
            out += (f'<div class="mg-block"><div class="mg-header" '
                    f'onclick="toggleMG(this)">'
                    f'<span class="mg-arrow">{"▼" if op else "▶"}</span>'
                    f'<span class="mg-ym">{ym[:4]}/{ym[5:7]}月</span>'
                    f'<span class="mg-stats">{len(v)}件&nbsp;'
                    f'{(v > 0).mean() * 100:.0f}%&nbsp;'
                    f'<span style="color:{pc};font-weight:700">{pn:+,.0f}円</span>'
                    f'</span></div>'
                    f'<div class="mg-body" id="mgb_eh{key}_{ym}" '
                    f'style="display:{"block" if op else "none"}">'
                    f'<div class="edate-grid">{chips}</div></div></div>\n')
        return out

    _blocks = ""
    for _bi, (_col, _lab) in enumerate(_V):
        _n, _wr, _tot, _per = _all[_lab]
        _blocks += (
            f'<div class="ehblk{" on" if _lab == "H" else ""}">'
            f'<p style="color:#c4b5fd;font-size:0.8rem;margin:2px 0 8px">'
            f'<b>{_lab}</b> — {_n:,}件 / 勝率 {_wr:.1f}% / '
            f'<b style="color:{"#4ade80" if _tot >= 0 else "#f87171"}">{_tot:+,.0f}円</b>'
            f' / <b>{_per:+,.0f}円/件</b></p>'
            f'{_mtable(_col)}{_mblocks(_col, _bi)}</div>')

    _EH_CSS = """<style>
.ehsel{display:flex;gap:6px;margin:10px 0 12px;flex-wrap:wrap}
.ehsel button{padding:6px 18px;background:#1e293b;border:1px solid #334155;
 border-radius:6px;cursor:pointer;color:#94a3b8;font-size:.85rem}
.ehsel button.on{background:#0f172a;border-color:#a78bfa;color:#c4b5fd;font-weight:700}
.ehblk{display:none} .ehblk.on{display:block}
.ehsub{color:#94a3b8;font-size:.78rem;line-height:1.8;margin:4px 0 10px}
.ehfold{margin-top:14px;background:#0f172a;border:1px solid #1e293b;border-radius:6px;
 padding:8px 12px}
.ehfold>summary{cursor:pointer;color:#93c5fd;font-size:.84rem;font-weight:700}
.ehfold table{border-collapse:collapse;width:100%;font-size:.78rem;margin-top:8px;
 font-variant-numeric:tabular-nums}
.ehfold th,.ehfold td{padding:4px 8px;text-align:right;border-bottom:1px solid #16233c;
 white-space:nowrap}
.ehfold thead th{position:sticky;top:0;background:#0f1b30;color:#93c5fd;font-size:.72rem}
.ehfold .ehk{text-align:left;color:#cbd5e1}
.ehfold .ehp{color:#4ade80} .ehfold .ehn{color:#f87171} .ehmut{color:#64748b}
.ehbox{overflow:auto;max-height:70vh;margin-top:8px}
</style>"""

    # ── 月ごとの対応のある検定 (現行 vs E / 現行 vs H / E vs H) ──────
    #    同じ月・同じ銘柄集団なので paired が最も検出力が高い。
    #    ⚠ 予算制約なし。実運用の判定は compare_budget_raw.py が正。
    _mon = sorted(r["month"].unique())
    _MS = {lab: [float(r.loc[r["month"] == m, col].dropna().sum()) for m in _mon]
           for col, lab in _V}
    _prows = ""
    for _x, _y in (("現行", "E"), ("現行", "H"), ("E", "H")):
        dd = [b - a2 for a2, b in zip(_MS[_x], _MS[_y])]
        n = len(dd)
        mu = _st.mean(dd) if n else 0.0
        sd = _st.stdev(dd) if n > 1 else 0.0
        se = sd / (n ** 0.5) if n > 1 else 0.0
        tt = mu / se if se > 0 else 0.0
        lo, hi = mu - 1.96 * se, mu + 1.96 * se
        win = sum(1 for x in dd if x > 0)
        _v = ("<b class='ehp'>有意にプラス</b>" if lo > 0 else
              ("<b class='ehn'>有意にマイナス</b>" if hi < 0 else
               "<span class='ehmut'>差を検出できず</span>"))
        _prows += (f'<tr><td class="ehk">{_y} − {_x}</td>'
                   f'<td class="{"ehp" if mu > 0 else "ehn"}">{mu:+,.0f}</td>'
                   f'<td>{sd:,.0f}</td><td><b>{tt:+.2f}</b></td>'
                   f'<td>{lo:+,.0f} 〜 {hi:+,.0f}</td>'
                   f'<td>{win}/{n}</td><td class="ehk">{_v}</td></tr>')
    _console = _hesc.escape("".join(_TEE.buf))

    _EH_BODY = f"""{_EH_CSS}
<p style="color:#a78bfa;font-size:0.82rem;margin-bottom:8px">
🔁 <b>エントリー方式の比較</b>。シグナル・銘柄選定・発注順・決済(損切 {a.sm}ATR /
利確 {a.tm}ATR / 引け成行)は<b>3方式とも同一</b>で、違うのは<b>注文の出し方だけ</b>。
<b>現行</b>=逆指値売り(前日終値−1ティック。下がったら約定) /
<b>E</b>=寄成売り(9:00の板寄せで必ず約定) /
<b>H</b>=指値売り(前日終値。上がったら約定。届かなければ建てない)。
</p>
<div class="ehsub">
{r['日'].min()} 〜 {r['日'].max()} / {r['date'].nunique():,}営業日 / {len(r):,}銘柄日 /
{a.qty}株固定 / 摩擦なし(slip=0) / 出所 <b>{_SRC}</b>　
<span class="ehmut">※ このタブは<b>予算制約なし</b>(全シグナルを建てた場合)。
隣の「{"{}".format("400万円×流動性順×日別")}」は予算込みなので直接は比較できない。
予算込みの E/H 比較は compare_budget_raw.py</span>
</div>
<div class="ehsel">
<button onclick="ehSel(this,0)">現行</button>
<button onclick="ehSel(this,1)">E (寄成)</button>
<button class="on" onclick="ehSel(this,2)">H (前日終値の指値)</button>
</div>
{_blocks}
<details class="ehfold"><summary>📊 統計 — 月ごとに対応をとった検定</summary>
<p style="color:#94a3b8;font-size:.78rem;line-height:1.7">
同じ月・同じ銘柄集団なので、相場全体の上下は両方に同じだけ効いて差し引きで消える
= 独立比較より検出力が高い。<span class="ehmut">⚠ 予算制約なし。採否の判定は
compare_budget_raw.py(予算400万)が正</span></p>
<table><thead><tr><th class="ehk">比較</th><th>月あたりの差</th><th>σ</th><th>t</th>
<th>95%CI(月)</th><th>勝ち月</th><th class="ehk">判定</th></tr></thead>
<tbody>{_prows}</tbody></table></details>
<details class="ehfold"><summary>📋 取引明細 ({len(_trows):,}件)</summary>
<div class="ehbox"><table><thead>
<tr><th class="ehk" rowspan="2">日付</th><th class="ehk" rowspan="2">銘柄</th>
<th rowspan="2">前日終値</th><th rowspan="2">現行 損益</th>
<th colspan="5">E (寄成)</th><th colspan="5">H (前日終値の指値)</th></tr>
<tr><th>建値</th><th>決済</th><th>理由</th><th>時刻</th><th>損益</th>
<th>建値</th><th>決済</th><th>理由</th><th>時刻</th><th>損益</th></tr>
</thead><tbody>{"".join(_trows)}</tbody></table></div></details>
<details class="ehfold"><summary>🖥 実行ログ</summary>
<div class="ehbox"><pre style="margin:0;color:#cbd5e1;font-size:.74rem;
line-height:1.5;white-space:pre">{_console}</pre></div></details>
<script>
function ehSel(el, i){{
  var p = el.parentNode.parentNode;
  p.querySelectorAll('.ehsel button').forEach(function(b,j){{b.classList.toggle('on', j===i);}});
  p.querySelectorAll('.ehblk').forEach(function(b,j){{b.classList.toggle('on', j===i);}});
}}
</script>"""

    if a.html.strip():
        _P(a.html).write_text(
            '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<title>E/H 成績</title><style>:root{color-scheme:dark}'
            'body{background:#0b1220;color:#e2e8f0;font-family:"Segoe UI",Meiryo,'
            'sans-serif;margin:0;padding:18px 22px}'
            # 単体HTMLにはレポート側のCSSが無いので、使っているクラスだけ最小限で補う
            '.mg-block{margin:8px 0;border:1px solid #1e293b;border-radius:6px}'
            '.mg-header{padding:7px 12px;cursor:pointer;background:#111c33;'
            'display:flex;gap:12px;align-items:center;border-radius:6px}'
            '.mg-arrow{color:#60a5fa}.mg-ym{font-weight:700}'
            '.mg-stats{color:#94a3b8;font-size:.82rem}'
            '.mg-body{padding:8px 12px}'
            '.edate-grid{display:flex;gap:6px;flex-wrap:wrap}'
            '.edate-btn{display:flex;flex-direction:column;gap:1px;padding:5px 9px;'
            'background:#0f172a;border:1px solid #334155;border-radius:5px;'
            'color:#cbd5e1;font-size:.72rem;text-align:center}'
            '.edate-mm{font-weight:700}.edate-stat{color:#94a3b8;font-size:.66rem}'
            'table{font-variant-numeric:tabular-nums}'
            '</style>'
            '<script>function toggleMG(h){var b=h.nextElementSibling;'
            'if(!b)return;var o=b.style.display!=="none";b.style.display=o?"none":"block";'
            'var a=h.querySelector(".mg-arrow");if(a)a.textContent=o?"\u25B6":"\u25BC";}'
            '</script></head><body>'
            '<h2 style="margin:0 0 6px">E / H 成績 — lss のエントリー方式の比較</h2>'
            + _EH_BODY + "</body></html>", encoding="utf-8")
        print(f"\n[HTML] {_P(a.html).resolve()}  ({len(_trows):,}行)")

    if a.inject_html.strip():
        import re as _re
        _tgt = _P(a.inject_html)
        if not _tgt.exists():
            print(f"[error] {_tgt} が見つかりません")
        else:
            _orig = _tgt.read_text(encoding="utf-8")
            _doc = _orig
            # ── 既存の差し込みを除去(冪等) ─────────────────────────
            #    ⚠ div の対応で削ると中身の "\n</div>\n" で非貪欲マッチが途中で
            #      止まり <script> と閉じ div が取り残される。マーカーで挟む。
            for _s0, _e0 in (("<!--EH-BTN-START-->", "<!--EH-BTN-END-->"),
                             ("<!--EH-PANE-START-->", "<!--EH-PANE-END-->"),
                             ("<!--EH-TAB-START-->", "<!--EH-TAB-END-->")):
                while _s0 in _doc:
                    _a0, _b0 = _doc.find(_s0), _doc.find(_e0)
                    if _a0 < 0 or _b0 < _a0:
                        break
                    _doc = _doc[:_a0] + _doc[_b0 + len(_e0):]
            _doc = _re.sub(
                r'\n?\s*<button class="ho-outer-btn" onclick="switchHoTab.eh.>'
                r'.*?</button>', "", _doc, flags=_re.S)

            # ── ① 損益タブの中(転換の隣)に入れる ────────────────────
            #    ボタン: <button class="detail-tab-btn"
            #             onclick="switchDetailTab(<seq>,'tenkan')">…</button>
            #    ペイン: <div id="detail_<seq>_tenkan" class="detail-tab-pane">
            #    switchDetailTab は id の接頭辞で走査するので登録は不要
            #    (nikkei_analysis.py:13535)。ペインは同じ親に居ればよいので、
            #    転換ペインの**直前**に差し込む(閉じ div を探さずに済む)。
            _m = (_re.search(r"switchDetailTab\((\d+),'tenkan'\)", _doc)
                  or _re.search(r"switchDetailTab\((\d+),'all'\)", _doc))
            _done = False
            if _m:
                _seq = _m.group(1)
                _anch = (f"switchDetailTab({_seq},'tenkan')"
                         if f"switchDetailTab({_seq},'tenkan')" in _doc
                         else f"switchDetailTab({_seq},'all')")
                _bi = _doc.find(_anch)
                _be = _doc.find("</button>", _bi)
                _pk = (f'<div id="detail_{_seq}_tenkan" class="detail-tab-pane">'
                       if f'id="detail_{_seq}_tenkan"' in _doc
                       else f'<div id="detail_{_seq}_all" class="detail-tab-pane')
                _pi = _doc.find(_pk)
                if _be > 0 and _pi > 0:
                    _btn = ('<!--EH-BTN-START-->'
                            f'<button class="detail-tab-btn" '
                            f'onclick="switchDetailTab({_seq},\'eh\')" '
                            f'style="border-color:#a78bfa">&#x1F501; E/H 比較 '
                            f'<span style="font-size:0.72rem;color:#c4b5fd">'
                            f'({len(_trows):,}件)</span></button>'
                            '<!--EH-BTN-END-->')
                    _pane = ('\n<!--EH-PANE-START-->\n'
                             f'<div id="detail_{_seq}_eh" class="detail-tab-pane">\n'
                             f'{_EH_BODY}\n</div>\n<!--EH-PANE-END-->\n')
                    _doc = _doc[:_pi] + _pane + _doc[_pi:]
                    _be2 = _doc.find("</button>", _doc.find(_anch)) + len("</button>")
                    _doc = _doc[:_be2] + _btn + _doc[_be2:]
                    _done = True
                    _where = f"損益タブ内(転換の隣) / detail_{_seq}_eh"

            # ── ② 見つからなければ外側タブにフォールバック ──────────
            if not _done:
                _nav = '<div class="ho-outer-nav">'
                _i2 = _doc.find(_nav)
                _j2 = _doc.find("</div>", _i2) if _i2 >= 0 else -1
                if _i2 < 0 or _j2 < 0:
                    print("[error] タブ構造が見つかりません。"
                          "レポートの生成コードが変わった可能性があります")
                    _tgt = None
                else:
                    _BTN = ('\n  <button class="ho-outer-btn" '
                            'onclick="switchHoTab(\'eh\')">&#x1F501; E/H 比較</button>')
                    _doc = _doc[:_j2] + _BTN + "\n" + _doc[_j2:]
                    _k2 = _doc.rfind("</body>")
                    _pane = (f'\n<!--EH-TAB-START-->\n'
                             f'<div id="ho-eh" class="ho-outer-pane">\n{_EH_BODY}\n'
                             f'</div>\n<!--EH-TAB-END-->\n')
                    _doc = (_doc[:_k2] + _pane + _doc[_k2:]) if _k2 > 0 else _doc + _pane
                    _done = True
                    _where = "外側タブ (ho-eh) ※損益タブ内が見つからず"

            if _done and _tgt is not None:
                _P(str(_tgt) + ".bak").write_text(_orig, encoding="utf-8")
                _tgt.write_text(_doc, encoding="utf-8")
                print(f"\n[差し込み] {_tgt.resolve()}")
                print(f"  {_where}  ({len(_trows):,}行)")
                print(f"  元のファイルは {_tgt.name}.bak に退避")
