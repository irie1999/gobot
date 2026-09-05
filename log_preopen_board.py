r"""log_preopen_board.py — 寄り前の気配値を貯めて、始値とどれだけリンクするか測る

⛔ **照会のみ。絶対に発注しない**(register / board / unregister しか叩かない)。

■ なぜ要るか (2026-08-16 ユーザー提案)

K(09:00確認方式)の最大の制約は **kabu の銘柄登録が50件しかない**こと。
候補は 中央49件・最大277件(選定なしなら中央154件)なので、47〜95% の日で溢れる。
09:00 は数秒しか無いので、バッチで回す余地もほとんど無い(§18.38)。

**ところが 08:00〜09:00 は 3,600秒ある。**
寄り前の実測は **0.5件/秒**(§18.44。場外の6.5件/秒とは別物)なので、
45分で **約1,350銘柄**。1周終わらなくても読んだぶんは全部データになる。

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

  ⚠ 2026-08-17: 速度は当初想定の1/10(**0.5件/秒** / §18.44)と判明したが、
  **広く取る方針は変わらない**。サンプル数は「1周終わるか」ではなく
  「何件読めたか」で決まる: 0.5件/秒 × 45分 = **約1,350件/日**。
  候補だけ(数百)に絞ると同じ銘柄を数回読むだけでユニークは増えず、
  1,000件に届くのが **1営業日 vs 4営業日** と4倍遅くなる。

  既定の母集団は `lss_proposal_full.py`。無ければ cumul → holdout → prime。
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
    そこから 09:00 までの秒数 × **0.5件/秒**(寄り前の実測 / §18.44) =
    カバーできる銘柄数。⛔ 場外の 6.5件/秒 で換算しないこと(10倍ずれる)。
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
ap.add_argument("--watch", type=int, default=50,
                help="用途B(登録する何件を選ぶか)の N。既定50=kabu の登録上限")
ap.add_argument("--glob", type=str, default="preopen_board_*.csv",
                help="--verify: 読むログのグロブ")
args = ap.parse_args()

_COLS = ["date", "ts", "hm", "to_open_s", "symbol", "is_cand",
         "bid", "ask", "mid",
         "bid_sign", "ask_sign", "mkt_sell_qty", "mkt_buy_qty",
         "current_price", "prev_close", "gap_bp",
         # ★ 用途B(登録する50件を気配で選べるか)の比較相手が要る。
         #   いまのやり方=流動性降順 と同じ土俵で捕捉率を出すため(2026-08-18)。
         "liquidity"]


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
    _liqs: dict = {}          # (date, symbol) -> 売買代金(古いログには無い)
    _miss = 0
    _late = 0   # 寄り後に取った行(誤実行ぶん)
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
        # ⛔⛔ **寄り後に取った行は捨てる** (2026-08-18)。
        #   .\jorder を引け後に誤実行すると 15:3x の気配が同じファイルに
        #   追記される(実際に50行入った)。それは『寄り前の気配』ではなく
        #   ザラ場/引けの値なので、混ぜると「気配は始値をよく当てる」という
        #   **逆向きに強い偽の結論**が出る(始値は既に確定しているのだから
        #   当たって当然)。to_open_s <= 0 は寄り以降。
        # ⛔ **_by_hm より前で落とすこと**。後ろに置くと時刻別の表にだけ
        #   残ってしまう(2026-08-18 に一度そう書いた)。
        if _ts <= 0:
            _late += 1
            continue
        _t = (_g_pre, _g_act, str(r.get("bid_sign") or ""),
              str(r.get("ask_sign") or ""), _cd)
        _by_hm.setdefault(str(r.get("hm") or "?"), []).append(_t)
        _recs.append((_ts, _k, _t))
        try:
            _lq = float(r.get("liquidity") or 0)
        except Exception:
            _lq = 0.0
        if _lq > 0:
            _liqs[_k] = max(_liqs.get(_k, 0.0), _lq)
    if _late:
        print(f"⚠ 寄り後({args.open_at}以降)に取った {_late:,}行を除外しました"
              f"（引け後の誤実行ぶん。始値が確定した後の値なので混ぜると"
              f"『気配はよく当たる』という偽の結論になります）")
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

    # ══════════════════════════════════════════════════════════════════
    #  ★★★ 本命の問い: 気配は『登録する50件を選ぶ』のに使えるか (2026-08-18)
    # ══════════════════════════════════════════════════════════════════
    # ⛔ 上の反転率は **「気配だけで判定して発注する」(用途A)** の精度。
    #    A は誤建てがそのまま損になるので数%の精度が要る → 30% で棄却。
    #
    # ★ しかし実際にやりたいのは **「登録する50件を気配で選ぶ」(用途B)**。
    #    最終判定は 09:00 の**実際の始値**なので、
    #      ・誤建て(気配は合格・実際は不合格) → 09:00 で弾かれる = **ほぼ無料**
    #      ・取逃し(気配は不合格・実際は合格) → これだけが本当の損失
    #    そして比較相手は『完璧』ではなく **いまのやり方(流動性降順)** で、
    #    それ自体が 38.7% を取り逃している(捕捉率 61.3% / 2026-08-18 実測)。
    #    → **反転率が高くても、捕捉率で勝てば採用する価値がある。**
    #
    # 判定は §18.24 の作法どおり **ランダム12本の帯**と比べる。
    _dsym: dict = {}          # 日 -> [(気配gap, 実gap, symbol, liq)]
    for _ts0, (_d0, _s0), _t0 in _recs:
        _e = _dsym.setdefault(_d0, {})
        # 同じ銘柄が複数時刻にあるので **寄りに最も近い1本**を採る(実運用と同じ)
        if _s0 not in _e or _ts0 < _e[_s0][0]:
            _e[_s0] = (_ts0, _t0[0], _t0[1], _liqs.get((_d0, _s0), 0.0))
    _NCAP = int(args.watch or 50)

    def _cap(_key, _seed=None) -> tuple:
        """上位N件に入った『実際の合格』の割合(捕捉率)と、その分母。"""
        import random as _rnd
        _hit = _tot = 0
        for _d0, _e in _dsym.items():
            _ok = [s for s, v in _e.items() if v[2] >= _th]
            if not _ok:
                continue
            _tot += len(_ok)
            _lst = list(_e.items())
            if _seed is not None:
                _rnd.Random(f"{_seed}:{_d0}").shuffle(_lst)
            else:
                _lst.sort(key=lambda kv: _key(kv[1]), reverse=True)
            _top = {s for s, _ in _lst[:_NCAP]}
            _hit += sum(1 for s in _ok if s in _top)
        return (_hit / _tot * 100 if _tot else 0.0), _tot

    _rc = [_cap(None, _sd)[0] for _sd in range(12)]
    _rm = sum(_rc) / len(_rc)
    _rs = (sum((x - _rm) ** 2 for x in _rc) / max(1, len(_rc) - 1)) ** 0.5
    _pre, _ntot = _cap(lambda v: v[1])
    _nday = len([1 for _e in _dsym.values()
                 if any(v[2] >= _th for v in _e.values())])
    print(f"\n{'=' * 84}")
    print(f"■ ★★ 本命: 気配で『登録する{_NCAP}件』を選べるか (用途B)")
    print(f"{'=' * 84}")
    print(f"  最終判定は 09:00 の実際の始値なので、**誤建ては 09:00 で弾かれる**。")
    print(f"  効くのは取逃しだけ。比較相手は『完璧』ではなく **いまのやり方**。")
    print(f"  対象: {_nday}営業日 / 実際の合格 {_ntot}件 / "
          f"1日あたり平均 {len(_dsym) and sum(len(_e) for _e in _dsym.values()) // len(_dsym)}銘柄を読んだ想定\n")
    print(f"  {'切り方':<22}{'捕捉率':>8}{'z':>8}   判定")
    print(f"  {'ランダム12本(基準線)':<22}{_rm:>7.1f}%{'':>8}   σ={_rs:.2f}")
    _rows_ax = [("**気配のギャップ降順**", _pre)]
    if any(v[3] for _e in _dsym.values() for v in _e.values()):
        _rows_ax.append(("流動性 降順(現行)", _cap(lambda v: v[3])[0]))
    else:
        print(f"  {'流動性 降順(現行)':<22}{'—':>8}{'':>8}   "
              f"⚠ ログに liquidity 列が無い(次回から入ります)")
    for _lbl, _v in _rows_ax:
        _z = (_v - _rm) / _rs if _rs > 0 else 0.0
        print(f"  {_lbl:<22}{_v:>7.1f}%{_z:>+8.2f}   "
              + ("★帯の外" if abs(_z) >= 2.2 else "帯の中(=効果なし)"))
    print(f"\n  ※ |z|>=2.2 (12本=t(11)) が帯の外。捕捉率は **中間指標**で、"
          f"§18.38 の傾き\n"
          f"    (捕捉+19.1pt = +126,768円/月)で円換算してから月次σ(15万)と"
          f"比べること。")
    if _nday < 15:
        print(f"  ⛔ **まだ {_nday}営業日しかありません**。捕捉率は日ごとの"
              f"ばらつきが大きいので、\n"
              f"     15〜20営業日は貯めてから判断すること"
              f"(いま見えている値はノイズです)。")
    print(f"""
■ 読み方 — **上の反転率は「用途A」の話。判断は「用途B」の捕捉率でする**

  ⛔⛔ 2026-08-18 に一度読み違えた。反転率だけ見て『気配は使えない』と
     結論しかけたが、**測っていた問いが違った**:

    用途A: 気配だけで判定して発注する
           → 誤建てがそのまま損。数%の精度が要る
           → 実測 反転率 30.5% で **棄却**(これは確定)

    用途B: 気配で **登録する {_th:.0f}件を選ぶ** (最終判定は 09:00 の実際の始値)
           → 誤建ては 09:00 で弾かれる = **ほぼ無料**
           → 取逃しだけが損。しかも比較相手は『完璧』ではなく
             **いまのやり方(流動性降順)**で、それ自体が約4割 取り逃している
           → **反転率が高くても捕捉率で勝てば採用する価値がある**

  ★ したがって判断材料は上の「■ ★★ 本命」ブロックの **捕捉率と z**。
    反転率の表は「気配だけで発注できるか」を否定するためだけのもの。

  ★★ **精度とカバー範囲は直接トレードオフ**。寄りに近いほど当たるが、
    近い時間帯ほど読める銘柄は少ない(6.5件/秒)。
        そこから 09:00 までの秒数 × 6.5件/秒 = **カバーできる銘柄数**。
    例) 08:55 まで遡れるなら 300秒 × 6.5 ≒ 1,900銘柄 → 上限50件の制約は消える

  ⚠ ただし **watch を増やす価値そのものが 50 で飽和する**(2026-08-18 実測):
        25→50 +84,692円/月 / 50→100 +24,177 / 100→無制限 +4,357
    予算400万が律速で、1日13〜18件しか建てられないため(§18.42)。
    **予算を上げないと、気配で壁を破っても取り分は月+28,534円が天井。**

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
#
# ⚠ 2026-08-17: 速度は当初想定の1/10(0.5件/秒)と判明したが、**広く取る方針は
#   変わらない**。サンプル数は「1周終わるか」ではなく「何件読めたか」で決まる:
#     0.5件/秒 × 45分 = **約1,350件/日**  ← 1周終わらなくても全部データ
#   候補だけ(299銘柄)に絞ると同じ銘柄を4回読むだけで、ユニークは299のまま。
#   → 1,000件に届くのが **1営業日 vs 4営業日**。広いほうが4倍速い。
#   加えて候補だけだと「シグナルが出た銘柄」という偏った標本になる。
#   広く取れば is_cand 列で候補/候補外の性質差を確認できる。
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
# kabu が登録を受け付けなかったコード。一括登録は all-or-nothing なので、
# 一度弾かれたものを次のバッチに混ぜると **また50件まるごと落ちる**。
_SKIP_CODES: set = set()
# 銘柄 -> 売買代金。用途B(登録する50件を気配で選べるか)の比較相手が
# 『流動性降順』なので、同じログに持っておかないと同じ土俵で測れない。
_LIQ: dict = {}


def _load_liq(_p: str) -> None:
    """候補CSVの liquidity 列を拾う(無ければ何もしない)。"""
    try:
        import csv as _c2
        with open(_p, encoding="utf-8-sig", newline="") as _f:
            for _r in _c2.DictReader(_f):
                _s = str(_r.get("symbol") or "").upper() \
                    .removesuffix(".T").split(".")[0]
                try:
                    _v = float(_r.get("liquidity") or 0)
                except Exception:
                    _v = 0.0
                if _s and _v > 0:
                    _LIQ[_s] = max(_LIQ.get(_s, 0.0), _v)
    except Exception:
        pass
    if _LIQ:
        print(f"[preopen] 売買代金 {len(_LIQ):,}銘柄を {_p} から読みました"
              f"(用途Bの比較相手=流動性降順)")


def _today_candidates() -> list[str]:
    for _p in (f"k_signals_{_dt.date.today():%Y%m%d}.csv", "lss_trades_K.csv"):
        _c = _codes_in(_p)
        if _c:
            print(f"[preopen] 今日の候補: {_p} {len(_c):,}銘柄 → 先頭に置きます")
            _load_liq(_p)
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
    # ⛔⛔ **一括登録は all-or-nothing**(2026-08-19 に実測)。1銘柄でも kabu が
    #   受け付けないコード(上場廃止 / 別市場 / ETF 等)が混ざると 400 で
    #   **50件まるごと落ちる**。その後 get_board は全部 ReadTimeout になり、
    #   再試行(5回×バックオフ)で1バッチに数分かかる = 気配ログが全滅する。
    #   母集団を選定なしの1,540銘柄に広げた初日に発生した。
    #   → **1件ずつ登録し直して、悪いコードだけ捨てる**。
    if _ok == 0 and len(_b) > 1:
        print(f"  ⚠ 一括登録が 0件。**1件ずつ登録し直します**"
              f"(1銘柄でも不正なコードがあると50件まるごと 400 になるため)",
              flush=True)
        # ⛔⛔ **429 は『不正なコード』ではない**(2026-08-21 実測)。
        #   母集団を1,540銘柄(31バッチ)に広げた初日、register を叩きすぎて
        #   kabu のレート制限に入り、一括が 400 → 1件ずつ50連打 → **全部 429**。
        #   旧コードはそれを全部 _SKIP_CODES に入れていたので、
        #   **健全な50銘柄が丸ごと以後スキップ**になっていた。
        #   さらに悪いことに、レート制限の最中に50回連打するのは
        #   **制限を悪化させる**。同じトークンを使う 09:00 の発注(k_open_confirm)
        #   まで巻き添えにするので、429 を見たらフォールバックを打ち切る。
        _good, _bad, _rate = [], [], 0
        for _s1 in _b:
            # ⛔⛔ **戻り値で判定すること**(2026-08-24 修正)。kabu_api.register は
            #   以前 例外を握り潰して print だけしていたので、この except は
            #   **一度も発火しなかった**。結果 429 のガードが死んでいて、
            #   44連続で叩き続けレート制限を悪化させた(その朝の 09:00 発注に影響)。
            try:
                _ok1 = cli.register(_s1)
                _err1 = "" if _ok1 else str(getattr(cli, "last_register_error", ""))
            except Exception as _re1:            # 将来 raise に変えても拾えるように
                _ok1, _err1 = False, str(_re1)
            if _ok1:
                _good.append(_s1)
                _rate = 0
            else:
                if "429" in _err1:
                    _rate += 1
                    if _rate >= 3:
                        print(f"  ⛔ **レート制限(429)が続くのでフォールバックを"
                              f"中止**します。残り {len(_b) - len(_good) - len(_bad)}"
                              f"銘柄はスキップコードに入れません"
                              f"(銘柄のせいではないため)。\n"
                              f"     ⚠ register を叩きすぎています。母集団を"
                              f"減らすか --every を延ばしてください。\n"
                              f"     ⚠ 同じトークンを使う 09:00 の発注まで"
                              f"巻き添えになります", flush=True)
                        time.sleep(5.0)       # 少しだけ冷ます
                        break
                else:
                    _rate = 0
                    _bad.append(_s1)          # 本当に登録できないコードだけ
        _ok = len(_good)
        _b = _good
        if _bad:
            _SKIP_CODES.update(_bad)
            print(f"  ⚠ 登録できない {len(_bad)}銘柄を以後スキップします: "
                  f"{', '.join(map(str, _bad[:10]))}"
                  + (" …" if len(_bad) > 10 else ""), flush=True)
    if _ok < len(_b):
        print(f"  ⚠ 登録 {_ok}/{len(_b)}件 しか受理されませんでした "
              f"(kabu の上限は50件)", flush=True)
    if _ok == 0:
        print(f"  ⛔ このバッチは1件も登録できませんでした。板は読まずに次へ"
              f"(全件 ReadTimeout で数分溶かすのを避ける)", flush=True)
        return _rows

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
                # ★ 用途B の比較相手(いまのやり方=流動性降順)を同じ土俵で
                #   測るために要る。取れない銘柄は 0 = 最後尾扱い(18.21)。
                "liquidity": _LIQ.get(str(_s), 0.0),
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
_t_start = time.time()   # 母集団の切り詰め判定に使う実測開始時刻
_capped = False          # 切り詰めは1回だけ
while True:
    _t0 = time.time()
    if _FINAL and not _in_final and _dt.datetime.now() >= _FINAL:
        # 直前スイープに入る。読みかけの周は捨てて先頭(=候補)から読み直す。
        _in_final = True
        _cursor = 0
        print(f"  [{_dt.datetime.now():%H:%M:%S}] ★ 直前スイープ開始 — "
              f"先頭に戻して寄りまで回し続けます", flush=True)
    # ⛔ 一度 kabu に弾かれたコードを次のバッチに混ぜると、一括登録が
    #   all-or-nothing なので **また50件まるごと 400** になる(2026-08-19)。
    _b = [x for x in _syms[_cursor:_cursor + args.batch]
          if str(x) not in _SKIP_CODES]
    if not _b:
        _cursor += args.batch
        if _cursor >= len(_syms):
            _cursor = 0
            _lap += 1
        continue
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
    # ★★ **窓に収まる本数まで母集団を切り詰める**(2026-08-24)。
    #   場中の /board は 0.5〜1.5件/秒しか出ない(§18.44)。1,540銘柄=31バッチを
    #   45分で回るのは無理で、実測は 9/31バッチで締切に当たった。
    #   ⛔ 途中で切れると **毎朝おなじ先頭のバッチだけ**が読まれ、後半の銘柄は
    #     永久に1件も貯まらない。母集団を広げた意味が消える。
    #   → 実測の1バッチ時間から到達可能な本数を出し、そこまでに絞る。
    #     絞ったことは必ず出す(§18.24「no silent caps」)。
    _elapsed = time.time() - _t_start
    _done_b = max(1, (_i0 // max(1, args.batch)) + 1)
    _per_b = _elapsed / _done_b
    _left_s = (_deadline - _dt.datetime.now()).total_seconds()
    _can_b = _done_b + int(max(0.0, _left_s) // max(1.0, _per_b))
    # ⛔ **締切を過ぎてから起動された場合は切り詰めない**(2026-08-24)。
    #   _left_s が負なので「1バッチしか回れない = 母集団を50件に切れ」と
    #   誤作動する。窓の外の実行(引け後の誤起動など)は直後の締切判定で
    #   止まるので、そこに任せる。
    if not _capped and _lap == 0 and _left_s > 0 and _can_b < _nb:
        _keep = max(args.batch, _can_b * args.batch)
        if _keep < len(_syms):
            print(f"  ⛔ このペース(1バッチ {_per_b:.0f}秒)では締切 {args.until} "
                  f"までに {_can_b}/{_nb}バッチしか回れません。\n"
                  f"     母集団を **{len(_syms):,} → {_keep:,}銘柄** に切り詰めます"
                  f"(流動性/候補の上位から)。\n"
                  f"     ⚠ 途中で切れると毎朝おなじ先頭しか貯まりません。"
                  f"切り詰めれば毎朝 全部を1周できます。\n"
                  f"     広げたいなら --until を遅く / --batch を大きく / "
                  f"母集団を --symbols で絞ってください", flush=True)
            _syms = _syms[:_keep]
            _nb = -(-len(_syms) // max(1, args.batch))
            if _cursor >= len(_syms):
                _cursor = 0
                _lap += 1
        _capped = True
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
