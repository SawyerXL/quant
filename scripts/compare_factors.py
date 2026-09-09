"""
四因子对照测试 — 统一框架, 只换选股内核
(a) 等权全持(baseline) (b) 低波动 (c) 质量(ROE) (d) 价值(B/P)
"""
import sys, os
sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from data.storage import load_meta
from run_backtest_a2 import _make_rebal_dates, get_position_ratio
from run_backtest_a import (load_panels, calc_metrics, BACKTEST_START,
    COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP)
from run_backtest_a4 import MA10_EXIT_DAYS, MA_EXIT_WINDOW

def pct(s):
    return float(str(s).strip('%')) / 100

cal = load_meta('trade_calendar')
end = sorted([d for d in cal['trade_date'] if d <= '2026-12-31'])[-1]
calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
c800 = load_meta('csi800')
codes = sorted(c800['code'])
print('Loading price data...')
panel, ap = load_panels(codes, BACKTEST_START, end)
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None
d = _make_rebal_dates(calendar, 'biweekly')

# Load fundamental data
vf = load_meta('value_factors')
vf['report_date'] = vf['report_date'].astype(str)
print('Panel: %s  VF: %s' % (panel.shape, vf.shape))

all_dates = panel.index
rebal_set = set(d)
TOP_N = 30  # same as A-4

# ── Factor selection functions ──
def select_equal_weight(date, i):
    """(a) Equal weight: use all stocks, no selection"""
    # Just pick top TOP_N by recent turnover (closest to no selection)
    if ap is not None and i >= 20:
        avg_amt = ap.iloc[max(0,i-20):i].mean().dropna()
        return avg_amt.nlargest(TOP_N).index.tolist(), {}
    return sorted([c for c in panel.columns if c in set(c800['code'])])[:TOP_N], {}

def select_low_vol(date, i):
    """(b) Low volatility: 60-day std of returns, lowest first"""
    if i < 60:
        return select_equal_weight(date, i)
    rets = panel.iloc[i-60:i].pct_change(fill_method=None).std()
    low_vol = rets.dropna().nsmallest(TOP_N)
    return low_vol.index.tolist(), {}

def select_quality(date, i):
    """(c) Quality: highest ROE proxy (eps_ttm/bvps)"""
    if vf.empty:
        return select_equal_weight(date, i)
    date_str = str(date.date())
    # Find latest report before date
    vf_q = vf[vf['report_date'] <= date_str.replace('-','')].copy()
    vf_latest = vf_q.sort_values('report_date').groupby('code').last()
    vf_latest = vf_latest[(vf_latest['eps_ttm'] > 0) & (vf_latest['bvps'] > 0)]
    vf_latest['roe'] = vf_latest['eps_ttm'] / vf_latest['bvps']
    available = [c for c in vf_latest.index if c in panel.columns]
    if len(available) < TOP_N:
        return select_equal_weight(date, i)
    top = vf_latest.loc[available].nlargest(TOP_N, 'roe')
    return top.index.tolist(), {}

def select_value(date, i):
    """(d) Value: highest book-to-price (bvps / price)"""
    if vf.empty:
        return select_equal_weight(date, i)
    date_str = str(date.date())
    vf_q = vf[vf['report_date'] <= date_str.replace('-','')].copy()
    vf_latest = vf_q.sort_values('report_date').groupby('code').last()
    vf_latest = vf_latest[vf_latest['bvps'] > 0]
    cur_prices = panel.ffill().iloc[i]
    bp_ratios = {}
    for code in vf_latest.index:
        if code not in panel.columns: continue
        pr = cur_prices.get(code)
        bv = vf_latest.loc[code, 'bvps']
        if pr and not pd.isna(pr) and pr > 0:
            bp_ratios[code] = bv / pr
    bp_s = pd.Series(bp_ratios).dropna()
    if len(bp_s) < TOP_N:
        return select_equal_weight(date, i)
    return bp_s.nlargest(TOP_N).index.tolist(), {}

# ── Unified backtest loop ──
def run_unified(select_fn, label):
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0; pos_ratio = 1.0

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # Step 1: return
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
                ma10 = hist.mean(); cur_p = panel.iloc[i].get(code)
                if pd.isna(cur_p) or cur_p <= 0: continue
                if cur_p < ma10:
                    days_below_ma10[code] = days_below_ma10.get(code,0) + 1
                else:
                    days_below_ma10[code] = 0
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
                top_codes, _ = select_fn(date, i)
                n = min(len(top_codes), TOP_N)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                new_w = {c: pos_ratio/n for c in top_codes[:n]}

                enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w)/2 * COMMISSION * 2

                cp_s = panel.ffill().iloc[i]
                for c in new_set - old_set:
                    ep = cp_s.get(c)
                    if ep and not pd.isna(ep): entry_prices[c] = float(ep)
                for c in old_set - new_set:
                    entry_prices.pop(c, None); days_below_ma10.pop(c, None)
                if not cur_weights: entry_hwm = cumul_nav
                cur_weights = new_w
                nav_since = 1.0

        # Step 4: portfolio stop
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

# ── Run all four ──
tests = [
    ('(a)等权全持',    select_equal_weight),
    ('(b)低波动60日',  select_low_vol),
    ('(c)质量ROE',     select_quality),
    ('(d)价值B/P',     select_value),
]

results = {}
for label, fn in tests:
    print('Running %s...' % label, end=' ', flush=True)
    nav = run_unified(fn, label)
    m = calc_metrics(nav)
    results[label] = {'nav': nav, 'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
                       'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率'])}
    print('done (ann=%.1f%%)' % (results[label]['ar']*100))

# ── CAPM Alpha + HAC t-test ──
print()
print('Running CAPM regressions...')
# Build monthly returns
baseline_nav = results['(a)等权全持']['nav']
monthly_dates = baseline_nav.resample('ME').last().index
rf_m = 0.02/12

# Market returns from CSI800 index
idx_monthly = idx_c.resample('ME').last()
mkt_r = idx_monthly.pct_change().dropna()

results_table = []
for label in [t[0] for t in tests]:
    nav = results[label]['nav']
    nav_m = nav.resample('ME').last()
    strat_r = nav_m.pct_change().dropna()
    # Align
    common = strat_r.index.intersection(mkt_r.index)
    y = strat_r[common].values - rf_m
    X = mkt_r[common].values - rf_m

    # OLS
    Xc = np.column_stack([np.ones(len(X)), X])
    bh = np.linalg.inv(Xc.T @ Xc) @ Xc.T @ y
    alpha_m = bh[0]; beta = bh[1]
    alpha_a = (1+alpha_m)**12 - 1
    resid = y - Xc @ bh
    n = len(y)

    # HAC (Newey-West, lag=3)
    nw_lags = 3
    XtX_inv = np.linalg.inv(Xc.T @ Xc)
    S = Xc.T @ np.diag(resid**2) @ Xc / n
    for lag in range(1, nw_lags+1):
        w = 1 - lag/(nw_lags+1)
        G = Xc[lag:].T @ np.diag(resid[lag:]*resid[:-lag]) @ Xc[:-lag] / n
        S += w * (G + G.T)
    V_hac = XtX_inv @ S @ XtX_inv / n
    se_hac = np.sqrt(np.diag(V_hac))
    t_hac = alpha_m / se_hac[0]

    # Excess vs baseline (a)
    y_excess = (strat_r[common].values - baseline_nav.resample('ME').last().pct_change().dropna()[common].values)
    alpha_excess_m = np.mean(y_excess)
    alpha_excess_a = (1+alpha_excess_m)**12 - 1
    # HAC for excess
    n_e = len(y_excess)
    resid_e = y_excess - alpha_excess_m
    S_e = np.sum(resid_e**2)/n_e
    for lag in range(1, nw_lags+1):
        w = 1 - lag/(nw_lags+1)
        S_e += 2*w * np.sum(resid_e[lag:]*resid_e[:-lag])/n_e
    se_e_hac = np.sqrt(S_e/n_e)
    t_e_hac = alpha_excess_m / se_e_hac if se_e_hac > 0 else 0

    results_table.append({
        'label': label, 'ar': results[label]['ar'], 'sr': results[label]['sr'],
        'dd': results[label]['dd'], 'alpha_m': alpha_m, 'alpha_a': alpha_a,
        'beta': beta, 't_hac': t_hac, 'excess_a': alpha_excess_a, 't_excess_hac': t_e_hac,
    })

# ── Output table ──
print()
print('='*90)
print('四因子对照 — 统一框架(仓位管理+MA10止损+双周调仓)')
print('='*90)
print('%-16s  %7s  %6s  %7s  %7s  %7s  %7s  %6s' % (
    '选股内核','年化','夏普','回撤','CAPMα','vs(a)超额','HAC t','结论'))
print('-'*90)
for r in results_table:
    sig = '显著(p<0.05)' if abs(r['t_excess_hac']) > 1.96 else '不显著'
    print('%-16s  %+6.1f%%  %5.2f  %+6.1f%%  %+6.1f%%  %+6.1f%%  %+6.2f  %s' % (
        r['label'], r['ar']*100, r['sr'], r['dd']*100,
        r['alpha_a']*100, r['excess_a']*100, r['t_excess_hac'], sig))
print('-'*90)
print('HAC t使用Newey-West(lag=3)标准误。|t|>1.96=显著。vs(a)超额=选股特异收益。')
