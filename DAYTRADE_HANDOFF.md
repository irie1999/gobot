# デイトレ 引き継ぎメモ

新セッションで参照する目的の要約。詳細は各ソースを直接読むこと。

## 1. データソース (重要)

`daytrade_data.py` の `load_intraday_batch(symbols, days, source=...)`
を介して全データを取得する。

| source | 動作 |
|--------|------|
| `local` | `data/minute_5m/*.pkl` のみ (J-Quants 由来の長期データ) |
| `yfinance` | yfinance API のみ。**5分足は最大60日まで** |
| `auto` | local → yfinance の順 |
| `hybrid` | **推奨**: 60日より前 = pkl (J-Quants), 直近60日 = yfinance snapshot |

### hybrid の仕組み (`_load_hybrid_batch`)
- `hybrid_cutoff_days=60` を境に pkl と yfinance を切替
- 60日以内は corporate action や `yfinance_update.py` の干渉で混在し得るため
  yfinance の生値 (auto_adjust=False) を優先
- snapshot は `data/yfinance_snapshots/<YYYY-MM-DD>/` に保存され、
  同じ snapshot_date を指定すれば結果が再現される

### 主要スクリプトのデフォルト
- `simple_report_all_shifts.py` : `source=hybrid`、snapshot 自動検出
- `scan_walkforward_daytrade.py` : `source=local` (長期データ必要)

## 2. パイプライン全体像

```
[1回だけ]  scan_walkforward_daytrade.py --all-periods --folds 2 \
              --swing-thresholds --max-price 6000 --min-price 1000
              → walkforward_daytrade_results/*.csv

[1回だけ]  gen_watchlist_from_walkforward.py --all-periods
              → daytrade_winners_<日付>_shift{30,60,90,120,150,180}d.py

[毎日]     simple_report_all_shifts.py
              → simple_report_all_shifts_<日付>.html
              (run_oos_report.py がラップ)
```

watchlist (`daytrade_winners_*_shift*d.py`) は**固定**。
銘柄選定はやり直さない。

## 3. 主要ファイル

- `daytrade_data.py` ― 5分足ローダ (local/yfinance/hybrid)
- `daytrade_engine_5m.py` ― 5分足バックテストエンジン
  - `ENTRY_CUTOFF = 14:30`, `FORCE_CLOSE = 14:55`
  - `backtest_symbol_5m()`, `calc_stats()`
- `daytrade_strategies_5m.py` ― ロング戦略 (DON, MACD, RSI2, A7, VOL, MOM)
- `daytrade_strategies_5m_short.py` ― ショート戦略 (DON_S, MACD_S,
  RSI2_S, A7_S, VOL_S, MOM_S, GAP_S)
- `simple_report_all_shifts.py` ― **メイン OOS レポート**
- `nikkei_filter_daytrade.py` ― 日経前日比による地合いフィルタ
  - `load_history()` は前日終値の変化率を返す (look-ahead bias なし)
- `signal_risk_check_daytrade.py` ― リスク警告 (流動性/ボラ/高値圏)
- `holdout_periods_report_daytrade.py` ― 期間別レポート (参考実装)
- `run_signals_holdout_all_daytrade.py` ― シグナル + 損益レポート
- `daily_fetch_minute.py` ― 5分足データ日次取得

## 4. 評価方法 (simple_report_all_shifts.py)

### 6 shift OOS
- shift=30/60/90/120/150/180 日 で銘柄選定 → 各 shift の除外期間を OOS とする
- 同一 (sym, strategy) ペアは shift をまたいで 1スコアに集約
- 全体タブ = 採用ペア × 全期間 (~18ヶ月)

### pair_score (0-40 が実用域)
`score = 100 × robustness × pf_factor × wr_factor × sample_factor`
- robustness: 選ばれた shift 数 / 6
- pf_factor: 平均 OOS PF (0.8-2.3 → 0-1)
- wr_factor: 平均 OOS 勝率 (0-60% → 0-1)
- sample_factor: OOS 取引数 / 30

### Q score (look-ahead-free シグナル品質)
`Q = pair_score + time_adj + count_adj + loss_adj + pnl_adj` を [0,100]
にクリップ
- time_adj: 寄付/引け前 -5, 通常 +5
- count_adj: 同日同銘柄 1回目 +10, 2回目 0, 3回目 -10, 4+ -15
- loss_adj: 同銘柄当日損切歴あり -10
- pnl_adj: 当日累積 PnL +10K 以上 +5, -10K 以下 -5

### 採用フィルタ (現在のデフォルト)
- `PAIR_FILTER_MIN_ROBUSTNESS = 2/6`
- `PAIR_FILTER_MIN_PF = 1.0`
- `PAIR_FILTER_MIN_PNL = 0`

### SAME_DAY_LOCK
同日同銘柄に複数戦略が発火 → 最も早い entry_dt の戦略のみ残す。
long/short は独立に適用。

### 地合いフィルタ (`MARKET_FILTER_THR = 1.5`)
- ショート: 前日日経 ≥ +1.5% の日を除外
- ロング: 前日日経 ≤ -1.5% の日を除外

### 100株・300万/日 シミュレーション
Q>=45 / Q>=35 で 100株固定、1日 300万円を上限に Q降順で採用。
損益 = `100 × 買値 × 損益率`

## 5. 現状の評価結果 (2026-06-16, 全体タブ・ショート 18ヶ月分)

```
全 1,063 取引 / 勝率 42% / PF 1.25 / +247,038円 / 最大DD -24.9%
Q>=45 推奨    130 / 勝率 44% / PF 1.33 / +18,011円  (18ヶ月)
100株シム Q>=45 / PF 1.27 / +14,598円              (18ヶ月)
地合いフィルタ後 886 / PF 1.32 / +250,017円
```

**結論: 資金効率が悪く、スリッページ・手数料込みでは厳しい。**
根本的な見直しが必要 (戦略・タイムフレーム・銘柄選定方法 etc)。

## 6. 既知の弱点 / 検討課題

- PF 1.25 は薄い (現実のスリッページで崩れやすい)
- 5分足デイトレは手数料・スプレッドの相対影響大
- Q>=45 フィルタでも 18ヶ月で +14,598円 → 1ヶ月 ~800円
- 地合いフィルタは利益を僅かに改善するが取引数を削るため微妙
- スイングの方が資金効率が良いという比較が出ている

## 7. ブランチ運用

現セッションのブランチ: `claude/add-day-trading-reference-pXCUQ`
- 最新コミット: `c9292e7` (デフォルトフィルタを 2/6, PF1.0 に変更)
- 主な追加: 地合いフィルタ行、100株シミュレーション、`run_oos_report.py`

## 8. 毎日の実行コマンド

```bash
# レポート再生成 (watchlist は固定なので毎回これだけ)
python simple_report_all_shifts.py

# ラッパー (同じ)
python run_oos_report.py
python run_oos_report.py --open      # 終了後ブラウザで開く
python run_oos_report.py --refresh   # バックテストキャッシュをクリア
```
