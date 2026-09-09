"""
小盘低波动 — 实盘前终检
  配置: 季频 N=40 缓冲40% + 轻保护(CSI800破MA200+VIX飙升→半仓)

Part A: Walk-Forward 滚动训练-测试 (3年训练→1年测试)
Part B: 当前持仓清单 + 流动性检查 (实盘执行清单)
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
FINAL_N_HOLD = 40
FINAL_BUFFER = 13       # N=40 buffer → cutoff at 53 (≈前40%概念)
FINAL_FREQ = 'quarterly'
CAPITAL = 100_000        # 10万实盘资金
MAX_SINGLE_PCT = 0.05    # 单日成交占比上限5%

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
print('═' * 90)
print('小盘低波动 实盘前终检 — 季频N=40缓冲+轻保护')
print(f'  配置: 季频调仓 | {FINAL_N_HOLD}只等权 | 缓冲(前{FINAL_N_HOLD+FINAL_BUFFER}保留) | 轻保护')
print(f'  资金: {CAPITAL/10000:.0f}万 | 成本: 0.3%单边')
print('═' * 90)

# ── Load data ──
print('\n>>> Loading data...')
sc_universe = pd.read_parquet('data_store/meta/smallcap_universe_bs.parquet')

# Build point-in-time pool map: for each snapshot date, the valid codes
pool_snapshots = []
for _, row in sc_universe.iterrows():
    pool_snapshots.append((row['date'], set(row['codes'].split(','))))
pool_snapshots.sort(key=lambda x: x[0])  # sort by date
print(f'  Pool: {len(pool_snapshots)} snapshots, {len(pool_snapshots[-1][1])} codes/latest')

def get_pool_codes(date_str):
    """Return valid codes set for a given date (most recent snapshot ≤ date)."""
    for snap_date, codes in reversed(pool_snapshots):
        if snap_date <= date_str:
            return codes
    return pool_snapshots[0][1]  # fallback: first snapshot

# Still load ALL baostock stocks for price panel (warm-up needs broad data),
# but POOL FILTER will be applied at selection time.
all_codes = set()
for _, codes in pool_snapshots:
    all_codes.update(codes)
all_codes = sorted(all_codes)
pool_size_latest = len(pool_snapshots[-1][1])
print(f'  Load universe: {len(all_codes)} unique codes (union of all snapshots)')
print(f'  Current pool: {pool_size_latest} codes (will be enforced at selection)')

cal = load_meta('trade_calendar')
cal_dates = sorted(cal['trade_date'].tolist())

# Find usable date range
end_data = sorted([d for d in cal_dates if d <= '2025-12-31'])[-1]
# Also check if there's data beyond 2025
all_cal_dates = sorted(cal_dates)
print(f'  日历范围: {all_cal_dates[0]} → {all_cal_dates[-1]}')

calendar = [d for d in cal_dates if BACKTEST_START <= d <= end_data]
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None

prices, amounts, names = {}, {}, {}
for code in all_codes:
    fpath = BAOSTOCK_DIR / f'{code}.parquet'
    if not fpath.exists(): continue
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date']); df = df.set_index('date').sort_index()
    df = df[(df.index >= BACKTEST_START) & (df.index <= end_data)]
    if len(df) < MIN_BARS: continue
    cs = pd.to_numeric(df['close'], errors='coerce').replace(0, np.nan).dropna()
    am = pd.to_numeric(df['amount'], errors='coerce').replace(0, np.nan).dropna()
    if len(cs) >= MIN_BARS:
        prices[code] = cs; amounts[code] = am
        if 'name' in df.columns:
            nm = df['name'].dropna().iloc[-1] if not df['name'].dropna().empty else code
            names[code] = str(nm)

panel = pd.DataFrame(prices).sort_index()
ap_full = pd.DataFrame(amounts).sort_index()
all_dates = panel.index
n_stocks = panel.shape[1]
print(f'  Panel: {panel.shape[0]}d × {n_stocks} stocks ({all_dates[0].date()} → {all_dates[-1].date()})')

# ── CSI800 volatility ──
idx_vol = pd.Series(np.nan, index=idx_c.index) if idx_c is not None else pd.Series()
if idx_c is not None:
    idx_ret = idx_c.pct_change().dropna()
    idx_vol = idx_ret.rolling(20).std() * np.sqrt(252)

def is_vix_spike(date):
    if idx_vol.empty: return False
    date_ts = pd.Timestamp(date)
    if date_ts not in idx_vol.index: return False
    cur_vol = idx_vol.get(date_ts, np.nan)
    if pd.isna(cur_vol) or cur_vol <= 0: return False
    lookback = idx_vol.loc[:date_ts].iloc[-252:]
    if len(lookback) < 60: return False
    median_vol = lookback.median()
    return cur_vol > max(median_vol * 1.5, 0.30)

# ── Selection ──
def _make_quarterly_dates(calendar):
    dates = pd.DatetimeIndex(sorted(calendar))
    result = []
    for yr in range(dates[0].year, dates[-1].year + 1):
        for mo in [3, 6, 9, 12]:
            md = dates[(dates.year == yr) & (dates.month == mo)]
            if len(md):
                result.append(str(md[-1].date()))
    return sorted(set(result))

def select_low_vol_buffered(date, i, n_top=FINAL_N_HOLD, cur_hold=None, buffer_rank=FINAL_BUFFER):
    """Low-vol selection FILTERED to current pool snapshot (point-in-time)."""
    if i < 65:
        return select_fallback(date, i, n_top)
    rets = panel.iloc[i-60:i].pct_change(fill_method=None).std()
    lv = rets.dropna()

    # POINT-IN-TIME FILTER: only consider stocks in current pool
    pool = get_pool_codes(str(date.date()))
    lv = lv[lv.index.isin(pool)]
    if lv.empty:
        return select_fallback(date, i, n_top)

    sorted_codes = lv.nsmallest(len(lv)).index.tolist()
    if cur_hold and buffer_rank > 0:
        cutoff = n_top + buffer_rank
        held_keeping = [c for c in cur_hold if c in sorted_codes[:cutoff]]
        remaining = [c for c in sorted_codes if c not in set(held_keeping)]
        selected = held_keeping + remaining[:n_top - len(held_keeping)]
        return selected[:n_top]
    return sorted_codes[:n_top]

def select_fallback(date, i, n_top):
    """Fallback: top by turnover, filtered to current pool."""
    pool = get_pool_codes(str(date.date()))
    if ap_full is not None and i >= 20:
        avg = ap_full.iloc[max(0,i-20):i].mean().dropna()
        avail = [c for c in avg.index if c in panel.columns and c in pool]
        if len(avail) >= n_top:
            return avg[avail].nlargest(n_top).index.tolist()
    return sorted([c for c in panel.columns if c in pool])[:n_top]

# ── Backtest (returns NAV + metrics) ──
def run_backtest_on_period(dates_subset, rebal_dates, start_idx_offset=0):
    """Run backtest on a specific date subset. Returns (nav, turnover, n_rebals)"""
    rebal_set = set(rebal_dates)
    port_rets = pd.Series(0.0, index=dates_subset)
    cur_weights = {}; entry_prices = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0
    turnover_sum = 0.0; n_rebals = 0

    for i, date in enumerate(dates_subset):
        abs_i = i + start_idx_offset
        date_str = str(date.date())

        # mark-to-market
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[abs_i-1].get(code)
                cp = panel.iloc[abs_i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp/pp - 1)
            port_rets.iloc[i] += ret

        # rebalance
        if date_str in rebal_set and abs_i >= MIN_BARS:
            below_ma200 = get_position_ratio(idx_c, date) < 1.0 if idx_c is not None else False
            vix = is_vix_spike(date)
            if below_ma200 and vix:
                pos_ratio = 0.5
            else:
                pos_ratio = 1.0

            if pos_ratio <= 0:
                cur_weights = {}; entry_prices = {}
                nav_since = 1.0; entry_hwm = cumul_nav
            else:
                cur_hold = set(cur_weights.keys())
                top_codes = select_low_vol_buffered(date, abs_i, cur_hold=cur_hold)
                n = min(len(top_codes), FINAL_N_HOLD)
                old_set = set(cur_weights.keys())
                new_set = set(top_codes[:n])
                new_w = {c: pos_ratio/n for c in top_codes[:n]}

                enter_w = sum(new_w.get(c,0) for c in new_set - old_set)
                exit_w = sum(cur_weights.get(c,0) for c in old_set - new_set)
                port_rets.iloc[i] -= (enter_w + exit_w)/2 * REAL_COST * 2

                turnover_sum += (enter_w + exit_w) / 2
                n_rebals += 1

                cp_s = panel.ffill().iloc[abs_i]
                for c in new_set - old_set:
                    ep = cp_s.get(c)
                    if ep and not pd.isna(ep): entry_prices[c] = float(ep)
                for c in old_set - new_set:
                    entry_prices.pop(c, None)
                if not cur_weights: entry_hwm = cumul_nav
                cur_weights = new_w
                nav_since = 1.0

        # portfolio stops
        if cur_weights and i > 0:
            if nav_since <= (1+PERIOD_STOP) or (cumul_nav/entry_hwm-1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}
                nav_since = 1.0; entry_hwm = cumul_nav

        # cash
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD/252
        nav_since *= (1+port_rets.iloc[i])
        cumul_nav *= (1+port_rets.iloc[i])

    n_days = len(dates_subset); n_years = n_days / 252
    rebal_per_year = n_rebals / n_years if n_years > 0 else 0
    turnover_annual = (turnover_sum / n_rebals * rebal_per_year) if n_rebals > 0 else 0
    return (1+port_rets).cumprod(), turnover_annual, n_rebals

def metric(nav):
    m = calc_metrics(nav)
    return {'ar': pct(m['年化收益率']), 'sr': float(m['夏普比率']),
            'dd': pct(m['最大回撤']), 'vol': pct(m['年化波动率'])}

def yearly_from_nav(nav):
    annual = nav.resample('YE').last().pct_change().dropna()
    return {d.year: float(v) for d, v in annual.items()}

# ══════════════════════════════════════════════════════════════════════
# PART A: WALK-FORWARD + YEARLY ROBUSTNESS
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 90)
print('Part A: Walk-Forward 稳健性检验 + 逐年表现')
print('  方法: 3年预热建立组合 → 1年OOS, 滚动向前 (组合状态连续)')
print('  配置: 季频 N=40 缓冲+轻保护 | 0.3%成本')
print('=' * 90)

quarterly_all = _make_quarterly_dates(calendar)
years_in_sample = sorted(set(d.year for d in all_dates if d.year >= 2019))

# Walk-forward with continuous portfolio state:
# Run on train_start → test_end, extract only test-year returns.
# This is the correct approach for rules-based strategies: the training
# period establishes the portfolio (holdings, entry prices, HWM) which
# carries continuously into the test period.
wf_windows = []
test_years = [y for y in years_in_sample if y >= 2022]

for test_yr in test_years:
    train_start = f'{test_yr-3}-01-01'
    test_end = f'{test_yr}-12-31'

    # Run full period: training + test, then slice test year
    period_dates = all_dates[(all_dates >= train_start) & (all_dates <= test_end)]
    test_dates_slice = all_dates[(all_dates >= f'{test_yr}-01-01') & (all_dates <= test_end)]

    if len(period_dates) < 500 or len(test_dates_slice) < 200:
        continue

    start_idx = list(all_dates).index(period_dates[0])
    period_rebals = [d for d in quarterly_all if train_start <= d <= test_end]

    print(f'\n  ── WF: 预热{train_start[:4]}-{test_yr-1} → OOS {test_yr} ──')
    nav_period, to_period, nr_period = run_backtest_on_period(
        period_dates, period_rebals, start_idx_offset=start_idx)

    # Slice test year from full-period NAV
    nav_test = nav_period[nav_period.index >= test_dates_slice[0]]
    # Rebase to 1.0 at test start
    nav_test = nav_test / nav_test.iloc[0]

    m_test = metric(nav_test)
    cd_test = to_period * REAL_COST * 2
    net_test = m_test['ar'] - cd_test

    # Rolling 12-month Sharpe (monthly returns within test year)
    monthly = nav_test.resample('ME').last().pct_change().dropna()
    roll_sr = (monthly.mean() / monthly.std() * np.sqrt(12)) if len(monthly) >= 3 and monthly.std() > 0 else 0

    print(f'    OOS净年化: {net_test*100:+.1f}%  |  OOS夏普: {m_test["sr"]:.2f}  |  月频Sharpe: {roll_sr:.2f}')
    print(f'    OOS回撤: {m_test["dd"]*100:+.1f}%  |  换手: {to_period:.0%}  |  调仓: {nr_period}次')

    wf_windows.append({
        'test_year': test_yr,
        'train_period': f'{train_start[:4]}-{test_yr-1}',
        'net_ar': net_test,
        'sr': m_test['sr'],
        'roll_sr': roll_sr,
        'dd': m_test['dd'],
        'turnover': to_period,
    })

# Summary table
print('\n  ── Walk-Forward 汇总 (3年预热→1年OOS, 组合连续) ──')
print(f'  {"OOS年":<8} {"预热窗口":<12} {"OOS净年化":>10} {"OOS夏普":>8} {"月频SR":>8} {"OOS回撤":>8} {"换手":>8}')
print('  ' + '-' * 72)
all_net, all_sr = [], []
for w in wf_windows:
    all_net.append(w['net_ar']); all_sr.append(w['sr'])
    print(f'  {w["test_year"]:<8} {w["train_period"]:<12} {w["net_ar"]*100:+9.1f}% {w["sr"]:7.2f} {w["roll_sr"]:7.2f} {w["dd"]*100:+7.1f}% {w["turnover"]:7.0%}')

avg_net = np.mean(all_net) if all_net else 0
avg_sr = np.mean(all_sr) if all_sr else 0
print(f'  {"均值":<8} {"":<12} {avg_net*100:+9.1f}% {avg_sr:7.2f}')
print(f'  OOS胜率(净>0): {sum(1 for n in all_net if n > 0)}/{len(all_net)}')
print(f'  OOS稳定性(净年化标准差): {np.std(all_net)*100:.1f}%')

# ── Full-sample yearly breakdown ──
print('\n  ── 全样本逐年表现 (连续运作) ──')
quarterly_full = _make_quarterly_dates(calendar)
nav_full, to_full, nr_full = run_backtest_on_period(all_dates, quarterly_full, start_idx_offset=0)
m_full = metric(nav_full)
cd_full = to_full * REAL_COST * 2
net_full = m_full['ar'] - cd_full

yb = yearly_from_nav(nav_full)
print(f'  {"年份":<8} {"净收益":>10} {"图示":>40}')
print('  ' + '-' * 58)
for yr in sorted(yb.keys()):
    bar = '█' * max(0, min(40, int(abs(yb[yr])*200)))
    print(f'  {yr:<8} {yb[yr]*100:+9.1f}%  {bar}')
print(f'  {"全期":<8} {net_full*100:+9.1f}%  夏普{m_full["sr"]:.2f}  回撤{m_full["dd"]*100:+.1f}%  换手{to_full:.0%}')

# Rolling 24-month Sharpe (forward-looking: at each month, Sharpe over next 24 months)
monthly_nav = nav_full.resample('ME').last().dropna()
if len(monthly_nav) >= 26:
    roll_24m_ret = monthly_nav.pct_change(24).dropna()  # 24-month total return
    # Approximate: annualized from 24-month return
    roll_24m_ar = (1 + roll_24m_ret) ** (1/2) - 1
    print(f'\n  滚动24月年化收益:')
    print(f'    范围: {roll_24m_ar.min()*100:+.1f}% ~ {roll_24m_ar.max()*100:+.1f}%')
    print(f'    均值: {roll_24m_ar.mean()*100:+.1f}%  中位: {roll_24m_ar.median()*100:+.1f}%')
    neg_24m = (roll_24m_ar < 0).sum()
    total_24m = len(roll_24m_ar)
    print(f'    负值占比: {neg_24m}/{total_24m} ({neg_24m/total_24m*100:.0f}%)')

# CSI800 benchmark comparison
if idx_c is not None:
    idx_aligned = idx_c[idx_c.index.isin(all_dates)]
    idx_nav = idx_aligned / idx_aligned.iloc[0]
    idx_m = metric(idx_nav)
    idx_yb = yearly_from_nav(idx_nav)
    print(f'\n  {"CSI800基准":<8} {idx_m["ar"]*100:+9.1f}%  夏普{idx_m["sr"]:.2f}  回撤{idx_m["dd"]*100:+.1f}%')
    print(f'  逐年对比:')
    print(f'  {"年份":<8} {"策略":>10} {"CSI800":>10} {"超额":>10}')
    print('  ' + '-' * 42)
    for yr in sorted(yb.keys()):
        strat_ret = yb.get(yr, 0)
        idx_ret = idx_yb.get(yr, 0)
        excess = strat_ret - idx_ret
        print(f'  {yr:<8} {strat_ret*100:+9.1f}% {idx_ret*100:+9.1f}% {excess*100:+9.1f}pp')

# ══════════════════════════════════════════════════════════════════════
# PART B: CURRENT HOLDINGS + EXECUTION CHECKLIST
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 90)
print('Part B: 当前持仓清单 + 实盘执行检查')
print(f'  生成日期: {all_dates[-1].date()} (最新可用数据)')
print(f'  资金量: ¥{CAPITAL:,} ({CAPITAL/10000:.0f}万)')
print('=' * 90)

# Find latest rebalance date and next rebalance
latest_date = all_dates[-1]
latest_date_str = str(latest_date.date())

# Which quarter are we in?
# Data ends 2025-12-31. Next rebalance would be end of Mar 2026.
# But wait - the user said today is 2026-06-25. The data ends at 2025.
# We need to show the holdings as of the LAST rebalance date in the data,
# and note the NEXT rebalance date based on the real calendar.

# For the current holdings, use the most recent quarterly rebalance in the data
quarterly_dates_in_data = [d for d in quarterly_all if d <= latest_date_str]
last_rebal = quarterly_dates_in_data[-1] if quarterly_dates_in_data else None
print(f'  最近调仓日(数据内): {last_rebal}')

# Now, for the actual live execution:
# Today is 2026-06-25, next quarterly rebalance = 2026-06-30 (last trading day of June)
# But we don't have live data. We'll show what WOULD be held based on last available data
# and note that the actual next rebalance is June 30, 2026.

# Find the index position for the latest rebalance date
if last_rebal:
    last_rebal_ts = pd.Timestamp(last_rebal)
    last_i = list(all_dates).index(last_rebal_ts) if last_rebal_ts in all_dates else len(all_dates)-1

    # Get holdings as of this date
    holdings = select_low_vol_buffered(last_rebal_ts, last_i, cur_hold=None)
    holdings = holdings[:FINAL_N_HOLD]

    # Determine position ratio based on tail protection at last rebal
    below = get_position_ratio(idx_c, last_rebal_ts) < 1.0 if idx_c is not None else False
    vix_flag = is_vix_spike(last_rebal_ts)
    pos = 0.5 if (below and vix_flag) else 1.0
    single_wt = pos / len(holdings) if holdings else 0
    single_amount = CAPITAL * single_wt
    protect_note = '⚠️ 半仓(破MA200+VIX飙升)' if pos < 1.0 else '满仓'

    # Get names
    info_df = load_meta('stock_info_full')
    name_map = {}
    if not info_df.empty:
        info_df['code'] = info_df['code'].astype(str).str.zfill(6)
        for _, r in info_df.iterrows():
            name_map[r['code']] = r.get('stock_name', r.get('name', ''))

    # Get 20-day average turnover for each holding
    print(f'\n  ── 持仓清单 ({len(holdings)}只) ──')
    print(f'  仓位状态: {protect_note}')
    print(f'  单只权重: {single_wt*100:.1f}% | 单只金额: ¥{single_amount:,.0f}')
    print()
    print(f'  {"代码":<8} {"名称":<10} {"权重":>6} {"金额":>8} {"近20日均成交(万)":>16} {"成交占比":>10} {"状态":>6}')
    print('  ' + '-' * 78)

    total_liq_ok = 0
    for code in holdings:
        name = name_map.get(code, names.get(code, code))
        # 20-day average amount
        amt_series = ap_full[code].dropna() if code in ap_full.columns else pd.Series()
        if len(amt_series) >= 20:
            avg_amt_20d = float(amt_series.iloc[-20:].mean())
        else:
            avg_amt_20d = float(amt_series.mean()) if len(amt_series) > 0 else 0
        avg_amt_wan = avg_amt_20d / 10000  # convert to 万

        # Single order as % of daily turnover
        if avg_amt_20d > 0:
            trade_ratio = single_amount / avg_amt_20d
        else:
            trade_ratio = 999

        liq_ok = '✓' if trade_ratio < MAX_SINGLE_PCT else ('⚠️' if trade_ratio < 0.10 else '✗')
        if trade_ratio < MAX_SINGLE_PCT:
            total_liq_ok += 1

        print(f'  {code:<8} {name:<10} {single_wt*100:5.1f}% ¥{single_amount:>7,.0f} {avg_amt_wan:>14.0f}万 {trade_ratio*100:>9.2f}% {liq_ok:>6}')

    print(f'\n  流动性: {total_liq_ok}/{len(holdings)} 只通过(单笔<{MAX_SINGLE_PCT*100:.0f}%日成交)')

    # Summary stats
    amts = []
    for code in holdings:
        amt_series = ap_full[code].dropna() if code in ap_full.columns else pd.Series()
        if len(amt_series) >= 20:
            amts.append(float(amt_series.iloc[-20:].mean()) / 10000)
    if amts:
        print(f'  近20日均成交: 最小{min(amts):.0f}万 / 中位{np.median(amts):.0f}万 / 最大{max(amts):.0f}万')
        print(f'  单笔¥{single_amount:,.0f}占中位成交的{single_amount/(np.median(amts)*10000)*100:.3f}%')

# ── Execution timeline ──
print('\n  ── 调仓时间线 ──')
print(f'  上一个调仓日(数据内): {last_rebal}')
# Find next quarterly rebalance
# The calendar might have 2026 dates
calendar_2026 = [d for d in all_cal_dates if d >= '2026-01-01']
if calendar_2026:
    q_2026 = []
    for mo in [3, 6, 9, 12]:
        md = [d for d in calendar_2026 if d[5:7] == f'{mo:02d}']
        if md:
            q_2026.append(md[-1])
    if q_2026:
        print(f'  2026年季频调仓日: {", ".join(q_2026)}')
        # Find next one after today (2026-06-25)
        today_str = '2026-06-25'
        next_rebals = [d for d in q_2026 if d >= today_str]
        if next_rebals:
            print(f'  >>> 下一个调仓日: {next_rebals[0]} (距今{(pd.Timestamp(next_rebals[0]) - pd.Timestamp(today_str)).days}天)')
    else:
        print(f'  2026年: 日历中暂无完整季度数据')
else:
    print(f'  日历止于{all_cal_dates[-1]}, 2026年数据待更新')
    print(f'  预计下次调仓: 2026-06-30 (季末最后一个交易日)')

# ── Real-time caveat ──
print(f'\n  ── 注意事项 ──')
print(f'  1. 以上清单基于最后可用数据({latest_date_str})计算, 非实时行情')
print(f'  2. 实盘前需用当日数据重新跑选股, 确认名单未因股价异动而大幅变化')
print(f'  3. 调仓日检查: 排除停牌、ST、涨跌停、无效代码')
print(f'  4. 当日未成交部分: T+1限价单补足, 避免追高')
print(f'  5. 空仓期间资金: 放入华宝添益(511880)货基')
print(f'  6. 前3次调仓: 记录真实滑点, 用于修正成本假设')

print()
print('═' * 90)
print('终检完毕。')
print('═' * 90)
