"""
setup_global_claude_md.py
ローカルPC で実行: python setup_global_claude_md.py
~/.claude/CLAUDE.md を作成してgobotの共通知識を全セッションで共有する。
"""
from pathlib import Path

CONTENT = """\
# gobot トレードシステム 共通知識（全セッション共有）

詳細仕様: gobot リポジトリの CLAUDE.md 参照

---

## BTスコアの仕組み

BTスコア = 直近365日バックテストを6期間(30/60/90/120/150/180日)にスライスして平均

  平均勝率        × 0.4   → 最大40点
  平均PF ÷ 10    × 30   → 最大30点（PF=10でキャップ、∞は10扱い）
  期間安定性      × 20   → 最大20点（全期間プラスで満点）
  取引回数補正    × 10   → 最大10点（20取引で満点）
  ランク: ★★★≥80 / ★★≥60 / ★≥40 / △<40

ATRペナルティ: 損切り幅>7% → スコア × max(0.5, 1-(損切り幅-7%)/30)

---

## WF FOLDS 定義（scan_walkforward.py）

FOLDS = [
    ("fold1", 730, 370, 370, 180),  # TRAIN: 730〜370日前(12M) / TEST: 370〜180日前(6M)
    ("fold2", 550, 180, 180,   0),  # TRAIN: 550〜180日前(12M) / TEST: 180〜0日前(6M)
]

合格条件:
  TRAIN: trades≥5, PF≥1.5, win_rate≥55%, total_pnl>0
  TEST:  trades≥2, PF≥1.2, win_rate≥45%, total_pnl>0

---

## In-sample/OOSの関係

  銘柄選定（scan_walkforward）: ホールドアウトN日を除いた期間のみ使用
  BTスコアの365日:              訓練期間(In-sample) + ホールドアウト期間(OOS) 両方含む
  → BTスコアはIn-sample biasあり。OOS検証は損益タブのHO180dで確認

  HO30d〜HO180dの6設定それぞれが異なるWATCHLIST（異なる銘柄セット）を持つ
  HO180dで損益プラス = 完全に知らない相場でも機能した証拠

---

## 日次コマンド（スイング）

python run_signals_holdout_all.py --both --max-price 6000 --min-price 1000 --force --days 365
→ signals_holdout_all_both_YYYY-MM-DD.html（ロング/ショートタブ）

---

## 主要定数（backtest_limit_entry.py）

ENTRY_EXPIRE = 3        # 逆指値有効日数
MAX_HOLD     = 15       # 最大保有日数（RSI2=7, MOM=20 は別設定）
FIXED_QTY    = 100      # 固定株数
SLIPPAGE_STOP_PCT  = 0.005   # 逆指値 +0.5%
FEE_PCT_ONE_WAY    = 0.001   # 片道手数料 0.1%

---

## 主要ファイル早見表

| ファイル                      | 役割                                          |
|------------------------------|-----------------------------------------------|
| run_signals_holdout_all.py   | メイン日次レポート（--both でロング+ショート統合） |
| check_signals_stop.py        | 逆指値B（MACD/A7/RSI2）シグナル + BTスコア     |
| check_signals_breakout.py    | ブレイクアウト（DON/VOL/MOM）シグナル + BTスコア |
| backtest_limit_entry.py      | 唯一の約定ロジック（触ると全体に影響）           |
| scan_walkforward.py          | WF銘柄スキャン（ホールドアウト対応）            |
| nikkei_analysis.py           | 相場環境・トレンド・エントリー分析HTML生成       |
| position_server.py           | ポジション管理Webサーバー                       |
"""

path = Path.home() / ".claude" / "CLAUDE.md"
path.parent.mkdir(parents=True, exist_ok=True)

if path.exists():
    backup = path.with_suffix(".md.bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"既存ファイルをバックアップ: {backup}")

path.write_text(CONTENT, encoding="utf-8")
print(f"保存完了: {path}")
print(f"サイズ: {path.stat().st_size} bytes")
