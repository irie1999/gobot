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

  1. バッチ回し     check_board_limits --rotate 100   ← **既定OFF(結論済み)**
       ⛔ 2026-08-17 に決着(§18.44)。寄り前/場中は **0.3〜0.6件/秒**で、
          2周目も速くならない = バッチ回しでは 50件の壁を破れない。
          5〜8分かかって気配ログの時間を奪うので既定OFF。--with-rotate で復活。

  2. 気配ログ       log_preopen_board --prod   ← **08:00〜08:45**
       **母集団は広く取る**(lss_proposal_full.py)。0.5件/秒 × 45分 =
       約1,350件/日で、判定に要る1,000件に **1営業日**で届く。
       ⚠ 1周終わらなくてよい。読んだぶんが全部データ。候補に絞ると
          同じ銘柄を数回読むだけでユニークが増えず4倍遅くなる。
       ★ 今日の候補が先頭に来る + 08:37 から直前スイープで読み直すので、
          いちばん知りたい銘柄は寄り直前の気配で上書きされる。
       ⛔ K のウォームアップ(08:47)より前に終える。トークンは1つ。

  3+4. 対照測定     check_board_limits --warmup / --open   ← **既定OFF**
       41銘柄の対照。K のウォームアップ時間を奪うので、要るときだけ
       --with-board-speed。K記録(5)自体が本番速度の実測になる。

  5. K の記録       k_open_confirm --poll   ← **08:47 空読み → 09:00 本番**
       ⛔⛔ ここが最重要。候補は数百銘柄(=6バッチ級)で、**コールドだと
          1バッチ48秒 → 全部で約5分**。09:00 に間に合わせるには 08:47 には
          空読みを始めていないといけない。旧設定(09:00過ぎに起動)では
          登録がまるごとコールドで、始値スナップショットが5分遅れていた。
       全候補の始値を取り、+50bp判定 → 合格件数 → 予算÷件数 で株数まで出して
       k_paper_<日付>.csv に書く。09:30 まで10秒ごとに回して遅寄りも拾う。
       ⛔ 既定は **発注しない**(記録だけ)。`.\jorder`(= --execute)を付けた
          ときだけ、合格銘柄を **保護指値売り @ 始値×(1-50bp)** で実発注する。

■ 使い方

    .\mtest            (= python morning_test.py --prod)
    python morning_test.py --prod --with-rotate    # バッチ回しも測る(結論済み)
    python morning_test.py --prod --no-open        # 09:00 の測定をしない
    python morning_test.py --prod --no-kpaper      # K の記録をしない
    python morning_test.py --dry-run               # 手順だけ表示して終了

■ ⛔ 注意

  ・**開始は 08:00**。0(シグナル収集)に5〜10分かかり、残りを 2(気配ログ)に
    充てたい。08:30 開始でも 09:00 の測定は守られるが、気配ログが削られる。
    前夜/早朝に `python k_open_confirm.py --collect` を済ませてあれば 0 は
    自動スキップされるので、08:20 開始でも足りる。
  ・実行中は発注サーバ / watcher を起動しない(kabu の有効トークンは1つ)。
  ・候補は 0(--collect)が **holdout_selected_symbols.py**(= レポートの
    発注リストと同じ母集団)から作る。⛔ そのファイルは `.\daily` が書くので、
    **前日に `.\daily` を回しておくこと**。無いと cumul にフォールバックし、
    価格・空売り可否のフィルタが掛かっていない母集団になる。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
from pathlib import Path
import time

ap = argparse.ArgumentParser(description="明日の朝の測定を順番に回す(照会のみ)")
ap.add_argument("--prod", action="store_true", help="本番(18080)。既定はデモ(18081)")
ap.add_argument("--n", type=int, default=41, help="速度測定の銘柄数")
ap.add_argument("--rotate", type=int, default=100, help="バッチ回しで読む銘柄数")
ap.add_argument("--symbols-file", type=str, default="",
                help="気配ログの銘柄ソース(既定 holdout_selected_symbols.py)")
# ⛔⛔ 気配ログは **K のウォームアップの前に終える**こと (2026-08-17)。
#   kabu の有効トークンは1つなので同時に走らせられない。そして K の候補は
#   299銘柄=6バッチあり、**コールドだと1バッチ48秒 → 全部で約5分**かかる。
#   09:00 に間に合わせるには 08:47 には空読みを始めていないといけない。
#   旧設定(気配ログ 08:57まで → K は09:00過ぎに起動)だと K の登録が
#   まるごとコールドで、09:00 の始値スナップショットが5分遅れていた。
# ⛔ 早く起動しすぎたときのガード (2026-08-17)。気配ログは締切まで回し続ける
#    ので、深夜に起動すると **何時間も無駄に kabu を叩く**(寄り前の気配が
#    出るのは 08:00 前後から)。ここまで待ってから始める。
#    ⚠ 既に過ぎていれば待たないので、08:30 起動でも影響しない。
ap.add_argument("--preopen-from", type=str, default="08:00",
                help="気配ログを始める時刻。これより早く起動したら待つ")
ap.add_argument("--preopen-until", type=str, default="08:45")
ap.add_argument("--k-warm-at", type=str, default="08:47",
                help="K の空読み開始。候補6バッチ×コールド48秒 ≒ 5分を見込む")
ap.add_argument("--warmup-at", type=str, default="08:58")
ap.add_argument("--open-at", type=str, default="09:00")
# ⛔ 2026-08-17 に結論が出た(§18.44): 場中/寄り前は 0.3〜0.6件/秒で、
#    2周目も速くならない = バッチ回しでは壁を破れない。毎朝やる意味が
#    無くなったので **既定OFF**。5〜8分かかり、気配ログの時間を奪う。
ap.add_argument("--skip-rotate", action="store_true",
                help="(既定で飛ばします。--with-rotate で有効化)")
ap.add_argument("--with-rotate", action="store_true",
                help="バッチ回しを測り直す。⛔結論は出ている(§18.44)")
ap.add_argument("--skip-preopen", action="store_true")
ap.add_argument("--no-open", action="store_true", help="09:00 の測定をしない")
ap.add_argument("--with-board-speed", action="store_true",
                help="41銘柄の対照測定(check_board_limits)も挟む。"
                     "⛔ K のウォームアップ時間を奪うので既定OFF")
ap.add_argument("--no-kpaper", action="store_true",
                help="K のペーパー記録をしない")
ap.add_argument("--poll-until", type=str, default="09:30",
                help="K記録のポーリング締切")
ap.add_argument("--poll-every", type=int, default=10,
                help="K記録のポーリング間隔(秒)")
ap.add_argument("--force-collect", action="store_true",
                help="k_signals_<日付>.csv が既にあっても作り直す")
ap.add_argument("--dry-run", action="store_true", help="手順だけ出して終了")
# ★★ 実発注 (2026-08-17)。既定OFF。付けない限り手順5は記録だけ。
ap.add_argument("--execute", action="store_true",
                help="⚠ 手順5で **実発注する**(J)。既定は記録のみ")
# ⛔ 既定は **60万**。価格帯の上限は6,000円なので 1単元=60万 必要。
#   50万にすると 5,000円超の銘柄が「0単元」で黙って落ちる(実測)。
#   滑りを測るのが目的なのに、値がさ株だけ母集団から消えると偏る。
ap.add_argument("--budget", type=float, default=60.0,
                help="--execute の予算(万円)。"
                     "既定60万 = 6,000円株1単元がちょうど建てられる最小額")
ap.add_argument("--max-notional", type=float, default=0.0,
                help="--execute の発注総額ハード上限(万円)。0=--budget と同じ")
ap.add_argument("--stop-delay-bars", type=int, default=4,
                help="watcher の損切り遅延(J は 4)")
ap.add_argument("--no-watch", action="store_true",
                help="--execute でも watcher を自動起動しない(手で起動する)")
# ★ watcher に渡す保険。J の保護指値が寄りで刺さらないと板に残り、昼に値が
#   戻ったところで約定しうる(モデルに無い時刻・値段の建玉になる)。
#   k_open_confirm が自分で取り消すが、そこで落ちた場合に残る。
#   ポーリング締切(09:10)の直後に watcher 側でも掃く。
ap.add_argument("--entry-cutoff", type=str, default="09:15",
                help="watcher が未約定の新規売りを取り消す時刻(保険)")
args = ap.parse_args()

# ⛔⛔ 実発注するときはポーリングを早く切る (2026-08-17)。
#   kabu の有効トークンは1つなので、**このスクリプトが終わるまで watcher を
#   起動できない**。既定の 09:30 まで回すと、09:00 に建てた建玉が30分間
#   無防備になる。J は delay4 = **09:20 に損切りを武装する**設計なので、
#   watcher が 09:30 起動では間に合わない。
#   遅寄りは実測で 09:06 までに93%(§18.44)なので 09:10 で十分。
_POLL_CAP = "09:10"
if args.execute and str(args.poll_until) > _POLL_CAP:
    print(f"[jorder] ポーリング締切を {args.poll_until} → **{_POLL_CAP}** に"
          f"早めます。\n"
          f"  理由: トークンは1つなので、このスクリプトが終わるまで watcher を"
          f"起動できません。\n"
          f"  09:30 まで回すと 09:00 の建玉が30分無防備になり、"
          f"delay{args.stop_delay_bars}(={args.stop_delay_bars * 5}分後=09:"
          f"{args.stop_delay_bars * 5:02d} に武装)に間に合いません。\n"
          f"  遅寄りは実測で09:06までに93%(§18.44)なので {_POLL_CAP} で足ります。"
          f"\n  変えるなら --poll-until を明示してください（自己責任）",
          flush=True)
    args.poll_until = _POLL_CAP

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


# ⛔ f-string の式に **バックスラッシュを入れられない**(Python 3.11)。
#   `.\watch` のような文字列は必ず先に作ってから埋め込む。
_HDR_MODE = ("🚀 手順5で **実発注します**(それ以外は照会のみ)" if args.execute
             else "⛔ 照会のみ。発注しません。")
_HDR_NOTE = ("→ 終わったら **すぐ watcher を起動**(--stop-delay-bars "
             f"{args.stop_delay_bars})。" if args.execute
             else "→ `.\\watch` も発注も不要。"
                  "トークンの取り合いが起きないので測定に集中できます。")
_TAIL_LBL = "J発注" if args.execute else "K記録"
_TAIL_NOTE = ("★ ordered / order_limit 列に実発注が残ります(`.\\fills` で突合)"
              if args.execute else "⛔ 発注していません。記録だけです")

print(f"""
{'=' * 74}
■ 朝の測定 — {_dt.date.today()} {'(dry-run)' if args.dry_run else ''}
{'=' * 74}
  接続先: {'★本番(18080)' if args.prod else 'デモ(18081)'}
  {_HDR_MODE}
  ★ 現行Hは実行しません(2026-08-16 決定)。**J/K のデータ取得に専念**します。
    {_HDR_NOTE}

  手順:
    0. シグナル収集 kabu不要      {'(スキップ)' if args.no_kpaper else ''}
    1. バッチ回し   --rotate {args.rotate}      {'' if (args.with_rotate and not args.skip_rotate) else '(スキップ: 結論済み / --with-rotate で有効)'}
    2. 気配ログ     {args.preopen_from}〜{args.preopen_until}     {'(スキップ)' if args.skip_preopen else ''}
    3+4. 対照測定   {args.warmup_at}/{args.open_at}   {'' if (args.with_board_speed or args.no_kpaper) else '(スキップ: --with-board-speed で有効)'}
    5. {'🚀 J実発注' if args.execute else 'K記録   '}    {args.k_warm_at} 空読み → {args.open_at} 本番 → {args.poll_until} まで {'(スキップ)' if args.no_kpaper else ''}
    6. 決済watcher  {'自動起動 → 15:30 まで' if (args.execute and not args.no_watch) else '(起動しません)'}

  ⛔ K の候補は数百銘柄(=6バッチ級)。**コールドだと1バッチ48秒**なので、
     09:00 に間に合わせるには {args.k_warm_at} には空読みを始める必要があります。
     だから気配ログを {args.preopen_until} で切っています(トークンは1つ)。
""", flush=True)

_res: list[tuple] = []

# ── 0. 今日のシグナル収集 (kabu を使わない / 一番先にやる) ──────────────────
# ⛔⛔ 候補は **WATCHLIST 全部ではなく「今日シグナルが出た銘柄」**。
#    yfinance のバックテストを回すので数分かかるが、kabu を一切触らないので
#    他の測定と競合しない。**09:00より前に終わらせる必要がある**ので最初に置く。
# ★ 既に今日ぶんが出来ていれば飛ばす (2026-08-17)。--collect は yfinance で
#   8,898ペアを回すので5〜10分かかる。朝の残り時間を食うので、手で先に
#   走らせてあるなら再実行しない。作り直したいときは --force-collect。
# ★ まず、手元に残っている過去の朝を保存し直す (2026-08-18)。
#   k_paper_*.csv がまだ消えていない日で、未保存のぶんだけ作る。上書きはしない。
#   これを **先頭**でやるのは、この後の --collect が k_signals_<今日>.csv を
#   上書きしうるため(今日を先に固めておく必要はないが、昨日以前の取りこぼしを
#   ここで拾える)。kabu を触らないので他の測定と競合しない。
if not args.dry_run:
    try:
        import k_morning_archive as _kma0
        _n_bf = _kma0.backfill(quiet=False)
        if _n_bf:
            print(f"[k_morning] 未保存だった朝 {_n_bf}日ぶんを保存しました",
                  flush=True)
    except Exception as _bfe:
        print(f"  ⚠ 朝の記録の保存に失敗({_bfe})", flush=True)

_sig_today = Path(f"k_signals_{_dt.date.today():%Y%m%d}.csv")
if not args.no_kpaper and _sig_today.exists() and not args.force_collect:
    print(f"\n[手順0] {_sig_today} が既にあるのでスキップします"
          f"(作り直すなら --force-collect)", flush=True)
    _res.append(("0. シグナル収集", True))
elif not args.no_kpaper:
    _cmd0 = _PY + ["k_open_confirm.py", "--collect"]
    if args.symbols_file:
        _cmd0 += ["--symbols-file", args.symbols_file]
    _ok0 = _run("0. 今日のシグナル収集 (k_signals_<日付>.csv / kabu 不要・数分かかる)",
                _cmd0)
    _res.append(("0. シグナル収集", _ok0))
    # ⛔ 実発注のときは、シグナルが作れていないのに先へ進まない。
    #   代用(WATCHLIST)で09:00を迎えると、今日シグナルの出ていない銘柄を
    #   読みにいくことになる。k_open_confirm 側でも止めるが、ここで気配ログの
    #   45分を無駄にしないよう先に落とす。
    if args.execute and not args.dry_run and not (
            _ok0 and _sig_today.exists()):
        sys.exit("\n⛔ 手順0(シグナル収集)に失敗しました。実発注は中止します。\n"
                 "   `python k_open_confirm.py --collect` を手で走らせて"
                 "原因を確認してください。")

# ── 1. バッチ回し (一番先。登録/解除を繰り返すので後続に429を残さない) ──────
if args.with_rotate and not args.skip_rotate:
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
    # ⛔ 直前スイープ(--final-from)は締切より前でないと一度も走らない。
    #    log_preopen_board の既定は 08:50 だが、K のウォームアップを
    #    08:47 に始めるため締切を 08:45 に前倒ししている。締切の8分前に合わせる。
    if not args.dry_run:
        _wait_until(_hm(args.preopen_from),
                    "寄り前の気配が出るのはこの頃から。早く回しても無駄打ちになる")
    _fin = (_hm(args.preopen_until) - _dt.timedelta(minutes=8)).strftime("%H:%M")
    _cmd = (_PY + ["log_preopen_board.py"] + _P
            + ["--until", args.preopen_until, "--every", "120",
               "--final-from", _fin])
    if args.symbols_file:
        _cmd += ["--symbols-file", args.symbols_file]
    _res.append(("2. 気配ログ", _run(
        f"2. 気配ログ (〜{args.preopen_until} / 初日)"
        f" ★母集団は広く取る。締切まで回し続けます", _cmd)))

# ── 3. ウォームアップ ──────────────────────────────────────────────────
# ⛔ K記録(手順5)と **同時に走らせられない**(トークンは1つ)。K の候補は
#    数百銘柄あり自前で空読み→09:00 に読むので、41銘柄の対照測定を挟むと
#    K のウォームアップ時間を奪う。既定では K を優先し、対照が要るときだけ
#    --with-board-speed を付ける(2026-08-17)。
if not args.no_open and (args.with_board_speed or args.no_kpaper):
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
#   ⛔ 発注は --execute のときだけ。既定は記録のみ。
#   4 の直後に走らせる(登録がまだウォームなので速い)。
if not args.no_kpaper:
    # ★★ ポーリング版 (2026-08-16)。09:00 以降に寄る銘柄も拾い、
    #   **寄り時刻の実データ**を貯める。これが「即時」(§18.41)の検証に直結。
    #   ⛔ 発注は --execute のときだけ(既定は記録のみ)。
    # ⛔ `--now` を付けない。付けると 08:47 に起動した瞬間に本番読みを始めて
    #    しまう。k_open_confirm 自身が --warm-at で空読み → --open-at まで
    #    待つので、**09:00 前に起動して待たせる**のが正しい(2026-08-17)。
    _cmd5 = (_PY + ["k_open_confirm.py"] + _P
             + ["--poll", "--poll-until", args.poll_until,
                "--warm-at", args.k_warm_at, "--open-at", args.open_at,
                "--every", str(args.poll_every), "--now-polls", "999"])
    if args.symbols_file:
        _cmd5 += ["--symbols-file", args.symbols_file]
    # ★★ 実発注 (2026-08-17)。--execute のときだけ。既定は記録のみ。
    #   少額から始める。--budget も --max-notional も万円。
    if args.execute:
        _cmd5 += ["--execute", "--budget", f"{args.budget:g}",
                  "--max-notional",
                  f"{(args.max_notional or args.budget):g}"]
    _res.append((("5. J発注" if args.execute else "5. K記録"), _run(
        f"5. {'J の実発注' if args.execute else 'K のペーパー記録'} — "
        f"{args.k_warm_at} に空読み → {args.open_at} に本番 → "
        f"{args.poll_every}秒ごとに{args.poll_until}までポーリング"
        + (f" / 🚀 **実発注** 予算{args.budget:g}万 "
           f"(上限{(args.max_notional or args.budget):g}万)"
           if args.execute else " (k_paper_<日付>.csv / ⛔発注しない)"),
        _cmd5)))

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
""" + (f"""  🚀 **J を実発注しました**（予算 {args.budget:g}万）。

  ▶▶ **いますぐ これを起動してください**（起動しないと引けまで持ちっぱなし）:

      python lss_exit_watcher.py --execute {'--prod ' if args.prod else ''}--all-dates --stop-delay-bars {args.stop_delay_bars} --entry-cutoff {args.entry_cutoff}

    ⛔ J は delay{args.stop_delay_bars}。バックテストとライブを必ず揃える(§18.9)。
    ⛔ 15:30(大引け)まで止めないこと。途中で切ると決済を取りこぼします(§18.4)。

  ▶ 引け後: `.\\fills` で実約定と突合 → **実滑りがここで初めて測れます**。
""" if args.execute else f"""  ★ **発注していません**（記録のみ）。出すなら `.\\jorder`。
    `.\\watch` も不要です(建玉が無いので監視対象がない)。""") + f"""

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
       ★ **締切の8分前から直前スイープ**(間隔なしで回し続け、先頭=候補に戻る)。
         気配は寄りに近いほど当たるので、いちばん知りたい銘柄がいちばん
         新しい気配で上書きされる。全行に `to_open_s`(寄りまでの残り秒)。
       ⛔ **1,000件 貯まるまで --verify を覗かないこと**(実質 in-sample になる)。
    5(本番読み) … ★ここが最重要。09:00 の1周が **何秒で終わるか**。
       候補299銘柄=6バッチをウォームで読めるなら 6.5件/秒 ≒ 46秒の見込み。
       数分かかるなら K は成立しない(5分遅れで優位が半減)。
    5. {_TAIL_LBL}     … k_paper_<日付>.csv。見るのは3つ:
       ①**09:00に未寄の割合**(BTの実測15.7%と合うか)
       ②**グループの一覧**(何時に何件寄ったか) ← 「即時」(§18.41)の実データ
       ③**1周の読込秒数**が間隔(10秒)に収まっているか
       {_TAIL_NOTE}
""")

# ══════════════════════════════════════════════════════════════════════
#  6. 決済 watcher (--execute のときだけ / 2026-08-17 ユーザー依頼)
# ══════════════════════════════════════════════════════════════════════
# ⛔ ここで **必ず** 起動する。起動しないと建玉は引けまで無防備。
#   トークンは1つなので、手順5(k_open_confirm)が完全に終わってから。
#   k_open_confirm は最後に unregister_all() してから抜けるので安全。
# ⚠ このプロセスは **15:30 まで動き続ける**。ターミナルは占有される。
#   途中で閉じると決済を取りこぼす(§18.4)。
# ⚠ 手順5 が失敗していても起動する。一部だけ約定している可能性があるため。
if args.execute and not args.no_watch:
    _n_today = 0
    try:
        import csv as _csv6
        _p6 = Path("ordered_signals_lss.csv")
        if _p6.exists():
            _today6 = f"{_dt.date.today()}"
            _n_today = sum(
                1 for r in _csv6.DictReader(open(_p6, encoding="utf-8"))
                if str(r.get("record_date") or "")[:10] == _today6)
    except Exception as _e6:
        print(f"  ⚠ ordered_signals_lss.csv を読めません({_e6})", flush=True)
    # ★ --entry-cutoff は **保険**。J の保護指値は寄りで刺さらないと板に残り、
    #   昼に値が戻ったところで約定しうる(モデルに無い時刻・値段の建玉になる)。
    #   k_open_confirm が発注ループの後に自分で取り消すが、そこで落ちた場合に
    #   残ってしまう。watcher 側でも未発動(CumQty==0)の lss 新規売りを掃く。
    #   09:15 = ポーリング締切(09:10)の直後。それ以前に掃くと、まだ約定判定が
    #   終わっていない注文を消しかねない。
    _wcmd = (_PY + ["lss_exit_watcher.py", "--execute"] + _P
             + ["--all-dates", "--stop-delay-bars", str(args.stop_delay_bars),
                "--entry-cutoff", args.entry_cutoff])
    print(f"""
{'=' * 74}
▶ 6. 決済 watcher を起動します（J = delay{args.stop_delay_bars}）
{'=' * 74}
  今日の発注記録: {_n_today}件 (ordered_signals_lss.csv)
  $ {' '.join(_wcmd[1:])}

  ⛔ **15:30(大引け)まで閉じないこと**。途中で切ると決済を取りこぼします(§18.4)。
  ⛔ この間は発注サーバ / 別の kabu スクリプトを起動しないこと(トークンは1つ)。
  ★ 損切りは約定の{args.stop_delay_bars * 5}分後に武装します(delay{args.stop_delay_bars})。
    それまでは利確と引け決済だけが有効です(設計どおり)。
  ▶ 引け後: `.\\fills` で実約定と突合 → **実滑りがここで初めて測れます**
{'=' * 74}
""", flush=True)
    if _n_today == 0:
        print("  ⚠ 今日の発注記録が **0件** です。合格銘柄が無かったか、"
              "発注が中止されています。\n"
              "    建玉が無ければ watcher は何もしません(起動しても無害)。",
              flush=True)
    if not args.dry_run:
        # ⛔⛔ **死んだら再起動する**(2026-08-19)。
        #   2026-08-19、watcher が naive/aware の比較1つで起動2秒で落ち、
        #   ここが一発起動だったので誰も気づかず **8建玉が終日ノーガード＋
        #   持ち越し**になった。損切りも利確も一度も置かれていない。
        #   決済プロセスのバグは必ずいつか出るので、
        #   「1回起動して終わり」という構造そのものを直す。
        _MKT_END = _dt.time(15, 30)
        _tries = 0
        while True:
            _tries += 1
            _run(f"6. 決済watcher{'' if _tries == 1 else f' (再起動 {_tries - 1}回目)'}",
                 _wcmd)
            if _dt.datetime.now().time() >= _MKT_END:
                break            # 大引けまで生き延びた = 正常終了
            if _n_today == 0:
                break            # 建玉が無いので再起動する意味がない
            if _tries >= 40:
                print("  ⛔⛔ watcher が 40回 落ちました。**手で対処してください**",
                      flush=True)
                break
            print(f"\n  ⛔⛔ **watcher が大引け前に終了しました** "
                  f"({_dt.datetime.now():%H:%M:%S})。\n"
                  f"     建玉 {_n_today}件 が無防備です。10秒後に再起動します。\n"
                  f"     ⚠ 何度も出るなら kabuステーションで建玉を確認し、"
                  f"手で返済してください。", flush=True)
            time.sleep(10)
        # 大引け後: 建玉が残っていないかを最後に必ず知らせる
        if _n_today:
            print(f"""
{'=' * 74}
▶ 決済の確認 — **必ず kabuステーションで建玉照会を見てください**
{'=' * 74}
  今日の発注 {_n_today}件。引け成行(MOC)が通っていれば建玉は 0 のはずです。
  ⛔ 残っていたら **一般信用デイトレは当日返済が原則**です。
     翌営業日の寄成で返済を予約するか、その場で返済してください。
{'=' * 74}""", flush=True)
    else:
        print("  (dry-run: 起動しません)", flush=True)
elif args.execute:
    print(f"""
  ⚠ --no-watch なので watcher を起動していません。**手で起動してください**:
      python lss_exit_watcher.py --execute {'--prod ' if args.prod else ''}--all-dates --stop-delay-bars {args.stop_delay_bars} --entry-cutoff {args.entry_cutoff}
""", flush=True)
