"""
P0 告警状态管理（2026-09-06 建，告警分级的第一层落地）
用法:
    python scripts/p0_alerts.py --list               # 列出未确认的 P0
    python scripts/p0_alerts.py --ack 隧道断          # 人工确认某一类
    python scripts/p0_alerts.py --ack all             # 全部确认
    python scripts/p0_alerts.py --sweep               # 升级清扫(cron 18:10)

分级规则(2026-09-06 用户定): 数据缺口/隧道断/质量异常/执行失败=P0
(必须响应); 其余降级或聚合日报。P0 连续 2 天未确认 → 升级重发。
静跑期没有真金白银, 是校准告警灵敏度的最佳窗口——两个月后带钱跑时
再发现"告警会被忽略"就晚了。
P0 通道公信力纪律: 只承载真实事件, 测试一律 --dry-run 或 [TEST] 前缀,
零假警报——发过一次假警报, 人的响应阈值永久性升高。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from loguru import logger
from monitoring.alerts import p0_list, p0_ack, P0_ESCALATE_DAYS, send_alert


def _days_since(s: str) -> float:
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - dt).total_seconds() / 86400
    except Exception:
        return 0.0


def cmd_list():
    state = p0_list()
    if not state:
        print("当前无未确认 P0 ✅")
        return
    print(f"{'类型':<12}{'首见':<20}{'末见':<20}{'持续(天)':>8}  详情")
    for t, e in state.items():
        print(f"{t:<12}{e.get('first_seen','?'):<20}{e.get('last_seen','?'):<20}"
              f"{_days_since(e.get('first_seen','')):>8.1f}  {e.get('detail','')}")


def cmd_sweep(dry_run: bool = False):
    """升级清扫: open 状态超过阈值天数的 P0 重发告警(带未确认天数前缀)。"""
    state = p0_list()
    if not state:
        logger.info("无 P0, 清扫无事")
        return
    n = 0
    for t, e in state.items():
        days = _days_since(e.get("first_seen", ""))
        if days >= P0_ESCALATE_DAYS:
            msg = (f"[P0未确认×{int(days)}天] {t} 已持续 {int(days)} 天未确认"
                   f" — 请立即处理(确认: python scripts/p0_alerts.py --ack {t})")
            if dry_run:
                print(f"[dry-run] 将发送: {msg}")
            else:
                send_alert(msg, level="error")
            n += 1
    logger.info(f"升级重发 {n} 条 P0" if n else f"无超阈值 P0({len(state)} 条 open)")


def main():
    parser = argparse.ArgumentParser(description="P0 告警状态管理")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只打印将发送的内容, 不发信")
    parser.add_argument("--ack", metavar="TYPE|all")
    args = parser.parse_args()

    if args.ack:
        n = p0_ack(None if args.ack == "all" else args.ack)
        print(f"已确认 {n} 条 P0")
    elif args.sweep:
        cmd_sweep(dry_run=args.dry_run)
    else:
        cmd_list()


if __name__ == "__main__":
    main()
