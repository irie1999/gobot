"""
regime_perf_oos.py — 現行(順張り・ロング)戦略の "本物の" レジーム状態別成績。

look-ahead を排除したローリング再選定 Walk-forward:
  各基準月で as-of 選定した WATCHLIST を、その直後の N ヶ月(=次の再選定まで)だけ
  運用(OOS)し、全基準月ぶんを繋ぎ合わせて大局レジーム状態別に集計する。

  regime_perf.py … 現WATCHLISTを過去全体に適用(in-sample bias, 過大評価)
  これ(oos版) … 各時点で選んだ銘柄の直後の未来だけ = 実運用と同じ(バイアスなし)

前提: 先に各基準月の as-of スキャンを回して CSV を用意する:
  foreach($b in "2018-06-30","2019-06-30",...,"2025-06-30"){
    python scan_walkforward.py --family both --as-of $b --max-price 6000 --workers 8
  }
  → walkforward_results/walkforward_<戦略>_<基準日>.csv が出力される

使い方:
  python regime_perf_oos.py --bases 2018-06-30,2019-06-30,2020-06-30,2021-06-30,2022-06-30,2023-06-30,2024-06-30,2025-06-30
  python regime_perf_oos.py            # walkforward_results から基準日を自動検出
  python regime_perf_oos.py --interval-months 12 --per-strategy 10 --max-price 6000
"""
import argparse
import csv
import glob
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument("--bases", default=None, help="基準日をカンマ区切りで(省略時はCSVから自動検出)")
ap.add_argument("--interval-months", type=int, default=12, help="各基準月のOOS窓(=再選定間隔)")
ap.add_argument("--per-strategy", type=int, default=10)
ap.add_argument("--monthly", action="store_true", help="OOSトレードを月別にも集計して表示")
ap.add_argument("--html", action="store_true", help="損益タブ風HTML(大局レジーム付き月別グリッド)を出力してブラウザで開く")
ap.add_argument("--min-bt", type=float, default=0.0,
                help="このBTスコア以上のOOSトレードだけで状態別/月別を集計(例70)。BT帯別表は常に表示")
ap.add_argument("--max-bt", type=float, default=0.0,
                help="このBTスコア未満のトレードだけで集計(例40=BT<40だけ)。--min-btと併用可")
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--max-dd", type=float, default=15.0)
ap.add_argument("--years", type=int, default=15, help="日経レジーム系列の取得年数")
ap.add_argument("--fee", type=float, default=None,
                help="片道手数料を上書き(例0=無料)。既定は0.001(往復0.2%%)")
ap.add_argument("--slip", type=float, default=None,
                help="逆指値買い/損切りスリッページを上書き(例0.002=0.2%%)。既定0.005")
ap.add_argument("--aggressive", action="store_true")
ap.add_argument("--short", action="store_true",
                help="ショート戦略で検証(A7_S/RSI2_S/MACD_S/DON_S/MOM_S/GAP_S)")
ap.add_argument("--fade", action="store_true",
                help="シグナルの逆を取る(ロング戦略→ショート/ショート戦略→ロング)で検証")
ap.add_argument("--mirror", action="store_true",
                help="各取引の損益を符号反転(=同じ約定値で逆サイドを取った鏡写し)。手数料は往復2倍で計上")
ap.add_argument("--losers", action="store_true",
                help="勝ち銘柄top-Nでなく『それまでの累積損益ワーストN』を選定(as-of)。--mirrorと併用してフェード検証")
ap.add_argument("--max-hold", type=int, default=0,
                help="保有上限(タイムカット)日数を上書き(例3=3日で終値決済)。ミラーの早期利確検証用。0=既定(戦略別)")
ap.add_argument("--holdout-select", action="store_true",
                help="6つのholdout窓(HO30〜180d)の各top-Nを統合して選定(walkforward_*_holdout{N}d_{base}.csv を使用)")
args = ap.parse_args()
if args.aggressive:
    os.environ["TRADING_MODE"] = "aggressive"

import numpy as np
import pandas as pd
import backtest_limit_entry as ble
import nikkei_analysis as na

# コスト上書き(あなたの実コストで測る用)。run_limit_backtest はモジュール定数を
# 実行時に参照するので、ここで書き換えれば全バックテストに反映される。
if args.fee is not None:
    ble.FEE_PCT_ONE_WAY = args.fee
if args.slip is not None:
    ble.SLIPPAGE_STOP_PCT = args.slip

# 検証対象の戦略群(ロング=--family both / ショート=--family short+short_brk)
if args.short:
    STRATS = ["A7_S", "RSI2_S", "MACD_S", "DON_S", "MOM_S", "GAP_S"]
    _MOD_NAMES = ("check_signals_short", "check_signals_short_breakout")
else:
    STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]
    _MOD_NAMES = ("check_signals_stop", "check_signals_breakout")
MODE_SUFFIX = "_aggressive" if args.aggressive else ""

_mods = {}
for _n in _MOD_NAMES:
    try:
        _mods[_n] = __import__(_n)
    except Exception as _e:
        print(f"[WARN] {_n} の読み込み失敗: {_e}")


def mod_for(strat):
    for o in _mods.values():
        if strat in getattr(o, "STRATEGY_PARAMS", {}):
            return o
    return None


def nikkei_state_series(years: int) -> pd.Series | None:
    c = na.fetch_n225(years)
    if c is None or len(c) < 260:
        return None
    c = c.sort_index()
    ma200 = c.rolling(200).mean()
    slope = ma200.pct_change(20) * 100
    er = (c - c.shift(60)).abs() / c.diff().abs().rolling(60).sum().replace(0, np.nan)
    above = c >= ma200
    st = pd.Series(index=c.index, dtype=object)
    for i in range(len(c)):
        e, s, a, m = er.iloc[i], slope.iloc[i], above.iloc[i], ma200.iloc[i]
        if not np.isfinite(m) or not np.isfinite(e):
            st.iloc[i] = "?"; continue
        if e < 0.20:
            st.iloc[i] = "range" if (e < 0.15 and abs(s) < 0.5) else "wait"
        elif a and s > 0:
            st.iloc[i] = "up"
        elif (not a) and s < 0:
            st.iloc[i] = "down"
        else:
            st.iloc[i] = "wait"
    return st


LABELS = {"up": "🟢 上げ", "wait": "🟡 横ばい待ち", "range": "🟠 真横ばい", "down": "🔴 下げ"}


def _detect_bases() -> list[str]:
    """walkforward_results から (全STRATが揃っている) 基準日を検出。"""
    found: dict[str, set] = defaultdict(set)
    for strat in STRATS:
        for f in glob.glob(f"walkforward_results/walkforward_{strat}{MODE_SUFFIX}_*.csv"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
            if m:
                found[m.group(1)].add(strat)
    # holdout系(_holdoutNNd_)を除外
    bases = [d for d, ss in found.items()
             if len(ss) >= 3 and not any("holdout" in os.path.basename(x)
                 for x in glob.glob(f"walkforward_results/*_{d}.csv"))]
    return sorted(bases)


HOLDOUT_WINDOWS = (30, 60, 90, 120, 150, 180)


def _rank_topN(rows: list) -> list:
    """1つのCSVの行から、選定条件でフィルタ→ソート→top-N を返す。"""
    if args.losers:
        # 累積損益ワーストN(as-of): total_test_pnl が最も低い銘柄を選ぶ(DD制約なし)
        filt = [r for r in rows
                if float(r.get("total_test_pnl", 0) or 0) < 0
                and args.min_price <= float(r.get("latest_price", 0) or 0) <= args.max_price]
        filt.sort(key=lambda r: float(r.get("total_test_pnl", 0) or 0))  # 昇順=最悪が先頭
    else:
        # 従来: 勝ち銘柄top-N
        filt = [r for r in rows
                if float(r.get("total_test_pnl", 0) or 0) > 0
                and float(r.get("max_drawdown_pct", 999) or 999) <= args.max_dd
                and args.min_price <= float(r.get("latest_price", 0) or 0) <= args.max_price]
        filt.sort(key=lambda r: -float(r.get("total_test_pnl", 0) or 0))
    return filt[: args.per_strategy]


def _read_csv(path: str) -> list:
    try:
        return list(csv.DictReader(open(path, encoding="utf-8")))
    except Exception:
        return []


def _select_watchlist(base: str) -> dict:
    """基準日 base の CSV から WATCHLIST を選定(TEST損益>0 / DD<=15 / 価格帯 / top-N)。
    --holdout-select 時は6つのholdout窓(HO30〜180d)それぞれの top-N を統合(重複除去)する。"""
    wl = {}
    for strat in STRATS:
        if args.holdout_select:
            # 6つのholdout窓それぞれから各top-Nを取り、統合(top10を弱いランクで薄めない)
            for hd in HOLDOUT_WINDOWS:
                path = (f"walkforward_results/walkforward_{strat}{MODE_SUFFIX}"
                        f"_holdout{hd}d_{base}.csv")
                for r in _rank_topN(_read_csv(path)):
                    if r.get("symbol"):
                        wl[(r["symbol"], strat)] = r.get("name", "")
        else:
            pat = f"walkforward_results/walkforward_{strat}{MODE_SUFFIX}_{base}.csv"
            for f in glob.glob(pat):
                if "holdout" in os.path.basename(f):
                    continue
                for r in _rank_topN(_read_csv(f)):
                    if r.get("symbol"):
                        wl[(r["symbol"], strat)] = r.get("name", "")
    return wl


def asof_bt(full, mod, sd, periods=(30, 90, 180, 365)):
    """シグナル日 sd 以前に決済したトレードだけで当時BTスコアを計算(先読みなし)。"""
    pr = {}
    for p in periods:
        lo = sd - timedelta(days=p)
        n = w = 0
        gp = gl = tot = 0.0
        for t in full:
            if t.get("reason") in ("発注中", "保有中"):
                continue
            ed = t.get("exit_dt")
            if ed is None:
                continue
            edd = ed.date() if hasattr(ed, "date") else ed
            if lo <= edd < sd:
                pnl = float(t.get("pnl", 0.0))
                n += 1; tot += pnl
                if pnl > 0:
                    w += 1; gp += pnl
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


def _agg(rows):
    """(pnl の list) → 件数/勝率/PF/損益/平均 の表示文字列タプル。"""
    n = len(rows)
    if n == 0:
        return (0, "—", "—", "—", "—")
    wins = [p for p in rows if p > 0]
    gp = sum(wins); gl = -sum(p for p in rows if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    tot = sum(rows)
    return (n, f"{len(wins)/n*100:.0f}%", pf_s, f"{tot:+,.0f}", f"{tot/n:+,.0f}")


def main():
    st = nikkei_state_series(args.years)
    if st is None:
        print("[ERROR] 日経データ取得失敗"); return

    bases = ([b.strip() for b in args.bases.split(",")] if args.bases else _detect_bases())
    if not bases:
        print("[なし] 基準日CSVが見つかりません。先に scan_walkforward --as-of を回してください。")
        return

    mode = "aggressive" if args.aggressive else "conservative"
    direction = "ショート" if args.short else "ロング"
    if args.fade:
        direction += "→逆(fade)"
    if args.losers:
        direction += "・損益ワースト選定"
    if args.mirror:
        direction += "→鏡写し(mirror)"
    print(f"ローリング再選定OOS [{direction}] / mode={mode} / 再選定間隔{args.interval_months}ヶ月 "
          f"/ 価格{args.min_price:.0f}-{args.max_price:.0f} / top{args.per_strategy}")
    print(f"コスト: 手数料片道{ble.FEE_PCT_ONE_WAY*100:.2f}% / "
          f"スリッページ{ble.SLIPPAGE_STOP_PCT*100:.2f}%")
    print(f"基準月: {', '.join(bases)}\n")

    today = ble._TODAY
    TR: list = []          # 全OOSトレード: {"pnl","state","month","bt"}
    per_base: list = []    # (base, n, pnl)

    for base in bases:
        bd = datetime.strptime(base, "%Y-%m-%d").date()
        oos_end = bd + timedelta(days=int(args.interval_months * 30.44))
        wl = _select_watchlist(base)
        if not wl:
            print(f"  {base}: 選定0件 (CSVなし/条件外) → スキップ")
            continue
        warm = bd - timedelta(days=760)
        bt_days = (today - warm).days
        b_n = 0; b_pnl = 0.0
        for (sym, strat), name in wl.items():
            mod = mod_for(strat)
            if mod is None:
                continue
            params = mod.STRATEGY_PARAMS.get(strat)
            if not params:
                continue
            cf, em, sm, tm = params
            df = ble.fetch(sym, bt_days, min_start_date=warm)
            if df is None:
                continue
            df = df[df.index <= pd.Timestamp(oos_end)]
            if len(df) < 210:
                continue
            etype = getattr(mod, "ENTRY_TYPE", "stop")
            if args.fade:   # シグナルの逆: 買い戦略→売り / 売り戦略→買い
                etype = "stop_sell" if etype == "stop" else "stop"
            res = ble.run_limit_backtest(sym, name, df, cf, em, sm, tm, bt_days, strat,
                                         entry_type=etype,
                                         max_hold=(args.max_hold if args.max_hold > 0 else None))
            if not res:
                continue
            full = res["trade_log"]
            for t in full:
                if t.get("reason") in ("発注中", "保有中") or t.get("signal_dt") is None:
                    continue
                sd = t["signal_dt"].date() if hasattr(t["signal_dt"], "date") else t["signal_dt"]
                # 基準月以降(=OOS)のシグナルのみ採用
                if not (bd <= sd <= oos_end):
                    continue
                state = st.asof(pd.Timestamp(sd))
                bt = asof_bt(full, mod, sd)   # 当時BTスコア(先読みなし)
                if args.mirror:
                    # 同じ約定で逆サイド(鏡写し): 損益を符号反転、手数料は往復2回分
                    _pnl = -float(t["pnl"]) - 2.0 * float(t.get("fee", 0) or 0)
                else:
                    _pnl = float(t["pnl"])
                TR.append({"pnl": _pnl,
                           "state": state if (state and state != "?") else None,
                           "month": pd.Timestamp(sd).strftime("%Y-%m"),
                           "bt": bt})
                b_n += 1; b_pnl += _pnl
        per_base.append((base, b_n, b_pnl))
        print(f"  {base}→+{args.interval_months}ヶ月: 選定{len(wl)}件 / OOS取引{b_n} / 損益{b_pnl:+,.0f}")

    # ── BTフィルタ(状態別/月別に適用) ──
    def _pnls(rows):
        return [r["pnl"] for r in rows]

    def _keep(r):
        if args.min_bt > 0 and (r["bt"] is None or r["bt"] < args.min_bt):
            return False
        if args.max_bt > 0 and (r["bt"] is None or r["bt"] >= args.max_bt):
            return False
        return True
    filt = [r for r in TR if _keep(r)]
    _nb = []
    if args.min_bt > 0:
        _nb.append(f"BT≥{args.min_bt:.0f}")
    if args.max_bt > 0:
        _nb.append(f"BT<{args.max_bt:.0f}")
    bt_note = f" ({'かつ'.join(_nb)}のみ)" if _nb else ""

    # ── 状態別集計 ──
    print(f"\n【状態別{bt_note}】")
    print(f"{'状態':<14}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}{'平均/件':>11}")
    print("-" * 62)
    for k in ["up", "wait", "range", "down"]:
        n, wr, pf_s, tot, avg = _agg(_pnls([r for r in filt if r["state"] == k]))
        print(f"{LABELS[k]:<14}{n:>6}{wr:>7}{pf_s:>7}{tot:>15}{avg:>11}")
    print("-" * 62)
    print("これは look-ahead を排除した OOS 成績(各時点で選定→直後だけ運用)。")

    # ── BT帯別集計(常に全トレードで表示) ──
    print(f"\n【BTスコア帯別(当時BT・先読みなし)】")
    print(f"{'BT帯':<14}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}{'平均/件':>11}")
    print("-" * 62)
    _bt_bands = [("BT≥70", 70, 1e9), ("BT60-69", 60, 70), ("BT40-59", 40, 60),
                 ("BT<40", -1e9, 40), ("スコアなし", None, None)]
    for lbl, lo, hi in _bt_bands:
        if lo is None:
            rows = [r for r in TR if r["bt"] is None]
        else:
            rows = [r for r in TR if r["bt"] is not None and lo <= r["bt"] < hi]
        n, wr, pf_s, tot, avg = _agg(_pnls(rows))
        print(f"{lbl:<14}{n:>6}{wr:>7}{pf_s:>7}{tot:>15}{avg:>11}")
    print("-" * 62)
    print("↑ BT≥70/≥60 だけOOSでプラスなら『高BTに絞れば本物』。全帯マイナスなら選定自体が過学習。")

    if args.monthly:
        by_month: dict = defaultdict(list)
        for r in filt:
            by_month[r["month"]].append(r["pnl"])
        print(f"\n【月別{bt_note}】")
        print(f"{'月':<9}{'取引':>6}{'勝率':>7}{'PF':>7}{'損益':>15}")
        print("-" * 46)
        for mk in sorted(by_month):
            pl = by_month[mk]
            n = len(pl)
            wins = [p for p in pl if p > 0]
            gp = sum(wins); gl = -sum(p for p in pl if p <= 0)
            pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
            pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
            print(f"{mk:<9}{n:>6}{len(wins)/n*100:>6.0f}%{pf_s:>7}{sum(pl):>+15,.0f}")
        print("-" * 46)
        print("↑ 各月の『本物のOOS』損益。レポート月別(現WATCHLISTを過去に当てた値=汚染)と")
        print("  比べると、holdout短設定の in-sample 分がどれだけ数字を盛っていたかが分かる。")

    if args.html:
        _write_html(filt, TR, per_base, st, direction, bt_note)


_REG_LBL = {"up": ("🟢上げ", "#4ade80"), "wait": ("🟡横ばい待ち", "#fbbf24"),
            "range": ("🟠真横ばい", "#fb923c"), "down": ("🔴下げ", "#f87171"),
            "?": ("―", "#64748b")}


def _write_html(filt, TR, per_base, st, direction, bt_note):
    """ローリングOOSの成績を損益タブ風HTMLで出力(大局レジーム付き月別グリッド)。"""
    def _pf(g, l):
        return "∞" if l <= 0 and g > 0 else (f"{g/l:.2f}" if l > 0 else "—")

    def _agg_row(rows):
        n = len(rows)
        if n == 0:
            return 0, 0.0, "—", 0.0, 0.0
        w = sum(1 for p in rows if p > 0)
        gp = sum(p for p in rows if p > 0); gl = -sum(p for p in rows if p <= 0)
        return n, w / n * 100, _pf(gp, gl), gp, gl

    # 月末レジーム(st から)
    def _month_reg(mk):
        try:
            end = pd.Timestamp(mk + "-28") + pd.offsets.MonthEnd(0)
            return st.asof(end)
        except Exception:
            return "?"

    pnls = [r["pnl"] for r in filt]
    n, wr, pf_s, gp, gl = _agg_row(pnls)
    tot = gp - gl
    tcol = "#4ade80" if tot >= 0 else "#f87171"

    def _card(l, v, c="#e2e8f0"):
        return (f'<div class="card"><div class="cl">{l}</div>'
                f'<div class="cv" style="color:{c}">{v}</div></div>')
    cards = "".join([
        _card("総取引", f"{n:,}件"), _card("勝率", f"{wr:.0f}%"),
        _card("PF", pf_s, "#4ade80" if pf_s != "—" and pf_s != "∞" and float(pf_s) >= 1 else "#e2e8f0"),
        _card("利益", f"+{gp:,.0f}", "#4ade80"), _card("損失", f"-{gl:,.0f}", "#f87171"),
        _card("合計損益", f"{tot:+,.0f}円", tcol)])

    # 状態別
    srows = ""
    for k in ["up", "wait", "range", "down"]:
        rr = [r["pnl"] for r in filt if r["state"] == k]
        sn, swr, spf, sgp, sgl = _agg_row(rr)
        st_tot = sgp - sgl
        lbl, col = _REG_LBL[k]
        c = "#4ade80" if st_tot >= 0 else "#f87171"
        srows += (f'<tr><td style="color:{col};font-weight:700">{lbl}</td>'
                  f'<td class="r">{sn}</td><td class="r">{swr:.0f}%</td><td class="r">{spf}</td>'
                  f'<td class="r" style="color:{c};font-weight:700">{st_tot:+,.0f}</td></tr>')

    # BT帯別(TR 全体)
    brows = ""
    for lbl, lo, hi in [("BT≥70", 70, 1e9), ("BT60-69", 60, 70), ("BT40-59", 40, 60),
                        ("BT<40", -1e9, 40), ("スコアなし", None, None)]:
        if lo is None:
            rr = [r["pnl"] for r in TR if r["bt"] is None]
        else:
            rr = [r["pnl"] for r in TR if r["bt"] is not None and lo <= r["bt"] < hi]
        bn, bwr, bpf, bgp, bgl = _agg_row(rr)
        bt_tot = bgp - bgl
        c = "#4ade80" if bt_tot >= 0 else "#f87171"
        brows += (f'<tr><td>{lbl}</td><td class="r">{bn}</td><td class="r">{bwr:.0f}%</td>'
                  f'<td class="r">{bpf}</td><td class="r" style="color:{c};font-weight:700">{bt_tot:+,.0f}</td></tr>')

    # 月別(大局レジーム付き)
    from collections import defaultdict as _dd
    bym = _dd(list)
    for r in filt:
        bym[r["month"]].append(r["pnl"])
    mrows = ""
    for mk in sorted(bym, reverse=True):
        pl = bym[mk]
        mn, mwr, mpf, mgp, mgl = _agg_row(pl)
        m_tot = mgp - mgl
        rlbl, rcol = _REG_LBL.get(_month_reg(mk), ("―", "#64748b"))
        c = "#4ade80" if m_tot >= 0 else "#f87171"
        mrows += (f'<tr><td><b>{mk}</b></td>'
                  f'<td style="text-align:center;color:{rcol};font-weight:700">{rlbl}</td>'
                  f'<td class="r">{mn}</td><td class="r">{mwr:.0f}%</td>'
                  f'<td class="r" style="color:#4ade80">+{mgp:,.0f}</td>'
                  f'<td class="r" style="color:#f87171">-{mgl:,.0f}</td>'
                  f'<td class="r" style="color:{c};font-weight:700">{m_tot:+,.0f}円</td></tr>')

    # 基準月別
    prows = "".join(
        f'<tr><td>{b}</td><td class="r">{c}件</td>'
        f'<td class="r" style="color:{"#4ade80" if p>=0 else "#f87171"};font-weight:700">{p:+,.0f}円</td></tr>'
        for b, c, p in per_base)

    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>ローリングOOS {direction}</title><style>
body{{background:#0a0e1a;color:#e2e8f0;font-family:'Segoe UI',sans-serif;margin:0;padding:16px;font-size:13px}}
h1{{font-size:1.1rem;margin:0 0 4px}} .sub{{color:#94a3b8;font-size:0.78rem;margin-bottom:12px}}
.cards{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}}
.card{{background:#111827;border:1px solid #1e293b;border-radius:8px;padding:7px 13px;min-width:90px}}
.cl{{color:#94a3b8;font-size:0.7rem}} .cv{{font-size:1.15rem;font-weight:700}}
h2{{font-size:0.9rem;margin:16px 0 6px;border-left:3px solid #3b82f6;padding-left:8px}}
table{{border-collapse:collapse;font-size:0.78rem;margin-bottom:4px}}
th,td{{padding:3px 10px;border-bottom:1px solid #16202f;white-space:nowrap}}
th{{color:#94a3b8;background:#0d1424;text-align:left}} td.r,th.r{{text-align:right}}
</style></head><body>
<h1>ローリング再選定OOS [{direction}]{bt_note}</h1>
<div class="sub">6ヶ月ごとにas-of選定→直後を運用した"汚染なし"の成績。大局レジーム=各月末のER+MA200傾き判定(先読みなし)。</div>
<div class="cards">{cards}</div>
<h2>月別損益（大局レジーム付き）</h2>
<table><thead><tr><th>月</th><th style="text-align:center">大局<br>レジーム</th><th class="r">件数</th>
<th class="r">勝率</th><th class="r">利益</th><th class="r">損失</th><th class="r">損益合計</th></tr></thead>
<tbody>{mrows}</tbody></table>
<h2>大局レジーム状態別</h2>
<table><thead><tr><th>状態</th><th class="r">取引</th><th class="r">勝率</th><th class="r">PF</th><th class="r">損益</th></tr></thead>
<tbody>{srows}</tbody></table>
<h2>BTスコア帯別（当時BT・先読みなし）</h2>
<table><thead><tr><th>BT帯</th><th class="r">取引</th><th class="r">勝率</th><th class="r">PF</th><th class="r">損益</th></tr></thead>
<tbody>{brows}</tbody></table>
<h2>基準月別</h2>
<table><thead><tr><th>基準月</th><th class="r">OOS取引</th><th class="r">損益</th></tr></thead>
<tbody>{prows}</tbody></table>
</body></html>"""
    fn = "regime_perf_oos.html"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML出力: {fn}")
    try:
        from _open_html import open_html
        open_html(fn)
    except Exception:
        try:
            import webbrowser
            webbrowser.open(fn)
        except Exception:
            pass


if __name__ == "__main__":
    main()
