$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$dir = "H:\quant"
$script = "H:\quant\scripts\intraday_watchlist.py"

Unregister-ScheduledTask -TaskName "Quant-Intraday" -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute $py -WorkingDirectory $dir -Argument $script
$trigger = New-ScheduledTaskTrigger -Daily -At "09:30" -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "Quant-Intraday" -Action $action -Trigger $trigger -Settings $settings -Description "Intraday watchlist to Linux" -RunLevel Highest -User "NT AUTHORITY\SYSTEM"

Write-Host "[OK] Quant-Intraday task created" -ForegroundColor Green
Write-Host "[TEST] Running once..."
& $py $script
Write-Host "[DONE]"
