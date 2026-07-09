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
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pandas as pd

JST = timezone(timedelta(hours=9))

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
        max_price: float | None, min_price: float) -> dict:
    """クロスセクションにランキングを組んで4方向を集計。"""
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
    ap.add_argument("--limit", type=int, default=None, help="先頭N銘柄だけ(デバッグ)")
    ap.add_argument("--source", default="local", choices=["local", "auto", "yfinance"])
    ap.add_argument("--self-test", action="store_true", help="合成データでロジック検証")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    from daytrade_data import load_intraday_batch, available_local_symbols

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

    points = {}
    for sym, df in data.items():
        pts = daily_points(df, rank_off, exit_off)
        if pts:
            points[sym] = pts
    if not points:
        print("[ERROR] 有効なデータがありません。")
        return 1

    res = run(points, top_n=args.top_n, slip_one_way=slip,
              max_price=max_price, min_price=args.min_price)

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
