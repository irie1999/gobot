# run_close_guard.ps1
# close 方式の損切りガードを「毎営業日の引け前」に自動実行するランナー。
# Windows タスクスケジューラから呼び出す想定。
#
# 動作:
#   1. このスクリプトのあるフォルダ(リポジトリ)へ移動
#   2. 土日はスキップ(祝日は未考慮)
#   3. 本番APIパスワードを User 環境変数から読み込む
#   4. close_stop_guard.py を本番(--prod) dry-run で実行し、結果を close_guard_log.txt に追記
#
# ※ 既定は dry-run(発注しない)。実発注したくなったら下の python 行に --execute を足す。
# ※ kabuステーションが「本番モードで起動・ログイン済み」でないと時価が取れません。

$ErrorActionPreference = "Continue"

# このスクリプトのあるフォルダ(リポジトリ直下)へ移動
Set-Location $PSScriptRoot

# 土日はスキップ
$dow = (Get-Date).DayOfWeek
if ($dow -eq "Saturday" -or $dow -eq "Sunday") {
    exit 0
}

# 本番APIパスワードを User スコープ環境変数から確実に読み込む
if (-not $env:KABU_API_PASSWORD_PROD) {
    $env:KABU_API_PASSWORD_PROD = [System.Environment]::GetEnvironmentVariable("KABU_API_PASSWORD_PROD", "User")
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path "close_guard_log.txt" -Value "`n==== $stamp  close_stop_guard 実行 ===="

# dry-run(発注なし)で判定。実発注する場合は末尾に --execute を追加する。
python close_stop_guard.py --prod --log my_positions.csv *>> close_guard_log.txt
