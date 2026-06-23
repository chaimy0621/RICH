@echo off
title RICH AWS Status Check
color 0A

echo ================================================================
echo   RICH AWS STATUS CHECK
echo   %date% %time%
echo ================================================================
echo.

set KEY=C:\Users\chaim\Desktop\RICH\rich-prod-key.pem
set HOST=ubuntu@3.86.86.145

echo [1/3] Checking last 60 lines of cron log...
echo ----------------------------------------------------------------
ssh -i "%KEY%" %HOST% "tail -60 /home/ubuntu/RICH/logs/cron.log"

echo.
echo ================================================================
echo [2/3] Today's RICH pick...
echo ----------------------------------------------------------------
ssh -i "%KEY%" %HOST% "cd /home/ubuntu/RICH && source /home/ubuntu/venv/bin/activate && python report_today.py"

echo.
echo ================================================================
echo [3/3] Last 5 run_metrics rows...
echo ----------------------------------------------------------------
ssh -i "%KEY%" %HOST% "tail -5 /home/ubuntu/RICH/logs/run_metrics.csv"

echo.
echo ================================================================
echo   DONE. Press any key to close.
echo ================================================================
pause > nul
