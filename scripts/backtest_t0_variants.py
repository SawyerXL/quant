"""
做T方案四选一回测: A趋势过滤 / B收紧了结 / C限价强平 / D基线 + 组合。
统一中间模型(摸到接回价但收盘没到位→50%当日成交), 另报乐观模型参照。
P&L口径: 每笔占投入资金(1/3仓位)的百分比, 与资金规模无关。
"""
import sys; sys.path.insert(0,'scripts'); sys.path.insert(0,'.')
import numpy as np
import pandas as pd
from loguru import logger; logger.remove()
from data.storage import load_meta, load_daily

START, END = '2019-01-01', '2026-08-25'
COMM = 0.0013 * 2

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
    for col in ['open','high','low','close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    if len(df) > 100:
        data[c] = df[['open','high','low','close']]
print(f'有效 {len(data)} 只')

def simulate(trigger=0.02, settle=0.01, gap_filter=0.0, limit_settle=False,
             limit_days=3, middle=True):
    """返回 trades list of pnl%(占1/3仓投入)。"""
    pnls = []
    fs_count = 0
    for code, df in data.items():
        o = df['open'].values; h = df['high'].values
        l = df['low'].values; c = df['close'].values
        n = len(df)
        for i in range(1, n - 1):
            prev = c[i-1]
            if prev < 1.0:  # 前复权早年的近零/负价假数据, 比值会爆炸
                continue
            gap = o[i] / prev - 1
            if gap_filter > 0 and abs(gap) > gap_filter:
                continue  # 趋势过滤: 大幅跳空日跳过
            hi, lo, cl = h[i], l[i], c[i]
            # 正T
            if hi >= prev * (1 + trigger):
                sp = prev * (1 + trigger); bp = sp * (1 - settle)
                if lo <= bp:
                    if (cl <= bp) or (not middle):
                        pnls.append(settle - COMM)  # 当日闭环
                    else:
                        pnls.append(0.5 * (settle - COMM) + 0.5 * fs_pnl(sp, 'buy', bp, i, o, h, l, c, n, limit_settle, limit_days))
                        fs_count += 0.5
                else:
                    pnls.append(fs_pnl(sp, 'buy', bp, i, o, h, l, c, n, limit_settle, limit_days))
                    fs_count += 1
            # 反T
            elif lo <= prev * (1 - trigger):
                bp = prev * (1 - trigger); sp = bp * (1 + settle)
                if hi >= sp:
                    if (cl >= sp) or (not middle):
                        pnls.append(settle - COMM)
                    else:
                        pnls.append(0.5 * (settle - COMM) + 0.5 * fs_pnl(bp, 'sell', sp, i, o, h, l, c, n, limit_settle, limit_days))
                        fs_count += 0.5
                else:
                    pnls.append(fs_pnl(bp, 'sell', sp, i, o, h, l, c, n, limit_settle, limit_days))
                    fs_count += 1
    return np.array(pnls), fs_count

def _cap(x):
    return max(-0.21, min(0.21, x))

def fs_pnl(entry_p, side, settle_p, i, o, h, l, c, n, limit_settle, limit_days):
    """隔夜了结 P&L%: 限价等了结价limit_days天, 或次日开盘市价。"""
    if not limit_settle:
        nxt = o[i+1] if i+1 < n else c[i]
        if nxt <= 0:
            return -COMM
        return _cap(((entry_p - nxt) / entry_p - COMM) if side == 'buy' else ((nxt - entry_p) / entry_p - COMM))
    # 限价等N天: 等了结价成交时, P&L是价差比例(entry→settle), 不是价格本身
    for d in range(1, limit_days + 1):
        j = i + d
        if j >= n:
            break
        if side == 'buy' and l[j] <= settle_p:
            return (entry_p - settle_p) / entry_p - COMM  # 等到接回价
        if side == 'sell' and h[j] >= settle_p:
            return (settle_p - entry_p) / entry_p - COMM
    j = min(i + limit_days + 1, n - 1)
    nxt = o[j] if j < n else c[i]
    if nxt <= 0:
        return -COMM
    return _cap(((entry_p - nxt) / entry_p - COMM) if side == 'buy' else ((nxt - entry_p) / entry_p - COMM))

variants = [
    ('D 基线(现行: ±2%/±1%/次日开盘强平)', dict()),
    ('A 趋势过滤(跳空>1%跳过)', dict(gap_filter=0.01)),
    ('B 收紧了结(±0.5%)', dict(settle=0.005)),
    ('C 限价强平(等3天)', dict(limit_settle=True, limit_days=3)),
    ('A+B 过滤+收紧', dict(gap_filter=0.01, settle=0.005)),
    ('A+C 过滤+限价强平', dict(gap_filter=0.01, limit_settle=True, limit_days=3)),
    ('B+C 收紧+限价强平', dict(settle=0.005, limit_settle=True, limit_days=3)),
    ('A+B+C 全上', dict(gap_filter=0.01, settle=0.005, limit_settle=True, limit_days=3)),
]

print(f'\n{"方案":34}{"笔数":>8}{"强平率":>8}{"每笔均值":>10}{"胜率":>8}{"总增厚":>10}')
print('='*80)
for name, kw in variants:
    p, fs = simulate(**kw)
    if len(p) == 0:
        print(f'{name:34} 无交易'); continue
    mean_pnl = p.mean()
    win = (p > 0).mean()
    print(f'{name:34}{len(p):>8}{fs/len(p):>7.1%}{mean_pnl*100:>+9.3f}%{win:>7.1%}{p.sum()*100:>+9.0f}%')

# 乐观模型对照(基线)
print()
print('── 乐观模型参照(摸到价即当日闭环) ──')
for name, kw in [('D 基线乐观', dict(middle=False)), ('A 趋势过滤乐观', dict(gap_filter=0.01, middle=False)),
                 ('B 收紧乐观', dict(settle=0.005, middle=False))]:
    p, fs = simulate(**kw)
    print(f'  {name:24}{len(p):>7}笔 强平率{fs/len(p):.1%} 每笔{p.mean()*100:+.3f}% 胜率{(p>0).mean():.1%}')
