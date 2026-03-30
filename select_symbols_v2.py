"""
V2 銘柄選定スクリプト — 3戦略 × 4期間バックテスト自動実行・銘柄選定
────────────────────────────────────────────────────────────────────────
実行:
  python select_symbols_v2.py             # 全戦略 × 4期間 でバックテスト
  python select_symbols_v2.py --macd      # MACD のみ
  python select_symbols_v2.py --a7        # A7 のみ
  python select_symbols_v2.py --rsi2      # RSI2 のみ
  python select_symbols_v2.py --top 25    # 各戦略 25 銘柄選定

出力:
  - コンソールに各戦略の推奨銘柄一覧を表示
  - symbols_watch_macd_v2.py    (MACD 推奨銘柄ファイル)
  - symbols_watch_a7_v2.py      (A7 推奨銘柄ファイル)
  - symbols_watch_rsi2_v2.py    (RSI2 推奨銘柄ファイル)
  - select_v2_report.html       (総合 HTML レポート・自動で開く)
"""

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

# ── 各戦略の V2 モジュールをインポート ────────────────────────
import backtest_macd_scan_v2         as macd_mod
import backtest_stoch_atr_trail_v2   as a7_mod
import rsi2_hv_v2                    as rsi2_mod

PERIODS = [
    (30,  "1ヶ月"),
    (90,  "3ヶ月"),
    (180, "6ヶ月"),
    (365, "1年"),
]

WORKERS = 16


# ══════════════════════════════════════════════════════════════════════
# MACD バックテスト
# ══════════════════════════════════════════════════════════════════════

def run_macd_period(sym: str, name: str, df, days: int) -> dict | None:
    """1銘柄・1期間の MACD バックテスト結果を返す"""
    try:
        r = macd_mod.run_backtest(sym, name, df, days)
        if r is None:
            return None
        return {
            "symbol":   sym,
            "name":     name,
            "trades":   r["trades"],
            "win_rate": r["win_rate"],
            "ret_pct":  r["ret_pct"],
            "pf":       r["pf"],
            "avg_hold": r["avg_hold"],
        }
    except Exception:
        return None


def run_macd_all(target: list[tuple], top_n: int) -> list[dict]:
    """全対象銘柄 × 4期間 MACD バックテストを実行し、推奨銘柄リストを返す"""
    total = len(target)
    print(f"\n  [MACD] データ取得中 ({total}銘柄)...")
    stock_data: dict[str, tuple] = {}
    for i, (sym, name) in enumerate(target, 1):
        df = macd_mod.fetch_df(sym, backtest_days=365)
        if df is not None:
            stock_data[sym] = (name, df)
        print(f"  {i}/{total}", end="\r", flush=True)
    print(f"  完了 {len(stock_data)}銘柄      ")

    # 4期間バックテスト
    scores: dict[str, dict] = {}
    for days, label in PERIODS:
        print(f"  [MACD] {label} バックテスト中...")
        tasks = [(s, n, d, days) for s, (n, d) in stock_data.items()]

        def _bt(task):
            return run_macd_period(task[0], task[1], task[2], task[3])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_bt, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                sym = r["symbol"]
                if sym not in scores:
                    scores[sym] = {
                        "symbol": sym, "name": r["name"],
                        "periods": {}, "score": 0,
                    }
                scores[sym]["periods"][label] = r

    # スコアリング
    for d in scores.values():
        sc = 0
        ret_sum = 0.0
        for label, r in d["periods"].items():
            if r["ret_pct"] > 0:  sc += 3
            if r["win_rate"] >= 60: sc += 2
            if r["trades"] >= 2:    sc += 1
            if r["pf"] >= 1.5:      sc += 1
            ret_sum += r["ret_pct"]
        d["score"]   = sc
        d["ret_sum"] = ret_sum
        d["pos_cnt"] = sum(1 for r in d["periods"].values() if r["ret_pct"] > 0)

    ranked = sorted(scores.values(),
                    key=lambda x: (-x["score"], -x["ret_sum"]))
    return ranked[:top_n]


# ══════════════════════════════════════════════════════════════════════
# A7 バックテスト
# ══════════════════════════════════════════════════════════════════════

def run_a7_period(sym: str, name: str, df, days: int) -> dict | None:
    try:
        r = a7_mod.run_backtest(sym, name, df, days)
        if r is None:
            return None
        return {
            "symbol":   sym,
            "name":     name,
            "trades":   r["trades"],
            "win_rate": r["win_rate"],
            "ret_pct":  r["ret_pct"],
            "pf":       (sum(t["pnl"] for t in r["trade_log"] if t["pnl"] > 0) /
                         abs(sum(t["pnl"] for t in r["trade_log"] if t["pnl"] < 0))
                         if any(t["pnl"] < 0 for t in r["trade_log"]) else float("inf")),
            "avg_hold": r["avg_hold"],
        }
    except Exception:
        return None


def run_a7_all(target: list[tuple], top_n: int) -> list[dict]:
    total = len(target)
    print(f"\n  [A7] データ取得中 ({total}銘柄)...")
    stock_data: dict[str, tuple] = {}
    for i, (sym, name) in enumerate(target, 1):
        df = a7_mod.fetch_df(sym, backtest_days=365)
        if df is not None:
            stock_data[sym] = (name, df)
        print(f"  {i}/{total}", end="\r", flush=True)
    print(f"  完了 {len(stock_data)}銘柄      ")

    scores: dict[str, dict] = {}
    for days, label in PERIODS:
        print(f"  [A7] {label} バックテスト中...")
        tasks = [(s, n, d, days) for s, (n, d) in stock_data.items()]

        def _bt(task):
            return run_a7_period(task[0], task[1], task[2], task[3])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_bt, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                sym = r["symbol"]
                if sym not in scores:
                    scores[sym] = {
                        "symbol": sym, "name": r["name"],
                        "periods": {}, "score": 0,
                    }
                scores[sym]["periods"][label] = r

    for d in scores.values():
        sc = 0
        ret_sum = 0.0
        for label, r in d["periods"].items():
            if r["ret_pct"] > 0:    sc += 3
            if r["win_rate"] >= 60: sc += 2
            if r["trades"] >= 2:    sc += 1
            if r["pf"] >= 1.5:      sc += 1
            ret_sum += r["ret_pct"]
        d["score"]   = sc
        d["ret_sum"] = ret_sum
        d["pos_cnt"] = sum(1 for r in d["periods"].values() if r["ret_pct"] > 0)

    ranked = sorted(scores.values(),
                    key=lambda x: (-x["score"], -x["ret_sum"]))
    return ranked[:top_n]


# ══════════════════════════════════════════════════════════════════════
# RSI2 バックテスト
# ══════════════════════════════════════════════════════════════════════

def run_rsi2_period(sym: str, name: str, df_raw, days: int, params: dict) -> dict | None:
    try:
        df_c   = rsi2_mod.calc(df_raw)
        trades = rsi2_mod.backtest_hv(
            df_c, days, params,
            use_ibs=True, use_consec=False,
        )
        if not trades:
            return None
        wins     = [t for t in trades if t["pnl"] > 0]
        loss     = [t for t in trades if t["pnl"] <= 0]
        total    = sum(t["pnl"] for t in trades)
        ret_pct  = sum(t["pct"] for t in trades)
        wr       = len(wins) / len(trades) * 100
        pf       = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in loss))
                    if loss and sum(t["pnl"] for t in loss) != 0 else float("inf"))
        avg_hold = sum(t["hold"] for t in trades) / len(trades)
        return {
            "symbol":   sym,
            "name":     name,
            "trades":   len(trades),
            "win_rate": wr,
            "ret_pct":  ret_pct,
            "pf":       pf,
            "avg_hold": avg_hold,
        }
    except Exception:
        return None


def run_rsi2_all(target: list[tuple], top_n: int) -> list[dict]:
    # 日経データでモード判定
    print(f"\n  [RSI2] 日経データ取得・モード判定中...")
    nk_df  = rsi2_mod.fetch_nikkei(365)
    mkt    = rsi2_mod._market_info(nk_df)
    if mkt.get("ok") and mkt["above200"]:
        params     = rsi2_mod.NORMAL
        mode_label = "通常モード"
    else:
        params     = rsi2_mod.HV
        mode_label = "高ボラモード"
    print(f"  モード: {mode_label}  (RSI2_ENTRY={params['RSI2_ENTRY']})")

    total = len(target)
    print(f"  [RSI2] データ取得中 ({total}銘柄)...")
    stock_data: dict[str, tuple] = {}
    for i, (sym, name) in enumerate(target, 1):
        df = rsi2_mod.fetch(sym, backtest_days=365)
        if df is not None:
            stock_data[sym] = (name, df)
        print(f"  {i}/{total}", end="\r", flush=True)
    print(f"  完了 {len(stock_data)}銘柄      ")

    scores: dict[str, dict] = {}
    for days, label in PERIODS:
        print(f"  [RSI2] {label} バックテスト中...")
        tasks = [(s, n, d, days, params) for s, (n, d) in stock_data.items()]

        def _bt(task):
            return run_rsi2_period(task[0], task[1], task[2], task[3], task[4])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_bt, t): t[0] for t in tasks}
            for fut in as_completed(futs):
                r = fut.result()
                if r is None:
                    continue
                sym = r["symbol"]
                if sym not in scores:
                    scores[sym] = {
                        "symbol": sym, "name": r["name"],
                        "periods": {}, "score": 0,
                    }
                scores[sym]["periods"][label] = r

    for d in scores.values():
        sc = 0
        ret_sum = 0.0
        for label, r in d["periods"].items():
            if r["ret_pct"] > 0:    sc += 3
            if r["win_rate"] >= 60: sc += 2
            if r["trades"] >= 2:    sc += 1
            if r["pf"] >= 1.5:      sc += 1
            ret_sum += r["ret_pct"]
        d["score"]   = sc
        d["ret_sum"] = ret_sum
        d["pos_cnt"] = sum(1 for r in d["periods"].values() if r["ret_pct"] > 0)

    ranked = sorted(scores.values(),
                    key=lambda x: (-x["score"], -x["ret_sum"]))
    return ranked[:top_n]


# ══════════════════════════════════════════════════════════════════════
# 出力: symbols_watch_*_v2.py
# ══════════════════════════════════════════════════════════════════════

def write_symbols_file(selected: list[dict], strategy: str, filename: str) -> Path:
    """選定銘柄を symbols_watch_*_v2.py として書き出す"""
    today = datetime.today().strftime("%Y-%m-%d")
    lines = [
        f'"""',
        f'{strategy} 戦略 監視対象銘柄 V2 — 4期間バックテスト選定 ({today})',
        f'select_symbols_v2.py により自動生成',
        f'"""',
        f'SYMBOLS = [',
    ]

    # スコア別にコメントを付ける
    for d in selected:
        pos = d["pos_cnt"]
        ret = d["ret_sum"]
        if pos == 4:
            comment = f"# ★4期間全プラス  累積{ret:+.1f}%"
        elif pos == 3:
            comment = f"# ★3期間プラス    累積{ret:+.1f}%"
        elif pos == 2:
            comment = f"# 2期間プラス      累積{ret:+.1f}%"
        else:
            comment = f"# 1期間プラス      累積{ret:+.1f}%"
        lines.append(f'    ("{d["symbol"]}", "{d["name"]}"),  {comment}')

    lines.append(']')
    path = Path(filename)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════
# コンソール表示
# ══════════════════════════════════════════════════════════════════════

def print_results(selected: list[dict], strategy: str, top_n: int) -> None:
    print()
    print("=" * 76)
    print(f"  {strategy} 推奨銘柄 TOP{top_n}  (4期間スコア順)")
    print("=" * 76)
    print(f"  {'#':<3} {'銘柄':<24} {'スコア':>5} {'期間+':>5}  "
          f"{'1M%':>7} {'3M%':>7} {'6M%':>7} {'1Y%':>7}  1M取引  3M取引  6M取引  1Y取引")
    print("  " + "-" * 96)

    for i, d in enumerate(selected, 1):
        p = d["periods"]
        def _r(label):
            r = p.get(label)
            if r is None:
                return "    N/A", "  -"
            sign = "+" if r["ret_pct"] >= 0 else ""
            return f"{sign}{r['ret_pct']:>6.1f}%", f"{r['trades']:>3}回"

        r1m_s, t1m = _r("1ヶ月")
        r3m_s, t3m = _r("3ヶ月")
        r6m_s, t6m = _r("6ヶ月")
        r1y_s, t1y = _r("1年")

        mark = "★" if d["pos_cnt"] >= 3 else " "
        label = f"{d['name']}({d['symbol']})"
        print(f" {mark}{i:<3} {label:<24} {d['score']:>5}  {d['pos_cnt']}/4  "
              f"{r1m_s} {r3m_s} {r6m_s} {r1y_s}   "
              f"{t1m}  {t3m}  {t6m}  {t1y}")

    print()


# ══════════════════════════════════════════════════════════════════════
# HTML レポート生成
# ══════════════════════════════════════════════════════════════════════

def _strategy_table_html(selected: list[dict], strategy: str, color: str) -> str:
    rows = ""
    for i, d in enumerate(selected, 1):
        p = d["periods"]
        def cell(label):
            r = p.get(label)
            if r is None:
                return '<td class="num" style="color:#555">N/A</td><td class="num" style="color:#555">-</td>'
            cls = "pos" if r["ret_pct"] >= 0 else "neg"
            sign = "+" if r["ret_pct"] >= 0 else ""
            return (f'<td class="num {cls}">{sign}{r["ret_pct"]:.1f}%</td>'
                    f'<td class="num">{r["trades"]}回</td>')

        mark  = "★" if d["pos_cnt"] >= 3 else ""
        label = f'{d["name"]}<br><small>{d["symbol"]}</small>'
        rows += (
            f'<tr>'
            f'<td class="rank">{i}</td>'
            f'<td class="name">{mark}{label}</td>'
            f'<td class="num">{d["score"]}</td>'
            f'<td class="num">{d["pos_cnt"]}/4</td>'
            + cell("1ヶ月") + cell("3ヶ月") + cell("6ヶ月") + cell("1年") +
            f'</tr>\n'
        )

    return f"""
<h2 style="color:{color};border-left:4px solid {color};padding-left:10px;margin:30px 0 10px">
  {strategy} 推奨銘柄
</h2>
<table>
  <thead>
    <tr>
      <th>#</th><th>銘柄</th><th>スコア</th><th>期間+</th>
      <th>1M%</th><th>1M回</th>
      <th>3M%</th><th>3M回</th>
      <th>6M%</th><th>6M回</th>
      <th>1Y%</th><th>1Y回</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
"""


def generate_html(
    macd_sel: list[dict] | None,
    a7_sel:   list[dict] | None,
    rsi2_sel: list[dict] | None,
) -> Path:
    today = datetime.today().strftime("%Y-%m-%d")

    body = ""
    if macd_sel:
        body += _strategy_table_html(macd_sel, "MACD V2", "#38bdf8")
    if a7_sel:
        body += _strategy_table_html(a7_sel,   "A7 V2",   "#4ade80")
    if rsi2_sel:
        body += _strategy_table_html(rsi2_sel, "RSI2 V2", "#f59e0b")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>V2 銘柄選定レポート {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Hiragino Kaku Gothic ProN',Meiryo,sans-serif;
     background:#0f172a;color:#e2e8f0;padding:24px;font-size:13px}}
h1{{font-size:1.4em;color:#fff;margin-bottom:6px}}
.subtitle{{color:#94a3b8;font-size:.85em;margin-bottom:24px}}
h2{{font-size:1.1em;margin:0}}
table{{width:100%;border-collapse:collapse;margin-bottom:10px}}
th{{background:#1e293b;padding:7px 10px;text-align:center;color:#94a3b8;
    font-weight:600;white-space:nowrap;border-bottom:1px solid #334155}}
td{{padding:7px 10px;border-bottom:1px solid #1e293b;white-space:nowrap}}
tr:hover{{background:#1e293b}}
.rank{{text-align:center;color:#64748b;font-weight:700}}
.name{{font-weight:600}}
.name small{{color:#64748b;font-weight:400;display:block}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:#4ade80}}
.neg{{color:#f87171}}
.note{{color:#94a3b8;font-size:.8em;margin-top:20px}}
</style>
</head>
<body>
<h1>V2 銘柄選定レポート</h1>
<p class="subtitle">
  生成: {today} &nbsp;|&nbsp;
  スコア = 期間プラス×3 + 勝率60%以上×2 + 取引2回以上×1 + PF1.5以上×1<br>
  ★ = 3期間以上プラス &nbsp;|&nbsp; use_consec=False, use_ibs=True (RSI2)
</p>
{body}
<p class="note">
  ※ 各戦略の V2 パラメータ使用<br>
  ※ MACD V2: VOL_SPIKE×1.0 / RSI上限65 / MA200フィルター無効<br>
  ※ A7 V2: STOCH_K=9 / 50MA トレンドフィルター / 過熱判定80<br>
  ※ RSI2 V2: NORMAL ENTRY≤15 / HV ENTRY≤8 / 連続RSI確認なし
</p>
</body>
</html>"""

    path = Path(f"select_v2_report_{today}.html")
    path.write_text(html, encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="V2 銘柄選定スクリプト — 3戦略×4期間バックテスト自動実行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python select_symbols_v2.py              # 全戦略（対象: 全上場 or 225銘柄）
  python select_symbols_v2.py --macd       # MACD のみ
  python select_symbols_v2.py --a7         # A7 のみ
  python select_symbols_v2.py --rsi2       # RSI2 のみ
  python select_symbols_v2.py --top 25     # 各戦略 25 銘柄選定
  python select_symbols_v2.py --universe prime     # プライム上場銘柄
  python select_symbols_v2.py --universe standard  # プライム+スタンダード
  python select_symbols_v2.py --universe all       # 全上場銘柄
  python select_symbols_v2.py --universe 225       # 日経225のみ（デフォルト）

  ※ prime/standard/all を使う場合は先に以下を実行:
    python fetch_listed_symbols.py --market prime
""")
    parser.add_argument("--macd",  action="store_true", help="MACD 戦略のみ実行")
    parser.add_argument("--a7",    action="store_true", help="A7 戦略のみ実行")
    parser.add_argument("--rsi2",  action="store_true", help="RSI2 戦略のみ実行")
    parser.add_argument("--top",   type=int, default=20, help="選定銘柄数 (default: 20)")
    parser.add_argument("--universe", default=None,
                        choices=["225", "prime", "standard", "all"],
                        help="スキャン対象 (default: 全上場ファイルがあれば使用、なければ225)")
    args = parser.parse_args()

    # フラグ未指定 → 全戦略
    run_all = not (args.macd or args.a7 or args.rsi2)
    do_macd = run_all or args.macd
    do_a7   = run_all or args.a7
    do_rsi2 = run_all or args.rsi2

    top_n  = args.top
    today  = datetime.today().strftime("%Y-%m-%d")

    # ── 対象銘柄ユニバースを決定 ───────────────────────────────
    universe_label = ""
    all_symbols: list[tuple]

    if args.universe == "225" or args.universe is None:
        # 全上場ファイルを優先して自動選択
        for candidate in ["symbols_listed_prime.py",
                          "symbols_listed_standard.py",
                          "symbols_listed_all.py"]:
            p = Path(candidate)
            if p.exists() and args.universe is None:
                import importlib.util
                spec = importlib.util.spec_from_file_location("_listed", p)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                all_symbols   = mod.SYMBOLS
                universe_label = f"{candidate} ({len(all_symbols)}銘柄)"
                break
        else:
            all_symbols   = macd_mod._ALL_SYMBOLS  # 225 銘柄
            universe_label = f"日経225 ({len(all_symbols)}銘柄)"
    else:
        fname = f"symbols_listed_{args.universe}.py"
        p = Path(fname)
        if not p.exists():
            print(f"\n  エラー: {fname} が見つかりません。")
            print(f"  先に以下を実行してください:")
            print(f"    python fetch_listed_symbols.py --market {args.universe}\n")
            return
        import importlib.util
        spec = importlib.util.spec_from_file_location("_listed", p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        all_symbols   = mod.SYMBOLS
        universe_label = f"{fname} ({len(all_symbols)}銘柄)"

    print()
    print("=" * 60)
    print(f"  V2 銘柄選定  ({today})")
    print(f"  対象ユニバース: {universe_label}")
    print(f"  期間: 1M / 3M / 6M / 1Y")
    print(f"  選定数: 各戦略 TOP {top_n}")
    print("=" * 60)

    macd_sel = a7_sel = rsi2_sel = None

    if do_macd:
        macd_sel = run_macd_all(all_symbols, top_n)
        print_results(macd_sel, "MACD V2", top_n)
        p = write_symbols_file(macd_sel, "MACD V2", "symbols_watch_macd_v2.py")
        print(f"  → {p} を出力しました")

    if do_a7:
        a7_sel = run_a7_all(all_symbols, top_n)
        print_results(a7_sel, "A7 V2", top_n)
        p = write_symbols_file(a7_sel, "A7 V2", "symbols_watch_a7_v2.py")
        print(f"  → {p} を出力しました")

    if do_rsi2:
        rsi2_sel = run_rsi2_all(all_symbols, top_n)
        print_results(rsi2_sel, "RSI2 V2", top_n)
        p = write_symbols_file(rsi2_sel, "RSI2 V2", "symbols_watch_rsi2_v2.py")
        print(f"  → {p} を出力しました")

    # HTML レポート
    html_path = generate_html(macd_sel, a7_sel, rsi2_sel)
    print(f"\n  HTMLレポート: {html_path.resolve()}")
    webbrowser.open(html_path.resolve().as_uri())
    print()


if __name__ == "__main__":
    main()
