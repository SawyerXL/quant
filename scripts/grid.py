"""
阶段A — 过热过滤强度主轴扫描 (S1→S5)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import pandas as pd, numpy as np, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from scripts.backtest_config import STAGE_A_CONFIGS
from scripts.backtest_data import get_price_panel
from scripts.backtest_engine import run_backtest, calc_metrics, make_rebal_dates
from data.storage import load_meta

np.random.seed(42)

# ═══ Load data once ═══
print("=" * 70)
print("阶段A: 过热过滤强度扫描")
print("=" * 70)
base_cfg = STAGE_A_CONFIGS[0]
panel, ap = get_price_panel(base_cfg.start_date, base_cfg.end_date)
cal = load_meta('trade_calendar')
cal_dates = sorted([d for d in cal['trade_date'] if base_cfg.start_date <= d <= base_cfg.end_date])
rebal = make_rebal_dates(cal_dates, 'biweekly')
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None
print(f"面板: {panel.shape[0]}d × {panel.shape[1]}只 | 调仓: {len(rebal)}次\n")

# ═══ Run S1-S5 ═══
labels = ['S1','S2','S3','S4','S5']
results, navs = [], {}

for label, cfg in zip(labels, STAGE_A_CONFIGS):
    cfg.overheat_mode = "eliminate"
    desc = f"20d<{cfg.max_20d_return}% 连涨<{cfg.max_consec_up_days} 5d<{cfg.max_5d_return}% 距高>{cfg.min_dist_from_high}%"
    print(f"  {label}: {desc}", end=' ', flush=True)

    nav, info = run_backtest(panel, ap, rebal, cfg, idx_c)
    m = calc_metrics(nav)

    ann = m['年化_float'] * 100; dd = m['回撤_float'] * 100
    sr = m['夏普_float']; wr = m['胜率_float'] * 100
    trades = info['trades']
    msw = info['max_single_weight']
    t3c = info['top3_concentration']

    tag = '' if trades >= 30 else ' ⚠️样本不足'
    print(f"→ 年化{ann:+.1f}% 夏普{sr:.2f} 回撤{dd:+.1f}% max_w={msw:.1%} top3={t3c:.1%}{tag}")

    results.append({
        '档位': label, '描述': desc,
        '年化收益': f"{ann:.2f}%", '夏普比率': f"{sr:.2f}",
        '最大回撤': f"{dd:.2f}%", '胜率': f"{wr:.1f}%",
        '交易笔数': trades, '样本不足': 'Y' if trades < 30 else '',
        'max_single_weight': f"{msw:.1%}",
        'top3_concentration': f"{t3c:.1%}",
        '_ann': ann, '_dd': dd, '_sr': sr, '_msw': msw,
    })
    navs[label] = nav

df = pd.DataFrame(results)
df.to_csv('logs/stage_a_results.csv', index=False, encoding='utf-8-sig')
print(f"\n✅ CSV: logs/stage_a_results.csv")

# ═══ Self-check: S4/S5 should hit max_single cap ═══
print("\n── 自检: 集中度上限生效验证 ──")
s4_msw = df[df['档位']=='S4']['_msw'].values[0]
s5_msw = df[df['档位']=='S5']['_msw'].values[0]
pos_cap = base_cfg.max_position_pct
for lbl, msw in [('S4', s4_msw), ('S5', s5_msw)]:
    if msw >= pos_cap * 0.9:
        print(f"  {lbl} max_single_weight={msw:.1%} ✅ 触及{pos_cap:.0%}仓位上限, 集中度管控正常")
    else:
        print(f"  {lbl} max_single_weight={msw:.1%} ⚠️ (上限{pos_cap:.0%}, 可能过滤过严)")

# ═══ Scatter plot ═══
fig, ax = plt.subplots(figsize=(10, 6))
for _, r in df.iterrows():
    ax.scatter(r['_ann'], r['_dd'], s=140, zorder=5)
    ax.annotate(r['档位'], (r['_ann'], r['_dd']),
        textcoords="offset points", xytext=(10, 6), fontsize=13, fontweight='bold')
ax.set_xlabel('年化收益 (%)', fontsize=12)
ax.set_ylabel('最大回撤 (%)', fontsize=12)
ax.set_title('阶段A: 过热过滤强度 — 效率前沿', fontsize=14)
ax.axhline(y=0, color='gray', alpha=0.3)
ax.grid(True, alpha=0.3)
plt.tight_layout(); plt.savefig('logs/stage_a_scatter.png', dpi=150)
print("✅ 散点图: logs/stage_a_scatter.png")

# ═══ NAV overlay ═══
fig2, ax2 = plt.subplots(figsize=(12, 6))
colors = ['#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd']
for label, c in zip(labels, colors):
    ax2.plot(navs[label].index, navs[label].values, color=c, label=label, linewidth=1)
nav_ew = (1 + panel.mean(axis=1).pct_change().fillna(0)).cumprod()
ax2.plot(nav_ew.index, nav_ew.values, 'gray', linestyle='--', linewidth=1, alpha=0.5, label='CSI800 EW')
ax2.set_ylabel('净值'); ax2.set_title('阶段A: 净值曲线对比', fontsize=14)
ax2.legend(fontsize=9); ax2.grid(True, alpha=0.3)
plt.tight_layout(); plt.savefig('logs/stage_a_nav.png', dpi=150)
print("✅ 净值图: logs/stage_a_nav.png")

# ═══ Print table ═══
print(f"\n{'='*90}")
print("阶段A 结果对比表")
print(f"{'='*90}")
cols = ['档位','年化收益','夏普比率','最大回撤','胜率','交易笔数','max_single_weight','top3_concentration']
print(df[cols].to_string(index=False))
