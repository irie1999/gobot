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

■ ★ 母集団は広く取る (2026-08-16 ユーザー提案)

  気配→始値の関係は **「発注候補であること」を必要としない**。どの銘柄でも
  同じように測れるので、母集団を広げるほど1日で取れる件数が増え、判定に要る
  500〜1,000件に **早く届く**(50件/日なら1ヶ月 / 500件/日なら2営業日)。

  既定の母集団は `lss_proposal_full.py`(選定なしの全候補・価格1,000〜6,000で
  空売り可のフィルタ済み / §18.38 #8)。無ければ cumul → holdout → prime の順。
  `--max-symbols` の既定は **0 = 無制限**。

  ⛔ ただし **締切(--until)を越えない**。バッチごとに時刻を見て打ち切る。
     カーソルは周をまたいで持ち越すので、読み切れなくても取りこぼしが偏らない。

■ ★★ 気配は **寄りに近いほど当たる**。だから最後の周を候補に充てる

  母集団を広げると1周に数十分かかるので、先頭の銘柄は 08:30 の古い気配、
  末尾は 08:55 の新しい気配、というムラが出る。これを是正するため:

  ・`--final-from`(既定 **08:50**)以降は **直前スイープ**。間隔を空けずに
    回し続け、カーソルを **先頭(=今日の候補)に戻す**。いちばん知りたい銘柄が
    いちばん新しい気配で上書きされる。
  ・`to_open_s`(寄りまでの残り秒)を全行に記録する。精度は「何時に読んだか」
    ではなく **「寄りまで何秒か」の関数**として見る。
  ・`--verify` は ①時刻別 ②**残り時間別**(本命) ③**各銘柄の最新気配だけ**
    (= 実運用の精度) の3つを出す。

  ★ 決めるのは1点だけ: **「反転率が許せる範囲に収まる いちばん早い時刻」**。
    そこから 09:00 までの秒数 × 6.5件/秒 = カバーできる銘柄数。
    精度とカバー範囲は直接トレードオフになっている。

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
                help="銘柄を読むファイル(既定は下記の自動検出)")
ap.add_argument("--max-symbols", type=int, default=0,
                help="読む銘柄数の上限。**0=無制限**(既定)。"
                     "母集団が大きいほど検証が早く終わる")
ap.add_argument("--batch", type=int, default=50, help="1バッチの件数(kabu の登録上限)")
ap.add_argument("--workers", type=int, default=2,
                help="並列数。⛔ 上げても速くならず429が増えるだけ(実測)")
ap.add_argument("--every", type=int, default=120, help="スナップショットの間隔(秒)")
ap.add_argument("--until", type=str, default="08:59", help="この時刻まで繰り返す")
ap.add_argument("--final-from", type=str, default="08:50",
                help="★ この時刻以降は『直前スイープ』。間隔を空けずに回し続け、"
                     "カーソルを先頭(=今日の候補)に戻す。空文字で無効")
ap.add_argument("--open-at", type=str, default="09:00",
                help="板寄せの時刻。to_open_s(寄りまでの秒)の基準")
ap.add_argument("--once", action="store_true", help="1回だけ取って終了")
ap.add_argument("--out", type=str, default="", help="出力CSV(既定 preopen_board_<日付>.csv)")
ap.add_argument("--verify", action="store_true",
                help="貯めたログと5分足の始値を突合する(ネットワーク不要)")
ap.add_argument("--gap-bp", type=float, default=50.0,
                help="--verify: 合格とみなすギャップ閾値(bp)")
ap.add_argument("--glob", type=str, default="preopen_board_*.csv",
                help="--verify: 読むログのグロブ")
args = ap.parse_args()

_COLS = ["date", "ts", "hm", "to_open_s", "symbol", "is_cand",
         "bid", "ask", "mid",
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
    _recs: list = []          # (寄りまでの残り秒, (date, symbol), タプル)
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
        _cd = str(r.get("is_cand") or "")
        _t = (_g_pre, _g_act, str(r.get("bid_sign") or ""),
              str(r.get("ask_sign") or ""), _cd)
        _by_hm.setdefault(str(r.get("hm") or "?"), []).append(_t)
        # ★ 寄りまでの残り秒。古いログには無いので ts から復元する。
        try:
            _ts = int(r.get("to_open_s") or 0)
        except Exception:
            _ts = 0
        if not _ts:
            try:
                _h, _m, _s = (int(x) for x in str(r.get("ts") or "").split(":"))
                _ts = 9 * 3600 - (_h * 3600 + _m * 60 + _s)
            except Exception:
                _ts = 0
        _recs.append((_ts, _k, _t))
    if not _by_hm:
        sys.exit(f"[error] 5分足と突合できた行がありません(未突合 {_miss:,}行)。"
                 f"5分足が最新まで揃っているか確認してください")

    _th = float(args.gap_bp)
    print(f"\n{'='*84}")
    print(f"■ 寄り前の気配 vs 始値 — 判定({_th:+.0f}bp以上で合格)がどれだけ一致するか")
    print(f"{'='*84}")
    print(f"{'時刻':>6}{'件数':>8}{'誤差中央':>10}{'90%点':>9}{'95%点':>9}"
          f"{'相関':>7}{'反転':>8}{'誤建て':>8}{'取逃し':>8}")
    def _line(_lbl: str, _v: list) -> None:
        _n = len(_v)
        if _n == 0:
            return
        _err = sorted(abs(a - b) for a, b, *_ in _v)

        def _q(p):
            return _err[min(_n - 1, int(_n * p))]
        _mp = sum(a for a, *_ in _v) / _n
        _ma = sum(b for _, b, *_ in _v) / _n
        _sp = math.sqrt(sum((a - _mp) ** 2 for a, *_ in _v) / max(1, _n - 1))
        _sa = math.sqrt(sum((b - _ma) ** 2 for _, b, *_ in _v) / max(1, _n - 1))
        _cov = sum((a - _mp) * (b - _ma) for a, b, *_ in _v) / max(1, _n - 1)
        _r = _cov / (_sp * _sa) if _sp > 0 and _sa > 0 else float("nan")
        _fp = sum(1 for a, b, *_ in _v if a >= _th and b < _th)   # 誤って建てる
        _fn = sum(1 for a, b, *_ in _v if a < _th and b >= _th)   # 取り逃す
        print(f"{_lbl:>6}{_n:>8,}{_q(0.5):>9.1f}bp{_q(0.9):>8.1f}bp"
              f"{_q(0.95):>8.1f}bp{_r:>7.3f}"
              f"{(_fp + _fn) / _n * 100:>7.1f}%{_fp:>8,}{_fn:>8,}")

    for _hm in sorted(_by_hm):
        _line(_hm, _by_hm[_hm])

    # ★★ 寄りまでの残り時間で切る。**気配は寄りに近いほど当たる**ので、
    #    精度は「何時に読んだか」ではなく「寄りまで何秒か」の関数。
    #    ここが『何分前まで遡れるか』= 何銘柄カバーできるかを決める。
    _BK = [(0, 60, "〜1分前"), (60, 180, "1〜3分"), (180, 300, "3〜5分"),
           (300, 600, "5〜10分"), (600, 1200, "10〜20分"),
           (1200, 1800, "20〜30分"), (1800, 10 ** 9, "30分〜")]
    _by_lead: dict = {}
    for _ts, _k2, _t in _recs:
        for _lo, _hi, _lbl in _BK:
            if _lo <= _ts < _hi:
                _by_lead.setdefault(_lbl, []).append(_t)
                break
    if _by_lead:
        print(f"\n  ── 寄りまでの残り時間で切る（★ここが本命）"
              f"{'─' * 32}")
        for _lo, _hi, _lbl in _BK:
            _line(_lbl, _by_lead.get(_lbl, []))

    # ★ 実運用で使うのは「その銘柄について持っている**いちばん新しい**気配」。
    #   平均ではなくこれが本番の精度になる。
    _fresh: dict = {}
    for _ts, _k2, _t in _recs:
        _p = _fresh.get(_k2)
        if _p is None or _ts < _p[0]:
            _fresh[_k2] = (_ts, _t)
    if _fresh:
        _fv = [t for _, t in _fresh.values()]
        _lead = sorted(ts for ts, _ in _fresh.values())
        _med = _lead[len(_lead) // 2]
        print(f"\n  ── 各銘柄の**最新**の気配だけ（= 実運用の精度）"
              f"{'─' * 30}")
        _line("最新", _fv)
        _fc = [t for _, t in _fresh.values()
               if str(t[4] if len(t) > 4 else "") == "1"]
        if _fc:
            _line("├候補", _fc)
            _line("└候補外", [t for _, t in _fresh.values()
                              if str(t[4] if len(t) > 4 else "") != "1"])
        print(f"    ※ 最新気配の『寄りまでの残り』中央値 {_med // 60}分{_med % 60}秒。"
              f"これが短いほど当たる")

    # ★ 母集団を広げているので、**発注候補だけ**の反転率も必ず並べる。
    #   候補はシグナルが出た銘柄なので気配の付き方が違いうる。ここが大きく
    #   食い違うなら、全銘柄で測った反転率を判断に使ってはいけない。
    _all = [t for _v in _by_hm.values() for t in _v]
    _cand = [t for t in _all if str(t[4] if len(t) > 4 else "") == "1"]
    print(f"  {'-' * 76}")
    _line("全体", _all)
    if _cand:
        _line("候補", _cand)
        _line("候補外",
              [t for t in _all if str(t[4] if len(t) > 4 else "") != "1"])
    else:
        print("  ⚠ is_cand=1 の行がありません(古いログ / 候補ファイルが無かった)")

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

  ★★ **精度とカバー範囲は直接トレードオフ**。寄りに近いほど当たるが、
    近い時間帯ほど読める銘柄は少ない(6.5件/秒)。決めるのはこの1点:

        「反転率が許せる範囲に収まる **いちばん早い時刻**」

    そこから 09:00 までの秒数 × 6.5件/秒 = **カバーできる銘柄数**。
    例) 08:55 まで遡れるなら 300秒 × 6.5 ≒ 1,900銘柄 → 上限50件の制約は消える
        08:59 しか使えないなら 60秒 × 6.5 ≒ 390銘柄 → それでも50件よりは広い

  ⚠ 「各銘柄の最新の気配だけ」の行が **本番の精度**。時刻ごとの行は
    「何分前まで遡れるか」を決めるためのもので、そのまま本番値ではない。

■ ⛔ 判定に足りるサンプル数

  反転率を ±2ポイントで測るには 500〜1,000件は要る。母集団を広く取れば
  1日で数百件貯まるので **数営業日**で届く(発注候補50件だけなら1ヶ月)。
  ⛔ 貯まる前に何度も覗くと、実質的に全期間 in-sample になる。
     **「N件貯まるまで見ない」と先に決めてから始めること**(18.35)。

  ⚠ 母集団を広げたぶん、**発注候補と同じ性質かは別に確かめる**こと。
    候補はシグナルが出た銘柄なので気配の付き方が違いうる。判定に使う前に
    `--symbols-file lss_trades_K.csv` で候補だけに絞った反転率も見ること。
""")


if args.verify:
    _verify()
    sys.exit(0)


# ══════════════════════════════════════════════════════════════════════
#  収集モード (既定)
# ══════════════════════════════════════════════════════════════════════
def _codes_in(_p: str) -> list[str]:
    """1ファイルから銘柄コードを拾う。出現順を保ったまま重複排除する。"""
    if not Path(_p).exists():
        return []
    import re
    _txt = Path(_p).read_text(encoding="utf-8", errors="ignore")
    # .py  … ('7203.T', '銘柄名', 'MACDTF') / ("7203.T", ...) の両方
    _codes = re.findall(r"""['"](\d{4}[A-Z0-9]?)\.T['"]""", _txt)
    # .csv … symbol 列(lss_trades_K.csv など)。出現順=発注順を保つ
    if not _codes and str(_p).lower().endswith(".csv"):
        try:
            for r in _csv.DictReader(open(_p, encoding="utf-8-sig")):
                _s = str(r.get("symbol") or "").upper().removesuffix(".T")
                if _s:
                    _codes.append(_s.split(".")[0])
        except Exception:
            return []
    _seen: set = set()
    _out = []
    for c in _codes:
        if c not in _seen:
            _seen.add(c)
            _out.append(c)
    return _out


# ★ 既定の母集団は **広いほうから**採る (2026-08-16 ユーザー提案)。
#   気配→始値の関係は「発注候補であること」を必要としない。母集団を広げれば
#   1日で数百件取れるので、判定に要る 500〜1,000件が **1ヶ月→数日**に縮む。
#   lss_proposal_full.py = 選定なしの全ペア(価格1,000〜6,000・空売り可で
#   フィルタ済み / §18.38 #8)。判定にそのまま使える唯一の広い母集団。
_SRC_CHAIN = [
    ("lss_proposal_full.py", "選定なしの全候補"),
    ("lss_proposal_cumul.py", "累積マージ(選定あり)"),
    ("holdout_selected_symbols.py", "レポートの選定銘柄"),
    ("symbols_listed_prime.py", "プライム全銘柄"),
]


# ★ 今日の発注候補は **先に**読む。時間切れで一巡できなくても、いちばん
#   知りたい銘柄は必ず取れる。加えて `is_cand` 列に印を付けておくことで、
#   後から「候補だけの反転率」と「全銘柄の反転率」を切り分けられる
#   (候補はシグナルが出た銘柄なので気配の付き方が違う可能性がある)。
_CAND: set = set()


def _today_candidates() -> list[str]:
    for _p in (f"k_signals_{_dt.date.today():%Y%m%d}.csv", "lss_trades_K.csv"):
        _c = _codes_in(_p)
        if _c:
            print(f"[preopen] 今日の候補: {_p} {len(_c):,}銘柄 → 先頭に置きます")
            return _c
    return []


def _load_symbols() -> list[str]:
    if args.symbols:
        return [s.strip().upper().removesuffix(".T").split(".")[0]
                for s in args.symbols.split(",") if s.strip()]
    if args.symbols_file:
        # ⛔ 明示指定で読めなかったら中止する。黙って別ソースに落ちると
        #    「別の母集団で測っていた」事故になる(§18.38 #8 で実際に踏んだ)。
        _c = _codes_in(args.symbols_file)
        if not _c:
            sys.exit(f"[error] {args.symbols_file} から銘柄コードを拾えません(0件)")
        print(f"[preopen] 母集団: {args.symbols_file} (明示指定) {len(_c):,}銘柄")
        return _c
    _pri = _today_candidates()
    _CAND.update(_pri)
    for _p, _lbl in _SRC_CHAIN:
        _c = _codes_in(_p)
        if _c:
            print(f"[preopen] 母集団: {_p} — {_lbl} {len(_c):,}銘柄")
            _seen = set(_pri)
            return _pri + [x for x in _c if x not in _seen]
    if _pri:
        print("[preopen] ⚠ 広い母集団が見つからないので候補だけで走ります")
        return _pri
    sys.exit(
        "[error] 銘柄ソースが1つも見つかりません。\n"
        f"  探した先: {', '.join(p for p, _ in _SRC_CHAIN)}\n"
        "  ⚠ holdout_selected_symbols.py は研究実行が **空** に上書きすること"
        "があります(§18.38)。\n"
        "     `.\\daily` を1回流して作り直すか、--symbols 7203,6758 で"
        "明示指定してください")


try:
    from kabu_api import KabuClient
except Exception as _e:
    sys.exit(f"[error] kabu_api を読めません: {_e}")

_syms = _load_symbols()
if args.max_symbols > 0:
    _syms = _syms[:args.max_symbols]
_out_path = args.out or f"preopen_board_{_dt.date.today():%Y%m%d}.csv"
_new_file = not Path(_out_path).exists()

print(f"[preopen] {len(_syms):,}銘柄 / {args.batch}件バッチ × "
      f"{-(-len(_syms) // args.batch)}回 → {_out_path}")
print(f"[preopen] ⛔ 照会のみ。発注しません。"
      f"⚠ watcher / 発注サーバと同時に走らせないこと(トークンは1つ)")

cli = KabuClient(prod=args.prod, dry_run=True)
cli.connect()


def _snap_batch(_b: list[str]) -> list[dict]:
    """1バッチ(=登録上限50件)ぶん読んで行を返す。"""
    from concurrent.futures import ThreadPoolExecutor
    _now = _dt.datetime.now()
    # ★ 寄りまでの残り秒。**気配は寄りに近いほど当たる**ので、精度は
    #   「何時に読んだか」ではなく「寄りまで何秒か」の関数として見る。
    _t_open = int((_OPEN_AT - _now).total_seconds())
    _rows: list[dict] = []
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
                "hm": f"{_now:%H:%M}", "to_open_s": _t_open,
                "symbol": str(_s),
                "is_cand": 1 if str(_s) in _CAND else 0,
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
    return _rows


def _write(_rows: list[dict]) -> int:
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


def _at(_s: str, _dh: int, _dm: int) -> _dt.datetime:
    try:
        _h, _m = (int(x) for x in str(_s).split(":"))
    except Exception:
        _h, _m = _dh, _dm
    return _dt.datetime.now().replace(hour=_h, minute=_m, second=0,
                                      microsecond=0)


_deadline = _at(args.until, 8, 59)
_OPEN_AT = _at(args.open_at, 9, 0)
# ★ 直前スイープ。ここから先は間隔を空けずに回し続け、カーソルを先頭
#   (=今日の候補)に戻す。気配は寄りに近いほど当たるので、**最後の周を
#   いちばん知りたい銘柄に充てる**。
_FINAL = _at(args.final_from, 8, 50) if str(args.final_from).strip() else None
if _FINAL:
    print(f"[preopen] 直前スイープ: {args.final_from} から間隔なしで回します"
          f"(カーソルを先頭に戻す)")

# ⛔ 締切の判定は **バッチごと**に行う。母集団が大きいと1周が数十分かかるので、
#   周と周のあいだでしか見ないと 09:00 を大きく越え、K の測定と発注枠を
#   食いつぶす(トークンは1つ / §18.5.1)。
# ★ カーソルは周をまたいで持ち越す。母集団が1周ぶん読み切れなくても、
#   次の周が続きから読むので取りこぼしが偏らない。
_cursor = 0
_lap = 0
_tot = 0
_in_final = False
_nb = -(-len(_syms) // max(1, args.batch))
while True:
    _t0 = time.time()
    if _FINAL and not _in_final and _dt.datetime.now() >= _FINAL:
        # 直前スイープに入る。読みかけの周は捨てて先頭(=候補)から読み直す。
        _in_final = True
        _cursor = 0
        print(f"  [{_dt.datetime.now():%H:%M:%S}] ★ 直前スイープ開始 — "
              f"先頭に戻して寄りまで回し続けます", flush=True)
    _b = _syms[_cursor:_cursor + args.batch]
    if not _b:
        break
    _i0 = _cursor
    _tot += _write(_snap_batch(_b))
    _cursor += args.batch
    _wrapped = _cursor >= len(_syms)
    if _wrapped:
        _cursor = 0
        _lap += 1
    print(f"  [{_dt.datetime.now():%H:%M:%S}] "
          f"{_i0 + 1:,}〜{min(_i0 + args.batch, len(_syms)):,}"
          f"/{len(_syms):,}銘柄 ({_i0 // max(1, args.batch) + 1}/{_nb}バッチ) "
          f"{time.time() - _t0:.1f}秒 / 通算{_tot:,}行"
          + (f" ★{_lap}周目 完了" if _wrapped else ""), flush=True)
    if args.once and _wrapped:
        break
    if _dt.datetime.now() >= _deadline:
        print(f"  [{_dt.datetime.now():%H:%M:%S}] 締切 {args.until} に到達。"
              f"ここで打ち切ります", flush=True)
        break
    if _wrapped and not _in_final:
        # 一巡したので --every まで待つ。読み切れていない間は待たずに続ける
        # (時間はすべて「まだ読んでいない銘柄」に使う)。
        # ⛔ 直前スイープ中は待たない。**寄りに近い読みほど価値がある**。
        _sleep = max(0.0, args.every - (time.time() - _t0))
        _sleep = min(_sleep,
                     max(0.0, (_deadline - _dt.datetime.now()).total_seconds()))
        if _sleep > 0:
            time.sleep(_sleep)

try:
    cli.unregister_all()
    print("[preopen] 登録を全解除しました(後続の発注に枠を残す)")
except Exception:
    pass
print(f"[preopen] 完了 → {_out_path} (今朝 {_tot:,}行 / {_lap}周)\n"
      f"  貯まったら: python log_preopen_board.py --verify\n"
      f"  ★ 反転率を ±2ポイントで測るには 500〜1,000件。"
      f"今朝のペースなら約{max(1, -(-1000 // max(1, _tot)))}営業日で届きます。\n"
      f"  ⛔ **届く前に何度も覗かないこと**(実質 in-sample になる / 18.35)")
