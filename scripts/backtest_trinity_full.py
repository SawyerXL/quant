"""
Track B 完整两层策略回测 (板块→个股, T+1执行, 计成本)

用法: python -W ignore scripts/backtest_trinity_full.py
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime
from data.storage import load_meta
from config.strategy_params.trinity import PORTFOLIO
from strategies.trinity.portfolio import TrinityPortfolio

START, END = "2019-01-01", "2024-12-31"
COMM = PORTFOLIO["commission"]; RF=0.025

print("加载数据...", flush=True); t0=datetime.now()
csi=load_meta("csi800"); codes=sorted([str(c) for c in csi["code"].tolist()])[:300]
from run_backtest_a import load_panels
panel,ap=load_panels(codes,START,END); info=load_meta("stock_info_full")
print(f"  {panel.shape[1]}只×{panel.shape[0]}天 ({int((datetime.now()-t0).total_seconds())}s)")

# 调仓日（双周）
td=panel.index
rd=[]; NAV=1.0; nav_s=pd.Series(dtype=float); fill_log=[]; holdings=[]
for yr in range(2019,2025):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.append(m[len(m)//2].strftime("%Y-%m-%d"))
        rd.append(m[-1].strftime("%Y-%m-%d"))
rd=sorted(set(rd))
print(f"调仓日: {len(rd)}  {rd[0]}→{rd[-1]}")

# 回测（单实例保持状态机记忆）
pf=TrinityPortfolio(); cash=PORTFOLIO["capital"]; total=cash
positions={}; cost_basis={}; fills=[]; cash_series=[]

# 预热：回放T-120日状态机
if rd:
    pf.warmup(panel,ap,info,rd[0])
    print(f"  预热完成，状态机从第一个调仓日起可用")

for i,d_str in enumerate(rd):
    try:
        sig=pf.select(panel,ap,info,d_str,list(positions.keys()))
    except Exception as e:
        fills.append(f"{d_str}: ERR {str(e)[:40]}")
        continue

    # 计算持仓收益（T+1: 用前一周期结束到现在的收益）
    if i>0:
        seg=panel[rd[i-1]:d_str]
        if len(seg)>1:
            rets=seg.pct_change().dropna(how='all')
            for c in list(positions.keys()):
                if c in rets.columns:
                    r=rets[c].prod() if len(rets)>1 else 0
                    total+=positions[c]*r

    # 卖出
    for c in sig.get("sell",[]):
        if c in positions:
            total-=positions[c]*COMM
            cash+=positions[c]
            positions.pop(c); cost_basis.pop(c,None)

    # 买入
    buys=sig.get("buy",[])
    per_cash=cash/max(len(buys),1) if buys else 0
    for c in buys:
        price=sig["prices"].get(c,0)
        shares=sig["shares"].get(c,0)
        cost=price*shares
        if cost>per_cash: shares=int(per_cash/price/100)*100; cost=price*shares
        if cost>0 and cost<=cash:
            cash-=cost; positions[c]=cost; cost_basis[c]=price; total-=cost*COMM

    n_fill=len(positions); pct=n_fill/PORTFOLIO["max_stocks"]
    fills.append({"date":d_str,"n":n_fill,"pct":pct,"cash_pct":cash/(total+cash)})
    fill_log.append(n_fill)
    cash_series.append(cash/(total+cash))

# 简化统计
print(f"\n{'='*55}")
print(f"  Track B 两层策略回测 {START}→{END}")
print(f"{'='*55}")
fills_df=pd.DataFrame(fills); fills_df["date"]=pd.to_datetime(fills_df["date"])
avg_fill=fills_df["n"].mean()
full_pct=(fills_df["n"]>=6).mean()*100; part_pct=(fills_df["n"].between(1,5)).mean()*100
empty_pct=(fills_df["n"]==0).mean()*100

print(f"  平均持仓: {avg_fill:.1f}只")
print(f"  满仓(6只): {full_pct:.0f}%  部分仓: {part_pct:.0f}%  空仓: {empty_pct:.0f}%")
print(f"  平均现金比: {np.mean(cash_series)*100:.0f}%")

# 分年度
for yr in range(2019,2025):
    fy=fills_df[fills_df["date"].dt.year==yr]
    if fy.empty: continue
    print(f"  {yr}: 均仓{fy['n'].mean():.1f}只  空仓{(fy['n']==0).mean()*100:.0f}%")

print(f"\n  注意: 完整PnL追踪需更多工程(定价/换手/止损明细)")
print(f"  当前输出: 持仓统计+现金占比(验证过滤器自然清空候选池)\n")
