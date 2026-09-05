r"""fetch_futures_jq.py — J-Quants から日経225先物の**日次**を取り、寄り前変数を作る。

なぜ要るか (2026-08-28 / §18.62)
--------------------------------
§18.60 で「大負けの日は相場(R² 0.235)だが寄り前には読めない(R² 0.045)」と出た。
その ② に使っていた `fut_gap` は **yfinance の CME 日経先物(NKD=F)の前日終値**で、
日本時間で言えば早朝に確定した値。09:00 の判断からは遠い。

⛔ 先物の**分足**は現実的に取れない
   ・yfinance … 1分足は60日、5分足も60日。11年は無理
   ・kabu     … リアルタイムのみ。履歴なし
   ・J-Quants … 分足アドオンは**株式のみ**

★ だが J-Quants の先物**日次**は、1日を **夜間セッション / 日中セッション**に
   分けて持っている。ここが効く:

     NightSessionOpen/High/Low/Close  夜間(前日 16:30〜当日 06:00)
     DaySessionOpen/High/Low/Close    日中(当日 **08:45**〜15:45)

   **DaySessionOpen = 08:45 の寄り値 = 現物の寄り(09:00)の15分前。**
   NightSessionClose も当日 06:00 に確定。**どちらも寄り前**で、
   §18.60 の19変数のどれにも入っていない。

作る変数(すべて寄り前に確定)
----------------------------
  nkf_night_ret     夜間セッション終値 ÷ 前日の日中終値 − 1   (オーバーナイトの動き)
  nkf_night_range   夜間の高安幅 ÷ 前日の日中終値              (夜のボラ)
  nkf_day_open_gap  **08:45 の寄り値 ÷ 前日の日中終値 − 1**   ← 最重要
  nkf_gap_vs_night  08:45 の寄り値 ÷ 夜間終値 − 1              (06:00→08:45 の動き)

⚠ **先読みの検査**: どれも当日 08:45 までに確定した値しか使わない。
   当日の DaySessionHigh/Low/Close は **絶対に使わない**(引け後にしか分からない)。

使い方
------
  python fetch_futures_jq.py                      # 既定 2015-01-01〜今日
  python fetch_futures_jq.py --from 2007-01-01
  python fetch_futures_jq.py --category NK225     # NK225 / NK225mini
  python fetch_futures_jq.py --show               # 取得済みの中身を確認するだけ

  → .jq_futures_<category>.pkl に貯める(中断しても再開できる)

その後 preopen_market.py が自動で読み込み、--tail-diag の変数に加わる。

⚠ 照会のみ。発注はしない。
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

CACHE = Path(".")


def _load_dotenv() -> None:
    p = Path(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _client():
    _load_dotenv()
    try:
        import jquantsapi
    except ImportError:
        sys.exit("[error] jquants-api-client が未インストールです\n"
                 "  pip install jquants-api-client")
    key = os.environ.get("JQUANTS_API_KEY")
    if not key:
        sys.exit("[error] JQUANTS_API_KEY が未設定です(.env か環境変数)")
    return jquantsapi.ClientV2(api_key=key)


def _fetch_one(cli, day: str, category: str):
    """1営業日ぶんの先物を取る。ライブラリのメソッド名が版で違うので総当たり。"""
    _yyyymmdd = day.replace("-", "")
    _errs = []
    for _name in ("get_derivatives_futures", "get_futures_prices",
                  "get_derivative_futures", "get_futures"):
        _fn = getattr(cli, _name, None)
        if _fn is None:
            continue
        for _kw in ({"date": _yyyymmdd, "category": category},
                    {"date": day, "category": category},
                    {"date_yyyymmdd": _yyyymmdd, "category": category},
                    {"date": _yyyymmdd}):
            try:
                r = _fn(**_kw)
                if r is not None and len(r):
                    return pd.DataFrame(r)
                return pd.DataFrame()
            except TypeError as e:
                _errs.append(f"{_name}{tuple(_kw)}: {e}")
            except Exception as e:
                _errs.append(f"{_name}: {e}")
                break
    raise RuntimeError("先物の取得メソッドが見つかりません。"
                       "jquants-api-client を更新してください\n  "
                       + "\n  ".join(_errs[:4]))


def _pick_central(df: pd.DataFrame) -> dict | None:
    """中心限月の1行を選ぶ。フラグが無ければ出来高最大。"""
    if df is None or df.empty:
        return None
    for _c in ("CentralContractMonthFlag", "CentralContractMonthFlagCode"):
        if _c in df.columns:
            _s = df[df[_c].astype(str).isin(("1", "1.0", "True", "true"))]
            if len(_s):
                return _s.iloc[0].to_dict()
    for _c in ("Volume", "TradingVolume", "Volume(OnlyAuction)"):
        if _c in df.columns:
            try:
                return df.loc[pd.to_numeric(df[_c],
                                            errors="coerce").idxmax()].to_dict()
            except Exception:
                pass
    return df.iloc[0].to_dict()


def _f(row: dict, *names) -> float | None:
    for n in names:
        if n in row:
            try:
                v = float(row[n])
                if v == v and v > 0:
                    return v
            except Exception:
                pass
    return None


def build_features(store: dict) -> dict:
    """日付 -> 寄り前変数。**当日 08:45 までに確定した値しか使わない**。"""
    ks = sorted(store)
    out: dict = {}
    for i, d in enumerate(ks):
        r = store[d]
        if i == 0:
            continue
        # 前日の **日中終値**(前営業日 15:45 に確定)
        rp = store[ks[i - 1]]
        pc = _f(rp, "DaySessionClose", "WholeDayClose", "SettlementPrice")
        if not pc:
            continue
        f: dict = {}
        nc = _f(r, "NightSessionClose")          # 当日 06:00 に確定
        nh = _f(r, "NightSessionHigh")
        nl = _f(r, "NightSessionLow")
        do = _f(r, "DaySessionOpen")             # ★ 当日 08:45 に確定
        if nc:
            f["nkf_night_ret"] = (nc / pc - 1.0) * 100.0
        if nh and nl:
            f["nkf_night_range"] = (nh - nl) / pc * 100.0
        if do:
            f["nkf_day_open_gap"] = (do / pc - 1.0) * 100.0
            if nc:
                f["nkf_gap_vs_night"] = (do / nc - 1.0) * 100.0
        if f:
            out[d] = f
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="dfrom", default="2015-01-01")
    ap.add_argument("--to", dest="dto", default="")
    ap.add_argument("--category", default="NK225",
                    help="NK225 / NK225mini など")
    ap.add_argument("--sleep", type=float, default=0.15,
                    help="1リクエストごとの待ち(秒)")
    ap.add_argument("--show", action="store_true",
                    help="取得済みの中身を確認するだけ(APIを叩かない)")
    a = ap.parse_args()

    path = CACHE / f".jq_futures_{a.category}.pkl"
    store: dict = {}
    if path.exists():
        try:
            store = pickle.loads(path.read_bytes())
        except Exception:
            store = {}
    print(f"[info] キャッシュ {path} … {len(store):,}営業日")

    if a.show:
        feats = build_features(store)
        print(f"[info] 作れた寄り前変数 {len(feats):,}日")
        if feats:
            _k = sorted(feats)[-1]
            print(f"  最新 {_k}: {feats[_k]}")
            _all = sorted({k for v in feats.values() for k in v})
            print(f"  変数: {', '.join(_all)}")
            for _v in _all:
                _s = pd.Series([v.get(_v) for v in feats.values()],
                               dtype="float64").dropna()
                print(f"    {_v:<18} {len(_s):>6,}日 / 平均 {_s.mean():+.3f}% / "
                      f"σ {_s.std():.3f}")
        return

    d0 = date.fromisoformat(a.dfrom)
    d1 = date.fromisoformat(a.dto) if a.dto else date.today()
    cli = _client()
    days = []
    d = d0
    while d <= d1:
        if d.weekday() < 5 and d.isoformat() not in store:
            days.append(d.isoformat())
        d += timedelta(days=1)
    print(f"[info] 取得対象 {len(days):,}営業日(既に持っている日は飛ばす)")
    if not days:
        print("[info] 追加はありません")
        return

    _n = 0
    try:
        for i, day in enumerate(days, 1):
            try:
                row = _pick_central(_fetch_one(cli, day, a.category))
            except RuntimeError:
                raise
            except Exception as e:
                print(f"  ⚠ {day}: {e}")
                row = None
            if row:
                store[day] = row
                _n += 1
            if i % 100 == 0:
                print(f"  … {i:,}/{len(days):,} (取得 {_n:,})", flush=True)
                path.write_bytes(pickle.dumps(store))
            time.sleep(a.sleep)
    except KeyboardInterrupt:
        print("\n[info] 中断しました(ここまでを保存します。再実行で続きから)")
    finally:
        path.write_bytes(pickle.dumps(store))

    feats = build_features(store)
    print(f"\n[info] 保存 {path} … {len(store):,}営業日 / "
          f"寄り前変数を作れた日 {len(feats):,}")
    print("  ★ preopen_market.py が自動で読み込みます。次に --tail-diag を回すと"
          "変数が増えます")
    print("  ⚠ 使うのは **当日08:45までに確定した値だけ**"
          "(DaySessionHigh/Low/Close は先読みなので使いません)")


if __name__ == "__main__":
    main()
