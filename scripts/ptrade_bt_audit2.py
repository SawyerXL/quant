# -*- coding: utf-8 -*-
"""审计2: 验证'2019年熔断后永久空仓'假说 — 逐月买卖结构/持仓衰减/NAV重建/最后交易代码"""
import pandas as pd
import numpy as np

DIR = '/root/quant/data_store/ptrade_backtest_1m'
trades = pd.read_csv(f'{DIR}/交易详情20260908082036.csv', encoding='gbk')
trades.columns = ['date', 'time', 'code', 'side', 'openclose', 'qty', 'price', 'fee']
trades['date'] = pd.to_datetime(trades['date'])
trades['code6'] = trades['code'].str[:6]
trades['value'] = trades['qty'] * trades['price']
trades['ym'] = trades.date.dt.to_period('M')

print('── 逐月买卖笔数与净额 ──')
for ym, grp in trades.groupby('ym'):
    b = grp[grp.side == '买']
    s = grp[grp.side == '卖']
    print(f'{ym}: 买{len(b):>3}笔{b.value.sum():>12,.0f}  卖{len(s):>3}笔{s.value.sum():>12,.0f}')

print('\n── 6-7月残存交易明细 ──')
tail = trades[trades.date >= '2019-06-01']
print(tail[['date', 'code6', 'side', 'qty', 'price', 'value']].to_string(index=False))

print('\n── 7月持仓代码 ──')
hold = pd.read_csv(f'{DIR}/持仓明细20260908082032.csv', encoding='gbk')
hold.columns = ['date', 'time', 'code', 'last_px', 'qty', 'longshort', 'cost', 'mv', 'pnl']
hold['date'] = pd.to_datetime(hold['date'])
hold['code6'] = hold['code'].str[:6]
h7 = hold[hold.date >= '2019-06-20']
print(h7[['date', 'code6', 'qty', 'last_px', 'mv']].to_string(index=False))

print('\n── 每日持仓数与市值(周频抽样) ──')
per = hold.groupby('date').agg(n=('code6', 'count'), mv=('mv', 'sum'))
per = per[::5]
print(per.to_string())

print('\n── 窗口NAV重建(100万起步, 逐日: 现金+持仓市值) ──')
# 现金: 100万 - 累计买入 + 累计卖出 - 手续费; 持仓市值: 持仓明细当日mv(次日更新前)
cash = 1_000_000.0
cum = []
mv_map = hold.groupby('date')['mv'].sum()
dates = sorted(set(trades['date']) | set(mv_map.index))
navs = {}
for d in dates:
    day = trades[trades.date == d]
    if len(day):
        cash -= day[day.side == '买']['value'].sum() + day[day.side == '买']['fee'].sum()
        cash += day[day.side == '卖']['value'].sum() - day[day.side == '卖']['fee'].sum()
    mv = mv_map.get(d, 0.0)
    navs[d] = cash + mv
nav = pd.Series(navs).sort_index()
print(f'NAV 峰值 {nav.max():,.0f} @ {nav.idxmax():%Y-%m-%d}')
print(f'NAV 末日 {nav.iloc[-1]:,.0f} @ {nav.index[-1]:%Y-%m-%d}')
print(f'窗口末回撤 {(1-nav.iloc[-1]/nav.max())*100:.1f}%')
print(nav[::10].to_string())

print('\n── 5月逐日净卖出(熔断后只卖不买?) ──')
may = trades[(trades.date >= '2019-04-25') & (trades.date <= '2019-05-31')]
day_net = may.groupby('date').apply(lambda g: g[g.side == '卖']['value'].sum() - g[g.side == '买']['value'].sum())
print(day_net.to_string())
