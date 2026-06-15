"""
gen_watchlist_from_walkforward.py
==================================
walkforward CSV から3-tuple WATCHLIST を自動生成。

各 walkforward CSV は1戦略の合格銘柄リスト。それらを集約して
(symbol, name, strategy) の3-tupleで WATCHLIST を作る。

これにより holdout_periods_report_daytrade.py で
「銘柄選定された戦略のみ」で取引評価が可能になる
(従来の「銘柄×全12戦略」のムダ計算を解消)。

【使い方】
  python gen_watchlist_from_walkforward.py
  python gen_watchlist_from_walkforward.py --mode standard
  python gen_watchlist_from_walkforward.py --min-pf 1.5
  python gen_watchlist_from_walkforward.py --output watchlist.py

【出力フォーマット】
  daytrade_winners_<日付>.py:
      SYMBOLS = [
          ("1605.T", "ＩＮＰＥＸ", "MOM_S"),
          ("5020.T", "ENEOS", "MOM_S"),
          ...
      ]
"""

from __future__ import annotations

import argparse
import csv
import glob
from datetime import datetime
from pathlib import Path

WF_DIR = Path("walkforward_daytrade_results")
KNOWN_MODES = ["standard", "lenient", "strict", "swing_relaxed", "swing"]


def find_latest_files(mode_filter: str | None = None):
    """walkforward CSV の中から最新日付グループを抽出。"""
    if not WF_DIR.exists():
        return None, []

    pattern = str(WF_DIR / "walkforward_*_*.csv")
    files = glob.glob(pattern)
    if not files:
        return None, []

    parsed = []
    for fp in files:
        stem = Path(fp).stem  # walkforward_<strat>_<mode>_<YYYY-MM-DD>
        parts = stem.split("_")
        if len(parts) < 4:
            continue
        date_str = parts[-1]
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        rest = "_".join(parts[1:-1])  # <strat>_<mode>
        # mode が swing_relaxed のように _ を含む場合があるので末尾マッチ
        matched = False
        for known in KNOWN_MODES:
            if rest.endswith("_" + known):
                strat = rest[:-(len(known) + 1)]
                parsed.append({
                    "file": fp, "strategy": strat,
                    "mode": known, "date": date_str,
                })
                matched = True
                break
        if not matched:
            # mode 不明 → そのままラベル化
            parsed.append({
                "file": fp, "strategy": rest,
                "mode": "unknown", "date": date_str,
            })

    if not parsed:
        return None, []

    if mode_filter:
        parsed = [p for p in parsed if p["mode"] == mode_filter]
        if not parsed:
            return None, []

    latest_date = max(p["date"] for p in parsed)
    return latest_date, [p for p in parsed if p["date"] == latest_date]


def load_winners(csv_path: str, min_pf: float, min_trades: int):
    """CSV から合格基準を満たす銘柄を読み込み。"""
    winners = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                pf_raw = row.get("total_pf", "0") or "0"
                pf = float("inf") if pf_raw == "inf" else float(pf_raw)
                trades = int(row.get("total_trades", "0") or 0)
                pnl = float(row.get("total_pnl", "0") or 0)
            except (ValueError, TypeError):
                continue
            if pf < min_pf or trades < min_trades or pnl <= 0:
                continue
            winners.append({
                "symbol": row.get("symbol", "").strip(),
                "name": row.get("name", "").strip(),
                "strategy": row.get("strategy", "").strip(),
                "pf": pf,
                "trades": trades,
                "pnl": pnl,
                "win_rate": float(row.get("total_win_rate", "0") or 0),
                "sharpe": float(row.get("sharpe", "0") or 0),
                "dd": float(row.get("total_dd", "0") or 0),
                "pass_folds": row.get("pass_folds", ""),
            })
    return winners


def generate_watchlist(mode=None, min_pf=1.3, min_trades=20,
                       output=None, verbose=True):
    """3-tuple WATCHLIST を生成して .py ファイルに書き出す。"""
    latest_date, files = find_latest_files(mode_filter=mode)
    if not files:
        print(f"[error] walkforward CSV が見つかりません ({WF_DIR}/)")
        if mode:
            print(f"  mode='{mode}' フィルタ — mode 指定を外すかモードを変えてみてください")
        else:
            print(f"  scan_walkforward_daytrade.py を先に実行してください")
        return False

    if verbose:
        print(f"[INFO] 最新日付: {latest_date}")
        print(f"[INFO] 対象CSV: {len(files)}本")
        if mode:
            print(f"[INFO] mode フィルタ: {mode}")

    all_winners = []
    per_strat_mode_count = {}
    for finfo in sorted(files, key=lambda x: (x["strategy"], x["mode"])):
        ws = load_winners(finfo["file"],
                           min_pf=min_pf, min_trades=min_trades)
        all_winners.extend(ws)
        key = f"{finfo['strategy']}/{finfo['mode']}"
        per_strat_mode_count[key] = len(ws)
        if verbose:
            print(f"  {key}: {len(ws)}件")

    if not all_winners:
        print(f"\n[error] 合格銘柄なし (min_pf={min_pf}, "
              f"min_trades={min_trades})")
        print(f"  --min-pf や --min-trades を緩めてみてください")
        return False

    # (sym, strat) ペアで重複排除 (同じペアが複数モードで合格しうる)
    # PnL 降順で最良の1行を採用
    seen = set()
    selected = []
    for w in sorted(all_winners, key=lambda x: -x["pnl"]):
        key = (w["symbol"], w["strategy"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(w)

    # 出力先決定
    if output is None:
        output = f"daytrade_winners_{latest_date}.py"
    output_path = Path(output)

    # 戦略別グループ化 (見やすさ + 集計)
    by_strat = {}
    for w in selected:
        by_strat.setdefault(w["strategy"], []).append(w)

    # 整形して .py ファイル出力
    lines = [
        '"""',
        f'{output_path.name}',
        '=' * 50,
        f'walkforward CSV ({latest_date}) から自動生成された 3-tuple WATCHLIST。',
        '',
        '各エントリ: (symbol, name, strategy)',
        '銘柄 × 戦略 で walkforward に合格したペアのみ。',
        'これにより「銘柄選定された戦略のみ」で取引評価される。',
        '',
        f'生成日時: {datetime.now().isoformat(timespec="seconds")}',
        f'mode フィルタ: {mode or "(全モード)"}',
        f'min_pf: {min_pf} / min_trades: {min_trades}',
        f'ユニーク銘柄: {len(set(w["symbol"] for w in selected))}',
        f'(銘柄×戦略) ペア: {len(selected)}',
        '"""',
        '',
        'SYMBOLS: list[tuple[str, str, str]] = [',
    ]

    for strat in sorted(by_strat.keys()):
        ws = sorted(by_strat[strat], key=lambda x: -x["pnl"])
        lines.append(f'    # === {strat} ({len(ws)}件) ===')
        for w in ws:
            pf_str = ("∞" if w["pf"] == float("inf")
                      else f"{w['pf']:.2f}")
            lines.append(
                f'    ("{w["symbol"]}", "{w["name"]}", "{w["strategy"]}"),'
                f'  # PF{pf_str}, 勝率{w["win_rate"]:.0f}%, '
                f'PnL{w["pnl"]:+,.0f}, DD{w["dd"]:.1f}%'
            )

    lines.extend([
        ']',
        '',
        '# サマリ',
        f'UNIQUE_SYMBOLS = {len(set(w["symbol"] for w in selected))}',
        f'TOTAL_PAIRS = {len(selected)}',
        f'BY_STRATEGY = {{',
    ])
    for strat in sorted(by_strat.keys()):
        lines.append(f'    {strat!r}: {len(by_strat[strat])},')
    lines.append('}')

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[OK] 出力: {output_path.resolve()}")
    print(f"  ユニーク銘柄: {len(set(w['symbol'] for w in selected))}")
    print(f"  (銘柄×戦略) ペア: {len(selected)}")
    print(f"\n  戦略別内訳:")
    for strat in sorted(by_strat.keys()):
        print(f"    {strat}: {len(by_strat[strat])}件")
    print(f"\n[NEXT] レポート生成:")
    print(f"  python holdout_periods_report_daytrade.py "
          f"--universe winners --watchlist {output_path.name} --both")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="walkforward CSV から3-tuple WATCHLIST 自動生成")
    parser.add_argument("--mode", default=None,
                        choices=KNOWN_MODES,
                        help="特定モードのみ採用 (省略時は全モード)")
    parser.add_argument("--min-pf", type=float, default=1.3,
                        help="最低 PF (デフォルト 1.3)")
    parser.add_argument("--min-trades", type=int, default=20,
                        help="最低 取引数 (デフォルト 20)")
    parser.add_argument("--output", default=None,
                        help="出力ファイル名 (省略時 daytrade_winners_<date>.py)")
    args = parser.parse_args()

    generate_watchlist(
        mode=args.mode,
        min_pf=args.min_pf,
        min_trades=args.min_trades,
        output=args.output,
    )


if __name__ == "__main__":
    main()
