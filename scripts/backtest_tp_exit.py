"""
TP1之后剩余仓位最优退出策略回测。
固定: TOP30等权+MA200+MA10+V2止损+TP1(+30%卖1/3)
变量: TP1后剩余2/3的退出规则, 比哪个累计利润最高。
"""
import sys; sys.path.insert(0,'scripts'); sys.path.insert(0,'.')
import pandas as pd, numpy as np
from data.storage import load_meta
from run_backtest_a import load_panels
from run_backtest_a2 import _make_rebal_dates, get_position_ratio

START,END='2019-01-01','2026-08-12'
N=30; STOP=-0.15; TP1=0.30; COMM=0.0013; RF=0.02

cal=load_meta('trade_calendar')
tdays=[d for d in cal['trade_date'] if START<=d<=END]
rebal=_make_rebal_dates(tdays,'biweekly')
codes=sorted(load_meta('csi800')['code'])
print('loading...')
panel,amt=load_panels(codes,START,END)
idx=load_meta('csi800_index'); idx['date']=pd.to_datetime(idx['date'])
idxc=pd.to_numeric(idx.set_index('date')['close'],errors='coerce').dropna()

def sel_top(ap,dt,n):
    h=ap[ap.index<=dt].iloc[-20:].mean().dropna()
    return h.nlargest(n).index.tolist()

def sim(panel, ap, rbd, idxc, tp_exit_rule, label):
    """tp_exit_rule(code,price,cost,entry_date,today_idx,post_tp1_peak) -> (sell_frac, new_peak) or (0,peak)"""
    dates=panel.index; rset=set(rbd)
    pr=pd.Series(0.0,index=dates)
    hld={}; tp1_done=set(); tp_exit_peaks={}; exit_dates={}
    pr2=1.0; tp_profit_total=0; exit_count=0
    for i,dt in enumerate(dates):
        ds=str(dt.date()); cur=panel.iloc[i]; prev=panel.iloc[i-1] if i>0 else None
        # MTM
        if hld and i>0:
            r=sum(h['w']*(cur.get(c,prev.get(c,0))/prev.get(c,1)-1) for c,h in hld.items() if prev.get(c) and cur.get(c) and prev[c]>0)
            pr.iloc[i]+=r
        cash=max(0,1.0-sum(h['w'] for h in hld.values()))
        pr.iloc[i]+=cash*RF/252
        # Update peaks
        for c,h in hld.items():
            cp=cur.get(c)
            if cp and not pd.isna(cp) and cp>0: h['peak']=max(h['peak'],cp)
        # V2 stop
        for c in list(hld):
            h=hld[c]; cp=cur.get(c)
            if cp and not pd.isna(cp) and cp>0 and cp/h['cost']-1<=STOP:
                pr.iloc[i]-=h['w']*COMM; tp1_done.discard(c); tp_exit_peaks.pop(c,None)
                del hld[c]
        # TP1 check
        for c in list(hld):
            if c in tp1_done: continue
            h=hld[c]; cp=cur.get(c)
            if cp and not pd.isna(cp) and cp/h['cost']-1>=TP1:
                sell_frac=h['w']/3; hld[c]['w']-=sell_frac
                pr.iloc[i]-=sell_frac*COMM
                tp_profit_total+=sell_frac*pr2
                tp1_done.add(c); tp_exit_peaks[c]=cp
                exit_dates[c]=i
                if hld[c]['w']<0.001: del hld[c]
        # Post-TP1 exit rule (the variable)
        for c in list(hld):
            if c not in tp1_done: continue
            h=hld[c]; cp=cur.get(c)
            if not cp or pd.isna(cp) or cp<=0: continue
            peak=tp_exit_peaks.get(c,h['peak'])
            frac,new_peak=tp_exit_rule(c,cp,h['cost'],exit_dates[c],i,peak)
            if frac>0:
                sell_frac=h['w']*frac; hld[c]['w']-=sell_frac
                pr.iloc[i]-=sell_frac*COMM
                tp_profit_total+=sell_frac*pr2
                exit_count+=1
                if new_peak>0: tp_exit_peaks[c]=new_peak
                if hld[c]['w']<0.001: del hld[c]; tp1_done.discard(c); tp_exit_peaks.pop(c,None)
        # Rebalance
        if ds in rset and i>=250:
            pos_ratio=get_position_ratio(idxc,dt) if idxc is not None else 1.0
            if pos_ratio<=0.3: hld={}; tp1_done.clear(); tp_exit_peaks.clear()
            else:
                sel=sel_top(ap,dt,N)
                if len(sel)>=N:
                    w=pos_ratio/N; old_w={c:h['w'] for c,h in hld.items()}
                    nh={}
                    for c in sel:
                        cp=float(cur.get(c,0))
                        if cp<=0 or pd.isna(cp): continue
                        if c in hld: h=hld[c]; h['w']=w; nh[c]=h
                        else: nh[c]={'cost':cp,'peak':cp,'w':w}
                    enter=sum(nh.get(c,{}).get('w',0) for c in set(nh)-set(old_w))
                    exit_=sum(old_w.get(c,0) for c in set(old_w)-set(nh))
                    pr.iloc[i]-=(enter+exit_)/2*COMM*2
                    hld=nh
                    # rebalance clears stocks not in top30; reset TP tracking for those
                    tp1_done&={c for c in nh};
                    tp_exit_peaks={c:v for c,v in tp_exit_peaks.items() if c in nh}
    return (1+pr).cumprod(), tp_profit_total, exit_count

# --- Exit rules ---
def exit_none(c,p,cost,entry_i,i,peak): return 0, peak  # 现行: 等TP2

def exit_tp2(c,p,cost,entry_i,i,peak):  # +60%
    if p/cost-1>=0.60: return 1.0, peak
    return 0, peak

def exit_time_days(days):
    def f(c,p,cost,entry_i,i,peak):
        if i-entry_i>=days: return 1.0, peak
        return 0, peak
    return f

def exit_trail(drop_pct):
    def f(c,p,cost,entry_i,i,peak):
        if (peak-p)/cost>=drop_pct: return 1.0, peak
        return 0, max(peak,p)
    return f

def exit_fixed(target):
    def f(c,p,cost,entry_i,i,peak):
        if p/cost-1>=target: return 1.0, peak
        return 0, peak
    return f

variants=[
    ('TP1后等TP2(+60%)', exit_tp2),
    ('TP1全清(不再等)', lambda c,p,cost,e,i,pk: (1.0,pk)),
    ('固定+45%清', exit_fixed(0.45)),
    ('固定+50%清', exit_fixed(0.50)),
    ('回撤-8%清', exit_trail(0.08)),
    ('回撤-12%清', exit_trail(0.12)),
    ('20天后全清', exit_time_days(20)),
    ('30天后全清', exit_time_days(30)),
    ('40天后全清', exit_time_days(40)),
]

def M(nav):
    nav=nav.dropna(); tr=nav.iloc[-1]/nav.iloc[0]-1
    days=(nav.index[-1]-nav.index[0]).days
    ann=(1+tr)**(365/max(days,1))-1
    r=nav.pct_change().dropna(); vol=r.std()*np.sqrt(252)
    sharpe=(ann-0.02)/vol if vol>0 else 0
    mdd=((nav-nav.cummax())/nav.cummax()).min()
    return ann,sharpe,mdd

h1='退出策略'; h2='年化'; h3='夏普'; h4='回撤'; h5='TP利润'; h6='退出次数'
print(f'\n{h1:22}{h2:>7}{h3:>6}{h4:>7}{h5:>10}{h6:>7}')
print('='*68)
for name,rule in variants:
    nav,tp_profit,exits=sim(panel,amt,rebal,idxc,rule,name)
    a,s,d=M(nav)
    print(f'{name:22}{a:>6.1%}{s:>6.2f}{d:>6.1%}{tp_profit:>10.3f}{exits:>7}')
