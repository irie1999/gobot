r"""compare_lss_stop_1m.py — lssの損切りルールを『1分足』で比較する(#6e)。

ユーザーの理想(2026-07-30): 「一瞬のヒゲで損切りたくない。1分間ぐらい損切ライン超が続いたら損切る」。
5分足では検証できなかった(5分終値=最大5分の猶予/touch=一瞬)。1分足取得(fetch_1m_all)が完了したので、
1分足で「K本(=K分)連続で終値が損切りラインを超えたら損切る」を実装し、以下を比較する:

  touch(即時)   : 1分足の高値が損切りに触れた瞬間に損切り(ヒゲでも発火。最もタイト)。
  1分持続       : 1分足の終値が損切り超で1本 → 損切り(ユーザーの理想。ヒゲ1本は回避)。
  2分/3分/5分持続: K本連続で終値が損切り超 → 損切り(猶予を伸ばす)。5分持続≒5分終値。
  delay1(寄5分stopなし): 寄りから5分(=5本)は損切り無効、以降touch(現行の実運用/BT既定の1分近似)。

信頼性(前回の教訓): エントリーは本番同等(min(トリガー,始値)+-3%指値ガード)。決済のstopルールだけ変える。
BTスコアは LSS_TRADES_CSV の bt列から(=レポートと一致)。BT>=--bt-min のペアのみ。OOS(--base-month)分割。
「持続で損切り=exitは確定バーの終値(=待った分だけ不利)」で計上=楽観にしない。

前提: fetch_1m_all.py で1分足を取得済(~/.jquants_cache/minute/<code>_1m.pkl)。先に .\daily で lss_trades.csv(BT列)。

使い方(あなたの機械で):
  set LSS_TRADES_CSV=lss_trades.csv & python compare_lss_stop_1m.py --bt-min 40 --workers 8
  python compare_lss_stop_1m.py --bt-min 40 --base-month 2026-01
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss損切りルールを1分足で比較(touch/1分持続/…/delay1)")
ap.add_argument("--trades-csv", type=str,
                default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"),
                help="BT列のソース(レポート出力の取引CSV)")
ap.add_argument("--bt-min", type=float, default=40.0, help="対象BTスコア下限(既定40)")
ap.add_argument("--base-month", type=str, default="2026-01",
                help="OOS分割の基準月(以前=TRAIN/in-sample、以後=TEST/OOS)。空=全期間のみ")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(lss既定0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(lss既定1.0)")
ap.add_argument("--days", type=int, default=500, help="遡及日数")
ap.add_argument("--gap", type=float, default=None,
                help="-X%%指値ガード(既定=_INTRADAY_5M_ENTRY_GAP_LIMIT=3%%)")
ap.add_argument("--limit", type=int, default=0, help="先頭Nペアだけ(デバッグ)")
ap.add_argument("--qty", type=int, default=None)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--no-cache", action="store_true")
ap.add_argument("--refresh-cache", action="store_true")
args = ap.parse_args()

import backtest_limit_entry as ble
from jquants_fetch import _load_pkl, _cache_path, MINUTE_CACHE_DIR, yf_to_jquants
from daytrade_data import split_by_day

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._INTRADAY_5M = False

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE = ble.FEE_PCT_ONE_WAY
SLIP = getattr(ble, "_INTRADAY_5M_SLIP", 0.0)
GAP = args.gap if args.gap is not None else getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.03)

# 比較する損切りルール。 (表示名, 種別, K, 遅延本数)
#   touch : 高値が損切り到達で即損切り(遅延=delay分だけ寄り無効)
#   sustain: 終値が損切り超をK本連続で損切り(exitは確定バー終値=不利側)
RULES = [
    ("touch(即時)",        "touch",   0, 0),
    ("1分持続",            "sustain", 1, 0),
    ("2分持続",            "sustain", 2, 0),
    ("3分持続",            "sustain", 3, 0),
    ("5分持続",            "sustain", 5, 0),
    ("delay1(寄5分stopなし)", "touch", 0, 5),
]


def _norm(sym: str) -> str:
    return str(sym).upper().removesuffix(".T").split(".")[0]


def _jq_to_yf(code: str) -> str:
    c = str(code).strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _load_bt_pairs() -> dict:
    p = Path(args.trades_csv)
    if not p.exists():
        print(f"[error] BTソースCSVが無い: {p} (先に .\\daily で lss_trades.csv 作成)", file=sys.stderr)
        return {}
    bt: dict = {}; name: dict = {}
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
        if r.get("name"):
            name[sym] = r.get("name")
    _load_bt_pairs.name = name
    return {k: v for k, v in bt.items() if v >= args.bt_min}


def _load_1m(sym_yf: str):
    """1分足キャッシュを直読み(ネット不要)。DatetimeIndex + ohlcv。無ければ None。"""
    try:
        cp = _cache_path(MINUTE_CACHE_DIR, f"{yf_to_jquants(sym_yf)}_1m")
        return _load_pkl(cp)
    except Exception:
        return None


def _entry_1m(bars, trigger):
    """lss(逆指値売り)の約定を1分足で。fill=min(トリガー,始値)+ -GAP%指値ガード。
    Returns (fill_price, entry_bar_index) or (None, None)。"""
    opens = bars["open"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    n = len(opens)
    if n < 2:
        return None, None
    o = float(opens[0])
    if o <= trigger:                       # 寄りがトリガー以下(ギャップダウン) → 始値約定
        if o < trigger * (1.0 - GAP):
            return None, None              # ギャップ過大 → 約定不可(指値下限)
        return o, 0
    for j in range(n):                     # 寄りがトリガー超 → ザラ場で下抜けた瞬間に約定
        if lows[j] <= trigger:
            return trigger, j
    return None, None                      # 終日トリガー未達


def _exit_short(bars, ebar, stop_p, target_p, kind, K, delay):
    """ショート決済。stop=上/target=下。Returns (exit_price, reason)。
    touch: 高値≥stop で即損切り(exit=stop)。sustain: 終値≥stop がK本連続で損切り(exit=確定バー終値)。
    利確は常に 安値≤target で target。同バーは損切り優先(touch)/持続はバー内利確優先。"""
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    n = len(highs)
    sustain = 0
    stop_from = ebar + max(0, delay)       # 遅延分は損切り無効
    for j in range(ebar, n):
        if kind == "touch":
            if j >= stop_from and highs[j] >= stop_p:   # 同バー損切り優先(悲観)
                return stop_p, "stop"
            if lows[j] <= target_p:
                return target_p, "target"
        else:  # sustain
            if lows[j] <= target_p:                     # バー内で利確に触れたら利確優先
                return target_p, "target"
            if j >= stop_from and closes[j] >= stop_p:  # 終値が損切り超 → 連続カウント
                sustain += 1
                if sustain >= K:
                    return float(closes[j]), "stop"     # 確定バー終値で損切り(待った分だけ不利)
            else:
                sustain = 0
    return float(closes[-1]), "close"                   # 引け(タイムカット)


def _pnl(entry_p, exit_p, qty) -> float:
    fee = (entry_p + exit_p) * qty * FEE
    return (entry_p - exit_p) * qty - fee                # ショート: 売り-買戻


def _collect(sym_yf: str, strat: str) -> list[dict]:
    """1(銘柄,戦略)の生シグナル(トリガー/損切/利確/約定日)+ その日の1分足 を返す。"""
    out = []
    params = None
    from sameday5m_core import mod_for
    params = getattr(mod_for(strat), "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return out
    cf = params[0]
    m1 = _load_1m(sym_yf)
    if m1 is None or m1.empty:
        return out
    by_day = split_by_day(m1)
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym_yf, args.days + 420)
        df_ind = cf(df_raw.copy())
        r = ble.run_limit_backtest(sym_yf, sym_yf, df_ind, lambda d: d, 0.0,
                                   args.sm, args.tm, args.days + 420, strat,
                                   entry_type="stop_sell", max_hold=0)
    except Exception:
        return out
    if not r:
        return out
    for t in r.get("trade_log", []):
        if t.get("reason") in ("発注中", "保有中"):
            continue
        edt = t.get("entry_dt")
        if edt is None:
            continue
        lp = float(t.get("order_limit", 0) or 0)
        osp = float(t.get("order_stop", 0) or 0)
        otp = float(t.get("order_target", 0) or 0)
        if lp <= 0 or osp <= 0 or otp <= 0:
            continue
        fd = edt.date() if hasattr(edt, "date") else edt
        db = by_day.get(fd)
        if db is None or len(db) < 2:
            continue
        out.append({"fd": pd.Timestamp(fd), "lp": lp,
                    "stop": max(osp, otp), "target": min(osp, otp),
                    "db": db[["open", "high", "low", "close"]].copy()})
    return out


def _stat(pnls):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "pnl": sum(pnls), "pf": pf, "wr": len(wins) / n * 100}


def _fmt(s):
    if not s:
        return "  取引なし"
    _pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return f"{s['n']:>6}件  損益{s['pnl']:>+13,.0f}  勝率{s['wr']:>4.0f}%  PF{_pf:>5}"


def _rule_pnls(sigs, kind, K, delay):
    out = []
    for s in sigs:
        db = s["db"]
        fill, ebar = _entry_1m(db, s["lp"])
        if fill is None:
            continue
        xp, _rsn = _exit_short(db, ebar, s["stop"], s["target"], kind, K, delay)
        out.append(_pnl(fill, xp, QTY))
    return out


def _report(sigs, title):
    print("\n" + "=" * 74)
    print(f"【{title}】 約定候補 {len(sigs)}シグナル")
    print("=" * 74)
    if not sigs:
        print("  (該当なし)"); return
    print("  損切りルール             総損益・勝率・PF")
    print("-" * 74)
    best = None
    for (lbl, kind, K, delay) in RULES:
        st = _stat(_rule_pnls(sigs, kind, K, delay))
        if st and (best is None or st["pnl"] > best[1]):
            best = (lbl, st["pnl"])
        print(f"  {lbl:<22} {_fmt(st)}")
    if best:
        print("-" * 74)
        print(f"  → 総損益 最大: {best[0]}")


def main():
    keep = _load_bt_pairs()
    if not keep:
        print("[error] BT対象ペアが0。", file=sys.stderr); return
    names = getattr(_load_bt_pairs, "name", {})
    pairs = sorted(keep.keys())
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] BT{args.bt_min:.0f}以上 {len(pairs)}ペア / sm={args.sm} tm={args.tm} "
          f"gap={GAP*100:.0f}% slip{SLIP*100:.1f}% fee{FEE*100:.2f}% / 決済=1分足・エントリー本番同等")

    import hashlib as _h, pickle as _pk
    _cd = Path(".stop1m_cache")
    _key = _h.md5("|".join(str(x) for x in [
        "stop1mv1", getattr(ble, "_BT_LOGIC_VER", "?"), args.sm, args.tm, GAP, args.days,
        args.bt_min, args.limit, QTY,
        _h.md5(",".join(f"{s}:{t}" for s, t in pairs).encode()).hexdigest(),
    ]).encode()).hexdigest()[:16]
    _cf = _cd / f"sigs_{_key}.pkl"

    sigs = None
    if _cf.exists() and not args.no_cache and not args.refresh_cache:
        try:
            sigs = _pk.loads(_cf.read_bytes())
            print(f"[cache] 再利用: {_cf} ({len(sigs)}シグナル) ※--refresh-cacheで再計算")
        except Exception:
            sigs = None
    if sigs is None:
        sigs = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_collect, _jq_to_yf(s), t): (s, t) for (s, t) in pairs}
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 100 == 0:
                    print(f"  ...{done}/{len(pairs)}ペア", flush=True)
                try:
                    sigs += fut.result()
                except Exception:
                    continue
        if not args.no_cache:
            try:
                _cd.mkdir(exist_ok=True)
                _cf.write_bytes(_pk.dumps(sigs))
                print(f"[cache] 保存: {_cf} ({len(sigs)}シグナル)")
            except Exception as _e:
                print(f"[cache] 保存失敗({_e})")

    if not sigs:
        print("[error] シグナル0件。1分足キャッシュ(fetch_1m_all)を確認。", file=sys.stderr); return

    _report(sigs, f"BT{args.bt_min:.0f}以上・全期間(in-sample含む)")
    bm = args.base_month.strip()
    if bm:
        try:
            be = pd.Period(bm, "M").end_time.normalize()
            _report([s for s in sigs if s["fd"] <= be], f"TRAIN ≤{be.date()} (in-sample)")
            _report([s for s in sigs if s["fd"] > be], f"TEST >{be.date()} (OOS)")
        except Exception as _e:
            print(f"[warn] base-month分割スキップ({_e})")

    print("\n" + "=" * 74)
    print("判断: TEST(OOS)で『1分持続』が touch/delay1 を総損益で上回れば採用価値あり。")
    print("  持続の損切りexitは確定バー終値(=待った分だけ不利)で計上済=楽観でない。")
    print("  採用時は sameday5m_firsttouch.short_exit_5m と lss_exit_watcher を1分持続対応にして揃える。")


if __name__ == "__main__":
    main()
