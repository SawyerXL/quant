"""
做T方案补充回测: E尾盘了结 / G单笔止损, 对照 D基线 / A趋势过滤(当前实盘口径)。

口径(与 backtest_t0_variants.py 完全一致):
- 信号时点: 当日盘中触及昨收±2%触发价即入场(正T卖/反T买), 日内先后未建模
- 了结: 当日触到了结价(±1%)→当日闭环; 否则按方案:
    D=次日开盘市价(含隔夜跳空, 不拆gap) | E=当日收盘价(≈14:50尾盘市价近似)
    G=日内止损stop(先触止损则止损, 止损与了结同日都触到→50/50折中)
- 中间模型: 触到了结价但收盘未到位→50%当日成交(最贴近实盘)
- 成本: 单边0.13%双边0.26%; P&L口径: 每笔占1/3仓投入的百分比
- 样本: CSI800随机300只(random seed 42, 与variants原版同), 2019-01-01~2026-08-25
- 完整性: 建样本时扫描>50%日跳脏数据, 脏跳日及其次日跳过(防假触发)

事前预测登记(2026-09-03, 跑之前写死):
1. E每笔均值 > D(-0.345%): 预计-0.05%~-0.20% (强平亏损主要来自隔夜跳空)
2. G1止损1% 优于 D: 砍极端尾部(-7.4%型→-1.26%含费)
3. A+E+G1 接近打平或微正, 最可能成为可实盘方案
错误风险: 单边日当日趋势损失若>隔夜跳空则E优势缩水; "先触止损后触了结"先后未建模, G可能被高估

判读矩阵: A+E+G1每笔>0且优于A→提交实盘替换; 优于A但仍<0→股票做T暂停只留CB/ETF;
不优于A→维持现状。
归因闸门: 若优势集中≤2个极端事件(某日崩盘被止损救回), 标注且不据此改实盘。
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
dirty_report = []
for c in sample:
    df = load_daily(c, START, END)
    if df.empty or 'high' not in df.columns:
        continue
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()
    for col in ['open','high','low','close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    if len(df) > 100:
        ret = df['close'].pct_change()
        bad = (ret.abs() > 0.5)
        if bad.sum() > 0:
            dirty_report.append((c, bad.sum(), ret[bad].abs().max()))
        # 脏跳日及其次日跳过: 防假触发/假了结/假强平
        skip = bad | bad.shift(1)
        df['_skip'] = skip.fillna(False)
        data[c] = df[['open','high','low','close','_skip']]
print(f'有效 {len(data)} 只')

print('\n── 完整性检查: >50%日跳扫描 ──')
if dirty_report:
    for c, n, mx in sorted(dirty_report, key=lambda x: -x[1])[:15]:
        print(f'  ⚠️ {c}: {n}个脏跳日, 最大{abs(mx)*100:.0f}% — 已跳过脏跳日及其次日')
    if len(dirty_report) > 15:
        print(f'  ... 共{len(dirty_report)}只有脏跳')
else:
    print('  ✅ 300只样本无>50%日跳')

def _cap(x):
    return max(-0.21, min(0.21, x))

def simulate(trigger=0.02, settle=0.01, gap_filter=0.0, tail_close=False,
             stop=0.0, middle=True, stop_adj='half'):
    """返回 (pnls, cnt, rec) — cnt按笔加权: settle/force_next/force_tail/stop。
    rec: 每笔(date, pnl)用于逐年/极端事件归因。
    stop_adj: 止损与了结同日都触到时的判读 — 'half'=50/50折中; 'close'=按收盘位置判读。"""
    pnls = []
    rec = []
    cnt = {'settle': 0.0, 'force_next': 0.0, 'force_tail': 0.0, 'stop': 0.0}
    for code, df in data.items():
        o = df['open'].values; h = df['high'].values
        l = df['low'].values; c = df['close'].values
        sk = df['_skip'].values
        idx = df.index
        n = len(df)
        for i in range(1, n - 1):
            if sk[i]:
                continue
            prev = c[i-1]
            if prev < 1.0 or prev != prev:  # 前复权早年近零/负价假数据
                continue
            gap = o[i] / prev - 1
            if gap_filter > 0 and abs(gap) > gap_filter:
                continue  # 趋势过滤: 大幅跳空日跳过
            hi, lo, cl = h[i], l[i], c[i]
            def force(w=1.0):
                if tail_close:
                    cnt['force_tail'] += w
                    return _cap((sp2 - cl) / sp2 - COMM)
                cnt['force_next'] += w
                nxt = o[i+1] if i+1 < n else c[i]
                if nxt <= 0:
                    return -COMM
                return _cap((sp2 - nxt) / sp2 - COMM)
            if hi >= prev * (1 + trigger):
                # 正T: 卖sp → 买回bp; 止损=买回价上移stop
                sp2 = prev * (1 + trigger); bp = sp2 * (1 - settle)
                stop_p = sp2 * (1 + stop)
                if stop > 0 and lo <= bp and hi >= stop_p:
                    w = 0.5
                    if stop_adj == 'close':
                        w = 1.0 if cl >= stop_p else (0.0 if cl <= bp else 0.5)
                    cnt['settle'] += 1 - w; cnt['stop'] += w
                    v = (1 - w) * (settle - COMM) + w * (-stop - COMM)
                    pnls.append(v); rec.append((idx[i], v))
                elif lo <= bp:
                    if (cl <= bp) or (not middle):
                        cnt['settle'] += 1
                        v = settle - COMM
                        pnls.append(v); rec.append((idx[i], v))
                    else:
                        cnt['settle'] += 0.5
                        v = 0.5 * (settle - COMM) + 0.5 * force(0.5)
                        pnls.append(v); rec.append((idx[i], v))
                elif stop > 0 and hi >= stop_p:
                    cnt['stop'] += 1
                    v = -stop - COMM
                    pnls.append(v); rec.append((idx[i], v))
                else:
                    v = force()
                    pnls.append(v); rec.append((idx[i], v))
            elif lo <= prev * (1 - trigger):
                # 反T: 买bp → 卖sp; 止损=卖出价下移stop
                bp = prev * (1 - trigger); sp2 = bp * (1 + settle)
                stop_p = bp * (1 - stop)
                def force(w=1.0):
                    if tail_close:
                        cnt['force_tail'] += w
                        return _cap((cl - bp) / bp - COMM)
                    cnt['force_next'] += w
                    nxt = o[i+1] if i+1 < n else c[i]
                    if nxt <= 0:
                        return -COMM
                    return _cap((nxt - bp) / bp - COMM)
                if stop > 0 and hi >= sp2 and lo <= stop_p:
                    w = 0.5
                    if stop_adj == 'close':
                        w = 1.0 if cl <= stop_p else (0.0 if cl >= sp2 else 0.5)
                    cnt['settle'] += 1 - w; cnt['stop'] += w
                    v = (1 - w) * (settle - COMM) + w * (-stop - COMM)
                    pnls.append(v); rec.append((idx[i], v))
                elif hi >= sp2:
                    if (cl >= sp2) or (not middle):
                        cnt['settle'] += 1
                        v = settle - COMM
                        pnls.append(v); rec.append((idx[i], v))
                    else:
                        cnt['settle'] += 0.5
                        v = 0.5 * (settle - COMM) + 0.5 * force(0.5)
                        pnls.append(v); rec.append((idx[i], v))
                elif stop > 0 and lo <= stop_p:
                    cnt['stop'] += 1
                    v = -stop - COMM
                    pnls.append(v); rec.append((idx[i], v))
                else:
                    v = force()
                    pnls.append(v); rec.append((idx[i], v))
    return np.array(pnls), cnt, rec

variants = [
    ('D 基线(次日开盘强平)', dict()),
    ('A 趋势过滤(当前实盘)', dict(gap_filter=0.01)),
    ('E 尾盘了结(收盘价)', dict(tail_close=True)),
    ('A+E 过滤+尾盘', dict(gap_filter=0.01, tail_close=True)),
    ('G05 止损0.5%(次日兜底)', dict(stop=0.005)),
    ('G1 止损1%(次日兜底)', dict(stop=0.01)),
    ('G15 止损1.5%(次日兜底)', dict(stop=0.015)),
    ('A+G05 过滤+止损0.5%', dict(gap_filter=0.01, stop=0.005)),
    ('A+G1 过滤+止损1%', dict(gap_filter=0.01, stop=0.01)),
    ('A+G15 过滤+止损1.5%', dict(gap_filter=0.01, stop=0.015)),
    ('A+E+G1 过滤+尾盘+止损1%', dict(gap_filter=0.01, tail_close=True, stop=0.01)),
    ('── 敏感性: 收盘判读 ──', None),
    ('A+G05 close判读', dict(gap_filter=0.01, stop=0.005, stop_adj='close')),
    ('A+G1 close判读', dict(gap_filter=0.01, stop=0.01, stop_adj='close')),
    ('A+E+G1 close判读', dict(gap_filter=0.01, tail_close=True, stop=0.01, stop_adj='close')),
]

print(f'\n{"方案":28}{"笔数":>7}{"闭环":>7}{"隔夜":>7}{"尾盘":>7}{"止损":>7}{"每笔均值":>9}{"胜率":>7}{"总增厚":>9}')
print('=' * 92)
results = {}
for name, kw in variants:
    if kw is None:
        print(); continue
    p, cnt, rec = simulate(**kw)
    if len(p) == 0:
        print(f'{name:28} 无交易'); continue
    tot = len(p)
    results[name] = (p, rec)
    print(f'{name:28}{tot:>7}{cnt["settle"]/tot:>6.1%}{cnt["force_next"]/tot:>6.1%}'
          f'{cnt["force_tail"]/tot:>6.1%}{cnt["stop"]/tot:>6.1%}'
          f'{p.mean()*100:>+8.3f}%{(p>0).mean():>6.1%}{p.sum()*100:>+8.0f}%')

print()
print('── 分布(每笔%): min / p1 / p5 / p50 / p95 / max ──')
for name in ['D 基线(次日开盘强平)', 'A 趋势过滤(当前实盘)', 'A+E 过滤+尾盘',
             'G1 止损1%(次日兜底)', 'A+G1 过滤+止损1%', 'A+E+G1 过滤+尾盘+止损1%']:
    p, _ = results[name]
    q = np.percentile(p, [0, 1, 5, 50, 95, 100])
    print(f'  {name:24} ' + ' '.join(f'{x*100:+.2f}' for x in q))

print()
print('── 逐年每笔均值% (归因闸门: 优势是否集中于个别年份) ──')
years = ['2019','2020','2021','2022','2023','2024','2025','2026']
hdr = f'  {"方案":24}' + ''.join(f'{y:>8}' for y in years)
print(hdr)
for name in ['D 基线(次日开盘强平)', 'A 趋势过滤(当前实盘)', 'G1 止损1%(次日兜底)',
             'A+G1 过滤+止损1%', 'A+E+G1 过滤+尾盘+止损1%']:
    p, rec = results[name]
    row = f'  {name:24}'
    for y in years:
        m = np.array([v for d, v in rec if str(d)[:4] == y])
        row += f'{m.mean()*100:+8.3f}' if len(m) else f'{"-":>8}'
    print(row)

print()
print('── 极端事件归因: 剔除最差N笔后 A+G1 每笔均值 ──')
p_ag1, rec_ag1 = results['A+G1 过滤+止损1%']
p_a, _ = results['A 趋势过滤(当前实盘)']
for drop in [0, 1, 2, 5, 10, 50, 100]:
    s = np.sort(p_ag1)
    kept = s[:-drop] if drop else s
    print(f'  A+G1 剔除最差{drop:>3}笔: 均值{kept.mean()*100:+.3f}%  剩余{len(kept)}笔')
s_a = np.sort(p_a)
for drop in [10, 100]:
    kept = s_a[:-drop]
    print(f'  A   剔除最差{drop:>3}笔: 均值{kept.mean()*100:+.3f}%  剩余{len(kept)}笔')
# 最差10笔的日期/方向归因
worst = sorted(rec_ag1, key=lambda x: x[1])[:10]
print('  A+G1 最差10笔:')
for d, v in worst:
    print(f'    {str(d)[:10]}  {v*100:+.2f}%')

print()
print('── 乐观模型参照(摸到价即当日闭环) ──')
for name, kw in [('D 基线乐观', dict(middle=False)), ('A 过滤乐观', dict(gap_filter=0.01, middle=False)),
                 ('E 尾盘乐观', dict(tail_close=True, middle=False)),
                 ('A+G1 乐观', dict(gap_filter=0.01, stop=0.01, middle=False)),
                 ('A+E+G1 乐观', dict(gap_filter=0.01, tail_close=True, stop=0.01, middle=False))]:
    p, cnt, rec = simulate(**kw)
    print(f'  {name:22}{len(p):>7}笔 每笔{p.mean()*100:+.3f}% 胜率{(p>0).mean():.1%} 总增厚{p.sum()*100:+.0f}%')
