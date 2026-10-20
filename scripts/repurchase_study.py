"""股票回购 PEAD 事件研究(2026-10-20, 预注册 prereg_repurchase_20261020.md)。

- 主臂 A: 回购起始时间 2019-2026-09, 计划金额下限>=5000万; 20 交易日去重取首条。
- 次臂 B: 完成实施, 最新公告日 2019-2026-09, 实际已回购>=5000万; 20 日去重。
- 入场: 事件日之后首个交易日开盘; 出场: 入场后第 20 个交易日收盘。
- 基准: 同窗等权; 成本 0.33%; 日聚类 t; IS/OOS; 60 日诊断。
用法: python scripts/repurchase_study.py
"""
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


def load_panel():
    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st = dict(zip(meta["code"], meta.get("is_st", pd.Series(False, index=meta.index)).astype(bool)))
    codes = sorted(c for c in meta["code"] if not st.get(c, False))
    prices, opens = {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 250:
                continue
            d = d.copy(); d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            cl = pd.to_numeric(d["close"], errors="coerce").dropna()
            op = pd.to_numeric(d["open"], errors="coerce")
            if len(cl) >= 250:
                prices[code] = cl; opens[code] = op.reindex(cl.index)
        except Exception:
            continue
    return pd.DataFrame(prices).sort_index(), pd.DataFrame(opens).sort_index()


def stats(x, cost):
    if x is None or x.empty:
        return {"n": 0}
    x = x.copy()
    x["ex"] = x["ret"] - x["bench"] - cost
    daily = x.groupby("entry")["ex"].mean()
    stock = x.groupby("code")["ex"].mean()
    n, nd, ns = len(x), len(daily), len(stock)
    td = float(daily.mean() / (daily.std() / np.sqrt(nd))) if nd > 1 and daily.std() > 0 else None
    ts = float(stock.mean() / (stock.std() / np.sqrt(ns))) if ns > 1 and stock.std() > 0 else None
    return {"n": int(n), "n_days": int(nd), "n_codes": int(ns),
            "mean_pct": float(x["ex"].mean() * 100),
            "median_pct": float(x["ex"].median() * 100),
            "win_pct": float((x["ex"] > 0).mean() * 100),
            "sd_pct": float(x["ex"].std() * 100),
            "daily_sd_pct": float(daily.std() * 100),
            "t_day": td, "t_stock": ts}


def main():
    rp = pd.read_parquet(META / "repurchase_events.parquet")
    rp["code"] = rp["code"].astype(str).str.zfill(6)
    rp["start"] = pd.to_datetime(rp["回购起始时间"], errors="coerce")
    rp["latest"] = pd.to_datetime(rp["最新公告日期"], errors="coerce")
    rp["amt_lo"] = pd.to_numeric(rp["计划回购金额区间-下限"], errors="coerce")
    rp["actual"] = pd.to_numeric(rp["已回购金额"], errors="coerce")
    rp["ratio_hi"] = pd.to_numeric(rp["占公告前一日总股本比例-上限"], errors="coerce")
    A = rp[(rp["start"] >= START) & (rp["start"] <= "2026-09-30") & (rp["amt_lo"] >= 5e7)].copy()
    B = rp[(rp["实施进度"] == "完成实施") & (rp["latest"] >= START) &
           (rp["latest"] <= "2026-09-30") & (rp["actual"] >= 5e7)].copy()
    print(f"A raw {len(A)}; B raw {len(B)}")

    panel, opens = load_panel()
    pidx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(panel.index)}
    print(f"panel {panel.shape[0]}天×{panel.shape[1]}只")
    cl = panel.replace(0, np.nan)
    op = opens.reindex(cl.index).replace(0, np.nan)
    prev = cl.shift(1)
    band = pd.Series({c: band_of(c) for c in cl.columns})
    lim_up = prev.mul(1 + band, axis=1).round(2)
    lim_dn = prev.mul(1 - band, axis=1).round(2)
    ret = cl.pct_change()
    jump20 = ret.abs().rolling(20).max().shift(-19)
    jump60 = ret.abs().rolling(60).max().shift(-59)
    bench20 = (cl.shift(-19) / op - 1).mean(axis=1)
    bench60 = (cl.shift(-59) / op - 1).mean(axis=1)

    def dedupe_20(df, datecol):
        kept = []
        for _, g in df.sort_values(datecol).groupby("code"):
            last = None
            for r in g.to_dict("records"):
                A_ = pd.Timestamp(r[datecol])
                j = int(cl.index.searchsorted(A_, side="left"))
                if j >= len(cl.index):
                    continue
                if last is None or j - last >= 20:
                    kept.append(r); last = j
        return pd.DataFrame(kept)

    def build(df, datecol, hold):
        counts = {"events": 0, "no_entry": 0, "no_exit": 0, "limit_up": 0,
                  "limit_dn": 0, "jump": 0, "used": 0}
        rows = []
        b = bench20 if hold == 20 else bench60
        jm = jump20 if hold == 20 else jump60
        for r in df.to_dict("records"):
            code = r["code"]; A_ = pd.Timestamp(r[datecol])
            if code not in cl.columns:
                counts["no_entry"] += 1; continue
            j = int(cl.index.searchsorted(A_, side="left"))
            if j >= len(cl.index):
                counts["no_entry"] += 1; continue
            E_i = j + 1; X_i = E_i + hold - 1
            if X_i >= len(cl.index):
                counts["no_exit"] += 1; continue
            E = cl.index[E_i]; X = cl.index[X_i]
            pE = op.at[E, code]; pX = cl.at[X, code]
            if pd.isna(pE) or pd.isna(pX) or pE <= 0 or pX <= 0:
                counts["no_entry"] += 1; continue
            lu = lim_up.at[E, code]; ld = lim_dn.at[X, code]
            if lu == lu and pE >= lu - 1e-6:
                counts["limit_up"] += 1; continue
            if ld == ld and pX <= ld + 1e-6:
                counts["limit_dn"] += 1; continue
            jv = jm.at[E, code]
            if jv == jv and jv > 0.5:
                counts["jump"] += 1; continue
            bv = b.at[E]
            if bv != bv:
                counts["no_exit"] += 1; continue
            counts["events"] += 1
            rows.append({"code": code, "A": A_.strftime("%Y-%m-%d"),
                         "entry": E.strftime("%Y-%m-%d"), "exit": X.strftime("%Y-%m-%d"),
                         "ret": pX / pE - 1, "bench": float(bv)})
            counts["used"] += 1
        return pd.DataFrame(rows), counts

    out = {"prereg": "logs/prereg_repurchase_20261020.md",
           "A_raw": int(len(A)), "B_raw": int(len(B)), "stats": {}}
    for name, df, datecol in (("A", dedupe_20(A, "start"), "start"),
                              ("B", dedupe_20(B, "latest"), "latest")):
        res20, c20 = build(df, datecol, 20)
        res60, c60 = build(df, datecol, 60)
        res20["year"] = pd.to_datetime(res20["entry"]).dt.year
        res20["seg"] = np.where(res20["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
        out[name] = {"counts20": c20, "counts60": c60, "n_dedup": int(len(df))}
        out["stats"][name] = {}
        for label, sub in (("all", res20), ("IS", res20[res20.seg == "IS_2019_2022"]),
                           ("OOS", res20[res20.seg == "OOS_2023_2026"])):
            out["stats"][name][label] = {str(c): stats(sub, c) for c in COSTS}
        out["stats"][name]["d60"] = stats(res60, PRIMARY_COST)
        res20.to_csv(f"/root/quant/logs/repurchase_{name}_returns.csv", index=False)
        print(f"{name}: counts20 {c20}")
        for label in ("all", "IS", "OOS"):
            s = out["stats"][name][label][str(PRIMARY_COST)]
            print(f"  {label}: n={s.get('n')} days={s.get('n_days')} mean={s.get('mean_pct')}% "
                  f"median={s.get('median_pct')}% win={s.get('win_pct')}% "
                  f"t_day={s.get('t_day')} t_stock={s.get('t_stock')}")
        print(f"  60d: {out['stats'][name]['d60']}")
    p = out["stats"]["A"]["all"][str(PRIMARY_COST)]
    o = out["stats"]["A"]["OOS"][str(PRIMARY_COST)]
    mde = 2.8 * p["daily_sd_pct"] / np.sqrt(max(1, p.get("n_days", 1))) if p.get("n") else None
    out["mde_pct"] = mde
    crit = {
        "1_mean_t": bool(p.get("mean_pct", -99) > 0 and p.get("t_day") is not None and p["t_day"] >= 2.5),
        "2_oos": bool(o.get("mean_pct", -99) > 0 and o.get("t_day") is not None and o["t_day"] >= 1.5),
        "3_n": bool(p.get("n", 0) >= 500 and p.get("n_days", 0) >= 200),
        "4_mde": bool(p.get("mean_pct", -99) >= max(mde or 99, 0.33)),
    }
    out["criteria_A"] = crit
    out["verdict_A"] = "PASS" if all(crit.values()) else "FAIL"
    pb = out["stats"]["B"]["all"][str(PRIMARY_COST)]
    ob = out["stats"]["B"]["OOS"][str(PRIMARY_COST)]
    critB = {
        "1_mean_t": bool(pb.get("mean_pct", -99) > 0 and pb.get("t_day") is not None and pb["t_day"] >= 2.5),
        "2_oos": bool(ob.get("mean_pct", -99) > 0 and ob.get("t_day") is not None and ob["t_day"] >= 1.5),
        "3_n": bool(pb.get("n", 0) >= 500),
    }
    out["criteria_B"] = critB
    out["verdict_B"] = "PASS" if all(critB.values()) else "FAIL"
    print("MDE:", round(mde, 2) if mde else None,
          "A", crit, out["verdict_A"], "B", critB, out["verdict_B"])
    with open("/root/quant/logs/repurchase_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
