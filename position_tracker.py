"""
position_tracker.py — 保有ポジション管理ツール
=================================================

約定銘柄を登録して「保有日数」「残り日数」「含み損益」をリアルタイムで確認する。

使い方:
  # 現在のポジションを一覧表示
  python position_tracker.py

  # 新しいポジションを登録（最小入力: 証券コード 約定値 損切値 目標値）
  python position_tracker.py --add 4631 4806 4486 5069

  # 戦略・株数・信用区分も指定
  python position_tracker.py --add 4631 4806 4486 5069 --strategy RSI2 --qty 100 --margin

  # ポジションを決済済みにする（損切り/目標/手動決済）
  python position_tracker.py --close 4631 --exit-price 5069 --reason target

  # 全ポジション（決済済み含む）を表示
  python position_tracker.py --all

CSV: my_positions.csv（既存ファイルを引き継ぎ）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

CSV_PATH = os.path.join(os.path.dirname(__file__), "my_positions.csv")
TODAY = date.today()

# 戦略別 MAX_HOLD（backtest_limit_entry.py と同期）
_MAX_HOLD_BY_STRATEGY = {"RSI2": 7, "MOM": 20}
DEFAULT_MAX_HOLD = 15

COLS = [
    "record_date", "symbol", "name", "strategy", "family",
    "signal_date", "signal_price", "order_price",
    "stop_price", "target_price",
    "status", "fill_date", "fill_price",
    "exit_date", "exit_price", "pnl",
    "updated_date", "side", "qty", "cash_margin",
]


def max_hold(strategy: str) -> int:
    return _MAX_HOLD_BY_STRATEGY.get((strategy or "").upper(), DEFAULT_MAX_HOLD)


def load() -> pd.DataFrame:
    if not os.path.exists(CSV_PATH):
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(CSV_PATH, dtype=str)
    for c in COLS:
        if c not in df.columns:
            df[c] = ""
    return df[COLS]


def save(df: pd.DataFrame) -> None:
    df[COLS].to_csv(CSV_PATH, index=False)


def _business_days(start: date, end: date) -> int:
    """営業日数（土日除く簡易版）"""
    if start >= end:
        return 0
    days = 0
    cur = start
    while cur < end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def fetch_price(symbol: str) -> float | None:
    code = symbol.split(".")[0]
    ticker = f"{code}.T"
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


# ── 登録 ────────────────────────────────────────────────────────────────────
def cmd_add(args) -> None:
    df = load()

    symbol = str(args.symbol).split(".")[0].strip()
    entry_p = float(args.entry_price)
    stop_p  = float(args.stop_price)
    target_p = float(args.target_price)
    strategy = (args.strategy or "").upper()
    qty      = int(args.qty)
    margin   = 3 if args.margin else 1  # 3=信用, 1=現物
    fill_date = str(args.fill_date or TODAY)
    side     = "long"

    # 銘柄名を yfinance から取得（失敗したら空欄）
    name = ""
    try:
        ticker = yf.Ticker(f"{symbol}.T")
        info = ticker.info
        name = info.get("shortName", info.get("longName", ""))[:10]
    except Exception:
        pass

    mh = max_hold(strategy)

    row = {
        "record_date":   str(TODAY),
        "symbol":        symbol,
        "name":          name,
        "strategy":      strategy,
        "family":        "stop",
        "signal_date":   fill_date,
        "signal_price":  entry_p,
        "order_price":   entry_p,
        "stop_price":    stop_p,
        "target_price":  target_p,
        "status":        "holding",
        "fill_date":     fill_date,
        "fill_price":    entry_p,
        "exit_date":     "",
        "exit_price":    "",
        "pnl":           "",
        "updated_date":  str(TODAY),
        "side":          side,
        "qty":           qty,
        "cash_margin":   margin,
    }

    # 重複チェック（同一symbol+fill_date）
    dup = df[(df["symbol"] == symbol) & (df["fill_date"] == fill_date) & (df["status"] == "holding")]
    if not dup.empty:
        print(f"⚠  {symbol} の {fill_date} 約定は既に登録済みです。")
        _show_row(dup.iloc[0])
        return

    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    save(df)

    timeout_date = pd.to_datetime(fill_date).date() + timedelta(days=mh + 3)
    print(f"✅ {symbol}({name}) を登録しました。")
    print(f"   約定値: {entry_p:,.0f}円  損切: {stop_p:,.0f}円  目標: {target_p:,.0f}円")
    print(f"   戦略: {strategy or '未設定'}  MAX_HOLD: {mh}日  株数: {qty}")
    print(f"   タイムカット目安: {timeout_date} ごろ")


# ── 決済 ────────────────────────────────────────────────────────────────────
def cmd_close(args) -> None:
    df = load()
    symbol = str(args.symbol).split(".")[0].strip()
    mask = (df["symbol"] == symbol) & (df["status"] == "holding")
    if mask.sum() == 0:
        print(f"❌ holding 中の {symbol} が見つかりません。")
        return
    if mask.sum() > 1:
        print(f"⚠  {symbol} のポジションが {mask.sum()} 件あります。fill_date で絞り込んでください。")
        print(df[mask][["symbol", "fill_date", "fill_price", "qty"]].to_string(index=False))
        return

    idx = df[mask].index[0]
    exit_p = float(args.exit_price)
    entry_p = float(df.at[idx, "fill_price"])
    qty     = int(df.at[idx, "qty"])
    pnl     = (exit_p - entry_p) * qty

    reason_map = {"target": "目標達成", "stop": "損切り", "timeout": "タイムカット", "manual": "手動決済"}
    reason = reason_map.get(args.reason or "manual", args.reason or "手動決済")

    df.at[idx, "status"]       = args.reason or "manual"
    df.at[idx, "exit_date"]    = str(TODAY)
    df.at[idx, "exit_price"]   = exit_p
    df.at[idx, "pnl"]          = round(pnl)
    df.at[idx, "updated_date"] = str(TODAY)
    save(df)

    sign = "+" if pnl >= 0 else ""
    print(f"✅ {symbol} を決済({reason})しました。")
    print(f"   約定: {entry_p:,.0f}円 → 決済: {exit_p:,.0f}円  損益: {sign}{pnl:,.0f}円")


# ── 一覧 ────────────────────────────────────────────────────────────────────
def _show_row(r) -> None:
    symbol = str(r["symbol"])
    name   = str(r["name"])[:10]
    strat  = "" if str(r["strategy"]) in ("nan", "None", "") else str(r["strategy"])
    fill_d = str(r["fill_date"])
    def _f(v) -> float:
        try:
            r = float(v)
            return 0.0 if r != r else r  # NaN check
        except (ValueError, TypeError):
            return 0.0

    fill_p = _f(r["fill_price"])
    stop_p = _f(r["stop_price"])
    tgt_p  = _f(r["target_price"])
    qty    = int(r["qty"]) if r["qty"] else 100
    mh     = max_hold(strat)

    try:
        fd = datetime.strptime(fill_d, "%Y-%m-%d").date()
    except Exception:
        fd = TODAY
    hold_d = _business_days(fd, TODAY)
    remain = max(0, mh - hold_d)

    # 含み損益
    cur_p = fetch_price(symbol)
    if cur_p:
        unreal = (cur_p - fill_p) * qty
        unreal_pct = (cur_p / fill_p - 1) * 100 if fill_p else 0
        price_str = f"現在値 {cur_p:,.0f}円"
        unreal_str = f"  含み{'損益'} {'+' if unreal>=0 else ''}{unreal:,.0f}円 ({'+' if unreal_pct>=0 else ''}{unreal_pct:.1f}%)"
    else:
        price_str = "現在値 取得失敗"
        unreal_str = ""

    # タイムカットアラート
    if remain == 0:
        remain_str = "🔴 タイムカット超過"
    elif remain <= 2:
        remain_str = f"⚠️  残り {remain}日 (急ぎ決済)"
    else:
        remain_str = f"残り {remain}日"

    # 損切り近接アラート
    alert = ""
    if cur_p and stop_p:
        to_stop = (cur_p / stop_p - 1) * 100
        if to_stop < 2:
            alert = f"  🔴 損切り近接({to_stop:+.1f}%)"
        elif to_stop < 5:
            alert = f"  ⚠️ 損切りまで{to_stop:.1f}%"
    if cur_p and tgt_p:
        to_tgt = (tgt_p / cur_p - 1) * 100 if cur_p else 0
        if to_tgt < 3:
            alert += f"  🎯 目標まで{to_tgt:.1f}%(もうすぐ)"

    margin_str = "信用" if str(r.get("cash_margin", "1")) == "3" else "現物"
    print(f"  [{symbol}] {name}  戦略:{strat or '-'}  {margin_str} {qty}株")
    stop_str = f"{stop_p:,.0f}円" if stop_p else "未設定"
    tgt_str  = f"{tgt_p:,.0f}円" if tgt_p  else "未設定"
    print(f"    約定:{fill_d}  @{fill_p:,.0f}円  損切:{stop_str}  目標:{tgt_str}")
    print(f"    保有:{hold_d}日 / MAX{mh}日  {remain_str}")
    print(f"    {price_str}{unreal_str}{alert}")
    print()


def cmd_list(show_all: bool = False) -> None:
    df = load()

    holding = df[df["status"] == "holding"]
    closed  = df[df["status"].isin(["target", "stop", "timeout", "manual", "expired"])]

    print("=" * 60)
    print(f"📊 ポジション管理  {TODAY}")
    print("=" * 60)

    if holding.empty:
        print("  保有中ポジションなし")
    else:
        print(f"\n🟢 保有中  ({len(holding)}件)\n")
        for _, r in holding.iterrows():
            _show_row(r)

    if show_all and not closed.empty:
        total_pnl = closed["pnl"].apply(lambda x: float(x) if x not in ("", None) and str(x).replace("-","").replace(".","").isdigit() else 0).sum()
        wins  = closed[closed["pnl"].apply(lambda x: float(x) if x not in ("", None) and str(x).replace("-","").replace(".","").isdigit() else 0) > 0]
        print(f"📁 決済済み  ({len(closed)}件)  合計損益: {'+' if total_pnl>=0 else ''}{total_pnl:,.0f}円  勝率: {len(wins)/len(closed)*100:.0f}%\n")
        for _, r in closed.sort_values("exit_date", ascending=False).iterrows():
            pnl_v = float(r["pnl"]) if r["pnl"] not in ("", None) and str(r["pnl"]).replace("-","").replace(".","").isdigit() else 0
            sign = "🟢" if pnl_v > 0 else "🔴"
            print(f"  {sign} [{r['symbol']}]{r['name']}  {r['exit_date']}  {'+' if pnl_v>=0 else ''}{pnl_v:,.0f}円  ({r['status']})")

    print()


# ── 本日シグナルから自動候補表示 ────────────────────────────────────────────
def cmd_today_signals() -> None:
    """本日のシグナルから未登録の保有候補を提案"""
    print("💡 本日シグナルから未登録の保有候補を確認するには:")
    print("   python run_signals.py --signal-only")
    print("   → シグナル銘柄を確認して --add で登録してください")


# ── CLI ─────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="ポジション管理ツール")
    sub = ap.add_subparsers(dest="cmd")

    # list (default)
    sub.add_parser("list", help="保有中ポジション一覧")
    sub.add_parser("all",  help="決済済みも含む全ポジション一覧")

    # add
    p_add = sub.add_parser("add", help="ポジション登録")
    p_add.add_argument("symbol",       help="証券コード (例: 4631)")
    p_add.add_argument("entry_price",  help="約定値")
    p_add.add_argument("stop_price",   help="損切り価格")
    p_add.add_argument("target_price", help="目標価格")
    p_add.add_argument("--strategy",   default="", help="戦略名 (MACD/A7/RSI2/DON/VOL/MOM)")
    p_add.add_argument("--qty",        type=int, default=100, help="株数 (デフォルト100)")
    p_add.add_argument("--margin",     action="store_true", help="信用取引 (デフォルト現物)")
    p_add.add_argument("--fill-date",  default=None, help="約定日 YYYY-MM-DD (デフォルト今日)")

    # close
    p_close = sub.add_parser("close", help="決済登録")
    p_close.add_argument("symbol",      help="証券コード")
    p_close.add_argument("exit_price",  help="決済価格")
    p_close.add_argument("--reason",    default="manual",
                         choices=["target", "stop", "timeout", "manual"],
                         help="決済理由")

    args = ap.parse_args()

    # 引数なし / list → デフォルト一覧
    if args.cmd is None or args.cmd == "list":
        cmd_list(show_all=False)
    elif args.cmd == "all":
        cmd_list(show_all=True)
    elif args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "close":
        cmd_close(args)


if __name__ == "__main__":
    main()
