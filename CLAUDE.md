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

## 5. 共通定数 (backtest_limit_entry.py:47-61)

```
ENTRY_EXPIRE  = 3         # 指値/逆指値の有効日数 (シグナル発生から3営業日)
MAX_HOLD      = 15        # 最大保有日数 (超えたら "タイムカット" で終値決済)
FIXED_QTY     = 100       # 常に100株固定 (取引数量)
INITIAL_CASH  = 500_000
POSITION_SIZE = 100_000
BACKTEST_DAYS = 365
WORKERS       = 4         # run_signals.py の --workers デフォルト
MIN_PRICE     = 100.0
MAX_PRICE     = 100_000.0
MAX_ATR_RATIO = 0.20      # ATR/終値 > 20% の異常ボラ銘柄は除外
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
