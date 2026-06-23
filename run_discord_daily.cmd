@echo off
setlocal
cd /d C:\Users\chaim\Desktop\RICH

if not exist logs mkdir logs

echo [%date% %time%] starting discord_daily_report.py>> logs\discord_daily.log
py discord_daily_report.py >> logs\discord_daily.log 2>&1
set ERR=%ERRORLEVEL%
echo [%date% %time%] exit code %ERR%>> logs\discord_daily.log

exit /b %ERR%
