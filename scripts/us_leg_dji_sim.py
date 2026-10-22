"""US 腿 DJIA 载体检验（N=72，预注册 logs/prereg_us_leg_dji_20261022.md）。

复用 `us_leg_40_60.py` 已缓存的股票腿 NAV（u=0/10/12.5/15 对应每组分资金
32/24/22/20 万）与现产 CB NAV；美股腿 = DJIA 指数 T-1（价格口径，不含股息，
真实载体收益更高但本检验偏保守）。判据：Δ年化≥MDE、回撤不劣于 u=0、
5% 卫星后仍在 15% 线内、载体溢价≤5%、ADV≥1亿。

用法：python scripts/us_leg_dji_sim.py
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily
from backtest_engine import make_rebal_dates
from us_leg_40_60 import (sim, metrics, blend, te_mde,
                          TOTAL, STOCK0, CB0, SAT)

START, END = "2019-01-01", "2026-10-19"
US = [0.0, 0.10, 0.125, 0.15]
OUT = Path("logs/us_leg_dji_20261022.json")


def main() -> int:
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10]))
    rebal = [d for d in make_rebal_dates(cal, "biweekly")
             if START <= d <= END]

    stock = {}
    for u in US:
        f = Path(f"logs/us_leg_40_60_nav_u{int(u*1000)}.csv")
        stock[u] = pd.read_csv(f, index_col=0, parse_dates=True)["nav"]
    cb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0,
                     parse_dates=True)["nav_snapdelay_par"]
    r_cb = cb.pct_change().dropna()

    ix = pd.read_parquet("data_store/meta/us_dji_sina.parquet")
    ix["date"] = pd.to_datetime(ix["date"])
    ix = ix.set_index("date")["close"].astype(float).sort_index()
    r_us = ix.pct_change().shift(1).dropna()

    r_s0 = stock[0.0].pct_change().dropna()
    out = {"as_of": END, "test": "DJIA T-1 / 513400 vehicle",
           "prereg": "logs/prereg_us_leg_dji_20261022.md",
           "core": "40/60 stock 640k + CB 960k; US carved from stock",
           "us_source": "DJIA price index (dividends excluded)",
           "vehicle": {"code": "513400", "premium_pct_20261020": 2.62,
                       "adv_20d_yi": 3.26},
           "arms": {}, "mde_vs_u0": {}}
    for u in US:
        r_s = stock[u].pct_change().dropna()
        legs = pd.concat([r_s.rename("s"), r_cb.rename("cb"),
                          r_us.rename("us")], axis=1, join="inner").dropna()
        w = {"s": (STOCK0 - u * TOTAL) / TOTAL, "cb": CB0 / TOTAL, "us": u}
        ret, trig, dh = sim(legs, w, rebal)
        m = metrics(ret)
        sat = blend(ret, r_cb, SAT / (TOTAL + SAT))
        ms = metrics(sat)
        key = f"u{int(u*1000)}"
        out["arms"][key] = {
            "weights": {k: round(v, 4) for k, v in w.items()}, **m,
            "sat5": ms, "sat5_margin15": round(15 + ms["dd_pct"], 2),
            "halts": {}}
        for tau in (-0.10, -0.15, -0.20, -0.25):
            hr, t, d = sim(legs, w, rebal, halt=(tau, 0.02))
            out["arms"][key]["halts"][str(tau)] = {
                **metrics(hr), "triggers": t, "days_halted": d}
        if u == 0:
            out["window"] = f"{legs.index.min().date()}~{legs.index.max().date()}"
            out["base_key"] = key
        else:
            # 同窗口 u=0 对照: 用 u=0 的股票 NAV + CB, 对齐到本臂日期
            legs0 = pd.concat([r_s0.rename("s"), r_cb.rename("cb")],
                              axis=1, join="inner").reindex(legs.index).dropna()
            ret0, _, _ = sim(legs0, {"s": STOCK0 / TOTAL, "cb": CB0 / TOTAL},
                             rebal)
            m0 = metrics(ret0)
            mde = te_mde(ret, ret0)
            out["mde_vs_u0"][key] = {
                "delta_ann_pp_same_window": round(m["ann_pct"] - m0["ann_pct"], 2),
                "base_ann_pct_same_window": m0["ann_pct"],
                "base_dd_pct_same_window": m0["dd_pct"], **mde}
        print(key, m, "sat5", ms, flush=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(out["mde_vs_u0"], ensure_ascii=False, indent=1))
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
