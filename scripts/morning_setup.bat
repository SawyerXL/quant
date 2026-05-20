@echo off
chcp 65001 >nul
REM ============================================================
REM  Run this script as Administrator at 8:00 AM
REM  After this, only login to QMT at 9:00 AM manually
REM  System will auto-execute at 14:35 today
REM ============================================================

cd /d H:\quant

echo.
echo ====================================================
echo   Quant System - Morning Setup
echo   %date% %time%
echo ====================================================
echo.

REM Step 1: Pull latest code
echo [1/4] Pulling latest code from GitHub...
git pull origin main
echo       Done.

REM Step 2: Create daily auto-task (14:30 every weekday, permanent)
echo.
echo [2/4] Creating daily task (14:30 Mon-Fri)...
schtasks /delete /tn "QuantTrackA_Daily" /f >nul 2>&1
schtasks /create /tn "QuantTrackA_Daily" ^
  /tr "cmd /c cd /d H:\quant && python scripts\run_daily.bat >> logs\auto_execute.log 2>&1" ^
  /sc WEEKLY ^
  /d MON,TUE,WED,THU,FRI ^
  /st 14:30 ^
  /ru "%USERNAME%" ^
  /f >nul 2>&1
echo       Done.

REM Step 3: Create one-time setup task for TODAY at 14:35
echo.
echo [3/4] Scheduling initial position setup for today at 14:35...
schtasks /delete /tn "QuantTrackA_Setup" /f >nul 2>&1
schtasks /create /tn "QuantTrackA_Setup" ^
  /tr "cmd /c cd /d H:\quant && python scripts\fetch_and_execute.py --track a --setup >> logs\setup_execute.log 2>&1 && python scripts\reconcile.py --track a >> logs\setup_execute.log 2>&1" ^
  /sc ONCE ^
  /st 14:35 ^
  /ru "%USERNAME%" ^
  /f >nul 2>&1
echo       Done.

REM Step 4: Verify tasks
echo.
echo [4/4] Verifying tasks...
echo --- Daily task (permanent) ---
schtasks /query /tn "QuantTrackA_Daily" /fo LIST | find "Next Run Time"
echo --- Setup task (today only) ---
schtasks /query /tn "QuantTrackA_Setup" /fo LIST | find "Next Run Time"

echo.
echo ====================================================
echo   Setup complete!
echo.
echo   TODO (only one thing left for you to do):
echo   09:00  Open QMT, login account: 1633013579
echo          Keep QMT running in background
echo.
echo   14:35  System will AUTO buy 30 stocks via QMT
echo   Daily  System will AUTO run at 14:30 every weekday
echo ====================================================
echo.
pause
