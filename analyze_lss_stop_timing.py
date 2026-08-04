"""analyze_lss_stop_timing.py — lss損切りの時間的傾向を分析する。

「損切りはいつ起きる？早めに見切れる？」を時間軸で可視化する。

分析軸:
  1. 損切り発火時刻分布 (何時何分に損切りが多いか)
  2. 約定→損切りまでのバー数 (delay1/2/3で何件救えるか)
  3. エントリーギャップ×損切り率 (大きなギャップダウンは損切り予測因子か)
  4. 時間帯別の平均損切り額 (寄りの損切りは遅い時間の損切りより深いか)
  5. 曜日×損切り率 (月曜日が危ないか など)

使い方:
  python analyze_lss_stop_timing.py                      # 標準(BT30以上, 直近240日)
  python analyze_lss_stop_timing.py --bt-min 40          # BT40以上
  python analyze_lss_stop_timing.py --days 365           # 直近1年
  python analyze_lss_stop_timing.py --html               # HTMLレポートも生成
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--symbols-file", type=str, default=None)
ap.add_argument("--days",    type=int,   default=240)
ap.add_argument("--bt-min",  type=float, default=30.0, help="BTスコア下限(実際に投資する集団)")
ap.add_argument("--sm",      type=float, default=0.1)
ap.add_argument("--tm",      type=float, default=1.0)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--source",  choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit",   type=int, default=0)
ap.add_argument("--html",    action="store_true", help="HTMLレポートも出力する")
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

QTY          = ble.FIXED_QTY
FEE_ONE_WAY  = ble.FEE_PCT_ONE_WAY
_GAP_LIMIT   = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)
_ON_CLOSE    = getattr(ble, "_INTRADAY_5M_ON_CLOSE", False)
TODAY        = pd.Timestamp.now().normalize()
WEEKDAYS_JA  = ["月", "火", "水", "木", "金", "土", "日"]


# ──────────────────────────────────────────────────────────────────────────────
def _load_selected():
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
    mod  = importlib.util.module_from_spec(spec)
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
    base     = round_to_tick(order_limit)
    trigger  = float(round_to_tick(base - tick_size(base)))
    stop_p   = float(ceil_to_tick(max(order_stop, order_target)))
    target_p = float(ceil_to_tick(min(order_stop, order_target)))
    return trigger, stop_p, target_p


def _bt_score(bt_trades) -> int:
    if not bt_trades:
        return 0
    pr = {}
    for P in _BT_PERIODS:
        cutoff = TODAY - pd.Timedelta(days=P)
        sub    = [p for (d, p) in bt_trades if pd.Timestamp(d) >= cutoff]
        if not sub:
            continue
        wins = sum(1 for p in sub if p > 0)
        gp   = sum(p for p in sub if p > 0)
        gl   = -sum(p for p in sub if p <= 0)
        pf   = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        pr[P] = {"trades": len(sub), "win_rate": wins / len(sub) * 100,
                 "pf": pf, "total_pnl": sum(sub)}
    return _calc_bt(pr)[0]


def _exit_base(opens, highs, lows, closes, ei, stop_p, target_p, day_close):
    """base(v13 delay0)で決済。戻り: (reason, exit_price_line, exit_idx)"""
    n = len(highs)
    for j in range(ei, n):
        if highs[j] >= stop_p:
            return "stop", stop_p, j
        if lows[j] <= target_p:
            return "target", target_p, j
    cx = day_close if (day_close and day_close > 0) else float(closes[-1])
    return "close", cx, n - 1


# ──────────────────────────────────────────────────────────────────────────────
def _scan_symbol(sym: str, name: str, strats: list[str]) -> list[dict]:
    """1銘柄・全戦略のlssトレードを処理し、各トレードの時間情報を返す。"""
    records = []
    _bt_window = max(args.days, max(_BT_PERIODS))

    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return records
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return records
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return records
    if df_raw is None or df_raw.empty:
        return records

    for strat in strats:
        mod    = mod_for(strat)
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

        bt_trades  = []
        local_recs = []  # このストラテジーの全トレード情報(BTスコア確定後に本 records に合流)

        for t in r.get("trade_log", []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            olp = float(t.get("order_limit",  0) or 0)
            osp = float(t.get("order_stop",   0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if olp <= 0 or osp <= 0 or otp <= 0:
                continue
            if olp < args.min_price or olp > args.max_price:
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

            gap_pct = (entry_fill - trigger) / trigger * 100 if trigger > 0 else 0.0

            highs  = db["high"].to_numpy(dtype=float)
            lows   = db["low"].to_numpy(dtype=float)
            closes = db["close"].to_numpy(dtype=float)
            opens  = db["open"].to_numpy(dtype=float)
            times  = db.index
            n      = len(lows)

            # 約定バー ei
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

            # base(v13 delay0)で決済
            reason, ex_price, exit_idx = _exit_base(opens, highs, lows, closes,
                                                     ei, stop_p, target_p, d_close)
            pnl_line = short_pnl(entry_fill, ex_price, reason, QTY, FEE_ONE_WAY, 0.0)

            # BTスコア用(直近365日)
            bt_trades.append((fd, pnl_line))

            # --days 窓外は統計に使わない
            if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=args.days):
                continue

            entry_bar_time = str(times[ei].strftime("%H:%M"))
            exit_bar_time  = str(times[exit_idx].strftime("%H:%M")) if exit_idx < n else "15:30"
            bars_to_exit   = exit_idx - ei          # 約定バー ei から何本後に決済したか
            dow            = pd.Timestamp(fd).dayofweek  # 0=月 4=金

            local_recs.append({
                "date": str(fd),
                "sym": sym,
                "strat": strat,
                "reason": reason,
                "entry_time": entry_bar_time,
                "exit_time": exit_bar_time,
                "bars_to_exit": bars_to_exit,
                "gap_pct": round(gap_pct, 2),
                "entry_fill": round(entry_fill, 1),
                "stop_p": round(stop_p, 1),
                "pnl": round(pnl_line, 0),
                "dow": dow,
            })

        # BTスコアで母集団を絞る
        bt_val = _bt_score(bt_trades)
        if args.bt_min > 0 and bt_val < args.bt_min:
            continue
        # BTスコアを各レコードに付与して合流
        for rec in local_recs:
            rec["bt"] = bt_val
            records.append(rec)

    return records


# ──────────────────────────────────────────────────────────────────────────────
def _hdr(title, width=72):
    print(f"\n{'─' * width}")
    print(f"  {title}")
    print('─' * width)


def main():
    sel = _load_selected()
    if args.limit > 0:
        sel = sel[:args.limit]

    # 銘柄をグループ化
    by_sym: dict[str, dict] = {}
    for code, name, strat in sel:
        if code not in by_sym:
            by_sym[code] = {"name": name, "strats": []}
        by_sym[code]["strats"].append(strat)

    print(f"[分析] {len(by_sym)} 銘柄 × 複数戦略 | BT{args.bt_min:.0f}以上 | 直近{args.days}日")

    all_records: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_scan_symbol, code, info["name"], info["strats"]): code
                for code, info in by_sym.items()}
        for fut in as_completed(futs):
            recs = fut.result() or []
            all_records.extend(recs)
            done += 1
            if done % 20 == 0:
                print(f"  [{done}/{len(by_sym)}] 処理済み…")

    if not all_records:
        print("[警告] トレード記録が0件です。--bt-min を下げるか --days を増やしてください。")
        return

    df = pd.DataFrame(all_records)
    stops = df[df["reason"] == "stop"].copy()
    all_n = len(df)
    stop_n = len(stops)
    tgt_n  = len(df[df["reason"] == "target"])
    cls_n  = len(df[df["reason"] == "close"])

    print(f"\n[集計] 全{all_n}件  損切={stop_n}件({stop_n/all_n*100:.1f}%)  "
          f"利確={tgt_n}件({tgt_n/all_n*100:.1f}%)  引け={cls_n}件({cls_n/all_n*100:.1f}%)")

    if stop_n == 0:
        print("[情報] 損切りが0件。終了。")
        return

    # ──────────────────────────────────────────────────
    # 1. 損切り発火時刻分布
    # ──────────────────────────────────────────────────
    _hdr("① 損切り発火時刻分布 (何時何分に損切りが多いか)")
    # 5分単位の時刻バケット → "09:00" "09:05" ...
    exit_time_counts: dict[str, int] = defaultdict(int)
    exit_time_pnl:   dict[str, list] = defaultdict(list)
    for _, row in stops.iterrows():
        t = row["exit_time"]
        exit_time_counts[t] += 1
        exit_time_pnl[t].append(row["pnl"])

    total_stops = len(stops)
    sorted_times = sorted(exit_time_counts.keys())
    print(f"  (総損切り{total_stops}件の時刻分布)\n")
    print(f"  {'時刻':>7}  {'件数':>4}  {'割合':>6}  {'平均損切り額':>10}  {'合計損切り額':>12}")
    print(f"  {'-------':>7}  {'----':>4}  {'------':>6}  {'----------':>10}  {'------------':>12}")
    running = 0
    for t in sorted_times:
        cnt   = exit_time_counts[t]
        pnls  = exit_time_pnl[t]
        avg_p = sum(pnls) / len(pnls) if pnls else 0
        tot_p = sum(pnls)
        running += cnt
        print(f"  {t:>7}  {cnt:>4}  {cnt/total_stops*100:>5.1f}%  {avg_p:>+10,.0f}円  {tot_p:>+12,.0f}円")

    # 時間帯別集計 (寄り/前半/後半)
    _hdr("① 時間帯別サマリー")
    bands = [("09:00-09:30  寄り直後", "09:00", "09:25"),
             ("09:30-10:30  午前前半", "09:30", "10:25"),
             ("10:30-11:30  午前後半", "10:30", "11:25"),
             ("11:30-12:30  昼前後",   "11:30", "12:25"),
             ("12:30-14:00  午後前半", "12:30", "13:55"),
             ("14:00-15:30  大引け前", "14:00", "15:25")]
    print(f"\n  {'時間帯':20}  {'損切件数':>7}  {'割合':>6}  {'平均損切額':>10}  {'合計損切額':>12}")
    print(f"  {'--------------------':20}  {'-------':>7}  {'------':>6}  {'----------':>10}  {'------------':>12}")
    for label, t_from, t_to in bands:
        sub = stops[(stops["exit_time"] >= t_from) & (stops["exit_time"] <= t_to)]
        cnt = len(sub)
        if cnt == 0:
            print(f"  {label:20}  {0:>7}  {'  0.0%':>6}  {'       --':>10}  {'           --':>12}")
            continue
        avg_p = sub["pnl"].mean()
        tot_p = sub["pnl"].sum()
        print(f"  {label:20}  {cnt:>7}  {cnt/total_stops*100:>5.1f}%  {avg_p:>+10,.0f}円  {tot_p:>+12,.0f}円")

    # ──────────────────────────────────────────────────
    # 2. 約定→損切りまでのバー数 (delay1/2/3/4 効果)
    # ──────────────────────────────────────────────────
    _hdr("② 約定→損切りまでの5分バー数 (delay で何件救えるか)")
    print("  bars=0 → 約定した5分足の中で損切り (delay1 で回避できる)")
    print("  bars=1 → 次の足(5分後)で損切り  (delay2 で回避できる)")
    print("  bars≥K → delay(K+1) で回避できる\n")

    bar_counts: dict[int, int] = defaultdict(int)
    bar_pnls:   dict[int, list] = defaultdict(list)
    for _, row in stops.iterrows():
        b = int(row["bars_to_exit"])
        bar_counts[b] += 1
        bar_pnls[b].append(row["pnl"])

    max_bar = max(bar_counts.keys()) if bar_counts else 0
    print(f"  {'バー数':>6}  {'件数':>5}  {'割合':>6}  {'平均損切額':>10}  {'累積件数':>7}  {'delay効果':}")
    print(f"  {'------':>6}  {'-----':>5}  {'------':>6}  {'----------':>10}  {'-------':>7}  ")
    cum = 0
    delay_savings = {}
    for b in range(max_bar + 1):
        cnt   = bar_counts.get(b, 0)
        pnls  = bar_pnls.get(b, [])
        avg_p = sum(pnls) / len(pnls) if pnls else 0
        cum   += cnt
        delay_savings[b] = {"cnt": cum, "pnl": sum(p for bb in range(b + 1) for p in bar_pnls.get(bb, []))}
        delay_label = f"← delay{b+1}で{cum}件({cum/total_stops*100:.0f}%)回避可能" if cnt > 0 else ""
        print(f"  {b:>6}  {cnt:>5}  {cnt/total_stops*100:>5.1f}%  {avg_p:>+10,.0f}円  {cum:>7}  {delay_label}")

    print()
    for d in range(1, min(5, max_bar + 2)):
        saved_n   = delay_savings.get(d - 1, {}).get("cnt", 0)
        saved_pnl = delay_savings.get(d - 1, {}).get("pnl", 0)
        print(f"  delay{d}: {saved_n}件({saved_n/total_stops*100:.0f}%)の損切りを回避 "
              f"→ 損失節減上限 {-saved_pnl:+,.0f}円 (損切りゼロになった場合の最大値)")

    # ──────────────────────────────────────────────────
    # 3. エントリーギャップ×損切り率
    # ──────────────────────────────────────────────────
    _hdr("③ エントリーギャップ × 損切り率 (大ギャップダウンが損切り予測因子か)")
    print("  gap_pct = (約定価格 - トリガー価格) / トリガー × 100")
    print("  lss(空売り)では、gap_pct が負 = ギャップダウン = 不利な約定\n")

    gap_bins = [("ギャップなし [0%, -)   ",  0.0,    99.0),
                ("ギャップ小  (-0.5%, 0%)", -0.5,     0.0),
                ("ギャップ中  (-1.5%,-0.5%)", -1.5,  -0.5),
                ("ギャップ大  (-3.0%,-1.5%)", -3.0,  -1.5),
                ("指値下限付近(-3.0%以下)",  -99.0,  -3.0)]

    print(f"  {'エントリーギャップ帯':28}  {'全件':>5}  {'損切件':>5}  {'損切率':>6}  {'損切時平均損切額':>12}")
    print(f"  {'----------------------------':28}  {'-----':>5}  {'------':>5}  {'------':>6}  {'------------':>12}")
    for label, lo, hi in gap_bins:
        sub_all  = df[   (df["gap_pct"] > lo) & (df["gap_pct"] <= hi)]
        sub_stop = stops[(stops["gap_pct"] > lo) & (stops["gap_pct"] <= hi)]
        n_all  = len(sub_all)
        n_stop = len(sub_stop)
        if n_all == 0:
            print(f"  {label:28}  {'0':>5}  {'0':>5}  {'  --':>6}  {'  --':>12}")
            continue
        avg_stop_pnl = sub_stop["pnl"].mean() if n_stop > 0 else 0
        print(f"  {label:28}  {n_all:>5}  {n_stop:>5}  "
              f"{n_stop/n_all*100:>5.1f}%  {avg_stop_pnl:>+12,.0f}円")

    # ──────────────────────────────────────────────────
    # 4. 曜日別の損切り率
    # ──────────────────────────────────────────────────
    _hdr("④ 曜日別の損切り率")
    print(f"  {'曜日':>5}  {'全件':>5}  {'損切件':>6}  {'損切率':>6}  {'損切時平均損切額':>12}  {'全件合計損益':>12}")
    print(f"  {'-----':>5}  {'-----':>5}  {'------':>6}  {'------':>6}  {'------------':>12}  {'------------':>12}")
    for dow in range(5):
        all_dow  = df[df["dow"] == dow]
        stop_dow = stops[stops["dow"] == dow]
        n_all  = len(all_dow)
        n_stop = len(stop_dow)
        if n_all == 0:
            continue
        avg_stop_pnl = stop_dow["pnl"].mean() if n_stop > 0 else 0
        total_pnl    = all_dow["pnl"].sum()
        print(f"  {WEEKDAYS_JA[dow]:>5}  {n_all:>5}  {n_stop:>6}  "
              f"{n_stop/n_all*100:>5.1f}%  {avg_stop_pnl:>+12,.0f}円  {total_pnl:>+12,.0f}円")

    # ──────────────────────────────────────────────────
    # 5. エントリー時刻 × 損切り率 (何時に入ったトレードが危ないか)
    # ──────────────────────────────────────────────────
    _hdr("⑤ エントリー時刻 × 損切り率 (何時に約定したトレードが損切りになりやすいか)")
    entry_band_data: dict[str, dict] = {}
    bands5 = [("09:00-09:15  寄り天", "09:00", "09:10"),
              ("09:15-09:30        ", "09:15", "09:25"),
              ("09:30-10:00  午前前", "09:30", "09:55"),
              ("10:00-11:30  午前中", "10:00", "11:25"),
              ("11:30-13:00  昼時間", "11:30", "12:55"),
              ("13:00-14:00  午後前", "13:00", "13:55"),
              ("14:00-15:30  大引け前", "14:00", "15:25")]
    print(f"\n  {'エントリー時刻帯':22}  {'全件':>5}  {'損切件':>5}  {'損切率':>6}  "
          f"{'損切時平均損切額':>12}  {'全件合計損益':>12}")
    print(f"  {'----------------------':22}  {'-----':>5}  {'-----':>5}  {'------':>6}  "
          f"{'------------':>12}  {'------------':>12}")
    for label, t_from, t_to in bands5:
        sub_all  = df[   (df["entry_time"] >= t_from) & (df["entry_time"] <= t_to)]
        sub_stop = stops[(stops["entry_time"] >= t_from) & (stops["entry_time"] <= t_to)]
        n_all  = len(sub_all)
        n_stop = len(sub_stop)
        if n_all == 0:
            print(f"  {label:22}  {'0':>5}  {'0':>5}  {'  --':>6}  {'            --':>12}  {'            --':>12}")
            continue
        avg_stop_pnl = sub_stop["pnl"].mean() if n_stop > 0 else 0
        total_pnl    = sub_all["pnl"].sum()
        print(f"  {label:22}  {n_all:>5}  {n_stop:>5}  "
              f"{n_stop/n_all*100:>5.1f}%  {avg_stop_pnl:>+12,.0f}円  {total_pnl:>+12,.0f}円")

    # ──────────────────────────────────────────────────
    # 6. サマリー: 損切り削減の実用的アドバイス
    # ──────────────────────────────────────────────────
    _hdr("⑥ サマリー: 損切り削減の実用的アドバイス")

    # delay1効果
    saved0 = delay_savings.get(0, {})
    d1_cnt = saved0.get("cnt", 0)
    d1_pnl = saved0.get("pnl", 0)
    d1_pct = d1_cnt / total_stops * 100 if total_stops > 0 else 0

    # 寄り直後(09:00-09:25)に損切りが集中しているか
    early_stop = stops[(stops["exit_time"] >= "09:00") & (stops["exit_time"] <= "09:25")]
    early_pct  = len(early_stop) / total_stops * 100 if total_stops > 0 else 0

    # ギャップダウン(-1.5%以下)トレードの損切り率
    gap_sub_all  = df[(df["gap_pct"] <= -1.5)]
    gap_sub_stop = stops[(stops["gap_pct"] <= -1.5)]
    gap_stop_rate = len(gap_sub_stop) / len(gap_sub_all) * 100 if len(gap_sub_all) > 0 else 0
    norm_sub_all  = df[(df["gap_pct"] > -1.5)]
    norm_sub_stop = stops[(stops["gap_pct"] > -1.5)]
    norm_stop_rate = len(norm_sub_stop) / len(norm_sub_all) * 100 if len(norm_sub_all) > 0 else 0

    print()
    print(f"  ■ 全損切り: {total_stops}件 / 全約定: {all_n}件 ({stop_n/all_n*100:.1f}%)")
    print()
    print(f"  ■ delay1 効果(寄り1本目の損切りなし): {d1_cnt}件({d1_pct:.0f}%)を回避可能")
    print(f"       → 損失節減上限(損切→利確/引けになった場合): {-d1_pnl:+,.0f}円")
    print(f"       → 実運用: lss_exit_watcher --stop-delay-bars 1 で既に適用中")
    print()
    print(f"  ■ 損切り時刻: {early_pct:.0f}% が 09:00-09:25 (寄り直後)に集中")
    if early_pct >= 40:
        print(f"       → 寄り天の一瞬の上ヒゲで刈られるケースが多い。delay が有効な状況。")
    else:
        print(f"       → 寄り直後に限らず日中分散している。他の対策が必要。")
    print()
    print(f"  ■ ギャップダウン1.5%以上: 損切率 {gap_stop_rate:.0f}%  vs  通常: {norm_stop_rate:.0f}%")
    if gap_stop_rate > norm_stop_rate * 1.3:
        print(f"       → 大ギャップダウンは損切りリスクが高い(gap見送りルールが有効)。")
    else:
        print(f"       → ギャップと損切り率に明確な相関なし。gap見送りの効果は限定的。")
    print()

    # CSVエクスポート
    out_csv = f"lss_stop_timing_{pd.Timestamp.now().strftime('%Y-%m-%d')}.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"  [出力] 全トレード → {out_csv}")

    # HTMLレポート
    if args.html:
        _make_html(df, stops, delay_savings, gap_bins, bands, bands5)


def _make_html(df, stops, delay_savings, gap_bins, bands, bands5):
    """シンプルなHTMLレポートを生成する。"""
    all_n  = len(df)
    stop_n = len(stops)
    tgt_n  = len(df[df["reason"] == "target"])
    cls_n  = len(df[df["reason"] == "close"])

    def _row(cells, bold=False, bg=""):
        td = "th" if bold else "td"
        style = f' style="background:{bg}"' if bg else ""
        return "<tr" + style + ">" + "".join(f"<{td}>{c}</{td}>" for c in cells) + "</tr>\n"

    exit_time_counts: dict[str, int] = defaultdict(int)
    exit_time_pnl:   dict[str, list] = defaultdict(list)
    for _, row in stops.iterrows():
        t = row["exit_time"]
        exit_time_counts[t] += 1
        exit_time_pnl[t].append(row["pnl"])

    # 損切り時刻表
    rows_t = ""
    for t in sorted(exit_time_counts.keys()):
        cnt  = exit_time_counts[t]
        pnls = exit_time_pnl[t]
        avg_p = sum(pnls) / len(pnls) if pnls else 0
        tot_p = sum(pnls)
        bg = "#7f1d1d22" if cnt / (len(stops) or 1) > 0.1 else ""
        rows_t += _row([t, cnt, f"{cnt/len(stops)*100:.1f}%",
                        f"{avg_p:+,.0f}円", f"{tot_p:+,.0f}円"], bg=bg)

    # delay効果表
    max_bar = max(delay_savings.keys()) if delay_savings else 0
    bar_counts: dict[int, int] = defaultdict(int)
    bar_pnls:   dict[int, list] = defaultdict(list)
    for _, row in stops.iterrows():
        b = int(row["bars_to_exit"])
        bar_counts[b] += 1
        bar_pnls[b].append(row["pnl"])
    rows_d = ""
    cum = 0
    for b in range(max_bar + 1):
        cnt  = bar_counts.get(b, 0)
        avg_p = sum(bar_pnls.get(b, [])) / (cnt or 1)
        cum   += cnt
        s_pnl = delay_savings.get(b, {}).get("pnl", 0)
        bg = "#fef08a44" if b < 2 else ""
        rows_d += _row([f"{b}本後", cnt, f"{cnt/stop_n*100:.1f}%",
                        f"{avg_p:+,.0f}円", cum, f"{cum/stop_n*100:.0f}%",
                        f"{-s_pnl:+,.0f}円 節減"], bg=bg)

    date_str = pd.Timestamp.now().strftime("%Y-%m-%d")
    out_html = f"lss_stop_timing_{date_str}.html"

    html = f"""<!doctype html><html lang="ja"><head>
<meta charset="utf-8">
<title>lss損切りタイミング分析 {date_str}</title>
<style>
body{{font-family:sans-serif;background:#0f172a;color:#e2e8f0;margin:24px;font-size:14px}}
h1{{color:#f1f5f9;font-size:1.3rem;margin-bottom:4px}}
h2{{color:#94a3b8;font-size:1.0rem;margin:24px 0 8px}}
.kpi{{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}}
.kpi-box{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 20px;min-width:120px}}
.kpi-val{{font-size:1.4rem;font-weight:bold;color:#f59e0b}}
.kpi-lbl{{font-size:0.75rem;color:#94a3b8}}
table{{border-collapse:collapse;width:100%;margin-bottom:16px}}
th{{background:#1e3a5f;color:#93c5fd;text-align:right;padding:6px 10px;font-size:0.85rem}}
th:first-child{{text-align:left}}
td{{border-bottom:1px solid #1e293b;padding:5px 10px;text-align:right;font-size:0.85rem}}
td:first-child{{text-align:left;color:#cbd5e1}}
tr:hover td{{background:#1e293b88}}
</style></head><body>
<h1>lss 損切りタイミング分析 — BT{args.bt_min:.0f}以上・直近{args.days}日</h1>
<p style="color:#64748b;font-size:0.8rem">生成: {date_str}</p>
<div class="kpi">
  <div class="kpi-box"><div class="kpi-val">{all_n}</div><div class="kpi-lbl">全約定件数</div></div>
  <div class="kpi-box"><div class="kpi-val">{stop_n}</div><div class="kpi-lbl">損切り件数</div></div>
  <div class="kpi-box"><div class="kpi-val">{stop_n/all_n*100:.1f}%</div><div class="kpi-lbl">損切り率</div></div>
  <div class="kpi-box"><div class="kpi-val">{tgt_n}</div><div class="kpi-lbl">利確件数</div></div>
  <div class="kpi-box"><div class="kpi-val">{stops["pnl"].mean():+,.0f}円</div><div class="kpi-lbl">平均損切り額</div></div>
  <div class="kpi-box"><div class="kpi-val">{stops["pnl"].sum():+,.0f}円</div><div class="kpi-lbl">損切り合計</div></div>
</div>
<h2>① 損切り発火時刻分布</h2>
<table><thead><tr><th>時刻</th><th>件数</th><th>割合</th><th>平均損切額</th><th>合計損切額</th></tr></thead>
<tbody>{rows_t}</tbody></table>
<h2>② 約定→損切りまでのバー数 (delay効果)</h2>
<p style="color:#94a3b8;font-size:0.8rem">黄背景=delay1/delay2で回避できる損切り件数</p>
<table><thead><tr><th>バー数</th><th>件数</th><th>割合</th><th>平均損切額</th><th>累積件数</th><th>累積割合</th><th>delay節減上限</th></tr></thead>
<tbody>{rows_d}</tbody></table>
</body></html>"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [出力] HTML → {out_html}")
    try:
        import subprocess, sys as _sys
        if _sys.platform.startswith("win"):
            subprocess.Popen(["start", out_html], shell=True)
        else:
            subprocess.Popen(["xdg-open", out_html])
    except Exception:
        pass


if __name__ == "__main__":
    main()
