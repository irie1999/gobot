@echo off
rem ── 引け前ガード: 損切り/タイムカット + 利確指値の確認・補完 ──
rem タスクスケジューラ・手動 どちらで実行しても、最後に必ず画面を保持する
rem （キーを押すまで閉じない）。
rem %~dp0 = この .bat があるフォルダ。cwd をそこに固定して CSV/JSON を正しく読む。
cd /d "%~dp0"

rem ── Python の解決 ───────────────────────────────────────────────
rem タスクスケジューラはユーザーのPATHを読まないことがあり、`python` が
rem 見つからない/Microsoft Store版スタブでハングする。py ランチャを優先する。
rem ★ それでも動かない場合は次行のremを外してフルパスを設定してください:
rem set "PYEXE=C:\Users\to732\AppData\Local\Programs\Python\Python311\python.exe"

if not defined PYEXE (
  where py >nul 2>&1 && set "PYEXE=py"
)
if not defined PYEXE (
  where python >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
  echo [ERROR] Python が見つかりません。run_close_guard.bat の PYEXE にフルパスを設定してください。
  goto :hold
)

rem ※本番口座に実発注します。テストしたい時は末尾の --execute を外す(dry-run)
"%PYEXE%" close_stop_guard.py --kabu --prod --execute --with-targets

:hold
echo.
echo ============================================================
echo  実行が完了しました(上の内容を確認してください)。
echo  このウィンドウを閉じるには何かキーを押してください。
echo ============================================================
pause >nul
