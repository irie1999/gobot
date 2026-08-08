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
| **今日の転換結果を出す** | **`.\tenkan`** | `.\fills` の後 | §18.5.2 |
| 日中の監視・自動決済 | `.\watch` | 寄り〜大引け 15:30 | §18.4 / §18.9 |
| **損切り遅延を1日ぶん検証** | **`.\delay`** | `.\fills` の後 | §18.9 |
| 発注リストを軽量・高速に出す | `.\dailyfast` | 朝 8:45 まで | §18.5 |

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

### 18.5.2 ★ 今日の転換結果 = `tenkan_today.py` (実注文ベース。レポートとは別物)

**「lssで約定しなかった銘柄を、代わりにロングしていたら?」を実注文ベースで計算する。**

```
.\fills            # 先に実行 (orders_<日付>.csv が出る)
.\tenkan           # 引数なしで当日の orders_<日付>.csv を自動検出して計算
.\tenkan --date 2026-08-04                       # 過去日
.\tenkan --symbols 3036,3132,6237                # 明示指定 (orders CSV 不要)
.\tenkan --no-save                               # 保存せず表示のみ
```

**必ずCSVに記録される (既定ON。`.\fills` と同じ思想):**
- `tenkan_<yyyyMMdd>.csv` — その日の銘柄別明細
- `tenkan_daily_log.csv` — **日次累積ログ**。1日1行。同じ日を再実行しても
  その行が上書きされるだけで重複しない。2日目以降は実行時に累積サマリー
  (日数/取引数/勝率/損益/プラス日数)も表示される。
  これが「lssが空振りした日に転換がどれだけ補ったか」の実データになる。

ルール: 09:09以降の最初バーOPENで買い → 11:30以前の最後バーOPENで売り / 100株固定 /
スリッページ0.05% / 手数料 片道0.1%。計算は `tenkan_sim.py` に一本化されており、
レポートの転換タブと**完全に同一ロジック**(重複銘柄は円単位で一致することを確認済み)。

**⚠ レポートの転換タブとは母集団が違う。混同しないこと:**

| | 母集団 | 用途 |
|---|---|---|
| `tenkan_today.py` | **実際に約定しなかった注文** | **実運用の判断・日々の実績記録** |
| レポート転換タブ | バックテストが未約定と判定したシグナル | 戦略評価(期待値・PF) |

バックテストの約定率(約90%)は実際(2026-08-05 実測 21%)より大幅に高いため、
レポート側は転換の母集団を過小に見積もる。2026-08-05 の実例: 実際の未約定11銘柄に対し
レポートの転換タブは4銘柄しか拾えていない(7銘柄を「lss約定」と誤判定)。

**2026-08-05 の実測(強い上げ日):**

| | 件数 | 勝率 | PF | 損益 | 投入資金 | 資金効率 |
|---|---|---|---|---|---|---|
| lss ショート(実約定) | 3 | 33.3% | 0.65 | +1,170円 | 976,630円 | +0.12% |
| **転換ロング(未約定11)** | 11 | **63.6%** | **4.00** | **+40,768円** | 3,819,609円 | **+1.07%** |
| 合算 | 14 | 57.1% | — | +41,938円 | 4,796,239円 | +0.87% |

**構造**: 上げた銘柄は前日終値を割らない → lssが約定しない → その「約定しなかった」こと自体が
「上げた」というシグナル → ロング転換が取れる。**lssが空振りした日ほど転換が効く**という
自然なヘッジ関係。約定が少ない日ほど転換の寄与が大きい。

### 18.5.3 ⛔ 転換に「締切時刻」を設けてはいけない (2026-08-05 検証・結論確定)

**問い**: lssシグナルは約定/不約定に分かれる。不約定なら転換したいが、不約定の確定は
大引け。09:05 等で締切を作り「そこまでに約定しなければ転換」にすべきか?

**答え: しない。実運用では「約定しなかった銘柄はそのまま放置」が正解。**
`analyze_tenkan_cutoff.py --days 240 --bt-min 40` (3,415シグナル / delay1 / 補正版fill)。
**比較の基準は『純lss(転換を一切しない)』**。締切なし(現行)は既に終日不約定を転換して
いるので基準ではない点に注意:

| ルール | lss | 転換 | 勝率 | PF | 総損益 | 純lss比 |
|---|---|---|---|---|---|---|
| **純lss(転換なし)** | 3072 | 0 | — | — | **+3,718,388円** | **基準** |
| 締切なし(=終日不約定だけ転換) | 3072 | 343 | 53.9% | 1.98 | +4,029,306円 | +310,918円 |
| 09:05 | 2382 | 1033 | 46.9% | 1.16 | +1,000,480円 | **−2,717,908円** |
| 09:10 | 2490 | 925 | 47.7% | 1.25 | +1,486,815円 | −2,231,573円 |
| 09:15 | 2560 | 855 | 48.0% | 1.31 | +1,771,123円 | −1,947,265円 |
| 09:20 | 2617 | 798 | 48.7% | 1.36 | +1,982,822円 | −1,735,566円 |
| 09:30 | 2685 | 730 | 49.6% | 1.45 | +2,365,282円 | −1,353,106円 |
| 10:00 | 2788 | 627 | 51.0% | 1.60 | +2,929,715円 | −788,673円 |
| 11:00 | 2887 | 528 | 52.5% | 1.80 | +3,538,754円 | −179,634円 |
| 全部転換 | 0 | 3415 | 30.8% | 0.43 | **−5,006,014円** | −8,724,402円 |

**締切は9パターン全てが純lssより悪い**(最も遅い11:00でも −18万円)。
純lssを上回るのは『終日不約定だけ転換』のみ(+31万円)だが、これは後述のとおり
09:09の判断時点では実装できない。

**分岐点は『締切の有無』ではなく『約定した銘柄を転換に混ぜるか』。** 混ぜた瞬間に負ける。
締切が早いほど混入が増えるので悪化幅が大きくなる。

**理由: 転換は『約定した銘柄』では全時刻帯で負ける。**

| 約定時刻帯 | 件数 | lss損益 | 同じ銘柄を転換したら |
|---|---|---|---|
| 〜09:05 | 2382 | +3,175,115 | **−2,831,379** |
| 09:06〜09:10 | 108 | +147,907 | −338,428 |
| 09:11〜09:15 | 70 | +61,514 | −222,795 |
| 09:16〜09:30 | 125 | +126,438 | −467,721 |
| 09:31〜10:00 | 103 | +79,833 | −484,600 |
| 10:01〜11:00 | 99 | +94,226 | −514,813 |
| 11:01〜 | 185 | +33,355 | −457,198 |
| **終日不約定** | 343 | — | **+310,919** ← ここだけプラス |

約定した=前日終値を割った=弱い銘柄。09:09に買って11:30に売れば負ける。転換が効くのは
「一日中 前日終値を割らなかった」=本当に強い銘柄だけ。

**⚠ 含意: 現在の転換の数字は systematic には再現できない。**
「終日不約定」は大引けまで分からないので、09:09の買い判断時点では未知(look-ahead)。
09:09時点で実装できる唯一のルール=09:05締切 は 純lss比 −272万。
→ レポートの転換タブや `.\tenkan` の数字は **実績記録としては正しいが、
そのまま「これだけ稼げる戦略」として扱ってはいけない**。

**★ 実運用の結論: 09:09エントリーである限り、約定しなかった銘柄はそのまま放置する。**
転換を発注ルールに組み込まない。`.\tenkan` は「もし転換していたら」の実績記録用。

**未検証の余地**: 転換のエントリーを遅らせる案(11:30買い→引け売り / 後場寄り買い→
引け売り)なら不約定がほぼ確定してから入れるので look-ahead をほぼ消せる。
ただし保有時間が短くなるぶん取れる値幅も小さくなるので過度な期待は禁物。要検証。

### 18.5.4 ★ 発注額(over-subscribe倍率)と BT閾値 — 運用方針 (2026-08-05)

#### 発注額 = 予算 × 2.0 倍

`sim_portfolio_lss.py --bt-min 40 --budget 4000000` (予算400万・上限キャンセル再現・OOS=2026-01以降):

| 倍率 | OOS損益 | 増分 | 稼働率 | ピーク最大 | ピーク増 | 増分効率 |
|---|---|---|---|---|---|---|
| x1.0 | +1,964,463 | — | 70% | 411万 | — | — |
| x1.5 | +2,235,580 | +271,117 | 88% | 604万 | +193万 | 1,405円/万円 |
| **x2.0** | **+2,506,411** | **+270,831** | **98%** | **745万** | **+141万** | **1,921円/万円 ★最大** |
| x3.0 | +2,857,573 | +351,162 | 109% | 1,126万 | +381万 | 922円/万円 |
| x4.0 | +2,916,517 | +58,944 | 113% | 1,401万 | +275万 | 214円/万円 |
| x5.0 | +2,942,461 | +25,944 | 115% | 1,560万 | +159万 | 163円/万円 |

増分効率(追加した証拠金ピーク1万円あたりの損益増)が **x2.0 で最大**。x4.0以降はほぼ増えない。
TRAIN も x3.0 でピークアウト後は減少し、OOS と一致。

```
python lss_budget_cap.py --execute --prod --budget 4000000 --budget-multiple 2.0 --bt-min 40
```
必要な証拠金余力 = **745万円**(急落日の同時保有ピーク最大)。
余力1,100万以上なら x3.0 で +35万円取れるが効率は半減。

**⚠ 未解決: 約定率がシミュ76% vs 実測20.7%(2026-08-05)で3.7倍乖離。**
実約定率が本当に20%台なら同じ稼働率を得るには x7 程度必要。1日のサンプルなので
`.\fills` を10営業日ぶん貯めて `analyze_fill_rate.py` で確定させてから再調整すること。
それまでは x2.0 で運用(現行1.18倍相当より確実に良い)。

#### BT閾値 = 一律にしない。戦略ごとに変える

`compare_lss_rules --bt-min 40` の BT帯×戦略表(240日・net現実)から 1件あたり期待値(円):

| 戦略 | BT0-19 | BT20-39 | BT40-59 | BT60-79 |
|---|---|---|---|---|
| **A7** | −738 | **+889** | +1,734 | **+2,280** |
| **RSI2** | −501 | **+1,582** | +1,204 | +1,338 |
| **VOLTF** | −745 | **+1,108** | +1,590 | — |
| MACDTF | −1,229 | +41 | +490 | +1,435 |
| MOM | −1,415 | −90 | +672 | +1,017 |
| DON | −1,379 | −238 | +358 | +486 |

| 戦略 | 最低BT | 根拠 |
|---|---|---|
| **A7 / RSI2 / VOLTF** | **20** | BT20-39でも +889〜+1,582円/件。BT40以上の平均(+814)を上回る |
| MACDTF / MOM | **40** | BT20-39は +41 / −90 でほぼゼロ |
| DON | **40**(最低優先) | BT40-59でも +358 と最弱 |
| 全戦略共通 | **BT20未満は発注しない** | 全6戦略マイナス(合計 −432万円) |

**発注順は『BT降順』ではなく『戦略×BT帯の期待値降順』が正しい。**
例: RSI2 BT25(+1,582) は DON BT65(+486) より優先すべきだが、現行のBT降順では逆になる。
※ 戦略別閾値・期待値順の発注は未実装(現行は LSS_BT_TAB_MIN=40 の一律)。

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

### 18.9 損切り遅延 delay2 + 引け間際カットオフ 13:00 (2026-08-06 確定)

**★ 現行の本番設定 (2026-08-06 に delay1 → delay2 へ変更):**

```
.\watch    = lss_exit_watcher.py --execute --prod --all-dates --budget-cap 4000000
             --stop-delay-bars 2 --entry-cutoff 13:00
.\daily    = set LSS_STOP_DELAY_BARS=2 (レポート側も必ず揃える)
```

**確定値 (compare_lss_rules.py --bt-min 40 --days 240 --min-price 1000 --max-price 6000):**

| ルール | net楽観 | net現実 | net保守 | 件数 |
|---|---|---|---|---|
| base | +1,791,907 | +1,791,907 | +515,124 | 2346 |
| delay1 (旧設定) | +2,831,646 | +2,358,023 | +1,256,622 | 2346 |
| **delay2** | +2,981,946 | **+2,436,051** | +1,360,972 | 2346 |
| delay3 / delay4 | +3,013,428 / +3,100,114 | +2,361,226 / +2,360,525 | — | 2346 |
| **delay2 + 13:00カットオフ** | **+3,007,244** | **+2,462,350** | **+1,410,908** | 2285 |

- **delay2 が明確なピーク**。delay3・delay4 は現実モデルで delay1 並みに戻る。
  delay1比 **+78,028円**。これは 2026-08-05 の検証(+84,334円)と**別実行でほぼ再現**しており、
  3つの約定モデル全部・9ヶ月中7ヶ月で勝つ。再現性がこの判断の主根拠。
- **カットオフは 13:00 が最良**(delay2比 現実 +26,299 / 保守 +49,936)。
  時刻スイープ: 14:30 **−1,388**(効かない) / 14:00 +11,445 / **13:00 +26,299** /
  前場のみ +7,635 / 寄り30分のみ **−152,407**(切りすぎ)。
- **カットオフの利得は約定モデルが悲観になるほど大きい**(楽観+25,298 → 現実+26,299 →
  保守+49,936)。遅い約定はバックテストが最も価格付けを誤る領域なので、この向きは重要。
  実証: 2026-08-06 5632三菱製鋼 14:59約定 → 実損 **−9,600円** に対しシミュは −4,552円
  (引け間際の成行滑り55円を捉えられていない)。
- **delay2 の優位は「引け間際は損切り武装前に大引けが来る」抜け穴由来ではない**。
  引け間際を丸ごと消した d2+14:30 が delay2 とほぼ同値(−1,388)= 優位は朝の通常トレード由来。

**⚠ バックテストとライブは必ず揃える** (§18.9 末尾と同じ鉄則)。
2026-08-06 以前は **engine既定が 0** で、env を設定し忘れたスクリプトだけレポートが base に
戻る事故のもとだった。**engine既定を 2 に変更して基準を live 側に寄せ、この穴を塞いだ**:

| 場所 | 既定 |
|---|---|
| `backtest_limit_entry._LSS_STOP_DELAY_BARS` (エンジン) | **2** |
| `run_signals_holdout_all._LSS_STOP_DELAY` (レポート/BTキャッシュ版トークン) | **2** |
| `daily.bat` / `dailyfast.bat` / `sweep_oos.bat` (env) | **2** |
| `export_merge_trades.py` / `sweep_base_months.py` / `sweep_lss_oos_monthly.py` / `run_oos_folds.py` | **2** |
| `sim_portfolio_lss` / `analyze_gap_bt` / `analyze_gap_fills` / `analyze_tenkan_cutoff` の `--stop-delay-bars` | **2** |
| `lss_exit_watcher --stop-delay-bars` (ライブ、`.\watch` が明示) | **2** |

base に戻して比較したいときだけ `set LSS_STOP_DELAY_BARS=0` / `--stop-delay-bars 0` / `--no-delay`。
値を変えると BTキャッシュ(版トークン `sd<N>`)が無効化されるので、切替後の初回 `.\daily` は遅い。

**1日の実績で判断しないこと。** 2026-08-06 の `.\delay`(実約定12銘柄)は delay2 を
**否定**する結果を出したが、その `d0比 +901円` は 5632三菱製鋼1件で作られており、除くと
−7,107円。12銘柄では1銘柄で結論が反転する。判断は必ず compare_lss_rules の母集団で。

---

### 18.10 ⛔ 戦略別BT閾値・期待値順の発注 = 不採用 (2026-08-06 厳密OOSで棄却)

**結論: 発注は現行の『一律BT40 + BT降順』のまま。二度と同じ道を辿らないための記録。**

#### 何を試したか
`compare_lss_rules` の BT帯×戦略の内訳で、同じBT帯でも戦略により1件あたり期待値が
桁違いだと分かった(delay2, 240日):

| BT20-39 | 1件あたり |
|---|---|
| RSI2 | +1,876円 |
| VOLTF | +1,518円 |
| A7 | +1,002円 |
| MACDTF | +678円 |
| DON | -1円 |
| MOM | **-14,529円** |

そこで ①戦略別BT下限(A7/RSI2/VOLTF/MACDTF=20, MOM/DON=40) と
②発注順を『戦略×BT帯の期待値降順』に変える、の2つを実装して検証した。

#### なぜ棄却したか — 実運用と同一パイプラインの10フォールド・ローリングOOS

**★ これが決定的な検証。** `run_oos_folds.py` が出した生CSV(基準月ごとに銘柄選定し直し、
提案を累積マージ → 翌月だけをOOSとして集計 = daily.bat と同一手順)10フォールドを、
`sim_oos_budget.py` で発注ルールだけ差し替えて比較した。

| OOS月 | A 従来 | B ①のみ | C ①+② | B−A | C−A |
|---|---|---|---|---|---|
| 2025-10 | -110,047 | -119,775 | -82,969 | -9,728 | +27,078 |
| 2025-11 | -172,237 | -175,849 | -193,529 | -3,612 | -21,292 |
| 2025-12 | +23,145 | +20,004 | -25,846 | -3,141 | -48,991 |
| 2026-01 | -31,501 | -31,501 | -40,983 | 0 | -9,482 |
| 2026-02 | +31,541 | +45,791 | -24,499 | +14,250 | -56,040 |
| 2026-03 | -107,834 | -137,391 | -161,922 | -29,557 | -54,088 |
| 2026-04 | +441,154 | +441,154 | +577,557 | 0 | +136,403 |
| 2026-05 | +416,406 | +408,598 | +321,461 | -7,808 | -94,945 |
| 2026-06 | +312,229 | +317,053 | **-190,410** | +4,824 | **-502,639** |
| 2026-07 | +484,737 | +484,737 | +286,134 | 0 | -198,603 |
| **合計** | **+1,287,593** | +1,252,821 | **+464,994** | **-34,772** | **-822,599** |

**Aに勝った月: B 2/10、C 2/10。C は -64%。** 4つのシムタイプ全部で同じ向き:

| シムタイプ | A 従来 | B ①のみ | C ①+② |
|---|---|---|---|
| 通常予算(実運用に最も近い) | **+1,287,593** | +1,252,821 | +464,994 |
| 約定額ベース | **+1,402,932** | +1,363,706 | +275,901 |
| ループ充填_全戦略 | +1,247,983 | **+1,275,217** | +182,602 |
| ループ充填_絞り | **+263,447** | -314,123 | -680,721 |

- **C(期待値順)は壊滅的**。2026-06 単月で -502,639。全シムタイプで大敗。
- **B(戦略別BT下限のみ)はほぼ中立〜わずかに悪化**。ループ充填_全戦略でだけ +27,234。
  導入する理由が無い。

※ この生CSVは 2026-08-05 実行ぶんで delay1 基準。**絶対値は delay2 と異なる**が、
  A/B/C は同じ pnl を並べ替えるだけなので比較の妥当性は保たれる。

#### 参考: 単発分割でも同じ結論だった
順位表を直近120日除外で作り(`--holdout-days 120`)、`sim_portfolio_lss --base-month 2026-04`
で 2026-05以降を検証しても **5倍率すべて A > B > C**(x2.0: A +2,180,127 / B +2,111,519 /
C +2,009,717)。しかも TRAIN では B/C が A を上回る(A +2,282,836 / B +2,489,949 /
C +2,475,379)= **TRAIN改善・OOS悪化の教科書的な過剰適合**。

#### ★ 教訓: 「全部買えるなら得」と「予算内でどれを買うか」は別問題

`compare_lss_rules` は **資金制約なしで全トレードを数える**。そこでは戦略別閾値が
+1,613,570円(+65.5%)に見えた。しかし予算を固定して発注順を再現すると逆になる。

BT20-39 の層は単体では確かにプラス(直近120日でも A7 +2,833 / VOLTF +2,381 /
RSI2 +2,327)。だが予算が有限だと **それを追加することでより良いトレードが押し出される**。
単体の期待値がプラスでも、機会費用を含めるとマイナスだった。

→ **発注ルールの変更は必ず `sim_portfolio_lss.py`(予算・上限キャンセル込み)で検証すること。**
   `compare_lss_rules` の総額比較だけで判断してはいけない。

#### 実装は残してある(既定OFF)
`lss_priority.py` + `LSS_PRIORITY` env。有効化しない限り一切影響しない。

| 設定 | 内容 |
|---|---|
| 未設定(既定) | 従来どおり。**これを変えない** |
| `LSS_PRIORITY=floors` | ①のみ(戦略別BT下限・並びはBT降順) |
| `LSS_PRIORITY=1` | ①+② |
| `LSS_BT_MIN_PER_STRATEGY` / `LSS_PRIORITY_TABLE` | 上書き |

**★ 再検証は必ずローリング(実運用と同一パイプライン)で行うこと:**
```
# ① フォールドごとの生CSVを作る(基準月ごとに選定し直し+累積マージ=daily.batと同一)
#    一度作れば発注ルールを変えて何度でも再集計できる(重いのはここだけ)
python run_oos_folds.py --workers 8
# → oos_raw_fold01_YYYYMM.csv 〜 oos_raw_foldNN_YYYYMM.csv

# ② 発注ルールだけ差し替えて集計(--raw はグロブ可。全フォールドまとめて読む)
python sim_oos_budget.py --raw "oos_raw_fold*.csv" --bt-mins 40                              # A 従来
$env:LSS_PRIORITY="floors"; python sim_oos_budget.py --raw "oos_raw_fold*.csv" --bt-mins 40  # B
$env:LSS_PRIORITY="1";      python sim_oos_budget.py --raw "oos_raw_fold*.csv" --bt-mins 40  # C
$env:LSS_PRIORITY=$null
```
順位表を作り直す場合は `compare_lss_rules --holdout-days N`(出力末尾に貼り付け用の
`$env:LSS_PRIORITY_TABLE` が出る)。**--holdout-days を付けずに作った表で検証しても
必ず良く出るだけで意味がない。**

#### 副産物として得た確かな知見(こちらは有効)
- **MOM/DON の BT20-39 は絶対に入れない**。MOM は -14,529円/件。低品質シグナルに
  損切り遅延を掛けると、だまし下げ→持続反発で無保護窓に走り上がられる(§18.9)。
  **delay2 を使う限り BT閾値40を下げてはいけない。**
- **RSI2 は BT に識別力が無い**(BT20-39 +1,876 / 40-59 +1,401 / 60-79 +1,886)。
  他の戦略(A7/VOLTF/MACDTF/DON)は BT が高いほど良く、単調。
- **DON が最弱**。発注対象2,346件のうち780件(33%)を占めて利益は15.9%。
  ただし期待値はプラスなので切らない。
- **120日窓は短すぎる**。順位が激しく入れ替わる(MOM BT20-39 が -14,529 → +1,541 と
  符号反転)。これはテールリスクが120日に現れなかっただけで「安全になった」ではない。
  240日窓なら順位はかなり安定する。

---

### 18.9.1 (旧) 損切り遅延 delay1 (2026-07-24) — 寄り1本目のヒゲ刈り回避

lssの損失の大半(BT30以上でも)は「約定直後の寄り付近で、逆行の反発に0.1ATR(≈+0.7%)の
タイト損切りが刈られる」もの。**約定した5分足の間は損切りを効かせず、次の5分グリッド
(09:05/09:10…)から損切りを有効化**すると、一瞬のヒゲを回避して本来の下げを取れる。

**検証(compare_lss_rules.py --bt-min 30, 現実的な約定モデル):**
- BT30以上(実際に投資する集団)で base vs delay1: **勝率48→54% / PF1.63→1.95 / +48%(現実モデル)**。
- **楽観/現実/保守の全fillモデルで delay1 が勝つ**(楽観→現実の目減り9%だけ=頑健)。

**再検証 (2026-08-05, BT40以上・240日・1000〜6000円):** 上記の結論は維持。

| モデル | base | delay1 | 差 |
|---|---|---|---|
| net楽観 | +1,937,595 | **+2,910,867** | +973,272 |
| net現実 | +1,937,595 | **+2,458,966** | +521,371 |
| net保守 | +659,446 | **+1,347,160** | +687,714 |
| 勝率 / PF | 50% / 1.73 | **55% / 1.87** | — |
| 損切り件数 | 989 | **870** | −119 |

月別は 9ヶ月中 8ヶ月で delay1 が勝つ(負けは 25-12 のみ)。delay2 が僅かに上(現実+605,705 / PF1.90)、
delay3・delay4 は現実で劣化するので **delay2 が頭打ち**。

**⚠ ツールが一度壊れていた事故 (2026-08-05 に修正済み・再発注意):**
`compare_lss_rules._stop_fills` が `real = line if j == ei else max(line, opens[j])` になっており、
**約定バー以外の全バーに窓埋め(始値約定)を課していた**。これは delay 系に構造的不利:
- base(stop_off=0) は約定バーで損切りできるので窓埋めをほぼ免除
- delay1 は定義上 約定バーで損切りできないので窓埋めが100%課される

結果、delay1 の net現実 が −4,128,744 と出て「delay1 は本番から外すべき」という**誤った結論**が
一度出た(1件あたり4,700円＝株価1.9%のスリッページという非現実的な水準)。

正しいモデル = **窓埋めは損切り注文を新規に板へ置いたバーだけ**(以降は板の逆指値がライン約定)。
これは `lss_exit_watcher --stop-delay-bars` の実装(①即時成行/②板逆指値)と一致する。
補正後の目減りは1件520円(株価0.2%)で妥当。旧挙動は `--legacy-stop-fill` で再現可能。
**net現実の数字を見るときは、必ず出力冒頭の「【補正版】/【旧モデル】」表示を確認すること。**

**寄りギャップ約定バーのズレ(調査済み・実質無害):**
寄りでトリガーを割って約定 → 反発でトリガー上に戻ると、約定バーが「最初に安値≤トリガーのバー」
まで後ろにズレ、その間の急騰(損切り区間)を取りこぼす(例 9042 2026-08-05 でレポート+3,404円 vs
実際−2,140円)。ただし全体influenceは **BT40で−1,001円**と実質ゼロ。`--gap_ei0` 系ルールで測定可能。
エンジン(sameday5m_firsttouch)の修正は不要と判断。

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
  `LSS_STOP_DELAY_BARS`(**既定2**。2026-08-06 に 0 から変更)で lss(stop_sell)のみに適用。
  BTキャッシュは版トークンに sd<N> 付与。
- ライブ: `lss_exit_watcher.py --stop-delay-bars K`(**現行 K=2**)。約定検知した5分足から
  K本ぶんは損切り(①即時成行/②板逆指値)を設置せず、その次の5分グリッドから有効化。
  検知≒約定時刻(寄り約定+寄りから常駐)。**利確・引けはこの間も有効**。既定0=即損切り。
- ライブ: `--entry-cutoff 13:00`(**現行ON**)。指定時刻以降は未発動の lss 新規売り逆指値を
  `cancel_gap_orders._budget_sweep` で取消し、新規を建てない。同日決済なので遅い約定ほど
  「利確まで走る時間が無いのに損切りだけ効く」非対称になる(§18.9 冒頭の表)。既定OFF。
- **両者を必ず揃える**(バックテストON+ライブOFFだと「レポートは勝つがliveは寄りで刈られる」乖離)。

**現状 = フォワード検証段階**: バックテスト(現実モデル)では十分堅牢。残る未知数は実スリッページ・
fill率・空売り在庫・watcherタイミングで、**実発注(デモ/小ロット)+forward_test で実測**して確定する。
恒久既定化(env/flag無しでON)は forward の実績を見てから。

---

### 18.11 ★ as-of BT (先読みなしのBTスコア) = 既定ON (2026-08-07 確定)

**過去の取引に『今日のBTスコア』を貼っていたため、レポートの損益タブが未来を知った状態で
発注順を決めていた。** 実測で **93.7%** の取引が該当。既定ONで修正済み。

#### 何が起きていたか (コード上の連鎖)

| # | 場所 | 内容 |
|---|---|---|
| ① | `check_signals_stop.py:46,337` | `PERIODS=[30,90,180,365]` / `cutoff = today - days` → BTの材料は **今日から遡って365日** |
| ② | `check_signals_stop.py:198-209` | 勝率×0.4 + PF/10×30 + プラス期間×20 + 取引数×10 → **儲かるほど高い** |
| ③ | `nikkei_analysis.py:7703-7710` | 凍結キャッシュに無ければ `_sig_sc = rec_score2`(=今日のスコア) |
| ④ | `nikkei_analysis.py:7720` | `"rec_score": _sig_sc` |
| ⑤ | `nikkei_analysis.py:9591,9802,9811` | 予算シミュのBT下限と **BT降順の発注順** がこれを読む |

**損益タブの表示窓(直近180日) ⊂ BTの材料窓(直近365日)** なので完全に重なる。
2026年2月の取引を並べ替えるスコアが、その取引自身と8月までの全取引から作られていた。

#### BTスコアの3経路 (`[BT出所]` 診断で内訳が出る)

| 経路 | 中身 | 正しいか |
|---|---|---|
| **凍結** | `signal_score_cache_lss.json` の日付つきスコア = 発生時の値 | ✅ |
| **as-of** | `full_trade_log` から当時の決済実績だけで再計算 (`_asof_bt_score`) | ✅ |
| **今日のスコア** | `rec_score2` へフォールバック | ❌ **先読み** |

`.\daily`(表示窓180日)では `full_trade_log` が作られず as-of が動かないため、
凍結に無い取引は全部③に落ちていた。凍結は実際に `.\daily` を走らせた日ぶんしか
溜まらないので、数ヶ月前はほぼ空。

**2026-08-07 実測(OFF時)**: `凍結=866件 / as-of=0件 / 今日のスコア=12,849件 (93.7%)`

#### 影響の大きさ (400万円 × BT降順 × BT40以上)

| 月 | OFF (先読みあり) | ON (先読みなし) |
|---|---|---|
| 2026/07 | 263件 61% **+382,603** | 258件 37% **-22,777** |
| 2026/06 | 236件 61% +646,714 | 230件 39% +95,771 |
| 2026/05 | 215件 60% +646,922 | 207件 42% +83,056 |
| 2026/04 | 274件 50% +202,112 | 210件 35% -36,790 |
| 2026/03 | 191件 57% +204,067 | 145件 49% +54,978 |
| 2026/02 | 184件 47% +187,811 | 164件 44% +102,737 |
| **6ヶ月計** | **+2,270,229** | **+276,975 (-88%)** |

件数はほぼ同じ = 取引が落ちたのではなく **並べ替わった** 結果。

**実約定が as-of ON 側を支持する**: 08/05 +1,170 / 08/06 -20,580 / 08/07 -28,340
= 3日で **-47,750円**、勝率30〜40%台。ON の 35-49% と一致し、OFF の60%とは一致しない。

#### 設定

```
daily.bat / dailyfast.bat:  if not defined LSS_ASOF_BT set "LSS_ASOF_BT=1"
```
比較のため1回だけ切るには実行前に `$env:LSS_ASOF_BT = "0"`。

- **発注リスト(シグナルタブ)は無傷** — ただし **当日実行に限る**(下記 ⚠ を必ず読むこと)。
  今日のBTは今日までのデータで計算した正しい値で、ON/OFF で1銘柄も変わらない。
  変わるのは「過去の成績をどう評価するか」だけ。

##### ⚠ ただし『過去日のシグナルタブを今日再生成』したものは先読み (2026-08-08 追記)

シグナルタブのBTは **常に実行日(今日)までの365日** から計算される。signal_dt では切らない:

| 場所 | コード |
|---|---|
| `check_signals_stop.py:334` | `today = datetime.now(JST).date()` ← **実行日**。シグナル日ではない |
| `check_signals_stop.py:337` | `cutoff = today - timedelta(days=days)` / `PERIODS=[30,90,180,365]` |

したがって:

| 使い方 | BTの中身 | 先読み |
|---|---|---|
| **当日朝に生成した発注リスト**(通常運用) | その日までの365日。当日シグナルは未決済 | **なし。正しい** |
| **過去日を今日再生成**(`signals_holdout_all_both_<過去日>.html`) | **実行日までの365日**=その日以降の決済結果込み | **あり** |

例: 2026-08-06 のシグナル画面を 08-08 に生成すると、表示される `BT:86` は 08-06/08-07 の
決済結果を織り込んだ値で、**08-06 当日に見えていた値ではない**。
「この順で発注していれば良かったか」の判断には使えない。その用途は損益タブ(as-of BT 済)を見る。

**as-of BT(`LSS_ASOF_BT`)はこの並びを直さない。** 損益タブの取引評価だけが対象。
凍結キャッシュ `signal_score_cache_lss.json` は当時の値を持つが、シグナルタブは参照していない。

対応: 過去日を指定して生成した場合、レポート上部に**橙色の警告帯を出す**ようにした
(`nikkei_analysis.py` の `_asof_warn`)。当日実行では出ない。
- 代償: 全ペアで `表示窓+400日` のバックテストを追加実行するので損益タブが約2倍遅くなる
  (実測 36.2s → 62.9s)。`full_trade_log` の窓が765日あるので、数ヶ月前の取引にも
  『当時の直近1年』が揃い as-of BT は正しく算出される(履歴不足なら 0=未実証 として除外)。

#### ⛔ これの意味するところ (重要)

**過去のOOS検証・予算倍率(§18.5.4)・BT閾値(§18.10)の数字は、すべて先読み込みで測られていた。**
`sim_portfolio_lss.py` / `sim_oos_budget.py` / `run_oos_folds.py` が読む pnl も同じ経路なので、
再検証するなら `LSS_ASOF_BT=1` を付けてやり直すこと。付けずに出した金額は上振れしている。

#### 残課題

- `lss_proposal_2026-07.py` が **150件**しかない(他の月は621〜1,012件)。選定が失敗している
  可能性があるので要調査。

---

### 18.12 ⛔ BTフィルタには識別力が無い (2026-08-07 as-of BT で確定)

**§18.11 で先読みを除去したら、BTスコアの識別力は消えた。**
「BT≥40 が最重要フィルター」「BT≥60 は一貫してプラス」は**バイアスの産物**だった。

#### 実測 (as-of BT ON / 400万円 / 7ヶ月 2026-02〜08 / delay1 / 1000〜6000円)

| BT閾値 | 取引 | 勝率 | 合計損益 | 月平均 |
|---|---:|---:|---:|---:|
| BT≥0 / 20 / 30 | 1,540 | **41.0%** | **+431,340** | +61,620 |
| **BT≥40 (現行)** | 1,405 | 40.8% | +263,527 | +37,647 |
| BT≥50 | 1,202 | 40.9% | +233,729 | +33,390 |
| BT≥60 | 769 | 42.5% | +390,214 | +55,745 |
| BT≥70 | 369 | 42.0% | +200,352 | +28,622 |

**勝率が 40.8〜42.5% で完全にフラット。** 先読みありのときは 54.1%→66.5% と単調に
上がっていた(下表)。あの勾配は**全部バイアス**だった。

| 参考: 同条件 as-of OFF | BT≥0 | BT≥40 | BT≥60 | BT≥70 |
|---|---:|---:|---:|---:|
| 勝率 | 54.1% | 55.1% | 59.6% | 66.5% |
| 合計 | +2,330,305 | +2,355,160 | +1,906,751 | +1,269,140 |

合計損益も非単調(431k→264k→234k→**390k**→200k)。BT50で底・BT60で回復は信号の形ではない。

閾値の差分(※予算シミュは閾値で買う銘柄自体が入れ替わるので厳密な帯別期待値ではない):

| 帯 | 件数 | 差分損益 | 1件あたり |
|---|---:|---:|---:|
| BT<40 | 135 | +167,813 | **+1,243** |
| BT40-49 | 203 | +29,798 | +147 |
| BT50-59 | 433 | **-156,485** | **-361** |
| BT60-69 | 400 | +189,862 | +475 |
| BT70+ | 369 | 369 → +200,352 | +543 |

BT40未満が最も稼ぎ、BT50台が最も損している。順序に意味が無い。

#### 実約定との整合 (as-of ON が正しいことの3度目の確認)

報告勝率 41.0% に対し実約定(08/05〜08/07)は 30〜40%台。OFF の 54〜61% とは合わない。
2026-08 の報告 -33,210円 vs 実損 -47,750円。差 -14,540円 は09:05損切りの滑り(§18.9)。

#### ★ 失効した根拠 (再検証するまで信じないこと)

BT層別に依存していた判断は**すべて根拠を失った**:

| 項目 | 何に依存していたか |
|---|---|
| §18.5.4 発注額 x2.0 | `sim_portfolio_lss --bt-min 40` の層別 |
| §18.5.4 戦略別BT閾値の期待値表 | BT帯×戦略の1件あたり期待値 |
| §18.9 delay2 / 13:00カットオフ | `compare_lss_rules --bt-min 40` |
| §18.10 戦略別閾値の棄却 | 同上 + 先読み込みの pnl |
| §18.3/§0 「BT≥60 が最重要フィルター」 | 本節で否定 |

再検証するなら `LSS_ASOF_BT=1` を付けてやり直すこと。

#### ★ ただし「BT40を外せ」とは言えない

合計が非単調＝どの閾値が最良かは決められない。月別の振れ幅(±20万円)に対して
閾値間の差(±17万円)が同程度なので、7ヶ月では区別がつかない。
**現行の BT40 は据え置き。ただし「BTで守られている」という前提は捨てる。**

#### 水準

月平均 +37,647円(BT40) 〜 +61,620円(BT0)。倍率x2.0の証拠金ピーク745万に対し月利0.5〜0.8%。
**直近2ヶ月はマイナス** (2026-07 -5,016 / 2026-08 -33,210)。
先読み込みで見えていた月+38万とは桁が違う。**ポジションサイズは相応に落とすこと。**

#### 再現コマンド

```powershell
del bt_tiers_asof.csv
$env:LSS_OOS_BUDGET_CSV = "bt_tiers_asof.csv"
$env:LSS_OOS_BUDGET_BT_TIERS = "0,20,30,40,50,60,70"
$env:LSS_OOS_BUDGET_DAYS = "180"
.\dailyfast --no-serve
$env:LSS_OOS_BUDGET_CSV = $null; $env:LSS_OOS_BUDGET_BT_TIERS = $null; $env:LSS_OOS_BUDGET_DAYS = $null
python aggregate_oos_budget.py --csv bt_tiers_asof.csv --manifest "" --sim-type 通常予算 --by-month
```
出力の `[BT出所]` で `今日のスコア=0件` を必ず確認する(pull漏れだとOFFのまま走る)。

---

### 18.13 ⛔ lss は通期でマイナス / 選別軸は存在しない (2026-08-07 確定)

**先読みを2つとも除去したうえで10ヶ月を測ったら、lss はマイナスだった。**
そして銘柄を選別できる軸は1つも見つからなかった。

#### 通期の成績 (as-of BT + per-symbol START_DATES / 転換を除外 / 全トレード)

| 期間 | 件数 | 損益 |
|---|---:|---:|
| 2025-10-01〜2026-04-09 (前半6ヶ月) | 3,279 | **-1,089,866円** |
| 2026-04-09〜2026-08-07 (直近4ヶ月) | 3,345 | +676,359円 |
| **10ヶ月通期** | **6,624** | **-413,506円** |

180日窓では +546,467円 に見えていた。**好調だった直近だけを切り取っていたため**。
窓を365日に広げると符号が反転する。**表示窓を変えて符号が変わる数字は信用しない。**

#### 転換が数字を膨らませていた

`lss_trades.csv` には戦略『転換』が 349件 / **+451,094円** 混ざっている。
転換は 18.5.3 で「09:09時点では不約定が確定せず systematic に再現できない」と
棄却済みのもので、lss ではない。これを含めると通期が黒字に見える。
**レポートの損益を見るときは転換が混ざっていることを必ず意識すること。**
`find_lss_edge.py` は既定で除外する(`--exclude-strat 転換`)。

#### 選別軸は1つも無い (`find_lss_edge.py`, 365日, TRAIN 126日/TEST 82日)

| 軸 | 結果 |
|---|---|
| BTスコア | 勝率が全帯 40.8〜42.5% でフラット(18.12) |
| 株価 / ATR% / 前日・5日・20日リターン | 候補ゼロ |
| 出来高比 / 売買代金 / 20日レンジ位置 / 連続上昇日数 | 候補ゼロ |
| 曜日 / 戦略 / 日経前日リターン | 候補ゼロ |
| 寄りギャップ / 日経寄りギャップ / 約定時刻 | 候補ゼロ |
| **同日発注数**(日の選択) | 候補ゼロ |

15軸 × 5分位 = 78検定で **候補0個**(帰無 平均0.3個 / 95%点1個)。

『同日発注数』は「予算400万が1日13件しか建てられない=シグナル過多の日を自動的に
切り捨てている、それが効いているのでは」という仮説で追加したが、TRAIN は全帯
マイナス、TEST は少ない日ほど悪い(〜18件で -1,271円/件)と**逆向き**で、否定された。

#### 統計の作法 (このツールで踏んだ罠。他の検証でも同じことが起きる)

1. **同日相関**: lss は同日決済なので、下げた日は全銘柄がまとめて勝つ。
   件数で t を計算すると実効サンプルを誤認する(6,624件ではなく208日)。
   日クラスタ頑健な分散が必須。曜日『火』は件数ベースで t=+4.9 だったが、
   クラスタ頑健では +2.3、TRAIN では +0.7 で消えた。
2. **多重検定**: 15軸×5帯を試せば偶然いくつか通る。損益を巡回シフト(日ブロック
   保持)した帰無較正で『偶然でも出る個数』を実測して並べること。
   完全シャッフルは日内相関を壊すので帰無分布が狭くなり偽陽性を過小評価する。
3. **TRAIN で効いていないものを候補にしない**: 符号一致だけを条件にすると
   TRAIN t=+0.2 のような無意味な値でも通る。それは検証ではなくTEST期間のノイズ。

#### ★ 結論と残る検証

**「良い銘柄を選ぶ」という発想は捨てる。** BTも属性も日も効かない。

lss にエッジがあるという証拠は現時点で無い。残っている検証は2つだけ:

1. **予算制約(400万・1日13件・上限キャンセル)そのものにエッジがあるか**
   → 365日窓の予算タブで確認する。母集団がマイナスでも予算シミュがプラスなら
      切り捨て方に構造がある。同じくマイナスなら lss にエッジは無い。
2. **決済パラメータ(損切ATR=0.1 / 利確ATR=1.0)の再最適化**
   → 「5分足最適」とあるが in-sample で決めた可能性が高い。
      TRAIN/TEST を分けて sm/tm をスイープする。

この2つが両方ダメなら、戦略を畳むか根本から作り直す判断になる。
**それまで実弾のサイズは上げないこと。**

---

### 18.14 ★ 手数料を 0 に修正 (2026-08-07)。過去の全金額が非互換になる

**実口座は信用大口優遇プランで手数料無料。ところがバックテストは往復0.2%を
引き続けていた。** 株価3,000円 × 100株なら **600円/件**。

これは lss/LDT の負け幅より大きい:

| | 件数 | ネット | 1件あたり | 手数料 | **粗利** |
|---|---:|---:|---:|---:|---:|
| lss (逆指値ショート・同日決済) | 6,624 | -413,506 | -62円 | -600円 | **+538円** |
| LDT (逆指値ロング・同日決済) | 5,553 | -1,945,452 | -350円 | -600円 | **+250円** |

**払っていないコストで赤字にしていた。** `verify_fills.py --fee` の既定が 0
(「信用大口優遇プランは手数料無料」)なのに、エンジン側だけ 0.001 のままだった。

#### 変更内容

| 場所 | 変更 |
|---|---|
| `backtest_limit_entry.FEE_PCT_ONE_WAY` | `0.001` → **`float(os.environ.get("LSS_FEE_ONE_WAY", "0"))`** |
| `run_signals_holdout_all._BT_LOGIC_VER` | `v13` → **`v15`**(v13キャッシュは手数料込みなので必ず分ける) |
| 同 (env で戻したとき) | `fee<‰×10>` トークンを付与して別キャッシュに |
| `sim_portfolio_lss` キャッシュ鍵 | `spv2` → `spv3` + `FEE` を鍵に追加 |

手数料ありで比較したいときだけ `set LSS_FEE_ONE_WAY=0.001`。

#### ⛔ 過去の数字はすべて非互換

18.11〜18.13 に記録した金額(as-of BT / 選定リーク除去後の -413,506円、
予算タブ 11ヶ月 -114,595円 など)は**すべて手数料込み**。手数料0で測り直すと
おおむね「+600円 × 件数」ぶん上振れする(概算):

| | 手数料込み(記録済み) | 手数料0の概算 |
|---|---:|---:|
| lss 全トレード 10ヶ月 (6,624件) | -413,506 | **約 +3.5M** |
| 予算タブ 11ヶ月 BT0 (2,216件) | -114,595 | **約 +1.2M** |
| 予算タブ 11ヶ月 BT40 (1,901件) | -24,813 | **約 +1.1M** |

**概算なので必ず測り直すこと。** 初回の `.\daily` はBTキャッシュ再構築で遅い。

#### 実測 (2026-08-07 / 手数料0で測り直し / lss / 365日 2025-08-08〜2026-08-08)

| | 件数 | 勝率 | 損益 |
|---|---:|---:|---:|
| レポート上部KPI | 5,226 | 41.3% | **+2,351,700円** |
| うち転換(実装不可・18.5.3) | 402 | — | +449,170円 |
| **lss本体(転換を除く)** | **4,824** | — | **+1,902,530円** |
| 参考: 再エントリー(1銘柄1ポジションで取れない) | 1,398 | 40.6% | +904,150円 |

**手数料だけを実口座(0)に合わせたら、同じレポートが -0.4M から +1.9M に反転した。**
1件あたり約600円 × 5,000件 ≒ 300万円が、払っていないコストとして引かれていた。

⚠ ただしこれは**予算制約なしの全トレード**。実運用は400万で1日13件しか建てられない。
   予算タブ / sim_portfolio_lss で測り直すまで「黒字」とは言えない。

#### ⚠ ただし「黒字が確定した」ではない

実約定 3日(08/05〜08/07)は **-47,750円**。実口座は手数料0なので、この数字は
既に手数料ゼロの世界の結果である。にもかかわらず負けている。

つまり **滑りが『幻の手数料』を上回っている可能性がある**:

| 2026-08 (5営業日) | |
|---|---:|
| レポート(手数料込み) | -33,210 |
| 実約定 | -47,750 |
| 差 | **-14,540** |

手数料0にすればレポートは +数万円 改善するが、実測との差はむしろ広がる。
**手数料ゼロ化の利得(+600円/件) vs 滑りの損失(?円/件) の比較が本番。**
`.\fills` の `slip_daily_log.csv` を10営業日ぶん貯めれば直接比較できる(18.A1)。

#### 副次的な確認事項

- `SLIPPAGE_STOP_PCT = 0.005` は据え置き。こちらは実在するコストで、
  実測(09:05損切りの成行滑り)はむしろこれより悪い。
- 5分足の同日決済経路は `_INTRADAY_5M_SLIP`(既定0)を使う。slip=0 方針のまま。

---

### 18.15 ★ 手数料0での予算タブ実測 (2026-08-07)。**符号が変わったのは手数料だけが理由**

`.\dailyfast --days 365` + `LSS_OOS_BUDGET_CSV` / 400万円 / delay1 /
as-of BT + per-symbol START_DATES(先読み除去済み) / **手数料0**:

| BT閾値 | 取引 | 勝率 | 11ヶ月合計 | 月平均 |
|---|---:|---:|---:|---:|
| BT≥0 | 2,374 | 41.0% | **+1,005,715円** | **+91,429円** |
| BT≥40 (現行) | 2,113 | 40.7% | **+883,421円** | **+80,311円** |

#### 手数料込み(18.13/18.14 に記録した値)との比較 — 変えたのは手数料だけ

| | 手数料 0.1%片道 | **手数料 0** | 差 |
|---|---:|---:|---:|
| BT≥0 | -114,595 | **+1,005,715** | +1,120,310 |
| BT≥40 | -24,813 | **+883,421** | +908,234 |

**as-of BT も START_DATES も先読み除去済みのまま。手数料だけで符号が反転した。**

#### 統計 (月次)

| | n | 月平均 | σ | t | 95%信頼区間 | プラス月 |
|---|---:|---:|---:|---:|---|---|
| BT0 11ヶ月 | 11 | +91,429 | 90,194 | **3.36** | +38,127〜+144,730 | 9/11 |
| BT40 11ヶ月 | 11 | +80,311 | 86,341 | **3.08** | +29,287〜+131,335 | 8/11 |
| BT0 10ヶ月(完全月のみ) | 10 | +100,590 | 89,516 | 3.55 | +45,108〜+156,073 | 9/10 |

**t>3・95%CIが全域プラス。** 手数料込みのときは月平均 -2,256円(BT40)で
「11ヶ月中プラス4ヶ月」だったのが、「プラス8〜9ヶ月」に変わった。

月別(BT40): +78,316 / -17,770 / +118,251 / +11,948 / +85,167 / -26,543 /
+151,014 / +197,393 / +227,603 / +58,231 / -189

#### BT40 は相変わらず BT0 に劣る (18.12 と整合)

BT40 は BT0 より **-122,294円 / 11ヶ月**。18.12 の「BTに識別力なし」と同じ向き。
ただし月次の振れ(σ≈9万)に対して差は月1.1万なので統計的には区別できない。
**現行 BT40 は据え置きでよいが、守ってくれてはいない。**

#### ⛔ 唯一にして最大の未解決: 実約定との乖離

| 2026-08 | |
|---|---:|
| レポート(手数料0) | **-189円** |
| 実約定(08/05〜08/07 の3日) | **-47,750円** |

**実口座は元から手数料0なので、実測は既に「手数料0の世界」の結果。**
それでも負けている。実約定は約22件なので1件あたり **約 -2,170円** の乖離で、
これは幻の手数料(600円/件)の3.6倍。**これが常態なら月 -30万円規模**になり、
上の +80,311円/月 は消える。

ただし3営業日・22件しかないので確定ではない(1件あたりσ≈4,500円なら 2.3σ)。
**`.\fills` の `slip_daily_log.csv` を10営業日ぶん貯めて確定させること(18.A1)。**
これが黒字/赤字を決める最後の関門。

#### 再現コマンド

```powershell
del bt_tiers_fee0.csv
$env:LSS_OOS_BUDGET_CSV = "bt_tiers_fee0.csv"
$env:LSS_OOS_BUDGET_BT_TIERS = "0,40"
$env:LSS_OOS_BUDGET_DAYS = "365"
.\dailyfast --no-serve --days 365
$env:LSS_OOS_BUDGET_CSV = $null; $env:LSS_OOS_BUDGET_BT_TIERS = $null; $env:LSS_OOS_BUDGET_DAYS = $null
python aggregate_oos_budget.py --csv bt_tiers_fee0.csv --manifest "" --sim-type 通常予算 --by-month
```

---

### 18.16 ⚠ 残っている先読み3件 (2026-08-07 点検)。18.15 の +883,421円 はまだ完全にクリーンではない

18.11〜18.14 で3種類の先読みを除去したが、**点検したらまだ3件残っていた**。
18.15 の数字を「純OOS」と呼ぶ前に、少なくとも①は測り直す必要がある。

#### ✅ 除去済み

| # | 何 | 確認方法 |
|---|---|---|
| as-of BT | 過去の取引に今日のBTを貼っていた | `[BT出所] 今日のスコア=0件` |
| per-symbol START_DATES | 未来の選定結果で過去を評価 | `[START_DATES per-symbol] 全1908件` |
| sim_portfolio の START_DATES | 同上(取り残されていた) | `[START_DATES] ... 注文を除外` |

#### ⛔ ① ユニバースの価格フィルタが『今日の株価』(最大の残存バイアス)

`_filter_wl_by_price` は **最新終値**でユニバースを切る。実測 **1,792→1,328(464件除外)**。

2025-10 に 3,000円 だった銘柄が今日 8,000円 なら、max=6,000 で **10ヶ月ぶん丸ごと除外**。
当時は普通に発注できた銘柄なのに。**lss はショートなので、上昇して上限を超えた銘柄
(=ショートが負ける銘柄)が系統的に消える = 有利方向のバイアス。**

発注リスト(今日のシグナル)には正しい挙動("今日100株買えるか")。過去の集計だけの問題。
取引単位のフィルタ(`_PNL_ENTRY_MAX_PRICE` = 約定値で判定)は先読みなしなので、
ユニバース側だけ外して影響を測れるようにした:

```powershell
del bt_tiers_nupf.csv
$env:LSS_NO_UNIVERSE_PRICE_FILTER = "1"
$env:LSS_OOS_BUDGET_CSV = "bt_tiers_nupf.csv"
$env:LSS_OOS_BUDGET_BT_TIERS = "0,40"
$env:LSS_OOS_BUDGET_DAYS = "365"
.\dailyfast --no-serve --days 365
$env:LSS_NO_UNIVERSE_PRICE_FILTER = $null
$env:LSS_OOS_BUDGET_CSV = $null; $env:LSS_OOS_BUDGET_BT_TIERS = $null; $env:LSS_OOS_BUDGET_DAYS = $null
python aggregate_oos_budget.py --csv bt_tiers_nupf.csv --manifest "" --sim-type 通常予算 --by-month
```

BTキャッシュは `nupf` トークンで別管理(初回は再構築で遅い)。

##### ★ 実測結果 (2026-08-07): **仮説は外れ。バイアスは逆向きだった**

| | 価格フィルタあり (18.15) | フィルタ外し (nupf) | 差 |
|---|---:|---:|---:|
| BT≥0 | 2,374件 +1,005,715 | 2,404件 **+1,073,138** | **+67,423** |
| BT≥40 | 2,113件 +883,421 | 2,171件 **+943,249** | **+59,828** |

**先読みを外したら数字が上がった。** つまりこのフィルタは戦略に**不利**に働いていた。

理由: lss は**同日決済**なので、長期の株価推移は当日の損益をほとんど決めない。
「上昇して6,000円を超えた銘柄=ショートが負ける」という直感は、保有し続ける戦略には
当てはまるが同日決済には当てはまらない。除外されていた集団は単に『別の集団』で、
むしろわずかに良かった(追加58件で +59,828円 = +1,032円/件、全体平均 +434円/件 より上)。
ただし58件なのでノイズの可能性もある。

**結論: この先読みは +883,421円 を水増ししていなかった。むしろ約6万円ぶん過小評価していた。**

#### ⚠ ② 空売り不可116件を『今日のリスト』で過去から除外

`not_shortable.py` は現時点の可否。当時は空売りできた銘柄かもしれない。
影響は①より小さいと思われるが方向は不明。`check_shortable.py` の履歴が無いので
現状は測れない。

#### ⚠ ③ パラメータを全期間の成績を見て選んでいる

delay1 / sm0.1 / tm1.0 / BT40 は、この11ヶ月を含むデータで選ばれた。
ただし:
- sm/tm は今日のスイープで「現行が最良、変えない」と結論(=変更していない)
- delay も同様
- 選択肢が少ないので過剰適合の余地は限定的

**それでも「11ヶ月の成績を、その11ヶ月を見て選んだ設定で測っている」ことは変わらない。**
厳密には基準月ごとのローリング(`run_oos_folds.py`)でしか消せない。

#### 結論

**18.15 の +883,421円 は「大部分の先読みを除いた値」であって「純OOS」ではない。**
①を測れば、どれだけ上振れしているかが分かる。

---

### 18.17 ★★ 実測で乖離の正体が判明 (2026-08-07)。**delay1 の無保護窓が全損失の86%**

`.\fills --date` を3営業日ぶん流して `slip_daily_log.csv` に貯めた実測。

| 日付 | 突合 | 実損益 | テスト | 差 | エントリ滑り | 決済滑り | 約定率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-08-05 | 3 | +1,170 | +7,600 | -6,430 | +630 | -7,060 | 20.0% |
| 2026-08-06 | 11 | -22,580 | -7,800 | -14,780 | -280 | -14,500 | 100.0% |
| 2026-08-07 | 11 | -28,340 | -6,300 | -22,040 | -2,050 | -19,990 | 73.3% |
| **合計** | **25** | **-49,750** | **-6,500** | **-43,250** | **-1,700** | **-41,550** | |

#### ① エントリーの約定モデルは正確 (-68円/件)

逆指値売りの `min(トリガー, 始値)` モデル(18.8)は実物と合っている。**ここは直す必要なし。**

#### ② 決済滑りの99%がたった6件

| 日 | 銘柄 | 約定 | 決済 | 実買戻 | テスト | 決済滑り | 区分 |
|---|---|---|---|---:|---:|---:|---|
| 08/07 | 4911 資生堂 | 09:00 | 09:05 | 3,536.5 | 3,410.0 | **-12,650** | delay無保護窓 |
| 08/07 | 5632 三菱製鋼 | 09:00 | 09:05 | 2,360.0 | 2,285.0 | **-7,500** | delay無保護窓 |
| 08/05 | 9042 阪急阪神 | 09:00 | 09:06 | 4,527.6 | 4,457.0 | **-7,060** | delay無保護窓 |
| 08/06 | 8061 西華産業 | 09:00 | 09:05 | 3,023.5 | 2,966.0 | **-5,750** | delay無保護窓 |
| 08/06 | 5632 三菱製鋼 | 14:59 | 15:00 | 2,335.0 | 2,280.0 | -5,500 | 引けMOC |
| 08/06 | 6277 ホソカワミクロン | 09:06 | 09:10 | 5,298.0 | 5,270.0 | -2,800 | delay無保護窓 |
| | **計** | | | | | **-41,260** | |

**残り19件の決済滑りは合計 -290円 (= -15円/件)。ほぼ完璧に一致している。**

#### ③ 原因は delay1 の無保護窓。18.9 が理論的に警告していたことの実測

> 18.9 より: 「delay1では『無保護窓で価格が損切りを通過→5分後に損切りを置いた瞬間に
> 走り上がった値で成行約定』の悲観を捕捉できない」

まさにこれ。**5件すべてが 09:05〜09:10 の損切り発動**で、delay1 が損切りを武装する
最初のグリッドと一致する。09:00〜09:05 の無保護窓で価格が損切りを大きく超え、
09:05 に watcher が①即時成行で買い戻した結果:

- 4911 資生堂: 損切り 3,410 に対し **3,536.5 で約定**(3.7%超過)
- 5632 三菱製鋼(08/07): 損切り 2,315 に対し **2,360 で約定**。テストは損切りに当たらず
  タイムカット +1,500円 の判定だった

#### ④ 収支: delay1 はバックテスト利得の6.4倍のコストを実運用で払っている

| | 1件あたり |
|---|---:|
| delay1 のバックテスト利得 (18.9: +521,371円 / 2,346件) | **+222円** |
| delay無保護窓の実測コスト (-35,760円 / 25件) | **-1,430円** |

バックテストは損切りを **stopちょうど**で約定させるので、この暴走を構造的に見られない。
`compare_lss_rules` の net保守(+0.5%)でも足りない。**実測は全モデルより悪い。**

#### ⑤ 引け間際(14:59約定→15:00決済)は別問題で -5,500円

`lss_exit_watcher --entry-cutoff` の対象。①より小さいので優先度は低い。

#### ★ 判断

**サンプルは5件しかないが、機構が特定できており理論とも一致する。**
そして非対称性が大きい:

- delay0 に戻して delay1 が正しかった場合の損失 = **+222円/件** の機会損失
- delay1 のまま続けて実測どおりだった場合の損失 = **-1,430円/件**

`delay0` は未検証の設定ではなく、エンジンの `base`(元の挙動)。

**推奨: ライブとレポートを両方 delay0 に揃える。**(18.9 の鉄則「両者を必ず揃える」)
```
watch.bat  : --stop-delay-bars 1  →  0
daily.bat  : set "LSS_STOP_DELAY_BARS=1"  →  0
dailyfast.bat : 同上
```
切替後の初回 `.\daily` はBTキャッシュ再構築(版トークン `sd1` → なし)で遅い。
**変更後も `.\fills` を貯め続けて、決済滑りが -15円/件 の水準に収まるか確認すること。**
