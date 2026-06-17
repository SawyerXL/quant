# QMT持仓自动推送 — 每30分钟运行，不依赖持久隧道
# 用法: powershell -ExecutionPolicy Bypass -File H:\quant\scripts\push_positions.ps1

$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$dir = "H:\quant"
$server = "47.116.166.139"
$user = "root"

# 1. 导出行情
Write-Host "[$(Get-Date -Format HH:mm)] Exporting QMT positions..."
& $py "$dir\scripts\export_qmt_positions.py" --no-push

# 2. SCP推送到Linux (每次独立的短连接，不依赖隧道)
$src = "$dir\logs\qmt_positions_latest.json"
if (Test-Path $src) {
    scp -o StrictHostKeyChecking=no -o ConnectTimeout=8 $src ${user}@${server}:/root/quant/logs/qmt_positions_latest.json
    Write-Host "[$(Get-Date -Format HH:mm)] Pushed to Linux"
} else {
    Write-Host "[$(Get-Date -Format HH:mm)] Export file not found"
}
