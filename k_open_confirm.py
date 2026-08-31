r"""k_open_confirm.py — J(09:00確認方式)の記録 と 実発注

⛔ **既定は記録だけ**。`--execute` を付けない限り1件も発注しません
   (kabu_api を dry_run で作るので、発注系は内容を print するだけ)。
   本番口座に出すには `--execute --prod` の **両方**が必要です。

■ 何をするか

    08:5x  候補を50件バッチで登録し、**1回空読み**してウォームにする
    09:00〜 --poll で回し続け、**寄った銘柄から順に**始値を取る
           → 前日終値比のギャップが +50bp 以上なら合格
           → そのグループに配って(第1グループ=予算×g1 / 以降=残り予算)
           → --execute なら **保護指値売り @ 始値×(1-50bp)** で即発注
           → k_paper_<日付>.csv に全部書く
    09:1x  **未約定の指値を取り消す**(板に残ると昼に約定してしまう)
           **武装時刻**(.lss_watcher_seen.json)を寄り時刻で固定して終了

  ★ 全部が寄るのを待ちません。1分待つと -15.8bp 逃げます(§18.44)。

■ 発注の作り (§18.37 改訂 / §18.38)

  ・注文は **指値売り**。⛔ 成行にしない — 板が飛んだときに掴まされる。
    指値売りは「指値以上」で約定するので、板が正常なら成行と同じ値で即約定し、
    急落しているときだけ約定しない。
  ・決済は `lss_exit_watcher` が **実約定価格基準**で OCO を組み直す。
    そのため ordered_signals_lss.csv に atr / sm / tm を書く
    (entry_mode="auction" = 実約定価格基準で再計算する印)。
  ・⚠ watcher は **--stop-delay-bars 4** で起動すること(J は delay4)。
    バックテストとライブを必ず揃える(§18.9 の鉄則)。

■ 少額で試すとき

    python k_open_confirm.py --prod --poll --execute --budget 60 --max-notional 60

  --budget = 配分に使う予算 / --max-notional = 発注総額のハード上限(別枠)。
  どちらも万円。超えたらそこで発注を止めます。

■ 母集団は **J に揃える** (2026-08-17 変更)

  --collect の収集元は **holdout_selected_symbols.py**(= `.\daily` が書く
  レポートの発注リストそのもの。cumul に 価格帯 + 空売り可 を掛けたもの)。
  そこから **流動性上位50件**(--watch-j)だけを 09:00 に読む。
  これで J タブ(cumul × watch50)と **母集団も切り方も一致**する。
  ⛔ --execute のときは --max-symbols を 50 に強制する。絞らないと候補全部
    (中央154銘柄)から建てることになり、バックテストと別物になる。

  ⛔ それまでは full(選定なし)を読んでいたが、K は §18.45 で棄却済み
    (kabu の登録上限50件は REST でも WebSocket でも同じ / 299銘柄の読取に
     3.6分かかり、その間の減衰 -30bp が利得 +35,258円/月 を食い潰す)。
    full を読むと 299銘柄のうち50件しか読めず、**その50件が J の候補とは
    限らない**。実測 2026-08-17:
      朝の合格 5件 = 1515 / 1762 / 2270 / 2432 / 3105  (full から)
      J タブ   4件 = 1762 / 3864 / 4776 / 5632         (cumul から)
      重なりは **1762 だけ**。同じ日の記録なのに突合できなかった。

  CSV には in_j / rank_liq が残るので後から再集計できる。
  ⚠ in_j は **ペア単位(銘柄×戦略)** で判定する。「その銘柄が cumul にあるか」
    ではない。cumul に A7 だけ載っている銘柄が MACDTF でシグナルを出しても
    J は建てないので、銘柄単位だと過大評価になる(2026-08-17 に 7936 で発覚)。

■ 使い方

    python k_open_confirm.py --collect                    # 前夜/早朝: 今日の候補を作る
    python k_open_confirm.py --prod --poll                 # 記録のみ(発注しない)
    python k_open_confirm.py --prod --poll --execute \
           --budget 50 --max-notional 50                  # ★少額で実発注
    python k_open_confirm.py --prod --poll --now --now-polls 1   # 動作確認(場中でも可)

■ ⚠ 注意

  ・kabu の有効トークンは1つ。watcher / 発注サーバと同時に走らせない。
  ・登録上限50件。候補が多い日はバッチで回す(--batch)。
    ⛔ バッチ回しが**毎回コールド**なら 09:00 には間に合わない。
       成立するかは `.\mtest` の 1(--rotate) で確かめる。
  ・**09:00 に寄らない銘柄がある**(実測15.7%。09:02〜09:06 に集中)。
    OpeningPrice が空 / OpeningPriceTime が 09:00 より後 の銘柄は
    late=1 で記録し、合格判定から外す(現行のバックテストと同じ扱い)。
    段階発注を採るならここを後から拾い直せる。
"""
from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
import re
import sys
import time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ap = argparse.ArgumentParser(
    description="J(09:00確認方式)の記録と実発注。⛔ --execute を付けない限り発注しない")
ap.add_argument("--prod", action="store_true", help="本番(18080)。既定はデモ(18081)")
ap.add_argument("--symbols-file", type=str, default="",
                help="候補のソース(既定 holdout_selected_symbols.py)")
ap.add_argument("--symbols", type=str, default="", help="カンマ区切りで明示指定")
ap.add_argument("--pool", type=str, default="lss_proposal_cumul.py",
                help="J(実装版)の母集団。in_j 列の判定に使う")
# ⛔ 既定300で **黙って切っていた**。候補は日によって変わるので、上限に
#    当たったことに気づけないとデータが欠ける(2026-08-17: 299銘柄で紙一重)。
#    0=無制限を既定にし、切るときは必ず警告を出す。
ap.add_argument("--max-symbols", type=int, default=0,
                help="読む銘柄数の上限。**0=無制限**(既定)")
ap.add_argument("--batch", type=int, default=50, help="1バッチ(kabu の登録上限)")
ap.add_argument("--workers", type=int, default=2,
                help="⛔ 上げても速くならず429が増えるだけ(実測)")
# ★★ 合格とするギャップ (2026-08-22: +50 → **+75bp**)。
#   レポートの監査ボードで、予算400万/300万の **両方** で 月平均÷σ の頂点。
#   7点(0/25/50/75/100/125/150)の単峰で +125/+150 は σ が増えて崩れる。
#     400万: 4.23(+50) → 5.04(+75) → 4.38(+100)   σ −4%
#     300万: 3.84(+50) → 5.22(+75) → 4.76(+100)   σ −19%
#   ★ **σ が下がるのは +75 だけ**。取引を21%減らして質を上げる形。
#   ⚠ 1日の建玉は 約7件 → 約5.5件 に減る。1銘柄の集中(95%点)は 23%→35%
#     に上がるが、**最大は上限50%のまま**(テールは変わらない)。
#   ⛔ レポート(LSS_EQ_GAP_BP)と必ず揃えること(§18.9)。戻すなら --gap-bp 50
ap.add_argument("--gap-bp", type=float, default=75.0,
                help="合格とするギャップ(bp)。既定75 (2026-08-22 に 50 から変更)")
ap.add_argument("--guard-bp", type=float, default=300.0,
                # ⛔ argparse の help は % 書式として展開される。Python 3.14 は
                #    add_argument の時点で検証するので、生の % があると
                #    ValueError: badly formed help string で **起動すらしない**。
                #    リテラルの % は必ず %% と書くこと(2026-08-16 に実際に落ちた)。
                help="これを超えるギャップは見送り(現行の±3%%ガード)")
# ★★ 実発注 (2026-08-17)。既定OFF。付けない限り1件も発注しない。
ap.add_argument("--execute", action="store_true",
                help="⚠ **実発注する**。既定は記録のみ。--prod を付けない限りデモ口座")
ap.add_argument("--limit-slip-bp", type=float, default=50.0,
                help="保護指値の下げ幅(bp)。始値×(1-これ) で売る。"
                     "成行にしないのは板が飛んだときに掴まされないため(18.38)")
ap.add_argument("--max-notional", type=float, default=0.0,
                help="発注総額の上限(万円)。0=--budget と同じ。"
                     "**予算とは別のハード上限**で、超えたらそこで発注を止める")
ap.add_argument("--margin-type", type=int, default=3,
                help="信用区分 3=一般デイトレ(既定) / 1=制度。"
                     "⛔ 制度(1)は空売りが貸借銘柄限定なので、非貸借銘柄も売る "
                     "J では 3 でないと MarginTradeType 不正で弾かれる")
ap.add_argument("--budget", type=float, default=400.0, help="予算(万円)")
# ★★ 1銘柄の金額上限 = **予算の50%** (2026-08-21 ユーザー承認)。
#   旧既定は 50万 固定(2026-08-15)。予算400万での比較で
#     上限50万  月平均/σ 2.76 (σ 165,068)
#     上限200万 月平均/σ 4.23 (σ 132,749)  ← 予算の50%
#     上限なし  月平均/σ 4.23 (σ 139,780)  ← 1銘柄に予算の99%が入りうる
#   上限なしと 200万 はリスク調整後で同値(差は月2.9万=0.2σ)。集中度が半分の
#   200万を採る。**比率で持つ**ので予算を変えても勝手にズレない(300万→150万)。
#   ⚠ 効くのは『合格が少ない日』だけ。7〜8件の日は 予算÷件数 が上限に届かない。
#   ⛔ §18.9 の鉄則: レポート側(LSS_EQ_MAX_YEN)と必ず揃えること。
ap.add_argument("--max-yen", type=float, default=0.0,
                help="1銘柄の上限(万円)。**0=予算の --max-yen-pct%%**(既定)")
ap.add_argument("--max-yen-pct", type=float, default=50.0,
                help="--max-yen が0のとき、予算の何%%を1銘柄の上限にするか")
ap.add_argument("--max-lot", type=int, default=10, help="1銘柄の最大単元")
ap.add_argument("--watch-j", type=int, default=50, help="J が09:00に読める件数")
ap.add_argument("--open-at", type=str, default="09:00")
ap.add_argument("--warm-at", type=str, default="08:55")
ap.add_argument("--now", action="store_true", help="待たずに いま1回読む")
# ★★ ポーリング (2026-08-16 ユーザー提案)
ap.add_argument("--poll", action="store_true",
                help="09:00 以降も回し続け、**寄った銘柄から順に**拾う")
ap.add_argument("--poll-until", type=str, default="09:30",
                help="--poll の締切(実測: 遅寄りの93%%が09:06までに寄る)")
ap.add_argument("--every", type=int, default=10, help="--poll の間隔(秒)")
ap.add_argument("--now-polls", type=int, default=3,
                help="--now --poll のとき何周だけ回すか(動作確認用)")
ap.add_argument("--collect", action="store_true",
                help="⛔ kabu を使わず、**今日のシグナルだけ**を収集して "
                     "k_signals_<日付>.csv に書き出す(09:00より前に走らせる)")
ap.add_argument("--signals-csv", type=str, default="",
                help="--collect の出力/入力(既定 k_signals_<日付>.csv)")
ap.add_argument("--sm", type=float, default=0.5, help="--collect の損切ATR")
ap.add_argument("--tm", type=float, default=1.0, help="--collect の利確ATR")
ap.add_argument("--days", type=int, default=365, help="--collect の窓")
# ★ レポート(dailyfast.bat の --min-price 1000 --price-ranges 6000)と揃える。
#   提案ファイルはフィルタ前なので、ここで落とさないと J/L/K と母集団が違う。
ap.add_argument("--min-price", type=float, default=1000.0,
                help="--collect の価格下限(注文値で判定)")
ap.add_argument("--max-price", type=float, default=6000.0,
                help="--collect の価格上限(注文値で判定)")
# ⛔⛔ **既定を 1.0 に変更**(2026-08-20)。
#   レポートの推奨変種(H寄り確認+50bpd4sm0.5資金均等)は **波なし** =
#   `予算 ÷ その日の件数` を 09:00 に全額配る。遅寄りは捨てる設計。
#   ところがライブの既定は 0.8 で、**09:00 に予算の80%しか使っていなかった**。
#   残り20%は遅寄り銘柄用だが、流動性上位50件の遅寄りは実測 2%(§18.44)
#   しかないので、ほとんどが**使われないまま終わる**。
#   = ライブはモデルの 0.8倍しか建てていない。§18.9 の鉄則(バックテストと
#     ライブを必ず揃える)に反していた。
#   段階配分を試したいときだけ 0.7〜0.9 を明示する(レポートの G1_70/80/90 と対)。
ap.add_argument("--g1", type=float, default=1.0,
                help="第1グループ(09:00の板寄せ)に配る予算の割合(既定1.0"
                     "=レポートの推奨変種と一致)。以降のグループは残り予算。"
                     "⛔端数配分はしない")
ap.add_argument("--out", type=str, default="")
# ⛔⛔ 時間外の実発注ガード (2026-08-18)。
#   2026-08-18 15:30 に `.\jorder` が誤って再実行され、**実発注を4件試みた**。
#   /board は引け後も当日の OpeningPrice を返し続けるので、判定はそのまま通ってしまう。
#   救われたのは kabu が ExpireDay=0 を『正しい有効期限を設定してください』
#   (Code 5) で弾いたからで、**ザラ場中に再実行していたら通っていた**。
#   保護指値は 始値×0.995 なので、14:00 に出せば現値より遥か下 = 即約定し、
#   モデルに無い建玉ができる。
ap.add_argument("--allow-late-orders", action="store_true",
                help="⛔ 時間外でも発注する。事故のもとなので通常は使わない")
# ★★ 終了直前に引け成行(MOC)を板へ置く (2026-08-20)。既定ON。
#   09:00 に建てた玉は、このスクリプトが終わる 09:10 まで板に何も乗らない
#   (トークンが1つなので watcher を並走できない)。2026-08-19 はその直後に
#   watcher が即死して6時間半 無防備 → 持ち越し → 強制決済になった。
#   MOC は朝に出しても大引けで約定するので、**手放す前に置く**のが正しい。
ap.add_argument("--no-moc-on-exit", action="store_true",
                help="⛔ 終了前に引け成行(MOC)を置かない。"
                     "置かないと watcher が起動するまで板が空になる")
# ★★ 新方式N として発注する (2026-08-31)。既定OFF = 従来どおり J。
#   N は §18.54/§18.55 で確定した仕様で、J と4点違う:
#     ① 候補     n_signals_<日付>.csv (前日リターン ≥ +1.753% / 売買代金上位50)
#     ② 合格     ギャップ ≥ +100bp    (J は +75bp)
#     ③ 株数     **100株固定**        (J は資金均等 / §18.58 ③・§18.36 で確定)
#     ④ 決済     **引けMOC だけ**。損切りも利確も置かない (§18.55)
#   ⛔ ④の帰結として watcher が要らない。J では「MOC が建玉を拘束して損切りを
#     置けない」排他があったが、N は損切りを置かないので衝突が消える。
#   ⛔ そして _verify の ATR チェックは N では**必ず落ちる**(バリアが無いので
#     ATR を持たない)。2026-08-31 の記録実行で合格2件が両方止まった。
# ⛔ 既定 0 = **指値は始値ちょうど**(2026-08-31 レビュー④の指摘を受けて)。
#   ・N に残るのは +21.9bp。往復の執行許容は 7.5bp。10bp でも広すぎる
#   ・そして ticks.json が無い環境では floor_to_tick_sym が**粗いテーブルに
#     フォールバック**するので、1ティックが 30〜60bp になりうる
#   ・バックテストは「始値で建てる」前提なので、始値ちょうどが最も忠実
#   ・約定しなければその銘柄を建てないだけで、損はしない
#   下げたいときだけ --n-limit-ticks 1 のように明示する。
ap.add_argument("--n-limit-ticks", type=int, default=0,
                help="N の保護指値を始値から何ティック下に置くか"
                     "(既定0 = 始値ちょうど)。⛔ bp で固定しない —"
                     " 呼値は銘柄区分で変わる(§18.31)")
ap.add_argument("--n-limit-max-bp", type=float, default=7.5,
                help="N の保護指値の許容幅の上限(bp・既定7.5 = 往復の執行許容)。"
                     "ティックが粗い銘柄で下げすぎないための頭打ち")
ap.add_argument("--n-ignore-stuck", action="store_true",
                help="⛔ 前回の未決済が残っていても起動する。手動で確認してから使うこと")
ap.add_argument("--n-mode", action="store_true",
                help="新方式N として発注する(§18.54)。候補=n_signals_<日付>.csv"
                     " / +100bp / 100株固定 / バリアなし(引けMOCだけ) /"
                     " watcher 不要")
args = ap.parse_args()

# ── N の既定値 ────────────────────────────────────────────────────────
#   ⛔ J の既定と違う値だけを差し替える。**明示指定はそのまま尊重する**ため、
#     「J の既定のままなら N の既定にする」という形にしている。
if args.n_mode:
    _nd: list = []
    if args.gap_bp == 75.0:
        args.gap_bp = 100.0
        _nd.append("--gap-bp 100")
    if args.poll_until == "09:30":
        # ⛔ ポーリングを切ると、その直後に引けMOC が置かれる(下の
        #   no_moc_on_exit ブロック)。N は watcher が無いので、
        #   **建ててから MOC までの無防備な時間**をここで決めている。
        #   09:10 = 遅寄りの93%が寄り終わる(§18.44)一方で、無防備は最大10分。
        args.poll_until = "09:10"
        _nd.append("--poll-until 09:10")
    if args.max_lot == 10:
        args.max_lot = 1                     # 100株固定(§18.58 ③)
        _nd.append("--max-lot 1")
    print("[N] 新方式N として発注します(§18.54)。"
          "候補=n_signals / +100bp / 100株固定 / バリアなし(引けMOCだけ)")
    if _nd:
        print(f"[N] J の既定から差し替え: {' / '.join(_nd)}")
    print("[N] ⛔ watcher は起動しません。決済は引け成行(MOC)だけです")


# ★ 1銘柄の上限を **比率**から解決する (2026-08-21)。
#   0 = 予算 × --max-yen-pct%。明示した値があればそれを使う。
#   ⛔ ここで args.max_yen を確定させるので、下流(_size_groups / 表示 / 検算)は
#     従来どおり args.max_yen を読むだけでよい。分散させない。
if args.max_yen <= 0 and args.max_yen_pct > 0:
    args.max_yen = round(args.budget * args.max_yen_pct / 100.0, 1)
    print(f"[上限] 1銘柄の上限を **予算{args.budget:g}万 × "
          f"{args.max_yen_pct:g}% = {args.max_yen:g}万** にしました"
          f"(--max-yen で固定値を明示できます)", flush=True)

# ── 実発注は 09:00 前後の窓の中だけ ───────────────────────────────────
#   窓 = [--open-at の20分前, --poll-until の20分後]。外なら **起動時に落とす**
#   (読み終わってから気づくのでは遅い)。記録だけ取りたいなら --execute を外す。
if args.execute and not args.allow_late_orders:
    def _hm2m(_s: str) -> int:
        _h, _m = (int(x) for x in str(_s).split(":"))
        return _h * 60 + _m
    _now_m = _dt.datetime.now().hour * 60 + _dt.datetime.now().minute
    _lo, _hi = _hm2m(args.open_at) - 20, _hm2m(args.poll_until) + 20
    if not (_lo <= _now_m <= _hi):
        sys.exit(
            f"[error] いま {_dt.datetime.now():%H:%M} は発注の窓の外です"
            f"（{_lo // 60:02d}:{_lo % 60:02d}〜{_hi // 60:02d}:{_hi % 60:02d}）。\n"
            f"  ⛔ **実発注しません**。/board は引け後も当日の始値を返し続けるので、\n"
            f"     時間外に走らせると『朝と同じ判定』で注文が飛びます"
            f"（2026-08-18 15:30 に実際に4件試みた）。\n"
            f"  ・記録だけ取り直す → --execute を外してください\n"
            f"  ・それでも出す     → --allow-late-orders（自己責任）")

_COLS = ["date", "seen_ts", "grp", "symbol", "in_j", "rank_liq", "liquidity",
         "prev_close", "open_p", "open_time", "current_price",
         # ⛔⛔ **売りは最良買い気配に当てる**(2026-08-19)。バックテストは
         #   「約定=始値」を仮定しているが、実発注は保護指値 @始値×(1-50bp) を
         #   板にぶつけるので、**その瞬間の最良買い気配**で約定する。
         #   2026-08-19 の実測は8件すべて始値より下(-16〜-47bp / 平均-26.5bp)で、
         #   これは丸ごとバックテストに載っていないコスト。
         #   ただし内訳が「スプレッド(構造的)」なのか「読取〜発注の数秒の値動き」
         #   なのかは、板を残していないと**分けられない**。だから残す。
         "bid", "ask", "bid_qty", "ask_qty",
         "gap_bp",
         "late", "pass_gap", "guard_ng", "stale_open", "lots_k", "yen_k",
         "atr", "stop_k", "target_k",
         # ★ 実発注したか(--execute)。dry-run では 0 のまま。
         "ordered", "order_limit"]


# ── 候補の読み込み ─────────────────────────────────────────────────────
def _codes_from(path: str) -> list[str]:
    _t = Path(path).read_text(encoding="utf-8")
    _c = re.findall(r"""['"](\d{4}[A-Z0-9]?)\.T['"]""", _t)
    if not _c and str(path).lower().endswith(".csv"):
        for r in _csv.DictReader(open(path, encoding="utf-8-sig")):
            _s = str(r.get("symbol") or "").upper().removesuffix(".T")
            if _s:
                _c.append(_s.split(".")[0])
    _seen: set = set()
    return [x for x in _c if not (x in _seen or _seen.add(x))]


_sig_csv = args.signals_csv or (
    f"n_signals_{_dt.date.today():%Y%m%d}.csv" if args.n_mode
    else f"k_signals_{_dt.date.today():%Y%m%d}.csv")
# 銘柄 -> ATR。K の OCO は **実約定価格(始値)** を基準に置くので、
# 損切り/利確は 09:00 に始値が出て初めて確定する。
_ATR: dict = {}
# 銘柄 -> 流動性(直近120日の平均売買代金)。**読む順=発注順**を決める。
# ⛔ ここが空だと --max-symbols が銘柄コード順に切ってしまう(2026-08-17)。
_SIG_LIQ: dict = {}
# 銘柄 -> その銘柄で今日シグナルが出た **戦略の本数**。
# ⛔ バックテストの J は (銘柄×戦略) を1件ずつ数えて `予算 ÷ 合格件数` で配り、
#    そのうえで銘柄ごとに --max-yen で頭を切る(§18.38 #3b)。ライブは銘柄単位に
#    重複排除して1本だけ発注するので、**戦略の本数で割らないと配分がズレる**
#    (7936 のように4戦略出る銘柄で大きく食い違う)。§18.9 の鉄則に従って揃える。
_NPAIR: dict = {}

# ══════════════════════════════════════════════════════════════════════
#  --collect : 今日のシグナルだけを収集する (kabu を使わない)
# ══════════════════════════════════════════════════════════════════════
# ⛔⛔ 候補は **WATCHLIST 全部ではなく「今日シグナルが出た銘柄」**。
#    2026-08-16 に holdout_selected_symbols.py(3,054ペア)を候補にしていて
#    誤りだった。実発注(lss_budget_cap.py)は _lss_signal_today で当日の
#    シグナルを1件ずつ拾うので、**同じ関数を使って揃える**。
#    ⚠ 収集は yfinance のバックテストなので数分かかる。**09:00より前に**
#      走らせること(kabu は一切触らないので他の測定と競合しない)。
if args.collect:
    try:
        from kabu_send_lss import _load_symbols, _lss_signal_today
    except Exception as _e:
        sys.exit(f"[error] kabu_send_lss を読めません: {_e}")
    # ⛔ kabu_send_lss._load_symbols は `lss_watchlist_proposal_*.py`(**旧命名**)
    #    を自動検出する。放っておくと数ヶ月前の古い提案を拾い、レポートの
    #    J/L/K とは **別の母集団** で記録することになる(2026-08-17 に実際に
    #    lss_watchlist_proposal_2026-07-15.py 5,639ペアを拾った)。
    #    レポートの土台(dailyfast.bat が渡す lss_proposal_full.py)に揃える。
    # ★★ 既定は **J の母集団(cumul)** (2026-08-17 変更)。
    #   ⛔ それまでは full(選定なし)を既定にしていたが、K は §18.45 で
    #     「kabu では技術的にも経済的にも成立しない」と棄却済み。full を読むと
    #     299銘柄のうち **50件しか読めず、しかもその50件が J の候補とは限らない**。
    #     実測 2026-08-17:
    #       朝の合格 5件 = 1515 / 1762 / 2270 / 2432 / 3105  (full から)
    #       J タブ   4件 = 1762 / 3864 / 4776 / 5632         (cumul から)
    #       重なりは **1762 だけ**。同じ日の記録なのに突合できなかった。
    #   ★ cumul の流動性上位50件を読めば、J タブ(cumul × watch50)と
    #     **母集団も切り方も一致**する。
    #   K/L を測りたいときだけ --symbols-file lss_proposal_full.py。
    # ★★ 正本は **holdout_selected_symbols.py** (2026-08-17)。
    #   これはレポートが LSS_SIGNAL_POOL(選定あり)+価格+空売り可で絞って
    #   書き出したファイルで、**画面の発注リストと1対1で対応する**。
    #   ライブの発注(kabu_send_lss._load_symbols)も同じものを読む。
    #   ⛔ ここで別のファイル(lss_proposal_cumul.py など)を読むと、価格・
    #     空売り可否のフィルタが掛かっていない 3,508ペアになり、朝の記録と
    #     発注リストが食い違う(2026-08-17: cumul 3,508 vs holdout 3,025)。
    _src = args.symbols_file
    if not _src:
        for _c in ("holdout_selected_symbols.py", args.pool,
                   "lss_proposal_cumul.py", "lss_proposal_full.py"):
            if _c and Path(_c).exists():
                _src = _c
                break
    _pairs = _load_symbols(_src or None)
    # ⛔ 提案ファイルは **フィルタ前**(9,240ペア)。レポートは読み込み時に
    #    ①空売り不可 ②価格帯(1,000〜6,000円) を落として 8,106ペアにしている。
    #    ここで揃えないと、レポートでは建てない銘柄まで 09:00 に読むことになり、
    #    ・kabu の読込時間を無駄に使う(登録上限50件の測定が不正確になる)
    #    ・J/L/K タブと母集団が食い違う
    #    (2026-08-17: 9,240ペア→703シグナル/416銘柄 と出て発覚)
    _ns: set = set()
    try:
        _nsp = Path(__file__).resolve().parent / "not_shortable.py"
        if _nsp.exists():
            _nsns: dict = {}
            exec(_nsp.read_text(encoding="utf-8"), _nsns)
            _ns = {str(x).upper().removesuffix(".T").split(".")[0]
                   for x in _nsns.get("NOT_SHORTABLE", [])}
    except Exception as _e:
        print(f"  ⚠ not_shortable.py 読み込み失敗: {_e} → 除外なしで続行")
    # --symbols を指定したらそこに絞る(動作確認用)。指定しなければ全ペア。
    if args.symbols:
        _pick = {s.strip().upper().removesuffix(".T").split(".")[0]
                 for s in args.symbols.split(",") if s.strip()}
        _pairs = [p for p in _pairs
                  if str(p[0]).upper().removesuffix(".T").split(".")[0] in _pick]
        print(f"[collect] --symbols で {len(_pairs):,}ペアに絞りました", flush=True)
    _n0 = len(_pairs)
    if _ns:
        _pairs = [p for p in _pairs
                  if str(p[0]).upper().removesuffix(".T").split(".")[0] not in _ns]
    print(f"[collect] 母集団: {_src or '(自動検出)'} → {len(_pairs):,}ペア"
          + (f" (空売り不可 {_n0 - len(_pairs):,}除外)" if _n0 != len(_pairs) else "")
          + f" / 価格 {args.min_price:,.0f}〜{args.max_price:,.0f}円"
          f"。今日のシグナルを収集します(kabu は使いません)", flush=True)
    # ★ 正本かどうかを毎朝ここで言う。食い違いは画面では気づけない。
    _CANON = "holdout_selected_symbols.py"
    if Path(_src or "").name == _CANON:
        print(f"  ✅ **レポートの発注リストと同じ母集団**です"
              f"(ライブの kabu_send_lss も同じファイルを読みます)", flush=True)
    else:
        print(f"  ⛔ 正本({_CANON})ではありません。読める{args.batch}件が"
              f"発注リストの上位{args.batch}件と一致しない可能性があります。"
              f"先に `.\\daily` を回して正本を作ってください", flush=True)
    _out: list = []
    _n_px = 0          # 価格帯で落とした件数
    for _i, (_c, _n, _st) in enumerate(_pairs):
        if _i and _i % 500 == 0:
            print(f"  … {_i:,}/{len(_pairs):,} ({len(_out)}件)", flush=True)
        try:
            _sg = _lss_signal_today(_c, _n, _st, args.sm, args.tm, args.days)
        except Exception:
            _sg = None
        if not _sg:
            continue
        # ★ 価格帯フィルタ。レポート側と同じく **注文値**で判定する
        #   (前日終値ではない。100株買えるかは注文値で決まる)。
        _px = float(_sg.get("order_price") or 0)
        if _px > 0 and not (args.min_price <= _px <= args.max_price):
            _n_px += 1
            continue
        _cd = str(_sg.get("symbol") or _c).upper() \
            .removesuffix(".T").split(".")[0]
        # ⛔⛔ liquidity を落としていた(2026-08-17)。これが無いと poll 側で
        #    流動性順に並べられず、--max-symbols が **銘柄コード順**に切ってしまう。
        #    実際 7936 アシックス(+204bp・4戦略)を読まずに取り逃した。
        #    発注順は流動性降順(§18.21)なので、ここは必ず持ち回す。
        _out.append({"symbol": _cd, "name": _n, "strategy": _st,
                     "order_price": _sg.get("order_price", 0),
                     "liquidity": _sg.get("liquidity", 0),
                     "atr": _sg.get("atr", 0)})
    # 重複銘柄は残す(複数戦略で出る)。登録は銘柄単位で重複排除する。
    with open(_sig_csv, "w", newline="", encoding="utf-8-sig") as f:
        # ⛔ liquidity を fieldnames に入れ忘れると DictWriter が
        #   ValueError("dict contains fields not in fieldnames") で落ちる
        #   = 手順0 でクラッシュして朝が止まる(2026-08-17 に実際に踏んだ)。
        w = _csv.DictWriter(f, fieldnames=["symbol", "name", "strategy",
                                           "order_price", "prev_close", "atr",
                                           "liquidity"])
        w.writeheader()
        w.writerows(_out)
    _uq = len({r["symbol"] for r in _out})
    print(f"[collect] シグナル {len(_out):,}件 / **{_uq:,}銘柄** → {_sig_csv}"
          + (f" (価格帯外 {_n_px:,}件を除外)" if _n_px else "")
          + f"\n  ⛔ 発注していません。09:00 の判定はこの銘柄だけを読みます。",
          flush=True)
    if _uq > args.batch:
        # ⚠ これは **J(実装版)** の話。K の記録は _read_all() が
        #   50件バッチで全部読むので、この件数がそのまま対象になる。
        #   むしろ「何件を何秒で読めるか」が明日の測定の本体。
        print(f"  ★ {_uq}銘柄 = {-(-_uq // args.batch)}バッチ。"
              f"K はこれを全部読みます(バッチ回しの実測が本番)。"
              f"J は流動性上位{args.watch_j}件だけを見ます", flush=True)
    sys.exit(0)

# ── 候補の決定 ────────────────────────────────────────────────────────
if args.symbols:
    _syms = [s.strip().upper().removesuffix(".T").split(".")[0]
             for s in args.symbols.split(",") if s.strip()]
elif Path(_sig_csv).exists():
    # ★ --collect が作った「今日のシグナル」を使う(これが正しい候補)
    #   ATR も一緒に持つ。K は **実約定価格(=始値)** を基準に OCO を置くので、
    #   損切り/利確はここで初めて確定する(シグナル時点の逆指値トリガー基準の
    #   stop/target とは別物)。§18.32 の「OCO の基準を実約定価格に変える」。
    _syms = []
    for r in _csv.DictReader(open(_sig_csv, encoding="utf-8-sig")):
        _s0 = str(r.get("symbol") or "").upper().removesuffix(".T").split(".")[0]
        if not _s0:
            continue
        try:
            _a0 = float(r.get("atr") or 0)
        except Exception:
            _a0 = 0.0
        # 同じ銘柄が複数戦略で出る。ATR は銘柄固有なので最初の非ゼロを採る。
        if _a0 > 0 and not _ATR.get(_s0):
            _ATR[_s0] = _a0
        try:
            _l0 = float(r.get("liquidity") or 0)
        except Exception:
            _l0 = 0.0
        if _l0 > 0 and _l0 > _SIG_LIQ.get(_s0, 0):
            _SIG_LIQ[_s0] = _l0
        _NPAIR[_s0] = _NPAIR.get(_s0, 0) + 1
        if _s0 not in _syms:
            _syms.append(_s0)
    print(f"[候補] {_sig_csv} から **今日のシグナル {len(_syms):,}銘柄**",
          flush=True)
else:
    # ⛔⛔ 実発注のときは **代用してはいけない**。WATCHLIST は「今日シグナルが
    #    出た銘柄」ではないので、代用するとシグナルの無い銘柄を建てにいく。
    #    (ATR が無いので _verify が全部止めるはずだが、そこに頼らない)
    if args.execute:
        sys.exit(f"[error] {_sig_csv} がありません。\n"
                 f"  ⛔ --execute では WATCHLIST で代用しません"
                 f"（今日のシグナルではないので、無関係の銘柄を建てます）。\n"
                 f"  先に `python k_open_confirm.py --collect` を"
                 f"09:00より前に走らせてください")
    _p = args.symbols_file or "holdout_selected_symbols.py"
    if not Path(_p).exists():
        sys.exit(f"[error] {_sig_csv} も {_p} もありません。\n"
                 f"  先に `python k_open_confirm.py --collect` を"
                 f"09:00より前に走らせてください")
    print(f"⛔ {_sig_csv} がありません。{_p}(WATCHLIST)で代用しますが、"
          f"**これは今日のシグナルではありません**。\n"
          f"   正しくは先に --collect を走らせること", flush=True)
    _syms = _codes_from(_p)
    if not _syms:
        sys.exit(f"[error] {_p} から銘柄を拾えません(0件)")

# ── 流動性(=読む順=発注順)を確定させる ────────────────────────────────
# ⛔⛔ **--max-symbols で切る前に**並べ替えること。以前は切った後に
#    並べ替えていて意味が無く、実質「銘柄コードの小さい順」で読んでいた。
#    2026-08-17 に 7936 アシックス(+204bp・4戦略)を取り逃して発覚。
_liq: dict = dict(_SIG_LIQ)
if not _liq:
    for _lf in ("lss_trades_K.csv", "lss_trades_H.csv", "lss_trades.csv"):
        if not Path(_lf).exists():
            continue
        try:
            for r in _csv.DictReader(open(_lf, encoding="utf-8-sig")):
                _s = str(r.get("symbol") or "").upper() \
                    .removesuffix(".T").split(".")[0]
                _v = float(r.get("liquidity") or 0)
                if _s and _v > _liq.get(_s, 0):
                    _liq[_s] = _v
        except Exception:
            pass
        if _liq:
            print(f"  [並び] {_lf} の liquidity で代用します"
                  f"(k_signals に列が無い古い版)", flush=True)
            break
if _liq:
    # 流動性が取れない銘柄は最後尾(板の薄さが分からないものを上位に入れない / §18.21)
    _syms.sort(key=lambda s: (-_liq.get(s, 0.0), s))
    print(f"  [並び] **流動性(売買代金)降順**に並べ替えました"
          f"(流動性あり {sum(1 for s in _syms if _liq.get(s, 0) > 0):,}"
          f"/{len(_syms):,}銘柄)", flush=True)
else:
    print(f"  ⛔ liquidity が1件も取れません。**銘柄コード順のまま**読みます。"
          f"\n     --collect を最新版で回し直してください"
          f"(古い k_signals は liquidity 列を持っていません)", flush=True)

# ⛔⛔ **実発注のときは watch50 で切る** (2026-08-17 発覚)。
#   バックテストの J は `cumul × watch50`(流動性上位50銘柄)。ところが
#   --max-symbols の既定は 0=無制限 で、--watch-j は **in_j の集計にしか
#   使っていなかった**(_j_seen)。つまり --execute すると候補を全部読んで
#   全部から建てることになり、**バックテストと別物**になる:
#     ・候補は中央154銘柄(§18.44 実測) = J の3倍の母集団
#     ・154銘柄は4バッチ。09:00 の読み取りが 2〜5分かかり、その間に
#       -30bp 逃げる(§18.44)。50銘柄36.5秒 は実測済みだが154は未測定
#   §18.9 の鉄則「バックテストとライブを必ず揃える」に従い、
#   --execute のときは --watch-j(=50) を上限にする。
#   記録だけの実行(K の研究)は従来どおり無制限のままでよい。
# ⛔ 予算が「価格帯の上限 × 100株」に届かないと、値がさ株が **黙って0単元で
#   落ちる**(実測: 予算50万だと6,000円株は建てられない)。滑りを測るのが目的
#   なのに値がさ株だけ母集団から消えると偏るので、必ず知らせる。
if args.execute:
    _need1 = args.max_price * 100
    if args.budget * 1e4 < _need1:
        print(f"  ⛔ 予算 {args.budget:.0f}万 では "
              f"**{args.max_price:,.0f}円台の銘柄が建てられません**"
              f"(1単元 {_need1 / 1e4:.0f}万 必要)。\n"
              f"    合格しても0単元で落ちるので、値がさ株だけ母集団から"
              f"消えて偏ります。\n"
              f"    → --budget {_need1 / 1e4:.0f} 以上にしてください",
              flush=True)

if args.execute and args.max_symbols <= 0:
    args.max_symbols = args.watch_j
    print(f"  ★ --execute なので読む銘柄を **流動性上位{args.watch_j}件**に"
          f"絞ります（= バックテストの J と同じ watch{args.watch_j}）。\n"
          f"    ⛔ 絞らないと候補全部(中央154銘柄)から建てることになり、"
          f"バックテストと別物になります(§18.9)。\n"
          f"    変えるなら --max-symbols を明示（自己責任）", flush=True)
if args.max_symbols > 0 and len(_syms) > args.max_symbols:
    print(f"  ⚠ 候補 {len(_syms):,}銘柄 を --max-symbols {args.max_symbols} で"
          f"切ります（{len(_syms) - args.max_symbols:,}銘柄は読みません）",
          flush=True)
    _syms = _syms[:args.max_symbols]

# J の母集団(in_j 列用)
# ⛔⛔ **ペア単位(銘柄×戦略)で判定する** (2026-08-17 発覚)。
#    以前は「その銘柄が cumul にあるか」の銘柄単位だったため、
#    7936 アシックスのように **cumul には A7 だけ載っていて、今日シグナルを
#    出したのは MACDTF/DON/VOLTF/MOM** という銘柄を in_j=1 と数えてしまう。
#    実際の J はペア単位で絞るので、この銘柄は J では1件も建てない。
#    → in_j は「**その銘柄で J の候補ペアが今日シグナルを出したか**」にする。
_jpool: set = set()          # J が建てうる銘柄(ペアで交差した銘柄だけ)
_jpairs: set = set()         # (銘柄, 戦略) の集合
if Path(args.pool).exists():
    try:
        _pt = Path(args.pool).read_text(encoding="utf-8")
        # ("7936.T", "アシックス", "A7"), の3要素タプル(引用符は ' も " も可)
        for _m in re.finditer(
                r"""\(\s*['"](\d{4}[A-Z0-9]?)(?:\.T)?['"]\s*,"""
                r"""\s*['"][^'"]*['"]\s*,\s*['"]([A-Za-z0-9_]+)['"]\s*\)""", _pt):
            _jpairs.add((_m.group(1), _m.group(2)))
    except Exception as _je:
        print(f"  ⚠ {args.pool} のペア抽出に失敗: {_je}", flush=True)
    # 今日のシグナル(銘柄×戦略)と交差させる。k_signals が無い場合は
    # 銘柄単位にフォールバック(旧挙動。ただし過大評価になる旨を出す)。
    _sig_pairs: set = set()
    if Path(_sig_csv).exists():
        try:
            for r in _csv.DictReader(open(_sig_csv, encoding="utf-8-sig")):
                _s1 = str(r.get("symbol") or "").upper() \
                    .removesuffix(".T").split(".")[0]
                _t1 = str(r.get("strategy") or "").strip()
                if _s1 and _t1:
                    _sig_pairs.add((_s1, _t1))
        except Exception:
            pass
    if _jpairs and _sig_pairs:
        _jpool = {s for (s, t) in (_jpairs & _sig_pairs)}
        print(f"  [J] {args.pool} と今日のシグナルを **ペア単位**で交差: "
              f"{len(_jpairs & _sig_pairs):,}ペア / {len(_jpool):,}銘柄"
              f"（銘柄単位なら {len({s for s, _ in _jpairs} & {s for s, _ in _sig_pairs}):,}銘柄"
              f" = 過大）", flush=True)
    else:
        _jpool = set(_codes_from(args.pool))
        print(f"  ⚠ [J] ペア交差できないので **銘柄単位**で判定します"
              f"({len(_jpool):,}銘柄)。in_j は過大評価になります", flush=True)


try:
    from kabu_api import KabuClient
except Exception as _e:
    sys.exit(f"[error] kabu_api を読めません: {_e}")

# ⛔ 時間外に読み直すと、朝の k_paper_<日付>.csv を **上書きしてしまう**
#   (2026-08-18 15:39 の誤実行で実際に潰れた。k_morning の write-once に
#    救われたが、作業用ファイルは失われた)。窓の外なら別名にする。
_out_path = args.out or f"k_paper_{_dt.date.today():%Y%m%d}.csv"
if not args.out:
    _h0, _m0 = (int(x) for x in str(args.poll_until).split(":"))
    if (_dt.datetime.now().hour * 60 + _dt.datetime.now().minute) > _h0 * 60 + _m0 + 20:
        _out_path = (f"k_paper_{_dt.date.today():%Y%m%d}"
                     f"_late{_dt.datetime.now():%H%M}.csv")
        print(f"  ⚠ 発注の窓の外なので、朝のファイルを守るため "
              f"**{_out_path}** に書きます", flush=True)
# ★ 発注するかどうかを **見出しで言い切る**。取り違えたら実弾なので、
#   dry-run と実発注が同じ見た目になってはいけない。
_mode_note = (
    (f"🚀 **実発注します**（{'本番口座 18080' if args.prod else 'デモ口座 18081'}"
     f" / 総額上限 {(args.max_notional or args.budget):.0f}万"
     f" / 保護指値 始値-{args.limit_slip_bp:.0f}bp）"
     + ("" if args.prod else "  ※ --prod が無いのでデモです"))
    if args.execute else
    "⛔ **発注しません**（記録のみ。出すなら --execute）")
print(f"""
{'=' * 74}
■ J(09:00確認方式) — {_dt.date.today()}
{'=' * 74}
  {_mode_note}
  候補 {len(_syms):,}銘柄 / {args.batch}件バッチ × {-(-len(_syms) // args.batch)}回
  合格 = 始値が前日終値 {args.gap_bp:+.0f}bp 以上（{args.guard_bp:+.0f}bp 超は見送り）
  予算 {args.budget:.0f}万 / 1銘柄上限 {args.max_yen:.0f}万 / 最大{args.max_lot}単元
  09:00に配る予算 {args.budget * args.g1:.0f}万 (--g1 {args.g1:g}){
    '' if args.g1 >= 1.0 else
    ' ⛔ **レポートは全額を09:00に配ります**。この設定だと'
    f'{args.budget * (1 - args.g1):.0f}万が遅寄り用に温存され、'
    '遅寄りが無ければ使われません(§18.9: バックテストとライブは揃える)'}
  J = 選定あり({args.pool} {len(_jpool):,}銘柄)の流動性上位{args.watch_j}件
  K = 全候補
  → {_out_path}
""", flush=True)

# ⛔ dry_run は **--execute を付けたときだけ** False。付けなければ発注系は
#   API を叩かず内容を print するだけ(kabu_api の設計 / §12.4)。
#   margin_type=3 (一般信用デイトレ) が必須。kabu_api の既定は 1(制度信用)で、
#   制度は空売りが貸借銘柄限定なので非貸借銘柄が MarginTradeType 不正で弾かれる。
#   他のライブ経路(kabu_send_lss / lss_budget_cap / lss_exit_watcher / order_server)
#   は全部 3 を渡しており、ここだけ漏れていた。
cli = KabuClient(prod=args.prod, dry_run=not args.execute,
                 margin_type=args.margin_type)
cli.connect()

# ══════════════════════════════════════════════════════════════════════
#  実発注 (--execute)
# ══════════════════════════════════════════════════════════════════════
# ★ 方式 = J(§18.37 改訂版):
#     09:00 以降、**寄った銘柄から順に** 始値を見て +50bp 以上なら建てる。
#     注文は **保護指値売り @ 始値 × (1 - limit_slip_bp)**。
#     ⛔ 成行にしない。板が飛んだときに掴まされる(§18.38)。指値売りは
#       「指値以上」で約定するので、板が正常なら成行と同じ値で即約定し、
#       急落しているときだけ約定しない = 掴まされない。
#     決済は lss_exit_watcher が **実約定価格基準**で OCO を組み直す
#     (ordered_signals_lss.csv の atr/sm/tm を読む / §18.32)。
# ★ N の settle が「**自分の注文だけ**」を追うための台帳 (2026-08-31 レビュー①)。
#   ⛔ 銘柄コードで拾ってはいけない。同じ銘柄に手動注文や他戦略の注文があると
#     それも取消・数量加算の対象になる。OrderId で追う。
_N_ORD: dict = {}      # OrderId -> 銘柄。send_sell の成功応答から
_N_SENT: set = set()   # 送信を試みた銘柄(記録・警告用)
_N_UNK: set = set()    # 送信で例外 = OrderId 不明。差分から復元する対象
_N_PRE: set = set()    # 最初の送信の**直前**にあった注文IDのスナップショット
_N_FAIL: set = set()   # 復元不能などの致命的な事象
_N_PRE_TAKEN = [False]
_N_OID_ARG = [""]       # 直近の OrderId(台帳に書くため)
_N_SETTLE_OK = [True]   # settle が全部そろったか。False なら非ゼロ終了
# ⛔⛔ 台帳の集約キーは **attempt_id**(2026-08-31 レビュー5巡目①)。
#   OrderId をキーにすると、同じ注文なのに
#     pending  order_id=""        → 別キー・最終状態 pending
#     settled  order_id="ABC123"  → 別キー・最終状態 settled
#   と2つに割れ、**正常に決済まで終わった翌朝も必ず exit 2** になる。
#   送信の前に attempt_id を作り、pending / ordered / failed / settled の
#   全行に同じものを書く。
_N_ATT = [""]           # 今から出す注文の attempt_id
_N_ATT_SYM: dict = {}   # 銘柄 -> attempt_id(settle が settled を書くため)
# ⛔⛔ 発注ライフサイクルの土台が崩れたら **その日はもう発注しない**
#   (レビュー7巡目)。いまのところ立つのは「送信前の注文一覧が取れない」
#   = タイムアウト復元の土台が無い場合だけ。立ったら以降の銘柄は全部見送る。
_N_ABORT = [False]
_EX = {"left": args.budget * 1e4, "n": 0, "yen": 0.0, "ng": 0,
       "cap": (args.max_notional or args.budget) * 1e4}
_ORDER_LOG = None
if args.execute and args.n_mode:
    # ⛔⛔ N は **別台帳**に書く(2026-08-31 レビュー⑤)。
    #   ordered_signals_lss.csv に書くと lss_exit_watcher が拾い、
    #   ATR が無い建玉には **緊急幅1%の損切り**を置く。N はバリアなしが
    #   確定仕様(§18.55)なので、そもそも watcher の視界に入れない。
    _N_LEDGER = Path(__file__).resolve().parent / "ordered_signals_n.csv"

    def _ORDER_LOG(_sig, _prod, _qty, entry_mode="", order_price=0.0,
                   status="", **_kw):
        _new = not _N_LEDGER.exists()
        with open(_N_LEDGER, "a", newline="", encoding="utf-8-sig") as _f:
            _w = _csv.writer(_f)
            if _new:
                _w.writerow(["ts", "date", "symbol", "name", "strategy",
                             "side", "qty", "order_price", "status",
                             "prod", "entry_mode", "exit", "order_id",
                             "attempt_id"])
            _w.writerow([f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S}",
                         f"{_dt.date.today()}", _sig.get("symbol", ""),
                         _sig.get("name", ""), _sig.get("strategy", "N"),
                         "sell", _qty, order_price or _sig.get("order_price", 0),
                         status, 1 if _prod else 0, entry_mode or "n_open",
                         "MOC", _kw.get("order_id", ""),
                         _kw.get("attempt_id", "")])
    print(f"[N] 発注記録は **{_N_LEDGER.name}** に書きます"
          f"(ordered_signals_lss.csv には書かない = watcher の視界に入れない)")

    # ⛔⛔ 起動時チェック(レビュー2巡目③ / 4巡目③で強化)。
    #   前回が MOC 設置の前に落ちていると **無防備な建玉**が残る。N には
    #   watcher が無いので誰も気づかない。**新規発注の前に**必ず止める。
    #   ⛔ status=="ordered" だけでは足りない。いちばん危ない停止位置は
    #     「pending を書いた → 送信した → 応答を受ける前に落ちた」で、
    #     台帳には pending しか残らない。注文は通っているかもしれない。
    #   → 銘柄ごとに **最後の状態**を見て、failed / settled まで進んで
    #     いないものは全部 未完了として扱う。
    try:
        if _N_LEDGER.exists():
            _rows_l = list(_csv.DictReader(
                open(_N_LEDGER, encoding="utf-8-sig")))
            _last_l: dict = {}
            for _r_l in _rows_l:
                # ⛔⛔ order_id をキーに含めない(レビュー5巡目①)。同じ注文でも
                #   pending は order_id="" なので**別キーに割れ**、決済まで
                #   終わっても pending 行が残って毎朝 exit 2 になっていた。
                #   attempt_id があればそれ、無ければ(古い台帳)銘柄で束ねる
                #   — N は1銘柄1日1注文なので銘柄で一意になる。
                _k_l = (str(_r_l.get("date", "")),
                        str(_r_l.get("attempt_id") or "")
                        or str(_r_l.get("symbol", "")))
                _last_l[_k_l] = _r_l          # 同じ注文の最後の行が残る
            _stuck = [v for v in _last_l.values()
                      if str(v.get("status", "")) not in ("failed", "settled")]
            if _stuck:
                print("\n⛔⛔ **前回の実行が決済設置まで終わっていません**\n"
                      f"   {_N_LEDGER.name} に failed / settled まで進んで"
                      f"いない注文が {len(_stuck)}件 あります:", flush=True)
                for _r_l in _stuck[-10:]:
                    print(f"     {_r_l.get('date')} {_r_l.get('symbol')} "
                          f"{_r_l.get('qty')}株 status={_r_l.get('status')} "
                          f"order_id={_r_l.get('order_id') or '(不明)'}",
                          flush=True)
                print("\n   ⛔ **無防備な建玉が残っている可能性があります**。\n"
                      "      ⚠ とくに status=pending は『送信したが応答を受け\n"
                      "        取る前に落ちた』かもしれず、注文が通っている\n"
                      "        可能性があります。\n"
                      "      kabu ステーションで売建を確認し、引け成行が無い\n"
                      "      ものは手動で出してから、もう一度実行してください。\n"
                      "      確認済みなら --n-ignore-stuck を付けて起動します。",
                      flush=True)
                if not args.n_ignore_stuck:
                    sys.exit(2)
                print("   ⚠ --n-ignore-stuck が付いているので続行します\n",
                      flush=True)
    except SystemExit:
        raise
    except Exception as _se:
        # ⛔ 復旧ガードが読めないまま実発注に進むのは逆向き(レビュー4巡目③)。
        print(f"\n⛔⛔ 台帳({_N_LEDGER.name})を読めません: {_se}\n"
              f"   前回の未決済が残っているか判断できないので **中止します**。\n"
              f"   kabu ステーションで売建を確認してから、必要なら\n"
              f"   --n-ignore-stuck を付けて起動してください。", flush=True)
        if not args.n_ignore_stuck:
            sys.exit(2)
elif args.execute:
    try:
        from kabu_send_lss import _log_ordered as _ORDER_LOG
    except Exception as _le:
        print(f"  ⚠ _log_ordered を読めません({_le})。"
              f"**発注は行いますが ordered_signals_lss.csv に残りません** "
              f"→ watcher が決済できないので手動決済が必要です", flush=True)
# ★ N の保護指値に使う **銘柄別の正しい呼値**。
#   ⛔ floor_to_tick(下) は本文に「実測より 5〜10倍粗い」と書かれた古いテーブル。
#   §18.31 で「呼値は銘柄区分(TOPIX100か否か)で違うのでハードコードしない」と
#   確定しているので、銘柄を渡せる版を最優先で使う。
try:
    from backtest_limit_entry import floor_to_tick_sym as _ntick_floor
except Exception:
    try:
        from eh_trades import _tick as _et_tick

        def _ntick_floor(p, sym=None):
            import math as _m
            _t = _et_tick(float(p))
            return _m.floor(float(p) / _t) * _t
    except Exception:
        def _ntick_floor(p, sym=None):
            return float(int(p))
try:
    from backtest_limit_entry import floor_to_tick as _floor_tick
except Exception:
    def _floor_tick(p):                     # 保険(呼値表が無くても動く)
        return float(int(p))


def _verify(_r, _lim: float, _qty: int) -> str:
    """発注直前の検算。異常なら理由を返す(空文字なら OK)。

    ⛔ 2026-08-13 の H 初日は ATR が記録に無く **損切りが丸ごと無効化**された。
      同じことを起こさないため、発注前に必ず全部そろっているか見る。
    """
    _op = float(_r.get("open_p") or 0)
    _atr = float(_r.get("atr") or 0)
    _sk = float(_r.get("stop_k") or 0)
    _tk = float(_r.get("target_k") or 0)
    if _op <= 0:
        return "始値が0"
    # ⛔⛔ N は **バリアを持たない**(損切りも利確も置かない / §18.55)。
    #   したがって ATR / stop_k / target_k が無いのが**正常**で、J のガードを
    #   そのまま当てると合格銘柄が全件止まる(2026-08-31 の記録実行で実測)。
    #   代わりに N では「株数が100であること」を必ず見る(下の共通チェック)。
    if args.n_mode:
        if _qty != 100:
            return f"N は100株固定のはずが {_qty}株(§18.58 ③)"
    else:
        if _atr <= 0:
            return "ATRが無い(損切りが無効化される)"
        if not (_sk > 0 and _tk > 0):
            return "損切/利確が無い"
        if not (_sk > _op > _tk):
            return (f"ショートの向きが逆(損切{_sk:,.1f} > 約定{_op:,.1f} > "
                    f"利確{_tk:,.1f} でない)")
    if _qty <= 0 or _qty % 100:
        return f"株数が不正({_qty})"
    if not (0 < _lim <= _op):
        return f"保護指値が始値より上({_lim:,.1f} > {_op:,.1f})"
    return ""


def _n_adopt_unknown() -> None:
    """送信で例外が出て OrderId が取れなかった銘柄を、注文IDの差分から拾う。

    ⛔ 例外の中身によっては **注文が通っている**。N には watcher が無いので、
      ここで拾えないと無防備な建玉がそのまま残る(2026-08-31 レビュー①)。
    """
    if not _N_UNK:
        return
    try:
        _now = cli.get_orders() or []
    except Exception as _e:
        print(f"    ⛔ 注文照会に失敗し、送信タイムアウト分を復元できません: {_e}\n"
              f"       対象 {sorted(_N_UNK)} — **kabu で手動確認してください**",
              flush=True)
        _N_FAIL.add("unknown_unrecoverable")
        return
    for _o in _now:
        _oid = str(_o.get("ID") or "")
        _sy = str(_o.get("Symbol", "")).upper().removesuffix(".T").split(".")[0]
        if not _oid or _oid in _N_PRE or _oid in _N_ORD:
            continue
        if _sy not in _N_UNK:
            continue
        if int(_o.get("CashMargin") or 0) != 2:
            continue
        if str(_o.get("Side") or "1") != "1":      # 1=売
            continue
        _N_ORD[_oid] = _sy
        _N_UNK.discard(_sy)          # ⛔ 復元できたものだけ外す
        print(f"    ⚠ {_sy} 送信タイムアウトだったが注文 {_oid} が成立していた"
              f" → settle の対象に入れます", flush=True)


def _n_close_positions(_sym: str, _qty: int, _exec_ids: set,
                       _retry_s: float = 60.0) -> list | None:
    """N が建てた建玉だけの ClosePositions を作る。作れなければ None。

    ⛔⛔ send_moc(close_positions=None) は kabu_api の中で
        ① cancel_open_close_orders() … **同一銘柄の既存返済注文を一括取消**
        ② _build_close_positions()   … **FIFO で建玉を選ぶ**
      をやる。同じ銘柄に手動・他戦略の古い売建があると、**そちらが決済され
      N の建玉が残る**(2026-08-31 レビュー2巡目②)。

    ここでは自分の注文の約定(Details の RecType=8)から ExecutionID を集め、
    **その ExecutionID を持つ建玉だけ**を HoldID に指定する。
    ⛔ 特定できないときは自動割当てに落とさず None を返す(fail-closed)。
    """
    if not _exec_ids:
        print(f"    ⛔ {_sym}: 自分の約定 ExecutionID が取れません。"
              f"**自動割当てに落とさず MOC を見送ります**", flush=True)
        return None
    # ⚠ 約定直後は positions への反映が遅れる / ExecutionID が一時的に空に
    #   なる事例が公式に報告されている。1回で諦めず数秒 再照会する。
    _mine, _other, _ps, _tries = [], 0, [], 0
    _t0 = time.time()
    while True:
        _tries += 1
        try:
            _ps = cli.get_margin_positions(_sym)
        except Exception as _pe:
            print(f"    ⚠ {_sym}: 建玉の取得で例外({_pe}) 再試行 {_tries}",
                  flush=True)
            _ps = []
        _mine, _other = [], 0
        for _p in _ps:
            if str(_p.get("Side", "")) != "1":              # 1=売建
                continue
            _lv = int(_p.get("LeavesQty") or _p.get("Qty") or 0)
            if _lv <= 0:
                continue
            _eid = str(_p.get("ExecutionID", "") or "")
            if _eid and _eid in _exec_ids:
                _mine.append({"HoldID": _eid, "Qty": _lv})
            else:
                _other += 1
        if sum(int(x["Qty"]) for x in _mine) >= _qty:
            break
        if time.time() - _t0 > _retry_s:
            break
        time.sleep(1.0)
    _have = sum(int(x["Qty"]) for x in _mine)
    print(f"    [建玉] {_sym}: 一致 {_have}株 / 約定 {_qty}株 "
          f"(自分の約定ID {len(_exec_ids)}件 / 建玉 {len(_ps)}本 / "
          f"他 {_other}本 / 再照会 {_tries}回)", flush=True)
    # ⛔⛔ **数量が完全に一致したときだけ** MOC を出す(レビュー4巡目②)。
    #   足りないぶんだけ返済すると、残りが無防備なまま「成功」で終わる。
    if _have != _qty:
        print(f"    ⛔ {_sym}: 建玉 {_have}株 ≠ 約定 {_qty}株 → "
              f"**MOC を見送ります**。\n"
              f"       kabu ステーションで売建を確認し、手動で引け成行を"
              f"出してください", flush=True)
        return None
    if _other:
        print(f"    ⚠ {_sym}: 自分のものでない売建 {_other}本 は触りません",
              flush=True)
    return _mine


def _n_settle(_timeout: float = 60.0, _every: float = 3.0) -> bool:
    """N: **自分の注文IDだけ**を終端まで追い、確定数量ぶんの引けMOC を置く。

    ⛔ J のやり方(未約定を取消 → 一部約定は残す → 建玉を舐めて MOC)は N では
      使えない。watcher が無いので後始末する者がいないため:
        ① 一部約定を残すと、MOC の数量を決めた**後に**残りが約定して無防備
        ② 建玉を舐めると、他戦略・手動の売建にも MOC を出し、その既存の
           返済注文を取り消す(§18.48 ⑦③)

    ⛔⛔ そして **銘柄コードで拾ってはいけない**(2026-08-31 レビュー①)。
      同じ銘柄に手動注文や他戦略の注文があると、それも取消・数量加算の
      対象になる。ここでは send_sell の応答で得た **OrderId** だけを追う。

    ⛔ fail-open にしない(同レビュー③)。照会失敗・非終端が残る・取消失敗・
      MOC失敗 のどれか1つでも False を返す。呼出側は非ゼロ終了する。
    """
    print(f"\n  ── N: 自分の注文を終端まで追って引け成行(MOC)を置きます "
          f"{'─' * 22}", flush=True)
    _n_adopt_unknown()
    if not _N_ORD:
        if _N_SENT:
            print(f"    ⛔⛔ 送信した {len(_N_SENT)}銘柄の注文IDが1つも取れて"
                  f"いません。**建玉が残っている可能性があります**。\n"
                  f"       kabu ステーションで確認し、売建があれば手動で"
                  f"引け成行を出してください: {sorted(_N_SENT)}", flush=True)
            return False
        print("    (送信した注文がありません)", flush=True)
        return True

    _ok = True
    # ⛔⛔ 「照会にまだ出てこない自分の注文ID」を成功扱いにしない
    #   (2026-08-31 レビュー5巡目②)。send_sell が OrderId を返した直後は
    #   /orders への反映が遅れることがある。そのとき _mine() は空・_open も
    #   空・_N_UNK も空になり、**板に注文が残っているのに約定ゼロで True**
    #   を返していた。全IDを一度は照会で確認するまで待つ。
    # ⛔⛔ さらに「一度見えて後で消える」穴を塞ぐ(レビュー6巡目)。
    #   /orders は毎回おなじ集合を返すとは限らない(状態や日付で絞られる・
    #   一時的に欠ける)。**今回の応答**だけを見ていると、9:02 に見えた注文が
    #   9:05 の応答から消えたとき _open も空・_miss も空(一度見たので)に
    #   なって break し、その注文の約定が _fill に入らず **MOC が出ない**。
    #   → OrderId ごとに **最後に見た注文オブジェクトを保持**し、
    #     判定も約定数量の集計も**キャッシュ**から行う。
    _seen: dict = {}        # OrderId -> (銘柄, 最後に見た注文オブジェクト)

    def _mine() -> list:
        _got = [(_N_ORD[str(_o.get('ID'))], _o)
                for _o in (cli.get_orders() or [])
                if str(_o.get("ID") or "") in _N_ORD]
        for _s_, _o_ in _got:
            _seen[str(_o_.get("ID") or "")] = (_s_, _o_)
        return _got

    def _term(_o) -> bool:
        return int(_o.get("OrderState") or _o.get("State") or 0) >= 5

    # ── 取消して終端まで待つ ─────────────────────────────────────
    _t0, _cxl_n, _seen_all = time.time(), {}, False
    while True:
        try:
            _mine()
            _seen_all = True
        except Exception as _ge:
            print(f"    ⛔ 注文照会に失敗: {_ge}", flush=True)
            _ok = False                      # ⛔ fail-open にしない
            if time.time() - _t0 > _timeout:
                break
            time.sleep(_every)
            continue
        # ⚠ 注文の反映は遅れることがある。待機中も復元を試み続ける。
        if _N_UNK:
            _n_adopt_unknown()
        # ★ 判定は**キャッシュ**から(今回の応答から消えても見失わない)。
        _open = [(_s_, _o_) for _s_, _o_ in _seen.values() if not _term(_o_)]
        # ★ まだ一度も照会に現れていない自分の注文(レビュー5巡目②)。
        _miss = set(_N_ORD) - set(_seen)
        if not _open and not _N_UNK and not _miss:
            break
        if not _open and not _miss and time.time() - _t0 > _timeout:
            break
        for _sy, _o in _open:
            _oid = str(_o.get("ID") or "")
            # ⛔ 取消は「1回出して終わり」にしない。応答が失敗なら再送する。
            if _cxl_n.get(_oid, 0) >= 3:
                continue
            try:
                _rr = cli.cancel_order(_oid)
                _cxl_n[_oid] = _cxl_n.get(_oid, 0) + 1
                if _rr.get("Result") == 0 or _rr.get("_dry_run"):
                    print(f"    ✂ {_sy} 残りを取消 "
                          f"(約定 {int(float(_o.get('CumQty') or 0))}株)",
                          flush=True)
                else:
                    print(f"    ⚠ {_sy} 取消の応答が失敗: {_rr} "
                          f"(再試行 {_cxl_n[_oid]}/3)", flush=True)
            except Exception as _ce:
                _cxl_n[_oid] = _cxl_n.get(_oid, 0) + 1
                print(f"    ⚠ {_sy} 取消で例外: {_ce} "
                      f"(再試行 {_cxl_n[_oid]}/3)", flush=True)
        if time.time() - _t0 > _timeout:
            # ⚠ 何が残ったかはループを出た直後の _miss / _nonterm が印字する。
            #   ここで出すと二重になるので黙って抜ける(判定は下で必ず付く)。
            break
        time.sleep(_every)

    # ⛔⛔ 反映されないままの注文IDは **成功扱いにしない**(レビュー5巡目②)。
    #   ここを通さないと「板に残っているのに約定ゼロで完了」になる。
    _miss = set(_N_ORD) - set(_seen)
    if _miss:
        print(f"\n    ⛔⛔ **注文が {_timeout:.0f}秒たっても注文照会に現れま"
              f"せん** ({len(_miss)}件)\n"
              f"       {sorted(_N_ORD[m] for m in _miss)} — 板に残っている\n"
              f"       可能性があります。取消も約定数量の確定もできていません。\n"
              f"       kabu ステーションで注文と売建を確認し、必要なら手動で\n"
              f"       取消・引け成行を出してください。", flush=True)
        _ok = False

    if not _seen_all:
        print(f"    ⛔⛔ 注文の状態を一度も取得できませんでした。"
              f"**手動確認が必要です**", flush=True)
        return False

    # ── 確定した約定数量と ExecutionID を銘柄単位で集める ────────
    # ⛔ **キャッシュから**集める(レビュー6巡目)。今回の応答から消えた注文の
    #   約定を取りこぼすと、その建玉に MOC が出ず無防備なまま持ち越す。
    # ⛔ 終端でない注文が残っていたら、その数量は**確定していない**。
    #   MOC は出す(無防備よりまし)が、成功扱いにはしない。
    _nonterm = [_s_ for _s_, _o_ in _seen.values() if not _term(_o_)]
    if _nonterm:
        print(f"    ⛔⛔ 終端になっていない注文が {len(_nonterm)}件 残ります "
              f"({sorted(set(_nonterm))})。**約定数量は確定していません**。\n"
              f"       この後 MOC を出しますが、数量が足りない可能性があります。"
              f"kabu の注文照会で必ず目視してください", flush=True)
        _ok = False
    _fill: dict = {}
    _eids: dict = {}
    for _sy, _o in _seen.values():
        _cq = int(float(_o.get("CumQty") or 0))
        if _cq > 0:
            _fill[_sy] = _fill.get(_sy, 0) + _cq
        # ★ 自分の約定の ExecutionID。これが MOC の HoldID になる。
        for _d in (_o.get("Details") or []):
            if int(_d.get("RecType") or 0) != 8:        # 8=約定
                continue
            _ei = str(_d.get("ExecutionID", "") or "")
            if _ei:
                _eids.setdefault(_sy, set()).add(_ei)
    if not _fill:
        print("    (約定した建玉がありません。MOC は不要です)", flush=True)
    # ⛔⛔ ここで return しない(レビュー4巡目①)。約定ゼロでも
    #   未解決の _N_UNK / _N_FAIL の検査と settled の記録は必ず通す。

    # ── MOC を置いて受付を確認 ───────────────────────────────────
    _mok = _mng = 0
    for _sy, _q in sorted(_fill.items()):   # 約定ゼロなら空ループ
        # ⛔ close_positions を **必ず明示**する。None にすると kabu_api が
        #   他戦略の返済注文を取り消し、FIFO で古い建玉を決済する(レビュー②)。
        _cp = _n_close_positions(_sy, _q, _eids.get(_sy, set()))
        if not _cp:
            _mng += 1
            continue
        try:
            _r = cli.send_moc(_sy, qty=sum(int(x["Qty"]) for x in _cp),
                              side="buy", cash_margin=3, close_positions=_cp)
        except Exception as _me:
            _mng += 1
            print(f"    ⛔ {_sy} {_q}株 MOC で例外: {_me}", flush=True)
            continue
        if _r.get("Result") == 0 or _r.get("_dry_run"):
            _mok += 1
            print(f"    ✅ {_sy} {_q}株 引け成行を設置", flush=True)
        else:
            _mng += 1
            print(f"    ⛔ {_sy} {_q}株 MOC 設置失敗: {_r}", flush=True)
    if _mng:
        print(f"\n  ⛔⛔ **{_mng}件 は引け成行を置けませんでした**。\n"
              f"     N には watcher がありません。このままだと**持ち越し**に\n"
              f"     なります(2026-08-19 は8建玉で -36,800円)。\n"
              f"     kabu ステーションで手動で引け成行を出してください。",
              flush=True)
        _ok = False
    else:
        print(f"    [MOC] {_mok}件すべて設置しました", flush=True)
    # ⛔⛔ 混在タイムアウトの取りこぼしを塞ぐ(2026-08-31 レビュー2巡目①)。
    #   A は OrderId が取れて MOC 成功、B はタイムアウトで復元できず …の
    #   ような混在で、_N_ORD が空でないため True を返してしまっていた。
    if _N_UNK:
        print(f"\n  ⛔⛔ **注文IDを特定できなかった銘柄が {len(_N_UNK)}件 "
              f"残っています**: {sorted(_N_UNK)}\n"
              f"     注文が通っていれば**無防備な建玉**です。kabu ステーションで\n"
              f"     売建を確認し、あれば手動で引け成行を出してください。",
              flush=True)
        _ok = False
    if _N_FAIL:
        print(f"\n  ⛔⛔ 致命的な事象: {sorted(_N_FAIL)} — 手動確認が必要です",
              flush=True)
        _ok = False
    if _ok:
        print(f"    ✅ N の決済設置は完了しました", flush=True)
        # ★ 台帳に settled を書いて、次回の起動時チェックを通す。
        if _ORDER_LOG:
            for _oid_s, _sy_s in _N_ORD.items():
                try:
                    # ★ pending と **同じ attempt_id** を書く(レビュー5巡目①)。
                    #   これが無いと pending 行が別キーで残り、翌朝 exit 2。
                    _ORDER_LOG({"symbol": _sy_s, "strategy": "N"}, args.prod,
                               int(_fill.get(_sy_s, 0)), entry_mode="n_open",
                               order_price=0, status="settled",
                               order_id=_oid_s,
                               attempt_id=_N_ATT_SYM.get(_sy_s, ""))
                except Exception as _we2:
                    # ⛔ ここで黙って True を返すと「今日は正常」と表示して
                    #   おきながら**翌朝 exit 2 で止まる**ことになる(レビュー
                    #   6巡目)。MOC は置けているので建玉は守られているが、
                    #   台帳が壊れていることは**今日のうちに**知らせる。
                    print(f"    ⛔ settled の記録に失敗({_sy_s}): {_we2}\n"
                          f"       MOC は設置済みなので建玉は守られていますが、"
                          f"台帳が書けていません。\n"
                          f"       **次回の起動は停止します**。"
                          f"{_N_LEDGER.name} を確認してください", flush=True)
                    _ok = False
    return _ok


def _seed_arm(_sym: str, _open_time: str) -> None:
    """損切りの武装時刻の起点を .lss_watcher_seen.json に書く。

    watcher の delay は「**建玉を初めて検知した時刻**の5分足」を起点にする
    (lss_exit_watcher._stop_arm_time / first_seen)。J は watcher を
    09:05〜09:10 に起動するので、09:00 に建てた玉でも first_seen が 09:10 に
    なり、delay4 の武装が 09:20 → **09:30 と10分ずれる**。
    → その銘柄が **寄った時刻** を先に書いて起点を固定する。この仕組みは
      watcher が「場中の再起動で無保護窓が伸びる」のを防ぐために持っている
      もの(§18.37)で、そこに正しい始点を入れるだけ。
    ⛔ 発注が通るたびに書く(最後にまとめて書くと、途中で落ちたときに失われる)。
    """
    try:
        import json as _json
        _sp = Path(__file__).resolve().parent / ".lss_watcher_seen.json"
        _today = f"{_dt.date.today():%Y-%m-%d}"
        _cur: dict = {}
        if _sp.exists():
            try:
                _cur = (_json.loads(_sp.read_text(encoding="utf-8"))
                        .get(_today) or {})
            except Exception:
                _cur = {}
        if str(_sym) in _cur:
            return                      # watcher が既に書いていたら尊重する
        # ⛔⛔ **tz-aware で書くこと**(2026-08-19)。watcher は
        #   `datetime.now(JST)` (aware) と比較するので、naive を書くと
        #   `can't compare offset-naive and offset-aware datetimes` で
        #   **watcher が起動2秒で落ち、建玉が引けまで残る**。実際に起きた。
        _JST = _dt.timezone(_dt.timedelta(hours=9))
        _now_j = _dt.datetime.now(_JST)
        _m = re.search(r"T?(\d{2}):(\d{2}):?(\d{2})?", str(_open_time or ""))
        if _m:
            _t = _now_j.replace(
                hour=int(_m.group(1)), minute=int(_m.group(2)),
                second=int(_m.group(3) or 0), microsecond=0)
        else:
            _h, _mi = (int(x) for x in str(args.open_at).split(":"))
            _t = _now_j.replace(hour=_h, minute=_mi,
                                second=0, microsecond=0)
        _cur[str(_sym)] = _t.isoformat()
        _sp.write_text(_json.dumps({_today: _cur}, ensure_ascii=False),
                       encoding="utf-8")
    except Exception as _se:
        print(f"    ⚠ {_sym}: 武装時刻の記録に失敗({_se})。watcher の起動時刻が"
              f"起点になり、無保護窓が最大10分伸びます", flush=True)


def _reject(_sym: str, _res: dict) -> str:
    """発注リジェクトを分類する。order_server と**同じ扱い**にする。

    ・100302 売建規制 … 当日限りの動的規制。恒久除外はしない
    ・4002013 一般信用デイトレ売り非対応 … 銘柄固有で安定するので
      not_shortable.py に恒久追加し、次回からシグナル・発注の両方で事前除外
    ⛔ ここを持たないと「毎朝同じ銘柄で失敗し続ける」ことになる。
    """
    _c = _res.get("Code")
    try:
        _ci = int(_c)
    except Exception:
        _ci = None
    _msg = str(_res.get("Message", ""))
    if _ci == 100302 or "売建規制" in _msg:
        return (f"本日『売建規制』(100302)。当日限りなので恒久除外はしません")
    if _ci == 4002013 or "MarginTradeType" in _msg:
        try:
            from order_server import _append_not_shortable
            _append_not_shortable(_sym, "一般デイトレ売り非対応(4002013)")
            return ("一般信用デイトレ売り非対応(4002013)。"
                    "not_shortable.py に追加しました(次回から事前除外)")
        except Exception as _ae:
            return (f"一般信用デイトレ売り非対応(4002013)。"
                    f"⚠ not_shortable への追記に失敗({_ae}) → 手で追加してください")
    return str(_res)


def _order_rows(_sel: list) -> None:
    """このグループの合格銘柄を発注する。--execute のときだけ実発注。"""
    if not _sel:
        return
    # ⛔ 発注ライフサイクルの土台が崩れた日は、以降のグループも全部見送る
    #   (レビュー7巡目)。09:00 の1本目で立てば、その日は1件も建てない。
    if _N_ABORT[0]:
        print(f"  ⛔ N は発注を中止しています(この {len(_sel)}銘柄も見送り)",
              flush=True)
        return
    for _r in sorted(_sel, key=lambda x: -float(x.get("gap_bp") or 0)):
        if _N_ABORT[0]:
            print(f"  ⛔ {_r['symbol']} 見送り(N の発注を中止しました)",
                  flush=True)
            continue
        if int(_r.get("ordered") or 0):
            continue          # 二重発注の保険(グループは1回しか通らないが念のため)
        _lot = int(_r.get("lots_k") or 0)
        if _lot <= 0:
            continue
        _qty = _lot * 100
        _op = float(_r.get("open_p") or 0)
        # ⛔ N の保護指値は **呼値ベース**(2026-08-31 レビュー④)。
        #   J の 50bp は N には広すぎる — 残っているのは +21.9bp なので、
        #   始値-50bp で約定した時点で期待値を失う。
        #   ⚠ bp で固定してはいけない。呼値は銘柄区分で変わる(§18.31)。
        if args.n_mode:
            # ⛔ _floor_tick(= backtest_limit_entry.floor_to_tick) は本文に
            #   「実測より 5〜10倍粗い」と書いてある**古いテーブル**。使わない。
            #   銘柄別の正しい呼値(_ntick_floor)で1ティックずつ下げ、
            #   **許容 bp を超えたらそこで止める**。両方とも要る:
            #     ・ティックだけだと、粗い呼値の銘柄で下げすぎる
            #     ・bp だけだと、呼値に乗らない値段になる
            #   ⚠ 1ティックでも許容を超えるなら **指値=始値**。バックテストは
            #     「始値で建てる」前提なので、それが最も忠実。約定しなければ
            #     その銘柄を建てないだけで、損はしない。
            _lim = float(_op)
            for _ in range(max(0, args.n_limit_ticks)):
                _nx = float(_ntick_floor(_lim - 1e-7, _r["symbol"]))
                if _nx <= 0 or (_op - _nx) / _op * 1e4 > args.n_limit_max_bp:
                    break
                _lim = _nx
        else:
            _lim = float(_floor_tick(_op * (1.0 - args.limit_slip_bp / 1e4)))
        _why = _verify(_r, _lim, _qty)
        if _why:
            _EX["ng"] += 1
            print(f"  ⛔ {_r['symbol']} 発注中止: {_why}", flush=True)
            continue
        _need = _qty * _op
        if _EX["yen"] + _need > _EX["cap"]:
            print(f"  ⛔ {_r['symbol']} 発注中止: 総額上限 "
                  f"{_EX['cap'] / 1e4:,.0f}万 を超えます"
                  f"(既発注 {_EX['yen'] / 1e4:,.0f}万 + {_need / 1e4:,.0f}万)",
                  flush=True)
            continue
        # ⛔⛔ **記録を先に書く**(2026-08-19)。旧実装は発注が成功してから
        #   記録していた。その間にプロセスが落ちると **注文だけが板に残り、
        #   watcher はそれを知らない** = 今日の実損と同じ無防備状態になる。
        #   記録が余分に残るのは無害(watcher が建玉を探して見つからないだけ)。
        #   失敗したら下で "failed" を追記して打ち消す。
        def _log(_st) -> bool:
            """台帳に1行書く。**書けたら True**(レビュー6巡目)。"""
            if not (_ORDER_LOG and args.execute):
                return True
            try:
                # ★ attempt_id は **pending から settled まで同じもの**を書く
                #   (レビュー5巡目①)。order_id は取れてから埋まる。
                _oid_kw = ({"order_id": _N_OID_ARG[0],
                            "attempt_id": _N_ATT[0]}
                           if args.n_mode else {})
                # ⛔ N はバリアを持たないので、損切/利確/ATR を **0 で書く**。
                #   記録自体は残す(§18.48 ⑦「記録を発注より先に書く」)が、
                #   万一 watcher が起動しても **武装するものが無い**状態にする。
                _nb = args.n_mode
                _ORDER_LOG({"symbol": _r["symbol"], "name": _r.get("name", ""),
                            "strategy": _r.get("strategy", ""),
                            "order_price": _lim,
                            "stop_price": 0.0 if _nb else float(_r["stop_k"]),
                            "target_price": 0.0 if _nb else float(_r["target_k"]),
                            "atr": 0.0 if _nb else float(_r["atr"]),
                            "sm": 0.0 if _nb else args.sm,
                            "tm": 0.0 if _nb else args.tm},
                           args.prod, _qty, entry_mode="auction",
                           order_price=_lim, status=_st,
                           **(_oid_kw if args.n_mode else {}))
            except Exception as _we:
                print(f"    ⚠ 発注記録({_st})の書き込み失敗({_we})。"
                      f"**watcher が決済できません** → 手動で買い戻すこと",
                      flush=True)
                return False
            return True

        _N_OID_ARG[0] = ""
        # ★ 送信の**前**に attempt_id を作る(レビュー5巡目①)。この1本の注文の
        #   pending / ordered / failed / settled が全部これで束ねられる。
        if args.n_mode:
            _N_ATT[0] = (f"{_dt.date.today():%Y%m%d}-{_r['symbol']}-"
                         f"{_uuid.uuid4().hex[:8]}")
            _N_ATT_SYM[str(_r["symbol"])] = _N_ATT[0]
        # ⛔⛔ N は台帳が書けなかったら **送信しない**(レビュー6巡目)。
        #   N には watcher が無いので、台帳に無い注文は
        #     ・その日の settle も(_N_ORD には入るので)一応追える
        #     ・だが**途中で落ちたら翌朝の起動ガードが気づけない**
        #   = 誰も知らない無防備な建玉になる。書けないなら建てないほうが安全。
        #   ⚠ J は watcher が建玉を見て決済するので、この扱いは N だけ。
        # ★ 最初の送信の**直前**に、既にある注文IDを控える。送信でタイムアウト
        #   したとき「増えたID = 自分のもの」で復元するため(レビュー①)。
        # ⛔⛔ これが取れなければ **N は1件も発注しない**(レビュー7巡目)。
        #   _N_PRE が無いと _n_adopt_unknown は「増えたID」を判定できず、
        #   ①送信タイムアウトした自分の注文を復元できない(= 無防備な建玉)
        #   ②最悪、他人・他戦略の注文を自分のものとして取消・決済する
        #   のどちらかになる。復元の土台が無いまま実弾を出さない。
        #   ⚠ 台帳(_log)・_N_SENT より**先に**判定する。ここで抜けるときは
        #     まだ1件も送っていないので、pending 行も _N_SENT も残さない。
        if args.n_mode and args.execute and not _N_PRE_TAKEN[0]:
            try:
                _N_PRE.update(str(_o.get("ID") or "")
                              for _o in (cli.get_orders() or []))
                _N_PRE_TAKEN[0] = True
            except Exception as _pe:
                _N_ABORT[0] = True
                _N_SETTLE_OK[0] = False      # 非ゼロ終了で気づけるように
                print(f"\n  ⛔⛔ **送信前の注文一覧を取れません**({_pe})\n"
                      f"     これが無いと、送信がタイムアウトしたときに自分の\n"
                      f"     注文を復元できません(無防備な建玉になる)。\n"
                      f"     → **N は本日1件も発注しません**。まだ1件も送って\n"
                      f"       いないので建玉はありません。kabu の接続を確認\n"
                      f"       してから、必要なら明日やり直してください。",
                      flush=True)
                continue
        if not _log("pending") and args.n_mode and args.execute:
            _EX["ng"] += 1
            print(f"  ⛔ {_r['symbol']} 発注中止: **台帳に書けませんでした**。"
                  f"記録の無い建玉は翌朝の起動ガードが検知できないので、"
                  f"建てません", flush=True)
            continue
        # ⛔ 送信の**前**に「出そうとした銘柄」を記録する(2026-08-31 レビュー①)。
        #   例外で OrderId が取れなくても、注文が通っている可能性がある。
        #   N はここを拾えないと settle の対象から漏れて無防備になる。
        _N_SENT.add(str(_r["symbol"]))
        try:
            _res = cli.send_sell(int(_r["symbol"]), qty=_qty, price=_lim,
                                 cash_margin=2,          # 信用新規(売建)
                                 order_type="limit",
                                 omit_close_positions=True)
        except Exception as _oe:
            _EX["ng"] += 1
            print(f"  ⛔ {_r['symbol']} 発注で例外: {_oe}", flush=True)
            # ⚠ 例外の中身によっては **注文が通っている**可能性がある
            #   (送信後にタイムアウト等)。打ち消さずに pending のまま残し、
            #   watcher に守らせる。余分な記録は無害。
            if args.n_mode:
                # ⛔ N には watcher が無い。ここで諦めると無防備な建玉が残る。
                #   settle が注文IDの差分から復元する(レビュー①)。
                _N_UNK.add(str(_r["symbol"]))
                print(f"     ⛔ N: 注文が通っている可能性があります。"
                      f"settle で注文IDの差分から復元を試みます", flush=True)
            else:
                print(f"     (記録は pending のまま残します。注文が通っていた"
                      f"場合に watcher が守れるように)", flush=True)
            continue
        # 成功判定は lss_budget_cap と揃える(Result==0 かつ OrderId あり)。
        # OrderId が無いのに Result=0 のケースを通すと、発注できていないのに
        # ordered_signals_lss.csv に残って watcher が空振りする。
        _ok = (not args.execute) or (str(_res.get("Result", "")) == "0"
                                     and bool(_res.get("OrderId")))
        if not _ok:
            _EX["ng"] += 1
            print(f"  ⛔ {_r['symbol']} 発注失敗: {_reject(_r['symbol'], _res)}",
                  flush=True)
            _log("failed")     # 上で書いた pending を打ち消す
            continue
        # ★ N は **OrderId を控える**。settle はこのIDだけを追う(レビュー①)。
        #   ⛔ メモリだけでなく台帳にも即書きする(レビュー2巡目③)。
        #     ここで落ちると watcher が無いので無防備な建玉が残る。
        if args.n_mode and _res.get("OrderId"):
            _oid_ = str(_res["OrderId"])
            _N_ORD[_oid_] = str(_r["symbol"])
            _N_OID_ARG[0] = _oid_
        _EX["n"] += 1
        _EX["yen"] += _need
        _r["ordered"] = 1
        _r["order_limit"] = _lim
        # ★ 損切りの武装の起点を **この銘柄が寄った時刻** に固定する。
        #   発注が通った直後に書く(まとめて書くと途中で落ちたとき失われる)。
        # ⛔ N は watcher を使わない(バリアが無いので武装するものが無い)。
        #   .lss_watcher_seen.json に書くと、後で watcher を起動したときに
        #   N の建玉まで J のルール(delay4 / sm0.5 / tm1.0)で触られる。
        if args.execute and not args.n_mode:
            _seed_arm(_r["symbol"], _r.get("open_time"))
        print(f"  {'✓ 発注' if args.execute else '(dry-run)'} "
              f"{_r['symbol']} {_qty:,}株 保護指値 {_lim:,.1f}"
              + (f"(始値 {_op:,.1f} / -{(_op - _lim) / _op * 1e4:.1f}bp"
                 + ("・呼値が粗いので始値ちょうど" if _lim >= _op else "") + ") "
                 if args.n_mode else
                 f"(始値 {_op:,.1f} / -{args.limit_slip_bp:.0f}bp) ")
              + ("決済=引けMOCのみ(バリアなし)" if args.n_mode else
                 f"損切 {float(_r['stop_k']):,.1f} / "
                 f"利確 {float(_r['target_k']):,.1f}")
              + f" / 累計 {_EX['yen'] / 1e4:,.0f}万", flush=True)
        # ★ 発注が通ったことを記録する(pending → ordered)。
        #   ⛔ entry_mode="auction" にすると watcher が **実約定価格基準**で
        #     OCO を組み直す(§18.32 / lss_exit_watcher:355-)。J も同じ扱いで正しい。
        #   ⚠ ここが失敗しても pending が残る。J は watcher が守り、N は
        #     この後の settle が MOC を置く。どちらにせよ **翌朝の起動ガードは
        #     pending を未完了として止める**ので、取りこぼしにはならない。
        _log("ordered")


# ★★ いま kabu に登録されている銘柄 (2026-08-18)。
#   ⛔ これまで **毎ポーリングで unregister_all + register_many** していた。
#     同じ50銘柄を10秒ごとに登録し直していたので、せっかく温まった購読を
#     毎回捨てていた。実測(2026-08-18 本番): 同じ50銘柄なのに
#       poll1 50.5s → poll2 55.0s → poll3 63.1s → poll6 64.3s
#     と **周を追うごとに遅くなる**(場外のウォーム実測は 41銘柄 6.3秒 =
#     6.5件/秒 なので6分の1)。温まっているなら速くなるはずで、逆に
#     なっているのは登録し直しが原因の可能性が高い。
#   → **銘柄集合が変わらない限り登録し直さない**。バッチが1つ(=50件以下)
#     なら、2周目以降は登録が一度も走らない。
#   ⚠ 100銘柄(2バッチ)にすると A/B の切替で毎周2回の登録が要る。まずは
#     50件のまま『登録しなければ何秒になるか』を測ること。そこが速ければ
#     2バッチぶんの時間が空く。
_REGISTERED: list = []


def _read_all(tag: str) -> dict:
    """全候補を50件バッチで読む。symbol -> board。"""
    _t0 = time.time()
    _out: dict = {}
    _n_reg = 0
    for _i in range(0, len(_syms), args.batch):
        _b = _syms[_i:_i + args.batch]
        if _REGISTERED != _b:
            # 1バッチ運用なら初回だけ通る。複数バッチなら切替のたび。
            try:
                cli.unregister_all()
            except Exception:
                pass
            _res = cli.register_many(_b)
            _REGISTERED[:] = list(_b)
            _n_reg += 1
            _ok = len((_res or {}).get("RegistList") or [])
            if _ok < len(_b):
                print(f"  ⚠ 登録 {_ok}/{len(_b)}件 (kabu の上限は50件)",
                      flush=True)
                _REGISTERED.clear()       # 失敗したら次回やり直す

        def _one(s):
            try:
                return s, cli.get_board(s)
            except Exception:
                return s, {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            for _s, _bd in ex.map(_one, _b):
                if _bd:
                    _out[_s] = _bd
    # ★ 登録が何回走ったかを出す。0 = 登録し直していない(=期待どおり)。
    #   毎周 1以上なら銘柄集合が動いているので、その理由を疑うこと。
    _el = time.time() - _t0
    print(f"  [{tag}] {len(_out):,}/{len(_syms):,}銘柄 を {_el:.1f}秒 で取得"
          f" ({len(_out) / max(0.1, _el):.1f}件/秒 / 登録 {_n_reg}回)",
          flush=True)
    return _out


def _wait(hm: str, why: str) -> None:
    _h, _m = (int(x) for x in str(hm).split(":"))
    _t = _dt.datetime.now().replace(hour=_h, minute=_m, second=0, microsecond=0)
    _s = (_t - _dt.datetime.now()).total_seconds()
    if _s <= 0:
        return
    print(f"\n[待機] {_t:%H:%M:%S} まで {_s:.0f}秒 — {why}", flush=True)
    while (_r := (_t - _dt.datetime.now()).total_seconds()) > 0:
        time.sleep(min(10.0, _r))


def _mk_row(_s: str, _bd: dict, _ts: str, _grp: int) -> dict:
    """板1件 → 記録用の1行。判定(合格/遅寄り/ガード)もここで済ませる。"""
    _pc = float(_bd.get("PreviousClose") or 0)
    _op = float(_bd.get("OpeningPrice") or 0)
    _ot = str(_bd.get("OpeningPriceTime") or "")
    # ⛔⛔ **OpeningPriceTime の日付を必ず見る** (2026-08-20)。
    #   /board は引け後も当日の OpeningPrice を返し続ける(:172)。つまり 09:00 に
    #   まだ寄っていない銘柄は **前日の** OpeningPrice / OpeningPriceTime
    #   ("2026-08-19T09:00:03+09:00") を返しうる。時刻だけ見ると "09:00" なので
    #   遅寄りにならず、**前日の始値 vs 前日終値**(=前日の日中変動)で合格判定して
    #   しまう。起動時の時間窓ガード(:170)はスクリプト単位なので、この
    #   **銘柄単位**の取り違えは防げない。
    #   日付が今日でない / そもそも取れない ものは『まだ寄っていない』とみなす。
    #   建てないコストは0、誤って建てるコストは実損なので厳しい側に倒す。
    _stale = 0
    if _op > 0:
        _md = re.search(r"(\d{4})-(\d{2})-(\d{2})", _ot)
        if not _md or _md.group(0) != f"{_dt.date.today()}":
            _stale = 1
            _op = 0.0                      # 当日の始値ではない = 未取得と同じ
    # ★ 09:00 に寄ったか。OpeningPrice が無い or 時刻が 09:00 より後なら遅寄り。
    #   ⚠ --poll では遅寄りも **建てる**(グループを分けて配分する)ので、
    #      late は記録用のフラグでしかない。
    _late = 0
    if _op <= 0:
        _late = 1
    else:
        _m = re.search(r"T?(\d{2}):(\d{2})", _ot)
        if _m and (int(_m.group(1)) * 60 + int(_m.group(2))) > 9 * 60:
            _late = 1
    _gap = ((_op - _pc) / _pc * 1e4) if (_op > 0 and _pc > 0) else None
    # ⛔ N にギャップ上限は **無い**(2026-08-31 レビュー③)。
    #   §18.55 で --max-gap-bp 150 は 月+49,587 → +21,075 と棄却済み。
    #   J の +300bp ガードを残すとバックテストとライブが食い違う(§18.9)。
    _guard = 0 if args.n_mode else (
        1 if (_gap is not None and _gap > args.guard_bp) else 0)
    # ⛔ N は **09:00 の始値**も価格帯に入っていることを条件にしている
    #   (バックテスト: x[x.open.between(MIN_PRICE, MAX_PRICE)])。
    #   前夜の終値で絞っただけではライブと食い違う。
    _pband = 1 if (args.n_mode and _op > 0
                   and not (args.min_price <= _op <= args.max_price)) else 0
    # ⛔ --poll では late を不合格にしない(遅寄りを拾うのが目的)。
    _lt_ng = 0 if args.poll else _late
    _pass = 1 if (_gap is not None and _gap >= args.gap_bp
                  and not _guard and not _lt_ng and not _pband) else 0
    if _pband:
        print(f"  ⛔ {_s} 始値 {_op:,.1f} が価格帯 "
              f"{args.min_price:,.0f}〜{args.max_price:,.0f}円 の外 → 見送り",
              flush=True)
    return {"date": f"{_dt.date.today()}", "seen_ts": _ts, "grp": _grp,
            "symbol": _s, "in_j": 1 if _s in _jpool else 0,
            "rank_liq": 0, "liquidity": _liq.get(_s, 0),
            "prev_close": _pc, "open_p": _op, "open_time": _ot,
            "current_price": _bd.get("CurrentPrice") or 0,
            # 売り注文が当たる先 = 最良買い気配(Bid)。数量も残すと板の厚さが分かる。
            "bid": _bd.get("BidPrice") or 0,
            "ask": _bd.get("AskPrice") or 0,
            "bid_qty": _bd.get("BidQty") or 0,
            "ask_qty": _bd.get("AskQty") or 0,
            "gap_bp": (round(_gap, 1) if _gap is not None else ""),
            "late": _late, "pass_gap": _pass, "guard_ng": _guard,
            "stale_open": _stale,
            "lots_k": 0, "yen_k": 0,
            # ★ OCO は **実約定価格(=始値)** を基準に置く (§18.32)。
            #   ショートなので 損切りは上、利確は下。
            #   シグナル時点の stop/target(逆指値トリガー基準)とは別物。
            "atr": round(_atr, 2) if (_atr := float(_ATR.get(_s, 0) or 0)) else "",
            "stop_k": round(_op + _atr * args.sm, 1) if (_atr and _op > 0) else "",
            "target_k": round(_op - _atr * args.tm, 1) if (_atr and _op > 0) else "",
            "ordered": 0, "order_limit": ""}


def _dump(_rows: list) -> None:
    """途中で落ちてもデータを失わないよう、毎周 書き出す。"""
    try:
        with open(_out_path, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.DictWriter(f, fieldnames=_COLS)
            w.writeheader()
            w.writerows(_rows)
    except Exception as e:
        print(f"  ⚠ CSV を書けません: {e}", flush=True)


def _size_group_live(_g: int, _sel: list) -> None:
    """1グループぶんだけ配る (発注と同時に走らせる版 / 2026-08-17)。

    _size_groups と同じ式(第1グループ=予算×g1 / 以降=残り予算)だが、
    **寄った瞬間に配って即発注する**ために切り出した。残り予算は _EX["left"]
    で持ち越す。
    ⛔ 未寄件数で割る動的配分にはしない(ユーザー判断: ライブで壊れやすい)。
    """
    if not _sel:
        return
    _bud, _cap = args.budget * 1e4, args.max_yen * 1e4
    _alloc = min(_bud * args.g1, _EX["left"]) if _g == 0 else _EX["left"]
    # 分母は **戦略の本数**(バックテストと揃える / _NPAIR のコメント参照)
    _np = sum(max(1, _NPAIR.get(str(r["symbol"]), 1)) for r in _sel)
    _per = _alloc / max(1, _np)
    _used = 0.0
    for r in sorted(_sel, key=lambda x: -float(x.get("gap_bp") or 0)):
        _u = float(r["open_p"] or 0) * 100
        if _u <= 0:
            continue
        # その銘柄の枠 = 1戦略ぶん × 本数。上限(--max-yen)で頭を切る。
        _sy = _per * max(1, _NPAIR.get(str(r["symbol"]), 1))
        if _cap > 0:
            _sy = min(_sy, _cap)
        _lot = min(args.max_lot, int(_sy // _u))
        if _cap > 0:
            _lot = min(_lot, int(_cap // _u))
        _lot = max(1, _lot)
        while _lot > 0 and _used + _lot * _u > _EX["left"]:
            _lot -= 1
        if _lot <= 0:
            continue
        r["lots_k"] = _lot
        r["yen_k"] = round(_lot * _u, 0)
        _used += _lot * _u
    _EX["left"] = max(0.0, _EX["left"] - _used)


def _size_groups(_rows: list) -> None:
    """グループごとに **固定上限** で配る (2026-08-16 ユーザー判断)。

    第1グループ(09:00の板寄せ) … 予算 × --g1
    以降の各グループ           … 残り予算
    ⛔ 端数配分はしない。締切までに寄らなかった候補ぶんの予算は使わない
       (§18.38 の充填と同じで、配り切るとリスク調整後は悪化する)。
    """
    # 流動性降順の順位(発注順 / §18.21)。0 は最後尾。
    _rows.sort(key=lambda r: (-float(r["liquidity"] or 0), str(r["symbol"])))
    for _i, r in enumerate(_rows):
        r["rank_liq"] = _i + 1
    _bud, _cap = args.budget * 1e4, args.max_yen * 1e4
    _R = _bud
    for _g in sorted({int(r["grp"]) for r in _rows}):
        _sel = [r for r in _rows if int(r["grp"]) == _g and r["pass_gap"]]
        if not _sel:
            continue
        _alloc = min(_bud * args.g1, _R) if _g == 0 else _R
        _np = sum(max(1, _NPAIR.get(str(r["symbol"]), 1)) for r in _sel)
        _per = _alloc / max(1, _np)
        _used = 0.0
        for r in _sel:
            _u = float(r["open_p"]) * 100
            if _u <= 0:
                continue
            _sy = _per * max(1, _NPAIR.get(str(r["symbol"]), 1))
            if _cap > 0:
                _sy = min(_sy, _cap)
            _lot = min(args.max_lot, int(_sy // _u))
            if _cap > 0:
                _lot = min(_lot, int(_cap // _u))
            _lot = max(1, _lot)          # 1件目は最低1単元(現行と同じ)
            # ⛔ N は **100株固定**。予算は「何銘柄建てるか」だけを決める。
            #   §18.58 ③(資金均等は100株の床で動けない) / §18.36・2026-08-31
            #   (1件31万固定も TRAIN +209円 t=0.07 / TEST -1,792円 t=-0.70 で
            #    差ゼロ)で確定済み。バックテストの +21.9bp もこの前提の値。
            if args.n_mode:
                _lot = 1
            # 残り予算を超えないところで打ち切る
            while _lot > 0 and _used + _lot * _u > _R:
                _lot -= 1
            if _lot <= 0:
                continue
            r["lots_k"] = _lot
            r["yen_k"] = round(_lot * _u, 0)
            _used += _lot * _u
        _R = max(0.0, _R - _used)


# ── ① ウォームアップ (登録直後の初回は48〜142秒かかる / §18.38) ───────────
if not args.now:
    _wait(args.warm_at, "登録して1回空読み(これを飛ばすと09:00が数分かかる)")
print("\n▶ ウォームアップ（空読み。値は使いません）", flush=True)
_read_all("warm")

# ══════════════════════════════════════════════════════════════════════
#  ポーリング (--poll) — 09:00 以降に寄る銘柄も拾う
# ══════════════════════════════════════════════════════════════════════
# ★★ 2026-08-16 ユーザー提案「9:06に寄り付いたらそこからすぐ注文を出せばいい」。
#   寄った銘柄から順に処理すれば、全部が寄るまで待つ必要が無い。
#   配分は **固定上限**(第1グループに 予算×G1、以降は残り予算)。
#   動的配分(残り予算 × 候補数 ÷ 未判定数)は毎回 未寄件数を数える必要があり
#   ライブで壊れやすい、というユーザー判断による。
#   ⛔ 端数配分はしない(§18.38 の充填と同じでリスク調整後は悪化)。
_rows: list = []
_seen: dict = {}          # symbol -> 最初に寄りを検知した時刻
_groups: list = []        # [(検知時刻, [銘柄...]), ...]
_read_ts = ""

if args.poll:
    _h9, _m9 = (int(x) for x in str(args.open_at).split(":"))
    _t_open = _dt.datetime.now().replace(hour=_h9, minute=_m9,
                                         second=0, microsecond=0)
    _he, _me = (int(x) for x in str(args.poll_until).split(":"))
    _t_end = _dt.datetime.now().replace(hour=_he, minute=_me,
                                        second=0, microsecond=0)
    if not args.now:
        _wait(args.open_at, "★ここから本番。寄った銘柄から順に拾う")
    print(f"\n▶ ポーリング開始（{args.every}秒ごと / {args.poll_until} まで）",
          flush=True)
    _n_poll = 0
    while True:
        _t0 = time.time()
        _n_poll += 1
        _bd_all = _read_all(f"poll{_n_poll}")
        _now_s = f"{_dt.datetime.now():%H:%M:%S}"
        _new = []
        for _s, _bd in _bd_all.items():
            if _s in _seen:
                continue
            _op = float(_bd.get("OpeningPrice") or 0)
            if _op <= 0:
                continue          # まだ寄っていない
            _seen[_s] = _now_s
            _new.append((_s, _bd))
        if _new:
            _groups.append((_now_s, [x[0] for x in _new]))
            for _s, _bd in _new:
                _rows.append(_mk_row(_s, _bd, _now_s,
                                     len(_groups) - 1))
            print(f"  [{_now_s}] **新たに寄った {len(_new)}件** "
                  f"(通算 {len(_seen)}/{len(_syms)}) "
                  f"/ 読込 {time.time() - _t0:.1f}秒", flush=True)
            # ★★ 寄った瞬間に配って発注する(= §18.38 の『即時』)。
            #   全部が寄るのを待たない。待つと 1分で -15.8bp 逃げる(§18.44)。
            _gi_now = len(_groups) - 1
            _sel_now = [r for r in _rows
                        if int(r["grp"]) == _gi_now and r["pass_gap"]]
            if _sel_now:
                _size_group_live(_gi_now, _sel_now)
                _order_rows(_sel_now)
        else:
            print(f"  [{_now_s}] 新規なし (通算 {len(_seen)}/{len(_syms)}) "
                  f"/ 読込 {time.time() - _t0:.1f}秒", flush=True)
        # ★ 途中で落ちてもデータを失わないよう毎回書き出す
        _dump(_rows)
        if args.now and _n_poll >= max(1, args.now_polls):
            break
        if _dt.datetime.now() >= _t_end or len(_seen) >= len(_syms):
            break
        _sl = max(0.0, args.every - (time.time() - _t0))
        if _sl > 0:
            time.sleep(_sl)
    _read_ts = f"{_dt.datetime.now():%H:%M:%S}"
else:
    # ── 従来: 09:00 に1回だけ読む ──────────────────────────────────
    if not args.now:
        _wait(args.open_at, "★ここが本番。板寄せ直後の始値を取る")
    print("\n▶ 本番の読み取り（09:00 の1回だけ）", flush=True)
    _read_ts = f"{_dt.datetime.now():%H:%M:%S}"
    _bd_all = _read_all("open")
    _groups.append((_read_ts, list(_bd_all)))
    for _s in _syms:
        _bd = _bd_all.get(_s) or {}
        if float(_bd.get("OpeningPrice") or 0) > 0:
            _seen[_s] = _read_ts
        _rows.append(_mk_row(_s, _bd, _read_ts, 0))

# ── 配分 (固定上限。第1グループ=予算×G1 / 以降=残り予算) ─────────────
# ⛔ --poll では **寄った瞬間に配って発注済み**なので、ここで配り直すと
#   CSV の株数が実際に出した注文と食い違う。順位だけ振り直す。
if args.poll:
    _rows.sort(key=lambda r: (-float(r["liquidity"] or 0), str(r["symbol"])))
    for _i, r in enumerate(_rows):
        r["rank_liq"] = _i + 1
else:
    _size_groups(_rows)
_dump(_rows)

_got = sum(1 for r in _rows if float(r["open_p"] or 0) > 0)
_late_n = sum(1 for r in _rows if r["late"])
_pass_k = [r for r in _rows if r["pass_gap"]]
_j_seen = [r for r in _rows if r["in_j"]][:args.watch_j]
_pass_j = [r for r in _j_seen if r["pass_gap"]]
print(f"""
{'=' * 74}
■ 結果 — {_out_path}
{'=' * 74}
  読めた       {_got:,}/{len(_rows):,}銘柄   (最終 {_read_ts})
  09:00に未寄  {_late_n:,}銘柄 ({_late_n / max(1, len(_rows)) * 100:.1f}%)
               ⚠ バックテストの実測は15.7%。大きく違うなら要調査
  グループ     {len(_groups)}回""")
# ⛔ 前日の OpeningPrice を掴んだ銘柄。1〜2件なら「まだ寄っていないだけ」で正常。
#   大半がこれなら kabu の書式が変わった等で **全件が黙ってスキップ**されている。
_stale_n = sum(1 for r in _rows if r.get("stale_open"))
if _stale_n:
    print(f"  ⛔ 前日の始値 {_stale_n:,}銘柄 — OpeningPriceTime が今日でないので"
          f"『未寄り』として除外しました")
    if _stale_n > len(_rows) * 0.5:
        print(f"     ⛔⛔ **過半数がこれ**。kabu の OpeningPriceTime の書式が"
              f"変わった可能性。CSV の open_time 列を確認すること")
for _gi, (_gt, _gs) in enumerate(_groups):
    print(f"    {_gi + 1}. {_gt}  {len(_gs)}銘柄")
print(f"""
  合格 {len(_pass_k):,}件 → 建てる {sum(1 for r in _rows if r['lots_k'])}件 / """
      f"""投入 {sum(float(r['yen_k'] or 0) for r in _rows) / 1e4:,.0f}万
  うちJ(選定あり上位{args.watch_j}) 合格 {len(_pass_j):,}件
""")

# ★ 合格銘柄の一覧 (2026-08-17 ユーザー要望)。CSV を開かなくても分かるように。
#   ⛔ 発注リストではない。あくまで「その朝 K なら何を建てたか」の記録。
if _pass_k:
    print(f"  ── 合格銘柄 ({len(_pass_k)}件) "
          f"{'─' * 46}\n"
          f"  {'銘柄':<8}{'J':>3}{'ギャップ':>10}{'前日終値':>10}"
          f"{'約定(始値)':>12}{'損切':>10}{'利確':>10}"
          f"{'株数':>8}{'投入額':>12}{'寄り時刻':>10}")
    for _r in sorted(_pass_k, key=lambda x: -float(x.get("gap_bp") or 0)):
        # ⛔ OpeningPriceTime は "2026-08-17T09:00:03+09:00" 形式。
        #    末尾スライスだと "03+09" になるので必ず正規表現で抜く。
        _mt = re.search(r"(\d{2}:\d{2}:\d{2})", str(_r.get("open_time") or ""))
        _tm = _mt.group(1) if _mt else ("遅寄" if str(_r.get("late")) == "1"
                                        else "-")
        # ⛔ f-string の式に引用符を入れ子にしない(Python 3.11 では
        #    バックスラッシュ不可でSyntaxError)。先に文字列を作る。
        _sk = f"{float(_r['stop_k']):,.1f}" if _r.get("stop_k") else "-"
        _tk = f"{float(_r['target_k']):,.1f}" if _r.get("target_k") else "-"
        print(f"  {_r['symbol']:<8}"
              f"{'✓' if str(_r.get('in_j')) == '1' else '':>3}"
              f"{float(_r['gap_bp'] or 0):>+9.0f}bp"
              f"{float(_r['prev_close'] or 0):>10,.1f}"
              f"{float(_r['open_p'] or 0):>12,.1f}"
              f"{_sk:>10}{_tk:>10}"
              f"{int(_r['lots_k'] or 0) * 100:>7,}株"
              f"{float(_r['yen_k'] or 0):>11,.0f}円"
              f"{_tm:>10}")
    print(f"    ※ 損切 = 始値 + {args.sm}ATR / 利確 = 始値 − {args.tm}ATR"
          f"（ショートなので損切が上）。**実約定価格を基準に置く**(§18.32)")
    _ng = [r for r in _rows if r.get("guard_ng")]
    if _ng:
        print(f"  ⚠ ガード超過({args.guard_bp:+.0f}bp 超)で見送り {len(_ng)}件: "
              + ", ".join(r["symbol"] for r in _ng[:10]))
if args.execute:
    # ══════════════════════════════════════════════════════════════════
    #  ★ 未約定の新規売り指値を **必ず取り消す** (2026-08-17)
    # ══════════════════════════════════════════════════════════════════
    # バックテストの J は「09:00 の始値で建てる。建たなければその日は無し」。
    # ところが保護指値(始値×0.995)は **板に残る**ので、寄り直後に急落して
    # 刺さらなかった注文が、昼に値が戻ってきたところで約定しうる。
    # そうなると『始値で建てた』ことになっていないポジションを、モデルに
    # 無い時刻・無い値段で持つことになる(§18.9 の鉄則違反)。
    # ⛔ 一部約定(CumQty>0)は残す。取り消すと建玉だけ残って watcher が
    #    決済できなくなる。cancel_gap_orders._budget_sweep と同じ方針。
    # ⛔ 条件を _EX["n"] にしない(レビュー②)。全件が送信タイムアウトだと
    #   _EX["n"] は0のままで、**不明な実注文を残したまま終了**してしまう。
    if args.n_mode and (_N_ORD or _N_SENT or _N_UNK):
        # ⛔ 代入で上書きしない(レビュー7巡目)。発注中止で False になって
        #   いるのを settle の True が消してしまう。
        _N_SETTLE_OK[0] = _n_settle() and _N_SETTLE_OK[0]
    elif _EX["n"]:
        _ACTIVE = {1, 2, 3, 4}          # 5=終了 は対象外
        try:
            _mine = {str(r["symbol"]) for r in _rows if int(r.get("ordered") or 0)}
            _n_cxl = _n_keep = 0
            for _o in (cli.get_orders() or []):
                _sy = str(_o.get("Symbol", "")).upper() \
                    .removesuffix(".T").split(".")[0]
                if _sy not in _mine:
                    continue            # 自分が今朝出した注文だけ触る
                if int(_o.get("CashMargin") or 0) != 2:
                    continue            # 信用新規売りのみ
                if int(_o.get("OrderState") or _o.get("State") or 0) not in _ACTIVE:
                    continue
                if float(_o.get("CumQty", 0) or 0) > 0:
                    _n_keep += 1
                    continue            # 一部約定は残す
                _rr = cli.cancel_order(_o.get("ID", ""))
                _n_cxl += 1
                print(f"  ✂ {_sy} 未約定の新規売り指値を取消"
                      f"（始値で刺さらなかったため / Result={_rr.get('Result')}）",
                      flush=True)
            if _n_cxl or _n_keep:
                print(f"  [取消] 未約定 {_n_cxl}件を取消 / 一部約定 {_n_keep}件は保持",
                      flush=True)
            else:
                print(f"  [取消] 未約定の新規売り指値はありません（全部 約定済み）",
                      flush=True)
        except Exception as _ce:
            print(f"  ⛔ 未約定注文の取消に失敗: {_ce}\n"
                  f"    **板に指値が残っています**。kabuステーションで手動取消を"
                  f"確認してください（昼に約定するとモデル外の建玉になります）",
                  flush=True)
    # ★ 損切りの武装の起点(.lss_watcher_seen.json)は、発注が通るたびに
    #   _seed_arm() が書いている。ここでは書けているかを確認するだけ。
    if _EX["n"]:
        try:
            import json as _json
            _sp = Path(__file__).resolve().parent / ".lss_watcher_seen.json"
            _today = f"{_dt.date.today():%Y-%m-%d}"
            _cur = (_json.loads(_sp.read_text(encoding="utf-8")).get(_today) or {}
                    ) if _sp.exists() else {}
            _miss = [str(r["symbol"]) for r in _rows
                     if int(r.get("ordered") or 0) and str(r["symbol"]) not in _cur]
            if _miss:
                print(f"  ⛔ 武装時刻が未記録: {', '.join(_miss)}\n"
                      f"    watcher の起動時刻が起点になり、無保護窓が最大10分"
                      f"伸びます（delay4 なら 09:20 → 09:30）", flush=True)
            else:
                print(f"  [武装] 損切りの起点を **寄り時刻** で記録済み"
                      f"（{_sp.name} / {len(_cur)}件）。watcher はここから"
                      f" delay 本数ぶん後に武装します", flush=True)
        except Exception as _se:
            print(f"  ⚠ 武装時刻の確認に失敗({_se})", flush=True)
    # ══════════════════════════════════════════════════════════════════
    #  ★★ トークンを手放す前に『引け成行(MOC)』を板へ置く (2026-08-20)
    # ══════════════════════════════════════════════════════════════════
    # ⛔ ここが 2026-08-19 の事故の分かれ目だった。
    #   09:00 に建てた玉は、このスクリプトが終わる 09:10 まで **板に何も
    #   乗っていない**(kabu のトークンは1つなので watcher を並走できない)。
    #   そして 09:10 に起動した watcher が naive/aware の比較1つで即死し、
    #   **その後6時間半ずっと無防備 → 持ち越し → 強制決済**になった。
    # ★ MOC は朝に出しても大引けで約定する。**終了する前に置いてしまえば**、
    #   この後 watcher が一度も起動しなくても『その日のうちに閉じる』。
    #   watcher が正常なら 15:20 に自分の MOC を出す前にこれを見つける
    #   (moc_placed 相当の重複は kabu 側で建玉拘束になるだけで無害)。
    # ⚠ 損切り逆指値は置かない。板は1本しか使えないので、**確実に閉じる方**を
    #   優先する(損切りは watcher のポーリングが担う)。
    # ⛔ N は _n_settle() で既に置いた。ここを通すと get_positions を舐めて
    #   **他戦略・手動の売建にも MOC を出す**(2026-08-31 レビュー②)。
    if args.n_mode:
        pass
    elif not args.no_moc_on_exit:
        print(f"\n  ── 引け成行(MOC)を板に置きます "
              f"{'─' * 40}\n"
              f"  ⛔ ここで置かないと、watcher が起動するまで板は空です。"
              f"2026-08-19 はそこで持ち越しました", flush=True)
        _mn = _mok = 0
        try:
            # ⛔⛔ **銘柄単位で合算してから1回だけ出す**(2026-08-24 修正)。
            #   kabu の信用返済は『銘柄単位で建玉を自動選択し、**発注時に既存の
            #   返済注文を取消す**』。同じ銘柄が複数戦略で発火して建玉が分かれると
            #   (例 7956 = 100株 × 2)、建玉ごとに send_moc を呼ぶと
            #   **2本目が1本目の MOC を取り消して自分の100株ぶんだけ置く**ので、
            #   結果的に半分が無防備になる(2026-08-24 に実際そうなった)。
            #   lss_exit_watcher は同じ理由で既に銘柄単位に合算している。
            _bysym: dict = {}
            for _p in cli.get_positions(product=2):
                if str(_p.get("Side", "")) != "1":
                    continue
                _q = int(_p.get("LeavesQty") or _p.get("Qty") or 0)
                if _q <= 0:
                    continue
                # 一般信用デイトレ(3)だけ。制度・一般長期の売建は多日保有が
                # ありうるので触らない(panic_close と同じ絞り方)。
                if str(_p.get("MarginTradeType") or "") != "3":
                    continue
                _ps = str(_p.get("Symbol", ""))
                _e = _bysym.setdefault(_ps, {"qty": 0, "n": 0, "name": ""})
                _e["qty"] += _q
                _e["n"] += 1
                _e["name"] = _e["name"] or (_p.get("SymbolName") or "")
            for _ps, _e in _bysym.items():
                _q = int(_e["qty"])
                _mn += 1
                try:
                    _r = cli.send_moc(_ps, qty=_q, side="buy", cash_margin=3)
                except Exception as _me:
                    print(f"    ⛔ {_ps} MOC 発注で例外: {_me}", flush=True)
                    continue
                if _r.get("Result") == 0 or _r.get("_dry_run"):
                    _mok += 1
                    print(f"    ✅ {_ps} {_e['name']} {_q}株 引け成行を設置"
                          + (f" (建玉{_e['n']}本を合算)" if _e["n"] > 1 else ""),
                          flush=True)
                else:
                    print(f"    ⛔ {_ps} MOC 設置失敗: {_r}", flush=True)
        except Exception as _mne:
            print(f"    ⛔ 建玉を取得できず MOC を置けません({_mne})", flush=True)
        if _mn == 0:
            print("    (対象の売建がありません。約定していない可能性があります)",
                  flush=True)
        elif _mok < _mn:
            print(f"\n  ⛔⛔ **{_mn - _mok}件 は MOC を置けませんでした**。"
                  f"watcher が落ちると持ち越しになります。\n"
                  f"     kabu の注文照会で確認し、必要なら手で引け成行を"
                  f"出してください", flush=True)
        else:
            print(f"  ✅ {_mok}件すべてに引け成行を設置。**watcher が起動しなくても"
                  f"大引けで決済されます**", flush=True)
    print(f"""
  🚀 **発注しました** {_EX['n']}件 / 総額 {_EX['yen'] / 1e4:,.1f}万"""
          + (f" / 中止 {_EX['ng']}件" if _EX["ng"] else "")
          + ("""
  ▶ 決済は **引け成行(MOC)だけ**です。上の [MOC] 行で全件そろっているか
    確認してください。そろっていれば、この PC を閉じても大引けで決済されます。
  ⛔⛔ **watcher を起動しないでください**（.\\watch / .\\jwatch）。
     N の建玉を J のルール(delay4 / sm0.5 / tm1.0)で触ります。しかも
     ATR が無い建玉には **緊急幅1%の損切り**を置きます(§18.55 のN仕様に反する)。
  ▶ 引け後: .\\fills で実約定と突合（実滑りがここで初めて測れます）
""" if args.n_mode else f"""
  ▶ 次に **必ず** これを起動して決済させる（J は delay4）:
      python lss_exit_watcher.py --execute {'--prod ' if args.prod else ''}--all-dates --stop-delay-bars 4
    ⛔ 起動しないと **引けまで持ちっぱなし**になります。
    ⛔ kabu の有効トークンは1つ。このスクリプトが終わってから起動すること。
  ▶ 引け後: .\\fills で実約定と突合（実滑りがここで初めて測れます）
"""))
    if _EX["ng"]:
        print(f"  ⚠ 中止 {_EX['ng']}件あり。理由は上のログ(⛔行)を確認してください。")
else:
    print(f"""
  ⛔ **発注していません**（記録のみ）。出すなら --execute を付けます。
  ★ 貯めたら 5分足の始値と突合して、**板の始値 = 5分足の始値** かを確認する
    (バックテストの前提そのもの)。ズレるなら J の全数字が影響を受けます。
""")
try:
    cli.unregister_all()
    print("[k_paper] 登録を全解除しました")
except Exception:
    pass

# ★ その朝を丸ごと保存する (2026-08-18 ユーザー依頼)。
#   k_signals / k_paper は同じ日にもう一度回すと上書きされるが、板・気配の
#   履歴はどのデータにも無い(§18.35)ので失うと復元できない。
#   **書き込みは1回だけ**。既にあれば触らない(signal_history と同じ方針)。
try:
    import k_morning_archive as _kma
    _kma.archive()
except Exception as _ae:
    print(f"  ⚠ 朝の記録の保存に失敗({_ae})。"
          f"k_signals_*.csv / k_paper_*.csv は残っているので、"
          f"`python k_morning_archive.py --backfill` で作り直せます", flush=True)


# ⛔⛔ N: 決済の設置が1件でも欠けたら **非ゼロで終了する**(2026-08-31 レビュー④)。
#   ランチャー(.\nexec)が errorlevel を見て「done」を出さないようにするため。
#   ⚠ ここを黙って0で終わると、無防備な建玉があるのに成功したように見える。
if args.n_mode and args.execute and not _N_SETTLE_OK[0]:
    print("\n⛔⛔ **N の決済設置が完了していません**。\n"
          "   kabu ステーションで売建を確認し、引け成行が無いものは\n"
          "   手動で出してください。**放置すると持ち越しになります**。",
          flush=True)
    sys.exit(1)

