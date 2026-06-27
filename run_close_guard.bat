@echo off
rem ── 引け前ガード: 損切り/タイムカット + 利確指値の確認・補完 ──
rem タスクスケジューラから毎営業日 14:50 に実行する想定。
rem %~dp0 = この .bat があるフォルダ。cwd をそこに固定して CSV/JSON を正しく読む。
cd /d "%~dp0"

rem ※本番口座に実発注します。テストしたい時は末尾の --execute を外す(dry-run)
python close_stop_guard.py --kabu --prod --execute --with-targets

rem 失敗時にウィンドウが即閉じないようにしたい場合は次行のremを外す
rem pause
