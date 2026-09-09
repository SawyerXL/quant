"""
个股持仓策略(最新v3 DEFAULT_CONFIG: TOP60+MA10-4d+30/60止盈) vs QMT主策略(TOP30+MA200+MA10+V2止损)
同一数据/同一区间/同一口径对比。
"""
import sys; sys.path.insert(0, 'scripts'); sys.path.insert(0, '.')
import pandas as pd, numpy as np
from loguru import logger; logger.remove()
from data.storage import load_meta
from run_backtest_a import load_panels
from run_backtest_a2 import _make_rebal_dates
import backtest_engine as be
from backtest_config import DEFAULT_CONFIG
from backtest_stoploss_ab import simulate as qmt_sim, v2_cost_only, _drop_corrupt_codes

START, END = '2019-01-01', '2026-07-10'
cal = load_meta('trade_calendar')
tdays = [d for d in cal['trade_date'].tolist() if START <= d <= END]
rebal = _make_rebal_dates(tdays, 'biweekly')
codes = _drop_corrupt_codes(sorted(load_meta('csi800')['code'].tolist()), START, END)
print(f'加载 {len(codes)} 只 CSI800 {START}→{END} ...')
panel, amt = load_panels(codes, START, END)
idx = load_meta('csi800_index'); idx['date'] = pd.to_datetime(idx['date'])
idxc = pd.to_numeric(idx.set_index('date')['close'], errors='coerce').dropna()

# 个股策略(最新, 按设计=常时满仓无MA200择时)
navp, _mp = be.run_backtest(panel, amt, rebal, DEFAULT_CONFIG, None)
# 参考: 个股策略叠加MA200择时
navpt, _ = be.run_backtest(panel, amt, rebal, DEFAULT_CONFIG, idxc)
# QMT主策略(V2, 含MA200择时)
navq, _sq = qmt_sim(panel, amt, rebal, idxc, v2_cost_only)

def M(nav):
    nav = nav.dropna()
    tr = nav.iloc[-1]/nav.iloc[0]-1
    days = (nav.index[-1]-nav.index[0]).days
    ann = (1+tr)**(365/max(days,1))-1
    r = nav.pct_change().dropna()
    vol = r.std()*np.sqrt(252)
    sharpe = (ann-0.02)/vol if vol > 0 else 0
    mdd = ((nav-nav.cummax())/nav.cummax()).min()
    mwin = (nav.resample('ME').last().pct_change().dropna() > 0).mean()
    return ann, sharpe, vol, mdd, mwin, tr

def yr(nav, y):
    s = nav[nav.index.year == y]
    return (s.iloc[-1]/s.iloc[0]-1) if len(s) >= 2 else None

print('\n' + '='*84)
print(f'{"指标":12}{"个股策略(满仓无择时)":>22}{"个股+MA200择时":>18}{"QMT主策略(TOP30+择时)":>22}')
print('='*84)
ap = M(navp); at = M(navpt); aq = M(navq)
rows = [('总收益', ap[5], at[5], aq[5], '%'), ('年化', ap[0], at[0], aq[0], '%'), ('夏普(rf2%)', ap[1], at[1], aq[1], 'x'),
        ('年化波动', ap[2], at[2], aq[2], '%'), ('最大回撤', ap[3], at[3], aq[3], '%'), ('月度胜率', ap[4], at[4], aq[4], '%')]
for name, vp, vt, vq, u in rows:
    if u == '%': print(f'{name:12}{vp:>21.1%}{vt:>17.1%}{vq:>21.1%}')
    else: print(f'{name:12}{vp:>21.2f}{vt:>17.2f}{vq:>21.2f}')
print('\n── 逐年 ──')
print(f'{"年":6}{"个股(满仓)":>12}{"个股+择时":>12}{"QMT主策略":>13}')
for y in range(2019, 2027):
    a, t, b = yr(navp, y), yr(navpt, y), yr(navq, y)
    if a is None and b is None: continue
    f = lambda x: ("%+.1f%%" % (x*100)) if x is not None else "—"
    print(f'{y:<6}{f(a):>12}{f(t):>12}{f(b):>13}')
