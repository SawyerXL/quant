# -*- coding: utf-8 -*-
"""run2 全期NAV重建: 逐日现金+持仓市值 → 年度收益/回撤路径/熔断周期识别"""
import pandas as pd
import numpy as np

DIR = '/root/quant/data_store/ptrade_backtest_1m/run2'
t = pd.read_csv(f'{DIR}/交易详情20260908143900.csv', encoding='gbk')
t.columns = ['date', 'time', 'code', 'side', 'openclose', 'qty', 'price', 'fee']
t['date'] = pd.to_datetime(t['date'])
t['value'] = t['qty'] * t['price']
t['ym'] = t['date'].dt.to_period('M')

h = pd.read_csv(f'{DIR}/持仓明细20260908143852.csv', encoding='gbk')
h.columns = ['date', 'time', 'code', 'last_px', 'qty', 'longshort', 'cost', 'mv', 'pnl']
h['date'] = pd.to_datetime(h['date'])
mv_map = h.groupby('date')['mv'].sum()

# 现金账: 100万 - 买 + 卖 - 费
cash = 1_000_000.0
dates = sorted(set(t['date']) | set(mv_map.index))
navs = {}
for d in dates:
    day = t[t.date == d]
    if len(day):
        cash -= day[day.side == '买']['value'].sum() + day[day.side == '买']['fee'].sum()
        cash += day[day.side == '卖']['value'].sum() - day[day.side == '卖']['fee'].sum()
    navs[d] = cash + mv_map.get(d, 0.0)
nav = pd.Series(navs).sort_index()

print(f'重建期末 NAV {nav.iloc[-1]:,.0f} (pTrade报告 +14.68% → {1_000_000*1.1468:,.0f})')
print(f'峰值 {nav.max():,.0f} @ {nav.idxmax():%Y-%m-%d}')
print(f'谷值 {nav.min():,.0f} @ {nav.idxmin():%Y-%m-%d}')
peak = nav.max()
dd = nav / nav.cummax() - 1
print(f'最大回撤 {dd.min()*100:.2f}% @ {dd.idxmin():%Y-%m-%d} (pTrade报告 -37.37%)')

print('\n年度收益(重建):')
yr = nav.resample('YE').last()
prev = 1_000_000.0
for d, v in yr.items():
    if d.year < 2019 or d > nav.index[-1]:
        continue
    r = v / prev - 1
    print(f'{d.year}: {r:+.2%}  期末nav {v:,.0f}')
    prev = v

print('\n半年度回撤谷:')
for d, v in dd.resample('6MS').min().items():
    print(f'{d:%Y-%m}: {v*100:.1f}%')

print('\n费率: 买中位 {:.2f}bp 卖中位 {:.2f}bp'.format(
    (t[t.side == '买']['fee'] / t[t.side == '买']['value'] * 1e4).median(),
    (t[t.side == '卖']['fee'] / t[t.side == '卖']['value'] * 1e4).median()))
print(f'总手续费 {t.fee.sum():,.0f} 元 ({t.fee.sum()/1e6:.1f}% of 100万)')

# ── 熔断周期识别: 净卖出>20万的日子(大规模出清) ──
print('\n大规模净卖出日(净卖>20万):')
for d in dates:
    day = t[t.date == d]
    if len(day) == 0:
        continue
    net = day[day.side == '卖']['value'].sum() - day[day.side == '买']['value'].sum()
    if net > 200_000:
        print(f'{d:%Y-%m-%d}: 卖{day[day.side=="卖"].value.sum():>10,.0f} '
              f'买{day[day.side=="买"].value.sum():>10,.0f} 净{net:>10,.0f} '
              f'nav={navs.get(d, 0):,.0f}')

# ── 持仓数突降段(出清段) ──
per = h.groupby('date').agg(n=('code', 'count'))
print('\n持仓数变化关键点(相邻日持仓数差<-20):')
prev_n = None
for d, n in per['n'].items():
    if prev_n is not None and prev_n - n > 20:
        print(f'{d:%Y-%m-%d}: {prev_n} → {n}只  nav={navs.get(d, 0):,.0f}')
    prev_n = n
nav.to_csv('/root/quant/data_store/ptrade_backtest_1m/run2/nav_recon.csv')
