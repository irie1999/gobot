# gobot 逆指値シグナル運用メモ

このドキュメントは Claude Code が gobot リポジトリを扱う際の前提知識です。
主軸は **`nikkei_analysis_holdout_2config.py` (ホールドアウト2設定分析レポート)** の運用・改修です。
今後の修正は原則このファイルを起点に考えてください。

---

## ⛔ 絶対にやらないこと (ユーザーの明示指示。例外なし)

1. **Artifact を公開しない**。分析結果・レポートは**必ずチャット内のテキスト(Markdown表)で返す**。
   HTMLレポートを作って公開する運用は禁止 (2026-08-05 指示)。
2. **許可 (permission) を求める操作をしない**。承認ダイアログが出る類の操作は避ける。
3. **`subscribe_pr_activity` を使わない**。

---

## ★★ 毎日使うコマンド (最頻出。聞かれたら即これを答える)

| やりたいこと | コマンド | 実行タイミング | 詳細 |
|---|---|---|---|
| **今日の発注リストを出す** | **`.\daily`** | 朝 8:45 まで | §18.5 |
| **今日の取引結果を出す** | **`.\fills`** | 引け後 15:30 以降 | §18.5.1 |
| 日中の監視・自動決済 | `.\watch` | 寄り〜大引け 15:30 | §18.4 |

- `.\fills` = `python verify_fills.py --prod --save`。kabu の実約定から当日の
  **実損益・fill率**を集計し、`fills_<日付>.csv` / `orders_<日付>.csv` を自動保存。
  **照会のみで発注しない。**
- **`.\daily` と `.\fills` と watcher は同時に走らせない** (kabu の有効トークンは
  1つ。401 でトークンの取り合いになる)。

---

## ★ データ場所メモ (毎回忘れないこと)

- **J-Quants の「完璧な」5分足データは `stock_5min` フォルダにある**。
  swingtrade リポジトリ内 (`data/minute_5m/`) には **無い**。
  - Windows実体: `C:\Users\to732\OneDrive\ドキュメント\kabu station\stock_5min`
    (swingtrade と stock_5min は "kabu station" 直下の兄弟フォルダ)
  - `daytrade_data.py` の `DATA_DIR` は自動検出する (`_resolve_data_dir`):
    環境変数 `MINUTE_5M_DIR` → `data/minute_5m` → 隣接 `stock_5min` の順。
  - 明示指定するなら各スクリプトの `--data-dir` か `set MINUTE_5M_DIR=...` で固定。
- 日足のスイング用データは `.rsi2_cache/` (yfinance永続キャッシュ)。こちらは別物。

---

## 0. メイン分析ツール（最重要）

### `nikkei_analysis_holdout_2config.py` = バックテスト分析のメインコマンド

ホールドアウト期間（直近N日）を除外したWF選定WATCHLISTで conservative / aggressive の
2設定を比較分析するHTMLレポートを生成します。

```
# 標準的な使い方（ホールドアウト180日、直近180日の損益確認）
python nikkei_analysis_holdout_2config.py --holdout-days 180 --days 180

# 直近30日だけ確認したい場合
python nikkei_analysis_holdout_2config.py --holdout-days 30 --days 30

# 予算フィルター（60万円で100株買える銘柄のみ）
python nikkei_analysis_holdout_2config.py --holdout-days 180 --days 180 --budget 600000

# 株価上限指定
python nikkei_analysis_holdout_2config.py --holdout-days 180 --days 180 --max-price 5000

# ブラウザを開かず HTML だけ生成
python nikkei_analysis_holdout_2config.py --holdout-days 180 --days 180 --no-browser

# 並列数を増やして高速化
python nikkei_analysis_holdout_2config.py --holdout-days 180 --days 180 --workers 8
```

**出力**: `nikkei_analysis_holdout2cfg_{N}d_{date}.html`

### レポートのタブ構成

| タブ | 内容 |
|---|---|
| タブ1 | シグナル判定（相場環境・今日使うべきスクリプト）|
| タブ2 | トレンド期間統計 |
| タブ3 | エントリー分析（上昇何日目に入るか）|
| タブ5 | 損益レポート — スクリプト別サマリー / スコア別実績 / ③BT×WFクロス分析 / ④高BT銘柄別成績 / 取引明細 |
| タブ7 | トレンド×相性バックテスト（conservative vs aggressive） |

### タブ5の主要セクション（分析の核心）

- **スコア別実績（② BTスコア軸）**: BTスコア帯ごとの勝率・PF・損益。BT≥60がプラスの境界線
- **③ BT×WFクロス分析**: BT≥60の中でWFスコア帯別に分割。WFがBT内でさらに識別力を持つか確認
- **④ 高BT銘柄別成績**: BT≥60の銘柄ごとの損益集計（損益降順）。損失の出ている特定銘柄を特定できる
  - 赤枠 = 損失-3万超 → スキップ候補
  - 緑枠 = 利益+5万超 → 優先銘柄

### 実運用でのフィルター基準（ホールドアウト検証で確認済み）

| 基準 | 内容 |
|---|---|
| **BTスコア≥60** | 全期間（30〜180日ホールドアウト）で一貫してプラス。最重要フィルター |
| **conservative優先** | 中長期ではconservativeがaggressive より安定 |
| **WFスコアは参考程度** | BTスコアの識別力の方が高い。WFのみで選ぶのは危険 |

---

## 1. エントリーポイント（サブコマンド）

**`run_signals.py`** = 今日のシグナル確認コマンド（日々の発注判断用）。

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
MAX_HOLD          = 10        # 最大保有日数 (超えたら "タイムカット" で終値決済)。全戦略10日・約定日基準 (2026-07-07: ⑬回復分析で10日が最良)。_TIMECUT_ENABLED=False で無効化可
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

## 12. kabu station 連携 (発注パイプライン)

逆指値シグナルを kabuステーション REST API に流し込んで自動発注する仕組み。

### 12.1 ファイル構成

| ファイル | 役割 |
|---|---|
| `kabu_token.py` | トークン取得のみのスタンドアロン (旧)。互換のため残置 |
| `kabu_api.py` | **連携の中核** `KabuClient`。トークン/時価/建玉/余力/発注/取消を集約 |
| `kabu_send_signals.py` | 今日の逆指値シグナルを **逆指値買い** で発注 (エントリー入口) |
| `close_stop_guard.py` | close 方式の損切りを **引け成行(MOC)** で自動決済 (§16 と対) |

### 12.2 KabuClient (kabu_api.py)

```python
from kabu_api import KabuClient
cli = KabuClient(prod=False, dry_run=True)  # 既定: デモ(18081) + dry-run
cli.connect()                                # トークン取得
cli.get_current_price(7203)                  # 時価
cli.send_stop_buy(7203, qty=100, trigger_price=3000)   # 逆指値買い(FOT=30,以上)
cli.send_moc(7203, qty=100, side="sell")               # 引け成行(FOT=16)
cli.send_stop_sell(7203, qty=100, trigger_price=2850)  # 損切り逆指値(FOT=30,以下)
```

- **dry_run=True** なら発注系は API を叩かず内容を print するだけ (安全)。
- 現物 `CashMargin=1` / 信用新規 `=2` / 信用返済 `=3` を引数で切替。
- `check_signal_on_date` の返り値 (`order_price`/`stop_price`/`target_price`) を
  そのまま `send_stop_buy` / `send_stop_sell` の trigger_price に渡せる設計。

### 12.3 運用フロー

```
# エントリー (寄り前 or 引け後): 今日のシグナルを逆指値買いで発注
python kabu_send_signals.py                  # dry-run
python kabu_send_signals.py --execute        # デモ口座に発注
python kabu_send_signals.py --execute --prod # 本番口座 (要明示)
python kabu_send_signals.py --with-stop      # 損切り逆指値も同時 (intraday運用)

# 損切り (毎営業日 14:50-14:55): close 方式の引け成行ガード
python close_stop_guard.py                   # dry-run (判定だけ)
python close_stop_guard.py --execute         # デモ口座に引け成行発注
python close_stop_guard.py --execute --prod  # 本番口座 (要明示)
```

### 12.4 安全設計 (誤発注防止)

- **全スクリプト デフォルト dry-run**。`--execute` のときだけ実発注。
- `--execute` でも接続先は **既定デモ(18081)**。本番(18080)は `--prod` 明示必須。
- API パスワードは環境変数 `KABU_API_PASSWORD` から読む (コード埋め込みしない)。

### 12.5 close 方式と intraday 方式の発注の違い (重要)

§16 の損切りモードと対応:
- **close (既定)**: エントリー時は損切り逆指値を出さない (`kabu_send_signals.py`
  を `--with-stop` なしで実行)。損切りは `close_stop_guard.py` が引け前に判定して
  引け成行で決済。ヒゲ刈り回避。
- **intraday**: エントリー時に損切り逆指値も同時に置く (`--with-stop`)。
  置きっぱなしで放置できるが、ザラ場のヒゲで刈られる。

### 12.6 既知の制約 / TODO

- **保有管理は forward_test_log.csv 依存**: `close_stop_guard.py` は
  `forward_test.py --record` で蓄積した CSV の filled/holding 行を保有とみなす。
  kabu の実建玉 (`get_positions`) との突合は未実装 (将来の整合チェック候補)。
- **信用返済の建玉指定**: `send_moc` の信用返済は `CashMargin=3` だが、
  返済建玉 (ClosePositions) の明示指定は未対応。現状は現物決済を想定。
- **スケジュール実行**: cron / タスクスケジューラは別途設定が必要 (スクリプト側は持たない)。
- **約定確認・リトライ**: 発注後の約定監視や部分約定処理は未実装。

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

## 16. BTスコア改修 TODO（未実装・要リマインド）

### 16.1 現状の暫定対応（実装済み）

`score_speed_patch.py` — import するだけで BTスコアに速度ボーナス最大+10点を追加。
既存コードを変更せずモンキーパッチで適用。

```python
import score_speed_patch  # check_signals_stop/breakout の calc_recommend_score を差し替える
```

速度ボーナス = `max(0, 1 - avg_target_days / 15) × 10点`

### 16.2 将来の抜本改修（未実装）

ユーザーの要望: BTスコア・WFスコア・安定型優先・目標達成速度を全て正しく反映した銘柄選定。

**設計方針:**
1. **BTスコア刷新** — 年率期待値ベースに変更
   ```
   年率期待値 = (勝率×平均利益 - 負け率×平均損失) / 平均保有日数 × 250日
   ```
   これにより勝率・PF・速さを1本の指標に統合できる。

2. **安定型をフィルター条件化** — スコア加算ではなく選定の前提条件に
   ```
   安定性スコア ≥ 閾値 の銘柄のみを選定対象にする（スコア化しない）
   ```

3. **scan_walkforward.py の composite_score** — 年率期待値ベースに変更

4. **build_watchlist.py** — `--min-stability` オプション追加

**変更ファイル:**
- `backtest_limit_entry.py` — `avg_target_days`, `avg_win_pnl`, `avg_loss_pnl` を返り値に追加
- `check_signals_stop.py` / `check_signals_breakout.py` — `calc_recommend_score` 刷新
- `scan_walkforward.py` — `composite_score` 変更、CSVカラム追加
- `build_watchlist.py` — `--min-stability` オプション追加

**注意:** 変更後は過去CSVのスコアと直接比較不可。`scan_walkforward.py` の再実行が必要。

---

## 16. 損切り評価モード (stop_mode) — close 既定化 (2026-06)

ストップ狩り(ヒゲ刈り)対策として、損切りの評価方式を選べるようにしました。
実測 (`analyze_stop_hunt.py`) の結果、**終値判定 (close) が既定** です。

### 16.1 定義 (`backtest_limit_entry.py`)

```python
# default_stop_mode(strategy_name, is_short)
#   "intraday" = ザラ場の安値/高値が損切り価格にタッチで約定 (ヒゲでも発火) ← 旧既定
#   "close"    = 終値が損切り価格を超えたときだけ約定 (引け判定・引け成行)  ← 新既定
```

`run_limit_backtest(..., stop_mode=None)` は未指定時 `default_stop_mode` で自動決定。
明示指定すれば上書き可 (分析・比較用)。

### 16.2 ポリシー (実測に基づく)

| 戦略 | stop_mode | 理由 |
|------|-----------|------|
| **全戦略 (ロング/ショート全部)** | `close` | ヒゲ刈り回避。**実運用 close_stop_guard が終値ベースのため統一** |

> **2026-07-07 変更**: 旧ポリシーは MOM ロングのみ `intraday`（§16.2 実測で close だと悪化したため据え置き）だったが、
> 実運用の `close_stop_guard` は終値ベースで、MOM だけバックテストとライブが乖離していた
> (終値では損切り未達なのにレポートは損切り表示)。ザラ場逆指値を実際には置かない運用に合わせ、
> **MOM も close に統一**（`default_stop_mode` の MOM 例外を削除）。MOM のザラ場優位を取り戻したい場合は
> `kabu_send_signals --with-stop` でザラ場逆指値を実際に置く運用に切り替える必要がある。

### 16.3 実測エビデンス (365日, in-sample, analyze_stop_hunt.py)

| 推奨ポリシー改善 | conservative | aggressive |
|---|---|---|
| 全体 | +594,751円 | +804,544円 |
| ロング(MOM除く) | +145,067 | +403,495 |
| ショート | +449,684 | +401,050 |
| 時期分割(前半/後半) | 全6区分 ✓改善 | 全6区分 ✓改善 |

- ショートが特に効く(踏み上げの上ヒゲ刈り回避)。conservative ショート PF 1.45→1.62。
- 代償は最大単発損失が約2万円深くなる程度。2万損件数はむしろ減少。
- **hybrid(終値+ザラ場ハード損切り)は棄却**: 深いヒゲで勝ちを刈る副作用があり、
  尾リスクをほとんど低減できなかった。

### 16.4 実運用上の意味 (重要)

- close 損切りは **引け(大引け近辺/MOC)で成行決済** する運用。ザラ場の逆指値据え置き
  ではない。kabu 発注では「終値が損切り価格を超えたら翌寄りor当日引け成行」で対応。
- **過去の CSV/HTML 数字は新ルールで上書きされ、旧 intraday の数字とは直接比較不可**
  (§14.1 と同じ注意)。`scan_walkforward.py` 等は再実行で新 CSV を得ること。
- 旧挙動に戻したい場合は呼び出しで `stop_mode="intraday"` を明示。

### 16.5 影響範囲

`run_limit_backtest` は唯一の約定ロジックなので、`run_signals.py` /
`verify_watchlist.py` / `forward_test.py` / `scan_walkforward.py` /
`nikkei_analysis*.py` すべてに自動反映される。

---

## 17. BTスコア・WFテスト・In-sample/OOSの関係（重要）

### 17.1 日次運用コマンド（最重要）

```bash
# ロング+ショート統合（1コマンド）
python run_signals_holdout_all.py --both --max-price 6000 --min-price 1000 --force --days 365
```

出力: `signals_holdout_all_both_YYYY-MM-DD.html`（ロング/ショートタブ切替）

個別実行:
```bash
python run_signals_holdout_all.py --max-price 6000 --min-price 1000 --force --days 365
python run_signals_holdout_all.py --short --max-price 6000 --min-price 1000 --force --days 365
```

### 17.2 BTスコアの計算方法

**BTスコアは WATCHLIST内の銘柄×戦略ごとに1つ**（ホールドアウト設定とは独立）。

`check_signals_stop.calc_recommend_score` / `check_signals_breakout.calc_recommend_score`
（2ファイルに同一実装）

```
BTスコア (0〜100) =
  直近365日バックテストを 30/60/90/120/150/180日 の6期間にスライス
  ↓ 全期間の平均で以下を算出
  平均勝率        × 0.4   → 最大40点
  平均PF ÷ 10    × 30   → 最大30点 (PF=10以上でキャップ、∞は10扱い)
  期間安定性      × 20   → 最大20点 (プラス期間数 / 有効期間数)
  取引回数補正    × 10   → 最大10点 (合計20取引で満点)

ランク: ★★★≥80, ★★≥60, ★≥40, △<40
```

ATRペナルティ (`apply_atr_penalty`):
- 損切り幅≤7%: ペナルティなし
- 損切り幅>7%: スコア × max(0.5, 1 - (損切り幅-7%) / 30)
- 損切り幅≥37%: スコア × 0.5（最大50%減）

### 17.3 WalkForward の銘柄選定とIn-sample/OOSの関係

```
時間軸（過去 → 今日）

│←── WF訓練+テスト期間（銘柄選定に使用）────→│←ホールドアウト N日→│ 今日
│  Fold1: [TRAIN 12M][TEST 6M]              │                   │
│          Fold2: [TRAIN 12M][TEST 6M]      │                   │
│                                           │                   │
│ ← scan_walkforward.py がここで銘柄選定 →  │←── OOS検証領域 ───│
└───────────────────────────────────────────────────────────────┘
```

**6ホールドアウト設定（HO30d〜HO180d）それぞれが異なる銘柄セット（WATCHLIST）を持つ**：
- HO30d: 直近30日を除いた期間でスキャン → 直近30日がOOS
- HO180d: 直近180日を除いた期間でスキャン → 直近180日がOOS

### 17.4 BTスコアのIn-sample biasに注意

BTスコアは直近365日（= WF訓練期間 + ホールドアウト期間）を全部使って計算される。
**つまりBTスコアには銘柄選定に使ったIn-sample期間も含まれており、Biasがある。**

| 区分 | WF銘柄選定 | BTスコアに含まれるか |
|------|-----------|---------------------|
| WF訓練+テスト期間 | 使った（In-sample） | **含まれる ← Bias** |
| ホールドアウト期間 | 使っていない（OOS） | 含まれる（純粋OOS部分）|

**実運用での解釈指針**:
- BTスコアは「直近1年の実績」の参考値として使う（銘柄選定の根拠にはしない）
- 銘柄選定の信頼性は WF テスト期間の成績（scan_walkforward CSV の `total_test_pnl`）で判断
- **損益タブ**（ホールドアウト設定別のPnL）が最も信頼できるOOS検証。HO180dで安定してプラスなら本物
- BTスコア≥60 は「直近1年で機能していた」という意味であり、「これからも機能する」保証ではない

### 17.5 トレンド継続日数の計算（2026-06-15改修済み）

`nikkei_analysis.py` の `extract_periods` / `_append_up`:
- **旧**: `(end_date - start_date).days` → 土日祝を含む暦日数
- **新**: `end_idx - start_idx` → yfinanceデータのバー数 = 土日祝を除く営業日数

表示ラベルは「日」（「営業日」ではない）。統計・分布・継続予測はすべて営業日ベースで再計算済み。

---

## 18. lss (ロング銘柄ショート) 運用まとめ (2026-07-18 セッション確定)

lss = 長候補株を逆指値空売り・同日決済。検証はほぼ結論が出ている。以下は確定事項。

### 18.1 決済ルールは「現行(両方タッチ)」が最善 (再検討不要)
`nikkei_analysis.py` ⑦損切りタブの「終値損切り比較」(BT30以上・2025-12基準・180日OOS)で検証:
- 現行(損切=逆指値タッチ / 利確=タッチ): 基準
- 損切りを日足終値(引けまで持つ): **−21%(−405万) 論外**。損失が引けまで走る。タイトな逆指値損切りは必須。
- 利確を日足終値(引けまで持つ): **+1.66%(+31.8万) ≒ 誤差**。実スリッページで消える水準。
- 5分足終値の利確: +100万相当だが、live実装(watcherの5分足終値監視)が必要でスリッページ懸念。採用せず。
→ **決済は現行のまま変えない**。この比較は既定スキップ(LSS_CLOSESTOP_RESWEEP=1 のときだけ計算・キャッシュ)。

### 18.2 銘柄選定の運用ルール (確定)
- **top指定なし**(`--lss-top` なし)。テスト合格・空売り可・価格レンジ内の全銘柄をシグナル化。
- **BT30以上・BT降順で、資金の許す範囲で投資**(BT0-29が唯一の負け帯)。
- 発注は必ず**シグナルタブ(発注リスト)**に従う(ライブ判定 check_signal_on_date = 実発注と同経路)。
  損益タブの取引明細は過去実績用(発注中は非表示済み)。
- 空売り不可は not_shortable.py で選定時に除外済み(top指定なし版でも除外。check_shortable.py で月1更新)。

### 18.3 空売り固有リスク (結論: ほぼ対策済み)
- **逆日歩**: 同日決済なら基本かからない(持ち越さない限り無関係)。
- **貸株在庫**: 静的な非貸借は not_shortable で除外済み。動的な在庫切れ(稀)だけ残リスク。
  kabu の一般信用(デイトレ)=MarginTradeType 3 が在庫・コスト面で有利。
- **空売り価格規制(51円/アップティック)**: lssは「前日終値-1ティック」の小トリガー+−3%指値下限ガードで
  −10%規制の手前で約定するため実質無関係。

### 18.4 引け決済(タイムカット)の運用鉄則
- 通常は **lss_exit_watcher.py(監視モード)** が日中OCO+引け決済まで自動。15:20にMOC切替→**大引け15:30**まで
  リトライ(東証2024/11以降15:30)。**watcherを15:30まで止めないこと**(途中終了で決済取りこぼし=過去メドレー事例)。
- **発注サーバ(order_server)とwatcherを同時起動しない**。kabuの有効トークンは1つで401の取り合いになる。
  朝はレポートの「発注→監視に切替」ボタンで片方ずつ。
- close_lss_guard.py は watcher を動かさない日のバックアップ(常駐日は実行しない=二重決済防止)。

### 18.5 daily コマンド (現行)
`.\daily` = `run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,0 --lss-proposal auto
--long-base 2025-12-31 --no-mirror --default-tab lss --force --no-news --no-risk --workers 8`
- lss主タブ。ロング/ショートは日別成績確認用に軽量(--no-analysis)で併存。
- --no-news/--no-risk で高速化。--workers 8(no-topで~3200ペア)。

### 18.5.1 ★ 今日の取引結果コマンド = `.\fills` (毎日聞かれる。忘れないこと)

**「今日の取引結果を出したい」= `.\fills`**。`verify_fills.py` を本番口座で叩き、
kabu の実注文/実約定 (`get_orders`) から当日の取引を集計する **照会専用**スクリプト。

```
.\fills                    # = python verify_fills.py --prod --save
.\fills --date 20260728    # 過去日
.\fills --no-date          # 日付で絞らず全注文
.\fills --expected exp.csv # 想定値CSVと乖離比較
.\fills --fee 0.001        # 片道手数料率を適用 (既定0=大口優遇プランは無料)
```

**出力2セクション**:
1. **全注文一覧** — 当日出した注文を約定/未約定問わず全部。注文株数・注文値・状態
   (全約定/一部約定/未約定/取消・失効/期限切れ)・約定株数/値/時刻。
   → **「注文は出したが約定しなかった」= fill率**が一目で分かる。
2. **結果** — ①のうち買戻しまで済んだ往復取引の**実損益・約定時刻**。

**自動保存CSV** (`--save` が `.\fills` に組み込み済み):
- `fills_<yyyyMMdd>.csv` — 往復取引の実損益
- `orders_<yyyyMMdd>.csv` — 全注文一覧

**注意**:
- **照会のみ。絶対に発注しない。**
- **実行タイミングは引け後 (15:30以降)**。MOC買戻しが約定してから。
- **kabu の有効トークンは1つ**。発注サーバ / lss_exit_watcher 稼働中は **401**
  (トークン取り合い) になる。片方を一瞬止めるか少し待って再実行。
- これが §18.7 のフォワード検証の**実測データそのもの**。レポート/バックテストの
  想定値と突き合わせて実運用乖離を測る。

### 18.6 データ・バックアップ
- 5分足(stock_5min)は J-Quants プラン上限で **2024-07 が最古**(分足アドオンは全プラン2年ローリング)。
  基準月スイープの最古は 2024-12(それ以前はTRAINに5分足が無く選定不可)。
- `backup_all.bat [dest]` でコード履歴(git bundle)+研究データ(stock_5min/forward_test/WF/proposals/.env)を
  一括バックアップ。コードはGitHub、データはUSB/別クラウドへ(.env は秘密・公開厳禁)。

### 18.7 残る唯一の重要課題 = フォワードテストで実運用乖離を測る
検証(OOS)は最良クラス(PF2.23/13ヶ月連続プラス)。あとは実約定・実スリッページ・fill率・実現勝率を
実測して、バックテストからの劣化を定量化するのが最後の確認。

**実測の主軸は `.\fills` (§18.5.1)**。kabu の実約定そのものなので最も信頼できる。
毎日引け後に走らせて `fills_<日付>.csv` を貯めれば、そのまま乖離分析の素材になる。
(`forward_test.py --record` は紙トレード記録で、実約定ではない点に注意)

### 18.8 約定モデルを現実化 (2026-07-19) — 楽観バイアス除去
lssの約定を **min(トリガー, その日の始値)** に変更(逆指値売りは寄りがトリガーを割って
始まると始値約定=空売りに不利)。−3%指値ガード(`_INTRADAY_5M_ENTRY_GAP_LIMIT`)を超える
ギャップダウンは約定不可。エンジン(run_limit_backtest)・選定(scan_lss_universe)・比較タブを統一。
- 旧モデル(常にトリガー約定)は楽観的だった。現実化で全lss数値が下がるが、これが本当の水準。
- それでもBT30以上は全月プラスを維持(OOS)。再エントリー分はPF≈1.0(=1銘柄1ポジション運用が正解)。
- 実装: sameday5m_firsttouch.short_entry_fill_5m。旧挙動は _INTRADAY_5M_REALISTIC_ENTRY=False。
- BTキャッシュは _BT_LOGIC_VER で版管理(約定/決済ロジック変更時は版を上げて自動無効化)。
- ⑦タブに指値ガード%スイープ(2/3/5/10/無制限)を追加し3%の妥当性を検証可能(RESWEEP時のみ計算)。
- 残改良余地: 寄りが大きく割れた銘柄は不利約定なので、見送り/優先度低下のルールを将来検討。

### 18.9 損切り遅延 delay1 (2026-07-24) — 寄り1本目のヒゲ刈り回避

lssの損失の大半(BT30以上でも)は「約定直後の寄り付近で、逆行の反発に0.1ATR(≈+0.7%)の
タイト損切りが刈られる」もの。**約定した5分足の間は損切りを効かせず、次の5分グリッド
(09:05/09:10…)から損切りを有効化**すると、一瞬のヒゲを回避して本来の下げを取れる。

**検証(compare_lss_rules.py --bt-min 30, 現実的な約定モデル):**
- BT30以上(実際に投資する集団)で base vs delay1: **勝率48→54% / PF1.63→1.95 / +48%(現実モデル)**。
- **楽観/現実/保守の全fillモデルで delay1 が勝つ**(楽観→現実の目減り9%だけ=頑健)。

**重要: 母集団で結論が正反対**:
- フィルター前(低品質)= だまし下げ→**持続反発**が多く、無保護窓で走り上がられ delay1 は崩壊(−18M)。
- BT30以上(本物の下げtrend)= 反発は一瞬のヒゲ→delay1 が勝つ。**必ず BT30以上で判断すること**。

**約定モデルの注意(楽観 vs 現実):**
- エンジン(短時間足決済)は損切りを **stopちょうど**で約定させる=**楽観**。delay1では「無保護窓で
  価格が損切りを通過→5分後に損切りを置いた瞬間に走り上がった値で成行約定」の悲観を捕捉できない。
- 現実的な下限は `compare_lss_rules.py` の **net現実/net保守**(損切り発火足=`max(stop,始値)`+0.5%)でのみ測れる。
  レポート(.\daily)の delay1 数字は楽観なので過大評価。判断は compare_lss_rules の現実モデルで。

**実装(バックテスト⇄ライブを一致させる):**
- バックテスト: `sameday5m_firsttouch.short_exit_5m(stop_delay_bars=K)`。エンジンは環境変数
  `LSS_STOP_DELAY_BARS`(既定0)で lss(stop_sell)のみに適用。BTキャッシュは版トークンに sd<N> 付与。
- ライブ: `lss_exit_watcher.py --stop-delay-bars 1`。約定検知した5分足の間は損切り(①即時成行/
  ②板逆指値)を設置せず、次の5分グリッドから有効化。検知≒約定時刻(寄り約定+寄りから常駐)。
  **利確・引けはこの間も有効**。既定0=現行(即損切り)。
- **両者を必ず揃える**(バックテストON+ライブOFFだと「レポートは勝つがliveは寄りで刈られる」乖離)。

**現状 = フォワード検証段階**: バックテスト(現実モデル)では十分堅牢。残る未知数は実スリッページ・
fill率・空売り在庫・watcherタイミングで、**実発注(デモ/小ロット)+forward_test で実測**して確定する。
恒久既定化(env/flag無しでON)は forward の実績を見てから。
