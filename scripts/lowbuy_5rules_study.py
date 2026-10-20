"""“低吸5绝招”日线可测子集 — 证伪检验（2026-10-20）。

预注册：logs/prereg_lowbuy_5rules_20261020.md（判据出数前锁）。
主检验 = 低位 + 横盘>=4月 + 红肥绿瘦 + 单阳不破；T+1 开盘 -> 20 交易日收盘。
用法：python scripts/lowbuy_5rules_study.py
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
    cl, op, low, vol = {}, {}, {}, {}
    for code in codes:
        try:
            d = load_daily(code, START, END)
            if d is None or d.empty or len(d) < 300:
                continue
            d = d.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            c = pd.to_numeric(d["close"], errors="coerce")
            good = c.dropna()
            if len(good) < 300:
                continue
            cl[code] = good
            op[code] = pd.to_numeric(d["open"], errors="coerce").reindex(good.index)
            low[code] = pd.to_numeric(d["low"], errors="coerce").reindex(good.index)
            vol[code] = pd.to_numeric(d["volume"], errors="coerce").reindex(good.index)
        except Exception:
            continue
    return (pd.DataFrame(cl).sort_index(), pd.DataFrame(op).sort_index(),
            pd.DataFrame(low).sort_index(), pd.DataFrame(vol).sort_index())


def stats(rows, cost, ret_col="ret", bench_col="bench", day_col="entry"):
    if rows is None or len(rows) == 0:
        return {"n": 0}
    x = rows.copy()
    x["ex"] = x[ret_col] - x[bench_col] - cost
    daily = x.groupby(day_col)["ex"].mean()
    stock = x.groupby("code")["ex"].mean()
    n, nd, ns = len(x), len(daily), len(stock)
    td = (float(daily.mean() / (daily.std() / np.sqrt(nd)))
          if nd > 1 and daily.std() > 0 else None)
    ts = (float(stock.mean() / (stock.std() / np.sqrt(ns)))
          if ns > 1 and stock.std() > 0 else None)
    return {"n": int(n), "n_days": int(nd), "n_codes": int(ns),
            "mean_pct": float(x["ex"].mean() * 100),
            "median_pct": float(x["ex"].median() * 100),
            "win_pct": float((x["ex"] > 0).mean() * 100),
            "sd_pct": float(x["ex"].std() * 100),
            "daily_sd_pct": float(daily.std() * 100),
            "t_day": td, "t_stock": ts}


def main():
    cl, op, low, vol = load_panel()
    print(f"panel {cl.shape[0]}天×{cl.shape[1]}只")
    ret = cl.pct_change()

    # 条件 A：低位（250 日区间下 30%）
    lo250 = cl.rolling(250, min_periods=220).min()
    hi250 = cl.rolling(250, min_periods=220).max()
    pos = (cl - lo250) / (hi250 - lo250)
    A = pos <= 0.30

    # 条件 B：横盘>=4月（80 日振幅<=20% 且 80 日漂移<=10%）
    hi80 = cl.rolling(80, min_periods=60).max()
    lo80 = cl.rolling(80, min_periods=60).min()
    rng80 = hi80 / lo80 - 1
    drift80 = (cl / cl.shift(80) - 1).abs()
    B = (rng80 <= 0.20) & (drift80 <= 0.10)

    # 条件 C：红肥绿瘦
    upv = vol.where(ret > 0, 0.0).rolling(80, min_periods=60).sum()
    dnv = vol.where(ret < 0, 0.0).rolling(80, min_periods=60).sum()
    C = upv >= 1.5 * dnv

    # 条件 D：单阳不破（过去 20 日内的大阳线，自该日起未跌破其最低价）
    big = (ret >= 0.05).astype(bool)
    D = big.copy()
    for k in range(1, 20):
        same = big.shift(k, fill_value=False)
        held = (low.rolling(k, min_periods=1).min() >= low.shift(k))
        D = D | (same & held.fillna(False))

    masks = {
        "A": A.fillna(False).values,
        "A+B": (A & B).fillna(False).values,
        "A+B+C": (A & B & C).fillna(False).values,
        "primary": (A & B & C & D).fillna(False).values,
    }

    # 前向收益与剔除（信号日 i：T+1 开盘 -> i+20 收盘）
    band = pd.Series({c: band_of(c) for c in cl.columns})
    lim_up = cl.shift(1).mul(1 + band, axis=1).round(2)
    lim_dn = cl.shift(1).mul(1 - band, axis=1).round(2)
    op0 = op.replace(0, np.nan)
    entry_ret = cl.shift(-20) / op0.shift(-1) - 1
    bench = entry_ret.mean(axis=1).values
    op_e = op0.shift(-1).values
    xc = cl.shift(-20).values
    lu = lim_up.shift(-1).values
    ld = lim_dn.shift(-20).values
    jump = ret.abs().rolling(20).max().shift(-20).values
    ret60 = cl.shift(-60) / op0.shift(-1) - 1
    bench60 = ret60.mean(axis=1).values
    ret_v = entry_ret.values
    dates = cl.index

    def collect(mask):
        m = mask
        ki, kj, counts = [], [], {"raw": 0, "no_exit": 0, "limit_up": 0,
                                  "limit_dn": 0, "jump": 0, "used": 0}
        for j in range(m.shape[1]):
            idx = np.flatnonzero(m[:, j])
            if idx.size == 0:
                continue
            counts["raw"] += int(idx.size)
            keep, p = [], 0
            while p < idx.size:
                i = idx[p]
                keep.append(i)
                p = int(np.searchsorted(idx, i + 20, side="left"))
            ki.append(np.asarray(keep, dtype=np.int64))
            kj.append(np.full(len(keep), j, dtype=np.int64))
        ii = np.concatenate(ki) if ki else np.array([], dtype=np.int64)
        jj = np.concatenate(kj) if kj else np.array([], dtype=np.int64)
        r = ret_v[ii, jj]
        o = op_e[ii, jj]
        x = xc[ii, jj]
        valid = np.isfinite(r) & np.isfinite(o) & (o > 0)
        counts["no_exit"] = int((~valid).sum())
        luv = lu[ii, jj]
        m1 = valid & np.isfinite(luv) & (o >= luv - 1e-6)
        counts["limit_up"] = int(m1.sum())
        valid = valid & ~m1
        ldv = ld[ii, jj]
        m2 = valid & np.isfinite(ldv) & np.isfinite(x) & (x <= ldv + 1e-6)
        counts["limit_dn"] = int(m2.sum())
        valid = valid & ~m2
        jv = jump[ii, jj]
        m3 = valid & np.isfinite(jv) & (jv > 0.5)
        counts["jump"] = int(m3.sum())
        valid = valid & ~m3
        counts["used"] = int(valid.sum())
        rows = pd.DataFrame({
            "code": cl.columns.values[jj[valid]],
            "entry": dates[ii[valid] + 1].strftime("%Y-%m-%d"),
            "ret": r[valid],
            "bench": bench[ii[valid]],
        })
        return rows, counts

    out = {"prereg": "logs/prereg_lowbuy_5rules_20261020.md", "masks": {},
           "stats": {}}
    results = {}
    for name, mask in masks.items():
        rows, counts = collect(mask)
        rows["year"] = pd.to_datetime(rows["entry"]).dt.year
        rows["seg"] = np.where(rows["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
        results[name] = rows
        out["masks"][name] = counts
        out["stats"][name] = {}
        for label, sub in (("all", rows), ("IS", rows[rows.seg == "IS_2019_2022"]),
                           ("OOS", rows[rows.seg == "OOS_2023_2026"])):
            out["stats"][name][label] = {str(c): stats(sub, c) for c in COSTS}
        rows.to_csv(f"/root/quant/logs/lowbuy_{name.replace('+', '')}_events.csv",
                    index=False)
        print(f"{name}: {counts}")
        for label in ("all", "IS", "OOS"):
            s = out["stats"][name][label][str(PRIMARY_COST)]
            print(f"  {label}: n={s.get('n')} days={s.get('n_days')} "
                  f"mean={s.get('mean_pct')}% med={s.get('median_pct')}% "
                  f"win={s.get('win_pct')}% t_day={s.get('t_day')} "
                  f"t_stock={s.get('t_stock')}")

    # 主检验判据
    p = out["stats"]["primary"]["all"][str(PRIMARY_COST)]
    o = out["stats"]["primary"]["OOS"][str(PRIMARY_COST)]
    mde = (2.8 * p["daily_sd_pct"] / np.sqrt(max(1, p.get("n_days", 1)))
           if p.get("n") else None)
    out["mde_pct"] = mde
    crit = {
        "1_mean_t": bool(p.get("mean_pct", -99) > 0
                         and p.get("t_day") is not None and p["t_day"] >= 2.5),
        "2_oos": bool(o.get("mean_pct", -99) > 0
                      and o.get("t_day") is not None and o["t_day"] >= 1.5),
        "3_n": bool(p.get("n", 0) >= 500 and p.get("n_days", 0) >= 200),
        "4_mde": bool(p.get("mean_pct", -99) >= max(mde or 99, 0.33)),
    }
    out["criteria"] = crit
    out["verdict"] = "PASS" if all(crit.values()) else "FAIL"

    # 诊断：60 日 + 低价分层
    pr = results["primary"]
    if len(pr):
        pr60 = pr.copy()
        ii = np.array([dates.get_loc(pd.Timestamp(d)) for d in pr["entry"]])
        # 无法直接索引信号日；用事件表的 entry 反推 60 日（重新按信号日收集更稳妥）
        # 简化：只做低价分层
        entry_px = op.shift(-1)
        px = []
        for _, r in pr.iterrows():
            j = cl.columns.get_loc(r["code"])
            i = dates.get_loc(pd.Timestamp(r["entry"])) - 1
            px.append(entry_px.iat[i, j] if i >= 0 else np.nan)
        pr = pr.assign(entry_px=px)
        for lab, sub in (("low_px<=10", pr[pr.entry_px <= 10]),
                         ("high_px>10", pr[pr.entry_px > 10])):
            out.setdefault("diag_px", {})[lab] = stats(sub, PRIMARY_COST)
        out["diag_px"] = out.get("diag_px", {})
        # 60 日诊断
        r60 = []
        for _, r in pr.iterrows():
            j = cl.columns.get_loc(r["code"])
            i = dates.get_loc(pd.Timestamp(r["entry"])) - 1
            if i + 60 < len(dates):
                e = op.iat[i + 1, j] if i + 1 < len(dates) else np.nan
                x = cl.iat[i + 60, j]
                if pd.notna(e) and e > 0 and pd.notna(x):
                    r60.append({"code": r["code"], "entry": r["entry"],
                                "ret": x / e - 1, "bench": bench60[i]})
        out["diag_60d"] = stats(pd.DataFrame(r60), PRIMARY_COST)

    print("MDE:", round(mde, 2) if mde else None, crit, out["verdict"])
    print("diag_px:", json.dumps(out.get("diag_px", {}), ensure_ascii=False)[:500])
    print("diag_60d:", out.get("diag_60d"))
    with open("/root/quant/logs/lowbuy_5rules_20261020.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
