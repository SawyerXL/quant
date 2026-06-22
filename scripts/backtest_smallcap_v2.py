"""
小盘多因子回测 v2 = v1动量 + 质量闸门(survivor-bias-free代理: 动态剔ST + 剔负净资产/极端高pb)。
对比 v1(纯动量): 看质量闸门能否堵住2021-24小盘熊市的-40%/年窟窿。
质量数据: baostock日线自带 isST / pbMRQ(干净,含退市股)。
其余同v1: 月度,top15,按市价记账,双边成本,退市强平,真实日期NAV,分年。
修正: 买入按(分数,代码)确定性排序,消除v1的set迭代非确定性。
用法: python scripts/backtest_smallcap_v2.py
"""
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd, numpy as np
from datetime import datetime
from data.storage import load_meta
import warnings; warnings.filterwarnings('ignore')

t0=datetime.now(); print(f"开始 {t0.strftime('%H:%M:%S')}", flush=True)
N=15; CAP=50000.0; COMM=0.00175; RF=0.025
MOM_LONG=126; MOM_SKIP=21; PB_MAX=8.0

uni=load_meta("smallcap_universe_bs"); uni["codes"]=uni["codes"].fillna("")
snap={pd.Timestamp(r["date"]): [c for c in r["codes"].split(",") if c] for _,r in uni.iterrows() if r["count"]>0}
snap_dates=sorted(snap); all_codes=sorted({c for cs in snap.values() for c in cs})

BS=Path("data_store/baostock/daily"); close={}, ; close={}; pb={}; st={}
for c in all_codes:
    f=BS/f"{c}.parquet"
    if not f.exists(): continue
    d=pd.read_parquet(f, columns=["date","close","pbMRQ","isST"])
    idx=pd.to_datetime(d["date"])
    s=pd.to_numeric(d.set_index(idx)["close"],errors="coerce")
    if (s>0).sum()<=MOM_LONG: continue
    close[c]=s
    pb[c]=pd.to_numeric(d.set_index(idx)["pbMRQ"],errors="coerce")
    st[c]=pd.to_numeric(d.set_index(idx)["isST"],errors="coerce")
panel=pd.DataFrame(close).sort_index()
pbp=pd.DataFrame(pb).reindex(panel.index); stp=pd.DataFrame(st).reindex(panel.index)
print(f"价格宽表 {panel.shape[0]}天 × {panel.shape[1]}只", flush=True)

td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)>=15: rd.append(m[-1])
rd=sorted(set(rd))
def snap_for(dt):
    e=[s for s in snap_dates if s<=dt]; return snap[e[-1]] if e else []
def at(df,c,dt):
    try:
        v=df.at[dt,c]; return float(v) if pd.notna(v) else np.nan
    except Exception: return np.nan
def px(c,dt):
    v=at(panel,c,dt); return v if (not np.isnan(v) and v>0) else np.nan
def last_px(c,dt):
    if c not in panel.columns: return np.nan
    s=panel[c].loc[:dt].dropna(); s=s[s>0]; return float(s.iloc[-1]) if len(s) else np.nan

pos,entry,cash={}, {}, CAP; rec=[]
for dt in rd:
    universe=set(snap_for(dt)); scored=[]
    for c in universe:
        if c not in panel.columns: continue
        p_now=px(c,dt); s=panel[c].loc[:dt].dropna(); s=s[s>0]
        if np.isnan(p_now) or len(s)<MOM_LONG: continue
        # 质量闸门
        isst=at(stp,c,dt); pbv=at(pbp,c,dt)
        if isst==1: continue                       # 动态剔ST
        if not np.isnan(pbv) and (pbv<=0 or pbv>PB_MAX): continue   # 剔负净资产/极端高pb
        p_old=s.iloc[-MOM_LONG]; p_skip=s.iloc[-MOM_SKIP] if len(s)>=MOM_SKIP else p_now
        if p_old>0: scored.append((p_skip/p_old-1, c))
    scored.sort(key=lambda x:(-x[0], x[1]))       # 确定性排序
    top=set(c for _,c in scored[:N]); top_order=[c for _,c in scored[:N]]
    for c in list(pos.keys()):
        pnow=px(c,dt)
        if c not in top or np.isnan(pnow):
            sp=pnow if not np.isnan(pnow) else last_px(c,dt)
            if np.isnan(sp): sp=entry[c]
            cash+=pos[c]*sp*(1-COMM); pos.pop(c); entry.pop(c,None)
    need=N-len(pos)
    for c in [x for x in top_order if x not in pos][:need]:
        p=px(c,dt)
        if np.isnan(p): continue
        q=max(int(min(cash/max(need,1),CAP*0.3)/p/100)*100,100); cost=q*p
        if cost*(1+COMM)<=cash: cash-=cost*(1+COMM); pos[c]=q; entry[c]=p
    mkv=sum(pos[c]*(px(c,dt) if not np.isnan(px(c,dt)) else last_px(c,dt)) for c in pos)
    rec.append((dt,(cash+mkv)/CAP))

ns=pd.Series([v for _,v in rec], index=pd.DatetimeIndex([d for d,_ in rec]))
def metrics(s):
    d=s.pct_change().dropna(); t=s.iloc[-1]/s.iloc[0]-1; y=max(len(d)/12,0.5)
    a=(1+t)**(1/y)-1; sr=(d.mean()-RF/12)/d.std()*np.sqrt(12) if d.std()>0 else 0
    return a,sr,(s/s.cummax()-1).min()
a,sr,dd=metrics(ns); no=ns[ns.index>="2024-01-01"]; ao,so,ddo=metrics(no)
ye=ns.groupby(ns.index.year).last(); prev=1.0
print("分年:")
for y,v in ye.items(): print(f"  {y}: {v/prev-1:+.1%}"); prev=v
print("="*64)
print(f"v2 小盘动量+质量闸门 top{N} 月度")
print(f"  全期: 年化{a:+.1%} 夏普{sr:.2f} 回撤{dd:.1%}")
print(f"  OOS24+: 年化{ao:+.1%} 夏普{so:.2f} 回撤{ddo:.1%}")
print(f"  (v1对照: 全期-6.8%/-85%回撤, OOS+40%靠2025)")
print("="*64); print(f"耗时{(datetime.now()-t0).seconds}s")
