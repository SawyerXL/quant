"""
Track A 生存者偏差 A/B (CSI800代理池, baostock含退市数据)。
唯一变量: 候选池是否含退市股。
  Arm B(干净): 全池含退市, 持仓股退市→按最后价强平(如实计退市亏损)
  Arm A(仅幸存): 候选池只留活到2025的, 模拟survivor-only回测
因子=6-1动量top30, 双周调仓(Track A节奏), 按市价记账/双边成本/真实日期NAV。
差值 = Track A 的真实生存者偏差。
"""
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pandas as pd, numpy as np
from datetime import datetime
from data.storage import load_meta
import warnings; warnings.filterwarnings('ignore')

t0=datetime.now(); print(f"开始 {t0.strftime('%H:%M:%S')}",flush=True)
N=30; CAP=50000.0; COMM=0.00175; RF=0.025; MOM_LONG=126; MOM_SKIP=21
uni=load_meta("csi800_universe_bs"); uni["codes"]=uni["codes"].fillna("")
snap={pd.Timestamp(r["date"]):[c for c in r["codes"].split(",") if c] for _,r in uni.iterrows() if r["count"]>0}
snap_dates=sorted(snap); all_codes=sorted({c for cs in snap.values() for c in cs})
BS=Path("data_store/baostock/daily"); ser={}; last_date={}
for c in all_codes:
    f=BS/f"{c}.parquet"
    if not f.exists(): continue
    d=pd.read_parquet(f,columns=["date","close"]); idx=pd.to_datetime(d["date"])
    s=pd.to_numeric(d.set_index(idx)["close"],errors="coerce"); s=s[s>0]
    if len(s)>MOM_LONG: ser[c]=s; last_date[c]=s.index.max()
panel=pd.DataFrame(ser).sort_index()
survivors={c for c,ld in last_date.items() if ld>=pd.Timestamp("2025-06-01")}
print(f"价格宽表 {panel.shape[0]}天 × {panel.shape[1]}只; 幸存 {len(survivors)}, 退市/停 {panel.shape[1]-len(survivors)}",flush=True)
td=panel.index; rd=[]
for yr in range(2019,2026):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)>=8: rd.extend([m[len(m)//2],m[-1]])
rd=sorted(set(rd))
def snap_for(dt):
    e=[s for s in snap_dates if s<=dt]; return snap[e[-1]] if e else []
def px(c,dt):
    try:
        v=panel.at[dt,c]; return float(v) if (pd.notna(v) and v>0) else np.nan
    except Exception: return np.nan
def last_px(c,dt):
    if c not in panel.columns: return np.nan
    s=panel[c].loc[:dt].dropna(); return float(s.iloc[-1]) if len(s) else np.nan

def run(survivor_only):
    pos,entry,cash={}, {}, CAP; rec=[]
    for dt in rd:
        pool=set(snap_for(dt))
        if survivor_only: pool &= survivors
        scored=[]
        for c in pool:
            if c not in panel.columns: continue
            p=px(c,dt); s=panel[c].loc[:dt].dropna(); s=s[s>0]
            if np.isnan(p) or len(s)<MOM_LONG: continue
            po=s.iloc[-MOM_LONG]; ps=s.iloc[-MOM_SKIP] if len(s)>=MOM_SKIP else p
            if po>0: scored.append((ps/po-1,c))
        scored.sort(key=lambda x:(-x[0],x[1])); top=set(c for _,c in scored[:N]); order=[c for _,c in scored[:N]]
        for c in list(pos.keys()):
            pn=px(c,dt)
            if c not in top or np.isnan(pn):
                sp=pn if not np.isnan(pn) else last_px(c,dt); sp=entry[c] if np.isnan(sp) else sp
                cash+=pos[c]*sp*(1-COMM); pos.pop(c); entry.pop(c,None)
        need=N-len(pos)
        for c in [x for x in order if x not in pos][:need]:
            p=px(c,dt)
            if np.isnan(p): continue
            q=max(int(min(cash/max(need,1),CAP*0.3)/p/100)*100,100); cost=q*p
            if cost*(1+COMM)<=cash: cash-=cost*(1+COMM); pos[c]=q; entry[c]=p
        mkv=sum(pos[c]*(px(c,dt) if not np.isnan(px(c,dt)) else last_px(c,dt)) for c in pos)
        rec.append((dt,(cash+mkv)/CAP))
    return pd.Series([v for _,v in rec],index=pd.DatetimeIndex([d for d,_ in rec]))
def metrics(s):
    d=s.pct_change().dropna(); t=s.iloc[-1]/s.iloc[0]-1; y=max(len(d)/26,0.5)
    a=(1+t)**(1/y)-1; sr=(d.mean()-RF/26)/d.std()*np.sqrt(26) if d.std()>0 else 0
    return a,sr,(s/s.cummax()-1).min()

print("="*70)
print(f"{'变体':<16}{'全期年化':>10}{'全期夏普':>9}{'全期回撤':>9}{'OOS年化':>10}{'OOS回撤':>9}")
res={}
for lab,so in [("B 含退市(干净)",False),("A 仅幸存(有偏)",True)]:
    ns=run(so); a,sr,dd=metrics(ns); no=ns[ns.index>="2024-01-01"]; ao,_,ddo=metrics(no)
    res[lab]=(a,ao); print(f"{lab:<16}{a:>+9.1%}{sr:>9.2f}{dd:>8.1%}{ao:>+9.1%}{ddo:>8.1%}",flush=True)
(ba,bo)=res["B 含退市(干净)"]; (aa,ao_)=res["A 仅幸存(有偏)"]
print("="*70)
print(f"生存者偏差(仅幸存 − 含退市): 全期 {(aa-ba)*100:+.1f}pp/年, OOS {(ao_-bo)*100:+.1f}pp/年")
print(f"耗时{(datetime.now()-t0).seconds}s")
