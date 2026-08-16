r"""k_open_confirm.py — K(09:00確認方式)を **記録だけ** する (ペーパー)

⛔⛔ **発注機能を持たせていません**。このファイルは kabu の
   register / board / unregister しか呼びません。売買系の関数は import も
   していないので、引数を間違えても発注は起こりません。

■ 何をするか (2026-08-16 ユーザー決定: 現行Hは止め、明日から J/K のデータ取得に専念)

    08:5x  候補を50件バッチで登録し、**1回空読み**してウォームにする
    09:00  全候補の /board を読み、**始値**を取る
           → 前日終値比のギャップを計算し、+50bp 以上を合格とする
           → 合格件数が確定 → 予算400万 ÷ 件数 で株数を決める
           → k_paper_<日付>.csv に全部書く

  これが K の朝の手順そのもの。**発注だけしない**版です。

■ J と K を同時に記録する

  J(実装版) = 選定あり(lss_proposal_cumul.py) の **流動性上位50件**だけ読む
  K(理想版) = 全候補を読む(バッチ回しが成立すれば)

  読むのは1回で済むので、**同じ朝のデータから両方を切り出す**。
  CSV に in_j / rank_liq を持たせてあるので、後から何通りにも再集計できる。

■ 使い方

    python k_open_confirm.py --prod                  # 08:5x に起動 → 09:00 に読む
    python k_open_confirm.py --prod --now            # いますぐ1回読む(動作確認用)
    python k_open_confirm.py --prod --gap-bp 50 --budget 400
    python k_open_confirm.py --symbols-file lss_trades_K.csv

■ ⚠ 注意

  ・kabu の有効トークンは1つ。watcher / 発注サーバと同時に走らせない。
  ・登録上限50件。候補が多い日はバッチで回す(--batch)。
    ⛔ バッチ回しが**毎回コールド**なら 09:00 には間に合わない。
       成立するかは `.\mtest` の 1(--rotate) で確かめる。
  ・**09:00 に寄らない銘柄がある**(実測15.7%。09:02〜09:06 に集中)。
    OpeningPrice が空 / OpeningPriceTime が 09:00 より後 の銘柄は
    late=1 で記録し、合格判定から外す(現行のバックテストと同じ扱い)。
    段階発注を採るならここを後から拾い直せる。
"""
from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ap = argparse.ArgumentParser(
    description="K(09:00確認方式)を記録だけする。⛔発注しない")
ap.add_argument("--prod", action="store_true", help="本番(18080)。既定はデモ(18081)")
ap.add_argument("--symbols-file", type=str, default="",
                help="候補のソース(既定 holdout_selected_symbols.py)")
ap.add_argument("--symbols", type=str, default="", help="カンマ区切りで明示指定")
ap.add_argument("--pool", type=str, default="lss_proposal_cumul.py",
                help="J(実装版)の母集団。in_j 列の判定に使う")
# ⛔ 既定300で **黙って切っていた**。候補は日によって変わるので、上限に
#    当たったことに気づけないとデータが欠ける(2026-08-17: 299銘柄で紙一重)。
#    0=無制限を既定にし、切るときは必ず警告を出す。
ap.add_argument("--max-symbols", type=int, default=0,
                help="読む銘柄数の上限。**0=無制限**(既定)")
ap.add_argument("--batch", type=int, default=50, help="1バッチ(kabu の登録上限)")
ap.add_argument("--workers", type=int, default=2,
                help="⛔ 上げても速くならず429が増えるだけ(実測)")
ap.add_argument("--gap-bp", type=float, default=50.0, help="合格とするギャップ(bp)")
ap.add_argument("--guard-bp", type=float, default=300.0,
                # ⛔ argparse の help は % 書式として展開される。Python 3.14 は
                #    add_argument の時点で検証するので、生の % があると
                #    ValueError: badly formed help string で **起動すらしない**。
                #    リテラルの % は必ず %% と書くこと(2026-08-16 に実際に落ちた)。
                help="これを超えるギャップは見送り(現行の±3%%ガード)")
ap.add_argument("--budget", type=float, default=400.0, help="予算(万円)")
ap.add_argument("--max-yen", type=float, default=50.0, help="1銘柄の上限(万円)")
ap.add_argument("--max-lot", type=int, default=10, help="1銘柄の最大単元")
ap.add_argument("--watch-j", type=int, default=50, help="J が09:00に読める件数")
ap.add_argument("--open-at", type=str, default="09:00")
ap.add_argument("--warm-at", type=str, default="08:55")
ap.add_argument("--now", action="store_true", help="待たずに いま1回読む")
# ★★ ポーリング (2026-08-16 ユーザー提案)
ap.add_argument("--poll", action="store_true",
                help="09:00 以降も回し続け、**寄った銘柄から順に**拾う")
ap.add_argument("--poll-until", type=str, default="09:30",
                help="--poll の締切(実測: 遅寄りの93%%が09:06までに寄る)")
ap.add_argument("--every", type=int, default=10, help="--poll の間隔(秒)")
ap.add_argument("--now-polls", type=int, default=3,
                help="--now --poll のとき何周だけ回すか(動作確認用)")
ap.add_argument("--collect", action="store_true",
                help="⛔ kabu を使わず、**今日のシグナルだけ**を収集して "
                     "k_signals_<日付>.csv に書き出す(09:00より前に走らせる)")
ap.add_argument("--signals-csv", type=str, default="",
                help="--collect の出力/入力(既定 k_signals_<日付>.csv)")
ap.add_argument("--sm", type=float, default=0.5, help="--collect の損切ATR")
ap.add_argument("--tm", type=float, default=1.0, help="--collect の利確ATR")
ap.add_argument("--days", type=int, default=365, help="--collect の窓")
# ★ レポート(dailyfast.bat の --min-price 1000 --price-ranges 6000)と揃える。
#   提案ファイルはフィルタ前なので、ここで落とさないと J/L/K と母集団が違う。
ap.add_argument("--min-price", type=float, default=1000.0,
                help="--collect の価格下限(注文値で判定)")
ap.add_argument("--max-price", type=float, default=6000.0,
                help="--collect の価格上限(注文値で判定)")
ap.add_argument("--g1", type=float, default=0.8,
                help="第1グループ(09:00の板寄せ)に配る予算の割合。"
                     "以降のグループは残り予算。⛔端数配分はしない")
ap.add_argument("--out", type=str, default="")
args = ap.parse_args()

_COLS = ["date", "seen_ts", "grp", "symbol", "in_j", "rank_liq", "liquidity",
         "prev_close", "open_p", "open_time", "current_price", "gap_bp",
         "late", "pass_gap", "guard_ng", "lots_k", "yen_k"]


# ── 候補の読み込み ─────────────────────────────────────────────────────
def _codes_from(path: str) -> list[str]:
    _t = Path(path).read_text(encoding="utf-8")
    _c = re.findall(r"""['"](\d{4}[A-Z0-9]?)\.T['"]""", _t)
    if not _c and str(path).lower().endswith(".csv"):
        for r in _csv.DictReader(open(path, encoding="utf-8-sig")):
            _s = str(r.get("symbol") or "").upper().removesuffix(".T")
            if _s:
                _c.append(_s.split(".")[0])
    _seen: set = set()
    return [x for x in _c if not (x in _seen or _seen.add(x))]


_sig_csv = args.signals_csv or f"k_signals_{_dt.date.today():%Y%m%d}.csv"

# ══════════════════════════════════════════════════════════════════════
#  --collect : 今日のシグナルだけを収集する (kabu を使わない)
# ══════════════════════════════════════════════════════════════════════
# ⛔⛔ 候補は **WATCHLIST 全部ではなく「今日シグナルが出た銘柄」**。
#    2026-08-16 に holdout_selected_symbols.py(3,054ペア)を候補にしていて
#    誤りだった。実発注(lss_budget_cap.py)は _lss_signal_today で当日の
#    シグナルを1件ずつ拾うので、**同じ関数を使って揃える**。
#    ⚠ 収集は yfinance のバックテストなので数分かかる。**09:00より前に**
#      走らせること(kabu は一切触らないので他の測定と競合しない)。
if args.collect:
    try:
        from kabu_send_lss import _load_symbols, _lss_signal_today
    except Exception as _e:
        sys.exit(f"[error] kabu_send_lss を読めません: {_e}")
    # ⛔ kabu_send_lss._load_symbols は `lss_watchlist_proposal_*.py`(**旧命名**)
    #    を自動検出する。放っておくと数ヶ月前の古い提案を拾い、レポートの
    #    J/L/K とは **別の母集団** で記録することになる(2026-08-17 に実際に
    #    lss_watchlist_proposal_2026-07-15.py 5,639ペアを拾った)。
    #    レポートの土台(dailyfast.bat が渡す lss_proposal_full.py)に揃える。
    _src = args.symbols_file
    if not _src:
        for _c in ("lss_proposal_full.py", "lss_proposal_cumul.py"):
            if Path(_c).exists():
                _src = _c
                break
    _pairs = _load_symbols(_src or None)
    # ⛔ 提案ファイルは **フィルタ前**(9,240ペア)。レポートは読み込み時に
    #    ①空売り不可 ②価格帯(1,000〜6,000円) を落として 8,106ペアにしている。
    #    ここで揃えないと、レポートでは建てない銘柄まで 09:00 に読むことになり、
    #    ・kabu の読込時間を無駄に使う(登録上限50件の測定が不正確になる)
    #    ・J/L/K タブと母集団が食い違う
    #    (2026-08-17: 9,240ペア→703シグナル/416銘柄 と出て発覚)
    _ns: set = set()
    try:
        _nsp = Path(__file__).resolve().parent / "not_shortable.py"
        if _nsp.exists():
            _nsns: dict = {}
            exec(_nsp.read_text(encoding="utf-8"), _nsns)
            _ns = {str(x).upper().removesuffix(".T").split(".")[0]
                   for x in _nsns.get("NOT_SHORTABLE", [])}
    except Exception as _e:
        print(f"  ⚠ not_shortable.py 読み込み失敗: {_e} → 除外なしで続行")
    _n0 = len(_pairs)
    if _ns:
        _pairs = [p for p in _pairs
                  if str(p[0]).upper().removesuffix(".T").split(".")[0] not in _ns]
    print(f"[collect] 母集団: {_src or '(自動検出)'} → {len(_pairs):,}ペア"
          + (f" (空売り不可 {_n0 - len(_pairs):,}除外)" if _n0 != len(_pairs) else "")
          + f" / 価格 {args.min_price:,.0f}〜{args.max_price:,.0f}円"
          f"。今日のシグナルを収集します(kabu は使いません)", flush=True)
    if _src != "lss_proposal_full.py":
        print(f"  ⚠ レポートの土台は lss_proposal_full.py です。"
              f"別ファイルだと J/L/K タブと母集団が食い違います", flush=True)
    _out: list = []
    _n_px = 0          # 価格帯で落とした件数
    for _i, (_c, _n, _st) in enumerate(_pairs):
        if _i and _i % 500 == 0:
            print(f"  … {_i:,}/{len(_pairs):,} ({len(_out)}件)", flush=True)
        try:
            _sg = _lss_signal_today(_c, _n, _st, args.sm, args.tm, args.days)
        except Exception:
            _sg = None
        if not _sg:
            continue
        # ★ 価格帯フィルタ。レポート側と同じく **注文値**で判定する
        #   (前日終値ではない。100株買えるかは注文値で決まる)。
        _px = float(_sg.get("order_price") or 0)
        if _px > 0 and not (args.min_price <= _px <= args.max_price):
            _n_px += 1
            continue
        _cd = str(_sg.get("symbol") or _c).upper() \
            .removesuffix(".T").split(".")[0]
        _out.append({"symbol": _cd, "name": _n, "strategy": _st,
                     "order_price": _sg.get("order_price", 0),
                     "prev_close": _sg.get("prev_close",
                                           _sg.get("close_prev", 0)),
                     "atr": _sg.get("atr", 0)})
    # 重複銘柄は残す(複数戦略で出る)。登録は銘柄単位で重複排除する。
    with open(_sig_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.DictWriter(f, fieldnames=["symbol", "name", "strategy",
                                           "order_price", "prev_close", "atr"])
        w.writeheader()
        w.writerows(_out)
    _uq = len({r["symbol"] for r in _out})
    print(f"[collect] シグナル {len(_out):,}件 / **{_uq:,}銘柄** → {_sig_csv}"
          + (f" (価格帯外 {_n_px:,}件を除外)" if _n_px else "")
          + f"\n  ⛔ 発注していません。09:00 の判定はこの銘柄だけを読みます。",
          flush=True)
    if _uq > args.batch:
        # ⚠ これは **J(実装版)** の話。K の記録は _read_all() が
        #   50件バッチで全部読むので、この件数がそのまま対象になる。
        #   むしろ「何件を何秒で読めるか」が明日の測定の本体。
        print(f"  ★ {_uq}銘柄 = {-(-_uq // args.batch)}バッチ。"
              f"K はこれを全部読みます(バッチ回しの実測が本番)。"
              f"J は流動性上位{args.watch_j}件だけを見ます", flush=True)
    sys.exit(0)

# ── 候補の決定 ────────────────────────────────────────────────────────
if args.symbols:
    _syms = [s.strip().upper().removesuffix(".T").split(".")[0]
             for s in args.symbols.split(",") if s.strip()]
elif Path(_sig_csv).exists():
    # ★ --collect が作った「今日のシグナル」を使う(これが正しい候補)
    _syms = []
    for r in _csv.DictReader(open(_sig_csv, encoding="utf-8-sig")):
        _s0 = str(r.get("symbol") or "").upper().removesuffix(".T").split(".")[0]
        if _s0 and _s0 not in _syms:
            _syms.append(_s0)
    print(f"[候補] {_sig_csv} から **今日のシグナル {len(_syms):,}銘柄**",
          flush=True)
else:
    _p = args.symbols_file or "holdout_selected_symbols.py"
    if not Path(_p).exists():
        sys.exit(f"[error] {_sig_csv} も {_p} もありません。\n"
                 f"  先に `python k_open_confirm.py --collect` を"
                 f"09:00より前に走らせてください")
    print(f"⛔ {_sig_csv} がありません。{_p}(WATCHLIST)で代用しますが、"
          f"**これは今日のシグナルではありません**。\n"
          f"   正しくは先に --collect を走らせること", flush=True)
    _syms = _codes_from(_p)
    if not _syms:
        sys.exit(f"[error] {_p} から銘柄を拾えません(0件)")
if args.max_symbols > 0 and len(_syms) > args.max_symbols:
    print(f"  ⚠ 候補 {len(_syms):,}銘柄 を --max-symbols {args.max_symbols} で"
          f"切ります（{len(_syms) - args.max_symbols:,}銘柄は読みません）",
          flush=True)
    _syms = _syms[:args.max_symbols]

# J の母集団(in_j 列用)
_jpool: set = set()
if Path(args.pool).exists():
    _jpool = set(_codes_from(args.pool))

# 流動性(発注順)。無ければ候補ファイルの出現順を使う(=既に流動性降順のはず)
_liq: dict = {}
for _f in ("lss_trades_K.csv", "lss_trades_H.csv", "lss_trades.csv"):
    if not Path(_f).exists():
        continue
    try:
        for r in _csv.DictReader(open(_f, encoding="utf-8-sig")):
            _s = str(r.get("symbol") or "").upper().removesuffix(".T").split(".")[0]
            _v = float(r.get("liquidity") or 0)
            if _s and _v > _liq.get(_s, 0):
                _liq[_s] = _v
    except Exception:
        pass
    if _liq:
        break

try:
    from kabu_api import KabuClient
except Exception as _e:
    sys.exit(f"[error] kabu_api を読めません: {_e}")

_out_path = args.out or f"k_paper_{_dt.date.today():%Y%m%d}.csv"
print(f"""
{'=' * 74}
■ K(09:00確認方式) の記録 — {_dt.date.today()}
{'=' * 74}
  ⛔ **発注しません**(register / board / unregister のみ)
  候補 {len(_syms):,}銘柄 / {args.batch}件バッチ × {-(-len(_syms) // args.batch)}回
  合格 = 始値が前日終値 {args.gap_bp:+.0f}bp 以上（{args.guard_bp:+.0f}bp 超は見送り）
  予算 {args.budget:.0f}万 / 1銘柄上限 {args.max_yen:.0f}万 / 最大{args.max_lot}単元
  J = 選定あり({args.pool} {len(_jpool):,}銘柄)の流動性上位{args.watch_j}件
  K = 全候補
  → {_out_path}
""", flush=True)

cli = KabuClient(prod=args.prod, dry_run=True)
cli.connect()


def _read_all(tag: str) -> dict:
    """全候補を50件バッチで読む。symbol -> board。"""
    _t0 = time.time()
    _out: dict = {}
    for _i in range(0, len(_syms), args.batch):
        _b = _syms[_i:_i + args.batch]
        try:
            cli.unregister_all()
        except Exception:
            pass
        _res = cli.register_many(_b)
        _ok = len((_res or {}).get("RegistList") or [])
        if _ok < len(_b):
            print(f"  ⚠ 登録 {_ok}/{len(_b)}件 (kabu の上限は50件)", flush=True)

        def _one(s):
            try:
                return s, cli.get_board(s)
            except Exception:
                return s, {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for _s, _bd in ex.map(_one, _b):
                if _bd:
                    _out[_s] = _bd
    print(f"  [{tag}] {len(_out):,}/{len(_syms):,}銘柄 を "
          f"{time.time() - _t0:.1f}秒 で取得", flush=True)
    return _out


def _wait(hm: str, why: str) -> None:
    _h, _m = (int(x) for x in str(hm).split(":"))
    _t = _dt.datetime.now().replace(hour=_h, minute=_m, second=0, microsecond=0)
    _s = (_t - _dt.datetime.now()).total_seconds()
    if _s <= 0:
        return
    print(f"\n[待機] {_t:%H:%M:%S} まで {_s:.0f}秒 — {why}", flush=True)
    while (_r := (_t - _dt.datetime.now()).total_seconds()) > 0:
        time.sleep(min(10.0, _r))


def _mk_row(_s: str, _bd: dict, _ts: str, _grp: int) -> dict:
    """板1件 → 記録用の1行。判定(合格/遅寄り/ガード)もここで済ませる。"""
    _pc = float(_bd.get("PreviousClose") or 0)
    _op = float(_bd.get("OpeningPrice") or 0)
    _ot = str(_bd.get("OpeningPriceTime") or "")
    # ★ 09:00 に寄ったか。OpeningPrice が無い or 時刻が 09:00 より後なら遅寄り。
    #   ⚠ --poll では遅寄りも **建てる**(グループを分けて配分する)ので、
    #      late は記録用のフラグでしかない。
    _late = 0
    if _op <= 0:
        _late = 1
    else:
        _m = re.search(r"T?(\d{2}):(\d{2})", _ot)
        if _m and (int(_m.group(1)) * 60 + int(_m.group(2))) > 9 * 60:
            _late = 1
    _gap = ((_op - _pc) / _pc * 1e4) if (_op > 0 and _pc > 0) else None
    _guard = 1 if (_gap is not None and _gap > args.guard_bp) else 0
    # ⛔ --poll では late を不合格にしない(遅寄りを拾うのが目的)。
    _lt_ng = 0 if args.poll else _late
    _pass = 1 if (_gap is not None and _gap >= args.gap_bp
                  and not _guard and not _lt_ng) else 0
    return {"date": f"{_dt.date.today()}", "seen_ts": _ts, "grp": _grp,
            "symbol": _s, "in_j": 1 if _s in _jpool else 0,
            "rank_liq": 0, "liquidity": _liq.get(_s, 0),
            "prev_close": _pc, "open_p": _op, "open_time": _ot,
            "current_price": _bd.get("CurrentPrice") or 0,
            "gap_bp": (round(_gap, 1) if _gap is not None else ""),
            "late": _late, "pass_gap": _pass, "guard_ng": _guard,
            "lots_k": 0, "yen_k": 0}


def _dump(_rows: list) -> None:
    """途中で落ちてもデータを失わないよう、毎周 書き出す。"""
    try:
        with open(_out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=_COLS)
            w.writeheader()
            w.writerows(_rows)
    except Exception as e:
        print(f"  ⚠ CSV を書けません: {e}", flush=True)


def _size_groups(_rows: list) -> None:
    """グループごとに **固定上限** で配る (2026-08-16 ユーザー判断)。

    第1グループ(09:00の板寄せ) … 予算 × --g1
    以降の各グループ           … 残り予算
    ⛔ 端数配分はしない。締切までに寄らなかった候補ぶんの予算は使わない
       (§18.38 の充填と同じで、配り切るとリスク調整後は悪化する)。
    """
    # 流動性降順の順位(発注順 / §18.21)。0 は最後尾。
    _rows.sort(key=lambda r: (-float(r["liquidity"] or 0), str(r["symbol"])))
    for _i, r in enumerate(_rows):
        r["rank_liq"] = _i + 1
    _bud, _cap = args.budget * 1e4, args.max_yen * 1e4
    _R = _bud
    for _g in sorted({int(r["grp"]) for r in _rows}):
        _sel = [r for r in _rows if int(r["grp"]) == _g and r["pass_gap"]]
        if not _sel:
            continue
        _alloc = min(_bud * args.g1, _R) if _g == 0 else _R
        _per = min(_alloc / len(_sel), _cap) if _cap > 0 else _alloc / len(_sel)
        _used = 0.0
        for r in _sel:
            _u = float(r["open_p"]) * 100
            if _u <= 0:
                continue
            _lot = min(args.max_lot, int(_per // _u))
            if _cap > 0:
                _lot = min(_lot, int(_cap // _u))
            _lot = max(1, _lot)          # 1件目は最低1単元(現行と同じ)
            # 残り予算を超えないところで打ち切る
            while _lot > 0 and _used + _lot * _u > _R:
                _lot -= 1
            if _lot <= 0:
                continue
            r["lots_k"] = _lot
            r["yen_k"] = round(_lot * _u, 0)
            _used += _lot * _u
        _R = max(0.0, _R - _used)


# ── ① ウォームアップ (登録直後の初回は48〜142秒かかる / §18.38) ───────────
if not args.now:
    _wait(args.warm_at, "登録して1回空読み(これを飛ばすと09:00が数分かかる)")
print("\n▶ ウォームアップ（空読み。値は使いません）", flush=True)
_read_all("warm")

# ══════════════════════════════════════════════════════════════════════
#  ポーリング (--poll) — 09:00 以降に寄る銘柄も拾う
# ══════════════════════════════════════════════════════════════════════
# ★★ 2026-08-16 ユーザー提案「9:06に寄り付いたらそこからすぐ注文を出せばいい」。
#   寄った銘柄から順に処理すれば、全部が寄るまで待つ必要が無い。
#   配分は **固定上限**(第1グループに 予算×G1、以降は残り予算)。
#   動的配分(残り予算 × 候補数 ÷ 未判定数)は毎回 未寄件数を数える必要があり
#   ライブで壊れやすい、というユーザー判断による。
#   ⛔ 端数配分はしない(§18.38 の充填と同じでリスク調整後は悪化)。
_rows: list = []
_seen: dict = {}          # symbol -> 最初に寄りを検知した時刻
_groups: list = []        # [(検知時刻, [銘柄...]), ...]
_read_ts = ""

if args.poll:
    _h9, _m9 = (int(x) for x in str(args.open_at).split(":"))
    _t_open = _dt.datetime.now().replace(hour=_h9, minute=_m9,
                                         second=0, microsecond=0)
    _he, _me = (int(x) for x in str(args.poll_until).split(":"))
    _t_end = _dt.datetime.now().replace(hour=_he, minute=_me,
                                        second=0, microsecond=0)
    if not args.now:
        _wait(args.open_at, "★ここから本番。寄った銘柄から順に拾う")
    print(f"\n▶ ポーリング開始（{args.every}秒ごと / {args.poll_until} まで）",
          flush=True)
    _n_poll = 0
    while True:
        _t0 = time.time()
        _n_poll += 1
        _bd_all = _read_all(f"poll{_n_poll}")
        _now_s = f"{_dt.datetime.now():%H:%M:%S}"
        _new = []
        for _s, _bd in _bd_all.items():
            if _s in _seen:
                continue
            _op = float(_bd.get("OpeningPrice") or 0)
            if _op <= 0:
                continue          # まだ寄っていない
            _seen[_s] = _now_s
            _new.append((_s, _bd))
        if _new:
            _groups.append((_now_s, [x[0] for x in _new]))
            for _s, _bd in _new:
                _rows.append(_mk_row(_s, _bd, _now_s,
                                     len(_groups) - 1))
            print(f"  [{_now_s}] **新たに寄った {len(_new)}件** "
                  f"(通算 {len(_seen)}/{len(_syms)}) "
                  f"/ 読込 {time.time() - _t0:.1f}秒", flush=True)
        else:
            print(f"  [{_now_s}] 新規なし (通算 {len(_seen)}/{len(_syms)}) "
                  f"/ 読込 {time.time() - _t0:.1f}秒", flush=True)
        # ★ 途中で落ちてもデータを失わないよう毎回書き出す
        _dump(_rows)
        if args.now and _n_poll >= max(1, args.now_polls):
            break
        if _dt.datetime.now() >= _t_end or len(_seen) >= len(_syms):
            break
        _sl = max(0.0, args.every - (time.time() - _t0))
        if _sl > 0:
            time.sleep(_sl)
    _read_ts = f"{_dt.datetime.now():%H:%M:%S}"
else:
    # ── 従来: 09:00 に1回だけ読む ──────────────────────────────────
    if not args.now:
        _wait(args.open_at, "★ここが本番。板寄せ直後の始値を取る")
    print("\n▶ 本番の読み取り（09:00 の1回だけ）", flush=True)
    _read_ts = f"{_dt.datetime.now():%H:%M:%S}"
    _bd_all = _read_all("open")
    _groups.append((_read_ts, list(_bd_all)))
    for _s in _syms:
        _bd = _bd_all.get(_s) or {}
        if float(_bd.get("OpeningPrice") or 0) > 0:
            _seen[_s] = _read_ts
        _rows.append(_mk_row(_s, _bd, _read_ts, 0))

# ── 配分 (固定上限。第1グループ=予算×G1 / 以降=残り予算) ─────────────
_size_groups(_rows)
_dump(_rows)

_got = sum(1 for r in _rows if float(r["open_p"] or 0) > 0)
_late_n = sum(1 for r in _rows if r["late"])
_pass_k = [r for r in _rows if r["pass_gap"]]
_j_seen = [r for r in _rows if r["in_j"]][:args.watch_j]
_pass_j = [r for r in _j_seen if r["pass_gap"]]
print(f"""
{'=' * 74}
■ 結果 — {_out_path}
{'=' * 74}
  読めた       {_got:,}/{len(_rows):,}銘柄   (最終 {_read_ts})
  09:00に未寄  {_late_n:,}銘柄 ({_late_n / max(1, len(_rows)) * 100:.1f}%)
               ⚠ バックテストの実測は15.7%。大きく違うなら要調査
  グループ     {len(_groups)}回""")
for _gi, (_gt, _gs) in enumerate(_groups):
    print(f"    {_gi + 1}. {_gt}  {len(_gs)}銘柄")
print(f"""
  合格 {len(_pass_k):,}件 → 建てる {sum(1 for r in _rows if r['lots_k'])}件 / """
      f"""投入 {sum(float(r['yen_k'] or 0) for r in _rows) / 1e4:,.0f}万
  うちJ(選定あり上位{args.watch_j}) 合格 {len(_pass_j):,}件

  ⛔ **発注していません**。この CSV は「その朝 K なら何を建てたか」の記録です。
  ★ 貯めたら 5分足の始値と突合して、**板の始値 = 5分足の始値** かを確認する
    (バックテストの前提そのもの)。ズレるなら K の全数字が影響を受けます。
""")
try:
    cli.unregister_all()
    print("[k_paper] 登録を全解除しました")
except Exception:
    pass
