"""
Track A 每日信号生成脚本（月末才真正输出新调仓信号）。
Linux 服务器 cron：
  50 14 * * 1-5 /path/to/venv/bin/python /path/to/quant/scripts/daily_signal_a.py
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date
from loguru import logger
from data.source import get_source
from data.storage import load_meta
from strategies.multi_factor import MultiFactor
from monitoring.alerts import send_alert

logger.add("logs/signal_a_{time:YYYY-MM-DD}.log", rotation="1 day", retention="60 days")

SIGNAL_FILE = Path(__file__).parent.parent / "data_store" / "meta" / "signal_a_latest.json"


def is_rebalance_day(today: str, calendar: list[str]) -> bool:
    """判断今日是否为月末最后一个交易日。"""
    future = [d for d in calendar if d > today]
    if not future:
        return False
    next_td = future[0]
    return today[:7] != next_td[:7]   # 月份不同说明今天是本月最后交易日


def run():
    today    = date.today().strftime("%Y-%m-%d")
    src      = get_source()
    calendar = src.get_trade_calendar()

    if today not in calendar:
        logger.info(f"{today} 非交易日")
        return

    if not is_rebalance_day(today, calendar):
        logger.info(f"{today} 非调仓日，跳过 Track A 信号生成")
        return

    logger.info(f"[Track A] 月末调仓日: {today}，开始生成信号")

    strategy = MultiFactor()
    codes    = strategy.generate_signal(today)

    if not codes:
        send_alert(f"[Track A] {today} 信号生成失败，请人工检查", level="error")
        return

    weights  = strategy.compute_weights(codes)
    signal   = {
        "date":    today,
        "codes":   codes,
        "weights": weights,
        "execute_date": _next_first_trade_day(today, calendar),
    }

    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(json.dumps(signal, ensure_ascii=False, indent=2))

    msg = (
        f"[Track A] 信号生成完成\n"
        f"日期: {today} → 执行日: {signal['execute_date']}\n"
        f"持仓: {len(codes)} 只\n"
        f"前5: {', '.join(codes[:5])}"
    )
    logger.info(msg)
    send_alert(msg)


def _next_first_trade_day(today: str, calendar: list[str]) -> str:
    """返回下月第一个交易日。"""
    future = [d for d in calendar if d > today]
    return future[0] if future else today


if __name__ == "__main__":
    run()
