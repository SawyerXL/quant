"""
每日数据更新脚本。
Linux 服务器 cron 配置：
  0 17 * * 1-5 /path/to/venv/bin/python /path/to/quant/scripts/daily_data_update.py
（周一至周五 17:00，收盘后1.5小时确保数据稳定）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date, timedelta
from loguru import logger
from data.source import get_source
from data.storage import save_daily, save_meta, load_meta
from data.cleaner import validate_data_completeness
from monitoring.alerts import send_alert

logger.add("logs/data_update_{time:YYYY-MM-DD}.log", rotation="1 day", retention="30 days")


def update_today():
    today = date.today().strftime("%Y-%m-%d")
    src   = get_source()

    # 1. 确认是交易日
    calendar = src.get_trade_calendar()
    if today not in calendar:
        logger.info(f"{today} 非交易日，跳过")
        return

    logger.info(f"开始更新 {today} 数据")

    # 2. 更新股票基本信息（每周一更新，其他日跳过）
    if date.today().weekday() == 0:
        info = src.get_stock_info()
        if not info.empty:
            save_meta("stock_info", info)
            logger.info(f"更新股票基本信息: {len(info)} 只")

    # 3. 更新全市场日线（增量）
    stock_info = load_meta("stock_info")
    if stock_info.empty:
        logger.error("stock_info 为空，请先初始化基础数据")
        send_alert("数据更新失败：stock_info 为空，请检查", level="error")
        return

    codes = stock_info["code"].tolist()
    failed = []
    for i, code in enumerate(codes):
        df = src.get_daily(code, today, today)
        if df.empty:
            failed.append(code)
        else:
            save_daily(code, df)
        if (i + 1) % 500 == 0:
            logger.info(f"进度: {i+1}/{len(codes)}")

    # 4. 更新交易日历
    cal_df = __import__("pandas").DataFrame({"trade_date": calendar})
    save_meta("trade_calendar", cal_df)

    # 5. 推送日报
    msg = f"数据更新完成: {today}, 成功 {len(codes)-len(failed)}/{len(codes)}, 失败 {len(failed)} 只"
    logger.info(msg)
    send_alert(msg)

    if len(failed) > 50:
        send_alert(f"警告：失败股票数量异常 ({len(failed)} 只)，请检查数据源", level="warning")


if __name__ == "__main__":
    update_today()
