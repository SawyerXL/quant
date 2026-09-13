"""
每日健康行 (2026-09-13 用户批准立项, dead-man's switch 极性反转) —
四次"告警未读"事故(日线挂三天/MCP从未工作/ingestion回退三天/隧道断两天)
的共同点: 系统静默被解读为健康。本脚本每天固定时刻落盘一行健康行:
  最新 bar 日期 / 各数据源最后成功时间 / 隧道状态 / 五审计结果 /
  实盘持仓快照时间。
**缺失这条消息本身 = 告警**——由 p0_alerts 链检查本文件新鲜度实现
(健康行超过 26 小时未更新 → P0 升级)。
不消耗假设预算(基础设施可靠性, 非策略假设)。
建议 cron: 每天 18:10(与 p0 sweep 同链, 先于 sweep 3 分钟):
  7 18 * * * cd /root/quant && .venv/bin/python scripts/daily_health_line.py >> logs/cron_health_line.log 2>&1
"""
import sys, json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily

OUT = Path("logs/daily_health_line.json")


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 最新 bar 日期(上证指数)
    sh = load_daily("SH000001", "2026-01-01", "2026-12-31")
    latest_bar = str(sh["date"].max())[:10] if not sh.empty else "?"

    # 各数据源最后成功时间(从日志摘要: 日更完成行)
    update_log = Path("logs/cron_data_update.log")
    last_update = "?"
    if update_log.exists():
        lines = update_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in reversed(lines):
            if "数据更新完成" in line:
                last_update = line[:19]
                break

    # 隧道状态(2222 端口可达性)
    import socket
    tunnel = "down"
    try:
        s = socket.create_connection(("127.0.0.1", 2222), timeout=5)
        s.close()
        tunnel = "up"
    except Exception:
        pass

    # 实盘持仓快照时间(最新 positions 文件)
    snap_time = "?"
    for f in sorted(Path("logs").glob("*.json")):
        if "positions_latest" in f.name:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                snap_time = str(d.get("exported_at", "?"))[:19]
            except Exception:
                pass

    # 五审计结果(最新单位一致性日志尾部)
    audit = "?"
    uclog = Path("logs/unit_consistency_check.log")
    if uclog.exists():
        audit = "pass" if "单位一致性通过" in uclog.read_text(
            encoding="utf-8", errors="ignore") else "?"

    # 结论依赖追踪心跳(2026-09-13 晚): try/except 吞异常会让"追踪器死了"
    # 与"没有结论受影响"同形——心跳缺失/err 必须告警, 不等静默积累
    dep = {"dep_scan": "missing"}
    try:
        from conclusion_deps import health as cdeps_health
        dep = cdeps_health()
    except Exception as e:
        dep = {"dep_scan": "err", "last_error": f"conclusion_deps 导入失败: {e}"}

    # 单变量 harness 运行状态(2026-09-13 晚): 状态=error 才告警
    # (never_run 属正常——harness 按需运行不在日更链)
    sv = {"status": "never_run"}
    svp = Path("logs/single_var_harness_state.json")
    if svp.exists():
        try:
            sv = json.loads(svp.read_text(encoding="utf-8"))
        except Exception as e:
            sv = {"status": "error", "error": f"harness 状态不可读: {e}"}

    # bar 新鲜度: 交易日历感知(2026-09-13晚用户裁定)——分母=上一交易日,
    # 不是 26 小时。周末/节假日不产生告警
    tds = sorted({str(d)[:10] for d in sh["date"]}) if not sh.empty else []
    last_td = max((d for d in tds if d <= now[:10]), default="?")
    bar_fresh = latest_bar >= last_td if last_td != "?" else True

    alerts = []
    # bootstrap 是合法运行前的宽限态, 不告警; missing/err 才告警
    if dep.get("dep_scan") in ("missing", "err"):
        alerts.append(f"结论依赖追踪器异常: {dep.get('dep_scan')} "
                      f"{dep.get('last_error') or ''}")
    if sv.get("status") == "error":
        alerts.append(f"harness 上次运行失败: {sv.get('error', '')}")
    if now[:10] in tds and not bar_fresh:
        alerts.append(f"最新bar({latest_bar})落后于今日交易日{now[:10]}, "
                      f"日更未完成或失败")
    for msg in alerts:
        try:
            from monitoring.alerts import send_alert
            send_alert(msg, level="warning")
        except Exception:
            pass

    line = {"ts": now, "latest_bar": latest_bar,
            "last_trading_day": last_td, "bar_fresh": bar_fresh,
            "last_data_update": last_update, "tunnel": tunnel,
            "audit": audit, "positions_snapshot": snap_time,
            "dep_scan": dep, "sv_harness": sv, "alerts": alerts}
    OUT.write_text(json.dumps(line, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(line, ensure_ascii=False))


if __name__ == "__main__":
    main()
