"""
价值因子增强 A/B: A-4基线(value_weight=0) vs A-4+价值(0.15)。
同一引擎 run_backtest_a4 跑两遍, 唯一变量=价值倾斜(monkey-patch打分函数)。
基线须复现已知 A-4(~14.6%)作自检; 只在OOS改善才考虑采纳。
"""
import os, sys; from pathlib import Path
sys.path.insert(0, '/root/quant'); sys.path.insert(0, '/root/quant/scripts')
import pandas as pd
from datetime import datetime
import run_backtest_a4 as a4
from run_backtest_a2 import compute_score_a2 as real_score, _make_rebal_dates
from run_backtest_a import load_panels, calc_metrics, BACKTEST_START
from data.storage import load_meta

VALUE_W = float(os.getenv("VALUE_W", "0.15"))
t0 = datetime.now(); print(f"开始: {t0:%H:%M:%S}", flush=True)

cal = load_meta("trade_calendar")
END = [d for d in sorted(cal["trade_date"].tolist()) if d <= "2026-12-31"][-1]
calendar = [d for d in cal["trade_date"].tolist() if BACKTEST_START <= d <= END]
rebal = _make_rebal_dates(calendar, "biweekly")
codes = sorted(load_meta("csi800")["code"].tolist())
panel, ap = load_panels(codes, BACKTEST_START, END)
info = load_meta("stock_info_full"); info = None if info.empty else info
idx = load_meta("csi800_index")
if idx.empty:
    index_close = None
else:
    idx["date"] = pd.to_datetime(idx["date"]); index_close = idx.set_index("date")["close"].sort_index()
print(f"数据: {panel.shape[0]}天×{panel.shape[1]}只 调仓{len(rebal)} {BACKTEST_START}~{END}", flush=True)

def m(nav):
    full = calc_metrics(nav)
    oos = nav[nav.index >= "2024-01-01"]
    o = calc_metrics(oos) if len(oos) > 2 else {}
    def g(d, k):
        try: return float(str(d.get(k, "0")).strip("%"))
        except: return 0.0
    return (g(full, "年化收益率"), g(full, "夏普比率"), g(full, "最大回撤"),
            g(o, "年化收益率"), g(o, "夏普比率"), g(o, "最大回撤"))

# 基线: value_weight=0 (与现有A-4代码路径完全一致)
a4.compute_score_a2 = real_score
print("跑基线 A-4...", flush=True)
nav_b = a4.run_backtest_a4(panel, rebal, ap, index_close, info)
b = m(nav_b)

# 变体: value_weight=VALUE_W
a4.compute_score_a2 = lambda pn, dt, amt, inf: real_score(pn, dt, amt, inf, value_weight=VALUE_W)
print(f"跑变体 A-4+价值({VALUE_W})...", flush=True)
nav_v = a4.run_backtest_a4(panel, rebal, ap, index_close, info)
v = m(nav_v)

print(f"\n{'='*72}\n  价值因子增强 A/B (CSI800, 双周, 按引擎正确记账)\n{'='*72}")
print(f"  {'变体':<18}{'全期年化':>9}{'全期夏普':>9}{'全期回撤':>9}{'OOS年化':>9}{'OOS夏普':>9}{'OOS回撤':>9}")
print(f"  {'-'*70}")
print(f"  {'A-4 基线':<18}{b[0]:>+8.1f}%{b[1]:>9.2f}{b[2]:>8.1f}%{b[3]:>+8.1f}%{b[4]:>9.2f}{b[5]:>8.1f}%")
print(f"  {'A-4+价值'+str(VALUE_W):<18}{v[0]:>+8.1f}%{v[1]:>9.2f}{v[2]:>8.1f}%{v[3]:>+8.1f}%{v[4]:>9.2f}{v[5]:>8.1f}%")
print(f"  {'-'*70}")
print(f"  Δ OOS年化 {v[3]-b[3]:+.1f}pp | Δ OOS夏普 {v[4]-b[4]:+.2f} | Δ OOS回撤 {v[5]-b[5]:+.1f}pp")
chk = "✅" if 10 <= b[0] <= 20 else "⚠️偏离Track A已知~14.6%"
print(f"  基线自检: 全期年化{b[0]:+.1f}% {chk}")
print(f"{'='*72}\n耗时{(datetime.now()-t0).seconds}s")
