"""新浪分红明细全量拉取 (2026-10-10, 高分红年化测量数据准备)。

为什么: 本地库无分红数据; MCP dividend 每票仅 20 条且逐票手工调;
新浪 stock_history_dividend_detail 单票一次拉全史(公告日/派息每10股/
除权除息日), 数据源沿用项目新浪主源。
宇宙: PIT 中证800成员(csi800_pit_weights, 2019-2026, 1335只)——高分红
集中在大中盘, 800 宇宙既有 PIT 声明又省拉取量。盲区(闸门七): 新浪对
退市/北交所覆盖不全, 800 宇宙内退市成员的分红事件可能缺失——测量结论
须带此声明。
产物: data_store/meta/dividends_sina.parquet (as_of 冻结于运行时刻)。
用法: python scripts/pull_dividend_sina.py [--limit N]
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

META_DIR = Path("data_store/meta")
OUT = META_DIR / "dividends_sina.parquet"


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    pit = pd.read_parquet(META_DIR / "csi800_pit_weights.parquet")
    codes = sorted(pit["code"].astype(str).unique())
    if limit:
        codes = codes[:limit]
    logger.info(f"宇宙 {len(codes)} 只, 开始拉取")

    rows, failed = [], []
    for i, code in enumerate(codes):
        for attempt in range(3):
            try:
                df = ak.stock_history_dividend_detail(
                    symbol=code, indicator="分红", date="")
                if df is not None and not df.empty:
                    df = df[df["进度"].astype(str).str.contains("实施", na=False)]
                    df = df[["公告日期", "派息", "除权除息日"]].copy()
                    df["code"] = code
                    rows.append(df)
                break
            except Exception as e:
                if attempt == 2:
                    failed.append(code)
                    logger.debug(f"{code} 拉取失败: {e}")
                time.sleep(1.0 * (attempt + 1))
        if (i + 1) % 200 == 0:
            logger.info(f"  {i+1}/{len(codes)}, 累计 {sum(len(r) for r in rows)} 条")
        time.sleep(0.3)

    if not rows:
        logger.error("0 条记录, 不落盘")
        return
    all_df = pd.concat(rows, ignore_index=True)
    all_df["派息"] = pd.to_numeric(all_df["派息"], errors="coerce")
    all_df["as_of"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_df.to_parquet(OUT, index=False)
    logger.info(f"完成: {len(all_df)} 条 / 失败 {len(failed)} 只 -> {OUT}")


if __name__ == "__main__":
    main()
