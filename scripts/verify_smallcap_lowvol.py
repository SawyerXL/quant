"""
小盘低波动因子严格归因验证 — 2×2对照矩阵 + 剔除年份 + 成本压力
一次跑完，只给数字表，不下结论。
"""
import sys, os
sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from pathlib import Path
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from data.storage import load_meta
from run_backtest_a2 import _make_rebal_dates, get_position_ratio
from run_backtest_a import (calc_metrics, BACKTEST_START,
    COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP)
from run_backtest_a4 import MA10_EXIT_DAYS, MA_EXIT_WINDOW

BAOSTOCK_DIR = Path('data_store/baostock/daily')
TOP_N = 30

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
print('═' * 85)
print('小盘低波动因子 — 归因验证 (2×2因子-择时对照)')
print('═' * 85)

# ── Load data (once) ──
print('\n>>> Loading baostock data...')
sc_universe = pd.read_parquet('data_store/meta/smallcap_universe_bs.parquet')
all_codes = set()
for codes_str in sc_universe['codes']:
    all_codes.update(codes_str.split(','))
all_codes = sorted(all_codes)

cal = load_meta('trade_calendar')
end = sorted([d for d in cal['trade_date'] if d <= '2025-12-31'])[-1]
calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None

prices, pbs, amounts = {}, {}, {}
for code in all_codes:
    fpath = BAOSTOCK_DIR / f'{code}.parquet'
    if not fpath.exists(): continue
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    df = df[(df.index >= BACKTEST_START) & (df.index <= end)]
    if len(df) < MIN_BARS: continue
    cs = pd.to_numeric(df['close'], errors='coerce').replace(0, np.nan).dropna()
    ps = pd.to_numeric(df['pbMRQ'], errors='coerce').replace(0, np.nan).dropna()
    am = pd.to_numeric(df['amount'], errors='coerce').replace(0, np.nan).dropna()
    if len(cs) >= MIN_BARS:
        prices[code] = cs; pbs[code] = ps; amounts[code] = am

panel = pd.DataFrame(prices).sort_index()
ap = pd.DataFrame(amounts).sort_index()
all_dates = panel.index
rebal_dates = _make_rebal_dates(calendar, 'biweekly')
rebal_set = set(rebal_dates)
print(f'  Panel: {panel.shape[0]}d × {panel.shape[1]} stocks, {len(rebal_dates)} rebalances')

# ── Factor functions ──
def select_equal_weight(date, i):
    if ap is not None and i >= 20:
        avg = ap.iloc[max(0,i-20):i].mean().dropna()
        avail = [c for c in avg.index if c in panel.columns]
        if len(avail) >= TOP_N:
            return avg[avail].nlargest(TOP_N).index.tolist(), {}
    return sorted(panel.columns)[:TOP_N], {}

def select_low_vol(date, i):
    if i < 65:
        return select_equal_weight(date, i)
    rets = panel.iloc[i-60:i].pct_change(fill_method=None).std()
    lv = rets.dropna().nsmallest(TOP_N)
    if len(lv) < TOP_N:
        return select_equal_weight(date, i)
    return lv.index.tolist(), {}

# ══════════════════════════════════════════════════════════════════════
# UNIFIED BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════
def run_backtest(select_fn, use_ma200=True, commission=COMMISSION, date_mask=None):
    """
    use_ma200=False → force_full (pos_ratio always 1.0)
    date_mask: boolean Series (True=include day), for leave-one-out
    """
    if date_mask is None:
        date_mask = pd.Series(True, index=all_dates)

    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0

    for i, date in enumerate(all_dates):
        if not date_mask.iloc[i]:
            continue
        date_str = str(date.date())

        # Step 1: mark-to-market
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i-1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp/pp - 1)
            port_rets.iloc[i] += ret

        # Step 2: MA10 exit
        if cur_weights and i >= 10:
            exits = []
            for code in list(cur_weights.keys()):
                col = panel[code] if code in panel.columns else None
                if col is None: continue
                hist = col.iloc[max(0,i-MA_EXIT_WINDOW+1):i+1].dropna()
                if len(hist) < max(5, MA_EXIT_WINDOW//2): continue
                ma10 = hist.mean()
                cur_p = panel.iloc[i].get(code)
                if pd.isna(cur_p) or cur_p <= 0: continue
                if cur_p < ma10:
                    days_below_ma10[code] = days_below_ma10.get(code,0) + 1
                else:
                    days_below_ma10[code] = 0
                if days_below_ma10.get(code,0) >= MA10_EXIT_DAYS:
                    exits.append(code)
            for code in exits:
                w = cur_weights.pop(code, 0)
                port_rets.iloc[i] -= w * commission
                entry_prices.pop(code, None); days_below_ma10.pop(code, None)

        # Step 3: rebalance
        if date_str in rebal_set and i >= MIN_BARS:
            raw_pos = get_position_ratio(idx_c, date) if (idx_c is not None and use_ma200) else 1.0
            if raw_pos <= 0.30 and use_ma200:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav
            else:
                pos_ratio = raw_pos if use_ma200 else 1.0
                top_codes, _ = select_fn(date, i)
                n = min(len(top_codes), TOP_N)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                new_w = {c: pos_ratio/n for c in top_codes[:n]}

                enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w)/2 * commission * 2

                cp_s = panel.ffill().iloc[i]
                for c in new_set - old_set:
                    ep = cp_s.get(c)
                    if ep and not pd.isna(ep): entry_prices[c] = float(ep)
                for c in old_set - new_set:
                    entry_prices.pop(c, None); days_below_ma10.pop(c, None)
                if not cur_weights: entry_hwm = cumul_nav
                cur_weights = new_w
                nav_since = 1.0

        # Step 4: portfolio stops
        if cur_weights and i > 0:
            if nav_since <= (1+PERIOD_STOP) or (cumul_nav/entry_hwm-1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav

        # Step 5: cash
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD/252
        nav_since *= (1+port_rets.iloc[i])
        cumul_nav *= (1+port_rets.iloc[i])

    return (1+port_rets).cumprod()

def metric(nav):
    m = calc_metrics(nav)
    return {'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
            'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率'])}

def excess_vs_baseline(nav_a, nav_b):
    """Monthly excess alpha (annualized) + HAC t"""
    ra = nav_a.resample('ME').last().pct_change().dropna()
    rb = nav_b.resample('ME').last().pct_change().dropna()
    common = ra.index.intersection(rb.index)
    excess = ra[common].values - rb[common].values
    alpha_m = np.mean(excess)
    alpha_a = (1+alpha_m)**12 - 1
    n = len(excess); resid = excess - alpha_m
    nw_lags = min(3, n//4)
    S = np.sum(resid**2)/n
    for lag in range(1, nw_lags+1):
        w = 1 - lag/(nw_lags+1)
        S += 2*w * np.sum(resid[lag:]*resid[:-lag])/n
    se = np.sqrt(S/n)
    t = alpha_m / se if se > 0 else 0
    return alpha_a, t

def crunch_dd(nav):
    """Max drawdown during 2024-01-02 to 2024-02-29"""
    c = nav[(nav.index >= '2024-01-02') & (nav.index <= '2024-02-29')]
    peak = c.cummax()
    return float(((c / peak - 1) * 100).min())

# ══════════════════════════════════════════════════════════════════════
# RUN ALL 4 GROUPS
# ══════════════════════════════════════════════════════════════════════
print('\n>>> Running 2×2 matrix...')
groups = {
    'G1 等权·无择时':  (select_equal_weight, False),
    'G2 低波·无择时':  (select_low_vol,      False),
    'G3 等权·MA200':   (select_equal_weight, True),
    'G4 低波·MA200':   (select_low_vol,      True),
}

navs = {}
for label, (fn, use_ma) in groups.items():
    print(f'  {label}...', end=' ', flush=True)
    navs[label] = run_backtest(fn, use_ma200=use_ma)
    m = metric(navs[label])
    print(f'ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}%')

# ══════════════════════════════════════════════════════════════════════
# TABLE 1: 2×2 matrix — full sample
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 85)
print('表1: 2×2 因子-择时对照矩阵 (全样本 2020-2025, baostock含退市股)')
print('=' * 85)

# Compute excesses
exc_2v1, t_2v1 = excess_vs_baseline(navs['G2 低波·无择时'], navs['G1 等权·无择时'])
exc_4v3, t_4v3 = excess_vs_baseline(navs['G4 低波·MA200'], navs['G3 等权·MA200'])
exc_3v1, t_3v1 = excess_vs_baseline(navs['G3 等权·MA200'], navs['G1 等权·无择时'])
exc_4v2, t_4v2 = excess_vs_baseline(navs['G4 低波·MA200'], navs['G2 低波·无择时'])

# Crunch drawdowns
dd_crunch = {k: crunch_dd(navs[k]) for k in navs}

rows = [
    ('G1 等权·无择时', navs['G1 等权·无择时'], '—'),
    ('G2 低波·无择时', navs['G2 低波·无择时'], '—'),
    ('G3 等权·MA200',  navs['G3 等权·MA200'],  '—'),
    ('G4 低波·MA200',  navs['G4 低波·MA200'],  '—'),
]

print(f'{"":<18} {"年化":>7} {"夏普":>6} {"回撤":>7} {"波动":>7} {"2024.1-2回撤":>12}')
print('-' * 65)
for label, nav, _ in rows:
    m = metric(nav)
    print(f'{label:<18} {m["ar"]*100:+6.1f}% {m["sr"]:5.2f} {m["dd"]*100:+6.1f}% {m["vol"]*100:+5.1f}% {dd_crunch[label]:+8.2f}%')

print()
print('归因拆解:')
print(f'  ┌─ 低波动纯效果 (G2-G1, 无择时): {exc_2v1*100:+.1f}% (t={t_2v1:+.2f})')
print(f'  ├─ 择时纯效果   (G3-G1, 等权):     {exc_3v1*100:+.1f}% (t={t_3v1:+.2f})')
print(f'  ├─ 低波+择时叠加 (G4-G3, 有择时):   {exc_4v3*100:+.1f}% (t={t_4v3:+.2f})')
print(f'  └─ 择时在低波上 (G4-G2):            {exc_4v2*100:+.1f}% (t={t_4v2:+.2f})')
print(f'  ★ 关键: G2vsG1="没了择时低波动还剩多少?" G4vsG3="择时基础上低波动还加不加分?"')

# ══════════════════════════════════════════════════════════════════════
# TABLE 2: Year removal — G2 and G4
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 85)
print('表2: 剔除极端年份 — G2(低波·无择时) 和 G4(低波·MA200)')
print('=' * 85)

years = sorted(set(d.year for d in all_dates if d.year >= 2020))

for group_label, group_fn, group_ma in [
    ('G2 低波·无择时', select_low_vol, False),
    ('G4 低波·MA200',  select_low_vol, True),
]:
    # Base for this group (need EW counterpart for excess)
    ew_fn = select_equal_weight
    ew_ma = group_ma

    base_nav = run_backtest(group_fn, use_ma200=group_ma)
    base_ew = run_backtest(ew_fn, use_ma200=ew_ma)
    base_m = metric(base_nav)
    base_exc, base_t = excess_vs_baseline(base_nav, base_ew)

    print(f'\n── {group_label} ──')
    print(f'{"场景":<16} {"年化":>7} {"夏普":>6} {"回撤":>7} {"vs等权超额":>9} {"HAC t":>6} {"判断":>12}')
    print('-' * 70)

    full_label = '完整期间'
    judge = '✓' if (base_m['ar'] >= 0.08 and base_m['sr'] >= 0.8) else '✗'
    print(f'{full_label:<16} {base_m["ar"]*100:+6.1f}% {base_m["sr"]:5.2f} {base_m["dd"]*100:+6.1f}% {base_exc*100:+8.1f}% {base_t:+6.2f} {judge:>12}')

    for drop_year in years:
        mask = pd.Series([d.year != drop_year for d in all_dates], index=all_dates)
        nav_lv = run_backtest(group_fn, use_ma200=group_ma, date_mask=mask)
        nav_ew = run_backtest(ew_fn, use_ma200=ew_ma, date_mask=mask)
        m = metric(nav_lv)
        exc, t_val = excess_vs_baseline(nav_lv, nav_ew)
        judge = '✓' if (m['ar'] >= 0.08 and m['sr'] >= 0.8) else '✗'
        print(f'{"剔除"+str(drop_year):<16} {m["ar"]*100:+6.1f}% {m["sr"]:5.2f} {m["dd"]*100:+6.1f}% {exc*100:+8.1f}% {t_val:+6.2f} {judge:>12}')

    # Impact ranking
    print(f'  逐年影响力 (剔除后年化变化 vs 完整):')
    impacts = []
    for drop_year in years:
        mask = pd.Series([d.year != drop_year for d in all_dates], index=all_dates)
        nav_lv = run_backtest(group_fn, use_ma200=group_ma, date_mask=mask)
        m = metric(nav_lv)
        impacts.append((drop_year, m['ar'] - base_m['ar']))
    impacts.sort(key=lambda x: x[1])
    for y, delta in impacts:
        direction = '↓拉低' if delta < 0 else '↑抬高'
        print(f'    剔除{y}: {delta*100:+.1f}pp {direction}')

print()
print('判断标准: 剔除任一年后年化≥8%且夏普≥0.8。')

# ══════════════════════════════════════════════════════════════════════
# TABLE 3: Cost stress — G2 and G4
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 85)
print('表3: 成本压力测试 — G2(低波·无择时) 和 G4(低波·MA200)')
print('=' * 85)

for group_label, group_fn, group_ma in [
    ('G2 低波·无择时', select_low_vol, False),
    ('G4 低波·MA200',  select_low_vol, True),
]:
    ew_fn = select_equal_weight; ew_ma = group_ma
    print(f'\n── {group_label} ──')
    print(f'{"成本档位":<20} {"年化":>7} {"夏普":>6} {"回撤":>7} {"波动":>7} {"vs等权超额":>9} {"HAC t":>6} {"判断":>12}')
    print('-' * 85)

    for cost_rate, cost_label in [(0.001, '单边0.1%(低)'), (0.00175, '单边0.175%(基准)'),
                                    (0.003, '单边0.3%(中)'), (0.005, '单边0.5%(高)')]:
        nav_lv = run_backtest(group_fn, use_ma200=group_ma, commission=cost_rate)
        nav_ew = run_backtest(ew_fn, use_ma200=ew_ma, commission=cost_rate)
        m = metric(nav_lv)
        exc, t_val = excess_vs_baseline(nav_lv, nav_ew)
        judge = '✓' if (m['ar'] >= 0.08) else '✗'
        print(f'{cost_label:<20} {m["ar"]*100:+6.1f}% {m["sr"]:5.2f} {m["dd"]*100:+6.1f}% {m["vol"]*100:+5.1f}% {exc*100:+8.1f}% {t_val:+6.2f} {judge:>12}')

    # Sensitivity
    base_nav = run_backtest(group_fn, use_ma200=group_ma, commission=0.00175)
    base_ar = metric(base_nav)['ar']
    print(f'  成本敏感度 (vs 0.175%基准):')
    for cost_rate, cost_label in [(0.001, '0.1%'), (0.003, '0.3%'), (0.005, '0.5%')]:
        nav = run_backtest(group_fn, use_ma200=group_ma, commission=cost_rate)
        delta = (metric(nav)['ar'] - base_ar) * 100
        print(f'    {cost_label}: {delta:+.1f}pp')

print()
print('判断标准: 0.5%成本下年化≥8%。')
print()
print('═' * 85)
print('归因三表完毕。核心问题:')
print('  G2vsG1 = 低波动选股本身的超额(去掉择时保护)')
print('  G4vsG3 = 择时基础上低波动还加不加分')
print('  踩踏期G2回撤 = 纯选股在极端行情下是真抗跌还是被打穿')
print('═' * 85)
