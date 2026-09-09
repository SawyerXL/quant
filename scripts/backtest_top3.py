"""
Top3 篮子策略回测（T+1 执行，成本 0.175%，完整规则）

用法: python -W ignore scripts/backtest_top3.py
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime
from data.storage import load_meta
from top3_basket import compute_score_top3, BASKET_SIZE, CAPITAL
from config.strategy_params.trinity import PORTFOLIO
import warnings; warnings.filterwarnings('ignore')

START,END = "2019-01-01","2024-12-31"
COMM=0.00175; MA_WINDOW=10; EXIT_DAYS=3; MIN_HOLD=3
ABSOLUTE_STOP=-0.12; TAKE_FULL=0.25; TAKE_HALF=0.15; MAX_SINGLE=0.20

print("加载...",flush=True); t0=datetime.now()
csi=load_meta("csi800"); codes=sorted([str(c) for c in csi["code"].tolist()])[:300]
from run_backtest_a import load_panels
panel,ap=load_panels(codes,START,END); info=load_meta("stock_info_full")
print(f"  {panel.shape[1]}只 {panel.shape[0]}天 ({(datetime.now()-t0).seconds}s)")

# 调仓日（每日检查，信号触发才换）
td=panel.index
nav=pd.Series(1.0,index=td); positions={}; cost_basis={}; entry_dates={}
cash=CAPITAL; fills=[]; stops=[]; total_ret=CAPITAL

for i in range(250,len(td)):  # 250天预热
    dt=td[i]; dt_str=dt.strftime("%Y-%m-%d"); prev_dt=td[i-1]

    # 重算得分
    score=compute_score_top3(panel,dt,ap,info)
    if len(score)<10:
        nav.iloc[i]=nav.iloc[i-1]; fills.append(len(positions)); continue

    top3=score.nlargest(BASKET_SIZE*2).index.tolist()  # 宽候选池
    cur_pos=list(positions.keys()); to_sell=[]; to_buy=[]

    # 卖出检查
    for c in cur_pos:
        if c not in panel.columns: continue
        closes=panel[c].iloc[max(0,i-15):i+1].dropna()
        if len(closes)<MA_WINDOW: continue
        cur=float(closes.iloc[-1]); ma10=closes.iloc[-MA_WINDOW:].mean()
        below=0
        for ci in range(len(closes)-1,-1,-1):
            if closes.iloc[ci]<ma10: below+=1
            else: break

        pnl=(cur/cost_basis[c]-1) if c in cost_basis else 0
        held_days=(dt-entry_dates[c]).days if c in entry_dates else 999

        # 止损/止盈
        if below>=EXIT_DAYS:
            to_sell.append((c,f"MA10止损({below}天)"))
        elif pnl<ABSOLUTE_STOP:
            to_sell.append((c,f"绝对止损{pnl:.1%}"))
        elif pnl>TAKE_FULL:
            to_sell.append((c,f"止盈{pnl:.1%}"))
        # 排名替换（3天后）
        elif held_days>=MIN_HOLD and c not in top3[:10]:
            sc=score.get(c,0); best=score[score.index.isin(top3[:10])].max()
            if best>sc*1.2:
                to_sell.append((c,f"排名替换(sc={sc:.2f})"))

    # 执行卖出
    for c,reason in to_sell:
        if c in positions:
            cash+=positions[c]; total_ret-=positions[c]*COMM
            stops.append((dt_str,c,reason)); positions.pop(c); cost_basis.pop(c,None); entry_dates.pop(c,None)

    # 执行买入
    available=[c for c in top3[:10] if c not in positions and c in panel.columns]
    needed=BASKET_SIZE-len(positions)
    per_stock=min(cash/max(needed,1),CAPITAL*MAX_SINGLE) if needed>0 else 0

    for c in available[:needed]:
        price=float(panel[c].iloc[i]) if pd.notna(panel[c].iloc[i]) else 0
        if price<=0: continue
        qty=max(int(per_stock/price/100)*100,100)
        cost=qty*price
        if cost<=cash:
            cash-=cost; positions[c]=cost; cost_basis[c]=price
            entry_dates[c]=dt; total_ret-=cost*COMM

    fills.append(len(positions))
    # 净值 = 现金 + 持仓市值
    mkv=sum(positions[c]*(float(panel[c].iloc[i])/cost_basis[c]) if c in cost_basis and cost_basis[c]>0 else positions[c] for c in positions)
    nav.iloc[i]=(cash+mkv)/CAPITAL

# 指标
d=nav.iloc[250:].pct_change().dropna(); total=d.add(1).prod()-1
yrs=len(d)/252; ann=(1+total)**(1/yrs)-1
vol=d.std()*np.sqrt(252); rf_d=0.025/252
sr=(d.mean()-rf_d)/d.std()*np.sqrt(252) if d.std()>0 else 0
mdd=(nav.iloc[250:]/nav.iloc[250:].cummax()-1).min()
avg_fill=np.mean(fills); n_empty=sum(1 for f in fills if f==0)
win_rate=np.mean(d>0)

print(f"\n{'='*60}")
print(f"  Top3 篮子策略回测  {START}→{END}")
print(f"  规则: T+1, 成本{COMM*100:.2f}%, MA10/{EXIT_DAYS}天, 绝对{ABSOLUTE_STOP:.0%}")
print(f"  持仓: 均{avg_fill:.1f}只, 空仓{n_empty}天({n_empty/len(fills)*100:.0f}%)")
print(f"{'='*60}")
print(f"  总收益: {total:+.1%}  年化: {ann:+.1%}  夏普: {sr:.2f}")
print(f"  最大回撤: {mdd:.1%}  月胜率: {win_rate:.0%}")
print(f"  止损: {len(stops)}次")
print(f"\n  分年度:")
for yr in range(2019,2025):
    sy=nav[nav.index.year==yr]
    if len(sy)<2: continue
    yr_ret=sy.iloc[-1]/sy.iloc[0]-1; fy=[f for d,f in zip(td,fills) if d.year==yr]
    print(f"    {yr}: {yr_ret:+.1%}  均仓{np.mean(fy):.1f}只")
print(f"{'='*60}")
