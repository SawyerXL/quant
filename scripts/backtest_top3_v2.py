"""
Top3 v2 回测：用主策略打分公式，选 Top3，双周调仓。
相比 v1: ① 打分改为稳健版 ② 双周固定调仓(非每日) ③ 简化规则
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime
from data.storage import load_meta
from run_backtest_a2 import compute_score_a2
import warnings; warnings.filterwarnings('ignore')

START,END = "2019-01-01","2024-12-31"
COMM=0.00175; MA=10; EXIT=3; CAP=50000; N=3; MAX_SINGLE=0.40

print("加载...",flush=True); t0=datetime.now()
csi=load_meta("csi800"); codes=sorted([str(c) for c in csi["code"].tolist()])[:500]
from run_backtest_a import load_panels
panel,ap=load_panels(codes,START,END); info=load_meta("stock_info_full")
print(f"  {panel.shape[1]}只 {panel.shape[0]}天 ({(datetime.now()-t0).seconds}s)")

# 调仓日（双周）
td=panel.index; rd=[]
for yr in range(2019,2025):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd)); rds=[d.strftime("%Y-%m-%d") for d in rd]

nav=[]; positions={}; cost_basis={}; cash=CAP; fills=[]; stops=[]; all_dates=panel.index

for i,dts in enumerate(rds):
    dt=pd.Timestamp(dts); score=compute_score_a2(panel,dt,ap,info)

    # 卖：MA10止损 + 调仓轮换
    to_sell=[]
    for c in list(positions.keys()):
        if c not in panel.columns: continue
        closes=panel[c].iloc[max(0,panel.index.get_loc(dt)-15):panel.index.get_loc(dt)+1].dropna()
        if len(closes)<MA: continue
        cur=float(closes.iloc[-1]); ma10=closes.iloc[-MA:].mean()
        below=0
        for ci in range(len(closes)-1,-1,-1):
            if closes.iloc[ci]<ma10: below+=1
            else: break
        pnl=(cur/cost_basis[c]-1) if c in cost_basis else 0
        if below>=EXIT: to_sell.append((c,f"MA10"))
        elif pnl<-0.15: to_sell.append((c,f"止损"))
        elif pnl>0.25: to_sell.append((c,f"止盈"))
        elif c not in score.nlargest(N*2).index: to_sell.append((c,f"轮换"))

    for c,reason in to_sell:
        if c in positions:
            cash+=positions[c]; positions.pop(c); cost_basis.pop(c,None)

    # 买
    candidates=[c for c in score.nlargest(N*2).index if c not in positions and c in panel.columns]
    needed=N-len(positions)
    per=max(min(cash/max(needed,1),CAP*MAX_SINGLE),10000) if needed>0 else 0

    for c in candidates[:needed]:
        price=float(panel[c].loc[dt]) if dt in panel[c].index else 0
        if price<=0: continue
        qty=max(int(per/price/100)*100,100); cost=qty*price
        if cost<=cash:
            cash-=cost; positions[c]=cost; cost_basis[c]=price

    # 市值
    mkv=0
    for c,cp in cost_basis.items():
        if c in panel.columns and dt in panel.index:
            cur_p=float(panel[c].loc[dt])
            if cur_p>0 and cp>0: mkv+=positions[c]*(cur_p/cp)
    nav.append((cash+mkv)/CAP); fills.append(len(positions))

# 指标
nav_s=pd.Series(nav,index=pd.DatetimeIndex([pd.Timestamp(d) for d in rds]))
d=nav_s.pct_change().dropna(); total=d.add(1).prod()-1
yrs=len(d)/(26); ann=(1+total)**(1/max(yrs,0.5))-1
vol=d.std()*np.sqrt(26); sr=(d.mean()-0.025/26)/d.std()*np.sqrt(26) if d.std()>0 else 0
mdd=(nav_s/nav_s.cummax()-1).min()

print(f"\n{'='*60}")
print(f"  Top3 v2 回测 (主策略公式, 双周调仓)")
print(f"  {rds[0]}→{rds[-1]}")
print(f"{'='*60}")
print(f"  总收益: {total:+.1%}  年化: {ann:+.1%}  夏普: {sr:.2f}")
print(f"  最大回撤: {mdd:.1%}")
print(f"\n  分年度:")
for yr in range(2019,2025):
    sy=nav_s[nav_s.index.year==yr]
    if len(sy)<2: continue
    yr_ret=sy.iloc[-1]/sy.iloc[0]-1; fy=[f for d,f in zip(rds,fills) if d.startswith(str(yr))]
    print(f"    {yr}: {yr_ret:+.1%}  均仓{np.mean(fy):.1f}只")
print(f"{'='*60}")
