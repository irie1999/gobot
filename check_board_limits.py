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
  1. 一括登録(register_many) と 1件ずつ登録 の所要時間
  2. /board を 直列 / 並列 で全件取る所要時間(= 09:00 に何秒かかるか)
  3. 登録上限(コードには50件と記録)に実際に当たるか
  4. レート制限に当たるか(エラーが出るか)

使い方:
  python check_board_limits.py --prod --n 41
  python check_board_limits.py --prod --symbols 7203,6758,9984
  python check_board_limits.py --prod --n 60      # 上限を超えさせて挙動を見る

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
args = ap.parse_args()

# 既定リスト: 流動性の高い主力。上限テスト用に多めに持つ。
_DEFAULT = [
    7203, 6758, 9984, 8306, 6501, 6902, 7267, 6367, 4063, 8058,
    9432, 8035, 6098, 4568, 7741, 6273, 6146, 8001, 8031, 9433,
    4661, 6301, 7011, 6857, 9983, 8316, 8411, 7751, 6954, 4502,
    6981, 7735, 4519, 6503, 6702, 8801, 8802, 3382, 2914, 4901,
    5108, 7269, 6752, 4543, 4578, 6178, 8766, 8750, 9022, 9020,
    4452, 6981, 7013, 5401, 5406, 3407, 4021, 4188, 2802, 2502,
]
# 重複を除いて順序は保つ(--n で先頭から取るため)
_DEFAULT = list(dict.fromkeys(_DEFAULT))

if args.symbols.strip():
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
print(f"\n[4] /board を **並列({args.workers})** で全件")
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
_sw = [int(x) for x in str(args.sweep).split(",") if str(x).strip().isdigit()]
if _sw:
    print("\n[4b] 並列数スイープ — 429 が出ない最大を探す")
    print(f"     {'並列':>4}{'秒':>8}{'429回':>8}{'timeout':>8}{'失敗':>8}")
    cli.quiet_429 = True
    _rows = []
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

# ── 判定 ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("■ K(09:00確認)が成立するか")
print("=" * 70)
_total = _t_bulk + _t_par
print(f"  登録 {_t_bulk:.2f}s + 並列取得 {_t_par:.2f}s = **{_total:.2f}s**")
# ⛔ 5分遅れると K の優位はほぼ消える(18.38)。逆算した目安を出す。
if _total <= 10:
    print("  ✅ 十分。09:00 の判定→発注に間に合う")
elif _total <= 30:
    print("  ⚠ 許容範囲だが、発注そのものの時間が別に乗る。並列数を上げて再測定")
else:
    print("  ⛔ 遅すぎる。この秒数だと K の優位(5分で消える)を削る。"
          "PUSH配信(WebSocket)への移行を検討")
print("\n  ⚠ ただし本番の 09:00 は板寄せ直後で API も混む。"
      "**寄り付き直後に一度測り直すこと**(この結果は空いている時間帯の値)")

if not args.no_unregister:
    for s in _syms:
        cli.unregister(s)
    print(f"\n[cleanup] {len(_syms)}銘柄の登録を解除しました")
