# 止损监控 — 盘中每20分钟自动执行硬止损(-15%/-18%)。由计划任务 Quant-StopLoss 调用。
$py  = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
$dir = "H:\quant"
$env:PYTHONIOENCODING = "utf-8"
& $py "$dir\scripts\stop_monitor.py" --go *>> "$dir\logs\stop_monitor.log"
