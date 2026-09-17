"""择时臂换手纯计数 (2026-09-17, 判据预锁见账本"择时换手纯计数判据锁")。

读引擎新增诊断 rebal_events（纯记录不改行为），对 A2c（择时开）逐次
调仓按 tier 跃迁分类：bull→bear / bear→bull / 档间切换 / 档内微调，
各组算平均换手((enter+exit)/2)与年频次，年化潜在节省=频次×换手×5bp，
对照锁死 Z=0.5pp。判据先于数据，出数后不许换 impact 假设。
用法: python scripts/tier_turnover_count.py 10 > logs/tier_turnover_20260917.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates
from backtest_return_attribution import BASE, A0_OV, WINDOWS
from s1_attribution_clean import COMPONENTS_OFF
from smoke_t1_open import build_panels, START, END

PATHS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
IMPACT = 0.0005   # 分档执行可省的冲击成本/单边(判据锁死)
Z_PP = 0.5        # 年化节省门槛 pp(判据锁死)
YEARS = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25

A2C_OV = {**A0_OV, **COMPONENTS_OFF,
          "pool_style": "amount", "pool_size": 30,
          "initial_capital": 500_000.0}


def classify(prev_tier, tier):
    if prev_tier > 0.3 >= tier:
        return "bull→bear"
    if prev_tier <= 0.3 < tier:
        return "bear→bull"
    if abs(prev_tier - tier) > 1e-9:
        return "档间切换"
    return "档内微调"


def main():
    panel, open_panel, ap, cal = build_panels()
    ic = None
    from data.storage import load_meta
    ic = load_meta("csi800_index").set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    base_dates = [d for d in make_rebal_dates(cal, "biweekly")
                  if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}
    paths = []
    for off in range(PATHS):
        shifted = [cal[idx.get(d, 0) + off] for d in base_dates
                   if idx.get(d, 0) + off < len(cal)]
        paths.append([d for d in shifted if START <= d <= END])

    groups = {}
    for dates in paths:
        cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                **A2C_OV, "commission": 0.0013})
        nav, info = run_backtest(panel, ap, dates, cfg, ic)
        prev = None
        for ev in info["rebal_events"]:
            t = ev["tier"]
            if prev is None:
                prev = t
                continue
            key = classify(prev, t)
            to = (ev["enter_w"] + ev["exit_w"]) / 2
            groups.setdefault(key, []).append(to)
            prev = t

    print(f"主窗 {START}~{END} | {PATHS} 路径 | 调仓事件合计 "
          f"{sum(len(v) for v in groups.values())}")
    print(f"{'事件组':10s} {'次数':>5s} {'年频次':>7s} {'均换手':>7s} "
          f"{'年化潜在节省(5bp)':>14s} {'判读':>10s}")
    for key in ("bull→bear", "bear→bull", "档间切换", "档内微调"):
        v = groups.get(key, [])
        if not v:
            print(f"{key:10s} {'0':>5s}")
            continue
        freq = len(v) / YEARS / PATHS
        to = float(np.mean(v))
        saving_pp = freq * to * IMPACT * 100
        verdict = "有空间" if saving_pp > Z_PP else "不达标"
        print(f"{key:10s} {len(v):>5d} {freq:>7.2f} {to*100:>6.1f}% "
              f"{saving_pp:>13.2f}pp {verdict:>10s}")
    print(f"\n判据(预锁): 任一组年化节省 > {Z_PP}pp → 分档执行有收益空间; "
          f"全部≤{Z_PP}pp 且换手主要在档内微调 → 分档解决不了, 转档位平滑")


if __name__ == "__main__":
    main()
