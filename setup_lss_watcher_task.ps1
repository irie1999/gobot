# ============================================================
# setup_lss_watcher_task.ps1
#   Register the lss intraday OCO exit watcher as a Windows Scheduled Task.
#
#   - Runs run_lss_watcher.bat every day at 8:45
#   - StartWhenAvailable: if the PC is off at 8:45, it runs the missed
#     instance the next time the PC starts / you log on
#   - Also adds an AtLogOn trigger as a safety net
#   - Duplicate launches are prevented by a lock inside the watcher
#     (even if triggered twice, only one instance actually runs)
#
#   Run (right-click -> Run with PowerShell, or):
#     powershell -ExecutionPolicy Bypass -File setup_lss_watcher_task.ps1
#
#   Remove:
#     Unregister-ScheduledTask -TaskName "LSS Exit Watcher" -Confirm:$false
#
#   NOTE: This file is intentionally ASCII-only. Windows PowerShell 5.1
#   reads .ps1 as the system ANSI codepage (Shift-JIS) unless there is a
#   UTF-8 BOM, so non-ASCII text would break parsing.
# ============================================================

$ErrorActionPreference = "Stop"

# Folder that contains this script (= swingtrade)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $dir "run_lss_watcher.bat"

if (-not (Test-Path $bat)) {
    Write-Host "[X] run_lss_watcher.bat not found: $bat" -ForegroundColor Red
    Write-Host "    Put this script in the swingtrade folder and run again."
    exit 1
}

$taskName = "LSS Exit Watcher"

# Action: launch run_lss_watcher.bat in the swingtrade folder
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $dir

# Triggers: daily at 8:45 + at logon
$trigDaily = New-ScheduledTaskTrigger -Daily -At 8:45AM
$trigLogon = New-ScheduledTaskTrigger -AtLogOn

# Settings: run missed starts when available, ignore new if already running, 8h limit
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# Register for the current interactive user (kabu station needs a user session)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($trigDaily, $trigLogon) -Settings $settings -Description "lss intraday OCO exit watcher - launched every trading day at 8:45" -Force | Out-Null

Write-Host "[OK] Scheduled task registered: $taskName" -ForegroundColor Green
Write-Host "     - Runs $bat every day at 8:45"
Write-Host "     - If PC is off at 8:45, runs at next start/logon (StartWhenAvailable)"
Write-Host "     - Duplicate launches blocked by lock (only one instance runs)"
Write-Host ""
Write-Host "Check:   Get-ScheduledTask -TaskName '$taskName'"
Write-Host "Run now: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove:  Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
