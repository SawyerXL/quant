"""N=70：被动 ETF + 引擎同款五档 MA200 择时（2026-10-21）。

预注册：logs/prereg_etf_timing_20261021.md（11:22:46，先于本脚本运行）。

- 择时：light panel 跑引擎取 `exposure_ts.target`（信号只依赖 csi800 与
  五档状态机，与全股票引擎同源）；tier0(0.30)→0（引擎清仓分支）。
- 被动腿：510300/510500 累计净值（全收益），双周回 50/50、与调仓日同步。
- 成本 0.13%/边、现金 1.4%/年；不做 MA10/止损/过热（只留 beta+择时）。
用法：python scripts/etf_timing_ab.py [--oos]
"""
import argparse
import importlib.util
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
COMM = 0.0013
CASH_YIELD = 0.014
CORE_STOCK, CORE_CB, SATELLITE = 640_000.0, 960_000.0, 80_000.0
OUT = Path("logs/etf_timing_ab_20261021.json")

_spec = importlib.util.spec_from_file_location(
    "tp4060", Path(__file__).parent / "three_piece_40_60.py")
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)


def calendar(start=START, end=END):
    idx = load_meta("csi800_index")
    idx["date"] = pd.to_datetime(idx["date"])
    d = idx[(idx["date"] >= start) & (idx["date"] <= end)]
    return d.set_index("date")["close"].astype(float).sort_index()


def etf_returns(cal_index: pd.DatetimeIndex) -> pd.DataFrame:
    etf = pd.read_parquet("data_store/meta/etf_total_return.parquet")
    etf["date"] = pd.to_datetime(etf["date"])
    piv = etf.pivot(index="date", columns="code", values="cum_nav").sort_index()
    piv = piv.reindex(cal_index).ffill()
    return piv.pct_change(fill_method=None).dropna()


def light_panel(cal_index: pd.DatetimeIndex):
    prices, amounts = {}, {}
    for c in ("510300", "510500"):
        d = load_daily(c, str(cal_index.min().date()), str(cal_index.max().date()))
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date").sort_index()
        prices[c] = pd.to_numeric(d["close"], errors="coerce").reindex(
            cal_index).ffill()
        amounts[c] = pd.to_numeric(d["amount"], errors="coerce").reindex(
            cal_index).ffill()
    return pd.DataFrame(prices), pd.DataFrame(amounts)


def regime_target(cal_index: pd.Series) -> tuple[pd.Series, dict, list]:
    panel, amounts = light_panel(cal_index.index)
    sh = load_daily("SH000001", "2014-06-01", str(cal_index.index.max().date()))
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly")
             if START <= d <= str(cal_index.index.max().date())]
    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=400_000,
                  pool_size=2, max_position_pct=0.5, diag_exposure=True)
    _, m = run_backtest(panel, amounts, rebal, cfg, cal_index)
    ts = pd.DataFrame(m["exposure_ts"],
                      columns=["date", "actual", "target", "nav"])
    ts["date"] = pd.to_datetime(ts["date"])
    ts = ts.set_index("date").sort_index()
    # tier0 目标 0.30 在引擎里走清仓分支 → 0 敞口
    tgt = ts["target"].where(ts["target"] > 0.3, 0.0).reindex(
        cal_index.index).ffill()
    stats = {"tier_switch_count": int(m["tier_switch_count"]),
             "target_counts": {str(k): int(v) for k, v in
                               ts["target"].round(2).value_counts().sort_index().items()},
             "first_rebal": next((d for d in rebal
                                  if panel.index.get_loc(d) >= 250), None)}
    return tgt, stats, rebal


def mix_returns(r300: pd.Series, r500: pd.Series, reb: set) -> pd.Series:
    w = np.array([0.5, 0.5])
    out = []
    for dt in r300.index:
        rr = np.array([r300.loc[dt], r500.loc[dt]], float)
        out.append(float(w @ rr))
        w = w * (1 + rr)
        w = w / w.sum()
        if str(dt.date()) in reb:
            w = np.array([0.5, 0.5])
    return pd.Series(out, index=r300.index)


def timed_sleeve(r_etf: pd.Series, tgt: pd.Series, reb: set,
                 min_bars: int = 250) -> pd.Series:
    cash = (1 + CASH_YIELD) ** (1 / 252) - 1
    w, out = 0.0, []
    for i, dt in enumerate(r_etf.index):
        r = float(r_etf.loc[dt])
        ret = w * r + (1 - w) * cash
        w = w * (1 + r) / (w * (1 + r) + (1 - w) * (1 + cash))
        if str(dt.date()) in reb and i >= min_bars:
            new_w = float(tgt.loc[dt])
            ret -= COMM * abs(new_w - w)
            w = new_w
        out.append(ret)
    return pd.Series(out, index=r_etf.index)


def blend(r1: pd.Series, r2: pd.Series, w2: float) -> pd.Series:
    idx = r1.index.intersection(r2.index)
    a, b = r1.reindex(idx).fillna(0.0), r2.reindex(idx).fillna(0.0)
    w = np.array([1 - w2, w2], float)
    out, last = [], None
    for dt, x, y in zip(idx, a, b):
        ym = (dt.year, dt.month)
        if ym != last:
            last = ym
            w = np.array([1 - w2, w2], float)
        out.append(float(np.dot(w, [x, y])))
        w = w * np.array([1 + x, 1 + y])
        w = w / w.sum()
    return pd.Series(out, index=idx)


def te_mde(r_a: pd.Series, r_b: pd.Series) -> dict:
    idx = r_a.index.intersection(r_b.index)
    d = (r_a.reindex(idx) - r_b.reindex(idx)).dropna()
    te = float(d.std() * np.sqrt(252))
    years = len(d) / 252
    se = te / np.sqrt(years)
    return {"n": int(len(d)), "years": round(years, 2),
            "TE_pct": round(te * 100, 2), "SE_pp": round(se * 100, 2),
            "MDE_pp": round(1.96 * se * 100, 2)}


def core_metrics(r_stock: pd.Series, r_cb: pd.Series) -> dict:
    legs = pd.concat([r_stock.rename("s"), r_cb.rename("cb")],
                     axis=1, join="inner").dropna()
    tp.W_STOCK, tp.W_CB = CORE_STOCK / (CORE_STOCK + CORE_CB), \
        CORE_CB / (CORE_STOCK + CORE_CB)
    base_nav, _, _ = tp.combo(legs)
    r = base_nav.pct_change().fillna(0)
    out = {"base": tp.metrics_ret(r),
           "margin_15": round(15 + tp.metrics_ret(r)["dd_pct"], 2),
           "halts": {}}
    for tau in (-0.10, -0.15, -0.20, -0.25):
        h_nav, trig, dh = tp.combo(legs, halt=(tau, 0.02))
        m = tp.metrics_ret(h_nav.pct_change().fillna(0))
        out["halts"][str(tau)] = {**m, "triggers": trig,
                                  "days_halted": dh}
    r_sat = blend(r, r_cb, SATELLITE / (CORE_STOCK + CORE_CB + SATELLITE))
    m_sat = tp.metrics_ret(r_sat)
    out["sat5"] = {**m_sat, "margin_15": round(15 + m_sat["dd_pct"], 2)}
    return out, r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos", action="store_true", help="跑 2015-2018 股腿 OOS")
    args = ap.parse_args()

    cal = calendar()
    r_etf_df = etf_returns(cal.index)
    r300, r500 = r_etf_df["510300"], r_etf_df["510500"]
    tgt, tstats, rebal = regime_target(cal)
    reb = set(rebal)
    r_etf = mix_returns(r300, r500, reb)
    r_timed = timed_sleeve(r_etf, tgt, reb)
    print(f"window {cal.index.min().date()}~{cal.index.max().date()} "
          f"n={len(cal)} tier_switches={tstats['tier_switch_count']} "
          f"first_rebal={tstats['first_rebal']}", flush=True)
    print("target counts", tstats["target_counts"], flush=True)

    nav_active = pd.read_csv("logs/stock_leg_nav_32w_20261021.csv",
                             index_col=0, parse_dates=True)["nav"]
    nav_active.index = pd.to_datetime(nav_active.index)
    nav_active = nav_active.reindex(cal.index).ffill()
    r_active = nav_active.pct_change(fill_method=None).dropna()

    common = r_active.index.intersection(r_timed.index)
    r_active = r_active.reindex(common)
    r_timed = r_timed.reindex(common)
    r_etf = r_etf.reindex(common)
    r300, r500 = r300.reindex(common), r500.reindex(common)

    cb_nav = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0,
                         parse_dates=True)["nav_snapdelay_par"]
    r_cb = cb_nav.pct_change().dropna()

    stand = {"active_32w": tp.metrics_ret(r_active),
             "passive_untimed": tp.metrics_ret(r_etf),
             "passive_timed": tp.metrics_ret(r_timed),
             "510300_timed": tp.metrics_ret(
                 timed_sleeve(r300, tgt, reb).reindex(common)),
             "510500_timed": tp.metrics_ret(
                 timed_sleeve(r500, tgt, reb).reindex(common))}

    core_t, r_core_t = core_metrics(r_timed, r_cb)
    core_a, r_core_a = core_metrics(r_active, r_cb)

    mde_stock = te_mde(r_timed, r_active)
    mde_core = te_mde(r_core_t, r_core_a)

    crit1 = {"ann>=2.02": stand["passive_timed"]["ann_pct"] >= 2.02,
             "dd>=-16.84": stand["passive_timed"]["dd_pct"] >= -16.84,
             "sharpe>=0.212": stand["passive_timed"]["sharpe"] >= 0.212}
    gate1 = all(crit1.values())
    crit2 = {"ann>=13.04": core_t["base"]["ann_pct"] >= 13.04,
             "dd>=-13.98": core_t["base"]["dd_pct"] >= -13.98,
             "sat5_margin>=0.63": core_t["sat5"]["margin_15"] >= 0.63,
             "sharpe>=1.43": core_t["base"]["sharpe"] >= 1.43}
    gate2 = all(crit2.values())

    out = {
        "hypothesis": "N=70 passive ETF + engine five-tier MA200 timing",
        "as_of": END,
        "window": f"{common.min().date()}~{common.max().date()}",
        "n": int(len(common)),
        "basis": {
            "timing": "exposure_ts.target (light panel, csi800-driven); tier0->0",
            "passive": "510300/510500 cum_nav total return, biweekly 50/50",
            "active": "engine 32w/group cached NAV (qfq contract, P0 blind spot)",
            "cb": "nav_snapdelay_par",
            "costs": f"commission {COMM}/side, cash {CASH_YIELD}/yr",
        },
        "regime": tstats,
        "standalone": stand,
        "core_40_60_timed": core_t,
        "core_40_60_active": {"base": core_a["base"],
                              "margin_15": core_a["margin_15"],
                              "sat5": core_a["sat5"]},
        "mde": {"stock_timed_vs_active": mde_stock,
                "core_timed_vs_active": mde_core},
        "criteria_gate1": crit1, "gate1_pass": gate1,
        "criteria_gate2": crit2, "gate2_pass": gate2,
        "blind_spots": [
            "active leg qfq old-history mixing (P0, unadjudicated)",
            "ETF cum_nav excludes secondary premium/discount changes",
            "timing target extracted from a light 2-ETF panel; signal is "
            "csi800-driven and identical to the full engine's state machine, "
            "but holiday rows are excluded via the csi800 calendar",
            "no MA10 exit/stop/overheat rules — pure beta + timing",
        ],
    }

    if args.oos and gate1:
        out["oos_2015_2018"] = oos_test()
    elif args.oos:
        out["oos_2015_2018"] = {"skipped": "gate1 failed"}

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({"standalone": stand, "gate1": gate1, "gate2": gate2,
                      "core_timed": core_t["base"],
                      "sat5": core_t["sat5"],
                      "mde_stock": mde_stock, "mde_core": mde_core},
                     ensure_ascii=False, indent=1, default=str), flush=True)
    print("->", OUT)
    return 0


def oos_test() -> dict:
    """2015-2018 股腿 OOS：择时 vs 无择时（CB 不可用）。

    起点用 2013-06：让 250 日 min_bars 暖机在 2015-01 前完成，否则 2015
    股灾会被"尚未建仓"而不是择时躲过（假保护）。"""
    cal = calendar("2013-06-01", "2018-12-31")
    rdf = etf_returns(cal.index)
    panel, amounts = light_panel(cal.index)
    sh = load_daily("SH000001", "2013-06-01", "2018-12-31")
    dates = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(dates, "biweekly") if d <= "2018-12-31"]
    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=400_000,
                  pool_size=2, max_position_pct=0.5, diag_exposure=True)
    _, m = run_backtest(panel, amounts, rebal, cfg, cal)
    ts = pd.DataFrame(m["exposure_ts"], columns=["date", "actual", "target", "nav"])
    ts["date"] = pd.to_datetime(ts["date"])
    ts = ts.set_index("date").sort_index()
    tgt = ts["target"].where(ts["target"] > 0.3, 0.0).reindex(cal.index).ffill()
    reb = set(rebal)
    r_etf = mix_returns(rdf["510300"], rdf["510500"], reb)
    r_timed = timed_sleeve(r_etf, tgt, reb)
    sub_t = r_timed[r_timed.index >= "2015-01-01"]
    sub_u = r_etf[r_etf.index >= "2015-01-01"]
    mt, mu = tp.metrics_ret(sub_t), tp.metrics_ret(sub_u)
    return {"timed": mt, "untimed": mu,
            "pass": (mu["dd_pct"] - mt["dd_pct"] >= 10.0
                     and mt["ann_pct"] >= mu["ann_pct"] - 2.0)}


if __name__ == "__main__":
    sys.exit(main())
