r"""sim_portfolio_lss.py — lss「注文の出し過ぎ(over-subscribe)」ポートフォリオ検証。

背景(ユーザー2026-08-01): 予算400万円に合わせて注文額400万で組むが、約定率が金額ベースで
~50%なので実際に埋まるのは約200万=恒常的に予算の半分しか働いていない。埋まる額を400万に
近づけるため、注文を予算より多めに出す(=over-subscribe)。ただし約定は相関する(急落日は一斉
約定)ので、liveでは「約定した金額が予算に達したら残りの逆指値をキャンセル」する上限管理が必須。

本ツールはそれを忠実に再現して倍率(何倍出すか)を比較する:
  * 各営業日、BT降順で M×予算ぶんの逆指値売り注文を出す(注文額=前日終値×株数で概算)。
  * その日の5分足で約定を時刻順に処理。約定した金額の累計が予算に達したら残りをキャンセル
    (=live上限管理)。寄り同時約定(同一足)はキャンセル不能なので丸ごと約定=急落日のオーバー
    を捕捉する。
  * 損益=約定した銘柄の同日決済pnl(本番同等: short_entry_fill_5m / short_exit_5m / short_pnl、
    delay1・指値ガード3%・slip=0)。
  * 出力: 倍率別の総損益 / 平均稼働率(deployed/budget) / フル稼働日% / 最大同時約定額(concurrent、
    =急落日の証拠金ピーク) / 予算超過日数。OOS(--base-month)分割。

約定は「銘柄×戦略」ペアの entry_sig から本番同等に再構成(analyze_nofill_short と同じ方式)。
BTは lss_trades.csv の bt列(=レポート一致)、BT>=--bt-min。同一銘柄が複数戦略で出た日は既定で
最高BTの1件に統合(--no-dedupe-symbol で無効=1銘柄複数ポジションを許す)。

使い方:
  set LSS_TRADES_CSV=lss_trades.csv & python sim_portfolio_lss.py --bt-min 40 --budget 4000000 --workers 8
  ... --multiples 1.0,1.5,2.0,2.5,3.0     # 倍率を振る
  ... --by-multiple                        # 倍率ごとの月次も出す
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import timedelta as _td
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import groupby
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss over-subscribe ポートフォリオ検証(予算上限キャンセル再現)")
ap.add_argument("--trades-csv", type=str, default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"))
ap.add_argument("--bt-min", type=float, default=40.0)
ap.add_argument("--budget", type=float, default=4_000_000.0, help="予算(円)。約定累計がこれに達したら残り注文をキャンセル")
ap.add_argument("--min-price", type=float, default=1000.0, help="対象銘柄の最低株価(実運用 daily と揃える。既定1000)")
ap.add_argument("--max-price", type=float, default=6000.0, help="対象銘柄の最高株価(実運用 daily=6000 と揃える。既定6000)")
ap.add_argument("--multiples", type=str, default="1.0,1.5,2.0,2.5,3.0",
                help="注文倍率(予算の何倍ぶん注文を出すか)をカンマ区切りで。1.0=現行(予算ぴったり)")
ap.add_argument("--base-month", type=str, default="2026-01", help="OOS分割(以前=TRAIN、以後=TEST)。空=全期間のみ")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍(lss=0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍(lss=1.0)")
ap.add_argument("--stop-delay-bars", type=int, default=1, help="損切り遅延(本)。lss=delay1=1(5分足)")
ap.add_argument("--qty", type=int, default=None, help="株数(既定=FIXED_QTY=100)")
ap.add_argument("--no-dedupe-symbol", action="store_true",
                help="同一銘柄が複数戦略で出た日を統合しない(=1銘柄複数ポジション許可)。既定=最高BTの1件に統合")
ap.add_argument("--by-multiple", action="store_true", help="倍率ごとに月次成績も出す")
ap.add_argument("--days", type=int, default=500)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--no-cache", action="store_true")
ap.add_argument("--refresh-cache", action="store_true")
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import split_by_day, load_intraday
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m, short_pnl

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._INTRADAY_5M = False

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE = ble.FEE_PCT_ONE_WAY
DELAY = max(0, int(args.stop_delay_bars))
GAP_LIMIT = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
MULTIPLES = [float(x) for x in args.multiples.split(",") if x.strip()]


def _norm(sym): return str(sym).upper().removesuffix(".T").split(".")[0]


def _jq_to_yf(code):
    c = str(code).strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _mins(ts):
    try:
        t = ts.time()
        return t.hour * 60 + t.minute
    except Exception:
        return 0


def _load_bt_pairs():
    p = Path(args.trades_csv)
    if not p.exists():
        print(f"[error] BTソースCSVが無い: {p}", file=sys.stderr); return {}
    bt = {}
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        sym = _norm(r.get("symbol") or r.get("code") or "")
        strat = str(r.get("strategy") or "").strip()
        if not sym or not strat:
            continue
        try:
            b = float(r.get("bt") or 0)
        except Exception:
            b = 0.0
        bt[(sym, strat)] = max(bt.get((sym, strat), -1e9), b)
    return {k: v for k, v in bt.items() if v >= args.bt_min}


def _collect(sym_yf, strat, bt):
    """1(銘柄,戦略)の lss 注文イベント(約定/不約定)を本番同等に集める。
    返り: list[dict]  各注文 = {date, sym, bt, order_notional, filled, fill_min, exit_min, fill_notional, pnl}"""
    out = []
    params = getattr(mod_for(strat), "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return out
    cf = params[0]
    try:
        m5 = load_intraday(sym_yf, days=args.days + 5, source="local")
    except Exception:
        return out
    if m5 is None or m5.empty:
        return out
    by_day = split_by_day(m5)
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym_yf, args.days + 420)
        df_ind = cf(df_raw.copy())
    except Exception:
        return out
    if df_ind is None or df_ind.empty or "entry_sig" not in df_ind.columns:
        return out
    close_by_date = {}
    try:
        for idx, c in df_ind["close"].items():
            close_by_date[idx.date() if hasattr(idx, "date") else idx] = float(c)
    except Exception:
        pass
    one_dates = sorted(by_day.keys())
    since = ble._TODAY - _td(days=args.days)
    sym = _norm(sym_yf)
    sig = df_ind[df_ind["entry_sig"].fillna(False)]
    for S, row in sig.iterrows():
        try:
            Sdate = S.date() if hasattr(S, "date") else S
        except Exception:
            continue
        if Sdate < since:
            continue
        prev_close = float(row.get("close", 0) or 0)
        atr = float(row.get("atr", 0) or 0)
        if prev_close <= 0 or atr <= 0 or atr != atr:
            continue
        if not (ble.MIN_PRICE <= prev_close <= ble.MAX_PRICE):
            continue
        if not (args.min_price <= prev_close <= args.max_price):
            continue                               # 実運用 daily の価格レンジ(1000-6000)と揃える
        if atr / prev_close > ble.MAX_ATR_RATIO:
            continue
        trigger = prev_close                       # em=0: lp = 前日終値
        stop = prev_close + atr * args.sm          # ショート損切=上
        target = prev_close - atr * args.tm        # ショート利確=下
        if not (target > 0 and target < trigger and stop > trigger):
            continue
        edate = next((d for d in one_dates if d > Sdate), None)
        if edate is None:
            continue
        db = by_day.get(edate)
        if db is None or len(db) < 1:
            continue
        d_open = float(db["open"].iloc[0])
        d_low = float(db["low"].min())
        d_high = float(db["high"].max())
        d_close = close_by_date.get(edate)
        order_notional = trigger * QTY
        fill = short_entry_fill_5m(db, trigger, is_rise_trigger=False, entry_gap_limit=GAP_LIMIT,
                                   day_open=d_open, day_low=d_low, day_high=d_high)
        rec = {"date": pd.Timestamp(edate), "sym": sym, "bt": bt,
               "order_notional": order_notional, "filled": False,
               "fill_min": None, "exit_min": None, "fill_notional": 0.0, "pnl": 0.0}
        if fill is None:
            out.append(rec)                        # 注文は出したが約定せず(=枠は使うが埋まらない)
            continue
        exit_p, reason, ent_ts, exit_ts = short_exit_5m(
            db, fill, stop, target, is_rise_trigger=False,
            stop_delay_bars=DELAY, day_low=d_low, day_high=d_high, day_close=d_close)
        if exit_p is None or reason in ("no_entry", "no_5m"):
            out.append(rec)
            continue
        rec.update({"filled": True, "fill_min": _mins(ent_ts), "exit_min": _mins(exit_ts),
                    "fill_notional": fill * QTY,
                    "pnl": short_pnl(fill, exit_p, reason, QTY, FEE, 0.0)})
        out.append(rec)
    return out


# ── ポートフォリオ・シミュレーション ──────────────────────────────
def _sim(records, mult):
    """1倍率のポートフォリオ結果を返す。各日: BT降順で M×予算ぶん注文→約定を時刻順→予算到達でキャンセル。"""
    budget = args.budget
    cap = budget * mult
    by_date = {}
    for r in records:
        by_date.setdefault(r["date"], []).append(r)

    days = []   # per-day dict: date, pnl, deployed, peak, ntaken, fully
    for d, recs in by_date.items():
        recs = sorted(recs, key=lambda r: r["bt"], reverse=True)
        if not args.no_dedupe_symbol:
            seen = set(); dedup = []
            for r in recs:
                if r["sym"] in seen:
                    continue
                seen.add(r["sym"]); dedup.append(r)
            recs = dedup
        # 1) BT降順で M×予算ぶん注文を出す(注文額=order_notional の累計が cap 到達で打ち止め)
        placed = []; placed_not = 0.0
        for r in recs:
            if placed_not >= cap:
                break
            placed.append(r); placed_not += r["order_notional"]
        # 2) 約定を時刻順に処理。約定累計が予算に達したら残りをキャンセル。
        #    同一足(同時刻)の約定はキャンセル不能=丸ごと約定(急落日のオーバーを捕捉)。
        fills = sorted([r for r in placed if r["filled"]], key=lambda r: r["fill_min"])
        taken = []; cum = 0.0
        for _tmin, grp in groupby(fills, key=lambda r: r["fill_min"]):
            if cum >= budget:                       # 予算到達済 → この時刻以降をキャンセル
                break
            for r in grp:                           # 同時刻グループは丸ごと約定
                taken.append(r); cum += r["fill_notional"]
        # 3) 同時保有額のピーク(=証拠金ピーク)を sweep で算出
        ev = []
        for r in taken:
            ev.append((r["fill_min"], 1, r["fill_notional"]))     # 約定=+
            ev.append((r["exit_min"], 0, -r["fill_notional"]))    # 決済=- (同時刻は約定を先に=保守的にピーク高め)
        ev.sort(key=lambda e: (e[0], -e[1]))
        curc = 0.0; peak = 0.0
        for _m, _k, delta in ev:
            curc += delta; peak = max(peak, curc)
        day_pnl = sum(r["pnl"] for r in taken)
        days.append({"date": d, "pnl": day_pnl, "deployed": cum, "peak": peak,
                     "ntaken": len(taken), "fully": 1 if cum >= budget * 0.999 else 0})
    return days


def _agg(days):
    if not days:
        return None
    n = len(days)
    pnl = sum(x["pnl"] for x in days)
    dep = sum(x["deployed"] for x in days) / n
    peak_avg = sum(x["peak"] for x in days) / n
    peak_max = max(x["peak"] for x in days)
    over = sum(1 for x in days if x["peak"] > args.budget * 1.1)      # ピークが予算+10%超の日数
    fully = sum(x["fully"] for x in days)
    ntr = sum(x["ntaken"] for x in days)
    return {"days": n, "pnl": pnl, "dep": dep, "util": dep / args.budget * 100,
            "peak_avg": peak_avg, "peak_max": peak_max, "over": over,
            "fully": fully, "fully_pct": fully / n * 100, "ntr": ntr}


def _fmt(a):
    if not a:
        return "  該当なし"
    return (f"損益{a['pnl']:>+13,.0f}  稼働{a['util']:>4.0f}%(平均{a['dep']/1e4:>4.0f}万)  "
            f"フル稼働{a['fully_pct']:>3.0f}%  同時保有ピーク平均{a['peak_avg']/1e4:>4.0f}万/最大{a['peak_max']/1e4:>4.0f}万  "
            f"予算+10%超日{a['over']:>3}  取引{a['ntr']:>5}")


def main():
    keep = _load_bt_pairs()
    if not keep:
        print("[error] BT対象ペアが0。", file=sys.stderr); return
    pairs = sorted(keep.keys())
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] BT{args.bt_min:.0f}以上 {len(pairs)}ペア / 予算{args.budget/1e4:.0f}万 / "
          f"倍率{MULTIPLES} / 価格{args.min_price:.0f}-{args.max_price:.0f}円 / "
          f"sm{args.sm} tm{args.tm} delay{DELAY} 株数{QTY} 指値ガード{GAP_LIMIT*100:.0f}% slip0 "
          f"/ 銘柄統合{'OFF' if args.no_dedupe_symbol else 'ON'}")

    import hashlib as _h, pickle as _pk
    _cd = Path(".simportfolio_cache")
    _key = _h.md5("|".join(str(x) for x in [
        "spv2", getattr(ble, "_BT_LOGIC_VER", "?"), args.sm, args.tm, DELAY, GAP_LIMIT,
        args.days, args.bt_min, args.limit, QTY, args.min_price, args.max_price,
        _h.md5(",".join(f"{s}:{t}" for s, t in pairs).encode()).hexdigest(),
    ]).encode()).hexdigest()[:16]
    _cf = _cd / f"records_{_key}.pkl"

    records = None
    if _cf.exists() and not args.no_cache and not args.refresh_cache:
        try:
            records = _pk.loads(_cf.read_bytes())
            print(f"[cache] 再利用: {_cf} ({len(records)}注文) ※--refresh-cacheで再計算")
        except Exception:
            records = None
    if records is None:
        records = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_collect, _jq_to_yf(s), t, keep[(s, t)]): (s, t) for (s, t) in pairs}
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 100 == 0:
                    print(f"  ...{done}/{len(pairs)}ペア", flush=True)
                try:
                    records += fut.result()
                except Exception:
                    continue
        if not args.no_cache:
            try:
                _cd.mkdir(exist_ok=True)
                _cf.write_bytes(_pk.dumps(records))
                print(f"[cache] 保存: {_cf} ({len(records)}注文)")
            except Exception as _e:
                print(f"[cache] 保存失敗({_e})")

    if not records:
        print("[error] 注文0件。5分足/CSVを確認。", file=sys.stderr); return

    n_fill = sum(1 for r in records if r["filled"])
    on = sum(r["order_notional"] for r in records)
    fn = sum(r["fill_notional"] for r in records)
    print("\n" + "=" * 96)
    print("【全注文の約定率(母数チェック)】")
    print(f"  注文 {len(records)}件 / 約定 {n_fill}件({n_fill/len(records)*100:.0f}%)  "
          f"/ 金額ベース約定率 {fn/on*100:.0f}%(注文額{on/1e8:.2f}億 → 約定額{fn/1e8:.2f}億)")
    print("  ※金額ベース約定率 ≒ 倍率1.0(予算ぴったり注文)時の稼働率の目安。ユーザー実測~50%と比較。")

    bm = args.base_month.strip()
    be = None
    if bm:
        try:
            be = pd.Period(bm, "M").end_time.normalize()
        except Exception:
            be = None

    # 倍率ごとにシミュレーション
    print("\n" + "=" * 96)
    print(f"【倍率別サマリー】予算{args.budget/1e4:.0f}万・約定累計が予算到達で残り注文キャンセル(live上限管理)")
    hdr = "TRAIN/OOS" if be is not None else "全期間"
    for mult in MULTIPLES:
        days = _sim(records, mult)
        print("-" * 96)
        print(f"■ 倍率 x{mult:.1f}(注文額={args.budget*mult/1e4:.0f}万ぶん出す)")
        print(f"    [全期間] {_fmt(_agg(days))}")
        if be is not None:
            tr = [x for x in days if x["date"] <= be]
            te = [x for x in days if x["date"] > be]
            print(f"    [TRAIN ] {_fmt(_agg(tr))}")
            print(f"    [OOS   ] {_fmt(_agg(te))}")
        if args.by_multiple:
            from collections import defaultdict
            mon = defaultdict(list)
            for x in days:
                mon[x["date"].strftime("%Y-%m")].append(x)
            print("      月次:")
            for mk in sorted(mon):
                a = _agg(mon[mk])
                print(f"        {mk}  損益{a['pnl']:>+12,.0f}  稼働{a['util']:>4.0f}%  "
                      f"同時ピーク最大{a['peak_max']/1e4:>4.0f}万  取引{a['ntr']:>4}")

    print("\n" + "=" * 96)
    print("読み方:")
    print("  ・稼働% = 実際に埋まった額 / 予算。倍率1.0で~50%なら『予算の半分しか働いていない』を数値化。")
    print("  ・倍率↑で稼働%↑・総損益↑(エッジが正なら)。ただし『同時保有ピーク最大』が予算を超える日=急落日の")
    print("    オーバー(証拠金/レバ超過)。予算+10%超日 が許容範囲か で最適倍率を決める。")
    print("  ・slip=0固定(ユーザー方針2026-08-01)。銘柄統合ONで1銘柄1ポジション(§18.8)を再現。")


if __name__ == "__main__":
    main()
