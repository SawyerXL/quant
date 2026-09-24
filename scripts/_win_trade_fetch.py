"""Windows 当日订单拉取(含成交价) — 2026-09-24 超付机制调查
本地驱动: ssh 触发 dump_qmt_orders.py → ssh type 拉回 → 解析打印。
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
WIN_HOST = "Administrator@127.0.0.1"
WIN_PORT = "2222"
SSH_KEY = "/root/.ssh/id_rsa"
WIN_PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
SSH_OPTS = ["-p", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]


def ssh_run(cmd, timeout=120):
    r = subprocess.run(["ssh"] + SSH_OPTS + [WIN_HOST, cmd],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


rc, out = ssh_run(
    f'"{WIN_PY}" H:\\quant\\scripts\\dump_qmt_orders.py', timeout=180)
print("dump rc:", rc, out.strip()[-120:])
rc, out = ssh_run(
    'cmd /c "cd /d H:\\quant\\logs && type *rders_latest.json"', timeout=60)
d, _ = json.JSONDecoder().raw_decode(out)  # 尾部可能混入多文件拼接
print("orders:", d.get("order_count"))
for o in d.get("orders", []):
    print(f"{o['code']:8} {o['direction']:4} {o['shares']:>4}张 "
          f"限价{o['limit_price']:>9} 成交价{o['fill_price']:>9} "
          f"成交{o['filled']:>4} 状态{o['status']}")
