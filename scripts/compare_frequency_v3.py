"""
频率对比 v3：干净四维验证（抑制debug日志）
"""
import os, sys
sys.path.insert(0, str(__file__).rsplit('/', 2)[0])
import numpy as np, pandas as pd
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from data.storage import load_meta
from run_backtest_a2 import _make_rebal_dates
from run_backtest_a import load_panels, calc_metrics, BACKTEST_START
from run_backtest_a4 import run_backtest_a4
from run_backtest_a4_fixed import run_backtest_a4_fixed

def parse(s):
    return float(str(s).strip("%")) / 100

cal = load_meta("trade_calendar")
end = sorted([d for d in cal["trade_date"] if d <= "2026-12-31"])[-1]
calendar = [d for d in cal["trade_date"] if BACKTEST_START <= d <= end]

c800 = load_meta("csi800")
panel, ap = load_panels(sorted(c800["code"]), BACKTEST_START, end)

si = load_meta("stock_info_full"); si = None if si.empty else si
idx = load_meta("csi800_index")
idx_c = None if idx.empty else pd.to_datetime(idx["date"]).pipe(
    lambda d: idx.set_index("date")["close"].sort_index() if not idx.empty else None)

def run(fn, freq):
    d = _make_rebal_dates(calendar, freq)
    nav = fn(panel, d, ap, idx_c, si)
    m = calc_metrics(nav)
    return {"n": len(d), "ar": parse(m["年化收益率"]),
            "sr": float(m["夏普比率"]), "dd": parse(m["最大回撤"]),
            "vol": parse(m["年化波动率"])}

print("=" * 75)
print("TEST 1: Bug影响量化 — 原始版 vs 修复版 (biweekly)")
print("=" * 75)
orig = run(run_backtest_a4, "biweekly")
fixd = run(run_backtest_a4_fixed, "biweekly")
print(f"  原始(bug): 年化={orig['ar']:+.1%}  夏普={orig['sr']:.2f}  回撤={orig['dd']:.1%}  波动={orig['vol']:.1%}")
print(f"  修复(fix): 年化={fixd['ar']:+.1%}  夏普={fixd['sr']:.2f}  回撤={fixd['dd']:.1%}  波动={fixd['vol']:.1%}")
print(f"  Bug虚增:   年化{(orig['ar']-fixd['ar']):+.1%}  夏普{(orig['sr']-fixd['sr']):+.2f}")

print()
print("=" * 75)
print("TEST 2: 修复版频率对比 — monthly vs biweekly vs weekly")
print("=" * 75)
for freq in ["monthly", "biweekly", "weekly"]:
    r = run(run_backtest_a4_fixed, freq)
    yr = (int(end[:4]) - int(BACKTEST_START[:4]) + 1)
    print(f"  {freq:10s}: 年化={r['ar']:+.1%}  夏普={r['sr']:.2f}  回撤={r['dd']:.1%}  "
          f"波动={r['vol']:.1%}  调仓={r['n']}次(年均{r['n']/yr:.0f})")

print()
print("=" * 75)
print("TEST 3: OOS验证 (修复版) — Train 2019-2022 / Test 2023-2026")
print("=" * 75)
cal_tr = [d for d in calendar if d <= "2022-12-31"]
cal_te = [d for d in calendar if d >= "2023-01-01"]
for freq in ["biweekly", "weekly"]:
    tr_d = _make_rebal_dates(cal_tr, freq)
    te_d = _make_rebal_dates(cal_te, freq)
    tr_nav = run_backtest_a4_fixed(panel, tr_d, ap, idx_c, si)
    te_nav = run_backtest_a4_fixed(panel, te_d, ap, idx_c, si)
    tr_m = calc_metrics(tr_nav)
    te_m = calc_metrics(te_nav)
    print(f"  {freq:10s}: Train 年化={parse(tr_m['年化收益率']):+.1%}  夏普={float(tr_m['夏普比率']):.2f}  |  "
          f"Test 年化={parse(te_m['年化收益率']):+.1%}  夏普={float(te_m['夏普比率']):.2f}  "
          f"回撤={parse(te_m['最大回撤']):.1%}")

print()
print("=" * 75)
print("结论")
print("=" * 75)
