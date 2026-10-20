"""美股腿溢价调整后的组合（2026-10-20）：用 513100 NAV 收益替代价格收益。

背景：513100 当前溢价约 18.6~20.5%，处于历史极高（均值 1.6%/p90 6.2%）。
价格回测包含 2019 至今约 +22pp 的溢价扩张（一次性），不能当作可重复收益。
本脚本：
  A) 用 NAV 收益（真实底层）重跑 u=15 组合；
  B) 一次性溢价回归（−18.5pp）冲击；
  C) 12 个月线性回归（每月 −18.5/12 pp）。
用法：python scripts/us_leg_premium_adjusted.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from backtest_engine import make_rebal_dates

TOTAL = 1_600_000.0
COST = 0.0003
OUT = Path("logs/us_leg_premium_adjusted_20261020.json")


def metrics(nav):
    """nav: 从初始 1.0 开始的净值序列（含首日一次性冲击）。"""
    vals = np.r_[1.0, np.asarray(nav, dtype=float)]
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    ann = vals[-1] ** (1 / years) - 1
    rets = vals[1:] / vals[:-1] - 1
    sr = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    peak = np.maximum.accumulate(vals)
    dd = (vals / peak - 1).min()
    return {"ann_pct": round(float(ann) * 100, 2),
            "sharpe": round(float(sr), 3),
            "dd_pct": round(float(dd) * 100, 2)}


def sim(legs, weights, rebal_dates, cost=COST):
    vals = np.array(weights, float); target = vals.copy()
    nav = 1.0; navs = []
    rbal = set(rebal_dates)
    for dt, row in legs.iterrows():
        if navs:
            vals = vals * (1 + row.values)
            tot = vals.sum()
            if tot > 0:
                vals = vals / tot
        nav *= (1 + float(np.dot(vals, row.values)))
        navs.append(nav)
        if dt.strftime("%Y-%m-%d") in rbal:
            nav *= (1 - cost * float(np.abs(target - vals).sum()) / 2)
            vals = target.copy()
    s = pd.Series(navs, index=legs.index)
    return s, s.pct_change().fillna(0)


def main():
    sn = pd.read_csv("logs/us_leg_partb_nav_u150.csv", index_col=0,
                     parse_dates=True).iloc[:, 0]
    s_ret = sn.pct_change().dropna()
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv",
                        index_col=0, parse_dates=True)
    joint.columns = ["s", "cb"]
    prem = pd.read_parquet("data_store/meta/etf_premium_513100.parquet")
    nav_ret = prem["nav_ret"].dropna()
    legs = pd.concat([s_ret.rename("s"), joint["cb"], nav_ret.rename("us")],
                     axis=1, join="inner").dropna()
    cal = [d.strftime("%Y-%m-%d") for d in legs.index]
    rebal = make_rebal_dates(cal, "biweekly")
    w = [0.475, 0.375, 0.15]
    out = {"as_of": str(legs.index.max().date()), "weights": w,
           "current_premium_pct": 18.6, "arms": {}}
    nav_a, ret_a = sim(legs, w, rebal)
    out["arms"]["A_nav_returns"] = metrics(nav_a)
    print("A nav returns", out["arms"]["A_nav_returns"])
    # B: 一次性 -18.5pp on US sleeve at start
    legs_b = legs.copy()
    legs_b.iloc[0, legs_b.columns.get_loc("us")] -= 0.185
    nav_b, _ = sim(legs_b, w, rebal)
    out["arms"]["B_oneoff_18.5pp"] = metrics(nav_b)
    print("B oneoff", out["arms"]["B_oneoff_18.5pp"])
    # C: 12 个月内线性回归（按交易日摊 18.5%）
    legs_c = legs.copy()
    n = len(legs_c)
    dec = np.where(np.arange(n) < 252, 1.0 / 252.0, 0.0)
    legs_c["us"] = legs_c["us"] - 0.185 * dec
    nav_c, _ = sim(legs_c, w, rebal)
    out["arms"]["C_12m_reversion"] = metrics(nav_c)
    print("C 12m", out["arms"]["C_12m_reversion"])
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
