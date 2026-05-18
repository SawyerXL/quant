"""
一次性历史数据初始化脚本。

数据源选择策略：
  - 股票列表：Akshare（本地）/ MCP FinQuery（服务器）
  - 日线行情：优先 MCP（服务器兼容），回退 Akshare（本地）
  - 东方财富封锁云服务器IP，云上必须用 MCP

使用方式：
    python scripts/init_history.py --start 2019-01-01
    python scripts/init_history.py --start 2019-01-01 --workers 5 --limit 100
"""
import sys
import argparse
import time
from pathlib import Path
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from data.storage import save_daily, save_meta, load_meta
from monitoring.alerts import send_alert

logger.add(
    "logs/init_history_{time:YYYY-MM-DD}.log",
    rotation="1 day", retention="7 days", level="INFO"
)


def _make_source():
    """
    自动选择数据源：MCP 优先（服务器上有效），Akshare 兜底（本地开发用）。
    """
    import os
    from dotenv import load_dotenv
    load_dotenv()

    # 先用 MCP 测一个简单请求，看能否连通
    try:
        from data.source.mcp_source import MCPSource
        src = MCPSource()
        cal = src.get_trade_calendar(start="2024-01-01", end="2024-01-05")
        if cal:
            logger.info("数据源：MCP（恒生聚源）")
            return src
    except Exception as e:
        logger.warning(f"MCP 连接失败（{e}），降级到 Akshare")

    from data.source.akshare_source import AkshareSource
    logger.info("数据源：Akshare")
    return AkshareSource()


def get_all_codes_from_mcp(src) -> list[str]:
    """通过 MCP FinQuery 获取全部A股代码。"""
    import pandas as pd
    try:
        from data.source.mcp_source import MCPSource
        if isinstance(src, MCPSource):
            results = src._call("FinQuery",
                "查询A股全部上市股票的股票代码和股票名称，返回所有股票")
            df = src._first_table(results)
            if not df.empty:
                for col in df.columns:
                    if "代码" in col:
                        codes = df[col].astype(str).str.split(".").str[0].tolist()
                        logger.info(f"从 MCP 获取股票列表：{len(codes)} 只")
                        return codes
    except Exception as e:
        logger.warning(f"MCP 获取股票列表失败：{e}")

    # 降级：用 Akshare 获取股票列表（只是列表，不拉行情）
    try:
        from data.source.akshare_source import AkshareSource
        ak_src = AkshareSource()
        info = ak_src.get_stock_info()
        if not info.empty:
            codes = info["code"].tolist()
            logger.info(f"从 Akshare 获取股票列表：{len(codes)} 只")
            # 保存股票基本信息
            save_meta("stock_info", info)
            return codes
    except Exception as e:
        logger.error(f"获取股票列表彻底失败：{e}")
    return []


def download_one(src, code: str, start: str, end: str) -> tuple[str, bool, str]:
    """下载单只股票日线，带重试。"""
    for attempt in range(3):
        try:
            df = src.get_daily(code, start, end)
            if df.empty:
                if attempt < 2:
                    time.sleep(1)
                    continue
                return code, False, "数据为空"
            save_daily(code, df)
            return code, True, ""
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)  # 指数退避
            else:
                return code, False, str(e)[:80]
    return code, False, "重试3次失败"


def init_daily(src, codes: list[str], start: str, end: str, workers: int):
    """并发下载全市场日线数据。"""
    total = len(codes)
    success, failed = 0, []

    logger.info(f"开始下载日线：{total} 只，{start}→{end}，{workers} 并发")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(download_one, src, c, start, end): c for c in codes}
        for i, future in enumerate(as_completed(futures)):
            code, ok, err = future.result()
            if ok:
                success += 1
            else:
                failed.append(code)
                if err and "数据为空" not in err:
                    logger.warning(f"  {code}: {err}")

            if (i + 1) % 200 == 0 or (i + 1) == total:
                pct = (i + 1) / total * 100
                logger.info(
                    f"进度 {i+1}/{total} ({pct:.1f}%)  "
                    f"成功:{success}  失败:{len(failed)}"
                )

    return success, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",   default="2019-01-01")
    parser.add_argument("--end",     default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--limit",   type=int, default=0)
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info(f"初始化开始: {args.start} → {args.end}，{args.workers} 并发")
    logger.info("=" * 60)

    t0 = time.time()
    src = _make_source()

    # 交易日历：合并新区间到已有日历，不覆盖（避免截断历史）
    try:
        from data.source.akshare_source import AkshareSource
        ak_src = AkshareSource()
        import pandas as pd
        new_cal = ak_src.get_trade_calendar(start=args.start, end=args.end)
        if new_cal:
            existing = load_meta("trade_calendar")
            existing_set = set(existing["trade_date"].tolist()) if not existing.empty else set()
            merged = sorted(existing_set | set(new_cal))
            save_meta("trade_calendar", pd.DataFrame({"trade_date": merged}))
            logger.info(f"交易日历已合并：共 {len(merged)} 个交易日（新增 {len(set(new_cal)-existing_set)} 天）")
    except Exception as e:
        logger.warning(f"交易日历获取失败：{e}")

    # 股票列表
    codes = get_all_codes_from_mcp(src)
    if not codes:
        logger.error("无法获取股票列表，退出")
        sys.exit(1)
    if args.limit:
        codes = codes[:args.limit]
        logger.info(f"测试模式：只处理前 {args.limit} 只")

    # 日线行情
    success, failed = init_daily(src, codes, args.start, args.end, args.workers)

    elapsed = (time.time() - t0) / 60
    summary = (
        f"初始化完成：成功 {success}/{len(codes)} 只，"
        f"失败 {len(failed)} 只，耗时 {elapsed:.1f} 分钟"
    )
    logger.info(summary)

    if failed:
        Path("logs/failed_codes.txt").write_text("\n".join(failed))
        logger.info(f"失败列表 → logs/failed_codes.txt")

    send_alert(summary)


if __name__ == "__main__":
    main()
