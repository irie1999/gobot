"""replay_trainer.py — 手持ちの分足でデイトレを「手で」練習するリプレイツール。

⚠⚠ 未完成 (WIP)。**まだ動きません。**
    実装済み : データ解決 / pkl ローダー / 出題の組み立て / 約定判定 / 集計レポート (--report)
    未実装   : ローカルHTTPサーバー / ブラウザUI / main() / argparse
    2026-08-21、練習の優先順位が「過去足リプレイ」から「kabu API の板PUSH を使った
    リアルタイム紙トレード」に変わったため、ここで中断している (DAYTRADE.md 参照)。
    再開する場合は下の設計メモの通りサーバーを足すこと。

コードに売買させない。あなたが1本ずつ足を送り、自分で建てて、自分で決済する。
未来の足はサーバ側に留めてブラウザへ送らないので、覗き見はできない。

使い方:
  python replay_trainer.py                       # ランダム出題(5分足)をブラウザで開く
  python replay_trainer.py --symbol 7203.T       # 銘柄を固定してランダムな日
  python replay_trainer.py --date 2026-03-14     # 日を固定
  python replay_trainer.py --interval 1          # 1分足で練習 (既定は5分足)
  python replay_trainer.py --demo                # データが無い環境でも動く合成足
  python replay_trainer.py --report              # 練習ログの集計を表示
  python replay_trainer.py --report --days 30    # 直近30日ぶんだけ集計

データの場所 (CLAUDE.md「★ データ場所メモ」と同じ解決):
  5分足 : 環境変数 MINUTE_5M_DIR → 隣接 stock_5min      (<コード>0.pkl)
  1分足 : 環境変数 MINUTE_1M_DIR → ~/.jquants_cache/minute (<コード>0_1m.pkl)

練習ログ:
  replay_practice_log.csv に1トレード1行で追記される。--report で集計。

⚠ 測れないこと (CLAUDE.md §1.3):
  足の中の高安の順序は分からないので、同一足内で完結する売買は再現できない。
  板・歩み値も存在しないので、約定できるか/スプレッドは練習に入らない。
  同一足で損切と利確の両方に触れた場合は **損切り優先(悲観)** で判定する。
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import pickle
import random
import socketserver
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

LOG_PATH = Path(__file__).resolve().parent / "replay_practice_log.csv"
SESSION_END = "15:25"     # ここまでに決済しなければタイムカット
ATR_PERIOD = 14
DEFAULT_QTY = 100

LOG_COLUMNS = [
    "practiced_at", "symbol", "trade_date", "interval", "side",
    "entry_time", "entry_px", "stop_px", "target_px",
    "exit_time", "exit_px", "exit_reason", "qty", "pnl",
    "r_multiple", "atr_at_entry", "bars_held", "pattern", "rule_ok", "note",
]


# ────────────────────────────────────────────────────────────
# データ解決 (daytrade_data / tenkan_sim と同じ規約。単体でも動くよう自前実装)
# ────────────────────────────────────────────────────────────

def find_minute_dirs() -> tuple[Path | None, Path | None]:
    """(5分足DIR, 1分足DIR) を返す。環境変数 → 既定パスの順。"""
    d1 = None
    env1 = os.environ.get("MINUTE_1M_DIR", "").strip()
    if env1:
        d1 = Path(env1)
    else:
        p = Path.home() / ".jquants_cache" / "minute"
        if p.exists():
            d1 = p

    d5 = None
    env5 = os.environ.get("MINUTE_5M_DIR", "").strip()
    if env5:
        d5 = Path(env5)
    if d5 is None or not d5.exists():
        here = Path(__file__).resolve().parent
        for c in [here / "data" / "minute_5m",
                  here.parent / "stock_5min",
                  here.parent / "stock_5min" / "data" / "minute_5m",
                  here / "data" / "stock_5min"]:
            try:
                if c.exists() and any(c.glob("*.pkl")):
                    d5 = c
                    break
            except Exception:
                pass
    return d5, d1


def _code(symbol: str) -> str:
    """7203.T → 72030"""
    c = symbol.strip().upper().replace(".T", "")
    return c + "0" if len(c) == 4 else c


def load_bars(symbol: str, interval: int) -> pd.DataFrame | None:
    """pkl を読んで [open, high, low, close, volume] / tz-naive JST index に正規化。"""
    d5, d1 = find_minute_dirs()
    if interval == 1:
        path = (d1 / f"{_code(symbol)}_1m.pkl") if d1 else None
    else:
        path = (d5 / f"{_code(symbol)}.pkl") if d5 else None
    if path is None or not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    need = ["open", "high", "low", "close"]
    if not all(c in df.columns for c in need):
        return None
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    try:
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Asia/Tokyo").tz_localize(None)
    except Exception:
        pass
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df.sort_index()


def available_symbols(interval: int) -> list[str]:
    d5, d1 = find_minute_dirs()
    out: list[str] = []
    if interval == 1 and d1 and d1.exists():
        for p in d1.glob("*_1m.pkl"):
            code = p.name.replace("_1m.pkl", "")
            if len(code) == 5 and code.endswith("0"):
                out.append(code[:4] + ".T")
    elif d5 and d5.exists():
        for p in d5.glob("*.pkl"):
            code = p.stem
            if len(code) == 5 and code.endswith("0"):
                out.append(code[:4] + ".T")
    return sorted(set(out))


def make_demo_bars(seed: int = 0) -> pd.DataFrame:
    """データが無い環境でも動作確認できる合成足 (2日ぶんの5分足)。"""
    rng = np.random.default_rng(seed)
    rows, px = [], 2000.0
    base = datetime(2026, 3, 13, 9, 0)
    for day in range(2):
        d0 = base + timedelta(days=day)
        px *= 1 + rng.normal(0, 0.004)
        for i in range(61):                       # 09:00〜15:00 の5分足
            t = d0 + timedelta(minutes=5 * i)
            if 11 * 60 + 30 <= t.hour * 60 + t.minute < 12 * 60 + 30:
                continue                          # 昼休み
            drift = rng.normal(0, 0.0016)
            o = px
            c = o * (1 + drift)
            h = max(o, c) * (1 + abs(rng.normal(0, 0.0009)))
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.0009)))
            rows.append((t, o, h, lo, c, float(rng.integers(3000, 60000))))
            px = c
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close", "volume"])
    return df.set_index("dt")


# ────────────────────────────────────────────────────────────
# 出題の組み立て
# ────────────────────────────────────────────────────────────

def atr_series(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


class Question:
    """1銘柄 × 1営業日ぶんの出題。未来の足はここに留めてブラウザへ渡さない。"""

    def __init__(self, symbol: str, interval: int, df: pd.DataFrame, trade_date):
        self.symbol = symbol
        self.interval = interval
        self.trade_date = pd.Timestamp(trade_date).date()

        atr = atr_series(df)
        day_mask = df.index.normalize() == pd.Timestamp(self.trade_date)
        self.day = df.loc[day_mask]
        prior = df.loc[df.index.normalize() < pd.Timestamp(self.trade_date)]

        self.prev_close = float(prior["close"].iloc[-1]) if len(prior) else float(self.day["open"].iloc[0])
        prev_day = prior.loc[prior.index.normalize() == prior.index.normalize().max()] if len(prior) else prior
        self.prev_high = float(prev_day["high"].max()) if len(prev_day) else self.prev_close
        self.prev_low = float(prev_day["low"].min()) if len(prev_day) else self.prev_close
        self.atr = atr.reindex(self.day.index).ffill().fillna(atr.iloc[-1] if len(atr) else 0.0)

        self.n = len(self.day)
        self.i = -1                                # 直近に開示したバーの添字
        # VWAP は開示済みバーだけで逐次計算する (未来を含めない)
        self._cum_pv = 0.0
        self._cum_v = 0.0

    def meta(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "trade_date": str(self.trade_date),
            "prev_close": round(self.prev_close, 2),
            "prev_high": round(self.prev_high, 2),
            "prev_low": round(self.prev_low, 2),
            "total_bars": self.n,
            "session_end": SESSION_END,
        }

    def reveal(self) -> dict | None:
        """次の1本を開示する。終わりなら None。"""
        if self.i + 1 >= self.n:
            return None
        self.i += 1
        ts = self.day.index[self.i]
        row = self.day.iloc[self.i]
        typ = (row["high"] + row["low"] + row["close"]) / 3.0
        self._cum_pv += typ * max(row["volume"], 0.0)
        self._cum_v += max(row["volume"], 0.0)
        vwap = (self._cum_pv / self._cum_v) if self._cum_v > 0 else float(row["close"])
        return {
            "i": self.i,
            "t": ts.strftime("%H:%M"),
            "o": round(float(row["open"]), 2),
            "h": round(float(row["high"]), 2),
            "l": round(float(row["low"]), 2),
            "c": round(float(row["close"]), 2),
            "v": float(row["volume"]),
            "vwap": round(vwap, 2),
            "atr": round(float(self.atr.iloc[self.i]), 2),
            "last": self.i + 1 >= self.n,
            "past_end": ts.strftime("%H:%M") >= SESSION_END,
        }

    def bar(self, i: int) -> pd.Series:
        return self.day.iloc[i]

    def time_at(self, i: int) -> str:
        return self.day.index[i].strftime("%H:%M")


def build_question(symbol: str | None, interval: int, date: str | None,
                   days: int, demo: bool) -> Question:
    if demo:
        df = make_demo_bars(random.randrange(10_000))
        sym = symbol or "DEMO.T"
        dates = sorted(set(df.index.normalize()))
        return Question(sym, interval, df, dates[-1])

    syms = [symbol] if symbol else available_symbols(interval)
    if not syms:
        raise SystemExit(
            "分足データが見つかりません。\n"
            "  5分足: 環境変数 MINUTE_5M_DIR か 隣接 stock_5min フォルダ\n"
            "  1分足: 環境変数 MINUTE_1M_DIR か ~/.jquants_cache/minute\n"
            "動作確認だけなら --demo を付けてください。"
        )
    random.shuffle(syms)
    for sym in syms[:60]:
        df = load_bars(sym, interval)
        if df is None or len(df) < 200:
            continue
        dates = sorted({d.date() for d in df.index.normalize()})
        if len(dates) < 2:
            continue
        pool = dates[1:]                            # 初日は前日が無いので除外
        if days > 0:
            pool = pool[-days:]
        if date:
            want = pd.Timestamp(date).date()
            if want not in pool:
                continue
            pick = want
        else:
            pick = random.choice(pool)
        q = Question(sym, interval, df, pick)
        if q.n >= 20:
            return q
    raise SystemExit("条件に合う銘柄×日が見つかりませんでした。--symbol / --date / --days を見直してください。")


# ────────────────────────────────────────────────────────────
# 約定判定 (悲観側。同一バーで損切と利確の両方に触れたら損切り)
# ────────────────────────────────────────────────────────────

def check_exit(side: str, bar: pd.Series, stop: float, target: float) -> tuple[float, str] | None:
    hi, lo = float(bar["high"]), float(bar["low"])
    if side == "long":
        hit_stop = stop is not None and lo <= stop
        hit_tgt = target is not None and hi >= target
    else:
        hit_stop = stop is not None and hi >= stop
        hit_tgt = target is not None and lo <= target
    if hit_stop:
        return stop, "stop"                          # 両方なら損切り優先
    if hit_tgt:
        return target, "target"
    return None


def append_log(row: dict) -> None:
    df = pd.DataFrame([{c: row.get(c, "") for c in LOG_COLUMNS}])
    header = not LOG_PATH.exists()
    df.to_csv(LOG_PATH, mode="a", header=header, index=False, encoding="utf-8-sig")


# ────────────────────────────────────────────────────────────
# 集計レポート
# ────────────────────────────────────────────────────────────

def report(days: int) -> None:
    if not LOG_PATH.exists():
        print("まだ練習ログがありません。先に練習してください。")
        return
    df = pd.read_csv(LOG_PATH, encoding="utf-8-sig")
    df = df[df["exit_reason"].notna() & (df["exit_reason"] != "")]
    if days > 0:
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
        df = df[pd.to_datetime(df["practiced_at"], errors="coerce") >= cutoff]
    if df.empty:
        print("対象期間に決済済みのトレードがありません。")
        return

    df["r_multiple"] = pd.to_numeric(df["r_multiple"], errors="coerce")
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    df["bars_held"] = pd.to_numeric(df["bars_held"], errors="coerce")

    def block(name: str, g: pd.DataFrame) -> str:
        n = len(g)
        wins = g[g["pnl"] > 0]
        losses = g[g["pnl"] <= 0]
        wr = len(wins) / n * 100
        gp, gl = wins["pnl"].sum(), -losses["pnl"].sum()
        pf = (gp / gl) if gl > 0 else float("inf")
        exp_r = g["r_multiple"].mean()
        return (f"{name:<14} {n:>4}件  勝率{wr:5.1f}%  "
                f"PF{pf:6.2f}  期待値{exp_r:+6.2f}R  "
                f"損益{g['pnl'].sum():+11,.0f}円  平均{g['bars_held'].mean():4.1f}本")

    print(f"\n=== 練習レポート ({len(df)}件) ===\n")
    print(block("全体", df))

    # 最重要指標: 損切りを外さなかったか
    ok = df["rule_ok"].astype(str).str.upper().isin(["Y", "YES", "TRUE", "1"])
    print(f"\n★ ルール遵守率  {ok.sum()}/{len(df)} = {ok.mean() * 100:.1f}%"
          f"   {'← ここが100%でない限り他の数字は信用できない' if ok.mean() < 1 else ''}")
    if (~ok).any():
        broke = df[~ok]
        print(f"  ルールを外した {len(broke)}件の損益: {broke['pnl'].sum():+,.0f}円 "
              f"(期待値 {broke['r_multiple'].mean():+.2f}R)")
        print(f"  守った       {ok.sum()}件の損益: {df[ok]['pnl'].sum():+,.0f}円 "
              f"(期待値 {df[ok]['r_multiple'].mean():+.2f}R)")

    for key, label in [("pattern", "型別"), ("side", "方向別")]:
        sub = df[df[key].astype(str).str.len() > 0]
        if sub.empty:
            continue
        print(f"\n--- {label} ---")
        for name, g in sorted(sub.groupby(sub[key].astype(str)), key=lambda kv: -len(kv[1])):
            if len(g) >= 1:
                print(block(str(name)[:14], g))

    print("\n--- 時間帯別 (エントリー時刻) ---")
    hh = pd.to_datetime(df["entry_time"], format="%H:%M", errors="coerce").dt.hour
    for h, g in df.groupby(hh):
        if pd.notna(h):
            print(block(f"{int(h):02d}時台", g))

    print("\n--- 決済理由 ---")
    for name, g in df.groupby("exit_reason"):
        print(block(str(name), g))
    print()
