"""
个人策略回测 — 三层漏斗 + 十维过滤 + 止损止盈
池子: 成交额TOP30(季度滚动) → 过滤过热 → MA10附近入场

用法: python scripts/backtest_personal_strategy.py
注意: 此脚本数据加载较慢(~30s), 仅作为独立回测入口;
      Web层请使用 backtest_engine.run_backtest() + BacktestConfig.DEFAULT_CONFIG
"""
import sys, os
sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from data.storage import load_meta, load_daily
from run_backtest_a2 import _make_rebal_dates, get_position_ratio
from run_backtest_a import (calc_metrics, BACKTEST_START, COMMISSION, MIN_BARS, CASH_YIELD, PERIOD_STOP, TRAILING_STOP)
from run_backtest_a4 import MA10_EXIT_DAYS, MA_EXIT_WINDOW

def pct(s): return float(str(s).strip('%')) / 100

# ═══ Strategy Functions (import-safe, 供外部调用) ═══

def select_personal_strategy(date, i, panel, ap, TOP_N=30):
    """三层漏斗选股 — 需要外部提供数据面板"""
    if i < 65:
        return [], {}

    # Layer 1: TOP by turnover
    if ap is not None and i >= 20:
        avg_amt = ap.iloc[max(0,i-20):i].mean().dropna()
        pool = avg_amt.nlargest(TOP_N * 2).index.tolist()  # TOP60 for filtering
    else:
        pool = sorted(panel.columns)[:TOP_N * 2]

    # Layer 2: Overbought filter
    eligible = []
    for code in pool:
        if code not in panel.columns: continue
        cl = panel[code]
        cur = panel.iloc[i].get(code)
        if pd.isna(cur) or cur <= 0: continue

        hist = cl.iloc[:i+1].dropna()
        if len(hist) < 60: continue
        cur_hist = hist.iloc[-1]

        ret20 = (cur_hist / hist.iloc[-21] - 1) * 100 if len(hist) >= 21 else 0
        if ret20 > 50: continue
        cons_up = 0
        for j in range(len(hist)-1, max(0,len(hist)-15), -1):
            if j > 0 and hist.iloc[j] > hist.iloc[j-1]:
                cons_up += 1
            else: break
        if cons_up >= 8: continue
        ret5 = (cur_hist / hist.iloc[-6] - 1) * 100 if len(hist) >= 6 else 0
        if ret5 > 15: continue
        # Layer 3: MA10附近
        high20 = hist.iloc[-20:].max()
        dh = (cur_hist / high20 - 1) * 100
        if dh > -3: continue

        eligible.append(code)

    if len(eligible) >= TOP_N:
        selected = eligible[:TOP_N]
    elif len(eligible) > 0:
        selected = eligible
    else:
        avg_amt_pool = ap.iloc[max(0,i-20):i].mean()
        available = [(c, avg_amt_pool.get(c,0)) for c in pool if c in panel.columns]
        available.sort(key=lambda x: x[1], reverse=True)
        selected = [c for c,_ in available[:TOP_N]]

    n = min(len(selected), TOP_N)
    return selected[:n], {c: 1.0/n for c in selected[:n]}

def run_backtest(panel, ap, all_dates, rebal_set, idx_c):
    """个人策略回测引擎 — 需要外部提供数据面板"""
    port_rets = pd.Series(0.0, index=all_dates)
    cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
    entry_hwm = {}; trail_hwm = {}
    cumul_nav = 1.0; pos_nav = 1.0
    TOP_N = 30; MAX_POS = 0.10

    for i, date in enumerate(all_dates):
        date_str = str(date.date())

        # Step 1: Mark-to-market
        if cur_weights and i > 0:
            ret = 0.0
            for code, w in cur_weights.items():
                pp = panel.iloc[i-1].get(code); cp = panel.iloc[i].get(code)
                if pp and cp and not pd.isna(pp) and not pd.isna(cp) and pp > 0:
                    ret += w * (cp/pp - 1)
            port_rets.iloc[i] += ret

        # Step 2: Individual stops
        if cur_weights and i >= 10:
            exits = []
            for code in list(cur_weights.keys()):
                col = panel[code] if code in panel.columns else None
                if col is None: continue
                hist = col.iloc[max(0,i-MA_EXIT_WINDOW+1):i+1].dropna()
                if len(hist) < max(5, MA_EXIT_WINDOW//2): continue
                ma10 = hist.mean(); cp = panel.iloc[i].get(code)
                if pd.isna(cp) or cp <= 0: continue

                if cp < ma10:
                    days_below_ma10[code] = days_below_ma10.get(code,0) + 1
                else:
                    days_below_ma10[code] = 0

                ep = entry_prices.get(code, cp)
                if ep > 0:
                    if cp/ep - 1 <= -0.12:
                        exits.append(code); continue
                    if code not in trail_hwm or cp > trail_hwm[code]:
                        trail_hwm[code] = cp
                    if trail_hwm.get(code, cp) > 0 and cp/trail_hwm[code] - 1 <= -0.18:
                        exits.append(code); continue

                if days_below_ma10.get(code,0) >= MA10_EXIT_DAYS:
                    exits.append(code)

            for code in set(exits):
                w = cur_weights.pop(code, 0)
                port_rets.iloc[i] -= w * COMMISSION
                entry_prices.pop(code, None); days_below_ma10.pop(code, None)
                trail_hwm.pop(code, None)

        # Step 3: Take profit
        if cur_weights and i > 0:
            take_profit = []
            for code in list(cur_weights.keys()):
                ep = entry_prices.get(code)
                if ep and ep > 0:
                    cp = panel.iloc[i].get(code)
                    if cp and cp/ep - 1 >= 0.50:
                        take_profit.append((code, cur_weights[code] * 0.33))
                    elif cp and cp/ep - 1 >= 0.25 and code not in trail_hwm:
                        take_profit.append((code, cur_weights[code] * 0.33))
            for code, reduce_w in take_profit:
                cur_weights[code] = cur_weights.get(code,0) - reduce_w
                port_rets.iloc[i] -= reduce_w * COMMISSION
                if code in trail_hwm: trail_hwm[code] = max(trail_hwm[code], panel.iloc[i].get(code,0))

        # Step 4: Rebalance
        if date_str in rebal_set and i >= MIN_BARS:
            pos_ratio = get_position_ratio(idx_c, date) if idx_c is not None else 1.0
            if pos_ratio <= 0.30:
                cur_weights = {}; entry_prices = {}; days_below_ma10 = {}
                trail_hwm = {}; pos_nav = 1.0
            else:
                top_codes, weights_dict = select_personal_strategy(date, i, panel, ap, TOP_N)
                n = min(len(top_codes), TOP_N)
                if n == 0: continue

                new_w = {}
                cash_w = max(0, pos_ratio - sum(cur_weights.values())) if cur_weights else pos_ratio
                if cash_w <= 0: continue

                w_per_stock = min(MAX_POS, cash_w / n)
                for c in top_codes[:n]:
                    new_w[c] = w_per_stock

                enter_w = sum(new_w.get(c,0) for c in set(new_w) - set(cur_weights))
                exit_w = sum(cur_weights.get(c,0) for c in set(cur_weights) - set(new_w))
                port_rets.iloc[i] -= (enter_w + exit_w)/2 * COMMISSION * 2

                cp_s = panel.ffill().iloc[i]
                for c in set(new_w) - set(cur_weights):
                    ep = cp_s.get(c)
                    if ep and not pd.isna(ep): entry_prices[c] = float(ep)

                cur_weights = new_w; pos_nav = 1.0

        # Step 5: Cash yield
        cash_r = max(0, 1.0 - sum(cur_weights.values())) if cur_weights else 1.0
        port_rets.iloc[i] += cash_r * CASH_YIELD/252
        cumul_nav *= (1+port_rets.iloc[i])

    return (1+port_rets).cumprod()


# ═══ CLI Entry Point ═══
if __name__ == '__main__':
    cal = load_meta('trade_calendar')
    end = sorted([d for d in cal['trade_date'] if d <= '2026-06-30'])[-1]
    calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
    c800 = load_meta('csi800'); codes = sorted(c800['code'].tolist())

    print('Loading data...')
    prices, amounts = {}, {}
    for code in codes:
        try:
            df = load_daily(code, BACKTEST_START, end)
            if df.empty: continue
            df['date'] = pd.to_datetime(df['date']); df = df.set_index('date').sort_index()
            cl = pd.to_numeric(df['close'], errors='coerce').dropna()
            amt = pd.to_numeric(df.get('amount', pd.Series(dtype=float)), errors='coerce').dropna()
            if len(cl) >= MIN_BARS:
                prices[code] = cl; amounts[code] = amt
        except: pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    print(f'Panel: {panel.shape[0]}d × {panel.shape[1]} stocks')

    idx = load_meta('csi800_index')
    idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None
    if idx_c is not None:
        idx_c.index = pd.to_datetime(idx_c.index)

    rebal_dates = _make_rebal_dates(calendar, 'biweekly')
    rebal_set = set(rebal_dates)
    all_dates = panel.index

    print('\nRunning personal strategy backtest...')
    nav_personal = run_backtest(panel, ap, all_dates, rebal_set, idx_c)
    m = calc_metrics(nav_personal)
    ar = pct(m['年化收益率']); sr = float(m['夏普比率']); dd = pct(m['最大回撤'])

    print(f'\n{"="*60}')
    print(f'个人策略回测 ({BACKTEST_START}→{end})')
    print(f'{"="*60}')
    print(f'  年化收益: {ar*100:+.2f}%')
    print(f'  夏普比率: {sr:.2f}')
    print(f'  最大回撤: {dd*100:+.2f}%')

    print(f'\n基准对比:')
    nav_ew = panel.mean(axis=1)
    nav_ew = (1 + nav_ew.pct_change().fillna(0)).cumprod()
    m_ew = calc_metrics(nav_ew)
    print(f'  CSI800等权: 年化{pct(m_ew["年化收益率"])*100:+.2f}% 夏普{float(m_ew["夏普比率"]):.2f} 回撤{pct(m_ew["最大回撤"])*100:+.2f}%')

    print(f'\nTOP30等权(无过滤):')
    top30 = ap.iloc[-250:].mean().nlargest(30).index.tolist()
    nav_top30 = panel[top30].mean(axis=1)
    nav_top30 = (1 + nav_top30.pct_change().fillna(0)).cumprod()
    m_t30 = calc_metrics(nav_top30)
    print(f'  年化{pct(m_t30["年化收益率"])*100:+.2f}% 夏普{float(m_t30["夏普比率"]):.2f} 回撤{pct(m_t30["最大回撤"])*100:+.2f}%')

    print(f'\n逐年:')
    for y in range(2020, 2027):
        yn = nav_personal[(nav_personal.index >= f'{y}-01-01') & (nav_personal.index <= f'{y}-12-31')]
        if len(yn) > 1:
            y_ret = (yn.iloc[-1]/yn.iloc[0] - 1)*100
            print(f'  {y}: {y_ret:+.2f}%')
