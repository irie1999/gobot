#!/bin/bash
# 日次レポート実行スクリプト
# 15:30 JST 以降は --force を自動付与（引け後の最新データで再計算）
# それ以前は --force なし（当日キャッシュがあれば即返し）

HOUR=$(TZ=Asia/Tokyo date +%H)
MIN=$(TZ=Asia/Tokyo date +%M)
CURRENT=$(( HOUR * 60 + MIN ))
THRESHOLD=$(( 15 * 60 + 30 ))  # 15:30

if [ "$CURRENT" -ge "$THRESHOLD" ]; then
    FORCE="--force"
    echo "[run_daily] 15:30以降のため --force を付与して実行"
else
    FORCE=""
    echo "[run_daily] 15:30前のため --force なしで実行（キャッシュ優先）"
fi

python run_signals_holdout_all.py --both --min-price 1000 --price-ranges 6000,10000 $FORCE "$@"
