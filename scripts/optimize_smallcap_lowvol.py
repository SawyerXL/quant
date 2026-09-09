"""
小盘低波动实盘化优化 — 降换手/做减法
测试: ①调仓频率 ②缓冲带 ③持仓数量 ④择时取舍
主口径: 0.3%单边成本
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
REAL_COST = 0.003  # 0.3% — 主口径

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
print('═' * 80)
print('小盘低波动 — 实盘化优化 (主口径: 0.3%单边成本)')
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
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
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

# ── Factor function ──
def select_low_vol(date, i, n_top=30, cur_hold=None, buffer=0):
    """
    n_top: target number of holdings
    cur_hold: set of currently held codes (for buffer)
    buffer: if >0, keep held stocks still in top (n_top+buffer)
    Returns (selected_codes_list, info_dict)
    """
    if i < 65:
        return select_fallback(date, i, n_top), {}
    rets = panel.iloc[i-60:i].pct_change(fill_method=None).std()
    lv = rets.dropna().nsmallest(max(n_top + buffer, n_top * 2))

    if cur_hold and buffer > 0:
        # Keep held stocks still in top (n_top+buffer)
        held_keeping = [c for c in cur_hold if c in lv.index[:n_top+buffer]]
        # Fill remaining slots from ranking, excluding already-kept
        remaining = [c for c in lv.index if c not in set(held_keeping)]
        selected = held_keeping + remaining[:n_top - len(held_keeping)]
        return selected[:n_top], {}
    else:
        if len(lv) < n_top:
            return select_fallback(date, i, n_top), {}
        return lv.index[:n_top].tolist(), {}

def select_fallback(date, i, n_top):
    """Equal-weight by turnover as fallback"""
    if ap is not None and i >= 20:
        avg = ap.iloc[max(0,i-20):i].mean().dropna()
        avail = [c for c in avg.index if c in panel.columns]
        if len(avail) >= n_top:
            return avg[avail].nlargest(n_top).index.tolist()
    return sorted(panel.columns)[:n_top]

# ══════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE (with turnover tracking)
# ══════════════════════════════════════════════════════════════════════
def run_backtest(select_fn, n_hold=30, buffer=0, freq='biweekly',
                 use_ma200=False, commission=REAL_COST):
    """
    Returns: (nav, turnover_annual)
    turnover_annual = average per-rebalance turnover × rebalances_per_year
    """
    rebal_dates = _make_rebal_dates(calendar, freq)
    rebal_set = set(rebal_dates)

    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0
    turnover_sum = 0.0; n_rebals = 0

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
                cur_hold = set(cur_weights.keys())
                top_codes, _ = select_fn(date, i, n_top=n_hold, cur_hold=cur_hold, buffer=buffer)
                n = min(len(top_codes), n_hold)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                new_w = {c: pos_ratio/n for c in top_codes[:n]}

                enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w)/2 * commission * 2

                # Track turnover
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

    # Annualize turnover: total turnover / number of rebalances × rebalances_per_year
    n_days = len(all_dates)
    n_years = n_days / 252
    rebal_per_year = n_rebals / n_years if n_years > 0 else 0
    turnover_annual = (turnover_sum / n_rebals * rebal_per_year) if n_rebals > 0 else 0

    return (1+port_rets).cumprod(), turnover_annual

def metric(nav):
    m = calc_metrics(nav)
    return {'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
            'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率'])}

# ══════════════════════════════════════════════════════════════════════
# TEST 1: Frequency — biweekly vs monthly
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试1: 调仓频率 — 双周 vs 月频 (低波·无择时·30只·无缓冲·0.3%成本)')
print('=' * 80)

results_freq = {}
for freq_label, freq in [('双周', 'biweekly'), ('月频', 'monthly')]:
    nav, to = run_backtest(select_low_vol, n_hold=30, freq=freq, use_ma200=False)
    m = metric(nav)
    results_freq[freq_label] = {**m, 'turnover': to}
    print(f'  {freq_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% turnover={to:.0%}')

print()
print(f'{"":<12} {"年化":>7} {"夏普":>6} {"回撤":>7} {"波动":>7} {"年化换手":>8} {"减:换手成本":>10} {"净年化":>7}')
print('-' * 72)
for label in ['双周', '月频']:
    r = results_freq[label]
    # Approximate: net = gross - (turnover × cost_rate × 2) / years
    # More precisely, turnover_annual × commission × 2 (buy+sell)
    cost_drag = r['turnover'] * REAL_COST * 2  # both sides
    net_ar = r['ar'] - cost_drag
    print(f'{label:<12} {r["ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["vol"]*100:+5.1f}% {r["turnover"]:7.0%} {cost_drag*100:+9.1f}% {net_ar*100:+6.1f}%')

# ══════════════════════════════════════════════════════════════════════
# TEST 2: Buffer zone — keep if still in top 40
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试2: 缓冲带 — 持仓保留若仍在前40名 (低波·无择时·30只·0.3%成本)')
print('=' * 80)

results_buf = {}
for buf_label, buf, freq in [('无缓冲/双周', 0, 'biweekly'), ('缓冲10/双周', 10, 'biweekly'),
                                ('无缓冲/月频', 0, 'monthly'), ('缓冲10/月频', 10, 'monthly')]:
    nav, to = run_backtest(select_low_vol, n_hold=30, buffer=buf, freq=freq, use_ma200=False)
    m = metric(nav)
    results_buf[buf_label] = {**m, 'turnover': to}
    print(f'  {buf_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} turnover={to:.0%}')

print()
print(f'{"":<16} {"年化":>7} {"夏普":>6} {"回撤":>7} {"年化换手":>8} {"减:换手成本":>10} {"净年化":>7}')
print('-' * 70)
for label in ['无缓冲/双周', '缓冲10/双周', '无缓冲/月频', '缓冲10/月频']:
    r = results_buf[label]
    cost_drag = r['turnover'] * REAL_COST * 2
    net_ar = r['ar'] - cost_drag
    print(f'{label:<16} {r["ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["turnover"]:7.0%} {cost_drag*100:+9.1f}% {net_ar*100:+6.1f}%')

# ══════════════════════════════════════════════════════════════════════
# TEST 3: Number of holdings — 30 vs 50
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试3: 持仓数量 — 30只 vs 50只 (低波·无择时·0.3%成本)')
print('=' * 80)

results_n = {}
for n_label, n, freq in [('30只/双周', 30, 'biweekly'), ('50只/双周', 50, 'biweekly'),
                           ('30只/月频', 30, 'monthly'), ('50只/月频', 50, 'monthly')]:
    nav, to = run_backtest(select_low_vol, n_hold=n, freq=freq, use_ma200=False)
    m = metric(nav)
    single_wt = 1.0 / n  # equal weight
    results_n[n_label] = {**m, 'turnover': to, 'single_wt': single_wt}
    print(f'  {n_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% single={single_wt:.1%}')

print()
print(f'{"":<16} {"年化":>7} {"夏普":>6} {"回撤":>7} {"单票权重":>8} {"年化换手":>8} {"减:换手成本":>10} {"净年化":>7}')
print('-' * 78)
for label in ['30只/双周', '50只/双周', '30只/月频', '50只/月频']:
    r = results_n[label]
    cost_drag = r['turnover'] * REAL_COST * 2
    net_ar = r['ar'] - cost_drag
    print(f'{label:<16} {r["ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["single_wt"]:7.1%} {r["turnover"]:7.0%} {cost_drag*100:+9.1f}% {net_ar*100:+6.1f}%')

# ══════════════════════════════════════════════════════════════════════
# TEST 4: Timing trade-off — no timing vs MA200
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 80)
print('测试4: 择时取舍 — 无择时 vs MA200 (低波·30只·双周·0.3%成本)')
print('=' * 80)

results_timing = {}
for t_label, use_ma in [('无择时(满仓)', False), ('MA200择时', True)]:
    nav, to = run_backtest(select_low_vol, n_hold=30, use_ma200=use_ma)
    m = metric(nav)
    results_timing[t_label] = {**m, 'turnover': to}
    print(f'  {t_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% turnover={to:.0%}')

# Also run with monthly
for t_label, use_ma in [('无择时(月频)', False), ('MA200(月频)', True)]:
    nav, to = run_backtest(select_low_vol, n_hold=30, freq='monthly', use_ma200=use_ma)
    m = metric(nav)
    results_timing[t_label] = {**m, 'turnover': to}
    print(f'  {t_label}: ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}% turnover={to:.0%}')

print()
print(f'{"":<18} {"年化":>7} {"夏普":>6} {"回撤":>7} {"年化换手":>8} {"减:换手成本":>10} {"净年化":>7} {"2024.1-2回撤":>12}')
print('-' * 85)

def crunch_dd(nav):
    c = nav[(nav.index >= '2024-01-02') & (nav.index <= '2024-02-29')]
    peak = c.cummax()
    return float(((c / peak - 1) * 100).min())

for label in ['无择时(满仓)', 'MA200择时', '无择时(月频)', 'MA200(月频)']:
    r = results_timing[label]
    nav, _ = run_backtest(select_low_vol, n_hold=30,
                          freq='monthly' if '月频' in label else 'biweekly',
                          use_ma200=('MA200' in label))
    cdd = crunch_dd(nav)
    cost_drag = r['turnover'] * REAL_COST * 2
    net_ar = r['ar'] - cost_drag
    print(f'{label:<18} {r["ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["turnover"]:7.0%} {cost_drag*100:+9.1f}% {net_ar*100:+6.1f}% {cdd:+9.2f}%')

print()
print('═' * 80)
print('优化四表完毕。主口径: 0.3%单边成本。目标: 净年化≥8% + 夏普≥0.8。')
print('═' * 80)
