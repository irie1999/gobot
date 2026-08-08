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
import threading
from datetime import date, time
from pathlib import Path

def _env_time(name: str, default: time) -> time:
    """HH:MM の環境変数を time に。壊れていれば既定に落とす。"""
    import os
    v = str(os.environ.get(name, "") or "").strip()
    if not v:
        return default
    try:
        h, m = (int(x) for x in v.split(":")[:2])
        return time(h, m)
    except Exception:
        return default


# 買い/売り時刻は env で動かせる。既定は現行(09:09買い→11:30売り)。
#   ⛔ 09:09 買いは『不約定が確定する前』に入るので、成績を後から集計すると
#      look-ahead になる (CLAUDE.md 18.5.3)。実装可能な唯一の締切 09:05 は
#      純lss比 −272万。**現行の転換は実績記録用であって発注ルールではない。**
#   → 18.5.3 が唯一「未検証」と書いた案 = **エントリーを遅らせる**
#      (11:30買い→引け売り / 後場寄り買い→引け売り)。不約定がほぼ確定してから
#      入るので look-ahead をほぼ消せる。それを試せるように env 化した。
#        set TENKAN_BUY_TIME=11:30 & set TENKAN_SELL_TIME=15:25
BUY_TIME = _env_time("TENKAN_BUY_TIME", time(9, 9))
SELL_TIME = _env_time("TENKAN_SELL_TIME", time(11, 30))
SLIP = 0.0005
# 手数料は実口座に合わせて既定0(信用大口優遇プランは無料)。CLAUDE.md 18.14 で
# backtest_limit_entry.FEE_PCT_ONE_WAY を 0 にしたのと同じ理由。
# 旧既定(片道0.1%)で比較したいときだけ set LSS_FEE_ONE_WAY=0.001。
FEE = float(__import__("os").environ.get("LSS_FEE_ONE_WAY", "0") or "0")
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


# pickle.load はスレッド並列で走らせると Python 3.14 で
#   SystemError: deallocated bytearray object has exported buffers
# を出すことがある。読み込みだけ直列化する(所要時間は支配的でない)。
_LOAD_LOCK = threading.Lock()


def _load_pkl(path: "Path"):
    if not path.exists():
        return None
    try:
        with _LOAD_LOCK:
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


# キャッシュ上限(銘柄数)。分足DataFrameは1銘柄で数十MBになることがあり、
# 数百銘柄ぶん抱えると Python 3.14 で
#   SystemError: deallocated bytearray object has exported buffers
# や MemoryError が出る。古いものから捨てて上限を守る。
CACHE_MAX_SYMBOLS = 24
_CACHE_ORDER: list = []


def _cache_put(ck, df) -> None:
    _CACHE[ck] = df
    _CACHE_ORDER.append(ck)
    while len(_CACHE_ORDER) > CACHE_MAX_SYMBOLS * 2:   # 1銘柄あたり最大2件(1m/5m)
        old = _CACHE_ORDER.pop(0)
        _CACHE.pop(old, None)


def _bars_one(symbol: str, minute: int):
    """minute=1 なら1分足、5 なら5分足。プロセス内キャッシュ(件数上限あり)。"""
    ck = (symbol, minute)
    if ck in _CACHE:
        return _CACHE[ck]
    d5, d1 = find_minute_dirs()
    code = symbol.upper().replace(".T", "") + "0"
    if minute == 1:
        df = _load_pkl(d1 / f"{code}_1m.pkl") if d1 else None
    else:
        df = _load_pkl(d5 / f"{code}.pkl") if d5 else None
    _cache_put(ck, df)
    return df


def drop_symbol(symbol: str) -> None:
    """指定銘柄の分足をキャッシュから捨てる(処理が終わった銘柄を明示的に解放する用)。"""
    for m in (1, 5):
        _CACHE.pop((symbol, m), None)
        try:
            _CACHE_ORDER.remove((symbol, m))
        except ValueError:
            pass


def bars(symbol: str):
    """互換用: 1分足があればそれ、無ければ5分足。日付の有無は見ない。
    ※ DataFrame は真偽値評価できないので `A or B` は使えない(ValueError)。"""
    df = _bars_one(symbol, 1)
    if df is None:
        df = _bars_one(symbol, 5)
    return df


def has_any_bars(symbol: str) -> bool:
    """1分足・5分足のどちらかにデータがあるか。"""
    return _bars_one(symbol, 1) is not None or _bars_one(symbol, 5) is not None


def _day_bars(symbol: str, d: "date"):
    """指定日のバーを返す。1分足を優先するが、その日が入っていなければ5分足へ落ちる。

    ※ ここが日付単位のフォールバックになっていないと、1分足キャッシュ
      (~/.jquants_cache/minute)が古いまま存在する銘柄で「分足なし」と誤判定される。
      実測: 2026-08 の未約定9件が全て『分足なし』で落ちていた(5分足には入っていた)。
    """
    for m in (1, 5):
        df = _bars_one(symbol, m)
        if df is None:
            continue
        try:
            day = df[df.index.date == d]
        except Exception:
            continue
        if len(day) >= 3:
            return day
    return None


def simulate_bars(day) -> "dict | None":
    """既に読み込み済みの『その日の分足DataFrame』から転換結果を計算する。

    呼び出し側が daytrade_data.load_intraday / split_by_day で既に分足を持っている
    場合はこちらを使うこと。simulate() を使うと同じpklをもう一度読むことになり、
    スレッド並列だと pickle.load が競合して
      SystemError: deallocated bytearray object has exported buffers
    が出る(実測: analyze_tenkan_cutoff を workers 6 で回したとき)。

    返り値: {"pnl", "buy_p", "sell_p", "buy_t", "sell_t"}
    """
    if day is None or len(day) < 3:
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


def simulate(symbol: str, d: "date") -> "dict | None":
    """指定日の転換結果。分足を自分で読み込む版(単発利用向け)。
    既に分足を持っているなら simulate_bars() を使うこと。"""
    return simulate_bars(_day_bars(symbol, d))


def release_cache() -> None:
    """読み込んだ分足DataFrameを破棄してメモリを返す。
    転換の生成が終わった後に呼ぶ(銘柄詳細タブなど後続処理のメモリを空けるため)。"""
    _CACHE.clear()
    _CACHE_ORDER.clear()


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


def build_from_orders_csvs(search_dir=".", min_price: float = 0.0,
                           max_price: float = 0.0, verbose: bool = True):
    """orders_<yyyyMMdd>.csv (.\\fills が出力) から『実際に約定しなかった売り注文』を
    拾って転換トレードを作る。

    バックテストの約定判定(約定率≈90%)は実際(実測21%)と大きくズレるため、実注文の
    記録がある日はそちらが正しい母集団になる。返り値の dates に含まれる日は、
    バックテスト由来の転換を捨ててこちらで置き換えること。

    Returns: (trades, dates)  dates = 実注文データで置き換えた日付の集合
    """
    import csv as _csv
    from datetime import datetime as _dt
    from pathlib import Path as _P

    trades: list[dict] = []
    dates: set = set()
    files = sorted(_P(search_dir).glob("orders_*.csv"))
    if not files:
        return trades, dates

    n_file = 0
    for fp in files:
        stem = fp.stem.replace("orders_", "")
        if len(stem) != 8 or not stem.isdigit():
            continue
        try:
            d = _dt.strptime(stem, "%Y%m%d").date()
        except Exception:
            continue
        try:
            with open(fp, encoding="utf-8-sig", newline="") as f:
                rows = list(_csv.DictReader(f))
        except Exception:
            continue
        if not rows:
            continue

        n_file += 1
        seen: set = set()
        made = 0
        for r in rows:
            if str(r.get("side", "")).strip() != "売":
                continue
            try:
                if int(float(r.get("cum_qty") or 0)) > 0:
                    continue          # 約定した = lss成立 → 転換の対象外
            except Exception:
                continue
            code = str(r.get("code", "")).strip()
            if not code or code in seen:
                continue
            seen.add(code)

            sym = code if code.endswith(".T") else code + ".T"
            res = simulate(sym, d)
            if res is None:
                continue
            # 価格レンジは転換の約定値(=当日の買値)で判定する。
            if min_price > 0 and res["buy_p"] < min_price:
                continue
            if max_price > 0 and res["buy_p"] > max_price:
                continue
            trades.append(make_trade(sym, str(r.get("name", "")), d, res, 0.0))
            made += 1

        if made:
            dates.add(str(d))

    if verbose and n_file:
        print(f"[転換/実注文] orders_*.csv {n_file}ファイルから {len(trades)}件生成 "
              f"(対象日 {len(dates)}日: {', '.join(sorted(dates))})", flush=True)
        print("   ※ この日はバックテスト判定ではなく実際の未約定注文を母集団にします。",
              flush=True)
    return trades, dates


def build_from_nofills(nofills, min_price: float = 0.0, max_price: float = 0.0,
                       exclude_keys=None, verbose: bool = True) -> list[dict]:
    """未約定シグナル(all_nofills 相当)から転換トレードを生成する。

    nofills の各要素に必要なキー: symbol / name / entry_d_raw(または exit_d_raw)
                                  / order_limit または entry_p / rec_score
    exclude_keys: 既に生成済みの (symbol, str(date)) 集合。重複を避ける。
    """
    import gc
    from collections import defaultdict

    out: list[dict] = []
    excl = exclude_keys or set()
    seen: set = set()
    n_price = n_nodata = n_dup = 0
    # 月別の内訳(なぜその月が出ないかを追えるようにする)
    m_in: dict = defaultdict(int)     # 入力にあった件数
    m_out: dict = defaultdict(int)    # 生成できた件数
    m_nodata: dict = defaultdict(int)  # 分足が無くて落ちた件数
    m_price: dict = defaultdict(int)   # 価格範囲外で落ちた件数

    # 銘柄順に処理し、銘柄が変わったら前銘柄の分足を捨てる。
    # 全銘柄ぶんの DataFrame を抱えると Python 3.14 で
    #   SystemError: deallocated bytearray object has exported buffers
    # や MemoryError が出るため、同時に1銘柄しか保持しない。
    def _key(t):
        d = t.get("entry_d_raw") or t.get("exit_d_raw")
        if hasattr(d, "date"):
            d = d.date()
        return (str(t.get("symbol") or ""), str(d))

    items = sorted((t for t in (nofills or []) if _key(t)[0] and _key(t)[1] != "None"),
                   key=_key)
    prev_sym = None
    n_sym = 0

    for t in items:
        sym = str(t.get("symbol") or "")
        d = t.get("entry_d_raw") or t.get("exit_d_raw")
        if hasattr(d, "date"):
            d = d.date()
        ym = str(d)[:7]
        m_in[ym] += 1

        if sym != prev_sym:
            if prev_sym is not None:
                drop_symbol(prev_sym)   # その銘柄の分足(1m/5m)を捨てる
                n_sym += 1
                if n_sym % 50 == 0:
                    gc.collect()
            prev_sym = sym

        key = (sym, str(d))
        if key in excl or key in seen:
            n_dup += 1
            continue

        ep = float(t.get("order_limit", 0) or 0) or float(t.get("entry_p", 0) or 0)
        if ep > 0:
            if (min_price > 0 and ep < min_price) or (max_price > 0 and ep > max_price):
                n_price += 1
                m_price[ym] += 1
                continue

        res = simulate(sym, d)
        if res is None:
            n_nodata += 1
            m_nodata[ym] += 1
            continue

        seen.add(key)
        m_out[ym] += 1
        out.append(make_trade(sym, str(t.get("name") or ""), d, res,
                              float(t.get("rec_score", 0) or 0)))

    _CACHE.clear()
    gc.collect()

    if verbose:
        print(f"[転換/未約定] 生成 {len(out)}件 "
              f"(価格範囲外 {n_price}件 / 分足なし {n_nodata}件 / 重複 {n_dup}件)",
              flush=True)
        if m_in:
            print("[転換/未約定] 月別内訳 (入力→生成 / 分足なし / 価格外):", flush=True)
            for ym in sorted(m_in):
                print(f"    {ym}  入力{m_in[ym]:>4}件 → 生成{m_out[ym]:>4}件"
                      f"  (分足なし {m_nodata[ym]:>3} / 価格外 {m_price[ym]:>3})",
                      flush=True)
    return out
