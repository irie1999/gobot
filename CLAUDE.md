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
- **1分足は別の場所にある。`stock_5min` には無い。**
  - Windows実体: `C:\Users\to732\.jquants_cache\minute\<コード>_1m.pkl` (銘柄ごとに1ファイル)
  - 解決は `tenkan_sim.find_minute_dirs()`: 環境変数 `MINUTE_1M_DIR` → 無ければ
    `~/.jquants_cache/minute`。**5分足とは解決ロジックも置き場も別**なので混同しないこと。
  - 取得は `python fetch_1m_all.py` (既定760日≒2年ローリング上限 / 中断→再開可 /
    `--refetch` で取り直し)。J-Quants の分足アドオンは全プラン2年ローリングなので
    5分足と同じく **2024-07 が最古**。
  - 在庫確認は `python check_minute_data.py`。
  - 用途: `verify_stop_fill_1m.py` (5分足モデルの検証 / §18.22) など。
    **1分足で測れるのは「1分以上保有する」戦略まで**。1分足の中の値動きの順序
    (高値と安値のどちらが先か) は分からないので、同一足内でエントリー→決済する
    戦略は測れない。板・歩み値・ティックの履歴は**存在しない**。
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

~~⚠ 未解決: 約定率がシミュ76% vs 実測20.7%(2026-08-05)で3.7倍乖離。~~
**⛔ 2026-08-08 撤回: この比較は不成立だった。** シミュ76%は**全期間平均**、
実測20.7%は**2026-08-05 の1日**で、しかもその日は §18.5.2 に「強い上げ日」と
記録されている(前日終値を割る銘柄が少ない日)。同じ日ならバックテストも低く出る。
**乖離があるかどうかも分かっていない**というのが正しい状態。

ユーザー判断(2026-08-08): 約定率に大きな差は無いと見て**この件は追わない**。
→ **x2.0 はそのまま**。倍率スイープは sim_portfolio_lss 内部で一貫して
  (バックテストの約定率で)測っているので、比較が不成立でも結論は揺れない。

⚠ ただし x2.0 自体は §18.12 の「失効した根拠」リストに載っている(先読みBTで
  測ったもの)。約定率とは**別の理由**で再検証が必要な点は変わらない。

⚠ **決済価格の精度とは別の話**。決済側は §18.22 で1分足2,703件と突合し
  ±260円/件 と検証済み(資生堂 2026-08-07 はモデル3,536.0 vs 実約定3,536.5)。
  「約定率が未検証」と「決済価格が正確」は両立する。混同しないこと。

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

### 18.10.1 ⛔ 素の `python run_signals_holdout_all.py` を使わない (2026-08-08)

**env は `.bat` が渡している。素の python で叩くと一切効かない。**

| env | daily.bat | **素の python(既定)** | 効果 |
|---|---|---|---|
| `LSS_ASOF_BT` | **1** | **OFF** | OFFだと過去の取引を『今日のBT』で並べる = **先読み**(18.11) |
| `LSS_STOP_DELAY_BARS` | **1** | **0** | live(`watch.bat` = 1)と食い違う |
| `LSS_BT_TAB_MIN` | 40 | 30 | 予算シミュのBT下限が変わる |
| `LSS_TRADES_CSV` | あり | 無し | `.\fills` の突合セクションが出ない |

実際に事故った例: LDT を素の python で測ったら勝率 79%/79%/62% と出た。
これは lss の先読みありプロファイル(52-71%)と同じ形で、as-of ON なら 37-47% 帯になる。

**★ 必ず `.bat` 経由で回すこと。** lss = `.\daily` / LDT = **`.\ldt`**(2026-08-08 追加)。
新しい測定を作るときも、まず daily.bat の env ブロックをコピーした .bat を作る。

⛔ **.bat は ASCII のみで書くこと。** cmd.exe は .bat を OEM コードページ(日本語Windowsは932)で
読むので、UTF-8 の日本語コメントが化ける。CP932 の2バイト目には `0x7C`(|) や `0x26`(&) を
持つ文字があり、cmd がそれを演算子と解釈して REM 行を分割し、断片をコマンドとして実行しようとする。
2026-08-08 に日本語コメント入りの ldt.bat を置いて実際に壊れた
(`'☆繧九・*LDT' は…認識されていません` が延々出る)。既存の .bat が全部英語コメントなのはこのため。

⚠ なお **18.9 の env 既定表は実機と食い違っている**(2026-08-08 点検):

| 場所 | 18.9 の記述 | **実際のコード/バッチ** |
|---|---|---|
| `backtest_limit_entry._LSS_STOP_DELAY_BARS` | 2 | **0** |
| `daily.bat` / `dailyfast.bat` | 2 | **1** |
| `watch.bat --stop-delay-bars` | 2 | **1** |
| `watch.bat --entry-cutoff` | 13:00 | **無し** |

**実機が正**(本セッションの測定はすべて delay1)。18.9 の delay2/13:00 は適用されていない。

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

シグナルタブのBTの出所は **2通り** (`nikkei_analysis.py:2323-2330` `_check_one`):

| # | 条件 | 使う値 |
|---|---|---|
| (a) | **凍結あり** | `_FROZEN_BT_SCORES[(sym, strat)]` で `calc_recommend_score` を上書き |
| (b) | 凍結なし | `calc_recommend_score` = **実行日までの365日** (`check_signals_stop.py:334` `today = datetime.now(JST).date()`) |

**当日実行ならどちらも正しい**(その日のシグナルはまだ決済していない)。
過去日を後から再生成すると、次の3つが『順位』を動かす:

| # | 何が起きるか |
|---|---|
| ① | 凍結辞書は **(銘柄,戦略) 単位で最新シグナル日の値**を持つ (`run_signals_holdout_all.py:1652-1667` `_cached_latest`)。表示日より後の凍結値が出うる |
| ② | 凍結が無い銘柄は実行日までの365日で再計算 = その日以降の決済結果込み = **先読み** |
| ③ | `_filter_wl_by_price` が **最新終値**でユニバースを切る(18.16)。当時は対象だった銘柄が**行ごと消え**、下位が繰り上がって順位がズレる |

**★ 実測(2026-08-08)では ③ が主犯だった。** 2026-08-06 のシグナルを 08-08 に再生成すると、
BTの**値は168件すべて当時と一致**(凍結が効いている)のに、当時3位の 6209.T DON(BT81)が
画面から消え、4位だった 4553.T A7(BT77)が3位に繰り上がっていた。
**値が同じでも順位は当時のものではない。**

**as-of BT(`LSS_ASOF_BT`)はこの並びを直さない。** 損益タブの取引評価だけが対象。

対応:
- 過去日を指定して生成した場合、レポート上部に**橙色の警告帯**を出す(`nikkei_analysis.py` の `_asof_warn`)。当日実行では出ない。
- **当時の並びの正本を復元するツール: `show_frozen_signals.py`**
  ```
  python show_frozen_signals.py --date 2026-08-06
  python show_frozen_signals.py --date 2026-08-06 --today-html signals_holdout_all_both_2026-08-06.html
  ```
  `--today-html` を付けると『当時 vs 今』の差分と、**消えた銘柄**を列挙する。
- 実発注の正本は `orders_<日付>.csv`(`.\fills --date <yyyyMMdd> --save`)。

##### ⛔⛔ さらに悪い: 過去日の再生成は『間違った値で永久凍結』する (2026-08-08)

凍結キャッシュ `signal_score_cache_lss.json` は **書き込み1回きり**
(`run_signals_holdout_all.py:1800-1803`):

```python
if _skey not in _score_cache:                    # ← 既にあれば上書きしない(正しい値は守られる)
    _real_bt = _sig.get("rec_score", 0)          # ← **その実行時点**のBTスコア
    _score_cache[_skey] = {"bt_score": _real_bt, "first_seen": str(TODAY)}
```

**まだ凍結されていないシグナルを、過去日指定で再生成すると、決済結果を織り込んだBTが
『発生時スコア』として永久に凍結される。**

そして損益タブは **凍結 → as-of → 今日のスコア** の順に引き、凍結が見つかればそこで確定する
(`nikkei_analysis.py:7722`)。つまり **汚染された凍結値は、正しい as-of 計算を上書きする。**
`LSS_ASOF_BT=1` でも直らない。3経路の中で最もたちが悪い。

| 凍結タイミング | 判定 | 理由 |
|---|---|---|
| シグナル日 〜 **翌営業日の引け前** | ✅ 正しい | lss は翌営業日に約定・同日決済。まだ決済していない |
| **翌営業日の引け後(15:30〜)** | ⛔ **汚染** | その日の約定・決済がBTに入っている |
| 翌々営業日以降 | ⛔ **汚染** | 同上(日付だけで判別できる) |

`first_seen` は日付しか持たないので「翌営業日の引け後」は判別できなかった。
2026-08-08 から `first_seen_ts`(時刻つき)も保存するようにした
(`run_signals_holdout_all.py:1800-1810`)。それ以前の書き込みはこの判定ができず、
日付だけで正常扱いになる(監査ツールが件数を明示する)。

**監査ツール: `audit_score_cache.py`**（`first_seen` と `signal_date` を突き合わせる）

```
python audit_score_cache.py                 # 内訳を出す
python audit_score_cache.py --list          # 汚染エントリ一覧
python audit_score_cache.py --purge         # 汚染分だけ削除(.bak を自動作成)
```

削除すると as-of BT(正しい再計算)に落ちるので**消して損はしない**。
出力の「平均BT — 汚染 vs 正しい凍結」で、汚染側が高ければ先読みが効いている兆候。

**★ `--date` で過去日を再生成したあとは必ずこのツールを流すこと。**
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

---

### 18.18 ⛔⛔ LDT (ロング逆指値買い・同日決済) = 却下確定 (2026-08-08)

**方向そのものが負けEV。選定をどう作り直しても救えない。** #7 鏡像と同じ判定だが、
**#7 より深い**(#7 は PF0.54〜0.65 / LDT は PF0.64〜0.83 だが件数が桁違いで確度が高い)。

#### 決定的な証拠: `scan_long_daytrade.py --aggregate` (選定なし・全9,022ペア・159,585取引・摩擦なし)

| 基準月 | TRAIN 期待値 | TEST 期待値 | PF(TEST) |
|---|---:|---:|---:|
| 2025-09 | -420 | **-520** | 0.74 |
| 2025-10 | -429 | **-518** | 0.75 |
| 2025-11 | -444 | **-502** | 0.76 |
| 2025-12 | -452 | **-494** | 0.77 |
| 2026-01 | -404 | **-635** | 0.73 |
| 2026-02 | -374 | **-831** | 0.67 |
| 2026-03 | -396 | **-786** | 0.69 |
| 2026-04 | -412 | **-793** | 0.69 |
| 2026-05 | -435 | **-727** | 0.68 |
| 2026-06 | -479 | **-290** | 0.83 |

**TRAIN も TEST も10基準月すべてマイナス。** 勝率 23〜29% / PF は一度も1.0を超えない。
#7 と違い **TRAIN(=in-sample)ですら負け**ているので、選定は「負けEVプールから勝ちを拾って
平均回帰させていた」だけ。

戦略別 (カットオフ 2026-02-28) も全滅:

| 戦略 | TRAIN 期待値 | TEST 期待値 |
|---|---:|---:|
| MACDTF | -419 | -724 |
| A7 | -311 | -638 |
| **RSI2** | **-109** | **-1,142** |
| DON | -403 | -783 |
| VOLTF | -444 | -796 |
| MOM | -429 | -959 |

RSI2 は TRAIN が最もマシ(-109)なのに TEST が最悪(-1,142)。過剰適合の教科書的な形。

#### 選定側も同時に否定された (`scan_long_daytrade` 通常モード)

| 基準月 | TRAIN | TEST(OOS) | OOSプラス率 |
|---|---:|---:|---:|
| 2025-09 | +5,908,770 | **-3,472,887** | 31% |
| 2026-02 | +10,395,843 | **-2,753,880** | 34% |
| 2026-05 | +13,570,806 | **-1,395,793** | 42% |
| 2026-06 | +13,107,952 | +96,516 | 46% |

TRAIN は単調に増える(+590万→+1,357万)のに TEST は10ヶ月中9でマイナス。
**OOSプラス率が全月50%未満**(31〜46%)= 選ばれたペアの6〜7割が未来で負ける。
2026-06 だけプラスなのは TEST が101ペアしかない**アーティファクト**(#7 と同じ罠)。

#### ⛔ 訂正: 「LDT の粗利は +250円/件」は誤りだった

18.14 の `LDT 5,553件 / ネット -350円/件 / 粗利 +250円/件` は、
**過剰適合した選定(ホールドアウトWATCHLIST 265ペア)の上での数字**だった。
コンセプト自体の地力は **-420〜-831円/件** で、手数料を0にしても届かない。
「手数料が作った赤字だった」という 2026-08-08 の中間結論は**取り下げる**。

#### ★ 副産物: ロング/ショートの非対称は日中ドリフトの直接証拠

**同一パラメータ(sm0.1/tm1.0/同日決済)で勝率がこれだけ違う:**

| | 勝率 | 期待値 |
|---|---:|---:|
| lss (ショート) | **41%** | プラス |
| LDT (ロング) | **23〜29%** | -420〜-831円/件 |

名目ペイオフは 1:10 なので勝率25%なら PF3.3 のはず。実測 0.7 =
**ロングは +1.0ATR まで走らない**(利確に届く前に反落する)。
ショートだけが届く = 日中は下向きにドリフトしている、ということ。

→ `analyze_intraday_drift.py` の仮説(日中ドリフトが下)を強く支持する。
   同日決済でロング方向を探すのは**構造的に無理**。今後この方向を再検討しないこと。

---

### 18.19 ⛔⛔ 日中ドリフトは存在しない (2026-08-08 確定)。先物版・持ち越し案は両方とも死んだ

`analyze_intraday_drift.py --min-price 1000 --max-price 6000 --split 2026-02-01`
**604,626銘柄日 / 511営業日 / 2024-07-04〜2026-08-07 / 1,540銘柄 / 摩擦なし**

| 区間 | ショート円/件 | % | t(日) | 95%CI | 判定 |
|---|---:|---:|---:|---|---|
| overnight | -17 | -0.066 | -0.14 | -277〜+242 | **ゼロ** |
| **intraday** | **+52** | +0.021 | **0.59** | **-113〜+212** | **ゼロ** |
| open30 | -3 | -0.001 | -0.08 | -95〜+88 | ゼロ |
| morning | -21 | -0.008 | -0.48 | -115〜+70 | ゼロ |
| lunch | +30 | +0.011 | 1.41 | -11〜+70 | ゼロ |
| afternoon | -5 | -0.001 | -0.08 | -99〜+90 | ゼロ |
| close30 | +51 | +0.019 | **2.92** | +17〜+85 | 有意だが **1.9bp** |

**7区間中6つが95%CIで0をまたぐ。** 唯一有意な close30 も +0.019% = **1.9bp** で、
売買スプレッド(5〜30bp)に全く届かず**取引不能**。

『毎日1トレード』(=指数を寄りで売って引けで買い戻す)の資産曲線:
**Sharpe 0.42 / 勝日 49.3% / 月別プラス 16/26ヶ月 / 最大DD -34,925円 / 累積 +25,181円**。
TRAIN(384日) +27円/件 t=0.24、TEST(127日) +126円/件 t=0.75。**どちらも有意でない。**

#### ⛔ これで死んだもの (再検討しないこと)

| 案 | 死因 |
|---|---|
| **先物版**(日経225先物を寄りで売って引けで買い戻す) | **売るべきドリフトが存在しない**。Sharpe 0.42 |
| **持ち越し案**(引け買い→翌寄り売りでオーバーナイトを取る) | overnight も **ゼロ**(ロング +17円/件 / t=-0.14 / CI -242〜+277) |
| 時間帯を絞る案(寄り30分だけ 等) | 全区間ゼロ。絞る先が無い |

#### ⛔ 訂正: 私(Claude)が出していた2つの主張は誤りだった

1. **「上昇は夜に起きている。同日決済という制約が儲かる区間を丸ごと捨てている」→ 誤り。**
   根拠にしていた #6 の『前日終値→寄り +4,677円/件』は **88件のサブセット**(lss不約定銘柄)の
   数字で、母集団1,540銘柄・511日で測ると **-17円/件・t=-0.14 = ゼロ**。
   **持ち越しに切り替える理由は無い**(リスクだけ増える)。
2. **「日中は下向きにドリフトしている」→ 誤り。** intraday は t=0.59 でゼロ。

#### ★ ではロング/ショートの非対称(18.18)は何だったのか

同一パラメータで lss 勝率41% / LDT 勝率23〜29%。**無条件のドリフトはゼロ**なので、
この差は**条件付きの反応の非対称**である:

> **前日終値を下に割った銘柄はそのまま下げ続ける。上に抜けた銘柄は反落する。**

つまりエッジは「ドリフト」ではなく「**下ブレイクへの反応**」にある。
だから素で売ってもゼロだが、下ブレイクを待って売る lss はプラスになりうる。

#### ★ 結論: lss のエッジは機構(トリガー+損切り/利確)の側にある

素の日中ショートがゼロである以上、lss の +418円/件 は **全部** 機構由来。
そして選定(BT/属性)は 18.12/18.13 で識別力ゼロと確定しているので、残るのは

  * **トリガー**: 前日終値-1ティックの逆指値ショート(=下ブレイク待ち)
  * **決済**: sm0.1 / tm1.0 の非対称OCO

の2つだけ。**この2つが lss の全てである。** 銘柄を選ぶ発想は完全に捨てること。

#### ツールの修正 (同じ誤読を防ぐ)

* ノイズ床は intraday の σ から出しているので**他区間には使えない**旨を明示。
  区間ごとの判定は各行の 95%CI で行う。
* 判定文を符号ベースから **CIベース**に変更。実測で intraday +52円/件 を
  『素でもプラス』と表示してしまっていた(CIは -113〜+212 でゼロ)。
* 有意な区間について **bp換算**を出し、5bp未満なら「スプレッドに届かず取引不能」と警告。

---

### 18.20 ★★ エッジは『信号』にあった (2026-08-08)。選定は不要と確定

**条件を揃えて3点を測ったら、初めて『効いている部品』が特定できた。**

#### ⛔ まず: 条件を揃えないと符号が反転する (実際にやりかけた事故)

`scan_lss_universe --aggregate` の既定は **delay0 / 価格フィルタなし**。
`analyze_uncond_break` は **delay1 / 1,000〜6,000円**。この2つを並べて
「信号を付けると -18 → -400 に悪化する」という**正反対の誤結論**を出しかけた。

| 条件 | 信号のみ TRAIN | 信号のみ TEST |
|---|---:|---:|
| delay0 / フィルタなし | **-358〜-436** | -265〜-467 |
| **delay1 / 1,000〜6,000** | **+9〜+81** | **+189〜+420** |

**必ず delay と価格フィルタを揃えること**(`[info]` に表示するようにした)。

#### ★ 揃えた3点比較 (delay1 / 1,000〜6,000円 / 摩擦なし / sm0.1 tm1.0)

| | TRAIN (〜2026-01) | TEST (2026-02〜) |
|---|---:|---:|
| **信号なし**(全銘柄×全営業日・400,116約定) | **-83円/件** (t=-4.30) | +181円/件 (t=0.39) |
| **信号のみ**(選定なし・予算なし) | **+20円/件** | **+326円/件** |
| 信号+選定+予算(予算タブ11ヶ月) | — | +418円/件 |
| **信号の寄与** | **+103円/件** | **+145円/件** |

**信号の寄与が TRAIN/TEST 両方で +100〜145円/件と一致する。** これが lss で唯一
再現した効果。しかも `--aggregate` は選定を一切していないので選定バイアスが無い。

10基準月すべてで **TRAIN も TEST もプラス**(TRAIN PF 1.01〜1.08 / TEST PF 1.16〜1.32)。
本セッションでこの形が出たのは初めて。

#### ★ 選定は不要 (18.12/18.13 と整合)

| | 期待値/件 |
|---|---:|
| 信号のみ (選定なし・7,910ペア) TEST | **+189〜+420** |
| 信号+WF選定+予算上限 (11ヶ月) | **+418** |

**ほぼ同じ。** WF選定・提案ファイル・BTスコア・WATCHLIST は **何も足していない**。
そして信号のみの母集団は 7,910ペア(選定後の約2.5倍)。
**選定機構を全部捨てて母集団を広げる方が、容量も維持コストも良い。**

#### ⚠ ただし半分はレジーム

同じ信号で TRAIN +9〜+81 / TEST +189〜+420 と **4〜20倍** 違う。
最も長い TEST (2025-09基準 = 2025-10〜2026-08 の11ヶ月) で +210円/件、
その TRAIN (2024-07〜2025-09 の14ヶ月) は **+9円/件**。

**最初の14ヶ月はほぼゼロ、直近11ヶ月が良かった。** 予算タブの +418円/件 は
まるごと後者の期間に乗っている。**エッジは実在するが、水準は期間で大きく振れる。**

#### 戦略別 (カットオフ 2026-02-28) — TRAINの順位はTESTで再現しない

| 戦略 | TRAIN | TEST |
|---|---:|---:|
| DON | **+102** | +319 |
| VOLTF | +73 | **+598** |
| MACDTF | +33 | +402 |
| MOM | +33 | +486 |
| A7 | **-82** | +234 |
| RSI2 | **-114** | +398 |

TRAIN最良の DON は TEST で5位、TRAIN最悪の RSI2 は TEST で4位。
**TRAIN成績で戦略を絞ると外れる**(18.10 と同じ教訓)。6戦略とも TEST はプラスなので
**全部使う**のが正解。

#### ★ 設計への含意

lss の構成要素の最終的な内訳:

| 部品 | 判定 |
|---|---|
| **6つのロング信号** | ✅ **これがエッジ**(+100〜145円/件) |
| トリガー(前日終値-1ティック) | ✅ 必要。深くすると単調に悪化(18.19の測定) |
| 損切り遅延 delay1 | ✅ 効果大(条件を揃えた時点で符号が変わるほど) |
| sm0.1 / tm1.0 のOCO | 未再検証(TRAIN/TESTを分けたスイープが残っている) |
| WF選定・提案ファイル | ❌ **不要**。捨てて母集団を広げる方が良い |
| BTスコア | ❌ 不要(18.12) |
| 銘柄属性フィルタ | ❌ 不要(18.13) |

**発注の並び順は『予測リターン』では決められない**(BTも属性も識別力ゼロ)。
リターンが予測できずコストは予測できる以上、**流動性・スプレッドの良い順**に
並べるのが合理的。これは未実装・未検証。

---

### 18.21 ⛔ BT降順の発注順は根拠なし (2026-08-08 確定)。先読みを除くとランダム以下

#### まず: sim_portfolio_lss の BT が先読みだった

`_load_bt_pairs()` が **(銘柄,戦略)ごとの期間最大BT** を作り、それを全日の発注順と
`--bt-min` に使っていた。**「6月にBT80に達したペアが1月の注文でも最優先」= 先読み。**
`lss_trades.csv` は entry_date ごとに正しい as-of BT を持っているのに潰していた。

`--bt-mode asof`(**新既定**)で修正。(sym,strat) の (日付, BT) 時系列を作り、
注文日**以前で最も新しい**値を引き継ぐ(carry forward)。過去の値しか見ないので先読みなし。

> ⚠ 実装で1度踏んだ罠: 日付を**完全一致**で引くと、lss_trades.csv が決済済みトレード
> しか持たないため **未約定注文にBTが付かず全部落ちる**(約定率 80%→99%、TRAIN消滅)。
> 必ず carry forward で引くこと。`--bt-mode static` で旧挙動を再現できる。

#### 結果 (予算400万 / delay1 / 1,000-6,000円 / 選定844ペア / OOS)

| 発注順 | BTの取り方 | OOS損益 | 取引 | 1件あたり |
|---|---|---:|---:|---:|
| BT降順 | **static(先読み)** | **+880,170** | 1,324 | **+665円** |
| BT降順 | asof(先読みなし) | +222,938 | 1,186 | +188円 |
| **ランダム** | asof(先読みなし) | **+287,865** | 1,196 | **+241円** |

* **BT順の優位の 72% は先読みだった**(+665 → +188円/件)。
* as-of の +188〜281円/件 は 18.20 の『信号のみ』TEST(+189〜+420円/件)と一致する。

#### ★ ノイズ帯で判定した (単一シードの比較は信用しない)

ランダム順を6シード(42,1,7,99,123,2024)回して散らばりを実測:

| | OOS損益 | z | 判定 |
|---|---:|---:|---|
| ランダム 6本 | +287,865 / +288,524 / +324,668 / +327,073 / +359,112 / +421,735 | — | 平均 **+334,830** / σ **50,310** |
| **流動性順** | **+310,105** | **-0.49** | **帯の中 = ランダムと区別できない** |
| **BT降順** | **+222,938** | **-2.22** | **帯の外(全6本より下) = 有意に悪い** |

**結論は2つ:**

1. **BT降順は有意に悪い。** 6本すべてのランダムを下回る。**必ずやめること。**
2. **流動性順にバックテスト上の優位は無い。** ランダムと同じ。
   `--rank` を何にしても(BT以外なら)成績は変わらない。

#### ★ それでも流動性順を採る理由 (バックテストが見られない場所)

**このシミュは `slip=0`。流動性順の利点はバックテストに映らない。**
18.17 の実測では決済滑りの99%が6件に集中し、**全部が無保護窓明けの成行約定**だった。
成行の滑りは板の厚さで決まるので、流動性の高い順に埋めれば -1,100円/件 が構造的に減る。

> **バックテストが支持しているのではない。バックテストが盲目な部分で得をするから選ぶ。**

バックテスト上はランダムと同等(下振れなし)、実運用では執行コストぶん有利。
**「BT降順をやめる」が本体で、「流動性順にする」はその置き換え先の選び方。**

#### ★ 実装 (2026-08-08 適用済み・既定変更)

発注順は **`lss_order_rank.py` に集約**した。レポートとライブで食い違わないようにするため、
**必ずここを経由する**(直接ソートを書かない)。

| 場所 | 経由 |
|---|---|
| `nikkei_analysis.py:2520` レポートのシグナルタブ | `_lor.sort_key(rec_score, liquidity)` |
| `lss_budget_cap.py:159` 実発注 | `_lor.sort_signals(signals)` |
| `kabu_send_lss.py` `_lss_signal_today` | シグナルに `liquidity`(直近120日の平均売買代金)を付与 |
| `sim_portfolio_lss.py --rank` | **既定 liquidity**(ライブと揃える。18.9 の鉄則) |

```
set LSS_ORDER_RANK=bt          旧BT降順に戻す(比較用。有意に悪いと実測済み)
未設定                          流動性(売買代金)降順 = 既定
```

* 流動性が取れない銘柄(0)は**最後尾**に回す。板の薄さが分からない銘柄を上位に入れると
  『滑りの小さい順』という狙いが崩れるため。
* 各所で `lss_order_rank.describe()` を印字するので、実行時にどちらで並んだか分かる。
* **BTフィルタ(`--bt-min 40`)は並び順とは別の話なので触っていない。** ただしこれも
  先読み(静的BT)で測っていたので、改めて as-of で検証する必要がある(未実施)。

#### ★ 適用後の実測 (2026-08-08 / レポート予算タブ / 7ヶ月 2026-02〜08)

| 発注順 | 件数 | 勝率 | 合計損益 | 月平均 | 1件あたり |
|---|---:|---:|---:|---:|---:|
| BT降順(旧) | 1,251 | 38% | +188,300 | +26,900 | +150円 |
| **流動性順(新)** | **1,195** | **38%** | **+467,350** | **+66,764** | **+391円** |
| 差 | -56 | ±0 | **+279,050 (+148%)** | | +241円 |

**取引数は減ったのに損益は2.5倍。** 1件の質が上がっている。
+391円/件 は 18.20 の『信号のみ』TEST(+189〜+420円/件)の範囲内で整合。

月別(流動性順): 2026/02 +16,450 / 03 -5,500 / 04 +26,900 / 05 +187,100 /
06 +133,700 / 07 +134,800 / 08 -26,100。**7ヶ月中5ヶ月プラス。**

⚠ **これは1回の比較。** sim_portfolio では +222,938 → +310,105 (+39%) だったので、
改善幅はツールによって大きく違う(母集団が別: レポート=1,328ペア / sim=844ペア)。
**「+148%改善」を確定値として扱わないこと。** 向きが一致していることが根拠。

#### タブのラベルも動的にした

予算タブのボタン/説明文が「BT降順」固定だったので、`_ORD_LBL` で実態に合わせた
(既定=「流動性順」/ `LSS_ORDER_RANK=bt` なら「BT降順」)。毎日見る画面で表記と実態が
ズレると誤解のもとになる(実際 2026-08-08 にズレた)。

#### ⚠ lss_trades.csv の窓が as-of BT の測定範囲を決める

`.\daily` の表示窓ぶん(既定180日)しか書かれないので、それ以前は BT履歴が無く 0(未実証)
になり `--bt-min` で落ちる。**これは正しい挙動**(当時BTは本当に存在しなかった)だが、
TRAIN/TEST 比較をしたければ先に `.\daily --days 730 --no-serve` で窓を広げること。

#### ⛔ 18.20「選定は不要」は誤りだった (撤回)

`--aggregate` は **資金制約なしの全トレード**を測る。予算400万では1日十数件しか
建てられないので、「全部買えるなら得か」と「予算内でどれを買うか」は別問題。
順序を揃えた予算シミュ(B vs D)では **選定ありが +1,274,150 良い**。
これは 18.10 に自分で書いた教訓そのもの:

> 発注ルールの変更は必ず `sim_portfolio_lss.py`(予算・上限キャンセル込み)で検証すること。
> `compare_lss_rules` の総額比較だけで判断してはいけない。

**`--aggregate` 系の総額比較で発注ルールを決めてはいけない。**
(ただし B/D は static BT の母集団フィルタを含むので、選定効果の正確な大きさは
 asof で測り直す必要がある)


---

### 18.22 ★★ 5分足モデルは正確だった (2026-08-08)。滑り懸念は v16 で既に解決していた

`verify_stop_fill_1m.py --workers 8` / **2,703件**(2,922件中93%) / 2026-02-09〜2026-07-31

| | 合計 | 1件あたり |
|---|---:|---:|
| 5分足モデル(現行v16) | -17,571,350 | -6,501 |
| **1分足で再現** | -16,867,991 | **-6,240** |
| 1分足(武装した分の最悪値) | -18,253,141 | -6,753 |
| **差 (1分 − 5分)** | **+703,359** | **+260** |
| 差 (最悪 − 5分) | -681,791 | -252 |

**1分足で忠実に再現しても、5分足モデルとの差は +260円/件(むしろモデルの方が悲観)。**
最悪ケースでも -252円/件。**2,703件で ±260円/件 に収まる = モデルは十分正確。**

#### ★ 18.17 の『決済滑り -1,662円/件』の正体 — v16 前の測定だった

3営業日25件の突合は **v16(`fill = max(stop, そのバーの始値)`)を入れる前**に行った。
当時の『テスト』値は楽観モデル(損切りラインちょうど)だったので、差が大きく出た。

実例で確認済み: 資生堂 2026-08-07
| | 決済値 | 損益 |
|---|---:|---:|
| 旧モデル(楽観) | 3,410.0 | -3,800 |
| **v16(現行)** | **3,536.0** | **-16,400** |
| **実約定** | **3,536.5** | **-16,440** |

**v16 と実約定の差は 0.5円(40円/件)。** 最大の滑り事例は v16 で既に捕捉されている。

→ **『滑り -1,100円/件 がエッジ +391円/件 を消す』という懸念は成立しない。**
   18.21 の +467,350円(7ヶ月)を下方修正する理由は無い。

#### 無保護窓(delay1)の実像 — 5分足では見えなかった部分

| | |
|---|---|
| 武装時にすでに損切り超(=即時成行) | **570件 (21.1%)** |
| 板に逆指値を置けた | 2,133件 (78.9%) |

無保護窓の最大逆行が損切りを超えた幅:
| 50%点 | 75%点 | 90%点 | 95%点 | 99%点 |
|---:|---:|---:|---:|---:|
| **-0.02%** | +0.44% | +1.18% | +1.98% | +3.98% |

**中央値はマイナス = 半分は窓の中で損切りにすら達していない。** 走るのは裾だけ。

⚠ 最大 +100.62% は明らかにデータ異常(分割等)。MAE分布の裾を見るときは除外すること。

#### 損の集中も再現されなかった

実測25件では最悪1件が -12,650円だったが、**2,703件の1分足再現では最悪 -3,000円**、
下位20%でも差分合計の -3.5% しか占めない。**外れ値の集中は起きていない。**
= 3営業日の6件は v16 前のモデル誤差であって、実運用の構造的コストではなかった。

#### 執行改善(成行→指値)は小さい

| ルール | 1件あたり | 成行比 |
|---|---:|---:|
| 成行(現行) | -6,240 | — |
| **指値 stop+0.3%** | **-6,075** | **+165** |
| 指値 stop+0.5% | -6,157 | +83 |
| 指値 stop+1.0% | -6,317 | **-76** |

「届かなければ引けまで持つ」を含めた計算で **stop+0.3% が最良(+165円/件)**。
ただし +1.0% では悪化するので、深い指値は逆効果。効果は小さく実装リスクに見合うか要検討。

#### ⚠ この検証が見ていないもの

* **スプレッド**。1分足は約定値なので、成行買いがアスクを払うぶんは入っていない。
* 成行の実約定は武装の数秒後。本ツールは『武装した分の始値』を使う(最悪値は同分の高値)。
  真値はこの2つの間 = **+260〜-252円/件**。
* `.ills` の実測は引き続き貯める価値がある。ただし**比較対象は v16 適用後のレポート**
  であること(18.17 の -1,662円/件 と直接比較してはいけない)。


---

### 18.23 ★★★ ローリングOOS(実運用と同一パイプライン)の確定値 (2026-08-08)

**基準月ごとに選定し直し → 累積マージ → 翌月だけをOOSとして集計。`daily.bat` と同一手順。**
このセッションで判明した先読み・汚染をすべて除去した後の数字。

```
python run_oos_folds.py --workers 8 --lss-only
python sim_oos_budget.py --raw "oos_raw_fold*.csv" --bt-mins 40
```

| シムタイプ | 月数 | 取引 | 勝率 | 損益 | 月平均 |
|---|---:|---:|---:|---:|---:|
| **通常予算**(実運用に最も近い) | 10 | **1,916** | **38.8%** | **+501,350円** | **+50,135円** |
| 約定額ベース | 10 | 2,059 | 39.3% | +521,750 | +52,175 |
| ループ充填_全戦略 | 10 | 2,885 | 38.9% | +620,700 | +62,070 |
| ループ充填_絞り(A7/RSI2/VOLTF) | 10 | 2,242 | 36.5% | +139,800 | +13,980 |

**1件あたり +262円** (501,350 / 1,916)。18.20 の『信号のみ』TEST(+189〜+420円/件)の
範囲内で整合。予算400万に対し **月利約1.25%**。

**絞り版(A7/RSI2/VOLTF)が大きく劣る**のは 18.20 と整合(TRAINの戦略順位はTESTで
再現しない。6戦略とも使うのが正解)。

#### この数字に含まれている条件(すべて実機と一致)

| | |
|---|---|
| as-of BT | ✅ ON (18.11) |
| per-symbol START_DATES | ✅ (B1リーク除去) |
| 手数料 | ✅ 0 (実口座と同じ / 18.14) |
| 損切り約定 | ✅ v16 現実モデル。1分足2,703件で ±260円/件 と検証済 (18.22) |
| 損切り遅延 | ✅ delay1 (watch.bat と一致) |
| 発注順 | ✅ 流動性(売買代金)降順 (18.21) |
| 転換 | ✅ **除外** (18.5.3。実装不可) |
| 選定 | ✅ 基準月ごとに選定し直し+累積マージ(= daily.bat) |

#### ⛔ ここに至るまでに潰した汚染 (同じ轍を踏まないこと)

| # | 何 | 影響 |
|---|---|---|
| 1 | `run_oos_folds` が **`LSS_ASOF_BT` 未設定**(=先読み) | 18.11 実測で6ヶ月 -88% |
| 2 | 同 `LSS_STOP_DELAY_BARS=2`(実機は1) | live と不一致 |
| 3 | 同 env 4つ未クリア(シェルの研究フラグが漏れる) | 条件が変わる |
| 4 | `sim_oos_budget` が **転換を予算シミュから除外していなかった** | 転換 359件 **+447,635円**(lss本体は 7,931件 -46,646円) |
| 5 | 同 発注順が **BT降順のまま**(18.21 で流動性順に統一済みだった) | z=-2.22 の劣る並び |
| 6 | 生CSVに `liquidity` 列が無く流動性順を再現できない | 列を追加した |

**④が最大。報告値のほぼ全部が転換由来だった。**
内訳セクションは除外していたのに本体が除外していない、という取り残し。

#### 比較

| 測定 | 期間 | 月平均 | 備考 |
|---|---|---:|---|
| レポート予算タブ(18.21) | 7ヶ月 | +66,764円 | 表示窓の集計。選定の累積構造は再現していない |
| **ローリングOOS(本節)** | **10ヶ月** | **+50,135円** | **実運用で得られたはずの成績** |

ローリングの方が **25%低い**。表示窓の集計は選定を1回分だけ使うので、
毎月選定し直す実運用より甘く出る。**採否の判断はローリング側で行うこと。**

---

### 18.24 ★★ ノイズ帯で判定する (2026-08-08)。BT40 → 30 に変更。発注順は差が測れない

ローリングOOS(18.23)の生CSV 10フォールドを使い、**ランダム順を12シード回して
ノイズ帯を作ってから**判定した。これをやるまで、帯の中の差を「改善」と読んでいた。

#### ★ まず結論: 発注順は何にしても区別できない

BT≥40 / 予算400万 / 通常予算 / 10ヶ月:

| 発注順 | OOS損益 | z | 帯内順位 |
|---|---:|---:|---|
| ランダム 12本 | +211,311 〜 +656,833 (平均 **+421,241** / σ **124,660**) | — | 基準の帯 |
| **流動性順(既定)** | +397,350 | **-0.19** | 8/13 |
| **BT降順** | +511,490 | **+0.72** | 9/13 |

**両方とも帯のど真ん中。** σ が10ヶ月合計の約30%あるので、単発比較の差は全部埋まる。

⛔ **18.21 の「BT降順は有意に悪い(z=-2.22)」は再現しなかった**(ここでは z=+0.72)。
   18.21 は `sim_portfolio_lss`(選定844ペア)、こちらは実運用と同一パイプラインの
   ローリングOOS。母集団が違う。同じく 18.21 の
   「BT降順 +188,300 → 流動性順 +467,350 (+148%)」も**この帯の中**で、改善として扱えない。

**それでも流動性順は既定のまま**。理由は 18.21 に書いたとおりで変わらない
(バックテストは slip=0 なので執行コストの利点が映らない。帯の中＝下振れしないので、
映らない部分で得をするほうを選ぶ)。

#### ★ 利益そのものも有意ではない

| BT≥40 / 流動性順 / 10ヶ月 | |
|---|---:|
| 合計 | +397,350円 |
| 月平均 | +39,735円 |
| 月次σ | 106,454円 |
| **t** | **+1.18** |
| **95%CI** | **-36,412 〜 +115,882 円/月** |
| プラス月 | 6/10 |

**信頼区間がゼロをまたぐ。** t=2 に到達するには **約29ヶ月**必要。

⚠ 18.15 の「t=3.36 / CI全域プラス」は**表示窓の集計**(選定を1回分だけ使う)。
  こちらはローリング。18.23 の方針どおり **採否はローリング側で判断する = t=+1.18 が正**。

#### ★ BT閾値 40 → 30 に変更 (実施済み)

同データのBT層スイープ(流動性順):

| BT閾値 | 取引 | 勝率 | 10ヶ月合計 | 月平均 | t=2 到達に必要な月数 |
|---:|---:|---:|---:|---:|---:|
| **30**(=プールの床=**フィルタなし**) | 2,053 | 40.2% | **+693,375** | **+69,338** | **9ヶ月** |
| 40 (旧) | 1,902 | 38.3% | +397,350 | +39,735 | 29ヶ月 |
| 50 | 1,695 | 38.5% | +519,125 | +51,912 | — |
| 60 | 1,322 | 40.6% | +419,390 | +41,939 | — |
| 70 | 811 | 39.5% | +205,795 | +20,580 | — |

**非単調で、最も緩い BT30 が最良。** 18.12(勝率が全帯フラット)・18.15(BT40 は BT0 に
-122,294円 劣る)と3回続けて同じ向き。しかも BT40 は**測定能力まで削っていた**
(29ヶ月 vs 9ヶ月)。差そのものは帯の中なので「確定した改善」ではない。論拠は
**フィルタが何も買っていないのに取引を減らしている**という消去法。

`_BUD_MIN_BT` は `max(30, ...)` でハード下限30なので、**30 が指定できる最小値**
(= 追加フィルタなし)。変更したのは env 1つ:

| 場所 | 旧 | 新 |
|---|---|---|
| `daily.bat` / `dailyfast.bat` `LSS_BT_TAB_MIN` | 40 | **30** |
| `run_oos_folds.py` / `sweep_lss_oos_monthly.py` / `sweep_oos.bat` | 40 | **30** |

戻すなら `set LSS_BT_TAB_MIN=40`。

※ `sweep_lss_oos_monthly.py` と `sweep_oos.bat` は `LSS_STOP_DELAY_BARS` が **2** で
  実機(daily.bat/watch.bat = 1)と食い違っていたので **1 に合わせた**。18.10.1 の
  「実機が正」に従う。

※ ライブの発注リスト(シグナルタブ)は**もともとBTで絞っていない**ので、この変更で
  発注銘柄は変わらない。変わるのはレポートの予算タブ/BT帯タブの集計。
  `lss_budget_cap.py --bt-min` は既定0(=絞らない)。**18.5.4 の推奨コマンドに
  書いてある `--bt-min 40` は使わないこと。**

#### ⛔ ツールのバグ2件 (どちらも結論を歪めていた)

1. **`sim_oos_budget._load_one_raw_csv` が `liquidity` 列を読んでいなかった。**
   `sort_key` は流動性0を最後尾に回して同値をBT降順にするので、
   **`LSS_ORDER_RANK=liquidity` のつもりで BT降順で走る**。修正済み。
   (旧CSVには列が無いので、値が無いときだけ `_liq_of` にフォールバック)
2. **`analyze_oos_edge` の帰無較正が縮退していた。** 日付ラベルだけを巡回させると
   日ブロックが丸ごと移動し `groupby("date")` の分割が元と同一のまま = t が不変。
   毎回まったく同じ候補数が出る。**損益の側を日ブロック単位で回す**のが正しい。

#### 選定属性の再探索 = やはり候補ゼロ (3回目)

`analyze_oos_edge.py --raw "oos_raw_fold*.csv" --bt-min 40`:
BTスコア / 流動性 / 建値 / 同日件数 / 銘柄重複 / **日内流動性順位** / 日内順位率 の
7属性×5分位 + 戦略 + 曜日 → **候補0個**(帰無の95%点 2個)。18.12 / 18.13 と同じ。

- **予算キャップの切り捨て自体にもエッジは無い**。「日内流動性上位N件だけ買う」を
  直接測ると N=5:+210 / N=13:+73 / N=30:+298 / 全部:-49 円/件 と非単調で t は全て1.3未満。
  前半が全部マイナス・後半が全部プラスで、**Nの効果よりレジーム差の方が桁違いに大きい**。
- **木曜だけ目につくが採用不可**。t=-2.12 / 円/件 -1,416 だが、損失の74%が3日に集中し、
  最悪5日を除くと **+11円/件**(t=+0.05)。全体の最悪5日のうち4日が木曜 =
  「木曜が悪い」ではなく「大きな逆行日がたまたま木曜に寄った」。fold別も 6/10 しか
  マイナスでない。5曜日試した後の |t|=2.12 は多重検定で消える。

#### ★ 次の一手 = sm/tm。ただし『名目と実態が乖離している』ことだけが根拠

実測(BT≥40 / 4,980件)で、決済が設計どおりに動いていないことが分かった:

| | 名目(設計値) | **実測** |
|---|---|---:|
| ペイオフ比 | sm0.1 / tm1.0 = **10:1** | **1.50:1** |
| 平均利益 / 平均損失 | — | +5,528 / -3,693円 |
| 損失の中央値(建玉比) | 0.1ATR ≈ 0.2〜0.3% | **-0.82%**(約3倍深い) |
| 利益の中央値(建玉比) | 1.0ATR ≈ 2〜3% | **+1.19%**(約半分) |
| 理論PF(勝率39.4%×10:1) | **6.50** | **0.98** |

損切りは名目の約3倍深く(delay1の無保護窓+ギャップ約定 / 18.17・18.22 と整合)、
利確は大半が 1.0ATR に届かず**引けでタイムカット**されている。
つまり `sm=0.1 / tm=1.0` という数字は**実際にはその意味を持っていない**。

⚠ ただしこれは「良くなる」保証ではない。掃いた項目(BT・属性・発注順・delay深さ・
  トリガー深さ)は全部ヌルだった。**土台の t が +1.18 しかないので、スイープで
  +15万円出ても帯の中**である点にも注意。

`sweep_lss_smtm.py` を **TEST窓を複数回せる**ように改修した(`--holdout-list`)。
窓1本の結果はノイズと区別できないので、**全窓で同じ推奨が出るときだけ**採用する。

```
python sweep_lss_smtm.py --sm-list 0.1,0.2,0.3,0.5 --tm-list 1.0,1.5,2.0 \
                         --holdout-list 60,120,180 --workers 8
```

採用前に必ず `sim_portfolio_lss.py`(予算・上限キャンセル込み)で再検証すること(18.10)。

#### ★ この節から持ち帰る作法

**発注ルール・パラメータの変更は、必ず「ランダム/代替条件の帯」を作ってから判定する。**
2条件を1回ずつ比べて差を語ってはいけない。今回の実測では、発注順を入れ替えるだけで
**σ=124,660円 / 10ヶ月**動く。それ未満の差は『測れていない』のであって『改善した』ではない。

---

### 18.25 ⛔ `compare_lss_rules --holdout-days` が効いていなかった (2026-08-08 修正)

**`--holdout-days N` は下限しか動かしておらず、直近N日が TRAIN に丸ごと残っていた。**
つまり TRAIN ⊇ TEST で、**TEST は in-sample の部分集合**だった。

```python
# 修正前 (compare_lss_rules.py:468) — 一方向フィルタしかない
if pd.Timestamp(fd) < TODAY - pd.Timedelta(days=_bt_window):
    continue
```

`TODAY` は N日前へずらしていた(:124)ので下限だけが動き、上限が無いため
今日までのトレードが全部残る。:123 のコメント「直近N日のデータは一切参照されない」は
**実装されていなかった**。

#### 発覚のしかた (この形を覚えておくこと)

`sweep_lss_smtm --holdout-list 60,120,180` で **TRAIN が3窓とも1円まで同一**だった:

| | 件数 | net現実 |
|---|---:|---:|
| TRAIN60 / TRAIN120 / TRAIN180 (sm0.1 tm1.0) | いずれも 13,805 | いずれも **+4,981,237** |

窓が違えば TRAIN も違うはず。同一 = 窓が効いていない。
(TEST は 2,062 / 4,029 / 6,200 と `--days` どおり変化していたので、
 `--days` は効いていて `--holdout-days` だけが効いていなかった)

#### 修正

`_HOLDOUT_CUT` を導入し、`fd > _HOLDOUT_CUT` のトレードを除外する。
BTスコア用の `bt_trades` を作る前に掛けるので、**BTが未来の決済結果を見るのも同時に防ぐ**。

`sweep_lss_smtm.py` 側にも「TRAIN が窓をまたいで同一なら分割が壊れている」検知を追加。

#### ⛔ 影響: これで測った過去の結論は測り直しが必要

| 記録 | 何を `--holdout-days` で測っていたか |
|---|---|
| §18.10 | 戦略別BT閾値の順位表を「直近120日除外で作った」— 除外できていなかった |
| §18.24 の sm/tm スイープ (2026-08-08) | TRAIN/TEST 分割そのものが不成立 |

§18.10 の結論(戦略別閾値は不採用)自体は **ローリングOOS(`run_oos_folds` + `sim_oos_budget`)で
別途否定されている**ので変わらない。順位表の作り方だけが誤っていた。

**★ 教訓: ホールドアウトは『下限をずらす』ではなく『上限で切る』。**
実装を信じず、**窓を変えて TRAIN の数字が動くか**を必ず確かめること。動かなければ効いていない。

---

### 18.26b ⛔⛔ 転換の再検証 (2026-08-10)。**70升 / 365日 / TRAIN+TEST で確定。二度と掘らない**

`.\tksweep` (= `analyze_tenkan_cutoff --days 365 --bt-min 30 --holdout-days 120`
締切10 × 決済7 = 70升 / 10,902シグナル / delay1 / v17)。18.26(240日・32升)より広く、
**TRAIN/TEST 分割つき**。結論は同じで、より強い。

**基準 = 純lss(転換なし) +12,262,500円 / 9,649件 / 勝率53.1%**

純lss比(円)。転換が実際に建つ升は **70升すべてマイナス**:

| 締切 | 0/0@15:25 | **0.1/1.0@15:25** | 0.3/1.5@15:25 | 0.5/2.0@15:25 |
|---|---:|---:|---:|---:|
| 09:05 | −7,945,492 | **−3,603,459** | −5,371,197 | −6,544,225 |
| 09:30 | −4,293,744 | **−1,915,824** | −2,978,149 | −3,436,188 |
| 11:00 | −2,133,648 | **−788,872** | −1,419,751 | −1,776,982 |
| 13:00 | −1,187,565 | **−503,589** | −850,886 | −977,180 |
| **14:00** | −680,216 | **−436,304** ← 実装可能な最良 | −487,334 | −592,290 |
| 全部転換 | −24,129,484 | −17,949,908 | −22,292,800 | −23,435,214 |

TRAIN/TEST も一致(14:00 × 0.1/1.0: TRAIN −343,188 / TEST −93,116)。期間ノイズではない。

#### ★ OCO は効く。ただしプラスには届かない

ユーザー指摘「せめて損切と利確は設定すべき」は**正しかった**。全10締切で
`0.1/1.0@15:25` が最も損失の小さい列で、09:05 では −3,525,677 → **−2,019,926** と半減。
そして 0.5/2.0 のような広い設定は逆に悪化 = **0.1/1.0(lssの鏡像)が最適**。
**それでもどの升もプラスにならない。** パラメータの問題ではない。

#### 約定時刻帯 × 損益 (構造の確認)

| 約定時刻帯 | 件数 | lss損益 | 同じ銘柄を転換したら |
|---|---:|---:|---:|
| 〜09:05 | 7,521 | **+10,704,250** | −5,214,409 |
| 09:06〜09:10 | 359 | +506,250 | −631,554 |
| 09:11〜09:30 | 608 | +450,900 | −1,950,497 |
| 09:31〜11:00 | 607 | +445,850 | −2,379,820 |
| 11:01〜 | 554 | +155,250 | −937,360 |
| **終日不約定** | 1,253 | — | **+1,441,827** |

**全時間帯で lss > 転換。** プラスは終日不約定だけ = 大引けまで分からない = 実装不可。

#### 副産物: 『13:00以降に約定した lss は建てない』(§18.9 の --entry-cutoff)

| 締切 | 捨てた件数 | 純lss比 | TRAIN | TEST |
|---|---:|---:|---:|---:|
| **13:00** | 1,513 | **+27,150** | **+13,850** | **+13,300** |
| 14:00 | 1,386 | +10,750 | +13,800 | −3,050 |
| 14:30 | 1,345 | −11,500 | −12,600 | +1,100 |
| 11:30 | 1,709 | −78,300 | −48,450 | −29,850 |

13:00 だけ TRAIN/TEST 両方プラスだが、**+27,150 は基準1,226万の0.2%・月次σ(≒12万)の1/5**。
採用の根拠にならない。18.26 が delay1 で −28,050 としていたのは240日窓の値で、
どちらもノイズ帯の内側 = **どちらでもよい(現状=カットオフ無しのまま)**。

#### レポートの転換タブ (09:09締切・実装可能) の実測

`.\dailyfast` 180日窓: **993件 / 勝率28.3% / PF0.34 / −2,043,558円 / 7ヶ月すべてマイナス**。
旧表示(終日不約定のみ=look-ahead)は +545,235円 だった。**この差が先読みの正体。**
必要資金も最大 9,414,555円(34銘柄同時)で、**予算400万では建てられない**。

#### ★ 結論

**転換は発注ルールに組み込まない。タブは記録専用。この方向はもう掘らない。**
再検証したくなったら `.\tksweep` を1回流せば同じ表が出る。**掘り直す前にこの節を読むこと。**

---

### 18.26 ⛔⛔ 転換は実装可能な形では全滅 (2026-08-08 確定)。この方向は閉じる

**締切=買い時刻に連動させ、決済ルール(時間 / OCO)も並べて測ったら、実装できる32セルが
全部マイナスだった。** §18.5.3 の結論を、時間整合の取れた形で再確認。

#### 測定条件

`analyze_tenkan_cutoff.py --days 240 --bt-min 30 --workers 8 --by-month`
628銘柄 / 5,821シグナル / lss約定 5,208件(89.5%) / delay1 / 1,000〜6,000円

**基準 = 純lss(転換なし) +7,406,600円 / 5,208件 / 勝率52.5%**

#### 純lss比 (円)。実装可能なセルは**全部マイナス**

| 締切(=買い時刻) | 0/0@11:30 | 0/0@15:25 | 0.1/1.0@15:25 | 0.3/1.5@15:25 |
|---|---:|---:|---:|---:|
| 締切なし(現行) | +1,023,664 | +1,453,799 | +303,458 | +1,043,954 |
| 09:05 | -4,361,866 | -5,135,729 | -2,342,093 | -3,184,555 |
| 09:30 | -1,906,761 | -2,666,232 | -1,142,094 | -1,601,764 |
| 10:00 | -1,193,205 | -1,892,156 | -893,289 | -1,174,565 |
| 11:00 | -464,606 | -1,146,909 | -452,573 | -726,769 |
| 11:30 | (縮退) | -912,914 | -342,683 | -501,570 |
| 12:30 | (縮退) | -842,274 | -344,090 | -431,079 |
| 13:00 | (縮退) | -490,867 | -215,838 | -303,461 |
| 14:00 | (縮退) | -203,233 | -143,946 | -156,407 |
| 全部転換 | -12,766,341 | -13,130,209 | -10,107,038 | -12,030,409 |

**締切が遅いほどマイナスが小さくなり、ゼロに漸近するだけ。** 一度もプラスにならない。

#### ★ 構造: 待つほど確信は増すが、取れる時間が消える

『締切なし(現行)』の +1,023,664円 は **大引けまで待って初めて分かる**値(18.5.3)。
締切を遅らせるほどそこに近づくが、**近づくほど大引けまでの保有時間が無くなる**ので
実際に取れる額は減る。両者が打ち消し合って、実装可能な領域では常にマイナスになる。

決済ルールをどう変えても向きは同じ:
- 大引けまで持つ(15:25)より **前場引け(11:30)のほうがマシ** = 長く持つほど悪い
- OCO(0.1/1.0 や 0.3/1.5)を置いても改善しない。早い締切では OCO のほうがマシだが
  (損失を切るので)、遅い締切では時間決済に劣る

#### 決済ルール別の月別も一貫 (9ヶ月)

4ルール × 全締切で、プラス月が過半を超える組み合わせは**ひとつも無い**。
最もマシな 14:00 × 0/0@11:30 でも 9ヶ月中プラス5ヶ月・合計 -34,250円。

#### ⚠ 『最良セル』の読み違いに注意 (ツールを修正した)

締切 >= 11:30 かつ 決済 0/0@11:30 のセルは **売り時刻 <= 買い時刻で建てられない**ので
転換0件になる。その値は転換の成績ではなく『**締切以降に約定した lss を捨てただけ**』の
効果。初回の出力は「最良 = 13:00 × 0/0@11:30 (-28,050円)」と表示したが、これは
転換0件の縮退セルだった。`*` 印を付け、最良セルの選定から除外するようにした。

#### 副産物: 遅い約定を捨てる効果は ほぼゼロ〜わずかにマイナス (delay1)

上記の縮退セル = §18.9 の `--entry-cutoff` と同じ問い:

| 締切 | 捨てた件数 | 純lss比 |
|---|---:|---:|
| 13:00 | 137 | **-28,050** |
| 14:00 | — | -34,250 |
| 12:30 | — | -130,950 |
| 11:30 | — | -133,850 |

**遅い約定は切らないほうがよい**(わずかに)。§18.9 は delay2 で 13:00 カットオフ
+26,299円 としていたが、**実機の delay1 では -28,050円 で符号が逆**。
どちらも数万円でノイズ帯(18.24: σ=124,660円/10ヶ月)の内側なので、
**どちらでもよい = 現状(カットオフ無し)のままでよい**。

#### 約定時刻帯 × 転換 (参考。買い09:09固定 = 実装不可)

| 約定時刻帯 | 件数 | lss損益 | 同じ銘柄を転換したら |
|---|---:|---:|---:|
| 〜09:05 | 4,065 | +6,168,350 | -2,416,315 |
| 09:06〜09:10 | 190 | +405,400 | -469,556 |
| 09:11〜09:15 | 117 | +114,450 | -607,395 |
| 09:16〜09:30 | 199 | +178,850 | -683,457 |
| 09:31〜10:00 | 166 | +164,150 | -692,828 |
| 10:01〜11:00 | 158 | +217,400 | -897,600 |
| 11:01〜 | 313 | +158,000 | -616,255 |
| **終日不約定** | 613 | — | **+1,023,664** |

**全時間帯で転換が lss に負ける。プラスは終日不約定だけ**(18.5.3 の再確認)。

#### ★ 結論

**転換は発注ルールに組み込まない。この方向は閉じる。**
`.\tenkan` / レポートの転換タブは **実績記録専用**。「もし転換していたら」の記録で
あって、戦略として扱わない。

⚠ レポートの損益に転換が混ざる点は変わらないので、数字を見るときは必ず除外すること
(18.13: 転換を含めると通期が黒字に見える)。

---

### 18.27 ★★ 5分足の株式分割 未調整 (2026-08-08 原因確定・修正済み)

**同じトレード集合で base(delay0) は健全なのに、delay1 だけ TRAIN窓で崩壊する。**

| 窓 | 件数 | base | delay1 | 差 | 円/件 |
|---|---:|---:|---:|---:|---:|
| TRAIN60 | 6,669 | +5,546,383 | **-34,163,917** | -39,710,300 | **-5,954** |
| TRAIN120 | 5,261 | +4,223,817 | -17,900,783 | -22,124,600 | -4,205 |
| TRAIN180 | 2,964 | +2,399,217 | -11,775,183 | -14,174,400 | -4,782 |
| TEST60 | 209 | +394,800 | +413,400 | +18,600 | **+89** |
| TEST120 | 1,054 | +1,998,250 | +2,576,400 | +578,150 | **+549** |
| TEST180 | 2,348 | +3,482,100 | +4,310,100 | +828,000 | **+353** |

**TEST(ホールドアウト無し)の delay1 効果 +89〜+549円/件 は §18.9.1 の +222円/件 と整合。
TRAIN(`--holdout-days` あり)だけ -4,200〜-6,000円/件 と10〜20倍逆向き。**

`_exit` / `_stop_fills` は日中の5分足配列だけで完結し `TODAY` に依存しない。
つまり計算式ではなく **どのトレードが母集団に入るか** が違う
(BTフィルタの基準日が TODAY と一緒にずれるため)。

#### ⛔ 仮説①「損切りが arm せず引けまで持つ」= **否定された** (cmp_reason.py)

決済理由の内訳は TRAIN も TEST もほぼ同じだった:

| 窓 | target | stop | close(引け) |
|---|---|---|---|
| TRAIN60 | 1,949→2,131 | 3,029→2,579 | 1,691→1,959 (25%→29%) |
| TEST60 | 74→77 | 73→67 | 62→65 (30%→31%) |
| TRAIN180 | 968→1,032 | 1,220→1,072 | 776→860 (26%→29%) |
| TEST180 | 715→776 | 991→860 | 642→712 (27%→30%) |

**件数の動き方は同じ。違うのは1件あたりの損失額。**

#### 仮説②: 窓埋め約定 `max(line, opens[stop_from])` の片側エラー

`base` は `stop_from == ei` なので `real = line` となり **`opens[]` を一度も読まない**。
`delay1` だけが5分足の始値を読む。5分足の値が日足由来の損切りラインから
何らかの理由でずれていると、`max()` が常にそちらを拾って**片側にだけ**損失が出る
(ずれが下向きなら line が選ばれて無害 = 一方向にしか効かない)。

PF から逆算すると TRAIN の損切りは平均で建値の **約5.5%** 上で約定している計算になる
(TEST は約0.7%)。損切りラインは +0.1ATR ≈ +0.25% なので **22倍の超過**。
5分足2本目の始値が平均5.5%飛ぶのは相場現象としては考えにくい。

#### ★★ 原因確定 = **5分足の株式分割 未調整** (audit_delay_diff.py)

TRAIN60(6,669件)のペナルティ **-42,457,500円のうち 91.6% が最悪100件**に集中。中身:

| 銘柄 | 建値 | 決済値(逆算) | 倍率 | MAE |
|---|---:|---:|---:|---:|
| 7013.T ＩＨＩ | 2,410 | 16,655 | **6.91x** | 601% |
| 1518.T 三井松島 | 1,380 | 6,940 | **5.03x** | 407% |
| 8392.T 大分銀行 | 2,004 | 10,022 | **5.00x** | 404% |
| 5741.T ＵＡＣＪ | 1,625 | 6,510 | **4.01x** | 304% |
| 4368.T 扶桑化学 | 3,100 | 9,500 | **3.06x** | 210% |
| 7628.T オーハシ | 1,190 | 2,418 | **2.03x** | 103% |

**倍率がきれいに 2/3/4/5/7 倍 = 株式分割。** MAE 601% は「1日で株価7倍」で、
東証の値幅制限がある以上 **物理的に不可能**。

機構:
- 5分足(`stock_5min`)は **ダウンロード時のまま**保存され、後から分割が起きても再調整されない
- 日足(yfinance)は分割を **遡及調整** する
- 注文値(トリガー/損切り/利確)は日足由来
- → 分割銘柄の分割前の日は「5分足 = 日足 × 分割比」になる
- → `max(line, bar_open)` は **片側にしか効かない**ので、5分足が高い側なら常にそちらが選ばれる
- → **`base` は `real = line` で `opens[]` を一度も読まないので無傷**。delay1 だけ壊れて見えた

TEST窓(直近180日)は MAE最大 9.9% でクリーン。**古い窓ほど「その後に分割した銘柄」が
増える**ので、TRAIN(古い期間)だけ壊れるという観測と完全に一致する。

#### 対処 (実装済み)

`intraday_integrity.py`: **同じ日の5分足の終値中央値と日足終値を突き合わせ、
30%以上ずれていたらその銘柄日を使わない**(日本株は値幅制限があるので正常なら数%以内)。

| 場所 | 状態 |
|---|---|
| `compare_lss_rules`(研究) | ガード適用 |
| `backtest_limit_entry`(**エンジン = レポート/OOS/発注判断**) | ガード適用・既定ON |
| 切り戻し | `set LSS_NO_INTEGRITY_GUARD=1` (BTキャッシュは `noig` トークンで別管理) |
| BTキャッシュ版 | `v16` → **`v17`** |

#### ★ 汚染範囲の実測

**検査 166,962銘柄日 / 汚染 2,311日 (1.38%) / 30銘柄。**

| 比率 | 日数 |
|---|---:|
| 2倍(1:2分割) | 1,253 |
| 3倍 | 510 |
| 4倍 | 284 |
| 5倍 | 148 |
| 10倍 | 56 |

**すべて整数倍で端数の混入なし。** 各銘柄の汚染期間は必ず分割の権利落ち日で終わる
(9984.T ソフトバンクG = 4倍で 2025-12-26まで / 3193.T = 2倍で 2026-07-29まで)。
開始が全銘柄 2025-07-07 で揃うのはスキャン窓(400日)の先頭だから。

`check_contamination.py` による突合:

| 対象 | 取引 | 汚染 | 影響 |
|---|---:|---:|---|
| ローリングOOS 生CSV | 4,513 | **0件** | **汚染なし。18.23/18.24 は撤回不要** |
| `lss_trades.csv`(レポート) | 5,623 | **1件** | **-173,500円**(3193.T) |

レポートは1件で **+2,117,600 → +2,291,100円**(+377 → +408円/件)。
OOS が無傷なのは、汚染期間が OOS窓(2025-10〜2026-07)より手前に集中しており、
OOS に登場する銘柄の取引がすべて権利落ち日より後だったため。

⚠ 直接の -173,500円 だけでなく、**その取引が入ったペアのBTスコアも潰されていた**
(BTは過去トレードの成績から作る)。順位・予算タブの選択にも波及していたので、
v17 で測り直すと数字だけでなく**並びも変わる**。

#### ★ v17 で再構築した実測 (2026-08-08)

`.\daily --no-serve` → `check_contamination.py --trades lss_trades.csv`:

| | 件数 | 合計 | 1件あたり |
|---|---:|---:|---:|
| v16(汚染あり) | 5,623 | +2,117,600 | +377円 |
| **v17(ガード後)** | **5,606** | **+2,308,550** | **+412円** |
| 差 | -17 | **+190,950** | +35円 |

**汚染日の取引 0件**を確認。予測(+173,500円)とほぼ一致し、残りは表示窓が1日
ずれたこと(2026-02-09→02-10)と、汚染BTが消えたことによる並び替えの効果。

`audit_score_cache.py` は 1,358件 100%正常(汚染0件)。`[BT出所] 今日のスコア=0件`、
発注順=流動性降順、キャッシュ `bt_v17sd1_*` も確認済み。

#### 再現・確認コマンド

```
python compare_lss_rules.py --days 365 --holdout-days 60 --bt-min 30        --min-price 1000 --max-price 6000 --workers 8 --sm 0.1 --tm 1.0 --audit delay1
python compare_lss_rules.py --days 180 --bt-min 30        --min-price 1000 --max-price 6000 --workers 8 --sm 0.1 --tm 1.0 --audit delay1
python audit_delay_diff.py
```

```
python intraday_integrity.py --symbols-file holdout_selected_symbols.py --days 400
python check_contamination.py --trades "oos_raw_fold*.csv"
python check_contamination.py --trades lss_trades.csv
```

根治するなら **5分足を分割調整して再保存**する(ガードは該当日を捨てるだけなので、
その日のトレードは検証から消える)。

```
python cmp_reason.py                     # 出力済みCSVを読むだけ
python compare_lss_rules.py --days 365 --bt-min 30 --min-price 1000 --max-price 6000 \
       --workers 8 --sm 0.1 --tm 1.0     # ホールドアウト無しの365日。base と delay1 を比較
python compare_lss_rules.py ... --holdout-days 60 --audit delay1   # 全トレード監査
```

**⛔ v17 より前に測った delay 系の数字は使わないこと。**

#### 副産物: base(delay0) の sm/tm スイープは健全で、初めてヌルでない結果が出た

同じ再解析を `--rule base` で読み直すと TRAIN/TEST の順位相関が
**+0.89 / +0.94 / +0.94** と3窓すべてで強く一致する(delay1 は -0.43〜-0.77)。

sm(損切ATR倍率) の TEST平坦域:

| 窓 | 平坦域 | 現行 0.1 |
|---|---|---|
| 60日 | [0.3, 0.7, 1.0] | 圏外(平坦域の45%) |
| 120日 | [0.5, 0.7, 1.0] | 圏外(55%) |
| 180日 | [0.5, 0.7, 1.0] | 圏外(58%) |
| **共通集合** | **[0.7, 1.0]** | **3窓すべてで圏外** |

tm(利確) は3窓とも [1.0, 1.5, 2.0] が平坦 = **現行1.0のままでよい**。

⚠ ただし **これは delay0 の結果で、実機は delay1**。sm と delay は
どちらも損切りを緩める方向なので**代替関係**にあり、delay1 の下では
sm を広げる利得の一部が既に取れている可能性がある。
**delay1 の測定が直るまで sm は動かさないこと。**

---

### 18.28 ⛔ sm/tm は変えない (2026-08-08 確定)。「改善」の正体は件数だった

v17(分割ガード後)で delay1 の測定が直り、sm/tm を実機条件で判定できるようになった。

```
python sweep_lss_smtm.py --sm-list 0.1,0.3,0.7,1.0 --tm-list 1.0,1.5 \
                         --holdout-list 60,180 --bt-min 30 --workers 8
```

#### まず: v17 で delay1 の測定が直った

| 窓 | v16(汚染あり) | **v17(ガード後)** |
|---|---:|---:|
| TRAIN60 sm0.1 tm1.0 | **-34,163,917** | **+8,946,450** |
| TRAIN180 sm0.1 tm1.0 | -11,775,183 | +3,779,950 |

18.27 の分割汚染が主因だったと確定。TRAIN/TEST の符号違い警告も出なくなった。

#### ツールの判定は「sm を [0.7, 1.0] へ動かす根拠がある」と出た

| 窓 | Spearman | TEST平坦域 | 現行0.1 |
|---|---:|---|---|
| 60日 | +0.80 | [0.3, 0.7, 1.0] | 圏外(48%) |
| 180日 | +0.80 | [0.7, 1.0] | 圏外(71%) |
| **共通集合** | | **[0.7, 1.0]** | **両窓で圏外** |

base(delay0) で出た結論と**完全に一致**していた(18.27 の副産物)。

#### ⛔ しかし分解すると 1件あたりは動いていない

| 窓 | sm0.1 | 0.3 | 0.7 | 1.0 | 1件あたり改善 | 件数増 |
|---|---:|---:|---:|---:|---:|---:|
| TRAIN60 | +1,285 | +1,302 | +1,258 | +1,209 | **+1%** | +34% |
| TEST60 | +1,984 | +2,707 | +2,764 | +2,681 | +39% | +44% |
| TRAIN180 | +1,178 | +1,175 | +1,217 | +1,203 | **+3%** | +40% |
| TEST180 | +1,932 | +1,893 | +1,979 | +1,891 | **+2%** | +42% |

**4窓中3窓で 1件あたりはフラット。件数だけが全窓で +34〜44% 増えている。**

理由: **`--bt-min` はその水準で計算したBTスコアで母集団を切る**。sm を広げると
勝率が上がり(54%→63%)、BTが上がり、**BT30を通るペアが増える**。
つまり sm の列ごとに母集団が違い、総額の比較は「より多く買えたか」を見ているだけ。

**実運用では効かない。** 予算400万は1日十数件で飽和する。sm0.1 の時点で既に
19件/日(TRAIN60: 6,963/365)あり、候補が26件/日に増えても買える枠は増えない。
1件あたりが同じなら得るものはゼロ。§18.10 の教訓そのもの:

> 『全部買えるなら得』と『予算内でどれを買うか』は別問題。

TEST60 だけ +39% だが 216〜311件と最小サンプルで、10倍の件数がある TEST180 は
+2%。窓間で一致していないのでノイズ。

#### ★ 追試 (2026-08-09): tm の**下側** 0.3 / 0.5 も明確にダメ

sm の掃き出しでは tm は 1.0 が下限だった。§18.24 の観測

> 利確の中央値は建玉比 +1.19%。名目 1.0ATR(2〜3%)の半分。大半が引けでタイムカット

から「利確に届いていないなら下げれば届く」と考えて 0.3 / 0.5 を追加。

`--sm-list 0.1,0.3 --tm-list 0.3,0.5,1.0,1.5 --holdout-list 60,180 --bt-min 30`

| 窓 | tm 0.3 | 0.5 | **1.0(現行)** | 1.5 |
|---|---:|---:|---:|---:|
| TEST60 **円/件** | +1,345 | +1,308 | **+1,865** | +1,862 |
| TEST180 **円/件** | +423 | +819 | **+1,373** | +1,409 |

**下げると1件あたりが落ちる。** 窓180では単調で、tm0.3 は現行の 1/3。
平坦域は 窓60=[1.0] / 窓180=[1.0,1.5] → 共通集合 **[1.0]**。現行が入っている。

★ 仮説の答え: 引けタイムカットで終わるトレードは「利確に届かなかった負け」ではなく、
**そこそこ利が乗った状態で終わっている**。tm を下げるとその利を早く切るだけだった。

※ 窓60では総額が tm1.5 で最大(+3,788,925)になるが、これは件数が +184% 増えた
  ためで1件あたりは +39% しか動いていない。**ツールの警告が実際に機能した**ケース。

#### ★ 結論: **sm も tm も現行のまま (sm0.1 / tm1.0)**

そして sm を広げる案には**実害側のコスト**もある。sm 0.1 → 0.7 は
**1回の損失が7倍深くなる**ので、証拠金・日次ドローダウンの前提が変わる。
得るものが無いのに尾リスクだけ増える。

#### ツールを修正 (総額だけ見ると必ずこの罠にかかる)

`sweep_lss_smtm.py` の軸テーブルに **TRAIN/TEST の 円/件** と **TEST件数(相対%)** を
併記し、「総額の改善が件数増で説明できてしまう」ときは自動で警告を出すようにした。

```
⚠ 総額の改善は **件数の増加(+42%)** が主因。1件あたりは +2% しか動いていない。
   --bt-min はその水準で計算したBTで母集団を切るので、水準ごとに母集団が違う。
   予算400万は1日十数件で飽和するため、候補が増えても実運用の利益は増えない(18.10)。
```

**★ 作法: パラメータを比べるときは、母集団サイズが変わっていないかを必ず確認する。**
総額が増えていても、1件あたりが動いていなければ予算制約下では何も得られない。

---

### 18.29 ★ 月別の行は **既にOOS**。基準月を削ったマージを作る必要はない (2026-08-08)

「6月までマージして7月・8月のOOSを見たい」→ **`.\daily` の月別表がそのまま答え**。

`merge_lss_proposals` の `START_DATES`(2026-08-07 から per-symbol が既定)は
**各ペアに『初出の基準月の翌月1日』**を設定し、`nikkei_analysis.py:7722` が
`_eff_since = max(since, pair_oos_start)` で集計を切る。したがって:

| 月別の行 | 含まれるペア |
|---|---|
| 2026/07 | 初出の基準月が **2026-06 以前** のペアだけ |
| 2026/08 | 初出が 2026-07 以前 |

**2026/07 の行は定義上すでに「6月までで選んだ銘柄の7月OOS」。**

#### 実測 (2026-08-08): 6月マージを作っても数字は変わらない

`diff_proposals.py lss_proposal_cumul.py lss_proposal_cumul_to202606.py`:

| | ペア数 |
|---|---:|
| 7月まで(通常) | 1,908 |
| 6月まで | 1,891 |
| **差** | **17ペア(7月初出)** |

その17ペアが動けるのは 2026-08 の5営業日だけで、生んだ取引は **1件**
(全部タブ 5049 → 5048)。予算キャップを通らず月別は**1円まで同一**だった。

#### 得られたOOS

| | 件数 | 勝率 | 損益 | 1件あたり |
|---|---:|---:|---:|---:|
| 2026/07 | 248 | 37% | +134,650円 | +543円 |
| 2026/08 (5営業日) | 62 | 39% | -12,500円 | -202円 |
| 合計 | 310 | — | +122,150円 | **+394円** |

18.23 のローリングOOS(+262円/件) / 180日レポート(+412円/件)と整合。
⚠ 2ヶ月310件では判定不能。月次σ≈106,000円(18.24)に対し両月とも ±1σ 内。

必要資金は 3,746,500〜3,964,450円(最大14〜17銘柄同時)で予算400万に収まる。

#### ★ 作法

- **月別表を読むだけでよい。** 基準月を削ったマージを手で作るのは二度手間。
- 特定フォールドを厳密に `daily.bat` と同一手順で回したいときだけ
  `run_oos_folds.py --fold-from 2026-07 --fold-to 2026-07`。
- ⛔ 実験で `.\dailyfast --lss-proposal ...` を走らせるときは
  **`$env:LSS_TRADES_CSV` を別名に逃がす**こと。既定のままだと `.\fills` の
  突合に使う本番 `lss_trades.csv` が実験結果で上書きされる。

---

### 18.30 ⛔ 日経先物ヘッジ = 棄却 (2026-08-09)。lss はすでに実質 市場中立だった

**仮説**: lss は毎日十数銘柄を全部ショートして同日決済するので、日次損益は
「その日の相場」に支配されているはず。日経先物を買えばその部分を打ち消せて、
平均(α)は変えずに σ だけ削れる → t が上がり、確定に要する期間が縮む。

`analyze_market_beta.py --top 13` (lss_trades.csv / 1,464取引 / 121営業日 /
2026-02-10〜2026-08-07 / 転換除外 / 日内 流動性上位13件):

| | |
|---|---:|
| β | **-503,548円** (日経 +1% で -5,035円) |
| βの t値 | **-2.83** (有意) |
| **R²** | **0.063** |
| α | +2,671円/日 |
| **σ削減** | **3.2%** (日次σ 31,796 → 30,777) |
| t=2 到達 | 37ヶ月 → 27ヶ月 |

**βは有意だが R²=0.063。日次損益のブレの 6.3% しか相場で説明できない。**
先物の手数料・証拠金・毎日の建玉管理に見合わない。

月別βも不安定で **2ヶ月は符号が逆**:

| 月 | β | R² |
|---|---:|---:|
| 2026-02 | -1,715,159 | 0.15 |
| 2026-03 | -623,897 | 0.10 |
| **2026-04** | **+298,383** | 0.02 |
| 2026-05 | -675,822 | 0.12 |
| 2026-06 | -641,425 | 0.14 |
| 2026-07 | -470,647 | 0.05 |
| **2026-08** | **+365,756** | 0.03 |

ヘッジ比率を固定できない。

#### ★ 収穫: 「全銘柄同方向だから相場に支配される」は**誤り**だった

lss は同日決済・タイトな損切り・銘柄分散により、**すでに実質 市場中立**。
分散の内訳:

- 日次σ 31,796円 / 1日あたり約12件
- **94% が銘柄固有**(市場要因は6%)
- 1件あたりのσ ≈ 31,796 / √12 ≈ **9,180円**

→ σ を削りたいなら市場ヘッジではなく **1件ごとのバラつき**を叩くしかない。
現行は `FIXED_QTY=100` 固定で建玉が 10万円(1,000円株)〜60万円(6,000円株)と
**6倍ばらついている**。ここを揃える(金額均等/ATR均等)ほうが直接効く見込み。
§13.8 の「volatility-parity 化の余地」が未検証のまま残っている。

---

### 18.31 ⛔ 流動性の足切り = 棄却 (3度目)。★ 残ったのは予算だけ (2026-08-09)

`analyze_selection_liquidity.py` で「枠外(切り捨てている側)が +619円/件 vs 枠内 +208円/件」
と出たので、発注順・選定・予算の3方向を最後まで詰めた。**プラスだったのは予算だけ。**

#### ① 発注順 = 打ち止め (18.21/18.24 の再確認、今度は反実仮想で)

同じトレード集合・同じ予算で並べ方だけ変え、**ランダム順24シードの帯**と比較
(`analyze_selection_liquidity.py`、レポート窓121日):

| ルール | 件数 | 損益 | 円/件 | z | 帯内順位 |
|---|---:|---:|---:|---:|---|
| ランダム×24(帯) | 1,543 | +300,388 | +195 | — | σ=136,430 |
| 流動性 降順(現行) | 1,422 | +295,250 | +208 | −0.04 | 12/24 |
| 流動性 昇順(薄い順) | 1,701 | +364,250 | +214 | +0.47 | 14/24 |
| 建値 昇順(安い順) | 2,117 | +309,100 | +146 | +0.06 | 12/24 |

**全部帯のど真ん中。** 薄い順が現行を上回る差(+69,000円)を消すのに必要な往復コスト差は
**1.6bp** で、呼値1ティック(約3.6bp)にも届かない。→ **現行(流動性降順)のまま。**

#### ② 枠内 vs 枠外の +412円/件 は『日の構成』だった (銘柄では取れない)

日ごとに対応をとった差 = **+64円/件 / t=+0.20**(プールの差は +412円/件)。

枠内は予算で頭打ちなので毎日ほぼ12件 = 全営業日に均等な重み。枠外はシグナルが
多い日に偏る。**シグナルが多い日ほど1件あたりが良い**ため、プールで比べると差が出る。
並べ替えでは取れない。18.24 の『同日発注数に候補ゼロ』と同じものを別角度から見ている。

#### ③ 流動性の足切り = 棄却 (`sweep_oos_budget.py --liq-floors`, ローリングOOS 10ヶ月)

**予算400万でだけプラスに見えるが、予算を変えると符号が反転する。**

| 予算＼足切り | なし | 1億 | 3億 | 5億 | 10億 | 30億 |
|---|---:|---:|---:|---:|---:|---:|
| 400万 | +573,200 | +578,100 | +626,600 | **+682,000** | +658,100 | +477,050 |
| 600万 | +1,050,850 | +1,072,450 | +1,011,650 | +960,950 | +874,650 | +551,500 |
| 800万 | +1,294,000 | +1,321,600 | +1,127,300 | +998,000 | +924,150 | +542,600 |
| 1,200万 | **+1,495,300** | +1,487,000 | +1,114,400 | +1,060,900 | +857,550 | +542,600 |

5億足切りの効果: 400万 **+108,800** → 600万 −89,900 → 800万 −296,000 → 1,200万 −434,400。

**★ これが決め手。** 薄い銘柄が本当に悪いなら、薄い銘柄をより多く買う大きい予算ほど
足切りの利得は**増える**はず。逆に減って符号まで反転する = 400万の +108,800 は
「たまたまそこで切れた」だけ。

帯別の分解(予算400万で実際に建った2,069件):

| 売買代金 | 件数 | 円/件 | **bp/件** | 建値平均 | 日クラスタt |
|---|---:|---:|---:|---:|---:|
| 〜1億 | 70 | −70 | −3.0 | 2,372 | −0.51 |
| 1〜3億 | 204 | −238 | −9.5 | 2,499 | −0.97 |
| 3〜5億 | 220 | −252 | −9.8 | 2,557 | −1.27 |
| 5〜10億 | 146 | +164 | +5.3 | 3,110 | −0.04 |
| 10〜30億 | 524 | +346 | +12.5 | 2,755 | +1.01 |
| 30億〜 | 905 | +527 | +17.2 | 3,061 | +0.94 |

- **bp/件 も単調** = 円/件の単調は建値の交絡ではない(建値は 2,372〜3,110 で単調でない)
- **しかし日クラスタ頑健 t は全帯 |t|<1.3**
- **帰無較正**(日ごとに流動性ラベルだけシャッフル×200): 実測 +20.2bp、
  帰無の中央 **+7.4bp**、90%区間 [−10.9, +23.7] → 実測は区間の中。**両側p=0.210**

⚠ この帰無は **0中心ではない**。流動性降順で埋める以上、薄い銘柄は
**予算が飽和しなかった日**にしか入らない。その日は候補が少ない日 = ②の悪い側。
だから日構造を保ったまま較正すると +7.4bp が『日の構成だけで出る差』として現れる。
0 と比べてはいけない。

**→ 3度目の「流動性に識別力なし」**(18.13 / 18.24 と整合)。**足切りは入れない。**

#### ④ 選定に流動性を入れるのも同じ結論。そもそも構造上ただの引き算

- 選定は **BT では作られていない**。`scan_lss_universe.py:286,291` = TRAIN の
  取引数≥8 / PF≥1.5 / 損益>0 を通過 → TRAIN期待値順。BT(`calc_recommend_score`)は
  `check_signals_*` が別途計算し、予算タブのフィルタに使うだけ。
- `--select-top` **既定0 = 上限なし**(:68,303)。合格ペアは全部通る。
  **枠が無いので押し出しが起きない** → 流動性を足しても薄い銘柄が落ちるだけで、
  液体な銘柄が繰り上がって入ることはない。
- したがって「選定に流動性条件を足す」と「発注直前に足切りする」は**結果が同一**。
  同じなら発注直前のほうが有利(閾値を後から変えられる/再スキャン不要)。
  そして③でその足切り自体を棄却したので、**選定は変えない**。

※ 軸の差し替えが意味を持つのは `--select-top N` で上限を入れてから。
  18.2 で「top指定なし」を確定させた設計の変更になるので、やるなら別途。

#### ★ ⑤ 残ったのは予算。600〜800万が妥当 (as-of での再測定)

| 予算 | 取引 | 勝率 | 10ヶ月合計 | 月平均 | **月次t** | 95%CI(月) | 建玉最大 | 円/建玉万円 |
|---:|---:|---:|---:|---:|---:|---|---:|---:|
| 400万 | 2,069 | 38.8% | +573,200 | +57,320 | +1.49 | **−18,062 〜 +132,702** | 400万 | 1,433 |
| **600万** | 2,777 | 39.5% | +1,050,850 | +105,085 | **+2.09** | +6,553 〜 +203,617 | 597万 | **1,760** |
| 800万 | 3,327 | 40.2% | +1,294,000 | +129,400 | **+2.27** | +17,786 〜 +241,014 | 799万 | 1,620 |
| 1,200万 | 3,933 | 40.7% | +1,495,300 | +149,530 | +2.19 | +15,545 〜 +283,515 | 1,189万 | 1,258 |

- **400万は 95%CI がゼロをまたぐ**(18.24 の t=+1.18 と整合)。600万以上で全域プラスになる。
- **資本効率(建玉ピーク1万円あたり)のピークは600万**(1,760円)。1,200万で1,258円まで落ちる。
- これは §18.5.4 の「x2.0(=発注額800万)」を **as-of・分割ガード後(v17)・流動性順で
  測り直した**もの。§18.12 の失効リストに載っていた項目のうち、これは**おおむね再確認された**
  (600〜800万 = x1.5〜x2.0)。

限界トレード(1段上げて新たに入ったぶんだけ):

| 区間 | 件数 | 勝率 | 円/件 | 建値平均 | **bp** | 流動性中央 | 判定 |
|---|---:|---:|---:|---:|---:|---:|---|
| 400→600万 | 708 | 41.7% | +675 | 2,796 | **24.1** | 5億 | ✅ 4ティック(14.3bp)の1.7倍 |
| 600→800万 | 550 | 43.8% | +442 | 2,647 | 16.7 | 3億 | ✅ 4ティック(15.1bp)をわずかに超える |
| 800→1,200万 | 606 | 43.2% | +332 | 2,674 | 12.4 | 3億 | △ 2ティック超・4ティック未満 |

#### ⛔ 訂正: 「往復30bp」は私(Claude)が置いた根拠のない推測だった

実データの建値は 2,647〜2,796円。この価格帯の**東証の呼値は1円 = 約3.6bp**。
往復で板を1回ずつ叩いて2ティック(7.2bp)、常に不利側で約定しても4ティック(14.3bp)。
30bp は過大で、限界24.1bp を「スプレッドで消える」と**誤判定していた**。

`sweep_oos_budget.py` の判定を `_tick(price)`(東証の呼値表)から計算した 2/4ティックに変更。
**制度上の最小刻みなので推測が入らない。** `--spread-bp` は既定0(不使用)。

⚠ 呼値は下限。実際のスプレッドはこれ以上になるので、超えていても『確実』ではない。
   実測は `.\fills` の `slip_daily_log.csv` からしか出ない。

#### 作法(この節で使ったもの)

1. **帯を作ってから判定する**(18.24)。2条件を1回ずつ比べない。
2. **円/件 と bp/件 を必ず併記する**。100株固定なので値がさ株ほど建玉が大きく、
   同じ%でも円が大きく出る。円だけの単調は建値の単調かもしれない。
3. **効果は必ず別の軸でも動かして確かめる**。足切りは予算を変えたら符号が反転した。
   1点でしか成立しない効果は、その点の偶然。
4. **帰無較正は日ブロックを保つ**。0 中心を仮定してはいけない。
5. **基準は推測値ではなく制度値**(呼値)から作る。

---

### 18.33 ⛔ 18.32 の「E/H が現行を有意に上回る」は**撤回** (2026-08-10 夜)。条件を揃えたら消えた

**判定は `.\daily` の HTML の中にある「⚖ 現行 / E / H の比較」表で行う。**
18.32 の t=+2.50 / +2.63 は使わないこと。

⛔ **外部ツールを作らないこと。** 一時 `ehm.bat` / `compare_eh_months.py` を用意したが、
『どのファイル・どの実行を見ているか』が毎回わからなくなり半日溶かした(2026-08-10)。
比較表は E/H ペインの先頭に、同じ `_run_budget_sim` の出力からその場で作るので
定義上ズレようがない。表には窓・予算・BT下限・発注順・遅延・sm/tm・手数料・生成日が
刻んであるので、別条件のファイルと取り違えることもない。

#### 何が間違っていたか — 比較の経路が別実装だった

18.32 は `compare_budget_raw.py`(生CSVを**外部で再シミュ**)で測っていた。レポートの
`_run_budget_sim` とは別実装で、**4点が違う**:

| | レポート `_run_budget_sim` | 再シミュ `sim_oos_budget` |
|---|---|---|
| 必要資金 | `order_limit × 株数`(**注文価格**) | `entry_p × 100`(**約定価格**) |
| 予算超過時 | **`continue`**(貪欲。次の安い注文を試す) | **`break`**(その日は打ち切り) |
| 不約定の重複排除 | 同日同銘柄に約定注文があれば入れない | しない |
| BTの出所 | レポート内の as-of `rec_score` | CSVの `bt_score` 列 |

さらに比較そのものに2つの非対称があった:
- **BT下限**: 現行=30 / E・H=**0**(`--bt-min-b` の既定が0)
- **重複**: E/H は `(銘柄,日)` で1件に畳み、現行は畳まない → **同じ400万でE/Hだけ多く分散できていた**

`run_oos_folds.py` の docstring に既に「**ズレたときの正解はレポート側**」と書いてあった。

#### ★ 決定: HTML(レポート)を正とする

理由: ①発注順を `lss_order_rank` から取り実発注 `lss_budget_cap` と同じ ②リポジトリ自身が
そう書いている ③実装が1つで済む。`compare_budget_raw` の数字は今後使わない。

#### ⛔ 訂正: 「表示窓の集計はローリングOOSより25%甘い」(18.23)の**理由の説明は誤り**

per-symbol START_DATES があるので、`.\daily` の月Mの行は「初出基準月 ≤ M−1 のペア」だけ =
**ローリングfoldと同じプール**。`run_oos_folds` は同じ `run_signals_holdout_all.py` を呼ぶ。
差は**プール構造ではなく上記の経路違い**だった。→ **月別の判定に `run_oos_folds` は不要**(18.29)。

#### 実測 (10ヶ月 2025-10〜2026-07 / 予算400万 / BT30 / 流動性順 / delay1 / v17 / 手数料0)

| | 件数 | 勝率 | 合計 | 円/件 | 月平均 | σ | t | プラス月 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 現行 | 2,235 | 41% | +1,039,535 | +465 | +103,954 | **185,965** | +1.77 | 6/10 |
| E | 2,135 | 29% | +1,269,474 | +595 | +126,947 | 123,725 | +3.24 | 8/10 |
| **H** | **1,802** | 31% | **+1,410,252** | **+783** | **+141,025** | **116,728** | **+3.82** | **9/10** |

| 対応検定 | 平均差/月 | t | 95%CI | 勝ち月 |
|---|---:|---:|---|---|
| E vs 現行 | +22,994 | **+0.58** | −55,161 〜 +101,149 | 7/10 |
| H vs 現行 | +37,072 | **+1.37** | −15,776 〜 +89,919 | 8/10 |
| H vs E | +14,078 | +0.65 | −28,242 〜 +56,397 | 4/10 |

**3つとも CI がゼロをまたぐ。10ヶ月では優劣を決められない。**

#### それでも残る構造(こちらは10ヶ月でも出ている)

1. **E/H は「平ら」**。現行が負けた4ヶ月(計 **−242,407**)で E **+238,902** / H **+173,439**。
   逆に現行の最良2ヶ月(2026-05/06 計 +822,162)では E +606,688 / H +649,681 と取りこぼす。
   トリガー(=無料の相場フィルタ)を外した当然の帰結。
2. **月次のブレが小さい**。月平均÷σ = 現行 **0.56** / E 1.03 / **H 1.21**(現行の2.2倍)。
3. **H は資金効率が最良**。1,802件(現行より19%少ない)で最大の +1,410,252円。

#### ⚠ H の非有意は 2026-06 の1ヶ月で決まっている

H が現行に負けたのは2ヶ月だけで、うち 2026-06 が **−161,722**(もう1つは −10,759)。
**この月を除くと t=+3.42。** 最悪月を落とすのは反則だが、**10ヶ月では単月1つで結論が反転する**
ということ(18.24「t=2 に29ヶ月」と同じ)。

#### 残っている非対称(E/H に**不利**な方向)

E/H は `require_open_bar` で**先頭バーが09:00でない銘柄日を落としている**(実測 636銘柄日=17%)。
現行は落としていない。18.32 の測定では落とした側が **+1,583円/件と2.7倍良かった**ので、
E/H は保守側に見積もられている。厳密な対応検定にするなら現行も同じ銘柄日に絞る必要がある(未実施)。

#### 判定コマンド

```
.\daily                   # E/H ペインの先頭に「⚖ 現行 / E / H の比較」が出る
.\daily --days 365        # 10ヶ月ぶん見る(既定180日=約6ヶ月。t は月数で変わる)
```
無効化は `set LSS_EH_TAB=0`。

⛔ **PowerShell の `set` は環境変数を設定しない**(`Set-Variable` のエイリアス)。
env を渡すなら `.bat` の中か `$env:NAME = "値"`。2026-08-10 にこれで
`LSS_OOS_BUDGET_CSV` が黙って効かず、CSV が出ないまま1往復した。

#### 修正済みの不具合 (2026-08-10)

**5分足の並列読込が `SystemError: deallocated bytearray object has exported buffers` で
落ち、その銘柄が丸ごと『データ不足』になっていた。** SystemError は Exception の
サブクラスなので `except Exception` に黙って飲まれる。同じ日・同じデータで3回走らせた実測:

| SystemError | データ不足 | E合計 |
|---:|---:|---:|
| 1回 | 636 | +1,269,474 |
| 3回 | 646 | +1,353,437 |
| 0回 | 569 | +1,265,259 |

**現行タブはこのローダを使わないので3回とも1円まで同一だった**(2,235件 / +1,039,535)。
`eh_trades` にリトライ+直列再読込を入れ、読込銘柄日数とデータ不足の内訳を印字するようにした。
ブレが残るなら `set LSS_EH_WORKERS=1` で直列化して切り分ける。

⚠ ブレ幅は E ±7% / H ±8% で、**判定(3つとも有意でない)は3回とも変わらなかった**。

---

### 18.32 ★★★ E案(翌寄り成行+同じOCO)が全関門を通過 (2026-08-10)。予算400万で現行を有意に上回る

> ⛔ **本節の「予算400万で現行を有意に上回る」は 18.33 で撤回済み。**
> 対応検定の t=+2.50 / +2.63 は経路違いの産物。**18.33 を読むこと。**
> 対照実験・sm/tm スイープ・ロング鏡像 G・配当落ちの切り分けなど、
> **予算シミュを通さない部分の知見は有効**。

**ユーザー発案**「終値で入って翌日売る」の検証から派生。持ち越し案そのものは死んだが、
**トリガーを捨てて翌朝の寄りで入る**案(E)が、このセッションで唯一すべての検証を通った。

`analyze_overnight_lss.py` / `compare_budget_raw.py`

#### 何が違うのか

| | 現行 lss | **E** |
|---|---|---|
| 発注 | 逆指値売り(前日終値−1ティック) | **寄付指値売り**(下限=前日終値×0.97 で−3%ガードも実現) |
| 約定 | ザラ場でトリガー到達時。届かなければ建てない | 09:00 の寄り。ほぼ必ず建つ |
| OCO | 約定価格から sm0.1 / tm1.0 | **同じ**(基準が寄り値になるだけ) |
| delay1 / 引け決済 / 分割ガード | — | **すべて同じ** |

**エントリー時刻以外は完全に同一。** `lss_exit_watcher` は発注種別以外そのまま流用できる。

#### なぜ効くのか(2つとも構造的)

1. **トリガーは前日終値の『下』**。寄りで入れば**必ず同じか高く売れる**(ショートに有利)。
2. **09:00 から建玉がある**ので利確 1.0ATR に届く時間が長い。現行は11時に約定したら
   そこからしか走れない。

つまり E は新しいエッジではなく、**同じエッジをより良い値段・より長い持ち時間で取る**もの。
§18.19/18.20 の「エッジは下ブレイクへの反応」と矛盾しない。

#### 実測 (ローリングOOS生CSV 10ヶ月 / 予算400万 / 流動性順 / 100株 / 摩擦なし)

| | 件数 | 勝率 | 円/件 | 月平均 | 月次t | 95%CI(月) |
|---|---:|---:|---:|---:|---:|---|
| 現行 lss | 2,069 | 38.8% | +277 | +57,320 | +1.49 | −18,062 〜 +132,702 |
| **E** | 2,144 | 28.2% | **+517** | **+110,879** | **+3.69** | **+51,951 〜 +169,806** |

**E の信頼区間の下限が、現行の点推定とほぼ同じ。**

対応のある検定(同じ日・同じ銘柄集団なので paired が最も検出力が高い):

| 予算 | 差(E−現行) | t | 95%CI(月) | E勝ち |
|---:|---:|---:|---|---|
| **400万** | **+53,559/月** | **+2.50** | **+11,492 〜 +95,626** | **8/10** |
| 600万 | +58,338/月 | +1.38 | −24,578 〜 +141,255 | 9/10 |
| 800万 | +62,431/月 | +1.15 | −44,073 〜 +168,935 | 8/10 |

400万は最悪月を除いても +69,815、最良月を除いても +44,503 で頑健。

#### 通した関門 (すべて通過)

| 検証 | 結果 |
|---|---|
| **対照実験**(同じ銘柄・**シグナルが出ていない日**に同じOCO) | **+72円/件 t=+0.79 ≒ ゼロ** vs E +594 t=+4.04 |
| 始値の出所ちがい(日足yfinance vs 5分足J-Quants) | ズレ 平均−0.4bp / 中央0.0 / 一致94.5%。E +594 vs F +589 = **差5円** |
| 寄り直後の取りこぼし | 先頭バーが09:00でない13%は+1,583円/件と2.7倍良い → `--require-open-bar` で除去。**+594** |
| 配当落ち | E は寄り後エントリーなので無関係(A/B/D は 2026-03 が+4,039円/件で汚染) |
| 決済ルールの同一性 | sm0.1 / tm1.0 / delay1 / −3%ガード / v16 / 分割ガード すべて現行と同じ |
| 選定のOOS性 | ローリングOOSの as-of 母集団をそのまま使用 |
| 限界トレードのbp | 400→600万 34.2bp(呼値4ティック15.2bpの2.3倍) |
| 予算制約 | `sim_oos_budget` を通して比較(18.10 の罠を回避) |

**対照実験を通ったのが大きい。** 0.1/1.0 は 10:1 の宝くじ型なので「利確損切を置けば
何でも良く見える」という疑い(ユーザー指摘)があったが、シグナルの無い日では
ほぼゼロだった。E の数字は払い出し形状では説明できない。

#### ★★ 決定版: 方向 × シグナル有無 の 2×2 (円/件)

| | シグナルあり | 対照(シグナルの無い日) |
|---|---:|---:|
| **ショート E** | **+594** (t=+4.04) | +40 (t=+1.07) |
| **ロング G** | **−441** (t=−4.69) | −25 |

**シグナルがある時だけ効き、方向を逆にすると符号が反転し、対照は両方向ともゼロ。**
検証として理想的な形。分解すると:

  方向成分 (E−G)/2 = **±517円/件**   /   両方向に共通する成分 (E+G)/2 = +77円/件
  対照の共通成分 = +8円/件

共通成分は方向成分の15%以下で、残差はノイズの範囲。**OCO の払い出し形状は
何も寄与していない。**

G = −441円/件 は §18.18 の LDT(トリガー付きロング, −420〜−831円/件)とほぼ一致。
**別ツール・別エントリー方式で同じ結論に着地**したので相互検証になっている。
「同日決済でロング方向は構造的に無理」(§18.18)は、トリガーの有無に関わらず成立する。

決済理由の比率も整合: G は **82%が損切り**(E 70% / D 72%)。利確到達は G 7% / E 13%。
ロングは +1.0ATR まで走らない、というのが §18.18 の観測そのもの。

#### ★ なぜロングが負けるのか = C(バリア無しの素の測定)が答え

| C 翌寄り→翌引け(ショート方向) | **+778円/件 / +29.4bp / t=+2.88** |
|---|---|

**lss のシグナルが出た銘柄は、翌日の日中に平均 29bp 下げている。** これが実体で、
E も G もこのドリフトの上に乗っているだけ。バリアはエッジを作っていない
(実際 E +594 は C +778 より**低い**。損切り0.1ATRがタイトすぎて下げる途中の揺れで
刈られるぶん)。10:1 の払い出しで必要な利確到達率は無ドリフトなら9.1%。
E は13% / G は7% で、ドリフトの符号がそのまま両側に出ている。

シグナルは6つとも「上げたこと」を条件に発火する(MACDゼロ抜け+出来高スパイク /
ドンチャン15日高値抜け / ROC>3% 等)。つまり**すでに走った後**に点灯するので、
翌日に買うのは伸びきった動きに乗ることになり短期の押し戻しに当たる。
§18.19 で無条件のドリフトはゼロなので、これは相場全体の性質ではなく
**シグナルによる条件付けの効果**。§18.19 の「上に抜けた銘柄は反落する」が
C で直接測れた形。

#### sm/tm スイープ (4×5升 / 同じ母集団 / 円/件)

| E ショート | tm0.3 | 0.5 | **1.0** | 1.5 | 2.0 |
|---|---:|---:|---:|---:|---:|
| **sm0.1(現行)** | +349 | +465 | **+594** | +580 | +583 |
| sm0.3 | +442 | +594 | +679 | +673 | +676 |
| **sm0.5** | +522 | +679 | **+741** | +726 | +728 |
| sm1.0 | +499 | +637 | +674 | +663 | +660 |

| G ロング | tm0.3 | 0.5 | 1.0 | 1.5 | 2.0 |
|---|---:|---:|---:|---:|---:|
| sm0.1 | −305 | −409 | −441 | −450 | −442 |
| sm0.3 | −428 | −589 | −581 | −591 | −587 |
| sm0.5 | −511 | −668 | −650 | −648 | −662 |
| sm1.0 | −611 | −726 | −683 | −687 | −699 |

- **G は 20/20 マイナス。パラメータではなく方向の問題**。ロングは打ち止め。
- **E は 20/20 プラス**で表面が滑らかな単峰。孤立したスパイクではないので、
  E がパラメータの産物でないことのもう1つの証拠。現行 0.1/1.0 は最良ではないが谷でもない。

⚠ **sm を広げる案は今は採用しない**(同じ10ヶ月で選び直したら過剰適合。18.28 の作法)。
   ただし §18.28 と質が違う点は記録しておく: あのとき sm を広げて総額が増えたのは
   **件数が増えたから**で1件あたりは動かなかった。今回は**母集団が固定**
   (同じ3,547件・BTフィルタが動かない)なので +594→+741 は純粋に1件あたりの改善。
   TRAIN/TEST を分けて測る価値がある。
   ⚠ ただし sm 0.1→0.5 は**1回の損失が5倍深くなる**。証拠金と日次ドローダウンの
     前提が変わるので、得るものが確定してから動かすこと。

#### ⚠ 採用前に残っているリスク (どちらも slip=0 のシムには映らない)

1. **上げ相場でトリガーの保護を失う。** 2026-07 は現行 +344,500(600万) に対し E は
   +38,847。上げ相場ではトリガーが発動せず**現行は自動的に見送る**が、E は全部寄りで
   入って刈られる。**トリガーは無料の相場フィルタでもあった。**
   600/800万で検出できないのはこの1ヶ月だけが原因。
2. **損切り比率が高い。** E は決済の **70%(2,483件)が損切り**。現行は勝率41.6%→E 29.4%
   なので明らかに増える。§18.17 の実測では実運用の滑りの99%が**無保護窓明けの成行
   買い戻し**に集中していた。損切りが増えるぶん執行コストを多く払う。
   ※ 決済モデル自体は §18.22 で1分足2,703件と突合し ±260円/件 と検証済み。
     これはモデル誤差ではなく**スプレッドぶんの実コスト**の話。

#### ★★ H(mirror = 前日終値で指値空売り) — E と統計的に区別できない (2026-08-10 追記)

2段分解が「**待つのは正しい**(9:00から持つのは -578円/件)」「**高く売るのが効く**
(+1,066円/件)」と出たので、両方を満たす形として H を測った。

  H: 前日終値で指値空売り。寄りが既に前日終値以上なら板寄せで約定(=始値)。
     到達しなければ**建てない**。ガードは +3%超のギャップアップでスキップ。
     決済は E/現行とまったく同じ。

| 予算400万 / 対応検定 | 差 | t | 95%CI(月) | 勝ち |
|---|---:|---:|---|---|
| E vs 現行 | +53,559/月 | +2.50 | +11,492 〜 +95,626 | 8/10 |
| H vs 現行 | +58,934/月 | +2.63 | +15,047 〜 +102,821 | 8/10 |
| **H vs E** | +5,215/月 | **+0.27** | **−32,267 〜 +42,697** | 3/10 |

**両方とも現行を有意に上回り、両者の差はゼロと区別できない**(600万でも t=+0.09)。
**バックテストでは E と H を選べない。** 別の根拠で決める必要がある。

| | E(寄成) | H(前日終値で指値) |
|---|---|---|
| 約定率 | 99.8% | 84.8% |
| 400万での取引 | 2,149件 | **1,818件**(同じ利益を少ない資金で) |
| 執行 | 板寄せ(スプレッドなし) | **指値=受動側**(スプレッドをもらう側) |
| 損切り比率 | 70% | 68% |
| 朝の露出 | 9:00から(E0 の -578 を丸かぶり) | **戻るまで待つ** |
| **2026-07(上げ相場)** | **+24,751** | **+181,896**(現行 +117,500) |

**H は E の唯一の弱点(上げ相場でトリガーの保護を失う)を埋めている。**

#### E と H は稼ぐ場所がほぼ直交している

| | 件数 | 円/件 |
|---|---:|---:|
| 両方が建てた日 | 3,012 | **H +704 / E +183** |
| H が取り逃がした日 | 535 | **E +2,908** (合計 +1,555,846) |

**E の利益の74%は、H が1件も建てない535日に集中している**(前日終値まで戻らなかった
= そのまま下げ続けた日)。H はその日を落とす代わりに、戻る日に高く売って稼ぐ。
総額はほぼ同じ(E +2,106,360 / H +2,120,119)。

#### ⛔ --out-raw のバグ (H に有利な先読みだった)

`sim_oos_budget` の『通常予算』は**不約定の注文も枠を消費する**(fill_budget=False)。
ところが --out-raw は**約定した行しか書き出していなかった**ので、シミュは
「どれが約定するかを事前に知っていた」ことになり、空いた枠で他の銘柄を詰められた。

E は約定率99.8%なので影響が無いが、**H は73.8%** なので大きく効いた:

| H vs 現行 400万 | 修正前 | **修正後** |
|---|---:|---:|
| 差 | +95,301/月 | **+58,934/月** |
| t | +4.98 | +2.63 |
| 勝ち月 | 10/10 | 8/10 |

**優位の38%が先読みだった。** 未約定は filled=0 / pnl=0 / entry_p=注文価格 で
書き出すよう修正済み。書き出し時に約定率を印字するようにした。

★ 教訓: **約定率が100%でない方式を予算シミュに載せるときは、必ず不約定行も出す。**
   出さないと「当たる注文だけ選べた」ことになる。

#### ★ 進め方

**一括切替はしない。小ロットで並走させ、`.\fills` で実際の損切り滑りを測ってから。**
+594円/件 の優位を執行コストがどれだけ食うかは実測でしか分からない。

実装の変更点は小さい:
- `kabu_send_lss` / `lss_budget_cap`: 逆指値売り → **寄付指値売り**(下限 = 前日終値×0.97)
- `lss_exit_watcher`: OCO の基準を「トリガー価格」から「実約定価格(寄り値)」に変える
- 発注順・予算・選定・BT・決済はすべて現行のまま

#### 副産物: 持ち越し(引け→翌日)は棄却

| | 円/件 | 判定 |
|---|---:|---|
| A 引け→翌寄り(純オーバーナイト) | +659 | ⛔ **2026-03 だけで合計の43%**。配当落ちの権利落ちギャップ。空売りは配当落調整金を払うので現実には存在しない。除くと +401 / t=+1.18 でゼロと区別できない |
| D 引け+OCO | +559 | ⛔ 14:55の仮判定+MOCなら実装は可能だが、E(+594) / H(+704) に**円/件で負ける**。損切り比率も72%と最悪(夜のギャップを止められない) |

#### 配当落ちを除くと D は有意でなくなる (--exclude-months 2026-03)

| | 全期間 | **2026-03 除外** | 差 | t(除外後) |
|---|---:|---:|---:|---:|
| 現行 | +348 | +364 | — | — |
| **D(引けで建てる)** | +559 | **+438** | **−121** | **+1.52** |
| D2(夜間損切り完璧) | +950 | +843 | −107 | +6.10 |
| E(寄成) | +594 | +597 | +3 | +4.00 |
| H(前日終値の指値) | +704 | **+717** | +13 | +5.35 |

**D だけが大きく落ち、t=+1.52 で有意でなくなる。配当落ちが D を支えていた。**
E と H はほぼ動かない(+3 / +13) = 同日決済が配当落ちと無関係であることの実測での裏付け。

#### ⛔ PTS で夜間の損切りをする案 = 追わない

D2 − D = **405円/件** が夜間ギャップのコスト。H(+717) に並ぶには D が +279円/件
改善する必要があるので、**PTS で夜間ギャップの69%を消せないと届かない**。

ニュース由来のギャップは PTS の気配も同時に飛ぶので回収できない(飛んだ後の値段で
約定する)。回収できるのは米国市場につれた緩やかなドリフトだけで、それが7割を
占めるとは考えにくい。加えて 東証建玉のPTS返済可否 / ナイトタイムの板が東証の数% /
kabu API の対応 / 貸株料・逆日歩・一般信用デイトレ不可 / 夜間の相場全体のテールリスク
が残る。**→ 持ち越し(D)は棄却。PTS は追わない。**

⛔ **訂正 (2026-08-10)**: 当初「持ち越しは資金回転が半減するので実質半分の成績」と
書いたが**誤り**。引けで買い戻して同じ引けで新規を建てれば資金は毎日1回転し、
E/H と同じ。回転数は変わらない。実際に発生する差は:
  ・貸株料が日数ぶん / 一般信用デイトレ(MarginTradeType 3)が使えない / 逆日歩
  ・夜間の相場全体のギャップリスクを100%被る
  ・権利落ち日をまたぐと配当落調整金(上記A)
**同日決済なら配当落ちは構造的に無関係**。これは lss/E/H に共通の利点。

§18.19 の「持ち越し案は死んだ」は維持。生き残ったのは**寄りで入る**案だけ。

#### ⛔ この検証で踏んだバグ (同じ形を繰り返さないこと)

1. **`short_exit_5m` の `_start=ei`**。この関数は『トリガーで約定する』前提で、
   最初に安値が entry_p に達したバーから損切り・利確を見る。寄りから建玉がある
   D/E にそのまま渡すと、「寄りが建値より上に飛び、昼に下りてきて建値に触れた」
   ケースで**朝の含み損の区間が丸ごと飛ばされ、負けだけが系統的に消える**。
   初回の実測は D=+1,692円/件 t=+9.08 と出た。`entry_p` に `+inf` を渡して ei=0 を
   強制すれば直る(stop_p/target_p は別引数なので値は歪まない)。
2. **`stop_delay_bars`**。delay1 は『約定した5分足の中は逆指値を置けない』という
   機構的理由(18.9)。前日引けから建玉がある D には当てはまらない(寄り前に置ける)。
3. **5分足の先頭バーが09:00とは限らない**(13%が09:03〜09:06)。寄り成行で入るのに
   先頭が09:05だと寄り直後の逆行が判定から抜ける。①と同じ系統。

**共通する形: 建玉を持っている区間の一部が判定対象から抜けると、必ず利益方向に出る。**
新しい決済経路を作ったら、まず『いつからいつまで判定しているか』を確かめること。

---

### 18.34 ⛔ 実施済み・候補ゼロ: 寄り前に『今日は全体で悪い日か』は分からない (2026-08-13)

**問い**: 09:00 より前に手に入る値だけで、その日の lss/H 全体の勝率・損益が悪くなると
予測できるか。できるなら『その日は一切建てない』というルールが作れる。

#### なぜ有望かもしれないか

lss/H は同日決済で全銘柄が同方向(ショート)。18.30 で「日次損益の94%は銘柄固有・
市場要因は6%(R²=0.063)」と出ているので**日中のヘッジは無意味**だが、
「その日に**参加するかどうか**」は別の問い。σ の6%しか説明できなくても、
裾(大きく負ける日)だけを避けられるなら価値がある。実際 18.24 で
「全体の最悪5日を除くと木曜の劣位が消える」= **少数の日が損益を支配している**。

#### 候補になる寄り前の変数(すべて 09:00 前に確定)

| 分類 | 変数 |
|---|---|
| 海外 | 前日の S&P500 / NASDAQ 終値リターン、VIX 水準と変化 |
| 先物 | 日経225先物(大阪ナイト / CME)の前日終値比 = **寄りギャップの事前推定** |
| 為替 | USDJPY の夜間変化 |
| 前日の日本市場 | 日経終値リターン、5日リターン、値上がり銘柄比率、騰落レシオ |
| 自前 | **その日のシグナル件数**(前夜に確定している) |
| カレンダー | SQ日、月末月初、日銀・FOMC・雇用統計の当日/前日 |

⚠ シグナル件数だけは **18.13 で単独では否定済み**(TRAIN 全帯マイナス / TEST は
少ない日ほど悪い = 逆向き)。市場変数との組み合わせは未検証。

#### 仮説(方向を持って測る)

lss/H は「前日終値を割る/寄りが上」で売る。だから
- **寄りが大きくギャップアップする日** → H は高く売れるので有利?
- **ギャップダウンする日** → 指値に届かない or 不利約定。18.8 の -3% ガードとも関係
- **VIX 急騰日** → 値幅が出るので 0.1ATR の損切りが刈られやすい?

先物ギャップは H の約定価格そのものに効くので、**最も筋が良い候補**。

#### 測り方(ここを外すと必ず偽陽性が出る)

1. **日次に集約してから測る**。1日1行(その日の合計損益・勝率・件数)にすれば
   18.13 の同日相関(下げた日は全銘柄まとめて勝つ)の問題が消え、実効サンプル =
   営業日数になる。件数ベースの t を使わないこと。
2. **予算シミュを通す**(18.10)。ただし『その日を丸ごと見送る』は他の日に振り替え
   られないので機会費用が無く、compare 系の総額比較でも比較的素直。とはいえ
   件数が減って t は落ちるので、必ず `sim_oos_budget` / レポートの予算タブで確認。
3. **ノイズ帯で判定**(18.24)。「同じ日数をランダムに落とした帯」を作り、
   |z| >= t(N-1) で初めて効果と言う。**閾値は 1.96 ではなく t 分布**(18.34 実装時に
   `_strategy_loo_html` の `_TTBL` を流用できる)。
4. **多重検定を数える**。上の候補は10個以上あり、分位で切ればさらに増える。
   帰無較正は**日ブロックを保ったまま**巡回シフトする(完全シャッフルは日内相関を
   壊して帰無分布が狭くなり偽陽性を過小評価する / 18.13)。
5. **TRAIN/TEST を分ける**。18.25 の事故(ホールドアウトが下限しか動かしておらず
   TRAIN ⊇ TEST だった)を繰り返さないため、**窓を変えて TRAIN の数字が動くか**を
   必ず確認する。
6. **リークに注意**。前日の日本市場の値は 15:30 確定なので OK。米国終値は
   日本時間 早朝で OK。**当日の日経寄り値は使えない**(09:00 = 発注済み)。

#### 事前の見立て(期待しすぎないこと)

- 18.13 の 15軸×5分位=78検定で**候補ゼロ**、18.24 の7属性でも**候補ゼロ**。
  ただしどちらも**銘柄属性**であって、**市場全体の寄り前変数は一度も測っていない**。
- 18.30 で市場β自体は有意(t=-2.83)だが R²=0.063。**平均を動かす力は弱い**。
  狙うなら平均ではなく**裾(大負けの日)の除去**。
- 月次σ ≈ 106,000円 (18.24) に対し、効果が数万円なら測れない。
  10ヶ月では **月あたり10万円規模の改善**でないと t=2 に届かない。

#### やること

1. `lss_trades.csv` / ローリングOOS生CSV から**日次パネル**を作る
   (日付, 件数, 勝率, 合計損益, 平均建値, 平均ATR%)
2. 寄り前変数を結合(yfinance で ^GSPC / ^IXIC / ^VIX / USDJPY=X / ^N225、
   先物は取得元を要検討。J-Quants には無い)
3. 単変量で分位別の日次損益 → 日次 t とノイズ帯 → TRAIN/TEST
4. 生き残ったものだけ『その日は建てない』ルールとして予算シミュに載せる
5. レポートに常設ブロックとして入れる(外部ツールを作らない / 18.33)

---

### 18.35b 寄り前の気配 — **用途Aは棄却 / 用途Bは未測定** (2026-08-18)

⛔⛔ **私(Claude)が一度『棄却』と書いたのは誤り。測っていた問いが違った。**
ユーザーの「本当に無理？」という差し戻しで気づいた。

| 用途 | 何をするか | 誤りのコスト | 判定 |
|---|---|---|---|
| **A** | 気配**だけ**で判定して発注 | 誤建てがそのまま損 | ⛔ **棄却**(下記) |
| **B** | 気配で**登録する50件を選ぶ**。最終判定は 09:00 の実際の始値 | 誤建ては 09:00 で弾かれる = **ほぼ無料**。取逃しだけが損 | ☐ **未測定** |

**B の比較相手は『完璧』ではなく、いまのやり方(流動性降順)。** それ自体が
**38.7% を取り逃している**(捕捉率 61.3% / 同日実測)。
→ **反転率が高くても捕捉率で勝てば採用する価値がある。**

`log_preopen_board.py --verify` に「■ ★★ 本命: 気配で登録する50件を選べるか」を
追加済み(日ごとに気配ギャップ降順で上位N → 実際の合格の捕捉率 / ランダム12本の帯 /
|z|>=2.2)。**ただし現在2営業日しかないのでノイズ。15〜20営業日は貯めること。**
比較相手を同じ土俵で出すため、ログに `liquidity` 列を追加した(古いログには無い)。

---

#### ⛔ 用途A = 棄却 (これは確定)

**`python log_preopen_board.py --verify` / 694件(各銘柄の最新気配) / 2営業日 /
644銘柄。サンプルは目標(500〜1,000件)に到達済み。**

| | 実測 | 先に決めた基準(18.35) |
|---|---:|---|
| **反転率(実運用の精度)** | **30.5%** | 数%なら可 / **10%超なら不可** |
| うち発注候補だけ | **35.6%** | 同上 |
| 誤差 中央 | **64.9bp** | ⛔ **判定閾値の +50bp より大きい** |
| 誤差 90%点 / 95%点 | 196bp / 251bp | — |
| 相関 | 0.267 | ほぼ無相関 |

**誤差の中央値が判定閾値(+50bp)を超えているので原理的に無理。**
694件中 誤建て144件 / 取逃し68件 = 建てるべきでない銘柄を2割方建てる。

**寄りに近づいても改善しない**(10〜20分前 30.3% / 20〜30分 32.4% / 30分〜 31.2%)。
08:10 の 20.4% や 08:13 の 16.0% は 49件のノイズ。ユーザーの事前の見立て
(「寄り前の気配値は当てにならない」)が実測で裏付けられた形。

#### ★ 用途A で死んだもの (B では死んでいない)

| 案 | 死因 |
|---|---|
| 気配だけで判定して **09:00 を待たずに発注** | 反転率30.5% |
| 気配で 09:00 の36秒を短縮 | 同上 |

**09:00 の始値で最終判定する方式は維持する。**
⚠ ただし「**登録する50件をどう選ぶか**」は別問題(用途B)で、まだ開いている。

#### ⚠ ただし watch を増やす価値そのものは小さい (2026-08-18 同日の実測)

同じ1回の実行(`.\jfast --days 365`)の推奨変種 `H寄り確認+50bpd4sm0.5資金均等`:

| watch | 件数 | 10ヶ月合計 | 月平均 | 1段上げた増分 |
|---:|---:|---:|---:|---:|
| 25 | 1,095 | +3,719,360 | +371,936 | — |
| **50(現行)** | 1,541 | +4,566,280 | **+456,628** | **+84,692/月** |
| 100 | 1,736 | +4,808,050 | +480,805 | +24,177/月 |
| 無制限 | 1,785 | +4,851,620 | +485,162 | +4,357/月 |

**単調に増えるが 50 で飽和する。** 50→無制限でも **+28,534円/月**(月次σ 15万の1/5)。
理由は **予算が律速**(§18.42): 400万では1日13〜18件しか建てられないので、
watch を増やしても建てる数は増えず選択肢が増えるだけ。そして選択軸には
識別力が無い(§18.12/§18.24/§18.31)。watch50 で既に合格シグナルの61%を捕捉。

⚠ **予算を上げれば律速が外れるので watch の価値は上がる**(未測定)。
**「予算を上げる」と「watch を増やす」はセット**。本番サイズに移すとき
(③予算スイープ)に一緒に測ること。

#### ▶ 残っている宿題

1. **用途B の捕捉率**を15〜20営業日ぶん貯めて測る(`--verify` に実装済み)。
   ログ収集は `.\mtest` に組み込み済みなので、**何もしなくても貯まる**。
2. 勝っていたら **予算とセットで**判断する(watch単独では月+28,534円が天井)。
3. それでも足りなければ 別ソース(楽天 MarketSpeed II RSS 等)。

#### ★ この節から持ち帰る作法

**「使えるか」を測る前に『何に使うのか』を確定させること。**
同じデータでも、用途Aなら数%の精度が要り、用途Bなら30%でも足りる。
そして**比較相手は常に『いまのやり方』であって『完璧』ではない**。
現行が4割取り逃しているなら、3割外す手法でも勝てる。

---

### 18.35 ☐ (棄却済み。経緯の記録) 寄り前の**気配値・板**で下がりそうか分かるか (2026-08-13 ユーザー依頼)

> ⛔ **結論は 18.35b。反転率 30.5% で棄却済み。以下は当時の設計メモ。**

18.34 が『**その日**を建てるか』の市場全体フィルターなのに対し、こちらは
『**その銘柄**を建てるか』の個別フィルター。08:00〜09:00 の板寄せ前気配から、
その銘柄が下げそうか(= H のショートが機能しそうか)を読めないか。

#### ⛔ 最重要: これは**バックテストできない**。今日からデータを貯めるしかない

板・気配は **どの履歴データにも無い**(J-Quants の5分足にも yfinance にも無い)。
過去に遡って検証する手段が存在しないので、**前向きにログを貯める以外に方法がない**。
→ **着手すべき最初の一手は分析ではなく「毎朝ログを取る」**。数ヶ月ぶん貯まって
   初めて検証できる。始めるのが遅いほど結論も遅くなる。

#### 取るもの (kabu `GET /board/{sym@ex}` = `kabu_api.KabuClient.get_board`)

| 分類 | 項目 |
|---|---|
| 気配 | `BidPrice` / `AskPrice`、`BidSign` / `AskSign`(**特別気配・連続約定気配のフラグ**) |
| 需給不均衡 | `MarketOrderSellQty` / `MarketOrderBuyQty`(**成行の売り買い数量差**) |
| 板の厚み | `Sell1..Sell10` / `Buy1..Buy10` の価格と数量 |
| 参考 | `CurrentPrice`、`TradingVolume`、前日終値との乖離 |

⚠ kabu は **未登録銘柄の /board が空**になる(`_register`)。発注リストの銘柄を
先に登録しておくこと。08:00〜09:00 の間、**1〜3分おきにスナップショット**を取る
(気配は寄りに向けて動くので、時刻ごとの推移そのものが情報になりうる)。

#### 事前の見立て(期待しすぎないこと)

- **実現した寄りギャップは 18.13 で候補ゼロ**(15軸×5分位=78検定)。気配値は
  その寄りギャップの**予報**でしかないので、素直に考えると同じく効かない。
- 気配値が**実現ギャップを超えて持っている情報**は次の3つ。狙うならここ:
  1. **成行の売り買い数量差**(板寄せ前の需給不均衡。実現ギャップには畳み込まれて消える)
  2. **特別気配 / 連続約定気配**(値が飛んでいる最中。H の指値が刺さる質が違う)
  3. **板の厚み・スプレッド**(執行コストの事前推定。18.21 で流動性順を既定にした
     理由が『バックテストに映らない執行コスト』だったので、**そこを直接測れる**)
- とくに ③ は 18.21 の宿題に直接答える。**発注順を『事前の板の厚さ』で決める**なら
  過去の売買代金より筋が良いはずで、これはログさえ貯まれば測れる。

#### 実運用でどう使うか(使えると分かってから)

H の注文は**前夜に出している**ので、気配で判断するなら 08:00〜09:00 に
**取り消す**形になる。watcher には既に取消の仕組みがある
(`cancel_gap_orders._budget_sweep` / `--entry-cutoff` / 寄り深ギャップ取消)ので、
条件を足すだけで実装できる。⚠ 逆に「気配を見てから新規に出す」は板寄せに
間に合わないので現実的でない。

#### 測り方(18.34 と同じ作法。加えて銘柄軸なので注意が増える)

1. **日次に集約するのは 18.34。こちらは銘柄×日なので同日相関が復活する**。
   日クラスタ頑健な分散を使うこと(18.13)。件数ベースの t は使わない。
2. **予算シミュを通す**(18.10)。銘柄を落とすと**その枠は別の銘柄で埋まる**ので、
   「その銘柄の損益」ではなく「落としたときに合計がどう動くか」を見る。
   戦略別 LOO(`_strategy_loo_html`)と同じ形にできる。
3. **同数をランダムに落とした帯**と比べ、|z| >= t(N-1) で判定(18.24)。
4. **多重検定**。板の項目は10個以上あり、分位で切ればさらに増える。
5. **TRAIN/TEST**。ログが貯まる前に何度も覗くと、実質的に全期間 in-sample になる。
   **最初に「N日貯まるまで見ない」と決めてから始めること**。

#### やること

1. `log_preopen_board.py`(仮) を作る。08:00〜09:00 に発注リストの銘柄を登録して
   1〜3分おきに /board をスナップショット → `preopen_board_<日付>.csv` に追記
2. `.\watch` の起動前に走らせる(**kabu の有効トークンは1つ**なので、watcher と
   同時に走らせない。08:00〜08:55 に取って終える運用が安全)
3. 3ヶ月ほど貯める。その間は分析しない
4. 貯まったら `lss_trades.csv` / 実約定と突合 → 上の作法で検定
5. 生き残ったら『寄り前に取り消す』ルールとして予算シミュに載せる

---

### 18.36 ★★★ H の確定値 (2026-08-13)。母集団の欠陥を直し、365日窓で測り直した

**この日より前の H の数字は全部無効。** 母集団に2つの欠陥があり、窓も180日(6ヶ月)
しかなかった。修正後・365日(10ヶ月)で測り直したのが本節。**以後はこれが基準。**

#### 消したリーク3つ

| # | 何 | 実測の影響 |
|---|---|---|
| ① | **不約定プールに4つ目のBT床**(`if _n_sc < 30: continue` のベタ書き) | `LSS_NO_BT_FILTER` で外せず、BT30未満の不約定が E/H の母集団に入らなかった |
| ② | **当日ぶんの未決着シグナルを母集団から除外** | 当日の実発注が『テストの母集団に無い』になる。08-13 は 11件→14件 / +35,008→**-28,392** に反転 |
| ③ | **窓が180日(6ヶ月)** | §18.33 の「3方式とも有意でない」はこの窓の結果。10ヶ月では結論が変わった |

②は「発注中」が**全部 最終バーの日付で**記録される仕様(`backtest_limit_entry:1181`
が `entry_dt=df.index[-1]` を一律で入れる)。そのまま入れると数日前のシグナルまで
当日の寄りで建てたことになるので、**シグナル日 < 最終バー の中の最新日**だけを採る。
判定キーは `signal_dt_raw`(`signal_dt` ではない / :7990)。

#### ★ H の採用が、ここで初めて数字に支持された

§18.33 では「3つとも CI がゼロをまたぐ = 優劣を決められない」だった。10ヶ月では:

| 比較 | 平均差/月 | t | 95%CI | 勝ち月 |
|---|---:|---:|---|---|
| **H vs 現行** | **+150,293** | **+4.82** | **+79,823 〜 +220,763** | 9/10 |
| **H vs E** | **+37,939** | **+2.55** | **+4,321 〜 +71,557** | 7/10 |
| E vs 現行 | +112,354 | +3.22 | +33,430 〜 +191,279 | 9/10 |

**3つとも CI 全域プラス。** 前半/後半に割っても維持(H vs 現行: 前半 t=+3.65 5/5勝 /
後半 t=+2.87 4/5勝)。

| 方式 | 件数 | 円/件 | 月平均 | 月次σ | t | プラス月 |
|---|---:|---:|---:|---:|---:|---|
| 現行(逆指値) | 2,231 | +169 | +37,775 | 147,903 | +0.81 | 6/10 |
| E(寄成) | 2,391 | +628 | +150,129 | 98,119 | +4.84 | 9/10 |
| **H(指値)** | **2,332** | **+806** | **+188,068** | **90,291** | **+6.59** | **10/10** |

条件: 窓365日 / 予算400万 / BT0(フィルタなし) / 流動性順 / delay1 / sm0.1 tm1.0 /
手数料0 / slip=0 / -5tick ザラ場込 / 当月除く。

#### ★ 設定は全部「変えない」。walk-forward が3ブロックとも現状を下回った

| ブロック | walk-forward | 現状(H) | 判定 |
|---|---:|---:|---|
| H の設定(13行) | +1,847,975 (固定最良の86.6%) | **+1,880,680** | ❌ 変えない |
| 発注順(4候補+帯) | +1,755,799 (85%) | **+1,880,680** | ❌ 変えない |
| 戦略別 LOO | +1,780,009 | **+1,880,680** | ❌ 6戦略とも使う |

**「毎月その時点で最良を選ぶ」と、何も選ばないより悪くなる。** 3ブロックで同じ形。

有望に見えた行はすべて ✓ が付かない(前半・後半で符号が揃わない):

| 行 | 固定での見え方 | なぜ却下 |
|---|---|---|
| **delay3** | +2,133,063 / t=+7.99 / 10勝 | walk-forward が6ヶ月選んでも合計が H 単独に届かない |
| **建値 高い順** | H で z=+2.30(帯の外) | 前半 z+0.74 / **後半 z+2.46 = 後半だけ** |
| **VOLTF を外すな** | z=-2.20 | 前半 z-0.13 / 後半 z-2.28 = 後半だけ。18セルの多重検定 |

⛔ **delay は動かさない。** 今日 4631 DIC が損切り 5,704 → 決済 5,830 と2.2%突き抜け、
1日の損失の53%を作った。損切り超過の合計 -27,292円 は当日の損失 -28,392 のほぼ全部で、
ライン約定なら -1,100 だった。それでも**6ヶ月・10ヶ月の両方で delay0 < delay1 < delay2 < delay3**。
1日から一般化しないこと(18.24)。

#### 累積マージは継続でよい(実測)

H の経過月数別 円/件: 0-1ヶ月 **+752** / 2-3ヶ月 **+839** / 4-6ヶ月 **+788** / 7ヶ月〜 **+702**。
**全帯プラスで単調劣化なし。** 古い基準月を落とす根拠は無い。

#### ☐ 唯一の宿題: 金額均等のσ

| | 合計 | 月次σ | 月平均/σ |
|---|---:|---:|---:|
| H(100株固定) | +1,880,680 | 90,291 | +2.08 |
| **◆金額均等31万** | +1,937,966 | **67,297** | **+2.88** |

**σ が25%下がり、合計も落ちていない。** §18.30 で宣言した基準
(「σ が下がっていないなら揃える理由は無い」)を初めて満たした行。
ただし walk-forward が 86.6% < 90% なのでルール上は不採用。
**次に窓を広げたときも σ が下がっていれば、そこで検討する。**

#### ⛔ 失効した記録(daily.bat のコメント / 「H を採用した理由」ログ)

| 項目 | 旧記録 | **本節の値** |
|---|---|---|
| H vs 現行 | +95,546/月 t=+3.88 | **+150,293/月 t=+4.82** |
| -5tick の walk-forward | 固定最良の99.5% | **86.6%** |
| 発注順 walk-forward (H) | 97% | **85%** |
| BT | 30以上(プールの床) | **0(フィルタなし)** |

#### 判定ルール(以後これで判断する。回す前に宣言すること)

1. **walk-forward < 現状** → その設定は変えない。以上
2. walk-forward ≥ 固定最良の90% **かつ** 前半・後半が同符号(✓) → 変更を検討
3. |z| / t は**月数に応じた t分布の臨界値**で判定(10ヶ月=2.26 / 16本の帯=2.13)
4. パラメータ比較は**1件あたり**で見る。件数増で総額が増えただけなら無意味(18.28)
5. 判定は**同じ1回の実行**の中で。設定ごとにレポートを回すと比較相手まで動く(18.24)

---

### 18.37 ★ 確定した運用設定の一覧 (2026-08-13)

| 項目 | 値 | 根拠 |
|---|---|---|
| **エントリー** | **H = 前日終値 −5ティックの指値売り**(ザラ場到達も取る) | 18.36 |
| 約定 | 寄りが指値以上なら板寄せ(52%)、届かなければザラ場で高値が触れたら(48%) | — |
| ギャップガード | 寄りが前日終値 +3% 超なら見送り | 18.8 |
| **損切り** | **実約定価格 + 0.1ATR** | 18.28(sm/tm は動かさない) |
| **利確** | **実約定価格 − 1.0ATR** | 同上 |
| 引け | 15:20 に MOC 切替 → **15:30 まで粘る** | 18.4 |
| **損切り遅延** | **delay1**(約定した5分足の間は損切りを置かない) | 18.36(delay2/3 が固定最良だが walk-forward が支持せず) |
| **サイジング** | **100株固定** | 18.36(金額均等はσで有望だが保留) |
| **発注順** | **流動性(売買代金120日平均)降順 / 同値は銘柄コード順** | 18.21・18.36 |
| **BT** | **一切使わない**(フィルタ・並び順・タイブレーク全部) | 18.12〜18.24・2026-08-12 |
| **戦略** | **6つ全部**(MACDTF/A7/RSI2/DON/VOLTF/MOM) | 18.36・18.20・18.23 |
| **選定** | 累積マージ(2025-09〜)。per-symbol START_DATES で先読み防止 | 18.36・18.29 |
| **予算** | 400万円 / 倍率 **1.0** | 18.32⑨(寄りで一斉約定するので over-subscribe が成立しない) |
| 価格帯 | 1,000〜6,000円 | — |
| 手数料 | **0**(信用大口優遇プラン) | 18.14 |
| 転換 | **使わない**(記録のみ) | 18.26b |
| レポート | **2タブ**(`--h-tab` / 既定 `--default-tab lss`)。比較ブロックは lss タブに入るので、そこを既定で開く。発注は H タブから。逆指値の発注ボタンはコードで塞いである | 2026-08-13 |

#### コマンド

```
.\daily        # 朝(〜8:45)。H タブのみ。--h-tab --no-lss --default-tab h
.\dailyfast    # 同上の軽量版
python lss_budget_cap.py --execute --prod --entry-mode limit --limit-ticks -5 --budget-multiple 1.0
.\watch        # 寄り前〜15:30。--stop-delay-bars 1
.\fills        # 引け後。lss_trades_H.csv と突合(lss へフォールバックしない)
```

**手順は上の4つだけ。検算のコマンドは増やさない**(2026-08-13 ユーザー指示)。

代わりに **発注そのものに検算を埋め込んである**ので、手を動かす必要は無い:
- 発注ボタン(`order_server`): 発注が通った直後に検算し、**ボタンの応答文字列**に
  `⛔ 発注記録に問題: …` を出す。クリックしたその場で分かる
- `lss_budget_cap --execute`: 発注ループの後に全件検算し `[検算] 異常なし` / `⛔ N件`
- 実体は `check_orders.verify_row()` で、**両経路が同じ関数を呼ぶ**。
  別々に書くと片方だけ直して片方が漏れる(初日はまさにそれで、🚀発注 は atr を
  渡していたのに『100株 発注』だけ渡していなかった)

検算するもの: 注文方式が limit か / atr・sm・tm が入っているか / ショートの向き
(損切>指値>利確) / 幅が ATR×sm・ATR×tm と整合するか / 株数。

⚠ `check_orders.py`(`.\chk`)は**手で全件見たいときだけの任意ツール**。
   日々の手順には入れない。

⛔ **lss(逆指値)からは発注できない**ようにコードで塞いである:
- `order_server`: lssショートで entry_mode が limit/auction でなければ発注中止(`LSS_H_ONLY=0` で解除)
- `lss_budget_cap`: `--entry-mode` 既定 limit。stop で `--execute` するには `--allow-stop-entry`

#### 2026-08-13(初日)の実測

| | 結果 |
|---|---|
| **エントリー約定モデル** | **4/4 で滑り +0.00%**(3,730 / 4,802 / 1,903.5 / 1,834.5 が1円も違わず) |
| 決済 | 実測が +2,856円 有利。修正後の損切りは1円以内で約定 |
| 実損益 | -3,110円(上位4件のみ発注) |
| 約定率 | 4/4 (100%) |

**寄りの板寄せ約定モデルは実物と一致した。** これは母集団の議論と独立した確定事項。

#### その日に見つけて直した不具合(5件)

| # | 内容 |
|---|---|
| 1 | 発注サーバが H の指値を黙って1ティック下げていた(即約定回避が limit にも効いていた) |
| 2 | `lss_budget_cap --entry-mode limit` が **逆指値売りで発注**していた(`if _auction:` の書き間違い) |
| 3 | ATR が発注記録に無く、**損切りが丸ごと無効化**されていた(4銘柄が無防備) |
| 4 | 安全ガードが fail-open(損切り<=約定値なら0にする) → **fail-safe に**(引き上げる) |
| 5 | 場中に watcher を再起動すると無保護窓が5分→10分に伸びる → `.lss_watcher_seen.json` で復元 |

#### ★ 残る唯一の未知数 = 実運用の滑り

このシムは **slip=0**。H は決済の多くが損切り=成行の買い戻しで、そこは板の厚さで滑る。
`.\fills` を **10営業日ぶん**貯めれば `slip_daily_log.csv` で直接測れる(方式列で H だけ集計)。
**+806円/件 がどれだけ残るかは実測でしか分からない。それまでサイズを上げないこと。**


---

### 18.34b ⛔⛔ §18.34 の結果 = 候補ゼロ (2026-08-13 実施)。**この方向は閉じる**

19変数 × 5分位を掃いて **実装できる候補はゼロ**。銘柄属性(18.13 の15軸 / 18.24 の7属性)
に続き、**市場全体の寄り前変数でも何も出なかった**。3系統すべて否定。

#### 掃いた変数(すべて 09:00 JST 前に確定 / `preopen_market.py`)

先物-現物%(寄りギャップ予想) / |先物-現物%| / VIX水準 / VIX変化% / S&P500前日% /
|S&P500前日%| / NASDAQ / SOX半導体 / 米10年債(bp) / DAX / ユーロSTOXX50 / KOSPI /
USDJPY / ドル指数 / WTI / 金 / S&P500先物 / 日経前日% / 日経5日%

⚠ **アジア市場は当日の情報にならない**。東京が 09:00 JST で最も早く開くので、
   KOSPI(09:00 KST)も上海・香港(10:30 JST)も東京の後。使えるのは前日の引けだけ。

#### 結果 (113営業日 / 合計 +1,272,641円 / 巡回シフト帯16本 / 閾値 t(15)=2.13)

|z|≥2.13 を超えたのは **2つだけ**(帰無の期待 19×0.05 = 1.0個。Poisson(1) で2個以上は26%):

| 変数 | z | なぜ採用しないか |
|---|---:|---|
| **DAX 前日%** | +3.82 | **最悪が40-60%(真ん中)の分位**。単調でも山型でもなく機構の説明がつかない。事前予想にも無い変数 |
| **\|S&P500前日%\|** | -2.84 | **捨てると -189,205 損する** = 「米国が大きく動った日ほど良い」。文献の予想と向きは合うが**降りる材料ではない** |

#### 文献の予想は当たらなかった

| 文献 | 予想 | 実測 |
|---|---|---|
| Nagel(2012 RFS) *Evaporating Liquidity* | 短期リバーサル=流動性供給のリターンは VIX で強く予測できる。H は指値売り=流動性供給なので VIX が高いほど良いはず | **z=+1.10 / 単調でない** |
| Chen et al.(2026) 前日S&P500と日経先物の日中リターン | 前日の米国が強いほど東京の寄り30分は弱い(過剰反応→反転)。H は寄りで売るので米国が強い日ほど良いはず | **z=+0.24 / 単調でない** |

向き自体は弱く一致(VIX最大20%は最小20%の5.8倍 / S&P500は60-80%が最高)するが**帯の中**。

#### ★ in-sample で出なかったことの意味

この探索は **19変数を全部見て、最も悪い分位を事後に選んでいる** = 見つける方向に
バイアスがかかっている。**それでも出なかった。in-sample で出ないものが OOS で出る
ことはない**ので、walk-forward を回すまでもなく決着している。
(取引データ自体は per-symbol START_DATES + as-of BT で OOS。変数もリークなし)

#### ⛔ 帰無較正を **2回** 間違えた。同じ轍を踏まないこと

| # | 誤った帰無 | 何が起きたか |
|---|---|---|
| ① | 同数の日をランダムに落とす | **最小を選ぶ操作**だけで z が平均 **+1.17**(2,000試行で実測)。8変数の z が +0.31〜+1.61 に並んだ |
| ② | 日をランダムに5等分して最小群を落とす | ①は相殺できるが**シャッフルが時系列クラスタを壊す**。持続変数(VIX水準/DAX/5日リターン)で並べると分位は「時期のかたまり」になり、損益のレジームと重なって大きく振れる。ランダム5等分の最小群は持続変数ソートより**平均 +302,822円 高い** → 19変数中**9つ**が「有意」になった |
| ✅ | **分位の切り方は固定し、損益の系列だけを日付順に巡回シフト** | 変数の時系列クラスタも損益の自己相関も保たれる |

②は 18.13 に「**帰無較正は日ブロックを保つ。完全シャッフルは相関を壊して帰無分布が
狭くなり偽陽性を過小評価する**」と自分で書いてあったのに違反した。

★ **z を見る前に、まず差の絶対額が意味のある大きさか確かめる。**
   ②のとき「先物-現物 +1,913円 で z=+2.75」「S&P500先物 **-1,117円なのに z=+2.68**」
   が出ていた。合計 +1,272,641円 に対する1,913円に有意もなにもない。

#### 残っている確認(任意)

113営業日 / 分位22日は薄い。`.\daily --days 365`(約230日・分位46日)で1回見れば
DAX の中央分位が消えるかどうかで確定する。**それ以外にこの方向でやることは無い。**

#### 実装

`preopen_market.py`(変数生成) + レポートの `_preopen_day_html`(🌅 寄り前の市場変数)。
`LSS_PREOPEN_TAB=0` で無効 / `LSS_PREOPEN_SEEDS` で帯の本数。
毎回 2.3s なので置いたままでよい。

---

### 18.38 ☐ 進行中: J案 = 09:00にギャップを確認して資金を集中する (2026-08-15)

**現時点で未採用。運用は H のまま(§18.37)。** 明日ここから続ける。

#### ★ なぜこの調査になったのか (経緯)

1. **ギャップは唯一 単調だった軸。** 選別軸は 101検定以上で全滅している
   (§18.12 BTスコア / §18.13 15軸78検定 / §18.24 7軸 / §18.31 流動性で3度目)。
   その中で「**寄りが指値をどれだけ上回ったか**」だけが 186検定中ただ1つ単調だった:

   | 寄りの超過幅 | ザラ場 | Q1 | Q2 | Q3 | Q4 |
   |---|---:|---:|---:|---:|---:|
   | 利確率 | 6.8% | 8.4% | 10.9% | 12.9% | **14.9%** |
   | 外れ率 | 8.1% | — | — | — | **3.8%** |

2. **そこで指値を前日終値より上(+Nbp)に置いてみたら、100株固定では H より悪かった。**
   +Nbp寄指 は5本とも `H寄指` に **CI全域マイナス**(+0bp -54,772 / +25bp -66,147 /
   +50bp -79,810 / +75bp -96,876 / +100bp -112,338 円/月)。
   ギャップで**選別**はできているのに(円/件は +1,042→+2,021 と単調に上昇)、
   件数が減ったぶんの資金が遊ぶので総額が落ちる。

3. **★ ユーザー指摘がブレークスルー**:
   > 事前の注文だとどれがギャップアップして約定するか分からないから資金を集中できない。
   > それなら 9:00 の寄り付きで始値からギャップアップを判定して、
   > すぐその銘柄に100株以上を投資するのが効率的では?

   事前の指値は「どれが約定するか 09:00 まで分からない」ので最悪ケース(全部約定)で
   サイズを決めるしかない = 平均的に資金が遊ぶ。**09:00確認だけが 予算÷件数 で
   割り切れる。** 100株固定で比べると、この唯一の利点を潰していた。

#### J案の定義

| | H(運用中) | **J(候補)** |
|---|---|---|
| 発注 | 前夜に 前日終値−5tick の指値売り | **09:00 の始値**を見て判定 → 即成行売り |
| 建てる条件 | 指値に到達 | 始値が 前日終値**+50bp 以上** |
| 約定価格 | 指値以上(板寄せ or ザラ場) | **始値**(近似) |
| **株数** | **100株固定** | **予算400万 ÷ その日の合格件数**(100株単位) |
| 不約定 | あり(発注枠を空振りに消費) | **なし**(確認してから出す) |
| 決済・選定・戦略 | — | **すべて H と同一**(sm0.1/tm1.0/delay1/引けMOC) |

#### 実測 (365日窓 / 予算400万 / 当月除く10ヶ月 / slip=0 / 手数料0 / delay1)

| 方式 | 件数 | 円/件 | 月平均 | 月次σ | t | 月平均÷σ | 呼値2tick後 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **H (運用中)** | 2,230 | +792 | +176,607 | 72,470 | +7.71 | 2.44 | +89,773 (51%残) |
| J +0bp | 2,048 | +1,100 | +225,333 | 147,562 | +4.83 | 1.53 | +122,513 |
| J +25bp | 1,577 | +1,998 | +315,065 | 157,323 | +6.33 | 2.00 | +222,235 |
| **J +50bp** | 1,258 | +3,078 | **+387,235** | 129,933 | +9.42 | 2.98 | **+302,565 (78%残)** |
| J +75bp | 973 | +4,065 | +395,476 | 124,358 | +10.06 | **3.18** | +317,306 |
| J +100bp | 699 | +5,308 | +371,056 | 183,983 | +6.38 | 2.02 | +301,646 |

- **滑らかな単峰**(0→25→50→75 単調増 → 100で低下)。ピークは +75 だが
  +50 との差は +8,241円/月 で σ(12.5万)に対して無意味。**プラトー = +50〜+75**。
- 対応検定 `J+50bp vs H`: **+210,628円/月 / t=+5.02 / CI +115,800〜+305,456 / 10勝0敗**
- 擬似OOS: 前半 +196,658 t=+4.18 **5/5勝** / 後半 +224,598 t=+3.00 **5/5勝**
- **+50 を推奨**(+75 と点推定は同じ。銘柄数が多く 6.0 vs 4.7件/日、
  後半で改善している側、プラトーの安全端)。

#### ★ 集中度 — σ にも t にも出ないリスク (2026-08-15 実測)

資金均等は 予算÷件数 なので、**閾値を上げるほど1銘柄あたりが膨らむ**。
月次σ は日次の**合計**で測るので「1銘柄に寄せたこと」自体は見えない。
**t=+9.42 は集中していないことを意味しない。**

| 方式 | 件数/日 中央<br>(絞る前) | 最少 | 1銘柄 中央 | 95%点<br>(予算比) | 最大<br>(予算比) | 単元上限 | **予算が効いた日** |
|---|---:|---:|---:|---:|---:|---:|---|
| +0bp | 15 | 1 | 28万 | 56万 (14%) | 348万 (87%) | 0% | **56%** 116/209日 −1,790件 |
| +50bp | 7 | 1 | 36万 | 113万 (28%) | 397万 (99%) | 1% | **22%** 43/192日 −347件 |
| +100bp | 4 | 1 | 46万 | 186万 (46%) | 397万 (99%) | 4% | 9% 15/**160**日 −69件 |
| +50bp 上位3 | 3 (7) | 1 | 121万 | 190万 (48%) | 397万 (99%) | **7%** | **0%** 0/192日 |
| +50bp 上位5 | 5 (7) | 1 | 70万 | 173万 (43%) | 397万 (99%) | 3% | **0%** |
| +50bp 上位8 | 7 | 1 | 45万 | 130万 (33%) | 397万 (99%) | 2% | **0%** |

#### ★★ この表から確定したこと (4件)

1. **発注順の調査は不要になった。** 上位N を入れると **予算が効いた日 0%**
   = 並べ替える対象が存在しない。絞りなしでも 22% の日にしか効かない。
   当初「①何件建てるか ②サイズ ③順番」の3つに整理したが、**③は消えた**。
   (§18.21/18.24/18.31 で発注順が3度ヌルだったのと整合。ただし今回は
    「効かない」ではなく「**出番が無い**」という別の理由)

2. **閾値上げは稼働日を減らすが、上位Nは減らさない。** +100bp は 209→**160日**
   (営業日の15%で1件も建てない)。上位N は相対順位なので **192日を維持**。
   集中度は 上位3(95%点48%) ≒ +100bp(46%) とほぼ同じなのに、稼働日が32日多い。
   → **絞るなら閾値より上位N**という構造的な理由が数字で確認できた。

3. **「最少=1」の日は 400万が丸ごと1銘柄に入る**(最大 397万 = 予算の99%)。
   上位N では絶対に直らない(絞る対象が無い)。**金額上限でしか直せない**。

4. **単元上限(10単元=1,000株)は設計ミス。** 株価で意味が6倍変わる
   (1,000円株なら100万 / 4,000円株なら**400万=予算全額**)。価格帯は1,000〜6,000円。
   金額で切る `LSS_EQ_MAX_YEN` を追加済み(既定0=無効)。

#### つまみ (すべて直交する)

| env | 何を制御するか | 既定 |
|---|---|---|
| `LSS_EQ_TAB_KEY` | J タブに出す変種 | `H指値+50bp寄指資金均等` |
| `LSS_H_CONFIRM_GAPS` | 掃くギャップ閾値(bp) | `0,50,100` |
| `LSS_EQ_TOPS` | **何件に配るか**(その日のギャップ上位N)。1件の日には効かない | `3,5,8` |
| `LSS_EQ_MAX_YEN` | **1銘柄の上限額**(万円)。1件の日にだけ効く | `0`(無効) |
| `LSS_EQ_MAX_LOT` | 配りきれるか。**上げても最大露出は増えない**(`予算÷件数`が先に効く) | `10` |
| `LSS_EQ_ORDER` | 予算で切る順(`gap`/`liq`) | `gap` |
| `LSS_H_CONFIRM_SLOW=1` | 09:05約定版(保守側の下限)を出す | OFF |

⚠ **資金均等は E/H キャッシュより後の純粋な後処理**(5分足を触らない)。
   `LSS_EQ_*` を変えてもキャッシュは無効化されず、変種を足しても計算はほぼ増えない。
   → **これらのスイープは軽い**(`.\dailyfast` 1回ぶん)。

#### ▶▶ 次にやること = **`.\hvar` を1回** (2026-08-15 ユーザーが帰宅後に実行予定)

```
git pull
.\hvar          # = LSS_H_VARIANT_TAB=1 & dailyfast --days 365 --no-serve
```

見る場所: ロング銘柄ショート → 損益タブ → 折りたたみ
**「🎯 H の設定比較（指値位置 × 寄指か）」**。読む順番は3つだけ:

1. 表の一番下 **「▶ walk-forward 選択」** 行 →
   **現行(H)の行より下なら、そこで終わり**。設定を選ぶこと自体に価値が無い
2. 前半/後半の列(**✓**) → 同符号でなければノイズ
3. そのあとで 差/月 と t

`_h_variants` を全部回すので、J の変種(資金均等 / 上位N / 充填)も自動で
同じ表に並び、walk-forward の選択肢に入る。

⛔ **walk-forward が消せるのは「並んだ選択肢から最良を選ぶ」リークだけ**。
   『+50bp を候補に入れよう』『09:00確認を作ろう』という**発想自体**が
   この10ヶ月を見た後に出ているので、通っても「たぶん本物」止まり。
   ただし**落ちたら確実に不採用**という強いフィルタにはなる
   (§18.36 では H の3ブロックすべてが落ち、「何も変えない」が正解だった)。

#### ☐ そのあとにやること (優先順)

1. ~~上位N の損益を見る~~ → **⛔ 実施済み・棄却**(2026-08-15。下記「実測で決着した3件」)

2. **`LSS_EQ_MAX_LOT=20` で上位3をフェアに測り直す。**
   いま7%が単元上限で頭打ち = 設計どおり集中できていない状態の数字。
   ⚠ 上げても1銘柄の最大露出は増えない(遊ぶ資金が減るだけ)。

3. **`LSS_EQ_MAX_YEN` のスイープ**(100/150/200万)。
   ⚠ **リスク管理であってリターンの最適化ではない。**
   「一番儲かる上限」ではなく「**損益が動かない範囲で一番きつい上限**」を採る。
   月次σ ≒ 13万なので、差がその半分以内なら『測れていない = タダで尾を切れた』。

4. **walk-forward**(§18.36 判定ルール1)。**walk-forward < 現状なら変えない。**
   ⛔ +50bp閾値も資金均等も上位Nも、**全部この同じ10ヶ月を見て決めている**。
   §18.28 で「毎月その時点の最良を選ぶと何も選ばないより悪くなる」を実測済み。

5. **09:00 自動発注の実装**(kabu `/board` で始値取得 → ギャップ判定 →
   予算÷件数 → 一括成行売り)。`lss_exit_watcher` は OCO の基準を
   「トリガー価格」から「実約定価格」に変えるだけで流用できる。

6. **`.\fills` を10営業日ぶん貯めて実滑りを確定**(§18.37 の残る未知数)。

#### ★★ 実測で決着した3件 (2026-08-15)

##### ① 100株固定だと資金の74%が死ぬ ← +Nbp寄指が H に負けていた理由の正体

水準表に **稼働率**(1日の投入額の中央値 ÷ 予算)と **2単元以上**の割合を追加して判明:

| 方式 | 稼働率 | 2単元以上 | 月平均 |
|---|---:|---:|---:|
| H | 90% (360万/日) | 0% | +176,607 |
| H寄指 | 70% (280万/日) | 0% | +176,012 |
| H指値+0bp寄指 | 44% (178万/日) | 0% | +121,240 |
| **H指値+50bp寄指** | **26% (102万/日)** | 0% | +96,202 |
| H指値+100bp寄指 | **13% (51万/日)** | 0% | +63,674 |
| **同 資金均等** | **87% (349万/日)** | **36%** | **+397,328** |

**指値を上げるほど稼働率が崖のように落ちる。** +50bp では 400万のうち
**102万しかポジションになっていない**。理由は「不約定の注文が発注枠を食う」から。
15件注文しても3件しか刺さらず、残り12件ぶんの枠が空振りに消える。
09:00確認にすると不約定がゼロになるのでこれが起きない。
**これが J の優位の主要因**(稼働率 3.4倍 → 月平均 4.1倍)。

##### ② 上位N絞り = 棄却

| 比較 | 平均差/月 | t | CI | 前半 | 後半 |
|---|---:|---:|---|---|---|
| 上位3 vs 絞りなし | +18,371 | +0.54 | ゼロまたぎ | +68,104 (4/5) | **−31,362 (2/5)** |
| 上位5 vs 絞りなし | **+34** | +0.00 | ゼロまたぎ | +43,840 (2/5) | **−43,772 (1/5)** |
| 上位8 vs 絞りなし | −8,021 | −0.50 | ゼロまたぎ | +7,962 (1/5) | **−24,004 (1/5)** |

**3本とも CI がゼロをまたぎ、擬似OOS の後半で符号が反転**(§18.36 判定ルール2に該当)。
しかもコストだけ増える: σ 113,197 → **160,602**(+42%) / t 11.10 → 8.19 /
95%点集中度 28% → **48%**。上位3の 円/件 +8,398 は件数が495しかないだけで総額は同じ。
→ **「高品質に集中」はデータでは支持されず。全合格に均等が正解。**
   (ギャップは単調だが、+50bp という閾値の時点でもう取り切っている)

##### ③ 充填(余りを配り切る) = 中立。採用しない

素の均等割りは `予算÷件数` を1単元の値段で割って**切り捨てる**ので端数が残る
(稼働率87% = 約52万/日が遊ぶ)。配り切る版 `…資金均等充填` を実装したが、
**投入資金が1.12倍になるだけで月平均÷σ は変わらない**(レバレッジの微調整であって
エッジではない)。+48,000/月 は月次σ 11万の半分以下で検出もできない。
滑りの絶対額とテールリスクは1.12倍になるので、**現状のままでよい**
(ユーザー判断 2026-08-15)。1回だけ見る価値があるのは
「稼働率が上がったのに月平均が1.12倍にならなかったら、追加した単元が悪い銘柄に
乗った = ギャップ降順に意味がない」という別の情報が取れるから。

##### ④ 「合格を増やして100株ずつ」 vs 「絞って複数単元」

| 閾値 | 件数/日 | 100株ずつなら | 稼働率 | 2単元以上 | 月平均 |
|---|---:|---:|---:|---:|---:|
| +0bp | 15 | 495万 = **予算オーバー** | 96% | 14% | +307,236 |
| **+50bp** | 7 | 231万 = **169万余る** | 87% | 36% | **+397,328** |
| +100bp | 4 | 132万 = 268万余る | 86% | 48% | +364,202 |

**+0bp は「合格が多いので100株ずつで予算が埋まる」ケース**そのもの。
それでも +50bp に **月 9万円 負ける**。
→ **合格を増やして薄く広げるより、絞って1銘柄あたりを厚くするほうが良い。**

#### ★ 発注タイミングは『09:00 の始値を見て即発注』で確定 (2026-08-15 ユーザー判断)

**8:59 の気配値で前倒しする案は採らない。**「寄り前の気配値は当てにならない」
というユーザー調査結果による。09:00 の始値は確定値なので**判定ミスがゼロ**に
なり、気配値方式より確実。

⚠ ただしこれで **最大の未検証点が『執行速度』に確定**する:

| 発注タイミング | 月平均 |
|---|---:|
| 始値で約定(0秒・モデル) | **+397,328** |
| 09:05で約定(5分遅れ) | 約 +170,000 ← **H と引き分け** |

**5分遅れると J の優位は消える。** 0秒〜5分の減衰カーブは不明。
つまり **J の +220,721円/月 は丸ごと執行速度に賭けている**。
測る方法は1つだけ: **実装して `.\fills` で「実約定値 vs その日の始値」を取る**。

保守側(09:05約定版)は `set LSS_H_CONFIRM_SLOW=1` で出せる。

##### ⛔ 成行にしないこと。指値は『保護』に使い、閾値は『判定』に使う

| | 成行売り | 指値売り @前日終値+50bp |
|---|---|---|
| 約定 | 確実 | 寄り直後に下げていると**約定しない** |
| 値段 | 板次第(飛ぶと不利) | +50bp 以上が保証される |
| 逆選択 | なし | **あり** |

⛔ 指値@+50bp は**逆選択**を起こす。ショートなので「寄った直後に下げた銘柄」が
本命だが、指値をそこに置くと**まさにその銘柄だけ取り逃し、上がった銘柄だけ
建てる**ことになる。

**推奨: `指値売り @ 始値 × (1 - 0.5%)`**(現在値より下に置く保護指値)。
板が正常なら成行と同じ値段で即約定し(指値売りは指値以上で約定する)、
板が飛んで急落したときだけ約定せず掴まされない。
既存 lss の −3% ギャップガード(`_INTRADAY_5M_ENTRY_GAP_LIMIT` / §18.8)と同じ考え方。
**閾値(+50bp)は判定に、指値は保護に。役割を分ける。**

#### 🕗 判定マージンのブロック (レポートに追加済み)

気配値の履歴はどのデータにも無い(§18.35)ので「気配がどれくらい当たるか」は
測れない。測れるのは **どれだけ外れても判定が変わらないか** だけ。
損益タブ →「🕗 判定マージン（8:59 の気配で前倒し判定できるか）」に
合格銘柄の寄りギャップ分布と、気配が m bp ずれたときにひっくり返る件数を出す。
気配値方式は採らないことになったが、**この分布自体は「合格銘柄がどれだけ強く
ギャップしているか」の素性**なので残す。

#### ★★★ walk-forward 通過 (2026-08-15 `.\hvar`)。J は初めて判定ルールを通った

**最良 = `H指値+50bp寄指d3資金均等`**(+5,033,310円 / 10ヶ月 / 予算400万)。

| §18.36 の判定ルール | 結果 |
|---|---|
| 1. walk-forward < 現状なら変えない | **+4,622,645 vs 現状 H +1,766,068** → 該当せず |
| 2. walk-forward ≥ 固定最良の90% | **91.8%** ✅ |
| 2. 前半・後半が同符号(✓) | **前半1位 / 後半1位** ✅ |

**月別の選択: 10月=H(初月は既定) → 11月以降ずっと `d3資金均等`。一度も変わらない。**
不足分の内訳も完全に一致する:
```
固定最良 5,033,310 − walk-forward 4,622,645 = 410,665
10月の d3資金均等 589,580 − 10月の H 178,915 = 410,665
```
= **不足はすべて「初月は選びようがない」ぶんだけ**。選択そのものは1度も間違えていない。
§18.36 では3ブロックすべてが落ちたのと対照的。

vs 現行H: **+326,724円/月 / t=+9.10 / CI +245,493〜+407,955 / 10勝0敗**
擬似OOS: 前半 +289,246 t=+4.58 5/5 / 後半 **+364,202 t=+10.88 5/5**
呼値2tick後: +503,331 → **+420,941 (84%残)** vs H +176,607 → +89,773 (51%残)

#### ★★ delay は d0 < d1 < d2 < d3 と単調増加。⛔ 事前の推論は外れた

| delay | 100株固定 | 資金均等 月平均 | 勝率 | 月次σ | 月平均/σ |
|---|---:|---:|---:|---:|---:|
| **d0** | +536,994 | +234,973 | **27%** | 112,146 | 2.10 |
| d1 (それまでの既定) | +962,017 | +397,328 | 42% | 113,197 | 3.51 |
| d2 | +1,278,811 | +463,019 | 47% | 139,655 | 3.32 |
| **d3** | **+1,437,687** | **+503,331** | **49%** | 125,254 | **4.02** |

| 比較 | 差/月 | t | CI | 勝ち月 | 前半 | 後半 |
|---|---:|---:|---|---|---|---|
| d1 vs **d3** | **−106,003** | −3.88 | **全域マイナス** | **0/10** | −74,234 (0/5) | −137,772 (0/5) |
| d1 vs d2 | −65,691 | −2.20 | ゼロまたぎ | 3/10 | −32,696 | −98,686 |

⛔ **私(Claude)の推論は外れた。** 「J は約定価格も時刻もその場で確定するので
delay1 の機構的根拠(18.9)が当てはまらない = d0 が有力」と書いたが、**d0 が断トツで最悪**。
効いていたのは §18.9 の**経験的な**ほうの根拠 =「寄り1本目のヒゲで損切りが刈られる」。
約定が確定しているかどうかとは無関係に、**寄り直後のノイズを避けるほうが大きい**。
勝率が 27%→49% と上がっているのがその証拠。

⚠ **d3 はスイープ範囲の端**。単調増加している以上 d4/d5 が上の可能性がある。
`LSS_EQ_DELAYS` の既定を `0,2,3,4,5` に伸ばした。次の `.\hvar` で分かる。

⛔ **delay3 = 15分間 損切りを置かない。** 1銘柄に最大397万入る日があるので、
伸ばすほどテールリスクが増える。10ヶ月に事故が無いのは**サンプルに無いだけ**
かもしれない。数字が良くてもここは別に判断すること。
採用するなら **ライブ(watcher)も同じ本数に揃える**(§18.9 の鉄則)。

#### ★★★ delay と sm は代替ではない。**delay は σ を下げ、sm は σ を上げる** (2026-08-15)

「delay が単調に良くなり続けるのは、delay→∞ = 損切りなし に収束しているから
ではないか」という仮説を、sm を広げた版・損切りなし版と同じ表に並べて検証した。
**仮説は半分だけ正しかった。**

100株固定(サイズの影響を除いた素の比較 / 636件):

| 設定 | 合計 | 円/件 | **月次σ** | **月平均/σ** |
|---|---:|---:|---:|---:|
| d1(現行 sm0.1) | +962,017 | +1,513 | 64,660 | 1.49 |
| d2 | +1,278,811 | +2,011 | 45,224 | 2.83 |
| d3 | +1,437,687 | +2,261 | 42,618 | 3.37 |
| d4 | +1,545,719 | +2,430 | 35,392 | 4.37 |
| **d5** | +1,576,681 | +2,479 | **29,297** | **5.38** |
| sm0.3 | +1,205,411 | +1,895 | 61,281 | 1.97 |
| sm0.5 | +1,565,164 | +2,461 | 74,497 | 2.10 |
| sm1.0 | +1,722,487 | +2,708 | 98,004 | 1.76 |
| 損切なし | +1,866,944 | +2,935 | 90,796 | 2.06 |

**σ が正反対に動く:**
```
delay を伸ばす: 57,167 → 64,660 → 45,224 → 42,618 → 35,392 → 29,297  ← 単調減少
sm を広げる:    64,660 → 61,281 → 74,497 → 98,004 → 90,796           ← 単調増加
```

合計だけ見ると「どちらも損切りを緩めて良くなる」に見えるが、**リスクの意味では
正反対**。

##### 機構

* 寄り直後の5分は最もボラが高く、0.1ATR(≈0.25%)の損切りはほぼ確実に一度触る
  → 負けが量産される(d0 勝率 27%)
* **delay は「損切りを外す」のではなく「損切りが機能する時間帯を、朝のノイズが
  収まった後に限定する」**。その後は 0.1ATR のタイトな損切りが効くので σ が下がる
* **sm を広げるのは一日中ゆるくすること**。朝のノイズは避けられるが
  **午後の本物の逆行も止められない** → σ が上がる

##### ⛔ 「損切りなし」は採らない

資金均等での比較:

| 設定 | 合計 | 月次σ | **月平均/σ** |
|---|---:|---:|---:|
| d3 | +5,033,310 | 125,254 | 4.02 |
| **d4** | +5,288,990 | 123,130 | **4.30** ← ピーク |
| d5 | +5,711,190 | 143,592 | 3.98 |
| sm1.0 | +6,210,460 | 199,903 | 3.11 |
| **損切なし** | **+6,462,360** | **241,584** | **2.67** ← 最低 |

合計は損切なしが最大だが σ が 1.7倍。加えて **ショートの損切りなしは損失に
上限が無い**。1銘柄に最大397万入る日があるので、TOB や上方修正で +30% 飛べば
−120万。10ヶ月に事故が無いのは**起きなかっただけ**。

##### ⛔ walk-forward の限界が露呈した

walk-forward は +5,866,665 = 固定最良の **90.8%** で形式上は通過するが、
選んだのは **損切なし**(1月以降ずっと)。**walk-forward は「合計」で選ぶので
σ を一切見ていない**。その結果 walk-forward 自身の σ が 293,308 と全変種で最大、
月平均/σ は 2.00 で最低クラスになった。

**§18.36 の判定ルールは『合計最良を選ぶこと』の妥当性しか検証しない。**
リスク調整は別に自分で見ること。

##### 現時点の推奨 = **d4**(または d5)。損切なしは不採用

| | 合計 | 月次σ | 月平均/σ | 無防備時間 |
|---|---:|---:|---:|---|
| **d4** | +5,288,990 | 123,130 | **4.30** | 20分 |
| d5 | +5,711,190 | 143,592 | 3.98 | 25分 |

差は10ヶ月ではノイズ帯。**無防備時間が短い d4 を推す。**
⚠ d5 はまだ端なので `LSS_EQ_DELAYS` を `0,2,3,4,5,6,8` に伸ばした。
   σ が d5 で上がり始めているので頭打ちの可能性が高い。

#### ★★★ 決着 (2026-08-15 d8・sm・損切なしまで掃いた)。**delay は d4 で確定**

##### ① 決済理由の内訳で delay の正体が実証された

100株固定・同じ636トレード、**d0 → d8**:

| | 件数 | 損益 |
|---|---|---|
| 利確 | 68 → **138** (+70件) | +64万 → +131万 (**+67万**) |
| 損切り | 442 → **274** (**−168件**) | −54万 → −77万 (−23万) |
| 引け | 101 → **199** (+98件) | +44万 → +97万 (**+53万**) |

**損切りが減った168件が、利確70件・引け98件へそのまま移っている**(+70+98=168 で一致)。
```
利確 +67万 + 引け +53万 − 損切りの悪化 23万 = +97万
合計の差 +1,511,187 − +536,994 = +97.4万   ← 一致
```
**損切りはノイズで刈っており、外すとその大半が利確か引けのプラスになる。**
⚠ 損切りの1件あたりは深くなる(−1,222円 → −2,810円)。無防備の間に走るぶん。
  だが件数の減少がそれを大きく上回る。

##### ★★ ② σ は d5 で底。delay には固有の最適点がある

```
d0 57,167 → d1 64,660 → d2 45,224 → d3 42,618 → d4 35,392 → d5 29,297 → d6 36,729 → d8 34,442
                                                             ↑ 底           ↑ 反転
```
**「損切りが要らないに漸近している」わけではなかった。** 合計も d8 で減少に転じる
(d5 +1,576,681 → d6 +1,599,081 → d8 +1,511,187)。**探索終了。**

##### ③ 損切りを完全に外す最後の一歩は割に合わない

**d6 → 損切なし**: 利確 +6万 / 損切り +65万(ゼロに) / **引け −44万**(467件 +51万まで悪化)
= 合計 **+27万**。その代償に **σ 36,729 → 90,796 (2.5倍)**。

⚠ sm 系も同じ形。sm0.5 の損切りは 131件で **−90万**(1件あたり −6,870円) =
d5 の −65万より**悪い**。件数は減るが1件が激烈に深くなる。
→ **損切りは「幅を広げる」のではなく「時間帯を限定する」のが正解**、と内訳が示している。

##### ⛔ ④ walk-forward が 90% を割った

| | walk-forward | 固定最良 | 比 |
|---|---:|---:|---:|
| d5まで | +5,189,535 | +5,711,190 | **90.9%** ✅ |
| **d8・sm・損切なし込み** | +5,737,095 | +6,462,360 | **88.8%** ❌ |

**選択肢を増やしたら walk-forward が落ちた。** 月別の選択も揺れる
(sm0.5 → d6 → 損切なし → … → d6 → 損切なし)。§18.36 判定ルール2 に照らすと不合格
= 「損切なし・sm・d8 を候補に入れると毎月の選択が安定しない」。

##### ★ 結論 = `H指値+50bp寄指d4資金均等`

| | |
|---|---|
| 根拠 | 資金均等の 月平均/σ が **4.30 でピーク**(d3 4.02 / d5 3.98 / d6 4.02 / d8 3.73) |
| 100株固定 | σ 35,392 / 月平均/σ 4.37 |
| 無防備時間 | **20分**(d5 は25分) |
| vs 現行H | +510,434円/月 / t=+14.56 / CI +431,111〜+589,757 / **10勝0敗** |
| 擬似OOS | 前半7位 / 後半7位 ✓ |

⚠ d4/d5/d6 の差は10ヶ月ではノイズ帯。**無防備時間が短い d4 を採る**という
運用上の理由で決めた。**パラメータ探索はここで打ち切る。**

#### ⛔ 訂正: 充填は「中立」ではなかった。ただしレバレッジ

`充填 vs 素`: **+38,757円/月 / t=+3.86 / CI +16,054〜+61,460 / 9勝1敗**、
前半 +34,758 (4/5) / 後半 +42,756 (5/5)。**CI全域プラス・両半期同符号**。
稼働率 87%→**97%** / 遊び 48万→**5万/日**。

⚠ ただし **月平均/σ は 3.51 → 3.25 と落ちる**(σ が 113,197→133,983)。
同じトレードを大きくしただけなので**対応検定はほぼ確実に有意になる**
(差の分散が小さいため)。**t が高いことはエッジの証拠にならない**。
リスク調整後は中立〜わずかに悪化。採用の判断は「レバレッジを上げたいか」であって
「エッジがあるか」ではない。

#### ⛔ 上位N絞りは 2回目も棄却

上位3: 対応検定 +18,371 t=+0.54 ゼロまたぎ / 後半 **−31,362 (2/5)**。
順位も 前半2位 → **後半6位**。上位5: +34 t=0.00。上位8: −8,021。
**3本とも変わらず棄却。**

##### ⚠ K のラベルが実装と食い違っていた (2026-08-16)。**数字は有効・名前が誤り**

⛔ **この節は当初「K の実測値は無効」と書いたが、それは誤りだった。訂正済み。**
数字(月平均 +454,112 / vs H +510,434 / walk-forward 91.8%)は **09:00確認方式
として読めば正当**で、捨てる必要はない。間違っていたのは**方式の名前**だけ。

推奨タブは `H指値+50bp寄指…` という名前だったが、これを

  * **前夜指値**として読むと … 株数を前夜に決めるのに `予算÷約定数` で
    配っており、**配分が先読み**(どれが約定するかは 09:00 まで不明)
  * **09:00確認**として読むと … 建てる条件(寄り ≥ 前日終値×1.005)も
    約定価格(始値)も配分(予算÷合格数)も **すべて 09:00 に確定している = 正当**

つまり **同じ計算が、名前次第で不正にも正当にもなる**。09:00確認として
読むのが正しく、2026-08-15 のユーザー決定(「始値で近似してよい」)とも一致する。
`LSS_EQ_METHOD=confirm`(既定)に切り替えて、名前を実装に合わせた。

###### 何が起きていたか

K は名前のとおり **指値**方式で、実装は「前夜に 前日終値×1.005 の**寄付指値**を置く」。
約定価格は **寄り値**(板寄せ)で、09:00 に何かを見て発注するのではない。
にもかかわらずサイズは `予算400万 ÷ その日の**約定数**` で配っていた。

| | いつ決まるか |
|---|---|
| 株数 | **前夜**(注文を出すとき) |
| どれが約定するか | **09:00**(寄り値が出て初めて分かる) |

**約定数で割るには「どれが+50bp以上ギャップするか」を前夜に知っている必要がある。**
候補が30件で約定が7件なら、実装できるサイズの約4倍を建てていたことになる。
§18.37 が「寄りで一斉約定するので over-subscribe が成立しない = 予算倍率1.0」と
確定しているのと**まったく同じ理屈**で、分母は候補数でなければならない。

###### ⛔⛔ さらに強い制約: **注文の総額も予算を超えられない** (2026-08-16 ユーザー指摘)

信用の委託保証金は**発注時**に要る。つまり前夜に置ける注文の合計は 400万まで。
候補が多い日は `予算÷候補数` が **1単元の値段に届かない**:

| 候補数 | 1銘柄あたり | 1単元(1,000〜6,000円株 = 10〜60万) |
|---:|---:|---|
| 7件 | 57万 | 1〜5単元 建てられる |
| 15件 | 27万 | 多くが1単元。5,000円超は枠オーバー |
| 30件 | 13万 | **1,300円超は1単元も置けない** |

→ **注文を出せない候補が出る**。しかも「どれを出すか」は前夜に決めるので
**ギャップでは並べられない**(09:00まで分からない = 先読み)。

**帰結: 指値方式は『集中』ができない。** 候補が多い日は 100株ずつを枠の許す
だけ並べる = **H とほぼ同じ形**になる。集中が効くのは候補が少ない日だけ
(1件18日 / 2件以下36日 / 全192日 = 19%)。

⚠ 「見込みで大きく建てる」は**使えない**。約定は同日内で強く相関するので、
**最も約定数が多い朝＝最も予算を超過する朝**。板寄せは一瞬でキャンセルも
間に合わない(§18.37 で予算倍率を 1.0 にしたのと同じ理由)。

###### 修正② (2026-08-16 適用済み)

- 前夜方式は **流動性降順**(前夜に分かる順 / 18.21)で候補を埋め、
  **発注枠が尽きたら打ち切る**。出せなかった候補は約定しても建たない
- 発注順にギャップを使わない(先読み)
- **充填(余りを配り切る)は無効**。板寄せ後に株数は足せない
- 集中度の表に **注文件数 / 約定率 / 枠切れ件数** を追加

###### ★ 方式ごとに正しい分母が違う (ここが本質)

| 方式 | 約定価格 | 正しい分母 |
|---|---|---|
| **H指値…寄指**(前夜に寄付指値) | **寄り値**(有利) | **候補数**(=約定+不約定)。集中できない |
| **H寄り確認**(09:00に見てから成行) | 09:05(不利) | **約定数**。集中できる |

**価格は指値が有利、割り当ては確認が有利。** 推奨タブは前者の価格と後者の
割り当てを両取りしていた。どちらが勝つかは測らないと分からない。

###### 修正 (2026-08-16 適用済み)

- `_size_equal_by_day(..., _nf=)` を追加。渡すと**候補数**で割る
- `H指値…寄指` の資金均等は**既定で候補数割**(=実装できる形)
- 旧挙動を `…資金均等約定数割` として**1本だけ**残した。消すと上振れ幅が測れない
- 監査ボードのつまみに `div` を追加(候補数割 ⇔ 約定数割 を兄弟として比較できる)
- ラベルの方式名を修正。ずっと「09:00確認」と表示していたので実装イメージがズレていた
- **`LSS_H_CONFIRM_SLOW` を既定ONに戻した**。OFF にした理由(「5分遅れることはない」)は
  約定価格の話でしかなく、分母を見ると立場が逆転する。両方出さないと比較できない
- 確認方式の損切り遅延が **0固定**だったのも直した(推奨と同じ delay/sm で並べられる)

###### ⛔⛔ 私(Claude)が無断で戻した2件 — どちらも修正済み (2026-08-16)

**2026-08-15 に確定した決定を、断りなく元に戻していた。同じことを繰り返さないこと。**

| 決定 | やってしまったこと | 状態 |
|---|---|---|
| **約定 = 始値**(「寄り付き直後に自動発注するので始値で近似してよい」) | confirm 実装が 09:05約定だったため、方式切替と同時に 09:05 が既定になった | ✅ **始値に戻した** |
| **`LSS_H_CONFIRM_SLOW` 既定OFF**(「5分遅れることはない」) | 「両方の帯を見たい」と考えて既定ONにした | ✅ **OFF に戻した** |

**同じ決定を2箇所で覆していた。** 方式を切り替えるときは、その方式が
**過去の決定のどれを暗黙に変えるか**を先に洗い出すこと。

★ 現在の意味: `LSS_H_CONFIRM_SLOW` は **09:05版(保守側の下限)の行を追加で
出すか**だけを制御する(既定OFF)。始値版は方式が confirm なら常に出る。
これは**発注設定ではなくレポートの表示**で、売買には一切影響しない。

それ以外(delay4 / sm0.5 / tm1.0 / ATR14 / 上限50万 / 重複保有 / ギャップ+50 /
発注順ギャップ降順 / 上位N不採用 / 充填不採用 / 予算400万 / .bat の env)は
**1つも触っていない**(`git diff` で env の既定変更が2件だけであることを確認済み)。

###### ★★ 決定: K は **09:00確認方式** に切替 (2026-08-16 ユーザー判断)

前夜指値は発注枠の制約で**構造的に集中できない**(上記)。集中できるのは
09:00確認だけなので、そちらを推奨(K)にする。

```
K = H寄り確認+50bpd4sm0.5資金均等
    09:00 に始値を見てギャップ判定 → 合格件数が確定 → 予算400万 ÷ 件数 で発注
    約定は **始値**(2026-08-15 の決定)。決済は sm0.5 / tm1.0 / delay4 / 引けMOC
    保守側の下限(09:05約定)は set LSS_H_CONFIRM_SLOW=1 で並べられる
```

`LSS_EQ_METHOD` 1本で切り替わる(既定 `confirm` / `limit` で前夜指値に戻る)。
推奨タブ・delay/sm/tm/ATR/ギャップの**全スイープが方式に追随**し、
**もう一方の方式も同じ delay/sm で必ず1本出る**ので直接比較できる。

⚠ **設定は全部 掃き直しになる。** delay4 / sm0.5 / +50bp / ATR14 は
すべて **前夜指値の母集団**で選んだもの。確認方式は約定価格が 09:05 で
建玉を持つ区間も違うので、§18.38 #1〜#3b の結論は引き継げない。

▶ `.\hvar` を1回。水準表で次の3行を比べる(すべて delay4 / sm0.5 / +50bp):

| 行 | 意味 |
|---|---|
| **`H寄り確認+50bpd4sm0.5資金均等`** | **確認 × 約定数割** = 新しい推奨(K) |
| `H指値+50bp寄指d4sm0.5資金均等` | 前夜指値 × 候補数割 + 発注枠 = 実装できる形 |
| 同 `…約定数割` | 旧挙動。**この差が先読みの大きさ** |

集中度の表の **「分母」列**で、前夜指値の「枠切れ N件」を必ず見ること。
そこが大きいほど「集中できない」が効いている。

判定は §18.36 のルール(walk-forward < 現状なら変えない / 前半・後半が同符号 /
1件あたりで見る)。

###### ▶ 09:00確認を採るなら実装要件 (kabu)

§18.38 末尾の実測がそのまま効く:
- **08:5x に候補を登録して1回空読みしておく**(登録直後の初回 /board は 48秒)
- ウォームなら41件 6.3秒 / **並列2**(上げても速くならず429だけ増える)
- 登録上限50件。候補がそれを超えるなら流動性上位50件に絞る
- タイムアウトは再試行する(修正済み)
- **09:00 に全部が寄るとは限らない**(9984 は 09:06 に寄った実例あり)。
  寄っていない銘柄をどう扱うかのルールが要る

###### ★ 副産物: kabu の /board を叩く要件が消えるかもしれない

指値方式なら **09:00 に板を読む必要が無い**(前夜に注文を置くだけ)。
つまり §18.38 末尾の kabu 取得制限(コールド48秒 / 登録上限50件 / スループット
6.5件/秒)は**指値方式には無関係**。制限が効くのは確認方式だけ。
上の比較で確認方式が勝った場合にだけ、あの制約が実装のボトルネックになる。

##### ★ K の現時点の仕様 (2026-08-15。**まだ候補。運用は §18.37 の H のまま**)

> ⛔⛔ **この表は古い → 現行は §18.48。** 2026-08-21 から **J を実運用**しており、
> 閾値は **+75bp**、1銘柄上限は **予算の50%**。下表の `+50bp` / `上限50万` は使わない。

⛔ **下表の「判定/発注/株数」の記述は実装と食い違っていた**(上記参照)。
実装は「前夜に 前日終値+50bp の寄付指値」であって「09:00 の始値を見て発注」ではない。
株数の行も候補数割に変わる。**測り直し後に書き換えること。**

| 項目 | 値 | 根拠 |
|---|---|---|
| 判定 | **09:00 の始値**が 前日終値 **+50bp 以上** | 18.38(0/25/50/75/100 の単峰) |
| 発注 | **指値売り @ 始値 × 0.995**(保護指値。成行にしない) | 逆選択を避ける。閾値は判定・指値は保護 |
| **株数** | **予算400万 ÷ その日の合格件数**(100株単位・切り捨て) | 100株固定だと稼働率26% |
| **1銘柄の上限** | **50万(常時)** | #3b。5,000円超は自動的に100株だけ。最大は60万(予算の15%) |
| 重複保有 | **許す**(1銘柄1件にしない) | #3。σ が下がらない |
| **損切り** | 約定値 **+0.5ATR** / **delay4**(20分後に武装) | #2 再測定(2026-08-15 夜): 月平均/σ が sm0.5 で単峰の頂点 |
| **利確** | 約定値 **−1.0ATR** | #1 |
| 引け | 15:20 MOC → 15:30 まで粘る | 18.4 |
| 発注順 | ギャップ降順(予算が効く日は16%のみ) | 18.38 |
| 選定・戦略・手数料 | **H と同一**(累積マージ / 6戦略 / 0) | — |

実測(10ヶ月・当月除く): **月平均 +454,112 / σ 94,016 / 月平均/σ 4.83 / vs H +510,434円/月**

###### ★★ kabu の取得制限 — 実測済み (2026-08-17)

`python check_board_limits.py --prod --n 41` の実測（照会のみ）:

| | 結果 |
|---|---|
| トークン取得 | 0.02s |
| **41銘柄の一括登録**(`register_many`) | **0.00s** / 受理41件（上限50に未達）|
| **初回(コールド) 全件取得** | **48.08s** ⛔ timeout も出る |
| **2回目以降(ウォーム) 全件取得** | **6.27s** / 失敗0 ✅ |
| スループット | **約6.5件/秒で固定** |

★★ **登録直後の初回 `/board` は桁違いに遅い**(kabu がその銘柄の板を購読しに
行くため)。**09:00 に登録して即読むと 40〜50秒かかって間に合わない。**

→ **K の実装要件: 08:5x に候補を登録して1回空読みしておくこと。**

★ **並列を上げても速くならない**。並列 2→16 で秒数がほぼ変わらず(6.1〜9.1s)、
429 だけが 8→40回 に増える。**推奨は並列2**。

⛔ **タイムアウトを再試行していなかった**。`_get_json` は 429 しか再試行せず、
read timeout はそのまま送出していた。実測で **41件中24件が取れなかった**。
K は全候補の始値が要るので致命的。RequestException も再試行するよう修正済み。

★ **実例: 9984 は 09:06 に寄っている**(2026-08-14)。09:00 に全部は寄らない
(§18.32 の「先頭5分足が09:00でない日が13%」と同じ)。**何分待つかのルールが要る**。

⚠ ここまでは**場外での実測**。本番の 09:00 は板寄せ直後で最も混むので、
**寄り付き直後に測り直すこと**(`.\watch` を起動する前に1回)。

⛔ **未検証の前提が残っている**(採用の判断はこれが片付いてから):
①**09:00直後に発注できるか**(5分遅れると H と引き分けまで落ちる) ②slip=0
③walk-forward(閾値・資金均等・上限を同じ10ヶ月で選んでいる) ④#4〜#9 の未監査項目

#### ⏱ 実行時間 — フィルタ探索が単独で 91.8% だった (2026-08-15 実測)

**「変種スイープが最大のコスト」という .bat のコメントは間違いだった。**
`.\hvar` の最後に出る `[⏱ 工程別 所要時間]`(2026-08-15 / 3,054ペア):

| 秒 | 割合 | 工程 |
|---:|---:|---|
| **303.3** | **91.8%** | **フィルタ探索** |
| 14.6 | 4.4% | 戦略別LOO |
| 5.9 | 1.8% | 予算スイープ |
| **1.7** | **0.5%** | **H設定の比較ブロック(=変種スイープ)** |
| 0.5 | 0.1% | 資金均等の変種生成 |
| 351.0 | 100% | ★ 全体 |

- **フィルタ探索を既定OFFにした**(`LSS_FILTER_SCAN=1` で掘れる)。
  §18.13(15軸78検定) §18.24(7軸) §18.31(流動性) と**3回とも候補ゼロ**で、
  発注判断には使わない。母集団に比例して重くなる(ペアが2倍→ここも2倍)。
- **変種を減らす案には意味が無い**(1.7s)。`hvar.bat` / `dailyfast.bat` の
  「the single biggest cost of the P&L tab」という記述を実測値で訂正した。
- `.\hvar` から H ペインを落とした(`--no-h-tab`)。`--h-tab` は lss をもう1回
  フル計算する。監査ボード・⚖比較・集中度は全部 lss ペインにある。

★ **作法: 速度も推測で潰さない。** 実行の最後に工程別の総括が降順で出る。

#### ☑ 設定監査チェックリスト (2026-08-15 開始。1つずつ潰す)

**delay1 は lss(逆指値)から継承したまま J で検証しておらず、測ったら d4 が正解
だった。** 同じ構図のものが他にも残っている。以下を1つずつ潰す。

##### 検証の手順 (毎回これに従う)

1. **1回の実行の中で並べる**。設定ごとにレポートを回さない(18.24)
2. **他の設定は固定**する(例: delay=4 に固定して sm だけ動かす)
3. **合計だけで判断しない**。必ず **月平均/σ** を見る
   (walk-forward は合計で選ぶので σ を無視する。2026-08-15 に実際そうなった)
4. **前半/後半が同符号(✓)**。片方だけなら期間に合わせ込んだもの
5. **ノイズ帯より小さい差は「測れていない」**(月次σ ≒ 12万)
6. **1件あたりで見る**。件数が増えて総額が増えただけなら意味がない(18.28)
7. **walk-forward < 現状なら変えない**(18.36)
8. 決めたら **ここに「何を測ってどう決めたか」を記録**して次へ

##### 進捗

| # | 設定 | 現行値 | 由来 | 状態 | 実行 |
|---|---|---|---|---|---|
| — | 損切り遅延 delay | **4** | lss継承→修正 | ✅ **確定**(d0〜d8。月平均/σ 4.30 でピーク) | 済 |
| — | ギャップ閾値 | **+50bp** | J固有 | ✅ 確定(0/25/50/75/100 の単峰・プラトー+50〜75) | 済 |
| — | サイズ | **資金均等** | J固有 | ✅ 確定(100株固定/上位N/充填と比較) | 済 |
| **1** | **利確 tm** | **1.0ATR** | lss継承→検証済 | ✅ **確定・変えない**(下記) | 済 |
| **2** | **損切 sm** | **0.5ATR** | lss継承→再測定 | ✅ **確定・変更済み**(新母集団で sm0.5 が頂点。下記) | 済 |
| **3** | **重複保有** | **許す(変えない)** | lss継承→検証済 | ✅ **確定**(1銘柄1件は不採用。下記) | 済 |
| **3b** | **1銘柄の金額上限** | **200万** | **J固有・新設** | ✅ **確定**(下記) | 済 |
| **4** | **予算** | 400万 | ❌ lss継承 | ☐ 未実装。18.31 では lss で600〜800万が効率ピーク | — |
| **5** | **ギャップガード** | ±3% | ❌ lss継承(18.8) | ☐ 未実装。+50bp閾値と直接相互作用する | — |
| **6** | **価格帯** | **1,000〜6,000円** | lss継承→検証済 | ✅ **確定・変えない**(下記) | 済 |
| **7** | **引け決済** | 15:20 MOC | ❌ lss継承 | ☐ 未実装。sameday5m の改修が要る(重い) | — |
| **8** | **銘柄選定・6戦略** | WF+累積マージ | lss継承→検証済 | ⚠ **選定なしのほうが良い**(下記)。切替は未決 | 済 |
| **9** | **提案ファイルの生成条件が揃っていない** | — | 引数の付け忘れ | ☐ **原因確定・全月 再スキャン待ち**(下記) | — |

##### ★★ #8 選定は効いていない — **選定なしのほうが良い** (2026-08-15 実測)

`make_full_proposal.py` で全ペア(1,540銘柄 × 6戦略 = 9,240 → 価格・空売り可で
**8,106ペア**)を作り、選定ありと同条件で比べた。**つまみは選定の有無だけ。**

| | 選定あり(3,054ペア) | **選定なし(8,106ペア)** |
|---|---:|---:|
| 件数 | 1,960 | **3,193** |
| 月平均 | +562,258 | **+854,572 (+52%)** |
| 月次σ | 154,202 | 217,390 |
| **月平均/σ** | 3.65 | **3.93** |
| **資本効率**(月平均÷実投入額) | **16.57%** | **21.36%** |
| 逆算した実投入額 | 339万/日(予算の85%) | **400万/日(100%)** |

###### ★ +52% の内訳 — 『資金が遊んでいた』だけではない

```
稼働率の差      400万 / 339万   = 1.18倍  (+18%)
1円あたりの差   21.36% / 16.57% = 1.29倍  (+29%)
                      掛けると  = 1.52倍  (+52%)  ← 実測と一致
```

**「資金が遊んでいた」のは +18% ぶんで、残り +29% は『1円あたりのリターンが
悪かった』ぶん。** 選定は資金を遊ばせただけでなく、**選んだ銘柄自体が平均より
悪かった**。

理由: 選定条件は「TRAIN で PF>=1.5 かつ 8取引以上 かつ 損益>0」= **過去に勝った
銘柄を選ぶ**ルール。過去に勝ったものを選ぶとその後は平均以下になる(平均回帰)。
しかも選定は **lss式のエントリー**の成績で選び、実際に建てるのは **K**なので、
そもそも転移する保証がない。

⚠ 実投入額は「遊び額の中央値」からの逆算なので概算。ただし内訳が多少ずれても
両方が同じ向きなので結論は変わらない。

**総額でもリスク調整後でも選定なしが上。** §18.20 の「選定は何も足していない」が
正しく、§18.21 の撤回(static BT 込みの比較)が誤りだった。

⚠ 選定ありは **START_DATES(先読み防止)で各ペアの集計開始が後ろにずれる**ぶん
不利になる。ただしそれは選定を使う以上**必ず付いてくる制約**なので、
運用上の比較としては公平。「選定を正しく(先読みを消して)使うと、使わないより
悪い」という結論は変わらない。

###### ★ K のルールで選定し直す案は成立しない

K は H の **1/6 しか発火しない**(母集団 2,871 vs 17,017)。「TRAIN に8取引以上」を
課すとほぼ何も通らず、**2026-07 が150件に崩壊したのと同じ失敗**になる。
1ペア8取引を貯めるには4〜6年かかるが、5分足は 2024-07 が最古なので不可能。

###### ★ 副作用: 金額上限50万のコストが激減した

母集団が増えて薄い日が消えたため:

| | 選定あり | **選定なし** |
|---|---:|---:|
| 上限50万 vs 上限なし | 月 **−141,145** | 月 **−44,342** |
| 資本効率 | — | 21.36% vs 22.47% (**−1.11pt**) |

**資本効率の差は相対5%だけ。** 損益差のほとんどは単にレバレッジを落としたぶんで、
リスク当たりではほぼ損していない。→ **#3b の「フラット50万」は正しかったと追認。**

###### ★★ 作法: **母集団(土台)を先に決めてから、つまみを掃く**

選定の有無は『つまみ』ではなく『土台』。土台を変えると多くのつまみが動く。
2026-08-15 の2回のボードを突き合わせた実測:

| つまみ | 選定あり | 選定なし | 要再掃き |
|---|---|---|---|
| **損切 sm** | 0.5 が単峰の頂点(5.60) | でこぼこ・0.05 が最高 | ★★ **必須** |
| **利確 tm** | 全部 ❌ | **tm2 が ✅**(4.08 vs 3.93) | ★★ **必須** |
| **ギャップ閾値** | +50bp | 未再検証(候補3倍なので上げる余地) | ★★ **必須** |
| 金額上限50万 | コスト月14万 | コスト月4.4万・ほぼ効かない | ★ 意味が変わる |
| 予算400万 | 未着手 | 満額稼働 | ★ #4 |
| **損切り遅延 delay4** | d4付近で頭打ち | **同じ** | ✅ 不要 |
| **ATR期間14日** | 全部±0.1以内 | **同じ** | ✅ 不要 |

**delay と ATR だけが母集団に依存しなかった。** 逆に言えば、それ以外は
土台を変えるたびに掃き直しになる。**先に土台を決めること。**

###### ⚠ ただし sm0.5 は新母集団では頂点ではない

| sm | 0.05 | 0.1 | 0.2 | 0.3 | **0.5** | 0.7 | 1.0 | なし |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 選定あり | 4.51 | 4.54 | 5.22 | 5.39 | **5.60** | 5.16 | 5.07 | 4.22 |
| **選定なし** | **4.32** | 4.24 | 4.15 | 3.81 | 3.93 | 3.79 | 3.74 | 4.02 |

**単峰が消えてでこぼこになる。sm の結論は母集団に依存していた。**
ATR14日 と delay4 はどちらの母集団でも維持されるので、そちらは堅い。

###### ⚠ 切替の代償: 実行時間が 38秒 → 620秒

5分足 653,394銘柄日 / 1,272銘柄 を読むため。内訳は 戦略別LOO 60s /
予算スイープ 32s / トレード生成 88s。毎朝の `.\daily` に影響する。

###### ⛔ この検証で半日溶かした事故 (再発防止済み)

`--lss-proposal` に**存在しないファイル**を渡したら、黙って組み込みの旧
WATCHLIST(**265ペア**)にフォールバックして走り、3,054ペアのつもりが 1/12 の
母集団で「もっともらしいが全く別のレポート」が出た(基準が 525件 / +166,326)。
ATR・delay・sm の結論を1周ぶん無駄にした。
→ **明示指定で読み込みに失敗したら中止**するようにした(`auto` のときだけ従来
どおりフォールバック)。実行後は必ず
`[lss] 新選定で上書き: ... → 価格≤6,000円で Nペア` の N を確認すること。

##### ★★ #9 提案ファイルの生成条件が揃っていなかった (2026-08-15 原因確定)

**監査ツール `audit_proposals.py` で全14ファイルのヘッダを突き合わせて判明。**
`lss_proposal_2026-07.py` が150件しかない件は**不具合ではなく引数の付け忘れ**だった。

###### 原因① 7月だけ `--stop-delay-bars` を付けずに実行 = delay0

```python
ap.add_argument("--stop-delay-bars", type=int, default=0, ...)   # ⛔ 既定 0
```

**生成日時が答えを持っていた:**

| 基準月 | 生成日 | delay | 件数 |
|---|---|---|---:|
| 2026-**07** | **08-02** | **0**(フラグ無しで実行) | **150** |
| 2026-06 | 08-03 | 1(翌日 フラグを付けて実行) | 973 |
| 2025-09〜2026-05 | 07-26 | —(フラグが存在する前) ≒ 1 | 621〜1,012 |

§18.38 の実測で **d0 は全 delay 中で断トツに最悪**(勝率27% vs d1 42%)。
合格条件が `PF>=1.5 かつ 取引>=8 かつ 損益>0` なので、d0 で計算すると通る
ペアが激減する。1,012 → 150 は整合する。前月ペアの引き継ぎ率も **13%**。

###### 原因② 全月 `fee=0.001` で選定していた

§18.14(2026-08-07)より **前**に生成されたため。実口座は手数料0なので、
**払っていないコストで `損益>0` と `PF>=1.5` を落としていた**(株価3,000円×100株で
600円/件)。`--fee` は既定 None → `ble.FEE_PCT_ONE_WAY`(= env `LSS_FEE_ONE_WAY`、
既定0)を読むので、**いま回せば引数なしで自動的に0になる**。

###### 作り直し (これ1回。全月まとめて)

```
python scan_lss_universe.py --base-months 2024-12,2025-03,2025-06,2025-09,2025-10,2025-11,2025-12,2026-01,2026-02,2026-03,2026-04,2026-05,2026-06,2026-07 --stop-delay-bars 1 --workers 8
```

⛔ **`--stop-delay-bars 1` は必須**(既定0のまま回すと7月と同じ失敗を全月でやる)。
★ 一括モードは5分足ロードとバックテストを**1回だけ**行って各基準月に振り分けるので、
14ヶ月でも1ヶ月とほぼ同じ時間。どのみち fee が変わるので全月やり直しになる。

**delay=4(K の設定)にしない理由**: 実運用はまだ H(delay1)で、選定は H と K が共有する。
そもそも選定のエントリーは **lss方式(前日終値−1ティックの逆指値)**で H とも K とも違う。
選定を K の条件で作り直すかどうかは **#8(銘柄選定)** の話。いまは壊れたものを直すだけ。

終わったら `python audit_proposals.py` で ①delay が全月1 ②fee が全月0
③**件数が全月で増える**(コストが消えるので合格が増える) を確認する。

###### ★ 再スキャンの結果 (2026-08-15 実施済み)

**件数がほぼ倍増し、単調増加も回復した。**

| 基準月 | 旧 | **新** |
|---|---:|---:|
| 2024-12 | 502 | **215** ⚠ |
| 2025-03 / 06 | 416 / 588 | 645 / 1,011 |
| 2025-09 / 12 | 621 / 775 | 1,522 / 1,886 |
| 2026-03 / 06 | 916 / 973 | 2,045 / 2,331 |
| **2026-07** | **150** | **2,398** |

⚠ **2024-12 だけ減ったのは正しい**。旧ファイルの生成日が 2026-07-18 で、
§18.8(約定モデルの現実化)を入れた 07-19 の**前日**。当時は「常にトリガー価格で
約定」の楽観モデルだったので PF が高く出て合格が多すぎた。**215 が本当の水準**。

⛔ **これで K の実測値(§18.38)は全部 測り直しになる。** 母集団が約2倍なので、
`予算400万 ÷ その日の合格件数` の分母が増え、1銘柄あたりが小さくなる。
集中度は下がり、**上限100万が効く日も減る**(コスト月9万も下がるはず)。
#3b の判断も新しい母集団で確認すること。

☐ **新しく出た論点(#8 で扱う)**: 合格率が 2,398/9,047 = **26.5%**(以前は1.7%)。
`PF>=1.5 かつ 取引>=8 かつ 損益>0` は**手数料0.2%を引いた状態で決めたしきい値**
なので、摩擦ゼロの今は実質かなり緩い。ただし §18.20/18.21 で「選定が足している
ものは小さい」とも出ているので上げるべきかは自明でない。**まず新しい母集団での
実測を見てから**。

###### ⛔ このツール自体で踏んだバグ (同じ形に注意)

`SELECTED` の実際の書式は **3要素・シングルクォート**
(`('7203.T', '銘柄名', 'MACDTF'),`)で、銘柄名にカンマも入りうる。
2要素・ダブルクォート想定の正規表現が一致せず **全ファイル0件** と出て、
「選定が全部壊れている」という無意味な出力になった。
→ ヘッダの `合格 Nペア` と本文の件数を突合し、**食い違ったら即エラー終了**
するようにした。**パース崩れを本物の異常と読み違えない**ための保険。

##### ✅ #6 価格帯 = **1,000〜6,000円 のまま確定** (2026-08-18)

ユーザー提案「6,000円より上を計測したい。**値がさ株が有利なら watch も少なく
済む**」。`.\pxsweep`(ユニバースを10,000円に広げた別実行 / 建値の上限を掃く)。

| 建値の上限 | 件数 | 月平均 | 月次σ | 月平均÷σ | 資本効率 | 前半 / 後半 | 1銘柄最大 |
|---|---:|---:|---:|---:|---:|---|---:|
| 2,000円 | 851 | +213,063 | −56% | 2.37 | 15.51% | − / − | 12% |
| 3,000円 | 1,442 | +316,733 | −49% | 3.03 | 15.08% | − / − | 12% |
| **4,000円** | 1,625 | +355,542 | −48% | **3.35** | 15.09% | − / − | 12% |
| **6,000円(現行)** | 1,651 | +456,381 | −18% | 2.73 | **16.23%** | − / − | 15% |
| 8,000円 | 1,615 | +452,518 | +3% | 2.16 | 14.22% | − / − | 20% |
| 上限なし(10,000) | 1,575 | **+494,722** | — | 2.43 | 15.02% | 基準 | 26% |

**★ 6本すべて 前半・後半とも基準を下回る。** 期間依存ですらなく、基準が最良。

**月平均÷σ も資本効率も単調でない**(2.37→3.03→3.35→2.73→2.16→2.43 /
15.51→15.08→15.09→16.23→14.22→15.02)。**軸として機能していない** =
§18.13/§18.24/§18.31/§18.38 の「建値に識別力なし」の **5回目の確認**。

#### ⛔ 『値がさ株で watch を節約する』は成立しない

**上限を上げても件数が減らない**(上限を切ると資金が余って他の銘柄が入るので、
むしろ 1,615〜1,651 と基準 1,575 より**増える**)。銘柄数が減らないなら watch も
減らせない。加えて同じ実行で watch を掃くと:

| watch | 月平均÷σ | 判定 |
|---|---:|---|
| 25 | 2.22 | 減らすと悪化 |
| **50(現行)** | **2.43** | — |
| 100 / 無制限 | 3.23 / 3.26 | ❌ 後半が符号逆(期間依存) |

**減らすと悪化・増やすのは期間依存 → 50 が座りが良い。**

#### ⛔ この調査で踏んだ実装の穴(3つとも「黙って消える」形)

| # | |
|---|---|
| 1 | `_KN_KEYS` への追加漏れ。`_knobs()` と `_LBLN` に足しても、差分ループが見るリストに無いと『基準と0個違い』になり **変種が丸ごと表から消える**(件数も判定も出ない)。逆向きの漏れを起動時に警告するようにした |
| 2 | 集中度の誤爆ガード(最大露出が1.5倍違えば つまみ2つ)が pmax を掴み、**全行 ⛔比較不能**。集中度を動かすのが目的のつまみは除外する |
| 3 | 「上限なし(6,000円)」のラベルが決め打ちで、10,000ユニバースの実行では嘘になっていた |

★ 作法: **つまみを足したら `_knobs()` / `_LBLN` / `_KN_KEYS` の3箇所**。

##### ★★ #3b 1銘柄の金額上限 = **50万** に決定 (2026-08-15 ユーザー判断・最終)

**資金均等は `予算 ÷ その日の合格件数` なので、合格が1銘柄の日は400万が丸ごと
1銘柄に入る。** これは月次σ にも t にも出ない(日次の**合計**で測るため)。
**t=+9 は集中していないことを意味しない。**

###### 合格が少ない日の実測 (192営業日 = 10ヶ月 / +50bp / d4)

| 合格件数 | 日数 | 割合 | 1銘柄あたり |
|---|---:|---:|---|
| **1件** | **18日** | **9.4%**(月1.8日) | 予算の99% |
| 2件以下 | 36日 | 18.8% | 50%以上 |
| 3件以下 | 53日 | 27.6% | 33%以上 |
| 5件以下 | 75日 | 39.1% | 20%以上 |
| 中央値 | 7件 | — | 36万(9%) |

**「400万が1銘柄」は稀な事故ではなく月2日ペースの定常事象。**
⚠ 閾値を上げると急増する: **+100bp では 1件の日が40日(25%)**。
§18.38 で +100bp を推さなかった判断が集中度の面からも裏付けられた。

###### 上限の効き (銘柄計 = 同じ銘柄が同日に複数戦略で出たぶんを合算)

| 上限 | 差/月 | **月次σ 比** | 月平均/σ | 銘柄計95%点 | **銘柄計 最大** | 遊び/日 | 単元上限 |
|---|---:|---:|---:|---:|---:|---:|---:|
| なし(基準) | — | — | 4.58 | 35% | **99%** | 55万 | 20件 |
| **200万** | **−19,847** | **0.19σ** | **4.83** ← 唯一 基準超 | 35% | **50%** | 68万 | 13件 |
| 150万 | −44,781 | 0.43σ | 4.48 | 32% | 37% | 80万 | 7件 |
| 100万 | −90,187 | 0.87σ | 4.14 | 23% | 25% | 103万 | **0件** |

###### ★ 100万を選んだのは「損益で選んでいない」から (混同しないこと)

**上限は必ず損をする。利益が最大なのは上限なし(+473,959)。** 測ったのは
「いくら儲かるか」ではなく **「切るのにいくら払うか」**。

事前に宣言した基準(損益の低下が月次σの半分=5.2万/月以内)で通るのは **200万と150万**。
**100万は 0.87σ で基準外＝実際にコストを払う選択**であり、それを承知で採った
(2026-08-15 ユーザー判断: 「この事象はあまり起きないので時間を賭けたくない / 100万でいい」)。

| | 月あたりのコスト | 1銘柄の最大 | 1銘柄で最悪いくら負けるか |
|---|---:|---:|---:|
| 上限なし | 0 | 397万 (99%) | ストップ高(+20%)で **−80万** |
| 200万 | −19,847 (0.19σ) | 200万 (50%) | −40万 |
| 100万 | −90,187 (0.87σ) | 100万 (25%) | −20万 |
| **50万(最終採用)** | 未測定(さらに下がる方向) | **50万 (12.5%)** | **−10万** |

###### ★ 最終決定 = **フラット50万(常時)** (2026-08-15 ユーザー判断)

⛔ **条件付き(合格3件以下の日だけ)は試して棄却した。** 4件以上の日は上限が
完全に外れるため、同じ銘柄が複数戦略で出ると `予算400万/4 = 100万 × 3枠
= 300万` になり、**1銘柄の最大が 280万(予算の70%)** まで膨らんだ。
「1銘柄50万」と言いながら実効280万で、上限が機能していなかった。

| ルール | 1銘柄 最大 | 月平均 | σ | 月平均/σ |
|---|---:|---:|---:|---:|
| **フラット50万(採用)** | **60万 (15%)** | +562,258 | 154,202 | 3.65 |
| 条件付き50万(3件以下) | **280万 (70%)** | +615,748 | 133,001 | 4.63 |
| 同 + 1銘柄1件 | 196万 (49%) | +627,050 | 128,903 | 4.86 |
| 上限200万 | 280万 (70%) | +678,953 | 94,157 | 7.21 |
| 上限なし | 392万 (98%) | +703,403 | 96,747 | 7.27 |

コストは **月 −11.4万(vs 上限200万)**。上限が効く日は月2〜3日なので、
それを承知で受け入れた(ユーザー判断: 「月に数件なので、もういい」)。

###### ⚠ σ で上限を比べてはいけない (2026-08-15 に判明)

上限を入れると**薄い日に資金が寝る**ので、その月に薄い日が何日あったかという
**偶然**が月次損益のばらつきに乗る。σ が増えるのはリスクが増えたからではなく、
**投入額が月ごとに揃わなくなっただけ**。監査ボードに
**資本効率(月平均 ÷ 実投入額 = 月利%)** の列を足したので、上限の比較は
そちらで行う。

⛔ そもそも **薄い日ほど1件あたりの期待値が良いという事実はない**
(§18.13 で『同日発注数』を掃いて候補ゼロ、TEST はむしろ少ない日ほど悪い)。
**薄い日に大きく張るのは同じエッジへのレバレッジでしかない。**
`予算÷件数` はリスクに基づくサイジングではなく「金を使い切るルール」で、
同じ銘柄の同じシグナルが**他に何本出たかで13倍変わる**。
固定額(50万)+予算制約 のほうが構造として一貫している。

###### （旧）上限の水準比較

**「1銘柄だったら50万円まで。5,000円以上の銘柄なら100株だけ」** = `上限50万` の
既存挙動そのもの。価格帯 1,000〜6,000円 で1単元は 10万〜60万なので:

| 株価 | 1単元 | 建てる株数 | 投入額 |
|---|---:|---|---:|
| 1,000円 | 10万 | 5単元(500株) | 50万 |
| 5,000円 | 50万 | 1単元(100株) | 50万 |
| **6,000円** | 60万 | **1単元(100株)だけ** | 60万 |

`_lot = max(1, _lot) if _had <= 0` (1件目は最低1単元)があるので、
**5,000円超は自動的に100株のみ**になる。同じ銘柄の2枠目は上限を使い切って
いるので建たない。

⚠ **これはリターンの最適化ではない。** 実測の傾向は「きつくするほど損益もσも
悪化」(100万 5.60 → 150万 6.83 → 200万 7.55)。50万の実コストは未測定だが
100万(月−90,187)より大きいはず。次の `.\hvar` で基準が50万になるので、
`上限なし / 150 / 200` の行との差でそのまま読める(追加の作業は不要)。

**コストの機構**: 上限が効く日が16%あり、その日は予算の26%(103万/日)が
使われず寝る。利益の19%に相当する。

⛔ **バックテストは 150/200/上限なし を区別できない。** 10ヶ月に大事故が
1回も無いので、上限の**便益**は測れていない(無い事象の頻度は測れない)。
測れたのは**コストだけ**。したがってこれは「最適値」ではなく
**「1銘柄でいくら負けられるか」というリスク許容度の宣言**である。

★ **副産物: 上限100万だと単元上限(10単元)に当たるのが0件になる。**
§18.38 で「株数で切るのは本来おかしい(1,000円株なら100万・4,000円株なら400万)」と
書いた問題は、金額上限を入れると自然に消える。

⚠ ツールが「— 測れていない」と出したのは ✅リスク低減の条件を σ −10%以上に
していて 200万が **−9%** で1ポイント届かなかったから。判定はしきい値をまたいだ
だけで、中身は「損益は基準内・σ も下がる」だった。

###### ⛔ 実装バグ: 上限が銘柄単位でなかった (修正済み)

上限を**1注文あたり**でかけていたので、同じ銘柄が同日に複数戦略で出ると
`上限100万 × 3枠 = 300万` になり**まったく効いていなかった**
(実測: 上限100万でも銘柄計 最大が予算の74%)。`_symyen` で銘柄ごとに累計して切る。
余りを配り切る充填パスも同じ辞書を尊重する。

##### ⛔ #3 重複保有 = 変えない。1銘柄1件は不採用 (2026-08-15)

| | 件数 | 月平均 | 月次σ | 月平均/σ | 銘柄計 最大 |
|---|---:|---:|---:|---:|---:|
| 基準(重複を許す) | 1,375 | +473,959 | 103,570 | **+4.58** | 99% |
| 1銘柄1件(予算を割る前に畳む) | 1,194 | +468,676 | 106,811 (**+3%**) | +4.39 | **99%** |
| ◆1銘柄1件(株数決定後に落とす) | 1,201 | +420,665 | 94,017 | +4.47 | — |

**σ が下がらない**(むしろ +3%)。§18.30 で宣言した基準
(「σ が下がっていないなら揃える理由は無い」)を満たさない。

★ そして事前の見立てどおり **重複を外しても最大は下がらない(99% → 99%)**。
最大は「合格が1銘柄しかない日」に出るので、その日には重複が存在しない。
**最大を下げられるのは金額上限だけ**。#3b が入れば重複を畳む役目も無くなる。

##### 再確認: sm/tm は2回目も全滅 (delay4・上限なしの下で)

| つまみ | 月平均 | 月次σ | 月平均/σ | 判定 |
|---|---:|---:|---:|---|
| sm0.05 | +461,363 | 112,007 (+8%) | 4.12 | ❌ σ増 |
| **sm0.1(現行)** | +473,959 | 103,570 | **4.58** | — |
| sm0.2 / 0.3 / 0.5 | +515,204 / +523,095 / +538,303 | +21% / +32% / **+52%** | 4.12 / 3.82 / 3.42 | ❌ σ増 |
| tm0.5 | +451,572 | 75,460 (−27%) | 5.98 | ❌ **前半+175,730 / 後半−399,600 で符号が逆** |
| tm1.5 / 2 / 3 | +464,308 / +467,063 / +453,451 | +20% / +14% / +17% | 3.74 / 3.96 / 3.74 | ❌ σ増 |

**sm を広げると月平均は上がるがσがそれ以上に増える**(§18.38「delay は σ を下げ、
sm は σ を上げる」の再現)。tm0.5 は月平均/σ 5.98 と目を引くが期間依存。

##### ✅ #1 利確 tm = 1.0 のまま確定 (2026-08-15。delay4 の下で掃いた)

| tm | 月平均 | 月次σ | 月平均/σ | 前半/後半(基準差) |
|---|---:|---:|---:|---|
| 0.5 | +500,631 | 93,575 | **+5.35** | +175,550 / **−458,230** |
| **1.0(現行)** | +528,899 | 123,130 | **+4.30** | — |
| 1.5 | +529,001 | 171,179 | +3.09 | −60,290 / +61,310 |
| 2.0 | +529,862 | 172,957 | +3.06 | −61,420 / +71,050 |
| 3.0 | +514,538 | 177,260 | +2.90 | −182,830 / +39,220 |

**4本とも前半と後半で符号が逆** = 期間への合わせ込み。tm0.5 は 月平均/σ 5.35 と
目を引くが前半で稼いで後半で崩れる。上げる方向は σ が増えて明確に悪化。
**一度も掃いていなかった設定だが、結果は「変えない」。**

##### ★★ #2 損切 sm 再測定 (2026-08-15 夜。**選定を直した新母集団**で頂点が動いた)

⛔ **下の「sm=0.1 のまま確定」は旧母集団(1,328ペア)の結論**。#9 で提案ファイルを
直して母集団が 3,054ペアになったら、**sm0.5 が明確な頂点**になった。

条件: 窓365日 / 予算400万 / 上限100万 / delay4 / 流動性順 / 手数料0 / 件数は全行 2,049 で固定

| sm | 月平均 | σ | **月平均/σ** | 前半 / 後半(基準差) |
|---|---:|---:|---:|---|
| 0.05 | +495,892 | −4% | 4.51 | −58,930 / −157,710 |
| **0.1(旧確定)** | +517,556 | — | 4.54 | — |
| 0.2 | +567,718 | −5% | 5.22 | +82,100 / +419,520 |
| 0.3 | +591,491 | −4% | 5.39 | +169,350 / +570,000 |
| **0.5** | **+622,250** | **−2%** | **5.60 ← 頂点** | +250,680 / +796,260 |
| 0.7 | +656,543 | **+12%** | 5.16 | +362,190 / +1,027,680 |
| 1.0 | +672,008 | **+16%** | 5.07 | +438,770 / +1,105,750 |
| 損切なし | +686,916 | **+43%** | 4.22 | ❌ σ増 |

**合計は最後まで増えるが σ は 0.5 を超えると膨らむ**(発動減 < 1回が深くなる、の交差点)。
勝ち月 10/10(前半5/5・後半5/5)。呼値2tick後の残存 80%→**83%**。

###### なぜ広げると良くなるのか (実測で機構が特定できた)

d4 系列 615件・100株固定・同じトレード集合:

| | 勝率 | 成行決済(損切り+引けMOC) | 利確(=615−成行) |
|---|---:|---:|---:|
| sm0.1 | 52% | 484 | 131 |
| sm0.5 | **72%** | 469 | 146 |
| 差 | **+123件** | −15 | **+15** |

**勝ちが123件増えたのに利確は15件しか増えていない。残り約108件は
「引けで決済して、それが利益だった」トレード。**
= **損切りは負けを切っていたのではなく、勝ちを切っていた。**
0.25%(0.1ATR)逆行で発動 → だが引けまで持てば下がっていた。

3つの理由:
1. **名目と実態の乖離**(§18.24): 名目10:1 に対し実測ペイオフ 1.50:1、
   損失の中央値 −0.82%(名目0.25%の3倍)。**払う額はほぼ同じで発動回数だけ多い**
2. **同日決済なので引けが『無料の損切り』**。保有は最長6時間で必ず決済され、
   損失は当日の値幅で頭打ち。スイングと違い「損切りなし＝青天井」ではない
3. delay4 は寄り20分のノイズだけ。**0.25%は一日中ノイズ帯の内側**

###### ⛔ walk-forward は形式上 合格したが、選んだものは最悪

```
▶ walk-forward 選択  +6,431,313 / σ 189,402 / 月平均/σ +3.40 / 10勝0敗
月ごとの選択: 10:H → 11:sm1 → 12:上限なし → 01-04:d4sm1 → 05-07:損切なし
```
- ルール1(walk-forward < 現状なら変えない): +6,431,313 vs 現状 +5,175,560 → 該当せず
- ルール2(固定最良 損切なし +6,869,160 の90%以上): **93.6%** ✅

**しかし walk-forward 自身の 月平均/σ は 3.40 で、何も変えない現状(4.54)より悪い。**
選んだのは **損切なし**(σ 162,824・安定度最下位)。§18.38 の警告がそのまま再現:

> walk-forward は「合計」で選ぶので **σ を一切見ていない**。

**sm0.5 を walk-forward が一度も選ばないのは 0.5 が悪いからではなく、
合計だけ見れば常に損切なしが勝つから。** リスク調整は別に自分で見ること。

###### ⚠ 数字に出ないリスク

1. 損切り幅が5倍(0.25%→1.25%)。ただし実効は5倍より小さいはず(現状も実際は
   −0.82% 払っている)。**推測なので `.\fills` で実測すべき**
2. **後半に偏る**(前半+250,680 / 後半+796,260 = 3.2倍)。符号は両半期とも正で
   5/5 なので基準は通るが、`上限なし` の 1.01倍と比べると期間依存の余地が残る

###### ✅ 状態: **sm0.5 に変更済み** (2026-08-15 ユーザー承認)

推奨タブ(K) = `H指値+50bp寄指d4sm0.5資金均等`。
戻すなら `set LSS_EQ_TAB_KEY2=H指値+50bp寄指d4資金均等`。

⛔ **実運用に移すときは `lss_exit_watcher` の OCO も同時に 0.5ATR にすること**
(§18.9 の鉄則: バックテストとライブを必ず揃える)。K はまだ運用していない
(運用中は H = 前日終値−5ティックの指値売り・sm0.1)ので、いまは急がない。

###### 監査ボードを『つまみ単位の差分』に変えた (この変更に伴う必須の直し)

基準が2つ以上のつまみを持つと **prefix 一致では兄弟を拾えない**
(`d4sm0.5` の prefix では `d4sm0.2` が拾えない)。逆に緩めると2つ以上
違う行が混ざる(それが 2026-08-15 昼の「sm/tm が一斉に ✅」事故)。

→ 名前から **(遅延 / 損切 / 利確 / 金額上限 / 上位N / 充填 / 1銘柄1件)** を
  復元し、**ちょうど1つだけ違う行**を兄弟とする。2つ以上違う行は
  **隠さずに出して ⛔比較不能** と書く(黙って消すと掃き直しの必要に
  気づけない)。

⚠ これに合わせて **delay/tm のスイープも推奨 sm の上に載せた**。載せないと
  基準とつまみが2つずれて全部 比較不能 になる。**sm を変えたら最適な delay も
  動きうる**(§18.38 の代替関係)ので、次の `.\hvar` では delay も
  `d0sm0.5 〜 d8sm0.5` で掃き直される。**d4 が頂点のままか要確認。**

##### （旧・参考）#2 損切 sm = 0.1 のまま確定 (2026-08-15 昼。**旧母集団**)

| sm | 月平均 | 月次σ | 月平均/σ |
|---|---:|---:|---:|
| 0.05 | +503,252 | 128,451 | 3.92 |
| **0.1(現行)** | +528,899 | 123,130 | **4.30** ← ピーク |
| 0.2 | +569,470 | 138,147 | 4.12 |
| 0.3 | +588,558 | 160,941 | 3.66 |
| 0.5 | +599,828 | 168,705 | 3.56 |

**きれいな単峰で現行が頂点。** 広げると月平均は上がるが σ がそれ以上に増える(+37%)。
§18.38 の「sm は σ を上げる」がそのまま出た。継承値だがたまたま正しかった。

##### ⛔ #3 重複保有: 最初の測り方が不公平だった (2026-08-15 修正)

`_run_budget_sim(one_per_symbol=True)` は **株数を決めた後に**重複を落とすので、
落とした177件ぶんの枠が丸ごと遊ぶ。月平均 −108,234 のうちどこまでが
『リスクを減らしたコスト』でどこからが『資金を遊ばせた損』か分からなかった。

→ `_size_equal_by_day(_dedup=True)` を追加。**予算を割る前に**同日同銘柄を
   ギャップ最大の1本に畳むので、浮いた枠は残りに配り直される。
   変種 `…資金均等1銘柄1件` として並ぶ。**これで測り直す。**

参考(不公平だった版): 件数 1,201 / 月平均 +420,665 / σ 94,017(**−24%**) /
月平均/σ **+4.47** / 前半後半とも符号一致。σ は確かに下がっている。

##### 1〜3 の判定基準 (先に決めておく)

* **tm**: 下げると円/件は下がるが件数は変わらない(利確に届く前に引けで切るだけ)。
  上げると利確到達が減り引けが増える。**決済理由の内訳(2段目)で確認**。
  → `d4`(月平均/σ 4.30)を超える行があるかだけ見る。
* **sm**: delay と代替関係だが **σ の向きが逆**(18.38)。
  合計が増えても σ が増えていたら不採用。
* **◆1銘柄1件**: **必ず総額が下がる**(−1,442,320円)。見るのは **σ が下がったか**。
  下がっていなければリスク低減の効果も無いので、外す理由が無い。

#### ⛔ 未検証の前提 (J を採用する前に必ず潰すこと)

| 前提 | 状態 |
|---|---|
| 09:00直後に発注できる(約定=**始値**で近似) | ⛔ **最大の未知数**。5分遅れると H と引き分けまで落ちる。実測は `.\fills` の「実約定値 vs 始値」のみ |
| slip=0 | 呼値2tick列で下限は出した(78%残)。実測は `.\fills` 待ち |
| 閾値・上位N が OOS で保つ | **walk-forward 未実施**。擬似OOS(前半/後半)は通過 |
| 1銘柄に400万入る日のテールリスク | 10ヶ月に事故は無いが、**サンプルに無いだけ** |

#### レポート側の変更 (2026-08-15)

- **🔁 J タブ**を H の隣に追加(月別サマリー + 日別カード + 明細)。
  数字は ⚖表の同名列と定義上一致(同じ `_run_budget_sim`)。
- ⛔ **方式の記号 G は使い回さないこと**。`analyze_overnight_lss.py` と §18.32 で
  **G = 翌寄りロング(E の鏡像・−441円/件で棄却済み)**。I は数字の1と紛れるので
  飛ばして **J**。台帳は `nikkei_analysis.py` の `_EH_LBL` 直前にコメントで置いた。
- 使わないタブを既定OFF(戻し方はレポート冒頭に表示):
  `LSS_TAB_ALL` / `LSS_TAB_ENTRY` / `LSS_TAB_TENKAN` / `LSS_TAB_EH_ALL`
- **予算内タブ(H/J)の明細は打ち切らない**。月別サマリー(全件)と日別カード(300件)が
  食い違っていた(2026/06 が サマリー150件 vs カード11件)。`DETAIL_ROW_CAP` は
  『全取引』タブ専用に戻し、予算内は `LSS_EH_ROW_CAP`(既定0=無制限)。
- 不約定シグナル(`all_nofills`)に label/color/WF/rank を持たせた。
  E/H/J は不約定も**建てる**ので明細に出るのに、この行だけ「設定」バッジが空・
  WFスコアも基準月バッジも無しだった。**表示だけの修正**(損益は変わらない)。
  E/H キャッシュ版は v1 → v2。

---

### 18.39 ★★ 明日(2026-08-17 月)やること — テスト計画 (2026-08-16 確定)

**朝の kabu 実機テストは `.\mtest` の1コマンドにまとめてある。手で順に叩かないこと。**
順番と時刻が決まっており、手順を間違えると測定にならない(§18.38 で実際に
`--warmup` を飛ばして140秒級の数字を出した)。

#### ⛔⛔ 現行Hは実行しない (2026-08-16 ユーザー決定)

**明日以降は J/K のデータ取得に専念する。** したがって:

- **H の発注をしない**(`.\daily` は候補リストを作るためだけに回す)
- **`.\watch` も不要**(建玉が無いので監視対象がない)
- **kabu のトークンの取り合いが起きない** → 測定に専念できる

#### ▶ 朝 (kabu 実機・照会のみ・**発注しない**)

```
1. .\daily              ← 候補リストを作るためだけ(発注はしない)
2. .\mtest              ← 08:30 までに開始。09:00 過ぎまで自動で回る
```

`.\mtest` = `python morning_test.py --prod`。中でこの順に回す:

| # | 時刻 | 何を測るか | 合否の見かた |
|---|---|---|---|
| **1** | 開始直後 | `--rotate 100`(50件×2バッチ×2周) | **2周目が1周目より速いか** |
| **2** | 〜08:57 | `log_preopen_board`(気配ログ **初日**) | CSV ができていれば OK |
| **3** | 08:58 | `--warmup`(空読み) | ⛔ 飛ばすと4が測定にならない |
| **4** | **09:00** | `--open`(**板寄せ直後の実測**) | 場外の 6.3秒 と比べる |
| **5** | 4の直後 | `k_open_confirm --now`(**K のペーパー記録**) | 09:00に未寄の割合 |

##### 5. K のペーパー記録 = **これが「J/K のデータ取得」の本体**

`k_open_confirm.py`。K の朝の手順そのものを回して、**発注だけしない**:

```
08:5x  候補を50件バッチで登録し、1回空読み(ウォーム)
09:00  全候補の /board を読んで **始値** を取る
       → 前日終値比のギャップ → +50bp 以上を合格
       → 合格件数が確定 → 予算400万 ÷ 件数 で株数
       → k_paper_<日付>.csv に全部書く
```

- **J と K を同時に記録する**。読むのは1回でいいので、CSV に `in_j`(選定ありか) と
  `rank_liq`(流動性順位)を持たせ、**後から何通りにも切り出せる**ようにした。
  J = 選定あり × 流動性上位50件 / K = 全候補。
- **`OpeningPriceTime` で遅寄りを判定**する(`late` 列)。バックテストの実測は
  **15.7%**。大きく違うならモデルの前提が崩れているので要調査。
- ⛔ **発注機能を持たせていない**(売買系を import すらしていない)。
  引数を間違えても発注は起こらない。

★ 貯めたら **板の始値 = 5分足の始値** かを突合する。これはバックテストの前提
そのもので、ズレるなら K の全数字が影響を受ける。

##### 1. バッチ回し = **今いちばん重要**

50件は **総登録数**の上限で、51件以上はリクエストごと 400 で失敗する(部分受理なし)。
だが「50件登録→読む→全解除→次の50件」で回せれば100件読める可能性がある。**未検証。**

| 2周目 | 意味 | 100件の所要 |
|---|---|---|
| **速い** | kabu が購読を覚えている → 寄り前に全バッチを空読みしておけば 09:00 は全部ウォーム | **15〜20秒** |
| **遅い** | 毎回コールド | **2〜5分 ⛔ 間に合わない** |

★ 成立すれば **K の「登録上限50件」制約が消える**。watch上限の実測は
25:+245,509 / 50:+372,277 / 100:+485,332 / 無制限:+539,407 と**単調**なので、
読める数を増やす価値は確定している(§18.38)。

##### 4. 本番の取得速度 = **一度も測っていない**

これまでの実測(コールド48〜142秒 / ウォーム41銘柄6.3秒 / 6.5件/秒 / 並列2が最適)は
**全部 場外**。本番の09:00は板寄せ直後で最も混む。
**5分遅れると K の優位は消える**ので、数十秒以内なら合格。
結果は `board_speed_log.csv` に追記され、朝ごとに貯まる。

#### ▶ 昼以降 (PC で回すだけ。kabu 不要)

```
.\daily                        ← ⚠ 先に1回。下記4件の前提
.\dailyfast --days 365 --no-serve
```

| # | 見るところ | 何を判定するか |
|---|---|---|
| **5** | **シグナルタブ** | 銘柄が出るか。`[発注リスト] … (発注判定 N件)` の N が 0 でないか |
| **6** | 🔎 **母集団の月別診断**(J/K 各ペイン先頭) | 7月が薄いのは①設定/データの事故か ②相場か |
| **7** | 監査ボード「**判定のタイミング**」 | 2段階 / 一括締切 / 即時 が現行を超えるか |
| **8** | ⚖比較の **基準 vs 実装版** | 選定ありの効果(watch が揃っている唯一のペア) |

##### 5. シグナルタブ — **バグ修正の確認**

`LSS_SIGNAL_POOL` の絞り込みで `primary_cfg` のキーを取り違えており
(戦略の位置が `[1]` なのに `[2]`= `is_stop` の bool を渡していた)、
**発注リストが丸ごと消えていた**。`("7203","True")` を探すので1件も一致しない。
`_check_one` は `primary_cfg` に無い銘柄を「重複」として `check_signal_on_date` を
呼ばないため、シグナルが全滅する。**シグナルタブが 0.2s で終わるのがサイン**。
修正済み + 空なら絞り込みを捨てて続行 + 「シグナルなし」に判定対象の件数を出す。

⚠ **`holdout_selected_symbols.py` が空になっている**(研究実行が上書きした)。
`.\daily` を1回流して作り直すこと。`log_preopen_board` の既定の銘柄ソースでもある。

##### 6. 月別診断 — §18.38 #9 と同じ事故が起きていないか

7月/8月が薄い理由が **①候補そのものが消えている(設定・データの事故)** か
**②候補はあるが合格が少ない(相場)** かを切り分ける。
**候補日数がその月の営業日数(19〜22日)を大きく下回っていたら ①を疑う。**
候補日数が中央値の7割を切る月は赤 + 見出しに ⛔ が出るので、閉じたままでも気づける。

★ K(理想版)は `lss_proposal_full.py`(選定なし)なので月別提案ファイルを参照しない
= §18.38 #9 と**同じ経路の事故は入らない**。J(実装版)は `lss_proposal_cumul.py` 経由
なので提案ファイルの品質がそのまま効く。**J だけ凹んでいれば提案ファイル側、
両方凹んでいれば相場か5分足側。**

##### 7. 段階発注 (2026-08-16 実装)

09:00 に寄らない銘柄が **15.7%** ある(09:02〜09:06 に集中)。いまは丸ごと捨てている。

| 行 | 約定価格 | サイズ | 実装 |
|---|---|---|---|
| 09:00 の一発(現行) | 始値 | 09:00 の件数 | ✅ 遅寄りを捨てる |
| **2段階(10分刻み)** | 09:10バーの値 | 段の配分 | ✅ **できる** |
| 一括締切09:10 | 09:10バーの値(全員) | 締切後の件数 | ✅ できる |
| **…即時** | **自分の始値** | 段の配分 | ⛔ そのままは不可 |

段の配分 = `残り予算 × その段の候補数 ÷ 未判定候補数`。寄ったか/まだかは板で確定するので
**先読みにならない**。最終段は残り全額。実際に使った額を引く(見込みで引くと超過する)。

★ **「即時」と「2段階」の差 = 執行速度の価値**。判定は各銘柄の始値でできる
(判定に件数は要らない)が、`予算÷件数` のサイズ決定だけが件数を待つ。
差が小さければ2段階で十分、大きければ1分ポーリングを作る価値がある。
部分的な実現案: **100株だけ即建てて段で積み増す**(1銘柄1単元は必ず建てる設計なので下回らない)。

⛔ **1分刻みは5分足では測れない**。先頭バーが09:05の銘柄が実際に何分に寄ったかが
データに無いため、**刻みの下限は5分**(保守側に「そのバーが終わるまで確定しない」扱い)。
ライブは `OpeningPriceTime` で秒単位に刻める。制約はバックテストのデータ側だけ。

env: `LSS_EQ_WAVES`(既定 `10:10,5:30,10:30` = 刻み:締切) / `LSS_EQ_CUTS`(既定 `10`)。

#### ▶ 判定の作法 (毎回これ。回す前に宣言する)

1. **walk-forward < 現状なら変えない**(§18.36 ルール1)。まずここを見る
2. **前半・後半が同符号(✓)** でなければノイズ
3. **月次σ ≒ 12万** より小さい差は「測れていない」であって「改善した」ではない
4. **1件あたり**で見る。件数増で総額が増えただけなら予算制約下では無意味(§18.28)
5. 判定は **同じ1回の実行**の中で。設定ごとにレポートを回すと比較相手まで動く(§18.24)

#### ▶ 気配ログについて (§18.35 の本命)

**目的は「気配の精度を知ること」ではなく『登録上限50件の回避』。**
09:00 は数秒しか無いが **08:00〜09:00 は3,600秒ある**。50件バッチがコールド140秒でも
1時間で20周=1,000銘柄読める。→ 寄り前の気配で判定できるなら**候補数の制限が消える**。

見るのは値のズレではなく **「判定の反転」**。K が気配に求めるのは
「+50bp以上か」の判定だけなので、余裕のある銘柄は数bpずれても変わらない。
`--verify` は時刻ごとに 誤差の分位・相関・**反転率(誤って建てる/取り逃す)** を出す。
**数%なら気配で判定してよい。10%超なら09:00の始値方式を維持。**

⛔ **バックテストできない**(板・気配の履歴はどのデータにも無い)。今日から貯めるしかない。

##### ★ 母集団は候補だけでなく **広く**取る (2026-08-16 ユーザー提案・実装済み)

気配→始値の関係は **「発注候補であること」を必要としない**。どの銘柄でも同じように
測れるので、母集団を広げるほど1日で取れる件数が増える:

| 母集団 | 1日 | 1,000件に届くまで |
|---|---:|---|
| 発注候補50件だけ(旧) | 約50件 | **約1ヶ月** |
| 全候補(既定 `lss_proposal_full.py` ≒1,300〜1,500銘柄) | **数百件** | **数営業日** |

- `--max-symbols` の既定を **0=無制限**に。母集団は `lss_proposal_full.py` →
  `lss_proposal_cumul.py` → `holdout_selected_symbols.py` → `symbols_listed_prime.py`
  の順に自動検出(どれを使ったか起動時に印字する)。
- **今日の候補(`k_signals_<日付>.csv`)を先頭に置く**。時間切れで一巡できなくても
  いちばん知りたい銘柄は必ず取れる。
- **`is_cand` 列**で候補/候補外を後から切り分ける。`--verify` は 全体/候補/候補外 の
  3行を並べるので、**候補だけ性質が違わないか**を必ず確認できる
  (候補はシグナルが出た銘柄なので気配の付き方が違いうる)。
- ⛔ **締切(`--until`)の判定をバッチごとに行うよう修正**。母集団が大きいと1周が
  数十分かかるので、周と周のあいだでしか見ないと 09:00 を越えて K の測定と
  発注枠を食いつぶす(トークンは1つ)。カーソルは周をまたいで持ち越すので、
  読み切れなくても取りこぼしが偏らない。

##### ★★ 気配は寄りに近いほど当たる。だから最後の周を候補に充てる (2026-08-16 ユーザー指摘)

母集団を広げると1周に数十分かかるので、**先頭の銘柄は08:30の古い気配・末尾は08:55の
新しい気配**というムラが出る(母集団を広げたことの副作用)。是正:

- `--final-from`(既定 **08:50**)以降は **直前スイープ**。間隔を空けずに回し続け、
  **カーソルを先頭(=今日の候補)に戻す**。いちばん知りたい銘柄がいちばん新しい気配で上書きされる
- **`to_open_s`(寄りまでの残り秒)を全行に記録**。精度は「何時に読んだか」ではなく
  **「寄りまで何秒か」の関数**として見る(古いログは `ts` から復元する)
- `--verify` は3つ出す: ①時刻別 ②**残り時間別(本命)** ③**各銘柄の最新気配だけ = 実運用の精度**

**★ 決めるのは1点だけ: 「反転率が許せる範囲に収まる いちばん早い時刻」。**
そこから 09:00 までの秒数 × 6.5件/秒 = カバーできる銘柄数。
**精度とカバー範囲は直接トレードオフ**になっている。

| 遡れる時刻 | 09:00までの秒 | カバーできる銘柄 | 意味 |
|---|---:|---:|---|
| 08:50 | 600 | 約3,900 | 母集団を全部読める |
| 08:55 | 300 | 約1,900 | **上限50件の制約が消える** |
| 08:59 | 60 | 約390 | それでも50件よりは広い |
| 09:00 のみ | 数秒 | **50** | 現状(登録上限が天井) |

⛔ **反転率を±2ポイントで測るには500〜1,000件。貯まる前に何度も覗かないこと**
   (実質 in-sample になる)。**「N件貯まるまで見ない」と先に決めてから始める。**

#### ▶ 積み残し (明日でなくてよい)

- ⚠ **H を止めたので `.\fills` の実滑り測定は止まる**(§18.37 の残る未知数が
  未解決のまま残る)。K に切り替えるときは、K の実約定で測り直すことになる。
- ~~`k_open_confirm.py`~~ → **実装済み**(上記5)
- `.\hvar --days 730` でレジーム検証(いまは365日=13ヶ月)
- CLAUDE.md §18.38 の「K の現時点の仕様」表は **前夜指値時代の記述が残っている**
  (判定/発注/株数の行)。confirm 方式で測り直したら書き換える

---

### 18.40 ⛔⛔ 朝の実測 (2026-08-16) は **先読み**だった。この数字は使わない

**★ 結論を先に: +562,258/月・月平均/σ 3.65 は無効。実際に取れるのは +157,520/月。**

#### 先読みの正体 = 「どれが約定するか」を前夜に知っていた

前夜 寄付指値は **株数を前夜に決めるしかない**。ところが「どの銘柄が翌朝
+50bp 以上ギャップアップして約定するか」は **09:00 まで分からない**。

```
✅ 実装できる形: 予算400万 ÷ 候補数（前夜に出した注文の本数）
⛔ 先読み:       予算400万 ÷ 約定数（翌朝 実際に約定した本数）
```

fill率は **19.8%**(約定3,577 / 不約定14,504)。約定数で割ると
**実際の5倍の資金を各銘柄に配れる**ことになる。

**証拠**: 朝のファイルの集中度テーブルに **「分母」列が無い**。
この列(`候補数 / 注文N件 / 約定X% / 枠切れN件`)は候補数割の修正と同時に
2026-08-16 に追加したもの。**朝のファイルはその修正前に生成されている。**

| 同じ 前夜指値・cumul土台・watch50 | 件数 | 月平均 |
|---|---:|---:|
| 朝（約定数割 = **先読み**） | 1,960 | +562,258 |
| 今日（候補数割 = 実装できる形） | **685** | **+157,520** |
| 差 | **−65%** | **−72%** |

#### ★ そして同条件で測ると 前夜指値そのものが却下された

同じ実行・同じ母集団・同じ watch で **つまみは方式だけ**:

| | 件数 | 月平均 | 月次σ | 月平均/σ | 資本効率 | 前半 / 後半 |
|---|---:|---:|---:|---:|---:|---|
| **09:00確認** | **1,433** | **+388,718** | 153,535 | 2.53 | 15.56% | — |
| 前夜 寄付指値 | 685 | +157,520 | 58,316 | 2.70 | 18.41% | **−1,017,500 / −1,294,480** |

**件数 −52% / 月平均 −59% / 10ヶ月で −2,311,980円。** 前半・後半とも大幅マイナス。
原因は fill率19.8%: **前夜に置いた注文の80%が空振りし、その枠は他に使えない**
(委託保証金は発注時に要る)。09:00確認は確認してから出すので空振りゼロ。

walk-forward(実装できる設定だけ / 除外20本)も **10ヶ月で一度も指値を選ばなかった**
(指値は候補に残っているのに)。**2つの独立した検定が同じ答えを出した。**

#### ▶ 正しい基準 (2026-08-16 時点)

> ⛔ **更新済み → §18.48。** 閾値は **+75bp**、上限は **予算の50%** に変わった。
> 以下は 2026-08-16 時点の記録。

```
09:00確認 +50bp / delay4 / 損切0.5ATR / 利確1.0ATR / ATR14 / 1銘柄上限50万 / watch50
  1,433件 / 月平均 +388,718 / σ 153,535 / 月平均/σ 2.53 / 資本効率 15.56%
```

walk-forward: +5,018,133 / σ136,868 / 3.67 / t+7.42 / 10勝0敗 / 固定最良の94.6%。
選んだのは「上限なし」8ヶ月連続だが、**資本効率は 15.56% → 15.57%(+0.01pt)**。
ボード自身の基準どおり **レバレッジぶんでエッジではない** → **上限50万のまま**。

**設定は1つも変えない。**(§18.36 の前例と同じ「何も変えない」)

---

#### (以下は先読み込みの記録。数字は使わないこと。手順の教訓のためだけに残す)

### 18.40b 朝の実測の詳細 (⛔ 数字は無効)

**ファイル: `signals_holdout_all_both_2026-08-14_sel.html`**（`_sel` は手で退避した名前。コードは付けない）

#### 何を回したか (⛔ ここを取り違えて丸一日潰した)

| | |
|---|---|
| コマンド | **`.\hvar`**（`LSS_H_VARIANT_TAB=1` = delay/sm/tm/ATR の行がある）+ **cumul 土台** |
| **発注方式** | **前夜 寄付指値** = `H指値+50bp寄指d4sm0.5資金均等` |
| watch | 50件 / poolフィルタ **なし** |
| 決済 | delay4 / 損切0.5ATR / 利確1.0ATR / 1銘柄上限50万 |
| 予算 | 400万・流動性順・日別 |

⛔ **画面のラベルは「09:00確認」と出ていたが誤り**。同じ説明文の中に
「この数字は ⚖表の『**H指値+50bp寄指**d4sm0.5資金均等』列と同一です」と書いてあり、
集中度の表も**全行 `H指値…寄指`**（`H寄り確認` が1行も無い）。§18.38 に記録済みの
ラベル/実装の食い違いが、そのまま残っていたファイル。

#### 成績 (11ヶ月 2025/10〜2026/08)

```
全件（重複保有あり）   1,960件 72% +5,840,610円
重複保有なし          1,887件 72% +5,689,360円   （差 -151,250 / 重複73件）
```

監査ボードの **基準行**（完全月10ヶ月ベース）:

| 件数 | 月平均 | 月次σ | 月平均/σ | 資本効率 | 1銘柄の最大 |
|---:|---:|---:|---:|---:|---:|
| 1,960 | **+562,258** | 154,202 | **3.65** | **16.57%** | 15% |

月別:

| 月 | 件数 | 勝率 | 損益 | 必要資金(同時保有ピーク) |
|---|---:|---:|---:|---:|
| 2026/08 | 122 | 60% | +218,030 | 3,993,450 (最大15銘柄) |
| 2026/07 | 256 | 72% | +653,240 | 4,029,600 (15銘柄) |
| 2026/06 | 233 | 72% | +790,930 | 3,969,800 (16銘柄) |
| 2026/05 | 197 | 69% | +690,280 | 4,052,500 (17銘柄) |
| 2026/04 | 206 | 79% | +705,390 | 4,042,350 (17銘柄) |
| 2026/03 | 84 | 77% | +360,330 | 3,995,950 (16銘柄) |
| 2026/02 | 230 | 62% | +611,250 | 4,042,700 (17銘柄) |
| 2026/01 | 174 | 74% | +467,010 | 3,981,700 (18銘柄) |
| 2025/12 | 184 | 82% | +514,820 | 4,036,200 (17銘柄) |
| 2025/11 | 130 | 71% | +318,500 | 4,018,750 (17銘柄) |
| 2025/10 | 144 | 80% | +510,830 | 4,030,350 (20銘柄) |

2026/08 の日別: 08/14 15件40% −13,590 / 08/13 15件60% +2,900 / 08/12 14件86% +54,080 /
08/10 14件86% +72,530 / 08/07 13件77% +94,110 / 08/06 13件38% −5,220 /
08/05 14件36% −23,730 / 08/04 15件53% +10,200 / 08/03 9件67% +26,750

#### ★ 集中度 — **400万の発注枠を守った上での成績**

| | |
|---|---|
| 件数/日 中央 | **13件** |
| 1銘柄 中央 / 95%点 / 最大 | 28万 / 52万(13%) / **61万(15%)** |
| 単元上限に当たった | 0件 / 金額上限超え 977件 |
| **遊び** | **61万/日**（400万の15%） |
| **予算が効いた日** | **36%（73/201日）・−820件** |

**前夜に 13件 × 約30万 ≒ 400万 の指値を置いている。** 候補が13件を超える日(36%)は
流動性降順で埋めて**枠切れ**（820件は注文自体を出せていない）。

#### walk-forward (§18.36 の両ルールを通った初めての例)

```
▶ walk-forward 選択  +6,679,453 / σ 154,961 / 月平均/σ 4.31 / t +10.83
                     CI +506,788〜+774,352 / 10勝0敗 / 前半 +2,922,323 / 後半 +3,483,380
月ごとの選択: 10:H → 11:上限200万 → 12〜07: **上限なし**（8ヶ月連続）
```

| ルール | 結果 |
|---|---|
| ① walk-forward > 現状 | +6,679,453 vs +5,622,580 ✅ |
| ② 固定最良の90%以上 | **95.0%** ✅ |
| ② 前半・後半が同符号 | ✅ |

#### 金額上限のスイープ (この土台では**外すほど σ が下がる**)

| 上限 | 10ヶ月計 | 月次σ | 月平均/σ | 資本効率 | 1銘柄の最大 |
|---|---:|---:|---:|---:|---:|
| **50万(現行)** | +5,622,580 | 154,202 | 3.65 | 16.57% | **15%** |
| **150万** | +6,527,400 | **95,623(−38%)** | 6.83 | **18.20%** | 37% |
| 200万 | +6,757,910 | **89,500(−42%)** | 7.55 | 18.72% | 50% |
| なし | +7,034,030 | 96,747 | 7.27 | 19.17% | **98%** ⛔ |

★ 50万→150万 で **+904,820円/10ヶ月・σ −38%・資本効率 +1.63pt**。
⚠ §18.38 #3b で「月に数件なので、もういい」と50万を選んだ経緯があるが、
  この土台で測ると代償が大きい。**リスク許容度の再判断が要る**。

#### 設定は全部いまが頂点 (cumul 土台で掃き直しても再現)

delay4 / 損切0.5ATR / 利確1.0ATR / ATR14日 — **単峰でいまが頂点**。
✅ が付いた delay6(+0.19pt) と ATR20(+0.07pt) は月次σ(15万)に対して無視できる。
利確1.5/2/3・ATR3/5/7/10 は **前半後半で符号が逆**（期間依存）。

#### ⛔⛔ この日ハマった罠 (同じことを繰り返さない)

1. **ラベルを信じて中身を確認しなかった。** 「K=09:00確認」と何度も断言したが、
   朝のファイルは前夜指値だった。**方式は集中度の表の行名で確認する**
   （`H指値…寄指` なら指値 / `H寄り確認…` なら確認）。
2. **別々の実行を比べた**(§18.24 違反)。朝(指値/`.\hvar`/cumul/watch50) と
   昼(確認/`.\dailyfast`/full/watch無制限) は **つまみが3つ違う**ので、
   何が効いているか分離できなかった。
3. **Ctrl+F は非表示タブ(`display:none`)を検索しない。** 「1/1しか出ない」は
   「存在しない」ではない。タブを切り替えてから検索する。
4. **監査ボードに delay/sm/tm/ATR の行があれば `.\hvar`**、無ければ `.\dailyfast`。
   ファイルの出所はこれで判別できる。

→ 対策として **発注方式をつまみにした**(2026-08-16)。推奨変種を作るとき
   もう一方の方式の対も必ず作るので、`.\hvar` 1回で対応検定が出る。

#### ▶ 次にやること

```
.\hvar --lss-proposal lss_proposal_cumul.py
```

監査ボードの **「発注方式」** の行を見る:

| 結果 | 判断 |
|---|---|
| **指値が勝つ／同等** | ✅ **09:00 の板読みは不要**。`.\mtest`・気配ログ・`k_open_confirm` はお蔵入り |
| 指値が大きく負ける(月10万超) | 板読みを実装する価値あり。§18.39 の測定を予定どおり |

⚠ 未解決: 基準1,960件が **START_DATES を E/H に適用していない=先読み** かどうか。
  J実装版(pool付き)が 1,433件で **27%少ない**のはこれが原因の可能性。
  次の実行の被覆チェック(`⛔ N/3,508ペアが土台に存在しません` / `✅`)で切り分ける。
  **ここが崩れると +562,258 ごと消えるので、実弾に移す前に片付けること。**

---

### 18.41 ★★ 「即時」は実装できる (2026-08-16 ユーザー指摘)。⛔ 私の「実装不可」は誤りだった

**ユーザー提案**:「9:00以降に寄り付く銘柄も拾うべき。9:06に寄り付いたら
そこからすぐ注文を出せばいい」

**正しい。** 私は「サイズ決定に件数が要るから即時は実装不可」と書いたが、
**待たなくても配分は決まる**。

#### 逐次配分 (先読みにならない)

```
その銘柄に配る額 = 残り予算 ÷（その銘柄 + まだ寄っていない候補数）
```

| 時刻 | 状況 | 配分 |
|---|---|---|
| 09:00 | 候補50・板寄せ40・合格8・未寄10 | 400万 × 40/50 = 320万 → 8件に 40万ずつ |
| 09:06 | 1件が寄る。未寄あと9 | 残り80万 × 1/10 = 8万 → 1単元 |
| 09:30 | 締切。端数を配る | — |

**「寄ったか/まだか」は板を見れば確定する**ので未来を使っていない。
段階配分(`残り予算 × その段の候補数 ÷ 未判定候補数`)を銘柄単位まで
細かくしただけ。**全部が寄るまで待つ必要も無い。**

#### 価値 (今日の監査ボード / J 基準)

| | 資本効率 | 月平均 | 基準との差 |
|---|---:|---:|---:|
| 基準(09:00の一発・遅寄りは捨てる) | 15.56% | +388,718 | — |
| 2段階(10分刻み・締切まで待つ) | 15.11% | +402,027 | +13,309 |
| **即時(寄った瞬間・自分の始値)** | **17.00%** | **+455,599** | **+66,881** |

**「待つ」だけでは +13,309 しか増えない。価値は『自分の始値で建てられること』。**

#### 実装要件 (全部クリアできる)

| | |
|---|---|
| ポーリング頻度 | 50件ウォームで **6.3秒** → **10秒ごと**。09:00〜09:30 で180回 |
| 寄りの検知 | kabu の **`OpeningPriceTime`**(秒単位) |
| 締切 | **09:30 で十分**(遅寄りの93%が09:06までに寄る) |
| 全部が寄るまで待つ | **不要**。寄った銘柄から順に処理する |

#### ⚠ 制約

**バックテストの粒度は5分が下限**(先頭バーが09:05の銘柄が実際に何分に
寄ったかがデータに無い)。「即時」の行は5分刻みの近似。実運用で10秒
ポーリングにすればもう少し良くなりうるが**測れない**。
**+66,881円/月 を上限の目安**とすること。

---

### 18.42 ★★ J vs K は +9.1% しか違わない。**律速は銘柄数ではなく予算** (2026-08-16)

同じ1回の実行から作った J/K の月別を突き合わせた確定値。⛔ **これ以前に私(Claude)が
出していた「選定なしで +38.8%」は 別実行どうしの比較で無効**(§18.24 の作法違反)。

| 月 | J 実装版 | K 理想版 | 差 |
|---|---:|---:|---:|
| 2026/07 | +189,030 | +180,810 | **−8,220** |
| 2026/06 | +681,550 | +712,350 | +30,800 |
| 2026/05 | +544,030 | +545,760 | +1,730 |
| 2026/04 | +524,910 | +633,110 | **+108,200** |
| 2026/03 | +274,070 | +302,270 | +28,200 |
| 2026/02 | +294,830 | +328,070 | +33,240 |
| 2026/01 | +291,590 | +351,890 | +60,300 |
| 2025/12 | +417,640 | +454,640 | +37,000 |
| 2025/11 | +280,870 | +305,240 | +24,370 |
| 2025/10 | +388,660 | +425,620 | +36,960 |
| **10ヶ月計** | **+3,887,180** | **+4,239,760** | **+352,580** |

**K−J = +35,258円/月 / t=+3.49 / 95%CI +12,437〜+58,079 / 9勝1敗。**
有意だが **+9.1%**。最良月を除いても +27,153、最悪月を除いても +40,089 で頑健。

#### ★ 中身は「質」ではなく「量」だけ

| | J | K |
|---|---:|---:|
| **勝率** | **72%** | **72%** ← 1ポイントも違わない |
| 件数(10ヶ月) | 1,412 | 1,593 (+12.8%) |
| **円/件** | **+2,753** | **+2,662** (−91円) |
| 限界トレード(Kが追加した181件) | — | **+1,948円/件** |

K は良いトレードを見つけているのではなく、**同じ質のを多く**建てているだけ。
追加分は平均より29%劣る(プラスなので入れる価値はあるが薄まる)。

#### ★★ 予算律速の裏付け

**必要資金(同時保有ピーク)が J も K も全月 388〜399万で400万に張り付いている。**
候補を3倍に増やしても使える金は400万のままなので、件数が12.8%しか増えない。

```
1銘柄の投入額 = 予算400万 ÷ その日の合格件数
```
合格が増えるほど1銘柄が薄くなり1単元(10〜60万)を割る → 「最低1単元」で建つので
**予算は約13〜18銘柄で尽きる**。合格が13件を超えたら何件あっても同じ。
K が勝つのは「J が13件に届かなかった日」だけ。§18.38 の7月の件数減も同じ機構。

#### ★ L 中間版タブを追加 (選定なし × watch50)

K は **選定なし** と **watch無制限** を同時に変えているので、+35,258 がどちらの
寄与か分からなかった。**L を挟んで分離する**:

| タブ | 母集団 | watch | 実装 |
|---|---|---|---|
| **J 実装版** | 選定あり(cumul) | 50件 | 現行 |
| **L 中間版** | **選定なし(full)** | 50件 | **★タダ**(提案ファイルの差し替えだけ) |
| **K 理想版** | 選定なし(full) | **無制限** | ⛔ 50件の壁を破る必要 |

  **L − J = 選定をやめた効果**（今日から実装できる）
  **K − L = 読める数を増やした効果**（バッチ回し / 気配 / 楽天RSS が要る）

**L−J がほぼ全部なら、明日のバッチ回し・気配ログは不要**になる。

★ L の中身は **監査ボードの基準(`_EQ_TAB_KEY2` = 接尾辞なし = pool無し × watch50)
そのもの**。既に計算済みなのでタブに出すだけで計算は増えない。
env は `LSS_EQ_TAB_L`(既定 = `_EQ_TAB_KEY2`)。

⚠ `lss_trades_K.csv` の中身は **基準(=L)** で、タブの『K 理想版』ではない。
ファイル名は下流(`.\fills` の突合 / `measure_entry_decay`)が参照しているので変えない。

#### 優先順位(更新)

| # | やること | 価値 |
|---|---|---|
| **1** | **09:00 の執行速度の実測**(`.\mtest`) | **★★★ 5分遅れると −50%**。桁が違う |
| **2** | L タブで 選定効果 と watch効果 を分離 | ★★ タダで +9% が取れるか判明 |
| 3 | 予算を上げる | ★ 律速そのもの。ただし資金の判断 |
| 4 | 50件の壁を破る(バッチ回し・気配) | ↓ 上限 +9%。2 の結果次第でゼロ |

**#1 のリスク(優位の半分が消える)が #2〜#4 の利得(+9%)を圧倒している。**

---

### 18.43 ⛔ argparse の help に生の `%` を書かない (2026-08-16)

**`ValueError: badly formed help string` で スクリプトが起動すらしない。**

```python
help="これを超えるギャップは見送り(現行の±3%ガード)"    # ⛔ 落ちる
help="これを超えるギャップは見送り(現行の±3%%ガード)"   # ✅
```

argparse は help を `help % params` で展開する。**Python 3.14 から
`add_argument()` の時点で検証される**ので、生の `%` があると
実行前に例外になる(3.13 までは `--help` を出したときだけ落ちた)。

日本語だと `%ガ` のように**次の文字がマルチバイト**になり、
`unsupported format character '?' (0x30ac)` という読みにくいエラーが出る。

- リテラルの `%` は必ず **`%%`**
- 正当なのは `%%` と `%(default)s` などの `%(` だけ
- **f-string の help も同じ**(argparse は展開後の文字列に % 書式を掛ける)

全 340ファイルを AST で走査して該当は `k_open_confirm.py` の2件のみ。修正済み。
再走査するときは `add_argument` の `help`/`metavar` と `ArgumentParser` の
`description`/`epilog` を見ること。

---

### 18.44 ★★★ 09:00 の取得速度を実測 (2026-08-17)。**K は kabu では不可能 / J は成立**

**本番の板寄せ直後を初めて測った。** これまでの実測(§18.38 の 6.5件/秒)は全部**場外**で、
場中は**4〜5倍遅い**。しかも**ウォームアップの効果は18%しかない**。

#### 実測 (本番18080 / 50銘柄 / workers=2)

| 周 | 時刻 | 50銘柄の所要 | 速度 |
|---|---|---:|---:|
| warm (空読み・コールド) | 08:46 | 44.6秒 | 1.1件/秒 |
| **poll1 (本番)** | **09:00:36** | **36.5秒** | **1.4件/秒** |
| poll2 | 09:01:07 | 31.0秒 | 1.6件/秒 |
| poll3 | 09:02:26 | **79.3秒** | 0.6件/秒 |
| poll4 | 09:02:57 | 30.6秒 | 1.6件/秒 |
| poll5 | 09:04:33 | **95.9秒** | 0.5件/秒 |
| 参考: 場外(2026-08-16) | — | 6.3秒/41銘柄 | **6.5件/秒** |

**中央値 36.5秒 / 最小 30.6 / 最大 95.9 — ブレが3倍。** 安定した値として期待できない。

#### ⛔ 結論①: K(全候補を読む)は kabu では成立しない

| 銘柄数 | 中央値換算 | 最悪換算 | |
|---:|---:|---:|---|
| **50 (J)** | **36秒** | 96秒 | ✅ |
| 154 (K 中央値) | 1.9分 | 4.9分 | ⛔ |
| 299 (2026-08-17) | **3.6分** | **9.6分** | ⛔ |
| 614 (K 最大) | 7.5分 | 19.6分 | ⛔⛔ |

**§18.42 の K−L = +151,323円/月 は、kabu を使う限り取れない。**
楽天 MarketSpeed II RSS など別ソースが要る。

⛔ **§18.39 の「バッチ回しが速ければ壁が消える」という本命は否定された。**
同日の `--rotate 100` は 1周目205秒 → 2周目266秒 で「毎回コールド」と判定されたが、
**それも誤読**。原因はコールドではなく**場中の混雑**で、ウォームでも1件0.6〜0.7秒かかる。

⛔ **`--workers 8` は逆効果**(2026-08-17 実測)。429 レート制限が大量発生し、
指数バックオフ(7.5秒待ち)で完全に詰まる。§18.38 の「並列は2が最適」は場中でも正しい。

#### ✅ 結論②: J(選定あり × watch50)は成立する

**50銘柄を 09:00〜09:00:36 で読み切った。発注開始は 09:00:36。**
§18.38 の「5分遅れると優位が半減」に対し **36秒はその1/8**。減衰は小さいはず
(正確なカーブは未測定)。

⚠ ただし最悪95.9秒のブレがあるので、**1.5分は見ておく**こと。

#### ★ 遅寄りは 2%(1/50)。バックテストの前提15.7%より遥かに少ない

```
[09:00:36] 新たに寄った 49件 (通算 49/50)
[09:04:33] 新たに寄った  1件 (通算 50/50)   ← 4.5分後
```

**流動性上位50件だから当然**(流動性が高い=寄りが早い)。
→ **J の実運用では遅寄りをほぼ気にしなくてよい**。
→ 逆に K の母集団(低流動性を含む)なら 15.7% に近づくはずで、段階発注(§18.39 #7)が要る。

#### その朝の K の記録 (k_paper_20260817.csv)

| | |
|---|---:|
| 読めた | 50/50銘柄 |
| 合格(寄り ≥ 前日終値+50bp) | **5件** |
| 建てた | 5件 / **投入 187万** (予算400万の47%) |
| うち J(選定あり)の銘柄 | **2件** |

**50銘柄しか読めないので予算の47%しか使えていない。** これが「壁」の直接のコスト。

#### 残る宿題

- **板の始値 = 5分足の始値 か**を突合する(バックテストの前提そのもの)。
  ズレるなら K/J の全数字が影響を受ける
- 09:00 の36秒の遅れが損益にどれだけ効くか(`measure_entry_decay.py`)
- 気配ログ(§18.35)は今日は取れていない(mtest を中断したため)。明日から再開

#### ★★ 36秒の遅れがいくらか — 1分足で実測 (2026-08-17)

`measure_entry_decay.py --trades-csv lss_trades_hvar_K.csv --minutes 1,2,3,5`
(2,311/2,332件 突合 / 13ヶ月)

| 経過 | 中央bp | 平均bp | 寄りより不利 | 円/件 |
|---|---:|---:|---:|---:|
| 1分 | **−15.8** | −20.7 | 60% | −597 |
| 2分 | −26.8 | −30.4 | 68% | −869 |
| 3分 | −29.4 | −33.0 | 69% | −939 |
| 5分 | −36.6 | −39.5 | 71% | −1,118 |

**最初の1分で全体の43%が逃げる**(−15.8 / −36.6)。ショートなので寄り直後に
下がる銘柄が6割 = **遅れるほど構造的に不利**。§18.19/18.20 の「下ブレイクへの
反応」と整合する。

損益への効き方 (基準 = 5分後に売る):

| | 1分 | 2分 | 3分 | 寄り |
|---|---:|---:|---:|---:|
| **②引け決済だけ(確実に取れるぶん)** | 月**+58,029** | +27,375 | +17,767 | — |
| ③全件を線形換算(上界) | 月**+100,421** | +48,029 | +34,417 | 月**+215,333** |

※ ②が下限。利確/損切りのトレードは建値と一緒にラインも動くので、
  金額はそのまま効かない(効くのは『どちらに当たるか』の確率)。

##### ★ J の水準の補正

レポートの J は **約定=始値(執行が瞬時)** を前提にしている
(`★ 方式: 09:00確認(始値を見てから発注 / 約定は**始値**)`)。実測36秒ぶんは過大:

```
レポート          月 +388,718（瞬時執行の前提）
36秒の遅れ        月 −5万〜−10万
────────────────────────────
実際に取れる      月 +29万〜34万
```

**09:05 近似(悲観の下限)より 月6〜10万円 良い。** 08:5x のウォームアップは正しかった。

⚠ 参考: 5分の減衰 −36.6bp は **呼値2ticks(約7bp)の5倍**。
**執行速度はスプレッドより重要**、という順位づけになる。

##### ⛔ K については結論が変わらない(むしろ悪化)

| 銘柄数 | 読み取り | その間の減衰 |
|---:|---:|---|
| 50 (J) | 36秒 | −10bp程度 |
| 299 | 3.6分 | **−30bp** |
| 614 | 7.5分 | −40bp超 |

**K は読み終わる頃に値幅の大半が逃げている。** 速度の問題と減衰の問題が
二重に効くので、kabu で K を追う価値はさらに下がった。

---

### 18.45 ⛔⛔ K は追わない (2026-08-17 確定)。技術的にも経済的にも成立しない

**§18.42 の K−L = +151,323円/月 / K−J = +35,258円/月 は「執行が瞬時」の
前提でしか成立しない。今日の実測で執行コストを入れると符号が反転する。**

#### ① 技術的な可否

| 手段 | 可否 |
|---|---|
| kabu REST(§18.44 実測) | ⛔ 0.5件/秒。299銘柄で3.6分、614銘柄で7.5分 |
| kabu WebSocket(PUSH配信) | ⛔ **登録上限50件は同じ**。読む時間はゼロになるが壁は残る |
| kabu のバッチ回し | ⛔ §18.44 で棄却。2周目も速くならない |
| 寄り前の気配で事前判定 | △ 08:00〜09:00 の3,600秒が使える唯一の道。反転率次第 |
| 楽天 MarketSpeed II RSS 等 | △ 多銘柄同時は設計上可能。別口座・Excel経由・実装増 |

**kabu は「登録上限50件」が構造的な壁**で、REST でも WebSocket でも変わらない。

#### ★★ ② 可能になっても経済的にマイナス

| | |
|---|---:|
| K−J の利得(§18.42 / 同じ実行の対応検定 t=3.49) | **+35,258円/月** |
| 299銘柄の読み取り(§18.44 実測 0.73秒/銘柄) | 3.6分 |
| 平均待ち(順に読むので半分) | 1.8分 |
| J との差 | **+1.5分** |
| 1.5分の減衰(§18.44 の1分足実測から) | 約 **−20bp** |
| 建玉35万に対して | −700円/件 |
| K の月間件数(2,983件/13ヶ月 = 230件/月) | **月 −161,000円** |
| **差引** | **月 −126,000円** ⛔ |

**利得の4倍以上のコスト。** しかも利得は slip=0 の理想値、コストは実測。

#### ③ 手法としても良くない (2026-08-17 ユーザー指摘)

| 問題 | |
|---|---|
| **予算が律速なので増えない** | §18.42。候補を3倍にしても取引は **12.8%** しか増えない |
| 把握できない | 数百銘柄の建玉を人間が追えない。異常時に手が回らない |
| 低流動性が混ざる | §18.44 の測定がきれいだったのは**流動性上位50件**だから |
| 1銘柄が薄くなる | 予算400万÷30件 = 13万 → 1単元も建たない銘柄が出る |
| 選定の意味が消える | 全部対象にするなら選ぶ意味がない |

**「多銘柄を対象にする」の実態は「多く建てる」ではなく「選択肢を増やす」。**
その価値は +9.1% しかなく、執行コストが食い潰す。

#### ★ 結論

> **K は追わない。J(50銘柄・選定あり・watch50)に集中する。**

J は0〜18件しか建たないので**人間が把握できる規模**で、執行も36秒で済むことが
§18.44 で実証された。

⚠ 気配ログ(§18.35)は続ける。K のためではなく、**J の36秒をさらに短縮できるか**と
  『気配→始値』という独立した知見のため。1営業日で1,000件貯まるので安い。

#### ✅ 板の始値 = 5分足の始値 — 完全一致を確認 (2026-08-17 引け後)

`python verify_open_price.py`（当日の k_paper 50銘柄 × J-Quants ローカル5分足）

| | 結果 |
|---|---|
| 完全一致(<0.5bp) | **50/50件 (100%)** |
| 誤差 中央 / 90% / 95% / **最大** | 0.00 / 0.00 / 0.00 / **0.00bp** |
| **判定(+50bp)の反転** | **0件 (0.00%)** |
| 先頭バーが09:00 | **50件 (100%)** |
| データ出所 | **ローカル(J-Quants) 50/50** ← yfinance に落ちていない |

**「最大0.00bp」は統計ではなく構造的一致。** 誤差が散らばって平均0なら
サンプル数の議論が要るが、**全件が1円も違わない**ので、kabu の `OpeningPrice` と
J-Quants 5分足の始値は **同じものを指している**。日数を重ねても変わらない。

→ **バックテストの前提(約定=5分足の始値)は成立している。板で判定・発注してよい。**

★ 副産物: §18.32 で罠として記録した「**5分足の先頭バーが09:00でない日が13%**」が
  **今日は0%**。流動性上位50件だから当然(流動性が高い=寄りが早い)。
  今朝の「遅寄り2%」と合わせて、**J の母集団はこの種の問題から構造的に自由**。

⚠ 1日・50銘柄・全部が高流動性。特別気配の日や低流動性銘柄では違いうるので、
  `--glob "k_paper_*.csv"` で貯めながら見続けること(ただし優先度は下がった)。

#### ⛔⛔ 2026-08-17 の測定は **銘柄コード順の50件** だった（ユーザー指摘で発覚）

**`--max-symbols 50` が「流動性上位50件」ではなく「銘柄コードの小さい順50件」を
読んでいた。** 7936 アシックス(**+204bp・4戦略**)を丸ごと取り逃した。

原因は2つ:

| # | |
|---|---|
| ① | **`--collect` の出力に `liquidity` 列が無かった** → 並べ替える材料が無い |
| ② | **並べ替えを `--max-symbols` で切った *後* に置いていた** → 意味が無い |

`k_signals_*.csv` の行順は提案ファイルの順(≒銘柄コード順)。コードのコメントに
「候補ファイルの出現順を使う(=既に流動性降順のはず)」とあったが **この前提が誤り**。
実際 その朝の合格5件は 1515 / 1762 / 2270 / 2432 / 3105 と**全部小さい番号**だった。

修正: `--collect` が `liquidity` を書き出し、poll は **切る前に**流動性降順へ
並べ替える(流動性が取れない銘柄は最後尾 / §18.21)。並び順を印字して黙って
コード順に落ちないようにした。

##### 影響範囲

| | |
|---|---|
| ⛔ 「合格5件 / うちJ 2件」 | **母集団が誤り。無効** |
| ⛔ +204bp のアシックスを4戦略ぶん取り逃した | J なら大きな利益だった |
| ✅ 執行速度 36.5秒 | **影響なし**(速度測定なので顔ぶれは無関係) |
| ✅ 板の始値 = 5分足(50/50一致) | **影響なし**(別の50銘柄でも結論は同じ) |
| ✅ 1762 の約定値・損切・利確がレポートと完全一致 | **有効**(偶然コード順で入っていた) |

★ 教訓: **「順序は既にこうなっているはず」というコメントを信じない。**
  切る・絞る処理の直前で明示的に並べ替え、**何順で並べたかを印字する**。

##### ★ そして 7936 は **J では建てない**(ペア単位で交差ゼロ)

| ファイル | 7936 のペア |
|---|---|
| **cumul(選定あり = J の母集団)** | **A7 のみ** |
| full(選定なし = K/L の母集団) | MACDTF / A7 / RSI2 / DON / VOLTF / MOM |

今日 7936 でシグナルが出たのは **MACDTF / DON / VOLTF / MOM**。
**cumul の A7 は発火せず、発火した4戦略は cumul に無い = 交差ゼロ。**
→ H タブに出て J タブに出ないのは **設計どおり**。不具合ではない。

⛔ 訂正: 「今朝読めていれば J でも合格していた」は誤り。
   取り逃したのは **K/L のトレードだけ**で、J には影響しない。

##### ⛔ `in_j` の判定が銘柄単位で過大だった(同時に修正)

`in_j` は「その**銘柄**が cumul にあるか」だったので、7936 のように
**cumul には A7 だけ載っていて別戦略で発火した銘柄**を J 候補と数えてしまう。
実際の J は **ペア単位(銘柄×戦略)** で絞る。

修正: cumul から (銘柄, 戦略) を抽出し、**今日のシグナルのペアと交差**させる。
交差できないときは銘柄単位にフォールバックし「過大評価になる」と警告を出す。

⚠ 2026-08-17 の「J 合格2件」(1762×DON / 3105×A7) は**ペア単位でも成立**して
  いたので、当日の数字は変わらない。修正は今後の過大カウントを防ぐもの。

##### ⛔ H タブと J タブは母集団が違う。月次を直接比べてはいけない

| | 母集団 | サイジング | 1銘柄上限 |
|---|---|---|---|
| **H タブ** | **選定なし**(土台 full) | 100株固定 | **なし** |
| **J タブ** | **選定あり**(cumul) | 資金均等 | 50万 |

2026-08-17 に「H +165,701 vs J +79,350」と並べたが、**条件が4つ違うので
比較として無効**。方式の比較は ⚖比較ブロック(同一母集団)で行う。

★ 実例: H は上限が無いので 7936 に **4枠 × 53.4万 = 213万**(必要資金の68%)を
  入れられた。J なら1銘柄50万が上限なので1枠だけ。今日は当たったが、
  逆に振れれば −95,184。**J の設計はこの集中を意図的に防いでいる**(§18.38 #3b)。

---

### 18.46 ⛔⛔ 2026-08-19 の事故 — watcher 即死 → 8建玉が終日ノーガード → 持ち越し → 強制決済

**J の運用で最初に起きた実損事故。原因は「決済を1本のプロセスに依存していた」こと。**

#### 経緯（分単位）

| 時刻 | 何が起きたか |
|---|---|
| 09:00:36 | `k_open_confirm` が8銘柄を発注 → 建玉ができる |
| 09:10 | `k_open_confirm` 終了・トークン解放 |
| 09:10 | watcher 起動 → **2秒後に即死** |
| 09:10〜15:30 | **6時間20分 板に何も乗っていない**。決済されず |
| 翌 08-20 07:07 | 証券会社が**寄り付き成行**で強制決済 |

#### 実額

| | |
|---|---:|
| 実現損益 | -13,120円 |
| 強制決済手数料 2,200円 × 8銘柄 | **-17,600円** |
| **合計** | **-30,720円** |
| （昨日の引けで決済できていれば） | +6,080円 |
| **事故のコスト** | **-36,800円**（夜間ギャップ -19,200 / 手数料 -17,600） |

8銘柄中7銘柄が夜間に不利側へ動いた。**同日決済の戦略が持ち越すと、設計上
織り込んでいない夜間ギャップを丸ごと被る**（§18.32 の D 案を棄却した理由と同じ）。

⛔ 一般信用（デイトレ / MarginTradeType 3）を持ち越すと返済期日を過ぎるので
**自分では閉じられない**（Code 100326「注文期限指定が返済期日を超過」）。
翌朝の強制決済を待つしかない。

#### ★ 設計上の発見: 引成(MOC)は朝に出しても大引けで約定する

決済に**生きたプロセスは要らない**。板に MOC を置いておけば、PC が落ちても
watcher が死んでも大引けで決済される。この事実がこの節の全修正の土台。

⚠ ただし**建玉拘束(4001005)** があり、板に置ける返済注文は建玉ごとに1本だけ。
損切り逆指値と MOC は**両立しない**。だから「MOC を置く」と「損切りを板に置く」は
排他で、現行は watcher が 09:20 に損切りへ差し替える設計。

#### 入れた修正

| # | 何 | どこ |
|---|---|---|
| 1 | **終了前に MOC を板へ置く**（既定ON / `--no-moc-on-exit` で無効） | `k_open_confirm` |
| 2 | `panic_close.py` 新設 — 一般信用デイトレの売建を引成で必ず閉じる保険 | 新規 |
| 3 | watcher の再起動をあきらめたら `panic_close --execute` を呼ぶ | `morning_test` |
| 4 | 全終了経路で建玉を数える `_final_position_check` / lock 検知は `rc=3` | `lss_exit_watcher` |
| 5 | MOC 3回失敗 or 15:24 で即時成行へ切替（**発注済みには触らない**＝二重決済しない） | 同上 |
| 6 | 未対応建玉・取得失敗を呼び元へ返す（黙って「対象なし」と嘘をつかない） | 同上 |
| 7 | 発注記録を**発注より先**に書く（`pending` → 送信 → `ordered`/`failed`） | 発注系4本 |
| 8 | `.\fills` が日跨ぎ決済を往復として拾う（+0円と誤表示していた） | `verify_fills` |
| 9 | 持ち越しのあった日は `slip_daily_log.csv` に**記録しない** | 同上 |

**7の原則: 記録が無くて注文がある＝無防備（実損）／記録があって注文が無い＝無害。
だから記録が先。**

#### 修正後の保護タイムライン

```
09:00:36  発注(保護指値売り)              ⛔ 無防備(数秒〜数分) ← 唯一残る穴
09:10     k_open_confirm が MOC を設置     ✅ MOC が板に乗る
09:10     watcher 起動                     ✅ MOC
09:20     損切り武装(delay4)                ✅ 損切り監視に差し替え
15:20     MOC へ戻す                        ✅ MOC
15:30     大引けで約定                       ✅ 決済完了
```

残る穴（発注〜09:10）を埋めるには発注直後に1件ずつ MOC を出す必要があるが、
建玉拘束があるので損切り逆指値と両立しない。**まず現行で回して MOC が
受け付けられるかを確認する**（09:10 の MOC は未検証）。

#### ⚠ この日の数字を成績に混ぜないこと

`.\fills` は持ち越しのあった日を `slip_daily_log.csv` から自動で除外する。
**戦略の損益ではなく事故の費用**なので、§18.37 の実スリッページ測定に入れると壊れる。

---

### 18.47 ★ 明日の手順（2026-08-21 以降の定型）

```powershell
# 前夜 15:40 以降  ← 15:40 より前だと「昨日のシグナル」になる
.\dailyfast --no-serve

# 当日
07:50  .\jorder --budget 300      ← ⛔ 15:30まで閉じない
15:30以降
       .\fills
       .\dailyfast --no-serve     ← 突合用に流し直す
```

⛔ **15:40 の境界**: `_expected_latest_bar_date()` は 大引け15:30 + yfinance の
反映ラグ10分 = **15:40** を「今日の終値が確定した」境界にしている。
それより前に流すと前営業日の終値でシグナルを作る。

⚠ `.\jorder` は `--execute` がハードコード。**予算は明示すること**（既定60万）。
`--max-notional` は指定しなければ自動で予算と同じ値になる。

#### 確認する3点（これだけ）

| 見るところ | 正常なら |
|---|---|
| `.\jorder` 終了直前 | 「引け成行(MOC)を N件 設置しました」が出る |
| 同 結果サマリー | `⛔ 前日の始値 N銘柄` が **0件**（過半数なら kabu の書式変更を疑う） |
| watcher | `6b. 引け成行の保険` が **出ない**（出たら再起動をあきらめている） |
| `.\fills` 末尾 | 「持ち越し決済」の警告が **出ない** |

#### ⛔ やらないこと

- `.\watch` / 発注サーバを `.\jorder` と**同時に起動しない**（kabu の有効トークンは1つ）
- ⛔⛔ **watcher が死んだときに `.\watch` で復帰しない**。`watch.bat` は旧 lss 用で
  **`--stop-delay-bars 1`**。J は delay4 なので、損切りが 09:20 ではなく 09:05 に
  武装してライブとバックテストがズレる(§18.9)。復帰は **`.\jwatch`**（`.\jorder`
  が起動するのと1文字も同じコマンド）。
  「別の lss_exit_watcher が稼働中です(lock)」と出たら、lock は 180秒 で自動的に
  無効になるので **3分待って再実行**。手で消さないこと（旧プロセスが生きていた
  場合に二重決済になる）。
  再起動で拾えるもの: 建玉は kabu から読むので発注元でなくてよい /
  `.lss_watcher_seen.json` が検知時刻を復元するので **delay4 がゼロから
  やり直しにならない** / 板に乗っている MOC はそのまま残る
- `--moc-first`（watcher 側）は**まだ使わない**。09:00 台の MOC 受付が未検証で、
  かつ損切り逆指値を置かない設計。小ロットの日に確認してから
- `.\jorder` を場外で流さない（時間窓ガードが落とすが、watcher が 15:30 まで
  トークンを掴む＋`panic_close` が走る）。リハーサルは `.\jorder --dry-run` か
  `python k_open_confirm.py --prod --now --out k_paper_test.csv`（発注しない）

---

### 18.48 ★★ J の初運用と設定変更 (2026-08-21〜22)

**§18.38 の K仕様表・§18.40 の「正しい基準」に書いてある `+50bp` / `1銘柄上限50万` は
本節で置き換わった。以後はここが正。**

#### ① 2026-08-21 = J の初運用。機構はすべて期待どおり動いた

当日の設定は **+50bp / 1銘柄上限50万固定 / 予算300万**(変更前)。

| | 結果 |
|---|---|
| 09:00 の1周(50銘柄) | **6.1秒**(§18.44 の本番実測 36.5秒の1/6。08:47 のウォームアップが効いた) |
| 遅寄り | **2%**(モデル前提 15.7%) |
| 約定率 | **7/7 = 100%** |
| MOC-on-exit(§18.46 の対策) | **7/7 設置成功。初検証** |
| **実損益** | **+16,870円**(テスト +7,230円) |
| エントリー滑り | 6/7 が始値より**有利**。平均 **+22.7bp / +5,420円** |
| 決済滑り | 3件で計 **+250円**(±2円以内) |
| 引け決済4件 | テストと **1円も違わない** |

**寄りの板寄せ約定モデルは2日連続で実物と一致**(§18.37 の初日と同じ)。

#### ② ⛔ 資金均等が「100株固定に縮退」していた

当日は7件とも100株・注文額 約260万だった。原因は上限ではなく **分母**:

```
枠 = min(予算300万 ÷ 合格7件, 上限50万) = min(42.9万, 50万) = 42.9万
     ↑ 上限50万には届いていない。効いていたのは 予算÷件数 のほう
42.9万で2単元を建てられるのは 1単元21.4万以下 = 株価2,150円以下だけ
実際の平均建玉 260万÷7 ≒ 37万(株価約3,700円) → 全部1単元
```

稼働率 87% は §18.38 ① の想定どおりで、**資金均等の計算は正しく動いていた**。
出てきた答えが100株だっただけ。レポートの注記(`nikkei_analysis.py:17266`)がこれ:

> 2単元以上の割合が低く稼働率も低いなら、資金均等は **100株固定に縮退している**。

**★ 作法: 稼働率と「2単元以上の割合」を必ずセットで見る。** どちらも低ければ
資金均等は名前だけで、実態は100株固定。

#### ③ 1銘柄の金額上限: **50万固定 → 予算の50%** (`01c94b3`)

予算を変えると上限の意味が変わってしまうため比率化した。同時に**下側**(12.5〜25%)を
初めて掃いた。締めるとコストを払うだけで、資本効率は単調に悪化する。

| env | 意味 | 既定 |
|---|---|---|
| `LSS_EQ_MAX_PCT` | 予算に対する比率(%) | **50** |
| `LSS_EQ_MAX_YEN` | 万円で直接指定(>0 ならこちらが優先) | 0 |
| `k_open_confirm --max-yen-pct` / `--max-yen` | ライブ側の同じもの | 50 / 0 |

予算300万なら1銘柄150万、400万なら200万。**比率なので集中度(%)は予算に依らず一定**。

⚠ 上限を上げると、それまで「上限に収まらないので建てない」と落ちていた
**重複保有(同一銘柄の2枠目)が建つ**ので件数が増える(実測 7件 → 9件)。

#### ④ ★ ギャップ閾値: **+50bp → +75bp** (`5496e8e`)

**四方すべて ❌ = 真の頂点。** 予算400万・+75bp を基準にした監査ボード:

| 基準(`H寄り確認+75bpd4sm0.5資金均等`) | 1,366件 / 月平均 **+641,842** / σ 127,318 / **月平均÷σ 5.04** / 資本効率 19.84% |
|---|---|

| 動かす先 | 判定 |
|---|---|
| +50bp | ❌ σ+4% / 月平均÷σ 4.23 / **前半 −389,820・後半 −408,430** |
| +100bp / +125bp | ❌ 前半と後半で符号が逆(期間依存) |
| +150bp | ❌ σ **+63%** |

予算300万でも同じく頂点。**σ が下がる唯一の候補**でもあった。
戻すなら `set LSS_EQ_GAP_BP=50`(既定75 / `nikkei_analysis.py:444`)。

同じ日(08-21)を +75bp で測り直すと **合格2件・500株/600株・¥3,615,000**。
閾値を上げると合格が減り、**そのぶん1件あたりの枠が厚くなる**(300万÷2=150万)。
④と③はこの経路でつながっている。

#### ⑤ ⛔ レポートとライブで予算が食い違っていた (`e3a103c`)

`LSS_BUDGET_MAN` がどの `.bat` にも無く **既定400万**で走っていた。ライブは
`.\jorder --budget 300` で **300万**。§18.9 の鉄則違反。

- `daily.bat` / `dailyfast.bat` に `LSS_BUDGET_MAN=400` を**明示**した
- レポート冒頭の「この実行の条件」に**予算を橙色で表示**する
- 研究は400万据え置き(ユーザー判断)。**意図的な不一致であることをコメントに明記**

#### ⑥ ⚠ 予算を上げるとどうなるか(整理。**未検証**)

**資金均等では、予算は「銘柄数」ではなく「単元数」しか動かさない。**
09:00確認の経路は合格ペアを必ず1単元は建てる(`nikkei_analysis.py:12577`
`_lot = max(1, _lot) if _had <= 0`)ので、**件数はギャップ閾値だけで決まる**。

| | 予算を2倍にすると |
|---|---|
| 100株固定 | 建てられる**銘柄が増える** → 分散が効き σ は2倍未満 |
| **資金均等(現行)** | 銘柄数は不変、**単元数が2倍** → 損益もσも2倍 = **月平均÷σ は不変** |

つまり **実質はレバレッジ**。レバレッジでない利得は「切り捨てロスが減る=稼働率↑」だけで、
効くのは数%。→ **最適化ではなくリスク許容度の宣言**(§18.38 #3b と同じ性質)。

⛔ レポートの `_budget_sweep_html`(`nikkei_analysis.py:17424`)は `_run_budget_sim` =
**H(100株固定)の経路**なので、上表のとおり増え方が違い **J の答えにならない**。
§18.38 #4「予算」は **未実装のまま**。掃くなら `_size_equal_by_day` に予算を渡す
別の行が要る(5分足を触らないので計算は軽い)。

**§18.37 の実スリッページ 10営業日が終わるまでサイズを上げない**という方針は変えない。

#### ⑦ 直した不具合

| # | 内容 | コミット |
|---|---|---|
| 1 | `.\fills` が**持ち越し決済**を「テストに無い」と誤報。突合から除外し、建てた日を表示 | — |
| 2 | ギャップ判定が **前日の始値**を掴みうる(`OpeningPriceTime` の時刻だけ見ていた)。日付ガード + `stale_open` 列 | — |
| 3 | 気配ログの **429 が健全な50銘柄をブラックリスト入り**させ、同じトークンの09:00発注まで巻き添えにしていた | `cb0969d` |
| 4 | watcher のログが 252行/分で本当のイベントが埋もれる → `_once()` で 14行/分に。建玉拘束(4001005)を「損切り設置済み」と誤報していたのも訂正 | `006c333` |
| 5 | watcher の `[監視]` に**含み損益**と1周ごとの合計を表示 | `f1bfd87` |
| 6 | ⛔ **私(Claude)が入れた退行**: `_fmt_kn` が文字列つまみ(`xhm`)で落ち、**監査ボードと H の設定比較が2つとも消えていた** | `7f9286f` |
| 7 | ⛔ `a5f1300` が**まったく効いていなかった**(`_EQ_TAB_BASE2` を `_EQ_TAB_KEY2` の定義**より前**に置き、既定が空文字になっていた)。充填・上位N が推奨設定で一度も測られていなかった | `482bc0e` |

★ ⑥⑦の教訓: **つまみを足したら `_knobs()` / `_LBLN` / `_KN_KEYS` の3箇所**、
そして **定義順**(既定値が他の定数を参照するなら必ず後ろ)。どちらも
「黙って消える/黙って空になる」形の壊れ方をする。

#### ⑧ ⚠ 08-21 の滑り記録は、そのままでは取れない

実運用は **+50bp / 上限50万** で7件建てたが、いまレポートを回すと **+75bp で2件**に
なるので `lss_trades_K.csv` に当日の7ペアが入らず `.\fills --date 20260821` が突合できない。
遡って記録したいなら**一度だけ当日の設定で回す**:

```powershell
$env:LSS_EQ_GAP_BP = "50"; $env:LSS_EQ_MAX_YEN = "50"
.\dailyfast --no-serve
.\fills --date 20260821
$env:LSS_EQ_GAP_BP = $null; $env:LSS_EQ_MAX_YEN = $null
.\dailyfast --no-serve      # ⛔ 必ず戻す(lss_trades_K.csv を現行設定に戻すため)
```

**★ 一般化: 設定を変えた日は、変える前の営業日の突合を先に済ませること。**
レポートは常に「今の設定」で過去を測り直すので、過去の実約定と比較する窓は
設定変更で閉じる。

#### ⑧b walk-forward の判定 (2026-08-22) → **変えない**

| | 合計(10ヶ月) | 月平均 | 月次σ | **月平均÷σ** |
|---|---:|---:|---:|---:|
| **基準(現行 +75bp 資金均等)** | +6,418,420 | +641,842 | **127,318** | **5.04** |
| ▶ walk-forward 選択(実装できる設定だけ / 除外29本) | +6,836,241 | +683,624 | **190,946 (+50%)** | **3.58 (−29%)** |

合計は +6.5% 上回るが **σ が +50%**。§18.38 に記録したとおり
**walk-forward は合計で選ぶので σ を見ていない**ので、リスク調整では明確に負ける。
さらに **月ごとの選択が揺れている**(`10:H → 11:上位3 → 12:H`)。
§18.38 で「候補を増やしたら選択が安定しなくなった = ルール2 不合格」としたのと同じ形。

→ **設定は動かさない。2026-08-21〜22 の変更はギャップ +75bp と上限の比率化だけ。**

#### ⑧c ⚠ 次に見る行: `◆1銘柄1件` (重複保有を外す)

| | 件数 | 合計 | 円/件 | 月次σ | 月平均÷σ |
|---|---:|---:|---:|---:|---:|
| 基準 | 1,366 | +6,418,420 | +4,699 | 127,318 | 5.04 |
| **◆1銘柄1件** | 1,168 | +5,728,330 | **+4,904** | **100,342 (−21%)** | **5.71 (+13%)** |

§18.38 #3 で棄却した理由は「**σ が下がらない**(むしろ +3%)」= §18.30 の基準を
満たさなかったこと。**+75bp では σ が −21% 下がる**ので条件が変わった。
判定は次回の `.\hvar` の**監査ボード**の該当行(基準との対応検定・前半/後半の符号)で行う。

⛔ **`⚖ 比較ブロック` の t を基準との差と読まないこと**(2026-08-22 に読み違えかけた)。
あのブロックは資金均等の全変種を **`H`(プレーンな100株固定)** とペアにする
(`nikkei_analysis.py:16929` `_PAIRS.append((_k, "H"))`)。実際 3行とも比較相手が
同じ 月平均 25,870円 に逆算できる:

```
1銘柄1件      572,833 − 546,963 = 25,870
金額均等31万   167,496 − 141,626 = 25,870
ATR均等       171,979 − 146,109 = 25,870   ← 一致 = 相手は H
```

**★ 作法: 差の列を見たら、必ず「その変種の月平均 − 差」で比較相手を逆算する。**
同じ値が複数行で出れば相手は共通、基準と違えばその t は基準との比較ではない。

#### ⑧d ⛔ `.\fills` の突合相手が **実発注より広い母集団**だった (2026-08-22 修正)

`lss_trades_K.csv`(= `.\fills` と `measure_entry_decay.py` が読むファイル)には
**基準(`_EQ_TAB_KEY2` = 選定なし × watch50 = L 中間版)** を書いていた。
実発注は **J 実装版(選定あり × watch50)** なので母集団が違う。

| | 母集団 | 件数/月(実測) |
|---|---|---:|
| 実発注 = **J 実装版** | 選定あり(cumul) × watch50 | 約105件 |
| _K.csv の中身(旧) = **L 中間版** | **選定なし** × watch50 | 約137件 (**+26%**) |

**資金均等は `予算 ÷ その日の合格件数` なので、件数が違えば株数が違う。**
つまり `.\fills` の「テスト損益」は**実発注では有り得ない株数**で計算されていた。
滑り(bp)は価格どうしの比較なので無事だが、**円の比較と fill率が狂う**。

→ `_kkey = _EQ_TAB_J`(空なら基準にフォールバックして**警告を出す**)に修正。
   ファイル名 `_K` は下流が参照しているので変えない。
   **どの変種を書いたかを毎回 print する**(名前と中身の食い違いは §18.40b で半日溶かした)。

★ 作法: **「実発注と同じ母集団か」を、突合するすべての経路で確認する。**
  §18.42 に「_K.csv の中身は基準(=L)」と**書いてあったのに**、
  実運用が H → J に移った時点でそこが穴になっていることに気づいていなかった。
  **記録があっても、前提が変わったら読み直すこと。**

#### ⑧e 参考: J/K タブに混ざっていないもの

聞かれたので確認した(2026-08-22)。**J/K タブと `_K.csv` は J だけ**で、以下は入らない:

| | 状態 | 根拠 |
|---|---|---|
| **転換** | ✅ 除外済み | `eh_trades.py:280` が母集団から落とす(§18.26b で棄却済みの手法) |
| ロング | ✅ 入らない | E/H/J はすべてショート |
| 再エントリー | ✅ 入らない | 同日決済 + 1ペア1日1件 |
| 現行lss(逆指値) | ✅ 入らない | 別の変種キー |
| **同一銘柄の重複保有** | ⚠ **入る** | 別戦略が同じ銘柄で発火したぶん。外す版が `◆1銘柄1件`(⑧c) |

⚠ ただし**レポート上部のKPIと『全取引』タブは別**で、そちらには転換が混ざる(§18.13)。
  混同しないこと。

#### ⑧f ⛔⛔ **全つまみの判定は L の上で行われていた** (2026-08-22 発覚・修正)

**監査ボードの基準は `_EQ_TAB_KEY2` = L 中間版(選定なし × watch50)固定だった。**
実際に発注する J(選定あり × watch50)でも、到達目標の K(選定なし × watch無制限)でもない。

> ⚠⚠ **訂正 (2026-08-22 実測)**: 機構としては本当だが、**日々の実行では実害ゼロ**だった。
> `.\daily` / `.\hvar` は土台が `lss_proposal_cumul.py`(選定あり)なので、選定フィルタが
> 1件も落とさず **L = J**。J 基準で出し直したボードは前日の L 基準のボードと **1円まで同一**
> (基準 1,366件 / +641,842 / σ127,318 / 5.04 / 19.84%、個別の行も一致)。
> つまり `_EQ_TAB_KEY2` という名前の基準は、**最初から live の形そのものだった**。
> ⛔ ただし **土台を広げた実行では本当に別物になる**(§18.38 #8 = 土台 full / 8,106ペア。
>   あのとき sm の単峰が消えたのは母集団が違ったため)。土台を変えたら必ず掃き直すこと。

| | 母集団 | 何だったか |
|---|---|---|
| **J 実装版** | 選定あり(cumul) × watch50 | **実際に発注している形** |
| **L 中間版** | **選定なし** × watch50 | ⛔ **監査ボードの基準はこれだった** |
| K 理想版 | 選定なし × watch無制限 | 実装不可(kabu 登録上限50件 / §18.45) |

つまみの兄弟行も **`pool=None`(=L)にしか作っていなかった**
(`_eq_modes` が全部 pool=None / 実装版・理想版は推奨設定に1本ずつだけ)。

##### なぜ問題か — 資金均等は母集団に依存する

```
枠 = 予算 ÷ その日の合格件数
```
L は J より件数が多い(実測 **137件/月 vs 約105件/月 = +30%**)ので **枠が薄い**。
したがって **件数に依存するつまみは L と J で結論が変わりうる**:

| つまみ | 母集団依存 | 理由 |
|---|---|---|
| **1銘柄の金額上限** | ★★ **大** | J は枠が厚い = **上限が効く日が増える**。L は上限が効きにくい世界 |
| **ギャップ閾値** | ★★ 大 | 閾値を上げたときの枠の伸びが J のほうが大きい |
| **充填 / 上位N** | ★★ 大 | どちらも件数の関数 |
| **watch件数** | ★★ 大 | 候補数そのものが変わる |
| delay / sm / tm / ATR | ☆ 小 | 1トレードの決済ルール。母集団に依らない(§18.42: J と K は勝率72%が一致・円/件も +2,753 vs +2,662) |

##### 修正 (`7ecfaa6`)

- `_eq_modes` を **J / K の母集団にも複製**(`LSS_EQ_POOL_ALL`、既定ON)。
  K は watch 無制限が定義なので watch を動かした行は複製しない(名前が衝突する)
- 監査ボード本体を `_board_body(_bk)` に切り出し、**J と K で1枚ずつ**出す。
  **L は既定で出さない**(`set LSS_EQ_BOARD_KEYS=J,K,L` で復活)
- 各ボードの先頭に母集団を明記し、混ぜないよう警告を出す
- ⚖ 比較ブロックで、実装版/理想版の行を **自分の母集団の基準**ともペアにする

##### ⛔ 掃き直しが要る設定

> ⚠ **上の訂正のとおり、土台=cumul の実行では L = J なので掃き直しは済んでいる**
>   (2026-08-22 に J 基準で出し直して同一を確認)。以下は **土台を広げた実行**
>   (`--lss-proposal lss_proposal_full.py`)に切り替えるときに効く話。

**`+75bp` と `上限=予算の50%` は件数に依存するので、土台を変えたら測り直すこと。**
delay4 / sm0.5 / tm1.0 / ATR14 は母集団に依らないので据え置きでよい
(§18.38 の「母集団(土台)を先に決めてから、つまみを掃く」と同じ形。
 あのときは選定あり/なしで sm の単峰が消えた)。

##### ⚠ 判定ブロックは「既定で開くタブ」にしか無い

⚖ 注文方式の比較 と ☑ 設定監査ボードは **HTML を軽くするため既定タブにだけ**積む
(`nikkei_analysis.py:21143`)。その既定タブが **K 優先**だったので、
**実際に発注している J のタブには判定が1つも無かった**(2026-08-22「これのどこが判定？」)。
→ `_DEF_TAB` の優先順を **J 先頭**に変更(`ehJ → ehK → ehL → ehH → budget`)。

J タブに元からあるのは *実績* だけ(母集団の月別診断 / エントリー方式の説明 /
全件⇔重複保有なし / 月別サマリー / 日別カード / 明細)。**判定はボードの側**。

##### ⛔ 土台と選定が同じファイルだと J = L になる

2026-08-22 の実行は 土台も選定も `lss_proposal_cumul.py` で、**選定フィルタが
1件も落とさず J 実装版 = L 中間版 = 1,366件**、K も「選定なし」ではなく
「同じ選定 × watch無制限」(1,624件)だった。タブ名だけ見ると別物に見える。

3つを別物として比べるなら **土台を選定なしにする**:
```
.\hvar --lss-proposal lss_proposal_full.py
```
選定あり ⊂ 選定なし なので、広いほうで回して狭いほうを切り出す(§18.42)。
⚠ 日々の `.\daily` は 土台=cumul でよい(発注リストと同じ母集団)。
条件スタンプに ⛔ 警告を出すようにした。

★ 作法: **判定に使うボードの母集団が『実際に発注している形』かを最初に確認する。**
  §18.42 に J/L/K の3つを定義しておきながら、判定はずっと L で行っていた。
  **定義しただけでは揃わない。どれを見ているかを画面に出すこと**(いま出るようにした)。

#### ⑧g ★★★ 母集団は『もう1本の帯』として使える (2026-08-22 確定)

J と K の2枚を突き合わせたら、**✅ が母集団で入れ替わる**ことが分かった。

| つまみ | J 実装版 | K 理想版 | |
|---|---|---|---|
| 建値下限 **1,500円** | ❌ 期間依存 | **✅ リスク低減**(σ−22% / 8.89) | **逆** |
| 建値下限 **2,000円** | **✅ リスク低減**(σ−34% / 6.98) | ❌(σ+5%) | **逆** |
| 利確ATR 1→**0.5** | ❌ 期間依存 | **✅ リスク低減** | **逆** |
| **1銘柄1件** | ❌(σ+5%) | **✅**(前半後半とも+) | **逆** |
| 決済締切 15:20 / 15:25 | **✅** | ❌ 期間依存 | **逆** |
| 1銘柄上限 200万→**300万** | **✅** | ❌(σ+5%) | **逆** |
| 損切ATR 0.5→0.1 | ✅ リスク低減 | — 測れていない | 不一致 |
| **ATR期間 14→20** | ✅ | ✅ | **一致**(ただし +11,503 = 0.09σ) |
| **損切り遅延 4→6** | ✅ | ✅ | **一致**(ただし +5,589 = 0.06σ) |

一方 **❌ は驚くほどよく一致する**: ギャップ閾値6方向 / 判定のタイミング全10行 /
上位N(3,5,8) / 充填 / 建値の上限(2,000〜5,000円) / 損切ATRを広げる(0.7,1.0,なし) /
利確ATRを広げる(1.5,2,3) — **すべて両板で ❌**。

##### ★ 作法

> **両方の母集団で ❌ = 本物の不採用。片方だけ ✅ = 母集団依存 = ノイズ。**

§18.24 の「帯を作ってから判定する」の応用。ランダム順の帯を作るのは重いが、
**母集団を変えるのは資金均等の後処理を1本増やすだけ**なので実質タダで
第2の帯が手に入る。**つまみの判定は必ず J と K の両方で見ること。**

現行設定を支持する結論(+75bp / 09:00の一発 / 上位N不採用 / 充填不採用 /
sm0.5 / tm1.0 / 建値上限なし)は **母集団を変えても1つも崩れなかった**。
逆に変更候補は **ほぼ全部が母集団を変えると消えた**。

⛔ §18.48 ⑧c で「次に見る」とした `◆1銘柄1件` は **J で ❌**(σ+5%)。
   K では ✅ だが逆向きなので **棄却**。⑧c の候補は消えた。
⛔ 2026-08-22 の J板で「建値下限が次に見る価値あり」としたのも **撤回**
   (1,500円と2,000円で J/K が正反対)。

##### 参考: K は J より σ が26%小さい(ただし実装不可)

| | 件数 | 月平均 | σ | 月平均÷σ | 集中 95%点 |
|---|---:|---:|---:|---:|---:|
| J(watch50) | 1,366 | +641,842 | 127,318 | 5.04 | 33% |
| K(watch無制限) | 1,624 | +671,444 | **93,813 (−26%)** | **7.16** | **28%** |

差は総額ではなく **σ**(+29,602/月 は 0.23σ でノイズ / σ は −26%)。1日に建てる
銘柄が増えて分散が効く。**読める数を増やす価値の根拠が「総額」から「σ」に
変わった**が、§18.45 の実装可否(kabu 登録上限50件 / 299銘柄で3.6分 / 減衰−30bp)は
変わらないので **追わない**。

#### ⑩ ★ 2026-08-24 = +75bp / 上限50% の初日。**+16,690円**

`.\jorder --budget 300 --skip-preopen` / 合格5件 / 約定 5/5 (100%)。

| 銘柄 | 株数 | 実売り | 実買戻 | 決済 | 損益 |
|---|---:|---:|---:|---|---:|
| 7956 ピジョン | 200 | 2,190.2 | 2,142.0 | 15:30 | +9,630 |
| 4587 ペプチドリーム | 400 | 1,120.0 | 1,099.5 | 15:30 | +8,200 |
| 4443 Sansan | 200 | 2,268.9 | 2,248.0 | 15:30 | +4,180 |
| 3382 セブン＆アイ | 200 | 2,045.3 | 2,045.0 | 15:30 | +60 |
| 9119 飯野海運 | 200 | 1,728.2 | 1,755.1 | **11:04** | **−5,380** |
| | | | | | **+16,690** |

**全部 200〜400株 = 資金均等が初めて機能した**(先週は7件とも100株固定で、
枠42.9万に対し1単元が30〜37万だったため。今日は合格5件で枠60万)。
投入174.9万に対し +0.95%。先週(+16,870円 / 7件)とほぼ同額を、件数を減らして出した。

##### ★★ エントリーの遅延コストが実測できた = **−30.9bp ≒ 約3分の遅れ**

| 銘柄 | 実約定 | テスト始値 | bp |
|---|---:|---:|---:|
| 4587 | 1,120.0 | 1,126.0 | **−53.3** |
| 4443 | 2,268.9 | 2,281.0 | **−53.0** |
| 3382 | 2,045.3 | 2,049.5 | −20.5 |
| 7956 | 2,190.2 | 2,194.0 | −17.3 |
| 9119 | 1,728.2 | 1,730.0 | −10.4 |
| **平均** | | | **−30.9** |

**決済は5件とも一致**(誤差1円以内)。ズレはエントリーだけ。

原因は §18.44 で予測していた**執行遅延**。ライブは 09:00 の板寄せに間に合って
いない(50銘柄の板を読むのに33秒 + 発注の往復)ので、**約定はザラ場**。
レポートは『約定=始値(執行が瞬時)』が前提。§18.44 の1分足実測
(1分 −15.8 / 2分 −26.8 / **3分 −29.4** / 5分 −36.6 bp)と照らすと **3分相当**。

⚠ **レポートの J の数字はこのぶん過大**。§18.44 の見積り(月 −5万〜−10万)と整合。

##### 板の逆指値が置けない代償は +12bp だけ

9119 が唯一の損切り: 損切りライン 1,753 → 実買戻し 1,755.1 = **+2.1円(+12.1bp)**。
MOC が建玉を拘束して板の逆指値を置けず(§18.46 の設計上の排他)、5秒ポーリングの
成行で決済した結果。**この精度なら板の逆指値が無くても実害はほぼ無い。**

##### 直した不具合5件 (`c614779`)

| # | 内容 |
|---|---|
| 1 | **watcher の終了時建玉チェックが誤報**。15:30 の板寄せで約定していても建玉への反映が数十秒遅れ、「5件残ったまま終了」と出た(実際は全件決済済み)。MOC 発注済みの銘柄だけ 10秒×2回 待って読み直す。**誤報は本物の持ち越し(08-19)を見逃す** |
| 2 | ⛔⛔ **`kabu_api.register` が例外を握り潰していた**。print だけで返り値も例外も無く、`log_preopen_board` の「429 が3回続いたら中止」ガードが**一度も発火しなかった**。44連続で叩いてレート制限を悪化させ、09:00 の発注まで巻き添えにした → bool を返す |
| 3 | **MOC が建玉ごとに発注されていた**。kabu の信用返済は発注時に既存の返済注文を取消すので、建玉が分かれた銘柄(7956=100株×2)は2本目が1本目を消して半分が無防備 → **銘柄単位で合算**(watcher は既にそうしていた) |
| 4 | **気配ログが窓に収まらず毎朝おなじ先頭だけ**を読んでいた(1,540銘柄=31バッチ / 実測 9/31 で締切)→ 実測の1バッチ時間から到達可能な本数に切り詰める |
| 5 | **`.\fills` の【指値のズレ】が J で必ず誤警告**。H は指値どうしを比べられるが、J のライブ指値は『始値×0.995 の保護指値』でテストは『始値そのもの』→ J では【エントリーのズレ】を bp で出し、§18.44 の減衰表と照合する |

★ おまけ: 同一銘柄の建玉が2本あると `[auction] OCO再計算` の抑制が効かず数百行
流れた(平均約定が 0.1円違い、銘柄だけを鍵にしていたので値が毎周入れ替わる)。
鍵に HoldID を含めた。

##### ⛔ その朝に踏んだ運用上の罠

**気配ログの 429 ストームで 09:00 の発注が危うくなった**(08-21 と同じ)。
08:14 に 9/31バッチ・1バッチ150秒で、08:45 の締切に間に合わないうえ 429 が
積み上がっていた。→ **Ctrl+C して `.\jorder --budget 300 --skip-preopen` で再起動**。
20分の静止でレート制限が回復し、08:47 の空読みは 50/50・エラー0・32.8秒 で完走。

★ 判断の根拠: 気配ログは研究データで**その日の発注には不要**(§18.35b: 用途Aは
棄却済み / 用途Bは15〜20営業日必要)。**発注を守るほうが常に優先。**

#### ⑨ 手順は変わらない (§18.47 のまま)

```powershell
.\dailyfast --no-serve          # 前夜 15:40 以降
07:50  .\jorder --budget 300
15:30以降  .\fills / .\dailyfast --no-serve
```

起動ヘッダで **「合格 = 始値が前日終値 +75bp 以上」** と **1銘柄上限(300万なら150万)** の
2つを確認する。復帰は `.\jwatch`(⛔ `.\watch` は旧 lss 用で delay1。J は delay4)。
