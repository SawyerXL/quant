"""Phase 4 参数扫描"""
import sys, os
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import numpy as np, pandas as pd
from loguru import logger; logger.remove(); logger.add(sys.stderr, level='ERROR')

from data.storage import load_meta
from run_backtest_a2 import _make_rebal_dates
from run_backtest_a import load_panels, calc_metrics, BACKTEST_START
from run_backtest_a4 import run_backtest_a4

import run_backtest_a as a_mod
import run_backtest_a3 as a3_mod
import run_backtest_a4 as a4_mod

def pct(s):
    return float(str(s).strip('%')) / 100

cal = load_meta('trade_calendar')
end = sorted([d for d in cal['trade_date'] if d <= '2026-12-31'])[-1]
calendar = [d for d in cal['trade_date'] if BACKTEST_START <= d <= end]
print('Loading data...')
c800 = load_meta('csi800')
panel, ap = load_panels(sorted(c800['code']), BACKTEST_START, end)
si = load_meta('stock_info_full'); si = None if si.empty else si
idx = load_meta('csi800_index')
idx_c = idx.set_index('date')['close'].sort_index() if not idx.empty else None
d = _make_rebal_dates(calendar, 'biweekly')

orig_ps = a_mod.PERIOD_STOP
orig_ts = a_mod.TRAILING_STOP
orig_rbt = a_mod.REGIME_BEAR_THR
orig_rbl = a_mod.REGIME_BULL_THR
orig_gt = a3_mod.GRACE_THRESHOLD
orig_ma = a4_mod.MA10_EXIT_DAYS

def go():
    nav = run_backtest_a4(panel, d, ap, idx_c, si)
    m = calc_metrics(nav)
    return pct(m['年化收益率']), float(m['夏普比率']), pct(m['最大回撤'])

bl_ar, bl_sr, bl_dd = go()
print('Baseline: ann=%+.1f%%  sharpe=%.2f  maxdd=%.1f%%' % (bl_ar*100, bl_sr, bl_dd*100))
print()

for label, set_fn, vals in [
    ('PERIOD_STOP', lambda v: setattr(a_mod, 'PERIOD_STOP', v), [-0.10, -0.12, -0.20, -0.25]),
    ('TRAILING_STOP', lambda v: setattr(a_mod, 'TRAILING_STOP', v), [-0.12, -0.15, -0.22, -0.25]),
    ('REGIME(bear/bull)', lambda v: (setattr(a_mod, 'REGIME_BEAR_THR', v[0]), setattr(a_mod, 'REGIME_BULL_THR', v[1])), [(0.96,1.04), (0.97,1.03)]),
    ('GRACE_THRESHOLD', lambda v: setattr(a3_mod, 'GRACE_THRESHOLD', v), [0.10, 0.20, 0.25]),
    ('MA10_EXIT_DAYS', lambda v: setattr(a4_mod, 'MA10_EXIT_DAYS', v), [2, 4, 5]),
]:
    print('--- %s ---' % label)
    for v in vals:
        set_fn(v)
        ar, sr, dd = go()
        delta = ar - bl_ar
        if isinstance(v, tuple):
            vstr = '%s/%s' % v
        elif isinstance(v, float):
            vstr = '%+.0f%%' % (v*100)
        else:
            vstr = str(v)
        print('  %-12s ann=%+.1f%%  sr=%.2f  dd=%.1f%%  delta=%+.1f%%' % (vstr, ar*100, sr, dd*100, delta*100))
    print()

# restore
a_mod.PERIOD_STOP = orig_ps
a_mod.TRAILING_STOP = orig_ts
a_mod.REGIME_BEAR_THR = orig_rbt
a_mod.REGIME_BULL_THR = orig_rbl
a3_mod.GRACE_THRESHOLD = orig_gt
a4_mod.MA10_EXIT_DAYS = orig_ma
print('Done.')
