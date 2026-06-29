"""
双信号回测对比:
  S1(旧): 成交额TOP30 + 等权 (当前v2)
  S2(新): TOP100成交额 → 多维筛选30只 + 等权
统一框架: 双周调仓 + MA200择时 + MA10出清 + 组合止损
"""
import sys, os
sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='WARNING')

from data.storage import load_meta, load_daily
from run_backtest_a2 import _make_rebal_dates, get_position_ratio
from run_backtest_a import (calc_metrics, BACKTEST_START,
    COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP)
from run_backtest_a4 import MA10_EXIT_DAYS, MA_EXIT_WINDOW
from scripts.run_backtest_a import load_panels

TOP_N = 30; BUDGET_RATIO = 1.0 / TOP_N
MAX_SECTOR = 10  # 单一行业上限

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
print('═' * 80)
print('双信号回测对比: S1(成交额TOP30) vs S2(多维筛选30)')
print('═' * 80)

# ── Load universe & calendar ──
cal = load_meta('trade_calendar')
cal_dates = sorted(cal['trade_date'].tolist())
end = [d for d in cal_dates if d <= '2025-12-31'][-1]
calendar = [d for d in cal_dates if BACKTEST_START <= d <= end]
rebal_dates = _make_rebal_dates(calendar, 'biweekly')
rebal_set = set(rebal_dates)

c800 = load_meta('csi800')
codes = sorted(c800['code'].tolist())
info = load_meta('stock_info_full')
ind_map = {}
for _, r in info.iterrows():
    ind_map[r['code']] = r.get('industry_l1', '其他')

fq = load_meta('financial_quarterly')
fq['report_date'] = fq['report_date'].astype(str)

idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None

print(f'回测周期: {BACKTEST_START} → {end}')
print(f'调仓频率: 双周, {len(rebal_dates)}次')
print(f'CSI800: {len(codes)}只')

# ══════════════════════════════════════════════════════════════════════
# Load full panel (once, for backtest engine)
# ══════════════════════════════════════════════════════════════════════
print('\n>>> 加载全期价格数据...')
panel_full, amt_full = load_panels(codes, BACKTEST_START, end)
all_dates = panel_full.index
print(f'  面板: {panel_full.shape[0]}d × {panel_full.shape[1]} stocks')

# ══════════════════════════════════════════════════════════════════════
# Selection functions
# ══════════════════════════════════════════════════════════════════════
def select_s1_turnover(date, i, panel_sub, amt_sub, **kw):
    """S1: 成交额TOP30"""
    avg = amt_sub.iloc[max(0,i-20):i].mean().dropna()
    available = []
    for code in avg.nlargest(100).index:
        p = panel_sub.iloc[i].get(code)
        if p and not pd.isna(p) and p > 0:
            min_lot = 200 if str(code).startswith('688') else 100
            if p * min_lot <= BUDGET_RATIO * 1e6:  # proxy budget
                available.append(code)
        if len(available) >= TOP_N:
            break
    return available[:TOP_N]

def select_s2_multidim(date, i, panel_sub, amt_sub, **kw):
    """S2: TOP100成交额 → 多维筛选30"""
    avg = amt_sub.iloc[max(0,i-20):i].mean().dropna()

    # Step 1: Top 100 by turnover, filter affordability
    pool = []
    for code in avg.nlargest(100).index:
        if code not in panel_sub.columns: continue
        p = panel_sub.iloc[i].get(code)
        if p and not pd.isna(p) and p > 0:
            min_lot = 200 if str(code).startswith('688') else 100
            if p * min_lot <= BUDGET_RATIO * 1e6:
                pool.append(code)
        if len(pool) >= 100: break

    if len(pool) < TOP_N:
        return pool[:TOP_N]

    # Step 2: Score each
    date_str = str(date.date()) if hasattr(date, 'date') else date
    scores = []
    for code in pool:
        col = panel_sub[code].iloc[max(0,i-60):i+1].dropna()
        if len(col) < 20: continue
        cur = col.iloc[-1]; ma10 = col.iloc[-10:].mean()
        ret5 = (cur/col.iloc[-6]-1)*100 if len(col)>=6 else 0
        ret20 = (cur/col.iloc[-21]-1)*100 if len(col)>=21 else 0
        high20 = col.iloc[-20:].max(); dh = (cur/high20-1)*100
        cons_up = sum(1 for j in range(len(col)-1,max(0,len(col)-15),-1) if col.iloc[j]>col.iloc[j-1])

        # EPS (point-in-time)
        eps = 0.0
        fq_sub = fq[fq['report_date'] <= date_str.replace('-','')]
        fq_code = fq_sub[fq_sub['code'] == code]
        if not fq_code.empty:
            eps = fq_code.sort_values('report_date').iloc[-1].get('eps', 0) or 0

        # Score
        score = 0
        if ret20 > 80: continue
        if ret20 > 50: score -= 6
        elif ret20 > 30: score -= 4
        if ret5 > 15: score -= 3
        if cons_up >= 5: score -= 3
        if dh > -1 and cons_up >= 2: score -= 1

        if cur > ma10: score += 2
        else: score -= 2
        if dh < -5: score += 2
        elif dh < -3: score += 1

        if eps > 1.0: score += 2
        elif eps > 0.3: score += 1
        elif eps < 0: score -= 2

        scores.append({'code':code, 'score':score, 'ind': ind_map.get(code,'其他')})

    # Step 3: Select top 30 with sector cap
    scores.sort(key=lambda x: x['score'], reverse=True)
    selected = []; seen_inds = {}
    for s in scores:
        ind = s['ind']
        if seen_inds.get(ind, 0) >= MAX_SECTOR: continue
        selected.append(s['code'])
        seen_inds[ind] = seen_inds.get(ind, 0) + 1
        if len(selected) >= TOP_N: break

    if len(selected) < TOP_N:
        for s in scores:
            if s['code'] not in selected:
                selected.append(s['code'])
            if len(selected) >= TOP_N: break

    return selected[:TOP_N]

# ══════════════════════════════════════════════════════════════════════
# Unified backtest engine
# ══════════════════════════════════════════════════════════════════════
def run_backtest(select_fn, label):
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # Step 1: return
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel_full.iloc[i-1].get(code)
                cp = panel_full.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp/pp - 1)
            port_rets.iloc[i] += ret

        # Step 2: MA10 exit
        if cur_weights and i >= 10:
            exits = []
            for code in list(cur_weights.keys()):
                col = panel_full[code] if code in panel_full.columns else None
                if col is None: continue
                hist = col.iloc[max(0,i-MA_EXIT_WINDOW+1):i+1].dropna()
                if len(hist) < max(5, MA_EXIT_WINDOW//2): continue
                ma10 = hist.mean(); cur_p = panel_full.iloc[i].get(code)
                if pd.isna(cur_p) or cur_p <= 0: continue
                if cur_p < ma10: days_below_ma10[code] = days_below_ma10.get(code,0) + 1
                else: days_below_ma10[code] = 0
                if days_below_ma10.get(code,0) >= MA10_EXIT_DAYS:
                    exits.append(code)
            for code in exits:
                w = cur_weights.pop(code, 0)
                port_rets.iloc[i] -= w * COMMISSION
                entry_prices.pop(code, None); days_below_ma10.pop(code, None)

        # Step 3: rebalance
        if date_str in rebal_set and i >= MIN_BARS:
            pos_ratio = get_position_ratio(idx_c, date) if idx_c is not None else 1.0
            if pos_ratio <= 0.30:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav
            else:
                top_codes = select_fn(date, i, panel_full, amt_full)
                n = min(len(top_codes), TOP_N)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                new_w = {c: pos_ratio/n for c in top_codes[:n]}

                enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w)/2 * COMMISSION * 2

                cp_s = panel_full.ffill().iloc[i]
                for c in new_set - old_set:
                    ep = cp_s.get(c)
                    if ep and not pd.isna(ep): entry_prices[c] = float(ep)
                for c in old_set - new_set:
                    entry_prices.pop(c, None); days_below_ma10.pop(c, None)
                if not cur_weights: entry_hwm = cumul_nav
                cur_weights = new_w; nav_since = 1.0

        # Step 4: stops
        if cur_weights and i > 0:
            if nav_since <= (1+PERIOD_STOP) or (cumul_nav/entry_hwm-1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav

        # Step 5: cash
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD/252
        nav_since *= (1+port_rets.iloc[i]); cumul_nav *= (1+port_rets.iloc[i])

    return (1+port_rets).cumprod()

# ══════════════════════════════════════════════════════════════════════
# Run both
# ══════════════════════════════════════════════════════════════════════
print('\n>>> 运行 S1(成交额TOP30)...'); nav_s1 = run_backtest(select_s1_turnover, 'S1')
print('>>> 运行 S2(多维筛选30)...'); nav_s2 = run_backtest(select_s2_multidim, 'S2')

m1 = calc_metrics(nav_s1); m2 = calc_metrics(nav_s2)
ar1 = pct(m1['年化收益率']); ar2 = pct(m2['年化收益率'])
sr1 = float(m1['夏普比率']); sr2 = float(m2['夏普比率'])
dd1 = pct(m1['最大回撤']); dd2 = pct(m2['最大回撤'])
vol1 = pct(m1['年化波动率']); vol2 = pct(m2['年化波动率'])

# ══════════════════════════════════════════════════════════════════════
# Output
# ══════════════════════════════════════════════════════════════════════
print(f'\n{"="*80}')
print(f'回测对比结果')
print(f'{"="*80}')
print(f'{"指标":<20} {"S1 成交额TOP30":>18} {"S2 多维筛选30":>18} {"差异":>12}')
print(f'{"─"*80}')
print(f'{"年化收益率":<20} {ar1*100:>17.1f}% {ar2*100:>17.1f}% {(ar2-ar1)*100:>+11.1f}%')
print(f'{"夏普比率":<20} {sr1:>18.2f} {sr2:>18.2f} {sr2-sr1:>+11.2f}')
print(f'{"最大回撤":<20} {dd1*100:>17.1f}% {dd2*100:>17.1f}% {(dd2-dd1)*100:>+11.1f}%')
print(f'{"年化波动率":<20} {vol1*100:>17.1f}% {vol2*100:>17.1f}% {(vol2-vol1)*100:>+11.1f}%')

# Year-by-year
print(f'\n逐年对比:')
print(f'{"年份":<8} {"S1年化":>10} {"S2年化":>10} {"差异":>10}')
years = sorted(set(d.year for d in all_dates if d.year >= 2020))
for y in years:
    n1 = nav_s1[(nav_s1.index >= f'{y}-01-01') & (nav_s1.index <= f'{y}-12-31')]
    n2 = nav_s2[(nav_s2.index >= f'{y}-01-01') & (nav_s2.index <= f'{y}-12-31')]
    if len(n1) > 1 and len(n2) > 1:
        r1 = n1.iloc[-1]/n1.iloc[0] - 1
        r2 = n2.iloc[-1]/n2.iloc[0] - 1
        days = len(n1)
        if days < 200: r1 = (1+r1)**(252/days) - 1; r2 = (1+r2)**(252/days) - 1
        print(f'{y:<8} {r1*100:>9.1f}% {r2*100:>9.1f}% {(r2-r1)*100:>+9.1f}%')

print(f'\n{"="*80}')
print(f'S1策略: 成交额TOP30 → 等权 → 买得起就进')
print(f'S2策略: TOP100成交额 → 多维打分(过热惩罚+EPS+MA+行业分散) → 取30只 → 等权')
print(f'{"="*80}')
