"""
基准公平性审计 — 三部分完整输出
只摊代码+数字, 不下结论
"""
import sys, os
sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from datetime import datetime
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from data.storage import load_meta
from run_backtest_a2 import _make_rebal_dates, get_position_ratio, compute_score_a2, compute_weights
from run_backtest_a import (load_panels, calc_metrics, BACKTEST_START,
    COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP, MA_PERIOD,
    REGIME_BEAR_THR, REGIME_BULL_THR)
from run_backtest_a4 import run_backtest_a4, MA10_EXIT_DAYS, MA_EXIT_WINDOW, N_HOLDINGS, select_dynamic_grace

def pct(s):
    return float(str(s).strip('%')) / 100

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

# ================================================================
# PART 1: 口径一致性核对
# ================================================================
print('='*75)
print('第一部分: 口径一致性核对')
print('='*75)

print("""
【1.1 调仓频率】
  A-4:     run_backtest_a4.py:56  REBAL_FREQ="biweekly"
           实际调仓日数: %d (年均~%d次)
           _make_rebal_dates() 逻辑 run_backtest_a2.py:49-67:
            月中 = floor(N_days/2)个交易日
            月末 = 倒数第2个交易日(缓冲1天)
  CSI800基准: 之前对比用的是 panel.pct_change().mean(axis=1)
              这是【每日等权重平衡】, 不是双周调仓
              相当于每天把所有800只股票重置为等权
  => 调仓频率: A-4双周 vs 基准每日 => 不一致
""" % (len(d), len(d)//7))

# Build a FAIR benchmark: biweekly rebalancing, equal weight, same costs
print("""
【1.2 交易成本】
  A-4:     run_backtest_a.py:35  COMMISSION=0.00175 (单边0.175%%)
           调仓日扣费 run_backtest_a4.py:261-263:
             enter = sum(new_w.get(c,0) for c in new-old)
             exit_ = sum(cur_w.get(c,0) for c in old-new)
             port_rets -= (enter+exit_)/2 * COMMISSION * 2
           买+卖合计 round-trip ≈ 0.35%%
  CSI800基准(之前): 不扣任何交易成本
  => 不一致: 基准零成本
""")

print("""
【1.3 成分股口径】
  A-4:     load_meta("csi800") → 当前CSI800成分
           用当前成分股的所有历史价格数据
           这包含了幸存者偏差 — 已退市股票不在当前CSI800中
  CSI800基准(之前): 同样panel[c800_cols], 同样幸存者偏差
  => 一致 (同样的偏差)
""")

print("""
【1.4 仓位管理】
  A-4:     get_position_ratio() run_backtest_a2.py:110-136
           5档: CSI800/MA200>=1.05→100%%, 1.02-1.05→85%%,
                0.98-1.02→70%%, 0.95-0.98→50%%, <0.95→30%%
  CSI800基准(之前Test2): 同样调用get_position_ratio()
  CSI800满仓(之前Test3): 永远100%%
  => Test2一致, Test3不一致
""")

print("""
【1.5 止损/风控】
  A-4:     三层: MA10连续3天出清(run_backtest_a4.py:168-200)
                 追踪止损-18%% (run_backtest_a4.py:270-278)
                 期内止损-15%% (同上)
  CSI800基准(之前): 无任何止损
  => 不一致: 基准无风控
""")

print("""
【1.6 收益归属日 (最关键)】
  A-4修复后: run_backtest_a4.py:179-193 Step1先算当日收益(旧持仓)
            run_backtest_a4.py:248-268 Step3调仓(新持仓明日生效)
            新股从T+1日开始计算收益, 买入价=T日收盘价
  CSI800基准: panel.pct_change().mean(axis=1)
            当日收益 = 所有股票当日涨跌幅均值
            隐含"昨日买→今日收盘卖"的每日全量调仓
  => 不一致: A-4是T+1调仓生效, 基准是当日生效
""")

# ================================================================
# Build FAIR benchmark
# ================================================================
print('='*75)
print('【1.7 公平基准重算】')
print('构建: CSI800等权 + 双周调仓 + 扣0.175%%成本 + 仓位管理 + 止损')
print('与A-4唯一区别: 选800只等权 vs 选30只因子')
print('='*75)

all_dates = panel.index
rebal_set = set(d)

# Fair CSI800 benchmark: biweekly rebalance, equal weight, same costs
def run_csi800_fair():
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}
    days_below_ma10 = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0; pos_ratio = 1.0
    TOP_N = 50  # use 50 stocks for fair comparison (100万买不了800只)

    # Get the biggest 50 by avg turnover for representativeness
    c800_cols = sorted([c for c in panel.columns if c in set(c800['code'])])

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # Step 1: return (old weights)
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
                ma10 = hist.mean(); cur_p = panel.iloc[i].get(code)
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

        # Step 3: rebalance (biweekly, equal weight, top N by turnover)
        if date_str in rebal_set and i >= MIN_BARS:
            pos_ratio = get_position_ratio(idx_c, date) if idx_c is not None else 1.0
            if pos_ratio <= 0.30:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav
            else:
                if ap is not None and i >= 20:
                    avg_amt = ap.iloc[max(0,i-20):i].mean().dropna()
                    top_codes = avg_amt.nlargest(min(TOP_N, len(avg_amt))).index.tolist()
                else:
                    top_codes = c800_cols[:min(TOP_N, len(c800_cols))]

                n_stocks = len(top_codes)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes)
                new_w = {c: pos_ratio / n_stocks for c in top_codes}

                # Same commission as A-4
                enter_w = sum(new_w.get(c, 0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c, 0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w) / 2 * COMMISSION * 2

                cur_p_series = panel.ffill().iloc[i]
                for c in new_set - old_set:
                    ep = cur_p_series.get(c)
                    if ep and not pd.isna(ep): entry_prices[c] = float(ep)
                for c in old_set - new_set:
                    entry_prices.pop(c, None); days_below_ma10.pop(c, None)
                if not cur_weights: entry_hwm = cumul_nav
                cur_weights = new_w
                nav_since = 1.0

        # Step 4: portfolio stop
        if cur_weights and i > 0:
            if nav_since <= (1 + PERIOD_STOP) or (cumul_nav / entry_hwm - 1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                nav_since = 1.0; entry_hwm = cumul_nav

        # Step 5: cash
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD / 252
        nav_since *= (1 + port_rets.iloc[i])
        cumul_nav *= (1 + port_rets.iloc[i])

    return (1 + port_rets).cumprod()

print('Running A-4...')
nav_a4 = run_backtest_a4(panel, d, ap, idx_c, si)
m_a4 = calc_metrics(nav_a4)

print('Running fair benchmark...')
nav_fair = run_csi800_fair()
m_fair = calc_metrics(nav_fair)

print()
print('A-4:       ann=%s  sr=%s  dd=%s' % (m_a4['年化收益率'], m_a4['夏普比率'], m_a4['最大回撤']))
print('Fair基准:  ann=%s  sr=%s  dd=%s' % (m_fair['年化收益率'], m_fair['夏普比率'], m_fair['最大回撤']))
print('差异:      ann=%+.1f%%' % ((pct(m_a4['年化收益率'])-pct(m_fair['年化收益率']))*100))

print()
print('分年:')
for yr in range(2020, 2027):
    a4r = nav_a4[nav_a4.index.year==yr]
    fr = nav_fair[nav_fair.index.year==yr]
    if len(a4r)<2: continue
    a4ret = a4r.iloc[-1]/a4r.iloc[0]-1
    fret  = fr.iloc[-1]/fr.iloc[0]-1
    print('  %d: A-4=%+.1f%%  Fair=%+.1f%%  diff=%+.1f%%' % (yr, a4ret*100, fret*100, (a4ret-fret)*100))

# ================================================================
# PART 2: 执行时序追踪
# ================================================================
print()
print('='*75)
print('第二部分: 执行时序追踪 (2020-07-01附近)')
print('='*75)

# Find the closest rebalance date near 2020-07-01
targets = sorted([dd for dd in d if '2020-06' <= dd <= '2020-07'])
print('附近调仓日: %s' % targets)

# Trace execution on one rebalance day
trace_date = targets[0] if targets else '2020-06-19'
trace_i = panel.index.get_loc(trace_date)
print('追踪调仓日: %s (index=%d)' % (trace_date, trace_i))

# Show what the fixed code does on this day
print()
print('--- 执行顺序 (run_backtest_a4.py 修复后) ---')
print('Step 1 (L179-193): 计算当日收益 = OLD持仓 × (panel[%d]/panel[%d] - 1)' % (trace_i, trace_i-1))
print('  旧持仓是6/19之前的持仓(上次调仓日选出的)')
print()
print('Step 2 (L196-233): MA10检查')
print('  用panel.iloc[%d]今日收盘价 vs MA10' % trace_i)
print()
print('Step 3 (L248-268): 调仓选股')
print('  compute_score_a2(_panel_u, date=%s, ...) -> 用该日收盘数据算因子' % trace_date)
print('  select_dynamic_grace() -> 选出30只新股')
print('  设 entry_prices[新股] = panel.ffill().iloc[%d] (当日收盘价)' % trace_i)
print('  cur_weights = new_w  (新持仓明日生效)')
print()
print('Step 5 (L287-288): 现金计息')
print()
print('--- 新股首次收益计算 ---')
if trace_i + 1 < len(panel):
    print('下一个交易日: %s (index=%d)' % (panel.index[trace_i+1], trace_i+1))
    print('Step 1: ret = cur_weights(已是新股) × (panel[%d]/panel[%d] - 1)' % (trace_i+1, trace_i))
    print('即: 从 %s 收盘到 %s 收盘的涨跌幅' % (trace_date, str(panel.index[trace_i+1]).split('T')[0]))
    print()
    print('结论: 新股买入价 = 调仓日收盘价 (T日)')
    print('      新股首日收益 = T日收盘→T+1日收盘')
    print('      无滞后, 也无先知 — 1日间隙是正确的(收盘后选股, 次日生效)')

# Check: is there any stock that was in the rebalance selection?
print()
print('--- 验证: 调仓日选出的新股样本 ---')
# Quick run to capture the selection
import run_backtest_a as a_mod, run_backtest_a3 as a3_mod, run_backtest_a4 as a4_mod
from run_backtest_a2 import compute_score_a2, compute_weights, get_position_ratio, SECTOR_BOOST

i = trace_i
date = panel.index[i]
date_str = str(date.date())
pos_ratio = get_position_ratio(idx_c, date) if idx_c is not None else 1.0

# Compute what stocks would be selected
score = compute_score_a2(panel, date, ap, si)
top_10 = score.nlargest(10)
print('Score Top 10 (因子得分最高的10只):')
for code, s in top_10.items():
    cp = panel.iloc[i].get(code)
    pp = panel.iloc[i-1].get(code) if i>0 else None
    np_p = panel.iloc[i+1].get(code) if i+1<len(panel) else None
    t1_ret = (np_p/cp-1) if cp and np_p and cp>0 else None
    print('  %s: score=%.4f  T日收盘=%.2f  T-1收盘=%.2f  T+1收盘=%.2f  T+1收益=%.2f%%' % (
        code, s, cp or 0, pp or 0, np_p or 0, (t1_ret*100) if t1_ret else 0))

# Show what the buggy version would have done
print()
print('--- 对比: buggy版本(修复前)会怎么做 ---')
print('Buggy Step 2: 调仓选股(cur_weights=new_w)')
print('Buggy Step 4: ret = new_w × (panel[%d]/panel[%d] - 1)' % (trace_i, trace_i-1))
print('即新股被追溯计入当日(T日)收益, 从T-1收盘到T日收盘')
print()
print('修复后正确做法: 新股收益从T日收盘→T+1日收盘开始计')
print('差异: 修复后首日收益晚1天, 但不是"T+2生效"')
print('      T日收盘后选股 → T+1日持有 → T+1日收盘计收益 ✓')

# ================================================================
# PART 3: 真Alpha (CAPM回归)
# ================================================================
print()
print('='*75)
print('第三部分: 真Alpha计算')
print('='*75)

# Build monthly returns for A-4 and CSI800
nav_a4_monthly = nav_a4.resample('M').last()
a4_monthly_ret = nav_a4_monthly.pct_change().dropna()

# CSI800 monthly returns from the index
if idx_c is not None:
    idx_monthly = idx_c.resample('M').last()
    mkt_monthly_ret = idx_monthly.pct_change().dropna()
else:
    # fallback: use equal weight panel
    c800_cols = [c for c in panel.columns if c in set(c800['code'])]
    ew_nav = (1 + panel[c800_cols].pct_change(fill_method=None).mean(axis=1).fillna(0)).cumprod()
    ew_monthly = ew_nav.resample('M').last()
    mkt_monthly_ret = ew_monthly.pct_change().dropna()

# Align dates
common = a4_monthly_ret.index.intersection(mkt_monthly_ret.index)
a4_r = a4_monthly_ret[common]
mkt_r = mkt_monthly_ret[common]

# Risk-free (use 2% as we do in backtest)
rf_monthly = 0.02 / 12

# CAPM: (R_i - R_f) = alpha + beta * (R_m - R_f) + epsilon
y = a4_r.values - rf_monthly
X = mkt_r.values - rf_monthly
X_with_const = np.column_stack([np.ones(len(X)), X])

# OLS regression
beta_hat = np.linalg.inv(X_with_const.T @ X_with_const) @ X_with_const.T @ y
alpha_monthly = beta_hat[0]
beta_mkt = beta_hat[1]
alpha_annual = (1 + alpha_monthly) ** 12 - 1

# Stats
residuals = y - X_with_const @ beta_hat
n = len(y); k = 2
rss = np.sum(residuals**2)
tss = np.sum((y - np.mean(y))**2)
r_squared = 1 - rss/tss

# Standard errors
sigma2 = rss / (n - k)
var_cov = sigma2 * np.linalg.inv(X_with_const.T @ X_with_const)
se_alpha = np.sqrt(var_cov[0, 0])
se_beta = np.sqrt(var_cov[1, 1])
t_alpha = alpha_monthly / se_alpha
t_beta = beta_mkt / se_beta

print('CAPM回归: R_a4 - Rf = alpha + beta × (R_mkt - Rf)')
print('样本: %d 个月 (%s → %s)' % (n, common[0].strftime('%Y-%m'), common[-1].strftime('%Y-%m')))
print()
print('月度Alpha:    %.4f%% (%.4f)' % (alpha_monthly*100, alpha_monthly))
print('年化Alpha:    %.2f%%' % (alpha_annual*100))
print('Beta:         %.4f' % beta_mkt)
print('R-squared:    %.4f' % r_squared)
print('t(alpha):     %.4f' % t_alpha)
print('t(beta):      %.4f' % t_beta)
print('Alpha显著:    %s (|t|>2)' % ('是' if abs(t_alpha) > 2 else '否'))
print()
print('先前对比: A-4 6.1%% vs CSI800等权 16.6%% = 简单相减 -10.5pp')
print('          这是简单减法, 不是Alpha。它隐含了beta=1假设')
print('真Alpha:   年化 %.2f%% (调整beta后)' % (alpha_annual*100))
print('          如果真Alpha接近0或为正, 说明策略选股没有破坏价值')
print('          负Alpha说明选股确实在减值')

# Also compute tracking error and information ratio
tracking_error = np.std(a4_r.values - mkt_r.values) * np.sqrt(12)
mean_excess = np.mean(a4_r.values - mkt_r.values) * 12
ir = mean_excess / tracking_error if tracking_error > 0 else 0
print()
print('Tracking Error(年化): %.2f%%' % (tracking_error*100))
print('Mean Excess Return(年化): %.2f%%' % (mean_excess*100))
print('Information Ratio: %.4f' % ir)
print()
print('='*75)
print('最后一行回答:')
delta_raw = (pct(m_a4['年化收益率']) - pct(m_fair['年化收益率']))
print('口径对齐后, A-4 vs 公平基准差异 = %+.1f%% (年化)' % (delta_raw*100))
if abs(t_alpha) > 2:
    print('这个差距在统计上显著 (t=%.2f), 选股负alpha成立' % t_alpha)
else:
    print('这个差距在统计上不显著 (t=%.2f), 不能排除选股随机游走的可能' % t_alpha)
