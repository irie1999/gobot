"""
compare_entry_margin_v2.py  ―  v2 WATCHLIST で「指値上限マージン」を比較
=================================================================
逆指値注文はトリガー価格で買うが、ギャップアップが発生すると実際の
約定価格が大幅に高くなるリスクがある。
「指値上限マージン」は「トリガー価格から何%以内のギャップアップまで許容するか」
を制御する定数（LIMIT_ENTRY_MARGIN_PCT）。

  超過したギャップ: その日の安値がマージン内に戻れば戻り価格で約定、
                    戻らなければ不約定（キャンセル）。

比較候補:
  成行相当 (∞): ギャップアップ上限なし、常に寄付き価格で約定 → 約定率最大
  1%          : +1%以内のギャップのみ許容 → 厳しい
  3% (現行)   : デフォルト設定
  5%          : やや緩い
  10%         : 非常に緩い

使い方:
  python compare_entry_margin_v2.py               # 365日
  python compare_entry_margin_v2.py --days 180    # 180日
  python compare_entry_margin_v2.py --no-browser
  python compare_entry_margin_v2.py --workers 8

出力: compare_margin_v2_{date}.html
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── TRADING_MODE を import 前に設定 ──────────────────────────────────────────
if "--aggressive" in sys.argv:
    os.environ["TRADING_MODE"] = "aggressive"
else:
    os.environ.setdefault("TRADING_MODE", "conservative")

import backtest_limit_entry as _bte
from backtest_limit_entry import (
    fetch,
    calc_macd, calc_a7, calc_rsi2,
    run_limit_backtest,
    WORKERS as _DEFAULT_WORKERS,
)
from scan_breakout_entry import calc_donchian, calc_vol_breakout, calc_momentum

JST     = timezone(timedelta(hours=9))
DAYS_DEFAULT = 365

# ── v2 WATCHLIST ──────────────────────────────────────────────────────────────
# (symbol, name, strategy, calc_fn, entry_atr_mult, stop_atr_mult, target_atr_mult)
ALL_STOCKS: list[tuple] = [
    # ── 逆指値B: A7 ──
    ("7003.T", "三井Ｅ＆Ｓ",                             "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("6752.T", "パナソニックホールディングス",             "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("8360.T", "山梨中央銀行",                             "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("4506.T", "住友ファーマ",                             "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("6101.T", "ツガミ",                                   "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("1814.T", "大末建設",                                 "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("8173.T", "上新電機",                                 "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("9956.T", "バローホールディングス",                   "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("8544.T", "京葉銀行",                                 "A7",   calc_a7,   0.0, 1.5, 3.0),
    ("5831.T", "しずおかフィナンシャルグループ",           "A7",   calc_a7,   0.0, 1.5, 3.0),
    # ── 逆指値B: RSI2 ──
    ("3563.T", "ＦＯＯＤ＆ＬＩＦＥ ＣＯＭＰＡＮＩＥＳ", "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("8309.T", "三井住友トラスト・ホールディングス",       "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("8061.T", "西華産業",                                 "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("5482.T", "愛知製鋼",                                 "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("5821.T", "平河ヒューテック",                         "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("9960.T", "東テク",                                   "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("6501.T", "日立製作所",                               "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("6237.T", "イワキポンプ",                             "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("2540.T", "養命酒製造",                               "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    ("8393.T", "宮崎銀行",                                 "RSI2", calc_rsi2, 0.0, 2.0, 4.0),
    # ── 逆指値B: MACD ──
    ("6278.T", "ユニオンツール",                           "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("9531.T", "東京瓦斯",                                 "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("1515.T", "日鉄鉱業",                                 "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("9072.T", "ニッコンホールディングス",                 "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("8377.T", "ほくほくフィナンシャルグループ",           "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("7322.T", "三十三フィナンシャルグループ",             "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("5482.T", "愛知製鋼 (MACD)",                          "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("1417.T", "ミライト・ワン",                           "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("7157.T", "ライフネット生命保険",                     "MACD", calc_macd, 0.0, 1.5, 3.0),
    ("6914.T", "オプテックス",                             "MACD", calc_macd, 0.0, 1.5, 3.0),
    # ── ブレイクアウト: MOM ──
    ("7242.T", "カヤバ",                                   "MOM",  calc_momentum,    0.0, 1.5, 3.0),
    ("6762.T", "ＴＤＫ",                                   "MOM",  calc_momentum,    0.0, 1.5, 3.0),
    ("8237.T", "松屋",                                     "MOM",  calc_momentum,    0.0, 1.5, 3.0),
    ("7966.T", "リンテック",                               "MOM",  calc_momentum,    0.0, 1.5, 3.0),
    ("4554.T", "富士製薬工業",                             "MOM",  calc_momentum,    0.0, 1.5, 3.0),
    ("3964.T", "オークネット",                             "MOM",  calc_momentum,    0.0, 1.5, 3.0),
    # ── ブレイクアウト: DON ──
    ("6875.T", "メガチップス",                             "DON",  calc_donchian,    0.0, 1.5, 3.0),
    ("1515.T", "日鉄鉱業 (DON)",                           "DON",  calc_donchian,    0.0, 1.5, 3.0),
    # ── ブレイクアウト: VOL ──
    ("4461.T", "第一工業製薬",                             "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("7013.T", "ＩＨＩ",                                   "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("4390.T", "ＩＰＳ",                                   "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("9887.T", "松屋フーズホールディングス",               "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("6284.T", "日精エー・エス・ビー機械",                 "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("1803.T", "清水建設",                                 "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("1945.T", "東京エネシス",                             "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("9843.T", "ニトリホールディングス",                   "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
    ("6952.T", "カシオ計算機",                             "VOL",  calc_vol_breakout,0.0, 1.5, 3.0),
]

# ── マージン候補 ──────────────────────────────────────────────────────────────
MARGIN_CONFIGS: list[tuple[str, float]] = [
    ("成行相当 (∞)",  99.0),   # 上限なし: 常に寄付き価格で約定
    ("1%",             0.01),
    ("3% (現行)",      0.03),
    ("5%",             0.05),
    ("10%",            0.10),
]

ENTRY_TYPE = "stop"


def _pf_str(pf: float) -> str:
    if pf == float("inf") or pf > 999:
        return "∞"
    return f"{pf:.2f}"


def run_one(sym: str, name: str, strat: str, calc_fn, em: float, sm: float, tm: float,
             days: int) -> dict | None:
    df = fetch(sym, days)
    if df is None:
        return None
    return run_limit_backtest(sym, name, df, calc_fn, em, sm, tm, days, strat,
                              entry_type=ENTRY_TYPE)


def run_all_with_margin(margin: float, days: int, workers: int) -> dict[str, dict]:
    """指定マージンで全銘柄をバックテスト実行。{sym_strat_key: result} を返す。"""
    _bte.LIMIT_ENTRY_MARGIN_PCT = margin
    results: dict[str, dict] = {}

    def _task(row):
        sym, name, strat, calc_fn, em, sm, tm = row
        r = run_one(sym, name, strat, calc_fn, em, sm, tm, days)
        key = f"{sym}_{strat}"
        return key, r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_task, row) for row in ALL_STOCKS]
        for fut in as_completed(futs):
            try:
                key, r = fut.result()
                if r:
                    results[key] = r
            except Exception:
                pass

    return results


def _agg(results: dict[str, dict], strategies: list[str]) -> dict:
    """指定戦略グループの集計値を返す。"""
    trades    = [t for k, r in results.items()
                 for s in strategies if k.endswith(f"_{s}")
                 for t in r.get("trade_log", []) if t.get("reason") != "発注中"]
    signals   = sum(r.get("signals", 0) for k, r in results.items()
                    for s in strategies if k.endswith(f"_{s}"))
    filled    = sum(r.get("filled", 0)  for k, r in results.items()
                    for s in strategies if k.endswith(f"_{s}"))
    n         = len(trades)
    wins      = sum(1 for t in trades if t["pnl"] > 0)
    gp        = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl        = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf        = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    total_pnl = sum(t["pnl"] for t in trades)
    fill_rate = filled / signals * 100 if signals > 0 else 0.0
    avg_pnl   = total_pnl / n if n > 0 else 0.0
    return dict(signals=signals, filled=filled, fill_rate=fill_rate,
                trades=n, wins=wins, wr=wins/n*100 if n else 0,
                pf=pf, total_pnl=total_pnl, avg_pnl=avg_pnl)


def build_html(all_results: dict[str, dict[str, dict]], days: int) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    mode_str  = os.environ.get("TRADING_MODE", "conservative")

    STOP_STRATS = ["MACD", "A7", "RSI2"]
    BRK_STRATS  = ["DON", "VOL", "MOM"]
    ALL_STRATS  = STOP_STRATS + BRK_STRATS
    margin_labels = [m for m, _ in MARGIN_CONFIGS]

    def _row(label: str, strats: list[str], cls: str = "") -> str:
        row = f"<tr><td class='lbl {cls}'>{label}</td>"
        best_pnl = max(
            _agg(all_results[ml], strats)["total_pnl"] for ml in margin_labels
        )
        for ml in margin_labels:
            a = _agg(all_results[ml], strats)
            pnl_cls = "profit" if a["total_pnl"] >= 0 else "loss"
            highlight = " best" if abs(a["total_pnl"] - best_pnl) < 1 else ""
            row += (
                f"<td>{a['signals']}</td>"
                f"<td>{a['fill_rate']:.0f}%</td>"
                f"<td>{a['trades']}</td>"
                f"<td>{a['wr']:.1f}%</td>"
                f"<td>{_pf_str(a['pf'])}</td>"
                f"<td class='{pnl_cls}{highlight}'>{a['total_pnl']:+,.0f}</td>"
                f"<td class='{pnl_cls}'>{a['avg_pnl']:+,.0f}</td>"
            )
        return row + "</tr>"

    margin_headers = ""
    for ml in margin_labels:
        margin_headers += f"<th colspan='7'>{ml}</th>"

    sub_headers = (
        "<th>SIG</th><th>約定率</th><th>取引</th>"
        "<th>勝率</th><th>PF</th><th>損益合計</th><th>平均/取引</th>"
    ) * len(MARGIN_CONFIGS)

    rows = (
        _row("【全戦略】",        ALL_STRATS,  "all")
        + _row("─ 逆指値B 計",   STOP_STRATS, "stop")
        + _row("　MACD",          ["MACD"])
        + _row("　A7",            ["A7"])
        + _row("　RSI2",          ["RSI2"])
        + _row("─ ブレイクアウト計", BRK_STRATS, "brk")
        + _row("　DON",           ["DON"])
        + _row("　VOL",           ["VOL"])
        + _row("　MOM",           ["MOM"])
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>指値上限マージン比較 v2 — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:6px; font-size:1.4rem; }}
  .sub {{ color:#94a3b8; margin-bottom:6px; font-size:0.82rem; line-height:1.7; }}
  .note {{ background:#1e293b; border-left:3px solid #60a5fa; padding:10px 14px;
           margin:16px 0 24px; font-size:0.82rem; color:#cbd5e1; line-height:1.7; }}
  h2 {{ color:#60a5fa; margin:28px 0 10px; font-size:1.05rem;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.78rem; margin-bottom:20px; }}
  th {{ background:#1e293b; color:#94a3b8; padding:5px 7px; text-align:center;
        border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:4px 7px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b88; }}
  .lbl {{ text-align:left !important; font-weight:600; min-width:120px; color:#e2e8f0; }}
  .all  {{ background:#172554 !important; color:#93c5fd !important; font-size:0.9rem; }}
  .stop {{ background:#14532d88; color:#86efac; }}
  .brk  {{ background:#4c1d9588; color:#c4b5fd; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .best   {{ background:#1d4ed844 !important; font-weight:700; }}
  .sep th {{ background:#0f172a; border:none; height:6px; }}
</style>
</head>
<body>
<h1>指値上限マージン 比較バックテスト (v2 WATCHLIST)</h1>
<p class="sub">
  生成日: {today_str}　 期間: {days}日　 モード: {mode_str}<br>
  銘柄数: {len(ALL_STOCKS)} (逆指値B×30 + ブレイクアウト×17)
</p>

<div class="note">
  <b>指値上限マージンとは？</b><br>
  逆指値注文のトリガー価格から <b>何%以内のギャップアップまで許容するか</b> の設定。<br>
  例: 3% の場合、終値1,000円 → 逆指値1,000円 → 翌日寄り付きが1,030円以内なら約定、
  1,031円以上のギャップアップは日中に1,030円以下まで戻れば「戻り価格で約定」、
  戻らなければ <b>キャンセル（不約定）</b> となる。<br><br>
  <b>成行相当(∞)</b>: 上限なし。常に寄付き価格で約定。約定率は最大だが高値掴みリスクあり。<br>
  <b>緑ハイライト</b> = 損益合計が最大の設定
</div>

<h2>戦略別比較</h2>
<table>
  <thead>
    <tr>
      <th rowspan="2">グループ</th>
      {margin_headers}
    </tr>
    <tr>{sub_headers}</tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>

<p class="sub" style="margin-top:8px">
  SIG=シグナル発生数　約定率=約定シグナル/全シグナル　平均/取引=損益合計÷取引数
</p>
</body>
</html>"""
    return html


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",       type=int, default=DAYS_DEFAULT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--workers",    type=int, default=_DEFAULT_WORKERS)
    parser.add_argument("--aggressive", action="store_true")
    args = parser.parse_args()

    mode = os.environ.get("TRADING_MODE", "conservative")
    print(f"={'='*60}")
    print(f"  指値上限マージン比較 (v2 WATCHLIST / {mode})")
    print(f"  銘柄数: {len(ALL_STOCKS)} | 期間: {args.days}日")
    print(f"  候補: {[m for m,_ in MARGIN_CONFIGS]}")
    print(f"={'='*60}")

    orig_margin = _bte.LIMIT_ENTRY_MARGIN_PCT
    all_results: dict[str, dict[str, dict]] = {}

    for label, margin in MARGIN_CONFIGS:
        print(f"\n▶ マージン {label} ({margin*100:.0f}%) を実行中 ...", flush=True)
        results = run_all_with_margin(margin, args.days, args.workers)
        all_results[label] = results
        # 簡易集計をターミナルに表示
        trades = [t for r in results.values()
                  for t in r.get("trade_log", []) if t.get("reason") != "発注中"]
        sigs   = sum(r.get("signals", 0) for r in results.values())
        filled = sum(r.get("filled", 0)  for r in results.values())
        n      = len(trades)
        wins   = sum(1 for t in trades if t["pnl"] > 0)
        gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf     = gp / gl if gl > 0 else float("inf")
        pnl    = sum(t["pnl"] for t in trades)
        print(f"  SIG={sigs} 約定率={filled/sigs*100:.0f}% "
              f"取引={n} 勝率={wins/n*100:.1f}% PF={_pf_str(pf)} "
              f"損益={pnl:+,.0f}円")

    # マージンを元の値に戻す
    _bte.LIMIT_ENTRY_MARGIN_PCT = orig_margin

    # ── ターミナルサマリー ──
    print(f"\n{'='*75}")
    print(f"  {'設定':<16} {'約定率':>6} {'取引':>5} {'勝率':>6} {'PF':>6} {'損益':>14} {'平均/取引':>10}")
    print("  " + "-" * 65)
    for label, _ in MARGIN_CONFIGS:
        results = all_results[label]
        trades  = [t for r in results.values()
                   for t in r.get("trade_log", []) if t.get("reason") != "発注中"]
        sigs    = sum(r.get("signals", 0) for r in results.values())
        filled  = sum(r.get("filled", 0)  for r in results.values())
        n       = len(trades)
        wins    = sum(1 for t in trades if t["pnl"] > 0)
        gp      = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl      = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf      = gp / gl if gl > 0 else float("inf")
        pnl     = sum(t["pnl"] for t in trades)
        avg     = pnl / n if n > 0 else 0
        fr      = filled / sigs * 100 if sigs > 0 else 0
        print(f"  {label:<16} {fr:>5.0f}%  {n:>5}  {wins/n*100 if n else 0:>5.1f}%"
              f"  {_pf_str(pf):>6}  {pnl:>+13,.0f}円  {avg:>+9,.0f}円")

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out   = Path(f"compare_margin_v2_{today}.html")
    out.write_text(build_html(all_results, args.days), encoding="utf-8")
    print(f"\nHTML: {out.resolve()}")

    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
