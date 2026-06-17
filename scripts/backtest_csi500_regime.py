"""CSI500 牛熊切换回测: MA200>1.05→6只, <1.05→10只"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime; from pathlib import Path
from data.storage import load_meta
from run_backtest_a2 import compute_score_a2
from run_backtest_a import load_panels
from strategies.csi500_enhanced import get_position_size
import akshare as ak
import warnings; warnings.filterwarnings('ignore')

t0=datetime.now()
LOG_DIR=Path("logs/backtest"); LOG_DIR.mkdir(exist_ok=1)
print(f"开始: {t0.strftime('%H:%M:%S')}", flush=True)

# 数据
csi5=ak.index_stock_cons(symbol="000905")
codes=sorted([str(c) for c in csi5['品种代码'].tolist()])[:180]
panel,ap=load_panels(codes,"2019-01-01","2025-12-31"); info=load_meta("stock_info_full")
print(f"CSI500: {panel.shape[1]}只 {panel.shape[0]}天", flush=True)

# CSI800用于MA200
idx=ak.stock_zh_index_daily(symbol="sh000906")
idx['date']=pd.to_datetime(idx['date']); idx=idx.set_index('date').sort_index()
csi800=idx['close']

# 调仓日
td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd))
CAP=50000; COMM=0.00175; RF=0.025

for mode,label in [("regime","牛熊切换"),("fixed8","固定N=8"),("fixed6","固定N=6"),("fixed10","固定N=10"),
                    ("fixed15","固定N=15"),("fixed30","固定N=30")]:
    nav,pos,cost_b,cash=[],{},{},CAP; fills=[]
    for dts in rd:
        dt=pd.Timestamp(dts); score=compute_score_a2(panel,dt,ap,info)
        if len(score)<6: continue

        if mode=="regime":
            N=get_position_size(csi800,dt)
        else:
            N=int(mode.replace("fixed",""))

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
        nav.append((cash+mkv)/CAP); fills.append(len(pos))

    ns=pd.Series(nav,index=pd.DatetimeIndex([pd.Timestamp(d) for d in rd[:len(nav)]]))
    d=ns.pct_change().dropna(); t=d.add(1).prod()-1
    y=max(len(d)/(26),0.5); a=(1+t)**(1/y)-1; dd=(ns/ns.cummax()-1).min()
    sr=(d.mean()-RF/26)/d.std()*np.sqrt(26) if d.std()>0 else 0; wr=np.mean(d>0)

    # 分年OOS
    ns_oos=ns[ns.index>="2024-01-01"]
    do=ns_oos.pct_change().dropna(); to=do.add(1).prod()-1
    yo=max(len(do)/(26),0.5); ao=(1+to)**(1/yo)-1
    ddo=(ns_oos/ns_oos.cummax()-1).min()

    flag="⭐" if mode=="regime" else ""
    bull_pct=(np.array(fills)==6).mean()*100 if mode=="regime" else 0
    print(f"  {label:<14} 全期{a:+.1%} OOS{ao:+.1%} 夏普{sr:.2f} 回撤{dd:.1%} OOS回撤{ddo:.1%} "
          f"{'牛'+str(int(bull_pct))+'%' if mode=='regime' else ''} {flag}")

print(f"\n总耗时: {(datetime.now()-t0).seconds}s")
EOF