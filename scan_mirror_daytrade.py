"""scan_mirror_daytrade.py — lss鏡像(#7): ショート候補を逆指値買い・同日決済で選定/OOS検証。

lss = ロング候補(強気セットアップ)を逆指値売り(下ブレイク・同日)。
本ツール(#7 = lssの真の鏡像) = ショート候補(弱気セットアップ)を逆指値買い(上ブレイク・同日)。

  狙い(1月型 踏み上げ/リバウンド相場のヘッジ):
    デッドクロス等の弱気セットアップ銘柄が、翌日むしろ前日終値を上抜ける
    = 弱気の踏み上げ/リバウンド。その上ブレイクを順張りで買い、同日で利確/損切り。
    lss(下ブレイクショート)の裏で稼ぐ両建て候補。銘柄はlss/long_daytradeと別物になる。

scan_long_daytrade.py との違いは「信号ソースだけ」:
  * long_daytrade = mod_for(ロング信号 MACDTF/A7/RSI2/DON/VOLTF/MOM)を逆指値買い。
  * mirror(本ツール) = ショート信号(A7_S/RSI2_S/MACD_S/DON_S/MOM_S/GAP_S)を逆指値買い。
  約定/決済メカニクス(entry_type="stop"=上ブレイク買い / long_* 5分足first-touch)は完全に共通。
  信号は entry_sig(弱気セットアップ日)だけを使い、そこに逆指値買いを置く(符号は買い=ロング)。

設計(lss/long_daytradeと同じ):
  * 母集団 = stock_5min にある全銘柄 × 6ショート戦略。5分足が無い銘柄は対象外。
  * 決済は必ず 5分足 first-touch (sameday5m_firsttouch の long_* を使用)。
  * 同日決済=日跨ぎしない → 「約定日」で TRAIN/TEST に振り分けるだけで正しいホールドアウト。
  * 選定は TRAIN(基準月以前)のみ。TEST(基準月より後)は OOS 検証として報告(選定に不使用)。
  * ★空売りではない(逆指値買い)ので not_shortable 除外は不要。

使い方(あなたの機械で):
  python scan_mirror_daytrade.py --base-months 2025-09,2025-10,...,2026-06 --sm 0.1 --tm 1.0 --source local --workers 8
出力: mirror_proposal_<基準月>.py (--base-months) / mirror_watchlist_proposal_<date>.py (単一)

OOS検証の読み方:
  各基準月の出力に「TRAIN合算 損益 / TEST(OOS) 損益 / OOSプラス率」が出る。
  TRAINで選んだ鏡像ペアが TEST(未来)でもプラスなら本物。全基準月で TEST が安定プラスなら
  #7 は採用検討に値する。TESTがまだらor負けなら鏡像は成立せず(却下)。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss鏡像(ショート候補を逆指値買い同日決済)の銘柄選定/OOS検証")
ap.add_argument("--base-month", type=str, default="2025-12",
                help="TRAIN の終端月(この月末まで=選定 / 翌月以降=OOS検証)")
ap.add_argument("--base-months", type=str, default="",
                help="複数基準月をカンマ区切りで一括選定。5分足ロード・バックテストを1回だけ行い、"
                     "各基準月でTRAIN/TESTに振り分けて mirror_proposal_<基準月>.py を出力(高速)。")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(既定=5分足スイープ最適0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(既定=5分足スイープ最適1.0)")
ap.add_argument("--stop-delay-bars", type=int, default=0,
                help="選定バックテストの損切り遅延本数。0(既定)=同バー損切りを損切り扱い(厳しい)。"
                     "1=約定5分足の損切りを無効化(甘い=厚い選定)。lss/長daytradeと揃えるなら同値。")
ap.add_argument("--days", type=int, default=800, help="遡及日数(TRAIN先頭を十分含む長さ)")
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部。デバッグ用)")
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--budget", type=float, default=0.0,
                help="予算(円)。指定すると max-price=budget/FIXED_QTY に自動設定")
ap.add_argument("--max-per-symbol", type=int, default=0,
                help="1銘柄あたり採用する戦略数の上限(0=無制限)")
ap.add_argument("--qty", type=int, default=None, help="株数(既定=FIXED_QTY)")
ap.add_argument("--slip", type=float, default=0.0, help="損切りの不利スリップ(0.005=0.5%%)")
ap.add_argument("--fee", type=float, default=None, help="片道手数料率(既定=FEE_PCT_ONE_WAY)")
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--train-min-trades", type=int, default=8, help="TRAIN最低取引数")
ap.add_argument("--train-min-pf", type=float, default=1.5, help="TRAIN最低PF")
ap.add_argument("--train-min-wr", type=float, default=0.0, help="TRAIN最低勝率%%(既定0=不問)")
ap.add_argument("--test-min-trades", type=int, default=3, help="TEST最低取引数(検証不足判定用)")
ap.add_argument("--top", type=int, default=40, help="コンソール上位件数")
ap.add_argument("--select-top", type=int, default=0, help="提案の上限件数(0=全部)")
ap.add_argument("--out", type=str, default=None, help="提案ファイル名(既定=mirror_watchlist_proposal_<date>.py)")
ap.add_argument("--no-cache", action="store_true", help="取引ログキャッシュを使わない")
ap.add_argument("--refresh-cache", action="store_true", help="キャッシュ無視で再計算・上書き")
ap.add_argument("--aggregate", action="store_true",
                help="【診断】選定せず全ペアの生の同日期待値をTRAIN/TESTで集計。"
                     "鏡像コンセプト自体の地力を測る(選定overfitと切り分け)。キャッシュ利用で数秒。")
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import available_local_symbols, load_intraday, split_by_day
from sameday5m_firsttouch import long_exit_5m, long_pnl, long_entry_fill_5m
# ★lss鏡像: 信号は「ロング6戦略の忠実な鏡像(弱気セットアップ)」。それを逆指値買いする。
#   check_signals_short(旧)はロングの厳密反転になっていない箇所が多く信号頻度が非対称だったため、
#   mirror_signals(ロング calc を条件ごとに1:1反転)に差し替え=「すべてをロングの鏡像」。
from mirror_signals import MIRROR_CALC

ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY if args.fee is None else args.fee
BASE_END = (pd.Period(args.base_month, "M").end_time).normalize()
if args.budget > 0:
    args.max_price = min(args.max_price, args.budget / max(1, QTY))

# ロング6戦略(MACDTF/A7/RSI2/DON/VOLTF/MOM)を条件ごとに1:1反転した忠実な鏡像。
# 戦略名はロングと同一(=すべてロングの鏡像)。信号頻度・合格数がロングと対称になる。
_SHORT_CALC = MIRROR_CALC


def _jq_to_yf(code: str) -> str:
    c = code.strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"
    return c + ".T"


def _strategies() -> list[str]:
    return list(_SHORT_CALC.keys())


def _name_map() -> dict:
    try:
        import symbols_listed_prime as _p
        return {c: n for (c, n) in getattr(_p, "SYMBOLS", [])}
    except Exception:
        return {}


def _stat(pnls: list):
    n = len(pnls)
    if n == 0:
        return None
    wins = [p for p in pnls if p > 0]
    gp = sum(wins); gl = -sum(p for p in pnls if p <= 0)
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {"n": n, "pnl": sum(pnls), "pf": pf, "wr": len(wins) / n * 100,
            "exp": sum(pnls) / n}


def _scan_symbol(sym: str, name: str, strats: list[str]) -> list[dict]:
    """1銘柄・全ショート戦略の逆指値買い同日決済を回し、(sym,strat)ごとに (約定日, pnl) の生取引を返す。"""
    out = []
    try:
        m5 = load_intraday(sym, days=args.days + 5, source=args.source)
    except Exception:
        return out
    by_day = split_by_day(m5) if (m5 is not None and not m5.empty) else {}
    if not by_day:
        return out
    try:
        df_raw = ble.fetch(sym, args.days + 420)
    except Exception:
        return out
    if df_raw is None or df_raw.empty:
        return out

    for strat in strats:
        cf = _SHORT_CALC.get(strat)
        if cf is None:
            continue
        try:
            df_ind = cf(df_raw.copy())   # ★ショート(弱気)セットアップの entry_sig を付与
        except Exception:
            continue
        try:
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.sm, args.tm, args.days + 420, strat,
                                       entry_type="stop", max_hold=0)   # ★逆指値買い(上ブレイク)
        except Exception:
            continue
        if not r:
            continue
        trades = []
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
            if lp < args.min_price or lp > args.max_price:
                continue
            fd = edt.date() if hasattr(edt, "date") else edt
            db = by_day.get(fd)
            if db is None or len(db) < 2:
                continue
            stop_p, target_p = min(osp, otp), max(osp, otp)   # ★ロング: 損切=下 / 利確=上
            entry_fill = long_entry_fill_5m(db, lp, entry_gap_limit=0.03)  # 上ブレイク買い
            if entry_fill is None:
                continue   # ギャップアップ過大 → 約定不成立
            xp, reason, _e, _x = long_exit_5m(db, lp, stop_p, target_p,
                                              stop_delay_bars=args.stop_delay_bars)
            if reason in ("no_5m", "no_entry"):
                continue
            pnl = long_pnl(entry_fill, xp, reason, QTY, FEE_ONE_WAY, args.slip)
            trades.append((pd.Timestamp(fd), pnl))
        if not trades:
            continue
        out.append({"sym": sym, "name": name, "strat": strat, "trades": trades})
    return out


def _pf(x):
    return "∞" if x == float("inf") else f"{x:.2f}"


def _select_and_write(results: list[dict], base_month: str, multi: bool):
    base_end = pd.Period(base_month, "M").end_time.normalize()
    selected = []
    for r in results:
        train = [p for (fd, p) in r["trades"] if fd <= base_end]
        tr = _stat(train)
        if not tr:
            continue
        if (tr["n"] >= args.train_min_trades and tr["pf"] >= args.train_min_pf
                and tr["pnl"] > 0 and tr["wr"] >= args.train_min_wr):
            test = [p for (fd, p) in r["trades"] if fd > base_end]
            selected.append({"sym": r["sym"], "name": r["name"], "strat": r["strat"],
                             "train": tr, "test": _stat(test)})
    selected.sort(key=lambda r: -r["train"]["exp"])

    n_before_cap = len(selected)
    if args.max_per_symbol > 0:
        per: dict = {}; capped = []
        for r in selected:
            if per.get(r["sym"], 0) >= args.max_per_symbol:
                continue
            per[r["sym"]] = per.get(r["sym"], 0) + 1
            capped.append(r)
        selected = capped
    n_after_cap = len(selected)
    if args.select_top > 0:
        selected = selected[:args.select_top]

    print("\n" + "=" * 90)
    print(f"■ [{base_month}] (鏡像=ショート候補を逆指値買い) TRAIN合格 {n_before_cap}ペア / 全{len(results)}ペア"
          + (f" → 1銘柄{args.max_per_symbol}戦略上限 {n_after_cap}" if args.max_per_symbol > 0 else "")
          + (f" → 上位{args.select_top} {len(selected)}" if args.select_top > 0 else "")
          + f" ({len({r['sym'] for r in selected})}銘柄)")
    if not selected:
        print(f"  [{base_month}] 合格ゼロ。しきい値を緩めるか5分足在庫を増やす。")
        return
    test_pos = test_have = 0
    for r in selected:
        te = r["test"]
        if te and te["n"] >= args.test_min_trades:
            test_have += 1
            test_pos += 1 if te["pnl"] > 0 else 0
    tr_tot = sum(r["train"]["pnl"] for r in selected)
    te_tot = sum(r["test"]["pnl"] for r in selected if r["test"])
    print(f"  TRAIN合算 損益 {tr_tot:>+13,.0f}円 (≤{base_end.date()}) / "
          f"TEST(OOS) 損益 {te_tot:>+13,.0f}円 (>{base_end.date()})"
          + (f" / OOSプラス率 {test_pos}/{test_have}" if test_have else ""))

    out_path = Path(f"mirror_proposal_{base_month}.py") if multi else \
        (Path(args.out) if args.out else Path(f"mirror_watchlist_proposal_{ble._TODAY}.py"))
    lines = [
        '"""lss鏡像(ショート候補を逆指値買い)WATCHLIST提案 — scan_mirror_daytrade.py 生成',
        f'  基準月(TRAIN終端): {base_month} / sm={args.sm} tm={args.tm} / slip={args.slip} fee={FEE_ONE_WAY}'
        f' / stop_delay_bars={args.stop_delay_bars}',
        f'  選定: TRAIN({base_end.date()}以前)で PF>={args.train_min_pf} 取引>={args.train_min_trades} 損益>0。',
        f'  合格 {len(selected)}ペア。TRAIN期待値順。鏡像(逆指値買い=上ブレイク・同日決済)。',
        '"""',
        f'BASE_MONTH = "{base_month}"',
        "SELECTED = [",
    ]
    for r in selected:
        tr, te = r["train"], r["test"]
        te_note = f"TEST {te['n']}件 PF{_pf(te['pf'])} {te['pnl']:+,.0f}" if te else "TEST データ無"
        lines.append(
            f'    ({r["sym"]!r}, {r["name"]!r}, {r["strat"]!r}),'
            f'  # TRAIN {tr["n"]}件 PF{_pf(tr["pf"])} 勝率{tr["wr"]:.0f}% {tr["pnl"]:+,.0f} / {te_note}')
    lines.append("]")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [出力] {out_path.resolve()}")


def _aggregate_report(results: list[dict], base_list: list[str]):
    """選定なし=全ペアの生取引を TRAIN/TEST に割り、鏡像コンセプトの地力を測る。
    選定後(TRAIN合格ペア)がOOSで負けても、全ペアの地力が
      * 負 → コンセプトが逆(方向が間違い)。選定を変えても救えない=却下。
      * 正 → コンセプトにはエッジ有。選定方法がoverfitしているだけ=選定を作り直す価値あり。
    """
    def _fmt(s):
        if not s:
            return "取引なし"
        _pf_s = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        return (f"{s['n']:>6}件 損益{s['pnl']:>+13,.0f} PF{_pf_s:>5} "
                f"勝率{s['wr']:>4.0f}% 期待値{s['exp']:>+8,.0f}/件")

    print("\n" + "#" * 90)
    print("【集計診断】選定なし=全ペア(全ショート戦略×全銘柄)の生の同日期待値。鏡像コンセプトの地力。")
    print("  TESTが負 → 方向が根本的に逆(却下) / TESTが正 → 選定方法のoverfit(作り直す価値)")
    print("#" * 90)
    for bm in base_list:
        be = pd.Period(bm, "M").end_time.normalize()
        train = []; test = []
        for r in results:
            for (fd, p) in r["trades"]:
                (train if fd <= be else test).append(p)
        print(f"\n■ [{bm}] 全ペア地力")
        print(f"    TRAIN(≤{be.date()}): {_fmt(_stat(train))}")
        print(f"    TEST (>{be.date()}): {_fmt(_stat(test))}")

    # 単一カットオフ(中央付近)で戦略別の地力も見る(どの弱気戦略も効かないか確認)。
    _mid = base_list[len(base_list) // 2]
    _be = pd.Period(_mid, "M").end_time.normalize()
    print("\n" + "-" * 90)
    print(f"■ 戦略別の全ペア地力 (カットオフ {_be.date()} / TRAIN≤ vs TEST>)")
    by_strat_tr: dict = {}; by_strat_te: dict = {}
    for r in results:
        s = r["strat"]
        for (fd, p) in r["trades"]:
            (by_strat_tr if fd <= _be else by_strat_te).setdefault(s, []).append(p)
    for s in _strategies():
        print(f"    {s:<7} TRAIN {_fmt(_stat(by_strat_tr.get(s, [])))}")
        print(f"    {s:<7} TEST  {_fmt(_stat(by_strat_te.get(s, [])))}")


def main():
    _seen = set(); universe = []
    for _s in available_local_symbols():
        yf = _jq_to_yf(_s)
        if yf not in _seen:
            _seen.add(yf); universe.append(yf)
    if args.limit > 0:
        universe = universe[:args.limit]
    if not universe:
        print("[error] stock_5min に銘柄がありません。", file=sys.stderr)
        return
    names = _name_map()
    strats = _strategies()
    base_list = [b.strip() for b in args.base_months.split(",") if b.strip()] \
        if args.base_months else [args.base_month]
    multi = bool(args.base_months)
    print(f"[info] (鏡像) 母集団(5分足あり) {len(universe)}銘柄 × {len(strats)}ショート戦略 = "
          f"{len(universe)*len(strats)}ペア候補")
    print(f"[info] 基準月: {', '.join(base_list)} / sm={args.sm} tm={args.tm} / "
          f"{'摩擦なし' if (args.slip==0 and FEE_ONE_WAY==0) else f'slip{args.slip*100:.2f}%/手数料{FEE_ONE_WAY*100:.2f}%'}")
    if multi:
        print(f"[info] 一括モード: 取引ログを1回だけ計算し、{len(base_list)}基準月に振り分け(高速)")

    import hashlib as _hashlib, pickle as _pickle
    from pathlib import Path as _Path
    _cache_dir = _Path(".mirror_scan_cache")
    _eng_ver = str(getattr(ble, "_BT_LOGIC_VER", "?"))
    _key_src = "|".join(str(x) for x in [
        "mirrorscanv1", _eng_ver, args.sm, args.tm, args.slip, FEE_ONE_WAY, args.stop_delay_bars,
        args.source, args.days, args.limit, args.min_price, args.max_price, QTY,
        len(universe), _hashlib.md5(",".join(universe).encode()).hexdigest(), ",".join(strats),
    ])
    _key = _hashlib.md5(_key_src.encode()).hexdigest()[:16]
    _cache_f = _cache_dir / f"trades_{_key}.pkl"

    results = None
    if _cache_f.exists() and not args.no_cache and not args.refresh_cache:
        try:
            results = _pickle.loads(_cache_f.read_bytes())
            print(f"[cache] 取引ログを再利用: {_cache_f} ({len(results)}ペア) ※--refresh-cacheで再計算", flush=True)
        except Exception as _ce:
            print(f"[cache] 読込失敗({_ce}) → 再計算", flush=True)
            results = None
    if results is None:
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_scan_symbol, s, names.get(s, s), strats): s for s in universe}
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 50 == 0:
                    print(f"  ...{done}/{len(universe)}銘柄", flush=True)
                try:
                    results += fut.result()
                except Exception:
                    continue
        if not args.no_cache:
            try:
                _cache_dir.mkdir(exist_ok=True)
                _cache_f.write_bytes(_pickle.dumps(results))
                print(f"[cache] 取引ログを保存: {_cache_f} ({len(results)}ペア)", flush=True)
            except Exception as _ce:
                print(f"[cache] 保存失敗({_ce})", flush=True)

    if args.aggregate:
        _aggregate_report(results, base_list)
        return

    for bm in base_list:
        _select_and_write(results, bm, multi)


if __name__ == "__main__":
    main()
