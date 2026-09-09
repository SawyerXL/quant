"""
框架优化对照 — 选股固定等权全持
测试: (a)基线 (b)+IF对冲 (c)波动率目标 (d)两者叠加
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
import run_backtest_a as a_mod

def pct(s):
    return float(str(s).strip('%')) / 100

cal = load_meta('trade_calendar')
end = sorted([d for d in cal['trade_date'] if d <= '2026-12-31'])[-1]
calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
c800 = load_meta('csi800')
codes = sorted(c800['code'])
print('Loading data...')
panel, ap = load_panels(codes, BACKTEST_START, end)
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None
d = _make_rebal_dates(calendar, 'biweekly')

all_dates = panel.index
rebal_set = set(d)
TOP_N = 30
c800_set = set(c800['code'])

# Equal-weight stock basket daily return
c800_cols = sorted([c for c in panel.columns if c in c800_set])
stock_rets = panel[c800_cols].pct_change(fill_method=None).mean(axis=1)  # daily EW return

# CSI800 index return (for hedge)
if idx_c is not None:
    idx_rets = idx_c.pct_change().reindex(all_dates).fillna(0)
else:
    idx_rets = stock_rets  # fallback

# ── Unified backtest ──
def run(label, hedge=False, vol_target=False):
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0
    pos_ratio = 1.0
    realized_vol = 0.15  # initial guess
    HEDGE_COST = 0.0003   # IF futures round-trip ≈ 0.03% (roll cost)
    TARGET_VOL = 0.15

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # Step 0: Determine position ratio
        if i >= MIN_BARS:
            if vol_target:
                # Vol targeting: position = target_vol / trailing_60d_realized_vol
                if i >= 60:
                    daily_std = stock_rets.iloc[i-60:i].std()
                    if daily_std > 0:
                        realized_vol = daily_std * np.sqrt(252)
                vol_pos = TARGET_VOL / max(realized_vol, 0.05)
                pos_ratio = max(0.30, min(1.0, vol_pos))
            else:
                # Original MA200 5-tier
                pos_ratio = get_position_ratio(idx_c, date) if idx_c is not None else 1.0

        # Step 1: daily return (old weights)
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i-1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp/pp - 1)
            port_rets.iloc[i] += ret

        # IF Hedge: short index when regime says reduce
        if hedge and i >= MIN_BARS and pos_ratio < 1.0:
            hedge_pct = 1.0 - pos_ratio
            # Short index position: hedge_pct of portfolio
            hedge_ret = -hedge_pct * idx_rets.iloc[i]
            port_rets.iloc[i] += hedge_ret
            # Roll cost (apply monthly, approximately every 21 trading days)
            if i % 21 == 0:
                port_rets.iloc[i] -= hedge_pct * HEDGE_COST

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
            if pos_ratio <= 0.30 and not hedge:
                # With hedge, we stay invested even in bear (just short index)
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav
            else:
                if ap is not None and i >= 20:
                    avg_amt = ap.iloc[max(0,i-20):i].mean().dropna()
                    top_codes = avg_amt.nlargest(min(TOP_N, len(avg_amt))).index.tolist()
                else:
                    top_codes = c800_cols[:min(TOP_N, len(c800_cols))]

                n = len(top_codes)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                # With hedge: always 100% stock allocation (hedge handles downside)
                stock_weight = 1.0 if hedge else pos_ratio
                new_w = {c: stock_weight/n for c in top_codes[:n]}

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
        # With hedge: stock_weight + hedge = 1.0 when pos_ratio<1, but stocks still 100%
        # Without hedge: stock_weight = pos_ratio, rest is cash
        stock_alloc = 1.0 if hedge else pos_ratio
        hedge_alloc = (1.0 - pos_ratio) if (hedge and pos_ratio < 1.0) else 0.0
        cash_alloc = max(0, 1.0 - stock_alloc - hedge_alloc)
        port_rets.iloc[i] += cash_alloc * CASH_YIELD/252

        nav_since *= (1+port_rets.iloc[i])
        cumul_nav *= (1+port_rets.iloc[i])

    return (1+port_rets).cumprod()

# ── Run all four ──
tests = [
    ('(a)基线(等权+MA200五档)', False, False),
    ('(b)+IF对冲(熊市短IF)',    True,  False),
    ('(c)波动率目标15%',        False, True),
    ('(d)对冲+波动率目标',      True,  True),
]

results = {}
for label, hedge, vt in tests:
    print('Running %s...' % label, end=' ', flush=True)
    nav = run(label, hedge=hedge, vol_target=vt)
    m = calc_metrics(nav)
    results[label] = {
        'nav': nav,
        'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
        'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率']),
    }
    calmar = results[label]['ar'] / abs(results[label]['dd']) if results[label]['dd'] != 0 else 0
    results[label]['calmar'] = calmar
    print('done (ann=%.1f%% sr=%.2f dd=%.1f%% calmar=%.2f)' % (
        results[label]['ar']*100, results[label]['sr'],
        results[label]['dd']*100, calmar))

# ── CAPM Alpha + HAC ──
print()
print('CAPM + HAC...')
idx_monthly = idx_c.resample('ME').last()
mkt_r = idx_monthly.pct_change().dropna()
rf_m = 0.02/12

table = []
for label in [t[0] for t in tests]:
    nav = results[label]['nav']
    nav_m = nav.resample('ME').last()
    strat_r = nav_m.pct_change().dropna()
    common = strat_r.index.intersection(mkt_r.index)
    y = strat_r[common].values - rf_m
    X = mkt_r[common].values - rf_m
    Xc = np.column_stack([np.ones(len(X)), X])
    bh = np.linalg.inv(Xc.T @ Xc) @ Xc.T @ y
    alpha_m = bh[0]; beta = bh[1]
    alpha_a = (1+alpha_m)**12 - 1
    resid = y - Xc @ bh
    n = len(y)

    # HAC Newey-West lag=3
    l = 3
    XtX = np.linalg.inv(Xc.T @ Xc)
    S = Xc.T @ np.diag(resid**2) @ Xc / n
    for lag in range(1, l+1):
        w = 1 - lag/(l+1)
        G = Xc[lag:].T @ np.diag(resid[lag:]*resid[:-lag]) @ Xc[:-lag] / n
        S += w * (G + G.T)
    se_hac = np.sqrt(np.diag(XtX @ S @ XtX / n))
    t_hac = alpha_m / se_hac[0]

    r = results[label]
    table.append({
        'label': label, 'ar': r['ar'], 'sr': r['sr'],
        'dd': r['dd'], 'calmar': r['calmar'], 'vol': r['vol'],
        'alpha_a': alpha_a, 'beta': beta, 't_hac': t_hac,
    })

# ── Output ──
print()
print('='*95)
print('框架优化对照 — 选股固定: 等权全持30只')
print('='*95)
print('%-28s  %7s  %6s  %7s  %7s  %7s  %7s  %6s' % (
    '框架','年化','夏普','回撤','波动','Calmar','CAPMα','|t|'))
print('-'*95)
baseline_sr = table[0]['sr']
for r in table:
    sr_flag = '✓' if r['sr'] > 0.8 else ('↑%.2f' % (r['sr']-baseline_sr))
    print('%-28s  %+6.1f%%  %5.2f  %+6.1f%%  %5.1f%%  %6.2f  %+6.1f%%  %5.2f  %s' % (
        r['label'], r['ar']*100, r['sr'], r['dd']*100, r['vol']*100,
        r['calmar'], r['alpha_a']*100, r['t_hac'], sr_flag))
print('-'*95)
print('IF对冲代理:CSI800指数(非IF,实盘有跟踪误差) | 夏普列右边为vs基线的改善')
print('CAPMα用HAC t(Newey-West lag=3); |t|>1.96=显著')
