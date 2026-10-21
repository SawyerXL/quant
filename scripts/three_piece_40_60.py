"""40/60 配比三件套（用户 2026-10-21 裁定“先验证再动”）。

口径：股票腿 64 万（2×32 万/组，lot=100，实盘日程）+ CB 腿 96 万
（nav_biweekly_par，实盘 CB 双周 NAV）；月度回目标权重+日漂移；0 成本基准，
另报 0.13% 再平衡成本敏感性。
- Part A：组合画像 + 15% 熔断 B 模式网格（冻结权重至回撤回到 τ+2pp）。
- Part B：股票腿 32 万/组跳票/覆盖率（引擎 lot_skips/trades）+ 50 万/组对照。
窗口：股票引擎 2019-01~2026-10-19 ∩ CB NAV 至 2026-10-09。
用法：python scripts/three_piece_40_60.py
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
from backtest_engine import run_backtest, make_rebal_dates, calc_metrics

START, END = "2019-01-01", "2026-10-19"
W_STOCK, W_CB = 0.4, 0.6
OUT = Path("logs/three_piece_40_60_20261021.json")


def metrics_ret(r):
    r = r.dropna()
    nav = (1 + r).cumprod()
    y = len(r) / 252
    return {"n": int(len(r)),
            "ann_pct": round(float((nav.iloc[-1] ** (1 / y) - 1) * 100), 2),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 3),
            "dd_pct": round(float((nav / nav.cummax() - 1).min() * 100), 2)}


def combo(legs, halt=None, cost=0.0):
    """legs: DataFrame s,cb；月度回目标权重+日漂移；halt=(tau, recover_pp) 或 None。"""
    nav, peak = 1.0, 1.0
    w = np.array([W_STOCK, W_CB], float)
    target = w.copy()
    halted = False
    last_month = None
    trig = 0
    days_halted = 0
    navs = []
    for dt, row in legs.iterrows():
        ym = (dt.year, dt.month)
        if ym != last_month:
            last_month = ym
            if not halted:
                nav *= (1 - cost * float(np.abs(target - w).sum()) / 2)
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
        if halted:
            days_halted += 1
        navs.append(nav)
        w = w * (1 + vals)
        w = w / w.sum()
    return pd.Series(navs, index=legs.index), trig, days_halted


def main():
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
    print(f"panel {panel.shape[0]}天×{panel.shape[1]}只, rebal {len(rebal)}", flush=True)

    out = {"as_of": "2026-10-19", "weights": [W_STOCK, W_CB],
           "stock_per_group": 320_000, "cb_capital": 960_000, "part_b": {}}
    # Part B: 32万/组 与 50万/组对照
    navs = {}
    for cap in (320_000.0, 500_000.0):
        cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=cap)
        nav, m = run_backtest(panel, ap, rebal, cfg, idx_c)
        navs[cap] = nav
        cm = calc_metrics(nav)
        out["part_b"][f"{cap/10000:.0f}万/组"] = {
            "ann": cm["年化收益率"], "sharpe": cm["夏普比率"],
            "dd": cm["最大回撤"], "lot_skips": int(m.get("lot_skips", 0)),
            "trades": int(m.get("trades", 0))}
        print("PartB", cap, out["part_b"][f"{cap/10000:.0f}万/组"], flush=True)

    r_s = navs[320_000.0].pct_change().dropna()
    ncb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0, parse_dates=True)
    r_cb = ncb["nav_biweekly_par"].pct_change().dropna()
    legs = pd.concat([r_s.rename("s"), r_cb.rename("cb")], axis=1,
                     join="inner").dropna()
    print(f"combo window {legs.index.min().date()}~{legs.index.max().date()} n={len(legs)}",
          flush=True)
    base_nav, _, _ = combo(legs)
    r = base_nav.pct_change().fillna(0)
    out["part_a"] = {"base": metrics_ret(r)}
    out["part_a"]["halts"] = {}
    for tau in (-0.10, -0.15, -0.20, -0.25):
        h_nav, trig, dh = combo(legs, halt=(tau, 0.02))
        hr = h_nav.pct_change().fillna(0)
        mh = metrics_ret(hr)
        mb = metrics_ret(r)
        out["part_a"]["halts"][str(tau)] = {
            **mh, "triggers": trig, "days_halted": dh,
            "damage_ann_pp": round(mh["ann_pct"] - mb["ann_pct"], 3),
            "damage_dd_pp": round(mh["dd_pct"] - mb["dd_pct"], 3)}
        print("halt", tau, out["part_a"]["halts"][str(tau)], flush=True)
    # 成本敏感性
    c_nav, _, _ = combo(legs, cost=0.0013)
    out["part_a"]["cost_0.13"] = metrics_ret(c_nav.pct_change().fillna(0))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("->", OUT)


if __name__ == "__main__":
    main()
