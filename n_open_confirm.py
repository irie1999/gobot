r"""n_open_confirm.py — 新方式N の 09:00 判定を**記録だけ**する

⛔⛔ **1円も発注しません。** k_open_confirm.py(J の実発注に使っていたもの)を
   コピーして、発注を **物理削除** した版です:

     ① cli.send_sell(...)  → 削除 (信用新規売建)
     ② cli.send_moc(...)   → 削除 (引け成行)
     ③ KabuClient(dry_run=True) で固定
     ④ --execute を渡すと **起動時にエラーで落ちる**

   板の読み取り(/board)しか叩きません。引数を間違えても発注は起こりません。
   実発注したいときは k_open_confirm.py の方を使ってください。

★ なぜ新規に書かず、使っていたコードをコピーしたのか (2026-08-26 ユーザー判断)
────────────────────────────────────────────────────────────────────
k_open_confirm.py は J の実運用3日で動作実績がある。ウォームアップ・
poll ループ・遅寄り検知・OpeningPriceTime の日付チェック(§18.48⑦)・
429 対策など、実際の朝に踏んだ問題への対処が全部入っている。
**新規に書き直すと、それを全部やり直すことになる。**

■ 何をするか

    08:5x  候補を50件バッチで登録し、**1回空読み**してウォームにする
           (⛔ 飛ばすと 09:00 の初回が 40〜50秒かかる / §18.44)
    09:00〜 --poll で回し続け、**寄った銘柄から順に**始値を取る
           → 前日終値比のギャップが **+100bp 以上**なら合格 (§18.54)
           → k_paper_<日付>.csv に全部書く
    ⛔ 発注はしない。武装時刻の記録(.lss_watcher_seen.json)も意味を持たない

■ 使い方

    # ① 前夜/早朝: 候補を作る (n_paper.py の方。日足だけ。kabu 不要)
    python n_paper.py --collect

    # ② 08:50: ウォームアップ
    python n_open_confirm.py --prod --warmup --from n_signals_<日付>.csv

    # ③ 09:00: 判定を記録
    python n_open_confirm.py --prod --poll --from n_signals_<日付>.csv

    # ④ 引け後: 終値を埋めて損益 (kabu 不要)
    python n_paper.py --close

■ J との違い

    ギャップ閾値   J: +75bp        → **N: +100bp**
    候補          J: 選定ファイル  → **N: 前日リターン ≥ +1.753%**
    サイズ        J: 資金均等      → N は記録専用なので意味を持たない
    決済          J: OCO + 引けMOC → **N: 引けMOC だけ**(バリアなし)

⚠ 以下は k_open_confirm.py 由来の説明です。発注に関する記述は
   **このファイルには当てはまりません**(全部削除済み)。
"""
from __future__ import annotations

import argparse
import csv as _csv
import datetime as _dt
import re
import sys
import time
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
ap.add_argument("--gap-bp", type=float, default=100.0,
                help="合格とするギャップ(bp)。**新方式N は 100**(§18.54)。"
                     "J は 75 だった(このスクリプトは N 用)")
ap.add_argument("--guard-bp", type=float, default=300.0,
                # ⛔ argparse の help は % 書式として展開される。Python 3.14 は
                #    add_argument の時点で検証するので、生の % があると
                #    ValueError: badly formed help string で **起動すらしない**。
                #    リテラルの % は必ず %% と書くこと(2026-08-16 に実際に落ちた)。
                help="これを超えるギャップは見送り(現行の±3%%ガード)")
# ⛔⛔ n_open_confirm は **記録専用**。--execute は受け付けるが、
#    渡されたら起動時に落とす(k_open_confirm と取り違えた事故を防ぐため)。
#    send_sell / send_moc はこのファイルから **物理削除済み**なので、
#    仮にここを通しても発注は起こらない。二重の封じ。
ap.add_argument("--execute", action="store_true",
                help="⛔ このスクリプトでは **使えません**(記録専用)。"
                     "渡すとエラーで終了します。発注は k_open_confirm.py の方で")
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
ap.add_argument("--n-mode", action="store_true", default=True,
                help="新方式N として動く(既定ON)。候補CSVの既定を "
                     "n_signals_<日付>.csv にする")
ap.add_argument("--no-futures", action="store_true",
                help="日経先物のスナップショットを記録しない")
ap.add_argument("--futures-code", type=str, default="NK225mini",
                help="記録する先物(NK225 / NK225mini / NK225micro)")
ap.add_argument("--futures-symbol", type=str, default="",
                help="銘柄コードを直接指定(自動解決できないとき)")
args = ap.parse_args()

# ⛔⛔ 記録専用であることを **起動時に確定させる**。
#    k_open_confirm.py(発注する方)と取り違えた事故を防ぐ。
if args.execute:
    raise SystemExit(
        "⛔ n_open_confirm.py は **記録専用**です。--execute は使えません。\n"
        "   send_sell / send_moc はこのファイルから物理削除してあるので、\n"
        "   仮に通しても発注は起こりません。\n"
        "   実発注したいなら k_open_confirm.py の方を使ってください。")
args.execute = False

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
# ⚠ 50件を超えるとバッチのローテーションになる。文言は f-string の入れ子に
#   できない(Python 3.11)ので、ここで組み立てておく。
_NBATCH = -(-len(_syms) // args.batch)
_BATCH_WARN = "" if len(_syms) <= args.batch else (
    "\n  ⚠ **kabu の登録上限は50件**。" + str(_NBATCH) + "バッチをローテーション"
    "するので、\n     1周目は登録し直しのぶん遅くなります"
    "(実測 50件で30〜96秒 / §18.44)。\n"
    "     **寄った銘柄は次の周から外す**ので2周目以降は速くなります。\n"
    "     板の始値は寄れば動かないので、**遅れても選定は正しく出ます**")
print(f"""
{'=' * 74}
■ J(09:00確認方式) — {_dt.date.today()}
{'=' * 74}
  {_mode_note}
  候補 {len(_syms):,}銘柄 / {args.batch}件バッチ × {_NBATCH}回{_BATCH_WARN}
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
# ⛔⛔ **dry_run=True で固定**。n_open_confirm は記録専用なので、
#    引数に何を渡しても kabu_api の発注系は API を叩かない(§12.4)。
#    さらに下で send_sell / send_moc の呼び出し自体を物理削除してある。
cli = KabuClient(prod=args.prod, dry_run=True,
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
_EX = {"left": args.budget * 1e4, "n": 0, "yen": 0.0, "ng": 0,
       "cap": (args.max_notional or args.budget) * 1e4}
_ORDER_LOG = None
if args.execute:
    try:
        from kabu_send_lss import _log_ordered as _ORDER_LOG
    except Exception as _le:
        print(f"  ⚠ _log_ordered を読めません({_le})。"
              f"**発注は行いますが ordered_signals_lss.csv に残りません** "
              f"→ watcher が決済できないので手動決済が必要です", flush=True)
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
    if _atr <= 0:
        return "ATRが無い(損切りが無効化される)"
    if not (_sk > 0 and _tk > 0):
        return "損切/利確が無い"
    if not (_sk > _op > _tk):
        return f"ショートの向きが逆(損切{_sk:,.1f} > 約定{_op:,.1f} > 利確{_tk:,.1f} でない)"
    if _qty <= 0 or _qty % 100:
        return f"株数が不正({_qty})"
    if not (0 < _lim <= _op):
        return f"保護指値が始値より上({_lim:,.1f} > {_op:,.1f})"
    return ""


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
    for _r in sorted(_sel, key=lambda x: -float(x.get("gap_bp") or 0)):
        if int(_r.get("ordered") or 0):
            continue          # 二重発注の保険(グループは1回しか通らないが念のため)
        _lot = int(_r.get("lots_k") or 0)
        if _lot <= 0:
            continue
        _qty = _lot * 100
        _op = float(_r.get("open_p") or 0)
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
        def _log(_st):
            if not (_ORDER_LOG and args.execute):
                return
            try:
                _ORDER_LOG({"symbol": _r["symbol"], "name": _r.get("name", ""),
                            "strategy": _r.get("strategy", ""),
                            "order_price": _lim,
                            "stop_price": float(_r["stop_k"]),
                            "target_price": float(_r["target_k"]),
                            "atr": float(_r["atr"]), "sm": args.sm,
                            "tm": args.tm},
                           args.prod, _qty, entry_mode="auction",
                           order_price=_lim, status=_st)
            except Exception as _we:
                print(f"    ⚠ 発注記録({_st})の書き込み失敗({_we})。"
                      f"**watcher が決済できません** → 手動で買い戻すこと",
                      flush=True)

        _log("pending")
        try:
            # ⛔⛔ **発注を物理削除** (n_open_confirm は記録専用)。
            #    元は cli.send_sell(...) で信用新規売建を出していた。
            #    ここを消してあるので、引数を何にしても1円も発注されない。
            raise RuntimeError("n_open_confirm は発注しません(記録専用)")
        except Exception as _oe:
            _EX["ng"] += 1
            print(f"  ⛔ {_r['symbol']} 発注で例外: {_oe}", flush=True)
            # ⚠ 例外の中身によっては **注文が通っている**可能性がある
            #   (送信後にタイムアウト等)。打ち消さずに pending のまま残し、
            #   watcher に守らせる。余分な記録は無害。
            print(f"     (記録は pending のまま残します。注文が通っていた場合に"
                  f"watcher が守れるように)", flush=True)
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
        _EX["n"] += 1
        _EX["yen"] += _need
        _r["ordered"] = 1
        _r["order_limit"] = _lim
        # ★ 損切りの武装の起点を **この銘柄が寄った時刻** に固定する。
        #   発注が通った直後に書く(まとめて書くと途中で落ちたとき失われる)。
        if args.execute:
            _seed_arm(_r["symbol"], _r.get("open_time"))
        print(f"  {'✓ 発注' if args.execute else '(dry-run)'} "
              f"{_r['symbol']} {_qty:,}株 保護指値 {_lim:,.0f}"
              f"(始値 {_op:,.1f} / -{args.limit_slip_bp:.0f}bp) "
              f"損切 {float(_r['stop_k']):,.1f} / 利確 {float(_r['target_k']):,.1f}"
              f" / 累計 {_EX['yen'] / 1e4:,.0f}万", flush=True)
        # ★ 発注が通ったことを記録する(pending → ordered)。
        #   ⛔ entry_mode="auction" にすると watcher が **実約定価格基準**で
        #     OCO を組み直す(§18.32 / lss_exit_watcher:355-)。J も同じ扱いで正しい。
        #   ⚠ ここが失敗しても pending が残っているので watcher は守れる。
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
# ★ 銘柄ごとの /board 送信・受信時刻(2026-09-01 レビュー)。1周に約6秒かかる
#   ので、周回の時刻を全銘柄に付けると秒単位の減衰が測れない。
#   symbol -> (req_ts, resp_ts)。各スレッドが自分のキーだけを書く。
_BOARD_TS: dict = {}


def _read_all(tag: str, pending: list | None = None) -> dict:
    """候補を50件バッチで読む。symbol -> board。

    ★ pending を渡すと **その銘柄だけ**読む(2026-08-27)。
      OpeningPrice は寄れば動かないので、一度取れた銘柄を読み直す意味は無い。
      候補が50件を超えるとき、これが無いと毎周 全バッチを読み直すことになり、
      ・1周が銘柄数に比例して伸びる
      ・バッチ切替のたびに unregister+register が走る
        (2026-08-18 に入れた『銘柄集合が変わらなければ登録し直さない』
         最適化が **完全に無効化される**)
      ・429 が積み上がる(§18.48 ⑦ で 09:00 の発注が危うくなった前例)
      1周目のあと残るのは遅寄りだけ(実測 2〜15%)なので、2周目以降は
      たいてい1バッチに収まる。
    """
    _t0 = time.time()
    _out: dict = {}
    _n_reg = 0
    _tgt = list(pending) if pending is not None else list(_syms)
    for _i in range(0, len(_tgt), args.batch):
        _b = _tgt[_i:_i + args.batch]
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
            # ★★ **銘柄ごとの** 送信/受信時刻を残す(2026-09-01 レビュー)。
            #   50件の1周に約6秒かかるので、周回終了時の1つの時刻を全銘柄に
            #   付けると、いま測りたい **秒単位の減衰がぼやける**。
            #   ⚠ 各スレッドは自分の銘柄キーだけを書くので競合しない。
            _q0 = _dt.datetime.now()
            try:
                _b_ = cli.get_board(s)
            except Exception:
                _b_ = {}
            _BOARD_TS[s] = (f"{_q0:%H:%M:%S.%f}"[:-3],
                            f"{_dt.datetime.now():%H:%M:%S.%f}"[:-3])
            return s, _b_
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
    _guard = 1 if (_gap is not None and _gap > args.guard_bp) else 0
    # ⛔ --poll では late を不合格にしない(遅寄りを拾うのが目的)。
    _lt_ng = 0 if args.poll else _late
    _pass = 1 if (_gap is not None and _gap >= args.gap_bp
                  and not _guard and not _lt_ng) else 0
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


# ★★ 気配の推移を残す (2026-09-01)。
#   k_paper_*.csv は **銘柄ごとに1行**(寄った瞬間のスナップショット)しか
#   持たないので、次の2つが判定できなかった:
#     ・その指値が 09:10 までに約定したか(最初の1枚では不約定でも後で約定する)
#     ・成行なら いくらで売れたか(= その瞬間の最良買い気配)
#   1周ごとに全銘柄を追記する。9,000行/日 程度で軽い。
#   ⛔ このファイルは **記録専用**。発注には一切使わない。
_QPATH = Path(__file__).resolve().parent / f"n_quotes_{_dt.date.today():%Y%m%d}.csv"
# ⛔ ts(周回の時刻)を秒単位の分析に使わないこと。1周に約6秒かかるので、
#   1銘柄目と50銘柄目では6秒ずれる。**req_ts / resp_ts を使う**(ミリ秒つき)。
#
# ★ 各列の意味 (2026-09-01 レビュー2巡目で訂正)
#   req_ts/resp_ts … こちらがいつ読んだか。**通信品質は resp_ts − req_ts** で見る
#   ask_*          … kabu の命名では **AskPrice が最良"買い"気配**(=売れる値段)。
#                    ask_time はその気配自身の時刻、ask_sign は気配フラグ
#                    (特別気配などの状態はここで分離する)
#   ⛔ cur_ts (CurrentPriceTime) は **最終"約定"の時刻**であって気配の時刻ではない。
#     resp_ts − cur_ts が数秒でも、最良買い気配は有効に存在し 100株 売れうる。
#     **これを鮮度ゲートに使って行を落としてはいけない**(私の誤り。補助列として残す)
#   ⛔ resp_ts − ask_time が大きい行も自動除外しない。気配が動いていないだけで
#     そのまま有効な場合がある
#   ★ buy1_*/sell1_* は Buy1/Sell1 の写し。ask_* と突き合わせれば
#     **命名の向きをデータ側で検証できる**(コメントが1年間 逆だった前例がある)
_QCOLS = ["date", "ts", "req_ts", "resp_ts", "poll", "symbol",
          "prev_close", "open_p", "open_time",
          "current_price", "cur_ts",
          "bid", "bid_qty", "bid_time", "bid_sign",
          "ask", "ask_qty", "ask_time", "ask_sign",
          "gap_bp", "pass_gap", "late", "stale_open"] + [
    # ★ 板10段。sell1 は最良"売り"気配なので buy1 との差がスプレッド。
    #   累積数量で「100株の成行がいくらで約定するか」が確定する。
    _c for _lv in range(1, 11) for _c in (
        f"buy{_lv}_price", f"buy{_lv}_qty",
        f"sell{_lv}_price", f"sell{_lv}_qty")] + [
    "buy1_time", "buy1_sign", "sell1_time", "sell1_sign",
    # ★ 成行100株の加重平均(Buy1から消化)。「成行 vs 指値」の成行側の値。
    #   ⚠ 生の板(buy1..10)が正本。この列は日々の目視と定義の統一のため。
    "mkt100_px", "mkt100_qty", "mkt100_lv"]


# ★ mkt100 の自己検査カウンタ。ask_qty>=100 の行のうち、100株が1段で
#   埋まらなかった件数。0 でなければ板の読み方にバグがある。
_QCHK = {"n": 0, "bad": 0}


def _mkt100(_bd: dict, _qty: int = 100) -> tuple:
    """**成行で _qty 株 売ったときの加重平均**。Buy1 から高値側に消化する。

    ★ これが「成行 vs 指値」の成行側の値。最良気配1本では
      「100株そこで取れるか」すら分からなかった(§18.44 の宿題)。
    ⛔ kabu の命名では Buy1 が最良"買い"気配(=こちらが売れる一番高い値)。
      板が薄くて届かなければ got < qty になり、平均は取れたぶんだけの値。
    返り値 (加重平均, 取れた株数, 使った段数)。
    ⛔⛔ got < qty は **「成行でも建てられない」ではない**(2026-09-01 レビュー)。
      板は10段しか配信されないので、これは「**上位10段だけでは100株の値段を
      算定できない**」という意味。11段目以下で約定しうる。
      → そのときは加重平均を **欠損("")** にする。途中までの平均を書くと
        実際より有利な値になり、成行の評価を甘くする。
    """
    _got, _cost, _lv = 0, 0.0, 0
    for _i in range(1, 11):
        _d = _bd.get(f"Buy{_i}")
        _d = _d if isinstance(_d, dict) else {}
        _p = float(_d.get("Price") or 0)
        _q = int(float(_d.get("Qty") or 0))
        if _p <= 0 or _q <= 0:
            break                       # 板がそこで尽きている
        _take = min(_qty - _got, _q)
        _cost += _p * _take
        _got += _take
        _lv = _i
        if _got >= _qty:
            break
    # ⛔ 100株に届かなければ価格は **欠損**。途中までの平均は書かない。
    _px = round(_cost / _got, 4) if _got >= _qty else ""
    return _px, _got, _lv


def _qdump(_bd_all: dict, _ts: str, _poll: int) -> None:
    """この周に読んだ板を1行ずつ追記する。まだ寄っていない銘柄は書かない。"""
    try:
        _new = not _QPATH.exists()
        with open(_QPATH, "a", newline="", encoding="utf-8-sig") as _f:
            _w = _csv.DictWriter(_f, fieldnames=_QCOLS, extrasaction="ignore")
            if _new:
                _w.writeheader()
            _n = 0
            for _s, _bd in (_bd_all or {}).items():
                _r = _mk_row(_s, _bd, _ts, 0)
                if float(_r.get("open_p") or 0) <= 0:
                    continue                      # まだ寄っていない
                _q0, _q1 = _BOARD_TS.get(_s, ("", ""))
                _r["ts"], _r["poll"] = _ts, _poll
                _r["req_ts"], _r["resp_ts"] = _q0, _q1
                # ⛔ cur_ts は最終**約定**の時刻。鮮度ゲートに使わない(上の注記)
                _r["cur_ts"] = str(_bd.get("CurrentPriceTime") or "")
                # ★ 気配自身の時刻とフラグ。特別気配の分離は ask_sign で行う
                for _k, _c in (("AskTime", "ask_time"), ("AskSign", "ask_sign"),
                               ("BidTime", "bid_time"), ("BidSign", "bid_sign")):
                    _r[_c] = str(_bd.get(_k) or "")
                # ★ Buy1/Sell1 の写し。ask_* と突き合わせて命名の向きを
                #   データ側で検証するため(AskPrice == Buy1.Price のはず)。
                # ★★ **板の厚みを10段ぶん残す**(2026-09-01)。これまで最良気配
                #   1本しか保存しておらず、板の応答には入っていたのに捨てていた。
                #   これがあると 1件の観測から次が出せる:
                #     ・100株を成行で売ったら **いくらで約定するか**(累積数量で確定)
                #     ・スプレッド(buy1 と sell1 の差)
                #     ・気配が厚いのか薄いのか(=その値段が本物か)
                #     ・株数を増やしたときの滑り(将来 予算を上げる判断に使える)
                #   発火数(1日2〜6件)は増やせないので、**1件あたりの情報量**を上げる。
                for _lv in range(1, 11):
                    for _k, _p in ((f"Buy{_lv}", f"buy{_lv}"),
                                   (f"Sell{_lv}", f"sell{_lv}")):
                        _d = _bd.get(_k)
                        _d = _d if isinstance(_d, dict) else {}
                        _r[f"{_p}_price"] = _d.get("Price") or ""
                        _r[f"{_p}_qty"] = _d.get("Qty") or ""
                        if _lv == 1:
                            _r[f"{_p}_time"] = str(_d.get("Time") or "")
                            _r[f"{_p}_sign"] = str(_d.get("Sign") or "")
                (_r["mkt100_px"], _r["mkt100_qty"],
                 _r["mkt100_lv"]) = _mkt100(_bd, 100)
                # ★★ 自己検査(2026-09-01 レビュー提案)。最良買い気配の数量が
                #   100株以上あるなら、100株は **1段目だけ**で埋まるはず。
                #   2段目以降を消化していたら、板の展開か数量の読み方がバグ。
                #   既存25件は全部 ask_qty>=100 だったので、初日に必ず効く。
                if float(_r.get("ask_qty") or 0) >= 100:
                    _QCHK["n"] += 1
                    if int(_r["mkt100_lv"] or 0) > 1:
                        _QCHK["bad"] += 1
                        if _QCHK["bad"] == 1:
                            print(f"  ⛔⛔ **板の読み方がおかしい**: {_s} は"
                                  f" 最良買い気配 {_r['ask']} × {_r['ask_qty']}株"
                                  f"(100株以上)なのに、100株の消化に"
                                  f" {_r['mkt100_lv']}段 使っています。\n"
                                  f"     buy1={_r.get('buy1_price')}"
                                  f"×{_r.get('buy1_qty')} / "
                                  f"buy2={_r.get('buy2_price')}"
                                  f"×{_r.get('buy2_qty')}\n"
                                  f"     → Buy1..Buy10 の展開か数量の単位を"
                                  f"疑ってください", flush=True)
                _w.writerow(_r)
                _n += 1
        if _poll == 1:
            print(f"  [気配] {_QPATH.name} に毎周 追記します"
                  f"(この周 {_n}件)", flush=True)
            if _QCHK["n"]:
                print(f"  [検査] 最良気配に100株以上ある {_QCHK['n']}件のうち、"
                      f"100株が1段で埋まらなかった **{_QCHK['bad']}件**"
                      + ("  ← 0 が正常" if not _QCHK["bad"]
                         else "  ⛔ **板の読み方を疑うこと**"), flush=True)
    except Exception as _qe:
        # ⚠ 記録だけの機能なので、失敗しても本処理は止めない。
        print(f"  ⚠ 気配の記録に失敗({_qe})", flush=True)


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
# ══════════════════════════════════════════════════════════════════════
# ★★ 日経先物のスナップショット (2026-08-28 追加)
#   **大阪の日中セッションは 08:45 開始 = 現物の15分前**。
#   その15分間の値動きは日足のどこにも無く、§18.60 の19変数にも入っていない。
#   バックテストできないので **今日から貯めるしかない**(§18.35 と同じ形)。
#   ⛔ 照会のみ。発注はしない。失敗しても本体の記録は絶対に止めない。
_FUT_ROWS: list = []
_FUT_SYM = ""
_FUT_EX = 23        # 大阪 日中セッション(08:45 開始)


def _fut_snap(tag: str) -> None:
    """先物の板を1回読んで記録する。例外は握り潰す(本体を止めない)。"""
    global _FUT_SYM
    if args.no_futures:
        return
    try:
        if not _FUT_SYM:
            _FUT_SYM = args.futures_symbol or cli.resolve_future(
                args.futures_code, 0)
            if not _FUT_SYM:
                print("  ⚠ 先物の銘柄コードを解決できません(記録をスキップ)",
                      flush=True)
                args.no_futures = True
                return
            print(f"  [先物] {args.futures_code} = {_FUT_SYM} "
                  f"(大阪 日中 / 08:45 開始)", flush=True)
        b = cli.get_board(_FUT_SYM, _FUT_EX) or {}
        _FUT_ROWS.append({
            "ts": _dt.datetime.now(
                _dt.timezone(_dt.timedelta(hours=9))
            ).isoformat(timespec="seconds"),
            "tag": tag, "symbol": _FUT_SYM,
            "price": b.get("CurrentPrice"), "open": b.get("OpeningPrice"),
            "high": b.get("HighPrice"), "low": b.get("LowPrice"),
            "prev_close": b.get("PreviousClose"),
            "bid": b.get("BidPrice"), "ask": b.get("AskPrice"),
            "volume": b.get("TradingVolume"),
        })
    except Exception as _e:
        print(f"  ⚠ 先物の記録に失敗（本体は継続）: {_e}", flush=True)


print("\n▶ ウォームアップ（空読み。値は使いません）", flush=True)
_fut_snap("warm")
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
    _fut_snap("open")          # ★ 09:00 直前/直後の先物
    print(f"\n▶ ポーリング開始（{args.every}秒ごと / {args.poll_until} まで）",
          flush=True)
    _n_poll = 0
    while True:
        _t0 = time.time()
        _n_poll += 1
        _pend = [x for x in _syms if x not in _seen]      # 表示用(本当に未取得)
        # ★★ 合格銘柄は **寄った後も読み続ける**(2026-09-01)。
        #   これまでは寄った瞬間の1枚しか残っていなかったので、
        #   「その指値が 09:10 までに約定したか」を後から判定できなかった。
        #   実例: 5301(2026-09-01)は最初の気配 1,835.5 では w=0(指値1,836)が
        #   約定不能だったのに、**その後 実際に約定**している。1枚だけ見ると
        #   約定率を過小に見積もる。
        #   ⛔ 1バッチに収まるなら pending を渡さない。_b が毎周おなじになり
        #     **登録し直しが起きない**(ライブ実測: 登録0回 / 50件 6.2秒)。
        #     pending が要るのは候補が50件を超えるとき(多バッチの切替削減)。
        _track = [r["symbol"] for r in _rows if int(r.get("pass_gap") or 0)]
        _tgt = (None if len(_syms) <= args.batch
                else _pend + [x for x in _track if x not in _pend])
        _bd_all = _read_all(f"poll{_n_poll}", pending=_tgt)
        _now_s = f"{_dt.datetime.now():%H:%M:%S}"
        _qdump(_bd_all, _now_s, _n_poll)
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
                  f"(通算 {len(_seen)}/{len(_syms)} / 今周は未取得 "
                  f"{len(_pend)}件 = {-(-len(_pend) // args.batch)}バッチ) "
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
            print(f"  [{_now_s}] 新規なし (通算 {len(_seen)}/{len(_syms)} / "
                  f"今周は未取得 {len(_pend)}件 = "
                  f"{-(-len(_pend) // args.batch)}バッチ) "
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
    if _EX["n"]:
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
    if not args.no_moc_on_exit:
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
                    # ⛔⛔ **発注を物理削除**。元は cli.send_moc(...)。
                    raise RuntimeError("n_open_confirm は発注しません")
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
          + f"""
  ▶ 次に **必ず** これを起動して決済させる（J は delay4）:
      python lss_exit_watcher.py --execute {'--prod ' if args.prod else ''}--all-dates --stop-delay-bars 4
    ⛔ 起動しないと **引けまで持ちっぱなし**になります。
    ⛔ kabu の有効トークンは1つ。このスクリプトが終わってから起動すること。
  ▶ 引け後: .\\fills で実約定と突合（実滑りがここで初めて測れます）
""")
    if _EX["ng"]:
        print(f"  ⚠ 中止 {_EX['ng']}件あり。理由は上のログ(⛔行)を確認してください。")
else:
    print(f"""
  ⛔ **発注していません**（記録のみ）。出すなら --execute を付けます。
  ★ 貯めたら 5分足の始値と突合して、**板の始値 = 5分足の始値** かを確認する
    (バックテストの前提そのもの)。ズレるなら J の全数字が影響を受けます。
""")
# ★ 先物のスナップショットを保存(§18.62)。**バックテストできないので貯めるしかない**
if _FUT_ROWS:
    try:
        _fp = f"futures_snap_{_dt.date.today():%Y%m%d}.csv"
        _cols = ["ts", "tag", "symbol", "price", "open", "high", "low",
                 "prev_close", "bid", "ask", "volume"]
        with open(_fp, "w", newline="", encoding="utf-8-sig") as _fh:
            _w = _csv.DictWriter(_fh, fieldnames=_cols)
            _w.writeheader()
            _w.writerows(_FUT_ROWS)
        _p0 = next((r for r in _FUT_ROWS if r["tag"] == "warm"), None)
        _p1 = next((r for r in _FUT_ROWS if r["tag"] == "open"), None)
        print(f"[先物] {len(_FUT_ROWS)}点 → {_fp}")
        if _p0 and _p1 and _p0.get("price") and _p1.get("price"):
            _mv = (float(_p1["price"]) / float(_p0["price"]) - 1.0) * 100.0
            print(f"  ★ ウォームアップ({_p0['ts'][11:19]}) {_p0['price']} → "
                  f"09:00直前({_p1['ts'][11:19]}) {_p1['price']} = "
                  f"**{_mv:+.3f}%**")
            print(f"  ⚠ これは **どの日足にも無い情報**です"
                  f"(大阪の日中セッションは08:45開始 = 現物の15分前)。"
                  f"数ヶ月貯めてから検定します")
    except Exception as _e:
        print(f"  ⚠ 先物の保存に失敗: {_e}")

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
