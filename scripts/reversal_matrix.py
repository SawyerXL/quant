"""反转效应描述性矩阵 (2026-09-17 深夜, 前置测量, 不耗预算)。

用户裁定: 本周唯一有两次独立支持的方向(大跌次日+62.1% / 隔夜溢价
入选组-0.805%反转)。本脚本只做描述: N日跌幅→次M日收益,
N∈{1,3,5}, M∈{1,3,5,10}, 全样本不筛选不择时。
声明: 本次仅描述, 不据此立项; 若要立项, 须另行预锁判据+消耗预算重测。
盲区: 幸存者样本(当前宇宙回填)/05-15缺口ffill桥接/M日重叠窗口
(计数为重叠观测非独立样本)。
用法: python scripts/reversal_matrix.py > logs/reversal_matrix_20260917.log 2>&1
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from backtest_return_attribution import WINDOWS
from smoke_t1_open import build_panels

START, END = WINDOWS["main"]
NS = (1, 3, 5)
MS = (1, 3, 5, 10)
BOTTOM_Q = 0.10   # 底部十分位=跌幅组
TOP_Q = 0.90


def main():
    panel, _open, _ap, cal = build_panels()
    close = panel.astype(float)
    ret1 = close.pct_change()

    print(f"主窗 {START}~{END} | 全样本主板+双创+创业板(无筛选无择时)")
    print("底部10%跌幅组的次M日收益 (均值/中位/夏普(重叠窗,年化), "
          "对比全样本与顶部10%)")
    print()
    print(f"{'N日跌幅':8s} {'M日':4s} {'n(底)':>8s} {'底10%均值':>10s} "
          f"{'全样本均值':>10s} {'顶10%均值':>10s} {'底-全(pp)':>9s} "
          f"{'底-顶(pp)':>9s} {'底组夏普':>8s}")
    for n in NS:
        past = close.pct_change(n)
        for m in MS:
            fwd = close.shift(-m) / close - 1
            stacked = pd.DataFrame({"past": past.stack(), "fwd": fwd.stack()})
            stacked = stacked.dropna()
            if stacked.empty:
                continue
            qlo, qhi = stacked["past"].quantile(BOTTOM_Q), stacked["past"].quantile(TOP_Q)
            lo = stacked[stacked["past"] <= qlo]
            hi = stacked[stacked["past"] >= qhi]
            lo_mean = lo["fwd"].mean()
            all_mean = stacked["fwd"].mean()
            hi_mean = hi["fwd"].mean()
            lo_sharpe = (lo["fwd"].mean() / lo["fwd"].std()
                         * np.sqrt(252 / m)) if lo["fwd"].std() > 0 else 0
            print(f"{n:>8d} {m:>4d} {len(lo):>8d} {lo_mean*100:>9.3f}% "
                  f"{all_mean*100:>9.3f}% {hi_mean*100:>9.3f}% "
                  f"{(lo_mean-all_mean)*100:>+8.2f} "
                  f"{(lo_mean-hi_mean)*100:>+8.2f} {lo_sharpe:>8.2f}")
        print()
    print("声明: 仅描述不立项; M日收益重叠(观测非独立); 幸存者样本; "
          "05-15缺口ffill。若要立项: 另行预锁判据+消耗预算重测。")


if __name__ == "__main__":
    main()
