"""分析持仓天数与胜率的关系"""
import sys, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from run_backtest_a2 import compute_score_a2, select_industry_balanced, _make_rebal_dates, N_HOLDINGS
from run_backtest_a import load_panels, BACKTEST_START
from data.storage import load_meta

BACKTEST_END = '2025-12-31'
cal_df = load_meta('trade_calendar')
calendar = [d for d in cal_df['trade_date'].tolist() if BACKTEST_START <= d <= BACKTEST_END]
rebal_dates = _make_rebal_dates(calendar)

panel, amount_panel = load_panels(
    sorted(load_meta('csi800')['code'].tolist()), BACKTEST_START, BACKTEST_END
)
stock_info = load_meta('stock_info_full')
stock_info = None if stock_info.empty else stock_info

hold_tracker, records, prev = {}, [], []

for d in rebal_dates:
    dt   = pd.Timestamp(d)
    hist = panel[panel.index <= dt]
    if len(hist) < 250:
        continue
    score = compute_score_a2(panel, dt, amount_panel, stock_info)
    if len(score) < N_HOLDINGS:
        continue
    selected = select_industry_balanced(score, stock_info, N_HOLDINGS, 8)
    prices   = hist.iloc[-1]

    for code in set(prev) - set(selected):
        entry = hold_tracker.pop(code, None)
        ep    = prices.get(code)
        if (entry and entry['price'] > 0
                and isinstance(ep, (int, float))
                and not pd.isna(ep) and ep > 0):
            days = (dt - entry['date']).days
            pnl  = float(ep) / entry['price'] - 1
            records.append({'days': days, 'pnl': pnl, 'win': pnl > 0})

    for code in set(selected) - set(prev):
        ep = prices.get(code)
        if isinstance(ep, (int, float)) and not pd.isna(ep) and ep > 0:
            hold_tracker[code] = {'date': dt, 'price': float(ep)}
    prev = selected

df = pd.DataFrame(records)

# ── 按持仓区间统计 ────────────────────────────────────────────────────────
buckets = [
    (0,   7,  '< 1周'),
    (7,   14, '1-2周'),
    (14,  21, '2-3周'),
    (21,  28, '3-4周'),
    (28,  42, '1-1.5月'),
    (42,  60, '1.5-2月'),
    (60,  90, '2-3月'),
    (90,  999,'> 3月'),
]

print('=' * 68)
print('  持仓天数 vs 胜率 / 期望收益  (策略A-2，2019-2025)')
print('=' * 68)
print(f"{'区间':<10} {'笔数':>5} {'胜率':>7} {'盈利均':>9} {'亏损均':>9} {'单笔期望':>9}")
print('-' * 68)

rows = []
for lo, hi, lbl in buckets:
    sub = df[(df['days'] >= lo) & (df['days'] < hi)]
    if len(sub) < 5:
        continue
    wr = sub['win'].mean()
    aw = sub.loc[sub['win'],  'pnl'].mean() if sub['win'].any()  else 0.0
    al = sub.loc[~sub['win'], 'pnl'].mean() if (~sub['win']).any() else 0.0
    ex = wr * aw + (1 - wr) * al
    rows.append((lbl, len(sub), wr, aw, al, ex))
    print(f"{lbl:<10} {len(sub):>5} {wr:>7.1%} {aw:>+9.1%} {al:>+9.1%} {ex:>+9.2%}")

print('-' * 68)
wr_all = df['win'].mean()
aw_all = df.loc[df['win'],  'pnl'].mean()
al_all = df.loc[~df['win'], 'pnl'].mean()
ex_all = wr_all * aw_all + (1 - wr_all) * al_all
print(f"{'全部':<10} {len(df):>5} {wr_all:>7.1%} {aw_all:>+9.1%} {al_all:>+9.1%} {ex_all:>+9.2%}")

# ── 三段比较 ──────────────────────────────────────────────────────────────
print()
print('── 三段汇总 ──')
for lbl2, mask in [('<2周', df['days'] < 14),
                   ('2-4周', (df['days'] >= 14) & (df['days'] < 28)),
                   ('>4周', df['days'] >= 28)]:
    sub  = df[mask]
    wr   = sub['win'].mean()
    aw   = sub.loc[sub['win'],  'pnl'].mean() if sub['win'].any()  else 0.0
    al   = sub.loc[~sub['win'], 'pnl'].mean() if (~sub['win']).any() else 0.0
    ex   = wr * aw + (1 - wr) * al
    pr   = aw / abs(al) if al != 0 else float('inf')
    print(f"  {lbl2:<5}  胜率={wr:.1%}  盈利均={aw:+.1%}  亏损均={al:+.1%}  "
          f"盈亏比={pr:.2f}  单笔期望={ex:+.2%}  ({len(sub)}笔)")

# ── 核心结论 ──────────────────────────────────────────────────────────────
print()
print('── 结论 ──')
s = df[df['days'] < 14]
m = df[(df['days'] >= 14) & (df['days'] < 28)]
l = df[df['days'] >= 28]
trend = 'UP' if s['win'].mean() > m['win'].mean() > l['win'].mean() else 'NO_CLEAR_TREND'
print(f"  胜率趋势: <2周={s['win'].mean():.1%} -> 2-4周={m['win'].mean():.1%} -> >4周={l['win'].mean():.1%}")
print(f"  结论：持仓越短，胜率{'越高（符合直觉）' if s['win'].mean() > l['win'].mean() else '不一定越高（注意！）'}")
