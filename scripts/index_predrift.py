"""指数调仓预漂移（公告后→生效前）— N=54 新假设(2026-10-20)。

预注册: logs/prereg_index_predrift_20261020.md (出数前锁)
- 事件: 15 个半年调样; 调入票长仓。
- 公告日 A = 生效周五 - 14 天(第二个星期五); 入场 E = A 后首个交易日收盘;
  出场 X = 生效周五收盘(T 前一交易日)。成本 0.33% 主口径。
- 判据: 全样本净>0且事件级 t>=2.5; OOS t>=1.5; N>=500; 均值>=MDE 0.94%。
用法: python scripts/index_predrift.py
"""
import bisect
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-10-19"
COSTS = (0.0033, 0.0023, 0.0053)
PRIMARY_COST = 0.0033
MDE = 0.0094
META = Path("/root/quant/data_store/meta")


def band_of(code):
    c = str(code)
    if c.startswith(("8", "4", "92")):
        return 0.30
    if c.startswith(("688", "300", "301")):
        return 0.20
    return 0.10


def second_friday(y, m):
    fr = []
    for day in range(1, 32):
        try:
            x = pd.Timestamp(year=y, month=m, day=day)
        except ValueError:
            break
        if x.weekday() == 4:
            fr.append(x)
    return fr[1]


def main():
    w = pd.read_parquet(META / "csi800_pit_weights.parquet")
    w["trade_date"] = pd.to_datetime(w["trade_date"])
    dates = sorted(w["trade_date"].unique())
    raw = []
    for d0, d1 in zip(dates, dates[1:]):
        s0 = set(w[w.trade_date == d0]["code"].astype(str))
        s1 = set(w[w.trade_date == d1]["code"].astype(str))
        add, rem = sorted(s1 - s0), sorted(s0 - s1)
        if add and rem and d1.month in (6, 12):
            raw.append((d0, d1, add, rem))

    sh = load_daily("SH000001", "2018-06-01", END)
    cal = sorted(pd.to_datetime(sh["date"]).dt.strftime("%Y-%m-%d").tolist())
    calset = set(cal)

    def next_td(d):
        j = bisect.bisect_right(cal, d)
        return cal[j] if j < len(cal) else None

    events = []
    for d0, d1, add, rem in raw:
        eff_fri = second_friday(d1.year, d1.month)
        T = next_td(eff_fri.strftime("%Y-%m-%d"))
        ann = (eff_fri - pd.Timedelta(days=14))
        E = next_td(ann.strftime("%Y-%m-%d"))
        X = eff_fri.strftime("%Y-%m-%d")
        if T is None or E is None or X not in calset or E not in calset:
            continue
        if cal.index(E) >= cal.index(X):
            continue
        events.append({"snap0": d0.strftime("%Y-%m-%d"), "snap1": d1.strftime("%Y-%m-%d"),
                       "ann": ann.strftime("%Y-%m-%d"), "E": E, "X": X, "T": T,
                       "add": add, "rem": rem})
    print(f"事件 {len(events)} 个")
    for e in events:
        print(f"  {e['T']}: A={e['ann']} E={e['E']} X={e['X']} 调入{len(e['add'])} 调出{len(e['rem'])}")

    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st = dict(zip(meta["code"], meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    codes = sorted(c for c in meta["code"] if not st.get(c, False))
    closes = {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy(); d["date"] = pd.to_datetime(d["date"])
            s = pd.to_numeric(d.set_index("date")["close"], errors="coerce").dropna()
            if len(s) >= 250:
                closes[code] = s
        except Exception:
            continue
    panel = pd.DataFrame(closes).sort_index()
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只")

    def px(code, dt):
        try:
            return float(panel.at[pd.Timestamp(dt), code])
        except Exception:
            return np.nan

    rows = []
    counts = {"raw": 0, "no_bar": 0, "limit_up_entry": 0, "limit_down_exit": 0,
              "jump": 0, "used": 0}
    for e in events:
        E, X = e["E"], e["X"]
        iE, iX = cal.index(E), cal.index(X)
        Ep = cal[iE - 1]; Xp = cal[iX - 1]
        # 基准
        cE = panel.loc[pd.Timestamp(E)]; cX = panel.loc[pd.Timestamp(X)]
        bench = float((cX / cE - 1).dropna().mean())
        for tag, lst in (("add", e["add"]), ("rem", e["rem"])):
            for code in lst:
                counts["raw"] += 1
                pE, pX = px(code, E), px(code, X)
                pEp, pXp = px(code, Ep), px(code, Xp)
                if not (pE == pE and pX == pX) or pE <= 0 or pX <= 0:
                    counts["no_bar"] += 1; continue
                band = band_of(code)
                if pEp == pEp and pEp > 0 and pE >= round(pEp * (1 + band), 2) - 1e-6:
                    counts["limit_up_entry"] += 1; continue
                if pXp == pXp and pXp > 0 and pX <= round(pXp * (1 - band), 2) + 1e-6:
                    counts["limit_down_exit"] += 1; continue
                if abs(pX / pE - 1) > 0.5:
                    counts["jump"] += 1; continue
                counts["used"] += 1
                rows.append({"event": e["T"], "ann": e["ann"], "entry": E, "exit": X,
                             "side": tag, "code": code, "ret": pX / pE - 1,
                             "bench": bench})
    df = pd.DataFrame(rows)
    df["year"] = pd.to_datetime(df["event"]).dt.year
    df["seg"] = np.where(df["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
    out = {"prereg": "logs/prereg_index_predrift_20261020.md",
           "events": len(events), "counts": counts,
           "event_dates": [{k: e[k] for k in ("T", "ann", "E", "X")} for e in events],
           "n_add": int((df.side == "add").sum()), "n_rem": int((df.side == "rem").sum()),
           "stats": {}}

    def evstats(sub, cost):
        d = sub[sub.side == "add"].copy()
        if d.empty:
            return {"n_events": 0}
        d["ex"] = d["ret"] - d["bench"] - cost
        ev = d.groupby("event")["ex"].mean()
        n = len(ev)
        t = float(ev.mean() / (ev.std() / np.sqrt(n))) if n > 1 and ev.std() > 0 else None
        return {"n": int(len(d)), "n_events": int(n),
                "mean_pct": float(d["ex"].mean() * 100),
                "median_pct": float(d["ex"].median() * 100),
                "win_pct": float((d["ex"] > 0).mean() * 100),
                "event_mean_pct": float(ev.mean() * 100),
                "event_sd_pct": float(ev.std() * 100),
                "se_pct": float(ev.std() / np.sqrt(n) * 100) if n > 1 else None,
                "t": t,
                "event_means": {k: round(float(v * 100), 3) for k, v in ev.items()}}

    for label, sub in (("all", df), ("IS", df[df.seg == "IS_2019_2022"]),
                       ("OOS", df[df.seg == "OOS_2023_2026"])):
        out["stats"][label] = {str(c): evstats(sub, c) for c in COSTS}
        r = sub[sub.side == "rem"].copy()
        if not r.empty:
            r["ex"] = r["ret"] - r["bench"] - PRIMARY_COST
            out["stats"][label]["rem_primary"] = {
                "n": int(len(r)), "mean_pct": float(r["ex"].mean() * 100)}
    p = out["stats"]["all"][str(PRIMARY_COST)]
    o = out["stats"]["OOS"][str(PRIMARY_COST)]
    crit = {
        "1_mean_t": bool(p.get("mean_pct", -9) > 0 and p.get("t") is not None and p["t"] >= 2.5),
        "2_oos_t": bool(o.get("mean_pct", -9) > 0 and o.get("t") is not None and o["t"] >= 1.5),
        "3_n": bool(p.get("n", 0) >= 500),
        "4_mde_cost": bool(p.get("mean_pct", -9) >= max(MDE * 100, 0.33)),
    }
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(crit.values()) else "FAIL"
    print(json.dumps({k: {c: {x: v[c].get(x) for x in ("n","n_events","mean_pct","median_pct","win_pct","event_mean_pct","event_sd_pct","se_pct","t")}
                          for c in v if c != "rem_primary"} for k, v in out["stats"].items()},
                     ensure_ascii=False, indent=1, default=str))
    print("criteria", crit, "verdict", out["verdict"])
    with open("/root/quant/logs/index_predrift_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    df.to_csv("/root/quant/logs/index_predrift_20261020.csv", index=False)


if __name__ == "__main__":
    main()
