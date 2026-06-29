"""
小盘四因子对照测试 — baostock含退市股数据, 统一框架(仓位管理+MA10止损+双周调仓)
(a) 等权全持(baseline) (b) 动量(12-1月) (c) 低波动(60日) (d) 价值(1/PB)
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

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
# 1. Load small-cap universe & build price panel from baostock
# ══════════════════════════════════════════════════════════════════════
print('═' * 70)
print('小盘四因子对照测试 — baostock含退市股')
print('═' * 70)

print('\n[1/4] Loading small-cap universe (baostock point-in-time)...')
sc_universe = pd.read_parquet('data_store/meta/smallcap_universe_bs.parquet')
all_codes = set()
for codes_str in sc_universe['codes']:
    all_codes.update(codes_str.split(','))
all_codes = sorted(all_codes)
print(f'  {len(all_codes)} unique codes across {len(sc_universe)} semiannual snapshots')
print(f'  Snapshot range: {sc_universe["date"].min()} → {sc_universe["date"].max()}')

# Calendar
cal = load_meta('trade_calendar')
end = sorted([d for d in cal['trade_date'] if d <= '2025-12-31'])[-1]
calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
rebal_dates = _make_rebal_dates(calendar, 'biweekly')
rebal_set = set(rebal_dates)
print(f'  Trading days: {len(calendar)}, rebalance dates: {len(rebal_dates)}')

# Market index for CAPM
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None
print(f'  CSI800 index: {len(idx_c)} days')

# ══════════════════════════════════════════════════════════════════════
# 2. Build price + pbMRQ panels from baostock daily parquets
# ══════════════════════════════════════════════════════════════════════
print('\n[2/4] Building price & pbMRQ panels from baostock...')

prices = {}
pbs = {}
amounts = {}
missing = 0
for code in all_codes:
    fpath = BAOSTOCK_DIR / f'{code}.parquet'
    if not fpath.exists():
        missing += 1
        continue
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    # Filter to backtest window
    df = df[(df.index >= BACKTEST_START) & (df.index <= end)]
    if len(df) < MIN_BARS:
        continue
    close_s = pd.to_numeric(df['close'], errors='coerce')
    pb_s = pd.to_numeric(df['pbMRQ'], errors='coerce')
    amt_s = pd.to_numeric(df['amount'], errors='coerce')
    # Drop zeros and negatives
    close_s = close_s.replace(0, np.nan).dropna()
    pb_s = pb_s.replace(0, np.nan).dropna()
    amt_s = amt_s.replace(0, np.nan).dropna()
    if len(close_s) >= MIN_BARS:
        prices[code] = close_s
        pbs[code] = pb_s
        amounts[code] = amt_s
if missing:
    print(f'  {missing} codes not found in baostock (delisted before 2019 or never in bs)')

panel = pd.DataFrame(prices).sort_index()
pb_panel = pd.DataFrame(pbs).sort_index()
ap = pd.DataFrame(amounts).sort_index()
print(f'  Price panel:  {panel.shape[0]} days × {panel.shape[1]} stocks')
print(f'  pbMRQ panel:  {pb_panel.shape[0]} days × {pb_panel.shape[1]} stocks')
print(f'  Amount panel: {ap.shape[0]} days × {ap.shape[1]} stocks')

# ══════════════════════════════════════════════════════════════════════
# 3. Factor selection functions
# ══════════════════════════════════════════════════════════════════════
TOP_N = 30
all_dates = panel.index

def select_equal_weight(date, i):
    """(a) Equal-weight baseline: pick by turnover (closest to no-selection)"""
    if ap is not None and i >= 20:
        avg_amt = ap.iloc[max(0,i-20):i].mean().dropna()
        available = [c for c in avg_amt.index if c in panel.columns]
        if len(available) >= TOP_N:
            return avg_amt[available].nlargest(TOP_N).index.tolist(), {}
    return sorted(panel.columns)[:TOP_N], {}

def select_momentum(date, i):
    """(b) Momentum: 12-1 month returns (240d skip 20d), highest first"""
    if i < 250:
        return select_equal_weight(date, i)
    # Returns from t-240 to t-20 (skip most recent month to avoid reversal)
    p_start = panel.iloc[i-240]
    p_end = panel.iloc[i-20]
    mom = (p_end / p_start - 1).dropna()
    # Remove extreme outliers
    mom = mom[(mom > -0.95) & (mom < 50)]
    available = [c for c in mom.index if c in panel.columns]
    if len(available) < TOP_N:
        return select_equal_weight(date, i)
    return mom[available].nlargest(TOP_N).index.tolist(), {}

def select_low_vol(date, i):
    """(c) Low volatility: 60-day std of daily returns, lowest first"""
    if i < 65:
        return select_equal_weight(date, i)
    rets = panel.iloc[i-60:i].pct_change(fill_method=None).std()
    low_vol = rets.dropna().nsmallest(TOP_N)
    if len(low_vol) < TOP_N:
        return select_equal_weight(date, i)
    return low_vol.index.tolist(), {}

def select_value(date, i):
    """(d) Value: highest B/P = 1/pbMRQ (cheapest stocks)"""
    if i < 5:
        return select_equal_weight(date, i)
    cur_pb = pb_panel.iloc[i]
    bp = (1.0 / cur_pb).dropna()
    bp = bp.replace([np.inf, -np.inf], np.nan).dropna()
    # Winsorize at 99th percentile to avoid extreme outliers
    cap = bp.quantile(0.99)
    bp = bp.clip(upper=cap)
    available = [c for c in bp.index if c in panel.columns]
    if len(available) < TOP_N:
        return select_equal_weight(date, i)
    return bp[available].nlargest(TOP_N).index.tolist(), {}

# ══════════════════════════════════════════════════════════════════════
# 4. Unified backtest loop (same as compare_factors.py)
# ══════════════════════════════════════════════════════════════════════
def run_unified(select_fn, label):
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0; pos_ratio = 1.0

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # Step 1: mark-to-market return
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
                port_rets.iloc[i] -= w * COMMISSION
                entry_prices.pop(code, None); days_below_ma10.pop(code, None)

        # Step 3: rebalance
        if date_str in rebal_set and i >= MIN_BARS:
            pos_ratio = get_position_ratio(idx_c, date) if idx_c is not None else 1.0
            if pos_ratio <= 0.30:
                # Regime filter: reduce to cash
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav
            else:
                top_codes, _ = select_fn(date, i)
                n = min(len(top_codes), TOP_N)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                new_w = {c: pos_ratio/n for c in top_codes[:n]}

                # Commission on turnover
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

        # Step 4: portfolio-level stops
        if cur_weights and i > 0:
            if nav_since <= (1+PERIOD_STOP) or (cumul_nav/entry_hwm-1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav

        # Step 5: cash yield on idle portion
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD/252
        nav_since *= (1+port_rets.iloc[i])
        cumul_nav *= (1+port_rets.iloc[i])

    return (1+port_rets).cumprod()

# ══════════════════════════════════════════════════════════════════════
# 5. Run all four factors
# ══════════════════════════════════════════════════════════════════════
print('\n[3/4] Running four-factor comparison...')
tests = [
    ('(a)等权全持',      select_equal_weight),
    ('(b)动量12-1月',   select_momentum),
    ('(c)低波动60日',    select_low_vol),
    ('(d)价值1/PB',     select_value),
]

results = {}
for label, fn in tests:
    print(f'  {label}...', end=' ', flush=True)
    nav = run_unified(fn, label)
    m = calc_metrics(nav)
    results[label] = {'nav': nav, 'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
                       'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率'])}
    print(f'ann={results[label]["ar"]*100:+.1f}%  sr={results[label]["sr"]:.2f}  dd={results[label]["dd"]*100:+.1f}%')

# ══════════════════════════════════════════════════════════════════════
# 6. CAPM Alpha + HAC t-test (Newey-West lag=3)
# ══════════════════════════════════════════════════════════════════════
print('\n[4/4] CAPM regressions + HAC inference...')
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
    # Align dates
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
    t_hac = alpha_m / se_hac[0] if se_hac[0] > 0 else 0

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

# ══════════════════════════════════════════════════════════════════════
# 7. Output
# ══════════════════════════════════════════════════════════════════════
print()
print('=' * 100)
print('小盘四因子对照 — baostock含退市股, 统一框架(仓位管理+MA10止损+双周调仓)')
print(f'股票池: {len(all_codes)}只(小盘段800-1800, point-in-time) | 持仓: TOP_{TOP_N} | 周期: {BACKTEST_START}→{end}')
print('=' * 100)
print('%-18s  %7s  %6s  %7s  %7s  %7s  %7s  %7s  %6s' % (
    '选股内核','年化','夏普','回撤','波动','CAPMα','vs(a)超额','HAC t','结论'))
print('-' * 100)
for r in results_table:
    sig = '显著(p<0.05)' if abs(r['t_excess_hac']) > 1.96 else '不显著'
    print('%-18s  %+6.1f%%  %5.2f  %+6.1f%%  %+5.1f%%  %+6.1f%%  %+6.1f%%  %+6.2f  %s' % (
        r['label'], r['ar']*100, r['sr'], r['dd']*100, r.get('vol',0)*100 if 'vol' in r else results[r['label']]['vol']*100,
        r['alpha_a']*100, r['excess_a']*100, r['t_excess_hac'], sig))
print('-' * 100)
print('HAC t使用Newey-West(lag=3)标准误。|t|>1.96=显著。vs(a)超额=选股特异收益。')
print('数据源: baostock (含退市股, 5779只日线), 无生存者偏差。')
print()

# Year-by-year breakdown
print('逐年年化收益率:')
print('%-18s' % '选股内核', end='')
years = sorted(set(d.year for d in all_dates if d.year >= 2020))
for y in years:
    print('  %6s' % str(y), end='')
print()
for label in [t[0] for t in tests]:
    nav = results[label]['nav']
    print('%-18s' % label, end='')
    for y in years:
        yn = nav[(nav.index >= f'{y}-01-01') & (nav.index <= f'{y}-12-31')]
        if len(yn) > 1:
            y_ret = (yn.iloc[-1]/yn.iloc[0] - 1)
            # Annualize if partial year
            n_days = len(yn)
            if n_days < 200:
                y_ret = (1+y_ret)**(252/n_days) - 1
            print('  %+6.1f%%' % (y_ret*100), end='')
        else:
            print('  %6s' % 'N/A', end='')
    print()
