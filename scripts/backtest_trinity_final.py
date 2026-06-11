"""
Track B 最终回测报告 (price_position=0.85, T+1, 成本0.175%)
输出: logs/backtest_trinity_report.txt
"""
import sys; sys.path.insert(0,'/root/quant'); sys.path.insert(0,'/root/quant/scripts')
import pandas as pd, numpy as np
from datetime import datetime
from pathlib import Path
from data.storage import load_meta
from config.strategy_params.trinity import PORTFOLIO, STOCK_SCORE
from strategies.trinity.portfolio import TrinityPortfolio
import warnings; warnings.filterwarnings('ignore')

START, END = "2019-01-01", "2024-12-31"
COMM = PORTFOLIO["commission"]
CAP  = PORTFOLIO["capital"]
RF   = 0.025
N_PANEL = 500
LOG_DIR = Path("logs/backtest"); LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── 数据 ──────────────────────────────────────────────
print("加载...", flush=True); t0=datetime.now()
csi=load_meta("csi800"); codes=sorted([str(c) for c in csi["code"].tolist()])[:N_PANEL]
from run_backtest_a import load_panels
panel,ap=load_panels(codes,START,END); info=load_meta("stock_info_full")
print(f"  {panel.shape[1]}只×{panel.shape[0]}天 {(datetime.now()-t0).seconds}s")

# 调仓日（双周）
td=panel.index; rd=[]
for yr in range(2019,2025):
    for mo in range(1,13):
        m=td[(td.year==yr)&(td.month==mo)]
        if len(m)<8: continue
        rd.append(m[len(m)//2]); rd.append(m[-1])
rd=sorted(set(rd)); rds=[d.strftime("%Y-%m-%d") for d in rd]

# ── 回测引擎 ──────────────────────────────────────────
pf=TrinityPortfolio()
nav, bh_nav = [1.0], [1.0]        # 净值：策略 vs 买入持有(平均候选股)
positions={}; cost_basis={}; entry_dates={}
fills=[]; trades=[]; stops=[]; daily_pos=pd.Series(dtype=float)
ma_below={}; cash_balance=CAP; total_assets=CAP

# 加权跟踪：对每只持仓股按等权计算组合收益
pnl_log=[]  # per holding per rebalance period pnl

for i in range(1, len(rds)):
    dt, prev_dt = rds[i], rds[i-1]

    # ── 调仓 ──
    sig = pf.select(panel,ap,info,dt,list(positions.keys()), ma_below)
    new_holdings = sig.get("holdings",[])
    sell_list    = sig.get("sell",[])
    buy_list     = sig.get("buy",[])
    prices       = sig.get("prices",{})
    shares_dict  = sig.get("shares",{})

    # ── 计算上期收益 → 更新净值 ──
    seg = panel[prev_dt:dt]
    seg_ret = 1.0
    for c in list(positions.keys()):
        if c in seg.columns and len(seg) > 1:
            r = (seg[c].iloc[-1] / seg[c].iloc[0] - 1) if seg[c].iloc[0] > 0 else 0
            seg_ret *= (1 + r * (positions.get(c,0)/total_assets))
    if positions:
        total_assets = total_assets * seg_ret

    # ── 卖出 ──
    for c in sell_list:
        if c in positions:
            total_assets -= positions[c] * COMM
            pnl = positions[c] - cost_basis.get(c, positions[c])
            hold_days = (pd.Timestamp(dt) - entry_dates.get(c, pd.Timestamp(prev_dt))).days
            trades.append({"code":c,"entry_date":str(entry_dates.get(c,"?")),
                          "exit_date":dt,"pnl":pnl,"hold_days":hold_days,
                          "exit_reason":"MA10止损" if c in sig.get("ma10_exits",[]) else "调仓卖出"})
            cash_balance += positions[c]; positions.pop(c); cost_basis.pop(c,None); entry_dates.pop(c,None)

    # ── 买入 ──
    for c in buy_list:
        price = prices.get(c,0); shares = shares_dict.get(c,0)
        cost = min(price*shares, cash_balance * PORTFOLIO["max_single_pct"])
        if cost > 0 and cost <= cash_balance and price > 0:
            cash_balance -= cost; positions[c]=cost; cost_basis[c]=cost
            entry_dates[c]=pd.Timestamp(dt); total_assets -= cost*COMM

    # ── MA10 止损 ──
    exits = sig.get("ma10_exits",[])
    for c in exits:
        if c in positions:
            total_assets -= positions[c] * COMM
            pnl = positions[c] - cost_basis.get(c, positions[c])
            stops.append({"code":c,"pnl":pnl,"entry":str(entry_dates.get(c,"?")),"exit":dt})
            cash_balance += positions[c]; positions.pop(c); cost_basis.pop(c,None); entry_dates.pop(c,None)

    total_assets = cash_balance + sum(positions.values())
    nav.append(total_assets / CAP)
    bh_seg = panel[prev_dt:dt]
    if len(bh_seg) > 1 and len(bh_seg.columns) > 10:
        bh_ret = bh_seg.iloc[-1].mean() / bh_seg.iloc[0].mean()
        bh_nav.append(bh_nav[-1] * bh_ret)
    else:
        bh_nav.append(bh_nav[-1])
    fills.append(len(positions)); daily_pos[dt] = len(positions)

# ── 指标函数 ──────────────────────────────────────────
nav_s=pd.Series(nav,index=[pd.Timestamp(d) for d in rds])
bh_s=pd.Series(bh_nav,index=nav_s.index)

def metrics(ns):
    d=ns.pct_change().dropna(); t=ns.iloc[-1]-1; y=max(len(d)/252,0.5)
    a=(1+t)**(1/y)-1; v=d.std()*np.sqrt(252)
    s=(d.mean()-RF/252)/d.std()*np.sqrt(252) if d.std()>0 else 0
    m=(ns/ns.cummax()-1).min()
    return t,a,v,s,m

tb,ab,vb,sb,db = metrics(nav_s)
tb_bh,ab_bh,vb_bh,sb_bh,db_bh = metrics(bh_s)
fills_arr=np.array(fills); avg_f=float(np.mean(fills_arr))
empty_pct = float(np.mean(np.array(fills_arr)==0)*100)

# ── 输出 ──────────────────────────────────────────────
report = []
def p(s=""): report.append(s); print(s)

p(f"\n{'='*65}")
p(f"  Track B 两层策略回测报告  {START}→{END}")
p(f"  price_position=0.85  T+1 成本={COMM*100:.3f}%  候池={N_PANEL}只")
p(f"{'='*65}")
p(f"\n  {'指标':<16} {'Buy&Hold':>12} {'Track B':>12}")
p(f"  {'─'*42}")
p(f"  {'总收益':<16} {tb:>+11.1%} {tb_bh:>+11.1%}")
p(f"  {'年化收益':<16} {ab:>+11.1%} {ab_bh:>+11.1%}")
p(f"  {'年化波动':<16} {vb:>11.1%} {vb_bh:>11.1%}")
p(f"  {'夏普比率':<16} {sb:>11.2f} {sb_bh:>11.2f}")
p(f"  {'最大回撤':<16} {db:>11.1%} {db_bh:>11.1%}")
p(f"\n  持仓诊断:")
p(f"    平均持仓: {avg_f:.1f}只  空仓占比: {empty_pct:.0f}%")
p(f"    止损次数: {len(stops)}  止损占比: {len(stops)/max(len(trades),1)*100:.0f}%")

# 分年度
p(f"\n  分年度:")
for yr in range(2019,2025):
    sy=nav_s[nav_s.index.year==yr]; by=bh_s[bh_s.index.year==yr]
    if len(sy)<2: continue
    s_ret=sy.iloc[-1]/sy.iloc[0]-1; b_ret=by.iloc[-1]/by.iloc[0]-1
    fy=[n for d,n in zip(rds,fills) if d.startswith(str(yr))]
    avg_y=float(np.mean(fy)) if fy else 0
    p(f"    {yr}  Track B{s_ret:>+7.1%}  B&H{b_ret:>+7.1%}  均仓{avg_y:.1f}只")

# Track A 相关性
p(f"\n  Track A 相关性:")
try:
    a_nav=pd.read_csv('logs/backtest_a4_nav.csv',index_col=0,parse_dates=True).squeeze()
    a_rets=a_nav.resample('M').last().pct_change().dropna()
    b_rets=nav_s.resample('M').last().pct_change().dropna()
    common=a_rets.index.intersection(b_rets.index)
    corr=a_rets[common].corr(b_rets[common])
    p(f"    Track A/B 月收益相关性: {corr:.3f}")
except Exception as e:
    p(f"    无Track A净值，跳过 ({str(e)[:30]})")

# 保存
out = LOG_DIR / "backtest_trinity_final.txt"
Path(out).write_text("\n".join(report)); p(f"\n  ✅ 报告: {out}")
