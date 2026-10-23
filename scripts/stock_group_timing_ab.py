"""100万基准：单组合 canonical vs live {2,7} 两组（2026-10-23）。

背景：100万三件套的股票腿用**单组合 canonical**（`three_piece`），而 live
分组信号是 **{2,7} 两组**（g0=锚点+2、g1=锚点+7，各 20 万，资金等权）。
本脚本在同一面板/同一引擎/同一窗口上跑三条路径，量化：
1. 单组合 20 万（复现 2.66%/0.383/−15.07 基准）；
2. g0=+2、g1=+7 各 20 万；
3. 两组等权平均（live 结构的股票腿）；
4. 各自与 CB（nav_snapdelay_par）做 40/60 月度回目标权重组合。

只读研究，不改策略参数/信号链。窗口/配置与 100 万三件套一致。
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics
from three_piece_40_60 import combo, metrics_ret

START, END = "2019-01-03", "2026-10-19"
CAP = 200_000.0
OUT = ROOT / "logs" / "stock_group_timing_ab_20261023.json"


def main():
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    base = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    idx = {d: i for i, d in enumerate(cal)}

    def shift(off):
        out = []
        for d in base:
            i = idx.get(d)
            if i is None or i + off >= len(cal):
                continue
            x = cal[i + off]
            if START <= x <= END:
                out.append(x)
        return sorted(set(out))

    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist()
    prices, amounts = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d.empty:
                continue
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            amt = pd.to_numeric(d.get("amount", pd.Series(dtype=float)),
                                errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl
                if len(amt) >= 250:
                    amounts[code] = amt
        except Exception:
            pass
    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    idx_c = load_meta("csi800_index").set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    print(f"panel {panel.shape[0]}天×{panel.shape[1]}只, "
          f"base {len(base)} g0 {len(shift(2))} g1 {len(shift(7))}", flush=True)

    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=CAP)
    navs, metas = {}, {}
    for name, dates in [("single", base), ("g0", shift(2)), ("g1", shift(7))]:
        nav, m = run_backtest(panel, ap, dates, cfg, idx_c)
        navs[name] = nav
        cm = calc_metrics(nav)
        metas[name] = {"ann": cm["年化收益率"], "sharpe": cm["夏普比率"],
                       "dd": cm["最大回撤"],
                       "lot_skips": int(m.get("lot_skips", 0)),
                       "trades": int(m.get("trades", 0))}
        print(name, metas[name], flush=True)
        nav.to_csv(ROOT / "logs" / f"stock_leg_nav_20w_{name}.csv",
                   header=["nav"])

    r0 = navs["g0"].pct_change().dropna()
    r1 = navs["g1"].pct_change().dropna()
    r_avg = pd.concat([r0.rename("g0"), r1.rename("g1")], axis=1,
                      join="inner").mean(axis=1)
    nav_avg = (1 + r_avg).cumprod()
    nav_avg.to_csv(ROOT / "logs" / "stock_leg_nav_20w_avg.csv",
                   header=["nav"])
    cm_avg = calc_metrics(nav_avg)

    ncb = pd.read_csv(ROOT / "logs" / "cb_sched_compare_nav.csv",
                      index_col=0, parse_dates=True)
    r_cb = ncb["nav_snapdelay_par"].pct_change().dropna()
    out = {"as_of": "2026-10-23", "capital_per_group": CAP,
           "window": [START, END], "stock": {}, "combo": {}}
    for name, r_s in [("single", navs["single"].pct_change().dropna()),
                      ("g0", r0), ("g1", r1),
                      ("two_group", r_avg)]:
        out["stock"][name] = (metas[name] if name in metas else
                              {"ann": cm_avg["年化收益率"],
                               "sharpe": cm_avg["夏普比率"],
                               "dd": cm_avg["最大回撤"],
                               "lot_skips": metas["g0"]["lot_skips"]
                               + metas["g1"]["lot_skips"],
                               "trades": metas["g0"]["trades"]
                               + metas["g1"]["trades"]})
    for name, r_s in [("single", navs["single"].pct_change().dropna()),
                      ("two_group", r_avg)]:
        legs = pd.concat([r_s.rename("s"), r_cb.rename("cb")], axis=1,
                         join="inner").dropna()
        nav, trig, days = combo(legs)
        out["combo"][name] = {"metrics": metrics_ret(nav.pct_change().fillna(0)),
                              "trig": int(trig), "days_halted": int(days),
                              "n": int(len(legs))}
        nav.to_csv(ROOT / "logs" / f"combo_nav_20w_{name}.csv", header=["nav"])
        print("combo", name, out["combo"][name], flush=True)
    # 月度配对差（single vs two_group）
    nav_s = pd.read_csv(ROOT / "logs" / "combo_nav_20w_single.csv",
                        index_col=0, parse_dates=True)["nav"]
    nav_t = pd.read_csv(ROOT / "logs" / "combo_nav_20w_two_group.csv",
                        index_col=0, parse_dates=True)["nav"]
    rs = nav_s.resample("ME").last().pct_change().dropna()
    rt = nav_t.resample("ME").last().pct_change().dropna()
    diff = (rt - rs).dropna()
    out["combo"]["monthly_diff"] = {
        "n": int(len(diff)), "mean_pct": round(float(diff.mean() * 100), 4),
        "t": round(float(diff.mean() / diff.std() * np.sqrt(len(diff))), 2)
        if diff.std() > 0 else None}
    out["delta_combo_ann_pp"] = round(
        out["combo"]["two_group"]["metrics"]["ann_pct"]
        - out["combo"]["single"]["metrics"]["ann_pct"], 2)
    out["delta_combo_dd_pp"] = round(
        out["combo"]["two_group"]["metrics"]["dd_pct"]
        - out["combo"]["single"]["metrics"]["dd_pct"], 2)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    print("->", OUT)


if __name__ == "__main__":
    main()
