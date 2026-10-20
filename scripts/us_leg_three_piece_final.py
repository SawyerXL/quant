"""美股腿三件套终版：实际降低资金后的股票腿 + CB + 513100（2026-10-20）。

- 股票腿：logs/us_leg_partb_nav_u{0,100,125,150}.csv（引擎实跑，实盘日程，lot=100）。
- CB：logs/stock_cb_joint_returns.csv 的 cb（60 万固定）。
- 美股：513100 实际二级市场价格（人民币，含溢价/汇率）。
- 权重：股票=2×cap/160万; CB=60/160; 美股=u。
- 再平衡：实盘日程；腿间漂移；3bp/边。
用法：python scripts/us_leg_three_piece_final.py
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
OUT = Path("logs/us_leg_three_piece_final_20261020.json")


def metrics(ret):
    nav = (1 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365.25
    ann = nav.iloc[-1] ** (1 / years) - 1
    sr = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
    dd = (nav / nav.cummax() - 1).min()
    # 年度回撤
    yearly = {}
    for y, g in ret.groupby(ret.index.year):
        n = (1 + g).cumprod()
        yearly[int(y)] = {"ret_pct": round(float(n.iloc[-1] - 1) * 100, 2),
                          "dd_pct": round(float((n / n.cummax() - 1).min()) * 100, 2)}
    return {"ann_pct": round(float(ann) * 100, 2), "sharpe": round(float(sr), 3),
            "dd_pct": round(float(dd) * 100, 2), "years": round(float(years), 2),
            "yearly": yearly}


def sim(legs, weights, rebal_dates, cost=COST):
    vals = np.array(weights, dtype=float)
    target = vals.copy()
    nav = 1.0
    navs = []
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
            turnover = float(np.abs(target - vals).sum()) / 2.0
            nav *= (1 - cost * turnover)
            vals = target.copy()
    s = pd.Series(navs, index=legs.index)
    return s, s.pct_change().fillna(0)


def main():
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv",
                        index_col=0, parse_dates=True)
    joint.columns = ["s", "cb"]
    etf = pd.read_parquet("data_store/meta/etf_513100_sina.parquet")
    etf["date"] = pd.to_datetime(etf["date"])
    us = etf.set_index("date")["close"].astype(float).sort_index().pct_change().dropna()
    cal = [d.strftime("%Y-%m-%d") for d in joint.index]
    rebal = make_rebal_dates(cal, "biweekly")
    out = {"as_of": str(joint.index.max().date()), "arms": {}}
    for u, tag in ((0.0, "u0"), (0.10, "u100"), (0.125, "u125"), (0.15, "u150")):
        p = Path(f"logs/us_leg_partb_nav_{tag}.csv")
        if not p.exists():
            print("missing", p); continue
        sn = pd.read_csv(p, index_col=0, parse_dates=True).iloc[:, 0]
        s_ret = sn.pct_change().dropna()
        legs = pd.concat([s_ret.rename("s"), joint["cb"], us.rename("us")],
                         axis=1, join="inner").dropna()
        stock_total = 2 * {0.0: 500_000, 0.10: 420_000,
                           0.125: 400_000, 0.15: 380_000}[u]
        w = [stock_total / TOTAL, 600_000 / TOTAL, u]
        if abs(sum(w) - 1.0) > 1e-9:
            print("weight sum", u, sum(w)); continue
        nav, ret = sim(legs, w, rebal)
        m = metrics(ret)
        out["arms"][tag] = {"u": u, "weights": [round(x, 4) for x in w],
                            "n_days": int(len(legs)), **m}
        print(tag, out["arms"][tag], flush=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
