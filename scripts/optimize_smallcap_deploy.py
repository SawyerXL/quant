"""
小盘低波动 — 实盘落地优化 (降换手 + 轻量尾部保护)
两大部分:
  Part 1: 降换手 — freq(双周/月/季) × n_hold(20/30/40) × buffer(严格/缓冲)
  Part 2: 轻量尾部保护 — 纯低波 vs 低波+极端降仓

全部在 0.3% 单边成本下测。只给数字表，不下结论。
目标: 0.3%成本下净年化≥8%, 夏普≥1.0, 换手可控。
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
REAL_COST = 0.003  # 0.3% 单边

def pct(s):
    return float(str(s).strip('%')) / 100

# ══════════════════════════════════════════════════════════════════════
print('═' * 90)
print('小盘低波动 — 实盘落地优化')
print('Part 1: 降换手 (freq×n_hold×buffer) ＋ Part 2: 轻量尾部保护')
print('主口径: 0.3%单边成本 | 数据: baostock含退市股 | 2019-2025')
print('═' * 90)

# ── Load data (once) ──
print('\n>>> Loading data...')
sc_universe = pd.read_parquet('data_store/meta/smallcap_universe_bs.parquet')
# Build point-in-time pool map
pool_snapshots = []
for _, row in sc_universe.iterrows():
    pool_snapshots.append((row['date'], set(row['codes'].split(','))))
pool_snapshots.sort(key=lambda x: x[0])

def get_pool_codes(date_str):
    """Return valid codes set for a given date (most recent snapshot ≤ date)."""
    for snap_date, codes in reversed(pool_snapshots):
        if snap_date <= date_str:
            return codes
    return pool_snapshots[0][1]

all_codes = set()
for _, codes in pool_snapshots:
    all_codes.update(codes)
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
n_stocks = panel.shape[1]
print(f'  Panel: {panel.shape[0]}d × {n_stocks} stocks')

# ── CSI800 volatility (VIX proxy) ──
idx_vol = pd.Series(np.nan, index=idx_c.index) if idx_c is not None else pd.Series()
if idx_c is not None:
    idx_ret = idx_c.pct_change().dropna()
    idx_vol_20d = idx_ret.rolling(20).std() * np.sqrt(252)
    idx_vol = idx_vol_20d

def is_vix_spike(date, vol_series=idx_vol):
    """VIX飙升: 20日年化波动 > 1.5倍过去一年中位数 且 >30%"""
    if vol_series.empty: return False
    date_ts = pd.Timestamp(date)
    if date_ts not in vol_series.index: return False
    cur_vol = vol_series.get(date_ts, np.nan)
    if pd.isna(cur_vol) or cur_vol <= 0: return False
    # 1Y lookback
    lookback = vol_series.loc[:date_ts].iloc[-252:]
    if len(lookback) < 60: return False
    median_vol = lookback.median()
    return cur_vol > max(median_vol * 1.5, 0.30)

# ── Rebalance date generators ──
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

# ── Selection: low-vol with buffer ──
def select_low_vol_buffered(date, i, n_top=30, cur_hold=None, buffer_rank=0,
                            smooth_n=1, rank_history=None):
    """
    Low-vol selection with optional buffer and rank smoothing.

    buffer_rank: if >0, keep held stocks still within top (n_top+buffer_rank).
                 e.g. n_top=30, buffer_rank=10 → keep if still in top 40.
    smooth_n: number of past rankings to average (1 = raw).
    rank_history: mutable list of past rank dicts (for smoothing).

    Returns (selected_codes, updated_rank_history)
    """
    if rank_history is None:
        rank_history = []

    if i < 65:
        return select_fallback(date, i, n_top), rank_history

    rets = panel.iloc[i-60:i].pct_change(fill_method=None).std()
    lv = rets.dropna()

    # POINT-IN-TIME FILTER: only consider stocks in current pool
    pool = get_pool_codes(str(date.date()))
    lv = lv[lv.index.isin(pool)]
    if lv.empty:
        return select_fallback(date, i, n_top), rank_history

    # Current period ranks (1 = lowest vol)
    cur_ranks = {c: r+1 for r, c in enumerate(lv.nsmallest(len(lv)).index)}

    rank_history.append(cur_ranks)
    if len(rank_history) > max(smooth_n, 1):
        rank_history = rank_history[-max(smooth_n, 1):]

    # Smooth ranks
    if smooth_n <= 1 or len(rank_history) <= 1:
        avg_ranks = cur_ranks
    else:
        all_codes_in_hist = set()
        for rh in rank_history:
            all_codes_in_hist.update(rh.keys())
        avg_ranks = {}
        for c in all_codes_in_hist:
            ranks = [rh.get(c, 9999) for rh in rank_history]
            avg_ranks[c] = np.mean(ranks)

    sorted_codes = sorted(avg_ranks, key=lambda c: avg_ranks[c])

    # Buffer logic
    if cur_hold and buffer_rank > 0:
        cutoff = n_top + buffer_rank
        held_keeping = [c for c in cur_hold if c in sorted_codes[:cutoff]]
        remaining = [c for c in sorted_codes if c not in set(held_keeping)]
        selected = held_keeping + remaining[:n_top - len(held_keeping)]
        return selected[:n_top], rank_history
    else:
        return sorted_codes[:n_top], rank_history

def select_fallback(date, i, n_top):
    """Fallback: top by turnover, filtered to current pool."""
    pool = get_pool_codes(str(date.date()))
    if ap is not None and i >= 20:
        avg = ap.iloc[max(0,i-20):i].mean().dropna()
        avail = [c for c in avg.index if c in panel.columns and c in pool]
        if len(avail) >= n_top:
            return avg[avail].nlargest(n_top).index.tolist()
    return sorted([c for c in panel.columns if c in pool])[:n_top]

# ══════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ══════════════════════════════════════════════════════════════════════
def run_backtest(n_hold=30, freq='monthly', buffer_rank=0, smooth_n=1,
                 use_tail_protect=False, commission=REAL_COST):
    """
    Core engine. No MA10, no MA200 progressive timing.
    Optional tail protection: only reduce to 50% when
    (index < MA200) AND (VIX proxy spikes).
    """
    if freq == 'quarterly':
        rebal_dates = _make_quarterly_dates(calendar)
    else:
        rebal_dates = _make_rebal_dates(calendar, freq)
    rebal_set = set(rebal_dates)

    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}
    entry_prices = {}
    cumul_nav = 1.0; entry_hwm = 1.0; nav_since = 1.0
    turnover_sum = 0.0; n_rebals = 0
    rank_history = []

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

        # Step 2: No MA10 exit (G2 baseline doesn't use it)

        # Step 3: rebalance
        if date_str in rebal_set and i >= MIN_BARS:
            # Determine position ratio
            if use_tail_protect and idx_c is not None:
                below_ma200 = get_position_ratio(idx_c, date) < 1.0
                vix = is_vix_spike(date)
                if below_ma200 and vix:
                    pos_ratio = 0.5  # half position in extreme
                else:
                    pos_ratio = 1.0  # full position otherwise
            else:
                pos_ratio = 1.0  # always full (G2)

            if pos_ratio <= 0:
                cur_weights = {}; entry_prices = {}
                nav_since = 1.0; entry_hwm = cumul_nav
                rank_history = []
            else:
                cur_hold = set(cur_weights.keys())
                top_codes, rank_history = select_low_vol_buffered(
                    date, i, n_top=n_hold, cur_hold=cur_hold,
                    buffer_rank=buffer_rank, smooth_n=smooth_n,
                    rank_history=rank_history)
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
                    entry_prices.pop(c, None)
                if not cur_weights: entry_hwm = cumul_nav
                cur_weights = new_w
                nav_since = 1.0

        # Step 4: portfolio stops
        if cur_weights and i > 0:
            if nav_since <= (1+PERIOD_STOP) or (cumul_nav/entry_hwm-1) <= TRAILING_STOP:
                cur_weights = {}; entry_prices = {}
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

def crunch_dd(nav, start='2024-01-02', end='2024-02-29'):
    """Max DD during a specific window (小盘踩踏期)"""
    c = nav[(nav.index >= start) & (nav.index <= end)]
    if len(c) < 2: return 0.0
    peak = c.cummax()
    dd = (c / peak - 1).min()
    return float(dd)

def yearly_breakdown(nav):
    """Annual returns from NAV series"""
    annual = nav.resample('YE').last().pct_change().dropna()
    result = {}
    for d, v in annual.items():
        result[d.year] = float(v)
    return result

# ══════════════════════════════════════════════════════════════════════
# PART 1: TURNOVER REDUCTION GRID
#   freq: biweekly / monthly / quarterly
#   n_hold: 20 / 30 / 40
#   buffer: strict (top-N only) / buffered (keep if top N*1.33)
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 90)
print('Part 1: 降换手优化 — 3×3×2 全因子网格')
print('  策略: 低波动选股(无择时G2) | 成本: 0.3%单边 | 排名平滑: 1期(原始)')
print('=' * 90)

freqs = [
    ('双周', 'biweekly'),
    ('月频', 'monthly'),
    ('季频', 'quarterly'),
]
holdings = [20, 30, 40]
buffer_modes = [
    ('严格', 0),       # strict top-N, no buffer
    ('缓冲40%', None),  # will compute: n_hold → buffer so cutoff = round(n_hold * 40/30)
]

# Precompute buffer values for each n_hold
def buf_for(n, label):
    if label == '严格': return 0
    # "跌出前40%才换出": cutoff at ~33% above N
    # For N=30, buffer=10 → cutoff=40; for N=20, buffer=7 → cutoff=27; N=40, buffer=13 → cutoff=53
    return max(1, round(n * 10 / 30))

grid_results = []
for freq_label, freq in freqs:
    for n_hold in holdings:
        for buf_label, _ in buffer_modes:
            buf = buf_for(n_hold, buf_label)
            label = f'{freq_label} N={n_hold} {buf_label}'
            print(f'  Running: {label}...', end=' ', flush=True)
            nav, to, n_r = run_backtest(
                n_hold=n_hold, freq=freq, buffer_rank=buf, smooth_n=1,
                use_tail_protect=False)
            m = metric(nav)
            cost_drag = to * REAL_COST * 2
            net_ar = m['ar'] - cost_drag
            cdd = crunch_dd(nav)
            yb = yearly_breakdown(nav)
            grid_results.append({
                'label': label, 'freq': freq_label, 'n_hold': n_hold,
                'buf_label': buf_label, 'buf': buf,
                'gross_ar': m['ar'], 'net_ar': net_ar, 'sr': m['sr'],
                'dd': m['dd'], 'vol': m['vol'],
                'turnover': to, 'cost_drag': cost_drag,
                'crash_dd': cdd, 'n_rebals': n_r, 'yearly': yb,
            })
            print(f'net={net_ar*100:+.1f}% sr={m["sr"]:.2f} to={to:.0%}', flush=True)

# Print Part 1 summary
print('\n')
print(f'{"策略 (无择时G2)":<28} {"毛年化":>7} {"成本":>7} {"净年化":>7} {"夏普":>6} {"回撤":>7} {"踩踏DD":>8} {"年换手":>8} {"调仓":>4}')
print('-' * 95)
for r in sorted(grid_results, key=lambda r: (-r['net_ar'], r['turnover'])):
    judge = '✓' if (r['net_ar'] >= 0.08 and r['sr'] >= 1.0) else ''
    print(f'{r["label"]:<28} {r["gross_ar"]*100:+6.1f}% {r["cost_drag"]*100:+6.1f}% {r["net_ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["crash_dd"]:+7.2f}% {r["turnover"]:7.0%} {r["n_rebals"]:>4} {judge}')

# ── Part 1 sub-tables: by frequency ──
for freq_label, freq in freqs:
    print(f'\n  ── {freq_label} 子表 ──')
    print(f'  {"配置":<20} {"净年化":>7} {"夏普":>6} {"回撤":>7} {"踩踏DD":>8} {"年换手":>8}')
    print('  ' + '-' * 65)
    subset = [r for r in grid_results if r['freq'] == freq_label]
    for r in sorted(subset, key=lambda r: -r['net_ar']):
        print(f'  N={r["n_hold"]} {r["buf_label"]:<8} {r["net_ar"]*100:+6.1f}% {r["sr"]:5.2f} {r["dd"]*100:+6.1f}% {r["crash_dd"]:+7.2f}% {r["turnover"]:7.0%}')

# ── Target zone check ──
print(f'\n  >>> 达标/接近目标(净年化≥8% 且 夏普≥1.0):')
met_target = [r for r in grid_results if r['net_ar'] >= 0.08 and r['sr'] >= 1.0]
if met_target:
    for r in sorted(met_target, key=lambda r: -r['net_ar']):
        print(f'      ✓ {r["label"]}: 净{r["net_ar"]*100:+.1f}% 夏普{r["sr"]:.2f} 换手{r["turnover"]:.0%}')
else:
    # Show closest
    close = sorted(grid_results, key=lambda r: -(r['net_ar']/0.08 + r['sr']/1.0))[:3]
    print(f'      (无完全达标, 最接近者:)')
    for r in close:
        print(f'      ~ {r["label"]}: 净{r["net_ar"]*100:+.1f}% 夏普{r["sr"]:.2f} 换手{r["turnover"]:.0%}')

# Candidates for Part 2: best from each freq regime, plus best overall by net_ar+sr
candidates_for_p2 = []
for freq_label, freq in freqs:
    freq_subset = [r for r in grid_results if r['freq'] == freq_label]
    # Best by combined net_ar + Sharpe
    best_f = max(freq_subset, key=lambda r: r['net_ar']/0.08 + r['sr']/1.0)
    candidates_for_p2.append(best_f)
# Also add the absolute best by net_ar (regardless of turnover)
best_net = max(grid_results, key=lambda r: r['net_ar'])
if best_net not in candidates_for_p2:
    candidates_for_p2.append(best_net)

print(f'\n  >>> Part 2 候选 (各频率最优 + 净年化最高):')
for c in candidates_for_p2:
    print(f'      {c["label"]}: 净{c["net_ar"]*100:+.1f}% 夏普{c["sr"]:.2f} 换手{c["turnover"]:.0%}')

print()
print('目标: 0.3%成本下净年化≥8% 且 夏普≥1.0')
print('═' * 90)

# ══════════════════════════════════════════════════════════════════════
# PART 2: LIGHTWEIGHT TAIL PROTECTION
#   Test on multiple candidate configs from Part 1.
# ══════════════════════════════════════════════════════════════════════
print('\n')
print('=' * 90)
print('Part 2: 轻量尾部保护')
print('  保护规则: CSI800破MA200 且 VIX飙升(20日波动>1.5×年中枢 且>30%) → 半仓')
print('            其他情况 → 满仓(不做渐进式降仓)')
print('=' * 90)

freq_map = {'双周':'biweekly', '月频':'monthly', '季频':'quarterly'}

# ── Protection trigger stats (independent of config) ──
print('\n  ── 轻保护触发全景 ──')
if idx_c is not None:
    trigger_count = 0
    trigger_dates = []
    for d in all_dates:
        below_ma200 = get_position_ratio(idx_c, d) < 1.0
        vix = is_vix_spike(d)
        if below_ma200 and vix:
            trigger_count += 1
            trigger_dates.append(str(d.date()))
    print(f'  CSI800破MA200+VIX飙升: {trigger_count}/{len(all_dates)}天 ({trigger_count/len(all_dates)*100:.1f}%)')
    if trigger_dates:
        episodes = []
        prev = None
        for d_str in trigger_dates:
            d = pd.Timestamp(d_str)
            if prev is None or (d - prev).days > 5:
                episodes.append(d_str)
            prev = d
        print(f'  独立触发时段: {len(episodes)}段 — {", ".join(episodes)}')
else:
    print('  (无CSI800指数数据)')

# ── Run comparison on each candidate ──
for idx, cand in enumerate(candidates_for_p2):
    freq_key = freq_map[cand['freq']]
    print(f'\n  ── Candidate {idx+1}: {cand["label"]} ──')

    # Re-run both sides
    nav_base, to_base, nr_base = run_backtest(
        n_hold=cand['n_hold'], freq=freq_key,
        buffer_rank=cand['buf'], smooth_n=1, use_tail_protect=False)
    m_base = metric(nav_base)
    cd_base = to_base * REAL_COST * 2

    nav_prot, to_prot, nr_prot = run_backtest(
        n_hold=cand['n_hold'], freq=freq_key,
        buffer_rank=cand['buf'], smooth_n=1, use_tail_protect=True)
    m_prot = metric(nav_prot)
    cd_prot = to_prot * REAL_COST * 2

    print(f'  {"":<20} {"毛年化":>7} {"成本":>7} {"净年化":>7} {"夏普":>6} {"回撤":>7} {"波动":>7} {"踩踏DD":>8} {"年换手":>8}')
    print('  ' + '-' * 91)
    for label, nav, to, m in [
        ('纯低波动(基线)', nav_base, to_base, m_base),
        ('低波动+轻保护', nav_prot, to_prot, m_prot),
    ]:
        cd = to * REAL_COST * 2
        net = m['ar'] - cd
        cdd = crunch_dd(nav)
        print(f'  {label:<20} {m["ar"]*100:+6.1f}% {cd*100:+6.1f}% {net*100:+6.1f}% {m["sr"]:5.2f} {m["dd"]*100:+6.1f}% {m["vol"]*100:+5.1f}% {cdd:+7.2f}% {to:7.0%}')

    net_base_val = m_base['ar'] - cd_base
    net_prot_val = m_prot['ar'] - cd_prot
    dd_pct = (m_prot['dd'] - m_base['dd']) * 100
    cdd_pct = (crunch_dd(nav_prot) - crunch_dd(nav_base)) * 100
    print(f'  {"Δ(保护-基线)":<20} {m_prot["ar"]*100-m_base["ar"]*100:+6.1f}pp {"":>6} {net_prot_val*100-net_base_val*100:+6.1f}pp {m_prot["sr"]-m_base["sr"]:+5.2f} {dd_pct:+6.1f}pp {"":>6} {cdd_pct:+7.2f}pp {to_prot-to_base:+7.0%}')

    # Yearly
    yb_base = yearly_breakdown(nav_base)
    yb_prot = yearly_breakdown(nav_prot)
    all_years = sorted(set(list(yb_base.keys()) + list(yb_prot.keys())))
    print(f'  {"年份":<8} {"纯低波":>8} {"+轻保护":>8} {"Δ":>8}')
    print('  ' + '-' * 34)
    for yr in all_years:
        b = yb_base.get(yr, 0)
        p = yb_prot.get(yr, 0)
        print(f'  {yr:<8} {b*100:+7.1f}% {p*100:+7.1f}% {(p-b)*100:+7.1f}pp')

print()
print('═' * 90)
print('优化完毕。主口径: 0.3%单边成本。目标: 净年化≥8% + 夏普≥1.0。')
print('Part 1: 降换手 — 3频率×3持仓×2缓冲 全因子网格')
print('Part 2: 轻量尾部保护 — CSI800破MA200+VIX飙升→半仓')
print('═' * 90)
