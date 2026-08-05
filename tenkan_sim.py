"""tenkan_sim.py — lss未約定 → ロング転換 の5分足シミュレーション(共通実装)。

売買ルール:
  買い: BUY_TIME(09:09) 以降の最初バーの OPEN
        1分足なら09:09、5分足なら09:10のバー → 厳密一致は不要
  売り: SELL_TIME(11:30) 以前の最後バーの OPEN (前場引け相当)
        11:30バーがあればそれ、なければ最終前場バー(11:25等)
  スリッページ 0.05% / 手数料 片道 0.1% / 100株固定

利用側:
  nikkei_analysis.py      レポートの転換タブ(未約定シグナルから毎日自動生成)
  tenkan_today.py         指定日の転換をその場で計算するCLI
  run_signals_holdout_all.py  OOS CSV由来の転換(過去フォールド分)
"""
from __future__ import annotations

import os
import pickle
from datetime import date, time
from pathlib import Path

BUY_TIME = time(9, 9)
SELL_TIME = time(11, 30)
SLIP = 0.0005
FEE = 0.001
QTY = 100

_DIR5: "Path | None" = None
_DIR1: "Path | None" = None
_DIRS_READY = False
_CACHE: dict = {}


def find_minute_dirs() -> "tuple[Path | None, Path | None]":
    """(5分足DIR, 1分足DIR) を返す。環境変数 → 既定パスの順で自動検出。"""
    global _DIR5, _DIR1, _DIRS_READY
    if _DIRS_READY:
        return _DIR5, _DIR1

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
        for c in [here / "data" / "stock_5min",
                  here.parent / "stock_5min",
                  here.parent / "stock_5min" / "data" / "stock_5min"]:
            try:
                if c.exists() and any(c.glob("*.pkl")):
                    d5 = c
                    break
            except Exception:
                pass

    _DIR5, _DIR1, _DIRS_READY = d5, d1, True
    return _DIR5, _DIR1


def _load_pkl(path: "Path"):
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
        df.columns = [c.lower() for c in df.columns]
        try:
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize("Asia/Tokyo")
            else:
                df.index = df.index.tz_convert("Asia/Tokyo")
        except Exception:
            pass
        return df
    except Exception:
        return None


def bars(symbol: str):
    """銘柄の分足DataFrame(1分足優先→5分足)。プロセス内キャッシュ。"""
    if symbol in _CACHE:
        return _CACHE[symbol]
    d5, d1 = find_minute_dirs()
    code = symbol.upper().replace(".T", "") + "0"
    df = _load_pkl(d1 / f"{code}_1m.pkl") if d1 else None
    if df is None and d5:
        df = _load_pkl(d5 / f"{code}.pkl")
    _CACHE[symbol] = df
    return df


def simulate(symbol: str, d: "date") -> "dict | None":
    """指定日の転換結果。約定不能・データ無しなら None。

    返り値: {"pnl", "buy_p", "sell_p", "buy_t", "sell_t"}
    """
    df = bars(symbol)
    if df is None:
        return None
    try:
        day = df[df.index.date == d]
    except Exception:
        return None
    if len(day) < 3:
        return None

    times = [t.time() for t in day.index]

    buy_p = buy_t = None
    for i, t in enumerate(times):
        if t >= BUY_TIME:
            buy_p = float(day.iloc[i]["open"])
            buy_t = t.strftime("%H:%M")
            break

    sell_p = sell_t = None
    for i in range(len(times) - 1, -1, -1):
        if times[i] <= SELL_TIME:
            sell_p = float(day.iloc[i]["open"])
            sell_t = times[i].strftime("%H:%M")
            break

    if not buy_p or not sell_p:
        return None

    b = buy_p * (1 + SLIP)
    s = sell_p * (1 - SLIP)
    pnl = (s - b) * QTY - (b + s) * QTY * FEE
    return {"pnl": pnl, "buy_p": b, "sell_p": s, "buy_t": buy_t, "sell_t": sell_t}


def release_cache() -> None:
    """読み込んだ分足DataFrameを破棄してメモリを返す。
    転換の生成が終わった後に呼ぶ(銘柄詳細タブなど後続処理のメモリを空けるため)。"""
    _CACHE.clear()


def _rank(score: float) -> str:
    if score >= 80:
        return "★★★"
    if score >= 60:
        return "★★"
    if score >= 40:
        return "★"
    return "△"


def make_trade(symbol: str, name: str, d: "date", res: dict,
               score: float = 0.0) -> dict:
    """レポートの取引明細に混ぜられる trade dict を作る。"""
    ds = d.strftime("%m/%d")
    return {
        "label": "転換ロング",
        "color": "#60a5fa",
        "symbol": symbol,
        "name": name,
        "strategy": "転換",
        "score": score,
        "rank": _rank(score),
        "is_wf": False,
        "wf_score": 0,
        "rec_score": score,
        "preoos_score": score,
        "signal_dt_raw": d,
        "bt_type": "",
        "entry_d_raw": d,
        "exit_d_raw": d,
        "entry_time": res.get("buy_t") or "09:09",
        "pnl": res["pnl"],
        "reason": "転換決済",
        "entry_dt": ds,
        "exit_dt": ds,
        "entry_p": res["buy_p"],
        "exit_p": res["sell_p"],
        "qty": QTY,
        "hold_days": 0,
        "days_neg": 0,
        "days_to_fill": 0,
        "order_limit": 0,
        "order_stop": 0,
        "order_target": 0,
    }


def build_from_nofills(nofills, min_price: float = 0.0, max_price: float = 0.0,
                       exclude_keys=None, verbose: bool = True) -> list[dict]:
    """未約定シグナル(all_nofills 相当)から転換トレードを生成する。

    nofills の各要素に必要なキー: symbol / name / entry_d_raw(または exit_d_raw)
                                  / order_limit または entry_p / rec_score
    exclude_keys: 既に生成済みの (symbol, str(date)) 集合。重複を避ける。
    """
    out: list[dict] = []
    excl = exclude_keys or set()
    seen: set = set()
    n_price = n_nodata = n_dup = 0

    for t in nofills or []:
        sym = str(t.get("symbol") or "")
        d = t.get("entry_d_raw") or t.get("exit_d_raw")
        if not sym or not d:
            continue
        if hasattr(d, "date"):
            d = d.date()
        key = (sym, str(d))
        if key in excl or key in seen:
            n_dup += 1
            continue

        ep = float(t.get("order_limit", 0) or 0) or float(t.get("entry_p", 0) or 0)
        if ep > 0:
            if min_price > 0 and ep < min_price:
                n_price += 1
                continue
            if max_price > 0 and ep > max_price:
                n_price += 1
                continue

        res = simulate(sym, d)
        if res is None:
            n_nodata += 1
            continue

        seen.add(key)
        out.append(make_trade(sym, str(t.get("name") or ""), d, res,
                              float(t.get("rec_score", 0) or 0)))

    if verbose:
        print(f"[転換/未約定] 生成 {len(out)}件 "
              f"(価格範囲外 {n_price}件 / 分足なし {n_nodata}件 / 重複 {n_dup}件)",
              flush=True)
    return out
