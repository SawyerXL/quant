"""A0 引擎外部校准收官 (2026-09-18, 归一库定稿后)。

09-16 上午校准(旧库): 形状级通过(相关0.9792/beta0.966), 水平级
未落槌(逐年残差 ±2~4pp), 终判待干净库曲线——本脚本用 09-16 晚
归一库上的 S1_A0 曲线收官。基准=官方 000906 价格指数(价格口径
对价格口径, 口径闸门一致)。判读沿用校准四轮定案: 形状=相关+beta,
水平=逐年残差(端点年=阶跃型可修 / 中间年=平滑漂移修不掉)。
用法: python scripts/a0_external_calib.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_meta

CURVE = Path("logs/s1_curve_S1_A0_pit800全关_013.csv")


def main():
    a0 = pd.read_csv(CURVE, index_col=0, parse_dates=True)
    a0 = a0.iloc[:, 0] if a0.ndim > 1 else a0
    idx = load_meta("csi800_index").set_index("date")["close"].sort_index()
    idx.index = pd.to_datetime(idx.index)
    common = a0.index.intersection(idx.index)
    a0c, ixc = a0.loc[common], idx.loc[common]

    ra, rb = a0c.pct_change().dropna(), ixc.pct_change().dropna()
    common_r = ra.index.intersection(rb.index)
    ra, rb = ra.loc[common_r], rb.loc[common_r]
    corr = float(ra.corr(rb))
    beta = float(np.cov(ra, rb)[0, 1] / np.var(rb))
    ann_a = float((1 + ra).prod() ** (252 / len(ra)) - 1)
    ann_b = float((1 + rb).prod() ** (252 / len(rb)) - 1)
    print(f"归一库 A0 vs 官方000906价格指数 (n={len(ra)}日)")
    print(f"日收益相关 {corr:.4f} | beta {beta:.3f} | "
          f"年化 A0={ann_a*100:+.2f}% 基准={ann_b*100:+.2f}% "
          f"差={100*(ann_a-ann_b):+.2f}pp")

    # 逐年残差(对数差累积)
    d = (np.log(a0c) - np.log(ixc)).diff().dropna()
    years = {}
    for y in range(2019, 2027):
        mask = d.index.year == y
        if mask.sum() > 20:
            years[y] = float(d[mask].sum())
    print("逐年残差(对数累计):")
    for y, v in years.items():
        flag = "端点" if y in (2019, 2026) else ""
        print(f"  {y}: {v*100:+.2f}pp {flag}")
    res = list(years.values())
    print(f"残差样本 std = {np.std(res)*100:.2f}pp (n={len(res)}年)")
    print(f"端点年残差: 2019={years.get(2019, 0)*100:+.2f}pp "
          f"2026={years.get(2026, 0)*100:+.2f}pp | "
          f"中间年(2020-25) std={np.std([v for y, v in years.items() if 2020 <= y <= 2025])*100:.2f}pp")
    print("\n收官判读: 形状级=" + ("通过" if corr > 0.9 else "待查")
          + " | 水平级=" + ("落槌: 残差形态与旧库同构(端点阶跃+中间平滑漂移), 非污染库修复可消除"
                            if abs(years.get(2019, 0)) > 0.05 or abs(years.get(2026, 0)) > 0.05
                            else "落槌: 残差收缩, 污染段假说成立"))


if __name__ == "__main__":
    main()
