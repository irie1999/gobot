r"""analyze_gap_edge.py — 「上げて寄った銘柄は、その日 下げるか」を最も安全な形で測る

════════════════════════════════════════════════════════════════════════════
★★★ 事前宣言 (2026-08-25)。**測る前に書いた。以後この基準を動かさない。**
════════════════════════════════════════════════════════════════════════════

これまで負けてきた原因は「測ってから良かった方を採る」を繰り返したこと。
方式を4回(lss→E→H→J / 2026-08-10〜16)、パラメータを数十回。その多重検定を
帰無較正していないので、勝った設定が本物かノイズか区別がつかなくなった。

**今回は測る前に合格ラインを決め、それをこのファイルに埋め込む。**
結果を見てからここを書き換えたら、この測定は無効になる。

── 仮説 (1つだけ) ──────────────────────────────────────────
  **ギャップアップして寄った銘柄は、その日の日中(寄り→引け)に下げる。**

  ⚠ 2026-08-25 の第1回は「**シグナルが出た銘柄が**」という条件付きで測り、
    不合格だった(§18.52)。そのとき同時に測った対照群(シグナルなし 506,547銘柄日)
    が**同じ形をしており、TESTでは対照のほうが良い帯が3つ**あった。
    → **シグナルは何も足していない**ので、条件を外して仮説そのものを縮めた。
    これは基準を緩めたのではなく、**測定が「シグナルは不要」と示したため**。

── 測るもの ────────────────────────────────────────────────
  C = (D+1 の始値 − D+1 の終値) × 100株     ← ショート。**バリアを1つも持たない**
      sm / tm / delay / 資金均等 / 予算 / 発注順 / 選定 / **シグナル** が全部無い。
      だから **パラメータ選択のバイアスが構造的に入らない**。

── 実運用の形 (--exec auction) ─────────────────────────────
  前夜に「前日終値 × (1 + N/10000)」の **寄付指値**を空売りで置くだけ。
  ギャップが N を超えた日だけ **板寄せで約定 = 約定価格は始値**。
  板寄せは呼値を払わないので **エントリーの執行コストはゼロ**。
  引けの買い戻し(MOC)で片道1ティック(≒3.3bp)だけ払う。
  ⚠ 09:00 に板を読んでから発注する形(--exec confirm)は実測 **−30.9bp**
    (2026-08-24 / 5件)。J が負けていた主因はここ。

── 母集団 (lss を1ミリも通らない) ──────────────────────────
  6戦略(MACDTF/A7/RSI2/DON/VOLTF/MOM)の `entry_sig` が True の日 D をすべて。
  **lss が約定したかどうかと無関係**。ここが §18.50 で汚染された部分:
    - `run_limit_backtest` の trade_log は「発注中」を捨てる
    - `scan_lss_universe._scan_symbol` も `reason in ("発注中","保有中")` を捨てる
    - `eh_trades` は trades+nofills から拾うので、期限切れが落ちていた
  → **どれも「ギャップアップして前日終値まで戻らなかった日」= ショートに有利な日
    を系統的に落としていた**。本ツールはシグナルから直接作るので起こりえない。

── 対照群 (これが無いと何も言えない) ───────────────────────
  同じ銘柄の **シグナルが出ていない日**。同じギャップ帯で同じ計算。
  §18.19 で「無条件の日中ドリフトはゼロ」(604,626銘柄日 / t=0.59)と確定済み。
  だから対照がプラスに出たら、それは測定の誤りかレジームであって仮説の支持ではない。

════════════════════════════════════════════════════════════════════════════
★ 合格ライン — **5つ全部**を満たしたときだけ「土台がある」と言う
════════════════════════════════════════════════════════════════════════════

  ① bp/件 ≥ **執行コストの3倍**。--exec で自動的に決まる:
       auction (前夜 寄付指値) … 板寄せは呼値を払わない。引けMOCの片道 3.3bp だけ
                                  → **PASS_BP = 10.0**
       confirm (09:00に板を読んでから発注) … 実測 −30.9bp (2026-08-24 / 5件)
                                  → **PASS_BP = 35.0**
       ⚠ 第1回(§18.52)は confirm を前提にしながら 15.0 と置いた。**甘かった**。
         執行方式ごとに水準が変わるのは当然なので、**方式を引数にして測る前に
         紐付けた**。結果を見てから水準をいじることはしない。
       ⚠ 呼値は下限。実スプレッドはこれ以上。

  ② 日クラスタ頑健 t ≥ +2.0
       同じ日の全銘柄はその日の方向を共有するので、実効サンプルは
       件数ではなく **営業日数**(§18.13)。件数ベースの t は使わない。

  ③ **既見期間と未使用期間の両方**で ①② を満たす
       ★★ ここが第2回の肝。**日足は2020-09から6年ある**(5分足の2年制約は
          同日決済を5分足で判定していたときのもの。寄りで建てて引けで返すだけなら
          日足の始値・終値で足りる)。

            2024-03 〜 2026-08  … 第1回で見た = **既見**
            2020-09 〜 2024-03  … **一度も見ていない = 本物のホールドアウト(3.5年)**

          ⛔ 第1回で「75〜150bp が良い」という表を見てしまった以上、
             既見期間で N を選ぶのは in-sample。未使用期間が唯一の検証手段。
       ⚠ ホールドアウトは **上限で切る**(§18.25 の事故: 下限しか動かさず
         TRAIN ⊇ TEST になっていた)。窓を変えて数字が動くことを必ず確認する。
       ⚠ 2020-2021 はコロナ相場でレジームが違う。落ちたとき「レジームのせい」と
         言い訳しないために、**先に「落ちたら不合格」と決めておく**。

  ④ ギャップ帯で単調
       帯を7つ切れば偶然どれかは良く見える。**特定の帯を選ぶのは多重検定**。
       単調でなければ軸として機能していない(§18.31 の流動性・§18.38 の建値が
       まさにそうだった)。Spearman ≥ +0.7 を単調とみなす。

  ⑤ 帰無較正の95%点を超える
       帰無は **同じ日の中で帯ラベルをシャッフル**(日効果を完全に保つ / §18.48⑪)。
       日をまたぐシャッフルは日内相関を壊して帰無分布が狭くなり、偽陽性を
       過小評価する(§18.13)。
       ⚠ 第1回の「対照群(シグナルなし)がゼロ」は、シグナルの寄与を測るための条件。
         シグナルを土台から外したので、この条件は役目を終えた。代わりに ③ を
         『既見 vs 未使用』に強化してある(条件は減っていない)。
         シグナル有無の比較は **参考として出し続ける**(§18.52 の再確認)。

  ⛔ 1つでも落ちたら **不合格。パラメータを足して救わない。**
     救おうとした瞬間に、これまでと同じ多重検定が始まる。

════════════════════════════════════════════════════════════════════════════
★ 試行の記録 — `gap_edge_trials.csv` に**自動で追記される**
════════════════════════════════════════════════════════════════════════════
  多重検定の補正には「何回試したか」が要る。これまで記録していなかったので
  補正できなかった(方式4回 / パラメータ数十回 / §18.51 B2)。
  **本ツールは実行のたびに1行書く。消さないこと。**
  10回試して1回通ったなら、その1回は帰無でも起こりうる(1 − 0.95^10 = 40%)。

════════════════════════════════════════════════════════════════════════════
⚠ このツールが測っていないもの (合格しても残る未知数)
════════════════════════════════════════════════════════════════════════════
  * スプレッド・執行遅延 — §18.44 の実測で 1分 −15.8bp / 5分 −36.6bp。
    C の bp から**引く**こと。バックテストでは永遠に確定しない。
  * 空売り在庫・貸株可否 — 今日のリストしか無い(§18.51 B5)。
  * 生存バイアス — 今日の上場銘柄しか見ていない(§18.51 B6)。
  * 決算・特別気配・IPO — 全部建てる前提。

════════════════════════════════════════════════════════════════════════════
★★ 第3回 (2026-08-25 / 回す前に宣言。以後 書き換えない)
════════════════════════════════════════════════════════════════════════════
  第2回も **全帯合計**で判定してしまった(母集団の39%がギャップダウン = 仮説に
  含まれない集団)。第1回は「閾値をどこに置いても両窓は通らない」と確認できたので
  判定は変わらなかったが、**第2回は変わる**(100〜150bp が未使用・既見の両方で
  bp≥9.9 かつ t≥2.0 を満たした)。だから閾値を持つ形を**初めて事前宣言して測る**。

  仮説   : 寄りが前日終値 **+100bp 以上**の銘柄を空売りし、**引けで買い戻す**と勝つ
  閾値   : **100bp 固定。スイープしない。**
           ⚠ これは第1回・第2回で見た表から決めた値。**既見期間では検証できない。**
  母集団 : シグナル不問(2回とも「シグナルの寄与ゼロ」と出た)
  執行   : 前夜に寄付指値 → 板寄せ約定 → 引けMOC。執行コスト 3.3bp
  検証   : **2015-05 〜 2020-09 (5.4年)。一度も見ていない。**
           第1回=2024-03以降 / 第2回=2020-09以降 しか見ていない。

  合格 (全部):
    ① bp/件 ≥ 9.9 (執行3.3bp×3)
    ② 日クラスタ頑健 t ≥ 2.0
    ④ **検証期間の前半・後半で両方プラス**(閾値ありだと帯が2つしか残らず
       単調性 ρ が意味をなさないので、④をこれに差し替える。条件は減っていない)
    ⑤ 帰無較正の95%点を超える

  ⛔ 落ちたら **終わり**。閾値を動かして再挑戦しない。
  ⛔ これを回すと **手持ちの未使用データは尽きる**。次は前向きに貯めるしかない。
  ⛔ 2015-2020 には **コロナショック(2020-03)** が入る。落ちたときに
     「相場が特殊だった」と言わない。先にそう決めた。

  コマンド:
    python analyze_gap_edge.py --workers 8 --days 4200 --min-gap-bp 100 \
                               --split 2020-09-01,2024-03-01

使い方
------
  ★ 第2回(ギャップだけを土台にする / 未使用期間で検証):
      python analyze_gap_edge.py --workers 8 --days 2100 --split 2024-03-01

  参考:
  python analyze_gap_edge.py --workers 8 --pool sig      # 第1回と同じ(シグナル限定)
  python analyze_gap_edge.py --workers 8 --exec confirm  # 09:00に板を読む形
  python analyze_gap_edge.py --workers 8 --limit 200     # デバッグ

⚠ `--days 2100` はキャッシュ(.rsi2_cache)がそのまま使える。fetch は
  (days+230)×1.5 日を取るので、第1回(--days 800)の時点で既に 2020-09 まで
  入っている。再ダウンロードは起きない。

⚠ 照会のみ。発注も設定変更もしない。日足だけで完結する(5分足不要)ので軽い。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

# ── 事前宣言した合格ライン。**結果を見てから書き換えない** ──────────────
# ① は執行方式で決まる(執行コストの3倍)。方式は --exec で選ぶ。
EXEC_COST_BP = {
    # 前夜に寄付指値 → 板寄せ約定。エントリーは呼値を払わない。引けMOCの片道のみ
    "auction": 3.3,
    # 09:00に板を読んでから発注 → 実測 −30.9bp (2026-08-24 / 5件 / §18.48⑩)
    "confirm": 30.9,
}
PASS_T = 2.0          # ② 日クラスタ頑健 t の下限
PASS_RHO = 0.7        # ④ ギャップ帯の単調性 (Spearman)
TRIALS_CSV = "gap_edge_trials.csv"   # 試行の記録(多重検定の補正に使う)

# ギャップ帯 (bp)。J の合格判定 +75bp をまたぐように切ってある。
GAP_EDGES = [(-1e9, 0.0), (0.0, 25.0), (25.0, 50.0), (50.0, 75.0),
             (75.0, 100.0), (100.0, 150.0), (150.0, 1e9)]

ap = argparse.ArgumentParser(
    description="ギャップアップ後の日中ショートに地力があるかを、lss を通さず測る")
ap.add_argument("--days", type=int, default=2100,
                help="遡及日数(既定2100=約6年。キャッシュがそのまま使える)")
ap.add_argument("--split", type=str, default="2024-03-01",
                help="期間の境界 yyyy-MM-dd。**カンマ区切りで複数可**"
                     "(例 2020-09-01,2024-03-01 → 3期間)。"
                     "**判定は最も古い窓に対して行う**(そこが未使用期間)。空なら分割しない")
ap.add_argument("--min-gap-bp", type=float, default=0.0,
                help="判定対象をこのギャップ以上に絞る(bp)。"
                     "0=絞らない。**仮説が『ギャップアップした銘柄』なら必ず指定する**"
                     "(第1回・第2回はこれを付けず、母集団の39%%を占める"
                     "ギャップダウンを混ぜて薄めていた)")
ap.add_argument("--rank-seeds", type=str, default="42,1,7,99,123,2024,5,17,31,64,88,101",
                help="発注順の判定に使うランダムのシード(§18.24 の帯)")
ap.add_argument("--side", choices=["short", "long", "both"], default="short",
                help="short(既定) = 前日上げ × ギャップアップ を空売り(= N)。"
                     "long = その **完全な鏡像**(前日下げ × ギャップダウンを買う)。"
                     "符号を全部反転するだけなので、閾値もスイープも判定も"
                     "そのまま使える。"
                     "both = **両方を1つの予算・1つの watch50 で回す**"
                     "(--sweep-ops 専用)。日次の相関とσも出す")
ap.add_argument("--max-gap-bp", type=float, default=0.0,
                help="ギャップの **上限** bp(0=上限なし)。§18.53 の帯別表で "
                     "150bp〜 は +5.3bp と 100〜150bp(+11.8bp)の半分以下だった。"
                     "極端なギャップは本物のニュースで続伸しやすい")
ap.add_argument("--pool", choices=["all", "sig", "nosig"], default="all",
                help="判定に使う母集団。all=シグナル不問(第2回の土台) / "
                     "sig=シグナルが出た日だけ(第1回と同じ) / nosig=出ていない日だけ")
ap.add_argument("--exec", dest="exec_mode", choices=["auction", "confirm"],
                default="auction",
                help="執行方式。auction=前夜に寄付指値(板寄せ約定・コスト3.3bp) / "
                     "confirm=09:00に板を読んでから発注(実測30.9bp)。"
                     "**合格ライン①がこれで決まる**")
ap.add_argument("--min-price", type=float, default=1000.0, help="建値の下限")
ap.add_argument("--max-price", type=float, default=6000.0, help="建値の上限")
ap.add_argument("--qty", type=int, default=100)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ(0=全部)")
ap.add_argument("--seeds", type=int, default=200, help="帰無較正の本数")
ap.add_argument("--seed", type=int, default=42)
ap.add_argument("--out", type=str, default="", help="銘柄日の明細をCSVに書く")
ap.add_argument("--dump-only", action="store_true",
                help="CSVを書くだけ。合否を出さず、試行回数にも数えない。"
                     "export_intraday_cache の --pairs を作るときはこれ")
ap.add_argument("--note", type=str, default="",
                help="試行記録(gap_edge_trials.csv)に残すメモ。何を試したのか")
ap.add_argument("--explore", action="store_true",
                help="★ 探索モード。**TRAIN(最も古い窓)だけ**で選別軸を掃く。"
                     "TEST は集計すらしない(誤って見ないためのガード)。"
                     "判定は出さない — 出た候補を --confirm で1回だけ検証する")
ap.add_argument("--axes", type=str, default="",
                help="探索する軸(カンマ区切り)。空なら全部。名前は --list-axes で確認")
ap.add_argument("--list-axes", action="store_true", help="探索できる軸を並べて終了")
ap.add_argument("--nq", type=int, default=5, help="軸を何分位に切るか(既定5)")
ap.add_argument("--axis-seeds", type=int, default=50,
                help="軸探索の帰無較正の本数(既定50。軸×分位ぶん回るので重い)")
ap.add_argument("--min-ret1", type=float, default=0.0,
                help="★ 前日リターン%% の下限。**N の母集団そのもの**(1.753)を指定すると、"
                     "『ret1 を固定した **残差** にまだ軸があるか』を探索できる。"
                     "⛔ これまでの --explore はこれを掛けずに回していたので、"
                     "『ret1 が最強』までしか見ていなかった。既定0=掛けない")
ap.add_argument("--sweep-barrier", action="store_true",
                help="★ 損切り/利確を **TRAIN だけ**で掃く。"
                     "『効果がない』の確認は TRAIN で完結する(TRAIN で効かない"
                     "ものが TEST で効くことは期待できない / §18.13)ので、"
                     "**TEST を1回も使わない**")
ap.add_argument("--sm-list", type=str, default="0,0.1,0.3,0.5,1.0,2.0",
                help="損切りATR倍率(0=損切りなし=現行)")
ap.add_argument("--tm-list", type=str, default="0,0.5,1.0,2.0",
                help="利確ATR倍率(0=利確なし=現行)")
ap.add_argument("--sweep-grid", action="store_true",
                help="★ 前日リターン × ギャップ の **2次元**を TRAIN だけで掃く。"
                     "現行(1.753%% × 100bp)は2つを**別々に**決めたので、"
                     "組み合わせとして最適かは見ていない")
ap.add_argument("--ret1-list", type=str, default="0,0.5,1.0,1.5,1.753,2.5,3.5",
                help="前日リターン下限の候補(%%)")
ap.add_argument("--gap-list", type=str, default="50,75,100,150,200,300",
                help="ギャップ下限の候補(bp)")
ap.add_argument("--sweep-ops", action="store_true",
                help="★ 運用パラメータ(watch上限 / 予算 / 1日の建玉数上限 / "
                     "同一銘柄の連日)を TRAIN だけで掃く")
ap.add_argument("--both-orders", action="store_true",
                help="両方触れた日を『利確優先(楽観)』でも集計して並べる。"
                     "既定は **損切り優先(悲観)のみ**"
                     "(sameday5m_firsttouch と同じ / §18.51 C)")
ap.add_argument("--stop-slip-pct", type=float, default=0.0,
                help="損切り発動時のスリッページ(0.005=0.5%%不利)。"
                     "日足ではギャップ幅が分からないので、悲観側の下限を"
                     "手で置くためのつまみ。既定0=ラインちょうど(楽観)")
ap.add_argument("--ops-gap-list", type=str, default="100,125,150,200",
                help="--sweep-ops で掃くギャップ閾値(bp)。⛔ 母集団は"
                     " --min-gap-bp で切ってあるので、それ未満は測れない。"
                     "下げる方向を見るなら --min-gap-bp 25 --ops-gap-list "
                     "25,50,75,100,125 のように **母集団を先に広げる**こと。"
                     "空文字で無効")
ap.add_argument("--ops-gap-ref", type=float, default=100.0,
                help="--sweep-ops のギャップ表で **比較の基準にする閾値**(bp)。"
                     "既定100 = N の本番設定。対応検定はこの行との差で出す。"
                     "⛔ --min-gap-bp(母集団の下端)とは別物")
ap.add_argument("--sweep-relax", action="store_true",
                help="★★ 「絞る」ではなく「**増やす**」。別の軸が強い銘柄だけ"
                     "ギャップ閾値を下げて拾えるか。"
                     "⛔ N は件数不足(稼働率40%%)なので、フィルタで bp を上げても"
                     "総額は落ちる。足せるかを見る。TRAIN のみ")
ap.add_argument("--relax-axis", type=str, default="up_streak",
                help="--sweep-relax で使う軸(--list-axes で一覧)")
ap.add_argument("--relax-axis-list", type=str, default="0,1,2,3,4,5,6",
                help="--relax-axis の閾値(この値以上を追加対象にする)。"
                     "⛔ **0 は『条件なし』のコントロール行**。これが無いと"
                     "『軸が効いた』のか『gap 閾値を下げただけ』なのか判別できない")
ap.add_argument("--relax-gap-list", type=str, default="25,50,60,75,90",
                help="緩めたギャップ閾値(bp)")
ap.add_argument("--dump-picks", type=str, default="",
                help="★ その日実際に建てた明細(銘柄/株数/建値)を CSV に書き出す。"
                     "ポートフォリオ損切りの検証(analyze_portfolio_stop.py)に使う。"
                     "TRAIN/TEST の両方を1つのファイルに出す(win 列で区別)")
ap.add_argument("--hedge", action="store_true",
                help="★★ **同日ヘッジ**(寄りで日経先物を買い、引けで売る)で"
                     "σ を削れるか。⛔ これは予測ではない(当日の相場を当てる"
                     "必要がない)ので §18.60 の壁を回避する。"
                     "TRAIN で比率を決めて TEST で答え合わせ")
ap.add_argument("--hedge-ratios", type=str, default="0,0.25,0.5,0.75,1.0,1.25",
                help="ヘッジ比率(1.0 = TRAIN の β を丸ごと打ち消す)")
ap.add_argument("--hedge-cost-bp", type=float, default=1.0,
                help="先物の往復コスト(bp / 建玉に対して)。既定1.0")
ap.add_argument("--tail-diag", action="store_true",
                help="★★ **大負けの日を寄り前に読めるか**を、順番を追って測る。"
                     "①事後(同日の相場)でどこまで説明できるか=予測可能性の上限 "
                     "→ ②寄り前変数→同日の相場 → ③寄り前変数→N の損益(単変量/"
                     "裾特化/多変量)。TRAIN で作って TEST で答え合わせ。"
                     "⛔ 一晩かける想定。--tail-nulls で帰無の本数を増やせる")
ap.add_argument("--tail-nulls", type=int, default=2000,
                help="--tail-diag の帰無較正(日ブロックを保った巡回シフト)の本数")
ap.add_argument("--tail-pairs", action="store_true", default=True,
                help="--tail-diag で2変数の交互作用も掃く(既定ON)")
ap.add_argument("--no-tail-pairs", dest="tail_pairs",
                action="store_false",
                help="交互作用を掃かない(速くしたいとき)")
ap.add_argument("--tail-pair-min", type=int, default=60,
                help="交互作用のセルに要求する最小日数")
ap.add_argument("--tail-q", type=float, default=0.10,
                help="『大負けの日』の定義(下位何割か)。既定0.10=下位10%%")
ap.add_argument("--sweep-market", action="store_true",
                help="★★ 前日の日経・S&P500・VIX・為替など **寄り前の外部指標**で"
                     "『その日は建てない』ルールが作れるか。総当たり+帰無較正。"
                     "⚠ §18.34b は lss で候補ゼロだったが **113営業日しか"
                     "なかった**。N は2,843営業日ある")
ap.add_argument("--sweep-size", action="store_true",
                help="★★ **マイナス月を抑えられるか**。サイジング × 1日の件数上限"
                     "を掃いて、月次σ・最悪月・マイナス月数で見る。"
                     "TRAIN で選んで TEST で答え合わせ")
ap.add_argument("--search-switch", action="store_true",
                help="★★ N と鏡像の使い分けルールを **総当たり**して、"
                     "帰無(日ブロックを保った巡回シフト)で較正する。"
                     "TRAIN と TEST を並べて出す。"
                     "⛔ 『最良を選ぶ』操作ごと帰無に入れるので、"
                     "何通り試しても比較は公平")
ap.add_argument("--switch-nulls", type=int, default=200,
                help="--search-switch の帰無較正の本数")
ap.add_argument("--sweep-regime", action="store_true",
                help="★ 相場の状態(前夜に確定)で N と鏡像を使い分けられるか。"
                     "⛔ TRAIN のみ。TEST は使わない")
ap.add_argument("--regime-seeds", type=str, default="42,1,7,99,123,2024,5,17",
                help="--sweep-regime の帰無較正(巡回シフト)の本数")
ap.add_argument("--confirm-both", action="store_true",
                help="★★ **TEST を1回だけ使って『両建て(N+鏡像)』を検証する**。"
                     "合格条件はコードに焼き込んであり(_BOTH_PASS)、"
                     "実行時に変更できない。⛔ 1回しか使えない")
ap.add_argument("--confirm", type=str, default="",
                help="★ 検証モード。'軸:分位' を指定して **TEST で1回だけ**測る"
                     "(例 atr_pct:Q1 / dow:0)。⛔ 1つの候補につき1回だけ")
ap.add_argument("--budget-man", type=float, default=400.0,
                help="--confirm のとき月別の実額を出す予算(万円)。0で出さない")
ap.add_argument("--months", type=int, default=24,
                help="月別を何ヶ月ぶん表示するか(既定24。0=全部)")
ap.add_argument("--no-refetch", action="store_true",
                help="キャッシュが --days ぶん遡っていなくても再ダウンロードしない。"
                     "⛔ 古い窓のデータが欠けたまま測ることになる(第3回の事故)")
ap.add_argument("--min-density", type=float, default=0.10,
                help="判定窓の1日あたり銘柄日数が、最も濃い窓の何倍を下回ったら"
                     "『測定不能』にするか(既定0.10=1/10)")
a = ap.parse_args()

# ⛔ 数値リストの引数は **スキャンの前に** 検証する。
#   スキャンは1,540銘柄 × 数千日で10分級。末尾に `]` が紛れただけで
#   10分走ってから ValueError で落ちるのは無駄が大きい(2026-08-27 に発生)。
def _numlist(_v: str, _name: str) -> list[float]:
    _out = []
    for _x in str(_v).split(","):
        _x = _x.strip()
        if not _x:
            continue
        try:
            _out.append(float(_x))
        except ValueError:
            import sys as _s
            _s.exit(f"[error] --{_name} に数値でない値があります: {_x!r}\n"
                    f"        渡された値: {_v!r}\n"
                    f"        ⚠ PowerShell では ] や ^ が紛れやすいので確認を")
    return _out


for _nm in ("sm_list", "tm_list", "ret1_list", "gap_list", "ops_gap_list",
            "relax_axis_list", "relax_gap_list", "seeds", "regime_seeds"):
    if hasattr(a, _nm):
        _numlist(getattr(a, _nm), _nm.replace("_", "-"))
for _nm in ("split",):
    _v = getattr(a, _nm, "") or ""
    for _x in str(_v).split(","):
        if not _x.strip():
            continue
        try:
            pd.Timestamp(_x.strip())
        except Exception:
            sys.exit(f"[error] --{_nm} が日付として読めません: {_x!r}")

# ── TRAIN を要求するモードは、**スキャンの前に** --split を検査する ──────
#    1,540銘柄 × 4200日 のスキャンは10分かかる。終わってから落とさない。
# ══════════════════════════════════════════════════════════════════════
# ★★ 両建ての合格条件 — **2026-08-27 に、TEST を1度も見ずに決めた**
# ══════════════════════════════════════════════════════════════════════
#   ⛔ ここを後から緩めないこと。緩めた時点で検証ではなくなる。
#   ⚠ TEST は1候補につき1回きり(§18.54 で ATR%Q1 を消費した前例あり)。
#     今回 使うのは **「両建て」1候補**。鏡像単体は別候補にしない。
#
#   TRAIN の実測(2015-02〜2020-08 / 予算400万 / watch50):
#     N単独   月+49,587 / 鏡像単独 月+47,839
#     両建て  月+88,212 / 日次σ 32,352 / 相関 -0.062
#     月平均÷σ  片側 0.42 → 両建て 0.61
_BOTH_PASS = {
    # ① 両建ての月平均÷σ が N単独を上回る(これが採用の目的そのもの)
    "ratio_beats_short": True,
    # ② 両建ての月次 t がこの値以上(利益がノイズと区別できる)
    "t_min": 2.0,
    # ③ 鏡像 **単体** も月平均>0 かつ t>=1.0。
    #    片側が死んでいるのに合成で誤魔化すのを防ぐ。
    "mirror_t_min": 1.0,
    # ④ 日次相関がこの値以下(独立性が保たれている。強い正相関なら
    #    『同じものを2倍やっている』だけでレバレッジと変わらない)
    "corr_max": 0.30,
    # ⑤ TEST を半分に割って、**両方の半期**で月平均÷σ が N単独を上回る
    "both_halves": True,
}

_NEEDS_TRAIN = bool(a.explore or a.confirm or a.confirm_both or a.sweep_regime
                    or a.search_switch or a.sweep_size or a.sweep_market
                    or a.sweep_grid
                    or a.sweep_ops or a.sweep_barrier or a.sweep_relax
                    or a.tail_diag or a.hedge or bool(a.dump_picks))
if _NEEDS_TRAIN and not a.split:
    import sys as _sys
    _sys.exit("[error] このモードは TRAIN/TEST の分割が要ります。"
              "--split 2020-09-01 のように境界を指定してください\n"
              "        (指定しないと『測ってから良かった方を採る』が再開します)")

if a.list_axes:
    print("探索できる軸 (すべて寄り時点で確定 = D までの日足 + D+1 の始値):")
    _AX = {
        "atr_pct": "ATR%(ボラ) — 低ボラ銘柄の大ギャップほど異常＝反落しやすい?",
        "liq": "売買代金20日平均 — 薄いほどオーバーシュートしやすい(が執行も重い)",
        "range_pos": "20日レンジ位置% — 既に高値圏でのギャップは続伸しやすい?",
        "ret1": "前日リターン% — 前日も上げていたなら過熱?",
        "ret2": "2日リターン%(過熱の窓は1日が最適か?)",
        "ret3": "3日リターン%(同上)",
        "ret5": "5日リターン%",
        "ret20": "20日リターン%",
        "gap_hi_bp": "前日**高値**からのギャップbp — 終値だけでなく高値も抜けたか",
        "up_streak": "連続上昇日数",
        "vol_ratio": "出来高比(D/20日平均) — ⚠ D の出来高。当日は先読みなので使わない",
        "entry_p": "建値",
        "gap_bp": "ギャップbp(参考。既に閾値で切っている)",
        "dow": "曜日(0=月 〜 4=金)",
    }
    for _k, _d in _AX.items():
        print(f"  {_k:<12}{_d}")
    print("\n⚠ atr_pct / liq / range_pos / up_streak / entry_p / dow は §18.13 で"
          "測って候補ゼロ。\n   ただしあれは lss の母集団(約定した日だけ)。"
          "今回は全銘柄日なので、母集団が違う。")
    sys.exit(0)

# ★ 前日リターンの下限(§18.54 で TRAIN の5分位 Q5 の境界として決めた値)。
#   ⛔ _ops_sim にベタ書きしていたので、他から参照できなかった。定数にする。
_RET1_MIN = 1.753

# ① の水準は執行方式で決まる(執行コストの3倍)。**測る前に紐付けてある**
# ★ 鏡像。**符号を全部反転する**ので、下流の
#   「ret1 >= 1.753」「gap_bp >= 100」「pnl」がそのまま鏡像の条件になる。
#   long は 前日 -1.753% 以下 × ギャップ -100bp 以下 を **買う**。
#   both は **short 向きでスキャンして、あとから鏡像を複製する**。
#   2回スキャンすると10分×2かかるうえ、母集団がズレる余地ができる。
_SIDE = -1.0 if a.side == "long" else 1.0
if a.side == "both" and not (a.sweep_ops or a.confirm_both or a.sweep_regime
                             or a.search_switch or a.sweep_size):
    sys.exit("[error] --side both は --sweep-ops / --confirm-both / "
             "--sweep-regime 専用です。\n"
             "        判定(--confirm)や探索(--explore)は片側ずつ行ってください"
             "(両側を混ぜると『どちらの効果か』が分離できません)")

EXEC_BP = EXEC_COST_BP[a.exec_mode]
PASS_BP = round(EXEC_BP * 3.0, 1)

import backtest_limit_entry as ble                       # noqa: E402
from daytrade_data import available_local_symbols        # noqa: E402
from sameday5m_core import mod_for                       # noqa: E402

# キャッシュにこの日まで遡って入っていることを要求する。足りなければ再ダウンロード。
# ⚠ これを渡さないと fetch はキャッシュの開始日を見ない(上の _scan のコメント参照)。
from datetime import timedelta as _td                    # noqa: E402
_MIN_START = None
if not a.no_refetch:
    _MIN_START = (ble.datetime.now(ble.JST).date() - _td(days=a.days + 420))

# エンジンのモードグローバルは触らせない(pnl は自前で日足から計算する)
ble._MIRROR_PNL = False
ble._ENTRY_TYPE_FORCE = None
ble._INTRADAY_5M = False

STRATS = ["MACDTF", "A7", "RSI2", "DON", "VOLTF", "MOM"]


def _jq_to_yf(code: str) -> str:
    c = str(code).strip()
    if c.endswith(".T"):
        return c
    if len(c) == 5 and c.endswith("0"):
        c = c[:4]
    return f"{c}.T"


def _scan(sym: str) -> list[dict]:
    """1銘柄の全営業日について (ギャップ, 日中ショート損益, シグナル有無) を返す。

    ⛔ lss のバックテスト(run_limit_backtest)を**呼ばない**。日足のシグナル列だけを
       読むので、「lss が約定したか」は母集団に一切影響しない。
    """
    try:
        # ⛔ min_start_date を渡さないと、fetch は **キャッシュの開始日を一切見ずに**
        #   返す(backtest_limit_entry.py:456 の else 枝)。--days をいくら伸ばしても
        #   キャッシュが2020年始まりならそのまま使われる。
        #   2026-08-25 の第3回はこれで判定窓が 1日1.3銘柄になり、判定不能だった。
        df = ble.fetch(sym, a.days + 420, min_start_date=_MIN_START)
    except Exception:
        return []
    if df is None or len(df) < 250:
        return []
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()

    # 各戦略のシグナル日を集める(和集合。どれか1つでも出ていればシグナル日)
    sig_days: dict[pd.Timestamp, list[str]] = {}
    for st in STRATS:
        try:
            mod = mod_for(st)
            params = getattr(mod, "STRATEGY_PARAMS", {}).get(st)
            if not params:
                continue
            cf = params[0]
            d = cf(df.copy())
        except Exception:
            continue
        if "entry_sig" not in d.columns:
            continue
        try:
            for ts in d.index[d["entry_sig"].fillna(False).astype(bool)]:
                sig_days.setdefault(pd.Timestamp(ts).normalize(), []).append(st)
        except Exception:
            continue

    # ── 選別軸。**すべて D(前日終値)時点で確定しているものだけ** ──────────
    # ⛔ 当日(D+1)の出来高・高値・安値は引け後にしか分からない = 先読み。使わない。
    #    使ってよいのは D までの日足と、D+1 の**始値**だけ。
    _c, _h, _l = df["close"], df["high"], df["low"]
    _v = df["volume"] if "volume" in df.columns else pd.Series(0.0, index=df.index)
    _pcs = _c.shift(1)
    _tr = pd.concat([_h - _l, (_h - _pcs).abs(), (_l - _pcs).abs()], axis=1).max(axis=1)
    _atr_v = _tr.ewm(span=14, adjust=False).mean()        # ATR 本体(バリア用)
    _atr_pct = (_atr_v / _c * 100.0)
    _turn = (_c * _v).rolling(20).mean()                  # 売買代金 20日平均
    _mx20, _mn20 = _c.rolling(20).max(), _c.rolling(20).min()
    _rngpos = ((_c - _mn20) / (_mx20 - _mn20).replace(0.0, float("nan")) * 100.0)
    _ret1, _ret5, _ret20 = (_c.pct_change(1) * 100.0, _c.pct_change(5) * 100.0,
                            _c.pct_change(20) * 100.0)
    # 過熱の窓は1日が最適か? 2日・3日も並べる(ret1 と同じく _SIDE を掛ける)
    _ret2, _ret3 = _c.pct_change(2) * 100.0, _c.pct_change(3) * 100.0
    _volr = _v / _v.rolling(20).mean().replace(0.0, float("nan"))
    _sl, _s = [], 0                                       # 連続上昇日数(D時点まで)
    for _u in (_c > _c.shift(1)).fillna(False).tolist():
        _s = _s + 1 if _u else 0
        _sl.append(_s)
    _streak = pd.Series(_sl, index=df.index, dtype=float)

    def _fv(s, i):
        try:
            x = float(s.iloc[i])
            return x if x == x else None
        except Exception:
            return None

    # 全営業日について D → D+1 を測る。シグナル日かどうかはフラグで持つだけ。
    out: list[dict] = []
    _idx = df.index
    _cut = _idx[-1] - pd.Timedelta(days=a.days)
    for pos in range(1, len(_idx) - 1):
        d0 = _idx[pos]                    # D  (シグナル判定日 = 終値で判定)
        if d0 < _cut:
            continue
        d1 = _idx[pos + 1]                # D+1(建てる日)
        try:
            pc = float(df["close"].iloc[pos])
            o1 = float(df["open"].iloc[pos + 1])
            c1 = float(df["close"].iloc[pos + 1])
        except Exception:
            continue
        if not (pc > 0 and o1 > 0 and c1 > 0):
            continue
        # 建値(= D+1 の始値)で価格フィルタ。実運用と同じく「その日いくらで建てるか」
        if o1 < a.min_price or o1 > a.max_price:
            continue
        _st = sig_days.get(d0, [])
        out.append({
            "date": str(d1.date()),
            "symbol": sym,
            "entry_p": o1,
            "gap_bp": (o1 - pc) / pc * 10_000.0 * _SIDE,
            # C: 寄りで建てて引けで決済。short=(始-終) / long=(終-始)
            "pnl": (o1 - c1) * a.qty * _SIDE,
            "sig": 1 if _st else 0,
            "strats": ",".join(sorted(set(_st))),
            # ── バリア(損切り/利確)を測るための D+1 の値幅 ──
            # ⚠ 日足なので「高値と安値のどちらが先か」は分からない(§18.6)。
            #   両方触れた日は 損切り優先/利確優先 の**両方**を出す。
            # long は建値を軸に上下を反転(バリアの向きを short と揃えるため)
            "d1_high": (float(df["high"].iloc[pos + 1]) if _SIDE > 0
                        else 2 * o1 - float(df["low"].iloc[pos + 1])),
            "d1_low": (float(df["low"].iloc[pos + 1]) if _SIDE > 0
                       else 2 * o1 - float(df["high"].iloc[pos + 1])),
            "d1_close": c1,
            "atr": _fv(_atr_v, pos) or 0.0,    # D 時点の ATR(前夜に確定)
            # ── 選別軸(D時点で確定) ──
            "atr_pct": _fv(_atr_pct, pos),
            "liq": _fv(_turn, pos),
            "range_pos": _fv(_rngpos, pos),
            "ret1": (lambda _x: None if _x is None else _x * _SIDE)(_fv(_ret1, pos)),
            "ret2": (lambda _x: None if _x is None else _x * _SIDE)(_fv(_ret2, pos)),
            "ret3": (lambda _x: None if _x is None else _x * _SIDE)(_fv(_ret3, pos)),
            "ret5": _fv(_ret5, pos),
            "ret20": _fv(_ret20, pos),
            # 前日**高値**からのギャップ。前日終値だけでなく高値も抜けているか
            # (= より強い過熱か)。short は上抜け、long は下抜けを正にする
            "gap_hi_bp": (lambda _r: None if _r is None or _r <= 0 else
                          (o1 - _r) / _r * 10_000.0 * _SIDE)(
                _fv(df["high"] if _SIDE > 0 else df["low"], pos)),
            "up_streak": _fv(_streak, pos),
            "vol_ratio": _fv(_volr, pos),
            "dow": float(d1.dayofweek),
        })
    return out


# 探索できる軸。**すべて寄り時点で確定している**(D までの日足 + D+1 の始値)。
AXES = {
    "atr_pct":   "ATR%(ボラ)",
    "liq":       "売買代金20日平均",
    "range_pos": "20日レンジ位置%",
    "ret1":      "前日リターン%",
    "ret2":      "2日リターン%(過熱の窓)",
    "ret3":      "3日リターン%(過熱の窓)",
    "ret5":      "5日リターン%",
    "ret20":     "20日リターン%",
    "gap_hi_bp": "前日**高値**からのギャップbp(前日終値だけでなく高値も抜けたか)",
    "up_streak": "連続上昇日数",
    "vol_ratio": "出来高比(D/20日平均)",
    "entry_p":   "建値",
    "gap_bp":    "ギャップbp(参考)",
    "dow":       "曜日",
}


def _qlabel(sub: pd.DataFrame, col: str, nq: int):
    """軸を分位(または離散値)に切って (ラベル, 添字Series) を返す。"""
    s = sub[col] if col in sub.columns else None
    if s is None:
        return None
    s = pd.to_numeric(s, errors="coerce")
    ok = s.notna()
    if int(ok.sum()) < 500:
        return None
    if col == "dow":
        return s.where(ok).map(lambda x: f"{int(x)}" if x == x else None)
    try:
        q = pd.qcut(s[ok], nq, labels=False, duplicates="drop")
    except Exception:
        return None
    out = pd.Series(index=sub.index, dtype=object)
    out.loc[q.index] = [f"Q{int(v) + 1}" for v in q]
    return out


def _axis_scan(w: pd.DataFrame, col: str, label: str, nq: int, seeds: int):
    """1軸を分位に切って、最良分位の bp を帰無分布と比べる。

    ⚠ 実測でも帰無でも **同じ『最良を選ぶ』操作**をする。
       最大値を選ぶ操作それ自体で z が平均 +1 前後ずれる(§18.34b で実測 +1.17)。
       0 と比べてはいけない。
    ⚠ シャッフルは **同じ日の中だけ**。日をまたぐと日内相関が壊れて帰無分布が
       狭くなり、偽陽性を過小評価する(§18.13)。
    """
    lab = _qlabel(w, col, nq)
    if lab is None:
        return None
    sub = w.loc[lab.notna()].copy()
    sub["_q"] = lab[lab.notna()]
    if sub.empty:
        return None
    rows = []
    for q, g in sub.groupby("_q"):
        if len(g) < 200:
            continue
        rows.append((str(q), len(g), _bp(g), _cluster_t(g)))
    if len(rows) < 2:
        return None
    rows.sort(key=lambda x: x[0])
    best = max(rows, key=lambda x: x[2])
    worst = min(rows, key=lambda x: x[2])
    # ⛔ 日の中で値が一定の軸(曜日など)は、日の中でシャッフルしても何も変わらない
    #    = 帰無 == 実測 になり較正不能。候補として扱ってはいけない。
    #    2026-08-25 に曜日で 実測/帰無中央/帰無95% が3つとも +21.7 になって発覚。
    _const_in_day = bool(sub.groupby("date")["_q"].nunique().max() <= 1)
    if _const_in_day:
        return {"label": label, "col": col, "rows": rows, "best": best,
                "worst": worst, "null_med": float("nan"),
                "null_p95": float("nan"), "hit": False, "uncalib": True}
    # 帰無: 日の中で分位ラベルだけを入れ替え、**同じく最良分位を選ぶ**
    import random as _rnd
    rng = _rnd.Random(a.seed)
    groups = [(list(g["_q"]),
               (g["pnl"] / (g["entry_p"] * a.qty) * 10_000.0).tolist())
              for _, g in sub.groupby("date")]
    nulls = []
    for _ in range(max(1, seeds)):
        acc: dict[str, list[float]] = {}
        for qs, vs in groups:
            qs2 = list(qs)
            rng.shuffle(qs2)
            for k, v in zip(qs2, vs):
                acc.setdefault(k, []).append(v)
        cand = [sum(v) / len(v) for k, v in acc.items() if len(v) >= 200]
        if cand:
            nulls.append(max(cand))
    if not nulls:
        return None
    nulls.sort()
    p95 = nulls[min(len(nulls) - 1, int(len(nulls) * 0.95))]
    med = nulls[len(nulls) // 2]
    return {"label": label, "col": col, "rows": rows, "best": best,
            "worst": worst, "null_med": med, "null_p95": p95,
            "hit": best[2] > p95, "uncalib": False}


def _band(g: float) -> str:
    for lo, hi in GAP_EDGES:
        if lo <= g < hi:
            return ("< 0" if hi == 0.0 else
                    f"{int(lo)}〜" if hi > 1e8 else f"{int(lo)}〜{int(hi)}")
    return "?"


def _cluster_t(sub: pd.DataFrame) -> float:
    """日クラスタ頑健 t。実効サンプル = 営業日数(件数ではない / §18.13)。"""
    if sub.empty:
        return 0.0
    dm = sub.groupby("date")["pnl"].mean()
    if len(dm) < 2:
        return 0.0
    sd = dm.std(ddof=1)
    if not (sd == sd) or sd <= 0:
        return 0.0
    return float(dm.mean() / (sd / (len(dm) ** 0.5)))


def _bp(sub: pd.DataFrame) -> float:
    if sub.empty:
        return 0.0
    return float((sub["pnl"] / (sub["entry_p"] * a.qty) * 10_000.0).mean())


def _spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0

    def _rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        for pos, i in enumerate(order):
            rk[i] = float(pos)
        return rk

    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return float(num / (dx * dy)) if dx > 0 and dy > 0 else 0.0


def _band_table(r: pd.DataFrame, title: str) -> list[tuple[str, int, float, float, float]]:
    """ギャップ帯別の表を出し、(帯, 件数, 円/件, bp, t) を返す。"""
    print(f"\n  {title}")
    print(f"    {'ギャップ(bp)':<13}{'件数':>9}{'円/件':>10}{'bp/件':>9}{'日t':>8}")
    print("    " + "-" * 49)
    rows = []
    for lo, hi in GAP_EDGES:
        lbl = ("< 0" if hi == 0.0 else
               f"{int(lo)}〜" if hi > 1e8 else f"{int(lo)}〜{int(hi)}")
        sub = r[r["band"] == lbl]
        if len(sub) < 30:                 # 30件未満は出さない(読み違えのもと)
            continue
        _b, _t = _bp(sub), _cluster_t(sub)
        rows.append((lbl, len(sub), float(sub["pnl"].mean()), _b, _t))
        print(f"    {lbl:<13}{len(sub):>9,}{sub['pnl'].mean():>+10,.0f}"
              f"{_b:>+9.1f}{_t:>+8.2f}")
    return rows


def _null_calib(r: pd.DataFrame, hi_lbl: str, lo_lbl: str) -> tuple[float, float, float]:
    """帰無較正: **同じ日の中で** 帯ラベルをシャッフル(日効果を完全に保つ)。

    ⛔ 日をまたぐシャッフルは日内相関を壊し、帰無分布が狭くなって偽陽性を
       過小評価する(§18.13)。日ブロックは必ず保つ。
    """
    import random as _rnd
    rng = _rnd.Random(a.seed)
    obs_hi = r[r["band"] == hi_lbl]
    obs_lo = r[r["band"] == lo_lbl]
    obs = _bp(obs_hi) - _bp(obs_lo)
    groups = [g for _, g in r.groupby("date")]
    spreads = []
    for _ in range(max(1, a.seeds)):
        hi_v, lo_v = [], []
        for g in groups:
            bands = list(g["band"])
            rng.shuffle(bands)
            _bpv = (g["pnl"] / (g["entry_p"] * a.qty) * 10_000.0).tolist()
            for bnd, v in zip(bands, _bpv):
                if bnd == hi_lbl:
                    hi_v.append(v)
                elif bnd == lo_lbl:
                    lo_v.append(v)
        if hi_v and lo_v:
            spreads.append(sum(hi_v) / len(hi_v) - sum(lo_v) / len(lo_v))
    if not spreads:
        return obs, 0.0, 0.0
    spreads.sort()
    p95 = spreads[min(len(spreads) - 1, int(len(spreads) * 0.95))]
    med = spreads[len(spreads) // 2]
    return obs, med, p95


# ── 実行 ────────────────────────────────────────────────────────────────
_seen, universe = set(), []
for s in available_local_symbols():
    y = _jq_to_yf(s)
    if y not in _seen:
        _seen.add(y)
        universe.append(y)
if a.limit > 0:
    universe = universe[:a.limit]
if not universe:
    sys.exit("[error] stock_5min に銘柄がありません(available_local_symbols が空)")

_POOL_LBL = {"all": "シグナル不問(ギャップだけが土台)",
             "sig": "シグナルが出た日だけ(第1回と同じ)",
             "nosig": "シグナルが出ていない日だけ"}
_EXEC_LBL = {"auction": "前夜に寄付指値 → 板寄せ約定(エントリーの呼値なし)",
             "confirm": "09:00に板を読んでから発注(実測 −30.9bp)"}
print(f"[info] 母集団 {len(universe):,}銘柄 / 遡及{a.days}日 / "
      f"建値 {a.min_price:,.0f}〜{a.max_price:,.0f}円 / {a.qty}株")
print(f"[info] 判定する母集団 = **{_POOL_LBL[a.pool]}**")
print(f"[info] 執行方式 = {a.exec_mode} ({_EXEC_LBL[a.exec_mode]}) / "
      f"執行コスト {EXEC_BP:.1f}bp"
      + (" / ★**鏡像(long: 前日下げ × ギャップダウンを買う)**" if a.side == "long"
         else " / ★**両建て(short + その鏡像を1つの予算・watch50 で)**"
         if a.side == "both" else "")
      + (f" / ギャップ上限 {a.max_gap_bp:.0f}bp" if a.max_gap_bp > 0 else ""))
print(f"[info] 測るもの = C(寄りで売って引けで買い戻す)。"
      f"**sm/tm/delay/資金均等/予算/発注順/選定を1つも持たない**")
print(f"[info] 母集団は **シグナルから直接**作る(lss のバックテストを通らない / §18.50)")
print(f"[info] 合格ライン(事前宣言・不変): bp≥{PASS_BP}(=執行{EXEC_BP:.1f}bp×3) / "
      f"t≥{PASS_T} / 単調ρ≥{PASS_RHO} / 帰無95%点超え / **既見・未使用 両方**")

_rows: list[dict] = []
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    futs = {ex.submit(_scan, s): s for s in universe}
    for i, f in enumerate(as_completed(futs), 1):
        try:
            _rows.extend(f.result() or [])
        except Exception:
            pass
        if i % 200 == 0:
            print(f"  … {i}/{len(universe)}銘柄 / {len(_rows):,}銘柄日", flush=True)

if not _rows:
    sys.exit("[error] 1件も集まりませんでした")
r_all = pd.DataFrame(_rows)
r_all["band"] = r_all["gap_bp"].map(_band)
if a.out:
    r_all.to_csv(a.out, index=False, encoding="utf-8-sig")
    print(f"[info] 明細を書きました: {a.out} ({len(r_all):,}行)")

_sig = r_all[r_all["sig"] == 1]
_ctl = r_all[r_all["sig"] == 0]
print(f"\n{'=' * 78}")
print(f"■ 母集団 — シグナル {len(_sig):,}銘柄日 / 対照 {len(_ctl):,}銘柄日 / "
      f"{r_all['date'].nunique():,}営業日")
print(f"{'=' * 78}")
print(f"  ⚠ シグナル有無は **参考**(§18.52 で寄与ゼロと出た)。判定は --pool = "
      f"{a.pool} に対して行う")


def _pool_of(w: pd.DataFrame) -> pd.DataFrame:
    """判定対象。--pool でシグナル有無、--min-gap-bp でギャップ閾値を掛ける。"""
    if a.pool == "sig":
        w = w[w["sig"] == 1]
    elif a.pool == "nosig":
        w = w[w["sig"] == 0]
    if a.min_gap_bp > 0:
        w = w[w["gap_bp"] >= a.min_gap_bp]
    if a.max_gap_bp > 0:
        w = w[w["gap_bp"] <= a.max_gap_bp]
    # ★ N の母集団そのもの(前日リターンの下限)。これを掛けると
    #   「ret1 を固定した**残差**にまだ軸があるか」を探索できる。
    #   ⛔ これまでの --explore は ret1 を掛けずに回していたので、
    #      見つかったのは「ret1 が最強」までで、その先を一度も見ていなかった。
    if a.min_ret1 > 0:
        w = w[w["ret1"] >= a.min_ret1]
    return w


# ── 期間分割。**上限で切る**(§18.25) ───────────────────────────────
#    --split はカンマ区切りで複数可。判定は **最も古い窓**(=未使用期間)に対して行う。
_windows = [("全期間", r_all)]
_judge_win = "全期間"
_DENSITY_FAIL = ""      # 空でなければ「データが入っていない」= 判定を出さない
if a.split:
    _cuts = [str(pd.Timestamp(x.strip()).date()) for x in a.split.split(",") if x.strip()]
    _cuts.sort()
    _bounds = [None] + _cuts + [None]
    _segs = []
    for _i in range(len(_bounds) - 1):
        _lo_b, _hi_b = _bounds[_i], _bounds[_i + 1]
        _m = pd.Series(True, index=r_all.index)
        if _lo_b is not None:
            _m &= r_all["date"] >= _lo_b
        if _hi_b is not None:
            _m &= r_all["date"] < _hi_b
        _seg = r_all[_m]
        if _seg.empty:
            continue
        _nm = (f"{str(_seg['date'].min())[:7]}〜{str(_seg['date'].max())[:7]}")
        _segs.append((_nm, _seg))
    if _segs:
        _judge_win = _segs[0][0]          # 最も古い = 未使用期間
        print(f"\n  ★★ **判定するのは最も古い窓だけ** = {_judge_win} "
              f"({_segs[0][1]['date'].nunique():,}営業日)")
        _dens = []
        for _i, (_nm, _seg) in enumerate(_segs):
            _tag = "★ 未使用(判定対象)" if _i == 0 else "既見(参考)"
            _d = len(_seg) / max(1, _seg["date"].nunique())
            _dens.append(_d)
            print(f"     {_nm}  {_seg['date'].nunique():>5,}営業日  "
                  f"{len(_seg):>9,}銘柄日  {_d:>8,.1f}銘柄/日   {_tag}")
        # ⛔ データ欠落を『不合格』と報告しないための門番。
        #   2026-08-25 の第3回は判定窓が 1.3銘柄/日(他窓の1/900)で、
        #   中身が空なのに「不合格」と出た。相場ではこの差は出ない。
        _dmax = max(_dens) if _dens else 0.0
        if _dmax > 0 and _dens[0] / _dmax < a.min_density:
            _DENSITY_FAIL = (f"判定窓の密度 {_dens[0]:.1f}銘柄/日 は "
                             f"最も濃い窓 {_dmax:.1f}銘柄/日 の "
                             f"{_dens[0] / _dmax * 100:.1f}% しかない")
            print(f"\n  ⛔⛔ **{_DENSITY_FAIL}**")
            print(f"      相場でこの差は出ない = **データが入っていない**。")
            print(f"      --no-refetch を外して再実行し、キャッシュを "
                  f"{a.days + 420}日ぶん遡らせること。")
            print(f"      この実行の判定は **測定不能** として記録する"
                  f"(不合格ではない)。")
        print(f"  ⚠ ホールドアウトは **上限で切る**(§18.25 の事故)")
        print(f"  ⚠ 落ちたときに『相場が特殊だった』と言わないこと(宣言済み)")
        _windows += _segs

# ══ 探索モード / 検証モード ═══════════════════════════════════════════
#   探索: TRAIN(最も古い窓)だけを掃く。**TEST は集計すらしない**。
#   検証: 指定した1候補を TEST で1回だけ測る。
# ⛔ _train を使うのは explore/confirm だけではない。sweep 3種も使う。
#    ここを取り違えると **重いスキャンが終わってから NameError で落ちる**
#    (2026-08-26 に3本とも踏んだ)。必要なモードは _NEEDS_TRAIN に集約する。
if _NEEDS_TRAIN:
    _segs_only = [(_n, _s) for _n, _s in _windows if _n != "全期間"]
    if not _segs_only:
        sys.exit("[error] --split が要ります(TRAIN/TEST を分けないと探索できません)")
    _train_n, _train = _segs_only[0]
    _test_n = "＋".join(n for n, _ in _segs_only[1:]) or "(なし)"
    _test = (pd.concat([s for _, s in _segs_only[1:]])
             if len(_segs_only) > 1 else r_all.iloc[0:0])
    if a.side == "both":
        # ★ 鏡像を **複製**して足す。符号を反転すれば、下流の
        #   「ret1 >= 1.753」「gap_bp >= min_gap_bp」「pnl」がそのまま
        #   ロング側の条件になる(= --side long と1円まで同じ)。
        #   ⚠ 同じ (銘柄,日) が両側に入ることは無い。ret1 が
        #     +1.753% 以上かつ -1.753% 以下になることはないため。
        # ⛔⛔ **TRAIN と TEST の両方に掛けること**(2026-08-27 に踏んだ)。
        #   TRAIN だけに掛けていたので、--confirm-both で TEST を見ると
        #   ロングが 0件 になり、両建てがショート単独と同じ数字になった。
        #   『鏡像単体 t=0.00』という有り得ない結果で気づいた。
        def _mirror_of(_df):
            if _df is None or _df.empty:
                return _df
            _m = _df.copy()
            for _c in ("gap_bp", "pnl", "ret1"):
                _m[_c] = -_m[_c]
            _hi = _m["d1_high"].copy()
            _m["d1_high"] = 2 * _m["entry_p"] - _m["d1_low"]
            _m["d1_low"] = 2 * _m["entry_p"] - _hi
            _m["side"] = -1
            _o = _df.copy()
            _o["side"] = 1
            return pd.concat([_o, _m], ignore_index=True)

        _n0, _t0 = len(_train), len(_test)
        _train = _mirror_of(_train)
        _test = _mirror_of(_test)
        print(f"\n[both] 鏡像を複製しました。"
              f"TRAIN {_n0:,}→{len(_train):,}行 / TEST {_t0:,}→{len(_test):,}行")
        if _t0 and len(_test) != _t0 * 2:
            print(f"       ⛔ TEST が2倍になっていません。複製に失敗しています")
        print(f"       ⚠ **watch も予算も1つを共有**します。kabu の登録上限は"
              f"合計50件なので、両側の候補を混ぜて売買代金順に上位を取ります")

# ══════════════════════════════════════════════════════════════════════
# 運用シミュ本体。**TRAIN でも TEST でも同じ関数を使う**(2026-08-27)。
#   以前は --sweep-ops の中に埋まっていて TEST 側から呼べなかった。
#   ⛔ 実装を2つ持つと、片方だけ直して食い違う(§18.48 ⑧d の前例)。
# ══════════════════════════════════════════════════════════════════════
def _make_ops_sim(_src_all, _pool_df, _ond):
    # ★ 発注順と予算配分を差し替えられるようにする(2026-08-27)。
    #   ⛔ 既定は現行(gap / merge)。**引数を足しただけで挙動は変えていない**。
    _ALL_D = sorted(_src_all["date"].unique())
    _HALF = _ALL_D[len(_ALL_D) // 2] if _ALL_D else ""

    def _ops_sim(watch: int, budget_man: float, max_n: int, one_per_sym: bool,
                 order: str = "gap", alloc: str = "merge", seed: int = 0,
                 half: int = 0, sizing: str = "qty", size_arg: float = 0.0):
        """日ごとに 候補→watch→ギャップ→予算 の順で建てる(N のタブと同じ順序)。

        order … gap(既定|ギャップ|降順) / liq(売買代金降順) / gapasc / price(安い順)
                / rand(その日ごとにシャッフル。seed で再現)
        alloc … merge(既定: 1つの財布) / split50(側ごとに半額) /
                prop(その日の合格数で按分) / alt(交互に取る)
        half  … 0=全期間 / 1=前半 / 2=後半 (擬似OOS)
        sizing… qty(既定 100株固定) / equal(金額均等 予算÷合格件数) /
                cap(100株固定 + 1銘柄の金額上限 size_arg万円) /
                atr(ATR均等 = リスク均等。1件あたり size_arg円のリスク)
                ⚠ 100株固定は建値で建玉が6倍ばらつく(§18.30)。
                  σ を削る唯一の未検証レバー。
        """
        _cap = budget_man * 10_000.0
        _tot, _cnt, _used, _miss = 0.0, 0, 0.0, 0
        # ⚠ 候補は「ret1 で絞る前の全銘柄日」。watch は候補に掛かる
        _src = _src_all if a.pool == "all" else _pool_df
        if half == 1:
            _src = _src[_src["date"] < _HALF]
        elif half == 2:
            _src = _src[_src["date"] >= _HALF]
        _rng = np.random.default_rng(seed) if order == "rand" else None
        _seen_sym: dict = {}
        # both のときだけ使う。日次の相関とσを出すため
        _daily: dict = {}                      # date -> [short, long]
        _dcap: dict = {}                       # date -> [short投入, long投入]
        _dcnt: dict = {}                       # date -> [short件数, long件数]
        _byside = {1: [0.0, 0], -1: [0.0, 0]}
        _picks: list = []                      # 実際に建てた明細
        _pushed = {1: 0, -1: 0}                # 予算で押し出された件数
        for _d, _g in _src.groupby("date"):
            _c = _g[_g["ret1"] >= _RET1_MIN]
            if _c.empty:
                continue
            _w = _c.sort_values("liq", ascending=False, na_position="last")
            _wd = _w if watch <= 0 else _w.head(watch)
            # ⛔ ここは _pool_of を通らない(候補は ret1 で絞る前が必要)ので、
            #    **ギャップの上限を自分で掛ける**。2026-08-26 に掛け忘れて
            #    --max-gap-bp が1円も効かず、上限あり/なしで同じ数字が出た。
            _gm = _wd["gap_bp"] >= a.min_gap_bp
            _gc = _c["gap_bp"] >= a.min_gap_bp
            if a.max_gap_bp > 0:
                _gm &= _wd["gap_bp"] <= a.max_gap_bp
                _gc &= _c["gap_bp"] <= a.max_gap_bp
            _hit = _wd[_gm]
            _miss += len(_c[_gc]) - len(_hit)
            # ── 発注順 ────────────────────────────────────────────
            if order == "liq":
                _hit = _hit.sort_values("liq", ascending=False, na_position="last")
            elif order == "gapasc":
                _hit = _hit.sort_values("gap_bp", ascending=True)
            elif order == "price":
                _hit = _hit.sort_values("entry_p", ascending=True)
            elif order == "rand":
                _hit = _hit.iloc[_rng.permutation(len(_hit))] if len(_hit) else _hit
            else:                                   # gap(既定)
                _hit = _hit.sort_values("gap_bp", ascending=False)
            # ── 予算配分 ──────────────────────────────────────────
            #   merge 以外は側ごとに財布を分ける。alt は1つの財布で交互に取る。
            _sd_of = (lambda _r: int(getattr(_r, "side", 1)))
            _pool = {1: _cap, -1: _cap}
            if alloc == "split50":
                _pool = {1: _cap / 2, -1: _cap / 2}
            elif alloc == "prop":
                _ns = int((_hit["side"].values > 0).sum()) if "side" in _hit else len(_hit)
                _nl = len(_hit) - _ns
                _tt = max(_ns + _nl, 1)
                _pool = {1: _cap * _ns / _tt, -1: _cap * _nl / _tt}
            elif alloc == "alt" and "side" in _hit and len(_hit):
                # 側ごとに順位を付け、(順位, 側) で並べ直す = S,L,S,L,…
                _hh = _hit.copy()
                _hh["_k"] = _hh.groupby("side").cumcount()
                _hit = _hh.sort_values(["_k", "side"], ascending=[True, False])
            _cash, _n = _cap, 0
            # ★ サイジング。1件あたりの株数を決める(100株単位)。
            #   pnl は 100株 で計算済みなので、株数の比で伸縮する。
            _tgt = (_cap / max(len(_hit), 1)) if sizing == "equal" else 0.0
            for _r in _hit.itertuples():
                if max_n > 0 and _n >= max_n:
                    break
                if one_per_sym and _seen_sym.get(_r.symbol) == _d:
                    continue
                _px = float(_r.entry_p)
                _lot = a.qty
                if sizing == "equal" and _px > 0:
                    _lot = max(a.qty, int(_tgt / (_px * a.qty)) * a.qty)
                elif sizing == "atr" and size_arg > 0:
                    _at = float(getattr(_r, "atr", 0.0) or 0.0)
                    if _at > 0:
                        _lot = max(a.qty,
                                   int(size_arg / (_at * a.qty)) * a.qty)
                if sizing == "cap" and size_arg > 0:
                    _lot = min(_lot, max(a.qty,
                                         int(size_arg * 1e4 / (_px * a.qty)) * a.qty))
                elif sizing in ("equal", "atr") and size_arg > 0:
                    _lot = min(_lot, max(a.qty,
                                         int(size_arg * 1e4 / (_px * a.qty)) * a.qty))
                _scale = _lot / a.qty
                _cost = _px * _lot
                if alloc in ("split50", "prop"):
                    _sd0 = _sd_of(_r)
                    if _cost > _pool[_sd0]:
                        _pushed[_sd0] = _pushed.get(_sd0, 0) + 1
                        continue
                    _pool[_sd0] -= _cost
                    _cash -= _cost
                elif _cost > _cash:
                    _pushed[int(getattr(_r, "side", 1))] = \
                        _pushed.get(int(getattr(_r, "side", 1)), 0) + 1
                    continue
                else:
                    _cash -= _cost
                _pp = float(_r.pnl) * _scale
                _tot += _pp
                _n += 1
                # ★ 実際に建てた明細。ポートフォリオ損切りの検証に要る
                #   (どの銘柄を何株建てたかは _ops_sim の中でしか分からない)
                _picks.append({
                    "date": _d, "symbol": _r.symbol,
                    "side": int(getattr(_r, "side", 1)),
                    "entry_p": float(_px), "qty": int(_lot),
                    "gap_bp": float(getattr(_r, "gap_bp", 0.0) or 0.0),
                    "d1_close": float(getattr(_r, "d1_close", 0.0) or 0.0),
                    "atr": float(getattr(_r, "atr", 0.0) or 0.0),
                    "pnl": _pp,
                })
                _seen_sym[_r.symbol] = _d
                _sd = int(getattr(_r, "side", 1))
                _v = _byside.setdefault(_sd, [0.0, 0])
                _v[0] += _pp; _v[1] += 1
                _dv = _daily.setdefault(_d, [0.0, 0.0])
                _dv[0 if _sd > 0 else 1] += _pp
                _cv = _dcap.setdefault(_d, [0.0, 0.0])
                _cv[0 if _sd > 0 else 1] += _cost
                _kv = _dcnt.setdefault(_d, [0, 0])
                _kv[0 if _sd > 0 else 1] += 1
            _cnt += _n
            _used += _cap - _cash
        return {"pnl": _tot, "n": _cnt, "used": _used / _ond,
                "picks": _picks,
                "miss": _miss, "per": (_tot / _cnt if _cnt else 0.0),
                "daily": _daily, "byside": _byside,
                "dcap": _dcap, "pushed": _pushed, "dcnt": _dcnt}
    return _ops_sim, _ALL_D, _HALF


if a.sweep_grid:
    # ══ 前日リターン × ギャップ の 2次元 (TRAIN のみ) ═══════════════
    #   ⛔ TEST は使わない。現行(1.753% × 100bp)は2つを**別々に**決めた:
    #      ret1 は「ギャップ100bp以上の中での5分位のQ5」、gap は第1回の表。
    #      組み合わせとして最適かは一度も見ていない。
    _gt = _train if a.pool == "all" else (
        _train[_train["sig"] == (1 if a.pool == "sig" else 0)])
    if a.max_gap_bp > 0:                     # ⛔ 同上。_pool_of を通らないので自分で
        _gt = _gt[_gt["gap_bp"] <= a.max_gap_bp]
    if _gt.empty:
        sys.exit("[error] TRAIN が空です")
    _r1s = [float(x) for x in a.ret1_list.split(",") if x.strip()]
    _gps = [float(x) for x in a.gap_list.split(",") if x.strip()]
    _ndays = max(1, _gt["date"].nunique())
    print(f"\n{'=' * 78}\n■ 前日リターン × ギャップ — **TRAIN({_train_n}) だけ**\n{'=' * 78}")
    print(f"  ⛔ TEST は1回も使いません")
    print(f"  対象 {len(_gt):,}銘柄日 / {_ndays:,}営業日")
    print(f"  ★ 現行 = ret1 ≥ {1.753}% × gap ≥ 100bp")
    for _what, _fmt in (("bp/件", "bp"), ("件/日", "n")):
        print(f"\n  ── {_what} ──")
        print(f"    {'前日%':<8}" + "".join(f"{'gap' + str(int(g)):>11}" for g in _gps))
        print("    " + "-" * (8 + 11 * len(_gps)))
        for _r1 in _r1s:
            _row = f"    {_r1:<8.3f}"
            for _gp in _gps:
                _s = _gt[(_gt["ret1"] >= _r1) & (_gt["gap_bp"] >= _gp)]
                if len(_s) < 200:
                    _row += f"{'—':>11}"
                    continue
                if _fmt == "bp":
                    _v = _bp(_s)
                    _mk = "★" if (abs(_r1 - 1.753) < 1e-6 and abs(_gp - 100) < 1e-6) else " "
                    _row += f"{_v:>+10.1f}{_mk}"
                else:
                    _row += f"{len(_s) / _ndays:>11.1f}"
            print(_row)
    print(f"\n  ── 日クラスタ t ──")
    print(f"    {'前日%':<8}" + "".join(f"{'gap' + str(int(g)):>11}" for g in _gps))
    print("    " + "-" * (8 + 11 * len(_gps)))
    for _r1 in _r1s:
        _row = f"    {_r1:<8.3f}"
        for _gp in _gps:
            _s = _gt[(_gt["ret1"] >= _r1) & (_gt["gap_bp"] >= _gp)]
            _row += (f"{_cluster_t(_s):>+11.2f}" if len(_s) >= 200 else f"{'—':>11}")
        print(_row)
    print(f"\n  {'=' * 68}")
    print(f"  ★ 読み方: **★(現行) と明確に違う升があるか**だけを見る。")
    print(f"     ⚠ 件数が減れば bp は上がりやすい(強い銘柄だけ残るので)。")
    print(f"        **bp と 件/日 を必ずセットで**見ること。bp が上がっても")
    print(f"        件/日 が予算(1日十数件)を下回ったら、月の総額は落ちます。")
    print(f"  ⛔ 良い升があっても採用しないこと。TEST での検証が要ります")
    print(f"     (TEST は既に2回使用済み)。")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.sweep_relax:
    # ══════════════════════════════════════════════════════════════════
    # ★★ 「絞る」ではなく「**増やす**」— 別の軸が強い銘柄だけ gap 閾値を下げる
    # ══════════════════════════════════════════════════════════════════
    #   ⛔ N は **件数不足**であって予算不足ではない(§18.55: 稼働率40%)。
    #      フィルタで bp/件 を上げても、件数が減れば月の総額は落ちる。
    #      だから探すべきは「どれを捨てるか」ではなく「**どれを足せるか**」。
    #
    #      建てる = (ret1 >= 1.753 かつ gap >= 100)          … 現行
    #             ∪ (AXIS >= 閾値   かつ gap >= 緩めた閾値)   … 追加分
    #
    #   ★ 見るのは **追加分が単体でプラスか**。合計だけ見ると、
    #      件数が増えたぶん薄まっただけでも「良くなった」に見える(§18.28)。
    #   ⛔ TEST は使わない。TRAIN で完結する。
    _rt = _train if a.pool == "all" else (
        _train[_train["sig"] == (1 if a.pool == "sig" else 0)])
    if a.max_gap_bp > 0:
        _rt = _rt[_rt["gap_bp"] <= a.max_gap_bp]
    if _rt.empty or a.relax_axis not in _rt.columns:
        sys.exit(f"[error] TRAIN が空か、軸 {a.relax_axis} がありません")
    _nd = max(1, _rt["date"].nunique())
    _base_m = (_rt["ret1"] >= _RET1_MIN) & (_rt["gap_bp"] >= a.min_gap_bp)
    _base = _rt[_base_m]

    print(f"\n{'=' * 78}")
    print(f"■ 閾値を**下げて増やす** — 軸 {a.relax_axis} / **TRAIN({_train_n}) だけ**")
    print(f"{'=' * 78}")
    print(f"  ⛔ TEST は1回も使いません")
    print(f"  ⚠ N は **件数不足**(§18.55 稼働率40%)。絞るのではなく足せるかを見ます")
    print(f"  対象 {len(_rt):,}銘柄日 / {_nd:,}営業日")
    print(f"  ★ 現行 = ret1 ≥ {_RET1_MIN}% × gap ≥ {a.min_gap_bp:.0f}bp"
          f" … {len(_base):,}件 / {len(_base) / _nd:.1f}件/日 / "
          f"**{_bp(_base):+.1f}bp** / 日t {_cluster_t(_base):+.2f}")
    print(f"  ⚠ 合格ライン {PASS_BP}bp は **追加分そのもの**に掛けます"
          f"(薄い取引を足すだけなら意味がない)")

    _axs = [float(x) for x in a.relax_axis_list.split(",") if x.strip()]
    _gps = [float(x) for x in a.relax_gap_list.split(",") if x.strip()]

    # ⛔⛔ 追加分は2種類あって、コストがまったく違う。混ぜて判定してはいけない。
    #   (A) ret1 >= 1.753 だが gap が閾値に届かず落ちていた
    #       → **前夜の候補プールに既にいる**(watch50 の抽選に参加済み)。
    #         プールが増えないので誰も押し出さない = タダ
    #       ⚠ ただし実質「gap 閾値を下げる」だけ。--sweep-grid で掃き済みの領域
    #   (B) ret1 < 1.753 だが AXIS が強い
    #       → **新しく watch する必要がある**。§18.55 で 82%の日が既に50件の壁に
    #         当たっているので、流動性順で **既存の +15.5bp を押し出す**
    #       ★ ここだけが本当に新しい。そして無料ではない
    _watched = _rt["ret1"] >= _RET1_MIN
    _best = None
    for _lbl, _key in (("追加分 **(A) 候補プールを増やさない** bp/件", "bpA"),
                       ("追加分 (A) 件/日", "nA"),
                       ("追加分 **(B) 候補プールが増える** bp/件", "bpB"),
                       ("追加分 (B) 件/日", "nB"),
                       ("追加分 (B) 日t", "tB"),
                       ("追加分 (A+B) bp/件 ⚠混合", "bp"),
                       ("追加分 (A+B) 件/日", "n"),
                       ("追加分 (A+B) 日t", "t"),
                       ("合計 bp/件", "cbp")):
        print(f"\n  ── {_lbl} ──")
        print(f"    {a.relax_axis[:9]:<10}"
              + "".join(f"{'gap' + str(int(g)):>11}" for g in _gps))
        print("    " + "-" * (10 + 11 * len(_gps)))
        for _ax in _axs:
            _row = f"    {('条件なし' if _ax <= 0 else f'{_ax:.3g}'):<10}"
            for _gp in _gps:
                _add_m = ((_rt[a.relax_axis] >= _ax) & (_rt["gap_bp"] >= _gp)
                          & ~_base_m)
                if _key.endswith("A"):
                    _add_m = _add_m & _watched
                elif _key.endswith("B"):
                    _add_m = _add_m & ~_watched
                _add = _rt[_add_m]
                if len(_add) < 200:
                    _row += f"{'—':>11}"
                    continue
                if _key in ("bpA", "nA"):
                    _row += (f"{_bp(_add):>+11.1f}" if _key == "bpA"
                             else f"{len(_add) / _nd:>11.1f}")
                elif _key in ("bpB", "nB", "tB"):
                    _row += (f"{_bp(_add):>+11.1f}" if _key == "bpB" else
                             f"{len(_add) / _nd:>11.1f}" if _key == "nB" else
                             f"{_cluster_t(_add):>+11.2f}")
                elif _key == "bp":
                    _v = _bp(_add)
                    _row += f"{_v:>+11.1f}"
                    _t = _cluster_t(_add)
                    # ★ 最良は **(B) 新規watchが要る側**だけで判定する。
                    #   (A) は実質「gap 閾値を下げる」だけで --sweep-grid 掃き済み。
                    _bm = _add_m & ~_watched
                    _b = _rt[_bm]
                    if _ax > 0 and len(_b) >= 200:
                        _vb, _tb2 = _bp(_b), _cluster_t(_b)
                        if _vb >= PASS_BP and _tb2 >= PASS_T:
                            _c = (_vb, _tb2, _ax, _gp, len(_b))
                            if _best is None or _c[0] > _best[0]:
                                _best = _c
                elif _key == "n":
                    _row += f"{len(_add) / _nd:>11.1f}"
                elif _key == "t":
                    _row += f"{_cluster_t(_add):>+11.2f}"
                else:
                    _row += f"{_bp(_rt[_base_m | _add_m]):>+11.1f}"
            print(_row)

    print(f"\n  {'=' * 68}")
    print(f"  ★★ 読み方: どの升も **『条件なし』の行と比べる**こと。")
    print(f"     上回っていなければ、効いているのは軸ではなく"
          f"**gap 閾値を下げたこと**です")
    print(f"     (それは --sweep-grid で掃き済みの領域)。")
    print(f"  ★ 判定は **(B) 候補プールが増える側だけ**に掛けます。")
    print(f"     (A) は ret1 の条件を満たしていて gap だけ届かなかった分 = "
          f"実質『gap 閾値を下げる』で、--sweep-grid で掃き済みの領域です。")
    if _best is None:
        print(f"\n  ⛔ **(B) に足せる升がありません。**")
        print(f"     候補プールが増える追加分で bp≥{PASS_BP} かつ t≥{PASS_T} を"
              f"満たす組み合わせはゼロでした。")
        print(f"     = watch50 を消費してまで拾う価値のある取引は無い。**現行のまま**。")
    else:
        _v, _t, _ax, _gp, _n = _best
        print(f"\n  ★ (B) が単体で合格する最良: {a.relax_axis} ≥ {_ax:g} × "
              f"gap ≥ {_gp:.0f}bp")
        print(f"     {_n:,}件 / {_n / _nd:.1f}件/日 / **{_v:+.1f}bp** / 日t {_t:+.2f}")
        print(f"  ⛔⛔ **これでも採用しないこと。** (B) は "
              f"**前夜の候補プールを増やします**。")
        print(f"     §18.55 で **82%の日が既に50件の壁**に当たっているので、"
              f"新しい候補を入れると")
        print(f"     流動性順で **現行の +{_bp(_base):.1f}bp の取引が押し出されます**。")
        print(f"     {_v:+.1f}bp < +{_bp(_base):.1f}bp なら、"
              f"**入れ替わるだけで悪化**します。")
        print(f"  ▶ 判断は --sweep-ops(watch50 と予算を通す)でのみ可能です。")
    print(f"\n  ⚠ 『合計 bp/件』が下がっていても、**件数が増えていれば月の総額は"
          f"増えうる**")
    print(f"     (N は稼働率40%で資金が余っている)。ただし watch50 が先に効きます。")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.dump_picks:
    # ══ 実際に建てた明細を書き出す ══════════════════════════════════
    #   ⛔ どの銘柄を何株建てたかは **_ops_sim の中でしか決まらない**
    #     (watch50 → ギャップ → 予算 の順に効くため)。外から再現できない。
    _rows_p: list = []
    for _wn, _wf in (("TRAIN", _train),
                     ("TEST", _test if _test is not None else None)):
        if _wf is None or not len(_wf):
            continue
        _wp = _pool_of(_wf)
        if _wp.empty:
            continue
        _sim, _, _ = _make_ops_sim(_wf, _wp, max(1, _wp["date"].nunique()))
        _r = _sim(50, a.budget_man or 400.0, 0, False)
        for _p in _r["picks"]:
            _p["win"] = _wn
            _rows_p.append(_p)
        print(f"[dump] {_wn}: {len(_r['picks']):,}件 / {_r['pnl']:+,.0f}円")
    if not _rows_p:
        sys.exit("[error] 建てた明細がありません")
    pd.DataFrame(_rows_p).to_csv(a.dump_picks, index=False,
                                 encoding="utf-8-sig")
    print(f"[dump] {a.dump_picks} に {len(_rows_p):,}行 書きました")
    print(f"  ★ 次: python analyze_portfolio_stop.py --picks {a.dump_picks}")
    sys.exit(0)

if a.hedge:
    # ══════════════════════════════════════════════════════════════════
    # ★★ 同日ヘッジ — 寄りで日経先物を買い、引けで売る
    # ══════════════════════════════════════════════════════════════════
    #   ⛔ **これは予測ではない。** 当日の相場を当てる必要がないので、
    #     §18.60 の「①相場で説明できる / ②寄り前には読めない」の壁を回避する。
    #     N は同日決済(寄り→引け)なので、ヘッジも同じ区間で完結する。
    #
    #   §18.30 で lss のヘッジを棄却した理由は **R²=0.063 と小さすぎた**こと。
    #   N は R²=0.235(§18.60)で前提が違うため、測り直す価値がある。
    #
    #   ⚠ 見るのは **総額ではなく 月平均÷σ**。ヘッジは α を変えずに σ を削る
    #     のが狙いなので、総額が動かないのが正常(§18.28 / §18.38 の逆)。
    import math as _math

    _ht = _pool_of(_train)
    _he = _pool_of(_test) if _test is not None and len(_test) else None
    if _ht.empty:
        sys.exit("[error] TRAIN が空です")
    try:
        from preopen_market import sameday_features
    except Exception as _e:
        sys.exit(f"[error] 同日の日経を取れません: {_e}")

    print(f"\n{'=' * 78}")
    print(f"■ 同日ヘッジ — 寄りで日経先物を買い、引けで売る")
    print(f"{'=' * 78}")
    print(f"  ⛔ **予測ではありません**。当日の相場を当てる必要がないので、"
          f"§18.60 の壁(寄り前には読めない)を回避します")
    print(f"  ⚠ 見るのは **月平均÷σ**。総額が変わらないのが正常です"
          f"(α は変えず σ だけ削るのが狙い)")
    print(f"  先物コスト {a.hedge_cost_bp:.1f}bp(往復 / 建玉に対して)")

    def _panel(src, pool):
        _nd = max(1, pool["date"].nunique())
        _sim, _, _ = _make_ops_sim(src, pool, _nd)
        _r = _sim(50, a.budget_man or 400.0, 0, False)
        _d = {str(k): (v[0] + v[1]) for k, v in _r["daily"].items()}
        _c = {str(k): (v[0] + v[1]) for k, v in _r["dcap"].items()}
        _ks = sorted(_d)
        _sd = sameday_features(_ks)
        _keep = [k for k in _ks if k in _sd]
        return (_keep,
                np.array([_d[k] for k in _keep], float),
                np.array([_c.get(k, 0.0) for k in _keep], float),
                np.array([_sd[k]["n225_same_day"] for k in _keep], float))

    _kd, _pn, _cap, _mk = _panel(_train, _ht)
    if len(_kd) < 100:
        sys.exit(f"[error] TRAIN の有効日が少なすぎます({len(_kd)}日)")

    # β = 日経の日中1%あたり N の損益がいくら動くか(TRAIN で推定)
    _b1 = np.polyfit(_mk, _pn, 1)
    _beta, _alpha = float(_b1[0]), float(_b1[1])
    _pred = np.polyval(_b1, _mk)
    _r2 = 1.0 - ((_pn - _pred) ** 2).sum() / ((_pn - _pn.mean()) ** 2).sum()
    print(f"\n  ── TRAIN({_train_n}) {len(_kd):,}営業日 ──")
    print(f"    β = **{_beta:+,.0f}円 / 日経日中+1%** / R² {_r2:.3f} / "
          f"α {_alpha:+,.0f}円/日")
    print(f"    平均投入 {_cap.mean():,.0f}円/日")
    # ★★ ヘッジは σ だけでなく **『市場ベータ由来の利益』も消す**。
    #   N はショートなので、日経が日中に平均で下がっていればその分は
    #   ベータの取り分。ヘッジすると σ と一緒に消える。
    #   合成データの検算(2026-08-28)で、σ −7.9% と 平均 −8.2% が相殺して
    #   月平均÷σ が **1ミリも改善しない**ケースを確認した。
    #   §18.19 では日中ドリフトはゼロ(t=0.59)なので実データでは小さいはずだが、
    #   **出さないと判断できない**。
    _mkm = float(_mk.mean())
    _lose = -_beta * _mkm            # 比率1.0 のとき1日あたり失う利益
    print(f"    日経の日中% 平均 = **{_mkm:+.3f}%**"
          f"(§18.19 ではドリフトはゼロ)")
    print(f"    → 比率1.0 で失う『ベータ由来の利益』= "
          f"**{_lose:+,.0f}円/日**(月 {_lose * 20:+,.0f}円)")
    if abs(_lose * 20) > abs(_pn.mean() * 20) * 0.3:
        print(f"      ⛔ 月平均 {_pn.mean() * 20:+,.0f}円 の3割を超えます。"
              f"**ヘッジは σ と一緒に利益も削ります**")
    _need = -_beta        # β<0 なので買いヘッジ。1%あたりこの額だけ利益が要る
    print(f"    ヘッジ比率1.0 = 日経+1% で **{_need:+,.0f}円** 得るポジション")
    print(f"      = 日経マイクロ先物(指数×10)なら 約 **{_need / 4200:.1f}枚** 相当"
          f"(日経42,000円のとき1枚が+1%で約4,200円)")

    def _stat(_p):
        _m: dict = {}
        for _i, _d in enumerate(_kd):
            _m[_d[:7]] = _m.get(_d[:7], 0.0) + float(_p[_i])
        _v = np.array([_m[k] for k in sorted(_m)], float)
        _mu = float(_v.mean())
        _sd2 = float(_v.std(ddof=1)) if len(_v) > 1 else 0.0
        _t = _mu / (_sd2 / _math.sqrt(len(_v))) if _sd2 > 0 else 0.0
        return _mu, _sd2, (_mu / _sd2 if _sd2 else 0.0), _t, int((_v > 0).sum()), len(_v)

    def _apply(_p, _mkt, _capv, _h):
        """ヘッジ後の日次損益。日経が +x% のとき +_need*_h*x/1 を得る。"""
        _g = (-_beta) * _h * (_mkt / 1.0)
        _cost = abs((-_beta) * _h) * 100.0 * (a.hedge_cost_bp / 10_000.0)
        return _p + _g - _cost

    _hs = [float(x) for x in a.hedge_ratios.split(",") if x.strip()]
    for _lbl, _kk, _pp, _cc, _mm in (("TRAIN", _kd, _pn, _cap, _mk),) + (
            (("TEST", *_panel(_test, _he)),) if _he is not None and len(_he) else ()):
        if _lbl == "TEST":
            _kd, _pn, _cap, _mk = _kk, _pp, _cc, _mm
            if len(_kd) < 100:
                print(f"\n  ⛔ TEST の有効日が少なすぎます({len(_kd)}日)")
                break
            _bt = np.polyfit(_mk, _pn, 1)
            print(f"\n  ── TEST({_test_n}) {len(_kd):,}営業日 ──")
            print(f"    ⚠ TEST の β = {float(_bt[0]):+,.0f}円/1% "
                  f"(TRAIN {_beta:+,.0f})。**大きく違えば比率を固定できません**")
        print(f"    {'比率':<8}{'月平均':>12}{'月次σ':>12}{'÷σ':>8}"
              f"{'t':>8}{'プラス月':>10}")
        _base = None
        for _h in _hs:
            _q = _apply(_pn, _mk, _cap, _h)
            _mu, _sd2, _rt, _t, _pos, _nm = _stat(_q)
            if _h == 0.0:
                _base = _rt
            _mk2 = " ★現行" if _h == 0.0 else (
                "  ✅" if (_base is not None and _rt > _base * 1.10) else "")
            print(f"    {_h:<8.2f}{_mu:>+12,.0f}{_sd2:>12,.0f}{_rt:>8.2f}"
                  f"{_t:>+8.2f}{f'{_pos}/{_nm}':>10}{_mk2}")

    print(f"\n  {'=' * 68}")
    print(f"  ★ 判定:")
    print(f"    ・**月平均÷σ が比率0(現行)より明確に高い**比率があるか")
    print(f"    ・その比率が **TRAIN と TEST で同じ**か(違えば固定できない)")
    print(f"    ・TEST の β が TRAIN と近いか(§18.30 では lss の月別βが"
          f"**2ヶ月符号が逆**だった)")
    print(f"  ⛔ 総額(月平均)が増えることは期待しないこと。σ を削るのが狙いです")
    print(f"  ⚠ 先物のコスト・証拠金・kabu の対応は**この数字に入っていません**")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.tail_diag:
    # ══════════════════════════════════════════════════════════════════
    # ★★ 大負けの日を寄り前に読めるか — **順番を追って**測る
    # ══════════════════════════════════════════════════════════════════
    #   ⛔ §18.34b(lss/19変数/113日)も 2026-08-27 の --sweep-market も
    #      「19変数を掃いて候補ゼロ」で終わった。同じことを繰り返さないため、
    #      **先に予測可能性の上限を測る**。
    #
    #      ① 事後: N の日次損益は **同日の相場**でどこまで説明できるか
    #         → ここが小さければ、寄り前の変数がどれだけ優秀でも届かない
    #      ② 事前→事後: 寄り前変数は **同日の相場**をどこまで読めるか
    #      ③ 事前→損益: 寄り前変数で **N の損益**を直接読めるか
    #         (a)単変量 (b)裾に特化 (c)多変量
    #
    #   ①×② が ③ の上限。①が 0.05 で ②が 0.05 なら ③ は 0.0025 相当で、
    #   何を掃いても出ない。**出ないことの理由が分かる**のが今回の目的。
    import math as _math

    _tt = _pool_of(_train)
    _te = _pool_of(_test) if _test is not None and len(_test) else None
    if _tt.empty:
        sys.exit("[error] TRAIN が空です")
    _tnd = max(1, _tt["date"].nunique())
    print(f"\n{'=' * 78}")
    print(f"■ 大負けの日を寄り前に読めるか — **順番を追って測る**")
    print(f"{'=' * 78}")
    print(f"  TRAIN {_train_n} / {_tnd:,}営業日"
          + (f" / TEST {_test_n}" if _te is not None else " / TEST なし"))
    print(f"  ⚠ 判定は **日次パネル**(1日1行)。実効サンプルは取引件数ではなく"
          f"**営業日数**です(§18.13 の同日相関)")

    # ── 日次損益パネルを作る(実運用と同じ watch50 / 予算400万 の経路) ────
    def _daily_panel(src, pool, nd):
        _sim, _, _ = _make_ops_sim(src, pool, nd)
        _r = _sim(50, a.budget_man or 400.0, 0, False)
        _d = {str(k): (v[0] + v[1]) for k, v in _r["daily"].items()}
        _ks = sorted(_d)
        return _ks, np.array([_d[k] for k in _ks], float)

    _kd, _pn = _daily_panel(_train, _tt, _tnd)
    print(f"\n  ── ① 日次損益パネル (watch50 / 予算{a.budget_man or 400:.0f}万) ──")
    print(f"    {len(_kd):,}営業日 / 平均 {_pn.mean():+,.0f}円 / "
          f"σ {_pn.std(ddof=1):,.0f}円")
    _thr = float(np.quantile(_pn, a.tail_q))
    _bad = _pn <= _thr
    print(f"    『大負けの日』= 下位{a.tail_q * 100:.0f}% = {int(_bad.sum()):,}日 "
          f"(損益 ≤ {_thr:+,.0f}円) / その合計 {_pn[_bad].sum():+,.0f}円")
    print(f"    ⚠ 全体 {_pn.sum():+,.0f}円 に対し、この{int(_bad.sum())}日だけで "
          f"{_pn[_bad].sum():+,.0f}円 = **少数の日が損益を支配**")

    # ── ② 事後: 同日の相場でどこまで説明できるか(=上限) ─────────────
    try:
        from preopen_market import sameday_features, SAMEDAY_LABELS
        _sd = sameday_features(_kd)
    except Exception as _e:
        _sd, SAMEDAY_LABELS = {}, {}
        print(f"\n  ⛔ 同日の相場を取得できません({_e})。①の上限が測れないので"
              f"以降は参考値です")

    def _fit(x, y):
        """単回帰。(β, R², t) を返す。"""
        _m = np.isfinite(x) & np.isfinite(y)
        x, y = x[_m], y[_m]
        if len(x) < 30 or x.std() == 0:
            return float("nan"), float("nan"), float("nan"), 0
        _b = np.polyfit(x, y, 1)
        _p = np.polyval(_b, x)
        _ss = ((y - y.mean()) ** 2).sum()
        _r2 = 1.0 - ((y - _p) ** 2).sum() / _ss if _ss > 0 else float("nan")
        _se = _math.sqrt(max(((y - _p) ** 2).sum() / (len(x) - 2), 0)
                         / max(((x - x.mean()) ** 2).sum(), 1e-12))
        return float(_b[0]), float(_r2), (_b[0] / _se if _se > 0 else 0.0), len(x)

    _cap = float("nan")
    if _sd:
        print(f"\n  ── ② 【事後・上限】同日の相場でどこまで説明できるか ──")
        print(f"    ⛔ これは **寄り前には分かりません**。予測可能性の"
              f"**天井**を知るためだけの数字です")
        print(f"    {'変数':<34}{'β(円/1%)':>12}{'R²':>8}{'t':>8}")
        for _k, _lb in SAMEDAY_LABELS.items():
            _x = np.array([_sd.get(d, {}).get(_k, np.nan) for d in _kd], float)
            _b, _r2, _t, _n = _fit(_x, _pn)
            if _n:
                print(f"    {_lb[:33]:<34}{_b:>+12,.0f}{_r2:>8.3f}{_t:>+8.2f}")
                if _k == "n225_same_day":
                    _cap = _r2
        _x = np.array([_sd.get(d, {}).get("n225_same_day", np.nan) for d in _kd],
                      float)
        _mb = np.isfinite(_x)
        if _mb.any():
            _bd = _bad & _mb
            print(f"\n    大負けの日({int(_bd.sum())}日)の 日経 日中%: "
                  f"平均 {_x[_bd].mean():+.2f}% / 全日 {_x[_mb].mean():+.2f}%")
            print(f"    大負けの日のうち **日経が日中プラスだった日**: "
                  f"{int((_x[_bd] > 0).sum())}/{int(_bd.sum())} "
                  f"({(_x[_bd] > 0).mean() * 100:.0f}%) "
                  f"— 全日では {(_x[_mb] > 0).mean() * 100:.0f}%")
        if _cap == _cap:
            print(f"\n    ★★ **予測可能性の上限 R² = {_cap:.3f}**")
            if _cap < 0.10:
                print(f"       ⛔ 同日の相場(=完璧な後知恵)ですら {_cap * 100:.0f}%"
                      f"しか説明できません。")
                print(f"          **大負けは銘柄固有**で、寄り前の市場変数では"
                      f"原理的に読めません。")
            else:
                print(f"       ★ 相場で {_cap * 100:.0f}% 説明できます。"
                      f"あとは『その相場を寄り前に読めるか』(③)次第です")

    # ── ③ 事前: 寄り前変数 ─────────────────────────────────────────
    try:
        from preopen_market import preopen_features, LABELS as FEAT_LABELS
    except Exception:
        try:
            from preopen_market import preopen_features
            FEAT_LABELS = {}
        except Exception as _e:
            sys.exit(f"[error] preopen_market を読めません: {_e}")
    # ⛔⛔ **TRAIN と TEST で同じ関数を使うこと**(2026-08-28 に踏んだ)。
    #   TRAIN 側だけに self_* と *_abs を足していたので、TEST では
    #   41変数中23本が全部 NaN になり **有効日ゼロ**で ③-c が動かなかった。
    #   §18.32「鏡像の複製が TRAIN にしか掛かっていなかった」と同じ形。
    #   §18.48 ⑧d の教訓どおり、**実装は1つにする**。
    def _build_feats(_kk, _pp):
        _f = preopen_features(_kk)

        # ★★ **N 自身の履歴**。外部データが要らず、確実に寄り前に確定して
        #   いて、しかも一度も測っていない。『明らかにそういう相場』に
        #   いちばん近いのはこれ(悪い地合いが続く / ボラが上がっている)。
        #   ⛔ すべて **前日まで**。当日の値は一切使わない。
        #   ⚠ 窓の先頭21日は履歴が足りないので自然に落ちる。
        for _i2, _d2 in enumerate(_kk):
            _h = _pp[:_i2]                    # ← 当日を含まない
            if len(_h) < 21:
                continue
            _neg = 0
            for _x2 in _h[::-1]:              # 連続マイナス日数
                if _x2 < 0:
                    _neg += 1
                else:
                    break
            _f.setdefault(_d2, {}).update({
                "self_prev": float(_h[-1]),                  # 前日の損益
                "self_5d": float(_h[-5:].sum()),             # 直近5日
                "self_20d": float(_h[-20:].sum()),           # 直近20日
                "self_vol20": float(_h[-20:].std(ddof=1)),   # 直近20日のσ
                "self_win20": float((_h[-20:] > 0).mean()),  # 直近20日の勝日率
                "self_negstreak": float(_neg),
            })

        # ★ 非線形: **大きさ**(絶対値)。「大きく動いた日」は符号ではなく幅。
        _absk = [k for k in {kk2 for v in _f.values() for kk2 in v}
                 if k.endswith(("_ret", "_chg", "_gap"))
                 and not k.startswith("self_")]
        for _d2, _f2 in _f.items():
            for _k2 in _absk:
                if _k2 in _f2 and f"{_k2}_abs" not in _f2:
                    _f2[f"{_k2}_abs"] = abs(_f2[_k2])
        return _f

    _pf = _build_feats(_kd, _pn)

    _vars = sorted({k for v in _pf.values() for k in v})
    _n_ext = len([v for v in _vars if not v.startswith("self_")
                  and not v.endswith("_abs")])
    _n_abs = len([v for v in _vars if v.endswith("_abs")])
    _n_slf = len([v for v in _vars if v.startswith("self_")])
    print(f"\n  ── ③ 寄り前変数 ── **{len(_vars)}本** "
          f"(外部 {_n_ext} / 絶対値 {_n_abs} / **N自身の履歴 {_n_slf}**)")
    print(f"     {', '.join(_vars)}")
    if _n_ext <= 6:
        print(f"    ⛔⛔ **6本以下はダミーの可能性があります**"
              f"(本物は15〜19本)。preopen_market.py を確認してください")

    _X = np.array([[_pf.get(d, {}).get(v, np.nan) for v in _vars] for d in _kd],
                  float)

    if _sd:
        print(f"\n  ── ③-0 【事前→事後】寄り前変数は『同日の相場』を読めるか ──")
        _y0 = np.array([_sd.get(d, {}).get("n225_same_day", np.nan) for d in _kd],
                       float)
        _rr = []
        for _i, _v in enumerate(_vars):
            _b, _r2, _t, _n = _fit(_X[:, _i], _y0)
            if _n:
                _rr.append((_r2, _t, _v))
        _rr.sort(reverse=True)
        print(f"    {'変数':<16}{'R²':>8}{'t':>8}")
        for _r2, _t, _v in _rr[:5]:
            print(f"    {FEAT_LABELS.get(_v, _v)[:15]:<16}{_r2:>8.3f}{_t:>+8.2f}")
        _best2 = _rr[0][0] if _rr else float("nan")
        if _cap == _cap and _best2 == _best2:
            print(f"    ★ ①×② の目安 = {_cap:.3f} × {_best2:.3f} = "
                  f"**{_cap * _best2:.4f}** ← ③で出せる R² のおおよその天井")

    # ── ③-a 単変量 × 分位 (平均と裾の両方) ───────────────────────────
    _nq = max(2, a.nq)
    print(f"\n  ── ③-a 【事前→損益】単変量 × {_nq}分位 ──")
    print(f"    ⚠ 『裾集中』= 大負けの日がその分位にどれだけ偏るか。"
          f"均等なら {100 / _nq:.0f}%")
    print(f"    ⛔ **帰無を2本出します**(2026-08-27 に合成データで検証):")
    print(f"       巡回 = 日ブロックを保つ。持続する変数では **効果そのものを"
          f"吸収して見逃す**(保守的)")
    print(f"       シャフ = 日を並べ替える。日次パネルなので同日相関は既に"
          f"潰れており、pnl の自己相関がゼロなら妥当")
    print(f"    {'変数':<16}{'最悪分位':>9}{'平均/日':>12}{'裾集中':>8}"
          f"{'巡回95%':>9}{'シャフ95%':>10}{'判定':>8}")

    _rng = np.random.default_rng(a.seed)
    _hits = []
    for _i, _v in enumerate(_vars):
        _x = _X[:, _i]
        _m = np.isfinite(_x)
        if _m.sum() < 200:
            continue
        try:
            _q = pd.qcut(pd.Series(_x[_m]), _nq, labels=False, duplicates="drop")
        except Exception:
            continue
        _q = np.asarray(_q, float)
        if len(set(_q[np.isfinite(_q)])) < 2:
            continue
        _pm, _bm = _pn[_m], _bad[_m]

        def _gm(_arr, _lab, _k):          # 空グループは nan(落とさない)
            _sel = _lab == _k
            return float(_arr[_sel].mean()) if _sel.any() else float("nan")

        _ks2 = sorted({int(v) for v in _q if np.isfinite(v)})
        _means = np.array([_gm(_pm, _q, k) for k in _ks2], float)
        if not np.isfinite(_means).any():
            continue
        _worst = _ks2[int(np.nanargmin(_means))]
        _wi = _ks2.index(_worst)
        _conc = float(_bm[_q == _worst].mean()) if (_q == _worst).any() else 0.0
        # 帰無を2本。どちらも **同じ『最悪分位を選ぶ』操作**を掛ける。
        _nl, _ns = [], []
        _L = len(_pm)

        def _null_once(_pr, _br, _acc):
            _mn = np.array([_gm(_pr, _q, k) for k in _ks2], float)
            if np.isfinite(_mn).any():
                _acc.append(float(_br[_q == _ks2[int(np.nanargmin(_mn))]].mean()))

        for _s2 in range(a.tail_nulls):
            _sh = int(_rng.integers(1, _L)) if _L > 2 else 1
            _null_once(np.roll(_pm, _sh), np.roll(_bm, _sh), _nl)   # 巡回
            _pi = _rng.permutation(_L)
            _null_once(_pm[_pi], _bm[_pi], _ns)                     # シャッフル
        if not _nl or not _ns:
            continue
        _p95 = float(np.quantile(_nl, 0.95))
        _s95 = float(np.quantile(_ns, 0.95))
        _ok = _conc > _p95                       # 両方超えたときだけ ✅
        _okS = _conc > _s95
        _jd = ("  ✅両方" if (_ok and _okS) else
               "  △期間" if _okS else "  —")
        if _ok and _okS:
            _hits.append((_v, _worst, _conc, _p95))
        print(f"    {FEAT_LABELS.get(_v, _v)[:15]:<16}Q{_worst + 1:<8}"
              f"{_means[_wi]:>+12,.0f}{_conc * 100:>7.0f}%"
              f"{_p95 * 100:>8.0f}%{_s95 * 100:>9.0f}%{_jd:>8}")
    _ac = (float(np.corrcoef(_pn[:-1], _pn[1:])[0, 1]) if len(_pn) > 30
           else float("nan"))
    print(f"    掃いた {len(_vars)}本 / **両方の帰無を超えた {len(_hits)}本** "
          f"(帰無の期待 {len(_vars) * 0.05:.1f}本)")
    print(f"    ⚠ 日次損益の1日ラグ自己相関 = **{_ac:+.3f}**。"
          f"ゼロに近いほどシャッフル帰無が妥当です")
    print(f"    ⚠ **△期間** = シャッフルだけ超えた = 『その変数が悪い』と"
          f"『たまたま悪い時期がその変数の値と重なった』を区別できない")
    print(f"    ★ 読み方(合成データで検証済み / 2026-08-27):")
    print(f"      ・自己相関がゼロ近く かつ ✅/△ が0本 → **本当に何も無い**")
    print(f"      ・△/✅ が**多数**出た かつ 自己相関が高い → 『時期』の構造は"
          f"あるが、**どの変数かは分離できていない**")
    print(f"        (持続する変数は互いに似た形になるため。③-c の TEST で確かめる)")
    print(f"      ・効果が実在すると **損益自体に自己相関が現れます**"
          f"(合成で ar=0 から +0.30 が出た)")

    # ── ③-b 2変数の交互作用 ────────────────────────────────────────
    #   「VIX が高くて **かつ** 先物がギャップアップ」のような組み合わせ。
    #   単変量では消えても、条件を重ねると出ることがある。
    #   ⛔ ペア数が多いので多重検定が強く効く。**帰無にも同じ『最良ペアを
    #     選ぶ』操作を掛ける**ので、何ペア試しても比較は公平(§18.34b)。
    if a.tail_pairs:
        _cd = [v for v in _vars
               if np.isfinite(_X[:, _vars.index(v)]).mean() > 0.9]
        _npair = len(_cd) * (len(_cd) - 1) // 2
        print(f"\n  ── ③-b 2変数の交互作用 ({len(_cd)}本 → {_npair}ペア) ──")
        _lo, _hi = {}, {}
        for _v in _cd:
            _x = _X[:, _vars.index(_v)]
            _m = np.isfinite(_x)
            _lo[_v] = _m & (_x <= np.quantile(_x[_m], 1.0 / 3.0))
            _hi[_v] = _m & (_x >= np.quantile(_x[_m], 2.0 / 3.0))
        _cells = []
        for _i2 in range(len(_cd)):
            for _j2 in range(_i2 + 1, len(_cd)):
                for _sa, _da in ((_lo, "低"), (_hi, "高")):
                    for _sb, _db in ((_lo, "低"), (_hi, "高")):
                        _sel = _sa[_cd[_i2]] & _sb[_cd[_j2]]
                        if int(_sel.sum()) >= a.tail_pair_min:
                            _cells.append((_sel, _cd[_i2], _da, _cd[_j2], _db))
        print(f"    条件を満たすセル {len(_cells):,}個 "
              f"(最小 {a.tail_pair_min}日)")
        if not _cells:
            print(f"    ⛔ セルがありません(--tail-pair-min を下げてください)")
        else:
            def _pairbest(_p):
                _bv, _bi = np.inf, -1
                for _ix, _c in enumerate(_cells):
                    _mu2 = float(_p[_c[0]].mean())
                    if _mu2 < _bv:
                        _bv, _bi = _mu2, _ix
                return _bv, _bi

            _bv, _bi = _pairbest(_pn)
            _nn = max(50, a.tail_nulls // 20)
            _nl2, _ns2 = [], []
            for _s3 in range(_nn):
                _nl2.append(_pairbest(np.roll(_pn, int(_rng.integers(
                    1, len(_pn)))))[0])
                _ns2.append(_pairbest(_rng.permutation(_pn))[0])
            _p05 = float(np.quantile(_nl2, 0.05))
            _s05 = float(np.quantile(_ns2, 0.05))
            _, _a2, _da2, _b2, _db2 = _cells[_bi]
            _n2 = int(_cells[_bi][0].sum())
            print(f"    最悪のセル: {FEAT_LABELS.get(_a2, _a2)} が{_da2} "
                  f"かつ {FEAT_LABELS.get(_b2, _b2)} が{_db2}")
            print(f"      {_n2}日 / 平均 {_bv:+,.0f}円/日 "
                  f"(全日 {_pn.mean():+,.0f}円/日)")
            print(f"      帰無({_nn}本 / 同じ『最良セルを選ぶ』操作つき)")
            print(f"        巡回 5%点 {_p05:+,.0f}円 → "
                  f"{'✅ 外' if _bv < _p05 else '— 中'}"
                  f"   シャッフル 5%点 {_s05:+,.0f}円 → "
                  f"{'✅ 外' if _bv < _s05 else '— 中'}")
            print(f"        判定: "
                  + ("**✅ 両方の帰無の外**" if (_bv < _p05 and _bv < _s05)
                     else "**△ 期間効果**(シャッフルだけ外 = "
                          "『この組み合わせが悪い』と『悪い時期と重なった』を"
                          "区別できない)" if _bv < _s05
                     else "— 帰無の中"))
            print(f"    ⚠ 帰無の平均が全日平均より低いのは正常です。"
                  f"**{len(_cells):,}セルから最小を選ぶ操作**だけで下がります")

    # ── ③-c 多変量: TRAIN で作って TEST で答え合わせ ──────────────────
    print(f"\n  ── ③-c 【多変量】TRAIN で線形モデル → TEST で答え合わせ ──")
    _fin = np.isfinite(_X).all(axis=1)
    if _fin.sum() < 200:
        # 欠損の多い変数を落として作り直す
        _keep = [i for i in range(len(_vars))
                 if np.isfinite(_X[:, i]).mean() > 0.9]
        _X2, _v2 = _X[:, _keep], [_vars[i] for i in _keep]
        _fin = np.isfinite(_X2).all(axis=1)
    else:
        _X2, _v2 = _X, _vars
    if _fin.sum() < 200:
        print(f"    ⛔ 欠損が多すぎて作れません({int(_fin.sum())}日)")
    else:
        _Xt, _yt = _X2[_fin], _pn[_fin]
        _mu, _sg = _Xt.mean(axis=0), _Xt.std(axis=0)
        _sg[_sg == 0] = 1.0
        _Z = np.hstack([np.ones((len(_Xt), 1)), (_Xt - _mu) / _sg])
        _co, *_ = np.linalg.lstsq(_Z, _yt, rcond=None)
        _pred = _Z @ _co
        _ss = ((_yt - _yt.mean()) ** 2).sum()
        _r2in = 1.0 - ((_yt - _pred) ** 2).sum() / _ss if _ss > 0 else 0.0
        print(f"    TRAIN {int(_fin.sum()):,}日 / 変数 {len(_v2)}本 / "
              f"**in-sample R² {_r2in:.4f}**")
        _cut = float(np.quantile(_pred, 0.20))
        _skip = _pred <= _cut
        print(f"    TRAIN で『予測が下位20%の日は建てない』: "
              f"{_yt.sum():+,.0f} → {_yt[~_skip].sum():+,.0f}円 "
              f"({_yt[~_skip].sum() - _yt.sum():+,.0f})")
        # TEST
        if _te is not None and len(_te):
            _tnd2 = max(1, _te["date"].nunique())
            _kd2, _pn2 = _daily_panel(_test, _te, _tnd2)
            _pf2 = _build_feats(_kd2, _pn2)   # ★ TRAIN と同じ関数
            _X3 = np.array([[_pf2.get(d, {}).get(v, np.nan) for v in _v2]
                            for d in _kd2], float)
            _f2 = np.isfinite(_X3).all(axis=1)
            if _f2.sum() >= 100:
                _Z2 = np.hstack([np.ones((int(_f2.sum()), 1)),
                                 (_X3[_f2] - _mu) / _sg])
                _p2, _y2 = _Z2 @ _co, _pn2[_f2]
                _ss2 = ((_y2 - _y2.mean()) ** 2).sum()
                _r2out = (1.0 - ((_y2 - _p2) ** 2).sum() / _ss2
                          if _ss2 > 0 else 0.0)
                _sk2 = _p2 <= _cut
                _d2 = _y2[~_sk2].sum() - _y2.sum()
                print(f"\n    ★ TEST {int(_f2.sum()):,}日 / "
                      f"**out-of-sample R² {_r2out:.4f}**")
                print(f"    TEST で同じルール(予測が閾値以下の日は建てない): "
                      f"{_y2.sum():+,.0f} → {_y2[~_sk2].sum():+,.0f}円 "
                      f"(**{_d2:+,.0f}**) / 降りた日 {int(_sk2.sum())}日")
                if _r2out <= 0:
                    print(f"    ⛔ **out-of-sample R² がマイナス** = "
                          f"平均を予測に使うより悪い。モデルに予測力なし")
            else:
                _ms = [(_v3, float(np.isfinite(_X3[:, _i3]).mean()))
                       for _i3, _v3 in enumerate(_v2)]
                _ms = [m for m in _ms if m[1] < 0.5]
                print(f"    ⛔ TEST の有効日が少なすぎます"
                      f"({int(_f2.sum())}日 / {len(_kd2)}日中)")
                if _ms:
                    print(f"       欠損が多い変数: "
                          + ", ".join(f"{m[0]}({m[1] * 100:.0f}%)"
                                      for m in _ms[:8]))
                    print(f"       ⚠ TRAIN と TEST で **同じ変数が作れていない**"
                          f"可能性があります")

    print(f"\n  {'=' * 68}")
    print(f"  ★ 読み方(この順で見る):")
    print(f"    ① 上限 R² が 0.10 未満 → **相場では説明できない**。③で何が出ても偶然")
    print(f"    ② 寄り前→同日 の R² が小さい → 相場が読めても寄り前には分からない")
    print(f"    ③ 裾が帰無を超えた本数が『期待 {len(_vars) * 0.05:.1f}本』を"
          f"明確に上回るか")
    print(f"    ④ 多変量の **out-of-sample R²** と TEST の実額。ここが本番")
    print(f"  ⛔ ①②が小さいのに③④で何か出たら、それは多重検定の産物です")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.sweep_market:
    # ══════════════════════════════════════════════════════════════════
    # ★★ 寄り前の外部指標で「その日は建てない」ルールが作れるか
    # ══════════════════════════════════════════════════════════════════
    #   ⚠ §18.34b で19変数を掃いて候補ゼロだったが、あれは **lss** で
    #     **113営業日**しかなかった。N は 2,843営業日 = 25倍。やる価値がある。
    #   ⛔ 帰無は日ブロックを保った巡回シフト。各シフトでも同じ全ルールを
    #     掃いて最良を取るので、**何通り試したかは自動的に補正される**。
    try:
        from preopen_market import preopen_features as _pof
    except Exception as _e:
        sys.exit(f"[error] preopen_market を読めません: {_e}")
    print(f"\n{'=' * 78}")
    print(f"■ 寄り前の外部指標で『その日は建てない』ルールが作れるか")
    print(f"{'=' * 78}")
    print(f"  ⚠ §18.34b は **lss / 113営業日**で候補ゼロ。"
          f"N は **{len(set(_train['date']) | set(_test['date'])):,}営業日**")

    _pan = {}
    for _t, _df in (("TRAIN", _train), ("TEST", _test)):
        if _df is None or _df.empty:
            continue
        _f, _, _ = _make_ops_sim(_df, _pool_of(_df),
                                 max(1, _df["date"].nunique()))
        _r = _f(50, 400.0, 0, False)
        _pan[_t] = {d: (v[0] + v[1]) for d, v in _r["daily"].items()}

    _alld = sorted(set().union(*[set(v) for v in _pan.values()]))
    print(f"  寄り前の指標を取得します({len(_alld):,}営業日ぶん / yfinance)…",
          flush=True)
    _feat = _pof(_alld)
    _vars: dict = {}
    for _d, _fv in _feat.items():
        for _k, _v in (_fv or {}).items():
            _vars.setdefault(_k, {})[_d] = float(_v)
    _vars = {k: v for k, v in _vars.items() if len(v) >= len(_alld) * 0.7}
    if not _vars:
        sys.exit("[error] 寄り前の指標が1つも取れませんでした")
    print(f"  使える指標 **{len(_vars)}本**: " + ", ".join(sorted(_vars))[:200])

    def _mk_arr(tag):
        _dd3 = sorted(d for d in _pan[tag]
                      if all(d in v for v in _vars.values()))
        _y = np.array([_pan[tag][d] for d in _dd3], float)
        _X = {k: np.array([_vars[k][d] for d in _dd3], float) for k in _vars}
        _mo = np.array([int(str(d)[:4]) * 12 + int(str(d)[5:7]) for d in _dd3])
        return _y, _X, _mo, _dd3

    def _sc(y, mo):
        _u = np.unique(mo)
        _m = np.array([y[mo == x].sum() for x in _u], float)
        if len(_m) < 3:
            return 0.0, 0.0, 0.0, 0
        _mu, _sd = float(_m.mean()), float(_m.std(ddof=1))
        return _mu, _sd, (_mu / _sd if _sd else 0.0), int((_m < 0).sum())

    # ルール: 「指標 v が 分位 q より 低い日 / 高い日 は建てない」
    _RL = [(k, q, side) for k in _vars for q in (20, 40, 60, 80)
           for side in ("low", "high")]

    def _ap(y, X, rule, cuts=None):
        _k, _q, _sd2 = rule
        _v = X[_k]
        _c = np.percentile(_v, _q) if cuts is None else cuts
        _z = y.copy()
        _z[(_v < _c) if _sd2 == "low" else (_v >= _c)] = 0.0
        return _z

    _out2 = {}
    for _t in _pan:
        _y, _X, _mo, _dd3 = _mk_arr(_t)
        _out2[_t] = (_y, _X, _mo)
        _mu, _sd, _r3, _ng2 = _sc(_y, _mo)
        print(f"  {_t}: {len(_y):,}日 / 基準 月平均 {_mu:+,.0f} / "
              f"÷σ {_r3:.2f} / 負月 {_ng2}")

    _yT, _XT, _moT = _out2["TRAIN"]
    _rank2 = []
    for _rule in _RL:
        _a2 = _sc(_ap(_yT, _XT, _rule), _moT)
        _rank2.append((_rule, _a2))
    _rank2.sort(key=lambda x: -x[1][2])
    _b3 = _sc(_yT, _moT)

    print(f"\n  ── 上位12 ({len(_RL)}通り中) ──")
    print(f"    {'指標':<14}{'閾値':>5}{'捨てる':>7}"
          + "".join(f"{t + ' 月平均':>13}{'÷σ':>7}{'負月':>6}" for t in _out2))
    for _rule, _a2 in _rank2[:12]:
        _k, _q, _sd2 = _rule
        _s4 = f"    {_k:<14}{_q:>4}%{('低い日' if _sd2 == 'low' else '高い日'):>7}"
        for _t in _out2:
            _y2, _X2, _mo2 = _out2[_t]
            _x2 = _sc(_ap(_y2, _X2, _rule), _mo2)
            _s4 += f"{_x2[0]:>+13,.0f}{_x2[2]:>7.2f}{_x2[3]:>6}"
        print(_s4)
    _s4 = f"    {'★基準(全部建てる)':<14}{'':>5}{'':>7}"
    for _t in _out2:
        _y2, _X2, _mo2 = _out2[_t]
        _x2 = _sc(_y2, _mo2)
        _s4 += f"{_x2[0]:>+13,.0f}{_x2[2]:>7.2f}{_x2[3]:>6}"
    print(_s4)

    print(f"\n  ── 帰無較正 ({a.switch_nulls}本 / 日ブロックを保った巡回シフト) ──")
    _rng3 = np.random.default_rng(20260827)
    _n3 = len(_yT)
    _nl2 = []
    for _i in range(max(10, a.switch_nulls)):
        _sh2 = int(_rng3.integers(20, _n3 - 20))
        _Xs = {k: np.roll(v, _sh2) for k, v in _XT.items()}
        _nl2.append(max(_sc(_ap(_yT, _Xs, r), _moT)[2] for r in _RL))
    _nl2 = np.array(_nl2, float)
    _obs2 = _rank2[0][1][2]
    _p952 = float(np.percentile(_nl2, 95))
    print(f"    実測の最良  **{_obs2:.3f}**  ({_rank2[0][0][0]} "
          f"{_rank2[0][0][1]}% {_rank2[0][0][2]})")
    print(f"    帰無の最良  中央 {float(np.median(_nl2)):.3f} / "
          f"95%点 **{_p952:.3f}** / 最大 {float(_nl2.max()):.3f}")
    print(f"    基準(全部建てる) {_b3[2]:.3f}")
    print(f"    p値 = **{float((_nl2 >= _obs2).mean()):.3f}**  "
          + ("← 帰無を超えています" if _obs2 > _p952 else
             "← 帰無の中。**偶然の範囲**です"))
    print(f"\n  {'=' * 68}")
    if _obs2 > _p952 and _obs2 > _b3[2]:
        print(f"  ★ TRAIN で帰無を超えました。**TEST の列**を見てください")
    else:
        print(f"  ⛔ **帰無の中です。**{len(_RL)}通り掃いても、"
              f"寄り前の外部指標で\n     『建てない日』は作れません"
              f"(§18.34b と同じ結論。今回は25倍のデータで確認)")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.sweep_size:
    # ══════════════════════════════════════════════════════════════════
    # ★★ マイナス月を抑えられるか — サイジング × 1日の件数上限
    # ══════════════════════════════════════════════════════════════════
    #   ⛔ 損切り(ATR倍/固定円)・利確・決済時刻・寄り5分撤退・ギャップ上限・
    #     寄り前19変数・銘柄属性 は **すべて棄却済み**(§18.55 / §18.56 /
    #     §18.34b / §18.13 / §18.24)。
    #   ★ 残っている唯一の未検証レバーが **サイジング**(§18.30):
    #     「FIXED_QTY=100 固定で建玉が 10万〜60万と6倍ばらついている。
    #       σ を削りたいなら1件ごとのバラつきを叩くしかない」
    #   ⚠ サイジングは決定的な変換なので、ラベルをずらす帰無較正は使えない。
    #     代わりに **TRAIN で選んで TEST で答え合わせ**+前半/後半で見る。
    print(f"\n{'=' * 78}")
    print(f"■ マイナス月を抑えられるか — サイジング × 1日の件数上限")
    print(f"{'=' * 78}")
    print(f"  ⛔ 損切り・利確・決済時刻・ギャップ上限・寄り前変数は棄却済み。")
    print(f"     残っている未検証レバーは **サイジング**だけ(§18.30)")
    print(f"  ⚠ 100株固定は建値で建玉が **6倍**ばらつく"
          f"(1,000円株=10万 / 6,000円株=60万)")

    def _mstat2(res, ond):
        _dd2 = res["daily"]
        _m: dict = {}
        for _d, _v in _dd2.items():
            _k = str(_d)[:7]
            _m[_k] = _m.get(_k, 0.0) + _v[0] + _v[1]
        _v2 = np.array([_m[k] for k in sorted(_m)], float)
        _dv = np.array([v[0] + v[1] for v in _dd2.values()], float)
        if len(_v2) < 3:
            return None
        _mu, _sd = float(_v2.mean()), float(_v2.std(ddof=1))
        _us = float(res["used"])
        return {"mu": _mu, "sd": _sd, "r": (_mu / _sd if _sd else 0.0),
                "worst": float(_v2.min()), "neg": int((_v2 < 0).sum()),
                "nm": len(_v2), "wd": float(_dv.min()),
                "n": res["n"], "used": _us,
                # ★ 資本効率 = 月平均 ÷ 実際に使った額。
                #   これが横ばいなら **レバレッジを動かしただけ**(§18.38 #3b)。
                "eff": (_mu / _us * 100.0 if _us > 0 else 0.0)}

    _SZ = [("qty", 0.0, "100株固定 ★現行"),
           ("equal", 0.0, "金額均等(予算÷件数)"),
           ("equal", 50.0, "金額均等 上限50万"),
           ("cap", 30.0, "100株固定 上限30万"),
           ("cap", 40.0, "100株固定 上限40万"),
           ("atr", 3000.0, "ATR均等(1件3千円risk)"),
           ("atr", 5000.0, "ATR均等(1件5千円risk)")]
    _CAPN = [0, 20, 13, 8, 5]

    _sims = {}
    for _tag, _df in (("TRAIN", _train), ("TEST", _test)):
        if _df is None or _df.empty:
            continue
        _f, _, _ = _make_ops_sim(_df, _pool_of(_df),
                                 max(1, _df["date"].nunique()))
        _sims[_tag] = (_f, max(1, _df["date"].nunique()))

    print(f"\n  ⛔ **投入/日 と 資本効率 を必ず見ること。**")
    print(f"     月平均もσも同じ比率で増えているなら、それは"
          f"**レバレッジを上げただけ**で\n     リスクは減っていません"
          f"(資本効率が横ばいならそれ)。§18.28 / §18.38 #3b")
    print(f"\n  {'サイジング':<24}{'上限':>6}"
          + "".join(f"{t + ' 月平均':>13}{'σ':>10}{'÷σ':>7}"
                   f"{'最悪月':>12}{'負月':>6}{'投入/日':>9}{'効率%':>7}"
                   for t in _sims))
    print("  " + "-" * (30 + 64 * len(_sims)))
    _rows2 = []
    for _sz, _sa, _lb in _SZ:
        for _mn in _CAPN:
            _r2 = {}
            for _t in _sims:
                _f, _ond2 = _sims[_t]
                _r2[_t] = _mstat2(_f(50, 400.0, _mn, False,
                                     sizing=_sz, size_arg=_sa), _ond2)
            if not all(_r2.values()):
                continue
            _rows2.append(((_sz, _sa, _lb, _mn), _r2))
    # 現行(100株固定・上限なし)を基準に
    _b2 = next(x for x in _rows2 if x[0][0] == "qty" and x[0][3] == 0)
    # TRAIN の 月平均÷σ 降順
    _rows2.sort(key=lambda x: -x[1]["TRAIN"]["r"])
    for _key, _r2 in _rows2[:16]:
        _sz, _sa, _lb, _mn = _key
        _s3 = f"  {_lb:<24}{('なし' if _mn == 0 else str(_mn)):>6}"
        for _t in _sims:
            _x = _r2[_t]
            _s3 += (f"{_x['mu']:>+13,.0f}{_x['sd']:>10,.0f}{_x['r']:>7.2f}"
                    f"{_x['worst']:>+12,.0f}{_x['neg']:>4}/{_x['nm']}"
                    f"{_x['used'] / 1e4:>8,.0f}万{_x['eff']:>7.2f}")
        print(_s3)
    print("  " + "-" * (30 + 64 * len(_sims)))
    _bs = _b2[1]
    _s3 = f"  {'★現行(100株/上限なし)':<24}{'なし':>6}"
    for _t in _sims:
        _x = _bs[_t]
        _s3 += (f"{_x['mu']:>+13,.0f}{_x['sd']:>10,.0f}{_x['r']:>7.2f}"
                f"{_x['worst']:>+12,.0f}{_x['neg']:>4}/{_x['nm']}"
                f"{_x['used'] / 1e4:>8,.0f}万{_x['eff']:>7.2f}")
    print(_s3)

    # ── 前半/後半 ────────────────────────────────────────────────
    print(f"\n  ── 上位5つを TRAIN の前半/後半でも見る ──")
    print(f"  {'サイジング':<24}{'上限':>6}{'前半 ÷σ':>10}{'後半 ÷σ':>10}"
          f"{'基準 前半':>11}{'基準 後半':>11}{'判定':>8}")
    _ft, _ond3 = _sims["TRAIN"]
    _bh = {h: _mstat2(_ft(50, 400.0, 0, False, half=h), _ond3) for h in (1, 2)}
    for _key, _r2 in _rows2[:5]:
        _sz, _sa, _lb, _mn = _key
        _hh2 = {h: _mstat2(_ft(50, 400.0, _mn, False, sizing=_sz,
                               size_arg=_sa, half=h), _ond3) for h in (1, 2)}
        if not all(_hh2.values()):
            continue
        _ok2 = (_hh2[1]["r"] > _bh[1]["r"]) and (_hh2[2]["r"] > _bh[2]["r"])
        print(f"  {_lb:<24}{('なし' if _mn == 0 else str(_mn)):>6}"
              f"{_hh2[1]['r']:>10.2f}{_hh2[2]['r']:>10.2f}"
              f"{_bh[1]['r']:>11.2f}{_bh[2]['r']:>11.2f}"
              f"{('✓' if _ok2 else '✗'):>8}")

    print(f"\n  {'=' * 68}")
    print(f"  ⛔ **100株が最小単位なので、サイジングは『大きくする』方向にしか"
          f"動けません。**\n     1単元より小さくできない以上、"
          f"高い銘柄のリスクを下げる手段がありません。")
    print(f"     σ を下げたいなら 予算を落とす(=レバレッジを下げる)か、"
          f"件数上限で\n     エクスポージャーを削るしかありません。")
    print(f"  ★ 読み方: **月平均を落とさずに σ・最悪月・マイナス月数が"
          f"下がる**ものだけが意味を持つ。")
    print(f"     月平均が落ちているなら、それは単にサイズを下げただけ"
          f"(レバレッジの調整)。")
    print(f"  ⚠ TRAIN で選んで **TEST の列と前半/後半 ✓** の両方を満たさなければ"
          f"期間依存。")
    print(f"  ⛔ 予算400万は固定。予算を変えるのは §18.38 #3b のとおり"
          f"レバレッジであって\n     リスク低減ではない(σ も比例して動く)")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.search_switch:
    # ══════════════════════════════════════════════════════════════════
    # ★★ N と鏡像の使い分けを **総当たり**して帰無で較正する
    # ══════════════════════════════════════════════════════════════════
    #   ⛔ 「何通り試したか」を制限するのではなく、**『最良を選ぶ』操作ごと
    #     帰無分布に入れる**(§18.13 / §18.24 の作法)。こうすれば何百通り
    #     試しても比較は公平になる。
    #   ⛔ 帰無は **日ブロックを保った巡回シフト**。完全シャッフルは日内相関を
    #     壊して帰無分布が狭くなり、偽陽性を過小評価する(§18.13)。
    if a.side != "both":
        sys.exit("[error] --search-switch は --side both と一緒に使ってください")
    print(f"\n{'=' * 78}")
    print(f"■ N と鏡像の使い分け — **総当たり + 帰無較正**")
    print(f"{'=' * 78}")

    def _sides_of(df):
        _s1 = df[df["side"] == 1] if "side" in df else df
        _s2 = df[df["side"] == -1] if "side" in df else df.iloc[0:0]
        return _s1, _s2

    def _panel(df, tag):
        """N単独 / 鏡像単独 / 両建て の **日次損益**を作る(全部 予算400万)。"""
        _s1, _s2 = _sides_of(df)
        _out = {}
        for _k, _d in (("N", _s1), ("M", _s2), ("B", df)):
            if _d is None or _d.empty:
                _out[_k] = {}
                continue
            _f, _, _ = _make_ops_sim(_d, _pool_of(_d),
                                     max(1, _d["date"].nunique()))
            _r = _f(50, 400.0, 0, False)
            _out[_k] = {_dt2: (_v[0] + _v[1]) for _dt2, _v in _r["daily"].items()}
        # 相場の指標(前夜に確定)。ret1 は鏡像側で反転済みなので side==1 だけ
        _mk2 = (_s1.groupby("date")["ret1"].mean().sort_index()
                .rename("mkt1").to_frame())
        _mk2["mkt5"] = _mk2["mkt1"].rolling(5).mean()
        _mk2["mkt20"] = _mk2["mkt1"].rolling(20).mean()
        _mk2["absmkt1"] = _mk2["mkt1"].abs()
        _cn2 = (_s1[_s1["ret1"] >= _RET1_MIN].groupby("date").size().rename("a"))
        _cm2 = (_s2[_s2["ret1"] >= _RET1_MIN].groupby("date").size().rename("b"))
        _cc3 = pd.concat([_cn2, _cm2], axis=1).fillna(0.0)
        _mk2["cand"] = ((_cc3["a"] / (_cc3["a"] + _cc3["b"]).replace(0, np.nan))
                        .reindex(_mk2.index) * 100.0)
        # ★ その日の候補の総数(=どれだけ材料が出た日か)。前夜に確定。
        #   §18.13 は lss で『同日発注数』を掃いて候補ゼロだったが、
        #   N/鏡像では未検証なので入れる。
        _mk2["ntot"] = (_cc3["a"] + _cc3["b"]).reindex(_mk2.index)
        _mk2 = _mk2.dropna()
        _days = [d for d in _mk2.index
                 if d in _out["N"] or d in _out["M"] or d in _out["B"]]
        _days = sorted(_days)
        _P = {k: np.array([_out[k].get(d, 0.0) for d in _days], float)
              for k in ("N", "M", "B")}
        _A = {c: _mk2.loc[_days, c].to_numpy(float)
              for c in ("mkt1", "mkt5", "mkt20", "absmkt1", "cand", "ntot")}
        _mo = np.array([int(str(d)[:4]) * 12 + int(str(d)[5:7]) for d in _days])
        print(f"  {tag}: {len(_days):,}営業日")
        return _P, _A, _mo

    def _score(v, mo):
        """日次 → 月次にして (月平均, σ, 月平均÷σ, t) を返す。"""
        _u = np.unique(mo)
        _m = np.array([v[mo == x].sum() for x in _u], float)
        if len(_m) < 3:
            return 0.0, 0.0, 0.0, 0.0
        _mu, _sd = float(_m.mean()), float(_m.std(ddof=1))
        if _sd <= 0:
            return _mu, 0.0, 0.0, 0.0
        return _mu, _sd, _mu / _sd, _mu / (_sd / np.sqrt(len(_m)))

    # ── ルールの総当たり ──────────────────────────────────────────
    #   軸 × 閾値(5分位の切れ目) × (低いとき, 高いとき) の全組み合わせ。
    #   選択肢は N単独 / 鏡像単独 / 両建て / 建てない の4つ。
    _CH = ("N", "M", "B", "X")          # X = その日は建てない
    _AXN = {"mkt1": "前日の相場", "mkt5": "5日の相場", "mkt20": "20日の相場",
            "absmkt1": "前日の値幅(絶対値)", "cand": "候補の偏り",
            "ntot": "候補の総数"}

    def _rules():
        for _ax in _AXN:
            for _qi in (20, 40, 60, 80):
                for _lo in _CH:
                    for _hi in _CH:
                        if _lo == _hi:
                            continue
                        yield (_ax, _qi, _lo, _hi)

    _RULES = list(_rules())

    def _apply(P, A, rule, cut=None):
        _ax, _qi, _lo, _hi = rule
        _v = A[_ax]
        _c = np.percentile(_v, _qi) if cut is None else cut
        _z = np.zeros(len(_v), float)
        for _mask, _ch in ((_v < _c, _lo), (_v >= _c, _hi)):
            if _ch != "X":
                _z[_mask] = P[_ch][_mask]
        return _z

    _res = {}
    for _tag, _df in (("TRAIN", _train), ("TEST", _test)):
        if _df is None or _df.empty:
            continue
        _P, _A, _mo = _panel(_df, _tag)
        _res[_tag] = (_P, _A, _mo)

    # ── 基準(切り替えなし) ────────────────────────────────────────
    print(f"\n  ── 基準(切り替えなし) ──")
    print(f"    {'':<10}" + "".join(f"{t:>34}" for t in _res))
    print(f"    {'':<10}" + "".join(f"{'月平均':>13}{'月平均÷σ':>11}{'t':>10}"
                                    for t in _res))
    _base = {}
    for _k, _nm3 in (("N", "N単独"), ("M", "鏡像単独"), ("B", "両建て")):
        _row2 = f"    {_nm3:<10}"
        for _t in _res:
            _P, _A, _mo = _res[_t]
            _mu, _sd, _r, _tt = _score(_P[_k], _mo)
            _base.setdefault(_t, {})[_k] = _r
            _row2 += f"{_mu:>+13,.0f}{_r:>11.2f}{_tt:>+10.2f}"
        print(_row2)

    # ── 総当たり ──────────────────────────────────────────────────
    print(f"\n  ── 使い分けルールの総当たり ({len(_RULES)}通り) ──")
    _rank = []
    for _rule in _RULES:
        _row3 = {}
        for _t in _res:
            _P, _A, _mo = _res[_t]
            _row3[_t] = _score(_apply(_P, _A, _rule), _mo)
        _rank.append((_rule, _row3))
    _rank.sort(key=lambda x: -x[1]["TRAIN"][2])       # TRAIN の 月平均÷σ 順

    print(f"    ⛔ **TRAIN で選ぶ**。TEST は答え合わせに見るだけ")
    print(f"\n    {'軸':<16}{'閾値':>6}{'低い日':>7}{'高い日':>7}"
          + "".join(f"{t + ' 月平均':>14}{'÷σ':>8}" for t in _res))
    print("    " + "-" * 76)
    for _rule, _row3 in _rank[:12]:
        _ax, _qi, _lo, _hi = _rule
        _s2 = f"    {_AXN[_ax]:<16}{_qi:>5}%{_lo:>7}{_hi:>7}"
        for _t in _res:
            _s2 += f"{_row3[_t][0]:>+14,.0f}{_row3[_t][2]:>8.2f}"
        print(_s2)

    # ── 帰無較正: **『最良を選ぶ』操作ごと**シフトする ────────────
    print(f"\n  ── 帰無較正 ({a.switch_nulls}本 / 日ブロックを保った巡回シフト) ──")
    print(f"    ⛔ 各シフトでも **同じ{len(_RULES)}通りを掃いて最良を取る**。")
    print(f"       だから『何通り試したか』は自動的に補正される")
    _P, _A, _mo = _res["TRAIN"]
    _n = len(_mo)
    _rng2 = np.random.default_rng(20260827)
    _null = []
    for _i in range(max(10, a.switch_nulls)):
        _sh = int(_rng2.integers(20, _n - 20))
        _As = {k: np.roll(v, _sh) for k, v in _A.items()}   # 指標だけずらす
        _best = max(_score(_apply(_P, _As, r), _mo)[2] for r in _RULES)
        _null.append(_best)
    _null = np.array(_null, float)
    _obs = _rank[0][1]["TRAIN"][2]
    _b0 = max(_base["TRAIN"].values())
    _p95 = float(np.percentile(_null, 95))
    print(f"    実測の最良      **{_obs:.3f}**  ({_AXN[_rank[0][0][0]]} "
          f"{_rank[0][0][1]}% / 低={_rank[0][0][2]} 高={_rank[0][0][3]})")
    print(f"    帰無の最良      中央 {float(np.median(_null)):.3f} / "
          f"95%点 **{_p95:.3f}** / 最大 {float(_null.max()):.3f}")
    print(f"    基準(切り替えなしの最良) {_b0:.3f}")
    _pv = float((_null >= _obs).mean())
    print(f"    p値 = **{_pv:.3f}**  "
          + ("← 帰無の95%点を超えています" if _obs > _p95 else
             "← 帰無の中。**偶然の範囲**です"))
    print(f"\n  {'=' * 68}")
    if _obs > _p95 and _rank[0][1]["TRAIN"][2] > _b0:
        print(f"  ★ TRAIN では帰無を超えました。**TEST の列で答え合わせ**を"
              f"してください。\n     TEST でも基準を超えていれば本物の候補です")
    else:
        print(f"  ⛔ **帰無の中です。**{len(_RULES)}通り掃いた中の最良でも、"
              f"ラベルをずらした\n     偶然の最良と区別できません。"
              f"使い分けルールは無い、が結論です")
    print(f"  ⚠ どのルールも slip=0。切り替えは注文数を減らすので、"
          f"実運用では執行コストが下がる方向に働きます(未計測)")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.sweep_regime:
    # ══════════════════════════════════════════════════════════════════
    # ★ 相場の状態で N と鏡像を使い分けられるか (TRAIN のみ)
    # ══════════════════════════════════════════════════════════════════
    #   ★ 仮説(機構つき・後知恵ではない):
    #     下げ相場のギャップアップ = 逆行した異常値 → 反落しやすい → **N が効く**
    #     上げ相場のギャップダウン = 逆行した異常値 → 反発しやすい → **鏡像が効く**
    #   つまり **N は下げ相場、鏡像は上げ相場** で強いはず。対称で機構がある。
    #
    #   ⛔ TEST は 2026-08-27 に「両建て」で消費済み。ここで良い結果が出ても
    #     **同じ窓で検証できない**。前向き(.\norder)でしか確かめられない。
    #     だから判定は出さず、**TRAIN の中で前半/後半に割って一貫するか**
    #     だけを見る。
    if a.side != "both":
        sys.exit("[error] --sweep-regime は --side both と一緒に使ってください")
    print(f"\n{'=' * 78}")
    print(f"■ 相場の状態で使い分けられるか — **TRAIN({_train_n}) だけ**")
    print(f"{'=' * 78}")
    print(f"  ★ 仮説: **N は下げ相場 / 鏡像は上げ相場** で強い")
    print(f"     (逆行したギャップほど異常値で、反対に振れやすい)")
    print(f"  ⛔ TEST は「両建て」で消費済み。ここで良くても **同じ窓では"
          f"検証できません**。\n     前向き(.\\norder)でしか確かめられないので、"
          f"合否は出しません")

    # ── 相場の状態を母集団から作る。**すべて前夜に確定** ──────────────
    #   mkt1  = その日の全銘柄の前日リターンの平均 = 「前日の相場」
    #   mkt20 = mkt1 の20日平均 = トレンド
    #   ⛔ ret1 は鏡像側で符号を反転してあるので **side==1 の行だけ**使う。
    _ms = _train[_train["side"] == 1] if "side" in _train else _train
    _mk = (_ms.groupby("date")["ret1"].mean().sort_index()
           .rename("mkt1").to_frame())
    _mk["mkt20"] = _mk["mkt1"].rolling(20).mean()
    # ★ 3本目の軸: **その日どちら側に候補が多いか**。
    #   候補は ret1(前夜に確定)だけで決まるので、これも前夜に分かる。
    #   「候補が多い side = その日の相場が向いている side」という直接の指標。
    if "side" in _train:
        _cn = (_train[(_train["side"] == 1) & (_train["ret1"] >= _RET1_MIN)]
               .groupby("date").size().rename("n_n"))
        _cmm = (_train[(_train["side"] == -1) & (_train["ret1"] >= _RET1_MIN)]
                .groupby("date").size().rename("n_m"))
        _cc2 = pd.concat([_cn, _cmm], axis=1).fillna(0.0)
        _mk["cand_ratio"] = ((_cc2["n_n"] / (_cc2["n_n"] + _cc2["n_m"]))
                             .reindex(_mk.index) * 100.0)
    _mk = _mk.dropna()
    if _mk.empty:
        sys.exit("[error] 相場の状態を作れませんでした")
    print(f"\n  相場の指標: {len(_mk):,}営業日 / "
          f"mkt20 {_mk['mkt20'].min():+.3f}% 〜 {_mk['mkt20'].max():+.3f}%")

    # ⛔ _ot / _ond は --sweep-ops の中でしか定義されていない。ここで作る。
    _rg_ot = _pool_of(_train)
    _rg_ond = max(1, _rg_ot["date"].nunique() if not _rg_ot.empty else 1)
    _sim_all, _, _ = _make_ops_sim(_train, _rg_ot, _rg_ond)

    def _by_regime(col: str, nq: int, half: int = 0):
        """相場の分位ごとに N単独 / 鏡像単独 の 円/件 を返す。"""
        _q = pd.qcut(_mk[col], nq, labels=False, duplicates="drop")
        _lab = dict(zip(_mk.index, _q))
        _r = _sim_all(50, 400.0, 0, False, half=half)
        _out = {}
        for _d, _v in _r["daily"].items():
            _k = _lab.get(_d)
            if _k is None or _k != _k:
                continue
            _a = _out.setdefault(int(_k), [0.0, 0.0, 0, 0])
            _a[0] += _v[0]; _a[1] += _v[1]
            _c = _r["dcnt"].get(_d, [0, 0])
            _a[2] += _c[0]; _a[3] += _c[1]
        return _out, _mk[col], _q

    _AX2 = [("mkt20", "トレンド(直近20日の相場)"), ("mkt1", "前日の相場")]
    if "cand_ratio" in _mk:
        _AX2.append(("cand_ratio", "その日の候補の偏り(N候補の割合%)"))
    for _col, _nm2 in _AX2:
        _o, _sv, _q = _by_regime(_col, 5)
        print(f"\n  ── {_nm2} の5分位ごと ──")
        print(f"    {'分位':<8}{'範囲(%)':>16}{'N 円/件':>12}{'鏡像 円/件':>13}"
              f"{'N 件数':>9}{'鏡像 件数':>10}{'どちらが上':>12}")
        print("    " + "-" * 80)
        _dir = []
        for _k in sorted(_o):
            _a = _o[_k]
            _rng = _sv[_q == _k]
            _pn = _a[0] / _a[2] if _a[2] else 0.0
            _pm = _a[1] / _a[3] if _a[3] else 0.0
            _w = "N" if _pn > _pm else "鏡像"
            _dir.append(_w)
            print(f"    Q{_k + 1:<7}{_rng.min():>+7.2f}〜{_rng.max():>+7.2f}"
                  f"{_pn:>+12,.0f}{_pm:>+13,.0f}{_a[2]:>9,}{_a[3]:>10,}"
                  f"{_w:>12}")
        print("    " + "-" * 80)
        # ★ **両方の向き**を見る(2026-08-27 修正)。
        #   A(事前宣言) Q1=N / Q5=鏡像   … 逆行した異常値は反転する
        #   B(逆向き)   Q1=鏡像 / Q5=N   … 相場と同方向に行き過ぎた側が戻る
        #   ⛔ B は TRAIN の表を見てから思いついたもの。TRAIN の前半/後半で
        #     安定するかを **ここで(TEST を使わずに)** 確かめる。
        #     安定して初めて TEST を1回使う価値がある。
        _okA = _dir[0] == "N" and _dir[-1] == "鏡像"
        _okB = _dir[0] == "鏡像" and _dir[-1] == "N"
        print(f"    全期間: Q1={_dir[0]} / Q5={_dir[-1]}  → "
              + ("**A(事前宣言)に一致**" if _okA else
                 "**B(逆向き)に一致**" if _okB else "どちらでもない"))
        # ★ 単調性も見る。**両端だけの比較は2択なので偶然当たる**。
        #   各側の 円/件 が分位に対して単調なら、機構がある可能性が上がる。
        _pn_all = [(_o[k][0] / _o[k][2] if _o[k][2] else 0.0) for k in sorted(_o)]
        _pm_all = [(_o[k][1] / _o[k][3] if _o[k][3] else 0.0) for k in sorted(_o)]
        def _rho(v):
            _x = np.arange(len(v), dtype=float)
            return float(np.corrcoef(_x, np.array(v, float))[0, 1]) if len(v) > 2 else 0.0
        print(f"    単調性(分位との順位相関): N {_rho(_pn_all):+.2f} / "
              f"鏡像 {_rho(_pm_all):+.2f}")
        # 前半/後半で A / B のどちらが立つか
        _hd = {"A": [], "B": []}
        for _hh in (1, 2):
            _oh, _svh, _qh = _by_regime(_col, 5, half=_hh)
            if not _oh:
                _hd["A"].append("—"); _hd["B"].append("—")
                continue
            _ks = sorted(_oh)
            _a1, _a5 = _oh[_ks[0]], _oh[_ks[-1]]
            _n1 = _a1[0] / _a1[2] if _a1[2] else 0.0
            _m1 = _a1[1] / _a1[3] if _a1[3] else 0.0
            _n5 = _a5[0] / _a5[2] if _a5[2] else 0.0
            _m5 = _a5[1] / _a5[3] if _a5[3] else 0.0
            _hd["A"].append("✓" if (_n1 > _m1 and _n5 < _m5) else "✗")
            _hd["B"].append("✓" if (_m1 > _n1 and _n5 > _m5) else "✗")
        print(f"    A(Q1=N/Q5=鏡像)  前半 {_hd['A'][0]} / 後半 {_hd['A'][1]}"
              + ("  ★両方✓" if _hd["A"] == ["✓", "✓"] else ""))
        print(f"    B(Q1=鏡像/Q5=N)  前半 {_hd['B'][0]} / 後半 {_hd['B'][1]}"
              + ("  ★両方✓" if _hd["B"] == ["✓", "✓"] else ""))

    print(f"\n  {'=' * 68}")
    print(f"  ★ 読み方: **A か B の片方が、前半・後半とも ✓** のときだけ")
    print(f"     TEST を使う価値があります。両方 ✗ / 半期で入れ替わる = 期間依存。")
    print(f"  ⚠ TEST(2020-09〜2026-08) は「両建て」で **1回使用済み**です。")
    print(f"     ここで候補が出たら **通算2回目**になります。")
    print(f"     禁止ではありませんが、2回試せば帰無でも約10%はどれか通るので、")
    print(f"     **合格が出たときは必ず『2回目』と併記**してください(§18.53)。")
    print(f"  ⚠ 5分位 × {len(_AX2)}指標 = {5 * len(_AX2)}通り見ています。"
          f"偶然どれかが仮説に一致する"
          f"確率は低くありません。\n     **前半・後半の両方で ✓ でなければ"
          f"ノイズ**として扱ってください")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.confirm_both:
    # ══════════════════════════════════════════════════════════════════
    # ★★ 両建ての検証 — **TEST を1回だけ使う**
    # ══════════════════════════════════════════════════════════════════
    #   ⛔ 合格条件は _BOTH_PASS に焼き込んである。実行時に変えられない。
    #   ⛔ 落ちたら『相場が特殊だった』と言わないこと(宣言済み)。
    if a.side != "both":
        sys.exit("[error] --confirm-both は --side both と一緒に使ってください")
    if _test.empty:
        sys.exit("[error] TEST が空です(--split を確認してください)")
    print(f"\n{'=' * 78}")
    print(f"■ 両建ての検証 — **TEST({_test_n}) で1回だけ**")
    print(f"{'=' * 78}")
    print(f"  ⛔ TEST は1候補につき1回きり。今回使うのは **『両建て』1候補**です")
    print(f"  合格条件(2026-08-27 に TEST を見ずに決定):")
    print(f"    ① 両建ての 月平均÷σ が **N単独を上回る**")
    print(f"    ② 両建ての月次 t ≥ {_BOTH_PASS['t_min']:.1f}")
    print(f"    ③ 鏡像 **単体** も 月平均>0 かつ t ≥ {_BOTH_PASS['mirror_t_min']:.1f}")
    print(f"    ④ 日次相関 ≤ {_BOTH_PASS['corr_max']:+.2f}")
    print(f"    ⑤ TEST を半分に割って **両方の半期**で ① が成立")
    print(f"  ⚠ **5つ全部**を満たしたときだけ合格。1つでも落ちたら不採用")

    _tsim, _tALL, _tHALF = _make_ops_sim(_test, _pool_of(_test),
                                         max(1, _test["date"].nunique()))
    _tond = max(1, _test["date"].nunique())

    def _mstats(daily, pick):
        """日次パネル → 月次の (平均, σ, t, 月数, プラス月)。pick: 0=S 1=L 2=合計"""
        _m: dict = {}
        for _d, _v in daily.items():
            _k = str(_d)[:7]
            _m[_k] = _m.get(_k, 0.0) + (_v[0] if pick == 0 else
                                        _v[1] if pick == 1 else _v[0] + _v[1])
        _v = np.array([_m[k] for k in sorted(_m)], float)
        if len(_v) < 2:
            return 0.0, 0.0, 0.0, len(_v), 0
        _mu, _sd = float(_v.mean()), float(_v.std(ddof=1))
        _t = _mu / (_sd / np.sqrt(len(_v))) if _sd > 0 else 0.0
        return _mu, _sd, _t, len(_v), int((_v > 0).sum())

    # ⛔⛔ ① の比較相手は **N単独(予算400万を独り占め)**。
    #   2026-08-27 の初回実装は「両建ての中のショート成分」と比べていた。
    #   あれは予算を鏡像と分け合った状態なので、**N単独より不利に出る**。
    #   条件文は最初から「N単独を上回る」と書いてあるので、これは
    #   ゴールポストの移動ではなく **実装の誤りの修正**。
    _test_s = _test[_test.get("side", 1) == 1] if "side" in _test else _test
    _ssim, _, _ = _make_ops_sim(_test_s, _pool_of(_test_s),
                                max(1, _test_s["date"].nunique()))
    _res = _tsim(50, 400.0, 0, False)
    _dd = _res["daily"]
    _ss = np.array([v[0] for v in _dd.values()], float)
    _ll = np.array([v[1] for v in _dd.values()], float)
    _corr = (float(np.corrcoef(_ss, _ll)[0, 1])
             if len(_ss) > 2 and _ss.std() > 0 and _ll.std() > 0 else 0.0)

    print(f"\n  {'':<12}{'月平均':>13}{'月次σ':>12}{'月平均÷σ':>11}"
          f"{'t':>8}{'月数':>7}{'プラス月':>9}")
    print("  " + "-" * 74)
    _row = {}
    for _lb, _pk in (("ショート", 0), ("ロング", 1), ("両建て", 2)):
        _mu, _sd, _t, _nm, _pos = _mstats(_dd, _pk)
        _row[_pk] = (_mu, _sd, _t)
        print(f"  {_lb:<12}{_mu:>+13,.0f}{_sd:>12,.0f}"
              f"{(_mu / _sd if _sd else 0):>11.2f}{_t:>+8.2f}{_nm:>7}"
              f"{_pos:>6}/{_nm}")
    print("  " + "-" * 74)
    print(f"  日次相関(ショート vs ロング) = **{_corr:+.3f}**")

    # ★ N単独(400万を独り占め)を **同じ関数**で測る(§18.48 ⑧d)
    _sres = _ssim(50, 400.0, 0, False)
    _sa = _mstats(_sres["daily"], 2)            # 片側なので合計=ショート
    _r_s = _sa[0] / _sa[1] if _sa[1] else 0.0
    _r_b = _row[2][0] / _row[2][1] if _row[2][1] else 0.0
    print(f"\n  ── ★ ① の比較相手 = **N単独(予算400万を独り占め)** ──")
    print(f"  {'N単独':<12}{_sa[0]:>+13,.0f}{_sa[1]:>12,.0f}"
          f"{_r_s:>11.2f}{_sa[2]:>+8.2f}{_sa[3]:>7}{_sa[4]:>6}/{_sa[3]}")
    print(f"  ⚠ 上の表の『ショート』は **両建ての中の成分**(予算を鏡像と"
          f"分け合った状態)。\n     採否の比較相手はこちらです")
    # ★ 月次の相関も出す。日次と符号が違うことがある(2026-08-27: 日次 -0.055 /
    #   月次 +0.266)。**月次σ を決めるのは月次の相関**なので、こちらが本質。
    _mv = {}
    for _pk in (0, 1, 2):
        _mm: dict = {}
        for _d, _v in _dd.items():
            _k = str(_d)[:7]
            _mm[_k] = _mm.get(_k, 0.0) + (_v[0] if _pk == 0 else
                                          _v[1] if _pk == 1 else _v[0] + _v[1])
        _mv[_pk] = np.array([_mm[k] for k in sorted(_mm)], float)
    _cm = (float(np.corrcoef(_mv[0], _mv[1])[0, 1])
           if len(_mv[0]) > 2 and _mv[0].std() > 0 and _mv[1].std() > 0 else 0.0)
    print(f"\n  **月次**の相関 = {_cm:+.3f}  (日次 {_corr:+.3f})")
    if _cm > _corr + 0.10:
        print(f"  ⛔ **月次のほうが正に寄っています。**σ を決めるのは月次の相関"
              f"なので、\n     日次の相関から期待するほど分散は効きません")

    # ⑤ 半期
    _h = {}
    for _hh in (1, 2):
        _rh = _tsim(50, 400.0, 0, False, half=_hh)
        _sh = _ssim(50, 400.0, 0, False, half=_hh)     # ★ N単独(同じ半期)
        _ms, _mb = _mstats(_sh["daily"], 2), _mstats(_rh["daily"], 2)
        _h[_hh] = ((_mb[0] / _mb[1] if _mb[1] else 0.0),
                   (_ms[0] / _ms[1] if _ms[1] else 0.0))
        print(f"  {'前半' if _hh == 1 else '後半'}: "
              f"両建て 月平均÷σ {_h[_hh][0]:.2f} / N単独 {_h[_hh][1]:.2f}")

    _chk = [
        ("① 月平均÷σ が N単独超", _r_b > _r_s, f"{_r_b:.2f} vs {_r_s:.2f}"),
        ("② 両建ての t ≥ 2.0", _row[2][2] >= _BOTH_PASS["t_min"],
         f"t={_row[2][2]:+.2f}"),
        ("③ 鏡像単体 >0 かつ t ≥ 1.0",
         _row[1][0] > 0 and _row[1][2] >= _BOTH_PASS["mirror_t_min"],
         f"月平均{_row[1][0]:+,.0f} t={_row[1][2]:+.2f}"),
        ("④ 相関 ≤ +0.30", _corr <= _BOTH_PASS["corr_max"], f"{_corr:+.3f}"),
        ("⑤ 前半・後半とも ①", _h[1][0] > _h[1][1] and _h[2][0] > _h[2][1],
         f"前半 {_h[1][0]:.2f}>{_h[1][1]:.2f} / 後半 {_h[2][0]:.2f}>{_h[2][1]:.2f}"),
    ]
    print(f"\n  {'=' * 68}")
    for _nm2, _ok, _det in _chk:
        print(f"  {'✅' if _ok else '❌'} {_nm2:<26}{_det}")
    _pass = all(x[1] for x in _chk)
    print(f"  {'=' * 68}")
    print(f"  ★★ 判定: **{'合格' if _pass else '不合格'}**")
    if _pass:
        print(f"     両建ては TRAIN/TEST の両方で N単独を上回りました。")
        print(f"     ⚠ ただし **執行コストは1円も引いていません**"
              f"(slip=0 / 呼値なし)。実測は .\\norder を貯めてから")
    else:
        print(f"     ❌ が1つでもあれば不採用。**基準を緩めて再判定しないこと**")
    print(f"  ⛔ TEST はこれで消費しました。同じ窓で別の案を試すと"
          f"多重検定になります")
    sys.exit(0)

if a.sweep_ops:
    # ══ 運用パラメータ (TRAIN のみ) ═══════════════════════════════════
    #   watch上限 / 予算 / 1日の建玉数上限 / 同一銘柄の連日。
    #   ⛔ TEST は使わない。
    _ot = _pool_of(_train)
    if _ot.empty:
        sys.exit("[error] TRAIN が空です")
    _ond = max(1, _ot["date"].nunique())
    print(f"\n{'=' * 78}\n■ 運用パラメータ — **TRAIN({_train_n}) だけ**\n{'=' * 78}")
    print(f"  ⛔ TEST は1回も使いません")
    print(f"  対象 {len(_ot):,}銘柄日 / {_ond:,}営業日 / "
          + (f"★両建て |ret1| ≥ 1.753% × |gap| ≥ {a.min_gap_bp:.0f}bp "
             f"(上げ→売り / 下げ→買い)" if a.side == "both" else
             f"ret1 ≥ 1.753% × gap ≥ {a.min_gap_bp:.0f}bp" if _SIDE > 0 else
             f"★鏡像 ret1 ≤ -1.753% × gap ≤ -{a.min_gap_bp:.0f}bp を**買う**")
          + (f" 〜 {a.max_gap_bp:.0f}bp" if a.max_gap_bp > 0 else ""))

    _ops_sim, _ALL_D, _HALF = _make_ops_sim(_train, _ot, _ond)

    if a.max_gap_bp > 0:
        _n_hi = int((_train["gap_bp"] > a.max_gap_bp).sum())
        print(f"  ⚠ ギャップ上限 {a.max_gap_bp:.0f}bp で **{_n_hi:,}銘柄日を除外**"
              f"(除外前 {len(_train):,})。0件なら上限が効いていない")
    _b0 = _ops_sim(50, 400.0, 0, False)
    print(f"\n  ★ 現行(watch50 / 予算400万 / 上限なし) = "
          f"{_b0['pnl']:+,.0f}円 / {_b0['n']:,}件 / {_b0['per']:+,.0f}円/件 / "
          f"月換算 {_b0['pnl'] / _ond * 20:+,.0f}円")

    # ★★ ギャップ閾値 — **一度も ops sim を通していなかった**(2026-08-27)
    #   --sweep-grid は bp/件 だけを見て「現行のまま」と結論したが、
    #   N は **件数不足**(稼働率40%)なので、bp/件 が下がっても件数が増えれば
    #   月の総額は増えうる。watch50 と予算を通して初めて判定できる。
    #   ⚠ _ops_sim は a.min_gap_bp を **呼び出し時に読む**ので、
    #     一時的に差し替えれば1回の実行で掃ける(元に戻すこと)。
    def _mser(res):
        """月別の合計を日付順の numpy 配列で返す(印字しない)。

        ⛔ 総額だけで閾値を選ぶと必ず罠にかかる(§18.28 / §18.38)。
           件数を増やせば損益もσも増えるので、**月平均÷σ** を併記しないと
           『レバレッジ』と『質の改善』を区別できない。
        """
        _dd = res["daily"]
        _m: dict = {}
        for _d, _v in _dd.items():
            _m[str(_d)[:7]] = _m.get(str(_d)[:7], 0.0) + _v[0] + _v[1]
        _ks = sorted(_m)
        return _ks, np.array([_m[k] for k in _ks], float)

    if a.ops_gap_list.strip():
        _gsv = a.min_gap_bp
        _gl = sorted({float(x) for x in a.ops_gap_list.split(",") if x.strip()}
                     | {_gsv, a.ops_gap_ref})
        print(f"\n  ── ★★ ギャップ閾値(bp) ── **watch50 と予算を通した判定**")
        print(f"     ⚠ 母集団は --min-gap-bp {_gsv:.0f} で切ってあるので、"
              f"それ**未満**の升は測れません")
        print(f"    {'gap':<8}{'件/日':>7}{'円/件':>9}"
              f"{'月平均':>11}{'月次σ':>11}{'÷σ':>7}"
              f"{'差/月':>11}{'対応t':>7}{'執行後 差/月':>13}{'同 t':>7}")
        _cur_s = None
        _cur_r = None
        _mean_ep: dict = {}       # gap -> その設定で実際に建てた建値の平均
        _rows_g = []
        try:
            for _gv in _gl:
                if _gv < _gsv:
                    _rows_g.append((_gv, None))
                    continue
                a.min_gap_bp = _gv
                _r = _ops_sim(50, 400.0, 0, False)
                _rows_g.append((_gv, (_r, _mser(_r))))
                # ⚠ 建値の平均は『その閾値を満たした母集団』で近似する。
                #   watch50/予算を通した実際の建玉そのものではないが、
                #   閾値間の**差**を出すのが目的なので十分。
                _pp = _ot[_ot["gap_bp"] >= _gv]
                _mean_ep[_gv] = (float(_pp["entry_p"].mean()) if len(_pp)
                                 else 0.0)
                if abs(_gv - a.ops_gap_ref) < 1e-6:
                    _cur_s, _cur_r = _mser(_r), _r
        finally:
            a.min_gap_bp = _gsv
        # ★ 現行(= --min-gap-bp で切った母集団の下端)を基準にした **対応検定**。
        #   閾値は入れ子(gap150 ⊂ … ⊂ gap25)なので月次系列の相関が高く、
        #   単独の t より検出力が高い。
        for _gv, _pk in _rows_g:
            if _pk is None:
                print(f"    {_gv:<8.0f}{'— 母集団の外':>13}")
                continue
            _r, (_ks, _ms) = _pk
            _mu = float(_ms.mean()) if len(_ms) else 0.0
            _sd = float(_ms.std(ddof=1)) if len(_ms) > 1 else 0.0
            _dt, _dtt = "—", "—"
            # ⚠ 閾値がきついと取引ゼロの月が出て月数が揃わない。
            #   **月キーの和集合で 0 埋めして揃える**(揃わないと対応検定が消える)
            if _cur_s is not None:
                _um = sorted(set(_ks) | set(_cur_s[0]))
                _d1 = dict(zip(_ks, _ms))
                _d0 = dict(zip(_cur_s[0], _cur_s[1]))
                _df = np.array([_d1.get(k, 0.0) - _d0.get(k, 0.0) for k in _um],
                               float)
            else:
                _df = np.array([], float)
            if len(_df) > 1:
                _ds = float(_df.std(ddof=1))
                _dm = float(_df.mean())
                _dt = f"{_dm:+,.0f}"
                _dtt = (f"{_dm / (_ds / np.sqrt(len(_df))):+.2f}" if _ds > 0
                        else "—")
            # ★★ 執行コストを引いた差。**件数に比例するので、件数を増やす方向には
            #   不利に働く**。これを引かずに閾値を選ぶと、増やす方向が必ず有利に
            #   見える(2026-08-27 に手計算で気づいたので列にした)。
            #   1件あたり = 建値 × 100株 × EXEC_BP。建値はその設定の実測平均を使う。
            _dt2, _dtt2 = "—", "—"
            if _cur_r is not None and len(_df) > 1:
                _e1 = _mean_ep.get(_gv, 0.0) * a.qty * EXEC_BP / 10_000.0
                _e0 = _mean_ep.get(a.ops_gap_ref, 0.0) * a.qty * EXEC_BP / 10_000.0
                # 月あたりの執行コスト差(件数×単価。20営業日/月に換算)
                _dc = (_r['n'] / _ond * _e1 - _cur_r['n'] / _ond * _e0) * 20.0
                _dm2 = float(_df.mean()) - _dc
                _ds2 = float(_df.std(ddof=1))
                _dt2 = f"{_dm2:+,.0f}"
                _dtt2 = (f"{_dm2 / (_ds2 / np.sqrt(len(_df))):+.2f}" if _ds2 > 0
                         else "—")
            _mk = " ★本番" if abs(_gv - a.ops_gap_ref) < 1e-6 else ""
            print(f"    {_gv:<8.0f}{_r['n'] / _ond:>7.1f}"
                  f"{_r['per']:>+9,.0f}{_mu:>+11,.0f}{_sd:>11,.0f}"
                  f"{(_mu / _sd if _sd else 0):>7.2f}{_dt:>11}{_dtt:>7}"
                  f"{_dt2:>13}{_dtt2:>7}{_mk}")
        print(f"     ★★ 見るのは **月平均÷σ** と **対応t**。総額(月平均)だけで"
              f"選ぶと、件数を増やしただけの")
        print(f"        **レバレッジ**を『改善』と読み違えます(§18.28 / §18.38)。")
        print(f"     ⚠ 対応t は **★本番({a.ops_gap_ref:.0f}bp)** との差。"
              f"閾値は入れ子なので月次の相関が高く、単独の t より検出力があります")
        if _cur_s is None:
            print(f"     ⛔ 本番{a.ops_gap_ref:.0f}bp が母集団の外なので"
                  f"対応検定を出せません(--min-gap-bp を下げてください)")
        print(f"     ★★ **『執行後 差/月』と『同 t』で判定すること。**")
        print(f"        執行コスト({EXEC_BP:.1f}bp/件)は **件数に比例**するので、"
              f"件数を増やす方向には必ず不利に働きます。")
        print(f"        引く前の『差/月』だけを見ると、増やす方向が"
              f"**常に良く見えます**")
        print(f"     ⛔ 良い升があっても採用しないこと。TEST での検証が要ります")

    print(f"\n  ── watch上限 ──")
    print(f"    {'watch':<10}{'損益':>14}{'件数':>9}{'円/件':>10}"
          f"{'取逃し':>9}{'月換算':>12}")
    for _w in (20, 30, 50, 100, 200, 0):
        _r = _ops_sim(_w, 400.0, 0, False)
        _lb = "無制限" if _w == 0 else str(_w)
        _mk = " ★" if _w == 50 else ""
        print(f"    {_lb:<10}{_r['pnl']:>+14,.0f}{_r['n']:>9,}"
              f"{_r['per']:>+10,.0f}{_r['miss']:>9,}"
              f"{_r['pnl'] / _ond * 20:>+12,.0f}{_mk}")
    print(f"\n  ── 予算(watch50 固定) ──")
    print(f"    {'予算':<10}{'損益':>14}{'件数':>9}{'円/件':>10}"
          f"{'投入/日':>10}{'月換算':>12}")
    for _bm in (200.0, 300.0, 400.0, 600.0, 800.0, 1200.0):
        _r = _ops_sim(50, _bm, 0, False)
        _mk = " ★" if _bm == 400 else ""
        print(f"    {_bm:<10,.0f}{_r['pnl']:>+14,.0f}{_r['n']:>9,}"
              f"{_r['per']:>+10,.0f}{_r['used'] / 1e4:>9,.0f}万"
              f"{_r['pnl'] / _ond * 20:>+12,.0f}{_mk}")
    print(f"\n  ── 1日に建てる件数の上限(watch50 / 予算400万) ──")
    print(f"    {'上限':<10}{'損益':>14}{'件数':>9}{'円/件':>10}{'月換算':>12}")
    for _mn in (3, 5, 8, 13, 20, 0):
        _r = _ops_sim(50, 400.0, _mn, False)
        _lb = "なし" if _mn == 0 else str(_mn)
        _mk = " ★" if _mn == 0 else ""
        print(f"    {_lb:<10}{_r['pnl']:>+14,.0f}{_r['n']:>9,}"
              f"{_r['per']:>+10,.0f}{_r['pnl'] / _ond * 20:>+12,.0f}{_mk}")
    _r1 = _ops_sim(50, 400.0, 0, True)
    print(f"\n  ── 同じ銘柄を1日1回だけ ──")
    print(f"    現行(制限なし) {_b0['pnl']:>+14,.0f} / {_b0['n']:,}件")
    print(f"    1日1回だけ     {_r1['pnl']:>+14,.0f} / {_r1['n']:,}件 "
          f"(差 {_r1['pnl'] - _b0['pnl']:+,.0f})")
    # ══ 月別 (§18.24: 判定は総額ではなく月次σ/t で行う) ══════════════
    def _monthly(res, tag: str):
        _dd, _kk = res["daily"], res["dcnt"]
        _m: dict = {}
        for _d, _v in _dd.items():
            _k = str(_d)[:7]
            _a = _m.setdefault(_k, [0.0, 0.0, 0, 0])
            _a[0] += _v[0]; _a[1] += _v[1]
            _c = _kk.get(_d, [0, 0])
            _a[2] += _c[0]; _a[3] += _c[1]
        if not _m:
            return
        _ks = sorted(_m)
        _tot = np.array([_m[k][0] + _m[k][1] for k in _ks], float)
        _both_side = a.side == "both"
        print(f"\n  ── 月別 ({tag}) ──")
        if _both_side:
            print(f"    {'月':<10}{'ショート':>13}{'ロング':>13}{'合計':>13}"
                  f"{'件数':>9}")
        else:
            print(f"    {'月':<10}{'損益':>13}{'件数':>9}")
        for k in _ks:
            _v = _m[k]
            if _both_side:
                print(f"    {k:<10}{_v[0]:>+13,.0f}{_v[1]:>+13,.0f}"
                      f"{_v[0] + _v[1]:>+13,.0f}{_v[2] + _v[3]:>9,}")
            else:
                print(f"    {k:<10}{_v[0] + _v[1]:>+13,.0f}{_v[2] + _v[3]:>9,}")
        _mu, _sd = float(_tot.mean()), float(_tot.std(ddof=1))
        _t = _mu / (_sd / np.sqrt(len(_tot))) if _sd > 0 else 0.0
        _ci = 1.96 * _sd / np.sqrt(len(_tot))
        _pos = int((_tot > 0).sum())
        print(f"    {'-' * 56}")
        print(f"    {len(_ks)}ヶ月 / 月平均 {_mu:+,.0f}円 / 月次σ {_sd:,.0f}円 / "
              f"**月平均÷σ {(_mu / _sd if _sd else 0):.2f}**")
        print(f"    t = **{_t:+.2f}** / 95%CI {_mu - _ci:+,.0f} 〜 {_mu + _ci:+,.0f}円 "
              f"/ プラス月 **{_pos}/{len(_ks)}**")
        if abs(_t) < 2.0:
            _need = int(np.ceil((2.0 * _sd / max(abs(_mu), 1e-9)) ** 2))
            print(f"    ⚠ **CI がゼロをまたぎます**(t<2)。"
                  f"t=2 に届くには約 {_need}ヶ月 必要です")
        print(f"    ⚠ 端の月は日数が欠けるので上下に振れます")

    _monthly(_b0, "現行 watch50 / 予算400万 / 上限なし")

    if a.side == "both":
        # ══ 両建ての本題 = **日次のσが下がるか** ═══════════════════
        #   総額が増えるのは当たり前(遊んでいた資金を使うだけ)。
        #   価値があるのは「上げ日にショートが負けるとき、ロングが助けるか」。
        _dd = _b0["daily"]
        _ss = np.array([v[0] for v in _dd.values()], float)
        _ll = np.array([v[1] for v in _dd.values()], float)
        _cc = _ss + _ll
        _bs, _bl = _b0["byside"].get(1, [0, 0]), _b0["byside"].get(-1, [0, 0])
        _dc = _b0["dcap"]
        _cs = sum(v[0] for v in _dc.values()) / _ond
        _cl = sum(v[1] for v in _dc.values()) / _ond
        _pu = _b0["pushed"]
        print(f"\n  ── ★ 両建て(予算400万・watch50 を共有) ──")
        print(f"  ⛔ **200万ずつ分けてはいません。** 400万を1つの財布として、"
              f"両側まぜて\n     |ギャップ| の大きい順に埋めます"
              f"(片側だけの日はその側が400万まで使えます)")
        print(f"    {'':<12}{'損益':>14}{'件数':>9}{'円/件':>10}"
              f"{'月換算':>12}{'日次σ':>12}{'投入/日':>11}{'押出し':>10}")
        for _lb, _p, _n_, _sr, _cv, _pv in (
                ("ショート", _bs[0], _bs[1], _ss, _cs, _pu.get(1, 0)),
                ("ロング", _bl[0], _bl[1], _ll, _cl, _pu.get(-1, 0)),
                ("合計", _b0["pnl"], _b0["n"], _cc, _cs + _cl,
                 _pu.get(1, 0) + _pu.get(-1, 0))):
            print(f"    {_lb:<12}{_p:>+14,.0f}{_n_:>9,}"
                  f"{(_p / _n_ if _n_ else 0):>+10,.0f}"
                  f"{_p / _ond * 20:>+12,.0f}{_sr.std(ddof=1):>12,.0f}"
                  f"{_cv / 1e4:>10,.0f}万{_pv:>10,}")
        print(f"    ⚠ 投入/日 は **平均**。予算が埋まる日と、候補が1件も"
              f"出ない日の平均なので、\n       稼働率が低くても"
              f"『混んだ日には押し出されている』ことは普通に起きます")
        _r = (float(np.corrcoef(_ss, _ll)[0, 1])
              if len(_ss) > 2 and _ss.std() > 0 and _ll.std() > 0 else float("nan"))
        _sep = float(_ss.std(ddof=1) + _ll.std(ddof=1))
        _red = (1 - _cc.std(ddof=1) / _sep) * 100 if _sep > 0 else 0.0
        print(f"\n    日次損益の相関 (ショート vs ロング) = **{_r:+.3f}**")
        print(f"    σ: 単純合算 {_sep:,.0f} → 実際 {_cc.std(ddof=1):,.0f} "
              f"= **{_red:+.0f}%**")
        # ★ σ削減の内訳。**独立 と 逆相関 は別物**(2026-08-27)。
        #   ρ=0 でも sqrt 合成で σ は下がる。そこを分けないと
        #   「鏡像がショートの負けを打ち消している」と誤読する。
        _s0, _l0 = float(_ss.std(ddof=1)), float(_ll.std(ddof=1))
        _indep = float(np.sqrt(_s0 ** 2 + _l0 ** 2))     # ρ=0 のときのσ
        _act = float(_cc.std(ddof=1))
        _r_indep = (1 - _indep / _sep) * 100 if _sep > 0 else 0.0
        _r_corr = (1 - _act / _indep) * 100 if _indep > 0 else 0.0
        print(f"    内訳: **独立(2本に分けたこと) {_r_indep:+.0f}%** / "
              f"**逆相関 {_r_corr:+.0f}%**")
        print(f"      → σ が下がる理由のほとんどは『独立な2本目』であって、"
              f"\n        『ショートの負けを鏡像が打ち消す』ではありません")
        _neg = _ss < 0
        if _neg.any():
            _avg_loss = float(_ss[_neg].mean())
            _avg_hedge = float(_ll[_neg].mean())
            _cov = (-_avg_hedge / _avg_loss * 100) if _avg_loss < 0 else 0.0
            print(f"\n    ── ショートが負けた日 {int(_neg.sum()):,}日 ──")
            print(f"      ショートの平均損失   {_avg_loss:>+12,.0f}円")
            print(f"      同じ日のロング       {_avg_hedge:>+12,.0f}円 "
                  f"(合計 {_ll[_neg].sum():+,.0f}円)")
            print(f"      **穴埋め率 {_cov:.0f}%**"
                  + ("  ← 防げていません(ヘッジではなく分散)" if _cov < 50 else ""))
            _both_neg = int(((_ss < 0) & (_ll < 0)).sum())
            print(f"      両方とも負けた日     {_both_neg:,}日 "
                  f"({_both_neg / max(int(_neg.sum()), 1) * 100:.0f}%)")
            _w5 = np.argsort(_cc)[:5]
            print(f"      合計の最悪5日: "
                  + " / ".join(f"{_cc[i]:+,.0f}(S{_ss[i]:+,.0f} L{_ll[i]:+,.0f})"
                               for i in _w5))
        _mo = _cc.sum() / _ond * 20
        _sh = _mo / (_cc.std(ddof=1) * np.sqrt(20)) if _cc.std(ddof=1) > 0 else 0.0
        print(f"\n    月換算 {_mo:+,.0f}円 / 月次σ ≒ "
              f"{_cc.std(ddof=1) * np.sqrt(20):,.0f}円 / 月平均÷σ **{_sh:.2f}**")
        print(f"    ⚠ 相関が **0 に近い/マイナス**なら両建ての価値がある。")
        print(f"       **プラスなら『同じものを2倍やっている』だけ**で、")
        print(f"       σ も比例して増えるので予算を増やすのと変わらない。")
        print(f"    ⚠ 資金は競合する。片側だけの投入/日を足した値より、")
        print(f"       上の『合計』の件数が少なければ **予算で押し出されている**。")
    # ══ 発注順 と 予算配分 (TRAIN のみ / §18.24 の帯で判定) ═══════════
    #   ⛔ 2条件を1回ずつ比べて差を語らない。**ランダムの帯を先に作る**。
    #      帯より小さい差は「改善した」ではなく「測れていない」。
    _SEEDS = [int(x) for x in str(a.rank_seeds).split(",") if x.strip()]
    print(f"\n  ── ★ 発注順 (ランダム{len(_SEEDS)}シードの帯で判定) ──")
    print(f"  ⛔ §18.21/§18.24/§18.31 では J/lss の発注順が **3回とも帰無の中**"
          f"でした。\n     N でも同じ可能性が高いので、帯の外に出たときだけ意味があります")
    _band = [_ops_sim(50, 400.0, 0, False, order="rand", seed=_s)["pnl"]
             for _s in _SEEDS]
    _bm, _bs = float(np.mean(_band)), float(np.std(_band, ddof=1))
    _tc = 2.20 if len(_SEEDS) >= 12 else 2.45          # t(n-1) 95%
    print(f"    ランダム{len(_SEEDS)}本: 平均 {_bm:+,.0f} / σ {_bs:,.0f} / "
          f"範囲 {min(_band):+,.0f} 〜 {max(_band):+,.0f}")
    # ⛔ 帯の幅がゼロ/極小 = **予算が効いていない**(押し出しが起きない)ので、
    #   並べ替えても結果が変わらない。z は発散するだけで意味がない。
    _degen = _bs <= abs(_bm) * 1e-6
    if _degen:
        print(f"    ⛔ **帯の幅がゼロです。予算が効いていないので発注順は"
              f"比較できません。**\n"
              f"       (全候補が建てられている = 並べ替えても同じ。"
              f"押し出しが起きる母集団/予算でだけ意味があります)")

    def _zof(_v):
        return float("nan") if _degen else (_v - _bm) / _bs

    def _jd_of(_z):
        if _degen:
            return "比較不能"
        return "帯の外" if abs(_z) >= _tc else "帯の中"
    print(f"\n    {'発注順':<16}{'損益':>14}{'件数':>9}{'円/件':>10}"
          f"{'z':>8}{'判定':>10}   前半 / 後半")
    _ORD = [("gap", "|ギャップ|降順 ★現行"), ("liq", "売買代金 降順"),
            ("price", "建値 安い順"), ("gapasc", "|ギャップ|昇順")]
    for _o, _lb in _ORD:
        _r = _ops_sim(50, 400.0, 0, False, order=_o)
        _z = _zof(_r["pnl"])
        _h1 = _ops_sim(50, 400.0, 0, False, order=_o, half=1)["pnl"]
        _h2 = _ops_sim(50, 400.0, 0, False, order=_o, half=2)["pnl"]
        _b1 = np.mean([_ops_sim(50, 400.0, 0, False, order="rand",
                                seed=_s, half=1)["pnl"] for _s in _SEEDS[:4]])
        _b2 = np.mean([_ops_sim(50, 400.0, 0, False, order="rand",
                                seed=_s, half=2)["pnl"] for _s in _SEEDS[:4]])
        _sg = "✓" if (_h1 - _b1) * (_h2 - _b2) > 0 else "—"
        _zs = "  —  " if _degen else f"{_z:>+8.2f}"
        print(f"    {_lb:<16}{_r['pnl']:>+14,.0f}{_r['n']:>9,}"
              f"{_r['per']:>+10,.0f}{_zs:>8}{_jd_of(_z):>10}   "
              f"{_h1 - _b1:>+10,.0f} / {_h2 - _b2:>+10,.0f} {_sg}")
    print(f"    ⚠ 前半/後半は **同じ半期のランダム4本の平均との差**。"
          f"符号が揃って ✓ でなければ期間依存")

    if a.side == "both":
        print(f"\n  ── ★ 予算配分 (両建てのときだけ意味がある) ──")
        print(f"    {'配分':<26}{'損益':>14}{'件数':>9}{'円/件':>10}"
              f"{'投入/日':>10}{'z':>8}{'判定':>10}")
        _ALC = [("merge", "1つの財布 ★現行"),
                ("split50", f"側ごとに半額({400 / 2:,.0f}万ずつ)"),
                ("prop", "その日の合格数で按分"),
                ("alt", "交互に取る(S,L,S,L…)")]
        for _al, _lb in _ALC:
            _r = _ops_sim(50, 400.0, 0, False, alloc=_al)
            _z = _zof(_r["pnl"])
            _zs = "  —  " if _degen else f"{_z:>+8.2f}"
            print(f"    {_lb:<26}{_r['pnl']:>+14,.0f}{_r['n']:>9,}"
                  f"{_r['per']:>+10,.0f}{_r['used'] / 1e4:>9,.0f}万"
                  f"{_zs:>8}{_jd_of(_z):>10}")
        print(f"    ⚠ **投入/日を必ず見ること。** 側ごとに財布を分けると、"
              f"片側に候補が無い日に\n       その半分が丸ごと遊びます"
              f"(1つの財布なら もう一方が使えます)")
        print(f"    ⚠ z は **発注順のランダム帯**を基準にしています"
              f"(配分の帯は別に作る必要がありますが、\n"
              f"       まず『帯と同じオーダーか』を見るだけで足ります)")

    print(f"\n  {'=' * 68}")
    print(f"  ★ 読み方: **★(現行) より明確に良い行があるか**だけを見る。")
    print(f"     ⚠ 予算は **レバレッジ**。増やせば損益もσも比例して増えるので、")
    print(f"        『最適値』ではなく **リスク許容度の宣言**(§18.38 #3b)。")
    print(f"     ⚠ watch は kabu の登録上限50件が実装上の天井(§18.44)。")
    print(f"        100/200 が良くても **kabu では実現できません**。")
    print(f"  ⛔ 良い行があっても採用しないこと。TEST での検証が要ります。")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.sweep_barrier:
    # ══ 損切り/利確のスイープ (TRAIN のみ) ═══════════════════════════
    #   ⛔ TEST は 1 回も使わない。「効果がない」の確認は TRAIN で完結する。
    #   ⚠ 日足なので **高値と安値のどちらが先か分からない**(§18.6)。
    #      両方触れた日は「損切り優先(保守)」と「利確優先(楽観)」の両方を出す。
    #      真値はその間にある。
    #   ⚠ 約定は **ラインちょうど**(楽観)。実際はギャップで飛ぶ(§18.9.1)。
    #      つまりこの測定は **バリアに有利**。それでも現行に勝てないなら結論は強い。
    _tb = _pool_of(_train)
    _tb = _tb[(_tb["atr"] > 0) & _tb["d1_high"].notna() & _tb["d1_low"].notna()]
    if _tb.empty:
        sys.exit("[error] バリアを測れる行がありません")
    _sms = [float(x) for x in a.sm_list.split(",") if x.strip()]
    _tms = [float(x) for x in a.tm_list.split(",") if x.strip()]
    print(f"\n{'=' * 78}\n■ 損切り/利確のスイープ — **TRAIN({_train_n}) だけ**\n{'=' * 78}")
    print(f"  ⛔ TEST は1回も使いません。『効果がない』の確認は TRAIN で完結します")
    print(f"  対象 {len(_tb):,}銘柄日 / {_tb['date'].nunique():,}営業日 / "
          + (f"ギャップ ≥{a.min_gap_bp:.0f}bp" if _SIDE > 0 else
           f"★鏡像 ギャップ ≤-{a.min_gap_bp:.0f}bp を**買う**")
          + (f" 〜 {a.max_gap_bp:.0f}bp" if a.max_gap_bp > 0 else ""))
    print(f"  ⚠ 日足なので高値/安値の**順序が分からない**。両方触れた日は"
          f"『損切り優先(保守)』『利確優先(楽観)』の両方を出します")
    print(f"  ⚠ 約定は**ラインちょうど**(楽観)。実際はギャップで飛ぶ(§18.9.1)ので、"
          f"この測定は**バリアに有利**です")

    _ep = _tb["entry_p"].to_numpy(dtype=float)
    _at = _tb["atr"].to_numpy(dtype=float)
    _hi = _tb["d1_high"].to_numpy(dtype=float)
    _lo = _tb["d1_low"].to_numpy(dtype=float)
    _cl = _tb["d1_close"].to_numpy(dtype=float)
    _dt = _tb["date"].to_numpy()

    def _bar_pnl(sm: float, tm: float, stop_first: bool = True):
        """ショート: 損切り=建値+sm*ATR(上) / 利確=建値-tm*ATR(下)。

        ⚠ 両方触れた日は **stop_first=True(損切り優先=悲観)** が既定。
           sameday5m_firsttouch と同じ扱い(§18.51 C)。
        ⚠ 損切りの約定は `stop * (1 + --stop-slip-pct)`。既定0=ラインちょうど。
        """
        import numpy as _np
        _stop = _ep + _at * sm if sm > 0 else None
        _targ = _ep - _at * tm if tm > 0 else None
        # 損切りは**不利側**に滑る(ショートなので上に払う)
        _sfill = (_stop * (1.0 + a.stop_slip_pct)) if _stop is not None else None
        _hit_s = (_hi >= _stop) if _stop is not None else _np.zeros(len(_ep), bool)
        _hit_t = (_lo <= _targ) if _targ is not None else _np.zeros(len(_ep), bool)
        _exit = _cl.copy()
        _both = _hit_s & _hit_t
        _only_s = _hit_s & ~_hit_t
        _only_t = _hit_t & ~_hit_s
        if _sfill is not None:
            _exit = _np.where(_only_s, _sfill, _exit)
        if _targ is not None:
            _exit = _np.where(_only_t, _targ, _exit)
        if _both.any():
            _exit = _np.where(_both, _sfill if stop_first else _targ, _exit)
        return ((_ep - _exit) * a.qty, int(_hit_s.sum()), int(_hit_t.sum()),
                int(_both.sum()))

    _orders = ([(True, "損切り優先(悲観)"), (False, "利確優先(楽観・参考)")]
               if a.both_orders else [(True, "損切り優先(悲観)")])
    _base_v = float(_bar_pnl(0.0, 0.0)[0].mean())
    print(f"\n  ★ 現行(バリアなし) = **{_base_v:+,.0f}円/件**"
          f"{f' / 損切りスリッページ {a.stop_slip_pct * 100:.2f}%' if a.stop_slip_pct else ''}")
    for _order, _olbl in _orders:
        print(f"\n  ── {_olbl} ── **現行との差**(円/件)。+ なら現行より良い")
        print(f"    {'損切ATR':<9}" + "".join(f"{'tm' + str(t):>12}" for t in _tms))
        print("    " + "-" * (9 + 12 * len(_tms)))
        for _sm in _sms:
            _row = f"    {('なし' if _sm == 0 else str(_sm)):<9}"
            for _tm in _tms:
                _m = float(_bar_pnl(_sm, _tm, _order)[0].mean())
                _d = _m - _base_v
                if _sm == 0 and _tm == 0:
                    _row += f"{'★現行':>12}"
                else:
                    _row += f"{_d:>+11,.0f}{'✅' if _d > 0 else ' '}"
            print(_row)
    # 発動件数
    for _sm in [s for s in _sms if s > 0][:3]:
        _v, _ns, _nt, _nb = _bar_pnl(_sm, 1.0)
        print(f"\n  sm{_sm} / tm1.0 の発動: 損切り {_ns:,}件 "
              f"({_ns / len(_ep) * 100:.0f}%) / 利確 {_nt:,}件 "
              f"({_nt / len(_ep) * 100:.0f}%) / **両方触れた {_nb:,}件 "
              f"({_nb / len(_ep) * 100:.0f}%)** ← ここは悲観側(損切り)で数えている")
    print(f"\n  {'=' * 68}")
    print(f"  ★ 読み方: **✅ が無ければ「意味がない」で確定**。")
    if a.stop_slip_pct <= 0:
        print(f"     ⚠ いまは損切りが **ラインちょうど**で約定する前提(楽観)。")
        print(f"        実際はギャップで飛ぶ(§18.9.1)ので、"
              f"--stop-slip-pct 0.005 で悲観側も見ること。")
    print(f"     この測定は **バリアに有利**なので、ここで勝てないなら")
    print(f"     実運用では確実に負けます。")
    print(f"  ⛔ ✅ があっても、そこで採用しないこと。TEST での検証が要ります")
    print(f"     (そして TEST は既に2回使っています)。")
    print(f"  {'=' * 68}")
    sys.exit(0)

if a.explore:
    _tr = _pool_of(_train)
    print(f"\n{'=' * 78}\n■ 軸別探索 — **TRAIN({_train_n}) だけ**\n{'=' * 78}")
    print(f"  ⛔ TEST({_test_n}) は **集計していません**。誤って見ないためのガードです。")
    print(f"  対象 {len(_tr):,}銘柄日 / {_tr['date'].nunique():,}営業日 / "
          f"ギャップ ≥{a.min_gap_bp:.0f}bp / {a.nq}分位")
    print(f"  ⚠ 帰無は **実測と同じ『最良分位を選ぶ』操作**を掛けて較正します。"
          f"最大を選ぶだけで z は平均+1ずれる(§18.34b)。0 と比べてはいけません。")
    _want = [x.strip() for x in a.axes.split(",") if x.strip()] or list(AXES)
    _hits, _tried = [], 0
    print(f"\n  {'軸':<22}{'最良':>6}{'件数':>9}{'bp/件':>9}{'日t':>8}"
          f"{'帰無中央':>9}{'帰無95%':>9}  判定")
    print("  " + "-" * 84)
    for _ax in _want:
        if _ax not in AXES:
            print(f"  ⚠ 未知の軸: {_ax}(--list-axes で確認)")
            continue
        _res = _axis_scan(_tr, _ax, AXES[_ax], a.nq, a.axis_seeds)
        if not _res:
            print(f"  {AXES[_ax]:<22}{'—':>6}{'(データ不足)':>9}")
            continue
        _b = _res["best"]
        if _res.get("uncalib"):
            # 日の中で値が一定の軸(曜日) = シャッフルが効かず較正不能
            print(f"  {_res['label']:<22}{_b[0]:>6}{_b[1]:>9,}{_b[2]:>+9.1f}"
                  f"{_b[3]:>+8.2f}{'—':>9}{'—':>9}  ⛔ 較正不能(日の中で一定)")
            continue
        _tried += 1
        _mk = "✅ 候補" if _res["hit"] else "—"
        if _res["hit"]:
            _hits.append(_res)
        print(f"  {_res['label']:<22}{_b[0]:>6}{_b[1]:>9,}{_b[2]:>+9.1f}"
              f"{_b[3]:>+8.2f}{_res['null_med']:>+9.1f}{_res['null_p95']:>+9.1f}  {_mk}")
    print(f"\n  掃いた軸 {_tried} / **候補 {len(_hits)} 個** "
          f"(帰無の期待 {_tried * 0.05:.1f} 個)")
    if _hits:
        print(f"\n  候補の中身:")
        for _r in _hits:
            print(f"    ▶ {_r['label']} ({_r['col']})")
            for _q, _n, _bpv, _tv in _r["rows"]:
                _m = " ★最良" if _q == _r["best"][0] else ""
                print(f"       {_q:<5}{_n:>9,}{_bpv:>+9.1f}bp{_tv:>+8.2f}{_m}")
        print(f"\n  ⛔ **ここで採用しないこと。** TRAIN で最良を選んだだけです。")
        print(f"     TEST で検証するには 1候補につき1回だけ:")
        for _r in _hits:
            print(f"       python analyze_gap_edge.py --workers {a.workers} "
                  f"--days {a.days} --min-gap-bp {a.min_gap_bp:.0f} "
                  f"--split {a.split} --confirm {_r['col']}:{_r['best'][0]}")
    else:
        print(f"\n  ⛔ 候補ゼロ。この母集団でも選別軸は見つかりませんでした。")
        print(f"     §18.13(15軸78検定) / §18.24 / §18.31 / §18.48⑪ と同じ結論です。")
    sys.exit(0)

if a.confirm:
    _cax, _, _cq = a.confirm.partition(":")
    _cax, _cq = _cax.strip(), _cq.strip()
    if _cax not in AXES or not _cq:
        sys.exit(f"[error] --confirm は '軸:分位' の形で指定します(例 atr_pct:Q1)。"
                 f"軸は {', '.join(AXES)}")
    print(f"\n{'=' * 78}\n■ 検証 — **TEST({_test_n}) で1回だけ**\n{'=' * 78}")
    print(f"  候補: {AXES[_cax]} の {_cq}  /  ギャップ ≥{a.min_gap_bp:.0f}bp")
    print(f"  ⚠ 分位の境界は **TRAIN({_train_n}) で決めて TEST に当てはめます**。"
          f"TEST の分布で切り直すと、それは検証ではなく再探索です。")
    _trp, _tep = _pool_of(_train), _pool_of(_test)
    if _cax == "dow":
        _sel_tr = _trp[_trp["dow"] == float(_cq)]
        _sel_te = _tep[_tep["dow"] == float(_cq)]
    else:
        _s_tr = pd.to_numeric(_trp[_cax], errors="coerce")
        try:
            _edges = pd.qcut(_s_tr[_s_tr.notna()], a.nq, retbins=True,
                             duplicates="drop")[1]
        except Exception:
            sys.exit("[error] TRAIN で分位を作れません")
        _qi = int(_cq.lstrip("Qq")) - 1
        if not (0 <= _qi < len(_edges) - 1):
            sys.exit(f"[error] 分位 {_cq} が範囲外(1〜{len(_edges) - 1})")
        _lo_e = -float("inf") if _qi == 0 else float(_edges[_qi])
        _hi_e = float("inf") if _qi == len(_edges) - 2 else float(_edges[_qi + 1])
        print(f"  TRAIN で決めた境界: {_lo_e:,.4g} 〜 {_hi_e:,.4g}")
        _sel_tr = _trp[(pd.to_numeric(_trp[_cax], errors="coerce") >= _lo_e)
                       & (pd.to_numeric(_trp[_cax], errors="coerce") < _hi_e)]
        _sel_te = _tep[(pd.to_numeric(_tep[_cax], errors="coerce") >= _lo_e)
                       & (pd.to_numeric(_tep[_cax], errors="coerce") < _hi_e)]
    print(f"\n  {'窓':<12}{'件数':>10}{'bp/件':>10}{'日t':>9}")
    print("  " + "-" * 42)
    for _nm, _sel in ((f"TRAIN", _sel_tr), (f"TEST", _sel_te)):
        print(f"  {_nm:<12}{len(_sel):>10,}{_bp(_sel):>+10.1f}{_cluster_t(_sel):>+9.2f}")
    _base_tr, _base = _bp(_trp), _bp(_tep)
    _got_tr, _got = _bp(_sel_tr), _bp(_sel_te)
    _d_tr, _d_te = _got_tr - _base_tr, _got - _base
    # TEST の**全分位**を出す。判定は指定分位だけだが、形が見えないと
    # 「最良だけ跳ねている」のか「単調」なのかが分からない。
    # ⚠ ここで別の分位に乗り換えないこと。それは再探索。
    if _cax != "dow":
        print(f"\n  TEST の全分位 (境界は TRAIN で決めたもの / 判定は {_cq} のみ)")
        print(f"    {'分位':<6}{'件数':>10}{'bp/件':>9}{'日t':>8}   TRAIN")
        print("    " + "-" * 46)
        _sv_tr = pd.to_numeric(_trp[_cax], errors="coerce")
        _sv_te = pd.to_numeric(_tep[_cax], errors="coerce")
        for _qq in range(len(_edges) - 1):
            _le = -float("inf") if _qq == 0 else float(_edges[_qq])
            _he = float("inf") if _qq == len(_edges) - 2 else float(_edges[_qq + 1])
            _g_te = _tep[(_sv_te >= _le) & (_sv_te < _he)]
            _g_tr = _trp[(_sv_tr >= _le) & (_sv_tr < _he)]
            if _g_te.empty:
                continue
            _m2 = " ★判定" if _qq == _qi else ""
            print(f"    Q{_qq + 1:<5}{len(_g_te):>10,}{_bp(_g_te):>+9.1f}"
                  f"{_cluster_t(_g_te):>+8.2f}{_bp(_g_tr):>+8.1f}{_m2}")
        print(f"    ⚠ ここで別の分位に乗り換えないこと。それは検証ではなく再探索です。")
    print(f"\n  {'窓':<8}{'絞らない':>10}{'絞った':>10}{'改善':>10}")
    print("  " + "-" * 38)
    print(f"  {'TRAIN':<8}{_base_tr:>+10.1f}{_got_tr:>+10.1f}{_d_tr:>+10.1f}")
    print(f"  {'TEST':<8}{_base:>+10.1f}{_got:>+10.1f}{_d_te:>+10.1f}")
    _ok1, _ok2 = _got >= PASS_BP, _cluster_t(_sel_te) >= PASS_T
    print(f"\n  {'✅' if _ok1 else '⛔'} ① TEST bp ≥ {PASS_BP}       {_got:+.1f}bp")
    print(f"  {'✅' if _ok2 else '⛔'} ② TEST t ≥ {PASS_T}          "
          f"t={_cluster_t(_sel_te):+.2f}")
    print(f"  {'✅' if _got > _base else '⛔'} ③ TEST で絞らない場合を上回る  {_d_te:+.1f}bp")
    _ok4 = _d_tr > 0 and (_d_te <= 0 or _d_tr >= _d_te / 3.0)
    print(f"  {'✅' if _ok4 else '⛔'} ④ **TRAIN でも効いている**    "
          f"TRAIN {_d_tr:+.1f}bp / TEST {_d_te:+.1f}bp")
    _core = _ok1 and _ok2 and _got > _base
    print(f"\n  {'=' * 68}")
    print(f"  {'✅ **①〜③ 合格。**' if _core else '⛔ **不合格。**'}"
          f" 候補 {AXES[_cax]}:{_cq}")
    if _core and not _ok4:
        # §18.13 に自分で書いた作法。③だけだと TEST 期間のノイズを拾う。
        print(f"\n  ⚠⚠ **ただし ④ が落ちている。そのまま採用してはいけない。**")
        print(f"     TRAIN の改善 {_d_tr:+.1f}bp に対し TEST は {_d_te:+.1f}bp。")
        print(f"     §18.13:『TRAIN で効いていないものを候補にしない。符号一致だけを")
        print(f"     条件にすると TRAIN t=+0.2 のような無意味な値でも通る。**それは")
        print(f"     検証ではなく TEST 期間のノイズ**』")
        print(f"     → --explore で TRAIN の分位別を見ること(TEST は汚れません)。")
        print(f"       TRAIN でも単調に効いていれば本物、Q1 だけ跳ねていればノイズ。")
    print(f"  ⛔ 不合格なら、別の分位・別の軸で試し直さないこと。")
    print(f"     試すたびに TEST が既見になり、検証手段が減ります。")
    print(f"  {'=' * 68}")

    # ── 月別の実額 (予算シミュレーション) ────────────────────────────
    #   毎日、候補を **流動性降順**に並べて予算が尽きるまで建てる。
    #   ⚠ 予算超過は `continue`(貪欲。次の安い注文を試す)。レポート側 _run_budget_sim
    #     と同じ挙動に揃える(§18.33: ズレたときの正解はレポート側)。
    #   ⚠ slip=0。呼値も引いていない **理論値の上限**。
    #   ⚠ 発注順は §18.24 で「何にしてもランダムと区別できない」と出ている。
    #     流動性降順にするのは、バックテストに映らない執行コストのため(§18.21)。
    if a.budget_man > 0:
        def _sim(sel: pd.DataFrame, lbl: str):
            if sel.empty:
                return None
            _cap = a.budget_man * 10_000.0
            _recs = []
            for _d, _g in sel.groupby("date"):
                _g = _g.sort_values("liq", ascending=False, na_position="last")
                _cash, _p, _n = _cap, 0.0, 0
                for _ep, _pn in zip(_g["entry_p"], _g["pnl"]):
                    _cost = float(_ep) * a.qty
                    if _cost > _cash:
                        continue
                    _cash -= _cost
                    _p += float(_pn)
                    _n += 1
                _recs.append({"date": _d, "month": str(_d)[:7], "pnl": _p,
                              "n": _n, "used": _cap - _cash})
            return pd.DataFrame(_recs)

        for _lbl, _sel in (("TEST", _sel_te), ("TRAIN", _sel_tr)):
            _sm = _sim(_sel, _lbl)
            if _sm is None or _sm.empty:
                continue
            _mo = _sm.groupby("month").agg(
                日数=("pnl", "size"), 建てた=("n", "sum"),
                投入=("used", "mean"), 損益=("pnl", "sum"),
                勝日=("pnl", lambda s: int((s > 0).sum())))
            print(f"\n{'=' * 78}")
            print(f"■ 月別の実額 — {_lbl} / 予算 {a.budget_man:,.0f}万円 / "
                  f"流動性降順 / **slip=0 の理論値**")
            print(f"{'=' * 78}")
            _show = _mo if a.months <= 0 else _mo.tail(a.months)
            print(f"  {'月':<9}{'日数':>5}{'建てた':>7}{'件/日':>7}"
                  f"{'投入/日':>10}{'損益':>12}{'勝日':>7}")
            print("  " + "-" * 58)
            for _m, _r in _show.iterrows():
                _wd = f"{int(_r['勝日'])}/{int(_r['日数'])}"
                print(f"  {_m:<9}{int(_r['日数']):>5}{int(_r['建てた']):>7}"
                      f"{_r['建てた'] / max(1, _r['日数']):>7.1f}"
                      f"{_r['投入'] / 10_000:>9,.0f}万{_r['損益']:>+12,.0f}"
                      f"{_wd:>8}")
            _mv = _mo["損益"]
            _mu, _sd = float(_mv.mean()), float(_mv.std(ddof=1))
            _tt = (_mu / (_sd / (len(_mv) ** 0.5))) if len(_mv) > 1 and _sd > 0 else 0.0
            print("  " + "-" * 58)
            print(f"  {len(_mv)}ヶ月  月平均 {_mu:>+11,.0f}円  月次σ {_sd:>10,.0f}円  "
                  f"月平均/σ {_mu / _sd if _sd > 0 else 0:.2f}  t={_tt:+.2f}")
            print(f"          プラス月 {int((_mv > 0).sum())}/{len(_mv)}  "
                  f"最良 {_mv.max():+,.0f}  最悪 {_mv.min():+,.0f}")
            _need_m = int((2.0 / max(_tt, 1e-9)) ** 2 * len(_mv)) if _tt > 0 else 0
            if _tt > 0:
                print(f"          t=2 に届くのに必要な月数: 約{_need_m}ヶ月")
        print(f"\n  ⚠ **slip=0 / 呼値も引いていない理論値の上限**です。")
        print(f"     実スプレッドぶん(呼値3.3bp〜)は必ず下がります。")
        print(f"  ⛔ 月別を見て『良い月だけ採用する』ことはできません。"
              f"どの月かは事前に選べません。")
    sys.exit(0)

_verdict: dict[str, dict] = {}
for _wname, _w in _windows:
    if _w.empty:
        continue
    print(f"\n{'=' * 78}\n■ {_wname}\n{'=' * 78}")
    _wp = _pool_of(_w)                       # 判定対象(--pool)
    _rows_pool = _band_table(_wp, f"★ 判定対象: {_POOL_LBL[a.pool]} ({len(_wp):,}銘柄日)")
    # 参考: シグナル有無で分けた表(§18.52 の再確認。判定には使わない)
    if a.pool == "all":
        _ws, _wc = _w[_w["sig"] == 1], _w[_w["sig"] == 0]
        if not _ws.empty:
            _band_table(_ws, f"参考: シグナルあり ({len(_ws):,}銘柄日)")
        if not _wc.empty:
            _band_table(_wc, f"参考: シグナルなし ({len(_wc):,}銘柄日)")
    # 単調性(帯の並び vs bp)
    _rho = 0.0
    if len(_rows_pool) >= 3:
        _xs = list(range(len(_rows_pool)))
        _rho = _spearman([float(x) for x in _xs], [x[3] for x in _rows_pool])
    _all_bp, _all_t = _bp(_wp), _cluster_t(_wp)
    print(f"\n    {'★ 判定対象 合計':<22}{len(_wp):>9,}"
          f"{_wp['pnl'].mean():>+10,.0f}{_all_bp:>+9.1f}{_all_t:>+8.2f}")
    # ④ 閾値ありだと帯が2つしか残らず ρ が意味をなさない。前半/後半の符号一致に差し替え
    _h1_bp = _h2_bp = 0.0
    if a.min_gap_bp > 0 and not _wp.empty:
        _ds = sorted(_wp["date"].unique())
        _mid = _ds[len(_ds) // 2]
        _h1, _h2 = _wp[_wp["date"] < _mid], _wp[_wp["date"] >= _mid]
        _h1_bp, _h2_bp = _bp(_h1), _bp(_h2)
        print(f"    前半({str(_ds[0])[:7]}〜) {_bp(_h1):+.1f}bp / "
              f"後半({str(_mid)[:7]}〜) {_bp(_h2):+.1f}bp   ← ④ 両方プラスが合格")
    else:
        print(f"    単調性 Spearman = {_rho:+.2f} (合格 ≥ {PASS_RHO})")
    _verdict[_wname] = {"bp": _all_bp, "t": _all_t, "rho": _rho,
                        "h1": _h1_bp, "h2": _h2_bp,
                        "n": len(_wp), "rows": _rows_pool}

# ── 帰無較正 ──────────────────────────────────────────────────────
#   閾値ありのとき: **判定窓の中で**「閾値以上」vs「ギャップダウン」を比べる。
#     ⚠ 判定窓に揃えないと、既見期間の数字で較正することになる。
#   閾値なしのとき: 全期間の最上位帯 vs 最下位帯 (第1回・第2回と同じ)。
_null = None
_pool_all = _pool_of(r_all)
_wdict = dict(_windows)
if a.min_gap_bp > 0:
    _nsrc = _wdict.get(_judge_win, r_all)
    if a.pool == "sig":
        _nsrc = _nsrc[_nsrc["sig"] == 1]
    elif a.pool == "nosig":
        _nsrc = _nsrc[_nsrc["sig"] == 0]
    _nsrc = _nsrc.copy()
    _hi, _lo = f"≥{int(a.min_gap_bp)}bp", "< 0"
    _nsrc["band"] = _nsrc["gap_bp"].map(
        lambda g: _hi if g >= a.min_gap_bp else (_lo if g < 0 else "中間"))
    _nwin = _judge_win
else:
    _nsrc = _pool_all
    _rws = _verdict.get("全期間", {}).get("rows", [])
    _hi, _lo = (_rws[-1][0], _rws[0][0]) if len(_rws) >= 2 else ("", "")
    _nwin = "全期間"
if _hi and _lo and not _nsrc.empty:
    print(f"\n{'=' * 78}\n■ 帰無較正 — 同じ日の中で帯ラベルをシャッフル ({a.seeds}本)\n{'=' * 78}")
    print(f"  ⛔ 日をまたぐシャッフルは日内相関を壊して帰無分布が狭くなり、"
          f"偽陽性を過小評価する(§18.13)")
    print(f"  対象窓 = {_nwin} ({_nsrc['date'].nunique():,}営業日 / {len(_nsrc):,}銘柄日)")
    _obs, _med, _p95 = _null_calib(_nsrc, _hi, _lo)
    print(f"\n  スプレッド『{_hi}』−『{_lo}』")
    print(f"    実測      {_obs:+8.1f} bp")
    print(f"    帰無 中央 {_med:+8.1f} bp   ← 0 中心とは限らない。ここと比べること")
    print(f"    帰無 95%  {_p95:+8.1f} bp")
    _null = (_obs, _med, _p95)
    print(f"    → {'✅ 帰無の95%点を超えた' if _obs > _p95 else '⛔ 帰無の中。軸として機能していない'}")

# ── 判定 (事前宣言した条件。ここを書き換えたらこの測定は無効) ──────────
# ⛔ --dump-only は判定しない。--out で CSV を作るだけの実行が「不合格」として
#    試行回数に積まれると、多重検定の補正が壊れる(2026-08-26 に実際に起きた)。
if a.dump_only:
    print(f"\n{'=' * 78}\n■ --dump-only — 合否は出しません\n{'=' * 78}")
    print(f"  CSV を書くための実行なので、判定も試行記録もしません。")
    print(f"  仮説を測るときは --min-gap-bp <閾値> を付けて --dump-only を外すこと。")
    raise SystemExit(0)

print(f"\n{'=' * 78}\n■ 判定 — 事前宣言した条件\n{'=' * 78}")
if a.min_gap_bp <= 0:
    print(f"  ⛔⛔ **--min-gap-bp が 0 です。ギャップ仮説を測っていません。**")
    print(f"     ① の bp/件 は **全 {len(_pool_all):,}銘柄日の平均**"
          f"(ギャップがマイナスの日も全部込み)。")
    print(f"     帯別の表では上の帯がプラスでも、合計はほぼ 0 になります。")
    print(f"     **この『不合格』を『仮説が否定された』と読まないこと。**")
    print(f"     仮説を測るなら --min-gap-bp 100 のように閾値を付けること。")
    print(f"     CSV を作りたいだけなら --dump-only を付けること(試行に数えない)。")
print(f"  母集団={a.pool} / 執行={a.exec_mode}({EXEC_BP:.1f}bp) / "
      f"①の水準は執行コストの3倍 = {PASS_BP}bp")
if a.min_gap_bp > 0:
    print(f"  ★ 判定は **最も古い窓({_judge_win}) だけ**。他は既見なので参考")
_need = [_judge_win] if a.min_gap_bp > 0 else (
    [w for w, _ in _windows if w != "全期間"] or ["全期間"])
_pass = []
for _k in _need:
    v = _verdict.get(_k)
    if not v:
        _pass.append((f"{_k}", False, "窓が空"))
        continue
    _pass.append((f"① {_k} bp/件 ≥ {PASS_BP}", v["bp"] >= PASS_BP, f"{v['bp']:+.1f}bp"))
    _pass.append((f"② {_k} 日クラスタ t ≥ {PASS_T}", v["t"] >= PASS_T, f"t={v['t']:+.2f}"))
    if a.min_gap_bp > 0:
        _pass.append((f"④ {_k} 前半・後半とも プラス",
                      v["h1"] > 0 and v["h2"] > 0,
                      f"前半 {v['h1']:+.1f} / 後半 {v['h2']:+.1f}bp"))
    else:
        _pass.append((f"④ {_k} 単調 ρ ≥ {PASS_RHO}", v["rho"] >= PASS_RHO,
                      f"ρ={v['rho']:+.2f}"))
if _null:
    _pass.append(("⑤ 帰無の95%点を超える", _null[0] > _null[2],
                  f"{_null[0]:+.1f} vs {_null[2]:+.1f}bp"))
else:
    _pass.append(("⑤ 帰無較正", False, "計算できず"))

for _lbl, _ok, _det in _pass:
    print(f"  {'✅' if _ok else '⛔'} {_lbl:<34}{_det}")
_ok_all = all(x[1] for x in _pass)
_result = "合格" if _ok_all else "不合格"
if _DENSITY_FAIL:
    _result = "測定不能"          # データ欠落を「不合格」と数えない
elif a.min_gap_bp <= 0:
    # 閾値なし = ギャップ仮説を測っていない。記録は残すが試行には数えない。
    _result = "参考(閾値なし)"
print(f"\n  {'=' * 60}")
if _DENSITY_FAIL:
    print(f"  ⛔⛔ **測定不能。上の判定は読まないこと。**")
    print(f"     {_DENSITY_FAIL}")
    print(f"     判定窓にデータが入っていないので、①②が落ちるのは当たり前。")
    print(f"     **これは仮説の不合格ではない。** 試行回数にも数えない。")
    print(f"     → キャッシュを遡らせて測り直すこと(--no-refetch を付けない)。")
elif _ok_all:
    print(f"  ✅ **全条件 合格。土台がある。**")
    print(f"     ⛔ ここでパラメータ(sm/tm/delay/予算)を足して最適化しないこと。")
    print(f"        足した瞬間に、これまでと同じ多重検定が始まる。")
    print(f"     次は **前向きに小ロットで回して実約定を測る**(バックテストはここまで)。")
else:
    print(f"  ⛔ **不合格。この方向は追わない。**")
    print(f"     パラメータを足して救おうとしないこと。救おうとした瞬間に")
    print(f"     『測ってから良かった方を採る』が再開する(方式4回・パラメータ数十回の再現)。")
print(f"  {'=' * 60}")

# ── 試行の記録。多重検定の補正に使う。**消さないこと** ────────────────
try:
    import csv as _csv
    from datetime import datetime as _dtn
    from pathlib import Path as _Pth
    # ⚠ 列は **固定**。判定窓の名前で列名を作ると、窓が変わったときヘッダと
    #   値がズレる(第2回=未使用/既見・第3回=年月レンジ)。
    _HDR = ["実行時刻", "母集団", "執行", "閾値bp", "PASS_BP", "遡及日", "境界",
            "判定窓", "件数", "判定bp", "判定t", "判定④", "帰無実測", "帰無95",
            "判定", "メモ"]
    _tp = _Pth(TRIALS_CSV)
    _old_hdr = None
    if _tp.exists():
        try:
            with open(_tp, encoding="utf-8-sig") as _rh:
                _old_hdr = next(_csv.reader(_rh), None)
        except Exception:
            pass
    _jw = _need[0] if _need else "全期間"
    _v0 = _verdict.get(_jw, {})
    _c4 = (f"前半{_v0.get('h1', 0):+.1f}/後半{_v0.get('h2', 0):+.1f}"
           if a.min_gap_bp > 0 else f"ρ={_v0.get('rho', 0):+.2f}")
    with open(_tp, "a", newline="", encoding="utf-8-sig") as _fh:
        _w = _csv.writer(_fh)
        if _old_hdr != _HDR:
            _w.writerow(_HDR)
        _w.writerow([_dtn.now().strftime("%Y-%m-%d %H:%M:%S"), a.pool, a.exec_mode,
                     a.min_gap_bp, PASS_BP, a.days, a.split or "-",
                     _jw, len(_pool_all),
                     f"{_v0.get('bp', 0):.1f}", f"{_v0.get('t', 0):.2f}", _c4,
                     f"{_null[0]:.1f}" if _null else "",
                     f"{_null[2]:.1f}" if _null else "",
                     _result,
                     (a.note or (f"⛔ {_DENSITY_FAIL}" if _DENSITY_FAIL else ""))])
    # ヘッダ行を除き、**測定不能は数えない**(データ欠落は仮説を試したことにならない)
    # ⚠ 数えるのは「仮説を測った実行」だけ。
    #    ・測定不能(データ欠落) … 仮説を試したことにならない
    #    ・閾値bp = 0        … ①が全銘柄日の平均になるのでギャップ仮説を
    #                          測っていない。**過去の行も遡って除外する**
    #                          (§18.52 に「閾値を事前に決めるべきだった」と
    #                           自分で記録した設計ミスがそのまま数に入っていた)
    def _counts(_r: list) -> bool:
        if not _r or _r[0] == "実行時刻":
            return False
        if "測定不能" in _r or "参考" in _r:
            return False
        try:
            return float(_r[3]) > 0        # 閾値bp
        except Exception:
            return True                    # 読めない古い行は安全側で数える
    with open(_tp, encoding="utf-8-sig") as _rh:
        _rows = [_r for _r in _csv.reader(_rh)]
    _n_trials = sum(1 for _r in _rows if _counts(_r))
    _skipped = sum(1 for _r in _rows
                   if _r and _r[0] != "実行時刻" and not _counts(_r))
    print(f"\n  [試行記録] {TRIALS_CSV} に追記 — **通算 {_n_trials} 回目**"
          + (f"  (数えない行 {_skipped}: 測定不能 / 閾値なし)" if _skipped else ""))
    if _n_trials >= 3:
        _fp = 1.0 - (0.95 ** _n_trials)
        print(f"    ⚠ {_n_trials}回試すと、**中身がランダムでも {_fp*100:.0f}% の確率で"
              f"どれか1つは 5% 水準を通る**。")
        print(f"       合格が出ても、この回数を必ず併記すること(§18.51 B2 の反省)。")
except Exception as _e:
    print(f"\n  [warn] 試行記録を書けませんでした: {_e}")

print(f"""
{'=' * 78}
■ このツールが測っていないもの (合格しても残る未知数)
{'=' * 78}
  * スプレッド・執行遅延 — §18.44 実測 1分 −15.8bp / 3分 −29.4bp / 5分 −36.6bp。
    しかも 60〜71%が不利側。**上の bp から引くこと。**
  * 空売り在庫・貸株可否 — 今日のリストしか無い
  * 生存バイアス — 今日の上場銘柄しか見ていない
  * 決算・特別気配・IPO — 全部建てる前提
""")
