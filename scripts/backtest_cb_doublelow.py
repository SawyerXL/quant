"""
可转债双低策略 — 基准对照(全市场等权)
策略: 每月选 双低(价格+溢价率*100)最低25只, 等权月调仓
基准: 同期全市场存续可转债等权全持(同universe/同成本/同调仓频率)
严格: 双边0.1%成本 | 排除临近强赎/低评级/小规模 | point-in-time | 含退市债
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from loguru import logger
from data.storage import load_meta

REAL_COST = 0.001   # 转债双边0.1%
TOP_N = 25
CASH_YIELD = 0.02

def pct(s): return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════
logger.info("加载数据...")

snapshots = load_meta("cb_snapshots")
snapshots['snap_date'] = pd.to_datetime(snapshots['snap_date'])
snap_dates = sorted(snapshots['snap_date'].unique())
logger.info(f"  快照: {len(snap_dates)}个月, {len(snapshots)}条记录")

# Build daily price panel
daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots['code'].unique())

price_panel = {}
logger.info("  构建价格面板...")
loaded = 0
for code in snap_codes:
    fpath = daily_dir / f"{code}.parquet"
    if not fpath.exists(): continue
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    prev = None
    for _, row in df.iterrows():
        d = row['date']; p = float(row['close']) if 'close' in row else None
        if p and p > 0:
            # 脏数据过滤(2026-08-31): 停牌0价行剔除; 相对上一有效价跳变>50%剔除
            if prev is not None and abs(p / prev - 1) > 0.5:
                continue
            price_panel.setdefault(d, {})[code] = p
            prev = p
    loaded += 1
logger.info(f"  加载{loaded}只, {len(price_panel)}个交易日")

all_dates = sorted(price_panel.keys())

# ══════════════════════════════════════════════════════════════════
# 选股
# ══════════════════════════════════════════════════════════════════
def select_double_low(snap):
    df = snap.copy()
    # 排除低评级 (BBB+及以下)
    rating_order = {'AAA':0,'AA+':1,'AA':2,'AA-':3,'A+':4,'A':5,'A-':6}
    df['rn'] = df['rating'].map(rating_order).fillna(99)
    df = df[df['rn'] <= rating_order.get('A-', 6)]
    # 排除小规模 (<0.5亿)
    if 'size' in df.columns: df = df[df['size'] >= 0.5]
    # 排除临近强赎 (价>130且溢价<5%)
    if 'price' in df.columns and 'premium' in df.columns:
        df = df[~((df['price']>130) & (df['premium']<5))]
    # 双低排序
    if 'dblow' not in df.columns:
        df['dblow'] = df['price'] + 100 * df['premium'] / 100
    return df.sort_values('dblow').head(TOP_N)['code'].tolist()

# ══════════════════════════════════════════════════════════════════
# 通用回测: strategy="dblow" or "equal"(全市场等权基准)
# ══════════════════════════════════════════════════════════════════
def run_bt(strategy="dblow"):
    nav = pd.Series(1.0, index=all_dates)
    cur_weights = {}
    snap_idx = 0
    last_snap_date = None

    for i, date in enumerate(all_dates):
        date_str = str(date.date())[:10]
        prices_today = price_panel.get(date, {})

        # Mark-to-market
        if cur_weights and i > 0:
            ret = 0.0
            prev_prices = price_panel.get(all_dates[i-1], {})
            for code, w in cur_weights.items():
                pp = prev_prices.get(code); cp = prices_today.get(code)
                if pp and cp and pp > 0: ret += w * (cp/pp - 1)
            nav.iloc[i] = nav.iloc[i-1] * (1 + ret)
        else:
            nav.iloc[i] = nav.iloc[i-1] if i > 0 else 1.0

        # Handle delisted bonds (price gone)
        delisted = [c for c in cur_weights if c not in prices_today]
        for c in delisted:
            w = cur_weights.pop(c)
            nav.iloc[i] -= w * REAL_COST

        # Find current snapshot
        while snap_idx < len(snap_dates) and str(snap_dates[snap_idx].date())[:10] <= date_str:
            last_snap_date = snap_dates[snap_idx]
            snap_idx += 1

        # Rebalance
        if last_snap_date and str(last_snap_date.date())[:10] == date_str:
            snap = snapshots[snapshots['snap_date'] == last_snap_date]
            if snap.empty: continue

            avails = [c for c in snap['code'].unique() if c in prices_today]

            if strategy == "dblow":
                selected = select_double_low(snap[snap['code'].isin(avails)])
                if len(selected) < TOP_N: continue
                selected = selected[:TOP_N]
            else:
                # 全市场等权基准: 所有通过过滤的债
                df = snap[snap['code'].isin(avails)].copy()
                rating_order = {'AAA':0,'AA+':1,'AA':2,'AA-':3,'A+':4,'A':5,'A-':6}
                df['rn'] = df['rating'].map(rating_order).fillna(99)
                df = df[df['rn'] <= rating_order.get('A-', 6)]
                if 'size' in df.columns: df = df[df['size'] >= 0.5]
                if 'price' in df.columns and 'premium' in df.columns:
                    df = df[~((df['price']>130) & (df['premium']<5))]
                selected = df['code'].unique().tolist()
                if len(selected) < 10: continue

            n = len(selected)
            old_set = set(cur_weights.keys())
            new_set = set(selected)
            new_w = {c: 1.0/n for c in selected}

            enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
            exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
            nav.iloc[i] *= (1 - (enter_w + exit_w)/2 * REAL_COST * 2)

            cur_weights = new_w
    return nav

# ══════════════════════════════════════════════════════════════════
print("═" * 80)

print("可转债双低策略 — 基准对照(全市场等权)")
print("═" * 80)


nav_dblow = run_bt("dblow")
nav_equal = run_bt("equal")

# Align dates
common = nav_dblow.index.intersection(nav_equal.index)
nav_d = nav_dblow[common]; nav_e = nav_equal[common]

def metrics(nav_s):
    n = len(nav_s); ar = (nav_s.iloc[-1]/nav_s.iloc[0])**(252/n) - 1
    dr = nav_s.pct_change(fill_method=None).dropna()
    sr = dr.mean()/dr.std()*np.sqrt(252) if dr.std()>0 else 0
    dd = (nav_s/nav_s.cummax()-1).min(); vol = dr.std()*np.sqrt(252)
    return ar, sr, dd, vol

d_ar,d_sr,d_dd,d_vol = metrics(nav_d)
e_ar,e_sr,e_dd,e_vol = metrics(nav_e)

# Excess + HAC
d_m = nav_d.resample('ME').last().pct_change(fill_method=None).dropna()
e_m = nav_e.resample('ME').last().pct_change(fill_method=None).dropna()
cm = d_m.index.intersection(e_m.index)
ex = d_m[cm].values - e_m[cm].values
am = np.mean(ex); aa = (1+am)**12-1; n_ex = len(ex)
res = ex - am; nwl = min(3, n_ex//4)
S = np.sum(res**2)/n_ex
for lag in range(1, nwl+1):
    w = 1-lag/(nwl+1); S += 2*w*np.sum(res[lag:]*res[:-lag])/n_ex
se = np.sqrt(S/n_ex); t_val = am/se if se>0 else 0

print(f"  期间: {common[0].date()} → {common[-1].date()} ({len(common)}天)")
print()
print(f"  {'':<20} {'年化':>7} {'夏普':>6} {'回撤':>7} {'波动':>7}")
print(f"  {'-'*50}")
print(f"  {'双低策略':<20} {d_ar*100:+6.1f}% {d_sr:5.2f} {d_dd*100:+6.1f}% {d_vol*100:+5.1f}%")
print(f"  {'全市场等权(基准)':<20} {e_ar*100:+6.1f}% {e_sr:5.2f} {e_dd*100:+6.1f}% {e_vol*100:+5.1f}%")
print()
print(f"  超额Alpha: {aa*100:+.1f}% | HAC t: {t_val:+.2f} | {'显著(p<0.05)' if abs(t_val)>1.96 else '不显著'}")
print(f"  策略: 双低TOP{TOP_N}等权月调仓 | 基准: 全市场等权月调仓(同universe/同成本)")
print(f"  成本: 双边0.1% | 排除: A-以下/规模<0.5亿/临近强赎")
print()

# 逐年
print(f"  {'年份':<8} {'双低':>8} {'等权':>8} {'超额':>8}")
print(f"  {'-'*36}")
for y in range(2020, 2026):
    nd = nav_d[(nav_d.index>=f'{y}-01-01')&(nav_d.index<=f'{y}-12-31')]
    ne = nav_e[(nav_e.index>=f'{y}-01-01')&(nav_e.index<=f'{y}-12-31')]
    if len(nd)<5: continue
    dy = (nd.iloc[-1]/nd.iloc[0])**(252/len(nd)) - 1
    ey = (ne.iloc[-1]/ne.iloc[0])**(252/len(ne)) - 1
    print(f"  {y:<8} {dy*100:+7.1f}% {ey*100:+7.1f}% {dy-ey:+7.1f}pp")
print("═" * 80)
# 2026-09-06: 保存双低策略日收益序列(与主策略相关性度量用)
nav_d.to_frame("cb").to_csv("logs/cb_doublelow_nav.csv")
print("已保存 logs/cb_doublelow_nav.csv")

