"""三连验·白昼采集 (2026-09-17, 系统 cron 15:30 迁移版)。

三件事只读只记不修, 写账本 overnight_open_questions.md:
  ① 白昼隧道翻转: cron_tunnel.log 今日 09:00~15:00
  ② 快照导出链唯一主证据: qmt_snapshot_state.json (今日 AND ok=True)
     + 产物 exported_at=今日 仅作参考(可能来自冗余链)
  ③ Windows 冗余导出健康(旁证): ExportDaily/Snapshot-1510 上次结果
用法: python scripts/daytime_harvest.py
"""
import json
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


def day_flips(today: str) -> list[str]:
    flips = []
    log = Path("logs/cron_tunnel.log")
    if not log.exists():
        return flips
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if today in ln and ("恢复" in ln or "断开" in ln):
            hh = ln[11:13] if len(ln) > 13 else ""
            if hh.isdigit() and 9 <= int(hh) < 15:
                flips.append(ln[:19])
    return flips


def win_task_result(task: str) -> str:
    cmd = ["ssh"] + SSH_OPTS + [WIN_HOST,
                                f"schtasks /query /tn \\Quant-{task} /fo LIST /v"]
    try:
        # Windows schtasks 输出为 GBK, ssh 透传原字节
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="gbk", errors="replace", timeout=30)
        raw = r.stdout
        last, res = "?", "?"
        for ln in raw.splitlines():
            if "上次运行时间" in ln:
                last = ln.split(":", 1)[1].strip()
            elif "上次结果" in ln:
                res = ln.split(":", 1)[1].strip()
        return f"{last} res={res}"
    except Exception as e:
        return f"查询失败: {e}"


def main():
    today = date.today().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%H:%M:%S")
    flips = day_flips(today)
    state = {}
    sp = Path("logs/qmt_snapshot_state.json")
    if sp.exists():
        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            state = {"error": "状态文件不可读"}
    # 2026-09-20 修复: 非交易日(周末)15:05 链不跑——判据基准改
    # 最近交易日(今天=交易日时即今天), 否则周末恒报假"主证据不过"
    ref_day = today
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        _sys.path.insert(0, str(Path(__file__).parent))
        from data.storage import load_meta as _lm
        _cal = sorted(_lm("trade_calendar")["trade_date"].astype(str).tolist())
        _past = [d for d in _cal if d <= today]
        if _past:
            ref_day = _past[-1]
    except Exception:
        pass
    snap_ok = (state.get("date") == ref_day and state.get("ok") is True)
    exported = "?"
    lp = Path("logs/qmt_positions_latest.json")
    if lp.exists():
        try:
            exported = str(json.loads(lp.read_text(encoding="utf-8"))
                           .get("exported_at", "?"))
        except Exception:
            pass
    exp_daily = win_task_result("ExportDaily")
    exp_snap = win_task_result("Snapshot-1510")

    verdict = ("导出链通过" if snap_ok else
               f"导出链主证据不过: state={state}")
    rec = (f"\n## 白昼采集（{today} {ts}, 系统cron）\n"
           f"①白昼翻转 {len(flips)} 次 {flips if flips else '✓ 零翻转'}\n"
           f"②快照主证据: {verdict}; 产物 exported_at={exported}(参考)\n"
           f"③冗余旁证: ExportDaily[{exp_daily}] Snapshot-1510[{exp_snap}]\n")
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(rec)
    print(rec)
    if not snap_ok:
        try:
            from monitoring.alerts import send_alert
            send_alert(f"[白昼采集] 快照导出链主证据不过: {state}",
                       level="warning")
        except Exception as e:
            logger.error(f"告警发送失败: {e}")


if __name__ == "__main__":
    main()
