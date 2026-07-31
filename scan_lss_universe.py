"""scan_lss_universe.py — lss(ロング銘柄ショート/逆指値空売り・同日決済)専用の銘柄選定スキャン。

これまで lss は「ロング用に選定された WATCHLIST」で運用してきたが、それは代理指標
(ロングの質)で選ばれた副産物にすぎない。本ツールは lss 自身のエッジ(下ブレイク順張り
ショートの同日期待値)で銘柄を選び直す。

設計(バイアス回避の要点):
  * 母集団 = stock_5min にある全銘柄 (= 流動性でDL済みの自然な母集団) × 6戦略。
    5分足が無い銘柄は同日決済を正確に測れないので対象外。
  * 決済は必ず 5分足 first-touch (sameday5m_firsttouch)。日足近似は楽観に出るので使わない。
  * 同日決済トレードは日跨ぎしない=互いに独立。よって「約定日」で TRAIN/TEST に振り分けるだけで
    正しいホールドアウトになる (fold のトリミング機構は不要)。
  * **選定は TRAIN(基準月以前) のみ**で行い、TEST(基準月より後)は選定に一切使わず
    「検証(OOS)」として報告する。TEST を選定条件に入れると未来を覗くリークになる。

使い方(あなたの機械で。この環境はデータ取得不可):
  python scan_lss_universe.py --base-month 2025-12 --sm 0.1 --tm 1.0 --source local
  python scan_lss_universe.py --base-month 2025-12 --slip 0.005        # 実運用スリップ込み
  python scan_lss_universe.py --limit 100                              # デバッグ(先頭100銘柄)
出力:
  * コンソール: 母集団規模 / 選定数 / TRAIN実績 / **選定銘柄のTEST(OOS)検証** / 上位ランキング
  * lss_watchlist_proposal_<date>.py : SELECTED=[(code,name,strat),...] (check_signals_* に貼れる形式)
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ap = argparse.ArgumentParser(description="lss専用の銘柄選定スキャン(TRAIN選定/TEST検証)")
ap.add_argument("--base-month", type=str, default="2025-12",
                help="TRAIN の終端月(この月末まで=選定期間 / 翌月以降=OOS検証)。例 2025-12")
ap.add_argument("--base-months", type=str, default="",
                help="複数基準月をカンマ区切りで一括選定(例 2025-10,2025-11,2026-01)。指定すると "
                     "5分足ロード・バックテストを1回だけ行い、各基準月でTRAIN/TESTに振り分けて "
                     "lss_proposal_<基準月>.py を月ごとに出力(大幅高速化)。--out は無視。")
ap.add_argument("--sm", type=float, default=0.1, help="損切ATR倍率(既定=5分足スイープ最適0.1)")
ap.add_argument("--tm", type=float, default=1.0, help="利確ATR倍率(既定=5分足スイープ最適1.0)")
ap.add_argument("--stop-delay-bars", type=int, default=0,
                help="選定バックテストの損切り遅延本数。0(既定)=v13(同バー損切りを損切り扱い=厳しい=薄い選定)。"
                     "1=約定5分足の損切りを無効化(≒pre-v13の同バー無視=甘い=厚い選定)。"
                     "12/3/6の旧proposal(7/20・pre-v13)相当に揃えたいなら1。")
ap.add_argument("--days", type=int, default=800, help="遡及日数(TRAIN先頭を十分含む長さ)")
ap.add_argument("--source", choices=["auto", "local", "yfinance"], default="local")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部。デバッグ用)")
ap.add_argument("--min-price", type=float, default=0.0)
ap.add_argument("--max-price", type=float, default=1e9)
ap.add_argument("--budget", type=float, default=0.0,
                help="予算(円)。指定すると max-price=budget/FIXED_QTY に自動設定(100株で買える上限)。"
                     "値がさ大型を除外し『円』ランキングのスケール偏りも抑える")
ap.add_argument("--max-per-symbol", type=int, default=0,
                help="1銘柄あたり採用する戦略数の上限(TRAIN期待値上位から。0=無制限)。"
                     "同一銘柄への建玉集中を防ぐ。推奨2")
ap.add_argument("--qty", type=int, default=None, help="株数(既定=FIXED_QTY)")
ap.add_argument("--slip", type=float, default=0.0, help="損切り買い戻しの不利スリップ(0.005=0.5%%)")
ap.add_argument("--fee", type=float, default=None, help="片道手数料率(既定=FEE_PCT_ONE_WAY)")
ap.add_argument("--workers", type=int, default=6)
# 選定しきい値(TRAIN のみに適用)
ap.add_argument("--train-min-trades", type=int, default=8, help="TRAIN最低取引数")
ap.add_argument("--train-min-pf", type=float, default=1.5, help="TRAIN最低PF")
ap.add_argument("--train-min-wr", type=float, default=0.0, help="TRAIN最低勝率%%(既定0=不問)")
ap.add_argument("--test-min-trades", type=int, default=3,
                help="TEST最低取引数(下回るとOOS判定を『検証不足』扱い。選定条件ではない)")
ap.add_argument("--top", type=int, default=40, help="コンソールに出す上位件数")
ap.add_argument("--select-top", type=int, default=0,
                help="提案ファイルに書き出すペア数の上限(TRAIN期待値上位から。0=全部)。"
                     "run_signals_holdout_all --lss-proposal で扱える件数に絞る用途。推奨150")
ap.add_argument("--out", type=str, default=None, help="提案ファイル名(既定=lss_watchlist_proposal_<date>.py)")
ap.add_argument("--no-cache", action="store_true",
                help="母集団取引ログのキャッシュを使わない(常に再計算)")
ap.add_argument("--refresh-cache", action="store_true",
                help="キャッシュを無視して再計算し、上書き保存する")
args = ap.parse_args()

import backtest_limit_entry as ble
from daytrade_data import available_local_symbols, load_intraday, split_by_day
from sameday5m_core import mod_for
from sameday5m_firsttouch import short_exit_5m, short_pnl, short_entry_fill_5m

# エンジンのモードグローバルは既定(未強制)に固定。pnl は自前の5分足で計算するので、
# エンジン側の mirror 反転 / 5分足置換 / 幅強制が絡まないようにする。
ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._MAX_HOLD_FORCE = None
ble._SM_FORCE = None
ble._TM_FORCE = None
ble._INTRADAY_5M = False

QTY = args.qty if args.qty is not None else ble.FIXED_QTY
FEE_ONE_WAY = ble.FEE_PCT_ONE_WAY if args.fee is None else args.fee
BASE_END = (pd.Period(args.base_month, "M").end_time).normalize()
# --budget 指定時は max-price を budget/株数 に上書き(既存 --max-price と併用時は厳しい方)
if args.budget > 0:
    args.max_price = min(args.max_price, args.budget / max(1, QTY))


def _jq_to_yf(code: str) -> str:
    """J-Quants 5桁(pkl名) → yfinance形式。 72030 → 7203.T / 既に .T ならそのまま。
    daytrade_data.yf_to_jquants(7203.T→72030) の逆。ble.fetch(日足=yfinance)は .T 形式が必須。"""
    c = code.strip().upper()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c[-1] == "0" and c[:4].isalnum():
        return c[:4] + ".T"      # 標準的な内国株(4桁+末尾0)
    return c + ".T"


def _strategies() -> list[str]:
    # ホールドアウトレポート(run_signals_holdout_all)が集計する6戦略に合わせる。
    # MACD/VOL(非TF)はレポート非対応 → 提案を run_signals_holdout_all --lss-proposal に
    # そのまま渡せるよう、ここでは含めない。
    return ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _name_map() -> dict:
    """symbols_listed_prime.py の SYMBOLS から code→name。無ければ空。"""
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
    """1銘柄について全戦略の lss を回し、(sym,strat)ごとに (約定日, pnl) の取引リストを返す。
    5分足は銘柄あたり1回だけロード(最も重い処理)。取引は基準月に依存しないので、複数基準月の
    選定でも本関数は1回だけ実行し、main側で各基準月の BASE_END で TRAIN/TEST に振り分ける
    (=5分足ロード・バックテストを基準月ごとに繰り返さない=大幅高速化)。"""
    out = []
    # 5分足を1回だけロード(この銘柄の最重処理)。無ければ検証不能なのでスキップ。
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
        mod = mod_for(strat)
        params = getattr(mod, "STRATEGY_PARAMS", {}).get(strat)
        if not params:
            continue
        cf, _em, _sm, _tm = params
        try:
            df_ind = cf(df_raw.copy())
        except Exception:
            continue
        try:
            r = ble.run_limit_backtest(sym, name, df_ind, lambda d: d, 0.0,
                                       args.sm, args.tm, args.days + 420, strat,
                                       entry_type="stop_sell", max_hold=0)
        except Exception:
            continue
        if not r:
            continue
        trades = []   # (約定日 Timestamp, pnl) 基準月に依存しない生の取引
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
            stop_p, target_p = max(osp, otp), min(osp, otp)
            # 現実的な約定価格(ギャップ考慮 + -3%指値ガード)。エンジンと揃える。
            entry_fill = short_entry_fill_5m(
                db, lp, False,
                entry_gap_limit=getattr(ble, "_INTRADAY_5M_ENTRY_GAP_LIMIT", 0.02))
            if entry_fill is None:
                continue   # ギャップ過大 → 約定不成立
            xp, reason, _e, _x = short_exit_5m(db, lp, stop_p, target_p, False,
                                               stop_delay_bars=args.stop_delay_bars)  # lss=下落約定
            if reason in ("no_5m", "no_entry"):
                continue
            pnl = short_pnl(entry_fill, xp, reason, QTY, FEE_ONE_WAY, args.slip)
            trades.append((pd.Timestamp(fd), pnl))
        if not trades:
            continue
        out.append({"sym": sym, "name": name, "strat": strat, "trades": trades})
    return out


def _pf(x):
    return "∞" if x == float("inf") else f"{x:.2f}"


def _select_and_write(results: list[dict], base_month: str, multi: bool):
    """共有の取引ログ(results)を、この基準月の BASE_END で TRAIN/TEST に振り分けて選定・出力。"""
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
    selected.sort(key=lambda r: -r["train"]["exp"])   # TRAIN期待値順

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
    print(f"■ [{base_month}] TRAIN合格 {n_before_cap}ペア / 全{len(results)}ペア"
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

    out_path = Path(f"lss_proposal_{base_month}.py") if multi else \
        (Path(args.out) if args.out else Path(f"lss_watchlist_proposal_{ble._TODAY}.py"))
    lines = [
        '"""lss専用WATCHLIST提案 — scan_lss_universe.py 生成',
        f'  基準月(TRAIN終端): {base_month} / sm={args.sm} tm={args.tm} / slip={args.slip} fee={FEE_ONE_WAY}'
        f' / stop_delay_bars={args.stop_delay_bars}({"pre-v13相当=厚い" if args.stop_delay_bars>=1 else "v13=薄い"})',
        f'  選定: TRAIN({base_end.date()}以前)で PF>={args.train_min_pf} 取引>={args.train_min_trades} 損益>0。',
        f'  合格 {len(selected)}ペア。TRAIN期待値順。',
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


def main():
    # pkl名(J-Quants 5桁)を yfinance形式(.T)へ変換。ble.fetch / load_intraday とも .T 前提。
    _seen = set(); universe = []
    for _s in available_local_symbols():
        yf = _jq_to_yf(_s)
        if yf not in _seen:
            _seen.add(yf); universe.append(yf)
    if args.limit > 0:
        universe = universe[:args.limit]
    if not universe:
        print("[error] stock_5min に銘柄がありません(available_local_symbols が空)。"
              "--source local のデータ場所を確認してください。", file=sys.stderr)
        return
    names = _name_map()
    strats = _strategies()
    base_list = [b.strip() for b in args.base_months.split(",") if b.strip()] \
        if args.base_months else [args.base_month]
    multi = bool(args.base_months)
    print(f"[info] 母集団(5分足あり) {len(universe)}銘柄 × {len(strats)}戦略 = "
          f"{len(universe)*len(strats)}ペア候補")
    print(f"[info] 基準月: {', '.join(base_list)} / sm={args.sm} tm={args.tm} / "
          f"{'摩擦なし' if (args.slip==0 and FEE_ONE_WAY==0) else f'slip{args.slip*100:.2f}%/手数料{FEE_ONE_WAY*100:.2f}%'}")
    if multi:
        print(f"[info] 一括モード: 取引ログを1回だけ計算し、{len(base_list)}基準月に振り分けて出力(高速)")

    # ── バックテストは基準月に依存しないので、取引ログを1回だけ計算 ──
    #   取引ログは「基準月」に依存しない(基準月の違いはTRAIN/TESTの振り分けだけ)。
    #   そこで取引ログ(results)をディスクキャッシュし、同一パラメータの2回目以降は再計算しない。
    #   キャッシュキーは"取引に影響する条件"のみ(基準月は含めない=別基準月でも再利用可)。
    import hashlib as _hashlib, pickle as _pickle
    from pathlib import Path as _Path
    _cache_dir = _Path(".lss_scan_cache")
    _eng_ver = str(getattr(ble, "_BT_LOGIC_VER", "?"))
    _key_src = "|".join(str(x) for x in [
        "scanv2",                                   # 選定/取引計算ロジックの版(変えたら手動で上げる)
        _eng_ver, args.sm, args.tm, args.slip, FEE_ONE_WAY, args.stop_delay_bars,
        args.source, args.days, args.limit, args.min_price, args.max_price, QTY,
        len(universe), _hashlib.md5(",".join(universe).encode()).hexdigest(),
        ",".join(strats),
    ])
    _key = _hashlib.md5(_key_src.encode()).hexdigest()[:16]
    _cache_f = _cache_dir / f"trades_{_key}.pkl"

    results = None
    if _cache_f.exists() and not args.no_cache and not args.refresh_cache:
        try:
            results = _pickle.loads(_cache_f.read_bytes())
            print(f"[cache] 取引ログを再利用: {_cache_f} ({len(results)}ペア) "
                  f"※再計算するなら --refresh-cache", flush=True)
        except Exception as _ce:
            print(f"[cache] 読込失敗({_ce}) → 再計算します", flush=True)
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
                print(f"[cache] 取引ログを保存: {_cache_f} ({len(results)}ペア) "
                      f"→ 次回同条件は即ロード", flush=True)
            except Exception as _ce:
                print(f"[cache] 保存失敗({_ce})", flush=True)

    # ── 各基準月で TRAIN/TEST に振り分けて選定・出力 ──
    for bm in base_list:
        _select_and_write(results, bm, multi)


if __name__ == "__main__":
    main()
