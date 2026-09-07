#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kabuステーションの **PUSH配信(WebSocket)** で板を受け取る。

★ なぜ要るのか (2026-09-07)
  09:00 の始値を REST で1銘柄ずつ読むと、本番の実測で **50銘柄 36.5秒**
  (0.73秒/銘柄 / §18.44)。先頭で読まれた銘柄は速いが、最後の銘柄は
  36秒待たされる。**平均18秒**の遅れになる。

  §18.44 の1分足実測(2,311件)だと、始値からの乖離はこう進む:

      1分 -15.8bp / 2分 -26.8 / 3分 -29.4 / 5分 -36.6   （60〜71%が不利側）

  凹型なので18秒なら **約 -6bp**。N のグロスのエッジは +15.7bp/件なので、
  **読み取りの待ち時間だけでエッジの4割**を落としている計算になる。

  PUSH は /register した銘柄の板更新が**向こうから飛んでくる**ので、
  ポーリングの待ち時間そのものが消える。「まだ寄っていないか」を
  問い合わせるのではなく **寄った瞬間に届く**。

⛔ **登録上限50件は REST と同じ**(§18.45)。これは watch を増やす手段ではない。
   ここで取りに行くのは **速度だけ**。件数を増やしたいなら別のデータ源が要る。

⛔ **接続は1本だけ**。kabu は WebSocket を1接続しか受け付けず、2本目が
   繋ぐと1本目が切られる。`.\\nexec` と同時に別プロセスで繋がないこと。

⚠ PUSH は **変化したときだけ**飛んでくる。動きのない銘柄は一度も来ない。
   だから REST の読み取りを捨ててはいけない。**PUSH で埋まったぶんだけ
   REST を省く**、という使い方にする(呼び出し側の責任)。

使い方(照会のみ・**発注しない**)
    python kabu_ws.py --prod --symbols 7203,9984 --seconds 60
    python kabu_ws.py --prod --from-signals --seconds 120

    ⛔ 実行は **引け後**にすること。kabu の有効トークンは1つなので、
       `.\\nexec` / `.\\norder` / watcher が動いている間は 401 で取り合う。
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import threading as _th
import time as _time

JST = _dt.timezone(_dt.timedelta(hours=9))


class BoardStream:
    """PUSH配信を裏で受け続け、最新の板を辞書に溜めるだけの入れ物。

    ★ 設計方針: **このクラスは判断をしない。** 始値が有効かどうか、
      日付が今日かどうかの判定は呼び出し側(k_open_confirm._mk_row)が既に
      持っているので、ここでは触らない。受けたものをそのまま渡す。
    """

    def __init__(self, base_url: str, verbose: bool = True):
        # http://localhost:18080 → ws://localhost:18080/kabusapi/websocket
        self.url = base_url.replace("http://", "ws://") + "/kabusapi/websocket"
        self.verbose = verbose
        self._board: dict[str, dict] = {}
        self._lock = _th.Lock()
        self._ws = None
        self._th: _th.Thread | None = None
        self._stop = False
        self.connected = False
        self.err = ""
        # ── 計測用。**これが目的**なので必ず残す ──────────────────
        self.n_msg = 0
        self.t_connect: float = 0.0
        self.first_msg_ts: str = ""
        # 銘柄ごとに「始値が初めて 0 でなくなった時刻」。REST の到着時刻と
        # 突き合わせて、PUSH が何秒速かったかを実測するために使う。
        self.open_seen: dict[str, str] = {}

    # ── 接続 ────────────────────────────────────────────────
    def start(self, wait_s: float = 5.0) -> bool:
        """裏スレッドで接続する。接続できたら True。

        websocket-client が無ければ False を返すだけで例外は投げない
        (呼び出し側は REST にそのまま落ちればよい)。
        """
        try:
            import websocket as _wsmod        # pip install websocket-client
        except Exception:
            self.err = ("websocket-client が入っていません "
                        "(pip install websocket-client)")
            if self.verbose:
                print(f"  ⚠ PUSH配信を使いません: {self.err}", flush=True)
            return False

        def _on_open(_w):
            self.connected = True
            self.t_connect = _time.time()
            if self.verbose:
                print(f"  [ws] 接続しました {self.url}", flush=True)

        def _on_msg(_w, _m):
            try:
                d = _json.loads(_m)
            except Exception:
                return
            sym = str(d.get("Symbol") or "")
            if not sym:
                return
            # ⚠ **update で重ねる**(置き換えない)。PUSH が部分更新でも
            #   既に持っている始値を消さないため。
            with self._lock:
                self.n_msg += 1
                if not self.first_msg_ts:
                    self.first_msg_ts = f"{_dt.datetime.now(JST):%H:%M:%S.%f}"[:-3]
                cur = self._board.setdefault(sym, {})
                cur.update(d)
                if sym not in self.open_seen:
                    try:
                        if float(cur.get("OpeningPrice") or 0) > 0:
                            self.open_seen[sym] = (
                                f"{_dt.datetime.now(JST):%H:%M:%S.%f}"[:-3])
                    except Exception:
                        pass

        def _on_err(_w, _e):
            self.err = str(_e)
            self.connected = False

        def _on_close(_w, *_a):
            self.connected = False

        self._ws = _wsmod.WebSocketApp(
            self.url, on_open=_on_open, on_message=_on_msg,
            on_error=_on_err, on_close=_on_close)

        def _run():
            # ⚠ 落ちたら繋ぎ直す。ただし止めると決めたら二度と繋がない。
            while not self._stop:
                try:
                    self._ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:                    # noqa: BLE001
                    self.err = str(e)
                if self._stop:
                    break
                self.connected = False
                _time.sleep(1.0)

        self._th = _th.Thread(target=_run, daemon=True)
        self._th.start()
        _t0 = _time.time()
        while _time.time() - _t0 < wait_s:
            if self.connected:
                return True
            _time.sleep(0.1)
        if self.verbose:
            print(f"  ⚠ PUSH配信に {wait_s:.0f}秒で繋がりませんでした"
                  f"{': ' + self.err if self.err else ''}。REST だけで進みます",
                  flush=True)
        return False

    def stop(self) -> None:
        self._stop = True
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    # ── 読み出し ─────────────────────────────────────────────
    def snapshot(self) -> dict:
        """symbol -> board の浅いコピー。呼び出し側が触っても壊れない。"""
        with self._lock:
            return {k: dict(v) for k, v in self._board.items()}

    def has_open(self, sym: str) -> bool:
        """その銘柄の始値が PUSH で届いているか。"""
        with self._lock:
            try:
                return float((self._board.get(sym) or {}).get(
                    "OpeningPrice") or 0) > 0
            except Exception:
                return False

    @property
    def ok(self) -> bool:
        return bool(self.connected)

    def describe(self) -> str:
        with self._lock:
            _n, _s, _o = self.n_msg, len(self._board), len(self.open_seen)
        return (f"受信 {_n:,}件 / 銘柄 {_s:,} / 始値あり {_o:,} / "
                f"{'接続中' if self.connected else '切断'}"
                + (f" / err={self.err}" if self.err else ""))


# ── 単体での疎通確認 (照会のみ・**発注しない**) ─────────────────────
if __name__ == "__main__":
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prod", action="store_true", help="本番(18080)")
    ap.add_argument("--symbols", type=str, default="",
                    help="カンマ区切り。省略時は --from-signals")
    ap.add_argument("--from-signals", action="store_true",
                    help="n_signals_<今日>.csv の銘柄を使う")
    ap.add_argument("--signals-csv", type=str, default="",
                    help="--from-signals の読み先(既定 n_signals_<今日>.csv)")
    ap.add_argument("--seconds", type=int, default=60, help="受信する秒数")
    ap.add_argument("--compare-rest", action="store_true",
                    help="同じ銘柄を REST でも1周読み、所要秒数を並べる")
    # ★★ 本命 (2026-09-07)。登録上限50件を **回して** 超えられるか。
    #   §18.44 は「2周目も REST が36秒かかる」ので棄却したが、それは
    #   読み取りの話。PUSH なら読む時間はゼロで、残るのは
    #   **登録してから配信が始まるまで**の待ちだけ。始値は動かないので
    #   遅れて届いても値は正しい。そこが何秒かで採否が決まる。
    ap.add_argument("--rotate", type=int, default=0,
                    help="N銘柄を50件ずつ回して、バッチごとの"
                         "『登録→初回受信』『登録→全件揃う』秒数を測る")
    ap.add_argument("--batch", type=int, default=50, help="--rotate の1バッチ")
    ap.add_argument("--batch-wait", type=int, default=90,
                    help="--rotate で1バッチを待つ上限秒")
    a = ap.parse_args()

    # ⛔ 発注系は一切 import しない。KabuClient は登録と(比較時のみ)/board だけ。
    from kabu_api import KabuClient

    _syms: list[str] = []
    if a.symbols:
        _syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    else:
        _p = a.signals_csv or f"n_signals_{_dt.datetime.now(JST):%Y%m%d}.csv"
        if not os.path.exists(_p):
            sys.exit(f"[error] {_p} がありません。--symbols で明示してください")
        import csv as _csv
        _all: list[str] = []
        _shadow: list[str] = []       # ★ 51〜150位(shadow_n=1)。順位順に並ぶ
        with open(_p, encoding="utf-8-sig") as _f:
            for _r in _csv.DictReader(_f):
                _c = str(_r.get("symbol") or _r.get("code") or "").strip()
                if not _c:
                    continue
                _c = _c.replace(".T", "")
                _all.append(_c)
                # ★ 朝に実際に watch した50件を優先する(候補は126件ありうる)。
                #   watched_n が無い古い CSV なら _all のほうを使う。
                _tru = ("", "0", "False", "false")
                if str(_r.get("watched_n") or _r.get("watched") or "").strip() \
                        not in _tru:
                    _syms.append(_c)
                elif str(_r.get("shadow_n") or "").strip() not in _tru:
                    _shadow.append(_c)
        if a.rotate:
            # ★★ 回すときは **watched(発注対象) + shadow(51〜150位)** を
            #   この順に並べる。batch1 が実際の発注対象、batch2以降が
            #   シャドーの裾になり、**朝の本番と同じ顔ぶれ・同じ順**で測れる。
            #   shadow_n が無い古い CSV なら候補全部にフォールバックする。
            if _shadow:
                _syms = _syms + _shadow
                print(f"[info] {_p}: 発注対象(watched) {len(_syms) - len(_shadow)}件"
                      f" + **シャドー(51位以降) {len(_shadow)}件** = {len(_syms)}件"
                      f" を、この順で回します")
            else:
                _syms = _all
                print(f"[info] {_p} に shadow_n がないので候補 {len(_syms)}件"
                      f" 全部を回します(順位順とは限りません)")
        elif not _syms:
            print("[info] watched_n が無いので先頭50件を使います")
            _syms = _all
        else:
            print(f"[info] {_p} の watched_n から {len(_syms)}件")
    if not _syms:
        sys.exit("[error] 銘柄が0件です")
    if a.rotate:
        # ★★ 足りなければ **流動性の高い主力**で水増しする。
        #   これは kabu の購読レイテンシを測るだけなので、銘柄が今日の候補で
        #   ある必要はない。むしろ **今日 登録していない銘柄**のほうが
        #   コールドで、2バッチ目以降の実態に近い。
        #   ⛔ リストを写経しない。check_board_limits.py から読む
        #     (2箇所に置くと片方だけ直して片方が残る)。
        if len(_syms) < a.rotate:
            import re as _re
            try:
                _txt = open("check_board_limits.py", encoding="utf-8").read()
                _m = _re.search(r"_DEFAULT\s*=\s*\[(.*?)\]", _txt, _re.S)
                _pad = list(dict.fromkeys(
                    _re.findall(r"\b(\d{4})\b", _m.group(1)))) if _m else []
            except Exception:
                _pad = []
            _have = set(_syms)
            _add = [s for s in _pad if s not in _have]
            if _add:
                print(f"[info] {len(_syms)}件では {a.rotate}件に足りないので、"
                      f"check_board_limits の主力リストから {len(_add)}件 足します"
                      f"(**今日 登録していない = コールド**なので2バッチ目の"
                      f"実態に近い)")
                _syms = _syms + _add
            if len(_syms) < a.rotate:
                print(f"[warn] それでも {len(_syms)}件 しかありません。"
                      f"--symbols で明示してください")
        _syms = _syms[:max(1, a.rotate)]
    elif len(_syms) > 50:
        print(f"[warn] {len(_syms)}件 → kabu の登録上限で先頭50件にします")
        _syms = _syms[:50]

    print(f"[info] {len(_syms)}銘柄 / {a.seconds}秒 受信 / "
          f"{'本番(18080)' if a.prod else 'デモ(18081)'}")
    print("[info] ⛔ 照会のみ。1件も発注しません")
    print("[info] ⛔ .\\nexec / .\\norder / watcher と同時に走らせないこと"
          "(トークンは1つ)")

    cli = KabuClient(prod=a.prod, dry_run=True)
    cli.connect()

    # ══════════════════════════════════════════════════════════════════
    #  --rotate : 登録上限50件を **回して** 超えられるか (2026-09-07)
    # ══════════════════════════════════════════════════════════════════
    # ⛔ §18.44 は「バッチ回しは不可能」と結論したが、それは **REST の
    #   読み取りが2周目も36秒かかる**という測定。PUSH なら読む時間はゼロ。
    #   残るのは『登録してから配信が始まるまで』の待ちだけで、そこは未測定。
    # ★ 始値は動かないので、遅れて届いても **値は正しい**。
    #   問われるのは「何秒遅れるか」= 何bp 失うか、だけ。
    if a.rotate:
        _bs = max(1, a.batch)
        _batches = [_syms[i:i + _bs] for i in range(0, len(_syms), _bs)]
        print(f"\n[rotate] {len(_syms)}銘柄 を {_bs}件 × {len(_batches)}バッチ")
        print("[rotate] ⛔ 照会のみ。発注しません。建玉にも注文にも触れません")
        st = BoardStream(cli.base_url, verbose=True)
        if not st.start():
            sys.exit("[error] PUSH配信に繋がりませんでした")
        _res = []
        for _bi, _b in enumerate(_batches, 1):
            _want = {s.replace(".T", "") for s in _b}
            try:
                cli.unregister_all()
            except Exception:
                pass
            # ⚠ 前バッチの残りを数えないよう、受信済みの銘柄集合を控える
            _before = set(st.snapshot())
            _t0 = _time.time()
            _rr = cli.register_many(sorted(_want))
            _nok = len((_rr or {}).get("RegistList") or [])
            _t_reg = _time.time() - _t0
            _first = None
            _full = None
            while _time.time() - _t0 < a.batch_wait:
                _time.sleep(0.25)
                _snap = st.snapshot()
                _hit = {s for s in _want
                        if float((_snap.get(s) or {}).get("OpeningPrice") or 0) > 0
                        and s not in _before}
                if _hit and _first is None:
                    _first = _time.time() - _t0
                    print(f"  [batch{_bi}] 初回受信 {_first:.1f}s", flush=True)
                if len(_hit) >= _nok and _nok:
                    _full = _time.time() - _t0
                    break
            # ⛔ **登録に失敗したバッチは測定外**(2026-09-07)。
            #   register は部分受理されないので、1件でも無効なコード
            #   (上場廃止など)が混ざると PUT 全体が 400 で落ちる。
            #   そのとき届いた件数を数えると意味のない数字になる
            #   (実際 登録0件なのに『取得49』と表示して混乱した)。
            if _nok <= 0:
                _res.append((_bi, len(_b), 0, None, _t_reg, None, None))
                print(f"  [batch{_bi}] 要求{len(_b)} **登録に失敗** → 測定外",
                      flush=True)
                # ★★ 原因を切り分ける。意味が正反対の2つがありうる:
                #   (a) 無効なコードが混ざった → 実運用では起きない(回せる)
                #   (b) 総数が50を超えた/解除が効かない → **回すこと自体が不可能**
                #   半分ずつ試せば (a) なら犯人が特定でき、(b) なら
                #   「半分(25件)でも落ちる」という形で出る。
                print("  [診断] 半分ずつ試して原因を切り分けます", flush=True)

                def _try(_sub: list) -> bool:
                    try:
                        cli.unregister_all()
                    except Exception:
                        pass
                    _rr2 = cli.register_many(sorted(_sub))
                    return len((_rr2 or {}).get("RegistList") or []) > 0

                _cur = sorted(_want)
                _guilty: list = []
                for _depth in range(8):        # 50件なら6回で1件まで絞れる
                    if len(_cur) <= 1:
                        _guilty = _cur
                        break
                    _h = len(_cur) // 2
                    _L, _R = _cur[:_h], _cur[_h:]
                    _okL = _try(_L)
                    _okR = _try(_R)
                    print(f"    左{len(_L)}件={'OK' if _okL else 'NG'} / "
                          f"右{len(_R)}件={'OK' if _okR else 'NG'}", flush=True)
                    if _okL and _okR:
                        # ⛔ 半分ずつなら両方通る = **件数の問題**(b)
                        print(f"    ⛔ **半分ずつなら両方通ります**。"
                              f"つまり無効コードではなく "
                              f"**総登録数が50を超えている**のが原因です。"
                              f"unregister_all が効いていない可能性 → "
                              f"**回すのは不可能**", flush=True)
                        _guilty = []
                        break
                    _cur = _L if not _okL else _R
                else:
                    _guilty = _cur
                if _guilty:
                    print(f"    ✅ **犯人は {', '.join(_guilty)}**(無効なコード)。"
                          f"件数の問題ではないので、有効な銘柄だけなら"
                          f"**回せます**", flush=True)
                try:
                    cli.unregister_all()
                except Exception:
                    pass
                continue
            _got = len({s for s in _want
                        if float((st.snapshot().get(s) or {}).get(
                            "OpeningPrice") or 0) > 0 and s not in _before})
            _res.append((_bi, len(_b), _nok, _got, _t_reg, _first, _full))
            print(f"  [batch{_bi}] 要求{len(_b)} 登録{_nok} 取得{_got} / "
                  f"登録{_t_reg:.1f}s / 初回"
                  f"{'—' if _first is None else f'{_first:.1f}s'} / 全件"
                  f"{'届かず' if _full is None else f'{_full:.1f}s'}", flush=True)
        st.stop()
        try:
            cli.unregister_all()
        except Exception:
            pass

        print("\n" + "=" * 72)
        print("■ 判定: PUSH なら50件の壁を回して超えられるか")
        print("=" * 72)
        print(f"  {'batch':>6} {'要求':>5} {'登録':>5} {'取得':>5} "
              f"{'登録s':>7} {'初回s':>7} {'全件s':>7}")
        for _bi, _nb, _nok, _got, _tr, _f1, _fa in _res:
            if _got is None:
                print(f"  {_bi:>6} {_nb:>5} {0:>5} {'—':>5} {_tr:>7.1f} "
                      f"{'—':>7} {'—':>7}   ⛔ 登録失敗 = 測定外")
                continue
            print(f"  {_bi:>6} {_nb:>5} {_nok:>5} {_got:>5} {_tr:>7.1f} "
                  f"{'—' if _f1 is None else f'{_f1:>7.1f}'} "
                  f"{'—' if _fa is None else f'{_fa:>7.1f}'}")
        _bad = [r[0] for r in _res if r[3] is None]
        if _bad:
            print(f"\n  ⚠ batch{','.join(map(str, _bad))} は登録に失敗したので"
                  f"判定に含めていません")
        # ★ 2バッチ目以降の『全件揃うまで』が実質のコスト。
        #   §18.44 の1分足実測(1分 -15.8bp / 2分 -26.8 / 3分 -29.4 / 5分 -36.6)
        #   で bp に直す。N のグロスは +15.7bp/件。
        _later = [r[6] for r in _res[1:] if r[6] is not None]
        if not _later:
            print("\n  ⛔ **2バッチ目が届きませんでした**。回すのは不可能です"
                  f"(上限 {a.batch_wait}秒)。50件のままにしてください")
        else:
            _avg = sum(_later) / len(_later)

            def _bp(_s: float) -> float:
                # 実測点を線形につなぐ(0s=0 / 60s=-15.8 / 120s=-26.8 /
                # 180s=-29.4 / 300s=-36.6)。凹型なので短い側が効く。
                _pts = [(0, 0.0), (60, 15.8), (120, 26.8),
                        (180, 29.4), (300, 36.6)]
                for (x0, y0), (x1, y1) in zip(_pts, _pts[1:]):
                    if _s <= x1:
                        return y0 + (y1 - y0) * (_s - x0) / (x1 - x0)
                return 36.6
            _c = _bp(_avg)
            print(f"\n  2バッチ目以降の『全件揃うまで』 平均 **{_avg:.1f}秒**")
            print(f"  → §18.44 の減衰カーブで **約 -{_c:.1f}bp**"
                  f"(N のグロスは +15.7bp/件)")
            if _c < 8.0:
                print(f"  ✅ **回す価値があります**。いま1件も建てていない"
                      f"51件目以降が +{15.7 - _c:.1f}bp で取れる計算")
            elif _c < 15.7:
                print(f"  ⚠ 薄いが黒字圏(+{15.7 - _c:.1f}bp)。"
                      f"バッチ2までにして3以降は捨てるのが妥当")
            else:
                print(f"  ⛔ **グロスを食い切ります**。回しても意味がありません")
            print("\n  ⚠ これは1日の1回の測定。**09:00 の板寄せ直後は混む**ので、"
                  "採否は朝に測り直してから決めること")
        raise SystemExit(0)

    _r = cli.register_many([s.replace(".T", "") for s in _syms])
    _ok = len((_r or {}).get("RegistList") or [])
    print(f"[info] 登録 {_ok}/{len(_syms)}件")

    st = BoardStream(cli.base_url)
    if not st.start():
        sys.exit("[error] PUSH配信に繋がりませんでした。"
                 "kabuステーションの API 設定で PUSH配信が有効か確認してください")

    _t0 = _time.time()
    _last = 0
    while _time.time() - _t0 < a.seconds:
        _time.sleep(2.0)
        if st.n_msg != _last:
            print(f"  [{_time.time() - _t0:5.1f}s] {st.describe()}", flush=True)
            _last = st.n_msg
    st.stop()

    print(f"\n[結果] {st.describe()}")
    _snap = st.snapshot()
    _op = {k: v for k, v in _snap.items()
           if float(v.get("OpeningPrice") or 0) > 0}
    print(f"  始値が届いた銘柄: **{len(_op)}/{len(_syms)}**")
    if _op:
        _k = sorted(_op)[0]
        print(f"  例: {_k} 始値 {_op[_k].get('OpeningPrice')} "
              f"/ OpeningPriceTime {_op[_k].get('OpeningPriceTime')}")
    # ★★ これが本命の確認。PUSH のメッセージに始値が入っていなければ
    #    速度がいくら速くても N には使えない。
    if not _op and st.n_msg:
        print("  ⛔ **受信はあるが OpeningPrice が入っていません**。"
              "PUSH のメッセージ形式を確認してください(下に生データを出します)")
        _any = next(iter(_snap.values()), {})
        print("     " + ", ".join(sorted(_any)[:25]))
    elif not st.n_msg:
        print("  ⚠ 1件も受信していません。**場中でないと配信は来ません**"
              "(場外での0件は異常ではない)")

    if a.compare_rest:
        print("\n[比較] 同じ銘柄を REST で1周読みます")
        _t1 = _time.time()
        _n = 0
        for _s in _syms:
            try:
                if cli.get_board(_s.replace(".T", "")):
                    _n += 1
            except Exception:
                pass
        _el = _time.time() - _t1
        print(f"  REST: {_n}/{len(_syms)}銘柄 を {_el:.1f}秒 "
              f"({_n / max(0.1, _el):.1f}件/秒)")
        print(f"  → 最後の銘柄は {_el:.0f}秒待たされる。平均 {_el / 2:.0f}秒。"
              f"PUSH ならこれが 0 になる")
    cli.unregister_all()
