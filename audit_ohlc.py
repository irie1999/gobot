r"""audit_ohlc.py — 日足の**始値**が壊れていないか監査する(件数を測るだけ)

なぜ要るか (2026-08-28)
-----------------------
`backtest_limit_entry._clean_prices` は **終値の前日比が50%を超える行**しか
落としていません:

    pct = df["close"].pct_change().abs()
    df  = df[pct <= 0.5].copy()          # ← close だけ。open/high/low は素通り

ところが N は
  ・**始値**で建てる
  ・ギャップ = (始値 − 前日終値) / 前日終値 で判定する
ので、**始値が壊れていると偽のギャップが作られます**。しかも N はギャップが
大きい銘柄を選ぶので、**壊れた行が系統的に標本へ入ります**。
§18.27(分足の分割汚染)・§18.50(母集団の抜け)と同じ形で、
「戦略のフィルタ自身が欠陥データを選び取る」という一番たちの悪い型です。

⛔ このスクリプトは **何も直しません**。件数を数えるだけです。
   `_clean_prices` を変えると全キャッシュ・全結果が無効になるので、
   大きさを知ってから決めます。

見るもの
--------
  A. OHLC 整合性   始値と終値が 安値〜高値 の内側にあるか / 安値 <= 高値 か
  B. 始値のギャップ |始値 / 前日終値 - 1| が大きすぎないか(既定30%)
  C. 日中の値幅     |終値 / 始値 - 1| が大きすぎないか(同上)
  D. ★ そのうち **N のギャップ条件(>= +100bp)を通ってしまう**のは何件か
     ← これが本丸。全体の何%が汚染由来かが分かる

⚠ 30% は東証の値幅制限(株価比 14〜30%)より緩い上限です。正常な値動きは
  1本も落ちません。落ちたものは本物の異常です。

使い方
------
  python audit_ohlc.py                        # 既定 プライム全銘柄 / 4200日
  python audit_ohlc.py --limit 300            # お試し
  python audit_ohlc.py --thr 0.25             # 判定を厳しく
  python audit_ohlc.py --list 30              # 該当行を並べる
  python audit_ohlc.py --purge                # ⛔ 該当銘柄のキャッシュを消す

⚠ 照会のみ。発注はしません。
"""
from __future__ import annotations

import argparse
import os
import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pickle

import pandas as pd

from backtest_limit_entry import _CACHE_DIR, _clean_prices, fetch
# ⛔ 固定%で判定すると **低位株を誤検出**する。100円未満の値幅制限は±30円=
#   株価比30%超なので、正常な値動きが引っかかる(2026-08-28 実測: 44件の
#   最多 6740.T の7件が全部これ)。東証の制限値幅テーブルで判定する。
from check_price_limit import _width


def _load_symbols(path: str) -> list[str]:
    mod = importlib.import_module(Path(path).stem)
    for name in ("SYMBOLS", "SYMBOL_LIST", "ALL_SYMBOLS"):
        v = getattr(mod, name, None)
        if not v:
            continue
        return [x[0] if isinstance(x, (tuple, list)) else str(x) for x in v]
    sys.exit(f"[error] {path} に銘柄リストが見つかりません")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="symbols_listed_prime.py")
    ap.add_argument("--days", type=int, default=4200)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--thr", type=float, default=0.0,
                    help="固定比率で判定する(0.30=30%%)。**既定0=値幅制限で判定**。"
                         "⛔ 固定%%は低位株を誤検出する(100円未満は±30円=30%%超が合法)")
    ap.add_argument("--limit-slack", type=float, default=1.10,
                    help="値幅制限の何倍を超えたら異常とみなすか(既定1.10)")
    ap.add_argument("--gap-bp", type=float, default=100.0,
                    help="N のギャップ条件(bp)。汚染がここを通る件数を数える")
    ap.add_argument("--list", type=int, default=0, help="該当行をN件並べる")
    ap.add_argument("--purge", action="store_true",
                    help="⛔ 該当した銘柄のキャッシュ pkl を消す(取り直させる)")
    ap.add_argument("--allow-download", action="store_true",
                    help="キャッシュに無い銘柄を yfinance から取る。既定は取らない"
                         "(キャッシュの監査なので不要。上場廃止銘柄で待たされる)")
    ap.add_argument("--via-fetch", action="store_true",
                    help="pkl 直読みではなく fetch() 経由にする。fetch は "
                         "_clean_prices を通すので **落とされた後**の行しか"
                         "見えない(=汚染を数えられない)。比較用")
    ap.add_argument("--cap-per-symbol", type=int, default=200,
                    help="1銘柄あたり保持する該当行の上限(メモリ対策)")
    a = ap.parse_args()

    if not a.allow_download:
        # ⛔ 1,541銘柄を1件ずつ yfinance に問い合わせると、上場廃止銘柄の
        #   タイムアウトで数分待たされる。監査対象はキャッシュそのもの。
        os.environ["GOBOT_OFFLINE"] = "1"

    # ★ キャッシュの場所を**必ず表示してから**始める。
    #   git worktree で作業フォルダを分けると .rsi2_cache は gitignore なので
    #   新しい側には存在せず、offline だと fetch が全銘柄 None を返して
    #   「1銘柄も読めませんでした」としか出ない(2026-08-28 に実際に起きた)。
    #   **黙って0件を返さない**。場所と件数を出して、空なら即座に止める。
    _pk = sorted(_CACHE_DIR.glob("*.pkl")) if _CACHE_DIR.exists() else []
    print(f"[info] 日足キャッシュ: {_CACHE_DIR.resolve()}")
    print(f"[info]   存在={_CACHE_DIR.exists()} / pkl {len(_pk):,}件"
          + (f" / 例 {_pk[0].name}" if _pk else ""))
    if not _pk and not a.allow_download:
        sys.exit(
            "\n[error] キャッシュが空です。以下のどれかです:\n"
            "  ① 環境変数 GOBOT_CACHE_DIR が未設定(このウィンドウで設定し直す)\n"
            '     $env:GOBOT_CACHE_DIR = "C:\\...\\swingtrade\\.rsi2_cache"\n'
            "  ② 別の作業フォルダで動いている(git worktree は .rsi2_cache を"
            "共有しません)\n"
            "  ③ そもそもキャッシュを作っていない → --allow-download で取得")

    syms = _load_symbols(a.symbols)
    if a.limit > 0:
        syms = syms[:a.limit]
    _mode = (f"固定 {a.thr * 100:.0f}%" if a.thr > 0
             else f"**東証の値幅制限 × {a.limit_slack:.2f}**")
    print(f"[info] 銘柄 {len(syms):,} / 遡り {a.days:,}日 / 判定 {_mode}"
          + ("" if a.allow_download else " / キャッシュのみ(DLしない)"))
    print(f"[info] ⛔ 何も直しません。件数を数えるだけです"
          + ("(--purge 指定あり → 最後に該当銘柄のキャッシュを消します)"
             if a.purge else ""))

    _CUT = pd.Timestamp.today().normalize() - pd.Timedelta(days=a.days)

    def _read(sym: str):
        """⛔ fetch() を通さず pkl を直接読む。

        fetch() は「最新バーが古い」キャッシュを陳腐とみなして捨てるので、
        オフラインだと None が返る(2026-08-28 に検算で判明)。そして監査の
        対象は **保存されている中身そのもの** なので、鮮度判定は不要。
        さらに fetch() は _clean_prices を通すため、**落とされた後**の行しか
        見えない。それでは「何が落ちているか」を数えられない。
        """
        p = _CACHE_DIR / f"{sym.replace('.', '_')}.pkl"
        if not p.exists():
            return None
        try:
            with open(p, "rb") as f:
                d = pickle.load(f)
        except Exception:
            return None
        if not isinstance(d, pd.DataFrame) or d.empty:
            return None
        if not all(c in d.columns for c in ("open", "high", "low", "close")):
            return None
        try:
            d = d[d.index >= _CUT]
        except Exception:
            return None
        return d if len(d) >= 3 else None

    def _one(sym: str):
        d = _read(sym) if not a.via_fetch else None
        if a.via_fetch:
            try:
                d = fetch(sym, a.days)
            except Exception:
                return None
        if d is None or len(d) < 3:
            return None
        o, h, l, c = d["open"], d["high"], d["low"], d["close"]
        pc = c.shift(1)
        t = pd.DataFrame({
            "symbol": sym, "date": d.index,
            "prev_close": pc.to_numpy(), "open": o.to_numpy(),
            "high": h.to_numpy(), "low": l.to_numpy(), "close": c.to_numpy(),
        })
        # A. OHLC 整合性
        t["bad_ohlc"] = ((o > h) | (o < l) | (c > h) | (c < l) | (l > h)).to_numpy()
        # B. 始値のギャップ / C. 日中の値幅
        t["gap"] = (o / pc - 1.0).to_numpy()
        t["intra"] = (c / o - 1.0).to_numpy()
        # 制限値幅(円)を前日終値から引き、比率に直す。0 なら固定%にフォールバック
        if a.thr > 0:
            _lim = pd.Series(a.thr, index=t.index)
        else:
            _pcv = t["prev_close"].fillna(0.0)
            _lim = (_pcv.map(lambda x: _width(x) if x > 0 else 0.0)
                    / _pcv.replace(0.0, float("nan"))) * a.limit_slack
        t["bad_gap"] = t["gap"].abs() > _lim
        t["bad_intra"] = t["intra"].abs() > _lim
        t["bad"] = t["bad_ohlc"] | t["bad_gap"] | t["bad_intra"]
        t["n_gap"] = t["gap"] * 10_000.0 >= a.gap_bp     # N の条件を通るか
        t = t.dropna(subset=["prev_close"])
        # ⛔ 全パネルを返すと 1,541銘柄で 669万行 になり MemoryError。
        #   **銘柄ごとに集計して捨てる**。持ち帰るのは集計値と該当行(少数)だけ。
        _g, _i = t["gap"], t["intra"]
        _ng, _nb = t["n_gap"], t["n_gap"] & t["bad"]
        _no = t["n_gap"] & ~t["bad"]
        st = {
            "n": len(t), "sym": sym,
            "bad_ohlc": int(t["bad_ohlc"].sum()), "bad_gap": int(t["bad_gap"].sum()),
            "bad_intra": int(t["bad_intra"].sum()), "bad": int(t["bad"].sum()),
            "ng": int(_ng.sum()), "ng_bad": int(_nb.sum()), "ng_ok": int(_no.sum()),
            "ng_bad_gap": float(_g[_nb].sum()), "ng_bad_intra": float(_i[_nb].sum()),
            "ng_ok_gap": float(_g[_no].sum()), "ng_ok_intra": float(_i[_no].sum()),
            # ★ 平均は異常値1件(株価559億など)で壊れるので、中央値用に生値も持つ
            "_bi": list(_i[_nb].to_numpy()), "_oi": list(_i[_no].to_numpy()),
        }
        # ★ エンジン(_clean_prices)を通したら何が残るか。
        #   「キャッシュに汚染がある」と「エンジンがそれを見ている」は別の話。
        try:
            _cp = _clean_prices(d.copy())
        except Exception:
            _cp = None
        st["cp_none"] = 1 if _cp is None else 0
        st["cp_drop"] = 0 if _cp is None else int(len(d) - len(_cp))
        _bd = t[t["bad"]]
        if len(_bd) > a.cap_per_symbol:
            _bd = _bd.reindex(_bd["gap"].abs().sort_values(ascending=False).index
                              ).head(a.cap_per_symbol)
        return st, _bd

    stats, bads, ok = [], [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(_one, syms), 1):
            if r is not None:
                stats.append(r[0])
                if len(r[1]):
                    bads.append(r[1])
                ok += 1
            if i % 300 == 0:
                print(f"  … {i:,}/{len(syms):,}", flush=True)
    if not stats:
        sys.exit("[error] 1銘柄も読めませんでした"
                 + ("" if a.allow_download else
                    "(キャッシュのみモードです。--allow-download で取得できます)"))

    s = pd.DataFrame(stats)
    _n = int(s["n"].sum())
    print(f"\n{'=' * 72}")
    print(f"■ 日足の始値・OHLC 監査 — {ok:,}銘柄 / {_n:,}銘柄日")
    print(f"{'=' * 72}")
    for _k, _lbl in (("bad_ohlc", "A. OHLC 整合性エラー(始値/終値が安値〜高値の外)"),
                     ("bad_gap", f"B. 始値ギャップが {_mode} 超"),
                     ("bad_intra", f"C. 日中変化が {_mode} 超")):
        _c = int(s[_k].sum())
        print(f"  {_lbl:<44}{_c:>7,}件 ({_c / max(1, _n) * 100:.4f}%)")
    _bad = int(s["bad"].sum())
    print(f"  {'いずれか':<44}{_bad:>7,}件 ({_bad / max(1, _n) * 100:.4f}%) / "
          f"{int((s['bad'] > 0).sum()):,}銘柄")

    # ── ★ 本丸: N のギャップ条件を通ってしまう汚染 ──────────────
    _NG, _NB, _NO = int(s["ng"].sum()), int(s["ng_bad"].sum()), int(s["ng_ok"].sum())
    print(f"\n  ── ★ N のギャップ条件(>= +{a.gap_bp:.0f}bp)を通る行 ──")
    print(f"     全体          {_NG:>8,}件")
    print(f"     うち汚染      {_NB:>8,}件 "
          f"(**{_NB / max(1, _NG) * 100:.3f}%**)")
    if _NB:
        print(f"     汚染行の平均ギャップ  "
              f"{s['ng_bad_gap'].sum() / _NB * 100:+.1f}%"
              f"  (正常行 {s['ng_ok_gap'].sum() / max(1, _NO) * 100:+.2f}%)")
        import numpy as _np
        _bi = _np.array([x for v in s["_bi"] for x in v], float)
        _oi = _np.array([x for v in s["_oi"] for x in v], float)
        print(f"     汚染行の平均 日中変化 "
              f"{s['ng_bad_intra'].sum() / _NB * 100:+.1f}%"
              f"  (正常行 {s['ng_ok_intra'].sum() / max(1, _NO) * 100:+.2f}%)")
        if len(_bi):
            print(f"     ★ 汚染行の**中央値** 日中変化 "
                  f"{_np.median(_bi) * 100:+.2f}%"
                  f"  (正常行 {_np.median(_oi) * 100:+.2f}%)")
            print(f"        ⚠ 平均は異常値1件(株価559億など)で壊れます。"
                  f"**判断は中央値で**")
        print(f"     ⚠ 日中変化がショートに有利な向き(マイナス)へ偏っていれば、"
              f"汚染が利益を作っています")

    print(f"\n  ── エンジン(_clean_prices)を通すとどうなるか ──")
    print(f"     終値の前日比50%超で落ちる行   {int(s['cp_drop'].sum()):>7,}件")
    print(f"     まるごと除外される銘柄        {int(s['cp_none'].sum()):>7,}銘柄")
    print(f"     ⚠ 上の A/B/C はこの除去を**通り抜けた**ぶんです"
          f"(終値しか見ていないため)")

    t = pd.concat(bads, ignore_index=True) if bads else pd.DataFrame()
    if a.list > 0 and len(t):
        print(f"\n  ── 該当行(ギャップの大きい順 {a.list}件) ──")
        _v = t.reindex(t["gap"].abs().sort_values(ascending=False).index).head(a.list)
        print(f"     {'日付':<12}{'銘柄':<9}{'前日終値':>10}{'始値':>10}"
              f"{'安値':>10}{'高値':>10}{'終値':>10}{'ギャップ':>9}{'':>4}")
        for r in _v.itertuples():
            _f = ("OHLC" if r.bad_ohlc else ("GAP" if r.bad_gap else "INTRA"))
            print(f"     {str(r.date)[:10]:<12}{r.symbol:<9}{r.prev_close:>10,.1f}"
                  f"{r.open:>10,.1f}{r.low:>10,.1f}{r.high:>10,.1f}"
                  f"{r.close:>10,.1f}{r.gap * 100:>+8.1f}% {_f:>5}")

    print(f"\n  ── 該当が多い銘柄 ──")
    _bs = (s[s["bad"] > 0].set_index("sym")["bad"]
           .sort_values(ascending=False))
    for _s, _c in _bs.head(10).items():
        print(f"     {_s:<10}{_c:>5,}件")

    if a.purge and len(_bs):
        print(f"\n  ⛔ キャッシュを消します({len(_bs):,}銘柄)")
        _d = 0
        for s in _bs.index:
            p = _CACHE_DIR / f"{s.replace('.', '_')}.pkl"
            if p.exists():
                p.unlink()
                _d += 1
        print(f"     {_d:,}件 削除。次回の実行で取り直されます")
        print(f"     ⚠ 取り直しても直らないなら、yfinance 側のデータが壊れています")

    print(f"\n  {'=' * 66}")
    print(f"  ⚠ `_clean_prices` は **終値の前日比50%超**しか落としません。")
    print(f"     始値・高値・安値・OHLC整合性は検査していないので、"
          f"ここで出た行は素通りしています。")
    print(f"  ⚠ N はギャップの大きい銘柄を選ぶので、**壊れた始値は"
          f"選ばれやすい**方向に働きます。")
    print(f"  {'=' * 66}")


if __name__ == "__main__":
    main()
