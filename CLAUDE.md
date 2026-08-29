# gobot 逆指値シグナル運用メモ

このドキュメントは Claude Code が gobot リポジトリを扱う際の前提知識です。
主軸は **`run_signals.py` (逆指値シグナル統合レポート)** の運用・改修です。
今後の修正は原則このファイルを起点に考えてください。

---

## 1. エントリーポイント

**`run_signals.py`** = 日々の運用コマンド。以下を1コマンドで実行します。

```
python run_signals.py                    # 全期間(365日) HTMLレポート
python run_signals.py --days 90          # 直近90日
python run_signals.py --date 2026-04-08  # 任意日シグナル確認
python run_signals.py --signal-only      # HTML生成せずシグナルだけ表示
python run_signals.py --no-browser       # HTML生成のみ（ブラウザ自動起動なし）
python run_signals.py --workers 8        # 並列数
```

内部で `check_signals_stop` (逆指値B: MACD/A7/RSI2) と `check_signals_breakout`
(ブレイクアウト: DON/VOL/MOM) の2グループを **ThreadPoolExecutor で並列実行**
(`run_signals.py:142-146`) し、タブ付き統合HTMLを `signals_combined_<date>.html`
に出力します。シグナル行は両グループ横断で `calc_recommend_score` 降順ソート
(`run_signals.py:154-164`)。

---

## 2. 依存ツリー

```
run_signals.py
├── check_signals_stop.py      (逆指値B  MACD / A7 / RSI2)
│     └── backtest_limit_entry.run_limit_backtest(entry_type="stop")
├── check_signals_breakout.py  (ブレイクアウト DON / VOL / MOM)
│     ├── scan_breakout_entry.calc_donchian / calc_vol_breakout / calc_momentum
│     └── backtest_limit_entry.run_limit_backtest(entry_type="stop")
└── backtest_limit_entry.py    (共通バックテストエンジン)
```

`run_limit_backtest` は **唯一の逆指値約定ロジック**。分岐は
`backtest_limit_entry.py:339-340` と `:433-438`。ここを触ると全スクリプトに影響します。

関連スキャン (普段は走らせない):
- `scan_entry_compare.py` — 1800銘柄を A(指値) / B(逆指値) / C(逆指値+ATR×0.1) 3パターンで比較
- `scan_breakout_entry.py` — ブレイクアウト3戦略のユニバーススキャン
- `compare_entry_mult.py` — 24銘柄で entry_atr_mult を細かく比較

---

## 3. 逆指値の数式と意味

シグナル前日終値 `close_prev` と前日ATR `atr_prev` から注文価格を計算します。

```
逆指値注文価格  order_p = close_prev + atr_prev * entry_atr_mult
損切り価格      stop    = order_p   - atr_prev * stop_atr_mult
目標価格        target  = order_p   + atr_prev * target_atr_mult
```

- `entry_atr_mult = 0.0` → **終値ちょうど** を逆指値に設定（= 翌日 高値 ≥ 前日終値 で約定）
- `entry_type="stop"` 時の約定条件: `hi >= order_p` (`backtest_limit_entry.py:339`)
- `entry_type="limit"` 時の約定条件: `lo <= order_p` (指値＝押し目買い、逆指値版と対比用)

定義箇所:
- `backtest_limit_entry.py:433-441` (バックテスト側)
- `check_signals_stop.py:144-146`, `check_signals_breakout.py` の `check_signal_on_date` (シグナル確認側)

**同名の `check_signal_on_date` が stop / breakout 双方に存在し、本体ロジックはほぼ同一**。
数式変更時は両方触る必要があります（いずれ共通化の余地あり）。

---

## 4. 戦略パラメータ (em / sm / tm)

`STRATEGY_PARAMS = { "戦略": (calc_fn, entry_atr_mult, stop_atr_mult, target_atr_mult) }`

### 逆指値B (`check_signals_stop.py:75-80`)

| 戦略 | em  | sm  | tm  | 備考 |
|------|-----|-----|-----|------|
| MACD | 0.0 | 1.5 | 3.0 | MACD(8,17,5) + 出来高スパイク + MA10 |
| A7   | 0.0 | 1.5 | 3.0 | ストキャス(14,3,3) + ATR + MA75 |
| RSI2 | 0.0 | 2.0 | 4.0 | RSI(2) + MA200 + IBS<0.35。**指値版は0.5だが stopは0.0に統一** |

### ブレイクアウト (`check_signals_breakout.py:76-80`)

| 戦略 | em  | sm  | tm  | 条件 (緩和パラメータ) |
|------|-----|-----|-----|--------|
| DON  | 0.0 | 1.5 | 3.0 | 終値 > 15日高値(前日) かつ 終値 > MA50 |
| VOL  | 0.0 | 1.5 | 3.0 | 終値 > 5日高値(前日) かつ 出来高 > 20日平均×1.5 |
| MOM  | 0.0 | 1.5 | 3.0 | ROC(10)>3% かつ MA25 > MA75 |

**全戦略が em=0.0 (終値ちょうどで逆指値)** で統一されています。
数値を変更する時は `run_limit_backtest` を再走させて WATCHLIST も更新する運用。

---

## 5. 共通定数 (backtest_limit_entry.py:47-70)

```
ENTRY_EXPIRE      = 3         # 指値/逆指値の有効日数 (シグナル発生から3営業日)
MAX_HOLD          = 15        # 最大保有日数 (超えたら "タイムカット" で終値決済)
FIXED_QTY         = 100       # 常に100株固定 (取引数量)
INITIAL_CASH      = 500_000
POSITION_SIZE     = 100_000
BACKTEST_DAYS     = 365
WORKERS           = 4         # run_signals.py の --workers デフォルト
MIN_PRICE         = 100.0
MAX_PRICE         = 100_000.0
MAX_ATR_RATIO     = 0.20      # ATR/終値 > 20% の異常ボラ銘柄は除外

# 実運用コストモデル (§14 参照)
SLIPPAGE_STOP_PCT = 0.005     # 逆指値買い +0.5% / 損切り売り -0.5% の不利約定
SLIPPAGE_LIMIT_PCT = 0.0      # 指値は注文価格ちょうどで約定
FEE_PCT_ONE_WAY   = 0.001     # 手数料 片道 0.1% → 往復 0.2%
```

状態機械は `idle → pending → in_pos → idle` (`backtest_limit_entry.py:306-415`)。
**約定と同日に決済が走るケース** (`:349-376`) は、hi/lo が両方ヒットしたら
"目標達成優先"。同日決済を無視すると検証結果と運用結果がズレるので
修正時は注意。

---

## 6. シグナル判定日のセマンティクス (過去にハマった)

- **引け後運用**: シグナル判定は当日終値で行う（寄り付き前の判定はしない）
- `target_date is None` (本日モード) のとき: `prev_idx = -1`、**最新足の `entry_sig` を見る**
  (`check_signals_stop.py:123-124`)
- `--date YYYY-MM-DD` 指定時: その日そのものを判定日とする (`prev_idx` = 指定日の行)
  (`check_signals_stop.py:125-131`)
- **過去の修正履歴 (重要)**:
  - `1182492` シグナル日をシグナル確認日と一致 (`prev_idx -2 → -1`)
  - `e7d8687` `--date` 指定時のシグナル判定を指定日の**終値**に修正
  - `300b166` `--date` 指定時はアクティブシグナルを無視
  - `b02a36a` 引け後運用に変更 (当日終値で判定)

この辺を再度触るときは上記コミットを必ず読んでから変更してください。

### 削除済み機能 (復活させない)

- `cb7d97b` **保有中追跡機能は削除** (シグナル不具合のため revert 済み)。
  「未約定シグナルを約定まで表示し続ける機能」や「保有中シグナルを損切り・利確・
  タイムアウトまで追跡する機能」は過去に入れて戻しています。再導入する場合は
  `4858cdf` / `97ae745` を参考にしつつ、シグナル判定との整合に注意。

---

## 7. おすすめスコア (calc_recommend_score)

`check_signals_stop.py:83-108` と `check_signals_breakout.py:84-109` に同一実装。
統合レポートでは両者をまぜてソートします (`run_signals.py:154-164`)。

```
score = round(
    avg_wr * 0.4                                     # 勝率      最大 40点
  + (avg_pf / 10) * 30                               # PF        最大 30点 (PF=10 キャップ、∞は10扱い)
  + stable * 20                                      # 期間安定性 最大 20点 (プラス期間/有効期間)
  + min(t_trades / 20, 1) * 10                       # 取引回数  最大 10点 (20取引で満点)
)
rank = ★★★≥80, ★★≥60, ★≥40, △<40
```

スコアは **最低1取引ある期間のみで平均** (trades == 0 の期間は除外)。
PF=∞ (損失トレード無し) は 10 として扱う、ここを変えると上位ランキングが
大きく動くので注意。

---

## 8. WATCHLIST のソース

両方ともスキャン結果をハードコードしているので、**再スキャンしない限り WATCHLIST
は更新されません**。

- `check_signals_stop.WATCHLIST` (31銘柄): `scan_entry_compare.py` パターンB
  の `--max-price 5000` スキャン上位。MACD / A7 / RSI2 それぞれで複数期間安定銘柄。
- `check_signals_breakout.WATCHLIST` (24銘柄): `scan_breakout_entry.py` 緩和
  パラメータ (DON=15日 / VOL=5日×1.5 / MOM=ROC>3%) のスキャン上位。

WATCHLIST を更新するときは:
1. `python scan_entry_compare.py --max-price 5000` などを走らせる
2. 生成HTMLから成績上位を拾う
3. それぞれの `WATCHLIST` 定数を書き換え
4. `python run_signals.py --days 365` で全期間再計算して確認

---

## 9. HTML 生成の流れ

1. `check_signals_stop.build_html(stop_items, show_days, date_label)` → フル HTML 文字列
2. `check_signals_breakout.build_html(...)` → フル HTML 文字列
3. `run_signals.build_combined_html(stop_html, brk_html)` (`run_signals.py:66-112`)
   - 各HTMLから `<style>` と `<body>` を正規表現で抜き出す (`_extract_style` / `_extract_body`)
   - ブレイクアウト固有の `.tag-don` / `.tag-vol` / `.tag-mom` CSSだけ追加
   - タブUI (`.tab-nav` / `.tab-pane` / `switchTab()`) を付けて結合

タブCSSを壊すと双方の表で色やタグが崩れるので、修正するときは両側のHTMLを開いて
目視確認するのが早い。

---

## 10. 今後の改修で想定される落とし穴・TODO

### すでに判明している共通化候補

1. **`check_signal_on_date` が stop / breakout でほぼコピペ**
   → 共通モジュール (例: `_signal_common.py`) に切り出すと両方から使える
2. **`calc_recommend_score` も stop / breakout で同一**
   → 同上
3. **逆指値価格計算式 `order_p = close + atr * em` が3箇所に分散**
   - `backtest_limit_entry.py:435`
   - `check_signals_stop.py:144`
   - `check_signals_breakout.py:145`
   → ヘルパー関数化すると em のチューニングが1箇所で済む

推定で約250行削減可能。ただし `run_signals.py` は両モジュールを
`import check_signals_stop as _stop / check_signals_breakout as _brk` で
直接参照しているので、共通化する場合はこの import 面も調整が必要
(`run_signals.py:26-27`)。

### 注意すべき副作用

- `FIXED_QTY = 100` 固定なので、コスト計算や総損益の単位は「100株 × 値差」。
  可変数量化する場合は `backtest_limit_entry.py` 全体に波及します。
- `run_signals.py` は `_stop._DEFAULT_WORKERS` を workers のデフォルトに使う
  (`run_signals.py:124`)。`check_signals_stop.py` が `WORKERS as _DEFAULT_WORKERS`
  を re-export している前提なので、後者の export を外すと壊れます。
- タブHTMLの `_extract_style` は `<style>...</style>` を1個目しか拾わない。
  将来 build_html 側で `<style>` を複数出すようになった場合は書き直す必要あり。

### やらない方が良いこと (過去の失敗)

- 保有中ポジションの追跡機能の再導入 (§6 参照)
- シグナル判定日を "翌営業日" に戻すこと (`1182492` で -2 → -1 に直した)
- `--date` 指定時に「アクティブシグナルを混在」させること (`300b166` で revert 済み)
- **現 WATCHLIST の選定方法 (同一期間でスキャン→再バックテスト)** をそのまま
  使い続けること。これは in-sample bias で勝率/PF が異常に高く出る。
  再選定するなら **Walk-forward (§13)** を使う。

---

## 11. 開発ブランチ

固定作業ブランチ:
- `claude/kabu-station-token-retrieval-hJZ29` (現行セッション)

`main` ブランチは本リポジトリには存在しないので、差分比較するときは
`git log <branch> --oneline` で上から辿ってください。

---

## 12. 参考: kabu station (別件)

`kabu_token.py` は kabuステーション REST API トークン取得用のスタンドアロン
スクリプト。現状 `run_signals.py` とは **未連携**。将来、逆指値シグナルを
そのまま kabu API に流し込む場合は、`check_signal_on_date` の返り値
(`order_price` / `stop_price` / `target_price`) をそのまま `sendorder` に渡せる
設計になっていることを押さえておいてください。接続先は デモ `18081` / 本番 `18080`。

---

## 13. Walk-forward 銘柄選定 (新パイプライン)

既存の「スキャン→同期間再バックテスト」が持つ **in-sample bias** を排除するため、
時期をずらした Walk-forward 方式で WATCHLIST を再選定する 3 スクリプトを追加しました。
**今後 WATCHLIST を差し替えるときは必ずこのパイプラインを使ってください。**

### 13.1 ファイル構成

| ファイル | 役割 |
|---|---|
| `risk_metrics.py` | MaxDD / 最大連敗 / Sharpe / 資産曲線 / リカバリーファクター |
| `scan_walkforward.py` | ユニバース (~1800銘柄) × 6戦略 × 3 fold の Walk-forward バックテスト → CSV |
| `build_watchlist.py` | CSV を読んでフィルター・ランキング → WATCHLIST 提案の Python コード |
| `verify_watchlist.py` | 提案ファイルで check_signals_stop/breakout を monkey-patch 実行 → 検証 HTML 自動表示 |

### 13.2 Walk-forward の fold 設計 (`scan_walkforward.FOLDS`)

```
Fold 1: TRAIN 730〜540日前  /  TEST 540〜360日前
Fold 2: TRAIN 540〜360日前  /  TEST 360〜180日前
Fold 3: TRAIN 360〜180日前  /  TEST 180〜  0日前
```

- TRAIN と TEST は非重複
- TRAIN (選定) は「そのパターンで銘柄を選ぶ期間」、TEST は「擬似的な未来データ」
- 3 fold 全てで TRAIN+TEST を通過した銘柄 = **時期依存しない本物の候補**

### 13.3 合格条件 (`scan_walkforward.py:65-70`)

| 区分 | trades | PF | win_rate | total_pnl |
|------|--------|-----|----------|-----------|
| TRAIN | ≥3 | ≥1.5 | ≥55% | >0 |
| TEST  | ≥2 | ≥1.2 | ≥45% | >0 |

閾値を弄るときはここを直接編集するか、引数化してください。

### 13.4 実装のキモ: df トリミングで _TODAY を触らない

`run_limit_backtest` は内部で `_TODAY - backtest_days` を cutoff に使う前提設計。
Walk-forward ではウィンドウごとに異なる start/end が必要ですが、`_TODAY` の
monkey-patch はスレッド間で競合するので、代わりに:

1. `full_df[full_df.index <= window_end]` で df 後方を事前トリミング
2. `backtest_days = (_TODAY - window_start).days` で start を指定

これで `_TODAY` を触らずに任意ウィンドウを切り出せます。ThreadPoolExecutor で
並列実行しても安全です (`scan_walkforward._run_window` 参照)。

### 13.4.5 予算フィルター (--budget / --max-price)

`FIXED_QTY=100` 株固定のため、「100株買える株価の銘柄のみ」をスキャン対象に
絞り込める `--budget` / `--max-price` オプションを両スクリプトに用意しています。

- `scan_walkforward.py --budget 600000` → `latest_price > 6000` の銘柄はスキャンせずスキップ
  (未スキャンなので計算コストも削減)
- `build_watchlist.py --budget 600000` → 既存 CSV を事後フィルター (再スキャン不要)

`latest_price` は `scan_walkforward.walkforward_one()` で
`full_df.iloc[-1]["close"]` として取得され、**予算指定が無くても** CSV に
含まれます (`walkforward_results/*.csv` の `latest_price` カラム)。
古い CSV に `latest_price` が無い場合、`build_watchlist.py --budget` は
そのフィルターをスキップします (後方互換)。

換算式: `effective_max_price = budget / 100` (FIXED_QTY=100 株想定)。
もし 1 回の発注で使う株数を将来変えるなら、ここを書き換える必要があります。

**注意点**:
- フィルターは **最新終値** (データの最終日) で判定します。過去の高値ではありません。
- 過去には手の届いた (例: シグナル発生日は 5,800円) が、直近の上昇で手の届かない
  (8,000円) 銘柄は除外されます。これは「今日から運用を始める」という前提では正しい挙動です。
- 予算ギリギリの銘柄を選ぶと、1 回のトレードでほぼ全資金を投入することになります。
  複数ポジション同時保有したい場合は `--budget (全額 / 希望同時保有数)` を指定してください
  (例: 60万円で 3 銘柄持ちたい → `--budget 200000`)。

### 13.5 実行手順

```
# Step 0 (初回のみ): ユニバース (銘柄リスト) を生成
python fetch_listed_symbols.py --market prime   # プライム ~1800銘柄 (推奨)
python fetch_listed_symbols.py --market all     # 全市場 ~4000銘柄
# → symbols_listed_prime.py / symbols_listed_all.py が生成される

# Step 1: Walk-forward スキャン (ユニバース × 6戦略 × 3 fold)
python scan_walkforward.py                      # 自動検出: prime > all > 225
python scan_walkforward.py --family stop        # 逆指値Bのみ
python scan_walkforward.py --family breakout    # ブレイクアウトのみ
python scan_walkforward.py --workers 8          # 並列数
python scan_walkforward.py --symbols symbols_listed_all.py   # 明示指定
python scan_walkforward.py --limit 50           # 先頭50銘柄だけ (デバッグ)
python scan_walkforward.py --budget 600000      # 60万円で100株買える銘柄のみ
python scan_walkforward.py --max-price 6000     # 株価6000円以下 (--budget と同義)
# → walkforward_results/walkforward_<STRATEGY>_<date>.csv  が出力される
# → CSV には latest_price カラムが含まれる (--budget 無しでも計測される)

# Step 2: CSV から WATCHLIST 提案を生成
python build_watchlist.py                       # 戦略あたり10銘柄
python build_watchlist.py --per-strategy 8      # 戦略あたり8銘柄
python build_watchlist.py --max-dd 10           # MaxDD上限10%
python build_watchlist.py --min-sharpe 0.3      # Sharpe下限0.3
python build_watchlist.py --budget 600000       # 60万円で100株買える銘柄のみ (事後フィルター)
# → watchlist_proposal_<date>.py  が出力される

# Step 3: 提案 WATCHLIST で検証バックテスト (HTML 自動オープン)
python verify_watchlist.py                      # 最新の提案ファイルを自動検出
python verify_watchlist.py --days 180           # 期間指定
python verify_watchlist.py --stop-only          # 逆指値Bのみ
python verify_watchlist.py --no-browser         # ブラウザ起動しない
# → signals_verification_<date>.html  が出力・オープン
# 既存コードは一切変更せず、ランタイムで _stop.WATCHLIST / _brk.WATCHLIST を
# monkey-patch して run_signals のバックテスト経路を再利用する。

# Step 4: 結果に納得したら手動で差し替え
# watchlist_proposal_<date>.py の STOP_WATCHLIST / BRK_WATCHLIST を
# check_signals_stop.py / check_signals_breakout.py の WATCHLIST に貼り付け

# Step 5: 旧 vs 新の比較 (必要なら)
python run_signals.py --days 365                # 差し替え後で最終確認
```

### 13.6 WATCHLIST を更新する際のチェックリスト

1. `scan_walkforward.py` を実行 → 全 6 戦略の CSV が出ているか
2. `build_watchlist.py` を実行 → `watchlist_proposal_<date>.py` を確認
3. 提案の中に **folds_passed=3 の銘柄** が最低でも数銘柄あるか?
   (ゼロの場合、閾値が厳しすぎる or 相場環境が異常)
4. `max_drawdown_pct` が全銘柄 15% 以下に収まっているか
5. `train_to_test_degradation_pct` が極端に高い (>80%) 銘柄は除外推奨
   → overfit の兆候
6. 新 WATCHLIST を `check_signals_stop.py` / `check_signals_breakout.py` に貼り付け
7. `python run_signals.py --days 365` で旧 WATCHLIST と成績を比較
8. 勝率/PF が現実的な値 (50-65% / 1.2-2.0) に落ち着いたら成功

### 13.7 WATCHLIST の「世代管理」

スキャン結果 CSV は日付付きなので、過去の選定結果を残せます。

```
walkforward_results/
  walkforward_MACD_2026-04-11.csv
  walkforward_MACD_2026-07-11.csv   ← 3ヶ月後の再選定
  ...
watchlist_proposal_2026-04-11.py
watchlist_proposal_2026-07-11.py    ← 世代比較可能
```

「前回選定銘柄が次のスキャンでも生き残るか」を見ることで、選定の再現性を
継続的に検証できます。理想的には **3ヶ月ごと** に再実行するのがおすすめ。

### 13.8 既知の限界

- **銘柄ユニバース**: `scan_walkforward.load_universe()` が以下の順で自動検出:
  1. `symbols_listed_prime.py`    (プライム ~1800銘柄) ← **推奨**
  2. `symbols_listed_all.py`      (全上場 ~4000銘柄)
  3. `symbols_listed_standard.py` (プライム+スタンダード)
  4. `symbols_all.py`             (日経225, 225銘柄) ← フォールバック

  `symbols_listed_*.py` は `fetch_listed_symbols.py` で生成。JPX の `data_j.xls`
  をダウンロードしてパースする仕組みで、7日間キャッシュ (`.jpx_listed_cache.pkl`)
  あり。JPX の Excel フォーマット変更時は `fetch_listed_symbols._parse_xls`
  の調整が必要。
- **初回実行時間**: 1800銘柄 × 6戦略 × 3fold × 2(train/test) = 約6.5万回の
  バックテスト。yfinance から各銘柄 800日分の日足をDLするため、初回は
  1〜2時間かかることがある。2回目以降は `.rsi2_cache/` のキャッシュで数分に短縮。
- **yfinance レート制限**: 1800銘柄を一気に叩くとレートリミットに引っかかる可能性。
  その場合は `--limit 500` で分割実行 → キャッシュが貯まってから全量実行を推奨。
- **FIXED_QTY=100 固定**: 銘柄間リスクが不均等。Walk-forward 検証も 100 株固定の
  前提で行うので、高ボラ銘柄が不利。将来的に volatility-parity 化する余地あり。
- **ベンチマーク比較**: §14 で `fetch_n225_return` を追加済み。build_html の
  戦略サマリーに α vs 日経 列が表示されます。
- **セクター制約なし**: 地銀 7-8 銘柄のような集中が起きうる。出力 CSV に
  セクター情報は無いので、目視で確認するか将来的に `yfinance` 等で sector
  を追加する必要がある。
- **データキャッシュ**: `backtest_limit_entry.fetch` は永続キャッシュ
  (`.rsi2_cache/`) を使う。2026-04-16 以降、キャッシュ判定は **「最新バー日付
  >= `_expected_latest_bar_date()`」** に変更。引け前にキャッシュが作られても
  引け後の実行で自動再取得されるため、**手動キャッシュ削除は不要**になった。
  `_expected_latest_bar_date()` は 平日 15:00 JST 以降は今日、それ以前は前営業日、
  週末は金曜を返す (祝日は未考慮)。

---

## 14. 実運用コストモデル & リスク指標 & フォワードテスト

バックテストと実運用の乖離を小さくするため、以下の改善を追加済みです。

### 14.1 スリッページ & 手数料モデル

`backtest_limit_entry.py:66-69` に定数を集中:

```python
SLIPPAGE_STOP_PCT  = 0.005   # 逆指値買い +0.5% / 損切り売り -0.5%
SLIPPAGE_LIMIT_PCT = 0.0
FEE_PCT_ONE_WAY    = 0.001   # 片道 0.1%, 往復 0.2%
```

適用箇所 (`run_limit_backtest`):
- **逆指値買い約定** (`:348-357`): `entry_p = limit_price * (1 + SLIPPAGE_STOP_PCT)`
- **損切り売り約定** (`:376-380`, `:428-430`): `exit_p = stop_price * (1 - SLIPPAGE_STOP_PCT)`
- **目標達成 / タイムカット**: スリッページなし (指値・成行 close で処理)
- **手数料**: 全トレードで `fee = (entry_p + exit_p) * qty * FEE_PCT_ONE_WAY` を pnl から差し引き

このモデル変更は **`run_signals.py` / `verify_watchlist.py` / `scan_walkforward.py`**
すべてに同時適用されます (モジュール定数変更だけ)。
変更後は過去の CSV / HTML の数字と **直接比較できない** 点に注意。
必要なら `scan_walkforward.py` を再実行して新しい CSV を得てください。

パラメータを変えたい場合は該当定数を編集するだけ。例: 信用取引で手数料 0.05% なら
`FEE_PCT_ONE_WAY = 0.0005` に。

### 14.2 HTML にリスク指標 & ベンチマーク α を表示

`check_signals_stop.py:181-` と `check_signals_breakout.py:186-` の `build_html`
の戦略サマリーテーブルに以下の 4 列を追加:

| 列 | 計算元 |
|---|---|
| **MaxDD%** | `risk_metrics.calc_max_drawdown` (戦略別の結合 trade_log から) |
| **連敗** | `risk_metrics.calc_max_consecutive_losses` |
| **Sharpe** | `risk_metrics.calc_sharpe` (年率換算, 1年=20取引想定) |
| **α vs 日経** | `(戦略リターン% / INITIAL_CASH) - 日経平均リターン%` |

サブタイトルに以下を表示:
- スリッページ値 (e.g. 0.50%)
- 手数料値 (e.g. 片道 0.10% / 往復 0.20%)
- 日経平均リターン (e.g. +12.3% over 365日)

日経平均は `backtest_limit_entry.fetch_n225_return(days)` で取得
(プロセス内でキャッシュ)。失敗時は 0.0 を返すので HTML には影響しない。

### 14.3 フォワードテスト (紙トレード記録)

`forward_test.py` が毎日のシグナルを CSV (`forward_test_log.csv`) に蓄積し、
実データで約定・決済を自動判定するスクリプト。

```
python forward_test.py --record            # 毎日引け後に実行
python forward_test.py --record --no-update # 既存評価をスキップ
python forward_test.py --report            # 全期間の集計レポート
python forward_test.py --report --days 30  # 過去30日分のみ
```

**動作フロー**:
1. `collect_today_signals()`: `check_signals_stop` と `check_signals_breakout` の
   全 WATCHLIST × 全戦略で今日のシグナルを `check_signal_on_date(None)` で検出
2. `evaluate_entry()`: 既存の log 行について
   - pending → fetch で最新データ取得 → 高値 ≥ 注文価格なら filled (entry_p に
     スリッページ込み)、3営業日以内に約定しなければ expired
   - filled → 目標/損切り/タイムカット (MAX_HOLD=15日) を判定、決済時の pnl を
     手数料込みで計算
3. 新規シグナルと更新されたログを CSV に保存

**CSV 行の status 遷移**:
```
pending → filled → { target, stop, timeout, holding }
pending → expired (3営業日以内に約定せず)
```

`holding` は「約定したがまだ決済していない」中間状態。次回 `--record` 時に
再評価される。

**レポートの読み方**:
- `fill率` = 約定したシグナル / 全シグナル。実運用でこれが 50% を下回るなら
  `entry_atr_mult` の調整や逆指値→成行切替を検討
- `勝率` = target / (target + stop + timeout)。バックテスト値から
  10〜30% 劣化が目安
- `損益` = 決済済みの合計円 (スリッページ・手数料込み)。プラスなら戦略が機能

運用例:
- Day 1: `python forward_test.py --record` → 今日のシグナル10件記録
- Day 2: `python forward_test.py --record` → 昨日の10件を再評価 (約定7件, 期限切3件)
  + 今日の新規シグナル記録
- Day 30: `python forward_test.py --report --days 30` → 過去30日の集計

バックテストと現実のズレを定量的に測れるので、**運用判断の根拠になる唯一の数字** です。
CLAUDE.md §10 の「やらない方が良いこと (過去の失敗)」を更新する前に、必ずフォワード
テストで裏を取ってから変更することをおすすめします。

### 14.4 注意点

- **フォワードテストは本物の取引ではない**: 実運用と違い、資金制約・保有数制約・
  税金は無視されている。「どの銘柄に入るべきか / 勝率がどう推移するか」の
  参考データとして使ってください。
- **コストモデルは近似**: 実際の手数料は証券会社プランによって大きく異なる。
  信用取引は現物よりかなり安い (0.03%程度)。逆にスリッページは個別銘柄の流動性で
  激しく変わる (出来高の少ない銘柄で -2% とかザラ)。
- **CSV のマージは注意**: `forward_test.py` は重複 (record_date + symbol + strategy)
  を排除するが、手動で CSV を編集するときは注意。最悪、ログを消して `--record`
  しなおせば OK (過去日の遡及記録はできない)。

---

## 15. トレードモード プリセット (conservative / aggressive)

目標倍率 (tm) と損切倍率 (sm) の 2 プリセットを全スクリプトで切替可能。
**デフォルトは conservative** (標準)。回転率優先の積極利確モードを使いたい場合は
`--aggressive` で opt-in。

(過去に aggressive をデフォルト化した経緯あり、2026-04-14 に conservative に戻した。
コミット履歴参照)

### 15.1 プリセット値

| 戦略 | **conservative (デフォルト)** | aggressive (opt-in) | 目標% | 損切% |
|---|---|---|---|---|
| MACD | **0.0, 1.5, 3.0** | 0.0, 1.0, 1.5 | **+9%** / 積極+4.5% | -4.5% / 積極-3% |
| A7   | **0.0, 1.5, 3.0** | 0.0, 1.0, 1.5 | **+9%** / 積極+4.5% | -4.5% / 積極-3% |
| RSI2 | **0.0, 2.0, 4.0** | 0.0, 1.2, 1.8 | **+12%** / 積極+5.4% | -6% / 積極-3.6% |
| DON  | **0.0, 1.5, 3.0** | 0.0, 1.0, 1.5 | **+9%** / 積極+4.5% | -4.5% / 積極-3% |
| VOL  | **0.0, 1.5, 3.0** | 0.0, 1.0, 1.5 | **+9%** / 積極+4.5% | -4.5% / 積極-3% |
| MOM  | **0.0, 1.5, 3.0** | 0.0, 1.0, 1.5 | **+9%** / 積極+4.5% | -4.5% / 積極-3% |

conservative は **2R 設定** (target = 2 × stop)、勝率・PF重視。
aggressive は **1.5R 設定**、回転率重視。
ATR 換算で表記しているので、実効 % は銘柄のボラにより変動します。

### 15.2 切替方法

**デフォルト (conservative) で実行**:
```
python run_signals.py                    # conservative モード (デフォルト)
python verify_watchlist.py               # 同上
python scan_walkforward.py --budget 600000
python forward_test.py --record
```

**積極利確 (aggressive) で実行 (opt-in)**:
```
python run_signals.py --aggressive
python verify_watchlist.py --aggressive
python scan_walkforward.py --aggressive --budget 600000
python forward_test.py --record --aggressive
```

実際の切替: 各スクリプトの **最初の import より前** に `sys.argv` を
チェックして `os.environ["TRADING_MODE"]` を設定する。
その後 `check_signals_stop` / `check_signals_breakout` / `scan_walkforward`
が import されると、モジュールトップで env var を読んで
`STRATEGY_PARAMS` / `STRATEGY_DEFS` をプリセットに差し替える。

**環境変数 (シェル全体で固定)**:
```
export TRADING_MODE=aggressive   # 常に aggressive で固定
python run_signals.py            # --aggressive 不要
```

### 15.3 出力ファイル名の分離

**デフォルト (conservative) は suffix なし**、**aggressive は `_aggressive` suffix**。

| スクリプト | conservative (デフォルト) 出力 | aggressive 出力 |
|---|---|---|
| run_signals.py | `signals_combined_YYYY-MM-DD.html` | `signals_combined_aggressive_YYYY-MM-DD.html` |
| verify_watchlist.py | `signals_verification_YYYY-MM-DD.html` | `signals_verification_aggressive_YYYY-MM-DD.html` |
| scan_walkforward.py | `walkforward_STRATEGY_YYYY-MM-DD.csv` | `walkforward_STRATEGY_aggressive_YYYY-MM-DD.csv` |
| forward_test.py | `forward_test_log.csv` | `forward_test_log_aggressive.csv` |

### 15.4 運用上の注意

- **WATCHLIST は互換**: aggressive モードに切替えても、`check_signals_stop.WATCHLIST` /
  `check_signals_breakout.WATCHLIST` の銘柄リストはそのまま使える。ただし
  **各戦略に最適な銘柄構成はモードごとに違う可能性**がある (aggressive は小さな
  利幅を取るため、よりトレンドが弱くても勝てる銘柄が向く)。
  理想的には `scan_walkforward.py --aggressive` で aggressive 用 WATCHLIST を
  別途作り、提案ファイル → `check_signals_*.py` に貼り付けると良い。
- **バックテスト数字の比較は同モード内で**: conservative と aggressive の
  数字を直接比較すると誤解を生む。例えば aggressive は「勝率高いが総損益は低い」
  ケースが多い。どちらが優れているかは Sharpe 比率や期待値で判断するのが妥当。
- **フォワードテストはモードごとに別ログ**: `forward_test_log.csv` (conservative)
  と `forward_test_log_aggressive.csv` が独立。両方記録したければ 2 回実行:
  ```
  python forward_test.py --record                # conservative
  python forward_test.py --record --aggressive   # aggressive
  ```
- **ENTRY_EXPIRE / MAX_HOLD は共通**: 現状プリセットで変えていない
  (3 営業日有効 / 15 日保有上限)。より回転率を上げたいなら `backtest_limit_entry.py`
  の `MAX_HOLD = 15 → 7`, `ENTRY_EXPIRE = 3 → 2` を検討。こちらはモジュール
  定数なので手動書き換え。

### 15.5 A/B テストの流れ (推奨)

```
# 同じ日に 2 モード実行して数字を比較
python run_signals.py                  # conservative (デフォルト)
python run_signals.py --aggressive     # aggressive

# 並べてブラウザで開き、以下を比較:
#  - 勝率 (aggressive の方が高いはず)
#  - PF (conservative の方が高いはず)
#  - 総損益 (年間で勝つ方を選ぶ)
#  - 平均保有日数 (aggressive は短い)
#  - Sharpe 比率 (リスク調整後)

# フォワードテストも 2 モード並行で記録
python forward_test.py --record                # conservative ログ (デフォルト)
python forward_test.py --record --aggressive   # aggressive ログ

# 1ヶ月後、それぞれレポート
python forward_test.py --report                # conservative 実績
python forward_test.py --report --aggressive   # aggressive 実績
```

実運用のどちらが優秀かは **バックテストではなく フォワードテストの実績** で判断してください。

---

## 16. デイトレ: ギャップ反転の日足検証 (`gap_reversal_daily.py`)

**同日決済（寄りで建てて引けで閉じる）** の仕組みを検討するための検証スクリプト。
逆指値シグナル (§1〜§15) とは独立で、`run_signals.py` 系には一切影響しません。

### 16.1 検証している仮説

> 前日に大きく動いた銘柄が、翌朝さらに同じ方向へギャップして寄ると、その日のうちに戻る。

上げ側にギャップ → 空売り、下げ側 → 買い。始値で建てて引けで決済。

### 16.2 なぜ日足だけで足りるか

このルールの判定に必要なのは `前日終値 → 始値 → 終値` の 3 点だけで、
**日中の価格の到達順序を必要としません**。したがって分足 (直近2年) ではなく
日足 (2007年〜) の全期間で検証できます。分足が要るのは「決済を引けより前に
早めるか」「損切りを置くか」を決めるときだけ。ここを混ぜると、19年ぶんの
証拠を捨てて2年で判断することになります。

### 16.3 叩き台からの3つの変更

1. **残差ギャップ化** — 単純なギャップは市場要因を含む。反転するのは
   個別銘柄が市場から乖離した分だけ。ローリング120日β (t-1 までの情報のみ) で
   `resid_gap = gap - beta * 指数ギャップ` を計算する。
2. **ATR 正規化** — `+1.75%` / `+1.0%` の固定閾値は、日経が2%動いた日に全銘柄が
   引っかかり、凪の日は何も出ない。ATR20 で割ると候補数が安定する。
   固定閾値版は `--raw` で再現可能。
3. **決算日の分離** — 大きく動いた翌日のギャップの相当数は決算。決算ギャップは
   反転せず同方向にドリフトする (PEAD)。`--earnings` で分離して集計する。
   決算グループが逆符号にならないなら、「過剰反応の巻き戻し」という説明自体が誤り。

### 16.4 レポートの読み方 (7セクション)

| # | 内容 | 落第条件 |
|---|---|---|
| 1 | 生リターン と α (同日ユニバース平均を控除) | α が消える = 市場の日中ドリフトを拾っていただけ |
| 2 | トレード単位 t vs 日次集約 t | **同じ日のシグナルは独立ではない**。日次集約の方だけを見る |
| 3 | 年別 α | 特定の年だけで稼いでいる |
| 4 | ギャップ幅の5分位別 α | 単調に増えない = ノイズ |
| 5 | プラセボ (前日の動きなし / 符号反転 / 決算あり) | 予想通りに壊れない |
| 6 | コスト感応度 | エッジ/コスト比 < 3 |
| 7 | 資金制約下 (1日13銘柄上限) と資金換算 | 執行可能な上位だけにすると消える |
| 8 | 裾の依存度 (中央値 / 上位5日の寄与 / 上下1%除去後) | 上位10%の日で総損益の9割 = 少数の極端な日頼み |
| 9 | 方向別 (買い / 空売り) | 空売り側にしかエッジが無い = 制度的制約の照会が必須 |

**採用の目安**: 日次 t > 3 かつ 年別で大半がプラス かつ 単調性あり かつ
エッジ/コスト比 > 3 かつ 裾を落としても t が残る。ここを通って初めて、
分足での執行検証に進む価値がある。

### 16.4.5 閾値グリッド (`--grid`) — 台地か尖りか

閾値を1組だけ試して良い数字が出ても、それは「データを何度も見た結果その組が
当たっただけ」かもしれない (多重比較)。`--grid` は前日の動き × ギャップの
7×7 を総当たりし、各区画の 日次t / グロスα / 件数 を出す。

- **t が高い区画が面で広がっている** → エッジは本物
- **1区画だけ高く周囲が低い** → その閾値は偶然の可能性が高い
- **件数が3桁を切る区画** → t の値によらず信頼できない
- **前日 0.00 の行が下の行と同じ** → 前日の動きという条件は効いていない
- **端の列で単調増加が続く** → 探索範囲が狭い。`--grid-gap` で伸ばす

前日 0.00 (条件なし) の行を含めるのが要点。パネルは `|gap_z| >= 0.3` の行を
必ず保持するので、ギャップ閾値 0.3 以上の区画は前日閾値によらず母集団が完全。
出力は t / グロスα / 件数 の3表で、最後に最良区画とその再現コマンドを出す。

```
python gap_reversal_daily.py --grid
python gap_reversal_daily.py --grid --grid-gap "1,2,3,4,5" --grid-prev "0,1,2"
```

```
python gap_reversal_daily.py --grid
```

グリッド時はパネルを最も緩い閾値 (前日 0.9 ATR) で構築するので、
通常実行よりメモリを使う。

### 16.3.5 ⛔ 始値ノイズによる見せかけのエッジ

`gap = 始値 / 前日終値` と `o2c = 終値 / 始値` は **同じ始値を分子と分母に
共有している** ため、始値の測定ノイズ (板寄せの偏り・気配のバウンス) だけで
機械的に負の相関を持ちます。**ランダムウォークでも必ず出ます。**

実測: 合成ランダムウォーク (エッジ0) で α = +42bp / t = 27。控除すると +2.6bp / t = 1.7。

レポート §1 の 3 行目「α (始値ノイズも控除)」が、`o2c ~ gap` の回帰の傾き分を
差し引いた値です。**この行が消えるなら、それは過剰反応の巻き戻しではなく
微細構造ノイズ**なので、そこで打ち切ってください。

**傾きは |ギャップ| の十分位ごとに推定し、控除には「最小帯域の傾き」を使います
(2026-08 に2度修正)。**

始値に乗算ノイズ ε があると `gap = 真のギャップ + ε` / `o2c = 真の日中変化 - ε`
となり、**|ギャップ| が小さい帯域ではノイズが分散を支配するので傾きは -1 に
近づきます**。逆に大きいギャップの帯域ではノイズの寄与は無視できます。
したがってノイズ由来の成分は最小帯域から推定するのが正しい。

⛔ **シグナル自身の帯域の傾きを引いてはいけません。** それは「反転はギャップに
比例する」という効果そのものの控除になり、比例する本物の効果まで消します。
一度この誤りを犯し、α 56.8bp を 12.8bp まで削って「棄却」と誤読しました。

読み方 (§1 の下に帯域表が出ます):

- **最小の帯域が強く負 (-0.1 以下など)** → 始値ノイズの兆候。3行目が消えたら打ち切り
- **最小の帯域が 0 か正** → 始値ノイズは実質ゼロ。この経路では棄却できない

実測 (東証プライム 1,362銘柄 / 2007-2026):

```
0.00% 〜 0.03%   +1.1120     ← 負でない = ノイズなし
0.03% 〜 0.17%   +0.1310
0.31% 〜 0.46%   -0.0141
0.82% 〜 1.07%   -0.0418
2.04% 以上       -0.0367     ← 効果そのもの
```

日本株の寄り付きは板寄せで単一の約定価格が付くため、始値に気配のバウンスが
乗らないのは自然な結果です。**日本株ではこの経路での棄却は期待できません。**

⛔ この診断はデータ破損があると機能しません。ギャップ 122万% のような行が
説明変数に入ると回帰の傾きが 0 に潰れます。§16.5.5 の整合性チェックを必ず
先に通してください。

### 16.5 使い方

```
python gap_reversal_daily.py --coverage       # データの被覆状況 + 年別の銘柄日数
python gap_reversal_daily.py --grid           # 閾値グリッド (台地か尖りか)
python gap_reversal_daily.py                  # .rsi2_cache/ を自動検出して実行
python gap_reversal_daily.py --self-test      # 合成データで配管を確認 (要ネットワーク無し)
python gap_reversal_daily.py --limit 200      # 200銘柄で試す
python gap_reversal_daily.py                  # 全ユニバース
python gap_reversal_daily.py --raw            # 1.75%/1.0% の固定閾値版
python gap_reversal_daily.py --earnings       # 決算日を取得して分離
python gap_reversal_daily.py --cost-bps 40 --max-names 13
python gap_reversal_daily.py --csv panel.csv  # 候補パネルを書き出す

# ローカル日足を使う (yfinance を叩かない)
python gap_reversal_daily.py --data-dir ./daily        # <SYM>.csv/.parquet/.pkl を並べたディレクトリ
python gap_reversal_daily.py --data-file all.csv       # symbol,date,ohlcv の長形式1ファイル
python gap_reversal_daily.py --data-dir ./daily --no-index   # 指数ファイルが無い場合
```

**引数なしで実行すると `.rsi2_cache/` を自動で使います** (`--cache-dir` で変更、
`--no-cache` で無効化)。`backtest_limit_entry.fetch` が作った `<7203_T>.pkl` を
そのまま読み、`7203_T` → `7203.T` に読み替えます。250本未満の銘柄は除外。
指数 `^N225.pkl` がキャッシュに無ければ yfinance から取りに行き、それも不可なら
`--no-index` を促して終了します。

`--coverage` は履歴の開始日の分布 (最古 / 中央 / 90%点) と最終日を出して終了します。
`.rsi2_cache/` は `fetch` が開始日を見ない設計のため **2007年まで遡っていない
ことが多い**ので、長期の結論を出す前に必ず確認してください。中央値で5年未満の
場合は警告が出ます。

`--data-dir` / `--data-file` を指定すると yfinance を一切使いません
(ネットワーク不可の環境用)。列名は大文字小文字を問わず
`open/high/low/close/volume` (+ 日付は `date`/`datetime`/index のどれでも可)。
ファイル名 `7203_T.csv` / `7203.csv` / `72030.csv` はいずれも `7203.T` として
も引けます。市場成分の控除には指数 (既定 `^N225`) が必要で、同じディレクトリに
`^N225.csv` を置くか `--index-symbol` で名前を指定します。無い場合は
`--no-index` (β=1・指数リターン0 扱い = 残差ギャップが生ギャップになる)。

`--self-test` は既知の 25bp のエッジを埋め込んだ合成データを流し、レポートが
それを検出できるかを確認します (ネットワーク不要)。ロジックを触ったら必ず実行。

### 16.5.5 ⛔ データ破損の除外 (必須)

`.rsi2_cache/` には **始値が壊れている行**が混ざっています。実測で
「ギャップ 1,227,196%」「Q5 の上限 394,860,203 ATR」が出ました。
`backtest_limit_entry.fetch` は終値の前日比 50% 超しか検査しないため、
**始値は素通り**します。

`gap_reversal_daily` は2段構えで除外します。

1. **OHLC 整合性** — 始値・終値は必ず 安値〜高値 の内側にある。外れていたら破損
2. **値幅制限** — 東証には値幅制限があるので、1,000〜6,000円の株が1日に
   30% を超えて動くことはあり得ない。`|gap|` `|o2c|` `|前日比|` が
   `MAX_GAP` (既定 0.30) を超える行を除外 (`--max-gap` で変更可)

除外件数はパネル構築後に必ず表示されます。内訳は `--audit` で一覧できます。

```
python gap_reversal_daily.py --audit
```

- **始値だけ桁違い** → yfinance の誤植
- **OHLC 全体がきれいな整数倍** → 株式分割の未調整

いずれも該当銘柄のキャッシュを消して取り直してください。

**この汚染は `--prev-thr 0.0` にした瞬間に効きます。** 破損行は終値が正常なので
前日リターンが小さく、前日閾値を課している間は偶然弾かれています。閾値を緩めた
途端に流れ込むので、「緩めたら成績が上がった」ときは真っ先に破損を疑うこと。

### 16.6 データ

- 日足キャッシュは **`.gapmr_cache/`**（`.rsi2_cache/` とは別）。
  `backtest_limit_entry.fetch` はキャッシュの**開始日を見ない**ため長期検証に
  使えません。`gap_reversal_daily.fetch_daily` は開始日も検証します。
- 初回は 2007年以降の日足を全銘柄ぶん取得するので時間がかかります。
- 決算日は `yfinance.get_earnings_dates` (直近数年のみ)。取れない銘柄日は
  除外せず「決算情報なし」グループとして別集計します。

### 16.6.5 方向で制度的制約が全く違う

空売り側 (ギャップアップを売る) には **空売り規制・貸株の可否・増担保・
値幅制限** が効き、板も薄い。買い側 (ギャップダウンを買う) にはこれが無い。
レポート §9 で必ず分けて見ること。

- 買い側だけで成立する → 制度的制約を回避できる。実装が一気に楽になる
- 空売り側にしか無い → 実弾の前に証券会社への照会が必須
  (`check_shortable.py` / `not_shortable.py` / `check_price_limit._TSE_LIMIT_TABLE`)

大きなギャップ (2.5 ATR 以上 = 株価で 5〜6%) はほぼ決算・材料日なので、
往復 30bp というコスト仮定はこの水準では楽観的すぎる可能性が高い。
§6 のコスト感応度を 80bp まで見て判断すること。

### 16.8 ⛔ 始値では建てられない (`gap_reversal_intraday.py`)

`gap_reversal_daily.py` は 始値→終値 で成績を測りますが、**これは執行できません**。
残差ギャップは当日の始値と指数の始値を見て初めて計算できるので、09:00 の
板寄せで始値が付いた後に判定するなら、実際の約定はその次の売買価格です。
事前に条件付きの寄成注文を置ける注文仕様が無い限り、日足の 始値→引け 成績を
そのまま執行可能な成績と見なすことはできません。

`gap_reversal_intraday.py` が分足で価格の階段を測ります。

| 価格 | 意味 |
|---|---|
| 日足の始値 | 条件判定に使う価格。**執行不能** |
| 分足の板寄せ価格 (最初のバーの始値) | 同上。**執行不能** |
| **最初のバーの終値** | 板寄せ後、**実際に入れる最速の価格** |
| +1分 / +5分 / +10分 / +30分 | 遅延による劣化 |

さらに、入る時点までの出来高 × 割合 を各銘柄の上限として資金制約をかけ、
買い側 / 空売り側 を完全に分離して集計します。

```
python gap_reversal_intraday.py --self-test
python gap_reversal_intraday.py                      # 1分足を自動検出
python gap_reversal_intraday.py --interval 5m
python gap_reversal_intraday.py --max-vol-share 0.05
```

**ホールドアウト**: 分足があるのは 2024-07 以降。閾値・ユニバース・残差化の
設計は日足側で探索して決めた経緯があり、t は楽観側に寄っています。
分足期間は **ルールを一切触らない最終ホールドアウト**として扱い、
`FROZEN_PREV_THR` / `FROZEN_GAP_THR` を凍結したまま執行モデルだけを検証します。
**ここで閾値を動かした瞬間、ホールドアウトではなくなります。**

分足の落とし穴 (N_CORE.md より):
- 5分足は**株式分割が未調整**。同じ日の分足終値の中央値と日足終値が 30% 以上
  ずれたら、その銘柄日を捨てる (実装済み)
- **先頭バーが 09:00 とは限らない**。09:05 始まりの銘柄日がある
- 11:30〜12:30 は昼休みでバーが無い。等間隔を仮定しない
- 銘柄コードが2種類。分足は J-Quants の5桁 (`72030`)、日足は `7203.T`

### 16.9 ⛔ 結論: 執行できない (2026-08 実測)

**この戦略は打ち切りです。** 分足ホールドアウト (2024-08〜2026-07、363件 / 181日、
往復30bp) の実測:

```
日足の始値 (執行不能)        +47.32bp   t= 2.03
分足の板寄せ価格 (執行不能)   +47.32bp   t= 2.03   ← 完全に一致
────────────────────────────────────────────────
最初のバーの終値 (≈09:01)    -17.77bp   t=-0.83   ← 実際に入れる最速
+1分                       -21.89bp   t=-0.97
+5分                       -58.25bp   t=-3.55
+10分                      -61.52bp   t=-3.69
+30分                      -36.66bp   t=-2.26
```

**エッジは 09:00 の板寄せ価格そのものに全部あり、09:01 には消えています。**
1分で 65bp 失われ、これはグロスのエッジ全体を上回ります。買い側・空売り側とも
同じ形 (+50.9→-9.1 / +48.2→-13.4)。出来高制約は無関係でした (約定率100%)。

**探索バイアスではありません。** 同期間の日足 α は +52.12bp / t=2.54 で、
全期間の 56.8bp とほぼ一致。日足の現象はホールドアウトでも再現しています。

論理はこう閉じています:

1. 残差ギャップを知るには板寄せの約定価格が要る
2. 板寄せに参加するには、その価格を知る前に注文を出していなければならない
3. 板寄せ後の最初の価格ではもう是正が終わっている

+5分・+10分が強く負なのも整合的で、行き過ぎが瞬時に是正された後は
ギャップ方向へのドリフトが続きます。**遅れて入るのは有害**です。

### 16.9.1 唯一残っている経路 (未検証)

**前場前の気配 (08:59) から残差ギャップを予測し、寄成注文で板寄せに参加する。**
これなら板寄せの約定価格そのものを受け取れます。ただし:

- **バックテストできません。** 板・気配の履歴が無い (N_CORE.md 参照)
- 検証は前進テストのみ: 毎朝 08:59 の気配を記録 → 残差ギャップを予測 →
  実際の始値と突き合わせる、を最低3ヶ月
- 気配が始値をどれだけ予測するかが全て。外れれば、予測誤差ぶんだけ
  エッジが削れる。65bp/分 の減衰速度を考えると、余裕はほとんどありません

この経路を試さないなら、**ギャップ反転は終了**です。シグナルの判定に使う価格と
建てる価格が同一である戦略は、原理的に執行できません。次を探すなら
**「判定に使う価格より後に、別の価格で建てられる」構造**を最初の条件にしてください。

### 16.7 既知の限界

- **サバイバーシップバイアス**: `symbols_listed_prime.py` は今日の上場銘柄。
  2007年から回すと途中で消えた銘柄が抜ける。買い側 (暴落後の反発) で特に効く。
- **執行は検証できない**: 板・気配の履歴が無いので「09:00 に本当にその始値で
  建てられるか」は履歴では分からない。実弾の前に、毎朝 08:59 の気配で候補を
  確定 → 想定約定価格を記録 → 実際の始値と比較、を最低3ヶ月。
- **コストは仮定**: デフォルト往復 30bp (呼値 + 板寄せ + 引け成行)。
  3,000円以上は呼値5円なので実効コストが倍になる点に注意。
- ストップ高で引けに買い戻せない事故は**モデル化していません**。
