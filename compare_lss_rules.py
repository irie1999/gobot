"""compare_lss_rules.py — lss(逆指値空売り・同日決済)の決済/エントリー・ルール案を
ネット損益(勝ち+負け)・勝率・PFで比較する A/B 検証ツール。

analyze_lss_losses.py は「負けたトレード」だけを形状分析するが、本ツールは
**全トレード(勝ち・負け・引け)を各ルール案で再計算してネット期待値を比較**する。
ルール変更が本当に総損益を改善するかを判定するのが目的。

比較するルール案:
  base           : 現行 v13(約定バーから損切り・sm=0.1)。レポートと同じ。
  delay1         : 寄りの1本目(約定バー)は損切りを効かせない(利確のみ)。2本目以降から損切り。
                   = ライブで「寄り5分後に逆指値損切りを設置」。同バーのヒゲ刈り回避。
  delay2         : 約定バー+次の1本(=寄り10分)まで損切りなし。3本目から損切り。
  gap<-1.0%見送り: 寄りがトリガーより1.0%以上ギャップダウンしたトレードは発注しない。
  gap<-1.5/2.0% : 同上、しきい値違い。
  delay1+gap1.5  : 両方併用。
  sm0.2 / sm0.3  : 損切り幅を広げる(約定バーから)。
  delay1+sm0.2   : 寄り1本目なし + 損切り0.2。

使い方(あなたの機械で。5分足データが必要):
  python compare_lss_rules.py --days 240 --min-price 1000 --max-price 6000
  python compare_lss_rules.py --by-month           # 月別内訳も出す
出力: コンソール比較表 + compare_lss_rules_<date>.csv
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lssルール案のネット損益A/B比較")
ap.add_argument("--symbols-file", type=str, default=None)
ap.add_argument("--days", type=int, default=240)
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--slip", type=float, default=0.0)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--bt-min", type=float, default=0.0,
                help="BTスコア下限で母集団を絞る(実際に投資する集団)。30=BT30以上/70=BT70以上/0=全部。"
                     "各(銘柄,戦略)の現行base(v13)成績を直近365日で6期間スライスし calc_recommend_score で算出")
ap.add_argument("--by-month", action="store_true", help="月別内訳も出力")
ap.add_argument("--audit", type=str, default="",
                help="指定ルール(例: delay1)の全トレードを1件ずつCSVに監査出力。各トレードの "
                     "楽観(=レポート値)/現実/保守のpnlと理由・MAE(最大逆行)を並べ、『利確が現実でも"
                     "プラスか』『損切りが現実でどれだけ悪化するか』『利確に必要な含み損の深さ』を"
                     "全件確認できる。母集団は --bt-min に従う")
ap.add_argument("--stop-slip", type=float, default=0.005,
                help="保守モデルの損切りスリッページ(既定0.5%%)。実スリッページが大きい想定なら "
                     "0.01(1%%)/0.02(2%%)に上げてストレス。② テストより損切りが大きくなる懸念の検証用")
args = ap.parse_args()

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick, round_to_tick, tick_size
from check_signals_stop import PERIODS as _BT_PERIODS, calc_recommend_score as _calc_bt
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_pnl

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False

QTY = ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY
_GAP_LIMIT = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
_ON_CLOSE = getattr(ble, "_INTRADAY_5M_ON_CLOSE", False)
TODAY = pd.Timestamp.now().normalize()

# ルール案の定義。stop_off_bars=約定バーから何本 損切りを無効にするか(0=約定バーから有効=base)。
# sm2=損切りATR倍率(None=args.sm)。gap_skip=このギャップ率(負値)未満のギャップダウンは見送り。
RULES = [
    {"name": "base(v13 sm0.1)",       "stop_off": 0, "sm2": None, "gap_skip": None},
    {"name": "delay1(寄1本目stopなし)", "stop_off": 1, "sm2": None, "gap_skip": None},
    {"name": "delay2(寄2本目からstop)", "stop_off": 2, "sm2": None, "gap_skip": None},
    {"name": "delay3(寄3本目=15分後)",   "stop_off": 3, "sm2": None, "gap_skip": None},
    {"name": "delay4(寄4本目=20分後)",   "stop_off": 4, "sm2": None, "gap_skip": None},
    {"name": "gap<-1.0%見送り",        "stop_off": 0, "sm2": None, "gap_skip": -0.010},
    {"name": "gap<-1.5%見送り",        "stop_off": 0, "sm2": None, "gap_skip": -0.015},
    {"name": "gap<-2.0%見送り",        "stop_off": 0, "sm2": None, "gap_skip": -0.020},
    {"name": "delay1+gap<-1.5%",       "stop_off": 1, "sm2": None, "gap_skip": -0.015},
    {"name": "sm0.2(base)",            "stop_off": 0, "sm2": 0.2,  "gap_skip": None},
    {"name": "sm0.3(base)",            "stop_off": 0, "sm2": 0.3,  "gap_skip": None},
    {"name": "delay1+sm0.2",           "stop_off": 1, "sm2": 0.2,  "gap_skip": None},
    # 保険のハード損切り: 約定と同時に広い損切り(entry+H%)を置く。タイト損切りは delay1(次足)。
    # ①含み損に耐えきれず切る問題を機械化(H%で必ず止まる) & ②走り上がりの青天井損失をキャップ。
    {"name": "delay1+hard3%",          "stop_off": 1, "sm2": None, "gap_skip": None, "hard": 0.03},
    {"name": "delay1+hard5%",          "stop_off": 1, "sm2": None, "gap_skip": None, "hard": 0.05},
    # 損切り終値判定: 5分足が"終値"で損切り線を超えたバーで発火=一瞬の上ヒゲでは損切らない
    # (ユーザー案: 一定時間=1本(5分)ラインを超えたら本当に損切る)。利確はタッチのまま。
    {"name": "損切り終値判定(soc)",       "stop_off": 0, "sm2": None, "gap_skip": None, "soc": True},
    {"name": "delay1+損切り終値",         "stop_off": 1, "sm2": None, "gap_skip": None, "soc": True},

    # ─── ターゲット倍率スイープ (delay1ベース) ─────────────────────────
    # target_r: 損切り幅(stop_dist)の何倍下を利確とするか。現行はsm=0.1,tm=1.0で約10R相当。
    # 例: target_r=2 → 損切り幅の2倍下で利確(タイト)、target_r=7 → 7倍下(現行の約70%)
    {"name": "d1+target2R",              "stop_off": 1, "sm2": None, "gap_skip": None, "target_r": 2.0},
    {"name": "d1+target3R",              "stop_off": 1, "sm2": None, "gap_skip": None, "target_r": 3.0},
    {"name": "d1+target5R",              "stop_off": 1, "sm2": None, "gap_skip": None, "target_r": 5.0},
    {"name": "d1+target7R",              "stop_off": 1, "sm2": None, "gap_skip": None, "target_r": 7.0},

    # ─── Break-even 移動ストップ (delay1ベース) ─────────────────────────
    # be_r: MFEがstop_distのbe_r倍に達したらストップをエントリー価格(BE)に移動する。
    # 例: be_r=2 → 損切り幅の2倍有利に動いたらBE。その後反転してもBEで脱出。
    {"name": "d1+BE@2R",                 "stop_off": 1, "sm2": None, "gap_skip": None, "be_r": 2.0},
    {"name": "d1+BE@3R",                 "stop_off": 1, "sm2": None, "gap_skip": None, "be_r": 3.0},
    {"name": "d1+BE@5R",                 "stop_off": 1, "sm2": None, "gap_skip": None, "be_r": 5.0},
    {"name": "d1+BE@7R",                 "stop_off": 1, "sm2": None, "gap_skip": None, "be_r": 7.0},

    # ─── 損切り幅スイープ (delay1ベース) ────────────────────────────────
    # sm2: 現行sm=0.1に対して損切り幅を変える。広いほど損切りが遅くなる。
    {"name": "d1+sm0.15",                "stop_off": 1, "sm2": 0.15, "gap_skip": None},
    {"name": "d1+sm0.2",                 "stop_off": 1, "sm2": 0.20, "gap_skip": None},
    {"name": "d1+sm0.3",                 "stop_off": 1, "sm2": 0.30, "gap_skip": None},

    # ─── 複合ルール ──────────────────────────────────────────────────────
    {"name": "d1+BE@3R+target5R",        "stop_off": 1, "sm2": None, "gap_skip": None, "be_r": 3.0, "target_r": 5.0},
    {"name": "d1+BE@5R+target7R",        "stop_off": 1, "sm2": None, "gap_skip": None, "be_r": 5.0, "target_r": 7.0},
    {"name": "d1+sm0.15+BE@3R",          "stop_off": 1, "sm2": 0.15, "gap_skip": None, "be_r": 3.0},
    {"name": "d1+sm0.2+BE@3R",           "stop_off": 1, "sm2": 0.20, "gap_skip": None, "be_r": 3.0},

    # ─── delay1なし + 早期利確 / BE移動ストップ (baseベース) ─────────────
    # delay1の影響を切り離して、純粋に「早期利確/BE」の効果だけを測る。
    {"name": "base+target2R",            "stop_off": 0, "sm2": None, "gap_skip": None, "target_r": 2.0},
    {"name": "base+target3R",            "stop_off": 0, "sm2": None, "gap_skip": None, "target_r": 3.0},
    {"name": "base+target5R",            "stop_off": 0, "sm2": None, "gap_skip": None, "target_r": 5.0},
    {"name": "base+target7R",            "stop_off": 0, "sm2": None, "gap_skip": None, "target_r": 7.0},
    {"name": "base+BE@3R",               "stop_off": 0, "sm2": None, "gap_skip": None, "be_r": 3.0},
    {"name": "base+BE@5R",               "stop_off": 0, "sm2": None, "gap_skip": None, "be_r": 5.0},
    {"name": "base+BE@7R",               "stop_off": 0, "sm2": None, "gap_skip": None, "be_r": 7.0},
]

# --audit で監査するルール名(先頭一致。例 "delay1" → "delay1(寄1本目stopなし)")。
_AUDIT_NAME = next((ru["name"] for ru in RULES if ru["name"].startswith(args.audit)), None) \
    if args.audit else None


def _load_selected() -> list[tuple]:
    path = args.symbols_file
    if path is None:
        for cand in ("holdout_selected_symbols.py", "holdout_selected_symbols_short.py"):
            if Path(cand).exists():
                path = cand
                break
    if path is None:
        props = sorted(Path(".").glob("lss_watchlist_proposal_*.py"))
        if props:
            path = str(props[-1])
    if path is None or not Path(path).exists():
        sys.exit("[error] 選定ファイルが見つかりません(--symbols-file で指定)")
    spec = importlib.util.spec_from_file_location("_sel_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = getattr(mod, "SELECTED", None) or getattr(mod, "SELECTED_PAIRS", None)
    if rows is None:
        sys.exit(f"[error] {path} に SELECTED がありません")
    out = [(str(r[0]), str(r[1]) if len(r) >= 3 else "", str(r[-1])) for r in rows if len(r) >= 2]
    print(f"[選定] {path} から {len(out)} ペア読込")
    return out


def _day_ohlc(df_raw, fd):
    try:
        drow = df_raw.loc[df_raw.index.normalize() == pd.Timestamp(fd)]
        if len(drow):
            return (float(drow["open"].iloc[0]), float(drow["low"].iloc[0]),
                    float(drow["high"].iloc[0]), float(drow["close"].iloc[0]))
    except Exception:
        pass
    return (None, None, None, None)


def _prices(order_limit, order_stop, order_target):
    base = round_to_tick(order_limit)
    trigger = float(round_to_tick(base - tick_size(base)))
    stop_p = float(ceil_to_tick(max(order_stop, order_target)))
    target_p = float(ceil_to_tick(min(order_stop, order_target)))
    return trigger, stop_p, target_p


def _bt_score(trades) -> int:
    """(約定日, base-line pnl) のリストから BTスコアを算出(レポートのBTに近い近似)。
    直近365日を PERIODS(30/90/180/365)でスライスし勝率/PF/安定/取引数で採点。
    現行base(v13, stopちょうど=engineと同じ楽観fill)の成績で計算=レポートのBT定義に一致。"""
    if not trades:
        return 0
    pr = {}
    for P in _BT_PERIODS:
        cutoff = TODAY - pd.Timedelta(days=P)
        sub = [p for (d, p) in trades if pd.Timestamp(d) >= cutoff]
        if not sub:
            continue
        wins = sum(1 for p in sub if p > 0)
        gp = sum(p for p in sub if p > 0)
        gl = -sum(p for p in sub if p <= 0)
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[P] = {"trades": len(sub), "win_rate": wins / len(sub) * 100,
                 "pf": pf, "total_pnl": sum(sub)}
    return _calc_bt(pr)[0]


_STOP_SLIP = args.stop_slip   # 保守モデルの損切りスリッページ(既定0.5%、--stop-slip で可変)


def _exit(opens, highs, lows, closes, ei, stop_from, stop_p, target_p, day_close,
          hard_stop_p=None, stop_on_close=None, be_r=None, entry_fill=None, target_r=None):
    """約定バー ei から利確、stop_from(>=ei) からタイト損切りを first-touch。損切り優先。
    hard_stop_p(保険の広い損切り)を渡すと、約定と同時に(ei から)効く=無保護窓でも青天井を防ぐ。
    損切り発火時の買い戻し3通り(line=楽観/real=窓埋め/slip=+スリッページ)。

    be_r: MFEが stop_dist × be_r に達したらストップを entry_fill(BE)に移動する。
          例: be_r=3 → 損切り幅の3倍分有利に動いたらBE移動。その後反転しても0損失で脱出。
    entry_fill: 約定価格(be_r / target_r の基準点)。
    target_r: stop_dist × target_r 下を利確価格とする。現行の target_p を上書き。
              例: target_r=5 → 損切り幅の5倍下で利確(現行の約50%早め)。

    戻り: (reason, ex_line, ex_real, ex_slip, exit_idx)。"""
    n = len(highs)
    _soc = _ON_CLOSE if stop_on_close is None else stop_on_close

    # be_r / target_r 用の stop_dist(short: stop_p > entry_fill)
    stop_dist = 0.0
    if entry_fill is not None and stop_p > entry_fill:
        stop_dist = stop_p - entry_fill

    # target_r 指定: ターゲット価格を stop_dist × R 下に上書き
    if target_r is not None and stop_dist > 0 and entry_fill is not None:
        target_p = float(ceil_to_tick(entry_fill - target_r * stop_dist))

    # be_r: BEに移動するトリガー価格(短: entry_fill - be_r × stop_dist まで下落でBE発動)
    be_trigger_p = None
    if be_r is not None and stop_dist > 0 and entry_fill is not None:
        be_trigger_p = entry_fill - be_r * stop_dist
    eff_stop_p = stop_p   # BE発動後に entry_fill に更新される

    def _stop_fills(px, j):
        line = float(px)
        real = line if j == ei else max(line, float(opens[j]))   # 次足以降は窓埋め(始値)
        return line, real, real * (1.0 + _STOP_SLIP)

    for j in range(ei, n):
        # BE移動: lows がトリガーを下抜けたらストップをBE(entry_fill)に引き上げ
        if be_trigger_p is not None and lows[j] <= be_trigger_p:
            eff_stop_p = entry_fill   # BEに移動(entry=1000なら損切りも1000)
            be_trigger_p = None        # 一度だけ発動

        # タイト損切り(eff_stop_p): 武装済み(j>=stop_from)なら上昇時に先に当たる
        if j >= stop_from:
            sh = closes[j] >= eff_stop_p if _soc else highs[j] >= eff_stop_p
            if sh:
                px = float(closes[j]) if _soc else eff_stop_p
                ln, rl, sl = _stop_fills(px, j)
                return "stop", ln, rl, sl, j
        # 保険のハード損切り(広い): ei から常時
        if hard_stop_p is not None and highs[j] >= hard_stop_p:
            ln, rl, sl = _stop_fills(hard_stop_p, j)
            return "hardstop", ln, rl, sl, j
        th = closes[j] <= target_p if _ON_CLOSE else lows[j] <= target_p
        if th:
            tp = float(closes[j]) if _ON_CLOSE else target_p
            return "target", tp, tp, tp, j
    cx = day_close if (day_close and day_close > 0) else float(closes[-1])
    return "close", cx, cx, cx, n - 1


def _scan_symbol(sym: str, name: str, strats: list[str]) -> dict:
    """1銘柄・全戦略のlssトレードを各ルールで再計算し、ルール別の集計partialを返す。"""
    # pnl は損切り約定モデル別に3通り集計: line(楽観)/open(現実・窓埋め)/high(最悪)。
    # 勝率/PF は open(現実)ベースで表示。
    agg = {r["name"]: {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                       "pnl_line": 0.0, "pnl_real": 0.0, "pnl_slip": 0.0,
                       "tgt": 0, "stop": 0, "close": 0} for r in RULES}
    mon = {}  # (rule,month)->pnl (by-month用)
    audit_rows: list = []   # --audit 指定ルールの全トレード(監査用)
    strat_contrib: dict = {}  # 戦略別 base ルール成績(BT合格済みのみ)
    strat_band_contrib: dict = {}  # (戦略, BT帯下限) → base ルール成績(フィルター前・全BT記録)
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return {"agg": agg, "mon": mon}
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return {"agg": agg, "mon": mon}
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return {"agg": agg, "mon": mon}
    if df_raw is None or df_raw.empty:
        return {"agg": agg, "mon": mon}

    # BTスコアは直近365日で算出するので、ルール比較窓(--days)より長く見る必要がある。
    _bt_window = max(args.days, max(_BT_PERIODS))
    for strat in strats:
        mod = mod_for(strat)
        params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
        if not params:
            continue
        cf = params[0]
        try:
            df_ind = cf(df_raw.copy())
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.sm, args.tm, args.days + 420, strat,
                                       entry_type="stop_sell", max_hold=0)
        except Exception:
            continue
        if not r:
            continue
        # この (銘柄,戦略) 分をローカル集計し、BTスコアが閾値を満たしたら agg に合流する。
        local = {ru["name"]: {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                              "pnl_line": 0.0, "pnl_real": 0.0, "pnl_slip": 0.0,
                              "tgt": 0, "stop": 0, "close": 0} for ru in RULES}
        local_mon: dict = {}
        local_audit: list = []   # --audit ルールの全トレード(BT合格時のみ確定)
        bt_trades: list = []   # (約定日, base-lineのpnl) 直近365日 → BTスコア算出用
        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            olp = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if olp <= 0 or osp <= 0 or otp <= 0 or olp < args.min_price or olp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=_bt_window):
                continue
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            trigger, stop_p, target_p = _prices(olp, osp, otp)
            d_open, d_low, d_high, d_close = _day_ohlc(df_raw, fd)
            entry_fill = short_entry_fill_5m(db, trigger, False, entry_gap_limit=_GAP_LIMIT,
                                             day_open=d_open, day_low=d_low, day_high=d_high)
            if entry_fill is None:
                continue
            gap_pct = (entry_fill - trigger) / trigger if trigger > 0 else 0.0
            atr = (osp - olp) / args.sm if args.sm > 0 else 0.0
            opens = db["open"].to_numpy(dtype=float)
            highs = db["high"].to_numpy(dtype=float)
            lows = db["low"].to_numpy(dtype=float)
            closes = db["close"].to_numpy(dtype=float)
            n = len(lows)
            # 約定バー ei(5分足がトリガー未達なら日足安値救済で ei=0)
            ei = None
            for j in range(n):
                if lows[j] <= trigger:
                    ei = j
                    break
            if ei is None:
                if d_low is not None and d_low > 0 and d_low <= trigger:
                    ei = 0
                else:
                    continue
            # BTスコア用: base(現行v13, stopちょうど=engine楽観fill)の pnl を365日ぶん記録
            rb, exl, _, _, _ = _exit(opens, highs, lows, closes, ei, ei, stop_p, target_p, d_close)
            bt_trades.append((fd, short_pnl(entry_fill, exl, rb, QTY, FEE_ONE_WAY, 0.0)))
            # ルール比較は --days 窓のトレードだけ
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=args.days):
                continue
            mon_key = str(fd)[:7]
            for rule in RULES:
                if rule["gap_skip"] is not None and gap_pct <= rule["gap_skip"]:
                    continue  # ギャップダウン見送り = このトレードは発注しない
                if rule["sm2"] is not None and atr > 0:
                    sp = float(ceil_to_tick(olp + atr * rule["sm2"]))
                else:
                    sp = stop_p
                stop_from = ei + rule["stop_off"]
                hard_p = float(entry_fill * (1.0 + rule["hard"])) if rule.get("hard") else None
                reason, ex_line, ex_real, ex_slip, exit_idx = _exit(
                    opens, highs, lows, closes, ei, stop_from, sp, target_p, d_close,
                    hard_stop_p=hard_p, stop_on_close=rule.get("soc"),
                    be_r=rule.get("be_r"), entry_fill=entry_fill,
                    target_r=rule.get("target_r"))
                pnl_line = short_pnl(entry_fill, ex_line, reason, QTY, FEE_ONE_WAY, 0.0)
                pnl_real = short_pnl(entry_fill, ex_real, reason, QTY, FEE_ONE_WAY, 0.0)
                pnl_slip = short_pnl(entry_fill, ex_slip, reason, QTY, FEE_ONE_WAY, 0.0)
                a = local[rule["name"]]
                a["n"] += 1
                a["pnl_line"] += pnl_line
                a["pnl_real"] += pnl_real
                a["pnl_slip"] += pnl_slip
                # hardstop も損失なので stop に計上(利確=tgt / 引け=close / それ以外=stop)
                a["tgt" if reason == "target" else ("close" if reason == "close" else "stop")] += 1
                if pnl_real > 0:
                    a["win"] += 1
                    a["gp"] += pnl_real
                else:
                    a["gl"] += -pnl_real
                if args.by_month:
                    local_mon[(rule["name"], mon_key)] = \
                        local_mon.get((rule["name"], mon_key), 0.0) + pnl_real
                if rule["name"] == _AUDIT_NAME:
                    _ja = {"stop": "損切り", "target": "利確", "close": "引け",
                           "hardstop": "ハード損切り"}.get(reason, reason)
                    # MAE=最大逆行(約定〜決済までに価格がショートに逆行して付けた最大の含み損%)。
                    _mae_hi = float(highs[ei:exit_idx + 1].max()) if exit_idx >= ei else float(highs[ei])
                    _mae_pct = (_mae_hi - entry_fill) / entry_fill * 100 if entry_fill > 0 else 0.0
                    # MFE=最大有利到達点(ショート: どこまで価格が下落したか)。
                    # target_dist = entry→target の距離。MFE_ratio = 何%到達したか(100%=利確水準)。
                    _mfe_lo = float(lows[ei:exit_idx + 1].min()) if exit_idx >= ei else float(lows[ei])
                    _target_dist = entry_fill - target_p  # ショート: 下方向が有利
                    _mfe_ratio = (entry_fill - _mfe_lo) / _target_dist * 100 if _target_dist > 0 else 0.0
                    local_audit.append({
                        "date": str(fd), "sym": sym, "name": name, "strat": strat,
                        "reason": _ja, "entry": round(entry_fill, 1),
                        "stop": round(sp, 1), "target": round(target_p, 1),
                        "MAE最大逆行%": round(_mae_pct, 1),
                        "MFE利確到達率%": round(_mfe_ratio, 1),  # 100%=利確水準まで到達, 50%=半分まで
                        "pnl_report_楽観": round(pnl_line, 0),
                        "pnl_現実": round(pnl_real, 0),
                        "pnl_保守": round(pnl_slip, 0),
                        "現実−楽観": round(pnl_real - pnl_line, 0),
                    })
        # BTスコアで母集団を絞る(実際に投資する集団)。閾値未満は集計に含めない。
        bt_score_val = _bt_score(bt_trades)
        # BT帯別記録: bt_min フィルター前に全ペアを帯ごとに集積(戦略効果の独立検証用)
        _bt_band = (bt_score_val // 20) * 20  # 0/20/40/60/80...
        _base_s = local["base(v13 sm0.1)"]
        _band_key = (strat, _bt_band)
        _sc2 = strat_band_contrib.setdefault(_band_key, {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                                                          "pnl_real": 0.0, "tgt": 0, "stop": 0, "close": 0})
        for _k in _sc2:
            _sc2[_k] += _base_s[_k]
        if args.bt_min > 0 and bt_score_val < args.bt_min:
            continue
        for rname, a in local.items():
            t2 = agg[rname]
            for k in a:
                t2[k] += a[k]
        for k, v in local_mon.items():
            mon[k] = mon.get(k, 0.0) + v
        audit_rows.extend(local_audit)
        # 戦略別内訳: baseルールの成績をstrat単位で集積
        base_s = local["base(v13 sm0.1)"]
        sc = strat_contrib.setdefault(strat, {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                                               "pnl_real": 0.0, "tgt": 0, "stop": 0, "close": 0})
        for k in sc:
            sc[k] += base_s[k]
    return {"agg": agg, "mon": mon, "audit": audit_rows, "strat_contrib": strat_contrib,
            "strat_band_contrib": strat_band_contrib}


def _pf(gp, gl):
    return gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)


def _pf_s(gp, gl):
    v = _pf(gp, gl)
    return "∞" if v == float("inf") else f"{v:.2f}"


def main():
    sel = _load_selected()
    by_sym: dict[str, dict] = {}
    for code, name, strat in sel:
        d = by_sym.setdefault(code, {"name": name, "strats": []})
        if strat not in d["strats"]:
            d["strats"].append(strat)
        if name and not d["name"]:
            d["name"] = name
    syms = list(by_sym.items())
    if args.limit:
        syms = syms[:args.limit]
    _btlab = f" / BT{args.bt_min:.0f}以上に絞る" if args.bt_min > 0 else " / 全部(BTフィルタ無し)"
    print(f"[比較] {len(syms)}銘柄 / 遡及{args.days}日 / 価格{args.min_price:.0f}〜{args.max_price:.0f}円 "
          f"/ {len(RULES)}ルール{_btlab}")

    total = {r["name"]: {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                         "pnl_line": 0.0, "pnl_real": 0.0, "pnl_slip": 0.0,
                         "tgt": 0, "stop": 0, "close": 0} for r in RULES}
    mon_total: dict = {}
    audit_all: list = []
    strat_total: dict = {}       # 戦略別 base ルール集計(BT合格済み)
    strat_band_total: dict = {}  # (戦略, BT帯下限) → base ルール集計(全BT帯)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_symbol, code, v["name"], v["strats"]): code
                for code, v in syms}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                res = fut.result()
            except Exception:
                continue
            for name, a in res["agg"].items():
                t = total[name]
                for k in ("n", "win", "gp", "gl", "pnl_line", "pnl_real", "pnl_slip",
                          "tgt", "stop", "close"):
                    t[k] += a[k]
            for k, v in res["mon"].items():
                mon_total[k] = mon_total.get(k, 0.0) + v
            audit_all.extend(res.get("audit", []))
            for strat, sc in res.get("strat_contrib", {}).items():
                s = strat_total.setdefault(strat, {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                                                    "pnl_real": 0.0, "tgt": 0, "stop": 0, "close": 0})
                for k in s:
                    s[k] += sc[k]
            for (strat, band), sc in res.get("strat_band_contrib", {}).items():
                s = strat_band_total.setdefault((strat, band), {"n": 0, "win": 0, "gp": 0.0, "gl": 0.0,
                                                                  "pnl_real": 0.0, "tgt": 0, "stop": 0, "close": 0})
                for k in s:
                    s[k] += sc[k]
            if done % 50 == 0:
                base = total["base(v13 sm0.1)"]
                print(f"  ...{done}/{len(syms)}銘柄  base net(現実) {base['pnl_real']:+,.0f}円 "
                      f"({base['n']}件)")

    base = total["base(v13 sm0.1)"]
    print("\n" + "=" * 104)
    print("lss ルール案 ネット比較 — 損切り約定を 楽観(line)/現実(gap)/保守(+0.5%) で下限測定")
    print("=" * 104)
    print(f"{'ルール':<22}{'件数':>6}{'勝率':>6}{'PF':>6}{'利確':>5}{'損切':>5}{'引け':>5}"
          f"{'net楽観':>13}{'net現実':>13}{'net保守':>13}{'base比(現実)':>14}")
    for rule in RULES:
        t = total[rule["name"]]
        n = t["n"] or 1
        wr = t["win"] / n * 100
        d = t["pnl_real"] - base["pnl_real"]
        tag = " ←現状" if rule["name"].startswith("base") else ""
        print(f"{rule['name']:<22}{t['n']:>6}{wr:>5.0f}%{_pf_s(t['gp'], t['gl']):>6}"
              f"{t['tgt']:>5}{t['stop']:>5}{t['close']:>5}"
              f"{t['pnl_line']:>+13,.0f}{t['pnl_real']:>+13,.0f}{t['pnl_slip']:>+13,.0f}"
              f"{d:>+14,.0f}{tag}")
    print("\n※ net楽観=stopちょうど / net現実=次足損切りは窓埋め(始値約定)・同バーはstop / net保守=現実+0.5%")
    _pop = f"BT{args.bt_min:.0f}以上(実際に投資する集団)" if args.bt_min > 0 else "全選定ペア(BTフィルタ無し)"
    print(f"※ 見るポイント: delay1 の『net保守』が base の『net現実』を上回れば、liveでも堅牢。"
          f"base vs delay1 の『net現実』差が期待できる現実的な改善幅。母集団={_pop}。")

    # ── 戦略別内訳 (baseルール) ──
    if strat_total:
        print("\n" + "=" * 80)
        print(f"【戦略別内訳】baseルール / 母集団={_pop}")
        print("=" * 80)
        print(f"{'戦略':<12}{'件数':>6}{'勝率':>6}{'PF':>7}{'利確':>5}{'損切':>5}{'引け':>5}"
              f"{'net現実':>13}")
        for strat, s in sorted(strat_total.items(), key=lambda x: -x[1]["pnl_real"]):
            n = s["n"] or 1
            wr = s["win"] / n * 100
            print(f"{strat:<12}{s['n']:>6}{wr:>5.0f}%{_pf_s(s['gp'], s['gl']):>7}"
                  f"{s['tgt']:>5}{s['stop']:>5}{s['close']:>5}"
                  f"{s['pnl_real']:>+13,.0f}")

    # ── BTスコア帯×戦略別内訳 (baseルール, 全BT帯) ──
    if strat_band_total:
        print("\n" + "=" * 92)
        print("【BTスコア帯×戦略別内訳】baseルール — 同BT帯内での戦略差を確認(戦略効果がBT分布と独立か)")
        print("=" * 92)
        print(f"{'BT帯':<9}{'戦略':<12}{'件数':>6}{'勝率':>6}{'PF':>7}{'利確':>5}{'損切':>5}{'引け':>5}"
              f"{'net現実':>13}")
        bands = sorted({b for (_, b) in strat_band_total})
        for band in bands:
            band_label = f"{band}~{band+19}"
            entries = [(st, strat_band_total[(st, band)])
                       for (st, b) in strat_band_total if b == band and strat_band_total[(st, band)]["n"] > 0]
            if not entries:
                continue
            entries.sort(key=lambda x: -x[1]["pnl_real"])
            first = True
            for strat, s in entries:
                n = s["n"]
                wr = s["win"] / n * 100
                label = band_label if first else ""
                print(f"{label:<9}{strat:<12}{n:>6}{wr:>5.0f}%{_pf_s(s['gp'], s['gl']):>7}"
                      f"{s['tgt']:>5}{s['stop']:>5}{s['close']:>5}{s['pnl_real']:>+13,.0f}")
                first = False
            print()

    # ── 月別(--by-month) ──
    if args.by_month:
        months = sorted({m for (_, m) in mon_total})
        print("\n【月別ネット損益】")
        print(f"{'ルール':<22}" + "".join(f"{m[2:]:>10}" for m in months))
        for rule in RULES:
            row = "".join(f"{mon_total.get((rule['name'], m), 0.0):>10,.0f}" for m in months)
            print(f"{rule['name']:<22}{row}")

    # ── CSV ──
    date_s = str(TODAY.date())
    rows = []
    for rule in RULES:
        t = total[rule["name"]]
        n = t["n"] or 1
        rows.append({"rule": rule["name"], "trades": t["n"], "win_rate": round(t["win"]/n*100, 1),
                     "pf": round(_pf(t["gp"], t["gl"]), 2), "target": t["tgt"], "stop": t["stop"],
                     "close": t["close"], "net_optimistic_line": round(t["pnl_line"], 0),
                     "net_realistic_gap": round(t["pnl_real"], 0),
                     "net_conservative_slip": round(t["pnl_slip"], 0),
                     "vs_base_realistic": round(t["pnl_real"] - base["pnl_real"], 0)})
    pd.DataFrame(rows).to_csv(Path(f"compare_lss_rules_{date_s}.csv"), index=False,
                              encoding="utf-8-sig")
    print(f"\n[出力] compare_lss_rules_{date_s}.csv")

    # ── 監査(--audit): 指定ルールの全トレードを1件ずつ ──
    if _AUDIT_NAME and audit_all:
        adf = pd.DataFrame(audit_all).sort_values("pnl_現実")   # 現実で悪い順
        _short = args.audit
        apath = Path(f"lss_audit_{_short}_{date_s}.csv")
        adf.to_csv(apath, index=False, encoding="utf-8-sig")
        # 監査サマリー: 利確が現実でもプラスか / 損切りが現実でどれだけ悪化したか
        wins = adf[adf["pnl_現実"] > 0]   # 現実でプラスのトレード(利確+勝ち引け)
        stops = adf[adf["reason"].isin(["損切り", "ハード損切り"])]
        tgt = adf[adf["reason"] == "利確"]
        tgt_flip = int((tgt["pnl_現実"] <= 0).sum())   # 利確なのに現実マイナス
        print("\n" + "=" * 72)
        print(f"【監査】ルール『{_AUDIT_NAME}』 全{len(adf)}トレード (母集団=BT{args.bt_min:.0f}以上)")
        print("=" * 72)
        print(f"  利確判定: {len(tgt)}件 → 現実で損切りに転落 {tgt_flip}件 "
              f"({'想定どおり0(判定は楽観=現実)' if tgt_flip == 0 else '要確認'})")
        if len(stops):
            print(f"  損切り: {len(stops)}件 → 現実の悪化 平均 {stops['現実−楽観'].mean():+,.0f}円/件 "
                  f"(合計 {stops['現実−楽観'].sum():+,.0f}円)")
        # ① 含み損に耐えきれず切る問題の可視化: 勝ちトレードが我慢した含み損の深さ(MAE)
        if len(wins) and "MAE最大逆行%" in adf.columns:
            _m = wins["MAE最大逆行%"]
            print("\n  ── 勝ちトレードが利確までに我慢した含み損(MAE=最大逆行%) ──")
            print(f"     中央値 {_m.median():.1f}% / 平均 {_m.mean():.1f}% / 最大 {_m.max():.1f}%")
            for thr in (2, 3, 5):
                _cut = int((_m >= thr).sum())
                print(f"     含み損 {thr}%以上を耐えた勝ち = {_cut}件 "
                      f"({_cut/len(wins)*100:.0f}%) ← {thr}%で切っていたら失っていた利益")
            print("     → 『無保護窓の含み損に耐えきれず切る』と、これらの勝ちを損切りに変えてしまう。")
            print("       保険のハード損切り(delay1+hard3%/5% の行)で、切る水準を機械化して比較可能。")
        # ② 損切りトレードのMFE分析: 「利確方向に進んでから反転して損切られたか」
        if len(stops) and "MFE利確到達率%" in adf.columns:
            _mfe = stops["MFE利確到達率%"]
            print("\n  ── 損切りトレードの利確方向への到達率(MFE) ──")
            print(f"     中央値 {_mfe.median():.0f}% / 平均 {_mfe.mean():.0f}% / 最大 {_mfe.max():.0f}%")
            for thr in (50, 70, 90, 100):
                _n = int((_mfe >= thr).sum())
                print(f"     利確水準の{thr}%以上到達してから損切り = {_n}件 "
                      f"({_n/len(stops)*100:.1f}%)")
            print("     → 100%超 = 一度利確水準を突破したが反転して損切り。")
        print(f"\n  合計pnl: レポート(楽観) {adf['pnl_report_楽観'].sum():+,.0f}円 / "
              f"現実 {adf['pnl_現実'].sum():+,.0f}円 / 保守 {adf['pnl_保守'].sum():+,.0f}円")
        print(f"\n[出力] {apath}  (現実で悪い順。MFE列=利確方向への到達率 / MAE列=逆行耐久量)")


if __name__ == "__main__":
    main()
