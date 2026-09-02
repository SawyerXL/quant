"""
双arm摊平复验分析（2026-09-01，读 backtest_double_arm_tranched.py 落盘的序列）。

输出:
1. 每arm: 10路径摊平指标(全期+2019-2025子区间) + 单路径极差
2. A/B差值: 同路径成对差 vs 摊平差 —— 拥挤度过滤/pool_size 的差值
   是否在摊平后仍超出噪声带, 以及跨路径是否同向
3. 2组/3组部署口径: C(10,2)/C(10,3)全部组合的摊平年化分布
   (均分组合{0,5}/{0,3,6} vs 最优/最差组合 = 部署时偏移选择敏感度)
"""
import sys
from pathlib import Path
from itertools import combinations
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from backtest_engine import calc_metrics

OUT = Path(__file__).parent.parent / "logs" / "tranched_arms"
ARMS = ["vol5_p60", "vol999_p60", "vol5_p30"]
ARM_LABEL = {"vol5_p60": "现网(vol5%+pool60)",
             "vol999_p60": "无拥挤度过滤",
             "vol5_p30": "pool30"}


def load_arm(name):
    rets = []
    for off in range(10):
        r = pd.read_parquet(OUT / f"{name}_off{off}.parquet")
        r = r[~r.index.duplicated()].sort_index()
        rets.append(r)
    return pd.concat(rets, axis=1).dropna()


def ann_of(nav):
    cm = calc_metrics(nav)
    return float(cm["年化_float"]), float(cm["夏普_float"]), \
        float(cm["回撤_float"])


def sub_ann(nav, cutoff="2026-01-01"):
    seg = nav[nav.index < pd.Timestamp(cutoff)]
    if seg.empty:
        return float("nan")
    tot = seg.iloc[-1] / seg.iloc[0] - 1
    days = (seg.index[-1] - seg.index[0]).days
    return (1 + tot) ** (365 / max(days, 1)) - 1


def main():
    arms = {}
    for name in ARMS:
        j = load_arm(name)
        ens = (1 + j.mean(axis=1)).cumprod()
        arms[name] = (j, ens)
        a, s, d = ann_of(ens)
        print(f"{ARM_LABEL[name]:<22}: 摊平年化{a*100:+.2f}% 夏普{s:.2f} "
              f"回撤{d*100:+.1f}% | 2019-2025子区间{sub_ann(ens)*100:+.2f}%",
              flush=True)

    # 路径独立性: 日收益两两相关(理论1/n消减的前提是独立, 实测重叠度)
    print("\n=== 路径间日收益相关(现网arm) ===", flush=True)
    j0 = arms["vol5_p60"][0]
    corr = j0.corr().values
    tri = [corr[i, k] for i in range(10) for k in range(i + 1, 10)]
    print(f"  两两相关: 均值{np.mean(tri):.3f} 最小{np.min(tri):.3f} "
          f"最大{np.max(tri):.3f}", flush=True)
    print(f"  理论方差消减(独立)=1/10; 实测按均相关ρ={np.mean(tri):.2f}估算: "
          f"2组≈{(1+np.mean(tri))/2:.2f}倍方差(理想0.50), "
          f"3组≈{(1+2*np.mean(tri))/3:.2f}倍(理想0.33)", flush=True)

    # 单路径: 每arm年化分布 + 同路径成对差
    print("\n=== 单路径年化 vs 同路径成对差 ===", flush=True)
    anns = {}
    for name in ARMS:
        j = arms[name][0]
        anns[name] = []
        for off in range(10):
            nav = (1 + j.iloc[:, off]).cumprod()
            anns[name].append(ann_of(nav)[0])
        print(f"{ARM_LABEL[name]}: 均值{np.mean(anns[name])*100:+.2f}% "
              f"极差{(max(anns[name])-min(anns[name]))*100:.2f}pp "
              f"[{', '.join(f'{x*100:+.1f}' for x in anns[name])}]", flush=True)

    print("\n=== 拥挤度过滤A/B: 同路径成对差(vol5%-vol关闭) ===", flush=True)
    d = [anns["vol5_p60"][i] - anns["vol999_p60"][i] for i in range(10)]
    print(f"  路径差: {[f'{x*100:+.1f}' for x in d]}pp", flush=True)
    print(f"  均值{np.mean(d)*100:+.2f}pp 极差{(max(d)-min(d))*100:.2f}pp "
          f"| 摊平差: "
          f"{(ann_of(arms['vol5_p60'][1])[0]-ann_of(arms['vol999_p60'][1])[0])*100:+.2f}pp",
          flush=True)

    print("\n=== pool_size A/B: 同路径成对差(pool60-pool30, 均vol5%) ===",
          flush=True)
    d2 = [anns["vol5_p60"][i] - anns["vol5_p30"][i] for i in range(10)]
    print(f"  路径差: {[f'{x*100:+.1f}' for x in d2]}pp", flush=True)
    print(f"  均值{np.mean(d2)*100:+.2f}pp 极差{(max(d2)-min(d2))*100:.2f}pp "
          f"| 摊平差: "
          f"{(ann_of(arms['vol5_p60'][1])[0]-ann_of(arms['vol5_p30'][1])[0])*100:+.2f}pp",
          flush=True)

    # 2组/3组部署口径: 所有组合的摊平年化分布
    for n in (2, 3):
        print(f"\n=== {n}组摊平: C(10,{n})组合年化分布 "
              f"(现网arm vol5_p60) ===", flush=True)
        j = arms["vol5_p60"][0]
        vals = {}
        for combo in combinations(range(10), n):
            ens = (1 + j.iloc[:, list(combo)].mean(axis=1)).cumprod()
            vals[combo] = ann_of(ens)[0]
        arr = sorted(vals.values())
        lo, hi = arr[0], arr[-1]
        mid = arr[len(arr) // 2]
        even = {2: (0, 5), 3: (0, 3, 6)}[n]
        print(f"  最差{lo*100:+.2f}% 中位{mid*100:+.2f}% 最优{hi*100:+.2f}% "
              f"| 均分组合{even}: {vals[even]*100:+.2f}%", flush=True)
        # 组合间的敏感度 = 部署时选哪些偏移的风险
        print(f"  组合选择敏感度: 极差{(hi-lo)*100:.2f}pp "
              f"(10路径全摊平{ann_of(arms['vol5_p60'][1])[0]*100:+.2f}%)",
              flush=True)
        # 组合含offset0 vs 不含
        w0 = [v for c, v in vals.items() if 0 in c]
        wo0 = [v for c, v in vals.items() if 0 not in c]
        print(f"  含现网时点(0): 均值{np.mean(w0)*100:+.2f}% (n={len(w0)}) | "
              f"不含: 均值{np.mean(wo0)*100:+.2f}% (n={len(wo0)})", flush=True)


if __name__ == "__main__":
    main()
