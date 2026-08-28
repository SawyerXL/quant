"""
做T策略回测验证 v2 — 忠实复刻实盘规则, 重点审计隔夜强平。

实盘规则(t0_auto.py):
  - 触发: 现价 vs 昨收 ±2% (正T=涨2%卖, 反T=跌2%买)
  - 了结: ±1% 反向挂单, 当日成交才算闭环
  - 隔夜: 当日第二腿没成交 → 次日开盘市价强平 (铁律4)

日内先后无法从日线还原, 用两个模型夹逼真实值:
  A 乐观: 当天 high/low 都摸到目标价 → 视为当日闭环 (原"94.5%"口径)
  B 保守: 第二腿要求收盘价也到了结价方向(收盘<=接回价才算接回) → 强平率上界

P&L 用投入资金的百分比(与仓位规模无关):
  闭环 = +1%(价差) - 双边佣金
  强平 = 次日开盘相对触发价的缺口% - 双边佣金
"""
import sys; sys.path.insert(0,'scripts'); sys.path.insert(0,'.')
import numpy as np
import pandas as pd
from loguru import logger; logger.remove()
from data.storage import load_meta, load_daily

START, END = '2019-01-01', '2026-08-25'
TRIGGER, SETTLE = 0.02, 0.01
COMM = 0.0013 * 2  # 双边佣金

codes = sorted(load_meta('csi800')['code'].tolist())
rng = np.random.RandomState(42)
sample = rng.choice(codes, size=300, replace=False)
print(f'加载 {len(sample)} 只 ...')
data = {}
for c in sample:
    df = load_daily(c, START, END)
    if df.empty or 'high' not in df.columns:
        continue
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    for col in ['open','high','low','close','volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    if len(df) > 100:
        data[c] = df[['open','high','low','close','volume']]
print(f'有效 {len(data)} 只')

def simulate(conservative: bool):
    rt_pos, rt_neg, fs_pos, fs_neg = [], [], [], []
    fs_rt_rates = []  # 每年强平率
    for code, df in data.items():
        closes = df['close'].values; opens = df['open'].values
        highs = df['high'].values; lows = df['low'].values
        for i in range(1, len(df) - 1):
            prev = closes[i-1]
            if prev <= 0: continue
            hi, lo = highs[i], lows[i]
            # 正T
            if hi >= prev * (1 + TRIGGER):
                sell_p = prev * (1 + TRIGGER); buy_p = sell_p * (1 - SETTLE)
                filled = (lo <= buy_p) if not conservative else (closes[i] <= buy_p)
                if filled:
                    rt_pos.append(SETTLE - COMM)
                else:
                    nxt = opens[i+1]
                    if nxt > 0:
                        fs_pos.append((sell_p - nxt) / sell_p - COMM)
            # 反T
            elif lo <= prev * (1 - TRIGGER):
                buy_p = prev * (1 - TRIGGER); sell_p = buy_p * (1 + SETTLE)
                filled = (hi >= sell_p) if not conservative else (closes[i] >= sell_p)
                if filled:
                    rt_pos.append(SETTLE - COMM)
                else:
                    nxt = opens[i+1]
                    if nxt > 0:
                        fs_pos.append((nxt - buy_p) / buy_p - COMM)
    return (pd.Series(rt_pos, name='rt'), pd.Series(fs_pos, name='fs'))

for label, cons in [('A 乐观(原94.5%口径)', False), ('B 保守(收盘到位)', True)]:
    rt, fs = simulate(cons)
    n_rt, n_fs = len(rt), len(fs)
    n = n_rt + n_fs
    print(f'\n{"="*64}')
    print(f'  {label}')
    print(f'{"="*64}')
    print(f'  当日闭环: {n_rt}笔 | 每笔+{rt.mean()*100:.2f}% (价差1%-佣金)')
    print(f'  隔夜强平: {n_fs}笔 | 强平率 {n_fs/n:.1%} | 每笔{fs.mean()*100:+.2f}%')
    print(f'  强平胜率: {(fs>0).mean():.1%}')
    print(f'  合计(每笔平均): {(rt.sum()+fs.sum())/n*100:+.3f}% 投入资金')
    print(f'  年化增厚(按每天~n/N只票滚动做T, 粗估): {(rt.sum()+fs.sum())/n*100*n/1855*100:.1f}%/年/只')
    print(f'  总盈亏(相对投入资金总和的百分比和): {((rt.sum()+fs.sum()))*100:+.0f}% (非年化)')
