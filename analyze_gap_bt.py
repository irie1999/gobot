r"""analyze_gap_bt.py — 指値ガード%の最適値を『BT40以上・全取引・OOS・本番同等の約定』で調べる専用ツール。

これは analyze_gap_fills.py の後継(信頼版)。前版は決済を自作再実装していたため本番エンジンと
最適%がズレた(2026-07-31: 「2%最良」と出たが本番では2%が微減)。本版は:
  * 約定/決済を **本番エンジンと同一の呼び出し** で行う
    (short_entry_fill_5m に day_open/low/high、short_exit_5m に on_close/include_entry_bar/
     stop_delay_bars、pnl は short_pnl。slip=_INTRADAY_5M_SLIP・fee=FEE_PCT_ONE_WAY)。
  * **BTスコアはレポート出力CSV(LSS_TRADES_CSV)の bt 列** から取る(=本番のBTと完全一致)。
    → BT40以上の(銘柄,戦略)ペアだけを対象にする(=実際に発注する品質帯)。
  * **全取引(予算キャップなし)** で集計(深ギャップ玉のサンプルを厚く)。
  * **OOS分割**(--base-month。TRAIN=in-sample / TEST=擬似未来)。

「ガード=X%」は「寄りが -X% 超に飛んだ取引を約定不可=取り消す」の意味。よってガード%スイープ=
『何%超の下げで取引ごと取り消すべきか』の調査そのもの。各Xでの総損益/勝率/PFを比べ、最大の%を選ぶ。

前提: 一度 .\daily を回して LSS_TRADES_CSV(既定 lss_trades.csv)を作っておく(BT列のため)。

使い方(あなたの機械で・5分足が必要):
  set LSS_TRADES_CSV=lss_trades.csv
  python analyze_gap_bt.py --bt-min 40 --source local --workers 8
  python analyze_gap_bt.py --bt-min 40 --base-month 2026-01     # OOS分割の基準月
  python analyze_gap_bt.py --bt-min 30                          # BT30帯も見る
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="指値ガード%最適化(BT40以上・全取引・OOS・本番同等約定)")
ap.add_argument("--trades-csv", type=str,
                default=os.environ.get("LSS_TRADES_CSV", "lss_trades.csv"),
                help="BT列のソース(レポートが出力する取引CSV。既定=LSS_TRADES_CSV or lss_trades.csv)")
ap.add_argument("--bt-min", type=float, default=40.0, help="対象にするBTスコア下限(既定40)")
ap.add_argument("--base-month", type=str, default="2026-01",
                help="OOS分割の基準月(この月末以前=TRAIN/in-sample、以後=TEST/OOS)。空=全期間のみ")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(lss既定0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(lss既定1.0)")
ap.add_argument("--stop-delay-bars", type=int, default=1, help="損切り遅延(既定1=delay1=実運用)")
ap.add_argument("--days", type=int, default=500, help="遡及日数")
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--limit", type=int, default=0, help="先頭Nペアだけ(デバッグ)")
ap.add_argument("--qty", type=int, default=None)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--no-cache", action="store_true")
ap.add_argument("--refresh-cache", action="store_true")
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_entry_fill_5m, short_exit_5m, short_pnl

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False   # 生シグナル抽出用(5分足フィルタは後段で明示適用)

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY
SLIP = getattr(ble, "_INTRADAY_5M_SLIP", 0.0)
ON_CLOSE = getattr(ble, "_INTRADAY_5M_ON_CLOSE", False)
DELAY = max(0, int(args.stop_delay_bars))

# ガード閾値スイープ (None=無制限)。3%周辺を細かく。
GUARDS = [0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.10, None]
GLABEL = {0.015: "1.5%", 0.02: "2%", 0.025: "2.5%", 0.03: "3%",
          0.04: "4%", 0.05: "5%", 0.10: "10%", None: "無制限"}


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
    """LSS_TRADES_CSV から (正規化コード, 戦略) -> BT(最大) を作り、bt>=bt_min を返す。"""
    p = Path(args.trades_csv)
    if not p.exists():
        print(f"[error] BTソースCSVが無い: {p} (先に .\\daily を回して LSS_TRADES_CSV を作成)",
              file=sys.stderr)
        return {}
    bt: dict = {}
    name: dict = {}
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        sym = _norm(r.get("symbol") or r.get("code") or "")
        strat = str(r.get("strategy") or r.get("strat") or "").strip()
        if not sym or not strat:
            continue
        try:
            b = float(r.get("bt") or r.get("BT") or 0)
        except Exception:
            b = 0.0
        k = (sym, strat)
        bt[k] = max(bt.get(k, -1e9), b)
        if r.get("name"):
            name[sym] = r.get("name")
    keep = {k: v for k, v in bt.items() if v >= args.bt_min}
    _load_bt_pairs.name = name
    return keep


def _collect_signals(sym_yf: str, name: str, strat: str) -> list[dict]:
    """1(銘柄,戦略)の生シグナル(トリガー/損切/利確/約定日/その日の日足OHLC/5分足)を返す。
    5分足フィルタは掛けず、後段でガード別に short_entry_fill_5m を適用する。"""
    out = []
    params = getattr(mod_for(strat), "STRATEGY_PARAMS", {}).get(strat)
    if not params:
        return out
    cf = params[0]
    try:
        m5 = load_intraday(sym_yf, days=args.days + 5, source=args.source)
    except Exception:
        return out
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym_yf, args.days + 420)
    except Exception:
        return out
    if df_raw is None or df_raw.empty:
        return out
    try:
        df_ind = cf(df_raw.copy())
    except Exception:
        return out
    # 約定日ごとの日足OHLC(エンジンと同じく day_open/low/high/close を渡すため)
    day_ohlc = {}
    for d in df_raw.index:
        dd = d.date() if hasattr(d, "date") else d
        try:
            day_ohlc[dd] = (float(df_raw.loc[d, "open"]), float(df_raw.loc[d, "low"]),
                            float(df_raw.loc[d, "high"]), float(df_raw.loc[d, "close"]))
        except Exception:
            pass
    try:
        r = ble.run_limit_backtest(sym_yf, name, df_ind, lambda d: d, 0.0,
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
        do, dl, dh, dc = day_ohlc.get(fd, (None, None, None, None))
        out.append({
            "sym": _norm(sym_yf), "strat": strat, "fd": pd.Timestamp(fd),
            "lp": lp, "stop": max(osp, otp), "target": min(osp, otp),
            "do": do, "dl": dl, "dh": dh, "dc": dc,
            "db": db[["open", "high", "low", "close"]].copy(),  # 5分足(OHLC)を直接保持
        })
    return out


def _pnl_for(sig: dict, guard) -> float | None:
    """本番エンジンと同一手順で 1シグナルの同日損益を返す。約定しなければ None。"""
    db = sig["db"]
    lp, stop_p, target_p = sig["lp"], sig["stop"], sig["target"]
    ef = short_entry_fill_5m(db, lp, False, entry_gap_limit=guard,
                             day_open=sig["do"], day_low=sig["dl"], day_high=sig["dh"])
    if ef is None:
        return None    # ガード超 or 終日未達 → 約定不可(=取り消し扱い)
    gap_entry = abs(ef - lp) > 1e-9
    xp, rsn, _e, _x = short_exit_5m(db, lp, stop_p, target_p, False,
                                    on_close=ON_CLOSE, include_entry_bar=gap_entry,
                                    day_low=sig["dl"], day_high=sig["dh"], day_close=sig["dc"],
                                    stop_delay_bars=DELAY)
    if rsn in ("no_5m", "no_entry"):
        return None
    return short_pnl(ef, xp, rsn, QTY, FEE_ONE_WAY, SLIP)


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


def _report(sigs, title):
    print("\n" + "=" * 74)
    print(f"【{title}】 {len(sigs)}シグナル")
    print("=" * 74)
    if not sigs:
        print("  (該当なし)"); return
    print("  ガード   (=この%超の下げは取消/約定不可)   総損益・勝率・PF")
    print("-" * 74)
    best = None
    for g in GUARDS:
        st = _stat([p for s in sigs if (p := _pnl_for(s, g)) is not None])
        _ag = "   ∞ " if g is None else f"{GLABEL[g]:>5} "
        mark = ""
        if st and (best is None or st["pnl"] > best[1]):
            best = (g, st["pnl"])
        print(f"  {_ag}{_fmt(st)}")
    if best:
        _bl = "無制限" if best[0] is None else GLABEL[best[0]]
        print("-" * 74)
        print(f"  → 総損益 最大は ガード {_bl}(={_bl}超で取消)。ここが最適閾値。")


def main():
    keep = _load_bt_pairs()
    if not keep:
        print("[error] BT対象ペアが0。CSVパス/bt列/--bt-min を確認。", file=sys.stderr)
        return
    names = getattr(_load_bt_pairs, "name", {})
    pairs = sorted(keep.keys())
    if args.limit > 0:
        pairs = pairs[:args.limit]
    print(f"[info] BT{args.bt_min:.0f}以上の (銘柄,戦略) {len(pairs)}ペア / "
          f"sm={args.sm} tm={args.tm} delay={DELAY} / slip{SLIP*100:.2f}% "
          f"fee{FEE_ONE_WAY*100:.2f}% on_close={ON_CLOSE} / 約定=本番エンジン同等")

    import hashlib as _h, pickle as _pk
    _cd = Path(".gapbt_cache")
    _key = _h.md5("|".join(str(x) for x in [
        "gapbtv1", getattr(ble, "_BT_LOGIC_VER", "?"), args.sm, args.tm, DELAY, args.source,
        args.days, args.bt_min, args.limit, QTY,
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
            futs = {ex.submit(_collect_signals, _jq_to_yf(s), names.get(s, s), t): (s, t)
                    for (s, t) in pairs}
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
        print("[error] シグナル0件。5分足在庫/期間を確認。", file=sys.stderr)
        return

    _report(sigs, f"BT{args.bt_min:.0f}以上・全期間(in-sample含む)")
    bm = args.base_month.strip()
    if bm:
        try:
            be = pd.Period(bm, "M").end_time.normalize()
            tr = [s for s in sigs if s["fd"] <= be]
            te = [s for s in sigs if s["fd"] > be]
            _report(tr, f"BT{args.bt_min:.0f}以上・TRAIN ≤{be.date()} (in-sample)")
            _report(te, f"BT{args.bt_min:.0f}以上・TEST >{be.date()} (OOS=擬似未来)")
        except Exception as _e:
            print(f"[warn] base-month分割スキップ({_e})")

    print("\n" + "=" * 74)
    print("判断: 各表で『総損益 最大のガード%』が最適閾値。TEST(OOS)でも同じ%が最大なら本物。")
    print("  その%を現行3%と比べ、明確に上回る時だけ変更(=①BTガード ②実発注下限指値 ③寄り取消 を同値に)。")
    print("  差が小さい/OOSで一致しないなら現行3%維持(いじらない)。約定は本番エンジン同等なので信頼可。")


if __name__ == "__main__":
    main()
