"""
小盘低波动 — 专攻降换手
测试: ①去MA10止损 ②季频调仓 ③排名平滑 ④最优组合
主口径: 0.3%单边成本  目标: 换手<250% + 净年化≥8% + 夏普≥0.8
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
REAL_COST = 0.003

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
print('═' * 80)
print('小盘低波动 — 专攻降换手 (主口径: 0.3%成本)')
print('═' * 80)

# ── Load data ──
print('\n>>> Loading data...')
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

prices, amounts = {}, {}
for code in all_codes:
    fpath = BAOSTOCK_DIR / f'{code}.parquet'
    if not fpath.exists(): continue
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date']); df = df.set_index('date').sort_index()
    df = df[(df.index >= BACKTEST_START) & (df.index <= end)]
    if len(df) < MIN_BARS: continue
    cs = pd.to_numeric(df['close'], errors='coerce').replace(0, np.nan).dropna()
    am = pd.to_numeric(df['amount'], errors='coerce').replace(0, np.nan).dropna()
    if len(cs) >= MIN_BARS:
        prices[code] = cs; amounts[code] = am

panel = pd.DataFrame(prices).sort_index()
ap = pd.DataFrame(amounts).sort_index()
all_dates = panel.index
print(f'  Panel: {panel.shape[0]}d × {panel.shape[1]} stocks')

# ── Quarterly rebalance dates ──
def _make_quarterly_dates(calendar):
    """Last trading day of Mar/Jun/Sep/Dec."""
    dates = pd.DatetimeIndex(sorted(calendar))
    result = []
    for yr in range(dates[0].year, dates[-1].year + 1):
        for mo in [3, 6, 9, 12]:
            md = dates[(dates.year == yr) & (dates.month == mo)]
            if len(md):
                result.append(str(md[-1].date()))
    return sorted(set(result))

# ── Selection with rank smoothing ──
def select_low_vol_smoothed(rank_history, date, i, n_top=30, smooth_n=1):
    """
    rank_history: list of dicts [{code: rank}, ...] from past rebalance dates.
    smooth_n: number of periods to average (1 = raw).
    Returns (selected_codes, updated_rank_history).
    """
    if i < 65:
        # Fallback: equal-weight
        return select_fallback(date, i, n_top), rank_history

    rets = panel.iloc[i-60:i].pct_change(fill_method=None).std()
    lv = rets.dropna()

    # Current period ranks (1 = lowest vol)
    cur_ranks = {c: r+1 for r, c in enumerate(lv.nsmallest(len(lv)).index)}

    rank_history.append(cur_ranks)
    if len(rank_history) > smooth_n:
        rank_history = rank_history[-smooth_n:]

    if smooth_n == 1 or len(rank_history) == 1:
        avg_ranks = cur_ranks
    else:
        # Average rank across periods
        all_codes_in_history = set()
        for rh in rank_history:
            all_codes_in_history.update(rh.keys())
        avg_ranks = {}
        for c in all_codes_in_history:
            ranks = [rh.get(c, 9999) for rh in rank_history]
            avg_ranks[c] = np.mean(ranks)

    sorted_codes = sorted(avg_ranks, key=lambda c: avg_ranks[c])
    return sorted_codes[:n_top], rank_history

def select_fallback(date, i, n_top):
    if ap is not None and i >= 20:
        avg = ap.iloc[max(0,i-20):i].mean().dropna()
        avail = [c for c in avg.index if c in panel.columns]
        if len(avail) >= n_top:
            return avg[avail].nlargest(n_top).index.tolist()
    return sorted(panel.columns)[:n_top]

# ══════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE (parameterized)
# ══════════════════════════════════════════════════════════════════════
def run_backtest(n_hold=30, freq='monthly', use_ma10=True, use_ma200=False,
                 smooth_n=1, commission=REAL_COST):
    """
    Returns: (nav, turnover_annual, n_rebals)
    """
    if freq == 'quarterly':
        rebal_dates = _make_quarterly_dates(calendar)
    else:
        rebal_dates = _make_rebal_dates(calendar, freq)
    rebal_set = set(rebal_dates)

    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0
    turnover_sum = 0.0; n_rebals = 0
    rank_history = []  # for smoothed ranking

    for i, date in enumerate(all_dates):
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

        # Step 2: MA10 exit (optional)
        if use_ma10 and cur_weights and i >= 10:
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
                rank_history = []
            else:
                pos_ratio = raw_pos if use_ma200 else 1.0
                top_codes, rank_history = select_low_vol_smoothed(
                    rank_history, date, i, n_top=n_hold, smooth_n=smooth_n)
                n = min(len(top_codes), n_hold)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                new_w = {c: pos_ratio/n for c in top_codes[:n]}

                enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w)/2 * commission * 2

                turnover_sum += (enter_w + exit_w) / 2
                n_rebals += 1

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

    n_days = len(all_dates); n_years = n_days / 252
    rebal_per_year = n_rebals / n_years if n_years > 0 else 0
    turnover_annual = (turnover_sum / n_rebals * rebal_per_year) if n_rebals > 0 else 0

    return (1+port_rets).cumprod(), turnover_annual, n_rebals

def metric(nav):
    m = calc_metrics(nav)
    return {'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
            'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率'])}

# ══════════════════════════════════════════════════════════════════════
# TEST 1: Remove MA10 stop
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试1: 去掉MA10止损 — 低波·月频·无择时·30只·0.3%成本')
print('=' * 80)

results_ma10 = {}
for ma10_label, use_ma10 in [('有MA10止损', True), ('无MA10止损', False)]:
    nav, to, n_r = run_backtest(use_ma10=use_ma10)
    m = metric(nav)
    results_ma10[ma10_label] = {**m, 'turnover': to, 'n_rebals': n_r}
    print(f'  {ma10_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% turnover={to:.0%} rebalances={n_r}')

print()
cost_drag_yes = results_ma10['有MA10止损']['turnover'] * REAL_COST * 2
cost_drag_no  = results_ma10['无MA10止损']['turnover'] * REAL_COST * 2
net_yes = results_ma10['有MA10止损']['ar'] - cost_drag_yes
net_no  = results_ma10['无MA10止损']['ar'] - cost_drag_no
print(f'{"":<16} {"毛年化":>7} {"夏普":>6} {"回撤":>7} {"年化换手":>8} {"换手成本":>8} {"净年化":>7} {"调仓次数":>8}')
print('-' * 78)
print(f'{"有MA10止损":<16} {results_ma10["有MA10止损"]["ar"]*100:+6.1f}% {results_ma10["有MA10止损"]["sr"]:5.2f} {results_ma10["有MA10止损"]["dd"]*100:+6.1f}% {results_ma10["有MA10止损"]["turnover"]:7.0%} {cost_drag_yes*100:+7.1f}% {net_yes*100:+6.1f}% {results_ma10["有MA10止损"]["n_rebals"]:>8}')
print(f'{"无MA10止损":<16} {results_ma10["无MA10止损"]["ar"]*100:+6.1f}% {results_ma10["无MA10止损"]["sr"]:5.2f} {results_ma10["无MA10止损"]["dd"]*100:+6.1f}% {results_ma10["无MA10止损"]["turnover"]:7.0%} {cost_drag_no*100:+7.1f}% {net_no*100:+6.1f}% {results_ma10["无MA10止损"]["n_rebals"]:>8}')

delta_to = results_ma10['无MA10止损']['turnover'] - results_ma10['有MA10止损']['turnover']
delta_ar = net_no - net_yes
print(f'  去掉MA10: 换手 {delta_to:+.0%}, 净年化 {delta_ar*100:+.1f}pp, 回撤 {results_ma10["无MA10止损"]["dd"]*100:+.1f}% vs {results_ma10["有MA10止损"]["dd"]*100:+.1f}%')

# ══════════════════════════════════════════════════════════════════════
# TEST 2: Push to quarterly
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试2: 调仓频率推至季频 — 低波·无MA10·无择时·30只·0.3%成本')
print('=' * 80)

results_freq2 = {}
for freq_label, freq in [('月频', 'monthly'), ('季频', 'quarterly')]:
    nav, to, n_r = run_backtest(use_ma10=False, freq=freq)
    m = metric(nav)
    results_freq2[freq_label] = {**m, 'turnover': to, 'n_rebals': n_r}
    print(f'  {freq_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% turnover={to:.0%} rebalances={n_r}')

print()
print(f'{"":<12} {"毛年化":>7} {"夏普":>6} {"回撤":>7} {"年化换手":>8} {"换手成本":>8} {"净年化":>7} {"调仓次数":>8}')
print('-' * 74)
for label in ['月频', '季频']:
    r = results_freq2[label]
    cd = r['turnover'] * REAL_COST * 2
    net = r['ar'] - cd
    print(f'{label:<12} {r["ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["turnover"]:7.0%} {cd*100:+7.1f}% {net*100:+6.1f}% {r["n_rebals"]:>8}')

# ══════════════════════════════════════════════════════════════════════
# TEST 3: Rank smoothing
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试3: 排名平滑降轮动 — 低波·月频·无MA10·无择时·30只·0.3%成本')
print('=' * 80)

results_smooth = {}
for smooth_label, smooth_n in [('N=1(原始)', 1), ('N=3(三期均值)', 3)]:
    nav, to, n_r = run_backtest(use_ma10=False, smooth_n=smooth_n)
    m = metric(nav)
    results_smooth[smooth_label] = {**m, 'turnover': to, 'n_rebals': n_r}
    print(f'  {smooth_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% turnover={to:.0%}')

print()
print(f'{"":<16} {"毛年化":>7} {"夏普":>6} {"回撤":>7} {"年化换手":>8} {"换手成本":>8} {"净年化":>7}')
print('-' * 72)
for label in ['N=1(原始)', 'N=3(三期均值)']:
    r = results_smooth[label]
    cd = r['turnover'] * REAL_COST * 2
    net = r['ar'] - cd
    print(f'{label:<16} {r["ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["turnover"]:7.0%} {cd*100:+7.1f}% {net*100:+6.1f}%')

# Also test rank smoothing on quarterly
print('\n  ── 季频+排名平滑 ──')
for smooth_label, smooth_n in [('季频+N=1', 1), ('季频+N=2(两期均值)', 2)]:
    nav, to, n_r = run_backtest(use_ma10=False, freq='quarterly', smooth_n=smooth_n)
    m = metric(nav)
    cd = to * REAL_COST * 2
    net = m['ar'] - cd
    print(f'  {smooth_label}: 毛ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% turnover={to:.0%} 净ar={net*100:+.1f}%')

# ══════════════════════════════════════════════════════════════════════
# TEST 4: Optimal combo
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试4: 最优组合确认 — 对照表')
print('=' * 80)

combos = [
    # (label, n_hold, freq, use_ma10, use_ma200, smooth_n)
    ('基准:双周+MA10+择时',   30, 'biweekly',  True,  True,  1),
    ('A:月频+无MA10+无择时',    30, 'monthly',   False, False, 1),
    ('B:月频+无MA10+择时',      30, 'monthly',   False, True,  1),
    ('C:季频+无MA10+择时',      30, 'quarterly', False, True,  1),
    ('D:季频+无MA10+择时+平滑2', 30, 'quarterly', False, True,  2),
]

results_combo = {}
for label, n, freq, ma10, ma200, sn in combos:
    nav, to, n_r = run_backtest(n_hold=n, freq=freq, use_ma10=ma10, use_ma200=ma200, smooth_n=sn)
    m = metric(nav)
    cd = to * REAL_COST * 2
    net = m['ar'] - cd

    def dd_crunch(nv):
        c = nv[(nv.index >= '2024-01-02') & (nv.index <= '2024-02-29')]
        return float(((c / c.cummax() - 1) * 100).min())

    cdd = dd_crunch(nav)
    results_combo[label] = {**m, 'turnover': to, 'net': net, 'cdd': cdd, 'n_rebals': n_r}
    print(f'  {label}: 毛ar={m["ar"]*100:+.1f}% 净ar={net*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% to={to:.0%}')

print()
print(f'{"":<28} {"毛年化":>7} {"换手成本":>8} {"净年化":>7} {"夏普":>6} {"回撤":>7} {"踩踏回撤":>8} {"年化换手":>8} {"调仓":>4}')
print('-' * 95)
for label, n, freq, ma10, ma200, sn in combos:
    r = results_combo[label]
    cd = r['turnover'] * REAL_COST * 2
    judge = '✓' if (r['net'] >= 0.08 and r['sr'] >= 0.8 and r['turnover'] < 2.5) else ''
    print(f'{label:<28} {r["ar"]*100:+6.1f}% {cd*100:+7.1f}% {r["net"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["cdd"]:+7.2f}% {r["turnover"]:7.0%} {r["n_rebals"]:>4} {judge}')

print()
print('目标: 换手<250% + 净年化≥8% + 夏普≥0.8。主口径0.3%成本。')
print('═' * 80)
