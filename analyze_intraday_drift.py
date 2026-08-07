"""analyze_intraday_drift.py — 『同日決済に、そもそもどんな地力があるのか』を素で測る。

なぜ必要か
-----------
lss は「ロング候補を逆指値ショート・同日決済」。11ヶ月の予算タブで +883,421円(手数料0)。
一方これまでの調査で、**同日決済の『買い』はどのタイミング・どの母集団でも負ける**ことが
分かっている(pending_ideas #6 / #6b / #7 / LDT)。

  #6 の実測: 前日終値→大引け PF2.20 / 寄り→大引け PF0.49 / 9:05→大引け PF0.29
            「利益は前日終値→寄りの一晩ギャップ(+4,677円/件)に集中し、寄り以降はフェード」

これが正しければ、この市場は **引け→翌寄りがプラス / 寄り→引けがマイナス** で、
lss が効くのは「下向きの日中ドリフトを唯一ショートで拾っているから」かもしれない。

**その場合、lss の全機構(WF選定・BTスコア・逆指値・ATR損切り・delay)は何も足していない。**
本ツールは選定もトリガーも損切りも一切使わず、素の区間リターンだけを測って白黒つける。

出すもの
--------
1. 区間別の期待値 (100株・ショート目線の円 と %)
     overnight  前日終値 → 寄り        ← 持ち越し区間(ロングが儲かる区間のはず)
     intraday   寄り     → 大引け      ← これが lss の土俵
     open30     寄り     → 09:30
     morning    09:30    → 前場引け
     lunch      前場引け → 後場寄り
     afternoon  後場寄り → 引け30分前
     close30    引け30分前 → 大引け
2. **日クラスタ頑健な t 値**。同日決済は日内で強く相関するので、件数ベースの t は嘘になる
   (CLAUDE.md 18.13 の作法)。日ごとに等ウェイト平均を取り、日数を n として検定する。
3. 月別 / TRAIN・TEST 分割。区間の符号が時期で反転しないか。
4. 価格帯・曜日別の内訳(任意)。

使い方 (あなたの機械で。この環境は5分足を持たない)
--------------------------------------------------
  python analyze_intraday_drift.py                                  # 全銘柄・全期間
  python analyze_intraday_drift.py --min-price 1000 --max-price 6000  # 実運用の母集団に合わせる
  python analyze_intraday_drift.py --split 2026-02-01               # TRAIN/TEST 分割
  python analyze_intraday_drift.py --symbols-from lss_proposal_cumul.py  # lss選定銘柄だけ
  python analyze_intraday_drift.py --by-month --by-dow
  python analyze_intraday_drift.py --limit 200 --workers 8          # デバッグ

読み方
------
* intraday(寄り→大引け)のショート円がプラスで t>2 なら、**選定なしでも日中ショートは効く**。
  その値が lss の1件あたり(予算タブ 883,421/2,113 = +418円/件)と同水準なら、
  lss の機構は何も足していない。大きく下回るなら機構がエッジ。
* overnight がプラス(ロング目線)なら、この市場の上昇は夜に起きている。
  同日決済という制約自体が、儲かる区間を捨てていることになる。
* 区間別で1つだけ突出していれば、その時間帯だけに絞る(=保有時間を短くする)余地がある。
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from daytrade_data import (DATA_DIR, available_local_symbols, load_intraday)

# ── 区間定義 ────────────────────────────────────────────────────────
# (名前, 始点の指定, 終点の指定)。指定は "prev_close" / "open" / "close" / "HH:MM"。
# HH:MM は「その時刻**以前**の最後のバーの終値」= その時点で実際に約定できる値。
SEGMENTS = [
    ("overnight", "prev_close", "open"),
    ("intraday",  "open",       "close"),
    ("open30",    "open",       "09:30"),
    ("morning",   "09:30",      "11:30"),
    ("lunch",     "11:30",      "12:35"),
    ("afternoon", "12:35",      "15:00"),
    ("close30",   "15:00",      "close"),
]

ap = argparse.ArgumentParser(
    description="同日決済の地力を素で測る(選定・トリガー・損切りなし)")
ap.add_argument("--source", type=str, default="local", help="local / auto")
ap.add_argument("--days", type=int, default=800, help="遡る暦日数")
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(デバッグ)")
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--min-price", type=float, default=0.0, help="その日の寄り値で下限フィルタ")
ap.add_argument("--max-price", type=float, default=0.0, help="同 上限(0=無制限)")
ap.add_argument("--split", type=str, default="",
                help="TRAIN/TEST の境目 (YYYY-MM-DD)。未指定なら分割しない")
ap.add_argument("--symbols-from", type=str, default="",
                help="銘柄を絞る。SELECTED/PROPOSAL 形式の .py か、カンマ区切りコード")
ap.add_argument("--by-month", action="store_true")
ap.add_argument("--by-dow", action="store_true", help="曜日別")
ap.add_argument("--by-price", action="store_true", help="価格帯別")
ap.add_argument("--qty", type=int, default=100, help="1トレードの株数(既定100)")
ap.add_argument("--csv", type=str, default="", help="日次×銘柄の生データをCSV保存")
args = ap.parse_args()


# ── 銘柄の絞り込み ──────────────────────────────────────────────────
def _load_symbol_filter(spec: str) -> set[str] | None:
    """--symbols-from を解釈して 4桁コードの集合を返す。"""
    if not spec.strip():
        return None
    p = Path(spec)
    if p.exists():
        txt = p.read_text(encoding="utf-8", errors="ignore")
        codes = re.findall(r"[\"'](\d{4})(?:\.T)?[\"']", txt)
        if not codes:
            sys.exit(f"[error] {p} からコードを抽出できませんでした")
        return {c for c in codes}
    return {c.strip().upper().removesuffix(".T")
            for c in spec.split(",") if c.strip()}


_SYM_FILTER = _load_symbol_filter(args.symbols_from)


def _jq_to_yf(code: str) -> str:
    c = str(code).strip().upper().removesuffix(".T")
    if len(c) == 5 and c.isdigit() and c.endswith("0"):
        c = c[:4]
    return f"{c}.T"


# ── 1銘柄ぶんの区間リターン ─────────────────────────────────────────
def _at_or_before(day: pd.DataFrame, hhmm: str) -> float:
    """その時刻以前の最後のバーの終値。無ければ NaN。

    『その時点で実際に約定できる値』にしたいので、時刻ちょうどのバーが無くても
    直前のバーを使う(未来のバーは絶対に使わない)。
    """
    h, m = (int(x) for x in hhmm.split(":"))
    cut = day.index[0].normalize() + pd.Timedelta(hours=h, minutes=m)
    sub = day.loc[:cut]
    return float(sub["close"].iloc[-1]) if len(sub) else float("nan")


def _one_symbol(sym: str) -> pd.DataFrame | None:
    try:
        df = load_intraday(sym, days=args.days, source=args.source)
    except Exception:
        return None
    if df is None or df.empty or len(df) < 20:
        return None

    rows = []
    prev_close = float("nan")
    for d, day in df.groupby(df.index.normalize(), sort=True):
        if len(day) < 6:                       # 半日立会など。区間が取れない
            prev_close = float(day["close"].iloc[-1])
            continue
        rec = {
            "symbol": sym, "date": d,
            "prev_close": prev_close,
            "open": float(day["open"].iloc[0]),
            "close": float(day["close"].iloc[-1]),
        }
        for hhmm in ("09:30", "11:30", "12:35", "15:00"):
            rec[hhmm] = _at_or_before(day, hhmm)
        rows.append(rec)
        prev_close = rec["close"]

    return pd.DataFrame(rows) if rows else None


# ── 母集団 ──────────────────────────────────────────────────────────
_seen, universe = set(), []
for _s in available_local_symbols():
    yf = _jq_to_yf(_s)
    if yf in _seen:
        continue
    if _SYM_FILTER is not None and yf.removesuffix(".T") not in _SYM_FILTER:
        continue
    _seen.add(yf)
    universe.append(yf)

if args.limit > 0:
    universe = universe[:args.limit]
if not universe:
    sys.exit(f"[error] 5分足が見つかりません (DATA_DIR={DATA_DIR})。"
             f"MINUTE_5M_DIR を設定するか --source auto を試してください")

print(f"[母集団] {len(universe):,}銘柄 (DATA_DIR={DATA_DIR})"
      + (f" / --symbols-from で絞り込み" if _SYM_FILTER else ""))

parts = []
with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
    futs = {ex.submit(_one_symbol, s): s for s in universe}
    done = 0
    for f in as_completed(futs):
        done += 1
        if done % 200 == 0:
            print(f"  ...{done}/{len(universe)}銘柄", flush=True)
        r = f.result()
        if r is not None:
            parts.append(r)

if not parts:
    sys.exit("[error] 有効なデータが1銘柄もありませんでした")

px = pd.concat(parts, ignore_index=True)
px["date"] = pd.to_datetime(px["date"])

# 価格フィルタは『その日の寄り値』で判定する。最新終値で切るのは先読み(CLAUDE.md 18.16)。
n_before = len(px)
if args.min_price > 0:
    px = px[px["open"] >= args.min_price]
if args.max_price > 0:
    px = px[px["open"] <= args.max_price]
if len(px) != n_before:
    print(f"[価格フィルタ] {n_before:,} → {len(px):,} 銘柄日 "
          f"(寄り値 {args.min_price:,.0f}〜{args.max_price:,.0f}円。当日値なので先読みなし)")

px = px.sort_values(["date", "symbol"]).reset_index(drop=True)
print(f"[入力] {len(px):,} 銘柄日 / {px['date'].min().date()} 〜 {px['date'].max().date()} "
      f"/ {px['date'].nunique():,}営業日")


# ── 区間リターンを計算 ──────────────────────────────────────────────
def _col(name: str) -> pd.Series:
    return px[{"prev_close": "prev_close", "open": "open",
               "close": "close"}.get(name, name)]


for seg, a, b in SEGMENTS:
    s, e = _col(a).astype(float), _col(b).astype(float)
    r = (e - s) / s
    px[f"r_{seg}"] = r
    # ショート目線の円/qty株。価格が下がれば +。ロング目線は符号を反転して読む。
    px[f"y_{seg}"] = -(e - s) * args.qty

if args.csv:
    px.to_csv(args.csv, index=False, encoding="utf-8-sig")
    print(f"[保存] {args.csv}")


# ── 日クラスタ頑健な集計 ────────────────────────────────────────────
# 同日決済は日内で強く相関する(下げた日は全銘柄まとめて勝つ)。件数を n にすると
# 実効サンプルを何十倍にも誤認する。日ごとに等ウェイト平均を取り、日数で検定する。
def _daily_stats(d: pd.DataFrame, seg: str) -> dict:
    col = f"y_{seg}"
    g = d.dropna(subset=[col]).groupby("date")[col]
    daily = g.mean()
    n_days = len(daily)
    if n_days < 3:
        return {}
    mu = float(daily.mean())
    sd = float(daily.std(ddof=1))
    se = sd / math.sqrt(n_days) if sd > 0 else float("nan")
    t = mu / se if se and se > 0 else float("nan")
    rcol = f"r_{seg}"
    return {
        "件数": int(d[col].notna().sum()),
        "日数": n_days,
        "円/件": float(d[col].mean()),
        "円/日": mu,
        "%": float(d[rcol].mean() * 100),
        "t": t,
        "CI下": mu - 1.96 * se if se == se else float("nan"),
        "CI上": mu + 1.96 * se if se == se else float("nan"),
        "勝日%": float((daily > 0).mean() * 100),
    }


def _report(d: pd.DataFrame, title: str) -> None:
    if d.empty:
        return
    print(f"\n{'=' * 100}")
    print(f"■ {title}   {len(d):,}銘柄日 / {d['date'].nunique():,}営業日 "
          f"/ {d['date'].min().date()}〜{d['date'].max().date()}")
    print(f"  ※ 円は『{args.qty}株ショート』目線。ロングで読むなら符号を反転する")
    print(f"{'=' * 100}")
    print(f"  {'区間':<11}{'件数':>9}{'日数':>6}{'ショート円/件':>13}{'%':>8}"
          f"{'t(日)':>8}{'95%CI(円/日)':>22}{'勝日%':>7}")
    print("  " + "-" * 94)
    for seg, a, b in SEGMENTS:
        st = _daily_stats(d, seg)
        if not st:
            continue
        mark = " ★" if abs(st["t"]) >= 2.0 else ""
        print(f"  {seg:<11}{st['件数']:>9,}{st['日数']:>6}"
              f"{st['円/件']:>+13,.0f}{-st['%']:>+8.3f}"
              f"{st['t']:>8.2f}"
              f"{st['CI下']:>+11,.0f}{st['CI上']:>+11,.0f}"
              f"{st['勝日%']:>7.1f}{mark}")
    print("  " + "-" * 94)
    print("  ★ = |t| >= 2.0 (日クラスタ頑健)。% はショート目線の平均リターン")

    # ── ノイズ床 ────────────────────────────────────────────────
    # ⛔ 合成データで実証済み(2026-08-08): 真のドリフトが**ゼロ**でも、日共通ショック
    #    (σ=1.2%/日=下げ日は全銘柄まとめて負ける、という同日決済の実態)があるだけで
    #    intraday が +574円/件 (t=1.99) と出た。真値ありのときは同じ日数で t=12.43。
    #    → t は正しく効くが、**7区間も見れば1つは |t|>=2 になる**(多重検定)。
    #    ★1個は「効いている」の証拠にならない。t が一桁台前半なら疑ってかかること。
    _it0 = _daily_stats(d, "intraday")
    if _it0 and _it0["日数"] >= 20:
        _dl = d.dropna(subset=["y_intraday"]).groupby("date")["y_intraday"].mean()
        _floor = 1.96 * float(_dl.std(ddof=1)) / math.sqrt(len(_dl))
        print(f"  ノイズ床: 日次のばらつき(σ={_dl.std(ddof=1):,.0f}円/日)から、"
              f"{len(_dl)}営業日で識別できる下限は **±{_floor:,.0f}円/件**。")
        print(f"           これを下回る区間は、符号が何であれゼロと区別できない。")
        print(f"           ★が1個だけなら多重検定(7区間)の当たり。2〜3個以上か t が"
              f"二桁で初めて意味を持つ。")


_report(px, "全期間")

# ── TRAIN / TEST ────────────────────────────────────────────────────
if args.split:
    cut = pd.Timestamp(args.split)
    _report(px[px["date"] < cut], f"TRAIN (〜{cut.date()})")
    _report(px[px["date"] >= cut], f"TEST  ({cut.date()}〜)")

# ── 月別 ────────────────────────────────────────────────────────────
if args.by_month:
    print(f"\n{'=' * 100}")
    print("■ 月別 (ショート円/件)")
    print(f"{'=' * 100}")
    px["月"] = px["date"].dt.strftime("%Y-%m")
    segs = [s for s, _, _ in SEGMENTS]
    print(f"  {'月':<9}{'件数':>8}" + "".join(f"{s[:9]:>11}" for s in segs))
    print("  " + "-" * (17 + 11 * len(segs)))
    for mo, g in px.groupby("月"):
        row = "".join(f"{g[f'y_{s}'].mean():>+11,.0f}" for s in segs)
        print(f"  {mo:<9}{len(g):>8,}{row}")

# ── 曜日別 ──────────────────────────────────────────────────────────
if args.by_dow:
    print(f"\n{'=' * 100}")
    print("■ 曜日別 (ショート円/件)")
    print(f"{'=' * 100}")
    _dow = ["月", "火", "水", "木", "金", "土", "日"]
    px["曜日"] = px["date"].dt.dayofweek.map(lambda i: _dow[i])
    segs = [s for s, _, _ in SEGMENTS]
    print(f"  {'曜日':<7}{'件数':>8}" + "".join(f"{s[:9]:>11}" for s in segs))
    print("  " + "-" * (15 + 11 * len(segs)))
    for i in range(5):
        g = px[px["曜日"] == _dow[i]]
        if g.empty:
            continue
        row = "".join(f"{g[f'y_{s}'].mean():>+11,.0f}" for s in segs)
        print(f"  {_dow[i]:<7}{len(g):>8,}{row}")

# ── 価格帯別 ────────────────────────────────────────────────────────
if args.by_price:
    print(f"\n{'=' * 100}")
    print("■ 価格帯別 (ショート円/件)")
    print(f"{'=' * 100}")
    bins = [0, 1000, 2000, 3000, 5000, 8000, 1e9]
    labels = ["〜1000", "1000-2000", "2000-3000", "3000-5000", "5000-8000", "8000〜"]
    px["価格帯"] = pd.cut(px["open"], bins=bins, labels=labels, right=False)
    segs = [s for s, _, _ in SEGMENTS]
    print(f"  {'価格帯':<12}{'件数':>8}" + "".join(f"{s[:9]:>11}" for s in segs))
    print("  " + "-" * (20 + 11 * len(segs)))
    for lb in labels:
        g = px[px["価格帯"] == lb]
        if g.empty:
            continue
        row = "".join(f"{g[f'y_{s}'].mean():>+11,.0f}" for s in segs)
        print(f"  {lb:<12}{len(g):>8,}{row}")

# ── 判定の手引き ────────────────────────────────────────────────────
_it = _daily_stats(px, "intraday")
_on = _daily_stats(px, "overnight")
print(f"\n{'─' * 100}")
print("■ この数字の使い方")
print(f"{'─' * 100}")
if _it:
    print(f"  intraday(寄り→大引けのショート) = {_it['円/件']:+,.0f}円/件 (t={_it['t']:.2f})")
    print(f"    lss の実績は 予算タブ11ヶ月で +883,421円 / 2,113件 = **+418円/件**。")
    if _it["円/件"] >= 418 * 0.8:
        print(f"    → **同水準以上。選定・逆指値・損切りの機構は何も足していない可能性が高い。**")
        print(f"       素の日中ショートの方が銘柄数を稼げるぶん容量(capacity)も大きい。")
    elif _it["円/件"] > 0:
        print(f"    → 素でもプラスだが lss を下回る。機構が上乗せしている部分がある。")
        print(f"       差 {_it['円/件'] - 418:+,.0f}円/件 が『機構の付加価値』の上限。")
    else:
        print(f"    → 素ではマイナス。**lss のエッジは機構(選定+トリガー+損切り)の側にある。**")
        print(f"       日中ドリフトを拾っているだけ、という仮説は否定される。")
if _on:
    print(f"  overnight(前日終値→寄り) = ショート {_on['円/件']:+,.0f}円/件 "
          f"= ロング {-_on['円/件']:+,.0f}円/件 (t={_on['t']:.2f})")
    if -_on["円/件"] > 0:
        print(f"    → 上昇は夜に起きている。**同日決済という制約が、儲かる区間を丸ごと捨てている。**")
        print(f"       (持ち越しを避ける方針とのトレードオフ。判断はユーザー)")
print("  ※ 摩擦は一切引いていない(手数料0・滑り0)。実運用では貸株料と")
print("     スプレッドが乗るので、t が小さい区間は実質ゼロとみなすこと。")
