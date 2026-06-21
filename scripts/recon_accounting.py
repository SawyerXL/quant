"""
对账: 同一选股逻辑下, "按成本卖"(原引擎) vs "按市价卖"(正确) 对 N=6 收益的影响。
唯一变量=卖出记账方式。rd-only,快。验证 +39.6% 是否为会计漏洞造成。
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
print(f"CSI500: {panel.shape[1]}只 {panel.shape[0]}天", flush=True)
td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd)); CAP=50000; COMM=0.00175; RF=0.025; N=6

def price(c,dt):
    try:
        p=float(panel[c].loc[dt]); return p if p>0 else np.nan
    except Exception: return np.nan

# 两套账本并行
cost_pos,cost_cb,cost_cash={}, {}, CAP        # 原版: pos[c]=花的钱, 卖按成本退
mkt_pos,mkt_ep,mkt_cash={}, {}, CAP           # 正确: pos[c]=股数, 卖按市价
nav_cost,nav_mkt=[],[]

for dts in rd:
    dt=pd.Timestamp(dts); score=compute_score_a2(panel,dt,ap,info)
    if len(score)<N: continue
    top=score.nlargest(N).index.tolist()

    # —— 账本A: 按成本卖(完全复刻 regime) ——
    for c in list(cost_pos.keys()):
        if c not in top: cost_cash+=cost_pos[c]; cost_pos.pop(c); cost_cb.pop(c,None)
    need=N-len(cost_pos); cand=[c for c in top if c not in cost_pos]
    per=cost_cash/max(need,1) if need>0 else 0
    for c in cand[:need]:
        p=price(c,dt)
        if np.isnan(p): continue
        q=max(int(min(per,CAP*0.3)/p/100)*100,100); cost=q*p
        if cost<=cost_cash: cost_cash-=cost*(1+COMM); cost_pos[c]=cost; cost_cb[c]=p
    mkv=sum(cost_pos[c]*(price(c,dt)/cost_cb[c]) if not np.isnan(price(c,dt)) and cost_cb.get(c,0)>0 else cost_pos[c] for c in cost_pos)
    nav_cost.append((cost_cash+mkv)/CAP)

    # —— 账本B: 按市价卖(股数记账) ——
    for c in list(mkt_pos.keys()):
        if c not in top:
            p=price(c,dt); p=mkt_ep[c] if np.isnan(p) else p
            mkt_cash+=mkt_pos[c]*p*(1-COMM); mkt_pos.pop(c); mkt_ep.pop(c,None)
    need=N-len(mkt_pos); cand=[c for c in top if c not in mkt_pos]
    per=mkt_cash/max(need,1) if need>0 else 0
    for c in cand[:need]:
        p=price(c,dt)
        if np.isnan(p): continue
        q=max(int(min(per,CAP*0.3)/p/100)*100,100); cost=q*p
        if cost*(1+COMM)<=mkt_cash: mkt_cash-=cost*(1+COMM); mkt_pos[c]=q; mkt_ep[c]=p
    mkv=sum(mkt_pos[c]*price(c,dt) if not np.isnan(price(c,dt)) else mkt_pos[c]*mkt_ep[c] for c in mkt_pos)
    nav_mkt.append((mkt_cash+mkv)/CAP)

idx=pd.DatetimeIndex([pd.Timestamp(d) for d in rd[:len(nav_cost)]])
def metrics(nav):
    ns=pd.Series(nav,index=idx); d=ns.pct_change().dropna()
    t=ns.iloc[-1]/ns.iloc[0]-1; y=max(len(d)/26,0.5); a=(1+t)**(1/y)-1
    s=(d.mean()-RF/26)/d.std()*np.sqrt(26) if d.std()>0 else 0; dd=(ns/ns.cummax()-1).min()
    no=ns[ns.index>="2024-01-01"]; do=no.pct_change().dropna()
    to=no.iloc[-1]/no.iloc[0]-1; yo=max(len(do)/26,0.5); ao=(1+to)**(1/yo)-1
    ddo=(no/no.cummax()-1).min()
    return a,s,dd,ao,ddo
print(f"\n{'='*70}\n  N=6 对账: 卖出记账方式的影响 (其余完全相同)\n{'='*70}")
print(f"  {'记账方式':<18}{'全期年化':>10}{'全期回撤':>10}{'OOS年化':>10}{'OOS回撤':>10}")
for lab,nav in [("按成本卖(原引擎)",nav_cost),("按市价卖(正确)",nav_mkt)]:
    a,s,dd,ao,ddo=metrics(nav)
    print(f"  {lab:<18}{a:>+9.1%}{dd:>9.1%}{ao:>+9.1%}{ddo:>9.1%}", flush=True)
print(f"{'='*70}\n  耗时: {(datetime.now()-t0).seconds}s")
