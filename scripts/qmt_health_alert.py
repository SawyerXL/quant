"""
QMT终端连接健康检查 — 跑在 Linux, 是"全自动下单"的关键护栏。

为什么: fetch_and_execute 14:30 自动下单、qmt_snapshot 15:45 拉快照, 都要求 Windows 上
Matrix 终端处于登录状态。终端没登录/掉线时, 自动下单会静默失败(connect result=-1)。
本脚本盘前+调仓前 ssh 到 Windows 跑探针, 未连接就发邮件, 让人来得及登录。

三种结果:
  探针 OK        → 记日志, 不告警
  探针 FAIL      → 终端未登录 → 发邮件
  ssh 失败       → 隧道断/QMT机不可达 → 发邮件(快照+远程控制也全废)

Cron(交易日): 10 9 * * 1-5  和  25 14 * * 1-5
用法: python scripts/qmt_health_alert.py
"""
import subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from monitoring.alerts import send_alert

logger.add("logs/qmt_health_alert.log", rotation="7 days", retention="30 days")

WIN_PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
SSH = ["ssh", "-p", "2222", "-i", "/root/.ssh/id_rsa",
       "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=12"]
HOST = "Administrator@127.0.0.1"


def probe():
    """返回 (state, detail): state True=连上 / False=没连上 / None=ssh都不通。"""
    cmd = SSH + [HOST, f'"{WIN_PY}" H:\\quant\\scripts\\qmt_conn_probe.py']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except Exception as e:
        return None, f"ssh到Windows失败: {e}"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if "QMT_CONN_OK" in out:
        line = next((l for l in out.splitlines() if "QMT_CONN_OK" in l), "QMT_CONN_OK")
        return True, line.strip()
    if "QMT_CONN_FAIL" in out:
        line = next((l for l in out.splitlines() if "QMT_CONN_FAIL" in l), out)
        return False, line.strip()[:300]
    # 既没OK也没FAIL: 可能ssh通了但探针没跑起来(python路径/脚本缺失)
    return None, out[-300:] if out else "探针无输出"


def main():
    state, detail = probe()
    if state is True:
        logger.info(f"✅ QMT终端连接正常: {detail}")
    elif state is None:
        logger.error(f"🔴 QMT机不可达: {detail}")
        send_alert(f"【🔴 QMT机不可达】盘前/调仓前检查\n{detail}\n"
                   f"→ 反向隧道可能断, 自动下单+快照+远程控制都会失败, 请检查Windows隧道",
                   level="error")
    else:
        logger.error(f"🔴 QMT终端未连接: {detail}")
        send_alert(f"【🔴 QMT终端未登录】盘前/调仓前检查\n"
                   f"Matrix终端未登录 → 14:30自动下单会静默失败\n{detail}\n"
                   f"请在Windows QMT机登录申万宏源Matrix仿真终端",
                   level="error")


if __name__ == "__main__":
    main()
