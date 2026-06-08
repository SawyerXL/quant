# ================================================================
# Windows 自动化配置脚本（以管理员身份在 PowerShell 运行一次）
#
# 配置内容：
#   1. 每次开机自动建立到 Linux 的反向 SSH 隧道
#   2. 每日 15:35 自动导出 QMT 持仓到 Linux
#   3. 保证 sshd 开机自启
# ================================================================

$QuantDir = "H:\quant"
$LinuxServer = "47.116.166.139"
$LinuxUser = "root"

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Quant Windows 自动化配置" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. 确保 sshd 开机自启 ──────────────────────────────────────
$sshd = Get-Service sshd -ErrorAction SilentlyContinue
if ($sshd) {
    Set-Service -Name sshd -StartupType Automatic
    Write-Host "[OK] sshd 已设为开机自启" -ForegroundColor Green
} else {
    Write-Host "[WARN] sshd 未找到，跳过" -ForegroundColor Yellow
}

# ── 2. 创建定时任务：开机后自动建立 SSH 反向隧道 ────────────────
$tunnelTaskName = "Quant-SSH-Tunnel"
$tunnelCommand = "ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -R 2222:localhost:22 -N ${LinuxUser}@${LinuxServer}"

$tunnelExists = Get-ScheduledTask -TaskName $tunnelTaskName -ErrorAction SilentlyContinue
if ($tunnelExists) {
    Unregister-ScheduledTask -TaskName $tunnelTaskName -Confirm:$false
    Write-Host "[INFO] 已移除旧的隧道任务"
}

$tunnelAction = New-ScheduledTaskAction -Execute "ssh.exe" `
    -Argument "-o StrictHostKeyChecking=no -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -R 2222:localhost:22 -N ${LinuxUser}@${LinuxServer}"
$tunnelTrigger = New-ScheduledTaskTrigger -AtStartup
$tunnelSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $tunnelTaskName -Action $tunnelAction `
    -Trigger $tunnelTrigger -Settings $tunnelSettings -Description "开机自动建立到 Linux 的反向 SSH 隧道" -Force `
    -RunLevel Highest -User "NT AUTHORITY\SYSTEM"
Write-Host "[OK] 隧道任务已创建：$tunnelTaskName" -ForegroundColor Green

# ── 3. 创建定时任务：每日 15:35 导出 QMT 持仓 ──────────────────
$exportTaskName = "Quant-Export-QMT"
$exportExists = Get-ScheduledTask -TaskName $exportTaskName -ErrorAction SilentlyContinue
if ($exportExists) {
    Unregister-ScheduledTask -TaskName $exportTaskName -Confirm:$false
}

$exportAction = New-ScheduledTaskAction -Execute "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" `
    -WorkingDirectory "$QuantDir" `
    -Argument "$QuantDir\scripts\export_qmt_positions.py"
$exportTrigger = New-ScheduledTaskTrigger -Daily -At "15:35"
Register-ScheduledTask -TaskName $exportTaskName -Action $exportAction `
    -Trigger $exportTrigger -Description "每日收盘后导出 QMT 持仓并推送到 Linux" -Force `
    -RunLevel Highest -User "NT AUTHORITY\SYSTEM"
Write-Host "[OK] QMT导出任务已创建：$exportTaskName (每日 15:35)" -ForegroundColor Green

# ── 4. 创建定时任务：调仓日 14:33 拉取信号并预检查（不执行） ──
$fetchTaskName = "Quant-Fetch-Signal"
$fetchExists = Get-ScheduledTask -TaskName $fetchTaskName -ErrorAction SilentlyContinue
if ($fetchExists) {
    Unregister-ScheduledTask -TaskName $fetchTaskName -Confirm:$false
}

$fetchAction = New-ScheduledTaskAction -Execute "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe" `
    -WorkingDirectory "$QuantDir" `
    -Argument "$QuantDir\scripts\fetch_and_execute.py --dry-run"
# 月中 + 月末附近的周五（调仓日附近），14:33 拉取信号预览
# 实际执行仍需人工 --execute，这里只做 dry-run 预检查
$fetchTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "14:33"
Register-ScheduledTask -TaskName $fetchTaskName -Action $fetchAction `
    -Trigger $fetchTrigger -Description "每日 14:33 拉取信号并 dry-run 预检查" -Force `
    -RunLevel Highest -User "NT AUTHORITY\SYSTEM"
Write-Host "[OK] 信号预检任务已创建：$fetchTaskName (周一至五 14:33)" -ForegroundColor Green

# ── 5. 立即启动隧道（本次） ─────────────────────────────────────
Write-Host ""
Write-Host "[INFO] 正在启动 SSH 隧道..." -ForegroundColor Yellow
Start-Process -FilePath "ssh.exe" -ArgumentList "-o StrictHostKeyChecking=no -o ServerAliveInterval=60 -R 2222:localhost:22 -N ${LinuxUser}@${LinuxServer}" -WindowStyle Hidden
Write-Host "[OK] SSH 隧道已在后台启动" -ForegroundColor Green

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  配置完成！三项定时任务 + 隧道已启动" -ForegroundColor Cyan
Write-Host "  下次开机后隧道自动建立，无需手动操作" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

# 列出现有任务
Write-Host ""
Write-Host "已配置的定时任务："
Get-ScheduledTask -TaskName "Quant-*" | Format-Table TaskName, State, Triggers
