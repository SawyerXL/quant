# 隧道 keepalive 任务安装器 v2.3 (2026-09-14)
# 变更: 任务身份 Administrator(仅交互登录) → SYSTEM(无论用户是否登录都运行)。
# 两次长静默根因(09-10/11 无人登录任务零执行、09-13晚~14晨 18.5h): 任务
# "只使用交互方式"——修复后自愈不再依赖有人登录 Windows。
# SYSTEM 私钥: C:\ProgramData\quant-tunnel\id_ed25519 (ACL 仅 SYSTEM,
# OpenSSH 严格权限检查兼容; Administrator 私钥 ACL 不动)。
# 注: Register-ScheduledTask 在实测中报 0x80041318, 改用 schtasks 经典
# 配方(2026-09-14 实测通过并完成断电自愈验证)。
$ErrorActionPreference = "Stop"
$keyDir = "C:\ProgramData\quant-tunnel"
$keyFile = Join-Path $keyDir "id_ed25519"
$srcKey = "C:\Users\Administrator\.ssh\id_ed25519"

# 1. 部署 SYSTEM 私钥副本(幂等: 已存在且 ACL 正确则跳过)
if (-not (Test-Path $keyFile)) {
    New-Item -ItemType Directory -Path $keyDir -Force | Out-Null
    Copy-Item $srcKey $keyFile -Force
    icacls $keyFile /inheritance:r /grant "SYSTEM:F" | Out-Null
    Write-Output "key deployed: $keyFile (ACL: SYSTEM only)"
} else {
    Write-Output "key exists, skip: $keyFile"
}

# 2. 重建任务(schtasks 配方, 2026-09-14 实测): SYSTEM + 每5分钟 + 后台运行
& schtasks.exe /create /tn Quant-TunnelKeep /f /sc minute /mo 5 /ru SYSTEM /rl HIGHEST `
    /tr "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File H:\quant\scripts\tunnel_keepalive.ps1"
Write-Output "task re-registered: Quant-TunnelKeep (SYSTEM, every 5 min, run whether logged on or not)"
