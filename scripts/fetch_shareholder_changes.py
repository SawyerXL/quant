"""抓取股东增减持(董监高/股东)并落盘(2026-10-20, 股东增持候选)。

数据: akshare stock_ggcg_em(symbol="全部") — 147k 行, 含公告日/增减/数量/比例/股东名。
用法: python scripts/fetch_shareholder_changes.py
"""
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger

logger.remove()
META = Path("/root/quant/data_store/meta")


def main():
    import akshare as ak
    d = ak.stock_ggcg_em(symbol="全部")
    d = d.copy()
    d["代码"] = d["代码"].astype(str).str.zfill(6)
    for c in ("公告日", "变动开始日", "变动截止日"):
        d[c] = pd.to_datetime(d[c], errors="coerce")
    d["as_of"] = date.today().isoformat()
    out = META / "shareholder_changes.parquet"
    d.to_parquet(out, index=False)
    print(f"rows {len(d)}; date {d['公告日'].min()} -> {d['公告日'].max()}")
    print(d["持股变动信息-增减"].value_counts().to_string())
    print("unique codes", d["代码"].nunique())
    print("dup (code,公告日,股东名称,增减,数量):",
          d.duplicated(["代码", "公告日", "股东名称", "持股变动信息-增减",
                        "持股变动信息-变动数量"]).sum())
    print("sample 增持:")
    z = d[d["持股变动信息-增减"] == "增持"]
    print(z[["代码", "名称", "股东名称", "持股变动信息-变动数量",
             "持股变动信息-占总股本比例", "变动开始日", "变动截止日", "公告日"]].head(5).to_string())
    print("->", out)


if __name__ == "__main__":
    main()
