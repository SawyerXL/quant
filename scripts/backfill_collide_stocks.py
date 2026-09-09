"""
补数: 4 个指数/个股歧义码的个股数据 (2026-09-09 normalize 修复配套) —
000001=平安银行 / 000688=国城矿业 / 000905=厦门港务 / 000906=浙商中拓。
历史: 日更循环曾排除这 4 码(防指数被个股价覆盖), 代价=它们的个股
数据从未入库, 裸键文件里一直是指数点位(000001 读回 3942 点)。
2026-09-09 起指数已迁至 SH 前缀键, 裸键归还个股——本脚本从源拉取
个股日线补入(源按可用性返回全部可得历史, 新浪上限~1023根)。
用法: python scripts/backfill_collide_stocks.py > logs/backfill_collide_stocks.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
logger.remove()

from data.storage import save_daily
from daily_data_update import get_source

CODES = ["000001", "000688", "000905", "000906"]
START = "2019-01-01"
END = "2026-09-08"


def main():
    src = get_source()
    for code in CODES:
        try:
            df = src.get_daily(code, START, END)
            if df is None or df.empty:
                print(f"{code}: 源返回空")
                continue
            df = df.copy()
            df["date"] = df["date"].astype(str)
            rejected = save_daily(code, df)
            # 验证身份: 拉到的应是个股价格(<1000), 不是指数点位
            import pandas as pd
            cl = pd.to_numeric(df["close"], errors="coerce").dropna()
            print(f"{code}: {len(df)}行, close范围 {cl.min():.2f}~{cl.max():.2f}, "
                  f"拒绝{rejected}行, 最新 {df['date'].iloc[-1]}")
        except Exception as e:
            print(f"{code}: 失败 {e}")


if __name__ == "__main__":
    main()
