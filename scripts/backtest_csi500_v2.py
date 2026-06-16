#!/usr/bin/env python
"""CSI500中盘增强 v2 — 200只股票池, T+1, OOS验收"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime; from pathlib import Path
from data.storage import load_meta
from run_backtest_a2 import compute_score_a2
from run_backtest_a import load_panels
import akshare as ak
import warnings; warnings.filterwarnings('ignore')

LOG_DIR=Path("logs/backtest"); LOG_DIR.mkdir(exist_ok=1)
t0=datetime.now()
print(f"开始: {t0.strftime('%H:%M:%S')}",flush=True)

df5=ak.index_stock_cons(symbol="000905")
codes=sorted([str(c) for c in df5['品种代码'].tolist()])[:200]
panel,ap=load_panels(codes,"2019-01-01","2025-12-31"); info=load_meta("stock_info_full")
print(f"数据: {panel.shape[1]}只 {panel.shape[0]}天",flush=True)

td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd))
CAP=50000; COMM=0.00175; RF=0.025

def run_period(rds, N):
    nav,pos,cost_b,cash=[],{},{},CAP
    for dts in rds:
        dt=pd.Timestamp(dts); score=compute_score_a2(panel,dt,ap,info)
        if len(score)<N: continue
        top=score.nlargest(N).index.tolist()
        for c in list(pos.keys()):
            if c not in top: cash+=pos[c]; pos.pop(c); cost_b.pop(c,None)
        need=N-len(pos); cand=[c for c in top if c not in pos]
        per=cash/max(need,1) if need>0 else 0
        for c in cand[:need]:
            p=float(panel[c].loc[dt]) if dt in panel[c].index else 0
            if np.isnan(p) or p<=0: continue
            q=max(int(min(per,CAP*0.3)/p/100)*100,100); cost=q*p
            if cost<=cash: cash-=cost*(1+COMM); pos[c]=cost; cost_b[c]=p
        mkv=sum(pos[c]*(float(panel[c].loc[dt])/cost_b[c]) if dt in panel.index and c in panel.columns and c in cost_b and cost_b[c]>0 else pos[c] for c in pos)
        nav.append((cash+mkv)/CAP)
    return pd.Series(nav,index=pd.DatetimeIndex([pd.Timestamp(d) for d in rds[:len(nav)]]))

def metrics(ns):
    d=ns.pct_change().dropna(); t=ns.iloc[-1]/ns.iloc[0]-1
    y=max(len(d)/(26),0.5); a=(1+t)**(1/y)-1; v=d.std()*np.sqrt(26)
    s=(d.mean()-RF/26)/d.std()*np.sqrt(26) if d.std()>0 else 0
    dd=(ns/ns.cummax()-1).min(); w=np.mean(d>0)
    return {"total":t,"annual":a,"vol":v,"sharpe":s,"max_dd":dd,"win_rate":w}

# 训练
rds_train=[d for d in rd if pd.Timestamp("2019-01-01")<=d<=pd.Timestamp("2023-12-31")]
rds_oos=[d for d in rd if pd.Timestamp("2024-01-01")<=d<=pd.Timestamp("2025-12-31")]

print(f"\n{'='*60}")
print(f"  CSI500 中盘增强 v2  200只股票池  OOS验收")
print(f"{'='*60}")
print(f"  {'N':>4} {'训练年化':>10} {'OOS年化':>10} {'OOS夏普':>8} {'OOS回撤':>8} {'OOS胜率':>7} {'判定':>6}")
print(f"  {'-'*60}")

for N in [30,15,12,10,8,6]:
    t1=datetime.now(); print(f"  N={N}...",end=" ",flush=True)
    ns_train=run_period(rds_train,N)
    ns_oos=run_period(rds_oos,N)
    mt=metrics(ns_train); mo=metrics(ns_oos)
    elapsed=(datetime.now()-t1).seconds

    # OOS验收
    checks=[mo["annual"]>0.12, mo["sharpe"]>0.5, abs(mo["max_dd"])<0.35, mo["annual"]>0]
    passed=sum(checks)
    flag="✅" if passed>=3 else ("⚠️" if passed>=2 else "❌")
    print(f"\r  {N:>4} {mt['annual']:>+9.1%} {mo['annual']:>+9.1%} {mo['sharpe']:>7.2f} {mo['max_dd']:>7.1%} {mo['win_rate']:>6.0%} {flag:>6}",
          flush=True)

    # 自检
    d=ns_oos.pct_change().dropna()
    fwd=(1+mo["annual"])**(max(len(d)/26,0.5))-1
    if abs(fwd-mo["total"])>0.03: print(f"  ⚠️自检败",end="")

print(f"\n{'='*60}")
print(f"  总耗时: {(datetime.now()-t0).seconds}s")
print(f"  验收线: 年化>12% 夏普>0.5 回撤<-35% OOS>0")
print(f"{'='*60}")
EOF