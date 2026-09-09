"""
方案C回测: Top 50 大市值(按成交额)等权 + 全风控框架
vs 原A-4 vs CSI800等权基准
"""
import sys, os
sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from data.storage import load_meta
from run_backtest_a2 import _make_rebal_dates, get_position_ratio, MA_PERIOD
from run_backtest_a import (load_panels, calc_metrics, BACKTEST_START,
    COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP)
from run_backtest_a4 import MA10_EXIT_DAYS, MA_EXIT_WINDOW, N_HOLDINGS

def pct(s): return float(str(s).strip('%'))/100

cal = load_meta('trade_calendar')
end = sorted([d for d in cal['trade_date'] if d <= '2026-12-31'])[-1]
calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
c800 = load_meta('csi800')
codes = sorted(c800['code'])
print('Loading data...')
panel, ap = load_panels(codes, BACKTEST_START, end)
si = load_meta('stock_info_full'); si = None if si.empty else si
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None
d = _make_rebal_dates(calendar, 'biweekly')

all_dates = panel.index
rebal_set = set(str(dd.date()) if hasattr(dd, "date") else dd for dd in d)

# ── 方案C: Top 50 by turnover, equal weight ──
def run_top50():
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}
    days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0; pos_ratio = 1.0
    TOP_N = 50

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # Step 1: 当日收益(旧持仓)
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i - 1].get(code)
                cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp / pp - 1)
            port_rets.iloc[i] += ret

        # Step 2: MA10 exit
        if cur_weights and i >= 10:
            exits = []
            for code in list(cur_weights.keys()):
                col = panel[code] if code in panel.columns else None
                if col is None: continue
                hist = col.iloc[max(0, i - MA_EXIT_WINDOW + 1): i + 1].dropna()
                if len(hist) < max(5, MA_EXIT_WINDOW // 2): continue
                ma10 = hist.mean()
                cur_p = panel.iloc[i].get(code)
                if pd.isna(cur_p) or cur_p <= 0: continue
                if cur_p < ma10:
                    days_below_ma10[code] = days_below_ma10.get(code, 0) + 1
                else:
                    days_below_ma10[code] = 0
                if days_below_ma10.get(code, 0) >= MA10_EXIT_DAYS:
                    exits.append(code)
            for code in exits:
                w = cur_weights.pop(code, 0)
                port_rets.iloc[i] -= w * COMMISSION
                entry_prices.pop(code, None)
                days_below_ma10.pop(code, None)

        # Step 3: 调仓 - 选Top50 by recent turnover
        if date_str in rebal_set and i >= MIN_BARS:
            pos_ratio = get_position_ratio(idx_c, date) if idx_c is not None else 1.0

            if pos_ratio <= 0.30:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav
            else:
                # 按近20日平均成交额排名，取Top50
                if ap is not None and i >= 20:
                    avg_amount = ap.iloc[max(0,i-20):i].mean().dropna()
                    top_codes = avg_amount.nlargest(TOP_N).index.tolist()
                else:
                    # fallback: use available columns
                    top_codes = [c for c in panel.columns if c in set(c800['code'])][:TOP_N]

                # equal weight
                old_set = set(cur_weights.keys())
                new_set = set(top_codes)
                new_w = {c: pos_ratio / TOP_N for c in top_codes}

                # commission
                enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w) / 2 * COMMISSION * 2

                # update entry prices
                cur_p_series = panel.ffill().iloc[i]
                for c in new_set - old_set:
                    ep = cur_p_series.get(c)
                    if ep and not pd.isna(ep): entry_prices[c] = float(ep)
                for c in old_set - new_set:
                    entry_prices.pop(c, None)
                    days_below_ma10.pop(c, None)

                if not cur_weights: entry_hwm = cumul_nav
                cur_weights = new_w
                nav_since = 1.0

        # Step 4: 组合止损
        if cur_weights and i > 0:
            if nav_since <= (1 + PERIOD_STOP) or (cumul_nav / entry_hwm - 1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav

        # Step 5: 现金计息
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD / 252
        nav_since *= (1 + port_rets.iloc[i])
        cumul_nav *= (1 + port_rets.iloc[i])

    return (1 + port_rets).cumprod()

# ── 运行 ──
print('Running A-4 baseline...')
from run_backtest_a4 import run_backtest_a4
nav_a4 = run_backtest_a4(panel, d, ap, idx_c, si)
m_a4 = calc_metrics(nav_a4)
print('A-4: %s  Sharpe %s  DD %s' % (m_a4['年化收益率'], m_a4['夏普比率'], m_a4['最大回撤']), flush=True)

print('Running Plan C (Top50)...')
nav_c = run_top50()
m_c = calc_metrics(nav_c)
print('PlanC: %s  Sharpe %s  DD %s' % (m_c['年化收益率'], m_c['夏普比率'], m_c['最大回撤']), flush=True)

# CSI800 baseline
c800_cols = [c for c in panel.columns if c in set(c800['code'])]
nav_ew = (1 + panel[c800_cols].pct_change(fill_method=None).mean(axis=1).fillna(0)).cumprod()
m_ew = calc_metrics(nav_ew)
print('CSI800等权: %s  Sharpe %s  DD %s' % (m_ew['年化收益率'], m_ew['夏普比率'], m_ew['最大回撤']), flush=True)

# ── 分年 ──
print()
print('分年度对比:')
print('  %-6s  %8s  %8s  %8s' % ('年份','A-4','PlanC','CSI800'))
for yr in range(2020, 2027):
    a4r = nav_a4[nav_a4.index.year==yr]
    c4r = nav_c[nav_c.index.year==yr]
    ewr = nav_ew[nav_ew.index.year==yr]
    if len(a4r)<2: continue
    a4ret = a4r.iloc[-1]/a4r.iloc[0]-1
    cret  = c4r.iloc[-1]/c4r.iloc[0]-1
    ewret = ewr.iloc[-1]/ewr.iloc[0]-1
    print(f'  {yr}    {a4ret:>+7.1%}  {cret:>+7.1%}  {ewret:>+7.1%}')

print()
print('PlanC vs A-4: ann=%.1f%% vs %.1f%%  delta=%+.1f%%' % (
    pct(m_c['年化收益率'])*100, pct(m_a4['年化收益率'])*100,
    (pct(m_c['年化收益率'])-pct(m_a4['年化收益率']))*100))
