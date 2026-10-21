"""股腿被动化 A/B：主动引擎 vs 510300/510500 全收益（2026-10-21 挂账项）。

口径（判据见 logs/research_registration_20261021_stock_passive_ab.md）：
- 主动腿：股票引擎 32 万/组、lot=100、双周调仓（与三件套 Part B 同参）。
- 被动腿：510300/510500 各半，累计净值（全收益）日收益，月度回 50/50。
- 组合层：股票 64 万 + CB 96 万（nav_snapdelay_par，现产月度日程），
  月度回目标权重+日漂移，15% 熔断 B 模式网格。
- 被动占比 0/25/50/75/100%。
用法：python scripts/stock_leg_passive_ab.py
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
STOCK_CAP = 320_000.0
CORE_STOCK, CORE_CB, SATELLITE = 640_000.0, 960_000.0, 80_000.0
OUT = Path("logs/stock_leg_passive_ab_20261021.json")

# 复用三件套的 combo/metrics，避免两份实现漂移
_spec = importlib.util.spec_from_file_location(
    "tp4060", Path(__file__).parent / "three_piece_40_60.py")
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)


def blend(r1: pd.Series, r2: pd.Series, w2: float) -> pd.Series:
    """月度回目标权重+日漂移的两腿混合。"""
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


def core_metrics(r_stock: pd.Series, r_cb: pd.Series) -> dict:
    legs = pd.concat([r_stock.rename("s"), r_cb.rename("cb")],
                     axis=1, join="inner").dropna()
    tp.W_STOCK, tp.W_CB = CORE_STOCK / (CORE_STOCK + CORE_CB), \
        CORE_CB / (CORE_STOCK + CORE_CB)
    base_nav, _, _ = tp.combo(legs)
    r = base_nav.pct_change().fillna(0)
    out = {"base": tp.metrics_ret(r), "halts": {}}
    out["margin_15"] = round(15 + out["base"]["dd_pct"], 2)
    for tau in (-0.10, -0.15, -0.20, -0.25):
        h_nav, trig, dh = tp.combo(legs, halt=(tau, 0.02))
        m = tp.metrics_ret(h_nav.pct_change().fillna(0))
        out["halts"][str(tau)] = {**m, "triggers": trig, "days_halted": dh,
                                  "margin_15": round(15 + m["dd_pct"], 2)}
    return out


def te_mde(r_a: pd.Series, r_b: pd.Series) -> dict:
    idx = r_a.index.intersection(r_b.index)
    d = (r_a.reindex(idx) - r_b.reindex(idx)).dropna()
    if len(d) < 250:
        return {"n": int(len(d)), "TE": None, "SE": None, "MDE": None}
    te = float(d.std() * np.sqrt(252))
    years = len(d) / 252
    se = te / np.sqrt(years)
    return {"n": int(len(d)), "years": round(years, 2),
            "TE_pct": round(te * 100, 2), "SE_pp": round(se * 100, 2),
            "MDE_pp": round(1.96 * se * 100, 2)}


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--from-cache", action="store_true",
                     help="复用 logs/stock_leg_nav_32w_20261021.csv，跳过引擎")
    args = ap_.parse_args()
    cache = Path("logs/stock_leg_nav_32w_20261021.csv")
    if args.from_cache:
        if not cache.exists():
            print(f"CACHE_MISS {cache}")
            return 1
        nav = pd.read_csv(cache, index_col=0, parse_dates=True)["nav"]
        nav.index = pd.to_datetime(nav.index)
        r_active = nav.pct_change().dropna().rename("active")
        print(f"active from cache n={len(r_active)}", flush=True)
    else:
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
        print(f"panel {panel.shape[0]}天×{panel.shape[1]}只 rebal={len(rebal)}",
              flush=True)
        if panel.shape[1] < 4000:
            print(f"PANEL_FAIL width={panel.shape[1]} (<4000)")
            return 1

        cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=STOCK_CAP)
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        r_active = nav.pct_change().dropna().rename("active")
        nav.to_frame("nav").to_csv(cache)
        print(f"active 32万/组 trades={m.get('trades')} "
              f"lot_skips={m.get('lot_skips')}", flush=True)

    etf = pd.read_parquet("data_store/meta/etf_total_return.parquet")
    etf["date"] = pd.to_datetime(etf["date"])
    piv = etf.pivot(index="date", columns="code", values="cum_nav").sort_index()
    r300 = piv["510300"].pct_change(fill_method=None).dropna()
    r500 = piv["510500"].pct_change(fill_method=None).dropna()
    r_passive = blend(r300, r500, 0.5).rename("passive")
    # 统一样本窗：所有 arm 都裁到主动腿的索引（2019-01-02~2026-10-19），
    # 否则 100% 被动档会滑到 2013 起（含 2015 股灾），与其它档不可比。
    common = r_active.index.intersection(r_passive.index)
    r_active = r_active.reindex(common)
    r_passive = r_passive.reindex(common)
    r300 = r300.reindex(common)
    r500 = r500.reindex(common)
    print(f"common window {common.min().date()}~{common.max().date()} "
          f"n={len(common)}", flush=True)

    cb_nav = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0,
                         parse_dates=True)["nav_snapdelay_par"]
    r_cb = cb_nav.pct_change().dropna().rename("cb")

    out = {
        "as_of": "2026-10-19",
        "window": {"stock": f"{r_active.index.min().date()}~"
                            f"{r_active.index.max().date()}",
                   "etf": f"{r_passive.index.min().date()}~"
                          f"{r_passive.index.max().date()}",
                   "cb": f"{r_cb.index.min().date()}~{r_cb.index.max().date()}"},
        "basis": {"active": "引擎 qfq 契约（旧史复权混存=P0 盲区）",
                  "passive": "累计净值全收益，月度回50/50，不含溢价变动",
                  "cb": "nav_snapdelay_par（现产月度日程）"},
        "standalone_stock": {
            "active_32w": tp.metrics_ret(r_active),
            "passive_300_500_tr": tp.metrics_ret(r_passive),
            "510300_tr": tp.metrics_ret(r300),
            "510500_tr": tp.metrics_ret(r500),
        },
        "core_40_60": {},
        "core_40_60_sat5": {},
        "mde": {},
        "blind_spots": [
            "引擎旧史复权混存使主动腿 2019-2021 可能少计分红，偏向被动",
            "累计净值不含二级溢价/折价变动",
            "单市场单窗口，TE 估计有抽样误差；非新假设，不耗 OOS 预算",
        ],
    }
    def core_r(mix):
        legs = pd.concat([mix.rename("s"), r_cb.rename("cb")], axis=1,
                         join="inner").dropna()
        bnav, _, _ = tp.combo(legs)
        return bnav.pct_change().fillna(0)

    tp.W_STOCK, tp.W_CB = CORE_STOCK / (CORE_STOCK + CORE_CB), \
        CORE_CB / (CORE_STOCK + CORE_CB)
    r_core_active = core_r(r_active)
    out["mde"] = {"stock_vs_active": {}, "core_vs_active": {}}
    for wp in (0.0, 0.25, 0.5, 0.75, 1.0):
        r_mix = r_active if wp == 0 else (
            r_passive if wp == 1 else blend(r_active, r_passive, wp))
        key = f"passive_{int(wp*100)}"
        out["standalone_stock"][key] = tp.metrics_ret(r_mix)
        cm = core_metrics(r_mix, r_cb)
        out["core_40_60"][key] = cm
        # 5% 卫星（8万，CB-only 1x）：组合净值日收益与 CB 日收益月度回目标混合
        sat_key = f"passive_{int(wp*100)}"
        r_core_daily = r_core_active if wp == 0 else core_r(r_mix)
        r_sat = blend(r_core_daily, r_cb, SATELLITE / (CORE_STOCK + CORE_CB
                                                       + SATELLITE))
        m_sat = tp.metrics_ret(r_sat)
        out["core_40_60_sat5"][sat_key] = {
            "base": m_sat, "margin_15": round(15 + m_sat["dd_pct"], 2)}
        if wp > 0:
            out["mde"]["stock_vs_active"][key] = te_mde(r_mix, r_active)
            out["mde"]["core_vs_active"][key] = te_mde(r_core_daily,
                                                       r_core_active)
        print(key, "stock", out["standalone_stock"][key],
              "core", cm["base"], "margin15", cm["margin_15"],
              "sat5", {"ann": m_sat["ann_pct"], "dd": m_sat["dd_pct"],
                       "margin": round(15 + m_sat["dd_pct"], 2)},
              "MDE_core", (out["mde"]["core_vs_active"].get(key) or {}
                           ).get("MDE_pp"), flush=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
