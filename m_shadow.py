"""m_shadow.py — 鏡像(ギャップダウンを買う)を **記録だけ**する (⛔ 発注しない)

════════════════════════════════════════════════════════════════════════
★ これは何か (2026-09-12)
════════════════════════════════════════════════════════════════════════
N とは別プロセスで、鏡像の 09:00 判定を前向きに貯める。

  ⛔⛔ **k_open_confirm.py を1行も触らない**。N の執行は N2 で凍結中で
     (§18.66 のゲートを 20件かつ約定のある8営業日ぶん数えている最中)、
     そこに手を入れると版が変わって数え直しになる。

  ⛔⛔ **1円も発注しない**。KabuClient は発注メソッドも持つが呼ばない。
     dry_run=True で二重に封じたうえ、起動時に **自分のソースを AST で
     検査**して send_* / 発注系の呼び出しが1つも無いことを確かめる。

════════════════════════════════════════════════════════════════════════
★ なぜ 09:10 以降に走らせるのか
════════════════════════════════════════════════════════════════════════
kabu の有効トークンは1つ。`.\\nexec`(N の実発注)が走っている間に叩くと
**401 でトークンを取り合う**(CLAUDE.md ★ 毎日使うコマンド)。N が終わる
09:10 以降に回す。

板の **始値は寄れば動かない**ので、遅れて読んでも判定は正しい(§18.44)。
鏡像は記録専用なので速度は要らない。ここが N と決定的に違う点。

  ⚠ ただし『何時に寄ったか』は OpeningPriceTime に残るので、**後から
    「寄った順に予算を配ったらどうなるか」を先読みなしで再現できる**。
    ライブと同じ順序で測るために、この列は必ず残すこと。

════════════════════════════════════════════════════════════════════════
★ 使い方 (1日2コマンド)
════════════════════════════════════════════════════════════════════════
    09:10 以降   python m_shadow.py --prod
    15:40 以降   python m_shadow.py --close

  --close は自分では採点せず **n_paper.py --close を呼ぶ**。採点ロジックを
  写すと必ず片方だけ直って食い違う(§18.48 ⑧d)。

出力:
    m_signals_<日付>.csv  前夜の鏡像候補 (n_paper.py --collect --mirror が作る)
    m_paper_<日付>.csv    09:00 の板 (k_paper 互換。n_paper --close が読める)

  ⛔ **n_signals_<日付>.csv / k_paper_<日付>.csv を上書きしない**。
     N の本番の候補リストと板記録はそのまま。別名に逃がしてある。
"""
from __future__ import annotations

import argparse
import ast
import csv as _csv
import subprocess
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import console_safe  # noqa: F401  (Windows コンソールの文字化け対策)

JST = timezone(timedelta(hours=9))

# ── 鏡像の条件 (§18.55 / N の符号を反転しただけ) ──────────────────────
RET1_MIN = 1.753          # 前日リターン ≤ -1.753%
GAP_BP = 100.0            # 始値が前日終値 -100bp 以下なら合格
MIN_PRICE, MAX_PRICE = 1000.0, 6000.0
QTY = 100


# ══════════════════════════════════════════════════════════════════════
# ⛔ 発注しないことを **コードで** 確かめる
# ══════════════════════════════════════════════════════════════════════
_BANNED = ("send_", "cancel_order", "place_", "_execute_order")


def _assert_no_order_calls() -> None:
    """自分のソースに発注系の呼び出しが1つも無いことを AST で確かめる。

    ⛔ コメントで「発注しない」と書くだけでは、後から1行足したときに
       気づけない。**呼び出しノードを実際に見る**。
       起動のたびに走る(数ミリ秒)。落ちたら何もせず終了する。
    """
    try:
        _src = Path(__file__).read_text(encoding="utf-8")
    except Exception as _e:                                    # noqa: BLE001
        sys.exit(f"[error] 自己検査のためのソース読み込みに失敗: {_e}")
    _hit = []
    for _n in ast.walk(ast.parse(_src)):
        _nm = ""
        if isinstance(_n, ast.Attribute):
            _nm = _n.attr
        elif isinstance(_n, ast.Name):
            _nm = _n.id
        if _nm and any(_nm.startswith(_b) for _b in _BANNED):
            _hit.append(f"{_nm} (行 {getattr(_n, 'lineno', '?')})")
    if _hit:
        sys.exit("[error] ⛔ このスクリプトに発注系の参照があります。"
                 "記録専用のはずです:\n       " + "\n       ".join(_hit))


_assert_no_order_calls()


ap = argparse.ArgumentParser(
    description="鏡像(ギャップダウンを買う)の 09:00 判定を記録する (⛔ 発注しない)")
ap.add_argument("--prod", action="store_true",
                help="本番(18080)に接続する。⛔ 照会のみ。発注はしない")
ap.add_argument("--close", action="store_true",
                help="引け後: n_paper.py --close を鏡像の CSV で呼ぶ (kabu 不要)")
ap.add_argument("--date", type=str, default="", help="対象日 yyyy-MM-dd (既定 今日)")
ap.add_argument("--after", type=str, default="09:10",
                help="この時刻より前は走らない(N の発注とトークンを取り合うため)。"
                     "⛔ 外すなら --now")
ap.add_argument("--now", action="store_true",
                help="時間帯ガードを外して いま1回読む(動作確認用)")
ap.add_argument("--read-top", type=int, default=150,
                help="流動性順に何位まで読むか(0=全部)。50件バッチで回す。"
                     "⛔ 発注できるのは上位50件だが、**壁の外側も記録**して"
                     "おかないと『広げたらどうなるか』を後から測れない")
ap.add_argument("--watch", type=int, default=50,
                help="実際に建てられる件数(kabu の登録上限 / §18.44)。"
                     "記録には影響しない。表示と採点の区切りに使う")
ap.add_argument("--batch", type=int, default=50, help="1バッチ(kabu の登録上限)")
ap.add_argument("--workers", type=int, default=2,
                help="板読みの並列数。⛔ §18.44 実測で **2 が最適**。"
                     "上げても速くならず 429 が増えるだけ")
ap.add_argument("--ret1", type=float, default=RET1_MIN,
                help="前日リターンの下限(%%)。鏡像は符号を反転して使う")
ap.add_argument("--gap-bp", type=float, default=GAP_BP,
                help="ギャップの下限(bp)。鏡像は -これ以下 で合格")
ap.add_argument("--budget", type=float, default=400.0, help="--close の予算(万円)")
ap.add_argument("--no-collect", action="store_true",
                help="候補CSVが無くても作り直さない(既に作ってあるとき)")
a = ap.parse_args()

_TODAY = a.date or datetime.now(JST).strftime("%Y-%m-%d")
_YMD = _TODAY.replace("-", "")
# ⛔ **本番の n_signals_ / k_paper_ とは別名**。N の記録を1バイトも触らない。
_SIG_CSV = Path(f"m_signals_{_YMD}.csv")
_PAPER_CSV = Path(f"m_paper_{_YMD}.csv")


def _code4(sym: str) -> str:
    return str(sym).replace(".T", "").strip()


# ══════════════════════════════════════════════════════════════════════
# ① 候補CSV — **自分では作らない**。n_paper.py --collect --mirror を呼ぶ
# ══════════════════════════════════════════════════════════════════════
def _ensure_signals() -> None:
    """鏡像の候補リストを用意する。

    ⛔ 候補の定義(前日リターン・価格帯・流動性順)を **ここに写さない**。
       写した瞬間に2箇所になり、片方だけ直って食い違う(§18.48 ⑧d で
       実際に起きた)。n_paper.py を呼んで作らせる。

    ⚠ --watch 0 で作る。こうすると鏡像の候補が **全件** ランクつきで残るので、
       50件の壁の外側も後から測れる。実際に建てられるのは上位 --watch 件
       だけで、その区切りは採点時に rank_m で掛ける(n_paper --close と同じ)。
    """
    if _SIG_CSV.exists():
        print(f"[collect] {_SIG_CSV} は既にあります (作り直しません)", flush=True)
        return
    if a.no_collect:
        sys.exit(f"[error] {_SIG_CSV} がありません。--no-collect を外すか、\n"
                 f"        python n_paper.py --collect --mirror --watch 0 "
                 f"--signals-csv {_SIG_CSV}\n        を先に実行してください")
    _cmd = [sys.executable, "n_paper.py", "--collect", "--mirror",
            "--watch", "0", "--signals-csv", str(_SIG_CSV)]
    if a.date:
        _cmd += ["--date", a.date]
    print(f"[collect] 鏡像の候補を作ります (⛔ n_signals_{_YMD}.csv は触りません)\n"
          f"          {' '.join(_cmd)}", flush=True)
    _rc = subprocess.call(_cmd)
    if _rc != 0 or not _SIG_CSV.exists():
        sys.exit(f"[error] 候補の作成に失敗しました (rc={_rc})")


def _load_mirror() -> list[dict]:
    """m_signals_<日付>.csv から **鏡像の候補だけ** を流動性順に読む。"""
    out = []
    for r in _csv.DictReader(open(_SIG_CSV, encoding="utf-8-sig")):
        try:
            _rk = int(float(r.get("rank_m") or 0))
        except Exception:
            continue
        if _rk <= 0:                       # 鏡像の候補でない(N 側の行)
            continue
        try:
            r["rank_m"] = _rk
            r["prev_close"] = float(r["prev_close"])
            r["ret1"] = float(r["ret1"])
            r["liq"] = float(r.get("liq") or 0)
        except Exception:
            continue
        out.append(r)
    out.sort(key=lambda r: r["rank_m"])
    return out


# ══════════════════════════════════════════════════════════════════════
# ② 板を読む — 50件バッチでローテーション (⛔ 照会のみ)
# ══════════════════════════════════════════════════════════════════════
def _connect():
    """⛔ 照会専用。KabuClient は発注メソッドも持つが、**呼ばない**。"""
    try:
        from kabu_api import KabuClient
    except Exception as e:                                     # noqa: BLE001
        sys.exit(f"[error] kabu_api を import できません: {e}")
    cli = KabuClient(prod=bool(a.prod), dry_run=True)   # dry_run で二重に封じる
    cli.connect()
    return cli


def _read_boards(cli, cand: list[dict]) -> dict:
    """symbol(4桁) -> 板。登録上限50件をローテーションで越える。

    ⛔ **1バッチごとに解除する**。kabu の登録は *総数* 50件なので、
       解除しないと2バッチ目の register が 400 で丸ごと落ちる(§18.69)。
    """
    _codes = [_code4(r["symbol"]) for r in cand]
    _nb = -(-len(_codes) // a.batch)
    _out: dict = {}
    _fail = 0
    for _i in range(_nb):
        _b = _codes[_i * a.batch:(_i + 1) * a.batch]
        _t0 = _time.time()
        _reg = cli.register_many(_b)
        _nok = len((_reg or {}).get("RegistList", []) or [])
        if _nok < len(_b):
            # ⛔ /register は **部分受理しない**。1件でも無効な銘柄コードが
            #   混ざると PUT 全体が 400 で落ちる(§18.69 で上場廃止の 5486 が
            #   犯人だった)。数が合わないのは異常なので黙って進まない。
            print(f"  ⛔ バッチ{_i + 1}: 登録が {_nok}/{len(_b)}件 しか"
                  f"受理されていません(上場廃止銘柄が混ざっている可能性)",
                  flush=True)

        def _one(_c):
            try:
                return _c, cli.get_board(_c)
            except Exception as _e:                             # noqa: BLE001
                return _c, {"_err": type(_e).__name__}

        _got = 0
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            for _c, _bd in ex.map(_one, _b):
                if not _bd or _bd.get("_err"):
                    _fail += 1
                    continue
                _out[_c] = _bd
                _got += 1
        print(f"  バッチ{_i + 1}/{_nb}: {_got}/{len(_b)}件 "
              f"({_time.time() - _t0:.1f}s)", flush=True)
        if _i + 1 < _nb:
            # ⛔⛔ **解除できなければ次のバッチに進まない**。
            #   unregister_many は失敗したら _registered を消さない
            #   (fail-safe / kabu_api.py:309 の 2026-09-07 修正)。ここで
            #   勝手に discard すると、サーバに残っているのに「消えた」と
            #   思い込んで総数が50を超え、次の register が 400 で丸ごと
            #   落ちる。**まさにその fail-open を直したばかりの箇所**。
            if not cli.unregister_many(_b):
                print(f"  ⛔ バッチ{_i + 1} の解除に失敗しました。"
                      f"登録上限を越えるので、ここで読み取りを打ち切ります\n"
                      f"     (読めた {len(_out)}件 / 残り "
                      f"{len(_codes) - (_i + 1) * a.batch}件は記録されません)",
                      flush=True)
                break
            _time.sleep(0.5)            # /register のレート制限をまたぐ
    if _fail:
        print(f"  ⚠ 板が取れなかった銘柄 {_fail}件", flush=True)
    return _out


def _open_today(_bd: dict) -> tuple[float, int]:
    """板 → (当日の始値, 前日ぶんを掴んだか)。当日でなければ (0.0, 1)。

    ⛔⛔ /board は引け後も当日の OpeningPrice を返し続けるので、まだ寄って
      いない銘柄は **前日の** 始値を返しうる(§18.48⑦ / 2026-08-20)。
      日付を見ないと『前日の日中変動』でギャップ判定してしまう。
      判定は k_open_confirm._open_today と同じ形にしてある。
    """
    try:
        _op = float(_bd.get("OpeningPrice") or 0)
    except Exception:
        return 0.0, 0
    if _op <= 0:
        return 0.0, 0
    _ot = str(_bd.get("OpeningPriceTime") or "")
    if len(_ot) < 10 or _ot[:10] != _TODAY:
        return 0.0, 1
    return _op, 0


def _mk_row(r: dict, _bd: dict, _ts: str) -> dict:
    """板1件 → k_paper 互換の1行。判定もここで済ませる。

    ⛔ 列名は k_paper_<日付>.csv に合わせる。n_paper.py --close が
       `open_p` / `gap_bp` / `prev_close` / `symbol` を読むので、
       名前を変えると採点できない。
    """
    _pc = float(_bd.get("PreviousClose") or 0) or float(r["prev_close"])
    _ot = str(_bd.get("OpeningPriceTime") or "")
    _op, _stale = _open_today(_bd)
    # 09:00 に寄ったか。記録用のフラグで、合格判定には使わない
    #   (鏡像も遅寄りを拾う。寄った順の配分は open_time から後で再現する)
    _late = 1 if _op <= 0 else 0
    if _op > 0 and len(_ot) >= 16:
        try:
            if int(_ot[11:13]) * 60 + int(_ot[14:16]) > 9 * 60:
                _late = 1
        except Exception:
            pass
    _gap = ((_op - _pc) / _pc * 1e4) if (_op > 0 and _pc > 0) else None
    # ⛔ N は 09:00 の始値も価格帯に入っていることを条件にしている。
    #    鏡像も同じ(バックテストが x.open.between で切っているため)。
    _pband = 1 if (_op > 0 and not (MIN_PRICE <= _op <= MAX_PRICE)) else 0
    # ★ 鏡像は **-gap_bp 以下** で合格(符号が N の逆)。
    #   ⛔ 上限ガードは無い(§18.55 で棄却済み)。N と揃える。
    _pass = 1 if (_gap is not None and _gap <= -a.gap_bp and not _pband) else 0
    return {"date": _TODAY, "seen_ts": _ts, "grp": 0,
            "symbol": _code4(r["symbol"]),
            "in_j": 0, "rank_liq": r["rank_m"], "liquidity": r["liq"],
            "prev_close": round(_pc, 1), "open_p": round(_op, 1),
            "open_time": _ot,
            "current_price": _bd.get("CurrentPrice") or 0,
            # 買い注文が当たる先 = 最良売り気配。
            # ⛔⛔ kabu の Bid/Ask は **トレーダー目線で名前が逆**
            #   (公式仕様: BidPrice=Sell1.Price / AskPrice=Buy1.Price)。
            #   つまり買いで叩くのは **BidPrice**。2026-09-11 に N 側で
            #   3日間これを取り違えた。列名は k_paper と揃える。
            "bid": _bd.get("BidPrice") or 0,
            "ask": _bd.get("AskPrice") or 0,
            "bid_qty": _bd.get("BidQty") or 0,
            "ask_qty": _bd.get("AskQty") or 0,
            "gap_bp": (round(_gap, 1) if _gap is not None else ""),
            "late": _late, "pass_gap": _pass, "guard_ng": 0,
            "stale_open": _stale,
            "lots_k": 0, "yen_k": 0, "atr": "", "stop_k": "", "target_k": "",
            # ⛔ 記録専用。1件も発注していない
            "ordered": 0, "order_limit": ""}


_COLS = ["date", "seen_ts", "grp", "symbol", "in_j", "rank_liq", "liquidity",
         "prev_close", "open_p", "open_time", "current_price",
         "bid", "ask", "bid_qty", "ask_qty", "gap_bp",
         "late", "pass_gap", "guard_ng", "stale_open", "lots_k", "yen_k",
         "atr", "stop_k", "target_k", "ordered", "order_limit"]


def do_board() -> None:
    _ensure_signals()
    cand = _load_mirror()
    if not cand:
        print(f"\n[鏡像] {_TODAY}: 前夜の候補がゼロです "
              f"(前日リターン ≤ -{a.ret1}% の銘柄が無い)。記録することはありません",
              flush=True)
        return
    _n_all = len(cand)
    if a.read_top > 0:
        cand = cand[:a.read_top]
    _now = datetime.now(JST)
    if not a.now:
        try:
            _hh, _mm = (int(x) for x in a.after.split(":"))
        except Exception:
            sys.exit(f"[error] --after は HH:MM で指定してください: {a.after}")
        if (_now.hour * 60 + _now.minute) < _hh * 60 + _mm:
            sys.exit(f"[error] まだ {a.after} 前です ({_now:%H:%M:%S})。\n"
                     f"        ⛔ この時刻より前は .\\nexec(N の実発注)と"
                     f"kabu のトークンを取り合います。\n"
                     f"        N が終わってから実行してください "
                     f"(動作確認だけなら --now)")
    print(f"\n{'=' * 74}")
    print(f"■ 鏡像のシャドー記録 — {_TODAY}  "
          f"{'本番(18080)' if a.prod else 'デモ(18081)'}")
    print(f"{'=' * 74}")
    print(f"  ⛔ **1円も発注しません**。板を読んで記録するだけです")
    print(f"  候補 {_n_all:,}件 → 読む {len(cand):,}銘柄 "
          f"({-(-len(cand) // a.batch)}バッチ / "
          f"建てられるのは上位 {a.watch}件)", flush=True)
    cli = _connect()
    _bds = _read_boards(cli, cand)
    _ts = f"{datetime.now(JST):%H:%M:%S}"
    rows = [_mk_row(r, _bds[_c], _ts) for r in cand
            if (_c := _code4(r["symbol"])) in _bds]
    if not rows:
        sys.exit("[error] 板が1件も読めませんでした")
    with open(_PAPER_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.DictWriter(fh, fieldnames=_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    _pass = [r for r in rows if r["pass_gap"] == 1]
    _top = [r for r in _pass if int(r["rank_liq"]) <= a.watch]
    _stale = sum(1 for r in rows if r["stale_open"])
    _noopen = sum(1 for r in rows if float(r["open_p"]) <= 0)
    print(f"\n  読んだ           {len(rows):>5}件")
    print(f"  当日の始値なし   {_noopen:>5}件 "
          f"({_noopen / max(1, len(rows)) * 100:.0f}% / うち前日ぶん {_stale}件)")
    print(f"  **合格 (ギャップ ≤ -{a.gap_bp:.0f}bp)** {len(_pass):>5}件"
          + (f"  ← 上位{a.watch}件に限れば **{len(_top)}件**"
             if len(_pass) != len(_top) else ""))
    if _top:
        _need = sum(float(r["open_p"]) * QTY for r in _top)
        print(f"  100株ずつ建てるなら 約 {_need / 1e4:,.0f}万円")
        print(f"\n  {'#':<5}{'銘柄':<8}{'前日終値':>10}{'始値':>10}"
              f"{'ギャップ':>10}{'寄り時刻':>11}")
        print("  " + "-" * 56)
        for r in sorted(_top, key=lambda x: float(x["gap_bp"])):
            _ot = str(r["open_time"])
            print(f"  {int(r['rank_liq']):<5}{r['symbol']:<8}"
                  f"{float(r['prev_close']):>10,.1f}{float(r['open_p']):>10,.1f}"
                  f"{float(r['gap_bp']):>+10.0f}"
                  f"{(_ot[11:19] if len(_ot) > 18 else '?'):>11}")
    else:
        print(f"  → 今日は合格ゼロです")
    print(f"\n  → {_PAPER_CSV}")
    print(f"  引け後(15:40以降)に  python m_shadow.py --close")


# ══════════════════════════════════════════════════════════════════════
# ③ --close : 採点は **n_paper.py に任せる**
# ══════════════════════════════════════════════════════════════════════
def do_close() -> None:
    """⛔ 採点ロジックをここに写さない。

    n_paper.py --close は既に鏡像を side=-1 で正しく採点し(:846)、
    --seq-sides m で予算配分も鏡像だけに回せる(:964)。写すと必ず
    片方だけ直って食い違う(§18.48 ⑧d の教訓)。
    """
    if not _PAPER_CSV.exists():
        sys.exit(f"[error] {_PAPER_CSV} がありません。"
                 f"先に朝の板読み(python m_shadow.py --prod)を実行してください")
    if not _SIG_CSV.exists():
        sys.exit(f"[error] {_SIG_CSV} がありません(in_m を復元できません)")
    _cmd = [sys.executable, "n_paper.py", "--close",
            "--signals-csv", str(_SIG_CSV),
            "--paper-csv", str(_PAPER_CSV),
            "--seq-sides", "m",
            "--budget", str(a.budget),
            "--watch", str(a.watch),
            "--gap-bp", str(a.gap_bp)]
    if a.date:
        _cmd += ["--date", a.date]
    print(f"[close] 採点は n_paper.py に任せます (ロジックを写さないため)\n"
          f"        {' '.join(_cmd)}\n", flush=True)
    sys.exit(subprocess.call(_cmd))


if __name__ == "__main__":
    if a.close:
        do_close()
    else:
        do_board()
