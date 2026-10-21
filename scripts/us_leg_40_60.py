"""US 腿 40/60 三件套（2026-10-21，交接单挂账项）。

背景：10-20 的 US 分析是 **100/60 实盘名义**（股票 100万、CB 60万）。
现产核心是 **40/60**（股票 64万=2×32万/组、CB 96万）；US 腿从股票腿划拨
u×160万 → 股票 (64−160u) 万 → 每组分资金 (64−160u)/2 万，CB 仍 96 万。
美股腿用 **IXIC T-1（美东 T-1 收盘→中国 T）** 作主口径（指数本身、无溢价），
513100 二级市场价作载体参考（含 QDII 溢价，当前 >5% 不可买）。

窗口 2019-01~2026-10-19；引擎 lot=100、双周；CB `nav_snapdelay_par`
（现产月度 snapdelay）；腿间漂移 + 调仓日回目标权重，3bp/边；15% 熔断 B 网格。
用法：python scripts/us_leg_40_60.py
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
TOTAL = 1_600_000.0
STOCK0, CB0 = 640_000.0, 960_000.0
SAT = 80_000.0
COST = 0.0003
US = {0.0: 320_000.0, 0.10: 240_000.0, 0.125: 220_000.0, 0.15: 200_000.0}
OUT = Path("logs/us_leg_40_60_20261021.json")


def metrics(ret):
    ret = ret.dropna()
    nav = (1 + ret).cumprod()
    years = len(ret) / 252
    ann = nav.iloc[-1] ** (1 / years) - 1
    sr = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
    dd = (nav / nav.cummax() - 1).min()
    return {"ann_pct": round(float(ann) * 100, 2),
            "sharpe": round(float(sr), 3),
            "dd_pct": round(float(dd) * 100, 2), "n": int(len(ret))}


def sim(legs, weights, rebal, halt=None, cost=COST):
    """三腿：调仓日回目标权重；15% B 熔断=冻结调仓至回撤回到 τ+2pp。"""
    cols = list(legs.columns)
    w = np.array([weights[c] for c in cols], float)
    vals = w.copy()
    nav, peak = 1.0, 1.0
    halted, trig, dh = False, 0, 0
    rset = set(str(d)[:10] for d in rebal)
    navs = []
    for dt, row in legs.iterrows():
        vals = vals * (1 + row.values)
        tot = vals.sum()
        if tot > 0:
            vals = vals / tot
        r = float(np.dot(vals, row.values))
        nav *= (1 + r)
        navs.append(nav)
        peak = max(peak, nav)
        dd = nav / peak - 1
        if halt is not None:
            tau, rec = halt
            if not halted and dd <= tau:
                halted = True
                trig += 1
            elif halted and dd >= tau + rec:
                halted = False
                vals = w.copy()
        if halted:
            dh += 1
        elif str(dt.date()) in rset:
            cost_r = cost * float(np.abs(w - vals).sum()) / 2
            nav *= (1 - cost_r)
            navs[-1] = nav
            vals = w.copy()
    ret = pd.Series(navs, index=legs.index).pct_change().fillna(0)
    return ret, trig, dh


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


def te_mde(r_a, r_b):
    idx = r_a.index.intersection(r_b.index)
    d = (r_a.reindex(idx) - r_b.reindex(idx)).dropna()
    te = float(d.std() * np.sqrt(252))
    years = len(d) / 252
    se = te / np.sqrt(years)
    return {"n": int(len(d)), "TE_pct": round(te * 100, 2),
            "SE_pp": round(se * 100, 2), "MDE_pp": round(1.96 * se * 100, 2)}


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


def main() -> int:
    panel, ap, rebal, idx_c = load_panel()
    print(f"panel {panel.shape[0]}x{panel.shape[1]}", flush=True)
    if panel.shape[1] < 4000:
        print("PANEL_FAIL")
        return 1

    navs = {}
    for u, cap in US.items():
        cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=cap,
                      diag_exposure=True)
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        navs[u] = nav
        nav.to_frame("nav").to_csv(f"logs/us_leg_40_60_nav_u{int(u*1000)}.csv")
        print(f"u={u} cap={cap:,.0f} trades={m.get('trades')} "
              f"skips={m.get('lot_skips')}", flush=True)

    cb_nav = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0,
                         parse_dates=True)["nav_snapdelay_par"]
    r_cb = cb_nav.pct_change().dropna()

    ix = pd.read_parquet("data_store/meta/us_ixic_sina.parquet")
    ix["date"] = pd.to_datetime(ix["date"])
    ix = ix.set_index("date")["close"].astype(float).sort_index()
    r_ix = ix.pct_change().shift(1).dropna()
    etf = pd.read_parquet("data_store/meta/etf_513100_sina.parquet")
    etf["date"] = pd.to_datetime(etf["date"])
    etf = etf.set_index("date")["close"].astype(float).sort_index()
    r_etf = etf.pct_change().dropna()

    out = {"as_of": END, "core": "40/60 (stock 640k=2x cap, CB 960k)",
           "funding": "US carved from stock leg; CB fixed 960k",
           "us_primary": "IXIC T-1 (index, no premium)",
           "us_reference": "513100 secondary price (premium-inclusive)",
           "window": None, "arms": {}}
    base_ret = None
    for us_name, us_ret in (("IXIC_T1", r_ix), ("513100", r_etf)):
        for u, nav in navs.items():
            r_s = nav.pct_change().dropna()
            legs = pd.concat([r_s.rename("s"), r_cb.rename("cb"),
                              us_ret.rename("us")], axis=1,
                             join="inner").dropna()
            if len(legs) < 500:
                continue
            w = {"s": (STOCK0 - u * TOTAL) / TOTAL,
                 "cb": CB0 / TOTAL, "us": u}
            ret, trig, dh = sim(legs, w, rebal)
            m = metrics(ret)
            sat = blend(ret, r_cb, SAT / (TOTAL + SAT))
            ms = metrics(sat)
            key = f"{us_name}_u{int(u*1000)}"
            out["arms"][key] = {
                "weights": {k: round(v, 4) for k, v in w.items()},
                **m, "sat5": ms,
                "sat5_margin15": round(15 + ms["dd_pct"], 2),
                "halts": {}}
            for tau in (-0.10, -0.15, -0.20, -0.25):
                hr, t, d = sim(legs, w, rebal, halt=(tau, 0.02))
                out["arms"][key]["halts"][str(tau)] = {
                    **metrics(hr), "triggers": t, "days_halted": d}
            if base_ret is None and us_name == "IXIC_T1" and u == 0:
                base_ret = ret
            out["window"] = f"{legs.index.min().date()}~{legs.index.max().date()}"
            print(key, m, "sat5", ms, flush=True)

    if base_ret is not None:
        out["mde_vs_u0"] = {}
        # 用 u=0 与各 u 的日收益（IXIC 主口径）重算 MDE
        legs0 = pd.concat([navs[0.0].pct_change().rename("s"),
                           r_cb.rename("cb")], axis=1, join="inner").dropna()
        ret0, _, _ = sim(legs0, {"s": STOCK0 / TOTAL, "cb": CB0 / TOTAL}, rebal)
        for u in (0.10, 0.125, 0.15):
            r_s = navs[u].pct_change().dropna()
            legs = pd.concat([r_s.rename("s"), r_cb.rename("cb"),
                              r_ix.rename("us")], axis=1,
                             join="inner").dropna()
            w = {"s": (STOCK0 - u * TOTAL) / TOTAL, "cb": CB0 / TOTAL, "us": u}
            ret, _, _ = sim(legs, w, rebal)
            out["mde_vs_u0"][f"IXIC_T1_u{int(u*1000)}"] = {
                "delta_ann_pp": round(
                    metrics(ret)["ann_pct"] - metrics(ret0)["ann_pct"], 2),
                **te_mde(ret, ret0)}

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(out["arms"], ensure_ascii=False, indent=1, default=str)[:3000])
    print("MDE", json.dumps(out.get("mde_vs_u0", {}), ensure_ascii=False))
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
