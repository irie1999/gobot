@echo off
REM =====================================================
REM setup_startup.bat ― auto_dryrun.bat をWindowsスタートアップに登録
REM
REM 【動作】
REM 1. このファイルを管理者権限で実行 (右クリック→管理者として実行)
REM 2. Windowsスタートアップフォルダにショートカット作成
REM 3. 次回PC起動時から auto_dryrun.bat が自動実行される
REM
REM 【解除】
REM   shell:startup フォルダから auto_dryrun.lnk を削除
REM =====================================================

chcp 65001 > nul

cd /d "%~dp0"

set "startupFolder=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "shortcutPath=%startupFolder%\auto_dryrun.lnk"
set "targetPath=%CD%\auto_dryrun.bat"
set "workDir=%CD%"

echo =====================================================
echo  Windowsスタートアップ登録
echo =====================================================
echo  対象: %targetPath%
echo  登録先: %shortcutPath%
echo.

REM PowerShell でショートカット作成
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut('%shortcutPath%'); $sc.TargetPath = '%targetPath%'; $sc.WorkingDirectory = '%workDir%'; $sc.Description = 'kabu Donchian Bot Auto Dry-run'; $sc.Save(); Write-Host '✓ ショートカット作成成功'"

if %errorlevel% equ 0 (
    echo.
    echo =====================================================
    echo  ✓ 登録完了
    echo =====================================================
    echo.
    echo 次回PC起動時から auto_dryrun.bat が自動実行されます。
    echo.
    echo 【解除する場合】
    echo   1. Win+R → shell:startup → Enter
    echo   2. auto_dryrun.lnk を削除
    echo.
    echo 【テスト】
    echo   PC再起動 or auto_dryrun.bat を直接実行
    echo.
) else (
    echo.
    echo ✗ 登録失敗。管理者権限で実行してください。
    echo.
)

pause
