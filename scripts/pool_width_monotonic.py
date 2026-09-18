"""宇宙宽度单调性测量 (2026-09-18, 登记见账本"宇宙放宽方向登记", 预算 3→2)。

效应(W) = A1c_W − 全市场等权臂("无选择"零点, 替代 A0——A0 校准残差
会污染差分)。W ∈ {30, 100, 300, 全市场}。每票预算固定 2 万
(capital = W×2万) 防佣金地板成本伪单调。判据(预锁): 效应(W) 随 W
单调向 0 → 支持"排序轴负 alpha"; 非单调 → 噪声, 不据此选档。
用法: python scripts/pool_width_monotonic.py 10 > logs/pool_width_20260918.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from backtest_return_attribution import BASE, A0_OV
from s1_attribution_clean import COMPONENTS_OFF
from smoke_t1_open import build_panels, START, END

PATHS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
PER_STOCK = 20_000          # 每票 2万: 佣金地板不触发(登记锁死)
WIDTHS = [30, 100, 300, 0]  # 0 = 全市场(面板全宽)
YEARS = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25


def main():
    panel, open_panel, ap, cal = build_panels()
    n_all = panel.shape[1]
    widths = [w if w > 0 else n_all for w in WIDTHS]
    base_dates = [d for d in make_rebal_dates(cal, "biweekly")
                  if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}
    paths = []
    for off in range(PATHS):
        shifted = [cal[idx.get(d, 0) + off] for d in base_dates
                   if idx.get(d, 0) + off < len(cal)]
        paths.append([d for d in shifted if START <= d <= END])

    results = {}
    for w_req, w in zip(WIDTHS, widths):
        anns, rets = [], []
        for dates in paths:
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                    **A0_OV, **COMPONENTS_OFF,
                                    "pool_style": "amount", "pool_size": w,
                                    "initial_capital": w * PER_STOCK,
                                    "commission": 0.0013})
            nav, info = run_backtest(panel, ap, dates, cfg)
            nav = nav[nav.index >= pd.Timestamp(START)]
            anns.append(float(calc_metrics(nav)["年化_float"]))
            rets.append(nav.pct_change().dropna())
        ens = (1 + pd.concat(rets, axis=1).dropna().mean(axis=1)).cumprod()
        results[w_req] = (float(np.mean(anns)),
                          float(calc_metrics(ens)["年化_float"]))
        label = f"W={w_req}" if w_req else f"全市场({n_all}只)"
        print(f"{label}: 路径均值 {np.mean(anns)*100:+.2f}% "
              f"摊平 {calc_metrics(ens)['年化_float']*100:+.2f}%", flush=True)

    # 效应(W) = A1c_W − 全市场等权
    full_ann = results[0][1]
    effects = {w: (results[w][1] - full_ann) * 100 for w in WIDTHS}
    print("\n效应(W) = A1c_W − 全市场等权 (摊平口径):")
    for w in WIDTHS:
        print(f"  W={w if w else '全市场'}: {effects[w]:+.2f}pp")
    # 单调性判读(预锁): 效应(30)<效应(100)<效应(300)<0=效应(全市场)
    seq = [effects[30], effects[100], effects[300], effects[0]]
    strictly_increasing = all(b > a for a, b in zip(seq, seq[1:]))
    all_neg_before_zero = all(e < 0 for e in seq[:3])
    if strictly_increasing and all_neg_before_zero:
        verdict = "单调向0 → 支持排序轴负alpha解释, 取算术允许的最宽"
    else:
        verdict = ("非单调 → 噪声或别的东西, 不据此选档"
                   f"(序列: {['%+.2f' % e for e in seq]})")
    print(f"判读(预锁): {verdict}")


if __name__ == "__main__":
    main()
