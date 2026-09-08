# -*- coding: utf-8 -*-
"""
pTrade 100万回测产物审计 (2026-09-08)
闸门三项(量纲/连续性/身份) + 成交价语义 + 费率结构 + 5个月窗口盈亏 + 基准身份。
产物只有 2019-02-18~2019-07-22 切片(导出被截断), 全期结论只能靠此切片+用户粘贴的日志尾部推断。
"""
import glob
import pandas as pd
import numpy as np

DIR = '/root/quant/data_store/ptrade_backtest_1m'
trades = pd.read_csv(f'{DIR}/交易详情20260908082036.csv', encoding='gbk')
trades.columns = ['date', 'time', 'code', 'side', 'openclose', 'qty', 'price', 'fee']
trades['date'] = pd.to_datetime(trades['date'])
trades['code6'] = trades['code'].str[:6]
trades['value'] = trades['qty'] * trades['price']

print(f'交易笔数 {len(trades)}  日期 {trades.date.min():%Y-%m-%d} ~ {trades.date.max():%Y-%m-%d}')
print(f'买卖分布: {trades.side.value_counts().to_dict()}')
print(f'年分布: {trades.date.dt.year.value_counts().to_dict()}')
print(f'月分布: {trades.date.dt.to_period("M").value_counts().sort_index().to_dict()}')

# ── 本地行情对照 ──────────────────────────────────────────────
codes = sorted(trades['code6'].unique())
local = {}
missing = []
for c in codes:
    p = f'/root/quant/data_store/daily/2019/{c}.parquet'
    try:
        df = pd.read_parquet(p)
        df['date'] = pd.to_datetime(df['date'])
        need = ['open', 'high', 'low', 'close']
        if '前收盘（元）' in df.columns:
            df = df.rename(columns={'前收盘（元）': 'prev_close'})
        else:
            df = df.sort_values('date').reset_index(drop=True)
            df['prev_close'] = df['close'].shift(1)
        local[c] = df[need + ['prev_close', 'date']].set_index('date')
    except Exception:
        missing.append(c)
print(f'本地行情缺失代码: {missing}')

rows = []
for _, t in trades.iterrows():
    df = local.get(t['code6'])
    if df is None or t['date'] not in df.index:
        continue
    r = df.loc[t['date']]
    prev_close = float(r['prev_close'])
    if pd.isna(prev_close) or prev_close <= 0:
        continue
    lim_up = round(prev_close * 1.10, 2)
    lim_dn = round(prev_close * 0.90, 2)
    fill = float(t['price'])
    rows.append({
        'fill_vs_close_bp': (fill / float(r['close']) - 1) * 1e4,
        'fill_vs_open_bp': (fill / float(r['open']) - 1) * 1e4,
        'at_limit_up': abs(fill - lim_up) < 0.005,
        'at_limit_dn': abs(fill - lim_dn) < 0.005,
        'is_buy': t['side'] == '买',
        'value': t['value'],
    })
aud = pd.DataFrame(rows)
print(f'可对照笔数: {len(aud)} / {len(trades)}')
print('\n── 成交价语义（成交价 vs 本地日线）──')
print(f'成交价=收盘价 的笔数: {(aud.fill_vs_close_bp.abs() < 1).sum()} / {len(aud)}')
print(f'成交价 vs 收盘 偏离(bp): 中位 {aud.fill_vs_close_bp.median():.1f} 均值 {aud.fill_vs_close_bp.mean():.1f}')
print(f'成交价 vs 开盘 偏离(bp): 中位 {aud.fill_vs_open_bp.median():.1f}')
print(f'按涨停价成交的买单: {((aud.at_limit_up) & (aud.is_buy)).sum()}')
print(f'按跌停价成交的卖单: {((aud.at_limit_dn) & (~aud.is_buy)).sum()}')
print(f'按涨停价成交的卖单(荒谬): {((aud.at_limit_up) & (~aud.is_buy)).sum()}')
print(f'按跌停价成交的买单(荒谬): {((aud.at_limit_dn) & (aud.is_buy)).sum()}')

# ── 费率结构 ──────────────────────────────────────────────────
trades['fee_bp'] = trades['fee'] / trades['value'] * 1e4
print('\n── 费率结构 ──')
for side, grp in trades.groupby('side'):
    print(f'{side}: 笔数{len(grp)} 费率(bp) 中位{grp.fee_bp.median():.2f} 均值{grp.fee_bp.mean():.2f} '
          f'min{grp.fee_bp.min():.2f} max{grp.fee_bp.max():.2f} 单笔费中位{grp.fee.median():.2f}元')
    print(f'  常见费率值: {grp.fee_bp.round(2).value_counts().head(6).to_dict()}')
    print(f'  单笔费==5.00元的笔数: {(grp.fee.round(2) == 5.0).sum()}')

# ── 5个月窗口盈亏（已实现 + 未实现，不含现金收益）──
print('\n── 2019-02-18~2019-07-22 切片盈亏 ──')
sells = trades[trades.side == '卖'].copy()
buys = trades[trades.side == '买'].copy()
realized = 0.0
fees = trades['fee'].sum()
# 用持仓明细的最后一天最新价算未实现
hold = pd.read_csv(f'{DIR}/持仓明细20260908082032.csv', encoding='gbk')
hold.columns = ['date', 'time', 'code', 'last_px', 'qty', 'longshort', 'cost', 'mv', 'pnl']
hold['date'] = pd.to_datetime(hold['date'])
last_day = hold[hold.date == hold.date.max()]
print(f'最后持仓日: {hold.date.max():%Y-%m-%d} 持仓 {len(last_day)} 只 累计盈亏合计 {last_day.pnl.sum():,.0f} 元')
print(f'全窗口手续费合计: {fees:,.0f} 元')
print(f'最后一天持仓市值合计: {last_day.mv.sum():,.0f} 元')

# 已实现盈亏: 每个代码 卖总额 - 买总额（近似，因买卖量可能不等）
realized_pnl = 0.0
for c in codes:
    b = buys[buys.code6 == c]['value'].sum()
    s = sells[sells.code6 == c]['value'].sum()
    realized_pnl += s - b
print(f'买卖额差(近似已实现毛盈亏, 未扣除在途仓位变化): {realized_pnl:,.0f} 元')

# ── 基准身份 ──────────────────────────────────────────────────
idx = pd.read_parquet('/root/quant/data_store/meta/csi800_index.parquet')
idx['date'] = pd.to_datetime(idx['date'])
i0 = idx[idx.date == '2019-01-02']['close'].iloc[0]
i1 = idx[idx.date == '2026-08-31']['close'].iloc[0]
print(f'\n── 基准身份(000906 官方序列) ──')
print(f'2019-01-02={i0:.1f} → 2026-08-31={i1:.1f} 涨幅 {(i1/i0-1)*100:+.2f}% (pTrade报告 53.62%)')
w0 = idx[idx.date == '2019-02-18']['close'].iloc[0]
w1 = idx[idx.date == '2019-07-22']['close'].iloc[0]
print(f'切片窗口 2019-02-18~2019-07-22 指数: {(w1/w0-1)*100:+.2f}%')

# ── 非收盘成交深入 ────────────────────────────────────────────
print('\n── 偏离收盘的成交 (|偏离|>1bp) ──')
off = aud[aud.fill_vs_close_bp.abs() > 1].copy()
print(f'共 {len(off)} 笔: 偏离分布 min{off.fill_vs_close_bp.min():.0f}bp '
      f'p25{off.fill_vs_close_bp.quantile(.25):.0f} 中位{off.fill_vs_close_bp.median():.0f} '
      f'p75{off.fill_vs_close_bp.quantile(.75):.0f} max{off.fill_vs_close_bp.max():.0f}bp')

# ── 持仓明细结构 ──────────────────────────────────────────────
print('\n── 持仓明细结构 ──')
hold['code6'] = hold['code'].str[:6]
per_day = hold.groupby('date').agg(n=('code6', 'count'), mv=('mv', 'sum'))
print(per_day.head(8).to_string())
print('...')
print(per_day.tail(8).to_string())
print(f'日均持仓 {per_day.n.mean():.0f} 只, 中位 {per_day.n.median():.0f}, 最大 {per_day.n.max()}')

# ── 选股身份: 成交额TOP验证 (量纲先自检) ─────────────────────
print('\n── 选股身份抽查（成交额排名, 先做量纲自检）──')
import collections
amt_rank_all = {}
for c in local:
    try:
        a = pd.read_parquet(f'/root/quant/data_store/daily/2019/{c}.parquet')
        a['date'] = pd.to_datetime(a['date'])
        a = a.set_index('date')
        amt_rank_all[c] = a
    except Exception:
        pass
for d in ['2019-02-18', '2019-04-01', '2019-06-03']:
    dt = pd.to_datetime(d)
    day_trades = trades[trades.date == dt]
    amts = {}
    unit_flag = 0
    for c, a in amt_rank_all.items():
        if dt not in a.index:
            continue
        r = a.loc[dt]
        if pd.isna(r.get('amount')) or pd.isna(r.get('close')):
            continue
        # 量纲自检: amount/volume ≈ 均价 ∈ [low, high] (元口径) 或 ×10000 偏离(万元/万股)
        try:
            apx = float(r['amount']) / float(r['volume'])
            if not (float(r['low']) * 0.8 <= apx <= float(r['high']) * 1.2):
                apx /= 1e4
                if not (float(r['low']) * 0.8 <= apx <= float(r['high']) * 1.2):
                    continue  # 量纲混乱, 弃用
                amts[c] = float(r['amount'])  # 万元口径
            else:
                amts[c] = float(r['amount'])
        except Exception:
            continue
    if not amts:
        print(f'{d}: 无可排名数据')
        continue
    rank = pd.Series(amts).sort_values(ascending=False)
    bought = day_trades[day_trades.side == '买']['code6'].tolist()
    ranks = [list(rank.index).index(c) + 1 for c in bought if c in rank.index]
    print(f'{d}: 买入{len(bought)}只, 成交额排名 中位{np.median(ranks):.0f} 范围{min(ranks)}-{max(ranks)} '
          f'(可排名{len(rank)}只) 未入榜{len(bought)-len(ranks)}只')

# ── 基准边界敏感性 ────────────────────────────────────────────
print('\n── 基准边界敏感性 ──')
for d0, d1 in [('2019-01-02', '2026-08-31'), ('2019-01-02', '2026-08-28'),
               ('2019-01-03', '2026-08-31'), ('2018-12-28', '2026-08-31')]:
    try:
        v0 = float(idx[idx.date == d0]['close'].iloc[0])
        v1 = float(idx[idx.date == d1]['close'].iloc[0])
        print(f'{d0}→{d1}: {(v1/v0-1)*100:+.2f}%')
    except Exception:
        print(f'{d0}→{d1}: 无数据')

# ── 2019 切片逐月对照 ─────────────────────────────────────────
print('\n── 切片逐月: 策略买卖额 vs 指数 ──')
trades['ym'] = trades.date.dt.to_period('M')
for ym, grp in trades.groupby('ym'):
    b = grp[grp.side == '买']['value'].sum()
    s = grp[grp.side == '卖']['value'].sum()
    print(f'{ym}: 买{b:>12,.0f} 卖{s:>12,.0f} 净{ (b-s):>12,.0f}')
