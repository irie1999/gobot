"""
compare_entry_mult.py  ―  entry_atr_mult 比較バックテスト
=================================================================
現行設定（RSI2=0.5）と全戦略 entry_atr_mult=0.0 を並べて比較する。

【使い方】
  python compare_entry_mult.py               # 365日
  python compare_entry_mult.py --days 180    # 180日
  python compare_entry_mult.py --no-browser
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)

JST     = timezone(timedelta(hours=9))
PERIODS = [30, 90, 180, 365]

WATCHLIST: list[tuple[str, str, str]] = [
    ("4368.T", "扶桑化学工業",          "MACD"),
    ("4186.T", "東京応化工業",          "MACD"),
    ("5714.T", "DOWAホールディングス",   "MACD"),
    ("8341.T", "七十七銀行",            "MACD"),
    ("3964.T", "オークネット",          "MACD"),
    ("4118.T", "カネカ",                "MACD"),
    ("6323.T", "ローツェ",              "MACD"),
    ("5713.T", "住友金属鉱山",          "MACD"),
    ("5805.T", "SWCC",                  "MACD"),
    ("3104.T", "富士紡ホールディングス", "MACD"),
    ("1861.T", "熊谷組",                "A7"),
    ("6331.T", "三菱化工機",            "A7"),
    ("6954.T", "ファナック",            "A7"),
    ("8381.T", "山陰合同銀行",          "A7"),
    ("8031.T", "三井物産",              "A7"),
    ("4554.T", "富士製薬工業",          "A7"),
    ("8141.T", "新光商事",              "A7"),
    ("1605.T", "INPEX",                 "A7"),
    ("4506.T", "住友ファーマ",          "RSI2"),
    ("6644.T", "大崎電気工業",          "RSI2"),
    ("9507.T", "四国電力",              "RSI2"),
    ("9003.T", "相鉄ホールディングス",  "RSI2"),
    ("9678.T", "カナモト",              "RSI2"),
    ("5981.T", "東京製綱",              "RSI2"),
]

# ── 2つの設定 ──────────────────────────────────────────────────────
CONFIGS = {
    "現行 (RSI2=0.5)": {
        "MACD": (calc_macd, 0.0, 1.5, 3.0),
        "A7":   (calc_a7,   0.0, 1.5, 3.0),
        "RSI2": (calc_rsi2, 0.5, 2.0, 4.0),
    },
    "全て0.0 (RSI2=0.0)": {
        "MACD": (calc_macd, 0.0, 1.5, 3.0),
        "A7":   (calc_a7,   0.0, 1.5, 3.0),
        "RSI2": (calc_rsi2, 0.0, 2.0, 4.0),
    },
}


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def run_one(symbol: str, name: str, strategy: str, days: int, config: dict) -> dict | None:
    calc_fn, em, sm, tm = config[strategy]
    df = fetch(symbol, days)
    if df is None:
        return None
    return run_limit_backtest(symbol, name, df, calc_fn, em, sm, tm, days, strategy)


def build_html(
    results: dict[str, dict[str, dict]],   # config_name → symbol_key → result
    show_days: int,
) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    config_names = list(CONFIGS.keys())

    # ── 戦略サマリー比較
    summary_rows = ""
    for strat in ["MACD", "A7", "RSI2"]:
        for cfg_name in config_names:
            all_trades = [
                t
                for sym_key, r in results[cfg_name].items()
                if r and sym_key.endswith(f"_{strat}")
                for t in r.get("trade_log", [])
            ]
            n  = len(all_trades)
            w  = sum(1 for t in all_trades if t["pnl"] > 0)
            wr = w / n * 100 if n else 0
            gp = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
            gl = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
            pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
            tp = sum(t["pnl"] for t in all_trades)
            cls = "profit" if tp >= 0 else "loss"
            summary_rows += f"""
        <tr>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          <td>{cfg_name}</td>
          <td>{n}</td><td>{w}</td>
          <td>{wr:.1f}%</td><td>{_pf_str(pf)}</td>
          <td class="{cls}">{tp:+,.0f}円</td>
        </tr>"""

    # ── 銘柄別比較
    stock_rows = ""
    for strat in ["MACD", "A7", "RSI2"]:
        items = [(sym, name, st) for sym, name, st in WATCHLIST if st == strat]
        for sym, name, st in items:
            key = f"{sym}_{st}"
            cells = ""
            for cfg_name in config_names:
                r = results[cfg_name].get(key)
                if not r or r["trades"] == 0:
                    cells += "<td colspan='4' style='text-align:center;color:#64748b'>-</td>"
                else:
                    pnl_cls = "profit" if r["total_pnl"] >= 0 else "loss"
                    cells += (
                        f"<td>{r['trades']}</td>"
                        f"<td>{r['win_rate']:.0f}%</td>"
                        f"<td>{_pf_str(r['pf'])}</td>"
                        f"<td class='{pnl_cls}'>{r['total_pnl']:+,.0f}</td>"
                    )
            stock_rows += f"""
        <tr>
          <td class="sym">{sym}<br><small>{name}</small></td>
          <td><span class="tag tag-{strat.lower()}">{strat}</span></td>
          {cells}
        </tr>"""

    cfg_headers = "".join(f"<th colspan='4'>{c}</th>" for c in config_names)
    cfg_subheads = "<th>取引</th><th>勝率</th><th>PF</th><th>損益</th>" * len(config_names)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>entry_atr_mult 比較 — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.5rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.85rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.1rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym {{ text-align:left; font-weight:600; min-width:110px; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:99px; font-size:0.75rem; font-weight:600; }}
  .tag-macd {{ background:#1d4ed8; color:#bfdbfe; }}
  .tag-a7   {{ background:#065f46; color:#a7f3d0; }}
  .tag-rsi2 {{ background:#7c3aed; color:#ddd6fe; }}
  .cfg-a {{ background:#1e3a5f; }}
  .cfg-b {{ background:#1a3d2b; }}
</style>
</head>
<body>
<h1>entry_atr_mult 比較バックテスト</h1>
<p class="subtitle">
  生成日: {today_str} ／ 期間: {show_days}日 ／ 銘柄: 24銘柄<br>
  現行: MACD/A7=0.0, RSI2=0.5 &nbsp;|&nbsp; 比較: 全戦略=0.0
</p>

<h2>戦略サマリー比較（{show_days}日）</h2>
<table>
  <thead><tr>
    <th>戦略</th><th>設定</th><th>取引数</th><th>勝数</th><th>勝率</th><th>PF</th><th>損益合計</th>
  </tr></thead>
  <tbody>{summary_rows}</tbody>
</table>

<h2>銘柄別比較（{show_days}日）</h2>
<table>
  <thead>
    <tr>
      <th rowspan="2">銘柄</th>
      <th rowspan="2">戦略</th>
      {cfg_headers}
    </tr>
    <tr>{cfg_subheads}</tr>
  </thead>
  <tbody>{stock_rows}</tbody>
</table>

</body>
</html>"""
    return html


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",       type=int, default=365)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--workers",    type=int, default=_DEFAULT_WORKERS)
    args = parser.parse_args()

    print(f"比較バックテスト開始 ({len(WATCHLIST)}銘柄 × {len(CONFIGS)}設定 × {args.days}日)...", flush=True)

    # 設定ごとに並列実行
    results: dict[str, dict[str, dict]] = {c: {} for c in CONFIGS}

    tasks = [
        (cfg_name, sym, name, strat)
        for cfg_name in CONFIGS
        for sym, name, strat in WATCHLIST
    ]

    def _proc(task):
        cfg_name, sym, name, strat = task
        r = run_one(sym, name, strat, args.days, CONFIGS[cfg_name])
        return cfg_name, f"{sym}_{strat}", r

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_proc, t) for t in tasks]
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                cfg_name, key, r = fut.result()
                results[cfg_name][key] = r
            except Exception:
                pass
            if done % 10 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} 完了", flush=True)

    # ── ターミナル出力 ──
    print()
    print("=" * 75)
    print(f"  entry_atr_mult 比較  ({args.days}日)")
    print("=" * 75)
    print(f"  {'戦略':<6} {'設定':<22} {'取引':>4} {'勝率':>6} {'PF':>6} {'損益':>12}")
    print("  " + "-" * 60)
    for strat in ["MACD", "A7", "RSI2"]:
        for cfg_name, cfg in CONFIGS.items():
            all_trades = [
                t
                for sym, name, st in WATCHLIST if st == strat
                for t in (results[cfg_name].get(f"{sym}_{st}") or {}).get("trade_log", [])
            ]
            n  = len(all_trades)
            w  = sum(1 for t in all_trades if t["pnl"] > 0)
            wr = w / n * 100 if n else 0
            gp = sum(t["pnl"] for t in all_trades if t["pnl"] > 0)
            gl = abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0))
            pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
            tp = sum(t["pnl"] for t in all_trades)
            print(f"  {strat:<6} {cfg_name:<22} {n:>4} {wr:>5.1f}% {_pf_str(pf):>6} {tp:>+12,.0f}円")
        print()

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out_path = Path(f"compare_entry_mult_{today}.html")
    html = build_html(results, args.days)
    out_path.write_text(html, encoding="utf-8")
    print(f"HTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
