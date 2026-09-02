"""
隧道看门狗（2026-09-02）—— Linux 侧监控反向隧道(2222)可用性并告警。

Windows 端 Quant-TunnelKeep 计划任务每5分钟端到端自愈(实测杀进程→
自动恢复全链路OK), 但自愈窗口内(≤5分钟)以及 Windows 重启未登录期,
Linux 侧无人知晓隧道断了。本脚本补上可见性:
  - 隧道不通 → 企业微信告警一次(状态翻转才告警, 不刷屏)
  - 隧道恢复 → 记录+告警恢复
状态持久化在 logs/tunnel_state.json。
crontab: */5 与 Windows 任务错开2分钟(如 2,7,12...分), 避免同时抢连接。
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from monitoring.alerts import send_alert

STATE_FILE = Path("logs/tunnel_state.json")
PROBE_CMD = ["ssh", "-i", "~/.ssh/id_rsa", "-p", "2222",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=no",
             "Administrator@127.0.0.1", "echo TUNNEL_OK"]


def _load_state() -> str:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("state", "unknown")
        except Exception:
            pass
    return "unknown"


def _save_state(state: str):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"state": state, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        indent=2), encoding="utf-8")


def main():
    prev = _load_state()
    up = False
    try:
        r = subprocess.run(PROBE_CMD, capture_output=True, text=True, timeout=15)
        up = "TUNNEL_OK" in (r.stdout or "")
    except Exception:
        up = False

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if up:
        if prev != "up":
            _save_state("up")
            logger.info(f"隧道恢复: {now}")
            send_alert(f"🟢 Windows隧道已恢复 ({now})", level="info")
    else:
        if prev != "down":
            _save_state("down")
            logger.warning(f"隧道断开: {now}（Windows端计划任务将在≤5分钟内自愈）")
            send_alert(f"🔴 Windows隧道断开 ({now})，QMT持仓同步/执行链受影响。"
                       f"Windows端Quant-TunnelKeep应≤5分钟自愈，"
                       f"若超15分钟未恢复请检查Windows。", level="error")


if __name__ == "__main__":
    main()
