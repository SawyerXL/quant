"""N=32 池臂月频重算 (2026-09-18, 检验账本占号已登记, 预算=否)。

对象: pool30 买持纯臂(A1c 配置)在 双周 vs 月频 调仓下的效应差。
判据(登记): 自身 TE 的 σ≥0.35 → 候选资格(非确认); σ 只报点值,
效应±SE。TE=月频非重叠对数差分样本σ×√12(与 single_var_harness
te_stats 同源方法, 唯一实现)。
用法: python scripts/pool_monthly_check.py 10 > logs/pool_monthly_20260918.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from backtest_config import BacktestConfig, DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from backtest_return_attribution import BASE, A0_OV, WINDOWS
from s1_attribution_clean import COMPONENTS_OFF
from smoke_t1_open import build_panels, START, END
from overlay_review_te import te_stats

PATHS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
A1C_OV = {**A0_OV, **COMPONENTS_OFF,
          "pool_style": "amount", "pool_size": 30,
          "initial_capital": 500_000.0}


def main():
    panel, open_panel, ap, cal = build_panels()
    years = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    results = {}
    for freq in ("biweekly", "monthly"):
        base_dates = [d for d in make_rebal_dates(cal, freq)
                      if START <= d <= END]
        idx = {d: i for i, d in enumerate(cal)}
        anns, rets, tos = [], [], []
        for off in range(PATHS):
            shifted = [cal[idx.get(d, 0) + off] for d in base_dates
                       if idx.get(d, 0) + off < len(cal)]
            dates = [d for d in shifted if START <= d <= END]
            cfg = BacktestConfig(**{**DEFAULT_CONFIG.to_dict(), **BASE,
                                    **A1C_OV, "commission": 0.0013})
            nav, info = run_backtest(panel, ap, dates, cfg)
            nav = nav[nav.index >= pd.Timestamp(START)]
            cm = calc_metrics(nav)
            anns.append(float(cm["年化_float"]))
            rets.append(nav.pct_change().dropna())
            tos.append(info["total_commission"] / 0.0013 / years)
        ens = (1 + pd.concat(rets, axis=1).dropna().mean(axis=1)).cumprod()
        ecm = calc_metrics(ens)
        results[freq] = {"ens": ens, "ann": ecm["年化_float"],
                         "sharpe": ecm["夏普_float"], "dd": ecm["回撤_float"],
                         "path_mean": float(np.mean(anns)),
                         "turnover": float(np.mean(tos))}
        print(f"{freq}: 路径均值{np.mean(anns)*100:+.2f}% "
              f"摊平{ecm['年化_float']*100:+.2f}% 夏普{ecm['夏普_float']:.2f} "
              f"回撤{ecm['回撤_float']*100:.2f}% 换手{np.mean(tos)*100:.0f}%/年",
              flush=True)
    te = te_stats(results["biweekly"]["ens"], results["monthly"]["ens"], years)
    sigma = te["TE_pct"] / 100
    diff_pp = (results["monthly"]["ann"] - results["biweekly"]["ann"]) * 100
    print(f"月频−双周: {diff_pp:+.2f}pp | TE={te['TE_pct']:.1f}% "
          f"SE=±{te['SE_pp']:.2f}pp | σ={sigma:.2f} (判据: ≥0.35=候选)")
    verdict = "候选资格(非确认)" if sigma >= 0.35 else "不通过"
    print(f"判读(预锁): {verdict}")


if __name__ == "__main__":
    main()
