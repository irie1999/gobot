# 日経平均 予測 — 調査メモ & 手法提案 (v1)

作成: 2026-09-06 / 担当: Claude (相方: Codex)
対象ブランチ: `claude/nikkei-average-prediction-4m07mz`

このメモは「日経平均を予測する」という課題に対する **文献・実務調査** と、
gobot リポジトリで実際に検証するための **手法の優先順位付け** をまとめたもの。
コードは `n225_research.py` (評価ハーネス) / `n225_veto.py` (N 停止判定器) に対応。

> **具体的な運用要件がある場合は §10 を先に読むこと。**
> 「N (寄り後にギャップアップ銘柄を空売り→引け決済) の発注前 09:00 までに
> 日経の当日 始値→終値 を予測し、強気日だけ N を止める / 縮小する」という要件に対する
> 設計・検証手順・判定基準をまとめてある。§0-§9 はその背景となる一般調査。

---

## 0. 結論を先に (TL;DR)

1. **「日経平均の水準を当てる」は捨てる。** 日次の水準予測で MAPE 1〜2% という
   論文/ブログの数字は、`予測 = 前日終値` というナイーブ予測 (日次 MAPE ≒ 0.9%)
   に負けている。R²=0.97 も、トレンドのある水準系列なら自明に出る。
   **水準予測の精度指標 (MAPE / RMSE / R²) は、この問題では情報量ゼロ。**
2. **予測すべきは 3 つに分解したリターン。**
   - `gap` = 翌日始値 / 当日終値 − 1 (オーバーナイト)
   - `day` = 当日終値 / 当日始値 − 1 (日中/ザラ場)
   - `c2c` = gap + day
   日経の close-to-close 変動は **大部分が gap** で説明される (米国市場の翌朝反映)。
   gap は「当てやすいが、夜間先物が既に織り込んでいるので儲からない」。
   **本当のアルファは (a) 日中リターン `day` と (b) gap − 夜間先物が示唆する gap の残差** にしかない。
   ここを分けずに c2c の的中率を測るのが、この分野で最も多い自己欺瞞。
3. **確実に予測可能なのはリターンではなくボラティリティ。** HAR-RV / realized EGARCH 系は
   日経でも安定して機能する。→ 方向当てより **サイジングとレジームフィルタ** に使う方が
   期待値が高い。
4. **gobot にとっての最大の実利は「相場レジームフィルタ」。** 既存の 6 戦略
   (MACD/A7/RSI2/DON/VOL/MOM) は全て買い専。日経のレジーム (トレンド/レンジ/急落) で
   発注可否と枚数を切り替えるだけで、自由度が低く過学習しにくい形で成績が改善する見込み。
   「日経平均予測」を独立の予測器として作るより、**既存パイプラインの上に薄く乗せる**方が
   ROI が高い。
5. **評価プロトコルが本体。** 365 日のデータで検出できるのは 58% 以上の的中率だけ
   (§5 の検出力計算)。ベースライン (常に上昇 / 前日と同符号 / ランダムウォーク) を
   置かない実験は全部やり直しになる。

---

## 1. 何を予測するか — ターゲット定義

| 記号 | 定義 | 予測可能性 | 取引可能性 | 備考 |
|---|---|---|---|---|
| `c2c` | 終値[t] / 終値[t−1] − 1 | 中 | △ | gap に支配される。単独で測ると誤解を生む |
| `gap` | 始値[t] / 終値[t−1] − 1 | **高** | ✗ (単独では) | 米国市場が説明。現物では取れない |
| `gap_resid` | `gap` − 夜間先物(大取/CME)が示唆する gap | 低〜中 | **○** | 真のアルファ候補。要先物データ |
| `day` | 終値[t] / 始値[t] − 1 | 低〜中 | **○** | 寄り成行 → 引け成行で取れる。研究の主戦場 |
| `RV` | 実現ボラティリティ | **高** | ○ (間接) | サイジング / フィルタ / オプション |
| `regime` | 上昇 / レンジ / 急落 の状態 | 中 | **◎** | 既存 gobot 戦略のスイッチに直結 |

日中/オーバーナイトを分けずに c2c だけで評価すると、
「米国の前日リターンを入れたら的中率 65%」という結果が簡単に出る。
これは**予測ではなく単なる時差**であり、寄り付き時点で誰でも知っている情報。
現物・先物とも寄り付きで既に織り込まれているので 1 円も取れない。

---

## 2. 文献・実務サーベイ (要点)

### 2.1 方向予測の現実的な天井

- 為替・金利・株価指数のいずれでも「ランダムウォークを一貫して上回るのは難しい」が
  一般的結論。被験者実験でも予測成功率は 50% と有意差なし。
  ([Market participants or the random walk](https://www.sciencedirect.com/science/article/pii/S1544612323001253),
   [Predicting the unpredictable](https://www.sciencedirect.com/science/article/abs/pii/S0165188922002743))
- SPY を対象にした 2026 のウォークフォワード・ベンチマークは、**高い的中率が出る領域は
  サンプルサイズが急速に痩せる領域**であることを示している。閾値で絞った的中率を
  報告するときは必ず母数を併記せよ、という警告。
  ([A Statistical-Finance Benchmark for Same-Day Directional Stock Prediction](https://arxiv.org/html/2608.26106))
- → **実務的な合格ラインは「的中率 53〜56% を数千サンプルで、コスト後に維持」**。
  70% を謳う結果はほぼ確実にリーク (§6) か過学習。

### 2.2 深層学習・時系列基盤モデル

- 2026 のベンチマーク (TimeGPT / TimesFM-2.5 / Moirai-2.0 / Chronos / Chronos-2 vs
  NBEATS/NHITS/PatchTST/iTransformer/KAN) では、**ランダムウォークに対する改善は
  小さく散発的**。Diebold-Mariano で有意になったのは 5 銘柄中 2 ケースのみ。
  ([Pretrained Time-Series Foundation Models for Financial Return Forecasting](https://arxiv.org/abs/2606.27100))
- 別研究では、既製 TSFM の zero-shot は日次超過リターンで **CatBoost/LightGBM に劣後**。
  ([Re(Visiting) Time Series Foundation Models in Finance](https://arxiv.org/html/2511.18578v1))
- 日経 225 に対する LSTM / Transformer の実装記事は多数あるが、多くが「過去 60 営業日 →
  水準予測」で、評価指標が MAPE。§0-1 の理由で採用しない。
  ([Qiita: AIモデルを構築して日経平均株価を予測](https://qiita.com/kinopy513/items/7ec37b9bab24d6e20192))
- → **結論: まず GBDT (LightGBM / HistGradientBoosting) をベースラインに置く。
  深層/基盤モデルは「GBDT を有意に超えたら採用」の位置づけ。最初にやることではない。**

### 2.3 米国 → 日本のリードラグ (最重要の実装対象)

- 2026 年の研究: **前日の S&P500 リターンが高いほど、日本株の寄り後 30 分は低リターン
  (リバーサル)、大引け前 30 分は高リターン (モメンタム)** という日中パターン。
  ([How the prior day's S&P 500 returns influence the intraday returns of Nikkei 225 futures](https://www.sciencedirect.com/science/article/pii/S3050700626000204))
- 一方、寄り付きの非同時性を調整すると「米国が日本をリードする」という単純な関係は消える
  (双方の寄り値が相手のオーバーナイト変動を織り込むため)。
  ([The relationship between daily U.S. and Japanese equity prices](https://www.sciencedirect.com/science/article/abs/pii/0378426694000190))
- クロス指数のオーバーナイトリターンを **実行可能な** 朝ギャップ予測に変換する研究も出ている。
  ([When "overnight" is not simultaneous](https://www.sciencedirect.com/science/article/pii/S2214845026000918))
- 実務側: CME 日経先物は日本時間 20:00〜翌 5:15、大取ナイトセッションは 17:00〜翌 6:00。
  翌日の寄りは **既に夜間で値が付いている**。
  ([JPX ナイト・セッション](https://www.jpx.co.jp/derivatives/rules/trading-hours/01.html),
   [CME 日経平均先物チャート](https://chartpark.com/cme.html))
- → **実装方針: `gap` は「予測課題」ではなく「特徴量」として扱う。
  検証すべきは `day` (寄り→引け) と `gap_resid` (夜間先物との乖離)。**

### 2.4 ボラティリティ (最も再現性が高い領域)

- HAR-RV は異なる時間スケールのボラを統合するモデルで、GARCH / ARFIMA-RV を有意に上回る。
  ([A Practical Guide to harnessing the HAR volatility model](https://www.sciencedirect.com/science/article/abs/pii/S0378426621002417))
- 日経 225 では realized EGARCH / realized SV が HAR・REGARCH を上回るという結果。
  「今日のリターンと明日のボラの負の相関 (レバレッジ効果)」がキー。
  ([Improving volatility forecasts of the Nikkei 225 stock index](https://arxiv.org/abs/2502.02695))
- → **日次データしか無くても、Parkinson / Garman-Klass の レンジベース推定量で
  RV の代用が作れる。日足のみの環境でも HAR は組める。**

### 2.5 VIX / 日経VI

- VIX は **水準だけでなく「変化」も併用**すると予測力が上がる。VIX が下落している局面の
  日経リターンは一貫してプラス。VIX 40 超からの低下局面では半年後 +20% 超のケースも。
  ([マネクリ: 恐怖指数は「水準」だけでなく「変化」も使う](https://media.monex.co.jp/articles/-/28994))
- → 特徴量に `vix_level`, `vix_chg_1`, `vix_chg_5`, `vix_z20` を入れる。
  日経VI (^N225 のインプライド) も取得できれば **VIX との差 (日米ボラスプレッド)** が有用候補。

### 2.6 需給フロー (日本市場固有)

- 投資部門別売買状況 (JPX、毎週木曜引け後公表)。「外国人が買い越す月は日経が上昇」という
  傾向が 30 年以上継続。ただし **単週では騙され、4 週〜13 週の累計で見るべき**。
  ([JPX 投資部門別売買状況](https://www.jpx.co.jp/markets/statistics-equities/investor-type/04.html),
   [三井住友DS: 海外投資家と個人投資家の日本株売買状況](https://www.smd-am.co.jp/market/ichikawa/2025/05/irepo250527/))
- 裁定買い残は海外投機筋のポジションの鏡。2.5 兆円超で短期反落リスク。
  ([楽天証券トウシル: 裁定買い残の読み方](https://media.rakuten-sec.net/articles/-/50283))
- → **週次・公表ラグあり**。Point-in-Time (公表日ベース) で結合しないと巨大なリークになる。
  「当該週のデータを、その週の取引日に使う」は禁止。**公表日の翌営業日から使用可**。

### 2.7 ニュース / SNS センチメント

- 2026 の研究: **LLM でノイズの多い SNS を事前フィルタ**すると、無フィルタ・キーワード
  フィルタの双方に対して日経 225 の方向的中率が有意に改善。LSTM でも Transformer でも
  効いた (モデル非依存)。
  ([Enhancing the Accuracy of Nikkei 225 Forecasting Via LLM-Based Filtering of Noisy Social Media Data](https://dl.acm.org/doi/full/10.1145/3803291.3803359))
- → 効果は期待できるが、**日本語ニュースの PIT 付きアーカイブ**が必要でデータ整備コストが最大。
  フェーズ 3 以降。

### 2.8 季節性

- 日経は曜日・時間帯で「性格」が変わる (東京の価格発見、後場、欧州参入、米国主導のリプライシング)。
  ([The Nikkei 225's Hidden Clock](https://www.mql5.com/en/blogs/post/773070))
- 季節性チャートに基づく「3/14 買い → 4/7 売り」等の主張は、10 年 × 1 パターン = **サンプル 10**。
  多重検定を考えれば有意ではない。特徴量に月・曜日を入れるのは可だが、
  **単独のシーズナリティ戦略は採用しない**。
  ([Equity Clock N225 Seasonal Chart](https://equityclock.com/charts/nikkei-225-index-n225-seasonal-chart/))

---

## 3. 手法の優先順位 (期待エッジ × 実装コスト)

| # | 手法 | 期待エッジ | 実装コスト | 判定 |
|---|---|---|---|---|
| 1 | **リターンの gap / day 分解 + `day` の方向モデル** | 中 | 低 | ◎ 最優先 |
| 2 | **HAR-RV (レンジベース) ボラ予測 → サイジング/フィルタ** | 中〜高 | 低 | ◎ 最も再現性が高い |
| 3 | **日経レジーム分類 → 既存 gobot 6 戦略の発注フィルタ** | 中 | 低 | ◎ リポジトリ直結・自由度が低い |
| 4 | **VIX 水準×変化、日米ボラスプレッド** | 小〜中 | 低 | ○ 特徴量として必ず入れる |
| 5 | **GBDT (30〜50 特徴量) with walk-forward** | 小〜中 | 中 | ○ ML のベースライン |
| 6 | **`gap_resid` (夜間先物との乖離)** | 中 | 中 (先物データ) | ○ フェーズ 2 |
| 7 | **需給フロー (投資部門別 / 裁定買い残, 週次 PIT)** | 小〜中 | 中 | ○ フェーズ 2 |
| 8 | **LLM フィルタ済みニュース/SNS センチメント** | 中 | 高 | △ フェーズ 3 |
| 9 | 時系列基盤モデル (TimesFM/Chronos) zero-shot | 小 | 中 | △ 比較用に 1 回だけ |
| 10 | LSTM/Transformer による**水準**予測 (MAPE 評価) | ~0 | 中 | ✗ 採用しない |
| 11 | 単独シーズナリティ (「3/14 買い」等) | ~0 | 低 | ✗ 採用しない |
| 12 | 年次予測 (「2026 年末 84,466 円」等) | ~0 | 低 | ✗ 運用に接続しない |

---

## 4. 提案パイプライン

```
Phase 0  評価基盤とデータ層          ← ここが本体。ここを飛ばすと全部無駄になる
  0-1  パネル構築 (^N225 / ^GSPC / ^VIX / JPY=X / ^TNX / ^SOX)
       - 米国系列は日付インデックス上で必ず 1 日以上ラグ (§6.1)
  0-2  ターゲット 3 種 (gap / day / c2c) + RV
  0-3  walk-forward (expanding, purge/embargo 付き) 評価器
  0-4  ベースライン: 常に上昇 / 前日と同符号 / ランダムウォーク / AR(1)
  0-5  指標: 的中率, ベースライン差, 二項検定 p 値, Brier, LogLoss, AUC,
            コスト後 PnL, ターンオーバー, Sharpe

Phase 1  シンプルモデルで「あるかないか」を判定
  1-1  ロジスティック回帰 (L2) on 10〜15 特徴量 → `day` の方向
  1-2  HAR-RV 回帰 → 翌日 RV (対 naive RV_prev の OOS R² / QLIKE)
  1-3  結果を Phase 0 の指標で判定。ベースライン超えが無ければ深追いしない

Phase 2  データを足す
  2-1  夜間先物 (大取 / CME) → `gap_resid`
  2-2  投資部門別売買状況・裁定買い残 (週次, PIT 結合)
  2-3  日経VI → 日米ボラスプレッド
  2-4  GBDT + 特徴量重要度 / SHAP で 1-1 と比較

Phase 3  運用への接続 (gobot 本体)
  3-1  レジーム出力 (0..1 の確率 or 3 クラス) を run_signals に注入
  3-2  レジームで (a) 発注可否 (b) 枚数 (c) tm/sm プリセット を切替
  3-3  forward_test.py と同じ枠組みでレジームフィルタ有無の A/B を紙トレード記録
```

**Phase 1 で何も出なければ、そこで止めるのが正しい。** これは失敗ではなく、
「日次方向にはコスト後のエッジが無い」という有用な結論。その場合はボラ予測
(#2) とレジームフィルタ (#3) に全振りする。

---

## 5. 検出力 — 何日分のデータが要るか

方向的中率 p1 を、基準 p0 = 50% に対して有意水準 5% (両側)・検出力 80% で
検出するのに必要なサンプル数 n ≈ (z_{0.975} + z_{0.80})² · p0(1−p0) / (p1 − p0)²:

| 真の的中率 | 必要サンプル数 (営業日) | 年数 |
|---|---|---|
| 53% | ≈ 2,180 | ≈ 8.9 年 |
| 55% | ≈ 785 | ≈ 3.2 年 |
| 57% | ≈ 400 | ≈ 1.6 年 |
| 60% | ≈ 196 | ≈ 0.8 年 |

**含意:**
- `run_signals.py` 等で常用している **365 日ウィンドウでは、58% 未満のエッジは
  原理的に検出できない**。日経予測の検証は最低 **10 年 (≈2,450 営業日)** の
  日足で回すこと。yfinance の `^N225` は 1965 年頃から取れるので制約にならない。
- 逆に「1 年で 65% 出た」は、上表からすると**運の可能性が十分ある**か、リーク。

---

## 6. 落とし穴チェックリスト

### 6.1 タイムゾーン・リーク (この課題で最悪かつ最頻)
- 日経 D 日足の終値 = 15:00 JST。**S&P500 の D 日足の終値 = 翌 D+1 の 05:00〜06:00 JST。**
- したがって `^GSPC[D]` を使って `^N225[D]` を予測するのは **完全な未来情報**。
  日付で naive に merge するとこのバグが入り、的中率が 70% 超に跳ね上がる。
- **ルール: 米国・欧州系列は日付インデックス上で常に 1 日以上ラグさせる。**
  東京 D 日の寄り前に使える最新の米国情報は `^GSPC[D−1]` の足。
- `JPY=X` など 24 時間市場の日足はタイムスタンプの基準が曖昧。安全側に倒して 1 日ラグ。

### 6.2 週次・低頻度データの PIT
- 投資部門別売買状況は「対象週の翌週木曜」公表。**対象週の日付では使えない。**
  公表日 (+1 営業日) にずらして結合。裁定残高も同様。

### 6.3 水準予測の指標
- MAPE / RMSE / R² を水準に対して報告しない。必ず **リターン**に変換してから評価する。
  ナイーブ予測 (ŷ[t] = y[t−1]) を必ず並べる。

### 6.4 多重検定
- 試した特徴量セット × モデル × ハイパラの総数 N を記録する。
  素の p 値ではなく Bonferroni / Deflated Sharpe Ratio で判定。
  ([The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551))
- CPCV (Combinatorial Purged CV) は walk-forward より過学習検出に強いが、
  まずは purge/embargo 付き walk-forward で十分。エッジが出てから CPCV に上げる。

### 6.5 データ側
- yfinance の `^N225` は配当調整の概念が無いので `Close` と `Adj Close` の差に注意。
- 祝日・半休 (大発会など) の欠損。米国休場日との営業日ズレ。
- **ベンチマークが強い**: 日経はここ 3 年、年 +20% 超が続いている
  ([2025 年は +25%](https://www.forex.com/en/news-and-analysis/nikkei-225-outlook-record-high-tested-as-momentum-and-positioning-fade/))。
  上昇の基準率が 53〜55% 程度あるため、**「常に上昇」ベースラインが強敵**。
  的中率 54% は何も予測していないのと同じ可能性がある。必ずベースライン差で見る。

### 6.6 コスト
- 既存の `backtest_limit_entry.py` のコストモデル
  (`SLIPPAGE_STOP_PCT=0.005`, `FEE_PCT_ONE_WAY=0.001`) と整合させる。
  日経先物・ETF (1321/1570) なら手数料はもっと低いが、
  **日次で毎日ドテンすると往復 0.2% × 250 日 = 50%/年** が消える。
  → 方向モデルは「毎日売買」ではなく **確信度が閾値を超えた日だけ**建てる設計にする。

---

## 7. gobot 既存資産との接続

| 既存資産 | 再利用方法 |
|---|---|
| `backtest_limit_entry.fetch` / `.rsi2_cache/` | 永続キャッシュ機構をそのまま流用可 (最新バー日付ベースの判定) |
| `backtest_limit_entry.fetch_n225_return` | 既に `^N225` を取得している。パネル構築の起点にできる |
| `risk_metrics.py` | MaxDD / 連敗 / Sharpe / リカバリーファクターを予測モデルの PnL 評価に流用 |
| `scan_walkforward.py` の fold 設計 | 「`_TODAY` を触らず df を後方トリミングして窓を切る」手法 (CLAUDE.md §13.4) は そのまま使える |
| `forward_test.py` | レジームフィルタの A/B を紙トレードで記録する枠組みとして再利用 |
| `run_signals.py` | Phase 3 でレジーム確率を注入する接続点 |

**注意:** 既存の 6 戦略ロジックには手を入れない。レジームフィルタは
`run_signals.py` の外側 (シグナル採否・枚数) に薄く乗せる。

---

## 8. 実装済みスクリプト

### 8.1 `n225_research.py` — Phase 0 評価ハーネス

```
# データ取得 (要ネットワーク / yfinance)
python n225_research.py --build --years 15

# 方向予測の評価 (ベースライン + ロジスティック + GBDT)
python n225_research.py --eval --target day                 # 寄り→引け (本命)
python n225_research.py --eval --target day --asof strict   # 当日ギャップを一切使わない
python n225_research.py --eval --target gap                 # オーバーナイト (比較用)

# ボラティリティ予測 (HAR-RV vs naive)
python n225_research.py --eval --task vol

# ネットワーク無しで配管を自己テスト
python n225_research.py --selftest
```

`--selftest` は **シグナルゼロの合成データ**と **既知のシグナルを注入した合成データ**の
両方を流し、前者でベースライン超えが出ない / 後者で検出できることを確認する。
評価コード自体のリーク検査になっている。

出力指標:
`n / 的中率 / 基準率(多数派) / 差分 / 二項検定p / Brier / LogLoss / AUC /
コスト後PnL / 建玉数 / Sharpe`

`--asof` で情報境界を切り替える (詳細は §10.2):
- `strict` … 当日ギャップを使わない。リーク疑義ゼロの下限値
- `at_open` … 09:00 前に先物から観測できるギャップを使う (default)

### 8.2 `n225_veto.py` — N 停止判定器の検証

日経の日中上昇予測で N の発注を止める / 縮小する効果を、
**精度ではなく N のリスク指標**で判定する (§10)。

```
python n225_veto.py --npnl n_pnl.csv        # N の実損益 CSV (date,pnl) がある場合 [推奨]
python n225_veto.py                          # 無い場合は R^2=0.235 の代理損益で検証
python n225_veto.py --asof strict            # 厳格な情報境界
python n225_veto.py --veto-size 0.5          # 停止ではなく半分に縮小
python n225_veto.py --strong-pct 0.8         # 「強い上昇日」を +0.8% 超に変更
python n225_veto.py --selftest               # ネット不要の自己テスト
```

## 9. Codex との分担案 (提案)

同じ議題を並行で進めるので、**同じ結論に別ルートで到達したか**が最大の価値になる。
以下は衝突を減らしつつ相互検証できる分け方の案:

| | Claude | Codex |
|---|---|---|
| 主担当 | 評価基盤 (Phase 0) + `day` 方向モデル + レジームフィルタ | ボラ予測 (HAR-RV / realized EGARCH) + 需給フロー (PIT 結合) |
| 検証 | Codex のボラモデルを Claude のハーネスに載せて再現確認 | Claude の `day` モデルを独自コードで追試 |
| 共通 | ベースライン定義・コストモデル・walk-forward 仕様は **揃える** (でないと数字が比較できない) |

**必ず揃えるべき前提 (これがズレると比較不能):**
- ターゲット定義 (`gap` / `day` / `c2c` の式)
- 米国系列のラグ規則 (日付インデックス上 1 日以上)
- ベースライン (常に上昇 / 前日と同符号 / RW)
- 評価期間 (直近 10 年以上) と walk-forward の窓
- コスト (片道 0.1% + スリッページ、閾値未満の日は建てない)

---

## 10. N 向け設計 — 「日経日中上昇予測で N を止める」

### 10.1 要件の確認と、既存の分析との整合

運用側の要件:

- N は **寄り後に**ギャップアップ銘柄を空売りし、**引けで**決済する
- 大負け日が「日経が日中に上昇した日」に集中
- **N の最初の発注 (09:00) より前**に「今日の日経は始値→終値で上がるか」を予測したい
- 目的は日経を当てることではなく、**N の 月平均÷σ / CVaR / 最大DD を改善**すること

運用側の実測値:

| 説明変数 | N 損益に対する R² |
|---|---|
| 日経 前日終値→当日始値 (ギャップ) | 0.000 |
| 日経 当日始値→当日終値 (日中) | **0.235** |

これは §0-2 / §1 の主張と完全に一致する。N は寄り後に建てるので、
夜間の上昇は既に始値に織り込まれており、**ギャップは N の損益を説明しない**。
説明するのは日中リターンだけ。したがって **予測対象は `day` 一択**で、
`gap` や `c2c` をターゲットにするのは要件違反。

**ただし混同してはいけない点:**
「ギャップ → N 損益 の R² が 0」は、**ギャップが N の損益を直接説明しない**という意味であって、
**ギャップが `day` の予測に役立たない**ことは意味しない。
「大きく窓を開けて寄った日はその後どう動くか」は別の問いで、
ギャップは `day` の**特徴量**として有力な候補。しかも 09:00 前に観測できる (§10.2)。
`n225_research.py` / `n225_veto.py` はギャップを予測対象ではなく特徴量として扱っている。

### 10.2 09:00:00 締切で使える情報の棚卸し

| 時刻 (JST) | 情報 | 09:00前に確定 |
|---|---|---|
| 前日 15:00 | 東証 現物 大引け (日経終値・出来高) | ✓ |
| 前日 15:30〜 | 日経VI、投資部門別売買状況 (木曜)、裁定残 | ✓ (公表ラグを PIT で管理) |
| 前日 17:00 〜 当日 06:00 | 大阪取引所 ナイトセッション 日経先物 | ✓ |
| 当日 05:00〜06:00 | 米国株 終値 (S&P500 / SOX / VIX / 米10年金利) | ✓ |
| 当日 05:15 | CME 日経平均先物 終値 | ✓ |
| 当日 〜08:59 | ドル円 (24h) | ✓ |
| 当日 08:00〜 | 東証 板寄せ 気配値 | ✓ |
| **当日 08:45〜08:59** | **大阪取引所 デイセッション 日経先物** | ✓ ← **始値のほぼ確定推定値** |
| 当日 09:00:00 | 現物 始値 (板寄せ約定) | ✗ 締切と同時刻 |

**用語の訂正: `at_open` ではなく `at_open`。**
日経の現物始値は 09:00:00 の板寄せで確定する。これは「寄り前」ではなく「寄りと同時」。
N の発注も寄り前ではなく、各銘柄の実際の始値を見てから出る (実測: 板検知 09:00:00〜09:00:06、
実約定 09:00:01〜09:00:09、遅寄り銘柄は 09:00:50 や 09:03 以降)。

**したがって「N の発注が 09:00 より後だから日経始値を使える」とは限らない。**
使ってよいのは、以下の順序がミリ秒で実測されている場合に限る:

```
日経始値の受信  →  予測計算の完了  →  N の最初の send_sell 送信
```

逆順なら未来情報。この 3 つのタイムスタンプを記録して確認するのが、
モデルの精度検証より先に片付けるべき前提条件。

**待機コストの非対称性 (見落としやすい):**
日経始値を待って予測するなら、**veto するかどうかに関わらず毎日待つ**。
つまり待機コストは「建てた日」全部にかかり、休んだ日にはかからない。
警報率 10% なら「1 割の日を避けるために 9 割の日で待機コストを払う」。
N は寄り後 6 秒で既に大きく動くため、**予測利益より待機損失が大きくなりうる**。

`n225_veto.py --wait-cost X` で差し引けるほか、
**損益分岐待機コスト** (これ以下なら元が取れる 1 日あたりの額) を判定6で出す。
N の実測 (寄り後 N 秒での価格変化) がこれを下回らない限り採用できない。

なお yfinance の日足では 08:59 の先物スナップショットが取れないため、
本番投入前には `kabu_token.py` 経由の kabu station API で 08:59 の日経先物 /
板寄せ気配を実取得して比較しておくとよい。始値を待たずに済むなら待機コストがゼロになる。

### 10.3 判定シーケンス — 精度ではなくリスク指標、対照群は 2 つ

全体的中率は判定材料にならない。必要なのは**上昇警報の precision** と、
それが N の円建てリスク指標を実際に改善するかどうか。以下の順で潰す。

| # | 判定 | 合格条件 | 実装 |
|---|---|---|---|
| 1 | D10 の上昇警報 precision | 目標値以上 (既定 62.6%) | `--precision-target` |
| 2 | 警報日の N 損益合計 | マイナス、かつ同数ランダム抽出より有意に悪い | 非復元抽出の正規近似 |
| 3 | ランダム休場との比較 | p < 0.05 | ブートストラップ |
| 4 | **一律縮小との比較** | 月平均/σ で上回る | 解析的 |
| 5 | 月ブロック帰無較正 | p < 0.05 | ブロック並べ替え |
| 6 | 待機コスト | 損益分岐待機コスト > 実測待機コスト | `--wait-cost` |

**対照群 1: ランダム休場。**
何日か休めばσも最大DDも機械的に下がる。だから「veto なし vs あり」の比較は、
**予測力がゼロでもほぼ必ず改善して見える**。シグナルを一切含まない合成データでも
Sharpe 0.26 → 0.38、最大DD −26.3 → −21.8 になる。同じ日数をランダムに休んだ
分布と比べて初めて意味がある (そこでの p 値は 0.08〜0.34 で、正しく「差なし」と出る)。

**対照群 2: 一律縮小 (より鋭い)。**
全日を (1−q) 倍にすると、平均もσも同率で縮むので
**月平均÷σ と Sharpe は数学的に一切変わらない**。
したがって選択的 veto がこの 2 指標で一律縮小を上回ったら、
その改善は純粋に「日を選べている」ことに由来する。これが最も明快な選択性のテスト。

**CVaR と 最大DD の比較には注意。**
この 2 つは露出に比例して縮むので、生の値で比べると一律縮小が自動的に有利になる。
月平均で割ってスケール不変にしてから比較すること (`cvar_per_mean`)。

**多重性の留保。**
Bonferroni 0.0125 が正しいのは、事前に比較したものが本当に 4 本だけの場合。
特徴量・モデル・期間・閾値・目的変数を多数試してから 4 本に絞ったなら、
探索全体の多重性は残る。判定5 の月ブロック帰無較正は月内の自己相関を保ったまま
スコア↔損益の対応だけを壊すのでその一部を較正できるが、**最終的には
モデルと閾値を固定した未使用期間での確認が必要** (`--holdout-start`)。

### 10.4 「全体の的中率」より達成しやすい理由

日中方向の全日的中は最難関 (§5 の検出力より、53% を検出するのに 8.9 年)。
しかし **N が必要とするのはそれではない**:

- 全日を当てる必要はない。**上位 10% の日で日経日中リターンの平均が有意に高い**だけでよい
- 外す方向も非対称。「上がる日を休む」で取りこぼす利益 < 「上がる日に建てる」で被る損失、
  という関係が成り立てば、的中率 50% 台前半でもリスク指標は改善しうる
- したがって主要な一次判定は **デシル分析** (スコア上位デシルほど日経日中リターンが高いか)
  であって、的中率ではない。`n225_veto.py` はこれを最初に出す

### 10.5 実行手順

```
# Step 1: パネル構築 (10-15年)
python n225_research.py --build --years 15

# Step 2: そもそも日中方向に信号があるか (下限と上限) + エッジの出所
python n225_research.py --eval --target day --asof strict
python n225_research.py --eval --target day --asof at_open --coef

# Step 3: N の実損益で判定シーケンスを回す [本命]
python n225_veto.py --npnl n_pnl.csv --asof at_open
python n225_veto.py --npnl n_pnl.csv --asof strict          # ギャップ抜きの下限

# Step 4: 処置の比較 (停止 / 縮小 / 条件付きヘッジ)
python n225_veto.py --npnl n_pnl.csv --mode shrink --veto-size 0.5
python n225_veto.py --npnl n_pnl.csv --mode hedge           # 警報日だけ日経でヘッジ

# Step 5: 待機コストを入れる (N の実測値を入れる)
python n225_veto.py --npnl n_pnl.csv --wait-cost 300

# Step 6: 未使用期間での最終確認 (モデルと閾値を固定してから)
python n225_veto.py --npnl n_pnl.csv --holdout-start 2024-01-01
```

`n_pnl.csv` の形式 (先頭2列を日付・損益として読む):

```
date,pnl
2024-01-04,-12500
2024-01-05,8300
```

**実損益が無い場合**は R²=0.235 を再現した代理損益で走るが、これは
「日経日中との連動部分」しか持たない人工系列で、N の銘柄選択・約定・建玉数の
効果を含まない。**配管とロジックの確認用**であって、運用判断の根拠にはならない。

### 10.5.1 条件付きヘッジという第三の選択肢

無条件ヘッジはコストで割に合わないが、**警報日だけ**なら費用が大幅に下がる。
`--mode hedge` は、警報日に N を建てたまま日経をロングして指数成分だけを相殺する。

- ヘッジ数量は **t−1 までのデータで推定したローリングベータ** (500日窓)。リークなし
- コストは `--hedge-cost-bps` (既定 片道 5bps。指数先物/ETF は個別株より安い)
- 停止と違い**機会損失が出ない**のが利点。N の銘柄選択由来の利益は残る
- 一方、ヘッジ自体のコストと、ベータ推定誤差ぶんのリスクが乗る

停止 / 縮小 / ヘッジの 3 つを同じ判定シーケンスに通して比べるのが正しい。

### 10.6 期待値の見立て (正直なところ)

- **`--asof strict` (ギャップなし) で有意が出る可能性は低い**と見ている。
  前日大引けから翌朝までの情報だけで日中方向を当てるのは、文献的に最も薄い領域。
- **`--asof at_open` (ギャップあり) の方が見込みがある**。
  「大きく窓を開けて寄った後、その日どう動くか」は、
  ギャップの大きさ・直前ボラ・米国の動きとの整合性で条件付けられる余地がある。
  そしてこれは N の建玉対象 (ギャップアップ銘柄) と同じ現象を指数側で見ているので、
  構造的に関係があって当然。
- ただし **`at_open` が効いた場合こそ §10.2 の 4 (実先物データでの再検証) が必須**。
  現物始値を代理に使った近似が効果を過大評価している可能性が最も高いのがこのケース。
- 有意が出なかった場合の代替案:
  1. **N 側の銘柄選別を厳しくする** (指数予測ではなく個別のギャップ質で絞る)
  2. **ボラレジームでサイズを調整** (§2.4。日経日中方向より遥かに予測可能)
  3. **損切りルールの見直し** — CVaR / DD の改善が目的なら、
     予測より損切りの方が確実で自由度も低い

3 は身も蓋もないが、「予測が要らない解決策」を先に潰しておくのは重要。
veto が有意にならなかったときの本命はここ。

### 10.7 落とし穴 (N 固有)

- **休場による機会損失**: veto 率 30% は稼働日が 3 割減る。月平均が下がるなら
  CVaR/DD が改善しても総合的に劣る。主指標は運用側の指定通り **月平均÷σ** で見る。
- **N の損失要因は日経だけではない**: R²=0.235 は、**損益変動の 76.5% が日経以外**という意味。
  日経 veto で消せるのは原理的に最大でもその 23.5% ぶん。過度な期待は禁物。
- **レジーム変化**: walk-forward で学習しているが、日経は 2023 年以降レジームが変わっている
  (3 年連続 +20% 超)。OOS 期間を年別に分割して一貫性を確認すること。
- **多重検定**: veto 率 × 指標 × asof × strong-pct × veto-size を全部試すと数十通り。
  最良の 1 つを採用するのは過学習。**事前に主指標と veto 率を 1 つ決めてから走らせる**のが正しい。
- **紙トレードでの前進検証**: 有意が出ても、`forward_test.py` と同じ枠組みで
  最低 3 ヶ月は「veto 判定だけ記録して実際には止めない」期間を置くこと。

---

## 11. 参考文献

**予測可能性の現実:**
- [A Statistical-Finance Benchmark for Same-Day Directional Stock Prediction: Walk-Forward Evidence from SPY](https://arxiv.org/html/2608.26106)
- [Market participants or the random walk – who forecasts better?](https://www.sciencedirect.com/science/article/pii/S1544612323001253)
- [Predicting the unpredictable: New experimental evidence on forecasting random walks](https://www.sciencedirect.com/science/article/abs/pii/S0165188922002743)
- [Forecasting stock indices: a comparison of classification and level estimation models](https://www.sciencedirect.com/science/article/abs/pii/S0169207099000485)

**モデル比較:**
- [Pretrained Time-Series Foundation Models for Financial Return Forecasting (2026)](https://arxiv.org/abs/2606.27100)
- [Re(Visiting) Time Series Foundation Models in Finance](https://arxiv.org/html/2511.18578v1)
- [Predicting the direction of stock market prices using random forest](https://arxiv.org/pdf/1605.00003)

**日米リードラグ / オーバーナイト:**
- [How the prior day's S&P 500 returns influence the intraday returns of Nikkei 225 futures (2026)](https://www.sciencedirect.com/science/article/pii/S3050700626000204)
- [The relationship between daily U.S. and Japanese equity prices: spot vs futures](https://www.sciencedirect.com/science/article/abs/pii/0378426694000190)
- [When "overnight" is not simultaneous: forecasts of morning gaps](https://www.sciencedirect.com/science/article/pii/S2214845026000918)
- [Does Overnight News Explain Overnight Returns?](https://arxiv.org/pdf/2507.04481)

**ボラティリティ:**
- [Improving volatility forecasts of the Nikkei 225 using realized EGARCH](https://arxiv.org/abs/2502.02695)
- [A Practical Guide to harnessing the HAR volatility model](https://www.sciencedirect.com/science/article/abs/pii/S0378426621002417)
- [Realized Volatility: Survey with Application to Nikkei 225 Stock Index (一橋 経済研究)](https://econ-review.ier.hit-u.ac.jp/all-issues/013322.html)

**センチメント / LLM:**
- [Enhancing the Accuracy of Nikkei 225 Forecasting Via LLM-Based Filtering of Noisy Social Media Data (2026)](https://dl.acm.org/doi/full/10.1145/3803291.3803359)

**需給・市場構造 (日本):**
- [JPX 投資部門別売買状況](https://www.jpx.co.jp/markets/statistics-equities/investor-type/04.html)
- [JPX ナイト・セッション 取引時間](https://www.jpx.co.jp/derivatives/rules/trading-hours/01.html)
- [三井住友DS: 海外投資家と個人投資家の日本株売買状況](https://www.smd-am.co.jp/market/ichikawa/2025/05/irepo250527/)
- [楽天証券トウシル: 裁定買い残の読み方](https://media.rakuten-sec.net/articles/-/50283)
- [マネクリ: VIX は「水準」だけでなく「変化」も使う](https://media.monex.co.jp/articles/-/28994)

**検証手法:**
- [The Deflated Sharpe Ratio (Bailey & López de Prado)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [Backtest overfitting in the machine learning era: comparison of out-of-sample testing methods](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)

**季節性 (参考・非採用):**
- [The Nikkei 225's Hidden Clock](https://www.mql5.com/en/blogs/post/773070)
- [NIKKEI 225 Index Seasonal Chart (Equity Clock)](https://equityclock.com/charts/nikkei-225-index-n225-seasonal-chart/)
