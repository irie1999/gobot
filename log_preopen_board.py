r"""log_preopen_board.py — 寄り前の気配値を貯めて、始値とどれだけリンクするか測る

⛔ **照会のみ。絶対に発注しない**(register / board / unregister しか叩かない)。

■ なぜ要るか (2026-08-16 ユーザー提案)

K(09:00確認方式)の最大の制約は **kabu の銘柄登録が50件しかない**こと。
候補は 中央49件・最大277件(選定なしなら中央154件)なので、47〜95% の日で溢れる。
09:00 は数秒しか無いので、バッチで回す余地もほとんど無い(§18.38)。

**ところが 08:00〜09:00 は 3,600秒ある。**
50件バッチがコールドで140秒かかっても、1時間あれば 20周以上 = 1,000銘柄読める。

  → **寄り前の気配で判定できるなら、候補数の制限が事実上消える。**

これが本命の理由で、「気配の精度を知りたい」は手段でしかない。

■ ⛔ バックテストできない。今日から貯めるしかない

板・気配の履歴は **どのデータにも存在しない**(J-Quants の5分足にも yfinance にも無い)。
過去に遡って検証する手段が無いので、前向きにログを貯める以外に方法がない。
**始めるのが遅いほど結論も遅くなる。**

■ ★ 見るのは「値のズレ」ではなく「判定の反転」

K が気配に求めるのは値そのものではなく **「+50bp 以上か」の判定**だけ。
気配が 3bp ずれても、+120bp の銘柄の判定は変わらない。危ないのは閾値の際どい
銘柄だけ。したがって測るべきは

    気配で合格 → 実際は不合格   (誤って建てる)
    気配で不合格 → 実際は合格   (取り逃す)

の **反転率**。レポートの「🕗 判定マージン」ブロックが「気配が m bp ずれたら
何件ひっくり返るか」を既に出しているので、ここで m の分布が分かれば即答が出る。

■ 使い方

  # 毎朝 08:45〜08:59 に走らせる(既定は 08:59 まで2分おきにループ)
  python log_preopen_board.py --prod
  python log_preopen_board.py --prod --until 08:59 --every 120
  python log_preopen_board.py --prod --once            # 1回だけ

  # 貯まったら突合(5分足の始値と比べる)。ネットワーク不要
  python log_preopen_board.py --verify
  python log_preopen_board.py --verify --gap-bp 50     # 判定の閾値

■ ⚠ 運用上の注意

  ・**kabu の有効トークンは1つ**。watcher / 発注サーバと同時に走らせない。
    08:45〜08:55 に取って終える運用が安全(`.\watch` は寄り前に起動する)。
  ・登録を最後に全解除するので、後続の発注に登録枠を残さない。
  ・⛔ 気配が使えると分かっても、**バックテストの母集団は増やせない**
    (気配の履歴が無いので過去は測れない)。増やせるのは「これから」だけ。
"""
from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
import math
import os
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser(
    description="寄り前の気配値を貯めて始値との関係を測る(照会のみ・発注しない)")
ap.add_argument("--prod", action="store_true", help="本番(18080)。既定はデモ(18081)")
ap.add_argument("--symbols", type=str, default="", help="カンマ区切りの銘柄コード")
ap.add_argument("--symbols-file", type=str, default="",
                help="銘柄を読むファイル(既定 holdout_selected_symbols.py)")
ap.add_argument("--max-symbols", type=int, default=300,
                help="読む銘柄数の上限(50件バッチで回す)")
ap.add_argument("--batch", type=int, default=50, help="1バッチの件数(kabu の登録上限)")
ap.add_argument("--workers", type=int, default=2,
                help="並列数。⛔ 上げても速くならず429が増えるだけ(実測)")
ap.add_argument("--every", type=int, default=120, help="スナップショットの間隔(秒)")
ap.add_argument("--until", type=str, default="08:59", help="この時刻まで繰り返す")
ap.add_argument("--once", action="store_true", help="1回だけ取って終了")
ap.add_argument("--out", type=str, default="", help="出力CSV(既定 preopen_board_<日付>.csv)")
ap.add_argument("--verify", action="store_true",
                help="貯めたログと5分足の始値を突合する(ネットワーク不要)")
ap.add_argument("--gap-bp", type=float, default=50.0,
                help="--verify: 合格とみなすギャップ閾値(bp)")
ap.add_argument("--glob", type=str, default="preopen_board_*.csv",
                help="--verify: 読むログのグロブ")
args = ap.parse_args()

_COLS = ["date", "ts", "hm", "symbol", "bid", "ask", "mid",
         "bid_sign", "ask_sign", "mkt_sell_qty", "mkt_buy_qty",
         "current_price", "prev_close", "gap_bp"]


# ══════════════════════════════════════════════════════════════════════
#  突合モード (--verify)
# ══════════════════════════════════════════════════════════════════════
def _verify() -> None:
    import glob as _glob
    _files = sorted(_glob.glob(args.glob))
    if not _files:
        sys.exit(f"[error] {args.glob} が1つもありません。\n"
                 f"  まず毎朝 `python log_preopen_board.py --prod` で貯めてください。\n"
                 f"  ⛔ 気配の履歴はどのデータにも無いので、過去に遡れません。")
    _rows: list[dict] = []
    for _f in _files:
        for r in _csv.DictReader(open(_f, encoding="utf-8-sig")):
            _rows.append(r)
    if not _rows:
        sys.exit("[error] ログが空です")

    # ── 実際の始値を5分足から取る ────────────────────────────────────
    try:
        import daytrade_data as _dd
    except Exception as e:
        sys.exit(f"[error] daytrade_data を読めません: {e}")
    _syms = sorted({str(r["symbol"]) for r in _rows})
    _dates = sorted({str(r["date"]) for r in _rows})
    _days = (_dt.date.today() - _dt.date.fromisoformat(_dates[0])).days + 10
    print(f"[verify] ログ {len(_rows):,}行 / {len(_syms):,}銘柄 / "
          f"{len(_dates)}営業日 ({_dates[0]} 〜 {_dates[-1]})", flush=True)
    print(f"[verify] 5分足を読みます ({_days}日ぶん)…", flush=True)
    _open: dict = {}          # (date, symbol) -> 始値
    _bat = _dd.load_intraday_batch([f"{s}.T" for s in _syms], days=_days)
    for _sy, _df in (_bat or {}).items():
        if _df is None or len(_df) == 0:
            continue
        _c = str(_sy).upper().removesuffix(".T").split(".")[0]
        for _d, _dd2 in _dd.split_by_day(_df).items():
            if len(_dd2):
                _open[(str(_d)[:10], _c)] = float(_dd2["open"].iloc[0])

    # ── 時刻(hm)ごとに集計 ──────────────────────────────────────────
    _by_hm: dict = {}
    _miss = 0
    for r in _rows:
        _k = (str(r["date"]), str(r["symbol"]))
        _o = _open.get(_k)
        try:
            _mid = float(r.get("mid") or 0)
            _pc = float(r.get("prev_close") or 0)
        except Exception:
            continue
        if not _o or _mid <= 0 or _pc <= 0:
            _miss += 1
            continue
        _g_pre = (_mid - _pc) / _pc * 1e4        # 気配から見たギャップ(bp)
        _g_act = (_o - _pc) / _pc * 1e4          # 実際のギャップ(bp)
        _by_hm.setdefault(str(r.get("hm") or "?"), []).append(
            (_g_pre, _g_act, str(r.get("bid_sign") or ""),
             str(r.get("ask_sign") or "")))
    if not _by_hm:
        sys.exit(f"[error] 5分足と突合できた行がありません(未突合 {_miss:,}行)。"
                 f"5分足が最新まで揃っているか確認してください")

    _th = float(args.gap_bp)
    print(f"\n{'='*84}")
    print(f"■ 寄り前の気配 vs 始値 — 判定({_th:+.0f}bp以上で合格)がどれだけ一致するか")
    print(f"{'='*84}")
    print(f"{'時刻':>6}{'件数':>8}{'誤差中央':>10}{'90%点':>9}{'95%点':>9}"
          f"{'相関':>7}{'反転':>8}{'誤建て':>8}{'取逃し':>8}")
    for _hm in sorted(_by_hm):
        _v = _by_hm[_hm]
        _err = sorted(abs(a - b) for a, b, _, _ in _v)
        _n = len(_v)

        def _q(p):
            return _err[min(_n - 1, int(_n * p))]
        _mp = sum(a for a, _, _, _ in _v) / _n
        _ma = sum(b for _, b, _, _ in _v) / _n
        _sp = math.sqrt(sum((a - _mp) ** 2 for a, _, _, _ in _v) / max(1, _n - 1))
        _sa = math.sqrt(sum((b - _ma) ** 2 for _, b, _, _ in _v) / max(1, _n - 1))
        _cov = sum((a - _mp) * (b - _ma) for a, b, _, _ in _v) / max(1, _n - 1)
        _r = _cov / (_sp * _sa) if _sp > 0 and _sa > 0 else float("nan")
        _fp = sum(1 for a, b, _, _ in _v if a >= _th and b < _th)   # 誤って建てる
        _fn = sum(1 for a, b, _, _ in _v if a < _th and b >= _th)   # 取り逃す
        print(f"{_hm:>6}{_n:>8,}{_q(0.5):>9.1f}bp{_q(0.9):>8.1f}bp"
              f"{_q(0.95):>8.1f}bp{_r:>7.3f}"
              f"{(_fp + _fn) / _n * 100:>7.1f}%{_fp:>8,}{_fn:>8,}")

    print(f"\n  ※ 未突合 {_miss:,}行(5分足が無い / 気配が取れていない)")
    print(f"""
■ 読み方 — **値のズレではなく『判定の反転』を見ること**

  K が気配に求めるのは「{_th:+.0f}bp 以上か」の判定だけ。値が数bpずれても
  余裕のある銘柄は判定が変わらない。危ないのは閾値の際どい銘柄だけ。

  ★ **反転率が数%なら、寄り前の気配で判定してよい**。そうすれば 08:00〜09:00 の
    1時間を使えるので、50件バッチを何周も回せる = **登録上限が事実上消える**。
    (09:00 は数秒しか無いので50件が天井。ここが K 最大の制約 / §18.38)

  ⛔ 反転が10%を超えるなら気配では判定できない。09:00 の始値方式を維持し、
    候補は前夜に流動性上位50件へ絞るしかない。

  ⚠ 早い時刻でも精度が足りるなら、その分だけ多くのバッチを回せる。
    時刻ごとの行を見て **「何時まで遡れるか」** を決めること。

■ ⛔ 判定に足りるサンプル数

  反転率を ±2ポイントで測るには 500〜1,000件は要る。50件/日なら **1ヶ月**。
  ⛔ 貯まる前に何度も覗くと、実質的に全期間 in-sample になる。
     **「N日貯まるまで見ない」と先に決めてから始めること**(18.35)。
""")


if args.verify:
    _verify()
    sys.exit(0)


# ══════════════════════════════════════════════════════════════════════
#  収集モード (既定)
# ══════════════════════════════════════════════════════════════════════
def _load_symbols() -> list[str]:
    if args.symbols:
        return [s.strip().upper().removesuffix(".T").split(".")[0]
                for s in args.symbols.split(",") if s.strip()]
    _p = args.symbols_file or "holdout_selected_symbols.py"
    if not Path(_p).exists():
        sys.exit(f"[error] {_p} がありません。--symbols で明示指定してください")
    import re
    _txt = Path(_p).read_text(encoding="utf-8")
    # .py  … ('7203.T', '銘柄名', 'MACDTF') / ("7203.T", ...) の両方
    # .csv … symbol 列(lss_trades_K.csv など)。出現順=発注順を保つ
    _codes = re.findall(r"""['"](\d{4}[A-Z0-9]?)\.T['"]""", _txt)
    if not _codes and str(_p).lower().endswith(".csv"):
        for r in _csv.DictReader(open(_p, encoding="utf-8-sig")):
            _s = str(r.get("symbol") or "").upper().removesuffix(".T")
            if _s:
                _codes.append(_s.split(".")[0])
    if not _codes:
        sys.exit(
            f"[error] {_p} から銘柄コードを拾えません(0件)。\n"
            f"  ⚠ holdout_selected_symbols.py が **空** になっていることが"
            f"あります(研究実行が上書きする / §18.38)。\n"
            f"     `.\\daily` を1回流して作り直すか、\n"
            f"     --symbols-file lss_trades_K.csv / --symbols 7203,6758 "
            f"で指定してください")
    # 出現順を保ったまま重複排除(発注順=流動性降順を尊重する)
    _seen: set = set()
    _out = []
    for c in _codes:
        if c not in _seen:
            _seen.add(c)
            _out.append(c)
    return _out


try:
    from kabu_api import KabuClient
except Exception as _e:
    sys.exit(f"[error] kabu_api を読めません: {_e}")

_syms = _load_symbols()[:max(1, args.max_symbols)]
_out_path = args.out or f"preopen_board_{_dt.date.today():%Y%m%d}.csv"
_new_file = not Path(_out_path).exists()

print(f"[preopen] {len(_syms):,}銘柄 / {args.batch}件バッチ × "
      f"{-(-len(_syms) // args.batch)}回 → {_out_path}")
print(f"[preopen] ⛔ 照会のみ。発注しません。"
      f"⚠ watcher / 発注サーバと同時に走らせないこと(トークンは1つ)")

cli = KabuClient(prod=args.prod, dry_run=True)
cli.connect()


def _snap() -> int:
    """1周ぶん。全バッチを読んで CSV に追記し、書いた行数を返す。"""
    from concurrent.futures import ThreadPoolExecutor
    _now = _dt.datetime.now()
    _rows: list[dict] = []
    for _i in range(0, len(_syms), args.batch):
        _b = _syms[_i:_i + args.batch]
        try:
            cli.unregister_all()
        except Exception:
            pass
        _res = cli.register_many(_b)
        _ok = len((_res or {}).get("RegistList") or [])
        if _ok < len(_b):
            print(f"  ⚠ 登録 {_ok}/{len(_b)}件 しか受理されませんでした "
                  f"(kabu の上限は50件)", flush=True)

        def _one(s):
            try:
                return s, cli.get_board(s)
            except Exception:
                return s, {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for _s, _bd in ex.map(_one, _b):
                if not _bd:
                    continue
                _bid = float(_bd.get("BidPrice") or 0)
                _ask = float(_bd.get("AskPrice") or 0)
                # 気配の代表値。片側しか無ければあるほうを使う
                # (特別気配は片側だけ出ることがある)。
                _mid = ((_bid + _ask) / 2 if _bid > 0 and _ask > 0
                        else (_bid or _ask or 0))
                _pc = float(_bd.get("PreviousClose") or 0)
                _rows.append({
                    "date": f"{_now:%Y-%m-%d}", "ts": f"{_now:%H:%M:%S}",
                    "hm": f"{_now:%H:%M}", "symbol": str(_s),
                    "bid": _bid, "ask": _ask, "mid": _mid,
                    # ★ 特別気配 / 連続約定気配のフラグ。実現ギャップには
                    #   畳み込まれて消える情報で、狙うならここ(18.35)。
                    "bid_sign": _bd.get("BidSign") or "",
                    "ask_sign": _bd.get("AskSign") or "",
                    # ★ 板寄せ前の需給不均衡。同上。
                    "mkt_sell_qty": _bd.get("MarketOrderSellQty") or 0,
                    "mkt_buy_qty": _bd.get("MarketOrderBuyQty") or 0,
                    "current_price": _bd.get("CurrentPrice") or 0,
                    "prev_close": _pc,
                    "gap_bp": (round((_mid - _pc) / _pc * 1e4, 1)
                               if _mid > 0 and _pc > 0 else ""),
                })
    if not _rows:
        return 0
    global _new_file
    with open(_out_path, "a", newline="", encoding="utf-8-sig") as f:
        w = _csv.DictWriter(f, fieldnames=_COLS)
        if _new_file:
            w.writeheader()
            _new_file = False
        w.writerows(_rows)
    return len(_rows)


try:
    _hh, _mm = (int(x) for x in str(args.until).split(":"))
except Exception:
    _hh, _mm = 8, 59
_deadline = _dt.datetime.now().replace(hour=_hh, minute=_mm, second=0,
                                       microsecond=0)
_n_snap = 0
while True:
    _t0 = time.time()
    _n = _snap()
    _n_snap += 1
    print(f"  [{_dt.datetime.now():%H:%M:%S}] {_n:,}行 追記 "
          f"({time.time() - _t0:.1f}秒 / 通算{_n_snap}周)", flush=True)
    if args.once or _dt.datetime.now() >= _deadline:
        break
    _sleep = max(0.0, args.every - (time.time() - _t0))
    if _dt.datetime.now() + _dt.timedelta(seconds=_sleep) > _deadline:
        _sleep = max(0.0, (_deadline - _dt.datetime.now()).total_seconds())
        if _sleep <= 0:
            break
    time.sleep(_sleep)

try:
    cli.unregister_all()
    print("[preopen] 登録を全解除しました(後続の発注に枠を残す)")
except Exception:
    pass
print(f"[preopen] 完了 → {_out_path}\n"
      f"  貯まったら: python log_preopen_board.py --verify\n"
      f"  ⛔ 反転率を ±2ポイントで測るには 500〜1,000件(50件/日なら約1ヶ月)。\n"
      f"     **貯まる前に何度も覗かないこと**(実質 in-sample になる / 18.35)")
