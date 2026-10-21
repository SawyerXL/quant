"""量化 MA10 排空不重归一 bug 的影响面（2026-10-21，沙箱引擎）。

同一面板/配置（32万/组、lot=100、双周、2019-01~2026-10-19）并排跑：
- 生产引擎 `backtest_engine.run_backtest`（幸存者旧权重，现金只给新票）
- 沙箱 `backtest_engine_renorm.run_backtest`（统一目标权重 + |Δw| 双侧佣金）
比较股腿净值、暴露缺口、40/60（CB nav_snapdelay_par）、5%卫星、15%熔断。
不改生产文件/配置；结论只用于决定是否重跑校准。
用法：python scripts/engine_renorm_impact.py
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
from backtest_engine import run_backtest as run_buggy
from backtest_engine import make_rebal_dates

_spec = importlib.util.spec_from_file_location(
    "eng_renorm", Path(__file__).parent / "backtest_engine_renorm.py")
_eng = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_eng)
run_fix = _eng.run_backtest

_tp_spec = importlib.util.spec_from_file_location(
    "tp4060", Path(__file__).parent / "three_piece_40_60.py")
tp = importlib.util.module_from_spec(_tp_spec)
_tp_spec.loader.exec_module(tp)

START, END = "2019-01-01", "2026-10-19"
CAP = 320_000.0
CORE_STOCK, CORE_CB, SAT = 640_000.0, 960_000.0, 80_000.0
OUT = Path("logs/engine_renorm_impact_20261021.json")


def load_panel():
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
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    return panel, ap, rebal, idx_c


def mret(r):
    return tp.metrics_ret(r.dropna())


def blend(r1, r2, w2):
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


def core(r_stock, r_cb):
    legs = pd.concat([r_stock.rename("s"), r_cb.rename("cb")],
                     axis=1, join="inner").dropna()
    tp.W_STOCK, tp.W_CB = CORE_STOCK / (CORE_STOCK + CORE_CB), \
        CORE_CB / (CORE_STOCK + CORE_CB)
    nav, _, _ = tp.combo(legs)
    r = nav.pct_change().fillna(0)
    m = mret(r)
    out = {"base": m, "margin15": round(15 + m["dd_pct"], 2), "halts": {}}
    for tau in (-0.10, -0.15, -0.20, -0.25):
        hn, trig, dh = tp.combo(legs, halt=(tau, 0.02))
        mm = mret(hn.pct_change().fillna(0))
        out["halts"][str(tau)] = {**mm, "triggers": trig, "days_halted": dh}
    sat = blend(r, r_cb, SAT / (CORE_STOCK + CORE_CB + SAT))
    ms = mret(sat)
    out["sat5"] = {**ms, "margin15": round(15 + ms["dd_pct"], 2)}
    return out, r


def exposure_stats(m):
    ts = pd.DataFrame(m["exposure_ts"], columns=["date", "actual", "target", "nav"])
    ts["date"] = pd.to_datetime(ts["date"])
    ts = ts.set_index("date")
    tgt = ts["target"].where(ts["target"] > 0.3, 0.0)  # tier0 清仓
    gap = (tgt - ts["actual"]).clip(lower=0)
    return {
        "days": int(len(ts)),
        "mean_actual": round(float(ts["actual"].mean()), 4),
        "mean_target": round(float(tgt.mean()), 4),
        "mean_gap": round(float(gap.mean()), 4),
        "days_gap_gt_2pp": int((gap > 0.02).sum()),
        "days_gap_gt_10pp": int((gap > 0.10).sum()),
        "days_actual_lt_80pct_target": int(
            (ts["actual"] < 0.8 * tgt).sum()),
        "max_gap": round(float(gap.max()), 4),
    }


def main() -> int:
    global CAP, CORE_STOCK, CORE_CB, OUT
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--cap", type=float, default=CAP, help="每股资金")
    ap_.add_argument("--w-stock", type=float, default=0.4, help="股票腿权重")
    ap_.add_argument("--tag", default=None)
    args = ap_.parse_args()
    CAP = args.cap
    CORE_STOCK = 1_600_000.0 * args.w_stock
    CORE_CB = 1_600_000.0 * (1 - args.w_stock)
    OUT = Path(f"logs/engine_renorm_impact_{args.tag or 'base'}_20261021.json")
    print(f"config cap={CAP:,.0f} w_stock={args.w_stock:.3f} "
          f"stock={CORE_STOCK:,.0f} cb={CORE_CB:,.0f}", flush=True)
    panel, ap, rebal, idx_c = load_panel()
    print(f"panel {panel.shape[0]}x{panel.shape[1]} rebal={len(rebal)}",
          flush=True)
    if panel.shape[1] < 4000:
        print("PANEL_FAIL")
        return 1
    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=CAP,
                  diag_exposure=True)
    nav_b, m_b = run_buggy(panel, ap, rebal, cfg, idx_c)
    print("buggy done", m_b.get("trades"), flush=True)
    nav_f, m_f = run_fix(panel, ap, rebal, cfg, idx_c)
    print("fixed done", m_f.get("trades"), flush=True)

    r_b = nav_b.pct_change().dropna()
    r_f = nav_f.pct_change().dropna()
    common = r_b.index.intersection(r_f.index)
    r_b, r_f = r_b.reindex(common), r_f.reindex(common)

    cb_nav = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0,
                         parse_dates=True)["nav_snapdelay_par"]
    r_cb = cb_nav.pct_change().dropna()

    core_b, rc_b = core(r_b, r_cb)
    core_f, rc_f = core(r_f, r_cb)
    sat_b, sat_f = core_b["sat5"], core_f["sat5"]

    # 分年净值差（看 bug 集中在哪些年份）
    by_year = {}
    for y in range(2019, 2027):
        a = nav_b[nav_b.index.year == y]
        b = nav_f[nav_f.index.year == y]
        if len(a) > 20 and len(b) > 20:
            by_year[str(y)] = {
                "buggy_pct": round(float(a.iloc[-1] / a.iloc[0] - 1) * 100, 2),
                "fixed_pct": round(float(b.iloc[-1] / b.iloc[0] - 1) * 100, 2),
            }

    out = {
        "as_of": END,
        "config": {"stock_cap_per_group": CAP,
                   "w_stock": round(CORE_STOCK / (CORE_STOCK + CORE_CB), 4),
                   "w_cb": round(CORE_CB / (CORE_STOCK + CORE_CB), 4),
                   "satellite": SAT},
        "sandbox": "backtest_engine_renorm.py (only rebalance block differs)",
        "window": f"{common.min().date()}~{common.max().date()}",
        "stock_leg": {
            "buggy": mret(r_b), "fixed": mret(r_f),
            "fixed_minus_buggy_ann_pp": round(
                mret(r_f)["ann_pct"] - mret(r_b)["ann_pct"], 2),
            "final_nav_buggy": round(float(nav_b.iloc[-1]), 4),
            "final_nav_fixed": round(float(nav_f.iloc[-1]), 4),
        },
        "exposure": {"buggy": exposure_stats(m_b), "fixed": exposure_stats(m_f)},
        "by_year_stock": by_year,
        "core_40_60": {"buggy": core_b, "fixed": core_f},
        "delta_core": {
            "ann_pp": round(core_f["base"]["ann_pct"] - core_b["base"]["ann_pct"], 2),
            "sharpe": round(core_f["base"]["sharpe"] - core_b["base"]["sharpe"], 3),
            "dd_pp": round(core_f["base"]["dd_pct"] - core_b["base"]["dd_pct"], 2),
            "sat5_ann_pp": round(sat_f["ann_pct"] - sat_b["ann_pct"], 2),
            "sat5_dd_pp": round(sat_f["dd_pct"] - sat_b["dd_pct"], 2),
            "sat5_margin_pp": round(sat_f["margin15"] - sat_b["margin15"], 2),
        },
        "note": ("生产文件未改；沙箱仅用于量化。若偏移显著，现有 40/60、"
                 "被动化 A/B、50/50、N=70 结论都需在修复引擎上重跑。"),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: out[k] for k in
                      ("stock_leg", "exposure", "by_year_stock",
                       "delta_core")}, ensure_ascii=False, indent=1,
                     default=str), flush=True)
    print("core buggy", core_b["base"], "sat5", sat_b)
    print("core fixed", core_f["base"], "sat5", sat_f)
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
