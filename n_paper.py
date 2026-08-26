"""n_paper.py — 新方式N の 候補作成(--collect) と 引け後の損益(--close)

⛔ **09:00 の板読みは `n_open_confirm.py` の方を使ってください。**
   あちらは J の実運用で動作実績のあるコード(k_open_confirm.py)から
   発注だけを物理削除したもので、ウォームアップ・poll ループ・遅寄り検知・
   OpeningPriceTime の日付チェック・429対策が全部入っています。
   このファイルの --prod / --warmup は **簡易版**で、実績がありません。

n_paper.py — 新方式N の 09:00 判定を**記録だけ**する (発注しない)

⛔⛔ **このスクリプトは1円も発注しません。**
   kabu の照会 API (/board) しか呼びません。発注メソッド(send_*)は
   呼ばないどころか、このファイルのどこにも出てきません。
   引数を間違えても発注は起こりません。

★ 何のためか (2026-08-26 ユーザー依頼)
────────────────────────────────────────────────────────────────────
レポートの「★ 新方式N」タブは **日足の始値** で「ギャップ ≥ +100bp」を
判定している。実運用は **09:00 に kabu の板を読んで** 判定する。
この2つが一致するかは、実際に朝 板を読んでみないと分からない。

  ・板の始値 = 日足の始値 か  (§18.45 で J について 50/50 完全一致を確認済み。
    ただし母集団が違うので N でも同じとは限らない)
  ・09:00 に寄らない銘柄がどれだけあるか (遅寄り)
  ・前夜の候補が何件になるか (レポートの実測は中央値 120件/日)
  ・そのうち何件が合格するか

★ 使い方
────────────────────────────────────────────────────────────────────
  # ① 前夜 or 早朝: 候補を作る (kabu 不要。日足だけ)
  python n_paper.py --collect

  # ② 08:50 ごろ: kabu に登録してウォームアップ (§18.44: 登録直後の初回は遅い)
  python n_paper.py --prod --warmup

  # ③ 09:00: 板を読んで判定を記録
  python n_paper.py --prod

  # ④ 引け後: 終値を埋めて損益を出す (kabu 不要。日足だけ)
  python n_paper.py --close

  # 動作確認 (場中/場外どちらでも。時間帯ガードを外して1回読む)
  python n_paper.py --prod --now

★ 出力
────────────────────────────────────────────────────────────────────
  n_signals_<日付>.csv   前夜の候補 (--collect)
  n_paper_<日付>.csv     09:00 の判定 (--prod) → 引け後に損益を追記 (--close)

⚠ レポートとの既知のズレ
────────────────────────────────────────────────────────────────────
  レポートは **建値(D+1の始値)** で価格フィルタを掛けるが、前夜には始値が
  分からないので、ここでは **前日終値** で掛ける。境界付近の銘柄が入れ替わる。
"""
from __future__ import annotations

import argparse
import csv as _csv
import os
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

JST = timezone(timedelta(hours=9))

# ── 新方式N のパラメータ (§18.54)。レポート(nikkei_analysis._NG_*)と同じ既定 ──
RET1_MIN = float(os.environ.get("LSS_NEWGAP_RET1", "1.753"))   # 前日リターン下限(%)
GAP_BP = float(os.environ.get("LSS_NEWGAP_BP", "100"))         # ギャップ下限(bp)
WATCH = int(os.environ.get("LSS_NEWGAP_WATCH", "50"))          # 朝読める上限
MIN_PRICE = float(os.environ.get("LSS_NEWGAP_MIN_PRICE", "1000"))
MAX_PRICE = float(os.environ.get("LSS_NEWGAP_MAX_PRICE", "6000"))
QTY = 100

ap = argparse.ArgumentParser(
    description="新方式N の 09:00 判定を記録する (⛔ 発注しない)")
ap.add_argument("--collect", action="store_true",
                help="前夜/早朝: 日足から候補を作る (kabu 不要)")
ap.add_argument("--warmup", action="store_true",
                help="08:5x: kabu に登録して1回空読みする。"
                     "⛔ これを飛ばすと 09:00 の初回が 40〜50秒かかる(§18.44)")
ap.add_argument("--close", action="store_true",
                help="引け後: 終値を埋めて損益を出す (kabu 不要)")
ap.add_argument("--prod", action="store_true",
                help="本番口座(18080)に接続する。⛔ 照会のみ。発注はしない")
ap.add_argument("--now", action="store_true",
                help="時間帯ガードを外して いま1回読む(動作確認用)")
ap.add_argument("--date", type=str, default="",
                help="対象日 yyyy-MM-dd (既定 今日)")
ap.add_argument("--workers", type=int, default=8, help="--collect の並列数")
ap.add_argument("--board-workers", type=int, default=2,
                help="板読みの並列数。⛔ §18.44 実測で **2 が最適**。"
                     "上げても速くならず 429 が増えるだけ")
ap.add_argument("--ret1", type=float, default=RET1_MIN, help="前日リターン下限(%%)")
ap.add_argument("--gap-bp", type=float, default=GAP_BP, help="ギャップ下限(bp)")
ap.add_argument("--watch", type=int, default=WATCH, help="朝読める上限(0=無制限)")
ap.add_argument("--merge-j", action="store_true",
                help="★ J の候補(k_signals_<日付>.csv)も取り込んでマージする。"
                     "⛔ kabu の有効トークンは1つなので J と N を別々には"
                     "読めない。**1回の板読みで両方記録する**ための指定。"
                     "先に `python k_open_confirm.py --collect` を実行しておくこと")
ap.add_argument("--paper-csv", type=str, default="",
                help="--close が読む板読み結果。"
                     "既定 k_paper_<日付>.csv (n_open_confirm.py の出力)")
ap.add_argument("--gap-bp-j", type=float, default=75.0,
                help="--close で J として集計するギャップ(bp)。既定75")
a = ap.parse_args()

_TODAY = a.date or datetime.now(JST).strftime("%Y-%m-%d")
_YMD = _TODAY.replace("-", "")
_SIG_CSV = Path(f"n_signals_{_YMD}.csv")
_PAPER_CSV = Path(f"n_paper_{_YMD}.csv")


def _jq_to_yf(code: str) -> str:
    """J-Quants の5桁コード(末尾0)を yfinance の `NNNN.T` に直す。"""
    c = str(code).strip()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c.endswith("0"):
        c = c[:4]
    return f"{c}.T"


def _code4(sym: str) -> str:
    """`7203.T` → `7203` (kabu に渡す形)。"""
    return str(sym).replace(".T", "").strip()


# ══════════════════════════════════════════════════════════════════════
# ① --collect : 前夜の候補を作る (日足だけ。kabu 不要)
# ══════════════════════════════════════════════════════════════════════
def _scan_one(sym: str) -> dict | None:
    """1銘柄について、前日終値・前日リターン・流動性を返す。

    ⛔ **前夜に確定している値だけ**を使う。当日の始値・高値・安値・出来高は
       まだ存在しないので触らない。
    """
    try:
        import backtest_limit_entry as ble
        df = ble.fetch(sym, 260)
    except Exception:
        return None
    if df is None or len(df) < 25:
        return None
    try:
        _idx = pd.to_datetime(df.index).normalize()
        _c = df["close"]
        _v = df["volume"] if "volume" in df.columns else pd.Series(0.0, index=df.index)
        # 最終バー = 前営業日(D)。当日のバーはまだ無い前提。
        # ⚠ 場中に走らせると当日の未確定バーが入りうるので、日付を必ず見る。
        _last = _idx[-1]
        if str(_last.date()) >= _TODAY:
            # 当日のバーが既にある → その1本前を D とする
            if len(_idx) < 26:
                return None
            _pos = len(_idx) - 2
        else:
            _pos = len(_idx) - 1
        pc = float(_c.iloc[_pos])
        r1 = float(_c.iloc[_pos] / _c.iloc[_pos - 1] - 1.0) * 100.0
        lq = float((_c * _v).rolling(20).mean().iloc[_pos])
    except Exception:
        return None
    if not (pc > 0 and r1 == r1):
        return None
    return {"symbol": sym, "prev_close": round(pc, 1),
            "prev_date": str(_idx[_pos].date()),
            "ret1": round(r1, 3), "liq": round(lq if lq == lq else 0.0, 0)}


def do_collect() -> None:
    try:
        from daytrade_data import available_local_symbols
    except Exception as e:
        sys.exit(f"[error] daytrade_data を import できません: {e}")
    syms = sorted({_jq_to_yf(s) for s in available_local_symbols()})
    if not syms:
        sys.exit("[error] 銘柄が見つかりません")
    print(f"[collect] {len(syms):,}銘柄をスキャン (前日リターン ≥ {a.ret1}% / "
          f"建値 {MIN_PRICE:,.0f}〜{MAX_PRICE:,.0f}円)", flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(_scan_one, s): s for s in syms}
        for i, f in enumerate(as_completed(futs), 1):
            if i % 300 == 0:
                print(f"  … {i}/{len(syms)}", flush=True)
            try:
                r = f.result()
            except Exception:
                r = None
            if r:
                rows.append(r)
    # 前夜の絞り込み: 前日リターン + 価格帯
    # ⚠ レポートは **建値(当日の始値)** で価格を切るが、前夜には始値が無い。
    #    ここは **前日終値** で切る。境界付近は入れ替わる(既知のズレ)。
    _cand = [r for r in rows
             if r["ret1"] >= a.ret1
             and MIN_PRICE <= r["prev_close"] <= MAX_PRICE]
    _cand.sort(key=lambda r: (-r["liq"], r["symbol"]))
    for i, r in enumerate(_cand, 1):
        r["rank_liq"] = i
        r["watched"] = 1 if (a.watch <= 0 or i <= a.watch) else 0
    # ── J の候補を取り込む (--merge-j) ────────────────────────────
    # ⛔ kabu の有効トークンは1つ。J と N を別々の朝に読むことはできないので、
    #    **候補をマージして1回で読む**。板読みは1回、判定は後から何通りでも
    #    (CSV に gap_bp が入るので、+75bp でも +100bp でも再集計できる)。
    # ⚠ 50件の壁は共有される。J も N も単独で読むより少なくなる(避けられない)。
    _jset: set = set()
    if a.merge_j:
        _jcsv = Path(f"k_signals_{_YMD}.csv")
        if not _jcsv.exists():
            print(f"  ⚠ {_jcsv} がありません。先に "
                  f"`python k_open_confirm.py --collect` を実行してください。"
                  f"\n     J はマージせず N だけで続行します", flush=True)
        else:
            _jrows = {}
            for r in _csv.DictReader(open(_jcsv, encoding="utf-8-sig")):
                _sy = str(r.get("symbol") or "").strip()
                if not _sy:
                    continue
                _sy = _sy if _sy.endswith(".T") else f"{_sy}.T"
                _jset.add(_sy)
                if _sy not in _jrows:
                    try:
                        _jrows[_sy] = {
                            "symbol": _sy,
                            "prev_close": float(r.get("prev_close") or 0),
                            "liq": float(r.get("liquidity") or 0),
                            "prev_date": "", "ret1": float("nan")}
                    except Exception:
                        pass
            _have = {r["symbol"] for r in _cand}
            _add = [v for k, v in _jrows.items()
                    if k not in _have and v["prev_close"] > 0]
            _cand.extend(_add)
            print(f"  [merge-j] J の候補 {len(_jset):,}銘柄 → "
                  f"N に無い {len(_add):,}件を追加", flush=True)

    for r in _cand:
        r["in_n"] = 1 if (r.get("ret1") == r.get("ret1")      # NaN でない
                          and float(r.get("ret1") or 0) >= a.ret1) else 0
        r["in_j"] = 1 if r["symbol"] in _jset else 0

    # ⛔ 列は **k_signals_<日付>.csv 互換**にする。n_open_confirm.py(板読み)が
    #    `symbol` / `prev_close` / `liquidity` を読むので、名前を揃えないと
    #    流動性順に並べられず「銘柄コード順のまま読みます」に落ちる(§18.45 の事故)。
    for r in _cand:
        r["name"] = ""
        r["strategy"] = "N"
        r["order_price"] = 0.0       # N は寄りで判定するので注文価格を持たない
        r["atr"] = 0.0               # N はバリアが無いので ATR を使わない
        r["liquidity"] = r["liq"]
    with open(_SIG_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=[
            "symbol", "name", "strategy", "order_price", "prev_close", "atr",
            "liquidity", "prev_date", "ret1", "liq", "rank_liq", "watched",
            "in_j", "in_n"])
        w.writeheader()
        w.writerows(_cand)
    _nw = sum(r["watched"] for r in _cand)
    print(f"\n[collect] 読めた {len(rows):,}銘柄 → **候補 {len(_cand):,}件** "
          f"→ watch上限{a.watch}で **{_nw}件**", flush=True)
    if a.watch > 0 and len(_cand) > a.watch:
        print(f"  ⚠ 候補が上限を {len(_cand) - a.watch}件 超えました。"
              f"流動性の低い側を切っています(§18.44 の50件の壁)", flush=True)
    print(f"  → {_SIG_CSV}", flush=True)
    if _cand[:10]:
        print(f"\n  上位10件 (流動性降順):")
        print(f"    {'#':<4}{'銘柄':<10}{'前日終値':>10}{'前日%':>8}{'売買代金':>14}")
        for r in _cand[:10]:
            print(f"    {r['rank_liq']:<4}{r['symbol']:<10}{r['prev_close']:>10,.1f}"
                  f"{r['ret1']:>+8.2f}{r['liq'] / 1e8:>12,.1f}億")


# ══════════════════════════════════════════════════════════════════════
# ②③ --warmup / --prod : kabu の板を読む (⛔ 照会のみ)
# ══════════════════════════════════════════════════════════════════════
def _load_cand() -> list[dict]:
    if not _SIG_CSV.exists():
        sys.exit(f"[error] {_SIG_CSV} がありません。先に --collect を実行してください")
    out = []
    for r in _csv.DictReader(open(_SIG_CSV, encoding="utf-8-sig")):
        if str(r.get("watched", "1")) != "1":
            continue
        try:
            r["prev_close"] = float(r["prev_close"])
            r["ret1"] = float(r["ret1"])
            r["liq"] = float(r["liq"] or 0)
            r["rank_liq"] = int(r["rank_liq"])
        except Exception:
            continue
        out.append(r)
    return out


def _connect():
    """⛔ 照会専用。KabuClient は発注メソッドも持つが、**呼ばない**。"""
    try:
        from kabu_api import KabuClient
    except Exception as e:
        sys.exit(f"[error] kabu_api を import できません: {e}")
    cli = KabuClient(prod=bool(a.prod), dry_run=True)   # dry_run=True で二重に封じる
    cli.connect()
    return cli


def _read_boards(cli, cand: list[dict]) -> list[dict]:
    """全候補の /board を読む。⛔ 照会のみ。"""
    codes = [_code4(r["symbol"]) for r in cand]
    _t0 = _time.time()
    _reg = cli.register_many(codes)
    print(f"  登録 {len(codes)}件 → {_time.time() - _t0:.2f}s", flush=True)
    out, _fail = [], 0
    _t1 = _time.time()

    def _one(r):
        try:
            return r, cli.get_board(_code4(r["symbol"]))
        except Exception as e:
            return r, {"_err": str(e)}

    with ThreadPoolExecutor(max_workers=max(1, a.board_workers)) as ex:
        for r, bd in ex.map(_one, cand):
            if not bd or bd.get("_err"):
                _fail += 1
                out.append({**r, "open_p": 0.0, "open_time": "", "err": (bd or {}).get("_err", "?")})
                continue
            out.append({**r, "_bd": bd})
    print(f"  板読み {len(cand)}件 → {_time.time() - _t1:.2f}s"
          f"{f' / 失敗 {_fail}件' if _fail else ''}", flush=True)
    return out


def _judge(rows: list[dict]) -> list[dict]:
    """始値を取り出してギャップを判定する。

    ⛔⛔ **OpeningPriceTime の日付を必ず見る** (§18.48⑦ / 2026-08-20 の教訓)。
       /board は引け後も当日の OpeningPrice を返し続ける。09:00 にまだ寄って
       いない銘柄は **前日の** OpeningPrice を返すので、日付を見ないと
       前日の始値でギャップ判定してしまう。
    """
    out = []
    for r in rows:
        bd = r.pop("_bd", None)
        if bd is None:
            out.append({**r, "open_p": 0.0, "open_time": "", "stale_open": 1,
                        "gap_bp": 0.0, "pass": 0})
            continue
        _op = float(bd.get("OpeningPrice") or 0)
        _ot = str(bd.get("OpeningPriceTime") or "")
        _stale = 0
        if _ot:
            # ISO8601 "2026-08-26T09:00:00+09:00" の日付部分
            if _ot[:10] != _TODAY:
                _stale = 1
        elif _op > 0:
            _stale = 1              # 時刻が無いのに値がある = 判断できない
        _gap = ((_op - r["prev_close"]) / r["prev_close"] * 1e4
                if (_op > 0 and not _stale) else 0.0)
        _ok = 1 if (_op > 0 and not _stale and _gap >= a.gap_bp) else 0
        out.append({**r, "open_p": round(_op, 1), "open_time": _ot[11:19] if len(_ot) > 11 else "",
                    "stale_open": _stale, "gap_bp": round(_gap, 1), "pass": _ok,
                    "cur_price": float(bd.get("CurrentPrice") or 0)})
    return out


def do_board(warmup_only: bool) -> None:
    cand = _load_cand()
    if not cand:
        sys.exit(f"[error] {_SIG_CSV} に watch対象がありません")
    _now = datetime.now(JST)
    if not a.now and not warmup_only:
        if _now.hour < 9 or (_now.hour == 9 and _now.minute == 0 and _now.second < 2):
            print(f"[warn] まだ 09:00 前です ({_now:%H:%M:%S})。"
                  f"待たずに読むなら --now を付けてください", flush=True)
    print(f"\n[{'warmup' if warmup_only else 'board'}] "
          f"{_TODAY} / 候補 {len(cand)}件 / "
          f"{'本番(18080)' if a.prod else 'デモ(18081)'} / ⛔ 照会のみ・発注しない",
          flush=True)
    cli = _connect()
    rows = _read_boards(cli, cand)
    if warmup_only:
        _got = sum(1 for r in rows if r.get("_bd"))
        print(f"[warmup] 完了。{_got}/{len(rows)}件 読めました。"
              f"09:00 に `--prod` を実行してください", flush=True)
        return
    res = _judge(rows)
    _pass = [r for r in res if r["pass"] == 1]
    _late = [r for r in res if r["open_p"] <= 0 or r["stale_open"]]
    with open(_PAPER_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=[
            "symbol", "prev_date", "prev_close", "ret1", "liq", "rank_liq",
            "open_p", "open_time", "stale_open", "gap_bp", "pass", "cur_price"],
            extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(res, key=lambda r: (-r["pass"], -r["gap_bp"])))
    print(f"\n{'=' * 74}")
    print(f"■ 09:00 の判定 — {_TODAY}  (⛔ 発注していません)")
    print(f"{'=' * 74}")
    print(f"  読んだ候補       {len(res):>5}件")
    print(f"  09:00に寄らず/古い {len(_late):>5}件 "
          f"({len(_late) / max(1, len(res)) * 100:.0f}%)")
    print(f"  **合格 (ギャップ ≥ {a.gap_bp:.0f}bp)** {len(_pass):>5}件")
    if _pass:
        _need = sum(r["prev_close"] * QTY for r in _pass)
        print(f"  100株ずつ建てるなら 約 {_need / 1e4:,.0f}万円")
        print(f"\n  {'#':<4}{'銘柄':<10}{'前日終値':>10}{'始値':>10}"
              f"{'ギャップ':>10}{'前日%':>8}{'寄り時刻':>10}")
        print("  " + "-" * 62)
        for r in sorted(_pass, key=lambda x: -x["gap_bp"]):
            print(f"  {r['rank_liq']:<4}{r['symbol']:<10}{r['prev_close']:>10,.1f}"
                  f"{r['open_p']:>10,.1f}{r['gap_bp']:>+10.0f}"
                  f"{r['ret1']:>+8.2f}{r['open_time']:>10}")
    else:
        print(f"  → 今日は合格ゼロです")
    print(f"\n  → {_PAPER_CSV}")
    print(f"  引け後に `python n_paper.py --close` で損益を埋めてください")


# ══════════════════════════════════════════════════════════════════════
# ④ --close : 引け後に終値を埋めて損益を出す (日足だけ。kabu 不要)
# ══════════════════════════════════════════════════════════════════════
def do_close() -> None:
    """引け後: 板読み結果に終値をつけ、**J と N を別々に**集計する。

    ⛔ 板読みは1回きり(kabu のトークンは1つ)。だから判定は後からやる。
       CSV に gap_bp が入っているので、+75bp(J) でも +100bp(N) でも
       同じデータから再集計できる。
    """
    _pcsv = Path(a.paper_csv) if a.paper_csv else Path(f"k_paper_{_YMD}.csv")
    if not _pcsv.exists():
        _alt = _PAPER_CSV
        if _alt.exists():
            _pcsv = _alt
        else:
            sys.exit(f"[error] {_pcsv} も {_alt} もありません。"
                     f"先に朝の板読み(.\\norder)を実行してください")
    rows = list(_csv.DictReader(open(_pcsv, encoding="utf-8-sig")))
    if not rows:
        sys.exit(f"[error] {_pcsv} が空です")
    print(f"[close] {_pcsv} を読みました ({len(rows)}件)", flush=True)

    # ── 候補CSVから in_j / in_n を復元 ────────────────────────────
    _flag: dict = {}
    if _SIG_CSV.exists():
        for r in _csv.DictReader(open(_SIG_CSV, encoding="utf-8-sig")):
            _sy = str(r.get("symbol") or "").strip()
            _sy = _sy if _sy.endswith(".T") else f"{_sy}.T"
            _flag[_sy.replace(".T", "")] = (
                int(r.get("in_j") or 0), int(r.get("in_n") or 0))
    else:
        print(f"  ⚠ {_SIG_CSV} が無いので in_j/in_n を復元できません。"
              f"全件を N として集計します", flush=True)

    import backtest_limit_entry as ble
    _out = []
    for r in rows:
        _sy = str(r.get("symbol") or "").strip().replace(".T", "")
        _yf = f"{_sy}.T"
        try:
            _op = float(r.get("open_p") or 0)
            _gap = float(r.get("gap_bp") or 0)
        except Exception:
            _op, _gap = 0.0, 0.0
        _ij, _in = _flag.get(_sy, (0, 1))
        r["in_j"], r["in_n"] = _ij, _in
        r["close_p"], r["pnl"], r["bp"] = "", "", ""
        if _op > 0:
            try:
                df = ble.fetch(_yf, 40)
                _idx = pd.to_datetime(df.index).normalize()
                _ts = pd.Timestamp(_TODAY)
                if _ts in _idx:
                    _cl = float(df["close"].iloc[int(_idx.searchsorted(_ts))])
                    _pnl = (_op - _cl) * QTY
                    r["close_p"] = round(_cl, 1)
                    r["pnl"] = round(_pnl, 0)
                    r["bp"] = round(_pnl / (_op * QTY) * 1e4, 1)
            except Exception:
                pass
        _out.append(r)

    _fld = list(_out[0].keys())
    _o2 = Path(f"n_close_{_YMD}.csv")
    with open(_o2, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=_fld, extrasaction="ignore")
        w.writeheader()
        w.writerows(_out)

    def _score(tag: str, gap_min: float, key: str):
        _sel = [r for r in _out
                if int(r.get(key) or 0) == 1
                and r.get("pnl") != ""
                and float(r.get("gap_bp") or 0) >= gap_min]
        print(f"\n{'=' * 74}")
        print(f"■ {tag} — ギャップ ≥ {gap_min:+.0f}bp  ({_TODAY} / ペーパー)")
        print(f"{'=' * 74}")
        if not _sel:
            print(f"  合格ゼロ (または終値がまだ取れません)")
            return
        _tot = sum(float(r["pnl"]) for r in _sel)
        _bpm = sum(float(r["bp"]) for r in _sel) / len(_sel)
        _win = sum(1 for r in _sel if float(r["pnl"]) > 0)
        print(f"  {'銘柄':<10}{'始値':>10}{'終値':>10}{'損益':>10}"
              f"{'bp':>8}{'ギャップ':>10}")
        print("  " + "-" * 58)
        for r in sorted(_sel, key=lambda x: -float(x["pnl"])):
            print(f"  {str(r['symbol']):<10}{float(r['open_p']):>10,.1f}"
                  f"{float(r['close_p']):>10,.1f}{float(r['pnl']):>+10,.0f}"
                  f"{float(r['bp']):>+8.1f}{float(r['gap_bp']):>+10.0f}")
        print("  " + "-" * 58)
        print(f"  {len(_sel)}件 / 勝ち {_win}件 / **合計 {_tot:+,.0f}円** / "
              f"平均 {_bpm:+.1f}bp")

    _score("★ 新方式 N", a.gap_bp, "in_n")
    _score("J (参考・記録のみ)", a.gap_bp_j, "in_j")

    _both = [r for r in _out
             if int(r.get("in_j") or 0) == 1 and int(r.get("in_n") or 0) == 1]
    print(f"\n  候補の重なり: J∩N {len(_both)}件 / "
          f"J のみ {sum(1 for r in _out if int(r.get('in_j') or 0) == 1 and int(r.get('in_n') or 0) == 0)}件 / "
          f"N のみ {sum(1 for r in _out if int(r.get('in_n') or 0) == 1 and int(r.get('in_j') or 0) == 0)}件")
    print(f"\n  ⚠ これは **ペーパー**(発注していない)。摩擦ゼロの理論値です。")
    print(f"  ⚠ 50件の壁は J と N で **共有**しています。"
          f"単独で読むより両方とも少なくなります(kabu の制約 / §18.44)")
    print(f"  → {_o2}")


if a.collect:
    do_collect()
elif a.close:
    do_close()
elif a.warmup:
    do_board(warmup_only=True)
else:
    do_board(warmup_only=False)
