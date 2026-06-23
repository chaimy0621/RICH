@echo off
REM ===================================================================
REM  RICH - smart weekly Moomoo sync with retry state machine
REM  Starts on Monday (day 1 of the week); on failure, retries on each
REM  subsequent weekday run until success or 10 attempts, then HARD FAIL.
REM  Corrections vs draft (2026-06-08):
REM    * PYTHON  -> Python314  (the draft's venv path does not exist)
REM    * date    -> locale-independent WMIC stamp (matches run_data_agent.cmd)
REM    * added FAILED terminal guard so HARD FAIL actually stops retrying
REM ===================================================================
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "STATE_FILE=%ROOT%logs\moomoo_sync_state.txt"
set "PYTHON=C:\Users\chaim\AppData\Local\Programs\Python\Python314\python.exe"
REM  If you later create a venv, switch to:
REM  set "PYTHON=%ROOT%venv\Scripts\python.exe"

if not exist "%ROOT%logs" mkdir "%ROOT%logs"

REM --- locale-independent YYYY-MM-DD stamp (same trick as run_data_agent.cmd)
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set "DT=%%I"
set "STAMP=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%"
set "LOG=%ROOT%logs\moomoo_sync_%STAMP%.txt"

REM --- read state: IDLE / RETRYING / FAILED   (default IDLE 0)
set "MODE=IDLE"
set "ATTEMPTS=0"
if exist "%STATE_FILE%" (
    for /f "tokens=1,2" %%a in (%STATE_FILE%) do (
        set "MODE=%%a"
        set "ATTEMPTS=%%b"
    )
)

REM --- terminal guard: once HARD FAILed, stop until manually reset
if "%MODE%"=="FAILED" (
    echo %STAMP% skipped - state FAILED, manual reset required >> "%LOG%"
    goto :EOF
)

REM --- IDLE only starts on Monday (day 1 of the week); Tue-Fri are retries
for /f %%d in ('powershell -NoProfile -Command "(Get-Date).DayOfWeek"') do set "TODAY_DOW=%%d"
if "%MODE%"=="IDLE" if NOT "%TODAY_DOW%"=="Monday" goto :EOF

REM --- run sync
echo RUNNING sync attempt %ATTEMPTS% (%STAMP%) >> "%LOG%"
"%PYTHON%" sync_moomoo.py >> "%LOG%" 2>&1
if %ERRORLEVEL%==0 (
    echo IDLE 0 > "%STATE_FILE%"
    echo SUCCESS >> "%LOG%"
    goto :EOF
)

REM --- failed: increment attempts
set /a ATTEMPTS=%ATTEMPTS%+1
echo RETRYING %ATTEMPTS% > "%STATE_FILE%"
echo FAILED attempt %ATTEMPTS% >> "%LOG%"

if %ATTEMPTS% GEQ 10 (
    echo FAILED 10 > "%STATE_FILE%"
    echo VERDICT: HARD FAIL after 10 attempts >> "%LOG%"
)
endlocal
