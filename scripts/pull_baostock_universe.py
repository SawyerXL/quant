"""
从 baostock 拉全市场(含退市股)qfq日线 → data_store/baostock/daily/<code>.parquet
解决本地数据的生存者偏差。字段: date,close,volume,amount,turn,pbMRQ,isST。
主代码表 = 若干历史时点 query_all_stock 的并集(自然含当时在市、现已退市的票)。
用法: python scripts/pull_baostock_universe.py
"""
import sys, time; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import baostock as bs, pandas as pd
from loguru import logger

OUT=Path("data_store/baostock/daily"); OUT.mkdir(parents=True, exist_ok=True)
START,END="2019-01-01","2025-12-31"
FIELDS="date,close,volume,amount,turn,pbMRQ,isST"
SNAP_DATES=["2019-06-28","2021-06-30","2023-06-30","2025-06-30"]  # 取并集捕获退市股

bs.login()
# 1) 主代码表(含退市)
codes=set()
for d in SNAP_DATES:
    rs=bs.query_all_stock(day=d)
    while rs.next():
        c=rs.get_row_data()[0]   # 形如 sh.600519
        if c[:3] in ("sh.","sz.","bj.") and c[3] in "0369":  # A股个股,排指数(sh.000)
            codes.add(c)
codes=sorted(codes)
logger.info(f"主代码表(含退市): {len(codes)}只")

# 2) 逐只拉qfq日线
ok=empty=err=0; t0=time.time()
for i,code in enumerate(codes,1):
    try:
        rs=bs.query_history_k_data_plus(code,FIELDS,start_date=START,end_date=END,frequency="d",adjustflag="2")
        rows=[]
        while rs.next(): rows.append(rs.get_row_data())
        if not rows: empty+=1; continue
        df=pd.DataFrame(rows,columns=FIELDS.split(","))
        df["code"]=code.split(".")[1]
        df.to_parquet(OUT/f"{df['code'].iloc[0]}.parquet",index=False)
        ok+=1
    except Exception as e:
        err+=1; logger.warning(f"{code} 失败: {str(e)[:60]}")
    if i%500==0: logger.info(f"进度 {i}/{len(codes)} 成功{ok} 空{empty} 失败{err} 用时{time.time()-t0:.0f}s")
logger.info(f"完成: 成功{ok} 空{empty} 失败{err}/{len(codes)} 用时{time.time()-t0:.0f}s")
bs.logout()
