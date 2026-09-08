# ================================================================
# Quant-Git-Sync 安装脚本（在 Windows 以管理员 PowerShell 运行一次）
# 2026-09-08: 隧道方向反转动议落地——代码同步改为 Windows 出站拉取,
#   不再依赖 2222 反向隧道。git remote "linux" 指向 Linux 公网
#   12201 端口(Windows→Linux 出站 SSH, keepalive 已验证此链路稳定)。
#   数据回传本就走 HTTP(不依赖隧道), 隧道降级为可选便利设施。
# 任务: Quant-Git-Sync, 每日 08:40(盘前) + 21:00(盘后)。
# 日志: H:\quant\logs\git_sync.log
# ================================================================

$QuantDir = "H:\quant"
$Git = "F:\Program Files\Git\cmd\git.exe"

# git 不存在则提示安装
if (-not (Test-Path $Git)) {
    $Git = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
}
if (-not $Git) {
    Write-Error "未找到 git.exe, 请先安装 Git for Windows"
    exit 1
}

# 确保 linux remote 存在(指向 Linux 公网 SSH 12201, 出站)
$linuxRemote = git -C $QuantDir remote get-url linux 2>$null
if (-not $linuxRemote) {
    git -C $QuantDir remote add linux "ssh://root@106.15.61.81:12201/root/quant"
    Write-Host "[OK] 已添加 linux remote" -ForegroundColor Green
}

$taskName = "Quant-Git-Sync"
$exists = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($exists) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c $Git -C $QuantDir pull linux main >> $QuantDir\logs\git_sync.log 2>&1"
$t1 = New-ScheduledTaskTrigger -Daily -At "08:40"
$t2 = New-ScheduledTaskTrigger -Daily -At "21:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger $t1, $t2 -Settings $settings `
    -User "Administrator" -RunLevel Highest `
    -Description "从Linux出站拉取代码(不依赖2222反向隧道)" -Force

Write-Host "[OK] 任务已创建: $taskName (每日 08:40 / 21:00)" -ForegroundColor Green

# 立即跑一次验证
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 8
$r = (Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
if ($r -eq 0) {
    Write-Host "[OK] 首次拉取成功" -ForegroundColor Green
} else {
    Write-Warning "首次拉取 LastTaskResult=$r, 请检查 H:\quant\logs\git_sync.log"
}
