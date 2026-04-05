"""
build_watchlist_budget.py  ―  予算内購入可能な銘柄から強い押し目上位を選定
==========================================================================
全銘柄から現在株価が予算/100以下の銘柄だけを対象に、
強い押し目バックテスト（4期間スコア）で成績上位銘柄を抽出する。

【使い方】
  python build_watchlist_budget.py                      # デフォルト50万円
  python build_watchlist_budget.py --budget 300000      # 30万円
  python build_watchlist_budget.py --budget 1000000     # 100万円
  python build_watchlist_budget.py --top 20 --min-pf 1.5
  python build_watchlist_budget.py --workers 8
  python build_watchlist_budget.py --no-browser

事前準備（初回のみ）:
  python download_tse_symbols.py   # 東証全銘柄リスト生成

※ 1000銘柄規模でも株価フィルタ後は数百銘柄に絞られ、数分〜10分程度で完了。
"""

from __future__ import annotations

import argparse
import math
import pickle
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

import backtest_strong_pullback as _sp

JST              = timezone(timedelta(hours=9))
LOT_SIZE         = 100
WATCHLIST_PERIODS = [30, 90, 180, 365]
PERIOD_WEIGHTS    = {30: 4.0, 90: 3.0, 180: 2.0, 365: 1.0}
_CACHE_DIR        = Path(".rsi2_cache")


# ── 銘柄ロード ─────────────────────────────────────────────────

def _load_symbols() -> list[tuple[str, str]]:
    p = Path("symbols_listed_all.py")
    if p.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("_listed_all", p)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        syms = list(mod.SYMBOLS)
        print(f"  銘柄ユニバース: symbols_listed_all.py ({len(syms)}銘柄)")
        return syms
    from symbols_all import SYMBOLS as _S
    syms = list(_S)
    print(f"  銘柄ユニバース: symbols_all.py ({len(syms)}銘柄) ※ symbols_listed_all.pyが見つかりません")
    return syms


# ── 株価一括取得 ───────────────────────────────────────────────

def _fetch_prices_batch(symbols: list[str], batch_size: int = 200) -> dict[str, float]:
    """yfinance バッチダウンロードで現在株価を取得。"""
    prices: dict[str, float] = {}
    total  = len(symbols)
    done   = 0
    for i in range(0, total, batch_size):
        batch = symbols[i : i + batch_size]
        try:
            raw = yf.download(
                " ".join(batch), period="5d", interval="1d",
                auto_adjust=False, progress=False, threads=True,
            )
            if raw.empty:
                continue
            close = raw.get("Close", raw.get("close", None))
            if close is None:
                continue
            # マルチカラム (銘柄×日付) の場合
            if isinstance(close.columns if hasattr(close, "columns") else None, pd.MultiIndex):
                close = close  # already DataFrame
            elif isinstance(close, pd.Series):
                # 単一銘柄
                v = close.dropna()
                if not v.empty:
                    prices[batch[0]] = float(v.iloc[-1])
                done += len(batch)
                print(f"  株価取得: {min(done, total)}/{total}", end="\r")
                continue

            for sym in batch:
                try:
                    col = close[sym] if sym in close.columns else None
                    if col is None:
                        continue
                    v = col.dropna()
                    if not v.empty:
                        prices[sym] = float(v.iloc[-1])
                except Exception:
                    pass
        except Exception as e:
            print(f"\n  ※ バッチ取得エラー ({batch[0]}〜): {e}")
        done += len(batch)
        print(f"  株価取得: {min(done, total)}/{total}", end="\r")
    print()
    return prices


# ── スコアリング ───────────────────────────────────────────────

def _relative_rank(vals: dict[str, float], higher_is_better: bool) -> dict[str, int]:
    syms = [s for s, v in vals.items() if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not syms:
        return {s: 0 for s in vals}
    sorted_syms = sorted(syms, key=lambda s: vals[s], reverse=higher_is_better)
    n = len(sorted_syms)
    ranks: dict[str, int] = {s: 0 for s in vals}
    for i, s in enumerate(sorted_syms):
        ranks[s] = max(1, 5 - int(i * 5 / n))
    return ranks


def _score_stocks(
    results: list[dict],
    min_trades: int,
    min_pf: float,
    min_pf_90: float,
    min_total: int,
) -> list[dict]:
    """4期間スコアで銘柄をランキングして返す。"""
    all_syms = [r["symbol"] for r in results]
    result_map = {r["symbol"]: r for r in results}

    period_scores: dict[str, float] = {s: 0.0 for s in all_syms}

    for p in WATCHLIST_PERIODS:
        w = PERIOD_WEIGHTS[p]
        pf_vals = {}
        wr_vals = {}
        avg_vals = {}

        for sym in all_syms:
            ps = result_map[sym]["period_results"].get(p, {})
            n  = ps.get("n", 0)
            if n >= min_trades:
                pf = ps.get("pf", 0)
                if pf == float("inf"):
                    pf_vals[sym] = 10.0
                elif not math.isnan(pf) and pf > 0:
                    pf_vals[sym] = min(pf, 10.0)
                trades_p = result_map[sym]["period_trades"].get(p, [])
                wins_p   = [t for t in trades_p if t["pnl"] > 0]
                wr_vals[sym]  = len(wins_p) / n * 100 if n > 0 else 0
                avg_vals[sym] = sum(t["pnl"] for t in trades_p) / n if n > 0 else 0

        r_pf  = _relative_rank(pf_vals,  higher_is_better=True)
        r_wr  = _relative_rank(wr_vals,  higher_is_better=True)
        r_avg = _relative_rank(avg_vals, higher_is_better=True)

        for sym in all_syms:
            period_scores[sym] += (r_pf.get(sym, 0) + r_wr.get(sym, 0) + r_avg.get(sym, 0)) * w

    # フィルタ + ソート
    scored = []
    for sym in all_syms:
        r    = result_map[sym]
        s365 = r["period_results"].get(365, {})
        n365 = s365.get("n", 0)
        if n365 < min_trades:
            continue
        pf365 = s365.get("pf", 0)
        if math.isnan(pf365) if isinstance(pf365, float) else False:
            pf365 = 0
        pf365_v = 99.0 if pf365 == float("inf") else pf365
        if pf365_v < min_pf:
            continue
        if s365.get("total", 0) < min_total:
            continue
        # 直近90日 PF フィルタ
        s90  = r["period_results"].get(90, {})
        n90  = s90.get("n", 0)
        if n90 > 0:
            pf90 = s90.get("pf", 0)
            pf90_v = 99.0 if pf90 == float("inf") else (0 if math.isnan(pf90) else pf90)
            if pf90_v < min_pf_90:
                continue

        scored.append(dict(
            symbol        = sym,
            name          = r["name"],
            price         = r["last_close"],
            score         = period_scores[sym],
            period_results= r["period_results"],
            period_trades = r["period_trades"],
            n365          = n365,
            wr365         = s365.get("wr", 0),
            pf365         = pf365,
            total365      = s365.get("total", 0),
        ))

    scored.sort(key=lambda x: -x["score"])
    return scored


# ── HTML生成 ───────────────────────────────────────────────────

_CSS = """
body{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;
     margin:0;padding:20px;font-size:14px}
h1{font-size:1.4em;border-bottom:1px solid #21262d;padding-bottom:10px;margin-bottom:16px}
h2{font-size:1.1em;color:#58a6ff;margin:24px 0 10px}
table{border-collapse:collapse;width:100%;margin-bottom:20px}
th{background:#161b22;color:#8b949e;font-weight:600;padding:8px 10px;
   text-align:left;border-bottom:2px solid #21262d;font-size:.85em;
   position:sticky;top:0;z-index:1;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #21262d;vertical-align:middle;white-space:nowrap}
tr:hover td{background:#161b22}
.num{text-align:right}
.up{color:#3fb950}.dn{color:#f85149}.neu{color:#8b949e}
.sub{color:#8b949e;font-size:.78em;margin-top:1px}
details>summary{cursor:pointer;padding:10px 14px;background:#161b22;
                border-radius:6px;margin-bottom:4px;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{background:#1f2937}
a{color:#38bdf8;text-decoration:none}
a:hover{text-decoration:underline}
.badge{border:1px solid currentColor;border-radius:4px;padding:1px 8px;font-size:.85em}
"""


def _pf_td(ps: dict) -> str:
    n  = ps.get("n", 0)
    if n == 0:
        return "<td class='neu' style='text-align:right'>—</td>"
    pf = ps.get("pf", 0)
    s  = "∞" if pf == float("inf") else f"{pf:.2f}"
    cl = "up" if (pf == float("inf") or pf >= 1.0) else "dn"
    return f"<td class='{cl} num'>{s}<div class='sub'>{n}件</div></td>"


def _trade_rows(trades: list[dict]) -> str:
    if not trades:
        return "<tr><td colspan='9' style='color:#4b5563;text-align:center'>トレードなし</td></tr>"
    rows = ""
    for t in sorted(trades, key=lambda x: x.get("exit_dt") or ""):
        pnl   = t["pnl"]
        cls   = "up" if pnl > 0 else "dn"
        entry_p = t.get("entry_p", 0) or t.get("limit_p", 0)
        stop_p  = t.get("stop_p",  0)
        target  = t.get("target_p",0)
        rows += (
            f"<tr>"
            f"<td>{str(t.get('signal_dt',''))[:10]}</td>"
            f"<td>{str(t.get('entry_dt', ''))[:10]}</td>"
            f"<td>{str(t.get('exit_dt',  ''))[:10]}</td>"
            f"<td class='up num'>¥{entry_p:,.0f}</td>"
            f"<td class='dn num'>¥{stop_p:,.0f}</td>"
            f"<td class='up num'>¥{target:,.0f}</td>"
            f"<td class='num'>¥{t.get('exit_p',0):,.0f}</td>"
            f"<td class='{cls} num'>{pnl:+,.0f}円</td>"
            f"<td class='num'>{t.get('hold',0)}日</td>"
            f"<td>{t.get('reason','')}</td>"
            f"</tr>"
        )
    return rows


def build_html(scored: list[dict], budget: int, n_universe: int, n_affordable: int) -> str:
    now_str    = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    max_price  = budget // LOT_SIZE

    # ランキングテーブル
    rank_rows = ""
    for i, r in enumerate(scored, 1):
        w_color = "#f0c000" if i <= 3 else ("#94a3b8" if i <= 10 else "#e6edf3")
        pf_s    = "∞" if r["pf365"] == float("inf") else f"{r['pf365']:.2f}"
        tc      = "up" if r["total365"] >= 0 else "dn"
        sym     = r["symbol"]
        rank_rows += (
            f"<tr>"
            f"<td class='num' style='font-weight:700;color:{w_color}'>{i}</td>"
            f"<td><a href='https://finance.yahoo.co.jp/quote/{sym}' target='_blank'>{sym}</a></td>"
            f"<td>{r['name']}</td>"
            f"<td class='num'>¥{r['price']:,.0f}</td>"
            f"<td class='num up' style='font-weight:600'>{r['score']:.1f}</td>"
            + "".join(_pf_td(r["period_results"].get(p, {})) for p in WATCHLIST_PERIODS)
            + f"<td class='num'>{r['wr365']:.1f}%</td>"
            f"<td class='{tc} num'>{r['total365']:+,.0f}円</td>"
            f"</tr>"
        )

    # 銘柄詳細アコーディオン
    details_html = ""
    for r in scored:
        s365 = r["period_results"].get(365, {})
        n    = s365.get("n", 0)
        if n == 0:
            continue
        pf_s = "∞" if r["pf365"] == float("inf") else f"{r['pf365']:.2f}"
        tc   = "up" if r["total365"] >= 0 else "dn"
        t_rows = _trade_rows(r["period_trades"].get(365, []))
        details_html += f"""
<details>
  <summary>
    <b>{r['symbol']}</b> {r['name']} &nbsp;
    ¥{r['price']:,.0f} (100株: ¥{r['price']*LOT_SIZE:,.0f}) &nbsp;|&nbsp;
    {n}件 / 勝率{r['wr365']:.0f}% / PF{pf_s} /
    <span class='{tc}'>{r['total365']:+,.0f}円</span> (365日)
  </summary>
  <table style="margin-top:6px;font-size:.88em">
    <thead><tr>
      <th>シグナル日</th><th>約定日</th><th>決済日</th>
      <th>指値</th><th>損切</th><th>利確目標</th>
      <th>決済価格</th><th>損益</th><th>保有</th><th>理由</th>
    </tr></thead>
    <tbody>{t_rows}</tbody>
  </table>
</details>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>予算別監視リスト ― 強い押し目</title>
<style>{_CSS}</style>
</head>
<body>
<h1>予算別監視リスト ― 強い押し目（strong_pullback）
  <span style="font-size:.75em;color:#8b949e">
    予算: {budget:,}円 (株価≤¥{max_price:,}) ／ ユニバース: {n_universe}銘柄 ／ 予算内: {n_affordable}銘柄 ／ 選定: {len(scored)}銘柄 ／ {now_str}
  </span>
</h1>
<p style="font-size:.82em;color:#4b5563">
  スコア: 30日×4 / 90日×3 / 180日×2 / 365日×1 × 3指標（PF・勝率・平均損益）相対ランク<br>
  フィルタ: 表示はスコア上位（コマンドライン引数で調整可）
</p>

<h2>▶ スコアランキング（{len(scored)}銘柄）</h2>
<table>
<thead><tr>
  <th>#</th><th>コード</th><th>銘柄名</th><th>現在値</th>
  <th>スコア</th>
  {"".join(f"<th>{p}日 PF</th>" for p in WATCHLIST_PERIODS)}
  <th>勝率(365)</th><th>合計(365)</th>
</tr></thead>
<tbody>{rank_rows}</tbody>
</table>

<h2>▶ 銘柄別トレード詳細（クリックで展開）</h2>
{details_html}
</body>
</html>"""


# ── メイン ─────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="予算内銘柄を全ユニバースから強い押し目4期間スコアで選定")
    parser.add_argument("--budget",     type=int,   default=500_000, help="予算（円）デフォルト500000")
    parser.add_argument("--top",        type=int,   default=30,      help="表示上位銘柄数（デフォルト30）")
    parser.add_argument("--min-pf",     type=float, default=1.3,     help="最小PF（365日）デフォルト1.3")
    parser.add_argument("--min-pf-90",  type=float, default=1.0,     help="最小PF（90日）デフォルト1.0")
    parser.add_argument("--min-total",  type=int,   default=10_000,  help="最小合計損益（365日）デフォルト10000円")
    parser.add_argument("--min-trades", type=int,   default=3,       help="最小取引数（365日）デフォルト3件")
    parser.add_argument("--workers",    type=int,   default=6,       help="並列数（デフォルト6）")
    parser.add_argument("--no-browser", action="store_true",         help="ブラウザを開かない")
    args = parser.parse_args()

    budget    = args.budget
    max_price = budget // LOT_SIZE

    print(f"\n{'='*65}")
    print(f"  予算別監視リスト生成 ― 強い押し目（4期間スコア選定）")
    print(f"  予算: {budget:,}円  /  株価上限: ¥{max_price:,}  /  上位{args.top}銘柄")
    print(f"{'='*65}\n")

    # 1. 銘柄ロード
    all_stocks = _load_symbols()
    n_universe = len(all_stocks)

    # 2. 株価一括取得
    print(f"  株価取得中（{n_universe}銘柄）...")
    symbols_only = [sym for sym, _ in all_stocks]
    prices = _fetch_prices_batch(symbols_only)
    print(f"  株価取得完了: {len(prices)}銘柄")

    # 3. 予算フィルタ
    affordable = [(sym, nm) for sym, nm in all_stocks
                  if prices.get(sym, float("inf")) <= max_price and prices.get(sym, 0) > 0]
    n_affordable = len(affordable)
    print(f"  予算フィルタ後: {n_affordable}銘柄 (¥{max_price:,}以下)")

    if n_affordable == 0:
        print("  ※ 購入可能な銘柄が見つかりませんでした。")
        return

    # 4. 4期間バックテスト（並列）
    print(f"\n  バックテスト実行中（{n_affordable}銘柄 × 4期間）...")
    bt_cache = _sp._load_bt_cache()
    raw_results: list[dict] = []
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(_sp._process_symbol_multiperiod, sym, nm, WATCHLIST_PERIODS, bt_cache): (sym, nm)
            for sym, nm in affordable
        }
        for f in as_completed(futs):
            result = f.result()
            done += 1
            if done % max(n_affordable // 10, 5) == 0 or done == n_affordable:
                print(f"  進捗: {done}/{n_affordable} ({done/n_affordable*100:.0f}%)", end="\r")
            if result is not None:
                # 株価をpricesから補完（last_closeが0の場合）
                sym = result["symbol"]
                if result.get("last_close", 0) == 0 and sym in prices:
                    result["last_close"] = prices[sym]
                raw_results.append(result)

    _sp._save_bt_cache(bt_cache)
    print(f"\n  バックテスト完了: {len(raw_results)}銘柄")

    # 5. スコアリング＋フィルタ
    scored = _score_stocks(
        raw_results,
        min_trades = args.min_trades,
        min_pf     = args.min_pf,
        min_pf_90  = args.min_pf_90,
        min_total  = args.min_total,
    )
    scored = scored[:args.top]
    print(f"  フィルタ後: {len(scored)}銘柄\n")

    # 6. ターミナル出力
    ph = "  ".join(f"{p}日PF" for p in WATCHLIST_PERIODS)
    print(f"  {'#':>3}  {'コード':<10}  {'銘柄名':<22}  {'現在値':>8}  {'スコア':>6}  {ph}  {'勝率':>6}  {'合計損益':>12}")
    print("  " + "─" * 105)
    for i, r in enumerate(scored, 1):
        pf_cols = []
        for p in WATCHLIST_PERIODS:
            ps = r["period_results"].get(p, {})
            n  = ps.get("n", 0)
            pf = ps.get("pf", 0)
            if n == 0:
                pf_cols.append(f"{'—':>7}")
            else:
                s = "∞" if pf == float("inf") else f"{pf:.2f}"
                pf_cols.append(f"{s:>7}")
        pf_s  = "∞" if r["pf365"] == float("inf") else f"{r['pf365']:.2f}"
        print(f"  {i:>3}  {r['symbol']:<10}  {r['name']:<22}  {r['price']:>8,.0f}  "
              f"{r['score']:>6.1f}  {'  '.join(pf_cols)}  {r['wr365']:>5.1f}%  {r['total365']:>+12,.0f}円")

    # watchlist_sp_budget.py として保存
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    out_py  = Path("watchlist_sp_budget.py")
    lines   = [
        '"""',
        f"強い押し目 予算別監視リスト（自動生成）",
        f"生成日時: {now_str}",
        f"予算: {budget:,}円（株価≤¥{max_price:,}）/ ユニバース: {n_universe}銘柄 / 予算内: {n_affordable}銘柄",
        f"選定: 上位{len(scored)}銘柄（4期間スコア順）",
        '"""',
        "",
        "WATCHLIST_SP_BUDGET: list[tuple[str, str]] = [",
    ]
    for r in scored:
        lines.append(f'    ("{r["symbol"]}", "{r["name"]}"),  # ¥{r["price"]:,.0f}  score:{r["score"]:.1f}')
    lines += ["]", ""]
    out_py.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  監視リスト保存: {out_py}")

    # 7. HTML生成
    html     = build_html(scored, budget, n_universe, n_affordable)
    today_s  = datetime.now(JST).strftime("%Y%m%d")
    out_html = Path(f"budget_watchlist_{budget//10000}万円_{today_s}.html")
    out_html.write_text(html, encoding="utf-8")
    print(f"  HTML出力: {out_html}")

    if not args.no_browser:
        webbrowser.open(out_html.resolve().as_uri())

    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    main()
