# ============================================================
# setup_lss_watcher_task.ps1
#   lss 日中OCO決済ウォッチャーを Windows タスクスケジューラに登録する。
#
#   - 毎日 8:45 に run_lss_watcher.bat を起動
#   - StartWhenAvailable: その時刻にPCが起動していなくても、
#     次にPCを起動/ログオンしたタイミングで(起動漏れ分を)自動実行
#   - ログオン時トリガーも追加(念のため二重の保険)
#   - 多重起動は BAT/スクリプト側の lock で防止(重複しても1つだけ動く)
#
#   使い方(このファイルを右クリック →「PowerShell で実行」、または):
#     powershell -ExecutionPolicy Bypass -File setup_lss_watcher_task.ps1
#
#   解除したいとき:
#     Unregister-ScheduledTask -TaskName "LSS Exit Watcher" -Confirm:$false
# ============================================================

$ErrorActionPreference = "Stop"

# このスクリプトが置かれているフォルダ(=swingtrade)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $dir "run_lss_watcher.bat"

if (-not (Test-Path $bat)) {
    Write-Host "✗ run_lss_watcher.bat が見つかりません: $bat" -ForegroundColor Red
    Write-Host "  このスクリプトを swingtrade フォルダに置いて実行してください。"
    exit 1
}

$taskName = "LSS Exit Watcher"

# 実行内容: run_lss_watcher.bat を swingtrade フォルダで起動
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$bat`"" -WorkingDirectory $dir

# トリガー: 毎日 8:45 + ログオン時
$trigDaily = New-ScheduledTaskTrigger -Daily -At 8:45AM
$trigLogon = New-ScheduledTaskTrigger -AtLogOn

# 設定: 起動漏れは可能になり次第実行 / 多重起動は新規を無視 / 実行時間上限8時間
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# 現在のユーザーで登録(kabuステーションはユーザーセッションが必要なため対話ユーザーで実行)
Register-ScheduledTask -TaskName $taskName `
    -Action $action `
    -Trigger @($trigDaily, $trigLogon) `
    -Settings $settings `
    -Description "lss(逆指値空売り・同日決済)の日中OCO決済ウォッチャーを毎営業日8:45に起動" `
    -Force | Out-Null

Write-Host "✓ タスク登録完了: `"$taskName`"" -ForegroundColor Green
Write-Host "  - 毎日 8:45 に $bat を起動"
Write-Host "  - PCが8:45にオフでも、次回起動/ログオン時に自動実行(StartWhenAvailable)"
Write-Host "  - 多重起動は lock で防止(重複しても1つだけ動作)"
Write-Host ""
Write-Host "確認:   Get-ScheduledTask -TaskName `"$taskName`""
Write-Host "手動実行: Start-ScheduledTask -TaskName `"$taskName`""
Write-Host "解除:   Unregister-ScheduledTask -TaskName `"$taskName`" -Confirm:`$false"
