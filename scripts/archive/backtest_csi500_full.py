#!/usr/bin/env python
"""CSI500完整因子回测 - 训练/OOS分离, T+1, 成本0.175%"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime
from pathlib import Path
from data.storage import load_meta
from run_backtest_a2 import compute_score_a2
from run_backtest_a import load_panels
import akshare as ak
import warnings; warnings.filterwarnings('ignore')

t0=datetime.now()
print(f"开始: {t0.strftime('%H:%M:%S')}", flush=True)

df5=ak.index_stock_cons(symbol="000905")
codes=sorted([str(c) for c in df5['品种代码'].tolist()])[:80]
panel,ap=load_panels(codes,"2019-01-01","2025-12-31"); info=load_meta("stock_info_full")
print(f"数据: {panel.shape[1]}只 {panel.shape[0]}天", flush=True)

td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd))
CAP=50000; COMM=0.00175

results=[]
for period,start_d,end_d in [("OOS 2024-25","2024-01-01","2025-12-31"),
                               ("训练2019-23","2019-01-01","2023-12-31")]:
    rds=[d for d in rd if pd.Timestamp(start_d)<=d<=pd.Timestamp(end_d)]
    for N in [30,10,8]:
        t1=datetime.now(); print(f"  {period} N={N}...", end=" ", flush=True)
        nav,pos,cost_b,cash=[],{},{},CAP
        for dts in rds:
            dt=pd.Timestamp(dts)
            score=compute_score_a2(panel,dt,ap,info)
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
        ns=pd.Series(nav,index=pd.DatetimeIndex([pd.Timestamp(d) for d in rds[:len(nav)]]))
        d=ns.pct_change().dropna(); t=d.add(1).prod()-1
        y=max(len(d)/(26),0.5); a=(1+t)**(1/y)-1; dd=(ns/ns.cummax()-1).min()
        sr=(d.mean()-0.025/26)/d.std()*np.sqrt(26) if d.std()>0 else 0
        elapsed=(datetime.now()-t1).seconds
        results.append(f"  {period} N={N:>2}: 年化{a:+.1%} 夏普{sr:.2f} 回撤{dd:.1%} ({elapsed}s)")
        print(f"OK ({elapsed}s)", flush=True)

print(f"\n=== CSI500完整因子回测 ===")
print("\n".join(results))
print(f"总耗时: {(datetime.now()-t0).seconds}s")
(Path("logs/backtest")/"csi500_result.txt").write_text("\n".join(results))
print(f"\n完成: {datetime.now().strftime('%H:%M:%S')}")
EOF