# QMT持仓自动推送 + 盘中行情 — 一键安装
# 用法: powershell -ExecutionPolicy Bypass -File H:\quant\scripts\setup_push.ps1

$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$dir = "H:\quant"

Write-Host "=== Setup QMT Auto Push ===" -ForegroundColor Cyan

# 1. 删除旧任务
Unregister-ScheduledTask -TaskName "Quant-Push" -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "Quant-Intraday" -Confirm:$false -ErrorAction SilentlyContinue

# 2. QMT持仓推送 (每30分钟)
$action1 = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -File $dir\scripts\push_positions.ps1"
$trigger1 = New-ScheduledTaskTrigger -Once -At "09:35" -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName "Quant-Push" -Action $action1 -Trigger $trigger1 -RunLevel Highest -Force
Write-Host "[OK] Quant-Push: every 30min" -ForegroundColor Green

# 3. 盘中行情推送 (每5分钟, 已有intraday_watchlist.py)
$action2 = New-ScheduledTaskAction -Execute $py -WorkingDirectory $dir -Argument "$dir\scripts\intraday_watchlist.py"
$trigger2 = New-ScheduledTaskTrigger -Once -At "09:30" -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName "Quant-Intraday" -Action $action2 -Trigger $trigger2 -RunLevel Highest -Force
Write-Host "[OK] Quant-Intraday: every 5min" -ForegroundColor Green

# 4. 立即测试一次QMT推送
Write-Host ""; Write-Host "[TEST] Running export now..." -ForegroundColor Yellow
& $py "$dir\scripts\export_qmt_positions.py" --no-push
Write-Host "[TEST] Export done" -ForegroundColor Yellow

Write-Host ""; Write-Host "=== Setup Complete ===" -ForegroundColor Cyan
Write-Host "Push: every 30min (09:35-15:35)"
Write-Host "Intraday: every 5min (09:30-15:30)"
