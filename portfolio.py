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

import argparse
import io
import json
import sys

# Windows cp932 環境で Unicode 罫線文字を出力できるよう UTF-8 に再設定
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import http.server
import socketserver
import threading
import urllib.parse
import webbrowser
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


# ── 監視銘柄リストを全ファイルから収集 ─────────────────────
def _all_symbols() -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for mod in ("symbols_watch_combined", "symbols_watch_rsi2",
                "symbols_watch_a7", "symbols_watch", "symbols_all"):
        try:
            m = __import__(mod)
            for sym, name in m.SYMBOLS:
                seen.setdefault(sym, name)
        except Exception:
            pass
    return sorted(seen.items())


# ── Web UI ──────────────────────────────────────────────────
_WEB_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ポートフォリオ管理</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e0e0e0;min-height:100vh}
h1{padding:18px 24px;font-size:1.25rem;background:#1a1d27;border-bottom:1px solid #2a2d3a;color:#7eb3ff}
.tabs{display:flex;background:#1a1d27;border-bottom:2px solid #2a2d3a}
.tab{padding:11px 22px;cursor:pointer;font-size:.9rem;color:#888;transition:.2s}
.tab:hover{color:#ccc}
.tab.active{color:#7eb3ff;border-bottom:2px solid #7eb3ff;margin-bottom:-2px}
.panel{display:none;padding:24px}
.panel.active{display:block}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th{background:#1e2233;color:#7eb3ff;padding:9px 10px;text-align:left;font-weight:600}
td{padding:8px 10px;border-bottom:1px solid #1e2233}
tr:hover td{background:#1a1d27}
.pos{color:#4caf7d}.neg{color:#e05c5c}.neutral{color:#888}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:.75rem;font-weight:600}
.badge-macd{background:#1e3a5f;color:#7eb3ff}
.badge-a7{background:#1a3d2b;color:#6ecf8a}
.badge-rsi2{background:#3d2a00;color:#ffa040}
.card{background:#1a1d27;border:1px solid #2a2d3a;border-radius:8px;padding:20px;max-width:480px}
.card h3{margin-bottom:16px;color:#aaa;font-size:.9rem;text-transform:uppercase;letter-spacing:.05em}
.form-row{margin-bottom:14px}
label{display:block;font-size:.8rem;color:#888;margin-bottom:5px}
input,select{width:100%;padding:9px 12px;background:#0f1117;border:1px solid #2a2d3a;
  border-radius:6px;color:#e0e0e0;font-size:.9rem;outline:none}
input:focus,select:focus{border-color:#7eb3ff}
.btn{padding:10px 22px;border:none;border-radius:6px;cursor:pointer;font-size:.9rem;font-weight:600;transition:.2s}
.btn-buy{background:#1a4a2e;color:#6ecf8a}
.btn-buy:hover{background:#225c39}
.btn-sell{background:#4a1a1a;color:#e07070}
.btn-sell:hover{background:#5c2222}
.msg{margin-top:14px;padding:10px 14px;border-radius:6px;font-size:.85rem;display:none}
.msg.ok{background:#1a3d2b;color:#6ecf8a;display:block}
.msg.err{background:#3d1a1a;color:#e07070;display:block}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px;margin-bottom:24px}
.scard{background:#1a1d27;border:1px solid #2a2d3a;border-radius:8px;padding:16px}
.scard .strat{font-size:.75rem;font-weight:700;text-transform:uppercase;margin-bottom:8px}
.scard .val{font-size:1.3rem;font-weight:700}
.scard .sub{font-size:.78rem;color:#666;margin-top:4px}
.refresh{float:right;font-size:.78rem;color:#555;margin-top:4px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #333;
  border-top-color:#7eb3ff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.btn-edit{padding:3px 10px;font-size:.75rem;background:#1e2a3a;color:#7eb3ff;border:1px solid #2a3d5a;
  border-radius:4px;cursor:pointer;transition:.15s}
.btn-edit:hover{background:#2a3d5a}
.btn-del{padding:3px 8px;font-size:.75rem;background:#2a1a1a;color:#e07070;border:1px solid #4a2a2a;
  border-radius:4px;cursor:pointer;margin-left:4px;transition:.15s}
.btn-del:hover{background:#3d2020}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:100;align-items:center;justify-content:center}
.overlay.open{display:flex}
.modal{background:#1a1d27;border:1px solid #2a2d3a;border-radius:10px;padding:28px;width:420px;max-width:95vw}
.modal h3{color:#7eb3ff;margin-bottom:18px;font-size:1rem}
.modal-sym{font-size:.8rem;color:#666;margin-bottom:18px}
.btn-save{background:#1e3a5f;color:#7eb3ff;padding:9px 22px;border:none;border-radius:6px;cursor:pointer;font-weight:600;transition:.2s}
.btn-save:hover{background:#2a4d7a}
.btn-cancel{background:#222;color:#888;padding:9px 16px;border:none;border-radius:6px;cursor:pointer;margin-left:8px}
.btn-cancel:hover{background:#2a2d3a}
</style>
</head>
<body>
<h1>ポートフォリオ管理</h1>
<div class="tabs">
  <div class="tab active" onclick="showTab('positions')">保有ポジション</div>
  <div class="tab" onclick="showTab('buy')">買い登録</div>
  <div class="tab" onclick="showTab('sell')">売り登録</div>
  <div class="tab" onclick="showTab('history')">取引履歴</div>
  <div class="tab" onclick="showTab('summary')">サマリー</div>
</div>

<!-- 保有ポジション -->
<div id="tab-positions" class="panel active">
  <span class="refresh" id="pos-refresh"></span>
  <div id="pos-content"><span class="spinner"></span> 読み込み中...</div>
</div>

<!-- 買い登録 -->
<div id="tab-buy" class="panel">
  <div class="card">
    <h3>買い登録</h3>
    <div class="form-row">
      <label>銘柄コード</label>
      <input id="b-sym" list="sym-list" placeholder="例: 9022.T" autocomplete="off">
      <datalist id="sym-list">__DATALIST__</datalist>
    </div>
    <div class="form-row">
      <label>株数</label>
      <input id="b-qty" type="number" min="1" step="1" placeholder="例: 100">
    </div>
    <div class="form-row">
      <label>買値（円）</label>
      <input id="b-price" type="number" min="0" step="1" placeholder="例: 2500">
    </div>
    <div class="form-row">
      <label>手法</label>
      <select id="b-strat">
        <option>MACD</option><option>A7</option><option>RSI2</option>
      </select>
    </div>
    <div class="form-row">
      <label>日付（省略で今日）</label>
      <input id="b-date" type="date">
    </div>
    <button class="btn btn-buy" onclick="doBuy()">買い登録する</button>
    <div id="b-msg" class="msg"></div>
  </div>
</div>

<!-- 売り登録 -->
<div id="tab-sell" class="panel">
  <div class="card">
    <h3>売り登録</h3>
    <div class="form-row">
      <label>銘柄（保有中から選択）</label>
      <select id="s-sym"><option value="">-- 選択してください --</option></select>
    </div>
    <div class="form-row">
      <label>株数</label>
      <input id="s-qty" type="number" min="1" step="1" placeholder="例: 100">
    </div>
    <div class="form-row">
      <label>売値（円）</label>
      <input id="s-price" type="number" min="0" step="1" placeholder="例: 2700">
    </div>
    <div class="form-row">
      <label>日付（省略で今日）</label>
      <input id="s-date" type="date">
    </div>
    <button class="btn btn-sell" onclick="doSell()">売り登録する</button>
    <div id="s-msg" class="msg"></div>
  </div>
</div>

<!-- 取引履歴 -->
<div id="tab-history" class="panel">
  <div id="hist-content"><span class="spinner"></span> 読み込み中...</div>
</div>

<!-- サマリー -->
<div id="tab-summary" class="panel">
  <div id="sum-content"><span class="spinner"></span> 読み込み中...</div>
</div>

<!-- 編集モーダル -->
<div class="overlay" id="edit-overlay" onclick="closeEdit(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <h3>買い取引を修正</h3>
    <div class="modal-sym" id="edit-sym-label"></div>
    <input type="hidden" id="edit-idx">
    <div class="form-row">
      <label>株数</label>
      <input id="edit-qty" type="number" min="1" step="1">
    </div>
    <div class="form-row">
      <label>買値（円）</label>
      <input id="edit-price" type="number" min="0" step="1">
    </div>
    <div class="form-row">
      <label>手法</label>
      <select id="edit-strat">
        <option>MACD</option><option>A7</option><option>RSI2</option>
      </select>
    </div>
    <div class="form-row">
      <label>買付日</label>
      <input id="edit-date" type="date">
    </div>
    <div id="edit-msg" class="msg"></div>
    <div style="margin-top:18px">
      <button class="btn-save" onclick="doEdit()">保存</button>
      <button class="btn-cancel" onclick="closeEdit()">キャンセル</button>
    </div>
  </div>
</div>

<script>
const STRAT_BADGE = {
  MACD:'<span class="badge badge-macd">MACD</span>',
  A7:  '<span class="badge badge-a7">A7</span>',
  RSI2:'<span class="badge badge-rsi2">RSI2</span>',
};
function badge(s){return STRAT_BADGE[s]||s}
function pnlClass(v){return v>0?'pos':v<0?'neg':'neutral'}
function fmt(v){return v==null?'—':v.toLocaleString('ja-JP',{maximumFractionDigits:0})}
function fmtPct(v){return v==null?'—':(v>=0?'+':'')+v.toFixed(2)+'%'}

function showTab(name){
  document.querySelectorAll('.tab').forEach((t,i)=>{
    t.classList.toggle('active', ['positions','buy','sell','history','summary'][i]===name)
  });
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  if(name==='positions') loadPositions();
  if(name==='history')   loadHistory();
  if(name==='summary')   loadSummary();
  if(name==='sell')      loadSellSyms();
}

async function api(path, body){
  const opt = body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return r.json();
}

// ── ポジション ──
async function loadPositions(){
  document.getElementById('pos-refresh').textContent='';
  document.getElementById('pos-content').innerHTML='<span class="spinner"></span> 読み込み中...';
  const d = await api('/api/data');
  const pos = d.positions||[];
  const prices = d.prices||{};
  if(!pos.length){
    document.getElementById('pos-content').innerHTML='<p style="color:#666;padding:20px">保有ポジションはありません。</p>';
    return;
  }
  const strats = ['MACD','A7','RSI2'];
  let html='';
  let tc=0,tv=0,tp=0;
  for(const st of strats){
    const grp=pos.filter(p=>p.strategy===st);
    if(!grp.length) continue;
    html+=`<h3 style="margin:20px 0 8px;font-size:.85rem;color:#aaa">${badge(st)} 戦略</h3>`;
    html+=`<table><thead><tr><th>銘柄</th><th>株数</th><th>買値</th><th>現在値</th><th>含み損益</th><th>損益%</th><th>保有日</th><th>買付日</th><th></th></tr></thead><tbody>`;
    for(const p of grp.sort((a,b)=>a.buy_date>b.buy_date?1:-1)){
      const cur=prices[p.symbol];
      const cost=p.buy_price*p.qty;
      const val=cur!=null?cur*p.qty:cost;
      const pnl=cur!=null?val-cost:null;
      const pct=pnl!=null?pnl/cost*100:null;
      const days=Math.floor((Date.now()-new Date(p.buy_date))/(86400000));
      tc+=cost; tv+=val; if(pnl!=null) tp+=pnl;
      const pc=pnlClass(pnl);
      const idx=p._idx;
      const pd=JSON.stringify(p).replace(/"/g,'&quot;');
      html+=`<tr>
        <td><b>${p.name}</b><br><small style="color:#555">${p.symbol}</small></td>
        <td>${p.qty}</td>
        <td>¥${fmt(p.buy_price)}</td>
        <td>${cur!=null?'¥'+fmt(cur):'<span class="neutral">—</span>'}</td>
        <td class="${pc}">${pnl!=null?(pnl>=0?'+':'')+' ¥'+fmt(Math.abs(pnl)):'—'}</td>
        <td class="${pc}">${fmtPct(pct)}</td>
        <td>${days}日</td>
        <td>${p.buy_date}</td>
        <td style="white-space:nowrap">
          <button class="btn-edit" onclick='openEdit(${idx},${JSON.stringify(p)})'>編集</button>
          <button class="btn-del" onclick="delPos(${idx},'${p.symbol}')">削除</button>
        </td>
      </tr>`;
    }
    html+='</tbody></table>';
  }
  const tpc=pnlClass(tp);
  html+=`<div style="margin-top:16px;padding:12px 16px;background:#1e2233;border-radius:6px;font-size:.85rem">
    買付合計: ¥${fmt(tc)} ／ 評価合計: ¥${fmt(tv)} ／
    含み損益: <span class="${tpc}">${tp>=0?'+':''} ¥${fmt(Math.abs(tp))}</span>
  </div>`;
  if(d.realized!=null){
    const r=d.realized, tot=tp+r;
    html+=`<div style="margin-top:8px;padding:10px 16px;background:#1a1d27;border-radius:6px;font-size:.82rem;color:#888">
      実現損益: <span class="${pnlClass(r)}">${r>=0?'+':''} ¥${fmt(Math.abs(r))}</span>
      ／ 総合損益: <span class="${pnlClass(tot)}">${tot>=0?'+':''} ¥${fmt(Math.abs(tot))}</span>
    </div>`;
  }
  document.getElementById('pos-content').innerHTML=html;
  document.getElementById('pos-refresh').textContent='更新: '+new Date().toLocaleTimeString('ja-JP');
}

// ── 買い ──
async function doBuy(){
  const sym=(document.getElementById('b-sym').value||'').trim().toUpperCase();
  const qty=parseInt(document.getElementById('b-qty').value);
  const price=parseFloat(document.getElementById('b-price').value);
  const strat=document.getElementById('b-strat').value;
  const date=document.getElementById('b-date').value||null;
  const msg=document.getElementById('b-msg');
  if(!sym||isNaN(qty)||isNaN(price)){msg.className='msg err';msg.textContent='銘柄・株数・価格を入力してください';return}
  const r=await api('/api/buy',{symbol:sym,qty,price,strategy:strat,date});
  if(r.ok){msg.className='msg ok';msg.textContent=r.message;}
  else{msg.className='msg err';msg.textContent=r.error;}
}

// ── 売り ──
async function loadSellSyms(){
  const d=await api('/api/data');
  const pos=d.positions||[];
  const sel=document.getElementById('s-sym');
  const syms=[...new Set(pos.map(p=>p.symbol))];
  sel.innerHTML='<option value="">-- 選択してください --</option>'+
    syms.map(s=>{const p=pos.find(x=>x.symbol===s);return`<option value="${s}">${p.name} (${s}) 保有:${pos.filter(x=>x.symbol===s).reduce((a,b)=>a+b.qty,0)}株</option>`}).join('');
}

async function doSell(){
  const sym=document.getElementById('s-sym').value;
  const qty=parseInt(document.getElementById('s-qty').value);
  const price=parseFloat(document.getElementById('s-price').value);
  const date=document.getElementById('s-date').value||null;
  const msg=document.getElementById('s-msg');
  if(!sym||isNaN(qty)||isNaN(price)){msg.className='msg err';msg.textContent='銘柄・株数・価格を入力してください';return}
  const r=await api('/api/sell',{symbol:sym,qty,price,date});
  if(r.ok){msg.className='msg ok';msg.textContent=r.message;loadSellSyms();}
  else{msg.className='msg err';msg.textContent=r.error;}
}

// ── 履歴 ──
async function loadHistory(){
  document.getElementById('hist-content').innerHTML='<span class="spinner"></span>';
  const d=await api('/api/data');
  const hist=(d.history||[]).slice().sort((a,b)=>b.sell_date>a.sell_date?1:-1);
  if(!hist.length){document.getElementById('hist-content').innerHTML='<p style="color:#666;padding:20px">取引履歴はありません。</p>';return}
  let html=`<table><thead><tr><th>手法</th><th>銘柄</th><th>株数</th><th>買値</th><th>売値</th><th>損益</th><th>損益%</th><th>買付日</th><th>売却日</th></tr></thead><tbody>`;
  let total=0;
  for(const h of hist){
    const pc=pnlClass(h.profit);
    total+=h.profit;
    html+=`<tr>
      <td>${badge(h.strategy||'MACD')}</td>
      <td><b>${h.name}</b><br><small style="color:#555">${h.symbol}</small></td>
      <td>${h.qty}</td>
      <td>¥${fmt(h.buy_price)}</td>
      <td>¥${fmt(h.sell_price)}</td>
      <td class="${pc}">${h.profit>=0?'+':''} ¥${fmt(Math.abs(h.profit))}</td>
      <td class="${pc}">${h.profit_pct>=0?'+':''}${h.profit_pct.toFixed(2)}%</td>
      <td>${h.buy_date}</td>
      <td>${h.sell_date}</td>
    </tr>`;
  }
  html+='</tbody></table>';
  const tc=pnlClass(total);
  html+=`<div style="margin-top:12px;padding:10px 16px;background:#1e2233;border-radius:6px;font-size:.85rem">
    実現損益合計: <span class="${tc}">${total>=0?'+':''} ¥${fmt(Math.abs(total))}</span>
  </div>`;
  document.getElementById('hist-content').innerHTML=html;
}

// ── サマリー ──
async function loadSummary(){
  document.getElementById('sum-content').innerHTML='<span class="spinner"></span>';
  const d=await api('/api/summary');
  const st=d.strategies||{};
  let html='<div class="summary-grid">';
  for(const [name,s] of Object.entries(st)){
    const tot=s.unrealized+s.realized;
    const pc=pnlClass(tot);
    html+=`<div class="scard">
      <div class="strat">${badge(name)}</div>
      <div class="val ${pc}">${tot>=0?'+':''} ¥${fmt(Math.abs(tot))}</div>
      <div class="sub">含み: <span class="${pnlClass(s.unrealized)}">${s.unrealized>=0?'+':''}¥${fmt(Math.abs(s.unrealized))}</span>
        ／ 実現: <span class="${pnlClass(s.realized)}">${s.realized>=0?'+':''}¥${fmt(Math.abs(s.realized))}</span>
      </div>
      <div class="sub">決済${s.trades}回 勝率${s.win_rate.toFixed(0)}%</div>
    </div>`;
  }
  html+='</div>';
  const g=d.grand||{};
  const gtot=(g.unrealized||0)+(g.realized||0);
  html+=`<div style="padding:16px;background:#1e2233;border-radius:8px;font-size:.9rem">
    <b style="color:#aaa">全戦略合計</b><br><br>
    含み損益: <span class="${pnlClass(g.unrealized)}">${(g.unrealized||0)>=0?'+':''}¥${fmt(Math.abs(g.unrealized||0))}</span><br>
    実現損益: <span class="${pnlClass(g.realized)}">${(g.realized||0)>=0?'+':''}¥${fmt(Math.abs(g.realized||0))}</span><br>
    <b>総合損益: <span class="${pnlClass(gtot)}">${gtot>=0?'+':''}¥${fmt(Math.abs(gtot))}</span></b>
  </div>`;
  document.getElementById('sum-content').innerHTML=html;
}

// ── 編集モーダル ──
function openEdit(idx, p){
  document.getElementById('edit-idx').value=idx;
  document.getElementById('edit-sym-label').textContent=`${p.name}（${p.symbol}）`;
  document.getElementById('edit-qty').value=p.qty;
  document.getElementById('edit-price').value=p.buy_price;
  document.getElementById('edit-strat').value=p.strategy||'MACD';
  document.getElementById('edit-date').value=p.buy_date;
  document.getElementById('edit-msg').className='msg';
  document.getElementById('edit-overlay').classList.add('open');
}
function closeEdit(e){
  if(e && e.target!==document.getElementById('edit-overlay')) return;
  document.getElementById('edit-overlay').classList.remove('open');
}

async function doEdit(){
  const idx=parseInt(document.getElementById('edit-idx').value);
  const qty=parseInt(document.getElementById('edit-qty').value);
  const price=parseFloat(document.getElementById('edit-price').value);
  const strat=document.getElementById('edit-strat').value;
  const date=document.getElementById('edit-date').value;
  const msg=document.getElementById('edit-msg');
  if(isNaN(qty)||isNaN(price)||!date){msg.className='msg err';msg.textContent='入力値を確認してください';return}
  const r=await api('/api/edit-position',{idx,qty,price,strategy:strat,buy_date:date});
  if(r.ok){
    msg.className='msg ok';msg.textContent='✔ 保存しました';
    setTimeout(()=>{document.getElementById('edit-overlay').classList.remove('open');loadPositions();},700);
  } else {
    msg.className='msg err';msg.textContent=r.error;
  }
}

async function delPos(idx, sym){
  if(!confirm(`${sym} のこのポジションを削除しますか？`)) return;
  const r=await api('/api/delete-position',{idx});
  if(r.ok) loadPositions();
  else alert(r.error);
}

// 起動時
loadPositions();
</script>
</body>
</html>
"""


def _build_html() -> str:
    syms = _all_symbols()
    opts = "".join(f'<option value="{s}">{s} {n}</option>' for s, n in syms)
    return _WEB_HTML.replace("__DATALIST__", opts)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress access log
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/" or path == "/index.html":
            body = _build_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/data":
            data = load()
            syms = list({p["symbol"] for p in data["positions"]})
            prices = fetch_current_prices(syms)
            realized = sum(float(h["profit"]) for h in data.get("history", []))
            positions_with_idx = [dict(p, _idx=i) for i, p in enumerate(data["positions"])]
            self._send_json({
                "positions": positions_with_idx,
                "history":   data["history"],
                "prices":    {k: float(v) for k, v in prices.items()},
                "realized":  realized,
            })

        elif path == "/api/summary":
            data  = load()
            syms  = list({p["symbol"] for p in data["positions"]})
            prices = fetch_current_prices(syms)
            result = {}
            gu = gr = 0.0
            for strat in STRATEGIES:
                pos_g  = [p for p in data["positions"] if p.get("strategy") == strat]
                hist_g = [h for h in data["history"]   if h.get("strategy") == strat]
                unreal = sum((prices.get(p["symbol"], float(p["buy_price"])) - float(p["buy_price"])) * int(p["qty"])
                             for p in pos_g)
                real   = sum(float(h["profit"]) for h in hist_g)
                trades = len(hist_g)
                wins   = sum(1 for h in hist_g if float(h["profit"]) > 0)
                result[strat] = {
                    "unrealized": round(unreal, 0),
                    "realized":   round(real, 0),
                    "trades":     trades,
                    "win_rate":   wins / trades * 100 if trades else 0.0,
                }
                gu += unreal; gr += real
            self._send_json({"strategies": result,
                             "grand": {"unrealized": round(gu, 0), "realized": round(gr, 0)}})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length) or b"{}")
        path   = urllib.parse.urlparse(self.path).path

        if path == "/api/buy":
            try:
                sym   = str(body["symbol"]).upper()
                if not sym.endswith(".T"):
                    sym += ".T"
                qty   = int(body["qty"])
                price = float(body["price"])
                strat = str(body.get("strategy", "MACD"))
                date  = body.get("date") or None
                if strat not in STRATEGIES:
                    raise ValueError(f"unknown strategy {strat}")
                cmd_buy(sym, qty, price, strat, date)
                self._send_json({"ok": True,
                    "message": f"✔ 買い登録: {sym} {qty}株 ×¥{price:,.0f} [{strat}]"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})

        elif path == "/api/sell":
            try:
                sym   = str(body["symbol"]).upper()
                qty   = int(body["qty"])
                price = float(body["price"])
                date  = body.get("date") or None
                cmd_sell(sym, qty, price, date)
                self._send_json({"ok": True,
                    "message": f"✔ 売り登録: {sym} {qty}株 ×¥{price:,.0f}"})
            except SystemExit:
                self._send_json({"ok": False, "error": "保有数量を超えているか、ポジションがありません"})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})

        elif path == "/api/edit-position":
            try:
                idx   = int(body["idx"])
                data  = load()
                pos   = data["positions"]
                if idx < 0 or idx >= len(pos):
                    raise IndexError("ポジションが見つかりません")
                pos[idx]["qty"]       = int(body["qty"])
                pos[idx]["buy_price"] = float(body["price"])
                pos[idx]["strategy"]  = str(body.get("strategy", pos[idx].get("strategy", "MACD")))
                pos[idx]["buy_date"]  = str(body["buy_date"])
                save(data)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})

        elif path == "/api/delete-position":
            try:
                idx  = int(body["idx"])
                data = load()
                pos  = data["positions"]
                if idx < 0 or idx >= len(pos):
                    raise IndexError("ポジションが見つかりません")
                pos.pop(idx)
                save(data)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})

        else:
            self.send_response(404); self.end_headers()


class _ReuseServer(socketserver.TCPServer):
    allow_reuse_address = True  # bind前に設定が必要（Windows WinError 10048 対策）


def cmd_web(port: int = 7654) -> None:
    url = f"http://localhost:{port}"
    # ポートが使用中なら次のポートを試す
    for p in range(port, port + 10):
        try:
            server = _ReuseServer(("", p), _Handler)
            break
        except OSError:
            continue
    else:
        print(f"\n  ✘ ポート {port}〜{port+9} がすべて使用中です。他のアプリを終了してから再実行してください。\n")
        return
    actual_url = f"http://localhost:{p}"
    if p != port:
        print(f"\n  ポート {port} は使用中のため {p} で起動します。")
    print(f"\n  ポートフォリオ Web UI を起動しました → {actual_url}")
    print(f"  終了するには Ctrl+C を押してください。\n")
    with server:
        threading.Timer(0.5, lambda: webbrowser.open(actual_url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Web UI を終了しました。")


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
    parser.add_argument("--web",      action="store_true",
                        help="ブラウザでポートフォリオ管理画面を開く (localhost:7654)")
    parser.add_argument("--port",     type=int, default=7654,
                        help="--web 時のポート番号（デフォルト: 7654）")
    args = parser.parse_args()

    if args.web:
        cmd_web(args.port)
        return

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
