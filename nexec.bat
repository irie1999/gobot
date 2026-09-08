@echo off
REM ============================================================
REM nexec.bat - N (09:00 confirm) REAL ORDERS
REM
REM   .\nexec              DRY RUN against the LIVE book. Sends nothing.
REM   .\nexec --go         REAL ORDERS on the live account.
REM   .\nexec --go --budget 50    first day: small (1-2 names)
REM
REM   *** THE DRY RUN READS THE LIVE BOOK (--prod) ON PURPOSE. ***
REM   Only --go adds --execute. A dry run against the demo book would show
REM   different prices and different names, so it would not tell you what
REM   the real run is about to do (2026-08-31 review, item 6).
REM
REM   ASCII-only on purpose (Japanese comments break on Shift-JIS cmd, 18.10.1).
REM
REM WHAT N IS (18.54 / 18.55, differs from J in five ways)
REM   candidates  n_signals_<date>.csv  (prev-day return >= +1.753 pct,
REM                                      top 50 by 20-day turnover)
REM   pass        open >= prev close + 100bp, NO UPPER GAP LIMIT,
REM               and the 09:00 open must itself be 1,000-6,000 yen
REM   size        100 SHARES, FIXED
REM   entry       LIMIT AT THE OPEN PRICE, exactly. Nothing is passed here;
REM               k_open_confirm defaults are --n-limit-ticks 0 and
REM               --n-limit-max-bp 7.5, and with 0 ticks the limit is the open
REM               (see the loop at k_open_confirm.py:1375, it runs 0 times).
REM               A sell limit fills at or ABOVE its price, so this fills only
REM               while the price is at or above the open, and waits otherwise
REM               until 09:10 cancels it. About 80 pct fill.
REM               *** THIS BLOCK USED TO SAY THE OPPOSITE. ***
REM               It described "--n-limit-ticks 100 --n-limit-max-bp 300, a
REM               protective floor 3 pct below the open" and warned "do NOT
REM               use a limit AT the open". Those flags were never on the
REM               command line - only in this comment - and the code default
REM               was changed to 0 on 2026-08-31 with its own reasoning at
REM               k_open_confirm.py:231. Two decisions in opposite directions,
REM               one comment never updated. On 2026-09-07 it cost an hour:
REM               the unfilled names were read as "protective limit rejected
REM               a 3 pct collapse" when the truth is "one tick down and it
REM               does not fill".
REM               *** AND THE OLD WARNING WAS WRONG ON THE FACTS. ***
REM               Measured on the purchased Aug-2026 tick data (469 candidates,
REM               20 sessions, live order, 4M budget): sweeping the limit from
REM               the open down to a market order takes the fill rate from
REM               77 pct to 100 pct and does NOT raise P&L (+222,750 at the
REM               open, +212,950 at market; the whole range 201k-230k sits
REM               inside the random-order band sigma of 35,576). The names it
REM               misses look like winners only because that assumes filling
REM               AT the open. At the real +10s price they are 42 wins /
REM               36 losses. Leave it at the open. See CLAUDE.md 18.70.
REM   exit        CLOSING MOC ONLY. no stop, no target, NO WATCHER.
REM
REM HOW THE EXIT IS MADE SAFE (this is the part that took two rounds)
REM   J placed all orders, kept partial fills, then swept get_positions() for
REM   a closing MOC - and left the rest to the watcher. N has no watcher, so
REM   that shape leaves two holes: a partial fill can complete AFTER the MOC
REM   quantity was decided, and sweeping positions touches OTHER strategies'
REM   shorts (a margin close cancels their existing closing order).
REM   N instead settles only its own orders:
REM     1. wait until EVERY order id we were handed actually shows up in
REM        /orders. A fresh id can lag; if we skipped this we would read
REM        "no fills" while the order was still live on the book.
REM     2. cancel every one of our new-sell orders, filled-in-part included
REM     3. wait until every one of them reaches a terminal state
REM        -> only then is the filled quantity final
REM     4. place one closing MOC per symbol for exactly that quantity,
REM        naming our own HoldIDs, and only if they add up exactly
REM     5. confirm each MOC was accepted; shout loudly if any was not
REM   Anything short of all five prints a warning and exits non-zero.
REM
REM WHAT MAKES IT REFUSE TO TRADE AT ALL
REM   Before the FIRST sell, it snapshots the existing order ids. That
REM   snapshot is the only way to recover an order whose send timed out. If
REM   the snapshot cannot be read, N sends NOTHING for the day - it stops
REM   before writing the ledger, so no phantom rows are left behind either.
REM   Per name, it also refuses to send if the ledger row cannot be written:
REM   an order with no ledger row is a position tomorrow's guard cannot see.
REM
REM SEQUENCE
REM   0. wait for the window if we are outside it (clock only, no kabu).
REM      Start it whenever you like - the night before is fine. It waits for
REM      the next 08:40, skipping weekends, and only then builds the list.
REM   1. build the candidates (yfinance only, no kabu)
REM   2. 08:55  register + one warm read (skipping this costs 40-140s at 09:00)
REM   3. 09:00  poll every 10s, sell each name that opens and passes
REM   4. 09:10  polling ends -> the settle sequence above runs
REM   5. after the close:  .\fills
REM
REM AFTERWARDS
REM   Do NOT start .\watch or .\jwatch. They read ordered_signals_lss.csv;
REM   N writes ordered_signals_n.csv precisely so they cannot see it. If one
REM   did see an N position it would arm J's rules, and for a position with
REM   no ATR it falls back to an emergency 1 percent stop.
REM ============================================================
cd /d "%~dp0"

set "NGO="
set "NARGS="
set "NBUD=--budget 200"
REM --ws is ON by default. .\nexec --go --no-ws turns it off for one run.
set "NWS=--ws"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="-h"     goto :help
if /i "%~1"=="--help" goto :help
if /i "%~1"=="/?"     goto :help
if /i "%~1"=="--go"       set "NGO=--execute"      & shift & goto :parse
if /i "%~1"=="--budget"   set "NBUD=--budget %~2"  & shift & shift & goto :parse
if /i "%~1"=="--no-ws"    set "NWS="               & shift & goto :parse
set "NARGS=%NARGS% %~1"
shift
goto :parse
:parsed

echo ============================================================
if defined NGO (
  echo  N REAL ORDERS - LIVE ACCOUNT
  echo  *** this will send real orders at 09:00 ***
) else (
  echo  N DRY RUN - live book, nothing is sent
  echo  add --go to send real orders
)
echo    start it whenever you like - it waits for the next 08:40 by itself
echo    do NOT run .\norder, .\jorder, .\watch or the order server
echo    at the same time (kabu allows exactly one live token)
echo    do NOT start a watcher afterwards - N needs none, and a watcher
echo    would arm J's rules on N positions
echo ============================================================
echo.
REM Wait for the order window BEFORE anything else. It must come before the
REM candidate list, because Python freezes today's date at import: waiting
REM inside the main script across midnight leaves every symbol flagged
REM stale_open and nothing qualifies. Waiting here means the collect and the
REM main script both start on the correct day.
REM This step touches no kabu API and sends nothing - it only sleeps.
echo [0/3] waiting for the order window if we are outside it
python wait_window.py --until 08:40 --window-end 09:30
if errorlevel 1 (
  echo.
  echo *** STOPPED BEFORE STARTING - NO ORDERS WERE SENT ***
  exit /b 2
)
echo.
echo [1/3] building the candidate list (no kabu)
python n_paper.py --collect --no-mirror
if errorlevel 1 (
  echo.
  echo *** candidate list FAILED - stopping here ***
  exit /b 1
)
echo.
echo [2/3] warm read at 08:55, then [3/3] poll from 09:00 to 09:10
REM --ws : take the board over PUSH (WebSocket) instead of polling REST.
REM   WHY. Reading 50 names over REST measured 36.5 SECONDS at the open
REM   (0.73s per name, 18.44). The first name is fast, the last waits 36s,
REM   so the AVERAGE wait is about 18 seconds. The 1-minute-bar study in
REM   18.44 puts that at roughly -6bp, against a gross edge of +15.7bp per
REM   trade. That is nearly 40 pct of the edge lost to waiting in line.
REM   PUSH delivers the board when it changes, so the queue disappears.
REM   Measured 2026-09-08 after the close: register to first message 0.3s,
REM   90 pct of a 50-name batch inside 1.0-1.5s, nothing missing.
REM
REM   THIS DOES NOT CHANGE WHICH NAMES WE BUILD. Still the top 50 by
REM   turnover, still +100bp, still 100 shares, still MOC only. The only
REM   thing that changes is HOW FAST the open price reaches us. It moves
REM   live TOWARD the backtest, which assumes the open price itself.
REM
REM   FAIL-SAFE. If the socket will not connect, or a name never ticks, or
REM   PUSH hands back YESTERDAY's open, that name falls back to REST and
REM   the run behaves exactly as it does today. The downside is bounded to
REM   the current behaviour - see k_open_confirm.py around _read_all.
REM
REM   NOT MEASURED YET: the 09:00 auction itself. The 2026-09-08 numbers
REM   were taken at 09:08 with every name already printed. At the auction
REM   the push queue may back up. Watch the "PUSH n / HTTP n" counter in
REM   the read line - if PUSH is 0 every poll, the socket is not helping
REM   and it is falling back to REST anyway.
REM
REM   DO NOT run kabu_ws.py at the same time. kabu accepts ONE websocket
REM   connection and a second one drops the first. kabu_ws now refuses to
REM   start on a weekday between 08:30 and 15:40 for this reason.
REM
REM   To turn it off for a day: .\nexec --go --no-ws
REM   NWS is set in the parse loop above, the same way NGO and NBUD are.
REM   *** DO NOT use string substitution to detect --no-ws here. ***
REM   The first version tested NARGS with a substring replacement, but NARGS
REM   is DELETED by the empty set at the top, and a plain run
REM   (--go --budget 400) never adds to it because both are eaten by the
REM   parse loop. Replacement on an undefined variable broke the line and cmd
REM   printed a set-syntax error on the morning of a live run, 2026-09-09,
REM   minutes before the 09:00 orders. A flag in the parse loop cannot fail
REM   this way. Do not write percent signs in these REM lines either - cmd
REM   expands variables on REM lines too.
python k_open_confirm.py --n-mode --prod --poll %NWS% %NGO% %NBUD%%NARGS%
REM Exit 2 means the script stopped BEFORE connecting to kabu (outside the
REM order window, or last run left an unsettled order). Nothing was sent, so
REM do not send the operator hunting for naked shorts.
REM Check errorlevel 2 first: "if errorlevel N" means ">= N" in cmd.
if errorlevel 2 (
  echo.
  echo *** STOPPED BEFORE STARTING - NO ORDERS WERE SENT ***
  echo     The reason is printed above. Nothing to clean up.
  exit /b 2
)
if errorlevel 1 (
  echo.
  echo *** THE ORDER SCRIPT EXITED WITH AN ERROR ***
  echo     Orders may or may not have been sent. Check kabu station now,
  echo     and if there are open shorts make sure each one has a closing
  echo     MOC. Do NOT assume the run was clean.
  exit /b 1
)
echo.
echo ============================================================
echo  done. after the close (15:40 or later):
echo.
echo      .\fills --no-compare
echo.
echo  (--no-compare because .\fills reads J's detail rows for the backtest
echo   comparison; for N that would compare against the wrong population.)
echo.
echo  and for the paper comparison:
echo      python n_paper.py --close --budget 400 --seq-sides n
echo ============================================================
goto :eof

:help
echo .\nexec                    dry run against the live book (sends nothing)
echo .\nexec --go               REAL orders, live account
echo .\nexec --go --budget 50   first day: small
echo.
echo   N = prev-day +1.753 pct, open ^>= prev close +100bp, sell 100 shares
echo       at the open price, close with a closing MOC.
echo       No stop, no target, no watcher, no upper gap limit.
echo   Use .\norder for paper recording (that one cannot order at all).
goto :eof
