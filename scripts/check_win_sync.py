"""Windows 代码同步检查 (2026-09-17, Git-Sync 断供 8 天实锤后建)。

每晚 22:10 (Git-Sync 21:00 之后) ssh 读 Windows git HEAD 与本地比较,
状态落盘 logs/win_sync_state.json; 连续 ≥2 天 verified 不一致时告警
(健康行 18:40 读前夜状态, 见 daily_health_line 的 win_sync 字段)。
ssh 失败=unknown, 不计入连续失败(隧道问题由 tunnel watchdog 负责)。
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from loguru import logger

WIN_HOST = "Administrator@127.0.0.1"
WIN_PORT = "2222"
SSH_KEY = "/root/.ssh/id_rsa"
STATE_FILE = Path("logs/win_sync_state.json")
COUNTER_FILE = Path("logs/win_sync_fail_count.json")
SSH_OPTS = ["-p", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]


def local_head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                       text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else "?"


def win_head() -> str | None:
    cmd = ["ssh"] + SSH_OPTS + [WIN_HOST, "git -C H:\\quant rev-parse HEAD"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=45)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception as e:
        logger.warning(f"ssh 读 Windows HEAD 失败: {e}")
        return None


def main():
    lh = local_head()
    wh = win_head()
    today = date.today().strftime("%Y-%m-%d")
    if wh is None:
        state = {"date": today, "linux_head": lh, "win_head": None,
                 "synced": "unknown", "error": "ssh不可达(隧道?)"}
        logger.warning(f"win sync 不可验证: {state['error']}")
    else:
        synced = lh == wh
        state = {"date": today, "linux_head": lh, "win_head": wh,
                 "synced": synced}
        logger.info(f"win_head={wh[:8]} linux_head={lh[:8]} "
                    f"synced={synced}")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    # 连续失败计数: 只数 verified 不一致; unknown 不计数
    cnt = {"consecutive": 0, "last_fail": None}
    if COUNTER_FILE.exists():
        try:
            cnt = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if state["synced"] is False:
        cnt["consecutive"] = int(cnt.get("consecutive", 0)) + 1
        cnt["last_fail"] = today
    else:
        cnt["consecutive"] = 0
    COUNTER_FILE.write_text(json.dumps(cnt, ensure_ascii=False, indent=1))

    if cnt["consecutive"] >= 2:
        try:
            from monitoring.alerts import p0_alert
            p0_alert("Windows 代码同步断供",
                     f"连续 {cnt['consecutive']} 天 win HEAD≠linux HEAD"
                     f" (win={state.get('win_head', '?')[:8]} "
                     f"linux={state.get('linux_head', '?')[:8]}), "
                     f"Windows 执行链在跑旧代码")
        except Exception as e:
            logger.error(f"告警发送失败: {e}")


if __name__ == "__main__":
    main()
