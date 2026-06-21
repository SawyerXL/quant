"""
钉死实验: 同一份N=6 NAV, OOS用两种日期标签算, 隔离 rd[:len(nav)] 是否制造了+39.6%假象。
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime
from data.storage import load_meta
from run_backtest_a2 import compute_score_a2
from run_backtest_a import load_panels
import akshare as ak
import warnings; warnings.filterwarnings('ignore')

t0=datetime.now(); print(f"开始: {t0.strftime('%H:%M:%S')}", flush=True)
csi5=ak.index_stock_cons(symbol="000905")
codes=sorted([str(c) for c in csi5['品种代码'].tolist()])[:180]
panel,ap=load_panels(codes,"2019-01-01","2025-12-31"); info=load_meta("stock_info_full")
td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd)); CAP=50000; COMM=0.00175; RF=0.025; N=6
def px(c,dt):
    try:
        p=float(panel[c].loc[dt]); return p if p>0 else np.nan
    except Exception: return np.nan

pos,ep,cash={}, {}, CAP; rec=[]; skipped=[]
for dts in rd:
    dt=pd.Timestamp(dts); sc=compute_score_a2(panel,dt,ap,info)
    if len(sc)<N: skipped.append(str(dt)[:10]); continue   # 与原引擎同款跳过
    top=sc.nlargest(N).index.tolist()
    for c in list(pos.keys()):
        if c not in top:
            p=px(c,dt); p=ep[c] if np.isnan(p) else p
            cash+=pos[c]*p*(1-COMM); pos.pop(c); ep.pop(c,None)
    need=N-len(pos); cand=[c for c in top if c not in pos]
    per=cash/max(need,1) if need>0 else 0
    for c in cand[:need]:
        p=px(c,dt)
        if np.isnan(p): continue
        q=max(int(min(per,CAP*0.3)/p/100)*100,100); cost=q*p
        if cost*(1+COMM)<=cash: cash-=cost*(1+COMM); pos[c]=q; ep[c]=p
    mkv=sum(pos[c]*px(c,dt) if not np.isnan(px(c,dt)) else pos[c]*ep[c] for c in pos)
    rec.append((dt,(cash+mkv)/CAP))

vals=[v for _,v in rec]; real_dts=[d for d,_ in rec]
def oos(ns):
    no=ns[ns.index>="2024-01-01"]
    if len(no)<2: return None,None
    d=no.pct_change().dropna(); t=no.iloc[-1]/no.iloc[0]-1; y=max(len(d)/26,0.5)
    return (1+t)**(1/y)-1,(no/no.cummax()-1).min()

ns_bug=pd.Series(vals,index=pd.DatetimeIndex([pd.Timestamp(d) for d in rd[:len(vals)]]))  # 原引擎
ns_ok =pd.Series(vals,index=pd.DatetimeIndex(real_dts))                                    # 正确
ab,db=oos(ns_bug); ao,do=oos(ns_ok)
print(f"\n{'='*64}")
print(f"总调仓日{len(rd)}  出分(有效){len(rec)}  跳过{len(skipped)}")
print(f"跳过日期范围: {skipped[0] if skipped else '-'} ~ {skipped[-1] if skipped else '-'}")
print(f"原引擎贴标签 rd[:{len(vals)}] 末日={str(rd[len(vals)-1])[:10]}  真实末日={str(real_dts[-1])[:10]}")
print(f"{'-'*64}")
print(f"  OOS年化  [rd[:len] 原引擎方式] = {ab:+.1%}   回撤={db:.1%}")
print(f"  OOS年化  [真实日期  正确方式 ] = {ao:+.1%}   回撤={do:.1%}")
print(f"{'='*64}")
print(f"结论: {'✅ +39.6%是日期错位bug, 真实OOS≈' + format(ao,'+.1%') if ab and ao and abs(ab-ao)>0.05 else '两者接近,日期非主因'}")
print(f"耗时{(datetime.now()-t0).seconds}s")
