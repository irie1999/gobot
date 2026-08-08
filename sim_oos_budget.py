"""sim_oos_budget.py — 生トレードCSVから任意BT閾値で予算シミュを後から実行する。

sweep_lss_oos_monthly.py が生成する「生トレードCSV」（oos_raw_*.csv）を読み込み、
任意のBT閾値・予算で4種類の予算シミュを再計算する。
OOSスイープを1回実行すれば、何度でも閾値を変えて結果を確認できる。

生CSVの内容:
  fold, train_months, oos_month, entry_date, symbol, name, strategy,
  bt_score, entry_p, pnl, filled
  filled=1: 約定済み / filled=0: シグナルが出たが不約定（pnl=0）

使い方:
  # BT30以上で4シムタイプ比較
  python sim_oos_budget.py --raw oos_raw_20260804.csv

  # BT閾値を複数まとめて比較
  python sim_oos_budget.py --raw oos_raw_20260804.csv --bt-mins 30,40,50,60,70

  # フォールド別明細も表示
  python sim_oos_budget.py --raw oos_raw_20260804.csv --bt-mins 30,60 --verbose

  # CSV出力（Excelで開ける）
  python sim_oos_budget.py --raw oos_raw_20260804.csv --bt-mins 30,50,60 --out sim_result.csv

シムタイプ（現行のOOSスイープと同じ定義）:
  通常予算:     BT降順で発注。不約定も発注枠を消費（現実的な上限）
  約定額ベース: 約定のみが発注枠を消費（楽観的な上限）
  ループ充填_全戦略: BT降順で何周でも繰り返し400万を充填
  ループ充填_絞り:   A7/RSI2/VOLTFのみでループ充填

予算・絞り戦略は --budget / --strat-narrow で変更可能（既定: 400万 / A7,RSI2,VOLTF）。

注意: fill率の表示
  シグナル数 = 約定 + 不約定
  fill率 = 約定 / シグナル数 × 100
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

# ── 発注優先順位 / 戦略別BT下限 (lss_priority) ──────────────────────
# 既定は無効 = 従来どおり「一律 bt_min + BT降順」。LSS_PRIORITY で opt-in。
#   LSS_PRIORITY=floors … 戦略別BT下限のみ(並びはBT降順)
#   LSS_PRIORITY=1      … 下限 + 期待値降順
# 生CSVは BT>=30 の候補プールなので、ここで下限と並びを差し替えれば
# フォールド実行をやり直さずに A/B 比較できる。
try:
    import lss_priority as _lprio
except Exception:
    _lprio = None


def _bt_pass(t, bt_min: float) -> bool:
    if _lprio is None:
        return t["bt_score"] >= bt_min
    return _lprio.is_orderable(t.get("strategy", ""), t["bt_score"], bt_min)


# ── 発注順 ──────────────────────────────────────────────────────────
# ライブ(lss_budget_cap)・レポート(nikkei_analysis)・検証(sim_portfolio_lss)と
# 同じ lss_order_rank を経由する。既定=流動性(売買代金)降順。
# 18.21: BT降順はランダム6本すべてを下回る(z=-2.22)ので使わない。
try:
    import lss_order_rank as _lor
except Exception:
    _lor = None

_LIQ_CACHE: dict = {}


def _liq_of(sym: str) -> float:
    """銘柄の平均日次売買代金。生CSVに liquidity 列があればそちらが優先。

    ⚠ ここで算出する値は『今日から遡って120日』なので、古いフォールドにとっては
      厳密には as-of ではない。ただし **リターンではなくコスト(板の厚さ)の代理**
      なので、順位付けに使う限り実害は小さい。
      2026-08-08 以降に生成した生CSVは liquidity 列を持つので、この経路は使われない。
    """
    if sym in _LIQ_CACHE:
        return _LIQ_CACHE[sym]
    v = 0.0
    try:
        from backtest_limit_entry import fetch as _fq
        if _lor is not None:
            v = _lor.daily_turnover(_fq(sym, 200))
    except Exception:
        v = 0.0
    _LIQ_CACHE[sym] = v
    return v


def _order_key(t):
    if _lor is None:
        return (0.0, -t["bt_score"])
    # BT降順モードでは liquidity を見ないので取りに行かない(ネット取得を避ける)。
    if _lor.mode() == "bt":
        return _lor.sort_key(t["bt_score"], 0.0)
    _liq = t.get("liquidity")
    if _liq in (None, "", 0, 0.0):
        _liq = _liq_of(str(t.get("symbol", "")))
    return _lor.sort_key(t["bt_score"], _liq)


# === 定数（OOSスイープと合わせる） ===
_DEFAULT_BUDGET_MAN = 400      # 万円
_DEFAULT_STRAT_NARROW = {"A7", "RSI2", "VOLTF"}
_SIM_TYPES = ["通常予算", "約定額ベース", "ループ充填_全戦略", "ループ充填_絞り"]


# ─────────────────────────────────────────
# データ読み込み
# ─────────────────────────────────────────

def load_raw_csv(path: str) -> list[dict]:
    """生トレードCSVを読み込んで list[dict] で返す。

    path はグロブ可(例 "oos_raw_fold*.csv")。run_oos_folds.py はフォールドごとに
    別ファイルを書くので、まとめて読めないと多フォールド検証ができない。
    """
    from glob import glob as _glob
    paths = sorted(_glob(path)) if any(c in path for c in "*?[") else [path]
    if not paths:
        sys.exit(f"[error] {path} に一致するCSVがありません")
    rows = []
    for _p in paths:
        rows.extend(_load_one_raw_csv(_p))
    if len(paths) > 1:
        print(f"[入力] {len(paths)}ファイル / {len(rows)}行 を読込 "
              f"({paths[0]} 〜 {paths[-1]})")
    return rows


def _load_one_raw_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rows.append({
                "fold":         int(row.get("fold") or 0),
                "train_months": row.get("train_months", ""),
                "oos_month":    row.get("oos_month", ""),
                "entry_date":   row.get("entry_date", ""),
                "symbol":       row.get("symbol", ""),
                "name":         row.get("name", ""),
                "strategy":     row.get("strategy", ""),
                "bt_score":     float(row.get("bt_score") or 0),
                "entry_p":      float(row.get("entry_p") or 0),
                "pnl":          float(row.get("pnl") or 0),
                "filled":       int(row.get("filled") or 0),
            })
    return rows


# ─────────────────────────────────────────
# 予算シミュ（1日・1フォールド・1OOS月）
# ─────────────────────────────────────────

def _sim_one_day(
    day_trades: list[dict],
    bt_min: float,
    budget: float,
    fill_budget: bool,
    multi_lot: bool,
    strat_set: set[str] | None,
) -> tuple[int, int, float]:
    """1日分のトレードリストを受け取り (trades, wins, pnl) を返す。

    day_trades: 同一 entry_date のトレード（filled=1 と filled=0 を含む）
    fill_budget: True=約定額のみが枠消費 / False=不約定も枠消費
    multi_lot:   True=ループ充填（同一銘柄でも複数回投資可）
    strat_set:   None=全戦略 / set=その戦略のみ（ループ充填_絞り）
    """
    # ⛔ 転換は lss ではない(18.5.3: 09:09時点で不約定が確定せず systematic に
    #    再現できないと確定済み)。含めると損益が押し上がる(18.13: 349件 +451,094円)。
    #    内訳セクション(:383)は除外していたのに、**予算シミュ本体が除外していなかった**。
    candidates = [t for t in day_trades
                  if t.get("strategy") != "転換" and _bt_pass(t, bt_min)]

    # 戦略フィルター（絞り）
    if strat_set is not None:
        candidates = [t for t in candidates if t["strategy"] in strat_set]

    # BT降順ソート
    candidates.sort(key=_order_key)

    if not candidates:
        return 0, 0, 0.0

    if multi_lot:
        # ループ充填: BT降順で何周でも繰り返し budget を充填
        trades = 0
        wins = 0
        pnl = 0.0
        used = 0.0
        filled_only = [t for t in candidates if t["filled"] == 1]
        if not filled_only:
            return 0, 0, 0.0
        idx = 0
        while used < budget:
            t = filled_only[idx % len(filled_only)]
            cost = t["entry_p"] * 100
            if cost <= 0:
                idx += 1
                continue
            if used + cost > budget * 1.05:  # 5%オーバーで打ち切り
                break
            trades += 1
            if t["pnl"] > 0:
                wins += 1
            pnl += t["pnl"]
            used += cost
            idx += 1
            if idx >= len(filled_only) * 10:  # 無限ループ防止
                break
        return trades, wins, pnl

    else:
        # 通常予算 / 約定額ベース: 1銘柄1ロット、予算内で上から順に
        trades = 0
        wins = 0
        pnl = 0.0
        used = 0.0
        for t in candidates:
            cost = t["entry_p"] * 100
            if cost <= 0:
                cost = budget  # entry_p=0 は1件で予算消費（保守的）
            if fill_budget:
                # 約定額ベース: 約定のみが枠消費
                if t["filled"] == 1:
                    if used + cost > budget:
                        break
                    used += cost
                    trades += 1
                    if t["pnl"] > 0:
                        wins += 1
                    pnl += t["pnl"]
                # 不約定はカウントもしない
            else:
                # 通常予算: 不約定も枠消費（発注したが約定しなかった扱い）
                if used + cost > budget:
                    break
                used += cost
                if t["filled"] == 1:
                    trades += 1
                    if t["pnl"] > 0:
                        wins += 1
                    pnl += t["pnl"]
        return trades, wins, pnl


def run_sim(
    rows: list[dict],
    bt_min: float,
    budget_man: float,
    sim_type: str,
    strat_narrow: set[str],
) -> dict[tuple, dict]:
    """
    生データ rows を fold × oos_month × entry_date 単位でシミュ実行。
    返り値: {(fold, train_months, oos_month): {"trades", "wins", "pnl"}}
    """
    budget = budget_man * 10_000

    # sim_type → パラメータ変換
    if sim_type == "通常予算":
        fill_budget, multi_lot, strat_set = False, False, None
    elif sim_type == "約定額ベース":
        fill_budget, multi_lot, strat_set = True, False, None
    elif sim_type == "ループ充填_全戦略":
        fill_budget, multi_lot, strat_set = True, True, None
    elif sim_type == "ループ充填_絞り":
        fill_budget, multi_lot, strat_set = True, True, strat_narrow
    else:
        raise ValueError(f"未知のシムタイプ: {sim_type}")

    # fold × oos_month × entry_date でグルーピング
    groups: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["fold"], r["train_months"], r["oos_month"])
        groups[key][r["entry_date"]].append(r)

    result: dict[tuple, dict] = {}
    for key, by_date in sorted(groups.items()):
        tot_trades = tot_wins = 0
        tot_pnl = 0.0
        for date_str, day_rows in sorted(by_date.items()):
            t, w, p = _sim_one_day(
                day_rows, bt_min=bt_min, budget=budget,
                fill_budget=fill_budget, multi_lot=multi_lot, strat_set=strat_set,
            )
            tot_trades += t
            tot_wins += w
            tot_pnl += p
        result[key] = {
            "trades": tot_trades,
            "wins": tot_wins,
            "pnl": tot_pnl,
        }
    return result


# ─────────────────────────────────────────
# 集計・出力
# ─────────────────────────────────────────

def aggregate(result: dict[tuple, dict]) -> dict:
    """フォールド別結果を集計して全体サマリーを返す。"""
    tot = {"trades": 0, "wins": 0, "pnl": 0.0, "months": 0}
    for v in result.values():
        tot["trades"] += v["trades"]
        tot["wins"] += v["wins"]
        tot["pnl"] += v["pnl"]
        tot["months"] += 1
    return tot


def print_table(
    rows: list[dict],
    bt_min: float,
    budget_man: float,
    strat_narrow: set[str],
    verbose: bool,
) -> list[dict]:
    """シムタイプ別の集計を表示し、CSV出力用の行リストを返す。"""
    out_rows = []
    budget = budget_man * 10_000

    # fill率の計算（BT閾値フィルタ後）
    bt_filtered = [r for r in rows if _bt_pass(r, bt_min)]
    n_signals = len(bt_filtered)
    n_filled = sum(1 for r in bt_filtered if r["filled"] == 1)
    fill_rate = n_filled / n_signals * 100 if n_signals else 0.0

    print(f"\n{'='*68}")
    print(f"  BT>={bt_min:.0f}  予算{budget_man:.0f}万円  シグナル{n_signals}件（約定{n_filled}件, fill率{fill_rate:.1f}%）")
    print(f"{'='*68}")
    print(f"  {'シムタイプ':<20} {'月数':>4} {'取引':>6} {'勝率':>6} {'損益':>12} {'月平均':>10}")
    print(f"  {'-'*66}")

    for sim_type in _SIM_TYPES:
        result = run_sim(rows, bt_min=bt_min, budget_man=budget_man,
                         sim_type=sim_type, strat_narrow=strat_narrow)
        tot = aggregate(result)
        months = tot["months"]
        trades = tot["trades"]
        wins = tot["wins"]
        pnl = tot["pnl"]
        wr = wins / trades * 100 if trades else 0.0
        avg_pnl = pnl / months if months else 0.0
        sign = "+" if pnl >= 0 else ""
        print(f"  {sim_type:<20} {months:>4}月 {trades:>6}件 {wr:>5.1f}% "
              f"{sign}{pnl:>11,.0f}円 {sign}{avg_pnl:>9,.0f}円/月")

        out_rows.append({
            "bt_min": bt_min,
            "budget_man": budget_man,
            "sim_type": sim_type,
            "months": months,
            "trades": trades,
            "wins": wins,
            "win_rate_pct": round(wr, 1),
            "pnl": round(pnl, 0),
            "pnl_per_month": round(avg_pnl, 0),
            "n_signals": n_signals,
            "n_filled": n_filled,
            "fill_rate_pct": round(fill_rate, 1),
        })

        if verbose:
            # フォールド別明細
            for (fold, train_months, oos_month), v in sorted(result.items()):
                t, w, p = v["trades"], v["wins"], v["pnl"]
                wr_f = w / t * 100 if t else 0.0
                s = "+" if p >= 0 else ""
                print(f"    fold{fold} ({oos_month}) {t:>5}件 {wr_f:>5.1f}% {s}{p:>+10,.0f}円")

    return out_rows


def print_signal_breakdown(rows: list[dict], bt_min: float) -> None:
    """BT閾値フィルタ後の約定/不約定の内訳を表示する。"""
    bt_filtered = [r for r in rows if _bt_pass(r, bt_min)]
    by_month: dict[str, dict] = defaultdict(lambda: {"signals": 0, "filled": 0, "pnl": 0.0})
    for r in bt_filtered:
        m = r["oos_month"]
        by_month[m]["signals"] += 1
        if r["filled"] == 1:
            by_month[m]["filled"] += 1
            by_month[m]["pnl"] += r["pnl"]

    print(f"\n  ── OOS月別シグナル内訳 (BT>={bt_min:.0f}) ──")
    print(f"  {'OOS月':<10} {'シグナル':>8} {'約定':>6} {'fill率':>7} {'PnL(約定分)':>12}")
    for m in sorted(by_month.keys()):
        v = by_month[m]
        fr = v["filled"] / v["signals"] * 100 if v["signals"] else 0.0
        s = "+" if v["pnl"] >= 0 else ""
        print(f"  {m:<10} {v['signals']:>8}件 {v['filled']:>6}件 {fr:>6.1f}% {s}{v['pnl']:>11,.0f}円")


# ─────────────────────────────────────────
# メイン
# ─────────────────────────────────────────

def print_strategy_bt_breakdown(rows: list[dict]) -> None:
    """生CSVから『戦略 × BTスコア帯』の実測を出す(予算制約なし・全約定トレード)。

    compare_lss_rules の【BT帯×戦略別内訳】と同じ見方を、**実運用パイプラインの
    OOSデータ**で出す。両者がズレる場合、原因は母集団とBTスコアの定義の違い:
      ・compare_lss_rules … holdout_selected_symbols.py の単一選定 /
                            BTは「今日時点の直近365日」から算出
      ・この生CSV        … 基準月ごとに選定し直した累積マージ(=daily.bat) /
                            BTは「シグナル発生時に凍結したスコア」
    実運用でどちらが効くかを決めるのは後者。
    """
    from collections import defaultdict as _dd
    band_agg = _dd(lambda: {"n": 0, "pnl": 0.0, "win": 0})
    strat_agg = _dd(lambda: {"n": 0, "pnl": 0.0, "win": 0})
    cross = _dd(lambda: {"n": 0, "pnl": 0.0, "win": 0})
    for r in rows:
        if r["filled"] != 1:
            continue                      # 不約定は pnl=0 なので除く
        st = r["strategy"]
        if st == "転換":
            continue                      # 転換はlssではない(§18.5.2)
        band = int(r["bt_score"] // 20) * 20
        p = r["pnl"]
        for k, d in ((band, band_agg), (st, strat_agg), ((st, band), cross)):
            a = d[k]; a["n"] += 1; a["pnl"] += p; a["win"] += p > 0

    def _line(lbl, v, w=10):
        return (f"{lbl:<{w}}{v['n']:>7}{v['win'] / v['n'] * 100:>6.0f}%"
                f"{v['pnl']:>+14,.0f}{v['pnl'] / v['n']:>+11,.0f}")

    print("\n" + "=" * 60)
    print("【BT帯だけで見る】戦略を混ぜた全体 — BTが効いているかの本体")
    print("=" * 60)
    print(f"{'BT帯':<10}{'件数':>7}{'勝率':>7}{'合計損益':>14}{'1件あたり':>11}")
    for b in sorted(band_agg):
        print(_line(f"{b}~{b + 19}", band_agg[b]))

    print("\n" + "=" * 60)
    print("【戦略だけで見る】BT帯を混ぜた全体")
    print("=" * 60)
    print(f"{'戦略':<10}{'件数':>7}{'勝率':>7}{'合計損益':>14}{'1件あたり':>11}")
    for st, v in sorted(strat_agg.items(), key=lambda x: -x[1]["pnl"] / x[1]["n"]):
        print(_line(st, v))

    print("\n" + "=" * 72)
    print("【BT帯 × 戦略】同じBT帯の中で戦略差があるか = 戦略別閾値に意味があるか")
    print("=" * 72)
    print(f"{'BT帯':<9}{'戦略':<10}{'件数':>7}{'勝率':>7}{'合計損益':>14}{'1件あたり':>11}")
    for b in sorted(band_agg):
        ent = sorted([(st, v) for (st, bb), v in cross.items() if bb == b],
                     key=lambda x: -x[1]["pnl"] / x[1]["n"])
        first = True
        for st, v in ent:
            print(f"{(f'{b}~{b + 19}' if first else ''):<9}" + _line(st, v))
            first = False
        print()
    print("読み方: 『BT帯だけ』が単調なら BT は効いている。『BT帯×戦略』で同じ帯の中の")
    print("        戦略順位が期間や母集団で入れ替わるなら、戦略別に閾値を分ける根拠はない。")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="生トレードCSVから任意BT閾値で予算シミュを後から実行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--raw", required=True, help="生トレードCSV（oos_raw_*.csv）のパス")
    ap.add_argument("--by-strategy", action="store_true",
                    help="『BT帯だけ』『戦略だけ』『BT帯×戦略』の実測内訳を出す(予算制約なし)。"
                         "戦略別にBT閾値を分ける意味があるかを、実運用パイプラインのOOSデータで確認する")
    ap.add_argument("--bt-mins", default="30",
                    help="BT閾値（カンマ区切り複数可） 例: 30,40,50,60,70  (既定: 30)")
    ap.add_argument("--budget", type=float, default=float(_DEFAULT_BUDGET_MAN),
                    help=f"1日あたりの予算（万円） (既定: {_DEFAULT_BUDGET_MAN})")
    ap.add_argument("--strat-narrow", default=",".join(sorted(_DEFAULT_STRAT_NARROW)),
                    help=f"ループ充填_絞りで使う戦略（カンマ区切り） (既定: {','.join(sorted(_DEFAULT_STRAT_NARROW))})")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="フォールド別明細を表示")
    ap.add_argument("--signal-breakdown", action="store_true",
                    help="OOS月別のシグナル/約定内訳を表示")
    ap.add_argument("--out", help="集計結果をCSV出力するパス（省略可）")
    args = ap.parse_args()

    raw_path = Path(args.raw)
    # --raw はグロブ可(run_oos_folds.py はフォールドごとに別ファイルを書くため)
    _is_glob = any(c in args.raw for c in "*?[")
    if not _is_glob and not raw_path.exists():
        print(f"[ERROR] ファイルが見つかりません: {raw_path}", file=sys.stderr)
        sys.exit(1)
    if _is_glob:
        from glob import glob as _g
        if not _g(args.raw):
            print(f"[ERROR] パターンに一致するファイルがありません: {args.raw}", file=sys.stderr)
            sys.exit(1)

    rows = load_raw_csv(args.raw)
    if not rows:
        print("[ERROR] CSVが空です", file=sys.stderr)
        sys.exit(1)

    # パラメータ解析
    bt_mins = [float(x.strip()) for x in args.bt_mins.split(",") if x.strip()]
    strat_narrow = {s.strip() for s in args.strat_narrow.split(",") if s.strip()}
    budget_man = args.budget

    print(f"\n[sim_oos_budget] {args.raw}")
    print(f"  総行数: {len(rows)}行 / 約定: {sum(r['filled'] for r in rows)}件 "
          f"/ 不約定: {sum(1 - r['filled'] for r in rows)}件")
    folds = sorted(set(r['fold'] for r in rows))
    months = sorted(set(r['oos_month'] for r in rows))
    print(f"  フォールド: {folds}  OOS月: {months}")
    print(f"  予算: {budget_man:.0f}万円 / 絞り戦略: {sorted(strat_narrow)}")
    if _lprio is not None:
        print(f"  BT下限ルール: {_lprio.describe()}")
    # 実際に使っている発注順を必ず印字する(18.21 の条件取り違え対策)。
    if _lor is not None:
        print(f"  {_lor.describe()}")
    print(f"  転換は除外(18.5.3: 09:09時点で不約定が確定せず実装不可)")

    if args.by_strategy:
        print_strategy_bt_breakdown(rows)

    all_out_rows: list[dict] = []
    for bt_min in sorted(bt_mins):
        out_rows = print_table(rows, bt_min=bt_min, budget_man=budget_man,
                               strat_narrow=strat_narrow, verbose=args.verbose)
        if args.signal_breakdown:
            print_signal_breakdown(rows, bt_min=bt_min)
        all_out_rows.extend(out_rows)

    if args.out:
        fields = ["bt_min", "budget_man", "sim_type", "months", "trades", "wins",
                  "win_rate_pct", "pnl", "pnl_per_month",
                  "n_signals", "n_filled", "fill_rate_pct"]
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(all_out_rows)
        print(f"\n[出力] {Path(args.out).resolve()}")

    print()


if __name__ == "__main__":
    main()
