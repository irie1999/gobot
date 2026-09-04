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
import re as _re
import os
import subprocess
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
ap.add_argument("--mirror", action="store_true", default=False,
                help="鏡像(前日下げ × ギャップダウンを買う)も記録する。"
                     "既定OFF。Nの秒単位順序を1バッチで測るときは使わない")
ap.add_argument("--no-mirror", dest="mirror", action="store_false",
                help="鏡像を入れない(既定。norder もこの形)")
ap.add_argument("--merge-j", action="store_true",
                help="★ J の候補(k_signals_<日付>.csv)も取り込んでマージする。"
                     "⛔ kabu の有効トークンは1つなので J と N を別々には"
                     "読めない。**1回の板読みで両方記録する**ための指定。"
                     "先に `python k_open_confirm.py --collect` を実行しておくこと")
ap.add_argument("--sequence", action="store_true",
                help="★ 板読みCSVから **実際に発注が走る順番**を再現して出す"
                     "(kabu 不要。引け前でも見られる)")
ap.add_argument("--budget", type=float, default=400.0,
                help="--sequence の予算(万円)")
ap.add_argument("--seq-sides", type=str, default="n",
                help="--sequence で1つの予算を共有する側。"
                     "n=N のみ(既定) / m=鏡像のみ / nm=両建て。"
                     "鏡像は記録するが予算はN単独へ配る")
ap.add_argument("--paper-csv", type=str, default="",
                help="--close が読む板読み結果。"
                     "既定 k_paper_<日付>.csv (n_open_confirm.py の出力)")
ap.add_argument("--guard-bp-j", type=float, default=300.0,
                help="J のギャップ上限(bp)。これを超えたら見送り(§18.32)。"
                     "⛔ N と鏡像には上限が無い(§18.55 で棄却済み)")
ap.add_argument("--gap-bp-j", type=float, default=75.0,
                help="--close で J として集計するギャップ(bp)。既定75")
a = ap.parse_args()

_TODAY = a.date or datetime.now(JST).strftime("%Y-%m-%d")
_YMD = _TODAY.replace("-", "")
_SIG_CSV = Path(f"n_signals_{_YMD}.csv")
_PAPER_CSV = Path(f"n_paper_{_YMD}.csv")


def _repo_provenance() -> dict[str, str]:
    """この記録・採点を作ったコードの出所を返す。

    未追跡のCSVは実行のたびに増えるため dirty 判定から除き、追跡済みコードの
    変更だけを ``+dirty`` として残す。
    """
    _cwd = Path(__file__).resolve().parent

    def _git(*args: str) -> str:
        try:
            _p = subprocess.run(
                ["git", *args], cwd=_cwd, capture_output=True, text=True,
                timeout=5, check=False)
            return _p.stdout.strip() if _p.returncode == 0 else ""
        except Exception:
            return ""

    _commit = _git("rev-parse", "--short", "HEAD") or "unknown"
    _branch = _git("branch", "--show-current") or "unknown"
    if _git("status", "--porcelain", "--untracked-files=no"):
        _commit += "+dirty"
    return {"src_commit": _commit, "src_branch": _branch}


def _csv_provenance(rows: list[dict]) -> dict[str, str]:
    """CSVの先頭行から出所を読む。旧CSVは空文字のまま扱う。"""
    if not rows:
        return {"src_commit": "", "src_branch": ""}
    return {"src_commit": str(rows[0].get("src_commit") or "").strip(),
            "src_branch": str(rows[0].get("src_branch") or "").strip()}


def _show_close_provenance(label: str, src: dict[str, str],
                           current: dict[str, str], *, selecting: bool) -> None:
    """--close の出所照合を表示する。観測CSVは不一致でも利用を続ける。"""
    _sc, _sb = src.get("src_commit", ""), src.get("src_branch", "")
    _cc, _cb = current["src_commit"], current["src_branch"]
    if not _sc:
        _msg = "出所列なし"
        if selecting:
            print(f"  ⚠ {label}: {_msg}。候補選定コードを特定できません（採点は続行）",
                  flush=True)
        else:
            print(f"  [出所] {label}: {_msg}。板の観測値として利用します", flush=True)
        return
    _diff = _sc.removesuffix("+dirty") != _cc.removesuffix("+dirty")
    _branch_diff = bool(_sb and _cb and _sb != _cb)
    _dirty = _sc.endswith("+dirty")
    _detail = f"{_sb or '?'}@{_sc} / 採点 {_cb}@{_cc}"
    if selecting and (_diff or _branch_diff or _dirty):
        _why = []
        if _diff:
            _why.append("commit不一致")
        if _branch_diff:
            _why.append("branch不一致")
        if _dirty:
            _why.append("候補作成時dirty")
        print(f"  ⚠ {label}: {', '.join(_why)} — {_detail}（採点は続行）",
              flush=True)
    else:
        _suffix = "（不一致でも観測値として利用）" if not selecting else ""
        print(f"  [出所] {label}: {_detail}{_suffix}", flush=True)


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



def _hhmmss(v) -> str:
    """ISO文字列から HH:MM:SS を取り出す。

    ⛔ 末尾8文字を取ると **タイムゾーン**("00+09:00")になる
       (2026-08-27 に実際に表示が壊れた)。
       "2026-08-27T09:03:12+09:00" → "09:03:12"
    """
    _m = _re.search(r"T(\d{2}:\d{2}:\d{2})", str(v or ""))
    if _m:
        return _m.group(1)
    _m = _re.search(r"\b(\d{2}:\d{2}:\d{2})\b", str(v or ""))
    return _m.group(1) if _m else ""

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
    _N_CAND = len(_cand)               # ★ 絞る前の真の候補数(表示用)
    for i, r in enumerate(_cand, 1):
        r["rank_liq"] = i
        r["watched"] = 1 if (a.watch <= 0 or i <= a.watch) else 0
        r["rank_n"], r["watched_n"] = i, r["watched"]
        r["rank_m"], r["watched_m"] = 0, 0
    # ── ★ 鏡像 (前日下げ × ギャップダウンを買う) ────────────────────
    #   §18.55: TRAIN で N と同水準(月+47,839 vs +49,587)。符号を反転するだけ。
    #   ⚠ 同じ銘柄が N と鏡像の両方に入ることは無い(前日リターンが
    #     +1.753% 以上かつ -1.753% 以下になることはないため)。
    _mir = []
    if a.mirror:
        _mir = [r for r in rows
                if r["ret1"] <= -a.ret1
                and MIN_PRICE <= r["prev_close"] <= MAX_PRICE]
        _mir.sort(key=lambda r: (-r["liq"], r["symbol"]))
        for i, r in enumerate(_mir, 1):
            r["rank_m"], r["watched_m"] = i, 1 if (a.watch <= 0 or i <= a.watch) else 0
            r["rank_liq"], r["watched"] = 0, 0
            r["rank_n"], r["watched_n"] = 0, 0
        _cand.extend(_mir)
        print(f"  [mirror] 鏡像の候補 {len(_mir):,}件 "
              f"(前日リターン ≤ -{a.ret1}%)", flush=True)
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
            # ⛔⛔ **k_signals の prev_close は空**(2026-08-27 に発覚)。
            #   k_open_confirm._collect は fieldnames に prev_close を入れて
            #   いるが値を書いていないので、DictWriter が空文字を出す。
            #   ここで `prev_close > 0` を条件にすると **J 固有の銘柄が
            #   1件も追加されない**。実測 2026-08-27: J の67銘柄のうち
            #   N/鏡像と重なった24件しか読まず、43件を取り逃した。
            #   → 自前のスキャン結果(rows)から前日終値を引く。
            #     rows は1,540銘柄ぶん prev_close / liq / ret1 を持っている。
            _byc = {r["symbol"]: r for r in rows}
            _jrows, _nofill = {}, []
            for r in _csv.DictReader(open(_jcsv, encoding="utf-8-sig")):
                _sy = str(r.get("symbol") or "").strip()
                if not _sy:
                    continue
                _sy = _sy if _sy.endswith(".T") else f"{_sy}.T"
                _jset.add(_sy)
                if _sy in _jrows:
                    continue
                _src = _byc.get(_sy)
                try:
                    _pc = float(r.get("prev_close") or 0)
                except Exception:
                    _pc = 0.0
                if _pc <= 0 and _src:
                    _pc = float(_src["prev_close"])       # ★ 自前の値で補う
                try:
                    _lq = float(r.get("liquidity") or 0)
                except Exception:
                    _lq = 0.0
                if _lq <= 0 and _src:
                    _lq = float(_src["liq"])
                if _pc <= 0:
                    _nofill.append(_sy)                   # 日足が引けなかった
                    continue
                _jrows[_sy] = {"symbol": _sy, "prev_close": _pc, "liq": _lq,
                               "prev_date": (_src or {}).get("prev_date", ""),
                               "ret1": (_src or {}).get("ret1", float("nan"))}
            _have = {r["symbol"] for r in _cand}
            _add = [v for k, v in _jrows.items() if k not in _have]
            _cand.extend(_add)
            print(f"  [merge-j] J の候補 {len(_jset):,}銘柄 → "
                  f"N/鏡像に無い {len(_add):,}件を追加"
                  + (f" / 前日終値が引けず除外 {len(_nofill)}件" if _nofill else ""),
                  flush=True)
            if len(_jset) and not _add:
                print(f"  ⚠ J の追加が0件です。J の候補が全部 N/鏡像と"
                      f"重なっていない限り、これは異常です", flush=True)

    for r in _cand:
        _r1 = r.get("ret1")
        _ok = _r1 == _r1 and _r1 is not None                  # NaN でない
        r["in_n"] = 1 if (_ok and float(_r1) >= a.ret1) else 0
        r["in_m"] = 1 if (_ok and float(_r1) <= -a.ret1) else 0
        r["in_j"] = 1 if r["symbol"] in _jset else 0
        r.setdefault("rank_n", 0); r.setdefault("watched_n", 0)
        r.setdefault("rank_m", 0); r.setdefault("watched_m", 0)
        r.setdefault("rank_liq", 0); r.setdefault("watched", 0)
        r["rank_j"], r["watched_j"] = 0, 0
    # ★ J も自分の候補の中で上位50を持つ(N・鏡像と同じ扱い)。
    #   ⛔ 「3方式で50件を分け合う」ではなく **それぞれ50件**
    #      (2026-08-26 ユーザー指示)。板読みは50件バッチのローテーションで
    #      回せるうえ、板の始値は寄れば動かないので遅れても選定は正しい。
    _jc = sorted([r for r in _cand if r["in_j"] == 1],
                 key=lambda r: (-float(r.get("liq") or 0), r["symbol"]))
    for i, r in enumerate(_jc, 1):
        r["rank_j"] = i
        r["watched_j"] = 1 if (a.watch <= 0 or i <= a.watch) else 0
    # 読むのは **3方式の上位50の和集合**だけ。全候補を読むと429を招く(§18.48 ⑦)
    _n_all = len(_cand)
    _J_CAND = sum(1 for r in _cand if r["in_j"])
    # ⛔ ここで **読む対象**に絞る。以降の _cand は全候補ではないので、
    #   件数を表示するときは _N_CAND / len(_mir) / _J_CAND を使うこと
    #   (2026-08-27: 絞ったあとの数を『候補』と表示して 107件を62件と誤報した)。
    _cand = [r for r in _cand
             if r["watched_n"] or r["watched_m"] or r["watched_j"]]
    print(f"  [読む対象] 候補 {_n_all:,}件 → 3方式の上位{a.watch}の和集合 "
          f"**{len(_cand):,}銘柄** "
          f"({-(-len(_cand) // 50)}バッチ / 板の始値は寄れば動かないので"
          f"読むのが遅れても選定は正しい)", flush=True)

    # ⛔ 列は **k_signals_<日付>.csv 互換**にする。n_open_confirm.py(板読み)が
    #    `symbol` / `prev_close` / `liquidity` を読むので、名前を揃えないと
    #    流動性順に並べられず「銘柄コード順のまま読みます」に落ちる(§18.45 の事故)。
    for r in _cand:
        r["name"] = ""
        r["strategy"] = "N"
        r["order_price"] = 0.0       # N は寄りで判定するので注文価格を持たない
        r["atr"] = 0.0               # N はバリアが無いので ATR を使わない
        r["liquidity"] = r["liq"]
    _prov = _repo_provenance()
    for r in _cand:
        r.update(_prov)
    with open(_SIG_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=[
            "symbol", "name", "strategy", "order_price", "prev_close", "atr",
            "liquidity", "prev_date", "ret1", "liq", "rank_liq", "watched",
            "in_j", "in_n", "in_m", "rank_n", "watched_n",
            "rank_m", "watched_m", "rank_j", "watched_j",
            "src_commit", "src_branch"])
        w.writeheader()
        w.writerows(_cand)
    _nw = sum(r["watched"] for r in _cand)
    print(f"\n[collect] 読めた {len(rows):,}銘柄 → **読む対象 {len(_cand):,}銘柄** "
          f"(N {sum(r['watched_n'] for r in _cand)}件 / "
          f"鏡像 {sum(r['watched_m'] for r in _cand)}件 / "
          f"J {sum(r['watched_j'] for r in _cand)}件。重複は1回だけ読む)",
          flush=True)
    print(f"  → {_SIG_CSV}", flush=True)
    # ⛔ 板読みは **全候補**を読む(n_open_confirm が50件バッチで回す)。
    #    板の始値は寄れば動かないので、遅れて読んでも選定は正しい。
    #    50件の壁が効くのは **実発注**のときだけなので、方式ごとに上位50件を示す。
    def _show(tag: str, rows: list, rk: str, wk: str, total: int):
        """total = **絞る前**の候補数。rows は読む対象に絞ったあとなので、
        そのまま数えると候補を過少に表示する(2026-08-27 に実際に誤報)。"""
        _r = sorted([x for x in rows if int(float(x.get(rk) or 0)) > 0],
                    key=lambda x: int(float(x[rk])))
        if not _r:
            print(f"\n  {tag}: 候補ゼロ")
            return
        _n = sum(1 for x in _r if int(float(x.get(wk) or 0)) == 1)
        print(f"\n  {tag}: 候補 {total}件 → **建てられるのは上位 {_n}件**"
              + (f" (残り {total - _n}件は kabu の登録上限 / §18.44)"
                 if total > _n else ""))
        print(f"    {'#':<4}{'銘柄':<10}{'前日終値':>10}{'前日%':>8}{'売買代金':>14}")
        for x in _r[:10]:
            print(f"    {int(float(x[rk])):<4}{x['symbol']:<10}"
                  f"{x['prev_close']:>10,.1f}{x['ret1']:>+8.2f}"
                  f"{x['liq'] / 1e8:>12,.1f}億")
        if len(_r) > 10:
            print(f"    … 他 {len(_r) - 10}件")
    _show("★ N (ギャップアップを売る)", _cand, "rank_n", "watched_n", _N_CAND)
    if a.mirror:
        _show("★ 鏡像 (ギャップダウンを買う)", _cand, "rank_m", "watched_m",
              len(_mir))
    if _J_CAND:
        _show("J (参考・記録のみ)", _cand, "rank_j", "watched_j", _J_CAND)
    print(f"\n  ⚠ ここまでは **前夜の候補**。実際にどれが選ばれるかは"
          f"09:00 の始値(ギャップ)で決まります")


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

    # 板CSVは観測値なので出所が違っても利用する。候補CSVは選定結果なので、
    # 作成コードが違う/dirtyなら警告する。ただし過去記録を失わないよう弾かない。
    _current_prov = _repo_provenance()
    _show_close_provenance(_pcsv.name, _csv_provenance(rows), _current_prov,
                           selecting=False)

    # ★ 先に「どの銘柄が合格し、どの順で発注が走ったか」を出す(2026-08-27)。
    #   --sequence と同じもの。CSV を読むだけなので引け後に1回でまとまる。
    #   ⛔ ここで落ちても損益の集計は続ける(表示のための機能なので)。
    _seq = None
    try:
        _seq = do_sequence()
    except SystemExit:
        raise
    except Exception as _e:
        print(f"  ⚠ 発注シーケンスの表示に失敗: {type(_e).__name__}: {_e}",
              flush=True)

    # ── 候補CSVから in_j / in_n を復元 ────────────────────────────
    _flag: dict = {}
    _sig_rows = []
    if _SIG_CSV.exists():
        _sig_rows = list(_csv.DictReader(open(_SIG_CSV, encoding="utf-8-sig")))
        _show_close_provenance(_SIG_CSV.name, _csv_provenance(_sig_rows),
                               _current_prov, selecting=True)
        for r in _sig_rows:
            _sy = str(r.get("symbol") or "").strip()
            _sy = _sy if _sy.endswith(".T") else f"{_sy}.T"
            _flag[_sy.replace(".T", "")] = {
                "in_j": int(r.get("in_j") or 0),
                "in_n": int(r.get("in_n") or 0),
                "in_m": int(r.get("in_m") or 0),
                "rank_n": int(float(r.get("rank_n") or 0)),
                "rank_m": int(float(r.get("rank_m") or 0)),
                "rank_j": int(float(r.get("rank_j") or 0))}
    else:
        print(f"  ⚠ {_SIG_CSV} が無いので in_j/in_n を復元できません。"
              f"全件を N として集計します", flush=True)

    import backtest_limit_entry as ble
    # ── 終値が確定値か暫定値かを必ず言う (2026-09-04 の事故対策) ──────
    #   大引け 15:30 直後に叩くと yfinance は 15:29 頃の最終約定値を返すことが
    #   あり、それが「今日のバー」としてキャッシュに焼き付く。9/4 は 6銘柄
    #   すべてが実際の MOC 約定値と 3〜11円ずれ、ペーパー損益を +3,450円
    #   (17.4bp) 水増ししていた。**黙って使わない。必ず件数を出す。**
    _prov_syms: list = []
    _out = []
    for r in rows:
        _sy = str(r.get("symbol") or "").strip().replace(".T", "")
        _yf = f"{_sy}.T"
        try:
            _op = float(r.get("open_p") or 0)
            _gap = float(r.get("gap_bp") or 0)
        except Exception:
            _op, _gap = 0.0, 0.0
        _fl = _flag.get(_sy, {"in_j": 0, "in_n": 1, "in_m": 0,
                              "rank_n": 0, "rank_m": 0, "rank_j": 0})
        r["in_j"], r["in_n"], r["in_m"] = _fl["in_j"], _fl["in_n"], _fl["in_m"]
        r["rank_n"], r["rank_m"] = _fl["rank_n"], _fl["rank_m"]
        r["rank_j"] = _fl.get("rank_j", 0)
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
                    if ble.is_close_provisional(_yf, _TODAY):
                        _prov_syms.append(_sy)
            except Exception:
                pass
        _out.append(r)

    if _prov_syms:
        _fa = ble.cache_fetched_at(f"{_prov_syms[0]}.T")
        _hh, _mm = ble._close_settle_hhmm()
        print(f"\n  ⛔⛔ **終値が暫定値です** — {len(_prov_syms)}件 / "
              f"{len(_out)}件中", flush=True)
        print(f"     キャッシュ取得 {_fa:%H:%M} < 確定 {_hh:02d}:{_mm:02d} JST。"
              f"大引け直後の値なので MOC 約定値と数円ずれます", flush=True)
        print(f"     → **以下の損益はすべて暫定**。"
              f"{_hh:02d}:{_mm:02d} 以降に再実行すると自動で確定値に直ります",
              flush=True)
        print(f"     いま直すなら: $env:GOBOT_REFRESH_DATA=\"1\"; "
              f"python n_paper.py --close --date {_TODAY}", flush=True)

    _fld = list(_out[0].keys())
    _o2 = Path(f"n_close_{_YMD}.csv")
    with open(_o2, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=_fld, extrasaction="ignore")
        w.writeheader()
        w.writerows(_out)

    def _score(tag: str, gap_min: float, key: str, side: int = 1,
               rank_key: str = "", top: int = 0, guard: float = 0.0):
        """side=+1 ショート(ギャップアップを売る) / -1 ロング(ギャップダウンを買う)。

        ⛔ 板読みは全候補を読むが、**実運用で建てられるのは各方式の上位50件**
           (kabu の登録上限 / §18.44)。top を渡してそこで切る。
        """
        _sel = [r for r in _out
                if int(r.get(key) or 0) == 1
                and r.get("pnl") != ""
                and (float(r.get("gap_bp") or 0) >= gap_min if side > 0
                     else float(r.get("gap_bp") or 0) <= -gap_min)]
        if top > 0 and rank_key:
            _sel = [r for r in _sel
                    if 0 < int(float(r.get(rank_key) or 0)) <= top]
        # ⛔ **合格銘柄の一覧(上)と同じガードを掛けること。**
        #   掛け忘れると同じ実行の中で「合格7件」と「損益10件」が並ぶ
        #   (2026-08-27 に実際に出た)。
        if guard > 0:
            _sel = [r for r in _sel if abs(float(r.get("gap_bp") or 0)) <= guard]
        for r in _sel:                      # ロングは損益の符号が逆
            r["_p"] = float(r["pnl"]) * side
            r["_b"] = float(r["bp"]) * side
        _gl = (f"≥ +{gap_min:.0f}bp" if side > 0 else f"≤ -{gap_min:.0f}bp")
        print(f"\n{'=' * 74}")
        print(f"■ {tag} — ギャップ {_gl}  ({_TODAY} / ペーパー)"
              + (f" / 上位{top}件" if top > 0 else ""))
        print(f"{'=' * 74}")
        if not _sel:
            print(f"  合格ゼロ (または終値がまだ取れません)")
            return
        _tot = sum(r["_p"] for r in _sel)
        _bpm = sum(r["_b"] for r in _sel) / len(_sel)
        _win = sum(1 for r in _sel if r["_p"] > 0)
        print(f"  {'銘柄':<10}{'始値':>10}{'終値':>10}{'損益':>10}"
              f"{'bp':>8}{'ギャップ':>10}")
        print("  " + "-" * 58)
        for r in sorted(_sel, key=lambda x: -x["_p"]):
            print(f"  {str(r['symbol']):<10}{float(r['open_p']):>10,.1f}"
                  f"{float(r['close_p']):>10,.1f}{r['_p']:>+10,.0f}"
                  f"{r['_b']:>+8.1f}{float(r['gap_bp']):>+10.0f}")
        print("  " + "-" * 58)
        print(f"  {len(_sel)}件 / 勝ち {_win}件 / **合計 {_tot:+,.0f}円** / "
              f"平均 {_bpm:+.1f}bp")

    _score("★ 新方式 N (ギャップアップを売る)", a.gap_bp, "in_n",
           side=1, rank_key="rank_n", top=a.watch)
    _score("★ 鏡像 (ギャップダウンを買う)", a.gap_bp, "in_m",
           side=-1, rank_key="rank_m", top=a.watch)
    _score("J (参考・記録のみ)", a.gap_bp_j, "in_j",
           side=1, rank_key="rank_j", top=a.watch, guard=a.guard_bp_j)

    # ══════════════════════════════════════════════════════════════════
    # ★★ 予算制約つきの損益 — **これが実際に取れた額**
    # ══════════════════════════════════════════════════════════════════
    #   ⛔ 上の方式別の表は「合格を全部建てられたら」の値。
    #     予算400万では建てられない銘柄があるので、実額とは違う。
    #     2026-08-27 の初日に、見送った3件が全部勝ちで +29,000円 だった。
    if _seq and _seq.get("built"):
        _pnl_of = {}
        for r in _out:
            _sy = str(r.get("symbol") or "").strip().replace(".T", "")
            if r.get("pnl") not in ("", None):
                _pnl_of[_sy] = float(r["pnl"])
        def _sum(lst):
            _v = [(sy, sd, _pnl_of[sy] * sd) for sy, sd in lst if sy in _pnl_of]
            return _v, sum(x[2] for x in _v)
        _bv, _btot = _sum(_seq["built"])
        _sv, _stot = _sum(_seq["skip"])
        print(f"\n{'=' * 74}")
        print(f"■ ★★ 予算制約つき — **実際に取れた額** ({_TODAY} / ペーパー)")
        print(f"{'=' * 74}")
        _ws = sum(1 for _, _, p in _bv if p > 0)
        print(f"  建てた {len(_bv)}件 / 勝ち {_ws}件 / **合計 {_btot:+,.0f}円**")
        _ns = sum(p for _, sd, p in _bv if sd > 0)
        _nl = sum(p for _, sd, p in _bv if sd < 0)
        print(f"    内訳: N(売り) {_ns:+,.0f}円 / 鏡像(買い) {_nl:+,.0f}円")
        if _sv:
            print(f"\n  ⛔ **予算で見送った {len(_sv)}件 = {_stot:+,.0f}円**")
            for _sy, _sd, _p in sorted(_sv, key=lambda x: -x[2]):
                print(f"       {_sy:<8}{'売り' if _sd > 0 else '買い':<6}{_p:>+10,.0f}円")
            if _stot > 0:
                print(f"     → 見送った側が勝っています。"
                      f"**待たない方針のコストが {_stot:+,.0f}円**")
            else:
                print(f"     → 見送った側が負けています。今日は待たなくて正解でした")
        print(f"\n  ⚠ 方式別の表(上)は『合格を全部建てられたら』の値なので、"
              f"この額とは違います")

    _nm = sum(1 for r in _out
              if int(r.get("in_n") or 0) == 1 and int(r.get("in_m") or 0) == 1)
    if _nm:
        print(f"\n  ⛔ N と鏡像に同じ銘柄が {_nm} 件あります。"
              f"前日リターンが +{a.ret1}% 以上かつ -{a.ret1}% 以下は"
              f"有り得ないので、**集計が壊れています**")
    _both = [r for r in _out
             if int(r.get("in_j") or 0) == 1 and int(r.get("in_n") or 0) == 1]
    print(f"\n  候補の重なり: J∩N {len(_both)}件 / "
          f"J のみ {sum(1 for r in _out if int(r.get('in_j') or 0) == 1 and int(r.get('in_n') or 0) == 0)}件 / "
          f"N のみ {sum(1 for r in _out if int(r.get('in_n') or 0) == 1 and int(r.get('in_j') or 0) == 0)}件")
    print(f"  鏡像の候補: {sum(1 for r in _out if int(r.get('in_m') or 0) == 1)}件")
    print(f"\n  ⚠ これは **ペーパー**(発注していない)。摩擦ゼロの理論値です。")
    print(f"  ⚠ 3方式は **1回の板読み**を共有しています。板の始値は寄れば動かない"
          f"ので、\n     読むのが遅れても『どれが選ばれるか』は正しく出ます"
          f"(執行の速さが要るのは実発注のときだけ)。")
    print(f"  ⚠ 各方式は **それぞれ上位50件**を監視しています(共有ではない)。"
          f"\n     板読みは50件バッチのローテーションで回します(§18.44)")
    print(f"  → {_o2}")


# ══════════════════════════════════════════════════════════════════════
# ⑤ --sequence : 実際に発注が走る順番を再現する (kabu 不要)
# ══════════════════════════════════════════════════════════════════════
def do_sequence() -> None:
    """板読みCSVから **発注シーケンス**を作る。

    ★ ライブは「全部が寄るのを待たない」(§18.38 の『即時』)。寄った銘柄から
      順に配るので、順番は

        ① 寄った時刻のグループ順(先に寄ったグループが先)
        ② グループの中では |ギャップ| の大きい順

      になる。バックテストは **その日を丸ごと** |ギャップ|降順に並べるので、
      **遅く寄った大ギャップ銘柄はライブのほうが不利な順位**になる。
      その差もここで出す。

    ⛔ 株数は **100株固定**(バックテスト `_ops_sim` の a.qty と同じ)。
       資金均等ではない。J だけは資金均等なので別枠で扱う(§18.48)。
    """
    _pcsv = Path(a.paper_csv) if a.paper_csv else Path(f"k_paper_{_YMD}.csv")
    if not _pcsv.exists():
        _alt = _PAPER_CSV
        if not _alt.exists():
            sys.exit(f"[error] {_pcsv} も {_alt} もありません。"
                     f"先に朝の板読み(.\\norder)を実行してください")
        _pcsv = _alt
    rows = list(_csv.DictReader(open(_pcsv, encoding="utf-8-sig")))
    if not rows:
        sys.exit(f"[error] {_pcsv} が空です")

    _flag: dict = {}
    if _SIG_CSV.exists():
        for r in _csv.DictReader(open(_SIG_CSV, encoding="utf-8-sig")):
            _sy = str(r.get("symbol") or "").strip().replace(".T", "")
            _flag[_sy] = {k: int(float(r.get(k) or 0)) for k in
                          ("in_n", "in_m", "in_j", "rank_n", "rank_m", "rank_j")}
    else:
        print(f"  ⚠ {_SIG_CSV} が無いので方式を判別できません", flush=True)

    _want = str(a.seq_sides).lower()
    _cand = []
    for r in rows:
        _sy = str(r.get("symbol") or "").strip().replace(".T", "")
        _fl = _flag.get(_sy, {})
        try:
            _op = float(r.get("open_p") or 0)
            _gap = float(r.get("gap_bp") or 0)
        except Exception:
            continue
        if _op <= 0:
            continue                      # 寄っていない
        # 方式の判定。**その方式の上位50件に入っているものだけ**建てられる
        _sd = 0
        if "n" in _want and _fl.get("in_n") and 0 < _fl.get("rank_n", 0) <= a.watch \
                and _gap >= a.gap_bp:
            _sd = 1
        elif "m" in _want and _fl.get("in_m") and 0 < _fl.get("rank_m", 0) <= a.watch \
                and _gap <= -a.gap_bp:
            _sd = -1
        if _sd == 0:
            continue
        _cand.append({
            "sym": _sy, "side": _sd, "gap": _gap, "op": _op,
            "grp": int(float(r.get("grp") or 0)),
            "ts": str(r.get("seen_ts") or ""),
            "ot": str(r.get("open_time") or ""),
            "late": int(float(r.get("late") or 0))})

    # ── ★ まず「3方式それぞれ、どの銘柄が合格したか」 ──────────────
    #   資金の割り振りとは別に、**選定そのもの**を見たいことが多い。
    #   J は資金均等で株数の決め方が違うので発注シーケンスには入れないが、
    #   合格銘柄はここで出す。
    def _pass_list(tag: str, ik: str, rk: str, gmin: float, up: bool,
                   guard: float = 0.0):
        """guard>0 なら |ギャップ| がそれを超える銘柄を見送る。

        ⛔ **方式ごとに違う。** J は +300bp 超を見送る(§18.32/§18.48)が、
           N は上限を持たない(§18.55 で --max-gap-bp 150 を棄却)。
           ここを揃えてしまうと、どちらかがバックテストと食い違う。
        """
        _r, _ng = [], []
        for x in rows:
            _sy = str(x.get("symbol") or "").strip().replace(".T", "")
            _fl = _flag.get(_sy, {})
            if not _fl.get(ik):
                continue
            if not (0 < _fl.get(rk, 0) <= a.watch):
                continue                  # その方式の上位50件の外
            try:
                _g = float(x.get("gap_bp") or 0)
                _o = float(x.get("open_p") or 0)
            except Exception:
                continue
            if _o <= 0:
                continue                  # 寄っていない
            if guard > 0 and abs(_g) > guard:
                _ng.append((_sy, _g))
                continue
            if (_g >= gmin) if up else (_g <= -gmin):
                _r.append((_sy, _g, _o))
        _r.sort(key=lambda t: -abs(t[1]))
        _n_watch = sum(1 for x in rows
                       if 0 < _flag.get(str(x.get("symbol") or "")
                                        .strip().replace(".T", ""), {})
                       .get(rk, 0) <= a.watch)
        print(f"\n  {tag}")
        print(f"    監視 {_n_watch}件 → **合格 {len(_r)}件**")
        if _r:
            for _sy, _g, _o in _r:
                print(f"      {_sy:<8}{_g:>+8.0f}bp  始値 {_o:>9,.1f}")
        else:
            print(f"      (なし)")
        if _ng:
            print(f"      ⚠ ガード({guard:+.0f}bp 超)で見送り {len(_ng)}件: "
                  + ", ".join(f"{a_}({b_:+.0f})" for a_, b_ in _ng[:6]))

    print(f"\n{'=' * 78}")
    print(f"■ 09:00 の合格銘柄 — {_TODAY}")
    print(f"{'=' * 78}")
    _pass_list(f"★ N     ギャップ ≥ +{a.gap_bp:.0f}bp → **売り**",
               "in_n", "rank_n", a.gap_bp, True)
    _pass_list(f"★ 鏡像   ギャップ ≤ -{a.gap_bp:.0f}bp → **買い**",
               "in_m", "rank_m", a.gap_bp, False)
    _pass_list(f"J       ギャップ ≥ +{a.gap_bp_j:.0f}bp → 売り（参考・記録のみ）",
               "in_j", "rank_j", a.gap_bp_j, True, guard=a.guard_bp_j)
    print(f"\n  ⚠ ここまでは **合格したか**だけ。実際に建てられるかは"
          f"予算次第なので、下の発注シーケンスを見てください")

    _lbl = {1: "売り", -1: "買い"}
    print(f"\n{'=' * 78}")
    print(f"■ 発注シーケンス — {_TODAY} / 予算 {a.budget:,.0f}万 / "
          f"{'両建て(N+鏡像)' if _want == 'nm' else ('N のみ' if _want == 'n' else '鏡像のみ')}")
    print(f"{'=' * 78}")
    print(f"  ★ ライブは全部が寄るのを待ちません(§18.38)。")
    print(f"     **寄ったグループ順 → グループ内は |ギャップ| 降順** に発注します。")
    print(f"  ⛔ 株数は **100株固定**(バックテストと同じ)。資金均等ではありません")
    if not _cand:
        print(f"\n  合格ゼロ（{_pcsv} に条件を満たす銘柄がありません）")
        return

    def _run(order, tag):
        _cash, _seq, _out = a.budget * 1e4, 0, []
        for c in order:
            _cost = c["op"] * QTY
            _ok = _cost <= _cash
            if _ok:
                _cash -= _cost
                _seq += 1
            _out.append({**c, "n": _seq if _ok else 0, "ok": _ok,
                         "cost": _cost, "left": _cash})
        return _out, _cash

    # ── ライブの順(時刻グループ → |gap|降順) ──
    _live = sorted(_cand, key=lambda c: (c["grp"], -abs(c["gap"]), c["sym"]))
    _lv, _lcash = _run(_live, "live")
    print(f"\n  {'#':<4}{'寄り':<10}{'銘柄':<9}{'方式':<7}{'ギャップ':>10}"
          f"{'始値':>10}{'必要資金':>12}{'残り':>13}")
    print("  " + "-" * 76)
    _gp = None
    for r in _lv:
        if r["grp"] != _gp:
            _gp = r["grp"]
            print(f"  ── {r['ts']} に寄ったグループ ──")
        _n = f"{r['n']}" if r["ok"] else "⛔"
        print(f"  {_n:<4}{_hhmmss(r['ot']):<10}{r['sym']:<9}"
              f"{_lbl[r['side']]:<7}{r['gap']:>+10.0f}{r['op']:>10,.1f}"
              f"{r['cost']:>12,.0f}"
              + (f"{r['left']:>13,.0f}" if r["ok"] else f"{'見送り':>13}"))
    print("  " + "-" * 76)
    _nok = sum(1 for r in _lv if r["ok"])
    _ns = sum(1 for r in _lv if r["ok"] and r["side"] > 0)
    _nm = _nok - _ns
    print(f"  **{_nok}件 建てる**(売り {_ns} / 買い {_nm}) / "
          f"投入 {a.budget * 1e4 - _lcash:,.0f}円 "
          f"({(a.budget * 1e4 - _lcash) / (a.budget * 1e4) * 100:.0f}%) / "
          f"見送り {len(_lv) - _nok}件")

    # ── バックテストの順(その日を丸ごと |gap|降順)との差 ──
    _bt = sorted(_cand, key=lambda c: (-abs(c["gap"]), c["sym"]))
    _bv, _bcash = _run(_bt, "bt")
    _lset = {r["sym"] for r in _lv if r["ok"]}
    _bset = {r["sym"] for r in _bv if r["ok"]}
    if _lset != _bset:
        print(f"\n  ⚠ **ライブとバックテストで建てる銘柄が違います**")
        _only_b = sorted(_bset - _lset)
        _only_l = sorted(_lset - _bset)
        if _only_b:
            print(f"     バックテストなら建てたのに、ライブでは見送り: "
                  + ", ".join(_only_b))
            print(f"       → 遅く寄った大ギャップ銘柄が、先に寄った小ギャップに"
                  f"枠を取られたためです")
        if _only_l:
            print(f"     ライブでだけ建てる: " + ", ".join(_only_l))
        print(f"     ⛔ これは実装の誤りではなく、**待たない方針の代償**です"
              f"(待つと1分で -15.8bp 逃げる / §18.44)")
    else:
        print(f"\n  ✅ ライブの順とバックテストの順で、建てる銘柄は同じでした")

    _built = [(r["sym"], r["side"]) for r in _lv if r["ok"]]
    _skip = [(r["sym"], r["side"]) for r in _lv if not r["ok"]]
    _dl = [r for r in _lv if r["late"]]
    if _dl:
        print(f"\n  遅寄り {len(_dl)}件: "
              + ", ".join(f"{r['sym']}({_hhmmss(r['ot'])})" for r in _dl[:8]))
    print(f"\n  ⚠ **ペーパー**。実際には発注していません。")
    print(f"  ⚠ J は資金均等(§18.48)で株数の決め方が違うので、この表には出しません")
    return {"built": _built, "skip": _skip}


if a.sequence:
    do_sequence()
elif a.collect:
    do_collect()
elif a.close:
    do_close()
elif a.warmup:
    do_board(warmup_only=True)
else:
    do_board(warmup_only=False)
