"""check_board_limits.py — kabu ステーションの取得制限を **実機で測る** (照会のみ)

⛔ 発注しない。register / board / unregister しか叩かない。

何のためか (K = 09:00確認方式):
  K は 09:00 の始値を見て判定し、その場で発注する。§18.38 の実測では
  **5分遅れると K の優位はほぼ消える**(月+85万 → その1/4)。つまり
  「候補N銘柄の始値を何秒で取れるか」が K の成立条件そのもの。

  公式ドキュメント(https://kabucom.github.io/kabusapi/ptal/index.html)は
  レート制限を明記しているはずだが、この作業環境からは到達できない。
  **ドキュメントを読むより実機の秒数のほうが判断材料として上**なので測る。

測るもの:
  1. 一括登録(register_many) の所要時間
  2. /board を **コールド(登録直後) / ウォーム(2回目以降)** で全件取る時間
  3. 登録上限(コードには50件と記録)に実際に当たるか
  4. レート制限に当たるか / 並列数はいくつが最適か

★★ 2026-08-17 の実測でいちばん重要だった発見:

  **登録直後の初回 /board は桁違いに遅い**(kabu がその銘柄の板を購読しに行く)。
      初回(コールド) 41銘柄 並列2 → **48.08s**(timeout も出る)
      2回目(ウォーム) 同条件     → **6.27s**(失敗0)

  ⛔ **09:00 に登録して即読むと 40〜50秒かかって間に合わない。**
     → K の実装要件: **08:5x に候補を登録して1回空読みしておくこと。**

★ スループットは **約6.5件/秒で固定**。並列2→16 で秒数がほぼ変わらず
  (6.1〜9.1s)、429 だけが 8→40回 に増える。**並列を上げる意味がない**。
  推奨は並列2。

★★ 本番の測り方 — **2段階**。これが実運用の手順そのもの。

  08:5x)  python check_board_limits.py --prod --warmup --n 41
          → 登録して1回空読みし、**登録を残して**終了(この空読みが140秒級)

  09:00)  python check_board_limits.py --prod --open --n 41
          → 登録済み前提で **本番の1回だけ**を測る。開始/終了の実時刻・
            所要秒・始値の有無・**09:00 に寄っていない銘柄**を出し、
            board_speed_log.csv に追記する(朝ごとに貯まる)

  ⛔ この間 watcher / 発注サーバを起動しないこと(kabu の有効トークンは1つ)。
  ⛔ --warmup を飛ばして --open だけ流すと、登録直後の初回になるので
     140秒級の数字が出る。**手順を守らないと測定にならない**。

その他:
  python check_board_limits.py --prod --cap-probe 40,50,60,80,120   # 登録上限
  python check_board_limits.py --prod --symbols-file lss_trades_K.csv --n 50
  python check_board_limits.py --prod --n 41                        # 全部入り(場外用)

★★ 2026-08-16 の実測で確定したこと:
  ・**登録上限は 50件**。51件以上は **400 でリクエストごと失敗**する
    (部分受理されない)。候補が多い日は前夜に流動性上位50件へ絞ること。
  ・kabu は **OpeningPriceTime を返す** → 「まだ寄っていない」を確実に判別できる
    (実測: 9984 は 09:06 に寄っていた)。09:00 に寄らない銘柄は建てない
    (LSS_SKIP_LATE_OPEN=1)。

⚠ kabu の有効トークンは1つ。**watcher / 発注サーバを止めてから**実行すること
  (401 の取り合いになる)。
⚠ 寄り前〜ザラ場中に実行すると OpeningPrice の有無も確認できる。
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from kabu_api import KabuClient

ap = argparse.ArgumentParser(description="kabu の取得制限を実機で測る(照会のみ)")
ap.add_argument("--prod", action="store_true", help="本番(18080)。既定はデモ(18081)")
ap.add_argument("--symbols", type=str, default="",
                help="カンマ区切りの銘柄コード。省略時は --n 件を既定リストから")
ap.add_argument("--n", type=int, default=41,
                help="測る銘柄数(既定41 = 選定なしの1日の候補数)")
ap.add_argument("--workers", type=int, default=2,
                help="並列数。⛔ 上げても速くならない(kabu側でスループットが"
                     "固定)。実測では 2〜16 で秒数が変わらず429だけ増えた")
ap.add_argument("--serial", action="store_true",
                help="直列でも測る(41件で2分以上かかるので既定OFF)")
ap.add_argument("--sweep", type=str, default="2,4,6,8,12,16",
                help="並列数スイープ。429が出ない最大の並列数を探す。"
                     "空文字でスキップ")
ap.add_argument("--no-unregister", action="store_true",
                help="終了時に登録解除しない(次回の登録済み状態を残す)")
ap.add_argument("--symbols-file", type=str, default="",
                help="実際の候補リストを使う。lss_trades_K.csv / orders_*.csv の "
                     "symbol 列、または SELECTED を持つ提案 .py を読む")
ap.add_argument("--warmup", action="store_true",
                help="【08:5x に実行】登録して1回だけ空読みし、**登録を残して**終了。"
                     "これをやっておかないと 09:00 の初回が140秒級になる")
ap.add_argument("--open", dest="at_open", action="store_true",
                help="【09:00 に実行】登録済み前提で **本番の1回だけ**を測る。"
                     "コールド読み・上限プローブ・並列スイープは全部やらない")
ap.add_argument("--log", type=str, default="board_speed_log.csv",
                help="--open の結果を追記するCSV(朝ごとに貯まる)")
ap.add_argument("--ws", action="store_true",
                help="PUSH配信(WebSocket)に繋がるかだけ確かめる。照会のみ・"
                     "場外でも実行できる。REST が遅かった場合の逃げ道の下調べ")
ap.add_argument("--ws-seconds", type=int, default=20,
                help="--ws で待つ秒数(場外は配信が来ないので接続可否だけ見る)")
ap.add_argument("--cap-probe", type=str, default="",
                help="登録上限を実測する。カンマ区切りの件数 (例 40,50,60,80,120)。"
                     "毎回 全解除 → その件数を一括登録 → 受理数を数える")
args = ap.parse_args()

# 既定リスト: 流動性の高い主力。上限テスト用に多めに持つ。
_DEFAULT = [
    7203, 6758, 9984, 8306, 6501, 6902, 7267, 6367, 4063, 8058,
    9432, 8035, 6098, 4568, 7741, 6273, 6146, 8001, 8031, 9433,
    4661, 6301, 7011, 6857, 9983, 8316, 8411, 7751, 6954, 4502,
    6981, 7735, 4519, 6503, 6702, 8801, 8802, 3382, 2914, 4901,
    5108, 7269, 6752, 4543, 4578, 6178, 8766, 8750, 9022, 9020,
    4452, 6981, 7013, 5401, 5406, 3407, 4021, 4188, 2802, 2502,
    # ── 以下は **登録上限プローブ用**の追加分(50件を超えさせるため) ──
    2801, 2871, 3086, 3099, 3105, 3401, 3402, 3405, 3436, 3861,
    3863, 4004, 4005, 4042, 4043, 4061, 4183, 4208, 4324, 4507,
    4523, 4528, 4536, 4587, 4612, 4631, 4689, 4704, 4751, 4755,
    4911, 5019, 5020, 5101, 5201, 5202, 5214, 5232, 5233, 5301,
    5332, 5333, 5411, 5486, 5541, 5631, 5632, 5703, 5706, 5711,
    5713, 5714, 5801, 5802, 5803, 5901, 6103, 6113, 6136, 6141,
]
# 重複を除いて順序は保つ(--n で先頭から取るため)
_DEFAULT = list(dict.fromkeys(_DEFAULT))

def _load_symbols_file(path: str) -> list:
    """候補リストを読む。CSV(symbol列) でも 提案.py(SELECTED) でも可。"""
    import re as _re
    _txt = open(path, encoding="utf-8-sig").read()
    _out: list = []
    if path.lower().endswith(".csv"):
        import csv as _c
        import io as _io
        for _r in _c.DictReader(_io.StringIO(_txt)):
            _v = str(_r.get("symbol") or _r.get("Symbol") or "").strip()
            if _v:
                _out.append(_v)
    else:
        # 提案ファイル: ('7203.T', '銘柄名', 'MACDTF'), の1要素目
        for _m in _re.finditer(r"[(\[]\s*['\"]([0-9A-Za-z]{4}(?:\.T)?)['\"]", _txt):
            _out.append(_m.group(1))
    # 4桁コードに正規化して重複を除く(順序は保つ)
    _seen, _fin = set(), []
    for _v in _out:
        _c4 = _v.upper().removesuffix(".T").split(".")[0]
        if _c4 and _c4 not in _seen:
            _seen.add(_c4)
            _fin.append(_c4)
    return _fin


if args.symbols_file.strip():
    _syms = _load_symbols_file(args.symbols_file.strip())
    if not _syms:
        raise SystemExit(f"[error] {args.symbols_file} から銘柄を読めません")
    if args.n > 0:
        _syms = _syms[:args.n]
    print(f"[候補リスト] {args.symbols_file} から {len(_syms)}銘柄")
elif args.symbols.strip():
    _syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
else:
    _syms = [str(x) for x in _DEFAULT][:args.n]
    if len(_syms) < args.n:
        print(f"⚠ 既定リストは {len(_syms)}銘柄しかありません "
              f"(--n {args.n} に足りない)。--symbols で足してください")

print("=" * 70)
print(f"■ kabu 取得制限の実測 — {len(_syms)}銘柄 / "
      f"{'本番(18080)' if args.prod else 'デモ(18081)'}")
print("⛔ 照会のみ。発注しません")
print("=" * 70)

cli = KabuClient(prod=args.prod, dry_run=True)
_t = time.time()
cli.connect()
print(f"\n[1] トークン取得: {time.time() - _t:.2f}s")


def _board_one(_cli, sym):
    """1銘柄の /board を取り、(銘柄, 秒, 中身, 例外) を返す。
    ⚠ warmup / open の両モードから使うので、_one より前に置くこと。"""
    _s0 = time.time()
    try:
        return sym, (time.time() - _s0), _cli.get_board(sym), None
    except Exception as e:
        return sym, (time.time() - _s0), None, e

# ── PUSH配信(WebSocket)の疎通確認 (--ws) ─────────────────────────────────
# ⛔ REST は 6.5件/秒で頭打ち(並列を上げても変わらない)。1回読むだけなら
#    41銘柄6秒で足りるはずだが、板寄せ直後に膨らむなら PUSH に逃げる。
#    PUSH は /register した銘柄の板更新が**向こうから飛んでくる**ので、
#    ポーリングが要らない = 件数に比例した待ち時間が消える。
#    さらに「まだ寄っていない」を待つのではなく **寄った瞬間に届く**ので、
#    遅寄り銘柄の締切ルール(09:0Xまでに寄ったものだけ)が自然に書ける。
# ⚠ ここでやるのは **接続できるかどうか**だけ。場外では配信が来ないので
#    「0件受信」でも異常ではない。平日のザラ場中に流せば実データが流れる。
if args.ws:
    _wsurl = cli.base_url.replace("http://", "ws://") + "/kabusapi/websocket"
    print(f"\n[ws] PUSH配信の疎通確認: {_wsurl}")
    try:
        import websocket as _wsmod          # pip install websocket-client
    except Exception:
        print("  ⛔ websocket-client が入っていません。"
              "確認するなら: pip install websocket-client")
        raise SystemExit(1)
    _r = cli.register_many(_syms[:5])
    print(f"  登録(先頭5件): 受理 {len((_r or {}).get('RegistList') or [])}件")
    _got = {"n": 0, "syms": set(), "first": None, "err": None}

    def _on_msg(_w, _m):
        import json as _j
        _got["n"] += 1
        if _got["first"] is None:
            _got["first"] = time.time()
        try:
            _d = _j.loads(_m)
            _got["syms"].add(str(_d.get("Symbol") or ""))
        except Exception:
            pass

    def _on_err(_w, _e):
        _got["err"] = _e

    _t0 = time.time()
    _ws = _wsmod.WebSocketApp(_wsurl, on_message=_on_msg, on_error=_on_err)
    import threading as _th
    _th.Thread(target=lambda: _ws.run_forever(), daemon=True).start()
    while time.time() - _t0 < max(1, args.ws_seconds):
        time.sleep(0.5)
        if _got["n"] >= 50:
            break
    try:
        _ws.close()
    except Exception:
        pass
    if _got["err"]:
        print(f"  ⛔ 接続できません: {_got['err']}")
        print("     kabu ステーションの API 設定で PUSH配信 が有効か確認してください")
    elif _got["n"]:
        print(f"  ✅ **接続OK。{args.ws_seconds}秒で {_got['n']}件受信** "
              f"({len(_got['syms'])}銘柄)。初回まで "
              f"{(_got['first'] - _t0):.2f}s")
        print("     → REST が遅かったら PUSH に移行できます")
    else:
        print(f"  ⚠ 接続はできたが {args.ws_seconds}秒で受信0件。"
              f"**場外なら正常**(板が動いていない)。"
              f"平日ザラ場中にもう一度流して確かめてください")
    cli.unregister_all()
    raise SystemExit(0)

# ── 【08:5x】ウォームアップ ────────────────────────────────────────────────
# ⛔ 登録直後の初回 /board は 48〜142秒(実測)。kabu がその銘柄の板を購読しに
#    行くため。09:00 に登録して即読むと絶対に間に合わない。
if args.warmup:
    _t = time.time()
    _r = cli.register_many(_syms)
    _ok = len((_r or {}).get("RegistList") or [])
    print(f"\n[warmup] 登録: 要求 {len(_syms)}件 → 受理 {_ok}件 "
          f"({time.time() - _t:.2f}s)")
    if _ok < len(_syms):
        print(f"    ⛔ **{len(_syms) - _ok}件 登録できていません**。"
              f"kabu の登録上限は50件で、超えると 400 でリクエストごと失敗します。"
              f"候補を流動性上位50件に絞ってください")
    _t = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        _w = list(ex.map(lambda x: _board_one(cli, x), _syms))
    _nw = sum(1 for x in _w if x[2])
    print(f"[warmup] 空読み(コールド): {time.time() - _t:.2f}s / "
          f"取得できた {_nw}/{len(_syms)}件")
    print("\n✅ 登録を残したまま終了します。**09:00 ちょうどに次を実行**:")
    print(f"   python check_board_limits.py {'--prod ' if args.prod else ''}--open"
          + (f" --symbols-file {args.symbols_file}" if args.symbols_file else "")
          + f" --n {len(_syms)}")
    print("⚠ この間 watcher / 発注サーバを起動しないこと(トークンは1つ)")
    raise SystemExit(0)

# ── 【09:00】本番の1回だけを測る ──────────────────────────────────────────
if args.at_open:
    from datetime import datetime as _dt
    _t0 = _dt.now()
    _t = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        _o = list(ex.map(lambda x: _board_one(cli, x), _syms))
    _el = time.time() - _t
    _t1 = _dt.now()
    _ok = [x for x in _o if x[2]]
    _ng = [x for x in _o if x[2] is None]
    _lat = sorted(x[1] for x in _o)
    # 始値が入っているか / 何時に寄ったか
    _has = [x for x in _ok if (x[2] or {}).get("OpeningPrice")]
    _times = [str((x[2] or {}).get("OpeningPriceTime") or "") for x in _has]
    _at9 = sum(1 for t in _times if t[11:16] == "09:00")
    _late = [(x[0], str((x[2] or {}).get("OpeningPriceTime") or "")[11:16])
             for x in _has
             if str((x[2] or {}).get("OpeningPriceTime") or "")[11:16] != "09:00"]
    print("\n" + "=" * 70)
    print("■ 09:00 本番の実測 (照会のみ)")
    print("=" * 70)
    print(f"  開始 {_t0.strftime('%H:%M:%S.%f')[:-3]} → "
          f"終了 {_t1.strftime('%H:%M:%S.%f')[:-3]}")
    print(f"  **{len(_syms)}銘柄を {_el:.2f}s** (並列{args.workers} / "
          f"1件あたり中央 {_lat[len(_lat) // 2] * 1000:.0f}ms / "
          f"最遅 {_lat[-1] * 1000:.0f}ms)")
    print(f"  取得成功 {len(_ok)}/{len(_syms)}件"
          + (f" ⛔ 失敗 {len(_ng)}件: {_ng[0][3]}" if _ng else ""))
    print(f"  **始値あり {len(_has)}件** / うち 09:00 ちょうど {_at9}件 / "
          f"**09:00 でない {len(_late)}件**")
    for _sy, _hm in _late[:10]:
        print(f"      ⚠ {_sy} は {_hm} に寄っている → **建てない**"
              f"(LSS_SKIP_LATE_OPEN=1 と同じ判定)")
    _miss = len(_ok) - len(_has)
    if _miss:
        print(f"  ⚠ **始値がまだ入っていない {_miss}件**。"
              f"寄っていないので同じく建てない")
    print("\n  判定:")
    if _el <= 10:
        print(f"  ✅ {_el:.1f}s なら 09:00 の判定→発注に間に合う")
    elif _el <= 30:
        print(f"  ⚠ {_el:.1f}s。発注そのものの時間が別に乗る。要検討")
    else:
        print(f"  ⛔ {_el:.1f}s は遅すぎる。PUSH配信(WebSocket)を検討")
    if args.log:
        import csv as _cl
        import os as _os
        _new = not _os.path.exists(args.log)
        with open(args.log, "a", newline="", encoding="utf-8-sig") as _f:
            _w = _cl.writer(_f)
            if _new:
                _w.writerow(["date", "start", "end", "n", "workers", "sec",
                             "ok", "ng", "has_open", "at0900", "late",
                             "median_ms", "max_ms"])
            _w.writerow([_t0.strftime("%Y-%m-%d"),
                         _t0.strftime("%H:%M:%S.%f")[:-3],
                         _t1.strftime("%H:%M:%S.%f")[:-3],
                         len(_syms), args.workers, round(_el, 3),
                         len(_ok), len(_ng), len(_has), _at9, len(_late),
                         round(_lat[len(_lat) // 2] * 1000),
                         round(_lat[-1] * 1000)])
        print(f"\n[log] {args.log} に追記しました(朝ごとに貯めて比較できます)")
    print("\n⚠ 登録は **残したまま**にします(解除すると次が140秒級になる)")
    raise SystemExit(0)

# ── 登録上限のプローブ (--cap-probe) ─────────────────────────────────────
# ⛔ K は「その日の候補を全部登録して始値を読む」ので、候補数が上限を
#    超えるかどうかが実装の分かれ目。コードには50件と書いてあるが
#    **実機で確かめていない**。毎回 全解除してから測る(前の残りが混ざると
#    数が合わない)。
if args.cap_probe.strip():
    _probe = [int(x) for x in args.cap_probe.split(",")
              if str(x).strip().isdigit() and int(x) > 0]
    _pool = [str(x) for x in _DEFAULT]
    print("\n[1b] 登録上限のプローブ (全解除 → 一括登録 → 受理数)")
    if max(_probe) > len(_pool):
        print(f"     ⚠ 既定リストは {len(_pool)}銘柄しかありません。"
              f"{max(_probe)}件は測れないので --symbols-file で足してください")
    print(f"     {'要求':>6} {'受理':>6} {'秒':>7}  判定")
    _cap_found = None
    _last_ok = 0
    for _nq in _probe:
        if _nq > len(_pool):
            continue
        cli.unregister_all()
        _t0 = time.time()
        _r0 = cli.register_many(_pool[:_nq])
        _s0 = time.time() - _t0
        _ok0 = len((_r0 or {}).get("RegistList") or [])
        # ⛔ 上限超過は **部分受理ではなく 400 でリクエストごと失敗**する
        #    (2026-08-16 実測: 60件で 400 Bad Request → 受理0件)。
        #    「50件受理される」ではないので、呼ぶ側が **必ず事前に絞る**必要がある。
        if _ok0 >= _nq:
            _v = "✅ 全部通った"
            _last_ok = max(_last_ok, _nq)
        elif _ok0 == 0:
            _v = "⛔ **400 でリクエストごと拒否**(部分受理されない)"
            if _cap_found is None:
                _cap_found = _last_ok
        else:
            _v = f"⚠ 部分受理 {_ok0}件"
            if _cap_found is None:
                _cap_found = _ok0
        print(f"     {_nq:>6} {_ok0:>6} {_s0:>7.2f}  {_v}")
    # ★ 「50件登録済みの状態から さらに足せるか」= 総数の上限か1回の上限か
    if _last_ok > 0 and len(_pool) > _last_ok:
        cli.unregister_all()
        cli.register_many(_pool[:_last_ok])
        _r1 = cli.register_many(_pool[_last_ok:_last_ok + 1])
        _ok1 = len((_r1 or {}).get("RegistList") or [])
        print(f"     追加  {_ok1:>6}       -  "
              + ("⚠ **1回のリクエスト上限**であって総数の上限ではない"
                 if _ok1 else
                 f"⛔ **総登録数の上限が {_last_ok}件**"
                 f"(既に{_last_ok}件あると1件も足せない)"))
    cli.unregister_all()
    if _cap_found is None:
        print(f"     → 試した範囲({max(_probe)}件)では上限に当たりませんでした")
    else:
        print(f"     → **登録上限 = {_cap_found}件**。"
              f"⛔ 超えると **400 でリクエストごと失敗**するので、"
              f"候補が多い日は **前夜に流動性上位{_cap_found}件へ絞っておく**こと"
              f"(1件でも超えたら全滅する)")

# ── 一括登録 ──────────────────────────────────────────────────────────────
_t = time.time()
_res = cli.register_many(_syms)
_t_bulk = time.time() - _t
_n_ok = len((_res or {}).get("RegistList") or [])
print(f"\n[2] 一括登録(register_many): {_t_bulk:.2f}s")
print(f"    要求 {len(_syms)}件 → 受理 {_n_ok}件")
if _n_ok and _n_ok < len(_syms):
    print(f"    ⛔ **{len(_syms) - _n_ok}件が弾かれました** = 登録上限に到達。"
          f"上限は {_n_ok}件のようです")
elif not _n_ok:
    print("    ⚠ RegistList が空。API の応答形式が違うか、登録に失敗しています")

# ── /board を直列で全件 ───────────────────────────────────────────────────
def _one(sym):
    _s = time.time()
    try:
        b = cli.get_board(sym)
        return sym, (time.time() - _s), b, None
    except Exception as e:
        return sym, (time.time() - _s), None, e


_t_ser = 0.0
if args.serial:
    print("\n[3] /board を **直列** で全件")
    _t = time.time()
    _ser = [_one(s) for s in _syms]
    _t_ser = time.time() - _t
    _lat = sorted(x[1] for x in _ser)
    _err = [x for x in _ser if x[3] is not None]
    print(f"    合計 {_t_ser:.2f}s / 1件あたり "
          f"中央 {_lat[len(_lat) // 2] * 1000:.0f}ms"
          f" / 最遅 {_lat[-1] * 1000:.0f}ms")
    if _err:
        print(f"    ⛔ エラー {len(_err)}件: {_err[0][3]}")
    # ⛔ 2026-08-16(日)の実測で **1件あたり 5005ms(中央) / 最遅 5039ms** と
    #    出た。全件がほぼ同じ5秒で、429 は1件も出ていない。localhost で
    #    5秒は説明がつかず、並列だと1件あたり実質0.2秒で終わることとも
    #    矛盾する。**原因不明のまま数字を信じないこと**。休日で kabu が
    #    バックエンドに繋がらず待たされている可能性があるので、平日に
    #    測り直して同じ値なら本物。
    if _lat and _lat[len(_lat) // 2] > 2.0:
        print("    ⛔ 1件あたりが2秒を超えています。localhost では異常です。"
              "休日/場外の可能性があるので **平日に測り直してください**")
else:
    print("\n[3] 直列: スキップ (--serial で測る。41件で2分以上かかります)")

# ── /board を並列で全件 ───────────────────────────────────────────────────
# ★★ 登録直後の初回は **桁違いに遅い**(2026-08-17 実測)。
#   kabu ステーションが その銘柄の板を購読しに行くため。
#     初回(コールド) 41銘柄 並列2 → **48.08s** (timeout も出る)
#     2回目(ウォーム) 同条件   → **6.27s** (失敗0)
#   日曜の直列 5005ms/件 も、全件が『その銘柄の初回』だったからで説明がつく。
#   ⛔ **本番で使う数字はウォームのほう**。09:00 に登録して即読むと
#      40〜50秒かかって間に合わない。**08:5x に登録して1回空読みしておく**
#      ことが K の実装要件になる。
print(f"\n[4] /board **初回(コールド)** 並列({args.workers}) — "
      f"登録直後なので遅い。本番はここではない")
_t = time.time()
with ThreadPoolExecutor(max_workers=args.workers) as ex:
    _par = list(ex.map(_one, _syms))
_t_par = time.time() - _t
_err2 = [x for x in _par if x[3] is not None]
print(f"    合計 {_t_par:.2f}s"
      + (f"  (直列比 {_t_ser / max(_t_par, 1e-9):.1f}倍速)" if _t_ser else ""))
if _err2:
    print(f"    ⛔ エラー {len(_err2)}件: {_err2[0][3]}")
    print("       → レート制限に当たっている可能性。--workers を下げて再測定")

# ── 並列数のスイープ (429 が出ない最大を探す) ─────────────────────────────
# ★ 本番の並列数は「429 が出ない最大」で決めるしかない。429 が出ると
#   1.5〜4.5秒の待ちが入るので、**並列を上げすぎると逆に遅くなる**。
_rows: list = []
_sw = [int(x) for x in str(args.sweep).split(",") if str(x).strip().isdigit()]
if _sw:
    print("\n[4b] 並列数スイープ — 429 が出ない最大を探す")
    print(f"     {'並列':>4}{'秒':>8}{'429回':>8}{'timeout':>8}{'失敗':>8}")
    cli.quiet_429 = True
    for _w in _sw:
        cli.n_429 = 0
        cli.n_timeout = 0
        _t = time.time()
        _r = list(ThreadPoolExecutor(max_workers=_w).map(_one, _syms))
        _el = time.time() - _t
        _n4, _nt = getattr(cli, "n_429", 0), getattr(cli, "n_timeout", 0)
        _ng = sum(1 for x in _r if x[3] is not None)
        _rows.append((_w, _el, _n4, _nt, _ng))
        print(f"     {_w:>4}{_el:>8.2f}{_n4:>8}{_nt:>8}{_ng:>8}")
    cli.quiet_429 = False
    # ⛔ 判定基準を『429がゼロ』にしてはいけない(2026-08-17 に読み違えた)。
    #   実測では 並列2→16 で **秒数がほぼ変わらず(6.1〜7.6s)、429だけが
    #   8→41回に増えた**。kabu 側でスループットが固定されているので、
    #   並列を上げても1秒も速くならない。
    #   → 正しい基準は「**同じ速さなら 429/失敗が最少の並列数**」。
    if _rows:
        _tmin = min(r[1] for r in _rows)
        # 最速から10%以内を「同じ速さ」とみなし、その中で429+失敗が最少
        _cand = [r for r in _rows if r[1] <= _tmin * 1.10]
        _pick = min(_cand, key=lambda r: (r[2] + r[4], r[0]))
        print(f"\n     最速 {_tmin:.2f}s / スループット "
              f"約 {len(_syms) / max(_tmin, 1e-9):.1f}件/秒")
        if max(r[1] for r in _rows) <= _tmin * 1.3:
            print("     ⛔ **並列を上げても速くなっていません**"
                  "(kabu側でスループットが固定)。上げるだけ429が増えます")
        print(f"     → **推奨は 並列{_pick[0]}** "
              f"({_pick[1]:.2f}s / 429 {_pick[2]}回 / 失敗 {_pick[4]}件)")

# ── 中身の確認 (始値が取れているか) ────────────────────────────────────────
print("\n[5] 取れた中身 (先頭5件)")
print(f"    {'銘柄':<8}{'始値':>10}{'現在値':>10}{'前日終値':>10}  始値時刻")
for sym, _l, b, e in _par[:5]:
    if not b:
        print(f"    {sym:<8}  ⛔ {e}")
        continue
    print(f"    {str(sym):<8}{str(b.get('OpeningPrice')):>10}"
          f"{str(b.get('CurrentPrice')):>10}"
          f"{str(b.get('PreviousClose')):>10}  {b.get('OpeningPriceTime')}")
_no_open = sum(1 for _s, _l, b, _e in _par if b and not b.get("OpeningPrice"))
if _no_open:
    print(f"    ⚠ OpeningPrice が空/0 のもの {_no_open}件"
          f"(寄り前なら正常。ザラ場中なら要調査)")

# ★★ 遅寄りの判別 (2026-08-16)。K は 09:00 に件数を確定させないとサイズを
#    決められないが、**09:00 に寄らない銘柄がある**(実測 9984 は 09:06)。
#    OpeningPriceTime があれば「まだ寄っていない」を確実に判別できるので、
#    『09:0X までに寄った銘柄だけで確定する』ルールが実装できる。
_ot = [(str(_s), str((b or {}).get("OpeningPriceTime") or ""))
       for _s, _l, b, _e in _par if b]
_late = [(x, t) for x, t in _ot if t and t[11:16] not in ("09:00",)]
print(f"\n    [始値時刻] 取得できた {len(_ot)}件 / "
      f"09:00 ちょうど {len(_ot) - len(_late) - sum(1 for _x, t in _ot if not t)}件"
      f" / **09:00 でない {len(_late)}件**"
      + (f" / 時刻なし {sum(1 for _x, t in _ot if not t)}件"
         if any(not t for _x, t in _ot) else ""))
for _x, _t in _late[:5]:
    print(f"      ⚠ {_x} は {_t[11:16]} に寄っている")
if _late:
    print("      → **OpeningPriceTime で『まだ寄っていない』を判別できる**。"
          "09:0X までに寄った銘柄だけで件数を確定するルールが組める")

# ── 判定 ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("■ K(09:00確認)が成立するか")
print("=" * 70)
# ⛔ 判定には **ウォーム(2回目以降)** の秒数を使う。コールドは登録直後の
#   購読待ちで、本番は 08:5x に済ませておくもの。
_warm = min((r[1] for r in _rows), default=_t_par)
_total = _warm
print(f"  コールド(登録直後) {_t_par:.2f}s  ← 08:5x に済ませておく")
print(f"  **ウォーム(本番の09:00) {_warm:.2f}s**  ← これが判定対象")
# ⛔ 5分遅れると K の優位はほぼ消える(18.38)。逆算した目安を出す。
if _total <= 10:
    print("  ✅ 十分。09:00 の判定→発注に間に合う")
elif _total <= 30:
    print("  ⚠ 許容範囲だが、発注そのものの時間が別に乗る。並列数を上げて再測定")
else:
    print("  ⛔ 遅すぎる。この秒数だと K の優位(5分で消える)を削る。"
          "PUSH配信(WebSocket)への移行を検討")
print("\n  ★ 実装要件: **08:5x に候補を登録して1回空読みしておくこと**。"
      "09:00 に登録して即読むと 40〜50秒かかり間に合わない")
print("  ⚠ ただし本番の 09:00 は板寄せ直後で API も混む。"
      "**寄り付き直後に一度測り直すこと**(この結果は空いている時間帯の値)")

if not args.no_unregister:
    for s in _syms:
        cli.unregister(s)
    print(f"\n[cleanup] {len(_syms)}銘柄の登録を解除しました")
