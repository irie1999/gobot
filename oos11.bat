@echo off
REM ============================================================
REM oos11.bat - kept for muscle memory. Same as:  .\oos 2025-11
REM   The cutoff month is now an argument, so use oos.bat directly:
REM       .\oos 2026-01     bases 2025-09..2026-01 -> OOS from 2026-02
REM       .\oos 2025-11     bases 2025-09..2025-11 -> OOS from 2025-12
REM   ASCII-only on purpose (18.10.1).
REM ============================================================
cd /d "%~dp0"
call "%~dp0oos.bat" 2025-11 %*
