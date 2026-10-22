"""研究侧只读面板查询层（2026-10-22，吸收 TickFlow 的 Polars/Parquet 思路）。

原则：只读 data_store/daily/**/*.parquet，不写库、不替代 data.storage；
生产/信号链仍走 pandas load_daily。用于研究脚本的整面板扫描/聚合，
把 "逐票 load_daily + concat" 换成 Polars 的列式扫描。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

DAILY = Path(__file__).resolve().parent.parent / "data_store" / "daily"
_COLS = ["date", "code", "close", "volume", "amount"]


def _year_files(start: str, end: str) -> list[str]:
    y0, y1 = int(start[:4]), int(end[:4])
    files: list[str] = []
    for y in range(y0, y1 + 1):
        d = DAILY / str(y)
        if d.is_dir():
            files.extend(str(p) for p in sorted(d.glob("*.parquet")))
    return files


def scan(start: str, end: str, codes: list[str] | None = None,
         columns: list[str] | None = None) -> pl.LazyFrame:
    """扫描区间内的日线 parquet；codes 为空=全市场。

    codes 给定时按年目录精确定位文件，避免全目录扫描后再过滤。
    """
    cols = columns or _COLS
    if codes:
        files: list[str] = []
        for y in range(int(start[:4]), int(end[:4]) + 1):
            for c in codes:
                p = DAILY / str(y) / f"{str(c).zfill(6)}.parquet"
                if p.exists():
                    files.append(str(p))
    else:
        files = _year_files(start, end)
    if not files:
        return pl.LazyFrame(schema={c: pl.Utf8 for c in cols})
    # 部分历史文件带额外列(如 股票名称)，只取所需列，忽略 schema 差异
    lf = pl.scan_parquet(files, hive_partitioning=False, extra_columns="ignore")
    have = set(lf.collect_schema().names())
    lf = lf.select([c for c in cols if c in have])
    if "date" in have:
        lf = lf.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"))
        lf = lf.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    return lf


def amount_panel(start: str, end: str, codes: list[str] | None = None) -> pl.DataFrame:
    """date × code 成交额面板（万元，库契约），供 TOP-N/横截面研究。"""
    lf = scan(start, end, codes, ["date", "code", "amount"])
    return (lf.with_columns(pl.col("code").cast(pl.Utf8).str.zfill(6))
              .collect()
              .pivot(index="date", on="code", values="amount", aggregate_function="first")
              .sort("date"))


def close_panel(start: str, end: str, codes: list[str] | None = None) -> pl.DataFrame:
    """date × code 收盘价面板（qfq 口径，按 registry 使用）。"""
    lf = scan(start, end, codes, ["date", "code", "close"])
    return (lf.with_columns(pl.col("code").cast(pl.Utf8).str.zfill(6))
              .collect()
              .pivot(index="date", on="code", values="close", aggregate_function="first")
              .sort("date"))
