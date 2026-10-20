"""限售解禁 / 大宗折价事件研究(2026-10-19, #21)。

预注册: logs/prereg_unlock_block_20261019.md (判据/窗口/阈值出数前锁)
数据审计: logs/data_audit_index_unlock_block_20261019.md
- H1 解禁 R>=5%: 首个可卖日 E=D 后首个交易日; E+1 开盘买, E+10 收盘卖。
- H2 大宗 折溢率<=-10% 且 额/流通市值>=0.5%: T+1 开盘买, T+5 收盘卖。
- 基准=同窗等权买入持有; 成本主口径 0.33% 双边; 日聚类 t; IS/OOS。
用法: python scripts/unlock_block_event_study.py
"""
import bisect
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.storage import load_daily, load_meta

START, END = "2018-10-01", "2026-10-19"
SAMPLE_START, SAMPLE_END = "2019-01-01", "2026-09-30"
COSTS = (0.0033, 0.0023, 0.0053)
PRIMARY_COST = 0.0033
UNLOCK_MIN_RATIO = 0.05
BLOCK_MAX_DISCOUNT = -0.10
BLOCK_MIN_AMT_FLOAT = 0.005
UNLOCK_HOLD = 10          # 入场日为第 1 个交易日, 出场 = 入场+hold-1
BLOCK_HOLD = 5
META = Path("/root/quant/data_store/meta")


def band_of(code):
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def load_panel():
    meta = load_meta("stock_info_full")
    meta["code"] = meta["code"].astype(str)
    st_map = dict(zip(meta["code"], meta.get(
        "is_st", pd.Series(False, index=meta.index)).astype(bool)))
    list_map = dict(zip(meta["code"], meta.get(
        "list_date", pd.Series("", index=meta.index)).astype(str).str[:10]))
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
            frames.append(pd.DataFrame({
                "code": code, "date": d["date"].values,
                "open": opn.values, "close": close.values,
                "prev": close.shift(1).values,
            }))
        except Exception:
            continue
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["open"] > 0) & (df["close"] > 0) & (df["prev"] > 0)].copy()
    ld = df["code"].map(list_map)
    age = (df["date"] - pd.to_datetime(ld, errors="coerce")).dt.days
    df = df[age.fillna(9999) >= 60].copy()
    df["band"] = df["code"].map(band_of)
    df["limit_up"] = (df["prev"] * (1 + df["band"])).round(2)
    df["limit_down"] = (df["prev"] * (1 - df["band"])).round(2)
    df["jump"] = (df["close"] / df["prev"] - 1).abs()
    # 窗口脏跳: 股票自身 bar 的 [i, i+h-1] 最大值(近似同窗)
    df = df.sort_values(["code", "date"])
    for h, col in ((UNLOCK_HOLD, "jump_max10"), (BLOCK_HOLD, "jump_max5")):
        r = (df.groupby("code")["jump"].rolling(h, min_periods=1).max()
             .reset_index(level=0, drop=True))
        df[col] = df.assign(_r=r).groupby("code")["_r"].shift(-(h - 1))
    return df


def dedup_unlock(u):
    """同一股票 30 自然日内只保留比例最大的一条(按首个事件起 30 日聚类)。"""
    kept = []
    for _, g in u.sort_values("解禁时间").groupby("股票代码"):
        rows = g.to_dict("records")
        i = 0
        while i < len(rows):
            start = rows[i]["解禁时间"]
            cluster = [rows[i]]
            j = i + 1
            while j < len(rows) and (rows[j]["解禁时间"] - start).days <= 30:
                cluster.append(rows[j]); j += 1
            kept.append(max(cluster, key=lambda r: r["占解禁前流通市值比例"]))
            i = j
    return pd.DataFrame(kept)


def dedup_block(b, cal_idx):
    """同一股票 5 个交易日内只保留第一条。"""
    kept = []
    for _, g in b.sort_values("交易日期").groupby("证券代码"):
        last_i = None
        for r in g.to_dict("records"):
            i = cal_idx.get(pd.Timestamp(r["交易日期"]).strftime("%Y-%m-%d"))
            if i is None:
                continue
            if last_i is None or i - last_i >= BLOCK_HOLD:
                kept.append(r); last_i = i
    return pd.DataFrame(kept)


def bench_maps(df, cal, holds):
    """同窗等权基准: date i 入场, close(i+h-1)/open(i)-1 的截面均值。"""
    op = df.pivot(index="date", columns="code", values="open")
    cl = df.pivot(index="date", columns="code", values="close")
    op = op.reindex(pd.to_datetime(cal)); cl = cl.reindex(pd.to_datetime(cal))
    out = {}
    for h in holds:
        out[h] = (cl.shift(-(h - 1)) / op - 1).mean(axis=1)
    return out


def eval_events(ev, df, cal, cal_idx, bench, hold, jump_col):
    """合并入场/出场 bar, 剔除不可成交/脏跳, 返回收益表与计数。"""
    counts = {"candidates": len(ev), "boundary": 0, "no_entry_bar": 0,
              "no_exit_bar": 0, "limit_up_entry": 0, "limit_down_exit": 0,
              "jump": 0, "used": 0}
    if ev.empty:
        return pd.DataFrame(), counts
    entry = df[["code", "date", "open", "limit_up", jump_col]].rename(
        columns={"date": "entry_date"}).copy()
    exit_ = df[["code", "date", "close", "limit_down"]].rename(
        columns={"date": "exit_date"}).copy()
    entry["entry_date"] = pd.to_datetime(entry["entry_date"])
    exit_["exit_date"] = pd.to_datetime(exit_["exit_date"])
    x = ev.copy()
    x["entry_date"] = pd.to_datetime(x["entry_date"], errors="coerce")
    x["exit_date"] = pd.to_datetime(x["exit_date"], errors="coerce")
    x = x.merge(entry, on=["code", "entry_date"], how="left")
    c1 = x["open"].isna()
    counts["no_entry_bar"] = int(c1.sum())
    x = x[~c1]
    x = x.merge(exit_, on=["code", "exit_date"], how="left")
    c2 = x["close"].isna()
    counts["no_exit_bar"] = int(c2.sum())
    x = x[~c2]
    ok_entry = x["open"] < x["limit_up"] - 1e-6
    counts["limit_up_entry"] = int((~ok_entry).sum())
    x = x[ok_entry]
    ok_exit = x["close"] > x["limit_down"] + 1e-6
    counts["limit_down_exit"] = int((~ok_exit).sum())
    x = x[ok_exit]
    ok_jump = ~(x[jump_col].fillna(0) > 0.5)
    counts["jump"] = int((~ok_jump).sum())
    x = x[ok_jump].copy()
    x["ret"] = x["close"] / x["open"] - 1
    x["bench"] = x["entry_date"].map(bench)
    x = x.dropna(subset=["bench"])
    counts["used"] = len(x)
    x["year"] = pd.to_datetime(x["entry_date"]).dt.year
    x["seg"] = np.where(x["year"] <= 2022, "IS_2019_2022", "OOS_2023_2026")
    return x, counts


def stats(x, cost):
    if x is None or x.empty:
        return {"n": 0}
    x = x.copy()
    x["excess"] = x["ret"] - x["bench"] - cost
    daily = x.groupby("entry_date")["excess"].mean()
    n, nd = len(x), len(daily)
    t = float(daily.mean() / (daily.std() / np.sqrt(nd))) if nd > 1 and daily.std() > 0 else None
    eq = (1 + daily).cumprod()
    dd = float((eq / eq.cummax() - 1).min()) if len(eq) else None
    return {"n": int(n), "n_days": int(nd),
            "n_codes": int(x["code"].nunique()),
            "mean_excess_pct": float(x["excess"].mean() * 100),
            "median_pct": float(x["excess"].median() * 100),
            "win_rate_pct": float((x["excess"] > 0).mean() * 100),
            "t_day_cluster": t, "max_dd_pct": dd * 100 if dd is not None else None}


def main():
    u = pd.read_parquet(META / "unlock_events_2019_2026.parquet")
    b = pd.read_parquet(META / "block_trade_events_2019_2026.parquet")
    u["解禁时间"] = pd.to_datetime(u["解禁时间"])
    b["交易日期"] = pd.to_datetime(b["交易日期"])
    df = load_panel()
    print(f"panel {len(df)} rows, {df.code.nunique()} codes")
    sh = load_daily("SH000001", START, END)
    cal = sorted(pd.to_datetime(sh["date"]).dt.strftime("%Y-%m-%d").tolist())
    cal_idx = {d: i for i, d in enumerate(cal)}
    bench = bench_maps(df, cal, (UNLOCK_HOLD, BLOCK_HOLD))

    def next_td(d):
        j = bisect.bisect_right(cal, d)
        return cal[j] if j < len(cal) else None

    # ---- H1 unlock ----
    uu = u[(u["解禁时间"] >= SAMPLE_START) & (u["解禁时间"] <= SAMPLE_END)].copy()
    n_raw = len(uu)
    uu = dedup_unlock(uu)
    n_dedup = len(uu)
    uu = uu[uu["占解禁前流通市值比例"] >= UNLOCK_MIN_RATIO].copy()
    rows_u = []
    n_bound_u = 0
    for r in uu.to_dict("records"):
        D = pd.Timestamp(r["解禁时间"]).strftime("%Y-%m-%d")
        E = next_td(D)
        if E is None:
            n_bound_u += 1; continue
        entry_i = cal_idx[E] + 1
        exit_i = entry_i + UNLOCK_HOLD - 1
        if exit_i >= len(cal):
            n_bound_u += 1; continue
        rows_u.append({**r, "code": r["股票代码"], "event_date": D,
                       "entry_date": cal[entry_i], "exit_date": cal[exit_i]})
    ev_u = pd.DataFrame(rows_u)
    res_u, cnt_u = eval_events(ev_u, df, cal, cal_idx, bench[UNLOCK_HOLD],
                               UNLOCK_HOLD, "jump_max10")
    cnt_u["boundary"] = n_bound_u
    print("unlock raw/dedup/after_ratio", n_raw, n_dedup, len(uu), "counts", cnt_u)

    # ---- H2 block ----
    bb = b[(b["交易日期"] >= SAMPLE_START) & (b["交易日期"] <= SAMPLE_END)].copy()
    n_braw = len(bb)
    bb = bb[(bb["折溢率"] <= BLOCK_MAX_DISCOUNT) &
            (bb["成交总额/流通市值"] >= BLOCK_MIN_AMT_FLOAT)].copy()
    n_bfilt = len(bb)
    bb = dedup_block(bb, cal_idx)
    rows_b = []
    n_bound_b = 0
    for r in bb.to_dict("records"):
        T = pd.Timestamp(r["交易日期"]).strftime("%Y-%m-%d")
        entry = next_td(T)
        if entry is None:
            n_bound_b += 1; continue
        ei = cal_idx[entry]
        xi = ei + BLOCK_HOLD - 1
        if xi >= len(cal):
            n_bound_b += 1; continue
        rows_b.append({**r, "code": r["证券代码"], "event_date": T,
                       "entry_date": entry, "exit_date": cal[xi]})
    ev_b = pd.DataFrame(rows_b)
    res_b, cnt_b = eval_events(ev_b, df, cal, cal_idx, bench[BLOCK_HOLD],
                               BLOCK_HOLD, "jump_max5")
    cnt_b["boundary"] = n_bound_b
    print("block raw/filt/dedup", n_braw, n_bfilt, len(bb), "counts", cnt_b)

    out = {"registered_at": "2026-10-19T23:3x+08:00",
           "data_as_of": str(u["as_of"].iloc[0]),
           "unlock": {"definition": "ratio>=5%, E+1 open -> E+10 close",
                      "n_raw": n_raw, "n_dedup": n_dedup,
                      "n_after_ratio": len(uu), "counts": cnt_u, "stats": {}},
           "block": {"definition": "disc<=-10% & amt/float>=0.5%, T+1 open -> T+5 close",
                     "n_raw": n_braw, "n_after_filt": n_bfilt,
                     "n_dedup": len(bb), "counts": cnt_b, "stats": {}}}
    for name, res in (("unlock", res_u), ("block", res_b)):
        out[name]["stats"]["primary"] = stats(res, PRIMARY_COST)
        out[name]["stats"]["costs"] = {str(c): stats(res, c) for c in COSTS}
        for seg in ("IS_2019_2022", "OOS_2023_2026"):
            out[name]["stats"][seg] = stats(res[res.seg == seg], PRIMARY_COST)
        print(f"\n{name} primary:")
        for k, v in out[name]["stats"]["primary"].items():
            print(f"  {k}: {v}")
        for seg in ("IS_2019_2022", "OOS_2023_2026"):
            s = out[name]["stats"][seg]
            print(f"  {seg}: mean={s.get('mean_excess_pct')} t={s.get('t_day_cluster')} n={s.get('n')}")
        # 描述性分档(不入判据)
        if not res.empty and name == "unlock":
            res["ratio_bin"] = pd.cut(res["占解禁前流通市值比例"],
                                      [0.05, 0.20, 0.50, 1.01],
                                      labels=["5-20%", "20-50%", ">50%"])
            out[name]["stats"]["ratio_bins"] = {
                str(k): stats(v, PRIMARY_COST) for k, v in res.groupby("ratio_bin")}
        if not res.empty and name == "block":
            res["disc_bin"] = pd.cut(res["折溢率"],
                                     [-1, -0.20, -0.15, -0.10],
                                     labels=["<-20%", "-20~-15%", "-15~-10%"])
            out[name]["stats"]["disc_bins"] = {
                str(k): stats(v, PRIMARY_COST) for k, v in res.groupby("disc_bin")}

    allres = pd.concat([res_u, res_b], ignore_index=True)
    allres.to_csv("/root/quant/logs/unlock_block_returns.csv", index=False)
    with open("/root/quant/logs/unlock_block_event_study.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\n-> logs/unlock_block_event_study.json")


if __name__ == "__main__":
    main()
