r"""morning_test.py — 明日の朝に測るものを **1コマンド** で順番に回す (照会のみ)

⛔ **発注しない**。register / board / unregister しか叩かない。

■ なぜ1本にまとめたか

明日の朝に測りたいものが4つあり、**どれも時刻が決まっていて順番も決まっている**。
手で順に叩くと必ず取り違える(§18.38 で実際に --warmup を飛ばして140秒級の
数字を出した)。しかも **kabu の有効トークンは1つ**なので、`.\watch` や
発注サーバと重なると401の取り合いになる。

■ 何を測るか (実行順)

  1. バッチ回し     check_board_limits --rotate 100
       50件ずつ回して100銘柄読めるか。**2周目が速ければ kabu は購読を覚えている**
       = 寄り前に全バッチを空読みしておけば 09:00 は全部ウォームで回せる。
       ⛔ ここが本命。成立すれば K の「登録上限50件」制約が消える。
       ⚠ 登録/解除を繰り返すので **一番先にやる**(429 が後続に波及しないよう
          最後にクールダウンを置く)。

  2. 気配ログ       log_preopen_board --prod
       08:45〜08:57 の気配を貯める。**初日**。1ヶ月貯めて初めて判定できる。

  3. ウォームアップ  check_board_limits --warmup
       09:00 の測定を「登録直後の初回(48〜142秒)」にしないための空読み。
       ⛔ これを飛ばすと 09:00 の数字が測定にならない。

  4. 本番の速度     check_board_limits --open   ← **09:00 ちょうど**
       板寄せ直後の実測。board_speed_log.csv に追記される。
       これまでの実測は全部 **場外**で、本番の09:00は一度も測っていない。

■ 使い方

    .\mtest            (= python morning_test.py --prod)
    python morning_test.py --prod --skip-rotate    # バッチ回しを飛ばす
    python morning_test.py --prod --no-open        # 09:00 の測定をしない
    python morning_test.py --dry-run               # 手順だけ表示して終了

■ ⛔ 運用との衝突について (必ず読むこと)

  ・**開始は 08:30 まで**。1(バッチ回し)に数分かかる。
  ・実行中は `.\watch` / 発注サーバを **起動しない**(トークンは1つ)。
  ・09:00 の測定は6秒ほど。**delay1 なので 09:00〜09:05 は元々損切りを
    置かない**ため、この6秒を失っても実害はない(利確が寄り直後6秒で
    1.0ATR に届くことはまずない)。
  ・終わったら **すぐ `.\watch` を起動**すること。最後に画面に出す。
  ・⚠ H の発注(`.\daily` → 発注ボタン)は **このスクリプトの前に**済ませる。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
import time

ap = argparse.ArgumentParser(description="明日の朝の測定を順番に回す(照会のみ)")
ap.add_argument("--prod", action="store_true", help="本番(18080)。既定はデモ(18081)")
ap.add_argument("--n", type=int, default=41, help="速度測定の銘柄数")
ap.add_argument("--rotate", type=int, default=100, help="バッチ回しで読む銘柄数")
ap.add_argument("--symbols-file", type=str, default="",
                help="気配ログの銘柄ソース(既定 holdout_selected_symbols.py)")
ap.add_argument("--preopen-until", type=str, default="08:57")
ap.add_argument("--warmup-at", type=str, default="08:58")
ap.add_argument("--open-at", type=str, default="09:00")
ap.add_argument("--skip-rotate", action="store_true")
ap.add_argument("--skip-preopen", action="store_true")
ap.add_argument("--no-open", action="store_true", help="09:00 の測定をしない")
ap.add_argument("--dry-run", action="store_true", help="手順だけ出して終了")
args = ap.parse_args()

_P = ["--prod"] if args.prod else []
_PY = [sys.executable]


def _hm(s: str) -> _dt.datetime:
    _h, _m = (int(x) for x in str(s).split(":"))
    return _dt.datetime.now().replace(hour=_h, minute=_m, second=0, microsecond=0)


def _wait_until(t: _dt.datetime, why: str) -> None:
    _s = (t - _dt.datetime.now()).total_seconds()
    if _s <= 0:
        return
    print(f"\n[待機] {t:%H:%M:%S} まで {_s:.0f}秒 待ちます — {why}", flush=True)
    while (_r := (t - _dt.datetime.now()).total_seconds()) > 0:
        time.sleep(min(10.0, _r))


def _run(step: str, cmd: list[str]) -> bool:
    """1ステップ。**失敗しても止めない**(朝は先に進むほうが大事)。"""
    print(f"\n{'=' * 74}\n▶ {step}\n  $ {' '.join(cmd[1:])}\n{'=' * 74}",
          flush=True)
    if args.dry_run:
        return True
    _t0 = time.time()
    try:
        _r = subprocess.run(cmd)
        _ok = (_r.returncode == 0)
    except Exception as e:
        print(f"  ⛔ 起動できませんでした: {e}", flush=True)
        _ok = False
    print(f"  {'✅' if _ok else '⛔'} {step} — {time.time() - _t0:.0f}秒",
          flush=True)
    if not _ok:
        print("  ⚠ 失敗しましたが次に進みます(朝は先に進むほうが大事)。"
              "\n    401 ならトークンの取り合いです。watcher / 発注サーバを"
              "止めてから、この手順をやり直してください", flush=True)
    return _ok


print(f"""
{'=' * 74}
■ 朝の測定 — {_dt.date.today()} {'(dry-run)' if args.dry_run else ''}
{'=' * 74}
  接続先: {'★本番(18080)' if args.prod else 'デモ(18081)'}
  ⛔ 照会のみ。発注しません。
  ⛔ 実行中は .\\watch / 発注サーバを起動しないこと(トークンは1つ)。
  ⚠ H の発注は **これより前に** 済ませてください。

  手順:
    1. バッチ回し   --rotate {args.rotate}      {'(スキップ)' if args.skip_rotate else ''}
    2. 気配ログ     〜{args.preopen_until}          {'(スキップ)' if args.skip_preopen else ''}
    3. ウォームUP   {args.warmup_at}
    4. 本番速度     {args.open_at}          {'(スキップ)' if args.no_open else ''}
""", flush=True)

_res: list[tuple] = []

# ── 1. バッチ回し (一番先。登録/解除を繰り返すので後続に429を残さない) ──────
if not args.skip_rotate:
    _res.append(("1. バッチ回し", _run(
        f"1. バッチ回し ({args.rotate}銘柄を{args.rotate // 50}バッチ×2周)",
        _PY + ["check_board_limits.py"] + _P
        + ["--rotate", str(args.rotate)])))
    if not args.dry_run:
        print("\n[クールダウン] 30秒(/register のレート制限を後続に残さない)",
              flush=True)
        time.sleep(30)

# ── 2. 気配ログ ────────────────────────────────────────────────────────
if not args.skip_preopen:
    _cmd = (_PY + ["log_preopen_board.py"] + _P
            + ["--until", args.preopen_until, "--every", "120"])
    if args.symbols_file:
        _cmd += ["--symbols-file", args.symbols_file]
    _res.append(("2. 気配ログ", _run(
        f"2. 気配ログ (〜{args.preopen_until} / 初日)", _cmd)))

# ── 3. ウォームアップ ──────────────────────────────────────────────────
if not args.no_open:
    if not args.dry_run:
        _wait_until(_hm(args.warmup_at), "登録して1回空読み(初回は140秒級)")
    _res.append(("3. ウォームUP", _run(
        f"3. ウォームアップ ({args.n}銘柄を登録して空読み)",
        _PY + ["check_board_limits.py"] + _P
        + ["--warmup", "--n", str(args.n)])))

    # ── 4. 本番の速度 (09:00 ちょうど) ─────────────────────────────────
    if not args.dry_run:
        _wait_until(_hm(args.open_at), "★板寄せ直後の実測。ここが本番")
    _res.append(("4. 本番速度", _run(
        f"4. 本番の取得速度 ({args.open_at} / board_speed_log.csv に追記)",
        _PY + ["check_board_limits.py"] + _P
        + ["--open", "--n", str(args.n)])))

print(f"""
{'=' * 74}
■ 結果
{'=' * 74}""")
for _s, _ok in _res:
    print(f"  {'✅' if _ok else '⛔'} {_s}")

print(f"""
{'=' * 74}
▶▶ **いますぐ `.\\watch` を起動してください**
{'=' * 74}
  kabu の登録は解除済みです。発注枠は空いています。

  ★ 見るところ:
    1. バッチ回し … **2周目が1周目より速いか**。速ければ kabu は購読を
       覚えているので、寄り前に全バッチを空読みしておけば 09:00 は
       全部ウォームで回せる = **登録上限50件の制約が消える**。
       遅ければ毎回コールドなので、50件に絞るしかない。
    2. 気配ログ  … preopen_board_<日付>.csv が出来ていれば OK。
       ⛔ **1ヶ月貯めるまで --verify を覗かないこと**(実質 in-sample になる)。
    4. 本番速度  … 場外の 6.3秒 と比べる。板寄せ直後で遅くなっていないか。
       5分遅れると K の優位は消えるので、**数十秒以内**なら合格。
""")
