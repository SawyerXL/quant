"""
CSI500 N=6 加止损 A/B 回测（按股数正确记账，卖出按当时市价+双边成本）。
唯一变量=止损规则。输入严格沿用 backtest_csi500_regime.py。
变体: base(无止损) / ma10_3d(连破10日线3天) / hard15(单票-15%硬止损) / both
用法: python scripts/backtest_csi500_stop_ab.py
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
rd=sorted(set(rd)); rd_set=set(rd)
CAP=50000; COMM=0.00175; RF=0.025; N=6; HARD=-0.15; MA_N=10; MA_DAYS=3

def price(c,dt):
    try:
        p=float(panel[c].loc[dt]); return p if p>0 else np.nan
    except Exception: return np.nan

def ma10(c,dt):
    s=panel[c].loc[:dt].dropna()
    return s.iloc[-MA_N:].mean() if len(s)>=MA_N else np.nan

def run(stop_mode):
    # 按股数记账: pos[c]=shares, entry[c]=买入价, below[c]=连续跌破MA10天数
    pos,entry,below,cash={}, {}, {}, CAP
    nav=[]; sell_turnover=0.0
    def liquidate(c,dt):
        nonlocal cash,sell_turnover
        p=price(c,dt)
        if np.isnan(p): p=entry[c]              # 停牌按成本估,不凭空赚亏
        val=pos[c]*p; cash+=val*(1-COMM); sell_turnover+=val
        pos.pop(c); entry.pop(c,None); below.pop(c,None)

    for dt in td:
        # —— 每日止损检查(仅在有持仓且开启时) ——
        if stop_mode!="base" and pos:
            for c in list(pos.keys()):
                p=price(c,dt)
                if np.isnan(p): continue
                hit=False
                if stop_mode in ("hard15","both") and p/entry[c]-1<=HARD: hit=True
                if stop_mode in ("ma10_3d","both"):
                    m=ma10(c,dt)
                    if not np.isnan(m):
                        below[c]=below.get(c,0)+1 if p<m else 0
                        if below[c]>=MA_DAYS: hit=True
                if hit: liquidate(c,dt)
        # —— 调仓日 ——
        if dt in rd_set:
            score=compute_score_a2(panel,dt,ap,info)
            if len(score)>=N:
                top=score.nlargest(N).index.tolist()
                for c in list(pos.keys()):
                    if c not in top: liquidate(c,dt)
                need=N-len(pos); cand=[c for c in top if c not in pos]
                per=cash/max(need,1) if need>0 else 0
                for c in cand[:need]:
                    p=price(c,dt)
                    if np.isnan(p): continue
                    q=max(int(min(per,CAP*0.3)/p/100)*100,100); cost=q*p
                    if cost*(1+COMM)<=cash:
                        cash-=cost*(1+COMM); pos[c]=q; entry[c]=p; below[c]=0
            mkv=sum(pos[c]*price(c,dt) if not np.isnan(price(c,dt)) else pos[c]*entry[c] for c in pos)
            nav.append((cash+mkv)/CAP)
    ns=pd.Series(nav,index=pd.DatetimeIndex([pd.Timestamp(d) for d in rd[:len(nav)]]))
    return ns, sell_turnover

def metrics(ns):
    d=ns.pct_change().dropna(); t=ns.iloc[-1]/ns.iloc[0]-1
    y=max(len(d)/26,0.5); a=(1+t)**(1/y)-1
    s=(d.mean()-RF/26)/d.std()*np.sqrt(26) if d.std()>0 else 0
    dd=(ns/ns.cummax()-1).min()
    return a,s,dd

print(f"\n{'='*78}\n  CSI500 N=6 止损 A/B (按股数正确记账, 双边成本{COMM:.3%})\n{'='*78}")
print(f"  {'变体':<12}{'全期年化':>10}{'全期夏普':>9}{'全期回撤':>9}{'OOS年化':>10}{'OOS夏普':>9}{'OOS回撤':>9}")
print(f"  {'-'*74}")
for mode,lab in [("base","无止损(基线)"),("ma10_3d","+MA10连破3"),("hard15","+硬止损-15%"),("both","两者都加")]:
    ns,_=run(mode); a,s,dd=metrics(ns)
    no=ns[ns.index>="2024-01-01"]; ao,so,ddo=metrics(no)
    print(f"  {lab:<12}{a:>+9.1%}{s:>9.2f}{dd:>8.1%}{ao:>+9.1%}{so:>9.2f}{ddo:>8.1%}", flush=True)
print(f"{'='*78}\n  总耗时: {(datetime.now()-t0).seconds}s")
