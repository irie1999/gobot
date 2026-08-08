"""analyze_tenkan_cutoff.py — 「何時までに約定しなければ転換するか」を実測で決める。

運用上の問い:
  lssシグナルは 約定 / 不約定 に分かれる。不約定なら転換(ロング)したい。
  しかし「不約定」が確定するのは大引けなので、実運用では締切時刻を決めるしかない。
  09:05 で切ると、それ以降に約定する『遅れショート』を捨てることになる。
  → 捨てる遅れショートの損益 vs 拾える転換の損益、どちらが大きいか。

⛔ 2026-08-08 まで、転換の買いは常に 09:09 固定だった。締切 09:10 以降の行は
   『その時刻まで約定しなかった』という **その時点では未知の情報** を使って
   09:09 に買う = 時間逆行で、実装できないルールを測っていた。
   → --buy-mode cutoff (既定) で **締切時刻に買う** ようにした。これなら実運用と
     時間整合が取れる。旧挙動は --buy-mode fixed。

⛔ 転換の決済は「時間が来たら売る」だけで、**利確/損切りは一度も試されていなかった**。
   → --tk-sm / --tk-tm で OCO を置けるようにした。売却時刻も --tk-sell で動かせる
     (前場引け 11:30 が既定。大引けまで持つなら 15:25)。

やること:
  各シグナル日について5分足から
    ・約定バー(最初に安値<=トリガー)とその時刻
    ・lssとして持った場合の損益(損切り/利確/引け。delay1対応)
    ・転換した場合の損益(09:09以降の最初バーOPEN買い → 11:30以前の最後バーOPEN売り)
  を求め、締切時刻ごとに
    締切までに約定 → lss / それ以外(遅れ約定・終日不約定) → 転換
  として合算する。締切なし(=全部lss・現行)も同じ土俵で出す。

使い方(あなたの機械で。5分足データが必要):
  python analyze_tenkan_cutoff.py --days 240 --bt-min 40
  python analyze_tenkan_cutoff.py --days 240 --bt-min 40 --by-month
  python analyze_tenkan_cutoff.py --days 240 --bt-min 40 --workers 8
出力: コンソール比較表 + analyze_tenkan_cutoff_<date>.csv
"""
from __future__ import annotations

import argparse
import csv as _csv
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as _time
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="転換の締切時刻を実測で決める")
ap.add_argument("--days", type=int, default=240)
ap.add_argument("--sm", type=float, default=0.1)
ap.add_argument("--tm", type=float, default=1.0)
ap.add_argument("--min-price", type=float, default=1000.0)
ap.add_argument("--max-price", type=float, default=6000.0)
ap.add_argument("--bt-min", type=float, default=40.0,
                help="BTスコア下限(既定40。実際に投資する集団で判断すること)")
ap.add_argument("--stop-delay-bars", type=int, default=1,
                help="lssの損切り遅延(既定1=delay1。**実機 watch.bat --stop-delay-bars 1**"
                     "と揃える。CLAUDE.md 18.10.1『実機が正』)")
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--symbols-file", type=str, default=None)
ap.add_argument("--cutoffs", type=str, default="09:05,09:10,09:15,09:20,09:30,10:00,11:00",
                help="締切時刻のカンマ区切り。この時刻までに約定しなければ転換する")
ap.add_argument("--buy-mode", choices=["cutoff", "fixed"], default="cutoff",
                help="転換の買い時刻。**既定 cutoff = 締切時刻に買う**(実運用と時間整合)。"
                     "fixed は --tk-buy 固定で、締切がそれより後だと『まだ知らないこと』を"
                     "使って買うことになる(look-ahead)。2026-08-08 まで fixed 相当だった")
ap.add_argument("--tk-buy", type=str, default="09:09",
                help="--buy-mode fixed のときの買い時刻。cutoff モードでも"
                     "『締切なし/全部転換』の行はこの時刻を使う")
ap.add_argument("--tk-sell", type=str, default="11:30",
                help="転換の売り時刻(この時刻以前の最後バーOPEN)。引けまで持つなら 15:25")
ap.add_argument("--tk-sm", type=float, default=0.0,
                help="転換(ロング)の損切りATR倍率。0=損切りを置かない(時間決済のみ)。"
                     "**これは一度も試されていない**(転換は常に時間決済だった)")
ap.add_argument("--tk-tm", type=float, default=0.0,
                help="転換(ロング)の利確ATR倍率。0=利確を置かない")
ap.add_argument("--by-month", action="store_true")
ap.add_argument("--bt-window", type=int, default=420,
                help="BTスコア算出のためにバックテストを何日ぶん余分に回すか(既定420)。"
                     "小さくすると大幅に速くなるがBTスコアの精度が落ちる。"
                     "速く回したいときは 90 程度から。--bt-min 0 なら自動で30になる")
args = ap.parse_args()

# BTフィルターを使わないなら長い窓は不要 → 大幅に速くなる
_EXTRA = 30 if args.bt_min <= 0 else max(30, args.bt_window)

import backtest_limit_entry as ble
from backtest_limit_entry import ceil_to_tick, round_to_tick, tick_size
from check_signals_stop import PERIODS as _BT_PERIODS, calc_recommend_score as _calc_bt
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_pnl
import tenkan_sim as _tks

_GAP_LIMIT = getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)


def _day_ohlc(df_raw, fd):
    """日足の (始値, 安値, 高値, 終値)。compare_lss_rules と同一。"""
    try:
        drow = df_raw.loc[df_raw.index.normalize() == pd.Timestamp(fd)]
        if len(drow):
            return (float(drow["open"].iloc[0]), float(drow["low"].iloc[0]),
                    float(drow["high"].iloc[0]), float(drow["close"].iloc[0]))
    except Exception:
        pass
    return (None, None, None, None)


def _parse_hhmm(v: str, default):
    """'HH:MM' を time に。壊れていれば既定に落とす。"""
    try:
        hh, mm = str(v).strip().split(":")[:2]
        return _time(int(hh), int(mm))
    except Exception:
        print(f"[WARN] 時刻を解釈できません: {v} → {default} を使います")
        return default


def _prices(order_limit, order_stop, order_target):
    """注文値を呼値に丸めて (トリガー, 損切り, 目標) にする。compare_lss_rules と同一。"""
    base = round_to_tick(order_limit)
    trigger = float(round_to_tick(base - tick_size(base)))
    stop_p = float(ceil_to_tick(max(order_stop, order_target)))
    target_p = float(ceil_to_tick(min(order_stop, order_target)))
    return trigger, stop_p, target_p


# ── 転換(ロング)のシミュレーション ────────────────────────────────────
# tenkan_sim.simulate_bars はレポートと共用なので触らない。ここでは
#   ・買い時刻を締切に連動できる
#   ・利確/損切り(OCO)を置ける
# 拡張版をローカルに持つ。ルールが違うので別実装にしてある。
_TK_SLIP = 0.0005            # tenkan_sim と同じ
_TK_BUY = _parse_hhmm(args.tk_buy, _time(9, 9))
_TK_SELL = _parse_hhmm(args.tk_sell, _time(11, 30))


def _atr_of(olp: float, osp: float, otp: float) -> float:
    """注文値から ATR を逆算する。lssショートは
         order_stop   = 基準 + atr*sm
         order_target = 基準 - atr*tm
       なので (osp - olp)/sm で出る。sm が0なら tm 側から。"""
    if args.sm > 0:
        v = (osp - olp) / args.sm
    elif args.tm > 0:
        v = (olp - otp) / args.tm
    else:
        return 0.0
    return v if v == v and v > 0 else 0.0


def _tenkan(db, buy_t, atr: float) -> "float | None":
    """転換(ロング)の損益。buy_t 以降の最初バーOPENで買う。

    OCO(--tk-sm / --tk-tm)を置いた場合は先にタッチしたほうで決済し、
    どちらも触れなければ --tk-sell 以前の最後バーOPENで手仕舞う。
    同一バーで両方タッチしたら **損切り優先**(悲観側)。
    約定値は lss 側(v16)と同じ考え方:
      ロングの損切り(逆指値売り) → 窓を開けて下に飛べば不利 = min(stop, その足の始値)
      ロングの利確(指値売り)     → 窓を開けて上に飛べば有利 = max(target, その足の始値)
    """
    if db is None or len(db) < 3:
        return None
    times = [t.time() for t in db.index]
    bi = None
    for i, t in enumerate(times):
        if t >= buy_t:
            bi = i
            break
    if bi is None:
        return None
    buy_raw = float(db.iloc[bi]["open"])
    if buy_raw <= 0:
        return None
    # 時間決済の足(これ以降は建てない)
    li = None
    for i in range(len(times) - 1, -1, -1):
        if times[i] <= _TK_SELL:
            li = i
            break
    if li is None or li <= bi:
        return None

    b = buy_raw * (1 + _TK_SLIP)
    sell_raw = None
    if atr > 0 and (args.tk_sm > 0 or args.tk_tm > 0):
        stop_p = buy_raw - atr * args.tk_sm if args.tk_sm > 0 else None
        tgt_p = buy_raw + atr * args.tk_tm if args.tk_tm > 0 else None
        lows = db["low"].to_numpy(dtype=float)
        highs = db["high"].to_numpy(dtype=float)
        opens = db["open"].to_numpy(dtype=float)
        for j in range(bi, li + 1):
            hit_s = stop_p is not None and lows[j] <= stop_p
            hit_t = tgt_p is not None and highs[j] >= tgt_p
            if hit_s:                       # 同一バー両方タッチは損切り優先(悲観)
                sell_raw = min(stop_p, opens[j])
                break
            if hit_t:
                sell_raw = max(tgt_p, opens[j])
                break
    if sell_raw is None:
        sell_raw = float(db.iloc[li]["open"])
    if sell_raw <= 0:
        return None
    s = sell_raw * (1 - _TK_SLIP)
    return (s - b) * QTY - (b + s) * QTY * FEE


def _bt_score(trades) -> int:
    """(約定日, lss損益) から BTスコアを算出。compare_lss_rules._bt_score と同一。"""
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
    try:
        return _calc_bt(pr)[0]
    except Exception:
        return 0

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False

QTY = ble.FIXED_QTY
FEE = ble.FEE_PCT_ONE_WAY
TODAY = pd.Timestamp.today().normalize()

CUTOFFS: list = []
for _c in args.cutoffs.split(","):
    _c = _c.strip()
    if not _c:
        continue
    try:
        hh, mm = _c.split(":")
        CUTOFFS.append((_c, _time(int(hh), int(mm))))
    except Exception:
        print(f"[WARN] 締切時刻を解釈できません: {_c}")
# 比較の基準は『転換を一切しない純lss』。ここを外すと「締切なし(現行)」を
# ベースラインと誤読してしまう(現行は既に終日不約定を転換している)。
MODES = ([("純lss(転換なし)", "PURE")]
         + [("締切なし(現行)", None)]
         + CUTOFFS
         + [("全部転換(lssしない)", _time(0, 0))])


# ── 銘柄リスト ──────────────────────────────────────────────────────────────
def _load_pairs() -> list[tuple[str, str, list[str]]]:
    path = Path(args.symbols_file or "holdout_selected_symbols.py")
    if not path.exists():
        print(f"[ERROR] {path} が見つかりません。先に run_signals_holdout_all.py を実行してください。")
        sys.exit(1)
    ns: dict = {}
    exec(path.read_text(encoding="utf-8"), ns)
    pairs = ns.get("SELECTED") or ns.get("PAIRS") or ns.get("WATCHLIST") or []
    by_sym: dict = {}
    for p in pairs:
        if isinstance(p, (list, tuple)) and len(p) >= 3:
            sym, name, strat = str(p[0]), str(p[1]), str(p[2])
        elif isinstance(p, (list, tuple)) and len(p) == 2:
            sym, name, strat = str(p[0]), "", str(p[1])
        else:
            continue
        e = by_sym.setdefault(sym, [name, []])
        if strat not in e[1]:
            e[1].append(strat)
    out = [(s, v[0], v[1]) for s, v in by_sym.items()]
    if args.limit:
        out = out[:args.limit]
    return out


PAIRS = _load_pairs()
print(f"[選定] {len(PAIRS)}銘柄 / 遡及{args.days}日 / 価格{args.min_price:g}〜{args.max_price:g}円 "
      f"/ BT{args.bt_min:g}以上 / delay{args.stop_delay_bars}", flush=True)
print(f"[締切] {', '.join(m[0] for m in MODES)}", flush=True)


def _lss_exit(db, ei, trigger, stop_p, target_p, day_close):
    """lssとして持った場合の (損益, 理由)。delay は args.stop_delay_bars。
    損切り約定は『注文を新規に置いたバーだけ窓埋め』(compare_lss_rules の補正版と同じ)。"""
    opens = db["open"].to_numpy(dtype=float)
    highs = db["high"].to_numpy(dtype=float)
    lows = db["low"].to_numpy(dtype=float)
    closes = db["close"].to_numpy(dtype=float)
    n = len(highs)
    stop_from = ei + max(0, int(args.stop_delay_bars))
    for j in range(ei, n):
        if j >= stop_from and highs[j] >= stop_p:
            px = stop_p
            if j == stop_from and stop_from > ei:
                px = max(stop_p, float(opens[j]))   # 設置した瞬間に既に超えていた
            return px, "stop"
        if lows[j] <= target_p:
            return target_p, "target"
    cx = day_close if (day_close and day_close > 0) else float(closes[-1])
    return cx, "close"


def _scan(sym: str, name: str, strats: list[str]) -> list[dict]:
    """1銘柄の全シグナル日について (約定時刻, lss損益, 転換損益) を返す。"""
    rows: list[dict] = []
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return rows
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return rows
    try:
        df_raw = ble.fetch(sym, args.days + _EXTRA)
    except Exception:
        return rows
    if df_raw is None or df_raw.empty:
        return rows

    for strat in strats:
        mod = mod_for(strat)
        params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
        if not params:
            continue
        try:
            df_ind = params[0](df_raw.copy())
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.sm, args.tm, args.days + _EXTRA, strat,
                                       entry_type="stop_sell", max_hold=0)
        except Exception:
            continue
        if not r:
            continue

        bt_trades: list = []
        local: list[dict] = []
        for t in (r.get("trade_log") or []):
            if t.get("reason") in ("発注中", "保有中"):
                continue
            edt = t.get("entry_dt")
            if edt is None:
                continue
            # ※ フィールド名は order_limit / order_stop / order_target。
            #   stop_price / target_price ではない(ここを間違えると全件0件になる)。
            olp = float(t.get("order_limit", 0) or 0)
            osp = float(t.get("order_stop", 0) or 0)
            otp = float(t.get("order_target", 0) or 0)
            if olp <= 0 or osp <= 0 or otp <= 0:
                continue
            if args.min_price > 0 and olp < args.min_price:
                continue
            if args.max_price > 0 and olp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            # ※ by_day のキーは date オブジェクト。str(fd) では引けない。
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue

            trigger, osp, target_p = _prices(olp, osp, otp)
            d_open, d_low, d_high, d_close = _day_ohlc(df_raw, fd)

            entry_fill = short_entry_fill_5m(
                db, trigger, False, entry_gap_limit=_GAP_LIMIT,
                day_open=d_open, day_low=d_low, day_high=d_high)

            lows = db["low"].to_numpy(dtype=float)
            ei = None
            for j in range(len(lows)):
                if lows[j] <= trigger:
                    ei = j
                    break

            # ── lss として持った場合 ──
            lss_pnl = None
            fill_t = None
            if entry_fill is not None and ei is not None:
                fill_t = db.index[ei].time()
                xp, rsn = _lss_exit(db, ei, trigger, osp, target_p, d_close)
                lss_pnl = short_pnl(entry_fill, xp, rsn, QTY, FEE, 0.0)

            # ── 転換した場合 ──
            # ※ 既に load_intraday で読んだ db をそのまま渡す。_tks.simulate() だと
            #   同じpklをもう一度読むことになり、スレッド並列で pickle.load が競合して
            #   SystemError: deallocated bytearray object has exported buffers が出る。
            # 締切ごとに買い時刻を変えて計算する。
            # ⛔ 2026-08-08 まで買いは常に 09:09 固定だった。締切 09:10 以降の行は
            #    『まだ知らないこと(その時刻まで約定しなかった)』を使って09:09に買う
            #    = 時間逆行。実装可能なのは締切<=買い時刻の行だけだった。
            _atr = _atr_of(olp, osp, otp)
            tk_fixed = _tenkan(db, _TK_BUY, _atr)
            tk_by_cut: dict = {}
            for _lbl, _ct in CUTOFFS:
                tk_by_cut[_lbl] = (_tenkan(db, _ct, _atr)
                                   if args.buy_mode == "cutoff" else tk_fixed)
            tk_pnl = tk_fixed

            # BTスコア用(base=約定した分のlss損益)
            if lss_pnl is not None:
                bt_trades.append((fd, lss_pnl))

            local.append({
                "date": str(fd), "symbol": sym, "name": name, "strategy": strat,
                "fill_time": fill_t.strftime("%H:%M") if fill_t else "",
                "lss_pnl": lss_pnl, "tenkan_pnl": tk_pnl, "trigger": trigger,
                "tk_cut": tk_by_cut,
            })

        # BTスコアで足切り(compare_lss_rules と同じ算出)
        if args.bt_min > 0 and _bt_score(bt_trades) < args.bt_min:
            continue
        rows.extend(local)
    return rows


import time as _t
print(f"[窓] バックテスト {args.days + _EXTRA}日 "
      f"(--days {args.days} + BT用 {_EXTRA}日)  workers={args.workers}", flush=True)
if _EXTRA >= 400:
    print("   ※ 重い場合は --bt-window 90 で大幅に短縮できます"
          "(BTスコアの精度は落ちます)。--limit 100 で試し打ちも可。", flush=True)

ALL: list[dict] = []
_done = 0
_t0 = _t.time()
with ThreadPoolExecutor(max_workers=args.workers) as ex:
    futs = {ex.submit(_scan, s, n, st): s for s, n, st in PAIRS}
    for fu in as_completed(futs):
        try:
            ALL.extend(fu.result() or [])
        except Exception:
            pass
        _done += 1
        if _done % 25 == 0 or _done == len(PAIRS):
            _el = _t.time() - _t0
            _eta = _el / _done * (len(PAIRS) - _done)
            print(f"  ...{_done}/{len(PAIRS)}銘柄  シグナル{len(ALL)}件  "
                  f"経過{_el / 60:.1f}分 / 残り約{_eta / 60:.1f}分", flush=True)

if not ALL:
    print("[ERROR] シグナルが1件も取れませんでした。--days / --bt-min / 5分足データを確認してください。")
    sys.exit(1)

# 窓で絞る
_cut = (TODAY - pd.Timedelta(days=args.days)).date()
ALL = [r for r in ALL if str(r["date"]) >= str(_cut)]

n_fill = sum(1 for r in ALL if r["lss_pnl"] is not None)
n_tk = sum(1 for r in ALL if r["tenkan_pnl"] is not None)
print(f"\n[集計] シグナル {len(ALL)}件 / lss約定 {n_fill}件({n_fill / len(ALL) * 100:.1f}%) "
      f"/ 転換計算可 {n_tk}件", flush=True)


def _agg(mode_name, cutoff, label=None):
    """締切ごとに lss と 転換 を振り分けて集計する。

    転換の損益は **その締切時刻に買った場合**の値を使う(label 経由)。
    label が None の行(締切なし/全部転換)は --tk-buy 固定の値。
    """
    n_l = n_t = 0
    w = 0
    pnl = 0.0
    gp = gl = 0.0
    mon: dict = defaultdict(float)
    for r in ALL:
        use_lss = False
        if cutoff == "PURE":
            # 転換を一切しない。約定したものだけをlssとして計上する。
            if r["lss_pnl"] is None:
                continue
            use_lss = True
        elif cutoff is None:
            use_lss = r["lss_pnl"] is not None
        elif cutoff == _time(0, 0):
            use_lss = False                       # 全部転換
        else:
            use_lss = (r["lss_pnl"] is not None and r["fill_time"]
                       and r["fill_time"] <= cutoff.strftime("%H:%M"))
        if use_lss:
            p = r["lss_pnl"]
            n_l += 1
        else:
            p = (r["tk_cut"].get(label) if label is not None
                 else r["tenkan_pnl"])
            if p is None:
                continue
            n_t += 1
        pnl += p
        mon[str(r["date"])[:7]] += p
        if p > 0:
            w += 1
            gp += p
        else:
            gl += -p
    n = n_l + n_t
    return {"name": mode_name, "n": n, "lss": n_l, "tenkan": n_t,
            "wr": (w / n * 100 if n else 0),
            "pf": (gp / gl if gl > 0 else float("inf")),
            "pnl": pnl, "mon": mon}


RES = [_agg(nm, cu, nm if (nm, cu) in CUTOFFS else None) for nm, cu in MODES]
_base = RES[0]["pnl"]   # 純lss(転換なし)を基準にする

print("\n" + "=" * 96)
print("転換の締切時刻スイープ — この時刻までに約定しなければ転換(ロング)に切替")
print("=" * 96)
_oco = (f"損切ATR{args.tk_sm} / 利確ATR{args.tk_tm}"
        if (args.tk_sm > 0 or args.tk_tm > 0) else "OCOなし(時間決済のみ)")
print(f"  転換ルール: 買い={'締切時刻' if args.buy_mode == 'cutoff' else args.tk_buy} / "
      f"売り={args.tk_sell}以前の最後バー / {_oco}")
print(f"  母集団: BT>={args.bt_min:.0f} / 価格 {args.min_price:.0f}-{args.max_price:.0f} / "
      f"delay{args.stop_delay_bars} / 直近{args.days}日")
if args.buy_mode == "fixed":
    print("  " + "!" * 86)
    print("  ⛔ --buy-mode fixed: 買いは常に " + args.tk_buy + " です。")
    print("     それより後の締切行は『その時刻まで約定しなかった』という"
          "**まだ知らない情報**を使って買うことになります(時間逆行)。")
    print("     実装可能なのは 締切 <= 買い時刻 の行だけです。")
    print("  " + "!" * 86)
print("-" * 96)
print(f"{'締切':<22}{'合計':>6}{'lss':>6}{'転換':>6}{'勝率':>7}{'PF':>7}"
      f"{'総損益':>14}{'純lss比':>15}")
print("-" * 96)
for r in RES:
    pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
    d = r["pnl"] - _base
    mark = ("  ←基準" if r["name"].startswith("純lss")
            else ("  ←現行" if r["name"].startswith("締切なし") else ""))
    print(f"{r['name']:<22}{r['n']:>6}{r['lss']:>6}{r['tenkan']:>6}"
          f"{r['wr']:>6.1f}%{pf:>7}{r['pnl']:>13,.0f}円{d:>+13,.0f}円{mark}")

if args.by_month:
    months = sorted({m for r in RES for m in r["mon"]})
    print("\n【月別】")
    print(f"{'締切':<22}" + "".join(f"{m[2:]:>11}" for m in months))
    for r in RES:
        print(f"{r['name']:<22}" + "".join(f"{r['mon'].get(m, 0):>10,.0f}" for m in months))

_out = Path(f"analyze_tenkan_cutoff_{datetime.now().strftime('%Y-%m-%d')}.csv")
with open(_out, "w", newline="", encoding="utf-8-sig") as f:
    w_ = _csv.writer(f)
    w_.writerow(["cutoff", "trades", "lss_trades", "tenkan_trades",
                 "win_rate_pct", "pf", "total_pnl", "vs_pure_lss"])
    for r in RES:
        pf = 999.0 if r["pf"] == float("inf") else round(r["pf"], 2)
        w_.writerow([r["name"], r["n"], r["lss"], r["tenkan"], round(r["wr"], 1),
                     pf, round(r["pnl"], 0), round(r["pnl"] - _base, 0)])
print(f"\n[出力] {_out}")

# 約定時刻の分布(遅れ約定がどれだけ稼いでいるか)
print(f"\n【lss約定時刻 × 損益】遅い約定を捨ててよいかの判断材料"
      f"  ※転換列は {args.tk_buy} 買い固定の参考値")
print(f"{'約定時刻帯':<14}{'件数':>7}{'勝率':>7}{'lss損益':>14}{'同じ銘柄を転換した場合':>22}")
print("-" * 66)
BANDS = [("〜09:05", "09:05"), ("09:06〜09:10", "09:10"), ("09:11〜09:15", "09:15"),
         ("09:16〜09:30", "09:30"), ("09:31〜10:00", "10:00"),
         ("10:01〜11:00", "11:00"), ("11:01〜", "99:99")]
_prev = "00:00"
for lb, hi in BANDS:
    g = [r for r in ALL if r["lss_pnl"] is not None and r["fill_time"]
         and _prev < r["fill_time"] <= hi]
    _prev = hi
    if not g:
        continue
    lp = sum(r["lss_pnl"] for r in g)
    tp = sum(r["tenkan_pnl"] for r in g if r["tenkan_pnl"] is not None)
    wr = sum(1 for r in g if r["lss_pnl"] > 0) / len(g) * 100
    print(f"{lb:<14}{len(g):>7}{wr:>6.1f}%{lp:>13,.0f}円{tp:>21,.0f}円")
_nf = [r for r in ALL if r["lss_pnl"] is None]
if _nf:
    tp = sum(r["tenkan_pnl"] for r in _nf if r["tenkan_pnl"] is not None)
    print(f"{'終日不約定':<14}{len(_nf):>7}{'—':>7}{'—':>14}{tp:>21,.0f}円")
print("-" * 66)
print("※ 『同じ銘柄を転換した場合』が lss損益 を上回る時刻帯は、その帯で転換に切替える価値がある。")
