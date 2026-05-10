"""
一次性历史数据初始化脚本（用 Akshare，不消耗 MCP 配额）。

使用方式：
    python scripts/init_history.py --start 2019-01-01 --end 2024-12-31
    python scripts/init_history.py --start 2019-01-01          # end 默认今天
    python scripts/init_history.py --start 2019-01-01 --workers 10

预计耗时（5年全A股约5000只）：
    单线程：4-8小时
    5个并发：1-3小时（受 Akshare 限流影响）

建议在云服务器上跑，本地可以先用 --limit 100 验证流程：
    python scripts/init_history.py --start 2019-01-01 --limit 100
"""
import sys
import argparse
import time
from pathlib import Path
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from data.source.akshare_source import AkshareSource   # 强制用 Akshare，不读 .env
from data.storage import save_daily, save_meta, save_financial
from monitoring.alerts import send_alert

logger.add(
    "logs/init_history_{time:YYYY-MM-DD}.log",
    rotation="1 day", retention="7 days", level="INFO"
)


def get_all_stock_codes(src: AkshareSource) -> list[str]:
    """获取全市场股票代码列表。"""
    info = src.get_stock_info()
    if info.empty:
        logger.error("获取股票列表失败")
        return []
    codes = info["code"].tolist()
    logger.info(f"全市场股票数量：{len(codes)} 只")
    return codes


def download_one(src: AkshareSource, code: str, start: str, end: str) -> tuple[str, bool, str]:
    """下载单只股票日线数据，返回 (code, 是否成功, 错误信息)。"""
    try:
        df = src.get_daily(code, start, end)
        if df.empty:
            return code, False, "数据为空（可能已退市或停牌超限）"
        save_daily(code, df)
        return code, True, ""
    except Exception as e:
        return code, False, str(e)


def download_financial_one(src: AkshareSource, code: str) -> tuple[str, bool]:
    """下载单只股票财务数据。"""
    try:
        df = src.get_financial(code)
        if not df.empty:
            save_financial(code, df)
        return code, True
    except Exception:
        return code, False


def init_daily(src: AkshareSource, codes: list[str], start: str, end: str, workers: int):
    """并发下载全市场日线数据。"""
    total = len(codes)
    success, failed = 0, []

    logger.info(f"开始下载日线行情：{total} 只股票，{start} → {end}，{workers} 个并发")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download_one, src, c, start, end): c for c in codes}
        for i, future in enumerate(as_completed(futures)):
            code, ok, err = future.result()
            if ok:
                success += 1
            else:
                failed.append(code)
                if err and "数据为空" not in err:
                    logger.warning(f"  {code} 失败: {err}")

            # 进度汇报
            if (i + 1) % 200 == 0 or (i + 1) == total:
                pct = (i + 1) / total * 100
                logger.info(f"进度 {i+1}/{total} ({pct:.1f}%)  成功:{success}  失败:{len(failed)}")

            # 简单限速：Akshare 免费接口约 120 次/分钟
            # 每个 worker 自带延迟，此处不额外 sleep

    return success, failed


def init_trade_calendar(src: AkshareSource, start: str, end: str):
    """下载并存储交易日历。"""
    logger.info("下载交易日历...")
    import pandas as pd
    calendar = src.get_trade_calendar(start=start, end=end)
    if calendar:
        df = pd.DataFrame({"trade_date": calendar})
        save_meta("trade_calendar", df)
        logger.info(f"交易日历：{len(calendar)} 个交易日")
    else:
        logger.error("交易日历下载失败")


def init_stock_info(src: AkshareSource):
    """下载并存储股票基本信息。"""
    logger.info("下载股票基本信息...")
    info = src.get_stock_info()
    if not info.empty:
        save_meta("stock_info", info)
        logger.info(f"股票基本信息：{len(info)} 只")
    else:
        logger.error("股票基本信息下载失败")


def main():
    parser = argparse.ArgumentParser(description="历史数据初始化（使用 Akshare）")
    parser.add_argument("--start",   default="2019-01-01", help="开始日期")
    parser.add_argument("--end",     default=date.today().strftime("%Y-%m-%d"), help="结束日期")
    parser.add_argument("--workers", type=int, default=5, help="并发线程数（建议3-8）")
    parser.add_argument("--limit",   type=int, default=0,  help="仅下载前N只（测试用）")
    parser.add_argument("--skip-financial", action="store_true", help="跳过财务数据下载")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info(f"初始化开始: {args.start} → {args.end}")
    logger.info(f"并发数: {args.workers}, 测试限制: {args.limit or '无'}")
    logger.info("=" * 60)

    src = AkshareSource()
    t0 = time.time()

    # 1. 交易日历 + 基本信息
    init_trade_calendar(src, args.start, args.end)
    init_stock_info(src)

    # 2. 获取股票列表
    codes = get_all_stock_codes(src)
    if not codes:
        logger.error("股票列表为空，退出")
        sys.exit(1)
    if args.limit:
        codes = codes[:args.limit]
        logger.info(f"测试模式：只处理前 {args.limit} 只")

    # 3. 日线行情（主体任务）
    logger.info("-" * 40)
    success, failed = init_daily(src, codes, args.start, args.end, args.workers)

    elapsed = (time.time() - t0) / 60
    summary = (
        f"日线下载完成：成功 {success}/{len(codes)} 只，"
        f"失败 {len(failed)} 只，耗时 {elapsed:.1f} 分钟"
    )
    logger.info(summary)

    if failed:
        failed_file = Path("logs/failed_codes.txt")
        failed_file.write_text("\n".join(failed))
        logger.info(f"失败列表已写入 {failed_file}")

    # 4. 财务数据（可选，耗时更长）
    if not args.skip_financial:
        logger.info("-" * 40)
        logger.info("开始下载财务数据（可用 --skip-financial 跳过）...")
        fin_ok, fin_fail = 0, 0
        with ThreadPoolExecutor(max_workers=3) as executor:  # 财务接口更容易限流，用3个并发
            futures = {executor.submit(download_financial_one, src, c): c for c in codes}
            for i, future in enumerate(as_completed(futures)):
                code, ok = future.result()
                if ok:
                    fin_ok += 1
                else:
                    fin_fail += 1
                if (i + 1) % 200 == 0:
                    logger.info(f"财务进度 {i+1}/{len(codes)}  成功:{fin_ok}")
        logger.info(f"财务数据完成：成功 {fin_ok}，失败 {fin_fail}")

    total_elapsed = (time.time() - t0) / 60
    final_msg = f"初始化全部完成，总耗时 {total_elapsed:.1f} 分钟"
    logger.info(final_msg)
    send_alert(final_msg)


if __name__ == "__main__":
    main()
