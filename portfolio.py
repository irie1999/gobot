"""
統合ポートフォリオ管理ツール
────────────────────────────────────────────────────────────────────────
対応戦略: MACD（backtest_macd_scan.py）
          A7  （backtest_stoch_atr_trail.py）
          RSI2（rsi2_hv.py）

使い方:
  python portfolio.py                              # 保有ポジション一覧（現在値・含み損益）
  python portfolio.py --buy 9022.T 40 2500 --strategy RSI2
  python portfolio.py --buy 9022.T 40 2500 --strategy RSI2 --date 2026-03-30
  python portfolio.py --sell 9022.T 20 2650
  python portfolio.py --sell 9022.T 20 2650 --date 2026-03-31
  python portfolio.py --history                    # 決済済み取引履歴
  python portfolio.py --history --strategy RSI2   # 戦略別履歴
  python portfolio.py --summary                    # 戦略別損益サマリー
"""

import io

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yfinance as yf

PORTFOLIO_FILE = Path("portfolio.json")
STRATEGIES     = ("MACD", "A7", "RSI2")


# ── ファイル I/O ────────────────────────────────────────────
def load() -> dict:
    if PORTFOLIO_FILE.exists():
        try:
            with open(PORTFOLIO_FILE, encoding="utf-8") as f:
                data = json.load(f)
            # 旧フォーマット（strategyなし）との互換性
            for p in data.get("positions", []):
                p.setdefault("strategy", "MACD")
            for h in data.get("history", []):
                h.setdefault("strategy", "MACD")
            return data
        except Exception:
            pass
    return {"positions": [], "history": []}


def save(data: dict) -> None:
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── 銘柄名を全戦略リストから引く ───────────────────────────
def _lookup_name(symbol: str) -> str:
    try:
        from symbols_watch    import SYMBOLS as _m
        from symbols_all      import SYMBOLS as _ma
        from symbols_watch_a7 import SYMBOLS as _a7
        from symbols_watch_rsi2 import SYMBOLS as _r2
        for sym, name in list(_m) + list(_ma) + list(_a7) + list(_r2):
            if sym == symbol:
                return name
    except ImportError:
        pass
    return symbol


# ── 現在値を yfinance で一括取得 ────────────────────────────
def fetch_current_prices(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    prices: dict[str, float] = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                hist = tickers.tickers[sym].history(period="2d")
                if not hist.empty:
                    prices[sym] = float(hist["Close"].iloc[-1])
            except Exception:
                pass
    except Exception:
        pass
    return prices


# ── 買い登録 ────────────────────────────────────────────────
def cmd_buy(symbol: str, qty: int, price: float,
            strategy: str, date: str | None) -> None:
    data     = load()
    buy_date = date or datetime.today().strftime("%Y-%m-%d")
    name     = _lookup_name(symbol)
    entry = {
        "symbol":   symbol,
        "name":     name,
        "strategy": strategy,
        "qty":      qty,
        "buy_price": price,
        "buy_date": buy_date,
    }
    data["positions"].append(entry)
    save(data)
    print(f"\n  ✔ 買い登録 [{strategy}]  {name}({symbol})")
    print(f"    {qty}株 × ¥{price:,.0f} = ¥{qty * price:,.0f}  [{buy_date}]")
    print(f"    portfolio.json に保存しました。\n")


# ── 売り登録（FIFO） ────────────────────────────────────────
def cmd_sell(symbol: str, qty: int, price: float, date: str | None) -> None:
    data      = load()
    sell_date = date or datetime.today().strftime("%Y-%m-%d")

    open_pos = [p for p in data["positions"] if p["symbol"] == symbol]
    if not open_pos:
        print(f"\n  ✘ エラー: {symbol} の保有ポジションが見つかりません。\n")
        sys.exit(1)

    total_held = sum(p["qty"] for p in open_pos)
    if qty > total_held:
        print(f"\n  ✘ エラー: 売却数量 {qty}株 が保有数量 {total_held}株 を超えています。\n")
        sys.exit(1)

    remaining      = qty
    total_buy_cost = 0.0
    consumed       = []

    for pos in sorted(open_pos, key=lambda x: x["buy_date"]):
        if remaining <= 0:
            break
        take           = min(remaining, pos["qty"])
        total_buy_cost += take * pos["buy_price"]
        consumed.append((pos, take))
        remaining      -= take

    profit     = qty * price - total_buy_cost
    profit_pct = profit / total_buy_cost * 100 if total_buy_cost else 0.0

    for pos, take in consumed:
        pos["qty"] -= take
        hist_entry = {
            "symbol":     symbol,
            "name":       pos["name"],
            "strategy":   pos.get("strategy", "MACD"),
            "qty":        take,
            "buy_price":  pos["buy_price"],
            "buy_date":   pos["buy_date"],
            "sell_price": price,
            "sell_date":  sell_date,
            "profit":     round(take * (price - pos["buy_price"]), 0),
            "profit_pct": round((price - pos["buy_price"]) / pos["buy_price"] * 100, 2),
        }
        data["history"].append(hist_entry)
        if pos["qty"] == 0:
            data["positions"].remove(pos)

    save(data)
    sign = "+" if profit >= 0 else ""
    print(f"\n  ✔ 売り登録 [{consumed[0][0].get('strategy','MACD')}]  {open_pos[0]['name']}({symbol})")
    print(f"    {qty}株 × ¥{price:,.0f} = ¥{qty * price:,.0f}  [{sell_date}]")
    print(f"    損益: {sign}¥{profit:,.0f}  ({sign}{profit_pct:.2f}%)")
    print(f"    portfolio.json に保存しました。\n")


# ── ポジション一覧 ──────────────────────────────────────────
def cmd_list() -> None:
    data = load()
    positions = data["positions"]

    if not positions:
        print("\n  保有ポジションはありません。\n")
        return

    # 現在値を一括取得
    syms   = list({p["symbol"] for p in positions})
    prices = fetch_current_prices(syms)

    print()
    print("═" * 78)
    print("  保有ポジション一覧")
    print("═" * 78)

    strategy_order = ["MACD", "A7", "RSI2"]
    total_cost = total_value = total_profit = 0.0

    for strat in strategy_order:
        grp = [p for p in positions if p.get("strategy") == strat]
        if not grp:
            continue
        print(f"\n  ▶ {strat} 戦略")
        print(f"  {'銘柄':<22} {'株数':>5} {'買値':>8} {'現在値':>8} "
              f"{'含み損益':>10} {'損益%':>7} {'保有日':>5} {'買付日'}")
        print("  " + "─" * 74)

        for p in sorted(grp, key=lambda x: x["buy_date"]):
            sym       = p["symbol"]
            cur       = prices.get(sym)
            buy_p     = float(p["buy_price"])
            qty_      = int(p["qty"])
            cost      = buy_p * qty_
            hold_days = (datetime.today() - datetime.strptime(p["buy_date"], "%Y-%m-%d")).days
            label     = f"{p['name']}({sym})"

            if cur is not None:
                val      = cur * qty_
                pnl      = val - cost
                pnl_pct  = pnl / cost * 100
                pnl_sign = "+" if pnl >= 0 else ""
                cur_s    = f"{cur:>8,.0f}"
                pnl_s    = f"{pnl_sign}{pnl:>+10,.0f}"
                pct_s    = f"{pnl_sign}{pnl_pct:>6.1f}%"
                total_value  += val
                total_profit += pnl
            else:
                cur_s = "  取得失敗"
                pnl_s = "         —"
                pct_s = "      —"
                val   = cost
                total_value += cost

            total_cost += cost
            print(f"  {label:<22} {qty_:>5} {buy_p:>8,.0f} {cur_s} "
                  f"{pnl_s} {pct_s} {hold_days:>4}日  {p['buy_date']}")

    print()
    print("  " + "─" * 74)
    unrealized_sign = "+" if total_profit >= 0 else ""
    print(f"  {'合計':>22}       買付: ¥{total_cost:>12,.0f}   "
          f"評価: ¥{total_value:>12,.0f}")
    print(f"  {'':>22}       含み損益: {unrealized_sign}¥{total_profit:>+10,.0f}  "
          f"({unrealized_sign}{total_profit / total_cost * 100:.2f}%)" if total_cost else "")

    # 実現損益（過去）も表示
    history = data["history"]
    if history:
        realized = sum(h["profit"] for h in history)
        r_sign   = "+" if realized >= 0 else ""
        print(f"  {'':>22}       実現損益: {r_sign}¥{realized:>+10,.0f}")
        combined  = total_profit + realized
        c_sign    = "+" if combined >= 0 else ""
        print(f"  {'':>22}       総合損益: {c_sign}¥{combined:>+10,.0f}")

    print()


# ── 取引履歴 ────────────────────────────────────────────────
def cmd_history(strategy: str | None) -> None:
    data    = load()
    history = data["history"]

    if strategy:
        history = [h for h in history if h.get("strategy") == strategy]

    if not history:
        label = f"（{strategy}）" if strategy else ""
        print(f"\n  取引履歴{label}はありません。\n")
        return

    print()
    print("═" * 78)
    title = f"取引履歴  {f'[{strategy}]' if strategy else '（全戦略）'}"
    print(f"  {title}")
    print("═" * 78)
    print(f"  {'戦略':5} {'銘柄':<22} {'株数':>5} {'買値':>8} {'売値':>8} "
          f"{'損益':>10} {'損益%':>7} {'買付日':>12} {'売却日':>12}")
    print("  " + "─" * 80)

    total_profit = 0.0
    for h in sorted(history, key=lambda x: x["sell_date"], reverse=True):
        label     = f"{h['name']}({h['symbol']})"
        profit    = float(h["profit"])
        pnl_sign  = "+" if profit >= 0 else ""
        pnl_cls   = "▲" if profit >= 0 else "▽"
        total_profit += profit
        print(f"  {h.get('strategy','MACD'):5} {label:<22} {h['qty']:>5} "
              f"{h['buy_price']:>8,.0f} {h['sell_price']:>8,.0f} "
              f"{pnl_sign}{profit:>+10,.0f} {pnl_sign}{h['profit_pct']:>6.2f}%  "
              f"{h['buy_date']:>12}  {h['sell_date']:>12}  {pnl_cls}")

    print("  " + "─" * 80)
    t_sign = "+" if total_profit >= 0 else ""
    print(f"  実現損益合計: {t_sign}¥{total_profit:,.0f}\n")


# ── 戦略別サマリー ──────────────────────────────────────────
def cmd_summary() -> None:
    data      = load()
    positions = data["positions"]
    history   = data["history"]

    syms   = list({p["symbol"] for p in positions})
    prices = fetch_current_prices(syms)

    print()
    print("═" * 68)
    print("  戦略別損益サマリー")
    print("═" * 68)

    grand_unrealized = grand_realized = grand_trades = 0.0

    for strat in STRATEGIES:
        pos_grp  = [p for p in positions if p.get("strategy") == strat]
        hist_grp = [h for h in history   if h.get("strategy") == strat]

        unrealized = 0.0
        for p in pos_grp:
            cur = prices.get(p["symbol"])
            if cur:
                unrealized += (cur - float(p["buy_price"])) * int(p["qty"])

        realized = sum(float(h["profit"]) for h in hist_grp)
        trades   = len(hist_grp)
        wins     = sum(1 for h in hist_grp if float(h["profit"]) > 0)
        win_rate = wins / trades * 100 if trades else 0.0

        u_sign = "+" if unrealized >= 0 else ""
        r_sign = "+" if realized   >= 0 else ""
        t_sign = "+" if (unrealized + realized) >= 0 else ""

        print(f"\n  ▶ {strat} 戦略")
        print(f"    保有中: {len(pos_grp)}銘柄  "
              f"含み損益: {u_sign}¥{unrealized:,.0f}")
        print(f"    決済済: {trades}回  "
              f"勝率: {win_rate:.0f}%  "
              f"実現損益: {r_sign}¥{realized:,.0f}")
        print(f"    合計:   {t_sign}¥{unrealized + realized:,.0f}")

        grand_unrealized += unrealized
        grand_realized   += realized
        grand_trades     += trades

    print()
    print("  " + "─" * 50)
    gu_s = "+" if grand_unrealized >= 0 else ""
    gr_s = "+" if grand_realized   >= 0 else ""
    gt_s = "+" if (grand_unrealized + grand_realized) >= 0 else ""
    print(f"  【全戦略合計】")
    print(f"    含み損益: {gu_s}¥{grand_unrealized:,.0f}")
    print(f"    実現損益: {gr_s}¥{grand_realized:,.0f}")
    print(f"    総合損益: {gt_s}¥{grand_unrealized + grand_realized:,.0f}")
    print()


# ── --signal 出力用: 保有ポジション一覧（外部から呼び出し用）──
def print_positions_for_signal(strategy: str | None = None) -> None:
    """各戦略の --signal 出力末尾に挿入するポジション表示。
    strategy を指定すると該当戦略のみ表示。None なら全戦略。"""
    data      = load()
    positions = data["positions"]
    if strategy:
        positions = [p for p in positions if p.get("strategy") == strategy]

    if not positions:
        return

    syms   = list({p["symbol"] for p in positions})
    prices = fetch_current_prices(syms)

    title = f"実際の保有ポジション [{strategy}]" if strategy else "実際の保有ポジション（全戦略）"
    print(f"\n  ◆ {title}  ({len(positions)} 銘柄)")
    print(f"  {'戦略':5} {'銘柄':<22} {'株数':>5} {'買値':>8} {'現在値':>8} "
          f"{'含み損益':>10} {'損益%':>7} {'保有日':>5}")
    print("  " + "─" * 72)

    for p in sorted(positions, key=lambda x: (x.get("strategy",""), x["buy_date"])):
        sym       = p["symbol"]
        cur       = prices.get(sym)
        buy_p     = float(p["buy_price"])
        qty_      = int(p["qty"])
        hold_days = (datetime.today() - datetime.strptime(p["buy_date"], "%Y-%m-%d")).days
        label     = f"{p['name']}({sym})"

        if cur is not None:
            pnl      = (cur - buy_p) * qty_
            pnl_pct  = pnl / (buy_p * qty_) * 100
            pnl_sign = "+" if pnl >= 0 else ""
            cur_s    = f"{cur:>8,.0f}"
            pnl_s    = f"{pnl_sign}{pnl:>+10,.0f}"
            pct_s    = f"{pnl_sign}{pnl_pct:>6.1f}%"
        else:
            cur_s = "  データなし"
            pnl_s = "         —"
            pct_s = "      —"

        strat = p.get("strategy", "MACD")
        print(f"  {strat:5} {label:<22} {qty_:>5} {buy_p:>8,.0f} {cur_s} "
              f"{pnl_s} {pct_s} {hold_days:>4}日")
    print()


# ── メイン ──────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="統合ポートフォリオ管理（MACD / A7 / RSI2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用例:
  python portfolio.py                                   # 保有ポジション一覧
  python portfolio.py --buy 9022.T 40 2500 --strategy RSI2
  python portfolio.py --buy 9022.T 40 2500 --strategy RSI2 --date 2026-03-30
  python portfolio.py --sell 9022.T 40 2650
  python portfolio.py --sell 9022.T 40 2650 --date 2026-03-31
  python portfolio.py --history                         # 全戦略の取引履歴
  python portfolio.py --history --strategy RSI2         # RSI2の履歴のみ
  python portfolio.py --summary                         # 戦略別損益サマリー
""")

    parser.add_argument("--buy",      nargs=3, metavar=("SYMBOL", "QTY", "PRICE"),
                        help="買い登録: --buy 9022.T 40 2500")
    parser.add_argument("--sell",     nargs=3, metavar=("SYMBOL", "QTY", "PRICE"),
                        help="売り登録: --sell 9022.T 40 2650")
    parser.add_argument("--strategy", choices=STRATEGIES, default="MACD",
                        help="戦略タグ（--buy 時に使用）: MACD / A7 / RSI2")
    parser.add_argument("--date",     default=None,
                        help="取引日（省略時: 今日）: --date 2026-03-30")
    parser.add_argument("--history",  action="store_true",
                        help="決済済み取引履歴を表示")
    parser.add_argument("--summary",  action="store_true",
                        help="戦略別損益サマリーを表示")
    args = parser.parse_args()

    if args.buy:
        sym, qty_s, price_s = args.buy
        try:
            qty   = int(qty_s)
            price = float(price_s)
        except ValueError:
            print("\n  ✘ エラー: QTY は整数、PRICE は数値で指定してください。\n")
            sys.exit(1)
        sym = sym.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        cmd_buy(sym, qty, price, args.strategy, args.date)

    elif args.sell:
        sym, qty_s, price_s = args.sell
        try:
            qty   = int(qty_s)
            price = float(price_s)
        except ValueError:
            print("\n  ✘ エラー: QTY は整数、PRICE は数値で指定してください。\n")
            sys.exit(1)
        sym = sym.upper()
        if not sym.endswith(".T"):
            sym += ".T"
        cmd_sell(sym, qty, price, args.date)

    elif args.history:
        strat = args.strategy if "--strategy" in sys.argv else None
        cmd_history(strat)

    elif args.summary:
        cmd_summary()

    else:
        cmd_list()


if __name__ == "__main__":
    main()
