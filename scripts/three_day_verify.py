"""三连验 第2/3天 自动验证 (2026-09-17, cron 可靠性立项后从会话cron迁移)。

会话 cron 今天两次漏触发(10:05/18:50)——无人值守必须落到系统 cron。
本脚本在系统 cron 08:00 跑, 只读取证三件:
  ① tcp 127.0.0.1:2222 可连
  ② QMT 进程存活(ssh tasklist XtMiniQmt)
  ③ 昨夜 20:00~08:00 cron_tunnel.log 翻转数(应为零)
写账本 overnight_open_questions.md + 发邮件。判定: 三过=连续第N天
通过(第3天=睡眠修复最终检验, 关故障单); 任一失败=该环节标
"每日人工前置", 第3天故障单不关。
用法: python scripts/three_day_verify.py --day 2
"""
import argparse
import json
import socket
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

WIN_HOST = "Administrator@127.0.0.1"
WIN_PORT = "2222"
SSH_KEY = "/root/.ssh/id_rsa"
SSH_OPTS = ["-p", WIN_PORT, "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10"]
LEDGER = Path("logs/overnight_open_questions.md")


def tcp_ok() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(WIN_PORT)), timeout=8):
            return True
    except OSError:
        return False


def qmt_alive() -> bool:
    cmd = ["ssh"] + SSH_OPTS + [WIN_HOST, "tasklist | findstr /i XtMiniQmt"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return r.returncode == 0 and "XtMiniQmt" in r.stdout
    except Exception as e:
        logger.warning(f"QMT 存活查询失败: {e}")
        return False


def overnight_flips(today: str) -> list[str]:
    """昨夜 20:00~今晨 08:00 的断/连记录行。"""
    from datetime import timedelta
    flips = []
    log = Path("logs/cron_tunnel.log")
    if not log.exists():
        return flips
    yday = (date.fromisoformat(today) - timedelta(days=1)).strftime("%Y-%m-%d")
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        for d in (today, yday):
            if d in ln and ("恢复" in ln or "断开" in ln):
                hh = ln[11:13] if len(ln) > 13 else ""
                if hh.isdigit() and (int(hh) >= 20 or int(hh) < 8):
                    flips.append(ln[:19])
    return flips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", type=int, required=True, choices=(2, 3))
    args = ap.parse_args()
    today = date.today().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%H:%M:%S")

    ok_tcp, ok_qmt = tcp_ok(), qmt_alive()
    flips = overnight_flips(today)
    # 昨夜=前一日20:00起: 上一交易日日期可能跨周末, 用 day-1 近似+全量过滤
    ok_night = len(flips) == 0

    manual = []
    if not ok_tcp:
        manual.append("tcp(隧道不通)")
    if not ok_qmt:
        manual.append("QMT进程(不可查或未存活)")
    if not ok_night:
        manual.append(f"昨夜翻转{len(flips)}次")

    if args.day == 3 and not manual:
        verdict = ("三连验第 3 天全过 → **睡眠修复最终检验通过, "
                   "睡眠故障单关闭**")
    elif not manual:
        verdict = f"三连验第 {args.day} 天全过 → 无人值守链路连续第 {args.day} 天成立"
    else:
        verdict = (f"三连验第 {args.day} 天: 需人工环节 = {', '.join(manual)} "
                   "→ 显式记为'每日人工前置', "
                   + ("故障单不关" if args.day == 3 else ""))

    rec = (f"\n## 三连验第 {args.day} 天自动执行（{today} {ts}, 系统cron）\n"
           f"①tcp={'过' if ok_tcp else '败'} ②QMT={'活' if ok_qmt else '败'} "
           f"③昨夜翻转={len(flips)}次{' ✓' if ok_night else ''}\n"
           f"判定: {verdict}\n")
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(rec)
    print(rec)
    try:
        from monitoring.alerts import send_alert
        send_alert(f"[三连验D{args.day}] {verdict}", level="warning")
    except Exception as e:
        logger.error(f"告警发送失败: {e}")


if __name__ == "__main__":
    main()
