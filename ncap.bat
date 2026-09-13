@echo off
REM N capital sweep only. Does NOT run dailyfast (no 5-min tabs, no HTML).
REM   .\ncap              11.5 years, current price band
REM   .\ncap --days 730   2 years only (fast)
REM   .\ncap --out n_cap.txt
python n_capital.py %*
