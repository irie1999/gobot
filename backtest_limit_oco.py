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
POSITION_SIZE = 100_000   # 1回あたり投資金額（円）
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
                qty           = max(int(POSITION_SIZE / entry_p), 1)
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
        lp    = t.get("limit_price", t["entry_p"])
        rows += (
            f'<tr><td>{mark}{i}</td>'
            f'<td>{t["entry_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{t["exit_dt"].strftime("%Y-%m-%d")}</td>'
            f'<td>{lp:,.0f}</td>'
            f'<td>{t["entry_p"]:,.0f}</td>'
            f'<td>{t["exit_p"]:,.0f}</td>'
            f'<td class="{cls}">{t["pct"]:+.1f}%</td>'
            f'<td>{t["hold"]}日</td>'
            f'<td>{t["reason"]}</td></tr>\n'
        )
    header = (
        "<tr><th>#</th><th>エントリー日</th><th>決済日</th>"
        "<th>指値</th><th>約定値</th><th>決済値</th>"
        "<th>損益%</th><th>保有</th><th>理由</th></tr>"
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


# ── メイン ────────────────────────────────────────────────────────
def _load_symbols(universe: str | None) -> list[tuple[str, str]]:
    import importlib.util
    if universe is None:
        for candidate in ["symbols_listed_prime.py",
                          "symbols_listed_standard.py",
                          "symbols_listed_all.py"]:
            p = Path(candidate)
            if p.exists():
                spec = importlib.util.spec_from_file_location("_listed", p)
                mod  = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                print(f"  銘柄ユニバース: {candidate} ({len(mod.SYMBOLS)}銘柄)")
                return mod.SYMBOLS
    from symbols_all import SYMBOLS as _S
    print(f"  銘柄ユニバース: 日経225 ({len(_S)}銘柄)")
    return _S


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
    parser.add_argument("--universe", default=None, help="225 / prime / standard / all")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    args = parser.parse_args()

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
