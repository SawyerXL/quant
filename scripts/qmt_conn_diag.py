"""
QMT连接诊断 — 深层排查connect=-1根因。
跳过ssh, 在Windows本地跑, 避免中文路径编码问题。
"""
import os, sys, glob, json, time
sys.stdout.reconfigure(encoding='utf-8')
os.environ["ENV"] = "simulation"
sys.path.insert(0, "H:/quant")

# 1. 读QMT_PATH
env_f = r"H:\quant\.env"
qmt_path = ""
for line in open(env_f, encoding="utf-8"):
    if line.startswith("QMT_PATH="):
        qmt_path = line.strip().split("=", 1)[1].strip()
        break
print(f"QMT_PATH(.env): {qmt_path}")
print(f".env路径存在: {os.path.exists(qmt_path)}")
# 同时解析.env里的QMT_PATH再拼 datadir
if qmt_path:
    alt1 = os.path.join(qmt_path, "datadir")
    alt2 = qmt_path.replace("userdata_mini", "bin.x64")
    print(f"  datadir存在: {os.path.exists(alt1)}")
    print(f"  bin.x64存在: {os.path.exists(alt2)}")

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

# 4. 用xtdata查miniQMT实际datadir+账户
try:
    from xtquant import xtdata
    info = xtdata.get_service_info()
    print(f"xtdata service_info: {info}")
except Exception as e:
    print(f"xtdata get_service_info FAIL: {e}")

# 5. XtQuantTrader用不同路径/参数组合试
from xtquant import xttrader
from xtquant.xttype import StockAccount
QMT_ACCOUNT_ID = ""
for line in open(env_f, encoding="utf-8"):
    if line.startswith("QMT_ACCOUNT_ID="):
        QMT_ACCOUNT_ID = line.strip().split("=", 1)[1].strip()
        break

# 尝试1: .env的QMT_PATH
print(f"\n--- 尝试1: .env QMT_PATH ---")
try:
    t = xttrader.XtQuantTrader(qmt_path, int(time.time())%100000)
    t.start(); time.sleep(2)
    r = t.connect()
    print(f"  结果: connect()={r}")
except Exception as e:
    print(f"  FAIL: {e}")

# 尝试2: .env QMT_PATH + '/datadir' (xtdata汇报过datadir路径)
if qmt_path:
    try2 = qmt_path + "/datadir"
    print(f"\n--- 尝试2: {try2} ---")
    try:
        t2 = xttrader.XtQuantTrader(try2, int(time.time())%100000)
        t2.start(); time.sleep(2)
        r2 = t2.connect()
        print(f"  结果: connect()={r2}")
    except Exception as e:
        print(f"  FAIL: {e}")

# 6. 标准连接(用默认get_client, 对比)
try:
    from execution.qmt_client import get_client
    c = get_client()
    pos = c.get_positions()
    print(f"\nget_client() OK: {len(pos)} positions")
except Exception as e:
    print(f"\nget_client() FAIL: {e}")

# 5. 进程确认
import subprocess
r = subprocess.run("tasklist /fi \"imagename eq XtMiniQmt.exe\"", shell=True, capture_output=True, text=True, errors="replace")
print(f"\nXtMiniQmt.exe进程: {'在跑' if 'XtMiniQmt' in r.stdout else '未找到'}")
print(r.stdout.strip().split("\n")[-2] if r.stdout.strip() else "?")
