"""US 40/60 — 标普500 廉价载体口径（2026-10-21，轻量补算，不跑引擎）。

背景：40/60 IXIC 主口径结果成立；但最低溢价载体是标普500（513650）。
`etf_premium_513500.parquet` 的 nav_ret/px_ret 含 **2022-03-30 未复权拆分
（−49.8%）**，直接年化 6.02% 是错的；把拆分日收益置 0 后为 16.40%。
本脚本用拆分修正后的 513500 NAV/PX + 已保存的 32/22万/组股票 NAV + CB
现产月度序列，补算 40/60 u=0/12.5（及 10/15）。
用法：python scripts/us_leg_40_60_sp500.py
"""
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily
from backtest_engine import make_rebal_dates

_spec = importlib.util.spec_from_file_location(
    "us4060", Path(__file__).parent / "us_leg_40_60.py")
us = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(us)

OUT = Path("logs/us_leg_40_60_sp500_20261021.json")
SPLIT_DAY = "2022-03-30"


def adjusted_returns() -> tuple[pd.Series, pd.Series]:
    d = pd.read_parquet("data_store/meta/etf_premium_513500.parquet")
    nav = d["nav_ret"].copy()
    px = d["px_ret"].copy()
    # 拆分日：真实市场收益≈0（单日对年化影响<0.05pp），不做份额比例推断
    nav.loc[SPLIT_DAY] = 0.0
    px.loc[SPLIT_DAY] = 0.0
    return nav.dropna(), px.dropna()


def main() -> int:
    sh = load_daily("SH000001", "2014-06-01", "2026-10-19")
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly")
             if "2019-01-01" <= d <= "2026-10-19"]
    cb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0,
                     parse_dates=True)["nav_snapdelay_par"]
    r_cb = cb.pct_change().dropna()
    navs = {}
    for u, tag in ((0.0, 0), (0.10, 100), (0.125, 125), (0.15, 150)):
        navs[u] = pd.read_csv(f"logs/us_leg_40_60_nav_u{tag}.csv",
                              index_col=0, parse_dates=True)["nav"]
    r_nav, r_px = adjusted_returns()
    out = {"split_day": SPLIT_DAY, "split_note": "513500 2022-03-30 未复权拆分, 收益置0",
           "arms": {}, "mde_vs_u0": {}}
    base = {}
    for lens, r_us in (("SP500_NAV", r_nav), ("SP500_PX", r_px)):
        for u, nav in navs.items():
            legs = pd.concat([nav.pct_change().rename("s"),
                              r_cb.rename("cb"), r_us.rename("us")],
                             axis=1, join="inner").dropna()
            if len(legs) < 500:
                continue
            w = {"s": (640_000 - u * 1_600_000) / 1_600_000,
                 "cb": 0.6, "us": u}
            ret, trig, dh = us.sim(legs, w, rebal)
            m = us.metrics(ret)
            sat = us.blend(ret, r_cb, 80_000 / 1_680_000)
            ms = us.metrics(sat)
            key = f"{lens}_u{int(u*1000)}"
            out["arms"][key] = {**m, "weights": {k: round(v, 4) for k, v in w.items()},
                                "sat5": ms,
                                "sat5_margin15": round(15 + ms["dd_pct"], 2),
                                "n": int(len(legs))}
            if u == 0:
                base[lens] = ret
            print(key, m, "sat5", ms, flush=True)
    for lens in ("SP500_NAV", "SP500_PX"):
        if lens not in base:
            continue
        for u in (0.10, 0.125, 0.15):
            key = f"{lens}_u{int(u*1000)}"
            if key not in out["arms"]:
                continue
            out["mde_vs_u0"][key] = {
                "delta_ann_pp_same_window": round(
                    out["arms"][key]["ann_pct"] - out["arms"][f"{lens}_u0"]["ann_pct"], 2),
                **us.te_mde(
                    # 用 arm 的日收益（重算一次，权重同）
                    us.sim(pd.concat([navs[u].pct_change().rename("s"),
                                      r_cb.rename("cb"),
                                      (r_nav if lens == "SP500_NAV" else r_px).rename("us")],
                                     axis=1, join="inner").dropna(),
                           {"s": (640_000 - u*1_600_000)/1_600_000,
                            "cb": 0.6, "us": u}, rebal)[0],
                    base[lens])}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(out["mde_vs_u0"], ensure_ascii=False, indent=1))
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
