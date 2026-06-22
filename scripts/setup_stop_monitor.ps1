# 安装止损监控计划任务 Quant-StopLoss: 每个交易日盘中每20分钟执行 stop_monitor.py --go
# 用法: powershell -ExecutionPolicy Bypass -File H:\quant\scripts\setup_stop_monitor.ps1
$dir = "H:\quant"
Write-Host "=== Setup Quant-StopLoss ===" -ForegroundColor Cyan
Unregister-ScheduledTask -TaskName "Quant-StopLoss" -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File $dir\scripts\run_stop_monitor.ps1"
# 每天09:35起, 每20分钟一次, 持续5h25m(到~15:00)
$trigger = New-ScheduledTaskTrigger -Daily -At "09:35"
$rep = (New-ScheduledTaskTrigger -Once -At "09:35" -RepetitionInterval (New-TimeSpan -Minutes 20) -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 25)).Repetition
$trigger.Repetition = $rep
Register-ScheduledTask -TaskName "Quant-StopLoss" -Action $action -Trigger $trigger -RunLevel Highest -Force | Out-Null
Write-Host "[OK] Quant-StopLoss: 每交易日 09:35-15:00 每20分钟 --go" -ForegroundColor Green
Get-ScheduledTask -TaskName "Quant-StopLoss" | Select-Object TaskName,State | Format-Table
