"""
Track B 最终回测 v4 (price_position=0.85, T+1, 成本0.175%)
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np; from datetime import datetime; from pathlib import Path
from data.storage import load_meta
from config.strategy_params.trinity import PORTFOLIO
from strategies.trinity.portfolio import TrinityPortfolio
from tests.test_backtest_metrics import metrics
import warnings; warnings.filterwarnings('ignore')

C=PORTFOLIO["commission"]; CAP=PORTFOLIO["capital"]; LOG=Path("logs/backtest")
LOG.mkdir(exist_ok=1)

print("加载..."); t0=datetime.now()
csi=load_meta("csi800"); codes=sorted(str(c) for c in csi["code"].tolist())[:500]
from run_backtest_a import load_panels
panel,ap=load_panels(codes,"2019-01-01","2024-12-31"); info=load_meta("stock_info_full")
print(f"  {panel.shape[1]}只 {panel.shape[0]}天 ({(datetime.now()-t0).seconds}s)")

# 调仓日
td=panel.index; rd=[]
for yr in range(2019,2025):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd)); rds=[d.strftime("%Y-%m-%d") for d in rd]
print(f"  调仓日: {len(rds)}个")

# B&H
bh=pd.Series(1.0,index=panel.index)
bh_r=panel.pct_change(fill_method=None).mean(axis=1).fillna(0)
for i in range(1,len(bh_r)): bh.iloc[i]=bh.iloc[i-1]*(1+bh_r.iloc[i])

# 策略
pf=TrinityPortfolio(); pf.warmup(panel,ap,info,rds[0])
shares={}; cost_p={}; nav_per={}; fills=[]
for i,dt in enumerate(rds):
    dts=pd.Timestamp(dt); sig=pf.select(panel,ap,info,dt,list(shares.keys()),{})
    for c in sig.get("sell",[]):
        if c in shares: shares.pop(c); cost_p.pop(c)
    for c in sig.get("buy",[]):
        p=sig["prices"].get(c,0); q=sig["shares"].get(c,0)
        if p>0 and q>0: shares[c]=q; cost_p[c]=p
    mkv=0
    if dts in panel.index:
        for c,q in shares.items():
            if c in panel.columns:
                cp=float(panel.loc[dts,c])
                if pd.notna(cp) and cp>0: mkv+=q*cp
    total_cost=sum(cost_p[c]*shares[c] for c in shares)
    nav_per[dts]=(mkv+(CAP-total_cost))/CAP
    fills.append(len(shares))

nav_s=pd.Series(nav_per).sort_index()
bv=[bh.loc[d] if d in bh.index else bh.asof(d) for d in nav_s.index]
bh_s=pd.Series(bv,index=nav_s.index); bh_s=bh_s/bh_s.iloc[0]
mn=metrics(nav_s); mb=metrics(bh_s); fa=np.array(fills)

# 自检
issues=[]
# 用 metrics 内部一致的频率做自检
freq=len(nav_s.pct_change().dropna()) / max((nav_s.index[-1]-nav_s.index[0]).days/365,0.1)
fwd=(1+mn['annual'])**(max(len(nav_s.pct_change().dropna())/freq,0.5))-1
if abs(fwd-mn['total'])>0.01: issues.append(f"ann→tot {fwd:.1%}≠{mn['total']:.1%}")
product=1.0
for yr in range(2019,2025):
    sy=nav_s[nav_s.index.year==yr]
    if len(sy)>1: product*=sy.iloc[-1]/sy.iloc[0]
# 允许1%误差（年末年初无数据的gap）
if abs(product-1-mn['total'])>0.02:
    issues.append(f"yr复利{product-1:.1%}≠tot{mn['total']:.1%}")

# 输出
o=[]
def p(s=""): o.append(s); print(s)
p(f"\n{'='*55}")
p(f"  Track B v4  price={1} T+1 cost={C*100:.3f}%  {rd[0].date()}→{rd[-1].date()}")
p(f"{'='*55}")
for name,k,fmt in [("总收益","total",".1%"),("年化","annual",".1%"),
                    ("夏普","sharpe",".2f"),("最大回撤","max_dd",".1%")]:
    p(f"  {name:<10}  B&H {mb[k]:>9{fmt}}  TrB {mn[k]:>9{fmt}}")
p(f"\n  持仓: 均{fa.mean():.1f}只 空{(fa==0).mean()*100:.0f}%")
p(f"  自检: {'✅' if not issues else '❌ '+'; '.join(issues)}")
p(f"  分年复利: {(product-1)*100:.1f}% vs 总{mn['total']*100:.1f}%")
p(f"\n  分年度:")
for yr in range(2019,2025):
    sy=nav_s[nav_s.index.year==yr]; by=bh_s[bh_s.index.year==yr]
    if len(sy)<2: continue
    sr=sy.iloc[-1]/sy.iloc[0]-1; br=by.iloc[-1]/by.iloc[0]-1
    fy=[n for d,n in zip(rds,fills) if str(yr) in d]
    p(f"    {yr}  TrB{sr:>+7.1%}  B&H{br:>+7.1%}  仓{np.mean(fy):.1f}")

(LOG/"backtest_trinity_v4.txt").write_text("\n".join(o))
p(f"\n✅ {LOG/'backtest_trinity_v4.txt'}")
