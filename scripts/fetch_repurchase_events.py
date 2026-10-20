"""抓取股票回购数据并落盘(2026-10-20, 回购候选数据审计)。

数据: akshare stock_repurchase_em() — 全市场回购记录(含公告日/进度/金额/价格)。
用法: python scripts/fetch_repurchase_events.py
"""
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger

logger.remove()
META = Path("/root/quant/data_store/meta")


def main():
    import akshare as ak
    d = ak.stock_repurchase_em()
    d = d.copy()
    d["as_of"] = date.today().isoformat()
    # 代码列名兼容
    code_col = next((c for c in d.columns if "代码" in c), None)
    if code_col:
        d["code"] = d[code_col].astype(str).str.zfill(6)
    for c in d.columns:
        if "日期" in c or "时间" in c:
            d[c] = pd.to_datetime(d[c], errors="coerce")
    out = META / "repurchase_events.parquet"
    d.to_parquet(out, index=False)
    print(f"rows {len(d)}"); print("columns:", list(d.columns))
    for c in d.columns:
        if "进度" in c or "状态" in c:
            print(c, d[c].value_counts().head(10).to_string())
    for c in d.columns:
        if "日期" in c:
            print(c, d[c].min(), "->", d[c].max())
    print(d.head(3).to_string()[:1500])
    print("->", out)


if __name__ == "__main__":
    main()
