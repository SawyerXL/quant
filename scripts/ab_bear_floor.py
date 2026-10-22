"""熊市档 30% vs 0% A/B（2026-10-22）。

判据见 logs/prereg_bear_floor_20261022.md（先登记后出数）。
只读回测，不改实盘；两臂仅差 ma200_bear_pos。
"""
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd

from data.storage import load_daily, load_meta
from backtest_config import DEFAULT_CONFIG
from backtest_engine import run_backtest, make_rebal_dates

START = "2019-01-01"


def main():
    t0 = time.time()
    cb = pd.read_csv("logs/cb_sched_compare_nav.csv", index_col=0, parse_dates=True)
    END = str(cb.index[-1].date())
    print(f"END={END} START={START}", flush=True)

    meta = load_meta("stock_info_full")
    codes = meta["code"].tolist()
    prices, amounts = {}, {}
    for k, code in enumerate(codes):
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
        if (k + 1) % 1000 == 0:
            print(f"load {k+1}/{len(codes)} {time.time()-t0:.0f}s", flush=True)

    panel = pd.DataFrame(prices).sort_index()
    ap = pd.DataFrame(amounts).sort_index()
    ic = load_meta("csi800_index")
    ic = ic.set_index("date")["close"].sort_index()
    ic.index = pd.to_datetime(ic.index)
    sh = load_daily("SH000001", "2014-06-01", END)
    cal = sorted(set(pd.to_datetime(sh["date"]).astype(str).str[:10].tolist()))
    rebal = [d for d in make_rebal_dates(cal, "biweekly") if START <= d <= END]
    print(f"panel {panel.shape} rebal {len(rebal)}", flush=True)

    base = replace(DEFAULT_CONFIG, cash_yield=0.014, limit_gate=True, diag_exposure=True)
    # 2026-10-22: ma200_bear_pos 已删除(诊断为 no-op, A/B 两臂逐位一致);
    # 本脚本保留为历史工件: 两臂同配置，仅用于复现当时的 exposure 诊断。
    arms = {"bear30": base, "bear0": base}
    navs, results = {}, {}
    for name, cfg in arms.items():
        print(f"run {name} ...", flush=True)
        nav, res = run_backtest(panel, ap, rebal, cfg, ic)
        navs[name], results[name] = nav, res
        print(f"done {name} {time.time()-t0:.0f}s", flush=True)

    def stats(nav):
        r = nav.pct_change().dropna()
        days = (nav.index[-1] - nav.index[0]).days
        ann = nav.iloc[-1] ** (365 / days) - 1
        sr = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else float("nan")
        dd = (nav / nav.cummax() - 1).min()
        w = nav.loc["2026-07-20":]
        y = nav.loc["2026-01-01":]
        return {
            "total": round(float(nav.iloc[-1] - 1), 4),
            "ann": round(float(ann), 4),
            "sharpe": round(float(sr), 3),
            "maxdd": round(float(dd), 4),
            "w_20260720": round(float(w.iloc[-1] / w.iloc[0] - 1), 4) if len(w) > 1 else None,
            "ytd_2026": round(float(y.iloc[-1] / y.iloc[0] - 1), 4) if len(y) > 1 else None,
        }

    out = {n: stats(navs[n]) for n in arms}

    ex = {}
    for n, res in results.items():
        df = pd.DataFrame(res["exposure_ts"], columns=["date", "actual", "target", "nav"])
        df["date"] = pd.to_datetime(df["date"])
        bear = df[df["target"] <= 0.301]
        recent = df[df["date"] >= "2026-07-20"]
        ex[n] = {
            "n_days": int(len(df)),
            "avg_actual": round(float(df["actual"].mean()), 4),
            "bear_days": int(len(bear)),
            "bear_avg_actual": round(float(bear["actual"].mean()), 4) if len(bear) else None,
            "bear_avg_target": round(float(bear["target"].mean()), 4) if len(bear) else None,
            "recent_avg_actual": round(float(recent["actual"].mean()), 4) if len(recent) else None,
            "tier_sells": float(res.get("tier_sells", 0.0)),
            "tier_switch_count": int(res.get("tier_switch_count", 0)),
        }

    r0 = navs["bear0"].pct_change().dropna()
    r3 = navs["bear30"].pct_change().dropna()
    idx = r0.index.intersection(r3.index)
    d = (r0.loc[idx] - r3.loc[idx]).dropna()
    t = float(d.mean() / d.std() * np.sqrt(len(d))) if len(d) > 1 and d.std() > 0 else None
    se_ann = float(d.std() * np.sqrt(252) / np.sqrt(len(d))) if len(d) > 1 else None
    diff = {
        "ann_bear0_minus_bear30": round(out["bear0"]["ann"] - out["bear30"]["ann"], 4),
        "mean_daily_diff": round(float(d.mean()), 6),
        "t": round(t, 2) if t is not None else None,
        "se_ann": round(se_ann, 4) if se_ann is not None else None,
        "mde_ann": round(1.96 * se_ann, 4) if se_ann is not None else None,
        "corr": round(float(r0.loc[idx].corr(r3.loc[idx])), 4),
    }

    payload = {
        "as_of": END, "start": START,
        "data": "current data_store (qfq D rebuild, 2026-10-22)",
        "arms": {n: {"cash_yield": c.cash_yield,
                     "limit_gate": c.limit_gate, "pool_size": c.pool_size,
                     "ma200_thresh_shift": c.ma200_thresh_shift}
                 for n, c in arms.items()},
        "stats": out, "exposure": ex, "diff": diff,
        "prereg": "logs/prereg_bear_floor_20261022.md",
    }
    Path("logs/ab_bear_floor_20261022.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.DataFrame(navs).to_csv("logs/ab_bear_floor_nav_20261022.csv")
    print(json.dumps(payload, ensure_ascii=False, indent=1), flush=True)


if __name__ == "__main__":
    main()
