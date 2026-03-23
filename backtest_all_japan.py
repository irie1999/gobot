"""
全日本上場株バックテスト
  A0: ベースライン
  A6: ストキャスティクス

処理フロー:
  1. JPX 公開リストから全上場銘柄コードを取得（失敗時はフォールバックリスト）
  2. ThreadPoolExecutor で並列ダウンロード → disk キャッシュ保存
  3. A0 / A6 で各銘柄バックテスト
  4. アルゴリズムごとに利益上位 TOP_N 銘柄を出力
  5. HTML レポートを生成

使い方:
  python backtest_all_japan.py            # キャッシュを使って実行
  python backtest_all_japan.py --no-cache # キャッシュを再取得
  python backtest_all_japan.py --top 20   # 上位 20 銘柄を表示
"""

import argparse
import base64
import io
import logging
import os
import pickle
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ════════════════════════════════════════════════════════════════
# 定数
# ════════════════════════════════════════════════════════════════
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5
RISK_PER_TRADE = 0.03
INITIAL_CASH   = 500_000
MAX_COST_RATIO = 0.10
MAX_QTY        = 3000

TOP_N    = 10          # 上位表示件数（--top で上書き）
WORKERS  = 30          # 並列ダウンロードスレッド数
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache_japan")

JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)

# ローカル銘柄リスト: JPX からダウンロードした CSV を置くパス
# （ヘッダー行: code,name）
LOCAL_LIST_CSV = os.path.join(os.path.dirname(__file__), "japan_stocks.csv")

def _generate_candidate_codes() -> list[int]:
    """
    TSE 銘柄コードが存在しやすい範囲を網羅した候補コードを生成する。
    無効コードは並列ダウンロード時にスキップされる。
    TSE コード体系:
      1300-1999  水産・食料・農林
      2000-2999  食品・たばこ
      3000-3999  繊維・紙・化学（一部）
      4000-4999  化学・医薬
      5000-5999  石油・窯業・鉄鋼
      6000-6999  機械・電機
      7000-7999  精密・輸送・その他製造
      8000-8999  商社・金融・不動産
      9000-9999  運輸・通信・サービス
    コードは連続せず飛び番が多いため、全範囲を候補にしてスキャンする。
    """
    candidates = []
    for code in range(1300, 10000):
        candidates.append(code)
    return candidates


# ════════════════════════════════════════════════════════════════
# 銘柄リスト取得
# ════════════════════════════════════════════════════════════════

def fetch_jpx_stock_list() -> list[tuple[str, str]]:
    """
    銘柄リストを以下の優先順で取得する:
      1. ローカル CSV (japan_stocks.csv) ← 最速・確実
      2. JPX 公開 Excel (ネット接続時)
      3. 全範囲スキャン候補 (1300-9999) ← ダウンロード時にフィルタ
    """
    # ── 優先①: ローカル CSV ───────────────────────────────────
    if os.path.exists(LOCAL_LIST_CSV):
        try:
            df = pd.read_csv(LOCAL_LIST_CSV, dtype=str)
            pairs = []
            for _, row in df.iterrows():
                code = str(row.get("code", "")).strip().replace(".0", "")
                name = str(row.get("name", f"コード{code}")).strip()
                if len(code) == 4 and code.isdigit():
                    pairs.append((f"{code}.T", name))
            if pairs:
                print(f"  ローカル CSV から {len(pairs)} 銘柄 読み込み")
                return pairs
        except Exception as e:
            print(f"  ⚠ ローカル CSV 読み込み失敗: {e}")

    # ── 優先②: JPX 公開 Excel ─────────────────────────────────
    print("  JPX 上場銘柄リスト取得中 ...")
    try:
        resp = requests.get(JPX_URL, timeout=20,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        df = pd.read_excel(io.BytesIO(resp.content), header=0, dtype=str, engine="xlrd")
        df.columns = [str(c).strip() for c in df.columns]
        code_col = next((c for c in df.columns if "コード" in c), None)
        name_col = next((c for c in df.columns if "銘柄名" in c), None)
        if code_col is None or name_col is None:
            raise ValueError(f"列が見つかりません: {df.columns.tolist()}")
        pairs = []
        for _, row in df.iterrows():
            code = str(row[code_col]).strip().replace(".0", "")
            name = str(row[name_col]).strip()
            if len(code) == 4 and code.isdigit():
                pairs.append((f"{code}.T", name))
        print(f"  JPX リスト取得成功: {len(pairs)} 銘柄")
        return pairs
    except Exception as e:
        print(f"  ⚠ JPX リスト取得失敗 ({e})")

    # ── 優先③: 全範囲スキャン (1300-9999 の候補コード) ─────────
    candidates = _generate_candidate_codes()
    print(f"  → 全範囲スキャンモード: {len(candidates)} 候補コードを試みます")
    print(f"    (無効コードはダウンロード時に自動スキップ。初回は時間がかかります)")
    return [(f"{c}.T", f"{c}") for c in candidates]


# ════════════════════════════════════════════════════════════════
# データ取得 & キャッシュ
# ════════════════════════════════════════════════════════════════

def _cache_path(symbol: str) -> str:
    return os.path.join(CACHE_DIR, f"{symbol.replace('.', '_')}.pkl")


def fetch_data(symbol: str, start: str, end: str, use_cache: bool) -> pd.DataFrame | None:
    path = _cache_path(symbol)
    if use_cache and os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    try:
        df = yf.download(symbol, start=start, end=end,
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 60:
            return None
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
        df.columns = [c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        if len(df) < 60:
            return None
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(df, f)
        return df
    except Exception:
        return None


def parallel_download(
    stock_list: list[tuple[str, str]],
    start: str,
    end: str,
    use_cache: bool,
) -> dict[tuple[str, str], pd.DataFrame]:
    """並列ダウンロード。成功した銘柄のみ返す。"""
    total   = len(stock_list)
    results = {}
    done    = 0

    def _dl(item):
        sym, name = item
        df = fetch_data(sym, start, end, use_cache)
        return sym, name, df

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(_dl, item): item for item in stock_list}
        for fut in as_completed(futures):
            sym, name, df = fut.result()
            done += 1
            if df is not None:
                results[(sym, name)] = df
            # 進捗表示 (100件ごと)
            if done % 100 == 0 or done == total:
                print(f"  ダウンロード: {done}/{total}  取得成功: {len(results)}",
                      end="\r", flush=True)
    print()
    return results


# ════════════════════════════════════════════════════════════════
# インジケーター
# ════════════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    h = df["high"]
    l = df["low"]

    # EMA
    for span in (5, 20, 50, 200):
        df[f"ema{span}"] = c.ewm(span=span, adjust=False).mean()
    df["ema5_cross_dn20"] = (
        (df["ema5"] < df["ema20"]) &
        (df["ema5"].shift(1) >= df["ema20"].shift(1))
    )

    # RSI(14)
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

    # ボリンジャーバンド(20,2)
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std(ddof=0)
    df["bb_upper"] = sma20 + 2.0 * std20
    df["bb_lower"] = sma20 - 2.0 * std20
    df["bb_mid"]   = sma20
    df["bb_band"]  = 2.0 * std20

    # ATR(14)
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # ストキャスティクス(14, 3, 3)
    hh14 = h.rolling(14).max()
    ll14 = l.rolling(14).min()
    stoch_raw    = (c - ll14) / (hh14 - ll14).replace(0, np.nan) * 100
    df["stoch_k"] = stoch_raw.rolling(3).mean()
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()
    df["stoch_k_cross_up"] = (
        (df["stoch_k"] > df["stoch_d"]) &
        (df["stoch_k"].shift(1) <= df["stoch_d"].shift(1))
    )
    df["stoch_k_cross_dn"] = (
        (df["stoch_k"] < df["stoch_d"]) &
        (df["stoch_k"].shift(1) >= df["stoch_d"].shift(1))
    )

    return df


# ════════════════════════════════════════════════════════════════
# アルゴリズム定義
# ════════════════════════════════════════════════════════════════

def algo_A0_baseline(df: pd.DataFrame) -> pd.DataFrame:
    """A0: ベースライン
    Entry: EMA50上昇トレンド + (RSI<55 OR EMA5クロスアップ OR BB下限付近)
    Exit : RSI>60 OR BB上限到達 OR EMA5<EMA20
    """
    ema5_cross_up20 = (
        (df["ema5"] > df["ema20"]) &
        (df["ema5"].shift(1) <= df["ema20"].shift(1))
    )
    trend_up = df["close"] > df["ema50"]
    df["entry_sig"] = trend_up & (
        (df["rsi"] < 55) |
        ema5_cross_up20 |
        (df["close"] <= df["bb_lower"] + df["bb_band"])
    )
    df["exit_sig"] = (
        (df["rsi"]   > 60) |
        (df["close"] >= df["bb_upper"]) |
        df["ema5_cross_dn20"]
    )
    return df


def algo_A6_stoch(df: pd.DataFrame) -> pd.DataFrame:
    """A6: ストキャスティクス クロス
    Entry: %K が %D を 30 以下から上抜け
    Exit : %K が %D を 60 以上で下抜け OR %K が %D を上から下抜け
    """
    df["entry_sig"] = df["stoch_k_cross_up"] & (df["stoch_k"].shift(1) < 30)
    df["exit_sig"]  = df["stoch_k_cross_dn"] & (df["stoch_k"].shift(1) > 60)
    return df


ALGORITHMS = [
    ("A0: ベースライン",      algo_A0_baseline),
    ("A6: ストキャスティクス", algo_A6_stoch),
]


# ════════════════════════════════════════════════════════════════
# バックテストエンジン
# ════════════════════════════════════════════════════════════════

def calc_qty(cash: float, atr: float, close: float) -> int:
    stop_dist = atr * ATR_STOP_MULT
    if stop_dist <= 0:
        return 0
    qty_risk = int(cash * RISK_PER_TRADE / stop_dist)
    qty_cost = int(cash * MAX_COST_RATIO / close) if close > 0 else qty_risk
    return max(min(qty_risk, qty_cost, MAX_QTY), 1)


def backtest_symbol(df: pd.DataFrame, symbol: str, name: str) -> dict:
    cash       = INITIAL_CASH
    in_pos     = False
    entry_price = stop_price = 0.0
    qty        = 0
    entry_dt   = None
    trades     = []

    for i in range(1, len(df)):
        today = df.iloc[i]
        prev  = df.iloc[i - 1]

        if in_pos:
            exit_price = exit_reason = None
            if today["low"] <= stop_price:
                exit_price  = min(float(today["open"]), stop_price)
                exit_reason = "stop"
            elif prev["exit_sig"]:
                exit_price  = float(today["open"])
                exit_reason = "signal"

            if exit_price is not None:
                pnl   = (exit_price - entry_price) * qty
                cash += exit_price * qty
                trades.append({
                    "entry_dt":    entry_dt,
                    "exit_dt":     df.index[i],
                    "pnl":         pnl,
                    "entry_price": entry_price,
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "reason":      exit_reason,
                })
                in_pos = False

        if not in_pos and prev["entry_sig"]:
            q    = calc_qty(cash, float(prev["atr"]), float(prev["close"]))
            cost = float(today["open"]) * q
            if q > 0 and cost <= cash:
                entry_price = float(today["open"])
                stop_price  = entry_price - float(prev["atr"]) * ATR_STOP_MULT
                qty         = q
                cash       -= cost
                entry_dt    = df.index[i]
                in_pos      = True

    if in_pos:
        ep  = float(df.iloc[-1]["close"])
        pnl = (ep - entry_price) * qty
        cash += ep * qty
        trades.append({
            "entry_dt":    entry_dt,
            "exit_dt":     df.index[-1],
            "pnl":         pnl,
            "entry_price": entry_price,
            "exit_price":  ep,
            "qty":         qty,
            "reason":      "force_close",
        })

    total_pnl = cash - INITIAL_CASH
    n   = len(trades)
    wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    pf = (sum(wins) / abs(sum(losses))
          if losses and sum(losses) != 0 else float("inf"))

    return {
        "symbol":        symbol,
        "name":          name,
        "total_pnl":     total_pnl,
        "return_pct":    total_pnl / INITIAL_CASH * 100,
        "n_trades":      n,
        "win_rate":      len(wins) / n * 100 if n else 0.0,
        "profit_factor": pf,
        "trades":        trades,
    }


# ════════════════════════════════════════════════════════════════
# HTML レポート
# ════════════════════════════════════════════════════════════════

def fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _top_table_html(top_rows: list[dict], algo_id: str) -> str:
    rows = ""
    for rank, r in enumerate(top_rows, 1):
        sgn = "+" if r["total_pnl"] >= 0 else ""
        cls = "profit" if r["total_pnl"] >= 0 else "loss"
        pf  = "∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
        rows += (
            f'<tr><td class="num">{rank}</td>'
            f'<td>{r["symbol"]}</td>'
            f'<td>{r["name"]}</td>'
            f'<td class="{cls}">{sgn}{r["total_pnl"]:,.0f}</td>'
            f'<td class="num">{sgn}{r["return_pct"]:.1f}%</td>'
            f'<td class="num">{r["win_rate"]:.1f}%</td>'
            f'<td class="num">{pf}</td>'
            f'<td class="num">{r["n_trades"]}</td></tr>\n'
        )
    return rows


def export_html(
    algo_top: list[tuple[str, list[dict]]],
    n_total: int,
    n_ok: int,
    start: str,
    end: str,
    path: str,
) -> None:
    plt.rcParams["font.family"] = [
        "IPAexGothic", "Noto Sans CJK JP", "Hiragino Sans", "MS Gothic", "sans-serif"
    ]

    # アルゴごとに上位バーチャート
    charts_html = ""
    for algo_name, top_rows in algo_top:
        if not top_rows:
            continue
        labels = [f"{r['symbol']}\n{r['name'][:6]}" for r in top_rows]
        pnls   = [r["total_pnl"] for r in top_rows]
        colors = ["#276221" if v >= 0 else "#9c0006" for v in pnls]

        fig, ax = plt.subplots(figsize=(12, 4), facecolor="#f8f9fa")
        ax.set_facecolor("#ffffff")
        bars = ax.bar(labels, pnls, color=colors, width=0.6, edgecolor="white")
        for b, v in zip(bars, pnls):
            ax.text(
                b.get_x() + b.get_width() / 2,
                v + max(abs(x) for x in pnls) * 0.02 * (1 if v >= 0 else -1),
                f"{v:+,.0f}", ha="center",
                va="bottom" if v >= 0 else "top", fontsize=7,
            )
        ax.axhline(0, color="#888", linewidth=0.8)
        ax.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda x, _: f"{x:+,.0f}")
        )
        ax.set_title(f"{algo_name} — 利益上位銘柄 (円)", fontsize=12, pad=8)
        ax.grid(axis="y", linestyle=":", alpha=0.5)
        ax.spines[["top", "right"]].set_visible(False)
        plt.xticks(fontsize=7)
        fig.tight_layout()
        img = fig_to_b64(fig)
        charts_html += (
            f'<div class="chart-card">'
            f'<img src="data:image/png;base64,{img}" alt="{algo_name}"></div>\n'
        )

    # テーブルセクション
    tables_html = ""
    for algo_name, top_rows in algo_top:
        rows_html = _top_table_html(top_rows, algo_name)
        tables_html += f"""
<div class="section">
  <h2 class="sec">{algo_name} — 利益上位 {len(top_rows)} 銘柄</h2>
  <table>
    <thead><tr>
      <th>順位</th><th>コード</th><th>銘柄名</th>
      <th>総損益(円)</th><th>収益率</th><th>勝率</th><th>PF</th><th>取引数</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>全日本上場株バックテスト</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:"Yu Gothic UI","Hiragino Sans","Noto Sans JP",sans-serif;
        background:#f0f2f5;color:#222;font-size:14px}}
  .header{{background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460);
           color:#fff;padding:24px 32px}}
  .header h1{{font-size:1.5em;margin-bottom:6px}}
  .header p{{opacity:.85;font-size:.88em;line-height:1.8}}
  .section{{margin:24px 32px}}
  h2.sec{{font-size:1.05em;color:#1f3864;border-left:4px solid #2e75b6;
           padding-left:10px;margin-bottom:14px}}
  table{{width:100%;border-collapse:collapse;background:#fff;
         border-radius:8px;overflow:hidden;
         box-shadow:0 1px 4px rgba(0,0,0,.1)}}
  thead th{{background:#1f3864;color:#fff;padding:10px 14px;
            text-align:center;font-size:.86em;white-space:nowrap}}
  tbody tr:hover{{background:#eaf1fb!important}}
  td{{padding:9px 14px;border-bottom:1px solid #e8edf3;white-space:nowrap}}
  .num{{text-align:right}}
  .profit{{text-align:right;color:#276221;font-weight:600}}
  .loss{{text-align:right;color:#9c0006;font-weight:600}}
  .chart-card{{background:#fff;border-radius:8px;padding:16px;
               box-shadow:0 1px 4px rgba(0,0,0,.1);
               margin:0 32px 20px}}
  .chart-card img{{width:100%;height:auto}}
</style>
</head>
<body>
<div class="header">
  <h1>📊 全日本上場株バックテスト — A0/A6</h1>
  <p>期間: {start} ～ {end}（約5年）
     対象: {n_ok} / {n_total} 銘柄（データ取得成功分）
     初期資金: {INITIAL_CASH:,}円/銘柄　ストップ: ATR×{ATR_STOP_MULT}<br>
     生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div>

{tables_html}

<div class="section"><h2 class="sec">利益上位銘柄 チャート</h2></div>
{charts_html}

</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ════════════════════════════════════════════════════════════════
# メイン
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="全日本上場株バックテスト (A0/A6)")
    parser.add_argument("--top",      type=int,  default=TOP_N,   help="上位N銘柄 (default:10)")
    parser.add_argument("--no-cache", action="store_true",        help="キャッシュを再取得")
    args = parser.parse_args()

    top_n     = args.top
    use_cache = not args.no_cache

    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=365 * 5 + 60)).strftime("%Y-%m-%d")

    W = 72
    print(f"\n{'='*W}")
    print(f"  全日本上場株バックテスト — A0: ベースライン / A6: ストキャスティクス")
    print(f"  期間: {start} ～ {end}  初期資金: {INITIAL_CASH:,}円/銘柄")
    print(f"  キャッシュ: {'使用' if use_cache else '無効（再取得）'}  上位表示: {top_n}件")
    print(f"{'='*W}\n")

    # ── STEP 1: 銘柄リスト ──────────────────────────────────────
    print("[1/4] 銘柄リスト取得中 ...")
    stock_list = fetch_jpx_stock_list()
    print(f"  対象銘柄数: {len(stock_list)}\n")

    # ── STEP 2: データダウンロード ──────────────────────────────
    print(f"[2/4] データダウンロード中 (並列 {WORKERS} スレッド) ...")
    t0 = time.time()
    raw_data = parallel_download(stock_list, start, end, use_cache)
    elapsed  = time.time() - t0
    print(f"  完了: {len(raw_data)} / {len(stock_list)} 銘柄  ({elapsed:.0f}秒)\n")

    if not raw_data:
        print("ERROR: 有効なデータが取得できませんでした。")
        sys.exit(1)

    # ── STEP 3: インジケーター計算 ───────────────────────────────
    print("[3/4] インジケーター計算 & バックテスト実行中 ...")
    # インジケーターを一度だけ計算してキャッシュ
    ind_data = {}
    for key, df in raw_data.items():
        try:
            ind_data[key] = add_indicators(df.copy())
        except Exception:
            pass
    print(f"  インジケーター計算完了: {len(ind_data)} 銘柄")

    # アルゴごとにバックテスト
    algo_top: list[tuple[str, list[dict]]] = []

    for algo_name, algo_fn in ALGORITHMS:
        sym_results = []
        for (symbol, name), df_ind in ind_data.items():
            try:
                df = algo_fn(df_ind.copy())
                res = backtest_symbol(df, symbol, name)
                sym_results.append(res)
            except Exception:
                pass

        # 利益でソート → 上位 top_n
        sym_results.sort(key=lambda x: x["total_pnl"], reverse=True)
        top_rows = sym_results[:top_n]
        algo_top.append((algo_name, top_rows))

        # コンソール出力
        sgn_total = "+" if sym_results[0]["total_pnl"] >= 0 else "" if sym_results else ""
        print(f"\n  ┌─ {algo_name}  (バックテスト銘柄数: {len(sym_results)}) ─")
        print(f"  │ {'順位':<4} {'コード':<10} {'銘柄名':<20} "
              f"{'総損益':>12} {'収益率':>8} {'勝率':>7} {'取引数':>6}")
        print(f"  │ {'─'*70}")
        for rank, r in enumerate(top_rows, 1):
            sgn = "+" if r["total_pnl"] >= 0 else ""
            pf  = "∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
            print(f"  │ {rank:<4} {r['symbol']:<10} {r['name']:<20} "
                  f"{sgn}{r['total_pnl']:>10,.0f}円 "
                  f"{sgn}{r['return_pct']:>6.1f}% "
                  f"{r['win_rate']:>5.1f}% "
                  f"{r['n_trades']:>5}回")
        print(f"  └{'─'*72}")

    # ── STEP 4: HTML レポート ────────────────────────────────────
    print("\n[4/4] HTML レポート生成中 ...")
    html_path = os.path.join(os.path.dirname(__file__), "backtest_all_japan.html")
    export_html(algo_top, len(stock_list), len(ind_data), start, end, html_path)
    print(f"  保存完了: {os.path.abspath(html_path)}")
    print(f"\n{'='*W}")
    print("  完了！HTMLレポートを開いて詳細を確認してください。")
    print(f"{'='*W}\n")


if __name__ == "__main__":
    main()
