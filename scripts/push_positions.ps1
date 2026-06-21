# QMT持仓自动推送 — 每30分钟运行，由Python脚本处理SCP(带SSH密钥)
# 用法: powershell -ExecutionPolicy Bypass -File H:\quant\scripts\push_positions.ps1

$py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$dir = "H:\quant"

Write-Host "[$(Get-Date -Format HH:mm)] Exporting + pushing to Linux..."
& $py "$dir\scripts\export_qmt_positions.py"
Write-Host "[$(Get-Date -Format HH:mm)] Done"
