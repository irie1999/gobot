r"""morning_test.py — 明日の朝に測るものを **1コマンド** で順番に回す (照会のみ)

⛔ **発注しない**。register / board / unregister しか叩かない。

★ 2026-08-16 ユーザー決定: **現行Hは実行しない。J/K のデータ取得に専念する。**
   → `.\watch` も発注も不要。トークンの取り合いが起きないので測定に集中できる。

■ なぜ1本にまとめたか

朝に測りたいものが5つあり、**どれも時刻が決まっていて順番も決まっている**。
手で順に叩くと必ず取り違える(§18.38 で実際に --warmup を飛ばして140秒級の
数字を出した)。

■ 何を測るか (実行順)

  0. シグナル収集   k_open_confirm --collect   ← **kabu を使わない。最初にやる**
       WATCHLIST 全部ではなく **今日シグナルが出た銘柄** を集める
       (実発注 lss_budget_cap と同じ _lss_signal_today を使う)。
       yfinance のバックテストなので数分かかる。09:00 より前に終わらせる。

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

  5. K の記録       k_open_confirm --now   ← 4 の直後(登録がウォームなので速い)
       全候補の始値を取り、+50bp判定 → 合格件数 → 予算÷件数 で株数まで出して
       k_paper_<日付>.csv に書く。**その朝 K なら何を建てたか**の記録。
       ⛔ 発注しない(k_open_confirm は売買系を import すらしていない)。

■ 使い方

    .\mtest            (= python morning_test.py --prod)
    python morning_test.py --prod --skip-rotate    # バッチ回しを飛ばす
    python morning_test.py --prod --no-open        # 09:00 の測定をしない
    python morning_test.py --prod --no-kpaper      # K の記録をしない
    python morning_test.py --dry-run               # 手順だけ表示して終了

■ ⛔ 注意

  ・**開始は 08:30 まで**。1(バッチ回し)に数分かかる。
  ・実行中は発注サーバを起動しない(kabu の有効トークンは1つ)。
  ・⚠ 候補リストが要るので、**前日の引け後か当日の朝に `.\daily` を1回**
    流しておくこと(holdout_selected_symbols.py が候補のソース)。
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
ap.add_argument("--no-kpaper", action="store_true",
                help="K のペーパー記録をしない")
ap.add_argument("--poll-until", type=str, default="09:30",
                help="K記録のポーリング締切")
ap.add_argument("--poll-every", type=int, default=10,
                help="K記録のポーリング間隔(秒)")
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
  ★ 現行Hは実行しません(2026-08-16 決定)。**J/K のデータ取得に専念**します。
    → `.\\watch` も発注も不要。トークンの取り合いが起きないので測定に集中できます。

  手順:
    0. シグナル収集 kabu不要      {'(スキップ)' if args.no_kpaper else ''}
    1. バッチ回し   --rotate {args.rotate}      {'(スキップ)' if args.skip_rotate else ''}
    2. 気配ログ     〜{args.preopen_until}          {'(スキップ)' if args.skip_preopen else ''}
    3. ウォームUP   {args.warmup_at}
    4. 本番速度     {args.open_at}          {'(スキップ)' if args.no_open else ''}
    5. K記録        4の直後       {'(スキップ)' if args.no_kpaper else ''}
""", flush=True)

_res: list[tuple] = []

# ── 0. 今日のシグナル収集 (kabu を使わない / 一番先にやる) ──────────────────
# ⛔⛔ 候補は **WATCHLIST 全部ではなく「今日シグナルが出た銘柄」**。
#    yfinance のバックテストを回すので数分かかるが、kabu を一切触らないので
#    他の測定と競合しない。**09:00より前に終わらせる必要がある**ので最初に置く。
if not args.no_kpaper:
    _cmd0 = _PY + ["k_open_confirm.py", "--collect"]
    if args.symbols_file:
        _cmd0 += ["--symbols-file", args.symbols_file]
    _res.append(("0. シグナル収集", _run(
        "0. 今日のシグナル収集 (k_signals_<日付>.csv / kabu 不要・数分かかる)",
        _cmd0)))

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
        f"2. 気配ログ (〜{args.preopen_until} / 初日)"
        f" ★母集団は広く取る。締切まで回し続けます", _cmd)))

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

# ── 5. K のペーパー記録 (2026-08-16 ユーザー決定: 現行Hは止め、J/K に専念) ──
#   ⛔ 発注しない。k_open_confirm.py は売買系を import すらしていない。
#   4 の直後に走らせる(登録がまだウォームなので速い)。
if not args.no_kpaper:
    # ★★ ポーリング版 (2026-08-16)。09:00 以降に寄る銘柄も拾い、
    #   **寄り時刻の実データ**を貯める。これが「即時」(§18.41)の検証に直結。
    #   ⛔ 発注はしない(k_open_confirm は売買系を import すらしていない)。
    _cmd5 = (_PY + ["k_open_confirm.py"] + _P
             + ["--poll", "--now", "--poll-until", args.poll_until,
                "--every", str(args.poll_every), "--now-polls", "999"])
    if args.symbols_file:
        _cmd5 += ["--symbols-file", args.symbols_file]
    _res.append(("5. K記録", _run(
        f"5. K のペーパー記録 — **{args.poll_every}秒ごとに{args.poll_until}まで"
        f"ポーリング** (k_paper_<日付>.csv / ⛔発注しない)", _cmd5)))

print(f"""
{'=' * 74}
■ 結果
{'=' * 74}""")
for _s, _ok in _res:
    print(f"  {'✅' if _ok else '⛔'} {_s}")

print(f"""
{'=' * 74}
▶▶ 終了。kabu の登録は解除済みです
{'=' * 74}
  ★ 2026-08-16 のユーザー決定により **現行Hは実行しません**。
    したがって `.\\watch` も不要です(建玉が無いので監視対象がない)。
    トークンの取り合いも起きないので、測定に専念できます。

  ★ 見るところ:
    1. バッチ回し … **2周目が1周目より速いか**。速ければ kabu は購読を
       覚えているので、寄り前に全バッチを空読みしておけば 09:00 は
       全部ウォームで回せる = **登録上限50件の制約が消える**。
       遅ければ毎回コールドなので、50件に絞るしかない。
    2. 気配ログ  … preopen_board_<日付>.csv が出来ていれば OK。
       ★ 母集団は **候補だけでなく広く**取る(2026-08-16 ユーザー提案)。
         気配→始値の関係は発注候補である必要がないので、広げるほど1日の
         件数が増え、判定に要る 500〜1,000件に **数営業日**で届く。
         今日の候補は先頭に置き、`is_cand` 列で後から切り分けられる。
       ⛔ **1,000件 貯まるまで --verify を覗かないこと**(実質 in-sample になる)。
    4. 本番速度  … 場外の 6.3秒 と比べる。板寄せ直後で遅くなっていないか。
       5分遅れると K の優位は消えるので、**数十秒以内**なら合格。
    5. K記録     … k_paper_<日付>.csv。見るのは3つ:
       ①**09:00に未寄の割合**(BTの実測15.7%と合うか)
       ②**グループの一覧**(何時に何件寄ったか) ← 「即時」(§18.41)の実データ
       ③**1周の読込秒数**が間隔(10秒)に収まっているか
       ⛔ 発注していません。「その朝 K なら何を建てたか」の記録です。
""")
