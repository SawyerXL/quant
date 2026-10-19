"""R1c 可执行变体(2026-10-19 预注册后出数): T+1开买 → T+2开/收卖.

预注册: logs/prereg_r1c_executable_20261019.md (2026-10-19 22:39:18 CST)
判据(出数前锁):
  - P 臂(主): 成本0.33%, 日均超额>0 且日聚类 t≥2.5;
    OOS 2023-2026 同号且 t≥1.5.
  - S 臂(仅参考): 不得用来救 P 臂.
  - 顺延: T+2 开盘(或收盘)跌停封死 → T+3 开盘卖, 计入样本.
背景: R1b 的 T+1 当日买当日卖违反 A 股 T+1 交收, 不可执行。
用法: python scripts/limitup_overnight_variants.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2019-01-01", "2026-09-30"
COSTS = (0.0033, 0.0023, 0.0053)   # 主口径 0.33% = 佣金0.13% + 滑点0.2%
PRIMARY_COST = 0.0033
REGISTERED_AT = "2026-10-19T22:39:18+08:00"


def band_of(code):
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _stats(ret, bench, cost, valid, mdate, delayed):
    x = pd.DataFrame({"ret": ret, "bench": bench, "mdate": mdate,
                      "delayed": delayed})[valid].dropna(subset=["ret", "bench"])
    if x.empty:
        return {"n": 0, "n_days": 0, "mean_excess_pct": None, "t_day": None}
    x["excess"] = x["ret"] - x["bench"] - cost
    daily = x.groupby("mdate")["excess"].mean()
    n, nd = len(x), len(daily)
    t_day = float(daily.mean() / (daily.std() / np.sqrt(nd))) if nd > 1 and daily.std() > 0 else None
    eq = (1 + daily).cumprod()
    dd = float((eq / eq.cummax() - 1).min()) if len(eq) else None
    dl = x[x["delayed"]]
    return {
        "n": int(n), "n_days": int(nd),
        "mean_excess_pct": float(x["excess"].mean() * 100),
        "median_pct": float(x["excess"].median() * 100),
        "win_rate_pct": float((x["excess"] > 0).mean() * 100),
        "t_day_cluster": t_day,
        "max_dd_pct": dd * 100 if dd is not None else None,
        "delayed_n": int(dl["delayed"].sum()),
        "delayed_mean_excess_pct": float(dl["excess"].mean() * 100) if len(dl) else None,
    }


def main():
    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st_map = dict(zip(meta["code"], meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    list_map = dict(zip(meta["code"], meta.get("list_date", pd.Series("", index=meta.index)).astype(str).str[:10]))
    codes = sorted(c for c in meta["code"] if not st_map.get(c, False))

    frames = []
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.sort_values("date")
            close = pd.to_numeric(d["close"], errors="coerce")
            opn = pd.to_numeric(d["open"], errors="coerce")
            high = pd.to_numeric(d["high"], errors="coerce")
            prev = close.shift(1)
            band = band_of(code)
            lim_up = (prev * (1 + band)).round(2)
            frames.append(pd.DataFrame({
                "date": d["date"].values, "code": code,
                "open": opn.values, "close": close.values,
                "prev": prev.values,
                "limit_up": lim_up.values,
                "touch": ((high >= lim_up - 1e-6) & prev.notna()).values,
                "seal": ((close >= lim_up - 1e-6) & prev.notna()).values,
            }))
        except Exception:
            continue

    df = pd.concat(frames, ignore_index=True).sort_values(["code", "date"])
    df["code"] = df["code"].astype("category")
    df = df[(df["open"] > 0) & (df["close"] > 0) & (df["prev"] > 0)]
    ld = df["code"].astype(str).map(list_map)
    age = (pd.to_datetime(df["date"]) - pd.to_datetime(ld, errors="coerce")).dt.days
    df = df[age.fillna(9999) >= 60]

    g = df.groupby("code", sort=False)
    df["open_next"] = g["open"].shift(-1)
    df["open_next2"] = g["open"].shift(-2)
    df["open_next3"] = g["open"].shift(-3)
    df["close_next"] = g["close"].shift(-1)
    df["close_next2"] = g["close"].shift(-2)
    df["band"] = df["code"].astype(str).map(band_of)
    df["limit_up_next"] = (df["close"] * (1 + df["band"])).round(2)
    df["limit_down_next2"] = (df["close_next"] * (1 - df["band"])).round(2)
    df["date_next_s"] = pd.to_datetime(g["date"].shift(-1)).dt.strftime("%Y-%m-%d")

    # 与持仓窗口严格同口径的等权基准(按入场日 D=T+1 聚类)
    df["bench_P"] = df["open_next"] / df["open"] - 1      # D 开 → D+1 开
    df["bench_S"] = df["close_next"] / df["open"] - 1     # D 开 → D+1 收
    df["bench_2d"] = df["open_next2"] / df["open"] - 1    # D 开 → D+2 开(顺延窗口)
    date_s = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    bP = df.groupby(date_s, observed=True)["bench_P"].mean()
    bS = df.groupby(date_s, observed=True)["bench_S"].mean()
    b2 = df.groupby(date_s, observed=True)["bench_2d"].mean()

    ev = df[df["touch"] & ~df["seal"] & df["open_next"].notna()].copy()
    one_word = ev["open_next"] >= ev["limit_up_next"] - 1e-6
    excluded = ev[one_word].copy()
    excluded_ret = excluded["open_next2"] / excluded["open_next"] - 1
    ev = ev[~one_word].copy()

    # P 臂: T+2 开盘卖; 开盘跌停封死 → T+3 开盘
    p_delay = ev["open_next2"].notna() & (ev["open_next2"] <= ev["limit_down_next2"] + 1e-6)
    p_ret = np.where(p_delay, ev["open_next3"] / ev["open_next"] - 1,
                     ev["open_next2"] / ev["open_next"] - 1)
    p_valid = np.where(p_delay, ev["open_next3"].notna(), ev["open_next2"].notna())

    # S 臂: T+2 收盘卖; 收盘跌停封死 → T+3 开盘
    s_delay = ev["close_next2"].notna() & (ev["close_next2"] <= ev["limit_down_next2"] + 1e-6)
    s_ret = np.where(s_delay, ev["open_next3"] / ev["open_next"] - 1,
                     ev["close_next2"] / ev["open_next"] - 1)
    s_valid = np.where(s_delay, ev["open_next3"].notna(), ev["close_next2"].notna())

    ev = ev.merge(bP.rename("bP"), left_on="date_next_s", right_index=True, how="left")
    ev = ev.merge(bS.rename("bS"), left_on="date_next_s", right_index=True, how="left")
    ev = ev.merge(b2.rename("b2"), left_on="date_next_s", right_index=True, how="left")
    # 顺延出场的持仓窗口变成 D开→D+2开, 必须换同窗基准(预注册第三节)
    ev["bench_P_used"] = np.where(p_delay, ev["b2"], ev["bP"])
    ev["bench_S_used"] = np.where(s_delay, ev["b2"], ev["bS"])
    ev["year"] = pd.to_datetime(ev["date"]).dt.year
    ev["seg"] = np.where(ev["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")

    out = {"registered_at": REGISTERED_AT,
           "arm_P": "T+1 open buy → T+2 open sell",
           "arm_S": "T+1 open buy → T+2 close sell",
           "sample": [START, END], "primary_cost": PRIMARY_COST,
           "n_signal_events": int(len(ev) + int(one_word.sum())),
           "one_word_excluded": {"n": int(one_word.sum()),
                                 "mean_ret_pct": float(excluded_ret.mean() * 100)
                                 if len(excluded_ret) else None},
           "results": {}}

    for arm, ret, valid, bcol in (("P", p_ret, p_valid, "bench_P_used"),
                                  ("S", s_ret, s_valid, "bench_S_used")):
        out["results"][arm] = {}
        for cost in COSTS:
            out["results"][arm][f"cost_{cost}"] = _stats(
                ret, ev[bcol].values, cost, valid, ev["date_next_s"].values,
                np.asarray(valid) & np.asarray(p_delay if arm == "P" else s_delay))
        # 分段(主口径)
        seg = {}
        for s in ("IS_2019_2022", "OOS_2023_2026"):
            m = (ev["seg"] == s).values
            seg[s] = _stats(ret[m], ev.loc[m, bcol].values, PRIMARY_COST,
                            np.asarray(valid)[m], ev.loc[m, "date_next_s"].values,
                            np.asarray(valid)[m] & np.asarray((p_delay if arm == "P" else s_delay))[m])
        out["results"][arm]["segments_primary"] = seg

    p = out["results"]["P"][f"cost_{PRIMARY_COST}"]
    oos = out["results"]["P"]["segments_primary"]["OOS_2023_2026"]
    p_pass = (p["mean_excess_pct"] is not None and p["mean_excess_pct"] > 0
              and p["t_day_cluster"] is not None and p["t_day_cluster"] >= 2.5)
    oos_pass = (oos["mean_excess_pct"] is not None and oos["mean_excess_pct"] > 0
                and oos["t_day_cluster"] is not None and oos["t_day_cluster"] >= 1.5)
    out["verdict"] = {
        "P_primary_pass": bool(p_pass), "P_oos_pass": bool(oos_pass),
        "R1_family_status": "候选(需前向执行测量)" if (p_pass and oos_pass) else "关闭",
        "note": "S 臂不得挽救 P 臂; 前向 alpha 在6个月样本下不可测, 前向只能测滑点.",
    }

    rows = []
    for arm in ("P", "S"):
        for cost in COSTS:
            r = out["results"][arm][f"cost_{cost}"]
            rows.append({"arm": arm, "cost": cost, "n": r["n"], "days": r["n_days"],
                         "mean_excess_pct": round(r["mean_excess_pct"], 4)
                         if r["mean_excess_pct"] is not None else None,
                         "t": round(r["t_day_cluster"], 2)
                         if r["t_day_cluster"] is not None else None,
                         "median_pct": round(r["median_pct"], 4)
                         if r["median_pct"] is not None else None,
                         "win%": round(r["win_rate_pct"], 1)
                         if r["win_rate_pct"] is not None else None,
                         "maxDD%": round(r["max_dd_pct"], 2)
                         if r["max_dd_pct"] is not None else None,
                         "delayed": r["delayed_n"]})
    print(pd.DataFrame(rows).to_string(index=False))
    print("\nP 分段(0.33%):")
    for s, r in out["results"]["P"]["segments_primary"].items():
        print(f"  {s}: n={r['n']} days={r['n_days']} "
              f"mean={r['mean_excess_pct']}% t={r['t_day_cluster']}")
    print(f"\n一字板剔除: {out['one_word_excluded']}")
    print(f"判定: P主判据={'PASS' if p_pass else 'FAIL'} "
          f"OOS={'PASS' if oos_pass else 'FAIL'} → {out['verdict']['R1_family_status']}")

    import json
    Path("logs/limitup_overnight_variants.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已保存 logs/limitup_overnight_variants.json")


if __name__ == "__main__":
    main()
