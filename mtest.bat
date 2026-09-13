@echo off
REM ============================================================
REM mtest.bat - morning measurement run (READ-ONLY, never orders)
REM   Usage (swingtrade folder):  .\mtest   (PowerShell)  /  mtest  (cmd)
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd).
REM
REM   Runs, in order:
REM     1. check_board_limits --rotate 100   (can we batch past the 50 cap?)
REM     2. log_preopen_board                 (pre-open quotes, day 1 of ~1 month)
REM     3. check_board_limits --warmup       (08:58, so 09:00 is not a cold read)
REM     4. check_board_limits --open         (09:00 sharp, the real number)
REM
REM   START BY 08:30. Do NOT run .\watch or the order server at the same time
REM   (kabu allows exactly one live token -> 401 tug-of-war).
REM   Place the H orders BEFORE running this.
REM   When it finishes it tells you to start .\watch immediately.
REM ============================================================
python morning_test.py --prod %*
