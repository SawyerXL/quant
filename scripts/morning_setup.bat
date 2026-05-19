@echo off
REM ============================================================
REM  明天早上八点运行此脚本（管理员身份）
REM  完成后系统全自动，你只需要 09:00 登录一下 QMT
REM ============================================================

cd /d H:\quant

echo.
echo ====================================================
echo   量化交易系统 - 早间初始化
echo   %date% %time%
echo ====================================================
echo.

REM 1. 拉取最新代码
echo [1/4] 拉取最新代码...
git pull origin main
echo      完成

REM 2. 设置每日自动执行任务（永久，每个工作日14:30）
echo.
echo [2/4] 配置每日自动任务（工作日14:30）...
schtasks /delete /tn "QuantTrackA_Daily" /f >nul 2>&1
schtasks /create /tn "QuantTrackA_Daily" ^
  /tr "cmd /c cd /d H:\quant && python scripts\run_daily.bat >> logs\auto_execute.log 2>&1" ^
  /sc WEEKLY ^
  /d MON,TUE,WED,THU,FRI ^
  /st 14:30 ^
  /ru "%USERNAME%" ^
  /f >nul 2>&1
echo      完成

REM 3. 设置今日一次性建仓任务（14:35 执行，比日常晚5分钟确保信号已更新）
echo.
echo [3/4] 配置今日建仓任务（今天14:35，一次性）...
schtasks /delete /tn "QuantTrackA_Setup" /f >nul 2>&1
schtasks /create /tn "QuantTrackA_Setup" ^
  /tr "cmd /c cd /d H:\quant && python scripts\fetch_and_execute.py --track a --setup >> logs\setup_execute.log 2>&1 && python scripts\reconcile.py --track a >> logs\setup_execute.log 2>&1" ^
  /sc ONCE ^
  /sd %date:~0,10% ^
  /st 14:35 ^
  /ru "%USERNAME%" ^
  /f >nul 2>&1
echo      完成

REM 4. 验证任务创建成功
echo.
echo [4/4] 验证任务...
schtasks /query /tn "QuantTrackA_Daily" /fo LIST | find "下次运行时间"
schtasks /query /tn "QuantTrackA_Setup" /fo LIST | find "下次运行时间"

echo.
echo ====================================================
echo   ✅ 初始化完成！
echo.
echo   接下来你只需要做一件事：
echo   09:00 打开 QMT，登录仿真账号 1633013579
echo         登录后保持 QMT 在后台运行即可
echo.
echo   14:35 系统将自动建仓（30只股票）
echo   之后每个工作日 14:30 自动执行
echo ====================================================
echo.
pause
