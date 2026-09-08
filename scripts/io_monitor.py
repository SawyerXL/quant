"""IO/内存压力监控 — 每5分钟 cron 跑一次, 越阈值发告警。

2026-09-08 事故: 10:16 majflt/s≈6.4万(无swap颠簸)+iowait45% → udevd SIGABRT/
journald 被杀; 21:27 系统静默冻结 → 22:07 强启。等 SSH 连不上才发现已经晚了
—— 必须提前几分钟告警, 而不是事后看 sar。

数据源全部 /proc, 无外部依赖, 单次运行自身 IO≈0:
  /proc/diskstats  vdb/vda3 的 io_ticks 增量 → %util
  /proc/pressure   PSI 内存压力(10秒窗)
  /proc/meminfo    MemAvailable 占比
阈值策略: 单次超阈值只记不报(可能是一瞬间), 连续2次(≈10分钟)才发邮件,
同指标30分钟冷却防轰炸。cron: 3-58/5 * * * *
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

STATE = Path("/tmp/io_monitor_state.json")
WATCH_DEVS = ["vdb", "vda3"]
UTIL_THRESH = 90      # %util 连续2次>90% 告警
PSI_THRESH = 70       # 内存PSI full avg10 >70 连续2次告警
MEM_MIN_PCT = 5       # MemAvailable <5% 连续2次告警
COOLDOWN_MIN = 30     # 同指标告警冷却


def _read_disk_ticks() -> dict:
    """/proc/diskstats 第10字段 io_ticks(ms)。"""
    out = {}
    for line in Path("/proc/diskstats").read_text().splitlines():
        f = line.split()
        if len(f) >= 11 and f[2] in WATCH_DEVS:
            out[f[2]] = int(f[9]) + int(f[10])  # 读+写 ticks(按行计数)
    return out


def _read_psi_mem() -> float | None:
    try:
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            if line.startswith("full"):
                for part in line.split():
                    if part.startswith("avg10="):
                        return float(part.split("=")[1])
    except FileNotFoundError:
        pass
    return None


def _read_mem_avail_pct() -> float:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.strip().split()[0])
    return info["MemAvailable"] / info["MemTotal"] * 100


def _state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def _save_state(s: dict):
    STATE.write_text(json.dumps(s))


def _alert_once(s: dict, key: str, now: float, msg: str, level: str = "warning"):
    """同指标30分钟冷却。"""
    if now - s.get(f"last_{key}", 0) > COOLDOWN_MIN * 60:
        from monitoring.alerts import send_alert
        send_alert(msg, level=level)
        s[f"last_{key}"] = now
        return True
    return False


def main():
    import time
    now = time.time()
    s = _state()

    # ---- 磁盘 %util ----
    ticks = _read_disk_ticks()
    prev = s.get("ticks")
    s["ticks"] = ticks
    if prev and ticks:
        elapsed = now - s.get("ticks_ts", now)
        if elapsed > 30:  # 至少30秒采样窗, 否则噪声太大
            for dev in WATCH_DEVS:
                delta = ticks.get(dev, 0) - prev.get(dev, 0)
                util = delta / (elapsed * 10) * 100  # ms/ms → %
                s.setdefault(f"{dev}_hi", 0)
                if util > UTIL_THRESH:
                    s[f"{dev}_hi"] += 1
                    if s[f"{dev}_hi"] >= 2:
                        _alert_once(s, f"util_{dev}", now,
                                    f"IO饱和告警: {dev} %util={util:.0f}% 连续{s[f'{dev}_hi']}次>"
                                    f"{UTIL_THRESH}%(9/8教训: IO饱和会冻死SSH, 快查谁在写盘)")
                else:
                    s[f"{dev}_hi"] = 0
    s["ticks_ts"] = now

    # ---- 内存压力(PSI + MemAvailable) ----
    psi = _read_psi_mem()
    mem_pct = _read_mem_avail_pct()
    s.setdefault("psi_hi", 0)
    s.setdefault("mem_hi", 0)
    if psi is not None and psi > PSI_THRESH:
        s["psi_hi"] += 1
        if s["psi_hi"] >= 2:
            _alert_once(s, "psi", now,
                        f"内存压力告警: PSI full avg10={psi:.0f}% 连续{s['psi_hi']}次>"
                        f"{PSI_THRESH}%(9/8教训: 无swap颠簸majflt 6.4万/s杀死了udevd)")
    else:
        s["psi_hi"] = 0
    if mem_pct < MEM_MIN_PCT:
        s["mem_hi"] += 1
        if s["mem_hi"] >= 2:
            _alert_once(s, "mem", now,
                        f"内存告警: MemAvailable仅{mem_pct:.1f}% 连续{s['mem_hi']}次<"
                        f"{MEM_MIN_PCT}%, 即将触发OOM")
    else:
        s["mem_hi"] = 0

    _save_state(s)
    # 只在自己超阈值时写日志, 平时静默(避免cron log无限膨胀)
    hi = {k: v for k, v in s.items() if k.endswith("_hi") and v > 0}
    if hi:
        logger.warning(f"io_monitor: 压力量: {hi}")


if __name__ == "__main__":
    main()
