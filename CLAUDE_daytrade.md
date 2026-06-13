# デイトレ 5分足 戦略運用メモ

このドキュメントは Claude Code がデイトレ用コードを扱う際の前提知識です。
逆指値ロング (CLAUDE.md / `gobot/`) の5分足デイトレ移植版で、
**6戦略 × Walk-forward × ホールドアウト検証** を1コマンドで実行できます。

---

## 1. エントリーポイント

| コマンド | 役割 |
|---|---|
| `python scan_walkforward_daytrade.py` | prime ~1500銘柄 × 12戦略 × 3-fold WFスキャン |
| `python build_watchlist_daytrade.py` | WF CSV → WATCHLIST 生成 (戦略別 or 統合) |
| `python check_signals_daytrade.py` (モジュール) | 1銘柄 × 1戦略 のbacktest + スコア |
| `python run_signals_holdout_all_daytrade.py` | タブ付き統合HTML レポート |
| `python kabu_donchian_bot.py --dry-run --margin ...` | 実運用 bot (Donchian) |

---

## 2. 戦略一覧 (12種)

### Long (買い)
| 戦略 | em | sm | tm | 条件 (5分足) |
|---|---|---|---|---|
| **DON** | 0.0 | 1.5 | 3.0 | 過去8本(40分)高値ブレイク + 陽線 |
| **MACD** | 0.0 | 1.5 | 3.0 | MACD(8,17,5)クロス + 出来高1.2× + MA10上 |
| **RSI2** | 0.0 | 2.0 | 4.0 | RSI(2)<10 + MA20上 + IBS<0.35 |
| **A7** | 0.0 | 1.5 | 3.0 | Stoch(14,3,3) 反発 + MA75上 |
| **VOL** | 0.0 | 1.5 | 3.0 | 5本高値 + 出来高×1.5 |
| **MOM** | 0.0 | 1.5 | 3.0 | ROC10>3% + MA25>MA75 |

### Short (空売り、デイトレ信用)
| 戦略 | em | sm | tm | 条件 |
|---|---|---|---|---|
| DON_S/MACD_S/RSI2_S/A7_S/VOL_S/MOM_S | 各 long の対称版 | | | |

すべて `em=0.0` (翌バー寄付で約定) 設計。
`order_p = next_open + ATR×em` (long) または `next_open - ATR×em` (short)。

---

## 3. ファイル構成

```
daytrade_strategies_5m.py        ← 6 long 戦略のシグナル関数
daytrade_strategies_5m_short.py  ← 6 short 戦略のシグナル関数
daytrade_engine_5m.py            ← 共通バックテストエンジン (long/short)
risk_metrics_5m.py               ← Sharpe/MaxDD/連敗/RF/Calmar 計算
scan_walkforward_daytrade.py     ← 多戦略 × 多銘柄 WFスキャナ
build_watchlist_daytrade.py      ← WF CSV → WATCHLIST 生成
check_signals_daytrade.py        ← 銘柄×戦略 個別チェック
run_signals_holdout_all_daytrade.py ← タブ付き統合HTML

# 既存
daytrade_donchian.py             ← 旧Donchian (現運用版、本ポートに統合予定)
kabu_donchian_bot.py             ← 実運用bot
daytrade_data.py                 ← データロード (共通)
```

---

## 4. 共通設定

```python
# daytrade_strategies_5m.py
ENTRY_START   = dtime(9, 30)     # エントリー開始時刻
ENTRY_CUTOFF  = dtime(14, 30)    # エントリー期限
FORCE_CLOSE   = dtime(14, 55)    # 強制決済
WARMUP_BARS   = 20               # ウォームアップ (100分)

# daytrade_engine_5m.py
SLIPPAGE_STOP_PCT = 0.003        # 逆指値約定スリッページ ±0.3%
FEE_PCT_ONE_WAY   = 0.001        # 片道手数料 0.1% (信用デイトレは実質0)
```

---

## 5. Walk-Forward 設計

```python
# scan_walkforward_daytrade.py
FOLDS = [
    ("Fold1", 540, 360, 360, 180),   # TRAIN 540-360日前 / TEST 360-180日前
    ("Fold2", 360, 180, 180, 90),    # TRAIN 360-180日前 / TEST 180-90日前
    ("Fold3", 180, 90,  90,  0),     # TRAIN 180-90日前  / TEST 90-0日前
]

PASS_TRAIN = dict(trades=10, pf=1.3, win_rate=50, pnl=0)
PASS_TEST  = dict(trades=5,  pf=1.1, win_rate=45, pnl=0)
MIN_FOLDS  = 2                    # 3 fold中 2 fold以上で合格
```

→ **3 fold中 2 fold以上で TRAIN+TEST 通過** したら採用。
→ in-sample bias を排除した「真の優位性」のみ抽出。

---

## 6. スコア (おすすめ判定)

`risk_metrics_5m.calc_recommend_score`:
```
score = avg_wr*0.4 + (avg_pf/10)*30 + stable*20 + min(trades/20, 1)*10
最大100点。 ★★★≥80, ★★≥60, ★≥40, △<40
```

期間別 30/60/90/120/150/180日 の各統計を平均してスコア化。
trades==0 の期間は除外。PF=∞ は 10 として扱う。

---

## 7. 実運用フロー (推奨)

### 月次更新 (1ヶ月に1回)
```bash
# 1. データ更新 (約30分)
python yfinance_update.py --days 60 --workers 5

# 2. 全戦略WFスキャン (約1-2時間)
python scan_walkforward_daytrade.py --workers 4

# 3. WATCHLIST 生成
python build_watchlist_daytrade.py --combined --top 30 --max-price 6000 --min-price 1000

# 4. 動作確認
python run_signals_holdout_all_daytrade.py
```

### 日次運用 (毎朝)
```bash
# 1. 差分データ更新 (1-2分)
python yfinance_update.py --days 60 --daytrade-only --workers 5

# 2. シグナル確認
python run_signals_holdout_all_daytrade.py

# 3. bot 起動 (kabu STATION 接続後)
python kabu_donchian_bot.py --dry-run --margin --max-concurrent 1 \
    --max-risk 1000 --budget 300000 --poll 30
```

---

## 8. 実運用コストモデル

逆指値ロングとデイトレで違いがある:

| 項目 | 逆指値ロング (日足) | デイトレ (5分足) |
|---|---|---|
| スリッページ | 0.5% | **0.3%** (日中は流動性高) |
| 手数料 | 0.1%/片道 (現物) | **0% (デイトレ信用)** or 0.1% |
| 信用金利 | 1日 | **0** (日跨ぎなし) |
| 引け強制 | なし | **14:55 必須** |
| 持ち越し | 最大15日 | **当日完結** |

→ デイトレは「金利・持ち越しコスト 0」だが、引け強制で目標未達リスクあり。

---

## 9. 合格しやすい戦略の傾向 (推測)

逆指値ロング側の実績から、5分足でも有効と推定される戦略:

| 戦略 | デイトレ適性 | 理由 |
|---|---|---|
| **RSI2** | ★★★ | 押し目買い、相場局面問わず |
| **MACD** | ★★ | 出来高条件で偽シグナル減 |
| **DON** | ★★ | 既存ベースライン |
| **A7** | ★ | MA75 (375分=6時間) は1日内なので有効 |
| **VOL** | ★ | 出来高サージは5分足でも検出可 |
| **MOM** | △ | ROC10 (50分) は短期的 |

実際には WF スキャンで判定すべき。

---

## 10. 今後の改修TODO

1. **GAP_S 戦略** の前日終値ハンドリング (現状エンジン未対応)
2. **複数戦略並列実行** (bot 側で複数戦略のシグナルを統合)
3. **市況フィルタ統合** (nikkei_analysis + scan_walkforward の連携)
4. **リアルタイム JSON エクスポート** (signals_daytrade_latest.json)
5. **kabu_donchian_bot** を 多戦略対応の `kabu_daytrade_bot` に拡張
6. **forward_test** (ペーパートレード継続評価)
7. **LINE alert** (シグナル発生時に通知)

---

## 11. 過去のハマりどころ

- **データ鮮度**: yfinance は 60日上限。それ以前は J-Quants 必要 (有料)
- **列名混在**: J-Quants は `O/H/L/C/Vo`、yfinance は `Open/High/...`
  → `yf_df_to_jquants` で J-Quants 形式に統一保存
- **NaT 混入**: 古い pkl で日時 NaT があると resample 失敗
  → `_load_local` で try/except で吸収済み
- **OneDrive 同期**: data フォルダが OneDrive 配下だと書込ブロック発生
  → ジャンクションで gobot/data を共有する運用に
- **bot ログ format error**: Python 3.14 で `%+,.0f` がエラー → f-string で回避

---

## 12. 削除済み・しないこと

- 全期間backtest だけでの winners 抽出 (in-sample bias → カーブフィット)
- スリッページ・手数料を含まないPF評価 (実運用と乖離大)
- `daytrade_donchian_winners.py` を手動編集する運用 (build_watchlist_daytrade で自動生成すべき)
