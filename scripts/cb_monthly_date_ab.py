"""CB 月度调仓"实盘日期"精确 A/B（2026-10-21，用户批准月度变更后的实现前验证）。

背景：`backtest_cb_sched_compare.py` 的 monthly 臂只在"快照日=每月 1 日恰好是
交易日"时调仓，1 日休市的月份整月跳过（93 个快照日全部是月初 1 日）；nextday
臂在 1 日交易时用 1 日收盘快照在 1 日调仓（same-bar）。**snapdelay 才是无前视
实盘日程**：快照日之后的第一个交易日调仓（1 日休市→当月首个交易日；1 日交易
→次一交易日），快照数据在调仓日前已可见。本脚本用同一套股票腿引擎（32 万/组
= 40/60 真实资本；50 万/组 = 现行 100/60 名义）把各日程 × 三种退市回收口径的
组合画像一次算清，并跑 15% 熔断网格。

只用 2019+（CB NAV 2019 前为占位常数）。
产出：logs/cb_monthly_date_ab_20261021.json / .md
用法：python scripts/cb_monthly_date_ab.py
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates

START, END = "2019-01-01", "2026-10-19"
SCHEDULES = ["monthly", "nextday", "snapdelay", "biweekly", "quarterly"]
RECOVERIES = ["zero", "last", "par"]


def metrics_ret(r):
    r = r.dropna()
    nav = (1 + r).cumprod()
    y = len(r) / 252
    return {"n": int(len(r)),
            "ann_pct": round(float((nav.iloc[-1] ** (1 / y) - 1) * 100), 2),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 3),
            "dd_pct": round(float((nav / nav.cummax() - 1).min() * 100), 2)}


def combo(legs, w_stock, halt=None):
    nav, peak = 1.0, 1.0
    w = np.array([w_stock, 1 - w_stock], float)
    target = w.copy()
    halted = False
    last_month = None
    trig = 0
    navs = []
    for dt, row in legs.iterrows():
        ym = (dt.year, dt.month)
        if ym != last_month:
            last_month = ym
            if not halted:
                w = target.copy()
        vals = np.array([row["s"], row["cb"]], float)
        nav *= (1 + float(np.dot(w, vals)))
        peak = max(peak, nav)
        dd = nav / peak - 1
        if halt is not None:
            tau, rec = halt
            if not halted and dd <= tau:
                halted = True
                trig += 1
            elif halted and dd >= tau + rec:
                halted = False
                w = target.copy()
        navs.append(nav)
        w = w * (1 + vals)
        w = w / w.sum()
    return pd.Series(navs, index=legs.index), trig


def load_stock_nav(cap):
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist() if not meta.empty else []
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)), errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=cap)
    nav, _ = run_backtest(panel, ap, rebal, cfg, idx_c)
    return nav.pct_change().dropna()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-stock", action="store_true",
                    help="复用 logs/stock_leg_nav_{32,50}w.csv（省去重跑引擎）")
    args = ap.parse_args()
    out = {"as_of": "2026-10-19", "window": "2019-01-01~2026-10-09 (joint)"}
    stock = {}
    for cap_w, cap_yuan in ((32, 320_000.0), (50, 500_000.0)):
        cache = Path(f"logs/stock_leg_nav_{cap_w}w.csv")
        if args.reuse_stock and cache.exists():
            stock[cap_w] = pd.read_csv(cache, index_col=0,
                                       parse_dates=True).iloc[:, 0]
            print(f"reuse {cache}", flush=True)
        else:
            stock[cap_w] = load_stock_nav(cap_yuan)
            stock[cap_w].to_csv(cache)
            print(f"save {cache}", flush=True)
    ncb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0, parse_dates=True)
    for cap, w_stock, label in [(32, 0.4, "40/60 (32万/组)"),
                                (50, 0.625, "62.5/37.5 (实盘100/60名义)")]:
        out[label] = {}
        for sched in SCHEDULES:
            for rec in RECOVERIES:
                col = f"nav_{sched}_{rec}"
                r_cb = ncb[col].pct_change()
                legs = pd.concat([stock[cap].rename("s"), r_cb.rename("cb")],
                                 axis=1, join="inner").dropna()
                base_nav, _ = combo(legs, w_stock)
                m = metrics_ret(base_nav.pct_change().fillna(0))
                h_nav, trig = combo(legs, w_stock, halt=(-0.15, 0.02))
                mh = metrics_ret(h_nav.pct_change().fillna(0))
                out[label][f"{sched}_{rec}"] = {
                    **m, "halt15_triggers": trig,
                    "halt15_ann": mh["ann_pct"], "halt15_dd": mh["dd_pct"],
                    "halt15_damage_pp": round(mh["ann_pct"] - m["ann_pct"], 3)}
                print(label, sched, rec, out[label][f"{sched}_{rec}"], flush=True)
    Path("logs/cb_monthly_date_ab_20261021.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str))
    lines = ["# CB 月度调仓日期 A/B（2026-10-21）", "",
             "月度=快照日(1日)恰为交易日才调仓（49/93 个月，same-bar）；"
             "nextday=每月首个交易日；**snapdelay=快照日之后首个交易日"
             "（无前视=实盘口径）**；biweekly=2026-10-21 前旧实盘；"
             "quarterly=对照。窗口 2019+。", ""]
    for label in out:
        if label in ("as_of", "window"):
            continue
        lines += [f"## {label}", "",
                  "| 日程 | 回收 | 年化 | 夏普 | 回撤 | 15%触发 | 15%伤害 |",
                  "|---|---|---:|---:|---:|---:|---:|"]
        for k, v in out[label].items():
            s, r = k.split("_")
            lines.append(f"| {s} | {r} | {v['ann_pct']:+.2f}% | {v['sharpe']:.3f} | "
                         f"{v['dd_pct']:.2f}% | {v['halt15_triggers']} | "
                         f"{v['halt15_damage_pp']:+.2f}pp |")
        lines.append("")
    Path("logs/cb_monthly_date_ab_20261021.md").write_text("\n".join(lines))
    print("done")


if __name__ == "__main__":
    main()
