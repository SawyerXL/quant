"""中证800 PIT 成分+权重拉取 (2026-09-18, Tushare index_weight)。

数据源: tushare.pro index_weight (需2000积分), trade_date 粒度=
调整生效日。落盘 csi800_pit_weights.parquet (code/trade_date/weight,
as_of 元数据)。校验锚: 最新一期成员数=800 且与官方归档
(csi800_members_official.parquet) 800/800 一致, 否则拒档。
用法: python scripts/pull_pit_csi800.py [start=20190101] [end=今天]
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
from dotenv import load_dotenv
from loguru import logger

load_dotenv(Path(__file__).parent.parent / ".env")
import tushare as ts

START = sys.argv[1] if len(sys.argv) > 1 else "20190101"
END = sys.argv[2] if len(sys.argv) > 2 else date.today().strftime("%Y%m%d")
OUT = Path("data_store/meta/csi800_pit_weights.parquet")


def main():
    pro = ts.pro_api()
    df = pro.index_weight(index_code="000906.SH", start_date=START,
                          end_date=END)
    if df is None or df.empty:
        logger.error("index_weight 返回空——检查权限(需2000积分)或日期范围")
        sys.exit(1)
    df = df[["con_code", "trade_date", "weight"]].rename(
        columns={"con_code": "code"})
    df["code"] = df["code"].str.split(".").str[0]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df[df["weight"] > 0]
    logger.info(f"拉取 {df['trade_date'].min().date()}→"
                f"{df['trade_date'].max().date()} | "
                f"{df['trade_date'].nunique()} 个时点 | {len(df)} 条")

    # 校验锚: 最新时点成员=800 且与官方归档一致
    latest_d = df["trade_date"].max()
    latest = set(df[df["trade_date"] == latest_d]["code"])
    off = pd.read_parquet("data_store/meta/csi800_members_official.parquet")
    off_codes = set(off["code"].astype(str))
    if len(latest) != 800:
        logger.error(f"最新期成员 {len(latest)} ≠ 800, 拒档")
        sys.exit(1)
    diff = latest ^ off_codes
    if diff:
        logger.error(f"与官方归档不一致: {sorted(diff)[:10]}, 拒档")
        sys.exit(1)
    logger.info("校验锚通过: 最新期 800/800 与官方归档一致 ✓")

    df.to_parquet(OUT, index=False)
    logger.info(f"落盘 {OUT} (as_of={date.today()}, source=tushare)")
    # 时点断言: 每个 trade_date 成员数应为 800(除新指数早期)
    counts = df.groupby("trade_date")["code"].nunique()
    bad = counts[counts != 800]
    if len(bad):
        logger.warning(f"成员数≠800 的时点: {len(bad)} 个 "
                       f"({bad.index[0].date()} 起, {bad.iloc[0]}只)"
                       f"——2019 起应为 0, 非零须查")


if __name__ == "__main__":
    main()
