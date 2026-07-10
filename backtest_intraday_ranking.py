"""
backtest_intraday_ranking.py ― 「その日の値上がり/値下がりランキング」デイトレ検証
==================================================================================

ユーザー案の検証:
  「9:30 時点の値上がり率(値下がり率)ランキング上位の銘柄に投資し、
   その日のうちに決済(ロングなら売り / ショートなら買い戻し)したら儲かるか？」

過去のザラ場ランキングは kabu には保存が無いが、ローカルの5分足
(data/minute_5m/, daytrade_data.load_intraday) があれば **各日の任意時刻の
ランキングを再構築できる**。それを使って以下4方向を一度に測る:

  LONG_GAINERS   : 値上がり率トップN を 9:30 に買い → 引け(または指定時刻)で売り
  SHORT_GAINERS  : 値上がり率トップN を 9:30 に空売り → 引けで買い戻し
  LONG_LOSERS    : 値下がり率トップN を 9:30 に買い(リバウンド) → 引けで売り
  SHORT_LOSERS   : 値下がり率トップN を 9:30 に空売り → 引けで買い戻し

ランキングの基準: 前日終値比の騰落率 (SBI等の「本日の値上がり率」と同じ)。
  intraday_ret = price(9:30) / prev_close - 1

【重要な限界】
  - ローカル5分足の期間ぶんしか検証できない(yfinance 5分足は約60日)。
    → サンプルが小さく1相場環境のみ。過剰最適化に注意(結論は暫定)。
  - ランキング上位はボラが高く、成行のスリッページが大きい。--slippage で明示的に
    モデル化する(既定 片道0.3%)。ここを甘くすると数字が実態より良く出る。
  - 予算フィルタ(--budget / --max-price)で「100株買える銘柄」に絞れる。
    値上がりトップは高株価(半導体等)が多く、実際には買えない銘柄が並ぶため。

使い方:
  python backtest_intraday_ranking.py                       # ローカル全銘柄, top5, 引け決済
  python backtest_intraday_ranking.py --top-n 3 --budget 600000
  python backtest_intraday_ranking.py --rank-time 09:30 --exit-time 14:55
  python backtest_intraday_ranking.py --limit 300           # 先頭300銘柄(高速デバッグ)
  python backtest_intraday_ranking.py --slippage 0.5        # スリッページ片道0.5%で厳しめ
  python backtest_intraday_ranking.py --self-test           # 合成データでロジック検証
"""
from __future__ import annotations

import argparse
import html as _html
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

JST = timezone(timedelta(hours=9))

DIRECTIONS = ("LONG_GAINERS", "SHORT_GAINERS", "LONG_LOSERS", "SHORT_LOSERS")
DIR_LABELS = {
    "LONG_GAINERS":  "値上がりTOP買い",
    "SHORT_GAINERS": "値上がりTOP空売り",
    "LONG_LOSERS":   "値下がりTOP買い",
    "SHORT_LOSERS":  "値下がりTOP空売り",
}

# コストモデル(既定)。ランキング銘柄は流動性・ボラが激しいので片道0.3%を既定に。
FEE_ONE_WAY = 0.001          # 手数料 片道0.1%(往復0.2%)
DEFAULT_SLIP_ONE_WAY = 0.003  # スリッページ 片道0.3%(往復0.6%)
FIXED_QTY = 100


def _hhmm_to_offset(hhmm: str) -> pd.Timedelta:
    h, m = hhmm.split(":")
    return pd.Timedelta(hours=int(h), minutes=int(m))


def daily_points(df: pd.DataFrame, rank_off: pd.Timedelta,
                 exit_off: pd.Timedelta | None) -> dict:
    """1銘柄の5分足から、各日の (prev_close, p_rank, p_exit) を抽出。

    p_rank: rank時刻以降の最初のバーの始値(=そこで成行約定する想定)
    p_exit: exit時刻以降の最初のバーの始値。exit_off=None なら当日最終バーの終値。
    prev_close: 前営業日(データ上の前日)の最終バーの終値。
    """
    if df is None or df.empty:
        return {}
    d = df.copy()
    dates = pd.Index(d.index).normalize()
    d = d.assign(_date=dates)
    uniq = sorted(pd.unique(dates))
    out = {}
    for i, day_ts in enumerate(uniq):
        if i == 0:
            continue  # 前日終値が無い初日はスキップ
        day = d[d["_date"] == day_ts]
        if day.empty:
            continue
        rt = day_ts + rank_off
        after = day[day.index >= rt]
        if after.empty:
            continue
        p_rank = float(after.iloc[0]["open"])
        if p_rank <= 0:
            continue
        if exit_off is not None:
            et = day_ts + exit_off
            ex = day[day.index >= et]
            p_exit = float(ex.iloc[0]["open"]) if not ex.empty else float(day.iloc[-1]["close"])
        else:
            p_exit = float(day.iloc[-1]["close"])
        prev_day = d[d["_date"] == uniq[i - 1]]
        if prev_day.empty:
            continue
        prev_close = float(prev_day.iloc[-1]["close"])
        if prev_close <= 0:
            continue
        out[pd.Timestamp(day_ts)] = {
            "prev_close": prev_close, "p_rank": p_rank, "p_exit": p_exit,
        }
    return out


class Agg:
    """1方向(戦略)の損益集計。"""
    __slots__ = ("rets", "yens", "wins")

    def __init__(self):
        self.rets: list[float] = []
        self.yens: list[float] = []
        self.wins = 0

    def add(self, net_ret: float, p_entry: float):
        self.rets.append(net_ret)
        self.yens.append(net_ret * p_entry * FIXED_QTY)
        if net_ret > 0:
            self.wins += 1

    def summary(self) -> dict:
        n = len(self.rets)
        if n == 0:
            return {"n": 0, "wr": 0.0, "avg": 0.0, "total_yen": 0.0, "avg_yen": 0.0}
        total_yen = sum(self.yens)
        return {
            "n": n,
            "wr": self.wins / n * 100,
            "avg": sum(self.rets) / n * 100,   # 平均リターン%
            "total_yen": total_yen,
            "avg_yen": total_yen / n,
        }


def run(points_by_symbol: dict, top_n: int, slip_one_way: float,
        max_price: float | None, min_price: float, min_move: float = 0.0) -> dict:
    """クロスセクションにランキングを組んで4方向を集計。
    min_move: 9:30騰落率の絶対値(比率)がこれ未満の銘柄を除外。"""
    # date -> {sym: points}
    by_date: dict = defaultdict(dict)
    for sym, pts in points_by_symbol.items():
        for d, p in pts.items():
            by_date[d][sym] = p

    round_cost = (slip_one_way + FEE_ONE_WAY) * 2   # 往復コスト(比率)
    aggs = {k: Agg() for k in
            ("LONG_GAINERS", "SHORT_GAINERS", "LONG_LOSERS", "SHORT_LOSERS")}
    n_days = 0

    for d, syms in sorted(by_date.items()):
        ranked = []
        for sym, p in syms.items():
            pr = p["p_rank"]
            if pr < min_price:
                continue
            if max_price is not None and pr > max_price:
                continue
            ret = pr / p["prev_close"] - 1
            if min_move and abs(ret) < min_move:
                continue
            ranked.append((ret, sym, p))
        if len(ranked) < top_n * 2:
            continue  # ランキングを作るに足りない
        n_days += 1
        ranked.sort(key=lambda x: x[0], reverse=True)
        gainers = ranked[:top_n]
        losers = ranked[-top_n:]

        for _ret, _sym, p in gainers:
            r = p["p_exit"] / p["p_rank"] - 1        # ロング素リターン
            aggs["LONG_GAINERS"].add(r - round_cost, p["p_rank"])
            aggs["SHORT_GAINERS"].add(-r - round_cost, p["p_rank"])
        for _ret, _sym, p in losers:
            r = p["p_exit"] / p["p_rank"] - 1
            aggs["LONG_LOSERS"].add(r - round_cost, p["p_rank"])
            aggs["SHORT_LOSERS"].add(-r - round_cost, p["p_rank"])

    return {"n_days": n_days, "aggs": {k: v.summary() for k, v in aggs.items()}}


# ══════════════════════════════════════════════════════════════════
# 利確目標(TP)・損切り(SL)でザラ場決済する版
# ══════════════════════════════════════════════════════════════════
def _sim_tpsl_exit(rec, ei, entry_price, is_long, tp, sl):
    """entry_price から TP/SL を建て、entry バー以降のザラ場でどちらか先に
    タッチした方で決済。どちらも当たらなければ引け(last_close)。
    同一バーで両方タッチは保守的に SL 優先。返り値: (exit_price, 'tp'/'sl'/'close')。"""
    highs = rec["highs"]; lows = rec["lows"]; n = len(highs)
    if is_long:
        tp_p = entry_price * (1 + tp)
        sl_p = entry_price * (1 - sl)
        for j in range(ei, n):
            hit_sl = lows[j] <= sl_p
            hit_tp = highs[j] >= tp_p
            if hit_sl:            # 同一バー両方でも SL 優先(保守)
                return sl_p, "sl"
            if hit_tp:
                return tp_p, "tp"
    else:
        tp_p = entry_price * (1 - tp)    # ショートの利確は下
        sl_p = entry_price * (1 + sl)    # ショートの損切りは上
        for j in range(ei, n):
            hit_sl = highs[j] >= sl_p
            hit_tp = lows[j] <= tp_p
            if hit_sl:
                return sl_p, "sl"
            if hit_tp:
                return tp_p, "tp"
    return rec["last_close"], "close"


def run_tpsl(idx, entry_sec, top_n, slip, max_price, min_price, tp, sl,
             rank_basis="prevclose"):
    """ランキング → TP/SL(ザラ場)で4方向を集計。
    rank_basis: "prevclose"=前日終値比 / "open"=今日の寄りからの初動で順位付け。
    コスト: エントリー=slip+fee。決済= SLはslip+fee / TP・引けはfeeのみ。
    tp, sl は比率(例 0.03=3%)。"""
    fee = FEE_ONE_WAY
    entry_cost = slip + fee
    mx = np.inf if max_price is None else float(max_price)
    aggs = {k: Agg() for k in DIRECTIONS}
    reasons = {k: {"tp": 0, "sl": 0, "close": 0} for k in DIRECTIONS}
    n_days = 0
    by_date = defaultdict(list)
    for sym, recs in idx.items():
        for r in recs:
            pc = r["prev_close"]
            if pc is None or pc <= 0:
                continue
            tods = r["tods"]
            ei = int(np.searchsorted(tods, entry_sec, side="left"))
            if ei >= len(tods):
                continue
            ep = float(r["opens"][ei])
            if ep <= 0 or ep < min_price or ep > mx:
                continue
            base = r["day_open"] if rank_basis == "open" else pc
            if base <= 0:
                continue
            by_date[r["date"]].append((ep / base - 1, r, ei, ep))

    def _add(sel, long_key, short_key):
        for _ret, rec, ei, ep in sel:
            xp, rs = _sim_tpsl_exit(rec, ei, ep, True, tp, sl)
            ec = (slip + fee) if rs == "sl" else fee
            aggs[long_key].add((xp / ep - 1) - entry_cost - ec, ep)
            reasons[long_key][rs] += 1
            xp2, rs2 = _sim_tpsl_exit(rec, ei, ep, False, tp, sl)
            ec2 = (slip + fee) if rs2 == "sl" else fee
            aggs[short_key].add(-(xp2 / ep - 1) - entry_cost - ec2, ep)
            reasons[short_key][rs2] += 1

    for d, rows in sorted(by_date.items()):
        if len(rows) < top_n * 2:
            continue
        n_days += 1
        rows.sort(key=lambda x: x[0], reverse=True)
        _add(rows[:top_n], "LONG_GAINERS", "SHORT_GAINERS")
        _add(rows[-top_n:], "LONG_LOSERS", "SHORT_LOSERS")
    return {"n_days": n_days, "aggs": {k: v.summary() for k, v in aggs.items()},
            "reasons": reasons}


# ══════════════════════════════════════════════════════════════════
# 高速インデックス (時間帯スイープ用) ― 銘柄ごとに日次レコードを1回だけ構築
# ══════════════════════════════════════════════════════════════════
def index_data(data: dict) -> dict:
    """{sym: df} → {sym: [ {date, prev_close, tods(sec), opens, last_close} ]}。

    tods は各バーの「その日の秒(9:00=32400)」の昇順配列。searchsorted で任意
    時刻の価格を高速に引ける。prev_close は前営業日の最終バー終値。
    """
    idx = {}
    for sym, df in data.items():
        if df is None or df.empty:
            continue
        ts = df.index
        # groupby は遅いので numpy スライスで日境界を切る(indexは時刻昇順の前提)
        days = ts.normalize().values                         # datetime64[ns] (00:00)
        tod = (ts.hour * 3600 + ts.minute * 60 + ts.second).to_numpy().astype(int)
        opens = df["open"].to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        highs = df["high"].to_numpy(dtype=float) if "high" in df.columns else opens
        lows = df["low"].to_numpy(dtype=float) if "low" in df.columns else opens
        n = len(days)
        # 日が変わる位置
        bounds = np.flatnonzero(days[1:] != days[:-1]) + 1
        starts = np.concatenate(([0], bounds))
        ends = np.concatenate((bounds, [n]))
        recs = []
        prev_close = None
        for a, b_ in zip(starts, ends):
            last_close = float(closes[b_ - 1])
            recs.append({
                "date": pd.Timestamp(days[a]),
                "prev_close": prev_close,
                "day_open": float(opens[a]),   # 今日の寄り値(初動の起点)
                "tods": tod[a:b_],
                "opens": opens[a:b_],
                "highs": highs[a:b_],
                "lows": lows[a:b_],
                "last_close": last_close,
            })
            prev_close = last_close
        if recs:
            idx[sym] = recs
    return idx


def points_from_index(idx: dict, entry_sec: int, exit_sec: int | None) -> dict:
    """インデックスから (entry_sec, exit_sec) の points 辞書を高速生成。
    exit_sec=None は当日最終バー終値(引け)。"""
    pts = {}
    for sym, recs in idx.items():
        d = {}
        for r in recs:
            pc = r["prev_close"]
            if pc is None or pc <= 0:
                continue
            tods = r["tods"]
            ei = int(np.searchsorted(tods, entry_sec, side="left"))
            if ei >= len(tods):
                continue
            p_rank = float(r["opens"][ei])
            if p_rank <= 0:
                continue
            if exit_sec is None:
                p_exit = r["last_close"]
            else:
                xi = int(np.searchsorted(tods, exit_sec, side="left"))
                p_exit = float(r["opens"][xi]) if xi < len(tods) else r["last_close"]
            d[r["date"]] = {"prev_close": pc, "p_rank": p_rank, "p_exit": p_exit}
        if d:
            pts[sym] = d
    return pts


def _sec(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


def sweep(idx: dict, entry_list: list, exit_list: list, top_n: int,
          slip: float, max_price, min_price: float) -> list:
    """entry時刻 × exit時刻 のグリッドで4方向の平均%を計算。
    返り値: [ {entry, cells:{exit_label: {dir: avg%|None}}} ]"""
    rows = []
    for e in entry_list:
        e_sec = _sec(e)
        row = {"entry": e, "cells": {}}
        for x in exit_list:
            if x == "close":
                x_sec = None
            else:
                x_sec = _sec(x)
                if x_sec <= e_sec:
                    row["cells"][x] = None
                    continue
            pts = points_from_index(idx, e_sec, x_sec)
            res = run(pts, top_n, slip, max_price, min_price)
            row["cells"][x] = {k: res["aggs"][k]["avg"] for k in DIRECTIONS}
        rows.append(row)
    return rows


def sweep_fast(idx: dict, entry_list: list, exit_list: list, top_n: int,
               slip: float, max_price, min_price: float, min_move: float = 0.0) -> list:
    """高速スイープ。生ループは1回だけ(全時刻の価格を先に抽出)→あとはベクトル化。

    従来の sweep() は (entry×exit) combo ごとに全銘柄×日をループして遅かった。
    ここでは各銘柄×日について「必要な全 entry時刻・exit時刻の価格」を一度に取り、
    日付ごとの断面行列を作ってから、combo をベクトル演算で処理する。
    """
    entry_secs = [_sec(e) for e in entry_list]
    exit_secs = [None if x == "close" else _sec(x) for x in exit_list]
    n_e, n_x = len(entry_secs), len(exit_secs)
    round_cost = (slip + FEE_ONE_WAY) * 2
    mx = np.inf if max_price is None else float(max_price)

    # ── 断面データを1パスで構築 ──
    date_pc: dict = defaultdict(list)
    date_ent: dict = defaultdict(list)
    date_exi: dict = defaultdict(list)
    for sym, recs in idx.items():
        for r in recs:
            pc = r["prev_close"]
            if pc is None or pc <= 0:
                continue
            tods = r["tods"]; opens = r["opens"]; lastc = r["last_close"]
            m = len(tods)
            ei = np.searchsorted(tods, entry_secs, side="left")
            ent = [float(opens[j]) if j < m else 0.0 for j in ei]
            exi = []
            for xs in exit_secs:
                if xs is None:
                    exi.append(lastc)
                else:
                    j = int(np.searchsorted(tods, xs, side="left"))
                    exi.append(float(opens[j]) if j < m else lastc)
            d = r["date"]
            date_pc[d].append(pc)
            date_ent[d].append(ent)
            date_exi[d].append(exi)

    # ── combo ごとの集計器(sum_ret / count) ──
    dirs = DIRECTIONS
    sums = {k: np.zeros((n_e, n_x)) for k in dirs}
    cnts = {k: np.zeros((n_e, n_x), dtype=int) for k in dirs}

    for d in date_pc:
        pc = np.asarray(date_pc[d])
        ent = np.asarray(date_ent[d])          # (nsym, n_e)
        exi = np.asarray(date_exi[d])          # (nsym, n_x)
        for a in range(n_e):
            ep = ent[:, a]
            valid = (ep > 0) & (pc > 0) & (ep >= min_price) & (ep <= mx)
            if min_move:
                with np.errstate(divide="ignore", invalid="ignore"):
                    mv = np.abs(ep / pc - 1)
                valid = valid & (mv >= min_move)
            iv = np.where(valid)[0]
            if len(iv) < top_n * 2:
                continue
            rr = ep[iv] / pc[iv] - 1
            order = iv[np.argsort(-rr)]
            gain = order[:top_n]
            lose = order[-top_n:]
            for b in range(n_x):
                if exit_secs[b] is not None and exit_secs[b] <= entry_secs[a]:
                    continue
                xp = exi[:, b]
                rg = xp[gain] / ep[gain] - 1     # gainers 素リターン(ロング視点)
                rl = xp[lose] / ep[lose] - 1     # losers 素リターン(ロング視点)
                sums["LONG_GAINERS"][a, b] += np.sum(rg - round_cost)
                sums["SHORT_GAINERS"][a, b] += np.sum(-rg - round_cost)
                sums["LONG_LOSERS"][a, b] += np.sum(rl - round_cost)
                sums["SHORT_LOSERS"][a, b] += np.sum(-rl - round_cost)
                for k in dirs:
                    cnts[k][a, b] += top_n

    rows = []
    for a, e in enumerate(entry_list):
        row = {"entry": e, "cells": {}}
        for b, x in enumerate(exit_list):
            if exit_secs[b] is not None and exit_secs[b] <= entry_secs[a]:
                row["cells"][x] = None
                continue
            cell = {}
            for k in dirs:
                n = cnts[k][a, b]
                cell[k] = (sums[k][a, b] / n * 100) if n else None
            row["cells"][x] = cell
        rows.append(row)
    return rows


def data_health(idx: dict) -> dict:
    """データ健全性の要約。バー数/日・期間・薄い日の検出。"""
    bars = []
    dmin = dmax = None
    total_days = 0
    thin = 0
    for sym, recs in idx.items():
        for r in recs:
            nb = len(r["tods"])
            bars.append(nb)
            total_days += 1
            if nb < 30:           # 通常 ~54本(昼休み除く)。極端に少ない=欠損疑い
                thin += 1
            d = r["date"]
            dmin = d if (dmin is None or d < dmin) else dmin
            dmax = d if (dmax is None or d > dmax) else dmax
    bars_arr = np.array(bars) if bars else np.array([0])
    return {
        "n_symbols": len(idx),
        "date_min": dmin, "date_max": dmax,
        "sym_day_records": total_days,
        "bars_mean": float(bars_arr.mean()),
        "bars_min": int(bars_arr.min()),
        "bars_max": int(bars_arr.max()),
        "thin_days": thin,
    }


def sample_rankings(idx: dict, top_n: int, entry_sec: int,
                    max_price, min_price: float, n_dates: int = 3) -> list:
    """直近 n_dates 日について、実際のランキング上位/下位の生値を返す(データ検証用)。
    各要素: {date, gainers:[...], losers:[...]}。各銘柄 (sym, prev_close, p_rank,
    last_close, ret_rank%, ret_close%)。"""
    pts = points_from_index(idx, entry_sec, None)   # exit=引け
    by_date = defaultdict(list)
    for sym, d in pts.items():
        for dt, p in d.items():
            by_date[dt].append((sym, p))
    out = []
    for dt in sorted(by_date.keys())[-n_dates:]:
        ranked = []
        for sym, p in by_date[dt]:
            pr = p["p_rank"]
            if pr < min_price or (max_price is not None and pr > max_price):
                continue
            ret_rank = pr / p["prev_close"] - 1
            ret_close = p["p_exit"] / pr - 1
            ranked.append((ret_rank, sym, p["prev_close"], pr, p["p_exit"], ret_close))
        if len(ranked) < top_n * 2:
            continue
        ranked.sort(key=lambda x: x[0], reverse=True)
        out.append({
            "date": dt,
            "gainers": ranked[:top_n],
            "losers": list(reversed(ranked[-top_n:])),
        })
    return out


def magnitude_short_gainers(idx: dict, entry_sec: int, exit_sec: int | None,
                            slip: float, min_price: float, max_price,
                            edges=(3, 5, 8, 12, 18, 100)) -> list:
    """値上がり銘柄を「9:30時点の上げ幅(前日終値比%)」帯ごとに空売りした成績。

    top-N ではなく、その帯に入る全銘柄を空売りしたと仮定して反落の傾斜を見る。
    大きく上げた帯ほど反落が強く、コストを超えてプラスになるか？を検証する。
    返り値: [(帯ラベル, 件数, 勝率%, 平均net%, 平均gross%, 合計円)]
    """
    round_cost = (slip + FEE_ONE_WAY) * 2
    pts = points_from_index(idx, entry_sec, exit_sec)
    mx = float("inf") if max_price is None else float(max_price)
    labels = [f"+{edges[i]}〜{edges[i+1]}%" for i in range(len(edges) - 1)]
    labels[-1] = f"+{edges[-2]}%以上"
    agg = [{"n": 0, "w": 0, "net": 0.0, "gross": 0.0, "yen": 0.0} for _ in labels]
    for sym, d in pts.items():
        for dt, p in d.items():
            pr = p["p_rank"]
            if pr < min_price or pr > mx or p["prev_close"] <= 0:
                continue
            ret = (pr / p["prev_close"] - 1) * 100
            if ret < edges[0]:
                continue
            bi = None
            for i in range(len(edges) - 1):
                if edges[i] <= ret < edges[i + 1]:
                    bi = i
                    break
            if bi is None:
                continue
            gross = -(p["p_exit"] / pr - 1)      # 空売り素リターン
            net = gross - round_cost
            a = agg[bi]
            a["n"] += 1
            a["net"] += net
            a["gross"] += gross
            a["yen"] += net * pr * FIXED_QTY
            if net > 0:
                a["w"] += 1
    rows = []
    for lab, a in zip(labels, agg):
        if a["n"] == 0:
            rows.append((lab, 0, 0.0, 0.0, 0.0, 0.0))
        else:
            rows.append((lab, a["n"], a["w"] / a["n"] * 100,
                         a["net"] / a["n"] * 100, a["gross"] / a["n"] * 100, a["yen"]))
    return rows


# ══════════════════════════════════════════════════════════════════
# HTML レポート
# ══════════════════════════════════════════════════════════════════
def _cell_color(v):
    """平均%を緑(プラス)/赤(マイナス)の背景色に。強度は絶対値。"""
    if v is None:
        return "background:#1e293b;color:#475569", "―"
    cap = min(abs(v) / 1.5, 1.0)       # ±1.5%で最大濃度
    if v > 0:
        return f"background:rgba(34,197,94,{0.12+0.55*cap});color:#dcfce7", f"{v:+.2f}%"
    if v < 0:
        return f"background:rgba(239,68,68,{0.12+0.55*cap});color:#fee2e2", f"{v:+.2f}%"
    return "background:#334155;color:#cbd5e1", "0.00%"


def build_html(meta: dict, health: dict, samples: list, sweeps: list,
               summary: dict, mag: list | None = None) -> str:
    esc = _html.escape
    css = """
    body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',Meiryo,sans-serif;
         margin:0;padding:24px;line-height:1.5}
    h1{font-size:1.5rem;margin:0 0 4px} h2{font-size:1.15rem;margin:28px 0 10px;
       color:#93c5fd;border-left:4px solid #60a5fa;padding-left:10px}
    .sub{color:#94a3b8;font-size:0.85rem;margin-bottom:8px}
    table{border-collapse:collapse;margin:8px 0 18px;font-size:0.82rem}
    th,td{border:1px solid #334155;padding:5px 9px;text-align:right;white-space:nowrap}
    th{background:#1e293b;color:#cbd5e1} td.l,th.l{text-align:left}
    .note{color:#94a3b8;font-size:0.8rem;margin:4px 0 16px}
    .good{color:#4ade80} .bad{color:#f87171}
    .box{background:#1e293b;border:1px solid #334155;border-radius:8px;
         padding:12px 16px;margin:8px 0;display:inline-block}
    .warn{color:#fbbf24}
    """
    P = [f"<style>{css}</style>",
         f"<h1>その日のランキング上位デイトレ検証</h1>",
         f"<div class='sub'>{esc(meta['subtitle'])}</div>"]

    # ── データ健全性 ──
    P.append("<h2>① データ健全性（本当に合っているかの確認）</h2>")
    P.append("<div class='box'>")
    P.append(f"銘柄数: <b>{health['n_symbols']}</b>　")
    P.append(f"期間: <b>{health['date_min']:%Y-%m-%d}〜{health['date_max']:%Y-%m-%d}</b>　")
    P.append(f"銘柄×日レコード: <b>{health['sym_day_records']:,}</b><br>")
    P.append(f"1日あたりバー数 平均: <b>{health['bars_mean']:.1f}</b> "
             f"(最小{health['bars_min']} / 最大{health['bars_max']})　")
    thin_cls = "warn" if health['thin_days'] > health['sym_day_records'] * 0.1 else ""
    P.append(f"<span class='{thin_cls}'>薄い日(30バー未満): {health['thin_days']:,}</span>")
    P.append("</div>")
    P.append("<div class='note'>※ 東証は 9:00-11:30 / 12:30-15:00 で、5分足は昼休み除き"
             "1日約54本が正常。平均が54前後で薄い日が少なければデータは健全。</div>")

    # ── サンプル日のランキング実値 ──
    P.append("<h2>② サンプル日のランキング実値（前日終値→9:30→引け）</h2>")
    P.append("<div class='note'>実際の銘柄・値段を並べます。知っている日と照合して"
             "データが正しいか目視確認してください。ret(9:30)=前日終値比の騰落率で順位付け、"
             "ret(引け)=9:30から引けまでの実際の値動き(=このトレードの素リターン)。</div>")
    for s in samples:
        P.append(f"<h3 style='margin:10px 0 4px;color:#e2e8f0;font-size:0.95rem'>"
                 f"{s['date']:%Y-%m-%d (%a)}</h3>")
        for tag, rows in (("値上がりTOP", s["gainers"]), ("値下がりTOP", s["losers"])):
            P.append(f"<table><thead><tr><th class='l'>{tag}</th><th>コード</th>"
                     f"<th>前日終値</th><th>9:30値</th><th>引け値</th>"
                     f"<th>ret(9:30)</th><th>ret(引け)</th></tr></thead><tbody>")
            for i, (rr, sym, pc, pr, px, rc) in enumerate(rows, 1):
                rc_cls = "good" if rc > 0 else "bad"
                P.append(f"<tr><td class='l'>{i}</td><td>{esc(str(sym))}</td>"
                         f"<td>{pc:,.1f}</td><td>{pr:,.1f}</td><td>{px:,.1f}</td>"
                         f"<td>{rr*100:+.2f}%</td>"
                         f"<td class='{rc_cls}'>{rc*100:+.2f}%</td></tr>")
            P.append("</tbody></table>")

    # ── 時間帯スイープ ──
    P.append("<h2>③ 時間帯スイープ（エントリー時刻 × 決済時刻 → 平均%）</h2>")
    P.append("<div class='note'>各セル=100株1トレードあたり平均リターン%(スリッページ・手数料込み)。"
             "緑=プラス / 赤=マイナス。どの時間の組み合わせでもプラスに出る所があるかを見る。"
             "エッジがあれば緑の塊が出る。全面赤ならエッジ無し。</div>")
    exit_cols = meta["exit_list"]
    for dkey in DIRECTIONS:
        P.append(f"<h3 style='margin:10px 0 4px;font-size:0.95rem'>{DIR_LABELS[dkey]}</h3>")
        P.append("<table><thead><tr><th class='l'>entry\\exit</th>")
        for x in exit_cols:
            P.append(f"<th>{esc(x)}</th>")
        P.append("</tr></thead><tbody>")
        for row in sweeps:
            P.append(f"<tr><th class='l'>{esc(row['entry'])}</th>")
            for x in exit_cols:
                cell = row["cells"].get(x)
                v = cell[dkey] if isinstance(cell, dict) else None
                style, txt = _cell_color(v)
                P.append(f"<td style='{style}'>{txt}</td>")
            P.append("</tr>")
        P.append("</tbody></table>")

    # ── サマリー(既定 9:30→引け) ──
    P.append("<h2>④ サマリー（既定: 9:30エントリー → 引け決済）</h2>")
    P.append(f"<div class='sub'>検証日数 {summary['n_days']}日</div>")
    P.append("<table><thead><tr><th class='l'>戦略</th><th>取引</th><th>勝率</th>"
             "<th>平均%</th><th>平均円</th><th>合計円</th></tr></thead><tbody>")
    for k in DIRECTIONS:
        s = summary["aggs"][k]
        tot_cls = "good" if s["total_yen"] > 0 else "bad"
        P.append(f"<tr><td class='l'>{DIR_LABELS[k]}</td><td>{s['n']:,}</td>"
                 f"<td>{s['wr']:.1f}%</td><td>{s['avg']:+.2f}%</td>"
                 f"<td>{s['avg_yen']:+,.0f}</td>"
                 f"<td class='{tot_cls}'>{s['total_yen']:+,.0f}</td></tr>")
    P.append("</tbody></table>")

    # ── ⑤ 値上がりトップ空売り: 上げ幅帯別 ──
    if mag:
        P.append("<h2>⑤ 値上がりトップ空売り ― 上げ幅帯別の反落成績</h2>")
        P.append("<div class='note'>9:30時点の上げ幅(前日終値比%)の帯ごとに、その帯の"
                 "全銘柄を空売りして決済した成績。大きく上げた帯ほど反落が強いか、"
                 "そして net(コスト後)がプラスに転じる帯があるかを見る。"
                 "gross=コスト前 / net=スリッページ・手数料込み。</div>")
        P.append("<table><thead><tr><th class='l'>上げ幅帯</th><th>件数</th><th>勝率</th>"
                 "<th>平均gross%</th><th>平均net%</th><th>合計円</th></tr></thead><tbody>")
        for lab, n, wr, net, gross, yen in mag:
            ncls = "good" if net > 0 else "bad"
            gcls = "good" if gross > 0 else "bad"
            P.append(f"<tr><td class='l'>{esc(lab)}</td><td>{n:,}</td><td>{wr:.1f}%</td>"
                     f"<td class='{gcls}'>{gross:+.2f}%</td>"
                     f"<td class='{ncls}'>{net:+.2f}%</td><td>{yen:+,.0f}</td></tr>")
        P.append("</tbody></table>")
        P.append("<div class='note'>※ net がプラスの帯があれば「その上げ幅だけ空売り」候補。"
                 "ただし大きく上げた小型株の空売りは踏み上げ(squeeze)・借株難のリスクが高い。"
                 "件数が少ない帯は統計的に不安定。</div>")

    P.append(f"<div class='note'>{esc(meta['footer'])}</div>")
    return "<!doctype html><html><head><meta charset='utf-8'>" \
           "<title>ランキングデイトレ検証</title></head><body>" \
           + "\n".join(P) + "</body></html>"


def _self_test():
    """合成データでロジック(ランキング/リターン/コスト)を検証。"""
    idx = []
    base = pd.Timestamp("2026-07-06 09:00")
    # 2日分 × 3銘柄。各日 09:00〜15:00 の5分足を簡易生成。
    def mkday(day, o930, oclose):
        bars = pd.date_range(day + pd.Timedelta(minutes=30),
                             day + pd.Timedelta(hours=6), freq="5min")
        # 9:30 の始値を o930、最終バー終値を oclose にする直線
        n = len(bars)
        vals = [o930 + (oclose - o930) * i / (n - 1) for i in range(n)]
        return pd.DataFrame({"open": vals, "high": vals, "low": vals,
                             "close": vals, "volume": [1]*n}, index=bars)

    # prev_close 用に前日(7/03)も付ける
    d0 = pd.Timestamp("2026-07-03")
    d1 = pd.Timestamp("2026-07-06")
    data = {}
    # A: 前日終値100 → 9:30=110(+10%,トップ) → 引け121(+10%さらに上げ)
    data["A"] = pd.concat([mkday(d0, 100, 100), mkday(d1, 110, 121)])
    # B: 前日終値100 → 9:30=90(-10%,ボトム) → 引け99(+10%リバウンド)
    data["B"] = pd.concat([mkday(d0, 100, 100), mkday(d1, 90, 99)])
    # C: 前日終値100 → 9:30=100(±0) → 引け100
    data["C"] = pd.concat([mkday(d0, 100, 100), mkday(d1, 100, 100)])

    pts = {s: daily_points(df, pd.Timedelta(minutes=30), None) for s, df in data.items()}
    res = run(pts, top_n=1, slip_one_way=0.0, max_price=None, min_price=0.0)
    a = res["aggs"]
    print("self-test:", res["n_days"], "日")
    # top gainer=A(9:30=110→引け121): LONG_GAINERS ret ≈ +10%(手数料往復0.2%引き)
    lg = a["LONG_GAINERS"]
    ll = a["LONG_LOSERS"]
    print("  LONG_GAINERS avg%=", round(lg["avg"], 2), "(期待≈+9.8)")
    print("  LONG_LOSERS  avg%=", round(ll["avg"], 2), "(期待≈+9.8, B 90→99)")
    ok = abs(lg["avg"] - 9.8) < 0.5 and abs(ll["avg"] - 9.8) < 0.5
    print("  => ", "OK" if ok else "NG")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="その日のランキング上位デイトレ検証")
    ap.add_argument("--rank-time", default="09:30", help="ランキング判定/エントリー時刻 HH:MM")
    ap.add_argument("--exit-time", default="", help="決済時刻 HH:MM。空なら当日最終バー(引け)")
    ap.add_argument("--top-n", type=int, default=5, help="上位/下位 何銘柄を取るか")
    ap.add_argument("--days", type=int, default=60, help="遡る日数(ローカルデータの範囲内)")
    ap.add_argument("--slippage", type=float, default=DEFAULT_SLIP_ONE_WAY*100,
                    help="片道スリッページ%%(既定0.3)")
    ap.add_argument("--budget", type=float, default=None, help="予算(円)。100株買える株価に絞る")
    ap.add_argument("--max-price", type=float, default=None, help="株価上限(円)。--budget と同義")
    ap.add_argument("--min-price", type=float, default=100.0, help="株価下限(円)")
    ap.add_argument("--min-move", type=float, default=0.0,
                    help="9:30騰落率の絶対値がこの%%未満の銘柄をランキングから除外"
                         "(大きく動いた銘柄だけに絞る。例: --min-move 15)")
    ap.add_argument("--limit", type=int, default=None, help="先頭N銘柄だけ(デバッグ)")
    ap.add_argument("--source", default="local", choices=["local", "auto", "yfinance"])
    ap.add_argument("--data-dir", default=None,
                    help="5分足pklのフォルダを明示指定 (未指定なら自動検出: 環境変数"
                         "MINUTE_5M_DIR → data/minute_5m → 隣接daytrading/data/minute_5m)")
    ap.add_argument("--self-test", action="store_true", help="合成データでロジック検証")
    ap.add_argument("--html", action="store_true",
                    help="HTMLレポート(データ健全性/サンプル実値/時間帯スイープ)を出力")
    ap.add_argument("--sweep-entry", default="09:05,09:30,10:00,10:30,11:00,12:30,13:00,14:00",
                    help="スイープのエントリー時刻(カンマ区切り HH:MM)")
    ap.add_argument("--sweep-exit", default="09:30,10:00,11:00,12:30,13:30,14:55,close",
                    help="スイープの決済時刻(カンマ区切り。close=引け)")
    ap.add_argument("--no-browser", action="store_true", help="HTMLを自動で開かない")
    ap.add_argument("--sample-dates", type=int, default=3,
                    help="②サンプル日ランキングに表示する直近日数(既定3)")
    ap.add_argument("--tp", type=float, default=0.0,
                    help="利確目標%%(エントリー値比)。--sl と両方指定でザラ場TP/SL決済に切替")
    ap.add_argument("--sl", type=float, default=0.0,
                    help="損切り%%(エントリー値比)。例: --tp 3 --sl 2 で+3%%利確/-2%%損切り")
    ap.add_argument("--entry-sweep", action="store_true",
                    help="エントリー時刻を --sweep-entry のリストで振って4方向を一覧比較"
                         "(TP/SL指定時はTP/SL決済、なければ引け決済)")
    ap.add_argument("--grid", action="store_true",
                    help="利確(TP)×損切り(SL)の全組み合わせを4方向でマトリクス表示"
                         "(サーフェス全体を見て、プラス領域が本物か偶然かを判定)")
    ap.add_argument("--grid-tp", default="2,3,4,5,6", help="--grid のTP候補(カンマ区切り%%)")
    ap.add_argument("--grid-sl", default="1,1.5,2,3", help="--grid のSL候補(カンマ区切り%%)")
    ap.add_argument("--rank-basis", choices=["prevclose", "open"], default="prevclose",
                    help="順位付けの基準: prevclose=前日終値比(既定) / "
                         "open=今日の寄りからの初動(9:30固定にこだわらず初動の勢いで選ぶ)")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    # データ場所を明示指定されたら daytrade_data のパスを上書き
    if args.data_dir:
        import daytrade_data as _dd
        _dd.DATA_DIR = Path(args.data_dir)

    from daytrade_data import load_intraday_batch, available_local_symbols, DATA_DIR
    print(f"  5分足データ: {DATA_DIR}")

    symbols = available_local_symbols()
    if not symbols:
        print("[ERROR] data/minute_5m にローカル5分足がありません。"
              "download_all_minute.py で取得してください。")
        return 1
    if args.limit:
        symbols = symbols[:args.limit]

    max_price = args.max_price
    if max_price is None and args.budget:
        max_price = args.budget / FIXED_QTY

    rank_off = _hhmm_to_offset(args.rank_time)
    exit_off = _hhmm_to_offset(args.exit_time) if args.exit_time else None
    slip = args.slippage / 100.0

    print("=" * 70)
    print("その日のランキング上位デイトレ検証 (前日終値比の騰落率で順位付け)")
    print(f"  ランキング/エントリー: {args.rank_time}  決済: "
          f"{args.exit_time or '引け(最終バー)'}")
    print(f"  上位/下位: 各{args.top_n}銘柄  スリッページ片道: {args.slippage:.2f}% "
          f"(往復コスト {(slip+FEE_ONE_WAY)*2*100:.2f}%)")
    if max_price:
        print(f"  株価フィルタ: {args.min_price:.0f}〜{max_price:.0f}円 (100株で買える範囲)")
    print(f"  対象銘柄: {len(symbols)}  期間: 直近{args.days}日(ローカル範囲内)")
    print("=" * 70)

    data = load_intraday_batch(symbols, days=args.days, source=args.source)
    print(f"  データ取得: {len(data)}銘柄\n")

    idx = index_data(data)
    entry_sec = _sec(args.rank_time)
    exit_sec = _sec(args.exit_time) if args.exit_time else None
    _tpsl_mode = bool(args.tp and args.sl)

    if _tpsl_mode:
        # 利確目標(TP)・損切り(SL)でザラ場決済
        res = run_tpsl(idx, entry_sec, args.top_n, slip, max_price,
                       args.min_price, args.tp / 100.0, args.sl / 100.0,
                       rank_basis=args.rank_basis)
        print(f"★決済方式: 利確+{args.tp:.1f}% / 損切り-{args.sl:.1f}% "
              f"(ザラ場でタッチ→そこで決済 / 未達は引け)")
    else:
        points = {}
        for sym, df in data.items():
            pts = daily_points(df, rank_off, exit_off)
            if pts:
                points[sym] = pts
        if not points:
            print("[ERROR] 有効なデータがありません。")
            return 1
        res = run(points, top_n=args.top_n, slip_one_way=slip,
                  max_price=max_price, min_price=args.min_price,
                  min_move=args.min_move / 100.0)

    print(f"検証日数: {res['n_days']}日\n")
    labels = {
        "LONG_GAINERS":  "値上がりTOP を買い→決済 (順張りロング)",
        "SHORT_GAINERS": "値上がりTOP を空売り→買戻し (逆張りショート)",
        "LONG_LOSERS":   "値下がりTOP を買い→決済 (リバウンド狙いロング)",
        "SHORT_LOSERS":  "値下がりTOP を空売り→買戻し (順張りショート)",
    }
    print(f"  {'戦略':<40}{'取引':>6}{'勝率':>8}{'平均%':>9}{'平均円':>10}{'合計円':>13}")
    print("  " + "-" * 84)
    for k in ("LONG_GAINERS", "SHORT_GAINERS", "LONG_LOSERS", "SHORT_LOSERS"):
        s = res["aggs"][k]
        print(f"  {labels[k]:<40}{s['n']:>6}{s['wr']:>7.1f}%{s['avg']:>+8.2f}%"
              f"{s['avg_yen']:>+10,.0f}{s['total_yen']:>+13,.0f}")
    print()
    print("※ 平均円/合計円は 100株・前日終値比ランキングでのスリッページ&手数料込み概算。")
    print("※ 期間が短い(ローカル5分足の範囲)ため結果は暫定。プラスでも過信しないこと。")

    if _tpsl_mode:
        print("\n  [決済理由の内訳]  TP=利確 / SL=損切り / close=引け(未達)")
        for k in ("LONG_GAINERS", "SHORT_GAINERS", "LONG_LOSERS", "SHORT_LOSERS"):
            r = res["reasons"][k]
            tot = r["tp"] + r["sl"] + r["close"] or 1
            print(f"  {labels[k]:<40} TP {r['tp']:>4}({r['tp']/tot*100:>3.0f}%) "
                  f"SL {r['sl']:>4}({r['sl']/tot*100:>3.0f}%) "
                  f"引け {r['close']:>4}({r['close']/tot*100:>3.0f}%)")

    # ── エントリー時刻スイープ (9:30 以外もいろいろ試す) ──
    if args.entry_sweep:
        _elist = [s.strip() for s in args.sweep_entry.split(",") if s.strip()]
        _dmode = (f"TP+{args.tp:.1f}%/SL-{args.sl:.1f}%" if _tpsl_mode
                  else f"{args.exit_time or '引け'}決済")
        print("\n" + "=" * 70)
        print(f"エントリー時刻スイープ ({_dmode} / 各時刻でランキング→建てる)")
        print("=" * 70)
        print(f"  {'entry':<8}{'値上買い':>10}{'値上空売':>10}{'値下買い':>10}{'値下空売':>10}")
        print("  " + "-" * 48)
        for e in _elist:
            es = _sec(e)
            if _tpsl_mode:
                r = run_tpsl(idx, es, args.top_n, slip, max_price,
                             args.min_price, args.tp / 100.0, args.sl / 100.0,
                             rank_basis=args.rank_basis)
            else:
                pts = points_from_index(idx, es, exit_sec)
                r = run(pts, args.top_n, slip, max_price, args.min_price,
                        min_move=args.min_move / 100.0)
            a = r["aggs"]
            print(f"  {e:<8}"
                  f"{a['LONG_GAINERS']['avg']:>+9.2f}%"
                  f"{a['SHORT_GAINERS']['avg']:>+9.2f}%"
                  f"{a['LONG_LOSERS']['avg']:>+9.2f}%"
                  f"{a['SHORT_LOSERS']['avg']:>+9.2f}%")
        print("  ※ 各セル=1トレード平均%(コスト込み)。プラスがあれば緑候補。")

    # ── 利確TP × 損切りSL のグリッド (サーフェス全体) ──
    if args.grid:
        _tps = [float(x) for x in args.grid_tp.split(",") if x.strip()]
        _sls = [float(x) for x in args.grid_sl.split(",") if x.strip()]
        _short = {"LONG_GAINERS": "値上買い", "SHORT_GAINERS": "値上空売",
                  "LONG_LOSERS": "値下買い", "SHORT_LOSERS": "値下空売"}
        print("\n" + "=" * 70)
        print(f"利確TP × 損切りSL グリッド (エントリー{args.rank_time}, top{args.top_n}, "
              f"{len(_tps)}×{len(_sls)}={len(_tps)*len(_sls)}通り)")
        print("=" * 70)
        cube = {}
        for tp in _tps:
            for sl in _sls:
                r = run_tpsl(idx, entry_sec, args.top_n, slip, max_price,
                             args.min_price, tp / 100.0, sl / 100.0,
                             rank_basis=args.rank_basis)
                cube[(tp, sl)] = {k: r["aggs"][k]["avg"] for k in DIRECTIONS}
        _npos = 0
        for k in DIRECTIONS:
            print(f"\n  【{_short[k]}】 行=利確TP% / 列=損切りSL%  (セル=平均%)")
            print("   TP\\SL " + "".join(f"{sl:>8.1f}" for sl in _sls))
            for tp in _tps:
                cells = ""
                for sl in _sls:
                    v = cube[(tp, sl)][k]
                    if v > 0:
                        _npos += 1
                    cells += f"{v:>+7.2f}%"
                print(f"   {tp:>4.1f} {cells}")
        _total = len(_tps) * len(_sls) * 4
        print(f"\n  → 全{_total}マス中 プラス {_npos}マス。"
              f"{'散発的ならノイズ(過剰最適化)。まとまった緑の塊があれば本物候補。' if _npos else '全マス赤=エッジ無し確定。'}")

    # ── 値上がりトップ空売り: 上げ幅帯別の反落成績 (核心) ──
    mag = magnitude_short_gainers(idx, entry_sec, exit_sec, slip,
                                  args.min_price, max_price)
    print("\n" + "=" * 70)
    print(f"【値上がりトップ空売り】上げ幅帯別 (9:30空売り→"
          f"{args.exit_time or '引け'}買戻し / スリッページ・手数料込み)")
    print("=" * 70)
    print(f"  {'上げ幅帯':<12}{'件数':>7}{'勝率':>8}{'平均net%':>10}{'平均gross%':>12}{'合計円':>13}")
    print("  " + "-" * 62)
    for lab, n, wr, net, gross, yen in mag:
        print(f"  {lab:<12}{n:>7}{wr:>7.1f}%{net:>+9.2f}%{gross:>+11.2f}%{yen:>+13,.0f}")
    print("  ※ gross=コスト前。net(コスト後)がプラスの帯があれば「その上げ幅だけ空売り」候補。")
    print("  ※ ただし大きく上げた小型株の空売りは踏み上げ(squeeze)・借株難のリスクが高い。")

    # ── HTML レポート ──
    if args.html:
        print("\nHTMLレポート生成中 (時間帯スイープ)...")
        health = data_health(idx)
        samples = sample_rankings(idx, args.top_n, entry_sec, max_price,
                                  args.min_price, n_dates=args.sample_dates)
        if samples:
            print(f"  サンプル日: {samples[0]['date']:%Y-%m-%d}〜"
                  f"{samples[-1]['date']:%Y-%m-%d} ({len(samples)}日)  "
                  f"※データ最新日={health['date_max']:%Y-%m-%d}")
        entry_list = [s.strip() for s in args.sweep_entry.split(",") if s.strip()]
        exit_list = [s.strip() for s in args.sweep_exit.split(",") if s.strip()]
        print(f"  スイープ: entry {len(entry_list)} × exit {len(exit_list)} 通り (高速版)...")
        sweeps = sweep_fast(idx, entry_list, exit_list, args.top_n, slip,
                            max_price, args.min_price, min_move=args.min_move / 100.0)
        meta = {
            "subtitle": (f"銘柄{len(data)} / 直近{args.days}日 / 上位下位各{args.top_n} / "
                         f"スリッページ片道{args.slippage:.2f}%(往復{(slip+FEE_ONE_WAY)*2*100:.2f}%) / "
                         + (f"株価{args.min_price:.0f}〜{max_price:.0f}円" if max_price
                            else "株価フィルタなし")),
            "exit_list": exit_list,
            "footer": "※ ランキング=前日終値比の騰落率。平均%はスリッページ・手数料込み。"
                      "J-Quants 5分足使用。緑=プラス/赤=マイナス。",
        }
        out = Path(f"intraday_ranking_report_{datetime.now(JST):%Y-%m-%d}.html")
        out.write_text(build_html(meta, health, samples, sweeps, res, mag),
                       encoding="utf-8")
        print(f"  → {out.resolve()}")
        if not args.no_browser:
            try:
                import webbrowser
                webbrowser.open(out.resolve().as_uri())
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
