"""
小盘多因子回测 v1 (建在 baostock 防生存者偏差数据上)。
universe: smallcap_universe_bs (半年快照, 含退市股, 取最近≤dt的快照)
因子: 6-1 动量(过去126天到21天前的收益, 跳过最近1月避免反转) —— 验证过的主因子
组合: 月度调仓, 等权 top-N(默认15)
记账: 按股数/按市价卖/双边成本0.175%/真实日期NAV
退市处理: 持仓股在调仓日无价(已退市) → 按最后可得价强制清仓(如实计退市亏损)
OOS: 2024-01起; base自检打印, 无魔数对照(全新策略)
用法: python scripts/backtest_smallcap.py
"""
import sys, glob; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd, numpy as np
from datetime import datetime
from data.storage import load_meta
import warnings; warnings.filterwarnings('ignore')

t0=datetime.now(); print(f"开始 {t0.strftime('%H:%M:%S')}", flush=True)
N=15; CAP=50000.0; COMM=0.00175; RF=0.025
MOM_LONG=126; MOM_SKIP=21   # 6-1 动量

uni=load_meta("smallcap_universe_bs")
uni["codes"]=uni["codes"].fillna("")
snap={pd.Timestamp(r["date"]): [c for c in r["codes"].split(",") if c] for _,r in uni.iterrows() if r["count"]>0}
snap_dates=sorted(snap)
all_codes=sorted({c for cs in snap.values() for c in cs})
print(f"快照 {len(snap_dates)}个, 池内股票合计 {len(all_codes)}只", flush=True)

# baostock 收盘价宽表
BS=Path("data_store/baostock/daily"); ser={}
for c in all_codes:
    f=BS/f"{c}.parquet"
    if not f.exists(): continue
    d=pd.read_parquet(f, columns=["date","close"])
    s=pd.to_numeric(d.set_index(pd.to_datetime(d["date"]))["close"], errors="coerce").dropna()
    s=s[s>0]
    if len(s)>MOM_LONG: ser[c]=s
panel=pd.DataFrame(ser).sort_index()
print(f"价格宽表 {panel.shape[0]}天 × {panel.shape[1]}只", flush=True)

# 月度调仓日(每月最后交易日)
td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)>=15: rd.append(m[-1])
rd=sorted(set(rd))

def snap_for(dt):
    elig=[s for s in snap_dates if s<=dt]
    return snap[elig[-1]] if elig else []

def last_px_before(c,dt):
    s=panel[c].loc[:dt].dropna() if c in panel.columns else pd.Series(dtype=float)
    return float(s.iloc[-1]) if len(s) else np.nan
def px_at(c,dt):
    try:
        v=panel.at[dt,c]; return float(v) if pd.notna(v) and v>0 else np.nan
    except Exception: return np.nan

pos,entry,cash={}, {}, CAP; rec=[]
for dt in rd:
    universe=set(snap_for(dt))
    scores={}
    for c in universe:
        if c not in panel.columns: continue
        p_now=px_at(c,dt)
        s=panel[c].loc[:dt].dropna()
        if np.isnan(p_now) or len(s)<MOM_LONG: continue
        p_old=s.iloc[-MOM_LONG]; p_skip=s.iloc[-MOM_SKIP] if len(s)>=MOM_SKIP else p_now
        if p_old>0: scores[c]=p_skip/p_old-1
    top=set(sorted(scores, key=scores.get, reverse=True)[:N])
    for c in list(pos.keys()):
        pnow=px_at(c,dt)
        if c not in top or np.isnan(pnow):
            sell_p=pnow if not np.isnan(pnow) else last_px_before(c,dt)
            if np.isnan(sell_p): sell_p=entry[c]
            cash+=pos[c]*sell_p*(1-COMM); pos.pop(c); entry.pop(c,None)
    buys=[c for c in top if c not in pos]; need=N-len(pos)
    per=cash/max(need,1) if need>0 else 0
    for c in buys[:need]:
        p=px_at(c,dt)
        if np.isnan(p): continue
        q=max(int(min(per,CAP*0.3)/p/100)*100,100); cost=q*p
        if cost*(1+COMM)<=cash: cash-=cost*(1+COMM); pos[c]=q; entry[c]=p
    mkv=sum(pos[c]*(px_at(c,dt) if not np.isnan(px_at(c,dt)) else last_px_before(c,dt)) for c in pos)
    rec.append((dt,(cash+mkv)/CAP))

ns=pd.Series([v for _,v in rec], index=pd.DatetimeIndex([d for d,_ in rec]))
def metrics(s):
    d=s.pct_change().dropna(); t=s.iloc[-1]/s.iloc[0]-1
    y=max(len(d)/12,0.5); a=(1+t)**(1/y)-1
    sr=(d.mean()-RF/12)/d.std()*np.sqrt(12) if d.std()>0 else 0
    dd=(s/s.cummax()-1).min(); return a,sr,dd
a,sr,dd=metrics(ns); no=ns[ns.index>="2024-01-01"]; ao,so,ddo=metrics(no)
# 分年收益(看regime模式)
ye=ns.groupby(ns.index.year).last(); prev=1.0
print("分年收益:")
for y,v in ye.items():
    print(f"  {y}: {v/prev-1:+.1%}"); prev=v
print("="*64)
print(f"小盘动量 top{N} 月度 (防生存者偏差, 含退市强平)")
print(f"  全期: 年化{a:+.1%} 夏普{sr:.2f} 回撤{dd:.1%}  ({len(rd)}个调仓月)")
print(f"  OOS24+: 年化{ao:+.1%} 夏普{so:.2f} 回撤{ddo:.1%}")
print("="*64)
print(f"耗时 {(datetime.now()-t0).seconds}s")
