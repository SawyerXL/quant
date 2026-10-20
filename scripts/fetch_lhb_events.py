"""抓取龙虎榜机构买卖统计（2019-2026）并落盘。

数据: akshare stock_lhb_jgmmtj_em(start_date, end_date) — 机构专用席位买卖汇总。
按季度抓取，避免单次区间过大；只保留股票（剔除转债/基金）。
输出: data_store/meta/lhb_institution_2019_2026.parquet
用法: python scripts/fetch_lhb_events.py
"""
import time
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger

META = Path("/root/quant/data_store/meta")


def quarters():
    out = []
    for y in range(2019, 2027):
        for m in (1, 4, 7, 10):
            start = pd.Timestamp(year=y, month=m, day=1)
            end = start + pd.offsets.QuarterEnd(0)
            if start > pd.Timestamp("2026-10-19"):
                continue
            out.append((start.strftime("%Y%m%d"),
                        min(end, pd.Timestamp("2026-10-19")).strftime("%Y%m%d")))
    return out


def main():
    import akshare as ak
    parts = []
    for i, (s, e) in enumerate(quarters(), 1):
        try:
            d = ak.stock_lhb_jgmmtj_em(start_date=s, end_date=e)
            if d is not None and len(d):
                d = d.copy()
                d["fetch_start"] = s
                d["fetch_end"] = e
                parts.append(d)
            print(f"{i}/{len(quarters())} {s}-{e}: {0 if d is None else len(d)}", flush=True)
        except Exception as ex:
            print(f"{i} {s}-{e} ERR {str(ex)[:120]}", flush=True)
        time.sleep(0.4)
    if not parts:
        print("NO DATA")
        return
    allx = pd.concat(parts, ignore_index=True)
    code_col = next((c for c in allx.columns if "代码" in c), None)
    date_col = next((c for c in allx.columns if "日期" in c), None)
    if code_col:
        allx["code"] = allx[code_col].astype(str).str.zfill(6)
        # 剔除转债/基金（11/12/13 开头为转债；5 开头为基金）
        allx = allx[~allx["code"].str[:2].isin(["11", "12", "13"])]
        allx = allx[~allx["code"].str[:1].isin(["5"])]
    allx["as_of"] = date.today().isoformat()
    out = META / "lhb_institution_2019_2026.parquet"
    allx.to_parquet(out, index=False)
    print(f"rows {len(allx)} cols {list(allx.columns)}")
    if date_col:
        print("date range", allx[date_col].min(), "->", allx[date_col].max())
        print(allx[date_col].astype(str).str[:7].value_counts().sort_index().tail(12).to_string())
    print("->", out)


if __name__ == "__main__":
    main()
