"""冻结基线检查(2026-10-10 建, 2026-10-18 改为 blob 哈希为主)。

输出 Linux 侧六文件: git blob 哈希(跨平台可比) + MD5(仅参考, CRLF 会不同)。
Windows 侧对照命令:
  git -C H:\\quant status --porcelain
  git -C H:\\quant ls-files -s execution/ scripts/fetch_and_execute.py scripts/run_execute.bat scripts/cb_signal.py scripts/build_cb_data.py
基线(10-16 定稿, blob 哈希):
  qmt_client 9a466ccb / trader a527238c / fetch_and_execute d5f9a4e6 /
  run_execute.bat b1c9492f / cb_signal 83b018d1 / build_cb_data 95faa5c8
"""
import hashlib
import subprocess
from pathlib import Path

REPO = "/root/quant"
FILES = [
    "execution/qmt_client.py",
    "execution/trader.py",
    "scripts/fetch_and_execute.py",
    "scripts/run_execute.bat",
    "scripts/cb_signal.py",
    "scripts/build_cb_data.py",
]

head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                      text=True, cwd=REPO).stdout.strip()
print("HEAD:", head)
out = subprocess.run(["git", "ls-files", "-s"] + FILES,
                     capture_output=True, text=True, cwd=REPO).stdout
print("git blob 哈希(跨平台可比):")
print(out.strip())
print("MD5(仅 Linux 参考, 跨平台因 CRLF 不可比):")
for f in FILES:
    p = Path(REPO) / f
    if p.exists():
        print(f"  {hashlib.md5(p.read_bytes()).hexdigest()}  {f}")
