# 盘中实时行情定时任务 — 以管理员身份在 PowerShell 运行此脚本
# 用法: powershell -ExecutionPolicy Bypass -File H:\quant\scripts\setup_intraday.ps1

$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$dir = "H:\quant"
$script = "H:\quant\scripts\intraday_watchlist.py"

# 删除旧任务（如果存在）
Unregister-ScheduledTask -TaskName "Quant-Intraday" -Confirm:$false -ErrorAction SilentlyContinue

# 创建新任务：每天09:30-15:00每5分钟运行
$action = New-ScheduledTaskAction -Execute $py -WorkingDirectory $dir -Argument $script

$trigger = New-ScheduledTaskTrigger -Daily -At "09:30" `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 30)

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "Quant-Intraday" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "盘中每5分钟拉取监测股实时行情推回Linux" `
    -RunLevel Highest -User "NT AUTHORITY\SYSTEM"

Write-Host "✅ Quant-Intraday 定时任务已创建" -ForegroundColor Green
Write-Host ""

# 立即测试一次
Write-Host "测试运行..." -ForegroundColor Yellow
& $py $script
Write-Host "测试完成" -ForegroundColor Green
