"""指数调仓强制买卖 — 真实生效日修正重验(2026-10-20, 账本 N=53)。

预注册: logs/prereg_index_rebalance_fixed_20261020.md (出数前锁)
- 事件: PIT 权重 5末→6末 / 11末→12末 成分差; 生效日=第二个星期五的下一交易日。
- 主臂: 调入票 T-1 收盘买 -> T 收盘卖; 同窗等权全宇宙基准; 成本 0.33%。
- 统计: 15 个事件的逐事件均值, 事件级 t; IS/OOS。
- 诊断: T-10->T-1 / T->T+20 / 调入-调出配对差。
用法: python scripts/index_rebalance_fixed.py
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
    print(f"半年调样事件 {len(raw)} 个; 调入 {sum(len(a) for _,_,a,_ in raw)} / "
          f"调出 {sum(len(r) for _,_,_,r in raw)}")

    sh = load_daily("SH000001", "2018-06-01", END)
    cal = sorted(pd.to_datetime(sh["date"]).dt.strftime("%Y-%m-%d").tolist())
    calset = set(cal)

    def next_td(d):
        j = bisect.bisect_right(cal, d)
        return cal[j] if j < len(cal) else None

    events = []
    for d0, d1, add, rem in raw:
        eff = next_td(second_friday(d1.year, d1.month).strftime("%Y-%m-%d"))
        if eff is None or eff not in calset:
            continue
        i = cal.index(eff)
        if i < 25 or i + 20 >= len(cal):
            continue
        events.append({"snap0": d0.strftime("%Y-%m-%d"),
                       "snap1": d1.strftime("%Y-%m-%d"),
                       "T": eff, "add": add, "rem": rem,
                       "T_1": cal[i - 1], "T_2": cal[i - 2],
                       "T_10": cal[i - 10], "T20": cal[i + 20]})
    print(f"可用事件 {len(events)} 个")

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
    pidx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(panel.index)}
    print(f"面板 {panel.shape[0]}天×{panel.shape[1]}只")

    def px(code, dt):
        try:
            return float(panel.at[pd.Timestamp(dt), code])
        except Exception:
            return np.nan

    rows = []
    counts = {"rows_raw": 0, "no_bar": 0, "limit_up_entry": 0,
              "limit_down_exit": 0, "jump": 0, "used": 0}
    for e in events:
        T, T1, T2, T10, T20 = e["T"], e["T_1"], e["T_2"], e["T_10"], e["T20"]
        for tag, lst in (("add", e["add"]), ("rem", e["rem"])):
            for code in lst:
                counts["rows_raw"] += 1
                p10, p2, p1 = px(code, T10), px(code, T2), px(code, T1)
                p, p20 = px(code, T), px(code, T20)
                if not (p1 == p1 and p == p) or not (p1 > 0 and p > 0):
                    counts["no_bar"] += 1; continue
                band = band_of(code)
                # T-1 收盘涨停=买不到(基于 T-2 收盘); T 收盘跌停=卖不到
                if p2 == p2 and p2 > 0 and p1 >= round(p2 * (1 + band), 2) - 1e-6:
                    counts["limit_up_entry"] += 1; continue
                if p <= round(p1 * (1 - band), 2) + 1e-6:
                    counts["limit_down_exit"] += 1; continue
                if abs(p / p1 - 1) > 0.5:
                    counts["jump"] += 1; continue
                counts["used"] += 1
                rows.append({"event": T, "side": tag, "code": code,
                             "ret": p / p1 - 1,
                             "pre": (p1 / p10 - 1) if p10 == p10 and p10 > 0 else np.nan,
                             "post": (p20 / p - 1) if p20 == p20 and p > 0 else np.nan})
    df = pd.DataFrame(rows)
    df["year"] = pd.to_datetime(df["event"]).dt.year
    df["seg"] = np.where(df["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
    # 逐事件基准(用事件映射)
    bmap = {e["T"]: {"bench": float((panel.loc[pd.Timestamp(e["T"])] /
                                     panel.loc[pd.Timestamp(e["T_1"])] - 1).dropna().mean()),
                     "bench_pre": float((panel.loc[pd.Timestamp(e["T_1"])] /
                                         panel.loc[pd.Timestamp(e["T_10"])] - 1).dropna().mean()),
                     "bench_post": float((panel.loc[pd.Timestamp(e["T20"])] /
                                          panel.loc[pd.Timestamp(e["T"])] - 1).dropna().mean())}
            for e in events}
    df["bench"] = df["event"].map(lambda t: bmap[t]["bench"])
    df["bench_pre"] = df["event"].map(lambda t: bmap[t]["bench_pre"])
    df["bench_post"] = df["event"].map(lambda t: bmap[t]["bench_post"])

    out = {"prereg": "logs/prereg_index_rebalance_fixed_20261020.md",
           "events": len(events), "counts": counts,
           "n_add": int((df.side == "add").sum()),
           "n_rem": int((df.side == "rem").sum()), "stats": {}}

    def evstats(sub, cost):
        d = sub[sub.side == "add"].copy()
        if d.empty:
            return {"n_events": 0}
        d["ex"] = d["ret"] - d["bench"] - cost
        ev = d.groupby("event")["ex"].mean()
        n = len(ev)
        t = float(ev.mean() / (ev.std() / np.sqrt(n))) if n > 1 and ev.std() > 0 else None
        return {"n_events": int(n), "n": int(len(d)),
                "mean_pct": float(d["ex"].mean() * 100),
                "median_pct": float(d["ex"].median() * 100),
                "win_pct": float((d["ex"] > 0).mean() * 100),
                "event_mean_pct": float(ev.mean() * 100),
                "event_sd_pct": float(ev.std() * 100),
                "t": t,
                "event_means": {k: round(float(v * 100), 3) for k, v in ev.items()}}

    for label, sub in (("all", df), ("IS", df[df.seg == "IS_2019_2022"]),
                       ("OOS", df[df.seg == "OOS_2023_2026"])):
        out["stats"][label] = {str(c): evstats(sub, c) for c in COSTS}
        # 调出侧(诊断)
        r = sub[sub.side == "rem"].copy()
        if not r.empty:
            r["ex"] = r["ret"] - r["bench"] - PRIMARY_COST
            out["stats"][label]["rem_primary"] = {
                "n": int(len(r)), "mean_pct": float(r["ex"].mean() * 100)}
    # 诊断: 预/后窗(全样本)
    a = df[df.side == "add"].copy()
    for lab, rcol, bcol in (("pre_T10_T1", "pre", "bench_pre"),
                            ("post_T_T20", "post", "bench_post")):
        x = (a[rcol] - a[bcol]).dropna()
        ev = a.assign(x=a[rcol] - a[bcol]).dropna(subset=["x"]).groupby("event")["x"].mean()
        t = float(ev.mean() / (ev.std() / np.sqrt(len(ev)))) if len(ev) > 1 and ev.std() > 0 else None
        out["stats"][lab] = {"n": int(len(x)), "n_events": int(len(ev)),
                             "mean_pct": float(x.mean() * 100),
                             "t": t}
    # 判据
    p = out["stats"]["all"][str(PRIMARY_COST)]
    o = out["stats"]["OOS"][str(PRIMARY_COST)]
    crit = {
        "1_mean_t": bool(p.get("mean_pct", -9) > 0 and p.get("t") is not None and p["t"] >= 2.5),
        "2_oos_t": bool(o.get("mean_pct", -9) > 0 and o.get("t") is not None and o["t"] >= 1.5),
        "3_n": bool(p.get("n", 0) >= 500),
        "4_mde_cost": bool(p.get("mean_pct", -9) >= max(0.73, 0.33)),
    }
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(crit.values()) else "FAIL"
    print(json.dumps(out["stats"], ensure_ascii=False, indent=1, default=str)[:4000])
    print("criteria", crit, "verdict", out["verdict"])
    with open("/root/quant/logs/index_rebalance_fixed_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    df.to_csv("/root/quant/logs/index_rebalance_fixed_20261020.csv", index=False)


if __name__ == "__main__":
    main()
