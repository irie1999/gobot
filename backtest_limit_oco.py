"""
指値エントリー + OCO決済 バックテスト & シグナルスキャン
────────────────────────────────────────────────────────
■ 戦略概要
  1. シグナル判定（終値ベース）
     - RSI(2) ≤ 閾値（デフォルト 10）
     - 終値 > MA200（上昇トレンド）
     - IBS < 0.35（終値が日中安値圏）

  2. 指値エントリー（翌日から有効）
     - 指値価格 = シグナル日終値 - ATR × ENTRY_ATR_MULT
     - 有効日数 = ENTRY_EXPIRE_DAYS 日（未成立でキャンセル）

  3. OCO決済
     - 利確指値   : エントリー価格 + ATR × PROFIT_ATR_MULT
     - 損切り逆指値: エントリー価格 - ATR × STOP_ATR_MULT
     - 最大保有日数: FORCE_EXIT_DAYS 日（超過で終値決済）

■ 実行方法
  python backtest_limit_oco.py                   # 全銘柄スキャン
  python backtest_limit_oco.py --symbol 7203.T   # 個別銘柄詳細
  python backtest_limit_oco.py --optimize        # パラメーター最適化
  python backtest_limit_oco.py --days 730        # バックテスト期間変更
"""

import argparse
import itertools
import pickle
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── 定数 ────────────────────────────────────────────────────────
JST           = timezone(timedelta(hours=9))
_TODAY        = datetime.now(JST).date()
_CACHE_DIR    = Path(".rsi2_cache")
_BT_CACHE     = _CACHE_DIR / "bt_results_limit_oco.pkl"   # バックテスト結果キャッシュ
LOT_SIZE      = 100       # 1回あたり株数（固定100株）
BACKTEST_DAYS = 365
WORKERS       = 4

# ── デフォルトパラメーター ───────────────────────────────────────
DEFAULT_PARAMS = dict(
    RSI2_ENTRY        = 10.0,   # RSI(2) ≤ 閾値でシグナル
    ENTRY_ATR_MULT    =  0.3,   # 指値 = 終値 - ATR × 係数
    PROFIT_ATR_MULT   =  2.0,   # 利確 = エントリー + ATR × 係数
    STOP_ATR_MULT     =  1.0,   # 損切り = エントリー - ATR × 係数
    ENTRY_EXPIRE_DAYS =  2,     # 指値有効日数（未成立でキャンセル）
    FORCE_EXIT_DAYS   = 10,     # 最大保有日数（超過で終値強制決済）
)

# ── 監視銘柄選定パラメーター ─────────────────────────────────────
WATCHLIST_PERIODS = [30, 90, 180, 365]   # 1か月/3か月/6か月/1年
WL_MIN_TRADES     = 1      # 取引ありのピリオドで判定（0件は除外）
WL_MIN_WR         = 55.0   # 勝率55%以上
WL_MIN_PF         = 1.0    # PF 1.0以上（損益トントン以上）
WL_MIN_ACTIVE     = 2      # 取引ありのピリオドが最低この数必要

# ── グリッドサーチ候補 ───────────────────────────────────────────
GRID = dict(
    RSI2_ENTRY        = [5.0, 10.0, 15.0],
    ENTRY_ATR_MULT    = [0.1, 0.3, 0.5],
    PROFIT_ATR_MULT   = [1.5, 2.0, 3.0],
    STOP_ATR_MULT     = [0.5, 1.0, 1.5],
    ENTRY_EXPIRE_DAYS = [1, 2],
    FORCE_EXIT_DAYS   = [10],
)


# ── キャッシュ読み込み ───────────────────────────────────────────
def fetch(symbol: str, backtest_days: int) -> pd.DataFrame | None:
    """永続キャッシュ優先・フォールバックでダウンロード。"""
    persistent = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    if persistent.exists():
        try:
            mtime = datetime.fromtimestamp(persistent.stat().st_mtime, tz=JST)
            if mtime.date() == datetime.now(JST).date():
                with open(persistent, "rb") as f:
                    df = pickle.load(f)
                if len(df) >= 210:
                    return df
        except Exception:
            pass

    buf_days  = 200 + 30
    total_cal = int((backtest_days + buf_days) * 1.5)
    now_jst   = datetime.now(JST)
    dl_start  = (now_jst - timedelta(days=total_cal)).strftime("%Y-%m-%d")
    dl_end    = (now_jst + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        raw = yf.Ticker(symbol).history(start=dl_start, end=dl_end,
                                         interval="1d", auto_adjust=False, actions=False)
        if raw.empty:
            return None
        if raw.index.tz is not None:
            raw.index = raw.index.tz_localize(None)
        raw.columns = [str(c).lower() for c in raw.columns]
        raw = raw.loc[:, ~raw.columns.duplicated(keep="first")]
        cols = [c for c in ["open", "high", "low", "close", "volume"] if c in raw.columns]
        raw = raw[cols]
        _last = raw.iloc[-1]
        if pd.isna(_last["close"]) and _last["volume"] > 0:
            try:
                lp = yf.Ticker(symbol).fast_info.last_price
                if lp and not pd.isna(lp):
                    raw.at[raw.index[-1], "close"] = float(lp)
            except Exception:
                pass
        raw = raw.dropna(subset=["close"])
        if len(raw) < 210:
            return None
        return pd.DataFrame({
            "open":   raw["open"].to_numpy(dtype=float),
            "high":   raw["high"].to_numpy(dtype=float),
            "low":    raw["low"].to_numpy(dtype=float),
            "close":  raw["close"].to_numpy(dtype=float),
            "volume": raw["volume"].to_numpy(dtype=float),
        }, index=raw.index)
    except Exception:
        return None


# ── インジケーター計算 ───────────────────────────────────────────
def calc(raw: pd.DataFrame) -> pd.DataFrame:
    """RSI(2), MA200, ATR(14), IBS を追加したDataFrameを返す。"""
    df = raw.copy()

    # RSI(2)
    delta = df["close"].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1/2, adjust=False).mean()
    avg_l = loss.ewm(alpha=1/2, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    df["rsi2"] = 100 - 100 / (1 + rs)

    # MA200
    df["ma200"] = df["close"].rolling(200).mean()

    # ATR(14)
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(span=14, adjust=False).mean()

    # IBS（Internal Bar Strength）
    bar_range = df["high"] - df["low"]
    df["ibs"]  = np.where(
        bar_range > 0,
        (df["close"] - df["low"]) / bar_range,
        0.5,
    )

    return df.dropna(subset=["ma200"])


# ── バックテスト（指値エントリー + OCO決済）─────────────────────
def backtest_limit(df: pd.DataFrame, backtest_days: int, params: dict) -> list[dict]:
    """
    指値エントリー + OCO（利確指値 / 損切り逆指値）バックテスト。

    Returns: トレードログ（dict のリスト）
    """
    p       = params
    cutoff  = pd.Timestamp(_TODAY - timedelta(days=backtest_days))
    df      = df[df.index >= cutoff].copy()
    trades: list[dict] = []

    # 状態変数
    state          = "idle"    # idle / pending / in_pos
    limit_price    = 0.0       # 指値価格
    signal_atr     = 0.0       # シグナル時のATR（OCO計算用）
    signal_dt: pd.Timestamp | None = None
    entry_p        = 0.0
    entry_dt: pd.Timestamp | None  = None
    profit_target  = 0.0
    stop_loss      = 0.0
    qty            = 0
    days_pending   = 0

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        dt   = df.index[i]

        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])

        if pd.isna(prev.get("rsi2")) or pd.isna(prev.get("ma200")):
            continue

        # ── pending: 指値注文の約定チェック ────────────────────────
        if state == "pending":
            days_pending += 1

            if days_pending > p["ENTRY_EXPIRE_DAYS"]:
                # 有効期限切れ → キャンセル
                state = "idle"
                days_pending = 0

            elif lo <= limit_price:
                # 約定（当日の安値が指値以下）
                entry_p       = limit_price
                entry_dt      = dt
                profit_target = entry_p + signal_atr * p["PROFIT_ATR_MULT"]
                stop_loss     = entry_p - signal_atr * p["STOP_ATR_MULT"]
                qty           = LOT_SIZE
                state         = "in_pos"
                days_pending  = 0

                # 約定と同日に損切り／利確が発生するか確認
                exit_p = exit_reason = None
                if lo <= stop_loss:
                    # 損切りが先（保守的）
                    exit_p      = stop_loss
                    exit_reason = "損切り"
                elif hi >= profit_target:
                    exit_p      = profit_target
                    exit_reason = "利確"

                if exit_p is not None:
                    pnl = (exit_p - entry_p) * qty
                    trades.append(dict(
                        entry_dt=entry_dt, exit_dt=dt,
                        entry_p=entry_p, exit_p=exit_p, qty=qty,
                        pnl=pnl, pct=(exit_p - entry_p) / entry_p * 100,
                        hold=0, reason=exit_reason,
                        limit_price=limit_price,
                        stop_loss=stop_loss,
                        profit_target=profit_target,
                    ))
                    state = "idle"
                continue

        # ── in_pos: OCO決済チェック ────────────────────────────────
        if state == "in_pos":
            exit_p = exit_reason = None
            hold   = (dt - entry_dt).days

            if lo <= stop_loss:
                exit_p      = stop_loss
                exit_reason = "損切り"
            elif hi >= profit_target:
                exit_p      = profit_target
                exit_reason = "利確"
            elif hold >= p["FORCE_EXIT_DAYS"]:
                exit_p      = cl
                exit_reason = f"強制決済({hold}日)"

            if exit_p is not None:
                pnl = (exit_p - entry_p) * qty
                trades.append(dict(
                    entry_dt=entry_dt, exit_dt=dt,
                    entry_p=entry_p, exit_p=exit_p, qty=qty,
                    pnl=pnl, pct=(exit_p - entry_p) / entry_p * 100,
                    hold=hold, reason=exit_reason,
                    limit_price=limit_price,
                ))
                state = "idle"
            continue

        # ── idle: シグナル判定 ────────────────────────────────────
        if state == "idle":
            rsi_p  = float(prev["rsi2"])
            ma200  = float(prev["ma200"])
            close_p = float(prev["close"])
            ibs_p  = float(prev["ibs"])
            atr_p  = float(prev["atr"])

            if pd.isna(atr_p) or atr_p <= 0:
                continue

            # 基本シグナル条件
            if not (rsi_p <= p["RSI2_ENTRY"] and close_p > ma200 and ibs_p < 0.35):
                continue

            # 指値注文を設定
            limit_price  = close_p - atr_p * p["ENTRY_ATR_MULT"]
            signal_atr   = atr_p
            signal_dt    = prev.name
            state        = "pending"
            days_pending = 0

    # 未決済ポジション（バックテスト最終日）
    if state == "in_pos":
        lp   = float(df.iloc[-1]["close"])
        hold = (df.index[-1] - entry_dt).days
        trades.append(dict(
            entry_dt=entry_dt, exit_dt=df.index[-1],
            entry_p=entry_p, exit_p=lp, qty=qty,
            pnl=(lp - entry_p) * qty,
            pct=(lp - entry_p) / entry_p * 100,
            hold=hold, reason="保有中★",
            limit_price=limit_price,
        ))

    return trades


# ── パラメーター最適化 ────────────────────────────────────────────
def optimize_params(df: pd.DataFrame, backtest_days: int) -> dict:
    """グリッドサーチで最良パラメーターを返す（PF × 勝率 最大化）。"""
    keys   = list(GRID.keys())
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    best   = None
    best_score = -1.0

    for combo in combos:
        p      = dict(zip(keys, combo))
        trades = backtest_limit(df, backtest_days, p)
        if len(trades) < 3:
            continue
        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        wr     = len(wins) / len(trades)
        loss_sum = abs(sum(t["pnl"] for t in losses))
        pf     = (sum(t["pnl"] for t in wins) / loss_sum
                  if loss_sum > 0 else float("inf"))
        pf_cap = min(pf, 10.0)   # inf を除外するためにキャップ
        score  = pf_cap * wr
        if score > best_score:
            best_score = score
            best       = p

    return best or DEFAULT_PARAMS


# ── 今日のシグナル判定 ────────────────────────────────────────────
def _has_signal_today(df: pd.DataFrame, params: dict) -> dict | None:
    """最終行（今日）のシグナルを判定。シグナルありなら注文情報を返す。"""
    if len(df) < 2:
        return None
    prev   = df.iloc[-1]   # 今日の終値（シグナル判定は終値確定後）
    rsi_p  = float(prev.get("rsi2", float("nan")))
    ma200  = float(prev.get("ma200", float("nan")))
    close_p = float(prev.get("close", float("nan")))
    ibs_p  = float(prev.get("ibs", float("nan")))
    atr_p  = float(prev.get("atr", float("nan")))

    if any(pd.isna(v) for v in [rsi_p, ma200, close_p, ibs_p, atr_p]):
        return None
    if atr_p <= 0:
        return None

    p = params
    if rsi_p <= p["RSI2_ENTRY"] and close_p > ma200 and ibs_p < 0.35:
        limit_price    = close_p - atr_p * p["ENTRY_ATR_MULT"]
        profit_target  = limit_price + atr_p * p["PROFIT_ATR_MULT"]
        stop_loss      = limit_price - atr_p * p["STOP_ATR_MULT"]
        return dict(
            close=close_p, rsi2=rsi_p, ma200=ma200, ibs=ibs_p, atr=atr_p,
            limit_price=limit_price,
            profit_target=profit_target,
            stop_loss=stop_loss,
            expire_days=p["ENTRY_EXPIRE_DAYS"],
        )
    return None


# ── HTML生成 ──────────────────────────────────────────────────────
_CSS = """
body{font-family:'Meiryo',sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:20px}
h1{font-size:1.3em;color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:8px}
h2{font-size:1.05em;color:#79c0ff;margin-top:28px}
table{border-collapse:collapse;width:100%;font-size:0.82em;margin-top:8px}
th{background:#161b22;color:#8b949e;padding:6px 10px;border:1px solid #30363d;text-align:right}
th:first-child{text-align:left}
td{padding:5px 10px;border:1px solid #21262d;text-align:right}
td:first-child{text-align:left}
tr:nth-child(even){background:#161b22}
.win{color:#3fb950}.loss{color:#f85149}.neutral{color:#e6edf3}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.78em;font-weight:bold}
.badge-buy{background:#1a4731;color:#3fb950;border:1px solid #3fb950}
.badge-warn{background:#3d2b00;color:#d29922;border:1px solid #d29922}
.params{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px;
        font-size:0.83em;margin:8px 0;display:flex;flex-wrap:wrap;gap:12px}
.params span{color:#8b949e}.params b{color:#e6edf3}
"""


def _pct_class(v: float) -> str:
    return "win" if v > 0 else ("loss" if v < 0 else "neutral")


def _trade_table_html(trades: list[dict]) -> str:
    rows = ""
    for i, t in enumerate(trades, 1):
        cls   = _pct_class(t["pct"])
        mark  = "★" if "保有中" in t["reason"] else ""
        lp    = t.get("limit_price",   t["entry_p"])
        sl    = t.get("stop_loss",     float("nan"))
        tgt   = t.get("profit_target", float("nan"))
        pnl   = t.get("pnl", 0.0)
        sl_s  = f'{sl:,.0f}'  if not pd.isna(sl)  else "—"
        tgt_s = f'{tgt:,.0f}' if not pd.isna(tgt) else "—"
        rows += (
            f'<tr><td>{mark}{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{lp:,.0f}</td>'
            f'<td>{t["entry_p"]:,.0f}</td>'
            f'<td class="neg">{sl_s}</td>'
            f'<td class="pos">{tgt_s}</td>'
            f'<td>{t["exit_p"]:,.0f}</td>'
            f'<td class="{cls}">{t["pct"]:+.1f}%</td>'
            f'<td class="{cls}">{pnl:+,.0f}円</td>'
            f'<td>{t["hold"]}日</td>'
            f'<td>{t["reason"]}</td></tr>\n'
        )
    header = (
        "<tr><th>#</th><th>エントリー日</th><th>決済日</th>"
        "<th>指値</th><th>約定値</th><th>逆指値</th><th>利確目標</th><th>決済値</th>"
        "<th>損益%</th><th>損益(円)</th><th>保有</th><th>理由</th></tr>"
    )
    return f"<table>{header}{rows}</table>"


def _summary_html(trades: list[dict], label: str) -> str:
    if not trades:
        return f"<p>{label}: トレードなし</p>"
    wins      = [t for t in trades if t["pnl"] > 0]
    losses    = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    wr        = len(wins) / len(trades) * 100
    loss_sum  = abs(sum(t["pnl"] for t in losses))
    pf        = (sum(t["pnl"] for t in wins) / loss_sum
                 if loss_sum > 0 else float("inf"))
    pf_s      = "∞" if pf == float("inf") else f"{pf:.2f}"
    avg_hold  = sum(t["hold"] for t in trades) / len(trades)
    fill_n    = sum(1 for t in trades if "保有中" not in t["reason"])
    return (
        f"<b>{label}</b> | "
        f"取引: {len(trades)}回（約定: {fill_n}） | "
        f"勝率: {wr:.0f}% | PF: {pf_s} | "
        f"合計損益: <span class='{_pct_class(total_pnl)}'>{total_pnl:+,.0f}円</span> | "
        f"平均保有: {avg_hold:.1f}日"
    )


def _params_html(params: dict) -> str:
    items = "".join(
        f"<span>{k}:</span> <b>{v}</b>"
        for k, v in params.items()
    )
    return f'<div class="params">{items}</div>'


def build_html(
    signals: list[dict],
    scan_date: str,
    backtest_days: int,
) -> str:
    today_str = _TODAY.strftime("%Y-%m-%d")
    signal_rows = ""
    for s in signals:
        sym    = s["symbol"]
        name   = s["name"]
        sig    = s["signal"]
        stats  = s["stats"]
        params = s["params"]
        wr_s   = f"{stats['wr']:.0f}%" if stats else "—"
        pf_v   = stats["pf"] if stats else None
        pf_s   = ("∞" if pf_v == float("inf") else f"{pf_v:.2f}") if pf_v is not None else "—"
        signal_rows += (
            f'<tr>'
            f'<td>{sym}</td><td>{name}</td>'
            f'<td>{sig["close"]:,.0f}</td>'
            f'<td>{sig["rsi2"]:.1f}</td>'
            f'<td>{sig["ibs"]:.2f}</td>'
            f'<td class="win"><b>{sig["limit_price"]:,.0f}</b></td>'
            f'<td class="win">{sig["profit_target"]:,.0f}</td>'
            f'<td class="loss">{sig["stop_loss"]:,.0f}</td>'
            f'<td>{sig["expire_days"]}日</td>'
            f'<td>{wr_s}</td><td>{pf_s}</td>'
            f'</tr>\n'
        )

    signal_table = (
        "<table>"
        "<tr><th>コード</th><th>銘柄名</th><th>終値</th>"
        "<th>RSI2</th><th>IBS</th>"
        "<th>買い指値</th><th>利確指値</th><th>損切り逆指値</th>"
        "<th>有効期間</th><th>勝率</th><th>PF</th></tr>"
        + signal_rows
        + "</table>"
    ) if signals else "<p>本日シグナルなし</p>"

    detail_sections = ""
    for s in signals:
        sym    = s["symbol"]
        name   = s["name"]
        trades = s.get("trades", [])
        params = s["params"]
        detail_sections += (
            f"<h2>{sym} {name}</h2>"
            + _params_html(params)
            + f"<p>{_summary_html(trades, f'直近{backtest_days}日')}</p>"
            + _trade_table_html(trades)
        )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8">
<title>指値OCO シグナル {today_str}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>指値エントリー + OCO決済 シグナル — {today_str}</h1>
<p style="color:#8b949e">バックテスト期間: 直近{backtest_days}日 ／ スキャン日時: {scan_date}</p>

<h2>本日のシグナル銘柄 <span class="badge badge-buy">{len(signals)}件</span></h2>
{signal_table}

<h2>シグナル銘柄 バックテスト詳細</h2>
{detail_sections if detail_sections else '<p>詳細なし</p>'}
</body></html>"""


def _calc_period_stats(trades):
    if not trades:
        return dict(n=0, wr=float("nan"), pf=float("nan"), total=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    loss = [t for t in trades if t["pnl"] <= 0]
    wr   = len(wins) / len(trades) * 100
    ls   = abs(sum(t["pnl"] for t in loss))
    pf   = sum(t["pnl"] for t in wins) / ls if ls > 0 else float("inf")
    return dict(n=len(trades), wr=wr, pf=pf, total=sum(t["pnl"] for t in trades))


def _load_bt_cache() -> dict:
    """バックテスト結果キャッシュを読み込む。"""
    try:
        if _BT_CACHE.exists():
            with open(_BT_CACHE, "rb") as f:
                return pickle.load(f)
    except Exception:
        pass
    return {}


def _save_bt_cache(cache: dict) -> None:
    """バックテスト結果キャッシュを保存する。"""
    try:
        _CACHE_DIR.mkdir(exist_ok=True)
        with open(_BT_CACHE, "wb") as f:
            pickle.dump(cache, f)
    except Exception:
        pass


def _price_cache_mtime(symbol: str) -> float:
    """株価キャッシュファイルの更新日時を返す（なければ0）。"""
    p = _CACHE_DIR / f"{symbol.replace('.', '_')}.pkl"
    return p.stat().st_mtime if p.exists() else 0.0


def _process_symbol_multiperiod(symbol, name, periods, bt_cache: dict | None = None):
    # ── キャッシュヒット確認 ────────────────────────────────────
    mtime = _price_cache_mtime(symbol)
    cache_key = (symbol, tuple(sorted(periods)), LOT_SIZE)
    if bt_cache is not None and cache_key in bt_cache:
        cached_mtime, cached_result = bt_cache[cache_key]
        if cached_mtime == mtime and mtime > 0:
            return cached_result   # キャッシュ利用

    # ── バックテスト実行 ────────────────────────────────────────
    max_days = max(periods)
    df_raw = fetch(symbol, max_days)
    if df_raw is None:
        return None
    df = calc(df_raw)
    if len(df) < 50:
        return None
    period_results = {}
    period_trades  = {}
    for days in periods:
        trades = backtest_limit(df, days, DEFAULT_PARAMS)
        period_results[days] = _calc_period_stats(trades)
        period_trades[days]  = trades
    today_sig = _has_signal_today(df, DEFAULT_PARAMS)
    last_close = float(df.iloc[-1]["close"]) if len(df) > 0 else 0.0
    result = dict(symbol=symbol, name=name, period_results=period_results,
                  period_trades=period_trades, today_sig=today_sig, last_close=last_close)

    # ── キャッシュ保存 ──────────────────────────────────────────
    if bt_cache is not None:
        bt_cache[cache_key] = (mtime, result)

    return result


def _passes_watchlist_filter(period_results):
    # 取引ありのピリオドのみ判定（取引0件のピリオドはスキップ）
    active = [(days, s) for days, s in period_results.items() if s["n"] >= WL_MIN_TRADES]
    if len(active) < WL_MIN_ACTIVE:
        return False  # 取引のあるピリオドが少なすぎる
    for days, s in active:
        if pd.isna(s["wr"]) or s["wr"] < WL_MIN_WR:
            return False
        pf = s["pf"]
        if pd.isna(pf) or (pf != float("inf") and pf < WL_MIN_PF):
            return False
    return True


def build_watchlist_html(candidates, periods):
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    scan_dt   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    periods_s = sorted(periods)
    period_headers    = "".join(f'<th colspan="4">{d}日</th>' for d in periods_s)
    period_subheaders = "".join('<th>回数</th><th>勝率</th><th>PF</th><th>損益(円)</th>' for _ in periods_s)

    rows = ""
    for c in candidates:
        pr  = c["period_results"]
        sig = c["today_sig"]
        sig_mark = "★" if sig else ""
        sym_id = c["symbol"].replace(".", "_").replace("-", "_")
        period_cells = ""
        for d in periods_s:
            s    = pr.get(d, {})
            n    = s.get("n", 0)
            wr   = s.get("wr", float("nan"))
            pf   = s.get("pf", float("nan"))
            wr_s = f"{wr:.0f}%" if not pd.isna(wr) else "—"
            pf_s = ("∞" if pf == float("inf") else f"{pf:.2f}") if not pd.isna(pf) else "—"
            wr_cls  = "pos" if (not pd.isna(wr) and wr >= WL_MIN_WR) else "neg"
            pf_cls  = "pos" if (pf == float("inf") or (not pd.isna(pf) and pf >= WL_MIN_PF)) else "neg"
            total   = s.get("total", 0.0)
            tot_cls = "pos" if total >= 0 else "neg"
            tot_s   = f"{total:+,.0f}" if n > 0 else "—"
            period_cells += f'<td>{n}</td><td class="{wr_cls}">{wr_s}</td><td class="{pf_cls}">{pf_s}</td><td class="{tot_cls}">{tot_s}</td>'
        cl_s = f'{sig["close"]:,.0f}' if sig else "—"
        lp_s = f'{sig["limit_price"]:,.0f}' if sig else "—"
        st_s = f'{sig["stop_loss"]:,.0f}' if sig else "—"
        rows += (
            f'<tr class="sym-row" onclick="toggleDetail(\'{sym_id}\')">'
            f'<td>▶\u00a0{sig_mark}{c["symbol"]}</td><td>{c["name"]}</td>'
            f'<td>{cl_s}</td><td class="pos">{lp_s}</td><td class="neg">{st_s}</td>'
            + period_cells + f'</tr>\n'
        )

        # Build inline detail row for this symbol
        pt = c.get("period_trades", {})
        sections = ""
        if pt:
            for d in periods_s:
                trades = pt.get(d, [])
                if not trades:
                    continue
                total_pnl = sum(t["pnl"] for t in trades)
                wins      = [t for t in trades if t["pnl"] > 0]
                tc_cls    = "pos" if total_pnl >= 0 else "neg"
                t_rows = ""
                for i, t in enumerate(trades, 1):
                    cls   = "win" if t["pnl"] > 0 else ("hold" if "保有中" in t["reason"] else "lose")
                    lp    = t.get("limit_price",   t["entry_p"])
                    sl    = t.get("stop_loss",     float("nan"))
                    tgt   = t.get("profit_target", float("nan"))
                    sl_s  = f'{sl:,.0f}'  if not pd.isna(sl)  else "—"
                    tgt_s = f'{tgt:,.0f}' if not pd.isna(tgt) else "—"
                    t_rows += (
                        f'<tr class="{cls}"><td>{i}</td>'
                        f'<td>{t["entry_dt"].strftime("%m/%d")}</td>'
                        f'<td>{t["exit_dt"].strftime("%m/%d")}</td>'
                        f'<td>{lp:,.0f}</td>'
                        f'<td>{t["entry_p"]:,.0f}</td>'
                        f'<td class="neg">{sl_s}</td>'
                        f'<td class="pos">{tgt_s}</td>'
                        f'<td>{t["exit_p"]:,.0f}</td>'
                        f'<td class="{"pos" if t["pct"]>=0 else "neg"}">{t["pct"]:+.1f}%</td>'
                        f'<td class="{"pos" if t["pnl"]>=0 else "neg"}">{t["pnl"]:+,.0f}円</td>'
                        f'<td>{t["hold"]}日</td>'
                        f'<td>{t["reason"]}</td></tr>\n'
                    )
                sections += f"""
<h3 style="margin:14px 0 6px;font-size:0.95em;color:#94a3b8">{d}日間
  <span style="color:#666;font-size:0.85em">
    {len(trades)}回 / 勝:{len(wins)} / 損益:<span class="{tc_cls}">{total_pnl:+,.0f}円</span>
  </span></h3>
<table><thead><tr>
  <th>#</th><th>IN</th><th>OUT</th>
  <th>指値</th><th>約定値</th><th>逆指値</th><th>利確目標</th><th>決済値</th>
  <th>損益%</th><th>損益(円)</th><th>保有</th><th>理由</th>
</tr></thead><tbody>{t_rows}</tbody></table>"""
        rows += (
            f'<tr id="d_{sym_id}" class="detail-row" style="display:none">'
            f'<td colspan="99"><div class="detail-inner">{sections}</div></td></tr>\n'
        )

    signal_count = sum(1 for c in candidates if c["today_sig"])

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>監視銘柄リスト（指値OCO） {today_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Helvetica Neue',Arial,sans-serif;background:#0f1117;color:#dde1ec;padding:24px;font-size:13px}}
h1{{font-size:1.35em;color:#fff;border-left:4px solid #38bdf8;padding-left:12px;margin-bottom:6px}}
.meta{{color:#555;font-size:0.8em;margin:2px 0 16px 16px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0 24px}}
.card{{background:#16192a;border:1px solid #252840;border-radius:10px;padding:12px 18px;min-width:120px}}
.clabel{{font-size:0.7em;color:#666}}.cval{{font-size:1.4em;font-weight:700;margin-top:2px}}
.criteria{{background:#131825;border:1px solid #1e3a5f;border-radius:8px;padding:12px 18px;margin:0 0 20px;font-size:0.85em;color:#7ab3d4}}
.criteria b{{color:#38bdf8}}
table{{width:100%;border-collapse:collapse;font-size:0.83em}}
th{{background:#16192a;color:#666;padding:7px 10px;text-align:right;border-bottom:2px solid #252840;white-space:nowrap}}
th:first-child,th:nth-child(2),th:nth-child(3),th:nth-child(4),th:nth-child(5){{text-align:left}}
th[colspan]{{text-align:center;border-left:1px solid #2a2f4a;color:#94a3b8}}
td{{padding:6px 10px;text-align:right;border-bottom:1px solid #1c1f30;white-space:nowrap}}
td:first-child,td:nth-child(2),td:nth-child(3),td:nth-child(4),td:nth-child(5){{text-align:left}}
tr:hover>td{{background:#1b1f35}}
.pos{{color:#4ade80}}.neg{{color:#f87171}}.neu{{color:#c8cfe8}}
.footer{{margin-top:28px;color:#333;font-size:0.75em;text-align:right}}
.sym-row{{cursor:pointer}}
.sym-row td:first-child{{user-select:none}}
.detail-row>td{{padding:0;background:#0d1020!important;border-bottom:2px solid #252840}}
.detail-inner{{padding:12px 20px}}
.detail-inner table{{width:auto;min-width:600px}}
.detail-inner th{{background:#0d1020}}
tr.win>td{{background:rgba(74,222,128,.05)}}
tr.lose>td{{background:rgba(248,113,113,.05)}}
tr.hold>td{{background:rgba(251,191,36,.06)}}
</style></head><body>
<h1>監視銘柄リスト — 指値OCO戦略</h1>
<div class="meta">スキャン: {scan_dt} ／ 期間: {', '.join(str(d)+'日' for d in periods_s)}</div>
<div class="criteria">選定基準（全期間で満たすこと）：<b>取引回数 ≥ {WL_MIN_TRADES}回</b> ／ <b>勝率 ≥ {WL_MIN_WR:.0f}%</b> ／ <b>PF ≥ {WL_MIN_PF}</b></div>
<div class="cards">
  <div class="card"><div class="clabel">候補銘柄数</div><div class="cval pos">{len(candidates)}</div></div>
  <div class="card"><div class="clabel">本日シグナル</div><div class="cval pos">{signal_count}</div></div>
</div>
<table><thead>
<tr><th rowspan="2">コード</th><th rowspan="2">銘柄名</th>
<th rowspan="2">終値</th><th rowspan="2">指値</th><th rowspan="2">損切り</th>
{period_headers}</tr>
<tr>{period_subheaders}</tr>
</thead><tbody>{rows}</tbody></table>
<div class="footer">★ = 本日シグナルあり</div>
<script>
function toggleDetail(id){{
  var r=document.getElementById('d_'+id);
  if(!r)return;
  var open=r.style.display==='table-row';
  r.style.display=open?'none':'table-row';
  var sym=r.previousElementSibling;
  if(sym){{
    var td=sym.querySelector('td');
    if(td) td.textContent=td.textContent.replace(/^[▶▼]\u00a0/,''+(open?'▶\u00a0':'▼\u00a0'));
  }}
}}
</script>
</body></html>"""
    out = Path(f"watchlist_limit_oco_{today_str}.html")
    out.write_text(html, encoding="utf-8")
    return out


# ── メイン ────────────────────────────────────────────────────────
def _load_symbols(universe: str | None) -> list[tuple[str, str]]:
    """ユニバース名に応じて銘柄リストを返す。
    watch : 監視銘柄 / 225 : 日経225 / all : 全上場銘柄
    """
    if universe == "watch":
        from symbols_watch_rsi2 import SYMBOLS as _W
        return list(_W)
    if universe == "all":
        _p = Path("symbols_listed_all.py")
        if _p.exists():
            import importlib.util
            _spec = importlib.util.spec_from_file_location("_listed_all", _p)
            _mod  = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            print(f"  銘柄ユニバース: 全上場銘柄 ({len(_mod.SYMBOLS)}銘柄)")
            return list(_mod.SYMBOLS)
        print("  ※ symbols_listed_all.py が見つかりません。日経225を使用します。")
    # "225" または None またはその他 → 日経225
    from symbols_all import SYMBOLS as _S
    print(f"  銘柄ユニバース: 日経225 ({len(_S)}銘柄)")
    return list(_S)


def _run_single(sym: str, name: str, backtest_days: int, optimize: bool) -> dict | None:
    """1銘柄のデータ取得・計算・バックテストを実行。"""
    raw = fetch(sym, backtest_days)
    if raw is None:
        return None
    df = calc(raw)
    if len(df) < 50:
        return None

    params = optimize_params(df, backtest_days) if optimize else DEFAULT_PARAMS
    trades = backtest_limit(df, backtest_days, params)
    signal = _has_signal_today(df, params)

    wins      = [t for t in trades if t["pnl"] > 0]
    losses    = [t for t in trades if t["pnl"] <= 0]
    wr        = len(wins) / len(trades) * 100 if trades else 0.0
    loss_sum  = abs(sum(t["pnl"] for t in losses))
    pf        = (sum(t["pnl"] for t in wins) / loss_sum
                 if loss_sum > 0 else float("inf"))

    return dict(
        symbol=sym, name=name,
        signal=signal,
        trades=trades,
        params=params,
        stats=dict(wr=wr, pf=pf, n=len(trades)) if trades else None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="指値OCO バックテスト & シグナルスキャン")
    parser.add_argument("--symbol",   help="個別銘柄コード（例: 7203.T）")
    parser.add_argument("--optimize", action="store_true", help="パラメーター最適化（グリッドサーチ）")
    parser.add_argument("--days",     type=int, default=BACKTEST_DAYS, help="バックテスト期間（日）")
    parser.add_argument("--universe", default="225", choices=["watch", "225", "all"],
                        help="銘柄ユニバース: watch（監視銘柄）/ 225（日経225）/ all（全上場銘柄）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    parser.add_argument("--watchlist", action="store_true", help="4期間(30/90/180/365日)バックテストで監視銘柄を選定")
    args = parser.parse_args()

    # --universe 225/all の場合、--symbol 未指定なら自動で4期間バックテストモードへ
    if not args.watchlist and args.universe in ("225", "all") and not args.symbol:
        args.watchlist = True

    if args.watchlist:
        symbols = _load_symbols(args.universe)
        periods = [p for p in WATCHLIST_PERIODS if p <= args.days] or [args.days]
        bt_cache = _load_bt_cache()
        cache_key_sample = (symbols[0][0] if symbols else "", tuple(sorted(periods)))
        cached_count = sum(1 for sym, _ in symbols if (sym, tuple(sorted(periods)), LOT_SIZE) in bt_cache)
        print(f"\nバックテスト実行: {len(symbols)}銘柄 / 期間:{periods}日")
        if cached_count:
            print(f"  キャッシュ: {cached_count}銘柄（株価更新なし → スキップ）")
        all_results = []
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_process_symbol_multiperiod, sym, name, periods, bt_cache): (sym, name)
                    for sym, name in symbols}
            for fut in as_completed(futs):
                done += 1
                r = fut.result()
                if r:
                    all_results.append(r)
                if done % 20 == 0 or done == len(symbols):
                    print(f"  {done}/{len(symbols)} 完了", end="\r", flush=True)
        _save_bt_cache(bt_cache)
        print()
        # フィルターなし・全銘柄を利益順にソート
        candidates = [r for r in all_results if any(s["n"] > 0 for s in r["period_results"].values())]
        candidates.sort(key=lambda r: -sum(s.get("total", 0) for s in r["period_results"].values()))
        print(f"\nスキャン結果（利益順）: {len(candidates)}銘柄")
        print(f"  {'':2} {'コード':<10} {'銘柄名':<22} " + "  ".join(f"{d}日" .ljust(28) for d in sorted(periods)))
        print(f"  {'':2} {'':10} {'':22} " + "  ".join("勝率   PF    損益(円)".ljust(28) for _ in periods))
        print("  " + "─" * 120)
        for c in candidates:
            sig  = c["today_sig"]
            mark = "★" if sig else "  "
            pr   = c["period_results"]
            stats_str = "  ".join(
                f"{pr[d]['wr']:.0f}%  {pr[d]['pf']:.1f}  {pr[d]['total']:>+10,.0f}円" if pr[d]['n'] > 0 else f"{'—':<28}"
                for d in sorted(periods)
            )
            print(f"  {mark} {c['symbol']:<10} {c['name']:<22}  {stats_str}")
        path = build_watchlist_html(candidates, periods)
        print(f"\nHTML: {path.resolve()}")
        # CSV出力（run_ranking.py用）
        import csv as _csv
        _csv_path = Path(f"candidates_limit_oco.csv")
        with open(_csv_path, "w", newline="", encoding="utf-8") as _f:
            _w = _csv.writer(_f)
            _w.writerow(["symbol","name","last_close",
                         "30_n","30_wr","30_pf","30_total",
                         "90_n","90_wr","90_pf","90_total",
                         "180_n","180_wr","180_pf","180_total",
                         "365_n","365_wr","365_pf","365_total"])
            for _c in candidates:
                _pr = _c["period_results"]
                _row = [_c["symbol"], _c["name"], _c.get("last_close", 0)]
                for _d in [30, 90, 180, 365]:
                    _s = _pr.get(_d, {})
                    _pf = _s.get("pf", 0) or 0
                    _row += [_s.get("n", 0),
                             round(_s.get("wr", 0) or 0, 1),
                             round(0 if _pf == float("inf") else _pf, 4),
                             round(_s.get("total", 0) or 0, 0)]
                _w.writerow(_row)
        print(f"CSV: {_csv_path.resolve()}")
        if not args.no_browser:
            webbrowser.open(f"file://{path.resolve()}")
        return

    backtest_days = args.days
    scan_dt_str   = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    # ── 個別銘柄モード ────────────────────────────────────────────
    if args.symbol:
        sym = args.symbol
        raw = fetch(sym, backtest_days)
        if raw is None:
            print(f"データ取得失敗: {sym}")
            return
        df     = calc(raw)
        params = optimize_params(df, backtest_days) if args.optimize else DEFAULT_PARAMS
        trades = backtest_limit(df, backtest_days, params)
        signal = _has_signal_today(df, params)

        print(f"\n{'═'*60}")
        print(f"  指値OCO [{sym}]  直近{backtest_days}日")
        print(f"{'─'*60}")
        print(f"  パラメーター: {params}")
        if signal:
            print(f"\n  ★ 本日シグナルあり")
            print(f"     終値:          {signal['close']:,.0f}")
            print(f"     RSI(2):        {signal['rsi2']:.1f}")
            print(f"     IBS:           {signal['ibs']:.2f}")
            print(f"     買い指値:      {signal['limit_price']:,.0f}  （有効{signal['expire_days']}日）")
            print(f"     利確指値:      {signal['profit_target']:,.0f}")
            print(f"     損切り逆指値:  {signal['stop_loss']:,.0f}")
        else:
            print("\n  本日シグナルなし")

        if trades:
            wins  = [t for t in trades if t["pnl"] > 0]
            loss  = [t for t in trades if t["pnl"] <= 0]
            total = sum(t["pnl"] for t in trades)
            wr    = len(wins) / len(trades) * 100
            ls    = abs(sum(t["pnl"] for t in loss))
            pf    = sum(t["pnl"] for t in wins) / ls if ls > 0 else float("inf")
            print(f"\n  バックテスト: {len(trades)}回  勝率{wr:.0f}%  "
                  f"PF{'∞' if pf==float('inf') else f'{pf:.2f}'}  "
                  f"合計{total:+,.0f}円")
            print(f"\n  {'#':<3} {'エントリー':>10} {'決済':>10} "
                  f"{'指値':>8} {'約定':>8} {'決済値':>8} {'損益%':>7} {'保有':>5} 理由")
            print("  " + "─" * 75)
            for i, t in enumerate(trades, 1):
                cls  = "+" if t["pnl"] > 0 else ""
                mark = "★" if "保有中" in t["reason"] else " "
                lp   = t.get("limit_price", t["entry_p"])
                print(f"  {mark}{i:<3} "
                      f"{t['entry_dt'].strftime('%Y-%m-%d'):>10} "
                      f"{t['exit_dt'].strftime('%Y-%m-%d'):>10} "
                      f"{lp:>8,.0f} {t['entry_p']:>8,.0f} {t['exit_p']:>8,.0f} "
                      f"{t['pct']:>+7.1f}% {t['hold']:>4}日 {t['reason']}")
        else:
            print("  トレードなし")
        print()
        return

    # ── 全銘柄スキャンモード ─────────────────────────────────────
    symbols = _load_symbols(args.universe)
    total   = len(symbols)
    print(f"\n[指値OCO] シグナルスキャン中... ({total}銘柄)")
    if args.optimize:
        print("  ※ 最適化モード（グリッドサーチ）: 時間がかかります")

    results: list[dict] = []
    done = 0

    def _task(item):
        sym, name = item
        return _run_single(sym, name, backtest_days, args.optimize)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_task, item): item for item in symbols}
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            print(f"  {done}/{total}", end="\r", flush=True)
    print()

    signals = [r for r in results if r["signal"] is not None]
    signals.sort(key=lambda r: r["signal"]["rsi2"])   # RSI2 低い順

    print(f"\n  本日シグナル: {len(signals)}件")
    for s in signals:
        sig = s["signal"]
        print(f"    {s['symbol']:8s} {s['name'][:12]:<12}  "
              f"終値{sig['close']:>8,.0f}  RSI2={sig['rsi2']:.1f}  "
              f"指値={sig['limit_price']:,.0f}  "
              f"利確={sig['profit_target']:,.0f}  "
              f"損切={sig['stop_loss']:,.0f}")

    html_str  = build_html(signals, scan_dt_str, backtest_days)
    out_path  = Path(f"report_limit_oco_{_TODAY}.html")
    out_path.write_text(html_str, encoding="utf-8")
    print(f"\n  レポート出力: {out_path}")

    if not args.no_browser:
        webbrowser.open(str(out_path.resolve()))


if __name__ == "__main__":
    main()
