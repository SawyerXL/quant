"""
波动率加权配比 A/B（2026-09-03，结构层研究1——机构实践对照后的落地项）。

事前登记（参数变更三件套）:
  原因: 固定62.5/37.5(股票100万/CB60万)不随两腿波动率变化; 机构实践
        (风险平价/目标波动率, 国泰君安4.14%/夏普1.69/回撤1.55%、
        光大目标波动2%档夏普2.06)验证"波动率预算"是结构层确定性方向。
  预测: B(逆波动率加权)/C(目标波动率8%)相对A(固定配比):
        + 夏普更高、回撤更浅
        - 牛市段(2019-21/2024.7-26.8)收益略低(风险平价经典踏空税)
  判读(事前锁定): B夏普≥A+0.05 且回撤浅≥1.5pp → 有效, 上会合入;
        B年化损失>1.5pp → 踏空税过重不采纳; 中间地带 → 维持现状。
口径:
  - 股票腿 = lot约束(pool30×50万)2路径{2,7}摊平日收益(logs/lot_arms),
    即部署口径, 非无约束回测口径
  - CB腿 = 双低策略日NAV(backtest_cb_doublelow, 单日程, 未摊平——已知
    局限: CB腿时点噪声未消除, 结论引用时标注)
  - 再平衡: 月末, 成本0.13%×换手名义
  - 波动率: 60交易日已实现年化(σ=std×sqrt(252))
  - 样本: 全期2019-2026.8 + 子区间分解
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger
logger.remove()

from backtest_engine import calc_metrics
from backtest_cb_doublelow import run_bt as cb_run_bt

START, END = "2019-01-01", "2026-08-28"
REBAL_COST = 0.0013
W_FIXED = 100 / 160          # A: 固定62.5%股票
VOL_WIN = 60                 # 波动率窗口(交易日)
TARGET_VOL = 0.08            # C: 年化目标波动率8%


def load_stock_leg():
    """部署口径股票腿: lot约束pool30的{2,7}两组日收益均值合成。"""
    rets = []
    for off in (2, 7):
        r = pd.read_parquet(Path("logs/lot_arms") / f"lot_p30_off{off}.parquet")
        r = r[~r.index.duplicated()].sort_index()
        rets.append(r.iloc[:, 0])
    j = pd.concat(rets, axis=1).dropna()
    return j.mean(axis=1)


def load_cb_leg():
    nav = cb_run_bt("dblow")
    return nav.pct_change().dropna()


def simulate(rs, rc, mode, w_s=None):
    """月末再平衡的组合模拟。mode: 'fixed' | 'invol' | 'targetvol'"""
    df = pd.concat([rs.rename("s"), rc.rename("c")], axis=1).dropna()
    nav = 1.0
    navs = []
    w_s_prev = W_FIXED
    # 逐日: 组合收益 = w_s*rs + (1-w_s)*rc (日内漂移), 月末重置
    for i, (date, row) in enumerate(df.iterrows()):
        nav *= (1 + w_s_prev * row["s"] + (1 - w_s_prev) * row["c"])
        navs.append(nav)
        # 月末再平衡
        is_month_end = (i == len(df) - 1) or \
            (date.month != df.index[i + 1].month)
        if is_month_end:
            if mode == "fixed":
                w_new = W_FIXED
            elif mode == "invol":
                vol_s = df["s"].iloc[max(0, i - VOL_WIN):i + 1].std() * np.sqrt(252)
                vol_c = df["c"].iloc[max(0, i - VOL_WIN):i + 1].std() * np.sqrt(252)
                w_new = (1 / max(vol_s, 1e-6)) / \
                    (1 / max(vol_s, 1e-6) + 1 / max(vol_c, 1e-6))
            elif mode == "targetvol":
                # 组合目标波动率: 整体缩放系数×固定配比
                port_r = W_FIXED * df["s"] + (1 - W_FIXED) * df["c"]
                vol_p = port_r.iloc[max(0, i - VOL_WIN):i + 1].std() * np.sqrt(252)
                scale = min(1.0, TARGET_VOL / max(vol_p, 1e-6))
                w_new = W_FIXED * scale
            # 再平衡成本(换手名义×0.13%)
            turnover = abs(w_new - w_s_prev)
            nav *= (1 - turnover * REBAL_COST)
            w_s_prev = w_new
    return pd.Series(navs, index=df.index)


def report(name, nav):
    cm = calc_metrics(nav)
    seg = nav[nav.index < pd.Timestamp("2026-01-01")]
    sub = seg.iloc[-1] / seg.iloc[0] - 1 if len(seg) > 1 else 0
    days = (seg.index[-1] - seg.index[0]).days
    ann_sub = (1 + sub) ** (365 / days) - 1 if days else 0
    print(f"{name:<14} 年化{float(str(cm['年化收益率']).strip('%')):+7.2f}%  "
          f"夏普{float(cm['夏普比率']):5.2f} 回撤{float(str(cm['最大回撤']).strip('%')):+7.2f}%  "
          f"| 2019-25子区间年化{ann_sub*100:+.2f}%", flush=True)
    return nav


def main():
    print("事前登记: B/C夏普优于A且回撤更浅, 牛市段收益略低", flush=True)
    rs = load_stock_leg()
    rc = load_cb_leg()
    print(f"腿数据: 股票{rs.index[0].date()}→{rs.index[-1].date()} "
          f"({len(rs)}天, 年化波动{rs.std()*np.sqrt(252)*100:.1f}%) | "
          f"CB({len(rc)}天, 年化波动{rc.std()*np.sqrt(252)*100:.1f}%)",
          flush=True)

    print("\n=== 全期 2019-2026.8 ===", flush=True)
    navs = {}
    navs["A固定62.5/37.5"] = report("A固定62.5/37.5", simulate(rs, rc, "fixed"))
    navs["B逆波动率加权"] = report("B逆波动率加权", simulate(rs, rc, "invol"))
    navs["C目标波动8%"] = report("C目标波动8%", simulate(rs, rc, "targetvol"))

    print("\n=== 子区间(2026至今) ===", flush=True)
    for name, nav in navs.items():
        seg = nav[nav.index >= pd.Timestamp("2026-01-01")]
        r = seg.iloc[-1] / seg.iloc[0] - 1
        print(f"{name:<14} 2026至今 {r*100:+.2f}%", flush=True)


if __name__ == "__main__":
    main()
