"""
构建历史"小盘段"股票池（防生存者偏差，复用 build_historical_universe 的加载/过滤）。
方法：每半年时点按6个月日均成交额排名，取 800~1800 名（跳过前800大盘，size用流动性代理）。
含已退市/被踢股票。输出 data_store/meta/smallcap_universe.parquet [date,count,codes]。

⚠️ size=流动性代理(本地无市值数据), 仅作方向性v1; 上钱前需Tushare真实市值复验。
"""
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent)); sys.path.insert(0, str(Path(__file__).parent))
import pandas as pd
from loguru import logger
from data.storage import load_meta, save_meta
from build_historical_universe import get_rebalance_dates, load_amount_panel, MIN_AMOUNT, MIN_HIST_DAYS, LOOKBACK_DAYS

SKIP_LARGE = 800      # 跳过流动性前800(大盘=Track A的地盘)
BAND_SIZE  = 1000     # 取其后1000只 → 800~1800名 小盘段


def build():
    rebal=get_rebalance_dates()
    info=load_meta("stock_info_full"); info["code"]=info["code"].astype(str).str.zfill(6)
    st=set(info[info.get("is_st",False)==True]["code"]) if "is_st" in info.columns else set()
    ld=info.set_index("code")["list_date"].dropna().to_dict() if "list_date" in info.columns else {}

    daily=Path("data_store/daily"); allc=set()
    for yd in daily.iterdir():
        if yd.is_dir():
            for f in yd.glob("*.parquet"): allc.add(f.stem)
    allc=sorted(allc); logger.info(f"本地股票(含历史): {len(allc)}")

    cache={}
    for yr in sorted(set(d[:4] for d in rebal)):
        panel=load_amount_panel(allc, f"{int(yr)-1}-07-01", f"{yr}-12-31")
        if not panel.empty: cache[yr]=panel

    recs=[]
    for date in rebal:
        panel=cache.get(date[:4])
        if panel is None or panel.empty: recs.append({"date":date,"count":0,"codes":""}); continue
        dts=pd.Timestamp(date); hist=panel[panel.index<=dts].tail(LOOKBACK_DAYS)
        if len(hist)<MIN_HIST_DAYS: recs.append({"date":date,"count":0,"codes":""}); continue
        avg=hist.mean(); filt={}
        for code,val in avg.items():
            if pd.isna(val) or val<MIN_AMOUNT or code in st: continue
            l=ld.get(code)
            if l:
                try:
                    d=(dts-pd.Timestamp(l)).days
                    if 0<=d<252: continue
                except Exception: pass
            filt[code]=val
        ranked=sorted(filt, key=filt.get, reverse=True)
        band=ranked[SKIP_LARGE:SKIP_LARGE+BAND_SIZE]   # 小盘段
        recs.append({"date":date,"count":len(band),"codes":",".join(band)})
        logger.info(f"{date}: 候选{len(filt)} → 小盘段{len(band)}只 (跳过前{SKIP_LARGE})")

    df=pd.DataFrame(recs); save_meta("smallcap_universe", df)
    print(df[["date","count"]].to_string(index=False))
    # 生存者偏差自检: 看池里有多少票"现在已无数据/疑似退市"
    return df


if __name__=="__main__":
    build()
