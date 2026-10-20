"""美股腿最终配比模拟（2026-10-20，考后恢复；不耗 alpha 预算）。

口径：
- 两腿日收益 logs/stock_cb_joint_returns.csv（s=股票, cb=CB，2019-01~2026-09-18）。
- 美股腿：①实际车辆 513100（Sina，人民币二级市场价，含 QDII 溢价与汇率）
  ②纳指 .IXIC 代理（美东 T-1 收盘 → 中国 T，时区因果对齐）。
- 部署口径：CB 固定 60 万；美股腿从股票腿划拨（股票 100 万 − u×160 万）。
  研究口径：40/60 两腿按 (1−u) 缩放的 inside 对照。
- 再平衡：实盘日程（月中 n//2−1 + 月末倒数第二）；腿间漂移；换手成本 3bp/边。
- 未计：QDII 申赎限制、溢价冲击（另做 haircut 敏感性）、税费。
用法：python scripts/us_leg_final_sim.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from backtest_engine import make_rebal_dates

TOTAL = 1_600_000.0
STOCK0 = 1_000_000.0
CB0 = 600_000.0
COST = 0.0003
OUT = Path("logs/us_leg_final_sim_20261020.json")


def metrics(ret):
    nav = (1 + ret).cumprod()
    years = (ret.index[-1] - ret.index[0]).days / 365.25
    ann = nav.iloc[-1] ** (1 / years) - 1
    sr = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0.0
    dd = (nav / nav.cummax() - 1).min()
    return {"ann_pct": round(float(ann) * 100, 2),
            "sharpe": round(float(sr), 3),
            "dd_pct": round(float(dd) * 100, 2),
            "years": round(float(years), 2)}


def load_legs():
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv",
                        index_col=0, parse_dates=True)
    joint.columns = ["s", "cb"]
    etf = pd.read_parquet("data_store/meta/etf_513100_sina.parquet")
    etf["date"] = pd.to_datetime(etf["date"])
    etf = etf.set_index("date")["close"].astype(float).sort_index()
    e_ret = etf.pct_change().dropna()
    ix = pd.read_parquet("data_store/meta/us_ixic_sina.parquet")
    ix["date"] = pd.to_datetime(ix["date"])
    ix = ix.set_index("date")["close"].astype(float).sort_index()
    # 美东 T-1 收盘收益 -> 中国 T（shift 到下一 US 交易日，日期即中国可用日）
    ix_ret = ix.pct_change().shift(1).dropna()
    return joint, e_ret, ix_ret


def sim(legs, weights, rebal_dates, cost=COST):
    """按目标权重、定期再平衡；腿间漂移；成本按换手双边计。"""
    idx = legs.index
    w = pd.Series(weights, index=legs.columns, dtype=float)
    vals = w.values.copy()
    nav = 1.0
    navs = []
    rbal = set(str(d)[:10] for d in rebal_dates)
    for dt, row in legs.iterrows():
        if navs:
            # 漂移后的权重
            vals = vals * (1 + row.values)
            tot = vals.sum()
            if tot > 0:
                vals = vals / tot
        port_ret = float(np.dot(vals, row.values))
        nav *= (1 + port_ret)
        navs.append(nav)
        ds = dt.strftime("%Y-%m-%d")
        if ds in rbal:
            target = w.values
            turnover = float(np.abs(target - vals).sum()) / 2.0
            nav *= (1 - cost * turnover)
            vals = target.copy()
    ret = pd.Series(navs, index=idx).pct_change().fillna(0)
    # 再平衡当日的成本已计入 NAV；用 nav 序列的收益
    return pd.Series(navs, index=idx), ret


def main():
    joint, e_ret, ix_ret = load_legs()
    cal = [d.strftime("%Y-%m-%d") for d in joint.index]
    rebal = make_rebal_dates(cal, "biweekly")
    print(f"joint {joint.index.min().date()}~{joint.index.max().date()} n={len(joint)}")
    print(f"513100 {e_ret.index.min().date()}~{e_ret.index.max().date()}")
    print(f"IXIC(T-1) {ix_ret.index.min().date()}~{ix_ret.index.max().date()}")
    out = {"as_of": str(joint.index.max().date()),
           "funding": "CB fixed 60w; US carved from stock 100w",
           "cost_per_side": COST, "arms": {}}
    for uname, us in (("513100", e_ret), ("IXIC_T1", ix_ret)):
        legs = pd.concat([joint["s"], joint["cb"], us.rename("us")],
                         axis=1, join="inner").dropna()
        legs.columns = ["s", "cb", "us"]
        if len(legs) < 500:
            print(uname, "insufficient overlap", len(legs))
            continue
        for u in (0.0, 0.10, 0.125, 0.15, 0.20, 0.25):
            stock_cap = STOCK0 - u * TOTAL
            w = {"s": stock_cap / TOTAL, "cb": CB0 / TOTAL, "us": u}
            nav, ret = sim(legs, w, rebal)
            m = metrics(ret)
            key = f"{uname}_u{int(u*1000)}"
            out["arms"][key] = {"weights": {k: round(v, 4) for k, v in w.items()},
                                **m,
                                "n_days": int(len(legs)),
                                "start": str(legs.index.min().date()),
                                "end": str(legs.index.max().date())}
            print(key, out["arms"][key])
    OUT.write_text(__import__("json").dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)


if __name__ == "__main__":
    main()
