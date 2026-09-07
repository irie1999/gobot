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
    ap.add_argument("--seconds", type=int, default=60, help="受信する秒数")
    ap.add_argument("--compare-rest", action="store_true",
                    help="同じ銘柄を REST でも1周読み、所要秒数を並べる")
    a = ap.parse_args()

    # ⛔ 発注系は一切 import しない。KabuClient は登録と(比較時のみ)/board だけ。
    from kabu_api import KabuClient

    _syms: list[str] = []
    if a.symbols:
        _syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    else:
        _p = f"n_signals_{_dt.datetime.now(JST):%Y%m%d}.csv"
        if not os.path.exists(_p):
            sys.exit(f"[error] {_p} がありません。--symbols で明示してください")
        import csv as _csv
        _all: list[str] = []
        with open(_p, encoding="utf-8-sig") as _f:
            for _r in _csv.DictReader(_f):
                _c = str(_r.get("symbol") or _r.get("code") or "").strip()
                if not _c:
                    continue
                _c = _c.replace(".T", "")
                _all.append(_c)
                # ★ 朝に実際に watch した50件を優先する(候補は126件ありうる)。
                #   watched_n が無い古い CSV なら _all のほうを使う。
                if str(_r.get("watched_n") or _r.get("watched") or "").strip() \
                        not in ("", "0", "False", "false"):
                    _syms.append(_c)
        if not _syms:
            print(f"[info] watched_n が無いので先頭50件を使います")
            _syms = _all
        else:
            print(f"[info] {_p} の watched_n から {len(_syms)}件")
        _syms = _syms[:50]
    if not _syms:
        sys.exit("[error] 銘柄が0件です")
    if len(_syms) > 50:
        print(f"[warn] {len(_syms)}件 → kabu の登録上限で先頭50件にします")
        _syms = _syms[:50]

    print(f"[info] {len(_syms)}銘柄 / {a.seconds}秒 受信 / "
          f"{'本番(18080)' if a.prod else 'デモ(18081)'}")
    print("[info] ⛔ 照会のみ。1件も発注しません")
    print("[info] ⛔ .\\nexec / .\\norder / watcher と同時に走らせないこと"
          "(トークンは1つ)")

    cli = KabuClient(prod=a.prod, dry_run=True)
    cli.connect()
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
