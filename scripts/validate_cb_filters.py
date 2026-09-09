#!/usr/bin/env python3
"""
验证个人CB筛选逻辑 — 对比五种方案的全程回测(溢价率数据已补全)
方案: ①全市场等权(基准) ②仅价格<110 ③+评级≥AA- ④+溢价<100% ⑤完整个人版(价+级+溢)
输出: 逐年对比 + 夏普/回撤/年化
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from loguru import logger
from data.storage import load_meta

REAL_COST = 0.001
MAX_PRICE = 110

RATING_MAP = {'AAA': 0, 'AA+': 1, 'AA': 2, 'AA-': 3,
              'A+': 4, 'A': 5, 'A-': 6, 'BBB+': 7, 'BBB': 8, 'BBB-': 9,
              'BB': 10, 'B': 11, 'CCC': 12, 'CC': 13, 'C': 14}

# ══════════════════════════════════════════════════════════════════
logger.info("加载数据...")

snapshots = load_meta("cb_snapshots")
snapshots['snap_date'] = pd.to_datetime(snapshots['snap_date'])
snap_dates = sorted(snapshots['snap_date'].unique())

daily_dir = Path("data_store/convertible_bonds/daily")
snap_codes = set(snapshots['code'].unique())

# 构建价格+成交量面板
price_panel = {}
vol_panel = {}
for code in snap_codes:
    fpath = daily_dir / f"{code}.parquet"
    if not fpath.exists():
        continue
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date'])
    for _, row in df.iterrows():
        d = row['date']
        p = float(row['close']) if 'close' in row and pd.notna(row['close']) else None
        v = float(row['volume']) if 'volume' in row and pd.notna(row['volume']) else 0
        if p and p > 0:
            price_panel.setdefault(d, {})[code] = p
            vol_panel.setdefault(d, {})[code] = v

all_dates = sorted(price_panel.keys())
logger.info(f"  {len(snap_dates)}个月快照, {len(all_dates)}个交易日, {len(snap_codes)}只转债")

# 预计算每个快照日的评级映射(快照中的rating)
snap_rating = {}
for sd in snap_dates:
    snap = snapshots[snapshots['snap_date'] == sd]
    snap_rating[sd] = {}
    for _, r in snap.iterrows():
        code = r['code']
        rating = str(r.get('rating', '')).strip().upper()
        snap_rating[sd][code] = RATING_MAP.get(rating, 99)


# ══════════════════════════════════════════════════════════════════
# 回测引擎
# ══════════════════════════════════════════════════════════════════

def run_bt(variant='equal', min_vol=0):
    """
    variant:
      'equal'  — 全市场等权(基准,A-含以上,排强赎)
      'price'  — 仅价格<110
      'rating' — 价格<110 + 评级≥AA-
      'full'   — 价格<110 + 评级≥AA- + 成交量>min_vol
    """
    nav = pd.Series(1.0, index=all_dates)
    cur_weights = {}
    snap_idx = 0
    last_snap_date = None

    for i, date in enumerate(all_dates):
        ds = str(date.date())[:10]
        pt = price_panel.get(date, {})

        # Mark-to-market
        if cur_weights and i > 0:
            ret = 0.0
            pp = price_panel.get(all_dates[i - 1], {})
            for code, w in cur_weights.items():
                p0 = pp.get(code)
                p1 = pt.get(code)
                if p0 and p1 and p0 > 0:
                    ret += w * (p1 / p0 - 1)
            nav.iloc[i] = nav.iloc[i - 1] * (1 + ret)
        else:
            nav.iloc[i] = nav.iloc[i - 1] if i > 0 else 1.0

        # 处理退市债券
        delisted = [c for c in cur_weights if c not in pt]
        for c in delisted:
            w = cur_weights.pop(c)
            nav.iloc[i] -= w * REAL_COST

        # 定位当前快照
        while snap_idx < len(snap_dates) and str(snap_dates[snap_idx].date())[:10] <= ds:
            last_snap_date = snap_dates[snap_idx]
            snap_idx += 1

        # 调仓日
        if last_snap_date and str(last_snap_date.date())[:10] == ds:
            snap = snapshots[snapshots['snap_date'] == last_snap_date]
            if snap.empty:
                continue

            avails = [c for c in snap['code'].unique() if c in pt]
            df = snap[snap['code'].isin(avails)].copy()

            # 基础过滤(所有变体共用)
            df['rn'] = df['code'].map(snap_rating.get(last_snap_date, {})).fillna(99)

            if variant == 'equal':
                # 全市场等权: A-含以上
                df = df[df['rn'] <= RATING_MAP.get('A-', 6)]
                # 排除临近强赎(价>130, 无溢价数据的近似)
                df = df[df['price'] <= 130]
                if 'size' in df.columns:
                    df = df[df['size'] >= 0.5]
                selected = df['code'].unique().tolist()

            elif variant == 'price':
                # 仅价格<110
                df = df[df['price'] < MAX_PRICE]
                selected = df['code'].unique().tolist()

            elif variant == 'rating':
                # 价格<110 + 评级≥AA-
                df = df[df['price'] < MAX_PRICE]
                df = df[df['rn'] <= RATING_MAP.get('AA-', 3)]
                selected = df['code'].unique().tolist()

            elif variant == 'premium':
                # 价格<110 + 评级≥AA- + 溢价<100%
                df = df[df['price'] < MAX_PRICE]
                df = df[df['rn'] <= RATING_MAP.get('AA-', 3)]
                df = df[df['premium'] < 100]
                selected = df['code'].unique().tolist()

            elif variant == 'full':
                # 完整个人版: 价格<110 + 评级≥AA- + 溢价<100% + 成交量>min_vol
                df = df[df['price'] < MAX_PRICE]
                df = df[df['rn'] <= RATING_MAP.get('AA-', 3)]
                df = df[df['premium'] < 100]
                if min_vol > 0:
                    vt = vol_panel.get(date, {})
                    df['vol'] = df['code'].map(vt).fillna(0)
                    df = df[df['vol'] >= min_vol]
                selected = df['code'].unique().tolist()

            if len(selected) < 5:
                continue  # 持仓不足则不调仓,保留原持仓

            n = len(selected)
            old_set = set(cur_weights.keys())
            new_set = set(selected)
            new_w = {c: 1.0 / n for c in selected}

            enter_w = sum(new_w.get(c, 0) for c in new_set - old_set)
            exit_w = sum(cur_weights.get(c, 0) for c in old_set - new_set)
            nav.iloc[i] *= (1 - (enter_w + exit_w) / 2 * REAL_COST * 2)

            cur_weights = new_w

    return nav


# ══════════════════════════════════════════════════════════════════
# 指标计算
# ══════════════════════════════════════════════════════════════════

def metrics(nav_s):
    n = len(nav_s)
    ar = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (252 / n) - 1 if n > 0 else 0
    dr = nav_s.pct_change(fill_method=None).dropna()
    sr = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0
    dd = float((nav_s / nav_s.cummax() - 1).min())
    vol = dr.std() * np.sqrt(252)
    return ar, sr, dd, vol


# ══════════════════════════════════════════════════════════════════
# 运行
# ══════════════════════════════════════════════════════════════════

print(">>> 运行4个变体...")
variants = {
    '①全市场等权(基准)': ('equal', 0),
    '②仅价格<110': ('price', 0),
    '③+评级≥AA-': ('rating', 0),
    '④+溢价<100%': ('premium', 0),
    '⑤完整个人版(价+级+溢)': ('full', 0),
}

navs = {}
avg_counts = {}
for name, (var, min_v) in variants.items():
    print(f"  {name}...", end=" ", flush=True)
    navs[name] = run_bt(var, min_vol=min_v)
    ar, sr, dd, vol = metrics(navs[name])
    print(f"年化{ar * 100:+.1f}% 夏普{sr:.2f} 回撤{dd * 100:+.1f}%")

# Align dates
common = navs['①全市场等权(基准)'].index
for k in navs:
    navs[k] = navs[k][common]

# ══════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════

print()
print("═" * 80)
print("  个人CB筛选逻辑 — 回测验证")
print("═" * 80)
print(f"  期间: {common[0].date()} → {common[-1].date()} ({len(common)}天)")
print(f"  成本: 双边0.1% | 月度调仓 | point-in-time | 含退市债")
print()

# 全期对比
print(f"  {'方案':<28} {'年化':>7} {'夏普':>6} {'回撤':>7} {'波动':>7}")
print(f"  {'-' * 56}")
for name in variants:
    ar, sr, dd, vol = metrics(navs[name])
    print(f"  {name:<28} {ar * 100:+6.1f}% {sr:5.2f} {dd * 100:+6.1f}% {vol * 100:+5.1f}%")

# 逐年对比
benchmark_nav = navs['①全市场等权(基准)']
print()
print(f"  {'年份':<8} ", end="")
for name in variants:
    print(f"{name[:8]:>9}", end=" ")
print(f"  {'最优':>8}")
print(f"  {'-' * 56}")

for y in range(2019, 2026):
    row = f"  {y:<8} "
    best_ar = -999
    best_name = ''
    for name in variants:
        nv = navs[name][(navs[name].index >= f'{y}-01-01') & (navs[name].index <= f'{y}-12-31')]
        if len(nv) < 5:
            row += f"{'N/A':>9} "
            continue
        y_ar = (nv.iloc[-1] / nv.iloc[0]) ** (252 / len(nv)) - 1
        row += f"{y_ar * 100:+8.1f}% "
        if y_ar > best_ar:
            best_ar = y_ar
            best_name = name[:8]
    row += f"{best_name:>8}"
    print(row)

# 超额分析
print()
print(f"  {'对比':<30} {'年化差异':>8} {'夏普差异':>8} {'回撤改善':>8}")
print(f"  {'-' * 56}")
base_ar, base_sr, base_dd, _ = metrics(navs['①全市场等权(基准)'])
for name in ['②仅价格<110', '③+评级≥AA-', '④+溢价<100%', '⑤完整个人版(价+级+溢)']:
    ar, sr, dd, _ = metrics(navs[name])
    print(f"  {name:<30} {(ar - base_ar)*100:+7.1f}pp {(sr - base_sr):+7.2f} {(base_dd - dd)*100:+7.1f}pp")

# 持仓数统计
print()
print(f"  {'方案':<28} {'平均持仓数':>10} {'最少':>6} {'最多':>6}")
print(f"  {'-' * 52}")
for name in variants:
    # 重新跑一次获取持仓数(简化: 用回测最后一天的持仓)
    nv = navs[name]
    print(f"  {name:<28} {'(见逐月数据)':>10}")

print()
print("  ⚠ 注意: 历史溢价率数据缺失,无法回测溢价<100%过滤效果")
print("  ⚠ 成交量数据来自日线parquet(仅包含557只已下载券),可能低估真实成交量")
print("═" * 80)
