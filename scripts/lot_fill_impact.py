"""股票腿 lot 跳票现金再分配 — 影响面（2026-10-21，沙箱引擎）。

预注册 `logs/research_registration_20261021_lotfill.md`（16:20:16）。
沙箱 `backtest_engine_lotfill.py` = 重归一 + lot-fill（跳票权重分配给
买得起的剩余票，单票 ≤5%，过热/跌停不参与）。生产引擎未改。
对比：现产 32万/组缓存 NAV（1.50%/935 跳票）vs lot-fill。
用法：python scripts/lot_fill_impact.py
"""
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
from backtest_engine import make_rebal_dates

_spec = importlib.util.spec_from_file_location(
    "eng_lotfill", Path(__file__).parent / "backtest_engine_lotfill.py")
_eng = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_eng)
run_fill = _eng.run_backtest

_tp = importlib.util.spec_from_file_location(
    "tp4060", Path(__file__).parent / "three_piece_40_60.py")
tp = importlib.util.module_from_spec(_tp)
_tp.loader.exec_module(tp)

START, END = "2019-01-01", "2026-10-19"
CAP = 320_000.0
OUT = Path("logs/lot_fill_impact_20261021.json")


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
    se = te / np.sqrt(len(d) / 252)
    return {"n": int(len(d)), "TE_pct": round(te * 100, 2),
            "SE_pp": round(se * 100, 2), "MDE_pp": round(1.96 * se * 100, 2)}


def core(r_stock, r_cb):
    legs = pd.concat([r_stock.rename("s"), r_cb.rename("cb")],
                     axis=1, join="inner").dropna()
    tp.W_STOCK, tp.W_CB = 0.4, 0.6
    nav, _, _ = tp.combo(legs)
    r = nav.pct_change().fillna(0)
    m = tp.metrics_ret(r)
    out = {"base": m, "margin15": round(15 + m["dd_pct"], 2), "halts": {}}
    for tau in (-0.10, -0.15, -0.20, -0.25):
        hn, trig, dh = tp.combo(legs, halt=(tau, 0.02))
        mm = tp.metrics_ret(hn.pct_change().fillna(0))
        out["halts"][str(tau)] = {**mm, "triggers": trig, "days_halted": dh}
    sat = blend(r, r_cb, 80_000 / 1_680_000)
    ms = tp.metrics_ret(sat)
    out["sat5"] = {**ms, "margin15": round(15 + ms["dd_pct"], 2)}
    return out, r


def exposure(m):
    ts = pd.DataFrame(m["exposure_ts"], columns=["date", "actual", "target", "nav"])
    tgt = ts["target"].where(ts["target"] > 0.3, 0.0)
    gap = (tgt - ts["actual"]).clip(lower=0)
    return {"days": int(len(ts)),
            "mean_actual": round(float(ts["actual"].mean()), 4),
            "mean_target": round(float(tgt.mean()), 4),
            "mean_gap": round(float(gap.mean()), 4),
            "days_gap_gt_10pp": int((gap > 0.10).sum())}


def load_panel():
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    meta = load_meta("stock_info_full")
    prices, amounts = {}, {}
    for code in meta["code"].tolist() if not meta.empty else []:
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
    idx = load_meta("csi800_index")
    idx_c = idx.set_index("date")["close"].sort_index()
    idx_c.index = pd.to_datetime(idx_c.index)
    return panel, pd.DataFrame(amounts).sort_index(), rebal, idx_c


def main() -> int:
    panel, ap, rebal, idx_c = load_panel()
    print(f"panel {panel.shape[0]}x{panel.shape[1]}", flush=True)
    cfg = replace(DEFAULT_CONFIG, lot_size=100, initial_capital=CAP,
                  diag_exposure=True)
    nav_f, m_f = run_fill(panel, ap, rebal, cfg, idx_c)
    print(f"lotfill done trades={m_f.get('trades')} skips={m_f.get('lot_skips')}",
          flush=True)
    r_f = nav_f.pct_change().dropna()

    cur = pd.read_csv("logs/stock_leg_nav_32w_20261021.csv",
                      index_col=0, parse_dates=True)["nav"]
    r_c = cur.pct_change().dropna()
    common = r_c.index.intersection(r_f.index)
    r_c, r_f = r_c.reindex(common), r_f.reindex(common)

    cb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0,
                     parse_dates=True)["nav_snapdelay_par"]
    r_cb = cb.pct_change().dropna()

    c_cur, cr_cur = core(r_c, r_cb)
    c_fill, cr_fill = core(r_f, r_cb)

    out = {
        "as_of": END,
        "sandbox": "backtest_engine_lotfill.py (renorm + lot-fill, 5% cap)",
        "window": f"{common.min().date()}~{common.max().date()}",
        "stock_leg": {"current": tp.metrics_ret(r_c),
                      "lotfill": tp.metrics_ret(r_f)},
        "exposure_lotfill": exposure(m_f),
        "core_40_60": {"current": c_cur, "lotfill": c_fill},
        "delta": {
            "stock_ann_pp": round(tp.metrics_ret(r_f)["ann_pct"]
                                  - tp.metrics_ret(r_c)["ann_pct"], 2),
            "core_ann_pp": round(c_fill["base"]["ann_pct"]
                                 - c_cur["base"]["ann_pct"], 2),
            "core_dd_pp": round(c_fill["base"]["dd_pct"]
                                - c_cur["base"]["dd_pct"], 2),
            "sat5_margin_pp": round(c_fill["sat5"]["margin15"]
                                    - c_cur["sat5"]["margin15"], 2),
        },
        "mde_core": te_mde(cr_fill, cr_cur),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: out[k] for k in
                      ("stock_leg", "exposure_lotfill", "delta", "mde_core")},
                     ensure_ascii=False, indent=1, default=str))
    print("core current", c_cur["base"], "lotfill", c_fill["base"])
    print("sat5 current", c_cur["sat5"], "lotfill", c_fill["sat5"])
    print("->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
