@echo off
rem ── 引け前ガード: 損切り/タイムカット + 利確指値の確認・補完 ──
rem タスクスケジューラから毎営業日 14:50 に実行する想定（自動で閉じる）。
rem 手動で結果を見ながら実行したい時は run_close_guard_show.bat を使う。
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
  exit /b 1
)

rem ※本番口座に実発注します。テストしたい時は末尾の --execute を外す(dry-run)
"%PYEXE%" close_stop_guard.py --kabu --prod --execute --with-targets
