"""季末窗口粉饰（window dressing）— 约束驱动候选。

预注册：logs/prereg_window_dressing_20261020.md（N=66，判据出数前锁）。
用法：python scripts/window_dressing_study.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-10-19"
COST_LEG = 0.0033
COST_SPREAD = 2 * COST_LEG


def band_of(code):
    c = str(code)
    if c.startswith(("8", "4", "92")):
        return 0.30
    if c.startswith(("688", "300", "301")):
        return 0.20
    return 0.10


def load_panel():
    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st = dict(zip(meta["code"],
                  meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    codes = sorted(c for c in meta["code"] if not st.get(c, False))
    cl, op = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 300:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            c = pd.to_numeric(d["close"], errors="coerce").dropna()
            if len(c) < 300:
                continue
            cl[code] = c
            op[code] = pd.to_numeric(d["open"], errors="coerce").reindex(c.index)
        except Exception:
            continue
    return pd.DataFrame(cl).sort_index(), pd.DataFrame(op).sort_index()


def quarter_ends(dates):
    out = []
    for y in range(2019, 2027):
        for m in (3, 6, 9, 12):
            idx = [i for i, d in enumerate(dates) if d.year == y and d.month == m]
            if idx:
                out.append(idx[-1])
    return sorted(set(out))


def tstat(x):
    x = pd.Series(x).dropna()
    if len(x) < 2 or x.std() == 0:
        return None
    return float(x.mean() / (x.std() / np.sqrt(len(x))))


def main():
    cl, op = load_panel()
    dates = cl.index
    op0 = op.replace(0, np.nan)
    ret = cl.pct_change()
    band = pd.Series({c: band_of(c) for c in cl.columns})
    lim_up = cl.shift(1).mul(1 + band, axis=1).round(2)
    lim_dn = cl.shift(1).mul(1 - band, axis=1).round(2)
    print(f"panel {cl.shape[0]}天×{cl.shape[1]}只")

    qes = [i for i in quarter_ends(dates) if i >= 60]
    print("quarter ends", len(qes), dates[qes[0]].date(), dates[qes[-1]].date())

    recs = []
    for qi, qe in enumerate(qes):
        ws = qe - 4
        sig = qe - 5
        prev = [q for q in qes if q < qe]
        if not prev or sig < 0 or ws < 0:
            continue
        pqe = prev[-1]
        qtd = cl.iloc[sig] / cl.iloc[pqe] - 1
        entry = op0.iloc[ws]
        exitp = cl.iloc[qe]
        jump = ret.iloc[ws:qe + 1].abs().max()
        lu = lim_up.iloc[ws]
        ld = lim_dn.iloc[qe]
        valid = (qtd.notna() & entry.notna() & exitp.notna()
                 & (entry > 0) & (exitp > 0)
                 & ~(lu.notna() & (entry >= lu - 1e-6))
                 & ~(ld.notna() & (exitp <= ld + 1e-6))
                 & ~(jump.notna() & (jump > 0.5)))
        q = qtd[valid]
        if len(q) < 1000:
            continue
        r = (exitp / entry - 1)[valid]
        rk = q.rank(pct=True, method="average")
        win = rk > 0.9
        los = rk <= 0.1
        ew = r.mean()
        rec = {"qe": dates[qe].strftime("%Y-%m-%d"), "n": int(valid.sum()),
               "n_win": int(win.sum()), "n_los": int(los.sum()),
               "win": float(r[win].mean()), "los": float(r[los].mean()),
               "ew": float(ew),
               "spread": float(r[win].mean() - r[los].mean()),
               "win_ew": float(r[win].mean() - ew),
               "year": dates[qe].year}
        # 修正窗：下一季度前 5 日
        if qe + 5 < len(dates):
            e2 = op0.iloc[qe + 1]
            x2 = cl.iloc[qe + 5]
            lu2 = lim_up.iloc[qe + 1]
            ld2 = lim_dn.iloc[qe + 5]
            j2 = ret.iloc[qe + 1:qe + 6].abs().max()
            v2 = (qtd.notna() & e2.notna() & x2.notna() & (e2 > 0) & (x2 > 0)
                  & ~(lu2.notna() & (e2 >= lu2 - 1e-6))
                  & ~(ld2.notna() & (x2 <= ld2 + 1e-6))
                  & ~(j2.notna() & (j2 > 0.5)))
            rr = (x2 / e2 - 1)[v2]
            rk2 = qtd[v2].rank(pct=True, method="average")
            rec["rev_win"] = float(rr[rk2 > 0.9].mean())
            rec["rev_los"] = float(rr[rk2 <= 0.1].mean())
            rec["rev_spread"] = rec["rev_win"] - rec["rev_los"]
            rec["rev_n"] = int(v2.sum())
        recs.append(rec)

    d = pd.DataFrame(recs)
    d["seg"] = np.where(d["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
    d["spread_net"] = d["spread"] - COST_SPREAD

    out = {"prereg": "logs/prereg_window_dressing_20261020.md",
           "n_quarters": int(len(d)), "cost_spread": COST_SPREAD,
           "period": [d.qe.min(), d.qe.max()]}

    def agg(sub):
        s = sub["spread_net"]
        return {"n_q": int(len(sub)), "mean_spread_pct": float(s.mean() * 100),
                "median_spread_pct": float(s.median() * 100),
                "sd_q_pct": float(s.std() * 100),
                "t_quarter": tstat(s),
                "win_ew_mean_pct": float(sub["win_ew"].mean() * 100),
                "t_win_ew": tstat(sub["win_ew"]),
                "rev_mean_pct": float(sub["rev_spread"].mean() * 100)
                if "rev_spread" in sub else None,
                "t_rev": tstat(sub["rev_spread"]) if "rev_spread" in sub else None,
                "avg_n": float(sub["n"].mean()),
                "avg_win": float(sub["n_win"].mean()),
                "avg_los": float(sub["n_los"].mean())}

    out["all"] = agg(d)
    out["IS"] = agg(d[d.seg == "IS_2019_2022"])
    out["OOS"] = agg(d[d.seg == "OOS_2023_2026"])
    mde = (2.8 * out["all"]["sd_q_pct"] / np.sqrt(max(1, out["all"]["n_q"]))
           if out["all"]["n_q"] else None)
    out["mde_pct"] = mde
    p, o = out["all"], out["OOS"]
    crit = {
        "1_mean_t": bool(p["mean_spread_pct"] > 0 and p["t_quarter"] is not None
                         and p["t_quarter"] >= 2.5),
        "2_oos": bool(o["mean_spread_pct"] > 0 and o["t_quarter"] is not None
                      and o["t_quarter"] >= 1.5),
        "3_n": bool(min(p["avg_win"], p["avg_los"]) >= 200 and p["n_q"] >= 20),
        "4_mde": bool(p["mean_spread_pct"] >= max(mde or 99, 0.0)),
    }
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(crit.values()) else "FAIL"
    d.to_csv("logs/window_dressing_quarters.csv", index=False)
    with open("logs/window_dressing_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print("\nper-quarter:")
    print(d[["qe", "n", "win", "los", "ew", "spread", "spread_net",
             "rev_spread"]].to_string(index=False))


if __name__ == "__main__":
    main()
