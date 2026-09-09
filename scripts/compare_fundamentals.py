"""
基本面因子贡献回测 — EPS/ROE/毛利率稳定性 是否值得接入
测试: (a)当前A-2基线 (b)去EPS (c)EPS换ROE (d)加毛利率稳定性
统一框架: CSI800, 双周, 0.175%成本, MA10止损
"""
import sys, os
sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from data.storage import load_meta, load_daily
from run_backtest_a2 import _make_rebal_dates, get_position_ratio
from run_backtest_a import (load_panels, calc_metrics, BACKTEST_START,
    COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP)
from run_backtest_a4 import MA10_EXIT_DAYS, MA_EXIT_WINDOW
from factors.utils import zscore, industry_zscore

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
print('═' * 80)
print('基本面因子贡献回测 — CSI800, 双周调仓, 2019-2025')
print('═' * 80)

# ── Load data ──
print('\n>>> Loading CSI800 data...')
cal = load_meta('trade_calendar')
end = sorted([d for d in cal['trade_date'] if d <= '2025-12-31'])[-1]
calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
rebal_dates = _make_rebal_dates(calendar, 'biweekly')
rebal_set = set(rebal_dates)

c800 = load_meta('csi800')
codes = sorted(c800['code'])
print(f'  Loading {len(codes)} stocks...')
panel, ap = load_panels(codes, BACKTEST_START, end)

idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None

info = load_meta('stock_info_full')
if not info.empty:
    info['code'] = info['code'].astype(str).str.zfill(6)
    ind_map = dict(zip(info['code'], info['industry_l1'].fillna('其他')))
else:
    ind_map = {}

all_dates = panel.index
TOP_N = 30

print(f'  Panel: {panel.shape[0]}d × {panel.shape[1]} stocks')

# ── Load fundamentals ──
print('  Loading fundamental data...')
fin = load_meta('financial_quarterly')
fin['report_date'] = fin['report_date'].astype(str)

vf = load_meta('value_factors')
vf['report_date'] = vf['report_date'].astype(str)

# ── Pre-compute factor z-scores per rebalance date ──
print('  Pre-computing factor scores...')

def get_momentum_z(panel, date_i):
    """Multi-period momentum, industry-z-scored. Same as compute_score_a2."""
    if date_i < 250:
        return None
    p1m = panel.iloc[date_i] / panel.iloc[max(0,date_i-20)] - 1
    p6m = panel.iloc[date_i] / panel.iloc[max(0,date_i-120)] - 1
    p12m = panel.iloc[date_i] / panel.iloc[max(0,date_i-240)] - 1
    # Clip extremes
    for s in [p1m, p6m, p12m]:
        s.clip(-0.95, 50, inplace=True)
    # Industry-z each
    z1m = industry_zscore(p1m, ind_map) if ind_map else zscore(p1m)
    z6m = industry_zscore(p6m, ind_map) if ind_map else zscore(p6m)
    z12m = industry_zscore(p12m, ind_map) if ind_map else zscore(p12m)
    return 0.30 * z1m + 0.40 * z6m + 0.30 * z12m

def get_sharpe_z(panel, date_i):
    """6M return / 6M std, industry-z-scored."""
    if date_i < 130:
        return None
    rets = panel.iloc[date_i-120:date_i].pct_change(fill_method=None)
    sharpe = rets.mean() / rets.std().replace(0, np.nan)
    sharpe = sharpe.replace([np.inf, -np.inf], np.nan).dropna()
    return industry_zscore(sharpe, ind_map) if ind_map else zscore(sharpe)

def get_eps_z(panel, date_i, date_str):
    """TTM EPS, industry-z-scored. Same logic as compute_score_a2 _get_eps_factor."""
    if fin.empty:
        return None
    rd = date_str.replace('-', '')
    fq = fin[fin['report_date'] <= rd].copy()
    if fq.empty:
        return None
    fq = fq.sort_values('report_date').groupby('code').last()
    eps_s = fq['eps'].dropna()
    eps_s = eps_s[eps_s > 0]
    common = [c for c in eps_s.index if c in panel.columns]
    if len(common) < TOP_N:
        return None
    return industry_zscore(eps_s[common], ind_map) if ind_map else zscore(eps_s[common])

def get_roe_z(panel, date_i, date_str):
    """ROE = TTM net_profit / equity. Industry-z-scored.
    Approximate equity from: eps / (roe implied). Actually use net_profit.
    Since we don't have equity directly, use eps as proxy for profitability
    and combine with bvps from value_factors for ROE ≈ eps / bvps.
    """
    if fin.empty or vf.empty:
        return None
    rd = date_str.replace('-', '')
    # Get latest EPS
    fq = fin[fin['report_date'] <= rd].copy()
    if fq.empty: return None
    fq = fq.sort_values('report_date').groupby('code').last()
    eps_s = fq['eps'].dropna()

    # Get latest BVPS
    vq = vf[vf['report_date'] <= rd].copy()
    if vq.empty: return None
    vq = vq.sort_values('report_date').groupby('code').last()
    bvps_s = vq['bvps'].dropna()

    # ROE proxy = eps / bvps (book value per share)
    common = set(eps_s.index) & set(bvps_s.index) & set(panel.columns)
    if len(common) < TOP_N:
        return None
    roe = {}
    for c in common:
        if bvps_s[c] > 0 and eps_s[c] > 0:
            roe[c] = eps_s[c] / bvps_s[c]
    if len(roe) < TOP_N:
        return None
    roe_s = pd.Series(roe).dropna()
    return industry_zscore(roe_s, ind_map) if ind_map else zscore(roe_s)

# ── Unified selection functions with different factor weights ──
def make_selector(label, w_mom, w_sharpe, w_eps, w_roe):
    """Factory for factor-weighted selectors."""
    def select_fn(date, i):
        date_str = str(date.date())
        if i < 250:
            return select_fallback(i), {}

        scores = {}
        components = []

        mom_z = get_momentum_z(panel, i)
        if mom_z is not None and w_mom > 0:
            components.append(('mom', w_mom, mom_z))

        sharpe_z = get_sharpe_z(panel, i)
        if sharpe_z is not None and w_sharpe > 0:
            components.append(('sharpe', w_sharpe, sharpe_z))

        eps_z = get_eps_z(panel, i, date_str)
        if eps_z is not None and w_eps > 0:
            components.append(('eps', w_eps, eps_z))

        roe_z = get_roe_z(panel, i, date_str)
        if roe_z is not None and w_roe > 0:
            components.append(('roe', w_roe, roe_z))

        if not components:
            return select_fallback(i), {}

        # Combine weighted z-scores
        total_w = sum(w for _, w, _ in components)
        combined = pd.Series(0.0, index=panel.columns)
        for _, w, z in components:
            aligned = z.reindex(panel.columns)
            combined = combined.add(aligned * (w / total_w), fill_value=0)

        combined = combined.dropna()
        if len(combined) < TOP_N:
            return select_fallback(i), {}

        return combined.nlargest(TOP_N).index.tolist(), {}

    select_fn.__name__ = f'select_{label}'
    return select_fn

def select_fallback(i):
    """Fallback: top by recent turnover."""
    if ap is not None and i >= 20:
        avg = ap.iloc[max(0,i-20):i].mean().dropna()
        avail = [c for c in avg.index if c in panel.columns]
        if len(avail) >= TOP_N:
            return avg[avail].nlargest(TOP_N).index.tolist()
    return sorted(panel.columns)[:TOP_N]

# ══════════════════════════════════════════════════════════════════════
# Unified backtest engine (same as compare_factors.py)
# ══════════════════════════════════════════════════════════════════════
def run_backtest(select_fn):
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}; days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i-1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp/pp - 1)
            port_rets.iloc[i] += ret

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

        if cur_weights and i > 0:
            if nav_since <= (1+PERIOD_STOP) or (cumul_nav/entry_hwm-1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav

        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD/252
        nav_since *= (1+port_rets.iloc[i])
        cumul_nav *= (1+port_rets.iloc[i])

    return (1+port_rets).cumprod()

def metric(nav):
    m = calc_metrics(nav)
    return {'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
            'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率'])}

# ══════════════════════════════════════════════════════════════════════
# Run tests
# ══════════════════════════════════════════════════════════════════════
print('\n>>> Running factor contribution tests...')

tests = [
    # (label, w_mom, w_sharpe, w_eps, w_roe)
    ('(a)当前A-2: 63%M+27%Q+10%EPS', 0.63, 0.27, 0.10, 0.00),
    ('(b)去EPS: 70%M+30%Q',            0.70, 0.30, 0.00, 0.00),
    ('(c)EPS换ROE: 63%M+27%Q+10%ROE', 0.63, 0.27, 0.00, 0.10),
    ('(d)加ROE: 60%M+25%Q+10%E+5%ROE',0.60, 0.25, 0.10, 0.05),
    ('(e)纯动量: 100%M',                 1.00, 0.00, 0.00, 0.00),
    ('(f)动量+质量: 70%M+30%Q(无基本面)',0.70, 0.30, 0.00, 0.00),
]

results = {}
for label, wm, wq, we, wr in tests:
    fn = make_selector(label, wm, wq, we, wr)
    print(f'  {label}...', end=' ', flush=True)
    nav = run_backtest(fn)
    m = metric(nav)
    results[label] = {**m, 'nav': nav}
    print(f'ar={m["ar"]*100:+.1f}% sr={m["sr"]:.2f} dd={m["dd"]*100:+.1f}%')

# ── Output ──
print()
print('=' * 90)
print('基本面因子贡献 — CSI800, 双周调仓, 2019-2025')
print('=' * 90)
print(f'{"":<42} {"年化":>7} {"夏普":>6} {"回撤":>7} {"vs(a)超额":>9}')
print('-' * 75)
baseline_ar = results['(a)当前A-2: 63%M+27%Q+10%EPS']['ar']
for label, _, _, _, _ in tests:
    r = results[label]
    excess = r['ar'] - baseline_ar
    print(f'{label:<42} {r["ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {excess*100:+8.1f}pp')

print('-' * 75)
print('解读:')
print('  (b)vs(a): EPS的边际贡献(正值=EPS有用, 负值=EPS是噪音)')
print('  (c)vs(a): ROE替代EPS的效果')
print('  (d)vs(a): 在原基础上加ROE的边际效果')
print('  (f)vs(a): 所有基本面的总贡献')
