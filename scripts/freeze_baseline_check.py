"""冻结基线: 记录当前 HEAD + 执行路径文件 MD5 (10-19 开盘前对照用)。"""
import hashlib
import subprocess
from pathlib import Path

head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                      text=True, cwd="/root/quant").stdout.strip()
print("HEAD:", head)

files = [
    "scripts/fetch_and_execute.py",
    "execution/qmt_client.py",
    "execution/trader.py",
    "scripts/run_execute.bat",
]
for f in files:
    p = Path("/root/quant") / f
    if p.exists():
        h = hashlib.md5(p.read_bytes()).hexdigest()
        print(f"{f}: {h}")
    else:
        print(f"{f}: MISSING")
