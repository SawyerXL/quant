"""
用 baostock 全市场(含退市)数据建"防生存者偏差小盘池"。
size = 反推流通市值 close*volume*100/turn; 取市值800~1800名(小盘段)。
过滤: isST / 流动性下限(日均成交额) / 数据不足。半年度时点。
输出 data_store/meta/smallcap_universe_bs.parquet [date,count,codes]
用法(待baostock拉取完成后): python scripts/build_smallcap_universe_bs.py
"""
import sys, glob; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd, numpy as np
from loguru import logger
from data.storage import save_meta

BS=Path("data_store/baostock/daily")
SKIP_LARGE=800; BAND=1000; MIN_AMT=5e6; LOOKBACK=120; MIN_DAYS=20
def rebal_dates():
    r=[]
    for yr in range(2019,2026):
        for mo in (6,12): r.append(f"{yr}-{mo:02d}-末")
    return r  # 占位,下面用实际交易日替换

def build():
    fs=sorted(glob.glob(str(BS/"*.parquet")))
    logger.info(f"baostock日线文件: {len(fs)}只")
    # 汇总成长表 [date,code,fmcap,amount]
    parts=[]
    for f in fs:
        try:
            d=pd.read_parquet(f, columns=["date","close","volume","turn","amount","isST","code"])
        except Exception: continue
        for c in ("close","volume","turn","amount"): d[c]=pd.to_numeric(d[c],errors="coerce")
        d["isST"]=pd.to_numeric(d["isST"],errors="coerce").fillna(0)
        d=d[(d["turn"]>0)&(d["close"]>0)]
        if d.empty: continue
        d["fmcap"]=d["close"]*d["volume"]*100/d["turn"]
        parts.append(d[["date","code","fmcap","amount","isST"]])
    long=pd.concat(parts,ignore_index=True)
    long["date"]=pd.to_datetime(long["date"])
    logger.info(f"长表 {len(long):,} 行, {long['code'].nunique()}只")

    # 实际半年末交易日
    alld=sorted(long["date"].unique())
    rds=[]
    for yr in range(2019,2026):
        for mo in (6,12):
            cand=[d for d in alld if d.year==yr and d.month==mo]
            if cand: rds.append(max(cand))
    recs=[]
    for dt in rds:
        win=long[(long["date"]<=dt)&(long["date"]>dt-pd.Timedelta(days=LOOKBACK))]
        g=win.groupby("code").agg(fmcap=("fmcap","median"),amount=("amount","mean"),
                                   st=("isST","max"),n=("fmcap","size"))
        g=g[(g["n"]>=MIN_DAYS)&(g["amount"]>=MIN_AMT)&(g["st"]==0)]
        ranked=g.sort_values("fmcap",ascending=False)   # 大→小
        band=ranked.iloc[SKIP_LARGE:SKIP_LARGE+BAND].index.tolist()  # 市值800-1800名=小盘段
        recs.append({"date":str(dt.date()),"count":len(band),"codes":",".join(band)})
        logger.info(f"{str(dt.date())}: 候选{len(g)} → 小盘段{len(band)}只")
    df=pd.DataFrame(recs); save_meta("smallcap_universe_bs",df)
    print(df[["date","count"]].to_string(index=False))

if __name__=="__main__": build()
