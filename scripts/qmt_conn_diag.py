"""
QMT连接诊断 — 深层排查connect=-1根因。
跳过ssh, 在Windows本地跑, 避免中文路径编码问题。
"""
import os, sys, glob, json, time
os.environ["ENV"] = "simulation"
sys.path.insert(0, "H:/quant")

# 1. 读QMT_PATH
env_f = r"H:\quant\.env"
qmt_path = ""
for line in open(env_f, encoding="utf-8"):
    if line.startswith("QMT_PATH="):
        qmt_path = line.strip().split("=", 1)[1].strip()
        break
print(f"QMT_PATH: {qmt_path}")
print(f"path存在: {os.path.exists(qmt_path)}")

# 2. 查lock/session/pid文件
ud = qmt_path
for pattern in ["*.lock", "*session*", "*.pid", "*.sock"]:
    for f in glob.glob(f"{ud}\\**\\{pattern}", recursive=True):
        print(f"  FOUND: {f}")
print(f"--- userdata_mini一级目录 ---")
if os.path.exists(ud):
    for d in os.listdir(ud):
        print(f"  {d}")
print()

# 3. 手工连接测试(用xtquant直连, 绕过QMTClient类, 看具体错误)
from xtquant import xtdata
try:
    xtdata.connect()
    print("xtdata.connect() = OK")
except Exception as e:
    print(f"xtdata.connect() FAIL: {e}")

# 4. 标准连接
try:
    from execution.qmt_client import get_client
    c = get_client()
    pos = c.get_positions()
    print(f"get_client() OK: {len(pos)} positions")
except Exception as e:
    print(f"get_client() FAIL: {e}")

# 5. 进程确认
import subprocess
r = subprocess.run("tasklist /fi \"imagename eq XtMiniQmt.exe\"", shell=True, capture_output=True, text=True, errors="replace")
print(f"\nXtMiniQmt.exe进程: {'在跑' if 'XtMiniQmt' in r.stdout else '未找到'}")
print(r.stdout.strip().split("\n")[-2] if r.stdout.strip() else "?")
