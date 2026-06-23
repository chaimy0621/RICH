@echo off
REM ===================================================================
REM  RICH Daily Pipeline orchestrator
REM  Task: RICH_Daily_Pipeline (04:00 daily).  Working dir: this folder.
REM  Sequence:  a) python run_daily.py   b) python track_performance.py
REM  Fail-loud to Discord (%DISCORD_WEBHOOK_URL%); appends logs\rich_daily_pipeline.log.
REM ===================================================================
setlocal
cd /d "C:\Users\chaim\Desktop\RICH"
set "PY=C:\Users\chaim\AppData\Local\Programs\Python\Python314\python.exe"
set "CURL=%SystemRoot%\System32\curl.exe"
set "LOG=logs\rich_daily_pipeline.log"
if not exist "logs" mkdir "logs"

echo.>>"%LOG%"
echo === RICH Daily Pipeline %DATE% %TIME% ===>>"%LOG%"

echo [%TIME%] step a: run_daily.py>>"%LOG%"
"%PY%" run_daily.py >>"%LOG%" 2>&1
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" ( set "STEP=run_daily.py" & goto :fail )

echo [%TIME%] step b: track_performance.py>>"%LOG%"
"%PY%" track_performance.py >>"%LOG%" 2>&1
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" ( set "STEP=track_performance.py" & goto :fail )

echo [%TIME%] SUCCESS>>"%LOG%"
if defined DISCORD_WEBHOOK_URL "%CURL%" -s -H "Content-Type: application/json" -d "{\"content\": \"✅ RICH Daily Pipeline complete\"}" "%DISCORD_WEBHOOK_URL%" >nul 2>&1
endlocal & exit /b 0

:fail
echo [%TIME%] FAILED at %STEP% exit %EC%>>"%LOG%"
if defined DISCORD_WEBHOOK_URL "%CURL%" -s -H "Content-Type: application/json" -d "{\"content\": \"❌ RICH Daily Pipeline failed at %STEP% — exit %EC%\"}" "%DISCORD_WEBHOOK_URL%" >nul 2>&1
endlocal & exit /b 1
