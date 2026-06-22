"""
CSI800代理池(baostock含退市,按反推流通市值取前800) —— Track A 的地盘。
用于测 Track A 的生存者偏差。复用 build_smallcap_universe_bs 逻辑, 只改名次段为 0-800。
输出 data_store/meta/csi800_universe_bs.parquet [date,count,codes]
"""
import sys, glob; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd
from loguru import logger
from data.storage import save_meta

BS=Path("data_store/baostock/daily")
TOP=800; MIN_AMT=5e6; LOOKBACK=120; MIN_DAYS=20

def build():
    fs=sorted(glob.glob(str(BS/"*.parquet")))
    logger.info(f"baostock文件 {len(fs)}")
    parts=[]
    for f in fs:
        try: d=pd.read_parquet(f, columns=["date","close","volume","turn","amount","isST","code"])
        except Exception: continue
        for c in ("close","volume","turn","amount"): d[c]=pd.to_numeric(d[c],errors="coerce")
        d["isST"]=pd.to_numeric(d["isST"],errors="coerce").fillna(0)
        d=d[(d["turn"]>0)&(d["close"]>0)]
        if d.empty: continue
        d["fmcap"]=d["close"]*d["volume"]*100/d["turn"]
        parts.append(d[["date","code","fmcap","amount","isST"]])
    long=pd.concat(parts,ignore_index=True); long["date"]=pd.to_datetime(long["date"])
    logger.info(f"长表 {len(long):,}行")
    alld=sorted(long["date"].unique()); rds=[]
    for yr in range(2019,2026):
        for mo in (6,12):
            cand=[d for d in alld if d.year==yr and d.month==mo]
            if cand: rds.append(max(cand))
    recs=[]
    for dt in rds:
        win=long[(long["date"]<=dt)&(long["date"]>dt-pd.Timedelta(days=LOOKBACK))]
        g=win.groupby("code").agg(fmcap=("fmcap","median"),amount=("amount","mean"),st=("isST","max"),n=("fmcap","size"))
        g=g[(g["n"]>=MIN_DAYS)&(g["amount"]>=MIN_AMT)&(g["st"]==0)]
        band=g.sort_values("fmcap",ascending=False).iloc[:TOP].index.tolist()
        recs.append({"date":str(dt.date()),"count":len(band),"codes":",".join(band)})
        logger.info(f"{str(dt.date())}: top800取{len(band)}")
    df=pd.DataFrame(recs); save_meta("csi800_universe_bs",df)
    print(df[["date","count"]].to_string(index=False))
    # 退市暴露统计
    bs_alive=set(p.split('/')[-1][:-8] for p in glob.glob("data_store/daily/2025/*.parquet"))
    import glob as g2
    def lastd(c):
        try: return str(pd.to_datetime(pd.read_parquet(f"{BS}/{c}.parquet",columns=["date"])["date"]).max())[:10]
        except: return None
    r2020=[x for x in df[df["date"].str.startswith("2020")].head(1)["codes"]][0].split(",")
    delisted=[c for c in r2020 if (ld:=lastd(c)) and ld<"2024-07-01"]
    print(f"\n2020 CSI800代理池 {len(r2020)}只, 后来退市/长停 {len(delisted)}只 ({len(delisted)/max(len(r2020),1)*100:.1f}%)")
    print(f"退市样本: {delisted[:8]}")

if __name__=="__main__": build()
