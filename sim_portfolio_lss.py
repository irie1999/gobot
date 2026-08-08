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

厳密OOS(基準月選定→翌月のみ検証):
  set LSS_TRADES_CSV=lss_trades.csv & python sim_portfolio_lss.py \
      --proposal lss_proposal_2026-06.py --only-month 2026-07 \
      --budget 4000000 --multiples 1.0,1.2,1.5 --workers 8
  # --proposal: scan_lss_universe.py が出力した SELECTED ファイル
  # --only-month: 基準月の翌月を指定 → 選定に使っていないデータで検証
  # bt-min フィルターは無効(提案ファイルが既に選定済み)
  # 複数基準月をまとめて確認したい場合は --only-month を外して全期間/TRAIN分割で比較
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
ap.add_argument("--stop-delay-bars", type=int, default=2,
                help="損切り遅延(5分足の本数)。既定2=delay2=ライブ(watch)と同じ。CLAUDE.md §18.9")
ap.add_argument("--qty", type=int, default=None, help="株数(既定=FIXED_QTY=100)")
ap.add_argument("--no-dedupe-symbol", action="store_true",
                help="同一銘柄が複数戦略で出た日を統合しない(=1銘柄複数ポジション許可)。既定=最高BTの1件に統合")
ap.add_argument("--by-multiple", action="store_true", help="倍率ごとに月次成績も出す")
ap.add_argument("--days", type=int, default=500)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--no-cache", action="store_true")
ap.add_argument("--refresh-cache", action="store_true")
ap.add_argument("--proposal", type=str, default=None,
                help="lss_proposal_YYYY-MM.py ファイル。SELECTED の (code,strat) ペアのみを対象にする。"
                     "--bt-min フィルターより優先(提案ファイルが選定基準を持つ)。"
                     "厳密OOS検証: --only-month <翌月> と組み合わせる。"
                     "例: --proposal lss_proposal_2026-06.py --only-month 2026-07")
ap.add_argument("--start-dates", type=str, default="lss_proposal_cumul.py",
                help="START_DATES を持つ提案ファイル。各ペアが『いつからWATCHLISTに"
                     "居たか』で注文を切り、選定の先読みを除く(CLAUDE.md 18.11 と同型)。"
                     "空文字で無効化(絶対値は上振れするので相対比較専用)")
ap.add_argument("--bt-mode", type=str, default="asof", choices=["asof", "static"],
                help="発注順/下限に使うBTの取り方。asof(既定)=lss_trades.csv の"
                     "**その注文日**の as-of BT を使う(先読みなし)。"
                     "static=(銘柄,戦略)ごとの期間最大値を全日に適用する旧挙動。"
                     "旧挙動は『6月にBT80になったペアを1月の注文でも最優先』にするので"
                     "先読み。2026-08-08 まで static だった。")
ap.add_argument("--universe", type=str, default="selected",
                choices=["selected", "all"],
                help="発注候補の母集団。selected(既定)=lss_trades.csv の選定済みペア。"
                     "all=5分足のある全銘柄×全6戦略(WF選定を捨てる。18.20で選定は"
                     "何も足していないと確定したため、母集団を約2.5倍に広げられる)")
ap.add_argument("--rank", type=str, default="bt",
                choices=["bt", "liquidity", "random"],
                help="予算内で上から埋める順番。bt(既定)=BT降順。"
                     "liquidity=前日売買代金の大きい順(=執行コストの小さい順)。"
                     "random=対照(順序に意味があるか自体を検証する)。"
                     "18.12/18.13でリターンは予測できないと確定しているので、"
                     "予測できるコストで並べるのが liquidity の狙い。")
ap.add_argument("--rank-seed", type=int, default=42, help="--rank random の再現用シード")
ap.add_argument("--only-month", type=str, default=None,
                help="この月(YYYY-MM)のトレードのみでシミュレーション。"
                     "--proposal と組み合わせて「基準月選定→翌月のみ検証」の厳密OOSを測る。"
                     "例: --proposal lss_proposal_2026-06.py --only-month 2026-07")
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


# ── as-of BT: (sym, strat, 注文日) → その時点のBT ───────────────────────
# ⛔ 2026-08-08 まで sim は `max()` で期間全体の最大BTを取り、それを全日の
#    発注順・BT下限に使っていた。「6月にBT80になったペアが1月の注文でも最優先」
#    = 先読み。lss_trades.csv は entry_date ごとに as-of BT を持っているので、
#    日付キーで引けば先読みなしにできる。
_BT_BY_DATE: dict = {}


def _load_bt_by_date() -> dict:
    p = Path(args.trades_csv)
    if not p.exists():
        return {}
    out = {}
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        sym = _norm(r.get("symbol") or r.get("code") or "")
        strat = str(r.get("strategy") or "").strip()
        ed = str(r.get("entry_date") or "").strip()[:10]
        if not (sym and strat and ed):
            continue
        try:
            out[(sym, strat, ed)] = float(r.get("bt") or 0)
        except Exception:
            pass
    return out


def _bt_for(sym: str, strat: str, edate, static_bt: float) -> float:
    """その注文日のBT。asof モードで履歴が無ければ 0(=未実証) として先読みを避ける。"""
    if args.bt_mode != "asof":
        return static_bt
    return _BT_BY_DATE.get((sym, strat, str(edate)[:10]), 0.0)


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
    if _lprio is not None and _lprio.enabled():
        # 戦略別のBT下限(A7/RSI2/VOLTF/MACDTF=20, MOM/DON=40)。キーは (sym, strat)。
        return {k: v for k, v in bt.items()
                if _lprio.is_orderable(k[1], v, args.bt_min)}
    return {k: v for k, v in bt.items() if v >= args.bt_min}


# ── 発注優先順位 / 戦略別BT下限 (lss_priority) ──────────────────────
# 既定は無効 = 従来どおり「一律 --bt-min + BT降順」。LSS_PRIORITY=1 で有効化。
try:
    import lss_priority as _lprio
except Exception:
    _lprio = None


def _prio_key(rec):
    """発注順のキー(小さいほど先)。

    ⛔ 18.12/18.13 で BT も銘柄属性も『将来リターンの識別力ゼロ』と確定した。
       リターンが予測できずコストは予測できる以上、**執行コストの小さい順**
       (=流動性の高い順)に並べるのが合理的。--rank liquidity がそれ。
       --rank random は対照: これと差が無ければ『並び順そのものに意味が無い』
       =予算上限は単なるランダム抽出、と分かる。
    """
    if args.rank == "liquidity":
        # 前日売買代金(円)の降順。同値は銘柄コードで安定化。
        return (0.0, -float(rec.get("liq") or 0.0), rec.get("sym", ""))
    if args.rank == "random":
        import hashlib as _hh
        _k = f"{args.rank_seed}|{rec.get('sym','')}|{rec.get('strat','')}|{rec.get('date')}"
        return (0.0, int(_hh.md5(_k.encode()).hexdigest()[:8], 16), "")
    if _lprio is None:
        return (0.0, -float(rec.get("bt") or 0))
    return _lprio.priority_key(rec.get("strat", ""), float(rec.get("bt") or 0))


def _load_all_bt() -> dict:
    """lss_trades.csv から全 (sym,strat) のBT値を返す(bt_min フィルターなし)。
    proposal モードで BT順ソートの重みとして使う。未登録ペアは 0.0 で補完される。"""
    p = Path(args.trades_csv)
    if not p.exists():
        return {}
    bt: dict = {}
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
    return bt


def _load_start_dates(path: str) -> dict[tuple[str, str], "pd.Timestamp"]:
    """提案ファイルの START_DATES = {(code, strat): "YYYY-MM-DD"} を読む。

    merge_lss_proposals.py が per-symbol で書き出す『そのペアの初出基準月の翌月1日』。
    これより前の注文は、当時まだ WATCHLIST に入っていなかったので数えてはいけない。
    ファイルが無い/キーが無い場合は空 dict を返す(=フィルターしない)。
    """
    if not str(path or "").strip():
        return {}
    p = Path(path)
    if not p.exists():
        print(f"[START_DATES] {p} が無いのでスキップします "
              f"(.\\daily を1回流すと lss_proposal_cumul.py ができます)")
        return {}
    try:
        ns: dict = {}
        exec(compile(p.read_text(encoding="utf-8"), str(p), "exec"), ns)
        raw = ns.get("START_DATES") or {}
    except Exception as e:
        print(f"[START_DATES] {p} を読めません: {e}")
        return {}
    out: dict = {}
    for k, v in raw.items():
        try:
            code, strat = k
            out[(str(code).upper().removesuffix(".T").split(".")[0], str(strat))] = \
                pd.Timestamp(v).normalize()
        except Exception:
            continue
    return out


def _load_proposal_pairs(path: str) -> set[tuple[str, str]]:
    """lss_proposal_YYYY-MM.py の SELECTED=[(code,name,strat),...] を読んで (sym,strat) の集合を返す。"""
    import runpy
    ns = runpy.run_path(path)
    sel = ns.get("SELECTED")
    if not sel:
        print(f"[error] {path} に SELECTED が見つかりません", file=sys.stderr)
        return set()
    out: set = set()
    for row in sel:
        if len(row) >= 3:
            code = _norm(str(row[0]))
            strat = str(row[2]).strip()
            out.add((code, strat))
    return out


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
        # 前日売買代金 = 発注前に分かる値なので先読みではない。--rank liquidity で使う。
        try:
            _liq = float(row.get("volume", 0) or 0) * prev_close
        except Exception:
            _liq = 0.0
        _bt_here = _bt_for(sym, strat, edate, bt)
        if args.bt_mode == "asof" and args.universe != "all" and _bt_here < args.bt_min:
            continue        # その日のas-of BTが下限未満 = 当時は発注対象でなかった
        rec = {"date": pd.Timestamp(edate), "sym": sym, "bt": _bt_here, "strat": strat,
               "liq": _liq,
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
    """1倍率のポートフォリオ結果を返す。各日: 優先順(既定BT降順 / LSS_PRIORITY=1 で
    戦略×BT帯の期待値降順)で M×予算ぶん注文→約定を時刻順→予算到達でキャンセル。"""
    budget = args.budget
    cap = budget * mult
    by_date = {}
    for r in records:
        by_date.setdefault(r["date"], []).append(r)

    days = []   # per-day dict: date, pnl, deployed, peak, ntaken, fully
    for d, recs in by_date.items():
        recs = sorted(recs, key=_prio_key)
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
    # ── ペア集合の決定 ────────────────────────────────────────────────────
    if args.proposal:
        prop_pairs = _load_proposal_pairs(args.proposal)
        if not prop_pairs:
            print("[error] proposal ファイルの SELECTED が空", file=sys.stderr); return
        all_bt = _load_all_bt()
        keep = {pair: all_bt.get(pair, 0.0) for pair in prop_pairs}
        print(f"[info] --proposal {Path(args.proposal).name}: {len(keep)}ペア "
              f"(bt-min フィルター無効・BT順ソートのみ lss_trades.csv 参照)")
    elif args.universe == "all":
        # 選定を捨てて全銘柄×全6戦略。18.20 で『信号のみ』(選定なし)が
        # 『信号+選定+予算』とほぼ同じ期待値だと確定したため、母集団を広げる方が
        # 容量・維持コストとも良い。BT値は並び順(--rank bt)のためだけに引く。
        from daytrade_data import available_local_symbols as _als
        all_bt = _load_all_bt()
        _strats = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]
        _seen = set(); keep = {}
        for _s5 in _als():
            _c = _norm(_jq_to_yf(_s5))
            if _c in _seen:
                continue
            _seen.add(_c)
            for _t in _strats:
                keep[(_c, _t)] = all_bt.get((_c, _t), 0.0)
        print(f"[info] --universe all: 全{len(_seen)}銘柄 × {len(_strats)}戦略 "
              f"= {len(keep)}ペア (WF選定を使わない / --bt-min は無視)")
    else:
        keep = _load_bt_pairs()
        if not keep:
            print("[error] BT対象ペアが0。", file=sys.stderr); return

    pairs = sorted(keep.keys())
    if args.limit > 0:
        pairs = pairs[:args.limit]

    mode_label = f"proposal={Path(args.proposal).name}" if args.proposal else f"BT{args.bt_min:.0f}以上"
    if _lprio is not None:
        print(f"[lss] {_lprio.describe()}")
    only_month_label = f" / 検証月={args.only_month}" if args.only_month else ""
    print(f"[info] {mode_label} {len(pairs)}ペア / 予算{args.budget/1e4:.0f}万 / "
          f"倍率{MULTIPLES} / 価格{args.min_price:.0f}-{args.max_price:.0f}円 / "
          f"sm{args.sm} tm{args.tm} delay{DELAY} 株数{QTY} 指値ガード{GAP_LIMIT*100:.0f}% slip0 "
          f"/ 銘柄統合{'OFF' if args.no_dedupe_symbol else 'ON'}{only_month_label}")
    global _BT_BY_DATE
    if args.bt_mode == "asof":
        _BT_BY_DATE = _load_bt_by_date()
        print(f"[info] BT取り方=asof: {len(_BT_BY_DATE):,}件の(銘柄,戦略,日付)別BTを読み込み"
              f" (先読みなし)")
    else:
        print(f"[info] ⚠ BT取り方=static: 期間最大BTを全日に適用します = **先読み**")
    print(f"[info] 母集団={args.universe} / 発注順={args.rank}"
          f"{f'(seed{args.rank_seed})' if args.rank == 'random' else ''}")

    import hashlib as _h, pickle as _pk
    _cd = Path(".simportfolio_cache")
    _prop_tag = _h.md5(Path(args.proposal).read_bytes()).hexdigest()[:8] if args.proposal else "nopr"
    _key = _h.md5("|".join(str(x) for x in [
        # FEE を鍵に含める: 2026-08-07 に既定を 0.001→0 に変えたので、
        # 含めないと手数料込みの古いキャッシュが再利用されて誤った金額が出る。
        # 損切り約定モデル(v16: max(stop, bar_open))も鍵に含める。
        "spv6", getattr(ble, "_BT_LOGIC_VER", "?"), FEE, args.universe, args.bt_mode,
        getattr(__import__("sameday5m_firsttouch"), "_OPTIMISTIC_STOP_FILL", None),
        args.sm, args.tm, DELAY, GAP_LIMIT,
        args.days, args.bt_min, args.limit, QTY, args.min_price, args.max_price,
        _prop_tag,
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

    # ── START_DATES フィルター (選定の先読み除去) ─────────────────────────
    # ⛔ なぜ必要か (2026-08-07):
    #   lss_proposal_cumul.py は 2025-09〜2026-07 の全提案の和集合。ここから
    #   1,326ペアを取って500日まるごと評価すると、**2026-06 の選定で初めて入った
    #   銘柄で 2025-04 の取引を評価する**ことになる。2025-04 時点でその銘柄は
    #   WATCHLIST に存在しない。「6月時点で良かったから選ばれた」= その間の成績が
    #   良かった、という未来情報で過去を評価していることになる。
    #   これは as-of BT (CLAUDE.md 18.11) と同じ構造のリークで、merge_lss_proposals
    #   側は per-symbol START_DATES で修正済み。このツールだけ取り残されていた。
    #   実測の食い違い: .\daily 予算タブ(リーク除去済み) 11ヶ月 -114,595円 に対し、
    #   本ツールは +1,226,598円。絶対値がこれだけずれる。
    #   sm/delay の**相対比較**は同じリークが等しくかかるので有効だが、
    #   絶対値を見るときは必ずこのフィルターを通すこと。
    _sd_map = _load_start_dates(args.start_dates)
    if _sd_map:
        _b4 = len(records)
        records = [r for r in records
                   if r["date"] >= _sd_map.get(
                       (str(r["sym"]).upper().removesuffix(".T").split(".")[0],
                        str(r.get("strat", ""))), pd.Timestamp.min)]
        print(f"[START_DATES] {Path(args.start_dates).name} の {len(_sd_map)}件を適用 → "
              f"{_b4}注文 → {len(records)}注文 (選定前に遡った注文を除外)")
        if not records:
            print("[error] START_DATES で全注文が消えました。--start-dates '' で無効化できます",
                  file=sys.stderr)
            return
    else:
        print(f"[START_DATES] 未適用。**選定の先読みが残ります**(絶対値は上振れ)。"
              f" 相対比較にのみ使うこと")

    # ── --only-month フィルター ───────────────────────────────────────────
    om_period = None
    if args.only_month:
        try:
            om_period = pd.Period(args.only_month, "M")
            om_start = om_period.start_time.normalize()
            om_end   = om_period.end_time.normalize()
            before = len(records)
            records = [r for r in records if om_start <= r["date"] <= om_end]
            print(f"[info] --only-month {args.only_month}: {before}注文 → {len(records)}注文に絞り込み")
        except Exception as e:
            print(f"[warn] --only-month パース失敗({e})、フィルターをスキップ")

    n_fill = sum(1 for r in records if r["filled"])
    on = sum(r["order_notional"] for r in records)
    fn = sum(r["fill_notional"] for r in records)
    print("\n" + "=" * 96)
    hdr_suffix = f"  【OOS月: {args.only_month}】" if om_period else ""
    print(f"【全注文の約定率(母数チェック)】{hdr_suffix}")
    if on > 0:
        print(f"  注文 {len(records)}件 / 約定 {n_fill}件({n_fill/len(records)*100:.0f}%)  "
              f"/ 金額ベース約定率 {fn/on*100:.0f}%(注文額{on/1e8:.2f}億 → 約定額{fn/1e8:.2f}億)")
    else:
        print(f"  注文 {len(records)}件 / 約定 {n_fill}件  注文額0(価格レンジ外 or データなし)")
    print("  ※金額ベース約定率 ≒ 倍率1.0(予算ぴったり注文)時の稼働率の目安。ユーザー実測~50%と比較。")

    bm = args.base_month.strip()
    be = None
    if bm and not om_period:   # --only-month 指定時は TRAIN/TEST 分割を表示しない
        try:
            be = pd.Period(bm, "M").end_time.normalize()
        except Exception:
            be = None

    # 倍率ごとにシミュレーション
    print("\n" + "=" * 96)
    _mode_note = (f"proposal={Path(args.proposal).name}" if args.proposal
                  else f"BT{args.bt_min:.0f}以上")
    _month_note = f"  OOS月={args.only_month}" if om_period else ""
    print(f"【倍率別サマリー】予算{args.budget/1e4:.0f}万・約定累計が予算到達で残り注文キャンセル(live上限管理)"
          f"  [{_mode_note}{_month_note}]")
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
