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
ap.add_argument("--max-symbols", type=int, default=300)
ap.add_argument("--batch", type=int, default=50, help="1バッチ(kabu の登録上限)")
ap.add_argument("--workers", type=int, default=2,
                help="⛔ 上げても速くならず429が増えるだけ(実測)")
ap.add_argument("--gap-bp", type=float, default=50.0, help="合格とするギャップ(bp)")
ap.add_argument("--guard-bp", type=float, default=300.0,
                help="これを超えるギャップは見送り(現行の±3%ガード)")
ap.add_argument("--budget", type=float, default=400.0, help="予算(万円)")
ap.add_argument("--max-yen", type=float, default=50.0, help="1銘柄の上限(万円)")
ap.add_argument("--max-lot", type=int, default=10, help="1銘柄の最大単元")
ap.add_argument("--watch-j", type=int, default=50, help="J が09:00に読める件数")
ap.add_argument("--open-at", type=str, default="09:00")
ap.add_argument("--warm-at", type=str, default="08:55")
ap.add_argument("--now", action="store_true", help="待たずに いま1回読む")
ap.add_argument("--out", type=str, default="")
args = ap.parse_args()

_COLS = ["date", "read_ts", "symbol", "in_j", "rank_liq", "liquidity",
         "prev_close", "open_p", "open_time", "current_price", "gap_bp",
         "late", "pass_gap", "guard_ng",
         "lots_j", "yen_j", "lots_k", "yen_k"]


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


if args.symbols:
    _syms = [s.strip().upper().removesuffix(".T").split(".")[0]
             for s in args.symbols.split(",") if s.strip()]
else:
    _p = args.symbols_file or "holdout_selected_symbols.py"
    if not Path(_p).exists():
        sys.exit(f"[error] {_p} がありません。--symbols で明示指定してください")
    _syms = _codes_from(_p)
    if not _syms:
        sys.exit(f"[error] {_p} から銘柄を拾えません(0件)。\n"
                 f"  ⚠ 研究実行が上書きして **空** になっていることがあります。\n"
                 f"     `.\\daily` を1回流して作り直してください")
_syms = _syms[:max(1, args.max_symbols)]

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


# ── ① ウォームアップ (登録直後の初回は48〜142秒かかる / §18.38) ───────────
if not args.now:
    _wait(args.warm_at, "登録して1回空読み(これを飛ばすと09:00が数分かかる)")
print("\n▶ ウォームアップ（空読み。値は使いません）", flush=True)
_read_all("warm")

# ── ② 09:00 の本番読み ────────────────────────────────────────────────
if not args.now:
    _wait(args.open_at, "★ここが本番。板寄せ直後の始値を取る")
print("\n▶ 本番の読み取り", flush=True)
_read_ts = f"{_dt.datetime.now():%H:%M:%S}"
_bd_all = _read_all("open")

# ── ③ 判定 ───────────────────────────────────────────────────────────
_rows: list[dict] = []
for _s in _syms:
    _bd = _bd_all.get(_s) or {}
    _pc = float(_bd.get("PreviousClose") or 0)
    _op = float(_bd.get("OpeningPrice") or 0)
    _ot = str(_bd.get("OpeningPriceTime") or "")
    # ★ 09:00 に寄ったか。OpeningPrice が無い or 時刻が 09:00台の1分以降なら
    #   「まだ寄っていない」= 現行のバックテストでは建てない扱い(15.7%)。
    _late = 0
    if _op <= 0:
        _late = 1
    else:
        _m = re.search(r"T?(\d{2}):(\d{2})", _ot)
        if _m and (int(_m.group(1)) * 60 + int(_m.group(2))) > 9 * 60:
            _late = 1
    _gap = ((_op - _pc) / _pc * 1e4) if (_op > 0 and _pc > 0) else None
    _guard = 1 if (_gap is not None and _gap > args.guard_bp) else 0
    _pass = 1 if (_gap is not None and _gap >= args.gap_bp
                  and not _guard and not _late) else 0
    _rows.append({
        "date": f"{_dt.date.today()}", "read_ts": _read_ts, "symbol": _s,
        "in_j": 1 if _s in _jpool else 0,
        "rank_liq": 0, "liquidity": _liq.get(_s, 0),
        "prev_close": _pc, "open_p": _op, "open_time": _ot,
        "current_price": _bd.get("CurrentPrice") or 0,
        "gap_bp": (round(_gap, 1) if _gap is not None else ""),
        "late": _late, "pass_gap": _pass, "guard_ng": _guard,
        "lots_j": 0, "yen_j": 0, "lots_k": 0, "yen_k": 0})

# 流動性降順の順位(発注順 / §18.21)。0 は最後尾。
_rows.sort(key=lambda r: (-float(r["liquidity"] or 0), str(r["symbol"])))
for _i, r in enumerate(_rows):
    r["rank_liq"] = _i + 1


def _size(sel: list[dict], key: str) -> tuple:
    """予算 ÷ 合格件数。1銘柄上限・最大単元・最低1単元は現行と同じ。"""
    if not sel:
        return 0, 0.0
    _bud, _cap = args.budget * 1e4, args.max_yen * 1e4
    _per = min(_bud / len(sel), _cap) if _cap > 0 else _bud / len(sel)
    _used, _n = 0.0, 0
    for r in sel:
        _u = float(r["open_p"]) * 100
        if _u <= 0:
            continue
        _lot = min(args.max_lot, int(_per // _u))
        if _cap > 0:
            _lot = min(_lot, int(_cap // _u))
        _lot = max(1, _lot)              # 1件目は最低1単元(現行と同じ)
        r[f"lots_{key}"] = _lot
        r[f"yen_{key}"] = round(_lot * _u, 0)
        _used += _lot * _u
        _n += 1
    return _n, _used


# K = 全候補 / J = 選定あり かつ 流動性上位 watch_j 件だけ読めた前提
_pass_k = [r for r in _rows if r["pass_gap"]]
_j_seen = [r for r in _rows if r["in_j"]][:args.watch_j]
_pass_j = [r for r in _j_seen if r["pass_gap"]]
_nk, _yk = _size(_pass_k, "k")
_nj, _yj = _size(_pass_j, "j")

with open(_out_path, "w", newline="", encoding="utf-8-sig") as f:
    w = _csv.DictWriter(f, fieldnames=_COLS)
    w.writeheader()
    w.writerows(_rows)

_got = sum(1 for r in _rows if float(r["open_p"] or 0) > 0)
_late_n = sum(r["late"] for r in _rows)
print(f"""
{'=' * 74}
■ 結果 — {_out_path}
{'=' * 74}
  読めた       {_got:,}/{len(_rows):,}銘柄   (取得時刻 {_read_ts})
  09:00に未寄  {_late_n:,}銘柄 ({_late_n / max(1, len(_rows)) * 100:.1f}%)
               ⚠ バックテストの実測は15.7%。大きく違うなら要調査

  K(全候補)      合格 {len(_pass_k):,}件 → 建てる {_nk:,}件 / 投入 {_yk / 1e4:,.0f}万
  J(選定あり上位{args.watch_j})  合格 {len(_pass_j):,}件 → 建てる {_nj:,}件 / 投入 {_yj / 1e4:,.0f}万

  ⛔ **発注していません**。この CSV は「その朝 K なら何を建てたか」の記録です。
  ★ 貯めたら 5分足の始値と突合して、**板の始値 = 5分足の始値** かを確認する
    (バックテストの前提そのもの)。ズレるなら K の全数字が影響を受けます。
""")
try:
    cli.unregister_all()
    print("[k_paper] 登録を全解除しました")
except Exception:
    pass
