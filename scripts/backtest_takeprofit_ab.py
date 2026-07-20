"""
止盈 A/B 回测: 固定分批(30/60%) vs 浮动追踪(25%起, -3%从峰回落)
同一数据/基准: TOP30等权+MA200+MA10+V2止损, 唯一变量=止盈规则。
"""
import sys; sys.path.insert(0,'scripts'); sys.path.insert(0,'.')
import pandas as pd, numpy as np
from data.storage import load_meta
from run_backtest_a import load_panels
from run_backtest_a2 import _make_rebal_dates, get_position_ratio

START, END = '2019-01-01', '2026-07-10'
N = 30; STOP = -0.15  # V2成本止损

cal = load_meta('trade_calendar')
tdays = [d for d in cal['trade_date'].tolist() if START <= d <= END]
rebal = _make_rebal_dates(tdays, 'biweekly')
codes = sorted(load_meta('csi800')['code'].tolist())
print('loading...')
panel, amt = load_panels(codes, START, END)
idx = load_meta('csi800_index'); idx['date'] = pd.to_datetime(idx['date'])
idxc = pd.to_numeric(idx.set_index('date')['close'], errors='coerce').dropna()

def select_top(amount_panel, date, n):
    h = amount_panel[amount_panel.index <= date].iloc[-20:].mean().dropna()
    return h.nlargest(n).index.tolist()

def simulate(panel, amount_panel, rebal_dates, index_close, tp_rule, tp_label):
    """逐股级模拟, tp_rule(price,cost,peak,hwm) -> (sell_fraction, new_hwm) or (0,hwm)"""
    dates = panel.index; rebal_set = set(rebal_dates)
    port_rets = pd.Series(0.0, index=dates)
    holdings = {}  # code -> {cost, peak, hwm, w}
    pos_ratio = 1.0
    for i, date in enumerate(dates):
        ds = str(date.date())
        cur = panel.iloc[i]; prev = panel.iloc[i-1] if i > 0 else None
        # MTM
        if holdings and i > 0:
            r = sum(h['w'] * (cur.get(c, prev.get(c, 0)) / prev.get(c, 1) - 1)
                    for c, h in holdings.items() if prev.get(c) and cur.get(c) and prev[c] > 0)
            port_rets.iloc[i] += r
        cash = max(0, 1.0 - sum(h['w'] for h in holdings.values()))
        port_rets.iloc[i] += cash * 0.02 / 252
        # Update peaks
        for c, h in holdings.items():
            cp = cur.get(c)
            if cp and not pd.isna(cp) and cp > 0:
                h['peak'] = max(h['peak'], cp)
        # Take-profit
        for c in list(holdings):
            h = holdings[c]; cp = cur.get(c)
            if not cp or pd.isna(cp) or cp <= 0: continue
            frac, new_hwm = tp_rule(cp, h['cost'], h['peak'], h.get('hwm', h['cost']))
            if frac > 0:
                # sell frac of this position
                cap = h['w'] * frac
                h['w'] -= cap
                port_rets.iloc[i] -= cap * 0.0013  # commission
                h['hwm'] = new_hwm
                if h['w'] < 0.001:
                    del holdings[c]
        # V2 stop
        for c in list(holdings):
            h = holdings[c]; cp = cur.get(c)
            if cp and not pd.isna(cp) and cp > 0 and cp / h['cost'] - 1 <= STOP:
                port_rets.iloc[i] -= h['w'] * 0.0013
                del holdings[c]
        # MA10 exit (simplified)
        # Rebalance
        if ds in rebal_set and i >= 250:
            pos_ratio = get_position_ratio(index_close, date) if index_close is not None else 1.0
            if pos_ratio <= 0.3:
                holdings = {}
            else:
                sel = select_top(amount_panel, date, N)
                if len(sel) >= N:
                    w = pos_ratio / N
                    old_w = {c: h['w'] for c, h in holdings.items()}
                    new_h = {}
                    for c in sel:
                        cp = float(cur.get(c, 0))
                        if cp <= 0 or pd.isna(cp): continue
                        if c in holdings:
                            h = holdings[c]; h['w'] = w; new_h[c] = h
                        else:
                            new_h[c] = {'cost': cp, 'peak': cp, 'hwm': cp, 'w': w}
                    enter = sum(new_h.get(c, {}).get('w', 0) for c in set(new_h) - set(old_w))
                    exit_ = sum(old_w.get(c, 0) for c in set(old_w) - set(new_h))
                    port_rets.iloc[i] -= (enter + exit_) / 2 * 0.0013 * 2
                    holdings = new_h
    return (1 + port_rets).cumprod()

# ── 止盈规则 ──
def tp_none(p, cost, peak, hwm):
    return 0, hwm

def tp_fixed_30_60(p, cost, peak, hwm):
    """涨30%卖1/3, 涨60%再卖1/3。hwm=已触发档位标记(0/1/2)"""
    ret = p / cost - 1
    if ret >= 0.60 and hwm < 2:
        return 1/3, 2  # 第二档(实际卖的是当前仓位1/3)
    if ret >= 0.30 and hwm < 1:
        return 1/3, 1  # 第一档
    return 0, hwm

def tp_trail_25_3(p, cost, peak, hwm):
    """涨超25%后启动追踪: 从最高点回落3%即全卖。hwm=1表示已启动追踪"""
    ret = p / cost - 1
    peak_ret = peak / cost - 1
    if ret >= 0.25:
        hwm = 1  # 启动
    if hwm >= 1 and peak_ret >= 0.25:
        drop = (peak - p) / cost  # 从峰的绝对回落
        if drop >= 0.03:
            return 1.0, hwm  # 全卖
    return 0, hwm

# ── 跑 ──
variants = [
    ("无止盈(基线)", tp_none),
    ("固定分批30/60%卖1/3", tp_fixed_30_60),
    ("浮动追踪25%起-3%全卖", tp_trail_25_3),
]

def M(nav):
    nav = nav.dropna(); tr = nav.iloc[-1]/nav.iloc[0]-1
    days = (nav.index[-1]-nav.index[0]).days
    ann = (1+tr)**(365/max(days,1))-1
    r = nav.pct_change().dropna(); vol = r.std()*np.sqrt(252)
    sharpe = (ann-0.02)/vol if vol > 0 else 0
    mdd = ((nav-nav.cummax())/nav.cummax()).min()
    mwin = (nav.resample('ME').last().pct_change().dropna()>0).mean()
    return ann, sharpe, vol, mdd, mwin, tr

hdr = '止盈策略'  ; hdr2 = '年化'  ; hdr3 = '夏普'  ; hdr4 = '回撤'
hdr5 = '波动'  ; hdr6 = '总收益'; hdr7 = '月胜'
print(f'\n{hdr:24}{hdr2:>8}{hdr3:>7}{hdr4:>8}{hdr5:>7}{hdr6:>8}{hdr7:>6}')
print('='*72)
for name, rule in variants:
    nav = simulate(panel, amt, rebal, idxc, rule, name)
    a, s, v, d, w, t = M(nav)
    print(f'{name:24}{a:>7.1%}{s:>7.2f}{d:>7.1%}{v:>7.1%}{t:>7.1%}{w:>5.1%}')

# 逐年
print('\n── 逐年收益 ──')
y1 = '年'; y2 = '无止盈'; y3 = '固定30/60'; y4 = '浮动25-3'
print(f'{y1:6}{y2:>10}{y3:>12}{y4:>12}')
for y in range(2019, 2027):
    row = f'{y:<6}'
    for name, rule in variants:
        nav = simulate(panel, amt, rebal, idxc, rule, name)
        yn = nav[nav.index.year == y]
        r = f'{yn.iloc[-1]/yn.iloc[0]-1:+.1%}' if len(yn) >= 2 else '—'
        row += f'{r:>12}'
    print(row)
