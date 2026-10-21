"""CB 趋势过滤诊断（N=69 后续；非新假设，不入账本）。

目的：①逐年归因（改善是否集中在 ≤2 个事件）②19-22/23-26 分段
③V3 自身 NAV MA200（自计时）④半仓 ⑤组合 40/60 与 62.5/37.5。
窗口：CB 数据 2019+（2013-2018 为占位 1.0，已确认不可用）。
用法：python scripts/cb_timing_diagnostics.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from run_backtest_a import CASH_YIELD

COST = 0.001
OUT = Path("logs/cb_timing_diagnostics_20261021.json")


def met(r):
    r = r.dropna()
    if len(r) < 20:
        return {"n": int(len(r))}
    nav = (1 + r).cumprod()
    years = len(r) / 252
    return {"n": int(len(r)),
            "ann_pct": round(float((nav.iloc[-1] ** (1 / years) - 1) * 100), 2),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 3),
            "dd_pct": round(float((nav / nav.cummax() - 1).min() * 100), 2)}


def yearly(r):
    out = {}
    for y, g in r.groupby(r.index.year):
        if len(g) < 20:
            continue
        nav = (1 + g).cumprod()
        out[int(y)] = {"ret_pct": round(float((nav.iloc[-1] - 1) * 100), 2),
                       "dd_pct": round(float((nav / nav.cummax() - 1).min() * 100), 2)}
    return out


def overlay(r, sig, cash):
    sig = sig.reindex(r.index).ffill().fillna(0)
    sw = sig.diff().abs().fillna(0)
    return sig * r + (1 - sig) * cash - COST * sw, int((sw > 0).sum()), float(sig.mean())


def main():
    nav_full = pd.read_csv("logs/cb_eligibility_arms_nav.csv",
                           index_col=0, parse_dates=True)
    nav = nav_full.loc["2019-01-01":]
    r_base = nav["nav_V3_N10"].pct_change().dropna()
    cash = float((1 + CASH_YIELD) ** (1 / 252) - 1)
    idx = pd.read_parquet("data_store/meta/cb_index.parquet")
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.set_index("date")["close"].astype(float).sort_index()
    idx = idx.reindex(r_base.index, method="ffill")

    def sig_ma(series, ma=200):
        return (series > series.rolling(ma, min_periods=120).mean()
                ).astype(float).shift(1)

    arms = {}
    for name, sig in (
            ("yi_MA200", sig_ma(nav_full["nav_yi"])),
            ("v3_self_MA200", sig_ma(nav_full["nav_V3_N10"])),
            ("idx_MA200", sig_ma(idx))):
        r, sw, tin = overlay(r_base, sig, cash)
        arms[name] = {"all": met(r), "switches": sw, "time_in_pct": round(tin * 100, 1),
                      "yearly": yearly(r),
                      "p1_2019_2022": met(r[r.index < "2023-01-01"]),
                      "p2_2023_2026": met(r[r.index >= "2023-01-01"]),
                      "_ret": r}
        print(name, arms[name]["all"], "sw", sw, "tin", arms[name]["time_in_pct"],
              "p1", arms[name]["p1_2019_2022"], "p2", arms[name]["p2_2023_2026"])

    # 半仓 yi
    sig = sig_ma(nav["nav_yi"])
    r_half = sig * r_base + (1 - sig) * (0.5 * r_base + 0.5 * cash)
    arms["yi_MA200_half"] = {"all": met(r_half),
                             "yearly": yearly(r_half),
                             "p1_2019_2022": met(r_half[r_half.index < "2023-01-01"]),
                             "p2_2023_2026": met(r_half[r_half.index >= "2023-01-01"]),
                             "_ret": r_half}
    print("yi_half", arms["yi_MA200_half"]["all"])

    out = {"window": [str(r_base.index.min().date()), str(r_base.index.max().date())],
           "base": {"all": met(r_base), "yearly": yearly(r_base),
                    "p1_2019_2022": met(r_base[r_base.index < "2023-01-01"]),
                    "p2_2023_2026": met(r_base[r_base.index >= "2023-01-01"])},
           "arms": {k: {kk: vv for kk, vv in v.items() if kk != "_ret"}
                    for k, v in arms.items()}}

    # 组合层：40/60 与 62.5/37.5（股票腿 s 来自 joint CSV）
    joint = pd.read_csv("logs/stock_cb_joint_returns.csv",
                        index_col=0, parse_dates=True)
    joint.columns = ["s", "cb"]
    for wname, wc in (("40_60", 0.6), ("62.5_37.5", 0.375)):
        rows = {}
        for aname in ("base", "yi_MA200", "v3_self_MA200"):
            cb = r_base if aname == "base" else arms[aname]["_ret"]
            leg = pd.concat([joint["s"], cb.rename("cb")], axis=1,
                            join="inner").dropna()
            r = (1 - wc) * leg["s"] + wc * leg["cb"]
            rows[aname] = {"all": met(r), "yearly": yearly(r),
                           "p1": met(r[r.index < "2023-01-01"]),
                           "p2": met(r[r.index >= "2023-01-01"])}
        out[f"portfolio_{wname}"] = rows
        print("portfolio", wname, {k: v["all"] for k, v in rows.items()})

    # 归因：逐年 DD 改善
    out["yearly_dd_improvement"] = {}
    for y in out["base"]["yearly"]:
        b = out["base"]["yearly"][y]["dd_pct"]
        yy = arms["yi_MA200"]["yearly"].get(y, {}).get("dd_pct")
        out["yearly_dd_improvement"][y] = round((yy - b) if yy is not None else None, 2)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
