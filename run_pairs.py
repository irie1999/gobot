"""
run_pairs.py  ―  ペアトレード バックテスト & シグナルレポート
=============================================================
scan_pairs.py の出力 CSV を読み込み、各ペアについて
スプレッドZ-スコア戦略のバックテストを行い、
現在のシグナルをHTMLレポートで表示する。

【使い方】
  python run_pairs.py                         # 最新CSVを自動検出
  python run_pairs.py --csv pairs_scan_*.csv  # CSVを指定
  python run_pairs.py --z-entry 2.0           # エントリー閾値 (デフォルト2.0)
  python run_pairs.py --z-exit 0.5            # 決済閾値 (デフォルト0.5)
  python run_pairs.py --z-stop 4.0            # ストップ閾値 (デフォルト4.0)
  python run_pairs.py --days 365              # バックテスト期間 (デフォルト365日)
  python run_pairs.py --no-browser
"""

from __future__ import annotations

import io
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_limit_entry import fetch, FEE_PCT_ONE_WAY, WORKERS as _DEFAULT_WORKERS

JST = timezone(timedelta(hours=9))

# バックテスト定数
MAX_HOLD_PAIRS = 30   # ペアトレードの最大保有日数


def _load_latest_csv(csv_path: str | None) -> tuple[pd.DataFrame, Path]:
    """CSV ファイルを読み込む。指定なしの場合は最新の pairs_scan_*.csv を探す。"""
    if csv_path:
        p = Path(csv_path)
        if not p.exists():
            print(f"[ERROR] CSV が見つかりません: {p}", file=sys.stderr)
            sys.exit(1)
        return pd.read_csv(p), p

    candidates = sorted(Path(".").glob("pairs_scan_*.csv"), reverse=True)
    if not candidates:
        print("[ERROR] pairs_scan_*.csv が見つかりません。先に scan_pairs.py を実行してください。",
              file=sys.stderr)
        sys.exit(1)
    p = candidates[0]
    print(f"CSV 自動検出: {p}", flush=True)
    return pd.read_csv(p), p


def _fetch_close(sym: str, days: int) -> pd.Series | None:
    """終値系列を取得する。"""
    df = fetch(sym, days)
    if df is None or len(df) < 30:
        return None
    cutoff = pd.Timestamp(datetime.now(JST).date()) - pd.Timedelta(days=days)
    df = df[df.index >= cutoff]
    if len(df) < 30:
        return None
    return df["close"].astype(float)


def backtest_pair(
    sym_a: str, name_a: str,
    sym_b: str, name_b: str,
    hedge_ratio: float,
    days: int = 365,
    z_entry: float = 2.0,
    z_exit: float = 0.5,
    z_stop: float = 4.0,
    lookback: int = 252,
) -> dict:
    """
    1ペアのバックテストを実行する。

    スプレッド S(t) = log_A(t) - β * log_B(t)
    Z(t) = (S(t) - rolling_mean(lookback)) / rolling_std(lookback)

    シグナル:
      Z > +z_entry → sell_a_buy_b: A を qty_a 株空売り + B を qty_b 株買い
      Z < -z_entry → buy_a_sell_b: A を qty_a 株買い + B を qty_b 株空売り
    決済:
      |Z| < z_exit → 収束、利確
      |Z| > z_stop → 発散、損切り
      保有 > MAX_HOLD_PAIRS 日 → タイムカット
    """
    close_a = _fetch_close(sym_a, days + lookback + 30)
    close_b = _fetch_close(sym_b, days + lookback + 30)

    result_base = dict(
        sym_a=sym_a, name_a=name_a,
        sym_b=sym_b, name_b=name_b,
        hedge_ratio=hedge_ratio,
        trades=0, wins=0, win_rate=0.0,
        total_pnl=0.0, pf=0.0,
        trade_log=[],
        error=None,
    )

    if close_a is None or close_b is None:
        result_base["error"] = "データ取得失敗"
        return result_base

    # 共通インデックスを揃える
    aligned_a, aligned_b = close_a.align(close_b, join="inner")
    if len(aligned_a) < lookback + 10:
        result_base["error"] = f"データ不足 ({len(aligned_a)}行)"
        return result_base

    log_a = np.log(aligned_a.values)
    log_b = np.log(aligned_b.values)
    spread = log_a - hedge_ratio * log_b
    dates  = aligned_a.index
    prices_a = aligned_a.values
    prices_b = aligned_b.values

    # Z-score 計算 (rolling)
    z_scores = np.full(len(spread), np.nan)
    for i in range(lookback, len(spread)):
        window = spread[i - lookback: i]
        mu  = window.mean()
        sig = window.std()
        if sig > 0:
            z_scores[i] = (spread[i] - mu) / sig

    # バックテスト期間の開始インデックス
    cutoff_date = pd.Timestamp(datetime.now(JST).date()) - pd.Timedelta(days=days)
    start_idx = 0
    for i, d in enumerate(dates):
        if d >= cutoff_date:
            start_idx = i
            break

    # 発注数量: A を 100株, B を round(100*β) 株
    qty_a = 100
    qty_b = max(1, round(100 * abs(hedge_ratio)))

    trade_log: list[dict] = []
    position: dict | None = None  # アクティブポジション

    for i in range(max(start_idx, lookback), len(spread)):
        z = z_scores[i]
        if np.isnan(z):
            continue

        pa = prices_a[i]
        pb = prices_b[i]
        dt = dates[i]

        if position is None:
            # エントリー判定
            if z > z_entry:
                # sell_a_buy_b: A が割高 → A 空売り、B 買い
                position = dict(
                    direction="sell_a_buy_b",
                    entry_idx=i,
                    entry_date=dt,
                    entry_a=pa,
                    entry_b=pb,
                    entry_z=z,
                )
            elif z < -z_entry:
                # buy_a_sell_b: A が割安 → A 買い、B 空売り
                position = dict(
                    direction="buy_a_sell_b",
                    entry_idx=i,
                    entry_date=dt,
                    entry_a=pa,
                    entry_b=pb,
                    entry_z=z,
                )
        else:
            # 決済判定
            hold_days = i - position["entry_idx"]
            reason = None

            if abs(z) < z_exit:
                reason = "converge"   # 収束
            elif abs(z) > z_stop:
                reason = "stop"       # 発散損切り
            elif hold_days >= MAX_HOLD_PAIRS:
                reason = "timeout"    # タイムカット

            if reason:
                direction = position["direction"]
                entry_a   = position["entry_a"]
                entry_b   = position["entry_b"]

                # PnL 計算
                if direction == "sell_a_buy_b":
                    # A 空売り: (entry_A - exit_A) * qty_a
                    # B 買い:  (exit_B - entry_B) * qty_b
                    pnl_a = (entry_a - pa) * qty_a
                    pnl_b = (pb - entry_b) * qty_b
                else:
                    # buy_a_sell_b
                    pnl_a = (pa - entry_a) * qty_a
                    pnl_b = (entry_b - pb) * qty_b

                gross_pnl = pnl_a + pnl_b

                # 手数料 (往復 = 片道 × 2 × 4本 の簡略版: A 買い+売り, B 買い+売り)
                fee = (entry_a * qty_a + pa * qty_a + entry_b * qty_b + pb * qty_b) * FEE_PCT_ONE_WAY
                net_pnl = gross_pnl - fee

                trade_log.append(dict(
                    direction=direction,
                    entry_date=position["entry_date"],
                    exit_date=dt,
                    hold_days=hold_days,
                    entry_z=round(float(position["entry_z"]), 3),
                    exit_z=round(float(z), 3),
                    entry_a=round(float(entry_a), 2),
                    exit_a=round(float(pa), 2),
                    entry_b=round(float(entry_b), 2),
                    exit_b=round(float(pb), 2),
                    qty_a=qty_a,
                    qty_b=qty_b,
                    pnl_a=round(float(pnl_a), 0),
                    pnl_b=round(float(pnl_b), 0),
                    fee=round(float(fee), 0),
                    pnl=round(float(net_pnl), 0),
                    reason=reason,
                ))
                position = None

    # 集計
    if not trade_log:
        result_base["error"] = "取引なし"
        return result_base

    trades  = len(trade_log)
    wins    = sum(1 for t in trade_log if t["pnl"] > 0)
    total   = sum(t["pnl"] for t in trade_log)
    gp      = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
    gl      = sum(-t["pnl"] for t in trade_log if t["pnl"] < 0)
    pf      = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)

    return dict(
        sym_a=sym_a, name_a=name_a,
        sym_b=sym_b, name_b=name_b,
        hedge_ratio=hedge_ratio,
        trades=trades,
        wins=wins,
        win_rate=wins / trades * 100,
        total_pnl=total,
        pf=pf,
        trade_log=trade_log,
        error=None,
    )


def _pf_str(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _z_bar(z: float, width: int = 20) -> str:
    """Z-score を CSS バーで可視化する HTML 断片を返す。"""
    clamped = max(-5.0, min(5.0, z))
    # -5 〜 +5 を 0 〜 100% にマップ
    pct_center = 50
    pct_offset = int(clamped / 5.0 * 50)
    if pct_offset >= 0:
        bar_left  = pct_center
        bar_width = pct_offset
        bar_color = "#f87171"   # 赤 (割高側)
    else:
        bar_left  = pct_center + pct_offset
        bar_width = -pct_offset
        bar_color = "#4ade80"   # 緑 (割安側)

    return (
        f'<div style="position:relative;width:100%;height:14px;background:#1e293b;border-radius:3px">'
        f'<div style="position:absolute;left:49%;width:2px;height:100%;background:#475569"></div>'
        f'<div style="position:absolute;left:{bar_left}%;width:{bar_width}%;height:100%;'
        f'background:{bar_color};border-radius:2px;opacity:0.85"></div>'
        f'</div>'
    )


def build_html(
    pairs_df: pd.DataFrame,
    bt_results: list[dict],
    show_days: int,
    z_entry: float,
    z_exit: float,
    z_stop: float,
    csv_name: str,
) -> str:
    today_str = datetime.now(JST).strftime("%Y-%m-%d")

    # ── シグナルサマリー ──────────────────────────────────────────────
    active_rows = ""
    active_pairs = pairs_df[pairs_df["signal"] != "neutral"].copy()
    if active_pairs.empty:
        active_rows = '<tr><td colspan="8" style="text-align:center;color:#94a3b8">現在アクティブシグナルなし</td></tr>'
    else:
        for _, row in active_pairs.iterrows():
            z = row["z_score_current"]
            sig = row["signal"]
            if sig == "sell_a_buy_b":
                direction_html = '<span style="color:#f87171">↑売A買B</span>'
                detail = f"{row['sym_a']}({row['name_a']}) 空売り ← {row['sym_b']}({row['name_b']}) 買い"
            else:
                direction_html = '<span style="color:#4ade80">↓買A売B</span>'
                detail = f"{row['sym_a']}({row['name_a']}) 買い ← {row['sym_b']}({row['name_b']}) 空売り"
            z_cls = "loss" if z > 0 else "profit"
            active_rows += f"""
            <tr>
              <td class="sym">{row['sym_a']}<br><small>{row['name_a']}</small></td>
              <td class="sym">{row['sym_b']}<br><small>{row['name_b']}</small></td>
              <td>{direction_html}</td>
              <td class="{z_cls}">{z:+.3f}</td>
              <td>{_z_bar(z)}</td>
              <td>{row['coint_pvalue']:.4f}</td>
              <td>{row['hedge_ratio']:.4f}</td>
              <td style="text-align:left;font-size:0.78rem">{detail}</td>
            </tr>"""

    # ── バックテスト結果サマリー ──────────────────────────────────────
    bt_rows = ""
    valid_bts = [r for r in bt_results if not r["error"] and r["trades"] > 0]
    valid_bts.sort(key=lambda x: x["total_pnl"], reverse=True)

    for r in bt_results:
        if r["error"]:
            bt_rows += f"""
            <tr>
              <td class="sym">{r['sym_a']}<br><small>{r['name_a']}</small></td>
              <td class="sym">{r['sym_b']}<br><small>{r['name_b']}</small></td>
              <td colspan="6" style="color:#94a3b8;text-align:center">{r['error']}</td>
            </tr>"""
            continue
        pc = "profit" if r["total_pnl"] >= 0 else "loss"
        bt_rows += f"""
        <tr>
          <td class="sym">{r['sym_a']}<br><small>{r['name_a']}</small></td>
          <td class="sym">{r['sym_b']}<br><small>{r['name_b']}</small></td>
          <td>{r['trades']}</td>
          <td>{r['wins']}</td>
          <td>{r['win_rate']:.1f}%</td>
          <td>{_pf_str(r['pf'])}</td>
          <td class="{pc}">{r['total_pnl']:+,.0f}円</td>
          <td>{r['hedge_ratio']:.4f}</td>
        </tr>"""

    # ── 個別トレード ──────────────────────────────────────────────────
    trade_sections = ""
    for r in bt_results:
        if not r["trade_log"]:
            continue
        trade_rows = ""
        for t in r["trade_log"]:
            pnl_cls = "profit" if t["pnl"] > 0 else "loss"
            e_dt = t["entry_date"].strftime("%Y-%m-%d") if hasattr(t["entry_date"], "strftime") else str(t["entry_date"])
            x_dt = t["exit_date"].strftime("%Y-%m-%d")  if hasattr(t["exit_date"],  "strftime") else str(t["exit_date"])
            if t["direction"] == "sell_a_buy_b":
                dir_html = '<span style="color:#f87171">売A買B</span>'
            else:
                dir_html = '<span style="color:#4ade80">買A売B</span>'
            trade_rows += f"""
              <tr>
                <td>{e_dt}</td><td>{x_dt}</td>
                <td>{dir_html}</td>
                <td class="stop">{t['entry_z']:+.3f}</td>
                <td class="stop">{t['exit_z']:+.3f}</td>
                <td>{t['entry_a']:,.1f}</td><td>{t['exit_a']:,.1f}</td>
                <td>{t['entry_b']:,.1f}</td><td>{t['exit_b']:,.1f}</td>
                <td>{t['qty_a']} / {t['qty_b']}</td>
                <td class="profit">{t['pnl_a']:+,.0f}</td>
                <td class="profit">{t['pnl_b']:+,.0f}</td>
                <td class="loss">{t['fee']:,.0f}</td>
                <td class="{pnl_cls}">{t['pnl']:+,.0f}</td>
                <td>{t['hold_days']}日</td>
                <td>{t['reason']}</td>
              </tr>"""
        pc2 = "profit" if r["total_pnl"] >= 0 else "loss"
        trade_sections += f"""
      <div class="trade-section">
        <h3>{r['sym_a']} {r['name_a']} / {r['sym_b']} {r['name_b']}
          <span class="{pc2}">{r['total_pnl']:+,.0f}円</span>
          <small>β={r['hedge_ratio']:.4f}  ({show_days}日)</small>
        </h3>
        <table>
          <thead><tr>
            <th>エントリー</th><th>エグジット</th><th>方向</th>
            <th>エントリーZ</th><th>エグジットZ</th>
            <th>A入値</th><th>A出値</th><th>B入値</th><th>B出値</th>
            <th>数量(A/B)</th><th>A損益</th><th>B損益</th><th>手数料</th>
            <th>純損益</th><th>保有日数</th><th>理由</th>
          </tr></thead>
          <tbody>{trade_rows}</tbody>
        </table>
      </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>ペアトレード バックテスト & シグナル — {today_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:"Segoe UI","Hiragino Sans",sans-serif; background:#0f172a; color:#e2e8f0; padding:20px; }}
  h1 {{ color:#60a5fa; margin-bottom:4px; font-size:1.6rem; }}
  .subtitle {{ color:#94a3b8; margin-bottom:24px; font-size:0.9rem; }}
  h2 {{ color:#60a5fa; margin:28px 0 12px; font-size:1.2rem; border-left:3px solid #60a5fa; padding-left:10px; }}
  h3 {{ color:#e2e8f0; margin:16px 0 8px; font-size:1rem; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px; font-size:0.82rem; }}
  th {{ background:#1e293b; color:#94a3b8; padding:6px 8px; text-align:center; border:1px solid #334155; white-space:nowrap; }}
  td {{ padding:5px 8px; border:1px solid #1e293b; text-align:right; white-space:nowrap; }}
  tr:hover td {{ background:#1e293b; }}
  .sym    {{ text-align:left; font-weight:600; min-width:100px; }}
  .profit {{ color:#4ade80; }}
  .loss   {{ color:#f87171; }}
  .stop   {{ color:#38bdf8; }}
  .trade-section {{ margin-bottom:20px; }}
  .signal-badge {{ background:#38bdf8; color:#000; padding:2px 8px; border-radius:4px; font-size:0.8rem; }}
</style>
</head>
<body>
<h1>ペアトレード バックテスト &amp; シグナルレポート</h1>
<p class="subtitle">
  生成日: {today_str} ／ ソース CSV: {csv_name} ／ バックテスト期間: {show_days}日<br>
  Z エントリー閾値: ±{z_entry} ／ Z 決済閾値: ±{z_exit} ／ Z ストップ閾値: ±{z_stop} ／
  最大保有: {MAX_HOLD_PAIRS}日 ／ 手数料: 片道 {FEE_PCT_ONE_WAY*100:.2f}%（往復 {FEE_PCT_ONE_WAY*200:.2f}%）
</p>

<h2>現在のシグナル <span class="signal-badge">{len(active_pairs)}件</span></h2>
<p style="color:#94a3b8;font-size:0.82rem;margin-bottom:8px">
  ※ Z-score バーの赤 = A割高B割安（売A買B）、緑 = A割安B割高（買A売B）
</p>
<table>
  <thead><tr>
    <th>銘柄A</th><th>銘柄B</th><th>方向</th>
    <th>Z-score</th><th style="min-width:120px">Zバー (-5 ← 0 → +5)</th>
    <th>p値</th><th>β</th><th style="text-align:left">オペレーション</th>
  </tr></thead>
  <tbody>{active_rows}</tbody>
</table>

<h2>バックテスト結果（{show_days}日）</h2>
<table>
  <thead><tr>
    <th>銘柄A</th><th>銘柄B</th><th>取引数</th><th>勝数</th>
    <th>勝率</th><th>PF</th><th>損益合計</th><th>β</th>
  </tr></thead>
  <tbody>{bt_rows}</tbody>
</table>

<h2>個別トレード一覧（{show_days}日）</h2>
{trade_sections if trade_sections else '<p style="color:#94a3b8">取引なし</p>'}

</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="ペアトレード バックテスト & シグナルレポート")
    parser.add_argument("--csv",        type=str,   default=None,      help="CSVファイルパス (省略時=最新自動検出)")
    parser.add_argument("--z-entry",    type=float, default=2.0,       help="エントリーZ閾値 (デフォルト2.0)")
    parser.add_argument("--z-exit",     type=float, default=0.5,       help="決済Z閾値 (デフォルト0.5)")
    parser.add_argument("--z-stop",     type=float, default=4.0,       help="ストップZ閾値 (デフォルト4.0)")
    parser.add_argument("--days",       type=int,   default=365,       help="バックテスト期間 (デフォルト365)")
    parser.add_argument("--workers",    type=int,   default=_DEFAULT_WORKERS)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    pairs_df, csv_path = _load_latest_csv(args.csv)
    if pairs_df.empty:
        print("[ERROR] CSV が空です。", file=sys.stderr)
        sys.exit(1)

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"\nペアトレード バックテスト開始 ({len(pairs_df)}ペア, {args.days}日)...", flush=True)
    print(f"  Z エントリー: ±{args.z_entry}  決済: ±{args.z_exit}  ストップ: ±{args.z_stop}", flush=True)

    # 並列バックテスト
    bt_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(
                backtest_pair,
                row["sym_a"], row["name_a"],
                row["sym_b"], row["name_b"],
                float(row["hedge_ratio"]),
                args.days, args.z_entry, args.z_exit, args.z_stop,
            ): i
            for i, row in pairs_df.iterrows()
        }
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                bt_results.append(fut.result())
            except Exception as e:
                pass
            if done % 5 == 0 or done == len(pairs_df):
                print(f"  {done}/{len(pairs_df)} 完了", flush=True)

    # coint_pvalue 順に並べ直す（pairs_df の順序に合わせる）
    sym_pair_order = {(r["sym_a"], r["sym_b"]): i for i, r in enumerate(
        pairs_df[["sym_a", "sym_b"]].to_dict("records"))}
    bt_results.sort(key=lambda x: sym_pair_order.get((x["sym_a"], x["sym_b"]), 999))

    # コンソール結果表示
    print(f"\n{'ペア':<35} {'取引':>4} {'勝率':>6} {'PF':>6} {'損益':>10}")
    print("-" * 65)
    for r in bt_results:
        pair_str = f"{r['sym_a']}({r['name_a'][:5]})/{r['sym_b']}({r['name_b'][:5]})"
        if r["error"]:
            print(f"  {pair_str:<35}  {r['error']}")
        else:
            print(f"  {pair_str:<35} {r['trades']:>4} {r['win_rate']:>5.1f}%"
                  f" {_pf_str(r['pf']):>6} {r['total_pnl']:>+10,.0f}円")

    # アクティブシグナル
    active = pairs_df[pairs_df["signal"] != "neutral"]
    print(f"\n【アクティブシグナル ({len(active)}件)】")
    if not active.empty:
        for _, row in active.iterrows():
            direction = "A売・B買" if row["signal"] == "sell_a_buy_b" else "A買・B売"
            print(f"  [{direction}] {row['sym_a']}({row['name_a']}) / {row['sym_b']}({row['name_b']})"
                  f"  Z={row['z_score_current']:+.3f}  p={row['coint_pvalue']:.4f}")
    else:
        print("  (なし)")

    # HTML 出力
    html = build_html(
        pairs_df=pairs_df,
        bt_results=bt_results,
        show_days=args.days,
        z_entry=args.z_entry,
        z_exit=args.z_exit,
        z_stop=args.z_stop,
        csv_name=csv_path.name,
    )

    out_path = Path(f"pairs_report_{today_str}.html")
    out_path.write_text(html, encoding="utf-8")
    print(f"\nHTMLレポート: {out_path.resolve()}")

    if not args.no_browser:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
