"""H-W1 洗盘事件收益检验（预注册 N=79）— 2026-10-25。

纪律：
- 必须先有 census + MDE 锁定文件（`--census` 内含 `mde_cluster_pp`），
  否则拒绝运行（fail-fast，闸门五）；
- OOS 只用一次（2023-01-01 起）；不在 OOS 上调参；
- 事件/对照的收益差用同日 × 前期60日收益十分位 × 流通市值五分位匹配；
- 成本 0.25% 往返对事件与对照同扣，差异里抵消；报告绝对超额时已扣。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from data.storage import load_daily  # noqa: E402

CACHE_DIR = ROOT / "data_store" / "meta" / "float_shares"
WEIGHTS = ROOT / "data_store" / "meta" / "csi800_pit_weights.parquet"
LOG_DIR = ROOT / "logs"
COST = 0.0025


def load_membership():
    w = pd.read_parquet(WEIGHTS)
    w["trade_date"] = pd.to_datetime(w["trade_date"]).dt.normalize()
    w["code"] = w["code"].astype(str).str.zfill(6)
    snap = {d: set(g["code"]) for d, g in w.groupby("trade_date")}
    return snap, sorted(snap)


def members_at(ts, snap, snap_dates):
    i = np.searchsorted(np.array(snap_dates, dtype="datetime64[ns]"),
                        np.datetime64(ts), side="right") - 1
    return snap[snap_dates[i]] if i >= 0 else set()


def float_cached(code):
    p = CACHE_DIR / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        d = pd.read_parquet(p)
        d["date"] = pd.to_datetime(d["date"])
        return d
    except Exception:
        return None


def build_panel(codes, end):
    frames = []
    for i, code in enumerate(codes, 1):
        d = load_daily(code, "2018-06-01", end)
        if d is None or len(d) < 120:
            continue
        d = d.copy()
        d["date"] = pd.to_datetime(d["date"])
        d = d.sort_values("date").reset_index(drop=True)
        close = pd.to_numeric(d["close"], errors="coerce")
        op = pd.to_numeric(d["open"], errors="coerce")
        fs = float_cached(code)
        if fs is not None and len(fs):
            f = fs.drop_duplicates("date").sort_values("date")
            d = pd.merge_asof(d, f, on="date", direction="backward")
            float_sh = pd.to_numeric(d["float_shares"], errors="coerce")
        else:
            float_sh = pd.Series(np.nan, index=d.index)
        nxt = op.shift(-1)
        x = pd.DataFrame({
            "date": d["date"], "code": code,
            "close": close, "next_open": nxt,
            "ret60": close / close.shift(60) - 1,
            "fwd20": close.shift(-20) / nxt - 1,
            "fwd60": close.shift(-60) / nxt - 1,
            "mcap": close * float_sh,
            "amount": pd.to_numeric(d["amount"], errors="coerce"),
        })
        frames.append(x)
        if i % 200 == 0:
            print(f"panel {i}/{len(codes)}", flush=True)
    return pd.concat(frames, ignore_index=True)


def build_bench(end):
    b = load_daily("000906", "2018-06-01", end)
    b = b.copy()
    b["date"] = pd.to_datetime(b["date"])
    b = b.sort_values("date").reset_index(drop=True)
    close = pd.to_numeric(b["close"], errors="coerce")
    op = pd.to_numeric(b["open"], errors="coerce")
    nxt = op.shift(-1)
    return pd.DataFrame({
        "date": b["date"],
        "b_fwd20": close.shift(-20) / nxt - 1,
        "b_fwd60": close.shift(-60) / nxt - 1,
    })


def monthly_stats(ev, col):
    """事件按月聚类的 bootstrap：重采样事件月，返回均值/SE/t/CI。"""
    m = ev.dropna(subset=[col]).copy()
    if len(m) == 0:
        return None
    m["month"] = m["date"].str[:7]
    groups = [g[col].to_numpy(float) for _, g in m.groupby("month")]
    rng = np.random.default_rng(20261025)
    boots = []
    for _ in range(2000):
        pick = rng.integers(0, len(groups), len(groups))
        vals = np.concatenate([groups[i] for i in pick])
        boots.append(vals.mean())
    boots = np.array(boots)
    sd = float(m[col].std(ddof=1))
    return {
        "n_events": int(len(m)), "n_months": len(groups),
        "mean": float(m[col].mean()), "median": float(m[col].median()),
        "win_rate": float((m[col] > 0).mean()),
        "naive_t": float(m[col].mean() / (sd / np.sqrt(len(m)))) if sd > 0 else None,
        "boot_se": float(boots.std(ddof=1)),
        "boot_t": float(boots.mean() / boots.std(ddof=1)) if boots.std(ddof=1) > 0 else None,
        "boot_ci95": [float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(LOG_DIR / "washout_census_events_20261025.csv"))
    ap.add_argument("--census", default=str(LOG_DIR / "prereg_washout_census_20261025.json"))
    ap.add_argument("--end", default="2026-10-23")
    ap.add_argument("--is-end", default="2022-12-31")
    ap.add_argument("--oos-start", default="2023-01-01")
    args = ap.parse_args()

    cpath = Path(args.census)
    if not cpath.exists():
        raise SystemExit(f"缺 MDE 锁定文件 {cpath}：先跑 census 并写入 MDE，禁止先看收益")
    cen = json.loads(cpath.read_text())
    mde = cen.get("mde_cluster_pp")
    if mde is None:
        raise SystemExit("census 文件缺 mde_cluster_pp，拒绝运行")

    ev = pd.read_csv(args.events, dtype={"code": str})
    ev["code"] = ev["code"].str.zfill(6)
    ev["date"] = pd.to_datetime(ev["date"]).dt.normalize()
    ev = ev[ev["mature_60"] & ~ev["limit_up_t1"]].copy()
    print(f"events {len(ev)} (mature & tradable)")

    snap, snap_dates = load_membership()
    codes = sorted(set().union(*snap.values()))
    panel = build_panel(codes, args.end)
    bench = build_bench(args.end)
    panel = panel.merge(bench, on="date", how="left")
    panel["ex60"] = panel["fwd60"] - panel["b_fwd60"] - COST
    panel["ex20"] = panel["fwd20"] - panel["b_fwd20"] - COST

    event_dates = sorted(ev["date"].unique())
    rows = []
    for td in event_dates:
        td = pd.Timestamp(td)
        mem = members_at(td, snap, snap_dates)
        day = panel[(panel["date"] == td) & (panel["code"].isin(mem))].copy()
        day = day.dropna(subset=["ex60", "ret60", "mcap"])
        if len(day) < 50:
            continue
        day["dec"] = pd.qcut(day["ret60"], 10, labels=False, duplicates="drop")
        day["quint"] = pd.qcut(day["mcap"], 5, labels=False, duplicates="drop")
        ev_day = ev[ev["date"] == td]
        event_codes = set(ev_day["code"])
        for _, e in ev_day.iterrows():
            row = day[day["code"] == e["code"]]
            if len(row) == 0:
                continue
            row = row.iloc[0]
            cell = day[(day["dec"] == row["dec"]) & (day["quint"] == row["quint"])
                       & (~day["code"].isin(event_codes))]
            used = "dec+quint"
            if len(cell) < 5:
                cell = day[(day["dec"] == row["dec"]) & (~day["code"].isin(event_codes))]
                used = "dec"
            if len(cell) < 5:
                cell = day[~day["code"].isin(event_codes)]
                used = "all"
            rows.append({
                "code": e["code"], "date": td.strftime("%Y-%m-%d"),
                "ex60": float(row["ex60"]) * 100,
                "ex20": float(row["ex20"]) * 100 if pd.notna(row["ex20"]) else np.nan,
                "ctrl_ex60": float(cell["ex60"].mean()) * 100,
                "ctrl_n": int(len(cell)), "match": used,
                "d60": (float(row["ex60"]) - float(cell["ex60"].mean())) * 100,
                "d20": (float(row["ex20"]) - float(cell["ex20"].mean())) * 100
                if pd.notna(row["ex20"]) else np.nan,
                "prior_leg": e["prior_leg"], "cum_turnover": e["cum_turnover"],
                "days_since_high": e["days_since_high"], "dd": e["dd"],
            })
    res = pd.DataFrame(rows)
    if len(res) == 0:
        raise SystemExit("无可用事件（检查 panel/membership）")
    res["month"] = res["date"].str[:7]
    res.to_csv(LOG_DIR / "washout_event_results_20261025.csv", index=False)

    is_ev = res[res["date"] <= args.is_end]
    oos_ev = res[res["date"] >= args.oos_start]
    out = {
        "as_of": args.end, "mde_cluster_pp": mde,
        "cost_roundtrip": COST,
        "n_total": int(len(res)),
        "overall": {c: monthly_stats(res, c) for c in ["d60", "d20", "ex60"]},
        "IS": {c: monthly_stats(is_ev, c) for c in ["d60", "d20", "ex60"]},
        "OOS": {c: monthly_stats(oos_ev, c) for c in ["d60", "d20", "ex60"]},
        "match_usage": res["match"].value_counts().to_dict(),
        "by_year": res.groupby(res["date"].str[:4])["d60"].agg(["count", "mean"]).round(4).to_dict("index"),
    }
    oos = out["OOS"]["d60"]
    is_ = out["IS"]["d60"]
    if oos is None or is_ is None:
        verdict = "INSUFFICIENT"
    elif is_["mean"] > 0 and oos["mean"] >= mde:
        verdict = "PASS"
    elif is_["mean"] <= 0 or oos["mean"] <= 0:
        verdict = "FAIL"
    else:
        verdict = "UNDERPOWERED"
    cap = cen.get("verdict_cap")
    if cap == "UNDERPOWERED" and verdict == "PASS":
        verdict = "UNDERPOWERED"
    out["verdict"] = verdict
    out["verdict_cap"] = cap
    p = LOG_DIR / "washout_event_study_result_20261025.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: out[k] for k in ["n_total", "mde_cluster_pp", "overall", "IS", "OOS", "verdict"]},
                     ensure_ascii=False, indent=2, default=str))
    print("->", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
