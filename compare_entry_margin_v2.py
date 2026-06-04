"""
compare_entry_margin_v2.py  ―  v2 WATCHLIST で「指値上限マージン」を比較
=================================================================
逆指値注文のトリガー後、ギャップアップ時の「指値上限マージン」を
変えた場合の影響を 2 モードで比較する。

【マージンとは】
  逆指値がトリガーしても、ギャップアップ幅が X% を超えると
  日中に X% 以内まで戻ってきたときのみ約定 (戻り約定)、
  戻らなければ不約定 (キャンセル)。

【2つのモード】
  標準 (entry_risk_adjust=False):
    戻り約定でも sp/tp は元の lp ベース固定。
    ep > lp になった分だけ損切り時の損失が大きくなる。

  現実的 (entry_risk_adjust=True, デフォルト):
    戻り約定・ギャップ内約定で ep > lp になった場合、
    sp と tp を (ep - lp) だけシフトして R:R を維持する。
    sp が高くなる → 損切り確率は上がるが損失額は小さい。
    tp が高くなる → 目標価格が高くなるため勝率は少し下がる。

【比較候補】
  成行相当 (∞): ギャップアップ上限なし → 常に寄付き価格で約定
  1%          : トリガーから +1% 以内のみ許容 (厳しい)
  3% (現行)   : デフォルト設定
  5%          : やや緩い
  10%         : 非常に緩い

使い方:
  python compare_entry_margin_v2.py               # 365日・現実的モード
  python compare_entry_margin_v2.py --standard    # 標準モード (sp/tp固定)
  python compare_entry_margin_v2.py --both        # 両モードを並べて比較
  python compare_entry_margin_v2.py --days 180
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

JST          = timezone(timedelta(hours=9))
DAYS_DEFAULT = 365

# ── v2 WATCHLIST ──────────────────────────────────────────────────────────────
# (symbol, name, strategy, calc_fn, em, sm, tm)
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
    ("成行相当 (∞)",  99.0),
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
            days: int, risk_adjust: bool) -> dict | None:
    df = fetch(sym, days)
    if df is None:
        return None
    return run_limit_backtest(sym, name, df, calc_fn, em, sm, tm, days, strat,
                              entry_type=ENTRY_TYPE, entry_risk_adjust=risk_adjust)


def run_all_with_margin(margin: float, days: int, workers: int,
                        risk_adjust: bool) -> dict[str, dict]:
    """指定マージンで全銘柄をバックテスト。{sym_strat_key: result} を返す。"""
    _bte.LIMIT_ENTRY_MARGIN_PCT = margin
    results: dict[str, dict] = {}

    def _task(row):
        sym, name, strat, calc_fn, em, sm, tm = row
        r = run_one(sym, name, strat, calc_fn, em, sm, tm, days, risk_adjust)
        return f"{sym}_{strat}", r

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


def _stat_trades(results: dict[str, dict], strategies: list[str]) -> list[dict]:
    return [
        t
        for k, r in results.items()
        for s in strategies if k.endswith(f"_{s}")
        for t in r.get("trade_log", [])
        if t.get("reason") not in ("発注中",)
    ]


def _agg(results: dict[str, dict], strategies: list[str]) -> dict:
    trades  = _stat_trades(results, strategies)
    signals = sum(r.get("signals", 0) for k, r in results.items()
                  for s in strategies if k.endswith(f"_{s}"))
    filled  = sum(r.get("filled", 0)  for k, r in results.items()
                  for s in strategies if k.endswith(f"_{s}"))
    n       = len(trades)
    wins    = sum(1 for t in trades if t["pnl"] > 0)
    gp      = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl      = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf      = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
    total   = sum(t["pnl"] for t in trades)
    fr      = filled / signals * 100 if signals > 0 else 0.0
    avg     = total / n if n > 0 else 0.0

    # fill_type 内訳
    n_normal   = sum(1 for t in trades if t.get("fill_type") == "normal")
    n_gap_open = sum(1 for t in trades if t.get("fill_type") == "gap_open")
    n_comeback = sum(1 for t in trades if t.get("fill_type") == "gap_comeback")
    # 平均エントリープレミアム (lp からの乖離率)
    premiums = [
        (t["entry_p"] - t["order_limit"]) / t["order_limit"] * 100
        for t in trades
        if t.get("fill_type") in ("gap_open", "gap_comeback") and t.get("order_limit", 0) > 0
    ]
    avg_premium = sum(premiums) / len(premiums) if premiums else 0.0

    return dict(
        signals=signals, filled=filled, fill_rate=fr,
        trades=n, wins=wins, wr=wins/n*100 if n else 0,
        pf=pf, total_pnl=total, avg_pnl=avg,
        n_normal=n_normal, n_gap_open=n_gap_open, n_comeback=n_comeback,
        avg_premium=avg_premium,
    )


def build_html(
    all_results_ra: dict[str, dict[str, dict]],   # entry_risk_adjust=True
    all_results_std: dict[str, dict[str, dict]] | None,  # entry_risk_adjust=False (--both 時)
    days: int,
    show_both: bool,
) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    mode_str  = os.environ.get("TRADING_MODE", "conservative")

    STOP_STRATS = ["MACD", "A7", "RSI2"]
    BRK_STRATS  = ["DON", "VOL", "MOM"]
    ALL_STRATS  = STOP_STRATS + BRK_STRATS
    margin_labels = [m for m, _ in MARGIN_CONFIGS]

    def _best_margin(results_dict, strats):
        return max(margin_labels, key=lambda ml: _agg(results_dict[ml], strats)["total_pnl"])

    def _section_html(results_dict: dict, title: str, adjust_label: str) -> str:
        best_all = _best_margin(results_dict, ALL_STRATS)

        def _row(label: str, strats: list[str], cls: str = "") -> str:
            best = _best_margin(results_dict, strats)
            row = f"<tr><td class='lbl {cls}'>{label}</td>"
            for ml in margin_labels:
                a   = _agg(results_dict[ml], strats)
                pcl = "profit" if a["total_pnl"] >= 0 else "loss"
                hl  = " best" if ml == best else ""
                # 約定内訳
                if a["trades"] > 0:
                    p_normal   = a["n_normal"]   / a["trades"] * 100
                    p_gap_open = a["n_gap_open"] / a["trades"] * 100
                    p_comeback = a["n_comeback"] / a["trades"] * 100
                    breakdown  = (f"<small style='color:#94a3b8'>"
                                  f"通{p_normal:.0f}% "
                                  f"G{p_gap_open:.0f}% "
                                  f"戻{p_comeback:.0f}%</small>")
                    premium_str = (f"<small style='color:#fbbf24'>+{a['avg_premium']:.2f}%</small>"
                                   if a["avg_premium"] > 0.01 else
                                   "<small style='color:#64748b'>-</small>")
                else:
                    breakdown   = "<small>-</small>"
                    premium_str = "<small>-</small>"

                row += (
                    f"<td>{a['fill_rate']:.0f}%</td>"
                    f"<td>{a['trades']}</td>"
                    f"<td>{a['wr']:.1f}%</td>"
                    f"<td>{_pf_str(a['pf'])}</td>"
                    f"<td class='{pcl}{hl}'>{a['total_pnl']:+,.0f}</td>"
                    f"<td class='{pcl}'>{a['avg_pnl']:+,.0f}</td>"
                    f"<td>{breakdown}</td>"
                    f"<td>{premium_str}</td>"
                )
            return row + "</tr>"

        mh = "".join(f"<th colspan='8'>{ml}</th>" for ml in margin_labels)
        sh = (
            "<th>約定率</th><th>取引</th><th>勝率</th><th>PF</th>"
            "<th>損益合計</th><th>平均/取引</th><th>約定内訳</th><th>平均乖離</th>"
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

        best_a = _agg(results_dict[best_all], ALL_STRATS)
        return f"""
<h2>{title} <span style="font-size:0.8rem;color:#94a3b8">{adjust_label}</span></h2>
<p style="color:#94a3b8;font-size:0.8rem;margin-bottom:8px">
  最良マージン (全戦略損益合計): <b style="color:#4ade80">{best_all}</b>
  → 取引 {best_a['trades']} 件 / 勝率 {best_a['wr']:.1f}% / PF {_pf_str(best_a['pf'])}
     / 損益 {best_a['total_pnl']:+,.0f}円
</p>
<table>
  <thead>
    <tr><th rowspan="2">グループ</th>{mh}</tr>
    <tr>{sh}</tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
<p style="color:#64748b;font-size:0.78rem;margin-top:4px">
  約定内訳: 通=通常(ザラ場トリガー) G=ギャップ内(寄付き約定) 戻=ギャップ超過後の戻り約定
  ／平均乖離=ギャップ約定の平均エントリープレミアム (lp からの%上乗せ)
</p>"""

    sec1 = _section_html(all_results_ra, "現実的モード", "ep>lp 時に sp/tp を再計算 → R:R 維持")
    sec2 = (_section_html(all_results_std, "標準モード", "sp/tp 固定 (現行動作と同一)")
            if show_both and all_results_std else "")

    return f"""<!DOCTYPE html>
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
           margin:16px 0 24px; font-size:0.82rem; color:#cbd5e1; line-height:1.8; }}
  h2 {{ color:#60a5fa; margin:28px 0 10px; font-size:1.05rem;
        border-left:3px solid #60a5fa; padding-left:10px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.76rem; margin-bottom:12px; overflow-x:auto; display:block; }}
  th {{ background:#1e293b; color:#94a3b8; padding:5px 6px; text-align:center;
        border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:4px 6px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b88; }}
  .lbl {{ text-align:left !important; font-weight:600; min-width:130px; color:#e2e8f0; }}
  .all  {{ background:#172554 !important; color:#93c5fd !important; font-size:0.88rem; }}
  .stop {{ background:#14532d44; color:#86efac; }}
  .brk  {{ background:#4c1d9544; color:#c4b5fd; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .best   {{ background:#1d4ed855 !important; font-weight:700; text-decoration:underline; }}
</style>
</head>
<body>
<h1>指値上限マージン 比較バックテスト (v2 WATCHLIST)</h1>
<p class="sub">
  生成日: {today_str}　期間: {days}日　モード: {mode_str}<br>
  銘柄数: {len(ALL_STOCKS)} (逆指値B×30 + ブレイクアウト×17)
</p>

<div class="note">
  <b>指値上限マージンとは？</b><br>
  逆指値注文が発動した後、<b>寄付き価格がトリガーから何%以内のギャップアップまで許容するか</b>の設定。<br>
  超過した場合は「戻り約定」(日中に上限以下に戻れば約定) または「不約定」(キャンセル)。<br><br>
  <b>現実的モード (推奨)</b>: 戻り/ギャップ内 で ep &gt; lp になったとき、sp/tp を同額シフト。<br>
  &nbsp;→ 損切り幅・利確幅を元の ATR ベースに維持 (R:R が崩れない)。<br>
  &nbsp;→ ただし sp が高くなるため損切りに引っかかりやすくなる (勝率低下方向)。<br><br>
  <b>標準モード</b>: sp/tp を lp ベースで固定。ep が上がった分だけ損切り損失が大きくなる。<br><br>
  <b>緑下線</b> = そのグループで損益合計が最大のマージン設定
</div>
{sec1}
{sec2}
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",       type=int, default=DAYS_DEFAULT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--workers",    type=int, default=_DEFAULT_WORKERS)
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--standard",   action="store_true",
                        help="標準モード(sp/tp固定)のみ実行")
    parser.add_argument("--both",       action="store_true",
                        help="現実的モードと標準モードを両方実行して並べる")
    args = parser.parse_args()

    mode = os.environ.get("TRADING_MODE", "conservative")
    run_realistic = not args.standard
    run_standard  = args.standard or args.both

    print("=" * 62)
    print(f"  指値上限マージン比較 (v2 WATCHLIST / {mode})")
    print(f"  銘柄数: {len(ALL_STOCKS)} | 期間: {args.days}日")
    modes = []
    if run_realistic: modes.append("現実的 (R:R維持)")
    if run_standard:  modes.append("標準 (sp/tp固定)")
    print(f"  実行モード: {' + '.join(modes)}")
    print(f"  候補: {[m for m,_ in MARGIN_CONFIGS]}")
    print("=" * 62)

    orig_margin = _bte.LIMIT_ENTRY_MARGIN_PCT

    all_results_ra:  dict[str, dict[str, dict]] = {}
    all_results_std: dict[str, dict[str, dict]] | None = None

    def _run_set(risk_adjust: bool) -> dict[str, dict[str, dict]]:
        label = "現実的" if risk_adjust else "標準"
        out: dict[str, dict[str, dict]] = {}
        for ml, margin in MARGIN_CONFIGS:
            print(f"\n  [{label}] マージン {ml} 実行中 ...", flush=True)
            results = run_all_with_margin(margin, args.days, args.workers, risk_adjust)
            out[ml] = results
            trades = [t for r in results.values()
                      for t in r.get("trade_log", []) if t.get("reason") != "発注中"]
            sigs   = sum(r.get("signals", 0) for r in results.values())
            filled = sum(r.get("filled",  0) for r in results.values())
            n      = len(trades)
            wins   = sum(1 for t in trades if t["pnl"] > 0)
            gp     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
            gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
            pf     = gp / gl if gl > 0 else float("inf")
            pnl    = sum(t["pnl"] for t in trades)
            fr     = filled / sigs * 100 if sigs > 0 else 0
            n_comeback = sum(1 for t in trades if t.get("fill_type") == "gap_comeback")
            n_gap_open = sum(1 for t in trades if t.get("fill_type") == "gap_open")
            print(f"    SIG={sigs} 約定率={fr:.0f}% "
                  f"取引={n}(ギャップ内={n_gap_open} 戻り={n_comeback}) "
                  f"勝率={wins/n*100 if n else 0:.1f}% PF={_pf_str(pf)} "
                  f"損益={pnl:+,.0f}円")
        return out

    if run_realistic:
        print("\n▶ 現実的モード (entry_risk_adjust=True)")
        all_results_ra = _run_set(risk_adjust=True)

    if run_standard:
        print("\n▶ 標準モード (entry_risk_adjust=False)")
        all_results_std = _run_set(risk_adjust=False)

    if not run_realistic and all_results_std:
        all_results_ra = all_results_std
        all_results_std = None

    _bte.LIMIT_ENTRY_MARGIN_PCT = orig_margin

    # ── ターミナルサマリー ──
    print(f"\n{'='*72}")
    mode_lbl = "現実的" if run_realistic else "標準"
    disp = all_results_ra if run_realistic else all_results_std
    print(f"  {mode_lbl}モード 全戦略サマリー ({args.days}日)")
    print(f"  {'設定':<16} {'約定率':>6} {'取引':>5} {'勝率':>6} {'PF':>6} {'損益':>14} {'平均/取引':>10} {'戻り約定%':>9}")
    print("  " + "-" * 72)
    for ml, _ in MARGIN_CONFIGS:
        results = disp[ml]
        a = _agg(results, ["MACD", "A7", "RSI2", "DON", "VOL", "MOM"])
        cb_pct = a["n_comeback"] / a["trades"] * 100 if a["trades"] > 0 else 0
        print(f"  {ml:<16} {a['fill_rate']:>5.0f}%  {a['trades']:>5}  "
              f"{a['wr']:>5.1f}%  {_pf_str(a['pf']):>6}  {a['total_pnl']:>+13,.0f}円  "
              f"{a['avg_pnl']:>+9,.0f}円  {cb_pct:>8.1f}%")

    today = datetime.now(JST).strftime("%Y-%m-%d")
    out   = Path(f"compare_margin_v2_{today}.html")
    out.write_text(
        build_html(all_results_ra, all_results_std, args.days, args.both),
        encoding="utf-8"
    )
    print(f"\nHTML: {out.resolve()}")
    if not args.no_browser:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
