"""
QMT_PATH自修复 — 从运行中的XtMiniQmt.exe反推正确的userdata_mini路径,
写回.env, 然后验证连接。全程Windows本地跑, 无中文跨SSH问题。
只输出ASCII。
"""
import os, sys, subprocess, time, json
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, "H:/quant")

ENV_FILE = r"H:\quant\.env"

# ── Step 1: 从运行的XtMiniQmt.exe找真实路径 ──
print("Step1: locate XtMiniQmt.exe...")
exe_path = None
try:
    import psutil
    for p in psutil.process_iter(['name', 'exe']):
        if p.info['name'] == 'XtMiniQmt.exe' and p.info['exe']:
            exe_path = p.info['exe']
            break
except ImportError:
    # fallback: wmic
    r = subprocess.run('wmic process where name="XtMiniQmt.exe" get ExecutablePath /format:list',
                       shell=True, capture_output=True, text=True, errors="replace")
    for line in r.stdout.splitlines():
        if 'ExecutablePath=' in line and 'QtMiniQmt' in line:
            exe_path = line.split('=', 1)[1].strip()
            break

if not exe_path:
    print("FATAL: XtMiniQmt.exe not found")
    sys.exit(1)

bin_dir = os.path.dirname(exe_path)
# bin.x64 的上级目录下有 userdata_mini
parent = os.path.dirname(bin_dir)
userdata_mini = os.path.join(parent, "userdata_mini")
print(f"bin:       {os.path.exists(bin_dir)}")
print(f"userdata:  {os.path.exists(userdata_mini)}")
print(f"PATH_OK:   {os.path.exists(userdata_mini)}")

if not os.path.exists(userdata_mini):
    print("FATAL: userdata_mini not found at derived path")
    sys.exit(1)

# ── Step 2: 写回.env ──
print("\nStep2: fix .env QMT_PATH...")
lines = open(ENV_FILE, 'r', encoding='utf-8').readlines()
fixed = False
for i, line in enumerate(lines):
    if line.startswith("QMT_PATH="):
        new_line = f"QMT_PATH={userdata_mini}\n"
        if lines[i] != new_line:
            lines[i] = new_line
            fixed = True
        break
if fixed:
    open(ENV_FILE, 'w', encoding='utf-8').writelines(lines)
    print("ENV_UPDATED=1")
else:
    print("ENV_UPDATED=0 (已正确)")

# ── Step 3: 验证 ──
print("\nStep3: test connection...")
os.environ["ENV"] = "simulation"
os.environ["QMT_PATH"] = userdata_mini
from xtquant import xttrader
from xtquant.xttype import StockAccount

# 读账户
acct = ""
for line in open(ENV_FILE, encoding='utf-8'):
    if line.startswith("QMT_ACCOUNT_ID="):
        acct = line.strip().split("=", 1)[1].strip()
        break

try:
    t = xttrader.XtQuantTrader(userdata_mini, int(time.time()) % 100000)
    t.start(); time.sleep(2)
    r = t.connect()
    print(f"connect_result={r}")
    if r == 0:
        t.subscribe(StockAccount(acct))
        time.sleep(1)
        pos = t.query_stock_positions(StockAccount(acct))
        print(f"CONN_OK positions={len(pos) if pos else 0}")
    else:
        print("CONN_FAILED result=" + str(r))
except Exception as e:
    print(f"CONN_ERROR: {str(e)[:200]}")
