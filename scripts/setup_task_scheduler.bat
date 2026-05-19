@echo off
REM ============================================================
REM  一键配置 Windows 任务计划程序
REM  请以【管理员身份】运行此脚本
REM ============================================================

echo 正在配置 Track A 每日自动执行任务...

REM 创建任务：每个工作日 14:30 执行
schtasks /create /tn "QuantTrackA" ^
  /tr "H:\quant\scripts\run_daily.bat" ^
  /sc WEEKLY ^
  /d MON,TUE,WED,THU,FRI ^
  /st 14:30 ^
  /ru "%USERNAME%" ^
  /f

if %errorlevel% == 0 (
    echo.
    echo ✅ 任务创建成功！
    echo.
    echo 任务名称: QuantTrackA
    echo 执行时间: 每个工作日 14:30
    echo 执行内容: H:\quant\scripts\run_daily.bat
    echo.
    echo 查看任务: schtasks /query /tn "QuantTrackA"
    echo 手动触发: schtasks /run /tn "QuantTrackA"
    echo 删除任务: schtasks /delete /tn "QuantTrackA" /f
) else (
    echo.
    echo ❌ 创建失败，请确认以管理员身份运行
)

pause
