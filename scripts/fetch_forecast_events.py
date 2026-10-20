"""抓取业绩预告事件(PIT去重)并落盘(2026-10-20, PEAD-forecast 候选)。

预注册: logs/prereg_pead_forecast_20261020.md
- 季度 2019Q1~2026Q2; akshare stock_yjyg_em(季度末)。
- 每股票每季度只保留最早公告日; 指标优先"归属于上市公司股东的净利润", 其次"净利润"。
- 剔除公告日晚于该季报告截止日的行(更新/补充; 计数)。
用法: python scripts/fetch_forecast_events.py
"""
import time
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger

logger.remove()
META = Path("/root/quant/data_store/meta")

QUARTERS = [f"{y}{md}" for y in range(2019, 2027)
            for md in ("0331", "0630", "0930", "1231")
            if (y, md) <= (2026, "0630")]
DEADLINE = {"0331": "-04-30", "0630": "-08-31", "0930": "-10-31", "1231": "-04-30"}


def deadline_for(qend):
    y, md = qend[:4], qend[4:]
    if md == "1231":
        return f"{int(y)+1}{DEADLINE[md]}"
    return f"{y}{DEADLINE[md]}"


def main():
    import akshare as ak
    frames = []
    failed = []
    for q in QUARTERS:
        for attempt in range(3):
            try:
                d = ak.stock_yjyg_em(date=q)
                break
            except Exception as e:
                d = None
                time.sleep(1.5 * (attempt + 1))
        if d is None or d.empty:
            failed.append(q); continue
        d = d.copy()
        d["quarter"] = q
        frames.append(d)
        logger.info(f"{q}: {len(d)} rows")
        time.sleep(0.3)
    if not frames:
        raise SystemExit("无数据")
    df = pd.concat(frames, ignore_index=True)
    df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
    df["公告日期"] = pd.to_datetime(df["公告日期"], errors="coerce")
    df = df.dropna(subset=["公告日期", "股票代码", "预告类型"])
    dl = df["quarter"].map(deadline_for)
    late = df["公告日期"] > pd.to_datetime(dl)
    n_late = int(late.sum())
    df = df[~late].copy()
    # 指标优先级
    prim = df["预测指标"].isin(["归属于上市公司股东的净利润", "净利润"])
    df = df[prim].copy()
    # 同股票同季度取最早公告日; 同日多行优先归母
    df["prio"] = (df["预测指标"] != "归属于上市公司股东的净利润").astype(int)
    df = df.sort_values(["股票代码", "quarter", "公告日期", "prio"])
    df = df.drop_duplicates(["股票代码", "quarter"], keep="first")
    df["as_of"] = date.today().isoformat()
    out = META / "forecast_events_2019_2026.parquet"
    cols = ["股票代码", "股票简称", "quarter", "公告日期", "预测指标", "预测数值",
            "业绩变动幅度", "预告类型", "上年同期值", "as_of"]
    df[cols].to_parquet(out, index=False)
    print(f"events {len(df)} rows; late_excluded {n_late}; failed {failed}")
    print(df["预告类型"].value_counts().to_string())
    print(df.groupby("quarter").size().to_string())
    print("->", out)


if __name__ == "__main__":
    main()
