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

# ⛔ Windows で `> out.txt` にリダイレクトすると stdout が cp932 になり、
#   ⛔ ⚠ ✅ のような cp932 に無い記号で UnicodeEncodeError を出して
#   **スクリプトごと落ちる**(2026-09-08 に kabu_ws で実際に発生)。
#   import しただけで効く。出力の中身は変わらない。
import console_safe  # noqa: F401

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
        # ★★ 再接続の回数 (2026-09-07)。ローテーション中に接続が落ちると
        #   その間のメッセージを取りこぼす。**落ちていること自体が設計上の
        #   問題**なので必ず数える(実測で毎バッチ落ちていた)。
        self.n_reconnect = 0
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
            _again = self.t_connect > 0
            self.connected = True
            self.t_connect = _time.time()
            if _again:
                self.n_reconnect += 1
            if self.verbose:
                print(f"  [ws] {'**再接続**' if _again else '接続しました'}"
                      f" {self.url}"
                      + (f" (通算 {self.n_reconnect}回目)" if _again else ""),
                      flush=True)

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
    # ⛔ 平日 08:30〜15:40 は既定で中止する。接続時にトークンを取り直すため、
    #   走っている .\nexec のトークンを無効にして **決済(MOC)の設置を
    #   壊しうる**。これを外すなら nexec が MOC まで終えたことを目視で
    #   確認してから(自己責任)。
    ap.add_argument("--force-now", action="store_true",
                    help="発注の時間帯(平日 08:30-15:40)でも実行する。"
                         "nexec のトークンを奪うので自己責任")
    ap.add_argument("--batch-wait", type=int, default=90,
                    help="--rotate で1バッチを待つ上限秒")
    # ★★ 仮説の検証つまみ (2026-09-07)。
    #   実測で **再接続が起きたバッチだけ裾が壊れた**(90%点 5.5s → 60.2s /
    #   24.3s、未達4件は全部『約定済みなのに届かなかった』)。
    #   unregister_all で登録リストを空にしているのが原因では、という仮説。
    #   配信する銘柄がゼロになった時点で kabu がストリームを閉じている。
    #   → 入れ替えのあいだ **N件だけ登録を残す**と再接続が消えるかを見る。
    #   ⛔ 0 にすると従来どおり全解除(仮説の対照群)。
    ap.add_argument("--keep-alive", type=int, default=0,
                    help="バッチ入替のあいだ登録を残す件数(既定0=全解除)。"
                         "1以上にすると登録リストが空にならない")
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

    # ══════════════════════════════════════════════════════════════════
    #  ⛔⛔ 発注の時間帯に走らせない (2026-09-08)
    # ══════════════════════════════════════════════════════════════════
    # ここは「照会のみ」だが **無害ではない**。cli.connect() は
    # POST /token で **新しいトークンを取り直す**ので、そのとき
    # `.\nexec` が持っているトークンは **その場で無効になる**。
    # 以降 nexec の REST は 401 で落ちる。N は watcher を使わず
    # **引け成行(MOC)を置くところまで nexec 自身がやる**ので、
    # ここで割り込むと **決済が置かれないまま終わりうる**。
    # §18.46 の事故(8建玉が終日ノーガード → 持ち越し → 強制決済 -36,800円)は
    # まさに「決済を置く経路が途中で死んだ」形だった。
    #
    # 2026-09-08 に実際に 09:08 で走らせている(判定遅延 中央503s が証拠)。
    # docstring に「引け後にすること」と書いてあっても止まらなかったので、
    # **文章ではなくコードで止める**。
    _now = _dt.datetime.now(JST)
    _NG_FROM, _NG_TO = (8, 30), (15, 40)     # 08:30〜15:40 は発注・監視の時間帯
    _hm = (_now.hour, _now.minute)
    if _NG_FROM <= _hm < _NG_TO and _now.weekday() < 5 and not a.force_now:
        print(f"""
⛔ いま {_now:%H:%M} は **発注の時間帯**(08:30〜15:40)です。中止しました。

   このスクリプトは照会だけですが、接続時に **トークンを取り直す**ので、
   走っている `.\\nexec` のトークンが **その場で無効**になります。
   N は watcher を使わず、引け成行(MOC)を置くところまで nexec 自身が
   やるので、割り込むと **決済が置かれないまま終わる**ことがあります。

   → **15:40 以降**(引け後)に実行してください。
   → 平日の日中にどうしても測るなら --force-now(自己責任)。
      その場合は `.\\nexec` が **MOC の設置まで終わったこと**を
      画面で確認してからにすること。
""", flush=True)
        # ⛔ ここは `if __name__ == "__main__":` の直下(モジュール直書き)で
        #   関数の中ではない。`return` と書くと SyntaxError で **起動すら
        #   しなくなる**(2026-09-08 に実際に踏んだ)。しかも ast.parse は
        #   通してしまう(`return` の位置はコンパイル時に見るため)ので、
        #   構文チェックだけでは気づけない。必ず compile() で確認すること。
        sys.exit(0)

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
        _ka0 = max(0, a.keep_alive)
        # ⛔⛔ **keep-alive のぶんを引いてからバッチを切る**(2026-09-07 修正)。
        #   以前は 50件ずつ切ってから _want を45件にスライスしていたので、
        #   **残り5件が黙って捨てられ**、150件のつもりで140件しか
        #   測っていなかった。銘柄が消えるのに何も表示されない最悪の形。
        _q = list(_syms)
        _batches = []
        while _q:
            _cap = max(1, _bs - (_ka0 if _batches else 0))
            _batches.append(_q[:_cap])
            _q = _q[_cap:]
        _cov = sum(len(b) for b in _batches)
        print(f"\n[rotate] {len(_syms)}銘柄 を {len(_batches)}バッチ"
              f"({' + '.join(str(len(b)) for b in _batches)})"
              + (f" / keep-alive {_ka0}件ぶん2バッチ目以降は{_bs - _ka0}件"
                 if _ka0 else ""))
        # ★ 保存則(条件10)の入口。ここで欠けていたら測定にならない。
        if _cov != len(_syms):
            sys.exit(f"[error] バッチの合計 {_cov} != 対象 {len(_syms)}。"
                     f"銘柄が落ちています")
        print("[rotate] ⛔ 照会のみ。発注しません。建玉にも注文にも触れません")
        st = BoardStream(cli.base_url, verbose=True)
        if not st.start():
            sys.exit("[error] PUSH配信に繋がりませんでした")
        _res = []
        _prev: set = set()          # 直前のバッチで登録済みの銘柄
        for _bi, _b in enumerate(_batches, 1):
            _want = {s.replace(".T", "") for s in _b}
            # ★ 入れ替え。--keep-alive N なら **登録を空にしない**。
            #   kabu は登録ゼロでストリームを閉じている疑いがあるため。
            _ka = max(0, a.keep_alive)
            if _ka > 0 and _prev:
                _drop = sorted(_prev)[:max(0, len(_prev) - _ka)]
                # ⛔ **1件ずつ解除しない**(2026-09-07 の失敗)。45回の HTTP で
                #   429 に当たり、失敗を握り潰したまま追加登録して 400 になった。
                #   一括で解除し、**成否を必ず見る**。
                if not cli.unregister_many(_drop):
                    print(f"  ⛔ batch{_bi}: {len(_drop)}件の解除に失敗しました。"
                          f"枠が空かないので全解除に切り替えます", flush=True)
                    try:
                        cli.unregister_all()
                    except Exception:
                        pass
                    _prev = set()
                # ⛔ ここで _want を削らないこと。バッチを切る時点で
                #   keep-alive のぶんは引いてある(削ると銘柄が消える)。
            else:
                try:
                    cli.unregister_all()
                except Exception:
                    pass
            # ⚠ 前バッチの残りを数えないよう、受信済みの銘柄集合を控える
            _before = set(st.snapshot())
            _t0 = _time.time()
            _rr = cli.register_many(sorted(_want))
            # ⛔⛔ RegistList は **今回追加した数ではなく、現在の登録全部**を
            #   返す(2026-09-07 実測: keep-alive で45件だけ追加したのに50)。
            #   これを待機の終了条件にすると **来ない5件を待って90秒**
            #   ハングし、その間に WebSocket がアイドルで落ちて
            #   「ローテーションすると再接続する」と誤診した。
            #   → 登録の成否だけ RegistList で見て、**待つ数は _want の数**。
            _rl = (_rr or {}).get("RegistList") or []
            _reg_n = len(_rl)
            # ★ **どの銘柄が登録されたか**を集合で持つ(件数だけだと
            #   「無効コードで登録できなかった」を分類できない)。
            _reg_ok = {str((_x or {}).get("Symbol") or "") for _x in _rl}
            _nok = min(_reg_n, len(_want)) if _reg_n else 0
            _t_reg = _time.time() - _t0
            if _nok > 0:
                # 残した銘柄 + 今回登録した銘柄 = いま登録されているもの
                _prev = (set(sorted(_prev)[len(_prev) - _ka:]) if _ka > 0
                         else set()) | set(_want)
            # ★★ 「全件揃うか」で判定しない (2026-09-07 修正)。
            #   実測で 47/50・45/50 が1秒以内に届いたのに、残り3〜5件が
            #   来ないだけで『⛔ 回すのは不可能』と出していた。
            #   ⚠ そもそも **一度も約定していない銘柄には始値が無い**ので、
            #     100%は原理的に揃わないことがある。
            #   → 銘柄ごとの到着時刻を記録し、**分位**で見る。
            _arr: dict = {}                 # symbol -> 登録からの到着秒
            # ★★ **本命の指標**(2026-09-07 指摘で追加)。
            #   減衰カーブ(§18.44)は「**寄りからの経過**」で定義されている。
            #   「登録からの経過」で測ると、09:00:00 に寄った銘柄を
            #   09:00:05 に登録した場合の5秒が丸ごと抜ける。
            #       判定遅延 = 判定できた時刻 − OpeningPriceTime
            _lag: dict = {}                 # symbol -> 判定遅延(秒)
            _first = None
            _rc0 = st.n_reconnect

            def _open_epoch(_bd: dict):
                _ot = str((_bd or {}).get("OpeningPriceTime") or "")
                if not _ot:
                    return None
                try:
                    return _dt.datetime.fromisoformat(_ot).timestamp()
                except Exception:
                    return None
            while _time.time() - _t0 < a.batch_wait:
                _time.sleep(0.25)
                _snap = st.snapshot()
                _el = _time.time() - _t0
                for _s in _want:
                    if _s in _arr or _s in _before:
                        continue
                    if float((_snap.get(_s) or {}).get("OpeningPrice") or 0) > 0:
                        _arr[_s] = _el
                        _oe = _open_epoch(_snap.get(_s))
                        if _oe:
                            _lag[_s] = _time.time() - _oe
                if _arr and _first is None:
                    _first = min(_arr.values())
                    print(f"  [batch{_bi}] 初回受信 {_first:.1f}s", flush=True)
                # ⛔ **件数ではなく集合**で判定する(2026-09-07)。
                #   件数だと keep-alive 銘柄など『対象外の受信』を数え違える
                #   余地が残る。RegistList は登録の成否確認にだけ使う。
                if _want <= set(_arr):
                    break
            _sec = sorted(_arr.values())

            def _q(_v: list, _p: float) -> float:
                if not _v:
                    return float("nan")
                _sv = sorted(_v)
                return _sv[min(len(_sv) - 1, int(len(_sv) * _p))]

            def _pct(_p: float) -> float:
                return _q(_sec, _p)
            _full = _pct(0.90)              # ★ 判定は **90%点**で行う
            # ★ 判定遅延(寄りからの経過)。減衰カーブと同じ土俵の量。
            _lagv = list(_lag.values())
            _lag50, _lag90 = _q(_lagv, 0.5), _q(_lagv, 0.9)
            # ⛔ **登録に失敗したバッチは測定外**(2026-09-07)。
            #   register は部分受理されないので、1件でも無効なコード
            #   (上場廃止など)が混ざると PUT 全体が 400 で落ちる。
            #   そのとき届いた件数を数えると意味のない数字になる
            #   (実際 登録0件なのに『取得49』と表示して混乱した)。
            if _nok <= 0:
                _res.append((_bi, len(_b), 0, None, _t_reg, None, None,
                             None, 0, 0, 0, 0))
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
            # ⛔ **REST 確認より前に**待ち時間を確定させる(2026-09-07)。
            #   後で測ると診断の HTTP 往復が混ざり、--batch-wait 20 なのに
            #   「待ちs 35.3」と出て意味が分からなくなる。
            _waited = _time.time() - _t0
            _got = len(_arr)
            # ★★ 届かなかった銘柄を **REST で確かめる**。
            #   「PUSH が落とした」のか「そもそも今日まだ約定していない」のかで
            #   意味が正反対。前者は実装の問題、後者は現実(始値が存在しない)。
            #   ★★ **5分類**にする(2026-09-07)。2分類だと『遅寄り』を
            #     『接続不良』と誤診する。朝は特にここが混ざる。
            #       A PUSH自体が未受信       … 板が1度も飛んでこない
            #       B PUSH受信済みだが当日始値なし … 遅寄り(まだ寄っていない)
            #       C RESTには当日始値がある … **PUSH がまだ配信していない**
            #       D 登録できていない       … 無効コード/登録失敗
            #       E 時間切れ               … 上限に達して打ち切った
            #
            # ⛔⛔ C を『取りこぼし(バグ)』と読まないこと(2026-09-07)。
            #   **PUSH は値が動いたときしか飛んでこない。** 流動性の低い
            #   銘柄は購読しても数分ティックしないので何も来ない。
            #   実測でバッチが下(=流動性が低い)ほど C が増えた:
            #     batch1(1〜50位)  取得50/50 未達0
            #     batch2(51〜95位) 取得35/45 未達10 全部C
            #     batch3(96〜140位)取得28/45 未達17
            #   ★ そして **09:00 は正反対**。板寄せで全銘柄が必ず約定するので
            #     PUSH は即座に飛ぶ。**場中のこの数字は悲観側の下限**であり、
            #     朝の実測に置き換えるまで採否の根拠にしないこと。
            _miss = [s for s in _want if s not in _arr and s not in _before]
            _snapf = st.snapshot()
            _cls = {"A_push無受信": [], "B_遅寄り": [], "C_push未配信": [],
                    "D_未登録": [], "E_時間切れ": []}
            _timeout = _waited >= a.batch_wait * 0.95
            for _s in _miss[:12]:            # ⚠ 429 を避けるため上限12件
                if _s not in _reg_ok:
                    _cls["D_未登録"].append(_s); continue
                _bw = _snapf.get(_s)
                try:
                    _bb = cli.get_board(_s)
                except Exception:
                    _bb = {}
                _rest_open = float((_bb or {}).get("OpeningPrice") or 0) > 0
                if _rest_open:
                    # REST には当日の始値がある = PUSH が落とした
                    _cls["C_push未配信"].append(_s)
                elif _bw:
                    _cls["B_遅寄り"].append(_s)   # 板は来たが始値がまだ
                elif _timeout:
                    _cls["E_時間切れ"].append(_s)
                else:
                    _cls["A_push無受信"].append(_s)
            for _s in _miss[12:]:
                _cls["E_時間切れ"].append(_s)     # 未検査ぶんはここに寄せる
            _miss_traded = _cls["C_push未配信"]
            _miss_nottraded = _cls["B_遅寄り"] + _cls["A_push無受信"]
            # ★ 保存則(条件10): 対象 = 取得 + 各分類。説明不能があれば止める
            _acct = len(_arr) + sum(len(v) for v in _cls.values())
            if _acct != len(_want):
                print(f"  ⛔⛔ batch{_bi}: **説明不能 {len(_want) - _acct}件**"
                      f"(対象{len(_want)} = 取得{len(_arr)} + 分類"
                      f"{sum(len(v) for v in _cls.values())})。"
                      f"分類が漏れています", flush=True)
            _cls_s = " / ".join(f"{k}{len(v)}" for k, v in _cls.items()
                                if v)
            _res.append((_bi, len(_want), _nok, _got, _waited, _first, _full,
                         _pct(0.5), len(_miss), len(_miss_traded),
                         len(_miss_nottraded), st.n_reconnect - _rc0))
            print(f"  [batch{_bi}] 対象{len(_want)}(登録済み計{_reg_n}) "
                  f"**取得{_got}** / "
                  f"登録{_t_reg:.1f}s / 初回"
                  f"{'—' if _first is None else f'{_first:.1f}s'}"
                  f" / 中央{_pct(0.5):.1f}s / 90%点{_full:.1f}s"
                  + (f" / **判定遅延(寄りから) 中央{_lag50:.0f}s "
                     f"90%点{_lag90:.0f}s**" if _lagv else "")
                  + (f" / ⛔ 未達{len(_miss)}件[{_cls_s}]" if _miss else "")
                  + (f" / ⚠ 再接続{st.n_reconnect - _rc0}回"
                     if st.n_reconnect > _rc0 else ""), flush=True)
        st.stop()
        try:
            cli.unregister_all()
        except Exception:
            pass

        print("\n" + "=" * 72)
        print("■ 判定: PUSH なら50件の壁を回して超えられるか")
        print("=" * 72)
        print(f"  {'batch':>6} {'対象':>5} {'取得':>5} "
              f"{'初回s':>7} {'中央s':>7} {'90%s':>7} {'待ちs':>7} "
              f"{'未達':>5} {'再接続':>6}")
        for (_bi, _nb, _nok, _got, _tr, _f1, _fa, _md, _nm, _mt, _mn,
             _rc) in _res:
            if _got is None:
                print(f"  {_bi:>6} {_nb:>5} {'—':>5} "
                      f"{'—':>7} {'—':>7} {'—':>7} {'—':>7} {'—':>5} {'—':>6}"
                      f"   ⛔ 登録失敗 = 測定外")
                continue
            _hang = _tr >= a.batch_wait * 0.95
            print(f"  {_bi:>6} {_nb:>5} {_got:>5} "
                  f"{'—' if _f1 is None else f'{_f1:>7.1f}'} "
                  f"{_md:>7.1f} {_fa:>7.1f} {_tr:>7.1f} {_nm:>5} {_rc:>6}"
                  + ("   ⛔ 上限まで待った" if _hang else ""))
        # ★ 「上限まで待った」バッチは、**待ち時間そのものが原因で**
        #   アイドル切断・裾の悪化を起こしうる。ローテーションのコストと
        #   混同しないこと(2026-09-07 に実際に誤診した)。
        if any(r[4] >= a.batch_wait * 0.95 for r in _res if r[3] is not None):
            print(f"\n  ⛔ 上限({a.batch_wait}秒)まで待ったバッチがあります。"
                  f"その裾と再接続は **待たされたこと自体**が原因かもしれません。"
                  f"--batch-wait を短くして測り直してください")
        # ★ 未達の内訳。REST でも始値が無い = そもそも今日まだ約定していない
        #   銘柄なので、PUSH の失敗ではない。
        _mt_all = sum(r[9] for r in _res if r[3] is not None)
        _mn_all = sum(r[10] for r in _res if r[3] is not None)
        if _mt_all or _mn_all:
            print(f"\n  未達 {_mt_all + _mn_all}件のうち "
                  f"**C_push未配信(RESTには当日始値がある) {_mt_all}件**")
            # ★★ C は **バグではない**。PUSH は値が動いたときしか飛ばない。
            #   バッチが下(=流動性が低い)ほど増えるのがその証拠。
            _rate = [(r[0], r[1], r[3], r[1] - r[3]) for r in _res
                     if r[3] is not None]
            if len(_rate) >= 2:
                print("    バッチ別の取得率(流動性の高い順にバッチが並ぶ):")
                for _bi2, _nb2, _g2, _m2 in _rate:
                    print(f"      batch{_bi2}: {_g2}/{_nb2} "
                          f"({_g2 / max(1, _nb2) * 100:.0f}%) 未達{_m2}")
                _first_r = _rate[0][2] / max(1, _rate[0][1])
                _last_r = _rate[-1][2] / max(1, _rate[-1][1])
                if _first_r > _last_r + 0.1:
                    print("    ★ **下のバッチほど取得率が低い = 流動性の効果**。"
                          "PUSH は値が動いたときしか飛ばないので、"
                          "ティックの少ない銘柄は購読しても来ません。"
                          "**バグではありません。**")
            # ⛔⛔ **「09:00 なら取得率が上がる」とは言えない**(2026-09-07 撤回)。
            #   ① 全銘柄が09:00に寄るわけではない(実測 7384=09:00:50 /
            #      5801=09:06)
            #   ② batch2 を 09:00:05 に登録した時点で、09:00:00 の板寄せ
            #      イベントは **購読前に終わっている**
            #   ③ PUSH が本当に差分配信だけなら、その後値が動かない銘柄は
            #      朝でも届かない
            #   そして今日のデータは **差分配信説と整合する**(流動性の高い
            #   batch1 だけ100%。購読時に現在値を送るなら流動性と相関しない)。
            print("    ⛔ **『09:00 なら届く』とは言えません。**"
                  " PUSH が差分配信なら、09:00:05 に登録した時点で"
                  "板寄せイベントは購読前に終わっています。"
                  "朝も同じかそれ以上に届かない可能性があります")
            print("    → だから live は **PUSH を1秒だけ待ち、来ない銘柄は"
                  "REST で読む**。PUSH の被覆率に賭けない設計にすること")
        _rc_all = sum(r[11] for r in _res if r[3] is not None)
        if _rc_all:
            print(f"\n  ⚠ **WebSocket が {_rc_all}回 再接続しています**。"
                  f"ローテーションのたびに落ちているなら、その間の"
                  f"メッセージを取りこぼします。実装の要修正点です")
        _bad = [r[0] for r in _res if r[3] is None]
        if _bad:
            print(f"\n  ⚠ batch{','.join(map(str, _bad))} は登録に失敗したので"
                  f"判定に含めていません")
        # ★ 2バッチ目以降の『全件揃うまで』が実質のコスト。
        #   §18.44 の1分足実測(1分 -15.8bp / 2分 -26.8 / 3分 -29.4 / 5分 -36.6)
        #   で bp に直す。N のグロスは +15.7bp/件。
        # ★ 判定は **90%点**で行う(100%は原理的に揃わないことがある)
        _later = [r[6] for r in _res[1:]
                  if r[6] is not None and r[6] == r[6]]
        if not _later:
            print("\n  ⛔ **2バッチ目が1件も届きませんでした**。回すのは不可能です"
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
            print(f"\n  2バッチ目以降の『**90%が届くまで**』 平均 **{_avg:.1f}秒**")
            # ★★ **実運用は PUSH + REST のハイブリッド**(2026-09-07)。
            #   PUSH は10秒で90%届くが、残りは数十秒かかる(実測: 90秒待つと
            #   未達2件 / 20秒で切ると13件)。live は待たずに、届いていない
            #   銘柄だけ REST で読めばよい(k_open_confirm --ws が既にその形)。
            #   → 現実的なコストは 90%点 + 未達ぶんの REST 往復。
            _nmiss = [r[8] for r in _res[1:] if r[3] is not None]
            if _nmiss:
                _mavg = sum(_nmiss) / len(_nmiss)
                _hyb = _avg + _mavg * 0.73        # 0.73秒/銘柄(§18.44 実測)
                _ch = _bp(_hyb)
                print(f"  ハイブリッド(PUSH で待たず、未達 平均{_mavg:.1f}件だけ"
                      f" REST) → **{_hyb:.1f}秒 / 約 -{_ch:.1f}bp**"
                      f"(残り +{15.7 - _ch:.1f}bp)")
                print(f"    ⚠ 未達を待つより REST で取るほうが速い。"
                      f"**PUSH の裾を待たないこと**")
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
