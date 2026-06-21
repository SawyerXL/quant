"""
CSI500 N=6 止损 A/B (v2, 建在已对账验证的按市价记账引擎上)。
修正点: 复用 recon 的正确市价记账; 缓存每个调仓日打分(4变体复用); 预算MA10矩阵提速。
自检: base 变体必须复现 OOS≈+35.3% / 全期回撤≈-73%, 否则引擎仍有问题。
变体: base / ma10_3d(连破10线3天) / hard15(单票-15%) / both
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
CAP=50000; COMM=0.00175; RF=0.025; N=6; HARD=-0.15; MA_DAYS=3
ma10_df=panel.rolling(10).mean()   # 预算MA10矩阵, 日级查O(1)

# 缓存每个调仓日的 top-N (一次打分, 4变体复用)
print("缓存调仓日打分...", flush=True)
topN={}
for dts in rd:
    dt=pd.Timestamp(dts); sc=compute_score_a2(panel,dt,ap,info)
    if len(sc)>=N: topN[dt]=sc.nlargest(N).index.tolist()
print(f"缓存完成: {len(topN)}个调仓日, 用时{(datetime.now()-t0).seconds}s", flush=True)

def px(c,dt):
    try:
        p=float(panel[c].loc[dt]); return p if p>0 else np.nan
    except Exception: return np.nan

def run(stop_mode):
    pos,ep,below,cash={}, {}, {}, CAP; rec=[]
    def liq(c,dt):
        nonlocal cash
        p=px(c,dt); p=ep[c] if np.isnan(p) else p
        cash+=pos[c]*p*(1-COMM); pos.pop(c); ep.pop(c,None); below.pop(c,None)
    for dt in td:
        if stop_mode!="base" and pos:
            for c in list(pos.keys()):
                p=px(c,dt)
                if np.isnan(p): continue
                hit=False
                if stop_mode in ("hard15","both") and p/ep[c]-1<=HARD: hit=True
                if stop_mode in ("ma10_3d","both"):
                    m=ma10_df[c].loc[dt] if c in ma10_df.columns else np.nan
                    if not np.isnan(m):
                        below[c]=below.get(c,0)+1 if p<m else 0
                        if below[c]>=MA_DAYS: hit=True
                if hit: liq(c,dt)
        if dt in rd_set and dt in topN:
            top=topN[dt]
            for c in list(pos.keys()):
                if c not in top: liq(c,dt)
            need=N-len(pos); cand=[c for c in top if c not in pos]
            per=cash/max(need,1) if need>0 else 0
            for c in cand[:need]:
                p=px(c,dt)
                if np.isnan(p): continue
                q=max(int(min(per,CAP*0.3)/p/100)*100,100); cost=q*p
                if cost*(1+COMM)<=cash: cash-=cost*(1+COMM); pos[c]=q; ep[c]=p; below[c]=0
            mkv=sum(pos[c]*px(c,dt) if not np.isnan(px(c,dt)) else pos[c]*ep[c] for c in pos)
            rec.append((dt,(cash+mkv)/CAP))
    return pd.Series([v for _,v in rec], index=pd.DatetimeIndex([d for d,_ in rec]))

def metrics(ns):
    d=ns.pct_change().dropna(); t=ns.iloc[-1]/ns.iloc[0]-1
    y=max(len(d)/26,0.5); a=(1+t)**(1/y)-1
    s=(d.mean()-RF/26)/d.std()*np.sqrt(26) if d.std()>0 else 0; dd=(ns/ns.cummax()-1).min()
    return a,s,dd

print(f"\n{'='*78}\n  CSI500 N=6 止损 A/B v2 (按市价记账, 双边成本{COMM:.3%})\n{'='*78}")
print(f"  {'变体':<12}{'全期年化':>10}{'全期夏普':>9}{'全期回撤':>9}{'OOS年化':>10}{'OOS夏普':>9}{'OOS回撤':>9}")
print(f"  {'-'*74}")
base_oos=None
for mode,lab in [("base","无止损(基线)"),("ma10_3d","+MA10连破3"),("hard15","+硬止损-15%"),("both","两者都加")]:
    ns=run(mode); a,s,dd=metrics(ns)
    no=ns[ns.index>="2024-01-01"]; ao,so,ddo=metrics(no)
    if mode=="base": base_oos=ao
    print(f"  {lab:<12}{a:>+9.1%}{s:>9.2f}{dd:>8.1%}{ao:>+9.1%}{so:>9.2f}{ddo:>8.1%}", flush=True)
print(f"{'='*78}")
chk="✅引擎自检通过(base≈+35.3%)" if base_oos and abs(base_oos-0.353)<0.05 else f"⚠️base OOS={base_oos:+.1%} 偏离+35.3%,引擎仍需查"
print(f"  {chk}   总耗时: {(datetime.now()-t0).seconds}s")
