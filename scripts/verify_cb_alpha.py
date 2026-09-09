"""
可转债Alpha方向验证 — 四测试一次跑完
①回撤归因 ②防守属性 ③单因子拆解 ④干净等权
主口径: 0.1%双边 | point-in-time | 含退市债
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd, numpy as np
from loguru import logger; logger.remove()
from data.storage import load_meta

REAL_COST = 0.001; TOP_N = 25

# ══════════════════════════════════════════════
logger.info("加载数据...")
snapshots = load_meta("cb_snapshots")
snapshots['snap_date'] = pd.to_datetime(snapshots['snap_date'])
snap_dates = sorted(snapshots['snap_date'].unique())

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots['code'].unique())
price_panel = {}
for code in snap_codes:
    fpath = daily_dir / f"{code}.parquet"
    if not fpath.exists(): continue
    df = pd.read_parquet(fpath); df['date'] = pd.to_datetime(df['date'])
    for _, row in df.iterrows():
        d = row['date']; p = float(row['close']) if 'close' in row else None
        if p and p > 0: price_panel.setdefault(d, {})[code] = p
all_dates = sorted(price_panel.keys())
logger.info(f"  {len(all_dates)}天, {len(price_panel)}交易日")

# ══════════════════════════════════════════════
# 选股函数 — 四个变体
# ══════════════════════════════════════════════
RO = {'AAA': 0, 'AA+': 1, 'AA': 2, 'AA-': 3, 'A+': 4, 'A': 5, 'A-': 6}

def filter_base(snap, min_rating='A-', min_size=0.5, exclude_redeem=True, max_premium=None):
    """公共过滤"""
    df = snap.copy()
    df['rn'] = df['rating'].map(RO).fillna(99)
    df = df[df['rn'] <= RO.get(min_rating, 6)]
    if 'size' in df.columns: df = df[df['size'] >= min_size]
    if exclude_redeem and 'price' in df.columns and 'premium' in df.columns:
        df = df[~((df['price'] > 130) & (df['premium'] < 5))]
    if max_premium is not None and 'premium' in df.columns:
        df = df[df['premium'] <= max_premium]
    return df

def select_dblow(snap):
    df = filter_base(snap)
    if 'dblow' not in df.columns: df['dblow'] = df['price'] + 100 * df['premium'] / 100
    return df.sort_values('dblow').head(TOP_N)['code'].tolist()

def select_price_low(snap):
    """只选价格最低"""
    df = filter_base(snap)
    return df.sort_values('price').head(TOP_N)['code'].tolist()

def select_premium_low(snap):
    """只选溢价率最低(最股性)"""
    df = filter_base(snap, min_size=1.0)  # 溢价率低的债往往规模大一些
    return df.sort_values('premium').head(TOP_N)['code'].tolist()

def select_clean_equal(snap):
    """干净等权: 排除地雷但不过滤其他"""
    df = filter_base(snap, min_rating='A-', min_size=2.0, exclude_redeem=True, max_premium=50)
    return df['code'].unique().tolist()

def select_all_equal(snap):
    """全市场等权(基准)"""
    df = filter_base(snap)
    return df['code'].unique().tolist()

# ══════════════════════════════════════════════
# 回测引擎
# ══════════════════════════════════════════════
def run_bt(select_fn):
    nav = pd.Series(1.0, index=all_dates)
    cw = {}; si = 0; lsd = None
    for i, date in enumerate(all_dates):
        ds = str(date.date())[:10]; pt = price_panel.get(date, {})
        if cw and i > 0:
            ret = 0.0; pp = price_panel.get(all_dates[i-1], {})
            for code, w in cw.items():
                p0 = pp.get(code); p1 = pt.get(code)
                if p0 and p1 and p0 > 0: ret += w * (p1/p0 - 1)
            nav.iloc[i] = nav.iloc[i-1] * (1 + ret)
        else: nav.iloc[i] = nav.iloc[i-1] if i > 0 else 1.0
        for c in [c for c in cw if c not in pt]:
            w = cw.pop(c); nav.iloc[i] -= w * REAL_COST
        while si < len(snap_dates) and str(snap_dates[si].date())[:10] <= ds:
            lsd = snap_dates[si]; si += 1
        if lsd and str(lsd.date())[:10] == ds:
            snap = snapshots[snapshots['snap_date'] == lsd]
            if snap.empty: continue
            avails = [c for c in snap['code'].unique() if c in pt]
            sel = select_fn(snap[snap['code'].isin(avails)])
            if len(sel) < 10: continue
            n = len(sel); os2 = set(cw.keys()); ns2 = set(sel)
            nw = {c: 1.0/n for c in sel}
            ew = sum(nw.get(c,0) for c in ns2 - os2)
            xw = sum(cw.get(c,0) for c in os2 - ns2)
            nav.iloc[i] *= (1 - (ew + xw)/2 * REAL_COST * 2)
            cw = nw
    return nav

def met(nav_s):
    n = len(nav_s); ar = (nav_s.iloc[-1]/nav_s.iloc[0])**(252/n) - 1
    dr = nav_s.pct_change(fill_method=None).dropna()
    sr = dr.mean()/dr.std()*np.sqrt(252) if dr.std() > 0 else 0
    dd = float((nav_s/nav_s.cummax() - 1).min())
    vol = dr.std()*np.sqrt(252)
    return ar, sr, dd, vol

# ══════════════════════════════════════════════
# 运行所有变体
# ══════════════════════════════════════════════
print(">>> 运行5个变体...")
variants = {
    '双低': select_dblow,
    '低价单因子': select_price_low,
    '低溢价单因子': select_premium_low,
    '干净等权': select_clean_equal,
    '全市场等权': select_all_equal,
}
navs = {}
for name, fn in variants.items():
    print(f"  {name}...", end=" ", flush=True)
    navs[name] = run_bt(fn)
    ar, sr, dd, vol = met(navs[name])
    print(f"ar={ar*100:+.1f}% sr={sr:.2f} dd={dd*100:+.1f}%")

# Align all to common dates
common = navs['全市场等权'].index
for k in navs: navs[k] = navs[k][common]

# ══════════════════════════════════════════════
# 测试1: 回撤归因
# ══════════════════════════════════════════════
print()
print("═" * 80)
print("测试1: 回撤归因 — 逐年最大回撤对比")
print("═" * 80)
print(f"{'年份':<8} {'双低回撤':>8} {'等权回撤':>8} {'差异':>8} {'双低年化':>8} {'等权年化':>8}")
print("-" * 52)
for y in range(2020, 2026):
    nd = navs['双低'][(navs['双低'].index >= f'{y}-01-01') & (navs['双低'].index <= f'{y}-12-31')]
    ne = navs['全市场等权'][(navs['全市场等权'].index >= f'{y}-01-01') & (navs['全市场等权'].index <= f'{y}-12-31')]
    if len(nd) < 5: continue
    d_dd = float((nd/nd.cummax() - 1).min()) * 100
    e_dd = float((ne/ne.cummax() - 1).min()) * 100
    d_ar = (nd.iloc[-1]/nd.iloc[0])**(252/len(nd)) - 1
    e_ar = (ne.iloc[-1]/ne.iloc[0])**(252/len(ne)) - 1
    print(f"  {y:<8} {d_dd:+7.1f}% {e_dd:+7.1f}% {d_dd-e_dd:+7.1f}pp {d_ar*100:+7.1f}% {e_ar*100:+7.1f}%")

# Find worst drawdown period for 双低
nav_d = navs['双低']
dd_series = nav_d / nav_d.cummax() - 1
worst_idx = dd_series.idxmin()
worst_dd_start = nav_d[:worst_idx][nav_d[:worst_idx] / nav_d[:worst_idx].cummax() - 1 < -0.02]
if not worst_dd_start.empty:
    dd_start = worst_dd_start.index[-1]
    print(f"\n  双低最差回撤段: {dd_start.date()} → {worst_idx.date()} (回撤{dd_series.min()*100:.1f}%)")

# ══════════════════════════════════════════════
# 测试2: 防守属性 — 下跌月表现
# ══════════════════════════════════════════════
print()
print("═" * 80)
print("测试2: 防守属性 — 等权下跌月 vs 双低")
print("═" * 80)
ew_m = navs['全市场等权'].resample('ME').last().pct_change(fill_method=None).dropna()
dl_m = navs['双低'].resample('ME').last().pct_change(fill_method=None).dropna()
cm = ew_m.index.intersection(dl_m.index)
ew_m = ew_m[cm]; dl_m = dl_m[cm]

down_months = ew_m[ew_m < 0]
up_months = ew_m[ew_m > 0]

dl_in_down = dl_m[down_months.index]
dl_in_up = dl_m[up_months.index]

print(f"  总月份: {len(ew_m)} | 下跌月: {len(down_months)}({len(down_months)/len(ew_m)*100:.0f}%) | 上涨月: {len(up_months)}")
print(f"  {'':<20} {'平均月收益':>10} {'双低-等权':>10} {'跑赢次数':>10}")
print(f"  {'下跌月(等权<0)':<20} {ew_m[down_months.index].mean()*100:+9.2f}% {dl_in_down.mean()*100-ew_m[down_months.index].mean()*100:+9.2f}% {sum(dl_in_down.values > ew_m[down_months.index].values):>10}/{len(down_months)}")
print(f"  {'上涨月(等权>0)':<20} {ew_m[up_months.index].mean()*100:+9.2f}% {dl_in_up.mean()*100-ew_m[up_months.index].mean()*100:+9.2f}% {sum(dl_in_up.values > ew_m[up_months.index].values):>10}/{len(up_months)}")

# By year in down months
print()
print(f"  {'年份':<8} {'下跌月数':>8} {'等权均值':>8} {'双低均值':>8} {'超额':>8}")
print(f"  {'-'*44}")
for y in range(2020, 2026):
    y_mask = [idx.year == y for idx in down_months.index]
    if sum(y_mask) == 0: continue
    ew_yd = ew_m[down_months.index[y_mask]]
    dl_yd = dl_m[down_months.index[y_mask]]
    print(f"  {y:<8} {len(ew_yd):>8} {ew_yd.mean()*100:+7.2f}% {dl_yd.mean()*100:+7.2f}% {dl_yd.mean()*100-ew_yd.mean()*100:+7.2f}pp")

# ══════════════════════════════════════════════
# 测试3: 单因子拆解
# ══════════════════════════════════════════════
print()
print("═" * 80)
print("测试3: 单因子拆解 — 低价 vs 低溢价 vs 双低")
print("═" * 80)
print(f"  {'策略':<16} {'年化':>7} {'夏普':>6} {'回撤':>7} {'波动':>7} {'vs等权':>8}")
print(f"  {'-'*58}")
for name in ['双低', '低价单因子', '低溢价单因子', '全市场等权']:
    ar, sr, dd, vol = met(navs[name])
    exc = ar - met(navs['全市场等权'])[0]
    print(f"  {name:<16} {ar*100:+6.1f}% {sr:5.2f} {dd*100:+6.1f}% {vol*100:+5.1f}% {exc*100:+7.1f}pp")

# Yearly for single factors
print()
print(f"  {'年份':<8} {'低价年化':>8} {'低溢价年化':>8} {'双低年化':>8} {'等权年化':>8}")
print(f"  {'-'*44}")
for y in range(2020, 2026):
    row = [str(y)]
    for name in ['低价单因子', '低溢价单因子', '双低', '全市场等权']:
        nv = navs[name][(navs[name].index >= f'{y}-01-01') & (navs[name].index <= f'{y}-12-31')]
        if len(nv) < 5: row.append("N/A"); continue
        y_ar = (nv.iloc[-1]/nv.iloc[0])**(252/len(nv)) - 1
        row.append(f"{y_ar*100:+6.1f}%")
    print(f"  {'  '.join(row)}")

# ══════════════════════════════════════════════
# 测试4: 干净等权
# ══════════════════════════════════════════════
print()
print("═" * 80)
print("测试4: 干净等权 — 排除地雷(强赎/低评级/高溢价/小规模)的纯等权")
print("═" * 80)
for name in ['干净等权', '全市场等权']:
    ar, sr, dd, vol = met(navs[name])
    print(f"  {name:<16} 年化{ar*100:+6.1f}% 夏普{sr:.2f} 回撤{dd*100:+6.1f}% 波动{vol*100:+5.1f}%")

# Clean equal monthly count
clean_counts = []
for sd in snap_dates:
    snap = snapshots[snapshots['snap_date'] == sd]
    if snap.empty: continue
    sel = select_clean_equal(snap[snap['code'].isin(set(price_panel.get(pd.Timestamp(sd), {}).keys()) if pd.Timestamp(sd) in price_panel else set())])
    clean_counts.append((sd, len(sel)))
if clean_counts:
    avg_cnt = np.mean([c for _, c in clean_counts[-36:]])  # last 3 years
    print(f"  近3年均选入: {avg_cnt:.0f}只(全市场{len(snap_codes)}只)")
    print(f"  过滤掉: 强赎触发+低评级+高溢价+小规模")

# Yearly
print()
for y in range(2020, 2026):
    nv = navs['干净等权'][(navs['干净等权'].index >= f'{y}-01-01') & (navs['干净等权'].index <= f'{y}-12-31')]
    if len(nv) < 5: continue
    y_ar = (nv.iloc[-1]/nv.iloc[0])**(252/len(nv)) - 1
    y_dd = float((nv/nv.cummax() - 1).min()) * 100
    ew_nv = navs['全市场等权'][(navs['全市场等权'].index >= f'{y}-01-01') & (navs['全市场等权'].index <= f'{y}-12-31')]
    ew_ar = (ew_nv.iloc[-1]/ew_nv.iloc[0])**(252/len(ew_nv)) - 1
    ew_dd = float((ew_nv/ew_nv.cummax() - 1).min()) * 100
    print(f"  {y}: 干净{y_ar*100:+5.1f}% 回撤{y_dd:+5.1f}% | 全市场{ew_ar*100:+5.1f}% 回撤{ew_dd:+5.1f}%")

print("═" * 80)
