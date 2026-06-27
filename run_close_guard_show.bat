@echo off
rem ── 手動確認用: 引け前ガードを実行し、結果画面をキー入力まで保持する ──
rem ダブルクリックで実行 → 実行結果を表示したまま、キーを押すまでウィンドウが閉じない。
rem （タスクスケジューラには run_close_guard.bat の方を登録すること。こちらは手動確認専用）
call "%~dp0run_close_guard.bat" show
